# The test runner writes no bytecode into the checkout and fails when a test,
# or a process a test started, left some behind (#206). tools/evidence.py
# refuses a checkout with ignored files such as __pycache__ in it. It also
# splits the suite into chunks for the Windows CI job (#386): every test runs in
# exactly one chunk, and the required "Unit tests (Windows)" job passes only
# when every chunk did.
#   python tools/run_tests.py

import argparse
import contextlib
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_tests  # noqa: E402

# Runs the runner's main() against a stand-in checkout (argv[1]).
RUN = textwrap.dedent("""
    import sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[2])
    import run_tests
    run_tests.ROOT = Path(sys.argv[1])
    sys.argv = ["run_tests.py"] + sys.argv[3:]
    sys.exit(run_tests.main())
""")


class BytecodeScanTests(unittest.TestCase):
    def test_new_and_changed_files_are_reported_and_the_rest_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg" / "__pycache__").mkdir(parents=True)
            kept = root / "pkg" / "__pycache__" / "kept.cpython-314.pyc"
            changed = root / "pkg" / "__pycache__" / "changed.cpython-314.pyc"
            kept.write_bytes(b"a")
            changed.write_bytes(b"b")
            before = run_tests.bytecode(root)
            changed.write_bytes(b"bb")
            (root / "pkg" / "__pycache__" / "new.cpython-314.pyc").write_bytes(b"c")
            for skipped in (".git", ".venv", "dist", "extracted"):
                (root / skipped).mkdir()
                (root / skipped / "x.pyc").write_bytes(b"d")
            stray = run_tests.written(before, run_tests.bytecode(root))
            self.assertEqual(stray, [os.path.join("pkg", "__pycache__", "changed.cpython-314.pyc"),
                                     os.path.join("pkg", "__pycache__", "new.cpython-314.pyc")])


