# tools/session_state.py
# Whether this Windows session is locked (#507), for the release checks that
# drive a real window: tools/perf_check.py, tools/workspace_ui_check.py,
# tools/accessibility_check.py and tools/packaged_check.py. A locked session
# cannot show a window as a person sees it, nor open the clipboard
# (OpenClipboard fails with access denied), so a window step run while it is
# locked proves nothing either way.
#
# The test: the session's own lock flag, from WTSQuerySessionInformationW with
# WTSSessionInfoEx (WTSINFOEXW, Level 1, SessionFlags). Only when that cannot
# be read does it fall back to the input desktop's name (locked unless it is
# "Default"), which on Windows 11 26H2 (10.0.26300) still said "Default" while
# the session was locked (2 Nov 2026, #507). Microsoft documents that on
# Windows 7 and Windows Server 2008 R2 the LOCK and UNLOCK values are swapped;
# the app supports Windows 10 and 11 only.
#
# Content-free (AGENTS.md P3): only the flag, the desktop's name compared
# with "Default", and the session id compared with this process's are read.
# The structure also holds the user and domain names; they are never read,
# and nothing here is printed or recorded but yes-or-no answers and counts.
#
# Watch keeps the record a check writes in its report ("session"), and stops
# the check (SessionLocked) when a reading before or after a window step
# finds the session locked, or cannot be read. tools/evidence.py refuses a
# report whose record shows a lock, or that has none (its SESSION_RECORD,
# which a test keeps equal to RECORD below). A lock that starts and ends
# between two readings is not seen.
#
# Release evidence counts only readings of the WTS flag (the owner's
# decision DEC-084, 2 Nov 2026, #514): tools/evidence.py also refuses a report with
# any reading made from the input desktop's name. A development run may still
# use that fallback, and Watch says so (FALLBACK) the first time it does.

import struct
import sys
import threading

# The record's version: tools/evidence.py's SESSION_RECORD is kept equal to it
# (tests/test_session_state.py). Raise both together when the test changes.
RECORD = 1

# wtsapi32.h (Microsoft Learn: WTS_INFO_CLASS, WTSINFOEXW, WTSINFOEX_LEVEL1_W).
WTS_CURRENT_SERVER_HANDLE = None
WTS_SESSION_INFO_EX = 25            # WTSSessionInfoEx, the 26th WTS_INFO_CLASS member
WTS_SESSIONSTATE_LOCK = 0
WTS_SESSIONSTATE_UNLOCK = 1
WTS_SESSIONSTATE_UNKNOWN = -1       # 0xFFFFFFFF, read as the LONG it is
# WTSINFOEXW is a DWORD Level, then a union holding WTSINFOEX_LEVEL1_W, whose
# LARGE_INTEGER members align it to 8 bytes: SessionId (ULONG) at 8,
# SessionState at 12, SessionFlags (LONG) at 16.
LEVEL_OFFSET, SESSION_ID_OFFSET, FLAGS_OFFSET = 0, 8, 16
READ_BYTES = FLAGS_OFFSET + 4

DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2

WTS, DESKTOP = "wts", "input desktop"


# What a stop says: a lock seen as one, or a state that could not be read
# (which stops the check too, as it might be a lock). The docs quote the
# shared start, "the session locked".
LOCKED, UNREAD = "the session locked", "the session locked, or its state could not be read"


# What a check prints the first time a reading used the input desktop's name
# (#514): the run goes on, but its report is not release evidence.
FALLBACK = ("NOTE: the session's lock state was read from the input desktop's name, not "
            "Windows' session lock flag (WTS) ({where}). That fallback can miss a lock "
            "(#507): this run is fine for development, but tools/evidence.py check refuses "
            "its report as release evidence (#514).")


def _say(text):
    print(text, file=sys.stderr, flush=True)


class SessionLocked(RuntimeError):
    """The session was locked, or its state could not be read, at a step."""

    def __init__(self, where, unread=False):
        super().__init__(f"{UNREAD if unread else LOCKED} ({where})")
        self.where = where


def flags_from(data, session_id):
    """SessionFlags from the first READ_BYTES bytes of a WTSINFOEXW, or None
    when they are not a Level 1 record of session_id with a known flag."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < READ_BYTES:
        return None
    level, = struct.unpack_from("<I", data, LEVEL_OFFSET)
    sid, = struct.unpack_from("<I", data, SESSION_ID_OFFSET)
    flags, = struct.unpack_from("<i", data, FLAGS_OFFSET)
    if level != 1 or sid != session_id:
        return None
    return flags if flags in (WTS_SESSIONSTATE_LOCK, WTS_SESSIONSTATE_UNLOCK) else None


def _wts_bytes():
    """(the first READ_BYTES bytes of this session's WTSINFOEXW, its session
    id), or None when they cannot be read."""
    import ctypes
    from ctypes import wintypes
    k32, wts = ctypes.WinDLL("kernel32"), ctypes.WinDLL("wtsapi32")
    k32.GetCurrentProcessId.restype = wintypes.DWORD
    k32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    k32.ProcessIdToSessionId.restype = wintypes.BOOL
    wts.WTSQuerySessionInformationW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD)]
    wts.WTSQuerySessionInformationW.restype = wintypes.BOOL
    wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    session = wintypes.DWORD()
    if not k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(session)):
        return None
    buffer, size = ctypes.c_void_p(), wintypes.DWORD()
    if not wts.WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE, session.value,
                                           WTS_SESSION_INFO_EX, ctypes.byref(buffer),
                                           ctypes.byref(size)) or not buffer.value:
        return None
    try:
        if size.value < READ_BYTES:
            return None
        return ctypes.string_at(buffer.value, READ_BYTES), session.value
    finally:
        wts.WTSFreeMemory(buffer)


def wts_flags():
    """WTS_SESSIONSTATE_LOCK or _UNLOCK for this process's session, or None
    when Windows does not say (an error, or WTS_SESSIONSTATE_UNKNOWN)."""
    try:
        read = _wts_bytes()
    except (OSError, AttributeError, ValueError):
        return None
    return flags_from(*read) if read else None


def input_desktop():
    """The input desktop's name, "" when it cannot be opened (the secure
    desktop of a locked session, on older Windows), or None when the call
    is not there at all."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        user32.GetUserObjectInformationW.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        desktop = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not desktop:
            return ""
        try:
            name = ctypes.create_unicode_buffer(64)
            if not user32.GetUserObjectInformationW(desktop, UOI_NAME, name, ctypes.sizeof(name),
                                                    ctypes.byref(wintypes.DWORD())):
                return ""
            return name.value
        finally:
            user32.CloseDesktop(desktop)
    except (OSError, AttributeError):
        return None


