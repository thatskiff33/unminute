"""Facts that have one source (R12, #133): the minimum NVIDIA driver, speech
model names and what the app calls them, and download progress as a number.
The page shows what Python sends and reads nothing out of a title or message.
"""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asr_engines  # noqa: E402
from workspace import gpu, setup  # noqa: E402

APP_JS = (ROOT / "workspace" / "ui" / "app.js").read_text(encoding="utf-8")


class DriverMinimumTests(unittest.TestCase):
    def test_one_definition_in_code(self):
        minimum = str(gpu.MIN_DRIVER[0])
        found = []
        paths = [*sorted((ROOT / "workspace").glob("*.py")), ROOT / "workspace" / "ui" / "app.js"]
        for path in paths:
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(rf"\b{minimum}\b", line) and re.search(r"(?i)driver|or later|R5\d\d",
                                                                    line):
                    found.append(f"{path.name}:{n}")
        self.assertEqual([f for f in found if not f.startswith("gpu.py:")], [])

    def test_the_docs_name_the_same_minimum(self):
        for doc in ("README.md", "docs/WORKSPACE_GUIDE.md"):
            text = (ROOT / doc).read_text(encoding="utf-8")
            numbers = set(re.findall(r"(?i)driver[^.\n]{0,30}?\b(\d{3})\b", text))
            self.assertEqual(numbers, {str(gpu.MIN_DRIVER[0])}, doc)

    def test_the_page_gets_it_from_settings(self):
        self.assertIn("${s.gpu.min_driver}", APP_JS)


