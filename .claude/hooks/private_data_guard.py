# .claude/hooks/private_data_guard.py
# Claude Code PreToolUse hook, configured in .claude/settings.json. It keeps real
# meeting content out of an agent's context and out of commits (AGENTS.md,
# invariants P1 and P2):
#   - file tools may not touch a repo's extracted/ folder (old transcripts) or the
#     live workspace (%LOCALAPPDATA%\Unminute\workspace, or that data folder other
#     than its models, and the old %LOCALAPPDATA%\whisper folder, whose workspace
#     keeps real content until deleted), however the path is spelt: short (8.3)
#     names, junctions such as "Local Settings", \\?\ and \\localhost\C$ forms and
#     trailing dots are resolved first;
#   - shell commands (Bash, PowerShell, Monitor) may not name either of them as
#     something to read, list, copy or stage, nor search local AppData whole
#     (also with a wildcard or file name as the last part, or no folder named when
#     run from local AppData or above; inside brackets, a loop, a string run by
#     another shell or after a wrapper such as timeout; or fed local AppData by a
#     pipeline);
#   - git add and git commit may not name a media file.
# A leading ~ is the home folder, as the Grep and Glob tools read it, and /c/x is
# C:\x, as Git Bash reads it.
# Message text (commit messages, PR bodies, quoted heredocs and here-strings not
# fed to an interpreter, PowerShell text held in a variable) and the patterns given
# to text searches are ignored, so rules can be quoted and searched for.
# It is a guard rail, not a security boundary. Known gaps: a path held in a
# variable from an earlier tool call, or set with Set-Variable or in a scriptblock;
# a script file written in one step (Write, cat > f <<EOF, Set-Content) and run in
# another; a path the program builds itself (the app's own default_root(),
# GetFolderPath(28), obfuscation such as backtick escapes or a format string
# ("{0}\Unminute" -f ...), a wildcard in place of AppData or Local); a search
# program named by a variable or a full path (& $rg, /usr/bin/grep) inside another
# command; Set-Location -Path:<folder> followed by a relative path (it is taken as
# the home folder); a copy or archive of local AppData whole (Copy-Item -Recurse,
# robocopy, tar, Compress-Archive); and a link made and used in the same command (a
# later command through it is resolved).
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
    AppData\\Local (or 'AppData', 'Local' as parts of a path in code), the profile's
    "Local Settings" junction to it, GetFolderPath('LocalApplicationData'), or this
    user's own LOCALAPPDATA written out, which a redirected profile puts elsewhere
    (D:\\Profiles\\name\\Local, say)."""
    forms = [r"AppData(?:[\\/]+|['\"]\s*[,/]\s*['\"])Local", "LOCALAPPDATA",
             "LocalApplicationData", r"Local Settings", r"LOCALS~\d"]
    parts = [part for part in re.split(r"[\\/]+", os.environ.get("LOCALAPPDATA", "")) if part]
    if len(parts) > 1:
        forms.append(r"[\\/]+".join(re.escape(part) for part in parts))
    return "(?:" + "|".join(forms) + ")"


# The data folders, however the path is written. The new one is %LOCALAPPDATA%\Unminute,
# beside the models agents may use: that folder is blocked, except its models, but
# only next to local AppData, allowing a few characters of shell syntax between them
# (Join-Path, "$(...)", concatenation, a list of path parts in code). A clone of this
# repository is a folder named unminute holding the workspace package, so the name
# alone is not enough. The old one is %LOCALAPPDATA%\whisper, blocked whole (its
# whisper-models sibling is not), and its workspace anywhere: whisper\workspace (or
# its short name, WORKSP~1), or "whisper", "workspace" as parts of a path in code.
LOCAL_APPDATA = _local_appdata()
# models is allowed, but not a path that climbs out of it (models\..\workspace),
# whether the .. is in the same word or the next path part.
UP_NEXT = r"(?:[\\/]|['\"]\s*[,/+]?\s*r?['\"]|\s+)\.\.(?:[\\/'\"\s]|$)"
DATA_FOLDER = (r"Unminute(?!\w)(?![\\/'\",\s]{1,4}models(?!\w)"
               r"(?![^\s'\"]*?" + UP_NEXT + r"))")
OLD_FOLDER = r"whisper(?![\w-])"
SEPARATOR = r"[%}\)\]'\"\s+,\\/.]{1,10}"
LIVE_WORKSPACE = re.compile(
    r"(?<![\w-])whisper(?:[\\/]+|['\"]\s*[,/]\s*['\"])(?:workspace(?!\w)|WORKSP~\d)"
    r"|" + LOCAL_APPDATA + SEPARATOR + r"(?:" + DATA_FOLDER + "|" + OLD_FOLDER + ")",
    re.IGNORECASE)
DATA_NAMES = ("unminute", "whisper")
# A wildcard (or a {a,b} brace list) straight after local AppData (Unmin*, *, w*)
# that could stand for a data folder, unless what follows it is models.
WILDCARD_CHILD = re.compile(LOCAL_APPDATA + r"[%}\)'\"]*[\\/]+([^\\/\s'\"]*[*?\[{][^\\/\s'\"]*)"
                            r"(?:[\\/]+([^\\/\s'\"]*))?", re.IGNORECASE)
# Local AppData named as a folder of its own (not one inside it), and a data folder's
# name as a path of its own: together in one command (a pipeline, Join-Path
# -ChildPath, split arguments) they build the data folder.
BARE_LOCAL = re.compile(LOCAL_APPDATA + r"[%}\)\]'\"]*(?=[\s|;),]|$)", re.IGNORECASE)
DATA_WORD = re.compile(r"(?<![\w.\\/:$%~-])(?:" + DATA_FOLDER + "|" + OLD_FOLDER + ")",
                       re.IGNORECASE)
# Other names for folders in and beside local AppData, so that $env:TEMP\.. or
# $env:APPDATA\..\Local are seen for what they reach. TEMP is in local AppData
# unless moved; moving it only makes these patterns stricter.
VARIABLE = r"(?:\$env:|\$\{env:|\$\{|\$|%)"
LOCAL_VARIABLE = re.compile(VARIABLE + r"LOCALAPPDATA(?!\w)[}%]?", re.IGNORECASE)
TEMP_FOLDER = re.compile(VARIABLE + r"(?:TEMP|TMP)(?!\w)[}%]?"
                         r"|\[(?:System\.)?IO\.Path\]::GetTempPath\(\)"
                         r"|(?<![\w.~:$%/-])/tmp(?![\w.-])", re.IGNORECASE)
ROAMING_FOLDER = re.compile(VARIABLE + r"APPDATA(?!\w)[}%]?", re.IGNORECASE)
HOME_FOLDER = re.compile(VARIABLE + r"(?:USERPROFILE|HOME)(?!\w)[}%]?", re.IGNORECASE)
# A folder followed by .. (but not a drive, a variable or .. itself), removed until
# none is left: $env:LOCALAPPDATA\Temp\..\Unminute is $env:LOCALAPPDATA\Unminute.
UP_STEP = re.compile(r"(?<=[\\/])(?!\.\.?[\\/])[^\\/\s'\"]+[\\/]+\.\.(?=[\\/'\"\s]|$)")
# Windows drops trailing dots from a folder name (Local.\Unminute is Local\Unminute).
TRAILING_DOTS = re.compile(r"(?<=[^\\/.\s'\"])\.+(?=[\\/])")
# A Python string prefix (r'...', f"..."), which would otherwise separate the parts.
STRING_PREFIX = re.compile(r"(?<=[+,(\[\s])[rRfFbBuU]{1,2}(?=['\"])")
# extracted/ as a path of its own (not "unextracted/"), with or without a separator.
EXTRACTED = re.compile(r"(?<![\w.-])extracted(?:[\\/]|(?![\w.-]))", re.IGNORECASE)
MEDIA_TOKEN = re.compile(r"[^\s\"']+(?:" + "|".join(re.escape(e) for e in MEDIA_EXTENSIONS)
                         + r")(?=[\s\"']|$)", re.IGNORECASE)

# Text that is written, not read: heredoc bodies and here-strings, the values of
# message options and gh api's text fields. Text the shell runs first ($(...) or a
# backtick inside double quotes, an unquoted heredoc) can read a file, so those
# parts are kept and checked; so is a heredoc or here-string fed to an interpreter,
# which runs it as code. A file option (git commit -F, gh --body-file, gh api -F
# key=@file) names a file to read.
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1.*?^\s*\2\s*$", re.DOTALL | re.MULTILINE)
HERE_STRING = re.compile(r"@(['\"])[ \t]*\r?\n.*?\r?\n\1@", re.DOTALL)
INTERPRETER = re.compile(r"(?<![\w.-])(?:python[\d.]*|pythonw|py|node|deno|bash|sh|zsh|dash|"
                         r"pwsh|powershell|cmd|iex|invoke-expression|invoke-command|perl|ruby|"
                         r"sqlite3|wsl|uv|start-process)(?:\.exe)?(?![\w.-])", re.IGNORECASE)
DOUBLE = r"\"(?:[^\"\\]|\\.)*\""
FIELD_KEY = r"[\w.\[\]-]+=(?!@)"
MESSAGE_VALUE = re.compile(
    r"(?<![\w-])(?:-m|-b|-t|--message|--body|--title|--notes|--subject|--description|-[Vv]alue)"
    r"(?:\s+|=)(?:" + DOUBLE + r"|'[^']*'|@'.*?'@|@\".*?\"@)"
    r"|(?<![\w-])(?:-f|-F|--field|--raw-field)(?:\s+|=)"
    r"(?:" + FIELD_KEY + r"(?:" + DOUBLE + r"|'[^']*')"
    r"|\"" + FIELD_KEY + r"(?:[^\"\\]|\\.)*\"|'" + FIELD_KEY + r"[^']*')",
    re.DOTALL)
# Variables set in the command itself, substituted into what follows. PowerShell
# ($name = value) and bash (NAME=value, export NAME=value).
ASSIGNMENT = {
    "PowerShell": re.compile(r"(?:^|(?<=[;&|{(\n]))\s*\$(\w+)\s*=(?!=)\s*"
                             r"(@'.*?'@|@\".*?\"@|'[^']*'|\"(?:[^\"`]|`.)*\"|[^;\n|&]+)",
                             re.DOTALL),
    "Bash": re.compile(r"(?:^|(?<=[;&|\n]))\s*(?:export\s+|local\s+|declare\s+)?"
                       r"([A-Za-z_]\w*)=('[^']*'|\"(?:[^\"\\]|\\.)*\"|[^\s;&|]*)"),
}
TOKEN = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+", re.DOTALL)
# PowerShell's ${C:\path}, which reads the file, even inside double quotes.
PROVIDER_VARIABLE = re.compile(r"\$\{([A-Za-z]:[^}]*)\}")
# A search's pattern: the value of its pattern option, or else its first quoted argument.
# PowerShell also joins a value with a colon (-Pattern:x), and grep and rg take -e's
# value attached (-ex), case-sensitively (R13b round 3 review).
PATTERN_VALUE = re.compile(r"(?<![\w-])(?:-e|--regexp|-pattern|-simplematch)(?:\s+|=|:)"
                           r"(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+)", re.IGNORECASE)
ATTACHED_PATTERN = re.compile(r"(?<![\w-])-e(?![\s=:-])(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+)")
GREP_LIKE = {"grep", "egrep", "fgrep", "rg", "ag"}
SEGMENTS = re.compile(r"\|\||&&|[|;&\n]")
# Words that run the program after them (R13b round 3 review: timeout, xargs).
WRAPPERS = {"sudo", "env", "time", "&", ".", "command", "builtin", "exec", "nohup", "nice",
            "timeout", "stdbuf", "xargs"}
# A search or listing inside a segment: in (...), {...}, $(...), backticks, a string
# run by pwsh -c, bash -c or Invoke-Expression, or after foreach (... in.
NESTED = re.compile(r"(?<![\w.$:\\/-])(?:rg|ag|tree|find|grep|egrep|fgrep|get-childitem|gci|ls|"
                    r"dir|findstr|git)(?:\.exe)?(?![\w.:\\/-])", re.IGNORECASE)
NESTED_CMD = re.compile(r"cmd(?:\.exe)?\s+(?:/\S+\s+)*?/[ckr]\s*[\"']?\s*$", re.IGNORECASE)
NESTED_POWERSHELL = re.compile(r"(?:pwsh|powershell)(?:\.exe)?\s.*?-c\w*\s+[\"']?\s*$"
                               r"|(?:iex|invoke-expression)\s+[\"']?\s*$", re.IGNORECASE)
NESTED_BASH = re.compile(r"(?:bash|sh|zsh)(?:\.exe)?\s.*?-c\s+[\"']?\s*$", re.IGNORECASE)
# cmd's own part of cmd /c "program ...", with the opening quote.
CMD_WRAPPER = re.compile(r"\s*(?:\S*[\\/])?cmd(?:\.exe)?\s+(?:/[^\s/]+\s+)*?/[ckr]\s+[\"']?",
                         re.IGNORECASE)
STATEMENTS = re.compile(r"\|\||&&|[;&\n]")         # a pipeline stays together
# A change of folder at the start of a segment (or inside cmd /c "cd ... && ...").
CHANGE_FOLDER = re.compile(r"^[\s(\{\"']*(?:cmd(?:\.exe)?\s+/[ck]\s+[\"']?\s*)?"
                           r"(cd|chdir|pushd|popd|sl|set-location|push-location|pop-location)"
                           r"(?=[\s\"');]|$)(.*)", re.IGNORECASE | re.DOTALL)

# Commands whose first argument is a search pattern rather than a path.
SEARCHES = {"grep", "egrep", "fgrep", "rg", "findstr", "select-string", "sls", "ag"}
GIT_SEARCHES = {"grep", "log"}                 # git log -S/-G take a pattern
# Options that take a value, which is not the search pattern.
VALUE_OPTIONS = {
    "rg": {"-g", "--glob", "--iglob", "-t", "--type", "-T", "--type-not", "-m", "--max-count",
           "-A", "-B", "-C", "--after-context", "--before-context", "--context", "--max-depth",
           "-d", "-j", "--threads", "-M", "--max-columns", "--max-filesize", "-E", "--encoding",
           "-r", "--replace", "--sort", "--sortr", "--color", "--colors", "--type-add",
           "--pre", "--engine", "--context-separator", "--path-separator"},
    "grep": {"-A", "-B", "-C", "-m", "--include", "--exclude", "--exclude-dir", "-d", "-D",
             "--color", "--colour"},
    "select-string": {"-path", "-literalpath", "-encoding", "-context", "-include",
                      "-exclude", "-culture"},
}
VALUE_OPTIONS.update(egrep=VALUE_OPTIONS["grep"], fgrep=VALUE_OPTIONS["grep"],
                     sls=VALUE_OPTIONS["select-string"], ag=VALUE_OPTIONS["rg"])
# Listings, which with recursion take a last part that is a wildcard or a file name
# as a filter, and search every folder below the rest (so does findstr /s).
LISTINGS = ("get-childitem", "gci", "ls", "dir")
FILTER_LEAF = LISTINGS + ("findstr",)
VALUE_OPTIONS.update((name, {"-filter", "-include", "-exclude", "-depth", "-attributes"})
                     for name in LISTINGS)
# Options after which a search has no pattern argument at all.
NO_PATTERN = {"--files", "--type-list", "-f", "--file"}

FILE_FIELDS = ("file_path", "notebook_path", "path", "pattern")
FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep"}
# Monitor runs a command too; which shell it uses is not fixed, so both rules apply.
SHELL_TOOLS = {"Bash": ("Bash",), "PowerShell": ("PowerShell",),
               "Monitor": ("Bash", "PowerShell")}

ADVICE = ("It may hold real meeting content (AGENTS.md, invariant P2). Use --status for counts, "
          "or synthetic files in a temporary folder.")


# -- paths as Windows opens them ------------------------------------------------------

def _local_hosts():
    names = ["localhost", r"127\.0\.0\.1", r"\[?::1\]?"]
    if os.environ.get("COMPUTERNAME"):
        names.append(re.escape(os.environ["COMPUTERNAME"]))
    return "(?:" + "|".join(names) + ")"


DEVICE_UNC = re.compile(r"^\\\\[?.]\\UNC\\", re.IGNORECASE)
DEVICE_DRIVE = re.compile(r"^\\\\[?.]\\(?=[A-Za-z]:)")
ADMIN_SHARE = re.compile(r"^\\\\" + _local_hosts() + r"\\([A-Za-z])\$(?=\\|$)", re.IGNORECASE)


def _remote_drive(path):
    """True for a mapped network drive, where resolving links could wait on a server."""
    try:
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(path[:2] + "\\") == 4    # DRIVE_REMOTE
    except (AttributeError, OSError, ImportError):
        return False


# Git Bash's spelling of a drive (/c/Users, /mnt/c), which the Grep and Glob tools
# accept too (R13b round 3 review).
MSYS_DRIVE = re.compile(r"^[\\/](?:mnt[\\/])?([A-Za-z])(?=[\\/]|$)")


def _home(path):
    """path with a leading ~ (~, ~\\x, ~/x, ~name) as a home folder, as shells and
    Claude Code's Grep and Glob read it, and a Git Bash drive (/c/x) as C:\\x;
    anything else unchanged."""
    if isinstance(path, str) and path.startswith("~"):
        return ntpath.expanduser(path)
    if isinstance(path, str):
        return MSYS_DRIVE.sub(lambda m: m.group(1).upper() + ":\\", path, count=1)
    return path


def _canonical(path):
    """path as Windows opens it, lower-cased: \\\\?\\ and \\\\localhost\\C$ forms as the
    drive path, trailing dots and spaces dropped from each name, . and .. resolved, and
    on a local drive, links, junctions and short (8.3) names resolved (metadata only;
    nothing is read). "" when it is not a full path."""
    path = _home(path.strip().strip("\"'")).replace("/", "\\")
    path = DEVICE_UNC.sub(r"\\\\", path)
    path = DEVICE_DRIVE.sub("", path)
    path = ADMIN_SHARE.sub(lambda m: m.group(1) + ":", path)
    if not re.match(r"[A-Za-z]:\\|\\\\", path):
        return ""
    parts = path.split("\\")
    path = "\\".join(p if p in (".", "..") else (p.rstrip(". ") or p) for p in parts)
    path = ntpath.normpath(path)
    if re.match(r"[A-Za-z]:\\", path) and os.name == "nt" and not _remote_drive(path):
        try:
            path = ntpath.realpath(path)
        except (OSError, ValueError):
            pass
        path = DEVICE_DRIVE.sub("", path)
    return ntpath.normcase(path).rstrip("\\")


def _data_folders():
    """This user's data folders, canonical: the new one (with the name of the part
    agents may use) and the old one."""
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return ()
    return ((_canonical(ntpath.join(local, "Unminute")), "models"),
            (_canonical(ntpath.join(local, "whisper")), None))


DATA_FOLDERS = _data_folders()
# A folder that is local AppData or AppData by its name, whoever's profile it is in.
APPDATA_END = re.compile(r"\\appdata(?:\\local)?$|\\local settings$|\\locals~\d$")


def _in_data(path):
    """True when the canonical path is a data folder or inside one (not its models)."""
    for folder, allowed in DATA_FOLDERS:
        if folder and (path == folder or path.startswith(folder + "\\")):
            rest = path[len(folder) + 1:].split("\\")[0] if path != folder else ""
            if not (allowed and rest == allowed):
                return True
    return False


def _above_data(path):
    """True when the canonical path holds a data folder: a search from it reads it."""
    if not path:
        return False
    if APPDATA_END.search(path):
        return True
    return any(folder.startswith(path + "\\") for folder, _ in DATA_FOLDERS if folder)


def _brace_alternatives(pattern, limit=64):
    """A glob pattern's {a,b} lists expanded; an unclosed brace stands for anything.
    Past the limit, each list left stands for anything too, so a long list cannot
    hide a data folder's name (R13b round 2 review)."""
    found, out = [pattern], []
    while found and len(out) < limit:
        current = found.pop()
        m = re.search(r"\{([^{}]*)\}", current)
        if not m:
            out.append(current.replace("{", "*").replace("}", "*"))
            continue
        found.extend(current[:m.start()] + alt + current[m.end():]
                     for alt in m.group(1).split(","))
    for current in found:
        previous = None
        while previous != current:
            previous, current = current, re.sub(r"\{[^{}]*\}", "*", current)
        out.append(current.replace("{", "*").replace("}", "*"))
    return out


