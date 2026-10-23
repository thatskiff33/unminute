# Security hardening checks (#111 slice A2): the window shell's dispatch guard
# and navigation rule, settings validation, test-only engines, the workspace
# location, SQLite temporary storage, a token left in the environment,
# nvidia-smi, remote debugging (B4), and the installer's and uninstaller's
# folder checks and their wait for a file in use (#239).
#   python -m unittest discover -s tests -v

import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import keyprotect  # noqa: E402

from workspace import credentials, shell  # noqa: E402

# The installer tests start Windows PowerShell, and the junction tests cmd.exe,
# by the full path in the system folder, never by name (#237, #253). The helper
# is loaded from its file so tools/ stays off this module's import path.
_spec = importlib.util.spec_from_file_location("windows_powershell",
                                               ROOT / "tools" / "windows_powershell.py")
windows_powershell = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(windows_powershell)


def mklink_junction(link, target, **kwargs):
    """cmd /c mklink /J link target, with cmd.exe from the system folder and
    the minimal environment, never cmd by name (#253)."""
    return subprocess.run([windows_powershell.system_program("cmd.exe"), "/c", "mklink", "/J",
                           str(link), str(target)],
                          env=windows_powershell.environment(), **kwargs)


HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
HAVE_WEBVIEW = importlib.util.find_spec("webview") is not None


class DomEventTests(unittest.TestCase):
    """Only dragover and drop on the document reach pywebview, rebuilt so a
    page-supplied pywebviewFullPath never survives."""

    def drop(self, files, node="document"):
        return {"event": {"type": "drop", "dataTransfer": {"files": files}}, "nodeId": node}

    def test_page_supplied_full_path_is_removed(self):
        clean = shell._clean_dom_event(self.drop([
            {"name": "a.m4a", "pywebviewFullPath": "C:\\Users\\x\\secret.m4a"},
            {"name": "b.wav"}]))
        files = clean["event"]["dataTransfer"]["files"]
        self.assertEqual(files, [{"name": "a.m4a"}, {"name": "b.wav"}])
        self.assertNotIn("pywebviewFullPath", json.dumps(clean))

    def test_other_nodes_events_and_shapes_are_refused(self):
        for bad in (self.drop([], node="el-1"),
                    {"event": {"type": "click"}, "nodeId": "document"},
                    {"event": "drop", "nodeId": "document"},
                    {"event": {"type": "drop", "dataTransfer": {"files": "x"}},
                     "nodeId": "document"},
                    None, [], "drop"):
            self.assertIsNone(shell._clean_dom_event(bad), repr(bad))

    def test_dragover_carries_nothing_else(self):
        clean = shell._clean_dom_event({"event": {"type": "dragover", "x": 1, "files": [1]},
                                        "nodeId": "document"})
        self.assertEqual(clean, {"event": {"type": "dragover"}, "nodeId": "document"})


