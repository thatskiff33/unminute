# .claude/hooks/private_data_guard.py
# Claude Code PreToolUse hook, configured in .claude/settings.json. It keeps real
# meeting content out of an agent's context and out of commits (AGENTS.md,
# invariants P1 and P2):
#   - file tools may not touch a repo's extracted/ folder (old transcripts) or the
#     live workspace (%LOCALAPPDATA%\Unminute\workspace, or that data folder other
#     than its models), however the path is spelt: short (8.3)
#     names, junctions such as "Local Settings", \\?\ and \\localhost\C$ forms and
#     trailing dots are resolved first;
#   - shell commands (Bash, PowerShell, Monitor) may not name either of them as
#     something to read, list, copy or stage, nor search, copy or archive local
#     AppData or a folder above it whole (rg, grep -r, find, tree, ls -R,
#     Get-ChildItem -Recurse, dir /s, findstr /s, du, forfiles /s, where /r, git grep
#     --no-index, cp -r, rsync -a, Copy-Item -Recurse, robocopy /s, xcopy /s, tar,
#     zip -r, 7z, Compress-Archive, .NET's
#     AllDirectories, or interpreter code that walks it: os.walk, rglob, a
#     recursive readdir, from local AppData or the home folder named whole or read
#     from the environment, or from a quoted folder above local AppData, also after
#     a cd); also with a wildcard or file name as the last part, or no
#     folder named when run from local AppData or above (cd, pushd and popd are
#     followed, also at the start of bash -c (or -lc), pwsh -c and { } blocks and
#     after then, do or else; and to a $(...) that names local AppData or the
#     home folder, taken as that folder); inside brackets, a loop (bash's for list
#     is written where its variable is used), a string run by another shell or
#     after a wrapper such as timeout; or fed local AppData by a pipeline;
#   - shell commands may not search or list a source checkout that holds an
#     extracted/ folder recursively, except with rg or ag (which skip what
#     .gitignore names unless told not to);
#   - git add and git commit may not name a media file.
# A leading ~ is the home folder, as the Grep and Glob tools read it, and /c/x (or
# /mnt/c/x, /proc/cygdrive/c/x) is C:\x, as Git Bash reads it. Variables are
# written out as the folders they are: LOCALAPPDATA (and GetFolderPath(
# 'LocalApplicationData')), TEMP, APPDATA, USERPROFILE, HOME, HOMEDRIVE with HOMEPATH,
# SystemDrive, USERNAME, PUBLIC, OneDrive, GetFolderPath('UserProfile'), and any
# other whose value is a full path (windir, ProgramData), so a .. after one is seen
# for what it reaches; bash's operators on them (${LOCALAPPDATA:-x}, ${HOME%/}) are
# read as the variable, and the command is checked a second time with those that cut
# or replace part of the value (${TEMP%\*}, ${APPDATA%\Roaming}, ${TEMP/x/y},
# ${TEMP:0:30}) read as the folder above, as dirname gives (#221 round 2). The
# folder a cygpath, realpath, Resolve-Path, dirname or Split-Path gives is written
# out wherever it is used, up to eight levels deep. A
# variable set earlier in the same command (also after then, do, { or declare,
# typeset, readonly) is substituted where it is used, a $(...) or backtick value
# whole; a value exported (export, declare -x, after set -a) or never used is also
# checked where it is set. A variable straight after local AppData
# ($LOCALAPPDATA/$d) stands for any name. Each statement is checked, also one that
# is only an assignment (d=json.load(open(...)) in python -c code).
# Message text (gh's -m, -b, -t and -f fields, git's -m for commit, tag, merge,
# notes and stash, any program's --body, --message or --title, -Value of
# Set-Content, Add-Content, Out-File, sc or ac, quoted heredocs not fed to an
# interpreter or a program that reads file names from its input (xargs, read,
# cpio, tar -T, zip -@, --files-from, git --stdin-paths; quoted words that hold a
# space, such as a title, are not taken for that program, unless a quoted path),
# here-strings taken only by an assignment or a program that writes or sends text
# and keeps no copy in a variable (git only as a message: commit -F -; not with
# Tee-Object -Variable or -OutVariable), PowerShell text held in a variable) and
# the patterns given to text searches are ignored, so rules can be quoted and
# searched for. In bash, a
# backtick escaped with a backslash in a message is text (a Markdown code span);
# one that is not is run, and checked.
# Two layers run on every call (#221). main's earlier check runs first, unchanged in
# what it blocks for Unminute\workspace: after local AppData, a variable, a
# substitution, a wildcard folder name or a climb, or as a path of its own (so this
# hook blocks everything main did, apart from text that is only written: message
# options, -Value, quoted heredocs and here-strings, and such text read back later
# in the same command, a known gap below). The patterns for the data folder from
# before the rename were retired once the owner confirmed it deleted (#136).
# The deeper checks described above follow. Any error while checking blocks the
# call (exit 2), never only warns; the input is read as UTF-8, as Claude Code sends
# it (#221 round 2); input it cannot read blocks too. The command in settings.json
# fails closed as well (owner, 25 Oct 2026): it blocks when Python is not on PATH,
# turns any exit but 0 into 2, and sets onFailure to block, so a hook that cannot
# start, fails or runs past its 60 s timeout blocks the call (Claude Code 2.1.295
# and later; earlier versions let such a call through). A checkout with no hook
# file still allows, so the git command to get back is never blocked.
# Claude Code lets a call through when a hook runs past its
# time limit, so time is bounded too (#221 round 1). The common shapes of a
# long command are read in linear time (a 500 KB heredoc, 40,000 backslashes, 3,000
# message options, 50,000 closing brackets, 10,000 nested "$(, thousands of blank
# lines: milliseconds to a few hundred; resolving paths on disk is the main cost,
# and tests check the time). Not every shape is: each variable set and used later
# rescans the rest of the command (300 take about 0.15 s), and so do a chain of
# thousands of cd into ever deeper folders and thousands of heredocs or
# here-strings that are never closed (many unclosed @' openers: a 24 KB command
# took 1.8 to 5.8 s in two measurements, and a 90 KB one runs past the 20 s limit
# below, #236). So past 2,000 assignments or loops, 20
# million characters rescanned, or values that grow the command past twice its
# length plus a million characters (a=x; b=$a$a; c=$b$b ...), a command is blocked
# as too complex to check; and a command over 20,000 characters, or one that its
# variables grow past 20,000 (#221 round 2), is checked in a child process, and
# blocked if that takes more than 20 seconds or the child cannot start (so a long
# enough run of unclosed openers is blocked: it fails closed).
# It is a guard rail, not a security boundary: a text heuristic over the command,
# which cannot be complete. The target (owner, 19 Oct 2026): block the plain and
# common ways to read, list, search, copy or stage the data, never block ordinary
# work, and list what is left. Known gaps (#221):
#   - a path held in a variable from an earlier tool call, or set with Set-Variable
#     or New-Variable and used by name;
#   - a script file written in one step (Write, cat > f <<EOF, Set-Content) and run
#     in another, and text written to a file or the clipboard (-Value, a heredoc or
#     here-string to Set-Content or Set-Clipboard, gh -f) and read back later in
#     the same command (Get-Content $f | ForEach-Object { Get-Content $_ });
#   - a path the program builds itself or deliberately obscured: the app's own
#     default_root(), GetFolderPath(28), interpreter code that builds it in steps
#     the patterns do not join (one variable per part, string methods), PowerShell
#     backtick escapes (Unmin`ute), a name split across variables ($a$b), a format
#     string that builds the data folder ("{0}\Unminute" -f ...), string reversal
#     or encoding, a wildcard in place of AppData or Local (except straight before
#     Unminute\workspace), an interpreter nested in another (python -c
#     "subprocess.run(['rg', ...])"), and home-folder names not listed above
#     (HOMESHARE, GetEnvironmentVariable('USERPROFILE'), a subst or mapped drive);
#   - interpreter code that walks a tree another way (its own os.scandir or
#     os.listdir recursion, shutil.copytree), or from a folder held in a variable
#     or os.getcwd();
#   - a cd to a command's output that names no folder at or above local AppData
#     or the home folder ($(git rev-parse --show-toplevel), $(pwd), a variable
#     from an earlier call): the folder is then unknown, and later relative paths
#     are judged as written;
#   - a checkout's extracted/ folder reached from a folder above the checkout
#     (grep -r x ~/Local_Repos), or by interpreter code walking the checkout;
#   - a search program named by a variable or a full path (& $rg, /usr/bin/grep)
#     inside another command;
#   - a cd that is not at the start of a segment (after ;, &&, || or |), of bash -c,
#     pwsh -c or Invoke-Expression, of a { } block, or after then, do or else (for
#     example after a wrapper: time cd X): the folder is then not followed;
#   - heredoc lines read by a loop the heredoc ends (while read f; do cat "$f";
#     done <<'EOF'), or that become arguments through a substitution
#     (cat $(cat <<'EOF' ...), f=$(cat <<'EOF' ...); cat "$f"), and a program
#     named only inside quoted words that hold a space and that no shell or
#     interpreter on the line runs (env -S "python -" <<'EOF'); one fed to xargs,
#     read, cpio, tar -T, zip -@, --files-from or git --stdin-paths is checked;
#   - in a PowerShell command that holds no here-string, a heredoc span (<<EOF,
#     <<'EOF' or <<-EOF ... EOF, the delimiter quoted or not), which is dropped as
#     heredoc text as in Bash, although PowerShell has no
#     heredocs and runs what lies inside it (for example when one quoted argument
#     holds the opener and a later quoted string ends after the closing line):
#     code inside the span is not checked (#236; main's earlier check, the first
#     layer, misses it too);
#   - message text that PowerShell ends where the message pattern does not: a
#     message option's quoted value or here-string (', ", @' or @"; also -Value
#     and gh -f, -F, --field and --raw-field fields) closed by a typographic
#     quote (for a here-string, one before the closing @), which the pattern
#     reads on to a later plain quote, and a --message " inside a here-string
#     body, which the pattern pairs with a quote after the here-string's closer;
#     what lies between is dropped as text and not checked (#236; main's earlier
#     check, the first layer, misses it too);
#   - a message option or heredoc opener that the shell never starts, because it
#     is inside a quoted string or a comment (Bash and PowerShell) or is not a
#     heredoc operator at all (in Bash: a left shift in arithmetic, $((1<<EOF))
#     or (( )), a here-string <<<, an escaped \<<), which the patterns still pair
#     with a later quote or closing line; what lies between is dropped as text
#     and not checked (#242 reviews; main's earlier check, the first layer,
#     misses it too);
#   - a substitution that renames a folder in a variable's value
#     (${APPDATA/Roaming/Local});
#   - a cygpath, dirname or Split-Path nested more than eight levels deep (its
#     folder is then unknown);
#   - less common programs that walk a tree: attrib /s, icacls /t, takeown /r, fd,
#     Everything's es (copies and archives are checked: cp -r, rsync -a,
#     Copy-Item -Recurse, robocopy /s, xcopy /s, tar, zip -r, 7z,
#     Compress-Archive);
#   - a link made and used in the same command (a later command through it is
#     resolved), and a link inside a path more than 64 folders deep;
#   - Claude Code tools the hook's matcher in .claude/settings.json does not name,
#     such as publishing a local file as an artifact or sending one to the user
#     (the deny rules cover Read and Edit only);
#   - a python on PATH that is a shim (.cmd or .bat) which does not pass on
#     Python's exit code, so the guard's 2 becomes 0; and a session started in a
#     checkout of a branch from before the hook existed, which has no hook file
#     and so runs without the guard (CLAUDE_PROJECT_DIR is the folder the session
#     started in, also for work in other worktrees);
#   - Codex does not run the hook (AGENTS.md P2: review for Codex).
# Known false positives, which fail closed: from main's check, unminute\workspace
# as a relative path of its own (ls unminute/workspace from the folder above a
# checkout, ./unminute/workspace), after a variable ($env:TEMP\unminute\workspace)
# or in a folder named Local, and extracted named by any program but git (rsync
# --exclude extracted); the data folder named for a program that reads nothing
# (Test-Path $env:LOCALAPPDATA\Unminute); a recursive copy into the home folder or
# local AppData named whole (cp -r dist ~); a search pattern holding ;, &, | or a
# line break, which is split there like a command (grep -rn 'cd X; rg' tests reads
# X as a folder searched), so search for part of the text or use the Grep tool;
# a here-string naming the data and piped to gh or git with a quoted title that
# holds | (the title is split there, as on main), or anything else in the same
# command (a here-string is dropped as text only when the command is nothing but
# it, alone or assigned, piped on its closing line to one program that takes
# text), so give gh or git a here-string body as a command of its own; such a
# body naming the data is checked too when any non-ASCII or control character
# appears anywhere in the command, in the body or not (an em dash, a typographic
# apostrophe, a macron, a lone carriage return; tabs, line feeds and a carriage
# return before a line feed excepted), since PowerShell ends a string at some of
# them (#236); a Bash heredoc run by Monitor whose body holds a here-string naming
# the data (Monitor is checked as both Bash and PowerShell, and as PowerShell the
# heredoc is not dropped where a here-string is present, #236);
# local AppData named whole and piped into rg, ag or grep that reads the pipe (echo
# "$LOCALAPPDATA" | rg -o Local), which xargs would turn into a search of it; a
# search program's name in quoted text that is not a search, with local AppData
# after it (echo "rg -n x ~/AppData/Local" > notes.txt), which a quoted heredoc
# or Set-Content writes instead; any use of local AppData followed by a variable
# ($LOCALAPPDATA/$d), unless models follows; an operator on local AppData or a
# folder above it that changes the value (${HOME:+x}), read as the folder itself,
# and one that cuts or replaces part of a value but leaves it as it was, read as
# the folder above too (ls -R "${TEMP%/x}" lists local AppData's tree); a quoted
# single word on a heredoc's line that names an interpreter or xargs (--title
# "Python"); and a command too complex to check, or over 20,000 characters and
# too slow to check within 20 seconds, such as one with a long run of unclosed
# here-string openers (above).
# Stdlib only, so any Python works.
#
# Exit 2 with a reason on stderr blocks the tool call; exit 0 allows it. The
# settings run it through PowerShell (Git Bash may be missing), block when
# CLAUDE_PROJECT_DIR is not the full path of an existing folder (unset, empty,
# relative, drive-only or missing) or Python is not on PATH, skip it when the
# file is absent from an existing folder (a branch from before it existed; only
# for a hook path under 260 characters, which Windows PowerShell can test), and
# turn any exit but 0 into 2 (PowerShell would otherwise report the 2 as a 1).

