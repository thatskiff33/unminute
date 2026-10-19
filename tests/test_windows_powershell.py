# Windows PowerShell 5.1 for the build and check tools (#237): started by its
# absolute path in the system folder, never by name, with an allowlisted
# environment, so a .NET profiler, COMPlus_* or DOTNET_* setting or another
# module path in the parent never reaches it. Synthetic values only.
#   python tools/run_tests.py

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_package  # noqa: E402
import packaged_check  # noqa: E402
import perf_check  # noqa: E402
import process_listeners  # noqa: E402
import windows_powershell as wps  # noqa: E402

# What a profiler-injecting or otherwise hostile parent might carry.
POISON = {"COR_ENABLE_PROFILING": "1",
          "COR_PROFILER": "{00000000-0000-0000-0000-000000000000}",
          "COR_PROFILER_PATH": r"C:\synthetic\profiler.dll",
          "COMPlus_ETWEnabled": "0",
          "COMPLUS_Version": "v9.9",
          "DOTNET_gcServer": "1",
          "CORECLR_ENABLE_PROFILING": "1",
          "PSModulePath": r"C:\Program Files\PowerShell\7\Modules",
          "UNMINUTE_SYNTHETIC_PARENT": "x"}
# Prefixes that must never reach the child.
FORBIDDEN = ("COR_", "COMPLUS_", "DOTNET_", "CORECLR_")


def forbidden(names):
    return [n for n in names if n.upper().startswith(FORBIDDEN)]


class FakeWindows:
    """A system folder in a temporary directory, with or without a
    powershell.exe, and subprocess.run replaced by a recorder."""

    def __init__(self, root, with_exe=True):
        self.system = Path(root) / "Windows" / "System32"
        self.home = self.system / "WindowsPowerShell" / "v1.0"
        self.home.mkdir(parents=True)
        if with_exe:
            (self.home / "powershell.exe").write_bytes(b"")
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        out = "" if kwargs.get("text") else b""
        return subprocess.CompletedProcess(argv, 0, out, out)

    def patches(self):
        return (mock.patch.object(wps, "system_folder", return_value=str(self.system)),
                mock.patch.object(wps.subprocess, "run", self.run))


class WindowsPowerShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, **kw):
        return FakeWindows(self.root, **kw)

    def test_the_child_is_started_by_its_full_path(self):
        win = self.fake()
        first, second = win.patches()
        with first, second:
            wps.run("Get-Date")
        (argv, kwargs), = win.calls
        self.assertEqual(argv, [str(win.home / "powershell.exe"), "-NoProfile",
                                "-NonInteractive", "-Command", "Get-Date"])
        self.assertTrue(os.path.isabs(argv[0]))
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)

    def test_a_missing_powershell_fails_clearly_and_runs_nothing(self):
        # A powershell.exe on PATH or in the current folder is never used instead.
        win = self.fake(with_exe=False)
        decoy = self.root / "decoy"
        decoy.mkdir()
        (decoy / "powershell.exe").write_bytes(b"")
        first, second = win.patches()
        with first, second, mock.patch.dict(os.environ, {"PATH": str(decoy)}), \
                self.assertRaisesRegex(FileNotFoundError,
                                       "Windows PowerShell 5.1 is missing from the system "
                                       "folder"):
            wps.run("Get-Date")
        self.assertEqual(win.calls, [])

    def test_the_child_receives_only_the_allowlisted_environment(self):
        win = self.fake()
        parent = {**POISON, "PATH": str(self.root / "bin"), "SystemRoot": r"C:\Elsewhere",
                  "TEMP": str(self.root / "temp"), "TMP": str(self.root / "temp")}
        first, second = win.patches()
        with first, second, mock.patch.dict(os.environ, parent):
            wps.run("Get-Date", {"UNMINUTE_FILE": r"C:\a b\x.exe"})
        env = win.calls[0][1]["env"]
        allowed = {n.upper() for n in wps.KEPT} | {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE",
                                                   "PATH", "PSMODULEPATH", "UNMINUTE_FILE"}
        self.assertLessEqual({k.upper() for k in env}, allowed)
        self.assertEqual(forbidden(env), [])
        self.assertNotIn("UNMINUTE_SYNTHETIC_PARENT", env)
        self.assertEqual(env["PSModulePath"], str(win.home / "Modules"))
        self.assertEqual(env["PATH"].split(os.pathsep),
                         [str(win.system), str(win.system / "Wbem"), str(win.home)])
        self.assertEqual(env["SystemRoot"], str(win.system.parent))
        self.assertEqual(env["windir"], str(win.system.parent))
        self.assertEqual(env["TEMP"], str(self.root / "temp"))
        self.assertEqual(env["UNMINUTE_FILE"], r"C:\a b\x.exe")

    def test_every_tool_starts_powershell_through_the_helper(self):
        # The build's signature check, the package check's process tree, the
        # window checks' process tree and the performance check's machine
        # probe: each reaches the full path with the clean environment.
        sites = {
            "build_package.powershell": lambda: build_package.powershell("Get-Date",
                                                                         file="x"),
            "packaged_check.tree": lambda: packaged_check.tree(1),
            "process_listeners.process_tree": lambda: process_listeners.process_tree(1),
            "perf_check.WindowsProbe.cim": lambda: perf_check.WindowsProbe().cim(),
        }
        for name, call in sites.items():
            with self.subTest(site=name):
                # patches() replaces subprocess.run itself, so a tool that
                # still called it directly would be recorded too.
                win = FakeWindows(self.root / name)
                first, second = win.patches()
                with first, second, mock.patch.dict(os.environ, POISON):
                    call()
                self.assertEqual(len(win.calls), 1)
                argv, kwargs = win.calls[0]
                self.assertEqual(argv[0], str(win.home / "powershell.exe"))
                self.assertEqual(argv[1:4], ["-NoProfile", "-NonInteractive", "-Command"])
                env = kwargs.get("env")
                self.assertIsNotNone(env, "the child would inherit the whole environment")
                self.assertEqual(forbidden(env), [])
                self.assertEqual(env["PSModulePath"], str(win.home / "Modules"))
                self.assertNotIn("UNMINUTE_SYNTHETIC_PARENT", env)

    def test_no_tool_starts_powershell_by_name(self):
        for tool in ("build_package.py", "packaged_check.py", "process_listeners.py",
                     "perf_check.py"):
            with self.subTest(tool=tool):
                source = (ROOT / "tools" / tool).read_text(encoding="utf-8")
                self.assertNotIn('["powershell"', source)
                self.assertNotIn('"powershell.exe"', source)

    def test_the_build_writes_no_bytecode_for_the_helper(self):
        # The release build runs `python -I tools/build_package.py`, and -I
        # ignores PYTHONDONTWRITEBYTECODE: importing the helper must still leave
        # no tools/__pycache__ behind (tools/evidence.py counts it as unclean).
        tools = self.root / "tools"
        tools.mkdir()
        for name in ("build_package.py", "windows_powershell.py"):
            (tools / name).write_bytes((ROOT / "tools" / name).read_bytes())
        env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONDONTWRITEBYTECODE"}
        r = subprocess.run([sys.executable, "-I", str(tools / "build_package.py"), "--help"],
                           capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertFalse((tools / "__pycache__").exists())

    @unittest.skipUnless(sys.platform == "win32", "runs Windows PowerShell")
    def test_a_real_child_sees_no_profiler_or_module_path_from_the_parent(self):
        planted = self.root / "mods"
        planted.mkdir()
        poison = {**POISON, "PSModulePath": str(planted)}
        with mock.patch.dict(os.environ, poison):
            r = wps.run("Get-ChildItem env: | ForEach-Object Name; '--'; $env:PSModulePath; "
                        "(Get-AuthenticodeSignature -LiteralPath $env:UNMINUTE_FILE)"
                        ".GetType().FullName",
                        {"UNMINUTE_FILE": os.path.join(wps.system_folder(), "cmd.exe")},
                        timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        names, _, rest = r.stdout.partition("--")
        names = names.split()
        module_path, kind = rest.strip().splitlines()[-2:]
        self.assertEqual(forbidden(names), [])
        self.assertNotIn("UNMINUTE_SYNTHETIC_PARENT", names)
        self.assertNotIn(str(planted).lower(), module_path.lower())
        self.assertNotIn("powershell\\7", module_path.lower())
        self.assertEqual(kind, "System.Management.Automation.Signature")


if __name__ == "__main__":
    unittest.main()
