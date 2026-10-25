---
name: start-issue
description: Start work on a GitHub issue or a slice of #111 or #121 in the Unminute repo. Checks live state, ownership and file overlap, makes a fresh worktree from origin/main, and plans tests before code. Use at the start of any issue, or when resuming one from a handoff.
---

# Start an issue

The rules are in `AGENTS.md` ("Start here", "Invariants", "Stop and ask", "Several agents or sessions"). This is the order to apply them in, not a second copy.

1. Run `python tools/dev_status.py`. It shows the worktrees, open PRs, failing checks and the files open PRs share, read live from git and GitHub. Trust it over an earlier session's summary.
2. Read the issue with every comment: `gh issue view <n> --comments`. For a slice of #111 or #121, read the tracking issue's "How to resume" first. If there is a comment headed "Handoff" (see the session-handoff skill), resume from the latest one.
3. Check that nobody owns the issue already: look for an open PR or a worktree for it in step 1's output. If one exists, message the session that owns it rather than start a second branch. In Claude Code, use ListAgents and SendMessage.
4. From the main checkout (not from inside `.claude/worktrees/`), make the worktree from a fresh base, named as the tracking issue says:
   `git worktree add -b <branch> ../unminute-<short> origin/main`
5. List the files you expect to change and run `python tools/dev_status.py --no-fetch --files <paths>`. Before you edit, announce those files to the sessions that own the overlapping PRs.
6. Read only what "Where the rules live" in `AGENTS.md` names for those files.
7. If the change is under "Stop and ask", agree the approach and plan the security review the PR template asks for before writing code. Ask the same way for any decision that belongs to the owner. In Claude Code, use AskUserQuestion, with plain-English pros and cons for each option.
8. Plan the tests first. A regression test must fail without its fix, and you must show that it does (E1).
