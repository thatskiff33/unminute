# tests/test_commit_emails.py
# The Commit emails checks (tools/commit_emails.py, .github/workflows/email-guard.yml;
# the user's decision DEC-094): commit authors and committers use noreply addresses,
# Co-authored-by trailers carry noreply addresses, and no personal or work address is
# in a commit message, an added line, or a pull request's title or description.
# Findings name the domain only, never the address, because CI logs are public.
#
# Every address here is synthetic, and each personal or work one is split across two
# string literals, so this file never holds a whole one (the check scans every line a
# pull request adds).

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "commit_emails.py"
WORKFLOW = ROOT / ".github" / "workflows" / "email-guard.yml"
sys.path.insert(0, str(ROOT / "tools"))
import commit_emails  # noqa: E402

NOREPLY = "12345+someone@users.noreply.github.com"
GMAIL = "someone.else" + "@gmail.com"
WORK = "a.person" + "@unlimit.co.nz"
CLAUDE = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


def commit(**overrides):
    found = {"sha": "abcdef1234567890", "author": NOREPLY, "committer": "noreply@github.com",
             "message": "Fix a thing\n\n" + CLAUDE}
    found.update(overrides)
    return found


class Run:
    """Runs the checker as CI does, in a child process, and keeps its output."""

    def __init__(self, commits, diff=None, title="Fix a thing", body="", author="someone"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "commits.jsonl"
            path.write_text("".join(json.dumps(c) + "\n" for c in commits), encoding="utf-8")
            args = [str(path)]
            if diff is not None:
                (Path(tmp) / "added.diff").write_text(diff, encoding="utf-8")
                args.append(str(Path(tmp) / "added.diff"))
            env = {**os.environ, "PR_TITLE": title, "PR_BODY": body, "PR_AUTHOR": author}
            done = subprocess.run([sys.executable, "-I", str(SCRIPT), *args], env=env,
                                  capture_output=True, text=True, encoding="utf-8", check=False)
        self.code, self.out = done.returncode, done.stdout + done.stderr


class CommitTests(unittest.TestCase):
    def test_noreply_commits_pass(self):
        for one in (commit(),
                    commit(author="49699333+dependabot[bot]@users.noreply.github.com"),
                    commit(author="41898282+github-actions[bot]@users.noreply.github.com"),
                    commit(committer="NoReply@GitHub.com"),
                    commit(author=NOREPLY + " "),
                    commit(message="Bump x\n\nSigned-off-by: dependabot[bot] <support@github.com>"),
                    commit(message="x\n\nCo-authored-by: Someone <" + NOREPLY + ">"),
                    commit(message="x\n\nco-authored-by : A Tool <no-reply@example.org>")):
            with self.subTest(commit=one):
                run = Run([one])
                self.assertEqual(run.code, 0, run.out)
                self.assertIn("Commit emails OK", run.out)

    def test_findings_name_the_domain_only(self):
        cases = [
            (commit(author=GMAIL),
             "abcdef1: author email is not a GitHub noreply address (gmail.com)"),
            (commit(committer=WORK),
             "abcdef1: committer email is not a GitHub noreply address (unlimit.co.nz)"),
            (commit(author="dev" + "@example.com"),
             "author email is not a GitHub noreply address (another domain)"),
            (commit(author=""), "author email is not a GitHub noreply address (no domain)"),
            (commit(author=None), "author email is not a GitHub noreply address (no domain)"),
            # A noreply address with more after it is not one.
            (commit(author=f"x{NOREPLY}.evil.example"), "author email is not a GitHub noreply"),
            (commit(message=f"Fix\n\nCo-authored-by: Someone <{GMAIL}>"),
             "abcdef1: a Co-authored-by trailer is not a noreply address (gmail.com)"),
            (commit(message=f"Fix\n\n  Co-authored-by : A <{WORK.upper()}>"),
             "a Co-authored-by trailer is not a noreply address (unlimit.co.nz)"),
            # A trailer at any other domain names a person too.
            (commit(message="Fix\n\nCo-authored-by: B <b" + "@example.com>"),
             "a Co-authored-by trailer is not a noreply address (another domain)"),
            # Every address on a trailer, not only the first.
            (commit(message="Fix\n\nCo-authored-by: A <noreply@anthropic.com> <b"
                            + "@example.com>"),
             "a Co-authored-by trailer is not a noreply address (another domain)"),
            (commit(message=f"Thanks {GMAIL}"),
             "abcdef1: commit message contains a personal address (gmail.com)"),
            (commit(message="x\n\nReviewed-by: B <b.c" + "@googlemail.com>"),
             "commit message contains a personal address (googlemail.com)"),
            (commit(message="x\n\nReported-by: C <c" + "@mail.unlimit.co.nz>"),
             "commit message contains a personal address (unlimit.co.nz)"),
            # Full-width forms and invisible characters do not hide one.
            (commit(message="x ｓｏｍｅｏｎｅ" + "＠ｇｍａｉｌ．ｃｏｍ"),
             "commit message contains a personal address (gmail.com)"),
            (commit(message="x someone\u200b" + "@gmail.com"),
             "commit message contains a personal address (gmail.com)"),
        ]
        for one, expected in cases:
            with self.subTest(expected=expected):
                run = Run([one])
                self.assertEqual(run.code, 1, run.out)
                self.assertIn(expected, run.out)
                self.assertNotIn("someone.else@", run.out)
                self.assertNotIn("a.person@", run.out.lower())
                self.assertIn("To fix:", run.out)

    def test_no_address_reaches_the_log(self):
        # Every finding at once: the output holds no "@" except the fix's noreply form.
        run = Run([commit(author=GMAIL, committer=WORK,
                          message=f"{GMAIL}\n\nCo-authored-by: X <{WORK}>")],
                  diff=f"diff --git a/f b/f\n+++ b/f\n@@ -0,0 +1 @@\n+{GMAIL}\n",
                  title=f"From {WORK}", body=f"Ask {GMAIL}")
        self.assertEqual(run.code, 1)
        self.assertEqual(re.findall(r"\S+@\S+", run.out),
                         ["<id>+<login>@users.noreply.github.com`,"])

    def test_no_commits_or_too_many_fail(self):
        self.assertIn("no commits were read", Run([]).out)
        many = [commit(sha=f"{i:040x}") for i in range(commit_emails.MAX_COMMITS)]
        self.assertIn("split the pull request", Run(many).out)

    def test_bad_arguments(self):
        done = subprocess.run([sys.executable, "-I", str(SCRIPT)], capture_output=True,
                              text=True, check=False)
        self.assertEqual(done.returncode, 2)


class TextTests(unittest.TestCase):
    def test_title_and_description(self):
        run = Run([commit()], title=f"Ask {GMAIL}", body=f"Thanks\n\nCo-authored-by: <{WORK}>")
        self.assertEqual(run.code, 1)
        self.assertIn("pull request title contains a personal address (gmail.com)", run.out)
        self.assertIn("pull request description contains a personal address (unlimit.co.nz)",
                      run.out)

    def test_dependabot_descriptions_are_skipped(self):
        # They quote upstream release notes; its commits are still checked.
        run = Run([commit()], body=f"Thanks to {GMAIL}", author="dependabot[bot]")
        self.assertEqual(run.code, 0, run.out)
        run = Run([commit(author=GMAIL)], author="dependabot[bot]")
        self.assertEqual(run.code, 1)

    def test_not_an_address(self):
        for text in ("see https://gmail.com/help", "@gmail.com alone",
                     "x@gmail.community", "x@gmail.com.example.org", "x@notunlimit.co.nzz"):
            with self.subTest(text=text):
                self.assertEqual(commit_emails.find_personal(text), [])
        self.assertEqual(commit_emails.find_personal("Mail x" + "@gmail.com."), ["gmail.com"])


class DiffTests(unittest.TestCase):
    def test_added_lines_by_file_and_line(self):
        diff = ("diff --git a/docs/a.md b/docs/a.md\n--- a/docs/a.md\n+++ b/docs/a.md\n"
                "@@ -3,0 +4,2 @@\n+fine\n" f"+mail {GMAIL}\n"
                "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n"
                f"-old {WORK}\n" f"+++ {WORK}\n")
        found = commit_emails.check_diff(diff)
        self.assertEqual(found, ["docs/a.md:5: adds a personal address (gmail.com)",
                                 "b.py:1: adds a personal address (unlimit.co.nz)"])

    def test_file_names(self):
        # A name holding an address is a finding, and never quoted in one.
        name = "docs/zz" + "@gmail.com.txt"
        diff = (f"diff --git a/{name} b/{name}\nnew file mode 100644\n--- /dev/null\n"
                f"+++ b/{name}\n@@ -0,0 +1 @@\n+mail {GMAIL}\n")
        found = commit_emails.check_diff(diff)
        self.assertEqual(found, [
            "a file name holds a personal address (gmail.com)",
            "(a file with @ in its name):1: adds a personal address (gmail.com)"])
        # An empty new file has no +++ line: its header still names it.
        empty = f"diff --git a/{name} b/{name}\nnew file mode 100644\nindex 0000000..e69de29\n"
        self.assertEqual(commit_emails.check_diff(empty),
                         ["a file name holds a personal address (gmail.com)"])
        # Any other "@" in a name keeps it out of the finding too.
        other = "x/b" + "@example.com"
        diff = f"diff --git a/{other} b/{other}\n+++ b/{other}\n@@ -0,0 +1 @@\n+{GMAIL}\n"
        self.assertEqual(commit_emails.check_diff(diff),
                         ["(a file with @ in its name):1: adds a personal address (gmail.com)"])

    def test_only_newlines_split_lines(self):
        # Form feed, lone CR, NEL, U+2028 and the file separators do not end an added
        # line, so an address after one is still on it.
        for sep in (chr(0x0C), chr(0x0D), chr(0x85), chr(0x2028), chr(0x1C)):
            with self.subTest(sep=hex(ord(sep))):
                diff = f"diff --git a/f b/f\n+++ b/f\n@@ -0,0 +1 @@\n+text{sep}{GMAIL}\n"
                self.assertEqual(commit_emails.check_diff(diff),
                                 ["f:1: adds a personal address (gmail.com)"])
        # A CRLF diff still reads by line.
        crlf = f"diff --git a/f b/f\r\n+++ b/f\r\n@@ -0,0 +1 @@\r\n+{GMAIL}\r\n"
        self.assertEqual(commit_emails.check_diff(crlf),
                         ["f:1: adds a personal address (gmail.com)"])

    def test_licence_texts_lines_are_not_checked(self):
        # Upstream texts kept byte for byte (#176): their lines are not checked; their
        # names still are, and so is every other folder.
        lic = "packaging/licences/some-lib-1.0/LICENSE"
        diff = f"diff --git a/{lic} b/{lic}\n+++ b/{lic}\n@@ -0,0 +1 @@\n+(c) {GMAIL}\n"
        self.assertEqual(commit_emails.check_diff(diff), [])
        name = "packaging/licences/zz" + "@gmail.com.txt"
        diff = f"diff --git a/{name} b/{name}\n+++ b/{name}\n@@ -0,0 +1 @@\n+(c) {GMAIL}\n"
        self.assertEqual(commit_emails.check_diff(diff),
                         ["a file name holds a personal address (gmail.com)"])
        near = "packaging/licences.md"
        diff = f"diff --git a/{near} b/{near}\n+++ b/{near}\n@@ -0,0 +1 @@\n+{GMAIL}\n"
        self.assertEqual(commit_emails.check_diff(diff),
                         ["packaging/licences.md:1: adds a personal address (gmail.com)"])

    def test_removed_lines_are_not_findings(self):
        diff = f"diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +0,0 @@\n-{GMAIL}\n"
        self.assertEqual(commit_emails.check_diff(diff), [])

    def test_a_finding_is_one_workflow_command(self):
        # A file name in a finding cannot start a second workflow command.
        self.assertEqual(commit_emails.escape("a%b\r\n::stop-commands::x"),
                         "a%25b%0D%0A::stop-commands::x")


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.head, cls.jobs = cls.text.split("\njobs:", 1)

    def test_triggers(self):
        on = re.search(r"(?ms)^on:\n(.*?)^\S", self.head).group(1)
        self.assertEqual(re.findall(r"^  (\w+):", on, re.MULTILINE), ["pull_request", "push"])
        self.assertIn("types: [opened, synchronize, reopened, edited]", on)   # title, description
        self.assertEqual(on.count("branches: [main]"), 2)

    def test_read_only(self):
        self.assertIn("\npermissions: {}\n", self.head)
        self.assertNotRegex(self.text, r":\s*write\b")
        self.assertEqual(re.findall(r"^\s+([\w-]+): read\b", self.jobs, re.MULTILINE),
                         ["contents", "pull-requests", "contents"])
        self.assertNotIn("secrets.", self.text)
        self.assertEqual(self.text.count("persist-credentials: false"), 2)

    def test_pull_requests_run_the_bases_checker(self):
        self.assertIn('git show "$BASE_SHA:tools/commit_emails.py" > "$RUNNER_TEMP/check.py"',
                      self.jobs)
        # Only a base commit that is here and has no checker falls back to the PR's copy.
        self.assertIn('elif git cat-file -e "$BASE_SHA^{commit}"; then', self.jobs)
        self.assertIn("--text --unified=0", self.jobs)
        self.assertIn('python3 -I "$RUNNER_TEMP/check.py" "$RUNNER_TEMP/commits.jsonl" '
                      '"$RUNNER_TEMP/added.diff"', self.jobs)

    def test_untrusted_text_only_through_the_environment(self):
        # The title and description reach the script as variables, never in the script.
        for line in self.text.splitlines():
            if re.search(r"github\.event\.pull_request\.(title|body)\b", line):
                self.assertRegex(line, r"^\s+PR_(TITLE|BODY): \$\{\{")

    def test_main_checks_the_pushed_commits(self):
        main = self.jobs[self.jobs.index("\n  main:"):]
        self.assertIn("if: github.event_name == 'push'", main)
        self.assertIn('repos/$REPO/compare/$BEFORE...$AFTER', main)
        self.assertIn('python3 -I tools/commit_emails.py "$RUNNER_TEMP/commits.jsonl"', main)


if __name__ == "__main__":
    unittest.main()
