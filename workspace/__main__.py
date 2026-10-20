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
#                 cleanup) and the last database check; used by installers and
#                 updates (#26). No content.
#                 If the app data location is refused: {"error": ...}, exit 1.
#   --self-test   import every runtime dependency, check the offline guard, and
#                 check that the speaker model loader refuses a code-running
#                 checkpoint (a synthetic one, in memory)
#   --health      for an Intune Remediation detection script run as the
#                 signed-in user (R9b): JSON with the version, whether the
#                 workspace opens, whether the app is running, how the last run
#                 ended and crashes in the last 7 days. Exit 0 healthy, 1 needs
#                 attention. No content (docs/PACKAGING.md).
#   --gate        run by the installer from the NEW package before an update:
#                 may this version take over the existing workspace? (#26)
#                 Prints JSON; exit 0 allowed, 5 wait (with reasons), 6 refused.
#   --restore-lid run by the uninstaller before it removes anything (#191): puts
#                 back a lid action the app left at "Do nothing" (a crash, then
#                 no start), with the app's own rules (power.restore_only).
#                 Reads only power_restore.json; opens no window or database.
#                 Prints {"lid": state}; exit 0 put back or nothing waiting,
#                 1 not put back now, 2 damaged record (left as it is), 3 the
#                 app is open (power.RESTORE_CODES). No content or paths.

import json
import sqlite3
import sys
import time
from pathlib import Path

_STARTED = time.perf_counter()        # start-up timings (R9): from here, not interpreter start

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import offline_guard  # noqa: E402

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