def _glob_reaches_data(start, pattern):
    """True when Glob's pattern, from start, could list inside a data folder."""
    for alternative in _brace_alternatives(pattern.replace("/", "\\")):
        absolute = re.match(r"[A-Za-z]:|\\\\", alternative)
        full = alternative if absolute else f"{start}\\{alternative}"
        parts = [p for p in full.split("\\") if p not in ("", ".")]
        cut = next((i for i, p in enumerate(parts) if re.search(r"[*?\[]", p)), len(parts))
        literal = "\\".join(parts[:cut])
        if full.startswith("\\\\"):
            literal = "\\\\" + literal
        base = _canonical(literal) or literal.lower()
        rest = [p.lower() for p in parts[cut:]]
        if _in_data(base) and rest:
            return True
        folders = [folder for folder, _ in DATA_FOLDERS]
        # A folder named AppData or Local, in any profile: its data folders by name.
        if APPDATA_END.search(base):
            local = base if not base.endswith("\\appdata") else base + "\\local"
            folders += [local + "\\" + name for name in DATA_NAMES]
        for folder in folders:
            if not folder or not folder.startswith(base + "\\"):
                continue
            below = folder[len(base) + 1:].split("\\")
            for i, part in enumerate(rest):
                if part == "**":
                    return True
                if i < len(below):
                    if not fnmatch.fnmatchcase(below[i], part):
                        break
                    continue
                # Past the data folder: only models may follow (the old folder has
                # none, so allowing it there lists nothing).
                if part != "models":
                    return True
                break
    return False


