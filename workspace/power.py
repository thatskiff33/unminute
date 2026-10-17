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
#   - Windows ignores the request where policy turns off "Allow applications to
#     prevent automatic sleep" (AllowSystemPowerRequestAC/DC), without saying so.
#   - A failure is never fatal: recording carries on without the request.
#     Windows drops the request by itself if the app exits.
#
# The lid (#172, the user's decisions of 17 October 2026):
#   - LidSwitch: with the opt-in setting, the active power scheme's lid action
#     is set to "Do nothing" while recording and put back afterwards. The
#     original values are saved to a restore file before anything changes, so
#     a crash or power cut is put right at the next start. A value the user
#     changed meanwhile is left alone. Where Group Policy or Intune sets the
#     lid action, it stands aside.
#   - LidWatch: tells the app when the lid opens or closes while recording,
#     so it can say how long a recording carried on with the lid closed.
# Stdlib only (ctypes, winreg).

import ctypes
import json
import os
import sys
import threading
import uuid
from pathlib import Path

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
            handle = None
            try:                      # nothing here may stop a recording starting
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
                if self._api.set(handle, POWER_REQUEST_SYSTEM_REQUIRED):
                    self._handle, handle = handle, None
                    return True
                return False
            except Exception:
                return False
            finally:
                if handle and handle != INVALID_HANDLE_VALUE:   # created, not kept
                    try:
                        self._api.close(handle)
                    except Exception:
                        pass

    def release(self):
        with self._lock:
            handle, self._handle = self._handle, None
            if handle is None:
                return
            try:                      # nothing here may stop a recording ending
                self._api.clear(handle, POWER_REQUEST_SYSTEM_REQUIRED)
            except Exception:
                pass
            finally:
                try:
                    self._api.close(handle)
                except Exception:
                    pass


# -- the lid action (#172) ------------------------------------------------------------

SUB_BUTTONS = "4f971e89-eebd-4455-a8de-9e59040e7347"    # power buttons and lid
LIDACTION = "5ca83367-6e45-459f-a27b-476b1d01c936"      # what closing the lid does
LIDSWITCH_STATE = "ba3e0f4d-b817-4094-a2d1-d56379e6a0f3"  # GUID_LIDSWITCH_STATE_CHANGE
DO_NOTHING = 0
MAX_RESTORE_BYTES = 4096                                # a real restore record is about 80 bytes
# Where Group Policy (values ACSettingIndex/DCSettingIndex) or Intune (the
# Power CSP values) set the lid action; either means it is managed.
_SEP = chr(92)                                          # a backslash, kept out of literals
GP_LID_KEY = _SEP.join(("SOFTWARE", "Policies", "Microsoft", "Power", "PowerSettings",
                        LIDACTION.upper()))
GP_LID_VALUES = ("ACSettingIndex", "DCSettingIndex")
MDM_POWER_KEY = _SEP.join(("SOFTWARE", "Microsoft", "PolicyManager", "current", "device",
                           "Power"))
MDM_LID_VALUES = ("SelectLidCloseActionPluggedIn", "SelectLidCloseActionOnBattery")


class GUID(ctypes.Structure):
    _fields_ = [("bytes", ctypes.c_ubyte * 16)]

    @classmethod
    def of(cls, text):
        g = cls()
        ctypes.memmove(g.bytes, uuid.UUID(text).bytes_le, 16)
        return g

    def text(self):
        return str(uuid.UUID(bytes_le=bytes(self.bytes)))


class _PowrProf:
    """The scheme calls; tests pass a fake with the same four methods. read
    and active_scheme raise OSError on failure; write and apply return the
    Windows error code (0 is success)."""

    def __init__(self):
        self._p = ctypes.WinDLL("powrprof")
        self._k = ctypes.WinDLL("kernel32")
        self._sub, self._lid = GUID.of(SUB_BUTTONS), GUID.of(LIDACTION)

    def active_scheme(self):
        ptr = ctypes.POINTER(GUID)()
        rc = self._p.PowerGetActiveScheme(None, ctypes.byref(ptr))
        if rc:
            raise OSError(rc, "PowerGetActiveScheme")
        try:
            return ptr.contents.text()
        finally:
            self._k.LocalFree(ptr)

    def read(self, scheme, plugged_in):
        value = ctypes.c_ulong()
        fn = self._p.PowerReadACValueIndex if plugged_in else self._p.PowerReadDCValueIndex
        rc = fn(None, ctypes.byref(GUID.of(scheme)), ctypes.byref(self._sub),
                ctypes.byref(self._lid), ctypes.byref(value))
        if rc:
            raise OSError(rc, "PowerRead")
        return value.value

    def write(self, scheme, plugged_in, value):
        fn = self._p.PowerWriteACValueIndex if plugged_in else self._p.PowerWriteDCValueIndex
        return fn(None, ctypes.byref(GUID.of(scheme)), ctypes.byref(self._sub),
                  ctypes.byref(self._lid), ctypes.c_ulong(value))

    def apply(self, scheme):
        """Makes written values take effect (they apply to the active scheme)."""
        return self._p.PowerSetActiveScheme(None, ctypes.byref(GUID.of(scheme)))


