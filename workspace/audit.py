# workspace/audit.py
# A content-free, hash-chained audit log of how meetings are handled, with a
# Windows Event Log copy (#111 C5; docs/WORKSPACE_DESIGN.md "Audit log").
#
# What it is for (docs/SECURITY_MODEL.md T17, T18, T20, AS9): showing who
# imported, recorded, handed off, held or deleted what, and when, after the
# meeting itself is gone. It never holds content: each record is a fixed kind
# with fields that are random ids, fixed words, counts and true/false, each
# checked against its kind's rule before anything is written (KINDS), as
# workspace/diag.py does for the logs. Never a title, name, path, reason,
# template text, device name or error text (AGENTS.md P3).
#
# Storage: workspace\audit\audit-000001.jsonl and on, one canonical JSON line
# per record. Not in workspace.db: it is written while the workspace is locked
# and survives an empty-workspace migration. Each record names its chain (a
# random id), its sequence number in the chain and the SHA-256 of the line
# before it (prev), so an edited, removed or reordered line breaks the chain
# (verify). The chain hash is plain SHA-256: it works while locked and IT can
# check it without the user's key, but the same user can rebuild the file;
# the Event Log copy (chain, seq, kind and the line's SHA-256, never an id)
# is what shows that, and only a copy forwarded off the device resists the
# user (compare_event_log).
#
# Segments rotate at SEGMENT_BYTES; past MAX_BYTES the oldest is deleted and a
# segment_dropped record says which, by number and last line (so deleting old
# segments by hand shows while the drop records around it are kept).
# A line cut short by a crash is left as it is: the next record starts a new
# segment with tail_damaged. The kinds the page drives are rate-limited and
# what is held back is counted (suppressed); the records the app writes for
# imports and deletions (APP_WRITTEN) never are, nor the internal ones (the
# owner's decision, 21 Oct 2026). A failed write never blocks or undoes
# the action that was recorded: it is counted, and write_failed says how many
# at the next write.
#
# Standard library only at module level; ctypes is imported only inside the
# functions that call Windows (the window process's import rule, C5-16).

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

from workspace import diag

FORMAT = 1
SOURCE = "Unminute"                  # the Event Log source (unregistered: per-user installs)
SEGMENT_BYTES = 1 << 20              # a segment rotates at 1 MB
MAX_BYTES = 16 << 20                 # the folder keeps at most 16 MB (about 80,000 records)
TAIL_BYTES = 64 * 1024               # how much of the newest segment is read at open
RATE_PER_KIND_MINUTE = 20            # LIMITED (page-driven) kinds: records a minute, per kind
RATE_TOTAL_HOUR = 200                # ...and all of them together, an hour
MIRROR_RETRY_S = 60.0                # a failing Event Log is tried again this often
CLOSE_DRAIN_S = 2.0                  # at close, the mirror queue is drained for this long
ZERO = "0" * 64                      # prev of a chain's first record
BIG = 10 ** 12
# Six digits, or more without a leading zero (past 999999), so no two names
# share a number.
SEGMENT = re.compile(r"audit-([0-9]{6}|[1-9][0-9]{6,11})\.jsonl\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MIRROR_TEXT = re.compile(r"Unminute audit v1 chain=([0-9a-f]{32}) seq=([0-9]{1,12}) "
                         r"kind=([a-z_]{1,40}) sha256=([0-9a-f]{64})\Z")
INFO, WARNING = 0x0004, 0x0002       # EVENTLOG_INFORMATION_TYPE, EVENTLOG_WARNING_TYPE
EVENT_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"


class AuditError(ValueError):
    """A record that breaks its kind's rules (never written)."""


# -- field rules ---------------------------------------------------------------------

def _id(v):
    return isinstance(v, str) and HEX32.fullmatch(v) is not None


def _opt_id(v):
    return v is None or _id(v)