# -- shell text ---------------------------------------------------------------------

def _names_workspace(text):
    """True when text names the live workspace (or a data folder) as a path."""
    text = TRAILING_DOTS.sub("", text)
    if LIVE_WORKSPACE.search(text):
        return True
    for match in WILDCARD_CHILD.finditer(text):
        children = _brace_alternatives(match.group(1).lower())
        below = (match.group(2) or "").lower()
        if below != "models" and any(fnmatch.fnmatchcase(name, child)
                                     for name in DATA_NAMES for child in children):
            return True
    return False


def _expand(text):
    """text with local AppData's variables (and TEMP, APPDATA, the home folder) written
    as the folders they are, trailing dots and string prefixes dropped, and each folder
    followed by .. removed."""
    text = LOCAL_VARIABLE.sub(lambda m: "~\\AppData\\Local", text)
    text = TEMP_FOLDER.sub(lambda m: "~\\AppData\\Local\\Temp", text)
    text = ROAMING_FOLDER.sub(lambda m: "~\\AppData\\Roaming", text)
    text = HOME_FOLDER.sub(lambda m: "~", text)
    text = TRAILING_DOTS.sub("", text)
    text = STRING_PREFIX.sub("", text)
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


def _reaches_data(path):
    """True when a path names the workspace, or (resolved as Windows would) lies in
    a data folder."""
    return _names_workspace(path) or _in_data(_canonical(path))


