# Encrypted audio files (#111 C1, workspace/audiocrypt.py; design in
# docs/ENCRYPTION.md, "Audio files"): random-access reads, every block
# authenticated, a wrong key refused on opening, cut, reordered, extended or
# moved files refused, and no nonce used twice after a failed write.
# Synthetic samples only.
#   python -m unittest discover -s tests -v

import array
import importlib.util
import os
import random
import shutil
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workspace import audiocrypt  # noqa: E402

HAVE_CRYPTOGRAPHY = importlib.util.find_spec("cryptography") is not None

KEY = bytes(range(32))
CTX = ("meeting-ref-1", "audio")
F = audiocrypt.FRAMES_PER_BLOCK
START = audiocrypt.DATA_START                  # header (64) and header tag (16)
# Three int16 frames (1, -2, 3), mono, 16 kHz, file id 0x11 * 32, under KEY for
# ("m1", "audio"): header, header tag and one last block, built from HKDF and
# AESGCM directly (scratch script c1_vectors.py in the C1 pull request).
PINNED_FILE = bytes.fromhex(
    "554d413101020100803e000000400000" + "11" * 32 + "00" * 16
    + "e7d355f20a0905be2853d7f727f6e4da" + "576abcb102cce80c72bf9f4fa871c4ceb34b467db36c")


def samples(frames, channels=1, fmt="<f"):
    """Distinct synthetic samples."""
    n = frames * channels
    if fmt == "<f":
        return struct.pack(f"<{n}f", *((i * 0.25) % 1000.0 for i in range(n)))
    return struct.pack(f"<{n}h", *(((i * 7) % 65536) - 32768 for i in range(n)))


