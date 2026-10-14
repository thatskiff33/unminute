# workspace/shell.py
# Desktop shell: a pywebview (WebView2) window showing the bundled UI.
#
# The UI is loaded from a file:// URL. pywebview starts its built-in HTTP
# server (a localhost listener) for plain local paths, but not for file://
# URLs or inline HTML, so this keeps the app free of local listeners. The
# trial (tools/webview_trial.py) verifies that by listing listening sockets.
# private_mode keeps WebView2 from persisting cookies/storage between runs.

from pathlib import Path

UI_DIR = Path(__file__).resolve().parent / "ui"


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


def _allowed(uri):
    """Only the bundled UI folder may be shown; everything else is refused.
    pywebview injects the bridge into whatever page is loaded, so a remote
    page must never load. Both sides are percent-decoded, because Python and
    Chromium encode characters such as ( ) ' ! differently."""
    base = _norm_url(UI_DIR.as_uri()).rstrip("/") + "/"
    return _norm_url(uri).startswith(base)


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
        return original(window, func_name, param, value_id)

    guarded.__wrapped__ = original
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


def lock_navigation(window):
    """Cancel any navigation outside the bundled UI (WebView2 NavigationStarting).
    Attached at before_load, i.e. after the first (bundled) page navigation,
    and stays attached to the control for every later navigation and reload."""
    control = window.native.browser.webview

    def on_navigation_starting(sender, args):
        uri = str(args.Uri)
        if not _allowed(uri):
            args.Cancel = True
            blocked_navigations.append(uri.split("?")[0][:200])

    control.NavigationStarting += on_navigation_starting


def create_window(bridge, title="Meeting workspace", hidden=False, harden=True,
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
    window.events.before_load += lambda: _lock_once(window)
    return window


def _lock_once(window):
    if not getattr(window, "_nav_locked", False):
        lock_navigation(window)
        window._nav_locked = True


def run(func=None, args=None, debug=False):
    import webview
    webview.start(func, args, gui="edgechromium", http_server=False,
                  private_mode=True, debug=debug)
