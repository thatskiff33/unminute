# tests/test_perf_check.py
# The budget logic of tools/perf_check.py (docs/RELEASE_STANDARD.md, section 2),
# its synthetic microphone and the order of its states. The measuring itself
# opens a real window and is run by hand at release time.

import json
import shutil
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

    def test_every_measured_state_has_a_cpu_budget(self):
        # #216: the recording states were measured with no budget, so a release
        # run could not fail on them. The owner set their budgets on 23 Oct 2026
        # from a release-mode run; every state perf_check measures now has one.
        path = ROOT / "docs" / "perf-budgets.json"
        budgets = json.loads(path.read_text(encoding="utf-8"))["budgets"]
        for state in perf_check.STATES:
            limit = budgets.get(state, {}).get("cpu_percent_of_one_core")
            self.assertIsNotNone(limit, f"{state}: no CPU budget in {path.name}")

    def test_drafts_on_budgets_wait_for_perf_check_states(self):
        # #104: the owner set budgets for recording with drafts on (25 Oct 2026,
        # #189 sitting 8): 40% of one core visible, about 10% minimised. perf_check
        # has no drafts-on states yet (#104 slice 4), so the figures wait in
        # "pending_budgets", which compare() never reads. When perf_check gains
        # a state, it must move into "budgets" (the test above then needs it).
        path = ROOT / "docs" / "perf-budgets.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        pending = {state: limits for state, limits in data["pending_budgets"].items()
                   if not state.startswith("_")}
        self.assertEqual({"recording-drafts": {"cpu_percent_of_one_core": 40.0},
                          "recording-drafts-minimised": {"cpu_percent_of_one_core": 10.0}},
                         pending)
        for state in pending:
            self.assertNotIn(state, perf_check.STATES, f"{state}: measured, move it to budgets")
            self.assertNotIn(state, data["budgets"], f"{state}: in both sections")


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
                    mock.patch.object(perf_check, "display_watch", return_value=FakeDisplay()), \
                    mock.patch.object(perf_check, "refresh_now", return_value=None), \
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
                    rate, cb, done, blocks=perf_check.synthetic_blocks(rate, seconds=1)),
                key=bytes(range(32)))
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
        # An unlocked Workspace (#111 C4a): the bridge gate refuses unless
        # ws.locked is None, and a bare Mock's attribute is not None.
        ws = mock.Mock(locked=None)
        ws.consent_required.return_value = True
        ws.recording_sources.return_value = consent.MICROPHONE_ONLY    # the perf run's (#108)
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


def drawn_pages(fps=120.0, draws=None, undrawn_ms=None, step_ms=60000.0,
                visibility="visible"):
    """The page's counter as the child reports it (perf_check.page_reading):
    each read is step_ms of page time after the last, with a frame drawn just
    before it. While a recording state is shown the page runs fps animation
    frames a second and the level display draws `draws` times a second (by
    default every frame up to 60 a second, as #294 has it), never undrawn for
    longer than undrawn_ms (by default two draws' time)."""
    clock = {"now": 1000.0, "frames": 0, "draws": 0}
    draws = min(fps, 60.0) if draws is None else draws

    def page(commands):
        state = [c for c in commands if c not in ("probe", "check")][-1]
        clock["now"] += step_ms
        gap = step_ms
        if state in perf_check.DRAWING_STATES:
            clock["frames"] += round(fps * step_ms / 1000)
            clock["draws"] += round(draws * step_ms / 1000)
            gap = undrawn_ms if undrawn_ms is not None else (
                2000 / draws if draws else step_ms)
        return {"visibility": visibility, "frames": clock["frames"], "draws": clock["draws"],
                "undrawn_ms": gap, "now": clock["now"], "ping": clock["now"] - 5}
    return page


class FakeDisplay:
    """The run's display for one_run and parent(), as WindowsDisplay gives it:
    the display's states during each window (by the state being measured), the
    session's state, and whether start() and stop() were called."""

    def __init__(self, states=None, session=None, known=True, kept=True, at_start="on",
                 session_at_start=None):
        self.by_state = states or {}
        self.session_state = session if session is not None else {
            "locked": False, "screensaver": False}
        self.known, self.kept, self.calls, self.measuring = known, kept, [], None
        self.at_start, self.session_at_start = at_start, session_at_start

    def start(self):
        self.calls.append("start")
        return {"kept_awake": self.kept, "power_state_known": self.known}

    def current(self):
        return self.at_start if self.measuring is None else self.states(0, 0)[-1]

    def session(self):
        if self.measuring is None and self.session_at_start is not None:
            return self.session_at_start
        return self.session_state

    def stop(self):
        self.calls.append("stop")

    def states(self, t0, t1):
        return self.by_state.get(self.measuring, ["on"])


class FakeChild:
    """subprocess.Popen for one_run: answers each command from a script, as
    the child process would, and records the commands it was given."""

    def __init__(self, ready, check, page=None, display=None):
        import queue
        self.commands, self.pid, self._out = [], 4242, queue.Queue()
        self._check, self.returncode = check, None
        self._page, self._display = page or drawn_pages(), display
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
        elif command == "probe":
            self._send({"page": self._page(self.commands)})
        else:
            if self._display is not None:
                self._display.measuring = command
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

    def run_child(self, child, leak_minutes=0, held=None, display=None):
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
        display = display or child._display or FakeDisplay()
        return perf_check.one_run(args, 0, 0, leak_minutes, display=display)

    def test_the_leak_check_runs_before_the_recording_states(self):
        child = FakeChild(self.READY, lambda c: {"state": "recording", "seconds": 61.0})
        result = self.run_child(child, leak_minutes=1, held=60.0)
        # The page is read just before and after each visible state's window.
        self.assertEqual(["meetings", "probe", "probe", "settings", "probe", "probe",
                          "minimised", "meetings", "recording", "probe", "probe", "check",
                          "recording-minimised", "check", "quit"], child.commands)
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
        self.assertEqual(["meetings", "probe", "probe", "settings", "probe", "probe",
                          "minimised", "quit"], child.commands)
        self.assertEqual(list(perf_check.RECORDING_STATES), result["not_measured_by_policy"])
        for state in perf_check.IDLE_STATES:
            self.assertIn(state, result)


