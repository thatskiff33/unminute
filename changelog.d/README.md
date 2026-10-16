# Changelog fragments

Each pull request with a change a user or IT would notice adds one file here instead of editing `CHANGELOG.md`. Separate files never conflict, so open pull requests don't clash over the same lines.

## Writing a fragment

- **Name:** the issue or pull request number, then a few words: `117-skip-speakers.md`. Fragments are released in number order.
- **Content:** one or more bullets in the changelog's style, `- **What changed.** What it means for the user.`
- **Group (optional):** a first line ending in a colon, such as `Security hardening (#111, slice A5):`. Fragments with the same group line are released under one copy of it.
- **No `#` or `##` headings.** The version heading is added at release, and a `##` line would cut the release notes short.

`python tools/changelog.py --preview` shows the combined notes, and `--check` checks every fragment. The unit tests run the same check.

## At release

`python tools/changelog.py vX.Y.Z` (or `vX.Y.Z-preview.N`) writes a `## vX.Y.Z` section at the top of `CHANGELOG.md` and deletes the fragments. Add `--summary "One line"` for a line under the heading. Review the result and merge it before tagging, as `docs/RELEASING.md` describes. This README stays.
