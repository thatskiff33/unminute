# workspace/store.py
# SQLite state for the meeting workspace (#14). One state-owning process opens
# this; workers never write to it. See docs/WORKSPACE_DESIGN.md.
#
# Atomic boundaries:
#   - commit_chunk(): a chunk's segments and its "done" status in ONE
#     transaction, so a crash leaves either all of it or none of it. It also
#     refuses results for a superseded version or a stale content epoch, so a
#     late result can never recreate content after a change or cleanup (#17).
#   - edits are written only by save_edit(); nothing that writes generated
#     segments touches them.
# Threads: the UI bridge and the processing thread share this object, so
# every operation holds one lock (a single connection cannot nest
# transactions, and readers must not see another thread's uncommitted writes).

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS meetings (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    phase         TEXT NOT NULL DEFAULT 'new',
    paused        INTEGER NOT NULL DEFAULT 0,      -- explicit user pause, survives restart
    content_epoch INTEGER NOT NULL DEFAULT 0,      -- bumped by cleanup (#17); drops stale results
    last_error    TEXT,
    reviewed_at   REAL,      -- explicit "transcript reviewed" confirmation (#17)
    final_saved_at REAL,     -- explicit "final deliverable reviewed and saved" confirmation
    audio_removed_at REAL    -- app audio cleaned up; playback/retranscription unavailable
);

-- The reviewed transcript frozen at confirmation, before any audio is removed.
CREATE TABLE IF NOT EXISTS reviewed_snapshots (
    meeting_id TEXT PRIMARY KEY REFERENCES meetings(id),
    text       TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id               TEXT PRIMARY KEY,
    meeting_id       TEXT NOT NULL REFERENCES meetings(id),
    original_path    TEXT,                          -- only for #24 deletion authority
    original_sha256  TEXT NOT NULL,
    original_size    INTEGER NOT NULL,
    original_mtime_ns INTEGER NOT NULL,
    audio_sha256     TEXT NOT NULL,                 -- decoded 16 kHz mono float32
    samples          INTEGER NOT NULL,
    sample_rate      INTEGER NOT NULL,
    created_at       REAL NOT NULL,
    delete_original  INTEGER NOT NULL DEFAULT 0   -- explicit authority to delete the original (#24)
);

CREATE TABLE IF NOT EXISTS versions (
    id             TEXT PRIMARY KEY,
    meeting_id     TEXT NOT NULL REFERENCES meetings(id),
    source_id      TEXT NOT NULL REFERENCES sources(id),
    engine         TEXT NOT NULL,
    model          TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    precision      TEXT NOT NULL,   -- the requested output contract; actual per attempt below
    config_json    TEXT NOT NULL,
    config_hash    TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'active',  -- active | superseded
    created_at     REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_version
    ON versions(meeting_id) WHERE state = 'active';

CREATE TABLE IF NOT EXISTS chunks (
    version_id   TEXT NOT NULL REFERENCES versions(id),
    idx          INTEGER NOT NULL,
    start_sample INTEGER NOT NULL,   -- owned range, samples
    end_sample   INTEGER NOT NULL,
    ctx_start    INTEGER NOT NULL,   -- decode range, samples
    ctx_end      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | done
    failures     INTEGER NOT NULL DEFAULT 0,
    committed_at REAL,
    PRIMARY KEY (version_id, idx)
);

-- One row per attempt, with the backend/precision the engine actually used.
CREATE TABLE IF NOT EXISTS attempts (
    id         INTEGER PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES versions(id),
    chunk_idx  INTEGER NOT NULL,
    started_at REAL NOT NULL,
    backend    TEXT,
    precision  TEXT,
    result     TEXT NOT NULL DEFAULT 'running'
    -- running | committed | failed | stopped | interrupted | unavailable (model not ready)
);
CREATE INDEX IF NOT EXISTS attempts_by_chunk ON attempts(version_id, chunk_idx);

CREATE TABLE IF NOT EXISTS segments (
    id         INTEGER PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES versions(id),
    chunk_idx  INTEGER NOT NULL,
    start_s    REAL NOT NULL,
    end_s      REAL NOT NULL,
    text       TEXT NOT NULL,
    words_json TEXT              -- [[start, end, text], ...] when the engine provides them
);
CREATE INDEX IF NOT EXISTS segments_by_time ON segments(version_id, start_s);

CREATE TABLE IF NOT EXISTS edits (
    segment_id INTEGER PRIMARY KEY REFERENCES segments(id),
    text       TEXT NOT NULL,
    edited_at  REAL NOT NULL
);
-- Undo history for edits (#20): each change records the text it replaced
-- (NULL = the generated text). Removed with the transcript at cleanup.
CREATE TABLE IF NOT EXISTS edit_history (
    id         INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL REFERENCES segments(id),
    before     TEXT,
    after      TEXT,
    at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS edit_history_by_segment ON edit_history(segment_id);

-- Reserved for later issues (see docs/WORKSPACE_DESIGN.md).
-- Speaker separation (#16): a whole-meeting pass after transcription. Machine
-- attribution lives here; user corrections (#21) are stored separately.
CREATE TABLE IF NOT EXISTS speaker_runs (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES versions(id),
    status TEXT NOT NULL,      -- running | done | failed | interrupted | superseded
    hint_count INTEGER, started_at REAL, finished_at REAL, error TEXT,
    backend TEXT, precision TEXT,
    epoch INTEGER);            -- meeting content epoch at start; a stale finish is refused
-- exclusive = 0: pyannote's full turns, including overlapped speech (evidence)
-- exclusive = 1: one speaker at a time, used for attribution
CREATE TABLE IF NOT EXISTS speaker_turns (
    run_id TEXT NOT NULL REFERENCES speaker_runs(id), start_s REAL NOT NULL,
    end_s REAL NOT NULL, label TEXT NOT NULL, exclusive INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS segment_speakers (
    run_id TEXT NOT NULL REFERENCES speaker_runs(id),
    segment_id INTEGER NOT NULL REFERENCES segments(id),
    label TEXT,                -- NULL when no speaker overlaps the segment
    overlap INTEGER NOT NULL DEFAULT 0,   -- 1 if more than one person spoke during it
    PRIMARY KEY (run_id, segment_id));
-- Speaker naming and corrections (#21): an append-only log per speaker run
-- (kind: name | merge | assign). Meeting-only; removed with the transcript.
CREATE TABLE IF NOT EXISTS speaker_corrections (
    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES speaker_runs(id),
    kind TEXT NOT NULL, target TEXT NOT NULL, value TEXT NOT NULL, at REAL NOT NULL);
-- Handoff-only omissions and redactions (#22). Meeting content: removed with
-- the transcript at cleanup.
CREATE TABLE IF NOT EXISTS handoff_options (
    meeting_id TEXT PRIMARY KEY REFERENCES meetings(id),
    omitted TEXT NOT NULL, redactions TEXT NOT NULL);
-- Watched-folder intake (#23): configuration plus bounded suppression data
-- (keyed path hashes, content hashes); no names, no meeting content.
CREATE TABLE IF NOT EXISTS watched_folders (
    id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL, last_scan_at REAL, last_error TEXT,
    baseline_done INTEGER NOT NULL DEFAULT 0, include_existing INTEGER NOT NULL DEFAULT 0,
    delete_originals INTEGER NOT NULL DEFAULT 0,   -- #24: explicit, separate from import
    fetch_cloud INTEGER NOT NULL DEFAULT 0);       -- #24: read cloud-only files to fetch them
CREATE TABLE IF NOT EXISTS intake_seen (
    folder_id TEXT NOT NULL, key TEXT NOT NULL, size INTEGER, mtime_ns INTEGER,
    file_id INTEGER, state TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    PRIMARY KEY (folder_id, key));
CREATE TABLE IF NOT EXISTS intake_content (
    sha256 TEXT PRIMARY KEY, imported_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS intake_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cleanup_jobs (
    id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, phase TEXT NOT NULL,
    state TEXT NOT NULL, intent_at REAL NOT NULL, last_attempt_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS holds (
    id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, scope TEXT NOT NULL,
    reason TEXT NOT NULL, created_at REAL NOT NULL, reviewed_at REAL, released_at REAL);
CREATE TABLE IF NOT EXISTS receipts (
    job_id TEXT PRIMARY KEY, created_at REAL NOT NULL, completed_at REAL, result TEXT NOT NULL);
"""


OVERLAP_MIN_S = 0.3   # two people must talk at the same time this long to count
SPEAKER_RESTART_NOTE = (
    "Speaker identification was interrupted. It works on the whole meeting at once, so it "
    "will start again from the beginning; the transcript is unaffected.")


def attribute(start, end, turns, exclusive_turns):
    """(label, overlap) for one segment: the exclusive-turn speaker covering
    most of it, and whether different people spoke AT THE SAME TIME within it
    (from pyannote's full turns) for at least OVERLAP_MIN_S in total. A plain
    handover from one speaker to the next inside a segment is not overlap."""
    best, best_cover = None, 0.0
    for a, b, lbl in exclusive_turns:
        cover = min(end, b) - max(start, a)
        if cover > best_cover:
            best, best_cover = lbl, cover
    inside = [(max(a, start), min(b, end), lbl) for a, b, lbl in turns
              if min(b, end) > max(a, start)]
    together = 0.0
    for i, (a1, b1, l1) in enumerate(inside):
        for a2, b2, l2 in inside[i + 1:]:
            if l1 != l2:
                together += max(0.0, min(b1, b2) - max(a1, a2))
    return best, 1 if together >= OVERLAP_MIN_S else 0


class StoreError(RuntimeError):
    """A database operation failed (e.g. disk full). Nothing partial was saved."""


class SchemaTooNew(StoreError):
    """The database was written by a newer version of the app."""


def default_root():
    return Path(os.environ.get("LOCALAPPDATA", os.getcwd())) / "whisper" / "workspace"


def new_id():
    return uuid.uuid4().hex


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # isolation_level=None: we issue BEGIN/COMMIT ourselves (explicit transactions).
        self.db = sqlite3.connect(str(self.path), isolation_level=None, timeout=30,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            # Deleted rows are overwritten, not just unlinked (#17 cleanup).
            self.db.execute("PRAGMA secure_delete=ON")
            self.db.executescript(SCHEMA)       # outside any transaction; idempotent
            with self.tx() as db:
                row = db.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
                if row["v"] is None:
                    db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
                elif row["v"] > SCHEMA_VERSION:
                    raise SchemaTooNew(f"This workspace was created by a newer version of "
                                       f"the app (schema {row['v']}).")
                # Attempts still 'running' were cut short by a crash or kill of
                # the previous session (this process owns the state, #15).
                db.execute("UPDATE attempts SET result = 'interrupted' "
                           "WHERE result = 'running'")
                # Same for a speaker pass cut short: it restarts from the start.
                db.execute("UPDATE speaker_runs SET status = 'interrupted', error = ? "
                           "WHERE status = 'running'", (SPEAKER_RESTART_NOTE,))
        except BaseException:
            self.db.close()          # never leave the file locked on a refused open
            raise

    def close(self):
        with self._lock:
            self.db.close()

    @contextmanager
    def tx(self):
        """One atomic transaction; any database error rolls everything back."""
        with self._lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise StoreError(str(exc)) from exc
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException as exc:
                try:
                    self.db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if isinstance(exc, sqlite3.Error):
                    raise StoreError(str(exc)) from exc
                raise

    def _one(self, sql, args=()):
        with self._lock:
            try:
                return self.db.execute(sql, args).fetchone()
            except sqlite3.Error as exc:
                raise StoreError(str(exc)) from exc

    def _all(self, sql, args=()):
        with self._lock:
            try:
                return self.db.execute(sql, args).fetchall()
            except sqlite3.Error as exc:
                raise StoreError(str(exc)) from exc

    # -- meetings and sources ----------------------------------------------------

    def create_meeting(self, title):
        mid, now = new_id(), time.time()
        with self.tx() as db:
            db.execute("INSERT INTO meetings (id, title, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?)", (mid, title, now, now))
        return mid

    def delete_new_meeting(self, mid):
        """Remove a meeting that never got a source (e.g. a failed import)."""
        with self.tx() as db:
            if db.execute("SELECT 1 FROM sources WHERE meeting_id = ?", (mid,)).fetchone():
                raise StoreError("Meeting has content; use cleanup (#17).")
            db.execute("DELETE FROM meetings WHERE id = ?", (mid,))

    def meeting(self, mid):
        return self._one("SELECT * FROM meetings WHERE id = ?", (mid,))

    def add_source(self, mid, *, original_path, original_sha256, original_size,
                   original_mtime_ns, audio_sha256, samples, sample_rate):
        sid = new_id()
        with self.tx() as db:
            db.execute("INSERT INTO sources (id, meeting_id, original_path, original_sha256, "
                       "original_size, original_mtime_ns, audio_sha256, samples, sample_rate, "
                       "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (sid, mid, original_path, original_sha256, original_size,
                        original_mtime_ns, audio_sha256, samples, sample_rate, time.time()))
        return sid

    def source(self, sid):
        return self._one("SELECT * FROM sources WHERE id = ?", (sid,))

    def set_paused(self, mid, paused):
        with self.tx() as db:
            db.execute("UPDATE meetings SET paused = ?, updated_at = ? WHERE id = ?",
                       (1 if paused else 0, time.time(), mid))

    def set_phase(self, mid, phase, error=None):
        with self.tx() as db:
            db.execute("UPDATE meetings SET phase = ?, last_error = ?, updated_at = ? "
                       "WHERE id = ?", (phase, error, time.time(), mid))

    # -- versions and chunks -----------------------------------------------------

    def ensure_version(self, mid, sid, *, engine, model, model_revision, precision,
                       config, chunk_plan):
        """Return the active version for exactly these inputs, creating it (and
        superseding any other active version) if anything differs. The check
        and the creation are one transaction. chunk_plan is a list of
        (start, end, ctx_start, ctx_end) in samples."""
        chash = config_hash(config)
        with self.tx() as db:
            row = db.execute(
                "SELECT id FROM versions WHERE meeting_id = ? AND state = 'active' AND "
                "source_id = ? AND engine = ? AND model = ? AND model_revision = ? AND "
                "precision = ? AND config_hash = ?",
                (mid, sid, engine, model, model_revision, precision, chash)).fetchone()
            if row:
                return row["id"]
            vid = new_id()
            db.execute("UPDATE versions SET state = 'superseded' "
                       "WHERE meeting_id = ? AND state = 'active'", (mid,))
            db.execute("INSERT INTO versions (id, meeting_id, source_id, engine, model, "
                       "model_revision, precision, config_json, config_hash, created_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (vid, mid, sid, engine, model, model_revision, precision,
                        json.dumps(config, sort_keys=True), chash, time.time()))
            db.executemany("INSERT INTO chunks (version_id, idx, start_sample, end_sample, "
                           "ctx_start, ctx_end) VALUES (?,?,?,?,?,?)",
                           [(vid, i, *c) for i, c in enumerate(chunk_plan)])
        return vid

    def version(self, vid):
        return self._one("SELECT * FROM versions WHERE id = ?", (vid,))

    def chunks(self, vid, status=None):
        q, args = "SELECT * FROM chunks WHERE version_id = ?", [vid]
        if status:
            q += " AND status = ?"
            args.append(status)
        return self._all(q + " ORDER BY idx", args)

    def progress(self, vid):
        """(committed samples, total samples) from committed chunks only."""
        row = self._one(
            "SELECT COALESCE(SUM(CASE WHEN status = 'done' THEN end_sample - start_sample END), 0)"
            " AS done, COALESCE(SUM(end_sample - start_sample), 0) AS total "
            "FROM chunks WHERE version_id = ?", (vid,))
        return row["done"], row["total"]

    def start_attempt(self, vid, idx):
        with self.tx() as db:
            cur = db.execute("INSERT INTO attempts (version_id, chunk_idx, started_at) "
                             "VALUES (?,?,?)", (vid, idx, time.time()))
            return cur.lastrowid

    def finish_attempt(self, attempt_id, result, backend=None, precision=None):
        """Record how an attempt ended and the backend/precision actually used.
        A failure also counts against the chunk; pauses/stops do not."""
        with self.tx() as db:
            db.execute("UPDATE attempts SET result = ?, backend = COALESCE(?, backend), "
                       "precision = COALESCE(?, precision) WHERE id = ?",
                       (result, backend, precision, attempt_id))
            if result == "failed":
                db.execute("UPDATE chunks SET failures = failures + 1 WHERE (version_id, idx) = "
                           "(SELECT version_id, chunk_idx FROM attempts WHERE id = ?)",
                           (attempt_id,))

    def reset_failures(self, vid):
        """The user's explicit retry: pending chunks may be attempted again."""
        with self.tx() as db:
            db.execute("UPDATE chunks SET failures = 0 WHERE version_id = ? AND "
                       "status = 'pending'", (vid,))

    def attempts(self, vid, idx):
        return self._all("SELECT * FROM attempts WHERE version_id = ? AND chunk_idx = ? "
                         "ORDER BY id", (vid, idx))

    def commit_chunk(self, vid, idx, segments, *, epoch=None, attempt_id=None,
                     backend=None, precision=None):
        """Atomically store a chunk's segments and mark it done. Refused (returns
        False, nothing written) if the chunk is already done, the version is no
        longer active, or `epoch` no longer matches the meeting's content epoch."""
        with self.tx() as db:
            row = db.execute(
                "SELECT c.status, v.state, m.content_epoch FROM chunks c "
                "JOIN versions v ON v.id = c.version_id JOIN meetings m ON m.id = v.meeting_id "
                "WHERE c.version_id = ? AND c.idx = ?", (vid, idx)).fetchone()
            if (row is None or row["status"] == "done" or row["state"] != "active"
                    or (epoch is not None and epoch != row["content_epoch"])):
                return False
            db.executemany(
                "INSERT INTO segments (version_id, chunk_idx, start_s, end_s, text, words_json) "
                "VALUES (?,?,?,?,?,?)",
                [(vid, idx, s.start, s.end, s.text,
                  json.dumps([list(w) for w in s.words]) if getattr(s, "words", ()) else None)
                 for s in segments])
            db.execute("UPDATE chunks SET status = 'done', committed_at = ? "
                       "WHERE version_id = ? AND idx = ?", (time.time(), vid, idx))
            if attempt_id is not None:
                db.execute("UPDATE attempts SET result = 'committed', backend = ?, precision = ? "
                           "WHERE id = ?", (backend, precision, attempt_id))
        return True

    # -- transcript and edits ----------------------------------------------------

    def transcript(self, vid):
        """Segments in time order, with any human edit applied on top."""
        return self._all(
            "SELECT s.id, s.start_s, s.end_s, COALESCE(e.text, s.text) AS text, "
            "s.text AS generated, e.text IS NOT NULL AS edited, s.words_json "
            "FROM segments s LEFT JOIN edits e ON e.segment_id = s.id "
            "WHERE s.version_id = ? ORDER BY s.start_s, s.id", (vid,))

    # -- speaker separation (#16) -------------------------------------------------

    def start_speaker_run(self, vid, hint_count=None):
        """Begin a whole-meeting speaker pass. Earlier runs for this version are
        superseded; transcription is never touched."""
        rid = new_id()
        with self.tx() as db:
            # Only a pass still running is superseded now; the previous good
            # labels stay current until this pass actually succeeds.
            db.execute("UPDATE speaker_runs SET status = 'superseded' "
                       "WHERE version_id = ? AND status = 'running'", (vid,))
            epoch = db.execute("SELECT m.content_epoch FROM versions v JOIN meetings m "
                               "ON m.id = v.meeting_id WHERE v.id = ?", (vid,)).fetchone()
            db.execute("INSERT INTO speaker_runs (id, version_id, status, hint_count, started_at,"
                       " epoch) VALUES (?, ?, 'running', ?, ?, ?)",
                       (rid, vid, hint_count, time.time(), epoch[0] if epoch else None))
        return rid

    def finish_speaker_run(self, rid, turns, exclusive_turns, backend=None, precision=None):
        """Atomically store turns and per-segment attribution, mark the run done
        and supersede earlier completed runs. Refused (False, nothing written)
        if the run is no longer 'running', its version is no longer active or
        the meeting's content changed meanwhile (cleanup, #17)."""
        run = self._one("SELECT * FROM speaker_runs WHERE id = ?", (rid,))
        if run is None or run["status"] != "running":
            return False
        # Attribution is computed outside the write lock (long meetings).
        segs = self._all("SELECT id, start_s, end_s FROM segments WHERE version_id = ?",
                         (run["version_id"],))
        rows = [(rid, s["id"], *attribute(s["start_s"], s["end_s"], turns, exclusive_turns))
                for s in segs]
        with self.tx() as db:
            now = db.execute(
                "SELECT r.status, v.state, m.content_epoch FROM speaker_runs r "
                "JOIN versions v ON v.id = r.version_id JOIN meetings m ON m.id = v.meeting_id "
                "WHERE r.id = ?", (rid,)).fetchone()
            if (now is None or now["status"] != "running" or now["state"] != "active"
                    or now["content_epoch"] != run["epoch"]):
                return False
            db.executemany("INSERT INTO speaker_turns VALUES (?,?,?,?,?)",
                           [(rid, a, b, lbl, 0) for a, b, lbl in turns]
                           + [(rid, a, b, lbl, 1) for a, b, lbl in exclusive_turns])
            db.executemany("INSERT INTO segment_speakers VALUES (?,?,?,?)", rows)
            db.execute("UPDATE speaker_runs SET status = 'superseded' WHERE version_id = ? "
                       "AND status = 'done'", (run["version_id"],))
            db.execute("UPDATE speaker_runs SET status = 'done', finished_at = ?, backend = ?, "
                       "precision = ? WHERE id = ?", (time.time(), backend, precision, rid))
        return True

    def end_speaker_run(self, rid, status, error=None):
        """Mark a run failed or interrupted. Its partial data was never written."""
        with self.tx() as db:
            db.execute("UPDATE speaker_runs SET status = ?, error = ?, finished_at = ? "
                       "WHERE id = ? AND status = 'running'", (status, error, time.time(), rid))

    def speaker_runs(self, vid):
        return self._all("SELECT * FROM speaker_runs WHERE version_id = ? ORDER BY started_at",
                         (vid,))

    def current_speakers(self, vid):
        """{segment_id: (label, overlap)} from the latest completed run, or {}."""
        run = self._one("SELECT id FROM speaker_runs WHERE version_id = ? AND status = 'done' "
                        "ORDER BY started_at DESC LIMIT 1", (vid,))
        if run is None:
            return {}
        return {r["segment_id"]: (r["label"], bool(r["overlap"])) for r in self._all(
            "SELECT * FROM segment_speakers WHERE run_id = ?", (run["id"],))}

    def save_edit(self, segment_id, text):
        """Store a human edit on top of the generated text, recording what it
        replaced so it can be undone. Saving the generated text again removes
        the edit. Returns False if nothing changed."""
        with self.tx() as db:
            seg = db.execute("SELECT s.text AS generated, e.text AS edited FROM segments s "
                             "LEFT JOIN edits e ON e.segment_id = s.id WHERE s.id = ?",
                             (segment_id,)).fetchone()
            if seg is None:
                raise StoreError("That passage no longer exists.")
            current = seg["edited"]
            new = None if text == seg["generated"] else text
            if new == current:
                return False
            self._set_edit(db, segment_id, new)
            db.execute("INSERT INTO edit_history (segment_id, before, after, at) "
                       "VALUES (?,?,?,?)", (segment_id, current, new, time.time()))
        return True

    @staticmethod
    def _set_edit(db, segment_id, text):
        if text is None:
            db.execute("DELETE FROM edits WHERE segment_id = ?", (segment_id,))
        else:
            db.execute("INSERT INTO edits (segment_id, text, edited_at) VALUES (?,?,?) "
                       "ON CONFLICT(segment_id) DO UPDATE SET text = excluded.text, "
                       "edited_at = excluded.edited_at", (segment_id, text, time.time()))

    def undo_edit(self, vid):
        """Undo the most recent edit in this version. Returns the segment id,
        or None when there is nothing to undo. Survives restart."""
        with self.tx() as db:
            last = db.execute("SELECT h.* FROM edit_history h JOIN segments s "
                              "ON s.id = h.segment_id WHERE s.version_id = ? "
                              "ORDER BY h.id DESC LIMIT 1", (vid,)).fetchone()
            if last is None:
                return None
            self._set_edit(db, last["segment_id"], last["before"])
            db.execute("DELETE FROM edit_history WHERE id = ?", (last["id"],))
            return last["segment_id"]

    def edit_count(self, vid):
        return self._one("SELECT COUNT(*) AS n FROM edits e JOIN segments s "
                         "ON s.id = e.segment_id WHERE s.version_id = ?", (vid,))["n"]

    def can_undo(self, vid):
        return self._one("SELECT 1 FROM edit_history h JOIN segments s ON s.id = h.segment_id "
                         "WHERE s.version_id = ? LIMIT 1", (vid,)) is not None

    def search(self, vid, query, limit=500):
        """Positions (0-based, transcript order) of passages containing query,
        case-insensitive, in the edited text where there is an edit."""
        needle = query.casefold()
        return [i for i, r in enumerate(self.transcript(vid))
                if needle in r["text"].casefold()][:limit]

    def active_version(self, mid):
        return self._one("SELECT * FROM versions WHERE meeting_id = ? AND state = 'active'",
                         (mid,))

    def versions(self, mid):
        return self._all("SELECT * FROM versions WHERE meeting_id = ? ORDER BY created_at",
                         (mid,))
