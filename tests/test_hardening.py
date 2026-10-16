# Security hardening checks (#111 slice A2): the window shell's dispatch guard
# and navigation rule, settings validation, test-only engines, the workspace
# location, SQLite temporary storage, the earlier app's token and nvidia-smi.
#   python -m unittest discover -s tests -v

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
        for other in (shell.ui_url("trial.html"), shell.ui_url("app.js"),
                      shell.ui_url("img/logo.svg"), page + "%23/evil.html",
                      page + "%3F/../trial.html"):
            self.assertFalse(shell._allowed(other), other)
        self.assertTrue(shell._allowed(shell.ui_url("trial.html"), "trial.html"))
        self.assertFalse(shell._allowed(page, "trial.html"))


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


class EarlierTokenTests(unittest.TestCase):
    def tearDown(self):
        credentials._earlier["token"] = None

    def test_token_leaves_the_environment_and_stays_in_memory(self):
        with mock.patch.dict(os.environ, {"HF_TOKEN": " hf_example "}):
            credentials.adopt_earlier_token()
            self.assertNotIn("HF_TOKEN", os.environ)
        self.assertEqual(credentials.earlier_token(), "hf_example")

    def test_no_token_means_none(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            credentials.adopt_earlier_token()
        self.assertIsNone(credentials.earlier_token())


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
