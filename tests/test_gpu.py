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
import keyprotect  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))      # fake_worker_engines

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))

from workspace import asr_engines  # noqa: E402
from workspace import gpu  # noqa: E402


sys.path.append(str(Path(__file__).resolve().parents[1] / "tools"))
import decoder_runtime  # noqa: E402  (tools/decoder_runtime.py)


def setUpModule():
    # #299: the decoder runs in an AppContainer that cannot read this Python:
    # decodes here use a scratch runtime it can read and a throwaway profile.
    decoder_runtime.use()


def tearDownModule():
    decoder_runtime.stop()


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

    def test_damaged_audio_on_the_card_is_not_read_again_on_the_processor(self):
        # Code review of #281: audio that does not open (#111 C3a) is not the
        # card's fault, so the pass fails once, with no processor retry.
        from workspace import speakers
        from workspace.worker import WorkerError
        calls = []

        def fake(handle, model, audio, hint, threads, device="cpu", gpu_site=None):
            calls.append(device)
            raise WorkerError("AudioCryptError: The audio file is damaged (cut short). "
                              "(code U-3F2A1B)")
        handle = mock.Mock()
        with mock.patch.object(speakers, "worker_diarize", fake):
            with self.assertRaises(WorkerError) as e:
                speakers._diarize(handle, "pyannote", "a", None, 4, "cuda", "site")
        self.assertEqual(calls, ["cuda"])
        self.assertNotIn("graphics card", str(e.exception))
        handle.release_gpu.assert_not_called()

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
                            speaker_model="test:fake_worker_engines:fake_diarizer",
                            protector=keyprotect.TestProtector())

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

        from workspace import audiocrypt, media
        from workspace.worker import diarize
        path, key, context = Path(self.tmp.name) / "a.uma", bytes(range(32)), ("decoded", "m")
        with audiocrypt.AudioWriter(path, key, context, "float32", 1, 16000) as w:
            w.write(np.zeros(16000 * 3, dtype=np.float32))
        audio = media.AudioRef(path, key, context, 16000 * 3)
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

    def test_the_speaker_gpu_worker_adds_no_dll_folder_named_by_the_environment(self):
        # #358 (T25): the CUDA build of PyTorch, which pyannote.audio loads in
        # this worker, and TorchCodec add the user base's Library\bin
        # (PYTHONUSERBASE), %NVTOOLSEXT_PATH%\bin\x64, %CUDA_PATH_V*%\bin (no
        # cudart beside torch), their defaults under %ProgramFiles% (which
        # Windows sets for the worker from ProgramW6432) and the folder of an
        # ffmpeg on PATH to the DLL search path. Scratch folders named those
        # ways reach none of it.
        import json

        import fake_worker_engines as fakes
        import numpy as np

        from workspace import audiocrypt, media
        from workspace.worker import diarize
        path, key, context = Path(self.tmp.name) / "a.uma", bytes(range(32)), ("decoded", "m")
        with audiocrypt.AudioWriter(path, key, context, "float32", 1, 16000) as w:
            w.write(np.zeros(16000, dtype=np.float32))
        audio = media.AudioRef(path, key, context, 16000)
        with tempfile.TemporaryDirectory() as root:
            env, planted = fakes.planted_dll_environment(root)
            with mock.patch.dict(os.environ, env):
                here = fakes.torch_dll_folders()       # this process is not cleared
                for name in ("nvtx", "cuda", "pfiles_nvtx", "pfiles_cuda", "ffmpeg"):
                    self.assertIn(planted[name], here)
                turns, _, device, _ = diarize(
                    self.ws.handle, "test:fake_worker_engines:dll_folders_diarizer", audio,
                    None, 1, device="cuda", gpu_site=str(Path(self.tmp.name) / "gpu-site"))
        self.assertEqual(device, "cuda")
        self.assertIsNotNone(self.ws.handle._gpu)          # it ran in the GPU worker
        found = json.loads(turns[0][2])
        self.assertEqual([f for f in found if f.startswith(os.path.abspath(root))], [])
        self.ws.handle.release_gpu()


# -- Whisper on the graphics card (#248) -------------------------------------------------

