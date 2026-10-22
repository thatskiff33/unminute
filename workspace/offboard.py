# workspace/offboard.py
# Offboarding: what Unminute keeps for this Windows user, and its removal
# (#111 B8; the owner's decisions B8-1 to B8-8, 21 Oct 2026;
# docs/WORKSPACE_DESIGN.md "Offboarding").
#
#   python -m workspace --offboard-report   counts and yes or no only
#   python -m workspace --offboard          removes it: run as the user, app closed
#
# What is covered: the workspace (%LOCALAPPDATA%\Unminute\workspace) as a
# whole, the per-user models (Unminute\models), the Hugging Face token, and
# the lid record (put back first, with the app's own rules). The program
# itself is the uninstaller's (B8b, uninstall.ps1 -Offboard).
#
# The earlier version's data (0.4.3 and before, B8-3: its workspace and
# models reported, never opened or removed; only its token removed) is
# supported but dormant: EARLIER_FOLDERS and EARLIER_TOKENS are empty,
# because tests/test_consistency.py keeps the names from before the rename
# out of shipped code (K1, #136), and naming them needs the owner's
# agreement to a narrow exemption there (AGENTS.md E2). Until then the
# report says "earlier_version": null and nothing of the earlier version
# is touched.
#
# Nothing here deletes meeting content or expires a hold (C1, C2, C4): the
# workspace is removed only when nothing the update gate counts is left
# (update.blockers, B8-1), a locked workspace included (B8-6). The order
# (B8-4, B8-8): refuse Local System and an open app; finish a removal a
# crash left; check the blockers; put the lid back; write the offboarded
# audit record and its Event Log copy; erase the root key and empty the
# database's log; rename the workspace aside, so the app never opens a
# half-removed one; delete it; then the models, the tokens and Unminute
# itself if empty. A link at Unminute or Unminute\workspace is refused: the
# real data is elsewhere and is IT's to remove. Links inside, and a models
# folder IT redirected, are removed as links, never followed.
#
# The output is counts, states and fixed text only (P3): no path, name or
# content. Standard library only at module level (store, power, audit and
# credentials are imported where they are used).

import os
import re
import stat
import time
import uuid
from pathlib import Path

from workspace import diag, update

OK, FAILED, ACCOUNT, RUNNING, BLOCKED, REFUSED = 0, 1, 2, 3, 5, 6
TOKENS = ("Unminute/HuggingFaceToken",)              # credentials.TARGET (K1)
EARLIER_TOKENS = ()             # B8-3, dormant (see above): the earlier token's name
EARLIER_FOLDERS = ()            # B8-3, dormant: (workspace, models) under %LOCALAPPDATA%
ASIDE = re.compile(r"workspace\.offboarding-[0-9a-f]{32}\Z")
WAIT_S = 30.0                    # a file in use is waited for this long, then exit 1
MIB = 1 << 20

# What removal cannot reach, printed as fixed text with every --offboard
# (the design's section 4.5; docs/SECURITY_MODEL.md T29).
RESIDUALS = (
    "Copies made outside Unminute remain: whole-disk or profile backups, Volume Shadow "
    "Copies and File History. With this account's Windows keys, or the domain's backup "
    "key, such a copy can still be opened.",
    "On an SSD, data deleted before encryption at rest (plain text) may remain in free "
    "space until it is overwritten.",
    "Event Log entries (the audit log's copy) cannot be removed for one user; they follow "
    "the organisation's log retention.",
    "Data that version 0.4.3 or earlier left (its own workspace, models and token) is not "
    "removed: IT removes it by hand.",
)


class Where:
    """The folders offboarding looks at, from %LOCALAPPDATA% (`appdata`).
    earlier: the earlier version's (workspace, models), relative to it, or
    () when they are not looked at."""

    def __init__(self, appdata, earlier=EARLIER_FOLDERS):
        self.appdata = Path(appdata)
        self.base = self.appdata / "Unminute"                 # K1 names
        self.workspace = self.base / "workspace"
        self.models = self.base / "models"
        self.earlier = tuple(self.appdata / f for f in earlier)


