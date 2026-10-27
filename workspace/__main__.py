# workspace/__main__.py
# Start the Unminute window (#20):  python -m workspace
#
# The state-owning process: it installs the offline network guard before
# anything else, owns the database and runs processing on background threads;
# heavy models run in the managed worker process. Models are read from the
# existing local cache (%LOCALAPPDATA%\Unminute\models); nothing is downloaded.
#
# Support options (no window):
#   --version     the installed package version
#   --status      workspace contents as JSON counts (meetings, holds, pending
#                 cleanup, retention reminders), the last database check and
#                 the key's state at the last start (#111 C4a, never by
#                 unlocking it); used by installers and updates (#26).
#                 No content.
#                 If the app data location is refused: {"error": ...}, exit 1.
#   --self-test   import every runtime dependency, check the offline guard,
#                 check that the speaker model loader refuses a code-running
#                 checkpoint (a synthetic one, in memory), and on Windows that
#                 Windows Data Protection protects and unlocks a random key
#                 (the workspace's root key is kept that way, #111 C2b-2)
#   --check-decoder  for IT, run as the signed-in user (#299): the decoder
#                 starts in its AppContainer and decodes a second of synthetic
#                 silence. Prints {"decoder": "ok"}, or "failed" with the
#                 step and Windows error number; exit 0 ok, 1 otherwise. Run
#                 as Local System nothing is started ("system_account"). No
#                 content or paths.
#   --health      for an Intune Remediation detection script run as the
#                 signed-in user (R9b): JSON with the version, whether the
#                 workspace opens (or was locked at its last start), whether the
#                 app is running, how the last run ended and crashes in the
#                 last 7 days. Exit 0 healthy, 1 needs attention. No content
#                 (docs/PACKAGING.md).
#                 Also counts meetings that require action, and held ones
#                 past the threshold (#111 C6), without changing "healthy".
#   --gate        run by the installer from the NEW package before an update:
#                 may this version take over the existing workspace? (#26)
#                 Prints JSON; exit 0 allowed, 5 wait (with reasons), 6 refused.
#   --verify-audit  check the audit log's hash chain (#111 C5), run as the
#                 signed-in user: JSON counts only. --event-log also compares it
#                 with the Unminute entries in the local Application log, and
#                 --event-log-file <path> with an exported .evtx file instead.
#                 Exit 0 when the chain is ok (and matches, if compared), 1
#                 otherwise. Needs no key and reads no database. Run as SYSTEM
#                 it reads nothing: {"result": "system_account"}, exit 1.
#   --restore-lid run by the uninstaller before it removes anything (#191): puts
#                 back a lid action the app left at "Do nothing" (a crash, then
#                 no start), with the app's own rules (power.restore_only).
#                 Reads only power_restore.json; opens no window or database.
#                 Prints {"lid": state}; exit 0 put back or nothing waiting,
#                 1 not put back now, 2 damaged record (left as it is), 3 the
#                 app is open (power.RESTORE_CODES). No content or paths.
#   --offboard-report  what Unminute keeps for this Windows user (#111 B8),
#                 run as the user: one line of JSON with counts and yes or no
#                 (the workspace's state and blockers, models, token, lid
#                 record, other entries in Unminute). Exit 0 nothing to
#                 remove or all removable, 2 run as Local System (or Windows
#                 cannot say who), 3 the app is open, 4 other entries in
#                 Unminute are left alone (counted, never named), 5 blocked (meetings,
#                 holds, cleanup, recordings or audio left), 6 the location
#                 is refused (a link at Unminute or Unminute\workspace).
#   --offboard    removes it when nothing blocks, with the app closed: the lid
#                 is put back, an offboarded audit record goes to the Event
#                 Log, the root key is erased, then the workspace, models and
#                 token are removed (docs/PACKAGING.md, "Offboarding"). Exit
#                 codes as above, and 1 when a step failed (run it again).
#   --launch-check  the light first-start check (#111 B5b), run by the
#                 launcher (machine mode, and per-user since 23 Oct 2026) in
#                 a child process of the version it checks
#                 (workspace/launchcheck.py): prints one JSON line,
#                 {"result": "ok" | "slot_fault" | "env_fault", "part"}. Opens
#                 no window, database or key.
#
# A window start first runs the launcher pass (workspace/launch.py, #111 B5):
# in machine mode it decides which installed version opens this user's
# workspace, checks it (B5b) and may hand over to it, started with --slot
# <role> (update.ROLES; set only by a hand-over). A per-user install opens
# the version started, after only its first-start check (and a fallback to
# another kept version of the same storage format if that version fails it).
# The window then takes the single-instance mutex (this session)
# and the workspace lock (workspace/instance.py, every session of this user).
# A version started as compat (kept for an older workspace that still holds
# meetings) opens it in finish-only mode (B5b): no new imports, watched-folder
# intake or recordings. Only the current version (never compat or fallback)
# removes unused graphics card components in the background (B5-4).

