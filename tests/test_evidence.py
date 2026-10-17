# tests/test_evidence.py
# Release evidence names its package (#111 B3; tools/evidence.py). Each
# release-time check records the zip's SHA-256, the checkout it ran from, and
# whether it ran on exactly that zip's files, and a release is refused without
# one passing report of every kind naming the zip being published.

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import evidence  # noqa: E402

SHA = "a" * 64
COMMIT = "c" * 40
CLEAN = {"tools_commit": COMMIT, "tools_clean": True}


def clean():
    """tools_identity() as from a clean checkout of the package's commit."""
    return mock.patch.object(evidence, "tools_identity", return_value=CLEAN)
LONG_S = chr(0x17F)      # Latin small long s: a different name on NTFS, "s" to casefold()


def make_package(root, version="0.5.0", release=True):
    """A small package zip and the folder it was extracted (installed) to."""
    name = f"Unminute-{version}"
    folder = Path(root) / "versions" / name
    files = {"app/workspace/__init__.py": b"# app\n",
             "app/workspace/__pycache__/__init__.cpython-314.pyc": b"\0pyc",
             "runtime/lib/site.pth": b"import sitecustomize\n",
             "runtime/python.exe": b"MZ not really",
             "version.json": json.dumps({"version": version, "commit": COMMIT,
                                         "release": release}).encode()}
    for rel, data in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_bytes(data)
    manifest = {rel: [len(data), hashlib.sha256(data).hexdigest()]
                for rel, data in sorted(files.items())}
    (folder / "package-manifest.json").write_text(json.dumps(manifest, indent=0),
                                                  encoding="utf-8")
    zpath = Path(root) / f"{name}.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                z.write(p, f"{name}/{p.relative_to(folder).as_posix()}")
    return zpath, folder


