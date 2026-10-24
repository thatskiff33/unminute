# Tests for the live-draft trial harness (tools/live_trial.py, #104) and the
# draft mode of tools/compare_engines.py. Synthetic words and tones only: no
# meeting audio or transcript is used or written.

import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # fake_worker_engines

import compare_engines  # noqa: E402
import live_trial  # noqa: E402

from workspace import drafts  # noqa: E402

FAKE = "test:fake_worker_engines:"

# A fictional AMI-style word file: the same shape as the corpus's
# <meeting>.<speaker>.words.xml (timed words, punctuation marked, other events).
WORDS_A = """<?xml version="1.0" encoding="ISO-8859-1"?>
<nite:root nite:id="X1.A.words" xmlns:nite="http://nite.sourceforge.net/">
   <w nite:id="X1.A.words0" starttime="0.50" endtime="0.90">Alpha</w>
   <w nite:id="X1.A.words1" starttime="0.90" endtime="0.90" punc="true">,</w>
   <vocalsound nite:id="X1.A.words2" starttime="1.0" endtime="1.2" type="laugh"/>
   <w nite:id="X1.A.words3" starttime="3.00" endtime="3.40">gamma</w>
   <w nite:id="X1.A.words4">untimed</w>
</nite:root>
"""
WORDS_B = """<?xml version="1.0" encoding="ISO-8859-1"?>
<nite:root nite:id="X1.B.words" xmlns:nite="http://nite.sourceforge.net/">
   <w nite:id="X1.B.words0" starttime="1.50" endtime="2.00">beta</w>
   <w nite:id="X1.B.words1" starttime="9.00" endtime="9.50">delta</w>
</nite:root>
"""


class AmiWordsTests(unittest.TestCase):
    def test_words_from_every_speaker_merge_in_time_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "X1.A.words.xml").write_text(WORDS_A, encoding="iso-8859-1")
            Path(tmp, "X1.B.words.xml").write_text(WORDS_B, encoding="iso-8859-1")
            Path(tmp, "X2.A.words.xml").write_text(WORDS_B, encoding="iso-8859-1")
            words = live_trial.ami_words(tmp, "X1")
        # Punctuation, untimed words, other meetings and non-word events left out.
        self.assertEqual([w for _, _, w in words], ["Alpha", "beta", "gamma", "delta"])
        self.assertEqual(words[0][:2], (0.5, 0.9))

    def test_a_meeting_with_no_word_files_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit):
            live_trial.ami_words(tmp, "X1")

    def test_a_window_keeps_words_that_start_in_it_on_its_own_clock(self):
        words = [(0.5, 0.9, "a"), (1.5, 2.0, "b"), (3.0, 3.4, "c"), (9.0, 9.5, "d")]
        self.assertEqual(live_trial.in_window(words, 1.0, 3.0),
                         [(0.5, 1.0, "b"), (2.0, 2.4, "c")])
        self.assertEqual(live_trial.in_window(words, 0.0, None), words)


class AlignTests(unittest.TestCase):
    def test_matches_follow_the_best_alignment(self):
        ref = "the cat sat on the mat".split()
        hyp = "a cat sat on mat today".split()
        errors, pairs = live_trial.align(ref, hyp)
        self.assertEqual(errors, compare_engines.edit_distance(ref, hyp))
        self.assertEqual(pairs, [(1, 1), (2, 2), (3, 3), (5, 4)])

    def test_empty_sides(self):
        self.assertEqual(live_trial.align([], ["a"]), (1, []))
        self.assertEqual(live_trial.align(["a", "b"], []), (2, []))


class ScoreTests(unittest.TestCase):
    def test_delay_is_when_a_matched_word_appears_minus_when_it_was_spoken(self):
        # Spoken words (end times) and two draft phrases, each available at once.
        spoken = [(0.0, 0.5, "alpha"), (0.5, 1.0, "beta"), (1.0, 1.5, "gamma"),
                  (4.0, 4.5, "delta"), (4.5, 5.0, "echo")]
        phrases = [{"start": 0.0, "end": 1.6, "text": "Alpha beta gamma.", "at": 3.0},
                   {"start": 3.9, "end": 5.2, "text": "delta, foxtrot", "at": 6.5}]
        got = live_trial.score(phrases, spoken)
        # Matched: alpha 3.0-0.5, beta 3.0-1.0, gamma 3.0-1.5, delta 6.5-4.5.
        self.assertEqual(got["matched"], 4)
        self.assertEqual(got["words"], 5)
        self.assertEqual(got["errors"], 1)             # foxtrot for echo
        self.assertEqual(got["wer_pct"], 20.0)
        self.assertAlmostEqual(got["delay_median_s"], 2.0)
        self.assertAlmostEqual(got["delay_p90_s"], 2.5)

    def test_phrases_are_scored_in_the_order_they_were_spoken(self):
        spoken = [(0.0, 0.5, "alpha"), (2.0, 2.5, "beta")]
        late_first = [{"start": 1.9, "end": 2.6, "text": "beta", "at": 3.0},
                      {"start": 0.0, "end": 0.6, "text": "alpha", "at": 4.0}]
        got = live_trial.score(late_first, spoken)
        self.assertEqual(got["errors"], 0)
        self.assertEqual(sorted(got["delays"]), [0.5, 3.5])

    def test_no_spoken_words_gives_no_figures(self):
        got = live_trial.score([], [])
        self.assertIsNone(got["wer_pct"])
        self.assertIsNone(got["delay_median_s"])

    def test_percentile_is_the_nearest_rank(self):
        self.assertEqual(live_trial.percentile([5, 1, 3, 2, 4], 50), 3)
        self.assertEqual(live_trial.percentile(list(range(1, 11)), 90), 9)
        self.assertIsNone(live_trial.percentile([], 50))


