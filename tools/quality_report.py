# tools/quality_report.py
# Report-only code-size and complexity report (#365 part C; the owner approved
# the piece on 25 Oct 2026). The "Quality report (report only)" job in
# .github/workflows/quality.yml runs it on every pull request. It is not a gate:
# growth and new complexity are reported (complexity as warnings on the pull
# request when run on GitHub Actions), never failed. Making any of it a gate
# needs the owner (AGENTS.md E2).
#
#   python tools/quality_report.py             report against the baseline
#   python tools/quality_report.py --update    rewrite the baseline from this run
#
# It reports three things:
#   - file size: the line count of each tracked code file (SCOPES, EXTENSIONS),
#     and its growth against the committed baseline;
#   - function complexity: ruff's built-in McCabe rule, C901, over every Python
#     file ruff.toml covers, with --exit-zero and a threshold of MAX_COMPLEXITY.
#     Functions over it that are new, or more complex than in the baseline, are
#     listed;
#   - the largest Python modules, and the most complex functions.
#
# The baseline is tools/quality_baseline.json: {"lines": {path: lines},
# "complexity": {"path::function": [complexity, ...]}, "max_complexity": N}.
# Refresh it with --update and commit it in the pull request that accepts the
# figures: one that splits a large module or simplifies a function (the report
# lists them as shrunk or no longer over), or at a release. A refresh that takes
# in growth or new complexity says why in the pull request. Nothing reads the
# baseline but this tool.
#
# It reads only files git tracks, in the code folders, and counts their lines;
# it prints paths, function names and numbers, never file content. Needs git and
# ruff (ruff from requirements/ci.txt, already pinned and hash-locked; no new
# dependency). ruff writes no cache (--no-cache).
# Exit codes: 0 the report ran, whatever it found; 2 git or ruff could not run
# (its message is printed), and --update then writes nothing.

import argparse
import collections
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tools" / "quality_baseline.json"

# The code folders, and the file types counted in them. docs/ (including the
# design boards) and the locks are not code.
SCOPES = ("workspace/", "tools/", "tests/", "packaging/", ".github/workflows/")
EXTENSIONS = (".py", ".js", ".css", ".html", ".ps1", ".psm1", ".psd1", ".cmd", ".yml")

# ruff's default McCabe threshold, stated so a ruff upgrade cannot move it.
MAX_COMPLEXITY = 10
SHOWN = 15            # rows in each "largest" and "most complex" list
GROWTH_SHOWN = 25     # rows in the growth list

# ruff's C901 message: "`name` is too complex (12 > 10)".
TOO_COMPLEX = re.compile(r"^`(?P<name>[^`]+)` is too complex \((?P<n>\d+) > \d+\)$")


def cannot_run(message, result=None):
    """Exit 2: git or ruff did not run, so there is nothing to report or write."""
    if result is not None:
        sys.stdout.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
    print(f"quality report could not run: {message}", file=sys.stderr)
    sys.exit(2)


# --- File size ---------------------------------------------------------------

def in_scope(path):
    return path.startswith(SCOPES) and path.endswith(EXTENSIONS)


def tracked_code_files():
    """The tracked files in scope, as paths relative to the repository."""
    try:
        result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        cannot_run(f"git did not start ({exc.__class__.__name__})")
    if result.returncode != 0:
        cannot_run(f"git ls-files exit code {result.returncode}", result)
    return sorted(p for p in result.stdout.split("\0") if p and in_scope(p))


def count_lines(data):
    """Lines in a file's bytes; a last line without a newline still counts."""
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def line_counts(paths, root=ROOT):
    """{path: lines}. A tracked file missing from the checkout is left out."""
    counts = {}
    for path in paths:
        try:
            counts[path] = count_lines((root / path).read_bytes())
        except FileNotFoundError:
            continue
    return counts


def compare_sizes(current, baseline):
    """(grown, removed): grown is [(path, before or None, after)], largest growth
    first, a file new since the baseline counting all its lines; removed is the
    baseline paths no longer found."""
    grown = [(p, baseline.get(p), n) for p, n in current.items() if n > baseline.get(p, 0)]
    grown.sort(key=lambda r: (-(r[2] - (r[1] or 0)), r[0]))
    removed = sorted(set(baseline) - set(current))
    return grown, removed


# --- Complexity --------------------------------------------------------------

def run_ruff():
    """ruff's C901 diagnostics as parsed JSON."""
    if importlib.util.find_spec("ruff") is None:
        cannot_run("ruff is not installed (pip install --require-hashes --only-binary=:all: "
                   "-r requirements/ci.txt)")
    cmd = [sys.executable, "-m", "ruff", "check", "--select", "C901",
           "--config", f"lint.mccabe.max-complexity = {MAX_COMPLEXITY}",
           "--exit-zero", "--no-cache", "--output-format", "json", "."]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    # With --exit-zero ruff exits 0 whatever it finds; anything else means it
    # did not run (bad configuration, or Python could not start it).
    if result.returncode != 0:
        cannot_run(f"ruff exit code {result.returncode}", result)
    try:
        items = json.loads(result.stdout)
    except ValueError:
        cannot_run("ruff's output is not JSON", result)
    if not isinstance(items, list):
        cannot_run("ruff's output is not a list of diagnostics", result)
    return items


def relative(filename, root=ROOT):
    path = Path(filename)
    try:
        path = path.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    return path.as_posix()


def complexity(items, root=ROOT):
    """[(path, line, name, n)] for each C901 diagnostic, most complex first. A
    C901 message in another form stops the report (exit 2) rather than
    miscounting."""
    found = []
    for item in items:
        if item.get("code") != "C901":
            continue
        m = TOO_COMPLEX.match(item.get("message", ""))
        if not m:
            cannot_run("a C901 message in an unknown form")
        found.append((relative(item["filename"], root), int(item["location"]["row"]),
                      m.group("name"), int(m.group("n"))))
    found.sort(key=lambda r: (-r[3], r[0], r[1]))
    return found