def link_dir(target, link):
    """A directory link: a junction on Windows (no privilege needed), else a symlink."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def report(kind, sha=SHA, **change):
    ev = {"kind": kind, "package_sha256": sha, "commit": COMMIT, "release": True,
          "folder_matches_package": True, "folder_matched_before": True,
          "folder_differences": 0, "tools_commit": COMMIT, "tools_clean": True,
          "check_passed": True, "pass": True}
    if kind == "perf":
        ev["run"] = dict(evidence.PERF_RELEASE, quick=False)
    ev.update(change)
    return {"evidence": ev}


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class IdentityTests(TempDir):
    def listed(self, zpath, folder):
        return evidence.identity(zpath, folder)["folder_differences_listed"]

    def test_an_unchanged_folder_matches_its_zip(self):
        zpath, folder = make_package(self.root)
        ident = evidence.identity(zpath, folder)
        self.assertEqual(hashlib.sha256(zpath.read_bytes()).hexdigest(), ident["package_sha256"])
        self.assertEqual(("Unminute-0.5.0", "0.5.0", COMMIT, True),
                         (ident["package"], ident["version"], ident["commit"], ident["release"]))
        self.assertTrue(ident["folder_matches_package"])
        self.assertEqual(0, ident["folder_differences"])

    def test_changed_added_and_missing_files_are_found(self):
        zpath, folder = make_package(self.root)
        (folder / "app/workspace/__init__.py").write_bytes(b"# edited\n")
        (folder / "app/workspace/planted.pth").write_bytes(b"import os\n")
        (folder / "runtime/python.exe").unlink()
        self.assertEqual(["added: app/workspace/planted.pth",
                          "changed: app/workspace/__init__.py",
                          "missing: runtime/python.exe"], self.listed(zpath, folder))

    def test_names_compare_exactly(self):
        # Windows keeps names apart that casefold() treats as one: a planted
        # "ſite.pth" beside the listed "site.pth" must not pass as listed.
        zpath, folder = make_package(self.root)
        (folder / "runtime" / "lib" / f"{LONG_S}ite.pth").write_bytes(b"import os\n")
        self.assertEqual([f"added: runtime/lib/{LONG_S}ite.pth"], self.listed(zpath, folder))
        if os.name == "nt":                     # a case-only rename is a difference too
            (folder / "runtime" / "python.exe").rename(folder / "runtime" / "PYTHON.EXE")
            listed = self.listed(zpath, folder)
            self.assertIn("added: runtime/PYTHON.EXE", listed)
            self.assertIn("missing: runtime/python.exe", listed)

    def test_a_same_size_edit_is_found(self):
        zpath, folder = make_package(self.root)
        (folder / "runtime/python.exe").write_bytes(b"MZ not REALLY")
        self.assertEqual(["changed: runtime/python.exe"], self.listed(zpath, folder))

    def test_the_folder_is_compared_with_the_zips_manifest_not_its_own(self):
        # Rewriting the installed manifest cannot hide a planted file.
        zpath, folder = make_package(self.root)
        (folder / "app/planted.py").write_bytes(b"x = 1\n")
        manifest = json.loads((folder / "package-manifest.json").read_text(encoding="utf-8"))
        manifest["app/planted.py"] = [6, hashlib.sha256(b"x = 1\n").hexdigest()]
        (folder / "package-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        listed = self.listed(zpath, folder)
        self.assertIn("added: app/planted.py", listed)
        self.assertIn("changed: package-manifest.json", listed)

    def test_a_link_inside_the_folder_is_a_difference_and_never_followed(self):
        zpath, folder = make_package(self.root)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "Client letter.docx").write_bytes(b"private")
        link_dir(outside, folder / "app" / "linked")
        listed = self.listed(zpath, folder)
        self.assertEqual(["link: app/linked"], listed)        # no name from outside

    def test_a_hard_link_is_a_difference(self):
        # Content that matches but lives elsewhere too could change after the run.
        zpath, folder = make_package(self.root)
        outside = self.root / "outside.exe"
        outside.write_bytes(b"MZ not really")
        (folder / "runtime" / "python.exe").unlink()
        os.link(outside, folder / "runtime" / "python.exe")
        self.assertEqual(["hard link: runtime/python.exe"], self.listed(zpath, folder))

    def test_an_extra_folder_is_a_difference(self):
        zpath, folder = make_package(self.root)
        (folder / "app" / "empty").mkdir()
        self.assertEqual(["added folder: app/empty"], self.listed(zpath, folder))

    def test_manifest_names_that_leave_the_folder_are_refused(self):
        outside = self.root / "outside.txt"
        outside.write_bytes(b"x")
        folder = self.root / "pkg"
        folder.mkdir()
        manifest = {"../outside.txt": [1, hashlib.sha256(b"x").hexdigest()],
                    str(outside): [1, hashlib.sha256(b"x").hexdigest()],
                    "a\\b.py": [0, ""], "": [0, ""]}
        data = json.dumps(manifest).encode()
        (folder / "package-manifest.json").write_bytes(data)
        differences = evidence.folder_differences(folder, data)
        self.assertEqual(4, sum(d.startswith("unsafe name in the manifest") for d in differences))
        self.assertFalse(any(d.startswith(("missing", "changed")) for d in differences))

    def test_a_development_build_is_not_a_release(self):
        zpath, folder = make_package(self.root, release=False)
        self.assertFalse(evidence.identity(zpath, folder)["release"])

    def test_a_missing_folder_is_a_difference_not_a_crash(self):
        zpath, _ = make_package(self.root)
        ident = evidence.identity(zpath, self.root / "nowhere")
        self.assertFalse(ident["folder_matches_package"])
        self.assertTrue(ident["folder_differences"] > 0)

    def test_a_long_list_of_differences_is_capped_but_counted(self):
        zpath, folder = make_package(self.root)
        for i in range(evidence.MAX_LISTED + 5):
            (folder / f"app/extra{i}.py").write_bytes(b"")
        ident = evidence.identity(zpath, folder)
        self.assertEqual(evidence.MAX_LISTED + 5, ident["folder_differences"])
        self.assertEqual(evidence.MAX_LISTED, len(ident["folder_differences_listed"]))

    def tools_identity_with(self, status="", tags="H tools/evidence.py\n"):
        def git(cmd, **kwargs):
            self.assertEqual(["git", "--no-replace-objects", "-C"], cmd[:3])
            sub = cmd[4]
            if sub == "status":
                self.assertEqual(["--porcelain", "--untracked-files=all", "--ignored", "--",
                                  "tools", "tests", "docs"], cmd[5:])
            out = {"rev-parse": COMMIT + "\n", "status": status, "ls-files": tags}[sub]
            return subprocess.CompletedProcess(cmd, 0, out, "")
        with mock.patch.object(evidence.subprocess, "run", git):
            return evidence.tools_identity()

    def test_the_checkout_the_tools_ran_from_is_recorded(self):
        self.assertEqual(CLEAN, self.tools_identity_with())
        for status, tags in (("?? tools/extra.py\n", "H tools/evidence.py\n"),
                             ("!! tools/__pycache__/evidence.cpython-314.pyc\n",
                              "H tools/evidence.py\n"),
                             ("", "h tools/evidence.py\n"),          # assume-unchanged
                             ("", "S docs/perf-budgets.json\n")):     # skip-worktree
            with self.subTest(status=status, tags=tags):
                self.assertFalse(self.tools_identity_with(status, tags)["tools_clean"])
        with mock.patch.object(evidence.subprocess, "run", side_effect=OSError("no git")):
            self.assertEqual({"tools_commit": None, "tools_clean": False},
                             evidence.tools_identity())


class ZipTests(TempDir):
    def test_a_package_zip_is_read(self):
        zpath, _ = make_package(self.root)
        top, info, manifest = evidence.check_zip(zpath)
        self.assertEqual(("Unminute-0.5.0", "0.5.0"), (top, info["version"]))
        self.assertIn(b"version.json", manifest)

    def test_anything_else_stops_with_a_message(self):
        not_zip = self.root / "notes.txt"
        not_zip.write_text("x")
        two = self.root / "two.zip"
        with zipfile.ZipFile(two, "w") as z:
            z.writestr("A/version.json", "{}")
            z.writestr("B/version.json", "{}")
        bare = self.root / "bare.zip"
        with zipfile.ZipFile(bare, "w") as z:
            z.writestr("Unminute-0.5.0/version.json", "{}")
        odd = self.root / "odd.zip"
        with zipfile.ZipFile(odd, "w") as z:
            z.writestr("Unminute-0.5.0/version.json", "[]")
            z.writestr("Unminute-0.5.0/package-manifest.json", "{}")
        for path in (self.root / "absent.zip", not_zip, two, bare, odd):
            with self.subTest(path=path.name), \
                    self.assertRaisesRegex(SystemExit, "is not a package zip"):
                evidence.check_zip(path)


class StampTests(TempDir):
    def test_evidence_passes_only_on_the_unchanged_package_before_and_after(self):
        zpath, folder = make_package(self.root)
        before = evidence.identity(zpath, folder)
        self.assertTrue(evidence.stamp({}, "ui", zpath, folder, True, before)["pass"])
        self.assertFalse(evidence.stamp({}, "ui", zpath, folder, False, before)["pass"])
        self.assertFalse(evidence.stamp({}, "ui", zpath, folder, True)["pass"])   # no before
        (folder / "app/extra.py").write_bytes(b"")
        ev = evidence.stamp({}, "ui", zpath, folder, True, before)
        self.assertEqual((True, True, False), (ev["check_passed"], ev["folder_matched_before"],
                                               ev["pass"]))

    def test_stamping_a_report_file_keeps_its_figures(self):
        zpath, folder = make_package(self.root)
        out = self.root / "ui.json"
        out.write_text(json.dumps({"api_surface": ["status"]}), encoding="utf-8")
        with mock.patch("builtins.print"):
            evidence.stamp_file(out, "perf", zpath, folder, True,
                                evidence.identity(zpath, folder), run={"runs": 3})
        saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(["status"], saved["api_surface"])
        self.assertEqual(("perf", {"runs": 3}), (saved["evidence"]["kind"],
                                                 saved["evidence"]["run"]))
        self.assertEqual(hashlib.sha256(zpath.read_bytes()).hexdigest(),
                         saved["evidence"]["package_sha256"])
        self.assertIn("tools_commit", saved["evidence"])

    def test_an_unknown_kind_is_refused(self):
        zpath, folder = make_package(self.root)
        with self.assertRaises(ValueError):
            evidence.stamp({}, "vibes", zpath, folder, True)


class CheckTests(TempDir):
    def write(self, name, data):
        path = self.root / name
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def complete(self, **changes):
        return [self.write(f"{k}.json", changes.get(k, report(k))) for k in evidence.KINDS]

    def test_one_passing_report_of_every_kind_is_complete(self):
        self.assertEqual([], evidence.check(SHA, self.complete()))
        self.assertEqual([], evidence.check(f"  {SHA.upper()}\n", self.complete()))
        self.assertEqual([], evidence.check(SHA, self.complete(), commit=COMMIT))

    def test_each_problem_is_named(self):
        cases = {
            "names another package": report("perf", sha="b" * 64),
            "ran on a development build": report("perf", release=False),
            "differs from the package": report("perf", folder_matches_package=False),
            "differs from the package ": report("perf", folder_matched_before=False),
            "differs from the package  ": report("perf", folder_differences=3),
            "from a checkout of another commit": report("perf", tools_commit="d" * 40),
            "from a checkout with changes": report("perf", tools_clean=False),
            "did not pass": report("perf", check_passed=False, **{"pass": False}),
            "was not a full release run": report("perf", run=dict(evidence.PERF_RELEASE,
                                                                  quick=False, runs=1)),
            "was not a full release run ": report("perf", run=dict(evidence.PERF_RELEASE,
                                                                   quick=True)),
        }
        for words, bad in cases.items():
            with self.subTest(words):
                problems = evidence.check(SHA, self.complete(perf=bad))
                self.assertEqual(1, len(problems), problems)
                self.assertIn(words.strip(), problems[0])

    def test_malformed_fields_fail_without_a_crash(self):
        for words, bad in (("was not a full release run", report("perf", run=[1])),
                           ("was not a full release run", report("perf", run="full")),
                           ("differs from the package", report("perf", folder_differences=False)),
                           ("differs from the package", report("perf", folder_differences=0.0))):
            with self.subTest(bad=bad["evidence"]):
                problems = evidence.check(SHA, self.complete(perf=bad))
                self.assertEqual(1, len(problems), problems)
                self.assertIn(words, problems[0])

    def test_the_commit_given_must_be_the_packages(self):
        problems = evidence.check(SHA, self.complete(), commit="d" * 40)
        self.assertEqual(5, len(problems))
        self.assertTrue(all("names commit" in p for p in problems))
        self.assertIn("40 hex digits", evidence.check(SHA, self.complete(), commit="v0.5.0")[0])

    def test_the_report_itself_must_have_passed(self):
        for body in ({"pass": False}, {"pass": "true"}, {"failed": ["self-test passed"]}):
            with self.subTest(body=body):
                bad = report("packaged")
                bad.update(body)
                problems = evidence.check(SHA, self.complete(packaged=bad))
                self.assertEqual(["packaged.json: packaged did not pass"], problems)
        problems = evidence.check(SHA, self.complete(ui=report("ui", **{"pass": "true"})))
        self.assertEqual(["ui.json: ui did not pass"], problems)

    def test_missing_duplicate_unknown_and_unreadable_reports(self):
        paths = self.complete()
        paths.remove(self.root / "crash.json")
        paths.append(self.write("perf-again.json", report("perf")))
        paths.append(self.write("odd.json", report("vibes")))
        paths.append(self.write("listed.json", report(["perf"])))
        paths.append(self.write("plain.json", {"pass": True}))
        paths.append(self.write("twice.json", '{"evidence": {"kind": "crash"}, '
                                              '"evidence": {"kind": "crash"}}'))
        paths.append(self.root / "absent.json")
        problems = evidence.check(SHA, paths)
        unreadable = "not an evidence report (unreadable, or no evidence block)"
        self.assertIn("perf-again.json: a second perf report (also perf.json)", problems)
        self.assertIn("odd.json: unknown kind 'vibes'", problems)
        self.assertIn("listed.json: unknown kind ['perf']", problems)
        for name in ("plain.json", "twice.json", "absent.json"):
            self.assertIn(f"{name}: {unreadable}", problems)
        self.assertIn(f"missing: crash ({evidence.KINDS['crash']})", problems)
        self.assertEqual(7, len(problems))

    def test_no_reports_means_every_kind_is_missing(self):
        self.assertEqual(len(evidence.KINDS), len(evidence.check(SHA, [])))

    def test_the_expected_checksum_must_be_a_sha256(self):
        for bad in ("", "abc", SHA[:-1] + "g", SHA + "a", "sha256:" + SHA):
            with self.subTest(bad=bad):
                problems = evidence.check(bad, self.complete())
                self.assertEqual(1, len(problems))
                self.assertIn("64 hex digits", problems[0])

    def test_the_command_line_exit_code(self):
        with mock.patch("builtins.print"):
            self.assertEqual(0, evidence.main(["check", SHA, *map(str, self.complete())]))
            self.assertEqual(0, evidence.main(["check", SHA, "--commit", COMMIT,
                                               *map(str, self.complete())]))
            self.assertEqual(1, evidence.main(["check", SHA]))


class PrepareTests(TempDir):
    """evidence.prepare runs before a check: --zip needs the package's folder;
    with --app the check runs on that package's Python, as the app does, with
    the app's modules from that folder; given --zip, the folder must already be
    exactly that zip's, and a --zip that is not a package stops it at once."""

    def parse(self, *argv):
        ap = argparse.ArgumentParser(allow_abbrev=False)
        ap.add_argument("--app")
        ap.add_argument("--zip")
        return ap, ap.parse_args(argv)

    def package_python(self, folder, *, flags=True):
        exe = str(folder / "runtime" / "python.exe")
        f = types.SimpleNamespace(isolated=int(flags), dont_write_bytecode=int(flags))
        modules = {n: types.SimpleNamespace(__file__=str(folder / "app" / n / "__init__.py"))
                   for n in evidence.APP_MODULES}
        return [mock.patch.object(sys, "_base_executable", exe, create=True),
                mock.patch.object(sys, "executable", exe),
                mock.patch.object(sys, "flags", f),
                mock.patch.object(sys, "_xoptions",
                                  {"disable-remote-debug": True} if flags else {}),
                mock.patch.dict(sys.modules, modules), clean()]

    def run_prepare(self, patches, ap, args, app=None, version_dir=None):
        for p in patches:
            p.start()
        try:
            return evidence.prepare(ap, args, app, version_dir)
        finally:
            for p in reversed(patches):
                p.stop()

    def test_a_package_check_runs_as_the_app_does(self):
        zpath, folder = make_package(self.root)
        ap, args = self.parse("--app", str(folder / "app"), "--zip", str(zpath))
        before = self.run_prepare(self.package_python(folder), ap, args, folder / "app")
        self.assertTrue(before["folder_matches_package"])

    def test_without_zip_there_is_nothing_to_compare(self):
        _, folder = make_package(self.root)
        ap, args = self.parse("--app", str(folder / "app"))
        self.assertIsNone(self.run_prepare(self.package_python(folder), ap, args, folder / "app"))

    def test_zip_needs_app(self):
        ap, args = self.parse("--zip", "x.zip")
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            evidence.prepare(ap, args, None)

    def test_another_python_is_refused(self):
        _, folder = make_package(self.root)
        ap, args = self.parse("--app", str(folder / "app"))
        with self.assertRaisesRegex(SystemExit, "package's own Python"):
            evidence.prepare(ap, args, folder / "app")

    def test_the_apps_flags_are_needed(self):
        _, folder = make_package(self.root)
        ap, args = self.parse("--app", str(folder / "app"))
        with self.assertRaisesRegex(SystemExit, "with -I, -B, -X disable-remote-debug"):
            self.run_prepare(self.package_python(folder, flags=False), ap, args, folder / "app")

    def test_app_modules_must_come_from_the_package(self):
        _, folder = make_package(self.root)
        ap, args = self.parse("--app", str(folder / "app"))
        patches = self.package_python(folder)
        patches.append(mock.patch.dict(sys.modules, {"asr_engines": types.SimpleNamespace(
            __file__=str(self.root / "checkout" / "asr_engines.py"))}))
        with self.assertRaisesRegex(SystemExit, "asr_engines was imported from"):
            self.run_prepare(patches, ap, args, folder / "app")

    def test_a_wrong_zip_or_a_changed_folder_stops_before_the_check(self):
        zpath, folder = make_package(self.root)
        ap, args = self.parse("--zip", str(self.root / "no.zip"))
        with self.assertRaisesRegex(SystemExit, "is not a package zip"), clean():
            evidence.prepare(ap, args, version_dir=folder)
        (folder / "app" / "planted.py").write_bytes(b"")
        ap, args = self.parse("--zip", str(zpath))
        with self.assertRaisesRegex(SystemExit, "does not hold exactly the files"), clean():
            evidence.prepare(ap, args, version_dir=folder)

    def test_an_unclean_checkout_stops_before_the_check(self):
        zpath, folder = make_package(self.root)
        ap, args = self.parse("--zip", str(zpath))
        with self.assertRaisesRegex(SystemExit, "git clean -fdX -- tools tests docs"), \
                mock.patch.object(evidence, "tools_identity",
                                  return_value=dict(CLEAN, tools_clean=False)):
            evidence.prepare(ap, args, version_dir=folder)

    def test_the_app_goes_on_the_path_only_if_it_is_not_there(self):
        app = self.root / "app"
        app.mkdir()
        with mock.patch.object(sys, "path", ["", str(self.root / "lib"), str(app)]):
            evidence.add_app(app)
            self.assertEqual(["", str(self.root / "lib"), str(app)], sys.path)
        with mock.patch.object(sys, "path", ["", str(self.root / "lib")]):
            evidence.add_app(app)
            self.assertEqual(str(app.resolve()), sys.path[0])

    def test_app_folder_reads_app_from_the_command_line(self):
        default = Path("checkout")
        self.assertEqual(default, evidence.app_folder(default, ["x.py", "out.json"]))
        self.assertEqual((self.root / "app").resolve(),
                         evidence.app_folder(default, ["x.py", "--app", str(self.root / "app")]))
        self.assertEqual((self.root / "app").resolve(),
                         evidence.app_folder(default, ["x.py", f"--app={self.root / 'app'}"]))


