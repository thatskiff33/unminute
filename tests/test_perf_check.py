# tests/test_perf_check.py
# The budget logic of tools/perf_check.py (docs/RELEASE_STANDARD.md, section 2).
# The measuring itself opens a real window and is run by hand at release time.

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


if __name__ == "__main__":
    unittest.main()
