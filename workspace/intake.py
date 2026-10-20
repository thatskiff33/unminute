# workspace/intake.py
# Watched folders: automatic intake with reconciliation and duplicate
# detection (#23).
#
# Watching is explicit: the user adds a folder, and chooses whether recordings
# already in it are imported too. The app then reconciles each folder by
# scanning it (soon after launch, every few minutes while open, and on wake
# from sleep): a fully closed app simply catches up on its next launch.
# Polling was chosen over change notifications because notifications are lost
# while the app is closed and are unreliable on synced folders; a
# reconciliation scan is needed anyway, and a bounded folder makes it cheap
# (see LIMITS below).
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
#   intake_seen     folder id, a keyed hash of the path (its salt is sealed,
#                   store.intake_salt), size, mtime, file id, state; rows go
#                   when the file leaves the folder.
#   intake_content  the workspace's fingerprint (HMAC, #111 C2b-2) of imported
#                   content and when; kept SUPPRESS_DAYS (and at most
#                   SUPPRESS_MAX rows), then forgotten.
# Neither is meeting content; both are documented in docs/WORKSPACE_DESIGN.md.
# Content is compared by fingerprint, never by its plain SHA-256: the hash a
# scan computes becomes a fingerprint at once (store.fingerprint).

import hmac
import os
import time
from pathlib import Path

from workspace import keystore
from workspace.safe_paths import (CloudPlaceholderError, UnsafePathError, check_raw,
                                  validate_input_file)
from workspace.store import SALT_RESTARTED_NOTE

SCAN_INTERVAL_S = 300          # while the app is open
LAUNCH_SCAN_DELAY_S = 30       # the first scan waits for start-up to finish
STABLE_S = 5                   # a file must look the same this long apart (two scans)
STABLE_RECENT_S = 60           # ...and not have been written in the last minute
MAX_DEPTH = 2                  # subfolder levels below a watched folder
MAX_FILES = 5000               # per folder per scan: a bounded folder, not a drive
SUPPRESS_DAYS = 120
SUPPRESS_MAX = 20000

class IntakeError(ValueError):
    """A refused folder (message is safe to show)."""


def _salt(store):
    """(salt, restarted): see store.intake_salt."""
    return store.intake_salt()


def folder_identity(path):
    """A watched folder's identity: its real path (junctions, links and short
    names resolved), compared as Windows does (ntpath, so Linux CI agrees),
    with no trailing separator. C:\\Rec, c:\\rec\\ and a junction to it are
    the same folder. A drive root cannot reach this (_broad refuses it).
    One definition: keystore.stored_folder_identity of the resolved path,
    which store.add_folder fingerprints (add_folder gives it that path) and
    the schema 1 to 2 migration uses for paths resolved when added."""
    return keystore.stored_folder_identity(os.path.realpath(path))


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
    try:
        if os.name == "nt":
            check_raw(real)          # a mapped network drive resolves to \\server\share
    except UnsafePathError as exc:
        raise IntakeError("Network folders cannot be watched. Choose a folder on this "
                          "computer (synced folders such as OneDrive are fine).") from exc
    if _broad(real):
        raise IntakeError("Choose a specific folder for recordings, not a whole drive or "
                          "your whole user folder.")
    from workspace.store import new_id
    fid = new_id()
    if not store.add_folder(fid, real, include_existing):
        raise IntakeError("That folder is already watched.")
    return fid


def folders(store):
    return store.folders()


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


def deletion_roots(store, mid):
    """For one meeting: the folder its original came from, if that folder
    still has deletion authority right now. Authority belongs to the folder a
    recording was imported from, never to another (e.g. a parent) folder, and
    revoking it keeps the original. Paused folders keep their authority."""
    came_from = {s["intake_folder"] for s in store.sources(mid) if s["intake_folder"]}
    # A folder whose path cannot be read is never a deletion root.
    return [f["path"] for f in store.folders("delete_originals = 1")
            if f["id"] in came_from and not f.get("damaged")]


def authority_for(store, fid):
    row = store._one("SELECT delete_originals FROM watched_folders WHERE id = ?", (fid,))
    return bool(row and row["delete_originals"])


def remove_folder(store, fid):
    """Stop watching. Imported meetings stay; this folder's suppression rows go."""
    store.remove_folder(fid)


