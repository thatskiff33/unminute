# Tests for the agent workflow tooling: tools/dev_status.py's analysis, with
# canned git and gh output (no network), and the shared skills in .agents/skills,
# each with a Claude Code pointer in .claude/skills that matches it. Stdlib only.
#   python -m unittest discover -s tests -v

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import dev_status  # noqa: E402

SHARED = ROOT / ".agents" / "skills"
CLAUDE = ROOT / ".claude" / "skills"
REQUIRED = ["Ruff", "Unit tests (Windows)"]


def check(name, conclusion=None, status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name, "conclusion": conclusion, "status": status}


def pr(number, files=(), **fields):
    return {"number": number, "title": f"PR {number}", "headRefName": f"b{number}",
            "isDraft": False, "mergeStateStatus": "CLEAN", "headRefOid": "abcdef1234",
            "files": [{"path": f} for f in files], "statusCheckRollup": [], **fields}


def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    assert match, f"{path} has no frontmatter"
    fields = dict(line.split(": ", 1) for line in match.group(1).splitlines())
    return fields, match.group(2)


class WorktreeTests(unittest.TestCase):
    def test_branches_and_detached_heads(self):
        porcelain = ("worktree C:/r/unminute\nHEAD aaa\nbranch refs/heads/main\n\n"
                     "worktree C:/r/review\nHEAD bbb\ndetached\n\n"
                     "worktree C:/r/feat\nHEAD ccc\nbranch refs/heads/feat/x\n")
        self.assertEqual([{"path": "C:/r/unminute", "head": "aaa", "branch": "main"},
                          {"path": "C:/r/review", "head": "bbb", "branch": None},
                          {"path": "C:/r/feat", "head": "ccc", "branch": "feat/x"}],
                         dev_status.parse_worktrees(porcelain))

    def test_behind_and_ahead(self):
        self.assertEqual((22, 10), dev_status.behind_ahead("22\t10\n"))


class CheckTests(unittest.TestCase):
    def test_states_for_runs_and_contexts(self):
        rollup = [check("Ruff", "SUCCESS"), check("Lint", "SKIPPED"),
                  check("Unit tests (Windows)", None, "IN_PROGRESS"),
                  check("Syntax check", "FAILURE"), check("Old", "CANCELLED"),
                  {"__typename": "StatusContext", "context": "ext", "state": "PENDING"},
                  {"__typename": "StatusContext", "context": "ok", "state": "SUCCESS"},
                  {"__typename": "StatusContext", "context": "bad", "state": "ERROR"}]
        self.assertEqual({"Ruff": "SUCCESS", "Lint": "SUCCESS",
                          "Unit tests (Windows)": "WAITING", "Syntax check": "FAILURE",
                          "Old": "FAILURE", "ext": "WAITING", "ok": "SUCCESS",
                          "bad": "FAILURE"},
                         dev_status.check_states(rollup))

    def test_a_rerun_check_keeps_its_latest_run(self):
        # #364 review: a re-run that passes (a label added for Changelog entry, a
        # flaky job) is what GitHub merges on, whatever order gh lists them in.
        early = {**check("Ruff", "FAILURE"), "startedAt": "2026-10-25T13:26:00Z"}
        late = {**check("Ruff", "SUCCESS"), "startedAt": "2026-10-25T14:00:00Z"}
        for rollup in ([early, late], [late, early]):
            self.assertEqual({"Ruff": "SUCCESS"}, dev_status.check_states(rollup))
        rerunning = {**check("Ruff", None, "IN_PROGRESS"), "startedAt": "2026-10-25T15:00:00Z"}
        self.assertEqual({"Ruff": "WAITING"}, dev_status.check_states([late, rerunning]))
        # A queued re-run has no start time yet; gh writes it as 0001-01-01 (#364
        # review round 2, F2). It is still the run GitHub waits for.
        queued = {**check("Ruff", None, "QUEUED"), "startedAt": "0001-01-01T00:00:00Z",
                  "completedAt": "0001-01-01T00:00:00Z"}
        for rollup in ([early, late, queued], [queued, late, early]):
            self.assertEqual({"Ruff": "WAITING"}, dev_status.check_states(rollup))

    def test_summary_names_required_checks_that_never_reported(self):
        failing, waiting, missing = dev_status.check_summary([check("Ruff", "FAILURE")],
                                                             REQUIRED)
        self.assertEqual((["Ruff"], [], ["Unit tests (Windows)"]), (failing, waiting, missing))

    def test_required_checks_from_the_rules_api(self):
        rules = [{"type": "pull_request", "parameters": {}},
                 {"type": "required_status_checks", "parameters": {
                     "required_status_checks": [{"context": "Ruff"}, {"context": "Unit"}]}},
                 {"type": "code_scanning", "parameters": {}}]
        self.assertEqual(["Ruff", "Unit"], dev_status.parse_required(rules))


