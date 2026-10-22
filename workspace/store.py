# workspace/store.py
# SQLite state for Unminute's workspace (#14). One state-owning process opens
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
# Content is sealed and opened only while that lock is held (vault.Sealer's
# thread rule, #111 C2b-2): _seal and _unseal refuse otherwise.
#
# Schema 2 (#111 C2b-2, docs/ENCRYPTION.md): every content column is sealed
# (AES-GCM, through keystore.VaultCodec) under its meeting's text key or the
# workspace key, and renamed "<name>_sealed", so a raw read or write that
# went around the codec fails with "no such column". The root key is kept in
# crypto_meta, protected by Windows for this user; meeting keys in
# content_keys and recording keys in recording_keys, wrapped. Hashes that
# could confirm a known file are HMAC fingerprints ("_fp"). Files the app
# keeps beside the database (the handoff templates, #111 C2c) are sealed
# under the workspace key through file_sealer, never with the store's codec.

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

from workspace import keystore, vault
from workspace.dpapi import DpapiError

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

-- The workspace's root key, protected by the protector (Windows, for this
-- user), and its check value (vault.Keyring). One row; written only when the
-- database is created or migrated to schema 2. Only this module names it.
CREATE TABLE IF NOT EXISTS crypto_meta (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    format      INTEGER NOT NULL,     -- 1: vault v1 (docs/ENCRYPTION.md)
    protector   TEXT NOT NULL,        -- 'dpapi' in the app; tests use their own name
    root        BLOB NOT NULL,
    check_value BLOB NOT NULL,
    created_at  REAL NOT NULL);

