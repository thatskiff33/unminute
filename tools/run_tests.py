# tools/run_tests.py
# Run the unit tests and report every skipped test with its reason.
#
# With --strict-skips (used by the Windows CI job) the run fails if any test is
# skipped for a reason that is not in ALLOWED_SKIPS. The app is Windows-only,
# so on Windows every test should run: a skip there means a check that guards
# the clipboard, Credential Manager, junctions, file locks or Windows paths was
# not exercised, and green CI would overstate what was tested.
#
#   python tools/run_tests.py                  # same as unittest discover
#   python tools/run_tests.py --strict-skips   # Windows CI

import argparse
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict-skips", action="store_true",
                        help="fail when a test is skipped for an unlisted reason")
    args = parser.parse_args()

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
    if not result.wasSuccessful() or result.testsRun == 0:
        return 1
    if args.strict_skips and unexpected:
        print(f"\n{len(unexpected)} test(s) skipped for a reason not allowed in a strict run.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