import bisect
import fnmatch
import functools
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


# -- the first layer: main's reviewed checks (#207), unchanged in what they block ------
# They run before the deeper checks below, so this hook blocks everything main's did
# (#221: no regression against main). Only the two #207 round 8 nits widen them, and
# only the retired pre-rename folder's pattern has gone from them (#136).


def unminute_workspace_pattern(local_appdata):
    r"""The live workspace, %LOCALAPPDATA%\Unminute\workspace, however it is written.

    The repo is also called unminute and has a workspace/ package, so unminute
    and workspace straight after another folder's name (a checkout) are
    allowed. The pattern matches after AppData\Local, the configured
    LOCALAPPDATA folder, any variable or substitution ($env:LOCALAPPDATA,
    %LOCALAPPDATA% or %LOCALAPPDATA:~0%, ${d}, $(...), a backtick, an f-string's
    {base}), a folder name holding a wildcard (?ocal, [L]ocal, L*l), a climb
    from a folder ($env:TEMP\..), or as a piece of its own ('Unminute\workspace'
    given to Join-Path or appended to a folder, also straight after a variable
    that ends in a separator: ${d}Unminute, %D%Unminute), each with or without
    "." segments before it. A checkout in a folder named Local, named through a
    variable ($env:TEMP\unminute\workspace), written as ./unminute/workspace or
    reached by climbing more than one level is blocked too: that errs on the
    safe side. A single climb at the start (..\unminute\workspace) is a sibling
    checkout here; the deeper checks resolve it against the working folder.
    """
    # A closing character, read from the start of a run of them only, so a long
    # run is read once, not once per character (#221 round 1: quadratic on main
    # too). The first closing character of a run follows any % at its start.
    prefixes = [r"(?<![\w-])local(?:appdata)?", r"\$\{?[\w:]+\}?", r"%\w+(?::[^%\s]*)?%",
                r"(?<![%})\"'`])%*[)}`\"']", r"[\\/]\.\."]
    parts = [re.escape(part) for part in re.split(r"[\\/]+", local_appdata) if part]
    if parts:
        prefixes.append(r"(?<![\w-])" + r"[\\/]+".join(parts))
    # A folder name with a wildcard in it, from the wildcard on (linear: no wildcard
    # inside the repeated part; #207 round 8). Its tail already takes the closing
    # characters, so they are not read again after it (#221 round 1).
    wildcard = r"[*?\[][^\s\\/*?\[]*"
    after_local = r"(?:(?:" + "|".join(prefixes) + r")[%})\"'`]*|" + wildcard + r")[\\/]+"
    # Anywhere but straight after a folder's name, a dot or another separator,
    # with or without a leading separator (#207 round 8: a deny-list, so a
    # variable that keeps its trailing separator, ${d}Unminute, is matched).
    on_its_own = r"(?<![\w.\\/-])[\\/]*"
    # Either may be followed by "." segments (.\Unminute, Local\.\Unminute).
    current_folder = r"(?:\.[\\/]+)*"
    return re.compile(r"(?:" + after_local + r"|" + on_its_own + r")" + current_folder
                      + r"unminute[\\/]+workspace(?![\w-])", re.IGNORECASE)


UNMINUTE_WORKSPACE = unminute_workspace_pattern(os.environ.get("LOCALAPPDATA", ""))
PLAIN_MESSAGE = re.compile(
    r"(?<![\w-])(?:-m|-F|-b|-t|--message|--body|--title|--notes|--subject|--description)"
    r"(?:\s+|=)(\"(?:[^\"\\]|\\.)*\"|'[^']*'|@'.*?'@|@\".*?\"@)",
    re.DOTALL)
PLAIN_PATTERN = re.compile(r"(?<![\w-])(?:-e|--regexp|-pattern|-simplematch)(?:\s+|=)"
                           r"(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+)", re.IGNORECASE)


def _plain_names_workspace(text):
    return bool(UNMINUTE_WORKSPACE.search(text))


def _plain_shell(command):
    """main's shell check: a workspace path, extracted/ or staged media named in the
    text, with heredocs, message values and search patterns dropped."""
    command = PLAIN_MESSAGE.sub(" ", HEREDOC.sub(" ", command))
    for segment in SEGMENTS.split(command):
        words = segment.split()
        while words and (words[0] in ("sudo", "env", "time", "&", ".") or "=" in words[0]):
            words = words[1:]
        if not words:
            continue
        words = [w.strip("\"'").lower() for w in words]
        program, sub = _program(words), (words[1] if len(words) > 1 else "")
        text = segment
        if program in SEARCHES or (program == "git" and sub in GIT_SEARCHES):
            stripped = PLAIN_PATTERN.sub(" ", text)
            text = stripped if stripped != text else _plain_drop_first(text, program == "git")
        if _plain_names_workspace(text):
            return f"A command naming the live Unminute workspace is blocked. {ADVICE}"
        git_reads = sub in ("add", "stage", "mv") or "--no-index" in words
        if EXTRACTED.search(text) and (program != "git" or git_reads):
            return f"A command naming the repo's extracted/ folder is blocked. {ADVICE}"
        if program == "git" and sub in ("add", "commit", "stage"):
            media = MEDIA_TOKEN.search(text)
            if media:
                return (f"Staging or committing {media.group(0)} is blocked: recordings stay out "
                        "of the repo (AGENTS.md, invariant P1).")
    return None


def _plain_drop_first(text, git):
    """The segment without its first non-option argument (a search's pattern)."""
    tokens = TOKEN.findall(text)
    for i in range(2 if git else 1, len(tokens)):
        if not tokens[i].startswith("-"):
            return " ".join(tokens[:i] + tokens[i + 1:])
    return text


