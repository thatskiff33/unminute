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
        self.h = WorkerHandle(self.tmp.name, quiet=True)

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
        os.environ["WHISPER_TEST_MARKER"] = str(marker)
        try:
            self.h.load(fake("crash_once"), 1)
            first = self.h.pid
            with self.assertRaises(WorkerCrashed):
                self.h.transcribe(np.zeros(100, np.float32))
            self.h.load(fake("crash_once"), 1)
            self.assertEqual(self.h.transcribe(np.zeros(100, np.float32))[0][0].text, "recovered")
            self.assertNotEqual(self.h.pid, first)
        finally:
            os.environ.pop("WHISPER_TEST_MARKER", None)

    def test_stop_now_returns_promptly(self):
        import threading
        self.h.load(fake("sleepy"), 1)
        threading.Timer(0.5, self.h.stop_now).start()
        t = time.monotonic()
        with self.assertRaises(WorkerStopped):
            self.h.transcribe(np.zeros(100, np.float32))
        self.assertLess(time.monotonic() - t, 5)
        self.assertIsNone(self.h.pid)

    def test_idle_worker_unloads(self):
        h = WorkerHandle(self.tmp.name, idle_unload_s=0.5)
        try:
            h.load(fake("echo"), 1)
            self.assertIsNotNone(h.pid)
            time.sleep(2.0)
            self.assertIsNone(h.pid)                                 # memory released
            h.load(fake("echo"), 1)                                  # transparently restarts
            self.assertIsNotNone(h.pid)
        finally:
            h.close()

    def test_requests_are_bounded(self):
        with self.assertRaises(ValueError):
            self.h.transcribe(np.zeros(SR * 121, dtype=np.float32))

    def test_load_errors_carry_a_message_but_no_traceback(self):
        with self.assertRaises(WorkerError) as ctx:
            self.h.load(fake("load_fails"), 1)
        self.assertIn("failed verification", str(ctx.exception))
        self.assertNotIn("Traceback", str(ctx.exception))

    def test_worker_cannot_reach_the_network(self):
        self.h.load(fake("network"), 1)
        text = self.h.transcribe(np.zeros(10, np.float32))[0][0].text
        self.assertTrue(text.startswith("blocked"), text)


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class RunnerWithWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite")
        self.h = WorkerHandle(self.tmp.name)

    def tearDown(self):
        self.h.close()
        self.store.close()
        self.tmp.cleanup()

    def meeting(self, seconds=100):
        mid = self.store.create_meeting("t")
        sid = self.store.add_source(mid, original_path=None, original_sha256="a" * 64,
                                    original_size=1, original_mtime_ns=0, audio_sha256="b" * 64,
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

    def test_pause_with_stop_now_keeps_chunk_pending_and_is_not_a_failure(self):
        import threading
        mid, vid, reader = self.meeting()
        eng = WorkerEngine(self.h, fake("sleepy"), 1)

        def pause():
            self.store_pause_conn.set_paused(mid, True)
            self.h.stop_now()
        self.store_pause_conn = Store(Path(self.tmp.name) / "db.sqlite")   # UI thread's handle
        try:
            threading.Timer(1.0, pause).start()
            t = time.monotonic()
            self.assertEqual(runner.run_version(self.store, mid, vid, reader, eng),
                             runner.RunResult.PAUSED)
            self.assertLess(time.monotonic() - t, 6)
        finally:
            self.store_pause_conn.close()
        c = self.store.chunks(vid)[0]
        self.assertEqual((c["status"], c["failures"]), ("pending", 0))
        self.assertEqual(self.store.progress(vid)[0], 0)


if __name__ == "__main__":
    unittest.main()
