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
                        r"gh api repos/o/r --field body=@$env:LOCALAPPDATA\Unminute\workspace"):
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
                        "git check-ignore -v extracted/x"):
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
                        "git commit -F - <<'EOF'\nStop staging *.wav files in extracted/\nEOF"):
            for name in ("Bash", "PowerShell"):
                with self.subTest(command=command, tool=name):
                    self.assertIsNone(guard.decide(shell(command, name)))

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

    def test_media_extensions_cover_the_apps_import_list(self):
        source = (ROOT / "workspace" / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = next(ast.literal_eval(node.value) for node in ast.walk(tree)
                       if isinstance(node, ast.Assign)
                       and any(getattr(t, "id", None) == "AUDIO_EXTENSIONS" for t in node.targets))
        self.assertEqual(set(), set(imports) - set(guard.MEDIA_EXTENSIONS))


if __name__ == "__main__":
    unittest.main()
