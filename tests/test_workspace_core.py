# Durable core (#14): chunk planning, ownership, atomic commits and recovery.
# Uses a synthetic "recording" with known words at known times and a fake
# engine that hears exactly the words fully inside the slice it is given (and
# garbles words cut by the slice edge). Like real engines, it can return
# sentence-length segments with word timings (group > 1), which is what exposes
# boundary loss if ownership were decided per segment.
# Needs numpy (installed in CI for this job).
#   python -m unittest discover -s tests -v

import importlib.util
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
SR = 16000

if HAVE_NUMPY:
    import numpy as np

    from asr_engines import Segment
    from workspace import chunking, runner
    from workspace.store import SchemaTooNew, Store, StoreError

    class Tagged(np.ndarray):
        """An audio slice that remembers where it came from (test-only)."""

    class FakeReader:
        """Audio is loud (0.5) inside words and silent between them."""

        def __init__(self, words, total_s):
            self.words, self.total = words, int(total_s * SR)
            self.audio = np.zeros(self.total, dtype=np.float32)
            for t, d, _ in words:
                self.audio[int(t * SR):int((t + d) * SR)] = 0.5

        def __len__(self):
            return self.total

        def read(self, start, end):
            out = self.audio[start:end].copy().view(Tagged)
            out.offset = start / SR
            return out

    class FakeEngine:
        """Hears words fully inside the slice and garbles words cut by its
        edges. group=N returns N-word "sentences" with word timings, like
        Parakeet/Whisper; word_times=False drops the word timings."""

        device, precision = "cpu", "int8"

        def __init__(self, words, fail_at_offset=None, group=1, word_times=True):
            self.words, self.calls, self.fail_at_offset = words, [], fail_at_offset
            self.group, self.word_times = group, word_times

        def transcribe(self, audio):
            off, dur = audio.offset, len(audio) / SR
            self.calls.append(round(off, 3))
            if self.fail_at_offset is not None and abs(off - self.fail_at_offset) < 1e-6:
                raise RuntimeError("engine crashed")
            heard = []
            for t, d, text in self.words:
                if t >= off and t + d <= off + dur:
                    heard.append((t - off, t - off + d, " " + text))
                elif t < off + dur and t + d > off:      # cut by the slice edge
                    heard.append((max(0.0, t - off), min(dur, t + d - off), " #garbled#"))
            segs = []
            for i in range(0, len(heard), self.group):
                ws = heard[i:i + self.group]
                text = "".join(w[2] for w in ws).strip()
                segs.append(Segment(ws[0][0], ws[-1][1], text,
                                    tuple(ws) if self.word_times else ()))
            return segs, dur


