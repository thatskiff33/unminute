# Untrusted recordings (#111 slice A3): decoding in a contained child process
# (plain process, Windows job, Low integrity, checked binary replies), FFmpeg
# limited to the supported demuxers and local files, and the length and disk
# limits enforced by the app.
#   python -m unittest discover -s tests -v

import importlib.util
import io
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from fractions import Fraction
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
WINDOWS = sys.platform == "win32"
# In a virtual environment sys.executable is a launcher that starts the real
# interpreter as a second process; tests that contain a process use the
# interpreter itself, as workspace.media does.
PYTHON = getattr(sys, "_base_executable", sys.executable)


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


def encode(path, fmt, codec, video=False, rate=16000):
    """A one-second synthetic tone in any container PyAV can write."""
    import av
    import numpy as np
    tone = (0.2 * np.sin(2 * np.pi * 440 * np.arange(16000) / 16000)).astype(np.float32)
    with av.open(str(path), "w", format=fmt) as out:
        a = out.add_stream(codec, rate=48000 if codec == "libopus" else rate)
        a.layout = "mono"
        if codec == "wmav2":
            a.bit_rate = 64000              # the encoder refuses its default
        if video:
            v = out.add_stream("libx264", rate=10)
            v.width, v.height, v.pix_fmt = 64, 64, "yuv420p"
        res = av.AudioResampler(format=a.codec_context.format.name, layout="mono", rate=a.rate)
        frame = av.AudioFrame.from_ndarray(tone.reshape(1, -1), format="flt", layout="mono")
        frame.sample_rate, frame.pts, frame.time_base = 16000, 0, Fraction(1, 16000)
        for f in res.resample(frame) + res.resample(None):
            for p in a.encode(f):
                out.mux(p)
        for p in a.encode(None):
            out.mux(p)
        if video:
            for i in range(10):
                img = av.VideoFrame.from_ndarray(np.zeros((64, 64, 3), np.uint8), format="rgb24")
                img.pts = i
                for p in v.encode(img):
                    out.mux(p)
            for p in v.encode(None):
                out.mux(p)
    return path


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class DecoderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_normal_recording_decodes_in_the_contained_process(self):
        from workspace import media
        src = tone_wav(self.root / "tone.wav")
        dest = self.root / "audio.f32"
        samples, digest = media.decode_to_file(src, dest)
        self.assertEqual(samples, 16000)
        self.assertEqual(dest.stat().st_size, 16000 * 4)
        self.assertEqual(media.sha256_file(dest), digest)
        self.assertFalse(dest.with_suffix(".partial").exists())
        if WINDOWS:
            self.assertEqual(media.last_decoder_integrity, 0x1000)       # Low

    def test_every_supported_type_decodes(self):
        from workspace import app, media
        cases = {".wav": ("wav", "pcm_s16le"), ".flac": ("flac", "flac"),
                 ".mp3": ("mp3", "libmp3lame"), ".m4a": ("ipod", "aac"),
                 ".aac": ("adts", "aac"), ".ogg": ("ogg", "libopus"),
                 ".webm": ("webm", "libopus"), ".mkv": ("matroska", "flac"),
                 ".avi": ("avi", "pcm_s16le"), ".wma": ("asf", "wmav2"),
                 ".mp4": ("mp4", "aac", True), ".mov": ("mov", "aac", True)}
        self.assertEqual(set(cases), set(app.AUDIO_EXTENSIONS))
        for ext, (fmt, codec, *video) in cases.items():
            src = encode(self.root / f"in{ext}", fmt, codec, video=bool(video),
                         rate=44100 if ext == ".wma" else 16000)
            samples, _ = media.decode_to_file(src, self.root / f"out{ext}.f32")
            self.assertGreater(samples, 12000, ext)

    def test_files_only_the_demuxer_allowlist_refuses(self):
        from workspace import media
        tone_wav(self.root / "target.wav")
        # A relative concat entry would decode the neighbouring file even with
        # the local-file protocol rule: only the demuxer allowlist stops it.
        concat = self.root / "concat.mp3"
        concat.write_text("ffconcat version 1.0\nfile 'target.wav'\n", encoding="utf-8")
        # AIFF is a real audio format, just not one the app accepts.
        aiff = encode(self.root / "aiff.wav", "aiff", "pcm_s16be")
        for src in (concat, aiff):
            with self.assertRaises(media.DecodeError, msg=src.name) as ctx:
                media.decode_to_file(src, self.root / f"{src.name}.f32")
            self.assertNotIn(str(self.root), str(ctx.exception))

    def test_network_playlists_and_stream_descriptions_are_refused(self):
        from workspace import media
        cases = {"playlist.m4a": "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\n"
                                 "http://192.0.2.1/seg.ts\n#EXT-X-ENDLIST\n",
                 "session.wav": "v=0\no=- 0 0 IN IP4 192.0.2.1\ns=x\nc=IN IP4 192.0.2.1\n"
                                "t=0 0\nm=audio 5004 RTP/AVP 0\n"}
        for name, text in cases.items():
            src = self.root / name
            src.write_text(text, encoding="utf-8")
            with self.assertRaises(media.DecodeError, msg=name):
                media.decode_to_file(src, self.root / f"{name}.f32")

    def test_allowlist_names_no_risky_demuxer(self):
        from workspace import decode_child
        self.assertEqual(decode_child.OPEN_OPTIONS["protocol_whitelist"], "file")
        allowed = set(decode_child.OPEN_OPTIONS["format_whitelist"].split(","))
        for risky in ("hls", "concat", "rtsp", "rtp", "sdp", "dash", "image2", "tee", "data"):
            self.assertNotIn(risky, allowed)


