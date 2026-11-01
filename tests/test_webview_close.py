# tests/test_webview_close.py
# #509: once a close of the window is going ahead, the app ends WebView2's
# browser process itself, so the runtime's own shutdown (which can load
# Microsoft's account library, ask the Web Account Manager for the user's
# accounts and tokens, and start WinINet) never runs. Only that one process is
# ended, only for a close nothing cancelled, and the private-mode profile is
# still removed.

import ast
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workspace import shell  # noqa: E402


class FakeApi:
    """kernel32 as terminate_browser() sees it: one process, by name."""

    def __init__(self, name="C:\\Program Files (x86)\\Microsoft\\EdgeWebView\\Application\\1.0\\"
                            "msedgewebview2.exe", opens=True, terminates=True):
        self.name, self.opens, self.terminates = name, opens, terminates
        self.calls = []

    def open(self, pid):
        self.calls.append(("open", pid))
        return 77 if self.opens else None

    def image_name(self, handle):
        self.calls.append(("name", handle))
        return self.name

    def terminate(self, handle):
        self.calls.append(("terminate", handle))
        return self.terminates

    def wait(self, handle, ms):
        self.calls.append(("wait", handle, ms))

    def close(self, handle):
        self.calls.append(("close", handle))


class TerminateBrowserTests(unittest.TestCase):
    def test_ends_the_webview2_browser_and_waits_for_it(self):
        api = FakeApi()
        self.assertTrue(shell.terminate_browser(4242, api))
        self.assertEqual(api.calls, [("open", 4242), ("name", 77), ("terminate", 77),
                                     ("wait", 77, shell.BROWSER_EXIT_WAIT_MS), ("close", 77)])

    def test_never_ends_another_program(self):
        # A process id that is no longer WebView2's browser (it ended and the
        # id was reused) is left alone; the handle is still closed.
        for name in ("C:\\Windows\\System32\\notepad.exe", "C:\\x\\msedgewebview2.exe.evil",
                     "C:\\x\\msedge.exe", None):
            api = FakeApi(name=name)
            self.assertFalse(shell.terminate_browser(4242, api))
            self.assertNotIn("terminate", [c[0] for c in api.calls])
            self.assertEqual(api.calls[-1], ("close", 77))

    def test_name_is_compared_without_case(self):
        self.assertTrue(shell.terminate_browser(1, FakeApi(name="C:\\X\\MSEdgeWebView2.EXE")))

    def test_a_process_that_cannot_be_opened_or_ended(self):
        api = FakeApi(opens=False)
        self.assertFalse(shell.terminate_browser(4242, api))
        self.assertEqual(api.calls, [("open", 4242)])
        api = FakeApi(terminates=False)
        self.assertFalse(shell.terminate_browser(4242, api))
        self.assertNotIn("wait", [c[0] for c in api.calls])
        self.assertEqual(api.calls[-1], ("close", 77))


class Browser:
    """pywebview's EdgeChrome as end_browser() sees it."""

    def __init__(self, pid=4242, core=True):
        core_obj = types.SimpleNamespace(BrowserProcessId=pid) if core else None
        self.webview = types.SimpleNamespace(CoreWebView2=core_obj)


class EndBrowserTests(unittest.TestCase):
    def test_ends_the_controls_own_browser_and_marks_the_view(self):
        browser = Browser(pid=31337)
        with mock.patch.object(shell, "terminate_browser", return_value=True) as end:
            self.assertTrue(shell.end_browser(browser))
        end.assert_called_once_with(31337)
        self.assertTrue(browser.unminute_browser_ended)

    def test_not_marked_when_nothing_was_ended(self):
        browser = Browser()
        with mock.patch.object(shell, "terminate_browser", return_value=False):
            self.assertFalse(shell.end_browser(browser))
        self.assertFalse(getattr(browser, "unminute_browser_ended", False))
        with mock.patch.object(shell, "terminate_browser") as end:
            self.assertFalse(shell.end_browser(Browser(core=False)))    # never initialised
        end.assert_not_called()


def fake_forms():
    """System.Windows.Forms with just FormClosingEventHandler (pythonnet)."""
    system = types.ModuleType("System")
    windows = types.ModuleType("System.Windows")
    forms = types.ModuleType("System.Windows.Forms")
    forms.FormClosingEventHandler = lambda fn: fn
    system.Windows, windows.Forms = windows, forms
    return {"System": system, "System.Windows": windows, "System.Windows.Forms": forms}


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class EdgeChrome:
    """pywebview's view class, with its private-mode clean-up."""
    cleared = []

    def clear_user_data(self):
        EdgeChrome.cleared.append(self)


