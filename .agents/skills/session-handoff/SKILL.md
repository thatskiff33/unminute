---
name: session-handoff
description: Hand Unminute work over before stopping, when context runs long, or when another agent will continue. Posts one "Handoff" comment on the PR or issue so the next session resumes from GitHub, not from a transcript. Use before ending a session with work unfinished.
---

# Hand over work

GitHub is the record that lasts between sessions, and a transcript is not. Write the handoff where the start-issue skill will find it.

1. Run `python tools/dev_status.py --no-fetch`, and `git status` in your worktree. Push work in progress to the task's branch; a draft PR is fine. Leave nothing important only in an uncommitted or unpushed state.
2. Post one comment on the PR, or on the issue when there is no PR yet (`gh pr comment` or `gh issue comment`). Start it with the line `Handoff` and include:
   - the branch, the worktree folder and the head commit;
   - what is done and verified, with the checks actually run and their results;
   - what is not done or not verified;
   - the next one or two concrete steps;
   - open owner decisions, and where they were asked;
   - the files announced to other sessions, and any lock you hold or are waiting on.
3. Include no meeting content, names, tokens, or paths into the live workspace (P1, P3).
4. When a follow-up will not be done in this PR, raise it as a new linked issue ("Authority" in `AGENTS.md`), never only as a line in the handoff.
5. Times are New Zealand time.
