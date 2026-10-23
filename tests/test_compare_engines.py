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
tools, repo, wav, out, scratch = sys.argv[1:6]
sys.path[:0] = [repo, tools]
import os, tempfile, wave
tempfile.tempdir = scratch                   # the run's temporary files go here
import compare_engines
from workspace import engines
seen = []


class Fake:
    description = "fake"

    def transcribe(self, samples):
        import numpy as np
        with wave.open(wav) as w:            # the clip's own 16-bit samples, as floats
            pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2") / 32768.0
        seen.append([len(samples), str(samples.dtype), float(np.abs(samples - pcm).max())])
        return iter([]), len(samples) / 16000


engines.load_engine = lambda name, cache, threads: Fake()
manifest = out + ".manifest"
with open(manifest, "w", encoding="utf-8") as f:
    f.write(json.dumps({"set": "s", "id": 1, "wav": wav}) + "\n")
compare_engines.cmd_run(argparse.Namespace(repo=repo, engine="fake", cache=out + ".cache",
                                           threads=1, manifest=[manifest], out=out))
from workspace import media
print(json.dumps({"attempted": sorted(attempted), "seen": seen,
                  "integrity": media.last_decoder_integrity, "left": os.listdir(scratch)}))
"""


MISSING_PROBE = r"""
import argparse, json, sys
tools, repo, out = sys.argv[1:4]
sys.path[:0] = [repo, tools]
import compare_engines
from workspace import engines


def never(*args):
    raise AssertionError("the model loaded")


engines.load_engine = never
manifest = out + ".manifest"
with open(manifest, "w", encoding="utf-8") as f:
    f.write(json.dumps({"set": "s", "id": 7, "wav": out + ".missing.wav"}) + "\n")
compare_engines.cmd_run(argparse.Namespace(repo=repo, engine="fake", cache=out + ".cache",
                                           threads=1, manifest=[manifest], out=out))
"""


DAMAGED_PROBE = r"""
import argparse, json, sys
tools, repo, wav, out = sys.argv[1:5]
sys.path[:0] = [repo, tools]
import compare_engines
from workspace import engines


class Fake:
    description = "fake"

    def transcribe(self, samples):
        raise AssertionError("a refused clip reached the engine")


engines.load_engine = lambda name, cache, threads: Fake()
manifest = out + ".manifest"
with open(manifest, "w", encoding="utf-8") as f:
    f.write(json.dumps({"set": "s", "id": 9, "wav": wav}) + "\n")
compare_engines.cmd_run(argparse.Namespace(repo=repo, engine="fake", cache=out + ".cache",
                                           threads=1, manifest=[manifest], out=out))
"""


# Draft mode (#104): the draft engine and the app's DraftPipeline, with only the
# engine faked (loud 32 ms windows are speech). The full-pass loader is never used.
DRAFT_PROBE = r"""
import argparse, json, sys
tools, repo, wav, out, cut = sys.argv[1:6]
sys.path[:0] = [repo, tools]
import compare_engines
from workspace import drafts, engines
loaded = []


class FakeDraft:
    description = "fake drafts"

    def speech_prob(self, window):
        import numpy as np
        return 1.0 if float(np.sqrt(np.mean(np.square(window)))) > 0.02 else 0.0

    def reset(self):
        pass

    def transcribe_phrase(self, audio):
        return f"phrase {len(audio)}"


def draft_loader(name, cache, threads):
    loaded.append([name, threads, drafts.MAX_PHRASE_S])
    return FakeDraft()


def never(*args):
    raise AssertionError("the full pass loaded")


engines.load_engine, engines.load_draft_engine = never, draft_loader
manifest = out + ".manifest"
with open(manifest, "w", encoding="utf-8") as f:
    f.write(json.dumps({"set": "s", "id": 1, "wav": wav}) + "\n")
compare_engines.cmd_run(argparse.Namespace(repo=repo, engine="fake", cache=out + ".cache",
                                           threads=None, manifest=[manifest], out=out,
                                           draft_cut=float(cut)))