class CutTests(unittest.TestCase):
    def test_the_cut_reaches_new_phrase_splitters_and_is_restored(self):
        before = drafts.MAX_PHRASE_S
        with compare_engines.draft_cut(6.0):
            self.assertEqual(drafts.PhraseSplitter().max_w, drafts._windows(6.0))
        self.assertEqual(drafts.MAX_PHRASE_S, before)

    def test_a_cut_outside_the_trial_range_is_refused(self):
        for bad in (1.0, 31.0, float("nan")):
            with self.assertRaises(ValueError), compare_engines.draft_cut(bad):
                pass

    def test_unbroken_speech_is_cut_at_the_trial_length(self):
        # 12 s of tone (speech to the fake engine): cut near 6 s, not at the shipped cut.
        engine = _FakeDraft()
        tone = _tone(12.0)
        with compare_engines.draft_cut(6.0):
            phrases = compare_engines.draft_phrases(engine, tone)
        self.assertGreaterEqual(len(phrases), 2)
        for start, end, _ in phrases:
            self.assertLessEqual(end - start, _longest(6.0))


def _longest(cut_s):
    """The longest phrase a cut allows: whole 32 ms windows."""
    return drafts._windows(cut_s) * drafts.WINDOW_S + 1e-6


class _FakeDraft:
    """In-process stand-in for the draft engine: loud windows are speech."""

    def speech_prob(self, window):
        return 1.0 if float(np.sqrt(np.mean(np.square(window)))) > 0.02 else 0.0

    def reset(self):
        pass

    def transcribe_phrase(self, audio):
        return f"phrase {len(audio) / 16000:.1f}"


def _tone(seconds, rate=16000, lead=0.5):
    t = np.arange(int(seconds * rate)) / rate
    x = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    quiet = np.zeros(int(lead * rate), dtype=np.float32)
    return np.concatenate([quiet, x, quiet])


class LiveRunTests(unittest.TestCase):
    """The real draft session and draft worker process, with the fake draft
    engine, fed faster than real time (so its delays mean nothing)."""

    def test_the_draft_worker_cuts_at_the_trial_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            pcm = (_tone(5.0) * 32767).astype(np.int16)
            run = live_trial.run_cut(pcm, 16000, 2.0, cache=tmp,
                                     model=FAKE + "draft_phrases", allow_test=True,
                                     speed=10.0, quiet_s=1.5)
        phrases = run["phrases"]
        self.assertGreaterEqual(len(phrases), 2, run["state"])
        for p in phrases:
            # The worker's own splitter used the 2 s cut (the shipped cut would give one phrase).
            self.assertLessEqual(p["end"] - p["start"], _longest(2.0) + 1e-3)
            self.assertGreaterEqual(p["at"], 0.0)
        self.assertEqual(run["state"], drafts.RUNNING)
        self.assertEqual(run["skipped_s"], 0.0)
        self.assertEqual(run["dropped_s"], 0.0)

    def test_results_carry_counts_and_times_never_text(self):
        runs = [{"cut_s": 8.0, "phrases": [{"start": 0.0, "end": 1.0, "text": "secret",
                                            "at": 2.0}],
                 "load_s": 1.0, "cpu_s": 2.0, "wall_s": 20.0, "peak_gb": 0.9,
                 "skipped_s": 0.0, "dropped_s": 0.0, "state": "running"}]
        spoken = [(0.0, 1.0, "secret")]
        report = live_trial.report(runs, final_words=spoken, ref_words=None,
                                   config={"seconds": 1.0})
        self.assertNotIn("secret", repr(report))
        self.assertNotIn("secret", live_trial.table(report))
        row = report["runs"][0]
        self.assertEqual(row["agreement_pct"], 100.0)
        self.assertEqual(row["cpu_pct_one_core"], 10.0)


class CommandLineTests(unittest.TestCase):
    def test_audio_is_fed_in_the_recorders_blocks(self):
        from workspace import recorder
        self.assertEqual(live_trial.BLOCK_S, recorder.BLOCK_S)

    def test_help_runs(self):
        import subprocess
        tool = TOOLS / "live_trial.py"
        for args in ([], ["run"], ["manifest"]):
            r = subprocess.run([sys.executable, str(tool), *args, "--help"],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_run_needs_an_explicit_model_cache(self):
        # Never the installed app's models by default.
        import subprocess
        r = subprocess.run([sys.executable, str(TOOLS / "live_trial.py"), "run",
                            "--out", os.devnull, "x.wav"],
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--cache", r.stderr)


if __name__ == "__main__":
    unittest.main()
