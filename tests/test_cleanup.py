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


sys.path.append(str(Path(__file__).resolve().parents[1] / "tools"))
import decoder_runtime  # noqa: E402  (tools/decoder_runtime.py)


def setUpModule():
    # #299: the decoder runs in an AppContainer that cannot read this Python:
    # decodes here use a scratch runtime it can read and a throwaway profile.
    decoder_runtime.use()


def tearDownModule():
    decoder_runtime.stop()


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
            # #111 C3c: the audio key goes before any file, so the audio
            # already cannot be read and is recorded as removed; only the
            # leftover ciphertext waits for the retry (design 6.2).
            self.assertIsNotNone(self.store.meeting(self.mid)["audio_removed_at"])
            self.assertEqual(0, self.audio_rows(self.mid))
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

    # -- crypto-erase first (#111 C3c, design part 2, section 6) ------------------------

    def audio_rows(self, mid):
        return self.store._one("SELECT COUNT(*) FROM content_keys WHERE meeting_id = ? AND "
                               "purpose = 'audio'", (mid,))[0]

    def checkpointed_key(self, mid, purpose):
        """A meeting key's wrapped bytes, after a forced checkpoint, so they
        are in the main database file (where only secure_delete and a
        checkpoint can remove them), not only in the write-ahead log."""
        wrapped = bytes(self.store._one("SELECT wrapped FROM content_keys WHERE meeting_id = ? "
                                        "AND purpose = ?", (mid, purpose))[0])
        with self.store._lock:
            busy = self.store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        self.assertEqual(0, busy)
        self.assertIn(wrapped, self.db_path.read_bytes())
        return wrapped

    def in_database_files(self, needle):
        return [p.name for p in (self.db_path, self.db_path.with_name("workspace.db-wal"))
                if p.exists() and needle in p.read_bytes()]

    def state(self, mid):
        """(audio key present, audio file present, audio recorded as removed)."""
        return (self.audio_rows(mid) == 1, (self.media / mid / "audio.uma").exists(),
                self.store.meeting_state(mid)["audio_removed_at"] is not None)

    def test_the_audio_key_goes_before_any_file(self):
        # A folder that cannot be removed: the key is already gone and the
        # audio recorded as removed (it cannot be read any more); the job is
        # failed and retried for the files.
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        with mock.patch("shutil.rmtree", side_effect=PermissionError(13, "in use")):
            self.assertEqual(JobResult.FAILED, self.run_job(jid))
        self.assertEqual((False, True, True), self.state(self.mid))
        self.assertEqual("failed", self.job(jid)["state"])
        self.assertEqual({jid: JobResult.DONE}, cleanup.resume_pending(self.store, self.media))
        self.assertFalse((self.media / self.mid).exists())

    def test_crypto_erase_leaves_a_copy_of_the_audio_unreadable(self):
        from workspace import audiocrypt, media
        from workspace.store import KeyMissing
        path = self.media / self.mid / media.AUDIO_FILE
        path.unlink()
        key = self.store.meeting_key(self.mid, "audio")
        with audiocrypt.AudioWriter(path, key, media.audio_context(self.mid)) as w:
            w.write(bytes(4 * SR))                            # one second of silence
        copy = self.root / "copy.uma"                          # e.g. a stray backup
        copy.write_bytes(path.read_bytes())
        with audiocrypt.AudioReader(copy, key, media.audio_context(self.mid)) as r:
            self.assertEqual(SR, len(r))                       # control: it opens now
        self.run_job(cleanup.confirm_transcript_reviewed(self.store, self.mid))
        self.assertEqual(0, self.audio_rows(self.mid))
        with self.assertRaises(KeyMissing):
            self.store.meeting_key(self.mid, "audio")
        # Every key the store can still produce: the meeting's text key, the
        # workspace key, and any recording key.
        keys = [self.store.meeting_key(self.mid, "text"), self.store.keyring.workspace_key]
        keys += [self.store.recording_key(r) for r in self.store.recording_key_ids()]
        for k in keys:
            with self.assertRaises(audiocrypt.AudioCryptError):
                audiocrypt.AudioReader(copy, k, media.audio_context(self.mid))

    def test_the_track_activity_file_goes_with_the_audio_and_a_copy_cannot_be_opened(self):
        # #108 slice 5: a two-track meeting's track-activity file is part of
        # its audio (C1): removed with the folder at audio cleanup, sealed
        # under the audio key, so a stray copy opens with no key left.
        import numpy as np

        from workspace import activity
        from workspace.store import KeyMissing
        folder = self.media / self.mid
        n = 3000
        result = (np.full(n, -30, np.int8), np.full(n, -40, np.int8), np.ones(n, np.uint8))
        activity.write(folder, self.store.meeting_key(self.mid, "audio"), self.mid, result)
        copy = self.root / "copy"
        copy.mkdir()
        (copy / activity.FILE).write_bytes((folder / activity.FILE).read_bytes())
        activity.read(copy, self.store.meeting_key(self.mid, "audio"), self.mid)   # control
        self.assertEqual(self.run_job(cleanup.confirm_transcript_reviewed(self.store, self.mid)),
                         JobResult.DONE)
        self.assertFalse(folder.exists())
        with self.assertRaises(KeyMissing):
            self.store.meeting_key(self.mid, "audio")
        keys = [self.store.meeting_key(self.mid, "text"), self.store.keyring.workspace_key]
        for k in keys:
            with self.assertRaises(activity.ActivityError):
                activity.read(copy, k, self.mid)

    def test_no_wrapped_audio_key_is_left_in_the_database_files(self):
        wrapped = self.checkpointed_key(self.mid, "audio")
        self.run_job(cleanup.confirm_transcript_reviewed(self.store, self.mid))
        self.assertEqual([], self.in_database_files(wrapped))     # nor in the log
        # The text key after transcript cleanup (already so; pinned here).
        text = self.checkpointed_key(self.mid, "text")
        self.run_job(cleanup.confirm_final_saved(self.store, self.mid))
        self.assertEqual([], self.in_database_files(text))

    def test_a_hold_added_after_the_epoch_transaction_keeps_the_key(self):
        # Critique 9: cleanup_started is told after run_job's epoch
        # transaction and before _remove_audio's key transaction, the gap
        # the key transaction's own hold check closes.
        def hold_now(kind, **fields):
            if kind == "cleanup_started":
                self.add_hold("audio")
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.assertEqual(JobResult.HELD, self.run_job(jid, on_event=hold_now))
        self.assertEqual((True, True, False), self.state(self.mid))
        self.assertEqual("pending", self.job(jid)["state"])

    def test_a_crash_at_any_step_leaves_key_and_audio_together_or_both_gone(self):
        """A fault between each step of _remove_audio (the C7 design's
        offsets, moved here): every landing leaves either the audio key and
        the audio intact and not recorded as removed, or the key gone and
        the audio recorded as removed; never a key gone with the audio shown
        as available. The rerun finishes with one receipt."""
        class Crash(BaseException):
            """The process ends here (nothing catches it)."""

        def part_of_the_folder(folder, *a, **k):
            (Path(folder) / "audio.uma").unlink()
            raise Crash()
        landings = {
            "in the key transaction": ("workspace.store.Store.remove_audio_keys", Crash),
            "before the checkpoint": ("workspace.store.Store.erase_checkpoint", Crash),
            "before the folder": ("shutil.rmtree", Crash),
            "part of the folder": ("shutil.rmtree", part_of_the_folder),
            "before the originals": ("workspace.cleanup._remove_original", Crash),
            "before the receipt": ("workspace.cleanup.record_receipt", Crash),
        }
        for name, (target, effect) in landings.items():
            with self.subTest(landing=name):
                mid, _, original = self.make_meeting(authority=True)
                jid = cleanup.confirm_transcript_reviewed(self.store, mid)
                receipts = self.store._one("SELECT COUNT(*) FROM receipts")[0]
                with mock.patch(target, side_effect=effect), self.assertRaises(Crash):
                    self.run_job(jid, original_roots=[self.intake])
                key, audio, removed = self.state(mid)
                self.assertIn((key, audio, removed), {(True, True, False), (False, audio, True)})
                self.assertEqual("pending", self.job(jid)["state"])
                self.assertEqual(JobResult.DONE, self.run_job(jid, original_roots=[self.intake]))
                self.assertEqual((False, False, True), self.state(mid))
                self.assertFalse(original.exists())
                self.assertEqual(receipts + 1,
                                 self.store._one("SELECT COUNT(*) FROM receipts")[0])

    def test_a_busy_checkpoint_fails_the_job_with_a_fixed_note_and_retries(self):
        import sqlite3
        import threading
        from workspace.store import Store as S
        mid2, _, _ = self.make_meeting()
        reader = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM receipts").fetchone()   # a read snapshot
            jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
            with mock.patch.object(S, "ERASE_RETRIES_S", (0.01, 0.01)):
                self.assertEqual(JobResult.FAILED, self.run_job(jid))     # held throughout
            self.assertEqual("failed", self.job(jid)["state"])
            self.assertEqual(f"StoreError: {cleanup.ERASE_BUSY}", self.job(jid)["error"])
            # The key is gone, but no file is deleted while the log may still
            # hold a copy of it.
            self.assertEqual((False, True, True), self.state(self.mid))
            reader.rollback()
            self.assertEqual({jid: JobResult.DONE},
                             cleanup.resume_pending(self.store, self.media))
            self.assertFalse((self.media / self.mid).exists())
            # A reader that finishes within the retries: the job just finishes.
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM receipts").fetchone()
            jid2 = cleanup.confirm_transcript_reviewed(self.store, mid2)
            timer = threading.Timer(0.05, reader.rollback)
            timer.start()
            with mock.patch.object(S, "ERASE_RETRIES_S", (0.2, 0.2, 0.2)):
                self.assertEqual(JobResult.DONE, self.run_job(jid2))
            timer.join()
        finally:
            reader.close()

    def test_discard_destroys_both_keys(self):
        wrapped = [self.checkpointed_key(self.mid, p) for p in ("audio", "text")]
        audio, transcript = cleanup.discard(self.store, self.mid)
        self.assertEqual(JobResult.DONE, self.run_job(audio))
        self.assertEqual(JobResult.DONE, self.run_job(transcript))
        self.assertEqual(0, self.store._one("SELECT COUNT(*) FROM content_keys WHERE "
                                            "meeting_id = ?", (self.mid,))[0])
        for blob in wrapped:
            self.assertEqual([], self.in_database_files(blob))

    def test_a_failed_or_abandoned_import_erases_its_keys(self):
        from workspace.store import compaction_marker
        for remove in (lambda mid: self.store.delete_new_meeting(mid),
                       lambda mid: self.store.discard_unfinished_import(mid, lambda: None)):
            mid = self.store.create_meeting("synthetic unfinished")
            wrapped = self.checkpointed_key(mid, "audio")
            with mock.patch.object(self.store, "erase_checkpoint",
                                   wraps=self.store.erase_checkpoint) as erase:
                remove(mid)
            self.assertEqual(1, erase.call_count)
            self.assertEqual(0, self.store._one("SELECT COUNT(*) FROM content_keys WHERE "
                                                "meeting_id = ?", (mid,))[0])
            self.assertEqual([], self.in_database_files(wrapped))
        # A log that stays busy: the removal stands, and the compaction is
        # owed to the next sound start.
        mid = self.store.create_meeting("synthetic unfinished")
        self.assertFalse(compaction_marker(self.db_path).exists())
        with mock.patch.object(self.store, "erase_checkpoint", return_value=False):
            self.store.delete_new_meeting(mid)
        self.assertIsNone(self.store.meeting_state(mid))
        self.assertTrue(compaction_marker(self.db_path).exists())

    def test_a_failed_import_destroys_its_keys_before_its_folder(self):
        from workspace import media
        seen = []
        real = cleanup.shutil.rmtree

        def rmtree(folder, *a, **k):
            mid = Path(folder).name
            seen.append(self.store._one("SELECT COUNT(*) FROM content_keys WHERE "
                                        "meeting_id = ?", (mid,))[0])
            return real(folder, *a, **k)

        def decode(src, dest, *a, **k):
            Path(dest).write_bytes(b"partial")
            raise media.DecodeError("synthetic failure")
        with mock.patch.object(media, "decode_to_file", decode), \
                mock.patch("shutil.rmtree", rmtree), self.assertRaises(media.DecodeError):
            media.import_recording(self.store, self.media, self.original)
        self.assertEqual([0], seen)                     # no key left when files go
        self.assertEqual(1, self.store._one("SELECT COUNT(*) FROM meetings")[0])  # setUp's

    def test_an_audit_log_that_raises_never_changes_the_result(self):
        def broken(kind, **fields):
            raise RuntimeError("the audit log failed")
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.assertEqual(self.run_job(jid, on_event=broken), JobResult.DONE)
        self.assertFalse((self.media / self.mid).exists())
        self.assertIsNone(self.job(jid))                  # the receipt replaced it


if __name__ == "__main__":
    unittest.main()
