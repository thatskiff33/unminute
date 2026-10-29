# tests/test_ui_idle_check.py
# The ratchet logic of tools/ui_idle_check.py (docs/RELEASE_STANDARD.md,
# section 3). The check itself runs the page in headless Edge, in the CI
# workflow's "UI idle (headless Edge)" job (.github/workflows/ci.yml).

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import design_check  # noqa: E402
import ui_idle_check  # noqa: E402

METRICS = {"bridge_calls_per_minute", "list_rebuilds_per_minute", "timeouts_per_minute",
           "pending_animation_frames", "live_intervals", "fastest_interval_ms",
           "live_region_changes_per_minute"}


class RatchetTests(unittest.TestCase):
    def test_maxima_and_the_interval_floor(self):
        ratchets = {"hidden": {"bridge_calls_per_minute": 2, "fastest_interval_ms": 30000}}
        quiet = {"hidden": {"bridge_calls_per_minute": 2, "fastest_interval_ms": 60000}}
        self.assertEqual([], ui_idle_check.compare(quiet, ratchets))
        busy = {"hidden": {"bridge_calls_per_minute": 120, "fastest_interval_ms": 2000}}
        self.assertEqual([("hidden", "bridge_calls_per_minute", 120, 2),
                          ("hidden", "fastest_interval_ms", 2000, 30000)],
                         ui_idle_check.compare(busy, ratchets))

    def test_no_interval_at_all_passes_the_floor(self):
        self.assertEqual([], ui_idle_check.compare({"hidden": {"fastest_interval_ms": None}},
                                                   {"hidden": {"fastest_interval_ms": 30000}}))

    def test_a_figure_at_its_ratchet_passes(self):
        self.assertEqual([], ui_idle_check.compare(
            {"visible": {"bridge_calls_per_minute": 120, "fastest_interval_ms": 2000}},
            {"visible": {"bridge_calls_per_minute": 120, "fastest_interval_ms": 2000}}))

    def test_a_metric_the_page_did_not_report_fails(self):
        # A renamed or missing figure must not pass silently.
        self.assertEqual([("visible", "list_rebuilds_per_minute", None, 30)],
                         ui_idle_check.compare({"visible": {}},
                                               {"visible": {"list_rebuilds_per_minute": 30}}))

    RUNNING = {"bridge_calls_per_minute": 14, "meeting_rows": 3, "page_errors": [],
               "calls_per_minute": {"status": 6, "recording_status": 6, "meetings": 1,
                                    "reminders": 1},
               "redrawn_after_change": True}
    HIDDEN = {"bridge_calls_per_minute": 0, "page_errors": [],
              "catch_up_calls": ["status", "meetings", "reminders", "recording_status"]}

    def test_a_page_that_threw_or_is_not_running_fails(self):
        running, hidden = self.RUNNING, self.HIDDEN
        self.assertEqual([], ui_idle_check.sanity({"visible": running, "hidden": hidden}))
        threw = dict(hidden, page_errors=["TypeError: x is undefined"])
        self.assertEqual(1, len(ui_idle_check.sanity({"visible": running, "hidden": threw})))
        silent = {"bridge_calls_per_minute": 0, "meeting_rows": 0, "page_errors": []}
        self.assertEqual(1, len(ui_idle_check.sanity({"visible": silent, "hidden": hidden})))

    def test_a_page_that_does_not_catch_up_when_shown_again_fails(self):
        # Hidden pages must stop polling, but come back by themselves (#127),
        # lists included: ages and hold reviews moved on while hidden (R7b).
        for stuck in ([], None, ["status", "recording_status"]):
            hidden = dict(self.HIDDEN, catch_up_calls=stuck)
            problems = ui_idle_check.sanity({"visible": self.RUNNING, "hidden": hidden})
            self.assertEqual(1, len(problems), stuck)
            self.assertIn("shown again", problems[0])

    def test_a_page_whose_lists_stop_refreshing_fails(self):
        # The ratchets are ceilings: a page that fetched the lists only once
        # would pass them (R7b).
        never = dict(self.RUNNING, calls_per_minute={"status": 6, "recording_status": 6})
        stale = dict(self.RUNNING, redrawn_after_change=False)
        for visible, words in ((never, "in a minute"), (stale, "within one poll")):
            problems = ui_idle_check.sanity({"visible": visible, "hidden": self.HIDDEN})
            self.assertEqual(1, len(problems), words)
            self.assertIn(words, problems[0])

    def test_ratchet_file_covers_both_modes_with_known_metrics(self):
        data = json.loads((ROOT / "tests" / "budgets.json").read_text(encoding="utf-8"))
        ratchets = data["ui_idle"]["ratchets"]
        self.assertEqual(set(ui_idle_check.MODES), set(ratchets))
        for mode, limits in ratchets.items():
            self.assertEqual(METRICS, set(limits), mode)
        self.assertEqual({"transcribing": GROW_METRICS},
                         {m: set(v) for m, v in data["ui_transcribing"]["ratchets"].items()})


