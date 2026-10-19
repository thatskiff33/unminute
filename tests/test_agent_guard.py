# tests/test_agent_guard.py
# The Claude Code guard rail in .claude/ (AGENTS.md, invariants P1 and P2): it
# blocks agent access to real meeting content and the staging of media files,
# and leaves ordinary work, temporary synthetic audio and the repo's own
# workspace/ package alone. Stdlib only.

import ast
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "private_data_guard.py"

spec = importlib.util.spec_from_file_location("private_data_guard", HOOK)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def tool(name, cwd=ROOT, **tool_input):
    return {"tool_name": name, "tool_input": tool_input, "cwd": str(cwd)}


def shell(command, name="Bash"):
    return tool(name, command=command)


@contextlib.contextmanager
def profile_guard():
    """The hook loaded afresh under a synthetic Windows profile, C:\\Users\\someone, so
    checks of the folders above local AppData run the same on Linux CI."""
    home = r"C:\Users\someone"
    env = {"LOCALAPPDATA": home + r"\AppData\Local", "USERPROFILE": home}
    with mock.patch.dict(os.environ, env):
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        yield fresh


class FileToolTests(unittest.TestCase):
    def test_extracted_folder_beside_agents_md_is_blocked(self):
        for payload in (tool("Read", file_path="extracted/meeting.md"),
                        tool("Read", file_path=str(ROOT / "extracted" / "meeting.md")),
                        tool("Edit", file_path=str(ROOT / "extracted" / "a" / "b.txt")),
                        tool("Glob", pattern="extracted/**"),
                        tool("Grep", pattern="anything", path="extracted")):
            with self.subTest(payload=payload):
                self.assertIsNotNone(guard.decide(payload))

    def test_live_workspace_is_blocked(self):
        local = os.environ.get("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
        for path in (local + r"\Unminute\workspace\workspace.db",
                     "C:/Users/someone/AppData/Local/Unminute/workspace/media/1/audio.f32",
                     # the old folder keeps real content until the user deletes it
                     local + r"\whisper\workspace\workspace.db",
                     "C:/Users/someone/AppData/Local/whisper/workspace/media/1/audio.f32",
                     local + r"\whisper\WORKSP~1\workspace.db",     # its 8.3 short name
                     # a damaged workspace renamed aside, as the guide says
                     local + r"\Unminute\workspace-damaged\workspace.db",
                     # device, long-path and network forms of the same folder
                     "\\\\?\\" + local + r"\Unminute\workspace\workspace.db",
                     r"\\localhost\C$\Users\someone\AppData\Local\Unminute\workspace\a.db",
                     "//localhost/C$/Users/someone/AppData/Local/Unminute/workspace/a.db",
                     # .. from another folder in local AppData
                     local + r"\Temp\..\Unminute\workspace\workspace.db"):
            with self.subTest(path=path):
                self.assertIsNotNone(guard.decide(tool("Read", file_path=path)))
        # Relative to the working folder, or Glob's pattern relative to its path;
        # and the data folder around the workspace, except its models.
        for payload in (tool("Read", cwd=local, file_path=r"Unminute\workspace\settings.json"),
                        tool("Read", file_path=local + r"\Unminute\models\..\workspace\a.db"),
                        tool("Read", cwd=local + r"\Unminute\models",
                             file_path=r"..\workspace\a.db"),
                        tool("Glob", path=local, pattern="Unminute/workspace/**"),
                        tool("Glob", cwd=local, pattern="Unminute/workspace/**/*.json"),
                        tool("Glob", path=local + r"\Unminute", pattern="**/*.json"),
                        tool("Read", cwd=local + r"\Temp", file_path=r"..\Unminute\workspace\a.db"),
                        # a wildcard that stands for the data folder
                        tool("Glob", path=local, pattern="Unmin*/workspace/*"),
                        tool("Glob", path=local, pattern="*/workspace/*"),
                        tool("Glob", path=local, pattern="**/*.json"),
                        tool("Grep", pattern="budget", path=local + r"\Unminute")):
            with self.subTest(payload=payload):
                self.assertIsNotNone(guard.decide(payload))
        self.assertIsNone(guard.decide(tool("Glob", path=local + r"\Unminute\models",
                                            pattern="**/*.json")))
        self.assertIsNone(guard.decide(tool("Read",
                                            file_path=local + r"\Unminute\models\a\..\b.json")))

    def test_unminute_workspace_is_blocked(self):
        # The renamed data folder (#136), in the forms the old one is checked in
        # above. The old folder stays covered: it keeps real content until deleted.
        local = os.environ.get("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
        for path in (local + r"\Unminute\workspace\workspace.db",
                     "C:/Users/someone/AppData/Local/Unminute/workspace/media/1/audio.f32",
                     r"C:\Users\someone\AppData\Local\unminute\WORKSPACE",
                     "~/AppData/Local/Unminute/workspace/settings.json",
                     r"C:\Users\someone\AppData\Local\Temp\..\Unminute\workspace\workspace.db",
                     # A "." segment before it (main blocks these for the old folder).
                     r".\Unminute\workspace\a.json",
                     "./Unminute/workspace/a.json",
                     r"C:\Users\s\AppData\Local\.\Unminute\workspace\workspace.db"):
            for payload in (tool("Read", file_path=path), tool("Edit", file_path=path),
                            tool("Glob", pattern="**/*", path=path),
                            tool("Grep", pattern="anything", path=path)):
                with self.subTest(payload=payload):
                    self.assertIsNotNone(guard.decide(payload))
        # A Glob pattern naming it after a wildcard, as main blocks for the old folder.
        self.assertIsNotNone(guard.decide(tool("Glob", pattern="**/Unminute/workspace/**",
                                               path=r"C:\Users\someone\AppData\Local")))


    def test_unminute_workspace_under_a_moved_local_appdata_is_blocked(self):
        # LOCALAPPDATA redirected to a folder not named Local: the pattern also
        # takes the configured value as a prefix.
        pattern = guard.unminute_workspace_pattern(r"D:\Profiles\someone\Data")
        for text in (r"D:\Profiles\someone\Data\Unminute\workspace\workspace.db",
                     "D:/Profiles/someone/Data/Unminute/workspace"):
            with self.subTest(text=text):
                self.assertIsNotNone(pattern.search(text))
        self.assertIsNone(pattern.search(r"D:\Profiles\someone\Code\unminute\workspace\app.py"))
        # An unset LOCALAPPDATA adds nothing, and the usual forms still match.
        self.assertIsNotNone(guard.unminute_workspace_pattern("").search(
            r"C:\Users\someone\AppData\Local\Unminute\workspace"))


    def test_a_checkout_named_unminute_is_allowed(self):
        # The repo is also called unminute and has a workspace/ package: working
        # in it must not look like the data folder.
        for payload in (tool("Read", file_path=r"C:\Users\x\Local_Repos\unminute"
                                               r"\workspace\app.py"),
                        tool("Edit", file_path="C:/Users/x/Local_Repos/unminute"
                                               "/workspace/store.py"),
                        tool("Read", file_path=r"C:\Users\x\source\repos\Unminute"
                                               r"\workspace\ui\app.js"),
                        tool("Read", file_path=r"C:\Users\x\AppData\Local\Temp\unminute"
                                               r"\workspace\app.py"),
                        tool("Glob", pattern="workspace/**/*.py",
                             path=r"C:\Users\x\Local_Repos\unminute"),
                        tool("Grep", pattern="Unminute/workspace", path="."),
                        tool("Grep", pattern="x",
                             path=r"C:\Users\x\Local_Repos\unminute\workspace")):
            with self.subTest(payload=payload):
                self.assertIsNone(guard.decide(payload))

    def test_a_redirected_profile_is_recognised(self):
        # LOCALAPPDATA need not end in AppData\Local (a VDI profile on another drive).
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"D:\Profiles\someone\Local"}):
            redirected = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(redirected)
        for path in (r"D:\Profiles\someone\Local\Unminute\workspace\workspace.db",
                     "D:/Profiles/someone/Local/Unminute/workspace/settings.json"):
            with self.subTest(path=path):
                self.assertIsNotNone(redirected.decide(tool("Read", file_path=path)))
        self.assertIsNone(redirected.decide(
            tool("Read", file_path=r"D:\Profiles\someone\Local\Unminute\models\a.json")))

    def test_a_search_above_the_data_folder_is_blocked(self):
        # Grep searches its folder whole, and Glob's ** does too.
        local = r"C:\Users\someone\AppData\Local"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": local}):
            fresh = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fresh)
        for payload in (tool("Grep", pattern="budget", path=local),
                        tool("Grep", pattern="budget", path=local + "\\"),
                        tool("Grep", pattern="budget", path=r"C:\Users\someone"),
                        tool("Grep", pattern="budget", cwd=local),
                        tool("Grep", pattern="budget", cwd=r"C:\Users\someone", path="AppData"),
                        tool("Glob", path=r"C:\Users\someone", pattern="**/*.db")):
            with self.subTest(payload=payload):
                self.assertIsNotNone(fresh.decide(payload))
        for payload in (tool("Grep", pattern="budget", path=local + r"\Temp\review"),
                        tool("Grep", pattern="budget", path=local + r"\Unminute\models"),
                        tool("Grep", pattern="budget", path=r"C:\Users\someone\Local_Repos"),
                        tool("Grep", pattern="budget", cwd=r"C:\Users\someone\Repos\unminute"),
                        tool("Glob", path=r"C:\Users\someone", pattern="*.txt")):
            with self.subTest(payload=payload):
                self.assertIsNone(fresh.decide(payload))

    def test_a_home_folder_written_as_tilde_is_expanded(self):
        # Grep and Glob read a leading ~ as the home folder, which holds local
        # AppData (R13b round 2 review).
        home = r"C:\Users\someone"
        env = {"LOCALAPPDATA": home + r"\AppData\Local", "USERPROFILE": home}
        with mock.patch.dict(os.environ, env):
            fresh = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fresh)
            for payload in (tool("Grep", pattern="x", path="~"),
                            tool("Grep", pattern="x", path="~/"),
                            tool("Grep", pattern="x", path="~\\"),
                            tool("Grep", pattern="x", path="~/AppData"),
                            tool("Glob", path="~", pattern="**/*.json"),
                            tool("Glob", path="~", pattern="**/workspace/*.json"),
                            tool("Glob", pattern="~/**/settings.json"),
                            tool("Bash", command="cd ~; rg -n x"),
                            tool("PowerShell", command=r"Get-ChildItem -Recurse C:\*.json"),
                            tool("Bash", cwd=home, command="grep -rn x"),
                            tool("PowerShell",
                                 command="cd ~; Get-ChildItem -Recurse -Filter *.json")):
                with self.subTest(payload=payload):
                    self.assertIsNotNone(fresh.decide(payload))
            for payload in (tool("Grep", pattern="x", path="~/Local_Repos/unminute"),
                            tool("Glob", path="~", pattern=".claude/**/*.json"),
                            tool("Glob", pattern="~/Local_Repos/unminute/**/*.py"),
                            tool("Bash", command="cd ~/Local_Repos/unminute; rg -n x")):
                with self.subTest(payload=payload):
                    self.assertIsNone(fresh.decide(payload))

    def test_a_git_bash_drive_path_is_read_as_the_drive(self):
        # Git Bash spells C:\Users as /c/Users, and the Grep and Glob tools accept
        # it; a bare cd in bash goes home (R13b round 3 review).
        home = r"C:\Users\someone"
        env = {"LOCALAPPDATA": home + r"\AppData\Local", "USERPROFILE": home}
        with mock.patch.dict(os.environ, env):
            fresh = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fresh)
            for payload in (tool("Grep", pattern="x", path="/c/Users"),
                            tool("Grep", pattern="x", path="/c/Users/someone"),
                            tool("Grep", pattern="x", path="/mnt/c/Users/someone"),
                            tool("Glob", path="/c/Users/someone", pattern="**/settings.json"),
                            tool("Glob", pattern="/c/Users/someone/**/settings.json"),
                            tool("Read", file_path="/c/Users/someone/AppData/Local/Unminute"
                                                   "/workspace/settings.json"),
                            tool("Bash", command="grep -rn x /c/Users"),
                            tool("Bash", command="grep -rn x /c/Users/someone"),
                            tool("Bash", command="find /c/Users/someone -name settings.json"),
                            tool("Bash", command="cd /c/Users/someone && rg -n x"),
                            tool("Bash", command="cd /c/Users/someone/AppData; cat Local/Unminute"
                                                 "/workspace/a.json"),
                            tool("Bash", command="cd; rg -n x"),
                            tool("Bash", command="cd && grep -rn x .")):
                with self.subTest(payload=payload):
                    self.assertIsNotNone(fresh.decide(payload))
            for payload in (tool("Grep", pattern="x", path="/c/Users/someone/Local_Repos/unminute"),
                            tool("Glob", path="/c/Users/someone/Local_Repos", pattern="**/*.py"),
                            tool("Bash", command="cd /c/Users/someone/Local_Repos/unminute && "
                                                 "rg -n x"),
                            tool("Bash", command="cd; cd Local_Repos/unminute; rg -n x"),
                            tool("Bash", command="ls /c/Users/someone"),
                            tool("PowerShell", command=r"dir /s /c workspace")):
                with self.subTest(payload=payload):
                    self.assertIsNone(fresh.decide(payload))

    @unittest.skipUnless(sys.platform == "win32", "Windows paths")
    def test_a_recursive_listing_of_an_existing_folder_lists_only_that_folder(self):
        # A listing's last part is a filter only when it is a wildcard or not an
        # existing folder, and Git Bash's ls takes none; its -r only reverses the
        # order (R13b round 3 review). A synthetic profile of real folders.
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "profile"
            local = home / "AppData" / "Local"
            for folder in (local / "Temp", local / "Unminute" / "workspace", home / ".claude",
                           home / "Local_Repos", home / ".umv", home / "Documents",
                           home / "AppData" / "Roaming"):
                folder.mkdir(parents=True)
            env = {"LOCALAPPDATA": str(local), "USERPROFILE": str(home),
                   "APPDATA": str(home / "AppData" / "Roaming"), "TEMP": str(local / "Temp")}
            with mock.patch.dict(os.environ, env):
                fresh = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(fresh)
                for command, name in (
                        ("ls -R ~/.claude", "Bash"),
                        ("ls -r ~/.claude", "Bash"),
                        ("ls -R ~/Local_Repos", "Bash"),
                        ("ls -R /tmp", "Bash"),
                        (r"Get-ChildItem -Recurse $env:TEMP -Filter *.log", "PowerShell"),
                        (r"Get-ChildItem $env:TEMP -Recurse -Directory -Filter um*", "PowerShell"),
                        (r"Get-ChildItem -Recurse $env:TEMP", "PowerShell"),
                        (r"dir /s /b %TEMP%", "PowerShell"),
                        (r"dir /s /b %APPDATA%", "PowerShell"),
                        (r"Get-ChildItem -Recurse ~\.claude", "PowerShell"),
                        (r"Get-ChildItem -Recurse ~\Documents -Filter *.md", "PowerShell"),
                        (r"Get-ChildItem -Recurse $env:USERPROFILE\.claude -Filter *.md",
                         "PowerShell"),
                        (f"Get-ChildItem -Recurse {home}\\Local_Repos -Filter AGENTS.md",
                         "PowerShell"),
                        (f"Get-ChildItem -Recurse -Depth 1 {home}\\.umv", "PowerShell")):
                    with self.subTest(command=command):
                        self.assertIsNone(fresh.decide(shell(command, name)))
                # A file name, or a folder that is not there, is still a filter.
                for command, name in (
                        (r"Get-ChildItem $env:LOCALAPPDATA\settings.json -Recurse", "PowerShell"),
                        (r"Get-ChildItem -Recurse ~\nosuch", "PowerShell"),
                        (r"cmd /c dir /s %LOCALAPPDATA%\settings.json", "Bash"),
                        ("ls -R ~/AppData/Local", "Bash")):
                    with self.subTest(command=command):
                        self.assertIsNotNone(fresh.decide(shell(command, name)))

    def test_a_long_brace_list_cannot_hide_a_data_folder(self):
        # Past the expansion limit, a brace list stands for anything (R13b round 2).
        local = r"C:\Users\someone\AppData\Local"
        names = ",".join(["Unminute"] + [f"a{i}" for i in range(70)])
        self.assertIsNotNone(guard.decide(tool("Glob", path=local,
                                               pattern="{" + names + "}/workspace/*")))
        self.assertIsNone(guard.decide(tool("Glob", path=str(ROOT),
                                            pattern="{" + names + "}/workspace/*")))

    def test_the_old_data_folder_is_blocked_whole(self):
        # %LOCALAPPDATA%\whisper holds the old workspace's real meetings until the
        # user deletes it; its whisper-models sibling is only models (R13b review).
        local = r"C:\Users\someone\AppData\Local"
        for payload in (tool("Glob", path=local + r"\whisper", pattern="work*/*.json"),
                        tool("Glob", path=local, pattern="whisper/*/*.json"),
                        tool("Glob", path=local, pattern="w*/workspace/*"),
                        tool("Grep", pattern="budget", path=local + r"\whisper"),
                        tool("Read", file_path=local + r"\whisper\settings.json")):
            with self.subTest(payload=payload):
                self.assertIsNotNone(guard.decide(payload))
        for payload in (tool("Read", file_path=local + r"\whisper-models\a.json"),
                        tool("Glob", path=local + r"\whisper-models", pattern="**/*.json")):
            with self.subTest(payload=payload):
                self.assertIsNone(guard.decide(payload))

    def test_other_spellings_of_local_appdata_are_seen(self):
        # Windows opens all of these as local AppData (R13b review): device and
        # admin-share forms, the "Local Settings" junction, trailing dots, a folder
        # named AppData, and a wildcard or brace list standing for a part.
        local = r"C:\Users\someone\AppData\Local"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": local}):
            fresh = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fresh)
        for payload in (tool("Grep", pattern=".", path="\\\\?\\" + local),
                        tool("Grep", pattern=".",
                             path=r"\\localhost\C$\Users\someone\AppData\Local"),
                        tool("Grep", pattern=".", path="\\\\?\\UNC\\localhost\\C$\\Users\\someone"),
                        tool("Grep", pattern=".", path=r"C:\Users\someone\Local Settings"),
                        tool("Grep", pattern=".", path=local + "."),
                        tool("Grep", pattern=".", path=r"D:\Profiles\other\AppData"),
                        tool("Read", file_path=r"C:\Users\someone\Local Settings\Unminute"
                                               r"\workspace\settings.json"),
                        tool("Read", file_path=r"C:\Users\x\AppData\Local.\Unminute"
                                               r"\workspace\a.db"),
                        tool("Read", file_path=r"C:\Users\x\AppData.\Local\Unminute"
                                               r"\workspace\a.db"),
                        tool("Read", file_path=local + r"\whisper.\workspace\a.db"),
                        tool("Read", file_path=local + r"\Unminute.\workspace\a.db"),
                        tool("Glob", path=r"C:\Users\someone",
                             pattern="AppData/*/Unminute/workspace/*"),
                        tool("Glob", path=local, pattern="{Unminute,zz}/workspace/*.json"),
                        tool("Glob", path=local, pattern="{Unminute/workspace,zz}/*.json")):
            with self.subTest(payload=payload):
                self.assertIsNotNone(fresh.decide(payload))
        # A Glob whose literal start cannot reach AppData stays allowed.
        for payload in (tool("Glob", path=r"C:\Users\someone", pattern=".claude/**/*.json"),
                        tool("Glob", path=r"C:\Users\someone", pattern="Local_Repos/**/AGENTS.md"),
                        tool("Glob", path=local, pattern="*/models/*.json"),
                        tool("Glob", path=local, pattern="{Temp,Programs}/x/*.json"),
                        tool("Grep", pattern=".", path="\\\\?\\" + local + r"\Temp\review")):
            with self.subTest(payload=payload):
                self.assertIsNone(fresh.decide(payload))

    @unittest.skipUnless(sys.platform == "win32", "junctions are a Windows feature")
    def test_a_junction_or_short_name_is_resolved(self):
        # A synthetic profile in a temporary folder, with a "Local Settings" junction
        # to its local AppData as Windows makes one, and its 8.3 short name.
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "profile-with-a-long-name"
            local = home / "AppData" / "Local"
            (local / "Unminute" / "workspace").mkdir(parents=True)
            (local / "Temp").mkdir()
            (local / "Unminute" / "workspace" / "settings.json").write_text("{}")
            junction = home / "Local Settings"
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(local)],
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(0, made.returncode, made.stdout + made.stderr)
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
                fresh = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(fresh)
            for payload in (tool("Read", file_path=str(junction / "Unminute" / "workspace"
                                                       / "settings.json")),
                            tool("Grep", pattern=".", path=str(junction)),
                            tool("Grep", pattern=".", path=str(home)),
                            tool("Glob", path=str(junction), pattern="*/workspace/*"),
                            tool("PowerShell", command=f"Get-Content '{junction}\\Unminute"
                                                       "\\workspace\\settings.json'")):
                with self.subTest(payload=payload):
                    self.assertIsNotNone(fresh.decide(payload))
            self.assertIsNone(fresh.decide(tool("Grep", pattern=".", path=str(junction / "Temp"))))
            # The short name, where the volume makes them (CI's may not).
            import ctypes
            buffer = ctypes.create_unicode_buffer(1024)
            ctypes.windll.kernel32.GetShortPathNameW(str(home), buffer, 1024)
            if buffer.value and buffer.value.lower() != str(home).lower():
                self.assertIsNotNone(fresh.decide(tool("Grep", pattern=".", path=buffer.value)))
                self.assertIsNotNone(fresh.decide(tool("Glob", path=buffer.value,
                                                       pattern="**/*.json")))

    def test_ordinary_paths_are_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            unrelated = Path(temp) / "extracted" / "build.txt"     # no AGENTS.md beside it
            for payload in (tool("Read", file_path=str(ROOT / "workspace" / "store.py")),
                            tool("Read", file_path="workspace/app.py"),
                            tool("Write", file_path=str(Path(temp) / "probe.wav")),
                            tool("Read", file_path=str(unrelated)),
                            tool("Read", file_path=r"C:\Users\x\Local_Repos\unminute-r7"
                                                   r"\workspace\app.py"),
                            # a plain clone of this repository: unminute\workspace
                            tool("Read", file_path=r"C:\Users\x\Local_Repos\unminute"
                                                   r"\workspace\app.py"),
                            # ...and one in a temporary folder, which is under local AppData
                            tool("Read", file_path=r"C:\Users\x\AppData\Local\Temp\unminute"
                                                   r"\workspace\app.py"),
                            tool("Read", cwd=r"C:\Users\x\AppData\Local\Temp\review",
                                 file_path=r"..\unminute\workspace\app.py"),
                            tool("Glob", path=str(ROOT), pattern="workspace/**/*.py"),
                            tool("Grep", pattern="extracted/", path="."),
                            tool("Read", file_path=str(ROOT / "AGENTS.md"))):
                with self.subTest(payload=payload):
                    self.assertIsNone(guard.decide(payload))