# -- the deeper checks ------------------------------------------------------------------

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


# The data folder, however the path is written: %LOCALAPPDATA%\Unminute, beside the
# models agents may use. That folder is blocked, except its models, but only next to
# local AppData, allowing a few characters of shell syntax between them (Join-Path,
# "$(...)", concatenation, a list of path parts in code). A clone of this repository
# is a folder named unminute holding the workspace package, so the name alone is not
# enough.
LOCAL_APPDATA = _local_appdata()
# models is allowed, but not a path that climbs out of it (models\..\workspace),
# whether the .. is in the same word or the next path part.
UP_NEXT = r"(?:[\\/]|['\"]\s*[,/+]?\s*r?['\"]|\s+)\.\.(?:[\\/'\"\s]|$)"
DATA_FOLDER = (r"Unminute(?!\w)(?![\\/'\",\s]{1,4}models(?!\w)"
               r"(?![^\s'\"]*?" + UP_NEXT + r"))")
# A backtick closes a bash substitution: `cygpath $LOCALAPPDATA`/Unminute (#221).
SEPARATOR = r"[%}\)\]'\"`\s+,\\/.]{1,10}"
LIVE_WORKSPACE = re.compile(LOCAL_APPDATA + SEPARATOR + r"(?:" + DATA_FOLDER + ")",
                            re.IGNORECASE)
DATA_NAMES = ("unminute",)
# A wildcard (or a {a,b} brace list) straight after local AppData (Unmin*, *, U*)
# that could stand for a data folder, unless what follows it is models. A variable
# there ($d, ${d}, %d%, a loop's) stands for any name too, and a ) or backtick
# ends the name ($(ls -d .../Unm*)/workspace) (R13b round 5 review).
WILDCARD_CHILD = re.compile(LOCAL_APPDATA + r"[%}\)'\"`]*[\\/]+"
                            r"([^\\/\s'\")`]*[*?\[{$%][^\\/\s'\")`]*)"
                            r"(?:[\\/]+([^\\/\s'\")`]*))?", re.IGNORECASE)
UNKNOWN_NAME = re.compile(r"\$\{[^}]*\}?|\$\(.*|\$(?:env:)?\w+|%\w+%?", re.IGNORECASE)
# Local AppData named as a folder of its own (not one inside it), and a data folder's
# name as a path of its own: together in one command (a pipeline, Join-Path
# -ChildPath, split arguments) they build the data folder.
BARE_LOCAL = re.compile(LOCAL_APPDATA + r"[%}\)\]'\"`]*(?=[\s|;),`]|$)", re.IGNORECASE)
DATA_WORD = re.compile(r"(?<![\w.\\/:$%~-])(?:" + DATA_FOLDER + ")",
                       re.IGNORECASE)
# Other names for folders in and beside local AppData, so that $env:TEMP\.. or
# $env:APPDATA\..\Local are seen for what they reach. TEMP is in local AppData
# unless moved; moving it only makes these patterns stricter.
VARIABLE = r"(?:\$env:|\$\{env:|\$\{|\$|%)"
LOCAL_VARIABLE = re.compile(VARIABLE + r"LOCALAPPDATA(?!\w)[}%]?"
                            # .NET's name for it, with its quotes or without (a cd's
                            # folder has them removed; #221 round 6)
                            r"|\[(?:System\.)?Environment\]::GetFolderPath\(\s*(?:['\"]?"
                            r"|\[(?:System\.)?Environment\+SpecialFolder\]::)"
                            r"LocalApplicationData['\"]?\s*\)", re.IGNORECASE)
TEMP_FOLDER = re.compile(VARIABLE + r"(?:TEMP|TMP)(?!\w)[}%]?"
                         r"|\[(?:System\.)?IO\.Path\]::GetTempPath\(\)"
                         r"|(?<![\w.~:$%/-])/tmp(?![\w.-])", re.IGNORECASE)
ROAMING_FOLDER = re.compile(VARIABLE + r"APPDATA(?!\w)[}%]?", re.IGNORECASE)
# The home folder by other names (R13b round 4 review): HOMEDRIVE with HOMEPATH (or
# HOMEPATH alone, on the current drive), GetFolderPath('UserProfile'); and the
# folders beside or inside it whose .. reaches it or C:\Users: SystemDrive,
# USERNAME after Users, PUBLIC and OneDrive.
HOME_FOLDER = re.compile(VARIABLE + r"(?:USERPROFILE|HOME)(?!\w)[}%]?"
                         r"|(?:" + VARIABLE + r"HOMEDRIVE(?!\w)[}%]?\s*)?"
                         + VARIABLE + r"HOMEPATH(?!\w)[}%]?"
                         r"|\[(?:System\.)?Environment\]::GetFolderPath\(\s*['\"]?UserProfile"
                         r"['\"]?\s*\)", re.IGNORECASE)
# bash's operators on the variables above (${LOCALAPPDATA:-x}, ${TEMP%/}, ${HOME#x}),
# read as the variable, which on Windows is always set. Bounded, so a brace never
# closed is linear.
FOLDER_VARIABLES = (r"(LOCALAPPDATA|TEMP|TMP|APPDATA|USERPROFILE|HOME|HOMEDRIVE|HOMEPATH"
                    r"|SystemDrive|USERNAME|PUBLIC|OneDrive(?:Commercial|Consumer)?)")
PARAMETER_OPERATOR = re.compile(
    r"\$\{" + FOLDER_VARIABLES +
    r"(?::?[-=+?]|##?|%%?|//?|\^\^?|,,?|:\s*-?\d)[^}\n]{0,256}\}", re.IGNORECASE)
# The operators that cut the end off the value or replace part of it (suffix
# removal, substitution, a substring), which can climb from the folder to one above
# it: ${TEMP%\*} is local AppData. The command is also checked with these read as
# the folder above (_readings; #221 round 2).
PARENT_OPERATOR = re.compile(
    r"\$\{" + FOLDER_VARIABLES +
    r"(?:%%?|//?|:\s*\d|:\s+-\d)[^}\n]{0,256}\}", re.IGNORECASE)
# Any other variable, written out when it holds a full path.
ENV_VARIABLE = re.compile(VARIABLE + r"([A-Za-z_]\w*)(?!\w)[}%]?")
FULL_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^;\n]*$")
SYSTEM_DRIVE = re.compile(VARIABLE + r"(?:SystemDrive|HOMEDRIVE)(?!\w)[}%]?", re.IGNORECASE)
USER_NAME = re.compile(VARIABLE + r"USERNAME(?!\w)[}%]?|\[(?:System\.)?Environment\]::UserName",
                       re.IGNORECASE)
PUBLIC_FOLDER = re.compile(VARIABLE + r"PUBLIC(?!\w)[}%]?", re.IGNORECASE)
ONEDRIVE_FOLDER = re.compile(VARIABLE + r"OneDrive(?:Commercial|Consumer)?(?!\w)[}%]?",
                             re.IGNORECASE)
# A folder followed by .. (but not a drive, a variable or .. itself), removed until
# none is left: $env:LOCALAPPDATA\Temp\..\Unminute is $env:LOCALAPPDATA\Unminute.
# The separator before the folder goes with it, so "X\Local/.." becomes "X", not
# "X\", whose \" would read as an escaped quote (R13b round 5 review); after a
# drive or at the start of a word, the separator stays (C:\x\.. is C:\).
UP_STEP = re.compile(r"(?<=[^\\/\s'\":])[\\/]+(?!\.\.?[\\/])[^\\/\s'\"]+[\\/]+\.\.(?=[\\/'\"\s]|$)")
ROOT_UP_STEP = re.compile(r"(?<=[\\/])(?!\.\.?[\\/])[^\\/\s'\"]+[\\/]+\.\.(?=[\\/'\"\s]|$)")
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
# Which program a message option belongs to matters: -m, -b and -t are message text
# only for gh, and -m for the git subcommands that take a message (ls -t, cat -b,
# git diff -b and git commit -t take a path; R13b rounds 4 and 5), gh's -f/-F
# fields only for gh, and -Value only for the cmdlets that write text. The long
# forms are message text for any program.
MESSAGE_VALUE = re.compile(
    r"(?<![\w-])(?P<option>-m|-b|-t|--message|--body|--title|--notes|--subject|--description"
    r"|(?i:-value))"
    r"(?:\s+|=)(?:" + DOUBLE + r"|'[^']*'|@'.*?'@|@\".*?\"@)"
    r"|(?<![\w-])(?:-f|-F|--field|--raw-field)(?:\s+|=)"
    r"(?:" + FIELD_KEY + r"(?:" + DOUBLE + r"|'[^']*')"
    r"|\"" + FIELD_KEY + r"(?:[^\"\\]|\\.)*\"|'" + FIELD_KEY + r"[^']*')",
    re.DOTALL)
SHORT_MESSAGE = {"-m", "-b", "-t"}
GIT_MESSAGE = {"commit", "tag", "merge", "notes", "stash"}
TEXT_WRITERS = {"set-content", "add-content", "out-file", "sc", "ac"}
# Programs that only write, show or send the text they are given (a here-string's
# lines are names to read for any other, #221), an assignment, and programs that
# turn their input lines into arguments.
TEXT_TAKERS = TEXT_WRITERS | {"gh", "git", "write-output", "write-host", "echo", "clip",
                              "set-clipboard", "out-string", "tee-object", "out-null"}