class OverlapTests(unittest.TestCase):
    PRS = [pr(1, ["AGENTS.md", "a.py"]), pr(2, ["AGENTS.md", "b.py"]), pr(3, ["a.py"])]

    def test_paths_two_or_more_prs_change(self):
        self.assertEqual({"AGENTS.md": [1, 2], "a.py": [1, 3]}, dev_status.overlaps(self.PRS))

    def test_touching_normalises_paths(self):
        root = Path("C:/r/unminute") if sys.platform == "win32" else Path("/r/unminute")
        self.assertEqual({"a.py": [1, 3], "agents.md": [1, 2], "docs/x.md": []},
                         dev_status.touching(self.PRS, [r".\a.py", r"docs\x.md", "agents.md",
                                                        str(root / "a.py")], root))

    def test_a_path_outside_the_checkout_is_named_as_such(self):
        root = Path("C:/r/unminute") if sys.platform == "win32" else Path("/r/unminute")
        elsewhere = str(root.parent / "unminute-other" / "a.py")
        for path in (elsewhere, "../unminute-other/a.py", r"..\unminute-other\a.py"):
            with self.subTest(path=path):
                self.assertIsNone(dev_status.repo_path(path, root))
        self.assertEqual({}, dev_status.touching(self.PRS, [elsewhere], root))
        self.assertEqual("a.py", dev_status.repo_path("tools/../a.py", root))

    def test_short_path_shortens_only_the_home_folder_itself(self):
        home = str(Path.home()).replace("\\", "/")
        self.assertEqual("~/x/y", dev_status.short_path(home + "/x/y"))
        self.assertEqual(home + "X/y", dev_status.short_path(home + "X/y"))

    def test_a_pr_past_the_file_limit_is_flagged(self):
        self.assertTrue(dev_status.capped(pr(4, [f"f{i}" for i in range(100)])))
        self.assertFalse(dev_status.capped(pr(5, ["a.py"])))