def _count(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < BIG


def _opt_count(v):
    return v is None or _count(v)


def _opt_int(v):
    return v is None or (isinstance(v, int) and not isinstance(v, bool) and abs(v) < BIG)


def _bool(v):
    return isinstance(v, bool)


def _hash(v):
    return isinstance(v, str) and HEX64.fullmatch(v) is not None


def _one_of(*values):
    return lambda v: (v is None or isinstance(v, str)) and v in values


def valid_version(v):
    return v is None or (isinstance(v, str) and diag.FIELDS["version"](v) == v)


_MEETING = {"meeting": _id}
_JOB = {"meeting": _id, "phase": _one_of("audio", "transcript"), "job": _id}
_HOLD = {"meeting": _id, "hold": _id, "scope": _one_of("audio", "transcript", "all")}
_HANDOFF = {"meeting": _id, "template": _one_of("built_in", "custom"), "names_replaced": _bool,
            "omitted": _count, "redactions": _count}

# kind: (Windows event ID, Event Log level, {field: rule}). Every field is
# required and no other is accepted. The table is in docs/WORKSPACE_DESIGN.md.
KINDS = {
    "chain_start": (1000, INFO, {"previous": _one_of("first_use", "missing", "unreadable")}),
    "app_start": (1001, INFO, {"version": valid_version, "schema": _count}),
    "locked": (1002, INFO, {"reason": _one_of("windows", "key", "protector", "missing"),
                            "winerror": _opt_int}),
    "damage_continued": (1003, INFO, {}),
    "meeting_imported": (1100, INFO, {"meeting": _id,
                                      "source": _one_of("file", "folder", "recording"),
                                      "folder": _opt_id, "recording": _opt_id,
                                      "seconds": _count, "delete_original": _bool}),
    "recording_started": (1110, INFO, {"recording": _id,
                                       "consent": _one_of("confirmed", "not_required"),
                                       "wording": _opt_count,
                                       "method": _one_of("app-window", None)}),
    "recording_ended": (1111, INFO, {"recording": _id,
                                     "result": _one_of("saved", "discarded", "recovered"),
                                     "seconds": _count}),
    "recording_deleted": (1112, INFO, {"recording": _id}),
    "transcript_reviewed": (1200, INFO, {"meeting": _id, "job": _id}),
    "final_saved": (1201, INFO, {"meeting": _id, "job": _id}),
    "meeting_discarded": (1210, INFO, dict(_MEETING)),
    "cleanup_started": (1300, INFO, dict(_JOB)),
    "cleanup_done": (1301, INFO, {**_JOB, "originals_deleted": _count,
                                  "originals_kept": _count}),
    "cleanup_confirmed": (1302, INFO, dict(_JOB)),
    "cleanup_retry": (1303, INFO, dict(_JOB)),
    "hold_placed": (1400, INFO, dict(_HOLD)),
    "hold_released": (1401, INFO, dict(_HOLD)),
    "hold_kept": (1402, INFO, dict(_HOLD)),
    "handoff_copied": (1500, INFO, {**_HANDOFF, "what": _one_of("combined", "prompt_only")}),
    "handoff_saved": (1501, INFO, dict(_HANDOFF)),
    "assistant_opened": (1502, INFO, {"meeting": _id, "assistant": _one_of("default", "policy")}),
    "folder_added": (1600, INFO, {"folder": _id, "existing": _bool}),
    "delete_authority": (1601, INFO, {"folder": _id, "granted": _bool}),
    "folder_removed": (1602, INFO, {"folder": _id, "had_authority": _bool}),
    "tail_damaged": (1900, WARNING, {"bytes": _count}),
    "write_failed": (1901, WARNING, {"count": _count}),
    "segment_dropped": (1902, WARNING, {"segment": _count, "first_seq": _opt_count,
                                        "last_seq": _count, "last_hash": _hash}),
    "mirror_gap": (1903, WARNING, {"first_seq": _count, "last_seq": _count}),
    "suppressed": (1904, WARNING, {"of_kind": None, "count": _count}),   # of_kind: LIMITED
}
# Written by the log itself or at start: never rate-limited.
INTERNAL = frozenset({"chain_start", "app_start", "locked", "tail_damaged", "write_failed",
                      "segment_dropped", "mirror_gap", "suppressed"})
# Written by the app for imports and deletions: never reduced to a count (the
# owner's decision of 21 Oct 2026, #111), so a page flood or a legitimate burst
# never hides which meeting was imported or deleted. Failed cleanups are not
# re-recorded every minute (workspace/cleanup.py run_job), so these stay paced.
APP_WRITTEN = frozenset({"meeting_imported", "recording_ended", "cleanup_started",
                         "cleanup_done"})
# The kinds the page drives: limited per kind a minute and together an hour.
LIMITED = tuple(k for k in KINDS if k not in INTERNAL and k not in APP_WRITTEN)
KINDS["suppressed"][2]["of_kind"] = _one_of(*LIMITED)
COMMON = ("v", "chain", "seq", "at", "kind", "prev")      # every record's own keys
if any(set(rules) & set(COMMON) for _, _, rules in KINDS.values()):
    raise RuntimeError("an audit field takes the name of a key every record has")


def check(kind, fields):
    """Raise AuditError unless fields are exactly kind's fields, each within
    its rule. The message names the kind and field only, never a value."""
    if not isinstance(kind, str) or kind not in KINDS:
        raise AuditError("unknown audit kind")
    rules = KINDS[kind][2]
    if set(fields) != set(rules):
        raise AuditError(f"{kind}: fields differ from its rules")
    for name, rule in rules.items():
        if not rule(fields[name]):
            raise AuditError(f"{kind}: {name} breaks its rule")


def _canonical(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def line_hash(line):
    """SHA-256 (hex) of a record's line bytes, without its newline."""
    return hashlib.sha256(line).hexdigest()


def mirror_text(record, sha):
    """The Event Log copy of a record: chain, seq, kind and the line's SHA-256
    only (the owner's decision, 21 Oct 2026: ids stay in the local file)."""
    return (f"Unminute audit v1 chain={record['chain']} seq={record['seq']} "
            f"kind={record['kind']} sha256={sha}")


def parse(line):
    """A record from its line bytes, or None unless it is a canonical line
    of a known kind whose every field keeps its rule."""
    try:
        text = line.decode("ascii")
        record = json.loads(text)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(record, dict) or _canonical(record) != text:
        return None
    if record.get("v") != FORMAT or not _id(record.get("chain")) \
            or not _count(record.get("seq")) or record["seq"] < 1 \
            or not _count(record.get("at")) or not _hash(record.get("prev")):
        return None
    try:
        check(record.get("kind"), {k: v for k, v in record.items() if k not in COMMON})
    except AuditError:
        return None
    return record


# -- reading the folder ----------------------------------------------------------------

def segments(folder):
    """[(number, path)], oldest first; [] if there is no folder. OSError if it
    cannot be listed."""
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return []
    found = []
    for name in names:
        m = SEGMENT.fullmatch(name)
        if m:
            found.append((int(m.group(1)), Path(folder) / name))
    return sorted(found)


def _segment_name(number):
    return f"audit-{number:06d}.jsonl"


def _read_end(path, limit=TAIL_BYTES):
    """(bytes, whole): the last `limit` bytes of path, and whether that is the
    whole file."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - limit))
        return f.read(), size <= limit


def read_tail(folder):
    """The end of the log, from at most TAIL_BYTES of each segment read
    newest first until a whole line is found. A dict: state "none" (no
    folder or no segment), "unreadable" (a segment or the last whole line
    could not be read), or "ok" with the chain, seq and head (the last whole
    line's hash). Every state has numbers (segment numbers, oldest first),
    total (bytes) and torn (bytes after the newest segment's last newline)."""
    try:
        segs = segments(folder)
        sizes = [p.stat().st_size for _, p in segs]
    except OSError:
        return {"state": "unreadable", "numbers": None, "total": 0, "torn": 0}
    out = {"state": "none", "numbers": [n for n, _ in segs], "total": sum(sizes), "torn": 0}
    newest = len(segs) - 1
    while newest >= 0 and sizes[newest] == 0:
        newest -= 1                            # an empty newest segment: made, nothing written
    for i in range(newest, -1, -1):
        try:
            data, whole = _read_end(segs[i][1])
        except OSError:
            return {**out, "state": "unreadable"}
        cut = data.rfind(b"\n")
        if i == newest:
            out["torn"] = len(data) - cut - 1
            if cut < 0 and not whole:
                return {**out, "state": "unreadable"}     # no line end in 64 KB: not ours
        if cut < 0:
            continue                           # only a cut-short line here: look further back
        start = data.rfind(b"\n", 0, cut) + 1
        if start == 0 and not whole:
            return {**out, "state": "unreadable"}         # a line longer than 64 KB
        record = parse(data[start:cut])
        if record is None:
            return {**out, "state": "unreadable"}
        return {**out, "state": "ok", "chain": record["chain"], "seq": record["seq"],
                "head": line_hash(data[start:cut])}
    if out["torn"]:
        return {**out, "state": "unreadable"}  # cut-short bytes and no whole line before them
    return out


def tail_seq(folder):
    """The newest record's sequence number from the tail only (no full
    check), or None. For --status."""
    tail = read_tail(folder)
    return tail.get("seq") if tail["state"] == "ok" else None


def _scan(folder):
    """Every segment as (number, [(record or None, line bytes)], torn bytes
    count), oldest first. OSError if the folder or a segment cannot be read."""
    out = []
    for number, path in segments(folder):
        data = path.read_bytes()
        pieces = data.split(b"\n")
        torn = pieces.pop()
        out.append((number, [(parse(p), p) for p in pieces], len(torn)))
    return out


def verify(folder):
    """Check the whole log. Content-free counts: result "ok", "broken",
    "empty" or "unreadable"; records, chains (chains started here), starts
    (those chain starts by their `previous`: "missing" or "unreadable" means
    the history before it is gone), segments, first_seq and last_seq (oldest
    and newest kept), head (the newest whole line's hash), broken_at and
    reason (where and why it breaks), tail ("unfinished": the newest segment
    ends in a line with no line end, which the app may be writing; checked
    as a cut-short line at the next open)."""
    out = {"result": "empty", "records": 0, "chains": 0,
           "starts": {"first_use": 0, "missing": 0, "unreadable": 0}, "segments": 0,
           "first_seq": None, "last_seq": None, "head": None, "broken_at": None,
           "reason": None, "tail": None}
    try:
        scanned = _scan(folder)
    except OSError:
        return {**out, "result": "unreadable"}
    out["segments"] = len(scanned)
    nonempty = [i for i, (_, lines, torn) in enumerate(scanned) if lines or torn]
    newest = nonempty[-1] if nonempty else -1
    first = chain = seq = head = expect_torn = None
    drops = []                                    # segment_dropped records, oldest first

    def broken(at, reason):
        return {**out, "result": "broken", "broken_at": at, "reason": reason}

    for i, (_, lines, torn) in enumerate(scanned):
        for record, line in lines:
            expected = seq + 1 if seq is not None else None
            if record is None:
                return broken(expected, "format")
            kind = record["kind"]
            if kind == "chain_start":
                if record["seq"] != 1 or record["prev"] != ZERO:
                    return broken(record["seq"], "chain_start")
                out["chains"] += 1
                out["starts"][record["previous"]] += 1
                expect_torn = None                # a new chain starts afresh
            else:
                if first is not None:             # the oldest kept record is checked below
                    if record["chain"] != chain or record["seq"] != expected:
                        return broken(expected, "sequence")
                    if record["prev"] != head:
                        return broken(record["seq"], "link")
                # tail_damaged exactly after a cut-short line, with its length.
                # The oldest kept record may be one whose cut-short segment was
                # dropped at the cap.
                torn_ok = (kind == "tail_damaged") == (expect_torn is not None) and (
                    expect_torn is None or record["bytes"] == expect_torn)
                if not torn_ok and not (first is None and expect_torn is None):
                    return broken(record["seq"], "torn")
                expect_torn = None
            if first is None:
                first = record
            chain, seq, head = record["chain"], record["seq"], line_hash(line)
            out["records"] += 1
            if kind == "segment_dropped":
                drops.append(record)
        if torn:
            if i == newest:
                out["tail"] = "unfinished"
            elif expect_torn is not None:
                return broken(seq + 1 if seq is not None else None, "torn")
            else:
                expect_torn = torn          # the next record: tail_damaged, or a new chain
    if first is None:
        return out                                # empty (or only an unfinished line)
    if expect_torn is not None:
        return broken(seq + 1, "torn")
    # Every segment before the oldest kept one was dropped at the cap, and
    # each drop is recorded (C5-4): the kept drop records name consecutive
    # segment numbers, the newest of them the one just before the oldest
    # kept segment (or there is none, and the oldest is segment 1). A
    # segment deleted by hand leaves a number no record names, also once a
    # later cap drop has run, or before a later chain's first record.
    for older, newer in zip(drops, drops[1:], strict=False):
        if newer["segment"] != older["segment"] + 1:
            return broken(newer["seq"], "front_truncated")
    if scanned[0][0] != (drops[-1]["segment"] + 1 if drops else 1):
        return broken(first["seq"], "front_truncated")
    drop = drops[-1] if drops else None
    if first["kind"] != "chain_start":
        # The newest drop record must name exactly the line before the
        # oldest kept one.
        if drop is None or drop["last_seq"] != first["seq"] - 1 \
                or drop["last_hash"] != first["prev"]:
            return broken(first["seq"], "front_truncated")
    return {**out, "result": "ok", "first_seq": first["seq"], "last_seq": seq, "head": head}


# -- the Event Log copy ----------------------------------------------------------------

def token_user():
    """(SID bytes, SID text) of this process's user; None if Windows cannot
    say; False off Windows. Used for the Event Log entries' user (the owner's
    decision, 21 Oct 2026) and by --health and --verify-audit to refuse a run
    as Local System."""
    import ctypes
    from ctypes import wintypes
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except AttributeError:                      # not Windows (tests on Linux)
        return False
    except OSError:
        return None
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE))
    advapi32.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    advapi32.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_wchar_p))
    advapi32.GetLengthSid.argtypes = (ctypes.c_void_p,)
    advapi32.GetLengthSid.restype = wintypes.DWORD
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008,   # TOKEN_QUERY
                                     ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        buffer = ctypes.create_string_buffer(size.value)
        if not size.value or not advapi32.GetTokenInformation(
                token, 1, buffer, size, ctypes.byref(size)):
            return None
        sid = ctypes.c_void_p.from_buffer(buffer).value   # TOKEN_USER.User.Sid
        text = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return None
        try:
            return ctypes.string_at(sid, advapi32.GetLengthSid(sid)), text.value
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


