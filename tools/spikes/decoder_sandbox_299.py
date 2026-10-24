# tools/spikes/decoder_sandbox_299.py
# SPIKE for #299, not shipped and not used by the app: prototypes of the two
# ways to stop a compromised decoder reading the workspace. The owner chooses
# the approach (AGENTS.md, "Stop and ask"); until then nothing here is wired
# into workspace/. Run only against scratch folders and a scratch copy of a
# runtime, never %LOCALAPPDATA%\Unminute, an install folder or Program Files.
#
#   Option 1: a Medium mandatory label with no-read-up (and no-write-up),
#             inherited by files and folders, on the Unminute data folder:
#             label_no_read_up(), read_label().
#   Option 2: the decoder in an AppContainer with no capabilities, given the
#             recording as an open read-only handle: create_profile() and
#             delete_profile() (Windows starts no container process without a
#             registered profile), appcontainer_sid(), grant_appcontainer_read()
#             (grant ALL APPLICATION PACKAGES, S-1-15-2-1: an ACE for one
#             container's own SID made a folder unreadable to Low-integrity
#             processes outside any container in the spike's measurements),
#             start_in_appcontainer(), and BOOT (the decoder's entry point for
#             a handle instead of a path).
#
# The measurements and the options for the owner are on #299.
#
# Stdlib only (ctypes). Windows only.

import ctypes
import json
import os
import struct
import sys

if sys.platform == "win32":
    from ctypes import wintypes

    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _env = ctypes.WinDLL("userenv", use_last_error=True)

    _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD)]
    _adv.GetSecurityDescriptorSacl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL)]
    _adv.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p]
    _adv.SetNamedSecurityInfoW.restype = wintypes.DWORD
    _adv.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    _adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD)]
    _adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    _adv.FreeSid.argtypes = [ctypes.c_void_p]
    _k32.LocalFree.argtypes = [ctypes.c_void_p]
    _k32.LocalFree.restype = ctypes.c_void_p
    _env.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    _env.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    _env.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p)]
    _env.CreateAppContainerProfile.restype = ctypes.c_long
    _env.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    _env.DeleteAppContainerProfile.restype = ctypes.c_long

SE_FILE_OBJECT = 1
LABEL_SECURITY_INFORMATION = 0x10
SDDL_REVISION_1 = 1
# Medium label, no read up and no write up, inherited by files (OI) and
# folders (CI). A process below Medium (the decoder at Low, or in an
# AppContainer) can then neither read nor write anything under the folder.
NO_READ_UP_SDDL = "S:(ML;OICI;NRNW;;;ME)"


# -- Option 1: the no-read-up label ----------------------------------------------------

def label_no_read_up(path, sddl=NO_READ_UP_SDDL):
    """Put the label on `path` (a folder). SetNamedSecurityInfo also applies
    it to what is already inside, through inheritance. A Medium process may
    set a label up to Medium on an object it may change the owner of."""
    sd = ctypes.c_void_p()
    if not _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, SDDL_REVISION_1, ctypes.byref(sd), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted, sacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not _adv.GetSecurityDescriptorSacl(sd, ctypes.byref(present), ctypes.byref(sacl),
                                              ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        err = _adv.SetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION,
                                         None, None, None, sacl)
        if err:
            raise ctypes.WinError(err)
    finally:
        _k32.LocalFree(sd)


def read_label(path):
    """The mandatory label of `path` as SDDL ("S:(ML;...)"), or "" for none
    (which Windows treats as Medium, no write up)."""
    sd = ctypes.c_void_p()
    err = _adv.GetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION,
                                     None, None, None, None, ctypes.byref(sd))
    if err:
        raise ctypes.WinError(err)
    try:
        text = wintypes.LPWSTR()
        if not _adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                sd, SDDL_REVISION_1, LABEL_SECURITY_INFORMATION, ctypes.byref(text), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            _k32.LocalFree(text)
    finally:
        _k32.LocalFree(sd)


# -- Option 2: an AppContainer with no capabilities ------------------------------------

CONTAINER_NAME = "Unminute.Spike299.Decoder"


def appcontainer_sid(name=CONTAINER_NAME):
    """The AppContainer SID for `name`, as a string. Derived only: no
    profile is created and nothing is written to the registry."""
    sid = ctypes.c_void_p()
    hr = _env.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
    if hr:
        raise OSError(hr & 0xFFFFFFFF, "DeriveAppContainerSidFromAppContainerName")
    try:
        text = wintypes.LPWSTR()
        if not _adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            _k32.LocalFree(text)
    finally:
        _adv.FreeSid(sid)


