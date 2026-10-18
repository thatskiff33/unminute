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
import re
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


# Columns that hold meeting content, and watched-folder paths, which can name a
# client (#111 C2; docs/ENCRYPTION.md). Only this module names them in SQL, and
# every value read or written goes through the Store's codec, so C2b can seal
# them in one place. tests/test_content_access.py checks the SQL in workspace/
# and tools/ (docs/WORKSPACE_DESIGN.md, "Content columns" says what it sees).
CONTENT_COLUMNS = {
    "meetings": ("title", "last_error"),
    "reviewed_snapshots": ("text",),
    "sources": ("original_path",),
    "segments": ("text", "words_json"),
    "edits": ("text",),
    "edit_history": ("before", "after"),
    "speaker_runs": ("error",),
    "speaker_corrections": ("value",),
    "handoff_options": ("omitted", "redactions"),
    "holds": ("reason",),
    "watched_folders": ("path",),
}


_CONTENT_NAMES = frozenset(c for cols in CONTENT_COLUMNS.values() for c in cols)
_NOT_IN_A_CONDITION = re.compile(r"['\"`\[;*]|--|/\*|\bselect\b|\bin\b(?!\s*\()", re.I)


def plain_condition(where):
    """A where= condition for Store.meetings(), holds() or folders(): a fixed
    SQL expression on plain columns of the one table, with values passed as
    arguments. Refused (ValueError) if it has a quote, a comment, a second
    statement, a subquery, *, an IN not followed by "(" (SQLite reads
    "x IN holds" or "x IN json_each(?)" as a subquery), or
    names a content column, so no content is matched or read outside the codec
    (a subquery could compare a row of content columns without naming them)."""
    if (not isinstance(where, str) or _NOT_IN_A_CONDITION.search(where)
            or _CONTENT_NAMES & set(re.findall(r"\w+", where.lower()))):
        raise ValueError("A condition may name plain columns only, with values as arguments.")
    return where


class PlainCodec:
    """Content stored as it is (schema 1). C2b replaces it with a codec that
    seals each value under its meeting's text key (the workspace key when
    meeting is None), bound to its table, column and row."""

    def seal(self, meeting, table, column, row, value):
        return value

    def unseal(self, meeting, table, column, row, value):
        return value


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


# Not meeting content: kept across an empty-workspace migration (#26). Rows are
# copied as stored, so a migration that changes how the codec keeps a value
# (watched_folders.path in C2b) must convert it on the way.
CARRY_OVER = ("receipts", "watched_folders", "intake_content", "intake_meta",
              "intake_seen")


def _statements(script):
    """Split a schema script into single statements (for use in a transaction)."""
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        if line.lstrip().startswith("--") and not buf.strip():
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    return out


class MigrationBlocked(StoreError):
    """An older-schema workspace still holds content: it is never migrated in
    place (#26). The installer's gate normally prevents this ever happening."""


def local_appdata():
    """%LOCALAPPDATA%, which must be an absolute local path outside OneDrive.
    Meetings must never end up in the current folder, a synced folder or a
    network location because the variable is missing or redirected: refuse
    instead of guessing."""
    base = os.environ.get("LOCALAPPDATA", "")
    path = Path(base)
    missing = StoreError("Unminute cannot find this account's local app data folder "
                         "(LOCALAPPDATA). Ask your IT team.")
    if not base or not path.is_absolute() or base.startswith(("\\\\", "//")):
        raise missing
    # Compare real paths: short (8.3) names and junctions must not hide that
    # the folder is inside OneDrive or on a network share.
    real = _real(base)
    if _network_path(real) or _remote_drive(real):
        raise StoreError("This account's local app data folder is on a network drive. "
                         "Unminute keeps meetings on this computer. Ask your IT team.")
    resolved = _folder_key(real)
    profile = os.environ.get("USERPROFILE", "")
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        synced = os.environ.get(var, "")
        # A rooted path without a drive ("\Users\a\OneDrive") still counts:
        # Python 3.13 stopped calling it absolute, and realpath puts it on the
        # current drive.
        if not synced or not (os.path.isabs(synced) or synced.startswith(("\\", "/"))):
            continue
        synced_key = _folder_key(_real(synced))
        # A variable pointing at a drive root, or at or above the user's
        # profile, is a misconfiguration, not a synced AppData: ignore it
        # rather than refuse every normal setup.
        if os.path.dirname(synced_key.rstrip("\\/")) == synced_key.rstrip("\\/") or (
                profile and _folder_key(_real(profile)).startswith(synced_key)):
            continue
        if resolved.startswith(synced_key):
            raise StoreError("This account's local app data folder is inside OneDrive. "
                             "Unminute keeps meetings out of synced folders. Ask your IT team.")
    return path


