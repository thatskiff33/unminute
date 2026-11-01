# Which tests a pull request runs (#500): tools/select_tests.py and its map,
# tools/test_map.json. The map covers every tracked file and every test module,
# and ties each test module to what it imports, so a new module without a
# mapping fails here. The selection only ever widens towards the full suite;
# the chunks it asks for run 1 to N; and ci.yml runs it from the base commit,
# with nothing from the pull request in a shell line. A push to main and the
# nightly run take everything (the gate's side is in tests/test_run_tests.py).
#   python tools/run_tests.py --only test_select_tests

import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import select_tests as st  # noqa: E402

CI = ROOT / ".github" / "workflows" / "ci.yml"

# A small map over a stand-in checkout (see stand_in()).
MAP = {
    "run_everything": ["core/**", "tools/run.py", "docs/build.json"],
    "always": ["test_always", "test_b.B2"],
    "only_always": ["docs/**", "*.md"],
    "tests": {"test_a": ["src/a.py", "ui/**"], "test_b": ["src/b.py"], "test_gone": ["src/g.py"]},
}


def stand_in(tmp, b_classes=("B1", "B2")):
    """A checkout with tests/test_a.py, test_b.py (classes b_classes) and
    test_always.py; test_gone.py is mapped but missing."""
    root = Path(tmp)
    (root / "tests").mkdir()
    for module, classes in (("test_a", ("A",)), ("test_b", b_classes), ("test_always", ("Scan",))):
        (root / "tests" / f"{module}.py").write_text(
            "import unittest\n" + "".join(f"\n\nclass {c}(unittest.TestCase):\n    pass\n"
                                          for c in classes))
    return root


