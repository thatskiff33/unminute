# tools/dev_status.py
# One screen of live state for an agent starting, handing over or finishing work
# (AGENTS.md "Start here", "Several agents or sessions" and "Delivery"), from git
# and gh instead of an earlier session's memory: how far each worktree is behind
# origin/main and which open PR it belongs to, every open PR with its draft, merge
# and required-check state, and the files two or more open PRs change.
# It also checks the work-in-progress limits in AGENTS.md "Several agents or
# sessions" (#412): the number of open drafts, each open draft's size (lines
# changed outside tests/), age and conflict state, and the worktrees to tidy
# (detached, or on a branch whose PR has merged or closed or whose origin branch
# has gone). GitHub does not say when a PR began to conflict, so a conflicting
# draft is measured by the age of its last commit: it has not been rebased since.
# It changes nothing except refreshing origin's refs (skip with --no-fetch); git
# status runs without optional locks, so another session's commit is never held
# up. It prints branch names, PR numbers and titles, check names and repository
# paths only: never file contents, and nothing from ignored folders (extracted/).
#
#   python tools/dev_status.py                     # the whole picture
#   python tools/dev_status.py --files a.py b.md   # which open PRs change these files
#   python tools/dev_status.py --pr 363            # what stops arming this PR (exit 1 if any)
#
# --pr reads what the repository shows. It does not read a review's verdict,
# unresolved review threads, code-scanning alerts or open owner decisions, and it
# takes a recorded range-diff as written; it says so each time, and AGENTS.md
# "Authority" decides. Required checks still running do not stop it: the PR is
# marked ready and auto-merge armed on the reviewed head, and GitHub waits for them
# (AGENTS.md "Delivery").

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "main"
PR_FIELDS = ("number,title,headRefName,isDraft,mergeStateStatus,files,statusCheckRollup,"
             "createdAt,mergeable")
# Read per draft: listing commits for every open PR at once exceeds GitHub's
# GraphQL node limit.
DRAFT_FIELDS = "commits"
CLOSED_FIELDS = "number,headRefName,headRefOid,state"
CLOSED_LIMIT = 200           # recent merged or closed PRs read to find stale worktrees
# Work-in-progress limits (AGENTS.md "Several agents or sessions", #412).
DRAFT_CAP = 3                # open release-path drafts at once
SPLIT_LINES = 1000           # lines changed outside tests/ before a PR is split
CONFLICT_DAYS = 2            # days a conflicting draft may go without a rebase
TEST_DIR = "tests/"
VIEW_FIELDS = ("number,state,isDraft,mergeStateStatus,headRefOid,statusCheckRollup,"
               "comments,reviews,autoMergeRequest")
GH_FILE_LIMIT = 100          # gh returns at most this many files per pull request
PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}
WAITING = {"", "PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", "REQUESTED"}
UNSET_TIME = "0001-"           # gh writes an unset time as 0001-01-01T00:00:00Z
# Review records, one per line (the finish-pr skill asks for them). A commit is a
# 7 to 40 character hex name not followed by a word character or "..", so a range's
# end is never read as its start; [^\w\n] keeps the label and the commit on one line.
_GAP = r"[^\w\n]*"
_SHA = r"[0-9a-f]{7,40}(?!\w|\.\.)"
REVIEWED = re.compile(rf"(?im)^{_GAP}(?:commit|range) reviewed{_GAP}:{_GAP}"
                      rf"(?:([0-9a-f]{{7,40}})\.\.)?({_SHA})")
RANGE_DIFF = re.compile(rf"(?im)^{_GAP}range-diff{_GAP}:{_GAP}([0-9a-f]{{7,40}})\.\.({_SHA})"
                        rf"{_GAP}identical\b")