class AssessTests(unittest.TestCase):
    GREEN = [check(n, "SUCCESS") for n in REQUIRED]
    REVIEW = ["## Independent review\n\n- **Commit reviewed:** `abcdef1234` against base `x`."]

    def test_a_clean_reviewed_pr_has_no_blockers(self):
        blockers, info = dev_status.assess(pr(9, statusCheckRollup=self.GREEN), REQUIRED,
                                           self.REVIEW)
        self.assertEqual([], blockers)
        self.assertIn("confirm it is independent and read its verdict", " ".join(info))

    def test_only_a_recorded_review_line_counts(self):
        # #364 review round 2, F1: the author's own note naming the commit is not a
        # review of it.
        for notes in (["Response (abcdef1): fixed"], ["Pushed abcdef1, reviewed later"],
                      ["**Commit reviewed:** `1234567` against base `x`."],
                      # round 3, N5: the label must lead the line and the head must be
                      # the commit it names (the end of a range).
                      ["| F1 | Fixed in abcdef1: only a `Commit reviewed:` line counts |"],
                      ["Commit reviewed: none yet (abcdef1 awaits review)"],
                      ["**Commit reviewed:** `1234567` (head abcdef1 not yet)"],
                      ["**Range reviewed:** `abcdef1..1234567`"],
                      ["Range-diff: 1234567..abcdef1 shows two commits changed"],
                      # round 4: a range starting at the head, by backtracking (F-C) ...
                      ["Range reviewed: abcdef1...1234567"], ["Range reviewed: `abcdef1..HEAD`"],
                      ["Range reviewed: abcdef1..123456"],
                      # ... the label and the commit on different lines (F-D) ...
                      ["Commit reviewed:\nabcdef1 pushed, review to follow"],
                      ["**Commit reviewed:**\n\n- abcdef1 awaits"],
                      # ... a commit that only shares the first 7 characters (N-2) ...
                      ["Commit reviewed: abcdef1999"],
                      # ... "identical" not straight after the range (F-B) ...
                      ["Commit reviewed: 1234567", "Range-diff: 1234567..abcdef1 not identical"],
                      ["Commit reviewed: 1234567",
                       "Range-diff: 1234567..abcdef1 commit 2 changed, the others identical"],
                      # ... and a range-diff or a range review from a commit nobody
                      # reviewed (F-A, round 5 R5-3).
                      ["Range-diff: `0000000..abcdef1` identical"],
                      ["**Range reviewed:** `1234567..abcdef1`"]):
            with self.subTest(notes=notes):
                blockers, _ = dev_status.assess(pr(9, statusCheckRollup=self.GREEN),
                                                REQUIRED, notes)
                self.assertIn("no 'Commit reviewed:' line names the head commit abcdef1",
                              " ".join(blockers))
        for notes in (["**Commit reviewed:** `1234567`", "**Range reviewed:** `1234567..abcdef1`"],
                      ["- **Commit reviewed:** `ABCDEF1234` against base `x`."],
                      ["**Commit reviewed**: abcdef1234\r\n"],
                      ["Commit reviewed: abcdef1."],      # end of a sentence (R5-1)
                      # round 3, N6: a rebase that range-diff shows unchanged keeps the
                      # review of the commit it started from (AGENTS.md, Authority) ...
                      ["**Commit reviewed:** `9999999aaa`",
                       "Range-diff: `9999999..abcdef1` identical, every commit '='"],
                      # ... also across two rebases.
                      ["Commit reviewed: 1111111", "Range-diff: 1111111..2222222 identical",
                       "Range-diff: 2222222..abcdef1234 identical"]):
            with self.subTest(notes=notes):
                blockers, _ = dev_status.assess(pr(9, statusCheckRollup=self.GREEN),
                                                REQUIRED, notes)
                self.assertEqual([], blockers)

    def test_a_full_head_commit_matches_a_full_review_line(self):
        oid = "abcdef1234567890abcdef1234567890abcdef12"
        blockers, _ = dev_status.assess(pr(9, headRefOid=oid, statusCheckRollup=self.GREEN),
                                        REQUIRED, [f"**Commit reviewed:** `{oid}`"])
        self.assertEqual([], blockers)

    def test_draft_and_unrequired_failures_are_notes_not_blockers(self):
        # #364 review: otherwise "run gh pr ready when nothing blocks it" never arrives.
        rollup = self.GREEN + [check("Coverage", "FAILURE")]
        blockers, info = dev_status.assess(pr(9, isDraft=True, mergeStateStatus="BLOCKED",
                                              statusCheckRollup=rollup), REQUIRED, self.REVIEW)
        self.assertEqual([], blockers)
        self.assertIn("draft", info[0])
        self.assertIn("check failing, not required: Coverage", " ".join(info))

    def test_each_blocker_is_named(self):
        stuck = pr(9, mergeStateStatus="DIRTY",
                   statusCheckRollup=[check("Ruff", "FAILURE"),
                                      check("Unit tests (Windows)", None, "QUEUED")])
        blockers, _ = dev_status.assess(stuck, REQUIRED, ["Review of 1234567"])
        found = " | ".join(blockers)
        for text in ("required check failing: Ruff",
                     "no 'Commit reviewed:' line names the head commit abcdef1",
                     "merge state DIRTY", "range-diff"):
            with self.subTest(text=text):
                self.assertIn(text, found)

    def test_a_running_required_check_does_not_stop_arming(self):
        # 26 Oct 2026: review while CI runs, then arm auto-merge; GitHub waits for the
        # required checks, so a running one is a note, not a blocker.
        oid = "abcdef1234567890abcdef1234567890abcdef12"
        rollup = [check("Ruff", "SUCCESS"), check("Unit tests (Windows)", None, "IN_PROGRESS")]
        blockers, info = dev_status.assess(pr(9, isDraft=True, mergeStateStatus="BLOCKED",
                                              headRefOid=oid, statusCheckRollup=rollup),
                                           REQUIRED, [f"Commit reviewed: {oid}"])
        self.assertEqual([], blockers)
        joined = " | ".join(info)
        self.assertIn("required check still running: Unit tests (Windows)", joined)
        self.assertIn(f"--auto --squash --match-head-commit {oid}", joined)

    def test_an_armed_pr_says_to_disarm_before_pushing(self):
        _, info = dev_status.assess(pr(9, statusCheckRollup=self.GREEN,
                                       autoMergeRequest={"mergeMethod": "SQUASH"}),
                                    REQUIRED, self.REVIEW)
        self.assertIn("--disable-auto", " ".join(info))
        self.assertNotIn("auto-merge not armed", " ".join(info))

    def test_armed_on_an_unreviewed_head_is_a_blocker(self):
        # #394: a push after arming leaves auto-merge armed on a head nobody reviewed.
        blockers, info = dev_status.assess(pr(9, statusCheckRollup=self.GREEN,
                                              autoMergeRequest={"mergeMethod": "SQUASH"}),
                                           REQUIRED, [])
        self.assertIn("auto-merge is armed on the unreviewed head abcdef1: disarm now "
                      "(gh pr merge 9 --disable-auto)", blockers)
        self.assertNotIn("re-arm only on a reviewed head", " ".join(info))

    def test_no_arm_command_while_an_unrequired_check_fails(self):
        # #394: with the required checks green, arming would merge at once.
        rollup = self.GREEN + [check("Coverage", "FAILURE")]
        blockers, info = dev_status.assess(pr(9, statusCheckRollup=rollup), REQUIRED,
                                           self.REVIEW)
        self.assertEqual([], blockers)
        self.assertNotIn("auto-merge not armed", " ".join(info))
        self.assertIn("record the reason on the PR before arming", " ".join(info))

    def test_github_blocking_for_a_reason_not_read_here_is_reported(self):
        for merge, text in (("BLOCKED", "unresolved review threads"),
                            ("UNKNOWN", "not worked out yet")):
            with self.subTest(merge=merge):
                blockers, _ = dev_status.assess(pr(9, mergeStateStatus=merge,
                                                   statusCheckRollup=self.GREEN),
                                                REQUIRED, self.REVIEW)
                self.assertEqual(1, len(blockers))
                self.assertIn(text, blockers[0])

    def test_a_closed_or_merged_pr_is_not_ready(self):
        blockers, _ = dev_status.assess(pr(9, state="MERGED"), REQUIRED, [])
        self.assertEqual(["the PR is merged"], blockers)

    def test_notes_come_from_comments_and_reviews(self):
        view = {"comments": [{"body": "a"}, {"body": None}], "reviews": [{"body": "b"}]}
        self.assertEqual(["a", "", "b"], dev_status.notes_of(view))

    def test_the_output_says_what_it_does_not_check(self):
        for text in ("verdict", "unresolved review threads", "code-scanning",
                     "owner decisions", "range-diff"):
            with self.subTest(text=text):
                self.assertIn(text, dev_status.NOT_CHECKED)


