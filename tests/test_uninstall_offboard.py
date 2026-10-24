# tests/test_uninstall_offboard.py
# uninstall.ps1 -Offboard (#111 B8b; the owner's decisions B8-2 and B8-5 of
# 21 Oct 2026): the per-user uninstaller runs the installed app's
# --offboard-report and --offboard as the user before it removes anything,
# and removes the program only when offboarding finished cleanly.
#   - Offboarding comes after the uninstaller's own refusals and before any
#     removal, run as the uninstaller runs --restore-lid (isolated, no
#     bytecode, no remote debugging, from the version's app folder).
#   - Other entries left in Unminute's data folder (exit 4, others_left) are
#     reported with their count and the uninstall carries on (the owner's
#     decision of 23 Oct 2026). Any other result (blocked, open, Local
#     System, a refused location, a failed step, a hang, a version without
#     the switch, a line of an unexpected shape) stops the uninstall with
#     the app's exit code (1 for the uninstaller's own failures): the
#     program, its shortcut, its pins and its uninstall entry stay, and the
#     counts are printed (content-free).
#   - A rerun after a program removal that stopped part-way, with nothing
#     of offboarding's left, finishes the removal; with something left it
#     stops and says the program's files are incomplete.
#   - Without -Silent it shows what will go and asks for REMOVE.
#   - Pins that run this install's pythonw.exe are removed; pins to anything
#     else, and anything reached through a link anywhere on the pin path,
#     are kept.
#   - The real offboard module, through the uninstaller, removes an empty
#     synthetic workspace and stops at one that holds a meeting.
# Throwaway installs, LOCALAPPDATA, APPDATA, Start menu folder and uninstall
# key (tests/test_hardening.py's DataFolderOverlapTests); synthetic data with
# the test key protector; fake credentials, lid and no Event Log copy.
#   python -m unittest tests.test_uninstall_offboard -v

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import keyprotect  # noqa: E402
import test_hardening as hardening  # noqa: E402

MARKER = "Zqxvmarker"                     # synthetic text that must never reach the output
PINNED = Path("Microsoft", "Internet Explorer", "Quick Launch", "User Pinned")

# A stand-in for the app: records each start, then answers each switch as the
# plan in UNMINUTE_TEST_PLAN says: {switch: [exit code or "hang", output]}.
STUB_APP = '''\
import json, os, sys, time
from pathlib import Path
install = Path(__file__).resolve().parents[4]
log = Path(os.environ["UNMINUTE_TEST_LOG"])
calls = json.loads(log.read_text()) if log.exists() else []
calls.append({"argv": sys.argv[1:], "cwd": os.getcwd(), "isolated": sys.flags.isolated,
              "no_bytecode": sys.dont_write_bytecode, "x": sorted(sys._xoptions),
              "installed": (install / "current.json").is_file()})
log.write_text(json.dumps(calls))
code, out = json.loads(os.environ["UNMINUTE_TEST_PLAN"]).get(sys.argv[-1], [9, None])
if code == "hang":
    time.sleep(600)
if out is not None:
    print(out if isinstance(out, str) else json.dumps(out))
sys.exit(code)
'''

# The real offboard module, through a stand-in package whose path reaches
# this checkout's workspace folder: fake credentials, a fake lid, no Event
# Log copy, and the app taken as closed (the real __main__'s account, mutex
# and location checks are tests/test_offboard.py's).
REAL_INIT = '''\
__path__.append({real!r})
'''
REAL_MAIN = '''\
import json, os, sys
from workspace import offboard


class Creds:
    stored = {{"Scratch/UninstallOffboard"}}

    @classmethod
    def has_token(cls, target):
        return target in cls.stored

    @classmethod
    def remove_token(cls, target):
        return False


class Profiles:                         # the decoder's profile (#299), never the real one
    present = True

    @classmethod
    def profile_exists(cls):
        return cls.present

    @classmethod
    def delete_profile(cls):
        return cls.present


out, code = offboard.run(offboard.Where(os.environ["LOCALAPPDATA"]), sys.argv[-1] == "--offboard",
                         account=False, session_running=lambda: False, mirror=None,
                         tokens=("Scratch/UninstallOffboard",), creds=Creds,
                         lid=lambda path, running: "none", profiles=Profiles)
print(json.dumps(out))
sys.exit(code)
'''

OK_REPORT = {"result": "ok", "workspace": "empty", "blockers": {}, "leftover": 0, "others": 0,
             "models_mb": 12, "models_link": False, "token": True, "lid_record": False,
             "running": False, "decoder_profile": True}
