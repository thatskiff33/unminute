# workspace/location.py
# Where the workspace may be (D3): the checks that keep it on this computer,
# out of OneDrive and off network drives (#219), and refuse a data folder
# linked to a target that cannot be reached (#266); and whether a path is
# inside the real workspace folder (under_workspace_root, which keeps a test
# key protector away from it). Moved here unchanged from workspace/store.py
# (#414), which imports them back, so store.default_root(),
# store.BROKEN_LINK and the rest are the same objects as before. The
# refusals are store.StoreError, imported when a check runs: store.py
# imports this module, so this one cannot import store at its top. No path
# in any message (P3).

import os
from pathlib import Path


def local_appdata():
    """%LOCALAPPDATA%, which must be an absolute local path outside OneDrive.
    Meetings must never end up in the current folder, a synced folder or a
    network location because the variable is missing or redirected: refuse
    instead of guessing."""
    from workspace.store import StoreError
    base = os.environ.get("LOCALAPPDATA", "")
    path = Path(base)
    missing = StoreError("Unminute cannot find this account's local app data folder "
                         "(LOCALAPPDATA). Ask your IT team.")
    if not base or not path.is_absolute() or base.startswith(("\\\\", "//")):
        raise missing
    where = _synced_or_network(base)
    if where == "network":
        raise StoreError("This account's local app data folder is on a network drive. "
                         "Unminute keeps meetings on this computer. Ask your IT team.")
    if where == "onedrive":
        raise StoreError("This account's local app data folder is inside OneDrive. "
                         "Unminute keeps meetings out of synced folders. Ask your IT team.")
    return path


def _synced_or_network(path):
    """Where the real location of path is: "network" (a network share or a
    mapped network drive), "onedrive" (inside a folder a OneDrive variable
    names) or None (on this computer). Real paths are compared: short (8.3)
    names and junctions must not hide that a folder is inside OneDrive or on
    a network share. A folder that does not exist yet resolves through its
    nearest existing parent."""
    real = _real(path)
    if _network_path(real) or _remote_drive(real):
        return "network"
    resolved = _folder_key(real)
    profile = os.environ.get("USERPROFILE", "")
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        synced = os.environ.get(var, "")
        # A rooted path without a drive ("\Users\a\OneDrive") still counts:
        # Python 3.13 stopped calling it absolute, and realpath puts it on the
        # current drive.
        if not synced or not (os.path.isabs(synced) or synced.startswith(("\\", "/"))):
            continue
        synced_key = _folder_key(_real(synced))
        # A variable pointing at a drive root, or at or above the user's
        # profile, is a misconfiguration, not a synced AppData: ignore it
        # rather than refuse every normal setup.
        if os.path.dirname(synced_key.rstrip("\\/")) == synced_key.rstrip("\\/") or (
                profile and _folder_key(_real(profile)).startswith(synced_key)):
            continue
        if resolved.startswith(synced_key):
            return "onedrive"
    return None


def _real(path):
    try:
        real = os.path.realpath(path)
    except OSError:              # an error realpath does not handle itself
        return os.path.abspath(path)
    # Where the path does not exist, realpath stops resolving: for a junction
    # whose target is missing it returns the stored target as written, with
    # any short (8.3) name or junction in it left unresolved (#219 review). So
    # resolve the nearest existing parent again and add back the rest.
    head, tail = real, []
    while not os.path.exists(head):
        parent = os.path.dirname(head)
        if parent == head:       # nothing of it exists: keep what realpath gave
            return real
        tail.append(os.path.basename(head))
        head = parent
    if not tail:
        return real
    try:
        head = os.path.realpath(head)
    except OSError:
        return real
    return os.path.join(head, *reversed(tail))


def _network_path(path):
    """\\\\server\\share and \\\\?\\UNC\\ paths; not local device or volume
    paths such as \\\\?\\Volume{...} (virtual desktop profile disks)."""
    low = path.replace("/", "\\").lower()
    if low.startswith("\\\\?\\unc\\") or low.startswith("\\\\.\\unc\\"):
        return True
    if low.startswith("\\\\?\\") or low.startswith("\\\\.\\"):
        return False
    return low.startswith("\\\\")


