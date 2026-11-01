# tests/test_locked_check.py
# The real-window tools' locked-screen process (#111 C4b) sets up the decoder
# before it imports its synthetic recording (#494). The decoder runs in an
# AppContainer that cannot read a development Python (#299), so the
# --locked-only process of tools/workspace_ui_check.py and of
# tools/accessibility_check.py must point it at tools/decoder_runtime.py's
# scratch runtime first, as each tool's main path does. No window opens: the
# workspace, the window and the bridge are stand-ins, and the order of the
# decoder set-up and the import is what is checked. The test process already
# has the scratch runtime in use (tools/run_tests.py), so only this order shows
# the fault; a real decode here would pass without the fix.
#
# The session's lock state (#507) is a stand-in too: the real one depends on
# the machine (none can be read on Linux, and a locked Windows session is
# locked), and either stops the locked process before its window.

import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import decoder_runtime  # noqa: E402
import workspace_ui_check  # noqa: E402  (puts the app on the path first)

import accessibility_check  # noqa: E402, I001
import session_state  # noqa: E402

from workspace import media, shell  # noqa: E402


class _Workspace:
    """Stands in for workspace.app.Workspace: opened locked when given a
    protector, as locked_workspace opens it the second time."""

    def __init__(self, *args, protector=None, **kwargs):
        self.store = self.media = object()
        self.root = Path(args[0]) if args else None
        self.locked = SimpleNamespace(reason="windows") if protector else None

    def close(self):
        pass


UNLOCKED = {"locked": False, "how": session_state.WTS}
LOCKED = {"locked": True, "how": session_state.WTS}


def run_locked_only(test, tool, *patches, session=UNLOCKED):
    """The tool's --locked-only process on stand-ins. session: what each
    reading of the session's state says. Returns the order of the decoder
    set-up and the import, and the process's report."""
    order = []
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "locked.json"
        argv = [tool.__file__, str(Path(tmp) / "unused.json"), "--locked-only", str(out)]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(decoder_runtime, "use",
                                  side_effect=lambda: order.append("decoder")), \
                mock.patch.object(media, "import_recording",
                                  side_effect=lambda *a, **k: order.append("import")), \
                mock.patch.object(workspace_ui_check, "Workspace", _Workspace), \
                mock.patch.object(shell, "run"), \
                mock.patch("webbrowser.open"), \
                mock.patch.object(session_state, "read", return_value=session), \
                contextlib.ExitStack() as more:
            # Never mock.patch.stopall(): it would also stop the run's own
            # decoder_runtime.use() patches (tools/run_tests.py).
            for patch in patches:
                more.enter_context(patch)
            test.assertEqual(0, tool.main())
        report = json.loads(out.read_text(encoding="utf-8"))
    return order, report


def ui_window_patches():
    return (mock.patch.object(workspace_ui_check.api, "make_api"),
            mock.patch.object(workspace_ui_check, "logged_bridge"),
            mock.patch.object(shell, "create_window"),
            mock.patch.object(shell, "storage_folder"))


class LockedOnlyDecoderTests(unittest.TestCase):
    """Each tool's --locked-only process calls decoder_runtime.use() before it
    imports the synthetic recording."""

    def run_unlocked(self, tool, *patches):
        order, report = run_locked_only(self, tool, *patches)
        self.assertNotIn("error", report)
        self.assertFalse(report["session"]["locked"])
        self.assertEqual(report["session"]["readings"], report["session"]["wts"])
        return order

    def test_the_ui_checks_locked_process_sets_up_the_decoder_first(self):
        order = self.run_unlocked(workspace_ui_check, *ui_window_patches())
        self.assertEqual(["decoder", "import"], order)

    def test_the_accessibility_checks_locked_process_sets_up_the_decoder_first(self):
        order = self.run_unlocked(
            accessibility_check,
            mock.patch.object(accessibility_check, "create_window"))
        self.assertEqual(["decoder", "import"], order)


class LockedOnlySessionTests(unittest.TestCase):
    """A locked session stops each tool's --locked-only process before its
    window opens, named for the locked screen (#507)."""

    def assert_stopped_before_the_window(self, tool, create, *patches):
        _, report = run_locked_only(self, tool, *patches, session=LOCKED)
        create.assert_not_called()
        self.assertEqual("SessionLocked: the session locked (before the locked screen)",
                         report["error"])
        self.assertEqual((True, "before the locked screen"),
                         (report["session"]["locked"], report["session"]["locked_at"]))

    def test_the_ui_checks_locked_process_stops_before_its_window(self):
        with mock.patch.object(shell, "create_window") as create:
            self.assert_stopped_before_the_window(workspace_ui_check, create,
                                                  *ui_window_patches()[:2])

    def test_the_accessibility_checks_locked_process_stops_before_its_window(self):
        with mock.patch.object(accessibility_check, "create_window") as create:
            self.assert_stopped_before_the_window(accessibility_check, create)


if __name__ == "__main__":
    unittest.main()