def _repo_extracted(path, cwd):
    """True when path lies inside an extracted/ folder that sits beside an AGENTS.md."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(cwd or ".") / candidate
    for parent in (candidate, *candidate.parents):
        if parent.name.lower() == "extracted" and (parent.parent / "AGENTS.md").is_file():
            return True
    return False


def _run_parts(text, shell):
    """The parts of double-quoted or unquoted text that the shell runs first: each
    $(...), and in bash each `...`."""
    parts, i = [], 0
    while True:
        start = text.find("$(", i)
        if start < 0:
            break
        depth, j = 0, start + 1
        while j < len(text):
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            if depth == 0:
                break
            j += 1
        parts.append(text[start + 2:j])
        i = j + 1
    if shell == "Bash":
        parts += re.findall(r"`([^`]*)`", text)
    else:
        # ${C:\path} is the file's contents (PowerShell's file system provider).
        parts += [f"Get-Content '{path}'" for path in PROVIDER_VARIABLE.findall(text)]
    return " ; ".join(parts) if parts else " "


def _consumer(command, start, end):
    """The text around a heredoc or here-string on its own lines: the command that
    takes it, before it, and anything it is piped to, after it."""
    line_start = command.rfind("\n", 0, start) + 1
    before = re.split(r"\|\||&&|;", command[line_start:start])[-1]
    line_end = command.find("\n", end)
    after = re.split(r"\|\||&&|;", command[end:line_end if line_end >= 0 else len(command)])[0]
    return before + " " + after


def _drop_written(command, shell):
    """command with heredocs, here-strings and message text dropped, unless an
    interpreter runs them, keeping the parts the shell runs first."""
    def heredoc(match):
        # The rest of the heredoc's first line too: cat <<'EOF' | python -
        first_line = match.group(0).split("\n", 1)[0]
        piped = re.split(r"\|\||&&|;", first_line)[0]
        if INTERPRETER.search(_consumer(command, match.start(), match.start() + len(first_line))
                              + " " + piped):
            return match.group(0)
        if match.group(1):                       # a quoted delimiter: plain text
            return " "
        return _run_parts(match.group(0), shell)

    def here_string(match):
        if INTERPRETER.search(_consumer(command, match.start(), match.end())):
            return match.group(0)
        return " " if match.group(1) == "'" else _run_parts(match.group(0), shell)

    def message(match):
        text = match.group(0)
        literal = text.rstrip().endswith("'") or text.rstrip().endswith("'@")
        return " " if literal else _run_parts(text, shell)

    command = HEREDOC.sub(heredoc, command)
    if shell == "PowerShell":
        command = HERE_STRING.sub(here_string, command)
    return MESSAGE_VALUE.sub(message, command)


def _substitute(command, shell):
    """command with variables set in it written out where they are used later. A
    literal value (single quotes, a literal here-string), or PowerShell double-quoted
    text other than what it runs, is left only where used, when it is the whole of
    what is assigned and every later use is one written out here; otherwise it is
    also kept where it is set (R13b round 3 review: concatenation, $script:p and
    Get-Variable p hid a path)."""
    pattern = ASSIGNMENT.get(shell)
    if not pattern:
        return command
    flags = re.IGNORECASE if shell == "PowerShell" else 0
    scope = r"(?:(?:script|global|local|private):)?" if shell == "PowerShell" else ""
    offset = 0
    for match in list(pattern.finditer(command)):
        name, value = match.group(1), match.group(2).strip()
        if not value:
            continue
        start, end = match.start(2) + offset, match.end(2) + offset
        use = re.compile(r"\$\{?" + scope + re.escape(name) + r"(?!\w)\}?", flags)
        # Any mention of the name: $name, $any:name, or the bare word (Get-Variable).
        mention = re.compile(r"\$\{?(?:\w+:)?" + re.escape(name) + r"(?!\w)"
                             r"|(?<![\w$:-])" + re.escape(name) + r"(?![\w-])", flags)
        rest = command[end:]
        after = use.sub(lambda m, text=value: text, rest)
        whole = re.match(r"[ \t]*(?:[;\n|&]|$)", rest) is not None
        hidden = whole and len(mention.findall(rest)) == len(use.findall(rest))
        literal = value.startswith("'") or value.startswith("@'")
        if literal and hidden:
            middle = "''"
        elif shell == "PowerShell" and value.startswith('"') and hidden:
            # A PowerShell variable holds text: only what the shell runs in it is
            # checked here; the whole value is checked where it is used.
            middle = _run_parts(value, shell)
        else:
            middle = command[start:end]
        new = command[:start] + middle + after
        offset += len(new) - len(command)
        command = new
    return command


def _segments(command, shell):
    """The command's segments with message text removed, each as (words, text,
    dialect): the shell that reads the program's options, "cmd" for cmd /c."""
    for segment in SEGMENTS.split(command):
        words = segment.split()
        # Skip assignments and wrappers (with their options, counts and durations)
        # to reach the program name.
        while words and (words[0].lower() in WRAPPERS or "=" in words[0]):
            wrapper = words[0].lower() in WRAPPERS
            words = words[1:]
            while wrapper and words and re.fullmatch(r"-\S*|\d+(?:\.\d+)?[smhd]?|\{\}", words[0]):
                words = words[1:]
        words = [w.strip("\"'").lower() for w in words]
        program = _unwrap(words)
        dialect = shell
        if program is not words:
            dialect = "cmd"
            # cmd /c "program ...": the program's own text, without the quote.
            wrapper = CMD_WRAPPER.match(segment)
            if wrapper:
                segment = segment[wrapper.end():]
        if program:
            yield program, segment, dialect


