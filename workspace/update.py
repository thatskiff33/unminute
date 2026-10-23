# workspace/update.py
# Installation-aware updates and the empty-workspace migration gate (#26).
#
# One gate for both deployment modes. The installer runs the NEW package's
# runtime with `python -m workspace --gate` before switching version, so the
# decision is made by code that knows the new schema:
#
#   no workspace yet             -> allowed (fresh install)
#   same schema                  -> allowed: a compatible update keeps everything
#                                   (the installer already refuses while the app
#                                   runs, so nothing is processing or cleaning up)
#   newer schema                 -> allowed only when the workspace holds no
#                                   meeting content, no active retention hold and
#                                   no unfinished cleanup; otherwise it WAITS and
#                                   explains why. Minimal completed receipts and
#                                   configuration never block.
#   older schema (a downgrade)   -> refused: that version cannot open this workspace
#
# Nothing here expires a hold, deletes content or makes a content-bearing
# backup to let an update proceed.

import json
import os
import re
import sqlite3
from pathlib import Path

ALLOWED, WAIT, TOO_NEW = 0, 5, 6     # exit codes used by the installer
UNREADABLE = object()                  # current_schema(): the workspace exists but cannot be read

# Tables that never make a migration wait: bookkeeping (schema_version, the
# minimal receipts), watched-folder configuration (#23) and the workspace's
# root key (crypto_meta, #111 C2b-2). Every other table can hold meeting
# content. A watched folder's path is sealed like content
# (store.CONTENT_COLUMNS, #111 C2) because it can name a client, but it is
# configuration, not a meeting's content: it is carried over
# (store.CARRY_OVER), with the root key that opens it, and never blocks an
# update (C4). A meeting's or a recording's key (content_keys,
# recording_keys) and a recording source are not here: one left without its
# meeting is leftover content, and the update waits.
NON_CONTENT = {"schema_version", "receipts", "sqlite_sequence",
               # watched-folder configuration and bounded suppression data (#23)
               "watched_folders", "intake_seen", "intake_content", "intake_meta",
               # the root key, protected for this Windows user (#111 C2b-2)
               "crypto_meta"}


def deployment(app_file=None, trusted=None):
    """How this copy was installed: {"version", "mode":
    machine|managed|self-service|source}. "machine" (#111 B6b) only when
    current.json says so ("mode": "machine", written by install.ps1 -Machine)
    and the install passes machine_trusted(); a claim that fails it reads as
    the record's managed or self-service mode, with "machine_refused": True so
    the app can log it (P3: no path or name). trusted stands in for
    machine_trusted in tests."""
    here = Path(app_file or __file__).resolve()
    try:
        install_root = here.parents[4]           # <root>/versions/<v>/app/workspace/x.py
        # Windows PowerShell writes UTF-8 with a byte-order mark.
        current = json.loads((install_root / "current.json").read_text(encoding="utf-8-sig"))
        found = {"version": current.get("version"),
                 "mode": "managed" if current.get("managed") else "self-service"}
    except (OSError, ValueError, IndexError, AttributeError):
        return {"version": None, "mode": "source"}
    if current.get("mode") == "machine":
        if (trusted or machine_trusted)(install_root):
            found["mode"] = "machine"
        else:
            found["machine_refused"] = True
    return found


# -- The machine trust test (#111 B6b, design 5.2) -----------------------------------
#
# Machine mode changes what the app relies on (the launcher's hand-over between
# installed versions now; machine-wide models in B7), so a copy is "machine"
# only when all of these hold, whatever its current.json says:
#   1. its install folder, fully resolved, is FOLDERID_ProgramFiles\Unminute, as
#      Windows' known folder gives it (never an environment variable), and that
#      known folder is itself a real folder (no junction or link on the way);
#   2. the install folder's owner is Administrators or SYSTEM;
#   3. this process's user cannot change it: an AccessCheck against this
#      process's token grants no right to add, delete or change permissions.
# A per-user install that writes "machine" into its own current.json fails 1 and
# 3. Any error fails the test (it never grants). An elevated administrator also
# fails 3; the app runs without elevation.

