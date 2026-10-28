# tests/test_evidence.py
# Release evidence names its package (#111 B3; tools/evidence.py). Each
# release-time check records the zip's SHA-256, the checkout it ran from, and
# whether it ran on exactly that zip's files, and a release is refused without
# one passing report of every kind naming the zip being published.

import argparse
import contextlib
import hashlib
import json
import os
import re
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
import windows_powershell  # noqa: E402

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


TICK_TEST_MUTEX = "Local\\UnminuteTickTest"


@contextlib.contextmanager
def one_run_at_a_time(name, timeout_s=120.0):
    """Hold a named mutex for this Windows session, so concurrent suites on one
    machine take turns at something they share (here the stand-in form's fixed
    place on screen). An owner that died without releasing it hands it on
    (WAIT_ABANDONED); TimeoutError if another run keeps it for timeout_s."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        state = kernel32.WaitForSingleObject(handle, int(timeout_s * 1000))
        if state not in (0, 0x80):                     # WAIT_OBJECT_0, WAIT_ABANDONED
            raise TimeoutError(f"another run held {name} for {timeout_s} s")
        try:
            yield
        finally:
            kernel32.ReleaseMutex(handle)
    finally:
        kernel32.CloseHandle(handle)


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
    if kind == "accessibility":
        body["colour_mode"] = "both"
        body["views"] = both_modes()
    if kind == "perf":
        ev["run"] = dict(evidence.PERF_RELEASE, quick=False)
        body["machine"] = dict(MACHINE)
        body["each_run"] = json.loads(json.dumps(EACH_RUN))
        body["store"] = dict(STORE)
    ev.update(change)
    return body


# Every view tools/accessibility_check.py audits; its report names each one
# for its mode when it audits each view in light and then in dark (view_key).
A11Y_VIEWS = evidence.ACCESSIBILITY_VIEWS


def both_modes(views=A11Y_VIEWS):
    """The views of an accessibility report run with --colour-mode both."""
    return {f"{name} ({mode})": {"colour_mode": mode, "unnamed": []}
            for name in views for mode in ("light", "dark")}


# A synthetic machine, below the 8-core, 16 GB design aim on purpose: there is
# no named reference machine, so any specification recorded in full will do.
MACHINE = {"cpu": "Synthetic CPU 4000", "cores": 4, "logical_processors": 4,
           "memory_gb": 7.8, "windows": "Windows 11 24H2", "os_build": "10.0.26100.4061",
           "power_plan": "Balanced", "on_mains_power": True, "battery": True,
           "gpus": [{"name": "Synthetic GPU", "driver": "31.0.1"}], "display_refresh_hz": 60,
           "webview2": "140.0.1.2"}


def drawn():
    """A visible state's figures in one run, shown drawn (#292)."""
    return {"cpu_percent_of_one_core": 1.0, "drawing": {"drawn": True, "problems": []}}


# The sealed store's figures, as perf_check's store step records them (#111 C7).
STORE = {name: 0.01 for name in evidence.PERF_STORE_FIGURES}

# Every window measured in a visible state, drawn, as perf_check records it.
EACH_RUN = [{state: drawn() for state in evidence.PERF_VISIBLE_STATES}
            for _ in range(3)]


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

    STAMP_KEYS = {"kind", "package_sha256", "package", "version", "commit", "release",
                  "folder_matches_package", "folder_differences", "folder_differences_listed",
                  "tools_commit", "tools_clean", "folder_matched_before", "check_passed",
                  "pass"}

    def test_the_stamp_holds_no_path(self):
        # The stamp is added after a check's own name guard (review of
        # 78f17ee, #419): its fields are pinned, and none holds the zip's, the
        # folder's or any other absolute path, only names inside the package.
        zpath, folder = make_package(self.root)
        before = evidence.identity(zpath, folder)
        (folder / "app/extra.py").write_bytes(b"")
        (folder / "runtime/python.exe").write_bytes(b"changed")
        ev = evidence.stamp({}, "perf", zpath, folder, True, before, run={"runs": 3})
        self.assertEqual(self.STAMP_KEYS | {"run"}, set(ev))
        self.assertEqual(self.STAMP_KEYS, set(evidence.stamp({}, "ui", zpath, folder, True)))
        self.assertEqual(["added: app/extra.py", "changed: runtime/python.exe"],
                         ev["folder_differences_listed"])
        texts = [v for v in ev.values() if isinstance(v, str)]
        texts += ev["folder_differences_listed"]
        for text in texts:
            with self.subTest(text=text):
                for path in (self.root, folder, zpath, Path.home()):
                    self.assertNotIn(str(path).lower(), text.lower())
                    self.assertNotIn(path.as_posix().lower(), text.lower())
                self.assertNotRegex(text, r"(^|[\s:])([A-Za-z]:[\\/]|[\\/]{2}|/(?!/))")

    def test_a_stamped_report_that_would_carry_the_user_name_fails_without_it(self):
        # Synthetic user. A file named after the user, left in the version
        # folder, is listed by name in the stamp; a report body the check did
        # not guard carries it too. Either way the stamped report is the
        # guard's failing report, and the evidence fails (#419).
        import residue
        env = {"USERNAME": "Pat.Example", "USERPROFILE": "C:\\Users\\Pat.Example"}
        zpath, folder = make_package(self.root)
        before = evidence.identity(zpath, folder)
        out = self.root / "ui.json"
        for label, body, left in (("body", {"note": "run by Pat.Example"}, None),
                                  ("stamp", {"api_surface": ["status"]},
                                   "app/torchinductor_Pat.Example.py")):
            with self.subTest(where=label):
                if left:
                    (folder / left).write_bytes(b"")
                out.write_text(json.dumps(body), encoding="utf-8")
                with mock.patch("builtins.print") as printed:
                    ev = evidence.stamp_file(out, "ui", zpath, folder, True, before,
                                             environ=env)
                self.assertFalse(ev["pass"])
                text = out.read_text(encoding="utf-8")
                self.assertNotIn("pat.example", text.lower())
                saved = json.loads(text)
                self.assertEqual([residue.CARRIES_USER_NAME], saved["failed"])
                self.assertEqual({"kind": "ui", "check_passed": False, "pass": False},
                                 saved["evidence"])
                self.assertIn(residue.CARRIES_USER_NAME, printed.call_args.args[0])
                self.assertTrue(any(p.endswith("ui did not pass")
                                    for p in evidence.check(ev["package_sha256"], [out])))
        # The same report without the name is stamped as it was.
        (folder / "app/torchinductor_Pat.Example.py").unlink()
        out.write_text(json.dumps({"note": "run by someone"}), encoding="utf-8")
        with mock.patch("builtins.print"):
            ev = evidence.stamp_file(out, "ui", zpath, folder, True, before, environ=env)
        saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((True, "run by someone", ev), (ev["pass"], saved["note"],
                                                        saved["evidence"]))

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