def _walk(root, extensions, stop=None):
    """(files, complete): files with a recording extension, at most MAX_DEPTH
    levels down, never following links or junctions. complete is False if any
    folder could not be read or MAX_FILES was reached: the caller must not
    treat files it did not see as gone."""
    out, stack, complete = [], [(Path(root), 0)], True
    while stack:
        if len(out) >= MAX_FILES or (stop and stop()):
            return out, False
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            complete = False
            continue
        for e in entries:
            try:
                if e.is_symlink() or e.is_junction():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if depth < MAX_DEPTH:
                        stack.append((Path(e.path), depth + 1))
                elif e.is_file(follow_symlinks=False) and \
                        os.path.splitext(e.name)[1].lower() in extensions:
                    out.append(Path(e.path))
            except OSError:
                complete = False
                continue
    return out, complete


def already_imported(store, content_fp):
    """Content the workspace already has (by fingerprint): imported within
    the suppression window, from any folder or by hand."""
    if store._one("SELECT 1 FROM intake_content WHERE fp = ?", (content_fp,)):
        return True
    return store._one("SELECT 1 FROM sources WHERE original_fp = ?",
                      (content_fp,)) is not None


def importing_elsewhere(store, content_fp):
    """The same content is lined up for import from another file. Not yet
    imported: that import may still be held (its folder paused) or fail."""
    return store._one("SELECT 1 FROM intake_seen WHERE state = 'importing' AND content_fp = ?",
                      (content_fp,)) is not None


def remember_content(store, content_fp, now=None):
    with store.tx() as db:
        db.execute("INSERT OR REPLACE INTO intake_content (fp, imported_at) VALUES (?, ?)",
                   (content_fp, now or time.time()))


def import_finished(store, fid, key, content_fp, ok, now=None):
    """Called by the app after an import from a watched folder. Content is
    suppressed only once it was really imported; a failed import is shown and
    tried again if the file changes. A successful import ends the folder's
    restart note (store.intake_salt)."""
    with store.tx() as db:
        db.execute("UPDATE intake_seen SET state = ?, content_fp = NULL WHERE folder_id = ? "
                   "AND key = ?", ("imported" if ok else "failed", fid, key))
        if ok:
            row = db.execute("SELECT last_error FROM watched_folders WHERE id = ?",
                             (fid,)).fetchone()
            if row is not None and _has_restart_note(row[0]):
                rest = row[0][:-len(SALT_RESTARTED_NOTE)].strip() or None
                db.execute("UPDATE watched_folders SET last_error = ? WHERE id = ?",
                           (rest, fid))
    if ok:
        remember_content(store, content_fp, now)


def _has_restart_note(note):
    return isinstance(note, str) and note.endswith(SALT_RESTARTED_NOTE)


def _set_note(db, fid, now, note, done=False):
    """A folder's note after a scan. The restart note (store.intake_salt)
    stays until the folder's next successful import (import_finished), so a
    later scan never clears it: any other note comes first, then it. done:
    the walk was complete, so the baseline is done too."""
    row = db.execute("SELECT last_error FROM watched_folders WHERE id = ?", (fid,)).fetchone()
    if row is not None and _has_restart_note(row[0]):
        note = SALT_RESTARTED_NOTE if note is None else f"{note} {SALT_RESTARTED_NOTE}"
    if done:
        db.execute("UPDATE watched_folders SET last_scan_at = ?, last_error = ?, "
                   "baseline_done = 1 WHERE id = ?", (now, note, fid))
    else:
        db.execute("UPDATE watched_folders SET last_scan_at = ?, last_error = ? WHERE id = ?",
                   (now, note, fid))


def watching(store, fid):
    """The folder is still watched and turned on."""
    row = store._one("SELECT active FROM watched_folders WHERE id = ?", (fid,))
    return bool(row and row["active"])


def import_held(store, fid, key):
    """An import lined up before its folder was paused (or turned off by
    policy) waits: back to waiting, so the first scan after the folder is
    turned on again imports it (#152). Nothing if the folder was removed."""
    with store.tx() as db:
        db.execute("UPDATE intake_seen SET state = 'waiting', content_fp = NULL WHERE "
                   "folder_id = ? AND key = ? AND state = 'importing'", (fid, key))


def recover(store):
    """At startup: imports that were queued when the app closed are found again."""
    with store.tx() as db:
        db.execute("UPDATE intake_seen SET state = 'waiting', content_fp = NULL "
                   "WHERE state = 'importing'")


def purge(store, now=None):
    """Keep suppression data bounded."""
    now = now or time.time()
    with store.tx() as db:
        db.execute("DELETE FROM intake_content WHERE imported_at < ?",
                   (now - SUPPRESS_DAYS * 86400,))
        db.execute("DELETE FROM intake_content WHERE fp IN (SELECT fp FROM "
                   "intake_content ORDER BY imported_at DESC LIMIT -1 OFFSET ?)",
                   (SUPPRESS_MAX,))


RETRY_STATES = ("waiting", "cloud", "unreadable")