class ShellTests(unittest.TestCase):
    def test_commands_naming_private_content_are_blocked(self):
        for command in ("cat extracted/notes.md",
                        r"type extracted\notes.md",
                        "ls extracted",
                        "git add -f extracted",
                        r"Get-Content $env:LOCALAPPDATA\whisper\workspace\workspace.db",
                        r"Get-Content $env:LOCALAPPDATA\Unminute\workspace\workspace.db",
                        r'Get-Content "%LOCALAPPDATA%\Unminute\workspace\settings.json"',
                        "sqlite3 ~/AppData/Local/Unminute/workspace/workspace.db .dump",
                        r'Get-Content "$env:LOCALAPPDATA\whisper\workspace\settings.json"',
                        "sqlite3 ~/AppData/Local/whisper/workspace/workspace.db .dump",
                        "rg -n budget extracted/",
                        'grep -r "x" "extracted/"',
                        r"Select-String -Pattern 'x' -Path extracted\notes.md",
                        "python tools/run_tests.py && cat extracted/a.md",
                        "git diff --no-index extracted/a.md b.md",
                        # the new path built in PowerShell or Python (R13b review)
                        r"Get-Content (Join-Path $env:LOCALAPPDATA 'Unminute\workspace\a.json')",
                        r'Test-Path (Join-Path $env:LOCALAPPDATA "Unminute\workspace\a.db")',
                        r"Join-Path $env:LOCALAPPDATA Unminute workspace",
                        r'Get-Content "$($env:LOCALAPPDATA)\Unminute\workspace\settings.json"',
                        r"Get-Content ($env:LOCALAPPDATA + '\Unminute\workspace\settings.json')",
                        r"Get-Content ([Environment]::GetFolderPath('LocalApplicationData')"
                        r" + '\Unminute\workspace\settings.json')",
                        "python -c \"import os; print(open(os.path.join(os.environ['LOCALAPPDATA'],"
                        " 'Unminute', 'workspace', 'settings.json')).read())\"",
                        "python -c \"os.listdir(os.path.join(x, 'whisper', 'workspace'))\"",
                        r"Get-ChildItem $env:LOCALAPPDATA\Unminute\workspace-damaged",
                        # ...or the data folder around it, searched or listed whole
                        r"rg -n budget $env:LOCALAPPDATA\Unminute",
                        "Get-ChildItem -Recurse $env:LOCALAPPDATA\\Unminute\\",
                        'grep -rn budget "$LOCALAPPDATA"/Unminute',
                        # ...or named after changing to local AppData
                        r"Set-Location $env:LOCALAPPDATA; Get-Content Unminute\workspace\a.json",
                        'cd "$LOCALAPPDATA" && cat Unminute/workspace/settings.json',
                        r"pushd %LOCALAPPDATA% & type Unminute\workspace\settings.json",
                        # ...or reached through models
                        r"Get-Content $env:LOCALAPPDATA\Unminute\models\..\workspace\a.json",
                        r"cd $env:LOCALAPPDATA\Unminute\models; Get-Content ..\workspace\a.json",
                        r"git -C $env:LOCALAPPDATA\Unminute\models diff --no-index ..\workspace\a",
                        r"Get-Content $env:LOCALAPPDATA\whisper\models\..\workspace\a.json",
                        # ...or through another folder, or another name for local AppData
                        r"Get-Content $env:LOCALAPPDATA\Temp\..\Unminute\workspace\a.json",
                        r"Get-Content $env:TEMP\..\Unminute\workspace\a.json",
                        r"type %TEMP%\..\Unminute\workspace\a.json",
                        "cat /tmp/../Unminute/workspace/a.json",
                        r"Get-Content $env:APPDATA\..\Local\Unminute\workspace\a.json",
                        "python -c \"print(Path.home() / 'AppData' / 'Local' / 'Unminute' / 'x')\"",
                        r"Get-Content \\?\C:\Users\someone\AppData\Local\Unminute\workspace\a",
                        r"Get-Content \\localhost\C$\Users\x\AppData\Local\Unminute\workspace",
                        # ...or a wildcard standing for the data folder
                        r"Get-Content $env:LOCALAPPDATA\Unmin*\workspace\a.json",
                        r"Get-ChildItem -Recurse $env:LOCALAPPDATA\U*",
                        # ...or reached by changing folder, and back
                        r"cd $env:LOCALAPPDATA\Temp; cd ..; Get-Content Unminute\workspace\a.json",
                        r"cd $env:LOCALAPPDATA\Unminute\models; cd ..; Get-Content workspace\a",
                        r"cd $env:LOCALAPPDATA; cd Temp; cd -; Get-Content Unminute\workspace\a",
                        r"pushd $env:LOCALAPPDATA; pushd Temp; popd; cat Unminute\workspace\a",
                        r'cmd /c "cd /d %LOCALAPPDATA% && type Unminute\workspace\a.json"',
                        # ...or by an option that reads a file
                        r'git commit -F "%LOCALAPPDATA%\Unminute\workspace\a.txt"',
                        r'gh api repos/o/r/issues -F "body=@%LOCALAPPDATA%\Unminute\workspace\a"',
                        r"gh api repos/o/r --field body=@$env:LOCALAPPDATA\Unminute\workspace",
                        # the old data folder, whole (R13b review)
                        r"Get-Content $env:LOCALAPPDATA\whisper\works*\settings.json",
                        r"Get-Content $env:LOCALAPPDATA\whisper\*\settings.json",
                        "cat ~/AppData/Local/whisper/w*/settings.json",
                        r"rg -n budget $env:LOCALAPPDATA\whisper",
                        r"Get-ChildItem -Recurse $env:LOCALAPPDATA\whisper",
                        # trailing dots, which Windows drops
                        r"Get-Content $env:LOCALAPPDATA.\Unminute\workspace\a.json",
                        r"Get-Content $env:LOCALAPPDATA\whisper.\workspace\a.json",
                        r"Get-Content C:\Users\x\AppData\Local.\Unminute\workspace\a.json",
                        r'Get-Content "$env:USERPROFILE\Local Settings\Unminute\workspace\a.json"',
                        # split across arguments, a pipeline or a string prefix
                        r"Get-Content (Join-Path $env:LOCALAPPDATA '.\Unminute\workspace\a.json')",
                        r"$env:LOCALAPPDATA | Join-Path -ChildPath Unminute\workspace\a.json"
                        r" | Get-Content",
                        r"Join-Path -Path $env:LOCALAPPDATA -ChildPath Unminute\workspace\a.json",
                        r"Get-Content (Join-Path $env:LOCALAPPDATA\Unminute\models ..\workspace\a)",
                        "python -c \"open(os.path.join(os.environ['LOCALAPPDATA'], 'Unminute',"
                        " 'models', '..', 'workspace', 'a'))\"",
                        "python -c \"open(os.environ['LOCALAPPDATA']"
                        "+r'\\Unminute\\workspace\\a')\"",
                        # .. straight after local AppData
                        r"Get-Content $env:LOCALAPPDATA\..\Local\Unminute\workspace\a.json",
                        r"type %LOCALAPPDATA%\..\Local\Unminute\workspace\a.json",
                        r"Get-Content $env:TEMP\..\..\Local\Unminute\workspace\a.json",
                        # a shell search or recursive listing of local AppData whole
                        r"rg -n budget $env:LOCALAPPDATA",
                        r"Get-ChildItem -Recurse $env:LOCALAPPDATA -Filter *.json",
                        "grep -rn budget ~/AppData/Local",
                        r"dir /s %LOCALAPPDATA%",
                        # a search with no pattern argument, or the path as -Path's value
                        r"rg --files $env:LOCALAPPDATA\Unminute\workspace",
                        r"Select-String -Path $env:LOCALAPPDATA\Unminute\workspace\a.json 'x'",
                        # ...whose last part is a wildcard or a file name to look for
                        # below, through cmd /c, or git grep outside a repository
                        # (R13b round 2 review)
                        r"findstr /s /i budget %LOCALAPPDATA%\*.json",
                        r"Get-ChildItem -Recurse $env:LOCALAPPDATA\*.json | Select-String budget",
                        r"Get-ChildItem -Recurse -Path $env:LOCALAPPDATA\*.json | Select-String b",
                        r"Get-ChildItem $env:LOCALAPPDATA\settings.json -Recurse | Get-Content",
                        r"dir /s %LOCALAPPDATA%\*.json",
                        r"cmd /c dir /s /b %LOCALAPPDATA%",
                        r'cmd /c "findstr /s budget %LOCALAPPDATA%\*.json"',
                        r"git -C $env:LOCALAPPDATA grep --no-index -n budget",
                        # ...or that names no folder, run from local AppData
                        r"cd $env:TEMP; cd ..; rg -n budget",
                        r"cd $env:LOCALAPPDATA; rg -n budget",
                        r"cd $env:TEMP; cd ..; Get-ChildItem -Recurse -Filter *.json",
                        r"Set-Location $env:LOCALAPPDATA; Get-ChildItem -Recurse -Include *.db",
                        r"cd $env:LOCALAPPDATA; findstr /s budget *.json",
                        # a pattern given attached, with a colon or from a file, so
                        # the path is not taken for it (R13b round 3 review)
                        "grep --file=pats.txt ~/AppData/Local/Unminute/workspace/settings.json",
                        "grep -ebudget ~/AppData/Local/Unminute/workspace/settings.json",
                        "rg -ebudget ~/AppData/Local/Unminute/workspace/settings.json",
                        "Select-String -Pattern:budget C:\\Users\\someone\\AppData\\Local"
                        "\\Unminute\\workspace\\settings.json",
                        "grep --file=pats.txt -rn ~/AppData/Local",
                        "grep -fpats.txt -rn ~/AppData/Local"):
            for name in ("Bash", "PowerShell"):
                with self.subTest(command=command, tool=name):
                    self.assertIsNotNone(guard.decide(shell(command, name)))

    def test_commands_naming_the_unminute_workspace_are_blocked(self):
        # The renamed data folder (#136), in the forms the old one is checked in
        # above, and the usual ways of building it from LOCALAPPDATA.
        for command in (r"Get-Content $env:LOCALAPPDATA\Unminute\workspace\workspace.db",
                        r'Get-Content "$env:LOCALAPPDATA\Unminute\workspace\settings.json"',
                        "sqlite3 ~/AppData/Local/Unminute/workspace/workspace.db .dump",
                        r"type %LOCALAPPDATA%\Unminute\workspace\settings.json",
                        'ls "${LOCALAPPDATA}/Unminute/workspace"',
                        "cat /c/Users/someone/AppData/Local/unminute/WORKSPACE/settings.json",
                        r"Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Unminute\workspace')",
                        r"Get-Content ([Environment]::GetFolderPath('LocalApplicationData')"
                        r" + '\Unminute\workspace\settings.json')",
                        "rg -n budget ~/AppData/Local/Unminute/workspace",
                        # Main's old pattern blocks any folder before whisper\workspace;
                        # the new one takes any variable or substitution there.
                        "d=$LOCALAPPDATA; cat $d/Unminute/workspace/settings.json",
                        "cat \"${d}\"/Unminute/workspace/settings.json",
                        'cat "$(cygpath -u "$LOCALAPPDATA")/Unminute/workspace/settings.json"',
                        'cat "`cygpath -u $LOCALAPPDATA`/Unminute/workspace/settings.json"',
                        "python -c \"print(open(f'{base}/Unminute/workspace/a.json').read())\"",
                        r"Get-Content LOCALAPPDATA\Unminute\workspace\settings.json",
                        r"Get-Content $env:TEMP\..\Unminute\workspace\settings.json",
                        "python tools/run_tests.py && cat ~/AppData/Local/Unminute/workspace/a",
                        # A "." segment or a * wildcard straight before it: main blocks
                        # these forms for the old folder.
                        r"cd $env:LOCALAPPDATA; Get-Content .\Unminute\workspace\a.json",
                        r"Push-Location $env:LOCALAPPDATA; gc .\Unminute\workspace\a",
                        "cd ~/AppData/Local && cat ./Unminute/workspace/a.json",
                        "cat ~/AppData/Local/./Unminute/workspace/a",
                        r"Get-ChildItem -Path $env:LOCALAPPDATA -Recurse"
                        r" | ? FullName -like '*Unminute\workspace*'",
                        # A bound parameter (-Path:), a redirect, a ? wildcard, a
                        # drive-relative path or a cmd substring expansion before it:
                        # main blocks these forms for the old folder too.
                        r"cd $env:LOCALAPPDATA; Get-Content -Path:.\Unminute\workspace\a.json",
                        r"cd $env:LOCALAPPDATA; gci -LiteralPath:Unminute\workspace -Recurse",
                        r"Join-Path -Path $env:LOCALAPPDATA -ChildPath:Unminute\workspace",
                        r"cd $env:LOCALAPPDATA; type C:Unminute\workspace\a",
                        "cd ~/AppData/Local && wc -l <Unminute/workspace/a",
                        "ls ~/AppData/Loca?/Unminute/workspace",
                        r"type %LOCALAPPDATA:~0%\Unminute\workspace\a"):
            for name in ("Bash", "PowerShell"):
                with self.subTest(command=command, tool=name):
                    self.assertIsNotNone(guard.decide(shell(command, name)))


    def test_commands_in_a_checkout_named_unminute_are_allowed(self):
        for command in ("ls ../unminute/workspace",
                        r"Get-Content -Path:C:\Users\x\Local_Repos\unminute\workspace\api.py",
                        "git show HEAD:workspace/api.py",
                        r"cd $env:TEMP\unminute; ruff check workspace",
                        "ruff check C:/Users/x/Local_Repos/unminute/workspace",
                        "ruff check $HOME/Local_Repos/unminute/workspace",
                        r"cd $env:USERPROFILE\Local_Repos\unminute; ruff check workspace",
                        r"python -m py_compile C:\Users\x\Local_Repos\unminute\workspace\store.py",
                        "cd /c/Users/x/Local_Repos/unminute && git diff -- workspace/store.py",
                        r"git -C C:\Users\x\Local_Repos\unminute log --oneline -- workspace",
                        r'rg -n "Unminute\\workspace" workspace',
                        r'gh pr create --title "G" --body "P2: %LOCALAPPDATA%\Unminute\workspace"',
                        r'git commit -m "Guard %LOCALAPPDATA%\Unminute\workspace too"'):
            for name in ("Bash", "PowerShell"):
                with self.subTest(command=command, tool=name):
                    self.assertIsNone(guard.decide(shell(command, name)))

    def test_a_relative_workspace_path_from_local_appdata_is_blocked(self):
        local = r"C:\Users\someone\AppData\Local"
        for cwd in (local, local + "\\"):
            payload = tool("PowerShell", cwd=cwd, command=r"Get-Content Unminute\workspace\a.json")
            with self.subTest(cwd=cwd):
                self.assertIsNotNone(guard.decide(payload))
        # From models, .. climbs to the workspace.
        self.assertIsNotNone(guard.decide(tool("PowerShell", cwd=local + r"\Unminute\models",
                                               command=r"Get-Content ..\workspace\a.json")))
        self.assertIsNone(guard.decide(tool("PowerShell", cwd=local + r"\Unminute\models",
                                            command="Get-ChildItem .")))
        # Run from inside the workspace, any command can read it.
        self.assertIsNotNone(guard.decide(tool("PowerShell", cwd=local + r"\Unminute\workspace",
                                               command="Get-Content settings.json")))
        # A search that names no folder searches local AppData itself (R13b round 2).
        for cwd in (local, local + "\\"):
            for command in ("rg -n budget", "Get-ChildItem -Recurse -Filter *.json"):
                with self.subTest(cwd=cwd, command=command):
                    self.assertIsNotNone(guard.decide(tool("PowerShell", cwd=cwd,
                                                           command=command)))
        self.assertIsNone(guard.decide(tool("PowerShell", cwd=local + r"\Temp\review",
                                            command="rg -n budget")))
        # From another folder in local AppData, .. climbs to it.
        for command in (r"Get-Content ..\Unminute\workspace\a.json",
                        r"cd ..; Get-Content Unminute\workspace\a.json"):
            with self.subTest(command=command):
                self.assertIsNotNone(guard.decide(tool("PowerShell", cwd=local + r"\Temp",
                                                       command=command)))
        # Elsewhere, a clone of this repository named with a folder before it, or
        # one climb, is allowed.
        for cwd in (r"C:\Users\x\Local_Repos", local + r"\Temp\review"):
            payload = tool("PowerShell", cwd=cwd,
                           command=r"Get-Content C:\Users\x\Local_Repos\unminute\workspace\app.py")
            with self.subTest(cwd=cwd):
                self.assertIsNone(guard.decide(payload))
        self.assertIsNone(guard.decide(tool("PowerShell", cwd=local + r"\Temp\s\other",
                                            command=r"Get-Content ..\unminute\workspace\app.py")))
        # A relative unminute\workspace at the start of a path is blocked wherever
        # it is run, as main's check does (#221: kept, a known false positive).
        for cwd, command in ((r"C:\Users\x\Local_Repos", r"Get-Content unminute\workspace\app.py"),
                             (local + r"\Temp\s\other",
                              r"cd ..; Get-Content unminute\workspace\app.py")):
            with self.subTest(command=command):
                self.assertIsNotNone(guard.decide(tool("PowerShell", cwd=cwd, command=command)))

    def test_staging_media_is_blocked(self):
        for command in ("git add meeting.m4a", "git add docs/a.WAV tests/x.py",
                        'git commit -m "x" "Board meeting.mp4"'):
            with self.subTest(command=command):
                self.assertIsNotNone(guard.decide(shell(command)))

    def test_ordinary_commands_are_allowed(self):
        for command in ("python tools/run_tests.py --strict-skips",
                        r"runtime\python.exe -m workspace --status",
                        "git add AGENTS.md tests/test_agent_guard.py",
                        'git commit -m "Docs: AGENTS.md"',
                        r"python make_audio.py %TEMP%\probe.wav",
                        "python tools/ui_idle_check.py",
                        "grep -n extracted .gitignore",
                        "ls ../unminute-r7/workspace",
                        "ls ../unminute/workspace",
                        r"Get-Content C:\Users\x\AppData\Local\Temp\unminute\workspace\app.py",
                        r"cd $env:LOCALAPPDATA\Temp; ls unminute-r7\workspace",
                        r"cd $env:LOCALAPPDATA; cd Temp\review; Get-Content unminute\models\a",
                        r"gh api repos/o/r/issues/1 --raw-field body='%LOCALAPPDATA%\Unminute'",
                        r"Set-Content -Path notes.md -Value '%LOCALAPPDATA%\Unminute\workspace'",
                        r"Get-ChildItem $env:LOCALAPPDATA\Unminute\models",
                        r"Get-ChildItem $env:LOCALAPPDATA\Programs\Unminute",
                        r"Test-Path $env:LOCALAPPDATA\Unminute2",
                        r"cd $env:LOCALAPPDATA\Temp\s; git clone x unminute; ls unminute\tests",
                        "git -C ../unminute-r7 diff --stat",
                        r"Get-ChildItem $env:LOCALAPPDATA\*\models",
                        r"Get-Content $env:APPDATA\Code\settings.json",
                        "gh api graphql -F query=@q.graphql",
                        r"gh api repos/o/r -f body='see %LOCALAPPDATA%\Unminute\workspace'",
                        r"python tools/compare_engines.py --cache %LOCALAPPDATA%\Unminute\models",
                        "python -c \"import faster_whisper, workspace\"",
                        "git status --short",
                        "git check-ignore -v extracted/x",
                        r"Get-ChildItem $env:LOCALAPPDATA\whisper-models",
                        r"rg -n budget $env:LOCALAPPDATA\Temp\review",
                        r"Get-ChildItem -Recurse $env:LOCALAPPDATA\Unminute\models",
                        "rg --files workspace",
                        "rg -g '*.py' -n budget workspace",
                        r"Select-String -Path workspace\app.py 'x'",
                        "python - <<'EOF'\nprint(open('workspace/app.py').read()[:10])\nEOF"):
            with self.subTest(command=command):
                self.assertIsNone(guard.decide(shell(command)))

    def test_message_text_and_search_patterns_are_allowed(self):
        # Rules must stay quotable and searchable: commit messages, PR bodies,
        # heredocs and search patterns are text, not paths to read.
        for command in ('git commit -m "Block access to extracted/ and the live workspace"',
                        'git commit -m "Ignore *.wav and *.mp4"',
                        'git commit -m "Add a note on where extracted text goes"',
                        'grep -rn "extracted/" .gitignore',
                        r'rg -n "whisper\\workspace" workspace',
                        r"Select-String -Path AGENTS.md -Pattern 'whisper\\workspace'",
                        'git log -S "extracted/" --oneline',
                        r'gh pr create --title "G" --body "P2: %LOCALAPPDATA%\whisper\workspace"',
                        r'gh pr create --title "G" --body "P2: %LOCALAPPDATA%\Unminute\workspace"',
                        r'rg -n "LOCALAPPDATA\\Unminute\\workspace" docs',
                        "git commit -F - <<'EOF'\nStop staging *.wav files in extracted/\nEOF",
                        # only the $(...) of a message is run, not the rest of its text
                        'gh pr comment 207 --body "Fixed in $(git rev-parse --short HEAD).'
                        r' P2 blocks %LOCALAPPDATA%\Unminute\workspace"',
                        # local AppData and a data folder's name only as search
                        # patterns (R13b round 2 review)
                        'rg -n "LOCALAPPDATA" docs | rg Unminute',
                        'rg -n "LOCALAPPDATA|Unminute" docs',
                        "git grep -n LOCALAPPDATA -- docs | Select-String whisper",
                        "grep -rn LOCALAPPDATA docs | grep -i unminute",
                        "gh pr view 207 --json body --jq .body | Select-String 'LOCALAPPDATA', "
                        "'Unminute'",
                        r'findstr /s /n "LOCALAPPDATA\Unminute" docs\*.md',
                        r'findstr /s /n /c:"LOCALAPPDATA\Unminute" docs\*.md',
                        r"rg -n budget workspace\*.py",
                        # git log's pickaxe with its pattern attached (R13b round 3)
                        r'git log -S"%LOCALAPPDATA%\Unminute" --oneline',
                        r'git log -G"LOCALAPPDATA.Unminute" --oneline',
                        # a wrapper, with the search's own pattern
                        'timeout 60 rg -n "LOCALAPPDATA" docs',
                        "Write-Output (rg -n budget workspace)"):
            for name in ("Bash", "PowerShell"):
                with self.subTest(command=command, tool=name):
                    self.assertIsNone(guard.decide(shell(command, name)))
        # PowerShell text held in a variable or a here-string is a message too
        # (R13b review: main's hook allowed these).
        for command in ("$b = @'\nP2 covers %LOCALAPPDATA%\\Unminute\\workspace\n'@\n"
                        "gh pr comment 207 --body $b",
                        "@'\nP2 covers %LOCALAPPDATA%\\Unminute\\workspace\n'@ | "
                        "gh pr comment 207 --body-file -",
                        "$b = 'P2 covers %LOCALAPPDATA%\\Unminute\\workspace'; "
                        "gh pr comment 207 --body $b",
                        # ...and in double quotes (R13b round 2 review)
                        '$b = "P2 covers %LOCALAPPDATA%\\Unminute\\workspace"; '
                        "gh pr comment 207 --body $b",
                        '$b = "P2 covers %LOCALAPPDATA%\\whisper\\workspace"; '
                        "gh pr comment 207 --body $b"):
            with self.subTest(command=command):
                self.assertIsNone(guard.decide(shell(command, "PowerShell")))
        # cd inside echoed text does not move the tracked folder.
        self.assertIsNone(guard.decide(tool(
            "PowerShell", cwd=r"C:\Users\someone\Local_Repos",
            command='Write-Output "then cd $env:LOCALAPPDATA"; '
                    r"Get-Content Unminute\settings.json")))

    def test_code_and_variables_that_reach_the_workspace_are_checked(self):
        # A quoted heredoc or here-string fed to an interpreter is code; a variable
        # set earlier in the same command is followed (R13b review).
        for command, name in (
                ("python - <<'EOF'\nprint(open(r'C:\\Users\\someone\\AppData\\Local\\Unminute"
                 "\\workspace\\settings.json').read())\nEOF", "Bash"),
                ("bash <<'EOF'\ncat ~/AppData/Local/Unminute/workspace/a.json\nEOF", "Bash"),
                ("@'\nprint(open(r'C:\\Users\\someone\\AppData\\Local\\Unminute\\workspace"
                 "\\a.json').read())\n'@ | python -", "PowerShell"),
                ("$c = @'\nGet-Content $env:LOCALAPPDATA\\Unminute\\workspace\\a.json\n'@\n"
                 "Invoke-Expression $c", "PowerShell"),
                ('$l = $env:LOCALAPPDATA; Get-Content "$l\\Unminute\\workspace\\a.json"',
                 "PowerShell"),
                ("$p = 'C:\\Users\\someone\\AppData\\Local\\Unminute\\workspace\\a.json'; "
                 "Get-Content $p", "PowerShell"),
                ('L="$LOCALAPPDATA"; cat "$L/Unminute/workspace/a.json"', "Bash"),
                # a double-quoted value is still checked where it is used, and what
                # it runs where it is set (R13b round 2 review)
                ('$p = "C:\\Users\\someone\\AppData\\Local\\Unminute\\workspace\\a.json"; '
                 "Get-Content $p", "PowerShell"),
                ('$p = "$(Get-Content $env:LOCALAPPDATA\\Unminute\\workspace\\a.json)"; '
                 "Write-Output done", "PowerShell"),
                # a quoted heredoc piped to an interpreter
                ("cat <<'EOF' | python -\nprint(open(r'C:\\Users\\someone\\AppData\\Local"
                 "\\Unminute\\workspace\\settings.json').read())\nEOF", "Bash"),
                ("cat <<'EOF' | python3\nprint(open(r'C:\\Users\\someone\\AppData\\Local"
                 "\\Unminute\\workspace\\settings.json').read())\nEOF", "Bash"),
                # PowerShell's ${C:\path} reads the file, even in a message
                ('gh pr comment 207 --body "${C:\\Users\\someone\\AppData\\Local\\Unminute'
                 '\\workspace\\settings.json}"', "PowerShell"),
                # a path built by concatenation or -join, or read by another form
                # of the name, is checked where it is set (R13b round 3 review)
                ('$p = "$env:LOCALAPPDATA" + "\\Unminute\\workspace\\settings.json"; '
                 "Get-Content $p", "PowerShell"),
                ('$p = "$env:LOCALAPPDATA\\" + "Unminute\\workspace\\a.json"; Get-Content $p',
                 "PowerShell"),
                ('$p = "$env:LOCALAPPDATA", "Unminute", "workspace", "a.json" -join "\\"; '
                 "Get-Content $p", "PowerShell"),
                ('$p = "C:\\Users\\someone\\AppData\\Local\\Unminute\\workspace\\a.json"; '
                 "Get-Content $script:p", "PowerShell"),
                ('$p = "C:\\Users\\someone\\AppData\\Local\\Unminute\\workspace\\a.json"; '
                 "Get-Content (Get-Variable p -ValueOnly)", "PowerShell"),
                ("$p = 'C:\\Users\\someone\\AppData\\Local\\Unminute\\workspace\\a.json'; "
                 "Get-Content $script:p", "PowerShell")):
            with self.subTest(command=command):
                self.assertIsNotNone(guard.decide(shell(command, name)))

    def test_nested_wrapped_and_piped_searches_are_checked(self):
        # A search of local AppData inside brackets, a scriptblock, a loop, a string
        # run by another shell, after a wrapper, or fed local AppData by a pipeline
        # (R13b round 3 review).
        for command, name in (
                (r"(Get-ChildItem -Recurse $env:LOCALAPPDATA -Filter *.json).FullName",
                 "PowerShell"),
                ('pwsh -c "rg -n budget $env:LOCALAPPDATA"', "PowerShell"),
                (r"foreach ($f in Get-ChildItem -Recurse $env:LOCALAPPDATA -Filter *.json) "
                 "{ Get-Content $f }", "PowerShell"),
                (r"& { Get-ChildItem -Recurse $env:LOCALAPPDATA -Filter *.json }", "PowerShell"),
                ("Write-Output (rg -n budget $env:LOCALAPPDATA)", "PowerShell"),
                ('powershell -NoProfile -Command "rg -n budget $env:LOCALAPPDATA"',
                 "PowerShell"),
                ('Invoke-Expression "rg -n budget $env:LOCALAPPDATA"', "PowerShell"),
                ('bash -c "rg -n budget $LOCALAPPDATA"', "Bash"),
                ("bash -c 'rg -n budget \"$LOCALAPPDATA\"'", "Bash"),
                ('sh -c "grep -rn budget ~/AppData/Local"', "Bash"),
                ('files=$(grep -rl budget "$LOCALAPPDATA")', "Bash"),
                ('echo "$(rg -n budget ~/AppData/Local)"', "Bash"),
                ('echo "`rg -n budget ~/AppData/Local`"', "Bash"),
                ('for f in $(rg -l budget ~/AppData/Local); do cat "$f"; done', "Bash"),
                ('cmd /c"dir /s %LOCALAPPDATA%"', "Bash"),
                ('timeout 60 rg -n budget "$LOCALAPPDATA"', "Bash"),
                ('nice -n 5 grep -rn budget "$LOCALAPPDATA"', "Bash"),
                ('command rg -n budget "$LOCALAPPDATA"', "Bash"),
                ("echo $LOCALAPPDATA | xargs rg -n budget", "Bash"),
                ('echo "$LOCALAPPDATA" | xargs grep -rn budget', "Bash"),
                (r"Get-ChildItem $env:LOCALAPPDATA -Directory | Get-ChildItem -Recurse "
                 "-Filter *.json | Select-String budget", "PowerShell"),
                (r"Get-ChildItem $env:LOCALAPPDATA -Directory | "
                 "ForEach-Object { rg -n budget $_.FullName }", "PowerShell")):
            with self.subTest(command=command):
                self.assertIsNotNone(guard.decide(shell(command, name)))
        for command, name in (("timeout 60 rg -n budget workspace", "Bash"),
                              ("find . -name '*.py' | xargs grep -l budget", "Bash"),
                              ('echo "$(git rev-parse --short HEAD)"', "Bash"),
                              (r"Get-ChildItem $env:LOCALAPPDATA -Directory | "
                               "Select-Object Name", "PowerShell"),
                              ("(Get-ChildItem -Recurse workspace -Filter *.py).Count",
                               "PowerShell")):
            with self.subTest(command=command):
                self.assertIsNone(guard.decide(shell(command, name)))

    def test_short_message_options_are_text_only_for_git_and_gh(self):
        # -m, -b and -t are message text for git and gh only, and -Value for the
        # cmdlets that write text; another program's value is a path (R13b round 4).
        data = r"C:\Users\someone\AppData\Local\Unminute\workspace"
        for command, name in (
                ('ls -t "C:/Users/someone/AppData/Local/Unminute/workspace"', "Bash"),
                ('cat -b "C:/Users/someone/AppData/Local/Unminute/workspace/a.json"', "Bash"),
                ("sort -m 'C:/Users/someone/AppData/Local/whisper/workspace/a.txt'", "Bash"),
                (f'Get-Item -t "{data}\\a.json"', "PowerShell"),
                (f'Set-ItemProperty -Path HKCU:\\x -Name y -Value "{data}"', "PowerShell"),
                (f"gh-tool -f 'k={data}\\a.json'", "Bash")):
            with self.subTest(command=command):
                self.assertIsNotNone(guard.decide(shell(command, name)))
        for command, name in (
                (f'gh pr create -t "P2" -b "P2: {data}"', "Bash"),
                (f'git commit -m "P2: {data}"', "PowerShell"),
                (f'git commit -m "Fix a | b; c" -m "P2: {data}"', "Bash"),
                (f'git -C ../unminute-r7 commit -m "P2: {data}"', "Bash"),
                (f'& git commit -m "P2: {data}"', "PowerShell"),
                (f'git add AGENTS.md && git commit -m "P2: {data}"', "Bash"),
                (f"gh api repos/o/r/issues -f 'body=P2: {data}'", "Bash"),
                (f"Set-Content -Path notes.md -Value '{data}'", "PowerShell"),
                (f"Add-Content notes.md -Value 'P2: {data}'", "PowerShell"),
                ("ls -t workspace", "Bash")):
            with self.subTest(command=command):
                self.assertIsNone(guard.decide(shell(command, name)))

    def test_an_assigned_command_substitution_is_kept_whole(self):
        # A bash value from $(...) or backticks, with quotes inside, is substituted
        # whole where it is used (R13b round 4 review).
        for command in ('L=$(cygpath "$LOCALAPPDATA"); rg -n budget "$L"',
                        'L="$(cygpath -u "$LOCALAPPDATA")"; grep -rn budget "$L"',
                        'L=`cygpath "$LOCALAPPDATA"`; rg -n budget "$L"',
                        'export L=$(cygpath "$LOCALAPPDATA"); find "$L" -name "*.json"'):
            with self.subTest(command=command):
                self.assertIsNotNone(guard.decide(shell(command)))
        for command in ('H=$(git rev-parse --short HEAD); echo "$H"',
                        'V="$(python -c "print(1)")"; echo "$V"',
                        'R=$(git rev-parse --show-toplevel); rg -n budget "$R/workspace"'):
            with self.subTest(command=command):
                self.assertIsNone(guard.decide(shell(command)))

    def test_home_folder_climbs_and_other_names_are_expanded(self):
        # ~\.. keeps the folder it climbs from; a bare cd is the home folder, and an
        # empty popd stays; the home folder's other names (R13b round 4 review).
        home = r"C:\Users\someone"
        env = {"LOCALAPPDATA": home + r"\AppData\Local", "USERPROFILE": home,
               "SystemDrive": "C:", "PUBLIC": r"C:\Users\Public", "USERNAME": "someone",
               "HOMEDRIVE": "C:", "HOMEPATH": r"\Users\someone", "OneDrive": home + r"\OneDrive",
               "windir": r"C:\Windows", "ProgramData": r"C:\ProgramData"}
        with mock.patch.dict(os.environ, env):
            fresh = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fresh)
            for command, name in (
                    (r"rg -n budget ~\..", "Bash"),
                    ("rg -n budget ~/..", "Bash"),
                    (r"rg -n budget ~\..", "PowerShell"),
                    ("grep -rn budget $HOME/..", "Bash"),
                    (r"Get-ChildItem -Recurse $env:USERPROFILE\.. -Filter *.json", "PowerShell"),
                    ("cd ~/..; rg -n budget", "Bash"),
                    (r"cd $env:LOCALAPPDATA; popd; rg -n budget", "PowerShell"),
                    (r"Set-Location $env:LOCALAPPDATA; Pop-Location; "
                     "Get-ChildItem -Recurse -Filter *.json", "PowerShell"),
                    ('rg -n budget "$HOMEDRIVE$HOMEPATH"', "Bash"),
                    (r'Get-ChildItem -Recurse "$env:HOMEDRIVE$env:HOMEPATH" -Filter *.json',
                     "PowerShell"),
                    ("rg -n budget %HOMEDRIVE%%HOMEPATH%", "PowerShell"),
                    (r"rg -n budget $env:SystemDrive\Users", "PowerShell"),
                    (r"grep -rn budget %SystemDrive%\Users", "Bash"),
                    (r"rg -n budget C:\Users\$env:USERNAME", "PowerShell"),
                    (r"rg -n budget $env:PUBLIC\..", "PowerShell"),
                    (r"rg -n budget $env:OneDrive\..", "PowerShell"),
                    ("rg -n budget ([Environment]::GetFolderPath('UserProfile'))", "PowerShell"),
                    # any variable holding a full path, an option's joined value, a
                    # cd at the start of a block, and .NET's AllDirectories
                    (r"rg -n budget $env:windir\..\Users", "PowerShell"),
                    (r"rg -n budget %ProgramData%\..\Users", "PowerShell"),
                    (r"Get-ChildItem -Recurse -Path:$env:USERPROFILE -Filter *.json",
                     "PowerShell"),
                    (r"Set-Location -Path:$env:LOCALAPPDATA; rg -n budget", "PowerShell"),
                    (r"if ($true) { cd $env:LOCALAPPDATA }; rg -n budget", "PowerShell"),
                    (r"[IO.Directory]::GetFiles($env:LOCALAPPDATA, '*.json', 'AllDirectories')",
                     "PowerShell"),
                    (r"[IO.Directory]::EnumerateFiles($env:USERPROFILE, '*', 'AllDirectories')",
                     "PowerShell")):
                with self.subTest(command=command, tool=name):
                    self.assertIsNotNone(fresh.decide(shell(command, name)))
            for command, name in (
                    (r"rg -n budget $env:USERPROFILE\Local_Repos\unminute", "PowerShell"),
                    (r"rg -n budget ~\Local_Repos\unminute\..\unminute-r7", "PowerShell"),
                    ("cd ~/Local_Repos/unminute/..; rg -n x unminute", "Bash"),
                    (r"rg -n budget $env:OneDrive\Documents", "PowerShell"),
                    (r"Get-ChildItem $env:SystemDrive\Users", "PowerShell"),
                    (r"Get-ChildItem $env:windir\Fonts", "PowerShell"),
                    (r"Get-ChildItem -Recurse -Path:$env:USERPROFILE\.claude -Filter *.md",
                     "PowerShell"),
                    (r"[IO.Directory]::GetFiles(" + '"$env:USERPROFILE\\Local_Repos"'
                     + ", '*.md', 'AllDirectories')", "PowerShell")):
                with self.subTest(command=command, tool=name):
                    self.assertIsNone(fresh.decide(shell(command, name)))
            folder = fresh._Folder(home + r"\Local_Repos")
            folder.change("popd", "")
            self.assertEqual(home + r"\Local_Repos", folder.here)
            folder.change("cd", "")
            self.assertEqual(home, folder.here)

    def test_a_search_pattern_is_not_read_as_a_nested_command(self):
        # A pattern that quotes a search is text; -E and -SimpleMatch are switches
        # that take no value (R13b round 4 review).
        for command, name in (
                ('rg -n "rg -n budget $env:LOCALAPPDATA" docs', "Bash"),
                ('rg -n "rg -n budget $env:LOCALAPPDATA" docs', "PowerShell"),
                ("grep -rn 'find ~/AppData/Local' docs", "Bash"),
                (r"Select-String -Path docs\*.md -Pattern 'Get-ChildItem -Recurse "
                 r"$env:LOCALAPPDATA'", "PowerShell"),
                ("grep -E 'a|b' -rn workspace", "Bash")):
            with self.subTest(command=command, tool=name):
                self.assertIsNone(guard.decide(shell(command, name)))
        for command, name in (
                ("grep budget -E ~/AppData/Local/Unminute/workspace/a.json", "Bash"),
                (r"Select-String budget -SimpleMatch C:\Users\someone\AppData\Local\Unminute"
                 r"\workspace\a.json", "PowerShell")):
            with self.subTest(command=command, tool=name):
                self.assertIsNotNone(guard.decide(shell(command, name)))

    def test_cd_in_a_string_another_shell_runs_is_followed(self):
        # cd at the start of bash -c, pwsh -c or a scriptblock moves the folder the
        # rest runs in (R13b round 4 review).
        for command, name in (('bash -c "cd $LOCALAPPDATA && rg -n budget"', "Bash"),
                              ("sh -c 'cd ~/AppData/Local; grep -rn budget .'", "Bash"),
                              ('pwsh -c "Set-Location $env:LOCALAPPDATA; rg -n budget"',
                               "PowerShell"),
                              ('powershell -NoProfile -Command "cd $env:LOCALAPPDATA; '
                               'Get-ChildItem -Recurse -Filter *.json"', "PowerShell"),
                              (r"& { cd $env:LOCALAPPDATA; rg -n budget }", "PowerShell"),
                              ('if true; then cd "$LOCALAPPDATA"; fi; rg -n budget', "Bash"),
                              ('for d in a; do cd "$LOCALAPPDATA"; done; rg -n budget', "Bash"),
                              # -c joined with other flags (bash -lc, sh -ec)
                              ('bash -lc "cd $LOCALAPPDATA && rg -n budget"', "Bash"),
                              ("sh -ec 'cd ~/AppData/Local; grep -rn budget .'", "Bash")):
            with self.subTest(command=command):
                self.assertIsNotNone(guard.decide(shell(command, name)))
        for command in ('bash -c "cd workspace && rg -n x"', 'bash -lc "cd workspace && rg -n x"'):
            with self.subTest(command=command):
                self.assertIsNone(guard.decide(shell(command)))

    def test_other_recursive_programs_and_interpreter_walks_are_checked(self):
        # du, forfiles /s and where /r walk a folder's tree; so does code that walks
        # local AppData or the home folder named whole (R13b round 4 review).
        with profile_guard() as fresh:
            for command, name in (
                    (r"forfiles /p %LOCALAPPDATA% /s /m *.json", "PowerShell"),
                    (r"where /r %LOCALAPPDATA% *.json", "Bash"),
                    ("du -a ~/AppData/Local", "Bash"),
                    (r"cd $env:LOCALAPPDATA; du -a", "PowerShell"),
                    ("python -c \"import os; [print(f) for r, d, f in "
                     "os.walk(os.environ['LOCALAPPDATA'])]\"", "Bash"),
                    ("python - <<'EOF'\nimport os, pathlib\nfor p in pathlib.Path("
                     "os.environ['LOCALAPPDATA']).rglob('*.json'): print(p)\nEOF", "Bash"),
                    ("node -e \"console.log(require('fs').readdirSync(process.env.LOCALAPPDATA, "
                     "{recursive: true}))\"", "Bash"),
                    ("python -c \"from pathlib import Path; "
                     "print(list(Path.home().rglob('*.db')))\"",
                     "PowerShell"),
                    ("python -c \"import os; print(list(os.walk(os.path.expanduser('~'))))\"",
                     "Bash"),
                    # a folder above local AppData written out, or the home folder read
                    # from the environment, in code that walks it
                    ("python -c \"import os; print(list(os.walk(r'C:\\Users')))\"", "Bash"),
                    ("python -c \"import os; print(list(os.walk('C:/Users/someone/AppData')))\"",
                     "Bash"),
                    ("python -c \"import pathlib; print(list(pathlib.Path(r'C:\\Users')"
                     ".rglob('*.db')))\"", "PowerShell"),
                    ("python -c \"import glob; "
                     "print(glob.glob('C:/Users/**/*.db', recursive=True))\"",
                     "Bash"),
                    ("python -c \"import os; print(list(os.walk(os.environ['USERPROFILE'])))\"",
                     "Bash"),
                    ("python -c \"import os; print(list(os.walk(os.getenv('HOME'))))\"", "Bash"),
                    ("node -e \"require('fs').readdirSync(process.env.USERPROFILE, "
                     "{recursive: true})\"", "Bash"),
                    # a relative start, run after a cd to a folder above local AppData
                    ("cd ~ && python -c \"import os; print([r for r, d, f in os.walk('.')])\"",
                     "Bash"),
                    ("cd /c/Users && python -c \"import os; "
                     "print([r for r, d, f in os.walk('.')])\"",
                     "Bash"),
                    ("cd ~/.. && python -c \"import os; print(list(os.walk('.')))\"", "Bash"),
                    ("Set-Location C:\\Users; python -c \"import glob; print(glob.glob('**/*.db', "
                     "recursive=True))\"", "PowerShell")):
                with self.subTest(command=command):
                    self.assertIsNotNone(fresh.decide(shell(command, name)))
            for command, name in (
                    ("du -sh workspace", "Bash"),
                    ("where python", "PowerShell"),
                    ("forfiles /p workspace /m *.py", "PowerShell"),
                    ("python -c \"import glob; print(glob.glob('workspace/**/*.py', "
                     "recursive=True))\"", "Bash"),
                    ("python -c \"import os; print(os.listdir(os.environ['LOCALAPPDATA']))\"",
                     "Bash"),
                    ("python -c \"from pathlib import Path; print(list((Path.home() / "
                     "'Local_Repos').rglob('*.md')))\"", "Bash"),
                    ("python -c \"import os; print([r for r, d, f in os.walk('.')])\"", "Bash"),
                    ("python -c \"import os; print(list(os.walk('workspace')))\"", "PowerShell"),
                    ("python -c \"import pathlib; "
                     "print(list(pathlib.Path('tests').rglob('*.py')))\"",
                     "Bash"),
                    ("cd workspace && python -c \"import os; print(list(os.walk('.')))\"", "Bash")):
                with self.subTest(command=command):
                    self.assertIsNone(fresh.decide(shell(command, name)))

    def test_cygdrive_paths_and_variable_drive_reads_are_checked(self):
        # Git Bash's /proc/cygdrive/c is C:\, and a value read back through
        # PowerShell's variable: drive is substituted like $p (R13b round 4 review;
        # 27e1cac blocked the variable: forms).
        with profile_guard() as fresh:
            data = r"C:\Users\someone\AppData\Local\Unminute\workspace\a.json"
            for payload in (shell("rg -n budget /proc/cygdrive/c/Users"),
                            shell("grep -rn budget /proc/cygdrive/c/Users/someone/AppData"),
                            tool("Grep", pattern="budget", path="/proc/cygdrive/c/Users"),
                            shell(f'$p = "{data}"; Get-Content (Get-Content variable:p)',
                                  "PowerShell"),
                            shell(f'$p = "{data}"; Get-Content (Get-Item variable:p).Value',
                                  "PowerShell"),
                            shell(f"$p = '{data}'; Get-Content (Get-Item variable:\\p).Value",
                                  "PowerShell")):
                with self.subTest(payload=payload):
                    self.assertIsNotNone(fresh.decide(payload))
            for payload in (shell("ls /proc/cygdrive/c/Users"),
                            shell('$p = "notes: workspace"; Get-Content variable:q', "PowerShell")):
                with self.subTest(payload=payload):
                    self.assertIsNone(fresh.decide(payload))

    def test_monitor_commands_are_checked(self):
        # Monitor runs a shell command too.
        self.assertIsNotNone(guard.decide(tool(
            "Monitor", command="cat ~/AppData/Local/Unminute/workspace/settings.json")))
        self.assertIsNotNone(guard.decide(tool(
            "Monitor", command=r"Get-Content $env:LOCALAPPDATA\Unminute\workspace\a.json -Wait")))
        self.assertIsNone(guard.decide(tool("Monitor", command="gh pr checks 207 --watch")))

    def test_message_text_the_shell_runs_is_checked(self):
        # $(...), a bash backtick or an unquoted heredoc runs before the message is
        # written, so it can read the workspace.
        for command, name in (
                ('git commit -m "$(cat ~/AppData/Local/Unminute/workspace/a.txt)"', "Bash"),
                ('git commit -m "`cat ~/AppData/Local/Unminute/workspace/a.txt`"', "Bash"),
                ("cat <<EOF > x.md\n$(cat ~/AppData/Local/Unminute/workspace/a)\nEOF", "Bash"),
                (r'Set-Content x.md -Value "$(Get-Content $env:LOCALAPPDATA\Unminute\workspace\a)"',
                 "PowerShell")):
            with self.subTest(command=command):
                self.assertIsNotNone(guard.decide(shell(command, name)))
        # Quoted heredocs, single quotes and a PowerShell backtick are only text.
        for command, name in (
                ("git commit -m \"$(cat <<'EOF'\nP2: %LOCALAPPDATA%\\Unminute\\workspace\nEOF\n)\"",
                 "Bash"),
                ("git commit -m @'\n$(x) %LOCALAPPDATA%\\Unminute\\workspace\n'@", "PowerShell"),
                ('gh pr comment 1 --body "Ran `rg`; P2: %LOCALAPPDATA%\\Unminute\\workspace"',
                 "PowerShell")):
            with self.subTest(command=command):
                self.assertIsNone(guard.decide(shell(command, name)))

    def test_other_tools_are_allowed(self):
        self.assertIsNone(guard.decide(tool("WebSearch", query="extracted/ whisper/workspace")))


