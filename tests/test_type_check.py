# The report-only type check and branch coverage (#413): tools/type_check.py
# reads mypy's output, compares it with the committed baseline and never fails
# on what mypy finds; the Quality workflow runs it and measures branches and
# child processes. mypy itself is not needed here: its output is synthetic.
#   python tools/run_tests.py --only test_type_check

import configparser
import importlib.util
import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tc = load("type_check")


def read(path):
    return (ROOT / path).read_text(encoding="utf-8").replace("\r\n", "\n")


OUTPUT = (
    "workspace\\store.py:12: error: Item \"None\" of \"X | None\" has no attribute \"y\"  "
    "[union-attr]\n"
    "workspace\\store.py:12: note: See https://mypy.readthedocs.io for more\n"
    "workspace/app.py:40:7: error: Name \"f\" already defined on line 31  [no-redef]\n"
    "workspace/app.py:90: error: Need type annotation for \"x\"  [var-annotated]\n"
    "workspace/app.py:95: error: Need type annotation for \"x\"  [var-annotated]\n")


class ReadOutputTests(unittest.TestCase):
    def test_errors_are_keyed_without_line_numbers(self):
        found = tc.errors(OUTPUT)
        self.assertEqual([(p, n) for p, n, _ in found],
                         [("workspace/store.py", 12), ("workspace/app.py", 40),
                          ("workspace/app.py", 90), ("workspace/app.py", 95)])
        self.assertEqual(found[0][2], 'workspace/store.py: error: Item "None" of "X | None" '
                                      'has no attribute "y"  [union-attr]')
        # A line number inside the message moves with the code too.
        self.assertEqual(found[1][2],
                         'workspace/app.py: error: Name "f" already defined on line N  [no-redef]')

    def test_notes_and_summaries_are_not_errors(self):
        self.assertEqual(tc.errors("workspace/a.py:3: note: hint\nFound 1 error in 1 file\n"), [])


class CompareTests(unittest.TestCase):
    def test_counted_against_the_baseline(self):
        found = tc.errors(OUTPUT)
        keys = [k for _, _, k in found]
        new, fixed = tc.compare(found, keys[:3] + ["workspace/gone.py: error: old  [misc]"])
        # The second copy of an accepted error is new; one no longer found is fixed.
        self.assertEqual([(p, n) for p, n, _ in new], [("workspace/app.py", 95)])
        self.assertEqual(fixed, ["workspace/gone.py: error: old  [misc]"])

    def test_the_same_errors_report_nothing_new(self):
        found = tc.errors(OUTPUT)
        self.assertEqual(tc.compare(found, [k for _, _, k in reversed(found)]), ([], []))


