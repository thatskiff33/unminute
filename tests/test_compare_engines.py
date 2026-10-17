# Tests for the engine-comparison scoring (tools/compare_engines.py). Stdlib only.
#   python -m unittest discover -s tests -v

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from compare_engines import edit_distance, normalise  # noqa: E402


class NormaliseTests(unittest.TestCase):
    def check(self, text, expected):
        self.assertEqual(" ".join(normalise(text)), expected)

    def test_written_and_spoken_numbers_meet(self):
        self.check("margin of 7%.", "margin of seven percent")
        self.check("MARGIN OF SEVEN PERCENT", "margin of seven percent")
        self.check("109,000 tons", "one hundred nine thousand tons")

    def test_currency_ordinals_and_decimals(self):
        self.check("The biggest is $18,500, due on the 2nd",
                   "the biggest is eighteen thousand five hundred dollars due on the second")
        self.check("$10 million", "ten million dollars")
        self.check("the 21st at 9.30", "the twenty first at nine point three zero")

    def test_fillers_case_and_contractions(self):
        self.check("UM THE PROJECT, UH, IS OK", "the project is okay")
        self.check("MANUALS 'CAUSE THERE", "manuals because there")
        self.check("fifty-six", "fifty six")


class CommandLineTests(unittest.TestCase):
    def test_help_runs(self):
        import subprocess
        tool = Path(__file__).resolve().parents[1] / "tools" / "compare_engines.py"
        for args in ([], ["run"], ["score"]):
            r = subprocess.run([sys.executable, str(tool), *args, "--help"],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_score_on_synthetic_results(self):
        import json
        import subprocess
        import tempfile
        tool = Path(__file__).resolve().parents[1] / "tools" / "compare_engines.py"
        with tempfile.TemporaryDirectory() as tmp:
            res = Path(tmp) / "fake.jsonl"
            res.write_text("\n".join(json.dumps(x) for x in (
                {"engine": "fake", "description": "fake", "load_s": 1.0},
                {"set": "s", "id": 1, "ref": "a b c d", "hyp": "a x c",
                 "audio_s": 10.0, "proc_s": 2.0},
                {"peak_ram_gb": 1.5})) + "\n", encoding="utf-8")
            r = subprocess.run([sys.executable, str(tool), "score", str(res)],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("| fake | 50.0% | 50.0% |", r.stdout)


class RobustScoringTests(unittest.TestCase):
    def run_score(self, lines):
        import json
        import subprocess
        import tempfile
        tool = Path(__file__).resolve().parents[1] / "tools" / "compare_engines.py"
        with tempfile.TemporaryDirectory() as tmp:
            res = Path(tmp) / "r.jsonl"
            res.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
            return subprocess.run([sys.executable, str(tool), "score", str(res)],
                                  capture_output=True, text=True, timeout=60)

    def test_no_references_still_reports_timings(self):
        r = self.run_score([{"engine": "e", "load_s": 1},
                            {"set": "s", "id": 1, "hyp": "x", "audio_s": 6.0, "proc_s": 2.0},
                            {"peak_ram_gb": 1.0}])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("| e | - | - | 0.1 | 2 | 3.0 |", r.stdout)

    def test_interrupted_run_keeps_last_clip(self):
        r = self.run_score([{"engine": "e", "load_s": 1},
                            {"set": "s", "id": 1, "ref": "a b", "hyp": "a b",
                             "audio_s": 6.0, "proc_s": 2.0},
                            {"set": "s", "id": 2, "ref": "c d", "hyp": "x d",
                             "audio_s": 6.0, "proc_s": 2.0}])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("| e | 25.0% |", r.stdout)

    def test_uk_spelling_is_not_an_error(self):
        self.assertEqual(normalise("We organised the colour scheme"),
                         normalise("we organized the color scheme"))


class EditDistanceTests(unittest.TestCase):
    def test_substitution_deletion_insertion(self):
        self.assertEqual(edit_distance("a b c d".split(), "a x c".split()), 2)
        self.assertEqual(edit_distance([], "a b".split()), 2)
        self.assertEqual(edit_distance("a b".split(), "a b".split()), 0)


# The real `run` path in a child process, with only the engine faked: each clip
# is decoded by the app's decoder, and nothing imports faster_whisper or
# CTranslate2, which the app's Parakeet worker never loads (#187).
RUN_PROBE = r"""
import argparse, json, sys
attempted = set()


class Recorder:
    def find_spec(self, name, path=None, target=None):
        attempted.add(name.split(".")[0])
        return None


sys.meta_path.insert(0, Recorder())
tools, repo, wav, out = sys.argv[1:5]
sys.path[:0] = [repo, tools]
import compare_engines
from workspace import engines
seen = []


class Fake:
    description = "fake"

    def transcribe(self, samples):
        seen.append(len(samples))
        return iter([]), len(samples) / 16000


engines.load_engine = lambda name, cache, threads: Fake()
manifest = out + ".manifest"
with open(manifest, "w", encoding="utf-8") as f:
    f.write(json.dumps({"set": "s", "id": 1, "wav": wav}) + "\n")
compare_engines.cmd_run(argparse.Namespace(repo=repo, engine="fake", cache=out + ".cache",
                                           threads=1, manifest=[manifest], out=out))
from workspace import media
print(json.dumps({"attempted": sorted(attempted), "seen": seen,
                  "integrity": media.last_decoder_integrity}))
"""


@unittest.skipUnless(sys.platform == "win32", "the app's decoder runs in a Windows job")
class RunTests(unittest.TestCase):
    def test_clips_are_decoded_by_the_app_without_faster_whisper(self):
        import importlib.util
        import json
        import subprocess
        import tempfile
        import wave
        if not (importlib.util.find_spec("av") and importlib.util.find_spec("numpy")):
            self.skipTest("PyAV and numpy are not installed")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "synthetic.wav"
            with wave.open(str(wav), "wb") as w:      # 2 s of silence, 16 kHz mono
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\0\0" * 32000)
            out = Path(tmp) / "run.jsonl"
            # -B: no bytecode in the checkout (evidence runs refuse an unclean tools/).
            r = subprocess.run([sys.executable, "-B", "-c", RUN_PROBE, str(root / "tools"),
                                str(root), str(wav), str(out)],
                               capture_output=True, text=True, timeout=180)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            probe = json.loads(r.stdout.strip().splitlines()[-1])
            self.assertEqual(probe["seen"], [32000])
            self.assertNotIn("faster_whisper", probe["attempted"])
            self.assertNotIn("ctranslate2", probe["attempted"])
            # Decoded in the contained process (Low integrity, 0x1000), not here:
            # this process never even imports PyAV.
            self.assertEqual(probe["integrity"], 0x1000)
            self.assertNotIn("av", probe["attempted"])
            lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([lines[0]["engine"], lines[1]["audio_s"]], ["fake", 2.0])
            self.assertIn("peak_ram_gb", lines[-1])


if __name__ == "__main__":
    unittest.main()