ASSIGNED = re.compile(r"\s*\$[\w:]+\s*=\s*")
READS_INPUT = re.compile(r"(?<![\w.-])(?:xargs|read|mapfile|readarray|parallel|cpio)(?![\w.-])")
# git's options that read a message from standard input (commit -F -, --file=-).
GIT_STDIN_MESSAGE = re.compile(r"(?<!\S)(?:-F\s*-|--file(?:=|\s+)-)(?!\S)")
# Variables set in the command itself, substituted into what follows. PowerShell
# ($name = value) and bash (NAME=value, export NAME=value); the value's end is found
# by _value_end, so $(...), backticks and nested quotes stay whole.
ASSIGNMENT = {
    # A line break is a separator itself, so only spaces and tabs are skipped after
    # one: skipping any whitespace read a run of blank lines again from each line
    # (#221 round 1: quadratic).
    "PowerShell": re.compile(r"(?:^|(?<=[;&|{(\n]))[ \t]*\$(\w+)\s*=(?!=)\s*"
                             r"(@'.*?'@|@\".*?\"@|'[^']*'|\"(?:[^\"`]|`.)*\"|[^;\n|&]+)",
                             re.DOTALL),
    # Also after then, do, else or {, and after declare, typeset or readonly with
    # their flags (R13b round 5 review).
    "Bash": re.compile(r"(?:^|(?<=[;&|\n(]))[ \t]*(?:(?:then|do|else|\{)\s+)?"
                       r"(?:(?:export|local|declare|typeset|readonly)(?:\s+[-+]\w+)*\s+)?"
                       r"([A-Za-z_]\w*)=()"),
}
# bash: a value exported to child processes (export, declare -x, typeset -x), and
# set -a (or set -o allexport), after which every value set is exported.
EXPORTED = re.compile(r"(?<![\w-])export\s|(?<![\w-])(?:declare|typeset|local|readonly)\s+"
                      r"(?:[-+]\w+\s+)*?-\w*x\w*\s")
EXPORT_ALL = re.compile(r"(?<![\w-])set\s+(?:[-+]\w+\s+)*?(?:-\w*a\w*|-o\s+allexport)(?![\w-])")
# Bounds on the work of writing variables out (#221 round 1): more assignments or
# loops than any real command holds, characters rescanned for used variables, and
# growth past twice the command plus an allowance. Past any of them the command is
# blocked as too complex for the guard, never let through unchecked.
MAX_ASSIGNMENTS = 2000
MAX_WORK = 20_000_000
GROWTH_ALLOWANCE = 1_000_000


class TooComplex(Exception):
    """The command is too complex for the guard to check in time: it is blocked."""


class NeedsChild(Exception):
    """The command grew too long to check in the hook's own process, which has no
    time limit: main() checks it again in a child process that has one."""


class _Growth:
    """The lengths a command may grow to while its variables are written out:
    twice its own plus GROWTH_ALLOWANCE (TooComplex past it) and, in the hook's
    own process, grown_limit or its own length if longer (NeedsChild past it)."""

    def __init__(self, command, grown_limit=None):
        self.most = 2 * len(command) + GROWTH_ALLOWANCE
        self.parent = None if grown_limit is None else max(grown_limit, len(command))

    def check(self, length):
        if self.parent is not None and length > self.parent:
            raise NeedsChild
        if length > self.most:
            raise TooComplex


TOKEN = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+", re.DOTALL)
# Backslashes before a double quote, which PowerShell does not read as an escape.
# From the first backslash of a run only, so a long run is linear (#221).
PS_BACKSLASHES = re.compile(r"(?<!\\)\\+(?=\")")
# PowerShell's ${C:\path}, which reads the file, even inside double quotes.
PROVIDER_VARIABLE = re.compile(r"\$\{([A-Za-z]:[^}]*)\}")
# A search's pattern: the value of its pattern option, or else its first quoted argument.
# PowerShell also joins a value with a colon (-Pattern:x), and grep and rg take -e's
# value attached (-ex), case-sensitively (R13b round 3 review).
# -e is case-sensitive (grep's -E is a switch) and -SimpleMatch is a switch, not a
# pattern option (R13b round 4 review).
PATTERN_VALUE = re.compile(r"(?<![\w-])(?:-e|--regexp|(?i:-pattern))(?:\s+|=|:)"
                           r"(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+)")
ATTACHED_PATTERN = re.compile(r"(?<![\w-])-e(?![\s=:-])(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+)")
GREP_LIKE = {"grep", "egrep", "fgrep", "rg", "ag"}
# A PowerShell option with its value joined by a colon (-Path:C:\x).
COLON_VALUE = re.compile(r"-[A-Za-z]\w*:(?!:)(\S.*)$")
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
NESTED_BASH = re.compile(r"(?:bash|sh|zsh)(?:\.exe)?\s.*?-[a-z]*c\s+[\"']?\s*$", re.IGNORECASE)
# cmd's own part of cmd /c "program ...", with the opening quote.
CMD_WRAPPER = re.compile(r"\s*(?:\S*[\\/])?cmd(?:\.exe)?\s+(?:/[^\s/]+\s+)*?/[ckr]\s+[\"']?",
                         re.IGNORECASE)
STATEMENTS = re.compile(r"\|\||&&|[;&\n]")         # a pipeline stays together
# A change of folder at the start of a segment, of a $(...) or of a string another
# shell runs: cmd /c "cd ... && ...", bash -c, pwsh -c, Invoke-Expression, or after
# bash's then, do or else (R13b round 4 review). bash's -c may be joined with other
# flags (bash -lc, sh -ec).
CHANGE_FOLDER = re.compile(r"^[\s(\{\"'$]*(?:(?:cmd(?:\.exe)?\s+/[ck]\s+"
                           r"|(?:\S*[\\/])?(?:bash|sh|zsh|pwsh|powershell)(?:\.exe)?\s+"
                           r"(?:-\S+\s+)*?-[a-z]*c\w*\s+"
                           r"|(?:iex|invoke-expression)\s+)[\"'{]?\s*)?"
                           r"(?:(?:then|do|else)\s+)?"
                           r"(cd|chdir|pushd|popd|sl|set-location|push-location|pop-location)"
                           r"(?=[\s\"');]|$)(.*)", re.IGNORECASE | re.DOTALL)

# A change of folder at the start of a { } block later in a segment, as in
# if (...) { cd X } (R13b round 4 review).
BLOCK_CHANGE = re.compile(r"\{\s*(cd|chdir|pushd|sl|set-location|push-location)"
                          r"(?=[\s\"');]|$)([^}]*)", re.IGNORECASE)

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
# Programs whose options start with / (cmd's style), which are not Git Bash drives.
SLASH_PROGRAMS = ("findstr", "dir", "tree", "forfiles", "where", "robocopy", "xcopy")
# Copies and archives, which with recursion read every folder below what they copy
# (#221: the owner's target names copying). Always recursive: tar, 7z and
# Compress-Archive; the others with their recursive option.
ARCHIVERS = ("tar", "7z", "7za", "compress-archive")
COPIERS = ("cp", "rsync", "copy-item", "cpi", "copy", "robocopy", "xcopy", "zip")
# Code run by an interpreter that walks a folder's tree, and the home folder named
# whole in code: with local AppData (or the home folder) named whole, it reads every
# folder below (R13b round 4 review).
WALKER = re.compile(r"os\.walk\b|\.rglob\(|\bwalk(?:Sync)?\(|recursive\s*[=:]\s*true"
                    r"|\bglob\([^)]*\*\*|File::Find|Find\.find|Dir\.glob", re.IGNORECASE)
ALL_DIRECTORIES = re.compile(r"AllDirectories", re.IGNORECASE)   # .NET's recursive listing
BARE_HOME = re.compile(r"(?<![\w.~\\/:-])~[\\/]?(?=[\s'\"),;|}`]|$)")
HOME_CODE = re.compile(r"Path\.home\(\)(?!\s*(?:/|\.joinpath))"
                       r"|expanduser\(\s*r?['\"]~[\\/]?['\"]\s*\)(?!\s*,)"
                       # the home folder read from the environment, which fails
                       # closed even when a subfolder is joined to it (R13b round 4)
                       r"|(?:environ(?:\.get)?\s*[\[(]\s*|getenv\(\s*|process\.env\.|ENV\[\s*)"
                       r"['\"]?(?:USERPROFILE|HOME|HOMEPATH)(?!\w)")
# A quoted string in code, which a walk may start from.
CODE_STRING = re.compile(r"(['\"])([^'\"\n]+)\1")

FILE_FIELDS = ("file_path", "notebook_path", "path", "pattern")
FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep"}
# Monitor runs a command too; which shell it uses is not fixed, so both rules apply.
SHELL_TOOLS = {"Bash": ("Bash",), "PowerShell": ("PowerShell",),
               "Monitor": ("Bash", "PowerShell")}

ADVICE = ("It may hold real meeting content (AGENTS.md, invariant P2). Use --status for counts, "
          "or synthetic files in a temporary folder.")
SIMPLER = ("Split it into simpler commands, or write long text to a file with the Write tool "
           "first (AGENTS.md, invariant P2).")
COMPLEX = ("This command is too complex for the private data guard to check (thousands of "
           f"variables, or values that grow at each step), so it is blocked. {SIMPLER}")


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
# accept too (R13b round 3 review), and /proc/cygdrive/c (round 4).
MSYS_DRIVE = re.compile(r"^[\\/](?:mnt[\\/]|proc[\\/]cygdrive[\\/])?([A-Za-z])(?=[\\/]|$)")


def _home(path):
    """path with a leading ~ (~, ~\\x, ~/x, ~name) as a home folder, as shells and
    Claude Code's Grep and Glob read it, and a Git Bash drive (/c/x) as C:\\x;
    anything else unchanged."""
    if isinstance(path, str) and path.startswith("~"):
        return ntpath.expanduser(path)
    if isinstance(path, str):
        return MSYS_DRIVE.sub(lambda m: m.group(1).upper() + ":\\", path, count=1)
    return path


@functools.lru_cache(maxsize=4096)        # a command names the same folders often
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
    # Resolving a path costs a call per folder in it, so one more than 64 folders
    # deep (none is, in practice) is taken as written (#221: a long chain of cd
    # into ever deeper folders stays fast).
    if re.match(r"[A-Za-z]:\\", path) and os.name == "nt" and path.count("\\") <= 64 \
            and not _remote_drive(path):
        try:
            path = ntpath.realpath(path)
        except (OSError, ValueError):
            pass
        path = DEVICE_DRIVE.sub("", path)
    return ntpath.normcase(path).rstrip("\\")


