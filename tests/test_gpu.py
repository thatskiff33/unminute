# Optional GPU component for speaker identification: detection, verified
# install, refusal of tampered or unsafe downloads, fallback to the CPU, and
# the workspace setting. No real download, CUDA or graphics card is needed.
#   python -m unittest discover -s tests -v

import base64
import hashlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # fake_worker_engines

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))

from workspace import gpu  # noqa: E402


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
        z.writestr(f"{info}/RECORD", "\n".join(rows) + "\n")
    data = buf.getvalue()
    return data, {"name": name, "version": version, "size": len(data),
                  "url": f"https://download-r2.pytorch.org/whl/cu130/{name}.whl",
                  "sha256": hashlib.sha256(data).hexdigest()}


class DetectTests(unittest.TestCase):
    def run_smi(self, stdout, code=0):
        result = subprocess.CompletedProcess([], code, stdout=stdout, stderr="")
        with mock.patch.object(gpu.subprocess, "run", return_value=result):
            return gpu.detect()

    def test_card_and_driver(self):
        info = self.run_smi("NVIDIA RTX 500 Ada Generation Laptop GPU, 595.95, 4094\n")
        self.assertEqual(info, {"name": "NVIDIA RTX 500 Ada Generation Laptop GPU",
                                "driver": "595.95", "memory_mb": 4094, "driver_ok": True})

    def test_old_driver_is_flagged(self):
        self.assertFalse(self.run_smi("NVIDIA T500, 552.12, 4096\n")["driver_ok"])

    def test_no_card_or_no_driver(self):
        self.assertIsNone(self.run_smi("", code=9))
        with mock.patch.object(gpu.subprocess, "run", side_effect=OSError):
            self.assertIsNone(gpu.detect())


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

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def fetch(self, url, dest, size):
        Path(dest).write_bytes(self.blobs[url])

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
        evil, w = make_wheel("torch", "2.13.0+cu130", {"../outside.txt": b"x"})
        self.blobs = {w["url"]: evil}
        with mock.patch.object(gpu, "WHEELS", (w,)):
            with self.assertRaises(gpu.GpuError):
                gpu.install(self.cache, lambda m: None, self.fetch)
        self.assertFalse((self.cache / "gpu" / "outside.txt").exists())

    def test_only_the_pinned_download_host_is_used(self):
        from workspace import setup
        with self.assertRaises(setup.SetupError):
            setup._fetch("https://example.com/torch.whl", self.cache / "x", 10)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class FallbackTests(unittest.TestCase):
    def test_gpu_failure_falls_back_to_the_cpu_and_says_so(self):
        from workspace import speakers
        from workspace.worker import WorkerError

        def fake(handle, model, audio, hint, threads, device="cpu", gpu_site=None):
            if device == "cuda":
                raise WorkerError("GpuUnavailable: graphics card unavailable (old driver)")
            return [(0.0, 1.0, "SPEAKER_00")], [], "cpu", False
        with mock.patch.object(speakers, "worker_diarize", fake):
            turns, _, device, _ = speakers._diarize(None, "pyannote", "a", None, 4,
                                                    "cuda", "site")
        self.assertEqual(turns, [(0.0, 1.0, "SPEAKER_00")])
        self.assertTrue(device.startswith("cpu (graphics card not used"))

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
        self.ws = Workspace(root / "ws", root / "models", start=False, battery=False,
                            speaker_model="test:fake_worker_engines:fake_diarizer")

    def tearDown(self):
        self.ws.close()
        self.tmp.cleanup()

    def test_setting_needs_the_component(self):
        from workspace.app import WorkspaceError
        with self.assertRaises(WorkspaceError):
            self.ws.set_settings({"gpu_speakers": True})

    def test_device_is_the_gpu_only_in_faster_mode_with_the_component(self):
        with mock.patch.object(gpu, "status", return_value=("ready", "")):
            self.ws.set_settings({"gpu_speakers": True})
            self.assertEqual(self.ws._speaker_device()[0], "cpu")       # Quiet
            self.ws.set_settings({"mode": "faster"})
            device, site = self.ws._speaker_device()
            self.assertEqual(device, "cuda")
            self.assertEqual(site, str(gpu.site_dir(self.ws.cache_dir)))
        self.assertEqual(self.ws._speaker_device()[0], "cpu")           # component gone

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
        self.ws.handle.stop_now()                   # a stop reaches the GPU worker too
        self.assertTrue(self.ws.handle._gpu._stop_requested.is_set())


if __name__ == "__main__":
    unittest.main()
