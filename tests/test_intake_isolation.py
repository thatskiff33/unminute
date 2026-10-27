# Untrusted recordings (#111 slice A3): decoding in a contained child process
# (an AppContainer with no capabilities since #299, a Windows job, Low
# integrity, the recording as one read-only handle, checked binary replies), FFmpeg
# limited to the supported demuxers, their audio decoders and local files,
# input sample rates of 4 kHz to 384 kHz, and the length, size-ratio and disk
# limits enforced by the app (D2 red team, part 2). A microphone recording is
# fed to the same decoder over its stdin as one WAV (#111 C3b).
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
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import decoder_runtime  # noqa: E402  (tools/decoder_runtime.py)

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
WINDOWS = sys.platform == "win32"
# In a virtual environment sys.executable is a launcher that starts the real
# interpreter as a second process; tests that contain a process use the
# interpreter itself, as workspace.media does.
PYTHON = getattr(sys, "_base_executable", sys.executable)


def setUpModule():
    # The decoder runs in its AppContainer (#299), which cannot read this
    # Python: decodes here use a scratch runtime it can read, and a throwaway
    # profile (tools/decoder_runtime.py).
    decoder_runtime.use()


def tearDownModule():
    decoder_runtime.stop()


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


def encode(path, fmt, codec, video=False, rate=16000, seconds=1, silent=False):
    """A synthetic tone (or digital silence) in any container PyAV can write,
    with a video track if `video` is True (H.264) or a video encoder's name."""
    import av
    import numpy as np
    n = int(16000 * seconds)
    tone = (0.2 * np.sin(2 * np.pi * 440 * np.arange(n) / 16000)).astype(np.float32)
    if silent:
        tone = np.zeros(n, np.float32)
    with av.open(str(path), "w", format=fmt) as out:
        a = out.add_stream(codec, rate=48000 if codec == "libopus" else rate)
        a.layout = "mono"
        if codec == "wmav2":
            a.bit_rate = 64000              # the encoder refuses its default
        if codec == "g723_1":
            a.bit_rate = 6300               # its only rate (8 kHz mono)
        if video:
            v = out.add_stream("libx264" if video is True else video, rate=10)
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


def with_id3_picture(path, mp3, mime, image):
    """`mp3` (a path) with an ID3v2.3 tag holding one picture (APIC frame)."""
    body = b"\x00" + mime + b"\x00\x03cover\x00" + image
    frame = b"APIC" + struct.pack(">I", len(body)) + b"\x00\x00" + body
    size = len(frame)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    Path(path).write_bytes(b"ID3\x03\x00\x00" + syncsafe + frame + Path(mp3).read_bytes())
    return path


def with_flac_picture(path, flac, mime, image):
    """`flac` (a path) with a PICTURE metadata block after its STREAMINFO."""
    data = Path(flac).read_bytes()
    info_len = struct.unpack(">I", b"\0" + data[5:8])[0]
    info = bytearray(data[4:8 + info_len])
    last = info[0] & 0x80
    info[0] &= 0x7F                              # STREAMINFO is no longer the last block
    pic = (struct.pack(">II", 3, len(mime)) + mime + struct.pack(">I", 5) + b"cover"
           + struct.pack(">IIIII", 16, 16, 32, 0, len(image)) + image)
    head = bytes([0x06 | last]) + struct.pack(">I", len(pic))[1:]
    Path(path).write_bytes(b"fLaC" + bytes(info) + head + pic + data[8 + info_len:])
    return path