class EventLogMirror:
    """Writes each record's copy to the Windows Application log under
    `source` (unregistered for per-user installs: Windows uses the
    Application log and shows the text without a description). The process
    user's SID is passed (ReportEventW's lpUserSid), and Event Viewer shows
    it as the account: the writer supplies it, so it attributes the app's
    own entries but does not prove who wrote an entry. report() raises
    OSError when Windows refuses; the AuditLog counts that and tries again
    later."""

    def __init__(self, source=SOURCE):
        self.source = source
        self._handle = None
        self._sid = None
        self._api = None

    def _open(self):
        import ctypes
        from ctypes import wintypes
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.RegisterEventSourceW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
        advapi32.RegisterEventSourceW.restype = wintypes.HANDLE
        advapi32.ReportEventW.argtypes = (wintypes.HANDLE, wintypes.WORD, wintypes.WORD,
                                          wintypes.DWORD, ctypes.c_void_p, wintypes.WORD,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.LPCWSTR),
                                          ctypes.c_void_p)
        advapi32.ReportEventW.restype = wintypes.BOOL
        advapi32.DeregisterEventSource.argtypes = (wintypes.HANDLE,)
        handle = advapi32.RegisterEventSourceW(None, self.source)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        user = token_user()
        if user:
            self._sid = ctypes.create_string_buffer(user[0], len(user[0]))
        self._api, self._handle = advapi32, handle

    def report(self, event_id, level, text):
        import ctypes
        from ctypes import wintypes
        if self._handle is None:
            self._open()
        strings = (wintypes.LPCWSTR * 1)(text)
        sid = ctypes.cast(self._sid, ctypes.c_void_p) if self._sid is not None else None
        if not self._api.ReportEventW(self._handle, level, 0, event_id, sid, 1, 0, strings,
                                      None):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self._handle is not None:
            self._api.DeregisterEventSource(self._handle)
            self._handle = None


