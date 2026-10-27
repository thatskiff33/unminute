# tools/decoder_runtime.py
# A scratch runtime the decoder's AppContainer can read, for tests and for
# developer tools run from a checkout (#299).
#
# The app decodes every recording in a Windows AppContainer with no
# capabilities (workspace/appcontainer.py), which reads only what ALL
# APPLICATION PACKAGES may read. An installed app's runtime and app folders
# are (the installer grants it on its versions folder; Program Files has it by
# default), so its decoder starts there. A development or CI Python is not,
# and the app never falls back to decoding outside the container. Tests and
# tools never change permissions outside their own temporary folders, so, once
# per process, use() copies what the decoder needs into a temporary folder: the
# interpreter and its DLLs, the standard library (less its tests and GUI kits),
# numpy, PyAV and their bundled DLLs, and the imported workspace package;
# grants ALL APPLICATION PACKAGES read on that folder only; points
# media._runtime at it; and points the decoder at a throwaway AppContainer
# profile, so the app's own profile is never registered for the person running
# them. The folder and the profile are removed when the process ends; a
# profile left by a run that was killed is removed by the next one
# (sweep_profiles). It takes a few seconds. Off Windows it does nothing (there
# is no container).
#
#     import decoder_runtime            # tools/ on sys.path
#     decoder_runtime.use()             # before the first decode (tests)
#     with decoder_runtime.session():   # or for a block, removed after (tools)
#
# Not for a packaged build: tools/packaged_check.py tests the installed app's
# own container. Not in the package.

import atexit
import contextlib
import importlib
import importlib.machinery
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ALL_APPLICATION_PACKAGES = "S-1-15-2-1"
# Parts of the standard library the decoder never imports.
STDLIB_LEFT_OUT = {"test", "idlelib", "tkinter", "turtledemo", "ensurepip", "site-packages",
                   "lib2to3", "venv", "__phello__"}
_built = None
_profile = None
_patches = []
_users = 0


def icacls(*args):
    """icacls from the Windows system folder, never by name (#237, #253), with
    the minimal environment. Returns the CompletedProcess (text)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import windows_powershell
    return subprocess.run([windows_powershell.system_program("icacls.exe"), *map(str, args)],
                          capture_output=True, text=True, env=windows_powershell.environment())


def _locate(name):
    """Where package `name` is, found on sys.path without importing it and
    without import hooks (the tools that use this must not load PyAV
    themselves: the decoder does)."""
    return importlib.machinery.PathFinder.find_spec(name)


def _copy_tree(src, dest, skip=()):
    shutil.copytree(src, dest, ignore=lambda folder, names: [n for n in names if n in skip])


def build(base=None):
    """(interpreter, import path) of a scratch runtime the container can
    read, made once per process (base: where to make it; a new temporary
    folder, removed at exit, by default)."""
    global _built
    if _built is not None and base is None:
        return _built
    folder = Path(base or tempfile.mkdtemp(prefix="um-decoder-rt-"))
    if base is None:
        atexit.register(shutil.rmtree, folder, True)
    prefix = Path(sys.base_prefix)
    py = folder / "python"
    py.mkdir(parents=True)
    for item in prefix.iterdir():
        if item.is_file() and item.suffix.lower() in (".exe", ".dll"):
            shutil.copy2(item, py / item.name)
    if (prefix / "DLLs").is_dir():
        _copy_tree(prefix / "DLLs", py / "DLLs")
    _copy_tree(prefix / "Lib", py / "Lib", skip=STDLIB_LEFT_OUT)
    site = folder / "site"
    site.mkdir()
    for name in ("numpy", "av"):
        package = Path(_locate(name).submodule_search_locations[0])
        _copy_tree(package, site / name)
        libs = package.parent / f"{name}.libs"             # delvewheel's bundled DLLs
        if libs.is_dir():
            _copy_tree(libs, site / libs.name)
    app = folder / "app"
    workspace = Path(importlib.import_module("workspace").__file__).parent   # the one in use
    _copy_tree(workspace, app / "workspace", skip={"__pycache__", "ui"})
    if sys.platform == "win32":
        done = icacls(folder, "/grant", f"*{ALL_APPLICATION_PACKAGES}:(OI)(CI)(RX)", "/Q")
        if done.returncode:
            raise OSError("the scratch runtime could not be made readable to the container")
    result = (str(py / "python.exe"),
              [str(py / "Lib"), str(py / "DLLs"), str(py), str(site), str(app)])
    if base is None:
        _built = result
    return result


def profile():
    """A throwaway AppContainer profile name for this process, removed when it
    ends: never the app's own (appcontainer.PROFILE)."""
    global _profile
    if _profile is None:
        import secrets
        sweep_profiles()
        _profile = f"Unminute.Test.{os.getpid()}.{secrets.token_hex(4)}"
        atexit.register(_forget_profile, _profile)
    return _profile