def _program(words):
    name = re.split(r"[\\/]", words[0])[-1]
    return name[:-4] if name.endswith(".exe") else name


def _unwrap(words):
    """The words of the program that cmd /c (or /k) runs, in place of cmd's own."""
    while words and _program(words) == "cmd":
        at = next((i for i, w in enumerate(words[1:], 1) if w in ("/c", "/k", "/r")), None)
        if at is None or not all(w.startswith("/") for w in words[1:at]):
            break
        words = words[at + 1:]
    return words


def _git_sub(words):
    """git's subcommand, past git's own options (-C <folder>, -c <setting>)."""
    i = 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] == "-c" else 1          # -C and -c, lower-cased
    return words[i] if i < len(words) else ""


def _after_program(tokens, program, sub):
    """The index of the first token after the program (and git's subcommand), or
    None when the program is not found as a token of its own."""
    names = [_program([t.strip("\"'").lower()]) if t.strip("\"'") else "" for t in tokens]
    if program not in names:
        return None
    i = names.index(program) + 1
    if sub:
        while i < len(tokens) and names[i] != sub:
            i += 2 if tokens[i] in ("-C", "-c") else 1
        i += 1
    return i


def _drop_pattern(text, program, sub=None):
    """The segment without its search pattern: the first argument that is neither an
    option nor an option's value. Unchanged when the search takes no pattern."""
    tokens = TOKEN.findall(text)
    i = _after_program(tokens, program, sub)
    if i is None:
        return text
    takes_value = VALUE_OPTIONS.get(program, set())
    insensitive = program in ("select-string", "sls", "findstr")
    slash = program == "findstr"
    if slash:
        # findstr /c:"text" is the pattern, and /g:file reads them from a file.
        given = [k for k in range(i, len(tokens)) if tokens[k].lower().startswith("/c:")]
        if given:
            return " ".join(t for k, t in enumerate(tokens) if k not in given)
        if any(t.lower().startswith("/g:") for t in tokens[i:]):
            return text
    while i < len(tokens):
        token = tokens[i]
        key = token.lower() if insensitive else token
        # Patterns read from a file (-f x, --file=x, -fx, -rf x): none to drop.
        if key in NO_PATTERN or key.startswith("--file=") or (
                program in GREP_LIKE and re.fullmatch(r"-[A-Za-z]*f.*", token)):
            return text
        # git log's pickaxe with its pattern attached: -S"text", -Gtext.
        if sub == "log" and re.match(r"-[SG].", token):
            return " ".join(tokens[:i] + tokens[i + 1:])
        if token.startswith("-") or (slash and token.startswith("/")):
            i += 2 if key in takes_value else 1
            continue
        # PowerShell's list of patterns: 'a', 'b'
        end = i + 1
        while insensitive and end < len(tokens) and (tokens[end - 1].endswith(",")
                                                     or tokens[end].startswith(",")):
            end += 1
        return " ".join(tokens[:i] + tokens[end:])
    return text


