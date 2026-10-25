# workspace/shell.py
# Desktop shell: a pywebview (WebView2) window showing the bundled UI.
#
# The UI is loaded from a file:// URL. pywebview starts its built-in HTTP
# server (a localhost listener) for plain local paths, but not for file://
# URLs or inline HTML, so this keeps the app free of local listeners. The
# window checks (tools/process_listeners.py) verify that by listing sockets.
# private_mode keeps WebView2 from persisting cookies/storage between runs.

import json
import os
import threading
import time
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent / "ui"
# The app's microphone icon; next to the app code in the package.
ICON = Path(__file__).resolve().parents[1] / "unminute.ico"


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
    otherwise call "__init__", private attributes or their methods. The
    WebView2 backend module imports the function by name: when that module is
    already loaded (preload_backend), its reference is replaced too, and
    check_backend() refuses to start unless it is. Call it before
    webview.start()."""
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


SUSPEND_DELAY_S = 3.0      # long enough for the page's 800 ms autosave to finish


class Minimise:
    """What to tell WebView2 when the window is minimised and restored
    (docs/RELEASE_STANDARD.md, section 3). Microsoft advises using either
    suspend or the low-memory target, not both:

    - minimised with nothing recording: hide it (it stops rendering and the
      page sees itself hidden), then suspend the page after SUSPEND_DELAY_S,
      so an edit typed just before minimising is saved first. Suspending sets
      the low-memory target by itself;
    - minimised while recording (or a recording is opening): hide it and set
      the low-memory target, so the page keeps running for the recording and
      the close question;
    - restored: cancel a pending suspend, resume, put the memory target back
      to normal if it was set, and show it.

    `webview` has hide(), show(), low_memory(on), suspend() and resume().
    `later(seconds, fn)` runs fn later on the UI thread and returns something
    with cancel()."""

    def __init__(self, webview, recording, later):
        self.webview, self.recording, self.later = webview, recording, later
        self.minimised = False
        self.low = False           # set before the call, so a failure is still undone
        self.suspended = False
        self.pending = None

    def changed(self, minimised):
        if minimised == self.minimised:
            return
        self.minimised = minimised
        if minimised:
            self.webview.hide()
            if self.recording():
                self._low_memory()
            else:
                self.pending = self.later(SUSPEND_DELAY_S, self._suspend)
        else:
            if self.pending is not None:
                self.pending.cancel()
                self.pending = None
            try:
                if self.suspended:
                    self.suspended = False
                    self.webview.resume()      # resuming also puts memory back to normal
                if self.low:
                    self.low = False
                    self.webview.low_memory(False)
            finally:
                self.webview.show()

    def _low_memory(self):
        self.low = True
        self.webview.low_memory(True)

    def _suspend(self):
        # Still minimised and nothing recording? A recording that started in
        # the meantime keeps its page running at low memory instead.
        self.pending = None
        if not self.minimised:
            return
        if self.recording():
            self._low_memory()
            return
        self.suspended = True
        self.webview.suspend()


class _WebView2:
    """The WinForms WebView2 control, for Minimise. Called on the UI thread."""

    def __init__(self, control):
        self.control = control

    def hide(self):
        self.control.Visible = False          # sets the controller's IsVisible to false

    def show(self):
        self.control.Visible = True

    def low_memory(self, on):
        from Microsoft.Web.WebView2.Core import CoreWebView2MemoryUsageTargetLevel as Level
        core = self.control.CoreWebView2
        if core is not None:
            core.MemoryUsageTargetLevel = Level.Low if on else Level.Normal

    def suspend(self):
        core = self.control.CoreWebView2
        if core is not None:
            # Best effort: WebView2 declines in some cases (audio playing, for
            # example); the page then just stays hidden.
            core.TrySuspendAsync()

    def resume(self):
        core = self.control.CoreWebView2
        if core is not None and core.IsSuspended:
            core.Resume()


class _UiTimer:
    """A one-shot WinForms timer: its Tick runs on the UI thread."""

    def __init__(self, seconds, fn):
        from System import EventHandler
        from System.Windows.Forms import Timer
        self.timer = Timer()
        self.timer.Interval = max(1, int(seconds * 1000))

        def tick(sender, args):
            self.cancel()
            _guarded(fn)
        self.timer.Tick += EventHandler(tick)
        self.timer.Start()

    def cancel(self):
        self.timer.Stop()
        self.timer.Dispose()


def _guarded(fn, *args):
    """Run a window-quietening step. A WebView2 failure never stops the app:
    the rest of that step is skipped (a failed hide on minimise also skips
    scheduling the suspend), and the next minimise or restore runs its steps
    again (#196)."""
    try:
        fn(*args)
    except Exception:
        pass


def watch_minimise(window, recording):
    """Apply Minimise to the window's minimise and restore. Call once the window
    is shown. `recording` is a callable: is a recording open or opening now?"""
    from System import EventHandler
    from System.Windows.Forms import FormWindowState
    form = window.native
    state = Minimise(_WebView2(form.browser.webview), recording, _UiTimer)

    def on_resize(sender, args):              # WinForms raises this on the UI thread
        _guarded(state.changed, form.WindowState == FormWindowState.Minimized)
    form.Resize += EventHandler(on_resize)
    return state


def bring_to_front(window):
    """Restore a minimised window, as it was before (maximised stays
    maximised), and bring it forward, so a question the page is about to ask
    can be seen (closing from the taskbar while minimised)."""
    import ctypes

    from System import Action
    form = window.native

    def apply():
        user32 = ctypes.windll.user32
        handle = form.Handle.ToInt64()
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, 9)      # SW_RESTORE: back to normal or maximised
        form.Activate()
    form.Invoke(Action(apply))


