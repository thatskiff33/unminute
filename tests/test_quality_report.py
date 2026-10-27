# The report-only quality report (#365 part C): tools/quality_report.py counts
# the lines of tracked code files, reads ruff's C901 output, compares both with
# the committed baseline and never fails on what it finds; the Quality workflow
# runs it. ruff and git are not needed here: their output is synthetic.
#   python tools/run_tests.py --only test_quality_report

import importlib.util
import io
import json
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


qr = load("quality_report")


def read(path):
    return (ROOT / path).read_text(encoding="utf-8").replace("\r\n", "\n")


def diagnostic(path, row, name, n, code="C901"):
    return {"code": code, "filename": str(ROOT / path), "location": {"row": row, "column": 5},
            "message": f"`{name}` is too complex ({n} > 10)"}


RUFF = [diagnostic("workspace/app.py", 40, "main", 30),
        diagnostic("workspace/app.py", 90, "helper", 12),
        diagnostic("workspace/store.py", 10, "save", 14),
        diagnostic("workspace/store.py", 70, "save", 11),
        {"code": None, "filename": str(ROOT / "workspace" / "x.py"),
         "location": {"row": 1, "column": 1}, "message": "SyntaxError: bad"}]


class SizeTests(unittest.TestCase):
    def test_lines_are_counted_with_or_without_a_last_newline(self):
        self.assertEqual(qr.count_lines(b""), 0)
        self.assertEqual(qr.count_lines(b"a\n"), 1)
        self.assertEqual(qr.count_lines(b"a\r\nb"), 2)
        self.assertEqual(qr.count_lines(b"a\n\nb\n"), 3)

    def test_only_code_folders_and_types_are_in_scope(self):
        for path in ("workspace/app.py", "workspace/ui/index.html", "tools/x.ps1",
                     "tests/test_x.py", "packaging/install.ps1", ".github/workflows/ci.yml"):
            with self.subTest(path=path):
                self.assertTrue(qr.in_scope(path))
        for path in ("docs/design/boards/a.html", "requirements.txt", "README.md",
                     "workspace/assets/font.woff2", "extracted/x.py", "setup.py"):
            with self.subTest(path=path):
                self.assertFalse(qr.in_scope(path))

    def test_line_counts_read_only_the_named_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            (root / "tools" / "a.py").write_bytes(b"x = 1\ny = 2\n")
            counts = qr.line_counts(["tools/a.py", "tools/gone.py"], root=root)
        self.assertEqual(counts, {"tools/a.py": 2})

    def test_growth_new_and_removed_against_the_baseline(self):
        grown, removed = qr.compare_sizes({"a.py": 120, "b.py": 50, "c.py": 30, "d.py": 10},
                                          {"a.py": 100, "b.py": 60, "c.py": 30, "e.py": 5})
        self.assertEqual(grown, [("a.py", 100, 120), ("d.py", None, 10)])
        self.assertEqual(removed, ["e.py"])


class ComplexityTests(unittest.TestCase):
    def test_c901_only_with_relative_paths_most_complex_first(self):
        found = qr.complexity(RUFF)
        self.assertEqual(found, [("workspace/app.py", 40, "main", 30),
                                 ("workspace/store.py", 10, "save", 14),
                                 ("workspace/app.py", 90, "helper", 12),
                                 ("workspace/store.py", 70, "save", 11)])

    def test_an_unknown_c901_message_stops_the_report(self):
        bad = [dict(RUFF[0], message="main: complexity 30")]
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as cm:
            qr.complexity(bad)
        self.assertEqual(cm.exception.code, 2)

    def test_baseline_groups_by_file_and_function(self):
        self.assertEqual(qr.baseline_complexity(qr.complexity(RUFF)),
                         {"workspace/app.py::helper": [12], "workspace/app.py::main": [30],
                          "workspace/store.py::save": [14, 11]})

    def test_new_and_more_complex_against_the_baseline(self):
        found = qr.complexity(RUFF)
        baseline = {"workspace/app.py::main": [25], "workspace/store.py::save": [14],
                    "workspace/old.py::gone": [20]}
        worse, fixed = qr.compare_complexity(found, baseline)
        # main rose from 25; the second save is new (paired highest first); helper is new.
        self.assertEqual(worse, [("workspace/app.py", 40, "main", 30, 25),
                                 ("workspace/app.py", 90, "helper", 12, None),
                                 ("workspace/store.py", 70, "save", 11, None)])
        self.assertEqual(fixed, [("workspace/old.py::gone", 20)])

    def test_the_same_functions_report_nothing(self):
        found = qr.complexity(RUFF)
        self.assertEqual(qr.compare_complexity(found, qr.baseline_complexity(found)), ([], []))

    def test_a_simpler_function_is_not_reported_as_worse(self):
        found = qr.complexity(RUFF)
        baseline = dict(qr.baseline_complexity(found), **{"workspace/app.py::main": [40]})
        self.assertEqual(qr.compare_complexity(found, baseline), ([], []))