import json
import sqlite3
import sys
import time
from pathlib import Path

_STARTED = time.perf_counter()        # start-up timings (R9): from here, not interpreter start

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace import offline_guard  # noqa: E402

offline_guard.disable_telemetry()
offline_guard.install()

from workspace import credentials  # noqa: E402

# A Hugging Face token left in the user's environment (HF_TOKEN) is taken
# out of this process's environment before anything starts, so no child
# process (workers, WebView2, nvidia-smi) inherits it.
credentials.drop_environment_token()

APP_ID = "Unlimit.Unminute"     # also in packaging/install.ps1: shortcuts group with the window


def model_cache():
    """%LOCALAPPDATA%\\Unminute\\models (refuses a missing or synced location)."""
    from workspace.store import local_appdata
    return local_appdata() / "Unminute" / "models"


def package_version():
    from workspace.update import package_version
    return package_version()


def status(root=None, policy_reader=None):
    """Counts only: never titles, names or text. integrity: the last
    database check (R11), "ok", "failed" or "unchecked". keys: the key's
    state at the app's last start (#111 C4a), "ok", "locked" or "unchecked",
    read from logs/keys.json and never by unlocking the key (no DPAPI call:
    Intune may run this as SYSTEM); lock_reason and lock_code (the Windows
    error number) while locked. audit_seq: the audit log's newest sequence
    number, from its end only (#111 C5). reminders_due, action_required and
    held_overdue: the retention reminders (#111 C6), from plain columns and
    the managed policy, so they need no key. policy_reader: tests only."""
    import sqlite3

    from workspace import audit, diag, holds, policy
    from workspace.store import default_root
    root = Path(root or default_root()).absolute()
    db = root / diag.DB_FILE
    checked = diag.integrity_state(root / diag.LOGS_FOLDER)
    keys = diag.keys_state(root / diag.LOGS_FOLDER)
    locked = bool(keys and keys["result"] == "locked")
    out = {"version": package_version(), "workspace": db.exists(), "meetings": 0,
           "holds": 0, "pending_cleanup": 0, "receipts": 0, "schema": None,
           "integrity": checked["result"] if checked else "unchecked", "readable": True,
           "recordings": _count_folders(root / "recordings"),   # not yet in a meeting
           "keys": keys["result"] if keys else "unchecked",
           "lock_reason": keys["reason"] if locked else None,
           "lock_code": keys["winerror"] if locked else None,
           "audit_seq": audit.tail_seq(root / diag.AUDIT_FOLDER),
           "reminders_due": 0, "action_required": 0, "held_overdue": 0}
    if db.exists():
        con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)   # as_uri: # and % escaped
        try:
            q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
            out.update(meetings=q("SELECT COUNT(*) FROM meetings"),
                       holds=q("SELECT COUNT(*) FROM holds WHERE released_at IS NULL"),
                       pending_cleanup=q("SELECT COUNT(*) FROM cleanup_jobs"),
                       receipts=q("SELECT COUNT(*) FROM receipts"),
                       schema=q("SELECT MAX(version) FROM schema_version"))
            # An unreadable policy gives the app's defaults: these grant nothing.
            managed = policy.Policy(*(policy_reader or policy.read_registry)())
            out.update(holds.retention_counts(holds.retention_from(
                lambda sql: con.execute(sql).fetchall(), managed)))
        except sqlite3.DatabaseError:
            out["readable"] = False             # damaged (or not ours): counts unknown
        finally:
            con.close()
    return out