class DrawnWindowTests(unittest.TestCase):
    """#292: a window measured in a visible state counts only if it was drawn:
    the display on, the session unlocked and the page drawing frames."""

    ON = {"locked": False, "screensaver": False}

    def page(self, now, frames=0, draws=0, undrawn_ms=20.0, ping=None, visibility="visible"):
        return {"visibility": visibility, "frames": frames, "draws": draws,
                "undrawn_ms": undrawn_ms, "now": now, "ping": now - 5 if ping is None else ping}

    def report(self, state="recording", display=("on",), sessions=None, fps=120.0, draws=None,
               refresh_hz=120, **page1):
        draws = min(fps, 60.0) if draws is None else draws
        start = self.page(1000.0)
        end = self.page(301000.0, frames=round(fps * 300), draws=round(draws * 300), **page1)
        return perf_check.drawing_report(state, None if display is None else list(display),
                                         sessions or [self.ON, self.ON], start, end, refresh_hz)

    def test_a_drawn_window_counts(self):
        got = self.report()
        self.assertEqual({"drawn": True, "problems": [], "display": ["on"],
                          "page_visibility": ["visible", "visible"], "page_frames_per_s": 120.0,
                          "meter_draws_per_s": 60.0, "min_meter_draws_per_s": 30.0,
                          "longest_undrawn_s": 0.0}, got)
        # A view that animates nothing still drew a frame in the window, and
        # has no level display to count.
        got = self.report("meetings", fps=0)
        self.assertTrue(got["drawn"])
        self.assertIsNone(got["min_meter_draws_per_s"])
        self.assertIsNone(got["longest_undrawn_s"])
        # A dimmed display still draws every frame.
        self.assertTrue(self.report(display=("on", "dimmed"))["drawn"])

    def test_a_window_with_the_display_off_is_not_drawn(self):
        for display in (("off",), ("on", "off"), ("on", "off", "on"), ("unknown",)):
            with self.subTest(display=display):
                got = self.report(display=display)
                self.assertFalse(got["drawn"])
                self.assertIn(f"the display was {[s for s in display if s != 'on'][0]}",
                              got["problems"])
        got = self.report(display=None)
        self.assertEqual(["the display's power state was not known"], got["problems"])

    def test_a_locked_session_or_screen_saver_is_not_drawn(self):
        cases = (({"locked": True, "screensaver": False}, "the session was locked"),
                 ({"locked": False, "screensaver": True}, "the screen saver was running"),
                 (None, "the session's state could not be read"),
                 ({}, "the session was locked"))
        for session, problem in cases:
            with self.subTest(session=session):
                got = self.report(sessions=[self.ON, session])
                self.assertFalse(got["drawn"])
                self.assertIn(problem, got["problems"])

    def test_a_page_that_was_hidden_or_drew_nothing_is_not_drawn(self):
        got = self.report(visibility="hidden")
        self.assertIn("the page was visible, then hidden", got["problems"])
        got = self.report("meetings", fps=0, ping=900.0)       # no frame since the start
        self.assertEqual(["the page drew no frame"], got["problems"])
        got = perf_check.drawing_report("settings", ["on"], [self.ON, self.ON], None,
                                        self.page(2000.0))
        self.assertEqual(["the page did not report its frames"], got["problems"])

    def test_the_level_display_must_draw_while_recording(self):
        got = self.report(fps=5)
        self.assertFalse(got["drawn"])
        self.assertEqual(["the level display drew 5.0 times a second (at least 30.0 "
                          "expected)"], got["problems"])
        self.assertTrue(self.report(fps=30)["drawn"])
        self.assertTrue(self.report(fps=60)["drawn"])
        # Only the recording state has the level display drawing.
        self.assertEqual(("recording",), perf_check.DRAWING_STATES)
        self.assertEqual(("meetings", "settings", "recording"), perf_check.VISIBLE_STATES)
        self.assertTrue(set(perf_check.VISIBLE_STATES) <= set(perf_check.STATES))

    def test_display_states_during_a_window(self):
        events = [(0.0, "on"), (10.0, "off"), (20.0, "on")]
        self.assertEqual(["on"], perf_check.display_states(events, 1.0, 9.0))
        self.assertEqual(["on", "off"], perf_check.display_states(events, 5.0, 15.0))
        self.assertEqual(["off", "on"], perf_check.display_states(events, 12.0, 25.0))
        self.assertEqual(["on", "off"], perf_check.display_states(events, 1.0, 30.0))
        self.assertIsNone(perf_check.display_states(events, -1.0, 5.0))   # nothing known
        self.assertIsNone(perf_check.display_states([], 0.0, 5.0))

    def test_display_events_from_windows_values(self):
        now = [0.0]
        events = perf_check.DisplayEvents(clock=lambda: now[0])
        self.assertFalse(events.known.is_set())
        self.assertIsNone(events.current())
        for at, value in ((0.0, 1), (5.0, 2), (6.0, 0), (9.0, 7)):
            now[0] = at
            events.record(value)
        self.assertTrue(events.known.is_set())
        self.assertEqual(["on", "dimmed", "off", "unknown"], events.states(1.0, 10.0))
        self.assertEqual(["on"], events.states(1.0, 4.0))
        self.assertEqual("unknown", events.current())        # the last one recorded

    def test_the_page_reading_takes_only_counts(self):
        good = {"visibility": "visible", "frames": 3, "draws": 2, "undrawn_ms": 16.7,
                "now": 10.5, "ping": 9.0}
        self.assertEqual(good, perf_check.page_reading(json.dumps(good)))
        self.assertEqual(good, perf_check.page_reading(dict(good, extra="x")))
        for bad in (None, "", "null", "not json", "[1]", json.dumps(dict(good, frames=-1)),
                    json.dumps(dict(good, now="10")), json.dumps(dict(good, ping=True)),
                    json.dumps(dict(good, draws=None)), json.dumps(dict(good, undrawn_ms=-1)),
                    json.dumps({k: v for k, v in good.items() if k != "frames"}),
                    json.dumps({k: v for k, v in good.items() if k != "draws"}),
                    json.dumps(dict(good, visibility=None))):
            with self.subTest(bad=bad):
                self.assertIsNone(perf_check.page_reading(bad))

    def test_the_page_counter_only_wraps_the_pages_own_frames(self):
        js = perf_check.PAGE_COUNTER_JS
        # It wraps requestAnimationFrame and the 2D context's clearRect, and
        # asks for one frame per read, no loop.
        self.assertIn("window.requestAnimationFrame = (cb) => raf(", js)
        self.assertIn("ctx.clearRect = function", js)
        self.assertIn('this.canvas.id === "rec-canvas"', js)
        self.assertEqual(1, js.count("raf(() =>"))
        self.assertNotIn("setInterval", js)
        self.assertNotIn("setTimeout", js)
        # The level display's canvas is the one the app draws its meter on.
        app = (ROOT / "workspace" / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn('$("rec-canvas")', app)
        self.assertIn("clearRect(", app)

    def test_the_expected_draws_follow_the_display(self):
        # #292 review: half of what the meter draws on this display. It draws
        # every frame up to about 60 a second (#294; the owner's decision of
        # 23 Oct 2026, #216, after 30 in #315), so half of min(60, refresh
        # rate). This follows the meter's rate: it is not a looser bar.
        for hz, least in ((60, 30.0), (120, 30.0), (144, 30.0), (165, 30.0), (240, 30.0),
                          (48, 24.0), (30, 15.0), (24, 12.0), (None, 30.0), (0, 30.0),
                          (1, 30.0)):
            with self.subTest(hz=hz):
                self.assertEqual(least, perf_check.min_meter_draws(hz))
        # The meter at its rate counts: 60 a second on a 60 or 120 Hz display,
        # 72 at 144 Hz (every 2nd frame), 55 at 165 Hz (every 3rd), 24 on a
        # 24 Hz one; and half its rate on a 120 Hz display still counts.
        for fps, draws, hz in ((60, 60, 60), (120, 60, 120), (144, 72, 144),
                               (165, 55, 165), (24, 24, 24), (120, 30, 120)):
            with self.subTest(fps=fps, draws=draws):
                self.assertTrue(self.report(fps=fps, refresh_hz=hz, draws=draws)["drawn"])
        # On a 30 Hz display 15 draws a second (half its rate) still counts.
        self.assertTrue(self.report(fps=30, refresh_hz=30, draws=15)["drawn"])
        got = self.report(fps=120, draws=29)
        self.assertEqual(["the level display drew 29.0 times a second (at least 30.0 "
                          "expected)"], got["problems"])
        got = self.report(fps=24, refresh_hz=24, draws=11)
        self.assertEqual(["the level display drew 11.0 times a second (at least 12.0 "
                          "expected)"], got["problems"])

    def test_the_expected_rate_is_the_meters_own(self):
        # The tool's meter rate is the page's (METER_HZ in workspace/ui/app.js),
        # so a change to one without the other fails here. About 60 a second is
        # the owner's decision of 23 Oct 2026 (#216).
        app = (ROOT / "workspace" / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertRegex(app, rf"(?m)^const METER_HZ = {perf_check.METER_MAX_HZ};$")
        self.assertEqual(60, perf_check.METER_MAX_HZ)

    def test_the_meter_draws_are_counted_not_the_pages_frames(self):
        # #292 review: the page can run every frame while the meter draws none
        # (or only every second frame, #294): only the meter's draws count.
        got = self.report(fps=120, draws=0)
        self.assertFalse(got["drawn"])
        self.assertEqual(120.0, got["page_frames_per_s"])
        self.assertEqual(0.0, got["meter_draws_per_s"])
        self.assertTrue(self.report(fps=120, draws=60)["drawn"])

    def test_a_long_undrawn_stretch_is_not_drawn(self):
        # #292 review: a window drawn well on average can still have gone
        # undrawn for a long stretch (the display off for a minute of five).
        got = self.report(undrawn_ms=60000.0)
        self.assertEqual([f"the level display was not drawn for 60.0 s at a stretch (at most "
                          f"{perf_check.MAX_UNDRAWN_S} s allowed)"], got["problems"])
        self.assertTrue(self.report(undrawn_ms=perf_check.MAX_UNDRAWN_S * 1000)["drawn"])
        # A view without the level display is not held to it.
        self.assertTrue(self.report("meetings", fps=0, undrawn_ms=300000.0)["drawn"])

    def test_the_help_says_how_the_drawing_is_judged(self):
        text = " ".join(perf_check.HELP.split())
        self.assertIn("at least 30 a second on a 60 Hz or faster display", text)
        self.assertIn("up to about 60 a second", text)
        self.assertNotIn("about 30 a second", text)
        self.assertIn("no stretch longer than 5 s undrawn", text)
        self.assertNotIn("20 frames", text)
        self.assertEqual(5.0, perf_check.MAX_UNDRAWN_S)
        self.assertEqual(60, perf_check.METER_MAX_HZ)

    def test_the_docs_say_a_policy_lock_still_stops_the_run(self):
        # #292 review: keeping the display on does not stop a screen saver or
        # an inactivity lock set by policy; an unattended run must allow for it.
        releasing = " ".join((ROOT / "docs" / "RELEASING.md").read_text(
            encoding="utf-8").split())
        for text in (" ".join(perf_check.HELP.split()), releasing):
            self.assertIn("does not stop a screen saver or an inactivity lock set by Intune "
                          "or other policy", text)
            self.assertIn("an unattended release run must account for that", text.lower())


@unittest.skipUnless(shutil.which("node"), "Node.js not installed")
class PageCounterScriptTests(unittest.TestCase):
    """#292 review: PAGE_COUNTER_JS run in Node.js against a stand-in page,
    with the level meter drawing on every second frame as #294 has it."""

    SCRIPT = r"""
let now = 0; const queue = [];
globalThis.performance = { now: () => now };
globalThis.document = { visibilityState: "visible" };
globalThis.window = globalThis;
window.requestAnimationFrame = (cb) => { queue.push(cb); return queue.length; };
class CanvasRenderingContext2D { clearRect() {} }
globalThis.CanvasRenderingContext2D = CanvasRenderingContext2D;
const meter = new CanvasRenderingContext2D(); meter.canvas = { id: "rec-canvas" };
const other = new CanvasRenderingContext2D(); other.canvas = { id: "other" };
const installed = eval(process.argv[1]);
const frame = (ms) => { now += ms; const due = queue.splice(0); due.forEach((cb) => cb(now)); };
const read = () => JSON.parse(eval(process.argv[2]));
let n = 0, paused = false;
function draw() {                       // the meter: every second callback draws
  requestAnimationFrame(draw);
  n += 1;
  if (paused || n % 2) return;
  meter.clearRect(0, 0, 1, 1); meter.clearRect(0, 0, 1, 1);   // twice in one frame: one draw
  other.clearRect(0, 0, 1, 1);                               // another canvas: not counted
}
const first = read();
requestAnimationFrame(draw);
requestAnimationFrame(() => {});        // a second callback in the same frames
for (let i = 0; i < 120; i++) frame(1000 / 120);
const second = read();
paused = true;                             // the meter stops drawing for a while
for (let i = 0; i < 10; i++) frame(100);
paused = false; n = 1; frame(1);           // and draws once more
const third = read();
console.log(JSON.stringify({ installed, again: eval(process.argv[1]), first, second, third }));
"""

    def test_it_counts_the_meters_draws_and_its_longest_gap(self):
        out = subprocess.run(
            [shutil.which("node"), "-e", self.SCRIPT, "--", perf_check.PAGE_COUNTER_JS,
             perf_check.PAGE_READ_JS],
            capture_output=True, text=True, timeout=60, check=True)
        got = json.loads(out.stdout)
        self.assertIs(True, got["installed"])
        self.assertIs(True, got["again"])            # a second install changes nothing
        second, third = got["second"], got["third"]
        self.assertEqual(120, second["frames"] - got["first"]["frames"])   # frames, not callbacks
        self.assertEqual(60, second["draws"] - got["first"]["draws"])      # every second frame
        self.assertLess(second["undrawn_ms"], 20)
        self.assertEqual(61, third["draws"])
        self.assertGreaterEqual(third["undrawn_ms"], 1000)                 # the stopped stretch
        for reading in (got["first"], second, third):
            self.assertIsNotNone(perf_check.page_reading(json.dumps(reading)))


class DrawnRunTests(unittest.TestCase):
    """#292: one_run and parent() with a scripted child and a fake display (no
    window is opened). A visible state measured while not drawn stops the run."""

    READY = OneRunTests.READY

    def run_child(self, child, display):
        return OneRunTests.run_child(self, child, held=60.0, display=display)

    def test_every_visible_window_records_how_it_was_drawn(self):
        display = FakeDisplay()
        child = FakeChild(self.READY, lambda c: {"state": "recording", "seconds": 61.0},
                          display=display)
        result = self.run_child(child, display)
        for state in perf_check.VISIBLE_STATES:
            self.assertTrue(result[state]["drawing"]["drawn"], state)
        self.assertEqual(120.0, result["recording"]["drawing"]["page_frames_per_s"])
        self.assertEqual(60.0, result["recording"]["drawing"]["meter_draws_per_s"])
        for state in ("minimised", "recording-minimised"):
            self.assertNotIn("drawing", result[state])

    def test_a_window_with_the_display_off_is_unmeasurable(self):
        display = FakeDisplay(states={"recording": ["on", "off"]})
        child = FakeChild(self.READY, lambda c: {"state": "recording", "seconds": 61.0},
                          display=display)
        with self.assertRaises(perf_check.Unmeasurable) as caught:
            self.run_child(child, display)
        self.assertIn("recording was not drawn on screen (the display was off)",
                      str(caught.exception))
        self.assertNotIn("recording-minimised", child.commands)

    def test_a_locked_session_is_unmeasurable(self):
        display = FakeDisplay(session={"locked": True, "screensaver": False})
        child = FakeChild(self.READY, lambda c: None, display=display)
        with self.assertRaises(perf_check.Unmeasurable) as caught:
            self.run_child(child, display)
        self.assertIn("meetings was not drawn on screen (the session was locked)",
                      str(caught.exception))

    def test_a_level_display_not_drawing_is_unmeasurable(self):
        display = FakeDisplay()
        child = FakeChild(self.READY, lambda c: {"state": "recording", "seconds": 61.0},
                          page=drawn_pages(fps=2.0), display=display)
        with self.assertRaises(perf_check.Unmeasurable) as caught:
            self.run_child(child, display)
        self.assertIn("the level display drew 2.0 times a second", str(caught.exception))

    def test_a_page_drawing_frames_without_the_meter_is_unmeasurable(self):
        # #292 review: the page's own frames are not the meter's draws. A page
        # running every frame while the meter draws nothing is not drawn.
        display = FakeDisplay()
        child = FakeChild(self.READY, lambda c: {"state": "recording", "seconds": 61.0},
                          page=drawn_pages(fps=120.0, draws=0), display=display)
        with self.assertRaises(perf_check.Unmeasurable) as caught:
            self.run_child(child, display)
        self.assertIn("recording was not drawn on screen (the level display drew 0.0 times",
                      str(caught.exception))

    def test_without_a_display_nothing_counts_as_drawn(self):
        from types import SimpleNamespace
        child = FakeChild(self.READY, lambda c: None)
        with mock.patch.object(perf_check.subprocess, "Popen", return_value=child), \
                mock.patch.object(perf_check, "Job"), \
                mock.patch.object(perf_check, "measure", return_value={"private_mb": 1}), \
                mock.patch.object(perf_check.time, "sleep"), \
                mock.patch("builtins.print"), \
                self.assertRaises(perf_check.Unmeasurable) as caught:
            perf_check.one_run(SimpleNamespace(app=None, zip=None), 0, 0)
        self.assertIn("the display's power state was not known", str(caught.exception))

    def run_parent(self, display, page=None, refresh=(120, 120)):
        """A release-shaped parent() run against the scripted child and display.
        refresh: the display's refresh rate read with the machine, then again
        once the display is kept on."""
        import tempfile
        from types import SimpleNamespace
        child = FakeChild(self.READY, lambda c: {"state": "recording", "seconds": 61.0},
                          page=page, display=display)
        figures = {"cpu_percent_of_one_core": 1.0, "private_mb": 700}

        def refresh_now():
            display.calls.append("refresh")
            return refresh[1]
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "perf.json"
            args = SimpleNamespace(quick=False, settle=0, window=0, runs=1, leak_minutes=0,
                                   report=str(report), app=None, zip=None)
            env = {"machine": dict(MachineReportTests.FULL, display_refresh_hz=refresh[0])}
            with mock.patch.object(perf_check, "environment", return_value=env), \
                    mock.patch.object(perf_check, "display_watch", create=True,
                                      return_value=display), \
                    mock.patch.object(perf_check, "refresh_now", create=True,
                                      side_effect=refresh_now), \
                    mock.patch.object(perf_check, "store_step",      # #111 C7, no window
                                      return_value=dict(STORE)), \
                    mock.patch.object(perf_check.subprocess, "Popen", return_value=child), \
                    mock.patch.object(perf_check, "Job"), \
                    mock.patch.object(perf_check, "measure",
                                      side_effect=lambda *a: dict(figures)), \
                    mock.patch.object(perf_check, "check_held", return_value=60.0), \
                    mock.patch.object(perf_check.time, "sleep"), \
                    mock.patch.object(perf_check, "BUDGETS", Path(tmp) / "none.json"), \
                    mock.patch("builtins.print"):
                rc = perf_check.parent(args)
            return rc, json.loads(report.read_text(encoding="utf-8"))

    def test_a_release_run_on_a_display_that_turned_off_fails(self):
        # The regression (#292): overnight the display turned itself off and
        # the visible states were measured without their drawing cost, yet the
        # run passed. Now it is "could not measure" (exit 2), naming why.
        display = FakeDisplay(states={"recording": ["on", "off"]})
        rc, report = self.run_parent(display)
        self.assertEqual(2, rc)
        self.assertIn("recording was not drawn on screen (the display was off)",
                      report.get("error", ""))
        self.assertEqual(["start", "refresh", "stop"], display.calls)   # let go again

    def test_a_release_run_with_the_window_drawn_passes_and_says_so(self):
        display = FakeDisplay()
        rc, report = self.run_parent(display)
        self.assertEqual(0, rc)
        self.assertEqual({"kept_awake": True, "power_state_known": True, "at_start": "on",
                          "refresh_hz": 120}, report["display"])
        self.assertEqual(["start", "refresh", "stop"], display.calls)
        self.assertEqual([], perf_check.evidence.perf_not_drawn(report))

    def test_a_display_already_off_stops_before_measuring(self):
        # #292 review: known to be off at the start is as bad as not known.
        for at_start in ("off", "unknown", None):
            with self.subTest(at_start=at_start):
                display = FakeDisplay(at_start=at_start)
                rc, report = self.run_parent(display)
                self.assertEqual(2, rc)
                self.assertIn(f"the display was {at_start or 'in an unknown state'} as the "
                              "run started", report.get("error", ""))
                self.assertEqual([], report["each_run"])
                self.assertEqual(["start", "stop"], display.calls)
        # Dimmed still draws every frame.
        rc, _ = self.run_parent(FakeDisplay(at_start="dimmed"))
        self.assertEqual(0, rc)

    def test_a_session_locked_at_the_start_stops_before_measuring(self):
        for session, problem in (({"locked": True, "screensaver": False}, "the session was locked"),
                                 ({"locked": False, "screensaver": True},
                                  "the screen saver was running")):
            with self.subTest(session=session):
                display = FakeDisplay(session_at_start=session)
                rc, report = self.run_parent(display)
                self.assertEqual(2, rc)
                self.assertIn(f"{problem} as the run started", report.get("error", ""))
                self.assertEqual([], report["each_run"])

    def test_the_refresh_rate_is_read_once_the_display_is_kept_on(self):
        # #292 review: dynamic refresh can lower the rate while idle, so it is
        # read again after the display is kept on, and both kept if they differ.
        display = FakeDisplay()
        rc, report = self.run_parent(display, page=drawn_pages(fps=60.0), refresh=(48, 60))
        self.assertEqual(0, rc)
        self.assertEqual(["start", "refresh", "stop"], display.calls)
        self.assertEqual(60, report["display"].get("refresh_hz"))
        self.assertEqual(48, report["display"].get("refresh_hz_before_kept_on"))
        self.assertEqual(60, report["machine"]["display_refresh_hz"])
        # Not read again: the rate read with the machine stays, and is used.
        rc, report = self.run_parent(FakeDisplay(), refresh=(120, None))
        self.assertEqual(0, rc)
        self.assertIsNone(report["display"].get("refresh_hz", "missing"))
        self.assertEqual(120, report["display"].get("refresh_hz_before_kept_on"))
        self.assertEqual(120, report["machine"]["display_refresh_hz"])

    def test_the_expected_draws_use_the_rate_read_once_kept_on(self):
        # #292 review: the bar follows the display, not a fixed 20 a second.
        rc, report = self.run_parent(FakeDisplay(), page=drawn_pages(fps=120.0, draws=25.0))
        self.assertEqual(2, rc)
        self.assertIn("the level display drew 25.0 times a second (at least 30.0 expected)",
                      report.get("error", ""))
        # On a 48 Hz display (read once kept on) 26 draws a second is enough.
        rc, report = self.run_parent(FakeDisplay(), page=drawn_pages(fps=48.0, draws=26.0),
                                     refresh=(120, 48))
        self.assertEqual(0, rc)
        self.assertEqual(24.0, report["each_run"][0]["recording"]["drawing"].get(
            "min_meter_draws_per_s"))

    def test_a_long_undrawn_stretch_in_a_run_is_unmeasurable(self):
        rc, report = self.run_parent(FakeDisplay(), page=drawn_pages(undrawn_ms=90000.0))
        self.assertEqual(2, rc)
        self.assertIn("the level display was not drawn for 90.0 s at a stretch",
                      report.get("error", ""))

    def test_the_refresh_rate_now_is_a_rate_or_none(self):
        for value, expected in ((75, 75), (1, None), (0, None), (None, None)):
            with self.subTest(value=value), mock.patch.object(perf_check, "WindowsProbe") as p:
                p.return_value.display_refresh_hz.return_value = value
                self.assertEqual(expected, perf_check.refresh_now())
        with mock.patch.object(perf_check, "WindowsProbe", side_effect=OSError("no display")):
            self.assertIsNone(perf_check.refresh_now())

    def test_stop_lets_the_display_go_even_if_unwatching_fails(self):
        # #292 review: the execution state is released first, on its own, so
        # a failure to stop watching cannot leave the display kept on.
        from types import SimpleNamespace
        k32 = mock.Mock()
        powrprof = mock.Mock()
        powrprof.PowerSettingUnregisterNotification.side_effect = OSError("refused")
        fake_ctypes = SimpleNamespace(WinDLL=lambda name: k32, c_uint32=int, c_void_p=int)
        display = perf_check.WindowsDisplay()
        display._handle, display._keep = object(), (None, None, None, powrprof)
        with mock.patch.object(perf_check, "_win", return_value=(fake_ctypes, None)):
            display.stop()
        k32.SetThreadExecutionState.assert_called_once_with(perf_check.ES_CONTINUOUS)
        powrprof.PowerSettingUnregisterNotification.assert_called_once()
        # And the other way round: a failed release still stops watching.
        k32.SetThreadExecutionState.side_effect = OSError("refused")
        powrprof.PowerSettingUnregisterNotification.side_effect = None
        with mock.patch.object(perf_check, "_win", return_value=(fake_ctypes, None)):
            display.stop()
        self.assertEqual(2, powrprof.PowerSettingUnregisterNotification.call_count)
        self.assertIsNone(display._handle)

    def test_a_display_whose_state_cannot_be_read_stops_before_measuring(self):
        display = FakeDisplay(known=False)
        rc, report = self.run_parent(display)
        self.assertEqual(2, rc)
        self.assertIn("the display's power state could not be read", report["error"])
        self.assertEqual([], report["each_run"])
        self.assertEqual(["start", "stop"], display.calls)       # nothing read after it

    def test_a_display_that_could_not_be_kept_on_is_recorded(self):
        rc, report = self.run_parent(FakeDisplay(kept=False))
        self.assertEqual(0, rc)              # each window was still shown drawn
        self.assertIs(False, report["display"]["kept_awake"])

    @unittest.skipUnless(sys.platform == "win32", "asks this Windows machine")
    def test_the_windows_display_answers_without_failing(self):
        # Only the shape is checked: what the display is doing here is not.
        display = perf_check.WindowsDisplay()
        try:
            started = display.start()
            self.assertEqual({"kept_awake", "power_state_known"}, set(started))
            self.assertTrue(all(isinstance(v, bool) for v in started.values()))
            session = display.session()
            self.assertTrue(session is None or (set(session) == {"locked", "screensaver"}
                                                and all(isinstance(v, bool)
                                                        for v in session.values())))
            for _, state in display.events:
                self.assertIn(state, (*perf_check.DISPLAY_STATES.values(), "unknown"))
        finally:
            display.stop()
        hz = perf_check.WindowsProbe().display_refresh_hz()
        self.assertTrue(hz is None or (isinstance(hz, int) and hz >= 0))

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
                    mock.patch.object(perf_check, "display_watch", return_value=FakeDisplay()), \
                    mock.patch.object(perf_check, "refresh_now", return_value=None), \
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
        self.refresh = 120
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

    def display_refresh_hz(self):
        return self.refresh


class MachineSpecTests(unittest.TestCase):
    def test_the_specification_is_recorded_from_its_readers(self):
        spec = perf_check.machine_spec(FakeProbe())
        self.assertEqual({
            "cpu": "Synthetic CPU 8000X", "cores": 8, "logical_processors": 16,
            "memory_gb": 16.0, "windows": "Windows 11 24H2", "os_build": "10.0.26100.4061",
            "power_plan": "Balanced", "on_mains_power": True, "battery": True,
            "gpus": [{"name": "Synthetic GPU A", "driver": None},
                     {"name": "Synthetic GPU B", "driver": "31.0.1"}],
            "display_refresh_hz": 120, "webview2": "140.0.1.2"}, spec)
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
                          logical=None, refresh=None)
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

    def test_the_display_refresh_rate(self):
        # 0 and 1 mean "the hardware's default" in DEVMODE: not a rate (#292).
        for value, expected in ((60, 60), (144, 144), (0, None), (1, None), (None, None),
                                (True, None), ("120", None)):
            with self.subTest(value=value):
                spec = perf_check.machine_spec(FakeProbe(refresh=value))
                self.assertEqual(expected, spec["display_refresh_hz"])
                self.assertEqual([] if expected else ["display_refresh_hz"],
                                 perf_check.evidence.machine_problems(spec))

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
            "on_mains_power": True, "battery": True, "gpus": [], "display_refresh_hz": 120,
            "webview2": "140.0.1.2"}

    def run_parent(self, machine, zip_path=None):
        import tempfile
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "perf.json"
            args = SimpleNamespace(quick=True, settle=0, window=0, runs=1, leak_minutes=0,
                                   report=str(report), app=None, zip=zip_path)
            env = {"python": "3.14.0", "machine": machine}
            with mock.patch.object(perf_check, "environment", return_value=env), \
                    mock.patch.object(perf_check, "display_watch", return_value=FakeDisplay()), \
                    mock.patch.object(perf_check, "refresh_now", return_value=None), \
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