print(json.dumps({"loaded": loaded, "after": drafts.MAX_PHRASE_S}))
"""


@unittest.skipUnless(sys.platform == "win32", "the app's decoder runs in a Windows job")
class RunTests(unittest.TestCase):
    def test_a_clip_the_decoder_refuses_stops_the_run_naming_the_clip(self):
        import importlib.util
        import subprocess
        import tempfile
        if not importlib.util.find_spec("av"):
            self.skipTest("PyAV is not installed")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "damaged-synthetic.wav"
            wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVEjunk" + bytes(range(256)) * 8)
            r = subprocess.run([sys.executable, "-B", "-c", DAMAGED_PROBE, str(root / "tools"),
                                str(root), str(wav), str(Path(tmp) / "run.jsonl")],
                               capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 1, r.stderr[-2000:])
        self.assertIn("Clip s/9 could not be decoded", r.stderr)
        self.assertNotIn("reached the engine", r.stderr)
        self.assertNotIn("damaged-synthetic", r.stderr)      # the clip, not its path

    def test_a_missing_clip_stops_the_run_before_the_model_loads(self):
        import subprocess
        import tempfile
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([sys.executable, "-B", "-c", MISSING_PROBE, str(root / "tools"),
                                str(root), str(Path(tmp) / "run.jsonl")],
                               capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 1, r.stderr[-2000:])
        self.assertIn("1 clip(s) not found: s/7", r.stderr)
        self.assertNotIn("the model loaded", r.stderr)
        self.assertNotIn("missing.wav", r.stderr)            # the clip, not its path

    def test_clips_are_decoded_by_the_app_without_faster_whisper(self):
        import importlib.util
        import json
        import subprocess
        import tempfile
        import wave
        if not (importlib.util.find_spec("av") and importlib.util.find_spec("numpy")):
            self.skipTest("PyAV and numpy are not installed")
        import math
        import struct
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "synthetic.wav"
            tone = [round(16000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(32000)]
            with wave.open(str(wav), "wb") as w:      # 2 s of a 440 Hz tone, 16 kHz mono
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(struct.pack(f"<{len(tone)}h", *tone))
            out = Path(tmp) / "run.jsonl"
            scratch = Path(tmp) / "scratch"
            scratch.mkdir()
            # -B: no bytecode in the checkout (evidence runs refuse an unclean tools/).
            r = subprocess.run([sys.executable, "-B", "-c", RUN_PROBE, str(root / "tools"),
                                str(root), str(wav), str(out), str(scratch)],
                               capture_output=True, text=True, timeout=180)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            probe = json.loads(r.stdout.strip().splitlines()[-1])
            # The engine gets the clip's samples as float32: each 16-bit value / 32768.
            [(count, dtype, worst)] = probe["seen"]
            self.assertEqual((count, dtype), (32000, "float32"))
            self.assertLessEqual(worst, 1e-6)
            self.assertEqual(probe["left"], [])                 # no temporary file left
            self.assertNotIn("faster_whisper", probe["attempted"])
            self.assertNotIn("ctranslate2", probe["attempted"])
            # Decoded in the contained process (Low integrity, 0x1000), not here:
            # this process never even imports PyAV.
            self.assertEqual(probe["integrity"], 0x1000)
            self.assertNotIn("av", probe["attempted"])
            lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([lines[0]["engine"], lines[1]["audio_s"]], ["fake", 2.0])
            self.assertIn("peak_ram_gb", lines[-1])

    def test_draft_mode_runs_the_draft_path_at_the_trial_cut(self):
        import importlib.util
        import json
        import math
        import struct
        import subprocess
        import tempfile
        import wave
        if not (importlib.util.find_spec("av") and importlib.util.find_spec("numpy")):
            self.skipTest("PyAV and numpy are not installed")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "synthetic.wav"
            quiet = [0] * 8000
            tone = [round(12000 * math.sin(2 * math.pi * 220 * i / 16000)) for i in range(80000)]
            with wave.open(str(wav), "wb") as w:      # 0.5 s quiet, 5 s tone, 0.5 s quiet
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                samples = quiet + tone + quiet
                w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
            out = Path(tmp) / "run.jsonl"
            r = subprocess.run([sys.executable, "-B", "-c", DRAFT_PROBE, str(root / "tools"),
                                str(root), str(wav), str(out), "2"],
                               capture_output=True, text=True, timeout=180)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            probe = json.loads(r.stdout.strip().splitlines()[-1])
            # Loaded as the draft worker loads (its thread count), with the cut in force,
            # and the shipped cut back afterwards.
            from workspace import drafts
            self.assertEqual(probe["loaded"], [["fake", drafts.DRAFT_THREADS, 2.0]])
            self.assertEqual(probe["after"], drafts.MAX_PHRASE_S)
            lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(lines[0]["engine"], "fake drafts 2 s")
            self.assertEqual(lines[1]["audio_s"], 6.0)
            # 5 s of unbroken "speech" cut at 2 s or less (at the quietest window of
            # the last 1.5 s): at least three phrases, where the shipped 8 s gives one.
            self.assertGreaterEqual(lines[1]["phrases"], 3)
            self.assertEqual(len(lines[1]["hyp"].split()), 2 * lines[1]["phrases"])

    def test_draft_mode_refuses_a_cut_outside_the_trial_range_before_loading(self):
        import subprocess
        import tempfile
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "present.wav"
            wav.write_bytes(b"")
            r = subprocess.run([sys.executable, "-B", "-c", DRAFT_PROBE, str(root / "tools"),
                                str(root), str(wav), str(Path(tmp) / "run.jsonl"), "1"],
                               capture_output=True, text=True, timeout=120)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("A cut must be 2 to 30 seconds", r.stderr)
            self.assertFalse((Path(tmp) / "run.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