def _without_pattern(words, text):
    """text with its search pattern dropped, when the segment is a search."""
    program = _program(words)
    sub = _git_sub(words) if program == "git" else ""
    if program in SEARCHES or sub in GIT_SEARCHES:
        stripped = PATTERN_VALUE.sub(" ", text)
        if program in GREP_LIKE:
            stripped = ATTACHED_PATTERN.sub(" ", stripped)
        return stripped if stripped != text else _drop_pattern(text, program, sub)
    return text


def _arguments(text, program, sub):
    """The arguments that are neither options nor option values, after the program
    (and git's subcommand): the folders and files the command reads."""
    tokens = TOKEN.findall(text)
    i = _after_program(tokens, program, sub)
    if i is None:
        return []
    takes_value = VALUE_OPTIONS.get(program, set())
    insensitive = program in LISTINGS or program in ("select-string", "sls", "findstr")
    slash = program in ("findstr", "dir", "tree")         # cmd's /s-style options
    found = []
    for_value = False
    for token in tokens[i:]:
        key = token.lower() if insensitive else token
        if for_value:
            for_value = False
        elif token.startswith("-") or (slash and token.startswith("/")):
            for_value = key in takes_value
        else:
            found.append(token)
    return found


def _first_argument(text):
    """The first argument that is not an option (cd /d X, Set-Location -Path X)."""
    for token in TOKEN.findall(text):
        if not token.startswith("-") and token.lower() != "/d":
            return token
    return None


