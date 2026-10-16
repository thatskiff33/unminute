# tools/changelog.py
# Release notes from changelog fragments. Each pull request adds its own file
# to changelog.d/ instead of editing CHANGELOG.md, so open pull requests never
# conflict over the same lines. When a release is prepared, this script moves
# the fragments into a "## vX.Y.Z" section of CHANGELOG.md (the section the
# Release workflow reads) and deletes them.
#
#   python tools/changelog.py --check                 check the fragments
#   python tools/changelog.py --preview               print the unreleased notes
#   python tools/changelog.py vX.Y.Z [--summary TEXT] write the section, delete the fragments
#
# A fragment is Markdown: one or more "- **Summary.** Detail." bullets, and
# optionally a first line naming a group, ending with a colon (for example
# "Security hardening (#111, slice A5):"). Fragments are combined in file-name
# order (a leading number sorts as a number); fragments with the same group
# line are combined under one copy of it. changelog.d/README.md explains the
# format and is not a fragment. Stdlib only.

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENTS = "changelog.d"
CHANGELOG = "CHANGELOG.md"
NOT_FRAGMENTS = {"readme.md"}
# The tags the Release workflow accepts (.github/workflows/release.yml).
VERSION = re.compile(r"v\d+\.\d+\.\d+(-preview\.\d+)?")
BULLET = re.compile(r"[-*] ")


class ChangelogError(Exception):
    """A fragment or the changelog cannot be used. The message says why."""


def _order(path):
    # "9-x.md" before "10-x.md"; names without a number after numbered ones.
    m = re.match(r"(\d+)", path.name)
    return (int(m.group(1)) if m else float("inf"), path.name.lower())


def fragment_files(root=ROOT):
    folder = Path(root) / FRAGMENTS
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir() if p.is_file() and p.name.lower() not in NOT_FRAGMENTS]
    return sorted(files, key=_order)


def read_fragment(path):
    """(group line or None, body lines) for one fragment, after checking it."""
    name = path.name
    if path.suffix.lower() != ".md":
        raise ChangelogError(f"{FRAGMENTS}/{name}: fragments are .md files")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ChangelogError(f"{FRAGMENTS}/{name}: not UTF-8 text") from None
    lines = [line.rstrip() for line in text.lstrip("﻿").splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        raise ChangelogError(f"{FRAGMENTS}/{name}: empty")
    for line in lines:
        # A "#" or "##" heading would end the Release workflow's section early.
        if re.match(r"#{1,2}(\s|$)", line):
            raise ChangelogError(f"{FRAGMENTS}/{name}: no '#' or '##' headings in a fragment "
                                 "(the version heading is added at release)")
    group = None
    if not BULLET.match(lines[0]) and lines[0].endswith(":"):
        group = lines.pop(0)
        while lines and not lines[0]:
            lines.pop(0)
        if not lines:
            raise ChangelogError(f"{FRAGMENTS}/{name}: a group line with nothing under it")
    if not BULLET.match(lines[0]):
        raise ChangelogError(f"{FRAGMENTS}/{name}: start with a '- ' bullet (after the "
                             "optional group line ending in ':')")
    return group, lines


def check(root=ROOT):
    """Read every fragment; raise ChangelogError for the first bad one."""
    return [read_fragment(p) for p in fragment_files(root)]


def notes(root=ROOT):
    """The combined Markdown body of every fragment, or "" if there are none."""
    blocks, groups = [], {}
    for group, lines in check(root):
        if group is None:
            blocks.append(list(lines))
        elif group in groups:
            groups[group].extend(lines)
        else:
            groups[group] = [group, ""] + list(lines)
            blocks.append(groups[group])
    return "\n\n".join("\n".join(b) for b in blocks)


def release(version, summary=None, root=ROOT):
    """Write "## <version>" with the fragments' notes into CHANGELOG.md, above
    the newest version, then delete the fragments. Returns the section."""
    if not VERSION.fullmatch(version):
        raise ChangelogError(f"'{version}' is not vX.Y.Z or vX.Y.Z-preview.N")
    path = Path(root) / CHANGELOG
    text = path.read_bytes().decode("utf-8")      # bytes: keep the file's own line endings
    lines = text.splitlines()
    heading = f"## {version}"
    if heading in lines:
        raise ChangelogError(f"{CHANGELOG} already has '{heading}'")
    if "## Unreleased" in lines:
        raise ChangelogError(f"{CHANGELOG} still has '## Unreleased': move its entries into "
                             f"{FRAGMENTS}/ first")
    body = notes(root)
    if not body:
        raise ChangelogError(f"No fragments in {FRAGMENTS}/: nothing to release")
    section = [heading, ""]
    if summary:
        section += [summary.strip(), ""]
    section += body.split("\n") + [""]
    at = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))
    if at == len(lines) and lines and lines[-1]:
        section.insert(0, "")
    out = lines[:at] + section + lines[at:]
    newline = "\r\n" if "\r\n" in text else "\n"
    path.write_text(newline.join(out) + newline, encoding="utf-8", newline="")
    for fragment in fragment_files(root):
        fragment.unlink()
    return "\n".join(section)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Release notes from changelog.d fragments.")
    parser.add_argument("version", nargs="?", help="vX.Y.Z or vX.Y.Z-preview.N to release")
    parser.add_argument("--summary", help="a one-line summary under the version heading")
    parser.add_argument("--preview", action="store_true", help="print the unreleased notes")
    parser.add_argument("--check", action="store_true", help="check the fragments only")
    args = parser.parse_args(argv)
    try:
        if args.check:
            print(f"{len(check())} fragment(s) OK")
        elif args.preview:
            print(notes() or f"(no fragments in {FRAGMENTS}/)")
        elif args.version:
            print(release(args.version, args.summary))
            print(f"\nWritten to {CHANGELOG}; the fragments were deleted. Review, commit both, "
                  "and merge before tagging.")
        else:
            parser.error("give a version, --preview or --check")
    except ChangelogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
