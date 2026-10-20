# Speaker separation after transcription (#16), with fake diarizers in a real
# worker process (no models).
#   python -m unittest discover -s tests -v

import importlib.util
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
SR = 16000

if HAVE_NUMPY:
    import numpy as np

    from workspace.asr_engines import Segment
    from workspace import speakers
    from workspace.speakers import SpeakerResult
    from workspace.store import Store
    from workspace.worker import WorkerHandle


def fake(name):
    return f"test:fake_worker_engines:{name}"


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class SpeakerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / "db.sqlite", protector=keyprotect.TestProtector())
        self.handle = WorkerHandle(root, allow_test_engines=True)
        self.audio = root / "audio.f32"
        np.zeros(60 * SR, dtype="<f4").tofile(self.audio)
        mid = self.store.create_meeting("t")
        sid = self.store.add_source(mid, original_path=None, original_sha256="a" * 64,
                                    original_size=1, original_mtime_ns=0, audio_id="b" * 64,
                                    samples=60 * SR, sample_rate=SR)
        plan = [(a * SR, (a + 20) * SR, a * SR, (a + 20) * SR) for a in (0, 20, 40)]
        self.vid = self.store.ensure_version(mid, sid, engine="fake", model="fake",
                                             model_revision="1", precision="int8", config={},
                                             chunk_plan=plan)
        self.mid = mid
        # A committed transcript: one 4 s sentence every 5 s.
        for idx in range(3):
            segs = [Segment(t, t + 4.0, f"sentence at {t:.0f}")
                    for t in np.arange(idx * 20.0 + 1, idx * 20.0 + 20, 5.0)]
            self.store.commit_chunk(self.vid, idx, segs)
        self.before = [(r["id"], r["text"]) for r in self.store.transcript(self.vid)]

    def tearDown(self):
        self.handle.close()
        self.store.close()
        self.tmp.cleanup()

    def run_speakers(self, model="fake_diarizer", hint=None, audio=None):
        return speakers.run_speakers(self.store, self.mid, self.vid, self.handle,
                                     audio or self.audio, threads=1, hint=hint,
                                     model=fake(model))

    def assertTranscriptUntouched(self):
        self.assertEqual([(r["id"], r["text"]) for r in self.store.transcript(self.vid)],
                         self.before)

    def test_attribution_and_overlap_without_touching_text(self):
        self.assertEqual(self.run_speakers(), SpeakerResult.DONE)
        self.assertTranscriptUntouched()
        labels = self.store.current_speakers(self.vid)
        by_text = {r["text"]: labels[r["id"]] for r in self.store.transcript(self.vid)}
        self.assertEqual(by_text["sentence at 1"], ("SPEAKER_00", False))
        self.assertEqual(by_text["sentence at 11"], ("SPEAKER_01", False))
        self.assertEqual(by_text["sentence at 21"], ("SPEAKER_00", False))
        self.assertEqual(by_text["sentence at 26"], ("SPEAKER_00", True))   # B talked over A

    def test_failure_keeps_the_transcript_usable(self):
        self.assertEqual(self.run_speakers("diarizer_fails"), SpeakerResult.FAILED)
        self.assertTranscriptUntouched()
        self.assertEqual(self.store.current_speakers(self.vid), {})
        run = self.store.speaker_runs(self.vid)[-1]
        self.assertEqual(run["status"], "failed")
        self.assertIn("failed verification", run["error"])
        self.assertNotIn("Traceback", run["error"])

    def test_interrupted_pass_says_it_restarts_and_then_completes(self):
        threading.Timer(1.0, self.handle.stop_now).start()
        self.assertEqual(self.run_speakers("diarizer_sleepy"), SpeakerResult.STOPPED)
        run = self.store.speaker_runs(self.vid)[-1]
        self.assertEqual(run["status"], "interrupted")
        self.assertIn("start again from the beginning", run["error"])
        self.assertTranscriptUntouched()
        self.assertEqual(self.run_speakers(), SpeakerResult.DONE)

    def test_crashed_pass_is_marked_interrupted_on_reopen(self):
        self.store.start_speaker_run(self.vid)                 # app dies mid-pass
        path = self.store.path
        self.store.close()
        self.store = Store(path, protector=keyprotect.TestProtector())
        run = self.store.speaker_runs(self.vid)[-1]
        self.assertEqual(run["status"], "interrupted")
        self.assertIn("start again from the beginning", run["error"])

    def test_known_count_rerun_changes_only_the_speaker_phase(self):
        self.run_speakers()
        self.assertEqual(self.run_speakers(hint=3), SpeakerResult.DONE)
        self.assertTranscriptUntouched()
        runs = self.store.speaker_runs(self.vid)
        self.assertEqual([r["status"] for r in runs], ["superseded", "done"])
        self.assertEqual(runs[-1]["hint_count"], 3)
        used = {v[0] for v in self.store.current_speakers(self.vid).values()}
        self.assertEqual(used, {"SPEAKER_00", "SPEAKER_01", "SPEAKER_02"})

    def test_failed_rerun_keeps_the_previous_labels(self):
        self.assertEqual(self.run_speakers(), SpeakerResult.DONE)
        good = self.store.current_speakers(self.vid)
        self.assertEqual(self.run_speakers("diarizer_fails", hint=3), SpeakerResult.FAILED)
        self.assertEqual(self.store.current_speakers(self.vid), good)
        self.assertEqual([r["status"] for r in self.store.speaker_runs(self.vid)],
                         ["done", "failed"])

    def test_result_after_content_change_is_refused(self):
        rid = self.store.start_speaker_run(self.vid)
        with self.store.tx() as db:                        # e.g. cleanup started (#17)
            db.execute("UPDATE meetings SET content_epoch = content_epoch + 1")
        self.assertFalse(self.store.finish_speaker_run(rid, [(0, 60, "A")], [(0, 60, "A")]))
        self.assertEqual(self.store.current_speakers(self.vid), {})

    def test_handover_is_not_overlap(self):
        from workspace.store import attribute
        turns = [(0.0, 10.0, "A"), (10.0, 20.0, "B")]
        self.assertEqual(attribute(8.0, 12.0, turns, turns), ("A", 0))
        self.assertEqual(attribute(8.0, 12.0, turns + [(9.0, 11.0, "C")], turns)[1], 1)

    def test_failed_speaker_pass_does_not_break_next_transcription(self):
        from workspace.worker import diarize
        self.handle.load(fake("echo"), 1)
        from workspace.worker import WorkerError
        with self.assertRaises(WorkerError):
            diarize(self.handle, fake("diarizer_fails"), self.audio, None, 1)
        self.handle.load(fake("echo"), 1)                  # must reload, not assume loaded
        segs, _ = self.handle.transcribe(np.zeros(SR, dtype=np.float32))
        self.assertTrue(segs)

    def test_error_messages_hide_paths_with_spaces(self):
        from workspace.worker import safe_message
        exc = FileNotFoundError(2, "No such file", "C:\\Users\\Jo Doe\\a.f32")
        self.assertNotIn("Doe", safe_message(exc))

    def test_refusals(self):
        self.assertEqual(self.run_speakers(audio=Path(self.tmp.name) / "gone.f32"),
                         SpeakerResult.NO_AUDIO)
        for bad in (0, 21, "3", 2.5, True):
            self.assertEqual(self.run_speakers(hint=bad), SpeakerResult.BAD_HINT)
        self.assertEqual(self.store.speaker_runs(self.vid), [])

    def test_not_run_while_transcription_is_pending(self):
        sid = self.store.version(self.vid)["source_id"]
        vid2 = self.store.ensure_version(self.mid, sid, engine="fake", model="fake",
                                         model_revision="2", precision="int8", config={},
                                         chunk_plan=[(0, 60 * SR, 0, 60 * SR)])
        self.assertEqual(speakers.run_speakers(self.store, self.mid, vid2, self.handle,
                                               self.audio, threads=1,
                                               model=fake("fake_diarizer")),
                         SpeakerResult.NOT_TRANSCRIBED)

    def test_speech_model_is_released_before_speaker_separation(self):
        from workspace.worker import diarize
        self.handle.load(fake("echo"), 1)
        *_, asr_was_loaded = diarize(self.handle, fake("fake_diarizer"), self.audio, None, 1)
        self.assertTrue(asr_was_loaded)            # it was resident, and the worker freed it
        self.assertIsNone(self.handle._loaded)     # next transcription reloads it
        self.handle.load(fake("echo"), 1)
        segs, _ = self.handle.transcribe(np.zeros(SR, dtype=np.float32))
        self.assertTrue(segs)


if __name__ == "__main__":
    unittest.main()