def _folder_key(path):
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/") + os.sep


def _remote_drive(path):
    """True if the path's drive letter is a mapped network drive (Windows)."""
    if os.name != "nt":
        return False
    import ctypes
    import ntpath
    root = ntpath.splitdrive(path)[0] + "\\"
    return ctypes.WinDLL("kernel32").GetDriveTypeW(ctypes.c_wchar_p(root)) == 4  # REMOTE


# The workspace refused because its real location is not on this computer
# (#219). No path in either message (P3). The models folder is the one folder
# IT may redirect (docs/PACKAGING.md).
REDIRECTED = {
    "network": ("Unminute's workspace folder is redirected to a network drive. Unminute "
                "keeps meetings on this computer. Ask your IT team: only Unminute's models "
                "folder may be moved."),
    "onedrive": ("Unminute's workspace folder is redirected into OneDrive. Unminute keeps "
                 "meetings out of synced folders. Ask your IT team: only Unminute's models "
                 "folder may be moved."),
}

# The workspace refused because Unminute, or Unminute\workspace, is a junction
# or link whose target cannot be reached: removed, or on a drive that is not
# connected (#266). Opening it could only fail in mkdir, and the app closed
# without a message. No path in it (P3).
BROKEN_LINK = ("Unminute's workspace folder is linked to a folder that cannot be reached: "
               "it may have been removed, or be on a drive that is not connected. Ask your "
               "IT team to restore the folder or its drive, or to remove the link.")


def _broken_link(path):
    """True if path is a junction or symbolic link whose target cannot be
    reached (#266): missing, on a volume that is gone (a volume GUID or
    \\\\?\\ spelling), or itself a broken link (os.stat follows the whole
    chain). Not a folder that does not exist yet, and not a target this
    account may not open: those keep their own outcome."""
    if not (os.path.islink(path) or os.path.isjunction(path)):   # neither follows the link
        return False
    try:
        os.stat(path)
    except PermissionError:
        return False
    except OSError:
        return True
    return False


def _workspace_folder():
    # K1: the data folder's name is pinned (tests/test_consistency.py reads this line).
    return local_appdata() / "Unminute" / "workspace"


def default_root():
    """The workspace, %LOCALAPPDATA%\\Unminute\\workspace. local_appdata()
    checks %LOCALAPPDATA% itself, but a junction or link at Unminute, or at
    Unminute\\workspace, could still take the workspace into OneDrive or onto
    a network drive. So the real location of each is checked too, and the
    workspace is refused there (#219, D3). Either one being a junction or
    link whose target cannot be reached (a removed folder, or a drive that
    is not connected) is refused too, with its own message, BROKEN_LINK
    (#266): the workspace could not be created there. Only Unminute\\models
    may be redirected: it holds no meeting content, and the model cache
    (__main__.model_cache) does not come through here."""
    from workspace.store import StoreError
    root = _workspace_folder()
    for folder in (root.parent, root):
        where = _synced_or_network(folder)      # first: a missing target in OneDrive says so
        if where:
            raise StoreError(REDIRECTED[where])
        if _broken_link(folder):
            raise StoreError(BROKEN_LINK)
    return root


def under_workspace_root(path):
    """Whether path is inside the real workspace folder,
    %LOCALAPPDATA%\\Unminute\\workspace, compared as Windows does (ntpath,
    so Linux CI agrees). False only when LOCALAPPDATA is not set, as on
    Linux CI: there is no real workspace to protect then. A folder that
    default_root() refuses (in OneDrive, on a network drive, redirected or a
    broken link) still counts: a real workspace may be there on disk."""
    import ntpath
    base = os.environ.get("LOCALAPPDATA", "")
    if not base:
        return False
    root = Path(base) / "Unminute" / "workspace"      # the names of _workspace_folder (K1)
    a = ntpath.normcase(os.path.realpath(path))
    b = ntpath.normcase(os.path.realpath(root))
    try:
        return ntpath.commonpath([a, b]) == b
    except ValueError:
        return False
