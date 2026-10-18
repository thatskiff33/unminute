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


def multichannel_wav(path, data, rate, mask=None):
    """A 16-bit WAV of `data` (channels x samples, floats in -1..1). With
    mask=None it has a plain PCM header, which declares no channel layout;
    otherwise a WAVE_FORMAT_EXTENSIBLE header with that channel mask (0 means
    none given; 0x3F is 5.1: FL FR FC LFE BL BR)."""
    import numpy as np
    channels = data.shape[0]
    pcm = np.clip(np.round(data.T * 32767), -32768, 32767).astype("<i2").tobytes()
    if mask is None:
        fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * channels * 2, channels * 2, 16)
    else:
        pcm_guid = struct.pack("<IHH", 1, 0, 0x10) + bytes([0x80, 0, 0, 0xAA, 0, 0x38, 0x9B, 0x71])
        fmt = struct.pack("<HHIIHHHHI", 0xFFFE, channels, rate, rate * channels * 2,
                          channels * 2, 16, 22, 16, mask) + pcm_guid
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(pcm)) + pcm)
    Path(path).write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def speech_like(seconds, rate, seed):
    """Synthetic voiced sound: a gliding pitch (90 to 240 Hz) with harmonics
    shaped by two formants, in 4 Hz syllables, plus a little breath noise."""
    import numpy as np
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * rate)) / rate
    f0 = 165 + 75 * np.sin(2 * np.pi * 0.7 * t + seed)
    phase = 2 * np.pi * np.cumsum(f0) / rate
    voice = np.zeros_like(t)
    for h in range(1, 30):
        if h * 240 >= rate / 2:
            break
        formants = (np.exp(-((h * 165 - 700) / 300) ** 2)
                    + 0.5 * np.exp(-((h * 165 - 1800) / 500) ** 2))
        voice += (0.3 / h + formants) * np.sin(h * phase)
    syllables = np.clip(np.sin(2 * np.pi * 4 * t + seed), 0, None) ** 2
    signal = voice * syllables + 0.02 * rng.standard_normal(t.size)
    return signal / np.abs(signal).max()