def key(path, name):
    return f"{path}::{name}"


def baseline_complexity(found):
    """{"path::function": [n, ...]}, values sorted highest first."""
    grouped = collections.defaultdict(list)
    for path, _line, name, n in found:
        grouped[key(path, name)].append(n)
    return {k: sorted(v, reverse=True) for k, v in sorted(grouped.items())}


def compare_complexity(found, baseline):
    """(worse, fixed). worse is [(path, line, name, n, before or None)]: a function
    over the threshold that the baseline does not have (before None) or has at a
    lower complexity. Functions of the same name in one file are paired highest
    with highest. fixed is the baseline entries no longer over, as
    (key, before)."""
    current = collections.defaultdict(list)
    for path, line, name, n in found:
        current[key(path, name)].append((n, path, line, name))
    worse, fixed = [], []
    for k in sorted(set(current) | set(baseline)):
        now = sorted(current.get(k, []), key=lambda r: -r[0])
        before = sorted(baseline.get(k, []), reverse=True)
        for i, (n, path, line, name) in enumerate(now):
            if i >= len(before):
                worse.append((path, line, name, n, None))
            elif n > before[i]:
                worse.append((path, line, name, n, before[i]))
        fixed += [(k, b) for b in before[len(now):]]
    worse.sort(key=lambda r: (-r[3], r[0], r[1]))
    return worse, fixed


# --- Baseline and report -----------------------------------------------------

def read_baseline(path=None):
    path = path or BASELINE
    if not path.exists():
        return {"lines": {}, "complexity": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"lines": data.get("lines", {}), "complexity": data.get("complexity", {})}


def write_baseline(lines, found, path=None):
    path = path or BASELINE
    data = {
        "_about": "Report-only quality baseline (#365). Written by: "
                  "python tools/quality_report.py --update",
        "max_complexity": MAX_COMPLEXITY,
        "lines": dict(sorted(lines.items())),
        "complexity": baseline_complexity(found),
    }
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8", newline="\n")


def report(lines, found, baseline):
    base_lines, base_cx = baseline["lines"], baseline["complexity"]
    grown, removed = compare_sizes(lines, base_lines)
    shrunk = [p for p, n in lines.items() if p in base_lines and n < base_lines[p]]
    worse, fixed = compare_complexity(found, base_cx)
    total, base_total = sum(lines.values()), sum(base_lines.values())
    base_count = sum(len(v) for v in base_cx.values())
    out = ["Quality report (report only; baseline tools/quality_baseline.json)", "",
           f"Size: {len(lines)} code files, {total:,} lines (baseline {len(base_lines)} files, "
           f"{base_total:,} lines; {total - base_total:+,})"]
    if grown:
        out.append(f"Grown since the baseline: {len(grown)} file(s), largest growth first"
                   + (f" (the first {GROWTH_SHOWN})" if len(grown) > GROWTH_SHOWN else "") + ":")
        for path, before, after in grown[:GROWTH_SHOWN]:
            if before is None:
                out.append(f"  {'new':>7}  {path} ({after:,})")
            else:
                out.append(f"  {after - before:>+7,}  {path} ({before:,} -> {after:,})")
    out.append(f"Shrunk since the baseline: {len(shrunk)} file(s); removed: {len(removed)}")
    out += ["", f"Largest Python modules (lines, the first {SHOWN}):"]
    modules = sorted(((n, p) for p, n in lines.items() if p.endswith(".py")),
                     key=lambda r: (-r[0], r[1]))
    out += [f"  {n:>7,}  {p}" for n, p in modules[:SHOWN]]
    out += ["", f"Complexity (ruff C901, over {MAX_COMPLEXITY}): {len(found)} function(s) "
                f"(baseline {base_count}); new or more complex: {len(worse)}; "
                f"no longer over: {len(fixed)}"]
    if worse:
        out.append("New or more complex since the baseline:")
        for path, line, name, n, before in worse:
            was = "new" if before is None else f"was {before}"
            out.append(f"  {n:>3}  {path}:{line} {name} ({was})")
    if fixed:
        out.append("No longer over (run with --update to take them out):")
        out += [f"  {k} (was {b})" for k, b in fixed]
    out.append(f"Most complex (the first {SHOWN}):")
    out += [f"  {n:>3}  {path}:{line} {name}" for path, line, name, n in found[:SHOWN]]
    return "\n".join(out), worse


def escape(text):
    """A GitHub Actions workflow command's message (its % and newlines escaped)."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Report-only code size and complexity (ruff C901) against the committed "
                    "baseline (tools/quality_baseline.json). Exit 0 when the report ran, "
                    "whatever it found; 2 when git or ruff could not run.")
    parser.add_argument("--update", action="store_true",
                        help="rewrite tools/quality_baseline.json from this run")
    args = parser.parse_args(argv)
    lines = line_counts(tracked_code_files())
    found = complexity(run_ruff())
    if args.update:
        write_baseline(lines, found)
        print(f"Wrote {len(lines)} file(s) and {len(found)} complex function(s) "
              f"to {BASELINE.name}.")
        return 0
    text, worse = report(lines, found, read_baseline())
    print(text)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for path, line, name, n, before in worse:
            was = "new over the threshold" if before is None else f"was {before}"
            print(f"::warning file={path},line={line},title=Complexity (report only)::"
                  f"{escape(f'{name} has complexity {n} ({was}; threshold {MAX_COMPLEXITY})')}")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(f"```\n{text}\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