def make_cublas_wheel(dll=b"\x00" * 64):
    """A stand-in for NVIDIA's cuBLAS wheel: the two DLLs the Whisper worker
    loads (given bytes), a package file and a header that is not unpacked."""
    data, w = make_wheel("nvidia_cublas_cu12", "12.8.4.1", {
        "nvidia/__init__.py": b"", "nvidia/cublas/__init__.py": b"",
        "nvidia/cublas/bin/cublasLt64_12.dll": dll,
        "nvidia/cublas/bin/cublas64_12.dll": dll,
        "nvidia/cublas/include/cublas.h": b"// only for compiling\n"})
    w["url"] = gpu.WHISPER_DOWNLOAD_HOST + "70/61/test/nvidia_cublas_cu12-12.8.4.1.whl"
    return data, w


def install_cublas(test, cache, dll=b"\x00" * 64):
    """Install the stand-in Whisper component into cache with the app's own
    install code; WHISPER_WHEELS stays pinned to it for the rest of the test."""
    data, w = make_cublas_wheel(dll)
    patch = mock.patch.object(gpu, "WHISPER_WHEELS", (w,))
    patch.start()
    test.addCleanup(patch.stop)
    test.addCleanup(gpu._full_checks.clear)
    gpu.WHISPER.install(cache, lambda m: None, lambda url, dest, size: Path(dest).write_bytes(
        {w["url"]: data}[url]))
    return gpu.WHISPER.site_dir(cache)


def loadable_dll():
    """Bytes of a harmless DLL Windows can load (Python's own python3.dll),
    to stand in for cuBLAS where a test loads it in a real worker."""
    path = Path(sys.base_prefix) / "python3.dll"
    if not path.is_file():
        raise unittest.SkipTest("python3.dll not found beside Python")
    return path.read_bytes()


CARD = {"name": "NVIDIA test", "driver": "595.95", "memory_mb": 4096, "driver_ok": True}