def system_program(name):
    """`name` in the Windows system folder (GetSystemDirectoryW), never found
    by name on PATH or in the current folder (as the tools start taskkill,
    #253). FileNotFoundError if it is not there."""
    import ctypes
    buf = ctypes.create_unicode_buffer(260)
    n = ctypes.windll.kernel32.GetSystemDirectoryW(buf, 260)
    if not 0 < n < 260:
        raise FileNotFoundError("system folder")
    exe = os.path.join(buf.value, name)
    if not os.path.isfile(exe):
        raise FileNotFoundError(name)
    return exe


def read_event_log(source=None, provider=SOURCE):
    """The provider's entries as XML text (wevtutil, with an <Events> root):
    from the local Application log, or from an exported .evtx file (source).
    OSError if it cannot be read."""
    exe = system_program("wevtutil.exe")
    windows = os.path.dirname(os.path.dirname(exe))
    query = f"/q:*[System[Provider[@Name='{provider}']]]"
    args = [exe, "qe", source or "Application", query, "/f:xml", "/e:Events"]
    if source:
        args.append("/lf:true")
    result = subprocess.run(args, capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
                            env={"SystemRoot": windows, "windir": windows,
                                 "PATH": os.path.dirname(exe)})
    if result.returncode != 0:
        raise OSError("wevtutil could not read the log")
    return result.stdout.decode("utf-8", errors="replace")


