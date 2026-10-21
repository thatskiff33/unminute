# Explicit cleanup checkpoints (#17). Synthetic meetings only.
#   python -m unittest discover -s tests -v

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
SR = 16000
MARKER = "zebracrossing"   # distinctive transcript text to search for after cleanup

if HAVE_NUMPY:
    from workspace.asr_engines import Segment
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
        self.store = Store(self.db_path, protector=keyprotect.TestProtector())
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
        (folder / "audio.uma").write_bytes(os.urandom(4096))     # stands in for the audio
        (folder / "clips" / "c1.wav").write_bytes(b"clip")
        sid = self.store.add_source(mid, original_path=str(original),
                                    original_sha256=sha256_file(original),
                                    original_size=st.st_size, original_mtime_ns=st.st_mtime_ns,
                                    audio_id="b" * 64, samples=30 * SR, sample_rate=SR)
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
        self.assertIn(f"Speaker 1: edited {MARKER} note", self.store.snapshot(self.mid))
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
        self.assertIn("not in a folder", self.last_receipt())

        mid, _, original = self.make_meeting(authority=True)
        time.sleep(0.01)
        original.write_bytes(b"RIFF" + os.urandom(2000))           # changed replacement
        jid = cleanup.confirm_transcript_reviewed(self.store, mid)
        self.run_job(jid, original_roots=[self.intake])
        self.assertTrue(original.exists())
        self.assertIn("changed", self.last_receipt())

        mid, _, original = self.make_meeting(authority=True)
        jid = cleanup.confirm_transcript_reviewed(self.store, mid)
        self.run_job(jid, original_roots=[self.intake])
        self.assertFalse(original.exists())

    def job(self, jid):
        return self.store._one("SELECT * FROM cleanup_jobs WHERE id = ?", (jid,))

    def last_receipt(self):
        return self.store._one("SELECT result FROM receipts ORDER BY completed_at DESC, "
                               "rowid DESC LIMIT 1")["result"]

    def test_copy_or_export_alone_never_deletes(self):
        with self.store.tx():
            cleanup.render_reviewed(self.store, self.vid)           # what an export uses
        self.assertEqual(self.store._all("SELECT * FROM cleanup_jobs"), [])
        self.assertTrue((self.media / self.mid / "audio.uma").exists())

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
        handle = open(self.media / self.mid / "audio.uma", "rb")      # e.g. a stray reader
        try:
            self.assertEqual(self.run_job(jid), JobResult.FAILED)
            self.assertEqual(self.job(jid)["state"], "failed")
            self.assertNotIn(str(self.media), self.job(jid)["error"])     # no paths kept
            self.assertEqual(self.job(jid)["attempts"], 1)
            self.assertIsNone(self.store.meeting(self.mid)["audio_removed_at"])
        finally:
            handle.close()
        self.assertEqual(cleanup.resume_pending(self.store, self.media), {jid: JobResult.DONE})
        self.assertIsNone(self.job(jid))            # replaced by a receipt (#18)

    def test_intent_survives_a_crash_and_is_retried(self):
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.store.close()                                          # crash before removal
        self.store = Store(self.db_path, protector=keyprotect.TestProtector())
        self.assertEqual(cleanup.resume_pending(self.store, self.media), {jid: JobResult.DONE})
        self.assertFalse((self.media / self.mid).exists())

    def test_holds_block_every_attempt_until_released(self):
        hid = new_id()
        self.store.add_hold(hid, self.mid, "audio", "needed for a dispute", time.time())
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.assertEqual(self.run_job(jid), JobResult.HELD)
        self.assertEqual(cleanup.resume_pending(self.store, self.media), {jid: JobResult.HELD})
        self.assertTrue((self.media / self.mid / "audio.uma").exists())
        # An audio hold also blocks transcript cleanup (it removes everything left).
        jid2 = cleanup.confirm_final_saved(self.store, self.mid)
        self.assertEqual(self.run_job(jid2), JobResult.HELD)
        # Releasing the hold removes nothing by itself (#18): both jobs wait.
        from workspace import holds
        self.assertEqual(set(holds.release_hold(self.store, hid)), {jid, jid2})
        self.assertEqual(cleanup.resume_pending(self.store, self.media), {})
        self.assertTrue((self.media / self.mid / "audio.uma").exists())
        for j in (jid, jid2):
            holds.confirm_pending_cleanup(self.store, j)
        results = cleanup.resume_pending(self.store, self.media)
        self.assertEqual(set(results.values()), {JobResult.DONE})

    def add_hold(self, scope="all"):
        self.store.add_hold(new_id(), self.mid, scope, "late hold", time.time())

    def test_hold_added_while_stopping_the_worker_still_wins(self):
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.assertEqual(self.run_job(jid, quiesce=self.add_hold), JobResult.HELD)
        self.assertTrue((self.media / self.mid / "audio.uma").exists())
        self.assertEqual(self.job(jid)["state"], "pending")

    def test_hold_added_during_transcript_cleanup_keeps_everything(self):
        self.run_job(cleanup.confirm_transcript_reviewed(self.store, self.mid))
        jid = cleanup.confirm_final_saved(self.store, self.mid)
        real = cleanup._held
        calls = []

        def hold_arrives_late(db, mid, phase):         # after both earlier checks
            calls.append(phase)
            if len(calls) == 3:          # inside the cleanup's own transaction
                hid = new_id()
                db.execute("INSERT INTO holds (id, meeting_id, scope, reason_sealed, "
                           "created_at) VALUES (?, ?, 'all', ?, ?)",
                           (hid, mid, self.store._seal(mid, "holds", "reason", (hid,),
                                                       "late hold"), time.time()))
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
            sid = db.execute("SELECT id FROM sources WHERE meeting_id = ?", (mid,)).fetchone()[0]
            db.execute("UPDATE sources SET original_path_sealed = ? WHERE id = ?",
                       (self.store._seal(mid, "sources", "original_path", (sid,), str(link)),
                        sid))
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
        # Control: the text really is in the database files beforehand,
        # sealed (#111 C2b-2): its ciphertext is there, its plain text is not.
        self.store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        sealed = [r[0] for r in self.store._all("SELECT text_sealed FROM segments")]
        before = b"".join(f.read_bytes() for f in self.root.glob("workspace.db*"))
        self.assertTrue(sealed and all(blob in before for blob in sealed))
        self.assertNotIn(MARKER.encode(), before)
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.run_job(jid)
        jid2 = cleanup.confirm_final_saved(self.store, self.mid)
        self.assertEqual(self.run_job(jid2), JobResult.DONE)
        for table in ("meetings", "sources", "versions", "chunks", "segments", "edits",
                      "attempts", "speaker_runs", "speaker_turns", "segment_speakers",
                      "reviewed_snapshots"):
            self.assertEqual(self.store._one(f"SELECT COUNT(*) AS n FROM {table}")["n"], 0,
                             table)
        # No transcript text left in the database files, sealed or not, and
        # the meeting's keys are gone.
        self.assertEqual(0, self.store._one("SELECT COUNT(*) FROM content_keys")[0])
        self.store.close()
        for f in self.root.glob("workspace.db*"):
            self.assertNotIn(MARKER.encode(), f.read_bytes(), f.name)
            for blob in sealed:
                self.assertNotIn(blob, f.read_bytes(), f.name)
        self.store = Store(self.db_path, protector=keyprotect.TestProtector())

    # -- the audit log (#111 C5) ------------------------------------------------------------

    def audio_key(self, mid):
        try:
            self.store.meeting_key(mid, "audio")
            return True
        except Exception:
            return False

    def test_the_audit_log_hears_of_cleanup_before_anything_is_removed(self):
        mid, _, original = self.make_meeting(authority=True)
        events = []

        def on_event(kind, **fields):        # what is still there when each is told
            receipts = self.store._one("SELECT COUNT(*) AS n FROM receipts")["n"]
            events.append((kind, fields, (self.media / mid).exists(), original.exists(),
                           self.audio_key(mid), receipts))
        jid = cleanup.confirm_transcript_reviewed(self.store, mid)
        self.assertEqual(self.run_job(jid, original_roots=[self.intake], on_event=on_event),
                         JobResult.DONE)
        started, done = events
        self.assertEqual(started, ("cleanup_started", {"meeting": mid, "phase": "audio",
                                                       "job": jid}, True, True, True, 0))
        self.assertEqual(done, ("cleanup_done", {"meeting": mid, "phase": "audio", "job": jid,
                                                 "originals_deleted": 1, "originals_kept": 0},
                                False, False, False, 1))
        # Kept originals are counted from the fixed outcomes, not the receipt.
        mid, _, original = self.make_meeting(authority=True)
        events.clear()
        self.run_job(cleanup.confirm_transcript_reviewed(self.store, mid),
                     original_roots=[self.root / "elsewhere"], on_event=on_event)
        self.assertEqual((events[1][1]["originals_deleted"], events[1][1]["originals_kept"]),
                         (0, 1))
        events.clear()
        jid = cleanup.confirm_final_saved(self.store, mid)
        self.run_job(jid, on_event=on_event)
        self.assertEqual([(e[0], e[1]["phase"]) for e in events],
                         [("cleanup_started", "transcript"), ("cleanup_done", "transcript")])
        self.assertEqual((events[1][1]["originals_deleted"], events[1][1]["originals_kept"]),
                         (0, 0))

    def test_a_held_job_tells_the_audit_log_nothing(self):
        events = []
        self.add_hold("audio")
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.assertEqual(self.run_job(jid, on_event=lambda k, **f: events.append(k)),
                         JobResult.HELD)
        self.assertEqual(self.run_job(jid, quiesce=self.add_hold,
                                      on_event=lambda k, **f: events.append(k)), JobResult.HELD)
        self.assertEqual(events, [])

    def test_a_failing_job_tells_cleanup_started_once_until_try_again(self):
        """A failed job is retried every minute by the processing loop: only
        the first attempt and the first after Try again are told (#289
        review), so a stuck job never floods the audit log."""
        events = []
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        with mock.patch.object(cleanup, "_remove_audio", side_effect=OSError(13, "in use")):
            for _ in range(4):                            # the first, then automatic retries
                self.assertEqual(self.run_job(jid, on_event=lambda k, **f: events.append(k)),
                                 JobResult.FAILED)
            with self.store.tx() as db:                   # Try again (app.retry_cleanup)
                db.execute("UPDATE cleanup_jobs SET last_attempt_at = NULL WHERE id = ?",
                           (jid,))
            for _ in range(2):
                self.run_job(jid, on_event=lambda k, **f: events.append(k))
        self.assertEqual(self.job(jid)["attempts"], 6)
        self.assertEqual(self.run_job(jid, on_event=lambda k, **f: events.append(k)),
                         JobResult.DONE)
        self.assertEqual(events, ["cleanup_started", "cleanup_started", "cleanup_done"])

    def test_an_audit_log_that_raises_never_changes_the_result(self):
        def broken(kind, **fields):
            raise RuntimeError("the audit log failed")
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.assertEqual(self.run_job(jid, on_event=broken), JobResult.DONE)
        self.assertFalse((self.media / self.mid).exists())
        self.assertIsNone(self.job(jid))                  # the receipt replaced it


if __name__ == "__main__":
    unittest.main()
