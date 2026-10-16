# workspace/__main__.py
# Start the Unminute window (#20):  python -m workspace
#
# The state-owning process: it installs the offline network guard before
# anything else, owns the database and runs processing on background threads;
# heavy models run in the managed worker process. Models are read from the
# existing local cache (%LOCALAPPDATA%\whisper-models); nothing is downloaded.
#
# Support options (no window):
#   --version     the installed package version
#   --status      workspace contents as JSON counts (meetings, holds, pending
#                 cleanup); used by installers and updates (#26). No content.
#   --self-test   import every runtime dependency and check the offline guard
#   --gate        run by the installer from the NEW package before an update:
#                 may this version take over the existing workspace? (#26)
#                 Prints JSON; exit 0 allowed, 5 wait (with reasons), 6 refused.

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import offline_guard  # noqa: E402

offline_guard.disable_telemetry()
offline_guard.install()

from workspace import credentials  # noqa: E402

# A Hugging Face token the earlier app left in the user's environment is taken
# out of this process's environment before anything starts, so no child
# process (workers, WebView2, nvidia-smi) inherits it.
credentials.adopt_earlier_token()

APP_ID = "WhisperTranscriber.MeetingWorkspace"     # also in packaging/install.ps1; kept from
                                                   # the earlier name so taskbar pins still group


def model_cache():
    """%LOCALAPPDATA%\\whisper-models (refuses a missing or synced location)."""
    from workspace.store import local_appdata
    return local_appdata() / "whisper-models"


def package_version():
    """version.json beside app/ in an installed package, else "source"."""
    path = Path(__file__).resolve().parents[2] / "version.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "source"


def status(root=None):
    """Counts only: never titles, names or text."""
    import sqlite3

    from workspace.store import default_root
    db = Path(root or default_root()) / "workspace.db"
    out = {"version": package_version(), "workspace": db.exists(), "meetings": 0,
           "holds": 0, "pending_cleanup": 0, "receipts": 0, "schema": None}
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
            out.update(meetings=q("SELECT COUNT(*) FROM meetings"),
                       holds=q("SELECT COUNT(*) FROM holds WHERE released_at IS NULL"),
                       pending_cleanup=q("SELECT COUNT(*) FROM cleanup_jobs"),
                       receipts=q("SELECT COUNT(*) FROM receipts"),
                       schema=q("SELECT MAX(version) FROM schema_version"))
        finally:
            con.close()
    return out


def self_test():
    """Import every dependency the app needs and prove the guard blocks the
    network. Prints one line per check; exit code 0 only if all pass."""
    import importlib
    import socket
    ok = True
    offline_guard.disable_onnxruntime_telemetry()   # before onnxruntime is used below
    for name in ("numpy", "av", "onnxruntime", "onnx_asr", "faster_whisper", "ctranslate2",
                 "torch", "pyannote.audio", "huggingface_hub", "webview", "clr_loader",
                 "sounddevice",                    # loads PortAudio (microphone, #30)
                 "workspace.app", "workspace.api", "asr_engines"):
        try:
            importlib.import_module(name)
            print(f"ok      import {name}")
        except Exception as exc:              # report every failure, not just the first
            ok = False
            print(f"FAILED  import {name}: {type(exc).__name__}: {exc}")
    try:
        socket.create_connection(("example.com", 443), timeout=5).close()
        ok = False
        print("FAILED  network guard: a connection succeeded")
    except offline_guard.NetworkBlockedError:
        print("ok      network guard blocks outside connections")
    except OSError as exc:      # e.g. no internet: says nothing about the guard
        ok = False
        print(f"FAILED  network guard did not intercept ({type(exc).__name__})")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    if "--version" in sys.argv:
        print(package_version())
        return 0
    if "--status" in sys.argv:
        print(json.dumps(status()))
        return 0
    if "--self-test" in sys.argv:
        return self_test()
    if "--gate" in sys.argv:
        from workspace import update
        from workspace.store import SCHEMA_VERSION, default_root
        schema = SCHEMA_VERSION
        if "--schema" in sys.argv:          # for a version too old to check itself
            schema = int(sys.argv[sys.argv.index("--schema") + 1])
        result = update.gate(default_root() / "workspace.db", schema)
        print(json.dumps(result))
        return result["code"]
    from workspace import api, shell
    from workspace.app import Workspace
    from workspace.store import StoreError
    try:
        ws = Workspace(cache_dir=model_cache())
    except StoreError as exc:
        # e.g. a workspace from a newer version, or one that cannot be migrated:
        # explain instead of disappearing (pythonw has no console).
        _message(str(exc))
        return 1
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

    import threading
    import time as _time
    asked = {"at": 0.0}

    def allow_and_close():
        window.unminute_may_close = True
        window.destroy()

    def ask():
        # The page answers true when it shows the question; otherwise (page not
        # loaded, an error) the window closes and ws.close() saves the recording.
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
    try:
        # The WebView2 profile lives inside the workspace (and is wiped at the
        # next start if a crash left it), not in %TEMP%.
        shell.run(storage_path=shell.storage_folder(ws.root))
    except shell.ShellError as exc:
        _message(str(exc))
        return 1
    finally:
        ws.close()
    return 0


def _message(text):
    """A Windows message box: pythonw has no console to print to."""
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, text, "Unminute", 0x10)


if __name__ == "__main__":
    sys.exit(main())