def _crc8(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _crc16(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def silent_flac(path, frames, rate=4000, block=65535):
    """A valid FLAC file of digital silence in the largest blocks FLAC allows:
    each 13-byte frame holds `block` samples (16 s at 4 kHz). frames < 128."""
    info = struct.pack(">HH", block, block) + b"\0" * 6
    fields = (rate << 44) | (15 << 36) | (frames * block)         # rate, mono, 16-bit, samples
    info += fields.to_bytes(8, "big") + b"\0" * 16                 # no MD5
    out = b"fLaC" + bytes([0x80]) + len(info).to_bytes(3, "big") + info
    for n in range(frames):
        head = bytes([0xFF, 0xF8, 0x70, 0x08, n]) + struct.pack(">H", block - 1)
        body = head + bytes([_crc8(head)]) + b"\x00\x00\x00"       # CONSTANT subframe, 0
        out += body + struct.pack(">H", _crc16(body))
    Path(path).write_bytes(out)
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
        dest = self.root / "audio.uma"
        key, context = bytes(range(32)), ("decoded", "m")
        samples, file_id = media.decode_to_file(src, dest, key, context)
        self.assertEqual(samples, 16000)
        reader = media.AudioReader(dest, key, context)       # written encrypted (#111 C3a)
        try:
            self.assertEqual((len(reader), reader.file_id), (16000, file_id))
            plain = io.BytesIO()
            media.decode(src, plain)
            self.assertEqual(reader.read(0, 16000).astype("<f4").tobytes(), plain.getvalue())
        finally:
            reader.close()
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
            samples = media.decode(src, io.BytesIO())
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
                media.decode(src, io.BytesIO())
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
                media.decode(src, io.BytesIO())

    def test_allowlist_names_no_risky_demuxer(self):
        from workspace import decode_child
        self.assertEqual(decode_child.OPEN_OPTIONS["protocol_whitelist"], "file")
        allowed = set(decode_child.OPEN_OPTIONS["format_whitelist"].split(","))
        for risky in ("hls", "concat", "rtsp", "rtp", "sdp", "dash", "image2", "tee", "data"):
            self.assertNotIn(risky, allowed)

    def test_the_decoder_list_names_only_audio_decoders(self):
        import av

        from workspace import decode_child
        names = decode_child.OPEN_OPTIONS["codec_whitelist"].split(",")
        self.assertEqual(names, decode_child.AUDIO_DECODERS.split(","))
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(av.Codec(name, "r").type, "audio")

    def test_cover_art_and_video_tracks_never_reach_their_decoders(self):
        # Opening a file probes every stream. Before the decoder list, a video
        # track's decoder and an MP3's or FLAC's cover-art image decoder parsed
        # the file's bytes before the app chose the audio (D2 red team, part
        # 2, finding 1). Now FFmpeg refuses them, and the audio still decodes.
        import av

        from workspace import decode_child, media
        mp3 = encode(self.root / "plain.mp3", "mp3", "libmp3lame")
        flac = encode(self.root / "plain.flac", "flac", "flac")
        tiff = b"II*\x00\x08\x00\x00\x00\x0a\x00" + bytes(range(256)) * 16
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)) * 16
        cases = {"tiff": with_id3_picture(self.root / "cover.mp3", mp3, b"image/tiff", tiff),
                 "png": with_flac_picture(self.root / "cover.flac", flac, b"image/png", png),
                 "mpeg4": encode(self.root / "video.mkv", "matroska", "flac", video="mpeg4")}
        for decoder, src in cases.items():
            with self.subTest(src=src.name):
                level = av.logging.get_level()
                av.logging.set_level(av.logging.DEBUG)
                try:
                    with av.logging.Capture(True) as logs:
                        decode_child.decode(str(src), lambda kind, payload=b"": None)
                finally:
                    av.logging.set_level(level)
                said = [m for _, name, m in logs if (name or "").split(" @")[0] == decoder]
                self.assertTrue(said, decoder)
                for message in said:
                    self.assertIn("not on whitelist", message)
                samples = media.decode(src, io.BytesIO())
                self.assertGreater(samples, 12000)

    def test_an_audio_codec_outside_the_list_is_refused(self):
        from workspace import media
        src = encode(self.root / "yamaha.wav", "wav", "adpcm_yamaha")
        with self.assertRaises(media.DecodeError) as ctx:
            media.decode(src, io.BytesIO())
        self.assertEqual(str(ctx.exception), media._GENERIC)

    def test_phone_and_voip_speech_decoders_are_listed_and_two_are_not(self):
        # The owner's decision of 22 October 2026 (#302): phone and VoIP
        # speech codecs import (audio only); TrueSpeech and QDM2 stay refused.
        from workspace import decode_child
        names = decode_child.AUDIO_DECODERS.split(",")
        for name in ("g729", "g723_1", "gsm", "gsm_ms", "ilbc", "speex"):
            with self.subTest(allowed=name):
                self.assertIn(name, names)
        for name in ("truespeech", "qdm2"):
            with self.subTest(refused=name):
                self.assertNotIn(name, names)

    def test_g723_1_phone_speech_decodes(self):
        # The one listed phone codec this PyAV build can also encode: a
        # synthetic second of G.723.1 (6.3 kbit/s, 8 kHz) in a WAV.
        from workspace import media
        src = encode(self.root / "phone.wav", "wav", "g723_1", rate=8000)
        samples = media.decode(src, io.BytesIO())
        self.assertAlmostEqual(samples, 16000, delta=800)

    def test_a_stream_with_no_decoder_is_refused_not_crashed(self):
        # PyAV 17 leaves codec_context as None when FFmpeg has no decoder for
        # the stream; decoding it crashed the decoder process (finding 4).
        from workspace import media
        good = tone_wav(self.root / "tone.wav").read_bytes()
        for name, offset, value in (("bits-zero.wav", 34, 0), ("format-7777.wav", 20, 0x7777)):
            with self.subTest(name=name):
                data = bytearray(good)
                struct.pack_into("<H", data, offset, value)
                src = self.root / name
                src.write_bytes(bytes(data))
                with self.assertRaises(media.DecodeError) as ctx:
                    media.decode(src, io.BytesIO())
                self.assertEqual(str(ctx.exception), media._GENERIC)

    def test_the_bundled_pyav_raises_rather_than_crashes_without_a_decoder(self):
        # The second layer under the check above (#310): PyAV 18.1.0 and later
        # raise DecoderNotFoundError for a stream with no codec context
        # (PyAV-Org/PyAV#2344); PyAV 17.1.0 crashed the process. Run in a child
        # process with PyAV alone, no app guard, so a crash fails the test.
        script = (
            "import sys, av\n"
            "with av.open(sys.argv[1]) as c:\n"
            "    s = c.streams.audio[0]\n"
            "    assert s.codec_context is None\n"
            "    try:\n"
            "        for _ in c.decode(s):\n"
            "            pass\n"
            "    except av.error.DecoderNotFoundError:\n"
            "        print('refused')\n")
        good = tone_wav(self.root / "tone.wav").read_bytes()
        for name, offset, value in (("bits-zero.wav", 34, 0), ("format-7777.wav", 20, 0x7777)):
            with self.subTest(name=name):
                data = bytearray(good)
                struct.pack_into("<H", data, offset, value)
                src = self.root / name
                src.write_bytes(bytes(data))
                r = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(src)],
                                   capture_output=True, text=True, timeout=120)
                self.assertEqual((r.returncode, r.stdout.strip()), (0, "refused"), r.stderr)

    def test_sample_rates_outside_4_to_384_khz_are_refused(self):
        # A header claiming 1 Hz made each stored sample 16,000 output
        # samples: 86 KB decoded to 12 hours (finding 3).
        from workspace import media
        for rate in (1, 3999, 384001):
            with self.subTest(rate=rate):
                src = tone_wav(self.root / f"r{rate}.wav", seconds=0.01, rate=16000)
                data = bytearray(src.read_bytes())
                struct.pack_into("<II", data, 24, rate, rate * 2)
                src.write_bytes(bytes(data))
                with self.assertRaises(media.DecodeError) as ctx:
                    media.decode(src, io.BytesIO())
                self.assertEqual(str(ctx.exception), media._GENERIC)
        for rate in (4000, 384000):
            with self.subTest(rate=rate):
                src = tone_wav(self.root / f"ok{rate}.wav", seconds=0.5, rate=rate)
                samples = media.decode(src, io.BytesIO())
                self.assertAlmostEqual(samples, 8000, delta=100)

    def test_a_small_file_cannot_decode_to_hours(self):
        # 432 bytes of valid FLAC (silence in 16-second blocks at 4 kHz)
        # holds over 8 minutes: more than one second per 16 bytes of file.
        from workspace import decode_child, media
        src = silent_flac(self.root / "dense.flac", frames=30)
        self.assertGreater(30 * 65535 / 4000, src.stat().st_size / 16)     # seconds
        with self.assertRaises(media.DecodeError) as ctx:
            media.decode_to_file(src, self.root / "dense.uma", bytes(32), ("decoded", "m"))
        self.assertEqual(str(ctx.exception), media.MESSAGES[decode_child.E_TOO_DENSE])
        self.assertFalse((self.root / "dense.partial").exists())
        self.assertFalse((self.root / "dense.uma").exists())

    def test_digital_silence_from_real_encoders_is_within_the_limit(self):
        # The densest encoder measured: AAC at 8 kHz, about 64 bytes a second
        # of digital silence. A minute of it still imports.
        from workspace import media
        src = encode(self.root / "quiet.m4a", "ipod", "aac", rate=8000, seconds=60, silent=True)
        samples = media.decode(src, io.BytesIO())
        self.assertAlmostEqual(samples, 60 * 16000, delta=4000)


