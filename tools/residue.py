# tools/residue.py
# What the packaged checks (tools/packaged_check.py, tools/packaged_crash_check.py)
# look for in their throwaway profile, shared so both mean the same (#111 C7):
#
#   - find(): which files hold any of a set of byte patterns (a marker written
#     into the transcript, windows of the decoded audio, the synthetic
#     recording). It names files and patterns, never what it found.
#   - control(): the positive control. Since #111 phase C seals the database
#     and encrypts the audio, "no pattern found" also follows from a scanner
#     that finds nothing, so the same finder must find every pattern planted
#     in a control file outside the profile, or the report fails.
#   - unexpected(): files left after final cleanup that are not on the list of
#     the workspace's own files, built from the app's constants (names()), so
#     a rename in the app moves the list with it.
#   - listing() and new_or_changed(): what a run added or changed in a
#     folder outside the profile (names, sizes and times only, never
#     opened), and explain(): whether each such file is one the run is
#     known to leave there (EXPLAINED), with why it holds no content.
#   - redact() and redact_json(): an error tail, or a child's decoded
#     result, copied into a report without the throwaway base, the home
#     folder or the Windows user name (P3: the reports are published with a
#     release). guarded(): the last step before a report is written; a
#     report that would still carry the user name anywhere fails instead.
#     write_guarded(): guarded() and the write, for the performance, UI and
#     accessibility reports (#419); evidence.stamp_file() guards the stamped
#     report of every kind again.
#   - dumps(): crash dumps under the throwaway base, counted by name and
#     size (os.walk and stat only): no dump is ever opened, copied or
#     printed. system_dumps() counts the signed-in user's own dump folders,
#     only when a person asks (--report-system-dumps): a dump of the
#     installed app could hold real content (AGENTS.md P2).
#
# Standard library only: the checks run it in the package's own runtime.

import json
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, quote_plus

CHUNK = 8 << 20                       # bytes read at a time while scanning
CONTROL_FOLDER = "residue-control"    # in the base folder, outside the profile
DUMP_SUFFIXES = (".dmp", ".mdmp", ".hdmp")
CRASH_SUFFIX = ".crash"               # the app's own faulthandler file (workspace/diag.py)
# The runtime's executables and the window's, as Windows Error Reporting
# names their dumps (python.exe.1234.dmp) and report folders
# (AppCrash_python.exe_...).
DUMP_PROGRAMS = ("python.exe", "pythonw.exe", "msedgewebview2.exe")


# -- finding patterns -----------------------------------------------------------------

def _rel(path, base):
    return Path(os.path.relpath(path, base)).as_posix()


def _files(roots, unreadable, base):
    """Every file under roots. A folder that cannot be listed (access
    denied, a path too long) is added to unreadable, relative to base with a
    trailing "/", never skipped in silence: what is in it cannot be shown
    free of anything (review of #335). A root that does not exist holds
    nothing."""
    def refused(error):
        unreadable.append(_rel(error.filename or "?", base) + "/")
    for root in roots:
        root = Path(root)
        if root.is_file():
            yield root
            continue
        if not os.path.lexists(root):
            continue
        for folder, _dirs, names in os.walk(root, onerror=refused):
            for name in sorted(names):
                yield Path(folder) / name


def find(roots, patterns, base):
    """Which files under roots hold which patterns ({label: bytes}).
    Returns {"hits": {label: [file]}, "unreadable": [file], "scanned": [file]},
    each file relative to base (POSIX form). A file that cannot be read, or a
    folder that cannot be listed (with a trailing "/"), cannot be shown free
    of the patterns: the caller fails on it. Reads in CHUNK
    pieces that overlap by the longest pattern, so a large file costs bounded
    memory and a pattern across a boundary is still found."""
    patterns = {label: bytes(p) for label, p in patterns.items() if p}
    overlap = max((len(p) for p in patterns.values()), default=1) - 1
    hits, unreadable, scanned = {label: [] for label in patterns}, [], []
    for path in _files(roots, unreadable, base):
        name = _rel(path, base)
        found = set()
        try:
            with open(path, "rb") as f:
                tail = b""
                while True:
                    piece = f.read(CHUNK)
                    if not piece:
                        break
                    window = tail + piece
                    for label, p in patterns.items():
                        if label not in found and p in window:
                            found.add(label)
                    tail = window[-overlap:] if overlap else b""
        except OSError:
            unreadable.append(name)
            continue
        scanned.append(name)
        for label in found:
            hits[label].append(name)
    return {"hits": {k: v for k, v in hits.items() if v}, "unreadable": unreadable,
            "scanned": scanned}


