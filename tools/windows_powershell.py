# tools/windows_powershell.py
# Start Windows PowerShell 5.1, and other programs from the Windows system
# folder such as netstat, for the build and check tools (#235, #237).
#
# The child is started by its absolute path in the Windows system folder
# (GetSystemDirectoryW), never by name, so a powershell.exe or netstat.exe
# beside Python, in the current folder or earlier on PATH cannot stand in for
# it; if that file is missing, nothing runs. Its environment is built from an allowlist rather than
# inherited: variables that load code into a .NET process (COR_*, COMPlus_*,
# DOTNET_*, profilers) or point PowerShell at other modules (PSModulePath)
# never reach it, whatever shell or tool started the build. Standard library
# only, so the window checks can import it without loading anything heavy.

import os
import subprocess

# Folders the child may need, copied from the parent when set. None of them
# changes where PowerShell 5.1 finds modules: it takes Program Files' module
# folder from the known-folder API, not from the variable (checked on Windows
# 11, 20 October 2026).
KEPT = ("TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "ProgramData",
        "ProgramFiles")


def system_folder():
    """The Windows system folder, from GetSystemDirectoryW (never from PATH,
    the current folder or an environment variable)."""
    if os.name != "nt":
        raise OSError("Windows PowerShell needs Windows.")
    import ctypes
    buf = ctypes.create_unicode_buffer(260)
    n = ctypes.windll.kernel32.GetSystemDirectoryW(buf, 260)
    if not 0 < n < 260:
        raise OSError("Could not find the Windows system folder.")
    return buf.value


def home():
    """Windows PowerShell 5.1's own folder, under the system folder."""
    return os.path.join(system_folder(), "WindowsPowerShell", "v1.0")


def executable():
    """powershell.exe in the system folder; FileNotFoundError if it is not
    there (it is never looked for anywhere else)."""
    exe = os.path.join(home(), "powershell.exe")
    if not os.path.isfile(exe):
        raise FileNotFoundError("Windows PowerShell 5.1 is missing from the system folder: "
                                + exe)
    return exe


def system_program(name):
    """`name` (for example "NETSTAT.EXE") in the system folder itself;
    FileNotFoundError if it is not there (it is never looked for anywhere
    else). Start it with env=environment()."""
    exe = os.path.join(system_folder(), name)
    if not os.path.isfile(exe):
        raise FileNotFoundError(name + " is missing from the system folder: " + exe)
    return exe


def environment(extra=None):
    """The child's whole environment: the system folder's locations, a PATH of
    system folders only, PSModulePath pinned to 5.1's own modules (5.1 adds
    Program Files' modules, and an administrator is needed to change either),
    the folders in KEPT, and `extra`. Nothing else is inherited."""
    system = system_folder()
    windows = os.path.dirname(system)
    ps_home = os.path.join(system, "WindowsPowerShell", "v1.0")
    env = {name: os.environ[name] for name in KEPT if os.environ.get(name)}
    env.update({
        "SystemRoot": windows,
        "windir": windows,
        "SystemDrive": os.path.splitdrive(windows)[0],
        "PATH": os.pathsep.join([system, os.path.join(system, "Wbem"), ps_home]),
        "PSModulePath": os.path.join(ps_home, "Modules"),
    })
    env.update(extra or {})
    return env


def run(script, extra=None, **kwargs):
    """Run `script` in Windows PowerShell 5.1 (no profile, non-interactive)
    with environment(extra). Output is captured as text unless kwargs say
    otherwise. FileNotFoundError, before anything runs, if powershell.exe is
    missing from the system folder."""
    exe = executable()
    options = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}
    options.update(kwargs)
    return subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                          env=environment(extra), **options)