MACHINE_FOLDER = "Unminute"
OWNERS = ("S-1-5-32-544", "S-1-5-18")     # Administrators, SYSTEM
# FILE_ADD_FILE, FILE_ADD_SUBDIRECTORY, FILE_WRITE_EA, FILE_DELETE_CHILD,
# FILE_WRITE_ATTRIBUTES, DELETE, WRITE_DAC, WRITE_OWNER.
CHANGE_RIGHTS = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000
_FOLDERID_PROGRAM_FILES = "{905E63B6-C1BF-494E-B29C-65B732D3D21A}"


def machine_trusted(install_root, *, program_files=None, owner=None, can_change=None):
    """The machine trust test above. program_files, owner and can_change stand
    in for _program_files, _owner_sid and _can_change in tests."""
    try:
        known = (program_files or _program_files)()
        if not known or os.path.normcase(os.path.realpath(known)) != os.path.normcase(
                os.path.abspath(known)):
            return False
        root = os.path.realpath(install_root)
        if os.path.normcase(root) != os.path.normcase(os.path.join(os.path.abspath(known),
                                                                   MACHINE_FOLDER)):
            return False
        if (owner or _owner_sid)(root) not in OWNERS:
            return False
        return not (can_change or _can_change)(root)
    except Exception:   # any error fails the test (a ctypes ArgumentError, a TypeError, ...)
        return False


def _windows():
    if os.name != "nt":
        raise OSError("not Windows")
    import ctypes
    from ctypes import wintypes
    return ctypes, wintypes


def _program_files():
    """FOLDERID_ProgramFiles from SHGetKnownFolderPath (in a 64-bit process,
    C:\\Program Files)."""
    ctypes, wintypes = _windows()

    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                    ("d", ctypes.c_ubyte * 8)]

    guid = GUID()
    ole32 = ctypes.WinDLL("ole32")
    ole32.CLSIDFromString.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(GUID))
    if ole32.CLSIDFromString(_FOLDERID_PROGRAM_FILES, ctypes.byref(guid)) != 0:
        raise OSError("known folder id")
    path = ctypes.c_wchar_p()
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHGetKnownFolderPath.argtypes = (ctypes.POINTER(GUID), wintypes.DWORD,
                                             wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p))
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)) != 0:
        raise OSError("known folder")
    try:
        return path.value
    finally:
        ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
        ole32.CoTaskMemFree(ctypes.cast(path, ctypes.c_void_p))


def _security(path, what):
    """The folder's security descriptor (owner, group and permissions as asked)
    as bytes, from GetFileSecurityW."""
    ctypes, wintypes = _windows()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.GetFileSecurityW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    advapi32.GetFileSecurityW.restype = wintypes.BOOL
    needed = wintypes.DWORD(0)
    advapi32.GetFileSecurityW(str(path), what, None, 0, ctypes.byref(needed))
    if not needed.value or needed.value > 1 << 20:
        raise OSError("security descriptor")
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi32.GetFileSecurityW(str(path), what, buffer, needed, ctypes.byref(needed)):
        raise OSError("security descriptor")
    return buffer.raw


def _owner_sid(path):
    """The folder's owner as a SID string (S-1-...)."""
    ctypes, wintypes = _windows()
    raw = _security(path, 0x1)                          # OWNER_SECURITY_INFORMATION
    buffer = ctypes.create_string_buffer(raw, len(raw))
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32")
    advapi32.GetSecurityDescriptorOwner.argtypes = (ctypes.c_void_p,
                                                    ctypes.POINTER(ctypes.c_void_p),
                                                    ctypes.POINTER(wintypes.BOOL))
    advapi32.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p))
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    sid, defaulted = ctypes.c_void_p(), wintypes.BOOL()
    if not advapi32.GetSecurityDescriptorOwner(buffer, ctypes.byref(sid), ctypes.byref(defaulted)) \
            or not sid.value:
        raise OSError("owner")
    text = ctypes.c_wchar_p()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise OSError("owner")
    try:
        return text.value
    finally:
        kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _can_change(path):
    """Whether this process's user may add to, delete or change the
    permissions of the folder."""
    return _grants_change(_security(path, 0x1 | 0x2 | 0x4))   # owner, group, DACL


