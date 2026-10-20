# The schema-1 workspace DDL, frozen from workspace/store.py at 46be2e1 (the
# commit C2b-2 was written on, #111), for building real schema-1 databases to
# migrate in tests. Never edit it to follow the current schema: it is what an
# older build wrote. Synthetic data only.

import sqlite3

SCHEMA_1 = """
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
    delete_original  INTEGER NOT NULL DEFAULT 0,  -- explicit authority to delete the original (#24)
    intake_folder    TEXT     -- watched folder it came from (#24); NULL for imports by hand
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
    status TEXT NOT NULL,      -- running | done | failed | interrupted | superseded | skipped
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
-- (keyed path hashes, content hashes); no meeting content. A folder's path can
-- name a client, so it is kept through the codec like content (CONTENT_COLUMNS)
-- but counts as configuration: it is carried over (CARRY_OVER) and never
-- blocks an update (update.NON_CONTENT).
CREATE TABLE IF NOT EXISTS watched_folders (
    id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL, last_scan_at REAL, last_error TEXT,
    baseline_done INTEGER NOT NULL DEFAULT 0, include_existing INTEGER NOT NULL DEFAULT 0,
    delete_originals INTEGER NOT NULL DEFAULT 0,   -- #24: explicit, separate from import
    fetch_cloud INTEGER NOT NULL DEFAULT 0);       -- #24: read cloud-only files to fetch them
CREATE TABLE IF NOT EXISTS intake_seen (
    folder_id TEXT NOT NULL, key TEXT NOT NULL, size INTEGER, mtime_ns INTEGER,
    file_id INTEGER, state TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    content TEXT,              -- SHA-256 only while an import is in progress
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


def create(path):
    """A schema-1 workspace database at path (as a schema-1 build made it),
    open for the caller to fill. Returns the connection."""
    db = sqlite3.connect(str(path), isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA_1)
    db.execute("INSERT INTO schema_version VALUES (1)")
    return db
