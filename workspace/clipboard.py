# workspace/clipboard.py
# Put text on the Windows clipboard (#22 handoff). Done in Python rather than
# by page script so copying works the same way every time, whatever the page
# origin. The app never clears the clipboard by itself; the final cleanup
# confirmation tells the user how to (#22).
#
# The copy is marked so Windows keeps it out of clipboard history (Win+V) and
# cloud clipboard sync, and clipboard monitors are asked to ignore it.
# Function prototypes are declared on private WinDLL handles so other ctypes
# users in the process (e.g. pywebview) are not affected.

import ctypes
import sys
import time

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
_PRIVACY_FORMATS = (("ExcludeClipboardContentFromMonitorProcessing", b"\0\0\0\0"),
                    ("CanIncludeInClipboardHistory", b"\0\0\0\0"),      # DWORD 0
                    ("CanUploadToCloudClipboard", b"\0\0\0\0"))         # DWORD 0


class ClipboardError(RuntimeError):
    """The clipboard could not be used (message is safe to show)."""


def _api():
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    return user32, kernel32


def _global(kernel32, data):
    """A movable global memory block holding `data` (owned by the caller)."""
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise ClipboardError("Not enough memory to copy.")
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        raise ClipboardError("Not enough memory to copy.")
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(handle)
    return handle


def _open(user32):
    for _ in range(20):                     # another program may hold it briefly
        if user32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise ClipboardError("The clipboard is in use by another program. Try again.")


def copy_text(text):
    if sys.platform != "win32":
        raise ClipboardError("Copying is only available on Windows.")
    user32, kernel32 = _api()
    # Everything is prepared before the clipboard is touched, so a failure
    # here leaves the user's current clipboard as it was.
    blocks = [(CF_UNICODETEXT, _global(kernel32, (text + "\0").encode("utf-16-le")))]
    try:
        for name, value in _PRIVACY_FORMATS:
            fmt = user32.RegisterClipboardFormatW(name)
            if fmt:
                blocks.append((fmt, _global(kernel32, value)))
    except ClipboardError:
        for _, h in blocks:
            kernel32.GlobalFree(h)
        raise
    _open(user32)
    try:
        if not user32.EmptyClipboard():
            raise ClipboardError("Copying failed. Try again.")
        for i, (fmt, handle) in enumerate(blocks):
            if user32.SetClipboardData(fmt, handle):
                blocks[i] = (fmt, None)     # the clipboard owns it now
            elif fmt == CF_UNICODETEXT:
                raise ClipboardError("Copying failed. Try again.")
    finally:
        user32.CloseClipboard()
        for _, h in blocks:
            if h:
                kernel32.GlobalFree(h)


def read_text():
    """For tests: the clipboard's text."""
    user32, kernel32 = _api()
    _open(user32)
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