def _recursive(program, words, text="", dialect="PowerShell"):
    """True when the command reads a folder's files all the way down."""
    if program in ("rg", "ag", "tree", "find"):
        return True
    if program == "git":                       # outside the repository's own files
        return _git_sub(words) == "grep" and "--no-index" in words
    flags = [w for w in words[1:] if w.startswith(("-", "/"))]
    if program in ("grep", "egrep", "fgrep"):
        return any(w in ("--recursive", "--dereference-recursive")
                   or re.fullmatch(r"-[a-z]*[rR][a-z]*", w) for w in flags)
    if program in ("ls", "dir") and dialect == "Bash":
        # GNU ls and dir: -R recurses, -r only reverses the order (R13b round 3
        # review). Their options are case-sensitive, so read the text as written.
        return "/s" in flags or any(w == "--recursive" or re.fullmatch(r"-[A-Za-z]*R[A-Za-z]*", w)
                                    for w in TOKEN.findall(text))
    if program in LISTINGS:
        return any(w.startswith("-rec") or w in ("-r", "-depth", "/s") or
                   re.fullmatch(r"-[a-z]*R[a-z]*", w) for w in flags)
    if program == "findstr":
        return "/s" in flags
    return False


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
            # bash's (and PowerShell 7's) cd goes home; 5.1's stays. Taken as home,
            # which a search from there must not reach (R13b round 3 review).
            self.previous, self.here = self.here, "~"
        elif target == "-":
            self.previous, self.here = self.here, self.previous
        else:
            if verb in ("pushd", "push-location"):
                self.stack.append(self.here)
            self.previous, self.here = self.here, _resolve(self.here, target)


def _searches_above_data(text, program, sub, here, dialect="PowerShell"):
    """True when a recursive command searches local AppData or a folder above it
    without naming it whole: the folder it runs in, when it names no path, and the
    folder above a last part that is a wildcard (or, for findstr /s and a PowerShell
    or cmd listing, a file name it looks for in every folder below; an existing
    folder is listed itself, R13b round 3 review)."""
    args = _arguments(text, program, sub)
    folders = [] if args else [here]
    filters = program == "findstr" or (program in FILTER_LEAF and dialect != "Bash") or \
        program in ("get-childitem", "gci")
    for arg in args:
        head, sep, leaf = arg.strip("\"'").replace("/", "\\").rpartition("\\")
        if re.search(r"[*?\[]", leaf) or filters:
            if not re.search(r"[*?\[]", leaf) and program != "findstr":
                full = _canonical(_resolve(here, arg))
                if full and os.path.isdir(full):          # metadata only
                    continue
            if not sep:
                folders.append(here)
            elif head:
                folders.append(_resolve(here, head + "\\" if head.endswith(":") else head))
    return any(folder and (BARE_LOCAL.search(folder) or _above_data(_canonical(folder)))
               for folder in folders)


def _searches(words, text, here, dialect):
    """True when the command is a recursive search or listing that reaches the data:
    of local AppData named whole, of a folder above it, or from one."""
    program = _program(words)
    sub = _git_sub(words) if program == "git" else ""
    if not _recursive(program, words, text, dialect):
        return False
    expanded = _expand(_without_pattern(words, text))
    tokens = TOKEN.findall(expanded)
    if program == "git" and "-C" in tokens[:-1]:     # git -C runs it from another folder
        here = _resolve(here, tokens[tokens.index("-C") + 1])
    # cmd's own options (/s, /c) are not Git Bash drives.
    slash = program in ("findstr", "dir", "tree")
    paths = [_resolve(here, token) for token in tokens
             if not token.startswith("-") and not (slash and re.fullmatch(r"/\S", token))]
    return bool(BARE_LOCAL.search(expanded)
                or any(_above_data(_canonical(path)) for path in paths)
                or _searches_above_data(expanded, program, sub, here, dialect))


