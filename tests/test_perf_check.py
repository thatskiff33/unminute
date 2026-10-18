# tests/test_perf_check.py
# The budget logic of tools/perf_check.py (docs/RELEASE_STANDARD.md, section 2),
# its synthetic microphone and the order of its states. The measuring itself
# opens a real window and is run by hand at release time.

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import perf_check  # noqa: E402


class MedianTests(unittest.TestCase):
    def test_median_per_state_and_metric(self):
        runs = [{"meetings": {"cpu_percent_of_one_core": v, "private_mb": m, "names": ["x"]}}
                for v, m in ((0.3, 300), (0.1, 310), (0.2, 290))]
        self.assertEqual({"meetings": {"cpu_percent_of_one_core": 0.2, "private_mb": 300}},
                         perf_check.median_report(runs))

    def test_a_figure_missing_from_one_run_uses_the_others(self):
        runs = [{"leak": {"private_growth_mb": None}}, {"leak": {"private_growth_mb": 4.0}},
                {"leak": {"private_growth_mb": 6.0}}]
        self.assertEqual({"leak": {"private_growth_mb": 5.0}}, perf_check.median_report(runs))


class BudgetTests(unittest.TestCase):
    BUDGETS = {"meetings": {"cpu_percent_of_one_core": 0.3, "private_mb": None},
               "minimised": {"cpu_percent_of_one_core": 0.1},
               "worker": {"exit_after_last_use_s": 125},
               "leak": {"private_growth_mb": 10}}

    def test_over_budget_and_not_measured_are_both_reported(self):
        figures = {"meetings": {"cpu_percent_of_one_core": 0.4, "private_mb": 300},
                   "minimised": {"cpu_percent_of_one_core": 0.05}}
        over, missing = perf_check.compare(figures, self.BUDGETS)
        self.assertEqual([("meetings", "cpu_percent_of_one_core", 0.4, 0.3)], over)
        # A set budget with no figure fails; a budget of null is not set yet.
        self.assertEqual([("worker", "exit_after_last_use_s"), ("leak", "private_growth_mb")],
                         missing)

    def test_a_state_not_asked_for_is_skipped(self):
        figures = {"meetings": {"cpu_percent_of_one_core": 0.2},
                   "minimised": {"cpu_percent_of_one_core": 0.05},
                   "worker": {"exit_after_last_use_s": 121.0}}
        self.assertEqual(([], []), perf_check.compare(figures, self.BUDGETS, skip=("leak",)))

    def test_a_worker_that_never_unloads_is_over_its_budget(self):
        # one_run records it at the wait limit rather than leaving it out.
        figures = {"worker": {"exit_after_last_use_s": perf_check.WORKER_WAIT_S}}
        over, _ = perf_check.compare(figures, {"worker": {"exit_after_last_use_s": 125}})
        self.assertEqual([("worker", "exit_after_last_use_s", perf_check.WORKER_WAIT_S, 125)],
                         over)

    def test_one_failed_unload_in_three_runs_fails(self):
        # The median of [240, 121, 121] would pass; unloading is pass or fail.
        runs = [{"worker": {"exited": True, "exit_after_last_use_s": 121.0}},
                {"worker": {"exited": False, "exit_after_last_use_s": perf_check.WORKER_WAIT_S}},
                {"worker": {"exited": True, "exit_after_last_use_s": 121.0}}]
        worst = perf_check.worst_worker(runs)
        self.assertEqual(perf_check.WORKER_WAIT_S, worst)
        over, _ = perf_check.compare({"worker": {"exit_after_last_use_s": worst}},
                                     {"worker": {"exit_after_last_use_s": 125}})
        self.assertEqual(1, len(over))
        self.assertIsNone(perf_check.worst_worker([{"meetings": {}}]))

    def test_proposed_budgets_add_the_margin_and_round_up(self):
        proposed = perf_check.propose({"meetings": {"cpu_percent_of_one_core": 1.0,
                                                    "private_mb": 1000, "processes": 9},
                                       "settings": {"cpu_percent_of_one_core": 3.229},
                                       "minimised": {"cpu_percent_of_one_core": 0.01},
                                       "startup": {"startup_s": 2.0}})
        self.assertEqual({"meetings": {"cpu_percent_of_one_core": 1.2, "private_mb": 1200},
                          "settings": {"cpu_percent_of_one_core": 3.88},     # 3.8748, up
                          "minimised": {"cpu_percent_of_one_core": 0.06},
                          "startup": {"startup_s": 2.4}}, proposed)

    def test_budget_file_is_valid_and_uses_known_states_and_metrics(self):
        path = ROOT / "docs" / "perf-budgets.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        states = set(perf_check.STATES) | {"startup", "worker", "leak"}
        metrics = set(perf_check.BUDGETED) | {"exit_after_last_use_s", "private_growth_mb"}
        for state, limits in data["budgets"].items():
            self.assertIn(state, states, f"unknown state in {path.name}")
            self.assertTrue(set(limits) <= metrics, f"{state}: unknown metric in {path.name}")
            for metric, limit in limits.items():
                self.assertTrue(limit is None or limit > 0, f"{state} {metric}")


