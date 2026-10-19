# Windows PowerShell 5.1 (and netstat, taskkill and cmd) for the build and check tools and
# the tests (#237, #253): started by its absolute path in the system folder, never by name,
# with an allowlisted environment, so a .NET profiler, COMPlus_* or DOTNET_* setting or
# another module path in the parent never reaches it. Synthetic values only.
#   python tools/run_tests.py

import os
import re
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
import packaged_crash_check  # noqa: E402
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
# A system program started by name: its name first in an argument list, or at
# the start of a command line handed to subprocess or os (#237, #253).
SYSTEM_PROGRAMS = r"(?:powershell|netstat|taskkill|cmd)(?:\.exe)?"
BY_NAME = re.compile(r"""\[\s*[rRbBuU]?["']""" + SYSTEM_PROGRAMS + r"""["']"""
                     r"""|\b(?:run|Popen|call|check_call|check_output|system|popen)\(\s*"""
                     r"""[rRbBuUfF]?["']""" + SYSTEM_PROGRAMS + r"""[\s"']""", re.IGNORECASE)
# The same in a PowerShell tool, where PowerShell finds a bare name on PATH:
# Start-Process or the call operator with the name, or the name as a command
# of its own (at the start of a line, or after ; { ( | = && or ||).
PS_BY_NAME = re.compile(r"""(?:\bStart-Process(?:\s+-FilePath)?|&)\s+["']?"""
                        + SYSTEM_PROGRAMS + r"""(?=["'\s]|$)"""
                        r"""|(?:^|[;{(|=]|&&|\|\|)[ \t]*""" + SYSTEM_PROGRAMS
                        + r"""(?=[\s;|)}]|$)""", re.IGNORECASE | re.MULTILINE)
# PowerShell comments: <# #> help blocks, and # to the end of the line when the
# # starts a word (so a string such as '#32770' stays).
PS_COMMENTS = re.compile(r"<#.*?#>|(?:(?<=\s)|^)#[^\n]*", re.DOTALL | re.MULTILINE)


def ps_starts_by_name(text):
    """The starts of a system program by name in PowerShell source, comments
    left out (a usage line such as `powershell -File x.ps1` is not a start)."""
    return PS_BY_NAME.findall(PS_COMMENTS.sub("", text))


def forbidden(names):
    return [n for n in names if n.upper().startswith(FORBIDDEN)]