def _nested(text, shell):
    """The searches and listings inside a segment, each as (words, text, dialect):
    from the program's name to the end of the brackets around it (R13b round 3
    review: (...), { }, $(...), pwsh -c, bash -c, timeout, xargs)."""
    for match in NESTED.finditer(text):
        inner, depth = text[match.start():], 0
        for k, char in enumerate(inner):
            if char in "({":
                depth += 1
            elif char in ")}":
                if depth == 0:
                    inner = inner[:k]
                    break
                depth -= 1
            elif char == "`" and shell == "Bash":
                inner = inner[:k]
                break
        before = text[:match.start()]
        dialect = ("cmd" if NESTED_CMD.search(before) else
                   "PowerShell" if NESTED_POWERSHELL.search(before) else
                   "Bash" if NESTED_BASH.search(before) else shell)
        words = [w.strip("\"'").lower() for w in inner.split()]
        if words:
            yield words, inner, dialect


def _check_shell(command, cwd="", shell="Bash"):
    folder = _Folder(cwd)
    if _reaches_data(folder.here):
        return f"A command run in the live Unminute workspace is blocked. {ADVICE}"
    command = _drop_written(_substitute(command, shell), shell)
    blocked = f"A command naming the live Unminute workspace is blocked. {ADVICE}"
    searching = f"Searching a folder that holds the live Unminute workspace is blocked. {ADVICE}"
    # Local AppData and a data folder's name in one pipeline build the path; a
    # search's pattern is only text to find, so it adds neither.
    for statement in STATEMENTS.split(command):
        parts = list(_segments(statement, shell))
        expanded = _expand(" | ".join(_without_pattern(words, text)
                                      for words, text, _ in parts))
        if BARE_LOCAL.search(expanded) and DATA_WORD.search(expanded):
            return blocked
        # Local AppData passed along a pipeline (or to a loop) into a recursive
        # search or listing searches every folder below it (R13b round 3 review).
        if BARE_LOCAL.search(expanded) and any(
                _recursive(_program(w), w, t, d)
                for words, text, dialect in parts
                for w, t, d in [(words, text, dialect), *_nested(text, shell)]):
            return searching
    for words, text, dialect in _segments(command, shell):
        program = _program(words)
        sub = _git_sub(words) if program == "git" else ""
        segment = text
        # A search pattern may name anything; the paths searched may not.
        text = _without_pattern(words, text)
        expanded = _expand(text)
        tokens = TOKEN.findall(expanded)
        # git -C runs this one command from another folder.
        here = folder.here
        if program == "git" and "-C" in tokens[:-1]:
            here = _resolve(here, tokens[tokens.index("-C") + 1])
        # Each word as the path it names from the current folder, and the text as a
        # whole for paths built from several words.
        paths = [_resolve(here, token) for token in tokens if not token.startswith("-")]
        if _names_workspace(expanded) or any(_reaches_data(path) for path in paths):
            return blocked
        # A search or listing of local AppData, or a folder above it, reads the
        # data; so does one nested inside this segment.
        if _searches(words, segment, folder.here, dialect) or any(
                _searches(w, t, folder.here, d) for w, t, d in _nested(segment, shell)):
            return searching
        change = CHANGE_FOLDER.search(text)
        if change:
            folder.change(change.group(1), change.group(2))
            if _reaches_data(folder.here):
                return blocked
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
        # The tools read a leading ~ as the home folder (R13b round 2 review).
        tool_input = {key: _home(value) if key in FILE_FIELDS else value
                      for key, value in tool_input.items()}
        for field in FILE_FIELDS:
            if tool == "Grep" and field == "pattern":
                continue                        # Grep's pattern is text to find, not a path
            value = tool_input.get(field)
            if not isinstance(value, str) or not value:
                continue
            # A relative path is read from the working folder, and Glob's pattern
            # from its path.
            base = tool_input.get("path") if tool == "Glob" and field == "pattern" else None
            if base and cwd and not Path(base).is_absolute():
                base = ntpath.join(cwd, base)
            full = value if Path(value).is_absolute() else f"{base or cwd}/{value}"
            # With .. resolved: models\..\workspace is the workspace, and
            # workspace\..\models is the models.
            if any(_names_workspace(ntpath.normpath(v)) for v in (value, full)) or (
                    not (tool == "Glob" and field == "pattern") and _in_data(_canonical(full))):
                return f"{tool} on the live Unminute workspace is blocked. {ADVICE}"
            if _repo_extracted(value, cwd):
                return f"{tool} inside the repo's extracted/ folder is blocked. {ADVICE}"
        # Grep searches its path whole, and Glob lists what its pattern matches: from
        # local AppData, or a folder above it, they reach the workspace.
        start = tool_input.get("path") or cwd
        start = ntpath.join(cwd, start) if cwd and start else start
        canonical = _canonical(start) if start else ""
        if tool == "Grep" and canonical and (_above_data(canonical) or _in_data(canonical)):
            return f"{tool} over a folder holding the live Unminute workspace is blocked. {ADVICE}"
        pattern = tool_input.get("pattern")
        if tool == "Glob" and isinstance(pattern, str) and pattern and \
                _glob_reaches_data(canonical or start, pattern):
            return f"{tool} over a folder holding the live Unminute workspace is blocked. {ADVICE}"
        return None

    if tool in SHELL_TOOLS:
        command = tool_input.get("command")
        if not isinstance(command, str):
            return None
        for shell in SHELL_TOOLS[tool]:
            reason = _check_shell(command, cwd, shell)
            if reason:
                return reason
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
