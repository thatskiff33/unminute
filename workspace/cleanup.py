# workspace/cleanup.py
# Explicit cleanup checkpoints (#17). See docs/WORKSPACE_DESIGN.md.
#
# Two confirmations, never anything implicit (copy/export does not count):
#   1. confirm_transcript_reviewed(): freezes the reviewed text in the
#      database, then removes the app's copy of the audio (and an intake
#      original only with explicit authority and an unchanged identity).
#   2. confirm_final_saved(): after the user confirms the final deliverable
#      is reviewed and saved outside the app, removes all transcript working
#      content: text, edits, speaker data, attempts, versions, snapshot.
#
# Each step persists its intent as a cleanup job BEFORE removing anything, so
# a crash or a failed deletion leaves a pending/failed job that is retried
# (resume_pending) instead of being forgotten. Before removing anything the
# worker is stopped and file handles released, and the meeting's content
# epoch is bumped so a late worker result cannot recreate removed content.
# Holds (#18) are checked before every attempt; a finished job leaves only a
# minimal receipt (workspace.holds).

import hashlib
import os
import shutil
import sqlite3
import time
from pathlib import Path

from workspace import safe_paths
from workspace.holds import record_receipt
from workspace.media import sha256_file
from workspace.store import StoreError, new_id

AUDIO, TRANSCRIPT = "audio", "transcript"

# Shown to the user with each confirmation (wording reviewed in #22/#29).
LIMITS = (
    "This removes the app's own working copies on this computer. It cannot remove copies "
    "elsewhere: synced or backed-up folders (for example OneDrive), the original on a phone "
    "or recorder, or anything you shared with an AI assistant or other service. Ordinary "
    "deletion is not a forensic erasure of the disk.")


class CleanupError(RuntimeError):
    """A confirmation was refused (message is safe to show)."""


class JobResult:
    DONE, FAILED, HELD, WAITING, NOTHING_PENDING = (
        "done", "failed", "held", "waiting", "nothing_pending")


# -- confirmations ----------------------------------------------------------------

def confirm_transcript_reviewed(store, mid):
    """Checkpoint 1: freeze the reviewed transcript and record the intent to
    remove the meeting's audio. Removal itself happens in run_job()."""
    with store.tx() as db:
        meeting = db.execute("SELECT * FROM meetings WHERE id = ?", (mid,)).fetchone()
        if meeting is None:
            raise CleanupError("Meeting not found.")
        version = db.execute("SELECT id FROM versions WHERE meeting_id = ? AND state = 'active'",
                             (mid,)).fetchone()
        if version is None or db.execute("SELECT 1 FROM chunks WHERE version_id = ? AND "
                                         "status = 'pending'", (version["id"],)).fetchone():
            raise CleanupError("The transcript is not finished yet.")
        if db.execute("SELECT 1 FROM speaker_runs WHERE version_id = ? AND status = 'running'",
                      (version["id"],)).fetchone():
            raise CleanupError("Speaker identification is still running.")
        text = render_reviewed(store, version["id"])
        db.execute("INSERT OR REPLACE INTO reviewed_snapshots VALUES (?,?,?,?)",
                   (mid, text, hashlib.sha256(text.encode()).hexdigest(), time.time()))
        db.execute("UPDATE meetings SET reviewed_at = ? WHERE id = ?", (time.time(), mid))
        return _add_job(db, mid, AUDIO)


def confirm_final_saved(store, mid):
    """Checkpoint 2: the final deliverable is reviewed and saved outside the
    app. Records the intent to remove all transcript working content."""
    with store.tx() as db:
        meeting = db.execute("SELECT * FROM meetings WHERE id = ?", (mid,)).fetchone()
        if meeting is None:
            raise CleanupError("Meeting not found.")
        if meeting["reviewed_at"] is None:
            raise CleanupError("Confirm the transcript review first.")
        db.execute("UPDATE meetings SET final_saved_at = ? WHERE id = ?", (time.time(), mid))
        return _add_job(db, mid, TRANSCRIPT)