def lid_managed(read_value=None):
    """True if Group Policy or Intune sets what closing the lid does. Only
    values count (Intune leaves "_LastWrite" markers behind)."""
    if read_value is None:
        if sys.platform != "win32":
            return False
        read_value = _registry_value
    return any(read_value(GP_LID_KEY, v) is not None for v in GP_LID_VALUES) or any(
        read_value(MDM_POWER_KEY, v) is not None for v in MDM_LID_VALUES)


def _registry_value(key, name):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return winreg.QueryValueEx(k, name)[0]
    except FileNotFoundError:
        return None                       # not set; any other error (access denied)
                                          # propagates, and managed() then says yes


class LidSwitch:
    """override() while recording, restore() after and at every start.

    The restore file (configuration, no content) holds the scheme and the
    original plugged-in and on-battery values. It is written, flushed and
    renamed into place before the lid action changes, and removed only once
    the values are back."""

    def __init__(self, restore_path, api=None, managed=lid_managed):
        self.path = Path(restore_path)
        self._api = api
        self._managed = managed
        self._lock = threading.Lock()
        # None, "failed" (a restore is waiting and will be tried again) or
        # "lost" (the record was damaged; the user is told once).
        self.problem = None
        self._saved = None               # this process's own override, also kept in memory

    def _calls(self):
        if self._api is None:
            if sys.platform != "win32":
                raise OSError("no power scheme on this platform")
            self._api = _PowrProf()
        return self._api

    @property
    def active(self):
        """A change is in force (or waits to be put back)."""
        return self._saved is not None or self.path.exists()

    def managed(self):
        try:
            return bool(self._managed())
        except Exception:
            return True                  # cannot tell: never override IT

    def override(self):
        """Sets "Do nothing" for plugged in and on battery. Returns
        (True, "set" | "already") or (False, "managed" | "refused")."""
        with self._lock:
            if self.managed():
                return False, "managed"
            if self.path.exists():          # originals still waiting to go back
                self._restore_locked()
                if self.path.exists():
                    return False, "refused"  # never overwrite them with "Do nothing"
            try:
                api = self._calls()
                scheme = api.active_scheme()
                original = {"scheme": scheme, "ac": api.read(scheme, True),
                            "dc": api.read(scheme, False)}
            except Exception:
                return False, "refused"
            if not _valid_saved(original):
                return False, "refused"     # a value it could not faithfully put back
            if original["ac"] == DO_NOTHING and original["dc"] == DO_NOTHING:
                return True, "already"      # nothing to change, nothing to put back
            try:
                self._save(original)
            except OSError:
                return False, "refused"     # never change what could not be put back
            self._saved = dict(original)    # also kept in memory, in case the file goes
            try:
                for plugged_in in (True, False):
                    if api.write(scheme, plugged_in, DO_NOTHING):
                        raise OSError("write refused")
                if api.apply(scheme):
                    raise OSError("apply refused")
                if (api.read(scheme, True), api.read(scheme, False)) != (DO_NOTHING,
                                                                           DO_NOTHING):
                    raise OSError("not taken")
            except Exception:
                self._restore_locked()
                return False, "refused"
            return True, "set"

    def restore(self):
        """Puts back the saved values, if any (also after a crash). A side
        the user has changed since (no longer "Do nothing") is left alone."""
        with self._lock:
            self._restore_locked()

    def _restore_locked(self):
        """Never raises. A file that cannot be read just now, or values that
        cannot be written just now, are kept for the next try ("failed"). A
        damaged file cannot say what to put back: the lid action is left as it
        is and the user is told ("lost", the user's decision on #188)."""
        try:
            with open(self.path, "rb") as f:
                raw = f.read(MAX_RESTORE_BYTES + 1)
        except FileNotFoundError:
            if self._saved is None:
                return
            raw = None                      # removed by something else: use the copy in memory
        except OSError:
            self.problem = "failed"         # briefly locked or unreadable: try again later
            return
        if raw is None:
            saved = self._saved
        else:
            try:                            # a real record is about 80 bytes
                saved = (json.loads(raw.decode("utf-8")) if len(raw) <= MAX_RESTORE_BYTES
                         else None)
            except (ValueError, RecursionError):
                saved = None
        if not _valid_saved(saved):
            self.problem = "lost"           # nothing safe to put back: left as it is
            self._remove()
            return
        try:
            api = self._calls()
            scheme = saved["scheme"]        # written there even if no longer active
            for plugged_in, key in ((True, "ac"), (False, "dc")):
                if api.read(scheme, plugged_in) == DO_NOTHING:
                    if api.write(scheme, plugged_in, saved[key]):
                        raise OSError("write refused")
            if scheme == api.active_scheme():
                api.apply(scheme)
        except Exception:
            self.problem = "failed"         # kept, so the next try puts it back
            return
        self._saved = None
        if self._remove():
            self.problem = None

    def _save(self, original):
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(original, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _remove(self):
        """True once the file is gone. A file that cannot be deleted (locked,
        read-only) is kept: the values are back, and the next try removes it."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            self.problem = "failed"
            return False
        return True


def _valid_saved(saved):
    """The restore record: a real scheme GUID and lid actions 0 to 3."""
    if not (isinstance(saved, dict) and isinstance(saved.get("scheme"), str)
            and all(isinstance(saved.get(k), int) and not isinstance(saved.get(k), bool)
                    and 0 <= saved[k] <= 3 for k in ("ac", "dc"))):
        return False
    try:
        uuid.UUID(saved["scheme"])
    except ValueError:
        return False
    return True


# -- lid open and close notifications (#172) ------------------------------------------

_ALIVE = []                                     # lid callbacks, never freed (see LidWatch)
DEVICE_NOTIFY_CALLBACK = 2
PBT_POWERSETTINGCHANGE = 0x8013
_CALLBACK = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                               ctypes.c_void_p) if sys.platform == "win32" else None


class _PowerSetting(ctypes.Structure):          # POWERBROADCAST_SETTING
    _fields_ = [("PowerSetting", GUID), ("DataLength", ctypes.c_ulong),
                ("Data", ctypes.c_ubyte * 4)]


class LidWatch:
    """start() calls on_change(open) on every lid change (Windows also reports
    the current state at once); stop() ends it. A failure is never fatal: the
    recording carries on without the lid notice."""

    def __init__(self, on_change, api=None):
        self.on_change = on_change
        self._api = api
        self._handle = None
        self._keep = None                # (callback, params), made once and never freed

    def start(self):
        if self._handle is not None:
            return True
        try:
            if self._api is None:
                if sys.platform != "win32":
                    return False
                self._api = ctypes.WinDLL("powrprof")
            if self._keep is None:
                class Params(ctypes.Structure):    # DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS
                    _fields_ = [("Callback", _CALLBACK), ("Context", ctypes.c_void_p)]
                cb = _CALLBACK(self._notified)
                self._keep = (cb, Params(cb, None))
                # Kept for the life of the process: Microsoft does not say that
                # unregistering waits for a callback already running, and a
                # freed callback crashes the process.
                _ALIVE.append(self._keep)
            handle = ctypes.c_void_p()
            rc = self._api.PowerSettingRegisterNotification(
                ctypes.byref(GUID.of(LIDSWITCH_STATE)), DEVICE_NOTIFY_CALLBACK,
                ctypes.byref(self._keep[1]), ctypes.byref(handle))
            if rc or not handle.value:
                return False
            self._handle = handle.value
            return True
        except Exception:
            return False

    def stop(self):
        """True if Windows confirmed the end of the subscription (the callback
        stays alive either way)."""
        handle, self._handle = self._handle, None
        if handle is None:
            return True
        try:
            return self._api.PowerSettingUnregisterNotification(ctypes.c_void_p(handle)) == 0
        except Exception:
            return False

    def _notified(self, context, kind, setting):
        try:
            if kind == PBT_POWERSETTINGCHANGE and setting:
                s = ctypes.cast(setting, ctypes.POINTER(_PowerSetting)).contents
                if s.DataLength >= 4:
                    self.on_change(int.from_bytes(bytes(s.Data), "little") != 0)
        except Exception:
            pass                          # never raise into Windows
        return 0