def _count_folders(folder):
    """Subfolders of folder (a recording each); 0 if there is none."""
    try:
        return sum(1 for p in folder.iterdir() if p.is_dir())
    except OSError:
        return 0


def health(root=None):
    """For device management (an Intune Remediation run as the signed-in
    user): states and counts only, and never an error message. Needs
    attention when the workspace cannot be used (including when its last
    database check found damage), when run as SYSTEM (which sees no user's
    workspace, and must never run the user's own install), or when something
    crashed in the last 7 days (including a run that ended without closing,
    which the next start records as a crash). The launcher's fields (#111
    B5b, from logs\\launch.json): slot (the role the last start
    opened in, or "none"), update_waiting (it opened the version kept for an
    older workspace: workload, still healthy) and self_check; a fallback or
    a fault needs attention."""
    # action_required and held_overdue (#111 C6): counts of meetings, not app
    # faults, so they leave healthy alone; null when not counted (for
    # example run as SYSTEM), never a false "no backlog".
    out = {"version": "?", "workspace": "unreadable", "running": False,
           "last_run": "clean", "crashes_7d": 0, "schema": None,
           "action_required": None, "held_overdue": None,
           "slot": "none", "update_waiting": False, "self_check": "unknown"}
    try:
        _health(out, root)
    except Exception:                           # never a traceback (it holds folders)
        out["workspace"] = "unreadable"
    out["healthy"] = (out["workspace"] in ("ok", "none") and out["crashes_7d"] == 0
                      and out["last_run"] == "clean" and out["slot"] != "fallback"
                      and out["self_check"] not in ("slot_fault", "env_fault"))
    return out


def _health(out, root):
    from workspace import diag
    from workspace.store import StoreError, default_root
    out["version"] = diag._clean("version", package_version())   # read from the install
    account = _system_account()
    if account is None:                         # cannot tell: needs attention, never healthy
        return                                  # (workspace stays "unreadable")
    if account:
        out["workspace"] = "system_account"
        return
    try:
        root = Path(root or default_root())
    except StoreError:
        out["workspace"] = "location"           # missing, synced, redirected or broken (#219, #266)
        return
    logs = root / diag.LOGS_FOLDER
    from workspace import launchcheck
    out.update(launchcheck.health_state(logs))  # fixed words only (#111 B5b)
    marker = logs / f"app{diag.RUNNING_SUFFIX}"
    left = marker.exists()                      # before asking whether it runs
    owner = diag.run_owner_alive(marker) if left else None
    out["running"] = bool(owner) or _app_running()
    if left and not out["running"]:
        out["last_run"] = "did_not_close"
    out["crashes_7d"] = diag.recent_crashes(logs)
    try:
        counts = status(root)
    except Exception:
        out["workspace"] = "unreadable"
        return
    if not counts["readable"]:
        out["workspace"] = "unreadable"
        return
    out["workspace"] = "ok" if counts["workspace"] else "none"   # none: never opened here
    if counts["workspace"] and counts["keys"] == "locked":
        # Locked at the last start (#111 C4a), read from keys.json: this
        # never unlocks the key. Ranks after unreadable, before damaged.
        out["workspace"] = "locked"
    elif counts["integrity"] == "failed":
        out["workspace"] = "damaged"            # the last database check found damage
    out["schema"] = counts["schema"]
    out["action_required"] = counts["action_required"]
    out["held_overdue"] = counts["held_overdue"]


def _app_running(name=None):
    """Is the app open in this session (its single-instance mutex exists)?
    Never creates it. A copy run as administrator holds one this process may
    not open: that is running too, as in _single_instance."""
    import ctypes
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenMutexW.restype = ctypes.c_void_p
        handle = kernel32.OpenMutexW(0x00100000, False, name or MUTEX_NAME)   # SYNCHRONIZE
        error = ctypes.get_last_error()
    except (AttributeError, OSError):
        return False
    if handle:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return True
    return error == 5                           # ERROR_ACCESS_DENIED


_UNREAD = object()                              # _system_account: read the token itself


