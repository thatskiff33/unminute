# Retention holds and cleanup receipts (#18). Synthetic meetings only.
#   python -m unittest discover -s tests -v

import importlib.util
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
DAY = 86400

from workspace import holds  # noqa: E402  (no numpy needed: RetentionTests)

if HAVE_NUMPY:
    import test_cleanup

    from workspace import cleanup
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
        self.assertFalse((self.media / self.mid / "audio.uma").exists())
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
        self.assertTrue((self.media / self.mid / "audio.uma").exists())
        # Nor is a key destroyed (#111 C3c: the key goes first at cleanup).
        self.assertEqual({"audio", "text"}, self.key_purposes())
        [waiting] = holds.awaiting_confirmation(self.store)
        self.assertIn("confirm", waiting["error"])
        # Confirming the same checkpoint again is the explicit confirmation.
        self.assertEqual(cleanup.confirm_transcript_reviewed(self.store, self.mid), jid)
        self.assertEqual(self.run_all(), {jid: JobResult.DONE})
        self.assertFalse((self.media / self.mid / "audio.uma").exists())
        self.assertEqual({"text"}, self.key_purposes())

    def key_purposes(self):
        return {r[0] for r in self.store._all("SELECT purpose FROM content_keys WHERE "
                                              "meeting_id = ?", (self.mid,))}

    def test_release_racing_a_held_attempt_still_needs_confirmation(self):
        hid = holds.add_hold(self.store, self.mid, "audio", "dispute")
        jid = cleanup.confirm_transcript_reviewed(self.store, self.mid)
        holds.release_hold(self.store, hid)
        cleanup._mark(self.store, jid, "pending", cleanup.HELD_NOTE)   # a late "held" write
        self.assertEqual(self.run_all(), {})
        self.assertTrue((self.media / self.mid / "audio.uma").exists())

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
        self.assertTrue((self.media / self.mid / "audio.uma").exists())

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