class ModelNameTests(unittest.TestCase):
    def test_every_speech_model_has_one_label(self):
        speech = set(asr_engines.PARAKEET_MODELS) | set(asr_engines.WHISPER_MODELS)
        self.assertEqual(set(asr_engines.LABELS), speech)
        self.assertEqual(asr_engines.label("test:x:y"), "test:x:y")      # unknown: as it is

    def test_setup_takes_names_and_labels_from_asr_engines(self):
        source = (ROOT / "workspace" / "setup.py").read_text(encoding="utf-8")
        literals = re.findall(r"[\"'][^\"'\n]*(?:parakeet-tdt|large-v3)[^\"'\n]*[\"']", source)
        self.assertEqual(literals, [])                                    # no literal names
        for card in setup.CARDS:
            self.assertIn(card["name"], asr_engines.MANAGED_MODELS)
            self.assertTrue(card["label"])
            if card["role"] == "speech":
                self.assertEqual(card["label"], asr_engines.label(card["name"]))
                self.assertIn(card["label"], card["title"])

    def test_a_meeting_carries_its_models_label(self):
        from workspace.app import Workspace
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(Path(tmp) / "ws", Path(tmp) / "models", start=False, battery=False,
                           policy_reader=lambda: ({}, ()))
            try:
                facts = ws._facts({"model": asr_engines.PARAKEET_V3, "id": 1}, known=(None, None))
                self.assertEqual((facts["model"], facts["model_label"]),
                                 (asr_engines.PARAKEET_V3, "Parakeet v3"))
                self.assertIsNone(ws._facts(None)["model_label"])
            finally:
                ws.close()

    def test_the_page_reads_no_names_out_of_titles_or_ids(self):
        self.assertNotIn("c.title.replace", APP_JS)
        self.assertNotIn("function modelName", APP_JS)
        self.assertIn("d.model_label", APP_JS)


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.downloads = setup.Downloads(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_page_reads_no_numbers_out_of_messages(self):
        regexes = re.findall(r"/[^/\n]*(?:GB|%)[^/\n]*/[gimsuy]*\.(?:exec|test)\(", APP_JS)
        self.assertEqual(regexes, [])
        self.assertIn("dl.percent", APP_JS)

    def test_a_model_download_is_measured_by_what_it_has_written(self):
        name = asr_engines.RECOMMENDED_PARAKEET
        spec = asr_engines.MANAGED_MODELS[name]
        self.assertIsNone(self.downloads.percent(name))                 # nothing running
        self.downloads.state[name] = {"state": "running", "message": "Starting", "log": []}
        self.assertIsNone(self.downloads.percent(name))                 # nothing written yet
        asr_engines.staging_dir(spec, self.tmp.name).mkdir(parents=True)
        with mock.patch.object(setup, "_size_of", return_value=spec.download_bytes // 4):
            self.assertEqual(self.downloads.percent(name), 25)
        with mock.patch.object(setup, "_size_of", return_value=spec.download_bytes * 2):
            self.assertEqual(self.downloads.percent(name), 100)          # never past 100
        self.downloads.state[name] = {"state": "running", "message": "Checking", "log": [],
                                      "verify": True}
        self.assertIsNone(self.downloads.percent(name))                 # a check: no number
        self.downloads.state[name] = {"state": "ready", "message": "Done", "log": []}
        self.assertIsNone(self.downloads.percent(name))

    def test_the_graphics_component_is_measured_by_its_wheels(self):
        self.downloads.state[gpu.COMPONENT] = {"state": "running", "message": "x", "log": []}
        partial = gpu.component_dir(Path(self.tmp.name)).parent / (gpu.VERSION + ".partial")
        wheels = partial / "wheels"
        wheels.mkdir(parents=True)
        with mock.patch.object(setup, "_size_of", return_value=gpu.DOWNLOAD_BYTES // 2) as size:
            self.assertEqual(self.downloads.percent(gpu.COMPONENT), 50)
        self.assertEqual(Path(size.call_args.args[0]), wheels)

    def test_setup_state_sends_the_number(self):
        from workspace.app import Workspace
        ws = Workspace(Path(self.tmp.name) / "ws", Path(self.tmp.name) / "models", start=False,
                       battery=False, policy_reader=lambda: ({}, ()))
        try:
            name = asr_engines.RECOMMENDED_PARAKEET
            ws.downloads.state[name] = {"state": "running", "message": "Starting", "log": []}
            with mock.patch.object(ws.downloads, "percent", return_value=40), \
                    mock.patch.object(gpu, "detected", return_value=None):
                card = next(c for c in ws.setup_state()["cards"] if c["name"] == name)
            self.assertEqual((card["download"]["percent"], card["label"]), (40, "Parakeet v2"))
            self.assertNotIn("percent", ws.downloads.state[name])      # a copy, not the state
            with mock.patch.object(gpu, "detected", return_value=None):
                settings = ws.get_settings()
            self.assertEqual(settings["gpu"]["min_driver"], gpu.MIN_DRIVER[0])
            choices = settings["choices"]
            self.assertEqual([(c["name"], c["label"], c["recommended"]) for c in choices],
                             [(n, asr_engines.label(n), n == asr_engines.RECOMMENDED_PARAKEET)
                              for n in (*asr_engines.PARAKEET_MODELS, *asr_engines.WHISPER_MODELS)])
        finally:
            ws.close()


PROBE = r"""
import json, sys
from unittest import mock
attempted = set()

class Recorder:
    def find_spec(self, name, path=None, target=None):
        attempted.add(name.partition(".")[0])
        return None

sys.meta_path.insert(0, Recorder())
import asr_engines
from workspace import engines
with mock.patch.object(asr_engines, "parakeet_status", return_value=("ready", "")), \
        mock.patch.object(asr_engines.ParakeetEngine, "load", lambda self: self):
    engine = engines.load_engine(asr_engines.RECOMMENDED_PARAKEET, ".", 1)
print(json.dumps({"attempted": sorted(attempted), "decoder": engine.decode_audio is None}))
"""


class ParakeetWorkerTests(unittest.TestCase):
    def test_a_parakeet_engine_never_imports_faster_whisper(self):
        # A recorder sees every import attempt, even where faster_whisper and
        # CTranslate2 are not installed (CI's test install).
        result = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        seen = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(seen["decoder"])
        self.assertNotIn("faster_whisper", seen["attempted"])
        self.assertNotIn("ctranslate2", seen["attempted"])

    def test_a_file_path_needs_a_decoder(self):
        engine = asr_engines.ParakeetEngine(asr_engines.RECOMMENDED_PARAKEET, ".", 1)
        engine._asr = object()                          # as if loaded
        with self.assertRaises(asr_engines.ModelLoadError):
            engine.transcribe("synthetic.wav")


if __name__ == "__main__":
    unittest.main()
