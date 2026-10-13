# workspace/store.py
# SQLite state for the meeting workspace (#14). One state-owning process opens
# this; workers never write to it. See docs/WORKSPACE_DESIGN.md.
#
# Atomic boundaries:
#   - commit_chunk(): a chunk's segments and its "done" status in ONE
#     transaction, so a crash leaves either all of it or none of it.
#   - edits are written only by save_edit(); nothing that writes generated
#     segments touches them.

import hashlib
import json
import os
import sqlite3
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
    last_error    TEXT
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
    created_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    id             TEXT PRIMARY KEY,
    meeting_id     TEXT NOT NULL REFERENCES meetings(id),
    source_id      TEXT NOT NULL REFERENCES sources(id),
    engine         TEXT NOT NULL,
    model          TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    precision      TEXT NOT NULL,
    config_json    TEXT NOT NULL,
    config_hash    TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'active',  -- active | superseded
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    version_id   TEXT NOT NULL REFERENCES versions(id),
    idx          INTEGER NOT NULL,
    start_sample INTEGER NOT NULL,   -- owned range, samples
    end_sample   INTEGER NOT NULL,
    ctx_start    INTEGER NOT NULL,   -- decode range, samples
    ctx_end      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | done
    attempts     INTEGER NOT NULL DEFAULT 0,
    backend      TEXT,
    precision    TEXT,
    committed_at REAL,
    PRIMARY KEY (version_id, idx)
);

CREATE TABLE IF NOT EXISTS segments (
    id         INTEGER PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES versions(id),
    chunk_idx  INTEGER NOT NULL,
    start_s    REAL NOT NULL,
    end_s      REAL NOT NULL,
    text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS segments_by_time ON segments(version_id, start_s);

CREATE TABLE IF NOT EXISTS edits (
    segment_id INTEGER PRIMARY KEY REFERENCES segments(id),
    text       TEXT NOT NULL,
    edited_at  REAL NOT NULL
);

-- Reserved for later issues (see docs/WORKSPACE_DESIGN.md).
CREATE TABLE IF NOT EXISTS speaker_runs (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES versions(id),
    status TEXT NOT NULL, hint_count INTEGER, started_at REAL, finished_at REAL, error TEXT);
CREATE TABLE IF NOT EXISTS speaker_turns (
    run_id TEXT NOT NULL REFERENCES speaker_runs(id), start_s REAL NOT NULL,
    end_s REAL NOT NULL, label TEXT NOT NULL, overlap INTEGER NOT NULL DEFAULT 0);
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


class StoreError(RuntimeError):
    """A database operation failed (e.g. disk full). Nothing partial was saved."""


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
        # isolation_level=None: we issue BEGIN/COMMIT ourselves (explicit transactions).
        self.db = sqlite3.connect(str(self.path), isolation_level=None, timeout=30,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            self.db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))

    def close(self):
        self.db.close()

    @contextmanager
    def tx(self):
        """One atomic transaction; any database error rolls everything back."""
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

    # -- meetings and sources ----------------------------------------------------

    def create_meeting(self, title):
        mid, now = new_id(), time.time()
        with self.tx() as db:
            db.execute("INSERT INTO meetings (id, title, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?)", (mid, title, now, now))
        return mid

    def meeting(self, mid):
        return self.db.execute("SELECT * FROM meetings WHERE id = ?", (mid,)).fetchone()

    def add_source(self, mid, *, original_path, original_sha256, original_size,
                   original_mtime_ns, audio_sha256, samples, sample_rate):
        sid = new_id()
        with self.tx() as db:
            db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (sid, mid, original_path, original_sha256, original_size,
                        original_mtime_ns, audio_sha256, samples, sample_rate, time.time()))
        return sid

    def source(self, sid):
        return self.db.execute("SELECT * FROM sources WHERE id = ?", (sid,)).fetchone()

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
        superseding any other active version) if anything differs. chunk_plan is
        a list of (start, end, ctx_start, ctx_end) in samples."""
        chash = config_hash(config)
        row = self.db.execute(
            "SELECT id FROM versions WHERE meeting_id = ? AND state = 'active' AND "
            "source_id = ? AND engine = ? AND model = ? AND model_revision = ? AND "
            "precision = ? AND config_hash = ?",
            (mid, sid, engine, model, model_revision, precision, chash)).fetchone()
        if row:
            return row["id"]
        vid = new_id()
        with self.tx() as db:
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
        return self.db.execute("SELECT * FROM versions WHERE id = ?", (vid,)).fetchone()

    def chunks(self, vid, status=None):
        q = "SELECT * FROM chunks WHERE version_id = ?"
        args = [vid]
        if status:
            q += " AND status = ?"
            args.append(status)
        return self.db.execute(q + " ORDER BY idx", args).fetchall()

    def progress(self, vid):
        """(committed samples, total samples) from committed chunks only."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(CASE WHEN status = 'done' THEN end_sample - start_sample END), 0)"
            " AS done, COALESCE(SUM(end_sample - start_sample), 0) AS total "
            "FROM chunks WHERE version_id = ?",
            (vid,)).fetchone()
        return row["done"], row["total"]

    def note_attempt(self, vid, idx, backend, precision):
        with self.tx() as db:
            db.execute("UPDATE chunks SET attempts = attempts + 1, backend = ?, precision = ? "
                       "WHERE version_id = ? AND idx = ?", (backend, precision, vid, idx))

    def commit_chunk(self, vid, idx, segments):
        """Atomically store a chunk's segments and mark it done. Refuses to
        commit a chunk twice (a stale or repeated result is dropped)."""
        with self.tx() as db:
            row = db.execute("SELECT status FROM chunks WHERE version_id = ? AND idx = ?",
                             (vid, idx)).fetchone()
            if row is None or row["status"] == "done":
                return False
            db.executemany("INSERT INTO segments (version_id, chunk_idx, start_s, end_s, text) "
                           "VALUES (?,?,?,?,?)",
                           [(vid, idx, s.start, s.end, s.text) for s in segments])
            db.execute("UPDATE chunks SET status = 'done', committed_at = ? "
                       "WHERE version_id = ? AND idx = ?", (time.time(), vid, idx))
        return True

    # -- transcript and edits ----------------------------------------------------

    def transcript(self, vid):
        """Segments in time order, with any human edit applied on top."""
        return self.db.execute(
            "SELECT s.id, s.start_s, s.end_s, COALESCE(e.text, s.text) AS text, "
            "s.text AS generated, e.text IS NOT NULL AS edited "
            "FROM segments s LEFT JOIN edits e ON e.segment_id = s.id "
            "WHERE s.version_id = ? ORDER BY s.start_s, s.id", (vid,)).fetchall()

    def save_edit(self, segment_id, text):
        with self.tx() as db:
            db.execute("INSERT INTO edits (segment_id, text, edited_at) VALUES (?,?,?) "
                       "ON CONFLICT(segment_id) DO UPDATE SET text = excluded.text, "
                       "edited_at = excluded.edited_at", (segment_id, text, time.time()))
