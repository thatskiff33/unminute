# workspace/launchcheck.py
# The launcher's checks (#111 B5b, the owner's decisions B5-2 and B5-5 of
# 21 Oct 2026), run by the launcher pass (workspace/launch.py). The light
# check and its fallback run in machine mode and, since the owner's decision
# of 23 Oct 2026, in a per-user install too; the full check and the "update
# waiting" message are machine mode only.
#
#   Light check: at a version's first start for this user, a child process,
#       <runtime>\pythonw.exe LAUNCH_ARGS --launch-check, limited to 30 s. It
#       imports the window process's modules and pywebview's WebView2 module,
#       confirms the offline guard, and protects and unlocks a random value
#       with Windows Data Protection. It prints one JSON line: a result and,
#       for a fault, which part failed (fixed words only).
#   Full check: before the start that would migrate the workspace (a
#       migration cannot be undone), --self-test in a child process, limited
#       to 180 s, while a notice says the update is being got ready. It always
#       runs: it never consults the record below.
#
# Fault classes:
#   slot_fault  an import, the WebView2 module or the guard failed, or the
#               check hung or gave no answer: the version itself is at fault.
#               It is recorded and not tried again until the version is
#               reinstalled or repaired (its "installed" time in current.json
#               changes); the launcher falls back to another kept version of
#               the same storage format.
#   env_fault   Windows Data Protection failed: the user's profile, not the
#               version. Never a fallback, never recorded as a pass, so the
#               check runs again at the next start.
#
# The record, workspace\logs\launch.json (written in full or not at all,
# diag._write_state): per version its install time, result, part, code and
# time; the role and check result of the last start; the day the "update
# waiting" message was last shown; and which failed version's fallback
# message was shown. Version names, fixed words, codes and times only (P3).
# A forged record can at most force a fallback or skip a light check; it
# cannot skip the full check before a migration.
#
# Neither check opens the workspace database or its key. Standard library
# only (it runs before the window process's heavy imports).

import json
import os
import re
import subprocess
import time
from pathlib import Path

from workspace import diag, update

LIGHT_LIMIT_S = 30
FULL_LIMIT_S = 180
FEATURE = "launch-check"         # version.json "features": the version has --launch-check
RESULTS = ("ok", "slot_fault", "env_fault")
PARTS = ("import", "webview", "guard", "dpapi", "hung", "no_answer", "self_test")
# What the window process imports before it shows anything, and pywebview's
# WebView2 module (as --self-test imports it).
LIGHT_MODULES = ("workspace.app", "workspace.api", "workspace.shell", "webview",
                 "webview.platforms.edgechromium")
_RECORD_MAX = 64 * 1024
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_CREATE_NO_WINDOW = 0x08000000


# -- In the child: the light check --------------------------------------------------

def light_check(dpapi_round_trip):
    """{"result", "part"} for this version. dpapi_round_trip: __main__'s
    (passed, line). An import or guard failure is a slot fault and is
    reported before Windows Data Protection is tried."""
    import importlib
    for name in LIGHT_MODULES:
        try:
            importlib.import_module(name)
        except Exception:
            return {"result": "slot_fault",
                    "part": "webview" if name.startswith("webview") else "import"}
    if not _guard_holds():
        return {"result": "slot_fault", "part": "guard"}
    passed, _ = dpapi_round_trip()
    if not passed:
        return {"result": "env_fault", "part": "dpapi"}
    return {"result": "ok", "part": None}


def _guard_holds():
    """One connection attempt to 192.0.2.1 (TEST-NET-1, reserved for
    documentation) is stopped by the offline guard before it reaches Windows."""
    import socket

    from workspace import offline_guard
    try:
        socket.create_connection(("192.0.2.1", 443), timeout=3).close()
    except offline_guard.NetworkBlockedError:
        return True
    except OSError:
        return False
    return False


# -- In the launcher: running the checks ---------------------------------------------

