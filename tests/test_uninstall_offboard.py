# tests/test_uninstall_offboard.py
# uninstall.ps1 -Offboard (#111 B8b; the owner's decisions B8-2 and B8-5 of
# 21 Oct 2026): the per-user uninstaller runs the installed app's
# --offboard-report and --offboard as the user before it removes anything,
# and removes the program only when offboarding finished cleanly.
#   - Offboarding comes after the uninstaller's own refusals and before any
#     removal, run as the uninstaller runs --restore-lid (isolated, no
#     bytecode, no remote debugging, from the version's app folder).
#   - Any other result (blocked, open, Local System, other entries left, a
#     refused location, a failed step, a hang, a version without the switch)
#     stops the uninstall with the app's exit code (1 for the uninstaller's
#     own failures): the program, its shortcut, its pins and its uninstall
#     entry stay, and the counts are printed (content-free).
#   - Without -Silent it shows what will go and asks for REMOVE.
#   - Pins that run this install's pythonw.exe are removed; pins to anything
#     else, and anything reached through a link, are kept.
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


out, code = offboard.run(offboard.Where(os.environ["LOCALAPPDATA"]), sys.argv[-1] == "--offboard",
                         account=False, session_running=lambda: False, mirror=None,
                         tokens=("Scratch/UninstallOffboard",), creds=Creds,
                         lid=lambda path, running: "none")
print(json.dumps(out))
sys.exit(code)
'''

OK_REPORT = {"result": "ok", "workspace": "empty", "blockers": {}, "leftover": 0, "others": 0,
             "models_mb": 12, "models_link": False, "token": True, "lid_record": False,
             "running": False}
OFFBOARDED = dict(OK_REPORT, result="offboarded", workspace="none", models_mb=0, token=False,
                  removed={"workspace": True, "models": True, "token": True}, residuals=["x"])


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
            4: ({"result": "others_left", "others": 2}, "2 other item(s)"),
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
        os.rmdir(self.pins / "StartMenu")                       # the junction only

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
        self.assertLess(body.index("    if ($Options.Offboard) {"),
                        body.index("    Restore-LidSetting\n"))
        self.assertLess(body.index("    Restore-LidSetting\n"), body.index("Remove-Item"))


if __name__ == "__main__":
    unittest.main()
