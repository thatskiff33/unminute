# The report-only security lint (#415 item 2): tools/security_lint.py reads
# ruff's S (flake8-bandit) output, compares it with the triaged baseline in
# tools/security_baseline.json and never fails on what it finds. ruff is not
# needed here: its output is synthetic.
#   python tools/run_tests.py --only test_security_lint

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


sl = load("security_lint")


def read(path):
    return (ROOT / path).read_text(encoding="utf-8").replace("\r\n", "\n")


def diagnostic(path, row, code, message="m"):
    return {"code": code, "filename": str(ROOT / path), "location": {"row": row, "column": 1},
            "message": message}


RUFF = [diagnostic("workspace/store.py", 30, "S608", "Possible SQL injection"),
        diagnostic("workspace/store.py", 10, "S608", "Possible SQL injection"),
        diagnostic("workspace/app.py", 5, "S110"),
        diagnostic("tools/x.py", 7, "S603"),
        diagnostic("workspace/a.py", 1, "F401"),                       # not an S rule
        {"code": None, "filename": str(ROOT / "workspace" / "bad.py"),
         "location": {"row": 1, "column": 1}, "message": "SyntaxError: bad"}]


class CompareTests(unittest.TestCase):
    def test_only_s_rules_with_relative_paths_in_order(self):
        found = sl.findings(RUFF)
        self.assertEqual([(p, n, c) for p, n, c, _m in found],
                         [("tools/x.py", 7, "S603"), ("workspace/app.py", 5, "S110"),
                          ("workspace/store.py", 10, "S608"), ("workspace/store.py", 30, "S608")])

    def test_over_fewer_and_untriaged_against_the_baseline(self):
        found = sl.findings(RUFF)
        baseline = {"workspace/store.py::S608": {"count": 1, "why": "checked identifiers"},
                    "workspace/app.py::S110": {"count": 1, "why": " "},
                    "workspace/gone.py::S101": {"count": 2, "why": "fixed since"}}
        new, fixed, untriaged = sl.compare(found, baseline)
        # store.py has one more than accepted: both its findings are listed.
        self.assertEqual([(k, n, [f[1] for f in items]) for k, n, items in new],
                         [("tools/x.py::S603", 0, [7]), ("workspace/store.py::S608", 1, [10, 30])])
        self.assertEqual(fixed, [("workspace/gone.py::S101", 2, 0)])
        self.assertEqual(untriaged, ["workspace/app.py::S110"])

    def test_the_same_findings_report_nothing(self):
        found = sl.findings(RUFF)
        baseline = {k: {"count": n, "why": "accepted"} for k, n in sl.counts(found).items()}
        self.assertEqual(sl.compare(found, baseline), ([], [], []))


