# No test starts a real model download (#382): tools/run_tests.py installs
# tests/download_guard.py for the whole run, so a Downloads (or a Workspace)
# built without a fake target in any module is refused before its download
# process is sent anything, and the test it happened in fails, naming the
# cause, even when the error was raised in a thread or swallowed. No network
# is used: the guard refuses before anything is downloaded.
#   python tools/run_tests.py --only test_download_guard

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import download_guard  # noqa: E402
from workspace import asr_engines, setup  # noqa: E402

# A stray download in a process of its own, with the guard as the runner
# installs it; never started without the guard.
STRAY_DOWNLOAD = r"""
import json, sys, tempfile, threading, time
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
import download_guard
from workspace import setup
download_guard.install()
if not download_guard.installed():
    sys.exit("the guard did not install; nothing started")
download_guard.current = "zq.stray"
raised = []
threading.excepthook = raised.append
with tempfile.TemporaryDirectory() as tmp:
    downloads = setup.Downloads(Path(tmp) / "models")
    downloads.start(sys.argv[2])
    deadline = time.monotonic() + 60
    while (downloads.busy() or not raised) and time.monotonic() < deadline:
        time.sleep(0.02)
    print(json.dumps({"attempts": download_guard.take(),
                      "state": downloads.state[sys.argv[2]]["state"],
                      "downloaded": (Path(tmp) / "models").exists(),
                      "raised": [r.exc_type.__name__ for r in raised],
                      "message": "".join(str(r.exc_value) for r in raised)}))
"""

NOT_THROUGH_THE_RUNNER = ("the download guard is not installed: run the tests through "
                          "tools/run_tests.py, which installs it for every module (#382)")


class InstalledGuardTests(unittest.TestCase):
    """The guard as the runner installed it for this run."""

    def tearDown(self):
        download_guard.take()          # this module's own attempts are expected

    def test_the_runner_replaced_the_real_download_process(self):
        self.assertIs(setup._download_main, download_guard.REFUSED, NOT_THROUGH_THE_RUNNER)
        self.assertEqual(download_guard.REAL.__module__, "workspace.setup")
        self.assertEqual(download_guard.REAL.__name__, "_download_main")
        self.assertIs(setup.Downloads(".").target, download_guard.REFUSED)

    def test_a_stray_download_is_refused_before_its_process_gets_anything(self):
        # A Downloads with no fake target, as a Workspace's own is, in a child
        # process: the download process it starts gets nothing and ends (with
        # an EOFError of its own, kept out of this run's output).
        r = subprocess.run([sys.executable, "-B", "-c", STRAY_DOWNLOAD, str(ROOT),
                            asr_engines.RECOMMENDED_PARAKEET],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(got["attempts"], [["zq.stray", "handed to a download process"]])
        self.assertEqual(got["state"], "failed")
        self.assertFalse(got["downloaded"])
        self.assertEqual(got["raised"], ["RealDownloadRefused"])
        for part in ("workspace.setup._download_main", "Tests never download (#382)",
                     "during zq.stray"):
            self.assertIn(part, got["message"])

    def test_calling_it_in_this_process_is_refused(self):
        self.assertTrue(download_guard.installed(), NOT_THROUGH_THE_RUNNER)
        with self.assertRaises(download_guard.RealDownloadRefused) as caught:
            setup._download_main(None, asr_engines.RECOMMENDED_PARAKEET, ".")
        self.assertIn("Tests never download", str(caught.exception))
        self.assertEqual(download_guard.take(), [(self.id(), "called")])

    def test_the_opt_in_gives_the_real_one_then_the_guard_again(self):
        self.assertTrue(download_guard.installed(), NOT_THROUGH_THE_RUNNER)
        with download_guard.real_download_process() as real:
            self.assertIs(real, download_guard.REAL)
            self.assertIs(setup._download_main, download_guard.REAL)
        self.assertIs(setup._download_main, download_guard.REFUSED)
        self.assertIsNone(download_guard.check())
        self.assertEqual(download_guard.take(), [])

    def test_a_fake_patched_in_by_a_test_works_and_the_guard_comes_back(self):
        # tests/test_download_queue.py's way: patched in setUp, stopped in a cleanup.
        self.assertTrue(download_guard.installed(), NOT_THROUGH_THE_RUNNER)
        fake = mock.Mock()
        with mock.patch.object(setup, "_download_main", fake):
            self.assertIs(setup.Downloads(".").target, fake)
        self.assertIsNone(download_guard.check())


# A stand-in checkout: the real tests/download_guard.py and a workspace.setup
# whose _download_main only leaves a mark, run by the real runner.
SETUP = """
from pathlib import Path

def _download_main(conn, name, cache_dir):
    Path(cache_dir, "real-" + name).write_text("ran")
"""

STRAY = """
import os, sys, threading, unittest
from unittest import mock
import download_guard
from workspace import setup

OUT = os.environ["ZQ_OUT"]

class Stray(unittest.TestCase):
    def test_swallowed(self):
        try:
            setup._download_main(None, "swallowed", OUT)
        except BaseException:
            pass                                  # the run still fails

    def test_in_a_thread(self):
        thread = threading.Thread(target=lambda: setup._download_main(None, "thread", OUT))
        thread.start()
        thread.join()

    def test_fine(self):
        pass

    def test_opted_in(self):
        with download_guard.real_download_process() as real:
            real(None, "opted-in", OUT)

    def test_left_removed(self):
        mock.patch.object(setup, "_download_main", lambda *a: None).start()

    def test_zz_after_the_one_left_removed(self):     # tests run in name order
        self.assertIs(setup._download_main, download_guard.REFUSED)
"""


class RunnerReportTests(unittest.TestCase):
    """The real runner, in a child process, on a stand-in checkout."""

    def test_each_stray_download_fails_its_test_and_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp, "checkout"), Path(tmp, "out")
            (root / "tests").mkdir(parents=True)
            (root / "workspace").mkdir()
            out.mkdir()
            (root / "workspace" / "__init__.py").write_text("")
            (root / "workspace" / "setup.py").write_text(textwrap.dedent(SETUP))
            shutil.copy(ROOT / "tests" / "download_guard.py", root / "tests")
            (root / "tests" / "test_zq_stray.py").write_text(textwrap.dedent(STRAY))
            run = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[2]); "
                   "import run_tests; run_tests.ROOT = Path(sys.argv[1]); "
                   "sys.argv = ['run_tests.py']; sys.exit(run_tests.main())")
            env = dict(os.environ, ZQ_OUT=str(out))
            r = subprocess.run([sys.executable, "-B", "-c", run, str(root), str(ROOT / "tools")],
                               cwd=root, env=env, capture_output=True, text=True, timeout=120)
            marks = sorted(p.name for p in out.iterdir())
        output = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1, output)
        self.assertEqual(marks, ["real-opted-in"], output)      # only the opt-in ran it
        for test in ("test_swallowed", "test_in_a_thread"):
            self.assertIn(f"ERROR: test_zq_stray.Stray.{test} (download guard, #382)", output)
        self.assertIn("ERROR: test_zq_stray.Stray.test_left_removed (download guard, #382)",
                      output)
        self.assertIn("did not put it back", output)
        self.assertIn("Tests never download (#382)", output)
        self.assertIn("FAILED (errors=3)", output)
        for test in ("test_fine", "test_opted_in", "test_zz_after_the_one_left_removed"):
            self.assertNotIn(f"ERROR: test_zq_stray.Stray.{test} ", output)


if __name__ == "__main__":
    unittest.main()
