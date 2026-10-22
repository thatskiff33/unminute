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
import re
import sys
import threading
import time
import unicodedata
from urllib.parse import urlsplit, urlunsplit

POLICY_KEY = r"SOFTWARE\Policies\Unminute"
REFRESH_S = 900.0
UNREADABLE_RETRY_S = 30.0       # an unreadable key is often mid-rewrite: read again soon

# name -> (kind, app default, permission or not). kind "bool" is a DWORD 0/1,
# "int" a DWORD in range, "path" a REG_SZ or REG_EXPAND_SZ folder, "name" and
# "url" a REG_SZ checked by valid_name and valid_url.
SPEC = {
    # Setup: downloading or repairing models in the app.
    "AllowModelDownloads": ("bool", True, True),
    # The optional NVIDIA component (code downloaded into the user's profile).
    "AllowGpuComponent": ("bool", True, True),
    # Microphone recording (the user may still turn it off).
    "AllowMicrophoneRecording": ("bool", True, True),
    # Computer audio as a second track beside the microphone (#108). Allowed
    # when not set (the owner's decision 4, 20 Oct 2026); each user's switch
    # is still off by default, and it never applies without the microphone.
    "AllowComputerAudioRecording": ("bool", True, True),
    # Setting the Windows lid action to "Do nothing" while recording, if the
    # user turns it on (#172). Off by policy: never changed, and put back.
    "AllowLidOverride": ("bool", True, True),
    # Live draft text while recording (#104), if the user turns it on. Off by
    # policy: never started, and stopped and cleared when the app reads it.
    "AllowLiveDrafts": ("bool", True, True),
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
    # Hand off "Copy and open <name>" (#109): copy the handoff, then open this
    # https address in the user's browser. Name and address are set together
    # (see app.Workspace._assistant); the app's default is ChatGPT.
    "AllowHandoffAssistant": ("bool", True, True),
    "HandoffAssistantName": ("name", None, False),
    "HandoffAssistantUrl": ("url", None, False),
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


NAME_MAX = 30
URL_MAX = 300
NAME_PUNCTUATION = frozenset(" .-&'()+")
# A host name of ASCII labels (an internationalised name in its xn-- form),
# at least two of them, the last not all digits: never an IP address.
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# Characters a web address never needs here: quotes, angle brackets, a
# backslash (Windows reads it as a path), and the rest of RFC 3986's unsafe set.
_URL_REFUSED = frozenset("\"'<>\\^`{|}")


def valid_name(value):
    """The assistant's name as shown on the button: 1 to NAME_MAX letters
    (any script, macrons included), digits, single spaces and . - & ' ( ) +.
    Nothing invisible, no line breaks, no leading or trailing space."""
    if not isinstance(value, str) or not 1 <= len(value) <= NAME_MAX:
        return False
    if value != value.strip(" ") or "  " in value or value != unicodedata.normalize("NFC", value):
        return False
    return all(ch in NAME_PUNCTUATION or unicodedata.category(ch)[0] == "L"
               or unicodedata.category(ch) == "Nd" for ch in value)


def valid_url(value):
    """A web address the app may hand to Windows to open in the browser:
    https only, a host name (no IP address, user name or password), port 443
    or none, ASCII with no spaces, quotes or control characters, no fragment,
    at most URL_MAX characters. Parsed and rebuilt, it must equal the value
    exactly, so nothing urlsplit quietly drops (tabs, line breaks, leading or
    trailing spaces) gets through. Anything else (javascript:, file:,
    ms-settings:, a \\\\server\\share path) would be handed by Windows to
    another program, so it is refused."""
    if not isinstance(value, str) or not 1 <= len(value) <= URL_MAX or not value.isascii():
        return False
    if any(ch <= " " or ch == "\x7f" or ch in _URL_REFUSED for ch in value):
        return False
    if not value.startswith("https://") or "#" in value or "@" in value:
        return False
    # A percent-encoded control character (%00 to %1F, %7F) is refused too.
    if re.search(r"%(?![0-9A-Fa-f]{2})|%[01][0-9A-Fa-f]|%7[Ff]", value):
        return False
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    if parts.scheme != "https" or port not in (None, 443):
        return False
    if parts.netloc not in (host, host + ":443") or len(host) > 253:
        return False
    labels = host.split(".")
    if len(labels) < 2 or labels[-1].isdigit() or not all(_LABEL.fullmatch(x) for x in labels):
        return False
    return urlunsplit(parts) == value


def _parse(name, value, regtype):
    import winreg
    kind = SPEC[name][0]
    if kind in ("name", "url"):
        # REG_SZ only: an expandable value could change with the user's environment.
        check = valid_name if kind == "name" else valid_url
        if regtype != winreg.REG_SZ or not check(value):
            raise ValueError(name)
        return value
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