OFFBOARDED = dict(OK_REPORT, result="offboarded", workspace="none", models_mb=0, token=False,
                  decoder_profile=False,
                  removed={"workspace": True, "models": True, "token": True,
                           "decoder_profile": True}, residuals=["x"])


def is_reparse_point(path):
    """A junction or other reparse point (os.path.isjunction misses none here)."""
    import stat
    return bool(getattr(os.lstat(path), "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def shortcut(lnk, target):
    """A .lnk to target, written by Windows' own shortcut object (as pins are)."""
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:UNMINUTE_LNK); "
              "$s.TargetPath = $env:UNMINUTE_TARGET; $s.Save()")
    Path(lnk).parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([hardening.windows_powershell.executable(), "-NoProfile", "-Command",
                        script], capture_output=True, text=True, timeout=60,
                       env=hardening.windows_powershell.environment(
                           {"UNMINUTE_LNK": str(lnk), "UNMINUTE_TARGET": str(target)}))
    assert r.returncode == 0 and Path(lnk).is_file(), r.stdout + r.stderr


@unittest.skipUnless(sys.platform == "win32", "the uninstaller is Windows PowerShell")
class UninstallOffboardTests(unittest.TestCase):
    PROGRAMS, KEY, TEST_KEY = (hardening.DataFolderOverlapTests.PROGRAMS,
                               hardening.DataFolderOverlapTests.KEY,
                               hardening.DataFolderOverlapTests.TEST_KEY)
    uninstaller_copy = hardening.DataFolderOverlapTests.uninstaller_copy
    ROAMING = hardening.DataFolderOverlapTests.ROAMING
    key_exists = hardening.DataFolderOverlapTests.key_exists
    delete_test_key = hardening.DataFolderOverlapTests.delete_test_key
    LIMIT = "$OffboardSeconds = 900"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.local = self.root / "Local"
        self.local.mkdir()
        self.log = self.root / "started.json"
        self.copy, self.link = self.uninstaller_copy(self.tmp.name)
        self.pins = self.root / "roaming" / PINNED
        self.addCleanup(self.delete_test_key)

    # -- the throwaway install ------------------------------------------------------

    def install(self, name="Apps", app=None, features=("gate", "restore-lid", "offboard")):
        """current.json, version.json, a runtime (this Python's interpreter
        with a ._pth naming the standard library and the app folder), the
        stand-in app or `app`, the shortcut and the uninstall entry."""
        import winreg
        install = self.root / name
        target = install / "versions" / "9.9.0"
        (target / "app" / "workspace").mkdir(parents=True)
        (target / "app" / "workspace" / "__init__.py").write_text("")
        (target / "app" / "workspace" / "__main__.py").write_text(STUB_APP)
        if app is not None:
            for name_, text in app.items():
                (target / "app" / "workspace" / name_).write_text(text)
        (target / "version.json").write_text(json.dumps({"version": "9.9.0",
                                                          "features": list(features)}))
        (install / "current.json").write_text(json.dumps({"version": "9.9.0",
                                                           "path": str(target)}))
        shutil.copy(self.copy, install / "uninstall.ps1")
        base = Path(sys.base_prefix)
        runtime = target / "runtime"
        runtime.mkdir()
        shutil.copy(base / "python.exe", runtime)
        shutil.copy(base / "python.exe", runtime / "pythonw.exe")   # what pins run
        for dll in list(base.glob("python3*.dll")) + list(base.glob("vcruntime*.dll")):
            shutil.copy(dll, runtime)
        versioned = [d for d in base.glob("python3*.dll") if d.stem != "python3"]
        self.assertEqual(len(versioned), 1, versioned)
        (runtime / f"{versioned[0].stem}._pth").write_text("\n".join(
            [str(base / "Lib"), str(base / "DLLs"), str(target / "app")]) + "\n")
        # Every file the version holds, as tools/build_package.py lists them (sizes
        # and hashes are not what the uninstaller looks at).
        (target / "package-manifest.json").write_text(json.dumps(
            {p.relative_to(target).as_posix(): [p.stat().st_size, "0" * 64]
             for p in sorted(target.rglob("*")) if p.is_file()}))
        self.link.write_text("shortcut")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.TEST_KEY) as key:
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(install))
        return install

    def uninstall(self, install, plan=None, offboard=True, silent=True, stdin=None,
                  script=None):
        env = dict(os.environ, LOCALAPPDATA=str(self.local), UNMINUTE_TEST_LOG=str(self.log),
                   UNMINUTE_TEST_PLAN=json.dumps(plan or {}))
        call = (f"& '{script or self.copy}' -InstallDir '{install}'"
                + (" -Silent" if silent else "") + (" -Offboard" if offboard else ""))
        return subprocess.run([hardening.windows_powershell.executable(), "-NoProfile",
                               "-ExecutionPolicy", "Bypass",
                               "-Command", f"{call}; exit $LASTEXITCODE"],
                              input=stdin, capture_output=True, text=True, timeout=300,
                              cwd=self.tmp.name, env=env)

    def calls(self):
        return json.loads(self.log.read_text()) if self.log.exists() else []

    def assert_kept(self, install):
        """The program, its shortcut and its uninstall entry are all there."""
        self.assertTrue((install / "current.json").is_file())
        self.assertTrue((install / "versions" / "9.9.0" / "runtime" / "python.exe").is_file())
        self.assertTrue((install / "uninstall.ps1").is_file())
        self.assertTrue(self.link.is_file())
        self.assertTrue(self.key_exists())

    def assert_removed(self, install):
        self.assertFalse(install.exists())
        self.assertFalse(self.link.exists())
        self.assertFalse(self.key_exists())

    # -- order and how the app is run -----------------------------------------------

    def test_offboarding_runs_first_as_the_lid_restore_does(self):
        install = self.install()
        r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                     "--offboard": [0, OFFBOARDED]})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.calls()
        self.assertEqual([c["argv"] for c in calls], [["--offboard-report"], ["--offboard"]])
        for c in calls:
            self.assertEqual((c["isolated"], c["no_bytecode"]), (1, True))
            self.assertIn("disable-remote-debug", c["x"])
            self.assertEqual(os.path.normcase(c["cwd"]),
                             os.path.normcase(str(install / "versions" / "9.9.0" / "app")))
            self.assertTrue(c["installed"])                       # before any removal
        self.assert_removed(install)
        self.assertNotIn("workspace was kept", r.stdout + r.stderr)
        self.assertNotIn("models were kept", r.stdout + r.stderr)

    def test_without_the_switch_nothing_is_offboarded(self):
        install = self.install()
        r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                     "--offboard": [0, OFFBOARDED]}, offboard=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls(), [])
        self.assert_removed(install)

    # -- anything but a clean result stops the uninstall (B8-2) --------------------

    def test_a_report_that_is_not_clean_stops_it_with_nothing_removed(self):
        cases = {
            5: ({"result": "blocked", "workspace": "blocked",
                 "blockers": {"meetings": 2, "holds": 1}}, "meetings: 2, holds: 1"),
            3: ({"result": "running"}, "is open"),
            2: ({"result": "system_account"}, "Local System"),
            6: ({"result": "refused"}, "refused"),
            1: ({"result": "failed", "step": "lock"}, "lock"),
        }
        for code, (out, says) in cases.items():
            with self.subTest(code=code):
                self.log.unlink(missing_ok=True)
                install = self.install(f"Apps{code}")
                r = self.uninstall(install, {"--offboard-report": [code, out],
                                             "--offboard": [0, OFFBOARDED]})
                self.assertEqual(r.returncode, code, r.stdout + r.stderr)
                self.assertEqual([c["argv"] for c in self.calls()], [["--offboard-report"]])
                self.assertIn(says, r.stderr)
                self.assertIn("program was kept", r.stderr)
                self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]), out)
                self.assert_kept(install)

    def test_exit_2_names_both_causes(self):
        for out in ({"result": "system_account"}, {"result": "account_unknown"}):
            with self.subTest(out["result"]):
                self.log.unlink(missing_ok=True)
                install = self.install(f"Apps-{out['result']}")
                r = self.uninstall(install, {"--offboard-report": [2, out]})
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("Local System", r.stderr)
                self.assertIn("could not say which account", r.stderr)
                self.assert_kept(install)

    # -- other entries left in the data folder are reported, not a stop ------------

    def test_other_entries_left_are_reported_and_the_uninstall_carries_on(self):
        # The owner's decision of 23 Oct 2026: exit 4 (others_left) is
        # reported with its count; only a blocked offboarding stops it.
        report = dict(OK_REPORT, result="others_left", others=2)
        removed = dict(OFFBOARDED, result="others_left", others=2)
        for name, plan in (("both", {"--offboard-report": [4, report],
                                     "--offboard": [4, removed]}),
                           ("after", {"--offboard-report": [0, OK_REPORT],
                                      "--offboard": [4, removed]})):
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                install = self.install(f"Apps-{name}")
                ours = install / "versions" / "9.9.0" / "runtime" / "pythonw.exe"
                shortcut(self.pins / "TaskBar" / f"Unminute-{name}.lnk", ours)
                r = self.uninstall(install, plan)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual([c["argv"] for c in self.calls()],
                                 [["--offboard-report"], ["--offboard"]])
                self.assertIn("2 other item(s)", r.stderr)
                self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]), removed)
                self.assert_removed(install)
                self.assertFalse((self.pins / "TaskBar" / f"Unminute-{name}.lnk").exists())

    def test_other_entries_are_named_in_the_question(self):
        report = dict(OK_REPORT, result="others_left", others=3)
        install = self.install()
        r = self.uninstall(install, {"--offboard-report": [4, report],
                                     "--offboard": [4, dict(OFFBOARDED, result="others_left",
                                                            others=3)]},
                           silent=False, stdin="no\n")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("3 other item(s)", r.stdout)
        self.assertEqual([c["argv"] for c in self.calls()], [["--offboard-report"]])
        self.assert_kept(install)

    def test_an_offboarding_that_stops_keeps_the_program(self):
        # The report was clean, then the removal stopped: a meeting added
        # meanwhile, an app opened, or a step that failed (run it again).
        for code, out in ((5, {"result": "blocked", "blockers": {"meetings": 1}}),
                          (3, {"result": "running"}),
                          (1, {"result": "failed", "step": "workspace"})):
            with self.subTest(code=code):
                self.log.unlink(missing_ok=True)
                install = self.install(f"Apps{code}")
                r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                             "--offboard": [code, out]})
                self.assertEqual(r.returncode, code, r.stdout + r.stderr)
                self.assertEqual(len(self.calls()), 2)
                self.assertIn("program was kept", r.stderr)
                self.assert_kept(install)

    def test_a_result_it_cannot_read_stops_it(self):
        # No JSON, an unexpected code, or a crash: exit 1, program kept.
        for name, plan in (("garbled", {"--offboard-report": [0, "not json"]}),
                           ("unknown", {"--offboard-report": [9, OK_REPORT]}),
                           ("crash", {"--offboard-report": [0, OK_REPORT],
                                      "--offboard": [7, None]})):
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                install = self.install(f"Apps-{name}")
                r = self.uninstall(install, plan)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn("program was kept", r.stderr)
                self.assert_kept(install)

    def test_a_line_of_an_unexpected_shape_stops_it_and_is_not_passed_on(self):
        # The line is passed to standard output only when it has the app's
        # shape: one object, known fields of the right kinds, and a result
        # that matches the exit code.
        cases = {
            "array": [0, ["ok"]],
            "unknown field": [0, dict(OK_REPORT, note=MARKER)],
            "text for a count": [0, dict(OK_REPORT, others=MARKER)],
            "text for yes or no": [0, dict(OK_REPORT, token=MARKER)],
            "text for the decoder's profile": [0, dict(OK_REPORT, decoder_profile=MARKER)],  # #299
            "not a fixed word": [0, dict(OK_REPORT, workspace=MARKER)],
            "blocker not a count": [0, dict(OK_REPORT, blockers={"meetings": MARKER})],
            "result for another code": [0, dict(OK_REPORT, result="blocked")],
            "removal result": [0, dict(OK_REPORT, result="offboarded")],
            "others_left for 0": [0, dict(OK_REPORT, result="others_left")],
        }
        for n, (name, report) in enumerate(cases.items()):
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                install = self.install(f"Apps-shape{n}")
                r = self.uninstall(install, {"--offboard-report": report,
                                             "--offboard": [0, OFFBOARDED]})
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertEqual([c["argv"] for c in self.calls()], [["--offboard-report"]])
                self.assertNotIn(MARKER, r.stdout + r.stderr)
                self.assertNotIn('"result"', r.stdout)
                self.assert_kept(install)
        # The removal's line is checked the same way.
        self.log.unlink(missing_ok=True)
        install = self.install("Apps-removal")
        r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                     "--offboard": [0, dict(OFFBOARDED, removed={"x": MARKER})]})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn(MARKER, r.stdout + r.stderr)
        self.assert_kept(install)

    def test_a_hung_offboarding_is_stopped_and_the_program_kept(self):
        text = self.copy.read_text(encoding="utf-8")
        self.assertEqual(text.count(self.LIMIT), 1)
        quick = self.root / "uninstall-quick.ps1"
        quick.write_text(text.replace(self.LIMIT, "$OffboardSeconds = 3"), encoding="utf-8")
        install = self.install()
        began = time.monotonic()
        r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                     "--offboard": ["hang", None]}, script=quick)
        self.assertLess(time.monotonic() - began, 120)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("did not finish", r.stderr)
        self.assert_kept(install)

    def test_a_version_without_the_switch_is_never_started(self):
        # An older version would take --offboard for an ordinary start and open
        # its window: nothing runs, nothing is removed.
        install = self.install(features=("gate", "restore-lid"))
        r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                     "--offboard": [0, OFFBOARDED]})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.calls(), [])
        self.assertIn("cannot offboard", r.stderr)
        self.assertIn("older", r.stderr)
        self.assert_kept(install)

    # -- a rerun after a program removal that stopped part-way ----------------------

    def broken(self, name, part, pin=None):
        """An install whose program removal stopped part-way after a clean
        offboarding: the runtime, or the whole version, gone. pin: a pin to
        it, made while its files were there."""
        install = self.install(name)
        if pin:
            shortcut(pin, install / "versions" / "9.9.0" / "runtime" / "pythonw.exe")
        shutil.rmtree(install / "versions" / "9.9.0" / part if part else install / "versions")
        return install

    def test_a_rerun_with_nothing_left_to_offboard_finishes_the_removal(self):
        for name, part in (("runtime", "runtime"), ("versions", None)):
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                pin = self.pins / "TaskBar" / f"Unminute-{name}.lnk"
                install = self.broken(f"Apps-{name}", part, pin)
                r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                             "--offboard": [0, OFFBOARDED]})
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.calls(), [])
                self.assertNotIn("older", r.stdout + r.stderr)
                self.assertIn("incomplete", r.stderr)
                self.assertIn("may have stopped part-way", r.stderr)
                self.assertIn("Credential Manager", r.stderr)
                self.assert_removed(install)
                self.assertFalse(pin.exists())

    def test_a_rerun_with_data_left_says_the_files_are_incomplete(self):
        # Something of offboarding's is still there (a workspace, the models,
        # a part-removed workspace, or a data folder that is a link): the
        # program is kept and the message does not blame the version's age.
        data = self.local / "Unminute"
        for left in ("workspace", "models", "workspace.offboarding-" + "0" * 32, "link"):
            with self.subTest(left):
                if os.path.lexists(data):
                    if is_reparse_point(data):
                        os.rmdir(data)                           # the junction only
                    else:
                        shutil.rmtree(data)
                if left == "link":
                    (self.root / "data-elsewhere").mkdir(exist_ok=True)
                    hardening.mklink_junction(data, self.root / "data-elsewhere",
                                              capture_output=True, check=True)
                else:
                    (data / left).mkdir(parents=True)
                self.log.unlink(missing_ok=True)
                install = self.broken(f"Apps-{left[:12]}", "runtime")
                r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                             "--offboard": [0, OFFBOARDED]})
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertEqual(self.calls(), [])
                self.assertIn("incomplete", r.stderr)
                self.assertIn("may have stopped part-way", r.stderr)
                self.assertNotIn("older", r.stderr)
                self.assertTrue((install / "current.json").is_file())
                self.assertTrue(self.link.is_file())
                self.assertTrue(self.key_exists())
                self.assertTrue(os.path.lexists(data))
        os.rmdir(data)                                           # the junction only

    def test_a_part_removed_version_that_still_has_its_entry_points_is_incomplete(self):
        # The removal walks a version's module files before __main__.py, and app
        # before runtime: with __init__.py gone the app cannot start, so it is
        # never run, and the rerun rule applies (review of 842aabb, finding 5).
        data = self.local / "Unminute"
        for name, left in (("data", True), ("clean", False)):
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                if left:
                    (data / "workspace").mkdir(parents=True)
                elif data.exists():
                    shutil.rmtree(data)
                install = self.install(f"Apps-part-{name}")
                app = install / "versions" / "9.9.0" / "app" / "workspace"
                (app / "__init__.py").unlink()
                self.assertTrue((app / "__main__.py").is_file())
                r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                             "--offboard": [0, OFFBOARDED]})
                self.assertEqual(self.calls(), [])
                self.assertIn("incomplete", r.stderr)
                if left:
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertIn("Install the current version again", r.stderr)
                    self.assert_kept(install)
                else:
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assert_removed(install)

    def test_a_file_list_it_cannot_trust_is_never_run(self):
        # The version's package-manifest.json is read for its names only: one
        # that is not an object, lacks the entry points, or names a path out of
        # the version folder is unusable, so nothing runs and nothing goes.
        for name, listing in (("list", "[]"),
                              ("entry", json.dumps({"app/workspace/__main__.py": [1, ""]})),
                              ("dotdot", None), ("rooted", None)):
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                install = self.install(f"Apps-list-{name}")
                manifest = install / "versions" / "9.9.0" / "package-manifest.json"
                if listing is None:
                    names = json.loads(manifest.read_text())
                    names["app/../../current.json" if name == "dotdot" else "/runtime/x"] = [1, ""]
                    listing = json.dumps(names)
                manifest.write_text(listing)
                r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                             "--offboard": [0, OFFBOARDED]})
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertEqual(self.calls(), [])
                self.assertIn("cannot be read", r.stderr)
                self.assert_kept(install)

    def test_an_unexpected_failure_says_to_install_again_if_it_repeats(self):
        for step, says in (("unexpected", True), ("workspace", False)):
            with self.subTest(step):
                self.log.unlink(missing_ok=True)
                install = self.install(f"Apps-{step}")
                r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                             "--offboard": [1, {"result": "failed",
                                                                "step": step}]})
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn("Run this again", r.stderr)
                self.assertEqual("install the current version again" in r.stderr, says)
                self.assert_kept(install)

    # -- only this account's own install, never as Local System (review of 842aabb) --

    SID = "    return [Security.Principal.WindowsIdentity]::GetCurrent().User.Value"

    def test_another_accounts_install_is_never_run(self):
        # An IT account naming a leaver's folder: not the folder this account's
        # uninstall entry names, and not in this account's %LOCALAPPDATA%.
        for name in ("entry elsewhere", "no entry"):
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                install = self.install(f"Leaver-{name.split()[0]}")
                if name == "no entry":
                    self.delete_test_key()
                else:
                    import winreg
                    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.TEST_KEY) as key:
                        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ,
                                          str(self.root / "Mine"))
                r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                             "--offboard": [0, OFFBOARDED]})
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertEqual(self.calls(), [])
                self.assertIn("this Windows user's own", r.stderr)
                self.assertNotIn(str(self.root), r.stdout + r.stderr)
                self.assertTrue((install / "current.json").is_file())
                runtime = install / "versions" / "9.9.0" / "runtime"
                self.assertTrue((runtime / "python.exe").is_file())
                self.assertTrue(self.link.is_file())
        # Without -Offboard nothing is run, so the plain removal is unchanged.
        r = self.uninstall(install, offboard=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(install.exists())

    def test_an_install_in_this_users_local_folder_needs_no_entry(self):
        install = self.install("Local/Programs/Unminute")
        self.delete_test_key()
        r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                     "--offboard": [0, OFFBOARDED]})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c["argv"] for c in self.calls()], [["--offboard-report"], ["--offboard"]])
        self.assert_removed(install)

    def test_a_link_into_another_folder_is_not_this_users_own(self):
        # A junction in this account's %LOCALAPPDATA% that leads to another
        # profile's install does not make that install this account's.
        install = self.install("Leaver")
        self.delete_test_key()
        via = self.local / "Programs" / "Unminute"
        via.parent.mkdir(parents=True)
        hardening.mklink_junction(via, install, capture_output=True, check=True)
        try:
            r = self.uninstall(via, {"--offboard-report": [0, OK_REPORT],
                                     "--offboard": [0, OFFBOARDED]})
        finally:
            os.rmdir(via)                                       # the junction only
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.calls(), [])
        self.assertIn("this Windows user's own", r.stderr)
        self.assertTrue((install / "versions" / "9.9.0" / "runtime" / "python.exe").is_file())

    def test_local_system_never_starts_the_app(self):
        # Exit 2, as the app's own refusal, but before the user's runtime is run
        # as Local System at all; also when Windows cannot say which account.
        text = self.copy.read_text(encoding="utf-8")
        self.assertEqual(text.count(self.SID), 1)
        for name, line in (("system", '    return "S-1-5-18"'), ("none", "    return $null"),
                           ("error", '    throw "no account"')):
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                script = self.root / f"uninstall-{name}.ps1"
                script.write_text(text.replace(self.SID, line), encoding="utf-8")
                install = self.install(f"Apps-{name}")
                r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                             "--offboard": [0, OFFBOARDED]}, script=script)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertEqual(self.calls(), [])
                self.assertIn("Local System", r.stderr)
                self.assertIn("could not say which account", r.stderr)
                self.assert_kept(install)

    def test_the_uninstallers_own_refusals_come_first(self):
        # Something in the install folder the installer did not put there:
        # refused before the app is run at all.
        install = self.install()
        (install / "notes.txt").write_text("not the installer's")
        r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                     "--offboard": [0, OFFBOARDED]})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.calls(), [])
        self.assert_kept(install)

    # -- asked, without -Silent -----------------------------------------------------

    def test_without_silent_it_asks_for_remove(self):
        plan = {"--offboard-report": [0, OK_REPORT], "--offboard": [0, OFFBOARDED]}
        install = self.install("Apps-no")
        r = self.uninstall(install, plan, silent=False, stdin="remove\n")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Type REMOVE", r.stdout)
        self.assertIn("12 MB", r.stdout)
        self.assertEqual([c["argv"] for c in self.calls()], [["--offboard-report"]])
        self.assert_kept(install)
        self.log.unlink()
        install = self.install("Apps-yes")
        r = self.uninstall(install, plan, silent=False, stdin="REMOVE\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c["argv"] for c in self.calls()], [["--offboard-report"],
                                                             ["--offboard"]])
        self.assert_removed(install)

    # -- pins -----------------------------------------------------------------------

    def test_pins_to_this_install_go_and_others_stay(self):
        install = self.install()
        ours = install / "versions" / "9.9.0" / "runtime" / "pythonw.exe"
        other = self.root / "Other" / "versions" / "1.0.0" / "runtime" / "pythonw.exe"
        other.parent.mkdir(parents=True)
        shutil.copy(ours, other)
        shortcut(self.pins / "TaskBar" / "Unminute.lnk", ours)
        shortcut(self.pins / "StartMenu" / "Unminute.lnk", ours)
        shortcut(self.pins / "TaskBar" / "Other.lnk", other)
        shortcut(self.pins / "TaskBar" / "Tool.lnk", ours.parent / "python.exe")
        r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                     "--offboard": [0, OFFBOARDED]})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Warning", r.stderr)
        self.assert_removed(install)
        self.assertFalse((self.pins / "TaskBar" / "Unminute.lnk").exists())
        self.assertFalse((self.pins / "StartMenu" / "Unminute.lnk").exists())
        self.assertTrue((self.pins / "TaskBar" / "Other.lnk").is_file())
        self.assertTrue((self.pins / "TaskBar" / "Tool.lnk").is_file())

    def test_a_pin_folder_that_is_a_link_is_never_read_or_removed_through(self):
        install = self.install()
        ours = install / "versions" / "9.9.0" / "runtime" / "pythonw.exe"
        elsewhere = self.root / "elsewhere"
        shortcut(elsewhere / "Unminute.lnk", ours)
        shortcut(self.pins / "TaskBar" / "Unminute.lnk", ours)
        hardening.mklink_junction(self.pins / "StartMenu", elsewhere,
                                  capture_output=True, check=True)
        r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                     "--offboard": [0, OFFBOARDED]})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.pins / "TaskBar" / "Unminute.lnk").exists())
        self.assertTrue((elsewhere / "Unminute.lnk").is_file())
        self.assertIn("by hand", r.stderr)                      # a pin may be left
        os.rmdir(self.pins / "StartMenu")                       # the junction only

    def test_a_link_higher_on_the_pin_path_is_never_read_or_removed_through(self):
        # A link at User Pinned or any folder above it, up to the roaming
        # folder itself: the pins below are neither read nor removed.
        roaming = self.root / "roaming"
        for depth in range(len(PINNED.parts), -1, -1):
            at = roaming.joinpath(*PINNED.parts[:depth])
            with self.subTest(at.name):
                self.log.unlink(missing_ok=True)
                install = self.install(f"Apps{depth}")
                ours = install / "versions" / "9.9.0" / "runtime" / "pythonw.exe"
                elsewhere = self.root / f"elsewhere{depth}"
                pin = elsewhere.joinpath(*PINNED.parts[depth:], "TaskBar", "Unminute.lnk")
                shortcut(pin, ours)
                shutil.rmtree(roaming)
                at.parent.mkdir(parents=True, exist_ok=True)
                hardening.mklink_junction(at, elsewhere, capture_output=True, check=True)
                try:
                    r = self.uninstall(install, {"--offboard-report": [0, OK_REPORT],
                                                 "--offboard": [0, OFFBOARDED]})
                finally:
                    os.rmdir(at)                                # the junction only
                    roaming.mkdir(exist_ok=True)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("by hand", r.stderr)
                self.assertTrue(pin.is_file())
                self.assert_removed(install)

    def test_pins_are_left_without_the_switch(self):
        install = self.install()
        ours = install / "versions" / "9.9.0" / "runtime" / "pythonw.exe"
        shortcut(self.pins / "TaskBar" / "Unminute.lnk", ours)
        r = self.uninstall(install, offboard=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.pins / "TaskBar" / "Unminute.lnk").is_file())

    # -- the real offboard module, end to end ---------------------------------------

    def real_app(self):
        return {"__init__.py": REAL_INIT.format(real=str(ROOT / "workspace")),
                "__main__.py": REAL_MAIN.format()}

    def workspace(self, meeting=False):
        """An empty synthetic workspace (or one with a meeting) and a models folder."""
        ws = self.local / "Unminute" / "workspace"
        st = keyprotect.open_store(ws / "workspace.db")
        if meeting:
            st.create_meeting(MARKER)
        st.close()
        (ws / "logs").mkdir()
        (ws / "logs" / "app.log").write_bytes(b'{"event":"start"}\n')
        (ws / "settings.json").write_text("{}", encoding="ascii")
        models = self.local / "Unminute" / "models" / "speaker"
        models.mkdir(parents=True)
        (models / "model.bin").write_bytes(b"m" * 3000)
        (self.local / "Other").mkdir()
        (self.local / "Other" / "keep.txt").write_bytes(b"other")
        return ws

    def test_the_real_offboarding_removes_an_empty_workspace_then_the_program(self):
        self.workspace()
        install = self.install(app=self.real_app())
        ours = install / "versions" / "9.9.0" / "runtime" / "pythonw.exe"
        shortcut(self.pins / "TaskBar" / "Unminute.lnk", ours)
        r = self.uninstall(install)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.local / "Unminute").exists())
        self.assertEqual((self.local / "Other" / "keep.txt").read_bytes(), b"other")
        self.assert_removed(install)
        self.assertFalse((self.pins / "TaskBar" / "Unminute.lnk").exists())

    def test_the_real_offboarding_leaves_other_entries_and_carries_on(self):
        # A workspace set aside by the guide's recovery steps: never opened or
        # removed, counted, and the program removed all the same (23 Oct 2026).
        self.workspace()
        aside = self.local / "Unminute" / "workspace.renamed"
        aside.mkdir()
        (aside / "workspace.db").write_bytes(MARKER.encode())
        install = self.install(app=self.real_app())
        r = self.uninstall(install)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("1 other item(s)", r.stderr)
        self.assertNotIn(MARKER, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["result"], "others_left")
        self.assertEqual(sorted(p.name for p in (self.local / "Unminute").iterdir()),
                         ["workspace.renamed"])
        self.assertEqual((aside / "workspace.db").read_bytes(), MARKER.encode())
        self.assert_removed(install)

    def test_the_real_offboarding_stops_at_a_meeting_and_keeps_everything(self):
        ws = self.workspace(meeting=True)
        install = self.install(app=self.real_app())
        ours = install / "versions" / "9.9.0" / "runtime" / "pythonw.exe"
        shortcut(self.pins / "TaskBar" / "Unminute.lnk", ours)
        before = {p.relative_to(ws).as_posix(): p.read_bytes()
                  for p in ws.rglob("*") if p.is_file() and not p.name.endswith("-shm")
                  and not (p.name.endswith("-wal") and p.stat().st_size == 0)}
        r = self.uninstall(install)
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("meetings: 1", r.stderr)
        self.assertNotIn(MARKER, r.stdout + r.stderr)
        self.assertNotIn(str(self.root), r.stdout + r.stderr)
        after = {p.relative_to(ws).as_posix(): p.read_bytes()
                 for p in ws.rglob("*") if p.is_file() and not p.name.endswith("-shm")
                 and not (p.name.endswith("-wal") and p.stat().st_size == 0)}
        self.assertEqual(after, before)
        self.assertTrue((self.local / "Unminute" / "models" / "speaker" / "model.bin").is_file())
        self.assert_kept(install)
        self.assertTrue((self.pins / "TaskBar" / "Unminute.lnk").is_file())


