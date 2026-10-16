# tests/test_import_hygiene.py
# Just-in-time loading (docs/RELEASE_STANDARD.md, section 3): the window process
# must not load machine-learning, media or GPU libraries at import time. They
# belong in the worker or download process, or inside the function that needs
# them. A meta-path hook records every import that is attempted, so the check
# works even where the libraries are not installed (the Linux CI job).

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUDGETS = json.loads((ROOT / "tests" / "budgets.json").read_text(encoding="utf-8"))

# The modules the window process imports before it opens the window.
UI_MODULES = ("workspace.app", "workspace.api", "workspace.shell", "workspace.worker",
              "asr_engines")

# Top-level packages the window process must never import at import time.
HEAVY = ("torch", "torchaudio", "onnxruntime", "onnx_asr", "ctranslate2", "faster_whisper",
         "pyannote", "huggingface_hub", "sounddevice", "av", "webview")

PROBE = r"""
import importlib, json, sys
heavy = set(sys.argv[1].split(","))
attempted = set()

class Recorder:
    def find_spec(self, name, path=None, target=None):
        if name.partition(".")[0] in heavy:
            attempted.add(name)
        return None          # record only; the normal finders still run

sys.meta_path.insert(0, Recorder())
for module in sys.argv[2].split(","):
    importlib.import_module(module)
print(json.dumps({"attempted": sorted(attempted), "modules": len(sys.modules)}))
"""


def probe():
    result = subprocess.run(
        [sys.executable, "-c", PROBE, ",".join(HEAVY), ",".join(UI_MODULES)],
        cwd=ROOT, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError("importing the window modules failed:\n" + result.stderr[-2000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


class ImportHygieneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = probe()

    def test_window_process_imports_no_heavy_libraries(self):
        self.assertEqual(
            [], self.result["attempted"],
            "These are imported at module level by the window process. Import them inside "
            "the function that needs them, or in the worker.")

    @unittest.skipUnless(sys.platform == "win32", "module budget is measured on Windows")
    def test_module_count_within_budget(self):
        budget = BUDGETS["window_process_modules"]["value"]
        self.assertLessEqual(
            self.result["modules"], budget,
            f"The window process now imports {self.result['modules']} modules (budget {budget}). "
            "Find the new import; raise the budget only with a reason in the pull request.")


if __name__ == "__main__":
    unittest.main()