class RetentionTests(unittest.TestCase):
    """Retention reminders (#111 C6): each step's clock, the thresholds, and
    holds by the scope of the cleanup each step leads to. Plain columns
    only; nothing here deletes, holds or changes a job."""

    def setUp(self):
        import tempfile

        from workspace import policy
        from workspace.store import Store as PlainStore
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PlainStore(Path(self.tmp.name) / "workspace.db",
                                protector=keyprotect.TestProtector())
        self.now = time.time()
        self.defaults = policy.Policy()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def meeting(self, created_days_ago, reviewed_days_ago=None, saved=False):
        mid = self.store.create_meeting("Synthetic retention meeting")
        with self.store.tx() as db:
            db.execute("UPDATE meetings SET created_at = ?, reviewed_at = ?, final_saved_at = ? "
                       "WHERE id = ?",
                       (self.now - created_days_ago * DAY,
                        None if reviewed_days_ago is None else self.now - reviewed_days_ago * DAY,
                        self.now if saved else None, mid))
        return mid

    def job(self, mid, phase, state, days_ago):
        with self.store.tx() as db:
            db.execute("INSERT INTO cleanup_jobs (id, meeting_id, phase, state, intent_at) "
                       "VALUES (?, ?, ?, ?, ?)",
                       (f"{mid}-{phase}-{state}", mid, phase, state, self.now - days_ago * DAY))

    def hold(self, mid, scope, released_days_ago=None):
        hid = holds.add_hold(self.store, mid, scope, "Synthetic reason",
                             now=self.now - 60 * DAY)
        if released_days_ago is not None:
            with self.store.tx() as db:
                db.execute("UPDATE holds SET released_at = ? WHERE id = ?",
                           (self.now - released_days_ago * DAY, hid))
        return hid

    def state(self, p=None, now=None):
        return {r["meeting"]: r for r in holds.retention_state(
            self.store, p or self.defaults, now or self.now)}

    def test_each_step_counts_from_its_own_clock(self):
        review = self.meeting(31)
        hand_off = self.meeting(60, reviewed_days_ago=5)
        unfinished = self.meeting(60, reviewed_days_ago=50, saved=True)
        self.job(unfinished, "transcript", "pending", 40)
        waiting = self.meeting(60, reviewed_days_ago=55)
        self.job(waiting, "audio", "awaiting_confirmation", 50)
        self.hold(waiting, "audio", released_days_ago=3)
        self.hold(waiting, "all", released_days_ago=8)       # the latest release counts
        done = self.meeting(60, reviewed_days_ago=50, saved=True)  # nothing left to run
        rows = self.state()
        self.assertEqual((rows[review]["step"], rows[review]["days"], rows[review]["state"]),
                         (holds.REVIEW, 31, holds.DUE))
        self.assertEqual((rows[hand_off]["step"], rows[hand_off]["days"],
                          rows[hand_off]["state"]), (holds.HAND_OFF, 5, None))
        self.assertEqual((rows[unfinished]["step"], rows[unfinished]["days"]),
                         (holds.CLEANUP_UNFINISHED, 40))
        self.assertEqual((rows[waiting]["step"], rows[waiting]["days"]),
                         (holds.CONFIRM_CLEANUP, 3))
        self.assertNotIn(done, rows)

    def test_a_cleanup_job_wins_over_the_meetings_stage(self):
        # C6-2: reviewed two days ago (hand off), with a failed audio job from
        # 20 days ago: the job is the step, counted from its intent.
        mid = self.meeting(25, reviewed_days_ago=2)
        self.job(mid, "audio", "failed", 20)
        row = self.state()[mid]
        self.assertEqual((row["step"], row["days"], row["since"]),
                         (holds.CLEANUP_UNFINISHED, 20, self.now - 20 * DAY))

    def test_thresholds_at_their_boundaries(self):
        from workspace import policy
        mids = {d: self.meeting(d) for d in (13, 14, 29, 30)}

        def got(p):
            rows = self.state(p)
            return {d: rows[m]["state"] for d, m in mids.items()}
        self.assertEqual(got(self.defaults), {13: None, 14: None, 29: None, 30: holds.DUE})
        self.assertEqual(got(policy.Policy({"ReviewReminderDays": 0})),
                         {13: None, 14: None, 29: None, 30: None})            # 0 is off
        self.assertEqual(got(policy.Policy({"ActionRequiredDays": 14})),
                         {13: None, 14: holds.ACTION_REQUIRED, 29: holds.ACTION_REQUIRED,
                          30: holds.ACTION_REQUIRED})
        # Action required below the reminder threshold wins from its own.
        self.assertEqual(got(policy.Policy({"ReviewReminderDays": 29,
                                            "ActionRequiredDays": 14})),
                         {13: None, 14: holds.ACTION_REQUIRED, 29: holds.ACTION_REQUIRED,
                          30: holds.ACTION_REQUIRED})
        self.assertEqual(got(policy.Policy({"ReviewReminderDays": 14,
                                            "ActionRequiredDays": 30})),
                         {13: None, 14: holds.DUE, 29: holds.DUE, 30: holds.ACTION_REQUIRED})
        # A bad value is the app's default, never something stricter.
        self.assertEqual(got(policy.Policy(invalid=("ReviewReminderDays",
                                                    "ActionRequiredDays"))),
                         got(self.defaults))
        # A start in the future (the clock moved back) counts as 0 days.
        self.assertEqual(self.state(now=self.now - 40 * DAY)[mids[14]]["days"], 0)

    def test_holds_exempt_only_the_steps_whose_cleanup_they_block(self):
        # C6-1: review leads to audio cleanup; hand off to transcript cleanup,
        # which every hold blocks.
        unreviewed = {scope: self.meeting(40) for scope in holds.SCOPES}
        reviewed = {scope: self.meeting(80, reviewed_days_ago=40) for scope in holds.SCOPES}
        for scope in holds.SCOPES:
            self.hold(unreviewed[scope], scope)
            self.hold(reviewed[scope], scope)
        rows = self.state()
        self.assertEqual({s: rows[m]["state"] for s, m in unreviewed.items()},
                         {"audio": None, "transcript": holds.DUE, "all": None})
        self.assertEqual({s: rows[m]["state"] for s, m in reviewed.items()},
                         {"audio": None, "transcript": None, "all": None})
        # A released hold exempts nothing.
        released = self.meeting(40)
        self.hold(released, "all", released_days_ago=1)
        self.assertEqual(self.state()[released]["state"], holds.DUE)

    @unittest.skipUnless(HAVE_NUMPY, "numpy not installed (workspace.cleanup)")
    def test_the_exemption_follows_cleanups_own_rule(self):
        # One rule for cleanup and the reminders: a change to it changes both.
        self.assertIs(cleanup.hold_scopes, holds.hold_scopes)
        mid = self.meeting(40)
        self.hold(mid, "transcript")
        self.assertEqual(self.state()[mid]["state"], holds.DUE)
        everything = lambda phase: ("all", "audio", "transcript")  # noqa: E731
        with mock.patch.object(holds, "hold_scopes", everything):
            self.assertIsNone(self.state()[mid]["state"])

    @unittest.skipUnless(HAVE_NUMPY, "numpy not installed (workspace.cleanup)")
    def test_the_state_clears_when_the_step_is_done_held_or_discarded(self):
        reviewed, held, discarded = self.meeting(40), self.meeting(40), self.meeting(40)
        self.assertEqual({self.state()[m]["state"] for m in (reviewed, held, discarded)},
                         {holds.DUE})
        with self.store.tx() as db:                    # the review confirmed now
            db.execute("UPDATE meetings SET reviewed_at = ? WHERE id = ?", (self.now, reviewed))
        self.hold(held, "audio")
        cleanup.discard(self.store, discarded, unfinished=True)
        rows = self.state(now=time.time())
        self.assertEqual((rows[reviewed]["step"], rows[reviewed]["state"]),
                         (holds.HAND_OFF, None))
        self.assertIsNone(rows[held]["state"])
        self.assertEqual((rows[discarded]["step"], rows[discarded]["state"]),
                         (holds.CLEANUP_UNFINISHED, None))
        with self.store.tx() as db:                    # cleaned up: the meeting is gone
            db.execute("DELETE FROM cleanup_jobs WHERE meeting_id = ?", (discarded,))
            self.store.delete_meeting_row(db, discarded)
        self.assertNotIn(discarded, self.state())

    def test_held_overdue_counts_held_meetings_past_the_threshold(self):
        from workspace import policy
        past, inside, unheld = self.meeting(20), self.meeting(10), self.meeting(20)
        self.hold(past, "all")
        self.hold(inside, "all")
        p = policy.Policy({"ReviewReminderDays": 7, "ActionRequiredDays": 14})
        rows = self.state(p)
        self.assertEqual([rows[m]["held_overdue"] for m in (past, inside, unheld)],
                         [True, False, False])
        self.assertEqual([rows[m]["state"] for m in (past, inside, unheld)],
                         [None, None, holds.ACTION_REQUIRED])
        self.assertEqual(holds.retention_counts(list(rows.values())),
                         {"reminders_due": 0, "action_required": 1, "held_overdue": 1})
        # With action required off, the reminder threshold is the one IT sees.
        rows = self.state(policy.Policy({"ReviewReminderDays": 7}))
        self.assertEqual([rows[m]["held_overdue"] for m in (past, inside)], [True, True])
        self.assertFalse(any(r["held_overdue"] for r in self.state(
            policy.Policy({"ReviewReminderDays": 0})).values()))

    def test_working_it_out_changes_nothing_and_costs_three_queries(self):
        for days in (40, 20, 5):
            mid = self.meeting(days)
            self.hold(mid, "transcript")
            self.job(mid, "audio", "failed", days)
        tables = ("meetings", "holds", "cleanup_jobs", "receipts")

        def snapshot():
            return {t: [tuple(r) for r in self.store._all(f"SELECT * FROM {t}")] for t in tables}
        before, seen = snapshot(), []
        self.store.db.set_trace_callback(seen.append)
        try:
            self.state()
            three = len(seen)
            self.store.db.set_trace_callback(None)
            for _ in range(20):
                self.meeting(40)
            more = snapshot()
            self.store.db.set_trace_callback(seen.append)
            seen.clear()
            self.state()
        finally:
            self.store.db.set_trace_callback(None)
        self.assertEqual((three, len(seen)), (3, 3))
        self.assertEqual({t: v for t, v in snapshot().items() if t != "meetings"},
                         {t: v for t, v in before.items() if t != "meetings"})
        self.assertEqual(snapshot(), more)


if __name__ == "__main__":
    unittest.main()
