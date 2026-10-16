# workspace/shell.py
# Desktop shell: a pywebview (WebView2) window showing the bundled UI.
#
# The UI is loaded from a file:// URL. pywebview starts its built-in HTTP
# server (a localhost listener) for plain local paths, but not for file://
# URLs or inline HTML, so this keeps the app free of local listeners. The
# trial (tools/webview_trial.py) verifies that by listing listening sockets.
# private_mode keeps WebView2 from persisting cookies/storage between runs.

import os
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent / "ui"
# The microphone icon the earlier app uses; next to the app code in the package.
ICON = Path(__file__).resolve().parents[1] / "whisper.ico"


# Chromium switches for WebView2: resolve no host names (the page has nothing
# to fetch), and turn off background services that would otherwise contact
# Microsoft (SmartScreen, component updates, spell-check downloads, metrics).
WEBVIEW2_ARGS = " ".join([
    '--host-resolver-rules="MAP * ~NOTFOUND"',
    "--disable-features=msSmartScreenProtection,msEdgeEnhancedSecurityMode,"
    "SpareRendererForSitePerProcess,AutofillServerCommunication",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--no-pings",
    "--disable-sync",
    "--metrics-recording-only",
    "--disable-breakpad",
])


def harden_webview2():
    """Must run before the first window is created (read at WebView2 start)."""
    import os
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = WEBVIEW2_ARGS


def ui_url(page="index.html"):
    return (UI_DIR / page).as_uri()


blocked_navigations = []   # URLs the lock refused (for tests/diagnostics; no content)
blocked_calls = []         # bridge names the dispatch guard refused

# pywebview's own internal message used to deliver evaluate_js promise results.
_PYWEBVIEW_INTERNAL = frozenset({"pywebviewAsyncCallback"})


def _norm_url(uri):
    import urllib.parse
    return urllib.parse.unquote(str(uri)).replace("\\", "/").lower()


def _allowed(uri, page="index.html"):
    """Only the window's own bundled page may be shown; everything else is
    refused, including other files in the UI folder. pywebview injects the
    bridge into whatever page is loaded, so no other page must ever load. A
    #fragment or ?query on the page itself is allowed. Both sides are
    percent-decoded, because Python and Chromium encode characters such as
    ( ) ' ! differently."""
    # Cut the fragment and query before decoding, so an encoded "%23" or
    # "%3F" in the path cannot pass for one.
    target = _norm_url(str(uri).split("#", 1)[0].split("?", 1)[0])
    return target == _norm_url(ui_url(page))


class ShellError(RuntimeError):
    """The window cannot be opened safely (message is safe to show)."""


def _clean_dom_event(param):
    """The only DOM events the app listens to are dragover and drop on the
    document (workspace.api.enable_drop). Rebuild the event from the few
    fields pywebview needs, so a page-supplied pywebviewFullPath never
    survives: pywebview adds the real path only for files the native drop
    handler saw. Returns None for anything else."""
    if not isinstance(param, dict) or param.get("nodeId") != "document":
        return None
    event = param.get("event")
    if not isinstance(event, dict) or event.get("type") not in ("dragover", "drop"):
        return None
    clean = {"type": event["type"]}
    if event["type"] == "drop":
        transfer = event.get("dataTransfer")
        files = transfer.get("files") if isinstance(transfer, dict) else None
        if not isinstance(files, list):
            return None
        clean["dataTransfer"] = {"files": [{"name": f["name"][:260]} for f in files[:1000]
                                           if isinstance(f, dict)
                                           and isinstance(f.get("name"), str)]}
    return {"event": clean, "nodeId": "document"}


def _arguments_fit(window, func_name, param):
    """True if the page's arguments bind to the bridge method's signature.
    pywebview would otherwise call it, catch the TypeError and send the full
    stack trace (with install paths) back to the page."""
    import inspect
    if func_name in _PYWEBVIEW_INTERNAL:
        return True
    func = getattr(getattr(window, "_js_api", None), func_name, None)
    if func is None or not isinstance(param, (list, tuple)):
        return False
    try:
        inspect.signature(func).bind(*param)
    except TypeError:
        return False
    return True


