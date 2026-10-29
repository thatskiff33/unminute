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


class LockedOnlyDecoderTests(unittest.TestCase):
    """Each tool's --locked-only process calls decoder_runtime.use() before it
    imports the synthetic recording."""

    def run_locked_only(self, tool, *patches):
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
                    mock.patch("webbrowser.open"), contextlib.ExitStack() as more:
                # Never mock.patch.stopall(): it would also stop the run's own
                # decoder_runtime.use() patches (tools/run_tests.py).
                for patch in patches:
                    more.enter_context(patch)
                self.assertEqual(0, tool.main())
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertNotIn("error", report)
        return order

    def test_the_ui_checks_locked_process_sets_up_the_decoder_first(self):
        order = self.run_locked_only(
            workspace_ui_check,
            mock.patch.object(workspace_ui_check.api, "make_api"),
            mock.patch.object(workspace_ui_check, "logged_bridge"),
            mock.patch.object(shell, "create_window"),
            mock.patch.object(shell, "storage_folder"))
        self.assertEqual(["decoder", "import"], order)

    def test_the_accessibility_checks_locked_process_sets_up_the_decoder_first(self):
        order = self.run_locked_only(
            accessibility_check,
            mock.patch.object(accessibility_check, "create_window"))
        self.assertEqual(["decoder", "import"], order)


if __name__ == "__main__":
    unittest.main()
