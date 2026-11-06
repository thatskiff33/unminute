# tools/ts_check.py
# Report-only type check of the page's scripts (#489; the owner's decision
# DEC-089, coded from 3 Nov 2026 by the user's decision DEC-090): TypeScript's
# checker (tsc, pinned in package.json and package-lock.json) reads
# workspace/ui/*.js with the settings in tsconfig.json, and this compares what
# it finds with the committed baseline in tools/ts_baseline.txt. The "Type
# check (report only)" job in .github/workflows/quality.yml runs it on every
# pull request, after mypy. It is not a gate: errors new since the baseline are
# reported (as warnings on the pull request when run on GitHub Actions), never
# failed. Making it a gate needs the owner (AGENTS.md E2).
#
# Only the scripts that opt in with a "// @ts-check" line near their top are
# checked (tsconfig.json has checkJs off); the others are still read, because
# the checked ones import from them (the page's modules, #414, #488), so a name
# a checked module uses without importing it is reported too.
# The type notes are JSDoc comments, so the shipped code does not change.
# tools/page_globals.d.ts declares what the page uses but does not declare
# itself (the pywebview bridge); it is never in the package.
#
#   npm ci --ignore-scripts --no-audit --no-fund    install the pinned TypeScript
#   python tools/ts_check.py                        report against the baseline
#   python tools/ts_check.py --update               rewrite the baseline from this run
#
# The baseline holds one line per error, without its line and column, so moving
# code does not change it: "path: error TSnnnn: message", sorted. Rewrite it
# when a change fixes errors (the report lists them as fixed), so it only
# shrinks; adding to it hides a new error, so say why in the pull request.
#
# Needs Node.js on the PATH (CI uses the runner image's) and node_modules from
# npm ci. tsc writes nothing (noEmit). Exit codes: 0 tsc ran, whatever it
# found; 2 tsc could not run, or reported a problem with its settings rather
# than with the code (its own message is printed), and --update then writes
# nothing.

import argparse
import collections
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tools" / "ts_baseline.txt"
CONFIG = ROOT / "tsconfig.json"
TSC = ROOT / "node_modules" / "typescript" / "bin" / "tsc"
PAGE = ROOT / "workspace" / "ui"
INSTALL = "npm ci --ignore-scripts --no-audit --no-fund"

# "workspace/ui/core.js(12,5): error TS2339: Message" (tsc --pretty false). A
# message that continues on further, indented lines is keyed by its first line.
ERROR = re.compile(r"^(?P<path>[^()\n]+?)\((?P<line>\d+),(?P<col>\d+)\): "
                   r"error (?P<code>TS\d+): (?P<message>.+)$")
# Any other error line ("error TS18003: No inputs were found", or one placed in
# tsconfig.json): a problem with the settings, not with the code.
ANY_ERROR = re.compile(r"^(?:\S.*: )?error TS\d+: ")
# Line numbers inside a message move with the code.
LINE_REFERENCE = re.compile(r"\bon line \d+")
# The opt-in, as tsc reads it: a // @ts-check comment before the code.
OPT_IN = re.compile(r"^\s*//\s*@ts-check\b", re.MULTILINE)
CODE_FILES = (".js", ".ts")

HEADER = ("# TypeScript errors in the page's scripts accepted for now (#489), one per line\n"
          "# without its line and column, written by: python tools/ts_check.py --update\n")


def errors(output):
    """(found, problems): [(path, line, col, key)] for each error tsc placed in
    a script or declaration file, key being the baseline line; and every other
    error line, which means the settings are wrong."""
    found, problems = [], []
    for raw in output.splitlines():
        text = raw.rstrip()
        m = ERROR.match(text)
        path = m.group("path").replace("\\", "/") if m else ""
        if m and path.endswith(CODE_FILES):
            message = LINE_REFERENCE.sub("on line N", m.group("message").rstrip())
            found.append((path, int(m.group("line")), int(m.group("col")),
                          f"{path}: error {m.group('code')}: {message}"))
        elif ANY_ERROR.match(text):
            problems.append(text)
    return found, problems