class SelectTests(unittest.TestCase):
    """select() on a small map: narrow only for what the map knows."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = stand_in(tmp.name)

    def select(self, *paths):
        return st.select(list(paths), MAP, self.root)

    def test_nothing_changed_selects_nothing(self):
        self.assertEqual(self.select().mode, "none")

    def test_docs_only_runs_the_always_set(self):
        selection = self.select("docs/guide.md", "README.md")
        self.assertEqual(selection.mode, "some")
        self.assertEqual(selection.names, ["test_always", "test_b.B2"])

    def test_a_mapped_file_runs_its_tests_and_the_always_set(self):
        self.assertEqual(self.select("src/a.py").names, ["test_a", "test_always", "test_b.B2"])
        self.assertEqual(self.select("ui/deep/page.js", "docs/x.md").names,
                         ["test_a", "test_always", "test_b.B2"])

    def test_a_changed_test_module_runs_itself_and_covers_its_always_classes(self):
        self.assertEqual(self.select("tests/test_b.py").names, ["test_always", "test_b"])
        self.assertEqual(self.select("tests/test_a.py", "src/b.py").names,
                         ["test_a", "test_always", "test_b"])

    def test_shared_or_core_files_run_everything(self):
        for path in ("core/store.py", "core/deep/x.py", "tools/run.py", "docs/build.json"):
            with self.subTest(path=path):
                self.assertEqual(self.select("docs/x.md", path).mode, "all")

    def test_a_file_the_map_does_not_know_runs_everything(self):
        for path in ("src/new.py", "tools/run.pyc", "srca.py", "ui", "src/a.py.bak"):
            with self.subTest(path=path):
                self.assertEqual(self.select("src/a.py", path).mode, "all")

    def test_an_unusual_path_runs_everything(self):
        # git prints none of these for a real change (-z keeps names whole); a
        # crafted one never narrows the selection.
        for path in ("docs/../core/store.py", "docs/./x.md", "../x.md", "/docs/x.md",
                     "docs\\x.md", "docs/x.md\nsrc/a.py", "docs/\x00.md", "docs/\x1b.md",
                     "docs/\udcff.md", "docs/..", "."):
            with self.subTest(path=path):
                self.assertEqual(self.select(path).mode, "all")

    def test_a_removed_test_module_or_a_missing_mapped_one_runs_everything(self):
        self.assertEqual(self.select("tests/test_zq_removed.py").mode, "all")
        self.assertEqual(self.select("src/g.py").mode, "all")       # maps to test_gone

    def test_an_always_class_that_is_gone_widens_to_its_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = stand_in(tmp, b_classes=("B1",))
            self.assertEqual(st.select(["docs/x.md"], MAP, root).names, ["test_always", "test_b"])

    def test_reasons_show_paths_as_code(self):
        # The job summary is Markdown, and the paths come from the pull request.
        self.assertEqual(self.select("docs/a`b.md").reasons, ["``docs/a`b.md``: the always set"])
        self.assertEqual(self.select("src/zq.py").reasons, ["`src/zq.py` is not in the test map"])
        self.assertEqual(self.select("docs\\x.md").reasons,
                         ["an unusual path: `'docs\\\\x.md'`"])
        self.assertEqual(st.code("`x"), "`` `x ``")
        self.assertEqual(st.code("a``b"), "```a``b```")

    def test_a_malformed_map_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "map.json"
            for data in ({}, dict(MAP, always="test_a"), dict(MAP, tests={"test_a": "src/a.py"}),
                         dict(MAP, always=["test_a; rm -rf /"]), dict(MAP, tests={"a b": []})):
                with self.subTest(data=data):
                    bad.write_text(json.dumps(data))
                    with self.assertRaises(ValueError):
                        st.load_map(bad)

    def test_globs(self):
        self.assertTrue(st.matches(["docs/**"], "docs/a/b.md"))
        self.assertTrue(st.matches(["*.md"], "README.md"))
        self.assertFalse(st.matches(["*.md"], "docs/README.md"))
        self.assertFalse(st.matches(["src/*.py"], "src/x/y.py"))
        self.assertTrue(st.matches(["tests/test_?.py"], "tests/test_a.py"))
        self.assertFalse(st.matches(["a.b"], "axb"))                  # dots are literal
        self.assertFalse(st.matches([], "anything"))


class PlanTests(unittest.TestCase):
    """How many Windows runners a selection gets, and the job outputs."""

    DURATIONS = {"test_big.One": 100.0, "test_big.Two": 100.0, "test_big.Three": 100.0,
                 "test_small.Only": 5.0, "test_other.X": 495.0}       # 800 s: 100 s a chunk

    def count(self, mode, *names):
        return st.chunk_count(st.Selection(mode, names), self.DURATIONS)

    def test_chunks_follow_the_measured_time(self):
        self.assertEqual(self.count("none"), 0)
        self.assertEqual(self.count("all"), st.FULL_CHUNKS)
        self.assertEqual(self.count("some", "test_small"), 1)
        self.assertEqual(self.count("some", "test_small", "test_big.One"), 2)
        self.assertEqual(self.count("some", "test_big"), 3)
        # Never more runners than whole classes to deal out.
        self.assertEqual(self.count("some", "test_other"), 1)
        # A module the table does not know weighs UNKNOWN_MODULE_SECONDS.
        self.assertEqual(st.seconds(["test_zq_new"], self.DURATIONS), st.UNKNOWN_MODULE_SECONDS)
        self.assertEqual(st.seconds(["test_big.Zq"], self.DURATIONS), st.UNKNOWN_CLASS_SECONDS)
        self.assertEqual(st.seconds(["test_big", "test_small.Only"], self.DURATIONS), 305.0)

    def test_chunks_are_capped_at_the_full_suite(self):
        many = {f"test_m{i}.C": 1000.0 for i in range(20)}
        selection = st.Selection("some", [f"test_m{i}" for i in range(20)])
        self.assertEqual(st.chunk_count(selection, many), st.FULL_CHUNKS)

    def test_the_outputs_number_chunks_one_to_n_for_every_selection(self):
        cases = [st.Selection("none"), st.Selection("all"), st.Selection("some", ["test_small"]),
                 st.Selection("some", ["test_big", "test_other", "test_small.Only"])]
        for selection in cases:
            with self.subTest(selection=selection):
                out = st.outputs(selection, self.DURATIONS)
                chunks = json.loads(out["chunks"])
                self.assertEqual(chunks, list(range(1, len(chunks) + 1)))
                self.assertEqual(out["mode"], selection.mode)
                self.assertEqual(out["names"], " ".join(selection.names)
                                 if selection.mode == "some" else "")
        self.assertEqual(json.loads(st.outputs(st.Selection("all"), {})["chunks"]),
                         list(range(1, st.FULL_CHUNKS + 1)))
        self.assertEqual(st.outputs(st.Selection("none"), {})["chunks"], "[]")

    def test_a_name_that_is_not_a_test_is_never_handed_on(self):
        for bad in ("test_a b", "test_a;x", "$(x)", "test_a.B.c", "-x", "test_a\n"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    st.outputs(st.Selection("some", [bad]), {})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                st.write_outputs(Path(tmp) / "out", {"names": "a\nmode=none"})


class MapTests(unittest.TestCase):
    """The committed map against this checkout."""

    @classmethod
    def setUpClass(cls):
        cls.map = st.load_map()
        cls.durations = st.load_durations()

    def test_the_map_covers_every_file_and_test_module(self):
        # Every tracked file is classified, every test module is mapped or
        # always runs, every name and glob is live, and each test module's own
        # imports of repository code map to it. Fix the map, not the test:
        # python tools/select_tests.py --check lists the same.
        self.assertEqual(st.problems(ROOT, self.map), [])

    def test_a_gap_in_a_map_is_reported(self):
        # A new test module, a new source file, a new import, files a test
        # names (a join, a page's script path; not a docstring), and a shared
        # workspace file that imports one that is not shared, none mapped.
        with tempfile.TemporaryDirectory() as tmp:
            root = stand_in(tmp)
            (root / "src").mkdir()
            (root / "workspace").mkdir()
            for name in ("a.py", "b.py", "c.py", "d.js", "e.py", "new.py"):
                (root / "src" / name).write_text("")
            (root / "workspace" / "core.py").write_text("from workspace import helper\n")
            (root / "workspace" / "helper.py").write_text("")
            (root / "tests" / "test_a.py").write_text(
                '"""Not a read: src/e.py."""\n'
                "from src import a, c\n"
                'X = ROOT / "src" / "b.py"\n'
                'Y = ROOT / "docs" / "build.json"\n'
                "PAGE = '<script src=\"/src/d.js?v=1\"></script>'\n")
            (root / "tests" / "test_new.py").write_text("")
            files = ["src/a.py", "src/b.py", "src/c.py", "src/d.js", "src/e.py", "src/new.py",
                     "core/x.py", "tools/run.py", "docs/build.json", "README.md", "ui/x.js",
                     "workspace/core.py", "workspace/helper.py", "tests/test_a.py",
                     "tests/test_b.py", "tests/test_always.py", "tests/test_new.py"]
            test_map = dict(MAP, only_always=MAP["only_always"] + ["docs/zq/*"],
                            run_everything=MAP["run_everything"] + ["workspace/core.py"],
                            tests=dict(MAP["tests"], test_b=["src/b.py", "src/d.js", "src/e.py",
                                                             "workspace/helper.py"]))
            found = st.problems(root, test_map, files)
        self.assertEqual(sorted(found), sorted([
            "tests/test_new.py is not in the map's tests (or always)",
            "the map names test_gone, which is not in tests/",
            "src/new.py is not in the map",
            "src/c.py is not in the map",
            "the map's glob docs/zq/* matches no tracked file",
            "the map's glob src/g.py matches no tracked file",
            "tests/test_a.py imports src/c.py, which the map does not tie to test_a",
            "tests/test_a.py names src/b.py, which the map does not tie to test_a",
            "tests/test_a.py names src/d.js, which the map does not tie to test_a",
            "workspace/core.py is shared and imports workspace/helper.py, which is not",
        ]))

    def test_the_always_set_stays_quick(self):
        # A docs-only pull request runs only the always set, on one runner.
        self.assertLess(st.seconds(self.map["always"], self.durations), 60)

    def test_the_map_names_only_real_tests_and_keeps_its_own_test_always(self):
        self.assertIn("test_select_tests", self.map["always"])
        for name in self.map["always"] + list(self.map["tests"]):
            self.assertEqual(st.resolve_name(ROOT, name), name)

    def test_shared_files_run_everything(self):
        for path in ("tools/run_tests.py", "tools/select_tests.py", "tools/test_map.json",
                     "tools/test_durations.json", "tests/page_scripts.py", "tests/page_vm.js",
                     "tests/download_guard.py", "requirements.txt", "requirements.in",
                     # The page tests' fake bridge and frames, a test helper (#503 review).
                     "tools/design_check/fixtures.js", "tools/design_check/frame.html",
                     "tools/design_check/render.html", "tools/design_check/render.js",
                     # What the shared store imports (#503 review).
                     "workspace/location.py", "workspace/diag.py", "workspace/update.py",
                     "requirements/ci.txt", ".github/workflows/ci.yml",
                     ".github/workflows/quality.yml", "workspace/store.py",
                     "workspace/__init__.py"):
            with self.subTest(path=path):
                self.assertEqual(st.select([path, "docs/RELEASING.md"], self.map, ROOT).mode,
                                 "all")

    def test_a_docs_only_change_runs_the_docs_and_consistency_tests_on_one_runner(self):
        selection = st.select(["docs/WORKSPACE_DESIGN.md", "changelog.d/zq-note.md"],
                              self.map, ROOT)
        self.assertEqual(selection.mode, "some")
        self.assertIn("test_consistency", selection.names)
        self.assertEqual(st.chunk_count(selection, self.durations), 1)

    def test_each_test_module_runs_when_it_changes(self):
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            with self.subTest(module=path.stem):
                selection = st.select([f"tests/{path.name}"], self.map, ROOT)
                self.assertEqual(selection.mode, "some")
                self.assertIn(path.stem, selection.names)

    def test_a_doc_change_runs_the_tests_that_read_it(self):
        # tests/test_perf_check.py reads docs/RELEASING.md (#503 review).
        selection = st.select(["docs/RELEASING.md"], self.map, ROOT)
        self.assertEqual(selection.mode, "some")
        self.assertIn("test_perf_check", selection.names)

    def test_a_module_change_runs_the_tests_that_import_it(self):
        selection = st.select(["workspace/echo.py"], self.map, ROOT)
        self.assertEqual(selection.mode, "some")
        self.assertIn("test_two_track", selection.names)        # it imports workspace.echo
        self.assertNotIn("test_licence_texts", selection.names)


class CommandLineTests(unittest.TestCase):
    """--changed reads git's own list; the outputs go to $GITHUB_OUTPUT."""

    def git(self, root, *args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                              check=True, timeout=60).stdout.strip()

    def test_changed_paths_are_both_sides_of_a_rename_and_kept_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.git(root, "init", "-q")
            (root / "docs").mkdir()
            (root / "a.txt").write_text("a\n")
            (root / "b.txt").write_text("the same text, renamed\n" * 20)
            (root / "with space.md").write_text("x\n")
            (root / "docs" / "café.md").write_text("x\n")
            self.git(root, "add", "-A")
            base = self.git(root, "write-tree")
            (root / "a.txt").write_text("changed\n")
            (root / "b.txt").rename(root / "c.txt")
            (root / "with space.md").write_text("y\n")
            (root / "docs" / "café.md").write_text("y\n")
            self.git(root, "add", "-A")
            head = self.git(root, "write-tree")
            paths = st.changed_paths(root, base, head)
        self.assertEqual(sorted(paths), ["a.txt", "b.txt", "c.txt", "docs/café.md",
                                         "with space.md"])

    def run_main(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            summary = Path(tmp) / "summary"
            with contextlib.redirect_stdout(io.StringIO()):
                code = st.main([*args, "--root", str(ROOT), "--github-output", str(out),
                                "--summary", str(summary)])
            return code, out.read_text(encoding="utf-8"), summary.read_text(encoding="utf-8")

    def test_all_and_paths_write_the_outputs(self):
        code, out, summary = self.run_main("--all")
        self.assertEqual(code, 0)
        chunks = json.dumps(list(range(1, st.FULL_CHUNKS + 1)))
        self.assertEqual(out, f"mode=all\nnames=\nchunks={chunks}\n")
        self.assertIn("Test selection: **all**", summary)
        code, out, summary = self.run_main("--paths", "docs/RELEASING.md")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"\Amode=some\nnames=test_\w+[^\n]*\nchunks=\[1\]\n\Z")
        self.assertIn("docs/RELEASING.md", summary)