def status(root=None):
    """Counts only: never titles, names or text. integrity: the last
    database check (R11), "ok", "failed" or "unchecked"."""
    import sqlite3

    from workspace import diag
    from workspace.store import default_root
    root = Path(root or default_root()).absolute()
    db = root / "workspace.db"
    checked = diag.integrity_state(root / "logs")
    out = {"version": package_version(), "workspace": db.exists(), "meetings": 0,
           "holds": 0, "pending_cleanup": 0, "receipts": 0, "schema": None,
           "integrity": checked["result"] if checked else "unchecked", "readable": True,
           "recordings": _count_folders(root / "recordings")}   # not yet in a meeting
    if db.exists():
        con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)   # as_uri: # and % escaped
        try:
            q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
            out.update(meetings=q("SELECT COUNT(*) FROM meetings"),
                       holds=q("SELECT COUNT(*) FROM holds WHERE released_at IS NULL"),
                       pending_cleanup=q("SELECT COUNT(*) FROM cleanup_jobs"),
                       receipts=q("SELECT COUNT(*) FROM receipts"),
                       schema=q("SELECT MAX(version) FROM schema_version"))
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
    which the next start records as a crash)."""
    out = {"version": "?", "workspace": "unreadable", "running": False,
           "last_run": "clean", "crashes_7d": 0, "schema": None}
    try:
        _health(out, root)
    except Exception:                           # never a traceback (it holds folders)
        out["workspace"] = "unreadable"
    out["healthy"] = (out["workspace"] in ("ok", "none") and out["crashes_7d"] == 0
                      and out["last_run"] == "clean")
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
    logs = root / "logs"
    marker = logs / "app.running"
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
    if counts["integrity"] == "failed":
        out["workspace"] = "damaged"            # the last database check found damage
    out["schema"] = counts["schema"]


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


def _system_account():
    """Is this process running as Local System (S-1-5-18)? Intune runs a
    script there unless it is set to use the logged-on credentials. None if
    Windows cannot say (so --health never reports healthy on a guess); False
    off Windows."""
    import ctypes
    from ctypes import wintypes
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except AttributeError:                      # not Windows (tests on Linux)
        return False
    except OSError:
        return None
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE))
    advapi32.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    advapi32.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_wchar_p))
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008,   # TOKEN_QUERY
                                     ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        buffer = ctypes.create_string_buffer(size.value)
        if not size.value or not advapi32.GetTokenInformation(
                token, 1, buffer, size, ctypes.byref(size)):
            return None
        sid = ctypes.c_void_p.from_buffer(buffer).value   # TOKEN_USER.User.Sid
        text = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return None
        try:
            return text.value == "S-1-5-18"
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


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
        return power.restore_only(root / "power_restore.json", _app_running)
    except Exception:
        return "failed"


def self_test():
    """Import every dependency the app needs, prove the guard blocks the
    network and that a code-running speaker checkpoint is refused. Prints one
    line per check; exit code 0 only if all pass."""
    import importlib
    import socket
    ok = True
    offline_guard.disable_onnxruntime_telemetry()   # before onnxruntime is used below
    for name in ("numpy", "av", "onnxruntime", "onnx_asr", "faster_whisper", "ctranslate2",
                 "torch", "pyannote.audio", "huggingface_hub", "webview",
                 "webview.platforms.edgechromium", "clr_loader",
                 "sounddevice",                    # loads PortAudio (microphone, #30)
                 "truststore",                     # Windows certificates for downloads
                 "workspace.app", "workspace.api", "asr_engines"):
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


def main():
    if "--version" in sys.argv:
        print(package_version())
        return 0
    if "--restore-lid" in sys.argv:
        from workspace.power import RESTORE_CODES
        state = restore_lid()
        print(json.dumps({"lid": state}))
        return RESTORE_CODES[state]
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
    if "--health" in sys.argv:
        result = health()                   # never raises: no traceback on stderr
        print(json.dumps(result))
        return 0 if result["healthy"] else 1
    if "--gate" in sys.argv:
        from workspace import update
        from workspace.store import SCHEMA_VERSION, default_root
        schema = SCHEMA_VERSION
        if "--schema" in sys.argv:          # for a version too old to check itself
            schema = int(sys.argv[sys.argv.index("--schema") + 1])
        try:
            result = update.gate(default_root() / "workspace.db", schema)
        except StoreError as exc:
            result = {"code": update.TOO_NEW, "kind": "location", "current_schema": None,
                      "blockers": [], "message": str(exc)}
        print(json.dumps(result))
        return result["code"]
    from workspace import api, diag, shell
    from workspace.app import Workspace
    imported = time.perf_counter()
    instance = _single_instance()
    if instance is None:
        _message("Unminute is already open. Look for its window on the taskbar.")
        return 0
    try:
        # check_integrity: after an unclean exit (this run's marker is not
        # written yet, diag.install below), the database is checked (R11).
        ws = Workspace(cache_dir=model_cache(), check_integrity=True)
    except (StoreError, sqlite3.DatabaseError) as exc:
        # e.g. a workspace from a newer version, one that cannot be migrated,
        # or a damaged database: explain instead of disappearing (pythonw
        # has no console).
        _message(_open_failed(exc))
        return 1
    # Content-free diagnostics (R9): uncaught errors on any thread and native
    # crashes are recorded in workspace\logs\app.log from here on.
    unclean = diag.install(ws.log, others=("worker", "gpu-worker", "setup"))
    diag.summarise_output(ws.log)         # pythonw: no console text is kept, only type names
    ws.log.write("start", version=package_version(), python=sys.version.split()[0],
                 previous_run_unclean=unclean)
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
    """The message when the workspace will not open. A damaged database is
    said so, with a code, and kept for --status and --health (R11); anything
    else gives the store's own message."""
    from workspace import diag
    from workspace.store import StoreError, default_root, quick_check_file
    try:
        root = default_root()
    except StoreError:
        return str(exc)
    db, logs = root / "workspace.db", root / "logs"
    log = diag.Log(logs, "app")
    if db.exists() and quick_check_file(db) is False:
        last = diag.integrity_state(logs)
        if last and last["result"] == "failed" and last["code"]:
            code = last["code"]                 # recorded already (this start's own check)
        else:
            code = diag.new_code()
            diag.record_integrity(logs, "failed", code)
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