class LimitTests(unittest.TestCase):
    """The work-in-progress limits of AGENTS.md "Several agents or sessions" (#412)."""
    NOW = dev_status.datetime(2026, 10, 27, 0, 0, tzinfo=dev_status.UTC)

    @staticmethod
    def sized(number, lines, tests=0, **fields):
        files = [{"path": "workspace/a.py", "additions": lines - 10, "deletions": 10},
                 {"path": "tests/test_a.py", "additions": tests, "deletions": 0},
                 {"path": r"tests\fixtures\b.txt", "additions": tests, "deletions": 0}]
        return {**pr(number, createdAt="2026-10-23T00:00:00Z", **fields), "files": files}

    @staticmethod
    def commits(*dates):
        return [{"committedDate": d} for d in dates]

    def test_size_counts_lines_changed_outside_tests(self):
        self.assertEqual(1200, dev_status.non_test_lines(self.sized(1, 1200, tests=5000)))
        self.assertEqual(0, dev_status.non_test_lines(pr(2)))

    def test_age_in_days_and_unset_times(self):
        self.assertEqual(4.0, dev_status.days_since("2026-10-23T00:00:00Z", self.NOW))
        for stamp in ("", None, "0001-01-01T00:00:00Z"):
            self.assertIsNone(dev_status.days_since(stamp, self.NOW))
        latest = {"commits": self.commits("2026-10-20T00:00:00Z", "2026-10-24T12:00:00Z",
                                          "0001-01-01T00:00:00Z")}
        self.assertEqual(2.5, dev_status.last_commit_days(latest, self.NOW))
        self.assertIsNone(dev_status.last_commit_days({}, self.NOW))

    def test_a_large_draft_conflicting_for_days_gets_both_warnings(self):
        stale = self.sized(1, 1500, isDraft=True, mergeable="CONFLICTING",
                           commits=self.commits("2026-10-24T00:00:00Z"))
        summary, warnings = dev_status.pr_shape(stale, self.NOW)
        self.assertEqual("1,500 non-test lines, opened 4.0d ago, last commit 3.0d ago, "
                         "conflicts with main", summary)
        self.assertEqual(2, len(warnings))
        self.assertIn("split it, or say on the PR why it cannot be", warnings[0])
        self.assertIn("rebase it, split it, or park it with the reason on the PR", warnings[1])

    def test_limits_are_not_flagged_before_they_are_passed(self):
        cases = [  # at the size limit, conflicting but rebased a day ago, DIRTY but
                   # not a draft (the rebase rule is for drafts), and commits not read
            self.sized(1, 1000, tests=9000, isDraft=True, mergeable="MERGEABLE",
                       commits=self.commits("2026-10-20T00:00:00Z")),
            self.sized(2, 50, isDraft=True, mergeable="CONFLICTING",
                       commits=self.commits("2026-10-26T00:00:00Z")),
            self.sized(3, 50, mergeStateStatus="DIRTY",
                       commits=self.commits("2026-10-20T00:00:00Z")),
            self.sized(4, 50, isDraft=True, mergeable="CONFLICTING")]
        for case in cases:
            with self.subTest(pr=case["number"]):
                summary, warnings = dev_status.pr_shape(case, self.NOW)
                self.assertEqual([], warnings)
        self.assertIn("conflict state clean", dev_status.pr_shape(cases[0], self.NOW)[0])
        self.assertIn("conflicts with main", dev_status.pr_shape(cases[2], self.NOW)[0])
        self.assertNotIn("last commit", dev_status.pr_shape(cases[3], self.NOW)[0])
        self.assertIn("conflict state unknown", dev_status.pr_shape(pr(5), self.NOW)[0])

    def test_a_pr_past_the_file_limit_gives_a_lower_bound(self):
        big = pr(6, [f"f{i}" for i in range(100)])
        self.assertTrue(dev_status.pr_shape(big, self.NOW)[0].startswith("0+ non-test lines"))

    def test_more_than_three_open_drafts_is_noted(self):
        self.assertIsNone(dev_status.draft_cap_note([pr(n, isDraft=True) for n in range(3)]
                                                    + [pr(9)]))
        note = dev_status.draft_cap_note([pr(n, isDraft=True) for n in range(4)])
        self.assertIn("4 open drafts: at most 3", note)

    def test_branches_whose_upstream_has_gone(self):
        refs = "main\t\nfix/a\t[gone]\nfix/b\t[behind 3]\nfix/c\t[ahead 1, behind 2]\n"
        self.assertEqual({"fix/a"}, dev_status.gone_branches(refs))

    def test_stale_and_detached_worktrees(self):
        def tree(name, branch, head="aaa"):
            return {"path": f"C:/r/{name}", "head": head, "branch": branch}
        trees = [tree("main", "main"), tree("merged", "fix/merged"),
                 tree("newer", "fix/newer", head="ccc"), tree("closed", "fix/closed"),
                 tree("gone", "fix/gone"), tree("open", "fix/open"), tree("live", "fix/live"),
                 tree("review", None)]
        closed = [{"number": 1, "headRefName": "fix/merged", "headRefOid": "zzz",
                   "state": "MERGED"},
                  {"number": 2, "headRefName": "fix/merged", "headRefOid": "aaa",
                   "state": "MERGED"},
                  {"number": 3, "headRefName": "fix/newer", "headRefOid": "bbb",
                   "state": "MERGED"},
                  {"number": 4, "headRefName": "fix/closed", "headRefOid": "aaa",
                   "state": "CLOSED"},
                  {"number": 5, "headRefName": "fix/open", "headRefOid": "aaa",
                   "state": "CLOSED"},
                  {"number": 6, "headRefName": "main", "headRefOid": "aaa", "state": "MERGED"}]
        open_prs = [pr(7, headRefName="fix/open")]
        stale = dev_status.stale_worktrees(trees, open_prs, closed, {"fix/gone", "fix/open"})
        self.assertEqual([("C:/r/merged", "PR #2 merged"),
                          ("C:/r/newer", "PR #3 merged, but the worktree's head is not that "
                                         "PR's head"),
                          ("C:/r/closed", "PR #4 closed"),
                          ("C:/r/gone", "origin/fix/gone has gone")],
                         [(t["path"], reason) for t, reason in stale])


