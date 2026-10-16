# workspace/holds.py
# Reasoned retention holds and minimal cleanup receipts (#18).
#
# Holds
#   - Any user may hold a meeting's audio, transcript or everything, with a
#     reason, indefinitely: no approval, no automatic expiry.
#   - Holds live in the database, so they survive restart, and every cleanup
#     attempt checks them first (workspace.cleanup.is_held).
#   - Releasing a hold never deletes anything by itself: cleanup jobs that the
#     hold blocked wait in 'awaiting_confirmation' until the user explicitly
#     confirms that cleanup again.
#   - Holds not reviewed for HOLD_REVIEW_DAYS are listed as due for review
#     while the app is open. A closed app sends no reminders and does not
#     pretend to.
#
# Receipts
#   - A completed cleanup leaves only a receipt: a fresh random id, the time
#     cleanup was requested and completed, and the result. No meeting id,
#     names, filenames or source hashes. Receipts expire RECEIPT_DAYS after
#     the cleanup completed; the cleanup job row itself is removed.
#   - A failed cleanup keeps its job row (with the reason) only until a
#     retry succeeds.

import time

from workspace.store import new_id

DAY = 86400
HOLD_REVIEW_DAYS = 30
RECEIPT_DAYS = 30
SCOPES = ("audio", "transcript", "all")


class HoldError(ValueError):
    """A hold request was refused (message is safe to show)."""


def add_hold(store, mid, scope, reason, now=None):
    reason = (reason or "").strip()
    if scope not in SCOPES:
        raise HoldError("Choose what to keep: the audio, the transcript or everything.")
    if not reason:
        raise HoldError("Give a reason for keeping this material.")
    if store.meeting(mid) is None:
        raise HoldError("Meeting not found.")
    hid, now = new_id(), now or time.time()
    with store.tx() as db:
        db.execute("INSERT INTO holds (id, meeting_id, scope, reason, created_at, reviewed_at) "
                   "VALUES (?, ?, ?, ?, ?, ?)", (hid, mid, scope, reason[:500], now, now))
    return hid


def active_holds(store, mid=None):
    q, args = "SELECT * FROM holds WHERE released_at IS NULL", []
    if mid:
        q += " AND meeting_id = ?"
        args.append(mid)
    return store._all(q + " ORDER BY created_at", args)


def mark_reviewed(store, hid, now=None):
    """The user looked at the hold and kept it."""
    with store.tx() as db:
        db.execute("UPDATE holds SET reviewed_at = ? WHERE id = ? AND released_at IS NULL",
                   (now or time.time(), hid))


def due_for_review(store, now=None):
    """Active holds not reviewed in the last HOLD_REVIEW_DAYS (shown in the app)."""
    cutoff = (now or time.time()) - HOLD_REVIEW_DAYS * DAY
    return store._all("SELECT * FROM holds WHERE released_at IS NULL AND "
                      "COALESCE(reviewed_at, created_at) <= ? ORDER BY created_at", (cutoff,))


def release_hold(store, hid, now=None):
    """Release a hold. Cleanup it was blocking does NOT run: those jobs wait
    for the user's explicit confirmation (confirm_pending_cleanup)."""
    with store.tx() as db:
        hold = db.execute("SELECT * FROM holds WHERE id = ?", (hid,)).fetchone()
        if hold is None or hold["released_at"] is not None:
            return []
        db.execute("UPDATE holds SET released_at = ? WHERE id = ?", (now or time.time(), hid))
        # Only jobs this hold could have blocked: transcript cleanup removes
        # everything left, so every hold covers it; audio needs audio/all.
        audio_too = hold["scope"] in ("audio", "all")
        jobs = [r["id"] for r in db.execute(
            "SELECT id FROM cleanup_jobs WHERE meeting_id = ? AND state IN "
            "('pending', 'failed') AND (phase = 'transcript' OR ?)",
            (hold["meeting_id"], 1 if audio_too else 0))]
        for jid in jobs:
            db.execute("UPDATE cleanup_jobs SET state = 'awaiting_confirmation', error = ? "
                       "WHERE id = ?", ("Hold released: confirm before anything is removed.",
                                        jid))
        return jobs


def confirm_pending_cleanup(store, jid):
    """The user's explicit confirmation for a job that waited on a hold."""
    with store.tx() as db:
        db.execute("UPDATE cleanup_jobs SET state = 'pending', error = NULL "
                   "WHERE id = ? AND state = 'awaiting_confirmation'", (jid,))


def receipts(store):
    return store._all("SELECT * FROM receipts ORDER BY completed_at")


def awaiting_confirmation(store):
    return store._all("SELECT * FROM cleanup_jobs WHERE state = 'awaiting_confirmation' "
                      "ORDER BY intent_at")


# -- receipts --------------------------------------------------------------------------

def record_receipt(db, job, result, now=None):
    """Within the caller's transaction: replace a finished job with a minimal
    receipt. `result` must not name the meeting, people or files."""
    now = now or time.time()
    db.execute("INSERT INTO receipts (job_id, created_at, completed_at, result) "
               "VALUES (?, ?, ?, ?)", (new_id(), job["intent_at"], now, result[:300]))
    db.execute("DELETE FROM cleanup_jobs WHERE id = ?", (job["id"],))


def purge_expired_receipts(store, now=None):
    """Remove receipts RECEIPT_DAYS after the cleanup completed (call at startup)."""
    cutoff = (now or time.time()) - RECEIPT_DAYS * DAY
    with store.tx() as db:
        return db.execute("DELETE FROM receipts WHERE completed_at <= ?", (cutoff,)).rowcount


def unfinished_work(store, now=None):
    """Meetings still in the workspace with their age and next action, for the
    Active meetings list. Ordinary unfinished work gets no scheduled reminders."""
    now = now or time.time()
    out = []
    held = {r["meeting_id"] for r in store._all(
        "SELECT meeting_id FROM holds WHERE released_at IS NULL")}
    for m in store._all("SELECT * FROM meetings ORDER BY created_at"):
        if m["final_saved_at"]:
            action = "Cleanup pending"
        elif m["reviewed_at"]:
            action = "Hand off, then confirm the final output is saved"
        elif m["phase"] == "transcribed":
            action = "Review the transcript"
        elif m["phase"] == "error":
            action = "Needs attention"
        else:
            action = "Transcribing" if not m["paused"] else "Paused"
        out.append({"id": m["id"], "title": m["title"],
                    "age_days": int((now - m["created_at"]) // DAY), "action": action,
                    "held": m["id"] in held})
    return out
