# tests/test_webview_close.py
# #509: once a close of the window is going ahead, the app ends WebView2's
# browser process itself, so the runtime's own shutdown (which can load
# Microsoft's account library, ask the Web Account Manager for the user's
# accounts and tokens, and start WinINet) never runs. Only that one process is
# ended, only for a close nothing cancelled, and the private-mode profile is
# still removed.

import ast
import importlib.util
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


# System.Windows.Forms.CloseReason, with its .NET values.
CLOSE_REASON = types.SimpleNamespace(WindowsShutDown=1, UserClosing=3, TaskManagerClosing=4)


def fake_forms():
    """System.Windows.Forms with just FormClosingEventHandler and CloseReason
    (pythonnet)."""
    system = types.ModuleType("System")
    windows = types.ModuleType("System.Windows")
    forms = types.ModuleType("System.Windows.Forms")
    forms.FormClosingEventHandler = lambda fn: fn
    forms.CloseReason = CLOSE_REASON
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

    def close(self, cancel, reason=CLOSE_REASON.UserClosing):
        args = types.SimpleNamespace(Cancel=cancel, CloseReason=reason)
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

    def test_task_manager_ends_the_browser(self):
        shell.end_browser_on_close(self.window)
        end = self.close(cancel=False, reason=CLOSE_REASON.TaskManagerClosing)
        end.assert_called_once_with(self.form.browser)

    def test_a_windows_shutdown_or_sign_out_leaves_the_browser_running(self):
        # WinForms raises FormClosing at WM_QUERYENDSESSION and closes the form
        # only at WM_ENDSESSION; a shutdown cancelled in between must not leave
        # the window open without its browser (review of 09f0287).
        shell.end_browser_on_close(self.window)
        self.close(cancel=False, reason=CLOSE_REASON.WindowsShutDown).assert_not_called()

    def test_hooked_once(self):
        shell.end_browser_on_close(self.window)
        shell.end_browser_on_close(self.window)
        self.assertEqual(len(self.form.FormClosing.handlers), 1)

    def test_a_failure_never_stops_the_close(self):
        shell.end_browser_on_close(self.window)
        args = types.SimpleNamespace(Cancel=False, CloseReason=CLOSE_REASON.UserClosing)
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


class ShutdownTests(unittest.TestCase):
    """#512: the form's FormClosing handlers in the real order, pywebview's
    first (it asks the app's closing handler, __main__.closing), then
    shell.never_block_shutdown, then shell.end_browser_on_close. Windows Forms
    (Form.WndProc and WmClose) raises FormClosing for WM_QUERYENDSESSION with
    CloseReason.WindowsShutDown, closes the form at WM_ENDSESSION when its
    wParam is non-zero, and for a cancelled shutdown (wParam 0) raises nothing
    at all; a later click on the close button is CloseReason.UserClosing."""

    def setUp(self):
        backend = types.ModuleType("webview.platforms.edgechromium")
        backend.EdgeChrome = EdgeChrome
        self.addCleanup(setattr, EdgeChrome, "clear_user_data",
                        EdgeChrome.__dict__["clear_user_data"])
        patcher = mock.patch.dict(sys.modules, {**fake_forms(),
                                                "webview.platforms.edgechromium": backend})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.form = types.SimpleNamespace(FormClosing=Event(), browser=Browser())
        self.window = types.SimpleNamespace(native=self.form)
        self.recording, self.asked = True, 0
        self.form.FormClosing += self.pywebview_on_closing
        shell.never_block_shutdown(self.window)
        shell.end_browser_on_close(self.window)

    def app_closing(self):
        # __main__.closing, without its 10-second second-close rule (the test
        # closes more than 10 s apart): True lets the close go ahead.
        if getattr(self.window, "unminute_may_close", False) or not self.recording:
            return True
        self.asked += 1                       # the page shows the recording question
        return False

    def pywebview_on_closing(self, sender, args):
        # pywebview 6.2.1 BrowserForm.on_closing: cancel when a handler says so.
        if not self.app_closing():
            args.Cancel = True

    def close(self, reason):
        """One FormClosing, as WinForms raises it; True if the close goes ahead."""
        args = types.SimpleNamespace(Cancel=False, CloseReason=reason)
        with mock.patch.object(shell, "end_browser"):
            for handler in self.form.FormClosing.handlers:
                handler(self.form, args)
        return not args.Cancel

    def test_a_shutdown_or_task_manager_close_goes_ahead_while_recording(self):
        self.assertTrue(self.close(CLOSE_REASON.WindowsShutDown))
        self.assertTrue(self.close(CLOSE_REASON.TaskManagerClosing))

    def test_a_user_close_while_recording_waits_for_the_question(self):
        self.assertFalse(self.close(CLOSE_REASON.UserClosing))
        self.assertEqual(self.asked, 1)

    def test_a_cancelled_shutdown_still_asks_before_ending_a_recording(self):
        # Windows asks whether it may end the session (WM_QUERYENDSESSION);
        # the window lets it through.
        self.assertTrue(self.close(CLOSE_REASON.WindowsShutDown))
        # Another app, or the user at "these apps are preventing shutdown",
        # cancels it: WM_ENDSESSION with wParam 0, for which WinForms raises
        # no event. The window stays open and the recording carries on.
        # A later close by the user must still ask first (R1).
        self.assertFalse(self.close(CLOSE_REASON.UserClosing))
        self.assertFalse(getattr(self.window, "unminute_may_close", False))
        self.assertEqual(self.asked, 2)       # at the query (as before), and now

    def test_hooked_once(self):
        shell.never_block_shutdown(self.window)
        self.assertEqual(len(self.form.FormClosing.handlers), 3)


