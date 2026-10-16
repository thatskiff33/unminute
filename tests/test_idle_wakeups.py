# Background threads wait for work instead of waking every second (R7, #128).
# The watch and policy loops run here under a fake clock whose waits advance
# the clock instead of sleeping, so an hour of idling takes milliseconds and
# every wake-up is counted.
#   python tools/run_tests.py

import importlib.util
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))


class FakeTime:
    """A monotonic clock and a threading.Event stand-in in one. wait() moves
    the clock on by its timeout (or returns at once if set) and records the
    timeout; once the clock passes `until`, the workspace starts closing."""

    def __init__(self, ws, start, until):
        self.ws, self.t, self.until = ws, start, until
        self.waits, self.flag = [], False

    def __call__(self):
        return self.t

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True

    def clear(self):
        self.flag = False

    def wait(self, timeout):
        self.waits.append(timeout)
        if not self.flag:
            self.t += timeout
            if self.t >= self.until:
                self.ws._closing.set()
        return self.flag


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class WakeupTests(unittest.TestCase):
    def setUp(self):
        from workspace.app import Workspace
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = Workspace(root / "ws", root / "models", start=False, battery=False,
                            allow_test_engines=True, policy_reader=lambda: ({}, ()))

    def tearDown(self):
        self.ws.close()
        self.tmp.cleanup()

    def fake(self, attr, seconds, start=12.0):
        """Run the loop on a fake clock for `seconds`. 12.0: just after boot,
        where the monotonic clock is still small."""
        ft = FakeTime(self.ws, start, start + seconds)
        self.ws._clock = ft
        setattr(self.ws, attr, ft)
        return ft

    # -- watched folders ---------------------------------------------------------

    def run_watch(self, seconds, scan_now=False, during_first=None):
        ft = self.fake("_scan_wake", seconds)
        if scan_now:
            self.ws.scan_now()
        scans = []

        def scan():
            scans.append(ft.t)
            if during_first and len(scans) == 1:
                during_first()
        with mock.patch.object(self.ws, "_scan_folders", side_effect=scan):
            self.ws._watch_loop()
        return ft, scans

    def test_watch_thread_sleeps_until_the_next_scan(self):
        from workspace import app, intake
        ft, scans = self.run_watch(3600)
        # The launch scan waits for start-up, then one scan every interval.
        first = 12.0 + intake.LAUNCH_SCAN_DELAY_S
        self.assertEqual(scans, [first + k * intake.SCAN_INTERVAL_S for k in range(12)])
        # Never asleep longer than the cap, so a scan that fell due while the
        # computer slept runs within a minute of waking.
        self.assertLessEqual(max(ft.waits), app.WATCH_MAX_WAIT_S)
        # An hour idle: about one wake-up a minute plus the scans (was 3,600).
        self.assertLessEqual(len(ft.waits), 3600 / app.WATCH_MAX_WAIT_S + len(scans) + 1)

    def test_scan_now_scans_at_once(self):
        from workspace import intake
        _, scans = self.run_watch(400, scan_now=True)
        self.assertEqual(scans, [12.0, 12.0 + intake.SCAN_INTERVAL_S])

    def test_a_request_made_during_a_scan_scans_again(self):
        from workspace import intake
        # e.g. Check now, or a folder added, while a long scan is hashing
        _, scans = self.run_watch(400, during_first=self.ws.scan_now)
        first = 12.0 + intake.LAUNCH_SCAN_DELAY_S
        self.assertEqual(scans, [first, first, first + intake.SCAN_INTERVAL_S])

    # -- policy: clipboard clears and recording stops ---------------------------

    def test_policy_thread_waits_to_be_woken_when_idle(self):
        from workspace import app
        ft = self.fake("_policy_wake", 600)
        with mock.patch.object(self.ws, "_policy_tick") as tick:
            self.ws._policy_loop()
        self.assertEqual(set(ft.waits), {app.POLICY_IDLE_WAIT_S})
        self.assertEqual(tick.call_count, 600 / app.POLICY_IDLE_WAIT_S)

    def test_a_clipboard_clear_runs_when_due_without_polling(self):
        from workspace import app, clipboard
        ft = self.fake("_policy_wake", 600)
        self.ws._schedule_clipboard_clear(7, 100)       # wakes the thread
        self.assertTrue(ft.flag)
        cleared = []
        with mock.patch.object(clipboard, "clear_if_unchanged",
                               side_effect=lambda seq: cleared.append((seq, ft.t))):
            self.ws._policy_loop()
        self.assertEqual(cleared, [(7, 12.0 + 100)])    # on time
        # No faster than the idle wait: 30, 30, 30, then the last 10 to the clear.
        self.assertEqual(ft.waits[:4], [30.0, 30.0, 30.0, 10.0])
        self.assertLessEqual(max(ft.waits), app.POLICY_IDLE_WAIT_S)
        self.assertLessEqual(len(ft.waits), 600 / app.POLICY_IDLE_WAIT_S + 2)

    def test_a_recording_is_checked_every_second(self):
        from workspace import app
        self.assertEqual(self.ws._policy_wait_s(), app.POLICY_IDLE_WAIT_S)
        self.ws._rec_starting = True
        self.assertEqual(self.ws._policy_wait_s(), 1.0)
        self.ws._rec_starting = False
        self.ws._recording = mock.Mock()
        self.assertEqual(self.ws._policy_wait_s(), 1.0)
        self.ws._recording = None

    def test_starting_a_recording_wakes_the_policy_thread(self):
        from workspace.app import WorkspaceError
        # From its first second, not after the idle wait: a policy change read
        # meanwhile (Settings re-reads it) stops the recording at once.
        with mock.patch.object(self.ws, "_need_microphone"), \
                mock.patch.object(self.ws, "consent_required", return_value=False), \
                mock.patch.object(self.ws, "_device", side_effect=WorkspaceError("no device")):
            self.assertFalse(self.ws._policy_wake.is_set())
            with self.assertRaises(WorkspaceError):
                self.ws.start_recording(None)
        self.assertTrue(self.ws._policy_wake.is_set())

    # -- closing ----------------------------------------------------------------

    def test_close_wakes_both_threads_at_once(self):
        for target in (self.ws._watch_loop, self.ws._policy_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self.ws._threads.append(t)
        time.sleep(0.2)                                  # both are waiting
        started = time.monotonic()
        self.ws.close()
        self.assertLess(time.monotonic() - started, 5)  # the waits are 30 and 60 s
        self.assertFalse(any(t.is_alive() for t in self.ws._threads))
        self.ws.close = lambda: None                     # tearDown: already closed


if __name__ == "__main__":
    unittest.main()
