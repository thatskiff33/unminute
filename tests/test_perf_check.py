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


if __name__ == "__main__":
    unittest.main()