def _page_alert(message):
    """True for pywebview's alert message: the page's window.alert, which
    pywebview shows in a Windows message box before the dispatch guard sees
    it. Page text must never appear in a native box, where it could pose as
    instructions from the app (#111, follow-up to A6)."""
    try:
        data = json.loads(message)
    except (TypeError, ValueError):
        return False
    return isinstance(data, list) and bool(data) and data[0] == "_pywebviewAlert"


def install_alert_guard():
    """In the WebView2 backend: drop pywebview's alert message; before the
    first page loads, turn off WebView2's own alert, confirm and prompt
    dialogs and refuse every permission request and screen capture. Both
    handlers are bound when each window is created, so this runs before any
    window (check_backend calls it)."""
    import importlib
    view = importlib.import_module("webview.platforms.edgechromium").EdgeChrome
    notify = getattr(view.on_script_notify, "__wrapped__", view.on_script_notify)
    ready = getattr(view.on_webview_ready, "__wrapped__", view.on_webview_ready)

    def on_script_notify(self, sender, args):
        try:
            message = args.get_WebMessageAsJson()
        except Exception:
            return                                   # unreadable: nothing runs
        if _page_alert(message):
            blocked_calls.append("_pywebviewAlert")
            return
        return notify(self, sender, args)

    def on_webview_ready(self, sender, args):
        # pywebview turns the browser's dialogs on and then loads the page in
        # this one handler; the setting is read when a navigation starts, so
        # it is turned off just before each load it makes.
        loads = {name: getattr(self, name) for name in ("load_url", "load_html")}
        guard = {"on": False, "failed": False}     # once per window (this handler runs once)

        def quiet():
            """True once the page may load."""
            core = getattr(sender, "CoreWebView2", None)
            if core is None or guard["failed"]:
                return False                       # WebView2 failed to start: nothing loads
            try:
                _dialogs_off(core)
                if not guard["on"]:
                    core.ScriptDialogOpening += _refuse_script_dialog
                    core.PermissionRequested += _permission_refuser()
                    core.ScreenCaptureStarting += _refuse_screen_capture
                    guard["on"] = True
            except Exception:
                # Never a page that could ask for the microphone or the screen
                # (an older WebView2 Runtime has no screen-capture event):
                # nothing loads, and the window closes with a message.
                guard["failed"] = True
                _guard_failed(self.pywebview_window)
                return False
            return True

        def before(load):
            def wrapped(*a, **kw):
                if quiet():
                    return load(*a, **kw)
            return wrapped
        for name, load in loads.items():
            setattr(self, name, before(load))
        try:
            return ready(self, sender, args)
        finally:
            for name in loads:
                self.__dict__.pop(name, None)        # back to the class methods
            quiet()

    for handler, original in ((on_script_notify, notify), (on_webview_ready, ready)):
        handler.__wrapped__ = original
        handler.unminute_guard = True
    view.on_script_notify = on_script_notify
    view.on_webview_ready = on_webview_ready


def _permission_deny():
    from Microsoft.Web.WebView2.Core import CoreWebView2PermissionState
    return CoreWebView2PermissionState.Deny


def _permission_refuser():
    """A PermissionRequested handler that refuses every request at once, with
    no prompt: the microphone and camera (recording goes only through the
    app's own confirmed path, #111 A6), notifications, location and the rest."""
    deny = _permission_deny()                      # resolved now: no failure later

    def refuse(sender, args):
        args.State = deny
        blocked_calls.append(f"permission {args.PermissionKind}")
    return refuse


def _refuse_screen_capture(sender, args):
    # getDisplayMedia: no screen picker, and the page's call fails.
    args.Cancel = True
    blocked_calls.append("screen capture")


def _guard_failed(window):
    lock_failed.append(True)
    blocked_navigations.append("browser guard failed; window closed")
    _close_now(window)


def _refuse_script_dialog(sender, args):
    # Not accepted: alert returns, confirm answers false, prompt null.
    blocked_calls.append("script dialog")