class WorkflowTests(unittest.TestCase):
    """ci.yml runs the selection safely and follows it."""

    @classmethod
    def setUpClass(cls):
        cls.text = CI.read_text(encoding="utf-8")
        blocks = re.split(r"^  ([\w-]+):\s*$", cls.text[cls.text.index("\njobs:"):],
                          flags=re.MULTILINE)
        cls.jobs = dict(zip(blocks[1::2], blocks[2::2], strict=True))

    @staticmethod
    def run_blocks(text):
        """The text of every run: step (one line or a block)."""
        lines = text.splitlines()
        found = []
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", line)
            if not m:
                continue
            if m[2] and m[2][0] not in "|>":
                found.append(m[2])
                continue
            indent = len(m[1])
            body = []
            for nxt in lines[i + 1:]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                body.append(nxt)
            found.append("\n".join(body))
        return found

    def test_no_expression_is_written_into_a_shell_line(self):
        # Values reach the shell through env: only, never ${{ }} in run: (#500 review).
        blocks = self.run_blocks(self.text)
        self.assertGreater(len(blocks), 10)
        for block in blocks:
            self.assertNotIn("${{", block)

    def test_the_triggers_and_the_nightly_run(self):
        on = re.search(r"(?ms)^on:\n(.*?)^\S", self.text).group(1)
        self.assertEqual(["pull_request", "push", "schedule"],
                         re.findall(r"^  (\w+):", on, re.MULTILINE))
        self.assertRegex(on, r'\n  schedule:\n    - cron: "\d+ \d+ \* \* \*"\n')
        self.assertIn("\npermissions: {}\n", self.text)

    def test_the_selection_runs_from_the_base_commit_with_read_only_access(self):
        job = self.jobs["select-tests"]
        self.assertIn("    name: Select tests\n", job)
        self.assertIn("    permissions:\n      contents: read\n    outputs:", job)
        self.assertIn("persist-credentials: false", job)
        self.assertIn("fetch-depth: 2", job)
        self.assertIn("EVENT: ${{ github.event_name }}", job)
        self.assertIn("trusted='HEAD^1'", job)
        self.assertIn('git show "$trusted:tools/$file" > "$selector/$file"', job)
        self.assertIn("for file in select_tests.py test_map.json test_durations.json; do", job)
        self.assertIn('python3 -I "$selector/select_tests.py" --root . --changed \'HEAD^1\' HEAD',
                      job)
        self.assertIn('python3 -I "$selector/select_tests.py" --root . --all', job)
        # Only the files the selector reads come from the base.
        self.assertNotIn("python3 tools/", job)
        self.assertNotIn("cp tools/", job)

    def test_a_base_without_the_selector_runs_everything(self):
        job = self.jobs["select-tests"]
        chunks = json.dumps(list(range(1, st.FULL_CHUNKS + 1)))
        self.assertIn(f'{{ echo "mode=all"; echo "names="; echo "chunks={chunks}"; }} '
                      '>> "$GITHUB_OUTPUT"', job)

    def test_a_failing_base_selector_runs_everything_on_a_pull_request(self):
        # A broken selector on main would otherwise fail every pull request,
        # its own fix included (#503 review). Running everything is never
        # narrower, and the failed run's partial outputs are never used.
        job = self.jobs["select-tests"]
        self.assertIn('--github-output "$picked" --summary "$notes"; then', job)
        self.assertIn('cat "$picked" >> "$GITHUB_OUTPUT"', job)
        warning = "::warning::The base commit's test selector failed: every test runs."
        self.assertIn(warning, job)
        self.assertRegex(job[job.index(warning):], r"\A[^\n]*\n\s*everything\n")
        # A push to main or the nightly run still fails loudly.
        self.assertIn('python3 -I "$selector/select_tests.py" --root . --all \\\n'
                      '              --github-output "$GITHUB_OUTPUT"', job)

    def test_the_syntax_check_follows_the_selection_and_always_reports(self):
        job = self.jobs["check"]
        self.assertIn("    needs: select-tests\n", job)
        self.assertIn("    if: ${{ always() }}\n", job)
        self.assertIn("SELECTION: ${{ needs.select-tests.result }}", job)
        self.assertIn('if [ "$SELECTION" != success ]; then', job)
        self.assertIn("MODE: ${{ needs.select-tests.outputs.mode }}", job)
        self.assertIn("SELECTED: ${{ needs.select-tests.outputs.names }}", job)
        self.assertIn('read -r -a names <<< "$SELECTED"', job)
        self.assertIn('python tools/run_tests.py --only "${names[@]}"', job)
        self.assertIn("all) python tools/run_tests.py ;;", job)
        self.assertIn('*) echo "Unknown test selection."; exit 1 ;;', job)


if __name__ == "__main__":
    unittest.main()
