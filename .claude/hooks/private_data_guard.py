# .claude/hooks/private_data_guard.py
# Claude Code PreToolUse hook, configured in .claude/settings.json. It keeps real
# meeting content out of an agent's context and out of commits (AGENTS.md,
# invariants P1 and P2):
#   - file tools may not touch a repo's extracted/ folder (old transcripts) or the
#     live workspace (%LOCALAPPDATA%\Unminute\workspace, and the old
#     %LOCALAPPDATA%\whisper\workspace, which keeps real content until deleted);
#   - shell commands may not name either of them as something to read, list,
#     copy or stage;
#   - git add and git commit may not name a media file.
# Message text (commit messages, PR bodies, heredocs) and the patterns given to
# text searches are ignored, so rules can be quoted and searched for.
# It is a guard rail, not a security boundary: a command can build a path these
# patterns do not see. Codex does not run it. Stdlib only, so any Python works.
#
# Exit 2 with a reason on stderr blocks the tool call; exit 0 allows it. The
# settings run it through PowerShell (Git Bash may be missing), skip it when the
# file is absent (a branch from before it existed), and end with
# "exit $LASTEXITCODE": PowerShell otherwise turns the 2 into a 1, which only warns.

import json
import re
import sys
from pathlib import Path

# Kept a superset of workspace/app.py AUDIO_EXTENSIONS (tests/test_agent_guard.py
# checks), plus the workspace's decoded audio.
MEDIA_EXTENSIONS = (".mp3", ".m4a", ".wav", ".mp4", ".mov", ".mkv", ".flac", ".ogg", ".aac",
                    ".wma", ".webm", ".avi", ".f32")

# The live workspace, however the path is written. The old one anywhere
# (%LOCALAPPDATA%\whisper\workspace, ~/AppData/Local/whisper/workspace). The new one
# only under local AppData (%LOCALAPPDATA%, $env:LOCALAPPDATA, AppData\Local): a clone of
# this repository is a folder named unminute holding the workspace package.
LIVE_WORKSPACE = re.compile(
    r"(?<![\w-])whisper[\\/]+workspace(?![\w-])"
    r"|(?:AppData[\\/]+Local|LOCALAPPDATA[%}]?)[\\/]+Unminute[\\/]+workspace(?![\w-])",
    re.IGNORECASE)
# extracted/ as a path of its own (not "unextracted/"), with or without a separator.
EXTRACTED = re.compile(r"(?<![\w.-])extracted(?:[\\/]|(?![\w.-]))", re.IGNORECASE)
MEDIA_TOKEN = re.compile(r"[^\s\"']+(?:" + "|".join(re.escape(e) for e in MEDIA_EXTENSIONS)
                         + r")(?=[\s\"']|$)", re.IGNORECASE)

# Text that is written, not read: heredoc bodies and the values of message options.
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1.*?^\s*\2\s*$", re.DOTALL | re.MULTILINE)
MESSAGE_VALUE = re.compile(
    r"(?<![\w-])(?:-m|-F|-b|-t|--message|--body|--title|--notes|--subject|--description)"
    r"(?:\s+|=)(\"(?:[^\"\\]|\\.)*\"|'[^']*'|@'.*?'@|@\".*?\"@)",
    re.DOTALL)
TOKEN = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+", re.DOTALL)
# A search's pattern: the value of its pattern option, or else its first quoted argument.
PATTERN_VALUE = re.compile(r"(?<![\w-])(?:-e|--regexp|-pattern|-simplematch)(?:\s+|=)"
                           r"(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+)", re.IGNORECASE)
SEGMENTS = re.compile(r"\|\||&&|[|;&\n]")

# Commands whose first argument is a search pattern rather than a path.
SEARCHES = {"grep", "egrep", "fgrep", "rg", "findstr", "select-string", "sls", "ag"}
GIT_SEARCHES = {"grep", "log"}                 # git log -S/-G take a pattern

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


def _segments(command):
    """The command's segments with message text removed, each as (words, text)."""
    command = HEREDOC.sub(" ", command)
    command = MESSAGE_VALUE.sub(" ", command)
    for segment in SEGMENTS.split(command):
        words = segment.split()
        # Skip assignments and wrappers to reach the program name.
        while words and (words[0] in ("sudo", "env", "time", "&", ".") or "=" in words[0]):
            words = words[1:]
        if words:
            yield [w.strip("\"'").lower() for w in words], segment


def _program(words):
    name = re.split(r"[\\/]", words[0])[-1]
    return name[:-4] if name.endswith(".exe") else name


def _drop_first_argument(text, git):
    """The segment without its first non-option argument (a search's pattern)."""
    tokens = TOKEN.findall(text)
    start = 2 if git else 1                    # after "rg", or after "git grep"
    for i in range(start, len(tokens)):
        if not tokens[i].startswith("-"):
            return " ".join(tokens[:i] + tokens[i + 1:])
    return text


def _check_shell(command):
    for words, text in _segments(command):
        program = _program(words)
        sub = words[1] if len(words) > 1 else ""
        if program in SEARCHES or (program == "git" and sub in GIT_SEARCHES):
            # A search pattern may name anything; the paths searched may not.
            stripped = PATTERN_VALUE.sub(" ", text)
            text = stripped if stripped != text else _drop_first_argument(text, program == "git")
        if LIVE_WORKSPACE.search(text):
            return f"A command naming the live Unminute workspace is blocked. {ADVICE}"
        # Most git subcommands cannot read an ignored folder's files (check-ignore,
        # status); staging and --no-index diffs can.
        git_reads = sub in ("add", "stage", "mv") or "--no-index" in words
        if EXTRACTED.search(text) and (program != "git" or git_reads):
            return f"A command naming the repo's extracted/ folder is blocked. {ADVICE}"
        if program == "git" and sub in ("add", "commit", "stage"):
            media = MEDIA_TOKEN.search(text)
            if media:
                return (f"Staging or committing {media.group(0)} is blocked: recordings stay out "
                        "of the repo (AGENTS.md, invariant P1).")
    return None


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
        return _check_shell(command) if isinstance(command, str) else None
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
