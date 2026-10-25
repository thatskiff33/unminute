# tools/dev_status.py
# One screen of live state for an agent starting, handing over or finishing work
# (AGENTS.md "Start here", "Several agents or sessions" and "Delivery"), from git
# and gh instead of an earlier session's memory: how far each worktree is behind
# origin/main and which open PR it belongs to, every open PR with its draft, merge
# and required-check state, and the files two or more open PRs change.
# It changes nothing except refreshing origin's refs (skip with --no-fetch); git
# status runs without optional locks, so another session's commit is never held
# up. It prints branch names, PR numbers and titles, check names and repository
# paths only: never file contents, and nothing from ignored folders (extracted/).
#
#   python tools/dev_status.py                     # the whole picture
#   python tools/dev_status.py --files a.py b.md   # which open PRs change these files
#   python tools/dev_status.py --pr 363            # what still stops this PR (exit 1 if anything)
#
# --pr reads what the repository shows. It does not read a review's verdict,
# unresolved review threads, code-scanning alerts, open owner decisions or a
# range-diff after a rebase; it says so each time, and AGENTS.md "Authority" decides.

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "main"
PR_FIELDS = "number,title,headRefName,isDraft,mergeStateStatus,files,statusCheckRollup"
VIEW_FIELDS = ("number,state,isDraft,mergeStateStatus,headRefOid,statusCheckRollup,"
               "comments,reviews")
GH_FILE_LIMIT = 100          # gh returns at most this many files per pull request
PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}
WAITING = {"", "PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", "REQUESTED"}
NOT_CHECKED = ("Not checked here: the review's verdict and findings answered, unresolved "
               "review threads, code-scanning alerts, open owner decisions, the reason for "
               "any failing check that is not required, and range-diff after a rebase "
               "(AGENTS.md, Authority).")


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
    return parse_required(gh_json(["api", f"repos/{{owner}}/{{repo}}/rules/branches/{BASE}"]))


# --- Pure analysis (tested with canned data) ----------------------------------

def parse_required(rules):
    """Required status check names from the rules/branches API response."""
    return sorted({c["context"] for r in rules if r.get("type") == "required_status_checks"
                   for c in r.get("parameters", {}).get("required_status_checks", [])})


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
    contexts alike. A check run more than once keeps its latest run, as GitHub
    does for merging: the latest start time wins, then the later entry."""
    latest = {}
    for order, item in enumerate(rollup or []):
        name = item.get("name") or item.get("context") or "?"
        key = (item.get("startedAt") or item.get("completedAt") or "", order)
        if name not in latest or key >= latest[name][0]:
            latest[name] = (key, item)
    states = {}
    for name, (_, item) in latest.items():
        raw = (item.get("conclusion") or item.get("state") or "").upper()
        if item.get("status") and item["status"].upper() != "COMPLETED":
            raw = "IN_PROGRESS"
        states[name] = ("SUCCESS" if raw in PASSED else "WAITING" if raw in WAITING
                        else "FAILURE")
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


def capped(pr):
    return len(pr_files(pr)) >= GH_FILE_LIMIT


def overlaps(prs):
    """{path: [PR numbers]} for every path that two or more open PRs change."""
    seen = {}
    for pr in prs:
        for path in pr_files(pr):
            seen.setdefault(path, []).append(pr["number"])
    return {p: sorted(n) for p, n in sorted(seen.items()) if len(n) > 1}


def repo_path(path, root=ROOT):
    """A path as the repository names it: relative to root, forward slashes."""
    given = Path(path)
    if given.is_absolute():
        try:
            path = str(given.resolve().relative_to(Path(root).resolve()))
        except ValueError:
            pass                    # outside the checkout: matched as written
    return path.replace("\\", "/").removeprefix("./")


def touching(prs, paths, root=ROOT):
    """{path: [PR numbers]} for the given paths, matched without regard to case
    (Windows file names are not case-sensitive)."""
    found = {}
    for path in sorted({repo_path(p, root) for p in paths}):
        found[path] = sorted(pr["number"] for pr in prs
                             if path.lower() in {f.lower() for f in pr_files(pr)})
    return found


def notes_of(pr):
    """The text of a PR's comments and reviews."""
    return ([c.get("body") or "" for c in pr.get("comments") or []]
            + [r.get("body") or "" for r in pr.get("reviews") or []])