class MainTests(unittest.TestCase):
    """Findings are reported, never failed; only ruff not running fails, and
    then the baseline is left alone."""

    BASELINE = {"_about": "test", "findings": {
        "workspace/store.py::S608": {"count": 1, "why": "checked identifiers"},
        "workspace/gone.py::S101": {"count": 1, "why": "fixed since"}}}

    def run_main(self, env, argv=(), ruff_code=0, ruff_out=None, installed=True):
        def fake_run(cmd, **_kwargs):
            self.cmd = cmd
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
            with mock.patch.object(sl, "BASELINE", baseline), \
                    mock.patch.object(sl.subprocess, "run", side_effect=fake_run), \
                    mock.patch("importlib.util.find_spec", return_value=spec), \
                    mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
                try:
                    code = sl.main(list(argv))
                except SystemExit as exc:
                    code = exc.code
            self.baseline_after = baseline.read_text(encoding="utf-8")
            self.baseline_kept = self.baseline_after == baseline_text
            return code, out.getvalue(), summary.read_text(encoding="utf-8") \
                if summary.exists() else ""

    def test_new_findings_are_warnings_on_github_and_pass(self):
        code, out, summary = self.run_main({"GITHUB_ACTIONS": "true",
                                            "GITHUB_STEP_SUMMARY": "{summary}"})
        self.assertEqual(code, 0)
        self.assertIn("Findings: 4 (baseline 2); files and rules over the baseline: 3; "
                      "fewer than the baseline: 1; untriaged: 0", out)
        self.assertIn("By rule: S110 1, S603 1, S608 2", out)
        self.assertIn("::warning file=workspace/store.py,line=30,title=Security lint "
                      "(report only)::S608 Possible SQL injection", out)
        self.assertIn("workspace/gone.py::S101: 0 (was 1)", out)
        self.assertIn("files and rules over the baseline: 3", summary)
        self.assertEqual(self.cmd[1:], ["-m", "ruff", "check", "--select", "S", "--exit-zero",
                                        "--no-cache", "--output-format", "json", "."])

    def test_no_warnings_off_github(self):
        code, out, _ = self.run_main({"GITHUB_ACTIONS": "false"})
        self.assertEqual(code, 0)
        self.assertNotIn("::warning", out)

    def test_ruff_that_cannot_run_fails_and_keeps_the_baseline(self):
        cases = {"ruff fails": {"ruff_code": 2}, "ruff missing": {"installed": False},
                 "ruff not json": {"ruff_out": ""}, "ruff not a list": {"ruff_out": "{}"}}
        for label, kwargs in cases.items():
            for argv in ((), ("--update",)):
                with self.subTest(case=label, argv=argv):
                    code, out, _ = self.run_main({"GITHUB_ACTIONS": "false"}, argv=argv,
                                                 **kwargs)
                    self.assertEqual(code, 2)
                    self.assertNotIn("Security lint", out)
                    self.assertTrue(self.baseline_kept)

    def test_update_keeps_reasons_drops_fixed_and_leaves_new_ones_empty(self):
        code, out, _ = self.run_main({"GITHUB_ACTIONS": "false"}, argv=("--update",))
        self.assertEqual(code, 0)
        self.assertIn("Wrote 3 file(s) and rule(s), 4 finding(s)", out)
        self.assertIn("2 without a reason", out)
        data = json.loads(self.baseline_after)["findings"]
        self.assertEqual(data["workspace/store.py::S608"],
                         {"count": 2, "why": "checked identifiers"})
        self.assertEqual(data["tools/x.py::S603"], {"count": 1, "why": ""})
        self.assertNotIn("workspace/gone.py::S101", data)
        self.assertEqual(list(data), sorted(data))

    def test_help_has_a_description(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), self.assertRaises(SystemExit):
            sl.main(["--help"])
        self.assertIn("baseline", out.getvalue().split("options:")[0])


class BaselineTests(unittest.TestCase):
    def test_baseline_is_sorted_triaged_and_well_formed(self):
        # Well formed and triaged only: every accepted finding has its reason.
        # It is never required to match the current tree (that would be a gate,
        # which needs the owner, AGENTS.md E2).
        data = json.loads(read("tools/security_baseline.json"))
        findings = data["findings"]
        self.assertEqual(list(findings), sorted(findings))
        self.assertEqual([k for k in findings if not sl.KEY.match(k)], [])
        for k, entry in findings.items():
            with self.subTest(k):
                self.assertEqual(set(entry), {"count", "why"})
                self.assertIsInstance(entry["count"], int)
                self.assertGreater(entry["count"], 0)
                self.assertGreater(len(entry["why"].strip()), 20)

    def test_the_required_ruff_check_does_not_select_the_s_rules(self):
        # Report only: ruff.toml (the required "Ruff" check) keeps its own rules.
        config = read("ruff.toml")
        select = re.search(r"^select\s*=\s*\[([^\]]*)\]", config, re.MULTILINE).group(1)
        self.assertNotIn('"S"', select)
        self.assertNotIn("extend-select", config)

    def test_ci_runs_it_report_only_inside_the_quality_report_job(self):
        # The owner's decision of 28 Oct 2026 (#415): on every pull request, as a
        # step in the existing report-only job that already has the checkout and
        # ruff, so it adds no runner or install and no required check.
        workflow = read(".github/workflows/quality.yml")
        self.assertIn("  pull_request:\n", workflow[:workflow.index("\njobs:")])
        job = workflow[workflow.index("\n  quality-report:"):workflow.index("\n  type-check:")]
        self.assertIn("name: Quality report (report only)", job)
        self.assertIn("--require-hashes -r requirements/ci.txt", job)
        self.assertIn("run: python tools/security_lint.py\n", job)
        self.assertNotIn("security_lint.py --update", workflow)
        # Never in a workflow whose jobs are required checks.
        for name in ("ci.yml", "changelog.yml", "audit.yml", "codeql.yml"):
            with self.subTest(name):
                self.assertNotIn("security_lint", read(f".github/workflows/{name}"))


if __name__ == "__main__":
    unittest.main()