class RegisterTests(unittest.TestCase):
    """#411: the release register is printed from the issues and PRs themselves,
    not kept by hand in a #121 comment."""

    def test_gate_and_next_step_lines(self):
        body = ("Some text.\n\n**Release gate:** next preview\n- Next step: the owner\n"
                "Release gate: a second line is ignored\n")
        self.assertEqual(("next preview", "the owner"), dev_status.register_fields(body))
        self.assertEqual(("Later", None), dev_status.register_fields("* Release gate: Later"))
        self.assertEqual((None, None), dev_status.register_fields(
            "The release gate: mid-sentence is not a register line\n"))
        self.assertEqual((None, None), dev_status.register_fields(None))

    def test_rows_name_the_gate_and_the_state(self):
        issues = [{"number": 9, "title": "Later work", "body": "Release gate: Later"},
                  {"number": 5, "title": "Blocker", "body": "Release gate: next preview\n"
                                                             "Next step: orchestrator"},
                  {"number": 7, "title": "Not gated", "body": "Nothing here"}]
        prs = [pr(20, body="Closes #5"),
               pr(21, body="Fixes #5.\nRelease gate: next preview", isDraft=True,
                  statusCheckRollup=[check("Ruff", "FAILURE")])]
        rows = dev_status.register_rows(issues, prs, REQUIRED)
        self.assertEqual([("issue", 5), ("issue", 9), ("PR", 21)],
                         [(r["kind"], r["number"]) for r in rows])
        self.assertEqual("open PR #20 #21", rows[0]["state"])
        self.assertEqual("orchestrator", rows[0]["next"])
        self.assertEqual("no open PR closes it", rows[1]["state"])
        self.assertEqual("not named", rows[1]["next"])
        self.assertEqual("draft; clean; failing: Ruff; 1 required not reported",
                         rows[2]["state"])

    def test_closing_words_link_an_issue_only_by_its_number(self):
        prs = [pr(30, body="Part of #5; resolves #50"), pr(31, body="closed #5")]
        rows = dev_status.register_rows(
            [{"number": 5, "title": "x", "body": "Release gate: Later"}], prs, REQUIRED)
        self.assertEqual("open PR #31", rows[0]["state"])


