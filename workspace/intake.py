# workspace/intake.py
# Watched folders: automatic intake with reconciliation and duplicate
# detection (#23).
#
# Watching is explicit: the user adds a folder, and chooses whether recordings
# already in it are imported too. The app then reconciles each folder by
# scanning it (at launch, every few minutes while open, and on wake from
# sleep): a fully closed app simply catches up on its next launch. Polling was
# chosen over change notifications because notifications are lost while the
# app is closed and are unreliable on synced folders; a reconciliation scan is
# needed anyway, and a bounded folder makes it cheap (see LIMITS below).
#
# A file counts as new by its identity (path, file ID, size and modification
# time), never by its date, so an old recording copied in today is picked up.
# It is imported only once its size and time have been stable across two
# scans (still-copying files wait). Content is hashed once per new identity,
# never on every scan.
#
# Duplicates: automatic intake skips content that was already imported (from
# any folder or by hand) within the suppression window. A replacement saved
# over the same path with different content is imported. Importing by hand
# always works, so intentional reprocessing is never blocked.
#
# Suppression data is operational, bounded and holds no names:
#   intake_seen     folder id, a keyed hash of the path, size, mtime, file id,
#                   state; rows go when the file leaves the folder.
#   intake_content  SHA-256 of imported content and when; kept SUPPRESS_DAYS
#                   (and at most SUPPRESS_MAX rows), then forgotten.
# Neither is meeting content; both are documented in docs/WORKSPACE_DESIGN.md.

import hmac
import os
import secrets
import time
from pathlib import Path

from workspace.safe_paths import UnsafePathError, check_raw, validate_input_file

SCAN_INTERVAL_S = 300          # while the app is open
STABLE_S = 5                   # a file must look the same this long apart (two scans)
MAX_DEPTH = 2                  # subfolder levels below a watched folder
MAX_FILES = 5000               # per folder per scan: a bounded folder, not a drive
SUPPRESS_DAYS = 120
SUPPRESS_MAX = 20000

class IntakeError(ValueError):
    """A refused folder (message is safe to show)."""


def _salt(store):
    row = store._one("SELECT v FROM intake_meta WHERE k = 'salt'")
    if row:
        return row["v"].encode()
    salt = secrets.token_hex(16)
    with store.tx() as db:
        db.execute("INSERT OR IGNORE INTO intake_meta VALUES ('salt', ?)", (salt,))
    return _salt(store)


def _key(salt, path):
    """A keyed hash of the path: identifies "the same place" without storing names."""
    return hmac.new(salt, os.path.normcase(str(path)).encode("utf-8"), "sha256").hexdigest()


def _broad(path):
    """Folders too broad to watch: a drive root, the user profile itself,
    or the Windows and program folders."""
    p = Path(os.path.normcase(str(path))).resolve()
    if p == Path(p.anchor):
        return True
    for name in ("USERPROFILE", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)"):
        v = os.environ.get(name)
        if v and p == Path(os.path.normcase(v)).resolve():
            return True
    return False


def add_folder(store, path, include_existing=False):
    """Start watching a folder chosen in the native folder dialog."""
    try:
        if os.name == "nt":
            check_raw(path)
    except UnsafePathError as exc:
        raise IntakeError(str(exc)) from exc
    p = Path(path)
    if not p.is_dir():
        raise IntakeError("That folder could not be found.")
    real = os.path.realpath(p)
    if _broad(real):
        raise IntakeError("Choose a specific folder for recordings, not a whole drive or "
                          "your whole user folder.")
    from workspace.store import new_id
    fid = new_id()
    try:
        with store.tx() as db:
            db.execute("INSERT INTO watched_folders (id, path, created_at, include_existing) "
                       "VALUES (?, ?, ?, ?)", (fid, real, time.time(),
                                               1 if include_existing else 0))
    except Exception as exc:
        raise IntakeError("That folder is already watched.") from exc
    return fid


def folders(store):
    return store._all("SELECT * FROM watched_folders ORDER BY created_at")


def set_active(store, fid, active):
    with store.tx() as db:
        db.execute("UPDATE watched_folders SET active = ? WHERE id = ?", (1 if active else 0, fid))


