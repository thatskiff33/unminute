# tests/test_import_hygiene.py
# Just-in-time loading (docs/RELEASE_STANDARD.md, section 3): the window process
# must not load machine-learning, media or GPU libraries at import time. They
# belong in the worker or download process, or inside the function that needs
# them. A meta-path hook records every import that is attempted, so the check
# works even where the libraries are not installed (the Linux CI job), and an
# allowlist means a new third-party import fails here until it is justified.

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUDGETS = json.loads((ROOT / "tests" / "budgets.json").read_text(encoding="utf-8"))

# The modules the window process imports before it opens the window.
# workspace.__main__ is left out on purpose: importing it installs the network
# guard and drops a token left in the environment, which are start-up actions,
# not imports.
UI_MODULES = ("workspace.app", "workspace.api", "workspace.shell", "workspace.worker",
              "workspace.asr_engines", "workspace.launch", "workspace.launchcheck",
              "workspace.instance")

# The only non-standard-library top-level packages the window process may import.
ALLOWED = {"workspace", "numpy"}

# Named in failures, and checked against sys.modules in case a .pth file loads one.
HEAVY = ("torch", "torchaudio", "torchcodec", "onnxruntime", "onnx_asr", "ctranslate2",
         "faster_whisper", "tokenizers", "pyannote", "lightning", "pytorch_lightning",
         "safetensors", "huggingface_hub", "scipy", "sklearn", "pandas", "matplotlib",
         "sounddevice", "av", "webview")

PROBE = r"""
import importlib, json, sys
attempted = set()

class Recorder:
    def find_spec(self, name, path=None, target=None):
        attempted.add(name.partition(".")[0])
        return None          # record only; the normal finders still run

sys.meta_path.insert(0, Recorder())
for module in sys.argv[2].split(","):
    importlib.import_module(module)
heavy = set(sys.argv[1].split(","))
print(json.dumps({"attempted": sorted(attempted), "modules": len(sys.modules),
                  "heavy_loaded": sorted(m for m in sys.modules if m.partition(".")[0] in heavy)}))
"""


# The variables with which coverage.py starts itself in a child process (the
# report-only Coverage job measures child processes, #413). They are dropped for
# the probe, so it counts the window process's own modules, not coverage's; a
# run without coverage, such as "Unit tests (Windows)", has neither.
COVERAGE_CHILD_VARIABLES = ("COVERAGE_PROCESS_CONFIG", "COVERAGE_PROCESS_START")


def probe():
    env = {k: v for k, v in os.environ.items() if k.upper() not in COVERAGE_CHILD_VARIABLES}
    result = subprocess.run(
        [sys.executable, "-c", PROBE, ",".join(HEAVY), ",".join(UI_MODULES)],
        cwd=ROOT, capture_output=True, text=True, timeout=120, env=env)
    if result.returncode != 0:
        raise AssertionError("importing the window modules failed:\n" + result.stderr[-2000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


class ImportHygieneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = probe()

    def test_window_process_imports_only_allowed_packages(self):
        unexpected = sorted(n for n in self.result["attempted"]
                            if n not in sys.stdlib_module_names and not n.startswith("_")
                            and n not in ALLOWED)
        self.assertEqual(
            [], unexpected,
            "The window process imports these at module level. Import them inside the "
            "function that needs them, or in the worker; if one truly belongs in the window "
            "process, add it to ALLOWED with the reason in the pull request.")

    def test_no_heavy_library_is_loaded(self):
        self.assertEqual([], self.result["heavy_loaded"])

    @unittest.skipUnless(sys.platform == "win32", "module budget is measured on Windows")
    def test_module_count_within_budget(self):
        budget = BUDGETS["window_process_modules"]["value"]
        print(f"\nwindow process modules: {self.result['modules']} (budget {budget})",
              file=sys.stderr)
        self.assertLessEqual(
            self.result["modules"], budget,
            f"The window process now imports {self.result['modules']} modules (budget {budget}). "
            "Find the new import; raise the budget only with a reason in the pull request.")


if __name__ == "__main__":
    unittest.main()