WM_CLOSE, WM_QUERYENDSESSION, WM_ENDSESSION = 0x0010, 0x0011, 0x0016
NO_REASON = 0                                  # CloseReason.None


class WinFormsForm:
    """A form whose close reason behaves as in dotnet/winforms
    src/System.Windows.Forms/System/Windows/Forms/Form.cs (commit 05a8326, the
    same logic as the 2018 port of the .NET Framework source, 195f89a):

    - the reason starts as None and is kept between messages (_closeReason);
    - WndProc: WM_CLOSE sets TaskManagerClosing only while the reason is None;
      WM_QUERYENDSESSION and WM_ENDSESSION both set WindowsShutDown;
    - Close() and the close button (WmSysCommand, SC_CLOSE) set UserClosing,
      then WM_CLOSE;
    - WmClose: FormClosing for WM_CLOSE and WM_QUERYENDSESSION; for
      WM_ENDSESSION no event, the close is cancelled when wParam is 0; the form
      closes (FormClosed, Dispose) for WM_CLOSE or WM_ENDSESSION not cancelled.
      Nothing resets the reason for a top-level form shown without ShowDialog.

    `hooks` stand for window subclass procedures (SetWindowSubclass): they see
    each message before Windows Forms handles it."""

    def __init__(self):
        self.FormClosing = Event()
        self.browser = Browser()
        self.reason = NO_REASON
        self.closed = False
        self.hooks = []

    def send(self, msg, wparam=1):
        """One message to the window; True if it was not cancelled."""
        for hook in list(self.hooks):
            hook(msg, wparam)
        if msg == WM_CLOSE:
            if self.reason == NO_REASON:
                self.reason = CLOSE_REASON.TaskManagerClosing
        elif msg in (WM_QUERYENDSESSION, WM_ENDSESSION):
            self.reason = CLOSE_REASON.WindowsShutDown
        else:
            return True                                # not a close message (WmClose)
        args = types.SimpleNamespace(Cancel=False, CloseReason=self.reason)
        if msg != WM_ENDSESSION:
            for handler in self.FormClosing.handlers:
                handler(self, args)
            if msg == WM_QUERYENDSESSION:
                return not args.Cancel
        else:
            args.Cancel = wparam == 0
        if not args.Cancel:
            self.closed = True
        return not args.Cancel

    def close(self):
        # Form.Close(), or the user's close button (SC_CLOSE).
        self.reason = CLOSE_REASON.UserClosing
        return self.send(WM_CLOSE)


