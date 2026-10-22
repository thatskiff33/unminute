# workspace/safe_paths.py
# Validate file paths that come from the UI or from the file system before the
# app reads them. The web UI never supplies a path the backend trusts as-is:
#   - relative, UNC (\\server\share), device (\\?\, \\.\) and NUL-containing
#     paths are refused;
#   - the path is resolved with symlinks and junctions followed, and the real
#     target must still lie inside an allowed root (no junction escapes);
#   - only regular files with an allowed extension and size are accepted.
# Stdlib only.

import ntpath
import os
import stat
from pathlib import Path

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000   # cloud placeholder not on disk
FILE_ATTRIBUTE_OFFLINE = 0x1000


class UnsafePathError(ValueError):
    """The path was refused. The message is safe to show to the user."""


class CloudPlaceholderError(UnsafePathError):
    """The file is only in cloud storage (not downloaded to this computer)."""


def _is_within(child, root):
    child_n, root_n = os.path.normcase(child), os.path.normcase(root)
    try:
        return os.path.commonpath([child_n, root_n]) == root_n
    except ValueError:      # different drives
        return False


def check_raw(path):
    """Refuse path forms the app never needs, before touching the disk."""
    if not isinstance(path, (str, os.PathLike)):
        raise UnsafePathError("Not a file path.")
    text = os.fspath(path)
    if not text or "\x00" in text:
        raise UnsafePathError("Not a file path.")
    # Only "X:\..." local paths. This refuses UNC and device paths in every
    # separator mix (\\, //, /\, \/), which would otherwise contact a server
    # (and offer it Windows credentials) while being resolved, plus rooted
    # paths without a drive and drive-relative paths like "C:foo".
    drive, rest = ntpath.splitdrive(text)   # Windows rules on every OS (tests run on Linux)
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
        if text[:1] in "\\/" and text[1:2] in "\\/":
            raise UnsafePathError("Network and device paths are not accepted.")
        raise UnsafePathError("A full path is required.")
    if rest[:1] not in ("\\", "/"):
        raise UnsafePathError("A full path is required.")
    if ":" in rest:      # NTFS alternate data streams (file.wav:hidden)
        raise UnsafePathError("Not a file path.")
    return text


def resolve_within(path, roots):
    """Resolve `path` (following symlinks/junctions) and require the real
    target to sit inside one of `roots` (also resolved). Returns the real path."""
    text = check_raw(path)
    try:
        real = os.path.realpath(text, strict=True)
    except OSError as exc:
        raise UnsafePathError("The file could not be found.") from exc
    check_raw(real)      # a link must not lead to a network or device path either
    real_roots = [os.path.realpath(os.fspath(r)) for r in roots]
    if not any(_is_within(real, r) for r in real_roots):
        raise UnsafePathError("The file is outside the folders this app may read.")
    return Path(real)


DRIVE_REMOTE = 4
NETWORK_MESSAGE = ("Recordings on network drives are not accepted. Copy the recording to "
                   "this computer first (synced folders such as OneDrive are fine).")


MAX_PATH = 260
TOO_LONG_MESSAGE = ("The file's path is too long for Windows to open. Move it to a folder "
                    "with a shorter path, then try again.")


def _too_long(real):
    """The file exists but Windows cannot open it by this path: it is
    MAX_PATH characters or more and long paths are not turned on. Asked by
    the extended form (\\\\?\\), which only reads the file's details, and
    only for a drive-letter path (check_raw), never a network one."""
    text = str(real)
    if os.name != "nt" or len(text) < MAX_PATH:
        return False
    try:
        check_raw(text)
        os.stat("\\\\?\\" + text)
        return True
    except (UnsafePathError, OSError):
        return False


def _drive_type(text):
    """Windows drive type of the path's drive letter (DRIVE_REMOTE for a
    mapped network drive); None elsewhere."""
    if os.name != "nt":
        return None
    import ctypes
    root = ntpath.splitdrive(text)[0] + "\\"
    return ctypes.WinDLL("kernel32").GetDriveTypeW(ctypes.c_wchar_p(root))


def _refuse_links(text):
    """Refuse a path that passes through a symbolic link, checked with lstat
    (which does not follow links) before anything resolves the path: a link
    could lead to a network share, and resolving it would contact the server
    and offer it Windows credentials. Directory junctions, which can only
    point at local volumes, are still allowed and checked after resolving."""
    parts = Path(text).parts
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        try:
            st = os.lstat(current)
        except OSError:
            return                   # missing: reported by the checks that follow
        if stat.S_ISLNK(st.st_mode):
            raise UnsafePathError("Recordings reached through a symbolic link are not "
                                  "accepted. Choose the file itself.")


def validate_input_file(path, allowed_ext, max_bytes, roots=None, allow_cloud=False,
                        allow_network=False):
    """Accept an existing regular file with an allowed extension and size.

    `roots` limits where the real target may live (None = anywhere local).
    Mapped network drives and symbolic links are refused unless
    allow_network is set (managed policy, #111 A5); the resolved path is
    checked again in every case. Cloud placeholders that are not on disk are
    refused with a clear message rather than silently triggering a download
    (#24 handles hydration)."""
    text = check_raw(path)
    if not allow_network:
        if _drive_type(text) == DRIVE_REMOTE:
            raise UnsafePathError(NETWORK_MESSAGE)
        _refuse_links(text)
    if roots:
        real = resolve_within(text, roots)
    else:
        real = Path(os.path.realpath(text, strict=False))
        if not allow_network:        # a mapped drive resolves to \\server\share
            try:
                check_raw(real)      # a link or mapped drive must not lead to \\server
            except UnsafePathError as exc:
                raise UnsafePathError(NETWORK_MESSAGE) from exc
    if not allow_network and _drive_type(str(real)) == DRIVE_REMOTE:
        raise UnsafePathError(NETWORK_MESSAGE)
    try:
        st = os.stat(real)
    except OSError as exc:
        if _too_long(real):
            raise UnsafePathError(TOO_LONG_MESSAGE) from exc
        raise UnsafePathError("The file could not be found.") from exc
    if not stat.S_ISREG(st.st_mode):
        raise UnsafePathError("Only regular files can be imported.")
    attrs = getattr(st, "st_file_attributes", 0)
    if not allow_cloud and attrs & (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
                                    | FILE_ATTRIBUTE_OFFLINE):
        raise CloudPlaceholderError("The file is in cloud storage and not downloaded to this "
                              "computer yet. Make it available offline, then try again.")
    if real.suffix.lower() not in {e.lower() for e in allowed_ext}:
        raise UnsafePathError(f"{real.suffix or 'This file type'} files are not supported.")
    if st.st_size == 0:
        raise UnsafePathError("The file is empty.")
    if st.st_size > max_bytes:
        raise UnsafePathError("The file is larger than this app accepts.")
    return real