def set_delete_originals(store, fid, allowed):
    """Authority to delete reviewed originals from this folder (#24). Separate
    from watching (import authority); off by default; revocable at any time."""
    with store.tx() as db:
        db.execute("UPDATE watched_folders SET delete_originals = ? WHERE id = ?",
                   (1 if allowed else 0, fid))


def set_fetch_cloud(store, fid, allowed):
    """Read cloud-only files so the sync client downloads them (#24)."""
    with store.tx() as db:
        db.execute("UPDATE watched_folders SET fetch_cloud = ? WHERE id = ?",
                   (1 if allowed else 0, fid))


def deletion_roots(store):
    """Folders the user has authorised originals to be deleted from, now.
    Cleanup re-checks this at deletion time, so revoking takes effect at once."""
    return [r["path"] for r in store._all(
        "SELECT path FROM watched_folders WHERE delete_originals = 1")]


def authority_for(store, fid):
    row = store._one("SELECT delete_originals FROM watched_folders WHERE id = ?", (fid,))
    return bool(row and row["delete_originals"])


def remove_folder(store, fid):
    """Stop watching. Imported meetings stay; this folder's suppression rows go."""
    with store.tx() as db:
        db.execute("DELETE FROM intake_seen WHERE folder_id = ?", (fid,))
        db.execute("DELETE FROM watched_folders WHERE id = ?", (fid,))


def _walk(root, extensions):
    """Files with a recording extension, at most MAX_DEPTH levels down."""
    out, stack = [], [(Path(root), 0)]
    while stack and len(out) < MAX_FILES:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < MAX_DEPTH:
                        stack.append((Path(e.path), depth + 1))
                elif e.is_file(follow_symlinks=False) and \
                        os.path.splitext(e.name)[1].lower() in extensions:
                    out.append(Path(e.path))
            except OSError:
                continue
    return out


def already_imported(store, content_sha):
    """Content the workspace already has, or imported within the suppression window."""
    if store._one("SELECT 1 FROM intake_content WHERE sha256 = ?", (content_sha,)):
        return True
    return store._one("SELECT 1 FROM sources WHERE original_sha256 = ?",
                      (content_sha,)) is not None


def remember_content(store, content_sha, now=None):
    with store.tx() as db:
        db.execute("INSERT OR REPLACE INTO intake_content VALUES (?, ?)",
                   (content_sha, now or time.time()))


def purge(store, now=None):
    """Keep suppression data bounded."""
    now = now or time.time()
    with store.tx() as db:
        db.execute("DELETE FROM intake_content WHERE imported_at < ?",
                   (now - SUPPRESS_DAYS * 86400,))
        db.execute("DELETE FROM intake_content WHERE sha256 IN (SELECT sha256 FROM "
                   "intake_content ORDER BY imported_at DESC LIMIT -1 OFFSET ?)",
                   (SUPPRESS_MAX,))


