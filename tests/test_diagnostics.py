# Local, content-free diagnostics (R9, #130; docs/RELEASE_STANDARD.md, sections 5
# and 6): records hold fixed words, codes, numbers and reduced stack frames
# only; the logs stay within their cap; crashes in the app and the workers are
# recorded with a code; and a whole lifecycle with marker words (with and
# without spaces) in titles, file names, transcript text, speaker names, a
# token and error messages leaves none of them, and no folder of this machine,
# in any log.
#   python tools/run_tests.py

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workspace import diag  # noqa: E402

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
MARKER = "Zqmarker"          # also in tests/fake_worker_engines.py
SR = 16000


def records(folder):
    out = []
    for path in diag.files(folder):
        out += [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]
    return out


def strings(value):
    """Every string in a decoded record, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from strings(v)


def leaks(folder, *extra):
    """Marker words, this machine's folders or user name found in any log
    (decoded values and raw text), or in extra text (shared output)."""
    words = [MARKER.lower(), Path.home().name.lower(), str(Path.home()).lower(),
             tempfile.gettempdir().lower()]
    found = []
    values = [s.lower() for r in records(folder) for s in strings(r)]
    raw = [p.read_text(encoding="ascii").lower() for p in diag.files(folder)]
    for text in values + raw + [e.lower() for e in extra]:
        found += [w for w in words if w and w in text.replace("\\\\", "\\")]
    return sorted(set(found))


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "logs"
        self.log = diag.Log(self.folder, "app")

    def tearDown(self):
        self.log.stop_crash_capture()
        self.tmp.cleanup()

    def test_each_field_holds_only_what_it_may(self):
        self.log.write("start", version="0.5.0+source", python="3.12.10",
                       title=MARKER, note=f"{MARKER} board meeting", count=3, ratio=0.12345,
                       ok=True, nothing=None, nested={"a": 1})
        self.log.write("free text", x=1)                       # not a kind: written as "?"
        # Single words, token-shaped values, drive paths and bare file names
        # are refused wherever a field has its own rule.
        self.log.write("incident", code="hf_Ab1cD2eF", area="Smith_Board_Minutes.m4a",
                       version="C:/Users/someone/Documents", process=MARKER,
                       thread="C:/x", fatal="C:/Users/x", error="a b",
                       frames=[["C:/Users/someone/a.py", 1, "f"], ["workspace/app.py", 2,
                                                                     "<module>"]])
        first, second, third = records(self.folder)
        self.assertEqual((first["version"], first["python"]), ("0.5.0+source", "3.12.10"))
        self.assertEqual((first["title"], first["note"], first["nested"]), ("?", "?", "?"))
        self.assertEqual((first["count"], first["ratio"], first["ok"], first["nothing"]),
                         (3, 0.123, True, None))
        self.assertEqual(second["kind"], "?")
        for key in ("code", "area", "version", "process", "thread", "fatal", "error"):
            self.assertEqual(third[key], "?", key)
        self.assertEqual(third["frames"], [["?", 1, "f"], ["workspace/app.py", 2, "<module>"]])
        self.assertEqual(leaks(self.folder), [])

    def test_an_incident_keeps_the_type_and_frames_not_the_message(self):
        def inner():
            raise KeyError(f"{MARKER} said something private")
        try:
            inner()
        except KeyError as exc:
            code = self.log.incident("bridge", exc, method="meetings")
        [rec] = records(self.folder)
        self.assertEqual((rec["kind"], rec["code"], rec["area"], rec["error"], rec["method"]),
                         ("incident", code, "bridge", "KeyError", "meetings"))
        self.assertRegex(code, r"^U-[0-9A-F]{6}$")
        self.assertEqual(rec["frames"][-1][::2], ["tests/test_diagnostics.py", "inner"])
        self.assertEqual(leaks(self.folder), [])

    def test_deep_tracebacks_keep_only_the_innermost_frames(self):
        def down(n):
            if n == 0:
                raise RecursionError("deep")
            down(n - 1)
        try:
            down(200)
        except RecursionError as exc:
            self.assertEqual(len(diag.frames(exc.__traceback__)), 12)

    def test_frames_never_name_a_folder_outside_the_app_or_libraries(self):
        home = str(Path.home())
        self.assertEqual(diag.where(str(ROOT / "workspace" / "app.py")), "workspace/app.py")
        self.assertEqual(diag.where(os.path.join(home, "Documents", MARKER, "x.py")), "<other>")
        site = os.path.join(home, "venv", "Lib", "site-packages", "torch", "nn", "module.py")
        self.assertEqual(diag.where(site), "torch/nn/module.py")
        self.assertEqual(diag.where(threading.__file__), "stdlib/threading.py")
        self.assertEqual(diag.where("<frozen importlib._bootstrap>"),
                         "<frozen_importlib._bootstrap>")
        self.assertEqual(diag.where(""), "<other>")
        self.assertEqual(diag.where(r"\\server\share\x.py"), "<other>")

    def test_the_package_layout_is_recognised_from_its_python(self):
        # The embeddable Python: python3XX.zip and lib\\ beside python.exe.
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp).resolve() / "runtime"
            (runtime / "lib" / "numpy").mkdir(parents=True)
            zipped = runtime / f"python{sys.version_info[0]}{sys.version_info[1]}.zip"
            zipped.write_bytes(b"")
            roots = diag._library_roots(prefix=runtime)
            self.assertEqual(roots, [(zipped, "stdlib/"), (runtime / "lib", "")])
            with mock.patch.object(diag, "_ROOTS", roots):
                self.assertEqual(diag.where(str(runtime / "lib" / "numpy" / "__init__.py")),
                                 "numpy/__init__.py")
                self.assertEqual(diag.where(str(zipped / "threading.py")), "stdlib/threading.py")
                self.assertEqual(diag.where(str(runtime / "lib" / "os.py")), "os.py")
                self.assertEqual(diag.where("json\\decoder.py"), "stdlib/json/decoder.py")
            self.assertEqual(diag.where("json\\decoder.py"), "<other>")      # not the package
            # A development install: the standard library folder sysconfig names.
            dev = diag._library_roots(prefix=tmp, stdlib=runtime / "Lib")
            self.assertEqual([label for _, label in dev], ["stdlib/"])

    def test_frames_in_the_installed_package_layout(self):
        # The package: the standard library zipped beside python.exe, the
        # libraries in runtime\lib, the app in app\, and the GPU component's
        # site folder in the model cache.
        temp = Path(tempfile.gettempdir()).resolve()          # as where() sees paths
        base = temp / "Programs" / "WhisperWorkspace" / "versions" / "0.5.0"
        runtime, app = base / "runtime", base / "app"
        gpu = temp / "whisper-models" / "gpu" / "torch-2.13.0-cu130"
        with mock.patch.object(diag, "APP_ROOT", app), \
                mock.patch.object(diag, "_ROOTS", [(runtime / "python312.zip", "stdlib/"),
                                                   (runtime / "lib", "")]):
            self.assertEqual(diag.where(str(runtime / "python312.zip" / "threading.py")),
                             "stdlib/threading.py")
            self.assertEqual(diag.where(str(runtime / "lib" / "numpy" / "__init__.py")),
                             "numpy/__init__.py")
            self.assertEqual(diag.where(str(app / "workspace" / "app.py")), "workspace/app.py")
            self.assertEqual(diag.where(str(gpu / "site" / "torch" / "nn" / "module.py")),
                             "torch/nn/module.py")
            self.assertEqual(diag.where(str(Path.home() / "Documents" / "x.py")), "<other>")

    def test_the_logs_never_exceed_their_cap(self):
        with mock.patch.object(diag, "MAX_BYTES", 2000):
            for i in range(500):
                self.log.write("timing", n=i, page_loaded_s=1.5)
        logs = diag.files(self.folder)
        self.assertEqual(len(logs), diag.KEEP)
        self.assertLessEqual(sum(p.stat().st_size for p in logs), diag.KEEP * 2000)
        newest = [json.loads(line) for line in self.log.path.read_text().splitlines()]
        self.assertEqual(newest[-1]["n"], 499)                  # the newest is kept

    def test_a_rotation_that_fails_deletes_nothing_and_keeps_writing(self):
        with mock.patch.object(diag, "MAX_BYTES", 2000):
            for i in range(80):
                self.log.write("timing", n=i)                   # older files exist now
            before = {p.name: p.read_bytes() for p in diag.files(self.folder)}
            real = Path.replace

            def held(path, target):
                if Path(path).name == "app.log":
                    raise PermissionError("held open by a viewer")
                return real(path, target)
            with mock.patch.object(Path, "replace", held):
                for i in range(80, 140):                        # past the cap: rotations tried
                    self.log.write("timing", n=i)
            after = {p.name: p.read_bytes() for p in diag.files(self.folder)}
        self.assertEqual(set(after), set(before))
        for older in ("app.1.log", "app.2.log"):
            self.assertEqual(after[older], before[older])       # nothing moved or deleted
        last = [json.loads(line)["n"] for line in self.log.path.read_text().splitlines()]
        self.assertEqual(last[-1], 139)                         # still written

    def test_a_crash_from_an_unclean_run_names_the_crash_not_an_earlier_error(self):
        self.assertFalse(self.log.open_run())
        self.log.crash_path.write_text(
            # A native error the process went on from, then the real crash.
            "Windows fatal exception: code 0xe0000001\n\nCurrent thread 0x00000001 (most "
            'recent call first):\n  File "C:\\x\\a.py", line 1 in recovered\n\n'
            "Windows fatal exception: access violation\n\nThread 0x00000002 (most recent call "
            f'first):\n  File "C:\\Users\\{MARKER}\\other.py", line 7 in elsewhere\n\n'
            "Current thread 0x00001234 (most recent call first):\n"
            f'  File "C:\\Users\\{MARKER}\\app\\workspace\\worker.py", line 12 in run\n'
            f'  File "C:\\Users\\{MARKER}\\app\\workspace\\worker.py", line 3 in main\n',
            encoding="utf-8")
        unclean = diag.Log(self.folder, "app").open_run()      # the next start
        self.assertTrue(unclean)
        [crash] = [r for r in records(self.folder) if r["kind"] == "crash"]
        self.assertEqual(crash["fatal"], "Windows_fatal_exception_access_violation")
        self.assertEqual(crash["frames"], [["<other>", 3, "main"], ["<other>", 12, "run"]])
        self.assertEqual(crash["earlier"], 1)
        self.assertRegex(crash["code"], r"^U-")
        self.assertFalse(self.log.crash_path.exists())
        self.assertEqual(leaks(self.folder), [])

    def test_native_errors_something_recovered_from_are_counted_not_crashes(self):
        self.log.open_run()
        self.log.crash_path.write_text("Windows fatal exception: code 0xc0000005\n" * 3,
                                       encoding="utf-8")
        self.log.close_run()                                   # the run ended cleanly
        [rec] = records(self.folder)
        self.assertEqual((rec["kind"], rec["count"]), ("native_error", 3))
        self.assertFalse(diag.Log(self.folder, "app").open_run())

    def test_the_crash_file_stays_small_and_is_never_shared(self):
        self.log.capture_crashes()
        self.log._crash_file.write("Windows fatal exception: code 0xc0000005\n" * 8000)
        self.log._crash_file.flush()
        self.assertNotIn(self.log.crash_path, diag.files(self.folder))
        self.log.limit_crash_file()                            # past CRASH_MAX: counted
        self.assertLess(self.log.crash_path.stat().st_size, 1000)
        [rec] = records(self.folder)
        self.assertEqual((rec["kind"], rec["truncated"]), ("native_error", True))
        self.assertIsNotNone(self.log._crash_file)            # still capturing

    def test_console_output_is_reduced_to_type_names(self):
        import io
        out = diag.OutputSummary(self.log, "stderr")
        self.assertIsInstance(out, io.TextIOBase)              # libraries see a stream
        out.write(f"{MARKER} said a private thing\nUserWarning: {MARKER} was here\n")
        out.write(f"ImportError: DLL load failed for C:\\Users\\{MARKER}\\x.pyd\nUserWar")
        out.write(f"ning: again {MARKER}\nno newline yet {MARKER}")
        # Names only where Python writes them: an exception's last line, a warning.
        out.writelines([f"\nSegment: the {MARKER}Warning project is late\n",
                        "SmithException is late for the board\n",
                        f"Late_Payment_Warning: {MARKER} owes\n",          # not a loaded class
                        "WorkerCrashed: could not read Smith_Tax_Error.m4a\n",
                        "C:\\x\\a.py:12: DeprecationWarning: old call\n",
                        "torch.cuda.OutOfMemoryError: CUDA out of memory\n",
                        "Exception: bare\n"])
        recs = [(r["type"], r["count"]) for r in records(self.folder)]
        self.assertEqual(recs, [("UserWarning", 1), ("ImportError", 1), ("UserWarning", 2),
                                ("DeprecationWarning", 1), ("Exception", 1)])  # no torch here
        self.assertEqual(out.buffer.write(b"bytes"), 5)              # dropped, never kept
        self.assertEqual(leaks(self.folder), [])

    def test_uncaught_errors_on_any_thread_are_recorded(self):
        saved = sys.excepthook, threading.excepthook
        sys.excepthook = lambda *a: None                       # quiet chained hooks
        threading.excepthook = lambda args: None
        try:
            diag.use(self.log)

            def boom():
                raise ValueError(f"{MARKER} in a thread")
            t = threading.Thread(target=boom, name="workspace-processing")
            t.start()
            t.join()
            try:
                raise LookupError(MARKER)                      # main thread, uncaught
            except LookupError as exc:
                sys.excepthook(type(exc), exc, exc.__traceback__)
        finally:
            sys.excepthook, threading.excepthook = saved
            self.log.stop_crash_capture()
            diag.set_current(None)
        found = [(r["area"], r["error"], r.get("thread")) for r in records(self.folder)]
        self.assertEqual(found, [("thread", "ValueError", "workspace-processing"),
                                 ("uncaught", "LookupError", None)])
        self.assertEqual(leaks(self.folder), [])

    def test_a_log_never_recreates_a_removed_workspace(self):
        gone = Path(self.tmp.name) / "removed workspace" / "logs"
        diag.Log(gone, "app").write("start")
        self.assertFalse(gone.parent.exists())


class DownloadStartTests(unittest.TestCase):
    def test_a_download_that_cannot_start_does_not_leave_setup_busy(self):
        from workspace import setup
        with tempfile.TemporaryDirectory() as tmp:
            d = setup.Downloads(Path(tmp) / "models")
            # A real process object (join() refuses one never started), whose
            # start fails, as when Windows cannot create the process.
            never = setup.mp.get_context("spawn").Process(target=print)
            context = mock.Mock()
            context.Pipe.return_value = (mock.Mock(), mock.Mock())
            context.Process.return_value = never
            with mock.patch.object(setup.mp, "get_context", return_value=context), \
                    mock.patch.object(never, "start", side_effect=OSError("no process")):
                d.start("parakeet-tdt-0.6b-v2")
                for _ in range(200):
                    if not d.busy():
                        break
                    threading.Event().wait(0.05)
            self.assertFalse(d.busy())
            self.assertEqual(d.state["parakeet-tdt-0.6b-v2"]["state"], "failed")


@unittest.skipUnless(sys.platform == "win32", "Windows crash handling")
class NativeCrashTests(unittest.TestCase):
    def test_a_native_crash_in_the_app_is_recorded_at_the_next_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "logs"
            folder.mkdir()
            script = ("import sys, faulthandler; sys.path.insert(0, sys.argv[1]); "
                      "from workspace import diag; diag.install(diag.Log(sys.argv[2], 'app')); "
                      "faulthandler._sigsegv()")
            done = subprocess.run([sys.executable, "-c", script, str(ROOT), str(folder)],
                                  capture_output=True, timeout=60)
            self.assertNotEqual(done.returncode, 0)
            self.assertTrue(diag.Log(folder, "app").open_run())     # the next start
            [crash] = [r for r in records(folder) if r["kind"] == "crash"]
            self.assertRegex(crash["code"], r"^U-")
            self.assertTrue(crash["frames"])
            self.assertEqual(leaks(folder), [])

    def test_a_download_process_that_crashes_is_recorded(self):
        sys.path.insert(0, str(ROOT / "tests"))
        import fake_worker_engines

        from workspace import setup
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "logs"
            folder.mkdir()
            log = diag.Log(folder, "app")
            diag.set_current(log)
            try:
                d = setup.Downloads(Path(tmp) / "models",
                                    target=fake_worker_engines.fake_download_crashes)
                d.log_dir = str(folder)
                d.start("parakeet-tdt-0.6b-v2")
                for _ in range(600):
                    if d.state["parakeet-tdt-0.6b-v2"]["state"] != "running" and not d.busy():
                        break
                    threading.Event().wait(0.05)
            finally:
                diag.set_current(None)
            gone = [r for r in records(folder) if r["kind"] == "worker_exit"]
            self.assertEqual([r["process"] for r in gone], ["setup"])
            [crash] = [r for r in records(folder) if r["kind"] == "crash"]
            self.assertEqual((crash["process"], crash["code"]), ("setup", gone[0]["code"]))
            self.assertFalse((folder / "setup.crash").exists())
            self.assertEqual(leaks(folder), [])

    def test_a_worker_that_fails_to_start_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "logs"
            folder.mkdir()
            script = ("import sys; sys.path.insert(0, sys.argv[1]); from unittest import mock; "
                      "import offline_guard; from workspace.worker import _worker_main; "
                      "mock.patch.object(offline_guard, 'install', side_effect=OSError("
                      f"'{MARKER} C:/Users/{MARKER}')).start(); "
                      "_worker_main(None, sys.argv[2], False, None, False, sys.argv[2], 'worker')")
            done = subprocess.run([sys.executable, "-c", script, str(ROOT), str(folder)],
                                  capture_output=True, timeout=60)
            self.assertNotEqual(done.returncode, 0)
            [rec] = [r for r in records(folder) if r.get("area") == "worker_start"]
            self.assertEqual(rec["error"], "OSError")
            self.assertEqual(leaks(folder), [])


def write_wav(path, seconds, seed=1):
    import numpy as np
    rng = np.random.default_rng(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((rng.standard_normal(int(seconds * SR)) * 300).astype("<i2").tobytes())
    return path


def fake(name):
    return f"test:fake_worker_engines:{name}"


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
@unittest.skipUnless(os.name == "nt", "imports use Windows path rules (the app is Windows-only)")
class LifecycleTests(unittest.TestCase):
    """The whole lifecycle, with marker words wherever content goes."""

    def setUp(self):
        from workspace.app import Workspace
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = Workspace(self.root / "ws", self.root / "models", start=False,
                            allow_test_engines=True, speaker_model=fake("fake_diarizer"),
                            battery=False)
        diag.set_current(self.ws.log)

    def tearDown(self):
        diag.set_current(None)
        self.ws.close()
        self.tmp.cleanup()

    def logs(self):
        return self.ws.root / "logs"

    def test_markers_never_reach_the_logs(self):
        from workspace import api, media, speaker_names
        ws = self.ws
        ws.settings.update(model=fake("marker_words"), speakers=True)
        # Single words (no spaces) as well as phrases: a filter that only
        # refused spaces would let these through.
        rec = write_wav(self.root / MARKER / f"{MARKER}.wav", 30)
        ws.start()
        self.assertEqual(ws.import_paths([str(rec)])["accepted"], 1)
        self.assertTrue(ws.wait_idle(180))
        [m] = ws.meetings()
        self.assertEqual(m["title"], MARKER)
        mid = m["id"]
        vid = ws.store.active_version(mid)["id"]
        label = speaker_names.resolve(ws.store, vid)["order"][0]
        ws.rename_speaker(mid, label, MARKER)
        first = ws.transcript(mid)["items"][0]
        ws.edit(mid, first["id"], f"{MARKER} {MARKER}")
        page = api.make_api(ws)
        # Unexpected errors carrying the marker, from methods that take content.
        for method, call in (("rename", lambda: page.rename(mid, MARKER)),
                             ("rename_speaker", lambda: page.rename_speaker(mid, label, MARKER)),
                             ("edit", lambda: page.edit(mid, first["id"], MARKER)),
                             ("search", lambda: page.search(mid, MARKER)),
                             ("download_model", lambda: page.download_model(
                                 "pyannote", f"hf_{MARKER}Ab12Cd34")),
                             ("meetings", page.meetings)):
            with mock.patch.object(ws, method, side_effect=RuntimeError(
                    f"{MARKER} hf_{MARKER}Ab12 C:\\Users\\{MARKER}\\{MARKER}.txt")):
                reply = call()
            self.assertFalse(reply["ok"], method)
            self.assertRegex(reply["error"], r"code U-[0-9A-F]{6}", method)
        # An import failing unexpectedly (a bug, not a bad file).
        other = write_wav(self.root / MARKER / f"{MARKER}2.wav", 5, seed=2)
        with mock.patch.object(media, "import_recording", side_effect=RuntimeError(MARKER)):
            ws.import_paths([str(other)])
            self.assertTrue(ws.wait_idle(60))
        self.assertRegex(ws.status()["import_errors"][-1], r"code U-[0-9A-F]{6}")
        # A worker error carrying the marker; its code reaches the meeting.
        ws.settings.update(model=fake("raises_marker"))
        ws.retranscribe(mid)
        self.assertTrue(ws.wait_idle(120))
        ws.handle.shutdown()
        areas = {r.get("area") for r in records(self.logs())}
        self.assertTrue({"bridge", "worker", "import"} <= areas, areas)
        methods = {r.get("method") for r in records(self.logs()) if r.get("area") == "bridge"}
        self.assertEqual(methods, {"rename", "rename_speaker", "edit", "search",
                                   "download_model", "meetings"})
        self.assertEqual(leaks(self.logs(), *ws.status()["import_errors"]), [])

    def test_a_worker_crash_is_recorded_with_a_code(self):
        from workspace.worker import WorkerCrashed
        handle = self.ws.handle
        handle.load(fake("native_crash"), 1)
        import numpy as np
        with self.assertRaises(WorkerCrashed):
            handle.transcribe(np.zeros(SR, dtype="float32"))
        [gone] = [r for r in records(self.logs()) if r["kind"] == "worker_exit"]
        self.assertEqual(gone["process"], "worker")
        self.assertNotEqual(gone["exitcode"], 0)
        [crash] = [r for r in records(self.logs()) if r["kind"] == "crash"]
        self.assertEqual(crash["code"], gone["code"])
        self.assertEqual(leaks(self.logs()), [])

    def test_a_worker_that_dies_between_requests_is_recorded(self):
        handle = self.ws.handle
        handle.load(fake("crash_after_reply"), 1)
        import numpy as np
        handle.transcribe(np.zeros(SR, dtype="float32"))       # replies, then crashes
        for _ in range(200):
            if handle._proc is not None and not handle._proc.is_alive():
                break
            threading.Event().wait(0.05)
        handle.load(fake("sentences"), 1)                      # the next request
        [gone] = [r for r in records(self.logs()) if r["kind"] == "worker_exit"]
        self.assertNotEqual(gone["exitcode"], 0)
        self.assertTrue([r for r in records(self.logs()) if r["kind"] == "crash"])

    def test_a_processing_error_shows_its_code(self):
        ws = self.ws
        mid = ws.store.create_meeting("Synthetic")
        jobs = iter([("transcribe", mid, None)])

        def next_work():                # the job goes to the processing thread only
            if threading.current_thread().name != "workspace-processing":
                return None
            return next(jobs, None)
        with mock.patch.object(ws, "_next_work", side_effect=next_work), \
                mock.patch.object(ws, "_run", side_effect=RuntimeError(f"{MARKER} text")):
            ws.start()
            ws._wake.set()
            for _ in range(600):
                if ws.store.meeting(mid)["phase"] == "error":
                    break
                threading.Event().wait(0.05)
        error = ws.store.meeting(mid)["last_error"]
        [rec] = [r for r in records(self.logs()) if r.get("area") == "processing"]
        self.assertIn(f"code {rec['code']}", error)
        self.assertEqual(leaks(self.logs()), [])

    def test_bridge_incidents_go_to_this_workspaces_log(self):
        from workspace import api
        from workspace.app import Workspace
        first_root = self.ws.root
        api.make_api(self.ws)
        self.ws.close()
        other = Workspace(self.root / "ws2", self.root / "models", start=False,
                          allow_test_engines=True, battery=False)
        try:
            page = api.make_api(other)
            with mock.patch.object(other, "meetings", side_effect=RuntimeError("x")):
                page.meetings()
            self.assertTrue([r for r in records(other.root / "logs")
                             if r.get("area") == "bridge"])
            self.assertFalse([r for r in records(first_root / "logs")
                              if r.get("area") == "bridge"])
        finally:
            other.close()
            self.ws = Workspace(self.root / "ws", self.root / "models", start=False,
                                allow_test_engines=True, battery=False)


if __name__ == "__main__":
    unittest.main()
