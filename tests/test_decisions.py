# The decisions log (#365 part D): docs/DECISIONS.md lists each decision the user
# or the owner made, with its date, who, the decision, its source and what it
# supersedes. Every mention of such a decision in the Markdown docs (AGENTS.md
# included) cites its ID, and every cited ID is in the log.
#   python tools/run_tests.py --only test_decisions

import re
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = "docs/DECISIONS.md"
ID = re.compile(r"\bDEC-\d{3}\b")
ROW = re.compile(r"^\| (DEC-\d{3}) \|(.*)\|\s*$", re.MULTILINE)
# A mention of a decision: "the owner's decision", "the user's approval",
# "owner decisions" and the like, in any case.
MENTION = re.compile(r"\b(?:owner|user)(?:'s|s')? (?:decisions?|approval)\b"
                     r"|\b(?:owner|user) decisions?\b", re.IGNORECASE)
# Mentions that state a rule about decisions rather than cite one. Each is the
# whole phrase, whitespace collapsed; adding one needs its reason in the PR.
GENERIC = (
    "no owner decision is open",              # finish-pr skill
    "open owner decisions",                   # finish-pr and session-handoff skills
    "no user decision is open",               # AGENTS.md "Delivery"
    "behind the owner's approval",            # CONTROL_MATRIX.md, the release control
    "only by the owner's decision",           # RELEASE_STANDARD.md, accessibility rule
    "needs the owner's approval first",       # RELEASE_STANDARD.md, accessibility rule
    "needs the owner's decision first",       # RELEASE_STANDARD.md, accessibility rule
    "waits for the owner's approval",         # RELEASING.md, the release environment
    "only with the owner's approval",         # SECURITY_MODEL.md, the release control
)


def collapse(text):
    return re.sub(r"\s+", " ", text)


def markdown_docs():
    """The Markdown files the docs tests read (tests/test_consistency.py), except the log."""
    skip = {".git", "node_modules", "changelog.d", "build", "dist", "extracted", ".venv", "venv",
            "env", "__pycache__", "worktrees"}
    return sorted(p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.md")
                  if not skip & set(p.relative_to(ROOT).parts) and p.name != "CHANGELOG.md"
                  and p.relative_to(ROOT).as_posix() != LOG)


def uncited(text):
    """The mentions in text that cite no decision ID and are not a generic rule, with a
    little context: [(mention, context)]."""
    flat = collapse(text)
    found = []
    for m in MENTION.finditer(flat):
        if re.match(r"\s*DEC-\d{3}\b", flat[m.end():]):
            continue
        window = flat[max(0, m.start() - 40):m.end() + 40].lower()
        if any(g in window and g.find(m.group(0).lower()) != -1 for g in GENERIC):
            continue
        found.append((m.group(0), flat[max(0, m.start() - 60):m.end() + 60]))
    return found


def log_rows():
    """{ID: [date, who, decision, source, supersedes]} from the log's table."""
    rows = {}
    for ident, rest in ROW.findall((ROOT / LOG).read_text(encoding="utf-8")):
        if ident in rows:
            raise AssertionError(f"two rows for {ident}")
        rows[ident] = [c.strip() for c in rest.split(" | ")]
    return rows


class DecisionLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = log_rows()

    def test_each_entry_has_its_six_fields(self):
        self.assertGreaterEqual(len(self.rows), 50, "the row pattern no longer matches")
        for ident, cells in self.rows.items():
            with self.subTest(decision=ident):
                self.assertEqual(len(cells), 5, f"{ident}: ID, date, who, decision, source, "
                                                "supersedes")
                date, who, decision, source, supersedes = cells
                datetime.strptime(date, "%d %b %Y")
                self.assertIn(who, ("Owner", "User"))
                self.assertTrue(decision)
                self.assertRegex(source, r"\]\(https://github\.com/thatskiff33/unminute/"
                                         r"(?:issues|pull)/\d+")
                self.assertTrue(supersedes, f"{ident}: say None when it supersedes nothing")

    def test_ids_are_numbered_in_order_with_no_gaps(self):
        numbers = [int(i[4:]) for i in self.rows]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

    def test_ids_the_log_names_exist(self):
        text = (ROOT / LOG).read_text(encoding="utf-8")
        self.assertEqual(set(), set(ID.findall(text)) - set(self.rows))

    def test_agents_md_links_the_log(self):
        self.assertIn("docs/DECISIONS.md", (ROOT / "AGENTS.md").read_text(encoding="utf-8"))


class CitationTests(unittest.TestCase):
    def test_every_decision_mention_cites_an_id(self):
        docs = markdown_docs()
        self.assertIn("AGENTS.md", docs)
        self.assertIn("docs/SECURITY_MODEL.md", docs)
        missing = {doc: uncited((ROOT / doc).read_text(encoding="utf-8")) for doc in docs}
        self.assertEqual({}, {d: m for d, m in missing.items() if m},
                         "cite the decision's ID from docs/DECISIONS.md right after the "
                         "mention, or add the decision there first")

    def test_every_cited_id_is_in_the_log(self):
        known = set(log_rows())
        unknown = {}
        for doc in markdown_docs():
            ids = set(ID.findall((ROOT / doc).read_text(encoding="utf-8"))) - known
            if ids:
                unknown[doc] = sorted(ids)
        self.assertEqual({}, unknown)

    def test_the_check_finds_an_uncited_mention(self):
        # The check itself, on synthetic text: a mention without an ID is found,
        # whatever its form or line break; one with an ID, or a generic rule, is not.
        for text in ("an accepted risk (the owner's decision, 22 October 2026)",
                     "Always on (the owner's\ndecision).", "by owner decision on 21 Oct",
                     "(the user's approval, 25 October 2026)", "Owner decisions, 21 Oct:",
                     "the owner's decision DEC-12 of 1 Oct", "the owner's decision (DEC-001)"):
            with self.subTest(text=text):
                self.assertEqual(1, len(uncited(text)))
        for text in ("an accepted risk (the owner's decision DEC-031, 22 October 2026)",
                     "the owner's\ndecisions DEC-023 and DEC-033 of 21, 22 and 23 October",
                     "and no owner decision is open, run", "open owner decisions, and where",
                     "a decision that is the user's (under \"Stop and ask\")",
                     "release approval", "the owner-approved widening"):
            with self.subTest(text=text):
                self.assertEqual([], uncited(text))


if __name__ == "__main__":
    unittest.main()
