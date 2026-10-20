# Worker isolation, pause/stop behaviour and quiet defaults (#15).
# Spawns real worker processes with test-only engines (no models).
#   python -m unittest discover -s tests -v

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
SR = 16000

from workspace import resources  # noqa: E402

if HAVE_NUMPY:
    import numpy as np

    from workspace import runner
    from workspace.store import Store
    from workspace.worker import (WorkerCrashed, WorkerEngine, WorkerError, WorkerHandle,
                                  WorkerStopped)

    class PlainReader:
        def __init__(self, seconds):
            self.n = int(seconds * SR)

        def __len__(self):
            return self.n

        def read(self, a, b):
            return np.zeros(max(0, min(b, self.n) - a), dtype=np.float32)


def fake(name):
    return f"test:fake_worker_engines:{name}"


def wait_until(condition, timeout=30.0):
    """Poll instead of sleeping a fixed time: robust on a loaded machine."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


class QuietDefaultTests(unittest.TestCase):
    def test_threads_half_capped_at_four_min_one(self):
        for cpus, quiet in ((1, 1), (2, 1), (4, 2), (8, 4), (16, 4), (32, 4)):
            self.assertEqual(resources.worker_threads(resources.QUIET, cpus), quiet)
        self.assertEqual(resources.worker_threads(resources.FASTER, 16), 14)
        self.assertEqual(resources.worker_threads(resources.FASTER, 2), 1)

    def test_automatic_work_waits_on_battery_unless_overridden(self):
        self.assertFalse(resources.may_start_automatic_work(battery=True))
        self.assertTrue(resources.may_start_automatic_work(override=True, battery=True))
        self.assertTrue(resources.may_start_automatic_work(battery=False))
        self.assertIsInstance(resources.on_battery(), bool)


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = WorkerHandle(self.tmp.name, quiet=True, allow_test_engines=True)

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def test_transcribes_in_a_separate_low_priority_process(self):
        self.h.load(fake("echo"), 2)
        segs, dur = self.h.transcribe(np.zeros(SR * 3, dtype=np.float32))
        self.assertEqual(dur, 3.0)
        self.assertIn("n=48000 threads=2", segs[0].text)
        self.assertNotIn(f"pid={os.getpid()}", segs[0].text)       # not this process
        if sys.platform == "win32":
            self.assertEqual(resources.get_priority(self.h.pid),
                             resources.BELOW_NORMAL_PRIORITY_CLASS)

    def test_one_model_at_a_time_in_the_same_worker(self):
        self.h.load(fake("echo"), 2)
        pid = self.h.pid
        self.h.load(fake("echo"), 3)                                 # reload, same process
        self.assertEqual(self.h.pid, pid)
        self.assertIn("threads=3", self.h.transcribe(np.zeros(10, np.float32))[0][0].text)

    def test_crash_is_detected_and_next_request_restarts(self):
        marker = Path(self.tmp.name) / "crashed"
        os.environ["UNMINUTE_TEST_MARKER"] = str(marker)
        try:
            self.h.load(fake("crash_once"), 1)
            first = self.h.pid
            with self.assertRaises(WorkerCrashed):
                self.h.transcribe(np.zeros(100, np.float32))
            self.h.load(fake("crash_once"), 1)
            self.assertEqual(self.h.transcribe(np.zeros(100, np.float32))[0][0].text, "recovered")
            self.assertNotEqual(self.h.pid, first)
        finally:
            os.environ.pop("UNMINUTE_TEST_MARKER", None)

    def test_stop_now_returns_promptly(self):
        import threading
        self.h.load(fake("sleepy"), 1)
        threading.Timer(0.5, self.h.stop_now).start()
        t = time.monotonic()
        with self.assertRaises(WorkerStopped):
            self.h.transcribe(np.zeros(100, np.float32))
        self.assertLess(time.monotonic() - t, 20)   # well under the 30 s sleep
        self.assertIsNone(self.h.pid)

    def test_idle_worker_unloads(self):
        h = WorkerHandle(self.tmp.name, idle_unload_s=0.5, allow_test_engines=True)
        try:
            h.load(fake("echo"), 1)
            self.assertIsNotNone(h.pid)
            self.assertTrue(wait_until(lambda: h.pid is None))       # memory released
            h.load(fake("echo"), 1)                                  # transparently restarts
            self.assertIsNotNone(h.pid)
        finally:
            h.close()

    def test_idle_loop_sleeps_while_there_is_no_worker(self):
        # docs/RELEASE_STANDARD.md section 3: no background wake-ups without work.
        h = WorkerHandle(self.tmp.name, idle_unload_s=0.2, allow_test_engines=True)
        try:
            time.sleep(0.6)                          # several old 50 ms ticks' worth
            self.assertLessEqual(h.idle_wakeups, 1)
            h.load(fake("echo"), 1)                  # a worker: the loop wakes to watch it
            self.assertTrue(wait_until(lambda: h.pid is None))
            settled = h.idle_wakeups
            time.sleep(0.6)
            self.assertLessEqual(h.idle_wakeups - settled, 1)    # asleep again
        finally:
            h.close()
        self.assertTrue(wait_until(lambda: not h._timer.is_alive()))   # close ends the loop

    def test_requests_are_bounded(self):
        with self.assertRaises(ValueError):
            self.h.transcribe(np.zeros(SR * 121, dtype=np.float32))

    def test_load_errors_carry_a_message_but_no_traceback(self):
        with self.assertRaises(WorkerError) as ctx:
            self.h.load(fake("load_fails"), 1)
        self.assertIn("failed verification", str(ctx.exception))
        self.assertNotIn("Traceback", str(ctx.exception))

    def test_failed_model_switch_recovers(self):
        from workspace.worker import EngineUnavailable
        self.h.load(fake("echo"), 1)
        with self.assertRaises(EngineUnavailable):
            self.h.load(fake("load_fails"), 1)
        self.h.load(fake("echo"), 1)                       # back to a working model
        self.assertTrue(self.h.transcribe(np.zeros(10, np.float32))[0])

    def test_stop_between_requests_is_not_lost(self):
        self.h.load(fake("echo"), 1)
        self.h.stop_now()                                   # nothing in flight
        with self.assertRaises(WorkerStopped):
            self.h.transcribe(np.zeros(10, np.float32))
        with self.assertRaises(WorkerStopped):
            self.h.load(fake("echo"), 1)
        self.h.begin_run()                                  # the next run
        self.h.load(fake("echo"), 1)
        self.assertTrue(self.h.transcribe(np.zeros(10, np.float32))[0])

    def test_hung_worker_is_detected(self):
        self.h.load(fake("sleepy"), 1)
        t = time.monotonic()
        with self.assertRaises(WorkerCrashed):
            self.h.transcribe(np.zeros(10, np.float32), timeout=1.0)
        self.assertLess(time.monotonic() - t, 20)   # well under the 30 s sleep

    def test_reloads_by_itself_after_an_idle_unload(self):
        h = WorkerHandle(self.tmp.name, idle_unload_s=0.5, allow_test_engines=True)
        try:
            h.load(fake("echo"), 2)
            self.assertTrue(wait_until(lambda: h.pid is None))
            segs, _ = h.transcribe(np.zeros(10, np.float32))   # no explicit load
            self.assertIn("threads=2", segs[0].text)
        finally:
            h.close()

    def test_error_text_has_no_paths(self):
        from workspace.worker import safe_message
        msg = safe_message(RuntimeError(r"cannot open C:\Users\Jordan Smith\models\x.onnx: "
                                        r"and \\server\share\y and /home/jo/z"))
        self.assertNotIn("Users", msg)
        self.assertNotIn("server", msg)
        self.assertNotIn("/home", msg)
        self.assertIn("<path>", msg)

    def test_worker_cannot_reach_the_network(self):
        self.h.load(fake("network"), 1)
        text = self.h.transcribe(np.zeros(10, np.float32))[0][0].text
        self.assertTrue(text.startswith("blocked"), text)

    def test_worker_unpickles_weights_only(self):
        # #218: every torch.load in the worker is weights-only, whatever the
        # environment it inherited says.
        from unittest import mock
        h = WorkerHandle(self.tmp.name, allow_test_engines=True)
        try:
            with mock.patch.dict(os.environ, {"TORCH_FORCE_WEIGHTS_ONLY_LOAD": "0",
                                              "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD": "1"}):
                h.load(fake("weights_only_env"), 1)
            text = h.transcribe(np.zeros(10, np.float32))[0][0].text
        finally:
            h.close()
        self.assertEqual(text, "env {'TORCH_FORCE_WEIGHTS_ONLY_LOAD': '1', "
                               "'TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD': None}")


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class RunnerWithWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite", protector=keyprotect.TestProtector())
        self.h = WorkerHandle(self.tmp.name, allow_test_engines=True)

    def tearDown(self):
        self.h.close()
        self.store.close()
        self.tmp.cleanup()

    def meeting(self, seconds=100):
        mid = self.store.create_meeting("t")
        sid = self.store.add_source(mid, original_path=None, original_sha256="a" * 64,
                                    original_size=1, original_mtime_ns=0, audio_id="b" * 64,
                                    samples=seconds * SR, sample_rate=SR)
        plan = [(a, min(a + 20 * SR, seconds * SR), a, min(a + 20 * SR, seconds * SR))
                for a in range(0, seconds * SR, 20 * SR)]
        vid = self.store.ensure_version(mid, sid, engine="fake", model="fake", model_revision="1",
                                        precision="int8", config={}, chunk_plan=plan)
        return mid, vid, PlainReader(seconds)

    def test_full_run_through_the_worker(self):
        mid, vid, reader = self.meeting()
        eng = WorkerEngine(self.h, fake("echo"), 1)
        self.assertEqual(runner.run_version(self.store, mid, vid, reader, eng),
                         runner.RunResult.DONE)
        self.assertEqual(len(self.store.transcript(vid)), 5)

    def test_repeated_crashes_stop_instead_of_looping(self):
        mid, vid, reader = self.meeting()
        eng = WorkerEngine(self.h, fake("crash_always"), 1)
        results = [runner.run_version(self.store, mid, vid, reader, eng) for _ in range(5)]
        self.assertEqual(results, [runner.RunResult.FAILED] * 5)
        self.assertEqual(self.store.chunks(vid)[0]["failures"], runner.MAX_ATTEMPTS)
        self.assertIn("failed 3 times", self.store.meeting(mid)["last_error"])
        self.assertEqual(self.store.chunks(vid, status="done"), [])

    def test_unavailable_model_does_not_count_against_the_chunk(self):
        mid, vid, reader = self.meeting()
        for _ in range(4):
            eng = WorkerEngine(self.h, fake("load_fails"), 1)
            self.assertEqual(runner.run_version(self.store, mid, vid, reader, eng),
                             runner.RunResult.FAILED)
        self.assertEqual(self.store.chunks(vid)[0]["failures"], 0)
        self.assertEqual({a["result"] for a in self.store.attempts(vid, 0)}, {"unavailable"})
        self.assertIn("not ready", self.store.meeting(mid)["last_error"])
        eng = WorkerEngine(self.h, fake("echo"), 1)          # model repaired: it just works
        self.assertEqual(runner.run_version(self.store, mid, vid, reader, eng),
                         runner.RunResult.DONE)

    def test_explicit_retry_resets_the_failure_limit(self):
        mid, vid, reader = self.meeting()
        for _ in range(3):
            runner.run_version(self.store, mid, vid, reader,
                               WorkerEngine(self.h, fake("crash_always"), 1))
        eng = WorkerEngine(self.h, fake("echo"), 1)
        self.assertEqual(runner.run_version(self.store, mid, vid, reader, eng),
                         runner.RunResult.FAILED)               # capped, no attempt made
        self.assertEqual(runner.run_version(self.store, mid, vid, reader, eng,
                                            retry_failed=True),
                         runner.RunResult.DONE)

    def test_pause_with_stop_now_keeps_chunk_pending_and_is_not_a_failure(self):
        import threading
        mid, vid, reader = self.meeting()
        eng = WorkerEngine(self.h, fake("sleepy"), 1)

        def pause():
            self.store_pause_conn.set_paused(mid, True)
            self.h.stop_now()
        self.store_pause_conn = Store(Path(self.tmp.name) / "db.sqlite",
                                      protector=keyprotect.TestProtector())   # UI thread's handle
        try:
            threading.Timer(1.0, pause).start()
            t = time.monotonic()
            self.assertEqual(runner.run_version(self.store, mid, vid, reader, eng),
                             runner.RunResult.PAUSED)
            self.assertLess(time.monotonic() - t, 20)   # well under the 30 s sleep
        finally:
            self.store_pause_conn.close()
        c = self.store.chunks(vid)[0]
        self.assertEqual((c["status"], c["failures"]), ("pending", 0))
        self.assertEqual(self.store.progress(vid)[0], 0)


if __name__ == "__main__":
    unittest.main()
