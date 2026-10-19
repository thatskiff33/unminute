# Optional GPU component for speaker identification: detection, verified
# install, refusal of tampered or unsafe downloads, fallback to the CPU, and
# the workspace setting. No real download, CUDA or graphics card is needed.
#   python -m unittest discover -s tests -v

import base64
import contextlib
import hashlib
import importlib.util
import io
import itertools
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # fake_worker_engines

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))

import asr_engines  # noqa: E402
from workspace import gpu  # noqa: E402


def lock_error(winerror):
    """Windows' error for renaming a folder with a file in it open: access
    denied (5) or in use (32). Set by hand so the test runs on any system."""
    exc = PermissionError(13, "Access is denied")
    exc.winerror = winerror
    return exc


class FakeClock:
    """Stands in for the rename retry's clock and sleep. A pause moves the
    clock on by its length but really waits at most 50 ms, so a test takes no
    30 s and a real virus scan of the test's own new files can still end."""

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        time.sleep(min(seconds, 0.05))


def make_wheel(name, version, files):
    """A minimal wheel: files plus a dist-info RECORD with sha256 and sizes."""
    buf = io.BytesIO()
    info = f"{name}-{version}.dist-info"
    rows = []
    with zipfile.ZipFile(buf, "w") as z:
        for path, data in files.items():
            z.writestr(path, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            rows.append(f"{path},sha256={digest.decode()},{len(data)}")
        rows.append(f"{info}/RECORD,,")
        record = ("\n".join(rows) + "\n").encode()
        z.writestr(f"{info}/RECORD", record)
    data = buf.getvalue()
    return data, {"name": name, "version": version, "size": len(data),
                  "url": f"https://download-r2.pytorch.org/whl/cu130/{name}.whl",
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "record_sha256": hashlib.sha256(record).hexdigest()}


class ActivateTests(unittest.TestCase):
    def test_only_the_component_caches_its_bytecode_although_the_app_runs_with_b(self):
        # The app starts with -B and ships precompiled; the component is unpacked
        # from wheels without .pyc, so torch would be compiled at every start.
        # Every other module keeps -B: the GPU worker once cached the app's own
        # modules, and in a checkout left bytecode behind after the tests (#206).
        from importlib.machinery import SourceFileLoader
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(sys, "dont_write_bytecode", True), \
                mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.object(sys, "path_hooks", list(sys.path_hooks)), \
                mock.patch.dict(sys.path_importer_cache), \
                mock.patch.dict(sys.modules):
            site, app = Path(tmp, "gpu-site"), Path(tmp, "app")
            (site / "zqcomponent").mkdir(parents=True)
            app.mkdir()
            (site / "zqcomponent" / "__init__.py").write_text("from . import part\n")
            (site / "zqcomponent" / "part.py").write_text("VALUE = 2\n")
            (app / "zqapp.py").write_text("VALUE = 1\n")
            gpu.activate(str(site))
            sys.path.insert(1, str(app))
            self.assertEqual(sys.path[0], str(site))
            import zqapp  # noqa: F401
            import zqcomponent
            self.assertEqual(zqcomponent.part.VALUE, 2)
            self.assertTrue(sys.dont_write_bytecode)
            self.assertFalse((app / "__pycache__").exists())
            for source in (site / "zqcomponent" / "__init__.py",
                           site / "zqcomponent" / "part.py"):
                pyc = Path(importlib.util.cache_from_source(str(source)))
                self.assertTrue(pyc.is_file(), pyc.name)
                # Python's own loader takes the cache as valid, without compiling.
                loader = SourceFileLoader("zqcheck", str(source))
                with mock.patch.object(loader, "source_to_code",
                                       side_effect=AssertionError("compiled again")):
                    self.assertIsNotNone(loader.get_code("zqcheck"))

    def isolated_imports(self):
        # The import state activate() changes, restored after the test.
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in (("dont_write_bytecode", True), ("path", list(sys.path)),
                            ("path_hooks", list(sys.path_hooks))):
            stack.enter_context(mock.patch.object(sys, name, value))
        stack.enter_context(mock.patch.dict(sys.path_importer_cache))
        stack.enter_context(mock.patch.dict(sys.modules))
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        site = Path(tmp, "gpu-site")
        site.mkdir()
        return site

    def test_a_cache_that_cannot_be_written_is_no_error(self):
        # Each failure is raised inside the cache write itself (finding on fd22ec6:
        # a __pycache__ file was swallowed by Python's own set_data instead).
        site = self.isolated_imports()
        failures = (OSError("no cache"), NotImplementedError("no cache tag"),
                    ValueError("bad path"))
        for n in range(len(failures)):
            (site / f"zqnocache{n}.py").write_text(f"VALUE = {n}\n")
        gpu.activate(str(site))
        for n, failure in enumerate(failures):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(importlib.util, "cache_from_source",
                                      side_effect=failure) as cache_path:
                module = importlib.import_module(f"zqnocache{n}")
                self.assertEqual(module.VALUE, n)
                cache_path.assert_called_once()
        self.assertFalse((site / "__pycache__").exists())

    def test_the_cache_holds_only_code_compiled_from_the_file(self):
        # Bytes handed to the loader that are not the file's own are never cached,
        # even with the file's size; nor is the code when the file changed while
        # it was compiled (finding on fd22ec6).
        site = self.isolated_imports()
        source = site / "zqsame.py"
        source.write_text("VALUE = 2\n")
        changed = site / "zqchanged.py"
        changed.write_text("VALUE = 3\n")
        gpu.activate(str(site))
        finder = sys.path_hooks[0](str(site))
        spec = finder.find_spec("zqsame")
        spec.loader.source_to_code(b"VALUE = 9\n", str(source))
        self.assertFalse(Path(importlib.util.cache_from_source(str(source))).exists())
        loader = finder.find_spec("zqchanged").loader
        real_get_data, reads = loader.get_data, []

        def get_data(path):
            if path.endswith(".py"):
                reads.append(path)
                if len(reads) > 1:               # an edit after the compile
                    return b"VALUE = 4\n"
            return real_get_data(path)
        with mock.patch.object(loader, "get_data", side_effect=get_data):
            self.assertIsNotNone(loader.get_code("zqchanged"))
        self.assertEqual(len(reads), 2)
        self.assertFalse(Path(importlib.util.cache_from_source(str(changed))).exists())
        import zqsame
        self.assertEqual(zqsame.VALUE, 2)
        self.assertTrue(Path(importlib.util.cache_from_source(str(source))).is_file())

    def test_a_zip_under_the_component_keeps_pythons_own_importer(self):
        site = self.isolated_imports()
        archive = site / "zqextra.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("zqzipped.py", "VALUE = 5\n")
        gpu.activate(str(site))
        with self.assertRaises(ImportError):
            sys.path_hooks[0](str(archive))
        sys.path.append(str(archive))
        import zqzipped
        self.assertEqual(zqzipped.VALUE, 5)


class DetectTests(unittest.TestCase):
    def run_smi(self, stdout, code=0):
        result = subprocess.CompletedProcess([], code, stdout=stdout, stderr="")
        with mock.patch.object(gpu.subprocess, "run", return_value=result), \
                mock.patch.object(gpu, "_nvidia_smi", return_value="nvidia-smi.exe"):
            return gpu.detect()

    def test_card_and_driver(self):
        info = self.run_smi("NVIDIA RTX 500 Ada Generation Laptop GPU, 595.95, 4094\n")
        self.assertEqual(info, {"name": "NVIDIA RTX 500 Ada Generation Laptop GPU",
                                "driver": "595.95", "memory_mb": 4094, "driver_ok": True})

    def test_old_driver_is_flagged(self):
        self.assertFalse(self.run_smi("NVIDIA T500, 552.12, 4096\n")["driver_ok"])

    def test_no_card_or_no_driver(self):
        self.assertIsNone(self.run_smi("", code=9))
        with mock.patch.object(gpu.subprocess, "run", side_effect=OSError), \
                mock.patch.object(gpu, "_nvidia_smi", return_value="nvidia-smi.exe"):
            self.assertIsNone(gpu.detect())
        with mock.patch.object(gpu, "_nvidia_smi", return_value=None):
            self.assertIsNone(gpu.detect())          # no driver installed


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)
        torch, w1 = make_wheel("torch", "2.13.0+cu130", {
            "torch/__init__.py": b"__version__ = '2.13.0+cu130'\n",
            "torch/lib/cudart64_13.dll": b"\x00" * 64,
            "torch/include/huge/header.h": b"// only for compiling\n"})
        audio, w2 = make_wheel("torchaudio", "2.11.0+cu130", {"torchaudio/__init__.py": b""})
        self.blobs = {w1["url"]: torch, w2["url"]: audio}
        self.wheels = (w1, w2)
        self.patch = mock.patch.object(gpu, "WHEELS", self.wheels)
        self.patch.start()
        self.addCleanup(gpu._full_checks.clear)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def fetch(self, url, dest, size):
        Path(dest).write_bytes(self.blobs[url])

    def test_first_gpu_use_in_a_session_runs_the_full_check(self):
        gpu.install(self.cache, lambda m: None, self.fetch)
        gpu._full_checks.clear()
        (gpu.site_dir(self.cache) / "torch" / "deep.pyd").write_bytes(b"MZ")  # not at root
        self.assertEqual(gpu.check(self.cache), [])          # the quick check misses it
        self.assertFalse(gpu.verified_for_use(self.cache))   # the full check does not
        (gpu.site_dir(self.cache) / "torch" / "deep.pyd").unlink()
        self.assertFalse(gpu.verified_for_use(self.cache))   # refused until a check passes
        self.assertEqual(gpu.full_check(self.cache), [])     # Check every file
        self.assertTrue(gpu.verified_for_use(self.cache))

    def test_a_failed_full_check_stops_use_until_one_passes(self):
        # #153: once a GPU run has passed the full check, a same-size change
        # that Check every file finds must stop the next run too.
        gpu.install(self.cache, lambda m: None, self.fetch)
        gpu._full_checks.clear()
        self.assertTrue(gpu.verified_for_use(self.cache))            # full check passes
        dll = gpu.site_dir(self.cache) / "torch" / "lib" / "cudart64_13.dll"
        good = dll.read_bytes()
        dll.write_bytes(b"\x01" * len(good))                         # same size
        self.assertTrue(gpu.verified_for_use(self.cache))            # quick check only
        self.assertTrue(gpu.full_check(self.cache))                  # Check every file
        self.assertFalse(gpu.verified_for_use(self.cache))           # not used again
        self.assertEqual(gpu.status(self.cache)[0], "invalid")       # Repair is offered
        dll.write_bytes(good)
        self.assertFalse(gpu.verified_for_use(self.cache))           # not hashed again per run
        self.assertEqual(gpu.full_check(self.cache), [])             # Check every file passes
        self.assertTrue(gpu.verified_for_use(self.cache))
        self.assertEqual(gpu.status(self.cache)[0], "ready")

    def test_a_repair_starts_afresh(self):
        gpu.install(self.cache, lambda m: None, self.fetch)
        (gpu.site_dir(self.cache) / "torch" / "lib" / "cudart64_13.dll").write_bytes(
            b"\x01" * 64)
        marker = gpu.component_dir(self.cache) / "installed.json"
        st = marker.stat()                   # a second earlier: the repair's is newer
        os.utime(marker, ns=(st.st_atime_ns, st.st_mtime_ns - 10**9))
        self.assertTrue(gpu.full_check(self.cache))
        self.assertEqual(gpu.status(self.cache)[0], "invalid")
        gpu.install(self.cache, lambda m: None, self.fetch)          # Repair
        self.assertEqual(gpu.status(self.cache)[0], "ready")
        self.assertTrue(gpu.verified_for_use(self.cache))

    def test_install_verifies_and_skips_headers(self):
        self.assertEqual(gpu.status(self.cache)[0], "missing")
        gpu.install(self.cache, lambda m: None, self.fetch)
        self.assertEqual(gpu.status(self.cache)[0], "ready")
        self.assertEqual(gpu.check(self.cache, full=True), [])
        site = gpu.site_dir(self.cache)
        self.assertTrue((site / "torch" / "lib" / "cudart64_13.dll").is_file())
        self.assertFalse((site / "torch" / "include").exists())     # not needed to run

    def test_a_changed_file_is_found_by_the_full_check(self):
        gpu.install(self.cache, lambda m: None, self.fetch)
        (gpu.site_dir(self.cache) / "torch" / "lib" / "cudart64_13.dll").write_bytes(
            b"\x01" * 64)
        self.assertEqual(gpu.check(self.cache), [])                  # same size
        self.assertTrue(gpu.check(self.cache, full=True))
        (gpu.site_dir(self.cache) / "torchaudio" / "__init__.py").unlink()
        self.assertEqual(gpu.status(self.cache)[0], "invalid")

    def test_a_download_that_does_not_match_installs_nothing(self):
        self.blobs[self.wheels[0]["url"]] = b"not the published file"
        with self.assertRaises(gpu.GpuError):
            gpu.install(self.cache, lambda m: None, self.fetch)
        self.assertEqual(gpu.status(self.cache)[0], "missing")
        self.assertEqual(list((self.cache / "gpu").iterdir()), [])  # no staging left

    def test_unsafe_paths_in_a_download_are_refused(self):
        for name in ("../outside.txt", "C:/evil.txt", "\\evil.txt", "a\\..\\..\\b.txt"):
            evil, w = make_wheel("torch", "2.13.0+cu130", {name: b"x"})
            self.blobs = {w["url"]: evil}
            with mock.patch.object(gpu, "WHEELS", (w,)):
                with self.assertRaises(gpu.GpuError) as e:
                    gpu.install(self.cache, lambda m: None, self.fetch)
            self.assertIn("unsafe path", str(e.exception), name)

    def test_a_file_changed_with_its_record_line_is_found(self):
        gpu.install(self.cache, lambda m: None, self.fetch)
        site = gpu.site_dir(self.cache)
        (site / "torchaudio" / "__init__.py").write_bytes(b"import evil\n")
        record = site / "torchaudio-2.11.0+cu130.dist-info" / "RECORD"
        digest = base64.urlsafe_b64encode(hashlib.sha256(b"import evil\n").digest())
        record.write_text(f"torchaudio/__init__.py,sha256={digest.rstrip(b'=').decode()},12\n")
        self.assertEqual(gpu.status(self.cache)[0], "invalid")
        self.assertIn("changed", gpu.check(self.cache)[0])

    def test_added_files_that_could_be_loaded_are_found(self):
        gpu.install(self.cache, lambda m: None, self.fetch)
        site = gpu.site_dir(self.cache)
        (site / "numpy.py").write_text("print('shadowed')")
        self.assertIn("numpy.py: not part of the component", gpu.check(self.cache))
        (site / "numpy.py").unlink()
        (site / "torch" / "lib" / "evil.dll").write_bytes(b"MZ")
        self.assertIn("torch/lib/evil.dll: not part of the component", gpu.check(self.cache))
        (site / "torch" / "lib" / "evil.dll").unlink()
        (site / "torch" / "deep").mkdir()
        (site / "torch" / "deep" / "x.py").write_text("")
        self.assertEqual(gpu.check(self.cache), [])               # quick: root and lib
        self.assertTrue(gpu.check(self.cache, full=True))         # full: everywhere

    def test_an_unreadable_file_list_reads_as_damaged_never_crashes(self):
        gpu.install(self.cache, lambda m: None, self.fetch)
        record = gpu.site_dir(self.cache) / "torch-2.13.0+cu130.dist-info" / "RECORD"
        record.write_bytes(b"\xff\xfe not a record")
        self.assertEqual(gpu.status(self.cache)[0], "invalid")
        record.unlink()
        self.assertEqual(gpu.status(self.cache)[0], "invalid")

    def test_repair_keeps_only_the_latest_damaged_copy(self):
        gpu.install(self.cache, lambda m: None, self.fetch)
        for _ in range(3):
            gpu.install(self.cache, lambda m: None, self.fetch)
        kept = list((self.cache / "gpu").glob(gpu.VERSION + ".invalid-*"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(gpu.status(self.cache)[0], "ready")

    def lock_component(self, error, times=None):
        """Refuse renames of the download folder with `error`, `times` times
        (None: always), on the retry's fake clock. Returns (clock, calls):
        calls holds the fake clock's time at each attempt."""
        clock = FakeClock()
        calls = []
        left = [times]
        real = Path.rename
        staging = gpu.staging_dir(self.cache).name

        def rename(path, dest):
            if Path(path).name == staging:
                calls.append(clock.now)
                if left[0] is None or left[0] > 0:
                    if left[0] is not None:
                        left[0] -= 1
                    raise error
            return real(path, dest)

        for patcher in (mock.patch.object(Path, "rename", rename),
                        mock.patch.object(asr_engines, "_lock_clock", clock),
                        mock.patch.object(asr_engines, "_lock_sleep", clock.sleep)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return clock, calls

    def test_a_component_folder_in_use_for_a_moment_is_waited_for(self):
        # #249: the same wait as a model's move into place.
        clock, calls = self.lock_component(lock_error(32), times=2)
        gpu.install(self.cache, lambda m: None, self.fetch)
        self.assertEqual(gpu.status(self.cache)[0], "ready")
        # A real scan of the new files can add an attempt after the two refusals.
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual([b - a for a, b in itertools.pairwise(calls)][:2], [0.25, 0.5])

    def test_a_component_folder_still_in_use_fails_after_30_seconds(self):
        clock, calls = self.lock_component(lock_error(5))
        with self.assertRaises(PermissionError):
            gpu.install(self.cache, lambda m: None, self.fetch)
        self.assertAlmostEqual(calls[-1] - calls[0], asr_engines.LOCK_RETRY_SECONDS)
        self.assertEqual(gpu.status(self.cache)[0], "missing")

    def test_any_other_rename_error_of_the_component_fails_at_once(self):
        clock, calls = self.lock_component(PermissionError(13, "denied"))   # no Windows code
        with self.assertRaises(PermissionError):
            gpu.install(self.cache, lambda m: None, self.fetch)
        self.assertEqual(len(calls), 1)
        self.assertEqual(clock.sleeps, [])

    def test_redirects_leave_the_pinned_location_only_by_failing(self):
        from workspace import setup
        h = setup._SameHost()
        with self.assertRaises(setup.SetupError):
            h.redirect_request(None, None, 302, "Found", {}, "http://download-r2.pytorch.org/x")
        with self.assertRaises(setup.SetupError):
            h.redirect_request(None, None, 302, "Found", {}, "https://example.com/torch.whl")

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_long_paths_for_local_and_network_folders(self):
        self.assertTrue(str(gpu._long(r"C:\x\y")).startswith("\\\\?\\C:\\"))
        self.assertTrue(str(gpu._long(r"\\server\share\x")).startswith(
            "\\\\?\\UNC\\server\\share"))

    def test_only_the_pinned_download_host_is_used(self):
        from workspace import setup
        with self.assertRaises(setup.SetupError):
            setup._fetch("https://example.com/torch.whl", self.cache / "x", 10)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class FallbackTests(unittest.TestCase):
    def test_gpu_failure_falls_back_to_the_cpu_and_says_so(self):
        from workspace import speakers
        from workspace.worker import WorkerError
        handle = mock.Mock()

        def fake(handle, model, audio, hint, threads, device="cpu", gpu_site=None):
            if device == "cuda":
                raise WorkerError("GpuUnavailable: graphics card unavailable (old driver)")
            return [(0.0, 1.0, "SPEAKER_00")], [], "cpu", False
        with mock.patch.object(speakers, "worker_diarize", fake):
            turns, _, device, _ = speakers._diarize(handle, "pyannote", "a", None, 4,
                                                    "cuda", "site")
        self.assertEqual(turns, [(0.0, 1.0, "SPEAKER_00")])
        self.assertTrue(device.startswith("cpu (graphics card not used"))
        handle.release_gpu.assert_called_once()        # the failed GPU worker is stopped

    def test_a_problem_that_is_not_the_gpus_is_not_run_twice(self):
        from workspace import speakers
        from workspace.worker import WorkerError
        calls = []

        def fake(handle, model, audio, hint, threads, device="cpu", gpu_site=None):
            calls.append(device)
            raise WorkerError("ModelLoadError: the speaker model failed verification")
        with mock.patch.object(speakers, "worker_diarize", fake):
            with self.assertRaises(WorkerError):
                speakers._diarize(mock.Mock(), "pyannote", "a", None, 4, "cuda", "site")
        self.assertEqual(calls, ["cuda"])

    def test_a_cpu_failure_after_fallback_keeps_the_gpu_reason(self):
        from workspace import speakers
        from workspace.worker import WorkerCrashed, WorkerError

        def fake(handle, model, audio, hint, threads, device="cpu", gpu_site=None):
            if device == "cuda":
                raise WorkerCrashed("The worker ended unexpectedly.")
            raise WorkerError("MemoryError: not enough memory")
        with mock.patch.object(speakers, "worker_diarize", fake):
            with self.assertRaises(WorkerError) as e:
                speakers._diarize(mock.Mock(), "pyannote", "a", None, 4, "cuda", "site")
        self.assertIn("after the graphics card could not be used", str(e.exception))

    def test_a_deliberate_stop_is_not_retried_on_the_cpu(self):
        from workspace import speakers
        from workspace.worker import WorkerStopped
        calls = []

        def fake(handle, model, audio, hint, threads, device="cpu", gpu_site=None):
            calls.append(device)
            raise WorkerStopped("Stopped.")
        with mock.patch.object(speakers, "worker_diarize", fake):
            with self.assertRaises(WorkerStopped):
                speakers._diarize(None, "pyannote", "a", None, 4, "cuda", "site")
        self.assertEqual(calls, ["cuda"])


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
@unittest.skipUnless(os.name == "nt", "imports use Windows path rules (the app is Windows-only)")
class WorkspaceGpuTests(unittest.TestCase):
    def setUp(self):
        from workspace.app import Workspace
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = Workspace(root / "ws", root / "models", start=False,
                            allow_test_engines=True, battery=False,
                            speaker_model="test:fake_worker_engines:fake_diarizer")

    def tearDown(self):
        self.ws.close()
        self.tmp.cleanup()

    def test_setting_needs_a_card_driver_and_component(self):
        from workspace.app import WorkspaceError
        card = {"name": "NVIDIA test", "driver": "595.95", "memory_mb": 4096, "driver_ok": True}
        with mock.patch.object(gpu, "detected", return_value=card):
            with self.assertRaises(WorkspaceError):
                self.ws.set_settings({"gpu_speakers": True})        # no component
        with mock.patch.object(gpu, "detected", return_value={**card, "driver_ok": False}), \
                mock.patch.object(gpu, "status", return_value=("ready", "")):
            with self.assertRaises(WorkspaceError):
                self.ws.set_settings({"gpu_speakers": True})        # driver too old
        with mock.patch.object(gpu, "detected", return_value=None), \
                mock.patch.object(gpu, "status", return_value=("ready", "")):
            with self.assertRaises(WorkspaceError):
                self.ws.set_settings({"gpu_speakers": True})        # no NVIDIA card
        self.ws.set_settings({"gpu_speakers": False})               # turning off: always

    def test_check_every_file_that_fails_puts_speakers_on_the_processor(self):
        # #153 through the app, with a real (synthetic) component in the
        # workspace's model cache: Check every file fails after a GPU run passed.
        import time
        torch, w1 = make_wheel("torch", "2.13.0+cu130", {
            "torch/__init__.py": b"", "torch/lib/cudart64_13.dll": b"\x00" * 64})
        audio, w2 = make_wheel("torchaudio", "2.11.0+cu130", {"torchaudio/__init__.py": b""})
        blobs = {w1["url"]: torch, w2["url"]: audio}
        card = {"name": "NVIDIA test", "driver": "595.95", "memory_mb": 4096, "driver_ok": True}
        self.addCleanup(gpu._full_checks.clear)
        with mock.patch.object(gpu, "WHEELS", (w1, w2)), \
                mock.patch.object(gpu, "detected", return_value=card):
            gpu.install(self.ws.cache_dir, lambda m: None,
                        lambda url, dest, size: Path(dest).write_bytes(blobs[url]))
            self.ws.set_settings({"gpu_speakers": True, "mode": "faster"})
            self.assertEqual(self.ws._speaker_device()[0], "cuda")     # full check passes
            dll = gpu.site_dir(self.ws.cache_dir) / "torch" / "lib" / "cudart64_13.dll"
            dll.write_bytes(b"\x01" * 64)                             # same size
            self.assertEqual(self.ws._speaker_device()[0], "cuda")     # quick check only
            self.ws.verify_model(gpu.COMPONENT)                        # Check every file
            deadline = time.monotonic() + 10
            while (self.ws.downloads.state[gpu.COMPONENT]["state"] == "running"
                   and time.monotonic() < deadline):
                time.sleep(0.02)
            self.assertEqual(self.ws.downloads.state[gpu.COMPONENT]["state"], "failed")
            device, site = self.ws._speaker_device()
            self.assertTrue(device.startswith("cpu (graphics card not used:"), device)
            self.assertIn("repair it in Downloads", device)
            self.assertIsNone(site)
            cards = {c["name"]: c for c in self.ws.setup_state()["cards"]}
            self.assertEqual(cards[gpu.COMPONENT]["state"], "invalid")  # Repair is offered

    def test_a_refused_card_is_recorded_with_the_reason(self):
        # The pass records why it ran on the processor, which the meeting shows.
        from workspace import speakers
        calls = []

        def fake(handle, model, audio, hint, threads, device="cpu", gpu_site=None):
            calls.append(device)
            return [(0.0, 1.0, "SPEAKER_00")], [], "cpu", False
        note = "cpu (graphics card not used: the graphics card component failed its check)"
        with mock.patch.object(speakers, "worker_diarize", fake):
            _, _, device, _ = speakers._diarize(mock.Mock(), "pyannote", "a", None, 4,
                                                note, None)
            self.assertEqual(device, note)
            _, _, device, _ = speakers._diarize(mock.Mock(), "pyannote", "a", None, 4,
                                                "cpu", None)
            self.assertEqual(device, "cpu")
        self.assertEqual(calls, ["cpu", "cpu"])                # never a GPU attempt

    def test_device_is_the_gpu_only_in_faster_mode_with_the_component(self):
        card = {"name": "NVIDIA test", "driver": "595.95", "memory_mb": 4096, "driver_ok": True}
        with mock.patch.object(gpu, "status", return_value=("ready", "")), \
                mock.patch.object(gpu, "verified_for_use", return_value=True), \
                mock.patch.object(gpu, "detected", return_value=card):
            self.ws.set_settings({"gpu_speakers": True})
            self.assertEqual(self.ws._speaker_device()[0], "cpu")       # Quiet
            self.ws.set_settings({"mode": "faster"})
            device, site = self.ws._speaker_device()
            self.assertEqual(device, "cuda")
            self.assertEqual(site, str(gpu.site_dir(self.ws.cache_dir)))
        device = self.ws._speaker_device()[0]                          # component gone
        self.assertTrue(device.startswith("cpu (graphics card not used: the graphics card "
                                          "component is not downloaded"), device)

    def test_gpu_worker_runs_and_reports_the_device(self):
        import numpy as np

        from workspace.worker import diarize
        audio = Path(self.tmp.name) / "a.f32"
        np.zeros(16000 * 3, dtype="<f4").tofile(audio)
        turns, _, device, _ = diarize(self.ws.handle, "test:fake_worker_engines:fake_diarizer",
                                      audio, None, 2, device="cuda",
                                      gpu_site=str(Path(self.tmp.name) / "gpu-site"))
        self.assertTrue(turns)
        self.assertEqual(device, "cuda")
        self.assertIsNotNone(self.ws.handle._gpu)
        gpu_worker = self.ws.handle._gpu
        self.assertEqual(gpu_worker.gpu_site, str(Path(self.tmp.name) / "gpu-site"))
        self.ws.handle.stop_now()                   # a stop reaches the GPU worker too
        self.assertTrue(gpu_worker._stop_requested.is_set())
        # A stop in force when the GPU worker is fetched is carried over.
        self.assertTrue(self.ws.handle.gpu_worker(gpu_worker.gpu_site)._stop_requested.is_set())
        self.ws.handle.release_gpu()
        self.assertIsNone(self.ws.handle._gpu)


if __name__ == "__main__":
    unittest.main()
