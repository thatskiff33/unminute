# workspace/policy.py
# Managed policy set by the organisation (#111 slice A5).
#
# IT sets values under HKLM\SOFTWARE\Policies\Unminute, for example through
# Intune with the ADMX template in packaging/policy/. Standard users cannot
# change that key, so a policy value always wins over the user's own setting.
# Only HKLM is read (64-bit view); there is no per-user policy.
#
#   - A value that is absent means "not managed": the app's own default or
#     the user's setting applies.
#   - A value of the wrong type or out of range is ignored and reported. For a
#     permission (Allow...) that means the permission is withheld, never
#     granted: a mistyped policy must not open something up.
#   - The policy is read again when Settings opens and at least every
#     REFRESH_S seconds, so a change made by IT applies without a restart.
# Stdlib only (winreg).

import os
import sys
import threading
import time

POLICY_KEY = r"SOFTWARE\Policies\Unminute"
REFRESH_S = 900.0
UNREADABLE_RETRY_S = 30.0       # an unreadable key is often mid-rewrite: read again soon

# name -> (kind, app default, permission or not). kind "bool" is a DWORD 0/1,
# "int" a DWORD in range, "path" a REG_SZ or REG_EXPAND_SZ folder.
SPEC = {
    # Setup: downloading or repairing models in the app.
    "AllowModelDownloads": ("bool", True, True),
    # The optional NVIDIA component (code downloaded into the user's profile).
    "AllowGpuComponent": ("bool", True, True),
    # Microphone recording (the user may still turn it off).
    "AllowMicrophoneRecording": ("bool", True, True),
    # A confirmation before each recording that everyone present has
    # been told (#111 A6). Not a permission: a bad value keeps the confirmation.
    "RequireRecordingConsent": ("bool", True, False),
    # Watched folders, and authority to delete originals in them.
    "AllowWatchedFolders": ("bool", True, True),
    "AllowDeleteOriginals": ("bool", True, True),
    # Recordings on mapped network drives or reached through symbolic links.
    "AllowNetworkSources": ("bool", False, True),
    # Clear the app's own handoff copy from the clipboard after N minutes (0 = never).
    "ClipboardClearMinutes": ("int", 0, False),
    # Where the handoff Save dialog starts, and whether the suggested file
    # name may include the meeting title.
    "HandoffDefaultFolder": ("path", None, False),
    "HandoffFilenameIncludesTitle": ("bool", False, True),
}
INT_RANGES = {"ClipboardClearMinutes": (0, 1440)}
ERROR_NO_MORE_ITEMS = 259
# A policy key that exists but cannot be read in full: every value counts as
# invalid, so every permission is withheld and the rest use the app default.
UNREADABLE = tuple(SPEC)


class Policy:
    """The managed values in force. get() returns the policy value if the
    organisation set a valid one, else the app default."""

    def __init__(self, values=None, invalid=()):
        self.values = dict(values or {})
        self.invalid = tuple(invalid)

    def managed(self, name):
        return name in self.values or name in self.invalid

    def get(self, name):
        kind, default, permission = SPEC[name]
        if name in self.values:
            return self.values[name]
        if name in self.invalid and permission:
            return False                     # withheld, never granted by a bad value
        return default

    @property
    def unreadable(self):
        """The key exists but could not be read: everything is withheld, but
        that is not an explicit decision by IT."""
        return not self.values and self.invalid == UNREADABLE

    def summary(self):
        """For Settings: which values are managed (no paths)."""
        return {"managed": sorted(set(self.values) | set(self.invalid)),
                "invalid": sorted(self.invalid)}


def _parse(name, value, regtype):
    import winreg
    kind = SPEC[name][0]
    if kind in ("bool", "int"):
        if regtype != winreg.REG_DWORD or not isinstance(value, int):
            raise ValueError(name)
        if kind == "bool":
            if value not in (0, 1):
                raise ValueError(name)
            return bool(value)
        lo, hi = INT_RANGES[name]
        if not lo <= value <= hi:
            raise ValueError(name)
        return value
    if regtype not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
        raise ValueError(name)
    path = os.path.expandvars(value) if regtype == winreg.REG_EXPAND_SZ else value
    if len(path) > 400:
        raise ValueError(name)
    # Drive-letter paths only (a mapped drive is fine), the same rule as the
    # save itself: a \\server\share folder would be contacted just to check it.
    from workspace.safe_paths import UnsafePathError, check_raw
    try:
        check_raw(path)
    except UnsafePathError:
        raise ValueError(name) from None
    return path


def read_registry():
    """(values, invalid) from HKLM\\SOFTWARE\\Policies\\Unminute."""
    if sys.platform != "win32":
        return {}, ()
    import winreg
    values, invalid = {}, []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, POLICY_KEY, 0,
                             winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
    except FileNotFoundError:
        return {}, ()                        # no policy at all
    except OSError:
        return {}, UNREADABLE                # there, but unreadable: nothing is granted
    with key:
        i = 0
        while True:
            try:
                name, value, regtype = winreg.EnumValue(key, i)
            except OSError as exc:
                if getattr(exc, "winerror", None) == ERROR_NO_MORE_ITEMS:
                    break
                return {}, UNREADABLE        # a partial read could miss a restriction
            i += 1
            match = next((n for n in SPEC if n.lower() == name.lower()), None)
            if match is None:
                continue                     # unknown names are not ours to judge
            try:
                values[match] = _parse(match, value, regtype)
            except ValueError:
                invalid.append(match)
    return values, tuple(invalid)


class PolicySource:
    """Caches the policy and reads it again after REFRESH_S, or on refresh()."""

    def __init__(self, reader=read_registry, refresh_s=REFRESH_S, clock=time.monotonic):
        self._reader, self._refresh_s, self._clock = reader, refresh_s, clock
        self._lock = threading.Lock()
        self._policy, self._read_at = None, None

    def refresh(self):
        values, invalid = self._reader()
        with self._lock:
            self._policy, self._read_at = Policy(values, invalid), self._clock()
            return self._policy

    def current(self):
        with self._lock:
            policy, read_at = self._policy, self._read_at
        if policy is None:
            return self.refresh()
        wait = UNREADABLE_RETRY_S if policy.unreadable else self._refresh_s
        if self._clock() - read_at >= wait:
            return self.refresh()
        return policy