class ReportOnlyTests(unittest.TestCase):
    """New errors are reported, never failed; only a mypy that cannot run fails."""

    BASELINE_TEXT = tc.HEADER + "workspace/a.py: error: kept  [misc]\n"

    def run_main(self, stdout, returncode, env, stderr="", argv=(), installed=True):
        result = mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)
        # Whether mypy is installed is decided here, not by the test's own venv.
        spec = mock.Mock() if installed else None
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "baseline.txt"
            baseline.write_text(self.BASELINE_TEXT, encoding="utf-8")
            summary = Path(tmp) / "summary.md"
            env = {k: v.replace("{summary}", str(summary)) for k, v in env.items()}
            out = io.StringIO()
            with mock.patch.object(tc, "BASELINE", baseline), \
                    mock.patch.object(tc.subprocess, "run", return_value=result) as run, \
                    mock.patch("importlib.util.find_spec", return_value=spec), \
                    mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
                try:
                    code = tc.main(list(argv))
                except SystemExit as exc:
                    code = exc.code
            self.baseline_after = baseline.read_text(encoding="utf-8")
            self.ran = run.called
            return code, out.getvalue(), summary.read_text(encoding="utf-8") \
                if summary.exists() else ""

    def test_new_errors_are_warnings_on_github_and_pass(self):
        code, out, summary = self.run_main(OUTPUT, 1, {"GITHUB_ACTIONS": "true",
                                                       "GITHUB_STEP_SUMMARY": "{summary}"})
        self.assertEqual(code, 0)
        self.assertIn("new since the baseline: 4", out)
        self.assertIn("::warning file=workspace/app.py,line=90,title=mypy (report only)::", out)
        self.assertIn("new since the baseline: 4", summary)

    def test_a_mypy_that_cannot_run_fails(self):
        code, _, _ = self.run_main("mypy: error: bad config\n", 2, {"GITHUB_ACTIONS": "false"})
        self.assertEqual(code, 2)

    def test_a_missing_mypy_fails_and_keeps_the_baseline(self):
        # Review B1 on 2b8836f: Python exits 1 when it cannot import mypy, the
        # code mypy uses for "errors found". With no error line that is not a
        # clean run, and --update must not empty the baseline.
        missing = "No module named mypy\n"
        for argv in ((), ("--update",)):
            for installed in (True, False):
                with self.subTest(argv=argv, installed=installed):
                    code, out, _ = self.run_main("", 1, {"GITHUB_ACTIONS": "false"},
                                                 stderr=missing, argv=argv,
                                                 installed=installed)
                    self.assertEqual(code, 2)
                    self.assertNotIn("fixed since the baseline", out)
                    self.assertEqual(self.baseline_after, self.BASELINE_TEXT)
                    if not installed:
                        self.assertFalse(self.ran)

    def test_update_rewrites_the_baseline_when_mypy_ran(self):
        code, out, _ = self.run_main(OUTPUT, 1, {"GITHUB_ACTIONS": "false"}, argv=("--update",))
        self.assertEqual(code, 0)
        self.assertIn("Wrote 4 error(s)", out)
        self.assertNotIn("workspace/a.py: error: kept", self.baseline_after)

    def test_help_has_a_description(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), self.assertRaises(SystemExit):
            tc.main(["--help"])
        self.assertIn("baseline", out.getvalue().split("options:")[0])


class BaselineAndWorkflowTests(unittest.TestCase):
    def test_baseline_is_sorted_and_well_formed(self):
        lines = tc.read_baseline()
        self.assertEqual(lines, sorted(lines))
        line = re.compile(r"^workspace/[\w/]+\.py: error: .+  \[[a-z-]+\]$")
        self.assertEqual([x for x in lines if not line.match(x)], [])
        self.assertNotRegex("\n".join(lines), r"on line \d")

    def test_mypy_reads_no_installed_package_and_checks_windows(self):
        config = configparser.ConfigParser()
        config.read(ROOT / "mypy.ini", encoding="utf-8")
        self.assertEqual(config["mypy"]["platform"], "win32")
        self.assertEqual(config["mypy"]["no_site_packages"], "True")
        self.assertEqual(config["mypy"]["check_untyped_defs"], "True")

    def test_quality_workflow_runs_both_reports(self):
        workflow = read(".github/workflows/quality.yml")
        job = workflow[workflow.index("\n  type-check:"):workflow.index("\n  coverage:")]
        self.assertIn("--require-hashes --no-deps -r requirements/ci-mypy.txt", job)
        self.assertIn("run: python tools/type_check.py\n", job)
        self.assertNotIn("continue-on-error", job)
        coverage = configparser.ConfigParser()
        coverage.read(ROOT / ".coveragerc", encoding="utf-8")
        self.assertEqual(coverage["run"]["branch"], "True")
        self.assertEqual(coverage["run"]["patch"], "subprocess")
        self.assertEqual(coverage["run"]["source"], "workspace")
        self.assertNotIn("fail_under", coverage["report"])
        self.assertIn("python -m coverage combine", workflow)


if __name__ == "__main__":
    unittest.main()
