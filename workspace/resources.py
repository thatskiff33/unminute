# workspace/resources.py
# Quiet resource defaults (#15). Stdlib only (ctypes for Win32).
#
# Quiet (default): half the logical CPUs, capped at four, at least one, with
# the worker at below-normal priority so the laptop stays responsive.
# Faster (optional): all but two logical CPUs. Neither promises a speed.

import ctypes
import os
import sys

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
NORMAL_PRIORITY_CLASS = 0x00000020
PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

QUIET, FASTER = "quiet", "faster"


def worker_threads(mode=QUIET, cpu_count=None):
    n = cpu_count if cpu_count is not None else (os.cpu_count() or 2)
    if mode == FASTER:
        return max(1, n - 2)
    return max(1, min(4, n // 2))


def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.GetPriorityClass.argtypes = [ctypes.c_void_p]
    k.GetPriorityClass.restype = ctypes.c_uint32
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    return k


def set_priority(pid=None, priority=BELOW_NORMAL_PRIORITY_CLASS):
    """Set a process's priority class (default: this process). Returns success."""
    if sys.platform != "win32":
        try:
            os.nice(5)
            return True
        except OSError:
            return False
    k = _k32()
    h = k.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION, 0,
                      pid or os.getpid())
    if not h:
        return False
    try:
        return bool(k.SetPriorityClass(h, priority))
    finally:
        k.CloseHandle(h)


def get_priority(pid):
    if sys.platform != "win32":
        return None
    k = _k32()
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not h:
        return None
    try:
        return k.GetPriorityClass(h)
    finally:
        k.CloseHandle(h)


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_uint32), ("BatteryFullLifeTime", ctypes.c_uint32)]


def on_battery():
    """True only when Windows reports the machine is running on battery.
    Unknown status (desktops, VMs) counts as mains power."""
    if sys.platform != "win32":
        return False
    status = _PowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return False
    return status.ACLineStatus == 0


def may_start_automatic_work(override=False, battery=None):
    """Automatic processing waits on battery unless the user overrides it.
    Work the user starts explicitly is never blocked by this."""
    if override:
        return True
    return not (on_battery() if battery is None else battery)
