# tests/test_evidence.py
# Release evidence names its package (#111 B3; tools/evidence.py). Each
# release-time check records the zip's SHA-256 and whether it ran on that zip's
# unchanged files, and a release is refused without one passing report of
# every kind naming the zip being published.

import hashlib
import json
import os
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


def make_package(root, version="0.5.0", release=True):
    """A small package zip and the folder it was extracted (installed) to."""
    name = f"Unminute-{version}"
    folder = Path(root) / "versions" / name
    files = {"app/workspace/__init__.py": b"# app\n",
             "app/workspace/__pycache__/__init__.cpython-314.pyc": b"\0pyc",
             "runtime/python.exe": b"MZ not really",
             "version.json": json.dumps({"version": version, "commit": "c" * 40,
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


def report(kind, sha=SHA, **change):
    ev = {"kind": kind, "package_sha256": sha, "release": True,
          "folder_matches_package": True, "check_passed": True, "pass": True}
    ev.update(change)
    return {"evidence": ev}


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class IdentityTests(TempDir):
    def test_an_unchanged_folder_matches_its_zip(self):
        zpath, folder = make_package(self.root)
        ident = evidence.identity(zpath, folder)
        self.assertEqual(hashlib.sha256(zpath.read_bytes()).hexdigest(), ident["package_sha256"])
        self.assertEqual(("Unminute-0.5.0", "0.5.0", "c" * 40, True),
                         (ident["package"], ident["version"], ident["commit"], ident["release"]))
        self.assertTrue(ident["folder_matches_package"])
        self.assertEqual(0, ident["folder_differences"])

    def test_changed_added_and_missing_files_are_found(self):
        zpath, folder = make_package(self.root)
        (folder / "app/workspace/__init__.py").write_bytes(b"# edited\n")
        (folder / "app/workspace/planted.pth").write_bytes(b"import os\n")
        (folder / "runtime/python.exe").unlink()
        ident = evidence.identity(zpath, folder)
        self.assertFalse(ident["folder_matches_package"])
        self.assertEqual(["added: app/workspace/planted.pth",
                          "changed: app/workspace/__init__.py",
                          "missing: runtime/python.exe"], ident["folder_differences_listed"])

    def test_the_folder_is_compared_with_the_zips_manifest_not_its_own(self):
        # Rewriting the installed manifest cannot hide a planted file.
        zpath, folder = make_package(self.root)
        (folder / "app/planted.py").write_bytes(b"x = 1\n")
        manifest = json.loads((folder / "package-manifest.json").read_text(encoding="utf-8"))
        manifest["app/planted.py"] = [6, hashlib.sha256(b"x = 1\n").hexdigest()]
        (folder / "package-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        listed = evidence.identity(zpath, folder)["folder_differences_listed"]
        self.assertIn("added: app/planted.py", listed)
        self.assertIn("changed: package-manifest.json", listed)

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


class StampTests(TempDir):
    def test_evidence_passes_only_when_the_check_passed_on_the_unchanged_package(self):
        zpath, folder = make_package(self.root)
        self.assertTrue(evidence.stamp({}, "ui", zpath, folder, True)["pass"])
        self.assertFalse(evidence.stamp({}, "ui", zpath, folder, False)["pass"])
        (folder / "app/extra.py").write_bytes(b"")
        ev = evidence.stamp({}, "ui", zpath, folder, True)
        self.assertTrue(ev["check_passed"])
        self.assertFalse(ev["pass"])

    def test_stamping_a_report_file_keeps_its_figures(self):
        zpath, folder = make_package(self.root)
        out = self.root / "ui.json"
        out.write_text(json.dumps({"api_surface": ["status"]}), encoding="utf-8")
        with mock.patch("builtins.print"):
            evidence.stamp_file(out, "ui", zpath, folder, True)
        saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(["status"], saved["api_surface"])
        self.assertEqual("ui", saved["evidence"]["kind"])
        self.assertEqual(hashlib.sha256(zpath.read_bytes()).hexdigest(),
                         saved["evidence"]["package_sha256"])

    def test_an_unknown_kind_is_refused(self):
        zpath, folder = make_package(self.root)
        with self.assertRaises(ValueError):
            evidence.stamp({}, "vibes", zpath, folder, True)


class CheckTests(TempDir):
    def write(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def complete(self, **changes):
        return [self.write(f"{k}.json", changes.get(k, report(k))) for k in evidence.KINDS]

    def test_one_passing_report_of_every_kind_is_complete(self):
        self.assertEqual([], evidence.check(SHA, self.complete()))
        self.assertEqual([], evidence.check(f"  {SHA.upper()}\n", self.complete()))

    def test_each_problem_is_named(self):
        cases = {
            "names another package": report("perf", sha="b" * 64),
            "ran on a development build": report("perf", release=False),
            "differs from the package": report("perf", folder_matches_package=False),
            "did not pass": report("perf", check_passed=False, **{"pass": False}),
        }
        for words, bad in cases.items():
            with self.subTest(words):
                problems = evidence.check(SHA, self.complete(perf=bad))
                self.assertEqual(1, len(problems), problems)
                self.assertIn(words, problems[0])

    def test_a_pass_flag_alone_is_not_enough(self):
        problems = evidence.check(SHA, self.complete(ui=report("ui", check_passed=False)))
        self.assertEqual(["ui.json: ui did not pass"], problems)

    def test_missing_duplicate_unknown_and_unreadable_reports(self):
        paths = self.complete()
        paths.remove(self.root / "crash.json")
        paths.append(self.write("perf-again.json", report("perf")))
        paths.append(self.write("odd.json", report("vibes")))
        paths.append(self.write("plain.json", {"pass": True}))
        paths.append(self.root / "absent.json")
        problems = evidence.check(SHA, paths)
        self.assertIn("perf-again.json: a second perf report (also perf.json)", problems)
        self.assertIn("odd.json: unknown kind 'vibes'", problems)
        self.assertIn("plain.json: not an evidence report (no evidence block)", problems)
        self.assertIn("absent.json: not an evidence report (no evidence block)", problems)
        self.assertIn(f"missing: crash ({evidence.KINDS['crash']})", problems)
        self.assertEqual(5, len(problems))

    def test_no_reports_means_every_kind_is_missing(self):
        self.assertEqual(len(evidence.KINDS), len(evidence.check(SHA, [])))

    def test_the_expected_checksum_must_be_a_sha256(self):
        for bad in ("", "abc", SHA[:-1] + "g", SHA + "a"):
            with self.subTest(bad=bad):
                problems = evidence.check(bad, self.complete())
                self.assertEqual(1, len(problems))
                self.assertIn("64 hex digits", problems[0])

    def test_the_command_line_exit_code(self):
        with mock.patch("builtins.print"):
            self.assertEqual(0, evidence.main(["check", SHA, *map(str, self.complete())]))
            self.assertEqual(1, evidence.main(["check", SHA]))


class ArgumentTests(TempDir):
    def test_app_folder_reads_app_from_the_command_line(self):
        default = Path("checkout")
        self.assertEqual(default, evidence.app_folder(default, ["x.py", "out.json"]))
        self.assertEqual((self.root / "app").resolve(),
                         evidence.app_folder(default, ["x.py", "--app", str(self.root / "app")]))
        self.assertEqual((self.root / "app").resolve(),
                         evidence.app_folder(default, ["x.py", f"--app={self.root / 'app'}"]))

    def test_app_modules_must_come_from_the_package(self):
        app = self.root / "Unminute-0.5.0" / "app"
        inside = {n: types.SimpleNamespace(__file__=str(app / n / "__init__.py"))
                  for n in evidence.APP_MODULES}
        with mock.patch.dict(sys.modules, inside):
            evidence.require_app_modules(app)
        outside = dict(inside, asr_engines=types.SimpleNamespace(
            __file__=str(self.root / "checkout" / "asr_engines.py")))
        with mock.patch.dict(sys.modules, outside), \
                self.assertRaisesRegex(SystemExit, "asr_engines was imported from"):
            evidence.require_app_modules(app)

    def test_a_package_check_must_use_that_packages_python(self):
        app = self.root / "Unminute-0.5.0" / "app"
        (app.parent / "runtime").mkdir(parents=True)
        with self.assertRaisesRegex(SystemExit, "package's own Python"):
            evidence.require_package_runtime(app)
        exe = str(app.parent / "runtime" / "python.exe")
        with mock.patch.object(sys, "_base_executable", exe, create=True), \
                mock.patch.object(sys, "executable", exe):
            evidence.require_package_runtime(app)


class ToolTests(TempDir):
    def test_every_kind_is_stamped_by_its_tool(self):
        for kind, described in evidence.KINDS.items():
            with self.subTest(kind=kind):
                source = (ROOT / described.split()[0]).read_text(encoding="utf-8")
                self.assertIn("evidence.stamp_file(", source)
                self.assertRegex(source, rf'stamp_file\([^)]*"{kind}"')

    def test_package_checks_start_the_app_as_the_shortcut_does(self):
        flags = '"-I", "-B", "-X", "disable-remote-debug"'
        for tool in ("packaged_check.py", "packaged_crash_check.py", "perf_check.py"):
            with self.subTest(tool=tool):
                self.assertIn(flags, (ROOT / "tools" / tool).read_text(encoding="utf-8"))

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
                mock.patch.object(sys, "argv", argv), mock.patch("builtins.print"):
            self.assertEqual(rc, perf_check.main())
        return json.loads(out.read_text(encoding="utf-8"))["evidence"]

    def test_perf_evidence_needs_a_full_run_within_budget(self):
        zpath, folder = make_package(self.root)
        self.assertTrue(self.run_perf(zpath, folder, 0, "--leak-minutes", "30")["pass"])
        self.assertFalse(self.run_perf(zpath, folder, 1, "--leak-minutes", "30")["pass"])
        self.assertFalse(self.run_perf(zpath, folder, 0, "--leak-minutes", "5")["pass"])
        self.assertFalse(self.run_perf(zpath, folder, 0, "--quick", "--leak-minutes", "30")
                         ["pass"])

    def test_perf_evidence_needs_the_app_it_measured(self):
        import perf_check
        argv = ["perf_check.py", str(self.root / "perf.json"), "--zip", "x.zip"]
        with mock.patch.object(sys, "argv", argv), mock.patch("sys.stderr"), \
                self.assertRaises(SystemExit):
            perf_check.main()


@unittest.skipUnless(os.name == "nt", "the check imports Windows-only modules")
class PackagedVerdictTests(unittest.TestCase):
    # The figures of the B2b release build's run (#185), less the paths.
    GOOD = {"self_test": "PASS",
            "lifecycle": {"meetings_imported": 1, "transcribed_s": 1075, "worker_peak_mb": 2463,
                          "speakers": "done", "passages": 372, "meetings_left": 0,
                          "receipts": ["audio removed", "transcript removed"],
                          "blocked_network_attempts": 0},
            "residual": {"files_after_cleanup": ["settings.json", "workspace.db"],
                         "marker_found_in": [], "content_backups": []},
            "idle_window": {"processes": 7, "cpu_percent_of_one_core": 1.5,
                            "working_set_mb": 484}}

    def setUp(self):
        import packaged_check
        self.verdict = packaged_check.verdict

    def changed(self, section, **values):
        r = json.loads(json.dumps(self.GOOD))
        if section:
            r[section].update(values)
        else:
            r.update(values)
        return r

    def test_a_good_run_passes(self):
        self.assertEqual([], self.verdict(self.GOOD))

    def test_each_criterion_fails_on_its_own(self):
        cases = {
            "no error": self.changed(None, error="RuntimeError: x"),
            "self-test passed": self.changed(None, self_test="FAIL"),
            "transcribed with speakers": self.changed("lifecycle", speakers="failed"),
            "cleaned up, with both receipts": self.changed("lifecycle",
                                                           receipts=["audio removed"]),
            "no network attempts": self.changed("lifecycle", blocked_network_attempts=1),
            "no content left behind": self.changed("residual",
                                                   marker_found_in=["workspace.db"]),
            "idle window measured": self.changed("idle_window", processes=0),
        }
        for name, r in cases.items():
            with self.subTest(name):
                self.assertEqual([name], self.verdict(r))

    def test_a_lifecycle_error_fails(self):
        r = self.changed(None, lifecycle={"error": "lifecycle script exceeded 7,200 s"})
        self.assertIn("no error", self.verdict(r))


if __name__ == "__main__":
    unittest.main()