def _dialogs_off(core):
    """Turn off WebView2's own alert, confirm and prompt dialogs, which would
    show page text in a browser window (the page uses its own dialogs)."""
    core.Settings.AreDefaultScriptDialogsEnabled = False


def quiet_script_dialogs(window):
    """The same setting checked again once the window's page has loaded (the
    refusing handler is attached once, when WebView2 starts)."""
    _dialogs_off(window.native.browser.webview.CoreWebView2)


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


def create_window(bridge, *, allowed, title="Unminute", hidden=False, harden=True):
    """`allowed` is the exact set of bridge names the page may call
    (workspace.api.API_METHODS); there is no default."""
    # A backend still loading (preload_backend) finishes first, so the
    # dispatch guard below is installed with the backend module in place.
    wait_preload()
    import webview
    _log_to_current_stderr()

    if harden:
        harden_webview2()
    install_dispatch_guard(allowed)
    # New-window requests (window.open, target=_blank) must not reach the
    # system browser: load them in place, where the navigation lock refuses them.
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    webview.settings["ALLOW_DOWNLOADS"] = False
    # Otherwise pywebview adds --allow-file-access-from-files to WebView2.
    webview.settings["ALLOW_FILE_URLS"] = False
    window = webview.create_window(title, url=ui_url(), js_api=bridge,
                                   width=1100, height=760, min_size=(640, 480),
                                   hidden=hidden, text_select=True,
                                   # Ctrl+wheel zoom (#27); pywebview turns it off
                                   # by default. Ctrl+plus/minus/0: set_zoom().
                                   zoomable=True)
    window.events.before_load += lambda: _lock_once(window)
    return window


lock_failed = []          # set if a window's navigation lock could not attach


def _lock_once(window):
    if getattr(window, "_nav_locked", False):
        return
    try:
        lock_navigation(window)
        quiet_script_dialogs(window)
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


# -- loading the window's backend early (#339) ----------------------------------
# Loading pywebview's WebView2 backend (.NET through pythonnet, Windows Forms
# and the WebView2 control's assemblies) is about half a second of the
# window's start, and Python's platform.uname(), which pywebview asks first,
# is a WMI query of about 50 ms more. preload_backend() starts both on
# background threads once the offline guard is in place (workspace/__main__.py,
# after the launcher pass), so they run alongside the app's other imports and
# the workspace opening. It changes no setting and opens no window: the
# hardening switches, the dispatch guard and the alert guard are put in place
# exactly as before, by create_window() and check_backend(), which first wait
# for the loading to finish. A failure here is not reported: check_backend()
# loads the backend again and refuses to start as it always has.
#
# The thread is not a daemon: a process that ends while .NET is still loading
# (a second copy, a workspace that will not open) waits for the load to finish
# before Python's exit unloads pythonnet. Unloading it mid-load ends the
# process with an unhandled .NET exception (#378 review, B1).
_preload = {"thread": None}


def preload_backend():
    """Start loading the window's backend on a background thread (once).
    Returns at once; wait_preload() waits for it."""
    if _preload["thread"] is not None:
        return
    thread = threading.Thread(target=_load_backend, daemon=False, name="preload-backend")
    _preload["thread"] = thread
    thread.start()


def _load_backend():
    import importlib
    import platform
    try:
        # The WMI query on its own thread while .NET loads on this one, and
        # finished before pywebview's platform.system() asks for it (cached).
        query = threading.Thread(target=_quietly, args=(platform.uname,), daemon=True,
                                 name="preload-platform")
        query.start()
        importlib.import_module("clr")                          # pythonnet: loads .NET
        query.join()
        importlib.import_module("webview.guilib").initialize("edgechromium")
    except Exception:
        pass                       # check_backend() tries again and fails closed


def _log_to_current_stderr():
    """pywebview's console log handler holds the stderr of the moment webview
    was imported. Loaded early (preload_backend), that can be the console
    (None under pythonw) rather than diag.summarise_output's replacement, so
    its records would fail as logging errors instead of being summarised by
    type (#378 review, B2). Point it at the current stderr."""
    import logging
    import sys
    if sys.stderr is None:
        return
    for handler in logging.getLogger("pywebview").handlers:
        if type(handler) is logging.StreamHandler and handler.stream is not sys.stderr:
            handler.setStream(sys.stderr)


def _quietly(fn):
    try:
        fn()
    except Exception:
        pass


def wait_preload():
    """Wait until preload_backend()'s loading has finished; at once if it was
    never started."""
    thread = _preload["thread"]
    if thread is not None:
        thread.join()


def check_backend():
    """Fail closed before any window opens. pywebview silently falls back to
    the old Internet Explorer engine (MSHTML) when it cannot find WebView2;
    that engine ignores the page's Content-Security-Policy and has no
    navigation lock. The dispatch guard must also be in place in the WebView2
    backend, which imports the bridge entry point by name."""
    import importlib
    import sys

    wait_preload()                   # never two threads loading the backend at once
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
    try:
        install_alert_guard()
    except Exception:
        pass                                         # checked just below
    view = getattr(chromium, "EdgeChrome", None)
    if not (getattr(getattr(chromium, "js_bridge_call", None), "unminute_guard", False)
            and getattr(getattr(view, "on_script_notify", None), "unminute_guard", False)
            and getattr(getattr(view, "on_webview_ready", None), "unminute_guard", False)):
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