class UninstallOffboardTextTests(unittest.TestCase):
    """What the package and the uninstaller say to each other."""

    def test_the_package_declares_the_switch_the_uninstaller_looks_for(self):
        build = (ROOT / "tools" / "build_package.py").read_text(encoding="utf-8")
        uninstall = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertIn('"features": ["gate", "restore-lid", "launch-gate", "offboard"]', build)
        self.assertIn('-notcontains "offboard"', uninstall)
        self.assertIn('"-I -B -X disable-remote-debug -m workspace $Switch"', uninstall)
        main = (ROOT / "workspace" / "__main__.py").read_text(encoding="utf-8")
        self.assertIn('if "--offboard-report" in sys.argv or "--offboard" in sys.argv:', main)

    def test_offboarding_comes_after_the_refusals_and_before_any_removal(self):
        uninstall = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
        body = uninstall[uninstall.index("try {\n    # An empty -InstallDir"):]
        self.assertLess(body.index("did not put there"), body.index("    if ($Options.Offboard) {"))
        # The account and ownership check comes before anything is run.
        self.assertLess(body.index("Assert-OwnInstall $registered"),
                        body.index("Invoke-Offboard\n"))
        self.assertLess(body.index("    if ($Options.Offboard) {"),
                        body.index("    Restore-LidSetting\n"))
        self.assertLess(body.index("    Restore-LidSetting\n"), body.index("Remove-Item"))


if __name__ == "__main__":
    unittest.main()
