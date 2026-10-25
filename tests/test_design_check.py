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


if __name__ == "__main__":
    unittest.main()