class RunnerTests(unittest.TestCase):
    """The runner itself, in a child process, on a stand-in checkout."""

    def run_runner(self, root, *args):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
        # -B only, as a person starts it: -B does not reach the runner's children.
        return subprocess.run([sys.executable, "-B", "-c", RUN, str(root), str(ROOT / "tools"),
                               *args],
                              env=env, capture_output=True, text=True, timeout=120)

    def checkout(self, tmp, test_body):
        root = Path(tmp)
        (root / "tests").mkdir()
        (root / "zqmodule.py").write_text("VALUE = 1\n")
        (root / "tests" / "test_stand_in.py").write_text(textwrap.dedent("""
            import subprocess, sys, unittest
            from pathlib import Path
            ROOT = Path(__file__).resolve().parents[1]

            class StandIn(unittest.TestCase):
                def test_it(self):
            """) + textwrap.indent(textwrap.dedent(test_body), " " * 8))
        return root

    def test_a_child_started_without_b_writes_no_bytecode(self):
        # A bare sys.executable, as several tests start one: -B is not passed on.
        with tempfile.TemporaryDirectory() as tmp:
            root = self.checkout(tmp, """
                subprocess.run([sys.executable, "-c", "import zqmodule"], cwd=ROOT, check=True)
            """)
            r = self.run_runner(root)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(run_tests.bytecode(root), {})

    def test_bytecode_left_in_the_checkout_fails_the_run_and_is_kept(self):
        # As the GPU worker did when it turned -B off for every module.
        with tempfile.TemporaryDirectory() as tmp:
            root = self.checkout(tmp, """
                import py_compile
                py_compile.compile(str(ROOT / "zqmodule.py"), doraise=True)
            """)
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "old.cpython-314.pyc").write_bytes(b"x")   # before the run
            r = self.run_runner(root)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("1 bytecode file(s) written into the checkout", r.stdout)
            self.assertIn(os.path.join("__pycache__", "zqmodule."), r.stdout)
            self.assertNotIn("old.cpython", r.stdout)
            self.assertEqual(len(run_tests.bytecode(root)), 2)         # nothing deleted

    def test_each_chunk_runs_its_own_classes_and_applies_strict_skips_alone(self):
        # Three classes the table does not know: Alpha and Gamma go to chunk 1,
        # Beta to chunk 2. Only Gamma skips for a reason not allowed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_zq_chunks.py").write_text(textwrap.dedent("""
                import unittest

                class Alpha(unittest.TestCase):
                    def test_one(self): pass
                    def test_two(self): pass

                class Beta(unittest.TestCase):
                    def test_one(self): pass

                class Gamma(unittest.TestCase):
                    def test_one(self): self.skipTest("not allowed")
            """))
            first = self.run_runner(root, "--strict-skips", "--chunk", "1/2")
            second = self.run_runner(root, "--strict-skips", "--chunk", "2/2")
        self.assertIn("Chunk 1 of 2: 3 of the suite's 4 tests, in 2 classes.", first.stdout)
        self.assertIn("Chunk 2 of 2: 1 of the suite's 4 tests, in 1 classes.", second.stdout)
        self.assertIn("Chunk 1 of 2 ran 3 test(s).", first.stdout)
        self.assertIn("Chunk 2 of 2 ran 1 test(s).", second.stdout)
        self.assertIn("Gamma", first.stdout)
        self.assertNotIn("Gamma", second.stdout + second.stderr)
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)   # the skip
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        times = [line for line in second.stdout.splitlines()
                 if line.startswith(run_tests.TIMES_PREFIX)]
        self.assertEqual(len(times), 1)
        self.assertEqual(list(json.loads(times[0][len(run_tests.TIMES_PREFIX):])),
                         ["test_zq_chunks.Beta"])

    def test_only_runs_the_named_modules_classes_and_tests_with_strict_skips(self):
        # 26 Oct 2026: the quick local run for what a change touches (--only).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_zq_only.py").write_text(textwrap.dedent("""
                import unittest

                class Alpha(unittest.TestCase):
                    def test_one(self): pass
                    def test_two(self): pass

                class Beta(unittest.TestCase):
                    def test_one(self): self.skipTest("not allowed")
            """))
            (root / "tests" / "test_zq_other.py").write_text(textwrap.dedent("""
                import unittest

                class Gamma(unittest.TestCase):
                    def test_one(self): self.fail("must not run")
            """))
            alpha = self.run_runner(root, "--strict-skips", "--only", "test_zq_only.Alpha")
            one = self.run_runner(root, "--only", "test_zq_only.Alpha.test_two")
            module = self.run_runner(root, "--strict-skips", "--only", "test_zq_only")
            prefix = self.run_runner(root, "--only", "test_zq_on")
            both = self.run_runner(root, "--only", "test_zq_only", "--chunk", "1/2")
        self.assertEqual(alpha.returncode, 0, alpha.stdout + alpha.stderr)
        self.assertIn("Only test_zq_only.Alpha: 2 of the suite's 4 tests.", alpha.stdout)
        self.assertNotIn("Gamma", alpha.stdout + alpha.stderr)
        self.assertEqual(one.returncode, 0, one.stdout + one.stderr)
        self.assertIn("1 of the suite's 4 tests", one.stdout)
        self.assertEqual(module.returncode, 1, module.stdout + module.stderr)   # Beta's skip
        self.assertIn("3 of the suite's 4 tests", module.stdout)
        self.assertEqual(prefix.returncode, 1, prefix.stdout + prefix.stderr)   # whole names only
        self.assertIn("No test matched; nothing was run.", prefix.stdout)
        self.assertNotEqual(both.returncode, 0)
        self.assertIn("cannot be used together", both.stderr)

    def test_only_still_runs_and_fails_a_named_module_that_does_not_import(self):
        # Review of #408: unittest names an import failure unittest.loader._FailedTest.<module>.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_zq_broken.py").write_text("import zq_no_such_module" + chr(10))
            (root / "tests" / "test_zq_ok.py").write_text(textwrap.dedent("""
                import unittest

                class Fine(unittest.TestCase):
                    def test_one(self): pass
            """))
            alone = self.run_runner(root, "--only", "test_zq_broken")
            both = self.run_runner(root, "--only", "test_zq_broken", "test_zq_ok")
            other = self.run_runner(root, "--only", "test_zq_ok")
        for r in (alone, both):
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("zq_no_such_module", r.stdout + r.stderr)
            self.assertNotIn("No test matched", r.stdout)
        self.assertIn("2 of the suite's 2 tests", both.stdout)
        self.assertEqual(other.returncode, 0, other.stdout + other.stderr)   # not named, not run

    def test_class_times_from_chunk_logs_become_the_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = []
            for n, times in enumerate(({"m.A": 2.5}, {"m.B": 1.0})):
                log = Path(tmp) / f"chunk-{n}.log"
                log.write_text("2026-10-26T00:00:00.0Z noise\n2026-10-26T00:00:01.0Z "
                               + run_tests.TIMES_PREFIX + json.dumps(times) + "\n")
                logs.append(str(log))
            table = Path(tmp) / "table.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_tests.update_durations(logs, table), 0)
                self.assertEqual(run_tests.load_durations(table), {"m.A": 2.5, "m.B": 1.0})
                empty = Path(tmp) / "empty.log"
                empty.write_text("nothing\n")
                self.assertEqual(run_tests.update_durations([str(empty)], table), 1)


def the_suite():
    """The real suite, as tools/run_tests.py discovers it."""
    return unittest.defaultTestLoader.discover(str(ROOT / "tests"))


class ChunkTests(unittest.TestCase):
    """Dealing the suite out to the Windows CI job's chunks (#386)."""

    @classmethod
    def setUpClass(cls):
        cls.suite = the_suite()
        cls.grouped = run_tests.units(cls.suite)
        cls.durations = run_tests.load_durations()

    def test_every_class_lands_in_exactly_one_chunk_for_any_number(self):
        names = list(self.grouped)
        for chunks in range(1, 13):
            with self.subTest(chunks=chunks):
                parts = run_tests.assign(names, self.durations, chunks)
                self.assertEqual(len(parts), chunks)
                dealt = [name for part in parts for name in part]
                self.assertEqual(len(dealt), len(set(dealt)), "a class is in two chunks")
                self.assertEqual(set(dealt), set(names), "a class is in no chunk")

    def test_the_chunks_together_run_the_whole_suite_once(self):
        everything = [t.id() for t in run_tests.tests_in(self.suite)]
        self.assertGreater(len(everything), 1000)
        for chunks in (1, 3, 8, 11):
            with self.subTest(chunks=chunks):
                ids = [t.id() for k in range(1, chunks + 1)
                       for t in run_tests.tests_in(run_tests.chunk_suite(
                           self.suite, (k, chunks), self.durations))]
                self.assertEqual(sorted(ids), sorted(everything))

    def test_the_deal_is_the_same_each_time_whatever_the_order(self):
        names = list(self.grouped)
        first = run_tests.assign(names, self.durations, 8)
        shuffled = names[:]
        random.Random(386).shuffle(shuffled)
        self.assertEqual(run_tests.assign(shuffled, self.durations, 8), first)
        self.assertEqual(run_tests.assign(names, self.durations, 8), first)

    def test_a_class_keeps_its_tests_together_in_discovery_order(self):
        suite = run_tests.chunk_suite(self.suite, (2, 8), self.durations)
        order = [run_tests.unit_of(t) for t in run_tests.tests_in(suite)]
        runs = [name for i, name in enumerate(order) if i == 0 or order[i - 1] != name]
        self.assertEqual(len(runs), len(set(runs)), "a class's tests are split up")
        for name in runs:
            self.assertEqual([t.id() for t in run_tests.tests_in(suite)
                              if run_tests.unit_of(t) == name],
                             [t.id() for t in self.grouped[name]])

    def test_an_unknown_class_goes_to_the_chunk_with_least_work_and_moves_nothing(self):
        durations = {"m.A": 10.0, "m.B": 6.0, "m.C": 3.0}
        self.assertEqual(run_tests.assign(["m.C", "m.A", "m.B"], durations, 2),
                         [["m.A"], ["m.B", "m.C"]])
        # m.New is unknown: it goes after the known ones, to the lighter chunk (9 s).
        self.assertEqual(run_tests.assign(["m.New", "m.C", "m.A", "m.B"], durations, 2),
                         [["m.A"], ["m.B", "m.C", "m.New"]])
        # Several unknown ones spread out rather than pile onto one chunk.
        self.assertEqual(run_tests.assign(["x.P", "x.Q", "x.R"], {}, 3),
                         [["x.P"], ["x.Q"], ["x.R"]])

    def test_the_table_still_describes_the_suite(self):
        # A stale table balances nothing: refresh it with --update-durations
        # from the chunks' logs when this fails.
        self.assertTrue(all(isinstance(v, (int, float)) and v >= 0
                            for v in self.durations.values()))
        known = sum(1 for name in self.grouped if name in self.durations)
        self.assertGreaterEqual(known / len(self.grouped), 0.8,
                                "most test classes are missing from tools/test_durations.json")

    def test_a_chunk_is_written_k_of_n(self):
        self.assertEqual(run_tests.parse_chunk("3/8"), (3, 8))
        for bad in ("0/8", "9/8", "8", "1/0", "a/b", "-1/2", " 1/2"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                run_tests.parse_chunk(bad)
        with self.assertRaises(ValueError):
            run_tests.assign(["m.A"], {}, 0)

    def test_a_module_that_fails_to_import_still_lands_in_a_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "test_zq_broken.py").write_text("import zq_no_such_module\n")
            suite = unittest.TestLoader().discover(tmp, top_level_dir=tmp)
        names = list(run_tests.units(suite))
        self.assertEqual(len(names), 1)
        self.assertIn("test_zq_broken", names[0])
        self.assertEqual(sum(len(p) for p in run_tests.assign(names, {}, 4)), 1)