class CancelledShutdownTests(unittest.TestCase):
    """#520: Windows Forms keeps CloseReason.WindowsShutDown after a cancelled
    shutdown, so a later WM_CLOSE from another program reaches FormClosing as
    WindowsShutDown, not TaskManagerClosing."""

    def setUp(self):
        backend = types.ModuleType("webview.platforms.edgechromium")
        backend.EdgeChrome = EdgeChrome
        self.addCleanup(setattr, EdgeChrome, "clear_user_data",
                        EdgeChrome.__dict__["clear_user_data"])
        patcher = mock.patch.dict(sys.modules, {**fake_forms(),
                                                "webview.platforms.edgechromium": backend})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.form = WinFormsForm()
        self.window = types.SimpleNamespace(native=self.form)
        shell.never_block_shutdown(self.window)
        shell.end_browser_on_close(self.window)
        end = mock.patch.object(shell, "end_browser")
        self.end = end.start()
        self.addCleanup(end.stop)
        # The window subclass, as the fake form's hooks; True if installed,
        # as _watch_messages returns.
        watch = mock.patch.object(shell, "_watch_messages",
                                  side_effect=lambda form, seen: form.hooks.append(seen) or True)
        self.watch = watch.start()
        self.addCleanup(watch.stop)

    def cancelled_shutdown(self):
        # Windows asks (the window lets it through and keeps its browser),
        # then the shutdown is cancelled: WM_ENDSESSION with wParam 0.
        self.assertTrue(self.form.send(WM_QUERYENDSESSION))
        self.end.assert_not_called()
        self.assertFalse(self.form.send(WM_ENDSESSION, 0))
        self.assertFalse(self.form.closed)

    def test_the_reason_stays_windows_shutdown_after_a_cancelled_shutdown(self):
        self.cancelled_shutdown()
        self.assertEqual(self.form.reason, CLOSE_REASON.WindowsShutDown)

    def test_another_programs_close_after_a_cancelled_shutdown_ends_the_browser(self):
        self.cancelled_shutdown()
        self.assertTrue(self.form.send(WM_CLOSE))       # WM_CLOSE from another program
        self.assertTrue(self.form.closed)
        self.end.assert_called_once_with(self.form.browser)

    def test_a_new_shutdown_after_a_cancelled_one_is_still_only_a_query(self):
        self.cancelled_shutdown()
        self.cancelled_shutdown()                       # asserts the browser was kept
        self.assertTrue(self.form.send(WM_CLOSE))
        self.end.assert_called_once_with(self.form.browser)

    def test_a_shutdown_that_goes_ahead_closes_with_its_browser_left_to_windows(self):
        self.assertTrue(self.form.send(WM_QUERYENDSESSION))
        self.assertTrue(self.form.send(WM_ENDSESSION, 1))
        self.assertTrue(self.form.closed)
        self.end.assert_not_called()                    # T11 residual, unchanged

    def test_the_users_close_after_a_cancelled_shutdown_ends_the_browser(self):
        self.cancelled_shutdown()
        self.assertTrue(self.form.close())
        self.end.assert_called_once_with(self.form.browser)

    def test_watched_only_from_the_first_shutdown_query_and_once(self):
        self.assertFalse(self.form.send(WM_ENDSESSION, 0))           # no query yet: no event
        self.watch.assert_not_called()
        self.cancelled_shutdown()
        self.cancelled_shutdown()
        self.watch.assert_called_once()
        self.assertEqual(len(self.form.hooks), 1)

    def test_no_watch_on_an_ordinary_close(self):
        self.assertTrue(self.form.close())
        self.watch.assert_not_called()
        self.end.assert_called_once_with(self.form.browser)

    def test_a_watch_that_cannot_be_installed_keeps_the_browser(self):
        # The behaviour before #520: the close goes ahead, the browser is
        # left to its own shutdown, and nothing fails.
        self.watch.side_effect = OSError("no subclass")
        self.cancelled_shutdown()
        self.assertTrue(self.form.send(WM_CLOSE))
        self.end.assert_not_called()

    def failed_watch_tried_again(self, first_attempt):
        # #522: a failed install must not count as watching, or no later
        # shutdown query would try again.
        install = self.watch.side_effect
        attempts = [first_attempt]

        def watch(form, seen):
            if attempts:
                return attempts.pop()(form, seen)
            return install(form, seen)
        self.watch.side_effect = watch
        self.cancelled_shutdown()                       # not watched: the cancel is unseen
        self.assertTrue(self.form.send(WM_CLOSE))
        self.end.assert_not_called()                    # browser kept, as before #520
        self.form.closed = False
        self.cancelled_shutdown()                       # the next query watches
        self.assertEqual(self.watch.call_count, 2)
        self.assertEqual(len(self.form.hooks), 1)
        self.assertTrue(self.form.send(WM_CLOSE))
        self.end.assert_called_once_with(self.form.browser)

    def test_a_watch_that_raised_is_tried_again_at_the_next_query(self):
        def raises(form, seen):
            raise OSError("no subclass")
        self.failed_watch_tried_again(raises)

    def test_a_watch_that_was_refused_is_tried_again_at_the_next_query(self):
        # SetWindowSubclass returned FALSE: nothing was installed.
        self.failed_watch_tried_again(lambda form, seen: False)

    def test_a_destroyed_window_handle_forgets_the_cancel_and_watches_again(self):
        self.cancelled_shutdown()
        self.form.send(shell.WM_NCDESTROY)               # handle recreated: watch removed
        self.form.hooks.clear()
        self.assertTrue(self.form.send(WM_CLOSE))         # stale reason, cancel not known
        self.end.assert_not_called()
        self.form.closed = False
        self.cancelled_shutdown()                         # the next query watches again
        self.assertEqual(self.watch.call_count, 2)
        self.assertTrue(self.form.send(WM_CLOSE))
        self.end.assert_called_once_with(self.form.browser)