def _system_account(user=_UNREAD):
    """Is this process running as Local System (S-1-5-18)? Intune runs a
    script there unless it is set to use the logged-on credentials. None if
    Windows cannot say (so --health never reports healthy on a guess); False
    off Windows. The token's user is read by audit.token_user, which the
    audit log's Event Log copy also uses (#111 C5), unless the caller passes
    the one it read."""
    if user is _UNREAD:
        from workspace import audit
        user = audit.token_user()
    if user is None or user is False:
        return user
    return user[1] == "S-1-5-18"


def verify_audit(argv, root=None):
    """--verify-audit (#111 C5): (result, exit code). Counts and states only.
    Run as SYSTEM, or when Windows cannot say who runs it, nothing is read.
    The token's user is read once: the same answer refuses SYSTEM and filters
    the local Event Log by SID (review round 3)."""
    from workspace import audit, diag
    user = audit.token_user()
    account = _system_account(user)
    if account is None:
        return {"result": "account_unknown"}, 1
    if account:
        return {"result": "system_account"}, 1
    from workspace.store import StoreError, default_root
    try:
        folder = Path(root or default_root()) / diag.AUDIT_FOLDER
    except StoreError:
        return {"result": "location"}, 1    # missing, synced, redirected or broken (#219, #266)
    out = audit.verify(folder)
    ok = out["result"] == "ok"
    source = None
    if "--event-log-file" in argv:
        at = argv.index("--event-log-file") + 1
        if at >= len(argv) or not Path(argv[at]).is_file():
            return {**out, "event_log": {"result": "no_file"}}, 1
        source = str(Path(argv[at]).absolute())
    if source or "--event-log" in argv:
        # The local Application log holds every account's entries on this
        # computer: only the signed-in user's are compared, by the SID read
        # above; with none (off Windows) the log is not read, never compared
        # unfiltered. An exported file is compared whole (another device's
        # chains are other_chains).
        if source:
            out["event_log"] = audit.compare_event_log(folder, source=source)
        elif user:
            out["event_log"] = audit.compare_event_log(folder, sid=user[1])
        else:
            out["event_log"] = {"result": "account_unknown"}
        ok = ok and out["event_log"]["result"] == "match"
    return out, 0 if ok else 1


def offboard(argv):
    """--offboard-report and --offboard (#111 B8): (result, exit code). Run
    as Local System, or when Windows cannot say who runs it, nothing is
    read. A location the app itself refuses (synced, on a network drive, a
    broken link) is refused here too. --offboard holds the app's
    single-instance mutex while it runs, so the app cannot start in this
    session meanwhile."""
    import os

    from workspace import audit
    from workspace import offboard as ob
    account = _system_account()
    if account is None:
        return {"result": "account_unknown"}, ob.ACCOUNT
    if account:
        return {"result": "system_account"}, ob.ACCOUNT
    from workspace.store import StoreError, default_root, local_appdata
    try:
        default_root()                          # the app's own refusals (#219, #266)
        appdata = local_appdata()
    except StoreError:
        return {"result": "refused"}, ob.REFUSED
    remove = "--offboard" in argv
    if remove:
        held = _single_instance()
        session_running = (lambda: True) if held is None else (lambda: False)
    else:
        session_running = _app_running
    mirror = audit.EventLogMirror() if os.name == "nt" else None
    return ob.run(ob.Where(appdata), remove, account=False, session_running=session_running,
                  mirror=mirror)


def restore_lid():
    """--restore-lid (#191): the lid restore only, at the workspace the app
    itself uses. Returns a state in power.RESTORE_CODES; never raises."""
    try:
        from workspace import power
        from workspace.store import default_root
        try:
            root = default_root()               # the same refusals as the app's own start
        except Exception:
            return "location"
        return power.restore_only(root / power.RESTORE_FILE, _app_running)
    except Exception:
        return "failed"