class NavigationTests(unittest.TestCase):
    def test_only_the_windows_own_page(self):
        page = shell.ui_url("index.html")
        self.assertTrue(shell._allowed(page))
        self.assertTrue(shell._allowed(page + "#meeting"))
        self.assertTrue(shell._allowed(page + "?x=1"))
        for other in (shell.ui_url("other.html"), shell.ui_url("app.js"),
                      shell.ui_url("img/logo.svg"), page + "%23/evil.html",
                      page + "%3F/../other.html"):
            self.assertFalse(shell._allowed(other), other)
        self.assertTrue(shell._allowed(shell.ui_url("other.html"), "other.html"))
        self.assertFalse(shell._allowed(page, "other.html"))

    def test_the_window_needs_its_allowed_names_and_has_one_page(self):
        # No default bridge allowlist to fall back on, and no page to choose (#136).
        import inspect
        params = inspect.signature(shell.create_window).parameters
        self.assertIs(params["allowed"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(params["allowed"].default, inspect.Parameter.empty)
        self.assertNotIn("page", params)

    def test_the_lock_keeps_index_html_and_cancels_any_other_page(self):
        from types import SimpleNamespace

        class Event:                       # WebView2's NavigationStarting (+= adds a handler)
            def __init__(self):
                self.handlers = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self

        control = SimpleNamespace(NavigationStarting=Event())
        window = SimpleNamespace(native=SimpleNamespace(browser=SimpleNamespace(webview=control)),
                                 unminute_page="other.html")     # never followed
        with mock.patch.object(shell, "quiet_script_dialogs"):
            shell._lock_once(window)
        [handler] = control.NavigationStarting.handlers
        cancelled = {}
        for name in ("index.html#x", "other.html"):
            args = SimpleNamespace(Uri=shell.ui_url("index.html").rsplit("/", 1)[0] + "/" + name,
                                   Cancel=False)
            handler(None, args)
            cancelled[name] = args.Cancel
        self.assertEqual(cancelled, {"index.html#x": False, "other.html": True})

    def test_the_page_opens_no_link_itself(self):
        # #109: Copy and open asks the app to open the assistant; the page has
        # no link, window.open, location change or form that submits anywhere
        # (its forms are method="dialog"). Only its two stylesheets use href.
        import re
        ui = ROOT / "workspace" / "ui"
        html = (ui / "index.html").read_text(encoding="utf-8")
        script = (ui / "app.js").read_text(encoding="utf-8").replace(
            '"http://www.w3.org/2000/svg"', "SVG_NAMESPACE")            # a name, not an address
        self.assertEqual(re.findall(r"\bhref\s*=\s*\"([^\"]*)\"", html), ["tokens.css", "app.css"])
        for source in (html, script):
            for pattern in (r"window\.open\b", r"\blocation\b", r"\.href\b", r"\bsrcdoc\b",
                            r"\bhttps?://", r"[\"']a[\"']", r"[\"']iframe[\"']"):
                self.assertIsNone(re.search(pattern, source), pattern)
        for pattern in (r"<a\b", r"<iframe\b", r"<form(?![^>]*method=\"dialog\")",
                        r"<[^>]*\s(?:action|formaction|target)\s*="):         # markup only
            self.assertIsNone(re.search(pattern, html), pattern)

    def test_the_page_names_no_assistant_until_the_app_does(self):
        # #109 review: before the preview arrived (or if it failed), the page
        # showed "Copy and open ChatGPT" even where IT had turned it off or
        # named another assistant. Copy and open and its note now start hidden
        # and empty, Copy prompt and transcript is the main button until then,
        # and the preview's name and host fill them (app.js layoutHandoff).
        import re
        html = (ROOT / "workspace" / "ui" / "index.html").read_text(encoding="utf-8")
        for tag_id in ("ho-open", "ho-open-note"):
            tag = re.search(r'<[a-z]+ [^>]*\bid="' + tag_id + r'"[^>]*>', html).group(0)
            self.assertRegex(tag, r"\shidden[\s>]", tag_id)
        for span in ("ho-open-name", "ho-open-host"):
            self.assertIn(f'<span id="{span}"></span>', html)
        copy = re.search(r'<button [^>]*\bid="ho-copy"[^>]*>', html).group(0)
        self.assertIn('class="primary wide-btn"', copy)

    def test_every_hand_off_visit_starts_without_an_assistant(self):
        # #109 review round 2: reopening Hand off kept the last preview's
        # "Copy and open" name and host until the new preview arrived (and
        # for good if it failed). openHandoff now lays the page out as before
        # #109 before it waits for anything; the preview fills it in.
        import re
        script = (ROOT / "workspace" / "ui" / "app.js").read_text(encoding="utf-8")
        body = re.search(r"\nasync function openHandoff\(\) \{\n(.*?)\n\}\n", script, re.S)
        self.assertIsNotNone(body, "openHandoff not found")
        self.assertIn("layoutHandoff(null);", body.group(1).split("await", 1)[0])

    def test_the_hand_off_buttons_beneath_copy_and_open_stay_one_row(self):
        # #109 review: the three ways out beneath Copy and open are one flex
        # row (wrapping only where three cannot fit), not a grid with Copy
        # prompt and transcript spanning a row of its own.
        import re
        css = (ROOT / "workspace" / "ui" / "app.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        row = [(s.strip(), d) for s, d in rules
               if re.search(r"\.ho-three|\.ho-pair|#ho-row|#ho-copy\b", s)]
        self.assertTrue(any(s == ".ho-pair" and re.search(r"display\s*:\s*flex", d)
                            for s, d in row), ".ho-pair must stay a flex row")
        for selector, decls in row:
            self.assertNotRegex(decls, r"display\s*:\s*(inline-)?grid", selector)
            self.assertNotRegex(decls, r"grid-(column|row|area|template)", selector)


@unittest.skipUnless(HAVE_WEBVIEW, "pywebview not installed")
class DispatchGuardTests(unittest.TestCase):
    def setUp(self):
        import webview.util as util
        self.util = util
        self.seen = []
        self.original = util.js_bridge_call
        util.js_bridge_call = lambda w, name, p, i: self.seen.append((name, p))
        shell.install_dispatch_guard({"rename", "ping"})

    def tearDown(self):
        self.util.js_bridge_call = self.original

    def window(self):
        class Api:
            def rename(self, mid, title):
                return {"ok": True}

            def ping(self):
                return {"ok": True}

        class Window:
            _js_api = Api()
        return Window()

    def test_wrong_argument_counts_are_refused_before_the_call(self):
        w = self.window()
        for args in ([], ["m"], ["m", "t", "extra"], "m", None, {"mid": "m"}):
            self.util.js_bridge_call(w, "rename", args, "1")
        self.util.js_bridge_call(w, "rename", ["m", "t"], "1")
        self.util.js_bridge_call(w, "ping", [], "1")
        self.assertEqual([name for name, _ in self.seen], ["rename", "ping"])
        self.assertIn("rename(arguments)", shell.blocked_calls)

    def test_drop_event_reaches_pywebview_only_cleaned(self):
        shell.install_dispatch_guard({"pywebviewEventHandler"})
        w = self.window()
        self.util.js_bridge_call(w, "pywebviewEventHandler", {
            "event": {"type": "drop", "dataTransfer": {"files": [
                {"name": "a.m4a", "pywebviewFullPath": "C:\\x.m4a"}]}},
            "nodeId": "document"}, "eventHandler")
        self.util.js_bridge_call(w, "pywebviewEventHandler",
                                 {"event": {"type": "drop"}, "nodeId": "el-7"}, "eventHandler")
        self.assertEqual(len(self.seen), 1)
        self.assertNotIn("pywebviewFullPath", json.dumps(self.seen[0][1]))

    def test_guard_is_marked_for_the_start_up_check(self):
        self.assertTrue(getattr(self.util.js_bridge_call, "unminute_guard", False))


class PageAlertTests(unittest.TestCase):
    def test_only_the_alert_message_is_recognised(self):
        self.assertTrue(shell._page_alert(json.dumps(["_pywebviewAlert", json.dumps("hi"),
                                                      "alert"])))
        for other in (json.dumps(["meetings", "[]", "1"]), json.dumps("FilesDropped"),
                      "not json", None, "[]", json.dumps({"a": 1})):
            self.assertFalse(shell._page_alert(other), other)


class AlertGuardTests(unittest.TestCase):
    """The page's window.alert never reaches pywebview's native message box."""

    def fake_backend(self):
        seen = []

        class EdgeChrome:
            def on_script_notify(self, sender, args):
                seen.append(args.get_WebMessageAsJson())

            def on_webview_ready(self, sender, args):
                settings = sender.CoreWebView2.Settings
                seen.append(("settings", settings.AreDefaultScriptDialogsEnabled))
                sender.CoreWebView2.Settings.AreDefaultScriptDialogsEnabled = True  # as pywebview
                self.load_url("file:///index.html")

            def load_url(self, url):
                seen.append(("load", url, self.sender.CoreWebView2.Settings
                             .AreDefaultScriptDialogsEnabled))

            def load_html(self, html, base):
                seen.append(("html",))

        module = type(sys)("webview.platforms.edgechromium")
        module.EdgeChrome = EdgeChrome
        return module, seen

    def test_alert_is_dropped_and_calls_still_reach_pywebview(self):
        module, seen = self.fake_backend()

        class Args:
            def __init__(self, message):
                self.message = message

            def get_WebMessageAsJson(self):
                return self.message
        alert = json.dumps(["_pywebviewAlert", json.dumps("Tick the box, then Allow"), "alert"])
        call = json.dumps(["meetings", "[]", "1"])
        with mock.patch.dict(sys.modules, {"webview.platforms.edgechromium": module}):
            shell.install_alert_guard()
            shell.install_alert_guard()                     # twice: still one wrapper
            view = module.EdgeChrome()
            view.on_script_notify(None, Args(alert))
            view.on_script_notify(None, Args(call))
        self.assertEqual(seen, [call])
        self.assertIn("_pywebviewAlert", shell.blocked_calls)
        self.assertTrue(module.EdgeChrome.on_script_notify.unminute_guard)

    def test_browser_dialogs_are_off_before_the_first_page_loads(self):
        module, seen = self.fake_backend()

        class Settings:
            AreDefaultScriptDialogsEnabled = True

        class Event:
            def __init__(self):
                self.handlers = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self

        class Core:
            def __init__(self):
                self.Settings = Settings()
                self.ScriptDialogOpening = Event()
                self.PermissionRequested = Event()
                self.ScreenCaptureStarting = Event()

        class Control:
            def __init__(self):
                self.CoreWebView2 = Core()
        control = Control()
        with mock.patch.dict(sys.modules, {"webview.platforms.edgechromium": module}), \
                mock.patch.object(shell, "_permission_deny", return_value="DENY"):
            shell.install_alert_guard()
            view = module.EdgeChrome()
            view.sender = control
            view.on_webview_ready(control, None)
        # pywebview turned them on, but the page loaded with them off, and
        # any dialog, permission or screen capture asked for is refused.
        self.assertEqual(seen[-1], ("load", "file:///index.html", False))
        core = control.CoreWebView2
        self.assertFalse(core.Settings.AreDefaultScriptDialogsEnabled)
        self.assertEqual(core.ScriptDialogOpening.handlers, [shell._refuse_script_dialog])
        self.assertEqual(core.ScreenCaptureStarting.handlers, [shell._refuse_screen_capture])
        [refuse_permission] = core.PermissionRequested.handlers               # each once

        class Request:                        # the page asks for the microphone
            PermissionKind = "Microphone"
            State = "Default"
        request = Request()
        refuse_permission(None, request)
        self.assertEqual(request.State, "DENY")
        self.assertIn("permission Microphone", shell.blocked_calls)

        class Capture:                        # the page asks to capture the screen
            Cancel = False
        capture = Capture()
        shell._refuse_screen_capture(None, capture)
        self.assertTrue(capture.Cancel)
        self.assertIn("screen capture", shell.blocked_calls)
        self.assertNotIn("load_url", view.__dict__)              # the instance is left as it was
        self.assertTrue(module.EdgeChrome.on_webview_ready.unminute_guard)

    def test_no_page_loads_when_the_guard_cannot_attach(self):
        # An older WebView2 Runtime without the screen-capture event: the
        # page never loads and the window closes, with no error escaping into
        # WebView2's start-up.
        module, seen = self.fake_backend()

        class Settings:
            AreDefaultScriptDialogsEnabled = True

        class Event:
            def __iadd__(self, handler):
                return self

        class Core:
            def __init__(self):
                self.Settings = Settings()
                self.ScriptDialogOpening = Event()
                self.PermissionRequested = Event()

        class Control:
            def __init__(self):
                self.CoreWebView2 = Core()
        control = Control()
        closed = []
        try:
            with mock.patch.dict(sys.modules, {"webview.platforms.edgechromium": module}), \
                    mock.patch.object(shell, "_permission_deny", return_value="DENY"), \
                    mock.patch.object(shell, "_close_now", closed.append):
                shell.install_alert_guard()
                view = module.EdgeChrome()
                view.sender = control
                view.pywebview_window = "the window"
                view.on_webview_ready(control, None)
            self.assertFalse([s for s in seen if s[0] == "load"])
            self.assertEqual(closed, ["the window"])             # once
            self.assertTrue(shell.lock_failed)
        finally:
            shell.lock_failed.clear()


@unittest.skipUnless(HAVE_WEBVIEW, "pywebview not installed")
class BackendCheckTests(unittest.TestCase):
    def test_refuses_the_internet_explorer_fallback(self):
        guilib = importlib.import_module("webview.guilib")

        class Fallback:
            renderer = "mshtml"
        with mock.patch.object(guilib, "initialize", return_value=Fallback()):
            with self.assertRaises(shell.ShellError):
                shell.check_backend()

    def test_a_backend_that_cannot_load_is_refused_with_a_message(self):
        guilib = importlib.import_module("webview.guilib")
        with mock.patch.object(guilib, "initialize", side_effect=OSError("no .NET")):
            with self.assertRaises(shell.ShellError):
                shell.check_backend()

    def test_refuses_an_unguarded_backend(self):
        guilib = importlib.import_module("webview.guilib")

        class Chromium:
            renderer = "edgechromium"
        fake = type(sys)("webview.platforms.edgechromium")
        fake.js_bridge_call = lambda *a: None
        with mock.patch.object(guilib, "initialize", return_value=Chromium()), \
                mock.patch.dict(sys.modules, {"webview.platforms.edgechromium": fake}):
            with self.assertRaises(shell.ShellError):
                shell.check_backend()

    def test_refuses_a_backend_whose_alert_cannot_be_guarded(self):
        guilib = importlib.import_module("webview.guilib")

        class Chromium:
            renderer = "edgechromium"

        def guarded(*a):
            return None
        guarded.unminute_guard = True
        fake = type(sys)("webview.platforms.edgechromium")
        fake.js_bridge_call = guarded                       # calls guarded, alert not
        with mock.patch.object(guilib, "initialize", return_value=Chromium()), \
                mock.patch.dict(sys.modules, {"webview.platforms.edgechromium": fake}):
            with self.assertRaises(shell.ShellError):
                shell.check_backend()


class StorageFolderTests(unittest.TestCase):
    def test_leftovers_are_removed_and_a_fresh_folder_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "webview" / "old"
            (old / "EBWebView").mkdir(parents=True)
            (old / "EBWebView" / "Cache").write_text("x")
            new = shell.storage_folder(tmp)
            self.assertEqual(new.parent, Path(tmp) / "webview")
            self.assertFalse(old.exists())
            self.assertFalse(new.exists())          # WebView2 creates it

    @unittest.skipUnless(os.name == "nt", "NTFS junctions are Windows-only")
    def test_links_are_removed_never_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            precious = Path(tmp) / "Documents"
            (precious / "inner").mkdir(parents=True)
            (precious / "client.docx").write_text("keep me")
            ws = Path(tmp) / "ws"
            ws.mkdir()
            r = mklink_junction(ws / "webview", precious, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            shell.storage_folder(ws)                       # "webview" itself is a junction
            self.assertTrue((precious / "client.docx").exists())
            self.assertTrue((precious / "inner").is_dir())
            self.assertFalse(os.path.isjunction(ws / "webview"))
            inner = ws / "webview" / "old-profile"
            r = mklink_junction(inner, precious, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            shell.storage_folder(ws)                       # a junction inside it
            self.assertFalse(inner.exists())
            self.assertTrue((precious / "client.docx").exists())


class LockFailureTests(unittest.TestCase):
    """If the navigation lock cannot attach, the window is closed, and if even
    that fails the process ends: it never runs without the lock."""

    def setUp(self):
        shell.lock_failed.clear()

    def tearDown(self):
        shell.lock_failed.clear()

    def window(self):
        class Window:
            native = object()                 # no WebView2 control: the lock fails
        return Window()

    def test_window_is_closed_when_the_lock_fails(self):
        with mock.patch.object(shell, "_post_close") as close:
            shell._lock_once(self.window())
        close.assert_called_once()
        self.assertTrue(shell.lock_failed)

    def test_process_ends_if_the_window_cannot_be_closed(self):
        with mock.patch.object(shell, "_post_close", side_effect=RuntimeError), \
                mock.patch.object(shell.os, "_exit", side_effect=SystemExit) as end:
            with self.assertRaises(SystemExit):
                shell._lock_once(self.window())
        end.assert_called_once_with(3)


class StartUpTests(unittest.TestCase):
    """Run in a child process: importing workspace.__main__ installs the
    network guard, which must not affect this test process."""

    def run_main(self, code, env=None):
        import subprocess
        root = Path(__file__).resolve().parents[1]
        return subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                              capture_output=True, text=True, timeout=120)

    def test_gate_reports_a_bad_location_as_json(self):
        env = {k: v for k, v in os.environ.items() if k != "LOCALAPPDATA"}
        r = self.run_main("import sys; sys.argv = ['x', '--gate'];"
                          "import workspace.__main__ as m; sys.exit(m.main())", env)
        self.assertEqual(r.returncode, 6, r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["kind"], "location")

    RESTORE_LID = ("import json, sys; sys.argv = ['x', '--restore-lid']; "
                   "import workspace.__main__ as m; "
                   "m._app_running = lambda name=None: False; "   # an installed copy may be open
                   "code = m.main(); "
                   "heavy = ('webview', 'numpy', 'torch', 'av', 'workspace.app', "
                   "'workspace.api', 'workspace.shell', 'workspace.worker', "
                   "'workspace.diag'); "
                   "print(json.dumps([n for n in heavy if n in sys.modules])); "
                   "sys.exit(code)")

    def test_restore_lid_prints_a_state_only(self):
        # #191: the uninstaller's switch. No app data folder, then one with no
        # record: a fixed JSON state and exit code, no window or heavy import,
        # and nothing created. The power scheme is never read for these.
        env = {k: v for k, v in os.environ.items() if k != "LOCALAPPDATA"}
        r = self.run_main(self.RESTORE_LID, env)
        self.assertEqual((r.returncode, r.stdout.splitlines()[0], r.stderr),
                         (1, '{"lid": "location"}', ""))
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_main(self.RESTORE_LID, dict(os.environ, LOCALAPPDATA=tmp))
            self.assertEqual((r.returncode, r.stdout.splitlines(), r.stderr),
                             (0, ['{"lid": "none"}', "[]"], ""))
            self.assertEqual(list(Path(tmp).iterdir()), [])          # nothing created

    def test_restore_lid_leaves_the_lid_alone_for_a_damaged_record(self):
        # A record that cannot say what to put back is removed and nothing is
        # written (a damaged record never reaches the power scheme).
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "Unminute" / "workspace" / "power_restore.json"
            record.parent.mkdir(parents=True)
            record.write_text('{"scheme": "not-a-guid", "ac": 1, "dc": 1}', encoding="utf-8")
            r = self.run_main(self.RESTORE_LID, dict(os.environ, LOCALAPPDATA=tmp))
            self.assertEqual((r.returncode, r.stdout.splitlines(), r.stderr),
                             (2, ['{"lid": "damaged"}', "[]"], ""))
            self.assertFalse(record.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows named mutex")
    def test_restore_lid_stands_aside_while_the_app_is_open(self):
        # The app's own mutex, under a unique name: a record waits for the app.
        name = f"Local\\Unminute.Test.{os.getpid()}"
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "Unminute" / "workspace" / "power_restore.json"
            record.parent.mkdir(parents=True)
            record.write_text("{damaged", encoding="utf-8")
            code = ("import sys; sys.argv = ['x', '--restore-lid']; "
                    "import workspace.__main__ as m; "
                    f"m.MUTEX_NAME = {name!r}; "
                    f"held = m._single_instance({name!r}); "
                    "sys.exit(m.main())")
            r = self.run_main(code, dict(os.environ, LOCALAPPDATA=tmp))
            self.assertEqual((r.returncode, r.stdout.strip(), r.stderr),
                             (3, '{"lid": "running"}', ""))
            self.assertTrue(record.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows named mutex")
    def test_only_one_copy_holds_the_workspace(self):
        # A unique name: an installed, running copy of the app holds the real one.
        name = f"Local\\Unminute.Test.{os.getpid()}"
        r = self.run_main("import workspace.__main__ as m; "
                          f"a = m._single_instance({name!r}); "
                          f"b = m._single_instance({name!r}); "
                          "print(a is not None, b is None)")
        self.assertEqual(r.stdout.strip(), "True True", r.stderr)


class WorkspaceLocationTests(unittest.TestCase):
    def check(self, env):
        from workspace import store
        with mock.patch.dict(os.environ, env, clear=True):
            return store.local_appdata()

    def test_missing_relative_or_network_location_is_refused(self):
        from workspace.store import StoreError
        for env in ({}, {"LOCALAPPDATA": ""}, {"LOCALAPPDATA": "relative\\path"},
                    {"LOCALAPPDATA": "\\\\server\\share\\AppData\\Local"}):
            with self.assertRaises(StoreError, msg=repr(env)):
                self.check(env)

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_location_inside_onedrive_is_refused(self):
        from workspace.store import StoreError
        with self.assertRaises(StoreError):
            self.check({"LOCALAPPDATA": "C:\\Users\\a\\OneDrive - Firm\\AppData\\Local",
                        "OneDriveCommercial": "C:\\Users\\a\\OneDrive - Firm"})
        ok = self.check({"LOCALAPPDATA": "C:\\Users\\a\\AppData\\Local",
                         "OneDriveCommercial": "C:\\Users\\a\\OneDrive - Firm"})
        self.assertEqual(ok, Path("C:\\Users\\a\\AppData\\Local"))

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_a_onedrive_variable_without_a_drive_still_counts(self):
        # Python 3.13 and later no longer call "\Users\a\..." absolute (#111 B4).
        from workspace.store import StoreError
        drive = os.path.splitdrive(os.getcwd())[0]          # where realpath puts it
        with self.assertRaises(StoreError):
            self.check({"LOCALAPPDATA": f"{drive}\\Users\\a\\OneDrive - Firm\\AppData\\Local",
                        "OneDriveCommercial": "\\Users\\a\\OneDrive - Firm"})

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_volume_paths_are_local_and_unc_paths_are_not(self):
        from workspace import store
        self.assertFalse(store._network_path("\\\\?\\Volume{1234}\\Users\\a\\AppData"))
        self.assertFalse(store._network_path("C:\\Users\\a\\AppData\\Local"))
        for unc in ("\\\\server\\share\\a", "\\\\?\\UNC\\server\\share\\a", "//server/share"):
            self.assertTrue(store._network_path(unc), unc)

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_a_onedrive_variable_at_the_profile_or_a_drive_root_is_ignored(self):
        local = "C:\\Users\\a\\AppData\\Local"
        for synced in ("C:\\Users\\a", "C:\\Users\\a\\", "C:\\", "C:\\Users"):
            got = self.check({"LOCALAPPDATA": local, "OneDrive": synced,
                              "USERPROFILE": "C:\\Users\\a"})
            self.assertEqual(got, Path(local), synced)

    @unittest.skipUnless(os.name == "nt", "NTFS junctions are Windows-only")
    def test_a_junction_into_onedrive_is_refused(self):
        from workspace.store import StoreError
        with tempfile.TemporaryDirectory() as tmp:
            synced = Path(tmp) / "OneDrive - Firm"
            (synced / "AppData").mkdir(parents=True)
            link = Path(tmp) / "Local"
            r = mklink_junction(link, synced / "AppData", capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            try:
                with self.assertRaises(StoreError):
                    self.check({"LOCALAPPDATA": str(link), "OneDrive": str(synced)})
            finally:
                os.rmdir(link)

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_a_mapped_network_drive_is_refused(self):
        from workspace import store
        from workspace.store import StoreError
        with mock.patch.object(store, "_remote_drive", return_value=True):
            with self.assertRaises(StoreError):
                self.check({"LOCALAPPDATA": "C:\\Users\\a\\AppData\\Local"})


@unittest.skipUnless(os.name == "nt", "NTFS junctions are Windows-only")
class RedirectedDataFolderTests(unittest.TestCase):
    """#219 (D3): %LOCALAPPDATA% itself is local, but a junction at Unminute or
    at Unminute\\workspace takes the workspace into OneDrive or onto a network
    drive. Junctions need no privilege, so these run on every Windows machine.
    Only Unminute\\models may be redirected."""

    SYNCED = "OneDrive - Synthetic Firm"

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.synced = self.tmp / self.SYNCED
        self.share = self.tmp / "Share"
        self.cases = 0
        self.fresh_local()

    def fresh_local(self):
        """A new, empty LOCALAPPDATA for the next case."""
        self.cases += 1
        self.local = self.tmp / f"Local {self.cases}"
        self.local.mkdir()

    def junction(self, at, target, how="drive"):
        """LOCALAPPDATA\\<at> made a junction to target (created first)."""
        return self.make_junction(self.local / at, target, how)

    def make_junction(self, link, target, how="drive"):
        """link made a junction to target (created first), the target spelt
        as how says (see spelt)."""
        link.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=True)
        r = mklink_junction(link, self.spelt(target, how), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # Removes the junction only, never its target, and before the
        # temporary folder goes (cleanups run last in, first out).
        self.addCleanup(lambda: os.path.isjunction(link) and os.rmdir(link))
        return link

    def spelt(self, folder, how):
        """folder as given to mklink /J: "drive" (C:\\...), "\\\\?\\" (the
        long-path prefix before the drive; see BROKEN_SHAPES for what mklink
        stores) or "volume GUID" (the volume's own name,
        \\\\?\\Volume{...}\\..., which realpath keeps as written)."""
        if how == "drive":
            return str(folder)
        if how == "\\\\?\\":
            return "\\\\?\\" + str(folder)
        import ctypes
        drive, rest = os.path.splitdrive(str(folder))
        buffer = ctypes.create_unicode_buffer(64)
        made = ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(drive + "\\", buffer, 64)
        self.assertTrue(made and buffer.value.startswith("\\\\?\\Volume{"), how)
        return buffer.value + rest.lstrip("\\")

    # Ways a data folder junction's target can be missing (#266, from the
    # #265 reviews), each made under base by broken(). Of the spellings, only
    # "volume GUID" stores a valid target spelt differently from "drive"
    # (\??\Volume{...}\... against \??\C:\...). What "\\?\ prefix" stores
    # depends on how mklink is called. Through mklink_junction (as here),
    # given \\?\C:\x it stores \??\C:\?\C:\x: os.readlink shows it, and
    # os.stat fails with WinError 123, not 2 as for "drive" (seen on Windows
    # 11 builds 26200 and 26300). Run from PowerShell, mklink /J stored a
    # valid spelling (build 26300). Here it is one more unreachable target.
    BROKEN_SHAPES = ("missing", "link to a broken link", "inside a broken link",
                     "volume GUID", "\\\\?\\ prefix")

    def broken(self, at, shape, base):
        """LOCALAPPDATA\\<at> made a junction whose target, under base, is
        missing in the given shape. Returns what must stay uncreated."""
        n = self.cases
        gone = base / f"Gone {n}"
        hop = self.tmp / f"Hop {n}"
        if shape in ("missing", "volume GUID", "\\\\?\\ prefix"):
            how = {"volume GUID": "volume GUID", "\\\\?\\ prefix": "\\\\?\\"}.get(shape, "drive")
            link = self.junction(at, gone, how)
            os.rmdir(gone)
        elif shape == "link to a broken link":      # Unminute -> Hop -> (Gone, removed)
            self.make_junction(hop, gone)
            link = self.junction(at, hop)
            os.rmdir(gone)
        else:                                       # Unminute -> Hop\Moved; Hop -> (Gone, removed)
            (gone / "Moved").mkdir(parents=True)
            self.make_junction(hop, gone)
            link = self.junction(at, hop / "Moved")
            os.rmdir(gone / "Moved")
            os.rmdir(gone)
        self.assertTrue(os.path.lexists(link), shape)
        self.assertFalse(os.path.exists(link), shape)    # its target is missing
        return gone

    def test_a_data_folder_junction_whose_target_is_missing_is_refused(self):
        # #266: the app closed without its message (Workspace() failed in
        # mkdir). Now refused with a path-free message that names IT.
        from workspace import store
        from workspace.store import StoreError
        for at in ("Unminute", "Unminute\\workspace"):
            for shape in self.BROKEN_SHAPES:
                with self.subTest(at=at, shape=shape):
                    self.fresh_local()
                    gone = self.broken(at, shape, self.tmp / "Elsewhere")
                    with self.assertRaises(StoreError) as caught:
                        self.default_root(self.env())
                    self.assertEqual(str(caught.exception), store.BROKEN_LINK)
                    self.assert_path_free(str(caught.exception))
                    self.assertFalse(os.path.lexists(gone))      # nothing made there

    def test_a_missing_onedrive_target_spelt_another_way_is_refused(self):
        # The #265 round 2 shapes: a target inside a broken junction, or spelt
        # with the volume's GUID name, hid that the missing target was inside
        # OneDrive (before #266 these two at either level were not refused).
        # Every shape is refused, by either message, and nothing is created
        # in OneDrive.
        from workspace import store
        from workspace.store import StoreError
        for at in ("Unminute", "Unminute\\workspace"):
            for shape in self.BROKEN_SHAPES:
                with self.subTest(at=at, shape=shape):
                    self.fresh_local()
                    gone = self.broken(at, shape, self.synced)
                    with self.assertRaises(StoreError) as caught:
                        self.default_root(self.env())
                    self.assertIn(str(caught.exception),
                                  (store.BROKEN_LINK, store.REDIRECTED["onedrive"]))
                    self.assert_path_free(str(caught.exception))
                    self.assertFalse(os.path.lexists(gone))      # nothing made in OneDrive

    def test_a_junction_this_account_may_not_open_is_not_called_broken(self):
        # _broken_link's documented rule: a target refused for access is not
        # missing, so it keeps its own outcome rather than BROKEN_LINK.
        real_stat = os.stat
        for at in ("Unminute", "Unminute\\workspace"):
            with self.subTest(at=at):
                self.fresh_local()
                link = self.junction(at, self.tmp / "Elsewhere" / f"Locked {self.cases}")
                key = os.path.normcase(str(link))

                refused = []

                def stat(path, *args, _key=key, _refused=refused, **kwargs):
                    if os.path.normcase(os.fspath(path)) == _key:
                        _refused.append(path)
                        raise PermissionError(13, "Access is denied")
                    return real_stat(path, *args, **kwargs)
                with mock.patch("os.stat", stat):
                    self.assertEqual(self.default_root(self.env()),
                                     self.local / "Unminute" / "workspace")
                self.assertTrue(refused, "the patched stat never saw the junction")

    def test_a_working_data_folder_junction_is_not_called_broken(self):
        # A junction to a local folder that exists is still allowed (#219
        # refuses only OneDrive and network drives), and so is a data folder
        # not created yet.
        self.assertEqual(self.default_root(self.env()), self.local / "Unminute" / "workspace")
        for at in ("Unminute", "Unminute\\workspace"):
            with self.subTest(at=at):
                self.fresh_local()
                self.junction(at, self.tmp / "Elsewhere" / f"Here {self.cases}")
                self.assertEqual(self.default_root(self.env()),
                                 self.local / "Unminute" / "workspace")

    def env(self, var="OneDriveCommercial"):
        return {"LOCALAPPDATA": str(self.local), var: str(self.synced)}

    def child_env(self):
        """This process's environment for a child, with the test's folders."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")}
        env.update(self.env())
        return env

    def default_root(self, env):
        from workspace import store
        with mock.patch.dict(os.environ, env, clear=True):
            return store.default_root()

    def on_share(self):
        """_remote_drive as if self.share were a mapped network drive."""
        from workspace import store
        share = store._folder_key(os.path.realpath(self.share))
        return mock.patch.object(store, "_remote_drive",
                                 lambda path: store._folder_key(path).startswith(share))

    def assert_path_free(self, message):
        # P3: no folder name from this computer, only the app's own words.
        for part in (str(self.tmp), self.tmp.name, self.SYNCED, "Share", "Local"):
            self.assertNotIn(part, message)
        self.assertIn("Ask your IT team", message)

    def test_a_data_folder_junction_into_onedrive_is_refused(self):
        from workspace import store
        from workspace.store import StoreError
        for at in ("Unminute", "Unminute\\workspace"):
            for var in ("OneDriveCommercial", "OneDrive"):
                with self.subTest(at=at, var=var):
                    self.fresh_local()
                    target = self.synced / f"Moved {self.cases}"
                    self.junction(at, target)
                    with mock.patch.dict(os.environ, self.env(var), clear=True):
                        self.assertEqual(store.local_appdata(), self.local)  # itself is fine
                    with self.assertRaises(StoreError) as caught:
                        self.default_root(self.env(var))
                    self.assertEqual(str(caught.exception), store.REDIRECTED["onedrive"])
                    self.assert_path_free(str(caught.exception))
                    self.assertEqual(list(target.iterdir()), [])     # nothing written there

    def test_a_junction_into_onedrive_whose_target_is_missing_is_refused(self):
        # Round 1 review: realpath stops at a missing target and returns it as
        # stored, so a junction or 8.3 short name inside that stored target
        # hid OneDrive, and the app then failed in mkdir without its message.
        import ctypes
        from workspace import store
        from workspace.store import StoreError
        alias = self.tmp / "alias"
        self.synced.mkdir(parents=True, exist_ok=True)
        made = mklink_junction(alias, self.synced, capture_output=True, text=True)
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        self.addCleanup(lambda: os.path.isjunction(alias) and os.rmdir(alias))
        spellings = {"junction": alias}
        # The short name, where the volume makes them (CI's may not).
        buffer = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.GetShortPathNameW(str(self.synced), buffer, 1024)
        if buffer.value and Path(buffer.value).name.lower() != self.SYNCED.lower():
            spellings["short name"] = Path(buffer.value)
        for at in ("Unminute", "Unminute\\workspace"):
            for how, synced in spellings.items():
                with self.subTest(at=at, how=how):
                    self.fresh_local()
                    moved = f"Moved {self.cases}"
                    link = self.junction(at, synced / moved)
                    os.rmdir(self.synced / moved)                # the target is gone
                    self.assertFalse(os.path.exists(link))
                    with self.assertRaises(StoreError) as caught:
                        self.default_root(self.env())
                    self.assertEqual(str(caught.exception), store.REDIRECTED["onedrive"])
                    self.assert_path_free(str(caught.exception))
                    self.assertFalse((self.synced / moved).exists())   # nothing made there

    def test_a_data_folder_junction_onto_a_network_drive_is_refused(self):
        from workspace import store
        from workspace.store import StoreError
        for at in ("Unminute", "Unminute\\workspace"):
            with self.subTest(at=at):
                self.fresh_local()
                self.junction(at, self.share / f"Moved {self.cases}")
                with self.on_share():
                    with self.assertRaises(StoreError) as caught:
                        self.default_root(self.env())
                self.assertEqual(str(caught.exception), store.REDIRECTED["network"])
                self.assert_path_free(str(caught.exception))

    def test_redirecting_only_the_models_folder_is_allowed(self):
        models = self.junction("Unminute\\models", self.synced / "Models")
        expected = self.local / "Unminute" / "workspace"
        self.assertEqual(self.default_root(self.env()), expected)
        # The model cache the app uses is still the redirected folder. In a
        # child process: importing workspace.__main__ installs the network guard.
        r = subprocess.run([sys.executable, "-c",
                            "import workspace.__main__ as m; print(m.model_cache())"],
                           cwd=ROOT, env=self.child_env(), capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(r.stdout.strip()), models)
        os.rmdir(models)
        self.junction("Unminute\\models", self.share / "Models")
        with self.on_share():
            self.assertEqual(self.default_root(self.env()), expected)

    @unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
    def test_the_app_does_not_open_a_redirected_workspace(self):
        from workspace import app, store
        from workspace.store import StoreError
        target = self.synced / "Unminute"
        self.junction("Unminute", target)
        with mock.patch.dict(os.environ, self.env(), clear=True):
            with self.assertRaises(StoreError) as caught:
                app.Workspace(cache_dir=self.tmp / "models", start=False, battery=False,
                              protector=keyprotect.TestProtector())
        self.assertEqual(str(caught.exception), store.REDIRECTED["onedrive"])
        self.assertEqual(list(target.iterdir()), [])                 # no workspace.db there

    def run_main(self, flag, before=""):
        """The app's own start in a child process (importing workspace.__main__
        installs the network guard), with this test's folders."""
        return subprocess.run([sys.executable, "-c",
                               f"import json, sys; sys.argv = ['x', {flag!r}]; "
                               f"import workspace.__main__ as m; {before}sys.exit(m.main())"],
                              cwd=ROOT, env=self.child_env(), capture_output=True,
                              text=True, timeout=120)

    def test_status_and_health_report_it_without_a_path(self):
        self.junction("Unminute", self.synced / "Unminute")
        status = self.run_main("--status")
        self.assertEqual(status.returncode, 1, status.stderr)
        self.assert_path_free(status.stdout)
        self.assertIn("redirected into OneDrive", json.loads(status.stdout)["error"])
        health = self.run_main("--health")
        self.assertEqual(json.loads(health.stdout.strip().splitlines()[-1])["workspace"],
                         "location", health.stderr)

    def test_status_health_and_gate_report_a_broken_link_without_a_path(self):
        # #266 (P3): the same message as at start, and no folder in either.
        from workspace import store
        for at in ("Unminute", "Unminute\\workspace"):
            with self.subTest(at=at):
                self.fresh_local()
                self.broken(at, "missing", self.tmp / "Elsewhere")
                status = self.run_main("--status")
                self.assertEqual((status.returncode, status.stderr), (1, ""))
                self.assert_path_free(status.stdout)
                self.assertEqual(json.loads(status.stdout)["error"], store.BROKEN_LINK)
                health = self.run_main("--health")
                self.assertEqual((health.returncode, health.stderr), (1, ""))
                for part in (str(self.tmp), self.tmp.name, "Elsewhere", "Gone"):
                    self.assertNotIn(part, health.stdout)        # states only
                result = json.loads(health.stdout.strip().splitlines()[-1])
                self.assertEqual((result["workspace"], result["healthy"]), ("location", False))
                # The installer's workspace check stops with the same message.
                gate = self.run_main("--gate")
                self.assertEqual((gate.returncode, gate.stderr), (6, ""))
                result = json.loads(gate.stdout.strip().splitlines()[-1])
                self.assertEqual((result["kind"], result["message"]),
                                 ("location", store.BROKEN_LINK))

    @unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
    def test_the_app_names_a_broken_link_at_start(self):
        # #266: the message box, not a silent exit with a traceback. The box
        # is replaced by a print, and the single-instance check by a stand-in
        # (an installed copy may be open). Should the workspace ever open,
        # the start stops before any window (diag.install comes first).
        from workspace import store
        stand_ins = ("m._single_instance = lambda name=None: True; "
                     "m._message = lambda text: print(json.dumps(text)); "
                     "import workspace.diag as d; "
                     "d.install = lambda *a, **k: sys.exit(9); ")
        for at in ("Unminute", "Unminute\\workspace"):
            with self.subTest(at=at):
                self.fresh_local()
                gone = self.broken(at, "missing", self.tmp / "Elsewhere")
                r = self.run_main("--start", stand_ins)
                self.assertEqual((r.returncode, r.stderr), (1, ""))
                self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]),
                                 store.BROKEN_LINK)
                self.assertFalse(os.path.lexists(gone))          # nothing made there


class _Tripwire:
    """A stand-in Workspace for the bridge gate (#111 C4a): touching anything
    but what make_api sets up fails the test."""
    _allowed = ("locked", "log", "recording_indicator")

    def __init__(self, locked):
        object.__setattr__(self, "locked", locked)
        object.__setattr__(self, "log", None)
        object.__setattr__(self, "touched", [])

    def __getattr__(self, name):
        self.touched.append(name)
        raise AssertionError(f"the Workspace was called: {name}")


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class LockedBridgeTests(unittest.TestCase):
    """While locked, the page reaches only LOCKED_METHODS; every other bridge
    method refuses with the fixed text without calling the Workspace (I3)."""

    def call_all(self, page, names):
        import inspect
        out = {}
        for name in sorted(names):
            method = getattr(page, name)
            # Arguments of the right count; their values never matter here.
            params = [p for p in inspect.signature(method).parameters.values()
                      if p.default is inspect.Parameter.empty]
            try:
                out[name] = method(*([None] * len(params)))
            except AssertionError as exc:
                out[name] = exc
        return out

    def test_every_content_method_is_refused_while_locked(self):
        from workspace import api
        from workspace.app import LOCKED_TEXT
        self.assertLessEqual(api.LOCKED_METHODS, api.API_METHODS)
        gated = api.API_METHODS - api.LOCKED_METHODS - {"pywebviewEventHandler"}
        self.assertIn("meetings", gated)
        self.assertIn("continue_despite_damage", gated)     # never offered while locked
        ws = _Tripwire(locked=RuntimeError("locked"))
        page = api.make_api(ws)
        results = self.call_all(page, gated)
        self.assertEqual({n: {"ok": False, "error": LOCKED_TEXT} for n in gated}, results)
        self.assertEqual([], ws.touched)
        # Unlocked, the same calls reach the Workspace: the gate refused them.
        ws = _Tripwire(locked=None)
        results = self.call_all(api.make_api(ws), gated)
        reached = {n for n, r in results.items()
                   if isinstance(r, AssertionError) or r.get("error") != LOCKED_TEXT}
        self.assertEqual(gated, reached)

    def test_the_page_keeps_the_signatures_pywebview_reads(self):
        import inspect

        from workspace import api
        ws = _Tripwire(locked=None)
        page = api.make_api(ws)
        for name in api.API_METHODS - {"pywebviewEventHandler"}:
            fn = getattr(type(page), name)
            # pywebview builds the page's stubs from getfullargspec.
            self.assertNotIn("args", inspect.getfullargspec(fn).args, name)
        self.assertEqual(["self", "mid", "start", "count", "version"],
                         inspect.getfullargspec(type(page).transcript).args)

    @unittest.skipUnless(HAVE_WEBVIEW, "pywebview not installed")
    def test_a_drop_imports_nothing_while_locked(self):
        from workspace import api

        class Events:
            def __init__(self):
                self.handlers = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self

        window = mock.Mock()
        window._drop_enabled = False
        window.dom.document.events.dragover = Events()
        window.dom.document.events.drop = drops = Events()
        ws = mock.Mock(locked=RuntimeError("locked"))
        api.enable_drop(window, ws)
        event = {"dataTransfer": {"files": [{"pywebviewFullPath": "C:\\x\\a.wav"}]}}
        drops.handlers[0].callback(event)
        ws.import_paths.assert_not_called()
        ws.locked = None
        drops.handlers[0].callback(event)
        ws.import_paths.assert_called_once_with(["C:\\x\\a.wav"])


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class StoreTempTests(unittest.TestCase):
    def test_temporary_data_stays_in_memory(self):
        from workspace.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            s = Store(Path(tmp) / "workspace.db", protector=keyprotect.TestProtector())
            try:
                self.assertEqual(s.db.execute("PRAGMA temp_store").fetchone()[0], 2)
            finally:
                s.close()


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class SettingsAndTestEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_test_engines_need_the_test_flag(self):
        from workspace.app import Settings, WorkspaceError
        with self.assertRaises(WorkspaceError):
            Settings(self.path).update(model="test:os:getcwd")
        Settings(self.path, allow_test_engines=True).update(model="test:os:getcwd")

    def test_saved_values_are_checked_when_loaded(self):
        from workspace.app import DEFAULT_SETTINGS, Settings
        self.path.write_text(json.dumps({"model": "test:antigravity:x", "mode": "turbo",
                                         "speakers": "yes", "microphone": True,
                                         "unknown": 1}), encoding="utf-8")
        s = Settings(self.path)
        self.assertEqual(s["model"], DEFAULT_SETTINGS["model"])
        self.assertEqual(s["mode"], DEFAULT_SETTINGS["mode"])
        self.assertEqual(s["speakers"], DEFAULT_SETTINGS["speakers"])
        self.assertIs(s["microphone"], True)               # a valid value is kept
        self.assertEqual(sorted(s.repaired), ["mode", "model", "speakers"])

    def test_damaged_settings_file_falls_back_to_defaults(self):
        from workspace.app import DEFAULT_SETTINGS, Settings
        for content in ("not json", "[1, 2]", "null"):
            self.path.write_text(content, encoding="utf-8")
            self.assertEqual(Settings(self.path).values, DEFAULT_SETTINGS)

    def test_engine_loader_refuses_test_engines_without_the_flag(self):
        from workspace.asr_engines import ModelLoadError
        from workspace import engines
        with self.assertRaises(ModelLoadError):
            engines.load_engine("test:os:getcwd", self.tmp.name, 1)
        with self.assertRaises(ModelLoadError):
            engines._test_factory("test:os:_exit", False)

    def test_workspace_refuses_a_test_speaker_model_without_the_flag(self):
        from workspace.app import Workspace
        with self.assertRaises(ValueError):
            Workspace(Path(self.tmp.name) / "ws", Path(self.tmp.name) / "m", start=False,
                      speaker_model="test:fake_worker_engines:fake_diarizer",
                      protector=keyprotect.TestProtector())


class EnvironmentTokenTests(unittest.TestCase):
    def test_token_leaves_the_environment_and_is_not_kept(self):
        with mock.patch.dict(os.environ, {"HF_TOKEN": " hf_example ",
                                          "HUGGING_FACE_HUB_TOKEN": "hf_example"}):
            credentials.drop_environment_token()
            self.assertNotIn("HF_TOKEN", os.environ)
            self.assertNotIn("HUGGING_FACE_HUB_TOKEN", os.environ)
        self.assertFalse(hasattr(credentials, "earlier_token"))
        self.assertNotIn("hf_example", repr(vars(credentials)))

    def test_no_token_is_fine(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            credentials.drop_environment_token()
            self.assertNotIn("HF_TOKEN", os.environ)

    def test_the_window_process_and_its_children_never_see_it(self):
        # Importing workspace.__main__ (as the app starts) drops it before any
        # child process starts; run in a child so this process keeps its guard off.
        import subprocess
        root = Path(__file__).resolve().parents[1]
        code = ("import os, subprocess, sys\n"
                "import workspace.__main__\n"
                "names = ('HF_TOKEN', 'HUGGING_FACE_HUB_TOKEN')\n"
                "print(any(n in os.environ for n in names))\n"
                "r = subprocess.run([sys.executable, '-c', "
                "'import os; print(any(n in os.environ for n in "
                "(\"HF_TOKEN\", \"HUGGING_FACE_HUB_TOKEN\")))'], "
                "capture_output=True, text=True)\n"
                "print(r.stdout.strip())\n")
        env = dict(os.environ, HF_TOKEN="hf_example", HUGGING_FACE_HUB_TOKEN="hf_example")
        r = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.stdout.split(), ["False", "False"], r.stderr)
        self.assertNotIn("hf_example", r.stdout + r.stderr)


class DataFolderOverlapTests(unittest.TestCase):
    """#136: uninstalling deletes the install folder, so the install folder must
    never be, hold or sit inside %LOCALAPPDATA%\\Unminute (workspace and models),
    and the uninstaller removes only what the installer put there. Both scripts run
    for real, with a temporary LOCALAPPDATA."""

    MESSAGE = "must not be in, or hold"
    PROGRAMS = '[Environment]::GetFolderPath("Programs")'
    KEY = r'$RegKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Unminute"'
    # The copy's uninstall key: absent unless a test creates it, and removed after.
    TEST_KEY = rf"Software\Unminute-UnitTest-uninstall-{os.getpid()}"

    def run_script(self, script, install_dir, local, extra="", location=None, cwd=None):
        call = f"& '{script}' -InstallDir '{install_dir}' -Silent {extra}"
        if location:
            # A relative -InstallDir is read from PowerShell's location, which
            # Set-Location moves away from the process's own folder.
            call = f"Set-Location -LiteralPath '{location}'; {call}"
        return subprocess.run([windows_powershell.executable(), "-NoProfile",
                               "-ExecutionPolicy", "Bypass",
                               "-Command", f"{call}; exit $LASTEXITCODE"],
                              capture_output=True, text=True, timeout=180, cwd=cwd,
                              env=dict(os.environ, LOCALAPPDATA=str(local)))

    @unittest.skipUnless(sys.platform == "win32", "the installer is Windows PowerShell")
    def test_the_installer_refuses_an_install_folder_that_overlaps_the_data(self):
        # A temporary LOCALAPPDATA and a package that does not exist: the checks
        # come first, so nothing real is read or written.
        install = ROOT / "packaging" / "install.ps1"
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "Local"
            local.mkdir()
            absent = f"-Package '{Path(tmp) / 'absent.zip'}'"
            for where in (local / "Unminute", local / "Unminute" / "app", local):
                with self.subTest(where=where.name):
                    r = self.run_script(install, where, local, absent, cwd=tmp)
                    self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                    self.assertIn(self.MESSAGE, r.stderr)
            r = self.run_script(install, "Unminute", local, absent, location=local, cwd=tmp)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn(self.MESSAGE, r.stderr)
            for where in (r"\\localhost\C$\Unminute", "//localhost/C$/Unminute"):
                with self.subTest(where=where):
                    r = self.run_script(install, where, local, absent, cwd=tmp)
                    self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                    self.assertIn("own drive", r.stderr)
            r = self.run_script(install, "", local, absent, cwd=tmp)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("No install folder", r.stderr)
            # The default shape, and a sibling of the data folder: allowed, so the
            # run stops later, at a prerequisite or the missing package.
            for where in (local / "Programs" / "Unminute", local / "Unminute2"):
                with self.subTest(where=where.name):
                    r = self.run_script(install, where, local, absent, cwd=tmp)
                    self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertNotIn(self.MESSAGE, r.stdout + r.stderr)
            self.assertEqual(list(local.iterdir()), [])          # nothing was written

    @unittest.skipUnless(sys.platform == "win32", "the installer is Windows PowerShell")
    def test_the_installer_drops_a_trailing_backslash(self):
        # The registered uninstall command quotes -InstallDir: a trailing \ there
        # reads as \", a literal quote, and the uninstall fails (R13b round 2
        # review). A copy that stops after the folder checks and prints the folder.
        text = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
        stop = "    if ($Options.Rollback) { Invoke-Rollback }"
        self.assertEqual(text.count(stop), 1)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "Local"
            local.mkdir()
            copy = Path(tmp) / "install-copy.ps1"
            report = '    Write-Output "DIR=$($Options.InstallDir)"; exit 0'
            copy.write_text(text.replace(stop, report), encoding="utf-8")
            folder = Path(tmp) / "Apps" / "Unminute"
            for where in (f"{folder}\\", f"{folder}\\\\", f"{folder}/", str(folder)):
                with self.subTest(where=where):
                    r = self.run_script(copy, where, local, cwd=tmp)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertIn(f"DIR={folder}\n", r.stdout)
            self.assertFalse(folder.exists())

    def uninstaller_copy(self, tmp):
        """uninstall.ps1 with this computer's Start menu folder and uninstall key
        swapped for a temporary folder and a key that does not exist, so a
        mistake removes nothing real."""
        text = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertEqual((text.count(self.PROGRAMS), text.count(self.KEY)), (1, 1))
        programs = Path(tmp) / "programs"
        programs.mkdir()
        text = text.replace(self.PROGRAMS, f'"{programs}"').replace(
            self.KEY, f'$RegKey = "HKCU:\\{self.TEST_KEY}"')
        # Nothing else in it may reach this computer's own shortcuts or registry.
        for reach in ("GetFolderPath", "Start Menu\\", "$env:APPDATA", "HKCU:\\Software\\Microsoft",
                      "HKLM", "Registry::", "ComObject"):
            self.assertNotIn(reach.lower(), text.lower(), reach)
        copy = Path(tmp) / "uninstall-copy.ps1"
        copy.write_text(text, encoding="utf-8")
        return copy, programs / "Unminute.lnk"

    @unittest.skipUnless(sys.platform == "win32", "the uninstaller is Windows PowerShell")
    def test_the_uninstaller_removes_only_its_own_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "Local"
            data = local / "Unminute" / "workspace"
            data.mkdir(parents=True)
            kept = data / "workspace.db"
            kept.write_text("synthetic")
            copy, link = self.uninstaller_copy(tmp)
            link.write_text("shortcut")
            for where, location in ((local / "Unminute", None), (data, None), (local, None),
                                    ("Unminute", local)):           # relative to the location
                with self.subTest(where=str(where)):
                    r = self.run_script(copy, where, local, location=location, cwd=tmp)
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertIn("data folder", r.stderr)      # this check, not a later one
                    self.assertIn("so nothing was removed", r.stderr)
                    self.assertTrue(kept.is_file() and link.is_file())
            r = self.run_script(copy, "", local, cwd=tmp)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("No install folder", r.stderr)
            # An install folder holding something the installer did not put there
            # is left as it is.
            install = local / "Programs" / "Unminute"
            (install / "versions" / "0.5.0").mkdir(parents=True)
            (install / "current.json").write_text("{}")
            shutil.copy(copy, install / "uninstall.ps1")
            (install / "notes.txt").write_text("not the installer's")
            r = self.run_script(copy, install, local, cwd=tmp)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("did not put there", r.stderr)
            self.assertTrue((install / "versions" / "0.5.0").is_dir() and link.is_file())
            # Only its own: all removed, with the shortcut, and the data kept.
            (install / "notes.txt").unlink()
            r = self.run_script(copy, install, local, cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(install.exists())
            self.assertFalse(link.exists())
            self.assertTrue(kept.is_file())

    @unittest.skipUnless(sys.platform == "win32", "the uninstaller is Windows PowerShell")
    def test_the_uninstaller_needs_an_install_or_the_registered_folder(self):
        # A wrong -InstallDir (missing, empty, or a network or device form) must not
        # remove the real install's shortcut and uninstall entry and report success
        # (R13b review). The registered folder, with its files already gone, may.
        import winreg
        self.addCleanup(self.delete_test_key)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "Local"
            local.mkdir()
            copy, link = self.uninstaller_copy(tmp)
            link.write_text("shortcut")
            empty = Path(tmp) / "empty"
            empty.mkdir()
            typo = Path(tmp) / "typo"
            for where, says in ((typo, "No Unminute install"),
                                (empty, "No Unminute install"),
                                (r"\\localhost\C$\Programs\Unminute", "own drive"),
                                ("//localhost/C$/Programs/Unminute", "own drive"),
                                ("\\\\?\\" + str(typo), "own drive")):
                with self.subTest(where=str(where)):
                    r = self.run_script(copy, where, local, cwd=tmp)
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertIn(says, r.stderr)
                    self.assertIn("so nothing was removed", r.stderr)
                    self.assertTrue(link.is_file() and empty.is_dir())
            # An uninstall entry for another folder does not make this one an install.
            registered = Path(tmp) / "Programs" / "Unminute"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.TEST_KEY) as key:
                winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(registered))
            r = self.run_script(copy, typo, local, cwd=tmp)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertTrue(link.is_file() and self.key_exists())
            # An older or second install, while the entry names another folder that
            # still holds an install: its own files go, but the shortcut and the
            # entry are the other install's (R13b round 2 review).
            (registered / "versions" / "0.5.0").mkdir(parents=True)
            older = Path(tmp) / "Programs" / "OldInstall"
            (older / "versions" / "0.4.3").mkdir(parents=True)
            (older / "current.json").write_text("{}")
            r = self.run_script(copy, older, local, cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(older.exists())
            self.assertTrue(link.is_file() and self.key_exists())
            self.assertTrue(registered.is_dir())
            # The registered folder, already deleted: the shortcut and entry go.
            shutil.rmtree(registered)
            r = self.run_script(copy, registered, local, cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(link.exists())
            self.assertFalse(self.key_exists())

    @unittest.skipUnless(sys.platform == "win32", "the uninstaller is Windows PowerShell")
    def test_the_registered_folder_named_another_way_removes_its_entries(self):
        # The registered folder given through a junction, or by its 8.3 short name,
        # is the same install, not another one: the shortcut and the entry go with
        # it (R13b round 3 review).
        import ctypes
        import winreg
        self.addCleanup(self.delete_test_key)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "Local"
            local.mkdir()
            copy, link = self.uninstaller_copy(tmp)
            parent = Path(tmp) / "Programs With A Long Name"
            registered = parent / "Unminute"
            junction = Path(tmp) / "alias"
            made = mklink_junction(junction, parent, capture_output=True, text=True,
                                   timeout=30)
            self.assertEqual(0, made.returncode, made.stdout + made.stderr)
            aliases = [str(junction / "Unminute")]
            # The short name, where the volume makes them (CI's may not).
            registered.mkdir(parents=True)
            buffer = ctypes.create_unicode_buffer(1024)
            ctypes.windll.kernel32.GetShortPathNameW(str(registered), buffer, 1024)
            if buffer.value and buffer.value.lower() != str(registered).lower():
                aliases.append(buffer.value)
            for alias in aliases:
                with self.subTest(alias=alias):
                    (registered / "versions" / "0.5.0").mkdir(parents=True, exist_ok=True)
                    (registered / "current.json").write_text("{}")
                    link.write_text("shortcut")
                    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.TEST_KEY) as key:
                        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ,
                                          str(registered))
                    r = self.run_script(copy, alias, local, cwd=tmp)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertFalse((registered / "current.json").exists())
                    self.assertFalse(link.exists())
                    self.assertFalse(self.key_exists())
                    registered.mkdir(parents=True, exist_ok=True)
            os.rmdir(junction)

    def key_exists(self):
        import winreg
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.TEST_KEY))
            return True
        except FileNotFoundError:
            return False

    def delete_test_key(self):
        import winreg
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.TEST_KEY)
        except FileNotFoundError:
            pass

    def test_both_scripts_have_the_same_overlap_check(self):
        # The installer's copy is the one run for every case above.
        def check(name):
            text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
            start = text.index("# The data (workspace and models) is in")
            return text[start:text.index("\n}\n", start) + 3]
        self.assertEqual(check("install.ps1"), check("uninstall.ps1"))
        self.assertIn("function Get-DataOverlap", check("uninstall.ps1"))

    @unittest.skipUnless(sys.platform == "win32", "the uninstaller is Windows PowerShell")
    def test_the_uninstaller_never_removes_through_a_link(self):
        # #297 review Q2: a junction inside a version is removed as a link and what
        # it points to stays; a junction at versions or current.json stops the
        # uninstall before anything is removed.
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "Local"
            local.mkdir()
            copy, link = self.uninstaller_copy(tmp)
            target = Path(tmp) / "someone"
            (target / "versions" / "0.5.0").mkdir(parents=True)
            (target / "sentinel.txt").write_text("keep")
            kept = snapshot(target)

            def junction(at, to):
                made = mklink_junction(at, to, capture_output=True, text=True, timeout=30)
                self.assertEqual(0, made.returncode, made.stdout + made.stderr)
                self.addCleanup(lambda: os.path.lexists(at) and os.rmdir(at))

            def install(name):
                folder = Path(tmp) / name
                folder.mkdir()
                (folder / "current.json").write_text('{"version": "0.5.0"}')
                shutil.copy(copy, folder / "uninstall.ps1")
                return folder

            inside = install("inside")
            (inside / "versions" / "0.5.0" / "app").mkdir(parents=True)
            (inside / "versions" / "0.5.0" / "app" / "held.txt").write_text("ours")
            junction(inside / "versions" / "0.5.0" / "app" / "link", target)
            link.write_text("shortcut")
            r = self.run_script(copy, inside, local, cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(os.path.lexists(inside))
            self.assertEqual(snapshot(target), kept)
            for at in ("versions", "current.json"):
                with self.subTest(at=at):
                    folder = install(f"as-{at}")
                    if at == "current.json":
                        (folder / "current.json").unlink()
                        (folder / "versions" / "0.5.0").mkdir(parents=True)
                    junction(folder / at, target if at == "current.json" else target / "versions")
                    link.write_text("shortcut")
                    before = snapshot(folder)
                    r = self.run_script(copy, folder, local, cwd=tmp)
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertIn("link or junction", r.stderr)
                    self.assertIn("so nothing was removed", r.stderr)
                    self.assertEqual(snapshot(folder), before)
                    self.assertEqual(snapshot(target), kept)
                    self.assertTrue(link.is_file())

    def test_both_scripts_remove_folders_the_same_way(self):
        # #297: the uninstaller removes with the installer's walk, which never
        # follows a link, not with Remove-Item -Recurse.
        def walk(name):
            text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
            start = text.index("# Removes a folder the installer made without following links")
            end = text.index("\n}\n", text.index("function Remove-FolderTree")) + 3
            return text[start:end]
        self.assertEqual(walk("install.ps1"), walk("uninstall.ps1"))
        for function in ("Test-Link", "Remove-OwnFolder", "Remove-FolderTree"):
            self.assertIn(f"function {function}(", walk("uninstall.ps1"))
        uninstall = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertIn("Invoke-LockRetry { Remove-OwnFolder $path }", uninstall)
        self.assertNotRegex(uninstall,
                            r"Remove-Item[^\n]*\$(path|Options\.InstallDir)[^\n]*-Recurse")


# A hook for copies of install.ps1 and uninstall.ps1 (FolderInUseTests): at the named
# step, the copy says it is ready, then waits until the test says go.
WAIT_FOR_TEST = '''\
function Wait-ForTest([string]$Stage) {
    if ($env:UNMINUTE_TEST_WAIT -ne $Stage) { return }
    New-Item -ItemType File -Path "$env:UNMINUTE_TEST_SYNC.ready" | Out-Null
    while (-not (Test-Path -LiteralPath "$env:UNMINUTE_TEST_SYNC.go")) {
        Start-Sleep -Milliseconds 20
    }
}
'''
WINDOWS_ONLY = unittest.skipUnless(sys.platform == "win32", "the installer is Windows PowerShell")


def lock_retry_block(name):
    """The wait for a file in use (#239), as packaging/<name> has it: the time limit,
    Test-TransientLock and Invoke-LockRetry."""
    text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
    start = text.index("# A folder cannot be renamed or removed")
    return text[start:text.index("\n}\n", text.index("function Invoke-LockRetry")) + 3]


class FolderInUseTests(unittest.TestCase):
    """#239: a folder cannot be renamed or removed while a file in it is open, and
    antivirus software opens newly written files for a moment to scan them. The
    installer's two renames and the uninstaller's removal are tried again for up to
    30 seconds from the first refusal, for each step, when refused as access denied
    or in use, then fail with the message
    they always gave; any other error fails at once. Copies of the scripts run for
    real in temporary folders with a temporary LOCALAPPDATA (DataFolderOverlapTests);
    the installer's copy runs only Install-Version, on a small synthetic package. A
    hook pauses each copy just before the step while the test opens a file in the
    folder, as a scanner might."""

    PROGRAMS, KEY, TEST_KEY = (DataFolderOverlapTests.PROGRAMS, DataFolderOverlapTests.KEY,
                               DataFolderOverlapTests.TEST_KEY)
    uninstaller_copy = DataFolderOverlapTests.uninstaller_copy
    BOUND = "$LockRetrySeconds = 30\n"
    START = '$ErrorActionPreference = "Stop"\n'
    MAIN = "    if ($Options.Rollback) { Invoke-Rollback }\n"
    STAGING = "Rename-Item -LiteralPath $staging -NewName (Split-Path $target -Leaf)"
    OLD = "Rename-Item -LiteralPath $target -NewName (Split-Path $old -Leaf)"
    REMOVE = 'foreach ($name in "versions", "current.json")'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.local = self.root / "Local"
        self.local.mkdir()
        self.sync = self.root / "sync"
        self.package = self.root / "package"
        self.write_package(self.package, "new")
        self.install = self.root / "Apps"
        self.versions = self.install / "versions"

    @staticmethod
    def write_package(folder, label, version="1.0.0"):
        (folder / "app").mkdir(parents=True)
        (folder / "app" / "held.txt").write_text(label)
        (folder / "runtime").mkdir()
        (folder / "runtime" / "python.exe").write_bytes(b"synthetic")
        (folder / "uninstall.ps1").write_text("# synthetic\n")
        # As every package has: a live version names itself (the installer's folder
        # check, #297, takes a version folder without it for someone else's).
        (folder / "version.json").write_text(json.dumps({"version": version}))

    def with_hooks(self, text, steps, seconds=None):
        """text with the hook, called just before the line holding each step, and
        with the time limit set to seconds."""
        self.assertEqual(text.count(self.START), 1)
        text = text.replace(self.START, self.START + WAIT_FOR_TEST)
        if seconds is not None:
            self.assertEqual(text.count(self.BOUND), 1)
            text = text.replace(self.BOUND, f"$LockRetrySeconds = {seconds}\n")
        lines = text.split("\n")
        for stage, step in steps.items():
            at = [i for i, line in enumerate(lines) if step in line]
            self.assertEqual(len(at), 1, step)
            indent = lines[at[0]][:len(lines[at[0]]) - len(lines[at[0]].lstrip())]
            lines.insert(at[0], f"{indent}Wait-ForTest '{stage}'")
        return "\n".join(lines)

    def installer(self, seconds=None):
        """install.ps1, after its folder checks, running only Install-Version."""
        text = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
        self.assertEqual(text.count(self.MAIN), 1)
        text = text.replace(self.MAIN,
                            '    $target = Install-Version $env:UNMINUTE_TEST_PACKAGE "1.0.0"\n'
                            '    Write-Output "TARGET=$target"; exit 0\n')
        copy = self.root / "install-copy.ps1"
        copy.write_text(self.with_hooks(text, {"old": self.OLD, "staging": self.STAGING}, seconds),
                        encoding="utf-8")
        return copy

    def uninstaller(self, seconds=None):
        """The uninstaller's copy (DataFolderOverlapTests), and an install for it."""
        copy, link = self.uninstaller_copy(self.root)
        copy.write_text(self.with_hooks(copy.read_text(encoding="utf-8"),
                                        {"remove": self.REMOVE}, seconds), encoding="utf-8")
        link.write_text("shortcut")
        self.write_package(self.versions / "0.5.0", "old", "0.5.0")
        (self.install / "current.json").write_text("{}")
        shutil.copy(copy, self.install / "uninstall.ps1")
        return copy, link

    def hold(self, path, release_after=None):
        """Opens path without letting it be deleted, as a scanner might, until
        release_after seconds have passed or the test ends."""
        held = open(path, "rb")
        self.addCleanup(held.close)
        if release_after is not None:
            timer = threading.Timer(release_after, held.close)
            timer.daemon = True
            timer.start()
            self.addCleanup(timer.cancel)

    def run_copy(self, copy, stage, action, silent=True):
        """Runs the copy; when it reaches stage, action() runs while the copy waits.
        Returns the result and the seconds from then until the copy finished."""
        ready, go = Path(f"{self.sync}.ready"), Path(f"{self.sync}.go")
        ready.unlink(missing_ok=True)
        go.unlink(missing_ok=True)
        went, failed, stop = [], [], threading.Event()

        def watch():
            while not ready.exists():
                if stop.wait(0.02):
                    return
            try:
                action()
            except Exception as e:                  # reported below; the copy goes on
                failed.append(e)
            went.append(time.monotonic())
            go.write_text("")

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        call = f"& '{copy}' -InstallDir '{self.install}'" + (" -Silent" if silent else "")
        env = dict(os.environ, LOCALAPPDATA=str(self.local), UNMINUTE_TEST_WAIT=stage,
                   UNMINUTE_TEST_SYNC=str(self.sync), UNMINUTE_TEST_PACKAGE=str(self.package))
        r = subprocess.run([windows_powershell.executable(), "-NoProfile",
                            "-ExecutionPolicy", "Bypass",
                            "-Command", f"{call}; exit $LASTEXITCODE"],
                           capture_output=True, text=True, timeout=180, cwd=self.root, env=env)
        done = time.monotonic()
        stop.set()
        watcher.join(10)
        if failed:
            raise failed[0]
        self.assertTrue(went, "the copy never reached the step: " + r.stdout + r.stderr)
        return r, done - went[0]

    @WINDOWS_ONLY
    def test_a_staging_folder_in_use_for_a_moment_is_renamed_once_it_is_free(self):
        staging = self.versions / "1.0.0.partial"
        r, waited = self.run_copy(self.installer(), "staging",
                                  lambda: self.hold(staging / "app" / "held.txt", 3), silent=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"TARGET={self.versions / '1.0.0'}\n", r.stdout)
        self.assertEqual((self.versions / "1.0.0" / "app" / "held.txt").read_text(), "new")
        self.assertFalse(staging.exists())
        self.assertGreater(waited, 2.5)              # refused until the file was free
        self.assertEqual(r.stdout.count("Trying again for up to 30 seconds"), 1, r.stdout)

    @WINDOWS_ONLY
    def test_a_reinstall_waits_for_the_live_version_in_use_for_a_moment(self):
        live = self.versions / "1.0.0"
        self.write_package(live, "old")
        r, waited = self.run_copy(self.installer(), "old",
                                  lambda: self.hold(live / "app" / "held.txt", 3))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual((live / "app" / "held.txt").read_text(), "new")
        self.assertEqual(sorted(p.name for p in self.versions.iterdir()), ["1.0.0"])
        self.assertGreater(waited, 2.5)

    @WINDOWS_ONLY
    def test_a_staging_folder_that_stays_in_use_fails_as_before(self):
        # A 3-second limit for speed; the scripts' own is 30 seconds
        # (test_both_scripts_wait_the_same_way).
        staging = self.versions / "1.0.0.partial"
        r, waited = self.run_copy(self.installer(seconds=3), "staging",
                                  lambda: self.hold(staging / "app" / "held.txt"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertTrue(r.stderr.startswith("Installation failed: "), r.stderr)
        self.assertIn("1.0.0.partial", r.stderr)
        self.assertTrue(staging.is_dir())            # the next run removes it
        self.assertFalse((self.versions / "1.0.0").exists())
        self.assertTrue(2.5 < waited < 20, waited)

    @WINDOWS_ONLY
    def test_a_live_version_that_stays_in_use_is_left_in_place(self):
        live = self.versions / "1.0.0"
        self.write_package(live, "old")
        r, waited = self.run_copy(self.installer(seconds=3), "old",
                                  lambda: self.hold(live / "app" / "held.txt"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertTrue(r.stderr.startswith("Installation failed: "), r.stderr)
        self.assertEqual((live / "app" / "held.txt").read_text(), "old")
        self.assertFalse((self.versions / "1.0.0.old").exists())
        self.assertTrue(2.5 < waited < 20, waited)

    @WINDOWS_ONLY
    def test_any_other_error_fails_at_once(self):
        # With the scripts' own 30-second limit: no waiting for an error a retry
        # cannot fix, such as a folder that is already there or one that is gone.
        staging, target = self.versions / "1.0.0.partial", self.versions / "1.0.0"
        for name, action in (("already there", lambda: (target / "app").mkdir(parents=True)),
                             ("gone", lambda: shutil.rmtree(staging))):
            with self.subTest(name):
                shutil.rmtree(self.install, ignore_errors=True)
                r, waited = self.run_copy(self.installer(), "staging", action)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertTrue(r.stderr.startswith("Installation failed: "), r.stderr)
                self.assertLess(waited, 10)

    @WINDOWS_ONLY
    def test_the_uninstaller_waits_for_a_file_in_use_for_a_moment(self):
        copy, link = self.uninstaller()
        held = self.versions / "0.5.0" / "app" / "held.txt"
        r, waited = self.run_copy(copy, "remove", lambda: self.hold(held, 3))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.install.exists())
        self.assertFalse(link.exists())
        self.assertGreater(waited, 2.5)

    @WINDOWS_ONLY
    def test_the_uninstaller_fails_as_before_when_the_file_stays_in_use(self):
        copy, link = self.uninstaller(seconds=3)
        held = self.versions / "0.5.0" / "app" / "held.txt"
        r, waited = self.run_copy(copy, "remove", lambda: self.hold(held))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertTrue(r.stderr.startswith("Removal failed: "), r.stderr)
        # Still shows as installed: current.json, the shortcut and the held file stay.
        self.assertTrue((self.install / "current.json").is_file())
        self.assertTrue(held.is_file() and link.is_file())
        self.assertTrue(2.5 < waited < 20, waited)

    @WINDOWS_ONLY
    def test_the_time_limit_counts_from_the_first_refusal(self):
        # The uninstaller's removal of a large versions folder can run for tens of
        # seconds before it reaches a file a scanner holds; it still gets the whole
        # wait, which starts at the first refusal, not at the start of the step. A
        # 2-second limit for speed, with a first attempt that takes 3 seconds.
        script = self.root / "retry.ps1"

        def run(attempt):
            script.write_text(
                '$ErrorActionPreference = "Stop"\n'
                '$Options = @{ Silent = $false }\n'
                'function Write-Step([string]$Message) { Write-Output $Message }\n'
                + lock_retry_block("uninstall.ps1").replace(self.BOUND, "$LockRetrySeconds = 2\n")
                + '$script:tries = 0\n'
                'try {\n'
                '    Invoke-LockRetry {\n'
                '        $script:tries++\n'
                f'        {attempt}\n'
                '    }\n'
                '    Write-Output "DONE tries=$script:tries"\n'
                '} catch { Write-Output "FAILED tries=$script:tries $($_.Exception.Message)" }\n',
                encoding="utf-8")
            r = subprocess.run([windows_powershell.executable(), "-NoProfile",
                                "-ExecutionPolicy", "Bypass",
                                "-File", str(script)], capture_output=True, text=True,
                               timeout=60, cwd=self.root)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            return r.stdout

        slow_then_refused = ('if ($script:tries -eq 1) { Start-Sleep -Seconds 3; '
                             'throw [UnauthorizedAccessException]::new("held") }')
        out = run(slow_then_refused)
        self.assertIn("DONE tries=2", out)
        self.assertEqual(out.count("Trying again for up to 2 seconds"), 1, out)
        # Still bounded: a step refused every time fails once the wait has passed.
        start = time.monotonic()
        out = run('Start-Sleep -Milliseconds 500; throw [UnauthorizedAccessException]::new("held")')
        self.assertIn("FAILED", out)
        self.assertIn("held", out)
        self.assertGreater(int(out.split("FAILED tries=")[1].split()[0]), 2)
        self.assertLess(time.monotonic() - start, 20)

    def test_both_scripts_wait_the_same_way(self):
        block = lock_retry_block
        self.assertEqual(block("install.ps1"), block("uninstall.ps1"))
        self.assertIn(self.BOUND, block("install.ps1"))     # about 30 seconds a step (#239)
        install = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
        for step in (self.STAGING, self.OLD):
            self.assertIn("Invoke-LockRetry { " + step, install)
        uninstall = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertIn("Invoke-LockRetry { Remove-OwnFolder $path }", uninstall)


# Stand-ins that a copy of install.ps1 defines just before its main part
# (InstallFolderScopeTests): the prerequisites, the workspace check, the app-running
# check, the shortcut and the uninstall entry do nothing, and the package is a
# synthetic folder. The gate's stand-in says that it ran.
INSTALL_STUBS = '''\
function Test-Prerequisite { }
function Test-AppRunning { }
function Test-WorkspaceGate { Write-Output "GATE RAN" }
function Get-PackageRoot { return $env:UNMINUTE_TEST_PACKAGE }
function New-Shortcut { }
function Register-Uninstall { }
'''


def snapshot(folder):
    """Every path under folder, with each file's text, without following links."""
    found, todo = {}, [Path(folder)]
    while todo:
        for path in sorted(todo.pop().iterdir()):
            rel = path.relative_to(folder).as_posix()
            if path.is_junction() or path.is_symlink():
                found[rel] = "<link>"
            elif path.is_file():
                found[rel] = path.read_text(encoding="utf-8", errors="replace")
            else:
                found[rel] = "<dir>"
                todo.append(path)
    return found


@WINDOWS_ONLY
class InstallFolderScopeTests(unittest.TestCase):
    """#297 (D2 red-team): the installer writes and removes only inside a folder
    that is an install (current.json) or holds only what an install that stopped
    early leaves; it removes only folders it made, never through a link; and the
    versions it reads from version.json and current.json must be plain folder names
    before they become paths. A copy of install.ps1 runs for real in temporary
    folders, with a temporary LOCALAPPDATA and APPDATA, its Start menu folder and
    uninstall key swapped for temporary ones, and the steps that would reach this
    computer (prerequisites, the workspace check, the shortcut and the uninstall
    entry) replaced by stand-ins (INSTALL_STUBS). The package is synthetic."""

    MAIN = "# -- main -----"
    PROGRAMS, KEY, TEST_KEY = (DataFolderOverlapTests.PROGRAMS, DataFolderOverlapTests.KEY,
                               DataFolderOverlapTests.TEST_KEY)
    NOT_OURS = "already holds files that Unminute did not put there"
    LINK = "holds a link or junction"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # The long form of the path: the installer's manifest check compares paths
        # as Get-ChildItem spells them, which expands an 8.3 short name.
        self.root = Path(os.path.realpath(tmp.name))
        self.local = self.root / "Local"
        self.local.mkdir()
        self.appdata = self.root / "Roaming"
        self.appdata.mkdir()
        self.install = self.root / "Apps"
        self.versions = self.install / "versions"
        self.package = self.root / "package"
        self.copy = self.installer_copy()

    def installer_copy(self):
        text = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
        self.assertEqual((text.count(self.MAIN), text.count(self.PROGRAMS), text.count(self.KEY)),
                         (1, 1, 1))
        programs = self.root / "programs"
        programs.mkdir()
        text = text.replace(self.PROGRAMS, f'"{programs}"').replace(
            self.KEY, f'$RegKey = "HKCU:\\{self.TEST_KEY}"')
        text = text.replace(self.MAIN, INSTALL_STUBS + self.MAIN)
        copy = self.root / "install-copy.ps1"
        copy.write_text(text, encoding="utf-8")
        return copy

    def write_package(self, version="1.0.0", folder=None, schema=None):
        """A synthetic package: a few files, version.json (stating a storage
        format if schema is given) and the manifest."""
        import hashlib
        folder = folder or self.package
        shutil.rmtree(folder, ignore_errors=True)
        (folder / "app").mkdir(parents=True)
        (folder / "app" / "held.txt").write_text("new")
        (folder / "runtime").mkdir()
        (folder / "runtime" / "python.exe").write_bytes(b"synthetic")
        (folder / "uninstall.ps1").write_text("# synthetic\n")
        info = {"name": "Unminute", "version": version, "features": ["gate", "restore-lid"]}
        if schema is not None:
            info["schema"] = schema
        (folder / "version.json").write_text(json.dumps(info))
        manifest = {}
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                data = path.read_bytes()
                manifest[path.relative_to(folder).as_posix()] = [len(data),
                                                                 hashlib.sha256(data).hexdigest()]
        (folder / "package-manifest.json").write_text(json.dumps(manifest))

    @staticmethod
    def version_folder(folder, version=None, schema=None):
        """An installed version as the installer leaves it: version.json names it."""
        (folder / "runtime").mkdir(parents=True)
        (folder / "runtime" / "python.exe").write_bytes(b"synthetic")
        (folder / "uninstall.ps1").write_text("# synthetic\n")
        info = {"name": "Unminute", "version": version or folder.name, "features": ["gate"]}
        if schema is not None:
            info["schema"] = schema
        (folder / "version.json").write_text(json.dumps(info))

    def write_current(self, **record):
        self.install.mkdir(parents=True, exist_ok=True)
        (self.install / "current.json").write_text(json.dumps(record))

    def current(self):
        return json.loads((self.install / "current.json").read_text(encoding="utf-8-sig"))

    def run_installer(self, extra="", install=None):
        call = f"& '{self.copy}' -InstallDir '{install or self.install}' -Silent {extra}"
        # The helper's minimal environment: from pwsh 7 (as in CI), an inherited
        # PSModulePath hides 5.1's own Get-FileHash, which the manifest check uses.
        env = windows_powershell.environment({
            "LOCALAPPDATA": str(self.local), "APPDATA": str(self.appdata),
            "UNMINUTE_TEST_PACKAGE": str(self.package)})
        return subprocess.run([windows_powershell.executable(), "-NoProfile",
                               "-ExecutionPolicy", "Bypass",
                               "-Command", f"{call}; exit $LASTEXITCODE"],
                              capture_output=True, text=True, timeout=180, cwd=self.root,
                              env=env)

    def junction(self, link, target):
        made = mklink_junction(link, target, capture_output=True, text=True, timeout=30)
        self.assertEqual(0, made.returncode, made.stdout + made.stderr)
        self.addCleanup(lambda: os.path.lexists(link) and os.rmdir(link))

    def test_an_update_removes_only_the_versions_unminute_made(self):
        # The current and previous versions stay, an older Unminute version goes,
        # and anything else in versions is left in place and counted, without names.
        self.write_package("1.0.0")
        self.write_current(version="0.9.0", previous="0.8.0", managed=True)
        for name in ("0.9.0", "0.8.0", "0.7.0"):
            self.version_folder(self.versions / name)
        (self.versions / "project-a").mkdir()
        (self.versions / "project-a" / "data.txt").write_text("someone's work")
        (self.versions / "0.6.0").mkdir()                       # no version.json
        (self.versions / "0.6.0" / "data.txt").write_text("someone's work")
        self.version_folder(self.versions / "0.5.0", "0.5.1")   # names another version
        self.version_folder(self.versions / "Tool", "tool")     # case differs
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(p.name for p in self.versions.iterdir()),
                         ["0.5.0", "0.6.0", "0.9.0", "1.0.0", "Tool", "project-a"])
        self.assertEqual((self.versions / "project-a" / "data.txt").read_text(), "someone's work")
        self.assertEqual(self.current()["version"], "1.0.0")
        self.assertEqual(self.current()["previous"], "0.9.0")
        self.assertTrue(self.current()["managed"])
        self.assertIn("4 folder(s) in the versions folder were not made by Unminute", r.stderr)
        for name in ("project-a", "0.6.0", "Tool", str(self.root)):
            self.assertNotIn(name, r.stdout + r.stderr)

    def test_a_folder_that_is_not_an_install_is_refused_and_nothing_changes(self):
        def red_team(folder):                 # the D2 reproduction, with no current.json
            (folder / "versions" / "project-a").mkdir(parents=True)
            (folder / "versions" / "project-a" / "data.txt").write_text("someone's work")
            (folder / "versions" / "project-b").mkdir()
            (folder / "versions" / "1.0.0").mkdir()          # named like it, but empty

        def other_file(folder):               # a version of ours beside someone's file
            self.version_folder(folder / "versions" / "0.9.0")
            (folder / "notes.txt").write_text("someone's notes")

        def only_a_script(folder):            # someone's uninstall.ps1, no version
            folder.mkdir(parents=True)
            (folder / "uninstall.ps1").write_text("someone's script")

        def a_file_in_versions(folder):
            (folder / "versions").mkdir(parents=True)
            (folder / "versions" / "list.txt").write_text("someone's list")

        self.write_package("1.0.0")
        for make in (red_team, other_file, only_a_script, a_file_in_versions):
            with self.subTest(make.__name__):
                folder = self.root / make.__name__
                make(folder)
                before = snapshot(folder)
                r = self.run_installer(install=folder)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(self.NOT_OURS, r.stderr)
                self.assertIn("-InstallDir", r.stderr)
                self.assertNotIn(str(folder), r.stdout + r.stderr)
                self.assertEqual(snapshot(folder), before)
        # A file where the folder should be.
        afile = self.root / "a-file"
        afile.write_text("someone's file")
        r = self.run_installer(install=afile)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(afile.read_text(), "someone's file")

    def test_an_empty_or_new_folder_and_an_existing_install_are_accepted(self):
        self.write_package("1.0.0")
        for name in ("new", "empty"):
            with self.subTest(name):
                folder = self.root / name
                if name == "empty":
                    folder.mkdir()
                r = self.run_installer(install=folder)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertTrue((folder / "versions" / "1.0.0" / "version.json").is_file())
                self.assertTrue((folder / "uninstall.ps1").is_file())
        # An install (current.json) updates; what else is in it is left alone.
        self.write_current(version="0.9.0", previous=None, managed=False)
        self.version_folder(self.versions / "0.9.0")
        (self.install / "uninstall.ps1").write_text("# synthetic\n")
        (self.install / "readme.txt").write_text("someone's note")
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.current()["version"], self.current()["previous"]),
                         ("1.0.0", "0.9.0"))
        self.assertEqual((self.install / "readme.txt").read_text(), "someone's note")
        # The same version again.
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.current()["version"], self.current()["previous"]),
                         ("1.0.0", "0.9.0"))
        self.assertEqual(sorted(p.name for p in self.versions.iterdir()), ["0.9.0", "1.0.0"])

    def test_an_install_that_stopped_before_current_json_finishes(self):
        # What a first install leaves when it stops after copying (#245, #239): a
        # complete version and the uninstaller, or a copy still in progress, or one
        # of each from an earlier try. A rerun finishes it.
        def copied(folder):
            self.version_folder(folder / "versions" / "1.0.0")
            (folder / "uninstall.ps1").write_text("# synthetic\n")

        def copying(folder):
            (folder / "versions" / "1.0.0.partial" / "app").mkdir(parents=True)
            (folder / "versions" / "1.0.0.partial" / "app" / "held.txt").write_text("half")

        def replaced(folder):
            self.version_folder(folder / "versions" / "1.0.0.old", "1.0.0")
            self.version_folder(folder / "versions" / "0.9.0")
            (folder / "versions" / "0.9.0.partial").mkdir()
            (folder / "uninstall.ps1").write_text("# synthetic\n")

        self.write_package("1.0.0")
        for make in (copied, copying, replaced):
            with self.subTest(make.__name__):
                folder = self.root / make.__name__
                make(folder)
                r = self.run_installer(install=folder)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                record = json.loads((folder / "current.json").read_text(encoding="utf-8-sig"))
                self.assertEqual(record["version"], "1.0.0")
                self.assertEqual((folder / "versions" / "1.0.0" / "app" / "held.txt").read_text(),
                                 "new")
                self.assertFalse((folder / "versions" / "1.0.0.partial").exists())
                self.assertFalse((folder / "versions" / "1.0.0.old").exists())

    def test_links_in_the_install_folder_are_refused(self):
        # A junction where the installer would look or remove: refused before
        # anything is written, the link and what it points to are left as they were.
        target = self.root / "someone"
        target.mkdir()
        (target / "sentinel.txt").write_text("keep")
        self.write_package("1.0.0")

        def in_versions(folder):              # an install, with a junction in versions
            self.write_current(version="0.9.0", previous=None, managed=False)
            self.version_folder(self.versions / "0.9.0")
            self.junction(self.versions / "0.8.0", target)

        def as_versions(folder):              # versions itself is a junction
            folder.mkdir(parents=True)
            self.junction(folder / "versions", target)

        def as_staging(folder):               # the copy's working folder is a junction
            (folder / "versions").mkdir(parents=True)
            self.junction(folder / "versions" / "1.0.0.partial", target)

        def as_record(folder):                # current.json is a link to a folder
            folder.mkdir(parents=True)
            self.junction(folder / "current.json", target)

        for make in (in_versions, as_versions, as_staging, as_record):
            with self.subTest(make.__name__):
                folder = self.install if make is in_versions else self.root / make.__name__
                make(folder)
                before = snapshot(folder)
                r = self.run_installer(install=folder)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(self.LINK, r.stderr)
                self.assertEqual(snapshot(folder), before)
                self.assertEqual(sorted(p.name for p in target.iterdir()), ["sentinel.txt"])

    def test_a_link_inside_an_old_version_is_removed_as_a_link(self):
        # Removing an older version never follows a junction inside it.
        target = self.root / "someone"
        target.mkdir()
        (target / "sentinel.txt").write_text("keep")
        self.write_package("1.0.0")
        self.write_current(version="0.9.0", previous="0.8.0", managed=False)
        for name in ("0.9.0", "0.8.0", "0.7.0"):
            self.version_folder(self.versions / name)
        (self.versions / "0.7.0" / "app").mkdir()
        self.junction(self.versions / "0.7.0" / "app" / "link", target)
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.versions / "0.7.0").exists())
        self.assertEqual((target / "sentinel.txt").read_text(), "keep")

    def test_a_package_version_that_is_not_a_plain_name_is_refused(self):
        # "..\x" would install beside versions; "0.6.0." names the folder 0.6.0,
        # because Windows drops a final dot, so the old retention step then removed
        # the version it had just installed.
        # "1.0.0\n": a regular expression's $ also matches before a final newline
        # (#297 review N1).
        for version in ("..\\x", "0.6.0.", "", "a/b", ".hidden", "1.0.0.partial", "1.0.0\n"):
            with self.subTest(version=version):
                shutil.rmtree(self.install, ignore_errors=True)
                self.write_package(version)
                r = self.run_installer()
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertIn("version", r.stderr)
                self.assertFalse(self.install.exists())
                self.assertFalse((self.root / "x").exists())

    def test_a_previous_value_that_is_not_a_plain_name_is_ignored(self):
        # Reinstalling the same version keeps current.json's previous version; one
        # that is not a plain name is treated as absent.
        self.write_package("1.0.0")
        for bad in ("..\\x", "0.8.0.", "C:\\Elsewhere", "0.8.0\n"):
            with self.subTest(bad=bad):
                shutil.rmtree(self.install, ignore_errors=True)
                self.write_current(version="1.0.0", previous=bad, managed=False)
                self.version_folder(self.versions / "1.0.0")
                r = self.run_installer()
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.current()["version"], "1.0.0")
                self.assertIsNone(self.current()["previous"])

    def test_a_current_json_that_is_not_an_install_record_is_refused(self):
        # #297 review N2: only a current.json that reads as JSON and names a plain
        # version makes the folder an install. Another program's current.json, or
        # one that names no valid version, leaves the folder check as for any other
        # folder, so its versions\1.0.0.partial and the file itself are kept.
        self.write_package("1.0.0")
        records = {"another program's": json.dumps({"name": "Other", "settings": 1}),
                   "not JSON": "someone's notes", "empty": "",
                   "a path": json.dumps({"version": "..\\x"}),
                   "a final dot": json.dumps({"version": "0.8.0."}),
                   "a final newline": json.dumps({"version": "1.0.0\n"}),
                   "a list": json.dumps([{"version": "1.0.0"}]),
                   # #297 round 2 (R2-1): the version must be a JSON string, and a
                   # path, when there is one, must be this folder's versions\<version>.
                   "a number": json.dumps({"version": 100}),
                   "a version list": json.dumps({"version": ["1.0.0"]}),
                   "a path elsewhere": json.dumps({"version": "1.0.0",
                                                   "path": str(self.root / "versions" / "1.0.0")}),
                   "a path that is no string": json.dumps({"version": "1.0.0", "path": 5})}
        for number, (label, text) in enumerate(records.items()):
            with self.subTest(label):
                folder = self.root / f"record-{number}"
                (folder / "versions" / "1.0.0.partial").mkdir(parents=True)
                (folder / "versions" / "1.0.0.partial" / "data.txt").write_text("someone's work")
                (folder / "current.json").write_text(text)
                before = snapshot(folder)
                r = self.run_installer(install=folder)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(self.NOT_OURS, r.stderr)
                self.assertEqual(snapshot(folder), before)

    def test_an_install_the_installer_made_is_still_an_install(self):
        # The record the installer writes (version and path) makes the folder an
        # install, so an update over it carries on (#297 round 2).
        self.write_package("0.9.0")
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(Path(self.current()["path"]), self.versions / "0.9.0")
        self.write_package("1.0.0")
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.current()["version"], self.current()["previous"]),
                         ("1.0.0", "0.9.0"))

    # PowerShell 7's ConvertFrom-Json, unlike 5.1's, sends the items of a top-level
    # list down the pipeline one by one, so a one-item list reads as its object. This
    # stand-in gives Windows PowerShell 5.1 that behaviour.
    PS7_CONVERT = r"""
function ConvertFrom-Json {
    param([Parameter(ValueFromPipeline = $true, Position = 0)]$InputObject)
    process {
        foreach ($item in (Microsoft.PowerShell.Utility\ConvertFrom-Json $InputObject)) { $item }
    }
}
"""

    def record_check(self, folder, convert=""):
        """A script that runs install.ps1's own Test-InstallRecord on each
        folder/case-*.json and prints case-NN=1 for an install's record, 0 otherwise."""
        text = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
        start = text.index("$VersionPattern = '")
        end = text.index("\n}\n", text.index("function Test-InstallRecord")) + 3
        script = folder / "record-check.ps1"
        script.write_text('$ErrorActionPreference = "Stop"\n' + convert + text[start:end] + """
foreach ($file in Get-ChildItem -LiteralPath $args[0] -Filter 'case-*.json' | Sort-Object Name) {
    Write-Output ("{0}={1}" -f $file.BaseName, [int](Test-InstallRecord $file))
}
""", encoding="utf-8")
        return script

    def test_the_install_record_is_an_object_with_a_string_version_and_its_own_path(self):
        # #297 round 2 (R2-1): only a JSON object whose version is a JSON string with
        # a plain name, and whose path, if there is one, is <InstallDir>\versions\
        # <version> (letter case aside), is an install's record. On PowerShell 7 a
        # one-item list must not pass for its object.
        folder = self.root / "records"
        folder.mkdir()
        own = str(folder / "versions" / "1.0.0")
        cases = {
            "an object": ({"version": "1.0.0"}, 1),
            "with its path": ({"version": "1.0.0", "path": own, "previous": None}, 1),
            "with its path in other letter case": ({"version": "1.0.0", "path": own.upper()}, 1),
            "a list": ([{"version": "1.0.0"}], 0),
            "a list of its own record": ([{"version": "1.0.0", "path": own}], 0),
            "a string": ("1.0.0", 0),
            "an empty object": ({}, 0),
            "a number": ({"version": 100}, 0),
            "a version list": ({"version": ["1.0.0"]}, 0),
            "a version object": ({"version": {"name": "1.0.0"}}, 0),
            "a path elsewhere": ({"version": "1.0.0",
                                  "path": str(self.root / "versions" / "1.0.0")}, 0),
            "a path to another version": ({"version": "1.0.0",
                                           "path": str(folder / "versions" / "0.9.0")}, 0),
            "a path inside the version": ({"version": "1.0.0", "path": own + "\\app"}, 0),
            "a path that is no string": ({"version": "1.0.0", "path": 5}, 0),
            "a null path": ({"version": "1.0.0", "path": None}, 0),
        }
        expected = []
        for number, (record, verdict) in enumerate(cases.values()):
            (folder / f"case-{number:02}.json").write_text(json.dumps(record))
            expected.append(f"case-{number:02}={verdict}")
        # Whitespace before a list still leaves a list.
        (folder / "case-99.json").write_text(" \r\n\t" + json.dumps([{"version": "1.0.0"}]))
        expected.append("case-99=0")
        shells = [("Windows PowerShell 5.1", windows_powershell.executable(), ""),
                  ("5.1 with PowerShell 7's ConvertFrom-Json", windows_powershell.executable(),
                   self.PS7_CONVERT)]
        pwsh = shutil.which("pwsh")
        if os.environ.get("GITHUB_ACTIONS"):          # CI's Windows runner has PowerShell 7
            self.assertIsNotNone(pwsh, "PowerShell 7 (pwsh) is missing from the CI runner")
        if pwsh:
            shells.append(("PowerShell 7", pwsh, ""))
        for label, exe, convert in shells:
            with self.subTest(label):
                script = self.record_check(folder, convert)
                env = windows_powershell.environment()
                if convert == "" and exe == pwsh:
                    env.pop("PSModulePath")       # PowerShell 7 finds its own modules
                r = subprocess.run([exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                    str(script), str(folder)], capture_output=True, text=True,
                                   timeout=120, env=env, cwd=self.root)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(r.stdout.split(), expected, r.stderr)

    def test_rollback_refuses_a_previous_version_with_a_final_newline(self):
        # #297 review N1: "0.8.0\n" is not a plain name, so there is no previous version.
        self.write_package("1.0.0")
        self.write_current(version="0.9.0", previous="0.8.0\n", managed=False)
        for name in ("0.9.0", "0.8.0"):
            self.version_folder(self.versions / name)
        before = (self.install / "current.json").read_text()
        r = self.run_installer("-Rollback")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("There is no previous version to go back to.", r.stderr)
        self.assertNotIn("GATE RAN", r.stdout)
        self.assertEqual((self.install / "current.json").read_text(), before)

    def test_rollback_refuses_a_previous_version_that_is_not_a_plain_name(self):
        # -Rollback never looks outside versions or runs anything from there. Each
        # case has a folder that would run where the value leads.
        self.write_package("1.0.0")
        escape = self.install / "x"
        cases = (("previous", "..\\x"), ("previous", "0.8.0."), ("version", "..\\x"),
                 ("previous", "0.8.0-link"))
        for field, bad in cases:
            with self.subTest(field=field, bad=bad):
                shutil.rmtree(self.install, ignore_errors=True)
                record = {"version": "0.9.0", "previous": "0.8.0", "managed": False}
                record[field] = bad
                self.write_current(**record)
                for name in ("0.9.0", "0.8.0"):
                    self.version_folder(self.versions / name)
                self.version_folder(escape, "x")
                if bad == "0.8.0-link":
                    self.junction(self.versions / bad, self.versions / "0.8.0")
                before = (self.install / "current.json").read_text()
                r = self.run_installer("-Rollback")
                self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertNotIn("GATE RAN", r.stdout)
                self.assertEqual((self.install / "current.json").read_text(), before)

    def test_rollback_still_goes_back_to_a_plain_previous_version(self):
        self.write_package("1.0.0")
        self.write_current(version="0.9.0", previous="0.8.0", managed=True)
        for name in ("0.9.0", "0.8.0"):
            self.version_folder(self.versions / name)
        r = self.run_installer("-Rollback")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("GATE RAN", r.stdout)
        self.assertEqual((self.current()["version"], self.current()["previous"]),
                         ("0.8.0", "0.9.0"))

    # -- current.json format 2 (#111 B5a) ------------------------------------------

    def slots(self):
        """current.json's slots without the install times."""
        return {name: {k: v for k, v in entry.items() if k != "installed"}
                for name, entry in self.current()["slots"].items()}

    def test_an_install_records_each_versions_format_and_install_order(self):
        from workspace import update
        self.write_package("1.0.0", schema=2)
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        record = self.current()
        self.assertEqual((record["format"], record["version"], record["previous"]),
                         (2, "1.0.0", None))
        self.assertEqual(self.slots(), {"1.0.0": {"schema": 2, "seq": 1}})
        self.assertRegex(record["slots"]["1.0.0"]["installed"],
                         r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        state, read = update.read_slots(self.install)      # what the launcher reads
        self.assertEqual((state, read["slots"]), ("ok", {"1.0.0": {"schema": 2, "seq": 1}}))

    def test_an_update_from_format_1_numbers_the_kept_versions_oldest_first(self):
        # A version from before B5 states no format: recorded without one.
        self.write_package("1.0.0", schema=2)
        self.write_current(version="0.9.0", previous="0.8.0", managed=True)
        for name in ("0.9.0", "0.8.0"):
            self.version_folder(self.versions / name)
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.current()["version"], self.current()["previous"]),
                         ("1.0.0", "0.9.0"))
        self.assertEqual(self.slots(), {"0.9.0": {"seq": 1}, "1.0.0": {"schema": 2, "seq": 2}})
        self.assertTrue(self.current()["managed"])

    def format2(self, current, previous, slots, managed=False):
        self.write_current(format=2, version=current, previous=previous, managed=managed,
                           path=str(self.versions / current), slots=slots)

    def test_an_update_gives_the_new_version_the_next_number_and_drops_removed_ones(self):
        self.write_package("1.1.0", schema=3)
        self.format2("1.0.0", "0.9.0", {
            "1.0.0": {"schema": 2, "seq": 7, "installed": "2027-01-02T00:00:00Z"},
            "0.9.0": {"schema": 2, "seq": 5, "installed": "2027-01-01T00:00:00Z"}})
        for name in ("1.0.0", "0.9.0"):
            self.version_folder(self.versions / name, schema=2)
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.slots(), {"1.0.0": {"schema": 2, "seq": 7},
                                        "1.1.0": {"schema": 3, "seq": 8}})
        self.assertEqual(self.current()["slots"]["1.0.0"]["installed"], "2027-01-02T00:00:00Z")
        self.assertEqual(sorted(p.name for p in self.versions.iterdir()), ["1.0.0", "1.1.0"])

    def test_a_reinstall_keeps_the_number_and_records_a_new_install_time(self):
        self.write_package("1.0.0", schema=2)
        self.format2("1.0.0", "0.9.0", {
            "1.0.0": {"schema": 2, "seq": 7, "installed": "2027-01-02T00:00:00Z"},
            "0.9.0": {"schema": 2, "seq": 5, "installed": "2027-01-01T00:00:00Z"}})
        for name in ("1.0.0", "0.9.0"):
            self.version_folder(self.versions / name, schema=2)
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.current()["previous"], "0.9.0")
        self.assertEqual(self.slots(), {"0.9.0": {"schema": 2, "seq": 5},
                                        "1.0.0": {"schema": 2, "seq": 7}})
        installed = {k: v["installed"] for k, v in self.current()["slots"].items()}
        self.assertEqual(installed["0.9.0"], "2027-01-01T00:00:00Z")
        self.assertNotEqual(installed["1.0.0"], "2027-01-02T00:00:00Z")

    def test_rollback_writes_format_2_and_keeps_each_versions_number(self):
        self.write_package("1.0.0")
        self.format2("1.0.0", "0.9.0", {
            "1.0.0": {"schema": 2, "seq": 7, "installed": "2027-01-02T00:00:00Z"},
            "0.9.0": {"schema": 2, "seq": 5, "installed": "2027-01-01T00:00:00Z"}},
                     managed=True)
        for name in ("1.0.0", "0.9.0"):
            self.version_folder(self.versions / name, schema=2)
        r = self.run_installer("-Rollback")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        record = self.current()
        self.assertEqual((record["format"], record["version"], record["previous"],
                          record["managed"]), (2, "0.9.0", "1.0.0", True))
        self.assertEqual(record["slots"], {
            "0.9.0": {"schema": 2, "seq": 5, "installed": "2027-01-01T00:00:00Z"},
            "1.0.0": {"schema": 2, "seq": 7, "installed": "2027-01-02T00:00:00Z"}})
        self.assertEqual(record["path"].lower(), str(self.versions / "0.9.0").lower())

    def test_rollback_from_a_format_1_record_writes_format_2(self):
        self.write_package("1.0.0")
        self.write_current(version="0.9.0", previous="0.8.0", managed=False)
        for name in ("0.9.0", "0.8.0"):
            self.version_folder(self.versions / name)
        r = self.run_installer("-Rollback")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.current()["format"], self.current()["version"]), (2, "0.8.0"))
        self.assertEqual(self.slots(), {"0.8.0": {"seq": 1}, "0.9.0": {"seq": 2}})

    def test_both_scripts_use_the_same_version_name_pattern(self):
        import re

        def pattern(name, where):
            text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
            found = re.findall(where, text)
            self.assertEqual(len(found), 1, name)
            return found[0]

        found = pattern("install.ps1", r"(?m)^\$VersionPattern = '([^']+)'$")
        self.assertEqual(found, pattern("uninstall.ps1", r"-cnotmatch '([^']+)'"))
        # As .NET reads it (#297 review N1): a final newline is not part of a name.
        check = (f"$p = '{found}'; foreach ($n in '1.0.0', \"1.0.0`n\", \"1.0.0`r`n\") "
                 "{ Write-Output ([int]($n -cmatch $p)) }")
        r = subprocess.run([windows_powershell.executable(), "-NoProfile", "-Command", check],
                           capture_output=True, text=True, timeout=60,
                           env=windows_powershell.environment())
        self.assertEqual(r.stdout.split(), ["1", "0", "0"], r.stdout + r.stderr)