def install_dispatch_guard(allowed):
    """Only let the page call the exact method names in `allowed`.

    pywebview's js_bridge_call resolves the requested name with getattr over
    every dotted segment and no underscore filter, so page script could
    otherwise call "__init__", private attributes or their methods. Must run
    before the WebView2 backend module is imported (it imports the function
    by name), so call it before webview.start()."""
    import sys

    import webview.util as util
    original = getattr(util.js_bridge_call, "__wrapped__", util.js_bridge_call)
    permitted = frozenset(allowed) | _PYWEBVIEW_INTERNAL

    def guarded(window, func_name, param, value_id):
        if not isinstance(func_name, str) or func_name not in permitted:
            blocked_calls.append(str(func_name)[:80])
            return None
        if func_name == "pywebviewEventHandler":
            param = _clean_dom_event(param)
            if param is None:
                blocked_calls.append(func_name)
                return None
        elif window is not None and not _arguments_fit(window, func_name, param):
            # Refused before pywebview calls it; the page's promise stays
            # unresolved, which only a broken or hostile page would notice.
            blocked_calls.append(f"{func_name}(arguments)")
            return None
        return original(window, func_name, param, value_id)

    guarded.__wrapped__ = original
    guarded.unminute_guard = True
    util.js_bridge_call = guarded
    backend = sys.modules.get("webview.platforms.edgechromium")
    if backend is not None:            # already imported: patch its reference too
        backend.js_bridge_call = guarded


# Zoom levels as in Edge (50% to 300%).
ZOOM_STEPS = (0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)


def set_zoom(window, direction):
    """Change the WebView2 zoom by one step ("in", "out") or back to 100%
    ("reset"); returns the new factor. Needed because pywebview turns off the
    browser's own accelerator keys, which include Ctrl+plus and Ctrl+minus
    (Ctrl+wheel still works through zoomable=True)."""
    from System import Action
    form = window.native
    control = form.browser.webview
    box = {}

    def apply():
        now = float(control.ZoomFactor)
        if direction == "reset":
            new = 1.0
        elif direction == "in":
            new = next((z for z in ZOOM_STEPS if z > now + 0.01), ZOOM_STEPS[-1])
        else:
            new = next((z for z in reversed(ZOOM_STEPS) if z < now - 0.01), ZOOM_STEPS[0])
        control.ZoomFactor = new
        box["zoom"] = new

    form.Invoke(Action(apply))          # WebView2 must be used on the UI thread
    return box["zoom"]


def lock_navigation(window, page="index.html"):
    """Cancel any navigation away from the window's bundled page (WebView2
    NavigationStarting). Attached at before_load, i.e. after the first
    (bundled) page navigation, and stays attached to the control for every
    later navigation and reload."""
    control = window.native.browser.webview

    def on_navigation_starting(sender, args):
        uri = str(args.Uri)
        if not _allowed(uri, page):
            args.Cancel = True
            blocked_navigations.append(uri.split("?")[0][:200])

    control.NavigationStarting += on_navigation_starting


def create_window(bridge, title="Unminute", hidden=False, harden=True,
                  page="index.html", allowed=None):
    """`allowed` is the exact set of bridge names the page may call
    (workspace.api.API_METHODS for the workspace, the trial's by default)."""
    import webview

    from workspace.bridge import BRIDGE_METHODS
    if harden:
        harden_webview2()
    install_dispatch_guard(BRIDGE_METHODS if allowed is None else allowed)
    # New-window requests (window.open, target=_blank) must not reach the
    # system browser: load them in place, where the navigation lock refuses them.
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    webview.settings["ALLOW_DOWNLOADS"] = False
    # Otherwise pywebview adds --allow-file-access-from-files to WebView2.
    webview.settings["ALLOW_FILE_URLS"] = False
    window = webview.create_window(title, url=ui_url(page), js_api=bridge,
                                   width=1100, height=760, min_size=(640, 480),
                                   hidden=hidden, text_select=True,
                                   # Ctrl+wheel zoom (#27); pywebview turns it off
                                   # by default. Ctrl+plus/minus/0: set_zoom().
                                   zoomable=True)
    window.unminute_page = page
    window.events.before_load += lambda: _lock_once(window)
    return window