def self_test():
    """Import every dependency the app needs, prove the guard blocks the
    network and that a code-running speaker checkpoint is refused. Prints one
    line per check; exit code 0 only if all pass."""
    import importlib
    import socket
    # No DLL folder named by the environment (#357, #358, T25): CTranslate2,
    # PyTorch and TorchCodec, imported below, add folders from outside the app
    # to the DLL search path when they load (HIP_PATH, PYTHONUSERBASE,
    # NVTOOLSEXT_PATH, CUDA_PATH_V*, an ffmpeg on PATH). The launcher starts
    # this check on its own before a migration (#111 B5b), so it clears the
    # same environment every worker does, before any speech or model library
    # loads (numpy, which workspace.worker imports, comes first and adds only
    # its own numpy.libs folder) and before anything reads sysconfig, which
    # keeps the user base PyTorch reads (workspace.diag does, so it is not
    # imported before this); if it cannot, it imports nothing more and fails.
    try:
        from workspace.worker import clear_dll_path_environment
        clear_dll_path_environment()
    except Exception as exc:
        print(f"FAILED  DLL path variables could not be cleared ({type(exc).__name__})")
        print("FAIL")
        return 1
    ok = True
    offline_guard.disable_onnxruntime_telemetry()   # before onnxruntime is used below
    for name in ("numpy", "av", "onnxruntime", "onnx_asr", "faster_whisper", "ctranslate2",
                 "torch", "pyannote.audio", "huggingface_hub", "webview",
                 "webview.platforms.edgechromium", "clr_loader",
                 "sounddevice",                    # loads PortAudio (microphone, #30)
                 "truststore",                     # Windows certificates for downloads
                 "workspace.app", "workspace.api", "workspace.asr_engines"):
        try:
            importlib.import_module(name)
            print(f"ok      import {name}")
        except Exception as exc:              # report every failure, not just the first
            ok = False
            print(f"FAILED  import {name}: {type(exc).__name__}: {exc}")
    # The speaker model loader must refuse a code-running checkpoint and ones
    # that change a shared Enum member's attributes or its class (#218, #231):
    # synthetic ones, built in memory, with harmless payloads.
    try:
        from workspace import speaker_load
        checks = speaker_load.self_check()
    except Exception as exc:
        checks = [(False, f"speaker model loading checks could not run ({type(exc).__name__})")]
    for passed, text in checks:
        ok = ok and passed
        print(f"{'ok     ' if passed else 'FAILED '} {text}")
    if sys.platform == "win32":
        passed, text = dpapi_round_trip()
        ok = ok and passed
        print(f"{'ok     ' if passed else 'FAILED '} {text}")
    # The guard must stop each kind of attempt before it reaches Windows. The
    # target is 192.0.2.1 (TEST-NET-1, reserved for documentation): if the
    # guard ever failed, nothing real would be contacted and no DNS lookup
    # would leave the computer.
    import asyncio

    def tcp():
        socket.create_connection(("192.0.2.1", 443), timeout=3).close()

    def raw_tcp():                    # straight to connect(), no name lookup
        with socket.socket() as s:
            s.settimeout(3)
            s.connect(("192.0.2.1", 443))

    def udp():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(b"x", ("192.0.2.1", 53))

    def async_tcp():
        async def attempt():
            _, writer = await asyncio.wait_for(asyncio.open_connection("192.0.2.1", 443), 3)
            writer.close()
        asyncio.run(attempt())

    for label, attempt in (("TCP", tcp), ("raw TCP", raw_tcp), ("UDP", udp),
                           ("asyncio TCP", async_tcp)):
        try:
            attempt()
            ok = False
            print(f"FAILED  network guard: {label} reached the network")
        except offline_guard.NetworkBlockedError:
            print(f"ok      network guard blocks {label}")
        except OSError as exc:              # incl. TimeoutError: reached Windows, not blocked
            ok = False
            print(f"FAILED  network guard did not intercept {label} ({type(exc).__name__})")
    # The app starts with -X disable-remote-debug (Python 3.14's debugger
    # interface, PEP 768). A support run started without it says so; not a failure.
    if getattr(sys, "is_remote_debug_enabled", lambda: False)():
        print("note    remote debugging is on in this run (the app itself starts with "
              "-X disable-remote-debug)")
    else:
        print("ok      remote debugging off")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def dpapi_round_trip():
    """(passed, line): Windows Data Protection protects 32 random bytes for
    this user and unlocks the same bytes (vault.seal_root and open_root, with
    the workspace key's entropy). The line names the Windows error code only."""
    import os
    try:
        from workspace import vault
        data = os.urandom(vault.KEY_BYTES)
        if vault.open_root(vault.seal_root(data)) != data:
            return False, "dpapi round trip: different bytes came back"
        return True, "dpapi round trip"
    except Exception as exc:
        return False, f"dpapi round trip: {getattr(exc, 'code', type(exc).__name__)}"