CREATE TABLE IF NOT EXISTS meetings (
    id            TEXT PRIMARY KEY,
    title_sealed  BLOB NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    phase         TEXT NOT NULL DEFAULT 'new',
    paused        INTEGER NOT NULL DEFAULT 0,      -- explicit user pause, survives restart
    content_epoch INTEGER NOT NULL DEFAULT 0,      -- bumped by cleanup (#17); drops stale results
    last_error_sealed BLOB,
    reviewed_at   REAL,      -- explicit "transcript reviewed" confirmation (#17)
    final_saved_at REAL,     -- explicit "final deliverable reviewed and saved" confirmation
    audio_removed_at REAL    -- app audio cleaned up; playback/retranscription unavailable
);

-- Each meeting's keys (text, audio), wrapped (vault.Keyring.wrap): created
-- with the meeting, destroyed at its cleanup checkpoints (the erase points,
-- C3). Only this module names the table in SQL.
CREATE TABLE IF NOT EXISTS content_keys (
    meeting_id TEXT NOT NULL REFERENCES meetings(id),
    purpose    TEXT NOT NULL,          -- 'text' | 'audio' (audio used from C3a)
    wrapped    BLOB NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (meeting_id, purpose));

-- A microphone recording's key (#30, used from C3b), wrapped; made before
-- the recording starts, as no meeting exists until it is imported.
CREATE TABLE IF NOT EXISTS recording_keys (
    recording_id TEXT PRIMARY KEY,     -- the recorder's folder id
    wrapped      BLOB NOT NULL,
    created_at   REAL NOT NULL);

-- The reviewed transcript frozen at confirmation, before any audio is removed.
CREATE TABLE IF NOT EXISTS reviewed_snapshots (
    meeting_id  TEXT PRIMARY KEY REFERENCES meetings(id),
    text_sealed BLOB NOT NULL,
    text_fp     TEXT NOT NULL,                     -- fingerprint (purpose 'snapshot')
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id               TEXT PRIMARY KEY,
    meeting_id       TEXT NOT NULL REFERENCES meetings(id),
    original_path_sealed BLOB,                      -- only for #24 deletion authority
    original_fp      TEXT NOT NULL,                 -- fingerprint (purpose 'original')
    original_size    INTEGER NOT NULL,
    original_mtime_ns INTEGER NOT NULL,
    audio_id         TEXT NOT NULL,   -- decoded 16 kHz mono float32: its SHA-256 until C3a
    samples          INTEGER NOT NULL,
    sample_rate      INTEGER NOT NULL,
    created_at       REAL NOT NULL,
    delete_original  INTEGER NOT NULL DEFAULT 0,  -- explicit authority to delete the original (#24)
    intake_folder    TEXT     -- watched folder it came from (#24); NULL for imports by hand
);

-- A meeting made from a microphone recording (#30): which recording it came
-- from (stops a second import) and its details, sealed. Used from C3b.
-- Removed at audio cleanup, as recording.json was.
CREATE TABLE IF NOT EXISTS recording_sources (
    meeting_id   TEXT PRIMARY KEY REFERENCES meetings(id),
    recording_id TEXT NOT NULL UNIQUE,
    info_sealed  BLOB NOT NULL);

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

-- Ids never come back (AUTOINCREMENT): a sealed value is bound to its row id.
CREATE TABLE IF NOT EXISTS segments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   TEXT NOT NULL REFERENCES versions(id),
    chunk_idx    INTEGER NOT NULL,
    start_s      REAL NOT NULL,
    end_s        REAL NOT NULL,
    text_sealed  BLOB NOT NULL,
    words_sealed BLOB            -- [[start, end, text], ...] when the engine provides them
);
CREATE INDEX IF NOT EXISTS segments_by_time ON segments(version_id, start_s);

CREATE TABLE IF NOT EXISTS edits (
    segment_id  INTEGER PRIMARY KEY REFERENCES segments(id),
    text_sealed BLOB NOT NULL,
    edited_at   REAL NOT NULL
);
-- Undo history for edits (#20): each change records the text it replaced
-- (NULL = the generated text). Removed with the transcript at cleanup.
CREATE TABLE IF NOT EXISTS edit_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id    INTEGER NOT NULL REFERENCES segments(id),
    before_sealed BLOB,
    after_sealed  BLOB,
    at            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS edit_history_by_segment ON edit_history(segment_id);

-- Reserved for later issues (see docs/WORKSPACE_DESIGN.md).
-- Speaker separation (#16): a whole-meeting pass after transcription. Machine
-- attribution lives here; user corrections (#21) are stored separately.
CREATE TABLE IF NOT EXISTS speaker_runs (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES versions(id),
    status TEXT NOT NULL,      -- running | done | failed | interrupted | superseded | skipped
    hint_count INTEGER, started_at REAL, finished_at REAL, error_sealed BLOB,
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
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES speaker_runs(id),
    kind TEXT NOT NULL, target TEXT NOT NULL, value_sealed BLOB NOT NULL, at REAL NOT NULL);
-- Handoff-only omissions and redactions (#22). Meeting content: removed with
-- the transcript at cleanup.
CREATE TABLE IF NOT EXISTS handoff_options (
    meeting_id TEXT PRIMARY KEY REFERENCES meetings(id),
    omitted_sealed BLOB NOT NULL, redactions_sealed BLOB NOT NULL);
-- Watched-folder intake (#23): configuration plus bounded suppression data
-- (keyed path hashes, content fingerprints); no meeting content. A folder's
-- path can name a client, so it is sealed like content (CONTENT_COLUMNS,
-- under the workspace key) but counts as configuration: it is carried over
-- (CARRY_OVER) and never blocks an update (update.NON_CONTENT). A sealed
-- value has a random nonce, so uniqueness is on the folder's fingerprint.
CREATE TABLE IF NOT EXISTS watched_folders (
    id TEXT PRIMARY KEY, path_sealed BLOB NOT NULL,
    path_fp TEXT NOT NULL UNIQUE,                  -- fingerprint of intake.folder_identity
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL, last_scan_at REAL, last_error TEXT,
    baseline_done INTEGER NOT NULL DEFAULT 0, include_existing INTEGER NOT NULL DEFAULT 0,
    delete_originals INTEGER NOT NULL DEFAULT 0,   -- #24: explicit, separate from import
    fetch_cloud INTEGER NOT NULL DEFAULT 0);       -- #24: read cloud-only files to fetch them
CREATE TABLE IF NOT EXISTS intake_seen (
    folder_id TEXT NOT NULL, key TEXT NOT NULL, size INTEGER, mtime_ns INTEGER,
    file_id INTEGER, state TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    content_fp TEXT,           -- the content's fingerprint only while an import is in progress
    PRIMARY KEY (folder_id, key));
CREATE TABLE IF NOT EXISTS intake_content (
    fp TEXT PRIMARY KEY, imported_at REAL NOT NULL);
-- The intake path-hash salt (key 'salt'), sealed under the workspace key.
CREATE TABLE IF NOT EXISTS intake_meta (k TEXT PRIMARY KEY, v_sealed BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS cleanup_jobs (
    id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, phase TEXT NOT NULL,
    state TEXT NOT NULL, intent_at REAL NOT NULL, last_attempt_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS holds (
    id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, scope TEXT NOT NULL,
    reason_sealed BLOB NOT NULL, created_at REAL NOT NULL, reviewed_at REAL,
    released_at REAL);
CREATE TABLE IF NOT EXISTS receipts (
    job_id TEXT PRIMARY KEY, created_at REAL NOT NULL, completed_at REAL, result TEXT NOT NULL);
"""


# Columns that hold meeting content, and watched-folder paths and the intake
# salt, which can name a client or test a guessed path (#111 C2;
# docs/ENCRYPTION.md): {table: {logical name: physical (sealed) column}}.
# Only this module names them in SQL, and every value read or written goes
# through the Store's codec (_seal, _unseal), sealed for its place: rows come
# back with the logical names. tests/test_content_access.py checks the SQL in
# workspace/ and tools/ (docs/WORKSPACE_DESIGN.md, "Content columns").
CONTENT_COLUMNS = {
    "meetings": {"title": "title_sealed", "last_error": "last_error_sealed"},
    "reviewed_snapshots": {"text": "text_sealed"},
    "sources": {"original_path": "original_path_sealed"},
    "segments": {"text": "text_sealed", "words_json": "words_sealed"},
    "edits": {"text": "text_sealed"},
    "edit_history": {"before": "before_sealed", "after": "after_sealed"},
    "speaker_runs": {"error": "error_sealed"},
    "speaker_corrections": {"value": "value_sealed"},
    "handoff_options": {"omitted": "omitted_sealed", "redactions": "redactions_sealed"},
    "holds": {"reason": "reason_sealed"},
    "recording_sources": {"info": "info_sealed"},
    "watched_folders": {"path": "path_sealed"},
    "intake_meta": {"v": "v_sealed"},
}
# The physical columns, by table: what SQLite sees.
SEALED_COLUMNS = {t: frozenset(cols.values()) for t, cols in CONTENT_COLUMNS.items()}
# Values kept as JSON text: unreadable, each opens as this instead of
# DAMAGED_TEXT (so a reader that parses it never fails).
_DAMAGED_JSON = {("segments", "words_json"): None, ("handoff_options", "omitted"): "[]",
                 ("handoff_options", "redactions"): "[]", ("recording_sources", "info"): None}

_CONTENT_NAMES = frozenset(n for cols in CONTENT_COLUMNS.values()
                           for pair in cols.items() for n in pair)

# Tables that hold keys: the erase points. Only this module names them in SQL.
KEY_TABLES = ("crypto_meta", "content_keys", "recording_keys")

# A sealed value that does not open is shown as this, never as anything else
# (docs/ENCRYPTION.md, "Damaged values").
DAMAGED_TEXT = "[Could not be read]"
DAMAGED_REFUSAL = ("Part of this meeting could not be read, so nothing was produced. The "
                   "meeting may be damaged.")

# A meeting's plain columns: what state checks need, with nothing to open
# (meeting_state(), meetings(content=False)).
MEETING_STATE = ("id", "created_at", "updated_at", "phase", "paused", "content_epoch",
                 "reviewed_at", "final_saved_at", "audio_removed_at")
_STATE_COLUMNS = ", ".join(MEETING_STATE)
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


class KeyUnavailable(StoreError):
    """The workspace's root key cannot be used, so nothing can be opened or
    saved (#111 C2b-2; C4 shows the locked screen). reason: 'windows'
    (Windows would not unlock it: another user or computer, keys lost after a
    password reset, a damaged profile), 'key' (it unlocked to something that
    is not this workspace's key), 'protector' (the workspace was made with
    another protector) or 'missing' (no key where one must be). code: the
    Windows error number for 'windows', else None. The message is fixed text,
    with no path or value (P3). Never a new key over existing data, never
    plain text."""

    MESSAGE = "Windows could not unlock Unminute's encryption key on this computer."

    def __init__(self, reason, code=None):
        super().__init__(self.MESSAGE)
        self.reason = reason
        self.code = code


class DamagedContent(StoreError):
    """A sealed value, or a meeting's wrapped key, does not open: changed,
    moved to another place, or from another workspace. Reading for display
    shows DAMAGED_TEXT instead; anything that would produce text for use
    outside the app, or new content from old, refuses (Store.strict)."""

    def __init__(self, message=DAMAGED_REFUSAL):
        super().__init__(message)


class KeyMissing(DamagedContent):
    """A meeting's key row is absent (deleted at cleanup, or never made)."""


# Not meeting content: kept across an empty-workspace migration (#26). From
# schema 1 the rows go through keystore.transform_from_1 (folder paths and the
# salt sealed, hashes fingerprinted); from schema 2 on they are copied by
# column name, sealed values with them (their table and column names, and so
# their contexts, do not change), and crypto_meta keeps the root key that
# opens them.
CARRY_OVER = ("receipts", "watched_folders", "intake_content", "intake_meta",
              "intake_seen", "crypto_meta")

PLAIN_IN_SEALED = "A migration would copy a plain value into a sealed column."
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


def _plain_in_sealed(rows, carried):
    """Whether rows made from schema 1 (keystore.transform_from_1) would put
    anything but a sealed value in a sealed column, or anything but a
    fingerprint in a fingerprint column ("fp" or a name ending "_fp"). A
    fingerprint equal to a value schema 1 held (a plain SHA-256 copied
    across) counts as plain."""
    plain = {v for _cols, data in carried.values() for r in data for v in r
             if isinstance(v, str)}
    for table, entries in rows.items():
        sealed = SEALED_COLUMNS.get(table, frozenset())
        for entry in entries:
            for column, value in entry.items():
                if value is None:
                    continue
                if column in sealed:
                    if not (isinstance(value, bytes) and len(value) >= vault.OVERHEAD
                            and value[0] == vault.VERSION):
                        return True
                elif column == "fp" or column.endswith("_fp"):
                    if not (isinstance(value, str) and _FINGERPRINT.match(value)) \
                            or value in plain:
                        return True
    return False


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
    where = _synced_or_network(base)
    if where == "network":
        raise StoreError("This account's local app data folder is on a network drive. "
                         "Unminute keeps meetings on this computer. Ask your IT team.")
    if where == "onedrive":
        raise StoreError("This account's local app data folder is inside OneDrive. "
                         "Unminute keeps meetings out of synced folders. Ask your IT team.")
    return path


def _synced_or_network(path):
    """Where the real location of path is: "network" (a network share or a
    mapped network drive), "onedrive" (inside a folder a OneDrive variable
    names) or None (on this computer). Real paths are compared: short (8.3)
    names and junctions must not hide that a folder is inside OneDrive or on
    a network share. A folder that does not exist yet resolves through its
    nearest existing parent."""
    real = _real(path)
    if _network_path(real) or _remote_drive(real):
        return "network"
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
            return "onedrive"
    return None


def _real(path):
    try:
        real = os.path.realpath(path)
    except OSError:              # an error realpath does not handle itself
        return os.path.abspath(path)
    # Where the path does not exist, realpath stops resolving: for a junction
    # whose target is missing it returns the stored target as written, with
    # any short (8.3) name or junction in it left unresolved (#219 review). So
    # resolve the nearest existing parent again and add back the rest.
    head, tail = real, []
    while not os.path.exists(head):
        parent = os.path.dirname(head)
        if parent == head:       # nothing of it exists: keep what realpath gave
            return real
        tail.append(os.path.basename(head))
        head = parent
    if not tail:
        return real
    try:
        head = os.path.realpath(head)
    except OSError:
        return real
    return os.path.join(head, *reversed(tail))


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


# The workspace refused because its real location is not on this computer
# (#219). No path in either message (P3). The models folder is the one folder
# IT may redirect (docs/PACKAGING.md).
REDIRECTED = {
    "network": ("Unminute's workspace folder is redirected to a network drive. Unminute "
                "keeps meetings on this computer. Ask your IT team: only Unminute's models "
                "folder may be moved."),
    "onedrive": ("Unminute's workspace folder is redirected into OneDrive. Unminute keeps "
                 "meetings out of synced folders. Ask your IT team: only Unminute's models "
                 "folder may be moved."),
}

# The workspace refused because Unminute, or Unminute\workspace, is a junction
# or link whose target cannot be reached: removed, or on a drive that is not
# connected (#266). Opening it could only fail in mkdir, and the app closed
# without a message. No path in it (P3).
BROKEN_LINK = ("Unminute's workspace folder is linked to a folder that cannot be reached: "
               "it may have been removed, or be on a drive that is not connected. Ask your "
               "IT team to restore the folder or its drive, or to remove the link.")


def _broken_link(path):
    """True if path is a junction or symbolic link whose target cannot be
    reached (#266): missing, on a volume that is gone (a volume GUID or
    \\\\?\\ spelling), or itself a broken link (os.stat follows the whole
    chain). Not a folder that does not exist yet, and not a target this
    account may not open: those keep their own outcome."""
    if not (os.path.islink(path) or os.path.isjunction(path)):   # neither follows the link
        return False
    try:
        os.stat(path)
    except PermissionError:
        return False
    except OSError:
        return True
    return False


def _workspace_folder():
    # K1: the data folder's name is pinned (tests/test_consistency.py reads this line).
    return local_appdata() / "Unminute" / "workspace"


def default_root():
    """The workspace, %LOCALAPPDATA%\\Unminute\\workspace. local_appdata()
    checks %LOCALAPPDATA% itself, but a junction or link at Unminute, or at
    Unminute\\workspace, could still take the workspace into OneDrive or onto
    a network drive. So the real location of each is checked too, and the
    workspace is refused there (#219, D3). Either one being a junction or
    link whose target cannot be reached (a removed folder, or a drive that
    is not connected) is refused too, with its own message, BROKEN_LINK
    (#266): the workspace could not be created there. Only Unminute\\models
    may be redirected: it holds no meeting content, and the model cache
    (__main__.model_cache) does not come through here."""
    root = _workspace_folder()
    for folder in (root.parent, root):
        where = _synced_or_network(folder)      # first: a missing target in OneDrive says so
        if where:
            raise StoreError(REDIRECTED[where])
        if _broken_link(folder):
            raise StoreError(BROKEN_LINK)
    return root


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


def under_workspace_root(path):
    """Whether path is inside the real workspace folder,
    %LOCALAPPDATA%\\Unminute\\workspace, compared as Windows does (ntpath,
    so Linux CI agrees). False only when LOCALAPPDATA is not set, as on
    Linux CI: there is no real workspace to protect then. A folder that
    default_root() refuses (in OneDrive, on a network drive, redirected or a
    broken link) still counts: a real workspace may be there on disk."""
    import ntpath
    base = os.environ.get("LOCALAPPDATA", "")
    if not base:
        return False
    root = Path(base) / "Unminute" / "workspace"      # the names of _workspace_folder (K1)
    a = ntpath.normcase(os.path.realpath(path))
    b = ntpath.normcase(os.path.realpath(root))
    try:
        return ntpath.commonpath([a, b]) == b
    except ValueError:
        return False


PROTECTOR_REFUSED = "This workspace cannot be opened with a test key protector."
BUSY_TIMEOUT_MS = 30000           # the connection's wait for a lock (sqlite3.connect timeout=30)


class _OwnedLock:
    """The store's lock: re-entrant, and it knows which thread holds it, so
    content is sealed and opened only under it (_seal and _unseal check).
    acquisitions counts the outermost acquisitions (tests: one per accessor
    call, so a fetch and its unsealing happen under one hold). on_release,
    if set, is called at the outermost release while still holding the lock;
    what it returns (a callable, or None) is called just after the release."""

    def __init__(self):
        self._lock = threading.Lock()
        self._owner = None
        self._depth = 0
        self.acquisitions = 0
        self.on_release = None

    def acquire(self, blocking=True, timeout=-1):
        me = threading.get_ident()
        if self._owner == me:
            self._depth += 1
            return True
        if not self._lock.acquire(blocking, timeout):
            return False
        self._owner, self._depth = me, 1
        self.acquisitions += 1
        return True

    def release(self):
        if self._owner != threading.get_ident():
            raise RuntimeError("cannot release a lock this thread does not hold")
        self._depth -= 1
        if self._depth:
            return
        after = self.on_release() if self.on_release else None
        self._owner = None
        self._lock.release()
        if after is not None:
            after()

    def held(self):
        """Whether the calling thread holds the lock."""
        return self._owner == threading.get_ident()

    __enter__ = acquire

    def __exit__(self, *exc):
        self.release()


class _FileSealer:
    """Seals and opens one workspace file's bytes (Store.file_sealer), with
    its own AES-GCM object under its own lock. A file that does not open
    (changed, cut short, renamed from another file, or from another
    workspace) raises DamagedContent, with no value in the message."""

    def __init__(self, key, name):
        self._sealer = vault.Sealer(key)
        self._context = ("workspace", "file", name)
        self._lock = threading.Lock()

    def __repr__(self):
        return "<FileSealer>"                  # never the key

    def seal(self, data):
        with self._lock:
            return self._sealer.seal(data, self._context)

    def open(self, blob):
        with self._lock:
            try:
                return self._sealer.unseal(blob, self._context)
            except vault.VaultError:
                pass                # raised below, so no exception is chained to it
        raise DamagedContent()


class _TxKeys:
    """Key changes inside one transaction, applied to the key cache only once
    it commits: keys made (by create_meeting) and meetings whose keys were
    destroyed."""

    def __init__(self):
        self.new = {}               # meeting id -> (wrapped text key, its vault.Sealer)
        self.deleted = set()
        self.destroyed = False


def _next_ids(db, table, n):
    """n row ids for table, chosen before the insert (a value is sealed for
    its row first): above both the largest id in use and the largest ever
    given (sqlite_sequence, AUTOINCREMENT), so an id freed by an undo or a
    cleanup never comes back. Inside the caller's write transaction."""
    seq = db.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
    top = db.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
    first = max(seq[0] if seq else 0, top) + 1
    return range(first, first + n)


def _row(key):
    return key if isinstance(key, tuple) else (key,)


def _remove_quietly(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _install_new(new, path):
    """Put a newly built database in place (a separate function so tests can
    stop a creation between its commit and the rename)."""
    os.replace(new, path)


def compaction_marker(db_path):
    """A file beside the database (configuration, no content): a compaction
    is owed (#198), so the next start whose database check passes compacts
    (cleanup.compact_deferred)."""
    return Path(db_path).with_name("compact.pending")


def mark_compaction_owed(db_path):
    """Create the marker without following a link planted in its place: an
    existing file (or link) is left as it is, never written through. Returns
    whether this call created it."""
    try:
        fd = os.open(compaction_marker(db_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.close(fd)
    return True


class Store:
    def __init__(self, path, housekeeping=True, protector=None, on_unavailable="raise"):
        """housekeeping False (R11): open a database the check found damaged
        without marking interrupted work; mark_interrupted() runs later.
        protector: keeps the root key (keystore.DpapiProtector unless given;
        tests give their own, which is refused for the real workspace).
        on_unavailable: "raise" (KeyUnavailable when the key
        cannot be used) or "lock" (the store stays open read-only, with
        self.locked holding the KeyUnavailable: plain reads work, nothing is
        sealed, opened or written; the app's own start uses it, #111 C4a).
        Rows with no schema version open locked too ("missing"). Creating or
        migrating a workspace always raises when the key cannot be protected:
        there is no content to show."""
        if on_unavailable not in ("raise", "lock"):
            raise ValueError("on_unavailable is 'raise' or 'lock'")
        self.protector = protector if protector is not None else keystore.DpapiProtector()
        self.path = Path(path)
        # A test protector holds the root key in effect in clear text: never
        # for the real workspace (checked before anything is touched).
        if not self.protector.allows(self.path) or (
                self.protector.name != keystore.APP_PROTECTOR
                and under_workspace_root(self.path)):
            raise StoreError(PROTECTOR_REFUSED)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _OwnedLock()
        self._lock.on_release = self._take_damage
        self.locked = None          # the KeyUnavailable while locked (on_unavailable="lock")
        self.keyring = None
        # This open made the workspace's root key: a new database, a rebuilt
        # stub or the 1 to 2 migration (the audit log's first_use, #111 C5).
        self.new_root = False
        self.codec = None
        self.log = None             # diagnostics (Workspace sets its log); else diag's current one
        self._wrapped = {}          # meeting id -> wrapped text key (one load, then kept)
        self._sealers = {}          # meeting id -> vault.Sealer of its text key, made on first use
        self._tx = None             # _TxKeys while a transaction is open
        self._damage = {}           # (table, column) -> values that did not open, to report
        self._strict = threading.local()
        # Committed transactions so far: the page asks for the meeting lists
        # again only when this (or the app's own state) has changed (R7b).
        self.revision = 0
        made = None
        if not self.path.exists():
            made = self._create()                    # never anything on disk if this fails
        # isolation_level=None: we issue BEGIN/COMMIT ourselves (explicit transactions).
        self.db = sqlite3.connect(str(self.path), isolation_level=None, timeout=30,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            # Deleted rows are overwritten, not just unlinked (#17 cleanup).
            self.db.execute("PRAGMA secure_delete=ON")
            # Temporary tables and indices (VACUUM builds a copy of the whole
            # database) stay in memory, never in %TEMP% outside the workspace.
            self.db.execute("PRAGMA temp_store=MEMORY")
            # Read before anything is written: a database that is refused
            # (rows with no version, a key that cannot be used) is left
            # byte for byte as it was, so the journal mode is set only after.
            try:
                made = self._open_schema(housekeeping) or made
            except KeyUnavailable as exc:
                # Rows with no version ("missing", #111 C4a): content whose key
                # is unknown opens locked like any other key failure. A key
                # that cannot be protected for a new or migrated workspace
                # ("windows") always raises: there is no content to show.
                if on_unavailable == "raise" or exc.reason != "missing":
                    raise
                self._lock_down(exc)
                return
            self.new_root = made is not None
            try:
                self.keyring = self._unlock(made)
            except KeyUnavailable as exc:
                if on_unavailable == "raise":
                    raise
                self._lock_down(exc)
                return
            self.db.execute("PRAGMA journal_mode=WAL")   # a file already in WAL: no write
            self._load_keys()
            self.codec = keystore.VaultCodec(self.keyring, self._text_sealer)
            if housekeeping:
                self.mark_interrupted()
        except sqlite3.Error as exc:
            self.db.close()          # never leave the file locked on a refused open
            raise StoreError(str(exc)) from exc
        except BaseException:
            self.db.close()
            raise

    # -- creating, migrating and unlocking ------------------------------------------------

    def _protect(self, root):
        """The root key protected for storage, before anything is written.
        KeyUnavailable("windows") if the protector fails."""
        try:
            return self.protector.protect(root)
        except DpapiError as exc:
            raise KeyUnavailable("windows", exc.code) from None

    def _build(self, db, blob, keyring):
        """Every table, the version and the key, inside the caller's transaction."""
        for statement in _statements(SCHEMA):
            db.execute(statement)
        db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
        db.execute("INSERT INTO crypto_meta (id, format, protector, root, check_value, "
                   "created_at) VALUES (1, 1, ?, ?, ?, ?)",
                   (self.protector.name, blob, keyring.check_value, time.time()))

    def _create(self):
        """A new workspace: the root key is made and protected first, then the
        whole database is built in one transaction at workspace.db.new (with a
        rollback journal, so no committed page is left in a write-ahead log)
        and renamed into place. Any failure removes workspace.db.new: a failed
        first start leaves no workspace.db (update.current_schema: None)."""
        root = vault.new_key()
        blob = self._protect(root)
        keyring = vault.Keyring(root)
        new = self.path.with_name(self.path.name + ".new")
        journal = new.with_name(new.name + "-journal")
        _remove_quietly(new)                    # left by a start that stopped part-way
        _remove_quietly(journal)
        try:
            con = sqlite3.connect(str(new), isolation_level=None)
            try:
                con.execute("PRAGMA journal_mode=DELETE")
                con.execute("BEGIN IMMEDIATE")
                self._build(con, blob, keyring)
                con.execute("COMMIT")
            finally:
                con.close()
            _install_new(new, self.path)
        except BaseException as exc:
            _remove_quietly(new)
            _remove_quietly(journal)
            if isinstance(exc, sqlite3.Error):
                raise StoreError(str(exc)) from exc
            raise
        return keyring

    def _tables(self):
        return [r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]

    def _open_schema(self, housekeeping):
        """Read the version (read only) and act on it. Returns the keyring when
        this open made a new root key (a stub rebuilt, or a 1 to 2 migration)."""
        db = self.db
        has_version = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                                 "name = 'schema_version'").fetchone()
        current = (db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                   if has_version else None)
        if current is None:
            # No version: a stub from an older build if every table is empty
            # (built afresh, in place); otherwise data whose key is unknown,
            # never given a new key or version.
            if any(db.execute(f'SELECT 1 FROM "{t}" LIMIT 1').fetchone()
                   for t in self._tables()):
                raise KeyUnavailable("missing")
            if not housekeeping:
                raise MigrationBlocked("Unminute found damage in its workspace database, so it "
                                       "will not update the workspace's storage.")
            return self._rebuild_stub()
        if current > SCHEMA_VERSION:
            raise SchemaTooNew(f"This workspace was created by a newer version of "
                               f"the app (schema {current}).")
        if current < SCHEMA_VERSION:
            return self._migrate(current, housekeeping)
        return None

    def _rebuild_stub(self):
        root = vault.new_key()
        blob = self._protect(root)               # before anything is written
        keyring = vault.Keyring(root)
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            for t in self._tables():
                db.execute(f'DROP TABLE IF EXISTS "{t}"')
            self._build(db, blob, keyring)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return keyring

    def _migrate(self, current, housekeeping=True):
        """Opening a workspace written with an older schema (#26): migrate only
        if it holds no meeting content, no active hold and no unfinished
        cleanup. Migration rebuilds the tables; receipts and watched-folder
        configuration (CARRY_OVER) are carried over. No backup is made. Never
        while the database check holds work (housekeeping False, R11): damaged
        tables can read as empty, and rebuilding would drop what is left.
        From schema 1 (#111 C2b-2): a root key is made and protected before
        anything is written, the carried rows are sealed and fingerprinted
        (keystore.transform_from_1), and the file is compacted afterwards so
        the old plain folder paths and salt are not left in free pages or the
        write-ahead log. Returns the new keyring (from schema 1), else None."""
        db = self.db
        if not housekeeping:
            raise MigrationBlocked("Unminute found damage in its workspace database, so it "
                                   "will not update the workspace's storage.")
        from workspace import update
        found = update.blockers(self.path)
        if found:
            raise MigrationBlocked(" ".join(b["message"] for b in found))
        tables = self._tables()
        carried = {}
        for t in CARRY_OVER:
            if t in tables:
                cols = [r[1] for r in db.execute(f'PRAGMA table_info("{t}")')]
                carried[t] = (cols, [tuple(r) for r in db.execute(f'SELECT * FROM "{t}"')])
        keyring = blob = None
        if current < 2:
            root = vault.new_key()
            blob = self._protect(root)           # before BEGIN: nothing written if it fails
            keyring = vault.Keyring(root)
            rows = keystore.transform_from_1(carried, keyring)
            # Before anything is written: every sealed or fingerprinted value
            # must be one, never a plain value from schema 1.
            if _plain_in_sealed(rows, carried):
                raise StoreError(PLAIN_IN_SEALED)
        else:
            rows = {t: [dict(zip(cols, r, strict=True)) for r in data]
                    for t, (cols, data) in carried.items()}
        # A crash after the commit still compacts at the next sound start.
        marked = mark_compaction_owed(self.path)
        # One transaction: SQLite DDL is transactional, so a crash part-way
        # leaves the old workspace exactly as it was.
        db.execute("BEGIN IMMEDIATE")
        try:
            for t in tables:
                db.execute(f'DROP TABLE IF EXISTS "{t}"')
            for statement in _statements(SCHEMA):
                db.execute(statement)
            db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            if blob is not None:
                db.execute("INSERT INTO crypto_meta (id, format, protector, root, check_value, "
                           "created_at) VALUES (1, 1, ?, ?, ?, ?)",
                           (self.protector.name, blob, keyring.check_value, time.time()))
            for t, entries in rows.items():
                keep = {r[1] for r in db.execute(f'PRAGMA table_info("{t}")')}
                for entry in entries:
                    names = [c for c in entry if c in keep]
                    if not names:
                        continue
                    db.execute(f'INSERT OR IGNORE INTO "{t}" ({", ".join(names)}) VALUES '
                               f'({", ".join("?" * len(names))})',
                               tuple(entry[c] for c in names))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            if marked:
                _remove_quietly(compaction_marker(self.path))
            raise
        try:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
            busy = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        except sqlite3.Error:
            busy = 1                              # owed: the marker stays for the next start
        if not busy:
            _remove_quietly(compaction_marker(self.path))
        return keyring

    def _unlock(self, keyring=None):
        """The workspace's keyring from crypto_meta, or KeyUnavailable. With
        keyring (made by this open) the stored key is checked against it and
        not unprotected again."""
        row = self.db.execute("SELECT format, protector, root, check_value FROM crypto_meta "
                              "WHERE id = 1").fetchone()
        if row is None:
            raise KeyUnavailable("missing")
        if row["protector"] != self.protector.name:
            raise KeyUnavailable("protector")
        if row["format"] != 1:
            raise KeyUnavailable("key")
        if keyring is None:
            try:
                root = self.protector.unprotect(row["root"])
            except DpapiError as exc:
                raise KeyUnavailable("windows", exc.code) from None
            except (vault.VaultError, ValueError, TypeError):
                raise KeyUnavailable("key") from None
            try:
                keyring = vault.Keyring(root)
            except (ValueError, TypeError):
                raise KeyUnavailable("key") from None
        if not keyring.matches(row["check_value"]):
            raise KeyUnavailable("key")
        return keyring

    def _lock_down(self, error):
        """Lock mode: open read-only, nothing sealed, opened or written."""
        self.locked = error
        self.codec = keystore.LockedCodec(error)
        self.db.execute("PRAGMA query_only=ON")

    def _load_keys(self):
        """Every meeting's wrapped text key, in one statement; each is
        unwrapped on first use (_text_sealer)."""
        self._sealers = {}
        self._wrapped = dict(self.db.execute(
            "SELECT meeting_id, wrapped FROM content_keys WHERE purpose = 'text'").fetchall())

    def _text_sealer(self, mid):
        """The vault.Sealer of a meeting's text key (the codec calls this
        under the store's lock). KeyMissing if the meeting has no key,
        DamagedContent if its wrapped key does not open."""
        tx = self._tx
        if tx is not None:
            if mid in tx.deleted:
                raise KeyMissing()
            if mid in tx.new:
                return tx.new[mid][1]
        sealer = self._sealers.get(mid)
        if sealer is not None:
            return sealer
        wrapped = self._wrapped.get(mid)
        if wrapped is None:
            raise KeyMissing()
        try:
            key = self.keyring.unwrap(wrapped, "text", mid)
        except vault.VaultError:
            raise DamagedContent() from None
        sealer = self._sealers[mid] = vault.Sealer(key)
        return sealer

    def close(self):
        with self._lock:
            self.db.close()
            self._sealers, self._wrapped = {}, {}    # Python cannot wipe the key bytes

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
            for rid, vid, mid in db.execute(
                    "SELECT r.id, r.version_id, v.meeting_id FROM speaker_runs r LEFT JOIN "
                    "versions v ON v.id = r.version_id WHERE r.status = 'running'").fetchall():
                note = (None if mid is None else
                        self._seal_note(mid, "speaker_runs", "error", (rid, vid),
                                        SPEAKER_RESTART_NOTE))
                db.execute("UPDATE speaker_runs SET status = 'interrupted', error_sealed = ? "
                           "WHERE id = ?", (note, rid))

    # -- content columns (CONTENT_COLUMNS) ---------------------------------------

    def _seal(self, meeting, table, column, row, value):
        """value sealed for its place: meeting (None for a workspace value),
        table, logical column and row tuple. Only under the store's lock."""
        if not self._lock.held():
            raise RuntimeError("store lock not held")
        if value is None:
            return None
        physical = CONTENT_COLUMNS[table][column]
        try:
            return self.codec.seal(meeting, table, physical, _row(row), value)
        except vault.VaultError:
            raise DamagedContent() from None

    def _unseal(self, meeting, table, column, row, value):
        """The value _seal stored there, or DamagedContent (KeyMissing when
        the meeting has no key). Only under the store's lock."""
        if not self._lock.held():
            raise RuntimeError("store lock not held")
        if value is None:
            return None
        physical = CONTENT_COLUMNS[table][column]
        try:
            return self.codec.unseal(meeting, table, physical, _row(row), value)
        except vault.VaultError:
            raise DamagedContent() from None

    def _seal_note(self, meeting, table, column, row, note):
        """A note the app writes itself (an error, a restart note): sealed, or
        NULL if it cannot be (its meeting's key is damaged). Saving the state
        it goes with never fails because of the note."""
        try:
            return self._seal(meeting, table, column, row, note)
        except DamagedContent:
            self._note_damage(table, column)
            return None

    @contextmanager
    def strict(self):
        """While open (on this thread), a value that does not open raises
        DamagedContent instead of reading as DAMAGED_TEXT: for every path
        that produces text for use outside the app or new content from old
        (the handoff, the reviewed snapshot, edits and speaker names)."""
        depth = getattr(self._strict, "depth", 0)
        self._strict.depth = depth + 1
        try:
            yield self
        finally:
            self._strict.depth = depth

    def _open(self, meeting, table, column, row, value):
        """(value, damaged): the opened value, or for display DAMAGED_TEXT
        (or an empty value for JSON text) and True when it does not open.
        Raises DamagedContent instead inside strict()."""
        try:
            return self._unseal(meeting, table, column, row, value), False
        except DamagedContent:
            if getattr(self._strict, "depth", 0):
                raise
            self._note_damage(table, column)
            return _DAMAGED_JSON.get((table, column), DAMAGED_TEXT), True

    def _opened(self, table, row, meeting, key):
        """A row as a dict with its content columns opened under their logical
        names; any that did not open are listed in "damaged"."""
        if row is None:
            return None
        out, damaged = dict(row), []
        for column, physical in CONTENT_COLUMNS.get(table, {}).items():
            if physical in out:
                value, bad = self._open(meeting, table, column, key, out.pop(physical))
                out[column] = value
                if bad:
                    damaged.append(column)
        if damaged:
            out["damaged"] = damaged
        return out

    def _note_damage(self, table, column):
        self._damage[(table, column)] = self._damage.get((table, column), 0) + 1

    def _take_damage(self):
        """At the lock's outermost release: one content-free diagnostics event
        per (table, column) that did not open since the last release (P3: no
        values, titles or paths), written after the lock is released."""
        if not self._damage:
            return None
        found, self._damage = self._damage, {}
        log = self.log

        def report():
            from workspace import diag
            target = log or diag._current
            if target is None:
                return
            for (table, column), count in sorted(found.items()):
                target.write("damaged_value", table=table, column=column, count=count)
        return report

    def fingerprint(self, purpose, sha256_hex):
        """The workspace's fingerprint of a SHA-256 (hex) for a purpose
        ('original', 'snapshot'): what is stored instead of the plain hash."""
        with self._lock:
            if self.locked is not None:
                raise self.locked
            return keystore.fingerprint(self.keyring, purpose, sha256_hex)

    def file_sealer(self, name):
        """A sealer for one of the workspace's own files (#111 C2c: name is
        "templates.sealed"), under the workspace key with the context
        ("workspace", "file", name). It has its own vault.Sealer and lock, so
        it is never shared with the store's (N4), and the key's bytes are not
        handed out. KeyUnavailable while locked."""
        if not isinstance(name, str) or not name:
            raise ValueError("file_sealer takes a file name")
        with self._lock:
            if self.locked is not None:
                raise self.locked
            return _FileSealer(self.keyring.workspace_key, name)

    def same_fingerprint(self, purpose, sha256_hex, fp):
        """Whether a SHA-256 (hex) has the stored fingerprint fp."""
        import hmac
        # compare_digest refuses a str that is not ASCII (a changed stored value).
        return isinstance(fp, str) and fp.isascii() and hmac.compare_digest(
            self.fingerprint(purpose, sha256_hex), fp)

    @contextmanager
    def tx(self):
        """One atomic transaction; any database error rolls everything back.
        Refused (KeyUnavailable) while locked: nothing is written then."""
        with self._lock:
            if self.locked is not None:
                raise self.locked
            try:
                self.db.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise StoreError(str(exc)) from exc
            self._tx = keys = _TxKeys()
            try:
                yield self.db
                self.db.execute("COMMIT")
                self.revision += 1
            except BaseException as exc:
                self._tx = None
                self._sealers = {}               # a rollback clears the key cache
                try:
                    self.db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if isinstance(exc, sqlite3.Error):
                    raise StoreError(str(exc)) from exc
                raise
            self._tx = None
            if keys.destroyed:
                # Keys were destroyed: the cache is cleared and the map loaded
                # again, so nothing destroyed stays usable.
                self._load_keys()
            else:
                for mid, (wrapped, sealer) in keys.new.items():
                    self._wrapped[mid], self._sealers[mid] = wrapped, sealer

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

    # -- keys (the erase points, #111 C3) -------------------------------------------

    def destroy_keys(self, db, mid, purposes):
        """Delete a meeting's keys for these purposes ('text', 'audio') inside
        the caller's transaction (db from tx()): whatever was sealed under
        them can no longer be opened. The key cache is reloaded after the
        commit."""
        if self._tx is None:
            raise RuntimeError("destroy_keys runs inside the store's transaction")
        purposes = tuple(purposes)
        db.execute(f"DELETE FROM content_keys WHERE meeting_id = ? AND purpose IN "
                   f"({', '.join('?' * len(purposes))})", (mid, *purposes))
        self._tx.destroyed = True
        if "text" in purposes:
            self._tx.deleted.add(mid)

    def meeting_key(self, mid, purpose):
        """A meeting's key for a purpose (bytes), unwrapped under the store's
        lock. KeyMissing if it has none (never made, or destroyed),
        DamagedContent if it does not open. Never creates a key."""
        with self._lock:
            if self.locked is not None:
                raise self.locked
            row = self._one("SELECT wrapped FROM content_keys WHERE meeting_id = ? AND "
                            "purpose = ?", (mid, purpose))
            if row is None:
                raise KeyMissing()
            try:
                return self.keyring.unwrap(row["wrapped"], purpose, mid)
            except vault.VaultError:
                raise DamagedContent() from None

    def create_recording_key(self, recording_id):
        """A new key for a microphone recording (C3b), stored wrapped before
        the recording starts. Returns the key."""
        key = vault.new_key()
        with self.tx() as db:
            db.execute("INSERT INTO recording_keys (recording_id, wrapped, created_at) "
                       "VALUES (?, ?, ?)",
                       (recording_id, self.keyring.wrap(key, "recording", recording_id),
                        time.time()))
        return key

    def recording_key(self, recording_id):
        """A recording's key, or KeyMissing / DamagedContent."""
        with self._lock:
            if self.locked is not None:
                raise self.locked
            row = self._one("SELECT wrapped FROM recording_keys WHERE recording_id = ?",
                            (recording_id,))
            if row is None:
                raise KeyMissing()
            try:
                return self.keyring.unwrap(row["wrapped"], "recording", recording_id)
            except vault.VaultError:
                raise DamagedContent() from None

    def meeting_recording_key(self, mid):
        """The id of the microphone recording a meeting came from while its
        recording key is still there (imported, first version not yet set
        up), or None. Refused while locked (#111 C4a)."""
        with self._lock:
            if self.locked is not None:
                raise self.locked
            row = self._one("SELECT r.recording_id FROM recording_sources r JOIN "
                            "recording_keys k ON k.recording_id = r.recording_id "
                            "WHERE r.meeting_id = ?", (mid,))
            return row[0] if row else None

    def recording_key_ids(self):
        """The ids of the recordings that have a key (plain; for the start-up
        sweep of keys whose recording folder is gone). Refused while locked:
        no key row is read or changed then (#111 C4a, design 7.2)."""
        with self._lock:
            if self.locked is not None:
                raise self.locked
            return [r["recording_id"] for r in self._all(
                "SELECT recording_id FROM recording_keys ORDER BY recording_id")]

    def destroy_recording_key(self, recording_id):
        """Delete a recording's key (secure_delete), then empty the write-ahead
        log (erase_checkpoint). Returns whether the log was emptied."""
        with self.tx() as db:
            db.execute("DELETE FROM recording_keys WHERE recording_id = ?", (recording_id,))
        return self.erase_checkpoint()

    ERASE_RETRIES_S = (0.05, 0.1, 0.2, 0.4)

    def erase_checkpoint(self):
        """Checkpoint the write-ahead log into the database and truncate it,
        so a destroyed key's old pages are not left in the log. A reader (for
        example --status or --health) can keep the checkpoint busy: tried
        again after 50, 100, 200 and 400 ms, without holding the store's lock
        while waiting. Returns whether it completed."""
        for pause in (0, *self.ERASE_RETRIES_S):
            if pause:
                time.sleep(pause)
            with self._lock:
                # No busy wait inside SQLite (the connection's own is 30 s):
                # the retries above are the wait.
                try:
                    self.db.execute("PRAGMA busy_timeout = 0")
                    busy = self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
                except sqlite3.Error:
                    busy = 1
                finally:
                    self.db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            if not busy:
                return True
        return False

    # -- meetings and sources ----------------------------------------------------

    def create_meeting(self, title):
        """A new meeting with its own text and audio keys (wrapped), all in
        one transaction."""
        mid, now = new_id(), time.time()
        text_key, audio_key = vault.new_key(), vault.new_key()
        with self.tx() as db:
            wrapped = self.keyring.wrap(text_key, "text", mid)
            self._tx.new[mid] = (wrapped, vault.Sealer(text_key))
            db.execute("INSERT INTO meetings (id, title_sealed, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?)",
                       (mid, self._seal(mid, "meetings", "title", (mid,), title), now, now))
            db.executemany("INSERT INTO content_keys (meeting_id, purpose, wrapped, created_at) "
                           "VALUES (?, ?, ?, ?)",
                           [(mid, "text", wrapped, now),
                            (mid, "audio", self.keyring.wrap(audio_key, "audio", mid), now)])
        return mid

    def delete_new_meeting(self, mid):
        """Remove a meeting that never got a source (e.g. a failed import).
        Like discard_unfinished_import, a meeting that has ever had a hold,
        active or released, is kept (StoreError; #193, invariant C2:
        releasing a hold deletes nothing, and a released hold's row is never
        left without its meeting). It stays listed as an import that did not
        finish, and only the user's Discard removes it."""
        with self.tx() as db:
            if db.execute("SELECT 1 FROM sources WHERE meeting_id = ?", (mid,)).fetchone():
                raise StoreError("Meeting has content; use cleanup (#17).")
            if db.execute("SELECT 1 FROM holds WHERE meeting_id = ?", (mid,)).fetchone():
                raise StoreError("A retention hold keeps this meeting.")
            self.delete_meeting_row(db, mid)

    # -- deleting a meeting: only here (tests/test_content_access.py checks that
    #    no other module deletes rows from a table with content) ----------------

    def delete_meeting_row(self, db, mid):
        """The meeting's keys, recording source and sources, and then its row,
        inside the caller's transaction (db from tx()). Every deletion of a
        meeting row comes here (the foreign keys refuse a meeting row whose
        keys remain). Refused (StoreError, so the caller's transaction rolls
        back) while an active hold covers the meeting: holds block every
        deletion (#18, invariant C2); callers check first with their own
        wording."""
        if db.execute("SELECT 1 FROM holds WHERE meeting_id = ? AND released_at IS NULL",
                      (mid,)).fetchone():
            raise StoreError("A retention hold keeps this meeting.")
        self.destroy_keys(db, mid, ("text", "audio"))
        db.execute("DELETE FROM recording_sources WHERE meeting_id = ?", (mid,))
        db.execute("DELETE FROM sources WHERE meeting_id = ?", (mid,))
        db.execute("DELETE FROM meetings WHERE id = ?", (mid,))

    def delete_meeting_content(self, db, mid):
        """Everything left of a meeting, inside the caller's transaction:
        transcript cleanup (cleanup._remove_transcript), after its hold check
        in the same transaction. Versions with their chunks, attempts,
        passages, edits and history, speaker passes with their turns,
        attributions and corrections; the reviewed snapshot, handoff options,
        released holds; then the keys, the sources and the meeting row."""
        for (vid,) in db.execute("SELECT id FROM versions WHERE meeting_id = ?",
                                 (mid,)).fetchall():
            for (rid,) in db.execute("SELECT id FROM speaker_runs WHERE version_id = ?",
                                     (vid,)).fetchall():
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
        db.execute("DELETE FROM holds WHERE meeting_id = ? AND released_at IS NOT NULL", (mid,))
        self.delete_meeting_row(db, mid)

    def remove_audio_keys(self, db, mid):
        """Audio cleanup (cleanup._remove_audio), inside the transaction that
        records it: the meeting's audio key and its recording source go; the
        text key stays for the transcript."""
        self.destroy_keys(db, mid, ("audio",))
        db.execute("DELETE FROM recording_sources WHERE meeting_id = ?", (mid,))

    def discard_unfinished_import(self, mid, remove_folder):
        """Remove a meeting whose import never completed (app._discard_import),
        in one transaction under the store's lock, so no hold can be added
        between the check and the removal. A meeting that has ever had a hold,
        active or released, or that has a version, is kept (#193, invariant
        C2: releasing a hold deletes nothing). remove_folder() removes the
        app's own copy of the audio inside the transaction: if it raises (a
        locked file), the rows are rolled back, so the meeting stays listed
        and the next start tries again; never a folder of decoded audio with
        no record of it. Returns whether it removed the meeting."""
        with self._lock, self.tx() as db:
            if (db.execute("SELECT 1 FROM holds WHERE meeting_id = ?", (mid,)).fetchone()
                    or db.execute("SELECT 1 FROM versions WHERE meeting_id = ?",
                                  (mid,)).fetchone()):
                return False
            self.delete_meeting_row(db, mid)
            remove_folder()
        return True

    # -- reading meetings ----------------------------------------------------------

    def _meeting(self, row):
        return self._opened("meetings", row, row["id"], (row["id"],)) if row is not None else None

    def meeting(self, mid):
        """A meeting with its title and note opened, or None. For a state
        check use meeting_state(), which opens nothing."""
        with self._lock:
            return self._meeting(self._one("SELECT * FROM meetings WHERE id = ?", (mid,)))

    def meeting_state(self, mid):
        """A meeting's plain columns (MEETING_STATE) as a dict, or None.
        Reads and opens no content."""
        row = self._one(f"SELECT {_STATE_COLUMNS} FROM meetings WHERE id = ?", (mid,))
        return dict(row) if row is not None else None

    def meetings(self, where="1", args=(), *, content=True):
        """Meetings matching a condition on their plain columns (a fixed SQL
        expression from the caller, never user text), oldest first.
        content: True for every column, opened; False for the plain columns
        only (MEETING_STATE), nothing opened; or a tuple of the content
        columns to read and open as well, for example ("title",)."""
        with self._lock:
            if content is True:
                return [self._meeting(r) for r in self._all(
                    f"SELECT * FROM meetings WHERE {plain_condition(where)} ORDER BY created_at",
                    args)]
            extra = tuple(content or ())
            if not set(extra) <= set(CONTENT_COLUMNS["meetings"]):
                raise ValueError("Only the meeting's content columns can be asked for.")
            columns = ", ".join(MEETING_STATE + tuple(CONTENT_COLUMNS["meetings"][c]
                                                      for c in extra))
            return [self._meeting(r) for r in self._all(
                f"SELECT {columns} FROM meetings WHERE {plain_condition(where)} "
                "ORDER BY created_at", args)]

    def rename_meeting(self, mid, title):
        with self.tx() as db:
            db.execute("UPDATE meetings SET title_sealed = ? WHERE id = ?",
                       (self._seal(mid, "meetings", "title", (mid,), title), mid))

    def add_source(self, mid, *, original_path, original_sha256, original_size,
                   original_mtime_ns, audio_id, samples, sample_rate, recording=None):
        """A meeting's source. original_sha256 (hex) is kept as its
        fingerprint (original_fp). audio_id: the decoded audio's SHA-256 (hex)
        until C3a. recording: (recording_id, info JSON text) for a microphone
        recording (#30, #111 C3b): its sealed recording_sources row is stored
        in the same transaction, so no second import can follow. Its recording
        key is kept until the meeting's first version is set up
        (ensure_version), so a failure in between can be tried again (the
        owner's decision, 22 Oct 2026)."""
        sid = new_id()
        with self.tx() as db:
            db.execute("INSERT INTO sources (id, meeting_id, original_path_sealed, original_fp, "
                       "original_size, original_mtime_ns, audio_id, samples, sample_rate, "
                       "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (sid, mid, self._seal(mid, "sources", "original_path", (sid,),
                                             original_path),
                        self.fingerprint("original", original_sha256), original_size,
                        original_mtime_ns, audio_id, samples, sample_rate, time.time()))
            if recording is not None:
                recording_id, info = recording
                db.execute("INSERT INTO recording_sources (meeting_id, recording_id, "
                           "info_sealed) VALUES (?, ?, ?)",
                           (mid, recording_id,
                            self._seal(mid, "recording_sources", "info", (mid,), info)))
        return sid

    def recording_imported(self, recording_id):
        """Whether a meeting was already made from this recording (C3b)."""
        return self._one("SELECT 1 FROM recording_sources WHERE recording_id = ?",
                         (recording_id,)) is not None

    def recording_info(self, mid):
        """The details of the recording a meeting was made from (JSON text),
        or None (no recording, or unreadable)."""
        with self._lock:
            row = self._one("SELECT info_sealed FROM recording_sources WHERE meeting_id = ?",
                            (mid,))
            if row is None:
                return None
            return self._open(mid, "recording_sources", "info", (mid,), row["info_sealed"])[0]

    def _source(self, row):
        return (self._opened("sources", row, row["meeting_id"], (row["id"],))
                if row is not None else None)

    def source(self, sid):
        with self._lock:
            return self._source(self._one("SELECT * FROM sources WHERE id = ?", (sid,)))

    def sources(self, mid):
        """A meeting's sources, oldest first."""
        with self._lock:
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
                db.execute("UPDATE sources SET original_path_sealed = NULL, intake_folder = ? "
                           "WHERE id = ?", (folder_id, sid))

    def set_paused(self, mid, paused):
        with self.tx() as db:
            db.execute("UPDATE meetings SET paused = ?, updated_at = ? WHERE id = ?",
                       (1 if paused else 0, time.time(), mid))

    def set_phase(self, mid, phase, error=None):
        """The meeting's phase and note. A note that cannot be sealed (the
        meeting's key is damaged) is left out: the phase is still saved."""
        with self.tx() as db:
            db.execute("UPDATE meetings SET phase = ?, last_error_sealed = ?, updated_at = ? "
                       "WHERE id = ?",
                       (phase, self._seal_note(mid, "meetings", "last_error", (mid,), error),
                        time.time(), mid))

    # -- versions and chunks -----------------------------------------------------

    def ensure_version(self, mid, sid, *, engine, model, model_revision, precision,
                       config, chunk_plan):
        """Return the active version for exactly these inputs, creating it (and
        superseding any other active version) if anything differs. The check
        and the creation are one transaction. chunk_plan is a list of
        (start, end, ctx_start, ctx_end) in samples.

        The transaction that creates a version also deletes the recording key
        of the microphone recording the meeting came from, if it is still
        there (#111 C3b): the meeting's first version finishes its set-up, and
        the recording's chunks are not needed after that. The caller then
        empties the write-ahead log (erase_checkpoint; app._plan_version)."""
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
            db.execute("DELETE FROM recording_keys WHERE recording_id IN (SELECT recording_id "
                       "FROM recording_sources WHERE meeting_id = ?)", (mid,))
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

    def ran_on(self, vid):
        """Where a version's committed parts were transcribed: one row per
        (backend, precision) the engines reported, with the number of parts,
        in the order each was first used (#248: a fallback from the graphics
        card to the processor shows as a second row)."""
        return self._all("SELECT backend, precision, COUNT(*) AS parts, MIN(id) AS first "
                         "FROM attempts WHERE version_id = ? AND result = 'committed' "
                         "GROUP BY backend, precision ORDER BY first", (vid,))

    def commit_chunk(self, vid, idx, segments, *, epoch=None, attempt_id=None,
                     backend=None, precision=None):
        """Atomically store a chunk's segments and mark it done. Refused (returns
        False, nothing written) if the chunk is already done, the version is no
        longer active, or `epoch` no longer matches the meeting's content epoch.
        A meeting whose key does not open raises DamagedContent, and nothing is
        written (the runner stops without counting a failure)."""
        with self.tx() as db:
            row = db.execute(
                "SELECT c.status, v.state, m.content_epoch, m.id AS mid FROM chunks c "
                "JOIN versions v ON v.id = c.version_id JOIN meetings m ON m.id = v.meeting_id "
                "WHERE c.version_id = ? AND c.idx = ?", (vid, idx)).fetchone()
            if (row is None or row["status"] == "done" or row["state"] != "active"
                    or (epoch is not None and epoch != row["content_epoch"])):
                return False
            # Ids are chosen here, not by SQLite, so each passage's text is
            # bound to its row before it is written; they never come back.
            ids = _next_ids(db, "segments", len(segments))
            mid, rows = row["mid"], []
            for sid, s in zip(ids, segments, strict=True):
                words = (json.dumps([list(w) for w in s.words])
                         if getattr(s, "words", ()) else None)
                rows.append((sid, vid, idx, s.start, s.end,
                             self._seal(mid, "segments", "text", (sid, vid), s.text),
                             self._seal(mid, "segments", "words_json", (sid, vid), words)))
            db.executemany(
                "INSERT INTO segments (id, version_id, chunk_idx, start_s, end_s, text_sealed, "
                "words_sealed) VALUES (?,?,?,?,?,?,?)", rows)
            db.execute("UPDATE chunks SET status = 'done', committed_at = ? "
                       "WHERE version_id = ? AND idx = ?", (time.time(), vid, idx))
            if attempt_id is not None:
                db.execute("UPDATE attempts SET result = 'committed', backend = ?, precision = ? "
                           "WHERE id = ?", (backend, precision, attempt_id))
        return True

    # -- transcript and edits ----------------------------------------------------

    def _passage(self, r):
        """{id, start_s, end_s, text, generated, edited, words_json} from a row
        with s.id, s.version_id, s.start_s, s.end_s, generated (s.text),
        edit (e.text), s.words_json and mid; the edit, where there is one, is
        the text. A part that does not open is DAMAGED_TEXT (no word timings),
        listed in "damaged"."""
        mid, sid, vid = r["mid"], r["id"], r["vid"]
        generated, bad_text = self._open(mid, "segments", "text", (sid, vid), r["generated"])
        edit, bad_edit = self._open(mid, "edits", "text", (sid,), r["edit"])
        words, bad_words = self._open(mid, "segments", "words_json", (sid, vid), r["words"])
        out = {"id": sid, "start_s": r["start_s"], "end_s": r["end_s"],
               "text": generated if edit is None else edit, "generated": generated,
               "edited": int(edit is not None), "words_json": words}
        damaged = [n for n, bad in (("text", bad_text), ("edit", bad_edit),
                                    ("words_json", bad_words)) if bad]
        if damaged:
            out["damaged"] = damaged
        return out

    _PASSAGES = ("SELECT s.id, s.version_id AS vid, s.start_s, s.end_s, "
                 "s.text_sealed AS generated, e.text_sealed AS edit, s.words_sealed AS words, "
                 "v.meeting_id AS mid FROM segments s "
                 "JOIN versions v ON v.id = s.version_id "
                 "LEFT JOIN edits e ON e.segment_id = s.id ")
    # The same without the word timings: they are neither read nor opened.
    _PASSAGES_TEXT = ("SELECT s.id, s.version_id AS vid, s.start_s, s.end_s, "
                      "s.text_sealed AS generated, e.text_sealed AS edit, NULL AS words, "
                      "v.meeting_id AS mid FROM segments s "
                      "JOIN versions v ON v.id = s.version_id "
                      "LEFT JOIN edits e ON e.segment_id = s.id ")
    _IN_ORDER = "WHERE s.version_id = ? ORDER BY s.start_s, s.id"

    def transcript(self, vid, words=True):
        """Segments in time order, with any human edit applied on top.
        words=False: words_json is None (search, the reviewed snapshot and the
        handoff use the text only)."""
        sql = (self._PASSAGES if words else self._PASSAGES_TEXT) + self._IN_ORDER
        with self._lock:
            return [self._passage(r) for r in self._all(sql, (vid,))]

    def passage_count(self, vid):
        """How many passages a version has."""
        return self._one("SELECT COUNT(*) FROM segments WHERE version_id = ?", (vid,))[0]

    def transcript_page(self, vid, start, count, words=True):
        """Passages start to start + count - 1 of transcript(vid), paged in
        SQL: only these rows are read and opened."""
        sql = (self._PASSAGES if words else self._PASSAGES_TEXT) + self._IN_ORDER
        with self._lock:
            return [self._passage(r) for r in self._all(
                sql + " LIMIT ? OFFSET ?", (vid, max(0, int(count)), max(0, int(start))))]

    def passage_ids(self, vid):
        """A version's passage ids in transcript order. Opens nothing."""
        return [r[0] for r in self._all(
            "SELECT s.id FROM segments s " + self._IN_ORDER, (vid,))]

    def passage(self, segment_id):
        """One passage (as transcript()), or None."""
        with self._lock:
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
            row = db.execute("SELECT r.version_id, v.meeting_id FROM speaker_runs r LEFT JOIN "
                             "versions v ON v.id = r.version_id WHERE r.id = ?",
                             (rid,)).fetchone()
            if row is None:
                return
            # No version row (a damaged database): no meeting to bind a note to.
            note = None if row[1] is None else self._seal_note(
                row[1], "speaker_runs", "error", (rid, row[0]), error)
            db.execute("UPDATE speaker_runs SET status = ?, error_sealed = ?, finished_at = ? "
                       "WHERE id = ? AND status = 'running'", (status, note, time.time(), rid))

    def _run(self, r):
        run = self._opened("speaker_runs", r, r["mid"], (r["id"], r["version_id"]))
        del run["mid"]
        return run

    def _runs(self, where, args):
        """Speaker runs with their error opened, oldest first per version."""
        with self._lock:
            return [self._run(r) for r in self._all(
                "SELECT r.*, v.meeting_id AS mid FROM speaker_runs r "
                "JOIN versions v ON v.id = r.version_id WHERE " + where +
                " ORDER BY r.started_at, r.rowid", args)]

    def speaker_runs(self, vid):
        """Oldest first: the last run is the meeting's speaker state. Start
        times are strictly increasing per version (_next_run_time); rowid only
        orders rows written before that."""
        return self._runs("r.version_id = ?", (vid,))

    def active_speaker_runs(self):
        """Every speaker run of every active version, oldest first."""
        return self._runs("v.state = 'active'", ())

    def current_run(self, vid):
        """The latest finished speaker run of a version (the last 'done' run
        in speaker_runs() order), or None. Opens that run's error only, which
        a finished run never has (end_speaker_run writes it for a failed or
        interrupted run)."""
        with self._lock:
            r = self._one("SELECT r.*, v.meeting_id AS mid FROM speaker_runs r "
                          "JOIN versions v ON v.id = r.version_id WHERE r.version_id = ? AND "
                          "r.status = 'done' ORDER BY r.started_at DESC, r.rowid DESC LIMIT 1",
                          (vid,))
            return None if r is None else self._run(r)

    # -- speaker corrections (#21) ----------------------------------------------------

    def _corrections(self, where, args):
        with self._lock:
            return [self._opened("speaker_corrections", r, r["mid"], (r["id"], r["run_id"]))
                    for r in self._all(
                        "SELECT c.id, c.run_id, c.kind, c.target, c.value_sealed, c.at, "
                        "v.meeting_id AS mid FROM speaker_corrections c JOIN speaker_runs r "
                        "ON r.id = c.run_id JOIN versions v ON v.id = r.version_id WHERE "
                        + where + " ORDER BY c.id", args)]

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
            [cid] = _next_ids(db, "speaker_corrections", 1)
            db.execute("INSERT INTO speaker_corrections (id, run_id, kind, target, value_sealed, "
                       "at) VALUES (?, ?, ?, ?, ?, ?)",
                       (cid, rid, kind, str(target),
                        self._seal(row[0], "speaker_corrections", "value", (cid, rid), value),
                        time.time()))

    def undo_correction(self, rid):
        """Remove a run's latest correction (speaker_names.undo). Returns its
        kind, or None if the run has none."""
        with self.tx() as db:
            last = db.execute("SELECT id, kind FROM speaker_corrections WHERE run_id = ? "
                              "ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
            if last is None:
                return None
            db.execute("DELETE FROM speaker_corrections WHERE id = ?", (last["id"],))
            return last["kind"]

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
        the edit. Returns False if nothing changed. A passage that does not
        open is refused (DamagedContent): an edit that cannot be read is never
        replaced (D1)."""
        with self.tx() as db, self.strict():
            # The text only: the word timings are not needed, so a damaged
            # one does not refuse an edit of readable text.
            seg = db.execute(self._PASSAGES_TEXT + "WHERE s.id = ?", (segment_id,)).fetchone()
            if seg is None:
                raise StoreError("That passage no longer exists.")
            passage = self._passage(seg)
            mid = seg["mid"]
            current = passage["text"] if passage["edited"] else None
            new = None if text == passage["generated"] else text
            if new == current:
                return False
            self._set_edit(db, mid, segment_id, new)
            [hid] = _next_ids(db, "edit_history", 1)
            db.execute("INSERT INTO edit_history (id, segment_id, before_sealed, after_sealed, "
                       "at) VALUES (?,?,?,?,?)",
                       (hid, segment_id,
                        self._seal(mid, "edit_history", "before", (hid, segment_id), current),
                        self._seal(mid, "edit_history", "after", (hid, segment_id), new),
                        time.time()))
        return True

    def _set_edit(self, db, mid, segment_id, text):
        if text is None:
            db.execute("DELETE FROM edits WHERE segment_id = ?", (segment_id,))
        else:
            db.execute("INSERT INTO edits (segment_id, text_sealed, edited_at) VALUES (?,?,?) "
                       "ON CONFLICT(segment_id) DO UPDATE SET text_sealed = excluded.text_sealed, "
                       "edited_at = excluded.edited_at",
                       (segment_id, self._seal(mid, "edits", "text", (segment_id,), text),
                        time.time()))

    def undo_edit(self, vid):
        """Undo the most recent edit in this version. Returns the segment id,
        or None when there is nothing to undo. Survives restart. A history
        entry that does not open (changed, or moved to another passage) is
        refused (DamagedContent) and nothing changes."""
        with self.tx() as db, self.strict():
            last = db.execute("SELECT h.id, h.segment_id, h.before_sealed, h.after_sealed, "
                              "v.meeting_id AS mid FROM edit_history h "
                              "JOIN segments s ON s.id = h.segment_id "
                              "JOIN versions v ON v.id = s.version_id WHERE s.version_id = ? "
                              "ORDER BY h.id DESC LIMIT 1", (vid,)).fetchone()
            if last is None:
                return None
            # Both sides are opened, so the entry is bound to its passage even
            # when one side is empty (a first edit has no "before"). save_edit
            # never records an entry with neither.
            if last["before_sealed"] is None and last["after_sealed"] is None:
                raise DamagedContent()
            where = (last["id"], last["segment_id"])
            before = self._unseal(last["mid"], "edit_history", "before", where,
                                  last["before_sealed"])
            self._unseal(last["mid"], "edit_history", "after", where, last["after_sealed"])
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
        """Ids of the passages containing query, in transcript order, at most
        limit: case-insensitive, in the edited text where there is an edit.
        One pass that opens each passage's shown text once (the edit, or the
        generated text when there is none) and never the word timings. A
        passage that does not open is skipped."""
        needle, out = query.casefold(), []
        if limit <= 0:
            return out
        with self._lock:
            for r in self._all("SELECT s.id, s.version_id AS vid, s.text_sealed AS generated, "
                               "e.text_sealed AS edit, v.meeting_id AS mid FROM segments s "
                               "JOIN versions v ON v.id = s.version_id "
                               "LEFT JOIN edits e ON e.segment_id = s.id " + self._IN_ORDER,
                               (vid,)):
                place = ("edits", "text") if r["edit"] is not None else ("segments", "text")
                try:
                    if r["edit"] is not None:
                        text = self._unseal(r["mid"], "edits", "text", (r["id"],), r["edit"])
                    else:
                        text = self._unseal(r["mid"], "segments", "text", (r["id"], r["vid"]),
                                            r["generated"])
                except DamagedContent:
                    self._note_damage(*place)
                    continue
                if needle in text.casefold():
                    out.append(r["id"])
                    if len(out) >= limit:
                        break
        return out

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
            db.execute("INSERT INTO holds (id, meeting_id, scope, reason_sealed, created_at, "
                       "reviewed_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (hid, mid, scope, self._seal(mid, "holds", "reason", (hid,), reason),
                        now, now))

    def holds(self, where="1", args=()):
        """Holds matching a condition on their plain columns (a fixed SQL
        expression from the caller), oldest first, with their reasons opened.
        A hold whose reason does not open still holds (C2)."""
        with self._lock:
            return [self._opened("holds", r, r["meeting_id"], (r["id"],)) for r in self._all(
                f"SELECT * FROM holds WHERE {plain_condition(where)} ORDER BY created_at",
                args)]

    def handoff_options(self, mid):
        """(omitted, redactions) as stored (JSON text), or None. For display,
        values that do not open read as "[]"; inside strict() they raise."""
        with self._lock:
            row = self._one("SELECT omitted_sealed, redactions_sealed FROM handoff_options "
                            "WHERE meeting_id = ?", (mid,))
            if row is None:
                return None
            return (self._open(mid, "handoff_options", "omitted", (mid,),
                               row["omitted_sealed"])[0],
                    self._open(mid, "handoff_options", "redactions", (mid,),
                               row["redactions_sealed"])[0])

    def set_handoff_options(self, mid, omitted, redactions):
        with self.tx() as db:
            db.execute("INSERT INTO handoff_options (meeting_id, omitted_sealed, "
                       "redactions_sealed) VALUES (?, ?, ?) ON CONFLICT(meeting_id) DO UPDATE "
                       "SET omitted_sealed = excluded.omitted_sealed, "
                       "redactions_sealed = excluded.redactions_sealed",
                       (mid, self._seal(mid, "handoff_options", "omitted", (mid,), omitted),
                        self._seal(mid, "handoff_options", "redactions", (mid,), redactions)))

    def put_snapshot(self, db, mid, text, sha256):
        """The reviewed transcript frozen at confirmation, written inside the
        caller's transaction (db from tx()). sha256 (hex) is kept as its
        fingerprint."""
        db.execute("INSERT OR REPLACE INTO reviewed_snapshots (meeting_id, text_sealed, text_fp, "
                   "created_at) VALUES (?, ?, ?, ?)",
                   (mid, self._seal(mid, "reviewed_snapshots", "text", (mid,), text),
                    self.fingerprint("snapshot", sha256), time.time()))

    def snapshot(self, mid):
        """The reviewed snapshot's text, or None."""
        with self._lock:
            row = self._one("SELECT text_sealed FROM reviewed_snapshots WHERE meeting_id = ?",
                            (mid,))
            if row is None:
                return None
            return self._open(mid, "reviewed_snapshots", "text", (mid,), row["text_sealed"])[0]

    # -- watched folders (workspace/intake.py keeps the rules): workspace-wide
    #    values, so meeting is None --------------------------------------------------

    def add_folder(self, fid, path, include_existing):
        """False if the folder is already watched (the same folder identity,
        keystore.stored_folder_identity of the resolved path: path_fp is
        UNIQUE); any other failure raises StoreError as usual."""
        identity = keystore.stored_folder_identity(path)
        try:
            with self.tx() as db:
                db.execute("INSERT INTO watched_folders (id, path_sealed, path_fp, created_at, "
                           "include_existing) VALUES (?, ?, ?, ?, ?)",
                           (fid, self._seal(None, "watched_folders", "path", (fid,), path),
                            keystore.folder_fingerprint(self.keyring, identity),
                            time.time(), 1 if include_existing else 0))
        except StoreError as exc:
            if isinstance(exc.__cause__, sqlite3.IntegrityError):
                return False
            raise
        return True

    def folders(self, where="1", args=()):
        """Watched folders matching a condition on their plain columns, oldest
        first, with their paths opened (a path that does not open is
        DAMAGED_TEXT, listed in "damaged")."""
        with self._lock:
            return [self._opened("watched_folders", r, None, (r["id"],)) for r in self._all(
                f"SELECT * FROM watched_folders WHERE {plain_condition(where)} "
                "ORDER BY created_at", args)]

    def remove_folder(self, fid):
        """Stop watching a folder: its row and its suppression rows, in one
        transaction (intake.remove_folder). Imported meetings stay."""
        with self.tx() as db:
            db.execute("DELETE FROM intake_seen WHERE folder_id = ?", (fid,))
            db.execute("DELETE FROM watched_folders WHERE id = ?", (fid,))

    def intake_salt(self):
        """(salt, restarted): the key for intake path hashes (intake._key), as
        bytes, sealed under the workspace key and made once, in one
        transaction, the first time it is needed. If it cannot be read, or is
        missing while files are recorded under it, a new one is made in the
        same transaction as clearing every folder's record of its files
        (baseline_done and include_existing off, with a note), so the next
        scan records what is there and imports nothing it already had; never
        a new salt alone, which would make every file look new. restarted
        says whether that happened."""
        with self._lock:
            row = self._one("SELECT v_sealed FROM intake_meta WHERE k = 'salt'")
            if row is not None:
                try:
                    return self._unseal(None, "intake_meta", "v", ("salt",),
                                        row["v_sealed"]).encode(), False
                except DamagedContent:
                    self._note_damage("intake_meta", "v")
            elif self._one("SELECT 1 FROM intake_seen LIMIT 1") is None:
                with self.tx() as db:
                    salt = vault.new_key()[:16].hex()
                    db.execute("INSERT INTO intake_meta (k, v_sealed) VALUES ('salt', ?)",
                               (self._seal(None, "intake_meta", "v", ("salt",), salt),))
                return salt.encode(), False
            with self.tx() as db:
                salt = vault.new_key()[:16].hex()
                db.execute("INSERT OR REPLACE INTO intake_meta (k, v_sealed) VALUES ('salt', ?)",
                           (self._seal(None, "intake_meta", "v", ("salt",), salt),))
                db.execute("DELETE FROM intake_seen")
                db.execute("UPDATE watched_folders SET baseline_done = 0, include_existing = 0, "
                           "last_error = ?", (SALT_RESTARTED_NOTE,))
            return salt.encode(), True


SALT_RESTARTED_NOTE = (
    "Unminute could not read its record of the files in this folder and has started a new one. "
    "Recordings added since the folder was last checked may not have been imported: use Import "
    "to add any that are missing.")