GROW_METRICS = {"transcript_calls", "rows_fetched", "rows_drawn", "full_redraws"}
RECORDING_METRICS = METRICS | {
    "calls_per_minute.recording_status", "calls_per_minute.status", "meter_draws_per_second",
    "live_region_changes_per_minute"}


class RecordingTests(unittest.TestCase):
    # The Meetings page with a recording open (#340): the clock and level poll,
    # and the level meter's draws on a simulated 120 Hz display, about METER_HZ
    # a second while visible (#216), none while hidden.
    VISIBLE = {"page_errors": [], "meter_draws_per_second": 60.0,
               "calls_per_minute": {"recording_status": 630, "status": 30}}
    HIDDEN = {"page_errors": [], "meter_draws_per_second": 0,
              "catch_up_calls": ["status", "recording_status"], "catch_up_meter_draws": 60}

    def problems(self, visible=None, hidden=None, meter_hz=60):
        return ui_idle_check.recording_problems(
            {"visible": dict(self.VISIBLE, **(visible or {})),
             "hidden": dict(self.HIDDEN, **(hidden or {}))}, meter_hz)

    def test_a_page_that_draws_at_the_meter_rate_and_stops_when_hidden_passes(self):
        self.assertEqual([], self.problems())
        self.assertEqual([], self.problems({"meter_draws_per_second": 56.0}))

    def test_each_way_of_getting_the_recording_page_wrong_fails(self):
        for visible, hidden, words in (
                ({"page_errors": ["TypeError: x"]}, None, "threw"),
                (None, {"page_errors": ["TypeError: x"]}, "threw"),
                # The meter stopped, or drew on every frame of the display.
                ({"meter_draws_per_second": 0}, None, "not about 60"),
                ({"meter_draws_per_second": 120.0}, None, "not about 60"),
                ({"meter_draws_per_second": None}, None, "not about 60"),
                # The clock and level stopped updating.
                ({"calls_per_minute": {"status": 30}}, None, "recording_status"),
                # Drawing while minimised, or not coming back when shown again.
                (None, {"meter_draws_per_second": 1.5}, "while hidden"),
                (None, {"catch_up_meter_draws": 0}, "shown again"),
                (None, {"catch_up_calls": ["status"]}, "shown again")):
            problems = self.problems(visible, hidden)
            self.assertEqual(1, len(problems), (visible, hidden))
            self.assertIn(words, problems[0])

    def test_the_meter_rate_follows_the_page(self):
        # At about 30 a second the same figures are wrong, so the check
        # follows METER_HZ in recording.js rather than a copy of it.
        self.assertEqual(60, ui_idle_check.meter_hz())
        self.assertEqual(1, len(self.problems(meter_hz=30)))

    def test_per_method_ratchets(self):
        ratchets = {"visible": {"calls_per_minute.recording_status": 630,
                                "calls_per_minute.status": 30}}
        self.assertEqual([], ui_idle_check.compare(
            {"visible": {"calls_per_minute": {"recording_status": 630}}}, ratchets))
        self.assertEqual([("visible", "calls_per_minute.recording_status", 1200, 630)],
                         ui_idle_check.compare(
                             {"visible": {"calls_per_minute": {"recording_status": 1200}}},
                             ratchets))
        # No per-method figures at all is a figure missing, not zero calls.
        self.assertEqual([("visible", "calls_per_minute.recording_status", None, 630),
                          ("visible", "calls_per_minute.status", None, 30)],
                         ui_idle_check.compare({"visible": {}}, ratchets))

    def test_ratchet_file_covers_the_recording_page(self):
        data = json.loads((ROOT / "tests" / "budgets.json").read_text(encoding="utf-8"))
        ratchets = data["ui_recording"]["ratchets"]
        self.assertEqual(set(ui_idle_check.MODES), set(ratchets))
        for mode, limits in ratchets.items():
            self.assertEqual(RECORDING_METRICS, set(limits), mode)
        self.assertEqual(0, ratchets["hidden"]["meter_draws_per_second"])
        self.assertEqual(0, ratchets["hidden"]["bridge_calls_per_minute"])

    def test_a_recording_poll_that_rebuilds_the_meeting_list_fails(self):
        # The clock and level poll ten times a second; rebuilding the meeting
        # list on any of those polls must fail the ratchets, visible or hidden
        # (#348). Every other figure here sits exactly at its ratchet.
        data = json.loads((ROOT / "tests" / "budgets.json").read_text(encoding="utf-8"))
        ratchets = data["ui_recording"]["ratchets"]
        for mode, limits in ratchets.items():
            at_limit = {}
            for metric, limit in limits.items():
                group, dot, key = metric.partition(".")
                if dot:
                    at_limit.setdefault(group, {})[key] = limit
                else:
                    at_limit[metric] = limit
            self.assertEqual([], ui_idle_check.compare({mode: at_limit}, {mode: limits}), mode)
            rebuilt = dict(at_limit, list_rebuilds_per_minute=5)
            self.assertEqual([(mode, "list_rebuilds_per_minute", 5, 0)],
                             ui_idle_check.compare({mode: rebuilt}, {mode: limits}), mode)

    def test_every_live_region_view_is_ratcheted_at_zero(self):
        # #346 delta review F2: the meeting banner, the assertive meeting error
        # and Setup's speaker status were also set at every poll.
        data = json.loads((ROOT / "tests" / "budgets.json").read_text(encoding="utf-8"))
        ratchets = data["ui_live_regions"]["ratchets"]
        self.assertEqual(set(ui_idle_check.LIVE_SCENARIOS), set(ratchets))
        for name, limits in ratchets.items():
            self.assertEqual({"live_region_changes_per_minute": 0}, limits, name)
        for mode, limits in data["ui_idle"]["ratchets"].items():
            self.assertEqual(0, limits["live_region_changes_per_minute"], mode)

    def test_the_failing_bridge_run_must_make_its_failing_calls(self):
        # #404: the run tests nothing if the page never made the calls that fail
        # (a renamed scenario falls back to an empty one in fixtures.js).
        name, methods = ui_idle_check.FAILING_SCENARIO
        self.assertIn(name, ui_idle_check.LIVE_SCENARIOS)
        made = {name: {"calls_per_minute": {m: 3 for m in methods}}}
        self.assertEqual([], ui_idle_check.failing_problems(made))
        self.assertEqual(len(methods), len(ui_idle_check.failing_problems(
            {name: {"calls_per_minute": {"status": 30}}})))
        self.assertEqual(len(methods), len(ui_idle_check.failing_problems({})))

    def test_a_steady_recording_that_changes_a_live_region_fails(self):
        # A screen reader may read a live region again at each change, even to
        # the same text: Narrator stuttered "Rec... Rec..." when the card's
        # state line was set at every poll (#346, 26 Oct 2026). A steady
        # recording has no change of state, so no live region may change.
        data = json.loads((ROOT / "tests" / "budgets.json").read_text(encoding="utf-8"))
        for mode, limits in data["ui_recording"]["ratchets"].items():
            self.assertEqual(0, limits["live_region_changes_per_minute"], mode)
            self.assertEqual([(mode, "live_region_changes_per_minute", 600, 0)],
                             ui_idle_check.compare(
                                 {mode: {"live_region_changes_per_minute": 600}},
                                 {mode: {"live_region_changes_per_minute": 0}}))