class _Silence:
    """One second of digital silence at 16 kHz, as recorder.RecordingAudio
    gives a recording to media.WavFeed (rate, frames, blocks())."""
    rate, frames = 16000, 16000

    def blocks(self):
        yield b"\0" * (2 * self.frames)


def check_decoder(account=_UNREAD):
    """--check-decoder (#299): (result, exit code). The decoder starts in its
    AppContainer and decodes one second of synthetic silence fed over its
    stdin, so no file is involved. The result names the failed step and the
    Windows error number only (P3). Run as Local System, or when Windows
    cannot say who runs it, nothing is started: the container's profile
    belongs to the signed-in user."""
    import io
    account = _system_account() if account is _UNREAD else account
    if account is None:
        return {"decoder": "account_unknown"}, 1
    if account:
        return {"decoder": "system_account"}, 1
    from workspace import media
    try:
        samples = media.decode(None, io.BytesIO(), feed=media.WavFeed(_Silence()))
    except media.SandboxUnavailable as exc:
        return {"decoder": "failed", "step": exc.step, "winerror": exc.winerror}, 1
    except Exception:                       # never a message: it could hold a path
        return {"decoder": "failed", "step": "decode"}, 1
    if abs(samples - _Silence.frames) > 160:
        return {"decoder": "failed", "step": "decode"}, 1
    return {"decoder": "ok"}, 0