def _grants_change(descriptor):
    """Whether a security descriptor (bytes, self-relative) grants this
    process's token any of CHANGE_RIGHTS: AccessCheck with MAXIMUM_ALLOWED
    against an impersonation copy of the process token."""
    ctypes, wintypes = _windows()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32")

    class GENERIC_MAPPING(ctypes.Structure):
        _fields_ = [("read", wintypes.DWORD), ("write", wintypes.DWORD),
                    ("execute", wintypes.DWORD), ("all", wintypes.DWORD)]

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE))
    advapi32.DuplicateToken.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                        ctypes.POINTER(wintypes.HANDLE))
    advapi32.AccessCheck.argtypes = (ctypes.c_void_p, wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.POINTER(GENERIC_MAPPING), ctypes.c_void_p,
                                     ctypes.POINTER(wintypes.DWORD),
                                     ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.BOOL))
    sd = ctypes.create_string_buffer(descriptor, len(descriptor))
    token, copy = wintypes.HANDLE(), wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008 | 0x0002,
                                     ctypes.byref(token)):      # TOKEN_QUERY | TOKEN_DUPLICATE
        raise OSError("token")
    try:
        if not advapi32.DuplicateToken(token, 2, ctypes.byref(copy)):   # SecurityImpersonation
            raise OSError("token")
        try:
            mapping = GENERIC_MAPPING(0x120089, 0x120116, 0x1200A0, 0x1F01FF)   # FILE_GENERIC_*
            privileges = ctypes.create_string_buffer(1024)
            size = wintypes.DWORD(len(privileges))
            granted, status = wintypes.DWORD(0), wintypes.BOOL(False)
            if not advapi32.AccessCheck(sd, copy, 0x02000000, ctypes.byref(mapping), privileges,
                                        ctypes.byref(size), ctypes.byref(granted),
                                        ctypes.byref(status)):   # MAXIMUM_ALLOWED
                raise OSError("access check")
            return bool(status.value) and bool(granted.value & CHANGE_RIGHTS)
        finally:
            kernel32.CloseHandle(copy)
    finally:
        kernel32.CloseHandle(token)


def package_version():
    """version.json beside app/ in an installed package. From a source checkout,
    the newest prepared release from VERSION with "+source" (later changes may
    be included), or just "source" if there is no VERSION file."""
    path = Path(__file__).resolve().parents[2] / "version.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        version = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8")
        return version.strip() + "+source"
    except OSError:
        return "source"


# The gate reads only a path on a plain drive letter (C:\...). It never reads
# a UNC path (\\server\share, //server/share), an extended or device path
# (\\?\..., \\.\..., including \\?\UNC\...), an NT-namespace path (\??\...,
# which Windows passes straight through, so \??\UNC\server\share reaches the
# network) or a name Windows turns into a device: abspath() makes NUL, in any
# folder, and a bare CON into \\.\NUL and \\.\CON, which are refused. CON
# inside a folder (C:\x\CON) stays an ordinary local path, as Windows 11 reads
# it, and is read as one (#213). The workspace is never on a
# network share (D3), and as_uri() would turn \\localhost\C$\x into
# file://localhost/C%24/x, which SQLite opens as /C$/x on the current drive, a
# different file. A mapped network drive letter (Z:\) passes here: for the
# installer's --gate, store.default_root() refuses it first (for
# %LOCALAPPDATA% itself, and since #219 for Unminute and Unminute\workspace
# redirected there by a junction or link), so the gate never sees one.
_DRIVE = re.compile(r"[A-Za-z]:\\")
_NT_NAMESPACE = "\\??\\"


def _local(db_path):
    """Refuse a path that is not on a plain local drive letter: UNC, extended,
    device and NT-namespace paths, checked as written and as Windows resolves
    it against the current folder, before anything reads it. No path in the
    message (P3)."""
    raw = os.fspath(db_path)
    if os.name == "nt":
        # abspath() is GetFullPathNameW: string work only, no file access. It
        # turns a reserved name into \\.\NUL and a relative path under a UNC
        # current folder into \\server\share\..., both refused. It reads
        # \??\x as rooted on the current drive, so that prefix is refused as
        # written.
        local = (not raw.replace("/", "\\").startswith(_NT_NAMESPACE)
                 and _DRIVE.match(os.path.abspath(raw)) is not None)
    else:                               # Linux, for the tests
        local = not any(s.startswith(("//", "\\\\")) for s in (raw, os.path.abspath(raw)))
    if not local:
        raise sqlite3.OperationalError("The workspace path is not on a local drive.")