class EndSessionTests(unittest.TestCase):
    def test_messages(self):
        self.assertEqual((shell.WM_QUERYENDSESSION, shell.WM_ENDSESSION),
                         (WM_QUERYENDSESSION, WM_ENDSESSION))
        session = shell.EndSession()
        self.assertFalse(session.cancelled)
        session.seen(WM_ENDSESSION, 1)                   # the session ends
        self.assertFalse(session.cancelled)
        session.seen(WM_ENDSESSION, 0)
        self.assertTrue(session.cancelled)
        session.seen(WM_CLOSE, 0)
        self.assertTrue(session.cancelled)
        session.seen(WM_QUERYENDSESSION, 0)
        self.assertFalse(session.cancelled)
        session.watching = True
        session.seen(WM_ENDSESSION, 0)
        session.seen(shell.WM_NCDESTROY, 0)
        self.assertEqual((session.cancelled, session.watching), (False, False))


class SubclassProcTests(unittest.TestCase):
    def proc(self, seen=None, remove=None):
        self.defaults, self.removed = [], []

        def default(*message):
            self.defaults.append(message)
            return 7

        return shell.subclass_proc(seen or (lambda msg, wparam: None), default,
                                   remove or (lambda hwnd, uid: self.removed.append((hwnd, uid))))

    def test_every_message_is_handed_on_unchanged(self):
        seen = []
        proc = self.proc(seen=lambda msg, wparam: seen.append((msg, wparam)))
        self.assertEqual(proc(11, WM_ENDSESSION, 0, 5, shell.WATCH_ID, 0), 7)
        self.assertEqual(seen, [(WM_ENDSESSION, 0)])
        self.assertEqual(self.defaults, [(11, WM_ENDSESSION, 0, 5)])
        self.assertEqual(self.removed, [])

    def test_a_failure_never_changes_the_answer(self):
        def boom(*_):
            raise RuntimeError("x")
        proc = self.proc(seen=boom, remove=boom)
        self.assertEqual(proc(11, WM_QUERYENDSESSION, 0, 0, shell.WATCH_ID, 0), 7)
        self.assertEqual(proc(11, shell.WM_NCDESTROY, 0, 0, shell.WATCH_ID, 0), 7)
        self.assertEqual(len(self.defaults), 2)

    def test_a_failure_handing_on_never_refuses_to_end_the_session(self):
        # #522: if DefSubclassProc raised, the ctypes callback could answer 0
        # (observed, not documented by Python), which for WM_QUERYENDSESSION
        # refuses to end the session.
        def default(*message):
            raise OSError("exception: access violation")
        proc = shell.subclass_proc(lambda msg, wparam: None, default, lambda hwnd, uid: None)
        self.assertEqual(proc(11, WM_QUERYENDSESSION, 0, 0, shell.WATCH_ID, 0), 1)   # TRUE
        for msg in (WM_ENDSESSION, WM_CLOSE, shell.WM_NCDESTROY):
            self.assertEqual(proc(11, msg, 0, 0, shell.WATCH_ID, 0), 0)          # processed

    @unittest.skipUnless(sys.platform == "win32", "user32 is a Windows library")
    def test_windows_is_told_the_session_may_end(self):
        # The same through the native callback, called by user32 as Windows
        # would call it (CallWindowProcW, no window): TRUE, not 0.
        import ctypes
        from ctypes import wintypes

        def default(*message):
            raise OSError("exception: access violation")
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM)
        proc = shell.subclass_proc(lambda msg, wparam: None, default, lambda hwnd, uid: None)
        callback = proc_type(lambda hwnd, msg, wparam, lparam:
                             proc(hwnd, msg, wparam, lparam, shell.WATCH_ID, 0))
        user32 = ctypes.WinDLL("user32")
        user32.CallWindowProcW.argtypes = (ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                           wintypes.WPARAM, wintypes.LPARAM)
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        address = ctypes.cast(callback, ctypes.c_void_p).value
        self.assertEqual(user32.CallWindowProcW(address, None, WM_QUERYENDSESSION, 0, 0), 1)

    def test_removed_when_the_window_is_destroyed(self):
        proc = self.proc()
        proc(11, shell.WM_NCDESTROY, 0, 0, shell.WATCH_ID, 0)
        self.assertEqual(self.removed, [(11, shell.WATCH_ID)])

    @unittest.skipUnless(sys.platform == "win32", "comctl32 is a Windows library")
    def test_the_native_calls_load(self):
        # No window: comctl32 refuses a null window handle, so nothing is
        # subclassed, but the function types and exports are checked.
        form = types.SimpleNamespace(Handle=types.SimpleNamespace(ToInt64=lambda: 0))
        before = len(shell._watch_procs)
        self.assertFalse(shell._watch_messages(form, lambda msg, wparam: None))
        self.assertEqual(len(shell._watch_procs), before + 1)
        del shell._watch_procs[before:]


