# workspace/__main__.py
# Start the meeting workspace window (#20):  python -m workspace
#
# The state-owning process: it installs the offline network guard before
# anything else, owns the database and runs processing on background threads;
# heavy models run in the managed worker process. Models are read from the
# existing local cache (%LOCALAPPDATA%\whisper-models); nothing is downloaded.

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import offline_guard  # noqa: E402

offline_guard.disable_telemetry()
offline_guard.install()

from workspace import api, shell  # noqa: E402
from workspace.app import Workspace  # noqa: E402

MODEL_CACHE = Path(os.environ.get("LOCALAPPDATA", os.getcwd())) / "whisper-models"


def main():
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


if __name__ == "__main__":
    main()