def main():
    if "--version" in sys.argv:
        print(package_version())
        return 0
    if "--restore-lid" in sys.argv:
        from workspace.power import RESTORE_CODES
        state = restore_lid()
        print(json.dumps({"lid": state}))
        return RESTORE_CODES[state]
    if "--offboard-report" in sys.argv or "--offboard" in sys.argv:
        try:
            result, code = offboard(sys.argv)
        except Exception:                   # never a traceback (it holds folders)
            result, code = {"result": "failed", "step": "unexpected"}, 1
        print(json.dumps(result))
        return code
    from workspace.store import StoreError
    if "--status" in sys.argv:
        try:
            print(json.dumps(status()))
        except StoreError as exc:           # missing, synced, redirected or broken (#219, #266)
            print(json.dumps({"error": str(exc)}))
            return 1
        return 0
    if "--self-test" in sys.argv:
        return self_test()
    if "--launch-check" in sys.argv:
        from workspace import launchcheck
        result = launchcheck.light_check(dpapi_round_trip)
        print(json.dumps(result))
        return 0 if result["result"] == "ok" else 1
    if "--check-decoder" in sys.argv:
        result, code = check_decoder()
        print(json.dumps(result))
        return code
    if "--verify-audit" in sys.argv:
        result, code = verify_audit(sys.argv)
        print(json.dumps(result))
        return code
    if "--health" in sys.argv:
        result = health()                   # never raises: no traceback on stderr
        print(json.dumps(result))
        return 0 if result["healthy"] else 1
    if "--gate" in sys.argv:
        from workspace import diag, update
        from workspace.store import SCHEMA_VERSION, default_root
        schema = SCHEMA_VERSION
        if "--schema" in sys.argv:          # for a version too old to check itself
            schema = int(sys.argv[sys.argv.index("--schema") + 1])
        try:
            result = update.gate(default_root() / diag.DB_FILE, schema)
        except StoreError as exc:
            result = {"code": update.TOO_NEW, "kind": "location", "current_schema": None,
                      "blockers": [], "message": str(exc)}
        print(json.dumps(result))
        return result["code"]
    # The launcher pass (#111 B5): the decision in machine mode only; a
    # per-user install runs only the first-start check (B5b). Nothing heavy
    # is imported yet.
    from workspace import instance, launch
    outcome = launch.start(sys.argv)
    if outcome.exit is not None:
        if outcome.message:
            _message(outcome.message)
        return outcome.exit
    # The window's backend (.NET and WebView2's assemblies) loads on a
    # background thread from here, alongside the imports and the workspace
    # opening below (#339); the window waits for it (shell.preload_backend).
    from workspace import shell
    shell.preload_backend()
    from workspace import api, diag
    from workspace.app import Workspace
    imported = time.perf_counter()
    mutex = _single_instance()
    if mutex is None:
        _message("Unminute is already open. Look for its window on the taskbar.")
        return 0
    try:
        # check_integrity: after an unclean exit (this run's marker is not
        # written yet, diag.install below), the database is checked (R11).
        # on_unavailable="lock" (#111 C4a): a key Windows will not unlock
        # opens the workspace locked (nothing read but counts, nothing
        # written, every request refused) rather than closing. The protector
        # is never chosen here: the store's own (Windows Data Protection).
        # instance_lock (#111 B5): the workspace lock, which every Windows
        # session of this user sees (the mutex above is this session's only).
        # finish_only (#111 B5b): a version started as compat, kept for an
        # older workspace that still holds meetings, opens it for finishing
        # them only, so it can empty and the update can take over.
        ws = Workspace(cache_dir=model_cache(), check_integrity=True, on_unavailable="lock",
                       instance_lock=True, finish_only=outcome.role == "compat")
    except instance.Held as exc:
        _message(str(exc))                  # open in another session of this user
        return 0
    except (StoreError, sqlite3.DatabaseError) as exc:
        # e.g. a workspace from a newer version, one that cannot be migrated,
        # or a damaged database: explain instead of disappearing (pythonw
        # has no console).
        _message(_open_failed(exc))
        return 1
    # Content-free diagnostics (R9): uncaught errors on any thread and native
    # crashes are recorded in workspace\logs\app.log from here on.
    unclean = diag.install(ws.log, others=("worker", "gpu-worker", "draft-worker", "setup"))
    diag.summarise_output(ws.log)         # pythonw: no console text is kept, only type names
    ws.log.write("start", version=package_version(), python=sys.version.split()[0],
                 previous_run_unclean=unclean)
    if outcome.role in (None, "current"):
        # Unused graphics card components (#111 B5b, B5-4): the current
        # version only, in the background after start-up.
        ws.start_gpu_upkeep()
    # A current.json that claims machine mode for an install that fails the machine
    # trust test (#111 B6b): treated as per-user, and recorded (no path or name).
    from workspace import update
    if update.deployment().get("machine_refused"):
        ws.log.write("machine_claim_refused")
    opened = time.perf_counter()
    try:
        # Our own taskbar identity (matches the shortcut, set by install.ps1),
        # so the window shows the app's icon instead of Python's.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass
    holder = {}
    window = shell.create_window(api.make_api(ws, lambda: holder.get("window")),
                                 allowed=api.API_METHODS)
    holder["window"] = window
    window.events.loaded += lambda: api.enable_drop(window, ws)

    def timing():
        # Seconds from the start of this module: imports, opening the
        # workspace, and the page loaded (the window is interactive). Once.
        if getattr(timing, "done", False):
            return
        timing.done = True
        now = time.perf_counter()
        ws.log.write("timing", imports_s=imported - _STARTED, workspace_s=opened - imported,
                     page_loaded_s=now - _STARTED)
    window.events.loaded += timing

    import threading
    import time as _time
    asked = {"at": 0.0}

    def allow_and_close():
        window.unminute_may_close = True
        window.destroy()

    def ask():
        # The page answers true when it shows the question; otherwise (page not
        # loaded, an error) the window closes and ws.close() saves the recording.
        # A window closed from the taskbar while minimised is brought forward
        # first, so the question can be seen.
        try:
            shell.bring_to_front(window)
        except Exception:
            pass
        try:
            shown = window.evaluate_js("window.workspace.askClose()")
        except Exception:
            shown = False
        if not shown:
            allow_and_close()

    def closing():
        # A recording is open: keep the window and let the page ask (Stop and
        # save, Discard, Keep recording). The page closes it with close_app. A
        # second close within 10 s closes anyway (the recording is then saved).
        if getattr(window, "unminute_may_close", False) or not ws.recording_active():
            return True
        now = _time.monotonic()
        if now - asked["at"] < 10:
            return True
        asked["at"] = now
        threading.Thread(target=ask, daemon=True, name="ask-close").start()
        return False
    window.events.closing += closing

    hooked = {"done": False}

    def never_block_shutdown():
        # Windows shutdown, sign-out or Task Manager must not wait for the
        # question: a second handler on the form (it runs after pywebview's)
        # lets those closes through; ws.close() then stops and saves the recording.
        if hooked["done"]:
            return
        hooked["done"] = True
        try:
            from System.Windows.Forms import CloseReason   # pythonnet, loaded by the window

            def on_form_closing(sender, args):
                ending = (CloseReason.WindowsShutDown, CloseReason.TaskManagerClosing)
                if args.CloseReason in ending:
                    window.unminute_may_close = True
                    args.Cancel = False
            window.native.FormClosing += on_form_closing
        except Exception:
            pass
    window.events.shown += never_block_shutdown

    def quiet_when_minimised():
        # Minimised: WebView2 stops rendering; with nothing recording the page
        # is suspended, and while recording it runs at low memory
        # (docs/RELEASE_STANDARD.md). _capturing counts a recording that is
        # still opening, so its page is never suspended.
        try:
            shell.watch_minimise(window, ws._capturing)
        except Exception:
            pass                           # still works, just not as quietly
    window.events.shown += quiet_when_minimised
    try:
        # The WebView2 profile lives inside the workspace (and is wiped at the
        # next start if a crash left it), not in %TEMP%.
        shell.run(storage_path=shell.storage_folder(ws.root))
    except shell.ShellError as exc:
        _message(str(exc))
        return 1
    finally:
        try:
            ws.close()
        finally:
            ws.log.write("stop")
            ws.log.close_run()             # a clean end: not a crash next time
    if shell.lock_failed:
        _message("Unminute closed its window because it could not secure it. "
                 "Update the Microsoft Edge WebView2 Runtime or reinstall Unminute, "
                 "or ask your IT team.")
        return 1
    return 0


