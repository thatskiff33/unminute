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
# --chunk K/N runs the K-th of N parts of the suite, as the Windows CI job does
# on N machines at once (#386); --strict-skips applies to each part. The parts
# are whole test classes, dealt out by their measured times in
# tools/test_durations.json: every class lands in exactly one part, the same
# one each time for the same classes and table, and a class the table does not
# know goes to the part with the least work so far. Each part prints its
# seconds per class on a "Class times" line; --update-durations merges those
# lines from the parts' saved CI logs into the table.
#
# --only NAME ... runs just the named test modules, classes or tests (for
# example test_drafts, or test_hardening.MachineModeScriptTests), with the same
# bytecode and skip rules: the quick local run for what a change touches. The
# full suite runs on CI on every push, in parts (the owner's decision, 26 October
# 2026), so the shared laptop is kept for screen and timing work.
#
#   python tools/run_tests.py                  # same as unittest discover
#   python tools/run_tests.py --only test_drafts test_app.LockedPageTests
#   python tools/run_tests.py --strict-skips   # Windows CI, in one go
#   python tools/run_tests.py --strict-skips --chunk 2/8
#   python tools/run_tests.py --update-durations chunk-1.log chunk-2.log ...

import argparse
import json
import os
import re
import sys
import time
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

# Seconds per test class, measured on CI, for dealing classes out to chunks.
DURATIONS = Path(__file__).resolve().parent / "test_durations.json"
# The weight given to a class the table does not know yet.
UNKNOWN_SECONDS = 1.0
# Starts the line on which each chunk prints its seconds per class.
TIMES_PREFIX = "Class times: "


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


def tests_in(suite):
    """Every test in a suite, in the order it would run."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from tests_in(item)
        else:
            yield item


def unit_of(test):
    """The class a test belongs to, as "module.Class". A module that fails to
    import is one failing test from unittest itself, named after the module,
    so it still lands in a chunk and fails it."""
    cls = type(test)
    if cls.__module__.startswith("unittest."):
        return test.id()
    return f"{cls.__module__}.{cls.__qualname__}"


def only_suite(suite, names):
    """The tests whose id is one of names, or starts with one followed by a dot,
    as one suite in discovery order. A module that fails to import keeps its
    one failing test, named after the module, so it still runs and fails."""
    def wanted(test):
        if type(test).__module__.startswith("unittest."):
            # A module that failed to import: unittest names its one failing
            # test after the module (unittest.loader._FailedTest.<module>).
            return any(name.split(".")[0] == test._testMethodName for name in names)
        return any(test.id() == name or test.id().startswith(name + ".") for name in names)
    return unittest.TestSuite([test for test in tests_in(suite) if wanted(test)])


def units(suite):
    """The suite's tests grouped by class, {name: [tests]}, in discovery order."""
    grouped = {}
    for test in tests_in(suite):
        grouped.setdefault(unit_of(test), []).append(test)
    return grouped


def assign(names, durations, chunks):
    """Deal the named classes out to chunks, as a list of name lists.

    The classes in the table go first, longest first (ties by name), each to
    the chunk with the least work so far (ties to the lowest number); then the
    ones it does not know, by name, the same way at UNKNOWN_SECONDS each. So a
    class added later never moves a known one."""
    if chunks < 1:
        raise ValueError("chunks must be at least 1")
    known = sorted((n for n in set(names) if n in durations), key=lambda n: (-durations[n], n))
    unknown = sorted(n for n in set(names) if n not in durations)
    loads = [0.0] * chunks
    parts = [[] for _ in range(chunks)]
    for name in known + unknown:
        i = min(range(chunks), key=lambda k: (loads[k], k))
        parts[i].append(name)
        loads[i] += durations.get(name, UNKNOWN_SECONDS)
    return parts


def load_durations(path=DURATIONS):
    """The measured seconds per class, or {} when there is no table."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))["classes"]
    except FileNotFoundError:
        return {}


def parse_chunk(text):
    """'K/N' as (K, N), with 1 <= K <= N."""
    match = re.fullmatch(r"(\d+)/(\d+)", text)
    if not match or not 1 <= int(match[1]) <= int(match[2]):
        raise argparse.ArgumentTypeError(f"--chunk wants K/N with 1 <= K <= N, not {text!r}")
    return int(match[1]), int(match[2])


def chunk_suite(suite, chunk, durations):
    """The tests of chunk (K, N) as one suite, in discovery order, so each
    class's tests stay together and its fixtures run once."""
    k, n = chunk
    grouped = units(suite)
    mine = set(assign(list(grouped), durations, n)[k - 1])
    return unittest.TestSuite(t for name, tests in grouped.items() if name in mine for t in tests)


