# tools/run_tests.py
# Run the unit tests and report every skipped test with its reason.
#
# With --strict-skips (used by the Windows CI job) the run fails if any test is
# skipped for a reason that is not in ALLOWED_SKIPS. The app is Windows-only,
# so on Windows every test should run: a skip there means a check that guards
# the clipboard, Credential Manager, junctions, file locks or Windows paths was
# not exercised, and green CI would overstate what was tested.
#
# The run writes no bytecode into the checkout, and fails if anything did: a
# test, or a process it started, that left a new or changed .pyc behind (#206).
# tools/evidence.py refuses a checkout holding ignored files such as
# __pycache__. The runner reports such files but never deletes them.
#
#   python tools/run_tests.py                  # same as unittest discover
#   python tools/run_tests.py --strict-skips   # Windows CI

import argparse
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Skip reasons that are acceptable even in a strict run, with why.
ALLOWED_SKIPS = {
    # Another process held the clipboard; the test retries, then skips rather
    # than fail on contention it cannot control.
    "clipboard busy": "transient contention",
    # huggingface_hub and httpx are not in the CI test install (B1's lock adds them).
    "download libraries not installed": "model download stack not installed in CI",
}

# Folders the bytecode check does not walk: git's store, virtual environments,
# package build output (tools/build_package.py), and extracted/, which holds
# real transcripts that are never listed (AGENTS.md, P2).
NOT_SCANNED = {".git", ".venv", "venv", "env", "dist", "extracted"}


def bytecode(root):
    """Every bytecode file under root, as {relative path: (size, modified)}."""
    found = {}
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in NOT_SCANNED]
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                path = os.path.join(folder, name)
                try:
                    st = os.stat(path)
                except OSError:              # gone again since the listing
                    continue
                found[os.path.relpath(path, root)] = (st.st_size, st.st_mtime_ns)
    return found


def written(before, after):
    """The bytecode files that are new or changed since before."""
    return sorted(path for path, stat in after.items() if before.get(path) != stat)


def no_bytecode():
    """Neither this process nor any process it starts writes bytecode. -B alone
    does not reach a child started as a bare sys.executable; the variable does
    (a child started with -I ignores it, so it must pass -B itself)."""
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict-skips", action="store_true",
                        help="fail when a test is skipped for an unlisted reason")
    args = parser.parse_args()

    no_bytecode()
    before = bytecode(ROOT)

    # Same as "python -m unittest discover -s tests" run from the repo root.
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)

    unexpected = [(test, reason) for test, reason in result.skipped
                  if reason not in ALLOWED_SKIPS]
    if result.skipped:
        print(f"\nSkipped {len(result.skipped)} test(s):")
        for test, reason in result.skipped:
            mark = "  " if reason in ALLOWED_SKIPS else "! "
            print(f"{mark}{test.id()}: {reason}")
    stray = written(before, bytecode(ROOT))
    if stray:
        print(f"\n{len(stray)} bytecode file(s) written into the checkout during the run "
              "(by a test or a process it started; see #206). They are left in place:")
        for path in stray:
            print(f"! {path}")
    if not result.wasSuccessful() or result.testsRun == 0 or stray:
        return 1
    if args.strict_skips and unexpected:
        print(f"\n{len(unexpected)} test(s) skipped for a reason not allowed in a strict run.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
