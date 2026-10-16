# workspace/jobs.py
# Windows Job Objects for contained child processes (#111 slice A3).
#
# A job limits what a process can do even if code inside it misbehaves, for
# example a decoder flaw triggered by a crafted recording:
#   - memory: the process is stopped if it commits more than the limit;
#   - no child processes: it cannot start cmd.exe or anything else;
#   - no clipboard, desktop switching, global atoms, display or system
#     settings, and no Windows exit;
#   - killed when the job handle closes, so it never outlives the app;
#   - an unhandled exception ends it silently instead of showing a dialog.
# Stdlib only (ctypes over kernel32 and user32). Elsewhere contain() returns None.

import ctypes
import sys

JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_UILIMIT_ALL = 0x000000FF          # handles, clipboard read/write, system
                                             # parameters, display settings, global
                                             # atoms, desktop, exit Windows
JobObjectBasicUIRestrictions = 4
JobObjectExtendedLimitInformation = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class JobError(OSError):
    """The process could not be contained (it must not be given work)."""


if sys.platform == "win32":
    from ctypes import wintypes

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IO),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _UI(ctypes.Structure):
        _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]

    # Private handles: prototypes set here do not affect other ctypes users.
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL


class Job:
    """An open job handle. close() (or leaving a with block) kills every
    process still in the job."""

    def __init__(self, handle):
        self.handle = handle

    def close(self):
        handle, self.handle = self.handle, None
        if handle:
            _k32.CloseHandle(handle)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def contain(pid, memory_bytes):
    """Put process `pid` in a new job with the limits above. Returns a Job,
    or None when not on Windows. Raises JobError if any step fails: the
    caller must then stop the process rather than give it work."""
    if sys.platform != "win32":
        return None
    handle = _k32.CreateJobObjectW(None, None)
    if not handle:
        raise JobError(ctypes.get_last_error(), "CreateJobObject failed")
    job = Job(handle)
    try:
        info = _EXTENDED()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        info.BasicLimitInformation.ActiveProcessLimit = 1
        info.ProcessMemoryLimit = int(memory_bytes)
        if not _k32.SetInformationJobObject(handle, JobObjectExtendedLimitInformation,
                                            ctypes.byref(info), ctypes.sizeof(info)):
            raise JobError(ctypes.get_last_error(), "job limits could not be set")
        ui = _UI(JOB_OBJECT_UILIMIT_ALL)
        if not _k32.SetInformationJobObject(handle, JobObjectBasicUIRestrictions,
                                            ctypes.byref(ui), ctypes.sizeof(ui)):
            raise JobError(ctypes.get_last_error(), "job UI limits could not be set")
        process = _k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if not process:
            raise JobError(ctypes.get_last_error(), "the process could not be opened")
        try:
            if not _k32.AssignProcessToJobObject(handle, process):
                raise JobError(ctypes.get_last_error(), "the process could not be contained")
        finally:
            _k32.CloseHandle(process)
    except BaseException:
        job.close()
        raise
    return job
