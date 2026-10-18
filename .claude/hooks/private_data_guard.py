# .claude/hooks/private_data_guard.py
# Claude Code PreToolUse hook, configured in .claude/settings.json. It keeps real
# meeting content out of an agent's context and out of commits (AGENTS.md,
# invariants P1 and P2):
#   - file tools may not touch a repo's extracted/ folder (old transcripts) or the
#     live workspace (%LOCALAPPDATA%\Unminute\workspace, or that data folder other
#     than its models, and the old %LOCALAPPDATA%\whisper\workspace, which keeps
#     real content until deleted);
#   - shell commands may not name either of them as something to read, list,
#     copy or stage;
#   - git add and git commit may not name a media file.
# Message text (commit messages, PR bodies, heredocs) and the patterns given to
# text searches are ignored, so rules can be quoted and searched for.
# It is a guard rail, not a security boundary: a command can build a path these
# patterns do not see (a variable set in one command and used in the next, say),
# or search a folder above local AppData as a whole.
# Codex does not run it. Stdlib only, so any Python works.
#
# Exit 2 with a reason on stderr blocks the tool call; exit 0 allows it. The
# settings run it through PowerShell (Git Bash may be missing), skip it when the
# file is absent (a branch from before it existed), and end with
# "exit $LASTEXITCODE": PowerShell otherwise turns the 2 into a 1, which only warns.

import fnmatch
import json
import ntpath
import os
import re
import sys
from pathlib import Path

# Kept a superset of workspace/app.py AUDIO_EXTENSIONS (tests/test_agent_guard.py
# checks), plus the workspace's decoded audio.
MEDIA_EXTENSIONS = (".mp3", ".m4a", ".wav", ".mp4", ".mov", ".mkv", ".flac", ".ogg", ".aac",
                    ".wma", ".webm", ".avi", ".f32")


def _local_appdata():
    """Local AppData however a path names it: %LOCALAPPDATA%, $env:LOCALAPPDATA,
    AppData\\Local (or 'AppData', 'Local' as parts of a path in code),
    GetFolderPath('LocalApplicationData'), or this user's own LOCALAPPDATA written
    out, which a redirected profile puts elsewhere (D:\\Profiles\\name\\Local, say)."""
    forms = [r"AppData(?:[\\/]+|['\"]\s*[,/]\s*['\"])Local", "LOCALAPPDATA",
             "LocalApplicationData"]
    parts = [part for part in re.split(r"[\\/]+", os.environ.get("LOCALAPPDATA", "")) if part]
    if len(parts) > 1:
        forms.append(r"[\\/]+".join(re.escape(part) for part in parts))
    return "(?:" + "|".join(forms) + ")"


# The live workspace, however the path is written, and a renamed copy of it (the
# guide's workspace-damaged). The old one anywhere: %LOCALAPPDATA%\whisper\workspace,
# ~/AppData/Local/whisper/workspace (or its short name, WORKSP~1), or "whisper",
# "workspace" as parts of a path in code. The new one is in %LOCALAPPDATA%\Unminute,
# beside the models agents may use: that folder is blocked, except its models, but
# only next to local AppData, allowing a few characters of shell syntax between them
# (Join-Path, "$(...)", concatenation, a list of path parts). A clone of this
# repository is a folder named unminute holding the workspace package, so the name
# alone is not enough.
LOCAL_APPDATA = _local_appdata()
# models is allowed, but not a path that climbs out of it (models\..\workspace).
DATA_FOLDER = (r"Unminute(?!\w)(?![\\/'\",\s]{1,4}models(?!\w)"
               r"(?![^\s'\"]*?[\\/]\.\.(?:[\\/'\"\s]|$)))")
LIVE_WORKSPACE = re.compile(
    r"(?<![\w-])whisper(?:[\\/]+|['\"]\s*[,/]\s*['\"])(?:workspace(?!\w)|WORKSP~\d)"
    r"|" + LOCAL_APPDATA + r"[%}\)\]'\"\s+,\\/]{1,10}" + DATA_FOLDER,
    re.IGNORECASE)
# A wildcard straight after local AppData (Unmin*, *, U?minute) that could stand for
# the data folder, unless what follows it is models.
WILDCARD_CHILD = re.compile(LOCAL_APPDATA + r"[%}\)'\"]*[\\/]+([^\\/\s'\"]*[*?\[][^\\/\s'\"]*)"
                            r"(?:[\\/]+([^\\/\s'\"]*))?", re.IGNORECASE)
# Other names for folders in and beside local AppData, so that $env:TEMP\.. or
# $env:APPDATA\..\Local are seen for what they reach. TEMP is in local AppData
# unless moved; moving it only makes these patterns stricter.
TEMP_FOLDER = re.compile(r"(?:\$env:|\$\{env:|\$|%)(?:TEMP|TMP)(?!\w)[}%]?"
                         r"|\[(?:System\.)?IO\.Path\]::GetTempPath\(\)"
                         r"|(?<![\w.~:$%/-])/tmp(?![\w.-])", re.IGNORECASE)