def frames(*items):
    data = b""
    for kind, payload in items:
        data += kind + struct.pack("<I", len(payload)) + payload
    return data


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class ProtocolTests(unittest.TestCase):
    """The app reads only checked binary frames from the decoder: a reply can
    never run code here (nothing is unpickled) or put its own text in the app."""

    def collect(self, data, **kw):
        from workspace import media
        reader = media._Frames(io.BytesIO(data))
        out = io.BytesIO()
        result = media._collect(lambda: reader.next(5), out, **kw)
        return result, out.getvalue()

    def test_audio_frames_are_written_and_hashed_here(self):
        import hashlib
        audio = struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)
        (samples, digest), written = self.collect(frames((b"A", audio), (b"D", b"")))
        self.assertEqual((samples, written), (4, audio))
        self.assertEqual(digest, hashlib.sha256(audio).hexdigest())

    def test_broken_or_hostile_replies_are_refused(self):
        import pickle
        from workspace import media
        hostile = pickle.dumps(("ok", 1, "x"))            # what multiprocessing would have read
        for data in (hostile, frames((b"A", b"abc")), frames((b"X", b"")),
                     b"A" + struct.pack("<I", media.MAX_FRAME_BYTES + 1),
                     frames((b"A", b"\0" * 8))[:9],       # cut off mid-frame
                     frames((b"E", b"\x63")), b""):
            with self.assertRaises(media.DecodeError, msg=data[:12]) as ctx:
                self.collect(data)
            self.assertIn(str(ctx.exception), set(media.MESSAGES.values()) | {
                media._GENERIC, "The recording could not be decoded (the decoder stopped). "
                                "It may be damaged."})

    def test_error_codes_become_fixed_messages(self):
        from workspace import decode_child, media
        with self.assertRaises(media.DecodeError) as ctx:
            self.collect(frames((b"E", bytes([decode_child.E_TOO_LONG]))))
        self.assertEqual(str(ctx.exception), "The recording is longer than 12 hours.")

    def test_the_app_enforces_the_length_limit(self):
        from workspace import media
        audio = b"\0" * 4000
        with self.assertRaises(media.DecodeError) as ctx:
            self.collect(frames((b"A", audio), (b"A", audio), (b"D", b"")), max_samples=1500)
        self.assertIn("longer than 12 hours", str(ctx.exception))

    def test_the_app_keeps_free_disk_space(self):
        from workspace import media
        with mock.patch.object(media, "_free_bytes", return_value=10 << 20), \
                mock.patch.object(media, "_CHECK_EVERY", 4):
            with self.assertRaises(media.DecodeError) as ctx:
                self.collect(frames((b"A", b"\0" * 8), (b"D", b"")), folder=".")
        self.assertIn("free disk space", str(ctx.exception))

    def test_a_silent_decoder_times_out(self):
        from workspace import media
        r, w = os.pipe()
        reader = media._Frames(os.fdopen(r, "rb"))
        try:
            with self.assertRaises(media.DecodeError) as ctx:
                reader.next(0.2)
            self.assertIn("progress", str(ctx.exception))
        finally:
            os.close(w)


LOW_WRITE = r"""
import sys, json, os
sys.path[:] = json.loads(sys.argv[1])
from workspace import decode_child
level = decode_child.lower_integrity()
try:
    with open(os.path.join(sys.argv[2], "written-at-low.txt"), "w") as f:
        f.write("x")
    print(hex(level), "WRITE ALLOWED")
except OSError:
    print(hex(level), "write refused")
"""


@unittest.skipUnless(WINDOWS, "Windows integrity levels and job objects")
class ContainmentTests(unittest.TestCase):
    def test_lowered_decoder_cannot_write_the_users_files(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:    # %TEMP% is in the user's profile
            r = subprocess.run([PYTHON, "-I", "-B", "-c", LOW_WRITE, json.dumps(sys.path), tmp],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.stdout.strip(), "0x1000 write refused", r.stderr)
            self.assertEqual(os.listdir(tmp), [])

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


@unittest.skipUnless(WINDOWS, "Windows job objects")
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

    def test_inside_a_ui_limited_outer_job_the_ui_limits_are_dropped(self):
        from workspace import jobs
        calls = []

        def fake_assign(pid, memory, ui_limits):
            calls.append(ui_limits)
            if ui_limits:
                raise jobs.JobError(jobs.ERROR_NOT_SUPPORTED, "nested UI limits")
            return "job"
        with mock.patch.object(jobs, "_assign", fake_assign):
            self.assertEqual(jobs.contain(1234, 200 << 20), "job")
        self.assertEqual(calls, [True, False])
        with mock.patch.object(jobs, "_assign",
                               side_effect=jobs.JobError(5, "access denied")):
            with self.assertRaises(jobs.JobError):
                jobs.contain(1234, 200 << 20)           # other failures are not retried


if __name__ == "__main__":
    unittest.main()