NOT_CHECKED = ("Not checked here: the review's verdict and findings answered, unresolved "
               "review threads, code-scanning alerts, open owner decisions, the reason for "
               "any failing check that is not required, and the range-diff itself (a "
               "recorded one is taken as written) (AGENTS.md, Authority).")


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
    does for merging: a run not yet completed is the latest, then the latest
    start time, then the later entry."""
    latest = {}
    for order, item in enumerate(rollup or []):
        name = item.get("name") or item.get("context") or "?"
        started = item.get("startedAt") or ""
        unfinished = bool(item.get("status")) and item["status"].upper() != "COMPLETED"
        key = (unfinished, "" if started.startswith(UNSET_TIME) else started, order)
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
    """A path as the repository names it (relative to root, forward slashes), or
    None for a path outside the checkout. A relative path is read from root."""
    root = Path(root).resolve()
    try:
        path = str((root / path.replace("\\", "/")).resolve().relative_to(root))
    except ValueError:
        return None
    return path.replace("\\", "/")


def touching(prs, paths, root=ROOT):
    """{path: [PR numbers]} for the given paths, matched without regard to case
    (Windows file names are not case-sensitive); paths outside the checkout are
    left out."""
    found = {}
    for path in sorted({repo_path(p, root) for p in paths} - {None}):
        found[path] = sorted(pr["number"] for pr in prs
                             if path.lower() in {f.lower() for f in pr_files(pr)})
    return found


def notes_of(pr):
    """The text of a PR's comments and reviews."""
    return ([c.get("body") or "" for c in pr.get("comments") or []]
            + [r.get("body") or "" for r in pr.get("reviews") or []])


def same_commit(a, b):
    """Two abbreviations (7 or more characters) of one commit name."""
    a, b = a.lower(), b.lower()
    return a.startswith(b) or b.startswith(a)


def reviews_of(head, notes):
    """The recorded reviews that cover the head commit. A "Commit reviewed:" line
    naming one commit is a root. A range, "Range reviewed: old..new" (a review of
    the changes since old) or "Range-diff: old..new identical" (a rebase that
    changed nothing, as AGENTS.md "Authority" allows), covers new only when old
    is itself covered, so every commit traces back to a root. An author's note
    that only mentions the commit does not count, and the verdict is not read."""
    reviewed, carried = set(), []
    for text in notes:
        for m in REVIEWED.finditer(text):
            if m.group(1):
                carried.append((m.group(1), m.group(2).lower()))
            else:
                reviewed.add(m.group(2).lower())
        carried += [(m.group(1), m.group(2).lower()) for m in RANGE_DIFF.finditer(text)]
    grew = True
    while grew:
        grew = False
        for old, new in carried:
            if new not in reviewed and any(same_commit(old, r) for r in reviewed):
                reviewed.add(new)
                grew = True
    return sorted(r for r in reviewed if head.lower().startswith(r))


def assess(pr, required, notes):
    """(blockers, notes) for one PR. Blockers are what the repository shows still
    stops marking it ready and arming auto-merge under AGENTS.md "Authority" and
    "Delivery". Required checks still running are notes: GitHub waits for them
    once auto-merge is armed. Notes need a look before arming (a failing check
    that is not required needs its reason on the PR first)."""
    blockers, info = [], []
    state = pr.get("state", "OPEN")
    if state != "OPEN":
        return [f"the PR is {state.lower()}"], info
    if pr.get("isDraft"):
        info.append("draft: once nothing blocks it, run gh pr ready and arm auto-merge on "
                    "the reviewed head")
    failing, waiting, missing = check_summary(pr.get("statusCheckRollup"), required)
    blockers += [f"required check failing: {n}" for n in failing if n in required]
    info += [f"required check still running: {n} (GitHub waits for it once armed)"
             for n in waiting if n in required]
    blockers += [f"required check not reported: {n}" for n in missing]
    info += [f"check failing, not required: {n} (record the reason on the PR before "
             "arming: with the required checks green, arming merges at once)"
             for n in failing if n not in required]
    oid = pr.get("headRefOid") or ""
    head = oid[:7]
    reviewed = bool(oid) and reviews_of(oid, notes)
    if reviewed:
        info.append(f"a review of {head} is recorded; confirm it is independent and read "
                    "its verdict")
    elif oid:
        blockers.append(f"no 'Commit reviewed:' line names the head commit {head}: record "
                        "the independent review of this exact commit; a 'Range reviewed:' "
                        "or 'Range-diff: ... identical' line counts only from a commit that "
                        "is itself recorded as reviewed")
    merge = pr.get("mergeStateStatus", "")
    if merge in {"DIRTY", "BEHIND"}:
        blockers.append(f"merge state {merge}: rebase onto origin/{BASE} (a rebase keeps the "
                        "review only if git range-diff shows every commit '=')")
    elif merge == "UNKNOWN":
        blockers.append("merge state not worked out yet by GitHub: run --pr again shortly")
    elif (merge == "BLOCKED" and not blockers and not pr.get("isDraft")
          and not any(n in required for n in waiting)):
        blockers.append("GitHub still blocks the merge for a reason not read here, for "
                        "example unresolved review threads or a code-scanning alert")
    armed = pr.get("autoMergeRequest")
    number = pr.get("number", "<n>")
    unrequired_failing = [n for n in failing if n not in required]
    if armed and oid and not reviewed:
        # A push does not disarm auto-merge (seen on #391): GitHub would merge an
        # unreviewed head once the required checks pass.
        blockers.append(f"auto-merge is armed on the unreviewed head {head}: disarm now "
                        f"(gh pr merge {number} --disable-auto)")
    elif armed:
        info.append(f"auto-merge is armed: disarm (gh pr merge {number} --disable-auto) before any "
                    "push, and re-arm only on a reviewed head")
    elif oid and not blockers and not unrequired_failing:
        info.append(f"auto-merge not armed: gh pr merge {number} --auto "
                    f"--squash --match-head-commit {oid}")
    return blockers, info