def _same(row, ident):
    """Same file? Compare the file ID only where the filesystem provides a
    stable one (0 on some network and cloud filesystems)."""
    size, mtime, fid = ident
    if row["file_id"] and fid:
        return (row["size"], row["mtime_ns"], row["file_id"]) == ident
    return (row["size"], row["mtime_ns"]) == (size, mtime)


def scan(store, extensions, max_bytes, enqueue, now=None, hasher=None, stop=None):
    """Reconcile every active folder once. `enqueue(path, folder_id, key,
    content_fp)` queues an import; the app reports back with import_finished().
    hasher(path) returns the file's SHA-256 (hex). Returns {"queued",
    "waiting", "duplicates", "cloud", "errors"}, and "notice" (fixed text)
    when the record of the files seen had to be started again
    (store.intake_salt): each folder's note says so too, until that folder's
    next successful import (import_finished)."""
    from workspace.media import sha256_file
    hasher = hasher or sha256_file
    now = now or time.time()
    salt, restarted = _salt(store)
    stats = {"queued": 0, "waiting": 0, "duplicates": 0, "cloud": 0, "errors": 0}
    if restarted:
        stats["notice"] = SALT_RESTARTED_NOTE
    for f in folders(store):
        if stop and stop():
            break
        if not f["active"]:
            continue
        if f.get("damaged"):
            # Its location cannot be read: never scanned, never a deletion root.
            _folder_note(store, f["id"], now, FOLDER_UNREADABLE_NOTE)
            stats["errors"] += 1
            continue
        root = Path(f["path"])
        if not root.is_dir():
            _folder_note(store, f["id"], now, "The folder is not available (disconnected, "
                                              "renamed or offline). It will be checked again.")
            stats["errors"] += 1
            continue
        files, complete = _walk(root, extensions, stop)
        if stop and stop():
            break                        # closing or turned off, not unreadable: no note
        baseline = not f["baseline_done"] and not f["include_existing"]
        if baseline and not complete:
            # Never record a partial baseline: files it missed would later look new.
            _folder_note(store, f["id"], now, "The folder could not be read completely. It "
                                              "will be checked again.")
            stats["errors"] += 1
            continue
        seen = {r["key"]: r for r in store._all(
            "SELECT * FROM intake_seen WHERE folder_id = ?", (f["id"],))}
        present = set()
        stats_files = []
        for path in files:
            try:
                st = path.stat()
            except OSError:
                continue
            key = _key(salt, path)
            present.add(key)
            stats_files.append((path, key, (st.st_size, st.st_mtime_ns,
                                            getattr(st, "st_ino", 0))))
        # Moves and renames inside the folder keep their state (same file ID
        # and size), so an old recording renamed today is not "new".
        missing = {(r["file_id"], r["size"]): r for k, r in seen.items()
                   if k not in present and r["file_id"]}
        # Row changes wait in `writes` and are committed together: one
        # transaction per folder when nothing is new, not one per file. They
        # are committed early only before an import is queued, because the
        # import updates its row when it finishes. No transaction is held
        # while a file is checked or hashed.
        writes, stopped, held = [], False, False
        for path, key, ident in stats_files:
            if stop and stop():
                stopped = True
                break
            row = seen.get(key)
            if row is None:
                moved = missing.pop((ident[2], ident[0]), None) if ident[2] else None
                if moved is not None:
                    _put(writes, f["id"], key, ident, moved["state"], now)
                    continue
                state = "baseline" if baseline else "waiting"
                _put(writes, f["id"], key, ident, state, now)
                if state == "waiting":
                    stats["waiting"] += 1
                continue
            if not _same(row, ident):
                # Still copying, or replaced at the same path: wait for it to
                # settle, then compare content (a new recording is imported).
                _put(writes, f["id"], key, ident, "waiting", now)
                stats["waiting"] += 1
                continue
            if row["state"] not in RETRY_STATES:
                _touch(writes, f["id"], key, now)
                continue
            # Stable: unchanged since the previous scan, and not written recently
            # (a stalled copy is not mistaken for a finished one).
            if now - row["last_seen"] < STABLE_S or now - ident[1] / 1e9 < STABLE_RECENT_S:
                stats["waiting"] += 1
                continue
            try:
                os.stat(path)
                real = validate_input_file(path, extensions, max_bytes,
                                           allow_cloud=bool(f["fetch_cloud"]))
            except FileNotFoundError:
                continue                 # gone since the walk: its row goes next scan
            except PermissionError:
                _put(writes, f["id"], key, ident, "unreadable", now)
                stats["errors"] += 1
                continue
            except CloudPlaceholderError:
                _put(writes, f["id"], key, ident, "cloud", now)
                stats["cloud"] += 1
                continue
            except UnsafePathError:
                _put(writes, f["id"], key, ident, "refused", now)
                stats["errors"] += 1
                continue
            if not watching(store, f["id"]):
                held = True              # paused or removed meanwhile: nothing more from it
                break
            try:
                # Read-only: reading a cloud-only file (when allowed) makes the
                # sync client fetch it; no write access is ever needed.
                content = store.fingerprint("original", hasher(real))
            except PermissionError:
                _put(writes, f["id"], key, ident, "unreadable", now)
                stats["errors"] += 1
                continue
            except OSError:
                # e.g. paused sync or offline: the file is checked again next scan.
                _put(writes, f["id"], key, ident, "cloud", now)
                stats["cloud"] += 1
                continue
            # Lined up elsewhere is checked first: an import records its
            # source before its row stops being 'importing', so one finishing
            # between the two checks is still seen by one of them.
            if importing_elsewhere(store, content):
                # Checked again next scan: a duplicate once that import is
                # done, or imported from here if it was held or failed (#152).
                _put(writes, f["id"], key, ident, "waiting", now)
                stats["waiting"] += 1
                continue
            if already_imported(store, content):
                _put(writes, f["id"], key, ident, "duplicate", now)
                stats["duplicates"] += 1
                continue
            _put(writes, f["id"], key, ident, "importing", now, content=content)
            if not _commit(store, f["id"], writes):
                held = True              # paused or removed during the scan: nothing more
                break
            enqueue(real, f["id"], key, content)
            stats["queued"] += 1
        if stopped:
            # Closing part-way: keep what was seen, but the folder is not marked
            # scanned. A baseline marked done here would make the files not yet
            # reached look new next time, and import them.
            _commit(store, f["id"], writes)
            break
        if held:
            continue                     # as if not scanned, even if resumed this instant
        with store.tx() as db:
            if not _apply(db, f["id"], writes):
                # Paused or removed during the scan: as if not scanned. A
                # baseline left unfinished is done again when it is resumed; a
                # finished one stays, so files that arrive meanwhile are new.
                continue
            if complete:
                # Files that left the folder: forget their rows (bounded). Only
                # after a complete walk; otherwise unseen files are kept.
                gone = [k for k in seen if k not in present]
                db.executemany("DELETE FROM intake_seen WHERE folder_id = ? AND key = ?",
                               [(f["id"], k) for k in gone])
            note = (None if complete
                    else "Part of the folder could not be read; it will be checked again.")
            _set_note(db, f["id"], now, note, done=True)    # keeps a restart note
    purge(store, now)
    return stats


