# workspace/power.py
# Keeps the computer awake while a recording is open.
#
#   - A system-required power request is held only while recording, so the
#     idle sleep timeout does not end a long meeting in which nobody touches
#     the keyboard. Windows shows it in `powercfg /requests` with REASON.
#   - It cannot stop sleep that the user starts: Windows ends every power
#     request when the lid is closed with its action set to Sleep, the power
#     button is pressed, or Sleep is chosen from Start. On Modern Standby
#     laptops on battery it also ends five minutes after the sleep timeout
#     (Microsoft Learn, PowerSetRequest). Recording with the lid closed
#     therefore also needs the Windows lid action "Do nothing".
#   - A failure is never fatal: recording carries on without the request.
#     Windows drops the request by itself if the app exits.
# Stdlib only (ctypes).

import ctypes
import sys
import threading

REASON = "Unminute is recording a meeting."
POWER_REQUEST_CONTEXT_VERSION = 0
POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
POWER_REQUEST_SYSTEM_REQUIRED = 1           # POWER_REQUEST_TYPE.PowerRequestSystemRequired
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _Detailed(ctypes.Structure):
    _fields_ = [("LocalizedReasonModule", ctypes.c_void_p),
                ("LocalizedReasonId", ctypes.c_ulong),
                ("ReasonStringCount", ctypes.c_ulong),
                ("ReasonStrings", ctypes.c_void_p)]


class _Reason(ctypes.Union):
    _fields_ = [("Detailed", _Detailed), ("SimpleReasonString", ctypes.c_wchar_p)]


class REASON_CONTEXT(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("Flags", ctypes.c_ulong), ("Reason", _Reason)]


class _Kernel32:
    """The Windows calls; tests pass a fake with the same four methods."""

    def __init__(self):
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.create = k.PowerCreateRequest
        self.create.argtypes = [ctypes.POINTER(REASON_CONTEXT)]
        self.create.restype = ctypes.c_void_p
        self.set = k.PowerSetRequest
        self.clear = k.PowerClearRequest
        for fn in (self.set, self.clear):
            fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
            fn.restype = ctypes.c_int
        self.close = k.CloseHandle
        self.close.argtypes = [ctypes.c_void_p]
        self.close.restype = ctypes.c_int


class KeepAwake:
    """hold() while recording, release() after. Both are safe to repeat."""

    def __init__(self, api=None):
        self._api = api
        self._handle = None
        self._lock = threading.Lock()

    @property
    def held(self):
        return self._handle is not None

    def hold(self):
        """True if Windows accepted the request."""
        with self._lock:
            if self._handle is not None:
                return True
            try:
                if self._api is None:
                    if sys.platform != "win32":
                        return False
                    self._api = _Kernel32()
                reason = REASON_CONTEXT(POWER_REQUEST_CONTEXT_VERSION,
                                        POWER_REQUEST_CONTEXT_SIMPLE_STRING)
                reason.Reason.SimpleReasonString = REASON
                handle = self._api.create(ctypes.byref(reason))
                if not handle or handle == INVALID_HANDLE_VALUE:
                    return False
                if not self._api.set(handle, POWER_REQUEST_SYSTEM_REQUIRED):
                    self._api.close(handle)
                    return False
            except (OSError, AttributeError):
                return False
            self._handle = handle
            return True

    def release(self):
        with self._lock:
            handle, self._handle = self._handle, None
            if handle is None:
                return
            try:
                self._api.clear(handle, POWER_REQUEST_SYSTEM_REQUIRED)
            except OSError:
                pass
            finally:
                try:
                    self._api.close(handle)
                except OSError:
                    pass