STORE = {name: 0.01 for name in perf_check.STORE_FIGURES}


class StoreStepTests(unittest.TestCase):
    """#111 C7: the sealed store measured once per release run, with no window,
    before the windowed runs (the C2b-2 design's section 4.8). Measured, no
    budget yet."""

    def run_parent(self, store, quick=False):
        import tempfile
        from types import SimpleNamespace
        calls = []

        def step(args):
            calls.append("store")
            if isinstance(store, Exception):
                raise store
            return dict(store)

        def run(*a, **k):
            calls.append("run")
            return {"startup": {"startup_s": 1.0, "workspace_s": 0.2}}
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "perf.json"
            args = SimpleNamespace(quick=quick, settle=0, window=0, runs=2, leak_minutes=0,
                                   report=str(report), app=None, zip=None, compare=None)
            with mock.patch.object(perf_check, "environment", return_value={}), \
                    mock.patch.object(perf_check, "display_watch", return_value=FakeDisplay()), \
                    mock.patch.object(perf_check, "refresh_now", return_value=None), \
                    mock.patch.object(perf_check, "store_step", side_effect=step), \
                    mock.patch.object(perf_check, "one_run", side_effect=run), \
                    mock.patch.object(perf_check, "BUDGETS", Path(tmp) / "none.json"), \
                    mock.patch("builtins.print"):
                rc = perf_check.parent(args)
            return rc, calls, json.loads(report.read_text(encoding="utf-8"))

    def test_a_release_run_measures_the_store_once_before_the_windows(self):
        rc, calls, report = self.run_parent(STORE)
        self.assertEqual((0, ["store", "run", "run"]), (rc, calls))
        self.assertEqual(STORE, report["store"])
        # workspace_s is a startup median, with no budget and none proposed.
        self.assertEqual(0.2, report["median"]["startup"]["workspace_s"])
        self.assertNotIn("workspace_s", report["proposed_budgets_plus_20_percent"]["startup"])

    def test_quick_leaves_it_out(self):
        rc, calls, report = self.run_parent(STORE, quick=True)
        self.assertEqual(["run"], calls[:1])
        self.assertNotIn("store", calls)
        self.assertNotIn("store", report)

    def test_a_store_step_that_misses_a_figure_stops_the_run(self):
        for store in ({k: v for k, v in STORE.items() if k != "open_500_meetings_s"},
                      dict(STORE, page_5000_passages_s=None),
                      RuntimeError("the store step failed (exit 1)")):
            with self.subTest(store=store):
                rc, calls, report = self.run_parent(store)
                self.assertEqual((2, ["store"]), (rc, calls))
                self.assertTrue(report["error"])

    def test_store_problems_names_what_is_missing(self):
        self.assertEqual([], perf_check.store_problems(STORE))
        self.assertEqual(["the store step did not run"], perf_check.store_problems(None))
        self.assertEqual(["database_5000_passages_mb"], perf_check.store_problems(
            dict(STORE, database_5000_passages_mb="12")))

    def test_the_store_child_is_this_tool_without_a_window(self):
        from types import SimpleNamespace
        args = SimpleNamespace(app=None, zip=None)
        command, _ = perf_check.child_command(args, Path("w"))
        seen = {}

        def fake_run(cmd, **kw):
            seen["command"] = cmd
            figures = perf_check.PREFIX + json.dumps(STORE) + "\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=figures, stderr="")
        with mock.patch.object(perf_check.subprocess, "run", side_effect=fake_run):
            self.assertEqual(STORE, perf_check.store_step(args))
        self.assertIn("--store-child", seen["command"])
        self.assertNotIn("--child", seen["command"])

    def test_workspace_s_reaches_the_startup_figures(self):
        ready = dict(OneRunTests.READY, workspace_s=0.31)
        child = FakeChild(ready, lambda c: {"state": "recording", "seconds": 61.0})
        result = OneRunTests.run_child(self, child, held=60.0)
        self.assertEqual(0.31, result["startup"]["workspace_s"])