class TranscribingTests(unittest.TestCase):
    # A long meeting open while it is transcribed (#127): new passages are
    # appended, and the whole list is redrawn only where appending is not safe.
    RIGHT = {"page_errors": [], "each_once_in_order": True, "rows_shown": 3095,
             "rows_expected": 3095, "turns_right": True, "edit_held_back": True,
             "held_rows_drawn": 10, "held_rows_expected": 10, "edit_kept": True,
             "redraw_steps": [3, 7, 10], "expected_redraw_steps": [3, 7, 10]}

    def test_a_page_that_shows_every_passage_once_in_order_passes(self):
        self.assertEqual([], ui_idle_check.transcribing_problems(self.RIGHT))

    def test_each_way_of_showing_the_transcript_wrongly_fails(self):
        for change, words in (
                ({"page_errors": ["TypeError: x"]}, "threw"),
                ({"each_once_in_order": False, "rows_shown": 3105}, "once and in order"),
                ({"turns_right": False}, "turns"),
                ({"edit_held_back": False}, "being edited"),
                # The rows held back by the edit never drawn once it ended
                # (the given-up append's signature not put back) ...
                ({"held_rows_drawn": 0}, "held back during the edit"),
                # ... or the figure missing.
                ({"held_rows_drawn": None}, "held back during the edit"),
                ({"edit_kept": False}, "does not show the edit"),
                # A redraw missed (a re-done chunk appended after the end) ...
                ({"redraw_steps": [7, 10]}, "redrawn after steps"),
                # ... or the whole list redrawn on every step, as before #127.
                ({"redraw_steps": [1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12]}, "redrawn after steps"),
                ({"redraw_steps": None}, "redrawn after steps")):
            problems = ui_idle_check.transcribing_problems(dict(self.RIGHT, **change))
            self.assertEqual(1, len(problems), change)
            self.assertIn(words, problems[0])

    def test_the_counts_are_held_to_their_ratchets(self):
        ratchets = {"transcribing": {"rows_fetched": 9250, "full_redraws": 3}}
        self.assertEqual([], ui_idle_check.compare(
            {"transcribing": {"rows_fetched": 9250, "full_redraws": 3}}, ratchets))
        self.assertEqual([("transcribing", "rows_fetched", 36600, 9250),
                          ("transcribing", "full_redraws", 11, 3)],
                         ui_idle_check.compare(
                             {"transcribing": {"rows_fetched": 36600, "full_redraws": 11}},
                             ratchets))


