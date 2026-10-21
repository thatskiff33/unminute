# Watched-folder tests keep their folders in %TEMP%, which on Windows is
# inside AppData, and a folder anywhere in AppData cannot be watched (#111
# D2, intake._broad). start(case) gives intake._broad a user profile and app
# data folders on a path that holds none of those folders, for the length of
# one test. It patches only what intake._broad reads (intake._environ), not
# os.environ, which the test key protector's guard reads (keyprotect.py).
# The broad-folder tests in tests/test_intake.py (BroadFolderTests) do not
# use it: they set the real environment variables. Not a test module itself
# (no test_ prefix).

import os
import tempfile
from pathlib import Path
from unittest import mock


def start(case):
    """Patch intake._environ for this test; undone by the test's cleanup."""
    from workspace import intake      # here, so importing this module needs no dependencies
    profile = Path(Path(tempfile.gettempdir()).anchor) / "UnminuteTestProfile"
    fake = {"USERPROFILE": str(profile),
            "APPDATA": str(profile / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(profile / "AppData" / "Local")}
    patcher = mock.patch.object(intake, "_environ", lambda: {**os.environ, **fake})
    patcher.start()
    case.addCleanup(patcher.stop)