class RoundFiveTests(unittest.TestCase):
    """Phrasings the R13b round 5 review found allowed, each failing on 3d91ed6."""

    def check(self, fresh, blocked, allowed):
        for command, name in blocked:
            with self.subTest(command=command, tool=name):
                self.assertIsNotNone(fresh.decide(shell(command, name)))
        for command, name in allowed:
            with self.subTest(command=command, tool=name):
                self.assertIsNone(fresh.decide(shell(command, name)))

    def test_a_cd_to_a_command_substitution_is_followed(self):
        # The cd's argument is one whole shell word; cygpath X is X, dirname X and
        # Split-Path X are X\.., and an unknown $(...) naming local AppData or the
        # home folder is taken as that folder.
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ('D=$(cygpath -u "$LOCALAPPDATA"); cd "$D" && cat Unminute/workspace/settings.json',
                 "Bash"),
                ('cd "$(cygpath -u "$LOCALAPPDATA")" && sqlite3 Unminute/workspace/workspace.db '
                 '.tables', "Bash"),
                ('cd "$(cygpath -u "$LOCALAPPDATA")"; ls Unminute/workspace', "Bash"),
                ('cd "$(cygpath "$LOCALAPPDATA")" && rg -l budget', "Bash"),
                ('cd $(cygpath -u "$LOCALAPPDATA") && grep -rl budget .', "Bash"),
                ('cd `cygpath "$LOCALAPPDATA"` && rg -l budget', "Bash"),
                ('pushd "$(cygpath "$LOCALAPPDATA")" && rg -l budget', "Bash"),
                ('cd "$(cygpath "$USERPROFILE")" && rg -l budget', "Bash"),
                ('cd "$(dirname "$LOCALAPPDATA")" && rg -l budget', "Bash"),
                ('cd "$(cygpath -u "$LOCALAPPDATA")/Temp/.." && rg -l budget', "Bash"),
                ('cd "$(cygpath -u "$LOCALAPPDATA")/Unminute" && ls workspace', "Bash"),
                ('cd "$(echo "$LOCALAPPDATA" | tr a a)" && rg -l budget', "Bash"),
                ("Set-Location (Split-Path $env:LOCALAPPDATA); Get-ChildItem -Recurse -Filter *.db",
                 "PowerShell"),
                ("Set-Location (Resolve-Path ~); rg -l budget", "PowerShell"),
                ('Push-Location "$($env:LOCALAPPDATA)"; rg -l budget', "PowerShell"),
                ('Set-Location -Path "$env:LOCALAPPDATA"; gci -r', "PowerShell")),
                allowed=(
                ('cd "$(git rev-parse --show-toplevel)" && rg -n budget', "Bash"),
                ('cd "$(git rev-parse --show-toplevel)" && grep -rn budget workspace', "Bash"),
                ('cd "$(cygpath "$USERPROFILE")/Local_Repos/unminute" && rg -l budget', "Bash"),
                ('cd "$(cygpath -u "$LOCALAPPDATA")/Temp" && rg -l budget', "Bash"),
                ('pushd "$(mktemp -d)" && ls -R . && popd', "Bash"),
                ("cd -P workspace && ls", "Bash"),
                ("Push-Location (Split-Path $PSScriptRoot); rg -l budget", "PowerShell")))

    def test_a_climb_before_more_arguments_keeps_its_folder(self):
        # "$LOCALAPPDATA/.." expanded no longer ends in \" (an escaped quote), and a
        # PowerShell backslash before a quote is not an escape.
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ('find "$LOCALAPPDATA/.." -name "*.db"', "Bash"),
                ('find "$TEMP/.." -name "*.db"', "Bash"),
                ('grep -rn budget "$LOCALAPPDATA/.." --include="*.json"', "Bash"),
                ('rg budget "$LOCALAPPDATA/.." -g "*.json"', "Bash"),
                ('find "$LOCALAPPDATA/../.." -name "*.db"', "Bash"),
                ('rg budget "$env:LOCALAPPDATA\\..\\" -g "*.json"', "PowerShell"),
                ('rg budget "$env:LOCALAPPDATA\\.." -g "*.json"', "PowerShell"),
                ('rg x C:/Users/someone/AppData/Local/Temp/.. -g "*.json"', "Bash"),
                ('rg x "C:/Users/.."', "Bash")),
                allowed=(
                ('ls "$LOCALAPPDATA/Unminute/models/a/.."', "Bash"),
                ('rg -n budget "$TEMP/claude/../claude" -g "*.json"', "Bash")))

    def test_compact_assignments_in_interpreter_code_are_checked(self):
        # A ;-separated statement that is only an assignment (d=json.load(...)) is
        # checked, for both data folders.
        data = r"C:\Users\someone\AppData\Local"
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                (f"python -c \"import json;d=json.load(open(r'{data}\\Unminute\\workspace"
                 f"\\settings.json'));print(d)\"", "Bash"),
                (f"python -c \"import json;d=json.load(open(r'{data}\\Unminute\\workspace"
                 f"\\settings.json'));print(d)\"", "PowerShell"),
                (f"python -c \"import sqlite3;c=sqlite3.connect(r'{data}\\whisper\\workspace"
                 f"\\workspace.db');print(c)\"", "Bash"),
                ("python -c \"import os;p=os.path.join(os.environ['LOCALAPPDATA'],'Unminute',"
                 "'workspace');print(os.listdir(p))\"", "Bash"),
                ("python -c \"import pathlib;p=pathlib.Path.home()/'AppData/Local/Unminute/"
                 "workspace';print(list(p.iterdir()))\"", "Bash"),
                ("python - <<'EOF'\nimport os\np=os.path.expanduser(\"~/AppData/Local/Unminute/"
                 "workspace\")\nprint(os.listdir(p))\nEOF", "Bash"),
                ("python -c \"import os;root=os.environ['LOCALAPPDATA'];"
                 "print([r for r,d,f in os.walk(root)])\"", "Bash")),
                allowed=(
                ('python -c "a=1;b=2;print(a+b)"', "Bash"),
                ("x=1; y=2; echo $x", "Bash"),
                ("PYTHONDONTWRITEBYTECODE=1 python tools/run_tests.py", "Bash"),
                ("python -c \"import os;p=os.path.join('workspace','store.py');print(p)\"",
                 "Bash")))

    def test_git_short_options_are_message_text_only_where_git_takes_one(self):
        # -m only for commit, tag, merge, notes and stash; git's -b and -t take a
        # path or a name (git diff --no-index -b, git commit -t).
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ('git diff --no-index -b "$LOCALAPPDATA/Unminute/workspace/a.json" b.json',
                 "Bash"),
                ("git diff --no-index -b 'C:/Users/someone/AppData/Local/Unminute/workspace/"
                 "a.json' b.json", "Bash"),
                ('git diff --no-index -t "C:/Users/someone/AppData/Local/whisper/workspace/'
                 'a.json" b.json', "Bash"),
                ('git commit -t "C:/Users/someone/AppData/Local/Unminute/workspace/a.txt"',
                 "Bash")),
                allowed=(
                ('git stash push -m "wip rg -n x $LOCALAPPDATA"', "Bash"),
                ('git tag -a v1 -m "rg -n x $LOCALAPPDATA"', "Bash"),
                ('git merge -m "rg -n x $LOCALAPPDATA" topic', "Bash"),
                ('gh issue create -t "x" -b "cd $LOCALAPPDATA && rg x"', "Bash"),
                ("git checkout -b feature/x", "Bash"),
                ("git diff --no-index -b a.json b.json", "Bash")))

    def test_loop_variables_and_other_assignments_are_followed(self):
        # A variable straight after local AppData stands for any name; bash's for
        # list is written where its variable is used; assignments after then, do,
        # { and declare -x, readonly or typeset are substituted; a ) ends a name.
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ('ls "$LOCALAPPDATA" | while read d; do ls -R "$LOCALAPPDATA/$d"; done', "Bash"),
                ('ls -R "$LOCALAPPDATA/$d"', "Bash"),
                ('ls -R "$LOCALAPPDATA/${d}"', "Bash"),
                ('if true; then L=$LOCALAPPDATA; fi; rg -n budget "$L"', "Bash"),
                ('declare -x L=$LOCALAPPDATA; rg -n budget "$L"', "Bash"),
                ('readonly L=$LOCALAPPDATA; rg -n budget "$L"', "Bash"),
                ('typeset -r L=$LOCALAPPDATA; rg -n budget "$L"', "Bash"),
                ('{ L=$LOCALAPPDATA; rg -n budget "$L"; }', "Bash"),
                ('for d in "$LOCALAPPDATA"; do rg -n budget "$d"; done', "Bash"),
                ('for d in ~; do rg -n x "$d"; done', "Bash"),
                ('cat "$(ls -d $LOCALAPPDATA/Unm*)/workspace/settings.json"', "Bash")),
                allowed=(
                ('ls "$LOCALAPPDATA" | while read d; do ls -R "$LOCALAPPDATA/$d/models"; done',
                 "Bash"),
                ('ls "$TEMP/$x"', "Bash"),
                ("for f in $(git ls-files '*.py'); do ruff check \"$f\"; done", "Bash"),
                ('for f in workspace/*.py; do ruff check "$f"; done', "Bash"),
                ('readonly L=workspace; rg -n budget "$L"', "Bash")))

    def test_text_writer_aliases_and_the_walk_folder(self):
        # sc and ac write -Value text; a relative walk is judged from the folder
        # the interpreter runs in, not every folder visited before it.
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ('cd /c/Users && python -c "import os; print([r for r, d, f in os.walk(\'.\')])"',
                 "Bash"),
                ("cd /c/Users\npython - <<'EOF'\nimport os\nprint([r for r, d, f in "
                 "os.walk('.')])\nEOF", "Bash"),
                ('cd ~; python -c "import os; print([r for r, d, f in os.walk(\'.\')])"', "Bash")),
                allowed=(
                ("sc notes.md -Value 'rg -n budget $env:LOCALAPPDATA'", "PowerShell"),
                ("ac notes.md -Value 'rg -n budget $env:LOCALAPPDATA'", "PowerShell"),
                ('cd ~ && cd Local_Repos/unminute && python -c "import os; '
                 'print([r for r, d, f in os.walk(\'.\')])"', "Bash"),
                ('python -c "import os; print([r for r, d, f in os.walk(\'.\')])"; cd /c/Users',
                 "Bash")))

    @unittest.skipUnless(sys.platform == "win32", "Windows paths")
    def test_a_search_of_a_checkout_holding_extracted_is_blocked(self):
        # grep -r, rg -uu, Get-ChildItem -Recurse and findstr /s from a checkout read
        # its extracted/ folder; rg and ag skip it by .gitignore unless told not to.
        # Synthetic checkouts in a temporary folder; nothing in them is read.
        with tempfile.TemporaryDirectory() as temp:
            held, plain = Path(temp) / "held", Path(temp) / "plain"
            for checkout in (held, plain):
                (checkout / "workspace").mkdir(parents=True)
                (checkout / "AGENTS.md").write_text("synthetic\n", encoding="utf-8")
            (held / "extracted").mkdir()
            searches = (("grep -rn budget .", "Bash"), ("grep -rn budget", "Bash"),
                        ("grep -rn budget *", "Bash"), ("rg -uu budget", "Bash"),
                        ("rg --no-ignore -n budget .", "Bash"), ('find . -name "*.md"', "Bash"),
                        ("Get-ChildItem -Recurse -Filter *.md | Select-String budget",
                         "PowerShell"),
                        ("findstr /s /i budget *.md", "PowerShell"))
            for command, name in searches:
                with self.subTest(command=command, tool=name):
                    self.assertIsNotNone(guard.decide(tool(name, cwd=held, command=command)))
                    self.assertIsNone(guard.decide(tool(name, cwd=plain, command=command)))
            for command, name in (("rg -n budget", "Bash"), ("rg -n budget .", "Bash"),
                                  ("git grep -n budget", "Bash"),
                                  ("grep -rn budget workspace", "Bash")):
                with self.subTest(command=command, tool=name):
                    self.assertIsNone(guard.decide(tool(name, cwd=held, command=command)))
            # The checkout named as the folder searched, from anywhere.
            self.assertIsNotNone(guard.decide(tool("Bash", command=f'grep -rn budget "{held}"')))


