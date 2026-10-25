# tools/dev_status.py
# One screen of live state for an agent starting, handing over or finishing work
# (AGENTS.md "Start here", "Several agents or sessions" and "Delivery"), from git
# and gh instead of an earlier session's memory: how far each worktree is behind
# origin/main and which open PR it belongs to, every open PR with its draft, merge
# and required-check state, and the files two or more open PRs change.
# It changes nothing except refreshing origin's refs (skip with --no-fetch), and
# prints branch names, PR numbers, check names and repository paths only: never
# file contents, and nothing from a checkout's ignored folders (extracted/).
#
#   python tools/dev_status.py                     # the whole picture
#   python tools/dev_status.py --files a.py b.md   # which open PRs change these files
#   python tools/dev_status.py --pr 363            # what still stops this PR (exit 1 if anything)

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "main"
PR_FIELDS = "number,title,headRefName,isDraft,mergeStateStatus,files,statusCheckRollup"
GH_FILE_LIMIT = 100          # gh returns at most this many files per pull request
PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}
WAITING = {"", "PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", "REQUESTED"}


# --- Reading git and gh -------------------------------------------------------

def run(args, cwd=ROOT):
    """A command's standard output, or SystemExit with its error."""
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if done.returncode:
        sys.exit(f"{' '.join(args[:3])} failed: {done.stderr.strip()[:300]}")
    return done.stdout


def gh_json(args):
    return json.loads(run(["gh", *args]))


def required_checks():
    """The status checks the base branch's rulesets require, as GitHub reports them."""
    rules = gh_json(["api", f"repos/{{owner}}/{{repo}}/rules/branches/{BASE}"])
    return sorted({c["context"] for r in rules if r.get("type") == "required_status_checks"
                   for c in r["parameters"]["required_status_checks"]})


# --- Pure analysis (tested with canned data) ----------------------------------

def parse_worktrees(porcelain):
    """[{path, head, branch}] from `git worktree list --porcelain`; branch is None
    for a detached head."""
    trees, current = [], {}
    for line in porcelain.splitlines() + [""]:
        if not line:
            if current:
                trees.append(current)
            current = {}
        elif line.startswith("worktree "):
            current = {"path": line[9:], "head": "", "branch": None}
        elif line.startswith("HEAD "):
            current["head"] = line[5:]
        elif line.startswith("branch "):
            current["branch"] = line[7:].removeprefix("refs/heads/")
    return trees


def check_states(rollup):
    """{check name: state} from a statusCheckRollup, for check runs and status
    contexts alike; a check reported more than once keeps its worst state."""
    rank = {"FAILURE": 3, "WAITING": 2, "SUCCESS": 1}
    states = {}
    for item in rollup or []:
        name = item.get("name") or item.get("context") or "?"
        raw = (item.get("conclusion") or item.get("state") or "").upper()
        if item.get("status") and item["status"].upper() != "COMPLETED":
            raw = "IN_PROGRESS"
        state = "SUCCESS" if raw in PASSED else "WAITING" if raw in WAITING else "FAILURE"
        if rank[state] > rank.get(states.get(name), 0):
            states[name] = state
    return states


def check_summary(rollup, required):
    """(failing, waiting, missing): failing and waiting name every check; missing
    names required checks that have not reported at all."""
    states = check_states(rollup)
    failing = sorted(n for n, s in states.items() if s == "FAILURE")
    waiting = sorted(n for n, s in states.items() if s == "WAITING")
    missing = sorted(set(required) - set(states))
    return failing, waiting, missing


def pr_files(pr):
    return {f["path"] for f in pr.get("files") or []}


def overlaps(prs):
    """{path: [PR numbers]} for every path that two or more open PRs change."""
    seen = {}
    for pr in prs:
        for path in pr_files(pr):
            seen.setdefault(path, []).append(pr["number"])
    return {p: sorted(n) for p, n in sorted(seen.items()) if len(n) > 1}


def touching(prs, paths):
    """{path: [PR numbers]} for the given repository paths (forward slashes)."""
    wanted = {p.replace("\\", "/").removeprefix("./") for p in paths}
    return {p: sorted(pr["number"] for pr in prs if p in pr_files(pr)) for p in sorted(wanted)}


def blockers(pr, required, notes):
    """What still stops the PR being marked ready and merged under AGENTS.md
    "Authority", as far as the repository can tell. notes is the text of its
    comments and reviews; a review counts as recorded when one names the head
    commit (its first 7 characters), which is a heuristic, not a check of the
    review's quality."""
    found = []
    if pr.get("isDraft"):
        found.append("draft: mark ready (gh pr ready) once checks pass and the review is recorded")
    state = pr.get("mergeStateStatus", "")
    if state in {"DIRTY", "BEHIND"}:
        found.append(f"merge state {state}: rebase onto origin/{BASE} "
                     "(a rebase keeps the review only if git range-diff shows every commit '=')")
    failing, waiting, missing = check_summary(pr.get("statusCheckRollup"), required)
    for name in failing:
        found.append(f"check failing: {name}" + (" (required)" if name in required else
                                                 " (not required: record the reason on the PR)"))
    found += [f"check still running: {name}" for name in waiting if name in required]
    found += [f"required check not reported: {name}" for name in missing]
    head = (pr.get("headRefOid") or "")[:7]
    if head and not any(head in text for text in notes):
        found.append(f"no comment or review names the head commit {head}: "
                     "record the independent review of this exact commit")
    return found