class StoreEvidenceTests(TempDir):
    """#111 C7: a release's performance evidence measures the sealed store
    (the C2b-2 design's section 4.8 figures), each with a budget (#430)."""

    write, complete = CheckTests.write, CheckTests.complete

    def test_a_report_without_the_store_step_fails(self):
        body = report("perf")
        del body["store"]
        problems = evidence.check(SHA, self.complete(perf=body))
        self.assertEqual(1, len(problems), problems)
        self.assertIn("does not measure the sealed store", problems[0])

    def test_every_store_figure_is_needed(self):
        for name in evidence.PERF_STORE_FIGURES:
            for bad in (None, "0.5", True):
                with self.subTest(figure=name, value=bad):
                    body = report("perf")
                    body["store"][name] = bad
                    problems = evidence.check(SHA, self.complete(perf=body))
                    self.assertEqual(1, len(problems), problems)
                    self.assertIn(name, problems[0])

    def test_every_figure_has_a_budget(self):
        # Set from the first Phase C release run's medians plus 20%
        # (v0.5.0-preview.4; owner decision, 21 Oct 2026; #430), so a release
        # run fails when one is over (tests/test_perf_check.py).
        budgets = json.loads((ROOT / "docs" / "perf-budgets.json").read_text(encoding="utf-8"))
        self.assertEqual(set(evidence.PERF_STORE_FIGURES), set(budgets["budgets"]["store"]))
        self.assertIn("workspace_s", budgets["budgets"]["startup"])
        self.assertIn("store", budgets["_budget_notes"])


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
                 "display_refresh_hz": [None, 0, 1, "60", True, 59.9],
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
            "power, GPU: Synthetic GPU (driver 31.0.1), display at 60 Hz, WebView2 140.0.1.2.",
            out.call_args.args[0])
        line = evidence.machine_line(dict(MACHINE, gpus=[], battery=False))
        self.assertIn("on mains power (no battery), GPU: none,", line)
        self.assertIn("on battery,", evidence.machine_line(dict(MACHINE,
                                                                on_mains_power=False)))
        line = evidence.machine_line(dict(MACHINE, gpus=[{"name": "G", "driver": None}]))
        self.assertIn("GPU: G (driver not reported)", line)

    def test_every_visible_window_must_show_it_was_drawn(self):
        # #292: a window measured while the display was off or the page not
        # drawing is not evidence, whatever its figures say.
        body = report("perf")
        body["each_run"][1]["recording"]["drawing"] = {"drawn": False,
                                                       "problems": ["the display was off"]}
        del body["each_run"][2]["settings"]["drawing"]
        problems = evidence.check(SHA, self.complete(perf=body))
        self.assertEqual(["perf.json: perf does not show every visible window drawn on a "
                          "display that was on (not shown: recording in run 2, settings in "
                          "run 3)"], problems)
        for runs in (None, [], "x"):
            with self.subTest(runs=runs):
                body = report("perf")
                body["each_run"] = runs
                problems = evidence.check(SHA, self.complete(perf=body))
                self.assertEqual(["perf.json: perf does not show every visible window drawn "
                                  "on a display that was on (not shown: no runs recorded)"],
                                 problems)

    def test_a_drawn_flag_must_be_true_itself(self):
        for value in (None, 1, "true", {"drawn": True}):
            with self.subTest(value=value):
                body = report("perf")
                body["each_run"][0]["meetings"]["drawing"] = {"drawn": value}
                self.assertEqual(["meetings in run 1"], evidence.perf_not_drawn(body))
        body = report("perf")
        body["each_run"][0]["meetings"] = "figures"
        body["each_run"][1] = None
        self.assertEqual(["meetings in run 1", "run 2"], evidence.perf_not_drawn(body))

    def test_recording_off_by_policy_has_no_window_to_show(self):
        body = report("perf")
        for run in body["each_run"]:
            del run["recording"]
        body["not_measured_by_policy"] = ["recording", "recording-minimised"]
        self.assertEqual([], evidence.perf_not_drawn(body))
        self.assertEqual([], evidence.check(SHA, self.complete(perf=body)))

    def test_live_drafts_have_a_visible_window_to_show(self):
        # #104 slice 4: recording with live drafts on is a visible state too.
        self.assertIn("recording-drafts", evidence.PERF_VISIBLE_STATES)
        body = report("perf")
        del body["each_run"][1]["recording-drafts"]
        self.assertEqual(["recording-drafts in run 2"], evidence.perf_not_drawn(body))
        body["each_run"][1]["recording-drafts"] = {"drawing": {"drawn": False}}
        self.assertEqual(["recording-drafts in run 2"], evidence.perf_not_drawn(body))
        # Live drafts off by policy: that window is excused, no other.
        body = report("perf")
        for run in body["each_run"]:
            del run["recording-drafts"]
        body["not_measured_by_policy"] = ["recording-drafts", "recording-drafts-minimised"]
        self.assertEqual([], evidence.perf_not_drawn(body))

    def test_a_missing_visible_state_counts_only_if_policy_names_it(self):
        # #292 review: a visible state missing from a run is excused only when
        # the report names it as not measured by policy.
        for policy in (None, [], ["recording-minimised"], "recording"):
            with self.subTest(policy=policy):
                body = report("perf")
                del body["each_run"][0]["recording"]
                del body["each_run"][2]["meetings"]
                if policy is not None:
                    body["not_measured_by_policy"] = policy
                self.assertEqual(["recording in run 1", "meetings in run 3"],
                                 evidence.perf_not_drawn(body))
        # Policy names recording only: a missing meetings window is still refused.
        body = report("perf")
        del body["each_run"][0]["recording"]
        del body["each_run"][1]["meetings"]
        body["not_measured_by_policy"] = ["recording", "recording-minimised"]
        self.assertEqual(["meetings in run 2"], evidence.perf_not_drawn(body))

    def test_the_machine_command_refuses_an_incomplete_or_unreadable_report(self):
        for name, body in (("old.json", perf_with()), ("part.json", perf_with(drop=("gpus",))),
                           ("list.json", [1]), ("bad.json", "{")):
            path = self.write(name, body)
            with self.subTest(name), mock.patch("builtins.print") as out:
                self.assertEqual(1, evidence.main(["machine", str(path)]))
                self.assertTrue(out.call_args.args[0].startswith("MACHINE PROBLEM: "))
        with mock.patch("builtins.print"):
            self.assertEqual(1, evidence.main(["machine", str(self.root / "absent.json")]))


