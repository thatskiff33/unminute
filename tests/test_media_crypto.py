# Encrypted decoded meeting audio (#111 C3a, docs/ENCRYPTION.md "Audio files"):
# no plain copy at rest, the reader keeps the old reader's semantics, the
# file is authenticated in full once at import, runs check its length and
# file id only, a damaged block stops a run without counting a failure, and
# the meeting's audio key reaches the worker only inside the diarize request.
# Synthetic audio only (P1, P2).
#   python -m unittest discover -s tests -v

import base64
import hashlib
import importlib.util
import os
import pickle
import re
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HAVE_NUMPY = all(importlib.util.find_spec(m) for m in ("numpy", "cryptography"))
HAVE_AV = HAVE_NUMPY and importlib.util.find_spec("av") is not None
SR = 16000
BLOCK = 16384                     # audiocrypt.FRAMES_PER_BLOCK
# Known answer for test_a_decoded_audio_file_has_a_known_answer (key 0..31, file id 32..63,
# context ("decoded", "0123456789abcdef" * 2), samples 0, 1/8, 2/8, 3/8).
KAT_SHA256 = "ea192465193e00416af51823d07d59797e6ac74d81f9606f6e9ca30a6c4d7b19"

if HAVE_NUMPY:
    import numpy as np

    from workspace import audiocrypt, media, runner, speakers
    from workspace.asr_engines import Segment
    from workspace.store import Store


sys.path.append(str(Path(__file__).resolve().parents[1] / "tools"))
import decoder_runtime  # noqa: E402  (tools/decoder_runtime.py)


def setUpModule():
    # #299: the decoder runs in an AppContainer that cannot read this Python:
    # decodes here use a scratch runtime it can read and a throwaway profile.
    decoder_runtime.use()


def tearDownModule():
    decoder_runtime.stop()


def noise(seconds, seed=7):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * SR)) * 0.1).astype(np.float32)


def write_meeting_audio(store, media_root, mid, samples):
    """The meeting's audio.uma as the import writes it (its key, its
    context). Returns the file id."""
    folder = media.meeting_folder(media_root, mid)
    folder.mkdir(parents=True, exist_ok=True)
    with audiocrypt.AudioWriter(folder / media.AUDIO_FILE, store.meeting_key(mid, "audio"),
                                media.audio_context(mid), "float32", 1, SR) as w:
        w.write(samples)
        file_id = w.file_id
    return file_id


def flip(path, offset):
    data = bytearray(path.read_bytes())
    data[offset] ^= 0x01
    path.write_bytes(bytes(data))


def block_offset(index):
    """Where block `index` of a mono float32 file starts (a byte inside it)."""
    return audiocrypt.DATA_START + index * (BLOCK * 4 + audiocrypt.TAG_BYTES) + 10


class OneWordEngine:
    """Hears one word per chunk; records how many chunks it was given."""
    device, precision = "cpu", "int8"

    def __init__(self):
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return [Segment(0.1, 0.5, f"word{self.calls}", ((0.1, 0.5, " word"),))], len(audio) / SR