class GateTests(unittest.TestCase):
    """The required "Unit tests (Windows)" job that stands for the chunks."""

    def gate(self, needs):
        with contextlib.redirect_stdout(io.StringIO()):
            return run_tests.gate(needs if isinstance(needs, str) else json.dumps(needs))

    def test_passes_only_when_every_chunk_succeeded(self):
        needs = {"windows-tests-chunk": {"result": "success", "outputs": {}}}
        self.assertEqual(self.gate(needs), 0)

    def test_a_failed_cancelled_or_skipped_chunk_fails_it(self):
        for result in ("failure", "cancelled", "skipped", "", None):
            with self.subTest(result=result):
                self.assertEqual(self.gate({"windows-tests-chunk": {"result": result}}), 1)
        self.assertEqual(self.gate({"a": {"result": "success"}, "b": {"result": "skipped"}}), 1)

    def test_no_chunks_or_unreadable_input_fails_it(self):
        for needs in ("", "{}", "not json", {"windows-tests-chunk": None}):
            with self.subTest(needs=needs):
                self.assertEqual(self.gate(needs), 1)


class WorkflowTests(unittest.TestCase):
    """ci.yml runs the chunks and the gate the way the runner expects."""

    @classmethod
    def setUpClass(cls):
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        blocks = re.split(r"^  ([\w-]+):\s*$", text[text.index("\njobs:"):], flags=re.MULTILINE)
        cls.jobs = dict(zip(blocks[1::2], blocks[2::2], strict=True))

    def test_the_chunks_are_numbered_one_to_n_and_each_runs_its_own(self):
        chunk = self.jobs["windows-tests-chunk"]
        listed = re.search(r"^\s+chunk: \[([\d, ]+)\]$", chunk, re.MULTILINE)
        self.assertIsNotNone(listed, "the chunk matrix is not a plain list")
        numbers = [int(n) for n in listed[1].split(",")]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        self.assertIn("fail-fast: false", chunk)
        self.assertIn("CHUNK: ${{ matrix.chunk }}", chunk)
        self.assertIn("CHUNKS: ${{ strategy.job-total }}", chunk)
        self.assertIn("run: python tools/run_tests.py --strict-skips --chunk "
                      '"$env:CHUNK/$env:CHUNKS"', chunk)

    def test_the_required_job_needs_every_chunk_and_always_runs(self):
        gate = self.jobs["windows-tests"]
        self.assertIn("    name: Unit tests (Windows)\n", gate)
        self.assertIn("    needs: windows-tests-chunk\n", gate)
        self.assertIn("    if: ${{ always() }}\n", gate)
        self.assertIn("NEEDS: ${{ toJSON(needs) }}", gate)
        self.assertIn("run: python3 tools/run_tests.py --gate", gate)

    def test_no_other_windows_job_runs_the_whole_suite(self):
        # The chunks replace the single run; another full run would double the time.
        for job, body in self.jobs.items():
            if "windows-" in body and "tools/run_tests.py" in body and job != "windows-tests":
                self.assertIn("--chunk", body, f"{job} runs the whole suite")


if __name__ == "__main__":
    unittest.main()