class ToolTests(TempDir):
    def source(self, tool):
        return (ROOT / "tools" / tool).read_text(encoding="utf-8")

    def test_every_kind_is_stamped_by_its_tool_with_the_before_comparison(self):
        for kind, described in evidence.KINDS.items():
            with self.subTest(kind=kind):
                source = (ROOT / described.split()[0]).read_text(encoding="utf-8")
                self.assertRegex(source,
                                 rf'(?s)stamp_file\(.{{0,120}}"{kind}".{{0,300}}?before=before')
                self.assertIn("before = evidence.prepare(", source)
                self.assertIn("allow_abbrev=False", source)

    def test_the_window_checks_prepare_before_they_run_and_pass_on_their_verdict(self):
        for tool, verdict in (("workspace_ui_check.py", "not bad"),
                              ("accessibility_check.py", "not problems")):
            with self.subTest(tool=tool):
                main = self.source(tool).split("\ndef main():\n", 1)[1]
                prepare = main.index(
                    "before = evidence.prepare(ap, args, APP if args.app else None)")
                self.assertLess(prepare, main.index("Workspace("))
                self.assertIn(f"passed={verdict},", main)
                self.assertIn('return 0 if ev["pass"] else 1', main)

    def test_package_checks_start_the_app_as_the_shortcut_does(self):
        flags = '"-I", "-B", "-X", "disable-remote-debug"'
        for tool in ("packaged_check.py", "packaged_crash_check.py", "perf_check.py"):
            with self.subTest(tool=tool):
                self.assertIn(flags, self.source(tool))
        self.assertEqual(3, self.source("packaged_crash_check.py").count('["py"], *FLAGS,'))

    def test_helper_modules_cannot_shadow_the_standard_library_or_the_app(self):
        # tools/ and tests/ go on the import path after the standard library
        # (sys.path.append), and no module there takes a standard or app name.
        taken = set(sys.stdlib_module_names) | set(evidence.APP_MODULES)
        for folder in (ROOT / "tools", ROOT / "tests"):
            for p in folder.glob("*.py"):
                self.assertNotIn(p.stem, taken, p)
        for tool in ("perf_check.py", "packaged_check.py", "packaged_crash_check.py",
                     "workspace_ui_check.py", "accessibility_check.py", "webview_trial.py"):
            with self.subTest(tool=tool):
                text = self.source(tool)
                self.assertNotRegex(text, r'sys\.path\.insert\(0, str\([^)]*"(tools|tests)"\)')
                self.assertNotRegex(text, r"sys\.path\.insert\(0, str\(Path\(__file__\)"
                                          r"\.resolve\(\)\.parent\)\)")

    @unittest.skipUnless(os.name == "nt", "webview_trial imports Windows-only modules")
    def test_with_app_the_trial_helper_imports_the_packages_workspace(self):
        app = self.root / "Unminute-0.5.0" / "app"
        (app / "workspace").mkdir(parents=True)
        (app / "workspace" / "__init__.py").write_text("")
        (app / "workspace" / "shell.py").write_text("")
        (app / "workspace" / "bridge.py").write_text("def make_bridge(*a, **k):\n    pass\n")
        code = ("import sys; sys.path.append(sys.argv[3]); import webview_trial, workspace; "
                "print(workspace.__file__)")
        r = subprocess.run([sys.executable, "-I", "-B", "-c", code, "--app", str(app),
                            str(ROOT / "tools")], capture_output=True, text=True, cwd=self.root)
        self.assertEqual(0, r.returncode, r.stderr[-500:])
        self.assertEqual(Path(r.stdout.strip()).resolve(),
                         (app / "workspace" / "__init__.py").resolve())

    def run_perf(self, zpath, folder, rc, *extra):
        import perf_check
        out = self.root / "perf.json"

        def parent(args):
            out.write_text(json.dumps({"median": {}}), encoding="utf-8")
            return rc
        argv = ["perf_check.py", str(out), "--app", str(folder / "app"), "--zip", str(zpath),
                *extra]
        with mock.patch.object(perf_check, "parent", parent), \
                mock.patch.object(perf_check.evidence, "require_package_runtime"), \
                mock.patch.object(perf_check.evidence, "require_app_modules"), \
                clean(), \
                mock.patch.object(sys, "argv", argv), mock.patch("builtins.print"):
            code = perf_check.main()
        return code, json.loads(out.read_text(encoding="utf-8"))["evidence"]

    def test_perf_evidence_needs_a_full_release_run_within_budget(self):
        import perf_check
        self.assertEqual(perf_check.LEAK_BUDGET_MINUTES, evidence.PERF_RELEASE["leak_minutes"])
        zpath, folder = make_package(self.root)
        code, ev = self.run_perf(zpath, folder, 0, "--leak-minutes", "30")
        self.assertEqual((0, True), (code, ev["pass"]))
        self.assertEqual(dict(evidence.PERF_RELEASE, quick=False), ev["run"])
        for rc, extra in ((1, ()), (0, ("--quick",)), (0, ("--runs", "1")),
                          (0, ("--settle", "0")), (0, ("--window", "5"))):
            with self.subTest(rc=rc, extra=extra):
                code, ev = self.run_perf(zpath, folder, rc, "--leak-minutes", "30", *extra)
                self.assertFalse(ev["pass"])
                self.assertEqual(1, code)                   # evidence that fails exits 1
        code, ev = self.run_perf(zpath, folder, 0, "--leak-minutes", "5")
        self.assertEqual((1, False), (code, ev["pass"]))

    def test_perf_evidence_needs_the_app_it_measured(self):
        import perf_check
        argv = ["perf_check.py", str(self.root / "perf.json"), "--zip", "x.zip"]
        with mock.patch.object(sys, "argv", argv), mock.patch("sys.stderr"), \
                self.assertRaises(SystemExit):
            perf_check.main()


