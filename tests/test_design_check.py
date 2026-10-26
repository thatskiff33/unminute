# tests/test_design_check.py
# tools/design_check.py when headless Edge does not return in time: it says
# nothing was measured and exits 2, rather than dying with a raw traceback that
# reads like a page failure. No real Edge runs here.

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import design_check  # noqa: E402


class EdgeTimeoutTests(unittest.TestCase):
    def run_case(self, case, shots_before_timeout=0):
        """design_check.main() on one case, Edge making shots_before_timeout
        screenshots and then timing out. Returns (exit code, stderr, results)."""
        shots = [shots_before_timeout]

        def run(cmd, timeout=None, **kwargs):
            if shots[0]:
                shots[0] -= 1
                out = next(a for a in cmd if a.startswith("--screenshot="))
                Path(out.split("=", 1)[1]).write_bytes(b"")
                return subprocess.CompletedProcess(cmd, 0)
            raise subprocess.TimeoutExpired(cmd, timeout)

        err = io.StringIO()
        with (tempfile.TemporaryDirectory() as out,
              mock.patch.object(design_check, "edge", return_value=Path("msedge.exe")),
              mock.patch("subprocess.run", side_effect=run),
              mock.patch.object(sys, "argv", ["design_check.py", out, case]),
              contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO())):
            code = design_check.main()
            results = json.loads((Path(out) / "results.json").read_text(encoding="utf-8"))
        return code, err.getvalue(), results

    def assert_says_not_measured(self, message, page, mode, scenario):
        for words in (page, f"{mode} mode", f"scenario {scenario}",
                      f"own limit of {design_check.EDGE_LIMIT_S} s", "returned nothing",
                      "nothing was measured", "not a page result",
                      "no other headless Edge run is active"):
            self.assertIn(words, message)
        self.assertNotIn("Traceback", message)

    def test_a_board_that_times_out_is_not_measured_and_exits_2(self):
        code, message, results = self.run_case("home")
        self.assertEqual(2, code)
        self.assert_says_not_measured(message, "Home.dc.html", "board", "home")
        self.assertEqual([], results)

    def test_an_app_screen_that_times_out_names_the_app_page(self):
        code, message, results = self.run_case("home-dark", shots_before_timeout=1)
        self.assertEqual(2, code)
        self.assert_says_not_measured(message, "app/index.html", "app", "home-dark")
        self.assertEqual([], results)


class CanvasLayoutTests(unittest.TestCase):
    """docs/design/boards/canvas.json lays every board out once, in its row,
    with no overlaps (#403: a board left out of "order" was missing from the
    canvas sequence)."""

    BOARDS = ROOT / "docs" / "design" / "boards"

    def setUp(self):
        self.canvas = json.loads((self.BOARDS / "canvas.json").read_text(encoding="utf-8"))
        self.boards = self.canvas["boards"]

    def test_every_board_file_is_placed_and_in_the_order_once(self):
        files = sorted(p.name for p in self.BOARDS.glob("*.dc.html"))
        order = self.canvas["order"]
        self.assertEqual(files, sorted(self.boards))
        self.assertEqual(len(order), len(set(order)), "a board is in the order twice")
        self.assertEqual(files, sorted(order))

    def test_the_order_runs_row_by_row_left_to_right(self):
        by_position = sorted(self.boards, key=lambda b: (self.boards[b]["y"], self.boards[b]["x"]))
        self.assertEqual(by_position, self.canvas["order"])

    def test_no_two_boards_overlap(self):
        names = sorted(self.boards)
        for i, a in enumerate(names):
            ra = self.boards[a]
            for b in names[i + 1:]:
                rb = self.boards[b]
                apart = (ra["x"] + ra["w"] <= rb["x"] or rb["x"] + rb["w"] <= ra["x"]
                         or ra["y"] + ra["h"] <= rb["y"] or rb["y"] + rb["h"] <= ra["y"])
                self.assertTrue(apart, f"{a} overlaps {b}")

    def test_each_row_title_spans_its_row(self):
        rows = sorted(self.canvas["notes"].values(), key=lambda n: n["y"])
        for row, below in zip(rows, rows[1:] + [None], strict=True):
            ends = [b["x"] + b["w"] for b in self.boards.values()
                    if b["y"] > row["y"] and (below is None or b["y"] < below["y"])]
            self.assertTrue(ends, f"row {row['text']!r} has no boards")
            self.assertEqual(max(ends), row["maxW"], row["text"])


if __name__ == "__main__":
    unittest.main()