# Where Windows registers this user's AppContainer profiles, one key per SID
# with the profile name as "Moniker" (lower case).
MAPPINGS = (r"Software\Classes\Local Settings\Software\Microsoft\Windows"
            r"\CurrentVersion\AppContainer\Mappings")
_TEST_PROFILE = re.compile(r"unminute\.test\.(\d+)\.[0-9a-f]{8}")


def sweep_profiles():
    """Remove throwaway profiles left by test runs that were killed (atexit
    never ran): only names profile() makes, and only when the process that
    made them has ended, so another session's profile in use stays. Never
    the app's own. Returns the names removed."""
    if sys.platform != "win32":
        return []
    import winreg
    stale = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MAPPINGS) as mappings:
            for i in range(winreg.QueryInfoKey(mappings)[0]):
                try:
                    with winreg.OpenKey(mappings, winreg.EnumKey(mappings, i)) as key:
                        name = str(winreg.QueryValueEx(key, "Moniker")[0])
                except OSError:
                    continue
                match = _TEST_PROFILE.fullmatch(name.lower())
                if match and not _running(int(match[1])):
                    stale.append(name)
    except OSError:
        return []
    for name in stale:
        _forget_profile(name)
    return stale


def _running(pid):
    """Is a process with this id running? True when Windows cannot say."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x1000, False, pid)              # QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() != 87                  # ERROR_INVALID_PARAMETER: none
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259                              # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _forget_profile(name):
    try:
        from workspace import appcontainer
        appcontainer.delete_profile(name)
    except Exception:
        pass


def packaged():
    """Is this a package's own runtime (the embeddable Python, with its
    ._pth beside it)? Its install is readable by the container already."""
    return any(Path(sys.base_prefix).glob("python*._pth"))


def use():
    """Point media._runtime at the scratch runtime, and the decoder at a
    throwaway profile, until stop(). Windows only, only where numpy and PyAV
    are installed (the decoder needs both), and never in a package's own
    runtime (that is the app's real container, which the tool measures)."""
    global _users
    if sys.platform != "win32" or packaged() or not all(
            _locate(m) for m in ("numpy", "av")):
        return
    _users += 1
    if _patches:                        # already in use (tools/run_tests.py, then a module)
        return
    from workspace import appcontainer, media
    runtime = build()
    for patch in (mock.patch.object(media, "_runtime",
                                    lambda: (runtime[0], list(runtime[1]))),
                  mock.patch.object(appcontainer, "PROFILE", profile())):
        patch.start()
        _patches.append(patch)


def stop():
    """Undo one use(); the patches go when the last user stops."""
    global _users
    _users = max(0, _users - 1)
    if _users == 0:
        while _patches:
            _patches.pop().stop()


def release():
    """stop(), then remove this process's scratch runtime and throwaway
    profile now rather than at exit."""
    global _built, _profile, _users
    _users = 1
    stop()
    if _built is not None:
        shutil.rmtree(Path(_built[0]).parents[1], ignore_errors=True)
        _built = None
    if _profile is not None:
        _forget_profile(_profile)
        _profile = None


@contextlib.contextmanager
def session():
    """use() for the block, then release() (for tools)."""
    use()
    try:
        yield
    finally:
        release()