def _real(path):
    try:
        return os.path.realpath(path)
    except OSError:              # an error realpath does not handle itself
        return os.path.abspath(path)


def _network_path(path):
    """\\\\server\\share and \\\\?\\UNC\\ paths; not local device or volume
    paths such as \\\\?\\Volume{...} (virtual desktop profile disks)."""
    low = path.replace("/", "\\").lower()
    if low.startswith("\\\\?\\unc\\") or low.startswith("\\\\.\\unc\\"):
        return True
    if low.startswith("\\\\?\\") or low.startswith("\\\\.\\"):
        return False
    return low.startswith("\\\\")


def _folder_key(path):
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/") + os.sep


def _remote_drive(path):
    """True if the path's drive letter is a mapped network drive (Windows)."""
    if os.name != "nt":
        return False
    import ctypes
    import ntpath
    root = ntpath.splitdrive(path)[0] + "\\"
    return ctypes.WinDLL("kernel32").GetDriveTypeW(ctypes.c_wchar_p(root)) == 4  # REMOTE


def default_root():
    return local_appdata() / "whisper" / "workspace"


def new_id():
    return uuid.uuid4().hex


def _skip_count(db, vid):
    return db.execute("SELECT COUNT(*) FROM speaker_runs WHERE version_id = ? AND "
                      "status = 'skipped'", (vid,)).fetchone()[0]


def _next_run_time(db, vid):
    """A start time later than every earlier run of the version. The latest
    run is the meeting's speaker state, and time.time() can return the same
    value twice (about 16 ms steps on Windows), as a skip racing a start can."""
    last = db.execute("SELECT MAX(started_at) FROM speaker_runs WHERE version_id = ?",
                      (vid,)).fetchone()[0]
    now = time.time()
    return now if last is None or now > last else last + 1e-6


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


# SQLite result codes that mean the file is damaged (the primary code, the
# low byte of sqlite_errorcode): CORRUPT and NOTADB; IOERR too, if it happens
# again a moment later (another program can lock part of the file briefly).
# Others (busy, out of memory) say nothing about damage.
DAMAGE_CODES = {11, 26}
IOERR = 10


def quick_check_file(path, retry_s=1.0):
    """SQLite's quick check (R11) on its own read-only connection, before the
    workspace opens and before anything writes to it: True if sound, False if
    damaged, None if it could not be checked. Its findings name pages and
    tables, never content, and are not kept."""
    result = _quick_check(path)
    if result == "io":
        time.sleep(retry_s)
        result = _quick_check(path)
    return False if result == "io" else result


def _quick_check(path):
    try:
        con = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        return _damaged(exc)
    try:
        rows = con.execute("PRAGMA quick_check(1)").fetchall()
    except sqlite3.Error as exc:
        return _damaged(exc)
    finally:
        con.close()
    return len(rows) == 1 and rows[0][0] == "ok"


def _damaged(exc):
    code = getattr(exc, "sqlite_errorcode", None)
    primary = code & 0xFF if isinstance(code, int) else None
    if primary == IOERR:
        return "io"
    return False if primary in DAMAGE_CODES else None


