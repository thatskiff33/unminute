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
#   - dumps(): crash dumps under the throwaway base, counted by name and
#     size (os.walk and stat only): no dump is ever opened, copied or
#     printed. system_dumps() counts the signed-in user's own dump folders,
#     only when a person asks (--report-system-dumps): a dump of the
#     installed app could hold real content (AGENTS.md P2).
#
# Standard library only: the checks run it in the package's own runtime.

import os
import re
import secrets
import time
from pathlib import Path

CHUNK = 8 << 20                       # bytes read at a time while scanning
CONTROL_FOLDER = "residue-control"    # in the base folder, outside the profile
DUMP_SUFFIXES = (".dmp", ".mdmp", ".hdmp")
CRASH_SUFFIX = ".crash"               # the app's own faulthandler file (workspace/diag.py)
# The runtime's executables and the window's, as Windows Error Reporting
# names their dumps (python.exe.1234.dmp) and report folders
# (AppCrash_python.exe_...).
DUMP_PROGRAMS = ("python.exe", "pythonw.exe", "msedgewebview2.exe")


# -- finding patterns -----------------------------------------------------------------

def _files(roots):
    for root in roots:
        root = Path(root)
        if root.is_file():
            yield root
            continue
        for folder, _dirs, names in os.walk(root):
            for name in sorted(names):
                yield Path(folder) / name


def find(roots, patterns, base):
    """Which files under roots hold which patterns ({label: bytes}).
    Returns {"hits": {label: [file]}, "unreadable": [file], "scanned": [file]},
    each file relative to base (POSIX form). A file that cannot be read cannot
    be shown free of the patterns: the caller fails on it. Reads in CHUNK
    pieces that overlap by the longest pattern, so a large file costs bounded
    memory and a pattern across a boundary is still found."""
    patterns = {label: bytes(p) for label, p in patterns.items() if p}
    overlap = max((len(p) for p in patterns.values()), default=1) - 1
    hits, unreadable, scanned = {label: [] for label in patterns}, [], []
    for path in _files(roots):
        name = Path(os.path.relpath(path, base)).as_posix()
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
    workspace folder (%LOCALAPPDATA%\\Unminute\\workspace)."""
    from workspace import audit, diag, handoff, power, store
    return {
        "db": diag.DB_FILE,
        "files": (diag.DB_FILE, diag.DB_FILE + "-wal", diag.DB_FILE + "-shm",
                  diag.SETTINGS_FILE, diag.SETTINGS_UNREADABLE_FILE, handoff.TEMPLATES_FILE,
                  power.RESTORE_FILE, store.compaction_marker(diag.DB_FILE).name),
        "logs": diag.LOGS_FOLDER,
        "log_files": (diag.INTEGRITY_FILE, diag.KEYS_FILE),
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
    that allowed() does not name. POSIX paths relative to profile."""
    out = []
    for path in _files([profile]):
        rel = Path(os.path.relpath(path, profile)).as_posix()
        inside = Path(os.path.relpath(path, workspace)).as_posix()
        if inside.startswith("../") or not allowed(inside, n):
            out.append(rel)
    return out


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


def system_dumps(since, folders=None):
    """Files in the user's dump folders named after the runtime's or the
    window's executables, changed since `since` (time.time()), counted by
    name and time only (a report folder counts once). Run only when a person
    asks: the agent guard refuses listing these folders, and dumps of other
    Python programs on the machine count too."""
    count = 0
    for root in (system_dump_folders() if folders is None else folders):
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            if not _named_after_runtime(entry.name):
                continue
            try:
                if entry.stat().st_mtime >= since:
                    count += 1
            except OSError:
                continue
    return {"new_files": count, "folders": len(folders if folders is not None
                                               else system_dump_folders()),
            "since": time.strftime("%Y-%m-%d %H:%M", time.localtime(since))}