ROAMING_FOLDER = re.compile(r"(?:\$env:|\$\{env:|\$|%)APPDATA(?!\w)[}%]?", re.IGNORECASE)
# A folder followed by .. (but not a drive, a variable or .. itself), removed until
# none is left: $env:LOCALAPPDATA\Temp\..\Unminute is $env:LOCALAPPDATA\Unminute.
UP_STEP = re.compile(r"(?<=[\\/])(?!\.\.?[\\/])[^\\/\s'\"]+[\\/]+\.\.(?=[\\/'\"\s]|$)")
# extracted/ as a path of its own (not "unextracted/"), with or without a separator.
EXTRACTED = re.compile(r"(?<![\w.-])extracted(?:[\\/]|(?![\w.-]))", re.IGNORECASE)
MEDIA_TOKEN = re.compile(r"[^\s\"']+(?:" + "|".join(re.escape(e) for e in MEDIA_EXTENSIONS)
                         + r")(?=[\s\"']|$)", re.IGNORECASE)

# Text that is written, not read: heredoc bodies, the values of message options and
# gh api's text fields. Text the shell runs first ($(...) or a backtick inside double
# quotes, an unquoted heredoc) can read a file, so it is kept and checked. A file
# option (git commit -F, gh --body-file, gh api -F key=@file) names a file to read.
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1.*?^\s*\2\s*$", re.DOTALL | re.MULTILINE)
DOUBLE = r"\"(?:[^\"\\]|\\.)*\""
FIELD_KEY = r"[\w.\[\]-]+=(?!@)"
MESSAGE_VALUE = re.compile(
    r"(?<![\w-])(?:-m|-b|-t|--message|--body|--title|--notes|--subject|--description|-[Vv]alue)"
    r"(?:\s+|=)(?:" + DOUBLE + r"|'[^']*'|@'.*?'@|@\".*?\"@)"
    r"|(?<![\w-])(?:-f|-F|--field|--raw-field)(?:\s+|=)"
    r"(?:" + FIELD_KEY + r"(?:" + DOUBLE + r"|'[^']*')"
    r"|\"" + FIELD_KEY + r"(?:[^\"\\]|\\.)*\"|'" + FIELD_KEY + r"[^']*')",
    re.DOTALL)
# $(...) runs in both shells; a backtick runs a command in bash, but only escapes in
# PowerShell.
SHELL_RUNS = {"Bash": re.compile(r"\$\(|`"), "PowerShell": re.compile(r"\$\(")}
TOKEN = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+", re.DOTALL)
# A search's pattern: the value of its pattern option, or else its first quoted argument.
PATTERN_VALUE = re.compile(r"(?<![\w-])(?:-e|--regexp|-pattern|-simplematch)(?:\s+|=)"
                           r"(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+)", re.IGNORECASE)
SEGMENTS = re.compile(r"\|\||&&|[|;&\n]")
# A change of folder anywhere in a segment (cmd /c "cd ... && ...", too).
CHANGE_FOLDER = re.compile(r"(?:^|(?<=[\s\"'(]))(cd|chdir|pushd|popd|sl|set-location|"
                           r"push-location|pop-location)(?=[\s\"');]|$)(.*)",
                           re.IGNORECASE | re.DOTALL)

# Commands whose first argument is a search pattern rather than a path.
SEARCHES = {"grep", "egrep", "fgrep", "rg", "findstr", "select-string", "sls", "ag"}
GIT_SEARCHES = {"grep", "log"}                 # git log -S/-G take a pattern

FILE_FIELDS = ("file_path", "notebook_path", "path", "pattern")
FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep"}
SHELL_TOOLS = {"Bash", "PowerShell"}

ADVICE = ("It may hold real meeting content (AGENTS.md, invariant P2). Use --status for counts, "
          "or synthetic files in a temporary folder.")


def _names_workspace(text):
    """True when text names the live workspace (or its data folder) as a path."""
    if LIVE_WORKSPACE.search(text):
        return True
    for match in WILDCARD_CHILD.finditer(text):
        child, below = match.group(1), (match.group(2) or "")
        if fnmatch.fnmatch("unminute", child.lower()) and below.lower() != "models":
            return True
    return False


def _expand(text):
    """text with TEMP and APPDATA written as local AppData's folders, and each
    folder followed by .. removed."""
    text = TEMP_FOLDER.sub(lambda m: "LOCALAPPDATA\\Temp", text)
    text = ROAMING_FOLDER.sub(lambda m: "AppData\\Roaming", text)
    previous = None
    while previous != text:
        previous, text = text, UP_STEP.sub("", text)
    return text


def _resolve(here, word):
    """The folder or file word names, run from the folder here ("" when unknown)."""
    word = word.strip("\"'()")
    if here and not re.match(r"[\\/~$%\[(]|[A-Za-z]:", word):
        word = ntpath.join(here, word)
    return ntpath.normpath(word) if word else ""