def behind_ahead(counts):
    """(behind, ahead) from `git rev-list --left-right --count base...head`."""
    left, right = counts.split()
    return int(left), int(right)


# --- Output -------------------------------------------------------------------

def short_path(path):
    home = str(Path.home()).replace("\\", "/")
    path = path.replace("\\", "/")
    return "~" + path[len(home):] if path.lower().startswith(home.lower()) else path


def show_status(prs, required):
    by_branch = {pr["headRefName"]: pr["number"] for pr in prs}
    print(f"Worktrees (behind/ahead of origin/{BASE}, uncommitted file count):")
    for tree in parse_worktrees(run(["git", "worktree", "list", "--porcelain"])):
        ref = tree["branch"] or tree["head"]
        try:
            behind, ahead = behind_ahead(run(["git", "rev-list", "--left-right", "--count",
                                              f"origin/{BASE}...{tree['head']}"]))
            dirty = len(run(["git", "-C", tree["path"], "status", "--porcelain"]).splitlines())
        except SystemExit:          # a worktree whose folder has gone
            print(f"  {short_path(tree['path'])}  {tree['branch'] or 'detached'}  (unreadable: "
                  "git worktree prune?)")
            continue
        pr = by_branch.get(tree["branch"])
        label = f"PR #{pr}" if pr else "no open PR"
        print(f"  {short_path(tree['path'])}  {tree['branch'] or 'detached ' + ref[:7]}  "
              f"-{behind}/+{ahead}  dirty {dirty}  {label}")
    print(f"\nOpen pull requests ({len(prs)}):")
    for pr in sorted(prs, key=lambda p: p["number"]):
        failing, waiting, missing = check_summary(pr["statusCheckRollup"], required)
        flags = ["draft" if pr["isDraft"] else "ready", pr["mergeStateStatus"].lower()]
        if failing:
            flags.append("failing: " + ", ".join(failing))
        if waiting:
            flags.append(f"{len(waiting)} running")
        if missing:
            flags.append(f"{len(missing)} required not reported")
        if len(pr_files(pr)) >= GH_FILE_LIMIT:
            flags.append(f"{GH_FILE_LIMIT}+ files: overlap list may be incomplete")
        print(f"  #{pr['number']} {pr['headRefName']}  [{'; '.join(flags)}]")
        print(f"      {pr['title'][:100]}")
    shared = overlaps(prs)
    print(f"\nFiles changed by two or more open PRs ({len(shared)}):")
    for path, numbers in sorted(shared.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        print(f"  {len(numbers)}  {path}  " + " ".join(f"#{n}" for n in numbers))


def show_files(prs, paths):
    for path, numbers in touching(prs, paths).items():
        owners = " ".join(f"#{n}" for n in numbers) or "no open PR"
        print(f"  {path}: {owners}")


def show_pr(number, required):
    pr = gh_json(["pr", "view", str(number), "--json",
                  "number,isDraft,mergeStateStatus,headRefOid,statusCheckRollup,comments,reviews"])
    notes = [c.get("body") or "" for c in pr.get("comments") or []]
    notes += [r.get("body") or "" for r in pr.get("reviews") or []]
    found = blockers(pr, required, notes)
    print(f"PR #{number} at {pr['headRefOid'][:7]}: "
          + ("nothing found that stops it" if not found else f"{len(found)} to resolve"))
    for item in found:
        print(f"  - {item}")
    print("  Not checked here: open owner decisions, review findings answered, "
          "range-diff after a rebase (AGENTS.md, Authority).")
    return 1 if found else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Live worktree, pull request and "
                                     "overlap state for agents (AGENTS.md).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--files", nargs="+", metavar="PATH",
                       help="list the open PRs that change these repository paths")
    group.add_argument("--pr", type=int, help="list what still stops this PR")
    parser.add_argument("--no-fetch", action="store_true",
                        help="use origin's refs as they are")
    args = parser.parse_args(argv)
    if not args.no_fetch:
        run(["git", "fetch", "origin", "--prune", "--quiet"])
    required = required_checks()
    if args.pr:
        return show_pr(args.pr, required)
    prs = gh_json(["pr", "list", "--state", "open", "--limit", "100", "--json", PR_FIELDS])
    if args.files:
        show_files(prs, args.files)
    else:
        show_status(prs, required)
    return 0


if __name__ == "__main__":
    sys.exit(main())