# -- native confirmations and the recording indicator (#111 A6) -------------------

# The action button stays disabled this long after the question appears or
# returns to the front, and after any click or key press before then, so
# input meant for the page cannot land on it.
CONFIRM_DELAY_MS = 1000
_asking = threading.Lock()                 # one question at a time

# The page's design tokens (workspace/ui/tokens.css), for the window the app
# draws itself. Colours with transparency are mixed over the card colour.
_THEMES = {
    "light": {"card": "#FFFFFF", "text": "#142966", "muted": "#566591", "soft": "#EEF7FD",
              "line": (20, 41, 102, 0.28), "scrim": "#142966", "scrim_opacity": 0.45,
              "focus": "#E56600", "secondary": ("#FFFFFF", "#142966"),
              "primary": ("#142966", "#FFFFFF"), "danger": ("#B3261E", "#FFFFFF"),
              "record": ("#E56600", "#FFFFFF"),
              "icon": {"tick": ("#B4DDF6", "#142966"), "mic": ("#B4DDF6", "#142966"),
                       "bin": ("#B3261E", "#FFFFFF")}},
    "dark": {"card": "#142966", "text": "#FFFFFF", "muted": (255, 255, 255, 0.74),
             "soft": (255, 255, 255, 0.06), "line": (255, 255, 255, 0.32),
             "scrim": "#050C21", "scrim_opacity": 0.6, "focus": "#E56600",
             "secondary": ("#142966", "#FFFFFF"), "primary": ("#B4DDF6", "#142966"),
             "danger": ("#B3261E", "#FFFFFF"), "record": ("#E56600", "#FFFFFF"),
             "icon": {"tick": ("#B4DDF6", "#142966"), "mic": ("#B4DDF6", "#142966"),
                      "bin": ("#B3261E", "#FFFFFF")}},
}
# The page icons' strokes, in their 24-unit view box (workspace/ui/index.html).
_ICONS = {
    "tick": [("lines", [(5, 12), (10, 17), (19, 7)])],
    "bin": [("lines", [(4, 7), (20, 7)]), ("lines", [(10, 11), (10, 17)]),
            ("lines", [(14, 11), (14, 17)]), ("lines", [(6, 7), (7, 20), (17, 20), (18, 7)]),
            ("lines", [(9, 7), (9, 4), (15, 4), (15, 7)])],
    "mic": [("round", (9, 2, 6, 12, 3)), ("arc", (5, 3, 14, 14, 0, 180)),
            ("lines", [(12, 17), (12, 21)])],
}


def app_theme():
    """The page follows Windows' app colour mode (prefers-color-scheme); so
    does the confirmation."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return "light" if winreg.QueryValueEx(key, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "light"


def native_confirm(window, question, theme=None):
    """Ask a question (workspace.consent) in a window the app draws itself,
    in Unminute's own style, owned by the app window and outside the web page,
    so page script cannot answer it.

    - One question at a time: while one is open, another is not shown (No).
    - Enter and Esc mean Cancel; no button has a keyboard shortcut.
    - A statement to tick, if the question has one, must be ticked.
    - The action button is enabled only CONFIRM_DELAY_MS after the window
      appears or returns to the front, and after any click, key press or
      tick before then.
    - It answers a mouse button released over it; or, for keyboard, Narrator
      and voice control, Enter, Space or Invoke once it has kept the focus
      for CONFIRM_DELAY_MS (moved there on purpose, then held).
    - Windows high contrast uses the system colours; a minimised app is
      restored first, so the question opens centred over it.
    True only for the action."""
    if not _asking.acquire(blocking=False):
        return False
    try:
        return _ask(window, question, theme or _system_theme())
    finally:
        _asking.release()


def _system_theme():
    from System.Windows.Forms import SystemInformation
    return "contrast" if SystemInformation.HighContrast else app_theme()


def _contrast_theme():
    """Windows high contrast: the user's own system colours."""
    from System.Drawing import SystemColors
    window, text = SystemColors.Window, SystemColors.WindowText
    highlight = (SystemColors.Highlight, SystemColors.HighlightText)
    return {"card": window, "text": text, "muted": text, "soft": window, "line": text,
            "scrim": text, "scrim_opacity": 0.45, "focus": SystemColors.Highlight,
            "secondary": (SystemColors.Control, SystemColors.ControlText),
            "primary": highlight, "danger": highlight, "record": highlight,
            "icon": {"tick": highlight, "mic": highlight, "bin": highlight}}