def make_words(total_s, gap=0.35, word=0.4, repeat_every=7, pause_every=13, pause=1.2):
    """Words every ~0.75 s with occasional longer pauses and repeated phrases."""
    words, t, i = [], 0.5, 0
    while t + word < total_s - 0.5:
        text = "yes" if i % repeat_every in (0, 1, 2) else f"w{i}"   # "yes yes yes" runs
        words.append((round(t, 3), word, text))
        t += word + (pause if i % pause_every == pause_every - 1 else gap)
        i += 1
    return words


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "workspace.db"
        self.store = Store(self.db_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def new_meeting(self, total_s=200.0, **word_kw):
        words = make_words(total_s, **word_kw)
        reader = FakeReader(words, total_s)
        mid = self.store.create_meeting("Synthetic")
        sid = self.store.add_source(mid, original_path=None, original_sha256="a" * 64,
                                    original_size=1, original_mtime_ns=0,
                                    audio_sha256="b" * 64, samples=reader.total,
                                    sample_rate=SR)
        plan = chunking.plan_chunks(reader, reader.total)
        vid = self.store.ensure_version(mid, sid, engine="fake", model="fake", model_revision="1",
                                        precision="int8", config={"beam": 1}, chunk_plan=plan)
        return mid, sid, vid, words, reader, plan

    def spoken(self, vid, store=None):
        """All committed words in time order (segments may hold several)."""
        return " ".join(r["text"] for r in (store or self.store).transcript(vid)).split()

    def assertMatchesTruth(self, vid, words, store=None):
        self.assertEqual(self.spoken(vid, store), [w[2] for w in words])

    # -- planning and ownership ------------------------------------------------

    def test_plan_tiles_exactly_and_cuts_in_pauses(self):
        _, _, _, words, reader, plan = self.new_meeting(300.0)
        self.assertEqual(plan[0][0], 0)
        self.assertEqual(plan[-1][1], reader.total)
        for (a, b, ca, cb), nxt in zip(plan, plan[1:] + [None], strict=True):
            self.assertLess(a, b)
            self.assertLessEqual(ca, a)
            self.assertGreaterEqual(cb, b)
            if nxt:
                self.assertEqual(b, nxt[0])                    # no gap, no overlap
                self.assertEqual(reader.audio[b], 0.0)         # boundary in silence
        self.assertGreater(len(plan), 4)

    def test_full_run_matches_truth_word_and_sentence_segments(self):
        # group=8: ~6 s "sentences" with word times. Ownership per segment
        # would lose sentence tails at boundaries (found in review); per word
        # it must not.
        for group in (1, 8, 20):
            with self.subTest(group=group):
                mid, _, vid, words, reader, _ = self.new_meeting(300.0)
                self.assertEqual(runner.run_version(self.store, mid, vid, reader,
                                                    FakeEngine(words, group=group)),
                                 runner.RunResult.DONE)
                self.assertMatchesTruth(vid, words)
                self.assertNotIn("#garbled#", self.spoken(vid))
                self.assertEqual(self.store.meeting(mid)["phase"], "transcribed")

    def test_continuous_speech_and_no_silence_with_sentences(self):
        for kw in ({"gap": 0.05, "pause": 0.05}, {"gap": 0.0, "pause": 0.0}):
            for group in (1, 8):
                with self.subTest(**kw, group=group):
                    mid, _, vid, words, reader, plan = self.new_meeting(200.0, **kw)
                    runner.run_version(self.store, mid, vid, reader,
                                       FakeEngine(words, group=group))
                    self.assertMatchesTruth(vid, words)

    def test_boundary_sentence_is_split_between_chunks(self):
        mid, _, vid, words, reader, plan = self.new_meeting(200.0, gap=0.05, pause=0.05)
        runner.run_version(self.store, mid, vid, reader, FakeEngine(words, group=20))
        rows = self.store.transcript(vid)
        boundaries = [p[1] / SR for p in plan[:-1]]
        # No committed segment runs across an owned boundary by more than the
        # length of the one word that starts just before it (0.4 s here).
        for r in rows:
            for b in boundaries:
                self.assertFalse(r["start_s"] < b and r["end_s"] > b + 0.45, (r["start_s"], b))
        self.assertTrue(all(r["words_json"] for r in rows))

    def test_segments_without_word_times_are_divided_approximately_not_lost(self):
        # faster-whisper can return a segment without words; estimated word
        # times keep the run going and keep sentence tails (+/- a word or two
        # at a boundary, where the estimate may place a word on either side).
        mid, _, vid, words, reader, _ = self.new_meeting(300.0)
        result = runner.run_version(self.store, mid, vid, reader,
                                    FakeEngine(words, group=8, word_times=False))
        self.assertEqual(result, runner.RunResult.DONE)
        got = [w for w in self.spoken(vid) if w != "#garbled#"]
        truth = [w[2] for w in words]
        self.assertLessEqual(abs(len(got) - len(truth)), 2 * len(self.store.chunks(vid)))
        self.assertGreater(len(got), 0.95 * len(truth))

    def test_parakeet_words_follow_tokens_not_shared_timestamps(self):
        # Review case: TDT gave "it", "." and "The" the same timestamp.
        from asr_engines import split_sentences
        tokens = [" We", " did", " it", ".", " The", " end", "."]
        times = [0, .4, .8, .8, .8, 1.2, 1.5]
        a, b = split_sentences(10.0, 12.0, tokens, times, "We did it. The end.")
        self.assertEqual([w[2] for w in a.words], [" We", " did", " it."])
        self.assertEqual([w[2] for w in b.words], [" The", " end."])
        for boundary in (10.3, 10.9):
            mine_a = chunking.owned([a, b], 0, int(boundary * SR), 30 * SR)
            mine_b = chunking.owned([a, b], int(boundary * SR), 30 * SR, 30 * SR)
            spoken = " ".join(s.text for s in mine_a + mine_b).split()
            self.assertEqual(spoken, ["We", "did", "it.", "The", "end."], boundary)

    def test_interrupted_attempts_are_marked_on_reopen(self):
        mid, _, vid, *_ = self.new_meeting(60.0)
        self.store.start_attempt(vid, 0)          # process "dies" here
        self.store.close()
        self.store = Store(self.db_path)
        self.assertEqual([a["result"] for a in self.store.attempts(vid, 0)], ["interrupted"])

    def test_read_errors_become_store_error(self):
        mid, _, vid, words, reader, _ = self.new_meeting(60.0)
        self.store.close()
        with self.assertRaises(StoreError):
            self.store.meeting(mid)
        self.assertEqual(runner.run_version(self.store, mid, vid, reader, FakeEngine(words)),
                         runner.RunResult.STORE_ERROR)
        self.store = Store(self.db_path)

    # -- failure injection ---------------------------------------------------------

    def test_crash_before_commit_resumes_without_loss_or_duplication(self):
        mid, _, vid, words, reader, plan = self.new_meeting(300.0)

        class Crash(Exception):
            pass

        def boom(idx):
            if idx == 3:
                raise Crash()
        with self.assertRaises(Crash):
            runner.run_version(self.store, mid, vid, reader, FakeEngine(words, group=8),
                               hooks={"before_commit": boom, "reraise": True})
        self.assertEqual([c["status"] for c in self.store.chunks(vid)][:4],
                         ["done", "done", "done", "pending"])
        self.store.close()
        self.store = Store(self.db_path)                      # restart
        eng2 = FakeEngine(words, group=8)
        self.assertEqual(runner.run_version(self.store, mid, vid, reader, eng2),
                         runner.RunResult.DONE)
        self.assertMatchesTruth(vid, words)
        done_before = {round(plan[i][2] / SR, 3) for i in range(3)}
        self.assertFalse(done_before & set(eng2.calls))        # committed chunks not re-run

    def test_crash_after_commit_does_not_redo_the_chunk(self):
        mid, _, vid, words, reader, plan = self.new_meeting(300.0)

        class Crash(Exception):
            pass

        def boom(idx):
            if idx == 2:
                raise Crash()
        with self.assertRaises(Crash):
            runner.run_version(self.store, mid, vid, reader, FakeEngine(words),
                               hooks={"after_commit": boom, "reraise": True})
        eng2 = FakeEngine(words)
        runner.run_version(self.store, mid, vid, reader, eng2)
        self.assertNotIn(round(plan[2][2] / SR, 3), eng2.calls)
        self.assertMatchesTruth(vid, words)

    def test_each_attempt_is_recorded_with_the_engine_backend(self):
        mid, _, vid, words, reader, plan = self.new_meeting(200.0)
        eng = FakeEngine(words, fail_at_offset=plan[1][2] / SR)
        self.assertEqual(runner.run_version(self.store, mid, vid, reader, eng),
                         runner.RunResult.FAILED)
        self.assertEqual(self.store.chunks(vid)[1]["status"], "pending")
        self.assertEqual(self.store.chunks(vid)[1]["failures"], 1)
        self.assertEqual(self.store.meeting(mid)["phase"], "error")
        runner.run_version(self.store, mid, vid, reader, FakeEngine(words))
        self.assertMatchesTruth(vid, words)
        att = self.store.attempts(vid, 1)
        self.assertEqual([a["result"] for a in att], ["failed", "committed"])
        self.assertEqual((att[1]["backend"], att[1]["precision"]), ("cpu", "int8"))

    def test_commit_refused_twice_after_supersede_and_after_cleanup_epoch(self):
        mid, sid, vid, words, reader, plan = self.new_meeting(100.0)
        seg = [Segment(1.0, 1.4, "x")]
        self.assertTrue(self.store.commit_chunk(vid, 0, seg))
        self.assertFalse(self.store.commit_chunk(vid, 0, seg))      # repeated result dropped
        self.assertFalse(self.store.commit_chunk(vid, 1, seg, epoch=99))   # stale epoch
        new = self.store.ensure_version(mid, sid, engine="fake", model="fake",
                                        model_revision="2", precision="int8",
                                        config={"beam": 1}, chunk_plan=plan)
        self.assertNotEqual(new, vid)
        self.assertFalse(self.store.commit_chunk(vid, 1, seg))      # superseded version
        self.assertEqual(self.spoken(vid), ["x"])

    def test_only_one_active_version_per_meeting(self):
        mid, sid, vid, *_ = self.new_meeting(60.0)
        with self.assertRaises(StoreError):
            with self.store.tx() as db:
                db.execute("INSERT INTO versions (id, meeting_id, source_id, engine, model, "
                           "model_revision, precision, config_json, config_hash, created_at) "
                           "VALUES ('x', ?, ?, 'e', 'm', 'r', 'p', '{}', 'h', 0)", (mid, sid))

    def test_disk_full_rolls_back_and_resumes(self):
        mid, _, vid, words, reader, _ = self.new_meeting(300.0)
        result = runner.run_version(self.store, mid, vid, reader, FakeEngine(words, group=8),
                                    hooks={"after_commit": lambda idx: idx == 0 and
                                           self.fill_disk()})
        self.assertEqual(result, runner.RunResult.STORE_ERROR)
        chunks = self.store.chunks(vid)
        self.assertLess([c["status"] for c in chunks].count("done"), len(chunks))
        for c in chunks:                # no partial chunk
            n = self.store.db.execute("SELECT COUNT(*) FROM segments WHERE version_id = ? AND "
                                      "chunk_idx = ?", (vid, c["idx"])).fetchone()[0]
            self.assertEqual(n == 0, c["status"] == "pending")
        self.store.db.execute("PRAGMA max_page_count = 1000000")
        self.assertEqual(runner.run_version(self.store, mid, vid, reader,
                                            FakeEngine(words, group=8)),
                         runner.RunResult.DONE)
        self.assertMatchesTruth(vid, words)

    def fill_disk(self):
        pages = self.store.db.execute("PRAGMA page_count").fetchone()[0]
        self.store.db.execute(f"PRAGMA max_page_count = {pages}")

    def test_changed_audio_is_not_transcribed(self):
        mid, _, vid, words, reader, _ = self.new_meeting(100.0)
        shorter = FakeReader(words, 90.0)
        self.assertEqual(runner.run_version(self.store, mid, vid, shorter, FakeEngine(words)),
                         runner.RunResult.SOURCE_CHANGED)
        self.assertEqual(self.store.chunks(vid, status="done"), [])
        self.assertIn("changed", self.store.meeting(mid)["last_error"])

    # -- pause, versions, edits, threads ---------------------------------------

    def test_explicit_pause_survives_restart(self):
        mid, _, vid, words, reader, _ = self.new_meeting(300.0)
        runner.run_version(self.store, mid, vid, reader, FakeEngine(words),
                           hooks={"after_commit": lambda idx: idx == 1 and
                                  self.store.set_paused(mid, True)})
        done, total = self.store.progress(vid)
        self.assertTrue(0 < done < total)                      # truthful: committed only
        self.store.close()
        self.store = Store(self.db_path)                       # restart
        self.assertEqual(self.store.meeting(mid)["paused"], 1)
        eng = FakeEngine(words)
        self.assertEqual(runner.run_version(self.store, mid, vid, reader, eng),
                         runner.RunResult.PAUSED)
        self.assertEqual(eng.calls, [])
        self.store.set_paused(mid, False)
        runner.run_version(self.store, mid, vid, reader, FakeEngine(words))
        self.assertMatchesTruth(vid, words)

    def test_changed_settings_or_source_make_a_new_version_and_keep_the_old(self):
        mid, sid, vid, words, reader, plan = self.new_meeting(100.0)
        runner.run_version(self.store, mid, vid, reader, FakeEngine(words))
        first = self.store.transcript(vid)[0]
        self.store.save_edit(first["id"], "Edited by a person")
        same = self.store.ensure_version(mid, sid, engine="fake", model="fake",
                                         model_revision="1", precision="int8",
                                         config={"beam": 1}, chunk_plan=plan)
        self.assertEqual(same, vid)
        new_settings = self.store.ensure_version(mid, sid, engine="fake", model="fake",
                                                 model_revision="1", precision="int8",
                                                 config={"beam": 5}, chunk_plan=plan)
        self.assertNotEqual(new_settings, vid)
        sid2 = self.store.add_source(mid, original_path=None, original_sha256="c" * 64,
                                     original_size=2, original_mtime_ns=1, audio_sha256="d" * 64,
                                     samples=reader.total, sample_rate=SR)
        new_source = self.store.ensure_version(mid, sid2, engine="fake", model="fake",
                                               model_revision="1", precision="int8",
                                               config={"beam": 5}, chunk_plan=plan)
        self.assertNotIn(new_source, (vid, new_settings))
        self.assertEqual(self.store.version(vid)["state"], "superseded")
        self.assertEqual(self.store.transcript(vid)[0]["text"], "Edited by a person")
        self.assertEqual(self.store.transcript(new_source), [])

    def test_generated_output_never_overwrites_edits(self):
        mid, _, vid, words, reader, _ = self.new_meeting(300.0)
        runner.run_version(self.store, mid, vid, reader, FakeEngine(words),
                           hooks={"after_commit": lambda idx: idx == 0 and
                                  self.store.set_paused(mid, True)})
        seg = self.store.transcript(vid)[0]
        self.store.save_edit(seg["id"], "Corrected")
        self.store.set_paused(mid, False)
        runner.run_version(self.store, mid, vid, reader, FakeEngine(words))
        rows = self.store.transcript(vid)
        self.assertEqual(rows[0]["text"], "Corrected")
        self.assertEqual(rows[0]["generated"], seg["generated"])
        self.assertEqual(" ".join(r["text"] for r in rows[1:]).split(),
                         [w[2] for w in words[1:]])

    def test_ui_thread_can_edit_and_pause_while_a_run_commits(self):
        mid, _, vid, words, reader, _ = self.new_meeting(300.0)
        errors, stop = [], threading.Event()

        def ui():
            while not stop.is_set():
                try:
                    self.store.set_paused(mid, False)
                    rows = self.store.transcript(vid)
                    if rows:
                        self.store.save_edit(rows[0]["id"], "edited")
                except Exception as exc:     # any StoreError here is the bug
                    errors.append(exc)
        t = threading.Thread(target=ui)
        t.start()
        try:
            result = runner.run_version(self.store, mid, vid, reader, FakeEngine(words, group=8))
        finally:
            stop.set()
            t.join()
        self.assertEqual((result, errors), (runner.RunResult.DONE, []))

    def test_store_errors_are_wrapped_and_newer_schema_refused(self):
        self.store.db.execute("PRAGMA query_only = ON")
        with self.assertRaises(StoreError):
            self.store.create_meeting("x")
        self.store.db.execute("PRAGMA query_only = OFF")
        self.store.db.execute("UPDATE schema_version SET version = 999")
        self.store.close()
        with self.assertRaises(SchemaTooNew):
            Store(self.db_path)
        self.store = Store.__new__(Store)            # tearDown-safe dummy
        self.store.close = lambda: None


@unittest.skipUnless(HAVE_NUMPY and importlib.util.find_spec("av"), "numpy/PyAV not installed")
class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def tone(self, name="tone.wav", seconds=5):
        import wave
        src = self.root / name
        t = np.arange(SR * seconds) / SR
        pcm = (0.3 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2")
        with wave.open(str(src), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        return src

    def test_import_streams_to_disk_and_reads_slices(self):
        from workspace import media
        src = self.tone()
        mid, sid = media.import_recording(self.store, self.root / "media", src, "Tone")
        row = self.store.source(sid)
        self.assertEqual(row["samples"], SR * 5)
        self.assertEqual(row["original_sha256"], media.sha256_file(src))
        path = media.audio_path(self.root / "media", mid)
        self.assertEqual(path.stat().st_size, SR * 5 * 4)
        self.assertNotIn("tone", str(path).lower())        # no source name in app paths
        reader = media.AudioReader(path)
        part = reader.read(SR, SR * 2)
        self.assertEqual(len(part), SR)
        self.assertNotIsInstance(part, np.memmap)          # a copy of the slice only
        self.assertAlmostEqual(float(np.abs(part).max()), 0.3, places=2)
        reader.close()

    def test_failed_import_leaves_nothing_behind(self):
        from workspace import media
        bad = self.root / "broken.wav"
        bad.write_bytes(b"RIFF" + b"\x00" * 200)
        with self.assertRaises((OSError, ValueError)):     # PyAV's decode errors
            media.import_recording(self.store, self.root / "media", bad, "Broken")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0], 0)
        media_dir = self.root / "media"
        self.assertEqual(list(media_dir.iterdir()) if media_dir.exists() else [], [])

    def test_damaged_audio_file_is_detected_before_a_run(self):
        from workspace import media
        mid, sid = media.import_recording(self.store, self.root / "media", self.tone(), "Tone")
        path = media.audio_path(self.root / "media", mid)
        reader = media.AudioReader(path)
        plan = chunking.plan_chunks(reader, len(reader))
        vid = self.store.ensure_version(mid, sid, engine="fake", model="fake",
                                        model_revision="1", precision="int8", config={},
                                        chunk_plan=plan)
        reader.close()
        data = bytearray(path.read_bytes())
        data[100:104] = b"\x00\x00\x80\x7f"                   # same length, different bytes
        path.write_bytes(bytes(data))
        reader = media.AudioReader(path)
        try:
            self.assertEqual(runner.run_version(self.store, mid, vid, reader, object()),
                             runner.RunResult.SOURCE_CHANGED)
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()