class TimedResult(unittest.TextTestResult):
    """Notes the seconds each class took, each test counted from the end of
    the one before, so a class's setUpClass counts towards it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.class_times = {}
        self._mark = time.perf_counter()

    def stopTest(self, test):
        super().stopTest(test)
        now = time.perf_counter()
        name = unit_of(test)
        self.class_times[name] = self.class_times.get(name, 0.0) + now - self._mark
        self._mark = now


def update_durations(logs, path=DURATIONS):
    """Replace the table with the "Class times" lines from saved chunk logs."""
    found = {}
    for log in logs:
        for line in Path(log).read_text(encoding="utf-8", errors="replace").splitlines():
            at = line.find(TIMES_PREFIX)
            if at >= 0:
                found.update(json.loads(line[at + len(TIMES_PREFIX):]))
    if not found:
        print("No class times found in those logs.")
        return 1
    table = {"source": "class times from the CI logs " + ", ".join(Path(log).name for log in logs),
             "classes": dict(sorted(found.items()))}
    path.write_text(json.dumps(table, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(found)} class times written to {path.name}.")
    return 0


def gate(needs_json):
    """The final "Unit tests (Windows)" job: passes only when every job it
    needs succeeded. Failed, cancelled, skipped or none at all fails."""
    try:
        needs = json.loads(needs_json or "{}")
    except ValueError:
        needs = {}
    results = {job: (info or {}).get("result") for job, info in needs.items()}
    for job, result in sorted(results.items()):
        print(f"{job}: {result}")
    if not results or any(result != "success" for result in results.values()):
        print("Not every chunk passed.")
        return 1
    print("Every chunk passed.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict-skips", action="store_true",
                        help="fail when a test is skipped for an unlisted reason")
    parser.add_argument("--chunk", type=parse_chunk, metavar="K/N",
                        help="run only the K-th of N parts of the suite")
    parser.add_argument("--only", nargs="+", metavar="NAME",
                        help="run only these test modules, classes or tests")
    parser.add_argument("--update-durations", nargs="+", metavar="LOG",
                        help="rewrite the table from the chunks' class times in saved logs")
    parser.add_argument("--gate", action="store_true",
                        help="CI only: pass when every job in $NEEDS (JSON) succeeded")
    args = parser.parse_args()
    if args.only and args.chunk:
        parser.error("--only and --chunk cannot be used together")
    if args.update_durations:
        return update_durations(args.update_durations)
    if args.gate:
        return gate(os.environ.get("NEEDS", ""))

    no_bytecode()
    before = bytecode(ROOT)

    # Same as "python -m unittest discover -s tests" run from the repo root.
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    if args.only:
        everything = suite.countTestCases()
        suite = only_suite(suite, args.only)
        print(f"Only {', '.join(args.only)}: {suite.countTestCases()} of the suite's "
              f"{everything} tests.", flush=True)
        if not suite.countTestCases():
            print("No test matched; nothing was run.")
            return 1
    if args.chunk:
        everything = suite.countTestCases()
        suite = chunk_suite(suite, args.chunk, load_durations())
        print(f"Chunk {args.chunk[0]} of {args.chunk[1]}: {suite.countTestCases()} of the "
              f"suite's {everything} tests, in {len(units(suite))} classes.", flush=True)
    result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(suite)

    unexpected = [(test, reason) for test, reason in result.skipped
                  if reason not in ALLOWED_SKIPS]
    if result.skipped:
        print(f"\nSkipped {len(result.skipped)} test(s):")
        for test, reason in result.skipped:
            mark = "  " if reason in ALLOWED_SKIPS else "! "
            print(f"{mark}{test.id()}: {reason}")
    if args.chunk:
        print(f"\nChunk {args.chunk[0]} of {args.chunk[1]} ran {result.testsRun} test(s).")
        print(TIMES_PREFIX + json.dumps({name: round(seconds, 1) for name, seconds
                                         in sorted(result.class_times.items())}))
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
