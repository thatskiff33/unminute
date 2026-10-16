# tools/changelog.py
# Release notes from changelog fragments. Each pull request adds its own file
# to changelog.d/ instead of editing CHANGELOG.md, so open pull requests never
# conflict over the same lines. When a release is prepared, this script moves
# the fragments into a dated "## vX.Y.Z (YYYY-MM-DD)" section of CHANGELOG.md
# (the section the Release workflow reads), sets VERSION to X.Y.Z (or
# X.Y.Z-preview.N), and deletes the fragments. A full release also brings in
# the notes of its own previews (## vX.Y.Z-preview.N), whose fragments were
# used up then, so its notes cover everything since the last full release
# (#147); the preview sections stay as they were. VERSION is the single
# source of the version: the build reads it, and the Release workflow refuses
# a tag that differs (#131).
#
#   python tools/changelog.py --check                 check the fragments
#   python tools/changelog.py --preview [vX.Y.Z]      print the unreleased notes (with a
#                                                     version: as that release would have them)
#   python tools/changelog.py vX.Y.Z [--summary TEXT] [--date YYYY-MM-DD]
#                                                     write the section and VERSION,
#                                                     delete the fragments
#
# A fragment is Markdown: one or more "- **Summary.** Detail." bullets, and
# optionally a first line naming a group, ending with a colon (for example
# "Security hardening (#111, slice A5):"). Fragments without a group come
# first, as one list; then each group, under one copy of its line. Both are in
# file-name order (a leading number sorts as a number). changelog.d/README.md
# explains the format and is not a fragment. Stdlib only.

import argparse
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENTS = "changelog.d"
CHANGELOG = "CHANGELOG.md"
VERSION_FILE = "VERSION"
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
NOT_FRAGMENTS = {"readme.md"}
# The tags the Release workflow accepts (.github/workflows/release.yml), ASCII
# digits only as there.
VERSION = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(-preview\.[0-9]+)?")
BULLET = re.compile(r"[-*] ")
# A "#" or "##" heading would end the Release workflow's section early (its awk
# stops at the next line starting "## ").
HEADING = re.compile(r"#{1,2}(\s|$)")


class ChangelogError(Exception):
    """A fragment or the changelog cannot be used. The message says why."""


def _group_at(lines, i):
    """Does lines[i] read as a group line, as notes() writes one: an
    unindented line, not a bullet, ending in ":", then a blank line, then a
    bullet?"""
    line = lines[i]
    return (bool(line) and not line[0].isspace() and not BULLET.match(line)
            and line.endswith(":") and i + 2 < len(lines) and not lines[i + 1]
            and bool(BULLET.match(lines[i + 2])))


def _lines(text):
    # Line breaks only: str.splitlines() would also split on form feeds and
    # Unicode separators, silently rewriting them.
    return text.replace("\r\n", "\n").split("\n")


def _order(path):
    # "9-x.md" before "10-x.md"; names without a number after numbered ones.
    m = re.match(r"([0-9]+)", path.name)
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
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise ChangelogError(f"{FRAGMENTS}/{name}: not UTF-8 text") from None
    lines = []
    for line in _lines(text.lstrip("\ufeff")):
        line = line.rstrip()
        if line or (lines and lines[-1]):         # runs of blank lines count as one,
            lines.append(line)                    # as when the notes are read back
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        raise ChangelogError(f"{FRAGMENTS}/{name}: empty")
    for line in lines:
        if HEADING.match(line):
            raise ChangelogError(f"{FRAGMENTS}/{name}: no '#' or '##' headings in a fragment "
                                 "(the version heading is added at release)")
    group = None
    if lines[0][:1].isspace():
        raise ChangelogError(f"{FRAGMENTS}/{name}: the first line cannot be indented")
    if not BULLET.match(lines[0]) and lines[0].endswith(":"):
        group = lines.pop(0)
        while lines and not lines[0]:
            lines.pop(0)
        if not lines:
            raise ChangelogError(f"{FRAGMENTS}/{name}: a group line with nothing under it")
    if not BULLET.match(lines[0]):
        raise ChangelogError(f"{FRAGMENTS}/{name}: start with a '- ' bullet (after the "
                             "optional group line ending in ':')")
    for i in range(1, len(lines)):
        if _group_at(lines, i):
            # It would be read as a group line when a full release brings in
            # a preview's notes (#147).
            raise ChangelogError(f"{FRAGMENTS}/{name}: '{lines[i]}' reads as a group line; "
                                 "indent it, or end it without ':'")
    return group, lines