class HardeningTests(unittest.TestCase):
    """#221: the round 6 findings on 70f220f, the #207 round 7 and 8 carry-overs, gaps
    found while porting the hardening onto main, and a corpus of common phrasings.
    Synthetic paths under a synthetic profile; nothing is read."""

    DATA = r"C:\Users\someone\AppData\Local\Unminute\workspace"

    def check(self, fresh, blocked=(), allowed=()):
        for command, name in blocked:
            with self.subTest(command=command, tool=name):
                self.assertIsNotNone(fresh.decide(shell(command, name)))
        for command, name in allowed:
            with self.subTest(command=command, tool=name):
                self.assertIsNone(fresh.decide(shell(command, name)))

    def test_a_cd_to_local_appdata_by_its_folder_call_is_followed(self):
        # Round 6 regression on 70f220f: GetFolderPath('LocalApplicationData') as the
        # folder of a cd, then a recursive listing or a relative read.
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ("Set-Location ([Environment]::GetFolderPath('LocalApplicationData')); "
                 "Get-ChildItem -Recurse -Filter *.db", "PowerShell"),
                ("Set-Location ([Environment]::GetFolderPath('LocalApplicationData')); "
                 r"Get-Content Unminute\settings.json", "PowerShell"),
                ('cd ([System.Environment]::GetFolderPath("LocalApplicationData")); rg -n budget',
                 "PowerShell"),
                ("Push-Location ([Environment]::GetFolderPath([Environment+SpecialFolder]::"
                 "LocalApplicationData)); gci -r", "PowerShell"),
                ("Set-Location ([Environment]::GetFolderPath('LocalApplicationData')); cd Temp; "
                 r"Get-Content ..\Unminute\settings.json", "PowerShell")),
                allowed=(
                ("Set-Location ([Environment]::GetFolderPath('LocalApplicationData')); "
                 r"Get-ChildItem Unminute\models", "PowerShell"),
                ("cd ([Environment]::GetFolderPath('LocalApplicationData')); cd Temp\\review; "
                 "rg -n budget", "PowerShell")))

    def test_a_data_path_after_a_backtick_substitution_is_seen(self):
        # Round 6: a bash backtick closing the substitution that gives local AppData.
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ('cat "`cygpath -u $LOCALAPPDATA`/Unminute/settings.json"', "Bash"),
                ("cat `cygpath -u $LOCALAPPDATA`/whisper/settings.json", "Bash"),
                ('ls "`cygpath -u "$LOCALAPPDATA"`/Unminute"', "Bash"),
                ("ls -R `cygpath $LOCALAPPDATA`/Unmin*", "Bash")),
                allowed=(
                ('ls "`cygpath -u $LOCALAPPDATA`/Unminute/models"', "Bash"),
                ('ls "`cygpath -u $LOCALAPPDATA`/Temp"', "Bash")))

    def test_a_variable_that_keeps_its_trailing_separator_is_seen(self):
        # #207 round 8, nit 1: a base variable that ends in a separator, joined
        # straight onto the name, set in this command or an earlier one.
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ('$d = "$env:LOCALAPPDATA\\"; gci "${d}Unminute\\workspace"', "PowerShell"),
                ("D=~/AppData/Local/; ls ${D}Unminute/workspace", "Bash"),
                (r"set D=%LOCALAPPDATA%\&& type %D%Unminute\workspace\a.json", "PowerShell"),
                (r"set D=%LOCALAPPDATA%\&& type %D%Unminute\workspace\a.json", "Bash"),
                (r'gci "${d}Unminute\workspace"', "PowerShell"),
                ("ls ${D}Unminute/workspace", "Bash"),
                (r"type %D%Unminute\workspace\a.json", "PowerShell")),
                allowed=(
                ("ls ../unminute/workspace", "Bash"),
                (r"Get-Content C:\Users\x\Local_Repos\unminute\workspace\api.py", "PowerShell"),
                ("ruff check ~/Local_Repos/unminute/workspace", "Bash")))
        pattern = guard.unminute_workspace_pattern("")
        self.assertIsNotNone(pattern.search(r'gci "${d}Unminute\workspace"'))
        self.assertIsNone(pattern.search(r"ls ..\unminute\workspace"))

    def test_a_wildcard_inside_the_parent_folder_is_seen(self):
        # #207 round 8, nit 2: a wildcard anywhere in local AppData's own name.
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ("ls ~/AppData/?ocal/Unminute/workspace", "Bash"),
                ("ls ~/AppData/[L]ocal/Unminute/workspace", "Bash"),
                ("cat ~/AppData/L*l/Unminute/workspace/a.json", "Bash"),
                (r"Get-ChildItem $env:USERPROFILE\AppData\L?cal\Unminute\workspace",
                 "PowerShell")),
                allowed=(
                ("ls ~/Local_Repos/unminute*/workspace", "Bash"),
                ("rg -n x ~/Local_Repos/unminute/workspace/*.py", "Bash")))
            for pattern in (r"AppData\L?cal\Unminute\workspace\**",
                            "AppData/[L]ocal/Unminute/workspace/*"):
                with self.subTest(glob=pattern):
                    self.assertIsNotNone(fresh.decide(tool("Glob", cwd=r"C:\Users\someone",
                                                           pattern=pattern)))

    def test_a_single_climb_is_judged_from_the_folder_it_runs_in(self):
        # #207 round 7: ..\Unminute\workspace from %TEMP% is the live workspace;
        # from a folder beside a checkout it is that checkout (main allows both).
        local = r"C:\Users\someone\AppData\Local"
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                (r"cd $env:TEMP; Get-Content ..\Unminute\workspace\a.json", "PowerShell"),
                ('cd "$TEMP" && cat ../Unminute/workspace/a.json', "Bash"),
                ("cd /tmp && ls ../Unminute/workspace", "Bash")),
                allowed=(("ls ../unminute/workspace", "Bash"),))
            for payload in (tool("PowerShell", cwd=local + r"\Temp",
                                 command=r"Get-Content ..\Unminute\workspace\a.json"),
                            tool("Read", cwd=local + r"\Temp",
                                 file_path=r"..\Unminute\workspace\a.json")):
                with self.subTest(payload=payload):
                    self.assertIsNotNone(fresh.decide(payload))
            self.assertIsNone(fresh.decide(tool(
                "Bash", cwd=r"C:\Users\someone\Local_Repos\unminute-221",
                command="ls ../unminute/workspace")))

    def test_text_fed_to_a_reader_is_checked(self):
        # Found while porting: a here-string piped to Get-Content, or held in a
        # variable it reads, and a heredoc fed to xargs name files to read. Text a
        # message or a file only takes stays text.
        data = self.DATA + "\\a.json"
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ("@'\n" + data + "\n'@ | Get-Content", "PowerShell"),
                ("$p = @'\n" + data + "\n'@\nGet-Content $p", "PowerShell"),
                ("@'\n" + data + "\n'@ | ForEach-Object { Get-Content $_ }", "PowerShell"),
                ("cat <<'EOF' | xargs cat\n" + data.replace("\\", "/") + "\nEOF", "Bash"),
                ("xargs cat <<'EOF'\n" + data.replace("\\", "/") + "\nEOF", "Bash")),
                allowed=(
                ("$b = @'\nP2 covers %LOCALAPPDATA%\\Unminute\n'@\ngh pr comment 1 --body $b",
                 "PowerShell"),
                ("@'\nP2 covers %LOCALAPPDATA%\\Unminute\n'@ | Set-Content notes.md",
                 "PowerShell"),
                ("@'\nP2 covers %LOCALAPPDATA%\\Unminute\n'@ | gh pr comment 1 --body-file -",
                 "PowerShell"),
                ("cat <<'EOF' > notes.md\nP2 covers %LOCALAPPDATA%\\Unminute\nEOF", "Bash")))

    def test_copies_and_archives_of_local_appdata_are_blocked(self):
        # The owner's target (#221) names copying: a recursive copy or an archive of
        # local AppData or a folder above it copies the data.
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                (r"Copy-Item -Recurse $env:LOCALAPPDATA $env:TEMP\w", "PowerShell"),
                (r"Copy-Item $env:USERPROFILE\AppData -Destination D:\x -Recurse", "PowerShell"),
                (r"cpi -r $env:LOCALAPPDATA\* D:\x", "PowerShell"),
                ("cp -r ~/AppData/Local /tmp/w", "Bash"),
                ("cp -a ~ /d/backup", "Bash"),
                ("rsync -a ~/AppData/Local/ /d/backup", "Bash"),
                (r"robocopy %LOCALAPPDATA% C:\x /e", "PowerShell"),
                (r"robocopy $env:USERPROFILE C:\x /mir", "PowerShell"),
                (r"xcopy /s /e %LOCALAPPDATA% C:\x", "PowerShell"),
                ("tar -czf /tmp/a.tgz ~/AppData/Local", "Bash"),
                ("tar -czf /tmp/a.tgz -C ~/AppData Local", "Bash"),
                (r"Compress-Archive -Path $env:LOCALAPPDATA -DestinationPath x.zip", "PowerShell"),
                ("zip -r a.zip ~/AppData/Local", "Bash"),
                (r"7z a a.7z %LOCALAPPDATA%", "PowerShell")),
                allowed=(
                ("cp -r workspace /tmp/w", "Bash"),
                (r"Copy-Item -Recurse tests $env:TEMP\t", "PowerShell"),
                ("tar -czf dist.tgz dist", "Bash"),
                ("tar -xzf $TEMP/a.tgz -C $TEMP/a", "Bash"),
                (r"Compress-Archive -Path dist -DestinationPath $env:TEMP\d.zip", "PowerShell"),
                (r"robocopy dist $env:TEMP\d /e", "PowerShell"),
                (r"robocopy %LOCALAPPDATA% C:\x a.txt", "PowerShell"),
                (r"Copy-Item $env:LOCALAPPDATA\Unminute\models\a.json .", "PowerShell"),
                (r"Copy-Item -Recurse $env:LOCALAPPDATA\Unminute\models $env:TEMP\m",
                 "PowerShell"),
                ("cp ~/.gitconfig /tmp/g", "Bash")))

    def test_long_commands_are_checked_in_linear_time(self):
        # No catastrophic backtracking or scan of the whole command per match: each
        # long synthetic command (35 to 300 KB) is decided well within the budget,
        # and a quadratic scan would take many seconds (#221).
        bs = "\\"
        body = "PR text `x` $(y) 'q' \"d\" | ; & C:" + bs + "x" + bs + ".." + bs + "y\n"
        commands = (("ls " + bs * 40000 + "Unminute", "PowerShell"),
                    ("git commit " + " ".join(f'-m "para {i}"' for i in range(3000)), "Bash"),
                    ("gh api x " + " ".join(f"-f 'k{i}=v'" for i in range(3000)), "PowerShell"),
                    ("gh pr create --body \"$(cat <<'EOF'\n" + body * 5000 + "EOF\n)\"", "Bash"),
                    ("ls " + "?" * 50000, "Bash"),
                    ("echo " + "$(" * 10000, "Bash"),
                    ('echo "' + "a'b\"c" * 10000, "PowerShell"),
                    ("cat " + "x/../" * 10000 + "a", "Bash"),
                    # Round 1 (#221): runs of closing characters were read once per
                    # character by main's check too (50,000: about 11 s), deep nesting
                    # raised RecursionError, and set-and-used variables rescan the rest.
                    ('echo "' + ")" * 50000, "Bash"),
                    ("echo " + ')"' * 25000, "PowerShell"),
                    ("ls *" + ")" * 50000, "Bash"),
                    ("d=" + '"$(echo ' * 10000 + "x" + ')"' * 10000, "Bash"),
                    ("echo x" + "\n" * 20000, "Bash"),
                    ("echo x" + "\n" * 20000, "PowerShell"),
                    ("for x in" + " " * 20000, "Bash"),
                    ("".join(f"$v{i} = 'a'; Write-Output $v{i}; " for i in range(300)),
                     "PowerShell"))
        for command, name in commands:
            with self.subTest(start=command[:24], length=len(command), tool=name):
                start = time.perf_counter()
                guard.decide(shell(command, name))
                self.assertLess(time.perf_counter() - start, 2.0)

    # Round 1 review of f45268c (#221). Bash spells the data path with Git Bash's drive.
    BASH_DATA = "/c/Users/someone/AppData/Local/Unminute/workspace"

    def test_deeply_nested_substitutions_are_checked_not_crashed(self):
        # Thousands of nested "$( once raised RecursionError, so the hook exited 1
        # (a warning only) and the command ran; main blocks these.
        deep = '"$(' * 1000
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ("x=$(cat <<'EOF'\n" + deep + "\nEOF\n)\ncat " + self.BASH_DATA + "/a.json",
                 "Bash"),
                ("d=" + '"$(echo ' * 1000 + "x" + ')"' * 1000 + "; cat " + self.BASH_DATA
                 + "/settings.json", "Bash"),
                ("$d = (" + deep + "); Get-Content " + self.DATA + r"\a.json", "PowerShell")))

    def test_commands_too_complex_to_check_are_blocked_quickly(self):
        # Values that double at each step grew the command exponentially (the hook
        # ran past its time limit, which lets the call through), and thousands of
        # used variables rescanned the command each time. Blocked as too complex,
        # within the budget; a few hundred variables are still checked as usual.
        doubling = "a=x; b0=$a$a; " + "".join(f"b{i + 1}=$b{i}$b{i}; " for i in range(40))
        loops = "for a0 in x x; do :; done; " + "".join(
            f"for a{i + 1} in $a{i} $a{i}; do :; done; " for i in range(30))
        many = "".join(f"$v{i} = 'a'; Write-Output $v{i}; " for i in range(3000))
        with profile_guard() as fresh:
            for command, name in ((doubling + "echo $b40", "Bash"), (loops, "Bash"),
                                  (many, "PowerShell")):
                with self.subTest(start=command[:20], tool=name):
                    start = time.perf_counter()
                    self.assertEqual(fresh.COMPLEX, fresh.decide(shell(command, name)))
                    self.assertLess(time.perf_counter() - start, 2.0)
            self.check(fresh, allowed=(
                ("".join(f"$v{i} = 'a'; Write-Output $v{i}; " for i in range(200)),
                 "PowerShell"),))

    def test_an_exported_value_is_never_hidden(self):
        # A child process reads an exported value without its name being written,
        # so it is checked where it is set; so is a value set and never used.
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ("export UNMINUTE_DIR='" + self.BASH_DATA + "'; python tools/dump.py", "Bash"),
                ("declare -x D='" + self.BASH_DATA + "'; python x.py", "Bash"),
                ("set -a; D='" + self.BASH_DATA + "'; python x.py", "Bash"),
                ("D='" + self.BASH_DATA + "'; python x.py", "Bash"),
                ("$d = '" + self.DATA + "'; python x.py", "PowerShell")),
                allowed=(
                (r"$b = 'P2 covers %LOCALAPPDATA%\Unminute\workspace'; gh pr comment 1 --body $b",
                 "PowerShell"),
                ("b='P2 covers ~/AppData/Local/Unminute/workspace'; gh pr comment 1 --body \"$b\"",
                 "Bash"),
                ("export PYTHONDONTWRITEBYTECODE=1; python tools/run_tests.py", "Bash")))

    def test_escaped_backticks_in_a_bash_message_are_text(self):
        # bash prints \` as a backtick (a Markdown code span), and runs `...`.
        tick = "\\`"
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ('gh pr comment 228 --body "Now blocks `cat ' + self.BASH_DATA + '/a.json`."',
                 "Bash"),
                ('git commit -m "Blocks `rg -n x ~/AppData/Local`"', "Bash"),
                ('git commit -m "Blocks \\\\`rg -n x ~/AppData/Local\\\\`"', "Bash")),
                allowed=(
                ('gh pr comment 228 --body "Now blocks ' + tick
                 + r"Get-Content %LOCALAPPDATA%\Unminute\workspace\a.json" + tick + '."', "Bash"),
                ('git commit -m "Blocks ' + tick + "rg -n x ~/AppData/Local" + tick + '"', "Bash"),
                ('gh pr create --title x --body "' + tick + "rg -n x ~/AppData/Local" + tick
                 + '"', "Bash")))

    def test_text_piped_to_git_is_text_only_as_a_message(self):
        # git hash-object --stdin-paths reads every file a here-string names into
        # the object store; git commit -F - takes it as the message.
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ("@'\n" + self.DATA + "\\a.json\n'@ | git hash-object -w --stdin-paths",
                 "PowerShell"),
                ("@'\n" + self.DATA + "\\a.json\n'@ | git update-index --add --stdin",
                 "PowerShell")),
                allowed=(
                ("@'\nBlocks " + self.DATA + "\\a.json\n'@ | git commit -F -", "PowerShell"),
                ("@'\nBlocks " + self.DATA + "\n'@ | git tag -a v1 --file=-", "PowerShell")))

    def test_a_heredoc_of_file_names_is_checked(self):
        # Programs that read file names from their input (tar -T, zip -@, rsync
        # --files-from, cpio, git --stdin-paths) read the files a heredoc names.
        names = "\n" + self.BASH_DATA + "/a.json\nEOF"
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ("tar czf /tmp/x.tgz -T - <<'EOF'" + names, "Bash"),
                ("tar --files-from=- -czf /tmp/x.tgz <<'EOF'" + names, "Bash"),
                ("zip -@ x.zip <<'EOF'" + names, "Bash"),
                ("rsync -a --files-from=- / /tmp/x <<'EOF'" + names, "Bash"),
                ("cpio -o > /tmp/x.cpio <<'EOF'" + names, "Bash"),
                ("git hash-object -w --stdin-paths <<'EOF'" + names, "Bash"),
                ("git add --pathspec-from-file=- <<'EOF'" + names, "Bash")),
                allowed=(
                ("tar czf /tmp/x.tgz -T - <<'EOF'\nworkspace\ntests\nEOF", "Bash"),
                ("tar czf /tmp/x.tgz dist <<'EOF'" + names, "Bash"),
                ("cat <<'EOF' > notes.md" + names, "Bash")))

    def test_folders_from_dirname_and_parameter_operators_are_seen(self):
        # dirname, Split-Path and bash's ${NAME:-x} were followed only after a cd.
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ('grep -r decision "$(dirname "$TEMP")"', "Bash"),
                ("ls -R $(dirname $LOCALAPPDATA/x)", "Bash"),
                ("rg -n x `dirname $TEMP`", "Bash"),
                ("gci -r (Split-Path -Parent $env:TEMP)", "PowerShell"),
                ("Get-ChildItem -Recurse (Split-Path $env:TEMP) -Filter *.json", "PowerShell"),
                ('ls -R "${LOCALAPPDATA:-x}"', "Bash"),
                ('cat "${LOCALAPPDATA:-}/Unminute/settings.json"', "Bash"),
                ('rg -n x "${HOME%/}"', "Bash"),
                ("ls ${LOCALAPPDATA%/}/Unminute/workspace", "Bash"),
                # Round 2: suffix removal, substitution or a substring can climb from
                # Temp or Roaming to the folder above (the dirname idiom); read both
                # as the folder and as the folder above it.
                ('grep -r decision "${TEMP%\\\\*}"', "Bash"),
                ('rg -n decision "${TMP%\\\\*}"', "Bash"),
                ('cp -r "${TEMP%\\\\*}" /tmp/x', "Bash"),
                ('ls -R "${APPDATA%\\\\Roaming}"', "Bash"),
                ('grep -r decision "${APPDATA%\\\\*}/Local"', "Bash"),
                ('grep -r decision "${TEMP%%\\\\Temp}"', "Bash"),
                ('rg -n x "${TEMP/\\\\Temp/}"', "Bash"),
                ('ls -R "${TEMP:0:30}"', "Bash"),
                ('cd "${TEMP%\\\\*}" && grep -r decision .', "Bash"),
                ('cat "${LOCALAPPDATA%/}/Unminute/settings.json"', "Bash"),
                ('cat "${TEMP%x}/../Unminute/settings.json"', "Bash")),
                allowed=(
                ('cd "$(dirname "$0")" && ls', "Bash"),
                ("Get-ChildItem (Split-Path $env:TEMP) -Directory", "PowerShell"),
                ('echo "${TEMP%/}"; ls "${HOME}/Local_Repos"', "Bash"),
                ('rg -n x "$(dirname "$TEMP")/Temp/review"', "Bash"),
                ('ls "${HOME%/}/Local_Repos"', "Bash"),
                ('rg -n x "${TEMP%/}/review"', "Bash"),
                ('ls "${TEMP%\\\\*}"', "Bash")))

    def test_quoted_words_are_not_the_heredocs_program(self):
        # Round 2: the program that takes a heredoc was found anywhere on its line,
        # including a quoted title, so a PR body that quotes a data path was checked
        # as names whenever the title held Python, Bash, read or tar -T. A quoted
        # program path and a string another shell runs are still seen.
        body = (" --body-file - <<'EOF'\nThe guard now blocks `Get-Content "
                "$env:LOCALAPPDATA\\Unminute\\workspace\\a.json`.\nEOF")
        code = "\nprint(open(r'" + self.DATA + "\\a.json').read())\nEOF"
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                ('"C:/Program Files/Python314/python.exe" - <<\'EOF\'' + code, "Bash"),
                ("bash -c \"python -\" <<'EOF'" + code, "Bash"),
                ("\"python\" - <<'EOF'" + code, "Bash"),
                ("sh -c 'xargs cat' <<'EOF'\n" + self.BASH_DATA + "/a.json\nEOF", "Bash"),
                ("@'" + code[:-4] + "\n'@ | & \"C:\\Program Files\\Python314\\python.exe\" -",
                 "PowerShell"),
                # Round 3: an escaped quote or a comment must not hide the program
                # that reads a here-string's lines (main blocks these).
                ("@'\n" + self.DATA + "\\settings.json\n'@ | Write-Output `\""
                 " | ForEach-Object { Get-Content $_ } # \"x y\"", "PowerShell"),
                ("@'\n" + self.DATA + "\\settings.json\n'@ | Out-String -Stream <# \" #>"
                 " | ForEach-Object { Get-Content $_ } <# \" #>", "PowerShell"),
                # Round 4: a pipeline that goes on to the next line (main blocks these).
                ("@'\n" + self.DATA + "\\settings.json\n'@ |\nForEach-Object { Get-Content $_ }",
                 "PowerShell"),
                ("@'\r\n" + self.DATA + "\\settings.json\r\n'@ |\r\n    Get-Content",
                 "PowerShell"),
                ("@'\n" + self.DATA + "\\settings.json\n'@\n| ForEach-Object { Get-Content $_ }",
                 "PowerShell"),
                ("@'\n" + self.DATA + "\\settings.json\n'@ | Write-Output |\n"
                 "ForEach-Object { Get-Content $_ }", "PowerShell"),
                # Round 5: dropped only on a text-only consumer on its own lines; a
                # blank line, a backtick, a bracket or no consumer keeps it checked.
                ("@'\n" + self.DATA + "\\settings.json\n'@ |\n\nForEach-Object { Get-Content $_ }",
                 "PowerShell"),
                ("@'\n" + self.DATA + "\\settings.json\n'@ | Write-Output `\n"
                 "  -NoEnumerate | ForEach-Object { Get-Content $_ }", "PowerShell"),
                ("Get-Content -Path (\n@'\n" + self.DATA + "\\settings.json\n'@\n)", "PowerShell"),
                ("foreach ($f in (\n@'\n" + self.DATA + "\\settings.json\n'@\n))"
                 " { Get-Content $f }", "PowerShell"),
                ("$p = @(\n@'\n" + self.DATA + "\\settings.json\n'@\n)\nGet-Content $p",
                 "PowerShell"),
                ("@'\nBlocks " + self.DATA + "\n'@ |\n  gh pr create --title \"Notes\""
                 " --body-file -", "PowerShell")),
                allowed=tuple(
                ("gh pr create --title \"" + title + "\"" + body, "Bash") for title in (
                    "Move to Python 3.14", "Bash and PowerShell guard",
                    "Use python -I for the child", "Tests read synthetic files only",
                    "Meeting content read and written only through the store's codec",
                    "Guard: check tar -T heredocs", "Guard: --files-from heredocs",
                    "Guard: zip -@ heredocs")) + (
                ("@'\nBlocks " + self.DATA + "\n'@ | gh pr create --title \"Move to Python 3.14\""
                 " --body-file -", "PowerShell"),
                ))

    def test_text_kept_in_a_variable_is_checked(self):
        # Round 2: a here-string given to a program that takes text but also keeps
        # it in a variable (Tee-Object -Variable, -OutVariable and the like) was
        # dropped as written text, and the variable then read the data.
        data = "@'\n" + self.DATA + "\n'@"
        with profile_guard() as fresh:
            self.check(fresh, blocked=(
                (data + ' | Tee-Object -Variable p\nGet-Content "$p\\settings.json"', "PowerShell"),
                (data + " | Tee-Object -Variable p | Out-Null; Get-ChildItem -Recurse $p",
                 "PowerShell"),
                (data + " | Write-Output -OutVariable p | Out-Null; gci -r $p", "PowerShell"),
                (data + " | Write-Output -ov p | Out-Null; gci -r $p", "PowerShell"),
                (data + " | Out-String -OutVariable p", "PowerShell"),
                (data + " | Out-Null -OutVariable p", "PowerShell"),
                (data + " | Write-Host -InformationVariable p", "PowerShell"),
                (data + " | Tee-Object -va p", "PowerShell"),
                ("Write-Output -InputObject " + data + " -OutVariable p", "PowerShell")),
                allowed=(
                ("@'\nBlocks " + self.DATA + "\n'@ | Tee-Object -FilePath notes.md", "PowerShell"),
                ("@'\nBlocks " + self.DATA + "\n'@ | Set-Content -Path notes.md", "PowerShell"),
                ("@'\nBlocks " + self.DATA + "\n'@ | git commit -v -F -", "PowerShell")))

    # A corpus of the plain and common ways to read, list, search or copy the data,
    # and of ordinary work, so that no later change regresses either silently.
    COMMON_BLOCKED = (
        ("cat ~/AppData/Local/Unminute/workspace/settings.json", "Bash"),
        ("head -n 5 ~/AppData/Local/Unminute/workspace/settings.json", "Bash"),
        ("less $LOCALAPPDATA/Unminute/workspace/settings.json", "Bash"),
        ('sqlite3 "$LOCALAPPDATA/Unminute/workspace/workspace.db" .dump', "Bash"),
        (r"gc $env:LOCALAPPDATA\Unminute\workspace\settings.json", "PowerShell"),
        (r'Get-Content -LiteralPath "$env:LOCALAPPDATA\Unminute\workspace\a.json"', "PowerShell"),
        (r'[IO.File]::ReadAllText("$env:LOCALAPPDATA\Unminute\workspace\a.json")', "PowerShell"),
        (r"Import-Csv $env:LOCALAPPDATA\Unminute\workspace\a.csv", "PowerShell"),
        ("echo ~/AppData/Local/Unminute/workspace/a.json | xargs cat", "Bash"),
        ("ls -la ~/AppData/Local/Unminute/workspace", "Bash"),
        ("ls ~/AppData/Local/Unminute", "Bash"),
        ("find ~/AppData/Local -name '*.db'", "Bash"),
        ("find ~ -name workspace.db", "Bash"),
        ("rg -n budget ~/AppData/Local", "Bash"),
        ("grep -rn budget ~", "Bash"),
        (r"Get-ChildItem $env:LOCALAPPDATA\Unminute -Recurse", "PowerShell"),
        (r"gci -r $env:LOCALAPPDATA -Filter *.db", "PowerShell"),
        (r"dir $env:LOCALAPPDATA\Unminute\workspace", "PowerShell"),
        (r"Get-ChildItem -Path $env:USERPROFILE -Recurse -Include *.db", "PowerShell"),
        (r"Select-String -Path $env:LOCALAPPDATA\Unminute\workspace\*.json -Pattern x",
         "PowerShell"),
        (r"tree /f $env:LOCALAPPDATA\Unminute", "PowerShell"),
        (r"tree /f %LOCALAPPDATA%", "PowerShell"),
        ("cp -r ~/AppData/Local/Unminute/workspace /tmp/w", "Bash"),
        ("cp ~/AppData/Local/Unminute/workspace/workspace.db .", "Bash"),
        (r"Copy-Item -Recurse $env:LOCALAPPDATA\Unminute\workspace $env:TEMP\w", "PowerShell"),
        (r"robocopy $env:LOCALAPPDATA\Unminute C:\x /e", "PowerShell"),
        ("tar -czf /tmp/a.tgz -C ~/AppData/Local Unminute", "Bash"),
        (r"Compress-Archive -Path $env:LOCALAPPDATA\Unminute -DestinationPath x.zip", "PowerShell"),
        (r"cd $env:LOCALAPPDATA\Unminute; gc workspace\settings.json", "PowerShell"),
        ("cd ~/AppData/Local/Unminute && cat workspace/settings.json", "Bash"),
        ("cd ~/AppData/Local && ls -R", "Bash"),
        (r"Get-ChildItem ([Environment]::GetFolderPath('LocalApplicationData')) -Recurse",
         "PowerShell"),
        (r"Get-Content (Join-Path ([Environment]::GetFolderPath('LocalApplicationData'))"
         r" 'Unminute\settings.json')", "PowerShell"),
        (r"$l = [Environment]::GetFolderPath('LocalApplicationData'); gc $l\Unminute\a.json",
         "PowerShell"),
        (r"$l = $env:LOCALAPPDATA; gci -r $l", "PowerShell"),
        ("git add ~/AppData/Local/Unminute/workspace/a.json", "Bash"),
        ("git diff --no-index ~/AppData/Local/Unminute/workspace/a.json b.json", "Bash"),
        ("python -c \"print(open(r'C:\\Users\\someone\\AppData\\Local\\Unminute\\workspace"
         "\\a.json').read())\"", "Bash"),
        # bash runs an unescaped backtick inside a double-quoted message (#221 round 1).
        ('gh pr comment 1 --body "See `cat ~/AppData/Local/Unminute/workspace/a.json`"', "Bash"),
        ("export D=~/AppData/Local/Unminute/workspace; python tools/dump.py", "Bash"),
        ("tar czf /tmp/x.tgz -T - <<'EOF'\n/c/Users/someone/AppData/Local/Unminute\nEOF", "Bash"),
        ('rg -n x "$(dirname "$TEMP")"', "Bash"),
    )
    COMMON_ALLOWED = (
        ("python tools/run_tests.py --strict-skips", "Bash"),
        ("ruff check .", "Bash"),
        ("git status", "Bash"), ("git diff --stat", "Bash"), ("git log --oneline -5", "Bash"),
        ("rg -n budget workspace", "Bash"), ("grep -rn budget workspace tests", "Bash"),
        ("find . -name '*.py' -newer AGENTS.md", "Bash"),
        ("Get-ChildItem -Recurse workspace -Filter *.py", "PowerShell"),
        (r"Get-ChildItem $env:LOCALAPPDATA\Unminute\models", "PowerShell"),
        ("python -m workspace --status", "PowerShell"),
        ("ls $TEMP", "Bash"), (r"Get-ChildItem $env:TEMP", "PowerShell"),
        (r"Remove-Item -Recurse $env:TEMP\umv-scratch", "PowerShell"),
        ("gh pr create --draft --title x --body \"$(cat <<'EOF'\n"
         "P2 blocks %LOCALAPPDATA%\\Unminute\\workspace\nEOF\n)\"", "Bash"),
        ("gh pr checks 221", "Bash"), ("gh issue view 221 --comments", "Bash"),
        (r"Get-ChildItem $env:LOCALAPPDATA -Directory | Select-Object Name", "PowerShell"),
        ("ls ~", "Bash"), ("ls ~/AppData/Local", "Bash"),
        (r"cd C:\Users\someone\Local_Repos\unminute-221; git status", "PowerShell"),
        ("cd ../unminute && git log -1", "Bash"),
        (r"Get-Content C:\Users\someone\Local_Repos\unminute\workspace\app.py", "PowerShell"),
        ("python -m py_compile workspace/store.py", "Bash"),
        ("node --check workspace/ui/app.js", "Bash"),
        ("Invoke-ScriptAnalyzer -Path install.ps1", "PowerShell"),
        ("python tools/changelog.py --check", "Bash"),
        ("git worktree add -b x ../unminute-x origin/main", "Bash"),
        ("$env:PYTHONDONTWRITEBYTECODE=1; python -m unittest tests.test_store", "PowerShell"),
        # An escaped backtick is a Markdown code span bash prints (#221 round 1).
        ('gh pr comment 1 --body "Blocks \\`cat %LOCALAPPDATA%\\Unminute\\workspace\\a.json\\`"',
         "Bash"),
        ('git commit -m "Guard: block \\`rg -n x ~/AppData/Local\\`"', "Bash"),
        ("@'\nGuard: block %LOCALAPPDATA%\\Unminute\\workspace\n'@ | git commit -F -",
         "PowerShell"),
        # A title is not the program that takes the body (#221 round 2).
        ("gh pr create --title \"Move to Python 3.14\" --body-file - <<'EOF'\n"
         "The guard now blocks `cat $LOCALAPPDATA/Unminute/workspace/a.json`.\nEOF", "Bash"),
        ("gh pr create --title \"Tests read synthetic files only\" --body-file - <<'EOF'\n"
         "P2 blocks %LOCALAPPDATA%\\Unminute\\workspace\nEOF", "Bash"),
    )

    def test_common_phrasings_corpus(self):
        with profile_guard() as fresh:
            self.check(fresh, blocked=self.COMMON_BLOCKED, allowed=self.COMMON_ALLOWED)