def discard(store, mid):
    """The user's explicit choice to delete a meeting they do not need (a test
    or a wrong recording), with one confirmation instead of the two review
    checkpoints. Removal uses the same jobs as the checkpoints (secure
    deletion, minimal receipts). Refused while a hold is active. The original
    file is never deleted: any deletion authority for it is withdrawn first."""
    with store.tx() as db:
        meeting = db.execute("SELECT * FROM meetings WHERE id = ?", (mid,)).fetchone()
        if meeting is None:
            raise CleanupError("Meeting not found.")
        if db.execute("SELECT 1 FROM holds WHERE meeting_id = ? AND released_at IS NULL",
                      (mid,)).fetchone():
            raise CleanupError("A retention hold is active on this meeting. It cannot be "
                               "discarded until the hold is released.")
        if meeting["reviewed_at"]:
            # A reviewed meeting is cleaned up through the final-saved
            # confirmation, never discarded (two-checkpoint rule).
            raise CleanupError("This meeting's transcript review is confirmed. Finish it with "
                               "the final confirmation instead.")
        if db.execute("SELECT 1 FROM versions WHERE meeting_id = ? AND state = 'active'",
                      (mid,)).fetchone() is None:
            raise CleanupError("The recording is still being imported. Try again in a moment.")
        db.execute("UPDATE sources SET delete_original = 0 WHERE meeting_id = ?", (mid,))
        now = time.time()
        # Marked reviewed and final-saved: no further processing of the meeting.
        db.execute("UPDATE meetings SET paused = 1, reviewed_at = ?, final_saved_at = ? "
                   "WHERE id = ?", (now, now, mid))
        return _add_job(db, mid, AUDIO), _add_job(db, mid, TRANSCRIPT)


def _add_job(db, mid, phase):
    existing = db.execute("SELECT id FROM cleanup_jobs WHERE meeting_id = ? AND phase = ? AND "
                          "state IN ('pending', 'failed', 'awaiting_confirmation')",
                          (mid, phase)).fetchone()
    if existing:
        # Confirming the same checkpoint again is the explicit confirmation a
        # job needs after its hold was released (#18).
        db.execute("UPDATE cleanup_jobs SET state = 'pending', error = NULL WHERE id = ? "
                   "AND state = 'awaiting_confirmation'", (existing["id"],))
        return existing["id"]
    jid = new_id()
    db.execute("INSERT INTO cleanup_jobs (id, meeting_id, phase, state, intent_at) "
               "VALUES (?, ?, ?, 'pending', ?)", (jid, mid, phase, time.time()))
    return jid


def render_reviewed(store, vid):
    """The reviewed text as plain lines: [time] Speaker: text, with edits and
    the user's speaker names and corrections (#21) applied. Called inside the
    confirmation transaction (the store lock is re-entrant)."""
    from workspace.speaker_names import resolve
    rows = store._all(
        "SELECT s.id, s.start_s, COALESCE(e.text, s.text) AS text FROM segments s "
        "LEFT JOIN edits e ON e.segment_id = s.id WHERE s.version_id = ? "
        "ORDER BY s.start_s, s.id", (vid,))
    view = resolve(store, vid)
    lines = []
    for r in rows:
        h, rest = divmod(int(r["start_s"]), 3600)
        m, s = divmod(rest, 60)
        stamp = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        label = view["labels"].get(r["id"])
        who = f"{view['names'][label]}: " if label else ""
        lines.append(f"[{stamp}] {who}{r['text']}")
    return "\n".join(lines) + "\n"


# -- running jobs ---------------------------------------------------------------------

