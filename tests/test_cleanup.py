# Explicit cleanup checkpoints (#17). Synthetic meetings only.
#   python -m unittest discover -s tests -v

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
SR = 16000
MARKER = "zebracrossing"   # distinctive transcript text to search for after cleanup

if HAVE_NUMPY:
    import numpy as np

    from asr_engines import Segment
    from workspace import cleanup
    from workspace.cleanup import CleanupError, JobResult
    from workspace.media import sha256_file
    from workspace.store import Store, new_id


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.media = self.root / "media"
        self.intake = self.root / "intake"
        self.intake.mkdir()
        self.db_path = self.root / "workspace.db"
        self.store = Store(self.db_path)
        self.mid, self.vid, self.original = self.make_meeting()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def make_meeting(self, authority=False):
        original = self.intake / f"{new_id()}.wav"
        original.write_bytes(b"RIFF" + os.urandom(2000))
        st = original.stat()
        mid = self.store.create_meeting("Synthetic")
        folder = self.media / mid
        (folder / "clips").mkdir(parents=True)
        np.zeros(30 * SR, dtype="<f4").tofile(folder / "audio.f32")
        (folder / "clips" / "c1.wav").write_bytes(b"clip")
        sid = self.store.add_source(mid, original_path=str(original),
                                    original_sha256=sha256_file(original),
                                    original_size=st.st_size, original_mtime_ns=st.st_mtime_ns,
                                    audio_sha256="b" * 64, samples=30 * SR, sample_rate=SR)
        if authority:
            with self.store.tx() as db:
                db.execute("UPDATE sources SET delete_original = 1 WHERE id = ?", (sid,))
        vid = self.store.ensure_version(mid, sid, engine="fake", model="fake",
                                        model_revision="1", precision="int8", config={},
                                        chunk_plan=[(0, 30 * SR, 0, 30 * SR)])
        self.store.commit_chunk(vid, 0, [Segment(1.0, 3.0, f"the {MARKER} plan"),
                                         Segment(4.0, 6.0, "second sentence")])
        self.store.save_edit(self.store.transcript(vid)[1]["id"], f"edited {MARKER} note")
        rid = self.store.start_speaker_run(vid)
        self.store.finish_speaker_run(rid, [(0, 30, "SPEAKER_00")], [(0, 30, "SPEAKER_00")])
        return mid, vid, original

    def run_job(self, jid, **kw):
        return cleanup.run_job(self.store, jid, self.media, **kw)

    # -- checkpoint 1: reviewed transcript -> audio ------------------------------

    def test_review_freezes_text_then_removes_app_audio_only(self):
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        snap = self.store._one("SELECT * FROM reviewed_snapshots WHERE meeting_id = ?",
                               (self.mid,))
        self.assertIn(f"SPEAKER_00: edited {MARKER} note", snap["text"])
        self.assertTrue((self.media / self.mid).exists())          # intent only, so far
        self.assertEqual(self.run_job(jid), JobResult.DONE)
        self.assertFalse((self.media / self.mid).exists())         # audio + clips gone
        self.assertTrue(self.original.exists())                    # no authority: untouched
        self.assertIsNotNone(self.store.meeting(self.mid)["audio_removed_at"])
        self.assertEqual(len(self.store.transcript(self.vid)), 2)  # text kept

    def test_refused_while_unfinished(self):
        sid = self.store.version(self.vid)["source_id"]
        self.store.ensure_version(self.mid, sid, engine="fake", model="fake", model_revision="2",
                                  precision="int8", config={},
                                  chunk_plan=[(0, 30 * SR, 0, 30 * SR)])
        with self.assertRaises(CleanupError):
            cleanup.confirm_transcript_reviewed(self.store, self.mid)
        with self.assertRaises(CleanupError):
            cleanup.confirm_final_saved(self.store, self.mid)     # review not confirmed

    def test_original_deleted_only_with_authority_root_and_same_identity(self):
        mid, _, original = self.make_meeting(authority=True)
        jid = cleanup.confirm_transcript_reviewed(self.store, mid)
        self.assertEqual(self.run_job(jid, original_roots=[self.root / "elsewhere"]),
                         JobResult.DONE)
        self.assertTrue(original.exists())                         # outside authorised folder
        self.assertIn("not in a folder", self.job(jid)["error"])

        mid, _, original = self.make_meeting(authority=True)
        time.sleep(0.01)
        original.write_bytes(b"RIFF" + os.urandom(2000))           # changed replacement
        jid = cleanup.confirm_transcript_reviewed(self.store, mid)
        self.run_job(jid, original_roots=[self.intake])
        self.assertTrue(original.exists())
        self.assertIn("changed", self.job(jid)["error"])

        mid, _, original = self.make_meeting(authority=True)
        jid = cleanup.confirm_transcript_reviewed(self.store, mid)
        self.run_job(jid, original_roots=[self.intake])
        self.assertFalse(original.exists())

    def job(self, jid):
        return self.store._one("SELECT * FROM cleanup_jobs WHERE id = ?", (jid,))

    def test_copy_or_export_alone_never_deletes(self):
        with self.store.tx() as db:
            cleanup.render_reviewed(db, self.vid)                   # what an export uses
        self.assertEqual(self.store._all("SELECT * FROM cleanup_jobs"), [])
        self.assertTrue((self.media / self.mid / "audio.f32").exists())

    def test_quiesce_runs_before_anything_is_removed(self):
        seen = []
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.run_job(jid, quiesce=lambda: seen.append((self.media / self.mid).exists()))
        self.assertEqual(seen, [True])

    def test_late_worker_result_cannot_recreate_content(self):
        epoch = self.store.meeting(self.mid)["content_epoch"]
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.run_job(jid)
        with self.store.tx() as db:      # a pending chunk the stale worker was on
            db.execute("UPDATE chunks SET status = 'pending' WHERE version_id = ?", (self.vid,))
        self.assertFalse(self.store.commit_chunk(self.vid, 0, [Segment(0, 1, "stale")],
                                                 epoch=epoch))

    # -- failures, crashes, holds -----------------------------------------------------

    @unittest.skipUnless(os.name == "nt", "Windows file locking")
    def test_locked_file_fails_honestly_then_retries(self):
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        handle = open(self.media / self.mid / "audio.f32", "rb")      # e.g. a stray reader
        try:
            self.assertEqual(self.run_job(jid), JobResult.FAILED)
            self.assertEqual(self.job(jid)["state"], "failed")
            self.assertIsNone(self.store.meeting(self.mid)["audio_removed_at"])
        finally:
            handle.close()
        self.assertEqual(cleanup.resume_pending(self.store, self.media), {jid: JobResult.DONE})
        self.assertEqual(self.job(jid)["attempts"], 2)

    def test_intent_survives_a_crash_and_is_retried(self):
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.store.close()                                          # crash before removal
        self.store = Store(self.db_path)
        self.assertEqual(cleanup.resume_pending(self.store, self.media), {jid: JobResult.DONE})
        self.assertFalse((self.media / self.mid).exists())

    def test_holds_block_every_attempt_until_released(self):
        hid = new_id()
        with self.store.tx() as db:
            db.execute("INSERT INTO holds (id, meeting_id, scope, reason, created_at) "
                       "VALUES (?, ?, 'audio', 'needed for a dispute', ?)",
                       (hid, self.mid, time.time()))
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.assertEqual(self.run_job(jid), JobResult.HELD)
        self.assertEqual(cleanup.resume_pending(self.store, self.media), {jid: JobResult.HELD})
        self.assertTrue((self.media / self.mid / "audio.f32").exists())
        # An audio hold also blocks transcript cleanup (it removes everything left).
        jid2 = cleanup.confirm_final_saved(self.store, self.mid)
        self.assertEqual(self.run_job(jid2), JobResult.HELD)
        with self.store.tx() as db:
            db.execute("UPDATE holds SET released_at = ? WHERE id = ?", (time.time(), hid))
        results = cleanup.resume_pending(self.store, self.media)
        self.assertEqual(set(results.values()), {JobResult.DONE})

    def add_hold(self, scope="all"):
        with self.store.tx() as db:
            db.execute("INSERT INTO holds (id, meeting_id, scope, reason, created_at) "
                       "VALUES (?, ?, ?, 'late hold', ?)", (new_id(), self.mid, scope, time.time()))

    def test_hold_added_while_stopping_the_worker_still_wins(self):
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.assertEqual(self.run_job(jid, quiesce=self.add_hold), JobResult.HELD)
        self.assertTrue((self.media / self.mid / "audio.f32").exists())
        self.assertEqual(self.job(jid)["state"], "pending")

    def test_hold_added_during_transcript_cleanup_keeps_everything(self):
        self.run_job(cleanup.confirm_transcript_reviewed(self.store, self.mid))
        jid = cleanup.confirm_final_saved(self.store, self.mid)
        real = cleanup._held
        calls = []

        def hold_arrives_late(db, mid, phase):         # after both earlier checks
            calls.append(phase)
            if len(calls) == 3:
                db.execute("INSERT INTO holds (id, meeting_id, scope, reason, created_at) "
                           "VALUES (?, ?, 'all', 'late hold', ?)", (new_id(), mid, time.time()))
            return real(db, mid, phase)
        cleanup._held = hold_arrives_late
        try:
            self.assertEqual(self.run_job(jid), JobResult.HELD)
        finally:
            cleanup._held = real
        self.assertIsNotNone(self.store.meeting(self.mid))
        self.assertTrue(self.store.transcript(self.vid))

    def test_transcript_cleanup_waits_for_audio_cleanup(self):
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        with self.store.tx() as db:
            db.execute("UPDATE cleanup_jobs SET state = 'failed' WHERE id = ?", (jid,))
        jid2 = cleanup.confirm_final_saved(self.store, self.mid)
        self.assertEqual(self.run_job(jid2), JobResult.WAITING)
        self.assertIsNotNone(self.store.source(self.store.version(self.vid)["source_id"]))
        self.assertEqual(cleanup.resume_pending(self.store, self.media),
                         {jid: JobResult.DONE, jid2: JobResult.DONE})

    def test_linked_original_is_kept(self):
        mid, _, original = self.make_meeting(authority=True)
        link = self.intake / "link.wav"
        try:
            link.symlink_to(original)
        except OSError:
            self.skipTest("creating symlinks needs Developer Mode or admin on Windows")
        with self.store.tx() as db:
            db.execute("UPDATE sources SET original_path = ? WHERE meeting_id = ?",
                       (str(link), mid))
        jid = cleanup.confirm_transcript_reviewed(self.store, mid)
        self.assertEqual(self.run_job(jid, original_roots=[self.intake]), JobResult.DONE)
        self.assertTrue(link.exists() and original.exists())

    def test_compaction_error_fails_the_job_instead_of_escaping(self):
        self.run_job(cleanup.confirm_transcript_reviewed(self.store, self.mid))
        jid = cleanup.confirm_final_saved(self.store, self.mid)
        real = cleanup.compact

        def broken(store):
            raise cleanup.StoreError("Could not compact the database: disk full")
        cleanup.compact = broken
        try:
            self.assertEqual(self.run_job(jid), JobResult.FAILED)
        finally:
            cleanup.compact = real
        self.assertEqual(self.job(jid)["state"], "failed")
        self.assertEqual(cleanup.resume_pending(self.store, self.media), {jid: JobResult.DONE})

    # -- checkpoint 2: final output saved -> transcript ----------------------------------

    def test_final_confirmation_removes_all_transcript_content(self):
        # Control: the text really is in the database files beforehand.
        before = b"".join(f.read_bytes() for f in self.root.glob("workspace.db*"))
        self.assertIn(MARKER.encode(), before)
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.run_job(jid)
        jid2 = cleanup.confirm_final_saved(self.store, self.mid)
        self.assertEqual(self.run_job(jid2), JobResult.DONE)
        for table in ("meetings", "sources", "versions", "chunks", "segments", "edits",
                      "attempts", "speaker_runs", "speaker_turns", "segment_speakers",
                      "reviewed_snapshots"):
            self.assertEqual(self.store._one(f"SELECT COUNT(*) AS n FROM {table}")["n"], 0,
                             table)
        # No transcript text left readable in the database files.
        self.store.close()
        for f in self.root.glob("workspace.db*"):
            self.assertNotIn(MARKER.encode(), f.read_bytes(), f.name)
        self.store = Store(self.db_path)


if __name__ == "__main__":
    unittest.main()
