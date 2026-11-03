# The report-only type check of the page's scripts (#489): tools/ts_check.py
# reads tsc's output, compares it with the committed baseline and never fails
# on what tsc finds; TypeScript is a pinned, hash-locked development tool that
# the Quality workflow installs without install scripts and never ships.
# Node.js and TypeScript are not needed here: tsc's output is synthetic.
#   python tools/run_tests.py --only test_ts_check

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


ts = load("ts_check")


def read(path):
    return (ROOT / path).read_text(encoding="utf-8").replace("\r\n", "\n")


OUTPUT = (
    "workspace/ui/confirm.js(19,16): error TS2339: Property 'checked' does not exist on "
    "type 'HTMLElement'.\n"
    "workspace\\ui\\navigation.js(72,77): error TS2345: Argument of type 'string | number' "
    "is not assignable to parameter of type 'string'.\n"
    "  Type 'number' is not assignable to type 'string'.\n"
    "workspace/ui/core.js(40,3): error TS2451: Cannot redeclare 'x', declared on line 12.\n"
    "workspace/ui/confirm.js(68,7): error TS2339: Property 'returnValue' does not exist on "
    "type 'HTMLElement'.\n"
    "workspace/ui/confirm.js(70,13): error TS2339: Property 'returnValue' does not exist on "
    "type 'HTMLElement'.\n")
SETTINGS_PROBLEMS = (
    "error TS18003: No inputs were found in config file 'tsconfig.json'.\n",
    "tsconfig.json(3,5): error TS5023: Unknown compiler option 'bogus'.\n",
    "error TS5058: The specified path does not exist: 'tsconfig.json'.\n",
)


class ReadOutputTests(unittest.TestCase):
    def test_errors_are_keyed_without_line_or_column(self):
        found, problems = ts.errors(OUTPUT)
        self.assertEqual(problems, [])
        self.assertEqual([(p, n, c) for p, n, c, _ in found],
                         [("workspace/ui/confirm.js", 19, 16),
                          ("workspace/ui/navigation.js", 72, 77),
                          ("workspace/ui/core.js", 40, 3),
                          ("workspace/ui/confirm.js", 68, 7),
                          ("workspace/ui/confirm.js", 70, 13)])
        self.assertEqual(found[0][3], "workspace/ui/confirm.js: error TS2339: Property 'checked' "
                                      "does not exist on type 'HTMLElement'.")
        # A message's further lines are not part of its key.
        self.assertTrue(found[1][3].endswith("parameter of type 'string'."))
        # A line number inside the message moves with the code too.
        self.assertIn("declared on line N.", found[2][3])

    def test_a_problem_with_the_settings_is_not_a_code_error(self):
        for text in SETTINGS_PROBLEMS:
            with self.subTest(text=text):
                found, problems = ts.errors(text)
                self.assertEqual(found, [])
                self.assertEqual(problems, [text.strip()])

    def test_an_opt_in_is_a_comment_before_the_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "a.js").write_text('// A.\n// @ts-check: notes.\n"use strict";\n',
                                         encoding="utf-8")
            (folder / "b.js").write_text('// B.\n"use strict";\n// @ts-check\n',
                                         encoding="utf-8")
            (folder / "c.js").write_text('"use strict";\nconst s = "@ts-check";\n',
                                         encoding="utf-8")
            (folder / "d.js").write_text('// @ts-checked\n"use strict";\n', encoding="utf-8")
            self.assertEqual(ts.opted_in(folder), ["a.js"])


class CompareTests(unittest.TestCase):
    def test_counted_against_the_baseline(self):
        found, _ = ts.errors(OUTPUT)
        keys = [k for *_, k in found]
        new, fixed = ts.compare(found, keys[:4] + ["workspace/ui/gone.js: error TS1: old."])
        # The second copy of an accepted error is new; one no longer found is fixed.
        self.assertEqual([(p, n) for p, n, _, _ in new], [("workspace/ui/confirm.js", 70)])
        self.assertEqual(fixed, ["workspace/ui/gone.js: error TS1: old."])

    def test_the_same_errors_report_nothing_new(self):
        found, _ = ts.errors(OUTPUT)
        self.assertEqual(ts.compare(found, [k for *_, k in reversed(found)]), ([], []))


