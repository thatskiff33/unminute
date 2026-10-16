# workspace/credentials.py
# The Hugging Face token in the user's Windows Credential Manager (#19).
#
#   - Stored per Windows user as a generic credential (CRED_PERSIST_LOCAL_MACHINE:
#     this computer only, not roamed), encrypted by Windows.
#   - Never written to settings, logs, the database, command lines or the
#     environment; never returned to the page (only "remembered: yes/no").
#   - Removing it never affects models already downloaded: they load offline.
# Stdlib only (ctypes over advapi32).

import ctypes
import sys

TARGET = "WhisperTranscriber/HuggingFaceToken"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
MAX_TOKEN = 400


class CredentialError(RuntimeError):
    """Credential Manager failed (message is safe to show; never contains the token)."""


if sys.platform == "win32":
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", _FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _adv.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    _adv.CredWriteW.restype = wintypes.BOOL
    _adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    _adv.CredReadW.restype = wintypes.BOOL
    _adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _adv.CredDeleteW.restype = wintypes.BOOL
    _adv.CredFree.argtypes = [ctypes.c_void_p]


def _require_windows():
    if sys.platform != "win32":
        raise CredentialError("Remembering the token needs Windows Credential Manager.")


def save_token(token, target=TARGET):
    _require_windows()
    token = (token or "").strip()
    if not token or len(token) > MAX_TOKEN or any(c.isspace() for c in token):
        raise CredentialError("That does not look like a Hugging Face token.")
    blob = token.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = _CREDENTIAL(Type=CRED_TYPE_GENERIC, TargetName=target,
                       Comment="Hugging Face token for downloading speaker models",
                       CredentialBlobSize=len(blob),
                       CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)),
                       Persist=CRED_PERSIST_LOCAL_MACHINE, UserName="huggingface")
    if not _adv.CredWriteW(ctypes.byref(cred), 0):
        raise CredentialError(f"Windows could not store the token (error "
                              f"{ctypes.get_last_error()}).")


def load_token(target=TARGET):
    """The remembered token, or None."""
    _require_windows()
    ptr = ctypes.POINTER(_CREDENTIAL)()
    if not _adv.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        if ctypes.get_last_error() == ERROR_NOT_FOUND:
            return None
        raise CredentialError(f"Windows could not read the token (error "
                              f"{ctypes.get_last_error()}).")
    try:
        c = ptr.contents
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise CredentialError("The remembered token is damaged. Remove it and enter it "
                              "again.") from exc
    finally:
        _adv.CredFree(ptr)


def has_token(target=TARGET):
    """Whether a token is remembered, without reading the secret itself."""
    if sys.platform != "win32":
        return False
    ptr = ctypes.POINTER(_CREDENTIAL)()
    if not _adv.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        return False
    _adv.CredFree(ptr)
    return True


def remove_token(target=TARGET):
    """Forget the token. Returns True if one was removed."""
    _require_windows()
    if _adv.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
        return True
    if ctypes.get_last_error() == ERROR_NOT_FOUND:
        return False
    raise CredentialError(f"Windows could not remove the token (error "
                          f"{ctypes.get_last_error()}).")


# The earlier source-installed app (removed in #111 A7) asked users to keep
# their token in the HF_TOKEN environment variable, where it may still be. The
# app takes it (and the older name the Hugging Face library also reads) out of
# its own environment at start, so worker, download and WebView2 processes
# never inherit it. It is not used: Setup asks for a token.
ENVIRONMENT_TOKENS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")


def drop_environment_token():
    """Remove any Hugging Face token from this process's environment. Call
    once, before any child process starts."""
    import os
    for name in ENVIRONMENT_TOKENS:
        os.environ.pop(name, None)