def _entries(xml_text, provider, sid=None):
    """([(chain, seq, kind, sha) or None for an entry that does not parse],
    the number of entries left out because they carry another account's SID
    or none: only with `sid`)."""
    import xml.etree.ElementTree as ET
    if re.search(r"<!(DOCTYPE|ENTITY)", xml_text, re.IGNORECASE):
        raise AuditError("a document type declaration is refused")
    root = ET.fromstring(xml_text)
    out, others = [], 0
    for event in root.iter():
        if event.tag not in (EVENT_NS + "Event", "Event"):
            continue
        names = [p.get("Name") for p in event.iter() if p.tag.endswith("Provider")]
        if names and names[0] != provider:
            continue
        if sid is not None:
            users = [s.get("UserID") for s in event.iter() if s.tag.endswith("Security")]
            if not users or users[0] != sid:
                others += 1
                continue
        found = None
        for data in event.iter():
            if data.tag.endswith("Data") and data.text:
                m = MIRROR_TEXT.fullmatch(data.text.strip())
                if m:
                    found = (m.group(1), int(m.group(2)), m.group(3), m.group(4))
                    break
        out.append(found)
    return out, others


def compare_event_log(folder, source=None, provider=SOURCE, read=None, sid=None):
    """Check the file against its Event Log copy (C5-2). sid: count only the
    entries carrying this account's SID (the local log, which every account
    on the computer writes to; --verify-audit passes the signed-in user's),
    and report the rest as other_accounts. Counts: missing_in_log (file
    records with no entry, outside a mirror_gap range, from the oldest file
    record that has one, in file order across chains, so records made up
    under a new chain count too), missing_in_file (entries of a chain in the
    file, from the oldest seq the file still holds, whose seq the file does
    not have, including any past its last record), mismatched (same seq,
    another line hash: an edited or rebuilt chain), unmatched (entries that
    do not parse: someone else wrote them under the source name); and,
    without making it differ, other_chains (well-formed entries naming a
    chain the file does not have: another workspace's, other devices' in an
    exported file, or an earlier chain whose segments were all dropped; read
    it with verify's result and starts) and other_accounts. result "match",
    "differs", "no_log" (no entry names a chain in the file) or "unreadable".
    Counts only: no line, id or SID."""
    out = {"result": "unreadable", "entries": 0, "missing_in_log": 0, "missing_in_file": 0,
           "mismatched": 0, "unmatched": 0, "other_chains": 0, "other_accounts": 0}
    try:
        text = (read or read_event_log)(source, provider)
        entries, out["other_accounts"] = _entries(text, provider, sid)
        scanned = _scan(folder)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {**out, "other_accounts": 0}
    out["entries"] = len(entries)
    if not entries:
        return {**out, "result": "no_log"}
    lines, gaps, order = {}, {}, []
    for _, records, _ in scanned:
        for record, line in records:
            if record is None:
                continue
            lines.setdefault(record["chain"], {})[record["seq"]] = line_hash(line)
            order.append((record["chain"], record["seq"]))
            if record["kind"] == "mirror_gap":
                gaps.setdefault(record["chain"], []).append(
                    (record["first_seq"], record["last_seq"]))
    logged, known = set(), False
    for entry in entries:
        if entry is None:
            out["unmatched"] += 1
            continue
        if entry[0] not in lines:
            out["other_chains"] += 1
            continue
        known = True
        chain, seq, _, sha = entry
        oldest = min(lines[chain])
        if seq < oldest:
            continue                              # before the file's oldest kept record
        if seq not in lines[chain]:
            out["missing_in_file"] += 1
        elif lines[chain][seq] != sha:
            out["mismatched"] += 1
        logged.add((chain, seq))
    start = next((i for i, key in enumerate(order) if key in logged), len(order))
    for chain, seq in order[start:]:
        if (chain, seq) not in logged and not any(
                a <= seq <= b for a, b in gaps.get(chain, ())):
            out["missing_in_log"] += 1
    if not known and not out["unmatched"]:
        return {**out, "result": "no_log"}            # no entry of this file's chains
    differs = any(out[k] for k in ("missing_in_log", "missing_in_file", "mismatched",
                                   "unmatched"))
    return {**out, "result": "differs" if differs else "match"}


