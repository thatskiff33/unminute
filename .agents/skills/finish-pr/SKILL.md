---
name: finish-pr
description: Take an Unminute pull request from finished code to merged, following the repo's rules. Covers the checks, the final diff, PR evidence, an independent review of the exact commit, ready and merge. Use when a change is complete, or when asked whether a PR can be marked ready or merged.
---

# Finish a pull request

The rules are in `AGENTS.md` ("Checks", "Delivery", "Authority") and `.github/pull_request_template.md`. This is the order to apply them in, not a second copy.

1. Run the checks that "Checks" names for what changed. For a Python change, run `python tools/run_tests.py --strict-skips` and `ruff check .`, plus the UI checks for a UI change. Wait for any heavy-run lock another session holds.
2. Read the final diff with `git diff origin/main...HEAD`. Look for unrelated edits, private material (P1) and invisible characters.
3. Push, then open or update the PR from the template. Under "Checks run", list only the checks you actually ran, and say plainly what was not verified (E1).
4. Get an independent review of the fixed head commit. The reviewer must not have written or directed the change, and must work in a separate checkout of that exact commit. Give the reviewer the code and the rules, not your conclusions. Ask for a security review when anything is under "Stop and ask".
   - In Claude Code, use an Agent with isolation "worktree", or another session. `/code-review` accepts a PR number or a commit range, and `/security-review` reviews the branch against origin's default branch.
   - In Codex, use `/review`.
5. Record the reviewer, the commit, the verdict and the findings on the PR. Fix or answer each finding. Any change after the review needs its own review. After a rebase, use `git range-diff` as "Authority" describes.
6. Run `python tools/dev_status.py --pr <n>`. It lists what still stops the PR: draft state, merge state, failing or missing required checks, and no comment naming the head commit. It cannot see open owner decisions or whether findings were answered, so check those yourself.
7. When nothing stops it, run `gh pr ready <n>`. Squash-merge only under "Authority". Never push a tag or publish a release.
8. After the merge, delete the branch and the worktree. Tick the tracking issue. Close the issue only when every acceptance criterion is met, and say which PRs did it.
