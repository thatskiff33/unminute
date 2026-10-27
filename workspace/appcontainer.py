# workspace/appcontainer.py
# The decoder's AppContainer (#299; docs/SECURITY_MODEL.md T2 and T3).
#
# The decoder (workspace/decode_child.py) runs in a Windows AppContainer with
# no capabilities. Windows then gives it no access to the user's files or the
# workspace, and no network at all (internet, private network or loopback):
# AppContainer isolation grants each of those only through a capability, and
# this container has none. It can read what ALL APPLICATION PACKAGES may read
# (S-1-15-2-1: the runtime and the app code, granted by the installer on its
# versions folder, and by Windows on Program Files), the recording through the
# one read-only handle the app opens and passes to it, and its own pipes.
#
# Windows starts no process in an AppContainer without a registered profile, so
# the app makes one for the signed-in user when it first decodes (PROFILE: a
# key under HKCU\Software\Classes\Local Settings and a folder under
# %LOCALAPPDATA%\Packages; no administrator rights). The uninstaller and
# offboarding remove it (delete_profile). Nothing falls back to a decoder
# outside the container: when a step fails, the caller refuses the import (the
# owner's decision of 24 October 2026, #299).
#
# start() starts the process suspended. The caller's contain() puts it in its
# job, then its token is read from outside (an AppContainer, this profile's,
# no capabilities, Low integrity) before any of its code runs; only then is it
# resumed. Only its pipes, the null device and the handles the caller names
# are inherited (an explicit handle list).
#
# Windows only (stdlib: ctypes, msvcrt). Imported where it is used (decoding,
# offboarding, the self-test), never at window start.

import os
import sys

# The profile's name. Never renamed: the uninstaller (packaging/uninstall.ps1)
# and offboarding remove it by this name, so a new one would leave the old one
# behind for every user; tests/test_consistency.py keeps the copies equal.
PROFILE = "Unminute.Decoder"
DISPLAY_NAME = "Unminute decoder"
DESCRIPTION = "Decodes recordings for Unminute, with no file or network access."
ALL_APPLICATION_PACKAGES = "S-1-15-2-1"
LOW_INTEGRITY = 0x1000

# The steps that can fail, as the log records them (diag FIELDS "step").
STEPS = ("profile", "start", "contain", "check", "ready")

# The decoder's environment: what Windows needs to start a process in a
# container (CreateProcess fails with ERROR_ENVVAR_NOT_FOUND without TEMP and
# the like; Windows then points TEMP and LOCALAPPDATA at the container's own
# folder), and nothing else of the app's.
ENV_NAMES = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "USERPROFILE",
             "LOCALAPPDATA", "APPDATA", "PATHEXT", "NUMBER_OF_PROCESSORS",
             "PROCESSOR_ARCHITECTURE")

_ALREADY_EXISTS = 0x800700B7            # HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)
_NOT_FOUND = (0x80070002, 0x80070003)   # HRESULT_FROM_WIN32(FILE/PATH_NOT_FOUND)


class SandboxError(OSError):
    """A step of setting up the container failed. step: one of STEPS;
    winerror: the Windows error (a Win32 code, or an HRESULT that is not
    one), 0 when there is none. Carries no path or text of the user's."""

    def __init__(self, step, winerror=0):
        super().__init__(int(winerror or 0), step)
        self.step = step if step in STEPS else "start"
        self.code = int(winerror or 0)


