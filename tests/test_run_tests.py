# The test runner writes no bytecode into the checkout and fails when a test,
# or a process a test started, left some behind (#206). tools/evidence.py
# refuses a checkout with ignored files such as __pycache__ in it.
#   python tools/run_tests.py

import os
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
    sys.argv = ["run_tests.py"]
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

    def run_runner(self, root):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
        # -B only, as a person starts it: -B does not reach the runner's children.
        return subprocess.run([sys.executable, "-B", "-c", RUN, str(root), str(ROOT / "tools")],
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


if __name__ == "__main__":
    unittest.main()