def _data_folders():
    """This user's data folders, canonical, each with the name of the part agents may
    use: %LOCALAPPDATA%\\Unminute and its models."""
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return ()
    return ((_canonical(ntpath.join(local, "Unminute")), "models"),)


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
                # Past the data folder: only models may follow.
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
        children = _brace_alternatives(UNKNOWN_NAME.sub("*", match.group(1).lower()))
        below = (match.group(2) or "").lower()
        if below != "models" and any(fnmatch.fnmatchcase(name, child)
                                     for name in DATA_NAMES for child in children):
            return True
    return False


def _variable_path(match):
    """A variable's value when it is a full path (windir, ProgramData), so that a ..
    after it is seen for what it reaches (R13b round 4 review); otherwise unchanged."""
    value = os.environ.get(match.group(1), "")
    return value if FULL_PATH.match(value) else match.group(0)


def _expand(text):
    """text with local AppData's variables (and TEMP, APPDATA, the home folder) written
    as the folders they are, trailing dots and string prefixes dropped, and each folder
    followed by .. removed. A folder from cygpath, dirname, Split-Path and the like is
    written as the folder (dirname X is X\\..), and bash's ${NAME:-x}, ${NAME%/} and
    other operators on these variables as the variable (#221 round 1: they were
    followed only after a cd)."""
    text = _folder_of(text)
    text = PARAMETER_OPERATOR.sub(lambda m: "${" + m.group(1) + "}", text)
    text = LOCAL_VARIABLE.sub(lambda m: "~\\AppData\\Local", text)
    text = TEMP_FOLDER.sub(lambda m: "~\\AppData\\Local\\Temp", text)
    text = ROAMING_FOLDER.sub(lambda m: "~\\AppData\\Roaming", text)
    text = HOME_FOLDER.sub(lambda m: "~", text)
    profile = os.environ.get("USERPROFILE", "").rstrip("\\/")
    text = SYSTEM_DRIVE.sub(lambda m: os.environ.get("SystemDrive") or "C:", text)
    user = ntpath.basename(profile) or os.environ.get("USERNAME", "")
    if user:
        text = USER_NAME.sub(lambda m: user, text)
    public = os.environ.get("PUBLIC") or ntpath.join(ntpath.dirname(profile) or "C:\\Users",
                                                     "Public")
    text = PUBLIC_FOLDER.sub(lambda m: public, text)
    text = ONEDRIVE_FOLDER.sub(lambda m: "~\\OneDrive", text)
    text = ENV_VARIABLE.sub(_variable_path, text)
    text = TRAILING_DOTS.sub("", text)
    text = STRING_PREFIX.sub("", text)
    previous = None
    while previous != text:
        previous, text = text, UP_STEP.sub("", text)
        if text == previous:
            text = ROOT_UP_STEP.sub("", text)
    return text


def _resolve(here, word):
    """The folder or file word names, run from the folder here ("" when unknown). A
    leading ~ is expanded first, so ~\\.. keeps the folder it climbs from (R13b round
    4 review)."""
    word = _home(word.strip("\"'()`"))
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


# A bash backtick that is not escaped: after no backslash or an even number of them.
# From the first backslash of a run only, so a long run is linear.
BASH_BACKTICK = re.compile(r"(?<!\\)(?:\\\\)*`")


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
        # A backtick after an odd number of backslashes is literal text, which
        # bash prints (a Markdown code span in a message, #221 round 1).
        ticks = [m.end() - 1 for m in BASH_BACKTICK.finditer(text)]
        # An unclosed last backtick is bash's syntax error: nothing runs.
        parts += [text[ticks[k] + 1:ticks[k + 1]] for k in range(0, len(ticks) - 1, 2)]
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


# The start of a command that is only a here-string, or one assigned to a variable.
# Only ASCII white space: Python's \s also covers U+001C to U+001F, which
# PowerShell reads as word characters (round 8).
ALONE_LEAD = re.compile(r"[ \t\r\n]*(?:\$[A-Za-z0-9_:]+[ \t]*=[ \t]*)?")
# A command that may hold a here-string dropped as text: printable ASCII, tabs and
# line feeds, with a carriage return only before a line feed. PowerShell also ends
# a here-string or a string at typographic quotes and a lone carriage return, so
# any of those, or any other non-ASCII or control character, keeps it checked.
PLAIN_COMMAND = re.compile(r"(?:[\t\n\x20-\x7e]|\r\n)*\Z")
# Nothing but white space to the end of the command.
ALONE_TAIL = re.compile(r"\s*\Z")
# What may not follow a here-string on its closing line, outside quoted titles:
# a second pipe, a bracket, a backtick, a separator or a redirect.
NOT_ALONE = re.compile(r"[|()\[\]{}`;&<>]")


# PowerShell closes a quoted string at these too, which _unquoted does not pair.
SMART_QUOTES = frozenset("‘’‚‛“”„")


def _alone(command, start, end, lead_end):
    """True when the whole command is this one here-string, alone or assigned to a
    variable, and its closing line holds nothing or a single pipe to one program
    with no bracket, backtick, separator or redirect (#221 rounds 4 to 6). Only
    then may it be text only: anything before, after or around it could read it,
    and finding every such shape by its context did not converge. A lone carriage
    return (a line break to PowerShell) or a typographic quote (which closes a
    string there) after it keeps it checked (round 7). lead_end is where the
    command's lead (white space and an optional assignment) ends, found once per
    command, so this is linear: every here-string but one fails at once."""
    if lead_end != start or not PLAIN_COMMAND.match(command):
        return False
    line_end = command.find("\n", end)
    if line_end >= 0 and not ALONE_TAIL.match(command, line_end):
        return False
    rest = command[end:line_end if line_end >= 0 else len(command)].strip()
    if "\r" in rest or not SMART_QUOTES.isdisjoint(rest):
        return False
    if not rest:
        return True
    return rest.startswith("|") and not NOT_ALONE.search(_unquoted(rest[1:]))


# A quoted program path (a drive, a root, ~ or . at its start), which is kept.
PROGRAM_PATH = re.compile(r"[\"'](?:[A-Za-z]:|[\\/~.])")


def _unquoted(text):
    """text without the closed quoted strings that hold a space: a title or other
    words given as an argument (gh pr create --title "Move to Python 3.14"), which
    are not the program that takes a heredoc (#221 round 2). A quoted single word
    ("python", "--files-from=-") and a quoted program path ("C:/Program
    Files/Python/python.exe") are kept, and so is everything from a quote left
    open. One pass, so linear."""
    out, i, n = [], 0, len(text)
    while i < n:
        char = text[i]
        if char not in "\"'":
            out.append(char)
            i += 1
            continue
        j = i + 1
        while j < n and text[j] != char:
            j += 2 if char == '"' and text[j] in "\\`" else 1
        if j >= n:                               # left open: the rest as written
            out.append(text[i:])
            break
        quoted = text[i:j + 1]
        spaced = any(c.isspace() for c in quoted)
        out.append(" " if spaced and not PROGRAM_PATH.match(quoted) else quoted)
        i = j + 1
    return "".join(out)


def _drop_written(command, shell):
    """command with heredocs, here-strings and message text dropped, unless an
    interpreter runs them, keeping the parts the shell runs first."""
    def heredoc(match):
        # The rest of the heredoc's first line too: cat <<'EOF' | python -
        first_line = match.group(0).split("\n", 1)[0]
        piped = re.split(r"\|\||&&|;", first_line)[0]
        # Quoted words (a title) are not the program that takes it (#221 round 2).
        consumer = _unquoted(_consumer(command, match.start(),
                                       match.start() + len(first_line)) + " " + piped)
        # Kept too when xargs, read, tar -T and the like read its lines as names of
        # files to read (#221).
        if INTERPRETER.search(consumer) or _reads_names(consumer):
            return match.group(0)
        if match.group(1):                       # a quoted delimiter: plain text
            return " "
        return _run_parts(match.group(0), shell)

    def here_string(match):
        # Kept, as names to read, unless only an assignment or a program that takes
        # text takes it (#221: @'...'@ | Get-Content reads the files it names).
        # Which programs take the text is decided with the quotes kept, as on main:
        # _unquoted pairs quotes naively, so an escaped quote or a comment could
        # hide the program that reads the lines (round 3 review). Quoted titles are
        # dropped only when looking for an interpreter ("Move to Python 3.14").
        # Dropped as written text only when the whole command is this here-string,
        # alone or assigned, piped on its closing line to one program that only
        # takes text (rounds 4 to 6); every other shape keeps it checked, as main.
        consumer = _consumer(command, match.start(), match.end())
        if (not _alone(command, match.start(), match.end(), lead_end)
                or INTERPRETER.search(_unquoted(consumer)) or not _takes_text(consumer)):
            return match.group(0)
        return " " if match.group(1) == "'" else _run_parts(match.group(0), shell)

    def message(match):
        text = match.group(0)
        option = (match.group("option") or "").lower()
        words = _words_at(match.string, match.start(), starts)
        program = _program(words) if words else ""
        message_option = program == "gh" or (
            program == "git" and option == "-m" and _git_sub(words) in GIT_MESSAGE)
        # Another program's -m, -b, -t, -Value or -f takes a value that may be a
        # path: checked as one (R13b rounds 4 and 5).
        if (option in SHORT_MESSAGE and not message_option) or (
                option == "-value" and program not in TEXT_WRITERS) or (
                not option and program != "gh"):
            return text
        literal = text.rstrip().endswith("'") or text.rstrip().endswith("'@")
        return " " if literal else _run_parts(text, shell)

    # PowerShell has no heredocs: where a PowerShell command holds a here-string, a
    # <<'EOF' span is text it may run, and dropping it first could hide the
    # here-string's real closer (round 9). Elsewhere it is dropped as before.
    if shell != "PowerShell" or not HERE_STRING.search(command):
        command = HEREDOC.sub(heredoc, command)
    if shell == "PowerShell":
        lead_end = ALONE_LEAD.match(command).end()    # once per command (_alone)
        command = HERE_STRING.sub(here_string, command)
    starts = _segment_starts(command)
    return MESSAGE_VALUE.sub(message, command)