class EarlyErrorTests(unittest.TestCase):
    # The page keeps the first error thrown before its bridge is ready and
    # reports it once the bridge is (#171).
    THROWN = [[517, 30], [518, 30]]

    def problems(self, *reports, thrown=THROWN):
        return ui_idle_check.early_problems({"thrown": thrown, "reports": list(reports)})

    def test_the_first_error_reported_once_after_the_bridge_is_ready_passes(self):
        self.assertEqual([], self.problems({"args": ["error", 517, 30], "before_ready": False}))

    def test_a_lost_repeated_or_premature_report_fails(self):
        first = {"args": ["error", 517, 30], "before_ready": False}
        for reports, words in (((), "reported 0"), ((first, first), "reported 2"),
                               ((dict(first, before_ready=True),), "before it was ready")):
            problems = self.problems(*reports)
            self.assertEqual(1, len(problems), words)
            self.assertIn(words, problems[0])

    def test_a_report_of_anything_but_the_first_errors_place_fails(self):
        for args in (["error", 518, 30], ["error", 517, 30, "first early error"],
                     ["rejection", 0, 0]):
            problems = self.problems({"args": args, "before_ready": False})
            self.assertEqual(1, len(problems), args)
            self.assertIn("not the first error's", problems[0])

    def test_a_page_that_did_not_see_both_errors_fails(self):
        problems = self.problems(thrown=[[517, 30]])
        self.assertEqual(1, len(problems))
        self.assertIn("not the 2 thrown", problems[0])

    def test_errors_from_one_place_or_after_the_bridge_is_ready_fail(self):
        first = {"args": ["error", 517, 30], "before_ready": False}
        problems = self.problems(first, thrown=[[517, 30], [517, 30]])
        self.assertEqual(1, len(problems))
        self.assertIn("same place", problems[0])
        late = ui_idle_check.early_problems({"thrown": self.THROWN, "reports": [first],
                                             "thrown_after_ready": 1})
        self.assertEqual(1, len(late))
        self.assertIn("after its bridge was ready", late[0])


class EdgeTimeoutTests(unittest.TestCase):
    """Headless Edge that does not return in time measured nothing: the check
    says so in one paragraph and exits 2, never with a raw traceback and never 0
    (AGENTS.md E2). No real Edge runs here."""

    def timed_out(self, *before):
        """subprocess.run as Edge would go: each of before's dumps, then a timeout."""
        def run(cmd, timeout=None, **kwargs):
            if calls:
                return calls.pop(0)
            raise subprocess.TimeoutExpired(cmd, timeout)
        calls = [subprocess.CompletedProcess([], 0, stdout=s, stderr="") for s in before]
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(design_check, "edge",
                                              return_value=Path("msedge.exe")))
        stack.enter_context(mock.patch("subprocess.run", side_effect=run))
        return stack

    def main(self):
        err = io.StringIO()
        with (mock.patch.object(sys, "argv", ["ui_idle_check.py"]),
              contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO())):
            code = ui_idle_check.main()
        return code, err.getvalue()

    def assert_says_not_measured(self, message, mode, scenario):
        for words in ("app/index.html", f"{mode} mode", f"scenario {scenario}",
                      f"own limit of {ui_idle_check.EDGE_LIMIT_S} s", "returned nothing",
                      "nothing was measured", "not a page result",
                      "no other headless Edge run is active"):
            self.assertIn(words, message)
        self.assertNotIn("Traceback", message)

    def test_a_timeout_on_the_first_run_is_not_measured_and_exits_2(self):
        with self.timed_out():
            code, message = self.main()
        self.assertEqual(2, code)
        self.assert_says_not_measured(message, "visible", "home")

    def test_a_timeout_in_hidden_mode_names_hidden_mode(self):
        # As on 25 Oct 2026 (#336): the visible run returned, the hidden one did not.
        with self.timed_out('<pre id="perf-result">{}</pre>'):
            code, message = self.main()
        self.assertEqual(2, code)
        self.assert_says_not_measured(message, "hidden", "home")

    def test_each_run_names_its_own_mode_and_scenario(self):
        base = "http://127.0.0.1:1"
        runs = ((lambda: ui_idle_check.run_early(base), "early-error", "home"),
                (lambda: ui_idle_check.run_transcribing(base), "transcribing",
                 "meeting-transcribing-long"),
                (lambda: ui_idle_check.run_page(base, "hidden", 60000, "recording", 120),
                 "hidden", "recording"),
                (lambda: ui_idle_check.run_locked(base, 60000), "locked", "locked"))
        for run, mode, scenario in runs:
            with self.timed_out(), self.assertRaises(design_check.EdgeTimeout) as caught:
                run()
            self.assert_says_not_measured(str(caught.exception), mode, scenario)

    def test_the_exit_codes_stay_apart(self):
        # 1 is a page result (over a ratchet or a page problem); a timeout is not one.
        self.assertEqual(2, design_check.NOT_MEASURED)


