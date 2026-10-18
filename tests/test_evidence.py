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
import time
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


IMPORT_LISTS = ("path", "meta_path", "path_hooks")     # what decides where imports come from
SAFE_METHODS = ("append", "extend", "count", "index", "copy")


def path_prepends(source):
    """Lines where code (not text in a string) could put something ahead of
    the standard library: any method but append, extend, count, index or copy
    called on sys.path, sys.meta_path or sys.path_hooks; an assignment to or
    deletion of one of them or its items (`sys.path += [x]` only appends); or
    an alias of sys or of those lists, which the rest of the check could not
    follow."""
    import ast

    def is_list(node):
        return (isinstance(node, ast.Attribute) and node.attr in IMPORT_LISTS
                and isinstance(node.value, ast.Name) and node.value.id == "sys")

    def names(target):
        if isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                yield from names(item)
        elif isinstance(target, ast.Starred):
            yield from names(target.value)
        else:
            yield target

    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if is_list(node.func.value) and node.func.attr not in SAFE_METHODS:
                found.add(node.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [
                node.target]
            for target in (t for each in targets for t in names(each)):
                item = isinstance(target, ast.Subscript) and is_list(target.value)
                whole = is_list(target) and not isinstance(node, ast.AugAssign)
                if item or whole:
                    found.add(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == "sys" and a.asname for a in node.names):
                found.add(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module == "sys":
            if any(a.name in IMPORT_LISTS for a in node.names):
                found.add(node.lineno)
    return sorted(found)


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
    body = {"evidence": ev}
    if kind == "perf":
        ev["run"] = dict(evidence.PERF_RELEASE, quick=False)
        body["machine"] = dict(MACHINE)
    ev.update(change)
    return body


# A synthetic machine, below the 8-core, 16 GB design aim on purpose: there is
# no named reference machine, so any specification recorded in full will do.
MACHINE = {"cpu": "Synthetic CPU 4000", "cores": 4, "logical_processors": 4,
           "memory_gb": 7.8, "windows": "Windows 11 24H2", "os_build": "10.0.26100.4061",
           "power_plan": "Balanced", "on_mains_power": True, "battery": True,
           "gpus": [{"name": "Synthetic GPU", "driver": "31.0.1"}], "webview2": "140.0.1.2"}


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

    def test_git_settings_from_the_environment_are_not_used(self):
        seen = []

        def git(cmd, **kwargs):
            seen.append(kwargs["env"])
            out = {"rev-parse": COMMIT, "status": "", "ls-files": ""}[cmd[4]]
            return subprocess.CompletedProcess(cmd, 0, out, "")
        with mock.patch.dict(os.environ, {"GIT_DIR": "elsewhere/.git", "GIT_WORK_TREE": "x"}), \
                mock.patch.object(evidence.subprocess, "run", git):
            evidence.tools_identity()
        self.assertTrue(seen)
        for env in seen:
            self.assertFalse([k for k in env if k.upper().startswith("GIT_")])


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
            for top in ("A", "B"):
                z.writestr(f"{top}/version.json", "{}")
                z.writestr(f"{top}/package-manifest.json", "{}")
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


class NamesTests(TempDir):
    """evidence.py names: the release job's check that the zip it built is the
    release build of the tag's version and commit (#111 B3b)."""

    def test_the_release_build_of_this_version_and_commit(self):
        zpath, _ = make_package(self.root)
        self.assertEqual([], evidence.names(zpath, "0.5.0", COMMIT))
        with mock.patch("builtins.print"):
            self.assertEqual(0, evidence.main(["names", str(zpath), "0.5.0", COMMIT]))

    def test_anything_else_is_named(self):
        zpath, _ = make_package(self.root)
        self.assertEqual(["its folder is Unminute-0.5.0, not Unminute-0.5.1",
                          "it is version 0.5.0, not 0.5.1"],
                         evidence.names(zpath, "0.5.1", COMMIT))
        self.assertEqual([f"it was built from {COMMIT}, not {'d' * 40}"],
                         evidence.names(zpath, "0.5.0", "d" * 40))
        dev, _ = make_package(self.root / "dev", release=False)
        self.assertEqual(["it is not a release build"], evidence.names(dev, "0.5.0", COMMIT))
        with mock.patch("builtins.print"):
            self.assertEqual(1, evidence.main(["names", str(dev), "0.5.0", COMMIT]))


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
        for change in ({"pass": "true"}, {"check_passed": False}):
            with self.subTest(change=change):
                problems = evidence.check(SHA, self.complete(ui=report("ui", **change)))
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


def perf_with(machine=None, drop=()):
    """A passing perf report whose machine block is changed (None: removed)."""
    body = report("perf")
    if machine is None and not drop:
        del body["machine"]
    else:
        body["machine"] = {k: v for k, v in dict(MACHINE, **(machine or {})).items()
                           if k not in drop}
    return body


class MachineEvidenceTests(TempDir):
    """Owner decision, 18 Oct 2026 (#121): no named reference machine. The
    performance report must record the specification of whichever machine
    measured it, in full; what that machine is does not matter."""

    write, complete = CheckTests.write, CheckTests.complete

    def test_a_report_made_before_the_machine_was_recorded_fails(self):
        problems = evidence.check(SHA, self.complete(perf=perf_with()))
        self.assertEqual(["perf.json: perf records no machine specification (made before it "
                          "was required: run tools/perf_check.py again)"], problems)
        for bad in ([], "Synthetic CPU", None):
            with self.subTest(bad=bad):
                body = report("perf")
                body["machine"] = bad
                problems = evidence.check(SHA, self.complete(perf=body))
                self.assertEqual(1, len(problems), problems)
                self.assertIn("records no machine specification", problems[0])

    def test_every_field_is_needed(self):
        for name in evidence.PERF_MACHINE:
            with self.subTest(missing=name):
                problems = evidence.check(SHA, self.complete(perf=perf_with(drop=(name,))))
                self.assertEqual([f"perf.json: perf records the machine only in part "
                                  f"(not read: {name})"], problems)

    def test_unreadable_or_malformed_fields_are_named(self):
        cases = {"cpu": ["", "   ", None, 4], "cores": [0, "8", True, 8.0, None],
                 "logical_processors": [None, -1], "memory_gb": [0, "16", True, None],
                 "windows": [None], "os_build": [None, 10], "power_plan": [None, ""],
                 "on_mains_power": [None, 1, "yes"], "battery": [None, 0],
                 "gpus": [None, "none", [{"name": "Synthetic GPU"}],
                          [{"name": "", "driver": None}], [{"name": "G", "driver": 31}],
                          [{"name": "G", "driver": None, "serial": "x"}], ["G"]],
                 "webview2": [None, ""]}
        for name, values in cases.items():
            for value in values:
                with self.subTest(field=name, value=value):
                    problems = evidence.check(SHA, self.complete(
                        perf=perf_with({name: value})))
                    self.assertEqual(1, len(problems), problems)
                    self.assertIn(f"not read: {name})", problems[0])

    def test_any_machine_recorded_in_full_will_do(self):
        # Below the recommended 8 cores and 16 GB, no GPU, on battery: still
        # evidence. The budgets decide whether the figures pass, not the machine.
        small = perf_with({"cores": 2, "logical_processors": 2, "memory_gb": 4.0, "gpus": [],
                           "on_mains_power": False, "battery": True,
                           "power_plan": "Ausbalanciert"})
        self.assertEqual([], evidence.check(SHA, self.complete(perf=small), commit=COMMIT))

    def test_other_kinds_need_no_machine(self):
        self.assertNotIn("machine", report("ui"))
        self.assertEqual([], evidence.check(SHA, self.complete()))

    def test_the_machine_as_one_line_for_the_release_notes(self):
        path = self.write("evidence-perf.json", report("perf"))
        with mock.patch("builtins.print") as out:
            self.assertEqual(0, evidence.main(["machine", str(path)]))
        self.assertEqual(
            "Performance measured on: Synthetic CPU 4000, 4 cores (4 logical processors), "
            "7.8 GB memory, Windows 11 24H2 (10.0.26100.4061), Balanced power plan, on mains "
            "power, GPU: Synthetic GPU (driver 31.0.1), WebView2 140.0.1.2.",
            out.call_args.args[0])
        line = evidence.machine_line(dict(MACHINE, gpus=[], battery=False))
        self.assertIn("on mains power (no battery), GPU: none,", line)
        self.assertIn("on battery,", evidence.machine_line(dict(MACHINE,
                                                                on_mains_power=False)))
        line = evidence.machine_line(dict(MACHINE, gpus=[{"name": "G", "driver": None}]))
        self.assertIn("GPU: G (driver not reported)", line)

    def test_the_machine_command_refuses_an_incomplete_or_unreadable_report(self):
        for name, body in (("old.json", perf_with()), ("part.json", perf_with(drop=("gpus",))),
                           ("list.json", [1]), ("bad.json", "{")):
            path = self.write(name, body)
            with self.subTest(name), mock.patch("builtins.print") as out:
                self.assertEqual(1, evidence.main(["machine", str(path)]))
                self.assertTrue(out.call_args.args[0].startswith("MACHINE PROBLEM: "))
        with mock.patch("builtins.print"):
            self.assertEqual(1, evidence.main(["machine", str(self.root / "absent.json")]))


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

    @unittest.skipUnless(os.name == "nt", "Windows windows")
    def test_the_ui_check_only_presses_its_own_confirmations(self):
        # A window with the same title in another process (a real Unminute's
        # "Discard this meeting?") is never found, so never pressed or closed.
        import workspace_ui_check
        title = f"Unminute evidence test {os.getpid()}"
        script = ("Add-Type -AssemblyName System.Windows.Forms; "
                  "$f = New-Object Windows.Forms.Form; $f.Text = $env:UNMINUTE_TITLE; "
                  "$f.Opacity = 0; $f.ShowInTaskbar = $false; $f.Show(); "
                  "[Console]::Out.WriteLine('shown'); [Console]::Out.Flush(); "
                  "for ($i = 0; $i -lt 100; $i++) { [Windows.Forms.Application]::DoEvents(); "
                  "Start-Sleep -Milliseconds 100 }")
        other = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                  script], stdout=subprocess.PIPE, text=True,
                                 env=dict(os.environ, UNMINUTE_TITLE=title))
        try:
            self.assertEqual("shown", other.stdout.readline().strip())
            import ctypes
            self.assertTrue(ctypes.WinDLL("user32").FindWindowW(None, title))   # it is there
            self.assertEqual(0, workspace_ui_check.find_dialog(title, timeout=1))
        finally:
            other.kill()
            other.wait(10)

    @unittest.skipUnless(os.name == "nt", "Windows windows")
    def test_the_ui_check_ticks_a_statement_behind_another_window(self):
        # The UI check's confirmations are usually behind the user's foreground
        # window (a process started from a terminal cannot take the foreground).
        # WinForms' CheckBox acts on a mouse-up only if WindowFromPoint there is
        # the check box itself, so a posted click on a covered statement is
        # lost; Tab and Space tick it. A form that is never activated, off
        # screen under a top-most cover, stands in for the confirmation. It is
        # in another process, so find_dialog is pointed at it.
        import contextlib
        import ctypes
        import io
        from ctypes import wintypes

        import workspace_ui_check
        title = f"Unminute tick test {os.getpid()}"
        statement = "I have read this synthetic statement."
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -WarningAction SilentlyContinue -ReferencedAssemblies System.Windows.Forms "
            "-TypeDefinition 'public class QuietForm : System.Windows.Forms.Form { protected "
            "override bool ShowWithoutActivation { get { return true; } } }'; "
            "$f = New-Object QuietForm; $f.Text = $env:UNMINUTE_TITLE; "
            "$f.ShowInTaskbar = $false; $f.StartPosition = 'Manual'; "
            "$f.Location = New-Object Drawing.Point(-32000, -32000); "
            "$cancel = New-Object Windows.Forms.Button; $cancel.Text = 'Cancel'; "
            "$check = New-Object Windows.Forms.CheckBox; $check.Text = $env:UNMINUTE_CHECK; "
            "$check.AutoSize = $true; $check.Top = 40; "
            "$check.add_CheckedChanged({ [Console]::Out.WriteLine('checked ' + $check.Checked); "
            "[Console]::Out.Flush() }); "
            "$f.Controls.Add($cancel); $f.Controls.Add($check); $f.Show(); $cancel.Select(); "
            "$cover = New-Object QuietForm; $cover.Text = $env:UNMINUTE_TITLE + ' cover'; "
            "$cover.ShowInTaskbar = $false; $cover.FormBorderStyle = 'None'; "
            "$cover.TopMost = $true; $cover.StartPosition = 'Manual'; $cover.Bounds = $f.Bounds; "
            "$cover.Show(); "
            "[Console]::Out.WriteLine('shown'); [Console]::Out.Flush(); "
            "for ($i = 0; $i -lt 300 -and $f.Visible; $i++) { "
            "[Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 50 }; "
            "[Console]::Out.WriteLine('done'); [Console]::Out.Flush()")
        form = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                 script], stdout=subprocess.PIPE, text=True,
                                env=dict(os.environ, UNMINUTE_TITLE=title,
                                         UNMINUTE_CHECK=statement))
        try:
            self.assertEqual("shown", form.stdout.readline().strip())
            user32 = ctypes.WinDLL("user32")
            user32.FindWindowW.restype = ctypes.c_void_p
            user32.WindowFromPoint.restype = ctypes.c_void_p
            hwnd = user32.FindWindowW(None, title)
            cover = user32.FindWindowW(None, title + " cover")
            self.assertTrue(hwnd and cover)
            check = workspace_ui_check.dialog_button(hwnd, statement)
            rect = wintypes.RECT()
            user32.GetWindowRect(ctypes.c_void_p(check), ctypes.byref(rect))

            def on_top():
                """The window at the check box's centre is the check box."""
                at = wintypes.POINT((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
                return user32.WindowFromPoint(at) == check
            with mock.patch.object(workspace_ui_check, "find_dialog", return_value=hwnd):
                self.assertFalse(on_top())
                # Covered: a posted click is lost.
                self.assertTrue(workspace_ui_check.press(["click:" + statement], title))
                # Tab to it and Space ticks it all the same.
                self.assertTrue(workspace_ui_check.press(["tab", "space-on:" + statement],
                                                         title))
                # Uncovered, the same click acts (it unticks), so the click
                # itself was sound and only the cover lost it.
                user32.PostMessageW(ctypes.c_void_p(cover), 0x0010, 0, 0)     # WM_CLOSE
                for _ in range(100):
                    if on_top():
                        break
                    time.sleep(0.05)
                self.assertTrue(on_top())
                self.assertTrue(workspace_ui_check.press(["click:" + statement], title))
                # A step press() does not know is refused, not silently skipped.
                with contextlib.redirect_stderr(io.StringIO()) as said:
                    self.assertFalse(workspace_ui_check.press(["space-at:" + statement], title))
                self.assertIn("unknown step", said.getvalue())
            user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)          # WM_CLOSE
            lines = [line.strip() for line in form.stdout]
            self.assertEqual(["checked True", "checked False", "done"], lines)
        finally:
            form.kill()
            form.wait(10)
            form.stdout.close()

    def test_the_ui_check_ticks_its_statements_with_the_keyboard(self):
        # A posted click cannot tick a statement behind the user's foreground
        # window (the test above), so the native checks never tick by click.
        native = self.source("workspace_ui_check.py").split(
            "\ndef native_checks(", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn('"click:I have read', native)
        self.assertIn('"tab", "space-on:" + ticked["check"]', native)
        self.assertIn('["tab", "space-on:" + keyed["check"]', native)

    def test_a_confirmation_left_open_fails_the_ui_check_instead_of_hanging(self):
        source = self.source("workspace_ui_check.py")
        run_with = source.split("    def run_with(", 1)[1].split("\n    title = ", 1)[0]
        self.assertIn("0x0010", run_with)                                   # WM_CLOSE
        self.assertIn('out.setdefault("stuck_dialogs", []).append(title)', run_with)
        self.assertIn("clicker.join(STUCK_DIALOG_S + 15)", run_with)
        # A dialog the script never found (shown late) is still watched and closed.
        self.assertIn('if not find_dialog(title, timeout=0.2) and done["ok"]:', run_with)
        finder = source.split("def find_dialog(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("owner.value == pid", finder)
        self.assertIn('if nv.get("stuck_dialogs"):', source)

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
        # Their own code never puts anything ahead of the standard library
        # (path_prepends). The app goes first only through evidence.add_app.
        for tool in ("perf_check.py", "packaged_check.py", "packaged_crash_check.py",
                     "workspace_ui_check.py", "accessibility_check.py", "process_listeners.py"):
            with self.subTest(tool=tool):
                self.assertEqual([], path_prepends(self.source(tool)))
        # Code they import: the fake engines the UI check loads in-process, and
        # evidence.py, whose only change is add_app's.
        self.assertEqual([], path_prepends((ROOT / "tests" / "fake_worker_engines.py")
                                           .read_text(encoding="utf-8")))
        import ast
        source = self.source("evidence.py")
        add_app = next(n for n in ast.walk(ast.parse(source))
                       if isinstance(n, ast.FunctionDef) and n.name == "add_app")
        lines = path_prepends(source)
        self.assertTrue(lines)
        self.assertTrue(all(add_app.lineno <= n <= add_app.end_lineno for n in lines), lines)
        # The scripts the package checks run inside the package (from its app
        # folder, under -I) put only that folder first.
        for tool, name in (("packaged_check.py", "LIFECYCLE"),
                           ("packaged_crash_check.py", "CHILD")):
            with self.subTest(script=name):
                script = next(n.value.value for n in ast.parse(self.source(tool)).body
                              if isinstance(n, ast.Assign) and n.targets[0].id == name)
                lines = path_prepends(script)
                self.assertEqual(1, len(lines))
                self.assertEqual('sys.path.insert(0, ".")',
                                 script.splitlines()[lines[0] - 1].strip())

    def test_the_path_check_sees_every_way_to_prepend(self):
        for code in ('sys.path.insert(0, str(ROOT / "tools"))',
                     'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))',
                     "sys.path.insert(0, str(Path(__file__).resolve().parent))",
                     "sys.path.insert(len(sys.path) - 1, here)",
                     "sys.path[:0] = [str(HERE)]",
                     "sys.path[:] = [x] + sys.path",
                     "sys.path = [str(HERE)] + sys.path",
                     "sys.path[0] = x",
                     "sys.path.__setitem__(slice(0, 0), [x])",
                     "sys.path.clear()",
                     "sys.path.reverse()",
                     "sys.path: list = [x]",
                     "sys.path, other = [x], 1",
                     "del sys.path[:1]",
                     "sys.meta_path.insert(0, finder)",
                     "sys.path_hooks.insert(0, hook)",
                     "from sys import path as p",
                     "import sys as _s",
                     "if True:\n    sys.path.insert(0, x)"):
            with self.subTest(code=code):
                self.assertEqual([code.count("\n") + 1], path_prepends(code))
        self.assertEqual([2], path_prepends("sys.path += [x]\nsys.path[0:0] = [x]"))
        for code in ('sys.path.append(str(ROOT / "tools"))',
                     "sys.path.extend([x])",
                     "sys.path += [x]",
                     "n = sys.path.index(x) + sys.path.count(x)",
                     "saved = sys.path.copy()",
                     "CHILD = 'sys.path.insert(0, \".\")'",
                     "paths.insert(0, x)",
                     "self.path.insert(0, x)",
                     "import sys",
                     "from sys import argv"):
            with self.subTest(code=code):
                self.assertEqual([], path_prepends(code))

    def test_the_socket_helpers_import_nothing_from_the_app(self):
        # workspace_ui_check imports tools/process_listeners.py after its own
        # --app path setup. The helpers import only the standard library and
        # leave the import path alone, so they cannot load a checkout's
        # workspace before the package's.
        code = ("import sys; sys.path.append(sys.argv[1]); before = list(sys.path); "
                "import process_listeners; print(sys.path == before, "
                "any(m.split('.')[0] in ('workspace', 'asr_engines', 'offline_guard') "
                "for m in sys.modules))")
        r = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT / "tools")],
                           capture_output=True, text=True, cwd=self.root)
        self.assertEqual(0, r.returncode, r.stderr[-500:])
        self.assertEqual(r.stdout.split(), ["True", "False"])

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
        never = mock.Mock(side_effect=AssertionError("a real run started"))
        with mock.patch.object(sys, "argv", argv), mock.patch("sys.stderr"), \
                mock.patch.object(perf_check, "parent", never), \
                mock.patch.object(perf_check, "child", never), self.assertRaises(SystemExit):
            perf_check.main()
        never.assert_not_called()


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

    def test_with_zip_a_failing_check_or_a_changed_folder_exits_1(self):
        import packaged_crash_check as crash
        for tool, good, bad in ((self.tool, self.GOOD, self.changed(None, self_test="FAIL")),
                                (crash, {"pass": True, "failed": []},
                                 {"pass": False, "failed": ["x"]})):
            with self.subTest(tool=tool.__name__):
                zpath, folder = make_package(self.root / tool.__name__)
                code, saved, _ = self.run_main(tool, good, "--zip", str(zpath), folder=folder)
                self.assertEqual((0, True), (code, saved["evidence"]["pass"]))
                code, saved, _ = self.run_main(tool, bad, "--zip", str(zpath), folder=folder)
                self.assertEqual((1, False, False), (code, saved["evidence"]["check_passed"],
                                                     saved["evidence"]["pass"]))

                def plant(version_dir, cache, wav, report, *rest, filled=good):
                    report.update(json.loads(json.dumps(filled)))
                    (Path(version_dir) / "app" / "planted.py").write_bytes(b"")
                out = self.root / "planted.json"
                argv = ["tool.py", str(folder), "cache", "x.wav", str(out), "--zip", str(zpath)]
                with mock.patch.object(tool, "check", plant), clean(), \
                        mock.patch.object(sys, "argv", argv), mock.patch("builtins.print"):
                    self.assertEqual(1, tool.main())
                ev = json.loads(out.read_text(encoding="utf-8"))["evidence"]
                self.assertEqual((True, True, False, False),
                                 (ev["check_passed"], ev["folder_matched_before"],
                                  ev["folder_matches_package"], ev["pass"]))

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
