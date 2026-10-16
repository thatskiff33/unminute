## What and why

<!-- One or two sentences. Link the issue: "Closes #N" only when every acceptance criterion is met; otherwise "Part of #N". -->

## Definition of done

Tick each item, or replace it with "n/a" and the reason. See docs/RELEASE_STANDARD.md.

- [ ] Tests cover the changed behaviour, and pass on Linux and Windows CI with no new unexplained skips.
- [ ] A fragment in `changelog.d/` for anything a user or IT would notice (or: not release-worthy, because ...). See `changelog.d/README.md`.
- [ ] The README, user guide, docs and screenshots are updated in this PR. They link to the source of truth instead of restating counts or lists that change.
- [ ] Nothing in this PR contains meeting content, real names, real recordings, tokens or other private data: not in code, fixtures, logs, screenshots or this description.
- [ ] No budget went up in `tests/budgets.json` or `docs/perf-budgets.json` (or: raised because ...). These files arrive with #123 and #124.
- [ ] No new dependency, model or font (or: added, and its licence was checked: ...).
- [ ] Changes to the bridge, file paths, network, installer, workflows or stored data have had a security review (or: none touched).
- [ ] Independent review of the final commit: findings fixed or answered.

## Checks run

<!-- The commands and tools actually run, with results. Say plainly what was not verified (for example: real audio, a packaged build, a clean profile). -->
