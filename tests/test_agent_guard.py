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
        for path in (local + r"\whisper\workspace\workspace.db",
                     "C:/Users/someone/AppData/Local/whisper/workspace/media/1/audio.f32"):
            with self.subTest(path=path):
                self.assertIsNotNone(guard.decide(tool("Read", file_path=path)))

    def test_unminute_workspace_is_blocked(self):
        # The renamed data folder (#136), in the forms the old one is checked in
        # above. The old folder stays covered: it keeps real content until deleted.
        local = os.environ.get("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
        for path in (local + r"\Unminute\workspace\workspace.db",
                     "C:/Users/someone/AppData/Local/Unminute/workspace/media/1/audio.f32",
                     r"C:\Users\someone\AppData\Local\unminute\WORKSPACE",
                     "~/AppData/Local/Unminute/workspace/settings.json",
                     r"C:\Users\someone\AppData\Local\Temp\..\Unminute\workspace\workspace.db"):
            for payload in (tool("Read", file_path=path), tool("Edit", file_path=path),
                            tool("Glob", pattern="**/*", path=path),
                            tool("Grep", pattern="anything", path=path)):
                with self.subTest(payload=payload):
                    self.assertIsNotNone(guard.decide(payload))

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

    def test_ordinary_paths_are_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            unrelated = Path(temp) / "extracted" / "build.txt"     # no AGENTS.md beside it
            for payload in (tool("Read", file_path=str(ROOT / "workspace" / "store.py")),
                            tool("Read", file_path="workspace/app.py"),
                            tool("Write", file_path=str(Path(temp) / "probe.wav")),
                            tool("Read", file_path=str(unrelated)),
                            tool("Read", file_path=r"C:\Users\x\Local_Repos\unminute-r7"
                                                   r"\workspace\app.py"),
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
                        r'Get-Content "$env:LOCALAPPDATA\whisper\workspace\settings.json"',
                        "sqlite3 ~/AppData/Local/whisper/workspace/workspace.db .dump",
                        "rg -n budget extracted/",
                        'grep -r "x" "extracted/"',
                        r"Select-String -Pattern 'x' -Path extracted\notes.md",
                        "python tools/run_tests.py && cat extracted/a.md",
                        "git diff --no-index extracted/a.md b.md"):
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
                        "python tools/run_tests.py && cat ~/AppData/Local/Unminute/workspace/a"):
            for name in ("Bash", "PowerShell"):
                with self.subTest(command=command, tool=name):
                    self.assertIsNotNone(guard.decide(shell(command, name)))

    def test_commands_in_a_checkout_named_unminute_are_allowed(self):
        for command in ("ls ../unminute/workspace",
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
                        "git commit -F - <<'EOF'\nStop staging *.wav files in extracted/\nEOF"):
            for name in ("Bash", "PowerShell"):
                with self.subTest(command=command, tool=name):
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

    def test_media_extensions_cover_the_apps_import_list(self):
        source = (ROOT / "workspace" / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = next(ast.literal_eval(node.value) for node in ast.walk(tree)
                       if isinstance(node, ast.Assign)
                       and any(getattr(t, "id", None) == "AUDIO_EXTENSIONS" for t in node.targets))
        self.assertEqual(set(), set(imports) - set(guard.MEDIA_EXTENSIONS))


if __name__ == "__main__":
    unittest.main()
