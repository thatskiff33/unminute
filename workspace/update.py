# workspace/update.py
# Installation-aware updates and the empty-workspace migration gate (#26).
#
# One gate for both deployment modes. The installer runs the NEW package's
# runtime with `python -m workspace --gate` before switching version, so the
# decision is made by code that knows the new schema:
#
#   no workspace yet             -> allowed (fresh install)
#   same schema                  -> allowed: a compatible update keeps everything
#                                   (the installer already refuses while the app
#                                   runs, so nothing is processing or cleaning up)
#   newer schema                 -> allowed only when the workspace holds no
#                                   meeting content, no active retention hold and
#                                   no unfinished cleanup; otherwise it WAITS and
#                                   explains why. Minimal completed receipts and
#                                   configuration never block.
#   older schema (a downgrade)   -> refused: that version cannot open this workspace
#
# Nothing here expires a hold, deletes content or makes a content-bearing
# backup to let an update proceed.

import json
import sqlite3
from pathlib import Path

ALLOWED, WAIT, TOO_NEW = 0, 5, 6     # exit codes used by the installer
UNREADABLE = object()                  # current_schema(): the workspace exists but cannot be read

# Tables that can hold meeting content (everything except configuration-like
# bookkeeping: schema_version and the minimal receipts).
NON_CONTENT = {"schema_version", "receipts", "sqlite_sequence",
               # watched-folder configuration and bounded suppression data (#23)
               "watched_folders", "intake_seen", "intake_content", "intake_meta"}


def deployment(app_file=None):
    """How this copy was installed: {"version", "mode": managed|self-service|source}."""
    here = Path(app_file or __file__).resolve()
    try:
        install_root = here.parents[4]           # <root>/versions/<v>/app/workspace/x.py
        # Windows PowerShell writes UTF-8 with a byte-order mark.
        current = json.loads((install_root / "current.json").read_text(encoding="utf-8-sig"))
        return {"version": current.get("version"),
                "mode": "managed" if current.get("managed") else "self-service"}
    except (OSError, ValueError, IndexError):
        return {"version": None, "mode": "source"}


def _connect(db_path):
    return sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)


def current_schema(db_path):
    """The workspace schema; None if there is no workspace; UNREADABLE if a
    database exists but cannot be read (damaged, locked or not ours). The gate
    fails closed on UNREADABLE: never treat it as a fresh install."""
    p = Path(db_path)
    if not p.exists() or p.stat().st_size == 0:
        return None
    try:
        con = _connect(db_path)
    except sqlite3.Error:
        return UNREADABLE
    try:
        return con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] \
            or UNREADABLE
    except sqlite3.Error:
        return UNREADABLE
    finally:
        con.close()


def blockers(db_path):
    """What stops a schema-changing update, in plain language. Counts only."""
    if not Path(db_path).exists():
        return []
    con = _connect(db_path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")]
        count = (lambda t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
        out = []
        holds = (con.execute("SELECT COUNT(*) FROM holds WHERE released_at IS NULL")
                 .fetchone()[0] if "holds" in tables else 0)
        if holds:
            out.append({"kind": "holds", "count": holds, "message":
                        f"{holds} retention hold{'s are' if holds != 1 else ' is'} active. "
                        "Holds never expire on their own; the update waits until they are "
                        "released in the app by the people responsible."})
        meetings = count("meetings") if "meetings" in tables else 0
        if meetings:
            out.append({"kind": "meetings", "count": meetings, "message":
                        f"{meetings} meeting{'s are' if meetings != 1 else ' is'} still in the "
                        "workspace. Finish each one (review, hand off, then confirm the final "
                        "document is saved) so its content is cleaned up."})
        jobs = count("cleanup_jobs") if "cleanup_jobs" in tables else 0
        if jobs:
            out.append({"kind": "cleanup", "count": jobs, "message":
                        f"{jobs} cleanup step{'s have' if jobs != 1 else ' has'} not finished. "
                        "Open the app so it can finish (or confirm) them."})
        recordings = Path(db_path).parent / "recordings"
        waiting = (sum(1 for p in recordings.iterdir() if p.is_dir())
                   if recordings.is_dir() else 0)
        if waiting:
            out.append({"kind": "recordings", "count": waiting, "message":
                        f"{waiting} microphone recording{'s have' if waiting != 1 else ' has'} "
                        "not been imported yet. Open the app so it can import or discard "
                        "them."})
        known = {"holds", "meetings", "cleanup_jobs"} | NON_CONTENT
        leftover = sum(count(t) for t in tables if t not in known)
        if leftover and not meetings:
            out.append({"kind": "content", "count": leftover, "message":
                        "Some meeting content is still stored. Open the app and finish the "
                        "cleanup steps."})
        return out
    finally:
        con.close()


def gate(db_path, new_schema):
    """The update decision for a package whose schema is `new_schema`."""
    cur = current_schema(db_path)
    if cur is UNREADABLE:
        return {"code": WAIT, "kind": "unreadable", "current_schema": None, "blockers": [],
                "message": "The workspace could not be read, so the update waits rather than "
                           "risk it. Open the app to check it, or ask support "
                           "(python -m workspace --status)."}
    if cur is None:
        return {"code": ALLOWED, "kind": "fresh", "current_schema": None, "blockers": [],
                "message": "No workspace yet: the update can go ahead."}
    if cur == new_schema:
        return {"code": ALLOWED, "kind": "compatible", "current_schema": cur, "blockers": [],
                "message": "The workspace is compatible: everything is kept."}
    if cur > new_schema:
        return {"code": TOO_NEW, "kind": "too_new", "current_schema": cur, "blockers": [],
                "message": "This workspace was created by a newer version of the app, which "
                           "this version cannot open. Install the newer version again."}
    found = blockers(db_path)
    if found:
        return {"code": WAIT, "kind": "migration", "current_schema": cur, "blockers": found,
                "message": "This update changes how the workspace is stored, so it waits "
                           "until the workspace is empty: " +
                           " ".join(b["message"] for b in found)}
    return {"code": ALLOWED, "kind": "migration", "current_schema": cur, "blockers": [],
            "message": "The workspace is empty: the update can go ahead. Settings and "
                       "completed cleanup receipts are kept."}
