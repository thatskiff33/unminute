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
# Retention reminders (#111 C6)
#   - Each meeting's next step (confirm a cleanup, a cleanup that did not
#     finish, hand off, review) has its own clock. A step older than the
#     policy's ReviewReminderDays (default 30) is "Review due"; older than
#     ActionRequiredDays (default 0, off) it is "Action required". Shown while
#     the app is open, and counted by --status and --health for IT.
#   - Worked out from plain columns only (no title is opened) and stored
#     nowhere. It blocks, deletes and expires nothing (C1, C2).
#   - A step is exempt while a hold blocks the cleanup it leads to
#     (cleanup.hold_scopes); such a meeting past the threshold is counted as
#     held_overdue for IT, never shown on its row.
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


def hold_scopes(phase):
    """Hold scopes that block a cleanup phase ("audio" or "transcript"; also
    cleanup.hold_scopes). Transcript cleanup removes everything that remains,
    so any hold blocks it."""
    return ("all", "audio", "transcript") if phase == "transcript" else ("all", phase)


class HoldError(ValueError):
    """A hold request was refused (message is safe to show)."""


def add_hold(store, mid, scope, reason, now=None):
    reason = (reason or "").strip()
    if scope not in SCOPES:
        raise HoldError("Choose what to keep: the audio, the transcript or everything.")
    if not reason:
        raise HoldError("Give a reason for keeping this material.")
    if store.meeting_state(mid) is None:
        raise HoldError("Meeting not found.")
    hid, now = new_id(), now or time.time()
    store.add_hold(hid, mid, scope, reason[:500], now)
    return hid


def active_holds(store, mid=None):
    if mid:
        return store.holds("released_at IS NULL AND meeting_id = ?", (mid,))
    return store.holds("released_at IS NULL")


def mark_reviewed(store, hid, now=None):
    """The user looked at the hold and kept it."""
    with store.tx() as db:
        db.execute("UPDATE holds SET reviewed_at = ? WHERE id = ? AND released_at IS NULL",
                   (now or time.time(), hid))


def due_for_review(store, now=None):
    """Active holds not reviewed in the last HOLD_REVIEW_DAYS (shown in the app)."""
    cutoff = (now or time.time()) - HOLD_REVIEW_DAYS * DAY
    return store.holds("released_at IS NULL AND COALESCE(reviewed_at, created_at) <= ?",
                       (cutoff,))


def release_hold(store, hid, now=None):
    """Release a hold. Cleanup it was blocking does NOT run: those jobs wait
    for the user's explicit confirmation (confirm_pending_cleanup)."""
    with store.tx() as db:
        hold = db.execute("SELECT meeting_id, scope, released_at FROM holds WHERE id = ?",
                          (hid,)).fetchone()
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
    Active meetings list. Retention reminders for that work come from
    retention_state (#111 C6)."""
    now = now or time.time()
    out = []
    held = {r["meeting_id"] for r in store._all(
        "SELECT meeting_id FROM holds WHERE released_at IS NULL")}
    for m in store.meetings(content=("title",)):
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


# -- retention reminders (#111 C6) ----------------------------------------------------

# The steps in the order they win when more than one applies: a cleanup job's
# state comes before the meeting's stage, as it is what stops deletion now.
CONFIRM_CLEANUP, CLEANUP_UNFINISHED, HAND_OFF, REVIEW = (
    "confirm_cleanup", "cleanup_unfinished", "hand_off", "review")
DUE, ACTION_REQUIRED = "due", "action_required"


def thresholds(policy):
    """The reminder thresholds in force, in days (0 = off), and whether IT set
    each one: a value IT did not set validly is the app's default."""
    return {"review_days": policy.get("ReviewReminderDays"),
            "action_days": policy.get("ActionRequiredDays"),
            "managed": {"review": "ReviewReminderDays" in policy.values,
                        "action": "ActionRequiredDays" in policy.values}}


def reminder_level(days, review_days, action_days):
    if action_days and days >= action_days:
        return ACTION_REQUIRED
    if review_days and days >= review_days:
        return DUE
    return None


def retention_from(query, policy, now=None):
    """retention_state's work over any connection: query(sql) returns rows
    indexed by position. Three grouped queries, whatever the number of
    meetings, over plain columns only (no key needed, so --status can use
    it while the workspace is locked)."""
    now = now or time.time()
    limits = thresholds(policy)
    review_days, action_days = limits["review_days"], limits["action_days"]
    # The threshold a held meeting is counted against for IT (C6-3).
    overdue_days = action_days or review_days
    meetings = query("SELECT id, created_at, reviewed_at, final_saved_at FROM meetings "
                     "ORDER BY created_at")
    active, released = {}, {}
    for mid, scope, last_released, open_count in query(
            "SELECT meeting_id, scope, MAX(released_at), SUM(released_at IS NULL) "
            "FROM holds GROUP BY meeting_id, scope"):
        if open_count:
            active.setdefault(mid, set()).add(scope)
        if last_released is not None:
            released[mid] = max(released.get(mid, last_released), last_released)
    jobs = {}
    for mid, phase, state, intent_at in query(
            "SELECT meeting_id, phase, state, MIN(intent_at) FROM cleanup_jobs "
            "GROUP BY meeting_id, phase, state"):
        jobs.setdefault(mid, []).append((phase, state, intent_at))
    out = []
    for mid, created_at, reviewed_at, final_saved_at in meetings:
        mine = jobs.get(mid, [])
        waiting = [j for j in mine if j[1] == "awaiting_confirmation"]
        unfinished = [j for j in mine if j[1] in ("pending", "failed")]
        if waiting:
            # Only release_hold moves a job here: the clock starts at the
            # latest release (the job's intent if no release is recorded).
            step, phases = CONFIRM_CLEANUP, {j[0] for j in waiting}
            since = released.get(mid) or min(j[2] for j in waiting)
        elif unfinished:
            step, phases = CLEANUP_UNFINISHED, {j[0] for j in unfinished}
            since = min(j[2] for j in unfinished)
        elif reviewed_at and not final_saved_at:
            step, phases, since = HAND_OFF, {"transcript"}, reviewed_at
        elif not reviewed_at:
            # Confirming the review starts audio cleanup.
            step, phases, since = REVIEW, {"audio"}, created_at
        else:
            continue                      # final output saved and nothing left to run
        days = max(0, int((now - since) // DAY))     # a start in the future counts as 0
        scopes = active.get(mid, set())
        held = all(scopes & set(hold_scopes(phase)) for phase in phases)
        level = reminder_level(days, review_days, action_days)
        out.append({"meeting": mid, "step": step, "since": since, "days": days,
                    "state": None if held else level,
                    "held_overdue": held and bool(overdue_days) and days >= overdue_days})
    return out


def retention_state(store, policy, now=None):
    """Each meeting's outstanding step, its age in days and its reminder
    state: [{meeting, step, since, days, state, held_overdue}]. state is
    "due", "action_required" or None; held_overdue is True when only a hold
    keeps a step past the threshold quiet (counted for IT, C6-3)."""
    with store._lock:
        return retention_from(store._all, policy, now)    # one consistent view


def retention_counts(rows):
    """Counts only, for --status, --health and Copy diagnostics."""
    return {"reminders_due": sum(r["state"] == DUE for r in rows),
            "action_required": sum(r["state"] == ACTION_REQUIRED for r in rows),
            "held_overdue": sum(bool(r["held_overdue"]) for r in rows)}
