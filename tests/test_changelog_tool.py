# Tests for changelog fragments (tools/changelog.py): the fragments in the
# repository are valid, and a release writes the section the Release workflow
# reads. Stdlib only.
#   python -m unittest discover -s tests -v

import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import changelog  # noqa: E402
from changelog import ChangelogError  # noqa: E402

INTRO = "# Changelog\n\nRelease notes for Unminute. Unreleased changes are in changelog.d/.\n\n"
HEAD = INTRO + "## v0.4.3\n\n- **Older.** Kept.\n"


def keystore(root, complete):
    """A scratch workspace/keystore.py with PHASE_C_COMPLETE as given (#111):
    tools/changelog.py refuses a release while it is False."""
    (root / "workspace").mkdir(exist_ok=True)
    (root / "workspace" / "keystore.py").write_text(
        f"PHASE_C_COMPLETE = {complete}\nPHASE_C_FROZEN_VERSION = \"0.4.3\"\n",
        encoding="utf-8")


def workflow_notes(text, version):
    """The Release workflow's awk, line for line: lines after the dated heading
    '## <tag> (' up to the next line starting '## '."""
    out, on = [], False
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith(f"## {version} ("):
            on = True
            continue
        if on and line.startswith("## "):
            break
        if on:
            out.append(line)
    return "\n".join(out)


class RepositoryFragmentsTests(unittest.TestCase):
    def test_fragments_in_the_repository_are_valid(self):
        changelog.check()                     # raises with the file name if one is not

    def test_changelog_keeps_no_unreleased_section(self):
        lines = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
        self.assertNotIn("## Unreleased", lines)

    def test_workflow_reads_the_dated_version_heading(self):
        # tools/changelog.py writes "## vX.Y.Z (YYYY-MM-DD)"; the workflow's awk
        # (mirrored by workflow_notes above) takes only that dated heading.
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn('awk -v h="## $TAG"', workflow)
        self.assertIn('index($0, h " (") == 1 {on=1; next}', workflow)
        self.assertNotIn("$0 == h", workflow)
        self.assertIn("CHANGELOG.md", workflow)


class FragmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "changelog.d").mkdir()
        (self.root / "CHANGELOG.md").write_bytes(HEAD.encode())
        keystore(self.root, True)          # the release tests below: Phase C complete

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, name, text):
        (self.root / "changelog.d" / name).write_bytes(text.encode("utf-8"))

    def changelog(self):
        return (self.root / "CHANGELOG.md").read_bytes().decode("utf-8")

    def test_loose_fragments_form_one_list_then_each_group(self):
        self.add("README.md", "# How to write a fragment\n")          # not a fragment
        self.add("118-lightning.md", "- **Security update.** pytorch-lightning 2.6.6.\n")
        self.add("111-a5-policy.md", "Security hardening (#111):\n\n- **Policy.** IT settings.\n")
        self.add("111-a2-bridge.md", "Security hardening (#111):\n- **Bridge.** Tighter.\n")
        self.add("9-early.md", "- **Early.** First by number.\n")
        self.add("wording.md", "- **Wording.** No number, so last.\n")
        self.assertEqual(changelog.notes(self.root),
                         "- **Early.** First by number.\n"
                         "- **Security update.** pytorch-lightning 2.6.6.\n"
                         "- **Wording.** No number, so last.\n\n"
                         "Security hardening (#111):\n\n- **Bridge.** Tighter.\n"
                         "- **Policy.** IT settings.")

    def test_release_writes_the_section_the_workflow_reads(self):
        self.add("117-skip.md", "- **Skip speaker identification.** For one or two people.\n")
        self.add("111-a5.md", "Security hardening (#111, slice A5):\n\n- **Policy.** Managed.\n")
        changelog.release("v0.5.0", "Managed settings for IT.", root=self.root,
                          date="2026-10-20")
        self.assertEqual(self.changelog(), INTRO + (
            "## v0.5.0 (2026-10-20)\n\nManaged settings for IT.\n\n"
            "- **Skip speaker identification.** For one or two people.\n\n"
            "Security hardening (#111, slice A5):\n\n- **Policy.** Managed.\n\n"
            "## v0.4.3\n\n- **Older.** Kept.\n"))
        self.assertIn("- **Policy.** Managed.", workflow_notes(self.changelog(), "v0.5.0"))
        # v0.4.3 has an undated heading from before #131: it was released already,
        # and the workflow reads only dated headings.
        self.assertEqual(workflow_notes(self.changelog(), "v0.4.3"), "")
        self.assertEqual([p.name for p in (self.root / "changelog.d").iterdir()], [])
        self.assertEqual((self.root / "VERSION").read_bytes(), b"0.5.0\n")

    def test_release_keeps_crlf_endings_and_the_readme(self):
        (self.root / "CHANGELOG.md").write_bytes(HEAD.replace("\n", "\r\n").encode())
        self.add("README.md", "Format notes.\n")
        self.add("1-x.md", "- **X.** Y.\n")
        changelog.release("v0.5.0-preview.1", root=self.root, date="2026-10-20")
        self.assertEqual(self.changelog(), (INTRO + "## v0.5.0-preview.1 (2026-10-20)\n\n"
                                            "- **X.** Y.\n\n"
                                            "## v0.4.3\n\n- **Older.** Kept.\n")
                         .replace("\n", "\r\n"))
        self.assertTrue((self.root / "changelog.d" / "README.md").exists())
        self.assertEqual((self.root / "VERSION").read_bytes(), b"0.5.0-preview.1\n")

    def test_a_changelog_without_versions_gets_the_section_at_the_end(self):
        (self.root / "CHANGELOG.md").write_bytes(b"# Changelog\n\nIntro.\n")
        self.add("1-x.md", "- **X.** Y.\n")
        changelog.release("v0.1.0", root=self.root, date="2026-10-20")
        self.assertEqual(self.changelog(),
                         "# Changelog\n\nIntro.\n\n## v0.1.0 (2026-10-20)\n\n- **X.** Y.\n")

    def test_other_line_separators_are_left_alone(self):
        (self.root / "CHANGELOG.md").write_bytes((HEAD + "Page\x0cbreak and \u2028 kept.\n")
                                                 .encode())
        self.add("1-x.md", "- **X.** a\u2028b\n")
        changelog.release("v0.5.0", root=self.root)
        self.assertIn("Page\x0cbreak and \u2028 kept.", self.changelog())
        self.assertIn("- **X.** a\u2028b", self.changelog())

    def test_release_refusals(self):
        with self.assertRaisesRegex(ChangelogError, "nothing to release"):
            changelog.release("v0.5.0", root=self.root)
        self.add("1-x.md", "- **X.** Y.\n")
        for bad in ("0.5.0", "v0.5", "v0.5.0-rc1", "v0.5.0\n", "v١.0.0"):
            with self.assertRaisesRegex(ChangelogError, "not vX.Y.Z"):
                changelog.release(bad, root=self.root)
        for summary, reason in (("## Big release", "heading"), ("# Title", "heading"),
                                ("One\n## v9.9.9", "one line")):
            with self.assertRaisesRegex(ChangelogError, reason):
                changelog.release("v0.5.0", summary, root=self.root)
        with self.assertRaisesRegex(ChangelogError, "already has"):
            changelog.release("v0.4.3", root=self.root)          # undated, from before #131
        for bad in ("2026-13-01", "20-10-2026", "2026-10-20 ", "today", ""):
            with self.assertRaisesRegex(ChangelogError, "not a date"):
                changelog.release("v0.5.0", root=self.root, date=bad)
        (self.root / "CHANGELOG.md").write_text(
            HEAD.replace("## v0.4.3", "## v0.4.4 (2026-10-01)\n\n- **N.** M.\n\n## v0.4.3"),
            encoding="utf-8")
        with self.assertRaisesRegex(ChangelogError, "already has"):
            changelog.release("v0.4.4", root=self.root)          # dated
        self.assertFalse((self.root / "VERSION").exists())       # nothing written on refusal
        (self.root / "CHANGELOG.md").write_text(HEAD.replace("## v0.4.3", "## Unreleased\n\n"
                                                             "- old\n\n## v0.4.3"),
                                                encoding="utf-8")
        with self.assertRaisesRegex(ChangelogError, "Unreleased"):
            changelog.release("v0.5.0", root=self.root)
        self.assertTrue((self.root / "changelog.d" / "1-x.md").exists())   # nothing removed

    # -- a full release brings in its previews' notes (#147) ----------------------

    def test_a_full_release_brings_in_its_preview(self):
        self.add("1-a.md", "- **A.** In the preview.\n")
        self.add("2-g.md", "Security hardening:\n\n- **G1.** In the preview.\n")
        changelog.release("v0.5.0-preview.1", "Try it first.", root=self.root,
                          date="2026-10-20")
        preview = workflow_notes(self.changelog(), "v0.5.0-preview.1")
        self.add("3-b.md", "- **B.** Since the preview.\n")
        changelog.release("v0.5.0", "Everything since v0.4.3.", root=self.root,
                          date="2026-10-27")
        self.assertEqual(workflow_notes(self.changelog(), "v0.5.0"),
                         "\nEverything since v0.4.3.\n\n"
                         "- **A.** In the preview.\n- **B.** Since the preview.\n\n"
                         "Security hardening:\n\n- **G1.** In the preview.\n")
        self.assertNotIn("Try it first.", workflow_notes(self.changelog(), "v0.5.0"))
        self.assertEqual(workflow_notes(self.changelog(), "v0.5.0-preview.1"), preview)
        self.assertEqual((self.root / "VERSION").read_bytes(), b"0.5.0\n")

    def test_two_previews_and_no_new_fragments(self):
        self.add("1-a.md", "- **A.** First preview.\n")
        self.add("2-g.md", "Security hardening:\n\n- **G1.** First preview.\n")
        changelog.release("v0.5.0-preview.1", root=self.root, date="2026-10-20")
        self.add("3-b.md", "- **B.** Second preview.\n")
        self.add("4-g.md", "Security hardening:\n\n- **G2.** Second preview.\n")
        changelog.release("v0.5.0-preview.2", root=self.root, date="2026-10-22")
        changelog.release("v0.5.0", root=self.root, date="2026-10-27")   # nothing new
        self.assertEqual(workflow_notes(self.changelog(), "v0.5.0"),
                         "\n- **A.** First preview.\n- **B.** Second preview.\n\n"
                         "Security hardening:\n\n- **G1.** First preview.\n"
                         "- **G2.** Second preview.\n")

    def test_only_this_versions_previews_and_a_preview_gets_only_its_fragments(self):
        (self.root / "CHANGELOG.md").write_text(HEAD.replace(
            "## v0.4.3", "## v0.4.3-preview.1 (2026-10-01)\n\n- **Old.** Earlier version.\n\n"
                         "## v0.4.3"), encoding="utf-8")
        self.add("1-a.md", "- **A.** First.\n")
        changelog.release("v0.5.0-preview.1", root=self.root, date="2026-10-20")
        self.add("2-b.md", "- **B.** Second.\n")
        changelog.release("v0.5.0-preview.2", root=self.root, date="2026-10-22")
        self.assertEqual(workflow_notes(self.changelog(), "v0.5.0-preview.2"),
                         "\n- **B.** Second.\n")                 # previews: the changes since
        changelog.release("v0.5.0", root=self.root, date="2026-10-27")
        notes = workflow_notes(self.changelog(), "v0.5.0")
        self.assertIn("- **A.** First.\n- **B.** Second.", notes)
        self.assertNotIn("Earlier version", notes)

    def test_a_preview_with_multi_line_notes_is_brought_in_whole(self):
        self.add("1-a.md", "- **A.** Detail:\n\n  more about A.\n- **A2.** Next.\n")
        self.add("2-g.md", "Group one:\n\n- **G.** Indented\n  continuation ending:\n\n"
                           "- **G2.** After a blank line.\n")
        changelog.release("v0.5.0-preview.1", root=self.root, date="2026-10-20")
        changelog.release("v0.5.0", root=self.root, date="2026-10-27")
        self.assertEqual(workflow_notes(self.changelog(), "v0.5.0"),
                         workflow_notes(self.changelog(), "v0.5.0-preview.1"))

    def test_a_full_release_with_no_preview_and_no_fragments_is_refused(self):
        (self.root / "CHANGELOG.md").write_text(HEAD.replace(
            "## v0.4.3", "## v0.4.4-preview.1 (2026-10-01)\n\n- **P.** Other.\n\n## v0.4.3"),
            encoding="utf-8")
        with self.assertRaisesRegex(ChangelogError, "nothing to release"):
            changelog.release("v0.5.0", root=self.root)
        with self.assertRaisesRegex(ChangelogError, "nothing to release"):
            changelog.release("v0.5.0-preview.2", root=self.root)   # previews: fragments only
        self.add("1-x.md", "- **X.** Y.\n")
        with self.assertRaisesRegex(ChangelogError, "end with ':'"):
            changelog.release("v0.5.0", "Changes:", root=self.root)

    def test_the_repository_fragments_reach_the_full_release(self):
        # #147 done when: a full release after its preview covers every change
        # since the last full release. Run on a copy of this repository's
        # changelog and fragments, at a version it can never have (the test
        # must pass on release commits too). A release commit has no
        # fragments, so one synthetic fragment is added (never a skip: the
        # Windows job fails on unexpected skips).
        text = (ROOT / "CHANGELOG.md").read_bytes()
        lines = text.decode("utf-8").replace("\r\n", "\n").split("\n")
        self.assertFalse(any(line.startswith("## v999.0.0") for line in lines))
        (self.root / "CHANGELOG.md").write_bytes(text)
        for f in changelog.fragment_files(ROOT):
            (self.root / "changelog.d" / f.name).write_bytes(f.read_bytes())
        self.add("0-synthetic.md", "Synthetic group:\n\n- **Synthetic.** Always present.\n")
        everything = changelog.notes(self.root)
        changelog.release("v999.0.0-preview.1", root=self.root, date="2026-10-20")
        self.add("999-late.md", "- **Late.** After the preview.\n")
        changelog.release("v999.0.0", root=self.root, date="2026-10-27")
        full = workflow_notes(self.changelog(), "v999.0.0")
        for line in everything.split("\n"):
            self.assertIn(line, full)
        self.assertIn("- **Late.** After the preview.", full)

    def test_a_preview_heading_is_found_as_the_workflow_finds_it(self):
        # The workflow matches "## <tag> (" by prefix, so a trailing space
        # still publishes; it must still be brought in, not silently lost.
        self.add("1-a.md", "- **A.** First.\n")
        changelog.release("v0.5.0-preview.1", root=self.root, date="2026-10-20")
        (self.root / "CHANGELOG.md").write_text(self.changelog().replace(
            "## v0.5.0-preview.1 (2026-10-20)", "## v0.5.0-preview.1 (2026-10-20) "),
            encoding="utf-8")
        self.assertIn("- **A.** First.", changelog.planned("v0.5.0", root=self.root))

    def test_hand_edited_preview_sections_are_refused_not_guessed(self):
        self.add("1-a.md", "- **A.** First.\n")
        changelog.release("v0.5.0-preview.1", "Try it.", root=self.root, date="2026-10-20")
        good = self.changelog()
        for edit, why in (("Try it.\n\nInstall: download the zip.\n", "is not a bullet"),
                          ("Try it.\nInstall: download the zip.\n", "more than one line")):
            (self.root / "CHANGELOG.md").write_text(good.replace("Try it.\n", edit),
                                                    encoding="utf-8")
            with self.assertRaisesRegex(ChangelogError, why):
                changelog.release("v0.5.0", root=self.root)
        self.assertFalse((self.root / "VERSION").read_bytes() == b"0.5.0\n")
        # Trailing spaces and doubled blank lines from an editor are fine.
        (self.root / "CHANGELOG.md").write_text(
            good.replace("- **A.** First.\n", "- **A.** First.   \n\n\n"), encoding="utf-8")
        self.assertEqual(changelog.planned("v0.5.0", root=self.root), "- **A.** First.")

    def test_summaries_and_fragments_must_read_back_as_written(self):
        self.add("1-x.md", "- **X.** Y.\n")
        with self.assertRaisesRegex(ChangelogError, "start with '- '"):
            changelog.release("v0.5.0", "- Try this preview", root=self.root)
        for name, text, reason in (
                ("2-para.md", "- **A.** Detail.\n\nThe following changed:\n\n- **A2.** B.\n",
                 "reads as a group line"),
                ("3-indented.md", "  Group:\n\n- **G.** H.\n", "cannot be indented")):
            self.add(name, text)
            with self.assertRaisesRegex(ChangelogError, re.escape(name) + ".*" + reason):
                changelog.check(self.root)
            (self.root / "changelog.d" / name).unlink()
        self.add("5-blanks.md", "- **A.** Detail.\n\nNote:\n\n\n- **A2.** B.\n")   # two blanks
        with self.assertRaisesRegex(ChangelogError, "5-blanks.md.*reads as a group line"):
            changelog.check(self.root)
        (self.root / "changelog.d" / "5-blanks.md").unlink()
        self.add("4-ok.md", "- **A.** Detail:\n\n  - indented sub-point\n- **A2.** B.\n")
        changelog.check(self.root)

    def test_a_preview_with_only_a_summary_says_so(self):
        (self.root / "CHANGELOG.md").write_text(HEAD.replace(
            "## v0.4.3", "## v0.5.0-preview.1 (2026-10-20)\n\nNothing yet.\n\n## v0.4.3"),
            encoding="utf-8")
        with self.assertRaisesRegex(ChangelogError, "have no notes to bring in"):
            changelog.release("v0.5.0", root=self.root)

    def test_preview_of_a_full_release_shows_its_folded_notes(self):
        self.add("1-a.md", "- **A.** In the preview.\n")
        changelog.release("v0.5.0-preview.1", root=self.root, date="2026-10-20")
        self.add("2-b.md", "- **B.** Since.\n")
        self.assertEqual(changelog.planned(root=self.root), "- **B.** Since.")
        self.assertEqual(changelog.planned("v0.5.0", root=self.root),
                         "- **A.** In the preview.\n- **B.** Since.")
        self.assertEqual(changelog.planned("v0.5.0-preview.2", root=self.root),
                         "- **B.** Since.")
        with self.assertRaisesRegex(ChangelogError, "not vX.Y.Z"):
            changelog.planned("v0.5.0-rc.1", root=self.root)
        self.assertTrue((self.root / "changelog.d" / "2-b.md").exists())     # nothing written

    def test_a_fragment_that_cannot_be_deleted_is_named_and_the_rest_go(self):
        for n in (1, 2, 3):
            self.add(f"{n}-x.md", f"- **X{n}.** Y.\n")
        real_unlink = Path.unlink

        def unlink(path, *a, **kw):
            if path.name == "2-x.md":
                raise PermissionError("in use")
            return real_unlink(path, *a, **kw)
        with mock.patch.object(Path, "unlink", unlink):
            with self.assertRaisesRegex(ChangelogError, "2-x.md"):
                changelog.release("v0.5.0", root=self.root)
        self.assertIn("## v0.5.0", self.changelog())
        self.assertEqual(sorted(p.name for p in (self.root / "changelog.d").iterdir()),
                         ["2-x.md"])

    def test_bad_fragments_are_named(self):
        for name, text, reason in (
                ("1-empty.md", "\n\n", "empty"),
                ("2-heading.md", "## v9.9.9\n- **X.** Y.\n", "headings"),
                ("3-h1.md", "- **X.** Y.\n# Title\n", "headings"),
                ("4-prose.md", "Just a sentence.\n", "bullet"),
                ("5-group-only.md", "Security hardening:\n\n", "nothing under it"),
                ("6-notes.txt", "- **X.** Y.\n", ".md files"),
                ("7-bom-heading.md", "\ufeff## v9.9.9\n", "headings"),
                ("8-trailing.md", "- **X.** Y.\n##\t\n", "headings")):
            self.add(name, text)
            with self.assertRaisesRegex(ChangelogError, re.escape(name) + ".*" + reason):
                changelog.check(self.root)
            (self.root / "changelog.d" / name).unlink()
        self.add("9-subheading.md", "- **X.** Y.\n\n### Details\n\n- more\n")   # level 3 is fine
        self.add("10-bom.md", "\ufeff- **X.** Y.\n")
        changelog.check(self.root)

    def test_no_release_while_phase_c_is_not_complete(self):
        # #111 C2b-2: Phase C ships as one release. While PHASE_C_COMPLETE is
        # False (or cannot be read), no release section or VERSION is written,
        # and the fragments stay; --check and --preview keep working.
        self.add("1-a.md", "- **A.** First.\n")
        version = self.root / "VERSION"
        version.write_text("0.4.3\n", encoding="ascii")
        for state in ("False", "missing", "garbled", "None"):
            with self.subTest(state=state):
                path = self.root / "workspace" / "keystore.py"
                if state == "missing":
                    path.unlink()
                elif state == "garbled":
                    path.write_text("PHASE_C_COMPLETE = (\n", encoding="utf-8")
                else:
                    keystore(self.root, state)
                with self.assertRaisesRegex(ChangelogError, re.escape(
                        "Phase C (#111) is not complete: no release can be prepared from "
                        "this commit.")):
                    changelog.release("v0.5.0", root=self.root, date="2026-10-21")
                self.assertEqual(self.changelog(), HEAD)
                self.assertEqual(version.read_text(encoding="ascii"), "0.4.3\n")
                self.assertTrue((self.root / "changelog.d" / "1-a.md").exists())
                self.assertEqual(changelog.planned(root=self.root), "- **A.** First.")
                changelog.check(self.root)
        keystore(self.root, True)
        changelog.release("v0.5.0", root=self.root, date="2026-10-21")
        self.assertIn("## v0.5.0 (2026-10-21)", self.changelog())
        self.assertEqual(version.read_text(encoding="ascii"), "0.5.0\n")

    def test_the_reader_agrees_with_the_repository(self):
        # The constant read by parsing equals the one the app imports.
        sys.path.insert(0, str(ROOT))
        try:
            from workspace import keystore as real
        finally:
            sys.path.remove(str(ROOT))
        self.assertIs(changelog.phase_c_complete(ROOT), real.PHASE_C_COMPLETE)

    def test_command_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(changelog.main(["--check"]), 0)
            self.assertEqual(changelog.main(["--preview"]), 0)
        self.assertIn("fragment(s) OK", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(changelog.main(["--preview", "v999.0.0"]), 0)
        self.assertTrue(out.getvalue().strip())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            changelog.main([])


if __name__ == "__main__":
    unittest.main()
