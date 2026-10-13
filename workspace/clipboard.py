# workspace/clipboard.py
# Put text on the Windows clipboard (#22 handoff). Done in Python rather than
# by page script so copying works the same way every time, whatever the page
# origin. The app never clears the clipboard by itself; the final cleanup
# confirmation tells the user how to (#22).

import ctypes
import sys
import time

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class ClipboardError(RuntimeError):
    """The clipboard could not be used (message is safe to show)."""


def copy_text(text):
    if sys.platform != "win32":
        raise ClipboardError("Copying is only available on Windows.")
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

    data = (text + "\0").encode("utf-16-le")
    for _ in range(20):                     # another program may hold it briefly
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise ClipboardError("The clipboard is busy. Try again.")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise ClipboardError("Not enough memory to copy.")
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise ClipboardError("Copying failed. Try again.")
    finally:
        user32.CloseClipboard()


def read_text():
    """For tests: the clipboard's text."""
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    if not user32.OpenClipboard(None):
        raise ClipboardError("The clipboard is busy.")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