def _colour(value, under="#FFFFFF"):
    from System.Drawing import Color, ColorTranslator
    if not isinstance(value, (str, tuple)):
        return value                                  # already a colour (high contrast)
    if isinstance(value, tuple):
        base = ColorTranslator.FromHtml(under) if isinstance(under, str) else under
        r, g, b, a = value
        return Color.FromArgb(int(r * a + base.R * (1 - a)), int(g * a + base.G * (1 - a)),
                              int(b * a + base.B * (1 - a)))
    return ColorTranslator.FromHtml(value)


def _fade(colour, under, amount):
    """The colour at `amount` opacity over `under` (a disabled control)."""
    from System.Drawing import Color
    return Color.FromArgb(int(colour.R * amount + under.R * (1 - amount)),
                          int(colour.G * amount + under.G * (1 - amount)),
                          int(colour.B * amount + under.B * (1 - amount)))


def _rounded(x, y, w, h, r):
    from System.Drawing.Drawing2D import GraphicsPath
    path, d = GraphicsPath(), 2 * r
    path.AddArc(x, y, d, d, 180, 90)
    path.AddArc(x + w - d, y, d, d, 270, 90)
    path.AddArc(x + w - d, y + h - d, d, d, 0, 90)
    path.AddArc(x, y + h - d, d, d, 90, 90)
    path.CloseFigure()
    return path