# -- links and removal -----------------------------------------------------------------

def is_link(path):
    """A symbolic link, a junction or any other reparse point: never followed."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _long(path):
    """The extended form on Windows, so a deep folder (WebView2's) is not
    refused at MAX_PATH; string work only."""
    path = os.fspath(path)
    if os.name == "nt" and not path.startswith("\\\\"):
        return "\\\\?\\" + os.path.abspath(path)
    return path


def _remove_link(path):
    """The link itself, never what it points to."""
    try:
        os.rmdir(path)                  # a junction or directory link
    except NotADirectoryError:
        os.unlink(path)                 # a file link


def _remove_file(path):
    try:
        os.unlink(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)   # read-only; a file in use raises again
        os.unlink(path)


def remove_tree(path):
    """Remove path and everything in it. A link, anywhere (path included), is
    removed as a link and never followed. Something already gone is fine;
    anything that cannot be removed now raises OSError."""
    path = _long(path)
    try:
        if is_link(path):
            _remove_link(path)
            return
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            _remove_file(path)
            return
        with os.scandir(path) as it:
            names = [entry.path for entry in it]
        for name in names:
            remove_tree(name)
        os.rmdir(path)
    except FileNotFoundError:
        pass


def _waiting(action):
    """Run action, trying again for up to WAIT_S while a file is in use
    (as the installer waits, #239). Raises the last OSError."""
    deadline = time.monotonic() + WAIT_S
    while True:
        try:
            return action()
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def _rename_aside(where):
    """workspace -> workspace.offboarding-<32 hex>: the app never opens a
    half-removed workspace (it would start a new, empty one)."""
    aside = where.base / f"workspace.offboarding-{uuid.uuid4().hex}"
    os.rename(where.workspace, aside)
    return aside


def _size(path):
    """Bytes under path, without following links."""
    total = 0
    try:
        for here, dirs, files in os.walk(_long(path), followlinks=False):
            dirs[:] = [d for d in dirs if not is_link(os.path.join(here, d))]
            for name in files:
                try:
                    total += os.lstat(os.path.join(here, name)).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


# -- what is there ---------------------------------------------------------------------

def _leftovers(where):
    """Folders a removal that stopped part-way left (the app's own names only)."""
    try:
        return sorted(p for p in where.base.iterdir() if ASIDE.fullmatch(p.name))
    except OSError:
        return []


def _blockers(ws):
    """{kind: count} of what the update gate counts (B8-1). A database that
    cannot be read blocks as unreadable: never treated as empty."""
    db = ws / "workspace.db"
    if update.current_schema(db) is update.UNREADABLE:
        return {"unreadable": 1}
    try:
        found = {b["kind"]: b["count"] for b in update.blockers(db)}
    except Exception:
        return {"unreadable": 1}
    if not os.path.lexists(db):
        # The gate counts folders only beside a database; offboarding never
        # removes a recording or meeting audio left without one.
        recordings = ws / "recordings"
        waiting = (sum(1 for p in recordings.iterdir() if p.is_dir())
                   if recordings.is_dir() else 0)
        media = len(update._filled_folders(ws / "media"))
        found.update({k: n for k, n in (("recordings", waiting), ("media", media)) if n})
    return found


LOCK = "instance.lock"           # B5's workspace lock (#111 B5-10): its first byte


def _try_lock(fd):
    """Lock the first byte without waiting; OSError if another handle holds it."""
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:                                               # Linux, for the tests
        import fcntl
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0)


def _lock_held(ws):
    """Is the workspace lock held by the app in any Windows session of this
    user? Never creates the file. The probe's own lock goes when its handle
    closes."""
    try:
        fd = os.open(ws / LOCK, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:
        return False
    try:
        _try_lock(fd)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def _hold_lock(ws):
    """Take the workspace lock for the removal, so an app that honours it
    cannot open the workspace meanwhile in any session. The handle, or None
    if another process holds it. Closed before the rename (Windows will not
    rename a folder with a file open in it)."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    fd = os.open(ws / LOCK, flags, 0o600)
    try:
        _try_lock(fd)
    except OSError:
        os.close(fd)
        return None
    return fd


def _running(ws, session_running, holding=False):
    """The app is open: in this session (its mutex), or in any session of
    this user (the run marker's process is alive, or the workspace lock is
    held). holding: this run holds the lock itself, so it is not probed."""
    if session_running():
        return True
    marker = ws / "logs" / "app.running"
    if marker.exists() and diag.run_owner_alive(marker):
        return True
    return not holding and _lock_held(ws)


def assess(where, session_running, creds, names=(TOKENS, EARLIER_TOKENS)):
    """The report's facts: counts, states and yes or no. names: the token
    names (current, earlier); tests give scratch ones."""
    ws = where.workspace
    out = {"workspace": "none", "blockers": {}, "leftover": len(_leftovers(where)),
           "models_mb": 0, "models_link": is_link(where.models),
           "token": any(creds.has_token(t) for t in names[0]),
           "lid_record": os.path.lexists(ws / "power_restore.json"),
           "running": False,
           "earlier_version": None}
    if where.earlier:
        # Present or not only: nothing inside is opened (P2).
        out["earlier_version"] = {"workspace": os.path.lexists(where.earlier[0]),
                                  "models": os.path.lexists(where.earlier[1]),
                                  "token": any(creds.has_token(t) for t in names[1])}
    if os.path.lexists(where.models) and not out["models_link"]:
        size = _size(where.models)
        out["models_mb"] = (size + MIB - 1) // MIB
    if not os.path.lexists(ws):
        out["running"] = bool(session_running())
        return out
    out["running"] = _running(ws, session_running)
    found = _blockers(ws)
    out["blockers"] = found
    keys = diag.keys_state(ws / "logs")
    if found:
        out["workspace"] = "blocked"
    elif keys and keys["result"] == "locked":
        out["workspace"] = "locked_empty"
    else:
        out["workspace"] = "empty"
    return out


# -- the run ---------------------------------------------------------------------------

def _refused(where):
    """A link (or anything but a folder) at Unminute or Unminute\\workspace."""
    for path in (where.base, where.workspace):
        if is_link(path) or (os.path.lexists(path) and not os.path.isdir(path)):
            return True
    return False


def _record(ws, mirror, facts):
    """The offboarded audit record, before anything is removed (B8-4): written
    to the workspace's audit file and, when a mirror is given, reported to
    the Event Log. Whether both happened."""
    from workspace import audit
    log = audit.AuditLog(ws / "audit", mirror=mirror)
    written = log.record("offboarded", **facts)
    log.close()
    return written and (mirror is None or (log.stats["unmirrored"] == 0
                                           and log.summary()["mirror"] == "ok"))


def run(where, remove, *, account, session_running, mirror=None, tokens=TOKENS,
        earlier_tokens=EARLIER_TOKENS, creds=None, lid=None):
    """--offboard-report (remove False) or --offboard (remove True): (result,
    exit code). account: audit.token_user's answer reduced to True (Local
    System), False (a user) or None (Windows cannot say). session_running:
    whether the app is open in this session. mirror: the Event Log copy
    (audit.EventLogMirror in the app; None off Windows). creds: has_token
    and remove_token (workspace.credentials). lid: power.restore_only."""
    if account is None:
        return {"result": "account_unknown"}, ACCOUNT
    if account:
        return {"result": "system_account"}, ACCOUNT
    if _refused(where):
        return {"result": "refused", "workspace": "refused"}, REFUSED
    if creds is None:
        from workspace import credentials as creds
    if lid is None:
        from workspace.power import restore_only as lid
    names = (tuple(tokens), tuple(earlier_tokens))
    facts = assess(where, session_running, creds, names)
    if facts["running"]:
        return {"result": "running", **facts}, RUNNING
    if not remove:
        if facts["blockers"]:
            return {"result": "blocked", **facts}, BLOCKED
        return {"result": "ok", **facts}, OK
    return _remove(where, facts, session_running, mirror, creds, lid, names)


def _failed(step, facts):
    return {"result": "failed", "step": step, **facts}, FAILED


def _prepare(where, facts, session_running, mirror, lid):
    """Under the workspace lock: the lid, the audit record, a last look, and
    the root key's erase. None when the workspace may be renamed and
    deleted; otherwise the result to return."""
    ws = where.workspace
    running = lambda: _running(ws, session_running, holding=True)  # noqa: E731
    state = lid(ws / "power_restore.json", running)
    if state == "running":
        return {"result": "running", **facts, "running": True}, RUNNING
    if state not in ("none", "restored", "damaged"):   # damaged: nothing can put it back
        return _failed("lid", facts)
    record = {"locked": facts["workspace"] == "locked_empty",
              "models": os.path.lexists(where.models),
              "token": facts["token"],
              "earlier_token": bool(facts["earlier_version"]
                                    and facts["earlier_version"]["token"])}
    try:
        if not _record(ws, mirror, record):
            return _failed("audit", facts)
    except Exception:
        return _failed("audit", facts)
    # A last look before anything is erased: an app started meanwhile in
    # another session (before B5's lock is honoured), or anything it added.
    if running():
        return {"result": "running", **facts, "running": True}, RUNNING
    found = _blockers(ws)
    if found:
        return {"result": "blocked", **facts, "workspace": "blocked", "blockers": found}, BLOCKED
    db = ws / "workspace.db"
    if os.path.lexists(db):
        from workspace import store
        try:
            if not store.erase_root_key(db):
                return _failed("key", facts)
        except Exception:
            return _failed("key", facts)
    return None


def _remove(where, facts, session_running, mirror, creds, lid, names):
    ws = where.workspace
    # A removal a crash or a file in use stopped part-way was checked and
    # erased then: finish it first (C1: intent persists).
    try:
        for aside in _leftovers(where):
            _waiting(lambda aside=aside: remove_tree(aside))
    except OSError:
        return _failed("leftover", facts)
    if facts["blockers"]:
        return {"result": "blocked", **facts}, BLOCKED
    removed = {"workspace": False, "models": False, "token": False, "earlier_token": False}
    if os.path.lexists(ws):
        made = not os.path.lexists(ws / LOCK)
        try:
            held = _hold_lock(ws)
        except OSError:
            return _failed("lock", facts)
        if held is None:
            return {"result": "running", **facts, "running": True}, RUNNING
        try:
            outcome = _prepare(where, facts, session_running, mirror, lid)
        finally:
            os.close(held)
        if outcome is not None:
            if made:                    # a stopped run leaves the workspace as it found it
                try:
                    os.unlink(ws / LOCK)
                except OSError:
                    pass
            return outcome
        try:
            aside = _waiting(lambda: _rename_aside(where))
        except OSError:
            return _failed("rename", facts)
        try:
            _waiting(lambda: remove_tree(aside))
        except OSError:
            return _failed("workspace", facts)
        removed["workspace"] = True
    if os.path.lexists(where.models):
        try:
            _waiting(lambda: remove_tree(where.models))   # a link: the link only
        except OSError:
            return _failed("models", facts)
        removed["models"] = True
    try:
        for t in names[0]:
            removed["token"] = bool(creds.remove_token(t)) or removed["token"]
        for t in names[1]:
            removed["earlier_token"] = bool(creds.remove_token(t)) or removed["earlier_token"]
    except Exception:
        return _failed("token", facts)
    try:
        os.rmdir(where.base)            # only if now empty
    except OSError:
        pass
    after = assess(where, session_running, creds, names)
    after["models_link"] = facts["models_link"]          # what was removed was a link
    return {"result": "offboarded", **after, "removed": removed,
            "residuals": list(RESIDUALS)}, OK