def _connect(db_path):
    # as_uri() percent-encodes the path. Unescaped, a "#" or "?" in a folder
    # name ends the path (and "?mode=ro" with it, so the connection could
    # write) and a "%41" is decoded, so the workspace reads as UNREADABLE and
    # every update waits (#164).
    _local(db_path)
    return sqlite3.connect(Path(db_path).absolute().as_uri() + "?mode=ro", uri=True)


def _stat(db_path):
    """os.stat of the database, or None if it does not exist. Raises OSError
    when that cannot be told (for example, access is denied)."""
    try:
        return os.stat(db_path)
    except (FileNotFoundError, NotADirectoryError):
        if os.name != "nt":
            return None
    # Without long paths enabled (LongPathsEnabled 0), Windows reports a path
    # longer than MAX_PATH as missing even when it exists. The extended form
    # has no such limit: ask again, so an existing workspace never reads as a
    # fresh install. abspath() resolves the path as Windows would.
    try:
        return os.stat("\\\\?\\" + os.path.abspath(db_path))
    except (FileNotFoundError, NotADirectoryError):
        return None


def current_schema(db_path):
    """The workspace schema; None if there is no workspace; UNREADABLE if a
    database exists but cannot be read (damaged, locked or not ours), if the
    path is not on a local drive, or if whether it exists cannot be told. The
    gate fails closed on UNREADABLE: never treat it as a fresh install."""
    try:
        _local(db_path)                 # before any file access
        st = _stat(db_path)
    except (sqlite3.Error, OSError):
        return UNREADABLE
    if st is None or st.st_size == 0:
        return None
    try:
        con = _connect(db_path)
    except sqlite3.Error:
        return UNREADABLE
    try:
        return con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] \
            or UNREADABLE
    except sqlite3.Error:
        return UNREADABLE
    finally:
        con.close()


