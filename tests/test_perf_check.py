# tests/test_perf_check.py
# The budget logic of tools/perf_check.py (docs/RELEASE_STANDARD.md, section 2),
# its synthetic microphone and the order of its states. The measuring itself
# opens a real window and is run by hand at release time.

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

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

    def test_a_later_recording_state_with_nothing_recording_is_refused(self):
        # Review of 51a7605: a recording that ended in "recording" must not be
        # replaced by a second one, whose start (and the first one's saving)
        # would land in recording-minimised's figures.
        with self.assertRaises(perf_check.LostRecording):
            perf_check.steps("recording-minimised", False)
        # Recording before minimising: the window keeps the page running then.
        self.assertEqual(["restore", "show:meetings", "minimise"],
                         perf_check.steps("recording-minimised", True))

    def test_the_leak_check_comes_before_any_recording(self):
        self.assertEqual(["meetings", "settings", "minimised", "leak", "recording",
                          "recording-minimised"], perf_check.plan(30, True))
        self.assertEqual(list(perf_check.STATES), perf_check.plan(0, True))
        # Managed policy turns microphone recording off: idle states only.
        self.assertEqual(["meetings", "settings", "minimised", "leak"],
                         perf_check.plan(30, False))

    def test_a_full_run_records_once_and_ends_with_nothing_open(self):
        # The parent's order (plan): the idle states, the leak check's
        # Meetings view, the recording states, quit.
        recording, opened, seen = False, 0, {}
        for action in (*perf_check.plan(30, True), "quit"):
            state = "meetings" if action == "leak" else action
            for step in perf_check.steps(state, recording):
                if step == "record":
                    recording, opened = True, opened + 1
                elif step == "discard":
                    recording = False
            seen.setdefault(action, []).append(recording)
        self.assertEqual(1, opened)
        self.assertEqual({"meetings": [False], "settings": [False], "minimised": [False],
                          "leak": [False], "recording": [True],
                          "recording-minimised": [True], "quit": [False]}, seen)

    def test_a_state_that_starts_a_recording_waits_for_it(self):
        # Review of 51a7605: the child's own waits while starting a recording
        # must fit inside the parent's wait for that state.
        child = sum(perf_check.RECORD_WAITS_S.values())
        for state in perf_check.RECORDING_STATES:
            self.assertGreaterEqual(perf_check.step_timeout(state),
                                    perf_check.STEP_TIMEOUT_S + child, state)
        for state in perf_check.IDLE_STATES:
            self.assertEqual(perf_check.STEP_TIMEOUT_S, perf_check.step_timeout(state))

    def test_the_help_says_one_recording_runs_through_both_states(self):
        help_text = " ".join(perf_check.HELP.split())
        self.assertIn("That one recording runs through the app's own code for both "
                      "recording states", help_text)
        self.assertIn("is discarded after them", help_text)
        self.assertIn("after the idle states and before any recording", help_text)
        self.assertIn("not measured (policy)", help_text)

    def test_the_release_run_time_is_stated_from_its_shape(self):
        # Review of 51a7605: five states made "about 90 minutes" wrong. The
        # floor leaves out start-up and seeding; the worker waits about half
        # of WORKER_WAIT_S (its 120 s idle unload).
        states = len(perf_check.STATES) * (perf_check.RELEASE_SETTLE_S
                                           + perf_check.RELEASE_WINDOW_S)
        floor = (perf_check.RELEASE_RUNS * (states + perf_check.WORKER_WAIT_S / 2)
                 + perf_check.LEAK_BUDGET_MINUTES * 60 + perf_check.RELEASE_SETTLE_S)
        self.assertGreater(floor / 3600, 2.0)
        self.assertLess(floor / 3600, 2.5)
        phrase = "about two and a quarter hours"
        self.assertIn(phrase, " ".join(perf_check.HELP.split()))
        releasing = (ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")
        self.assertIn(phrase, releasing)
        self.assertNotIn("about 90 minutes", releasing)

    def test_the_help_names_the_states_and_the_synthetic_input(self):
        for state in perf_check.STATES:
            self.assertIn(state, perf_check.HELP)
        self.assertIn(f"{perf_check.SYNTHETIC_RATE // 1000} kHz", perf_check.HELP)
        self.assertIn(f"{perf_check.SYNTHETIC_BLOCK_S} s blocks", perf_check.HELP)
        self.assertIn("no microphone", perf_check.HELP)


class HeldRecordingTests(unittest.TestCase):
    """Review of 51a7605: a recording lost during a window fails the run."""

    def test_a_recording_that_kept_going_passes(self):
        got = perf_check.check_held("recording", {"state": "recording", "seconds": 1.2},
                                    {"state": "recording", "seconds": 80.4}, 80.0)
        self.assertEqual(79.2, got)

    def test_a_lost_recording_is_unmeasurable(self):
        before = {"state": "recording", "seconds": 1.0}
        cases = [(before, None),                                      # ended (saved or lost)
                 (before, {"state": "stopped", "seconds": 40.0}),     # stopped itself
                 (before, {"state": "paused", "seconds": 40.0}),      # paused
                 (before, {"state": "recording", "seconds": 10.0}),   # stalled
                 (before, {"state": "recording", "seconds": 0.5}),    # a new one
                 (None, {"state": "recording", "seconds": 80.0})]     # none at the start
        for b, a in cases:
            with self.assertRaises(perf_check.Unmeasurable, msg=repr((b, a))):
                perf_check.check_held("recording-minimised", b, a, 80.0)


class ConsentOutcomeTests(unittest.TestCase):
    """Review of 51a7605: the confirmation may be off by managed policy."""

    def setUp(self):
        from workspace import consent
        self.consent = consent
        self.title = consent.RECORDING_TITLE

    def outcome(self, required, record, asked):
        return perf_check.consent_outcome(required, record, asked, self.title,
                                          self.consent.valid_recording_record)

    def test_required_needs_a_valid_record_and_only_the_recording_question(self):
        record = self.consent.recording_record()
        self.assertEqual("asked and recorded", self.outcome(True, record, [self.title]))
        with self.assertRaises(RuntimeError):
            self.outcome(True, None, [self.title])
        with self.assertRaises(RuntimeError):
            self.outcome(True, {"method": "made up"}, [self.title])
        with self.assertRaises(RuntimeError):
            self.outcome(True, record, [self.title, self.consent.DISCARD_TITLE])

    def test_off_by_policy_accepts_no_record_and_no_question(self):
        self.assertEqual("off by policy", self.outcome(False, None, []))
        with self.assertRaises(RuntimeError):
            self.outcome(False, None, [self.title])
        with self.assertRaises(RuntimeError):
            self.outcome(False, self.consent.recording_record(), [])


class FakeChild:
    """subprocess.Popen for one_run: answers each command from a script, as
    the child process would, and records the commands it was given."""

    def __init__(self, ready, check):
        import queue
        self.commands, self.pid, self._out = [], 4242, queue.Queue()
        self._check, self.returncode = check, None
        self._send(ready)
        test = self

        class Stdin:
            def write(self, text):
                for command in text.splitlines():
                    test._command(command)

            def flush(self):
                pass
        self.stdin = Stdin()

    def _send(self, msg):
        self._out.put(perf_check.PREFIX + json.dumps(msg) + "\n")

    def _command(self, command):
        self.commands.append(command)
        if command == "quit":
            self.returncode = 0
            self._out.put(None)
        elif command == "check":
            self._send({"recording": self._check(self.commands)})
        else:
            msg = {"state": command}
            if command in perf_check.RECORDING_STATES:
                msg["recording"] = {"state": "recording", "seconds": 1.0}
                if command == perf_check.RECORDING_STATES[0]:
                    msg["consent"] = "asked and recorded"
            self._send(msg)

    @property
    def stdout(self):
        while (line := self._out.get(timeout=10)) is not None:
            yield line

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode or 0

    def kill(self):
        self.returncode = -9
        self._out.put(None)


class OneRunTests(unittest.TestCase):
    """The parent's run against a scripted child (no window is opened)."""

    READY = {"imports_s": 1.0, "window_to_interactive_s": 1.0, "startup_s": 2.0,
             "recording_allowed": True}

    def run_child(self, child, leak_minutes=0, held=None):
        from types import SimpleNamespace
        from unittest import mock
        figures = lambda *a: {"cpu_percent_of_one_core": 1.0, "private_mb": 700}  # noqa: E731
        snap = ([], {1: {"private": 2**20}})
        args = SimpleNamespace(app=None, zip=None)
        patches = [mock.patch.object(perf_check.subprocess, "Popen", return_value=child),
                   mock.patch.object(perf_check, "Job"),
                   mock.patch.object(perf_check, "measure", side_effect=figures),
                   mock.patch.object(perf_check, "snapshot", return_value=snap),
                   mock.patch.object(perf_check.time, "sleep"),
                   mock.patch("builtins.print")]
        if held is not None:          # the scripted recording does not really grow
            patches.append(mock.patch.object(perf_check, "check_held", return_value=held))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return perf_check.one_run(args, 0, 0, leak_minutes)

    def test_the_leak_check_runs_before_the_recording_states(self):
        child = FakeChild(self.READY, lambda c: {"state": "recording", "seconds": 61.0})
        result = self.run_child(child, leak_minutes=1, held=60.0)
        self.assertEqual(["meetings", "settings", "minimised", "meetings", "recording",
                          "check", "recording-minimised", "check", "quit"], child.commands)
        self.assertEqual(1, result["leak"]["minutes"])
        self.assertEqual("asked and recorded", result["recording_consent"])
        self.assertEqual(60.0, result["recording-minimised"]["recorded_s"])

    def test_a_recording_lost_during_its_window_is_unmeasurable(self):
        child = FakeChild(self.READY, lambda c: None)       # ended during the window
        with self.assertRaises(perf_check.Unmeasurable) as caught:
            self.run_child(child)
        self.assertIn("the recording ended during recording", str(caught.exception))
        self.assertNotIn("recording-minimised", child.commands)

    def test_policy_off_measures_the_idle_states_only(self):
        child = FakeChild({**self.READY, "recording_allowed": False}, lambda c: None)
        result = self.run_child(child)
        self.assertEqual(["meetings", "settings", "minimised", "quit"], child.commands)
        self.assertEqual(list(perf_check.RECORDING_STATES), result["not_measured_by_policy"])
        for state in perf_check.IDLE_STATES:
            self.assertIn(state, result)


class PolicyReportTests(unittest.TestCase):
    """Review of 51a7605: under managed policy the run is still measured."""

    def run_parent(self, results, budgets):
        import tempfile
        from types import SimpleNamespace
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            report, budget_file = Path(tmp) / "perf.json", Path(tmp) / "budgets.json"
            budget_file.write_text(json.dumps({"budgets": budgets}), encoding="utf-8")
            args = SimpleNamespace(quick=True, settle=0, window=0, runs=1, leak_minutes=0,
                                   report=str(report), app=None)
            with mock.patch.object(perf_check, "environment", return_value={}), \
                    mock.patch.object(perf_check, "one_run", side_effect=results), \
                    mock.patch.object(perf_check, "BUDGETS", budget_file), \
                    mock.patch("builtins.print"):
                rc = perf_check.parent(args)
            return rc, json.loads(report.read_text(encoding="utf-8"))

    def idle(self):
        return {s: {"cpu_percent_of_one_core": 0.2, "private_mb": 700}
                for s in perf_check.IDLE_STATES}

    def test_microphone_off_by_policy_keeps_the_idle_figures(self):
        run = {**self.idle(), "not_measured_by_policy": list(perf_check.RECORDING_STATES)}
        rc, report = self.run_parent([run], {"meetings": {"cpu_percent_of_one_core": 1.0}})
        self.assertEqual(0, rc)
        self.assertEqual(list(perf_check.RECORDING_STATES), report["not_measured_by_policy"])
        self.assertEqual(0.2, report["median"]["meetings"]["cpu_percent_of_one_core"])
        self.assertNotIn("recording", report["median"])

    def test_a_budgeted_recording_state_off_by_policy_is_not_measured(self):
        run = {**self.idle(), "not_measured_by_policy": list(perf_check.RECORDING_STATES)}
        rc, report = self.run_parent([run], {"recording": {"cpu_percent_of_one_core": 5.0}})
        self.assertEqual(1, rc)
        self.assertEqual([{"state": "recording", "metric": "cpu_percent_of_one_core"}],
                         report["not_measured"])

    def test_the_report_says_how_the_recording_was_confirmed(self):
        run = {**self.idle(), "recording": {"cpu_percent_of_one_core": 3.0, "private_mb": 750,
                                            "recorded_s": 79.0},
               "recording_consent": "off by policy"}
        rc, report = self.run_parent([run], {})
        self.assertEqual(0, rc)
        self.assertEqual(["off by policy"], report["recording_consent"])
        self.assertEqual([], report["not_measured_by_policy"])
        self.assertEqual(3.0, report["median"]["recording"]["cpu_percent_of_one_core"])


# -- the machine it ran on (#121: no named reference machine) -------------------------

SYNTHETIC_HOST, SYNTHETIC_USER = "SYNTH-HOST-0001", "synthetic.person"


def webview2_keys():
    """The registry keys perf_check reads the WebView2 version from, in order."""
    return [key for _, key in perf_check.WEBVIEW2_KEYS]


class FakeProbe:
    """Synthetic registry, CIM and system readers for machine_spec. Its CIM
    rows also carry properties that would identify a machine (the computer's
    name, a device instance ID): none may reach the specification. Records
    every registry read."""

    def __init__(self, **change):
        self.values = {
            ("HKLM", perf_check.CPU_KEY, "ProcessorNameString"): "  Synthetic CPU  8000X ",
            ("HKLM", perf_check.WINDOWS_KEY, "CurrentMajorVersionNumber"): 10,
            ("HKLM", perf_check.WINDOWS_KEY, "CurrentMinorVersionNumber"): 0,
            ("HKLM", perf_check.WINDOWS_KEY, "CurrentBuild"): "26100",
            ("HKLM", perf_check.WINDOWS_KEY, "UBR"): 4061,
            ("HKLM", perf_check.WINDOWS_KEY, "DisplayVersion"): "24H2",
            ("HKLM", webview2_keys()[0], "pv"): "140.0.1.2",
        }
        self.cim_rows = {
            "Win32_Processor": [{"NumberOfCores": 8, "SystemName": SYNTHETIC_HOST}],
            "Win32_VideoController": [
                {"Name": "Synthetic GPU B", "DriverVersion": "31.0.1",
                 "PNPDeviceID": "PCI\\VEN_0000&DEV_0000\\4&SYNTH&0&0008"},
                {"Name": "Synthetic GPU A", "DriverVersion": None}],
        }
        self.memory, self.status = 16 * 2**30, (1, 0)        # mains, a battery charging
        self.plan = "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)\n"
        self.logical = 16
        self.reads = []
        for name, value in change.items():
            setattr(self, name, value)

    def registry(self, hive, key, value):
        self.reads.append((hive, key, value))
        return self.values.get((hive, key, value))

    def cim(self):
        return self.cim_rows

    def memory_bytes(self):
        return self.memory

    def power_status(self):
        return self.status

    def power_plan(self):
        return self.plan

    def logical_processors(self):
        return self.logical


class MachineSpecTests(unittest.TestCase):
    def test_the_specification_is_recorded_from_its_readers(self):
        spec = perf_check.machine_spec(FakeProbe())
        self.assertEqual({
            "cpu": "Synthetic CPU 8000X", "cores": 8, "logical_processors": 16,
            "memory_gb": 16.0, "windows": "Windows 11 24H2", "os_build": "10.0.26100.4061",
            "power_plan": "Balanced", "on_mains_power": True, "battery": True,
            "gpus": [{"name": "Synthetic GPU A", "driver": None},
                     {"name": "Synthetic GPU B", "driver": "31.0.1"}],
            "webview2": "140.0.1.2"}, spec)
        # Exactly what release evidence asks for, and complete.
        self.assertEqual(list(perf_check.evidence.PERF_MACHINE), list(spec))
        self.assertEqual([], perf_check.evidence.machine_problems(spec))

    def test_nothing_that_identifies_the_machine_or_its_user_is_recorded(self):
        probe = FakeProbe()
        text = json.dumps(perf_check.machine_spec(probe))
        for marker in (SYNTHETIC_HOST, SYNTHETIC_USER, "PCI", "SYNTH&"):
            self.assertNotIn(marker, text)
        # Only the listed registry values are read (not, for example, the
        # registered owner beside the Windows version).
        for hive, key, value in probe.reads:
            self.assertIn(value, perf_check.REGISTRY_READS[(hive, key)])
        self.assertNotIn("RegisteredOwner", json.dumps(
            {f"{h} {k}": v for (h, k), v in perf_check.REGISTRY_READS.items()}))

    def test_an_unlisted_registry_read_is_refused(self):
        with mock.patch.dict(perf_check.REGISTRY_READS,
                             {("HKLM", perf_check.WINDOWS_KEY): ("CurrentBuild",)}):
            with self.assertRaises(ValueError):
                perf_check.machine_spec(FakeProbe())

    def test_cim_keeps_only_the_listed_properties(self):
        rows = {"Win32_Processor": [{"NumberOfCores": 4, "SystemName": SYNTHETIC_HOST,
                                     "Name": "Synthetic CPU"}],
                "Win32_VideoController": [{"Name": "Synthetic GPU", "DriverVersion": "1.2",
                                           "PNPDeviceID": "PCI\\SYNTH", "SystemName":
                                           SYNTHETIC_HOST}]}
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps(rows).encode("utf-8"))
        # Windows PowerShell itself (full path, minimal environment) is
        # tests/test_windows_powershell.py's.
        ps = perf_check.windows_powershell
        with mock.patch.object(ps, "run", return_value=done) as run:
            got = perf_check.WindowsProbe().cim()
        self.assertEqual({"Win32_Processor": [{"NumberOfCores": 4}],
                          "Win32_VideoController": [{"Name": "Synthetic GPU",
                                                     "DriverVersion": "1.2"}]}, got)
        script = run.call_args.args[0]
        self.assertIs(run.call_args.kwargs["text"], False)
        self.assertIn("Select-Object NumberOfCores)", script)
        self.assertIn("Select-Object Name,DriverVersion)", script)
        for failed in (subprocess.CompletedProcess([], 1, stdout=b""),
                       subprocess.CompletedProcess([], 0, stdout=b"not json"),
                       subprocess.CompletedProcess([], 0, stdout=b"[1, 2]")):
            with self.subTest(failed=failed), \
                    mock.patch.object(ps, "run", return_value=failed):
                self.assertIsNone(perf_check.WindowsProbe().cim())
        missing = FileNotFoundError("Windows PowerShell 5.1 is missing")
        with mock.patch.object(ps, "run", side_effect=missing):
            self.assertIsNone(perf_check.WindowsProbe().cim())

    def test_the_cim_query_only_tries_the_output_encoding(self):
        # ConstrainedLanguage mode refuses setting a .NET property; with
        # $ErrorActionPreference = 'Stop' a bare assignment ends the query.
        script = perf_check.cim_script()
        self.assertIn("try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}",
                      script)
        self.assertEqual(1, script.count("OutputEncoding"))

    @unittest.skipUnless(sys.platform == "win32", "runs Windows PowerShell")
    def test_the_cim_query_answers_in_constrained_language_mode(self):
        # As on a laptop with WDAC or AppLocker script enforcement: the session
        # is switched to ConstrainedLanguage, then runs the probe's own query.
        wrapper = ("$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'; "
                   "Invoke-Expression $env:UNMINUTE_TEST_CIM_SCRIPT")
        r = perf_check.windows_powershell.run(
            wrapper, {"UNMINUTE_TEST_CIM_SCRIPT": perf_check.cim_script()}, text=False,
            timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # Only the exit code and the shape are checked; nothing read is printed.
        self.assertEqual(0, r.returncode, "the CIM query failed in ConstrainedLanguage mode")
        data = json.loads(r.stdout.decode("utf-8-sig", errors="replace"))
        self.assertEqual(set(perf_check.CIM_READS), set(data))

    def test_what_cannot_be_read_is_none_and_named_not_guessed(self):
        probe = FakeProbe(values={}, cim_rows=None, memory=None, status=None, plan=None,
                          logical=None)
        spec = perf_check.machine_spec(probe)
        self.assertEqual(dict.fromkeys(perf_check.evidence.PERF_MACHINE), spec)
        self.assertEqual(list(perf_check.evidence.PERF_MACHINE),
                         perf_check.evidence.machine_problems(spec))
        # One processor whose cores are not reported: the total is unknown.
        rows = {"Win32_Processor": [{"NumberOfCores": 8}, {"NumberOfCores": None}],
                "Win32_VideoController": []}
        spec = perf_check.machine_spec(FakeProbe(cim_rows=rows))
        self.assertIsNone(spec["cores"])
        self.assertEqual([], spec["gpus"])                     # no adapter: known, and empty
        self.assertEqual(["cores"], perf_check.evidence.machine_problems(spec))

    def test_two_processors_add_their_cores(self):
        rows = {"Win32_Processor": [{"NumberOfCores": 8}, {"NumberOfCores": 8}],
                "Win32_VideoController": []}
        self.assertEqual(16, perf_check.machine_spec(FakeProbe(cim_rows=rows))["cores"])

    def test_power_battery_and_plan(self):
        cases = [((1, 128), True, False),         # mains, no battery (a desktop)
                 ((0, 1), False, True),           # on battery
                 ((255, 255), None, None)]        # not known
        for status, mains, battery in cases:
            with self.subTest(status=status):
                spec = perf_check.machine_spec(FakeProbe(status=status))
                self.assertEqual((mains, battery), (spec["on_mains_power"], spec["battery"]))
        for output, plan in (("Power Scheme GUID: 1234  (Ausbalanciert)", "Ausbalanciert"),
                             ("GUID du mode de gestion : 1234  (Utilisation normale)\r\n",
                              "Utilisation normale"),
                             ("", None), ("Access is denied.", None)):
            with self.subTest(output=output):
                self.assertEqual(plan, perf_check._power_plan(output))

    def test_windows_10_and_a_webview2_left_by_an_uninstall(self):
        probe = FakeProbe()
        probe.values.update({("HKLM", perf_check.WINDOWS_KEY, "CurrentBuild"): "19045",
                             ("HKLM", perf_check.WINDOWS_KEY, "DisplayVersion"): "22H2",
                             ("HKLM", webview2_keys()[0], "pv"): "0.0.0.0",
                             ("HKCU", webview2_keys()[2], "pv"): "139.0.9.9"})
        spec = perf_check.machine_spec(probe)
        self.assertEqual(("Windows 10 22H2", "10.0.19045.4061", "139.0.9.9"),
                         (spec["windows"], spec["os_build"], spec["webview2"]))

    def test_a_windows_server_is_not_called_a_client_release(self):
        # Server 2025 shares Windows 11's build (26100) and Server 2022's
        # (20348) is below 22000; InstallationType tells them apart.
        for build, kind, release, expected in (
                ("26100", "Server", "24H2", "Windows Server 24H2"),
                ("20348", "Server Core", "21H2", "Windows Server 21H2"),
                ("26100", "Client", "24H2", "Windows 11 24H2"),
                ("19045", None, "22H2", "Windows 10 22H2")):
            with self.subTest(build=build, kind=kind):
                probe = FakeProbe()
                probe.values.update({
                    ("HKLM", perf_check.WINDOWS_KEY, "CurrentBuild"): build,
                    ("HKLM", perf_check.WINDOWS_KEY, "DisplayVersion"): release,
                    ("HKLM", perf_check.WINDOWS_KEY, "InstallationType"): kind})
                self.assertEqual(expected, perf_check.machine_spec(probe)["windows"])

    @unittest.skipUnless(sys.platform == "win32", "reads this Windows machine")
    def test_this_machine_gives_only_its_specification(self):
        import os
        spec = perf_check.machine_spec(perf_check.WindowsProbe())
        self.assertEqual(list(perf_check.evidence.PERF_MACHINE), list(spec))
        text = json.dumps(spec).lower()
        for name in ("COMPUTERNAME", "USERNAME", "USERPROFILE", "USERDOMAIN"):
            value = os.environ.get(name, "").lower()
            if len(value) >= 3:
                self.assertNotIn(value, text, name)


class MachineReportTests(unittest.TestCase):
    """parent() records the machine at the report's top level, and release
    evidence (--zip) stops before measuring when it cannot be read in full."""

    FULL = {"cpu": "Synthetic CPU", "cores": 4, "logical_processors": 8, "memory_gb": 7.8,
            "windows": "Windows 11 24H2", "os_build": "10.0.26100.1", "power_plan": "Balanced",
            "on_mains_power": True, "battery": True, "gpus": [], "webview2": "140.0.1.2"}

    def run_parent(self, machine, zip_path=None):
        import tempfile
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "perf.json"
            args = SimpleNamespace(quick=True, settle=0, window=0, runs=1, leak_minutes=0,
                                   report=str(report), app=None, zip=zip_path)
            env = {"python": "3.14.0", "machine": machine}
            with mock.patch.object(perf_check, "environment", return_value=env), \
                    mock.patch.object(perf_check, "one_run", return_value={}) as run, \
                    mock.patch.object(perf_check, "BUDGETS", Path(tmp) / "none.json"), \
                    mock.patch("builtins.print"):
                rc = perf_check.parent(args)
            return rc, run, json.loads(report.read_text(encoding="utf-8"))

    def test_the_machine_is_recorded_at_the_top_level(self):
        rc, run, report = self.run_parent(dict(self.FULL))
        self.assertEqual((0, 1), (rc, run.call_count))
        self.assertEqual(self.FULL, report["machine"])
        self.assertEqual({"python": "3.14.0"}, report["environment"])

    def test_release_evidence_stops_before_measuring_an_unreadable_machine(self):
        rc, run, report = self.run_parent(dict(self.FULL, cores=None, gpus=None), "p.zip")
        self.assertEqual(2, rc)
        run.assert_not_called()
        self.assertIn("could not be read in full (cores, gpus)", report["error"])
        # Without --zip (a development run) it is measured and the gap shown.
        rc, run, report = self.run_parent(dict(self.FULL, cores=None))
        self.assertEqual((0, 1), (rc, run.call_count))
        self.assertIsNone(report["machine"]["cores"])

    def test_any_machine_will_do_for_release_evidence(self):
        # No named reference machine: a 4-core, 8 GB laptop measures as well.
        rc, run, report = self.run_parent(dict(self.FULL), "p.zip")
        self.assertEqual((0, 1), (rc, run.call_count))


if __name__ == "__main__":
    unittest.main()