class WhisperPinTests(unittest.TestCase):
    def test_the_component_is_pinned_to_its_own_https_location(self):
        self.assertTrue(gpu.WHISPER_DOWNLOAD_HOST.startswith("https://files.pythonhosted.org/"))
        for w in gpu.WHISPER_WHEELS:
            self.assertTrue(w["url"].startswith(gpu.WHISPER_DOWNLOAD_HOST), w["url"])
            self.assertRegex(w["sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(w["record_sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(w["size"], 0)
        self.assertEqual(gpu.WHISPER.download_bytes, gpu.WHISPER_DOWNLOAD_BYTES)
        # Removal finds a component's folders by its version prefix: neither
        # component's version may start with the other's.
        self.assertFalse(gpu.WHISPER_VERSION.startswith(gpu.VERSION))
        self.assertFalse(gpu.VERSION.startswith(gpu.WHISPER_VERSION))
        self.assertEqual(gpu.component(gpu.WHISPER_COMPONENT), gpu.WHISPER)
        self.assertIsNone(gpu.component("../gpu-whisper"))
        self.assertIsNone(gpu.component(None))

    def test_only_whisper_models_run_on_the_card_at_a_measured_precision(self):
        from workspace import asr_engines
        self.assertTrue(set(gpu.WHISPER_PRECISION) <= set(asr_engines.WHISPER_MODELS))
        self.assertTrue(set(gpu.WHISPER_PRECISION.values()) <= {"float16", "int8_float16"})
        self.assertFalse(any(asr_engines.is_parakeet(n) for n in gpu.WHISPER_PRECISION))

    def test_the_dlls_loaded_are_in_the_bin_folder(self):
        for name in gpu.WHISPER_DLLS:
            self.assertRegex(name, r"^cublas(Lt)?64_12\.dll$")
        self.assertEqual(gpu.WHISPER_DLLS[0], "cublasLt64_12.dll")    # cublas64_12 imports it


class WhisperComponentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)

    def test_installs_in_its_own_folder_beside_the_speaker_component(self):
        self.assertEqual(gpu.WHISPER.status(self.cache)[0], "missing")
        site = install_cublas(self, self.cache)
        self.assertEqual(gpu.WHISPER.status(self.cache)[0], "ready")
        self.assertEqual(gpu.WHISPER.check(self.cache, full=True), [])
        self.assertTrue((site / "nvidia" / "cublas" / "bin" / "cublas64_12.dll").is_file())
        self.assertFalse((site / "nvidia" / "cublas" / "include").exists())   # not needed
        self.assertNotEqual(gpu.WHISPER.component_dir(self.cache), gpu.component_dir(self.cache))
        self.assertEqual(gpu.status(self.cache)[0], "missing")      # the speaker one: not touched

    def test_an_added_dll_is_found_by_the_quick_check(self):
        site = install_cublas(self, self.cache)
        (site / "nvidia" / "cublas" / "bin" / "cudnn64_9.dll").write_bytes(b"MZ")
        self.assertIn("nvidia/cublas/bin/cudnn64_9.dll: not part of the component",
                      gpu.WHISPER.check(self.cache))
        self.assertEqual(gpu.WHISPER.status(self.cache)[0], "invalid")

    def test_a_changed_dll_stops_use_once_the_full_check_finds_it(self):
        site = install_cublas(self, self.cache)
        gpu._full_checks.clear()
        dll = site / "nvidia" / "cublas" / "bin" / "cublas64_12.dll"
        dll.write_bytes(b"\x01" * 64)                             # same size
        self.assertEqual(gpu.WHISPER.check(self.cache), [])      # the quick check misses it
        self.assertFalse(gpu.WHISPER.verified_for_use(self.cache))   # the first use's full check
        self.assertEqual(gpu.WHISPER.status(self.cache)[0], "invalid")
        self.assertEqual(gpu.status(self.cache)[0], "missing")    # nothing shared with it

    def test_a_download_that_does_not_match_installs_nothing(self):
        data, w = make_cublas_wheel()
        with mock.patch.object(gpu, "WHISPER_WHEELS", (w,)):
            with self.assertRaises(gpu.GpuError):
                gpu.WHISPER.install(self.cache, lambda m: None,
                                    lambda url, dest, size: Path(dest).write_bytes(b"other"))
        self.assertEqual(gpu.WHISPER.status(self.cache)[0], "missing")
        self.assertEqual(list((self.cache / "gpu").iterdir()), [])

    def test_removal_takes_only_its_own_folders(self):
        from workspace import setup
        install_cublas(self, self.cache)
        torch, w1 = make_wheel("torch", "2.13.0+cu130", {"torch/__init__.py": b""})
        audio, w2 = make_wheel("torchaudio", "2.11.0+cu130", {"torchaudio/__init__.py": b""})
        blobs = {w1["url"]: torch, w2["url"]: audio}
        with mock.patch.object(gpu, "WHEELS", (w1, w2)):
            gpu.install(self.cache, lambda m: None,
                        lambda url, dest, size: Path(dest).write_bytes(blobs[url]))
            self.assertEqual(setup.removal_targets(gpu.WHISPER_COMPONENT, self.cache),
                             [gpu.WHISPER.component_dir(self.cache)])
            setup.remove(gpu.WHISPER_COMPONENT, self.cache)
            self.assertEqual(gpu.WHISPER.status(self.cache)[0], "missing")
            self.assertEqual(gpu.status(self.cache)[0], "ready")


class WhisperActivateTests(unittest.TestCase):
    def test_only_the_components_cublas_is_loaded_by_full_path(self):
        # Nothing goes on the import path, and the DLLs are loaded by full path
        # (cuBLAS Lt first), so Windows uses them when CTranslate2 asks by name.
        calls, site = [], str(Path("C:/cache/gpu/cublas/site"))
        folder = os.path.join(site, "nvidia", "cublas", "bin")
        with mock.patch.object(os, "add_dll_directory", create=True,
                               side_effect=lambda p: calls.append(("folder", p))), \
                mock.patch("ctypes.WinDLL", create=True,
                           side_effect=lambda p: calls.append(("dll", p))), \
                mock.patch.object(gpu, "restrict_dll_search",
                                  side_effect=lambda: calls.append(("restrict",))), \
                mock.patch.object(sys, "path", list(sys.path)):
            before = list(sys.path)
            gpu.activate_whisper(site)
            self.assertEqual(sys.path, before)
        # The search order is restricted before anything is loaded (T25).
        self.assertEqual(calls, [("restrict",), ("folder", folder),
                                 ("dll", os.path.join(folder, "cublasLt64_12.dll")),
                                 ("dll", os.path.join(folder, "cublas64_12.dll"))])


    @unittest.skipUnless(os.name == "nt", "the Windows DLL search order")
    def test_a_library_on_path_or_in_the_current_folder_is_never_loaded_by_name(self):
        # cuBLASLt names nvrtc64_120_0.dll; a file of that kind on PATH or in
        # the current folder must not load into the Whisper GPU worker once
        # the search order is restricted (#263 review). The control run shows
        # the probe would load without the restriction.
        probe = ("import ctypes, sys\n"
                 "sys.path.insert(0, sys.argv[1])\n"
                 "from workspace import gpu\n"
                 "if sys.argv[2] == 'restrict':\n"
                 "    gpu.restrict_dll_search()\n"
                 "k32 = ctypes.WinDLL('kernel32', use_last_error=True)\n"
                 "k32.LoadLibraryW.restype = ctypes.c_void_p\n"
                 "k32.LoadLibraryW.argtypes = [ctypes.c_wchar_p]\n"
                 "for name in sys.argv[3:]:\n"
                 "    print(name, bool(k32.LoadLibraryW(name)))\n")
        dll = loadable_dll()
        repo = str(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as tmp:
            on_path, here = Path(tmp, "on-path"), Path(tmp, "current")
            on_path.mkdir()
            here.mkdir()
            (on_path / "unminute_path_probe.dll").write_bytes(dll)
            (here / "unminute_cwd_probe.dll").write_bytes(dll)
            env = dict(os.environ, PATH=str(on_path) + os.pathsep + os.environ["PATH"])
            results = {}
            for mode in ("control", "restrict"):
                out = subprocess.run(
                    [sys.executable, "-I", "-B", "-c", probe, repo, mode,
                     "unminute_path_probe.dll", "unminute_cwd_probe.dll"],
                    cwd=here, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(out.returncode, 0, out.stderr[-500:])
                results[mode] = out.stdout.split()
        self.assertEqual(results["control"], ["unminute_path_probe.dll", "True",
                                              "unminute_cwd_probe.dll", "True"])
        self.assertEqual(results["restrict"], ["unminute_path_probe.dll", "False",
                                               "unminute_cwd_probe.dll", "False"])


class WhisperGpuLoadTests(unittest.TestCase):
    """_load_whisper_gpu with CTranslate2 and faster_whisper stood in for, as
    a card below compute capability 7.0 behaves (#263 review, D4)."""

    def load(self, supported, refuse=None, model=asr_engines.WHISPER_TURBO):
        import types

        from workspace import engines
        made = []

        def whisper_model(folder, device, compute_type, cpu_threads):
            made.append((device, compute_type))
            if refuse is not None:
                raise refuse
            return mock.Mock()

        ct2 = types.SimpleNamespace(get_cuda_device_count=lambda: 1,
                                    get_supported_compute_types=lambda d: set(supported))
        fw = types.SimpleNamespace(WhisperModel=whisper_model)
        with mock.patch.dict(sys.modules, {"ctranslate2": ct2, "faster_whisper": fw}), \
                mock.patch.object(asr_engines, "find_verified", return_value=Path("m")), \
                mock.patch.object(asr_engines, "feature_width_problem", return_value=None):
            try:
                engine = engines.load_engine(model, "cache", 1,
                                             device="cuda")
            except Exception as exc:          # what the worker would send back
                return exc, made
        return engine, made

    def worker_error(self, exc):
        from workspace.worker import EngineUnavailable, safe_message
        return EngineUnavailable(f"{type(exc).__name__}: {safe_message(exc)} (code ab12)")

    def test_a_precision_the_card_cannot_run_falls_back_before_loading(self):
        from fake_worker_engines import PRECISION_REFUSED

        from workspace.engines import GpuUnavailable
        from workspace.worker import gpu_failure
        exc, made = self.load({"int8", "int8_float32", "float32"},
                              refuse=ValueError(PRECISION_REFUSED))
        self.assertIsInstance(exc, GpuUnavailable)
        self.assertIn("cannot run Whisper", str(exc))
        self.assertEqual(made, [])                            # never tried on the card
        self.assertTrue(gpu_failure(self.worker_error(exc)))

    def test_a_card_that_refuses_the_model_at_load_falls_back(self):
        # The precision is listed, but CTranslate2 still refuses at load.
        from fake_worker_engines import PRECISION_REFUSED

        from workspace.engines import GpuUnavailable
        from workspace.worker import gpu_failure
        exc, made = self.load({"float16", "int8_float16"}, refuse=ValueError(PRECISION_REFUSED))
        self.assertIsInstance(exc, GpuUnavailable)
        self.assertIn(PRECISION_REFUSED, str(exc))
        self.assertEqual(made, [("cuda", "float16")])
        self.assertTrue(gpu_failure(self.worker_error(exc)))

    def test_a_card_that_runs_the_precision_loads_on_it(self):
        engine, made = self.load({"float16", "int8_float16", "int8"})
        self.assertEqual((engine.device, engine.precision), ("cuda", "float16"))
        self.assertEqual(made, [("cuda", "float16")])

    # Large-v3's pinned precision on the card is int8_float16, not turbo's float16.
    def test_large_v3_is_refused_on_a_card_without_int8_float16(self):
        from workspace.engines import GpuUnavailable
        exc, made = self.load({"float16", "int8", "float32"}, model=asr_engines.WHISPER_LARGE)
        self.assertIsInstance(exc, GpuUnavailable)
        self.assertEqual(made, [])                            # never tried on the card

    def test_large_v3_loads_on_the_card_at_int8_float16(self):
        engine, made = self.load({"float16", "int8_float16", "int8"},
                                 model=asr_engines.WHISPER_LARGE)
        self.assertEqual((engine.device, engine.precision), ("cuda", "int8_float16"))
        self.assertEqual(made, [("cuda", "int8_float16")])


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class WhisperFallbackTests(unittest.TestCase):
    AUDIO = [0.0] * 16000

    def engine(self, gpu_effect):
        from workspace.worker import GpuWhisperEngine
        handle = mock.Mock()
        handle.load.return_value = ("Whisper (processor)", "cpu", "int8")
        handle.transcribe.return_value = (["processor"], 1.0)
        companion = handle.whisper_gpu_worker.return_value
        companion.load.return_value = ("Whisper (card)", "cuda", "float16")
        companion.transcribe.side_effect = gpu_effect
        engine = GpuWhisperEngine(handle, "large-v3-turbo", 4, "site", gpu.WHISPER_COMPONENT)
        return engine, handle, companion

    def test_the_card_runs_after_the_other_models_are_released(self):
        engine, handle, companion = self.engine(lambda audio, timeout: (["card"], 1.0))
        self.assertEqual(engine.transcribe(self.AUDIO), (["card"], 1.0))
        self.assertEqual((engine.device, engine.precision), ("cuda", "float16"))
        names = [c[0] for c in handle.mock_calls]
        self.assertLess(names.index("shutdown_for_whisper_gpu"),                     # D4
                        names.index("whisper_gpu_worker"))
        handle.whisper_gpu_worker.assert_called_once_with("site", gpu.WHISPER_COMPONENT)
        handle.load.assert_not_called()
        self.assertGreaterEqual(companion.transcribe.call_args.kwargs["timeout"], 300)

    def test_whisper_gpu_failure_falls_back_to_the_cpu_and_says_so(self):
        from workspace.worker import WorkerError
        engine, handle, companion = self.engine(
            WorkerError("RuntimeError: CUDA failed with error out of memory (code ab12)"))
        self.assertEqual(engine.transcribe(self.AUDIO), (["processor"], 1.0))   # same part
        self.assertTrue(engine.device.startswith("cpu (graphics card not used: CUDA failed "
                                                 "with error out of memory"), engine.device)
        self.assertEqual(engine.precision, "int8")                # what the processor used
        handle.release_gpu.assert_called_once_with("whisper")     # its memory is freed
        engine.transcribe(self.AUDIO)                             # the rest stays on the CPU
        self.assertEqual(companion.transcribe.call_count, 1)
        self.assertEqual(handle.transcribe.call_count, 2)

    def test_any_load_failure_on_the_card_but_the_models_own_falls_back(self):
        # CTranslate2's refusal names no CUDA word; it is still the card's (D4).
        from fake_worker_engines import PRECISION_REFUSED

        from workspace.worker import EngineUnavailable
        engine, handle, companion = self.engine(lambda audio, timeout: (["card"], 1.0))
        companion.load.side_effect = EngineUnavailable(f"ValueError: {PRECISION_REFUSED} "
                                                       f"(code ab12)")
        self.assertEqual(engine.transcribe(self.AUDIO), (["processor"], 1.0))
        self.assertTrue(engine.device.startswith(
            "cpu (graphics card not used: Requested float16 compute type"), engine.device)
        handle.release_gpu.assert_called_once_with("whisper")

    def test_a_model_that_fails_its_check_on_the_card_is_not_loaded_twice(self):
        from workspace.worker import EngineUnavailable
        engine, handle, companion = self.engine(lambda audio, timeout: (["card"], 1.0))
        companion.load.side_effect = EngineUnavailable(
            "ModelLoadError: Whisper large-v3 turbo is not installed or failed verification.")
        with self.assertRaises(EngineUnavailable):
            engine.transcribe(self.AUDIO)
        handle.load.assert_not_called()

    def test_a_crashed_or_hung_card_worker_falls_back(self):
        from workspace.worker import WorkerCrashed
        engine, handle, _ = self.engine(WorkerCrashed("The worker stopped responding."))
        engine.transcribe(self.AUDIO)
        self.assertTrue(engine.device.startswith("cpu (graphics card not used"))

    def test_a_problem_that_is_not_the_cards_is_not_run_twice(self):
        from workspace.worker import WorkerError
        engine, handle, _ = self.engine(
            WorkerError("ModelLoadError: Whisper large-v3 turbo failed verification"))
        with self.assertRaises(WorkerError):
            engine.transcribe(self.AUDIO)
        handle.transcribe.assert_not_called()

    def test_a_deliberate_stop_is_not_retried_on_the_processor(self):
        from workspace.worker import WorkerStopped
        engine, handle, _ = self.engine(WorkerStopped("Stopped."))
        with self.assertRaises(WorkerStopped):
            engine.transcribe(self.AUDIO)
        handle.transcribe.assert_not_called()

    def test_a_processor_failure_after_fallback_keeps_the_reason(self):
        from workspace.worker import WorkerCrashed, WorkerError
        engine, handle, _ = self.engine(WorkerCrashed("The worker ended unexpectedly."))
        handle.transcribe.side_effect = WorkerError("MemoryError: not enough memory")
        with self.assertRaises(WorkerError) as e:
            engine.transcribe(self.AUDIO)
        self.assertIn("after the graphics card could not be used", str(e.exception))

    def test_speaker_identification_first_ends_whisper_on_the_card(self):
        # One heavy model at a time: the Whisper GPU worker holds its model.
        from workspace import media
        from workspace.worker import diarize
        with tempfile.TemporaryDirectory() as tmp:
            audio = media.AudioRef(Path(tmp, "a.uma"), bytes(32), ("decoded", "m"), 16000)
            handle = mock.MagicMock()
            handle._request.return_value = ("ok", [], [], "cpu", False)
            diarize(handle, "pyannote", audio, None, 2)
        names = [c[0] for c in handle.mock_calls]
        self.assertLess(names.index("shutdown_whisper_gpu"), names.index("_request"))


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
@unittest.skipUnless(os.name == "nt", "imports use Windows path rules (the app is Windows-only)")
class WorkspaceWhisperGpuTests(unittest.TestCase):
    def setUp(self):
        import wave

        import numpy as np

        from workspace.app import Workspace
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ws = Workspace(self.root / "ws", self.root / "models", start=False,
                            allow_test_engines=True, battery=False,
                            speaker_model="test:fake_worker_engines:fake_diarizer",
                            protector=keyprotect.TestProtector())
        self.addCleanup(self.ws.close)
        pcm = (np.random.default_rng(1).standard_normal(100 * 16000) * 300).astype("<i2")
        self.rec = self.root / "synthetic-standup.wav"
        with wave.open(str(self.rec), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm.tobytes())
        detected = mock.patch.object(gpu, "detected", return_value=CARD)
        detected.start()
        self.addCleanup(detected.stop)

    def offer(self, model):
        """A test engine counts as a Whisper model offered on the card."""
        patch = mock.patch.dict(gpu.WHISPER_PRECISION, {model: "float16"})
        patch.start()
        self.addCleanup(patch.stop)
        return model

    def transcribe(self, engine):
        model = self.offer(f"test:fake_worker_engines:{engine}")
        self.ws.settings.update(model=model, speakers=False)
        self.ws.set_settings({"gpu_speakers": True, "mode": "faster"})
        self.ws.start()
        self.assertEqual(self.ws.import_paths([str(self.rec)])["accepted"], 1)
        self.assertTrue(self.ws.wait_idle(120))
        [m] = self.ws.meetings()
        return m["id"]

    def test_the_card_is_used_only_when_everything_allows_it(self):
        model = self.offer("test:fake_worker_engines:gpu_sentences")
        cpu = ("cpu", None)
        self.ws.settings.update(gpu_speakers=True, mode="quiet")
        self.assertEqual(self.ws._whisper_device(model), cpu)                  # Quiet
        self.ws.settings.update(mode="faster")
        self.assertEqual(self.ws._whisper_device(model), cpu)                  # not downloaded
        site = install_cublas(self, self.ws.cache_dir)
        self.assertEqual(self.ws._whisper_device(model), ("cuda", str(site)))
        self.assertEqual(self.ws._whisper_device("parakeet-tdt-0.6b-v2"), cpu)  # not offered
        self.ws.downloads._running = gpu.WHISPER_COMPONENT                     # being replaced
        self.assertEqual(self.ws._whisper_device(model), cpu)
        self.ws.downloads._running = None
        self.ws.settings.update(gpu_speakers=False)
        self.assertEqual(self.ws._whisper_device(model), cpu)                  # switched off
        self.ws.settings.update(gpu_speakers=True)
        (site / "nvidia" / "cublas" / "bin" / "cublas64_12.dll").write_bytes(b"\x01" * 64)
        self.assertTrue(gpu.WHISPER.full_check(self.ws.cache_dir))             # Check every file
        device, site_used = self.ws._whisper_device(model)
        self.assertTrue(device.startswith("cpu (graphics card not used: the graphics card "
                                          "component for Whisper failed its check"), device)
        self.assertIsNone(site_used)

    def test_the_switch_needs_only_one_component(self):
        install_cublas(self, self.ws.cache_dir)
        self.ws.set_settings({"gpu_speakers": True, "mode": "faster"})       # Whisper only
        # Speaker identification then runs on the processor without a failure note.
        self.assertEqual(self.ws._speaker_device(), ("cpu", None))
        cards = {c["name"]: c for c in self.ws.setup_state()["cards"]}
        card = cards[gpu.WHISPER_COMPONENT]
        self.assertEqual((card["role"], card["state"], card["removable"]), ("gpu", "ready", True))
        self.assertEqual(self.ws.get_settings()["gpu"]["whisper_component"], "ready")

    def test_a_meeting_transcribes_on_the_card_in_its_own_worker(self):
        install_cublas(self, self.ws.cache_dir, loadable_dll())
        mid = self.transcribe("gpu_sentences")
        d = self.ws.detail(mid)
        parts = len(self.ws.store.chunks(self.ws.store.active_version(mid)["id"]))
        self.assertEqual(d["ran_on"], [{"where": "gpu", "precision": "float16", "parts": parts,
                                        "fallback": False}])
        self.assertEqual(self.ws.handle._gpu_whisper.gpu_component, gpu.WHISPER_COMPONENT)
        self.assertIsNone(self.ws.handle._gpu)          # the speaker GPU worker never started
        self.assertFalse(self.ws._gpu_whisper_run)      # cleared when the run ended

    def test_out_of_graphics_memory_falls_back_without_losing_committed_parts(self):
        install_cublas(self, self.ws.cache_dir, loadable_dll())
        mid = self.transcribe("gpu_oom_after_first")
        vid = self.ws.store.active_version(mid)["id"]
        chunks = self.ws.store.chunks(vid)
        self.assertTrue(all(c["status"] == "done" and c["failures"] == 0 for c in chunks))
        self.assertEqual(self.ws.detail(mid)["ran_on"], [
            {"where": "gpu", "precision": "float16", "parts": 1, "fallback": False},
            {"where": "cpu", "precision": "int8", "parts": len(chunks) - 1, "fallback": True}])
        self.assertIsNone(self.ws.handle._gpu_whisper)  # the failed GPU worker was stopped
        self.assertEqual(self.ws.meetings()[0]["stage"], "review")

    def test_a_card_that_cannot_run_the_precision_falls_back_to_the_processor(self):
        # A real Whisper GPU worker whose load on the card fails with
        # CTranslate2's ValueError (a card below compute capability 7.0).
        install_cublas(self, self.ws.cache_dir, loadable_dll())
        mid = self.transcribe("gpu_precision_refused")
        chunks = self.ws.store.chunks(self.ws.store.active_version(mid)["id"])
        self.assertTrue(all(c["status"] == "done" and c["failures"] == 0 for c in chunks))
        self.assertEqual(self.ws.detail(mid)["ran_on"], [
            {"where": "cpu", "precision": "int8", "parts": len(chunks), "fallback": True}])
        self.assertEqual(self.ws.meetings()[0]["stage"], "review")

    def test_the_whisper_worker_loads_only_its_own_cublas(self):
        import json

        import numpy as np
        site = install_cublas(self, self.ws.cache_dir, loadable_dll())
        worker = self.ws.handle.whisper_gpu_worker(str(site), gpu.WHISPER_COMPONENT)
        _, device, precision = worker.load("test:fake_worker_engines:gpu_probe", 1)
        self.assertEqual((device, precision), ("cuda", "float16"))
        [seg], _ = worker.transcribe(np.zeros(16000, dtype=np.float32))
        report = json.loads(seg.text.split(" ", 1)[1])
        self.assertTrue(os.path.samefile(
            report["cublas"], site / "nvidia" / "cublas" / "bin" / "cublas64_12.dll"))
        self.assertNotEqual(os.path.normcase(report["path0"]), os.path.normcase(str(site)))
        self.assertNotEqual(report["pid"], self.ws.handle.pid)

    def test_the_whisper_worker_adds_no_dll_folder_named_by_hip_path(self):
        # #357 (T25): CTranslate2 4.8.3 adds %HIP_PATH%\bin to the DLL search
        # path when it loads, and in this worker that path is all Windows
        # searches by name (SetDefaultDllDirectories). A HIP_PATH the worker
        # inherits, here a scratch folder, is removed before then.
        import numpy as np
        site = install_cublas(self, self.ws.cache_dir, loadable_dll())
        worker = self.ws.handle.whisper_gpu_worker(str(site), gpu.WHISPER_COMPONENT)
        with tempfile.TemporaryDirectory() as hip:
            Path(hip, "bin").mkdir()
            with mock.patch.dict(os.environ, {"HIP_PATH": hip}):
                _, device, _ = worker.load("test:fake_worker_engines:dll_folders", 1)
            [seg], _ = worker.transcribe(np.zeros(16000, dtype=np.float32))
        self.assertEqual(device, "cuda")
        self.assertEqual(seg.text, "folders []")

    def test_removing_one_component_keeps_the_other_and_the_switch(self):
        install_cublas(self, self.ws.cache_dir)
        torch, w1 = make_wheel("torch", "2.13.0+cu130", {"torch/__init__.py": b""})
        audio, w2 = make_wheel("torchaudio", "2.11.0+cu130", {"torchaudio/__init__.py": b""})
        blobs = {w1["url"]: torch, w2["url"]: audio}
        with mock.patch.object(gpu, "WHEELS", (w1, w2)):
            gpu.install(self.ws.cache_dir, lambda m: None,
                        lambda url, dest, size: Path(dest).write_bytes(blobs[url]))
            self.ws.set_settings({"gpu_speakers": True})
            with mock.patch.object(self.ws.handle, "release_gpu") as release:
                self.ws.remove_model(gpu.WHISPER_COMPONENT)
            release.assert_called_once_with("whisper")             # the speaker one keeps going
            self.assertTrue(self.ws.settings["gpu_speakers"])      # still used for speakers
            self.assertEqual(gpu.status(self.ws.cache_dir)[0], "ready")
            self.ws.remove_model(gpu.COMPONENT)
            self.assertFalse(self.ws.settings["gpu_speakers"])     # off with the last one

    def test_a_run_on_the_card_blocks_replacing_only_its_own_component(self):
        from workspace.app import WorkspaceError
        started = []
        self.ws.downloads.start = lambda name, token=None, on_success=None: started.append(name)
        self.ws._busy, self.ws._gpu_whisper_run = ("x" * 32, "transcribe"), True
        try:
            for refused in (lambda: self.ws.download_model(gpu.WHISPER_COMPONENT),
                            lambda: self.ws.verify_model(gpu.WHISPER_COMPONENT)):
                with self.assertRaises(WorkspaceError):
                    refused()
            with self.assertRaises(WorkspaceError) as e:
                self.ws.remove_model(gpu.WHISPER_COMPONENT)
            self.assertIn("Transcription is using the graphics card", str(e.exception))
            self.ws.download_model(gpu.COMPONENT)       # the speaker component is not in use
            self.assertEqual(started, [gpu.COMPONENT])
        finally:
            self.ws._busy, self.ws._gpu_whisper_run = None, False

    def test_a_component_download_stops_only_its_own_worker(self):
        with mock.patch.object(self.ws.handle, "release_gpu") as release:
            self.ws._release_gpu_worker(gpu.WHISPER_COMPONENT)
            self.ws._release_gpu_worker(gpu.COMPONENT)
        self.assertEqual([c.args for c in release.call_args_list], [("whisper",), ("speakers",)])


if __name__ == "__main__":
    unittest.main()
