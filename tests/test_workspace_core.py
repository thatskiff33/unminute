# Durable core (#14): chunk planning, ownership, atomic commits and recovery.
# Uses a synthetic "recording" with known words at known times and a fake
# engine that hears exactly the words fully inside the slice it is given (and
# garbles words cut by the slice edge), so the committed transcript can be
# checked against the truth for missing or duplicated speech.
# Needs numpy (installed in CI for this job).
#   python -m unittest discover -s tests -v

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
SR = 16000

if HAVE_NUMPY:
    import numpy as np

    from workspace import chunking, runner
    from workspace.store import Store, StoreError

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
        """Hears words fully inside the slice; garbles words cut by its edges."""

        def __init__(self, words, fail_at_offset=None):
            self.words, self.calls, self.fail_at_offset = words, [], fail_at_offset

        def transcribe(self, audio):
            off, dur = audio.offset, len(audio) / SR
            self.calls.append(round(off, 3))
            if self.fail_at_offset is not None and abs(off - self.fail_at_offset) < 1e-6:
                raise RuntimeError("engine crashed")
            segs = []
            for t, d, text in self.words:
                if t >= off and t + d <= off + dur:
                    segs.append(runner.Seg(t - off, t - off + d, text))
                elif t < off + dur and t + d > off:      # cut by the slice edge
                    segs.append(runner.Seg(max(0.0, t - off), min(dur, t + d - off), "#garbled#"))
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

    def texts(self, vid, store=None):
        return [r["text"] for r in (store or self.store).transcript(vid)]

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

    def test_full_run_matches_truth_including_repeats(self):
        mid, _, vid, words, reader, _ = self.new_meeting(300.0)
        eng = FakeEngine(words)
        self.assertEqual(runner.run_version(self.store, mid, vid, reader, eng),
                         runner.RunResult.DONE)
        self.assertEqual(self.texts(vid), [w[2] for w in words])
        self.assertNotIn("#garbled#", self.texts(vid))
        self.assertEqual(self.store.meeting(mid)["phase"], "transcribed")

    def test_continuous_speech_without_pauses(self):
        # No pause longer than 0.05 s: boundaries must still not lose or repeat words.
        mid, _, vid, words, reader, _ = self.new_meeting(200.0, gap=0.05, pause=0.05)
        runner.run_version(self.store, mid, vid, reader, FakeEngine(words))
        self.assertEqual(self.texts(vid), [w[2] for w in words])

    def test_no_silence_at_all_cuts_mid_word_without_loss(self):
        # Words back to back: every boundary falls inside a word. The word is
        # owned by the chunk where it starts, which hears it whole in context.
        mid, _, vid, words, reader, plan = self.new_meeting(200.0, gap=0.0, pause=0.0)
        inside = [any(t < b / SR < t + d for t, d, _ in words) for _, b, _, _ in plan[:-1]]
        self.assertTrue(any(inside))
        runner.run_version(self.store, mid, vid, reader, FakeEngine(words))
        self.assertEqual(self.texts(vid), [w[2] for w in words])

    # -- failure injection ---------------------------------------------------------

    def test_crash_before_commit_resumes_without_loss_or_duplication(self):
        mid, _, vid, words, reader, plan = self.new_meeting(300.0)

        class Crash(Exception):
            pass

        def boom(idx):
            if idx == 3:
                raise Crash()
        eng = FakeEngine(words)
        with self.assertRaises(Crash):
            runner.run_version(self.store, mid, vid, reader, eng,
                               hooks={"before_commit": boom, "reraise": True})
        self.assertEqual([c["status"] for c in self.store.chunks(vid)][:4],
                         ["done", "done", "done", "pending"])
        # "Restart": new store connection and engine.
        self.store.close()
        self.store = Store(self.db_path)
        eng2 = FakeEngine(words)
        self.assertEqual(runner.run_version(self.store, mid, vid, reader, eng2),
                         runner.RunResult.DONE)
        self.assertEqual(self.texts(vid), [w[2] for w in words])
        done_before = {round(plan[i][2] / SR, 3) for i in range(3)}
        self.assertFalse(done_before & set(eng2.calls))         # committed chunks not re-run

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
        self.assertEqual(self.texts(vid), [w[2] for w in words])

    def test_engine_failure_leaves_chunk_pending_and_reports_error(self):
        mid, _, vid, words, reader, plan = self.new_meeting(200.0)
        eng = FakeEngine(words, fail_at_offset=plan[1][2] / SR)
        self.assertEqual(runner.run_version(self.store, mid, vid, reader, eng),
                         runner.RunResult.FAILED)
        self.assertEqual(self.store.chunks(vid)[1]["status"], "pending")
        self.assertEqual(self.store.meeting(mid)["phase"], "error")
        runner.run_version(self.store, mid, vid, reader, FakeEngine(words))
        self.assertEqual(self.texts(vid), [w[2] for w in words])

    def test_commit_is_refused_twice(self):
        mid, _, vid, words, reader, _ = self.new_meeting(100.0)
        seg = [runner.Seg(1.0, 1.4, "x")]
        self.assertTrue(self.store.commit_chunk(vid, 0, seg))
        self.assertFalse(self.store.commit_chunk(vid, 0, seg))   # stale/repeated result dropped
        self.assertEqual(self.texts(vid), ["x"])

    def test_disk_full_rolls_back_and_resumes(self):
        mid, _, vid, words, reader, _ = self.new_meeting(300.0)
        runner.run_version(self.store, mid, vid, reader, FakeEngine(words),
                           hooks={"after_commit": lambda idx: idx == 0 and self.fill_disk()})
        chunks = self.store.chunks(vid)
        done = [c["status"] for c in chunks].count("done")
        self.assertLess(done, len(chunks))
        # No partial chunk: every committed chunk has its segments, pending ones none.
        for c in chunks:
            n = self.store.db.execute("SELECT COUNT(*) FROM segments WHERE version_id = ? AND "
                                      "chunk_idx = ?", (vid, c["idx"])).fetchone()[0]
            if c["status"] == "pending":
                self.assertEqual(n, 0)
        self.store.db.execute("PRAGMA max_page_count = 1000000")
        self.assertEqual(runner.run_version(self.store, mid, vid, reader, FakeEngine(words)),
                         runner.RunResult.DONE)
        self.assertEqual(self.texts(vid), [w[2] for w in words])

    def fill_disk(self):
        pages = self.store.db.execute("PRAGMA page_count").fetchone()[0]
        self.store.db.execute(f"PRAGMA max_page_count = {pages}")

    # -- pause, versions, edits --------------------------------------------------

    def test_explicit_pause_survives_restart(self):
        mid, _, vid, words, reader, _ = self.new_meeting(300.0)
        runner.run_version(self.store, mid, vid, reader, FakeEngine(words),
                           hooks={"after_commit": lambda idx: idx == 1 and
                                  self.store.set_paused(mid, True)})
        done, total = self.store.progress(vid)
        self.assertTrue(0 < done < total)                     # truthful: committed only
        self.store.close()
        self.store = Store(self.db_path)                      # restart
        self.assertEqual(self.store.meeting(mid)["paused"], 1)
        eng = FakeEngine(words)
        self.assertEqual(runner.run_version(self.store, mid, vid, reader, eng),
                         runner.RunResult.PAUSED)
        self.assertEqual(eng.calls, [])
        self.store.set_paused(mid, False)
        runner.run_version(self.store, mid, vid, reader, FakeEngine(words))
        self.assertEqual(self.texts(vid), [w[2] for w in words])

    def test_changed_settings_make_a_new_version_and_keep_the_old(self):
        mid, sid, vid, words, reader, plan = self.new_meeting(100.0)
        runner.run_version(self.store, mid, vid, reader, FakeEngine(words))
        first = self.store.transcript(vid)[0]
        self.store.save_edit(first["id"], "Edited by a person")
        same = self.store.ensure_version(mid, sid, engine="fake", model="fake",
                                         model_revision="1", precision="int8",
                                         config={"beam": 1}, chunk_plan=plan)
        self.assertEqual(same, vid)
        new = self.store.ensure_version(mid, sid, engine="fake", model="fake",
                                        model_revision="2", precision="int8",
                                        config={"beam": 1}, chunk_plan=plan)
        self.assertNotEqual(new, vid)
        self.assertEqual(self.store.version(vid)["state"], "superseded")
        self.assertEqual(self.store.transcript(vid)[0]["text"], "Edited by a person")
        self.assertEqual(self.store.transcript(new), [])

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
        self.assertEqual([r["text"] for r in rows[1:]], [w[2] for w in words[1:]])

    def test_store_errors_are_wrapped(self):
        self.store.db.execute("PRAGMA query_only = ON")
        with self.assertRaises(StoreError):
            self.store.create_meeting("x")
        self.store.db.execute("PRAGMA query_only = OFF")
        self.assertIsInstance(sqlite3.sqlite_version, str)


@unittest.skipUnless(HAVE_NUMPY and importlib.util.find_spec("av"), "numpy/PyAV not installed")
class MediaTests(unittest.TestCase):
    def test_import_streams_to_disk_and_reads_slices(self):
        import wave

        from workspace import media
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "tone.wav"
            t = np.arange(SR * 5) / SR
            pcm = (0.3 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2")
            with wave.open(str(src), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(pcm.tobytes())
            store = Store(Path(tmp) / "db.sqlite")
            try:
                mid, sid = media.import_recording(store, Path(tmp) / "media", src, "Tone")
                row = store.source(sid)
                self.assertEqual(row["samples"], SR * 5)
                self.assertEqual(row["original_sha256"], media.sha256_file(src))
                path = media.audio_path(Path(tmp) / "media", mid)
                self.assertEqual(path.stat().st_size, SR * 5 * 4)
                self.assertNotIn("tone", str(path).lower())   # no source name in app paths
                reader = media.AudioReader(path)
                part = reader.read(SR, SR * 2)
                self.assertEqual(len(part), SR)
                self.assertNotIsInstance(part, np.memmap)       # a copy of the slice only
                self.assertAlmostEqual(float(np.abs(part).max()), 0.3, places=2)
                reader.close()
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