def behind_ahead(counts):
    """(behind, ahead) from `git rev-list --left-right --count base...head`."""
    left, right = counts.split()
    return int(left), int(right)


# --- Work-in-progress limits (pure, tested with canned data) ------------------

def non_test_lines(pr):
    """Lines changed (added plus deleted) outside tests/, from gh's file list. For
    a PR past GH_FILE_LIMIT files this is a lower bound (capped() says so)."""
    return sum((f.get("additions") or 0) + (f.get("deletions") or 0)
               for f in pr.get("files") or []
               if not f.get("path", "").replace("\\", "/").startswith(TEST_DIR))


def days_since(stamp, now):
    """Days from an ISO 8601 time as gh writes it (2026-10-24T05:50:26Z) to now,
    or None for a missing or unset time."""
    if not stamp or stamp.startswith(UNSET_TIME):
        return None
    then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (now - then).total_seconds() / 86400


def last_commit_days(pr, now):
    """Days since the PR's latest commit was made (a rebase makes new ones), or
    None when its commits were not read."""
    stamps = [c.get("committedDate") or "" for c in pr.get("commits") or []]
    stamps = [s for s in stamps if s and not s.startswith(UNSET_TIME)]
    return days_since(max(stamps), now) if stamps else None


def conflict_state(pr):
    """"conflicting", "clean" or "unknown" against the base branch."""
    if pr.get("mergeable") == "CONFLICTING" or pr.get("mergeStateStatus") == "DIRTY":
        return "conflicting"
    return "clean" if pr.get("mergeable") == "MERGEABLE" else "unknown"


def _days(value):
    return "?" if value is None else f"{value:.1f}d"


def pr_shape(pr, now):
    """(summary, warnings) for one open PR under the work-in-progress limits: its
    size outside tests/, its age, its last commit's age and its conflict state,
    and what AGENTS.md asks for when a limit is passed."""
    lines = non_test_lines(pr)
    size = f"{lines:,}{'+' if capped(pr) else ''} non-test lines"
    opened = days_since(pr.get("createdAt"), now)
    pushed = last_commit_days(pr, now)
    conflict = conflict_state(pr)
    summary = ", ".join([size, f"opened {_days(opened)} ago"]
                        + ([f"last commit {_days(pushed)} ago"] if pushed is not None else [])
                        + ["conflicts with " + BASE if conflict == "conflicting"
                           else f"conflict state {conflict}"])
    warnings = []
    if lines > SPLIT_LINES:
        warnings.append(f"over about {SPLIT_LINES:,} non-test lines: split it, or say on the "
                        "PR why it cannot be")
    if (pr.get("isDraft") and conflict == "conflicting" and pushed is not None
            and pushed > CONFLICT_DAYS):
        warnings.append(f"conflicts with {BASE} and not rebased for over {CONFLICT_DAYS} days: "
                        "rebase it, split it, or park it with the reason on the PR")
    return summary, warnings


def draft_cap_note(prs):
    """A note when more drafts are open than AGENTS.md allows on the release path,
    or None. The script cannot tell a release-path draft from any other, so it
    counts every open draft and leaves that call to the reader."""
    drafts = sum(1 for pr in prs if pr.get("isDraft"))
    if drafts <= DRAFT_CAP:
        return None
    return (f"{drafts} open drafts: at most {DRAFT_CAP} may be on the release path at once; "
            "finish one before starting another")


def gone_branches(refs):
    """Local branches whose upstream has gone, from `git for-each-ref
    --format=%(refname:short)%09%(upstream:track) refs/heads`."""
    gone = set()
    for line in refs.splitlines():
        name, _, track = line.partition("\t")
        if track.strip() == "[gone]":
            gone.add(name.strip())
    return gone


