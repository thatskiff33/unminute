# workspace/dpapi.py
# Windows Data Protection (DPAPI) for the workspace root key (#111 C1).
#
#   - User scope: only this Windows user, signed in, can unprotect what this
#     protects: not other users, and usually not on another computer. Per
#     Microsoft: with a roaming profile it can be unprotected on another
#     computer; on an Active Directory domain, the domain's DPAPI backup keys
#     unlock any domain user's data; and the user's keys are derived from
#     their password. What applies with Entra ID is to confirm (#189). Any
#     process running as the user can unprotect it, so DPAPI protects the key
#     at rest, not from malware in the session (docs/ENCRYPTION.md, "What it
#     does not do").
#   - Never shows a prompt (CRYPTPROTECT_UI_FORBIDDEN): a window that needs
#     an answer would block a background start.
#   - The optional entropy separates Unminute's blobs from other uses of the
#     same user's DPAPI keys; it is not a secret.
#   - The description stored in the clear inside the blob is a fixed word,
#     never content, and errors carry only the Windows error code (P3).
# Stdlib only (ctypes over crypt32 and kernel32).

import ctypes
import sys

CRYPTPROTECT_UI_FORBIDDEN = 0x1
DESCRIPTION = "Unminute"
MAX_BLOB = 1 << 16                  # a protected key is a few hundred bytes


class DpapiError(RuntimeError):
    """DPAPI failed. The message is safe to show; code is the Windows error
    number (0 when the failure was not Windows's), for the locked screen's
    "Copy details for IT"."""

    def __init__(self, message, code=0):
        super().__init__(message)
        self.code = code


if sys.platform == "win32":
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    _crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    _crypt.CryptProtectData.argtypes = [ctypes.POINTER(_BLOB), wintypes.LPCWSTR,
                                        ctypes.POINTER(_BLOB), ctypes.c_void_p,
                                        ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(_BLOB)]
    _crypt.CryptProtectData.restype = wintypes.BOOL
    _crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(_BLOB), ctypes.c_void_p,
                                          ctypes.POINTER(_BLOB), ctypes.c_void_p,
                                          ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(_BLOB)]
    _crypt.CryptUnprotectData.restype = wintypes.BOOL
    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel.LocalFree.restype = ctypes.c_void_p


def _require_windows():
    if sys.platform != "win32":
        raise DpapiError("Protecting the workspace key needs Windows.")


def _blob(data):
    """A DATA_BLOB over a copy of data, and the buffer that keeps it alive."""
    buf = (ctypes.c_ubyte * max(len(data), 1)).from_buffer_copy(bytes(data) or b"\0")
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf


def _take(out):
    """Copy out Windows's output buffer, wipe it and free it."""
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        if out.pbData:
            ctypes.memset(out.pbData, 0, out.cbData)
            _kernel.LocalFree(out.pbData)


def _check(data, name):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"{name} must be bytes")


def protect(data, entropy):
    """data protected for this Windows user on this computer."""
    _require_windows()
    _check(data, "data")
    _check(entropy, "entropy")
    src, plain = _blob(data)
    ent, _ent_buf = _blob(entropy)
    out = _BLOB()
    try:
        ok = _crypt.CryptProtectData(ctypes.byref(src), DESCRIPTION, ctypes.byref(ent), None,
                                     None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
        code = ctypes.get_last_error()
    finally:
        ctypes.memset(plain, 0, ctypes.sizeof(plain))       # our copy of the key
    if not ok:
        raise DpapiError(f"Windows could not protect the workspace key (error {code}).",
                         code)
    return _take(out)


def unprotect(blob, entropy):
    """What protect() was given, if this Windows user can unlock it.
    Raises DpapiError otherwise: another user or computer, a damaged blob,
    the wrong entropy, or Windows keys lost (for example after an
    administrator reset the password)."""
    _require_windows()
    _check(blob, "blob")
    _check(entropy, "entropy")
    if not blob or len(blob) > MAX_BLOB:
        raise DpapiError("The protected workspace key is damaged.")
    src, _src_buf = _blob(blob)
    ent, _ent_buf = _blob(entropy)
    out = _BLOB()
    if not _crypt.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None,
                                     None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        code = ctypes.get_last_error()
        raise DpapiError(f"Windows could not unlock the workspace key (error {code}).",
                         code)
    return _take(out)
