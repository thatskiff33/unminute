"""Fail when a personal or work email address would reach main (the Commit emails checks).

The repository is public, and so is every commit on main: its author and committer
addresses and its message. This keeps addresses off main from now on (the user's
decision DEC-094: main's history is not rewritten). It checks:

- every commit's author and committer address: a GitHub noreply address
  (``<id>+<login>@users.noreply.github.com``) or ``noreply@github.com``, the committer
  GitHub writes for squash and web commits;
- every ``Co-authored-by:`` trailer: a noreply address (GitHub's, or any whose local
  part is ``noreply`` or ``no-reply``, such as an AI tool's attribution line);
- commit messages, and the lines and file names a pull request adds: no address at a
  personal or work domain (``PERSONAL_DOMAIN``), except the lines of the third-party
  licence texts (``UPSTREAM_TEXTS``); a finding never quotes a file name that holds "@";
- the pull request's title and description: the same.

A pull request runs it on its commits (``.github/workflows/email-guard.yml``), and
again whenever its title or description is edited. A push to main runs it on the
commits pushed, because GitHub writes a squash commit at merge time with the merging
account's commit address and a message made from the pull request, which no pull
request check saw. Anything already pushed to a public branch is public;
the account's "Keep my email addresses private" setting is what keeps an address out.

CI logs are public, so a finding names only the commit, the field and the domain
(``gmail.com``, ``unlimit.co.nz``, or "another domain"), never the address.

Usage: commit_emails.py COMMITS.jsonl [ADDED.diff]
COMMITS.jsonl has one {"sha", "author", "committer", "message"} object per line. The
pull request's title, description and author's login come from PR_TITLE, PR_BODY and
PR_AUTHOR. Exits 1 on any finding, 2 on bad arguments.

Adapted from thatskiff33/nz-openbank-mcp's scripts/check_commit_emails.py (its PR #68).
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from pathlib import Path

# -- what counts as private and as personal ------------------------------------------

# GitHub's per-user noreply form (bots such as dependabot[bot] use it too).
NOREPLY = re.compile(r"[^@\s]+@users\.noreply\.github\.com", re.IGNORECASE)
# The committer GitHub records for squash, merge and web commits.
ALLOWED = {"noreply@github.com"}
# A trailer's address may also be a service's no-reply address, such as an AI
# tool's attribution line; it names no person.
SERVICE_NOREPLY = re.compile(r"no-?reply@[a-z0-9.-]+\.[a-z]{2,}", re.IGNORECASE)
# Any local part (stopping at spaces, brackets, quotes and punctuation that cannot
# end one), at a personal or work domain or a subdomain of one. Group 1 is the base
# domain; a subdomain is matched but never reported, since its label could be anything.
PERSONAL_DOMAIN = r"(?:[a-z0-9-]+\.)*(gmail\.com|googlemail\.com|unlimit\.co\.nz)"
PERSONAL = re.compile(
    # Not followed by more of a domain name; a sentence-ending full stop is fine.
    rf"[^\s<>@\"'`(),;:\[\]]+@{PERSONAL_DOMAIN}(?![\w-]|\.[\w-])",
    re.IGNORECASE,
)
# The same in a file name, where an extension may follow the domain.
PERSONAL_NAME = re.compile(rf"[^\s<>@\"'`(),;:\[\]/]+@{PERSONAL_DOMAIN}(?![\w-])", re.IGNORECASE)
# A Co-authored-by trailer line (any case, spaces allowed before the colon).
TRAILER_LINE = re.compile(r"^\s*co-authored-by\s*:(.*)$", re.IGNORECASE | re.MULTILINE)
# Third-party licence texts, kept byte for byte as published (the build checks each
# against its SHA-256 in packaging/licences/manifest.json, #176): they hold upstream
# authors' public addresses and cannot be edited, so their lines are not checked.
# Their file names still are, and so is the manifest, which this repository writes
# (tools/collect_licences.py).
UPSTREAM_TEXTS = ("packaging/licences/",)
OWN_FILES = {"packaging/licences/manifest.json"}
# The only bot whose descriptions are skipped: they quote upstream release notes.
BODY_EXEMPT_AUTHORS = {"dependabot[bot]"}
# The pull request commits endpoint lists at most 250 commits.
MAX_COMMITS = 250
FIX = (
    "To fix: run `git config user.email <id>+<login>@users.noreply.github.com`, then "
    "rewrite the branch's commits, for example `git rebase --exec 'git commit --amend "
    "--no-edit --reset-author' origin/main`, and force-push. Take personal addresses out "
    "of commit messages and trailers, changed files and the pull request's title and "
    "description. On main: the account that merged turns on \"Keep my email addresses "
    "private\" (docs/DEVELOPER_NOTES.md, \"Commit email addresses\")."
)


def normalise(text: str) -> str:
    """Fold full-width forms (NFKC) and drop invisible format characters (Cf)."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def find_personal(text: str) -> list[str]:
    """The base domains of the personal or work addresses in text."""
    return [match[1] for match in PERSONAL.finditer(normalise(text))]