def create_profile(name=CONTAINER_NAME):
    """Register the AppContainer profile for `name` for this user. Windows
    will not start a process in an AppContainer that has no profile
    (CreateProcess fails with ERROR_FILE_NOT_FOUND). The profile is a key
    under HKCU\\Software\\Classes\\Local Settings and a folder under
    %LOCALAPPDATA%\\Packages; no administrator rights are needed. Returns
    True if it was made now, False if it already existed."""
    sid = ctypes.c_void_p()
    hr = _env.CreateAppContainerProfile(name, name, name, None, 0, ctypes.byref(sid))
    if hr == 0:
        _adv.FreeSid(sid)
        return True
    if hr & 0xFFFFFFFF == 0x800700B7:                  # HRESULT_FROM_WIN32(ALREADY_EXISTS)
        return False
    raise OSError(hr & 0xFFFFFFFF, "CreateAppContainerProfile")


def delete_profile(name=CONTAINER_NAME):
    """Remove the profile create_profile() made (its key and folder)."""
    hr = _env.DeleteAppContainerProfile(name)
    if hr:
        raise OSError(hr & 0xFFFFFFFF, "DeleteAppContainerProfile")


def grant_appcontainer_read(folder, sid):
    """Read and execute for `sid` on `folder` and everything in it (an
    inherited ACE, as an installer would add). Returns icacls's exit code."""
    import subprocess
    return subprocess.run(["icacls", str(folder), "/grant", f"*{sid}:(OI)(CI)(RX)", "/Q"],
                          capture_output=True, text=True).returncode


if sys.platform == "win32":
    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _SECURITY_CAPABILITIES(ctypes.Structure):
        _fields_ = [("AppContainerSid", ctypes.c_void_p),
                    ("Capabilities", ctypes.POINTER(_SID_AND_ATTRIBUTES)),
                    ("CapabilityCount", wintypes.DWORD), ("Reserved", wintypes.DWORD)]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    _k32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    _k32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_void_p]
    _k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    _k32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
        wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
        ctypes.POINTER(_PROCESS_INFORMATION)]
    _k32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
                                ctypes.POINTER(_SECURITY_ATTRIBUTES), wintypes.DWORD]
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(_SECURITY_ATTRIBUTES), wintypes.DWORD,
                                 wintypes.DWORD, wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.ResumeThread.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]

PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x1
OPEN_EXISTING = 3
INVALID_HANDLE = wintypes.HANDLE(-1).value if sys.platform == "win32" else None


