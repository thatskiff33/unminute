# .claude/hooks/private_data_guard.py
# Claude Code PreToolUse hook, configured in .claude/settings.json. It keeps real
# meeting content out of an agent's context and out of commits (AGENTS.md,
# invariants P1 and P2):
#   - file tools may not touch a repo's extracted/ folder (the earlier app's
#     transcripts) or the live workspace (%LOCALAPPDATA%\whisper\workspace);
#   - shell commands may not name either of them;
#   - git add and git commit may not name a media file.
# It is a guard rail, not a security boundary: a command can build a path these
# patterns do not see. Codex does not run it. Stdlib only, so any Python works.
#
# Exit 2 with a reason on stderr blocks the tool call; exit 0 allows it. The
# settings run it through PowerShell (Git Bash may be missing) and end with
# "exit $LASTEXITCODE": PowerShell otherwise turns the 2 into a 1, which only warns.

import json
import re
import sys
from pathlib import Path

# Kept a superset of workspace/app.py AUDIO_EXTENSIONS (tests/test_agent_guard.py
# checks), plus the workspace's decoded audio.
MEDIA_EXTENSIONS = (".mp3", ".m4a", ".wav", ".mp4", ".mov", ".mkv", ".flac", ".ogg", ".aac",
                    ".wma", ".webm", ".avi", ".f32")

# The live workspace, however the path is written: %LOCALAPPDATA%\whisper\workspace,
# $env:LOCALAPPDATA/whisper/workspace, ~/AppData/Local/whisper/workspace.
LIVE_WORKSPACE = re.compile(r"(?<![\w-])whisper[\\/]+workspace(?![\w-])", re.IGNORECASE)

# extracted/ followed by a path separator, as a path of its own (not "unextracted/").
EXTRACTED_PATH = re.compile(r"(?<![\w.-])extracted[\\/]", re.IGNORECASE)
# A bare "extracted" given to a command that lists, reads, copies or stages it.
# Text searches (grep, rg) are left out so the word itself can still be searched for.
EXTRACTED_ARG = re.compile(
    r"\b(?:ls|dir|gci|get-childitem|tree|find|cat|type|gc|get-content|head|tail|less|more|"
    r"cp|copy|copy-item|xcopy|robocopy|add)\b[^|;&\n]*?"
    r"(?<![\w.-])extracted(?![\w.-])",
    re.IGNORECASE)

GIT_STAGING = re.compile(r"\bgit\b[^|;&\n]*?\b(?:add|commit)\b", re.IGNORECASE)
MEDIA_TOKEN = re.compile(r"[^\s\"']+(?:" + "|".join(re.escape(e) for e in MEDIA_EXTENSIONS)
                         + r")(?=[\s\"']|$)", re.IGNORECASE)

FILE_FIELDS = ("file_path", "notebook_path", "path", "pattern")
FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep"}
SHELL_TOOLS = {"Bash", "PowerShell"}

ADVICE = ("It may hold real meeting content (AGENTS.md, invariant P2). Use --status for counts, "
          "or synthetic files in a temporary folder.")


def _repo_extracted(path, cwd):
    """True when path lies inside an extracted/ folder that sits beside an AGENTS.md."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(cwd or ".") / candidate
    for parent in (candidate, *candidate.parents):
        if parent.name.lower() == "extracted" and (parent.parent / "AGENTS.md").is_file():
            return True
    return False


def decide(payload):
    """The reason to block this tool call, or None to allow it."""
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or ""

    if tool in FILE_TOOLS:
        for field in FILE_FIELDS:
            if tool == "Grep" and field == "pattern":
                continue                        # Grep's pattern is text to find, not a path
            value = tool_input.get(field)
            if not isinstance(value, str) or not value:
                continue
            if LIVE_WORKSPACE.search(value):
                return f"{tool} on the live Unminute workspace is blocked. {ADVICE}"
            if _repo_extracted(value, cwd):
                return f"{tool} inside the repo's extracted/ folder is blocked. {ADVICE}"
        return None

    if tool in SHELL_TOOLS:
        command = tool_input.get("command")
        if not isinstance(command, str):
            return None
        if LIVE_WORKSPACE.search(command):
            return f"A command naming the live Unminute workspace is blocked. {ADVICE}"
        if EXTRACTED_PATH.search(command) or EXTRACTED_ARG.search(command):
            return f"A command naming the repo's extracted/ folder is blocked. {ADVICE}"
        if GIT_STAGING.search(command):
            media = MEDIA_TOKEN.search(command)
            if media:
                return (f"Staging or committing {media.group(0)} is blocked: recordings stay out "
                        "of the repo (AGENTS.md, invariant P1).")
    return None


def main():
    # Unreadable input means the hook is wired wrongly: say so with exit 1, which
    # Claude Code shows without blocking, rather than allowing silently or
    # blocking every tool call in every session.
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        payload = None
    if not isinstance(payload, dict):
        print("private_data_guard: no readable hook input, so the guard is not active; "
              "check the hook command in .claude/settings.json", file=sys.stderr)
        return 1
    reason = decide(payload)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
