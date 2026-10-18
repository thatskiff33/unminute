# tests/test_minimise.py
# What the window tells WebView2 when it is minimised and restored (#129,
# docs/RELEASE_STANDARD.md section 3). The WinForms wiring in workspace/shell.py
# is thin; the decisions are in shell.Minimise, tested here with fakes. A real
# window was checked by hand (see PR #143): hidden, suspended and back.

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workspace import shell  # noqa: E402


class FakeWebView:
    def __init__(self):
        self.calls = []
        self.fail = set()

    def _do(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise OSError(f"{name} failed")

    def hide(self):
        self._do("hide")

    def show(self):
        self._do("show")

    def low_memory(self, on):
        self._do(f"low_memory {on}")

    def suspend(self):
        self._do("suspend")

    def resume(self):
        self._do("resume")


class FakeLater:
    """Holds delayed steps until the test runs them."""

    def __init__(self):
        self.waiting = []

    def __call__(self, seconds, fn):
        later = self

        class Handle:
            def __init__(self):
                self.seconds, self.fn, self.cancelled = seconds, fn, False

            def cancel(self):
                self.cancelled = True
                later.waiting.remove(self)
        handle = Handle()
        self.waiting.append(handle)
        return handle

    def run(self):
        for handle in list(self.waiting):
            self.waiting.remove(handle)
            handle.fn()


class MinimiseTests(unittest.TestCase):
    def setUp(self):
        self.webview, self.recording, self.later = FakeWebView(), [False], FakeLater()
        self.state = shell.Minimise(self.webview, lambda: self.recording[0], self.later)

    def test_idle_window_is_hidden_then_suspended_after_a_delay(self):
        self.state.changed(True)
        self.assertEqual(["hide"], self.webview.calls)          # not yet: autosave may run
        self.assertEqual(shell.SUSPEND_DELAY_S, self.later.waiting[0].seconds)
        self.later.run()
        self.assertEqual(["hide", "suspend"], self.webview.calls)  # no manual memory target
        self.webview.calls.clear()
        self.state.changed(False)
        self.assertEqual(["resume", "show"], self.webview.calls)

    def test_restoring_before_the_delay_cancels_the_suspend(self):
        self.state.changed(True)
        self.state.changed(False)
        self.assertEqual([], self.later.waiting)
        self.assertEqual(["hide", "show"], self.webview.calls)

    def test_a_recording_keeps_its_page_running_at_low_memory(self):
        self.recording[0] = True
        self.state.changed(True)
        self.assertEqual(["hide", "low_memory True"], self.webview.calls)
        self.assertEqual([], self.later.waiting)                  # never suspended
        self.recording[0] = False                                 # it ends while minimised
        self.webview.calls.clear()
        self.state.changed(False)
        self.assertEqual(["low_memory False", "show"], self.webview.calls)

    def test_a_recording_that_starts_during_the_delay_is_not_suspended(self):
        self.state.changed(True)
        self.recording[0] = True
        self.later.run()
        self.assertEqual(["hide", "low_memory True"], self.webview.calls)
        self.webview.calls.clear()
        self.state.changed(False)
        self.assertEqual(["low_memory False", "show"], self.webview.calls)

    def test_a_failed_step_still_restores_the_window(self):
        # The flag is set before the call, so a failure is undone on restore,
        # and the window is shown whatever fails.
        self.recording[0] = True
        self.webview.fail = {"low_memory True"}
        with self.assertRaises(OSError):
            self.state.changed(True)
        self.webview.fail = {"low_memory False"}
        self.webview.calls.clear()
        with self.assertRaises(OSError):
            self.state.changed(False)
        self.assertEqual(["low_memory False", "show"], self.webview.calls)

    def test_repeated_events_change_nothing(self):
        # WinForms raises Resize for moves and size changes too.
        self.state.changed(False)
        self.assertEqual([], self.webview.calls)
        self.state.changed(True)
        self.state.changed(True)
        self.assertEqual(1, len(self.later.waiting))

    # The fallback (#196): the window's Resize handler and the suspend timer
    # run each step through shell._guarded, as watch_minimise and _UiTimer do.
    # A WebView2 failure never reaches WinForms, and the next restore still
    # shows the window as normal.

    def test_a_failed_hide_carries_on_and_the_window_comes_back(self):
        self.webview.fail = {"hide"}
        shell._guarded(self.state.changed, True)               # no exception escapes
        self.assertEqual([], self.later.waiting)                # never suspended
        self.webview.fail = set()
        shell._guarded(self.state.changed, False)
        self.assertEqual(["hide", "show"], self.webview.calls)
        self.assertFalse(self.state.minimised)

    def test_a_failed_suspend_carries_on_and_is_undone_on_restore(self):
        self.webview.fail = {"suspend"}
        shell._guarded(self.state.changed, True)
        delayed = self.later.waiting.pop()
        shell._guarded(delayed.fn)                              # as the timer's Tick runs it
        self.webview.fail = set()
        shell._guarded(self.state.changed, False)
        self.assertEqual(["hide", "suspend", "resume", "show"], self.webview.calls)
        # Minimising again works as normal: nothing is stuck after the failure.
        self.webview.calls.clear()
        shell._guarded(self.state.changed, True)
        self.later.run()
        self.assertEqual(["hide", "suspend"], self.webview.calls)


if __name__ == "__main__":
    unittest.main()