def find_personal_name(text: str) -> list[str]:
    """As find_personal, for file names: an extension may follow the address
    (``someone@gmail.com.txt``), so a dot after the domain does not end the match."""
    return [match[1].lower() for match in PERSONAL_NAME.finditer(normalise(text))]


def domain(address: str) -> str:
    """Name a personal or work domain; any other could identify a machine or a person."""
    if "@" not in address:
        return "no domain"
    found = re.fullmatch(PERSONAL_DOMAIN, address.rsplit("@", 1)[-1].strip(), re.IGNORECASE)
    return found[1].lower() if found else "another domain"


def is_noreply(address: str) -> bool:
    address = normalise(address).strip()
    return address.lower() in ALLOWED or NOREPLY.fullmatch(address) is not None


def trailer_problems(sha: str, message: str) -> list[str]:
    """Every address on a Co-authored-by trailer must be a noreply address."""
    problems = []
    for line in TRAILER_LINE.findall(normalise(message)):
        addresses = re.findall(r"<([^<>]*)>", line) or [line]
        for address in (a.strip() for a in addresses):
            if not (is_noreply(address) or SERVICE_NOREPLY.fullmatch(address)):
                problems.append(f"{sha}: a Co-authored-by trailer is not a noreply address "
                                f"({domain(address)})")
    return problems


# -- the checks ---------------------------------------------------------------------

def check_commits(lines: list[str]) -> list[str]:
    problems: list[str] = []
    commits = [json.loads(line) for line in lines if line.strip()]
    if not commits:
        problems.append("no commits were read")
    if len(commits) >= MAX_COMMITS:
        problems.append(f"{len(commits)} commits: the API lists at most {MAX_COMMITS}; "
                        "split the pull request")
    for commit in commits:
        sha = str(commit.get("sha", ""))[:7]
        for role in ("author", "committer"):
            address = str(commit.get(role) or "")
            if not is_noreply(address):
                problems.append(f"{sha}: {role} email is not a GitHub noreply address "
                                f"({domain(address)})")
        message = str(commit.get("message") or "")
        problems.extend(trailer_problems(sha, message))
        problems.extend(f"{sha}: commit message contains a personal address ({found})"
                        for found in find_personal(message))
    return problems


def check_diff(diff: str) -> list[str]:
    """Personal addresses on the lines a diff adds, by file and new line number, and in
    the names of the files it touches. A finding never quotes a file name holding "@"."""
    problems: list[str] = []
    path, line_no, upstream = "?", 0, False
    # File headers (---, +++) come only before a file's first hunk; inside a hunk an
    # added line "++ x" also starts with "+++ ".
    in_header = True
    # Split on newlines only: str.splitlines() also breaks at form feeds, lone CRs,
    # U+2028 and others, and the piece after one would pass as an unchanged line.
    for line in diff.split("\n"):
        line = line.removesuffix("\r")
        if line.startswith("diff --git "):
            in_header, path, upstream = True, "?", False
            # The header names the file even when nothing else does (an empty new file,
            # a rename, a mode change).
            problems.extend(f"a file name holds a personal address ({found})"
                            for found in sorted(set(find_personal_name(line))))
        elif in_header and line.startswith("+++ "):
            path = line[4:].strip('"').removeprefix("b/")
            upstream = path.startswith(UPSTREAM_TEXTS) and path not in OWN_FILES
            if "@" in path:
                path = "(a file with @ in its name)"
        elif line.startswith("@@"):
            in_header = False
            hunk = re.search(r"\+(\d+)", line)
            line_no = int(hunk[1]) if hunk else 0
        elif in_header or line.startswith(("-", "\\")):
            continue        # header lines, removed lines, "\ No newline at end of file"
        elif line.startswith("+"):
            if not upstream:
                problems.extend(f"{path}:{line_no}: adds a personal address ({found})"
                                for found in find_personal(line))
            line_no += 1
        else:
            line_no += 1
    return problems


def escape(text: str) -> str:
    """A finding as one workflow-command line: a file name it quotes cannot start another."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def check_text(label: str, text: str) -> list[str]:
    return [f"pull request {label} contains a personal address ({found})"
            for found in find_personal(text)]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (1, 2):
        print("usage: commit_emails.py COMMITS.jsonl [ADDED.diff]", file=sys.stderr)
        return 2
    problems = check_commits(Path(args[0]).read_text(encoding="utf-8").splitlines())
    if len(args) > 1:
        problems += check_diff(Path(args[1]).read_text(encoding="utf-8", errors="replace"))
    if os.environ.get("PR_AUTHOR", "") not in BODY_EXEMPT_AUTHORS:
        problems += check_text("title", os.environ.get("PR_TITLE", ""))
        problems += check_text("description", os.environ.get("PR_BODY", ""))
    for problem in problems:
        print(f"::error::{escape(problem)}")
    if problems:
        print(FIX)
        return 1
    print("Commit emails OK: noreply addresses only, and no personal or work address")
    return 0


if __name__ == "__main__":
    sys.exit(main())