def run_job(store, jid, media_root, *, quiesce=None, original_roots=()):
    """Perform one cleanup job. `quiesce()` must stop the worker and release
    file handles; `original_roots` are the folders an original may be deleted
    from (deletion authority, #24). Returns a JobResult; a failure leaves the
    job 'failed' with its reason, to be retried."""
    job = store._one("SELECT * FROM cleanup_jobs WHERE id = ?", (jid,))
    if job is None or job["state"] not in ("pending", "failed"):
        return JobResult.NOTHING_PENDING     # finished, or awaiting confirmation after a hold
    mid, phase = job["meeting_id"], job["phase"]
    if is_held(store, mid, phase):
        _mark(store, jid, "pending", HELD_NOTE)
        return JobResult.HELD
    # Audio cleanup must finish first: transcript cleanup removes the source
    # records (identity and deletion authority) that audio cleanup needs.
    if phase == TRANSCRIPT and store._one(
            "SELECT 1 FROM cleanup_jobs WHERE meeting_id = ? AND phase = ? AND state != 'done'",
            (mid, AUDIO)):
        _mark(store, jid, "pending", WAITING_NOTE)
        return JobResult.WAITING
    try:
        if quiesce:
            quiesce()
        with store.tx() as db:       # late worker results are refused from here on
            # Re-check holds atomically: one may have been added while quiescing.
            if _held(db, mid, phase):
                held = True
            else:
                held = False
                db.execute("UPDATE meetings SET content_epoch = content_epoch + 1 "
                           "WHERE id = ?", (mid,))
                db.execute("UPDATE cleanup_jobs SET attempts = attempts + 1, "
                           "last_attempt_at = ? WHERE id = ?", (time.time(), jid))
        if held:
            _mark(store, jid, "pending", HELD_NOTE)
            return JobResult.HELD
        if phase == AUDIO:
            notes = _remove_audio(store, mid, Path(media_root), original_roots)
        else:
            notes = _remove_transcript(store, mid, Path(media_root))
    except HeldDuringCleanup:
        _mark(store, jid, "pending", HELD_NOTE)
        return JobResult.HELD
    except (OSError, StoreError) as exc:
        _mark(store, jid, "failed", _reason(exc))
        return JobResult.FAILED
    # Success: the job row is replaced by a minimal receipt (#18).
    with store.tx() as db:
        record_receipt(db, job, result=f"{phase} removed" + (f"; {notes}" if notes else ""))
    return JobResult.DONE


def resume_pending(store, media_root, *, quiesce=None, original_roots=()):
    """Retry every unfinished job (call at startup). Returns {job_id: result}."""
    jobs = store._all("SELECT id FROM cleanup_jobs WHERE state IN ('pending', 'failed') "
                      "ORDER BY intent_at")
    return {j["id"]: run_job(store, j["id"], media_root, quiesce=quiesce,
                             original_roots=original_roots) for j in jobs}


HELD_NOTE = "Held: kept until the hold is released."
WAITING_NOTE = "Waiting for audio cleanup to finish first."


class HeldDuringCleanup(RuntimeError):
    """A hold was added after the job started; nothing more is removed."""


def _reason(exc):
    """A failure reason without file paths (they name meetings and clients)."""
    if isinstance(exc, OSError):
        return f"{type(exc).__name__}: {exc.strerror or 'file error'}"[:300]
    return f"{type(exc).__name__}: {exc}"[:300]


def hold_scopes(phase):
    """Hold scopes that block a cleanup phase. Transcript cleanup removes
    everything that remains, so any hold blocks it."""
    return ("all", AUDIO, TRANSCRIPT) if phase == TRANSCRIPT else ("all", phase)


def _held(db, mid, phase):
    scopes = hold_scopes(phase)
    return db.execute("SELECT 1 FROM holds WHERE meeting_id = ? AND released_at IS NULL AND "
                      f"scope IN ({','.join('?' * len(scopes))})",
                      (mid, *scopes)).fetchone() is not None


def is_held(store, mid, phase):
    """True if an active hold covers this phase (holds table, #18)."""
    with store._lock:
        return _held(store.db, mid, phase)


def _mark(store, jid, state, error):
    """Update an unfinished job. Never touches a job in another state (for
    example one waiting for confirmation after its hold was released, #18)."""
    try:
        with store.tx() as db:
            db.execute("UPDATE cleanup_jobs SET state = ?, error = ? WHERE id = ? AND "
                       "state IN ('pending', 'failed')", (state, error, jid))
    except StoreError:
        pass


def _remove_audio(store, mid, media_root, original_roots):
    folder = media_root / mid
    if folder.exists():
        shutil.rmtree(folder)         # raises on a locked file: job stays 'failed'
    notes = []
    for src in store._all("SELECT * FROM sources WHERE meeting_id = ?", (mid,)):
        notes.append(_remove_original(src, original_roots))
    with store.tx() as db:
        db.execute("UPDATE meetings SET audio_removed_at = ? WHERE id = ?", (time.time(), mid))
    return "; ".join(n for n in notes if n) or None