class ArrayAudio:
    """A recording held in memory, streamed as the recorder's import does
    (rate, frames, blocks() of little-endian int16 bytes)."""

    def __init__(self, samples, rate, block_s=10, fail_after=None, short=0):
        self.samples, self.rate = samples, rate
        self.frames = len(samples) + short          # short: claims more than it holds
        self._step, self._fail_after = int(block_s * rate), fail_after

    def blocks(self):
        for i, a in enumerate(range(0, len(self.samples), self._step)):
            if self._fail_after is not None and i == self._fail_after:
                from workspace import audiocrypt
                raise audiocrypt.AudioCryptError("The audio file is damaged (cut short).")
            yield self.samples[a:a + self._step].astype("<i2").tobytes()


class Counting:
    """An output that keeps only how much was written and the first bytes."""

    def __init__(self, keep=1 << 20):
        self.n, self.head, self._keep = 0, bytearray(), keep

    def write(self, data):
        self.n += len(data)
        if len(self.head) < self._keep:
            self.head += data[: self._keep - len(self.head)]


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class StdinDecoderTests(unittest.TestCase):
    """#111 C3b: a microphone recording reaches the contained decoder over
    its stdin as one WAV, so no plain recording.wav is written; the same
    demuxer, decoders, limits and sandbox apply."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def speech(self, seconds, rate):
        import numpy as np
        rng = np.random.default_rng(111)
        t = np.arange(int(seconds * rate)) / rate
        x = 0.2 * np.sin(2 * np.pi * 180 * t) * (1 + np.sin(2 * np.pi * 3 * t))
        return np.clip((x + 0.02 * rng.standard_normal(len(t))) * 32767,
                       -32768, 32767).astype("<i2")

    def test_stdin_decode_equals_the_file_decode(self):
        from workspace import media
        for rate in (16000, 44100, 48000):
            pcm = self.speech(3.0, rate)
            path = self.root / f"r{rate}.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(pcm.tobytes())
            from_file, from_stdin = io.BytesIO(), io.BytesIO()
            n1 = media.decode(path, from_file)
            n2 = media.decode(None, from_stdin, feed=media.WavFeed(ArrayAudio(pcm, rate)))
            self.assertEqual(n1, n2, rate)
            self.assertEqual(from_file.getvalue(), from_stdin.getvalue(), rate)   # bit for bit
            if WINDOWS:
                self.assertEqual(media.last_decoder_integrity, 0x1000)          # Low

    def test_the_feed_hashes_and_counts_what_it_sends(self):
        import hashlib

        from workspace import media
        pcm = self.speech(1.0, 16000)
        feed = media.WavFeed(ArrayAudio(pcm, 16000))
        media.decode(None, io.BytesIO(), feed=feed)
        self.assertEqual(feed.sha256, hashlib.sha256(pcm.tobytes()).hexdigest())
        self.assertEqual((feed.sent, feed.size), (len(pcm) * 2, 44 + len(pcm) * 2))

    def test_ten_minutes_through_stdin_without_a_deadlock(self):
        # The feeder writes stdin on its own thread while this thread reads
        # the decoder's frames (queue of 8): neither can block the other.
        from workspace import media
        rate = 48000
        pcm = self.speech(600.0, rate)
        out = Counting()
        samples = media.decode(None, out, feed=media.WavFeed(ArrayAudio(pcm, rate)))
        self.assertEqual(out.n, samples * 4)
        self.assertLess(abs(samples - 600 * 16000), 16)

    def test_a_feed_that_fails_midway_fails_the_decode(self):
        from workspace import audiocrypt, media
        pcm = self.speech(30.0, 16000)
        with self.assertRaises(audiocrypt.AudioCryptError):   # never a shorter import
            media.decode(None, io.BytesIO(),
                         feed=media.WavFeed(ArrayAudio(pcm, 16000, fail_after=1)))
        with self.assertRaises(media.DecodeError):             # less than the header says
            media.decode(None, io.BytesIO(),
                         feed=media.WavFeed(ArrayAudio(pcm, 16000, short=16000)))

    def test_too_long_for_a_wav_header_is_refused_before_decoding(self):
        from workspace import media
        audio = type("Huge", (), {"rate": 48000, "frames": (0xFFFFFFFF - 36) // 2 + 1})()
        with self.assertRaisesRegex(media.DecodeError, "longer than 12 hours"):
            media.WavFeed(audio)

    def test_the_request_line_is_checked(self):
        from workspace import decode_child
        ok = {b'{"handle": 1234}\n': ("handle", 1234),
              b'{"handle": 4294967295}\n': ("handle", 0xFFFFFFFF),
              b'{"stdin": "wav", "bytes": 44}\n': ("stdin", 44)}
        for line, expected in ok.items():
            self.assertEqual(decode_child.read_request(io.BytesIO(line + b"RIFF")), expected)
        # A path is no longer a request (#299): the container could not open
        # it, and a decoder that asks for one gets nothing.
        for line in (b'{"stdin": "mp3", "bytes": 100}\n', b'{"stdin": "wav", "bytes": true}\n',
                     b'{"stdin": "wav", "bytes": 10}\n', b'{"stdin": "wav"}\n',
                     b'{"stdin": "wav", "bytes": 100, "handle": 8}\n', b'{"handle": "8"}\n',
                     b'{"handle": true}\n', b'{"handle": 0}\n', b'{"handle": -4}\n',
                     b'{"handle": 4294967296}\n', b'{"handle": 8.0}\n',
                     b'{"src": "C:/x.wav"}\n', b'["handle"]\n', b'{"handle": 8}',
                     b'{"handle": ' + b"8" * 70000 + b'}\n'):
            with self.assertRaises(ValueError, msg=line[:40]):
                decode_child.read_request(io.BytesIO(line))

    def test_the_stream_is_never_seeked(self):
        # Windows reports a pipe as seekable; PyAV then seeks it and fails.
        from workspace import decode_child
        stream = decode_child._Stream(io.BytesIO(b"abc"))
        self.assertFalse(stream.seekable())
        self.assertFalse(hasattr(stream, "seek"))
        self.assertEqual(stream.read(2), b"ab")

    def test_the_format_and_decoder_lists_are_unchanged(self):
        # C3b adds no demuxer (a raw PCM one would widen the list, E2): the
        # stream is a WAV, read by the "wav" demuxer already allowed.
        from workspace import decode_child
        self.assertEqual(decode_child.DEMUXERS,
                         "wav,mp3,mov,mp4,m4a,matroska,webm,flac,ogg,aac,asf,avi")
        self.assertEqual(decode_child.OPEN_OPTIONS["format_whitelist"], decode_child.DEMUXERS)


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
        out = io.BytesIO()
        samples = media.decode(src, out)
        audio = np.frombuffer(out.getvalue(), dtype="<f4")
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

    def test_audio_frames_are_written_here(self):
        # No hash any more (#111 C3a): the file is encrypted and authenticated.
        audio = struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)
        samples, written = self.collect(frames((b"A", audio), (b"D", b"")))
        self.assertEqual((samples, written), (4, audio))

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

    def test_the_app_enforces_the_limit_for_the_files_size(self):
        # Whatever the decoder reports, a file decodes to at most one second
        # of audio per 16 bytes (D2 red team, part 2, finding 3).
        from workspace import decode_child, media
        self.assertEqual(decode_child.max_samples_for(16 * 60), 60 * 16000)
        self.assertEqual(decode_child.max_samples_for(1 << 40), decode_child.MAX_DECODED_S * 16000)
        audio = b"\0" * 4000
        with self.assertRaises(media.DecodeError) as ctx:
            self.collect(frames((b"A", audio), (b"A", audio), (b"D", b"")), dense_limit=1500)
        self.assertEqual(str(ctx.exception), media.MESSAGES[decode_child.E_TOO_DENSE])

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
                media.decode(src, io.BytesIO(), folder=Path(tmp))
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
READY_THEN_WAIT = STAND_IN + ("lvl=d.lower_integrity() or 0;w(b'R',struct.pack('<I',lvl));"
                              "sys.stdin.buffer.readline();time.sleep(120)")
NEVER_READY = STAND_IN + "time.sleep(120)"
# Put before the real BOOT: every open of the null device by name fails, as
# it does in an AppContainer on Windows Server 2025 (the hosted CI runner,
# measured 25 October 2026) though not on Windows 11 build 26300.
NULL_REFUSED = (
    "import os,builtins;_o,_b=os.open,builtins.open;"
    "_x=lambda p:(_ for _ in ()).throw(PermissionError(13,'Access is denied',p));"
    "_n=lambda p:isinstance(p,str) and p.lower()==os.devnull.lower();"
    "os.open=lambda p,*a,**k:_x(p) if _n(p) else _o(p,*a,**k);"
    "builtins.open=lambda p,*a,**k:_x(p) if _n(p) else _b(p,*a,**k);"
)
ENDS_AT_ONCE = "import sys;sys.exit(3)"
TRICKLE = STAND_IN + ("lvl=d.lower_integrity() or 0;w(b'R',struct.pack('<I',lvl));"
                      "sys.stdin.readline()\nwhile True:\n w(b'A',b'\\0'*4);time.sleep(0.2)")


class Lines:
    """A decoder's stdin that counts the lines written to it (for checking
    that a refused decoder never got its request)."""

    def __init__(self, pipe):
        self.pipe, self.lines = pipe, 0

    def write(self, data):
        self.lines += data.count(b"\n")
        return self.pipe.write(data)

    def __getattr__(self, name):
        return getattr(self.pipe, name)


def process_gone(pid):
    """Has the process `pid` ended (or gone altogether)?"""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x00100000, False, pid)              # SYNCHRONIZE
    if not handle:
        return True
    try:
        return k32.WaitForSingleObject(handle, 10000) == 0
    finally:
        k32.CloseHandle(handle)


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

    def run_with(self, boot, error=None, **patches):
        """decode_to_file with `boot` as the decoder in its container;
        returns the error and the seconds it took."""
        import time

        from workspace import appcontainer, decode_child, media
        real_start = appcontainer.start

        def start(argv, *a, **kw):
            child = real_start(argv, *a, **kw)
            child.stdin = Lines(child.stdin)
            self.procs.append(child)
            return child
        begin = time.monotonic()
        with mock.patch.object(decode_child, "BOOT", boot), \
                mock.patch.object(appcontainer, "start", start), \
                mock.patch.multiple(media, **patches) if patches else mock.MagicMock():
            with self.assertRaises(error or media.DecodeError) as ctx:
                media.decode_to_file(self.src, self.root / "audio.uma", bytes(32),
                                     ("decoded", "m"))
        return ctx.exception, time.monotonic() - begin

    def assert_decoder_gone(self):
        self.assertTrue(self.procs)
        for proc in self.procs:
            self.assertIsNotNone(proc.wait(timeout=10))

    def test_a_silent_decoder_is_stopped_and_the_import_ends(self):
        error, took = self.run_with(SILENT, IDLE_TIMEOUT_S=1.0)
        self.assertIn("progress", str(error))
        self.assertLess(took, 30)
        self.assert_decoder_gone()
        self.assertFalse((self.root / "audio.partial").exists())

    def test_a_decoder_that_is_not_at_low_integrity_never_gets_the_file(self):
        # Checked from outside after READY, whatever the decoder reports.
        from workspace import decode_child, media
        with mock.patch.object(decode_child, "process_integrity", return_value=0x2000):
            error, _ = self.run_with(READY_THEN_WAIT, error=media.SandboxUnavailable)
        self.assertEqual((str(error), error.step), (media.SANDBOX_REFUSED, "check"))
        self.assert_decoder_gone()
        self.assertEqual([p.stdin.lines for p in self.procs], [1])   # the path line only

    def test_a_decoder_that_never_says_ready_is_refused_as_a_sandbox_failure(self):
        from workspace import media
        for boot, patches in ((NEVER_READY, {"READY_TIMEOUT_S": 1.0}), (ENDS_AT_ONCE, {})):
            with self.subTest(boot=boot[-20:]):
                self.procs = []
                error, took = self.run_with(boot, error=media.SandboxUnavailable, **patches)
                self.assertEqual((str(error), error.step), (media.SANDBOX_REFUSED, "ready"))
                self.assertLess(took, 30)
                self.assert_decoder_gone()

    def test_a_trickling_decoder_hits_the_overall_deadline(self):
        error, took = self.run_with(TRICKLE, _deadline_s=lambda src=None, size=None: 2.0)
        self.assertIn("far longer", str(error))
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


# What a compromised decoder can reach (#299; D2 red team, part 2, finding 2).
# READ_UP_PROBE stands in for native code that a hostile recording has taken
# over inside the decoder. media.decode starts and contains it exactly as it
# does the real decoder (media._runtime(), the AppContainer, jobs.contain and
# the token check before it runs, then a Low integrity check from outside),
# and it lowers its own integrity with decode_child.lower_integrity(), as
# decode_child.main does. It does not install the network guard, which covers
# Python code only. Its "recording" is a JSON file that names what to try,
# read through the request's handle as the decoder reads a recording (or by
# its path, the request before #299, so the same probe ran on main for the
# failing-first evidence); it answers with one audio frame holding JSON.
# Everything it touches is in the test's temporary folder, or a listener in
# this process.
READ_UP_PROBE = r"""
import hashlib, json, os, socket, sqlite3, struct, sys
sys.path[:] = json.loads(sys.stdin.readline())
from pathlib import Path
from workspace import decode_child, dpapi, vault
out = os.fdopen(os.dup(1), "wb")
def send(kind, payload=b""):
    out.write(kind + struct.pack("<I", len(payload)) + payload)
    out.flush()