class ReportOnlyTests(unittest.TestCase):
    """New errors are reported, never failed; only a tsc that cannot run fails."""

    BASELINE_TEXT = ts.HEADER + "workspace/ui/a.js: error TS2339: kept.\n"

    def run_main(self, stdout, returncode, env, argv=(), node="node", tsc=True):
        result = mock.Mock(returncode=returncode, stdout=stdout, stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "baseline.txt"
            baseline.write_text(self.BASELINE_TEXT, encoding="utf-8")
            tsc_path = Path(tmp) / "tsc"
            if tsc:
                tsc_path.write_text("", encoding="utf-8")
            summary = Path(tmp) / "summary.md"
            env = {k: v.replace("{summary}", str(summary)) for k, v in env.items()}
            out = io.StringIO()
            # Whether Node.js and TypeScript are installed is decided here, not
            # by the machine running the test.
            with mock.patch.object(ts, "BASELINE", baseline), \
                    mock.patch.object(ts, "TSC", tsc_path), \
                    mock.patch.object(ts.shutil, "which", return_value=node), \
                    mock.patch.object(ts.subprocess, "run", return_value=result) as run, \
                    mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
                try:
                    code = ts.main(list(argv))
                except SystemExit as exc:
                    code = exc.code
            self.baseline_after = baseline.read_text(encoding="utf-8")
            self.ran = run.called
            self.command = run.call_args[0][0] if run.called else None
            return code, out.getvalue(), summary.read_text(encoding="utf-8") \
                if summary.exists() else ""

    def test_new_errors_are_warnings_on_github_and_pass(self):
        code, out, summary = self.run_main(OUTPUT, 2, {"GITHUB_ACTIONS": "true",
                                                       "GITHUB_STEP_SUMMARY": "{summary}"})
        self.assertEqual(code, 0)
        self.assertIn("new since the baseline: 5", out)
        self.assertIn("fixed since the baseline: 1", out)
        self.assertIn("::warning file=workspace/ui/navigation.js,line=72,col=77,"
                      "title=TypeScript (report only)::", out)
        self.assertIn("new since the baseline: 5", summary)
        # tsc runs on Node.js directly, from the pinned package, writing nothing.
        self.assertEqual(self.command[0], "node")
        self.assertIn("--noEmit", self.command)
        self.assertEqual(self.command[self.command.index("--project") + 1], str(ts.CONFIG))

    def test_a_clean_run_passes(self):
        code, out, _ = self.run_main("", 0, {"GITHUB_ACTIONS": "false"})
        self.assertEqual(code, 0)
        self.assertIn("TypeScript errors: 0 (baseline 1)", out)

    def test_a_tsc_that_cannot_run_fails_and_keeps_the_baseline(self):
        # A settings problem, or a failing exit with no error placed in the
        # code, is not a run: otherwise every baseline error would look fixed,
        # and --update would empty the baseline.
        cases = [(text, 1) for text in SETTINGS_PROBLEMS]
        cases += [(OUTPUT + SETTINGS_PROBLEMS[0], 2), ("", 1), ("", 2), (OUTPUT, 3)]
        for argv in ((), ("--update",)):
            for stdout, returncode in cases:
                with self.subTest(argv=argv, stdout=stdout, returncode=returncode):
                    code, out, _ = self.run_main(stdout, returncode,
                                                 {"GITHUB_ACTIONS": "false"}, argv=argv)
                    self.assertEqual(code, 2)
                    self.assertNotIn("fixed since the baseline", out)
                    self.assertEqual(self.baseline_after, self.BASELINE_TEXT)

    def test_no_node_or_no_typescript_fails_before_running(self):
        for node, tsc in ((None, True), ("node", False)):
            with self.subTest(node=node, tsc=tsc):
                code, _, _ = self.run_main(OUTPUT, 2, {"GITHUB_ACTIONS": "false"},
                                           argv=("--update",), node=node, tsc=tsc)
                self.assertEqual(code, 2)
                self.assertFalse(self.ran)
                self.assertEqual(self.baseline_after, self.BASELINE_TEXT)

    def test_update_rewrites_the_baseline_when_tsc_ran(self):
        code, out, _ = self.run_main(OUTPUT, 2, {"GITHUB_ACTIONS": "false"}, argv=("--update",))
        self.assertEqual(code, 0)
        self.assertIn("Wrote 5 error(s)", out)
        lines = [x for x in self.baseline_after.splitlines() if not x.startswith("#")]
        self.assertEqual(lines, sorted(k for *_, k in ts.errors(OUTPUT)[0]))

    def test_help_has_a_description(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), self.assertRaises(SystemExit):
            ts.main(["--help"])
        self.assertIn("baseline", out.getvalue().split("options:")[0])


class BaselineAndSettingsTests(unittest.TestCase):
    def test_baseline_is_sorted_well_formed_and_only_for_checked_scripts(self):
        lines = ts.read_baseline()
        self.assertEqual(lines, sorted(lines))
        line = re.compile(r"^workspace/ui/[\w-]+\.js: error TS\d+: .+$")
        self.assertEqual([x for x in lines if not line.match(x)], [])
        self.assertNotRegex("\n".join(lines), r"on line \d")
        checked = set(ts.opted_in())
        self.assertEqual({x.split(":", 1)[0].rsplit("/", 1)[1] for x in lines} - checked, set())

    def test_scripts_opt_in_and_the_rest_are_still_read(self):
        config = json.loads(read("tsconfig.json"))
        options = config["compilerOptions"]
        self.assertIs(options["allowJs"], True)
        self.assertIs(options["checkJs"], False)          # opt in per script
        self.assertIs(options["noEmit"], True)            # tsc writes nothing
        self.assertIn("DOM", options["lib"])
        self.assertEqual(options["types"], [])            # no @types package is read
        self.assertEqual(config["include"], ["workspace/ui/*.js", "tools/page_globals.d.ts"])
        self.assertNotIn("files", config)
        self.assertNotIn("references", config)
        self.assertTrue(ts.opted_in(), "at least one script opts in")
        # Each opt-in is a line comment before the strict directive, so the
        # script runs as before.
        for name in ts.opted_in():
            head = read(f"workspace/ui/{name}").split('"use strict";', 1)[0]
            self.assertEqual([x for x in head.splitlines() if not x.startswith("//")], [],
                             name)

    def test_nothing_for_the_check_ships(self):
        # Only workspace/ and APP_FILES ship (tools/build_package.py); the
        # declarations, settings and npm files stay outside workspace/.
        allowlist = json.loads(read("packaging/app-allowlist.json"))["app"]
        for name in ("package.json", "package-lock.json", ".npmrc", "tsconfig.json",
                     "tools/page_globals.d.ts"):
            self.assertNotIn(name, allowlist)
        self.assertEqual([p for p in allowlist if p.endswith(".ts")], [])
        self.assertIn("\nnode_modules/\n", read(".gitignore"))


class PinnedToolTests(unittest.TestCase):
    """TypeScript is pinned by exact version and integrity hash, has no other
    package, and is installed with no install scripts."""

    def test_one_exact_pin_and_no_scripts(self):
        package = json.loads(read("package.json"))
        self.assertIs(package["private"], True)            # never published
        self.assertNotIn("scripts", package)
        self.assertNotIn("dependencies", package)
        self.assertEqual(list(package["devDependencies"]), ["typescript"])
        self.assertRegex(package["devDependencies"]["typescript"], r"^\d+\.\d+\.\d+$")

    def test_the_lock_holds_that_version_with_its_hash(self):
        version = json.loads(read("package.json"))["devDependencies"]["typescript"]
        lock = json.loads(read("package-lock.json"))
        self.assertEqual(lock["lockfileVersion"], 3)
        self.assertEqual(sorted(lock["packages"]), ["", "node_modules/typescript"])
        entry = lock["packages"]["node_modules/typescript"]
        self.assertEqual(entry["version"], version)
        self.assertEqual(entry["resolved"],
                         f"https://registry.npmjs.org/typescript/-/typescript-{version}.tgz")
        self.assertRegex(entry["integrity"], r"^sha512-[A-Za-z0-9+/]{86}==$")
        self.assertEqual(entry["license"], "Apache-2.0")
        self.assertIs(entry["dev"], True)
        self.assertNotIn("hasInstallScript", entry)
        self.assertNotIn("dependencies", entry)

    def test_npm_runs_no_scripts_and_makes_no_other_calls(self):
        settings = dict(line.split("=", 1) for line in read(".npmrc").splitlines()
                        if line and not line.startswith("#"))
        for key in ("audit", "fund", "update-notifier"):
            self.assertEqual(settings[key], "false", key)
        self.assertEqual(settings["ignore-scripts"], "true")
        self.assertNotIn("registry", settings)              # the public registry, as locked

    def test_the_quality_workflow_installs_from_the_lock_and_reports(self):
        workflow = read(".github/workflows/quality.yml")
        job = workflow[workflow.index("\n  type-check:"):workflow.index("\n  coverage:")]
        self.assertIn("name: Type check (report only)", job)
        self.assertIn("npm ci --ignore-scripts --no-audit --no-fund\n", job)
        self.assertIn("run: python tools/ts_check.py\n", job)
        self.assertLess(job.index("run: python tools/type_check.py"),
                        job.index("run: python tools/ts_check.py"))
        # The runner image's Node.js: no new action, and no npm install or npx.
        self.assertNotIn("setup-node", workflow)
        self.assertNotRegex(job, r"\bnpm (?:install|i)\b|\bnpx\b")
        self.assertNotIn("continue-on-error", job)
        self.assertNotIn("secrets.", job)
        self.assertIn("    permissions:\n      contents: read\n    steps:", job)

    def test_dependabot_watches_the_npm_tool(self):
        text = read(".github/dependabot.yml")
        block = text[text.index("  - package-ecosystem: npm\n"):]
        self.assertIn("    directory: /\n", block)
        self.assertIn("      interval: monthly\n", block)
        self.assertIn("      default-days: 7\n", block)
        # A new major version (TypeScript 6 or the native 7) is its own pull request.
        self.assertIn('update-types: ["version-update:semver-major"]', block)


class AnnotationEscapeTests(unittest.TestCase):
    def test_a_property_escapes_its_separators(self):
        # #536 review: a file name with ":" or "," must not end the property.
        self.assertEqual(ts.escape_property("a:b,c%d\n"), "a%3Ab%2Cc%25d%0A")
        self.assertEqual(ts.escape_property("workspace/ui/core.js"), "workspace/ui/core.js")


if __name__ == "__main__":
    unittest.main()