def _ask(window, question, theme):
    import ctypes

    from System import Action, EventHandler
    from System.Drawing import (Font, FontStyle, GraphicsUnit, Pen, Point, Rectangle, Size,
                                SolidBrush)
    from System.Drawing.Drawing2D import LineCap, SmoothingMode
    from System.Windows.Forms import (Button, CheckBox, DialogResult, Form, FormBorderStyle,
                                      FormStartPosition, FormWindowState, KeyEventHandler,
                                      Label, MouseEventHandler, PaintEventHandler, Panel, Screen,
                                      TextFormatFlags, TextRenderer, Timer)
    owner = window.native
    t = _contrast_theme() if theme == "contrast" else _THEMES[theme]
    box = {}

    def ask():
        if owner.WindowState == FormWindowState.Minimized:
            # Restored first, so the question opens centred over the app.
            ctypes.windll.user32.ShowWindow(ctypes.c_void_p(owner.Handle.ToInt64()), 9)
            owner.Activate()
        scale = getattr(owner, "DeviceDpi", 96) / 96.0

        def px(value):
            return int(round(value * scale))

        def font(size, bold=False, semibold=False):
            family = "Segoe UI Semibold" if semibold else "Segoe UI"
            return Font(family, size * scale, FontStyle.Bold if bold else FontStyle.Regular,
                        GraphicsUnit.Pixel)
        card = _colour(t["card"])
        text_c, muted = _colour(t["text"], t["card"]), _colour(t["muted"], t["card"])
        soft, line = _colour(t["soft"], t["card"]), _colour(t["line"], t["card"])
        focus = _colour(t["focus"])
        fonts = {"title": font(22, bold=True), "body": font(15),
                 "detail": font(15, semibold=True), "small": font(13),
                 "button": font(15, semibold=True), "check": font(15, semibold=True)}
        dialog, scrim, timer = Form(), None, Timer()
        disposables = list(fonts.values())
        try:
            # The page's dimmed backdrop over the app, then the card on top.
            if owner.WindowState != FormWindowState.Minimized:
                scrim = Form()
                scrim.FormBorderStyle = getattr(FormBorderStyle, "None")
                scrim.ShowInTaskbar = False
                scrim.StartPosition = FormStartPosition.Manual
                scrim.BackColor = _colour(t["scrim"])
                scrim.Opacity = t["scrim_opacity"]
                scrim.Bounds = owner.RectangleToScreen(owner.ClientRectangle)
                scrim.Show(owner)
            dialog.Text = question["title"]               # Narrator reads the title...
            dialog.AccessibleDescription = question["body"]   # ...and the question
            dialog.FormBorderStyle = getattr(FormBorderStyle, "None")
            dialog.StartPosition = FormStartPosition.CenterParent
            dialog.ShowInTaskbar = False
            dialog.BackColor = card
            dialog.KeyPreview = True
            width = min(px(580), (owner.ClientSize.Width if scrim else px(580)) - px(32))
            pad, inner = px(28), min(px(580), max(px(360), width)) - 2 * px(28)
            y = pad
            content = Panel()                            # scrolls if the screen is too short
            content.BackColor = card
            content.AutoScroll = True
            content.Location = Point(0, 0)

            def label(text, f, colour, w, back=card):
                item = Label()
                item.Text = text
                item.Font = f
                item.ForeColor, item.BackColor = colour, back
                item.UseMnemonic = False
                item.AutoSize = True
                item.MaximumSize = Size(w, 0)
                return item

            # Head: the page's icon circle and title.
            icon = Panel()
            icon.Size = Size(px(44), px(44))
            icon.Location = Point(pad, y)
            icon.BackColor = card
            icon_bg, icon_fg = (_colour(c) for c in t["icon"][question.get("icon", "tick")])

            def paint_icon(sender, e):
                g = e.Graphics
                g.SmoothingMode = SmoothingMode.AntiAlias
                brush = SolidBrush(icon_bg)
                g.FillEllipse(brush, 0, 0, px(44) - 1, px(44) - 1)
                brush.Dispose()
                pen = Pen(icon_fg, 2.2 * scale)
                pen.StartCap = pen.EndCap = LineCap.Round

                def at(x, y_):
                    return Point(px(11 + x * 22 / 24), px(11 + y_ * 22 / 24))
                for kind, shape in _ICONS[question.get("icon", "tick")]:
                    if kind == "lines":
                        g.DrawLines(pen, [at(*p) for p in shape])
                    elif kind == "round":
                        x, y_, w, h, r = shape
                        corner = at(x, y_)
                        path = _rounded(corner.X, corner.Y, px(w * 22 / 24), px(h * 22 / 24),
                                        px(r * 22 / 24))
                        g.DrawPath(pen, path)
                        path.Dispose()
                    else:
                        x, y_, w, h, start, sweep = shape
                        corner = at(x, y_)
                        g.DrawArc(pen, corner.X, corner.Y, px(w * 22 / 24), px(h * 22 / 24),
                                  start, sweep)
                pen.Dispose()
            icon.Paint += PaintEventHandler(paint_icon)
            title = label(question["title"], fonts["title"], text_c, inner - px(58))
            content.Controls.Add(icon)
            content.Controls.Add(title)
            title.Location = Point(pad + px(58),
                                   y + max(0, (px(44) - title.PreferredSize.Height) // 2))
            y += max(px(44), title.PreferredSize.Height) + px(16)
            body = label(question["body"], fonts["body"], text_c, inner)
            body.Location = Point(pad, y)
            content.Controls.Add(body)
            y += body.PreferredSize.Height + px(16)
            if question.get("detail"):
                detail = label(question["detail"], fonts["detail"], text_c, inner)
                detail.Location = Point(pad, y)
                content.Controls.Add(detail)
                y += detail.PreferredSize.Height + px(16)
            if question.get("limits"):
                limits = Panel()
                limits.BackColor = card
                note = label(question["limits"], fonts["small"], muted, inner - px(28), back=soft)
                note.Location = Point(px(14), px(12))
                limits.Controls.Add(note)
                limits.Size = Size(inner, note.PreferredSize.Height + px(24))
                limits.Location = Point(pad, y)

                def paint_limits(sender, e, panel=limits):
                    e.Graphics.SmoothingMode = SmoothingMode.AntiAlias
                    brush = SolidBrush(soft)
                    path = _rounded(0, 0, panel.Width - 1, panel.Height - 1, px(12))
                    e.Graphics.FillPath(brush, path)
                    path.Dispose()
                    brush.Dispose()
                limits.Paint += PaintEventHandler(paint_limits)
                content.Controls.Add(limits)
                y += limits.Height + px(16)
            check = None
            if question.get("check"):
                check = CheckBox()
                check.Text = question["check"]
                check.UseMnemonic = False
                check.Font = fonts["check"]
                check.ForeColor, check.BackColor = text_c, card
                text_w = inner - px(14 + 20 + 12 + 14)
                text_h = TextRenderer.MeasureText(check.Text, fonts["check"], Size(text_w, 0),
                                                  TextFormatFlags.WordBreak).Height
                check.Size = Size(inner, text_h + px(24))
                check.Location = Point(pad, y)

                def paint_check(sender, e, c=check, text_w=text_w):
                    g = e.Graphics
                    g.SmoothingMode = SmoothingMode.AntiAlias
                    g.Clear(card)
                    edge = Pen(focus if c.Focused else line, 2 * scale if c.Focused else scale)
                    path = _rounded(1, 1, c.Width - 3, c.Height - 3, px(12))
                    g.DrawPath(edge, path)
                    path.Dispose()
                    edge.Dispose()
                    top = (c.Height - px(20)) // 2
                    tick_box = _rounded(px(14), top, px(20), px(20), px(4))
                    if c.Checked:
                        fill = SolidBrush(_colour(t["primary"][0]))
                        g.FillPath(fill, tick_box)
                        fill.Dispose()
                        mark = Pen(_colour(t["primary"][1]), 2.4 * scale)
                        mark.StartCap = mark.EndCap = LineCap.Round
                        g.DrawLines(mark, [Point(px(18), top + px(10)), Point(px(22), top + px(14)),
                                           Point(px(30), top + px(6))])
                        mark.Dispose()
                    else:
                        border = Pen(line, 1.5 * scale)
                        g.DrawPath(border, tick_box)
                        border.Dispose()
                    tick_box.Dispose()
                    TextRenderer.DrawText(g, c.Text, fonts["check"],
                                          Rectangle(px(14 + 20 + 12), px(12), text_w,
                                                    c.Height - px(24)),
                                          text_c, TextFormatFlags.WordBreak
                                          | TextFormatFlags.VerticalCenter)
                check.Paint += PaintEventHandler(paint_check)
                content.Controls.Add(check)
                y += check.Height + px(20)
            else:
                y += px(4)

            # Foot: Cancel, then the action, at the right; the page's button styles.
            cancel, act = Button(), Button()
            states = {}
            for button, label_text, colours in (
                    (cancel, "Cancel", t["secondary"]),
                    (act, question["action"], t[question.get("style", "primary")])):
                button.Text = label_text
                button.UseMnemonic = False
                button.Font = fonts["button"]
                button.Height = px(44)
                button.Width = max(px(96), px(48) + TextRenderer.MeasureText(
                    label_text, fonts["button"]).Width)
                states[button] = {"hover": False, "colours": (_colour(colours[0]),
                                                              _colour(colours[1]))}

                def paint_button(sender, e, b=button):
                    g = e.Graphics
                    g.SmoothingMode = SmoothingMode.AntiAlias
                    g.Clear(card)
                    back, fore = states[b]["colours"]
                    if b is cancel:
                        back = soft if states[b]["hover"] and b.Enabled else card
                    if not b.Enabled:
                        back, fore = _fade(back, card, 0.45), _fade(fore, card, 0.45)
                    path = _rounded(1, 1, b.Width - 3, b.Height - 3, px(10))
                    brush = SolidBrush(back)
                    g.FillPath(brush, path)
                    brush.Dispose()
                    edge = Pen(focus if b.Focused else (line if b is cancel else back),
                               2 * scale if b.Focused else scale)
                    g.DrawPath(edge, path)
                    edge.Dispose()
                    path.Dispose()
                    TextRenderer.DrawText(g, b.Text, fonts["button"],
                                          Rectangle(0, 0, b.Width, b.Height), fore,
                                          TextFormatFlags.HorizontalCenter
                                          | TextFormatFlags.VerticalCenter
                                          | TextFormatFlags.SingleLine)
                button.Paint += PaintEventHandler(paint_button)

                def hover(on, b=button):
                    def handler(sender, e):
                        states[b]["hover"] = on
                        b.Invalidate()
                    return EventHandler(handler)
                button.MouseEnter += hover(True)
                button.MouseLeave += hover(False)
            cancel.DialogResult = DialogResult.Cancel
            act.Enabled = False
            total = pad + inner
            footer = px(44) + pad
            room = Screen.FromControl(owner).WorkingArea.Height - px(48)
            height = min(y + footer, max(room, px(240)))
            content.Size = Size(inner + 2 * pad, height - footer)
            act.Location = Point(total - act.Width, height - footer)
            cancel.Location = Point(total - act.Width - px(10) - cancel.Width, height - footer)
            dialog.Controls.Add(content)
            dialog.Controls.Add(cancel)
            dialog.Controls.Add(act)
            dialog.ClientSize = Size(inner + 2 * pad, height)
            dialog.AcceptButton = cancel                 # Enter means Cancel
            dialog.CancelButton = cancel                 # Esc means Cancel
            timer.Interval = CONFIRM_DELAY_MS
            ready = {"waited": False}

            focused = {"since": None}                  # when the action took the focus
            enabled = {"since": None}                  # when the action was enabled

            def update():
                on = ready["waited"] and (check is None or check.Checked)
                if on and not act.Enabled:
                    enabled["since"] = time.monotonic()
                if not on:
                    enabled["since"] = focused["since"] = None   # earlier waiting no longer counts
                act.Enabled = on

            def restart(*_):
                ready["waited"] = False                  # not yet: wait again
                update()
                timer.Stop()
                timer.Start()

            def waited(sender, args):
                timer.Stop()
                ready["waited"] = True
                update()

            def activated(sender, args):
                cancel.Focus()
                restart()

            def mouse_up_on_action(sender, args):
                if act.Enabled and act.ClientRectangle.Contains(args.Location):
                    dialog.DialogResult = DialogResult.Yes

            def focus_in(sender, args):
                focused["since"] = time.monotonic()
                act.Invalidate()

            def focus_out(sender, args):
                focused["since"] = None
                act.Invalidate()

            def held(stamp):
                return stamp is not None and time.monotonic() - stamp >= CONFIRM_DELAY_MS / 1000

            def act_clicked(sender, args):
                # Enter or Space need the focus, so they count only once the
                # button has held it for the full wait. An Invoke without the
                # focus (Voice Access "click Start recording") comes from an
                # assistive tool, never from typing or from the page: it counts
                # once the button has been enabled for the full wait.
                if not act.Enabled:
                    return
                if held(focused["since"]) if act.Focused else held(enabled["since"]):
                    dialog.DialogResult = DialogResult.Yes

            def made(sender, args):
                # Windows 11 rounds the card's corners and draws its edge.
                dwm = ctypes.WinDLL("dwmapi")
                hwnd = ctypes.c_void_p(dialog.Handle.ToInt64())
                dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
                border = ctypes.c_int(line.R | line.G << 8 | line.B << 16)
                dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), 4)

            timer.Tick += EventHandler(waited)
            dialog.Activated += EventHandler(activated)
            dialog.HandleCreated += EventHandler(made)
            act.MouseUp += MouseEventHandler(mouse_up_on_action)
            act.Click += EventHandler(act_clicked)
            act.GotFocus += EventHandler(focus_in)
            act.LostFocus += EventHandler(focus_out)
            if check is not None:
                check.CheckedChanged += EventHandler(lambda s, e: (restart(), check.Invalidate()))
                check.GotFocus += EventHandler(lambda s, e: check.Invalidate())
                check.LostFocus += EventHandler(lambda s, e: check.Invalidate())
            dialog.KeyDown += KeyEventHandler(lambda s, e: restart() if not act.Enabled else None)
            watched = [dialog, content, cancel, body, title, icon] + ([check] if check else [])
            for control in watched:
                control.MouseDown += MouseEventHandler(
                    lambda s, e: restart() if not act.Enabled else None)
            box["answer"] = dialog.ShowDialog(scrim or owner)
        finally:
            timer.Dispose()
            dialog.Dispose()
            if scrim is not None:
                scrim.Close()
                scrim.Dispose()
            for item in disposables:
                item.Dispose()

    owner.Invoke(Action(ask))                     # a window belongs to the UI thread
    return box.get("answer") == DialogResult.Yes


