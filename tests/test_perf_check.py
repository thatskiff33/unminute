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
        runs = [{"worker": {"exit_after_last_use_s": None, "exited": False}},
                {"worker": {"exit_after_last_use_s": 121.0, "exited": True}},
                {"worker": {"exit_after_last_use_s": 123.0, "exited": True}}]
        self.assertEqual({"worker": {"exit_after_last_use_s": 122.0}},
                         perf_check.median_report(runs))


class BudgetTests(unittest.TestCase):
    def test_only_figures_over_a_set_budget_are_reported(self):
        figures = {"meetings": {"cpu_percent_of_one_core": 0.4, "private_mb": 300},
                   "minimised": {"cpu_percent_of_one_core": 0.05}}
        budgets = {"meetings": {"cpu_percent_of_one_core": 0.3, "private_mb": None},
                   "minimised": {"cpu_percent_of_one_core": 0.1},
                   "settings": {"cpu_percent_of_one_core": 0.5}}       # not measured
        self.assertEqual([("meetings", "cpu_percent_of_one_core", 0.4, 0.3)],
                         perf_check.compare(figures, budgets))

    def test_proposed_budgets_add_the_margin(self):
        proposed = perf_check.propose({"meetings": {"cpu_percent_of_one_core": 1.0,
                                                    "private_mb": 1000, "processes": 9},
                                       "minimised": {"cpu_percent_of_one_core": 0.01},
                                       "startup": {"startup_s": 2.0}})
        self.assertEqual({"meetings": {"cpu_percent_of_one_core": 1.2, "private_mb": 1200},
                          "minimised": {"cpu_percent_of_one_core": 0.06},
                          "startup": {"startup_s": 2.4}}, proposed)

    def test_budget_file_is_valid_and_uses_known_metrics(self):
        path = ROOT / "docs" / "perf-budgets.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        known = set(perf_check.BUDGETED) | {"exit_after_last_use_s", "private_growth_mb"}
        for state, limits in data["budgets"].items():
            self.assertTrue(set(limits) <= known, f"{state}: unknown metric in {path.name}")
            for metric, limit in limits.items():
                self.assertTrue(limit is None or limit > 0, f"{state} {metric}")


if __name__ == "__main__":
    unittest.main()