def check(root=ROOT):
    """Read every fragment; raise ChangelogError for the first bad one."""
    return [read_fragment(p) for p in fragment_files(root)]


def notes(root=ROOT, released=()):
    """The combined Markdown body of every fragment, or "" if there are none:
    the ungrouped fragments as one list, then each group. released: bodies of
    sections already released (previews, oldest first), brought in ahead of
    the fragments, each group's notes with the same group. Each is (heading,
    body lines)."""
    loose, groups = [], {}
    parts = [_released_notes(body, heading) for heading, body in released]
    parts.append(([], {}))
    for group, lines in check(root):
        if group is None:
            parts[-1][0].extend(lines)
        else:
            parts[-1][1].setdefault(group, []).extend(lines)
    for part_loose, part_groups in parts:
        loose.extend(part_loose)
        for group, lines in part_groups.items():
            groups.setdefault(group, []).extend(lines)
    blocks = ([loose] if loose else []) + [[g, ""] + body for g, body in groups.items()]
    return "\n\n".join("\n".join(b) for b in blocks)


def _released_notes(body, heading="a preview"):
    """(ungrouped lines, {group line: lines}) from the body of a released
    section, the reverse of notes(). Its one-line summary, if any, is left
    out: it described that release. A section that is not as this script
    wrote it (more than one line before the notes, or a block that does not
    start with a bullet) is refused rather than guessed at."""
    lines = []
    for line in body:
        line = line.rstrip()                      # hand-edited trailing spaces
        if line or (lines and lines[-1]):         # runs of blank lines count as one
            lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    i = 0
    if lines and not BULLET.match(lines[0]) and not _group_at(lines, 0):
        if len(lines) > 1 and lines[1]:
            raise ChangelogError(
                f"{CHANGELOG}: the '{heading}' section has more than one line before its "
                "notes, so it is not as tools/changelog.py wrote it and its notes cannot be "
                "brought into the full release. Restore it, or write the full section by hand.")
        i = 2                                     # the summary, then its blank line
    loose, groups = [], {}
    current, started = loose, False
    while i < len(lines):
        if _group_at(lines, i):
            current, started = groups.setdefault(lines[i], []), False
            i += 2
            continue
        if not lines[i] and i + 1 < len(lines) and _group_at(lines, i + 1):
            i += 1                                # the blank line between blocks
            continue
        if not started and not BULLET.match(lines[i]):
            raise ChangelogError(
                f"{CHANGELOG}: the '{heading}' section is not as tools/changelog.py wrote it "
                f"('{lines[i]}' is not a bullet), so its notes cannot be brought into the full "
                "release. Restore it, or write the full section by hand.")
        started = True
        current.append(lines[i])
        i += 1
    return loose, groups


def _preview_bodies(lines, version):
    """(heading, body) for each of version's released previews, oldest first.
    A heading is "## vX.Y.Z-preview.N", alone or followed by " (": by prefix,
    as is_heading() and the Release workflow match. The body is the lines up
    to the next "## " line."""
    heading = re.compile(rf"## {re.escape(version)}-preview\.([0-9]+)(?:$| \()")
    found = []
    for i, line in enumerate(lines):
        m = heading.match(line)
        if m:
            end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("## ")),
                       len(lines))
            found.append((int(m.group(1)), line, lines[i + 1:end]))
    return [(h, body) for _, h, body in sorted(found, key=lambda t: t[0])]


def _check_summary(summary):
    text = summary.strip()
    if "\n" in text or "\r" in text:
        raise ChangelogError("The summary must be one line")
    if HEADING.match(text):
        raise ChangelogError("The summary cannot be a '#' or '##' heading (it would end the "
                             "release notes)")
    if text.endswith(":"):
        raise ChangelogError("The summary cannot end with ':' (it would read as a group line "
                             "when a full release brings in a preview's notes)")
    if BULLET.match(text):
        raise ChangelogError("The summary cannot start with '- ' or '* ' (it would read as a "
                             "note when a full release brings in a preview's notes)")
    return text


def is_heading(line, version):
    """Is this line the changelog heading for version? Headings are
    "## vX.Y.Z (YYYY-MM-DD)"; releases before #131 have no date."""
    return line == f"## {version}" or line.startswith(f"## {version} (")