def blockers(db_path):
    """What stops a schema-changing update, in plain language. Counts only.
    Reads plain columns and folder names only: no key is needed, so it works
    on a workspace whose key cannot be unlocked."""
    _local(db_path)                     # before any file access
    try:
        if _stat(db_path) is None:
            return []
    except OSError:
        pass                            # cannot tell: _connect reads it or raises
    con = _connect(db_path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")]
        count = (lambda t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
        out = []
        holds = (con.execute("SELECT COUNT(*) FROM holds WHERE released_at IS NULL")
                 .fetchone()[0] if "holds" in tables else 0)
        if holds:
            out.append({"kind": "holds", "count": holds, "message":
                        f"{holds} retention hold{'s are' if holds != 1 else ' is'} active. "
                        "Holds never expire on their own; the update waits until they are "
                        "released in the app by the people responsible."})
        meetings = count("meetings") if "meetings" in tables else 0
        if meetings:
            out.append({"kind": "meetings", "count": meetings, "message":
                        f"{meetings} meeting{'s are' if meetings != 1 else ' is'} still in the "
                        "workspace. Finish each one (review, hand off, then confirm the final "
                        "document is saved) so its content is cleaned up."})
        jobs = count("cleanup_jobs") if "cleanup_jobs" in tables else 0
        if jobs:
            out.append({"kind": "cleanup", "count": jobs, "message":
                        f"{jobs} cleanup step{'s have' if jobs != 1 else ' has'} not finished. "
                        "Open the app so it can finish (or confirm) them."})
        recordings = Path(db_path).parent / "recordings"
        waiting = (sum(1 for p in recordings.iterdir() if p.is_dir())
                   if recordings.is_dir() else 0)
        if waiting:
            out.append({"kind": "recordings", "count": waiting, "message":
                        f"{waiting} microphone recording{'s have' if waiting != 1 else ' has'} "
                        "not been imported yet. Open the app so it can import or discard "
                        "them."})
        # Meeting audio left in the workspace (media\<id>), with or without
        # its meeting: data the tables do not show (#111 C2b-2). The update
        # waits; nothing here deletes it (C4). The app never removes a folder
        # that has no meeting (for example one an earlier version left), so
        # the message names IT's next step for those (#111 C3a).
        ids = ({r[0].lower() for r in con.execute("SELECT id FROM meetings")
                if isinstance(r[0], str)} if "meetings" in tables else set())
        folders = _filled_folders(Path(db_path).parent / "media")
        if folders:
            orphans = sum(1 for name in folders if name.lower() not in ids)
            message = "Meeting audio files are still in the workspace."
            if orphans:
                message += (f" {orphans} of these folders belong{'s' if orphans == 1 else ''} "
                            "to no meeting, and Unminute never deletes such a folder by "
                            "itself. If nothing requires keeping them (no retention hold or "
                            "other reason applies), IT can delete them from the workspace's "
                            "media folder, then run the update again.")
            out.append({"kind": "media", "count": len(folders), "message": message})
        known = {"holds", "meetings", "cleanup_jobs"} | NON_CONTENT
        leftover = sum(count(t) for t in tables if t not in known)
        if leftover and not meetings:
            out.append({"kind": "content", "count": leftover, "message":
                        "Some meeting content is still stored. Open the app and finish the "
                        "cleanup steps."})
        return out
    finally:
        con.close()


def _filled_folders(folder):
    """The names of folder's subfolders that hold anything. One that cannot
    be read counts as filled: the update waits rather than guess."""
    if not folder.is_dir():
        return []
    names = []
    for p in folder.iterdir():
        try:
            if p.is_dir() and next(p.iterdir(), None) is not None:
                names.append(p.name)
        except OSError:
            names.append(p.name)
    return names


def gate(db_path, new_schema):
    """The update decision for a package whose schema is `new_schema`."""
    cur = current_schema(db_path)
    if cur is UNREADABLE:
        return {"code": WAIT, "kind": "unreadable", "current_schema": None, "blockers": [],
                "message": "The workspace could not be read, so the update waits rather than "
                           "risk it. Open the app to check it, or ask support "
                           "(python -m workspace --status)."}
    if cur is None:
        return {"code": ALLOWED, "kind": "fresh", "current_schema": None, "blockers": [],
                "message": "No workspace yet: the update can go ahead."}
    if cur == new_schema:
        return {"code": ALLOWED, "kind": "compatible", "current_schema": cur, "blockers": [],
                "message": "The workspace is compatible: everything is kept."}
    if cur > new_schema:
        return {"code": TOO_NEW, "kind": "too_new", "current_schema": cur, "blockers": [],
                "message": "This workspace was created by a newer version of the app, which "
                           "this version cannot open. Install the newer version again."}
    found = blockers(db_path)
    if found:
        return {"code": WAIT, "kind": "migration", "current_schema": cur, "blockers": found,
                "message": "This update changes how the workspace is stored, so it waits "
                           "until the workspace is empty: " +
                           " ".join(b["message"] for b in found)}
    return {"code": ALLOWED, "kind": "migration", "current_schema": cur, "blockers": [],
            "message": "The workspace is empty: the update can go ahead. Settings and "
                       "completed cleanup receipts are kept."}


# -- Launch (#111 B5) ---------------------------------------------------------------
#
# In machine mode (B6) the installer runs as SYSTEM, which sees no user's
# workspace, so whether a version may take over a workspace is decided when
# each user starts the app (workspace/launch.py). Per-user installs keep the
# install-time gate above (the owner's decision B5-1, 21 Oct 2026).

# How the app is started. install.ps1's $LaunchArgs, its gate run and the
# uninstaller's --restore-lid run use the same options (tests/test_launch.py
# keeps them equal): -I isolated, -B no bytecode written, and no remote
# debugger (PEP 768).
LAUNCH_ARGS = ["-I", "-B", "-X", "disable-remote-debug", "-m", "workspace"]

# What a hand-over starts a version as (--slot). current: the version
# current.json names. compat: the newest installed version of the
# workspace's older storage format, while the workspace still holds what
# blocks the update (B5b opens it for finishing meetings only). fallback:
# another version of the same format after a failed first-start check (B5b).
# There is no "newer" role: a downgrade or withdrawal is strict (the owner's
# decision B5-8, 21 Oct 2026), so a workspace from a newer version is never
# handed to one.
ROLES = ("current", "compat", "fallback")

# install.ps1's $VersionPattern and its other rules (tests/test_launch.py
# keeps them equal): letters and digits, then also . + _ -, at most 64
# characters, no final dot (Windows drops it) and not a working folder's name.
_VERSION_NAME = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
_WORKING = re.compile(r"\.(partial|old|retired)\Z")     # .retired: machine mode (#111 B6b)
_RECORD_MAX = 64 * 1024                 # current.json and version.json are far smaller


def version_name(value):
    """A plain version folder name, as the installer accepts one."""
    return (isinstance(value, str) and _VERSION_NAME.fullmatch(value) is not None
            and not value.endswith(".") and _WORKING.search(value) is None)


def whole(value):
    """A positive whole number (a JSON true is not one)."""
    return type(value) is int and value > 0


def read_record(path):
    """A small JSON object from path: the object, or None if it is missing,
    too large, not JSON or not an object. Windows PowerShell writes UTF-8
    with a byte-order mark."""
    try:
        with open(path, "rb") as f:
            raw = f.read(_RECORD_MAX + 1)
        if len(raw) > _RECORD_MAX:
            return None
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def read_slots(install_root):
    """current.json's record of the installed versions (format 2, #111 B5):
    (state, record). state is "missing" (no file, as in a source checkout),
    "damaged" (unreadable or any value fails its check), "format1" (an
    install from before B5: no versions known beyond its current one) or
    "ok". record: {"format", "version", "previous", "slots"}, where slots maps
    each version name to {"schema": a whole number or None (unknown, never
    handed over to), "seq": its install order}; None unless state is ok or
    format1. No value read here becomes a path unless it is a plain name."""
    path = Path(install_root) / "current.json"
    if not path.exists():
        return "missing", None
    data = read_record(path)
    if data is None:
        return "damaged", None
    version, previous = data.get("version"), data.get("previous")
    if not version_name(version) or not (previous is None or version_name(previous)):
        return "damaged", None
    if "format" not in data:
        return "format1", {"format": 1, "version": version, "previous": previous,
                           "slots": {version: {"schema": None, "seq": None}}}
    slots = data.get("slots")
    if data["format"] != 2 or type(data["format"]) is not int or not isinstance(slots, dict):
        return "damaged", None
    out = {}
    for name, entry in slots.items():
        if not version_name(name) or not isinstance(entry, dict):
            return "damaged", None
        schema, seq = entry.get("schema"), entry.get("seq")
        if not whole(seq) or not (schema is None or whole(schema)):
            return "damaged", None
        out[name] = {"schema": schema, "seq": seq}
    seqs = [e["seq"] for e in out.values()]
    if version not in out or len(set(seqs)) != len(seqs):
        return "damaged", None
    return "ok", {"format": 2, "version": version, "previous": previous, "slots": out}


def newest_of_schema(slots, schema):
    """The installed version of that storage format installed last (the
    highest seq), or None. A version of unknown format is never one."""
    found = [(e["seq"], name) for name, e in slots.items()
             if e["schema"] is not None and e["schema"] == schema]
    return max(found)[1] if found else None


def launch_decision(workspace, has_blockers, record):
    """Which installed version opens this workspace, decided as if the
    current version had been started, whichever one ran: (version, role,
    refusal). workspace: current_schema()'s answer (None, a number or
    UNREADABLE). has_blockers: blockers() found something; asked only when
    the workspace is older than the current version's format. refusal, when
    nothing may open it: "blocked" (older, still holding meetings, and no
    version of its format is installed) or "too_new" (from a newer version:
    strict, B5-8). Never an older format for an empty or unreadable
    workspace, so nothing creates or keeps a workspace an update cannot take."""
    current = record["version"]
    schema = record["slots"][current]["schema"]
    if workspace is UNREADABLE or not whole(workspace) or schema is None:
        return current, "current", None     # it shows its own damage, locked or location screen
    if workspace == schema or (workspace < schema and not has_blockers):
        return current, "current", None     # an older, empty workspace is migrated there
    if workspace < schema:
        compat = newest_of_schema(record["slots"], workspace)
        return (compat, "compat", None) if compat else (None, None, "blocked")
    return None, None, "too_new"
