# tests/test_ui_idle_check.py
# The ratchet logic of tools/ui_idle_check.py (docs/RELEASE_STANDARD.md,
# section 3). The check itself runs the page in headless Edge, in the quality
# workflow (.github/workflows/quality.yml).

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import ui_idle_check  # noqa: E402

METRICS = {"bridge_calls_per_minute", "list_rebuilds_per_minute", "timeouts_per_minute",
           "pending_animation_frames", "live_intervals", "fastest_interval_ms"}


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

    def test_a_page_that_threw_or_is_not_running_fails(self):
        running = {"bridge_calls_per_minute": 120, "meeting_rows": 3, "page_errors": []}
        self.assertEqual([], ui_idle_check.sanity({"visible": running, "hidden": running}))
        threw = dict(running, page_errors=["TypeError: x is undefined"])
        self.assertEqual(1, len(ui_idle_check.sanity({"visible": running, "hidden": threw})))
        silent = {"bridge_calls_per_minute": 0, "meeting_rows": 0, "page_errors": []}
        self.assertEqual(1, len(ui_idle_check.sanity({"visible": silent, "hidden": silent})))

    def test_ratchet_file_covers_both_modes_with_known_metrics(self):
        data = json.loads((ROOT / "tests" / "budgets.json").read_text(encoding="utf-8"))
        ratchets = data["ui_idle"]["ratchets"]
        self.assertEqual(set(ui_idle_check.MODES), set(ratchets))
        for mode, limits in ratchets.items():
            self.assertEqual(METRICS, set(limits), mode)


if __name__ == "__main__":
    unittest.main()