def stale_worktrees(trees, open_prs, closed_prs, gone):
    """[(tree, reason)] for worktrees on a branch with no open PR whose PR has
    merged or closed, or whose origin branch has gone. A merged PR whose head is
    not the worktree's head is still named, with that said, since the worktree
    may hold newer work. The base branch and detached heads are left out."""
    open_branches = {pr["headRefName"] for pr in open_prs}
    by_branch = {}
    for pr in sorted(closed_prs, key=lambda p: p["number"]):
        by_branch.setdefault(pr["headRefName"], []).append(pr)
    stale = []
    for tree in trees:
        branch = tree.get("branch")
        if not branch or branch == BASE or branch in open_branches:
            continue
        closed = by_branch.get(branch, [])
        same = [pr for pr in closed if (pr.get("headRefOid") or "") == tree["head"]]
        if same or closed:
            pr = (same or closed)[-1]
            reason = f"PR #{pr['number']} {pr['state'].lower()}"
            if not same:
                reason += ", but the worktree's head is not that PR's head"
            stale.append((tree, reason))
        elif branch in gone:
            stale.append((tree, f"origin/{branch} has gone"))
    return stale


# --- Output -------------------------------------------------------------------

def short_path(path):
    home = str(Path.home()).replace("\\", "/").rstrip("/")
    path = path.replace("\\", "/")
    if path.lower() == home.lower() or path.lower().startswith(home.lower() + "/"):
        return "~" + path[len(home):]
    return path


def show_status(prs, required, closed_prs, gone, now):
    by_branch = {pr["headRefName"]: pr["number"] for pr in prs}
    trees = parse_worktrees(run(["git", "worktree", "list", "--porcelain"]))
    print(f"Worktrees (behind/ahead of origin/{BASE}, uncommitted file count):")
    for tree in trees:
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
    show_limits(prs, trees, closed_prs, gone, now)


def show_limits(prs, trees, closed_prs, gone, now):
    """The work-in-progress limits of AGENTS.md "Several agents or sessions"."""
    drafts = sorted((pr for pr in prs if pr.get("isDraft")), key=lambda p: p["number"])
    print(f"\nOpen drafts ({len(drafts)}; at most {DRAFT_CAP} on the release path, split over "
          f"about {SPLIT_LINES:,} non-test lines, rebase a conflict within {CONFLICT_DAYS} days):")
    note = draft_cap_note(prs)
    if note:
        print(f"  ! {note}")
    for pr in sorted(prs, key=lambda p: p["number"]):
        if pr.get("isDraft"):
            pr = {**pr, **gh_json(["pr", "view", str(pr["number"]), "--json", DRAFT_FIELDS])}
        summary, warnings = pr_shape(pr, now)
        if pr.get("isDraft") or warnings:
            print(f"  #{pr['number']} {'draft' if pr.get('isDraft') else 'ready'}: {summary}")
            for warning in warnings:
                print(f"      ! {warning}")
    detached = [t for t in trees if not t["branch"]]
    stale = stale_worktrees(trees, prs, closed_prs, gone)
    print(f"\nWorktrees to tidy ({len(trees)} in all, {len(detached)} detached, {len(stale)} "
          "stale; remove with git worktree remove once nothing in them is needed):")
    for tree, reason in stale:
        print(f"  {short_path(tree['path'])}  {tree['branch']}  {reason}")
    if detached:
        print(f"  {len(detached)} detached (review or scratch checkouts), listed under "
              "Worktrees above")


def show_files(prs, paths):
    for path in paths:
        if repo_path(path) is None:
            print(f"  {short_path(path)}: outside this checkout")
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
    verdict = (f"{len(blockers)} to resolve" if blockers else
               "no blocker found; read the notes" if info else "nothing found that stops it")
    print(f"PR #{number} at {pr['headRefOid'][:7]}: {verdict}")
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
        closed = gh_json(["pr", "list", "--state", "closed", "--limit", str(CLOSED_LIMIT),
                          "--json", CLOSED_FIELDS])
        gone = gone_branches(run(["git", "for-each-ref",
                                  "--format=%(refname:short)%09%(upstream:track)",
                                  "refs/heads"]))
        show_status(prs, required_checks(), closed, gone, datetime.now(UTC))
    return 0


if __name__ == "__main__":
    sys.exit(main())