def _pipe():
    """(read, write) handles, both inheritable (the caller picks which end
    goes in the child's handle list)."""
    sa = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True)
    r, w = wintypes.HANDLE(), wintypes.HANDLE()
    if not _k32.CreatePipe(ctypes.byref(r), ctypes.byref(w), ctypes.byref(sa), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return r.value, w.value


def open_for_child(path):
    """A read-only, inheritable handle to `path`, opened by this (Medium)
    process. The child can read through it whatever its own token allows."""
    sa = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True)
    h = _k32.CreateFileW(str(path), GENERIC_READ, FILE_SHARE_READ, ctypes.byref(sa),
                         OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE or not h:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


class Child:
    """A process started in the AppContainer: pid, the process handle, and
    the parent's ends of its stdin, stdout and stderr as Python files."""

    def __init__(self, pi, stdin, stdout, stderr):
        self.pid = pi.dwProcessId
        self.handle = pi.hProcess
        self.stdin, self.stdout, self.stderr = stdin, stdout, stderr

    def wait(self, timeout_s):
        _k32.WaitForSingleObject(self.handle, int(timeout_s * 1000))
        code = wintypes.DWORD()
        _k32.GetExitCodeProcess(self.handle, ctypes.byref(code))
        return code.value

    def kill(self):
        _k32.TerminateProcess(self.handle, 1)

    def close(self):
        for f in (self.stdin, self.stdout, self.stderr):
            try:
                f.close()
            except OSError:
                pass
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None


def start_in_appcontainer(argv, cwd, env, extra_handles=(), name=CONTAINER_NAME, contain=None,
                          appcontainer=True):
    """Start argv in the AppContainer `name` with no capabilities, suspended;
    call contain(pid) (for example jobs.contain) before it runs a line; then
    resume it. Only its three pipe ends and extra_handles are inherited.
    appcontainer=False starts it the same way without the container (for
    comparison)."""
    import msvcrt
    import subprocess
    sid = ctypes.c_void_p()
    hr = _env.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
    if hr:
        raise OSError(hr & 0xFFFFFFFF, "DeriveAppContainerSidFromAppContainerName")
    in_r, in_w = _pipe()
    out_r, out_w = _pipe()
    err_r, err_w = _pipe()
    inherit = [in_r, out_w, err_w, *extra_handles]
    caps = _SECURITY_CAPABILITIES(sid, None, 0, 0)
    size = ctypes.c_size_t()
    _k32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
    attrs = ctypes.create_string_buffer(size.value)
    pi = _PROCESS_INFORMATION()
    try:
        if not _k32.InitializeProcThreadAttributeList(attrs, 2, 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        handles = (wintypes.HANDLE * len(inherit))(*inherit)
        updates = [(PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles, ctypes.sizeof(handles))]
        if appcontainer:
            updates.append((PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, ctypes.byref(caps),
                            ctypes.sizeof(caps)))
        for attr, value, n in updates:
            if not _k32.UpdateProcThreadAttribute(attrs, 0, attr, value, n, None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        si = _STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(si)
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput = in_r, out_w
        si.StartupInfo.hStdError = err_w
        si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
        block = "".join(f"{k}={v}\0" for k, v in env.items()) + "\0"
        env_buf = ctypes.create_unicode_buffer(block, len(block))
        cmd = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        if not _k32.CreateProcessW(argv[0], cmd, None, None, True,
                                   EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED
                                   | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                                   env_buf, str(cwd), ctypes.byref(si), ctypes.byref(pi)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _k32.DeleteProcThreadAttributeList(attrs)
        _adv.FreeSid(sid)
        for h in (in_r, out_w, err_w):
            _k32.CloseHandle(h)
    try:
        if contain is not None:
            contain(pi.dwProcessId)
        _k32.ResumeThread(pi.hThread)
    except BaseException:
        _k32.TerminateProcess(pi.hProcess, 1)
        raise
    finally:
        _k32.CloseHandle(pi.hThread)
    to_file = lambda h, mode, flags: os.fdopen(msvcrt.open_osfhandle(h, flags), mode)  # noqa: E731
    return Child(pi, to_file(in_w, "wb", 0), to_file(out_r, "rb", os.O_RDONLY),
                 to_file(err_r, "rb", os.O_RDONLY))


# The decoder's entry point for option 2: as decode_child.BOOT and main(), but
# the request names an inherited read-only handle instead of a path, which
# the AppContainer could not open. Sent with -c; argv-free.
BOOT = r"""
import json, os, struct, sys
sys.path[:] = json.loads(sys.stdin.readline())
from workspace import decode_child as d
out = os.fdopen(os.dup(1), "wb")
def send(kind, payload=b""):
    out.write(kind + struct.pack("<I", len(payload)) + payload)
    out.flush()
for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[var] = "1"
try:
    level = d.lower_integrity()
    from workspace import offline_guard
    offline_guard.disable_telemetry()
    offline_guard.install()
    import av, numpy
except Exception as exc:
    sys.stderr.write("setup: %r\n" % (exc,)); sys.stderr.flush()
    send(b"E", bytes([d.E_SETUP])); raise SystemExit
send(b"R", struct.pack("<I", level or 0))
req = json.loads(sys.stdin.buffer.readline())
import msvcrt
fh = os.fdopen(msvcrt.open_osfhandle(int(req["handle"]), os.O_RDONLY | os.O_BINARY), "rb")
try:
    d._decode(fh, os.fstat(fh.fileno()).st_size, None, send, d.MAX_DECODED_S * d.SAMPLE_RATE)
    send(b"D")
except d.DecodeFailed as exc:
    send(b"E", bytes([exc.code]))
except Exception as exc:
    sys.stderr.write("decode: %r\n" % (exc,)); sys.stderr.flush()
    send(b"E", bytes([d.E_UNREADABLE]))
"""


def read_frame(stream):
    """One decoder frame (kind, payload), or (None, b"") at the end."""
    head = stream.read(5)
    if len(head) < 5:
        return None, b""
    kind, n = head[:1], struct.unpack("<I", head[1:])[0]
    return kind, stream.read(n)


def send_line(stream, value):
    stream.write((json.dumps(value) + "\n").encode("utf-8"))
    stream.flush()