send(b"R", struct.pack("<I", decode_child.lower_integrity() or 0))
req = json.loads(sys.stdin.buffer.readline())
f = decode_child.open_handle(req["handle"]) if "handle" in req else open(req["src"], "rb")
with f:
    cfg = json.loads(f.read())
res = {"recording": "allowed: read"}
def attempt(name, fn):
    try:
        res[name] = "allowed: " + str(fn())
    except Exception as exc:
        res[name] = "denied: %s %s" % (type(exc).__name__, getattr(exc, "winerror", "") or "")
def read(path):
    with open(path, "rb") as f:
        return "read %d bytes" % len(f.read())
for name, path in cfg["files"].items():
    attempt("read " + name, lambda p=path: read(p))
for name, path in cfg["folders"].items():
    attempt("list " + name, lambda p=path: "listed %d" % len(os.listdir(p)))
def key():
    con = sqlite3.connect(Path(cfg["db"]).as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        blob = con.execute("SELECT root FROM crypto_meta").fetchone()[0]
    finally:
        con.close()
    return "unprotected " + hashlib.sha256(dpapi.unprotect(blob, vault.ROOT_ENTROPY)).hexdigest()
attempt("unprotect workspace key", key)
def write():
    with open(os.path.join(cfg["folders"]["workspace"], "written-at-low.txt"), "wb") as f:
        f.write(b"x")
    return "wrote"
attempt("write workspace", write)
def loopback():
    with socket.create_connection(("127.0.0.1", cfg["port"]), 5) as s:
        s.sendall(b"sent by the decoder")
    return "connected and sent"
attempt("loopback", loopback)
body = json.dumps(res).encode()
send(b"A", body + b" " * (-len(body) % 4))
send(b"D")
"""

# Files and folders of a scratch workspace laid out like the real one
# (%LOCALAPPDATA%\Unminute: workspace\ and models\; docs/SECURITY_MODEL.md,
# "Data inventory"), relative to its Unminute folder. workspace.db is made by
# the store with the app's own key protector; the rest hold synthetic bytes.
SCRATCH_FILES = {
    "workspace.db": "workspace/workspace.db",
    "settings.json": "workspace/settings.json",
    "templates.sealed": "workspace/templates.sealed",
    "decoded audio": "workspace/media/1/audio.uma",
    "recording chunk": "workspace/recordings/r1/chunk-000001.uma",
    "diagnostics log": "workspace/logs/app.log",
    "audit log": "workspace/audit/audit-000001.jsonl",
    "model file": "models/model.bin",
}
SCRATCH_FOLDERS = {"Unminute": "", "workspace": "workspace", "media": "workspace/media",
                   "models": "models"}
# The user's other files: a recording outside Unminute, relative to the folder
# that holds the scratch LocalAppData (as Documents sits beside AppData).
OTHER_FILES = {"another recording": "Documents/other-recording.wav"}


def make_scratch_workspace(base):
    """A scratch workspace at base (its Unminute folder) and the SHA-256 of
    its root key. The store makes workspace.db with Windows Data Protection,
    as the app does; nothing here is under the real data folder."""
    import hashlib
    import sqlite3

    from workspace import keystore, vault
    from workspace.store import Store
    s = Store(base / SCRATCH_FILES["workspace.db"], protector=keystore.DpapiProtector())
    try:
        s.create_meeting("synthetic title")
    finally:
        s.close()
    for name, rel in SCRATCH_FILES.items():
        path = base / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic " + name.encode())
    con = sqlite3.connect(base / SCRATCH_FILES["workspace.db"])
    try:
        blob = con.execute("SELECT root FROM crypto_meta").fetchone()[0]
    finally:
        con.close()
    return hashlib.sha256(vault.open_root(blob)).hexdigest()


def probe_scratch_workspace(base, folder):
    """Run READ_UP_PROBE through media.decode against the scratch workspace
    at base. folder: where its JSON "recording" goes (outside base). Returns
    (the probe's results, the integrity level media.decode saw, what a
    loopback listener here received)."""
    import json
    import socket
    import threading

    from workspace import decode_child, media
    received = []
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(20)

    def accept():
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(5)
                data = conn.recv(100)
                if data:                    # not the wake-up connection below, which sends none
                    received.append(data)
        except OSError:
            pass
    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    cfg = {"files": {**{n: str(base / r) for n, r in SCRATCH_FILES.items()},
                     **{n: str(Path(folder) / r) for n, r in OTHER_FILES.items()}},
           "folders": {n: str(base / r) for n, r in SCRATCH_FOLDERS.items()},
           "db": str(base / SCRATCH_FILES["workspace.db"]),
           "port": listener.getsockname()[1]}
    src = Path(folder) / "probe-request.json"
    src.write_text(json.dumps(cfg), encoding="utf-8")
    out = io.BytesIO()
    try:
        with mock.patch.object(decode_child, "BOOT", READ_UP_PROBE):
            media.decode(src, out)
    finally:
        try:
            # Ends the accept() if the probe never connected.
            socket.create_connection(("127.0.0.1", cfg["port"]), 2).close()
        except OSError:
            pass
        thread.join(10)
        listener.close()
    return json.loads(out.getvalue()), media.last_decoder_integrity, received


@unittest.skipUnless(WINDOWS and HAVE_DEPS, "Windows decoder process")
class ReadUpTests(unittest.TestCase):
    """#299 acceptance criterion 1: a process started and contained exactly as
    the decoder is (in its AppContainer, since #299) cannot read the workspace
    or the user's other files, cannot unprotect the workspace key, and cannot
    open a loopback connection.

    These three failed on main (the T2 residual before #299): run there, the
    probe read every file, unprotected the key and reached the listener. The
    first test checks the probe really ran contained like the decoder and
    still read its own recording, so the others cannot pass because the probe
    never ran."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.base = root / "LocalAppData" / "Unminute"
        cls.key_hash = make_scratch_workspace(cls.base)
        for name, rel in OTHER_FILES.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(b"synthetic " + name.encode())
        cls.results, cls.level, cls.received = probe_scratch_workspace(cls.base, root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_probe_runs_contained_like_the_decoder(self):
        self.assertEqual(0x1000, self.level)                   # Low, checked from outside
        expected = ({"recording", "unprotect workspace key", "write workspace", "loopback"}
                    | {"read " + n for n in [*SCRATCH_FILES, *OTHER_FILES]}
                    | {"list " + n for n in SCRATCH_FOLDERS})
        self.assertEqual(expected, set(self.results))
        self.assertEqual("allowed: read", self.results["recording"])   # it can still decode
        self.assertTrue(self.results["write workspace"].startswith("denied"), self.results)
        self.assertFalse((self.base / "workspace" / "written-at-low.txt").exists())

    def test_a_contained_decoder_cannot_read_the_workspace(self):
        # Also the user's other files (OTHER_FILES), outside Unminute.
        readable = {k: v for k, v in self.results.items()
                    if k.startswith(("read ", "list ")) and not v.startswith("denied")}
        self.assertEqual({}, readable)

    def test_a_contained_decoder_cannot_unprotect_the_workspace_key(self):
        got = self.results["unprotect workspace key"]
        self.assertTrue(got.startswith("denied"),
                        "the probe unprotected the scratch workspace's root key"
                        + (" (it matches)" if got.endswith(self.key_hash) else ""))

    def test_a_contained_decoder_cannot_open_a_loopback_connection(self):
        self.assertEqual(([], True), (self.received,
                                      self.results["loopback"].startswith("denied")))


@unittest.skipUnless(WINDOWS and HAVE_DEPS, "Windows decoder process")
class AppContainerTests(unittest.TestCase):
    """#299: how the decoder's container is set up, checked and refused. If
    any step fails the import is refused with SANDBOX_REFUSED, and no decoder
    outside the container is ever started (the owner's decision of 24 October
    2026). Profiles made here are throwaway ones, removed again."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = tone_wav(self.root / "tone.wav")

    def refused(self, **patches):
        """decode_to_file with appcontainer patched; Popen must never run.
        Returns the SandboxUnavailable raised."""
        from workspace import appcontainer, media

        def no_fallback(*a, **kw):
            raise AssertionError("a decoder was started outside the container")
        with mock.patch.multiple(appcontainer, **patches), \
                mock.patch.object(subprocess, "Popen", no_fallback):
            with self.assertRaises(media.SandboxUnavailable) as ctx:
                media.decode_to_file(self.src, self.root / "audio.uma", bytes(32),
                                     ("decoded", "m"))
        self.assertEqual(str(ctx.exception), media.SANDBOX_REFUSED)
        self.assertNotIn(str(self.root), str(ctx.exception))
        self.assertFalse((self.root / "audio.partial").exists())
        self.assertFalse((self.root / "audio.uma").exists())
        return ctx.exception

    def test_the_decoder_runs_in_this_appcontainer_with_no_capabilities(self):
        from workspace import appcontainer, media
        seen = []
        real = appcontainer.token_facts

        def facts(process):
            seen.append(real(process))
            return seen[-1]
        with mock.patch.object(appcontainer, "token_facts", facts):
            samples = media.decode(self.src, io.BytesIO())
        self.assertEqual(samples, 16000)
        self.assertEqual(seen, [(True, appcontainer.container_sid(), 0, 0x1000)])
        self.assertEqual(media.last_decoder_integrity, 0x1000)

    def test_a_profile_that_cannot_be_made_refuses_the_import(self):
        from workspace import appcontainer
        error = self.refused(ensure_profile=mock.Mock(
            side_effect=appcontainer.SandboxError("profile", 5)))
        self.assertEqual((error.step, error.winerror), ("profile", 5))

    def test_a_process_that_cannot_start_in_the_container_refuses_the_import(self):
        from workspace import appcontainer
        error = self.refused(start=mock.Mock(side_effect=appcontainer.SandboxError("start", 5)))
        self.assertEqual((error.step, error.winerror), ("start", 5))

    def test_a_runtime_the_container_cannot_read_refuses_the_import(self):
        # The real failure when the install folder lacks the installer's grant:
        # an interpreter in a folder ALL APPLICATION PACKAGES cannot read.
        import shutil

        from workspace import media
        py = self.root / "ungranted"
        py.mkdir()
        for item in Path(sys.base_prefix).iterdir():
            if item.is_file() and item.suffix.lower() in (".exe", ".dll"):
                shutil.copy2(item, py / item.name)
        acl = decoder_runtime.icacls(py).stdout
        self.assertNotIn("S-1-15-2-1", acl)
        self.assertNotIn("ALL APPLICATION PACKAGES", acl.upper())

        def no_fallback(*a, **kw):
            raise AssertionError("a decoder was started outside the container")
        with mock.patch.object(media, "_runtime", lambda: (str(py / "python.exe"), [])), \
                mock.patch.object(subprocess, "Popen", no_fallback):
            with self.assertRaises(media.SandboxUnavailable) as ctx:
                media.decode(self.src, io.BytesIO())
        self.assertIn(ctx.exception.step, ("start", "ready"))
        if ctx.exception.step == "start":
            self.assertEqual(ctx.exception.winerror, 5)            # access denied

    def test_a_process_outside_the_expected_container_is_never_resumed(self):
        from workspace import appcontainer, jobs
        pids = []
        real_contain = jobs.contain

        def contain(pid, memory):
            pids.append(pid)
            return real_contain(pid, memory)
        for facts in ((False, None, 0, 0x2000),                       # not in a container
                      (True, "S-1-15-2-1-2-3-4-5-6-7", 0, 0x1000),    # another container
                      (True, appcontainer.container_sid(), 1, 0x1000),   # a capability
                      (True, appcontainer.container_sid(), 0, 0x2000)):  # Medium
            with self.subTest(facts=facts), mock.patch.object(jobs, "contain", contain):
                error = self.refused(token_facts=mock.Mock(return_value=facts))
                self.assertEqual(error.step, "check")
                self.assertTrue(process_gone(pids[-1]))

    def test_the_real_readers_report_a_process_outside_any_container(self):
        # The refusals above feed token_facts made-up answers; this shows the
        # real readers, unpatched, describe a plain process (as a decoder
        # started outside the container would be) as not contained.
        import ctypes
        from ctypes import wintypes

        from workspace import appcontainer, decode_child
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        proc = subprocess.Popen([PYTHON, "-I", "-c", "import time; time.sleep(60)"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.kill)
        handle = k32.OpenProcess(0x1000, False, proc.pid)         # QUERY_LIMITED_INFORMATION
        self.assertTrue(handle)
        try:
            inside, sid, capabilities, level = appcontainer.token_facts(handle)
        finally:
            k32.CloseHandle(handle)
        self.assertEqual((inside, sid, capabilities), (False, None, 0))
        self.assertGreater(level, appcontainer.LOW_INTEGRITY)
        self.assertGreater(decode_child.process_integrity(proc.pid), decode_child.LOW_INTEGRITY)
        self.assertGreater(decode_child.process_integrity(os.getpid()),
                           decode_child.LOW_INTEGRITY)

    def test_a_job_that_cannot_be_set_refuses_the_import(self):
        from workspace import jobs, media
        with mock.patch.object(jobs, "contain", side_effect=jobs.JobError(5, "denied")), \
                mock.patch.object(subprocess, "Popen", mock.Mock(side_effect=AssertionError)):
            with self.assertRaises(media.SandboxUnavailable) as ctx:
                media.decode(self.src, io.BytesIO())
        self.assertEqual((ctx.exception.step, ctx.exception.winerror), ("contain", 5))

    def test_the_decoder_gets_only_its_pipes_the_null_device_and_the_recording(self):
        from workspace import appcontainer, media
        lists = []
        real_create = appcontainer._create

        def create(argv, env, inherit, std, sid, pi):
            lists.append((list(inherit), list(std), dict(env)))
            return real_create(argv, env, inherit, std, sid, pi)
        with mock.patch.object(appcontainer, "_create", create):
            media.decode(self.src, io.BytesIO())
        (inherit, std, env), = lists
        self.assertEqual(len(inherit), 4)                       # stdin, stdout, NUL, the file
        self.assertEqual(inherit[:3], std)
        self.assertLessEqual(set(env), set(appcontainer.ENV_NAMES) | {
            "PATH", "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"})

    def test_the_decoder_never_opens_the_null_device_itself(self):
        # Windows may refuse the null device to a process in an AppContainer
        # (Windows Server 2025 does: every decode there failed at "ready").
        # The decoder silences its output with the null device the app hands
        # it as stderr, so it decodes even where opening NUL is refused.
        from workspace import decode_child, media
        with mock.patch.object(decode_child, "BOOT", NULL_REFUSED + decode_child.BOOT):
            self.assertEqual(media.decode(self.src, io.BytesIO()), 16000)

    def test_a_throwaway_profile_is_made_found_and_removed(self):
        import secrets

        from workspace import appcontainer
        name = "Unminute.Test." + secrets.token_hex(6)
        self.addCleanup(appcontainer.delete_profile, name)
        self.assertFalse(appcontainer.profile_exists(name))
        sid = appcontainer.ensure_profile(name)
        try:
            self.assertEqual(sid.text(), appcontainer.container_sid(name))
        finally:
            sid.close()
        self.assertTrue(appcontainer.profile_exists(name))
        appcontainer.ensure_profile(name).close()                 # already there: the same
        self.assertTrue(appcontainer.delete_profile(name))
        self.assertFalse(appcontainer.profile_exists(name))
        self.assertFalse(appcontainer.delete_profile(name))       # nothing left to remove

    def test_throwaway_profiles_of_ended_runs_are_swept_and_live_ones_kept(self):
        # A test run that is killed never reaches atexit; the next run removes
        # what it left, but never a profile whose process still runs.
        import secrets

        from workspace import appcontainer
        ended = subprocess.Popen([PYTHON, "-I", "-c", "pass"])
        ended.wait(timeout=30)
        left = f"Unminute.Test.{ended.pid}.{secrets.token_hex(4)}"
        live = f"Unminute.Test.{os.getpid()}.{secrets.token_hex(4)}"
        other = "Unminute.Test." + secrets.token_hex(6)           # not a name profile() makes
        for name in (left, live, other):
            self.addCleanup(appcontainer.delete_profile, name)
            appcontainer.ensure_profile(name).close()
        swept = decoder_runtime.sweep_profiles()
        self.assertEqual([n.lower() for n in swept], [left.lower()])
        self.assertFalse(appcontainer.profile_exists(left))
        self.assertTrue(appcontainer.profile_exists(live))
        self.assertTrue(appcontainer.profile_exists(other))


@unittest.skipUnless(WINDOWS and HAVE_DEPS, "Windows decoder process")
class CheckDecoderTests(unittest.TestCase):
    """--check-decoder (#299): IT's check that the container decodes, with
    the failed step and Windows error only, never a message or a path."""

    def test_it_decodes_synthetic_silence_in_the_container(self):
        import workspace.__main__ as main
        self.assertEqual(main.check_decoder(account=False), ({"decoder": "ok"}, 0))

    def test_it_names_the_step_and_nothing_else(self):
        import workspace.__main__ as main
        from workspace import media
        with mock.patch.object(media, "_launch", side_effect=media.SandboxUnavailable("start", 5)):
            self.assertEqual(main.check_decoder(account=False),
                             ({"decoder": "failed", "step": "start", "winerror": 5}, 1))
        with mock.patch.object(media, "decode", side_effect=RuntimeError("C:\\Users\\someone")):
            self.assertEqual(main.check_decoder(account=False),
                             ({"decoder": "failed", "step": "decode"}, 1))

    def test_local_system_starts_nothing(self):
        import workspace.__main__ as main
        from workspace import media
        with mock.patch.object(media, "decode", side_effect=AssertionError("started")):
            self.assertEqual(main.check_decoder(account=True), ({"decoder": "system_account"}, 1))
            self.assertEqual(main.check_decoder(account=None), ({"decoder": "account_unknown"}, 1))


class AppContainerNamesTests(unittest.TestCase):
    def test_the_profile_name_and_steps_are_fixed(self):
        # The uninstaller and offboarding remove the profile by this name
        # (tests/test_consistency.py checks uninstall.ps1's copy). Read from
        # the source: this module points PROFILE at a throwaway name.
        import re

        from workspace import appcontainer, diag
        source = Path(appcontainer.__file__).read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'(?m)^PROFILE = "([^"]*)"', source), ["Unminute.Decoder"])
        self.assertEqual(appcontainer.ALL_APPLICATION_PACKAGES, "S-1-15-2-1")
        for step in appcontainer.STEPS:
            self.assertEqual(diag._clean("step", step), step)
        self.assertEqual(diag._clean("step", "C:\\Users\\someone"), "?")

if __name__ == "__main__":
    unittest.main()