class FakeWindows:
    """A system folder in a temporary directory, with or without a
    powershell.exe, and subprocess.run replaced by a recorder."""

    def __init__(self, root, with_exe=True, with_netstat=True, with_taskkill=True):
        self.system = Path(root) / "Windows" / "System32"
        self.home = self.system / "WindowsPowerShell" / "v1.0"
        self.home.mkdir(parents=True)
        if with_exe:
            (self.home / "powershell.exe").write_bytes(b"")
        if with_netstat:
            (self.system / "NETSTAT.EXE").write_bytes(b"")
        if with_taskkill:
            (self.system / "taskkill.exe").write_bytes(b"")
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

    def test_netstat_is_started_by_its_full_path_with_the_clean_environment(self):
        # The window checks' listener check: a netstat.exe planted beside
        # Python or in the current folder must not run instead (and could hide
        # a listener), and the parent's .NET settings never reach the child.
        win = self.fake()
        decoy = self.root / "decoy"
        decoy.mkdir()
        (decoy / "netstat.exe").write_bytes(b"")
        first, second = win.patches()
        with first, second, mock.patch.dict(os.environ, {**POISON, "PATH": str(decoy)}):
            self.assertEqual(process_listeners.listeners({1: "x"}), [])
        (argv, kwargs), = win.calls
        self.assertEqual(argv, [str(win.system / "NETSTAT.EXE"), "-ano"])
        self.assertTrue(os.path.isabs(argv[0]))
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        env = kwargs.get("env")
        self.assertIsNotNone(env, "the child would inherit the whole environment")
        self.assertEqual(forbidden(env), [])
        self.assertNotIn("UNMINUTE_SYNTHETIC_PARENT", env)
        self.assertEqual(env["PATH"].split(os.pathsep),
                         [str(win.system), str(win.system / "Wbem"), str(win.home)])

    def test_a_missing_netstat_fails_clearly_and_runs_nothing(self):
        win = self.fake(with_netstat=False)
        first, second = win.patches()
        with first, second, self.assertRaisesRegex(
                FileNotFoundError, "NETSTAT.EXE is missing from the system folder"):
            process_listeners.listeners({1: "x"})
        self.assertEqual(win.calls, [])

    def assert_clean_child(self, win, kwargs):
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        env = kwargs.get("env")
        self.assertIsNotNone(env, "the child would inherit the whole environment")
        self.assertEqual(forbidden(env), [])
        self.assertNotIn("UNMINUTE_SYNTHETIC_PARENT", env)
        self.assertEqual(env["PATH"].split(os.pathsep),
                         [str(win.system), str(win.system / "Wbem"), str(win.home)])

    def test_taskkill_is_started_by_its_full_path_with_the_clean_environment(self):
        # The package checks' hard kill (#253): a taskkill.exe planted on PATH
        # or in the current folder must not run instead (and leave the app
        # running), and the parent's .NET settings never reach the child.
        win = self.fake()
        decoy = self.root / "decoy"
        decoy.mkdir()
        (decoy / "taskkill.exe").write_bytes(b"")
        taskkill = str(win.system / "taskkill.exe")
        ctx = {"py": "python.exe", "script": "child.py", "env": {}, "app": str(self.root)}
        child_process = mock.Mock(stdout=[], stderr=[], pid=4321)
        child_process.poll.return_value = None
        first, second = win.patches()
        with first, second, mock.patch.dict(os.environ, {**POISON, "PATH": str(decoy)}):
            packaged_check.kill(wps.system_program("taskkill.exe"), [11, 12])
            with mock.patch.object(packaged_crash_check.subprocess, "Popen",
                                   return_value=child_process):
                child = packaged_crash_check.Child(ctx, "run")
            self.assertTrue(child.kill())
        self.assertEqual([argv for argv, _ in win.calls],
                         [[taskkill, "/F", "/PID", "12"], [taskkill, "/F", "/PID", "11"],
                          [taskkill, "/F", "/T", "/PID", "4321"]])
        for _, kwargs in win.calls:
            self.assert_clean_child(win, kwargs)

    def test_a_missing_taskkill_stops_the_crash_check_before_its_child_starts(self):
        win = self.fake(with_taskkill=False)
        ctx = {"py": "python.exe", "script": "child.py", "env": {}, "app": str(self.root)}
        first, second = win.patches()
        with first, second, \
                mock.patch.object(packaged_crash_check.subprocess, "Popen") as popen, \
                self.assertRaisesRegex(FileNotFoundError,
                                       "taskkill.exe is missing from the system folder"):
            packaged_crash_check.Child(ctx, "run")
        popen.assert_not_called()
        self.assertEqual(win.calls, [])

    def test_the_import_dialog_check_finds_taskkill_before_the_app_starts(self):
        # As the two Python checks do: a missing taskkill.exe stops the check
        # before the app or its throwaway profile exists, never in the finally
        # block that should kill the app (#256). The script needs an installed
        # package and opens windows, so its source order is checked here.
        source = (ROOT / "tools" / "import_dialog_check.ps1").read_text(encoding="utf-8")
        resolve = source.index("$taskkill = Join-Path ([Environment]::SystemDirectory) "
                               "'taskkill.exe'")
        check = source.index("if (-not (Test-Path -LiteralPath $taskkill -PathType Leaf))")
        stop = source.index("throw 'taskkill.exe is missing from the system folder'")
        profile = source.index("$profileDir = ")
        app = source.index("$proc = Start-Process")
        self.assertLess(resolve, check)
        self.assertLess(check, stop)
        self.assertLess(stop, profile)
        self.assertLess(profile, app)
        self.assertEqual(source.count("$taskkill = "), 1)
        self.assertIn("Start-Process $taskkill -ArgumentList", source[app:])

    def test_the_by_name_pattern_finds_each_way_of_writing_a_start(self):
        # Each name is written <name> here and unwrapped below, so this file's
        # own source holds no start by name for the scan to find.
        for text in ('subprocess.run(["<taskkill>", "/F"])', "Popen(['<cmd>', '/c'])",
                     'run([\n    "<powershell>", "-NoProfile"])',
                     'run(["<NETSTAT.EXE>", "-ano"])', 'subprocess.run("<taskkill> /F /PID 1")',
                     "os.system('<cmd> /c mklink')", 'check_output(f"<powershell> -Command {x}")'):
            with self.subTest(text=text):
                self.assertTrue(BY_NAME.search(text.replace("<", "").replace(">", "")))
        for text in ('wps.system_program("<cmd.exe>")', 'shell(r"<cmd> /c dir /s x")',
                     '"<powershell.exe>"', 'run([wps.system_program("<taskkill.exe>"), "/F"])',
                     'run(["<cmdkey>", "/list"])'):
            with self.subTest(text=text):
                self.assertIsNone(BY_NAME.search(text.replace("<", "").replace(">", "")))
        # The PowerShell forms need no unwrapping: the Python scan does not
        # look for them.
        for text in ("Start-Process taskkill -ArgumentList '/F'", "& cmd.exe /c dir",
                     "Start-Process -FilePath 'powershell' -Wait", "x; & 'netstat' -ano",
                     # A bare statement: PowerShell runs it from PATH too (#256).
                     "    taskkill /F /T /PID $proc.Id", "cmd /c dir", "x; NETSTAT.EXE -ano",
                     "if ($x) { taskkill /F }", "$out = netstat -ano", "$(cmd /c ver)",
                     "'a' | powershell -Command -", "x && cmd", "<#\n  x\n#>\ntaskkill /F",
                     "Get-Date  # note\ncmd /c dir"):
            with self.subTest(text=text):
                self.assertTrue(ps_starts_by_name(text))
        for text in ("Start-Process $taskkill -ArgumentList '/F'", "& $cmd /c dir",
                     "#   powershell -File tools\\x.ps1", "Start-Process cmdkey",
                     "& (Join-Path $Version 'runtime\\python.exe') -c $probe",
                     # A help block, a trailing comment, another program, a
                     # variable, part of a longer name, a string, and a # in a
                     # string before a comment.
                     "<#\n.EXAMPLE\n  powershell -NoProfile -File x.ps1\n#>",
                     "Get-Date  # then taskkill /F", "cmdkey /list", "$cmd = 1",
                     "Invoke-cmdlet", "Write-Output 'taskkill /F'",
                     "New-Object X('#32770') # cmd /c"):
            with self.subTest(text=text):
                self.assertEqual(ps_starts_by_name(text), [])

    def test_no_tool_or_test_starts_a_system_program_by_name(self):
        # Every tool and test, so a start added later is caught too: Windows
        # PowerShell, netstat, taskkill and cmd are started by their full path
        # in the system folder (windows_powershell), never by name (#237, #253).
        # tests/test_agent_guard.py finds PowerShell with shutil.which on
        # purpose, the way Claude Code does, because it checks the hook as
        # Claude Code runs it; that lookup is not a start by name.
        sources = sorted((ROOT / "tools").glob("*.py")) + sorted((ROOT / "tests").glob("*.py"))
        self.assertIn(ROOT / "tools" / "packaged_crash_check.py", sources)
        for path in sources:
            with self.subTest(file=path.name):
                found = BY_NAME.findall(path.read_text(encoding="utf-8"))
                self.assertEqual(found, [])
        # The PowerShell tools too, such as the import dialog check's taskkill.
        scripts = sorted((ROOT / "tools").rglob("*.ps1"))
        self.assertIn(ROOT / "tools" / "import_dialog_check.ps1", scripts)
        self.assertIn(ROOT / "tools" / "intune" / "Get-VerifiedRelease.ps1", scripts)
        for path in scripts:
            with self.subTest(file=path.name):
                found = ps_starts_by_name(path.read_text(encoding="utf-8"))
                self.assertEqual(found, [])
        # The tools #237 moved never name powershell.exe on its own either.
        for tool in ("build_package.py", "packaged_check.py", "process_listeners.py",
                     "perf_check.py"):
            with self.subTest(tool=tool):
                source = (ROOT / "tools" / tool).read_text(encoding="utf-8")
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
