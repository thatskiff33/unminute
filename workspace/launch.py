# workspace/launch.py
# The launcher pass (#111 B5): which installed version opens this user's
# workspace. It runs at the start of a window start only (never for the
# support switches), before the single-instance check and any heavy import.
# Standard library only.
#
# Machine mode only (the owner's decision B5-1, 21 Oct 2026). A per-user
# install keeps the install-time gate (install.ps1 waits with exit 5), so it
# starts exactly as before. update.deployment() reports "machine" only once
# B6b's machine trust test passes, so until then this pass always carries on
# in the version that was started.
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
#   B5b, before step 3 (workspace/launchcheck.py has the rules): the chosen
#      version's light check at its first start for this user, a fallback to
#      another kept version of the same format after a slot fault, the full
#      check (--self-test) before a start that would migrate the workspace,
#      and, before a compat start, the "update waiting" message at most once
#      a day.
#
# Nothing here reads meeting content or opens the key. The only thing
# written is the launcher's own record, workspace\logs\launch.json (and the
# app log's launch_check lines): version names, fixed words, codes, times.

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

from workspace import update

IT_MESSAGE = ("Unminute could not start the right version for your workspace on this "
              "computer. Ask your IT team.")
BLOCKED = ("Your workspace was made by an earlier version of Unminute and still holds "
           "meetings, so this version cannot take it over yet. Ask your IT team.")
TOO_NEW = ("This workspace was created by a newer version of the app (schema {schema}). "
           "Ask your IT team.")
# B5b (workspace/launchcheck.py): the daily "update waiting" message (B5-6),
# a fallback after a version's first-start check failed, no version left to
# fall back to, and a failed check before a migration.
WAITING = ("An update of Unminute is installed. It takes over when your workspace has no "
           "meetings, holds or unfinished cleanup. Until then version {version} opens, for "
           "finishing your meetings only.")
FALLBACK = ("This version of Unminute did not start correctly on this computer (code {code}), "
            "so version {version} opened instead. Ask your IT team.")
FAILED = ("This version of Unminute did not start correctly on this computer (code {code}). "
          "Ask your IT team.")
NOT_READY = ("Unminute could not get its update ready: Windows could not protect a test key "
             "for you on this computer (code {code}). Your workspace was not changed. Ask your "
             "IT team.")

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
          workspace_db=None, checks=None, show=None, notice=None, today=None):
    """The pass. here, executable, popen, deployment and workspace_db stand
    in for this file, sys.executable, subprocess.Popen, update.deployment and
    the workspace database in tests; checks, show, notice and today for the
    checks (launchcheck.Checks), the message box, the notice shown during
    the full check and today's date (B5b)."""
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
    found = None
    if state not in ("ok", "format1"):
        # Damaged: no hand-over and no check (nothing can be checked against
        # it). This version starts as before, from its real path.
        target, chosen, refusal, schema = this, "current", None, _ANY
    else:
        found, blocked = _workspace(record, workspace_db)
        target, chosen, refusal = update.launch_decision(found, blocked, record)
        schema = _ANY if record["format"] == 1 else (
            record["slots"][target]["schema"] if target else None)
    if refusal == "blocked":
        text = _blockers_text(workspace_db)
        return Outcome(exit=1, message=BLOCKED + (" " + text if text else ""))
    if refusal == "too_new":
        return Outcome(exit=1, message=TOO_NEW.format(schema=_schema_text(workspace_db)))
    if state in ("ok", "format1"):
        # B5b: the first-start check of the version that will open, a
        # fallback after a slot fault, the full check before a migration and
        # the daily "update waiting" message.
        checked = Checked(root, record, schema, _logs(workspace_db), checks, show,
                          notice, today).run(target, chosen, _migrates(found, record, chosen))
        if isinstance(checked, Outcome):
            return checked
        target, chosen = checked
    if target == this and real:
        return Outcome(role=chosen)
    return hand_over(root, target, chosen, schema, popen or subprocess.Popen)


def _migrates(found, record, role):
    """Would opening the current version migrate this workspace? Only an
    older workspace (launch_decision sends one that still holds anything the
    update gate counts to compat instead), and only with a format-2 record,
    which states the current version's storage format."""
    schema = record["slots"][record["version"]]["schema"]
    return (role == "current" and update.whole(found) and update.whole(schema)
            and found < schema)


def _logs(workspace_db):
    """The workspace's logs folder, or None when the location is refused."""
    try:
        return _db(workspace_db).parent / "logs"
    except Exception:
        return None


def _inform(text):
    """A Windows information box (pythonw has no console)."""
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, text, "Unminute", 0x40)