def _takes_text(consumer):
    """True when everything around a here-string (_consumer) only takes text: an
    assignment, or programs that write or send it (gh, git, Set-Content, Out-File,
    Write-Output, the clipboard)."""
    for part in consumer.split("|"):
        if not part.strip() or ASSIGNED.fullmatch(part):
            continue
        words = _lead_words(part.lstrip(" \t({$\"'"))
        if not words or _program(words) not in TEXT_TAKERS:
            return False
        # A program that also keeps the text in a variable (Tee-Object -Variable,
        # -OutVariable and the other common parameters), which a later command can
        # read as names (#221 round 2). gh, git and clip are not cmdlets (git -v).
        if _program(words) not in ("gh", "git", "clip") and any(
                _stores_variable(word) for word in words[1:]):
            return False
        # git takes text from its input only as a message (commit -F -); other
        # subcommands read the files it names (hash-object --stdin-paths,
        # #221 round 1).
        if _program(words) == "git" and not (_git_sub(words) in GIT_MESSAGE
                                             and GIT_STDIN_MESSAGE.search(part)):
            return False
    return True


# PowerShell's parameters that put a command's output or a stream in a variable,
# with their aliases. Any unambiguous prefix names one (-OutV, -Var).
VARIABLE_PARAMETERS = ("variable", "outvariable", "pipelinevariable", "informationvariable",
                       "errorvariable", "warningvariable")
VARIABLE_ALIASES = {"ov", "pv", "iv", "ev", "wv"}


def _stores_variable(word):
    """True when a PowerShell word is a parameter that stores the output, or a
    stream holding the text, in a variable: -Variable, -OutVariable (-ov),
    -PipelineVariable (-pv), -InformationVariable (-iv) and the like, by any
    prefix that holds its v (-va, -outv), with or without a joined :value."""
    if not word.startswith("-"):
        return False
    name = word[1:].split(":", 1)[0].lower()
    return name in VARIABLE_ALIASES or (
        "v" in name and any(full.startswith(name) for full in VARIABLE_PARAMETERS))


def _reads_names(consumer):
    """True when what takes a heredoc reads its lines as names of files to read:
    xargs, read and the like, cpio, tar -T, zip -@, rsync or tar --files-from, git
    --stdin-paths or --pathspec-from-file (#221 round 1). Word by word, so linear."""
    if READS_INPUT.search(consumer):
        return True
    for part in consumer.split("|"):
        words = [w.strip("\"'") for w in part.split()]
        programs = {_program([w.lower()]) for w in words if w}
        for word in words:
            if word.startswith(("--files-from", "--stdin-paths", "--pathspec-from-file")):
                return True
            if programs & {"tar", "bsdtar"} and re.fullmatch(r"-[A-Za-z]*T.*", word):
                return True
            if "zip" in programs and re.fullmatch(r"-\w*@", word):
                return True
    return False


def _substitute(command, shell, grown_limit=None):
    """command with variables set in it written out where they are used later. A
    literal value (single quotes, a literal here-string), or PowerShell double-quoted
    text other than what it runs, is left only where used, when it is the whole of
    what is assigned and every later use is one written out here; otherwise it is
    also kept where it is set (R13b round 3 review: concatenation, $script:p and
    Get-Variable p hid a path). With grown_limit, NeedsChild once the command grows
    past that many characters (or its own length, if longer)."""
    pattern = ASSIGNMENT.get(shell)
    if not pattern:
        return command
    # Bounds on the work, past which the command is blocked as too complex rather
    # than checked slowly (a hook that runs past its time limit lets the call
    # through): values that double at each step (a=x; b=$a$a; c=$b$b ...) grow
    # the command exponentially, and each used variable rescans the rest of it.
    # In the parent process, a command grown past grown_limit goes to the child
    # process and its time limit instead: the later scans are not all linear, and
    # a short command can grow to a megabyte (#221 round 2).
    bound = _Growth(command, grown_limit)
    work, count = 0, 0
    if shell == "Bash":
        command = _for_lists(command, bound)
    # bash's set -a (or set -o allexport) exports every variable set after it.
    exports_all = EXPORT_ALL.search(command)
    flags = re.IGNORECASE if shell == "PowerShell" else 0
    scope = r"(?:(?:script|global|local|private):)?" if shell == "PowerShell" else ""
    position = 0
    while True:
        match = pattern.search(command, position)
        if not match:
            break
        count += 1
        if count > MAX_ASSIGNMENTS or work > MAX_WORK:
            raise TooComplex
        bound.check(len(command))
        start = match.start(2)
        end = _value_end(command, start, shell, match.end(2))
        position = max(end, match.end())
        name, value = match.group(1), command[start:end].strip()
        if not value:
            continue
        # A value passed to child processes is used without its name being written
        # (export D=...; python x.py): it is never hidden (#221 round 1).
        exported = shell == "Bash" and (
            EXPORTED.search(match.group(0)) is not None
            or (exports_all is not None and exports_all.start() < match.start()))
        use = re.compile(r"\$\{?" + scope + re.escape(name) + r"(?!\w)\}?", flags)
        # Any mention of the name: $name, $any:name, the bare word (Get-Variable), or
        # the variable: drive (Get-Content variable:p, R13b round 4 review).
        mention = re.compile(r"\$\{?(?:\w+:)?" + re.escape(name) + r"(?!\w)"
                             r"|(?<![\w$:-])" + re.escape(name) + r"(?![\w-])"
                             r"|variable:[\\/]?" + re.escape(name) + r"(?!\w)", flags)
        rest = command[end:]
        whole = re.match(r"[ \t]*(?:[;\n|&]|$)", rest) is not None and not exported
        # A name not in the rest at all is neither used nor mentioned: no pattern
        # need scan the rest (#221: many assignments stay fast). Its value is then
        # kept where it is set, so main's check still sees it (#221 round 1).
        if (name.lower() not in rest.lower()) if flags else (name not in rest):
            after, hidden = rest, False
        else:
            work += len(rest)
            uses = len(use.findall(rest))
            bound.check(len(command) + uses * len(value))
            after = use.sub(lambda m, text=value: text, rest)
            hidden = whole and uses > 0 and len(mention.findall(rest)) == uses
        literal = value.startswith("'") or value.startswith("@'")
        if literal and hidden:
            middle = "''"
        elif shell == "PowerShell" and value.startswith('"') and hidden:
            # A PowerShell variable holds text: only what the shell runs in it is
            # checked here; the whole value is checked where it is used.
            middle = _run_parts(value, shell)
        else:
            middle = command[start:end]
        if after is not rest or middle != command[start:end]:
            command = command[:start] + middle + after
        position = start + len(middle)
    bound.check(len(command))
    return command


# The list ends at ;, a line break or the word do (linear: no whitespace run is read
# again at each step of the list; #221 round 1).
FOR_LIST = re.compile(r"(?:^|(?<=[;&|\n(]))[ \t]*for\s+([A-Za-z_]\w*)\s+in(?=\s)([^;\n]*?)"
                      r"(?:;|\n|(?<=\s)do(?!\w))")


def _for_lists(command, bound):
    """command with each $NAME after bash's for NAME in LIST written as the list, so
    a loop over local AppData named whole is seen for what it searches (R13b
    round 5 review). TooComplex past MAX_ASSIGNMENTS loops; the bound (_Growth) on
    its length."""
    loops = list(FOR_LIST.finditer(command))
    if len(loops) > MAX_ASSIGNMENTS:
        raise TooComplex
    for match in reversed(loops):
        name, values = match.group(1), match.group(2).strip()
        use = re.compile(r"\$\{?" + re.escape(name) + r"(?!\w)\}?")
        rest = command[match.end():]
        bound.check(len(command) + len(use.findall(rest)) * len(values))
        command = command[:match.end()] + use.sub(lambda m, text=values: text, rest)
    return command


def _quoted_end(text, i, escape):
    """The end of the double-quoted text that opens at i, past any $(...) or bash
    backtick inside it; the end of text when it is not closed."""
    return _closing_end(text, i, escape, quoted=True)


def _bracket_end(text, i, escape):
    """The end of the bracketed text that opens at i ("(" or "{"), with quotes and
    nested brackets inside it; the end of text when it is not closed."""
    return _closing_end(text, i, escape, quoted=False)


def _closing_end(text, i, escape, quoted):
    """Where double-quoted text (quoted) or bracketed text opening at i ends, with
    any nesting of the two inside it. One pass with a stack of its own, not
    recursion: thousands of nested "$( once raised RecursionError, which let the
    command through (#221 round 1)."""
    # Each frame is None for double-quoted text, or a bracket's depth.
    stack = [None] if quoted else [0]
    j, end = (i + 1 if quoted else i), len(text)
    while j < end:
        char = text[j]
        if char == escape:
            j += 2
            continue
        if stack[-1] is None:                     # inside double quotes
            if char == '"':
                stack.pop()
                j += 1
                if not stack:
                    return j
                continue
            if text.startswith("$(", j):
                stack.append(0)                   # the bracket frame reads its "("
                j += 1
                continue
            if char == "`" and escape == "\\":
                close = text.find("`", j + 1)
                j = end if close < 0 else close + 1
                continue
            j += 1
            continue
        if char == "'":                           # inside brackets
            close = text.find("'", j + 1)
            j = end if close < 0 else close + 1
            continue
        if char == '"':
            stack.append(None)
            j += 1
            continue
        if char in "({":
            stack[-1] += 1
        elif char in ")}":
            stack[-1] -= 1
            if stack[-1] == 0:
                stack.pop()
                j += 1
                if not stack:
                    return j
                continue
        j += 1
    return end


