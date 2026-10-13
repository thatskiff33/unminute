# workspace/__main__.py
# Start the meeting workspace window (#20):  python -m workspace
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

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import offline_guard  # noqa: E402

offline_guard.disable_telemetry()
offline_guard.install()

MODEL_CACHE = Path(os.environ.get("LOCALAPPDATA", os.getcwd())) / "whisper-models"


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
    for name in ("numpy", "av", "onnxruntime", "onnx_asr", "faster_whisper", "ctranslate2",
                 "torch", "pyannote.audio", "huggingface_hub", "webview", "clr_loader",
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
    except OSError:
        print("ok      network guard blocks outside connections")
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
    from workspace import api, shell
    from workspace.app import Workspace
    ws = Workspace(cache_dir=MODEL_CACHE)
    holder = {}
    window = shell.create_window(api.make_api(ws, lambda: holder.get("window")),
                                 allowed=api.API_METHODS)
    holder["window"] = window
    window.events.loaded += lambda: api.enable_drop(window, ws)
    try:
        shell.run()
    finally:
        ws.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
