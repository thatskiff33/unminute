# Changelog fragments

Each pull request with a change a user or IT would notice adds one file here instead of editing `CHANGELOG.md`. Separate files never conflict, so open pull requests don't clash over the same lines.

## Writing a fragment

- **Name:** the issue or pull request number, then a few words: `117-skip-speakers.md`. Within the release notes, fragments are in number order.
- **Content:** one or more bullets in the changelog's style, `- **What changed.** What it means for the user.`
- **Group (optional):** a first line ending in a colon, such as `Security hardening (#111, slice A5):`. Fragments without a group come first, as one list; then each group, with its fragments under one copy of its line.
- **No `#` or `##` headings.** The version heading is added at release, and a `##` line would cut the release notes short.

`python tools/changelog.py --preview` shows the combined notes, and `--check` checks every fragment. The unit tests run the same check.

## At release

`python tools/changelog.py vX.Y.Z` (or `vX.Y.Z-preview.N`) writes a dated `## vX.Y.Z (YYYY-MM-DD)` section at the top of `CHANGELOG.md`, sets `VERSION`, and deletes the fragments. A full release also brings in the notes of its own `-preview.N` sections, so it covers everything since the last full release when the previews were numbered `vX.Y.Z-preview.N` (see `docs/RELEASING.md`). Add `--summary "One line"` for a line under the heading, or `--date` for a date other than today. Review the result and merge it before tagging, as `docs/RELEASING.md` describes. This README stays.