class ParentTests(unittest.TestCase):
    def test_a_failing_environment_probe_is_not_measured_not_a_traceback(self):
        import tempfile
        from types import SimpleNamespace
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "perf.json"
            args = SimpleNamespace(quick=True, settle=0, window=0, runs=1, leak_minutes=0,
                                   report=str(report), app=None)
            with mock.patch.object(perf_check, "environment",
                                   side_effect=ValueError("damaged version.json")), \
                    mock.patch.object(perf_check, "one_run") as run, \
                    mock.patch("builtins.print"):
                self.assertEqual(perf_check.parent(args), 2)
            run.assert_not_called()
            saved = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(saved["error"],
                             "the environment probe failed (ValueError: damaged version.json)")
            self.assertEqual(saved["each_run"], [])

    def test_the_help_says_the_worker_unload_uses_the_slowest_run(self):
        self.assertIn("the slowest run", perf_check.HELP)



class FakeClock:
    """Monotonic time that moves only when the stream waits (or a test says)."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds
        return False                     # never stopped by waiting


def paced(clock, on_block, block_s=0.1, rate=1000, **kw):
    """A PacedStream on a fake clock with small blocks; on_block(stream, block, frames)."""
    finished = []
    blocks = perf_check.synthetic_blocks(rate, block_s, seconds=1)
    holder = {}

    def callback(block, frames, time_info, status):
        on_block(holder["stream"], block, frames)
    holder["stream"] = perf_check.PacedStream(rate, callback, lambda: finished.append(1),
                                              block_s=block_s, blocks=blocks, clock=clock,
                                              wait=clock.wait, **kw)
    return holder["stream"], finished


class SyntheticAudioTests(unittest.TestCase):
    def test_blocks_are_shaped_like_sounddevice_and_sound_like_speech(self):
        import numpy as np
        blocks = perf_check.synthetic_blocks()
        frames = int(perf_check.SYNTHETIC_RATE * perf_check.SYNTHETIC_BLOCK_S)
        self.assertEqual(60, len(blocks))                       # 6 s, looped
        for block in blocks:
            self.assertEqual((frames, 1), block.shape)          # (frames, channels)
            self.assertEqual(np.int16, block.dtype)             # the recorder's dtype
        rms = [float(np.sqrt(np.mean(b.astype(float) ** 2))) for b in blocks]
        self.assertGreater(max(rms), 1000)                      # the meter moves...
        self.assertLess(min(rms), max(rms) / 4)                 # ...with quieter moments
        self.assertLess(max(int(np.abs(b.astype(int)).max()) for b in blocks), 32767)

    def test_the_same_audio_every_run(self):
        a, b = perf_check.synthetic_blocks(seconds=1), perf_check.synthetic_blocks(seconds=1)
        self.assertTrue(all((x == y).all() for x, y in zip(a, b, strict=True)))


class PacedStreamTests(unittest.TestCase):
    def test_blocks_arrive_at_real_time_pace(self):
        clock, times = FakeClock(), []

        def on_block(stream, block, frames):
            times.append(round(clock(), 6))
            self.assertEqual((frames, 1), block.shape)
            if len(times) == 10:
                stream.stop()
        stream, finished = paced(clock, on_block)
        stream._run()
        # One block each time one block of time has passed, never early.
        self.assertEqual([round(0.1 * (n + 1), 6) for n in range(10)], times)
        self.assertEqual(10, stream.delivered)
        self.assertEqual(100, stream.frames)
        self.assertEqual([1], finished)                     # once, as on stop()

    def test_a_short_hold_up_catches_up_without_drift(self):
        clock, times = FakeClock(), []

        def on_block(stream, block, frames):
            times.append(round(clock(), 6))
            if len(times) == 3:
                clock.now += 0.35                           # a busy moment, under the limit
            if clock() >= 2.0 - 1e-9:
                stream.stop()
        stream, _ = paced(clock, on_block)
        stream._run()
        # The schedule is fixed from the start: 20 blocks by 2.0 s, none skipped.
        self.assertEqual(20, stream.delivered)
        self.assertEqual(0, stream.skipped)
        self.assertEqual(2.0, times[-1])

    def test_a_long_hold_up_carries_on_from_now_without_a_burst(self):
        clock, times = FakeClock(), []

        def on_block(stream, block, frames):
            times.append(round(clock(), 6))
            if len(times) == 3:
                clock.now += 5.0                            # held up beyond max_late_s
            if len(times) == 8:
                stream.stop()
        stream, _ = paced(clock, on_block)
        stream._run()
        gaps = [round(b - a, 6) for a, b in zip(times[3:], times[4:], strict=False)]
        self.assertEqual([0.1] * 4, gaps)                   # no burst of old blocks
        self.assertEqual(49, stream.skipped)

    def test_a_failing_callback_ends_the_stream_as_portaudio_does(self):
        clock = FakeClock()

        def on_block(stream, block, frames):
            raise RuntimeError("callback failed")
        stream, finished = paced(clock, on_block)
        stream._run()
        self.assertEqual([1], finished)        # the recorder then reports a lost device
        stream.close()
        self.assertEqual([1], finished)        # never twice

    def test_stop_ends_the_thread_promptly(self):
        import threading
        got = threading.Event()
        finished = []
        stream = perf_check.PacedStream(
            1000, lambda *a: got.set(), lambda: finished.append(1), block_s=0.01,
            blocks=perf_check.synthetic_blocks(1000, 0.01, seconds=0.1))
        stream.start()
        self.assertTrue(got.wait(5))
        stream.stop()
        self.assertFalse(stream._thread.is_alive())
        stream.close()
        self.assertEqual([1], finished)

    def test_the_recorder_saves_the_synthetic_audio_at_its_rate(self):
        # The seam the child uses: workspace.recorder.Recording's open_stream.
        import tempfile
        import time

        from workspace import recorder
        self.assertEqual(perf_check.SYNTHETIC_BLOCK_S, recorder.BLOCK_S)
        with tempfile.TemporaryDirectory() as tmp:
            rec = recorder.Recording(
                Path(tmp) / "rec", 0, "Synthetic input", 16000,
                open_stream=lambda device, rate, cb, done: perf_check.PacedStream(
                    rate, cb, done, blocks=perf_check.synthetic_blocks(rate, seconds=1)))
            rec.start()
            time.sleep(1.5)
            self.assertEqual("recording", rec.status()["state"])
            journal = rec.stop()
        seconds = journal["frames"] / 16000
        self.assertGreater(seconds, 0.7)                    # real time, give or take
        self.assertLess(seconds, 2.5)
        self.assertEqual("stopped", journal["state"])       # stopped, not a lost device
        self.assertFalse([e for e in journal["events"]
                          if e["kind"] in ("dropped", "overrun", "gap", "device_lost")])


class ConsentAnswerTests(unittest.TestCase):
    def test_only_the_recording_question_is_answered_yes(self):
        from workspace import cleanup, consent
        confirm, asked = perf_check.consent_answerer(consent.RECORDING_TITLE)
        self.assertTrue(confirm(consent.recording_question()))
        self.assertFalse(confirm(consent.delete_originals_question("C:/synthetic")))
        self.assertFalse(confirm(consent.discard_question("Synthetic", 0, cleanup.LIMITS)))
        self.assertEqual([consent.RECORDING_TITLE, consent.DELETE_ORIGINALS_TITLE,
                          consent.DISCARD_TITLE], asked)

    def test_the_shipped_bridge_makes_the_consent_record(self):
        # The child passes the answerer to make_api; the bridge, not the tool,
        # asks the question and makes the record start_recording needs.
        from unittest import mock

        from workspace import api, consent, diag
        ws = mock.Mock()
        ws.consent_required.return_value = True
        ws.start_recording.side_effect = lambda index, name, consent=None: {"started": True}
        confirm, asked = perf_check.consent_answerer(consent.RECORDING_TITLE)
        before = diag._current
        try:
            bridge = api.make_api(ws, confirm=confirm)
            result = bridge.start_recording(0, perf_check.SYNTHETIC_DEVICE["name"])
        finally:
            diag.set_current(before)
        self.assertEqual({"ok": True, "value": {"started": True}}, result)
        self.assertEqual([consent.RECORDING_TITLE], asked)
        record = ws.start_recording.call_args.kwargs["consent"]
        self.assertTrue(consent.valid_recording_record(record))
        self.assertEqual("app-window", record["method"])


class StateSequenceTests(unittest.TestCase):
    def test_recording_states_come_after_the_idle_ones(self):
        self.assertEqual(("meetings", "settings", "minimised"), perf_check.STATES[:3])
        self.assertEqual(perf_check.RECORDING_STATES, perf_check.STATES[3:])

    def test_each_state_from_idle(self):
        steps = perf_check.steps
        self.assertEqual(["restore", "show:meetings"], steps("meetings", False))
        self.assertEqual(["restore", "show:settings"], steps("settings", False))
        self.assertEqual(["restore", "show:meetings", "minimise"], steps("minimised", False))
        self.assertEqual(["restore", "show:meetings", "record"], steps("recording", False))
        # Recording before minimising: the window keeps the page running then.
        self.assertEqual(["restore", "show:meetings", "record", "minimise"],
                         steps("recording-minimised", False))
        self.assertEqual([], steps("quit", False))
        with self.assertRaises(ValueError):
            steps("recording_minimised", False)

    def test_an_open_recording_is_kept_only_for_the_recording_states(self):
        steps = perf_check.steps
        self.assertEqual(["restore", "show:meetings"], steps("recording", True))
        self.assertEqual(["restore", "show:meetings", "minimise"],
                         steps("recording-minimised", True))
        for state in ("meetings", "settings", "minimised"):
            self.assertEqual(["restore", "discard"], steps(state, True)[:2], state)
        self.assertEqual(["discard"], steps("quit", True))

    def test_a_full_run_records_once_and_ends_with_nothing_open(self):
        # The parent's order: every state, the leak check's Meetings view, quit.
        recording, opened, seen = False, 0, {}
        for state in (*perf_check.STATES, "meetings", "quit"):
            for step in perf_check.steps(state, recording):
                if step == "record":
                    recording, opened = True, opened + 1
                elif step == "discard":
                    recording = False
            seen.setdefault(state, []).append(recording)
        self.assertEqual(1, opened)
        self.assertEqual({"meetings": [False, False], "settings": [False],
                          "minimised": [False], "recording": [True],
                          "recording-minimised": [True], "quit": [False]}, seen)

    def test_the_help_names_the_states_and_the_synthetic_input(self):
        for state in perf_check.STATES:
            self.assertIn(state, perf_check.HELP)
        self.assertIn(f"{perf_check.SYNTHETIC_RATE // 1000} kHz", perf_check.HELP)
        self.assertIn(f"{perf_check.SYNTHETIC_BLOCK_S} s blocks", perf_check.HELP)
        self.assertIn("no microphone", perf_check.HELP)


if __name__ == "__main__":
    unittest.main()