class AccessibilityEvidenceTests(TempDir):
    """Owner decision, 22 Oct 2026 (#111, #298): release evidence runs the
    accessibility check in both light and dark mode. A report audited in one
    mode, or with a view missing from either, is not evidence."""

    write, complete = CheckTests.write, CheckTests.complete

    def problems_with(self, body):
        return evidence.check(SHA, self.complete(accessibility=body))

    def refused(self, body, shown):
        self.assertEqual([f"accessibility.json: accessibility was not audited in both light "
                          f"and dark (owner decision, 22 Oct 2026: run "
                          f"tools/accessibility_check.py with --colour-mode both; not shown: "
                          f"{shown})"], self.problems_with(body))

    def test_both_modes_pass(self):
        self.assertEqual([], self.problems_with(report("accessibility")))
        self.assertEqual([], evidence.accessibility_not_both(report("accessibility")))

    def test_a_run_in_one_mode_is_refused(self):
        for asked, modes in (("light", ["light"]), ("dark", ["dark"]),
                             ("system", ["light"]), ("system", ["dark"])):
            with self.subTest(asked=asked, modes=modes):
                body = report("accessibility")
                body["colour_mode"] = asked
                # As the tool names them in one mode: the view's own name.
                body["views"] = {name: {"colour_mode": mode, "unnamed": []}
                                 for name in A11Y_VIEWS for mode in modes}
                problems = self.problems_with(body)
                self.assertEqual(1, len(problems), problems)
                self.assertIn("was not audited in both light and dark", problems[0])
                self.assertIn(f"the run asked for {asked!r}, not 'both'", problems[0])

    def test_a_report_made_before_modes_were_recorded_is_refused(self):
        body = report("accessibility")
        del body["colour_mode"]
        body["views"] = {name: {"unnamed": []} for name in A11Y_VIEWS}
        listed = (["the run asked for None, not 'both'"]
                  + [f"{name} (no colour mode)" for name in A11Y_VIEWS]
                  + [f"{name} in {mode}" for name in A11Y_VIEWS for mode in ("light", "dark")])
        more = len(listed) - evidence.MAX_LISTED
        self.assertGreater(more, 0)     # more than are listed, so the rest are counted
        self.refused(body, ", ".join(listed[:evidence.MAX_LISTED]) + f", and {more} more")

    def test_each_view_needs_both_modes(self):
        body = report("accessibility")
        del body["views"]["meetings (dark)"]
        del body["views"]["handoff page (light)"]
        self.refused(body, "meetings in dark, handoff page in light")

    def missing(self, names):
        """The refusal of a report that leaves out whole views."""
        return (f"accessibility.json: accessibility does not cover {len(names)} of the "
                f"{len(A11Y_VIEWS)} views tools/accessibility_check.py audits, in either mode "
                f"(a run that stopped early, or a report edited down: run the check again "
                f"with --colour-mode both; missing: {', '.join(names)})")

    def test_every_view_the_tool_audits_is_required(self):
        # A report with a view left out in both modes (a run that stopped
        # early, or one edited down) is refused, not passed on what it has,
        # and the refusal says views are missing, not that a mode is.
        body = report("accessibility")
        del body["views"]["meetings (light)"]
        del body["views"]["meetings (dark)"]
        self.assertEqual([self.missing(["meetings"])], self.problems_with(body))
        self.assertEqual(["meetings"], evidence.accessibility_views_missing(body))
        for name in A11Y_VIEWS:
            with self.subTest(left_out=name):
                body = report("accessibility")
                body["views"] = both_modes([n for n in A11Y_VIEWS if n != name])
                self.assertEqual([self.missing([name])], self.problems_with(body))
        body = report("accessibility")
        body["views"] = both_modes(["setup", "meetings"])
        self.assertEqual([self.missing([n for n in A11Y_VIEWS if n not in ("setup", "meetings")])],
                         self.problems_with(body))
        # A view the tool does not define still needs both modes.
        body = report("accessibility")
        body["views"]["extra view (light)"] = {"colour_mode": "light", "unnamed": []}
        self.refused(body, "extra view in dark")

    def test_missing_views_and_a_missing_mode_are_told_apart(self):
        # A view left out entirely is listed once, as missing; a view audited
        # in one mode only is still a colour-mode gap, in its own refusal.
        body = report("accessibility")
        for key in ("meetings (light)", "meetings (dark)", "help (dark)"):
            del body["views"][key]
        self.assertEqual([self.missing(["meetings"]),
                          "accessibility.json: accessibility was not audited in both light and "
                          "dark (owner decision, 22 Oct 2026: run tools/accessibility_check.py "
                          "with --colour-mode both; not shown: help in dark)"],
                         self.problems_with(body))
        # A view recorded under the wrong mode was audited: a mode problem.
        body = report("accessibility")
        body["views"]["meetings (dark)"]["colour_mode"] = "light"
        self.assertEqual([], evidence.accessibility_views_missing(body))
        # No views at all stays "no views recorded", not seventeen missing.
        for views in (None, {}):
            with self.subTest(views=views):
                body = report("accessibility")
                body["views"] = views
                self.assertEqual([], evidence.accessibility_views_missing(body))

    def test_a_view_must_show_the_mode_it_is_named_for(self):
        body = report("accessibility")
        body["views"]["meetings (dark)"]["colour_mode"] = "light"
        self.refused(body, "meetings (dark) (shows light), meetings in dark")
        for value in (None, "both", "system", 1):
            with self.subTest(value=value):
                body = report("accessibility")
                body["views"]["meetings (dark)"]["colour_mode"] = value
                self.refused(body, "meetings (dark) (no colour mode), meetings in dark")

    def test_no_views_or_malformed_views_fail_without_a_crash(self):
        for views in (None, {}, [], "meetings"):
            with self.subTest(views=views):
                body = report("accessibility")
                body["views"] = views
                self.refused(body, "no views recorded")
        body = report("accessibility")
        body["views"]["meetings (dark)"] = "audit"
        self.refused(body, "meetings (dark) (no colour mode), meetings in dark")

    def test_other_kinds_need_no_colour_mode(self):
        self.assertNotIn("colour_mode", report("ui"))
        self.assertEqual([], evidence.check(SHA, self.complete()))

    def test_the_modes_are_the_accessibility_checks_both(self):
        # tools/evidence.py reads the report tools/accessibility_check.py
        # writes: the same two modes, and each view named for its mode.
        import accessibility_check
        modes = accessibility_check.colour_modes("both")
        self.assertEqual(list(evidence.ACCESSIBILITY_MODES), modes)
        self.assertEqual("meetings (dark)", accessibility_check.view_key("meetings", "dark",
                                                                          modes))
        # Every view the tool audits, and only those, is required.
        self.assertEqual(evidence.ACCESSIBILITY_VIEWS, tuple(accessibility_check.VIEWS))


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
        # The real flags with the two the check reads replaced: a codec imported for the
        # first time while this is in place (zipfile's cp437) reads others (#389).
        real = {n: getattr(sys.flags, n) for n in dir(sys.flags)
                if not n.startswith("_") and not callable(getattr(sys.flags, n))}
        f = types.SimpleNamespace(**{**real, "isolated": int(flags),
                                     "dont_write_bytecode": int(flags)})
        # A package's file is its __init__.py; a submodule's is its own .py.
        modules = {n: types.SimpleNamespace(__file__=str(
                       folder / "app" / n / "__init__.py" if "." not in n
                       else folder / "app" / (n.replace(".", "/") + ".py")))
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
        # The realistic mix-up is the workspace package itself coming from a
        # checkout (its submodules then follow it); a stray submodule is checked too.
        _, folder = make_package(self.root)
        ap, args = self.parse("--app", str(folder / "app"))
        checkout = self.root / "checkout" / "workspace"
        for name, path in (("workspace", checkout / "__init__.py"),
                           ("workspace.asr_engines", checkout / "asr_engines.py")):
            with self.subTest(name):
                patches = self.package_python(folder)
                patches.append(mock.patch.dict(sys.modules, {
                    name: types.SimpleNamespace(__file__=str(path))}))
                with self.assertRaisesRegex(SystemExit, f"^{re.escape(name)} was imported from"):
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
        other = subprocess.Popen([windows_powershell.executable(), "-NoProfile",
                                  "-NonInteractive", "-Command", script],
                                 stdout=subprocess.PIPE, text=True,
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
        # lost; Space and BM_CLICK posted to the check box tick it. A form that
        # is never activated, off screen under a top-most cover, stands in for
        # the confirmation. It is in another process, so find_dialog is
        # pointed at it. Every run uses the same place on screen, so concurrent
        # suites on one machine take turns (a second run's cover or form would
        # cover this one's).
        with one_run_at_a_time(TICK_TEST_MUTEX):
            self.tick_behind_another_window()

    def tick_behind_another_window(self):
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
            "function Say($line) { [Console]::Out.WriteLine($line); [Console]::Out.Flush() }; "
            "$f = New-Object QuietForm; $f.Text = $env:UNMINUTE_TITLE; $f.KeyPreview = $true; "
            "$f.ShowInTaskbar = $false; $f.StartPosition = 'Manual'; "
            "$f.Location = New-Object Drawing.Point(-32000, -32000); "
            "$cancel = New-Object Windows.Forms.Button; $cancel.Text = 'Cancel'; "
            "$check = New-Object Windows.Forms.CheckBox; $check.Text = $env:UNMINUTE_CHECK; "
            "$check.AutoSize = $true; $check.Top = 40; "
            "$check.add_CheckedChanged({ Say ('checked ' + $check.Checked) }); "
            "$check.add_MouseDown({ Say 'mouse' }); $f.add_KeyDown({ Say 'key' }); "
            "$f.Controls.Add($cancel); $f.Controls.Add($check); $f.Show(); $cancel.Select(); "
            "$cover = New-Object QuietForm; $cover.Text = $env:UNMINUTE_TITLE + ' cover'; "
            "$cover.ShowInTaskbar = $false; $cover.FormBorderStyle = 'None'; "
            "$cover.TopMost = $true; $cover.StartPosition = 'Manual'; $cover.Bounds = $f.Bounds; "
            "$cover.Show(); Say 'shown'; "
            "for ($i = 0; $i -lt 300 -and $f.Visible; $i++) { "
            "[Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 50 }; "
            "Say 'done'")
        form = subprocess.Popen([windows_powershell.executable(), "-NoProfile",
                                 "-NonInteractive", "-Command", script],
                                stdout=subprocess.PIPE, text=True,
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
                # Covered: a posted click arrives (MouseDown) but does not tick.
                self.assertTrue(workspace_ui_check.press(["click:" + statement], title))
                # Space posted to the check box ticks it; it needs no focus
                # (Cancel has it) and arrives as a key press (KeyDown).
                self.assertTrue(workspace_ui_check.press(["space-on:" + statement], title))
                # BM_CLICK unticks it with no key or mouse event at all, so in
                # the real check only CheckedChanged can restart the wait.
                self.assertTrue(workspace_ui_check.press(["toggle-on:" + statement], title))
                # Shift posted to the focused Cancel reaches the form as a key
                # press (KeyPreview) and ticks nothing ("keys wait again").
                self.assertTrue(workspace_ui_check.press(["shift-on:Cancel"], title))
                # Uncovered, the same click acts (it ticks), so the click
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
            self.assertEqual(["mouse", "key", "checked True", "checked False", "key",
                              "mouse", "checked True", "done"], lines)
        finally:
            form.kill()
            form.wait(10)
            form.stdout.close()

    @unittest.skipUnless(os.name == "nt", "Windows windows")
    def test_the_ui_check_posts_esc_and_an_unfocused_invoke(self):
        # The steps behind the Esc and Invoke scripts (#232), on an off-screen
        # stand-in form that is never activated, as the confirmation usually
        # is not: "invoke-on" (BM_CLICK) does nothing on a disabled button and
        # raises Click once it is enabled, without moving the focus there;
        # "until-enabled" waits for the enabling and fails the script if it does
        # not come in time; "esc-on" reaches the form's CancelButton. The
        # stand-in sits where the tick test's does, so they take turns.
        with one_run_at_a_time(TICK_TEST_MUTEX):
            self.esc_and_invoke_on_a_stand_in()

    def esc_and_invoke_on_a_stand_in(self):
        import ctypes
        import io

        import workspace_ui_check
        title = f"Unminute invoke test {os.getpid()}"
        statement = "I have read this synthetic statement."
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -WarningAction SilentlyContinue -ReferencedAssemblies System.Windows.Forms "
            "-TypeDefinition 'public class QuietForm : System.Windows.Forms.Form { protected "
            "override bool ShowWithoutActivation { get { return true; } } }'; "
            "function Say($line) { [Console]::Out.WriteLine($line); [Console]::Out.Flush() }; "
            "$f = New-Object QuietForm; $f.Text = $env:UNMINUTE_TITLE; "
            "$f.ShowInTaskbar = $false; $f.StartPosition = 'Manual'; "
            "$f.Location = New-Object Drawing.Point(-32000, -32000); "
            "$cancel = New-Object Windows.Forms.Button; $cancel.Text = 'Cancel'; "
            "$cancel.add_Click({ Say 'cancel' }); "
            "$ok = New-Object Windows.Forms.Button; $ok.Text = 'OK'; $ok.Left = 100; "
            "$ok.Enabled = $false; $ok.add_Click({ Say ('invoke focused ' + $ok.Focused) }); "
            "$check = New-Object Windows.Forms.CheckBox; $check.Text = $env:UNMINUTE_CHECK; "
            "$check.AutoSize = $true; $check.Top = 40; "
            "$check.add_CheckedChanged({ Say 'checked' }); "
            "$f.CancelButton = $cancel; "
            "$f.Controls.Add($cancel); $f.Controls.Add($ok); $f.Controls.Add($check); "
            "$timer = New-Object Windows.Forms.Timer; $timer.Interval = 2000; "
            "$timer.add_Tick({ $timer.Stop(); $ok.Enabled = $true; Say 'enabled' }); "
            "$f.Show(); $cancel.Select(); $timer.Start(); Say 'shown'; "
            "for ($i = 0; $i -lt 300 -and $f.Visible; $i++) { "
            "[Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 50 }; "
            "Say 'done'")
        form = subprocess.Popen([windows_powershell.executable(), "-NoProfile",
                                 "-NonInteractive", "-Command", script],
                                stdout=subprocess.PIPE, text=True,
                                env=dict(os.environ, UNMINUTE_TITLE=title,
                                         UNMINUTE_CHECK=statement))
        try:
            self.assertEqual("shown", form.stdout.readline().strip())
            user32 = ctypes.WinDLL("user32")
            user32.FindWindowW.restype = ctypes.c_void_p
            hwnd = user32.FindWindowW(None, title)
            self.assertTrue(hwnd)
            with mock.patch.object(workspace_ui_check, "find_dialog", return_value=hwnd):
                # Disabled: BM_CLICK is ignored, and a short wait for the
                # enabling fails the script rather than going on.
                self.assertTrue(workspace_ui_check.press(["invoke-on:OK"], title))
                with mock.patch.object(workspace_ui_check, "ENABLE_WAIT_S", 0.3), \
                        contextlib.redirect_stderr(io.StringIO()) as said:
                    self.assertFalse(workspace_ui_check.press(["until-enabled:OK"], title))
                self.assertIn("was not enabled", said.getvalue())
                # Once enabled, BM_CLICK raises Click with the focus elsewhere.
                self.assertTrue(workspace_ui_check.press(["until-enabled:OK", "invoke-on:OK"],
                                                         title))
                # Esc posted to the statement clicks CancelButton, and ticks nothing.
                self.assertTrue(workspace_ui_check.press(["esc-on:" + statement], title))
            user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)          # WM_CLOSE
            lines = [line.strip() for line in form.stdout]
            self.assertEqual(["enabled", "invoke focused False", "cancel", "done"], lines)
        finally:
            form.kill()
            form.wait(10)
            form.stdout.close()

    @unittest.skipUnless(os.name == "nt", "Windows mutex")
    def test_runs_take_turns_at_the_tick_test(self):
        # Another process holding the mutex keeps this run waiting; once it lets
        # go, this run goes ahead. A name of its own, so a real tick test
        # running at the same time is not held up.
        name = f"Local\\UnminuteTurnTest{os.getpid()}"
        holder = subprocess.Popen([sys.executable, "-I", "-c", (
            "import ctypes, sys\n"
            "k = ctypes.WinDLL('kernel32')\n"
            "k.CreateMutexW.restype = ctypes.c_void_p\n"
            "h = ctypes.c_void_p(k.CreateMutexW(None, True, sys.argv[1]))\n"
            "print('held', flush=True)\n"
            "sys.stdin.read()\n"
            "k.ReleaseMutex(h)\n"), name], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True)
        try:
            self.assertEqual("held", holder.stdout.readline().strip())
            began = time.monotonic()
            with self.assertRaises(TimeoutError):
                with one_run_at_a_time(name, timeout_s=0.5):
                    self.fail("went ahead while another run held the mutex")
            self.assertGreaterEqual(time.monotonic() - began, 0.4)
            holder.stdin.close()
            self.assertEqual(0, holder.wait(10))
            ran = []
            with one_run_at_a_time(name, timeout_s=10):
                ran.append(True)
            self.assertEqual([True], ran)
        finally:
            holder.kill()
            holder.wait(10)
            holder.stdout.close()
        # The tick test itself takes its turn under the shared name.
        body = Path(__file__).read_text(encoding="utf-8").split(
            "def test_the_ui_check_ticks_a_statement_behind_another_window(", 1)[1].split(
            "\n    def ", 1)[0]
        self.assertIn("with one_run_at_a_time(TICK_TEST_MUTEX):", body)

    def test_the_ui_check_never_ticks_its_statements_by_click(self):
        # A posted click cannot tick a statement behind the user's foreground
        # window (the test above), so the native checks tick with BM_CLICK
        # ("tick first": no key or mouse input, so only the tick restarts the
        # wait) and with Tab and Space (keyboard).
        native = self.source("workspace_ui_check.py").split(
            "\ndef native_checks(", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn('"click:I have read', native)
        self.assertIn('"click:Confirm", "open?", "toggle-on:" + ticked["check"],\n'
                      '         "click:Confirm", "open?"', native)
        self.assertIn('["tab", "space-on:" + keyed["check"]', native)

    def test_the_ui_check_guards_each_confirmation_rule(self):
        # Each scripted rule of workspace/shell.py's confirmation has a native
        # script that only that rule can pass (round 2 review of #229; Esc and
        # an Invoke without the focus, #232). docs/DEVELOPER_NOTES.md lists the
        # rules this check does not script.
        source = self.source("workspace_ui_check.py")
        native = source.split("\ndef native_checks(", 1)[1].split("\ndef ", 1)[0]
        # The quick Enter comes long after the action was enabled, so only the
        # focus hold (not "enabled for the wait") ignores it.
        self.assertIn('"space-on:" + keyed["check"], "wait:2.5", "tab", "tab",\n'
                      '         "enter-on:Confirm", "open?"', native)
        # A key press before the action is available restarts the wait: keys at
        # 0.2, 0.6 and 1.0 s, the action still ignored at 1.5 s.
        self.assertIn('["wait:0.2", "shift-on:Cancel", "wait:0.35", "shift-on:Cancel", '
                      '"wait:0.35",\n         "shift-on:Cancel", "wait:0.45", "click:Confirm", '
                      '"open?"', native)
        # So does a mouse press on the dialog (its text).
        self.assertIn('["wait:0.2", "click:" + plain["body"], "wait:0.2", "click:" + '
                      'plain["body"],\n         "wait:0.2", "click:" + plain["body"], '
                      '"wait:0.3", "click:Confirm", "open?"', native)
        # Enter on the ticked statement, long after the action was enabled,
        # means Cancel (AcceptButton).
        self.assertIn('["tab", "space-on:" + on_statement["check"], "wait:2.5",\n'
                      '         "enter-on:" + on_statement["check"]]', native)
        # Esc on the ticked statement, long after the action was enabled, means
        # Cancel (CancelButton), and closes the window itself (#232).
        self.assertIn('["tab", "space-on:" + escaped["check"], "wait:2.5",\n'
                      '         "esc-on:" + escaped["check"], "wait:0.5", "open?"]', native)
        # An Invoke without the focus, about 0.3 s after the action is enabled,
        # is ignored; about 1.8 s after, it answers (act_clicked; mutant M13 of #229).
        self.assertIn('["until-enabled:Confirm", "wait:0.3", "invoke-on:Confirm", "open?", '
                      '"wait:1.3",\n         "invoke-on:Confirm"]', native)
        # The verdict is native_problems (unit tests in tests/test_accessors.py),
        # and main fails on what it finds.
        verdict = source.split("\ndef native_problems(", 1)[1].split("\ndef ", 1)[0]
        main = source.split('nv = report.get("native")', 1)[1].split("recording indicator", 1)[0]
        self.assertIn("bad.extend(native_problems(nv))", main)
        for rule in ('nv.get("keys_restart_wait") is True', 'nv.get("open_after_keys") == [True]',
                     'nv.get("clicks_restart_wait") is True',
                     'nv.get("open_after_clicks") == [True]',
                     'nv.get("enter_on_statement") is False', 'nv.get("pressed_6")',
                     'nv.get("pressed_7")', 'nv.get("pressed_8")',
                     'nv.get("esc_on_statement") is False',
                     'nv.get("open_after_esc") == [False]', 'nv.get("pressed_9")',
                     'nv.get("invoke_after_enabled_wait") is True',
                     'nv.get("open_after_quick_invoke") == [True]', 'nv.get("pressed_10")'):
            with self.subTest(rule=rule):
                self.assertIn(rule, verdict)

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
        self.assertIn('    if nv.get("stuck_dialogs"):\n        bad.append(', source)

    def test_package_checks_start_the_app_as_the_shortcut_does(self):
        flags = '"-I", "-B", "-X", "disable-remote-debug"'
        for tool in ("packaged_check.py", "packaged_crash_check.py", "perf_check.py"):
            with self.subTest(tool=tool):
                self.assertIn(flags, self.source(tool))
        # Child, inspect, the token step and the audio windows (#111 C7).
        self.assertEqual(4, self.source("packaged_crash_check.py").count('["py"], *FLAGS,'))

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
                "any(m.split('.')[0] == 'workspace' "
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
        # A release run also records public speech with live drafts on (#104).
        speech = self.root / "public-speech.wav"
        speech.write_bytes(b"synthetic stand-in")
        argv = ["perf_check.py", str(out), "--app", str(folder / "app"), "--zip", str(zpath),
                "--speech", str(speech), "--draft-models", str(self.root), *extra]
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

    def test_a_release_perf_run_without_public_speech_stops_before_measuring(self):
        # #104 slice 4: the drafts-on states have budgets, so a release run
        # without --speech and --draft-models would fail them after hours.
        import perf_check
        zpath, folder = make_package(self.root)
        never = mock.Mock(side_effect=AssertionError("a real run started"))
        argv = ["perf_check.py", str(self.root / "perf.json"), "--app", str(folder / "app"),
                "--zip", str(zpath), "--leak-minutes", "30"]
        with mock.patch.object(sys, "argv", argv), mock.patch("sys.stderr") as err, \
                mock.patch.object(perf_check, "parent", never), \
                mock.patch.object(perf_check.evidence, "prepare"), \
                self.assertRaises(SystemExit) as caught:
            perf_check.main()
        self.assertEqual(2, caught.exception.code)
        never.assert_not_called()
        said = "".join(str(c.args[0]) for c in err.write.call_args_list)
        self.assertIn("a release run also measures recording with live drafts on", said)

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
    # The figures of the B2b release build's run (#185), less the paths, with
    # what #111 C7 adds (synthetic values in the shape the tool writes).
    ERASED = {"audio_key": "missing", "text_key": "opens", "media_folder_gone": True}
    GOOD = {"self_test": "PASS",
            "lifecycle": {"meetings_imported": 1, "transcribed_s": 1075, "worker_peak_mb": 2463,
                          "speakers": "done", "passages": 372, "speaker_named": True,
                          "windows": 8, "audio_key": "opens", "blocked_network_attempts": 0},
            "control": {"patterns": 10, "missed": [], "after_cleanup_missed": []},
            "at_rest": {"windows": 8, "found": {}, "unreadable": [], "files_scanned": 9,
                        "audit_files": 1, "verify_audit": "ok"},
            "recording": {"consent": "asked and recorded", "recording_windows": 8,
                          "key_while_recording": "opens", "stopped": True, "imported": 1,
                          "key_after_import": "missing", "key_before_discard": "opens",
                          "discarded": True,
                          "key_after_discard": "missing", "recordings_waiting": 0,
                          "other_questions": [],
                          "during": {"seconds": 8.1, "windows": 8, "control_missed": [],
                                     "found": {}, "unreadable": [], "chunks_scanned": 1}},
            "cleanup": {"meetings": 2, "after_audio_cleanup": [ERASED, ERASED],
                        "meetings_left": 0,
                        "receipts": ["audio removed", "audio removed", "transcript removed",
                                     "transcript removed"],
                        "blocked_network_attempts": 0, "wal_bytes": 0, "blockers": [],
                        "freelist_pages": 0, "meetings_in_store": 0},
            "residual": {"files_after_cleanup": ["Unminute/workspace/settings.json",
                                                 "Unminute/workspace/workspace.db"],
                         "found": {}, "unreadable": [], "unexpected_files": [],
                         "logs_scanned": ["Unminute/workspace/logs/app.log"],
                         "temp_files_left": [],
                         "redirected_files_left": {"TEMP": [], "APPDATA": [],
                                                   "USERPROFILE": []},
                         "left_unexplained": [],
                         "outside_new": {"model_cache": ["whisper/.verified/small-ab.json"],
                                         "LocalLow": []},
                         "outside_unexplained": [], "outside_unreadable": [],
                         "content_backups": []},
            "dumps": {"dumps": 0, "crash_files": 0},
            "idle_window": {"window_running": True, "processes": 7,
                            "cpu_percent_of_one_core": 1.5, "working_set_mb": 484},
            # #418: the window with content, through the real start.
            "window": {"start_files_before": {"instance_lock": False, "launch_record": False},
                       "shown": {"window_found": True, "opened": True,
                                 "transcript_shown": True, "playing": False},
                       "processes": 6, "closed": {"closed": True}, "exit_code": 0,
                       "processes_ended": True,
                       "start_files": {"instance_lock": True, "launch_record": True},
                       "profile_files_after_close": 0, "event_log_entries": 9,
                       "profile_wipe": {"files_before": 0, "files_left": 0},
                       "models_lent": {"linked": 4, "copied": 9, "unreadable": []},
                       "models_returned": {"new": ["parakeet/v3/abc/.verified.json"],
                                           "unexplained": [], "removed": True,
                                           "unreadable": []}},
            "event_log": {"window_step": 9, "both_windows": 14,
                          "why": "written by the app's own starts"}}

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

    def test_only_finished_microphone_chunks_count_as_scanned(self):
        scanned = ["Unminute/workspace/recordings/r1/chunk-000001.uma",
                   "Unminute/workspace/recordings/r1/chunk-000002.uma",
                   "Unminute/workspace/recordings/r1/chunk-000003.uma.tmp",   # still filling
                   "Unminute/workspace/recordings/r1/sys-000001.uma",   # the computer's track
                   "Unminute/workspace/recordings/r1/journal.json",
                   "Unminute/workspace/media/abc/audio.uma",
                   "temp/x.bin"]
        self.assertEqual(2, self.tool.chunks_scanned(scanned))
        self.assertEqual(0, self.tool.chunks_scanned(scanned[2:]))

    def test_each_criterion_fails_on_its_own(self):
        during = dict(self.GOOD["recording"]["during"])
        erased = self.ERASED
        shown = dict(self.GOOD["window"]["shown"])
        files = dict(self.GOOD["window"]["start_files"])
        lent, returned = self.GOOD["window"]["models_lent"], self.GOOD["window"]["models_returned"]
        cases = {
            "no error": [self.changed(None, error="RuntimeError: x")],
            "self-test passed": [self.changed(None, self_test="FAIL")],
            "transcribed with speakers": [self.changed("lifecycle", speakers="failed")],
            "cleaned up, with both receipts": [
                self.changed("cleanup", receipts=["audio removed", "transcript removed"]),
                self.changed("cleanup", meetings_left=1)],
            "no network attempts by the app process": [
                self.changed("lifecycle", blocked_network_attempts=1),
                self.changed("cleanup", blocked_network_attempts=1)],
            # #111 C7: each fails the report on its own.
            "scanner control found every pattern": [
                self.changed("control", missed=["marker"]),
                self.changed("control", after_cleanup_missed=["window_3"]),
                self.changed("control", patterns=0)],
            "nothing readable at rest": [
                self.changed("at_rest", found={"marker": ["Unminute/workspace/workspace.db"]}),
                self.changed("at_rest", found={"window_2": ["Unminute/workspace/x"]}),
                self.changed("at_rest", unreadable=["Unminute/workspace/workspace.db"]),
                self.changed("at_rest", windows=0),
                self.changed("lifecycle", speaker_named=False),
                self.changed("lifecycle", audio_key="missing")],
            "audit log verified, no content in it": [
                self.changed("at_rest", verify_audit="broken"),
                self.changed("at_rest", audit_files=0)],
            "recording encrypted while it records": [
                self.changed("recording", during=dict(during, found={"window_1": ["x"]})),
                self.changed("recording", during=dict(during, control_missed=["window_1"])),
                # Nothing found means something only if a chunk was scanned.
                self.changed("recording", during=dict(during, chunks_scanned=0)),
                self.changed("recording", during=None)],
            "recording confirmed, then its key erased": [
                self.changed("recording", key_after_import="opens"),
                self.changed("recording", key_after_discard="opens"),
                # "missing" means something only if the same id opened first.
                self.changed("recording", key_before_discard="missing"),
                self.changed("recording", key_before_discard=None),
                self.changed("recording", key_while_recording="missing"),
                self.changed("recording", recordings_waiting=1),
                self.changed("recording", consent=None),
                self.changed("recording", other_questions=["Delete this meeting?"]),
                self.changed("recording", error="the recording did not start")],
            "audio key erased, text key kept": [
                self.changed("cleanup", after_audio_cleanup=[erased,
                                                             dict(erased, audio_key="opens")]),
                self.changed("cleanup", after_audio_cleanup=[erased,
                                                             dict(erased, text_key="missing")]),
                self.changed("cleanup", after_audio_cleanup=[
                    erased, dict(erased, media_folder_gone=False)]),
                self.changed("cleanup", after_audio_cleanup=[erased])],
            "nothing to block an update, database compacted": [
                self.changed("cleanup", blockers=["media"]),
                self.changed("cleanup", freelist_pages=3),
                self.changed("cleanup", wal_bytes=4096),
                self.changed("cleanup", meetings_in_store=1)],
            "no content left behind": [
                self.changed("residual", found={"marker": ["Unminute/workspace/workspace.db"]}),
                self.changed("residual", unreadable=["Unminute/workspace/workspace.db"]),
                self.changed("residual", content_backups=["x.bak"])],
            "only the workspace's own files left": [
                self.changed("residual", unexpected_files=["stray.bin"]),
                self.changed("residual", unexpected_files=["workspace/media/"])],
            # TEMP leftovers are named and fail unless explained (review of #335).
            "nothing left in TEMP, APPDATA or the user folder": [
                self.changed("residual", left_unexplained=["TEMP/tmpab12.wav"]),
                self.changed("residual", left_unexplained=["USERPROFILE/.cache/x.bin"]),
                self.changed("residual", temp_files_left=3),
                self.changed("residual", redirected_files_left=None)],
            # The real LocalLow and model cache, by name (the owner's decision,
            # 26 Oct 2026).
            "nothing new outside the base unexplained": [
                self.changed("residual", outside_unexplained=["LocalLow/SomeApp/x.dat"]),
                self.changed("residual", outside_unreadable=["model_cache/gpu/"]),
                self.changed("residual", outside_new=None)],
            "logs scanned": [self.changed("residual", logs_scanned=[])],
            "no crash dumps": [self.changed("dumps", dumps=1),
                               self.changed("dumps", crash_files=1)],
            "idle window measured": [self.changed("idle_window", window_running=False),
                                     self.changed("idle_window", processes=1),
                                     self.changed("idle_window", working_set_mb=0)],
            # #418: the marked meeting in the real window, closed normally.
            "marked meeting shown in the real window, then closed": [
                self.changed("window", shown=dict(shown, window_found=False)),
                self.changed("window", shown=dict(shown, opened=False)),
                self.changed("window", shown=dict(shown, transcript_shown=False)),
                self.changed("window", shown=dict(shown, playing=True)),
                self.changed("window", shown=dict(shown, error="ElementNotAvailableException")),
                self.changed("window", shown=None),
                self.changed("window", closed={"closed": False}),
                self.changed("window", closed={"error": "the window's close step exceeded"}),
                self.changed("window", exit_code=1),
                self.changed("window", exit_code=None),
                self.changed("window", killed=True),
                self.changed("window", processes_ended=False)],
            "the model cache lent to the window, then taken back": [
                self.changed("window", models_lent={"linked": 0, "copied": 0,
                                                    "unreadable": []}),
                self.changed("window", models_lent=dict(lent, unreadable=["a/b.bin"])),
                self.changed("window", models_returned=dict(returned, removed=False)),
                self.changed("window", models_returned=dict(
                    returned, unexplained=["model_cache/parakeet/stray.bin"])),
                self.changed("window", models_returned=dict(returned, unreadable=["x/"])),
                self.changed("window", models_returned=None)],
            "the real start wrote its own files": [
                self.changed("window", start_files=dict(files, launch_record=False)),
                self.changed("window", start_files=dict(files, instance_lock=False)),
                # Written by that start: neither was there before it.
                self.changed("window", start_files_before=dict(files, instance_lock=False)),
                self.changed("window", start_files_before=None)],
            "the windows' Event Log entries counted": [
                self.changed("window", event_log_entries=None),
                self.changed("event_log", window_step=None),
                self.changed("event_log", both_windows=None),
                self.changed("event_log", both_windows=True),
                self.changed("event_log", why="")],
        }
        for name, reports in cases.items():
            for n, r in enumerate(reports):
                with self.subTest(name, case=n):
                    self.assertEqual([name], self.tool.verdict(r))

    def test_a_missing_section_fails(self):
        for section in ("control", "at_rest", "recording", "cleanup", "dumps", "window",
                        "event_log"):
            with self.subTest(section=section):
                r = json.loads(json.dumps(self.GOOD))
                del r[section]
                self.assertTrue(self.tool.verdict(r))

    def test_no_entry_written_is_a_count_too(self):
        # "Either no entry is written, or the report names how many" (#418).
        r = self.changed("window", event_log_entries=0)
        r["event_log"].update(window_step=0, both_windows=0)
        self.assertEqual([], self.tool.verdict(r))

    def window_run(self, code=0, ui_step=None):
        """window_step (#418) with the app, PowerShell, the process tree and
        taskkill stood in for. code: the app's exit code after Close, None
        when it does not end. Returns the step's result, the command that
        started the app and the processes ended with taskkill."""
        import residue
        tool = self.tool
        ws = self.root / "Unminute" / "workspace"
        ws.mkdir(parents=True)
        n = residue.names()
        started, killed = [], []
        cache = self.root / "cache"
        (cache / "parakeet" / "v3").mkdir(parents=True)
        (cache / "parakeet" / "v3" / "model.onnx").write_bytes(b"w" * (residue.LINK_FROM + 1))
        (cache / "parakeet" / "v3" / ".verified.json").write_text("{}", encoding="utf-8")

        class App:
            pid = 4242

            def __init__(self, command, **kw):
                started.append(command)
                self.code = None

            def wait(self, timeout):
                if code is None:
                    raise subprocess.TimeoutExpired("pythonw", timeout)
                self.code = code
                return code

            def poll(self):
                return self.code

        def ui(pid, mode, base):
            # The lent models are there while the window is open.
            lent = (ws.parent / tool.MODELS_FOLDER / "parakeet" / "v3" / "model.onnx").is_file()
            if mode == "open" and lent:
                # The real start's own files, and the profile it opens.
                (ws / n["lock"]).write_bytes(b"")
                (ws / n["logs"]).mkdir()
                (ws / n["logs"] / n["launch"]).write_text("{}", encoding="utf-8")
                profile = ws / n["webview"] / ("a" * 32) / "EBWebView"
                profile.mkdir(parents=True)
                (profile / "Local State").write_text("{}", encoding="utf-8")
                return {"window_found": True, "opened": True, "transcript_shown": True,
                        "playing": False}
            return {"closed": True}
        with mock.patch.object(tool.subprocess, "Popen", App), \
                mock.patch.object(tool, "window_ui", ui_step or ui), \
                mock.patch.object(tool, "tree", lambda pid: [pid, pid + 1]), \
                mock.patch.object(tool, "ended", lambda pids, timeout: True), \
                mock.patch.object(tool, "kill", lambda taskkill, pids: killed.extend(pids)), \
                mock.patch.object(tool.windows_powershell, "system_program",
                                  lambda name: name), \
                mock.patch.object(tool.residue, "event_log_entries", lambda folder: 3):
            out = tool.window_step(Path("runtime"), ["-I", "-B"], {}, Path("app"), ws, n,
                                   self.root, cache)
        return out, started, killed, ws, n

    def test_the_window_step_starts_the_app_as_the_shortcut_and_closes_it(self):
        out, started, killed, ws, n = self.window_run()
        self.assertEqual([[str(Path("runtime") / "pythonw.exe"), "-I", "-B", "-m", "workspace"]],
                         started)
        self.assertEqual([], killed)                  # closed normally: nothing ended by force
        # The models were lent (the weights linked, the record copied), then
        # taken back; the cache itself is untouched.
        self.assertEqual({"linked": 1, "copied": 1, "unreadable": []}, out["models_lent"])
        self.assertEqual({"new": [], "unexplained": [], "removed": True, "unreadable": []},
                         out["models_returned"])
        self.assertFalse((ws.parent / self.tool.MODELS_FOLDER).exists())
        self.assertEqual(self.tool.residue.LINK_FROM + 1,
                         (self.root / "cache" / "parakeet" / "v3" / "model.onnx").stat().st_size)
        self.assertEqual(({"instance_lock": False, "launch_record": False},
                          {"instance_lock": True, "launch_record": True}, 0, 1, 3, 2),
                         (out["start_files_before"], out["start_files"], out["exit_code"],
                          out["profile_files_after_close"], out["event_log_entries"],
                          out["processes"]))
        report = dict(json.loads(json.dumps(self.GOOD)), window=out)
        self.assertEqual([], self.tool.verdict(report))

    def test_a_window_that_does_not_close_is_ended_and_fails(self):
        out, _, killed, _, _ = self.window_run(code=None)
        self.assertEqual((None, True, [4242, 4243]), (out["exit_code"], out["killed"], killed))
        report = dict(json.loads(json.dumps(self.GOOD)), window=out)
        self.assertEqual(["marked meeting shown in the real window, then closed"],
                         self.tool.verdict(report))

    def test_the_lent_models_go_back_when_the_window_step_fails(self):
        def broken(pid, mode, base):
            raise FileNotFoundError("Windows PowerShell 5.1 is missing")
        with self.assertRaises(FileNotFoundError):
            self.window_run(ui_step=broken)
        self.assertFalse((self.root / "Unminute" / self.tool.MODELS_FOLDER).exists())
        self.assertTrue((self.root / "cache" / "parakeet" / "v3" / "model.onnx").is_file())

    def test_the_lent_models_go_where_the_real_start_looks(self):
        source = (ROOT / "workspace" / "__main__.py").read_text(encoding="utf-8")
        self.assertIn(f'local_appdata() / "Unminute" / "{self.tool.MODELS_FOLDER}"', source)

    def test_the_window_step_comes_before_the_at_rest_scan(self):
        import inspect
        source = inspect.getsource(self.tool.check)
        order = [source.index(s) for s in (
            'report["lifecycle"] = child("process")',
            'report["window"] = window_step(',
            "residue.control(base, patterns)",
            "at_rest = scan(base, roots + residue.rest_of(base, keep), patterns)",
            "residue.wipe_profile(workspace, names)",
            'report["cleanup"] = child("cleanup"',
            "after = scan(base, roots + others, patterns)",
            "residue.left_in(redirected, base, rest=others)")]
        self.assertEqual(sorted(order), order)

    def test_the_window_step_uses_the_shortcut_s_flags(self):
        import inspect
        install = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
        launch = re.search(r'^\$LaunchArgs = "(.+) -m workspace"$', install, re.M).group(1)
        source = inspect.getsource(self.tool.check)
        flags = re.search(r"flags = \[(.+?)\]", source).group(1)
        self.assertEqual(launch.split(), [f.strip().strip('"') for f in flags.split(",")])
        self.assertIn('[str(runtime / "pythonw.exe"), *flags, "-m", "workspace"]',
                      inspect.getsource(self.tool.window_step))

    def test_the_window_looks_for_the_meeting_the_lifecycle_marked(self):
        tool = self.tool
        self.assertEqual(("Marker " + tool.MARKER, "Marker " + tool.MARKER
                          + " for the residual check"), (tool.TITLE, tool.PASSAGE))
        self.assertIn('ws.rename(mid, "Marker " + marker)', tool.LIFECYCLE)
        self.assertIn('items[0]["id"], "Marker " + marker + " for the residual check")',
                      tool.LIFECYCLE)

    def test_the_window_script_presses_only_the_meeting_and_prints_no_text(self):
        script = self.tool.WINDOW_SCRIPT
        # Only the sidebar's Meetings and the meeting's title are pressed:
        # never Play, and no mouse or keyboard input.
        self.assertEqual(["$nav", "$open"],
                         re.findall(r"(\$\w+)\.GetCurrentPattern\(\$Invoke\)\.Invoke\(\)", script))
        self.assertEqual(2, script.count(".Invoke()"))
        for word in ("SendKeys", "SendInput", "mouse_event", "keybd_event", "Write-Host",
                     "Write-Output", ".Message"):
            with self.subTest(word=word):
                self.assertNotIn(word, script)
        # It prints one JSON line of answers; an error by its type only.
        self.assertTrue(script.rstrip().endswith("$r | ConvertTo-Json -Compress"))
        self.assertIn("$r.error = $_.Exception.GetType().Name", script)
        answers = re.findall(r"\$r\.\w+ = ([^;\n]+)", script)
        self.assertGreaterEqual(len(answers), 8)
        for value in answers:
            with self.subTest(value=value):
                self.assertRegex(value, r"^(\$true|\$false|\$null|\[bool\]\(Find-InApp |"
                                        r"\$play\.Current\.Name -eq |"
                                        r"\$_\.Exception\.GetType\(\)\.Name$)")
        # Closed through the window's own Close, as a person closes it.
        self.assertIn("[System.Windows.Automation.WindowPattern]::Pattern).Close()", script)

    def test_a_lifecycle_error_fails(self):
        r = self.changed(None, lifecycle={"error": "lifecycle script exceeded 7,200 s"})
        self.assertIn("no error", self.tool.verdict(r))

    def run_main(self, tool, filled, *extra, folder=None):
        if folder is None:
            _, folder = make_package(self.root)
        out = self.root / "report.json"
        calls = []

        def check(version_dir, cache, wav, report, *rest, **kw):
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

                def plant(version_dir, cache, wav, report, *rest, filled=good, **kw):
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

    def test_a_report_that_would_carry_the_user_name_fails_naming_only_where(self):
        # Synthetic name (review of 387f8d3). A library that names a folder
        # after the user (torch's torchinductor_<user>) in the redirected
        # TEMP, explained by an entry added under the owner's decision (b),
        # and a model record named after the user, explained too: the run
        # would pass, and the published report would carry the name.
        import packaged_crash_check as crash
        import residue
        name = "Tāne.Example"
        env = {"USERNAME": name, "USERPROFILE": "C:\\Users\\" + name}
        places = {p: self.root / "redirected" / p for p in ("TEMP", "APPDATA", "USERPROFILE")}
        for folder in places.values():
            folder.mkdir(parents=True)
        (places["TEMP"] / f"torchinductor_{name}").mkdir()
        (places["TEMP"] / f"torchinductor_{name}" / "x.py").write_bytes(b"")
        entry = (r"torchinductor_[^/]+/.+", "a compiler cache (a synthetic entry)")
        with mock.patch.dict(residue.EXPLAINED, {"TEMP": (entry,)}):
            left = residue.left_in(places, self.root)
        record = f"whisper/.verified/small-{name}.json"
        self.assertEqual(([], []), (left["unexplained"],
                                    residue.unexplained({"model_cache": [record]})))
        good = json.loads(json.dumps(self.GOOD))
        good["residual"].update(temp_files_left=left["files"]["TEMP"],
                                redirected_files_left=left["files"],
                                left_explained=left["explained"])
        good["residual"]["outside_new"]["model_cache"].append(record)
        self.assertEqual([], self.tool.verdict(good))
        crashed = {"pass": True, "failed": [],
                   "review_cleanup_crash": {"landing_residue": {
                       "temp_files": left["files"]["TEMP"], "left_unexplained": [],
                       "left_explained": left["explained"]}},
                   "outside": {"new": {"model_cache": [record], "LocalLow": []},
                               "unexplained": [], "unreadable": []}}
        for tool, filled, where in (
                (self.tool, good, [["residual", "redirected_files_left", "TEMP", 0],
                                   ["residual", "temp_files_left", 0],
                                   ["residual", "outside_new", "model_cache", 1]]),
                (crash, crashed, [["outside", "new", "model_cache", 0],
                                  ["review_cleanup_crash", "landing_residue", "temp_files",
                                   0]])):
            with self.subTest(tool=tool.__name__), mock.patch.dict(os.environ, env):
                code, saved, _ = self.run_main(tool, filled)
                text = (self.root / "report.json").read_text(encoding="utf-8")
                self.assertEqual(1, code)
                self.assertEqual((False, ["report would carry the Windows user name"]),
                                 (saved["pass"], saved["failed"]))
                self.assertEqual(sorted(where), sorted(saved["user_name_at"]))
                # Where, never what: no file name and no form of the user name.
                self.assertNotIn("torchinductor", text)
                self.assertNotIn("whisper", text)
                self.assertEqual([], residue.user_name_at(json.loads(text), env))

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


def crash_state(**change):
    """One inspect() of a crash profile (synthetic values in its shape)."""
    state = {"meetings": 1, "phases": ["done"], "errors": [], "chunks": {"done": 4},
             "segments": 40, "duplicate_segments": 0, "attempts": {"done": 4},
             "speaker_runs": ["done"], "speaker_turns": 20, "edits": 0, "cleanup_jobs": [],
             "active_holds": 0, "receipts": [], "audio_files": 1, "freelist_pages": 0,
             "text_sha256": "f" * 64, "marker_in_edits": False, "wal_bytes": 0,
             "audio_key": "opens", "audio_removed": False, "blockers": ["meetings", "media"]}
    state.update(change)
    return state


@unittest.skipUnless(os.name == "nt", "the checks import Windows-only modules")
class CrashVerdictTests(unittest.TestCase):
    """tools/packaged_crash_check.py's verdict, with what #111 C7 adds: kills
    placed by what the parent sees (an encrypted import, two landings around
    audio cleanup, a recording), the file list and positive control after final
    cleanup, and crash dumps."""

    KILL = {"result": 1, "killed": True}
    GONE = crash_state(meetings=0, segments=0, edits=0, audio_files=0, blockers=[],
                       audio_key=None, audio_removed=None,
                       receipts=["audio removed", "transcript removed"])
    REVIEWED = crash_state(audio_files=0, marker_in_edits=True, audio_key="missing",
                           audio_removed=True, receipts=["audio removed"],
                           blockers=["meetings"])
    CLEAN = {"files": ["crash/Unminute/workspace/workspace.db"], "found": {},
             "unreadable": [], "control_missed": [], "unexpected_files": [],
             "temp_files": [], "left_unexplained": [], "logs_scanned": 2}

    def good(self):
        k, clean = self.KILL, self.CLEAN
        return json.loads(json.dumps({
            "reference": crash_state(),
            "processing_crashes": {"kills": [k, k, k], "state": crash_state(
                attempts={"done": 4, "interrupted": 2}, speaker_runs=["failed", "done"]),
                "same_text_as_reference": True, "same_segment_count": True},
            "review_cleanup_crash": {"kill": k, "state": self.REVIEWED, "landings": [
                {"after_s": 0, "kill": k, "landed": "before", "state": self.REVIEWED},
                {"after_s": 0.05, "kill": k, "landed": "after", "state": self.REVIEWED}],
                "landing_residue": {"control_missed": [], "found": {}, "unreadable": [],
                                    "temp_files": [], "left_unexplained": []}},
            "final_cleanup_crash": {"kill": k, "state": self.GONE, "residue": clean},
            "hold_race": {"crash": k, "after_restart": crash_state(active_holds=1),
                          "release": {"result": [], "killed": False},
                          "after_release": crash_state(cleanup_jobs=[
                              {"phase": "audio", "state": "awaiting_confirmation",
                               "error": None}]),
                          "confirm": {"result": 1, "killed": False},
                          "after_confirm": crash_state(audio_files=0),
                          "after_final": self.GONE, "residue": clean},
            "import_crash": {"partial_seen": True, "partial_bytes": 786704, "killed": True,
                             "state": crash_state(meetings=0, audio_files=0, blockers=[],
                                                  audio_key=None, audio_removed=None),
                             "windows": 8, "control_missed": [], "found": {},
                             "unreadable": [], "partial_left": 0, "unexpected_files": [],
                             "temp_files": [], "left_unexplained": []},
            "recording_crash": {"seconds_at_kill": 12.3, "killed": True,
                                "kept": {"recovered": True, "seconds": 10.0, "imported": 1,
                                         "key_before_keep": "opens",
                                         "key_after_import": "missing"},
                                "state": crash_state(), "windows": 8, "control_missed": [],
                                "found": {}, "unreadable": [], "temp_files": [],
                                "left_unexplained": [], "partial_chunks_left": 0},
            "token_removal": {"saved": True, "present_after_removal": False,
                              "loaded_after_removal": False},
            "clipboard_sequence_unchanged": True,
            "outside": {"new": {"model_cache": [], "LocalLow": []}, "unexplained": [],
                        "unreadable": []},
            "dumps": {"dumps": 0, "crash_files": 6}}))

    def setUp(self):
        import packaged_crash_check
        self.tool = packaged_crash_check

    def test_a_good_run_passes_and_expected_crash_files_do_not_fail_it(self):
        self.assertEqual([], self.tool.verdict(self.good()))

    def test_each_new_criterion_fails_on_its_own(self):
        def with_(path, value):
            r = self.good()
            *keys, last = path
            node = r
            for key in keys:
                node = node[key]
            node[last] = value
            return r
        landing = ("review_cleanup_crash", "landings", 1)
        cases = {
            "every cleanup landing consistent, then finished": [
                with_((*landing, "landed"), "inconsistent"),
                with_((*landing, "state", "receipts"), ["audio removed", "audio removed"]),
                with_((*landing, "state", "audio_key"), "opens")],
            "only the workspace's own files after final cleanup": [
                with_(("final_cleanup_crash", "residue", "unexpected_files"), ["stray.bin"]),
                with_(("hold_race", "residue", "control_missed"), ["marker"]),
                with_(("hold_race", "residue", "logs_scanned"), 0)],
            "nothing left after final cleanup crash": [
                with_(("final_cleanup_crash", "residue", "found"),
                      {"window_1": ["crash/Unminute/workspace/workspace.db"]}),
                with_(("final_cleanup_crash", "state", "blockers"), ["media"])],
            "interrupted import discarded, nothing of it left": [
                with_(("import_crash", "partial_seen"), False),
                # Killed before the scan windows were all written: proves nothing.
                with_(("import_crash", "partial_bytes"), 80),
                with_(("import_crash", "partial_bytes"), None),
                with_(("import_crash", "state", "meetings"), 1),
                with_(("import_crash", "state", "blockers"), ["media"]),
                with_(("import_crash", "found"), {"window_4": ["import/Unminute/x"]}),
                with_(("import_crash", "control_missed"), ["window_4"]),
                # Shown directly, not from the app's own folder removal.
                with_(("import_crash", "partial_left"), 1),
                with_(("import_crash", "partial_left"), None),
                with_(("import_crash", "unexpected_files"), ["workspace/media/x/a.uma"])],
            "nothing left in TEMP, APPDATA or the user folder": [
                with_(("final_cleanup_crash", "residue", "left_unexplained"),
                      ["TEMP/tmp1.bin"]),
                with_(("hold_race", "residue", "left_unexplained"), ["APPDATA/x"]),
                with_(("hold_race", "residue", "temp_files"), None),
                with_(("import_crash", "left_unexplained"), ["TEMP/tmp2.bin"]),
                # The landing and recording profiles too (review of #335).
                with_(("review_cleanup_crash", "landing_residue", "left_unexplained"),
                      ["TEMP/tmp3.bin"]),
                with_(("recording_crash", "left_unexplained"), ["USERPROFILE/x.bin"]),
                with_(("recording_crash", "temp_files"), None)],
            "nothing plain in the landing profile": [
                with_(("review_cleanup_crash", "landing_residue", "found"),
                      {"marker": ["landing/Unminute/workspace/x"]}),
                with_(("review_cleanup_crash", "landing_residue", "control_missed"),
                      ["window_2"]),
                with_(("review_cleanup_crash", "landing_residue", "unreadable"),
                      ["landing/home/locked/"])],
            "nothing new outside the base unexplained": [
                with_(("outside", "unexplained"), ["LocalLow/SomeApp/x.dat"]),
                with_(("outside", "unreadable"), ["model_cache/gpu/"]),
                with_(("outside", "new"), None)],
            "recording kept after a crash, nothing plain left": [
                with_(("recording_crash", "kept", "recovered"), False),
                with_(("recording_crash", "kept", "seconds"), 2.0),
                with_(("recording_crash", "kept", "key_after_import"), "opens"),
                # "missing" means something only if the key opened before Keep.
                with_(("recording_crash", "kept", "key_before_keep"), "missing"),
                with_(("recording_crash", "state", "blockers"), ["meetings", "recordings"]),
                with_(("recording_crash", "found"), {"window_1": ["recording/Unminute/x"]}),
                with_(("recording_crash", "partial_chunks_left"), 1)],
            "no crash dumps": [with_(("dumps", "dumps"), 1)],
        }
        for name, reports in cases.items():
            for n, r in enumerate(reports):
                with self.subTest(name, case=n):
                    self.assertEqual([name], self.tool.verdict(r))
        r = with_(("import_crash", "killed"), False)
        self.assertEqual(["every kill landed on a live process"], self.tool.verdict(r))
        # A landing that never happened fails both.
        r = with_(("review_cleanup_crash", "landings"),
                  self.good()["review_cleanup_crash"]["landings"][:1])
        self.assertEqual(["every kill landed on a live process",
                          "every cleanup landing consistent, then finished"],
                         self.tool.verdict(r))

    def test_the_recording_chunks_counted_are_the_recordings_folder_s_only(self):
        source = Path(self.tool.__file__).read_text(encoding="utf-8")
        self.assertIn('"Unminute" / "workspace" / RECORDINGS', source)
        self.assertNotIn('"profile"].rglob("*.tmp")', source)

    def test_the_import_kill_waits_until_every_scan_window_is_written(self):
        # The kill size follows the encrypted audio's layout, and the blocks
        # it waits for cover the seconds the scan windows come from.
        from workspace import audiocrypt, media
        t = self.tool
        self.assertEqual(audiocrypt.DATA_START, t.AUDIO_DATA_START)
        self.assertEqual(audiocrypt.FRAMES_PER_BLOCK * 4 + audiocrypt.TAG_BYTES,
                         t.AUDIO_BLOCK_BYTES)                      # float32 frames
        written_s = t.IMPORT_KILL_BLOCKS * audiocrypt.FRAMES_PER_BLOCK / media.SAMPLE_RATE
        self.assertGreaterEqual(written_s, t.IMPORT_WINDOWS_S + 2)
        self.assertEqual(786704, t.IMPORT_KILL_BYTES)              # as in good()

    def test_watch_for_waits_for_the_size(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            (folder / "m").mkdir()
            partial = folder / "m" / "audio.partial"
            partial.write_bytes(b"x" * 80)
            alive = mock.Mock(poll=mock.Mock(return_value=None))
            with mock.patch.object(self.tool.time, "sleep"):
                self.assertIsNone(self.tool.watch_for(folder, "audio.partial", alive, 0.2,
                                                      1000))
                self.assertEqual(80, self.tool.watch_for(folder, "audio.partial", alive, 5))
                partial.write_bytes(b"x" * 1000)
                self.assertEqual(1000, self.tool.watch_for(folder, "audio.partial", alive, 5,
                                                           1000))

    def test_a_landing_is_classified_by_the_key_and_the_audio(self):
        landing = self.tool.landing
        self.assertEqual("before", landing(crash_state()))
        self.assertEqual("after", landing(crash_state(audio_key="missing",
                                                      audio_removed=True)))
        self.assertEqual("after", landing(crash_state(audio_key="missing", audio_removed=True,
                                                      audio_files=1)))   # files go next
        for bad in (crash_state(audio_key="missing"),               # key gone, audio "there"
                    crash_state(audio_removed=True),                # key kept, audio "gone"
                    crash_state(audio_files=0),                     # files gone, key kept
                    crash_state(audio_key="damaged"), crash_state(audio_key=None)):
            with self.subTest(state=bad):
                self.assertEqual("inconsistent", landing(bad))


class EvidenceChildrenTests(unittest.TestCase):
    """The packaged checks' children (review of #335): the throwaway
    workspace sits where the app's own would, so each passes an audit
    mirror that drops the Windows Event Log copy; and error text copied
    into a report has the base path, the home folder and the user name
    replaced (P3)."""

    def scripts(self):
        import packaged_check
        import packaged_crash_check
        return {"LIFECYCLE": packaged_check.LIFECYCLE, "CHILD": packaged_crash_check.CHILD}

    def test_every_workspace_a_child_opens_has_a_file_only_audit_mirror(self):
        import ast
        for name, source in self.scripts().items():
            tree = ast.parse(source)
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name) and n.func.id == "Workspace"]
            with self.subTest(script=name):
                self.assertTrue(calls)
                for call in calls:
                    mirror = {k.arg: k.value for k in call.keywords}.get("audit_mirror")
                    self.assertIsInstance(mirror, ast.Call)
                    self.assertEqual("FileOnlyMirror", mirror.func.id)

    def test_the_file_only_mirror_reports_nothing(self):
        import ast
        for name, source in self.scripts().items():
            tree = ast.parse(source)
            node = next(n for n in tree.body
                        if isinstance(n, ast.ClassDef) and n.name == "FileOnlyMirror")
            space = {}
            exec(compile(ast.Module([node], []), name, "exec"), space)
            mirror = space["FileOnlyMirror"]()
            with self.subTest(script=name), \
                    mock.patch("ctypes.WinDLL", side_effect=AssertionError("Windows called"),
                               create=True):
                self.assertIsNone(mirror.report(1900, 4, "app_start"))
            # An audit log with it writes its own file and gives the mirror
            # each record, which goes nowhere.
            from workspace import audit
            with tempfile.TemporaryDirectory() as tmp:
                log = audit.AuditLog(Path(tmp) / "audit", mirror=mirror, new_workspace=True)
                log.record("app_start", schema=2, version=None)
                log.close()
                self.assertTrue(list((Path(tmp) / "audit").iterdir()))
                self.assertEqual(0, log.stats["unmirrored"])

    def test_error_tails_copied_into_reports_are_redacted(self):
        import packaged_check
        import packaged_crash_check
        import perf_check
        sources = {tool.__name__: Path(tool.__file__).read_text(encoding="utf-8")
                   for tool in (packaged_check, packaged_crash_check, perf_check)}
        # No raw tail of a child's error output goes into a report.
        for tool, raw in (("packaged_check", "(r.stderr or r.stdout)[-2000:]}"),
                          ("packaged_check", '+ "".join(err)[-2000:]}'),
                          ("packaged_crash_check", '"".join(self.err)[-1500:])'),
                          ("packaged_crash_check", '"".join(c.err)[-1500:])'),
                          ("packaged_crash_check", '+ r.stderr[-1500:])'),
                          ("perf_check", '{r.stderr[-800:]}")')):
            with self.subTest(tool=tool, raw=raw):
                self.assertNotIn(raw, sources[tool])
        self.assertIn("residue.redact((r.stderr or r.stdout)[-2000:], base)",
                      sources["packaged_check"])
        self.assertIn("residue.redact(r.stderr[-800:], workdir)", sources["perf_check"])
        # A child's result is redacted once decoded: JSON escapes a name that
        # is not ASCII, and the escaped form would get through (review of #335).
        self.assertIn("return residue.redact_json(result, base)", sources["packaged_check"])
        self.assertIn('out = residue.redact_json(msg["result"], base)',
                      sources["packaged_check"])
        self.assertNotIn("residue.redact(r.stdout", sources["packaged_check"])
        self.assertNotIn("residue.redact(json.dumps(", sources["packaged_check"])

    def test_the_landing_scan_covers_the_landing_profile(self):
        # Only a packaged run would show a landing scan over the wrong folder,
        # or over none (review of 387f8d3, finding 4), so its arguments are
        # pinned here: the landing profile (with its redirected TEMP, APPDATA
        # and user folder, all under it: ctx()) and those redirected folders.
        import ast
        import packaged_crash_check
        tree = ast.parse(Path(packaged_crash_check.__file__).read_text(encoding="utf-8"))
        check = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name == "check")

        def calls(name):
            return [n for n in ast.walk(check) if isinstance(n, ast.Call)
                    and ast.unparse(n.func) == f"residue.{name}"]
        self.assertIn("ctx('landing')", [ast.unparse(n.value) for n in ast.walk(check)
                                         if isinstance(n, ast.Assign)
                                         and ast.unparse(n.targets[0]) == "land"])
        self.assertIn("[land['profile']]", [ast.unparse(c.args[0]) for c in calls("find")])
        self.assertIn("land['redirected']", [ast.unparse(c.args[0]) for c in calls("left_in")])
        ctx = next(n for n in ast.walk(check) if isinstance(n, ast.FunctionDef)
                   and n.name == "ctx")
        returned = next(n.value for n in ast.walk(ctx) if isinstance(n, ast.Return))
        profile = dict(zip((ast.literal_eval(k) for k in returned.keys), returned.values,
                           strict=True))
        self.assertEqual("base / name", ast.unparse(profile["profile"]))
        redirected = next(n.value for n in ast.walk(ctx) if isinstance(n, ast.Assign)
                          and ast.unparse(n.targets[0]) == "redirected")
        for folder in redirected.values:
            self.assertTrue(ast.unparse(folder).startswith("base / name / "))

    def test_a_child_s_failure_reaches_the_report_without_the_user_s_paths(self):
        import packaged_crash_check
        env = {"USERPROFILE": "C:\\Users\\Pat.Example", "USERNAME": "Pat.Example"}
        child = packaged_crash_check.Child.__new__(packaged_crash_check.Child)
        child.base = env["USERPROFILE"] + "\\Temp\\umcrash-ab12"
        child.err = [f"Traceback\n  File \"{child.base}\\child.py\", line 9\n",
                     "PermissionError: C:\\Users\\Pat.Example\\x\n"]
        with mock.patch.dict(os.environ, env):
            text = child.errors()
        self.assertNotIn("pat.example", text.lower())
        self.assertIn("<base>\\child.py", text)


if __name__ == "__main__":
    unittest.main()
