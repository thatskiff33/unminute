# Queue worker behaviour through the shared engine interface, with a fake
# engine (no models). Needs the app's dependencies (faster-whisper) to import
# transcriber_core, so it is skipped where they are not installed (e.g. CI).
#   python -m unittest discover -s tests -v

import importlib.util
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HAVE_APP_DEPS = importlib.util.find_spec("faster_whisper") is not None


class FakeEngine:
    description = "fake-model (test, cpu)"

    def __init__(self, segments, fail_on=None, on_segment=None):
        self.segments, self.fail_on, self.on_segment = segments, fail_on, on_segment

    def transcribe(self, audio):
        import asr_engines

        if self.fail_on and str(audio).endswith(self.fail_on):
            raise asr_engines.ModelLoadError("cannot read this file")

        def gen():
            for i, (s, e, t) in enumerate(self.segments):
                if self.on_segment:
                    self.on_segment(i)
                yield asr_engines.Segment(s, e, t)
        return gen(), self.segments[-1][1]

    def close(self):
        pass


@unittest.skipUnless(HAVE_APP_DEPS, "app dependencies not installed")
class QueueWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["LOCALAPPDATA"] = cls.tmp.name   # isolate caches/settings
        import transcriber_core
        cls.core = transcriber_core

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.out_dir = Path(tempfile.mkdtemp(dir=self.tmp.name))
        self.core.EXTRACT_DIR = self.out_dir
        self.events = []
        self.cancel, self.pause = threading.Event(), threading.Event()

    def run_queue(self, engine, names, fmt=".md"):
        self.core.get_model = lambda name, log: engine
        pending = [{"id": i, "path": str(self.out_dir / n)} for i, n in enumerate(names)]
        settings = {"model": "fake-model", "fmt": fmt, "speakers": False, "timestamps": True}
        self.core.queue_worker(pending, threading.Lock(), settings,
                               lambda k, p=None: self.events.append((k, p)),
                               self.cancel, self.pause)

    def statuses(self):
        return [p for k, p in self.events if k == "item_status"]

    def test_transcript_saved_with_engine_description(self):
        engine = FakeEngine([(0.0, 1.5, "Hello there."), (1.5, 3.0, "Second line.")])
        self.run_queue(engine, ["meeting.wav"])
        text = (self.out_dir / "meeting.md").read_text(encoding="utf-8")
        self.assertIn("- Model: fake-model (test, cpu)", text)
        self.assertIn("[0:00] Hello there.\n[0:01] Second line.", text)
        self.assertIn((0, "done"), self.statuses())
        self.assertIn(("duration", 3.0), self.events)

    def test_stop_saves_partial_transcript(self):
        engine = FakeEngine([(0.0, 1.0, "Kept."), (1.0, 2.0, "Not reached.")],
                            on_segment=lambda i: i == 1 and self.cancel.set())
        self.run_queue(engine, ["call.wav"], fmt=".txt")
        partial = (self.out_dir / "call (partial).txt").read_text(encoding="utf-8")
        self.assertIn("Kept.", partial)
        self.assertNotIn("Not reached.", partial)
        self.assertIn((0, "stopped"), self.statuses())

    def test_failed_file_does_not_stop_the_queue(self):
        engine = FakeEngine([(0.0, 1.0, "Fine.")], fail_on="bad.wav")
        self.run_queue(engine, ["bad.wav", "good.wav"], fmt=".txt")
        self.assertIn((0, "failed"), self.statuses())
        self.assertIn((1, "done"), self.statuses())
        self.assertTrue((self.out_dir / "good.txt").exists())

    def test_model_load_failure_is_not_retried_for_every_file(self):
        import asr_engines
        calls = []

        def failing_get_model(name, log):
            calls.append(name)
            raise asr_engines.ModelLoadError("checksum mismatch")

        self.core.get_model = failing_get_model
        pending = [{"id": i, "path": str(self.out_dir / f"f{i}.wav")} for i in range(3)]
        settings = {"model": "fake-model", "fmt": ".txt", "speakers": False, "timestamps": False}
        self.core.queue_worker(pending, threading.Lock(), settings,
                               lambda k, p=None: self.events.append((k, p)),
                               self.cancel, self.pause)
        self.assertEqual(len(calls), 1)
        self.assertEqual([p for p in self.statuses() if p[1] == "failed"],
                         [(0, "failed"), (1, "failed"), (2, "failed")])

    def test_parakeet_models_listed_with_whisper_kept(self):
        self.assertEqual(self.core.MODELS[0], "parakeet-tdt-0.6b-v2")
        for name in ("large-v3-turbo", "large-v3", "parakeet-tdt-0.6b-v3"):
            self.assertIn(name, self.core.MODELS)
            self.assertIn(name, self.core.MODEL_DOWNLOAD_SIZES)
            self.assertIn(name, self.core.MODEL_SHORT)

    def test_missing_parakeet_reports_missing(self):
        self.assertEqual(self.core.model_status("parakeet-tdt-0.6b-v2")[0], "missing")
        self.assertFalse(self.core.model_downloaded("parakeet-tdt-0.6b-v2"))


if __name__ == "__main__":
    unittest.main()