def _value_end(command, start, shell, matched_end=None, word=False):
    """Where the value assigned at start ends (R13b round 4 review: a bash value
    stopped at the first space or inner quote, so FOLDER=$(cygpath "$LOCALAPPDATA")
    kept only "$(cygpath"). Bash: the whole shell word, with its quotes, $(...),
    ${...} and backticks. PowerShell: a quoted value or here-string as matched, an
    unquoted one to the end of the statement, past brackets and quotes. A $(...)
    or quote left open runs to the end of the command, which fails closed. With word,
    a PowerShell value ends at a space too: one argument, (...) and all."""
    escape = "\\" if shell == "Bash" else "`"
    if shell == "PowerShell" and not word and command[start:start + 1] in ("'", '"', "@"):
        return matched_end
    j = start
    while j < len(command):
        char = command[j]
        if shell == "Bash" and (char in " \t\n;&|<>()"):
            break
        if shell == "PowerShell" and (char in ";\n|&)}" or (word and char in " \t")):
            break
        if char == escape:
            j += 2
        elif char == "'":
            close = command.find("'", j + 1)
            j = len(command) if close < 0 else close + 1
        elif char == '"':
            j = _quoted_end(command, j, escape)
        elif command.startswith("$(", j) or command.startswith("${", j):
            j = _bracket_end(command, j + 1, escape)
        elif char in "({" and shell == "PowerShell":
            j = _bracket_end(command, j, escape)
        elif char == "`" and shell == "Bash":
            close = command.find("`", j + 1)
            j = len(command) if close < 0 else close + 1
        else:
            j += 1
    return min(j, len(command))


def _segments(command, shell):
    """The command's segments with message text removed, each as (words, text,
    dialect): the shell that reads the program's options, "cmd" for cmd /c."""
    for segment in SEGMENTS.split(command):
        # A segment of assignments alone (x=1, or d=json.load(open(r'...')) inside
        # python -c code split at ;) is still checked, as text that runs no
        # program (R13b round 5 review).
        words = _lead_words(segment) or [w.strip("\"'").lower() for w in segment.split()][:1]
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


def _lead_words(segment):
    """The segment's words from its program on, lower-cased and unquoted: assignments
    and wrappers (with their options, counts and durations) skipped."""
    words = segment.split()
    while words and (words[0].lower() in WRAPPERS or "=" in words[0]):
        wrapper = words[0].lower() in WRAPPERS
        words = words[1:]
        while wrapper and words and re.fullmatch(r"-\S*|\d+(?:\.\d+)?[smhd]?|\{\}", words[0]):
            words = words[1:]
    return [w.strip("\"'").lower() for w in words]


def _segment_starts(command):
    """Where each segment of command starts: after each |, ;, & or line break outside
    quotes. One pass, so a command with many message options stays linear (#221)."""
    starts, quote = [0], None
    for k, char in enumerate(command):
        if quote:
            if char == quote and command[k - 1:k] not in ("\\", "`"):
                quote = None
        elif char in "'\"":
            quote = char
        elif char in "|;&\n":
            starts.append(k + 1)
    return starts


# How much of a segment is read for its program's words: enough for wrappers, the
# program and git's subcommand. Past it the program is unknown, and its short
# message options are then checked as paths (#221: linear time).
LEAD_LENGTH = 2000


def _words_at(command, position, starts):
    """The words of the segment that holds position, from its program on: the text
    back to the last |, ;, & or line break outside quotes (starts, from
    _segment_starts), read past brackets, wrappers and cmd /c."""
    start = starts[bisect.bisect_right(starts, position) - 1]
    lead = command[start:min(position, start + LEAD_LENGTH)]
    return _unwrap(_lead_words(lead.lstrip(" \t({$\"'")))


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
    slash = program in SLASH_PROGRAMS                     # cmd's /s-style options
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


def _path_words(tokens):
    """The tokens that may name a path: those that are not options, and the values
    joined to options with a colon (-Path:X, -LiteralPath:X)."""
    words = []
    for token in tokens:
        joined = COLON_VALUE.match(token)
        if joined:
            words.append(joined.group(1))
        elif not token.startswith("-"):
            words.append(token)
    return words


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
    # Copies and archives (#221): cp, rsync and zip with -r or -a (short options,
    # combined or not), Copy-Item -Recurse, robocopy /s, /e or /mir, xcopy /s or /e.
    if program in ARCHIVERS:
        return True
    if program in ("cp", "rsync", "zip"):
        return any(w in ("--recursive", "--archive") or re.fullmatch(r"-[a-z0-9]*[ra][a-z0-9]*", w)
                   for w in flags)
    if program in COPIERS:
        return any(w.startswith("-rec") or w in ("-r", "/s", "/e", "/mir") for w in flags)
    # Other programs that walk a folder's tree (R13b round 4 review): du lists every
    # folder below (and with -a every file), forfiles /s and where /r search below.
    if program == "du":
        return True
    if program == "forfiles":
        return "/s" in flags or "-s" in flags
    if program == "where":
        return "/r" in flags
    return False


class _Folder:
    """The folder a command runs in, followed through cd, pushd and popd."""

    def __init__(self, cwd):
        self.here = ntpath.normpath(cwd) if cwd else ""
        self.previous = self.here
        self.stack = []

    def change(self, verb, rest, shell="Bash"):
        verb = verb.lower()
        if verb in ("popd", "pop-location"):
            # An empty stack is an error, and the folder stays (R13b round 4 review).
            if self.stack:
                self.here = self.stack.pop()
            return
        word = _cd_word(rest, shell)
        if word is None:
            # bash's (and PowerShell 7's) cd goes home; 5.1's stays. Taken as home,
            # which a search from there must not reach (R13b round 3 review).
            self.previous, self.here = self.here, ntpath.expanduser("~")
        elif word == "-":
            self.previous, self.here = self.here, self.previous
        else:
            if verb in ("pushd", "push-location"):
                self.stack.append(self.here)
            self.previous, self.here = self.here, _cd_folder(self.here, word)


# A command whose output is the folder it is given (cygpath, realpath, Resolve-Path)
# or the folder above it (dirname, Split-Path), in a cd's argument (R13b round 5).
FOLDER_OF = re.compile(
    r"(?:\$\(|`|\()\s*(?:(?:cygpath|realpath|readlink|resolve-path|convert-path|get-item)"
    r"|(dirname|split-path))(?:\s+-[\w-]+)*\s+(\"[^\"`]*\"|'[^']*'|[^\s)`'\"]+)\s*[)`]",
    re.IGNORECASE)
# What the shell runs or substitutes, left in a cd's argument after that.
UNRESOLVED = re.compile(r"[$`%]|^\(")
# How many levels of FOLDER_OF nesting are written out ($(dirname "$(dirname X)")):
# deeper ones stay as written, so a long chain cannot make the work quadratic.
FOLDER_OF_DEPTH = 8


def _folder_of(text):
    """text with each cygpath X, realpath X or Resolve-Path X written as X, and
    dirname X or Split-Path X as X\\.., innermost first."""
    for _ in range(FOLDER_OF_DEPTH):
        previous = text
        text = FOLDER_OF.sub(lambda m: m.group(2).strip("\"'") + ("\\.." if m.group(1) else ""),
                             text)
        if text == previous:
            break
    return text


def _cd_word(rest, shell):
    """The argument of a cd, pushd or Set-Location as one whole shell word, with
    its quotes, $(...), backticks or PowerShell (...) (R13b round 5 review: a
    folder from cygpath was cut at its inner quote); past options (cd -P, cd /d,
    Set-Location -Path), or the value joined to one (-Path:X). "-" for the previous
    folder, None when it names none."""
    i = 0
    while True:
        while i < len(rest) and rest[i] in " \t":
            i += 1
        end = _value_end(rest, i, shell, word=True) if i < len(rest) else i
        if end <= i:
            return None
        token = rest[i:end]
        joined = COLON_VALUE.match(token)
        if joined:
            return joined.group(1)
        if token == "-" or not (token.startswith("-") or token.lower() == "/d"):
            return token
        i = end


def _cd_folder(here, word):
    """The folder a cd's argument names, from here. cygpath X and the like are X,
    dirname X is X\\.., and the variables are written out. When the shell still
    has to run or substitute part of it, the folder is unknown (""), unless that
    part names local AppData, the home folder or a folder at or above them: then
    it is that folder, which a search from there must not reach."""
    expanded = _expand(_folder_of(word).replace('"', "").replace("'", ""))
    if not UNRESOLVED.search(expanded):
        return _resolve(here, expanded)
    for part in re.split(r"[\s$`()]+", expanded):
        path = _resolve(here, part) if part else ""
        if path and (_above_data(_canonical(path)) or _in_data(_canonical(path))):
            return path
    return ""


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


def _walk_starts_above_data(code, here):
    """True when a quoted string in interpreter code names local AppData or a folder
    above it (C:\\Users, the profile, '.' run from there), up to its first wildcard
    part: a walk from it reads every folder below (R13b round 4)."""
    for match in CODE_STRING.finditer(code):
        parts = re.split(r"[\\/]", match.group(2))
        cut = next((i for i, p in enumerate(parts) if re.search(r"[*?\[]", p)), len(parts))
        # A pattern that starts with a wildcard ('**/*.db') walks from here.
        literal = "\\".join(parts[:cut]) or "."
        if literal.endswith(":"):
            literal += "\\"
        full = _canonical(_resolve(here, literal))
        if _above_data(full) or _in_data(full):
            return True
    return False


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
    slash = program in SLASH_PROGRAMS
    paths = [_resolve(here, token) for token in _path_words(tokens)
             if not (slash and re.fullmatch(r"/\S", token))]
    return bool(BARE_LOCAL.search(expanded)
                or any(_above_data(_canonical(path)) for path in paths)
                or _searches_above_data(expanded, program, sub, here, dialect))


def _holds_extracted(folder):
    """True when the folder is a source checkout (it holds AGENTS.md) with an
    extracted/ folder in it. Metadata only; nothing is read."""
    full = _canonical(folder) if folder else ""
    return bool(full) and os.path.isdir(ntpath.join(full, "extracted")) and \
        os.path.isfile(ntpath.join(full, "AGENTS.md"))


