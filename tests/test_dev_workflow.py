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
                  {"__typename": "StatusContext", "context": "ext", "state": "PENDING"}]
        self.assertEqual({"Ruff": "SUCCESS", "Lint": "SUCCESS",
                          "Unit tests (Windows)": "WAITING", "Syntax check": "FAILURE",
                          "Old": "FAILURE", "ext": "WAITING"},
                         dev_status.check_states(rollup))

    def test_a_rerun_check_keeps_its_worst_state(self):
        rollup = [check("Ruff", "FAILURE"), check("Ruff", "SUCCESS")]
        self.assertEqual({"Ruff": "FAILURE"}, dev_status.check_states(rollup))

    def test_summary_names_required_checks_that_never_reported(self):
        failing, waiting, missing = dev_status.check_summary([check("Ruff", "FAILURE")],
                                                             REQUIRED)
        self.assertEqual((["Ruff"], [], ["Unit tests (Windows)"]), (failing, waiting, missing))


class OverlapTests(unittest.TestCase):
    PRS = [pr(1, ["AGENTS.md", "a.py"]), pr(2, ["AGENTS.md", "b.py"]), pr(3, ["a.py"])]

    def test_paths_two_or_more_prs_change(self):
        self.assertEqual({"AGENTS.md": [1, 2], "a.py": [1, 3]}, dev_status.overlaps(self.PRS))

    def test_touching_normalises_paths(self):
        self.assertEqual({"a.py": [1, 3], "docs/x.md": []},
                         dev_status.touching(self.PRS, [".\\a.py", "docs\\x.md"]))


class BlockerTests(unittest.TestCase):
    def test_a_clean_reviewed_pr_has_none(self):
        ready = pr(9, statusCheckRollup=[check(n, "SUCCESS") for n in REQUIRED])
        self.assertEqual([], dev_status.blockers(ready, REQUIRED,
                                                 ["Review of abcdef1: approve"]))

    def test_each_blocker_is_named(self):
        stuck = pr(9, isDraft=True, mergeStateStatus="DIRTY",
                   statusCheckRollup=[check("Ruff", "FAILURE"), check("Coverage", "FAILURE")])
        found = "\n".join(dev_status.blockers(stuck, REQUIRED, ["Review of 1234567"]))
        for text in ("draft", "merge state DIRTY", "range-diff",
                     "check failing: Ruff (required)",
                     "check failing: Coverage (not required: record the reason",
                     "required check not reported: Unit tests (Windows)",
                     "no comment or review names the head commit abcdef1"):
            with self.subTest(text=text):
                self.assertIn(text, found)

    def test_a_running_required_check_blocks(self):
        running = pr(9, statusCheckRollup=[check("Ruff", "SUCCESS"),
                                           check("Unit tests (Windows)", None, "QUEUED")])
        self.assertEqual(["check still running: Unit tests (Windows)"],
                         dev_status.blockers(running, REQUIRED, ["abcdef1"]))


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