@unittest.skipUnless(HAVE_NUMPY, "numpy/cryptography not installed")
class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.key = bytes(range(32))

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_context_is_pinned(self):
        # Changing it would make every stored meeting's audio unreadable.
        self.assertEqual(media.audio_context("0" * 32), ("decoded", "0" * 32))
        self.assertEqual(media.AUDIO_FILE, "audio.uma")

    def test_a_decoded_audio_file_has_a_known_answer(self):
        # Crypto review of #281: the tuple above does not pin how the context
        # is encoded into the file key; these bytes do (with the header, the
        # nonces and the associated data). A change fails here, not on stored
        # meetings. Fixed key, file id and samples; nothing else is random.
        mid = "0123456789abcdef" * 2
        path = self.root / "kat.uma"
        with mock.patch.object(audiocrypt.secrets, "token_bytes",
                               return_value=bytes(range(32, 64))):
            with audiocrypt.AudioWriter(path, self.key, media.audio_context(mid), "float32",
                                        1, SR) as w:
                w.write(np.arange(4, dtype=np.float32) / 8)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), KAT_SHA256)
        reader = media.AudioReader(path, self.key, media.audio_context(mid))
        try:
            np.testing.assert_array_equal(reader.read(0, 4), np.arange(4, dtype=np.float32) / 8)
        finally:
            reader.close()
        with self.assertRaises(audiocrypt.AudioCryptError):     # another meeting's context
            media.AudioReader(path, self.key, media.audio_context("f" * 32))

    def test_reader_matches_the_old_memory_mapped_reader(self):
        samples = noise((3 * BLOCK + 123) / SR)
        n = len(samples)
        plain = self.root / "plain.f32"
        samples.astype("<f4").tofile(plain)
        old = np.memmap(plain, dtype="<f4", mode="r")       # what media.AudioReader was

        def old_read(start, end):
            start, end = max(0, int(start)), min(len(old), int(end))
            return np.array(old[start:end], dtype=np.float32)
        path = self.root / "audio.uma"
        with audiocrypt.AudioWriter(path, self.key, ("decoded", "m"), "float32", 1, SR) as w:
            w.write(samples)
        reader = media.AudioReader(path, self.key, ("decoded", "m"))
        try:
            self.assertEqual(len(reader), n)
            cases = [(-5, 10), (n - 10, n + 100), (5, 5), (10, 5), (0, 1), (n - 1, n),
                     (BLOCK - 3, BLOCK + 3), (2 * BLOCK, 3 * BLOCK), (0, n), (n + 5, n + 10),
                     (BLOCK, BLOCK + 1), (1.9, 7.2)]
            for start, end in cases:
                with self.subTest(start=start, end=end):
                    new = reader.read(start, end)
                    self.assertEqual(new.dtype, np.float32)
                    np.testing.assert_array_equal(new, old_read(start, end))
            # One deliberate difference: a negative end was a Python slice from
            # the end in the memory map; it is now clamped (nothing). No caller
            # passes one (times are checked, chunk ranges start at 0).
            self.assertEqual(len(reader.read(-20, -10)), 0)
            first = reader.read(100, 200)
            first[:] = 9.0                                    # a copy: the next read is unchanged
            np.testing.assert_array_equal(reader.read(100, 200), old_read(100, 200))
            self.assertEqual(reader.verify(), n)
        finally:
            reader.close()
            old._mmap.close()                                 # Windows: release the file
        reader.close()                                        # idempotent

    def test_the_reader_refuses_audio_that_is_not_the_decoded_format(self):
        path = self.root / "audio.uma"
        with audiocrypt.AudioWriter(path, self.key, ("decoded", "m"), "int16", 1, SR) as w:
            w.write(np.zeros(10, dtype="<i2"))
        with self.assertRaises(audiocrypt.AudioCryptError):
            media.AudioReader(path, self.key, ("decoded", "m"))
        with self.assertRaises(audiocrypt.AudioCryptError):  # another meeting's context
            path2 = self.root / "b.uma"
            with audiocrypt.AudioWriter(path2, self.key, ("decoded", "m"), "float32", 1, SR) as w:
                w.write(np.zeros(10, dtype=np.float32))
            media.AudioReader(path2, self.key, ("decoded", "other"))

    def test_the_audio_reference_never_shows_its_key(self):
        ref = media.AudioRef(self.root / "a.uma", self.key, ("decoded", "m"), 16000)
        for text in (repr(ref), str(ref)):
            self.assertNotIn(self.key.hex(), text)
            self.assertNotIn(repr(self.key), text)

    def test_no_plain_audio_reader_or_incomplete_files_in_the_app(self):
        # The decoded audio is never memory-mapped or written as raw float32
        # again, and only recovery may open an interrupted file (C3b: none).
        found = {}
        for path in sorted((ROOT / "workspace").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for name, pattern in (("memmap", r"np\.memmap|numpy\.memmap"),
                                  ("plain .f32", r"\.f32\b"),
                                  ("allow_incomplete=True", r"allow_incomplete\s*=\s*True")):
                if name == "allow_incomplete=True" and path.name == "audiocrypt.py":
                    continue
                if re.search(pattern, text):
                    found.setdefault(name, []).append(path.name)
        self.assertEqual({}, found)


@unittest.skipUnless(HAVE_NUMPY, "numpy/cryptography not installed")
class RunTests(unittest.TestCase):
    """Runs over encrypted audio, without the decoder (the file is written as
    an import writes it)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.media = self.root / "media"
        self.store = Store(self.root / "db.sqlite", protector=keyprotect.TestProtector())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def meeting(self, seconds=100, original_path=None):
        mid = self.store.create_meeting("Synthetic")
        samples = noise(seconds)
        file_id = write_meeting_audio(self.store, self.media, mid, samples)
        sid = self.store.add_source(mid, original_path=original_path, original_sha256="a" * 64,
                                    original_size=1, original_mtime_ns=0,
                                    audio_id=file_id.hex(), samples=len(samples),
                                    sample_rate=SR)
        step = 20 * SR
        plan = [(a, min(a + step, len(samples)), a, min(a + step, len(samples)))
                for a in range(0, len(samples), step)]
        vid = self.store.ensure_version(mid, sid, engine="fake", model="fake",
                                        model_revision="1", precision="int8", config={},
                                        chunk_plan=plan)
        return mid, sid, vid

    def run_meeting(self, mid, vid, engine=None):
        reader = media.open_audio(self.store, self.media, mid)
        try:
            return runner.run_version(self.store, mid, vid, reader, engine or OneWordEngine())
        finally:
            reader.close()

    def test_a_run_makes_no_full_pass_over_the_audio(self):
        mid, _, vid = self.meeting()
        with mock.patch.object(media, "sha256_file", side_effect=AssertionError("full pass")):
            self.assertEqual(self.run_meeting(mid, vid), runner.RunResult.DONE)
        self.assertEqual(len(self.store.chunks(vid, status="done")), 5)

    def test_a_damaged_block_stops_the_run_without_counting_a_failure(self):
        mid, _, vid = self.meeting()
        path = media.audio_path(self.media, mid)
        flip(path, block_offset(25 * SR // BLOCK))           # inside part 2 (20 s to 40 s)
        engine = OneWordEngine()
        self.assertEqual(self.run_meeting(mid, vid, engine), runner.RunResult.SOURCE_CHANGED)
        chunks = {c["idx"]: c for c in self.store.chunks(vid)}
        self.assertEqual(chunks[0]["status"], "done")        # committed before it: kept (D1)
        self.assertEqual(chunks[1]["status"], "pending")
        self.assertEqual(chunks[1]["failures"], 0)           # not the chunk's fault
        self.assertEqual([a["result"] for a in self.store.attempts(vid, 1)], ["stopped"])
        self.assertEqual(engine.calls, 1)                    # never reached the engine
        m = self.store.meeting(mid)
        self.assertEqual(m["phase"], "error")
        self.assertEqual(m["last_error"], runner.AUDIO_DAMAGED_KEPT)
        self.assertEqual(len(self.store.transcript(vid)), 1)

    def test_an_older_copy_or_another_file_is_never_transcribed(self):
        mid, _, vid = self.meeting(seconds=30)
        path = media.audio_path(self.media, mid)
        older = path.read_bytes()
        write_meeting_audio(self.store, self.root / "copy", mid, noise(30))
        # The same meeting's key and context, the same length: only the file
        # id tells it from the one imported (rollback, C1 review F9).
        os.replace(media.audio_path(self.root / "copy", mid), path)
        self.assertNotEqual(older[:64], path.read_bytes()[:64])
        engine = OneWordEngine()
        self.assertEqual(self.run_meeting(mid, vid, engine), runner.RunResult.SOURCE_CHANGED)
        self.assertEqual(engine.calls, 0)
        self.assertEqual(self.store.chunks(vid, status="done"), [])

    def test_a_shorter_file_is_never_transcribed(self):
        mid, _, vid = self.meeting(seconds=30)
        media.audio_path(self.media, mid).unlink()
        write_meeting_audio(self.store, self.media, mid, noise(29))
        self.assertEqual(self.run_meeting(mid, vid), runner.RunResult.SOURCE_CHANGED)

    def test_the_damaged_audio_note_depends_on_the_source(self):
        # "Import the recording again" only when that is possible (#111 C3a).
        original = self.root / "synthetic-original.wav"
        original.write_bytes(b"synthetic")
        cases = []
        for path in (str(original), None):
            mid, sid, vid = self.meeting(seconds=30, original_path=path)
            flip(media.audio_path(self.media, mid), block_offset(0))
            self.assertEqual(self.run_meeting(mid, vid), runner.RunResult.SOURCE_CHANGED)
            cases.append(self.store.meeting(mid)["last_error"])
        self.assertEqual(cases, [runner.AUDIO_DAMAGED_REIMPORT, runner.AUDIO_DAMAGED_KEPT])
        source = self.store.sources(mid)[0] | {"original_path": str(original)}
        self.assertEqual(runner.damaged_audio_note(source), runner.AUDIO_DAMAGED_REIMPORT)
        original.unlink()                                    # deleted or moved since
        self.assertEqual(runner.damaged_audio_note(source), runner.AUDIO_DAMAGED_KEPT)
        self.assertEqual(runner.damaged_audio_note(None), runner.AUDIO_DAMAGED_KEPT)
        damaged = {**source, "original_path": "[Could not be read]",
                   "damaged": ("original_path",)}
        self.assertEqual(runner.damaged_audio_note(damaged), runner.AUDIO_DAMAGED_KEPT)

    def test_the_missing_audio_note_depends_on_the_source(self):
        # Security review of #281: a microphone recording cannot be imported
        # again, so missing audio says so only when it can.
        original = self.root / "synthetic-original.wav"
        original.write_bytes(b"synthetic")
        _, sid, _ = self.meeting(seconds=30, original_path=str(original))
        source = self.store.source(sid)
        self.assertEqual(runner.missing_audio_note(source), runner.AUDIO_MISSING_REIMPORT)
        self.assertIn("Import the recording again", runner.AUDIO_MISSING_REIMPORT)
        original.unlink()                                    # deleted or moved since
        for case in (source, None, {**source, "original_path": None}):
            self.assertEqual(runner.missing_audio_note(case), runner.AUDIO_MISSING_KEPT)
        self.assertNotIn("Import", runner.AUDIO_MISSING_KEPT)


@unittest.skipUnless(HAVE_AV, "numpy/cryptography/PyAV not installed")
class ImportTests(unittest.TestCase):
    MARKER_SAMPLES = 4096

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.media = self.root / "ws" / "media"
        self.store = Store(self.root / "ws" / "workspace.db",
                           protector=keyprotect.TestProtector())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def marked_wav(self, seconds=6):
        """A synthetic WAV holding a fixed 4,096-sample pattern, unlikely by
        chance. Returns (path, the pattern as decoded float32 bytes)."""
        rng = np.random.default_rng(20261021)
        marker = rng.integers(-20000, 20000, self.MARKER_SAMPLES).astype("<i2")
        pcm = np.zeros(int(seconds * SR), dtype="<i2")
        at = 2 * SR + 17
        pcm[at:at + len(marker)] = marker
        src = self.root / "synthetic-marked.wav"
        with wave.open(str(src), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        return src, (marker.astype(np.float32) / 32768.0).astype("<f4").tobytes()

    def files(self):
        return [p for p in (self.root / "ws").rglob("*") if p.is_file()]

    def test_no_plain_decoded_audio_at_rest(self):
        import io
        src, marker = self.marked_wav()
        control = io.BytesIO()
        media.decode(src, control)                           # the decoder does produce it
        self.assertIn(marker, control.getvalue())
        mid, _ = media.import_recording(self.store, self.media, src, "Synthetic")
        self.store.erase_checkpoint()
        found = [p.name for p in self.files() if marker in p.read_bytes()]
        self.assertEqual([], found)
        self.assertTrue(media.audio_path(self.media, mid).is_file())
        reader = media.open_audio(self.store, self.media, mid)
        try:
            self.assertEqual(reader.read(0, len(reader)).astype("<f4").tobytes().count(marker), 1)
        finally:
            reader.close()

    def test_the_import_records_the_file_id_and_authenticates_the_file_once(self):
        src, _ = self.marked_wav()
        verified = []
        real = media.AudioReader.verify

        def counting(reader):
            verified.append(1)
            return real(reader)
        with mock.patch.object(media.AudioReader, "verify", counting):
            mid, sid = media.import_recording(self.store, self.media, src, "Synthetic")
        self.assertEqual(verified, [1])
        reader = media.open_audio(self.store, self.media, mid)
        try:
            self.assertEqual(self.store.source(sid)["audio_id"], reader.file_id.hex())
            self.assertEqual(self.store.source(sid)["samples"], len(reader))
        finally:
            reader.close()
        self.assertFalse(any(p.suffix == ".partial" for p in self.files()))

    def test_a_file_damaged_while_it_is_saved_fails_the_import_and_leaves_nothing(self):
        src, _ = self.marked_wav()
        real_replace = os.replace

        def damaging_replace(a, b):
            if str(a).endswith(".partial"):
                flip(Path(a), block_offset(1))               # a disk fault before the rename
            return real_replace(a, b)
        with mock.patch.object(media.os, "replace", damaging_replace):
            with self.assertRaises(media.DecodeError) as ctx:
                media.import_recording(self.store, self.media, src, "Synthetic")
        self.assertEqual(str(ctx.exception), media.SAVE_FAILED)
        db = self.store.db
        self.assertEqual(db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM content_keys").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
        self.assertEqual(list(self.media.iterdir()) if self.media.exists() else [], [])

    def test_a_stale_partial_file_does_not_stop_a_decode(self):
        src, _ = self.marked_wav()
        dest = self.root / "audio.uma"
        dest.with_suffix(".partial").write_bytes(b"left by an interrupted import")
        key = bytes(range(32))
        samples, file_id = media.decode_to_file(src, dest, key, ("decoded", "m"))
        self.assertFalse(dest.with_suffix(".partial").exists())
        reader = media.AudioReader(dest, key, ("decoded", "m"))
        try:
            self.assertEqual((len(reader), reader.file_id), (samples, file_id))
            self.assertEqual(reader.verify(), samples)
        finally:
            reader.close()


@unittest.skipUnless(HAVE_NUMPY, "numpy/cryptography not installed")
class WorkerKeyTests(unittest.TestCase):
    """The meeting's audio key reaches the worker only inside the diarize
    request, over its pipe, and is never logged."""

    def setUp(self):
        from workspace import diag
        from workspace.worker import WorkerHandle
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.media = self.root / "media"
        self.logs = self.root / "logs"
        self.store = Store(self.root / "db.sqlite", protector=keyprotect.TestProtector())
        self.handle = WorkerHandle(self.root / "models", allow_test_engines=True,
                                   log=diag.Log(self.logs, "app"))
        self.mid = self.store.create_meeting("Synthetic")
        samples = noise(60)
        file_id = write_meeting_audio(self.store, self.media, self.mid, samples)
        sid = self.store.add_source(self.mid, original_path=None, original_sha256="a" * 64,
                                    original_size=1, original_mtime_ns=0,
                                    audio_id=file_id.hex(), samples=len(samples),
                                    sample_rate=SR)
        plan = [(a * SR, (a + 20) * SR, a * SR, (a + 20) * SR) for a in (0, 20, 40)]
        self.vid = self.store.ensure_version(self.mid, sid, engine="fake", model="fake",
                                             model_revision="1", precision="int8", config={},
                                             chunk_plan=plan)
        self.key = self.store.meeting_key(self.mid, "audio")

    def tearDown(self):
        self.handle.close()
        self.store.close()
        self.tmp.cleanup()

    def forms(self):
        k = self.key
        return (k, k.hex().encode(), k.hex().upper().encode(), base64.b64encode(k),
                base64.urlsafe_b64encode(k), base64.b64encode(k).rstrip(b"="))

    def test_the_key_goes_only_in_the_diarize_request(self):
        from multiprocessing import connection

        from workspace.worker import WorkerEngine
        sent = []
        # Windows pipes are PipeConnection: patch the shared base class.
        base = connection._ConnectionBase
        real_send = base.send

        def recording_send(conn, obj):
            sent.append((obj[0] if isinstance(obj, tuple) else None, pickle.dumps(obj)))
            return real_send(conn, obj)
        started = []                                 # each worker process's arguments
        ctx = self.handle._ctx

        def process(*args, **kwargs):
            started.append(pickle.dumps(kwargs.get("args", ())))
            return ctx.Process(*args, **kwargs)
        self.handle._ctx = mock.Mock(Pipe=ctx.Pipe, Process=process)
        with mock.patch.object(base, "send", recording_send):
            reader = media.open_audio(self.store, self.media, self.mid)
            try:
                engine = WorkerEngine(self.handle, "test:fake_worker_engines:echo", 1)
                self.assertEqual(runner.run_version(self.store, self.mid, self.vid, reader,
                                                    engine), runner.RunResult.DONE)
            finally:
                reader.close()
            audio = media.audio_ref(self.store, self.media, self.mid)
            self.assertEqual(speakers.run_speakers(
                self.store, self.mid, self.vid, self.handle, audio, threads=1,
                model="test:fake_worker_engines:fake_diarizer"), speakers.SpeakerResult.DONE)
        ops = [op for op, _ in sent]
        self.assertIn("transcribe", ops)
        with_key = [op for op, data in sent if self.key in data]
        self.assertEqual(with_key, ["diarize"])
        # Never in the worker's arguments or the environment.
        self.assertTrue(started)
        for args in started:
            self.assertNotIn(self.key, args)
        environ = "\0".join(f"{k}={v}" for k, v in os.environ.items()).encode("utf-8", "replace")
        for form in self.forms():
            self.assertNotIn(form, environ)

    def test_a_diarize_timeout_comes_from_the_frames(self):
        from workspace import worker
        audio = media.AudioRef(self.root / "gone.uma", self.key, ("decoded", self.mid),
                               3000 * SR)                    # 50 minutes, no file needed
        handle = mock.MagicMock()
        handle._request.return_value = ("ok", [], [], "cpu", False)
        worker.diarize(handle, "pyannote", audio, None, 1)
        timeout = handle._request.call_args[0][1]
        self.assertEqual(timeout, max(worker.DIARIZE_TIMEOUT_FLOOR_S,
                                      worker.DIARIZE_TIMEOUT_FACTOR * 3000))
        request = handle._request.call_args[0][0]
        self.assertEqual(request[0], "diarize")
        self.assertEqual(request[2], (str(audio.path), self.key, ("decoded", self.mid)))

    def test_audio_the_worker_cannot_open_fails_safely_and_logs_no_key(self):
        from workspace.worker import WorkerError, diarize
        wrong = media.AudioRef(media.audio_path(self.media, self.mid), self.key,
                               ("decoded", "another meeting"), 60 * SR)
        with self.assertRaises(WorkerError) as ctx:
            diarize(self.handle, "test:fake_worker_engines:fake_diarizer", wrong, None, 1)
        message = str(ctx.exception)
        self.assertIn("AudioCryptError", message)
        self.assertNotIn(str(self.media), message)
        for form in self.forms()[1:]:                        # the text forms
            self.assertNotIn(form.decode("ascii"), message)
        self.handle.shutdown()
        logged = [p for p in self.logs.rglob("*") if p.is_file()]
        self.assertTrue(logged)
        for path in logged:
            data = path.read_bytes()
            for form in self.forms():
                self.assertNotIn(form, data, path.name)

    def test_damaged_audio_fails_the_speaker_pass_and_keeps_the_transcript(self):
        for idx in range(3):                                 # a committed transcript
            self.store.commit_chunk(self.vid, idx, [Segment(idx * 20 + 1.0, idx * 20 + 4.0,
                                                            f"sentence {idx}")])
        before = [(r["id"], r["text"]) for r in self.store.transcript(self.vid)]
        flip(media.audio_path(self.media, self.mid), block_offset(10))
        audio = media.audio_ref(self.store, self.media, self.mid)   # header and last block open
        result = speakers.run_speakers(self.store, self.mid, self.vid, self.handle, audio,
                                       threads=1, model="test:fake_worker_engines:fake_diarizer")
        self.assertEqual(result, speakers.SpeakerResult.FAILED)
        self.assertEqual([(r["id"], r["text"]) for r in self.store.transcript(self.vid)], before)
        [run] = self.store.speaker_runs(self.vid)
        self.assertEqual(run["status"], "failed")
        self.assertIn("AudioCryptError", run["error"] or "")


if __name__ == "__main__":
    unittest.main()