lock_failed = []          # set if a window's navigation lock could not attach


def _lock_once(window):
    if getattr(window, "_nav_locked", False):
        return
    try:
        lock_navigation(window, getattr(window, "unminute_page", "index.html"))
        window._nav_locked = True
    except Exception:
        # Without the lock any page could get the bridge: close instead.
        lock_failed.append(True)
        blocked_navigations.append("navigation lock failed; window closed")
        _close_now(window)


def _post_close(window):
    """Ask the window's form to close once the current UI callback returns
    (before_load runs on the UI thread, where window.destroy() would wait)."""
    from System import Action
    form = window.native
    form.BeginInvoke(Action(form.Close))


def _close_now(window):
    try:
        _post_close(window)
    except Exception:
        os._exit(3)              # cannot even close it: end the process, never run unlocked


def check_backend():
    """Fail closed before any window opens. pywebview silently falls back to
    the old Internet Explorer engine (MSHTML) when it cannot find WebView2;
    that engine ignores the page's Content-Security-Policy and has no
    navigation lock. The dispatch guard must also be in place in the WebView2
    backend, which imports the bridge entry point by name."""
    import importlib
    import sys

    try:
        # webview/__init__ has a global named guilib that hides the module.
        guilib = importlib.import_module("webview.guilib")
        backend = guilib.initialize("edgechromium")
    except Exception as exc:     # e.g. pythonnet or .NET could not load
        raise ShellError("Unminute could not start its window: a Windows component it "
                         "needs (WebView2 or .NET) could not be loaded. Ask your IT team.") from exc
    if getattr(backend, "renderer", None) != "edgechromium":
        raise ShellError("Unminute needs the Microsoft Edge WebView2 Runtime, which was not "
                         "found on this computer. Ask your IT team to install it.")
    chromium = sys.modules.get("webview.platforms.edgechromium")
    if not getattr(getattr(chromium, "js_bridge_call", None), "unminute_guard", False):
        raise ShellError("Unminute could not secure its window and will not start. "
                         "Reinstall it, or ask your IT team.")


def _is_link(path):
    return path.is_symlink() or os.path.isjunction(path)


def _remove_link(path):
    """Remove a symbolic link or junction itself, never what it points to."""
    try:
        os.rmdir(path)           # a junction or directory link
    except NotADirectoryError:
        os.unlink(path)          # a file link


def storage_folder(root):
    """A new, empty WebView2 profile folder inside the workspace, after
    removing any left by an earlier run that did not close cleanly. In
    private mode pywebview deletes the folder on a clean close; without this
    a crash would leave it behind. Links are removed, never followed: a
    "webview" junction must not turn this cleanup on another folder."""
    import shutil
    import uuid
    base = Path(root) / "webview"
    try:
        if _is_link(base):
            _remove_link(base)
        base.mkdir(parents=True, exist_ok=True)
        if _is_link(base) or not base.is_dir():
            raise OSError("not a plain folder")
        for old in base.iterdir():
            try:
                if _is_link(old):
                    _remove_link(old)
                elif old.is_dir():
                    shutil.rmtree(old)
                else:
                    old.unlink()
            except OSError:
                pass             # still in use: removed at a later start
    except OSError as exc:
        raise ShellError("Unminute could not prepare its window's data folder in the "
                         "workspace. Ask your IT team.") from exc
    return base / uuid.uuid4().hex


def run(func=None, args=None, debug=False, storage_path=None):
    import webview
    check_backend()
    webview.start(func, args, gui="edgechromium", http_server=False,
                  private_mode=True, debug=debug,
                  storage_path=None if storage_path is None else str(storage_path),
                  icon=str(ICON) if ICON.is_file() else None)