class EndOnCloseTests(unittest.TestCase):
    def setUp(self):
        EdgeChrome.cleared = []
        original = EdgeChrome.__dict__["clear_user_data"]
        self.addCleanup(setattr, EdgeChrome, "clear_user_data", original)
        backend = types.ModuleType("webview.platforms.edgechromium")
        backend.EdgeChrome = EdgeChrome
        modules = {**fake_forms(), "webview.platforms.edgechromium": backend}
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.form = types.SimpleNamespace(FormClosing=Event(), browser=Browser())
        self.window = types.SimpleNamespace(native=self.form)

    def close(self, cancel):
        args = types.SimpleNamespace(Cancel=cancel)
        with mock.patch.object(shell, "end_browser") as end:
            for handler in self.form.FormClosing.handlers:
                handler(self.form, args)
        return end

    def test_a_close_going_ahead_ends_the_browser(self):
        shell.end_browser_on_close(self.window)
        end = self.close(cancel=False)
        end.assert_called_once_with(self.form.browser)

    def test_a_cancelled_close_leaves_the_browser_running(self):
        # A recording is open and the page asks first: the window stays.
        shell.end_browser_on_close(self.window)
        self.close(cancel=True).assert_not_called()

    def test_hooked_once(self):
        shell.end_browser_on_close(self.window)
        shell.end_browser_on_close(self.window)
        self.assertEqual(len(self.form.FormClosing.handlers), 1)

    def test_a_failure_never_stops_the_close(self):
        shell.end_browser_on_close(self.window)
        args = types.SimpleNamespace(Cancel=False)
        with mock.patch.object(shell, "end_browser", side_effect=OSError("access denied")):
            self.form.FormClosing.handlers[0](self.form, args)
        self.assertFalse(args.Cancel)

    def test_profile_still_removed_after_the_browser_was_ended(self):
        shell.end_browser_on_close(self.window)
        shell.end_browser_on_close(types.SimpleNamespace(native=self.form))   # installed once
        self.assertTrue(EdgeChrome.clear_user_data.unminute_after_end)
        state = types.ModuleType("webview")
        state._state = {"private_mode": True}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(sys.modules, {"webview": state}):
            folder = Path(tmp) / "profile"
            (folder / "EBWebView").mkdir(parents=True)
            (folder / "EBWebView" / "Local State").write_text("{}", encoding="utf-8")
            view = EdgeChrome()
            disposed = []
            view.webview = types.SimpleNamespace(Dispose=lambda: disposed.append(True))
            view.user_data_folder = str(folder)
            view.unminute_browser_ended = True
            view.clear_user_data()
            self.assertEqual(disposed, [True])
            self.assertFalse(folder.exists())
            self.assertEqual(EdgeChrome.cleared, [])         # pywebview's own path not taken
            other = EdgeChrome()                             # browser not ended: unchanged
            other.clear_user_data()
            self.assertEqual(EdgeChrome.cleared, [other])

    def test_a_profile_outside_private_mode_is_kept(self):
        shell.end_browser_on_close(self.window)
        state = types.ModuleType("webview")
        state._state = {"private_mode": False}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(sys.modules, {"webview": state}):
            view = EdgeChrome()
            view.webview = types.SimpleNamespace(Dispose=lambda: None)
            view.user_data_folder = tmp
            view.unminute_browser_ended = True
            view.clear_user_data()
            self.assertTrue(Path(tmp).exists())


class StartWiringTests(unittest.TestCase):
    """The real start hooks it once the window is shown, after the handler
    that lets a Windows shutdown through, so it sees that handler's answer."""

    def test_hooked_last_when_the_window_is_shown(self):
        tree = ast.parse((ROOT / "workspace" / "__main__.py").read_text(encoding="utf-8"))
        shown = [node.value.id for node in ast.walk(tree)
                 if isinstance(node, ast.AugAssign) and isinstance(node.value, ast.Name)
                 and ast.unparse(node.target) == "window.events.shown"]
        self.assertIn("end_browser_on_close", shown)
        self.assertLess(shown.index("never_block_shutdown"), shown.index("end_browser_on_close"))
        source = ast.unparse(tree)
        self.assertIn("shell.end_browser_on_close(window)", source)


if __name__ == "__main__":
    unittest.main()