def file_key(file_id, ctx=CTX, key=KEY):
    """The documented per-file key: HKDF-SHA256, no salt, info = label, a
    zero byte, the file id and the context, each part typed and sized."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    info = b"unminute/v1/audio\0b" + struct.pack(">I", len(file_id)) + file_id + b"".join(
        b"s" + struct.pack(">I", len(p.encode())) + p.encode() for p in ctx)
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(key)


class FailingFile:
    """Wraps a file; one write raises after writing part of its bytes."""

    def __init__(self, f, fail_on):
        self._f, self.calls, self.fail_on = f, 0, fail_on

    def write(self, data):
        self.calls += 1
        if self.calls == self.fail_on:
            self._f.write(bytes(data)[:len(data) // 2])
            raise OSError(28, "No space left on device")
        return self._f.write(data)

    def __getattr__(self, name):
        return getattr(self._f, name)


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "needs cryptography")
class AudioCryptTests(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="audiocrypt-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.n = 0

    def path(self):
        self.n += 1
        return self.dir / f"{self.n}.uma"

    def write(self, data, fmt="float32", channels=1, rate=16000, ctx=CTX, key=KEY):
        p = self.path()
        with audiocrypt.AudioWriter(p, key, ctx, fmt, channels, rate) as w:
            w.write(data)
        return p

    def edit(self, p, fn):
        data = bytearray(p.read_bytes())
        out = self.path()
        out.write_bytes(bytes(fn(data)))
        return out

    def test_round_trip_at_every_block_edge(self):
        for frames in (0, 1, F - 1, F, F + 1, 2 * F, 3 * F + 5):
            with self.subTest(frames=frames):
                data = samples(frames)
                p = self.write(data)
                with audiocrypt.AudioReader(p, KEY, CTX) as r:
                    self.assertTrue(r.complete)
                    self.assertEqual(frames, len(r))
                    self.assertEqual(("float32", 1, 16000), (r.sample_format, r.channels,
                                                             r.rate))
                    self.assertEqual(data, r.read(0, frames + 10))
                    self.assertEqual(frames, r.verify())
                # Full blocks, then a last block of fewer than a full block.
                whole, rest = divmod(frames, F)
                self.assertEqual(START + whole * (F * 4 + 16) + rest * 4 + 16, p.stat().st_size)

    def test_int16_stereo(self):
        data = samples(F + 3, channels=2, fmt="<h")
        p = self.write(data, "int16", 2, 48000)
        with audiocrypt.AudioReader(p, KEY, CTX) as r:
            self.assertEqual(("int16", 2, 48000, F + 3),
                             (r.sample_format, r.channels, r.rate, len(r)))
            self.assertEqual(data, r.read(0, F + 3))
            self.assertEqual(data[(F - 1) * 4:(F + 1) * 4], r.read(F - 1, 2))

    def test_random_access_across_blocks(self):
        frames = 3 * F + 100
        data = samples(frames)
        p = self.write(data)
        with audiocrypt.AudioReader(p, KEY, CTX) as r:
            for start, count in ((0, 1), (F - 1, 2), (F, F), (F - 5, F + 10), (3 * F, 100),
                                 (3 * F + 99, 50), (frames, 5), (17, 0), (2 * F + 3, 2 * F)):
                with self.subTest(start=start, count=count):
                    expected = data[start * 4:min(frames, start + count) * 4]
                    self.assertEqual(expected, r.read(start, count))
            for start, count in ((frames + 1, 1), (-1, 1), (0, -1)):
                with self.subTest(start=start, count=count), self.assertRaises(ValueError):
                    r.read(start, count)

    def test_reads_into_a_buffer_of_samples(self):
        data = samples(F + 200)
        p = self.write(data)
        with audiocrypt.AudioReader(p, KEY, CTX) as r:
            got = r.read(0, 10)
            self.assertIsInstance(got, bytearray)                   # writable
            buf = array.array("f", bytes(4 * 100))
            self.assertEqual(100, r.readinto(buf, F - 20))          # across two blocks
            self.assertEqual(data[(F - 20) * 4:(F + 80) * 4], buf.tobytes())
            self.assertEqual(50, r.readinto(buf, F + 150))          # fewer at the end
            self.assertEqual(data[(F + 150) * 4:], buf.tobytes()[:200])
            with self.assertRaises(ValueError):
                r.readinto(bytearray(6), 0)                         # not whole frames
            with self.assertRaises(ValueError):
                r.readinto(buf, F + 201)

    def test_writes_in_any_pieces_and_after_a_flush(self):
        data = samples(2 * F + 7)
        p = self.path()
        with audiocrypt.AudioWriter(p, KEY, CTX) as w:
            w.write(memoryview(data)[:4 * 1000])
            w.flush()                                     # the part block waits
            for i in range(4 * 1000, len(data), 4 * 1000):
                w.write(memoryview(data)[i:i + 4 * 1000])
            self.assertEqual(2 * F + 7, w.frames)
        with audiocrypt.AudioReader(p, KEY, CTX) as r:
            self.assertEqual(data, r.read(0, len(r)))

    def test_writes_take_bytes_or_samples_of_the_files_format(self):
        p = self.path()
        with audiocrypt.AudioWriter(p, KEY, CTX) as w:
            w.write(array.array("f", [0.5, -0.5]))
            w.write(bytearray(8))
            for bad in (array.array("d", [0.5]), array.array("h", [1, 2])):
                with self.subTest(bad=bad.typecode), self.assertRaises(TypeError):
                    w.write(bad)
        with audiocrypt.AudioReader(p, KEY, CTX) as r:
            self.assertEqual(4, len(r))
        with audiocrypt.AudioWriter(self.path(), KEY, CTX, "int16") as w:
            w.write(array.array("h", [1, 2]))
            with self.assertRaises(TypeError):
                w.write(array.array("f", [0.5]))

    def test_the_pinned_file_opens(self):
        p = self.path()
        p.write_bytes(PINNED_FILE)
        with audiocrypt.AudioReader(p, KEY, ("m1", "audio")) as r:
            self.assertEqual((True, 3, "int16"), (r.complete, len(r), r.sample_format))
            self.assertEqual(struct.pack("<3h", 1, -2, 3), r.read(0, 3))

    def test_every_block_is_aes_gcm_as_documented(self):
        # Per-file key from HKDF (the file id in its info), header tag under
        # nonce ff ff ff ff then zeros, block nonce the index, associated data
        # header, index, last-block flag.
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        data = samples(2 * F + 3)
        raw = self.write(data).read_bytes()
        header, file_id = raw[:64], raw[16:48]
        aead = AESGCM(file_key(file_id))
        self.assertEqual(b"", aead.decrypt(b"\xff" * 4 + bytes(8), raw[64:80], header))
        record = F * 4 + 16
        plain = b""
        for index, last in ((0, 0), (1, 0), (2, 1)):
            rec = raw[START + index * record:START + (index + 1) * record]
            plain += aead.decrypt(bytes(4) + struct.pack(">Q", index), rec,
                                  header + struct.pack(">QB", index, last))
        self.assertEqual(data, plain)

    def test_the_header_is_as_documented(self):
        p = self.write(samples(5))
        magic, version, fmt, channels, rate, per_block, file_id, reserved = \
            struct.unpack("<4sBBHII32s16s", p.read_bytes()[:64])
        self.assertEqual((b"UMA1", 1, 1, 1, 16000, F, bytes(16)),
                         (magic, version, fmt, channels, rate, per_block, reserved))
        other = self.write(samples(5)).read_bytes()[16:48]
        # The whole 32-byte id is random: both halves differ between files.
        self.assertNotEqual(file_id[:16], other[:16])
        self.assertNotEqual(file_id[16:], other[16:])
        self.assertNotIn(samples(5), p.read_bytes())

    def test_a_writer_never_reuses_a_file(self):
        p = self.write(samples(5))
        with self.assertRaises(FileExistsError):
            audiocrypt.AudioWriter(p, KEY, CTX)
        with self.assertRaises(ValueError):
            with audiocrypt.AudioWriter(self.path(), KEY, CTX, "int16", 2) as w:
                w.write(b"\0\0")                                  # half a frame
        for bad in ({"sample_format": "pcm24"}, {"channels": 0}, {"channels": 9},
                    {"rate": 7999}, {"rate": 192001}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                audiocrypt.AudioWriter(self.path(), KEY, CTX, **bad)

    def test_a_failed_write_never_reuses_a_nonce(self):
        # The second block's write fails part way (a full disk). The block's
        # index is spent, nothing more can be written, and closing refuses to
        # seal the rest as the last block under that index.
        data = samples(3 * F)
        p = self.path()
        w = audiocrypt.AudioWriter(p, KEY, CTX)
        w._f = FailingFile(w._f, fail_on=2)                  # header was call 0
        with self.assertRaises(OSError):
            w.write(data)
        self.assertEqual(2, w._index)
        with self.assertRaises(ValueError):
            w.write(samples(1))
        with self.assertRaises(ValueError):
            w.close()
        with self.assertRaises(audiocrypt.AudioCryptError):
            audiocrypt.AudioReader(p, KEY, CTX)
        with audiocrypt.AudioReader(p, KEY, CTX, allow_incomplete=True) as r:
            self.assertEqual((False, F), (r.complete, len(r)))
            self.assertEqual(data[:F * 4], r.read(0, F))
        # A failure while sealing the last block leaves the file unfinished.
        q = self.path()
        w = audiocrypt.AudioWriter(q, KEY, CTX)
        w.write(samples(10))
        w._f = FailingFile(w._f, fail_on=1)
        with self.assertRaises(OSError):
            w.close()
        self.assertIsNone(w._f)                               # closed, not left open
        os.replace(q, self.dir / "moved-after-failure.uma")   # Windows: not open
        with self.assertRaises(audiocrypt.AudioCryptError):
            audiocrypt.AudioReader(self.dir / "moved-after-failure.uma", KEY, CTX)

    def test_an_interrupted_file_opens_only_when_allowed(self):
        data = samples(2 * F + 10)
        p = self.path()
        with self.assertRaises(RuntimeError):
            with audiocrypt.AudioWriter(p, KEY, CTX) as w:
                w.write(data)
                w.flush()
                raise RuntimeError("the app stopped")
        self.assertEqual(START + 2 * (F * 4 + 16), p.stat().st_size)    # whole blocks only
        with self.assertRaises(audiocrypt.AudioCryptError):
            audiocrypt.AudioReader(p, KEY, CTX)
        with audiocrypt.AudioReader(p, KEY, CTX, allow_incomplete=True) as r:
            self.assertEqual((False, 2 * F), (r.complete, len(r)))
            self.assertEqual(data[:2 * F * 4], r.read(0, 3 * F))
        # A write cut off part way through a block is left out.
        torn = self.edit(p, lambda b: b + bytes(1000))
        with audiocrypt.AudioReader(torn, KEY, CTX, allow_incomplete=True) as r:
            self.assertEqual((False, 2 * F), (r.complete, len(r)))
        with self.assertRaises(audiocrypt.AudioCryptError):
            audiocrypt.AudioReader(torn, KEY, CTX)

    def test_a_wrong_key_fails_on_opening_even_for_an_interrupted_file(self):
        short = self.write(samples(500))                        # under one block
        long = self.write(samples(3 * F))
        header_only = self.edit(long, lambda b: b[:START])
        for p in (short, long, header_only):
            for key, ctx in ((bytes(32), CTX), (KEY, ("meeting-ref-2", "audio"))):
                with self.subTest(p=p.name, ctx=ctx), \
                        self.assertRaises(audiocrypt.AudioCryptError) as cm:
                    audiocrypt.AudioReader(p, key, ctx, allow_incomplete=True).close()
                self.assertIn("the key is wrong", str(cm.exception))
        with audiocrypt.AudioReader(header_only, KEY, CTX, allow_incomplete=True) as r:
            self.assertEqual((False, 0), (r.complete, len(r)))

    def test_cut_or_extended_files_are_refused(self):
        p = self.write(samples(2 * F + 10))
        size = p.stat().st_size
        record = F * 4 + 16
        cuts = {
            "last block removed": lambda b: b[:START + 2 * record],
            "last block and one more removed": lambda b: b[:START + record],
            "a few bytes cut": lambda b: b[:-7],
            "one byte cut": lambda b: b[:-1],
            "header and tag only": lambda b: b[:START],
            "header only": lambda b: b[:64],
            "part header": lambda b: b[:40],
            "block repeated after the end": lambda b: b + b[START:START + record],
            "bytes added after the end": lambda b: b + bytes(16),
            "last block repeated": lambda b: b + b[START + 2 * record:],
        }
        for name, fn in cuts.items():
            with self.subTest(name), self.assertRaises(audiocrypt.AudioCryptError):
                audiocrypt.AudioReader(self.edit(p, fn), KEY, CTX).close()
        self.assertEqual(size, p.stat().st_size)
        # A changed last block fails its own check, and says so.
        def flip_last(b):
            b[-5] ^= 1
            return b
        with self.assertRaises(audiocrypt.AudioCryptError) as cm:
            audiocrypt.AudioReader(self.edit(p, flip_last), KEY, CTX).close()
        self.assertIn("damaged or not where it was stored", str(cm.exception))

    def test_reordered_or_changed_blocks_are_refused_when_read(self):
        data = samples(3 * F + 10)
        p = self.write(data)
        record = F * 4 + 16

        def swap(b):
            first, second = b[START:START + record], b[START + record:START + 2 * record]
            b[START:START + record], b[START + record:START + 2 * record] = second, first
            return b

        def flip(b):
            b[START + record + 100] ^= 1
            return b

        # Swapped: the first two blocks fail; changed: only the second does.
        for name, fn, good, bad in (("swapped", swap, 2 * F, 0), ("changed", flip, 0, F)):
            with self.subTest(name), audiocrypt.AudioReader(self.edit(p, fn), KEY, CTX) as r:
                self.assertEqual(data[good * 4:(good + 10) * 4], r.read(good, 10))
                for start in (bad, bad + F - 1, F + 5):
                    with self.assertRaises(audiocrypt.AudioCryptError):
                        r.read(start, 1)
                with self.assertRaises(audiocrypt.AudioCryptError):
                    r.verify()

    def test_a_changed_header_or_header_tag_is_refused(self):
        p = self.write(samples(F + 3))
        for i in range(START):
            changed = self.edit(p, lambda b, i=i: b[:i] + bytes([b[i] ^ 1]) + b[i + 1:])
            for allow in (False, True):
                with self.subTest(byte=i, allow=allow), \
                        self.assertRaises(audiocrypt.AudioCryptError):
                    audiocrypt.AudioReader(changed, KEY, CTX, allow_incomplete=allow).close()

    def test_a_block_from_another_file_does_not_fit(self):
        data = samples(F + 3)
        a, b = self.write(data), self.write(data)
        mixed = self.edit(a, lambda x: x[:START] + b.read_bytes()[START:])
        with self.assertRaises(audiocrypt.AudioCryptError):
            audiocrypt.AudioReader(mixed, KEY, CTX).close()

    def test_the_wrong_key_or_place_does_not_open(self):
        p = self.write(samples(F + 3))
        for key, ctx in ((bytes(32), CTX), (KEY, ("meeting-ref-2", "audio")),
                         (KEY, ("meeting-ref-1", "chunk", 0))):
            with self.subTest(ctx=ctx), self.assertRaises(audiocrypt.AudioCryptError) as cm:
                audiocrypt.AudioReader(p, key, ctx).close()
            self.assertIn("the key is wrong", str(cm.exception))

    def test_errors_name_nothing(self):
        p = self.dir / "Client Kowhai Ltd.uma"
        with audiocrypt.AudioWriter(p, KEY, ("Kowhai", "audio")) as w:
            w.write(samples(10))
        with self.assertRaises(audiocrypt.AudioCryptError) as cm:
            audiocrypt.AudioReader(p, KEY, ("other", "audio")).close()
        message = str(cm.exception)
        self.assertNotIn("Kowhai", message)
        self.assertNotIn(str(self.dir), message)
        self.assertIsNone(cm.exception.__cause__)

    def test_not_an_audio_file(self):
        for data in (b"", b"RIFF" + bytes(60), b"UMA1" + bytes(60), b"UMA2" + bytes(80)):
            p = self.path()
            p.write_bytes(data)
            with self.subTest(n=len(data)), self.assertRaises(audiocrypt.AudioCryptError):
                audiocrypt.AudioReader(p, KEY, CTX).close()

    def test_reader_closes_its_file_when_it_refuses(self):
        p = self.write(samples(F + 3))
        try:
            audiocrypt.AudioReader(p, bytes(32), CTX)
            self.fail("opened with the wrong key")
        except audiocrypt.AudioCryptError:
            # While the exception (and the reader it interrupted) is still
            # alive: Windows refuses to rename a file that is open.
            os.replace(p, self.dir / "moved.uma")

    def craft(self, reserved=bytes(16), per_block=F, fmt=2, channels=1, rate=16000):
        """A correctly sealed file with chosen header fields (raw primitives)."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        file_id = b"\x22" * 32
        header = struct.pack("<4sBBHII32s16s", b"UMA1", 1, fmt, channels, rate, per_block,
                             file_id, reserved)
        aead = AESGCM(file_key(file_id))
        tag = aead.encrypt(b"\xff" * 4 + bytes(8), b"", header)
        rec = aead.encrypt(bytes(12), bytes(2 * channels), header + struct.pack(">QB", 0, 1))
        p = self.path()
        p.write_bytes(header + tag + rec)
        return p

    def test_header_values_outside_the_format_are_refused_even_if_sealed(self):
        with audiocrypt.AudioReader(self.craft(), KEY, CTX) as r:     # the control
            self.assertEqual(1, len(r))
        for bad in ({"reserved": b"\x01" + bytes(15)}, {"per_block": F // 2}, {"fmt": 3},
                    {"channels": 0}, {"channels": 9}, {"rate": 7999}):
            with self.subTest(bad=bad), self.assertRaises(audiocrypt.AudioCryptError) as cm:
                audiocrypt.AudioReader(self.craft(**bad), KEY, CTX).close()
            self.assertIn("header", str(cm.exception))

    def test_closing_forgets_the_last_block_read(self):
        p = self.write(samples(2 * F + 3))
        r = audiocrypt.AudioReader(p, KEY, CTX)
        r.read(5, 1)
        self.assertEqual(0, r._cached[0])
        r.close()
        self.assertEqual((-1, b""), r._cached)
        with self.assertRaises(ValueError):
            r.read(5, 1)

    def test_reads_from_several_threads(self):
        frames = 4 * F + 77
        data = samples(frames)
        p = self.write(data)
        errors, wrong = [], []
        with audiocrypt.AudioReader(p, KEY, CTX) as r:
            def reader(seed):
                rnd = random.Random(seed)
                for _ in range(150):
                    start = rnd.randrange(frames)
                    count = rnd.randrange(1, 3 * F)
                    try:
                        got = r.read(start, count)
                    except Exception as exc:              # noqa: BLE001 - collected
                        errors.append(exc)
                        return
                    if got != data[start * 4:min(frames, start + count) * 4]:
                        wrong.append((start, count))
            threads = [threading.Thread(target=reader, args=(s,)) for s in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(120)
        self.assertEqual(([], []), (errors, wrong))
        # Closing while others read: they get a plain refusal, nothing else.
        r = audiocrypt.AudioReader(p, KEY, CTX)
        seen = []

        def keep_reading():
            for _ in range(2000):
                try:
                    r.read(random.randrange(frames), 100)
                except (ValueError, audiocrypt.AudioCryptError):
                    seen.append("refused")
                    return
                except Exception as exc:                  # noqa: BLE001 - collected
                    seen.append(exc)
                    return
        t = threading.Thread(target=keep_reading)
        t.start()
        r.close()
        t.join(60)
        self.assertTrue(all(s == "refused" for s in seen), seen)


if __name__ == "__main__":
    unittest.main()
