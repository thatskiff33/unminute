# Security hardening checks (#111 slice A2): the window shell's dispatch guard
# and navigation rule, settings validation, test-only engines, the workspace
# location, SQLite temporary storage, a token left in the environment,
# nvidia-smi, and remote debugging (B4).
#   python -m unittest discover -s tests -v

import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workspace import credentials, shell  # noqa: E402

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
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            precious = Path(tmp) / "Documents"
            (precious / "inner").mkdir(parents=True)
            (precious / "client.docx").write_text("keep me")
            ws = Path(tmp) / "ws"
            ws.mkdir()
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(ws / "webview"),
                                str(precious)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            shell.storage_folder(ws)                       # "webview" itself is a junction
            self.assertTrue((precious / "client.docx").exists())
            self.assertTrue((precious / "inner").is_dir())
            self.assertFalse(os.path.isjunction(ws / "webview"))
            inner = ws / "webview" / "old-profile"
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(inner), str(precious)],
                               capture_output=True, text=True)
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
        import subprocess
        from workspace.store import StoreError
        with tempfile.TemporaryDirectory() as tmp:
            synced = Path(tmp) / "OneDrive - Firm"
            (synced / "AppData").mkdir(parents=True)
            link = Path(tmp) / "Local"
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(synced / "AppData")],
                               capture_output=True, text=True)
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


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class StoreTempTests(unittest.TestCase):
    def test_temporary_data_stays_in_memory(self):
        from workspace.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            s = Store(Path(tmp) / "workspace.db")
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
        from asr_engines import ModelLoadError
        from workspace import engines
        with self.assertRaises(ModelLoadError):
            engines.load_engine("test:os:getcwd", self.tmp.name, 1)
        with self.assertRaises(ModelLoadError):
            engines._test_factory("test:os:_exit", False)

    def test_workspace_refuses_a_test_speaker_model_without_the_flag(self):
        from workspace.app import Workspace
        with self.assertRaises(ValueError):
            Workspace(Path(self.tmp.name) / "ws", Path(self.tmp.name) / "m", start=False,
                      speaker_model="test:fake_worker_engines:fake_diarizer")


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

    def run_script(self, script, install_dir, local, extra="", location=None, cwd=None):
        call = f"& '{script}' -InstallDir '{install_dir}' -Silent {extra}"
        if location:
            # A relative -InstallDir is read from PowerShell's location, which
            # Set-Location moves away from the process's own folder.
            call = f"Set-Location -LiteralPath '{location}'; {call}"
        return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
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
            r = self.run_script(install, r"\\localhost\C$\Unminute", local, absent, cwd=tmp)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("own drive", r.stderr)
            # The default shape, and a sibling of the data folder: allowed, so the
            # run stops later, at a prerequisite or the missing package.
            for where in (local / "Programs" / "Unminute", local / "Unminute2"):
                with self.subTest(where=where.name):
                    r = self.run_script(install, where, local, absent, cwd=tmp)
                    self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertNotIn(self.MESSAGE, r.stdout + r.stderr)
            self.assertEqual(list(local.iterdir()), [])          # nothing was written

    def uninstaller_copy(self, tmp):
        """uninstall.ps1 with this computer's Start menu folder and uninstall key
        swapped for a temporary folder and a key that does not exist, so a
        mistake removes nothing real."""
        text = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertEqual((text.count(self.PROGRAMS), text.count(self.KEY)), (1, 1))
        programs = Path(tmp) / "programs"
        programs.mkdir()
        text = text.replace(self.PROGRAMS, f'"{programs}"').replace(
            self.KEY, f'$RegKey = "HKCU:\\Software\\Unminute-UnitTest-absent-{os.getpid()}"')
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

    def test_both_scripts_have_the_same_overlap_check(self):
        # The installer's copy is the one run for every case above.
        def check(name):
            text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
            start = text.index("# The data (workspace and models) is in")
            return text[start:text.index("\n}\n", start) + 3]
        self.assertEqual(check("install.ps1"), check("uninstall.ps1"))
        self.assertIn("function Get-DataOverlap", check("uninstall.ps1"))


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