def surround_matrix_decode(path):
    """The decoder before #220, in-process for comparison: one PyAV resampler
    straight to 16 kHz mono, which mixes channels by FFmpeg's surround matrix."""
    import av
    import numpy as np
    out = []
    with av.open(str(path)) as container:
        resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
        for frame in container.decode(container.streams.audio[0]):
            out += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
        out += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    return np.concatenate(out)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class EqualDownmixTests(unittest.TestCase):
    """Every channel counts equally (#220): FFmpeg's surround matrix dropped a
    track it took for a low-frequency channel, halved or boosted others, and
    let a mix of similar channels exceed full scale."""

    SEGMENT = 8000                     # half a second at 16 kHz per active channel

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def decoded(self, src):
        """Decode in the contained process, as an import does."""
        import numpy as np
        from workspace import media
        dest = self.root / f"{Path(src).name}.f32"
        samples, _ = media.decode_to_file(src, dest)
        audio = np.fromfile(dest, dtype="<f4")
        self.assertEqual(len(audio), samples)
        return audio

    def one_track_at_a_time(self, channels, rate):
        """Channel k alone carries a 440 Hz tone in the k-th half second."""
        import numpy as np
        seg = rate // 2
        tone = 0.5 * np.sin(2 * np.pi * 440 * np.arange(seg) / rate)
        data = np.zeros((channels, seg * channels))
        for k in range(channels):
            data[k, k * seg:(k + 1) * seg] = tone
        return data

    def weights(self, audio, channels):
        """Each segment's level over the tone's own level, away from the edges."""
        import numpy as np
        ref = 0.5 / np.sqrt(2)
        seg = self.SEGMENT
        return [float(np.sqrt(np.mean(audio[k * seg + seg // 4:k * seg + 3 * seg // 4]
                                      .astype(np.float64) ** 2)) / ref)
                for k in range(channels)]

    def test_each_channel_counts_equally_whatever_the_header_declares(self):
        # A plain header and a mask of 0 declare no layout: FFmpeg guessed one
        # (2.1, 4.0, 5.1 ...) and dropped track 3 of 3 and track 4 of 6. A full
        # mask declares speaker positions (3.0, 3.1, 5.1 for 6 ...).
        for channels in range(2, 9):
            for mask in (None, 0, (1 << channels) - 1):
                with self.subTest(channels=channels, mask=mask):
                    src = multichannel_wav(self.root / f"c{channels}-{mask}.wav",
                                           self.one_track_at_a_time(channels, 16000),
                                           16000, mask)
                    for k, w in enumerate(self.weights(self.decoded(src), channels)):
                        self.assertAlmostEqual(w, 1 / channels, delta=1e-3, msg=f"track {k + 1}")

    def test_weights_hold_after_resampling(self):
        for channels, mask in ((3, None), (6, 0x3F), (8, 0)):
            with self.subTest(channels=channels, mask=mask):
                src = multichannel_wav(self.root / f"r{channels}.wav",
                                       self.one_track_at_a_time(channels, 48000), 48000, mask)
                for k, w in enumerate(self.weights(self.decoded(src), channels)):
                    self.assertAlmostEqual(w, 1 / channels, delta=5e-3, msg=f"track {k + 1}")

    def test_a_file_declared_5_1_keeps_every_track(self):
        # Declared by the container, not guessed: lossless FLAC, 5.1 layout.
        import av
        import numpy as np
        data = self.one_track_at_a_time(6, 16000).astype(np.float32)
        src = self.root / "surround.flac"
        with av.open(str(src), "w", format="flac") as out:
            stream = out.add_stream("flac", rate=16000)
            stream.layout = "5.1"
            resampler = av.AudioResampler(format=stream.codec_context.format.name,
                                          layout="5.1", rate=16000)
            frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(data.T).reshape(1, -1),
                                               format="flt", layout="5.1")
            frame.sample_rate, frame.pts, frame.time_base = 16000, 0, Fraction(1, 16000)
            for f in resampler.resample(frame) + resampler.resample(None):
                for p in stream.encode(f):
                    out.mux(p)
            for p in stream.encode(None):
                out.mux(p)
        with av.open(str(src)) as check:
            self.assertEqual(check.streams.audio[0].layout.name, "5.1")
        for k, w in enumerate(self.weights(self.decoded(src), 6)):
            self.assertAlmostEqual(w, 1 / 6, delta=1e-3, msg=f"track {k + 1}")      # LFE is 4

    def test_identical_channels_never_exceed_the_input_peak(self):
        # The same near-full-scale signal on every channel decodes to exactly
        # what the mono file decodes to: no gain, so nothing above full scale.
        import numpy as np
        for rate in (16000, 44100):
            voice = 0.99 * speech_like(2, rate, seed=1)
            mono = self.decoded(multichannel_wav(self.root / f"m{rate}.wav",
                                                 voice[None, :], rate))
            peak = np.abs(mono).max()
            if rate == 16000:
                self.assertAlmostEqual(peak, 0.99, delta=1e-4)
            for channels in range(2, 9):
                with self.subTest(rate=rate, channels=channels):
                    src = multichannel_wav(self.root / f"d{rate}-{channels}.wav",
                                           np.tile(voice, (channels, 1)), rate)
                    audio = self.decoded(src)
                    self.assertLessEqual(np.abs(audio).max(), peak)
                    self.assertLess(np.abs(audio - mono).max(), 1e-6)

    def test_mono_and_stereo_speech_match_the_previous_decoder(self):
        # Mono is unchanged. Stereo has the same waveform as before, 3 dB
        # quieter: FFmpeg added L and R at 0.707 each, the mean takes 0.5 each.
        import numpy as np
        for rate in (16000, 44100, 48000):
            with self.subTest(rate=rate, channels=1):
                src = multichannel_wav(self.root / f"mono{rate}.wav",
                                       0.8 * speech_like(3, rate, seed=2)[None, :], rate)
                before, after = surround_matrix_decode(src), self.decoded(src)
                self.assertEqual(len(after), len(before))
                self.assertLess(np.abs(after - before).max(), 1e-6)
            with self.subTest(rate=rate, channels=2):
                pair = np.stack([0.6 * speech_like(3, rate, seed=3),
                                 0.6 * speech_like(3, rate, seed=4)])
                src = multichannel_wav(self.root / f"stereo{rate}.wav", pair, rate)
                before, after = surround_matrix_decode(src), self.decoded(src)
                self.assertEqual(len(after), len(before))
                self.assertLess(np.abs(after - before / np.sqrt(2)).max(), 1e-6)


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
        stream = os.fdopen(r, "rb")
        reader = media._Frames(stream)
        try:
            with self.assertRaises(media.DecodeError) as ctx:
                reader.next(0.2)
            self.assertIn("progress", str(ctx.exception))
        finally:
            reader.stop()
            os.close(w)                   # the reader sees the end and finishes
            reader.join(5)
            stream.close()


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