def _remove_original(src, roots):
    """Delete an intake original only with explicit authority, inside an
    authorised folder, and only if it is still the exact file imported."""
    path = src["original_path"]
    if not src["delete_original"] or not path:
        return None                                   # no authority: never touched
    try:
        if os.name == "nt":             # Windows path rules (the app is Windows-only)
            safe_paths.check_raw(path)  # never a network, device or stream path
    except safe_paths.UnsafePathError:
        return "Original kept: not a local file path."
    p = Path(path)
    if not roots or not any(_inside(p, Path(r)) for r in roots):
        return "Original kept: not in a folder the app may delete from."
    try:
        st = p.lstat()
    except FileNotFoundError:
        return "Original already gone."
    # A link or junction: deleting it would leave the recording itself behind.
    reparse = getattr(st, "st_file_attributes", 0) & safe_paths.FILE_ATTRIBUTE_REPARSE_POINT
    if p.is_symlink() or reparse:
        return "Original kept: it is a link, not the recording itself."
    if (st.st_size, st.st_mtime_ns) != (src["original_size"], src["original_mtime_ns"]) \
            or sha256_file(p) != src["original_sha256"]:
        return "Original kept: the file changed since it was imported."
    p.unlink()
    return "Original deleted."


def _inside(p, root):
    try:
        return os.path.commonpath([os.path.normcase(os.path.realpath(p)),
                                   os.path.normcase(os.path.realpath(root))]) == \
            os.path.normcase(os.path.realpath(root))
    except ValueError:
        return False


def _remove_transcript(store, mid, media_root):
    with store.tx() as db:
        if _held(db, mid, TRANSCRIPT):
            raise HeldDuringCleanup()
        versions = [r["id"] for r in db.execute("SELECT id FROM versions WHERE meeting_id = ?",
                                                (mid,))]
        for vid in versions:
            runs = [r["id"] for r in db.execute("SELECT id FROM speaker_runs WHERE "
                                                "version_id = ?", (vid,))]
            for rid in runs:
                db.execute("DELETE FROM segment_speakers WHERE run_id = ?", (rid,))
                db.execute("DELETE FROM speaker_corrections WHERE run_id = ?", (rid,))
                db.execute("DELETE FROM speaker_turns WHERE run_id = ?", (rid,))
            db.execute("DELETE FROM speaker_runs WHERE version_id = ?", (vid,))
            db.execute("DELETE FROM edit_history WHERE segment_id IN "
                       "(SELECT id FROM segments WHERE version_id = ?)", (vid,))
            db.execute("DELETE FROM edits WHERE segment_id IN "
                       "(SELECT id FROM segments WHERE version_id = ?)", (vid,))
            db.execute("DELETE FROM segments WHERE version_id = ?", (vid,))
            db.execute("DELETE FROM attempts WHERE version_id = ?", (vid,))
            db.execute("DELETE FROM chunks WHERE version_id = ?", (vid,))
        db.execute("DELETE FROM versions WHERE meeting_id = ?", (mid,))
        db.execute("DELETE FROM reviewed_snapshots WHERE meeting_id = ?", (mid,))
        db.execute("DELETE FROM handoff_options WHERE meeting_id = ?", (mid,))
        db.execute("DELETE FROM sources WHERE meeting_id = ?", (mid,))
        db.execute("DELETE FROM holds WHERE meeting_id = ? AND released_at IS NOT NULL", (mid,))
        db.execute("DELETE FROM meetings WHERE id = ?", (mid,))
    compact(store)
    folder = media_root / mid                 # anything left (clips, exports)
    if folder.exists():
        shutil.rmtree(folder)                 # a failure is retried; the rows are gone
    return None


def compact(store):
    """Leave no removed text readable in the database files: secure_delete
    overwrites freed pages; checkpoint the WAL into the main file and
    truncate it; VACUUM rebuilds the file without free pages."""
    with store._lock:
        try:
            store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            store.db.execute("VACUUM")
            busy = store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        except sqlite3.Error as exc:
            raise StoreError(f"Could not compact the database: {exc}") from exc
    if busy:
        raise StoreError("Could not compact the database: it is in use. Will retry.")
