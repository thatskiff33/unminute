# workspace/safe_paths.py
# Validate file paths that come from the UI or from the file system before the
# app reads them. The web UI never supplies a path the backend trusts as-is:
#   - relative, UNC (\\server\share), device (\\?\, \\.\) and NUL-containing
#     paths are refused;
#   - the path is resolved with symlinks and junctions followed, and the real
#     target must still lie inside an allowed root (no junction escapes);
#   - only regular files with an allowed extension and size are accepted.
# Stdlib only.

import os
import stat
from pathlib import Path

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000   # cloud placeholder not on disk
FILE_ATTRIBUTE_OFFLINE = 0x1000


class UnsafePathError(ValueError):
    """The path was refused. The message is safe to show to the user."""


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
    if text.startswith(("\\\\", "//")):
        raise UnsafePathError("Network and device paths are not accepted.")
    if not os.path.isabs(text):
        raise UnsafePathError("A full path is required.")
    return text


def resolve_within(path, roots):
    """Resolve `path` (following symlinks/junctions) and require the real
    target to sit inside one of `roots` (also resolved). Returns the real path."""
    text = check_raw(path)
    try:
        real = os.path.realpath(text, strict=True)
    except OSError as exc:
        raise UnsafePathError("The file could not be found.") from exc
    real_roots = [os.path.realpath(os.fspath(r)) for r in roots]
    if not any(_is_within(real, r) for r in real_roots):
        raise UnsafePathError("The file is outside the folders this app may read.")
    return Path(real)


def validate_input_file(path, allowed_ext, max_bytes, roots=None):
    """Accept an existing regular file with an allowed extension and size.

    `roots` limits where the real target may live (None = anywhere local).
    Cloud placeholders that are not on disk are refused with a clear message
    rather than silently triggering a download (#24 handles hydration)."""
    text = check_raw(path)
    real = resolve_within(text, roots) if roots else Path(os.path.realpath(text, strict=False))
    try:
        st = os.stat(real)
    except OSError as exc:
        raise UnsafePathError("The file could not be found.") from exc
    if not stat.S_ISREG(st.st_mode):
        raise UnsafePathError("Only regular files can be imported.")
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_OFFLINE):
        raise UnsafePathError("The file is in cloud storage and not downloaded to this "
                              "computer yet. Make it available offline, then try again.")
    if real.suffix.lower() not in {e.lower() for e in allowed_ext}:
        raise UnsafePathError(f"{real.suffix or 'This file type'} files are not supported.")
    if st.st_size == 0:
        raise UnsafePathError("The file is empty.")
    if st.st_size > max_bytes:
        raise UnsafePathError("The file is larger than this app accepts.")
    return real