@unittest.skipUnless(all(__import__("importlib").util.find_spec(m) for m in ("numpy", "av")),
                     "numpy/av not installed")
class MeasureStoreTests(unittest.TestCase):
    def test_every_figure_is_measured(self):
        # Small numbers and the test key protector: the shape, not the speed.
        import tempfile

        import keyprotect
        with tempfile.TemporaryDirectory() as tmp:
            out = perf_check.measure_store(tmp, protector=keyprotect.TestProtector(),
                                           meetings=2, passages=30, opens=2, reads=2)
        self.assertEqual([], perf_check.store_problems(out))
        self.assertEqual((2, 30), (out["meetings"], out["passages"]))
        self.assertGreater(out["database_5000_passages_mb"], 0)


class CompareTests(unittest.TestCase):
    """--compare: an earlier release's report, a local file only."""

    MACHINE = MachineReportTests.FULL

    def report(self, machine=None, **figures):
        return {"machine": dict(self.MACHINE, **(machine or {})),
                "environment": {"app": {"version": "0.5.0-preview.3"}},
                "median": {"startup": {"startup_s": figures.get("startup_s", 2.0)},
                           "meetings": {"cpu_percent_of_one_core": 1.0, "private_mb": 700}},
                **({"store": dict(STORE)} if figures.get("store") else {})}

    def test_figures_in_both_are_set_side_by_side(self):
        now = self.report(startup_s=2.2, store=True)
        out = perf_check.compare_reports(now, self.report())
        self.assertEqual([], out["machines_differ_in"])
        rows = {(r["state"], r["metric"]): r for r in out["compared"]}
        self.assertEqual({("startup", "startup_s"), ("meetings", "cpu_percent_of_one_core"),
                          ("meetings", "private_mb")}, set(rows))
        self.assertEqual(10.0, rows[("startup", "startup_s")]["change_percent"])
        self.assertEqual(len(STORE), out["only_now"])        # preview.3 has no store step
        self.assertEqual({"version": "0.5.0-preview.3"}, out["earlier_app"])

    def test_other_machines_are_not_comparable(self):
        for change in ({"power_plan": "High performance"}, {"memory_gb": 15.6},
                       {"on_mains_power": False}, {"cpu": None}):
            with self.subTest(change=change):
                out = perf_check.compare_reports(self.report(change), self.report())
                self.assertEqual(list(change), out["machines_differ_in"])
                self.assertTrue(out["compared"])
                self.assertTrue(all(r["change_percent"] == perf_check.NOT_COMPARABLE
                                    for r in out["compared"]))
                self.assertEqual(self.MACHINE, out["machine_earlier"])

    def test_a_bad_file_stops_before_the_run(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "earlier.json"
            bad.write_text("[1, 2]", encoding="utf-8")
            for path in (bad, Path(tmp) / "missing.json"):
                argv = ["perf_check.py", str(Path(tmp) / "perf.json"), "--compare", str(path)]
                never = mock.Mock(side_effect=AssertionError("a run started"))
                with self.subTest(path=path.name), mock.patch.object(sys, "argv", argv), \
                        mock.patch("sys.stderr"), mock.patch.object(perf_check, "parent", never), \
                        self.assertRaises(SystemExit):
                    perf_check.main()
                never.assert_not_called()

    def test_the_comparison_reads_only_the_named_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence-perf.json"
            path.write_text(json.dumps(self.report()), encoding="utf-8")
            with mock.patch("urllib.request.urlopen",
                            side_effect=AssertionError("fetched")):
                self.assertEqual(self.report(), perf_check.earlier_report(path))


if __name__ == "__main__":
    unittest.main()