def assess(pr, required, notes):
    """(blockers, notes) for one PR. Blockers are what the repository shows still
    stops a merge under AGENTS.md "Authority"; notes are true but not, by
    themselves, a reason to wait (the draft state is lifted by `gh pr ready` once
    the blockers are gone). A review counts as found when a comment or review
    names the head commit's first 7 characters; its verdict is not read."""
    blockers, info = [], []
    state = pr.get("state", "OPEN")
    if state != "OPEN":
        return [f"the PR is {state.lower()}"], info
    if pr.get("isDraft"):
        info.append("draft: run gh pr ready once nothing blocks it, then run --pr again")
    failing, waiting, missing = check_summary(pr.get("statusCheckRollup"), required)
    blockers += [f"required check failing: {n}" for n in failing if n in required]
    blockers += [f"required check still running: {n}" for n in waiting if n in required]
    blockers += [f"required check not reported: {n}" for n in missing]
    info += [f"check failing, not required: {n} (record the reason on the PR)"
             for n in failing if n not in required]
    head = (pr.get("headRefOid") or "")[:7]
    if head and any(head in text for text in notes):
        info.append(f"a comment or review names {head} (its verdict is not read here)")
    elif head:
        blockers.append(f"no comment or review names the head commit {head}: record the "
                        "independent review of this exact commit")
    merge = pr.get("mergeStateStatus", "")
    if merge in {"DIRTY", "BEHIND"}:
        blockers.append(f"merge state {merge}: rebase onto origin/{BASE} (a rebase keeps the "
                        "review only if git range-diff shows every commit '=')")
    elif merge == "UNKNOWN":
        blockers.append("merge state not worked out yet by GitHub: run --pr again shortly")
    elif merge == "BLOCKED" and not blockers and not pr.get("isDraft"):
        blockers.append("GitHub still blocks the merge: look for unresolved review threads "
                        "or a code-scanning alert")
    return blockers, info


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
            dirty = len(run(["git", "--no-optional-locks", "-C", tree["path"], "status",
                             "--porcelain"]).splitlines())
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
        if capped(pr):
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
    for pr in prs:
        if capped(pr):
            print(f"  note: #{pr['number']} changes {GH_FILE_LIMIT}+ files and gh lists only "
                  f"{GH_FILE_LIMIT}; check it with gh pr diff {pr['number']} --name-only")


def show_pr(number, required):
    pr = gh_json(["pr", "view", str(number), "--json", VIEW_FIELDS])
    blockers, info = assess(pr, required, notes_of(pr))
    print(f"PR #{number} at {pr['headRefOid'][:7]}: "
          + ("nothing found that stops it" if not blockers else f"{len(blockers)} to resolve"))
    for item in blockers:
        print(f"  - {item}")
    for item in info:
        print(f"  note: {item}")
    print(f"  {NOT_CHECKED}")
    return 1 if blockers else 0


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # PR titles may hold any character
    parser = argparse.ArgumentParser(description="Live worktree, pull request and "
                                     "overlap state for agents (AGENTS.md).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--files", nargs="+", metavar="PATH",
                       help="list the open PRs that change these paths")
    group.add_argument("--pr", type=int, help="list what still stops this PR")
    parser.add_argument("--no-fetch", action="store_true",
                        help="use origin's refs as they are")
    args = parser.parse_args(argv)
    if not args.no_fetch:
        run(["git", "fetch", "origin", "--prune", "--quiet"])
    if args.pr:
        return show_pr(args.pr, required_checks())
    prs = gh_json(["pr", "list", "--state", "open", "--limit", "100", "--json", PR_FIELDS])
    if args.files:
        show_files(prs, args.files)
    else:
        show_status(prs, required_checks())
    return 0


if __name__ == "__main__":
    sys.exit(main())