_PUT = ("INSERT INTO intake_seen (folder_id, key, size, mtime_ns, file_id, state, "
        "first_seen, last_seen, content_fp) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(folder_id, key) DO UPDATE SET size = excluded.size, "
        "mtime_ns = excluded.mtime_ns, file_id = excluded.file_id, "
        "state = excluded.state, last_seen = excluded.last_seen, "
        "content_fp = excluded.content_fp")
FOLDER_UNREADABLE_NOTE = ("This folder's location could not be read, so it is not checked. "
                          "Remove it and add it again.")
_TOUCH = "UPDATE intake_seen SET last_seen = ? WHERE folder_id = ? AND key = ?"


def _put(writes, fid, key, ident, state, now, content=None):
    writes.append((_PUT, (fid, key, *ident, state, now, now, content)))


def _touch(writes, fid, key, now):
    writes.append((_TOUCH, (now, fid, key)))


def _apply(db, fid, writes):
    """Write the waiting row changes, unless the folder was paused or removed
    during the scan (#152): its rows went with a removal, and a paused
    folder is scanned afresh when it is turned back on. False then."""
    row = db.execute("SELECT active FROM watched_folders WHERE id = ?", (fid,)).fetchone()
    if row and row["active"]:
        for sql, args in writes:
            db.execute(sql, args)
    writes.clear()
    return bool(row and row["active"])


def _commit(store, fid, writes):
    """Commit the waiting row changes now (one transaction), if any. False if
    the folder was paused or removed during the scan."""
    if not writes:
        return True
    with store.tx() as db:
        return _apply(db, fid, writes)


def _folder_note(store, fid, now, message):
    with store.tx() as db:
        _set_note(db, fid, now, message)


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
                    "failed": counts.get("failed", 0),
                    "last_scan_at": f["last_scan_at"], "error": f["last_error"],
                    "waiting": counts.get("waiting", 0), "cloud": counts.get("cloud", 0),
                    "imported": counts.get("imported", 0),
                    "duplicates": counts.get("duplicate", 0)})
    return out