def control(base, patterns, finder=find):
    """The positive control: the labels of patterns the finder missed in a
    control file (empty when it found them all). The file is written in base,
    outside the profile, with random filler and each pattern at a known
    offset (one straddles a CHUNK boundary), scanned with the same finder,
    then removed."""
    folder = Path(base) / CONTROL_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "control.bin"
    try:
        with open(path, "wb") as f:
            for n, p in enumerate(patterns.values()):
                if n == 0:                    # across the first read's boundary
                    f.write(secrets.token_bytes(max(0, CHUNK - len(p) // 2)))
                else:
                    f.write(secrets.token_bytes(4096 + 97 * n))
                f.write(bytes(p))
            f.write(secrets.token_bytes(4096))
        hits = finder([folder], patterns, base)["hits"]
    finally:
        try:
            path.unlink()
            folder.rmdir()
        except OSError:
            pass
    return [label for label in patterns if not hits.get(label)]


def text_patterns(marker):
    """A text marker as the app could store it: UTF-8 and UTF-16 (a WebView2
    or Windows file keeps text as UTF-16)."""
    return {"marker": marker.encode("utf-8"), "marker_utf16": marker.encode("utf-16-le")}


def split_windows(raw, size):
    """Equal windows of size bytes, labelled window_1, window_2, ..."""
    return {f"window_{i // size + 1}": raw[i:i + size]
            for i in range(0, len(raw) - size + 1, size)}


# -- files allowed after final cleanup --------------------------------------------------

def names():
    """The workspace's own file names and patterns, from the app's constants
    (workspace/diag.py and the others named here; import the app first:
    evidence.add_app). Folders are relative to the
    workspace folder (%LOCALAPPDATA%\\Unminute\\workspace). They include
    what only the real start writes (the workspace lock, the launcher's
    record) and the download queue with its temporary file: today's checks
    write none of them before the scan after final cleanup, and a later step
    that did must not fail "only the workspace's own files" (review of
    #335). The window's WebView2 profile (workspace\\webview) is not on
    the list: no check opens the window with content yet (T7)."""
    from workspace import asr_engines, audit, diag, handoff, instance, power, setup, store
    queue = Path(setup.QUEUE_FILE)
    return {
        "db": diag.DB_FILE,
        "files": (diag.DB_FILE, diag.DB_FILE + "-wal", diag.DB_FILE + "-shm",
                  diag.SETTINGS_FILE, diag.SETTINGS_UNREADABLE_FILE, handoff.TEMPLATES_FILE,
                  power.RESTORE_FILE, store.compaction_marker(diag.DB_FILE).name,
                  instance.NAME, queue.name, queue.with_suffix(".tmp").name),
        "logs": diag.LOGS_FOLDER,
        "log_files": (diag.INTEGRITY_FILE, diag.KEYS_FILE, diag.LAUNCH_FILE),
        "verified_marker": asr_engines.VERIFIED_MARKER,
        "log_stem": diag.LOG_NAME.pattern,
        "log_suffixes": (diag.LOG_SUFFIX, diag.RUNNING_SUFFIX),
        "crash_suffix": diag.CRASH_SUFFIX,
        "audit": diag.AUDIT_FOLDER,
        "audit_segment": audit.SEGMENT.pattern,
    }


def allowed(rel, n):
    """Whether a file left in the workspace folder after final cleanup is one
    of its own (rel: relative to the workspace folder, POSIX form). A crash
    file is not judged here: dumps() counts it."""
    parts = rel.split("/")
    if len(parts) == 1:
        return parts[0] in n["files"]
    if len(parts) != 2:
        return False
    folder, name = parts
    if folder == n["logs"]:
        if name in n["log_files"]:
            return True
        for suffix in (*n["log_suffixes"], n["crash_suffix"]):
            if name.endswith(suffix) and re.fullmatch(n["log_stem"], name[:-len(suffix)]):
                return True
        return False
    if folder == n["audit"]:
        return re.fullmatch(n["audit_segment"], name) is not None
    return False


def unexpected(profile, workspace, n):
    """Files under profile (the throwaway Unminute folder) that are not the
    workspace's own: anything outside the workspace folder, and anything in it
    that allowed() does not name, and any folder that cannot be listed
    (with a trailing "/"). POSIX paths relative to profile."""
    out = []
    for path in _files([profile], out, profile):
        rel = _rel(path, profile)
        inside = _rel(path, workspace)
        if inside.startswith("../") or not allowed(inside, n):
            out.append(rel)
    return out


# -- outside the profile: what a run added or changed ---------------------------------------

# New or changed files a run may leave outside the profile, by place and a
# pattern of the name (relative to that place, POSIX form), each with why it
# holds no meeting content. Anything else a run leaves there fails the
# check. "{verified_marker}" stands for asr_engines.VERIFIED_MARKER (names()).
# The redirected places (the child's TEMP, APPDATA and user folder) explain
# nothing: no file is expected there. The model-cache patterns follow
# workspace/asr_engines.py (VERIFIED_MARKER in a model's folder, and
# _marker_for()'s <family>/.verified/<folder>-<key>.json for a Hugging Face
# copy); tests/test_residue.py ties them to the app's own paths.
#
# Adding an entry widens what a run may leave (AGENTS.md E2). The owner's
# decision, 26 Oct 2026 (#335): the agent running the release evidence may
# add an entry for a content-free library cache that run found, with why it
# holds no content, provided each entry is reviewed independently and named
# in the evidence report (docs/RELEASING.md, "Release evidence").
EXPLAINED = {
    "LocalLow": (
        (r"Microsoft/CryptnetUrlCache/.+",
         "Windows' cache of downloaded certificate data, shared by every program"),),
    "model_cache": (
        (r"(.+/)?{verified_marker}",
         "a model's verification record: its revision, file sizes and times (I1)"),
        (r"[^/]+/\.verified/[^/]+\.json",
         "a model's verification record, kept beside the models (I1)"),),
}


def listing(root, unreadable, base=None):
    """{name: (size, modified ns)} of every file under root, the name
    relative to root (POSIX form), from stat only: nothing is ever opened,
    so a folder of the signed-in user's own files can be compared before
    and after a run (P2). A folder that cannot be listed, or a file that
    cannot be read by stat, goes into unreadable (relative to base, or root)."""
    out = {}
    root = Path(root)
    for path in _files([root], unreadable, base or root):
        try:
            st = os.stat(path)
        except OSError:
            unreadable.append(_rel(path, base or root))
            continue
        out[_rel(path, root)] = (st.st_size, st.st_mtime_ns)
    return out


def new_or_changed(before, after):
    """Names in the after listing that are new, or whose size or time changed."""
    return sorted(name for name, stat in after.items() if before.get(name) != stat)


def real_places(cache, environ=None):
    """The real folders outside the throwaway base that a run is compared in,
    before and after, by name only: {place: folder}. The model cache is used
    as given; LocalLow has no variable of its own and is not redirected (it
    sits in the real profile, beside %LOCALAPPDATA%). The children's APPDATA
    and USERPROFILE are redirected into the base instead, and scanned."""
    environ = os.environ if environ is None else environ
    out = {"model_cache": Path(cache)}
    profile = environ.get("USERPROFILE")
    if profile:
        out["LocalLow"] = Path(profile) / "AppData" / "LocalLow"
    return out


def snapshot(places):
    """{place: listing()} of each place, and the folders that could not be
    listed, as "place/name/"."""
    out, unreadable = {}, []
    for place, root in places.items():
        refused = []
        out[place] = listing(root, refused)
        unreadable += [f"{place}/{name}" for name in refused]
    return out, unreadable


def compare(before, places, n=None, earlier=None):
    """What a run added or changed in places since the before snapshot (a
    (listing, unreadable) pair from snapshot()), by name only, with the
    names nothing explains and the folders that could not be listed then or
    now. earlier: a result of this function to add to (a file the run made
    and removed again is still named)."""
    then, refused = before
    now, unreadable = snapshot(places)
    new = {place: new_or_changed(then.get(place, {}), now[place]) for place in places}
    for place, names in ((earlier or {}).get("new") or {}).items():
        new[place] = sorted(set(new.get(place, [])) | set(names))
    unreadable = sorted(set(refused) | set(unreadable)
                        | set((earlier or {}).get("unreadable") or []))
    return {"new": new, "unexplained": unexplained(new, n), "explained": explained(new, n),
            "unreadable": unreadable}


def left_in(places, base):
    """Every file in the redirected places (all of them made by the run),
    by name: {"files": {place: [name]}, "unexplained": [...], "explained":
    {...} (explained()), "unreadable": [...]}, the unreadable folders
    relative to base."""
    files, unreadable = {}, []
    for place, root in places.items():
        files[place] = sorted(_rel(p, root) for p in _files([root], unreadable, base))
    return {"files": files, "unexplained": unexplained(files), "explained": explained(files),
            "unreadable": unreadable}


def _entry(place, name, n=None):
    """The EXPLAINED entry (pattern, why) that explains a file, or None."""
    marker = re.escape((n or {}).get("verified_marker", ".verified.json"))
    for pattern, why in EXPLAINED.get(place, ()):
        if re.fullmatch(pattern.replace("{verified_marker}", marker), name):
            return pattern, why
    return None


def explain(place, name, n=None):
    """Why a new or changed file at place (an EXPLAINED key) holds no
    content, or None when nothing explains it (the check fails then)."""
    entry = _entry(place, name, n)
    return entry[1] if entry else None


def unexplained(new, n=None):
    """{place: [name]} -> the names nothing explains, as "place/name"."""
    return sorted(f"{place}/{name}" for place, found in new.items() for name in found
                  if explain(place, name, n) is None)


def explained(new, n=None):
    """{place: [name]} -> how many of the names each EXPLAINED entry
    explains, as {"place: pattern": count}, so a report names every entry
    it relied on (the owner's decision, 26 Oct 2026, #335)."""
    out = {}
    for place, found in new.items():
        for name in found:
            entry = _entry(place, name, n)
            if entry:
                key = f"{place}: {entry[0]}"
                out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


# -- error text copied into a report ---------------------------------------------------------

def _short(path):
    """The Windows short (8.3) form of an existing path, or the path."""
    if os.name != "nt":
        return str(path)
    import ctypes
    try:
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf)):
            return buf.value
    except (OSError, AttributeError, ValueError):
        pass
    return str(path)


def _escaped(form):
    """form as text can carry it: as written, as JSON writes it (backslashes
    doubled, anything not ASCII as \\uXXXX) and as Python writes it to a pipe
    whose encoding cannot hold it (backslashreplace: \\xNN, \\uXXXX). A
    macronised name such as Tāne reaches a report in these forms (review
    of #335). An escape carries no case, so the escaped forms are made from
    the name as written, in lower case and in capitals."""
    out = {form}
    for f in (form, form.lower(), form.upper()):
        out |= {json.dumps(f)[1:-1], f.encode("ascii", "backslashreplace").decode("ascii")}
    return out


# The user name as a word: only a letter continues it, so "_", "-", ".", a
# digit, a space and either slash all end it (torchinductor_<name>,
# cache-<name>.bin), and a short name never matches inside a longer word
# ("ope" in "opens"). Review of 387f8d3 (#335).
_NAME_BEFORE = r"(?<![^\W\d_])"
_NAME_AFTER = r"(?![^\W\d_])"
PLACEHOLDERS = ("<base>", "<home>", "<user>")
CARRIES_USER_NAME = "report would carry the Windows user name"


def _spellings(name):
    """name as text can spell it before _escaped(): composed (NFC) and
    decomposed (NFD: a macron as a letter and a combining mark, as a file
    name from another system can arrive), and each of those URL-encoded (a
    file:// address; %20 or + for a space). Review of 78f17ee (#419)."""
    out = set()
    for form in {name, unicodedata.normalize("NFC", name), unicodedata.normalize("NFD", name)}:
        out |= {form, quote(form, safe=""), quote_plus(form, safe="")}
    return out


def _name_regex(names):
    """One pattern for every _escaped() form of each of the names'
    _spellings(), as a word (_NAME_BEFORE, _NAME_AFTER), case ignored; None
    when there is no name."""
    forms = sorted({f for name in names for spelt in _spellings(name) for f in _escaped(spelt)},
                   key=len, reverse=True)
    if not forms:
        return None
    return re.compile(_NAME_BEFORE + "(?:" + "|".join(map(re.escape, forms)) + ")"
                      + _NAME_AFTER, re.IGNORECASE)


def user_names(environ=None):
    """The names a report must not carry: the Windows user name, the profile
    folder's own name when it differs, and that folder's short (8.3) name,
    such as PATEXA~1, which a path can carry in place of the long one
    (review of 78f17ee, #419); each of 3 characters or more, as redact()
    replaces."""
    environ = os.environ if environ is None else environ
    profile = (environ.get("USERPROFILE") or "").rstrip("\\/")
    names = {environ.get("USERNAME") or ""}
    if profile:
        for form in {profile, _short(profile).rstrip("\\/")}:
            names.add(re.split(r"[\\/]", form)[-1])
    return {name for name in names if len(name) >= 3}


# A long run of hex digits standing on its own: a SHA-256 (64), a commit id
# (40) or a model's verification key (16, workspace/asr_engines.py). A user
# name made only of the letters a to f (Abe, Dee) turns up inside one about
# once in 200 hashes, so user_name_at() looks past these runs rather than
# fail on them (review of 78f17ee, #419). Only a whole run counts: with a
# letter or digit more on either side, or capitals and small letters mixed,
# it is not one, so a name beside a hash (a slash, an underscore or a space
# between) is still found.
_HEX_RUN = re.compile(r"(?<![0-9A-Za-z])(?:[0-9a-f]{16,}|[0-9A-F]{16,})(?![0-9A-Za-z])")


def redact(text, base=None, environ=None, error=True):
    """text with the throwaway base folder written as <base>, the home folder
    as <home> and then the Windows user name as <user>, in each form an error
    can carry (long and short names, either slash, the doubled backslashes
    of repr and JSON, escaped non-ASCII: _escaped()), case ignored. Error
    tails copied into a report go through this: the reports are published
    with a release, and these paths name the user (P3; review of #335).

    The user name is replaced as a word (_NAME_BEFORE, _NAME_AFTER): in
    error text (error) wherever it stands; otherwise (a key or value of a
    child's result, redact_json()) only in text that holds a slash or a
    backslash, a path. So a short user name never changes a plain key or
    value such as "opens" (review of #335). The name in other plain text of
    a result is left to guarded(), which fails the report on it (review of
    387f8d3)."""
    if not text:
        return text
    environ = os.environ if environ is None else environ
    forms = []
    for placeholder, path in (("<base>", base), ("<home>", environ.get("USERPROFILE"))):
        if not path:
            continue
        for p in {str(path), os.path.realpath(path), _short(path)}:
            p = p.rstrip("\\/")
            if len(p) < 4:                     # never a bare drive
                continue
            for form in {p, p.replace("\\", "/"), p.replace("\\", "\\\\")}:
                forms += [(f, placeholder) for f in _escaped(form)]
    for form, placeholder in sorted(forms, key=lambda f: -len(f[0])):
        text = re.sub(re.escape(form), placeholder, text, flags=re.IGNORECASE)
    user = environ.get("USERNAME")
    if user and len(user) >= 3 and (error or "/" in text or "\\" in text):
        text = _name_regex({user}).sub("<user>", text)
    return text


def redact_json(value, base=None, environ=None, error=False):
    """A child's decoded JSON result with redact() applied to every string in
    it, keys included: a value under a key that names an error ("error",
    "..._error") as error text, the rest by path only. Redacted after
    decoding, so a name that JSON escaped (\\u0101 for ā) is matched as
    written (review of #335)."""
    if isinstance(value, str):
        return redact(value, base, environ, error)
    if isinstance(value, list):
        return [redact_json(v, base, environ, error) for v in value]
    if isinstance(value, dict):
        return {redact(k, base, environ, False):
                redact_json(v, base, environ, error or "error" in k.lower())
                for k, v in value.items()}
    return value


def user_name_at(report, environ=None):
    """Where a report would carry the Windows user name (user_names()), in
    any _escaped() form, as a word (_NAME_BEFORE, _NAME_AFTER): every key
    and string as decoded, and the report as json.dumps writes it. Each
    place is its key path, a list of keys and list positions; a key that
    carries the name is written as "<key>", so a path never holds the name.
    The placeholders redact() writes are not the name, and nor is a match
    inside a long run of hex digits (_HEX_RUN: a hash or a commit id)."""
    rx = _name_regex(user_names(environ))
    if rx is None:
        return []

    def carries(text):
        for placeholder in PLACEHOLDERS:
            text = text.replace(placeholder, " ")
        return rx.search(_HEX_RUN.sub(" ", text)) is not None

    found = []

    def walk(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                named = carries(str(key))
                here = [*path, "<key>" if named else key]
                if named:
                    found.append(here)
                walk(item, here)
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                walk(item, [*path, i])
        elif isinstance(value, str) and carries(value):
            found.append(path)

    walk(report, [])
    # The whole report as written (json.dumps escapes what is not ASCII), in
    # case a form shows only there.
    if not found and any(carries(json.dumps(report, indent=2, ensure_ascii=a))
                         for a in (True, False)):
        found.append(["<the serialised report>"])
    return found


def guarded(report, environ=None):
    """The report to write, the last step before it is written: the report
    itself or, when it would carry the Windows user name anywhere
    (user_name_at()), a failing report in its place that names each key
    path and never the value (P3; review of 387f8d3, #335). It fails closed
    for every field: the file-name lists (left over and outside, explained
    or not) and any field added later included."""
    at = user_name_at(report, environ)
    if not at:
        return report
    earlier = [f for f in report.get("failed") or [] if isinstance(f, str)
               and not user_name_at([f], environ)]
    return {"when": report.get("when"), "pass": False,
            "failed": [CARRIES_USER_NAME, *earlier], "user_name_at": at}


def write_guarded(path, report, environ=None):
    """Write the report to path as JSON after guarded(), the last step, and
    return what was written: the report itself, or the failing report put
    in its place (P3). The performance, UI and accessibility checks write
    every report with this (review of 78f17ee, #419); a caller tells the two
    apart with `is`."""
    written = guarded(report, environ)
    Path(path).write_text(json.dumps(written, indent=2), encoding="utf-8")
    return written


def logs_scanned(scanned, n, prefix):
    """The log files among the scanned ones (prefix: the workspace folder,
    relative to the scan's base, POSIX form)."""
    folder = f"{prefix}/{n['logs']}/"
    return sorted(s for s in scanned if s.startswith(folder))


# -- crash dumps (names and sizes only) ------------------------------------------------------

def _is_dump(name):
    low = name.lower()
    return low.endswith(DUMP_SUFFIXES)


def dumps(base):
    """Crash files under the throwaway base, by name and size only (never
    opened): {"dumps": Windows dump files, "crash_files": the app's own
    crash files with something in them}. The app opens an empty crash file
    for each run (workspace/diag.py), so an empty one is not a crash."""
    out = {"dumps": 0, "crash_files": 0}
    for folder, _dirs, files in os.walk(base):
        for name in files:
            if _is_dump(name):
                out["dumps"] += 1
            elif name.lower().endswith(CRASH_SUFFIX):
                try:
                    if os.stat(os.path.join(folder, name)).st_size > 0:
                        out["crash_files"] += 1
                except OSError:
                    out["crash_files"] += 1          # cannot tell: counted
    return out


def system_dump_folders(environ=None):
    """The signed-in user's own dump folders: CrashDumps and Windows Error
    Reporting's, from the real environment (the checks redirect only their
    children's LOCALAPPDATA)."""
    environ = os.environ if environ is None else environ
    local, data = environ.get("LOCALAPPDATA"), environ.get("ProgramData")
    out = []
    if local:
        out += [Path(local) / "CrashDumps",
                Path(local) / "Microsoft" / "Windows" / "WER" / "ReportArchive",
                Path(local) / "Microsoft" / "Windows" / "WER" / "ReportQueue"]
    if data:
        out += [Path(data) / "Microsoft" / "Windows" / "WER" / "ReportArchive",
                Path(data) / "Microsoft" / "Windows" / "WER" / "ReportQueue"]
    return out


def _named_after_runtime(name):
    low = name.lower()
    return any(p in low for p in DUMP_PROGRAMS)


def _folder_label(path):
    """A dump folder named without the user's profile (P3): its last parts."""
    parts = Path(path).parts
    return "/".join(parts[-3:]) if len(parts) > 3 else Path(path).as_posix()


def system_dumps(since, folders=None):
    """Files in the user's dump folders named after the runtime's or the
    window's executables, changed since `since` (time.time()), counted by
    name and time only (a report folder counts once). Run only when a person
    asks: the agent guard refuses listing these folders, and dumps of other
    Python programs on the machine count too. A folder that does not exist
    holds none ("missing"); one that cannot be listed is named under
    "unreadable", so 0 is never read as "none" (review of #335)."""
    count, missing, unreadable = 0, [], []
    for root in (system_dump_folders() if folders is None else folders):
        try:
            entries = list(os.scandir(root))
        except FileNotFoundError:
            missing.append(_folder_label(root))
            continue
        except OSError:
            unreadable.append(_folder_label(root))
            continue
        for entry in entries:
            if not _named_after_runtime(entry.name):
                continue
            try:
                if entry.stat().st_mtime >= since:
                    count += 1
            except OSError:
                continue
    tried = len(folders if folders is not None else system_dump_folders())
    return {"new_files": count, "folders": tried,
            "folders_read": tried - len(missing) - len(unreadable),
            "missing": missing, "unreadable": unreadable,
            "since": time.strftime("%Y-%m-%d %H:%M", time.localtime(since))}