MUTEX_NAME = "Local\\Unminute.Workspace"


def _single_instance(name=MUTEX_NAME):
    """Hold a named mutex for the life of the process, so a second copy never
    opens the same workspace (or clears the first copy's window data). Returns
    the handle, or None if another copy holds it. If Windows cannot create the
    mutex for another reason, the app still starts."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, name)
    error = ctypes.get_last_error()
    if handle and error == 183:                    # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    if not handle and error == 5:                  # held by a copy run as administrator
        return None
    return handle or True


def _open_failed(exc):
    """The message when the workspace will not open. A key that cannot be
    set up is said so, with the Windows error number and a code: with
    on_unavailable="lock" (#111 C4a) a workspace with content opens locked,
    so a KeyUnavailable that reaches here is a new or empty workspace whose
    key Windows would not protect. A damaged database is said so, with a
    code, and kept for --status and --health (R11); anything else gives the
    store's own message."""
    from workspace import diag
    from workspace.store import KeyUnavailable, StoreError, default_root, quick_check_file
    try:
        root = default_root()
    except StoreError:
        return str(exc)
    db, logs = root / diag.DB_FILE, root / diag.LOGS_FOLDER
    log = diag.Log(logs, "app")
    if isinstance(exc, KeyUnavailable):
        # The record holds the reason and the Windows error number only.
        code = diag.new_code()
        log.write("key_unavailable", code=code, reason=exc.reason, winerror=exc.code)
        error = (f"error {exc.code}, " if isinstance(exc.code, int)
                 and not isinstance(exc.code, bool) else "")
        return (f"Unminute could not set up its encryption key with Windows ({error}code "
                f"{code}). Ask your IT team: the steps are in the Unminute user guide, under "
                f"\"If Unminute cannot unlock its workspace\".")
    if db.exists() and quick_check_file(db) is False:
        last = diag.integrity_state(logs)
        if last and last["result"] == "failed" and last["code"]:
            code = last["code"]                 # recorded already (this start's own check)
        else:
            code = diag.new_code()
            diag.record_integrity(logs, "failed", code, log=log)
            log.write("integrity", result="failed", code=code)
        return (f"Unminute found damage in its workspace database and cannot open it "
                f"(code {code}). Ask your IT team: the steps are in the Unminute user "
                f"guide, under \"If Unminute finds damage\".")
    if isinstance(exc, StoreError):
        return str(exc)
    return f"Unminute could not open its workspace (code {log.incident('open', exc)})."


def _message(text):
    """A Windows message box: pythonw has no console to print to."""
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, text, "Unminute", 0x10)


if __name__ == "__main__":
    sys.exit(main())