RECORDING_MARK = "● Recording - "


def show_recording(window, recording):
    """While recording, the window title starts with a red-dot "Recording"
    mark and the taskbar button carries a red-dot overlay (also for a pinned
    button, whose icon comes from the shortcut). Returns True if Windows took
    the overlay."""
    from System import Action
    form = window.native
    plain = getattr(window, "unminute_title", None)
    if plain is None:
        plain = window.unminute_title = window.title
    window.set_title(RECORDING_MARK + plain if recording else plain)
    box = {}

    def overlay():
        if getattr(window, "unminute_dot", None) is None:
            window.unminute_dot = _red_dot_icon()
        hicon = window.unminute_dot.Handle.ToInt64() if recording else 0
        box["ok"] = _set_overlay(form.Handle.ToInt64(), hicon,
                                 "Recording" if recording else None)

    form.Invoke(Action(overlay))                  # the taskbar wants the UI thread
    return box.get("ok", False)


def _red_dot_icon():
    """A 16 px red dot for the taskbar overlay. The icon is cloned and its
    original handle released (the pattern pywebview uses for its own icon)."""
    import ctypes

    from System.Drawing import Bitmap, Color, Graphics, Icon, SolidBrush
    from System.Drawing.Drawing2D import SmoothingMode
    image = Bitmap(16, 16)
    pen = Graphics.FromImage(image)
    white, red = SolidBrush(Color.White), SolidBrush(Color.FromArgb(220, 30, 30))
    try:
        pen.SmoothingMode = SmoothingMode.AntiAlias
        pen.FillEllipse(white, 0, 0, 15, 15)
        pen.FillEllipse(red, 2, 2, 11, 11)
        handle = image.GetHicon()
        try:
            return Icon.FromHandle(handle).Clone()
        finally:
            ctypes.WinDLL("user32").DestroyIcon(ctypes.c_void_p(handle.ToInt64()))
    finally:
        white.Dispose()
        red.Dispose()
        pen.Dispose()
        image.Dispose()


def _set_overlay(hwnd, hicon, description):
    """ITaskbarList3::SetOverlayIcon through COM (on the UI thread, which is
    an STA). hicon 0 removes the overlay. True if Windows accepted it."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                    ("d", ctypes.c_ubyte * 8)]

    def guid(text):
        g = GUID()
        ctypes.OleDLL("ole32").CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
        return g

    clsid = guid("{56FDF344-FD6D-11d0-958A-006097C9A090}")      # CLSID_TaskbarList
    iid = guid("{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}")        # IID_ITaskbarList3
    unknown = ctypes.c_void_p()
    try:
        ctypes.OleDLL("ole32").CoCreateInstance(ctypes.byref(clsid), None, 1,
                                                ctypes.byref(iid), ctypes.byref(unknown))
    except OSError:
        return False
    vtable = ctypes.cast(unknown, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

    def method(index, *argtypes):
        return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[index])

    try:
        if method(3)(unknown) != 0:                              # HrInit
            return False
        return method(18, wintypes.HWND, wintypes.HICON, wintypes.LPCWSTR)(   # SetOverlayIcon
            unknown, hwnd, hicon or None, description) == 0
    finally:
        method(2)(unknown)                                       # Release