class HookProcessTests(unittest.TestCase):
    def run_hook(self, payload):
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=30)

    def test_block_exits_2_with_a_reason(self):
        result = self.run_hook(shell("cat extracted/notes.md"))
        self.assertEqual(2, result.returncode)
        self.assertIn("P2", result.stderr)

    def test_allow_exits_0_quietly(self):
        result = self.run_hook(shell("git status"))
        self.assertEqual((0, "", ""), (result.returncode, result.stdout, result.stderr))

    @unittest.skipUnless(sys.platform == "win32" and os.environ.get("LOCALAPPDATA", "")
                         .lower().startswith("c:\\users\\"), "local AppData under C:\\Users")
    def test_the_real_hook_blocks_git_bash_and_wrapped_searches(self):
        # With this computer's own profile (R13b round 3 review).
        for payload in (tool("Grep", pattern="budget", path="/c/Users"),
                        tool("Glob", path="/c/Users", pattern="**/settings.json"),
                        shell("grep -rn budget /c/Users"),
                        shell('timeout 60 rg -n budget "$LOCALAPPDATA"'),
                        shell('pwsh -c "rg -n budget $env:LOCALAPPDATA"', "PowerShell"),
                        shell('$p = "$env:LOCALAPPDATA" + "\\Unminute\\workspace\\settings.json";'
                              " Get-Content $p", "PowerShell"),
                        # round 4: ~\.., a whole $(...) value, an empty popd
                        shell(r"rg -n budget ~\..", "PowerShell"),
                        shell('L=$(cygpath "$LOCALAPPDATA"); rg -n budget "$L"'),
                        shell(r"cd $env:LOCALAPPDATA; popd; rg -n budget", "PowerShell")):
            with self.subTest(payload=payload):
                self.assertEqual(2, self.run_hook(payload).returncode)

    def test_deep_nesting_exits_2_not_1(self):
        # #221 round 1: a RecursionError made the hook exit 1, which only warns, so
        # the command ran. The data path is a synthetic profile's; main blocks it.
        command = ("x=$(cat <<'EOF'\n" + '"$(' * 1000 + "\nEOF\n)\n"
                   "cat /c/Users/someone/AppData/Local/Unminute/workspace/a.json")
        result = self.run_hook(shell(command))
        self.assertEqual(2, result.returncode, result.stderr[-300:])

    @staticmethod
    def stdin(payload):
        """Standard input holding the payload as Claude Code sends it: UTF-8 bytes."""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return io.TextIOWrapper(io.BytesIO(data), encoding="cp1252")

    def test_an_error_while_checking_blocks_without_content(self):
        # Any error in the checks blocks the call (exit 2) and names only its type.
        command = "cat /c/Users/someone/private-name.json"
        long_command = command + " # " + "x" * (guard.CHILD_OVER + 10)
        for error in (RecursionError, MemoryError, ValueError, UnicodeEncodeError):
            made = (error("utf-8", "private-name", 0, 1, "private-name")
                    if error is UnicodeEncodeError else error("private-name"))
            # #221 round 2: also an error on the way to the child process, which
            # escaped the check and exited 1 (a warning only).
            for target, payload in (("decide", shell(command)),
                                    ("_checked_in_child", shell(long_command))):
                stderr = io.StringIO()
                with self.subTest(error=error.__name__, target=target), \
                        mock.patch.object(guard, target, side_effect=made), \
                        mock.patch.object(sys, "stdin", self.stdin(payload)), \
                        contextlib.redirect_stderr(stderr):
                    self.assertEqual(2, guard.main())
                    self.assertIn(error.__name__, stderr.getvalue())
                    self.assertNotIn("private-name", stderr.getvalue())

    def test_a_child_process_that_cannot_start_blocks(self):
        # #221 round 2: checking a long call here instead would have no time limit.
        payload = shell("git status # " + "x" * (guard.CHILD_OVER + 10))
        with mock.patch("subprocess.run", side_effect=OSError("no process")):
            code, reason = guard._decide_bounded(payload, b"{}", False)
        self.assertEqual(2, code)
        self.assertIn("could not start", reason)

    def test_utf8_input_is_read_as_utf8(self):
        # #221 round 2: standard input was read in the ANSI code page, so a Maori
        # macron (UTF-8 C4 81 or C5 8D, bytes cp1252 lacks) became a lone surrogate.
        # A long command then failed the encode for its child process with exit 1,
        # which only warns, and ran unchecked. Sent as Claude Code sends it: UTF-8.
        macrons = " M\u0101ori T\u014dku"
        data_path = "cat /c/Users/someone/AppData/Local/Unminute/workspace/settings.json"
        cases = ((data_path + " # " + "x" * 25000 + macrons, 2),
                 (data_path + " #" + macrons, 2),
                 ("cat <<'EOF' > notes.md\n" + ("T\u014dku M\u0101ori\n" * 3000) + "EOF", 0),
                 ("git commit -m 'Kia ora" + macrons + "'", 0))
        for command, expected in cases:
            data = json.dumps(shell(command), ensure_ascii=False).encode("utf-8")
            with self.subTest(length=len(command), expected=expected):
                result = subprocess.run([sys.executable, str(HOOK)], input=data,
                                        capture_output=True, timeout=60)
                self.assertEqual(expected, result.returncode,
                                 result.stderr.decode("utf-8", "replace")[-300:])
            with self.subTest(in_process=True, length=len(command)), \
                    mock.patch.object(sys, "stdin", self.stdin(shell(command))), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(expected, guard.main())

    def test_a_command_its_variables_grow_is_checked_under_the_time_limit(self):
        # #221 round 2: only a command over CHILD_OVER characters went to the child
        # process, but variables can grow a short one to a megabyte, and some later
        # scans are quadratic: an 8 KB command ran past Claude Code's 600 s hook
        # limit, which lets the call through. A command that grows past CHILD_OVER is
        # now checked in the child, under its time limit.
        def run(payload, limit=None):
            env = dict(os.environ)
            if limit is not None:
                env[guard.TIME_LIMIT_VARIABLE] = limit
            return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                                  capture_output=True, text=True, timeout=60, env=env)

        crafted = ("a='" + "<<A\n" * 2000 + "'\necho " + "$a " * 100
                   + "\ncat /c/Users/someone/AppData/Local/Unminute/workspace/a.json")
        self.assertLess(len(crafted), guard.CHILD_OVER)
        start = time.perf_counter()
        result = run(shell(crafted), "1")
        self.assertEqual(2, result.returncode)
        self.assertLess(time.perf_counter() - start, 30)
        # An ordinary short command whose variable is used often is checked in the
        # child (with no time at all it is blocked, which shows the route) and
        # allowed there with the usual limit.
        ordinary = shell("t='" + "x" * 5000 + "'; echo \"$t\" \"$t\" \"$t\" \"$t\" \"$t\"")
        self.assertLess(len(ordinary["tool_input"]["command"]), guard.CHILD_OVER)
        result = run(ordinary, "0")
        self.assertEqual(2, result.returncode)
        self.assertIn("took too long", result.stderr)
        result = run(ordinary)
        self.assertEqual((0, ""), (result.returncode, result.stderr))
        # In the hook's own process the check stops as soon as the command grows
        # past the limit; without one (the child), it is checked as before.
        for payload in (shell(crafted), ordinary):
            with self.assertRaises(guard.NeedsChild):
                guard.decide(payload, grown_limit=guard.CHILD_OVER)
        self.assertIsNone(guard.decide(ordinary))

    def test_a_long_command_is_checked_in_a_child_under_a_time_limit(self):
        # #221 round 1: Claude Code lets a call through when the hook runs past its
        # time limit, so a long command is checked in a child process and blocked
        # when that takes too long. The limit can be lowered (here, to nothing).
        filler = " ".join(f"-m 'para {i}'" for i in range(3000))      # about 40 KB
        ordinary = shell("git commit " + filler)
        private = shell("git commit " + filler + "; cat extracted/notes.md")
        self.assertGreater(len(ordinary["tool_input"]["command"]), guard.CHILD_OVER)
        result = self.run_hook(ordinary)
        self.assertEqual((0, ""), (result.returncode, result.stderr))
        result = self.run_hook(private)
        self.assertEqual(2, result.returncode)
        self.assertIn("extracted/", result.stderr)
        env = {**os.environ, guard.TIME_LIMIT_VARIABLE: "0"}
        result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(ordinary),
                                capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(2, result.returncode)
        self.assertIn("took too long", result.stderr)
        # The variable may only lower the limit.
        for value, expected in (("0.5", 0.5), ("1000", guard.TIME_LIMIT), ("x", guard.TIME_LIMIT),
                                ("-3", 0.0)):
            with self.subTest(value=value), mock.patch.dict(
                    os.environ, {guard.TIME_LIMIT_VARIABLE: value}):
                self.assertEqual(expected, guard._time_limit())

    def test_unreadable_input_warns_without_blocking(self):
        # Exit 1 is shown to the user but does not block; 2 would block everything.
        for text in ("not json", "", "[]"):
            with self.subTest(text=text):
                result = subprocess.run([sys.executable, str(HOOK)], input=text,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(1, result.returncode)
                self.assertIn("not active", result.stderr)

    def test_configured_command_blocks_through_powershell(self):
        # Runs the exact command from settings.json the way Claude Code does with
        # "shell": "powershell". Catches the exit code being lost (2 becoming 1)
        # and stdin not reaching Python.
        shell_exe = shutil.which("powershell") or shutil.which("pwsh")
        if not shell_exe:
            self.skipTest("PowerShell not installed")
        if not shutil.which("python"):                # the configured command runs "python"
            self.skipTest("python not on PATH")
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

        def run(payload, project):
            env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
            return subprocess.run([shell_exe, "-NoProfile", "-NonInteractive", "-Command",
                                   command], input=json.dumps(payload),
                                  capture_output=True, text=True, timeout=60, env=env)

        for payload, expected in ((shell("git add meeting.m4a"), 2), (shell("git status"), 0)):
            with self.subTest(command=payload["tool_input"]["command"]):
                result = run(payload, ROOT)
                self.assertEqual(expected, result.returncode, result.stderr)
        # #221 round 2: UTF-8 input through PowerShell, with a Maori macron in a long
        # command (the child process) and a short one.
        for text in ("cat /c/Users/someone/AppData/Local/Unminute/workspace/a.json # "
                     + "x" * 25000 + " M\u0101ori",
                     "cat /c/Users/someone/AppData/Local/Unminute/workspace/a.json # M\u0101ori"):
            data = json.dumps(shell(text), ensure_ascii=False).encode("utf-8")
            with self.subTest(length=len(text)):
                result = subprocess.run([shell_exe, "-NoProfile", "-NonInteractive", "-Command",
                                         command], input=data, capture_output=True,
                                        timeout=60, env={**os.environ,
                                                         "CLAUDE_PROJECT_DIR": str(ROOT)})
                self.assertEqual(2, result.returncode,
                                 result.stderr.decode("utf-8", "replace")[-300:])
        # A checkout from before the hook existed has no hook file. That must
        # allow, never block: exit 2 there would stop every tool call, including
        # the git command needed to get back.
        with tempfile.TemporaryDirectory() as empty:
            result = run(shell("git add meeting.m4a"), empty)
            self.assertNotEqual(2, result.returncode, result.stderr)


class SettingsTests(unittest.TestCase):
    def test_settings_deny_private_paths_and_run_the_hook(self):
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        deny = settings["permissions"]["deny"]
        self.assertIn("Read(./extracted/**)", deny)
        self.assertTrue(any("whisper/workspace" in rule for rule in deny))
        for rule in ("Read(~/AppData/Local/Unminute/workspace/**)",
                     "Edit(~/AppData/Local/Unminute/workspace/**)"):
            self.assertIn(rule, deny)
        hooks = [hook for entry in settings["hooks"]["PreToolUse"] for hook in entry["hooks"]]
        guards = [h for h in hooks if ".claude/hooks/private_data_guard.py" in h["command"]]
        self.assertEqual(1, len(guards))
        self.assertTrue(HOOK.is_file())
        # Claude Code runs hook commands in Git Bash unless told otherwise. Where Git
        # Bash is missing, the bash form found no project folder and blocked every
        # tool call; PowerShell is always present on Windows.
        self.assertEqual("powershell", guards[0].get("shell"))
        self.assertIn("$env:CLAUDE_PROJECT_DIR", guards[0]["command"])
        # Without this PowerShell reports the hook's exit 2 as 1, which only warns.
        self.assertTrue(guards[0]["command"].rstrip().endswith("exit $LASTEXITCODE"))
        self.assertIn("Test-Path", guards[0]["command"])     # a missing hook file allows
        # Every tool the hook checks is one the matcher sends it (Monitor runs a
        # shell command, R13b review).
        entry = next(e for e in settings["hooks"]["PreToolUse"]
                     if any(".claude/hooks/private_data_guard.py" in h["command"]
                            for h in e["hooks"]))
        matched = set(entry["matcher"].split("|"))
        self.assertEqual(set(), (set(guard.FILE_TOOLS) | set(guard.SHELL_TOOLS)) - matched)
        self.assertIn("Monitor", matched)

    def test_media_extensions_cover_the_apps_import_list(self):
        source = (ROOT / "workspace" / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = next(ast.literal_eval(node.value) for node in ast.walk(tree)
                       if isinstance(node, ast.Assign)
                       and any(getattr(t, "id", None) == "AUDIO_EXTENSIONS" for t in node.targets))
        self.assertEqual(set(), set(imports) - set(guard.MEDIA_EXTENSIONS))


if __name__ == "__main__":
    unittest.main()