STUB_APP = '''\
# A stand-in for the app: records how the uninstaller started it, then acts as told.
import json, os, sys, time
from pathlib import Path
install = Path(__file__).resolve().parents[4]
record = Path(os.environ["LOCALAPPDATA"], "Unminute", "workspace", "power_restore.json")
Path(os.environ["UNMINUTE_TEST_LOG"]).write_text(json.dumps({
    "argv": sys.argv[1:], "cwd": os.getcwd(), "isolated": sys.flags.isolated,
    "no_bytecode": sys.dont_write_bytecode, "x": sorted(sys._xoptions),
    "installed": (install / "current.json").is_file()
    and (install / "versions" / "9.9.0" / "runtime" / "python.exe").is_file(),
    "record": record.is_file()}))
mode = os.environ.get("UNMINUTE_TEST_MODE", "0")
if mode == "hang":
    time.sleep(600)
if mode == "crash":
    raise RuntimeError("synthetic failure")
if mode == "noise":
    print("x" * 200000)
    sys.stderr.write("y" * 200000)
sys.exit(int(mode) if mode.isdigit() else 0)
'''


@unittest.skipUnless(sys.platform == "win32", "the uninstaller is Windows PowerShell")
class UninstallLidRestoreTests(unittest.TestCase):
    """#191: before removing anything, the uninstaller runs the installed app
    once with --restore-lid, with a hard time limit, and carries on whatever
    happens. Throwaway installs in temporary folders, with a temporary
    LOCALAPPDATA, Start menu folder and uninstall key (DataFolderOverlapTests).
    The runtime is a copy of this Python's interpreter whose ._pth file gives
    it the standard library and the install's app folder, as the package's
    runtime has its own. A stand-in app records how it was started; the real
    app's switch is run only with records that never reach the power scheme."""

    PROGRAMS, KEY, TEST_KEY = (DataFolderOverlapTests.PROGRAMS, DataFolderOverlapTests.KEY,
                               DataFolderOverlapTests.TEST_KEY)
    uninstaller_copy = DataFolderOverlapTests.uninstaller_copy
    WARNING = "Warning: Unminute could not put back what closing the lid does."
    TIMEOUT = "$LidRestoreSeconds = 60"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.local = self.root / "Local"
        self.record = self.local / "Unminute" / "workspace" / "power_restore.json"
        self.record.parent.mkdir(parents=True)
        self.log = self.root / "started.json"
        self.copy, _ = self.uninstaller_copy(self.tmp.name)

    def install(self, name, app=None, features=("gate", "restore-lid"), version="9.9.0"):
        """A throwaway install: current.json, version.json, the runtime and
        the stand-in app (or app, a folder for the runtime's import path)."""
        install = self.root / name
        target = install / "versions" / "9.9.0"
        (target / "app" / "workspace").mkdir(parents=True)
        (target / "app" / "workspace" / "__init__.py").write_text("")
        (target / "app" / "workspace" / "__main__.py").write_text(STUB_APP)
        (target / "version.json").write_text(json.dumps({"version": "9.9.0",
                                                          "features": list(features)}))
        (install / "current.json").write_text(json.dumps({"version": version,
                                                           "path": str(target)}))
        shutil.copy(self.copy, install / "uninstall.ps1")
        base = Path(sys.base_prefix)
        runtime = target / "runtime"
        runtime.mkdir()
        shutil.copy(base / "python.exe", runtime)
        for dll in list(base.glob("python3*.dll")) + list(base.glob("vcruntime*.dll")):
            shutil.copy(dll, runtime)
        versioned = [d for d in base.glob("python3*.dll") if d.stem != "python3"]
        self.assertEqual(len(versioned), 1, versioned)
        (runtime / f"{versioned[0].stem}._pth").write_text("\n".join(
            [str(base / "Lib"), str(base / "DLLs"), str(app or target / "app")]) + "\n")
        return install

    def uninstall(self, install, mode="0", script=None):
        env = dict(os.environ, LOCALAPPDATA=str(self.local), UNMINUTE_TEST_LOG=str(self.log),
                   UNMINUTE_TEST_MODE=mode)
        call = f"& '{script or self.copy}' -InstallDir '{install}' -Silent"
        return subprocess.run([windows_powershell.executable(), "-NoProfile",
                               "-ExecutionPolicy", "Bypass",
                               "-Command", f"{call}; exit $LASTEXITCODE"],
                              capture_output=True, text=True, timeout=180, cwd=self.tmp.name,
                              env=env)

    def started(self):
        return json.loads(self.log.read_text()) if self.log.exists() else None

    def test_the_lid_is_put_back_before_anything_is_removed(self):
        self.record.write_text("{}")                     # its content is the app's business
        install = self.install("Apps")
        r = self.uninstall(install)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Warning", r.stderr)
        started = self.started()
        self.assertEqual(started["argv"], ["--restore-lid"])
        self.assertEqual((started["isolated"], started["no_bytecode"]), (1, True))
        self.assertIn("disable-remote-debug", started["x"])
        self.assertEqual(os.path.normcase(started["cwd"]),
                         os.path.normcase(str(install / "versions" / "9.9.0" / "app")))
        self.assertTrue(started["installed"] and started["record"])   # before removal
        self.assertFalse(install.exists())
        self.assertTrue(self.record.exists())            # the uninstaller never touches it

    def test_nothing_runs_without_a_record(self):
        install = self.install("Apps")
        r = self.uninstall(install)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(self.started())
        self.assertEqual(r.stderr, "")
        self.assertFalse(install.exists())

    def test_any_other_result_still_uninstalls_and_says_to_check(self):
        self.record.write_text("{}")
        for mode in ("1", "2", "3", "crash"):
            with self.subTest(mode=mode):
                self.log.unlink(missing_ok=True)
                install = self.install(f"Apps{mode}")
                r = self.uninstall(install, mode)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIsNotNone(self.started())
                self.assertIn(self.WARNING, r.stderr)
                self.assertNotIn("synthetic", r.stdout + r.stderr)   # its output is not shown
                self.assertFalse(install.exists())

    def test_a_noisy_restore_cannot_block_it(self):
        # More output than a pipe holds: read, never shown, and no hang.
        self.record.write_text("{}")
        install = self.install("Apps")
        r = self.uninstall(install, "noise")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("xxxx", r.stdout + r.stderr)
        self.assertNotIn("Warning", r.stderr)
        self.assertFalse(install.exists())

    def test_a_hung_restore_is_stopped_and_the_uninstall_carries_on(self):
        import time
        self.record.write_text("{}")
        text = self.copy.read_text(encoding="utf-8")
        self.assertEqual(text.count(self.TIMEOUT), 1)
        quick = self.root / "uninstall-quick.ps1"
        quick.write_text(text.replace(self.TIMEOUT, "$LidRestoreSeconds = 3"), encoding="utf-8")
        install = self.install("Apps")
        began = time.monotonic()
        r = self.uninstall(install, "hang", script=quick)
        self.assertLess(time.monotonic() - began, 120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNotNone(self.started())
        self.assertIn(self.WARNING, r.stderr)
        self.assertFalse(install.exists())               # stopped, so nothing stayed open

    def test_a_version_that_cannot_restore_is_never_started(self):
        # Without the feature, an older app would take the switch for an ordinary
        # start and open its window; a version that is not a plain folder name is
        # never followed, even where it leads to a version that would run. Either
        # way the uninstall carries on and says to check.
        self.record.write_text("{}")
        for name, kwargs in (("old", {"features": ("gate",)}),
                             ("path", {"version": "..\\..\\Elsewhere"}),
                             ("absolute", {"version": str(self.root / "Elsewhere")})):
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                install = self.install(f"Apps-{name}", **kwargs)
                # A version folder that would run, where the path leads.
                shutil.rmtree(self.root / "Elsewhere", ignore_errors=True)
                shutil.copytree(install / "versions" / "9.9.0", self.root / "Elsewhere")
                r = self.uninstall(install)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIsNone(self.started())
                self.assertIn(self.WARNING, r.stderr)
                self.assertFalse(install.exists())

    def test_the_real_switch_runs_from_the_installed_layout(self):
        # The app's own code (this checkout) through the same command line, with a
        # record that cannot say what to put back: it is removed, the power scheme
        # is never read or written, and the uninstall says to check the setting.
        # If an installed copy of the app is open on this computer, the switch
        # stands aside instead and the record stays.
        self.record.write_text('{"scheme": "not-a-guid", "ac": 1, "dc": 1}', encoding="utf-8")
        install = self.install("Apps", app=ROOT)
        open_ = app_is_open()
        r = self.uninstall(install)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(install.exists())
        self.assertIn(self.WARNING, r.stderr)
        self.assertEqual(self.record.exists(), open_)


def app_is_open():
    """Whether an installed copy of the app holds its mutex in this session."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    handle = kernel32.OpenMutexW(0x00100000, False, "Local\\Unminute.Workspace")
    error = ctypes.get_last_error()
    if handle:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    return bool(handle) or error == 5


class RestoreLidFeatureTests(unittest.TestCase):
    def test_the_package_declares_the_switch_the_uninstaller_looks_for(self):
        build = (ROOT / "tools" / "build_package.py").read_text(encoding="utf-8")
        uninstall = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertIn('"features": ["gate", "restore-lid", "launch-gate"]', build)
        self.assertIn('-notcontains "restore-lid"', uninstall)
        main = (ROOT / "workspace" / "__main__.py").read_text(encoding="utf-8")
        self.assertIn('if "--restore-lid" in sys.argv:', main)
        # Before anything is removed: the restore comes after the last refusal and
        # before the first removal.
        body = uninstall[uninstall.index("try {\n    # An empty -InstallDir"):]
        self.assertLess(body.index("did not put there"), body.index("    Restore-LidSetting\n"))
        self.assertLess(body.index("    Restore-LidSetting\n"), body.index("Remove-Item"))


class RemoteDebugTests(unittest.TestCase):
    """#111 B4: Python 3.14 lets another process run code in a running Python
    process (PEP 768). Every app process starts with -X disable-remote-debug."""

    def test_the_app_and_its_processes_start_with_remote_debugging_off(self):
        install = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
        self.assertIn('$LaunchArgs = "-I -B -X disable-remote-debug -m workspace"', install)
        self.assertEqual(install.count("Arguments = $LaunchArgs"), 2)   # new and pinned
        self.assertNotIn('Arguments = "-m workspace"', install)
        self.assertIn('@("-I", "-B", "-X", "disable-remote-debug", "-m", "workspace", "--gate")',
                      install)
        uninstall = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertIn('Arguments = "-I -B -X disable-remote-debug -m workspace --restore-lid"',
                      uninstall)
        media = (ROOT / "workspace" / "media.py").read_text(encoding="utf-8")
        self.assertIn('"-I", "-B", "-X", "disable-remote-debug", "-c"', media)

    def test_the_option_turns_it_off_and_workers_inherit_it(self):
        # What multiprocessing passes to the workers it starts, from a parent
        # started as the shortcut starts the app.
        # (Python 3.13 and earlier have no such interface, and so nothing to pass on.)
        code = ("import subprocess, sys; "
                "print(hasattr(sys, 'is_remote_debug_enabled')); "
                "print(getattr(sys, 'is_remote_debug_enabled', lambda: False)()); "
                "print(subprocess._args_from_interpreter_flags())")
        r = subprocess.run([sys.executable, "-X", "disable-remote-debug", "-c", code],
                           capture_output=True, text=True, timeout=60)
        has, enabled, flags = r.stdout.splitlines()
        self.assertEqual(enabled, "False", r.stderr)
        if has == "True":
            self.assertIn("'-X', 'disable-remote-debug'", flags)


class NvidiaSmiTests(unittest.TestCase):
    def test_never_found_through_path_or_the_current_folder(self):
        from workspace import gpu
        with tempfile.TemporaryDirectory() as tmp:
            planted = Path(tmp) / "nvidia-smi.exe"
            planted.write_text("x")
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with mock.patch.dict(os.environ, {"PATH": tmp}):
                    found = gpu._nvidia_smi()
            finally:
                os.chdir(cwd)
        self.assertNotEqual(found and os.path.normcase(os.path.dirname(found)),
                            os.path.normcase(tmp))


if __name__ == "__main__":
    unittest.main()