# Stand-in decoders (run with -c instead of decode_child.BOOT). Each reads the
# sys.path line like the real one, then misbehaves in one way.
STAND_IN = (
    "import sys,json,time,struct;sys.path[:]=json.loads(sys.stdin.readline());"
    "from workspace import decode_child as d;o=sys.stdout.buffer;"
    "w=lambda k,p=b'':(o.write(k+struct.pack('<I',len(p))+p),o.flush());"
)
SILENT = STAND_IN + ("lvl=d.lower_integrity() or 0;w(b'R',struct.pack('<I',lvl));"
                     "sys.stdin.readline();time.sleep(120)")
LIAR = STAND_IN + ("w(b'R',struct.pack('<I',0x1000));"        # claims Low, is Medium
                   "line=sys.stdin.readline();"
                   "open(sys.argv[1],'w').write('got the path' if line else 'no path')")
TRICKLE = STAND_IN + ("lvl=d.lower_integrity() or 0;w(b'R',struct.pack('<I',lvl));"
                      "sys.stdin.readline()\nwhile True:\n w(b'A',b'\\0'*4);time.sleep(0.2)")


@unittest.skipUnless(WINDOWS and HAVE_DEPS, "Windows decoder process")
class StandInDecoderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = tone_wav(self.root / "tone.wav")
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
        self.tmp.cleanup()

    def run_with(self, boot, extra_args=(), **patches):
        """decode_to_file with `boot` as the decoder; returns the error and
        the seconds it took."""
        import time

        from workspace import decode_child, media
        real_popen = subprocess.Popen

        def popen(cmd, *a, **kw):
            proc = real_popen([*cmd, *extra_args], *a, **kw)
            self.procs.append(proc)
            return proc
        start = time.monotonic()
        with mock.patch.object(decode_child, "BOOT", boot), \
                mock.patch.object(subprocess, "Popen", popen), \
                mock.patch.multiple(media, **patches) if patches else mock.MagicMock():
            with self.assertRaises(media.DecodeError) as ctx:
                media.decode_to_file(self.src, self.root / "audio.f32")
        return str(ctx.exception), time.monotonic() - start

    def assert_decoder_gone(self):
        self.assertTrue(self.procs)
        for proc in self.procs:
            self.assertIsNotNone(proc.wait(timeout=10))

    def test_a_silent_decoder_is_stopped_and_the_import_ends(self):
        message, took = self.run_with(SILENT, IDLE_TIMEOUT_S=1.0)
        self.assertIn("progress", message)
        self.assertLess(took, 30)
        self.assert_decoder_gone()
        self.assertFalse((self.root / "audio.partial").exists())

    def test_a_decoder_that_is_not_at_low_integrity_never_gets_the_file(self):
        marker = self.root / "marker.txt"
        message, _ = self.run_with(LIAR, extra_args=(str(marker),))
        self.assertIn("safely", message)
        self.assert_decoder_gone()
        self.assertEqual(marker.read_text() if marker.exists() else "no path", "no path")

    def test_a_trickling_decoder_hits_the_overall_deadline(self):
        message, took = self.run_with(TRICKLE, _deadline_s=lambda src: 2.0)
        self.assertIn("far longer", message)
        self.assertLess(took, 30)
        self.assert_decoder_gone()

    def test_no_reader_thread_is_left_behind(self):
        import threading
        import time
        self.run_with(SILENT, IDLE_TIMEOUT_S=1.0)
        for _ in range(50):
            if not any(t.name == "decoder-frames" for t in threading.enumerate()):
                break
            time.sleep(0.1)
        self.assertFalse(any(t.name == "decoder-frames" for t in threading.enumerate()))


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