WEBVIEW = importlib.util.find_spec("webview")


@unittest.skipUnless(WEBVIEW, "pywebview not installed")
class PinnedPywebviewTests(unittest.TestCase):
    """_clear_after_end() replaces pywebview 6.2.1's private-mode clean-up at
    class level. If an upgrade renames or changes it, this fails, so the
    replacement is re-checked rather than silently skipped (the profile would
    then stay until the next start's storage_folder clean-up). Read as source:
    importing the backend needs .NET."""

    def source(self, name):
        folder = Path(WEBVIEW.submodule_search_locations[0]) / "platforms"
        return ast.parse((folder / name).read_text(encoding="utf-8"))

    def test_edgechrome_still_clears_the_profile_the_way_it_replaces(self):
        views = [node for node in ast.walk(self.source("edgechromium.py"))
                 if isinstance(node, ast.ClassDef) and node.name == "EdgeChrome"]
        self.assertEqual(len(views), 1)
        clear = [node for node in views[0].body
                 if isinstance(node, ast.FunctionDef) and node.name == "clear_user_data"]
        self.assertEqual(len(clear), 1)
        body = ast.unparse(clear[0])
        for part in ("_state['private_mode']", "BrowserProcessId", "self.webview.Dispose()",
                     "shutil.rmtree(self.user_data_folder)"):
            self.assertIn(part, body)

    def test_the_form_still_calls_it_when_closed(self):
        self.assertIn("self.browser.clear_user_data()", ast.unparse(self.source("winforms.py")))


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
        self.assertIn("shell.never_block_shutdown(window)", source)

    def test_the_closing_handler_reads_the_allowance_the_tests_model(self):
        # ShutdownTests.app_closing stands in for __main__.closing: it lets a
        # close go ahead when the window is marked closable or nothing records.
        tree = ast.parse((ROOT / "workspace" / "__main__.py").read_text(encoding="utf-8"))
        closing = [node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "closing"]
        self.assertEqual(len(closing), 1)
        self.assertIn("if getattr(window, 'unminute_may_close', False) or not "
                      "ws.recording_active():\n        return True", ast.unparse(closing[0]))


if __name__ == "__main__":
    unittest.main()