def _win32(hr):
    """An HRESULT as the Win32 code it wraps (facility 7), else unsigned."""
    hr &= 0xFFFFFFFF
    return hr & 0xFFFF if (hr >> 16) == 0x8007 else hr


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    # Private handles: prototypes set here do not affect other ctypes users.
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _env = ctypes.WinDLL("userenv", use_last_error=True)
    _ole = ctypes.WinDLL("ole32")

    _env.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p)]
    _env.CreateAppContainerProfile.restype = ctypes.c_long
    _env.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    _env.DeleteAppContainerProfile.restype = ctypes.c_long
    _env.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    _env.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    _env.GetAppContainerFolderPath.argtypes = [wintypes.LPCWSTR,
                                               ctypes.POINTER(wintypes.LPWSTR)]
    _env.GetAppContainerFolderPath.restype = ctypes.c_long
    _ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    _adv.FreeSid.argtypes = [ctypes.c_void_p]
    _adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    _adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.HANDLE)]
    _adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _k32.LocalFree.argtypes = [ctypes.c_void_p]
    _k32.LocalFree.restype = ctypes.c_void_p
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.ResumeThread.argtypes = [wintypes.HANDLE]
    _k32.ResumeThread.restype = wintypes.DWORD
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    _k32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_void_p]
    _k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                    ("Buffer", ctypes.c_void_p)]

    # TokenSecurityAttributes' TOKEN_SECURITY_ATTRIBUTES_INFORMATION and its
    # TOKEN_SECURITY_ATTRIBUTE_V1 entries (the name is a counted string).
    class _TOKEN_SECURITY_ATTRIBUTE_V1(ctypes.Structure):
        _fields_ = [("Name", _UNICODE_STRING), ("ValueType", wintypes.WORD),
                    ("Reserved", wintypes.WORD), ("Flags", wintypes.DWORD),
                    ("ValueCount", wintypes.DWORD), ("Values", ctypes.c_void_p)]

    class _TOKEN_SECURITY_ATTRIBUTES_INFORMATION(ctypes.Structure):
        _fields_ = [("Version", wintypes.WORD), ("Reserved", wintypes.WORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p)]

    class _SECURITY_CAPABILITIES(ctypes.Structure):
        _fields_ = [("AppContainerSid", ctypes.c_void_p), ("Capabilities", ctypes.c_void_p),
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

    _k32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
        wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
        ctypes.POINTER(_PROCESS_INFORMATION)]

PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY = 0x0002000F
PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT = 0x01
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
TOKEN_QUERY = 0x0008
TokenIsAppContainer, TokenCapabilities, TokenAppContainerSid = 29, 30, 31
TokenSecurityAttributes = 39
# The token attribute Windows sets on a less privileged AppContainer (#439).
LPAC_ATTRIBUTE = "WIN://NOALLAPPPKG"
_CLAIM_INT64, _CLAIM_UINT64 = 1, 2
WAIT_TIMEOUT = 0x102


# -- the profile ---------------------------------------------------------------------

class _Sid:
    """A SID Windows allocated (FreeSid when done)."""

    def __init__(self, pointer):
        self.pointer = pointer

    def text(self):
        out = wintypes.LPWSTR()
        if not _adv.ConvertSidToStringSidW(self.pointer, ctypes.byref(out)):
            raise SandboxError("profile", ctypes.get_last_error())
        try:
            return out.value
        finally:
            _k32.LocalFree(out)

    def close(self):
        pointer, self.pointer = self.pointer, None
        if pointer:
            _adv.FreeSid(pointer)


def _derive(name):
    sid = ctypes.c_void_p()
    name = name or PROFILE
    hr = _env.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
    if hr:
        raise SandboxError("profile", _win32(hr))
    return _Sid(sid.value)


def container_sid(name=None):
    """The profile's AppContainer SID as text (derived; nothing is created)."""
    if sys.platform != "win32":
        return None
    sid = _derive(name)
    try:
        return sid.text()
    finally:
        sid.close()


def ensure_profile(name=None):
    """The profile's SID (a _Sid; close it), making the profile for this user
    first if it is not there. Raises SandboxError("profile")."""
    sid = ctypes.c_void_p()
    name = name or PROFILE
    hr = _env.CreateAppContainerProfile(name, DISPLAY_NAME, DESCRIPTION, None, 0,
                                        ctypes.byref(sid))
    if hr == 0:
        return _Sid(sid.value)
    if hr & 0xFFFFFFFF != _ALREADY_EXISTS:
        raise SandboxError("profile", _win32(hr))
    return _derive(name)


def profile_exists(name=None):
    """Is the profile registered for this user? Asks Windows for its folder,
    creating nothing. False off Windows. Raises SandboxError when Windows
    cannot say."""
    if sys.platform != "win32":
        return False
    text = container_sid(name)
    folder = wintypes.LPWSTR()
    hr = _env.GetAppContainerFolderPath(text, ctypes.byref(folder))
    if hr == 0:
        _ole.CoTaskMemFree(folder)
        return True
    if hr & 0xFFFFFFFF in _NOT_FOUND:
        return False
    raise SandboxError("profile", _win32(hr))


def delete_profile(name=None):
    """Remove the profile (its registry key and its folder under Packages).
    Returns whether there was one. Windows treats a missing profile as
    removed already. Raises SandboxError("profile") if it stays."""
    if sys.platform != "win32":
        return False
    name = name or PROFILE
    existed = profile_exists(name)
    hr = _env.DeleteAppContainerProfile(name)
    if hr:
        raise SandboxError("profile", _win32(hr))
    return existed


# -- the process -----------------------------------------------------------------------