class LockedTests(unittest.TestCase):
    # The locked screen (#111 C4b): asked for the status only, called only
    # what the app allows while locked, and then nothing at all (no poll, no
    # timer), with no meeting title on the page.
    QUIET = {"page_errors": [], "locked_shown": True, "sample_titles_shown": [],
             "bridge_calls_per_minute": 0, "timeouts_per_minute": 0, "live_intervals": 0,
             "pending_animation_frames": 0, "all_calls": {"status": 1}}

    def test_the_methods_allowed_while_locked_are_the_bridges(self):
        # Read from workspace/api.py's source: the check needs no packages.
        self.assertGreaterEqual(len(ui_idle_check.locked_methods()), 5)
        if importlib.util.find_spec("numpy") is None:
            self.skipTest("numpy not installed (workspace.api imports it)")
        sys.path.insert(0, str(ROOT))
        from workspace import api
        self.assertEqual(set(api.LOCKED_METHODS), ui_idle_check.locked_methods())

    def test_a_quiet_locked_screen_passes(self):
        allowed = {"status", "copy_diagnostics", "close_app"}
        self.assertEqual([], ui_idle_check.locked_problems(self.QUIET, allowed))

    def test_each_way_of_leaving_the_locked_screen_busy_fails(self):
        allowed = {"status", "copy_diagnostics", "close_app"}
        for change, words in (
                ({"page_errors": ["TypeError: x"]}, "threw"),
                ({"locked_shown": False}, "not shown"),
                ({"sample_titles_shown": ["Board meeting (sample)"]}, "meeting title"),
                ({"bridge_calls_per_minute": 6}, "bridge calls a minute"),
                ({"timeouts_per_minute": 6}, "timers a minute"),
                ({"live_intervals": 1}, "interval"),
                ({"pending_animation_frames": 1}, "animation frame"),
                ({"all_calls": {"status": 1, "meetings": 1}}, "meetings"),
                ({"all_calls": {}}, "never asked for the status"),
                ({"bridge_calls_per_minute": None}, "bridge calls a minute")):
            problems = ui_idle_check.locked_problems(dict(self.QUIET, **change), allowed)
            self.assertEqual(1, len(problems), change)
            self.assertIn(words, problems[0])

    def test_after_a_failed_first_status_the_start_may_ask_but_must_end_locked(self):
        # Review of 7b28de1: the first status() fails, so the page starts as
        # usual and asks for what the bridge then refuses; the next status
        # must still end on the locked screen, quiet. Its own label.
        late = dict(self.QUIET, all_calls={"status": 2, "setup_state": 1, "meetings": 1})
        self.assertEqual([], ui_idle_check.locked_problems(late, None, "locked after an error"))
        self.assertEqual([], ui_idle_check.locked_problems(
            late, None, "locked after an error", status_calls=2))
        for change, words in (({"locked_shown": False}, "not shown"),
                              ({"bridge_calls_per_minute": 12}, "bridge calls a minute"),
                              ({"all_calls": {"status": 1}}, "asked for the status 1 time")):
            problems = ui_idle_check.locked_problems(
                dict(late, **change), None, "locked after an error", status_calls=2)
            self.assertEqual(1, len(problems), change)
            self.assertTrue(problems[0].startswith("locked after an error: "), problems[0])
            self.assertIn(words, problems[0])


if __name__ == "__main__":
    unittest.main()
