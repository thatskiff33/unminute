# Untrusted recordings (#111 slice A3): decoding in a contained child process,
# FFmpeg limited to the supported demuxers and local files, decoded-length and
# disk-space limits, and the Windows job that contains the decoder.
#   python -m unittest discover -s tests -v

import importlib.util
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))


def tone_wav(path, seconds=1.0, rate=16000):
    import numpy as np
    n = int(seconds * rate)
    data = (0.2 * np.sin(2 * np.pi * 440 * np.arange(n) / rate) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    return path


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class DecoderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_normal_recording_decodes_in_the_child_process(self):
        from workspace import media
        src = tone_wav(self.root / "tone.wav")
        dest = self.root / "audio.f32"
        samples, digest = media.decode_to_file(src, dest)
        self.assertEqual(samples, 16000)
        self.assertEqual(dest.stat().st_size, 16000 * 4)
        self.assertEqual(media.sha256_file(dest), digest)
        self.assertFalse(dest.with_suffix(".partial").exists())

    def test_scripts_and_playlists_disguised_as_recordings_are_refused(self):
        from workspace import media
        target = tone_wav(self.root / "target.wav")
        cases = {
            # FFmpeg's concat demuxer would read another local file.
            "concat.mp3": "ffconcat version 1.0\nfile '"
                          + str(target).replace("\\", "/") + "'\n",
            # An HLS playlist would make FFmpeg fetch a network address.
            "playlist.m4a": "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\n"
                            "http://192.0.2.1/seg.ts\n#EXT-X-ENDLIST\n",
            # A session description would open RTP network streams.
            "session.wav": "v=0\no=- 0 0 IN IP4 192.0.2.1\ns=x\nc=IN IP4 192.0.2.1\n"
                           "t=0 0\nm=audio 5004 RTP/AVP 0\n",
        }
        for name, text in cases.items():
            src = self.root / name
            src.write_text(text, encoding="utf-8")
            dest = self.root / f"{name}.f32"
            with self.assertRaises(media.DecodeError, msg=name) as ctx:
                media.decode_to_file(src, dest)
            self.assertNotIn(str(self.root), str(ctx.exception))   # no paths in messages
            self.assertFalse(dest.exists() or dest.with_suffix(".partial").exists())

    def test_only_the_supported_demuxers_are_allowed(self):
        from workspace import media
        self.assertEqual(media.OPEN_OPTIONS["protocol_whitelist"], "file")
        allowed = set(media.OPEN_OPTIONS["format_whitelist"].split(","))
        for risky in ("hls", "concat", "rtsp", "rtp", "sdp", "dash", "image2", "tee", "data"):
            self.assertNotIn(risky, allowed)

    def test_decoding_stops_at_the_length_limit(self):
        from workspace import media
        src = tone_wav(self.root / "long.wav", seconds=2)
        with self.assertRaises(media.DecodeError) as ctx:
            media._decode(src, self.root / "x.partial", max_samples=8000)
        self.assertIn("longer than 12 hours", str(ctx.exception))
        self.assertFalse((self.root / "x.partial").exists())

    def test_decoding_keeps_free_disk_space(self):
        from workspace import media
        src = tone_wav(self.root / "tone.wav")
        with mock.patch.object(media, "_free_bytes", return_value=10 << 20):
            with self.assertRaises(media.DecodeError) as ctx:
                media._decode(src, self.root / "y.partial")
        self.assertIn("free disk space", str(ctx.exception))
        self.assertFalse((self.root / "y.partial").exists())

    def test_import_failure_message_is_shown_as_written(self):
        from workspace import app, media
        exc = media.DecodeError("The recording is longer than 12 hours.")
        self.assertEqual(app._import_message(exc), "The recording is longer than 12 hours.")


CHILD = r"""
import sys
sys.stdin.readline()                       # wait until the parent has contained us
what = sys.argv[1]
try:
    if what == "spawn":
        import subprocess
        subprocess.run([sys.executable, "-c", "pass"], check=True)
    elif what == "memory":
        block = bytearray(400 << 20)       # more than the 200 MB test limit
    print("ALLOWED", flush=True)
except Exception as exc:
    print("REFUSED", type(exc).__name__, flush=True)
"""


# In a virtual environment sys.executable is a launcher that starts the real
# interpreter as a second process; the job must hold the interpreter itself.
# (multiprocessing, used by the app, starts the base interpreter directly.)
PYTHON = getattr(sys, "_base_executable", sys.executable)


@unittest.skipUnless(sys.platform == "win32", "Windows job objects")
class JobObjectTests(unittest.TestCase):
    def run_contained(self, what):
        from workspace import jobs
        proc = subprocess.Popen([PYTHON, "-c", CHILD, what], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True)
        with jobs.contain(proc.pid, 200 << 20):
            out, _ = proc.communicate("go\n", timeout=60)
        return out.strip()

    def test_a_contained_process_cannot_start_another(self):
        self.assertTrue(self.run_contained("spawn").startswith("REFUSED"))

    def test_a_contained_process_cannot_exceed_its_memory_limit(self):
        self.assertTrue(self.run_contained("memory").startswith("REFUSED MemoryError"))

    def test_the_decoder_process_runs_inside_a_job(self):
        import ctypes
        from ctypes import wintypes

        from workspace import jobs, media
        if not HAVE_DEPS:
            self.skipTest("numpy/av not installed")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                       ctypes.POINTER(wintypes.BOOL)]
        seen = {}
        real_contain = jobs.contain

        def checking_contain(pid, memory):
            job = real_contain(pid, memory)
            process = k32.OpenProcess(0x1000, False, pid)    # QUERY_LIMITED_INFORMATION
            inside = wintypes.BOOL()
            k32.IsProcessInJob(process, job.handle, ctypes.byref(inside))
            k32.CloseHandle(process)
            seen["inside"], seen["memory"] = bool(inside.value), memory
            return job
        with tempfile.TemporaryDirectory() as tmp:
            src = tone_wav(Path(tmp) / "tone.wav")
            with mock.patch.object(jobs, "contain", checking_contain):
                media.decode_to_file(src, Path(tmp) / "audio.f32")
        self.assertEqual(seen, {"inside": True, "memory": media.DECODER_MEMORY})

    def test_closing_the_job_ends_the_process(self):
        from workspace import jobs
        proc = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(60)"])
        job = jobs.contain(proc.pid, 200 << 20)
        job.close()
        self.assertIsNotNone(proc.wait(timeout=10))

    def test_containment_failure_is_reported(self):
        from workspace import jobs
        with self.assertRaises(jobs.JobError):
            jobs.contain(0x7FFFFFF0, 200 << 20)         # no such process


if __name__ == "__main__":
    unittest.main()