class Checked:
    """B5b's checks for one start (workspace/launchcheck.py has the rules)."""

    def __init__(self, root, record, schema, logs, checks, show, notice, today):
        from workspace import diag, launchcheck
        self.root, self.record, self.schema = root, record, schema
        self.book = launchcheck.Book(logs)
        self.log = diag.Log(logs, "app") if logs is not None else None
        self.checks = checks or launchcheck.Checks()
        self.show = show or _inform
        self.notice = notice or launchcheck.Notice
        self.today = today or time.strftime("%Y-%m-%d")

    def _installed(self, name):
        return (self.record["slots"].get(name) or {}).get("installed")

    def _write(self, **fields):
        if self.log is not None:
            self.log.write("launch_check", **fields)

    def _light(self, name, exe, app, info):
        """(result, part, code) of the version's light check: recorded, or
        run now and recorded. A version from before B5b has no
        --launch-check and is never started with it: "unknown"."""
        from workspace import diag, launchcheck
        features = info.get("features")
        if not (isinstance(features, list) and launchcheck.FEATURE in features):
            return "unknown", None, None
        known = self.book.known(name, self._installed(name))
        if known is not None:
            return known["result"], known["part"], known["code"] or diag.new_code()
        result, part = self.checks.light(exe, app)
        code = None if result == "ok" else diag.new_code()
        self.book.record(name, self._installed(name), result, part, code)
        self._write(check=result, part=part, code=code)
        return result, part, code

    def _full(self, name, exe, app):
        """(result, part, code) of --self-test before a migration. Never
        read from the record; a slot fault is recorded."""
        from workspace import diag
        with self.notice():
            result, part = self.checks.full(exe, app)
        code = None if result == "ok" else diag.new_code()
        if result == "slot_fault":
            self.book.record(name, self._installed(name), result, part, code)
        self._write(check=result, part=part, code=code)
        return result, part, code

    def _fallback(self, tried):
        """The newest other kept version of the same storage format not yet
        tried, or None (a format-1 record knows no other version)."""
        if not update.whole(self.schema):
            return None
        found = [(e["seq"], n) for n, e in self.record["slots"].items()
                 if e["schema"] == self.schema and n not in tried]
        return max(found)[1] if found else None

    def run(self, target, role, migrating):
        """(version, role) to open, or an Outcome that ends the start."""
        tried, fault = [], None
        name = target
        while True:
            slot = _slot(self.root, name, self.schema)
            if slot is None:
                return Outcome(exit=1, message=IT_MESSAGE)
            exe, app, info = slot
            result, part, code = self._light(name, exe, app, info)
            if result in ("ok", "unknown") and migrating:
                result, part, code = self._full(name, exe, app)
            if result == "slot_fault":
                tried.append(name)
                fault = fault or (name, code)
                name, role = self._fallback(tried), "fallback"
                if name is None:
                    return self._end(FAILED.format(code=fault[1]), "slot_fault")
                continue
            if result == "env_fault" and migrating:
                # Never a fallback, and never a migration after a failed check.
                return self._end(NOT_READY.format(code=code), "env_fault")
            break
        self.book.last_role = role
        self.book.last_check = "slot_fault" if fault else (
            None if result == "unknown" else result)
        if fault is not None:
            failed = fault[0]
            installed = self._installed(failed)
            if failed not in self.book.fallback_shown \
                    or self.book.fallback_shown[failed] != installed:
                self.show(FALLBACK.format(code=fault[1], version=name))
                self.book.fallback_shown[failed] = installed
        if role == "compat" and self.book.waiting_shown != self.today:
            self.show(WAITING.format(version=name))
            self.book.waiting_shown = self.today
        self.book.save(set(self.record["slots"]), self.log)
        return name, role

    def _end(self, message, check):
        self.book.last_check = check
        self.book.save(set(self.record["slots"]), self.log)
        return Outcome(exit=1, message=message)


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


def _slot(root, name, schema):
    """(pythonw.exe, app folder, version.json) of version name, or None:
    only a plain folder directly inside <root>\versions, none of whose parts
    on the way is a link, holding a version.json that names the same version
    and (unless current.json is format 1) storage format."""
    from workspace.asr_engines import is_link
    if not update.version_name(name):
        return None
    versions = Path(root) / "versions"
    slot = versions / name
    runtime, app = slot / "runtime", slot / "app"
    exe = runtime / "pythonw.exe"
    if any(is_link(p) for p in (versions, slot, runtime, app, exe)):
        return None
    if not (slot.is_dir() and app.is_dir() and exe.is_file()):
        return None
    info = update.read_record(slot / "version.json")
    if (info is None or info.get("version") != name
            or (schema is not _ANY and _schema_of(info) != schema)):
        return None
    return exe, app, info


def hand_over(root, name, role, schema, popen):
    """Start version `name` as `role` by its real path, after _slot's
    checks, then end this process (exit 0). Anything else starts nothing:
    the IT message, exit 1."""
    refused = Outcome(exit=1, message=IT_MESSAGE)
    found = _slot(root, name, schema) if role in update.ROLES else None
    if found is None:
        return refused
    exe, app, _ = found
    try:
        popen([str(exe), *update.LAUNCH_ARGS, "--slot", role], cwd=str(app), close_fds=True)
    except (OSError, ValueError):
        return refused
    return Outcome(exit=0)