@unittest.skipUnless(os.name == "nt", "the checks import Windows-only modules")
class PackagedCheckTests(TempDir):
    # The figures of the B2b release build's run (#185), less the paths.
    GOOD = {"self_test": "PASS",
            "lifecycle": {"meetings_imported": 1, "transcribed_s": 1075, "worker_peak_mb": 2463,
                          "speakers": "done", "passages": 372, "meetings_left": 0,
                          "receipts": ["audio removed", "transcript removed"],
                          "blocked_network_attempts": 0},
            "residual": {"files_after_cleanup": ["settings.json", "workspace.db"],
                         "marker_found_in": [], "unreadable": [], "content_backups": []},
            "idle_window": {"window_running": True, "processes": 7,
                            "cpu_percent_of_one_core": 1.5, "working_set_mb": 484}}

    def setUp(self):
        super().setUp()
        import packaged_check
        self.tool = packaged_check

    def changed(self, section, **values):
        r = json.loads(json.dumps(self.GOOD))
        if section:
            r[section].update(values)
        else:
            r.update(values)
        return r

    def test_a_good_run_passes(self):
        self.assertEqual([], self.tool.verdict(self.GOOD))

    def test_each_criterion_fails_on_its_own(self):
        cases = {
            "no error": self.changed(None, error="RuntimeError: x"),
            "self-test passed": self.changed(None, self_test="FAIL"),
            "transcribed with speakers": self.changed("lifecycle", speakers="failed"),
            "cleaned up, with both receipts": self.changed("lifecycle",
                                                           receipts=["audio removed"]),
            "no network attempts by the app process": self.changed(
                "lifecycle", blocked_network_attempts=1),
            "no content left behind": self.changed("residual", marker_found_in=["workspace.db"]),
        }
        for name, r in cases.items():
            with self.subTest(name):
                self.assertEqual([name], self.tool.verdict(r))
        for change in ({"window_running": False}, {"processes": 1}, {"working_set_mb": 0}):
            with self.subTest(change=change):
                self.assertEqual(["idle window measured"],
                                 self.tool.verdict(self.changed("idle_window", **change)))
        for change in ({"unreadable": ["workspace.db"]}, {"content_backups": ["x.bak"]}):
            with self.subTest(change=change):
                self.assertEqual(["no content left behind"],
                                 self.tool.verdict(self.changed("residual", **change)))

    def test_a_lifecycle_error_fails(self):
        r = self.changed(None, lifecycle={"error": "lifecycle script exceeded 7,200 s"})
        self.assertIn("no error", self.tool.verdict(r))

    def run_main(self, tool, filled, *extra, folder=None):
        if folder is None:
            _, folder = make_package(self.root)
        out = self.root / "report.json"
        calls = []

        def check(version_dir, cache, wav, report, *rest):
            calls.append(version_dir)
            report.update(json.loads(json.dumps(filled)))
        argv = ["tool.py", str(folder), "cache", "x.wav", str(out), *extra]
        with mock.patch.object(tool, "check", check), mock.patch.object(sys, "argv", argv), \
                clean(), mock.patch("builtins.print"):
            code = tool.main()
        saved = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
        return code, saved, calls

    def test_packaged_exit_code_and_evidence(self):
        zpath, folder = make_package(self.root)
        code, saved, _ = self.run_main(self.tool, self.GOOD, "--zip", str(zpath), folder=folder)
        self.assertEqual((0, "packaged", True), (code, saved["evidence"]["kind"],
                                                 saved["evidence"]["pass"]))
        code, saved, _ = self.run_main(self.tool, self.GOOD)
        self.assertEqual((0, True), (code, saved["pass"]))
        code, saved, _ = self.run_main(self.tool, self.changed(None, self_test="FAIL"))
        self.assertEqual((1, ["self-test passed"]), (code, saved["failed"]))

    def test_a_wrong_zip_or_folder_stops_before_the_run(self):
        other_zip = make_package(self.root / "other", version="0.6.0")[0]
        _, folder = make_package(self.root)
        for tool_name in ("packaged_check", "packaged_crash_check"):
            tool = __import__(tool_name)
            for zpath, words in ((self.root / "absent.zip", "is not a package zip"),
                                 (other_zip, "does not hold exactly the files")):
                with self.subTest(tool=tool_name, words=words):
                    check = mock.Mock()
                    argv = ["tool.py", str(folder), "cache", "x.wav",
                            str(self.root / "report.json"), "--zip", str(zpath)]
                    with self.assertRaisesRegex(SystemExit, words), \
                            mock.patch.object(tool, "check", check), clean(), \
                            mock.patch.object(sys, "argv", argv):
                        tool.main()
                    check.assert_not_called()

    def test_the_crash_check_passes_only_on_its_verdict(self):
        import packaged_crash_check as crash
        code, saved, _ = self.run_main(crash, {"pass": True, "failed": []})
        self.assertEqual((0, True), (code, saved["pass"]))
        code, saved, _ = self.run_main(crash, {"pass": False, "failed": ["x"]})
        self.assertEqual(1, code)

    def test_a_crash_check_that_stops_says_so(self):
        import packaged_crash_check as crash

        def check(*args):
            raise RuntimeError("boom")
        out = self.root / "crash.json"
        _, folder = make_package(self.root)
        argv = ["tool.py", str(folder), "cache", "x.wav", str(out)]
        with mock.patch.object(crash, "check", check), mock.patch.object(sys, "argv", argv), \
                mock.patch("builtins.print"):
            self.assertEqual(1, crash.main())
        saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((False, ["the check stopped (RuntimeError)"]),
                         (saved["pass"], saved["failed"]))


if __name__ == "__main__":
    unittest.main()
