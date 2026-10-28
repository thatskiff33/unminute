# tools/security_lint.py
# Report-only security lint (#415 item 2): ruff's S rules (flake8-bandit) over
# every Python file ruff.toml covers, with --exit-zero, compared with the
# triaged baseline in tools/security_baseline.json. It is not a gate and no CI
# job runs it yet: the required "Ruff" check keeps ruff.toml's own rules, and
# running this in CI is a workflow change (AGENTS.md "Stop and ask"), left to
# #415. Making it a gate needs the owner (AGENTS.md E2).
#
#   python tools/security_lint.py             report against the baseline
#   python tools/security_lint.py --update    rewrite the baseline's counts
#
# The baseline holds, for each file and rule ("path::S603"), how many findings
# were triaged and why they are accepted ("why"). It has no line numbers, so
# moving code leaves it alone. A file and rule with more findings than the
# baseline allows is new: all its findings are listed, since which of them is
# new cannot be told without line numbers. A finding is triaged by fixing it,
# or by accepting it with a reason in "why"; --update keeps every reason,
# takes out what was fixed, and adds what is new with an empty "why", which the
# report lists as untriaged until a reason is written in. Rewrite it in the
# pull request that fixes findings (the report lists them as fixed); adding to
# it hides a finding, so say why in the pull request.
#
# It prints paths, line numbers, rule codes and ruff's own messages, never file
# content. Needs ruff from requirements/ci.txt (already pinned and
# hash-locked; no new dependency); ruff writes no cache (--no-cache).
# Exit codes: 0 the report ran, whatever it found; 2 ruff could not run (its
# message is printed), and --update then writes nothing.

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
BASELINE = ROOT / "tools" / "security_baseline.json"
ABOUT = ("Report-only security lint baseline (#415): ruff's S rules, triaged. Each entry is "
         "a file and rule, how many findings are accepted and why. Written by: "
         "python tools/security_lint.py --update (reasons are written by hand)")
KEY = re.compile(r"^[\w./-]+\.py::S\d{3}$")


def cannot_run(message, result=None):
    """Exit 2: ruff did not run, so there is nothing to report or write."""
    if result is not None:
        sys.stdout.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
    print(f"security lint could not run: {message}", file=sys.stderr)
    sys.exit(2)


def run_ruff():
    """ruff's S diagnostics as parsed JSON."""
    if importlib.util.find_spec("ruff") is None:
        cannot_run("ruff is not installed (pip install --require-hashes --only-binary=:all: "
                   "-r requirements/ci.txt)")
    cmd = [sys.executable, "-m", "ruff", "check", "--select", "S", "--exit-zero",
           "--no-cache", "--output-format", "json", "."]
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


def findings(items, root=ROOT):
    """[(path, line, code, message)] for each S diagnostic, by path and line. A
    file ruff cannot parse (code None) is the required Ruff check's to report."""
    found = []
    for item in items:
        code = item.get("code") or ""
        if not re.fullmatch(r"S\d{3}", code):
            continue
        found.append((relative(item["filename"], root), int(item["location"]["row"]), code,
                      item.get("message", "")))
    found.sort(key=lambda r: (r[0], r[1], r[2]))
    return found


def key(path, code):
    return f"{path}::{code}"


def counts(found):
    return collections.Counter(key(path, code) for path, _line, code, _m in found)


def read_baseline(path=None):
    path = path or BASELINE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("findings", {})


def compare(found, baseline):
    """(new, fixed, untriaged). new is [(key, allowed, [finding, ...])] for each
    file and rule with more findings than the baseline allows; fixed is
    [(key, before, now)] for each with fewer; untriaged is the baseline keys
    found now whose "why" is empty."""
    now = counts(found)
    by_key = collections.defaultdict(list)
    for f in found:
        by_key[key(f[0], f[2])].append(f)
    new, fixed = [], []
    for k in sorted(set(now) | set(baseline)):
        allowed = baseline.get(k, {}).get("count", 0)
        if now[k] > allowed:
            new.append((k, allowed, by_key[k]))
        elif now[k] < allowed:
            fixed.append((k, allowed, now[k]))
    untriaged = sorted(k for k in now if k in baseline and not baseline[k].get("why", "").strip())
    return new, fixed, untriaged


def write_baseline(found, old, path=None):
    """Counts from this run; every reason kept, a new entry's left empty."""
    path = path or BASELINE
    entries = {k: {"count": n, "why": old.get(k, {}).get("why", "")}
               for k, n in sorted(counts(found).items())}
    path.write_text(json.dumps({"_about": ABOUT, "findings": entries}, indent=1) + "\n",
                    encoding="utf-8", newline="\n")
    return entries


def report(found, new, fixed, untriaged, baseline):
    allowed = sum(e.get("count", 0) for e in baseline.values())
    by_rule = collections.Counter(code for _p, _l, code, _m in found)
    out = ["Security lint (ruff S, report only; baseline tools/security_baseline.json)", "",
           f"Findings: {len(found)} (baseline {allowed}); files and rules over the baseline: "
           f"{len(new)}; fewer than the baseline: {len(fixed)}; untriaged: {len(untriaged)}",
           "By rule: " + (", ".join(f"{c} {n}" for c, n in sorted(by_rule.items())) or "none")]
    if new:
        out += ["", "Over the baseline (triage: fix, or add the reason to the baseline):"]
        for k, n_allowed, items in new:
            out.append(f"  {k}: {len(items)} found, {n_allowed} accepted")
            out += [f"    {p}:{line} {code} {message}" for p, line, code, message in items]
    if fixed:
        out += ["", "Fewer than the baseline (run with --update to take them out):"]
        out += [f"  {k}: {now} (was {before})" for k, before, now in fixed]
    if untriaged:
        out += ["", "In the baseline with no reason (write its \"why\"):"]
        out += [f"  {k}" for k in untriaged]
    return "\n".join(out)


def escape(text):
    """A GitHub Actions workflow command's message (its % and newlines escaped)."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Report-only security lint: ruff's S rules (flake8-bandit) against the "
                    "triaged baseline (tools/security_baseline.json). Exit 0 when the report "
                    "ran, whatever it found; 2 when ruff could not run.")
    parser.add_argument("--update", action="store_true",
                        help="rewrite the baseline's counts from this run, keeping every reason")
    args = parser.parse_args(argv)
    found = findings(run_ruff())
    baseline = read_baseline()
    if args.update:
        entries = write_baseline(found, baseline)
        empty = sum(1 for e in entries.values() if not e["why"].strip())
        print(f"Wrote {len(entries)} file(s) and rule(s), {len(found)} finding(s), to "
              f"{BASELINE.name}; {empty} without a reason.")
        return 0
    new, fixed, untriaged = compare(found, baseline)
    text = report(found, new, fixed, untriaged, baseline)
    print(text)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for _k, _n, items in new:
            for path, line, code, message in items:
                print(f"::warning file={path},line={line},title=Security lint (report only)::"
                      f"{escape(f'{code} {message}')}")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(f"```\n{text}\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