class CiTimeTests(unittest.TestCase):
    """#411: AGENTS.md points at the measured CI time instead of a fixed figure."""

    RUN = {"id": 1, "run_attempt": 1, "created_at": "2026-10-27T02:00:00Z"}

    def test_minutes_from_push_to_the_required_job_end(self):
        jobs = [{"name": "Windows tests, chunk 1 of 8", "conclusion": "success",
                 "completed_at": "2026-10-27T02:09:00Z"},
                {"name": "Unit tests (Windows)", "conclusion": "success",
                 "completed_at": "2026-10-27T02:04:30Z"}]
        self.assertEqual(4.5, dev_status.ci_minutes(self.RUN, jobs))

    def test_reruns_failures_and_missing_jobs_are_not_measured(self):
        done = {"name": "Unit tests (Windows)", "conclusion": "success",
                "completed_at": "2026-10-27T02:04:30Z"}
        self.assertIsNone(dev_status.ci_minutes({**self.RUN, "run_attempt": 2}, [done]))
        self.assertIsNone(dev_status.ci_minutes(self.RUN, [{**done, "conclusion": "failure"}]))
        self.assertIsNone(dev_status.ci_minutes(self.RUN, [{**done, "name": "Ruff"}]))

    def test_median(self):
        self.assertEqual(4, dev_status.median([5, 3, 4]))
        self.assertEqual(3.5, dev_status.median([4, 3]))

    def test_the_job_measured_is_the_required_one(self):
        workflow = (ROOT / ".github" / "workflows" / dev_status.CI_WORKFLOW).read_text(
            encoding="utf-8")
        self.assertIn(f"name: {dev_status.CI_TIME_JOB}\n", workflow)
        self.assertIn("python tools/dev_status.py --ci-time",
                      (ROOT / "AGENTS.md").read_text(encoding="utf-8"))