def read_baseline(path=None):
    path = path or BASELINE                   # read when called, so a test can move it
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


def compare(current, baseline):
    """(new, fixed): the errors found but not in the baseline, as found, and the
    baseline lines no longer found. Counted, so a second copy of an accepted
    error is new."""
    allowed = collections.Counter(baseline)
    new = []
    for item in current:
        if allowed[item[3]] > 0:
            allowed[item[3]] -= 1
        else:
            new.append(item)
    fixed = sorted(allowed.elements())
    return new, fixed


def opted_in(folder=PAGE):
    """The page's scripts that opt in to the check, by name."""
    return sorted(p.name for p in folder.glob("*.js")
                  if OPT_IN.search(p.read_text(encoding="utf-8").split('"use strict"', 1)[0]))


def cannot_run(message, result=None):
    """Exit 2: tsc did not run, or not on the code, so there is no result to
    report or to write."""
    if result is not None:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
    print(f"tsc could not run: {message}", file=sys.stderr)
    sys.exit(2)


def run_tsc():
    node = shutil.which("node")
    if node is None:
        cannot_run("Node.js is not on the PATH")
    if not TSC.is_file():
        cannot_run(f"TypeScript is not installed ({INSTALL})")
    cmd = [node, str(TSC), "--project", str(CONFIG), "--noEmit", "--pretty", "false"]
    try:
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        cannot_run(type(exc).__name__)
    found, problems = errors(result.stdout)
    # tsc exits 0 when clean and 1 or 2 when it reports errors; with no error
    # placed in the code a non-zero exit is not a run (otherwise every baseline
    # error would look fixed, and --update would empty the baseline).
    if problems:
        cannot_run("it reported a problem with its settings", result)
    if result.returncode not in (0, 1, 2):
        cannot_run(f"exit code {result.returncode}", result)
    if result.returncode != 0 and not found:
        cannot_run(f"exit code {result.returncode} with no error reported", result)
    return found


def escape_property(text):
    """A workflow command property (its %, newlines, colons and commas escaped)."""
    return escape(text).replace(":", "%3A").replace(",", "%2C")


def escape(text):
    """A GitHub Actions workflow command's message (its % and newlines escaped)."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def report(current, new, fixed, baseline_count, checked):
    lines = [f"TypeScript errors: {len(current)} (baseline {baseline_count}); "
             f"new since the baseline: {len(new)}; fixed since the baseline: {len(fixed)}",
             f"Scripts checked (// @ts-check): {len(checked)}: {', '.join(checked) or 'none'}"]
    if new:
        lines += ["", "New since the baseline:"]
        lines += [f"  {p}:{n}:{c}: {k.split(': ', 1)[1]}" for p, n, c, k in new]
    if fixed:
        lines += ["", "Fixed since the baseline (run with --update to take them out):"]
        lines += [f"  {k}" for k in fixed]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Report-only TypeScript check of the page's scripts (workspace/ui) "
                    "against the committed baseline (tools/ts_baseline.txt). Exit 0 when tsc "
                    "ran, whatever it found; 2 when it could not run.")
    parser.add_argument("--update", action="store_true",
                        help="rewrite tools/ts_baseline.txt from this run")
    args = parser.parse_args(argv)
    current = run_tsc()
    if args.update:
        BASELINE.write_text(HEADER + "".join(f"{k}\n" for k in sorted(c[3] for c in current)),
                            encoding="utf-8", newline="\n")
        print(f"Wrote {len(current)} error(s) to {BASELINE.name}.")
        return 0
    baseline = read_baseline()
    new, fixed = compare(current, baseline)
    text = report(current, new, fixed, len(baseline), opted_in())
    print(text)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for path, line, col, key in new:
            print(f"::warning file={escape_property(path)},line={line},col={col},"
                  f"title=TypeScript (report only)::{escape(key.split(': ', 1)[1])}")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(f"```\n{text}\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