def _run(exe, app, switch, limit, runner):
    """(exit code, output) of exe LAUNCH_ARGS switch from app, or None when
    it could not start or ran past limit (subprocess.run then ends it)."""
    try:
        done = runner([str(exe), *update.LAUNCH_ARGS, switch], cwd=str(app),
                      stdin=subprocess.DEVNULL, capture_output=True, timeout=limit,
                      creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    out = done.stdout
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    return done.returncode, out or ""


def run_light(exe, app, runner=subprocess.run, limit=LIGHT_LIMIT_S):
    """(result, part) of the light check run by that version's own runtime."""
    ran = _run(exe, app, "--launch-check", limit, runner)
    if ran is None:
        return "slot_fault", "hung"
    lines = [line for line in ran[1].splitlines() if line.strip()]
    try:
        answer = json.loads(lines[-1]) if lines else None
    except ValueError:
        answer = None
    if (not isinstance(answer, dict) or answer.get("result") not in RESULTS
            or answer.get("part") not in (None, *PARTS)):
        return "slot_fault", "no_answer"
    if answer["result"] == "ok" and ran[0] != 0:
        return "slot_fault", "no_answer"
    return answer["result"], answer.get("part")


def run_full(exe, app, runner=subprocess.run, limit=FULL_LIMIT_S):
    """(result, part) of --self-test run by that version's own runtime. Its
    output is classified by its fixed prefixes and never kept: a failure of
    the Windows Data Protection round trip alone is an environment fault."""
    ran = _run(exe, app, "--self-test", limit, runner)
    if ran is None:
        return "slot_fault", "hung"
    code, out = ran
    lines = out.splitlines()
    failed = [line for line in lines if line.startswith("FAILED")]
    if code == 0 and not failed and "PASS" in (line.strip() for line in lines):
        return "ok", None
    if failed and all("dpapi round trip" in line for line in failed):
        return "env_fault", "dpapi"
    return "slot_fault", "self_test"


class Checks:
    """The two checks, run by a version's own runtime. Tests pass their own."""

    def light(self, exe, app):
        return run_light(exe, app)

    def full(self, exe, app):
        return run_full(exe, app)


# -- The record: logs\launch.json ----------------------------------------------------

class Book:
    """logs\\launch.json, read once and written in full (save). folder None
    (the workspace location is refused): nothing is read or kept, so every
    check runs and every message shows."""

    def __init__(self, folder):
        self.folder = Path(folder) if folder is not None else None
        self.slots, self.last_role, self.last_check = {}, None, None
        self.waiting_shown, self.fallback_shown = None, {}
        raw = read_raw(self.folder) if self.folder is not None else None
        if raw is None:
            return
        for name, entry in (raw.get("slots") or {}).items() if isinstance(
                raw.get("slots"), dict) else ():
            if update.version_name(name) and isinstance(entry, dict) \
                    and entry.get("result") in RESULTS:
                self.slots[name] = {
                    "installed": _text(entry.get("installed"), _TIME),
                    "result": entry["result"],
                    "part": entry.get("part") if entry.get("part") in PARTS else None,
                    "code": entry.get("code") if diag.FIELDS["code"](entry.get("code"))
                    == entry.get("code") else None,
                    "at": _text(entry.get("at"), _TIME)}
        if raw.get("last_role") in update.ROLES:
            self.last_role = raw["last_role"]
        if raw.get("last_check") in RESULTS:
            self.last_check = raw["last_check"]
        self.waiting_shown = _text(raw.get("waiting_shown"), _DAY)
        shown = raw.get("fallback_shown")
        if isinstance(shown, dict):
            self.fallback_shown = {k: _text(v, _TIME) for k, v in shown.items()
                                   if update.version_name(k)}

    def known(self, name, installed):
        """The recorded result for this version as installed now, or None
        when its check must run: never checked, reinstalled or repaired
        since, or an environment fault last time (checked at every start)."""
        entry = self.slots.get(name)
        if entry is None or entry["installed"] != installed or entry["result"] == "env_fault":
            return None
        return entry

    def record(self, name, installed, result, part, code):
        self.slots[name] = {"installed": installed, "result": result, "part": part,
                            "code": code, "at": _now()}

    def save(self, keep, log=None):
        """Write the record, keeping only versions in keep (the installed
        ones), so it never grows. Never raises."""
        if self.folder is None:
            return False
        state = {"slots": {k: v for k, v in self.slots.items() if k in keep},
                 "last_role": self.last_role, "last_check": self.last_check,
                 "waiting_shown": self.waiting_shown,
                 "fallback_shown": {k: v for k, v in self.fallback_shown.items() if k in keep}}
        return diag._write_state(self.folder, diag.LAUNCH_FILE, state, log)


def read_raw(folder):
    """launch.json as a dict, or None if it is missing, too large or not an
    object."""
    try:
        with open(Path(folder) / diag.LAUNCH_FILE, "rb") as f:
            data = f.read(_RECORD_MAX + 1)
        if len(data) > _RECORD_MAX:
            return None
        raw = json.loads(data.decode("ascii"))
    except (OSError, ValueError, RecursionError):
        return None
    return raw if isinstance(raw, dict) else None


def health_state(folder):
    """For --health (#111 B5b): {"slot": the role the last start opened in,
    or "none" (an ordinary per-user start, a source install, or no record),
    "update_waiting": that start went to the version kept for
    an older workspace, "self_check": "ok", "slot_fault", "env_fault" or
    "unknown"}. Fixed words only; anything else in the file reads as none or
    unknown."""
    book = Book(folder)
    role = book.last_role or "none"
    return {"slot": role, "update_waiting": role == "compat",
            "self_check": book.last_check or "unknown"}


def _text(value, pattern):
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# -- The notice while the full check runs --------------------------------------------

class Notice:
    """A native message box shown while the full check runs and closed by
    the launcher when it ends (WM_CLOSE to the box's window, found on the
    thread that shows it). Closing it early changes nothing: the check goes
    on. Windows only; elsewhere nothing is shown."""

    TITLE = "Unminute"
    TEXT = ("Getting the update of Unminute ready. This takes up to a few minutes, "
            "and this message closes by itself.")

    def __init__(self):
        self._thread = None
        self._tid = None

    def __enter__(self):
        if os.name != "nt":
            return self
        import threading
        ready = threading.Event()

        def show():
            import ctypes
            self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
            ready.set()
            ctypes.windll.user32.MessageBoxW(None, self.TEXT, self.TITLE, 0x40)  # information
        self._thread = threading.Thread(target=show, daemon=True, name="update-notice")
        self._thread.start()
        ready.wait(5)
        return self

    def __exit__(self, *exc):
        if self._thread is None or self._tid is None:
            return False
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = []
        callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(
            lambda hwnd, _: found.append(hwnd) or True)
        for _ in range(50):                     # the box may still be opening
            user32.EnumThreadWindows(self._tid, callback, 0)
            if found or not self._thread.is_alive():
                break
            time.sleep(0.1)
        for hwnd in found:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)                    # WM_CLOSE
        self._thread.join(5)
        return False