def _searches_extracted(words, text, here, dialect, dropped=False):
    """True when a recursive search or listing starts from a checkout that holds an
    extracted/ folder, which it would read (R13b round 5 review). rg and ag skip
    what .gitignore names, unless told not to (-u, --no-ignore)."""
    program = _program(words)
    sub = _git_sub(words) if program == "git" else ""
    if not _recursive(program, words, text, dialect):
        return False
    if program in ("rg", "ag") and not any(
            w.startswith(("--no-ignore", "--unrestricted", "--skip-vcs-ignores"))
            or re.fullmatch(r"-[a-z]*u[a-z]*|-U", w) for w in words[1:]):
        return False
    folders = []
    searched = text if dropped else _without_pattern(words, text)
    for arg in _arguments(_expand(searched), program, sub):
        clean = arg.strip("\"'")
        head, sep, leaf = clean.replace("/", "\\").rpartition("\\")
        # A wildcard last part (grep -r x *) searches the folder it is in.
        if re.search(r"[*?\[]", leaf):
            clean = (head or "\\") if sep else "."
        folders.append(_resolve(here, clean))
    # find with no folder named (find -name x) starts from here.
    if not folders or (program == "find" and len(words) > 1 and words[1].startswith("-")):
        folders.append(here)
    return any(_holds_extracted(folder) for folder in folders)


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


def _check_shell(command, cwd="", shell="Bash", grown_limit=None):
    folder = _Folder(cwd)
    if _reaches_data(folder.here):
        return f"A command run in the live Unminute workspace is blocked. {ADVICE}"
    if shell == "PowerShell":
        # "$env:LOCALAPPDATA\..\" ends at its quote in PowerShell. Doubled, the
        # backslashes read that way below too (R13b round 5 review).
        command = PS_BACKSLASHES.sub(lambda m: m.group(0) * 2, command)
    command = _drop_written(_substitute(command, shell, grown_limit), shell)
    # main's check first, on the text with only written text dropped (#221).
    plain = _plain_shell(command)
    if plain:
        return plain
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
                for w, t, d in [(words, text, dialect),
                                *_nested(_without_pattern(words, text), shell)]):
            return searching
    # Code run by an interpreter that walks local AppData, or the home folder, named
    # whole (R13b round 4 review: os.walk, rglob, readdirSync recursive).
    code = " ; ".join(_without_pattern(words, text) for words, text, _ in
                      _segments(command, shell))
    code_expanded = _expand(code)
    # A cd to the home folder is followed below, not taken as code that names it
    # (cd ~ && cd repo && python ... os.walk('.'), R13b round 5 review).
    named = _expand(" ; ".join(_without_pattern(words, text) for words, text, _ in
                               _segments(command, shell) if not CHANGE_FOLDER.search(text)))
    whole = BARE_LOCAL.search(code_expanded) or BARE_HOME.search(named) or HOME_CODE.search(code)
    walks = (INTERPRETER.search(code) and WALKER.search(code)) or ALL_DIRECTORIES.search(code)
    if walks and (whole or _walk_starts_above_data(code_expanded, folder.here)):
        return searching
    # The folder each interpreter runs in, for a walk that starts from a relative
    # path (the folder in force then, not every folder visited: R13b round 5).
    interpreted = []
    for words, text, dialect in _segments(command, shell):
        if INTERPRETER.search(text) and folder.here not in interpreted:
            interpreted.append(folder.here)
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
        paths = [_resolve(here, token) for token in _path_words(tokens)]
        if _names_workspace(expanded) or any(_reaches_data(path) for path in paths):
            return blocked
        # A search or listing of local AppData, or a folder above it, reads the
        # data; so does one nested inside this segment, outside the search's own
        # pattern (R13b round 4 review).
        if _searches(words, segment, folder.here, dialect) or any(
                _searches(w, t, folder.here, d) for w, t, d in _nested(text, shell)):
            return searching
        # A search from a checkout that holds extracted/ reads it (R13b round 5).
        # A nested search's text has its pattern dropped already.
        if _searches_extracted(words, segment, folder.here, dialect) or any(
                _searches_extracted(w, t, folder.here, d, dropped=True)
                for w, t, d in _nested(text, shell)):
            return f"Searching a checkout that holds an extracted/ folder is blocked. {ADVICE}"
        change = CHANGE_FOLDER.search(text) or BLOCK_CHANGE.search(text)
        if change:
            folder.change(change.group(1), change.group(2), shell)
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
    # Interpreter code walking a relative path, run after a cd to a folder above
    # local AppData (cd /c/Users && python -c "...os.walk('.')", R13b round 4).
    if walks and any(_walk_starts_above_data(code_expanded, here) for here in interpreted):
        return searching
    return None


def decide(payload, grown_limit=None):
    """The reason to block this tool call, or None to allow it. With grown_limit,
    NeedsChild when a shell command's variables grow it past that many characters
    (main() then checks it in a child process under the time limit)."""
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or ""

    if tool in FILE_TOOLS:
        # First main's checks, on each path as given.
        for field in FILE_FIELDS:
            value = tool_input.get(field)
            if tool == "Grep" and field == "pattern" or not isinstance(value, str) or not value:
                continue
            if _plain_names_workspace(value):
                return f"{tool} on the live Unminute workspace is blocked. {ADVICE}"
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
            for reading in _readings(command):
                try:
                    reason = _check_shell(reading, cwd, shell, grown_limit)
                except TooComplex:
                    return COMPLEX
                if reason:
                    return reason
    return None


def _readings(command):
    """The command as written and, when it cuts or replaces part of a folder
    variable's value (${TEMP%\\*}, ${APPDATA%\\Roaming}, ${TEMP/x/y}, ${TEMP:0:30}),
    also with each such value read as the folder above the variable's, as dirname
    gives (#221 round 2: suffix removal climbs from Temp to local AppData). Both
    are checked, as the value may be the folder or one above it."""
    yield command
    if PARENT_OPERATOR.search(command):
        yield PARENT_OPERATOR.sub(lambda m: "${" + m.group(1) + "}/..", command)


# A long call is checked in a child process under a time limit (#221 round 1):
# Claude Code lets a call through when its hook runs past the hook's own time
# limit, and a text heuristic cannot promise linear time for every shape of a long
# command. A short call is checked here, in one process, unless its variables grow
# it past the same length (NeedsChild, #221 round 2): then it goes to the child too.
CHILD_OVER = 20_000                 # characters of a shell command
TIME_LIMIT = 20.0                   # seconds
# The variable may lower the limit (the tests do), never raise it.
TIME_LIMIT_VARIABLE = "PRIVATE_DATA_GUARD_TIME_LIMIT"
CHILD_FLAG = "--child"
TOO_SLOW = ("This command took too long for the private data guard to check, so it is "
            f"blocked. {SIMPLER}")


def _verdict(payload, grown_limit=None):
    """(exit code, reason) for the hook input. Any error while checking blocks the
    call: exit 1 would only warn, and let through a command the guard never finished
    reading (#221 round 1: a RecursionError did). The reason names the error's type
    only, no content. With grown_limit, NeedsChild when the command's variables grow
    it past that many characters."""
    try:
        reason = decide(payload, grown_limit)
    except NeedsChild:
        raise
    except Exception as error:                   # RecursionError and MemoryError too
        reason = (f"private_data_guard could not check this call ({type(error).__name__}), "
                  f"so it is blocked. {SIMPLER}")
    return (2, reason) if reason else (0, "")


def _time_limit():
    try:
        return max(0.0, min(TIME_LIMIT, float(os.environ.get(TIME_LIMIT_VARIABLE, TIME_LIMIT))))
    except ValueError:
        return TIME_LIMIT


def _checked_in_child(data):
    """(exit code, reason) from a child process that checks the hook input (the
    bytes as received), blocked when it runs past the time limit, fails or cannot
    be started."""
    import subprocess                            # only for long calls
    try:
        child = subprocess.run([sys.executable, "-I", os.path.abspath(__file__), CHILD_FLAG],
                               input=data, capture_output=True, timeout=_time_limit(),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return 2, TOO_SLOW
    except OSError:
        # #221 round 2: fail closed, as for any other error; a long call checked
        # here instead would have no time limit.
        return 2, (f"private_data_guard could not start its time-limited check, so this "
                   f"call is blocked. {SIMPLER}")
    if child.returncode == 0:
        return 0, ""
    reason = child.stderr.decode("utf-8", "replace").strip()
    if child.returncode != 2 or not reason:
        reason = (f"private_data_guard could not check this call (exit {child.returncode}), "
                  f"so it is blocked. {SIMPLER}")
    return 2, reason


def _decoded(data):
    """The hook input as text. Claude Code sends UTF-8; anything it cannot decode
    becomes U+FFFD, so the text is always valid and can be encoded again (#221
    round 2: reading stdin as text used the ANSI code page, and a Maori macron
    left a lone surrogate that failed the child's encode, which only warned)."""
    return data.decode("utf-8-sig", "replace")


def main():
    child = sys.argv[1:] == [CHILD_FLAG]
    # Unreadable input means the hook is wired wrongly. The guard fails closed
    # (owner, 25 Oct 2026): block and say so, since Claude Code lets a call
    # through on exit 1.
    try:
        data = sys.stdin.buffer.read()
        payload = json.loads(_decoded(data))
    except (ValueError, OSError):
        payload = None
    if not isinstance(payload, dict):
        print("private_data_guard: no readable hook input, so this call is blocked; "
              "check the hook command in .claude/settings.json", file=sys.stderr)
        return 2
    try:
        code, reason = _decide_bounded(payload, data, child)
    except Exception as error:                   # anything after parsing blocks
        code, reason = 2, (f"private_data_guard could not check this call "
                           f"({type(error).__name__}), so it is blocked. {SIMPLER}")
    if reason and child:
        sys.stderr.buffer.write(reason.encode("utf-8", "replace"))
    elif reason:
        print(reason, file=sys.stderr)
    return code


def _decide_bounded(payload, data, child):
    """(exit code, reason) for the parsed hook input, with its time bounded: a long
    shell command, or one that its variables grow past CHILD_OVER characters, is
    checked in a child process under the time limit (#221 rounds 1 and 2)."""
    # Only a shell command's checks can take long; a file tool's path is short work
    # however long the text it writes.
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    shell = payload.get("tool_name") in SHELL_TOOLS and isinstance(command, str)
    if child or not shell:
        return _verdict(payload)
    if len(command) > CHILD_OVER:
        return _checked_in_child(data)
    try:
        return _verdict(payload, grown_limit=CHILD_OVER)
    except NeedsChild:
        return _checked_in_child(data)


if __name__ == "__main__":
    sys.exit(main())