def scan(store, extensions, max_bytes, enqueue, now=None, hasher=None):
    """Reconcile every active folder once. `enqueue(path, folder_id)` queues an
    import. Returns {"queued", "waiting", "duplicates", "cloud", "errors"}."""
    from workspace.media import sha256_file
    hasher = hasher or sha256_file
    now = now or time.time()
    salt = _salt(store)
    stats = {"queued": 0, "waiting": 0, "duplicates": 0, "cloud": 0, "errors": 0}
    for f in folders(store):
        if not f["active"]:
            continue
        root = Path(f["path"])
        if not root.is_dir():
            _folder_note(store, f["id"], now, "The folder is not available (disconnected, "
                                              "renamed or offline). It will be checked again.")
            stats["errors"] += 1
            continue
        baseline = not f["baseline_done"] and not f["include_existing"]
        seen = {r["key"]: r for r in store._all(
            "SELECT * FROM intake_seen WHERE folder_id = ?", (f["id"],))}
        present = set()
        for path in _walk(root, extensions):
            try:
                st = path.stat()
            except OSError:
                continue
            key = _key(salt, path)
            present.add(key)
            ident = (st.st_size, st.st_mtime_ns, getattr(st, "st_ino", 0))
            row = seen.get(key)
            if row is None:
                state = "baseline" if baseline else "waiting"
                _put(store, f["id"], key, ident, state, now, first=True)
                if state == "waiting":
                    stats["waiting"] += 1
                continue
            same = (row["size"], row["mtime_ns"], row["file_id"]) == ident
            if not same:
                # Still copying, or replaced at the same path: wait for it to settle.
                _put(store, f["id"], key, ident, "waiting", now)
                stats["waiting"] += 1
                continue
            if row["state"] not in ("waiting", "cloud"):
                _touch(store, f["id"], key, now)
                continue
            if now - row["last_seen"] < STABLE_S:
                stats["waiting"] += 1
                continue
            try:
                real = validate_input_file(path, extensions, max_bytes,
                                           allow_cloud=bool(f["fetch_cloud"]))
            except UnsafePathError as exc:
                cloud = "cloud" in str(exc)
                _put(store, f["id"], key, ident, "cloud" if cloud else "refused", now)
                stats["cloud" if cloud else "errors"] += 1
                continue
            try:
                # Read-only: reading a cloud-only file (when allowed) makes the
                # sync client fetch it; no write access is ever needed.
                content = hasher(real)
            except PermissionError:
                _put(store, f["id"], key, ident, "unreadable", now)
                stats["errors"] += 1
                continue
            except OSError:
                # e.g. paused sync or offline: the file is checked again next scan.
                _put(store, f["id"], key, ident, "cloud", now)
                stats["cloud"] += 1
                continue
            if already_imported(store, content):
                _put(store, f["id"], key, ident, "duplicate", now)
                stats["duplicates"] += 1
                continue
            remember_content(store, content, now)
            _put(store, f["id"], key, ident, "queued", now)
            enqueue(real, f["id"])
            stats["queued"] += 1
        with store.tx() as db:
            # Files that left the folder: forget their rows (bounded).
            gone = [k for k in seen if k not in present]
            db.executemany("DELETE FROM intake_seen WHERE folder_id = ? AND key = ?",
                           [(f["id"], k) for k in gone])
            db.execute("UPDATE watched_folders SET last_scan_at = ?, last_error = NULL, "
                       "baseline_done = 1 WHERE id = ?", (now, f["id"]))
    purge(store, now)
    return stats


def _put(store, fid, key, ident, state, now, first=False):
    with store.tx() as db:
        db.execute("INSERT INTO intake_seen (folder_id, key, size, mtime_ns, file_id, state, "
                   "first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(folder_id, key) "
                   "DO UPDATE SET size = excluded.size, mtime_ns = excluded.mtime_ns, "
                   "file_id = excluded.file_id, state = excluded.state, "
                   "last_seen = excluded.last_seen", (fid, key, *ident, state, now, now))


def _touch(store, fid, key, now):
    with store.tx() as db:
        db.execute("UPDATE intake_seen SET last_seen = ? WHERE folder_id = ? AND key = ?",
                   (now, fid, key))


def _folder_note(store, fid, now, message):
    with store.tx() as db:
        db.execute("UPDATE watched_folders SET last_scan_at = ?, last_error = ? WHERE id = ?",
                   (now, message, fid))


def summary(store):
    out = []
    for f in folders(store):
        counts = {r["state"]: r["n"] for r in store._all(
            "SELECT state, COUNT(*) AS n FROM intake_seen WHERE folder_id = ? GROUP BY state",
            (f["id"],))}
        out.append({"id": f["id"], "path": f["path"], "active": bool(f["active"]),
                    "delete_originals": bool(f["delete_originals"]),
                    "fetch_cloud": bool(f["fetch_cloud"]),
                    "unreadable": counts.get("unreadable", 0),
                    "last_scan_at": f["last_scan_at"], "error": f["last_error"],
                    "waiting": counts.get("waiting", 0), "cloud": counts.get("cloud", 0),
                    "imported": counts.get("queued", 0),
                    "duplicates": counts.get("duplicate", 0)})
    return out