def _query(token, info_class):
    """GetTokenInformation's buffer for info_class."""
    size = wintypes.DWORD()
    _adv.GetTokenInformation(token, info_class, None, 0, ctypes.byref(size))
    buf = ctypes.create_string_buffer(max(size.value, 4))
    if not _adv.GetTokenInformation(token, info_class, buf, len(buf), ctypes.byref(size)):
        raise SandboxError("check", ctypes.get_last_error())
    return buf


def _less_privileged(token):
    """Does the token carry LPAC_ATTRIBUTE with a non-zero whole-number value
    (a less privileged AppContainer, which ALL APPLICATION PACKAGES does not
    match)? Every pointer read here lies inside the buffer Windows filled."""
    buf = _query(token, TokenSecurityAttributes)
    start, end = ctypes.addressof(buf), ctypes.addressof(buf) + len(buf)

    def inside(address, size):
        return address and start <= address and address + size <= end
    info = _TOKEN_SECURITY_ATTRIBUTES_INFORMATION.from_buffer(buf)
    entry = ctypes.sizeof(_TOKEN_SECURITY_ATTRIBUTE_V1)
    if not info.AttributeCount or not inside(info.Attributes, info.AttributeCount * entry):
        return False
    attrs = (_TOKEN_SECURITY_ATTRIBUTE_V1 * info.AttributeCount).from_address(info.Attributes)
    for attr in attrs:
        name = attr.Name
        if not inside(name.Buffer, name.Length):
            continue
        if ctypes.wstring_at(name.Buffer, name.Length // 2) != LPAC_ATTRIBUTE:
            continue
        if (attr.ValueType not in (_CLAIM_INT64, _CLAIM_UINT64) or not attr.ValueCount
                or not inside(attr.Values, 8 * attr.ValueCount)):
            return False
        return any((ctypes.c_uint64 * attr.ValueCount).from_address(attr.Values))
    return False


def token_facts(process):
    """What the token of `process` (a handle with query access) says, read
    from outside: (in an AppContainer, its AppContainer SID as text or None,
    how many capabilities it holds, its integrity level, whether it is a less
    privileged AppContainer)."""
    from workspace.decode_child import _integrity_level
    token = wintypes.HANDLE()
    if not _adv.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
        raise SandboxError("check", ctypes.get_last_error())
    try:
        inside = bool(ctypes.c_uint32.from_buffer(_query(token, TokenIsAppContainer)).value)
        capabilities = ctypes.c_uint32.from_buffer(_query(token, TokenCapabilities)).value
        sid_text = None
        if inside:
            info = _query(token, TokenAppContainerSid)      # the SID lives in this buffer
            pointer = ctypes.c_void_p.from_buffer(info).value
            sid_text = _Sid(pointer).text() if pointer else None     # never freed: not ours
            del info
        try:
            level = _integrity_level(token)
        except OSError as exc:
            raise SandboxError("check", exc.errno or 0) from exc
        return inside, sid_text, capabilities, level, _less_privileged(token)
    finally:
        _k32.CloseHandle(token)


def check_token(process, expected_sid):
    """Refuse (SandboxError "check") unless `process` runs in the expected
    AppContainer as a less privileged one, with no capabilities, at Low
    integrity or below."""
    inside, sid_text, capabilities, level, lpac = token_facts(process)
    if (not inside or sid_text != expected_sid or capabilities != 0 or level > LOW_INTEGRITY
            or not lpac):
        raise SandboxError("check")


def environment(extra=None):
    """The decoder's environment block, sorted by name as Windows expects."""
    found = {k.upper(): v for k, v in os.environ.items() if k.upper() in ENV_NAMES}
    root = found.get("SYSTEMROOT", "")
    if root:
        found["PATH"] = os.path.join(root, "System32")
    found.update(extra or {})
    return found


class Child:
    """A process started in the container, with the parent's ends of its
    stdin and stdout as Python files. Like subprocess.Popen where media.decode
    uses it: pid, stdin, stdout, poll, wait, kill."""

    def __init__(self, pid, handle, stdin, stdout):
        self.pid, self._handle = pid, handle
        self.stdin, self.stdout = stdin, stdout
        self.returncode = None

    def poll(self):
        return self.wait(0, raising=False)

    def wait(self, timeout=None, raising=True):
        import subprocess
        if self.returncode is not None:
            return self.returncode
        ms = 0xFFFFFFFF if timeout is None else int(max(0, timeout) * 1000)
        if _k32.WaitForSingleObject(self._handle, ms) == WAIT_TIMEOUT:
            if raising:
                raise subprocess.TimeoutExpired("decoder", timeout)
            return None
        code = wintypes.DWORD()
        _k32.GetExitCodeProcess(self._handle, ctypes.byref(code))
        self.returncode = code.value
        return self.returncode

    def kill(self):
        if self.returncode is None:
            _k32.TerminateProcess(self._handle, 1)      # fails harmlessly once it has ended

    def close(self):
        handle, self._handle = self._handle, None
        if handle:
            _k32.CloseHandle(handle)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def start(argv, contain, handles=(), env=None, name=None):
    """Start argv in the container `name` with no capabilities, suspended;
    contain(pid) (the caller's job) and the token check run before it is
    resumed. Inherited: its stdin and stdout pipes, the null device as stderr,
    and `handles` (OS handles the caller owns, inheritable only for the call).
    The current folder is the interpreter's own (the container can read it).
    Returns a Child. Raises SandboxError naming the step that failed; the
    process never runs then."""
    import msvcrt
    sid = ensure_profile(name)
    try:
        expected = sid.text()
        in_r, in_w = os.pipe()                     # both non-inheritable
        out_r, out_w = os.pipe()
        null = os.open(os.devnull, os.O_WRONLY)
        child_ends = [msvcrt.get_osfhandle(fd) for fd in (in_r, out_w, null)]
        inherit = child_ends + [int(h) for h in handles]
        pi = _PROCESS_INFORMATION()
        try:
            for h in inherit:
                os.set_handle_inheritable(h, True)
            try:
                _create(argv, env if env is not None else environment(), inherit, child_ends,
                        sid.pointer, pi)
            finally:
                for h in handles:
                    os.set_handle_inheritable(int(h), False)   # the caller keeps them
        except BaseException:
            for fd in (in_w, out_r):
                os.close(fd)
            raise
        finally:
            for fd in (in_r, out_w, null):
                os.close(fd)
    finally:
        sid.close()
    child = Child(pi.dwProcessId, pi.hProcess, os.fdopen(in_w, "wb"), os.fdopen(out_r, "rb"))
    try:
        try:
            contain(pi.dwProcessId)
        except SandboxError:
            raise
        except OSError as exc:
            raise SandboxError("contain", exc.errno or 0) from exc
        check_token(pi.hProcess, expected)
        if _k32.ResumeThread(pi.hThread) == 0xFFFFFFFF:
            raise SandboxError("start", ctypes.get_last_error())
    except BaseException:
        child.kill()
        child.wait(10, raising=False)
        for f in (child.stdin, child.stdout):
            f.close()
        child.close()
        raise
    finally:
        _k32.CloseHandle(pi.hThread)
    return child


def _create(argv, env, inherit, std, sid_pointer, pi):
    """CreateProcessW in the container, suspended, with exactly `inherit`."""
    import subprocess
    caps = _SECURITY_CAPABILITIES(sid_pointer, None, 0, 0)
    opt_out = wintypes.DWORD(PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT)
    size = ctypes.c_size_t()
    _k32.InitializeProcThreadAttributeList(None, 3, 0, ctypes.byref(size))
    attrs = ctypes.create_string_buffer(size.value)
    if not _k32.InitializeProcThreadAttributeList(attrs, 3, 0, ctypes.byref(size)):
        raise SandboxError("start", ctypes.get_last_error())
    try:
        handles = (wintypes.HANDLE * len(inherit))(*inherit)
        for attr, value, n in ((PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles,
                                ctypes.sizeof(handles)),
                               (PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, ctypes.byref(caps),
                                ctypes.sizeof(caps)),
                               (PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY,
                                ctypes.byref(opt_out), ctypes.sizeof(opt_out))):
            if not _k32.UpdateProcThreadAttribute(attrs, 0, attr, value, n, None, None):
                raise SandboxError("start", ctypes.get_last_error())
        si = _STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(si)
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = std
        si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
        names = sorted(env, key=str.upper)
        block = "".join(f"{k}={env[k]}\0" for k in names) + "\0"
        env_buf = ctypes.create_unicode_buffer(block, len(block))
        cmd = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        cwd = os.path.dirname(os.path.abspath(argv[0]))
        if not _k32.CreateProcessW(argv[0], cmd, None, None, True,
                                   EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED
                                   | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                                   env_buf, cwd, ctypes.byref(si), ctypes.byref(pi)):
            raise SandboxError("start", ctypes.get_last_error())
    finally:
        _k32.DeleteProcThreadAttributeList(attrs)