class ReportOnlyTests(unittest.TestCase):
    """Growth and complexity are reported, never failed; only git or ruff not
    running fails, and then the baseline is left alone."""

    BASELINE = {"_about": "test", "max_complexity": 10,
                "lines": {"workspace/app.py": 100, "workspace/gone.py": 5},
                "complexity": {"workspace/app.py::main": [25]}}

    def run_main(self, env, argv=(), ruff_code=0, ruff_out=None, git_code=0, installed=True):
        def fake_run(cmd, **_kwargs):
            if cmd[0] == "git":
                return mock.Mock(returncode=git_code, stdout="workspace/app.py\0docs/x.md\0",
                                 stderr="fatal: test")
            return mock.Mock(returncode=ruff_code, stderr="",
                             stdout=json.dumps(RUFF) if ruff_out is None else ruff_out)

        spec = mock.Mock() if installed else None
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "baseline.json"
            baseline_text = json.dumps(self.BASELINE)
            baseline.write_text(baseline_text, encoding="utf-8")
            summary = Path(tmp) / "summary.md"
            env = {k: v.replace("{summary}", str(summary)) for k, v in env.items()}
            out = io.StringIO()
            with mock.patch.object(qr, "BASELINE", baseline), \
                    mock.patch.object(qr.subprocess, "run", side_effect=fake_run) as run, \
                    mock.patch.object(qr, "line_counts",
                                      side_effect=lambda paths: dict.fromkeys(paths, 130)), \
                    mock.patch("importlib.util.find_spec", return_value=spec), \
                    mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
                try:
                    code = qr.main(list(argv))
                except SystemExit as exc:
                    code = exc.code
            self.baseline_after = baseline.read_text(encoding="utf-8")
            self.baseline_kept = self.baseline_after == baseline_text
            self.commands = [c.args[0][0] if c.args[0][0] == "git" else "ruff"
                             for c in run.call_args_list]
            return code, out.getvalue(), summary.read_text(encoding="utf-8") \
                if summary.exists() else ""

    def test_growth_and_complexity_are_warnings_on_github_and_pass(self):
        code, out, summary = self.run_main({"GITHUB_ACTIONS": "true",
                                            "GITHUB_STEP_SUMMARY": "{summary}"})
        self.assertEqual(code, 0)
        self.assertIn("Size: 1 code files, 130 lines (baseline 2 files, 105 lines; +25)", out)
        self.assertIn("+30  workspace/app.py (100 -> 130)", out)
        self.assertIn("removed: 1", out)
        self.assertIn("new or more complex: 4", out)
        self.assertIn("::warning file=workspace/app.py,line=40,title=Complexity (report only)::"
                      "main has complexity 30 (was 25; threshold 10)", out)
        self.assertIn("Largest Python modules", summary)
        self.assertIn("new or more complex: 4", summary)
        # The docs/ path git listed is out of scope.
        self.assertNotIn("docs/x.md", out)

    def test_no_warnings_off_github(self):
        code, out, _ = self.run_main({"GITHUB_ACTIONS": "false"})
        self.assertEqual(code, 0)
        self.assertNotIn("::warning", out)

    def test_git_or_ruff_that_cannot_run_fails_and_keeps_the_baseline(self):
        cases = {"git fails": {"git_code": 128}, "ruff fails": {"ruff_code": 2},
                 "ruff missing": {"installed": False}, "ruff not json": {"ruff_out": ""},
                 "ruff not a list": {"ruff_out": "{}"}}
        for label, kwargs in cases.items():
            for argv in ((), ("--update",)):
                with self.subTest(case=label, argv=argv):
                    code, out, _ = self.run_main({"GITHUB_ACTIONS": "false"}, argv=argv,
                                                 **kwargs)
                    self.assertEqual(code, 2)
                    self.assertNotIn("Quality report", out)
                    self.assertTrue(self.baseline_kept)
                    if label == "ruff missing":
                        self.assertEqual(self.commands, ["git"])

    def test_update_rewrites_the_baseline(self):
        code, out, _ = self.run_main({"GITHUB_ACTIONS": "false"}, argv=("--update",))
        self.assertEqual(code, 0)
        self.assertIn("Wrote 1 file(s) and 4 complex function(s)", out)
        data = json.loads(self.baseline_after)
        self.assertEqual(data["lines"], {"workspace/app.py": 130})
        self.assertEqual(data["complexity"]["workspace/store.py::save"], [14, 11])

    def test_help_has_a_description(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), self.assertRaises(SystemExit):
            qr.main(["--help"])
        self.assertIn("baseline", out.getvalue().split("options:")[0])


class BaselineAndWorkflowTests(unittest.TestCase):
    def test_baseline_is_sorted_and_well_formed(self):
        # Well formed only: the baseline is refreshed when figures are accepted,
        # so it is never required to match the current tree (that would be a gate).
        data = json.loads(read("tools/quality_baseline.json"))
        self.assertEqual(data["max_complexity"], qr.MAX_COMPLEXITY)
        self.assertEqual(list(data["lines"]), sorted(data["lines"]))
        self.assertEqual([p for p in data["lines"] if not qr.in_scope(p)], [])
        self.assertTrue(all(isinstance(n, int) and n >= 0 for n in data["lines"].values()))
        self.assertEqual(list(data["complexity"]), sorted(data["complexity"]))
        key = re.compile(r"^[\w./-]+\.py::\w+$")
        self.assertEqual([k for k in data["complexity"] if not key.match(k)], [])
        for values in data["complexity"].values():
            self.assertEqual(values, sorted(values, reverse=True))
            self.assertTrue(all(n > qr.MAX_COMPLEXITY for n in values))

    def test_quality_workflow_runs_the_report_only_job(self):
        workflow = read(".github/workflows/quality.yml")
        job = workflow[workflow.index("\n  quality-report:"):workflow.index("\n  type-check:")]
        self.assertIn("name: Quality report (report only)", job)
        self.assertIn("--require-hashes -r requirements/ci.txt", job)
        self.assertIn("run: python tools/quality_report.py\n", job)
        self.assertIn("persist-credentials: false", job)
        self.assertIn("contents: read", job)
        self.assertNotIn("continue-on-error", job)

    def test_ruff_is_run_report_only_with_c901(self):
        source = read("tools/quality_report.py")
        self.assertIn('"--select", "C901"', source)
        self.assertIn('"--exit-zero"', source)
        self.assertIn('"--no-cache"', source)


if __name__ == "__main__":
    unittest.main()
