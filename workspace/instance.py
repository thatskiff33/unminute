# workspace/instance.py
# The workspace lock (#111 B5, the owner's decision B5-10, 21 Oct 2026).
#
# The app's single-instance mutex (Local\Unminute.Workspace) is per Windows
# session, so the same user signed in twice (a remote desktop session beside
# the console, say) could open the same workspace twice (D3). While the app
# runs, it also holds an exclusive lock on the first byte of
# workspace\instance.lock, which every session of the user sees. The lock is
# taken before the database check and the store open, so a storage-format
# migration only ever runs while it is held.
#
# The file is empty: no content, not a blocker, kept between runs. Windows
# releases the lock when the process ends, even after a crash. Standard
# library only.

import os

NAME = "instance.lock"
MESSAGE = ("Unminute is already open in another Windows session of yours. "
           "Close it there first.")

if os.name == "nt":
    import msvcrt

    def _lock(fd):
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)     # fails at once if held

    def _unlock(fd):
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:                                               # Linux, for the tests
    import fcntl

    def _lock(fd):
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0)

    def _unlock(fd):
        fcntl.lockf(fd, fcntl.LOCK_UN, 1, 0)


class Held(Exception):
    """Another process, in this or another Windows session, holds the lock."""

    def __init__(self):
        super().__init__(MESSAGE)


class Lock:
    """The held lock. release() (or the process ending) lets it go."""

    def __init__(self, fd):
        self._fd = fd

    def release(self):
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _unlock(fd)
        except OSError:
            pass                                    # closing releases it anyway
        finally:
            os.close(fd)

    def __del__(self):
        self.release()


def acquire(root):
    """Lock root\\instance.lock for this process. Returns a Lock; raises Held
    when another process holds it, and OSError when the file cannot be
    opened at all. The handle is not inherited by child processes."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    fd = os.open(os.path.join(root, NAME), flags, 0o600)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        _lock(fd)
    except OSError:
        os.close(fd)
        raise Held() from None
    return Lock(fd)