def release(version, summary=None, root=ROOT, date=None):
    """Write "## <version> (<date>)" with the fragments' notes into CHANGELOG.md,
    above the newest version, set VERSION, then delete the fragments. A full
    release also brings in its previews' notes. date is YYYY-MM-DD, today by
    default. Returns the section."""
    if not VERSION.fullmatch(version):
        raise ChangelogError(f"'{version}' is not vX.Y.Z or vX.Y.Z-preview.N")
    if date is None:
        date = datetime.date.today().isoformat()
    try:
        if not DATE.fullmatch(date):
            raise ValueError(date)
        datetime.date.fromisoformat(date)
    except ValueError:
        raise ChangelogError(f"'{date}' is not a date (YYYY-MM-DD)") from None
    summary = _check_summary(summary) if summary else None
    path = Path(root) / CHANGELOG
    text = path.read_bytes().decode("utf-8")      # bytes: keep the file's own line endings
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = _lines(text)
    if lines and lines[-1] == "":
        lines.pop()                               # the final newline is added back below
    heading = f"## {version} ({date})"
    if any(is_heading(line, version) for line in lines):
        raise ChangelogError(f"{CHANGELOG} already has a '## {version}' section")
    if "## Unreleased" in lines:
        raise ChangelogError(f"{CHANGELOG} still has '## Unreleased': move its entries into "
                             f"{FRAGMENTS}/ first")
    previews = [] if "-" in version else _preview_bodies(lines, version)
    body = notes(root, previews)
    if not body:
        raise ChangelogError(f"No fragments in {FRAGMENTS}/" + (
            f", and the {version}-preview.N sections have no notes to bring in"
            if previews else "") + ": nothing to release")
    section = [heading, ""]
    if summary:
        section += [summary, ""]
    section += body.split("\n") + [""]
    at = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))
    if at == len(lines):                          # no version yet: the section ends the file
        section.pop()
        if lines and lines[-1]:
            section.insert(0, "")
    out = lines[:at] + section + lines[at:]
    path.write_bytes((newline.join(out) + newline).encode("utf-8"))
    try:
        (Path(root) / VERSION_FILE).write_bytes((version[1:] + "\n").encode("ascii"))
    except OSError as exc:
        raise ChangelogError(f"{CHANGELOG} has '{heading}', but {VERSION_FILE} could not be "
                             f"written ({exc}). Set it to {version[1:]} by hand, then delete "
                             f"the fragments in {FRAGMENTS}/.") from None
    left = []
    for fragment in fragment_files(root):
        try:
            fragment.unlink()
        except OSError:
            left.append(fragment.name)            # keep going: delete all that can be
    if left:
        raise ChangelogError(f"{CHANGELOG} has '{heading}' and {VERSION_FILE} is set, but "
                             "these fragments could not be "
                             f"deleted (open in another program?): {', '.join(left)}. Delete "
                             "them by hand before committing, or they are released again.")
    return "\n".join(section)


def planned(version=None, root=ROOT):
    """The notes a release of version would have (the fragments, plus its
    previews' notes for a full release), without writing anything."""
    if version and not VERSION.fullmatch(version):
        raise ChangelogError(f"'{version}' is not vX.Y.Z or vX.Y.Z-preview.N")
    if not version or "-" in version:
        return notes(root)
    text = (Path(root) / CHANGELOG).read_bytes().decode("utf-8")
    return notes(root, _preview_bodies(_lines(text), version))


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # notes have macrons; a pipe may be cp1252
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="Release notes from changelog.d fragments.")
    parser.add_argument("version", nargs="?", help="vX.Y.Z or vX.Y.Z-preview.N to release")
    parser.add_argument("--summary", help="a one-line summary under the version heading")
    parser.add_argument("--date", help="the release date, YYYY-MM-DD (default: today)")
    parser.add_argument("--preview", action="store_true", help="print the unreleased notes")
    parser.add_argument("--check", action="store_true", help="check the fragments only")
    args = parser.parse_args(argv)
    try:
        if args.check:
            print(f"{len(check())} fragment(s) OK")
        elif args.preview:
            print(planned(args.version) or f"(no fragments in {FRAGMENTS}/)")
        elif args.version:
            print(release(args.version, args.summary, date=args.date))
            print(f"\nWritten to {CHANGELOG} and {VERSION_FILE}; the fragments were deleted. "
                  "Review, commit them, and merge before tagging.")
        else:
            parser.error("give a version, --preview or --check")
    except ChangelogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