# -- writing ---------------------------------------------------------------------------

class AuditLog:
    """The workspace's audit log (workspace\\audit). record() never raises
    for a write that fails (strict: a record that breaks its rules raises
    AuditError, as in tests; otherwise it is refused and one content-free
    incident is logged). mirror: an object with report(event_id, level,
    text), called on one background thread in record order, or None.
    new_workspace: this open made the workspace's root key (Store.new_root),
    so a missing audit folder starts the chain as first_use, not missing
    (C5-8). clock gives `at` (wall clock); timer the rate-limit windows and
    retries (monotonic)."""

    def __init__(self, folder, mirror=None, clock=time.time, new_workspace=False, log=None,
                 strict=False, timer=time.monotonic):
        self.folder = Path(folder)
        self.mirror = mirror
        self.clock, self.timer = clock, timer
        self.new_workspace = new_workspace
        self.log = log
        self.strict = strict
        self._lock = threading.RLock()
        self._loaded = False                 # the tail is read at the first record
        self._first_load = True
        self._pending = []                   # records owed before the next one (kind, fields)
        self._owed_drops = []                # segment_dropped records not yet written (kept
                                             # across a reload: the segment is already gone)
        self._chain = self._head = None
        self._seq = 0
        self._number = 0                     # the segment written to
        self._size = 0                       # its size
        self._total = 0                      # every segment's bytes
        self._failed = 0                     # records not written since the last good write
        self._closed = False
        # Rate limits (C5-5): per kind [window start, written, held back]; the hour.
        self._kinds = {}
        self._hour = [None, 0]
        # The Event Log copy (C5-12, C5-14).
        self._queue = queue.Queue()
        self._mirror_thread = None
        self._failing_since = None           # wall-clock time the mirror began to fail
        self._retry_at = None
        self._gap = None                     # [chain, first seq, last seq] without a copy
        self._inflight = None                # (chain, seq) the mirror thread is reporting
        self.stats = {"written": 0, "failed_writes": 0, "dropped_segments": 0,
                      "suppressed": 0, "refused": 0, "unmirrored": 0}

    # -- the public call ---------------------------------------------------------------

    def record(self, kind, **fields):
        """Write one record after the action it records has committed.
        Returns whether it was written."""
        try:
            check(kind, fields)
        except AuditError as exc:
            if self.strict:
                raise
            self.stats["refused"] += 1
            self._incident(exc)
            return False
        try:
            with self._lock:
                if self._closed:
                    return False
                now = self.timer()
                self._flush_suppressed(now)
                if kind in LIMITED and not self._allowed(kind, now):
                    return False
                return self._write(kind, fields)
        except Exception as exc:              # never break the action that was recorded
            self._incident(exc)
            return False

    def close(self):
        """Write held-back counts, then drain the mirror for at most
        CLOSE_DRAIN_S; what is left (and a copy still being reported) is
        recorded as a mirror_gap that covers itself and any record owed
        before it (written now rather than at the next start: no other state
        is kept)."""
        try:
            with self._lock:
                if self._closed:
                    return
                self._flush_suppressed(None)
        except Exception as exc:
            self._incident(exc)
        thread = self._mirror_thread
        alive = False
        if thread is not None:
            self._queue.put(None)
            thread.join(CLOSE_DRAIN_S)
            alive = thread.is_alive()
        try:
            with self._lock:
                self._closed = True
                left = []
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        left.append(item)
                gap = self._gap
                seqs = [item[1] for item in left if item[0] == self._chain]
                if gap is not None and gap[0] == self._chain:
                    seqs += [gap[1], gap[2]]
                if alive and self._inflight is not None and self._inflight[0] == self._chain:
                    seqs.append(self._inflight[1])    # it may yet fail, after this record
                if seqs:
                    self.stats["unmirrored"] += len(left)
                    first = min(seqs)
                    # last_seq is this record's own seq, worked out after any
                    # owed record (write_failed, tail_damaged) is written.
                    self._write("mirror_gap",
                                lambda: {"first_seq": first, "last_seq": self._seq + 1},
                                mirror=False)
                if alive:
                    self._queue.put(None)             # so the thread ends once Windows answers
        except Exception as exc:
            self._incident(exc)
        if self.mirror is not None and hasattr(self.mirror, "close") and (
                thread is None or not thread.is_alive()):
            try:
                self.mirror.close()
            except Exception:
                pass

    def summary(self):
        """For Copy diagnostics: counts and states only."""
        with self._lock:
            mirror = ("off" if self.mirror is None else
                      "failing" if self._failing_since is not None else "ok")
            return {"seq": self._seq if self._loaded else tail_seq(self.folder),
                    "mirror": mirror, "failing_since": self._failing_since,
                    **self.stats}

    # -- rate limits --------------------------------------------------------------------

    def _allowed(self, kind, now):
        hour = self._hour
        if hour[0] is None or now - hour[0] >= 3600:
            hour[0], hour[1] = now, 0
        state = self._kinds.setdefault(kind, [now, 0, 0])
        if now - state[0] >= 60:
            state[0], state[1] = now, 0
        if state[1] >= RATE_PER_KIND_MINUTE or hour[1] >= RATE_TOTAL_HOUR:
            state[2] += 1
            self.stats["suppressed"] += 1
            return False
        state[1] += 1
        hour[1] += 1
        return True

    def _flush_suppressed(self, now):
        """One suppressed record per kind once its minute is over and the
        hour's total is not spent (so a flood adds at most one record per
        kind an hour); every pending count at close (now None)."""
        spent = (self._hour[0] is not None and now is not None
                 and now - self._hour[0] < 3600 and self._hour[1] >= RATE_TOTAL_HOUR)
        for kind, state in self._kinds.items():
            if state[2] and (now is None or (now - state[0] >= 60 and not spent)):
                count, state[2] = state[2], 0
                self._write("suppressed", {"of_kind": kind, "count": count})

    # -- the file -------------------------------------------------------------------------

    def _load(self):
        """Read the tail and decide where the next record goes: on at the end
        of the newest segment, in a new segment after a cut-short line
        (tail_damaged first, C5-3), or a new chain (chain_start first)."""
        tail = read_tail(self.folder)
        if tail["numbers"] is None:
            raise OSError("the audit folder could not be listed")
        numbers, first = tail["numbers"], self._first_load
        self._first_load = False
        self._total = tail["total"]
        self._pending = []
        newest = numbers[-1] if numbers else 0
        if tail["state"] == "ok":
            self._chain, self._seq, self._head = tail["chain"], tail["seq"], tail["head"]
            if tail["torn"]:
                self._number, self._size = newest + 1, 0
                self._pending.append(("tail_damaged", {"bytes": tail["torn"]}))
            else:
                self._number = newest
                self._size = (self.folder / _segment_name(newest)).stat().st_size
        else:
            previous = ("unreadable" if tail["state"] == "unreadable" else
                        "first_use" if first and self.new_workspace else "missing")
            self._chain, self._seq, self._head = new_chain_id(), 0, ZERO
            self._number, self._size = newest + 1, 0
            self._pending.append(("chain_start", {"previous": previous}))
        self._loaded = True

    def _write(self, kind, fields, mirror=True):
        """Under the lock: any owed records, then this one (fields, or a
        callable giving them once the owed records are written). A failure is
        counted, and the tail is read again before the next write (a partial
        line then starts a new segment)."""
        try:
            if not self._loaded:
                self.folder.mkdir(exist_ok=True)   # never recreates a removed workspace
                self._load()
            while self._pending:
                self._append(*self._pending[0])
                self._pending.pop(0)
            self._write_owed_drops()
            if self._failed and kind != "write_failed":
                self._append("write_failed", {"count": self._failed})
                self._failed = 0
            self._append(kind, fields() if callable(fields) else fields, mirror)
        except OSError as exc:
            self._loaded = False
            self._failed += 1
            self.stats["failed_writes"] += 1
            self._incident(exc)
            return False
        try:
            self._cap()
        except OSError as exc:                    # the record above is written
            self._loaded = False
            self._incident(exc)
        return True

    def _append(self, kind, fields, mirror=True):
        record = {"v": FORMAT, "chain": self._chain, "seq": self._seq + 1,
                  "at": int(self.clock()), "kind": kind, "prev": self._head, **fields}
        line = _canonical(record).encode("ascii")
        if self._size and self._size + len(line) + 1 > SEGMENT_BYTES:
            self._number, self._size = self._number + 1, 0
        path = self.folder / _segment_name(self._number)
        unsynced = None
        with open(path, "ab") as f:
            f.write(line + b"\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError as exc:
                # The whole line is written; only forcing it to disk failed.
                # It stays a written record (the next one links to it, and
                # it gets its Event Log copy), never a failed write.
                unsynced = exc
        self._seq, self._head = record["seq"], line_hash(line)
        self._size += len(line) + 1
        self._total += len(line) + 1
        self.stats["written"] += 1
        if mirror and self.mirror is not None:
            event_id, level, _ = KINDS[kind]
            if kind == "chain_start" and fields["previous"] != "first_use":
                level = WARNING              # the history before it is gone (C5 design 5)
            self._queue.put((self._chain, self._seq, event_id, level,
                             mirror_text(record, self._head)))
            if self._mirror_thread is None:
                self._mirror_thread = threading.Thread(target=self._mirror_loop, daemon=True,
                                                       name="audit-mirror")
                self._mirror_thread.start()
        if unsynced is not None:
            self._incident(unsynced)

    def _cap(self):
        """Past MAX_BYTES, delete the oldest segments (never the one being
        written) and record each: segment_dropped names its number and the
        line before the next kept record, so verify can tell a drop from a
        hand deletion (C5-4). A segment with no whole line (only a line cut
        short, or nothing) is dropped too, naming the whole line before the
        next segment's first record. A drop record that cannot be written
        yet is owed and written before the next record."""
        while self._total > MAX_BYTES:
            try:
                segs = [s for s in segments(self.folder) if s[0] != self._number]
                if not segs:
                    return
                number, path = segs[0]
                lines = path.read_bytes().split(b"\n")
                lines.pop()                       # a cut-short line, or nothing
                records = [(parse(b), b) for b in lines if b]
                if records:
                    first, (last, last_line) = records[0][0], records[-1]
                    fields = {"first_seq": first["seq"] if first else None,
                              "last_seq": last["seq"] if last else 0,
                              "last_hash": line_hash(last_line)}
                else:
                    last_seq, last_hash = self._before_segment(number)
                    fields = {"first_seq": None, "last_seq": last_seq, "last_hash": last_hash}
                size = path.stat().st_size
                path.unlink()
            except OSError as exc:
                self._incident(exc)               # tried again after the next record
                return
            self._total -= size
            self.stats["dropped_segments"] += 1
            self._owed_drops.append({"segment": number, **fields})
            self._write_owed_drops()

    def _before_segment(self, number):
        """(seq, hash) of the whole line just before segment `number`'s
        successors, from the first whole record after it: its seq - 1 and
        its prev ((0, ZERO) when that record starts a chain or none is
        found)."""
        for later, path in segments(self.folder):
            if later <= number:
                continue
            data = path.read_bytes()
            cut = data.find(b"\n")
            if cut < 0:
                continue                          # only a line cut short here, too
            record = parse(data[:cut])
            if record is not None:
                return record["seq"] - 1, record["prev"]
            break
        return 0, ZERO

    def _write_owed_drops(self):
        while self._owed_drops:
            self._append("segment_dropped", self._owed_drops[0])
            self._owed_drops.pop(0)

    # -- the mirror thread -----------------------------------------------------------------

    def _mirror_loop(self):
        """The mirror's state (_gap, _failing_since, _inflight) changes only
        under the log's lock, which close() reads it under; report() itself
        runs outside the lock, so writers never wait for Windows."""
        while True:
            item = self._queue.get()
            if item is None:
                return
            chain, seq, event_id, level, text = item
            with self._lock:
                if self._failing_since is not None and self.timer() < self._retry_at:
                    self._add_gap(chain, seq)
                    continue
                self._inflight = (chain, seq)
            try:
                self.mirror.report(event_id, level, text)
            except Exception as exc:
                with self._lock:
                    self._inflight = None
                    if self._failing_since is None:
                        self._failing_since = int(self.clock())
                        self._incident(exc)
                    self._retry_at = self.timer() + MIRROR_RETRY_S
                    self._add_gap(chain, seq)
                continue
            with self._lock:
                self._inflight = None
                gap = None
                if self._failing_since is not None:
                    gap, self._gap, self._failing_since = self._gap, None, None
            if gap is not None and gap[0] == chain:
                self.record("mirror_gap", first_seq=gap[1], last_seq=gap[2])

    def _add_gap(self, chain, seq):
        self.stats["unmirrored"] += 1
        if self._gap is None or self._gap[0] != chain:
            self._gap = [chain, seq, seq]
        else:
            self._gap[2] = seq

    def _incident(self, exc):
        """One content-free incident: the area and the error's type name."""
        try:
            if self.log is not None:
                self.log.incident("audit", exc)
            else:
                diag.incident("audit", exc)
        except Exception:
            pass


def new_chain_id():
    import uuid
    return uuid.uuid4().hex
