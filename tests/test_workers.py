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
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
SR = 16000

from workspace import resources  # noqa: E402

if HAVE_NUMPY:
    import numpy as np

    from worker_exit import join_workers
    from workspace import diag, runner
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

    def test_worker_adds_no_dll_folder_named_by_the_environment(self):
        # #357 (T25): CTranslate2 4.8.3 adds %HIP_PATH%\bin to the DLL search
        # path when it loads. A HIP_PATH the worker inherits, here a scratch
        # folder, is removed first, so no folder outside the app is added; the
        # app's own environment keeps it.
        with tempfile.TemporaryDirectory() as hip:
            Path(hip, "bin").mkdir()
            with mock.patch.dict(os.environ, {"HIP_PATH": hip}):
                self.h.load(fake("dll_folders"), 1)
                self.assertEqual(os.environ["HIP_PATH"], hip)
            text = self.h.transcribe(np.zeros(10, np.float32))[0][0].text
        self.assertEqual(text, "folders []")

    @unittest.skipUnless(sys.platform == "win32", "Windows DLL search and executable lookup")
    def test_speaker_identification_adds_no_dll_folder_named_by_the_environment(self):
        # #358 (T25): PyTorch 2.14.1 and TorchCodec 0.17.0, which pyannote.audio
        # loads in this worker, add the user base's Library\bin (PYTHONUSERBASE),
        # %NVTOOLSEXT_PATH%\bin\x64, %CUDA_PATH_V*%\bin, their defaults under
        # %ProgramFiles% (which Windows sets for the worker from ProgramW6432)
        # and the folder of an ffmpeg on PATH to the DLL search path. Scratch
        # folders named those ways are all found here, and none in the worker;
        # the app keeps its own environment.
        import json

        import fake_worker_engines as fakes
        from workspace import audiocrypt, media
        from workspace.worker import diarize
        path, key, context = Path(self.tmp.name) / "a.uma", bytes(range(32)), ("decoded", "m")
        with audiocrypt.AudioWriter(path, key, context, "float32", 1, SR) as w:
            w.write(np.zeros(SR, dtype=np.float32))
        audio = media.AudioRef(path, key, context, SR)
        with tempfile.TemporaryDirectory() as root:
            env, planted = fakes.planted_dll_environment(root)
            with mock.patch.dict(os.environ, env):
                here = fakes.torch_dll_folders()       # this process is not cleared
                for name in ("nvtx", "cuda", "pfiles_nvtx", "pfiles_cuda", "ffmpeg"):
                    self.assertIn(planted[name], here)
                turns, _, _, _ = diarize(self.h, fake("dll_folders_diarizer"), audio, None, 1)
                self.assertEqual({k: os.environ.get(k) for k in env}, env)
        found = json.loads(turns[0][2])
        self.assertEqual([f for f in found if f.startswith(os.path.abspath(root))], [])

    @unittest.skipUnless(sys.platform == "win32", "Windows DLL search and executable lookup")
    def test_the_dll_path_environment_is_cleared_and_ffmpeg_kept_off_path(self):
        # #358 (T25): what every worker and --self-test clear. TorchCodec finds
        # ffmpeg with shutil.which, which on Windows looks in the current folder
        # first and treats an empty PATH entry as it: neither finds one after.
        import shutil

        from workspace import update, worker
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as root:
            here, keep = Path(root, "here"), Path(root, "keep")
            here.mkdir()
            keep.mkdir()
            (here / "ffmpeg.exe").write_bytes(b"")
            on_path = Path(root, "on-path")
            on_path.mkdir()
            (on_path / "ffmpeg.exe").write_bytes(b"")
            env = {"HIP_PATH": root, "PYTHONUSERBASE": root, "NVTOOLSEXT_PATH": root,
                   "CUDA_PATH_V13_0": root, "CUDA_PATH_V12_8": root, "CUDA_PATH": root,
                   "ProgramFiles": root, "ProgramW6432": root,
                   "PATH": os.pathsep.join([str(keep), "", str(on_path), ".", str(keep)])}
            try:
                os.chdir(here)
                with mock.patch.dict(os.environ, env):
                    self.assertIsNotNone(shutil.which("ffmpeg"))      # the current folder's
                    worker.clear_dll_path_environment()
                    self.assertIsNone(shutil.which("ffmpeg"))
                    self.assertEqual(os.environ["PATH"], os.pathsep.join([str(keep), str(keep)]))
                    gone = [name for name in env if name not in os.environ]
                    self.assertEqual(sorted(gone), ["CUDA_PATH_V12_8", "CUDA_PATH_V13_0",
                                                    "HIP_PATH", "NVTOOLSEXT_PATH",
                                                    "PYTHONUSERBASE"])
                    self.assertEqual(os.environ["CUDA_PATH"], root)   # read by no library here
                    # PyTorch's defaults under ProgramFiles: the real folder,
                    # from Windows' known-folder lookup, not the scratch one.
                    real = update._program_files()
                    self.assertNotEqual(os.path.normcase(real), os.path.normcase(root))
                    self.assertEqual(os.environ["ProgramFiles"], real)
                    self.assertEqual(os.environ["ProgramW6432"], real)
            finally:
                os.chdir(cwd)

    @unittest.skipUnless(sys.platform == "win32", "Windows known folders")
    def test_a_failed_program_files_lookup_fails_closed(self):
        # #358 (T25): if Windows cannot say where Program Files is, the
        # clearing raises (a worker then stops before any model library is
        # imported, and --self-test fails) and leaves neither ProgramFiles nor
        # ProgramW6432 as it came.
        from workspace import worker

        def broken():
            raise OSError("known folder")
        with tempfile.TemporaryDirectory() as root:
            for lookup in (broken, lambda: None, lambda: "relative"):
                with self.subTest(lookup=lookup), \
                        mock.patch.dict(os.environ, {"ProgramFiles": root,
                                                     "ProgramW6432": root}):
                    with self.assertRaises(OSError):
                        worker.clear_dll_path_environment(program_files=lookup)
                    self.assertNotIn("ProgramFiles", os.environ)
                    self.assertNotIn("ProgramW6432", os.environ)

    def test_a_user_base_sysconfig_has_already_kept_is_cleared(self):
        # #358 review of 3757d8b: sysconfig keeps the config variables it
        # first works out, the user base from PYTHONUSERBASE among them, and
        # PyTorch reads the user base from there. Coverage reads them at
        # start-up in every child process it measures, and other code could
        # too. In a fresh process that has read them first, the clearing still
        # leaves no user base the environment named; if the reset did not
        # take, it raises (fail closed).
        import subprocess
        import textwrap
        code = textwrap.dedent("""
            import os, sysconfig
            from unittest import mock
            sysconfig.get_config_vars()                 # read before the clearing
            before = sysconfig.get_config_var("userbase")
            from workspace import worker
            worker.clear_dll_path_environment()
            print(before)
            print(sysconfig.get_config_var("userbase"))
            os.environ["PYTHONUSERBASE"] = before
            with mock.patch.object(sysconfig, "get_config_var", return_value=before):
                try:
                    worker.clear_dll_path_environment()
                    print("not raised")
                except OSError:
                    print("raised")
            """)
        with tempfile.TemporaryDirectory() as scratch:
            userbase = os.path.join(scratch, "userbase")
            r = subprocess.run([sys.executable, "-B", "-c", code],
                               cwd=str(Path(__file__).resolve().parents[1]),
                               env=dict(os.environ, PYTHONUSERBASE=userbase),
                               capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        before, after, stale = r.stdout.strip().splitlines()[-3:]
        self.assertEqual(before, userbase)               # kept before the clearing
        self.assertNotEqual(after, userbase)
        self.assertEqual(stale, "raised")

    @unittest.skipUnless(sys.platform == "win32", "Windows DLL search and executable lookup")
    def test_an_ffmpeg_put_on_path_after_start_up_is_not_found_by_speaker_identification(self):
        # #358 review of 6df1173: speaker identification loads lazily (D4),
        # possibly long after the worker started. An ffmpeg put since then in
        # a folder still on the worker's PATH is not found when TorchCodec
        # loads: the worker clears again just before.
        import json

        from workspace import audiocrypt, media
        from workspace.worker import diarize
        path, key, context = Path(self.tmp.name) / "a.uma", bytes(range(32)), ("decoded", "m")
        with audiocrypt.AudioWriter(path, key, context, "float32", 1, SR) as w:
            w.write(np.zeros(SR, dtype=np.float32))
        audio = media.AudioRef(path, key, context, SR)
        with tempfile.TemporaryDirectory() as later:
            with mock.patch.dict(os.environ, {
                    "PATH": os.pathsep.join([later, os.environ.get("PATH", "")])}):
                self.h.load(fake("echo"), 1)                 # the worker starts
            Path(later, "ffmpeg.exe").write_bytes(b"")
            turns, _, _, _ = diarize(self.h, fake("dll_folders_diarizer"), audio, None, 1)
        found = json.loads(turns[0][2])
        self.assertNotIn(os.path.abspath(later), found)


class SlowToEnd:
    """A stand-in worker process that ends only after 8 s of waiting for it,
    as a loaded machine's can. join() counts the time it was given instead of
    sleeping."""
    pid, name = 4321, "unminute-worker"

    def __init__(self):
        self.waited, self.exitcode = 0.0, None

    def is_alive(self):
        return self.waited < 8.0

    def terminate(self):
        pass

    def join(self, timeout=None):
        self.waited += 8.0 if timeout is None else timeout
        if not self.is_alive():
            self.exitcode = 1


class NeverEnds:
    """A stand-in worker process that does not end however long it is waited
    for. join() moves a shared clock on by the time it was given instead of
    sleeping."""
    pid, name, exitcode = 4321, "unminute-worker", None

    def __init__(self, clock):
        self.clock = clock

    def is_alive(self):
        return True

    def terminate(self):
        pass

    def join(self, timeout=None):
        self.clock[0] += timeout


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class WorkerEndTests(unittest.TestCase):
    """#278: a worker that has ended, or been told to, leaves nothing open in
    the workspace, so its crash file is collected and its folder can go."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name) / "logs"
        self.log = diag.Log(self.logs, "app")
        self.h = WorkerHandle(self.tmp.name, quiet=True, allow_test_engines=True, log=self.log)

    def tearDown(self):
        try:
            self.h.close()
            join_workers(self)
        finally:
            self.tmp.cleanup()

    def test_the_crash_file_is_closed_before_the_worker_answers_exit(self):
        self.h.load(fake("slow_exit"), 1)
        crash = self.logs / "worker.crash"
        self.assertTrue(crash.exists())                      # faulthandler's file
        self.h._conn.send(("exit",))
        self.assertTrue(self.h._conn.poll(30))
        self.assertEqual(("ok",), self.h._conn.recv())
        self.assertTrue(self.h._proc.is_alive())             # still ending (60 s)
        # What the app does when a worker ends: read the file and delete it.
        self.log.collect_crash(False, crash_path=crash, process="worker")
        self.assertFalse(crash.exists())

    def test_a_worker_told_to_end_is_waited_for_until_it_has(self):
        slow = SlowToEnd()
        self.h._proc = slow
        self.h._kill()
        self.assertFalse(slow.is_alive())              # 5 s was too short (#278)
        self.assertIsNone(self.h._proc)

    def test_a_closed_handle_never_starts_a_worker(self):
        self.h.close()
        self.h.begin_run()                    # a run that began while the app closed
        with self.assertRaises(WorkerStopped):
            self.h.load(fake("echo"), 1)
        self.assertIsNone(self.h._proc)

    def test_a_run_that_began_as_the_handle_closed_starts_no_worker(self):
        self.h.close()
        # begin_run read _closed just before close() set it, then cleared the stop.
        self.h._stop_requested.clear()
        with self.assertRaises(WorkerStopped):
            self.h.load(fake("echo"), 1)
        self.assertIsNone(self.h._proc)

    def test_a_companion_asked_for_after_close_is_closed(self):
        self.h.close()
        companion = self.h.gpu_worker(self.tmp.name)
        self.assertTrue(companion._closed)
        self.assertTrue(companion._stop_requested.is_set())

    def _never_ending_workers(self):
        """This handle and both GPU companions, each with a process that does
        not end; the clock their waits move on."""
        clock = [0.0]
        handles = [self.h, self.h.gpu_worker(self.tmp.name),
                   self.h.whisper_gpu_worker(self.tmp.name, "whisper")]
        for h in handles:
            h._proc = NeverEnds(clock)
        return clock, handles

    def test_close_waits_for_all_its_workers_together(self):
        from workspace import worker
        clock, handles = self._never_ending_workers()
        with mock.patch.object(worker, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            self.h.close()
        self.assertEqual(worker.END_WAIT_S, clock[0])        # not once for each
        self.assertEqual([None] * 3, [h._proc for h in handles])

    def test_stop_now_waits_briefly_for_all_its_workers_together(self):
        # It runs while the app holds its own lock (pause, discard).
        from workspace import worker
        clock, handles = self._never_ending_workers()
        with mock.patch.object(worker, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            self.h.stop_now()
        self.assertEqual(worker.STOP_WAIT_S, clock[0])
        self.assertEqual([None] * 3, [h._proc for h in handles])


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