def _data_folders():
    """This user's data folders, for searches that start above them."""
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return ()
    return tuple(ntpath.normcase(ntpath.normpath(ntpath.join(local, *parts)))
                 for parts in (("Unminute",), ("whisper", "workspace")))


DATA_FOLDERS = _data_folders()


def _above_data(path):
    """True when path is a folder that holds a data folder: a search from it reads it."""
    path = ntpath.normcase(ntpath.normpath(path)).rstrip("\\") + "\\"
    return any(folder.startswith(path) for folder in DATA_FOLDERS)


def _repo_extracted(path, cwd):
    """True when path lies inside an extracted/ folder that sits beside an AGENTS.md."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(cwd or ".") / candidate
    for parent in (candidate, *candidate.parents):
        if parent.name.lower() == "extracted" and (parent.parent / "AGENTS.md").is_file():
            return True
    return False


def _written(match, shell):
    """Message text is dropped, unless the shell runs part of it first."""
    text = match.group(0)
    if text.startswith("<<"):
        expands = not match.group(1)            # an unquoted heredoc delimiter
    else:
        expands = not text.rstrip().endswith("'") and not text.rstrip().endswith("'@")
    return text if expands and SHELL_RUNS[shell].search(text) else " "


def _segments(command, shell):
    """The command's segments with message text removed, each as (words, text)."""
    command = HEREDOC.sub(lambda m: _written(m, shell), command)
    command = MESSAGE_VALUE.sub(lambda m: _written(m, shell), command)
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


def _first_argument(text):
    """The first argument that is not an option (cd /d X, Set-Location -Path X)."""
    for token in TOKEN.findall(text):
        if not token.startswith("-") and token.lower() != "/d":
            return token
    return None


class _Folder:
    """The folder a command runs in, followed through cd, pushd and popd."""

    def __init__(self, cwd):
        self.here = ntpath.normpath(cwd) if cwd else ""
        self.previous = self.here
        self.stack = []

    def change(self, verb, rest):
        verb = verb.lower()
        if verb in ("popd", "pop-location"):
            self.here = self.stack.pop() if self.stack else ""
            return
        target = "-" if rest.strip() == "-" else _first_argument(_expand(rest))
        if target is None:
            # bash's cd goes home; PowerShell's stays. Either way, unknown.
            self.previous, self.here = self.here, ""
        elif target == "-":
            self.previous, self.here = self.here, self.previous
        else:
            if verb in ("pushd", "push-location"):
                self.stack.append(self.here)
            self.previous, self.here = self.here, _resolve(self.here, target)


def _check_shell(command, cwd="", shell="Bash"):
    folder = _Folder(cwd)
    if _names_workspace(folder.here):
        return f"A command run in the live Unminute workspace is blocked. {ADVICE}"
    for words, text in _segments(command, shell):
        program = _program(words)
        sub = words[1] if len(words) > 1 else ""
        if program in SEARCHES or (program == "git" and sub in GIT_SEARCHES):
            # A search pattern may name anything; the paths searched may not.
            stripped = PATTERN_VALUE.sub(" ", text)
            text = stripped if stripped != text else _drop_first_argument(text, program == "git")
        expanded = _expand(text)
        tokens = TOKEN.findall(expanded)
        # git -C runs this one command from another folder.
        here = folder.here
        if program == "git" and "-C" in tokens[:-1]:
            here = _resolve(here, tokens[tokens.index("-C") + 1])
        # Each word as the path it names from the current folder, and the text as a
        # whole for paths built from several words.
        if _names_workspace(expanded) or any(_names_workspace(_resolve(here, token))
                                             for token in tokens if not token.startswith("-")):
            return f"A command naming the live Unminute workspace is blocked. {ADVICE}"
        change = CHANGE_FOLDER.search(text)
        if change:
            folder.change(change.group(1), change.group(2))
            if _names_workspace(folder.here):
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
            # A relative path is read from the working folder, and Glob's pattern
            # from its path.
            base = tool_input.get("path") if tool == "Glob" and field == "pattern" else None
            full = value if Path(value).is_absolute() else f"{base or cwd}/{value}"
            # With .. resolved: models\..\workspace is the workspace, and
            # workspace\..\models is the models.
            if any(_names_workspace(ntpath.normpath(v)) for v in (value, full)):
                return f"{tool} on the live Unminute workspace is blocked. {ADVICE}"
            if _repo_extracted(value, cwd):
                return f"{tool} inside the repo's extracted/ folder is blocked. {ADVICE}"
        # Grep searches its path whole, and Glob's ** does too: from local AppData,
        # or a folder above it, they reach the workspace.
        start = tool_input.get("path") or cwd
        start = ntpath.join(cwd, start) if cwd and start else start
        recursive = tool == "Grep" or (tool == "Glob" and "**" in str(tool_input.get("pattern")))
        if recursive and start and _above_data(start):
            return f"{tool} over a folder holding the live Unminute workspace is blocked. {ADVICE}"
        return None

    if tool in SHELL_TOOLS:
        command = tool_input.get("command")
        return _check_shell(command, cwd, tool) if isinstance(command, str) else None
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
