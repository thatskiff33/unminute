# Retention holds and cleanup receipts (#18). Synthetic meetings only.
#   python -m unittest discover -s tests -v

import importlib.util
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
DAY = 86400

if HAVE_NUMPY:
    import test_cleanup

    from workspace import cleanup, holds
    from workspace.cleanup import JobResult
    from workspace.holds import HoldError
    from workspace.store import Store


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class HoldTests(unittest.TestCase):
    # Same synthetic meeting (audio, clip, original, transcript) as the cleanup tests.
    if HAVE_NUMPY:
        setUp = test_cleanup.CleanupTests.setUp
        tearDown = test_cleanup.CleanupTests.tearDown
        make_meeting = test_cleanup.CleanupTests.make_meeting

    def run_all(self):
        return cleanup.resume_pending(self.store, self.media)

    def test_hold_needs_scope_and_reason(self):
        for scope, reason in (("audio", ""), ("audio", "   "), ("video", "dispute"),
                              (None, "dispute")):
            with self.assertRaises(HoldError):
                holds.add_hold(self.store, self.mid, scope, reason)
        with self.assertRaises(HoldError):
            holds.add_hold(self.store, "nope", "all", "dispute")
        self.assertEqual(holds.active_holds(self.store), [])

    def test_hold_survives_restart_and_blocks_cleanup(self):
        holds.add_hold(self.store, self.mid, "transcript", "client asked us to keep it")
        self.store.close()
        self.store = Store(self.db_path, protector=keyprotect.TestProtector())
        [h] = holds.active_holds(self.store, self.mid)
        self.assertEqual(h["reason"], "client asked us to keep it")
        cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.run_all()                           # audio is not covered by a transcript hold
        self.assertFalse((self.media / self.mid / "audio.f32").exists())
        jid = cleanup.confirm_final_saved(self.store, self.mid)
        self.assertEqual(self.run_all(), {jid: JobResult.HELD})
        self.assertIsNotNone(self.store.meeting(self.mid))

    def test_release_never_deletes_silently(self):
        hid = holds.add_hold(self.store, self.mid, "all", "legal hold")
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        self.assertEqual(self.run_all(), {jid: JobResult.HELD})
        self.assertEqual(holds.release_hold(self.store, hid), [jid])
        self.assertEqual(holds.release_hold(self.store, hid), [])        # idempotent
        # Startup retries and direct runs both leave the waiting job alone.
        self.assertEqual(self.run_all(), {})
        self.assertEqual(cleanup.run_job(self.store, jid, self.media), JobResult.NOTHING_PENDING)
        self.assertTrue((self.media / self.mid / "audio.f32").exists())
        [waiting] = holds.awaiting_confirmation(self.store)
        self.assertIn("confirm", waiting["error"])
        # Confirming the same checkpoint again is the explicit confirmation.
        self.assertEqual(cleanup.confirm_transcript_reviewed(self.store, self.mid), jid)
        self.assertEqual(self.run_all(), {jid: JobResult.DONE})
        self.assertFalse((self.media / self.mid / "audio.f32").exists())

    def test_release_racing_a_held_attempt_still_needs_confirmation(self):
        hid = holds.add_hold(self.store, self.mid, "audio", "dispute")
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        holds.release_hold(self.store, hid)
        cleanup._mark(self.store, jid, "pending", cleanup.HELD_NOTE)   # a late "held" write
        self.assertEqual(self.run_all(), {})
        self.assertTrue((self.media / self.mid / "audio.f32").exists())

    def test_release_only_touches_jobs_the_hold_covered(self):
        hid = holds.add_hold(self.store, self.mid, "transcript", "audit")
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        with self.store.tx() as db:            # e.g. a locked file; not caused by the hold
            db.execute("UPDATE cleanup_jobs SET state = 'failed' WHERE id = ?", (jid,))
        self.assertEqual(holds.release_hold(self.store, hid), [])
        self.assertEqual(self.run_all(), {jid: JobResult.DONE})

    def test_another_active_hold_still_blocks_after_confirmation(self):
        h1 = holds.add_hold(self.store, self.mid, "audio", "dispute")
        holds.add_hold(self.store, self.mid, "all", "audit")
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        holds.release_hold(self.store, h1)
        holds.confirm_pending_cleanup(self.store, jid)
        self.assertEqual(self.run_all(), {jid: JobResult.HELD})
        self.assertTrue((self.media / self.mid / "audio.f32").exists())

    def test_review_reminders_only_for_stale_holds(self):
        now = time.time()
        old = holds.add_hold(self.store, self.mid, "all", "old", now=now - 31 * DAY)
        holds.add_hold(self.store, self.mid, "audio", "recent", now=now - 5 * DAY)
        self.assertEqual([h["id"] for h in holds.due_for_review(self.store, now)], [old])
        holds.mark_reviewed(self.store, old, now=now)
        self.assertEqual(holds.due_for_review(self.store, now), [])
        self.assertEqual(len(holds.due_for_review(self.store, now + 31 * DAY)), 2)
        # Holds never expire on their own.
        self.assertEqual(len(holds.active_holds(self.store, self.mid)), 2)

    def test_receipt_is_minimal_and_expires(self):
        cleanup.confirm_transcript_reviewed(self.store, self.mid)
        cleanup.confirm_final_saved(self.store, self.mid)
        self.run_all()
        self.assertIsNone(self.store.meeting(self.mid))
        self.assertEqual(self.store._all("SELECT * FROM cleanup_jobs"), [])
        rs = holds.receipts(self.store)
        self.assertEqual(len(rs), 2)
        for r in rs:
            self.assertEqual(set(r.keys()), {"job_id", "created_at", "completed_at", "result"})
            blob = " ".join(str(r[k]) for k in r.keys())
            for private in (self.mid, "Synthetic", self.original.name, str(self.intake)):
                self.assertNotIn(private, blob)
        self.assertEqual(holds.purge_expired_receipts(self.store, time.time() + 29 * DAY), 0)
        self.assertEqual(holds.purge_expired_receipts(self.store, time.time() + 31 * DAY), 2)
        self.assertEqual(holds.receipts(self.store), [])

    def test_unfinished_work_lists_age_and_next_action(self):
        holds.add_hold(self.store, self.mid, "audio", "dispute")
        [row] = holds.unfinished_work(self.store, now=time.time() + 3 * DAY)
        self.assertEqual(row["age_days"], 3)
        self.assertTrue(row["held"])
        self.assertTrue(row["action"])


if __name__ == "__main__":
    unittest.main()
