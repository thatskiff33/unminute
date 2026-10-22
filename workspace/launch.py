# workspace/launch.py
# The launcher pass (#111 B5): which installed version opens this user's
# workspace. It runs at the start of a window start only (never for the
# support switches), before the single-instance check and any heavy import.
# Standard library only.
#
# Machine mode only (the owner's decision B5-1, 21 Oct 2026). A per-user
# install keeps the install-time gate (install.ps1 waits with exit 5), so it
# starts exactly as before. update.deployment() reports "machine" only for an
# install by install.ps1 -Machine that passes the machine trust test
# (update.machine_trusted, #111 B6b): Program Files' known folder, an
# administrator owner, and no right for this user to change it. Otherwise
# this pass carries on in the version that was started.
#
# Machine mode's Start menu entry starts %ProgramFiles%\Unminute\current, a
# junction to the current version. Step 3 then always re-launches from the
# version's real folder before anything else is imported, so the app, its
# workers and the decode child (sys.executable, sys._base_executable) all run
# from one version, even if an install switches the junction meanwhile. The
# short-lived support switches (--status, --health and the like) are not
# re-launched: started through current, they finish in whichever version it
# pointed at.
#
# In machine mode:
#   1. --slot <role> (set only by a hand-over): no second decision. The
#      process must run from its version's real path (not through the
#      `current` junction); if it does not, it stops with the IT message, so
#      a hand-over can never loop.
#   2. current.json (format 2) and the workspace's storage format and
#      blockers (read without a key, update.current_schema and blockers)
#      choose a version and role (update.launch_decision); any error reading
#      the workspace counts as unreadable, which opens the current version.
#   3. If the chosen version is this one and this process runs from its real
#      path, the app opens here. Otherwise the chosen version is started by
#      its real path, as an argument list, after checking that it is a plain
#      folder directly inside versions, not a link, whose version.json names
#      the same version and storage format as current.json; then this
#      process ends. Any mismatch starts nothing and shows the IT message.
#   4. No version may open it (older with meetings and no version of its
#      format kept, or from a newer version): a fixed message, exit 1.
#
# Nothing here reads meeting content, opens the key or writes anything.

import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from workspace import update

IT_MESSAGE = ("Unminute could not start the right version for your workspace on this "
              "computer. Ask your IT team.")
BLOCKED = ("Your workspace was made by an earlier version of Unminute and still holds "
           "meetings, so this version cannot take it over yet. Ask your IT team.")
TOO_NEW = ("This workspace was created by a newer version of the app (schema {schema}). "
           "Ask your IT team.")

_ANY = object()             # format 1: current.json records no storage format to compare


class Outcome(NamedTuple):
    """role: the role this process opens the workspace in (None: an ordinary
    start). exit: None to open the window here, else the exit code, after
    showing message if there is one."""
    role: object = None
    exit: object = None
    message: object = None


def slot_role(argv):
    """The role a hand-over gave this process, or None. ValueError if
    --slot is given without a known role."""
    if "--slot" not in argv:
        return None
    at = argv.index("--slot") + 1
    if at >= len(argv) or argv[at] not in update.ROLES:
        raise ValueError("unknown role")
    return argv[at]


def is_real(executable):
    """Does this process run from its real path (no junction or link on the
    way, such as machine mode's `current`)? Letter case aside."""
    try:
        return (os.path.normcase(os.path.abspath(executable))
                == os.path.normcase(os.path.realpath(executable)))
    except (OSError, ValueError):
        return False


def start(argv, *, here=None, executable=None, popen=None, deployment=None,
          workspace_db=None):
    """The pass. here, executable, popen, deployment and workspace_db stand
    in for this file, sys.executable, subprocess.Popen, update.deployment and
    the workspace database in tests."""
    try:
        role = slot_role(argv)
    except ValueError:
        return Outcome(exit=1, message=IT_MESSAGE)
    if (deployment or update.deployment)()["mode"] != "machine":
        return Outcome(role=role)               # per-user and source: as before
    here = Path(here or __file__).resolve()     # <root>\versions\<v>\app\workspace\launch.py
    this, root = here.parents[2].name, here.parents[4]
    real = is_real(executable or sys.executable)
    if role is not None:
        return Outcome(role=role) if real else Outcome(exit=1, message=IT_MESSAGE)
    if here.parents[3].name != "versions":
        return Outcome(exit=1, message=IT_MESSAGE)
    state, record = update.read_slots(root)
    if state not in ("ok", "format1"):
        # Damaged: no hand-over (nothing can be checked against it). This
        # version starts as before, from its real path.
        target, chosen, refusal, schema = this, "current", None, _ANY
    else:
        target, chosen, refusal = update.launch_decision(*_workspace(record, workspace_db),
                                                         record)
        schema = _ANY if record["format"] == 1 else (
            record["slots"][target]["schema"] if target else None)
    if refusal == "blocked":
        found = _blockers_text(workspace_db)
        return Outcome(exit=1, message=BLOCKED + (" " + found if found else ""))
    if refusal == "too_new":
        return Outcome(exit=1, message=TOO_NEW.format(schema=_schema_text(workspace_db)))
    if target == this and real:
        return Outcome(role=chosen)
    return hand_over(root, target, chosen, schema, popen or subprocess.Popen)


def _db(workspace_db):
    if workspace_db is not None:
        return Path(workspace_db)
    from workspace.store import default_root
    return default_root() / "workspace.db"


def _workspace(record, workspace_db):
    """(storage format, has blockers) of this user's workspace, read without
    a key. Any error, including a refused location, counts as unreadable:
    the current version then opens and shows its own screen."""
    try:
        db = _db(workspace_db)
        found = update.current_schema(db)
        schema = record["slots"][record["version"]]["schema"]
        if update.whole(found) and update.whole(schema) and found < schema:
            return found, bool(update.blockers(db))
        return found, False
    except Exception:
        return update.UNREADABLE, False


def _blockers_text(workspace_db):
    """The blockers' own messages (counts only), or "" if they cannot be read."""
    try:
        return " ".join(b["message"] for b in update.blockers(_db(workspace_db)))
    except Exception:
        return ""


def _schema_text(workspace_db):
    try:
        found = update.current_schema(_db(workspace_db))
    except Exception:
        found = None
    return found if update.whole(found) else "?"


def _schema_of(info):
    found = info.get("schema")
    return found if update.whole(found) else None


def hand_over(root, name, role, schema, popen):
    """Start version `name` as `role` by its real path, then end this
    process (exit 0). Only a plain folder directly inside <root>\\versions,
    none of whose parts on the way is a link, holding a version.json that
    names the same version and (unless current.json is format 1) storage
    format. Anything else starts nothing: the IT message, exit 1."""
    from workspace.asr_engines import is_link
    refused = Outcome(exit=1, message=IT_MESSAGE)
    if not update.version_name(name) or role not in update.ROLES:
        return refused
    versions = Path(root) / "versions"
    slot = versions / name
    runtime, app = slot / "runtime", slot / "app"
    exe = runtime / "pythonw.exe"
    if any(is_link(p) for p in (versions, slot, runtime, app, exe)):
        return refused
    if not (slot.is_dir() and app.is_dir() and exe.is_file()):
        return refused
    info = update.read_record(slot / "version.json")
    if (info is None or info.get("version") != name
            or (schema is not _ANY and _schema_of(info) != schema)):
        return refused
    try:
        popen([str(exe), *update.LAUNCH_ARGS, "--slot", role], cwd=str(app), close_fds=True)
    except (OSError, ValueError):
        return refused
    return Outcome(exit=0)
