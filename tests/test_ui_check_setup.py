# tests/test_ui_check_setup.py
# Two parts of tools/workspace_ui_check.py that its real-window run on
# 6 Nov 2026 found wrong, checked here without a window:
# - interpreter(): the locked scenario's own process runs on this Python
#   (sys.executable) with the options it was started with. In a Windows
#   virtual environment orig_argv[0] is the base interpreter, without the
#   environment's numpy and PyAV, so that process could not set up the
#   decoder's scratch runtime and its import was refused.
# - expected_setup_cards(): the Setup page's model cards include the
#   instant-words model while the app offers it (#105, setup.OFFER_INSTANT).

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import workspace_ui_check  # noqa: E402  (puts the app on the path first)

from workspace import gpu, setup  # noqa: E402


class InterpreterTests(unittest.TestCase):
    SCRIPT = r"C:\checkout\tools\workspace_ui_check.py"

    def interpreter(self, orig_argv, executable):
        with mock.patch.object(sys, "orig_argv", orig_argv, create=True), \
                mock.patch.object(sys, "argv", [self.SCRIPT, "report.json"]), \
                mock.patch.object(sys, "executable", executable):
            return workspace_ui_check.interpreter()

    def test_a_virtual_environment_runs_the_child_on_its_own_python(self):
        base = r"C:\Python312\python.exe"
        venv = r"C:\scratch\venv\Scripts\python.exe"
        got = self.interpreter([base, self.SCRIPT, "report.json"], venv)
        self.assertEqual(got, [venv])                 # not the base interpreter

    def test_a_packages_options_are_kept(self):
        runtime = r"C:\Program Files\Unminute\current\runtime\python.exe"
        got = self.interpreter([runtime, "-I", "-B", "-X", "disable-remote-debug",
                                self.SCRIPT, "report.json"], runtime)
        self.assertEqual(got, [runtime, "-I", "-B", "-X", "disable-remote-debug"])

    def test_without_the_script_in_orig_argv_this_python_alone(self):
        self.assertEqual(self.interpreter([], r"C:\py\python.exe"), [r"C:\py\python.exe"])


class SetupCardTests(unittest.TestCase):
    def cards(self, offer_instant, nvidia):
        with mock.patch.object(setup, "OFFER_INSTANT", offer_instant), \
                mock.patch.object(gpu, "detected", return_value=object() if nvidia else None):
            return workspace_ui_check.expected_setup_cards()

    def test_speech_and_speaker_models_then_instant_words_then_the_card_components(self):
        self.assertEqual(self.cards(False, False), 5)
        self.assertEqual(self.cards(True, False), 6)      # the instant-words model (#105)
        self.assertEqual(self.cards(False, True), 7)
        self.assertEqual(self.cards(True, True), 8)       # this laptop on 6 Nov 2026

    def test_it_matches_the_cards_setup_offers(self):
        # Setup's own list of model cards; setup.cards adds the two graphics
        # card components after it, only on an NVIDIA machine.
        with mock.patch.object(gpu, "detected", return_value=None):
            self.assertEqual(len(setup.offered_cards()), self.cards(setup.OFFER_INSTANT, False))


if __name__ == "__main__":
    unittest.main()
