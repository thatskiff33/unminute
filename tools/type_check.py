# tools/type_check.py
# Report-only static type check (#413; the owner's decision of 27 Oct 2026):
# mypy over the shipped code (workspace/), with the settings in mypy.ini,
# compared with the committed baseline in tools/mypy_baseline.txt. The
# "Type check (report only)" job in .github/workflows/quality.yml runs it on
# every pull request. It is not a gate: errors new since the baseline are
# reported (as warnings on the pull request when run on GitHub Actions), never
# failed. Making it a gate needs the owner (#365, AGENTS.md E2).
#
#   python tools/type_check.py             report against the baseline
#   python tools/type_check.py --update    rewrite the baseline from this run
#
# The baseline holds one line per error, without its line number, so moving
# code does not change it: "path: error: message  [code]", sorted. Rewrite it
# when a change fixes errors (the report lists them as fixed), so it only
# shrinks; adding to it hides a new error, so say why in the pull request.
#
# Needs mypy from requirements/ci-mypy.txt (pip install --require-hashes
# --no-deps --only-binary=:all: -r requirements/ci-mypy.txt). mypy writes no
# cache (--cache-dir is the null device), so nothing is left in the checkout.
# Exit codes: 0 mypy ran, whatever it found; 2 mypy could not run (its own
# message is printed).

import argparse
import collections
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tools" / "mypy_baseline.txt"
CONFIG = ROOT / "mypy.ini"
TARGETS = ("workspace",)

# "workspace\api.py:12: error: Message  [code]" (mypy's default output).
ERROR = re.compile(r"^(?P<path>[^:\n]+?):(?P<line>\d+):(?:\d+:)? error: (?P<message>.+)$")
# Line numbers inside a message ("already defined on line 40") move with the code.
LINE_REFERENCE = re.compile(r"\bon line \d+")

HEADER = ("# mypy errors accepted for now (#413), one per line without its line number,\n"
          "# written by: python tools/type_check.py --update\n")


def errors(output):
    """[(path, line, key)] for each error in mypy's output, key being the baseline line."""
    found = []
    for raw in output.splitlines():
        m = ERROR.match(raw.strip())
        if not m:
            continue
        path = m.group("path").replace("\\", "/")
        message = LINE_REFERENCE.sub("on line N", m.group("message").rstrip())
        found.append((path, int(m.group("line")), f"{path}: error: {message}"))
    return found


def read_baseline(path=BASELINE):
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


def compare(current, baseline):
    """(new, fixed): the errors found but not in the baseline, as (path, line, key),
    and the baseline lines no longer found. Counted, so a second copy of an
    accepted error is new."""
    allowed = collections.Counter(baseline)
    new = []
    for path, line, key in current:
        if allowed[key] > 0:
            allowed[key] -= 1
        else:
            new.append((path, line, key))
    fixed = sorted(allowed.elements())
    return new, fixed


def run_mypy():
    cmd = [sys.executable, "-m", "mypy", "--config-file", str(CONFIG),
           "--cache-dir", os.devnull, "--no-color-output", "--no-error-summary", *TARGETS]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    # mypy exits 1 when it finds errors and 2 when it cannot run.
    if result.returncode not in (0, 1):
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit(2)
    return result.stdout


def escape(text):
    """A GitHub Actions workflow command's message (its % and newlines escaped)."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def report(current, new, fixed, baseline_count):
    lines = [f"mypy errors: {len(current)} (baseline {baseline_count}); "
             f"new since the baseline: {len(new)}; fixed since the baseline: {len(fixed)}"]
    if new:
        lines += ["", "New since the baseline:"] + [f"  {p}:{n}: {k.split(': ', 1)[1]}"
                                                    for p, n, k in new]
    if fixed:
        lines += ["", "Fixed since the baseline (run with --update to take them out):"]
        lines += [f"  {k}" for k in fixed]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true",
                        help="rewrite tools/mypy_baseline.txt from this run")
    args = parser.parse_args(argv)
    current = errors(run_mypy())
    if args.update:
        BASELINE.write_text(HEADER + "".join(f"{k}\n" for k in sorted(c[2] for c in current)),
                            encoding="utf-8", newline="\n")
        print(f"Wrote {len(current)} error(s) to {BASELINE.relative_to(ROOT).as_posix()}.")
        return 0
    baseline = read_baseline()
    new, fixed = compare(current, baseline)
    text = report(current, new, fixed, len(baseline))
    print(text)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for path, line, key in new:
            print(f"::warning file={path},line={line},title=mypy (report only)::"
                  f"{escape(key.split(': ', 1)[1])}")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(f"```\n{text}\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