class SkillTests(unittest.TestCase):
    def test_each_shared_skill_has_a_matching_claude_pointer(self):
        shared = sorted(p.name for p in SHARED.iterdir() if p.is_dir())
        claude = sorted(p.name for p in CLAUDE.iterdir() if p.is_dir())
        self.assertTrue(shared, "no shared skills found")
        self.assertEqual(shared, claude, "every skill lives in .agents/skills with a "
                                         "Claude Code pointer of the same name")
        for name in shared:
            with self.subTest(skill=name):
                fields, _ = frontmatter(SHARED / name / "SKILL.md")
                pointer, body = frontmatter(CLAUDE / name / "SKILL.md")
                self.assertEqual(name, fields.get("name"))
                self.assertTrue(fields.get("description"))
                self.assertEqual(fields, pointer, "the pointer repeats the shared frontmatter")
                self.assertIn(f"`.agents/skills/{name}/SKILL.md`", body)
                self.assertLess(len(body), 300, "the pointer holds no steps of its own")

    def test_paths_the_skills_name_exist(self):
        for path in SHARED.glob("*/SKILL.md"):
            text = path.read_text(encoding="utf-8")
            for token in re.findall(r"`([^`\s]+\.(?:py|md|json))`", text):
                with self.subTest(skill=path.parent.name, path=token):
                    self.assertTrue((ROOT / token).exists(), f"{token} does not exist")


if __name__ == "__main__":
    unittest.main()