def read(flags=None, desktop=None):
    """{"locked": True, False or None (not known), "how": WTS, DESKTOP or
    None}. The WTS flag first (flags: wts_flags); the input desktop's name
    (desktop: input_desktop) only when there is none."""
    flags, desktop = flags or wts_flags, desktop or input_desktop
    value = flags()
    if value == WTS_SESSIONSTATE_LOCK:
        return {"locked": True, "how": WTS}
    if value == WTS_SESSIONSTATE_UNLOCK:
        return {"locked": False, "how": WTS}
    name = desktop()
    if name is None:
        return {"locked": None, "how": None}
    return {"locked": name.lower() != "default", "how": DESKTOP}


class Watch:
    """The session's state at each window step of a check, and the record of
    it for the check's report. check(where) reads it and raises
    SessionLocked unless it is unlocked; reading(where) only records it.
    Once a reading has found it locked or unread, the watch stays stopped.
    The first reading made from the input desktop's name, here or in a
    merged record, is said (FALLBACK, through say: stderr by default)."""

    def __init__(self, reader=None, say=None):
        self._read, self._lock = reader or (lambda: read()), threading.Lock()
        self._say = say or _say
        self.readings = self.by_wts = self.by_desktop = self.unread = 0
        self.locked_at = None
        self.unread_stop = False            # the stop came from a state not read as locked
        self.step = "a window step"         # the check names its steps (a fixed name)
        self.fallback_said = False

    def _fallback(self, where):
        """Say, once a run, that a reading used the input desktop's name."""
        with self._lock:
            if self.fallback_said:
                return
            self.fallback_said = True
        self._say(FALLBACK.format(where=where))

    @property
    def stopped(self):
        return self.locked_at is not None

    def reading(self, where):
        state = self._read()
        with self._lock:
            self.readings += 1
            if state.get("how") == WTS:
                self.by_wts += 1
            elif state.get("how") == DESKTOP:
                self.by_desktop += 1
            else:
                self.unread += 1
            if state.get("locked") is not False and self.locked_at is None:
                self.locked_at = str(where)
                self.unread_stop = state.get("locked") is not True
        if state.get("how") == DESKTOP:
            self._fallback(where)
        return state

    def check(self, where):
        """Raises SessionLocked unless the session is unlocked now, and has
        been at every reading before."""
        self.reading(where)
        if self.stopped:
            raise SessionLocked(self.locked_at, self.unread_stop)

    def merge(self, record):
        """Adds another process's record (a check's own child process) to
        this one. None (a child that wrote no report, which fails the check
        on its own) adds nothing: the readings around the child still count.
        Anything else that is not a record counts as one unread reading."""
        if record is None:
            return
        with self._lock:
            if not valid(record):
                self.readings += 1
                self.unread += 1
                if self.locked_at is None:
                    self.locked_at = "no record from the check's own child process"
                    self.unread_stop = True
                return
            self.readings += record["readings"]
            self.by_wts += record["wts"]
            self.by_desktop += record["input_desktop"]
            self.unread += record["unread"]
            if record["locked"] and self.locked_at is None:
                self.locked_at = record["locked_at"] or "in the check's own child process"
                # The child's record has counts only: any unread reading
                # there may be what stopped it.
                self.unread_stop = record["unread"] > 0
        if record["input_desktop"] > 0:     # the child's own note went to its captured output
            self._fallback("in the check's own child process")

    def record(self):
        """The report's "session" block: counts and the step's name only."""
        with self._lock:
            return {"record": RECORD, "readings": self.readings, "wts": self.by_wts,
                    "input_desktop": self.by_desktop, "unread": self.unread,
                    "locked": self.stopped, "locked_at": self.locked_at}


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def valid(record):
    """A session block of this version, with every field of the right type."""
    return (isinstance(record, dict) and record.get("record") == RECORD
            and all(_count(record.get(k)) for k in ("readings", "wts", "input_desktop",
                                                      "unread"))
            and isinstance(record.get("locked"), bool)
            and (record.get("locked_at") is None or isinstance(record.get("locked_at"), str)))


STOPPED = ("STOPPED: {what} ({where}), so this run is neither a pass nor a "
           "failure. Keep the session unlocked (an inactivity lock set by policy locks it "
           "too; docs/RELEASING.md) and run the check again.")


def stopped_message(watch):
    return STOPPED.format(what=UNREAD if watch.unread_stop else LOCKED, where=watch.locked_at)
