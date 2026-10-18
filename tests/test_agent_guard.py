# tests/test_agent_guard.py
# The Claude Code guard rail in .claude/ (AGENTS.md, invariants P1 and P2): it
# blocks agent access to real meeting content and the staging of media files,
# and leaves ordinary work, temporary synthetic audio and the repo's own
# workspace/ package alone. Stdlib only.

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
                            tool("Read", file_path="unminute/workspace/store.py"),
                            tool("Read", cwd=r"C:\Users\x\Local_Repos",
                                 file_path=r"unminute\workspace\app.py"),
                            # ...and one in a temporary folder, which is under local AppData
                            tool("Read", file_path=r"C:\Users\x\AppData\Local\Temp\unminute"
                                                   r"\workspace\app.py"),
                            tool("Read", cwd=r"C:\Users\x\AppData\Local\Temp\review",
                                 file_path=r"unminute\workspace\app.py"),
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
        # Elsewhere, unminute\workspace is a clone of this repository.
        for cwd in (r"C:\Users\x\Local_Repos", local + r"\Temp\review"):
            payload = tool("PowerShell", cwd=cwd, command=r"Get-Content unminute\workspace\app.py")
            with self.subTest(cwd=cwd):
                self.assertIsNone(guard.decide(payload))
        for command in (r"cd ..; Get-Content unminute\workspace\app.py",
                        r"Get-Content ..\unminute\workspace\app.py"):
            with self.subTest(command=command):
                self.assertIsNone(guard.decide(tool("PowerShell", cwd=local + r"\Temp\s\other",
                                                    command=command)))

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
                        "ls unminute/workspace",
                        r"Get-Content C:\Users\x\AppData\Local\Temp\unminute\workspace\app.py",
                        r"cd $env:LOCALAPPDATA\Temp; ls unminute\workspace",
                        r"cd $env:LOCALAPPDATA; cd Temp\review; Get-Content unminute\workspace\a",
                        r"gh api repos/o/r/issues/1 --raw-field body='%LOCALAPPDATA%\Unminute'",
                        r"Set-Content -Path notes.md -Value '%LOCALAPPDATA%\Unminute\workspace'",
                        r"Get-ChildItem $env:LOCALAPPDATA\Unminute\models",
                        r"Get-ChildItem $env:LOCALAPPDATA\Programs\Unminute",
                        r"Test-Path $env:LOCALAPPDATA\Unminute2",
                        r"cd $env:LOCALAPPDATA\Temp\s; git clone x unminute; ls unminute\workspace",
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
                    r"Get-Content unminute\workspace\app.py")))

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
                ("python -c \"from pathlib import Path; print(list(Path.home().rglob('*.db')))\"",
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
                ("python -c \"import glob; print(glob.glob('C:/Users/**/*.db', recursive=True))\"",
                 "Bash"),
                ("python -c \"import os; print(list(os.walk(os.environ['USERPROFILE'])))\"",
                 "Bash"),
                ("python -c \"import os; print(list(os.walk(os.getenv('HOME'))))\"", "Bash"),
                ("node -e \"require('fs').readdirSync(process.env.USERPROFILE, "
                 "{recursive: true})\"", "Bash"),
                # a relative start, run after a cd to a folder above local AppData
                ("cd ~ && python -c \"import os; print([r for r, d, f in os.walk('.')])\"",
                 "Bash"),
                ("cd /c/Users && python -c \"import os; print([r for r, d, f in os.walk('.')])\"",
                 "Bash"),
                ("cd ~/.. && python -c \"import os; print(list(os.walk('.')))\"", "Bash"),
                ("Set-Location C:\\Users; python -c \"import glob; print(glob.glob('**/*.db', "
                 "recursive=True))\"", "PowerShell")):
            with self.subTest(command=command):
                self.assertIsNotNone(guard.decide(shell(command, name)))
        for command, name in (
                ("du -sh workspace", "Bash"),
                ("where python", "PowerShell"),
                ("forfiles /p workspace /m *.py", "PowerShell"),
                ("python -c \"import glob; print(glob.glob('workspace/**/*.py', "
                 "recursive=True))\"", "Bash"),
                ("python -c \"import os; print(os.listdir(os.environ['LOCALAPPDATA']))\"", "Bash"),
                ("python -c \"from pathlib import Path; print(list((Path.home() / "
                 "'Local_Repos').rglob('*.md')))\"", "Bash"),
                ("python -c \"import os; print([r for r, d, f in os.walk('.')])\"", "Bash"),
                ("python -c \"import os; print(list(os.walk('workspace')))\"", "PowerShell"),
                ("python -c \"import pathlib; print(list(pathlib.Path('tests').rglob('*.py')))\"",
                 "Bash"),
                ("cd workspace && python -c \"import os; print(list(os.walk('.')))\"", "Bash")):
            with self.subTest(command=command):
                self.assertIsNone(guard.decide(shell(command, name)))

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
        self.assertTrue(any("Unminute/workspace" in rule for rule in deny))
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