class Store:
    def __init__(self, path, housekeeping=True, codec=None):
        """housekeeping False (R11): open a database the check found damaged
        without marking interrupted work; mark_interrupted() runs later.
        codec: how content columns are stored (PlainCodec unless given)."""
        self.codec = codec or PlainCodec()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # isolation_level=None: we issue BEGIN/COMMIT ourselves (explicit transactions).
        self.db = sqlite3.connect(str(self.path), isolation_level=None, timeout=30,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # Committed transactions so far: the page asks for the meeting lists
        # again only when this (or the app's own state) has changed (R7b).
        self.revision = 0
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            # Deleted rows are overwritten, not just unlinked (#17 cleanup).
            self.db.execute("PRAGMA secure_delete=ON")
            # Temporary tables and indices (VACUUM builds a copy of the whole
            # database) stay in memory, never in %TEMP% outside the workspace.
            self.db.execute("PRAGMA temp_store=MEMORY")
            self._migrate_if_needed(housekeeping)
            self.db.executescript(SCHEMA)       # outside any transaction; idempotent
            with self.tx() as db:
                row = db.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
                if row["v"] is None:
                    db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
                elif row["v"] > SCHEMA_VERSION:
                    raise SchemaTooNew(f"This workspace was created by a newer version of "
                                       f"the app (schema {row['v']}).")
            if housekeeping:
                self.mark_interrupted()
        except BaseException:
            self.db.close()          # never leave the file locked on a refused open
            raise

    def _migrate_if_needed(self, housekeeping=True):
        """Opening a workspace written with an older schema (#26): migrate only
        if it holds no meeting content, no active hold and no unfinished
        cleanup. Migration rebuilds the tables; receipts and watched-folder
        configuration (CARRY_OVER) are carried over. No backup is made. Never
        while the database check holds work (housekeeping False, R11): damaged
        tables can read as empty, and rebuilding would drop what is left."""
        db = self.db
        has_version = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                                 "name = 'schema_version'").fetchone()
        if not has_version:
            return
        current = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if current is None or current >= SCHEMA_VERSION:
            return                              # same schema, or "too new" (checked later)
        if not housekeeping:
            raise MigrationBlocked("Unminute found damage in its workspace database, so it "
                                   "will not update the workspace's storage.")
        from workspace import update
        found = update.blockers(self.path)
        if found:
            raise MigrationBlocked(" ".join(b["message"] for b in found))
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        # Configuration and bookkeeping that is not meeting content is carried
        # over, column by column where the new schema still has the column.
        carried = {}
        for t in CARRY_OVER:
            if t in tables:
                cols = [r[1] for r in db.execute(f'PRAGMA table_info("{t}")')]
                carried[t] = (cols, db.execute(f'SELECT * FROM "{t}"').fetchall())
        # One transaction: SQLite DDL is transactional, so a crash part-way
        # leaves the old workspace exactly as it was.
        db.execute("BEGIN IMMEDIATE")
        try:
            for t in tables:
                db.execute(f'DROP TABLE IF EXISTS "{t}"')
            for statement in _statements(SCHEMA):
                db.execute(statement)
            db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            for t, (cols, rows) in carried.items():
                keep = {r[1] for r in db.execute(f'PRAGMA table_info("{t}")')}
                use = [i for i, c in enumerate(cols) if c in keep]
                if not use or not rows:
                    continue
                names = ", ".join(f'"{cols[i]}"' for i in use)
                db.executemany(f'INSERT OR IGNORE INTO "{t}" ({names}) VALUES '
                               f'({", ".join("?" * len(use))})',
                               [tuple(r[i] for i in use) for r in rows])
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("VACUUM")

    def close(self):
        with self._lock:
            self.db.close()

    def mark_interrupted(self):
        """Attempts and speaker passes still 'running' were cut short by a
        crash or kill of the previous session (this process owns the state,
        #15). Run at open, or (R11) once a damaged database's hold is released."""
        with self.tx() as db:
            db.execute("UPDATE attempts SET result = 'interrupted' WHERE result = 'running'")
            # Same for a speaker pass cut short: it restarts from the start. A
            # run whose version row is missing (a damaged database opened with
            # Continue anyway) is still marked, without a note: it has no
            # meeting to bind one to, and it is never shown.
            for rid, mid in db.execute(
                    "SELECT r.id, v.meeting_id FROM speaker_runs r LEFT JOIN versions v "
                    "ON v.id = r.version_id WHERE r.status = 'running'").fetchall():
                note = (None if mid is None else
                        self._seal(mid, "speaker_runs", "error", rid, SPEAKER_RESTART_NOTE))
                db.execute("UPDATE speaker_runs SET status = 'interrupted', error = ? "
                           "WHERE id = ?", (note, rid))

    # -- content columns (CONTENT_COLUMNS) ---------------------------------------

    def _seal(self, meeting, table, column, row, value):
        return None if value is None else self.codec.seal(meeting, table, column, row, value)

    def _unseal(self, meeting, table, column, row, value):
        return None if value is None else self.codec.unseal(meeting, table, column, row, value)

    def _opened(self, table, row, meeting, key):
        """A row as a dict, with its content columns opened."""
        if row is None:
            return None
        out = dict(row)
        for column in CONTENT_COLUMNS.get(table, ()):
            if column in out:
                out[column] = self._unseal(meeting, table, column, key, out[column])
        return out

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
                self.revision += 1
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
                       "VALUES (?, ?, ?, ?)",
                       (mid, self._seal(mid, "meetings", "title", mid, title), now, now))
        return mid

    def delete_new_meeting(self, mid):
        """Remove a meeting that never got a source (e.g. a failed import)."""
        with self.tx() as db:
            if db.execute("SELECT 1 FROM sources WHERE meeting_id = ?", (mid,)).fetchone():
                raise StoreError("Meeting has content; use cleanup (#17).")
            db.execute("DELETE FROM meetings WHERE id = ?", (mid,))

    def _meeting(self, row):
        return self._opened("meetings", row, row["id"], row["id"]) if row is not None else None

    def meeting(self, mid):
        return self._meeting(self._one("SELECT * FROM meetings WHERE id = ?", (mid,)))

    def meetings(self, where="1", args=()):
        """Meetings matching a condition on their plain columns (a fixed SQL
        expression from the caller, never user text), oldest first."""
        return [self._meeting(r) for r in self._all(
            f"SELECT * FROM meetings WHERE {plain_condition(where)} ORDER BY created_at", args)]

    def rename_meeting(self, mid, title):
        with self.tx() as db:
            db.execute("UPDATE meetings SET title = ? WHERE id = ?",
                       (self._seal(mid, "meetings", "title", mid, title), mid))

    def add_source(self, mid, *, original_path, original_sha256, original_size,
                   original_mtime_ns, audio_sha256, samples, sample_rate):
        sid = new_id()
        with self.tx() as db:
            db.execute("INSERT INTO sources (id, meeting_id, original_path, original_sha256, "
                       "original_size, original_mtime_ns, audio_sha256, samples, sample_rate, "
                       "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (sid, mid, self._seal(mid, "sources", "original_path", sid, original_path),
                        original_sha256, original_size, original_mtime_ns, audio_sha256,
                        samples, sample_rate, time.time()))
        return sid

    def _source(self, row):
        return (self._opened("sources", row, row["meeting_id"], row["id"])
                if row is not None else None)

    def source(self, sid):
        return self._source(self._one("SELECT * FROM sources WHERE id = ?", (sid,)))

    def sources(self, mid):
        """A meeting's sources, oldest first."""
        return [self._source(r) for r in self._all(
            "SELECT * FROM sources WHERE meeting_id = ? ORDER BY created_at", (mid,))]

    def set_source_intake(self, sid, folder_id, may_delete):
        """Where an import came from (#24). The original's location is kept
        only when the folder's explicit setting allows deleting it."""
        with self.tx() as db:
            if may_delete:
                db.execute("UPDATE sources SET delete_original = 1, intake_folder = ? "
                           "WHERE id = ?", (folder_id, sid))
            else:
                db.execute("UPDATE sources SET original_path = NULL, intake_folder = ? "
                           "WHERE id = ?", (folder_id, sid))

    def set_paused(self, mid, paused):
        with self.tx() as db:
            db.execute("UPDATE meetings SET paused = ?, updated_at = ? WHERE id = ?",
                       (1 if paused else 0, time.time(), mid))

    def set_phase(self, mid, phase, error=None):
        with self.tx() as db:
            db.execute("UPDATE meetings SET phase = ?, last_error = ?, updated_at = ? "
                       "WHERE id = ?", (phase, self._seal(mid, "meetings", "last_error", mid,
                                                          error), time.time(), mid))

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
                "SELECT c.status, v.state, m.content_epoch, m.id AS mid FROM chunks c "
                "JOIN versions v ON v.id = c.version_id JOIN meetings m ON m.id = v.meeting_id "
                "WHERE c.version_id = ? AND c.idx = ?", (vid, idx)).fetchone()
            if (row is None or row["status"] == "done" or row["state"] != "active"
                    or (epoch is not None and epoch != row["content_epoch"])):
                return False
            # Ids are given here, not by SQLite, so each passage's text can be
            # bound to its row before it is written (C2b seals it).
            first = db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM segments").fetchone()[0]
            mid, rows = row["mid"], []
            for sid, s in enumerate(segments, start=first):
                words = (json.dumps([list(w) for w in s.words])
                         if getattr(s, "words", ()) else None)
                rows.append((sid, vid, idx, s.start, s.end,
                             self._seal(mid, "segments", "text", sid, s.text),
                             self._seal(mid, "segments", "words_json", sid, words)))
            db.executemany(
                "INSERT INTO segments (id, version_id, chunk_idx, start_s, end_s, text, "
                "words_json) VALUES (?,?,?,?,?,?,?)", rows)
            db.execute("UPDATE chunks SET status = 'done', committed_at = ? "
                       "WHERE version_id = ? AND idx = ?", (time.time(), vid, idx))
            if attempt_id is not None:
                db.execute("UPDATE attempts SET result = 'committed', backend = ?, precision = ? "
                           "WHERE id = ?", (backend, precision, attempt_id))
        return True

    # -- transcript and edits ----------------------------------------------------

    def _passage(self, r):
        """{id, start_s, end_s, text, generated, edited, words_json} from a row
        with s.id, s.start_s, s.end_s, generated (s.text), edit (e.text),
        s.words_json and mid; the edit, where there is one, is the text."""
        mid, sid = r["mid"], r["id"]
        generated = self._unseal(mid, "segments", "text", sid, r["generated"])
        edit = self._unseal(mid, "edits", "text", sid, r["edit"])
        return {"id": sid, "start_s": r["start_s"], "end_s": r["end_s"],
                "text": generated if edit is None else edit, "generated": generated,
                "edited": int(edit is not None),
                "words_json": self._unseal(mid, "segments", "words_json", sid, r["words_json"])}

    _PASSAGES = ("SELECT s.id, s.start_s, s.end_s, s.text AS generated, e.text AS edit, "
                 "s.words_json, v.meeting_id AS mid FROM segments s "
                 "JOIN versions v ON v.id = s.version_id "
                 "LEFT JOIN edits e ON e.segment_id = s.id ")

    def transcript(self, vid):
        """Segments in time order, with any human edit applied on top."""
        return [self._passage(r) for r in self._all(
            self._PASSAGES + "WHERE s.version_id = ? ORDER BY s.start_s, s.id", (vid,))]

    def passage(self, segment_id):
        """One passage (as transcript()), or None."""
        r = self._one(self._PASSAGES + "WHERE s.id = ?", (segment_id,))
        return self._passage(r) if r is not None else None

    # -- speaker separation (#16) -------------------------------------------------

    def start_speaker_run(self, vid, hint_count=None, skips_seen=None):
        """Begin a whole-meeting speaker pass. Earlier runs for this version are
        superseded; transcription is never touched. With skips_seen (the number
        of skips when the pass was chosen), a skip recorded since means the
        user's latest choice is no pass: nothing is started and None is
        returned. Checked in the same transaction as the start, so no skip can
        fall between the check and the start."""
        rid = new_id()
        with self.tx() as db:
            if skips_seen is not None and _skip_count(db, vid) != skips_seen:
                return None
            # Only a pass still running is superseded now; the previous good
            # labels stay current until this pass actually succeeds.
            db.execute("UPDATE speaker_runs SET status = 'superseded' "
                       "WHERE version_id = ? AND status = 'running'", (vid,))
            epoch = db.execute("SELECT m.content_epoch FROM versions v JOIN meetings m "
                               "ON m.id = v.meeting_id WHERE v.id = ?", (vid,)).fetchone()
            db.execute("INSERT INTO speaker_runs (id, version_id, status, hint_count, started_at,"
                       " epoch) VALUES (?, ?, 'running', ?, ?, ?)",
                       (rid, vid, hint_count, _next_run_time(db, vid),
                        epoch[0] if epoch else None))
        return rid

    def skip_count(self, vid):
        """How many times speakers were skipped for this version (start_speaker_run)."""
        return self._one("SELECT COUNT(*) FROM speaker_runs WHERE version_id = ? AND "
                         "status = 'skipped'", (vid,))[0]

    def finish_speaker_run(self, rid, turns, exclusive_turns, backend=None, precision=None):
        """Atomically store turns and per-segment attribution, mark the run done
        and supersede earlier completed runs. Refused (False, nothing written)
        if the run is no longer 'running', its version is no longer active or
        the meeting's content changed meanwhile (cleanup, #17)."""
        run = self._one("SELECT status, version_id, epoch FROM speaker_runs WHERE id = ?",
                        (rid,))
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

    def skip_speaker_run(self, vid, again=False):
        """Record that the user chose not to identify speakers for this
        version. A pass still running is superseded in the same transaction,
        so if it finishes anyway its result is refused (finish_speaker_run)
        and it cannot be restarted. Returns "skipped"; "labelled" (nothing
        recorded) if labels were saved first, also by a pass finishing just
        now; or "already" if the latest run is already a skip, unless `again`
        (a pass was asked for since, which this skip cancels)."""
        rid = new_id()
        with self.tx() as db:
            runs = db.execute("SELECT status FROM speaker_runs WHERE version_id = ? "
                              "ORDER BY started_at, rowid", (vid,)).fetchall()
            if any(r[0] == "done" for r in runs):
                return "labelled"
            if runs and runs[-1][0] == "skipped" and not again:
                return "already"
            db.execute("UPDATE speaker_runs SET status = 'superseded' "
                       "WHERE version_id = ? AND status = 'running'", (vid,))
            at = _next_run_time(db, vid)
            db.execute("INSERT INTO speaker_runs (id, version_id, status, started_at, "
                       "finished_at) VALUES (?, ?, 'skipped', ?, ?)", (rid, vid, at, at))
        return "skipped"

    def end_speaker_run(self, rid, status, error=None):
        """Mark a run failed or interrupted. Its partial data was never written."""
        with self.tx() as db:
            row = db.execute("SELECT v.meeting_id FROM speaker_runs r LEFT JOIN versions v "
                             "ON v.id = r.version_id WHERE r.id = ?", (rid,)).fetchone()
            if row is None:
                return
            # No version row (a damaged database): no meeting to bind a note to.
            note = None if row[0] is None else self._seal(row[0], "speaker_runs", "error",
                                                          rid, error)
            db.execute("UPDATE speaker_runs SET status = ?, error = ?, finished_at = ? "
                       "WHERE id = ? AND status = 'running'", (status, note, time.time(), rid))

    def _runs(self, where, args):
        """Speaker runs with their error opened, oldest first per version."""
        out = []
        for r in self._all("SELECT r.*, v.meeting_id AS mid FROM speaker_runs r "
                           "JOIN versions v ON v.id = r.version_id WHERE " + where +
                           " ORDER BY r.started_at, r.rowid", args):
            run = self._opened("speaker_runs", r, r["mid"], r["id"])
            del run["mid"]
            out.append(run)
        return out

    def speaker_runs(self, vid):
        """Oldest first: the last run is the meeting's speaker state. Start
        times are strictly increasing per version (_next_run_time); rowid only
        orders rows written before that."""
        return self._runs("r.version_id = ?", (vid,))

    def active_speaker_runs(self):
        """Every speaker run of every active version, oldest first."""
        return self._runs("v.state = 'active'", ())

    def current_run(self, vid):
        """The latest finished speaker run of a version, or None."""
        done = [r for r in self.speaker_runs(vid) if r["status"] == "done"]
        return done[-1] if done else None

    # -- speaker corrections (#21) ----------------------------------------------------

    def _corrections(self, where, args):
        return [self._opened("speaker_corrections", r, r["mid"], r["id"]) for r in self._all(
            "SELECT c.id, c.run_id, c.kind, c.target, c.value, c.at, v.meeting_id AS mid "
            "FROM speaker_corrections c JOIN speaker_runs r ON r.id = c.run_id "
            "JOIN versions v ON v.id = r.version_id WHERE " + where + " ORDER BY c.id", args)]

    def corrections(self, rid):
        """A run's corrections, oldest first, with their values opened."""
        return self._corrections("c.run_id = ?", (rid,))

    def active_corrections(self):
        """Corrections of every finished run of an active version, oldest first."""
        return self._corrections("r.status = 'done' AND v.state = 'active'", ())

    def add_correction(self, rid, kind, target, value):
        with self.tx() as db:
            row = db.execute("SELECT v.meeting_id FROM speaker_runs r JOIN versions v "
                             "ON v.id = r.version_id WHERE r.id = ?", (rid,)).fetchone()
            if row is None:
                raise StoreError("That speaker identification no longer exists.")
            cid = db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM speaker_corrections"
                             ).fetchone()[0]
            db.execute("INSERT INTO speaker_corrections (id, run_id, kind, target, value, at) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (cid, rid, kind, str(target),
                        self._seal(row[0], "speaker_corrections", "value", cid, value),
                        time.time()))

    def current_speakers(self, vid):
        """{segment_id: (label, overlap)} from the latest completed run, or {}.
        Test helper: the app does not call it."""
        run = self._one("SELECT id FROM speaker_runs WHERE version_id = ? AND status = 'done' "
                        "ORDER BY started_at DESC, rowid DESC LIMIT 1", (vid,))
        if run is None:
            return {}
        return {r["segment_id"]: (r["label"], bool(r["overlap"])) for r in self._all(
            "SELECT * FROM segment_speakers WHERE run_id = ?", (run["id"],))}

    def save_edit(self, segment_id, text):
        """Store a human edit on top of the generated text, recording what it
        replaced so it can be undone. Saving the generated text again removes
        the edit. Returns False if nothing changed."""
        with self.tx() as db:
            seg = db.execute(self._PASSAGES + "WHERE s.id = ?", (segment_id,)).fetchone()
            if seg is None:
                raise StoreError("That passage no longer exists.")
            passage = self._passage(seg)
            mid = seg["mid"]
            current = passage["text"] if passage["edited"] else None
            new = None if text == passage["generated"] else text
            if new == current:
                return False
            self._set_edit(db, mid, segment_id, new)
            hid = db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM edit_history").fetchone()[0]
            db.execute("INSERT INTO edit_history (id, segment_id, before, after, at) "
                       "VALUES (?,?,?,?,?)",
                       (hid, segment_id, self._seal(mid, "edit_history", "before", hid, current),
                        self._seal(mid, "edit_history", "after", hid, new), time.time()))
        return True

    def _set_edit(self, db, mid, segment_id, text):
        if text is None:
            db.execute("DELETE FROM edits WHERE segment_id = ?", (segment_id,))
        else:
            db.execute("INSERT INTO edits (segment_id, text, edited_at) VALUES (?,?,?) "
                       "ON CONFLICT(segment_id) DO UPDATE SET text = excluded.text, "
                       "edited_at = excluded.edited_at",
                       (segment_id, self._seal(mid, "edits", "text", segment_id, text),
                        time.time()))

    def undo_edit(self, vid):
        """Undo the most recent edit in this version. Returns the segment id,
        or None when there is nothing to undo. Survives restart."""
        with self.tx() as db:
            last = db.execute("SELECT h.id, h.segment_id, h.before, v.meeting_id AS mid "
                              "FROM edit_history h JOIN segments s ON s.id = h.segment_id "
                              "JOIN versions v ON v.id = s.version_id WHERE s.version_id = ? "
                              "ORDER BY h.id DESC LIMIT 1", (vid,)).fetchone()
            if last is None:
                return None
            before = self._unseal(last["mid"], "edit_history", "before", last["id"],
                                  last["before"])
            self._set_edit(db, last["mid"], last["segment_id"], before)
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

    # -- holds, handoff options, the reviewed snapshot (workspace/holds.py,
    #    handoff.py and cleanup.py keep the rules; the values live here) ---------

    def add_hold(self, hid, mid, scope, reason, now):
        with self.tx() as db:
            db.execute("INSERT INTO holds (id, meeting_id, scope, reason, created_at, "
                       "reviewed_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (hid, mid, scope, self._seal(mid, "holds", "reason", hid, reason),
                        now, now))

    def holds(self, where="1", args=()):
        """Holds matching a condition on their plain columns (a fixed SQL
        expression from the caller), oldest first, with their reasons opened."""
        return [self._opened("holds", r, r["meeting_id"], r["id"]) for r in self._all(
            f"SELECT * FROM holds WHERE {plain_condition(where)} ORDER BY created_at", args)]

    def handoff_options(self, mid):
        """(omitted, redactions) as stored (JSON text), or None."""
        row = self._one("SELECT omitted, redactions FROM handoff_options WHERE meeting_id = ?",
                        (mid,))
        if row is None:
            return None
        return (self._unseal(mid, "handoff_options", "omitted", mid, row["omitted"]),
                self._unseal(mid, "handoff_options", "redactions", mid, row["redactions"]))

    def set_handoff_options(self, mid, omitted, redactions):
        with self.tx() as db:
            db.execute("INSERT INTO handoff_options (meeting_id, omitted, redactions) "
                       "VALUES (?, ?, ?) ON CONFLICT(meeting_id) DO UPDATE SET "
                       "omitted = excluded.omitted, redactions = excluded.redactions",
                       (mid, self._seal(mid, "handoff_options", "omitted", mid, omitted),
                        self._seal(mid, "handoff_options", "redactions", mid, redactions)))

    def put_snapshot(self, db, mid, text, sha256):
        """The reviewed transcript frozen at confirmation, written inside the
        caller's transaction (db from tx())."""
        db.execute("INSERT OR REPLACE INTO reviewed_snapshots (meeting_id, text, sha256, "
                   "created_at) VALUES (?, ?, ?, ?)",
                   (mid, self._seal(mid, "reviewed_snapshots", "text", mid, text), sha256,
                    time.time()))

    def snapshot(self, mid):
        """The reviewed snapshot's text, or None."""
        row = self._one("SELECT text FROM reviewed_snapshots WHERE meeting_id = ?", (mid,))
        return (self._unseal(mid, "reviewed_snapshots", "text", mid, row["text"])
                if row is not None else None)

    # -- watched folders (workspace/intake.py keeps the rules): workspace-wide
    #    values, so meeting is None --------------------------------------------------

    def add_folder(self, fid, path, include_existing):
        """False if the folder is already watched (the path is UNIQUE); any
        other failure raises StoreError as usual."""
        try:
            with self.tx() as db:
                db.execute("INSERT INTO watched_folders (id, path, created_at, "
                           "include_existing) VALUES (?, ?, ?, ?)",
                           (fid, self._seal(None, "watched_folders", "path", fid, path),
                            time.time(), 1 if include_existing else 0))
        except StoreError as exc:
            if isinstance(exc.__cause__, sqlite3.IntegrityError):
                return False
            raise
        return True

    def folders(self, where="1", args=()):
        """Watched folders matching a condition on their plain columns, oldest
        first, with their paths opened."""
        return [self._opened("watched_folders", r, None, r["id"]) for r in self._all(
            f"SELECT * FROM watched_folders WHERE {plain_condition(where)} ORDER BY created_at",
            args)]
