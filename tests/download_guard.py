# tests/download_guard.py
# No test starts a real model download (#382). While #368 was written, a test
# let a starting Workspace resume a saved download queue before it swapped in
# a fake, and the real download process fetched public model files from
# Hugging Face. tools/run_tests.py (the only way the tests run) installs this
# guard for the whole run, after discovery and before the first test:
#
#   - workspace.setup._download_main, the code the download process runs, is
#     replaced by REFUSED. A Downloads built without a fake target (a
#     Workspace's default) takes REFUSED instead, and handing it to a new
#     process (pickling it, in the app process, before the child is sent
#     anything) or calling it raises RealDownloadRefused and records the
#     attempt, so nothing reaches the network;
#   - the runner marks the test during which an attempt happened as an error
#     naming the cause, even when the exception was raised in a background
#     thread or swallowed, and fails the run (see tools/run_tests.py);
#   - after each test the runner checks the guard is still in place: a test
#     that replaced setup._download_main and did not put it back is an error,
#     and the guard goes back.
#
# A test that fakes the download process patches setup._download_main (as
# tests/test_download_queue.py does) or passes a fake target to Downloads; both
# work as before. The tests that check the real download process on purpose,
# in this process and with no network (tests/test_download_process.py), opt in
# with `with download_guard.real_download_process():`, which says so where it
# is used.
#   python tools/run_tests.py --only test_download_guard

import contextlib
import sys
import threading
from unittest import mock

MESSAGE = ("A test started the real model download process (workspace.setup._download_main), "
           "which would download from Hugging Face. Tests never download (#382): pass a fake "
           "target to setup.Downloads or patch setup._download_main with a fake from "
           "tests/fake_worker_engines.py. A test that checks the real download process on "
           "purpose, with no network, uses download_guard.real_download_process().")
REMOVED = ("The test replaced workspace.setup._download_main and did not put it back, so the "
           "tests after it were no longer guarded against a real download (#382). Patch it "
           "with mock.patch.object in setUp and stop the patch in a cleanup. The guard is back.")

current = None            # the id of the test running now, set by the runner
REAL = None               # the real _download_main, for real_download_process()
_attempts = []            # [(test id or None, how)], until take()
_lock = threading.Lock()


class RealDownloadRefused(AssertionError):
    """A test tried to start the real download process."""


def _refuse(how):
    test = current
    with _lock:
        _attempts.append((test, how))
    return RealDownloadRefused(f"{MESSAGE} ({how}, during {test or 'no test'})")


class _Refused:
    """Stands in for setup._download_main while the tests run."""

    def __call__(self, *args, **kwargs):
        raise _refuse("called")

    def __reduce_ex__(self, protocol):
        # Downloads hands the target to the download process by pickling it
        # when the process starts: refused here, in the app process.
        raise _refuse("handed to a download process")

    def __repr__(self):
        return "<download_guard.REFUSED: no real downloads in tests (#382)>"


REFUSED = _Refused()


def _is_real(function):
    return (getattr(function, "__module__", None) == "workspace.setup"
            and getattr(function, "__name__", None) == "_download_main")


def install():
    """Replace the real download process for the rest of this process."""
    global REAL
    from workspace import setup
    if setup._download_main is REFUSED:
        return
    if not _is_real(setup._download_main):
        raise RuntimeError("download_guard.install() found a fake already in place")
    REAL = setup._download_main
    setup._download_main = REFUSED


def installed():
    """Whether the guard is in place in the setup module loaded now."""
    setup = sys.modules.get("workspace.setup")
    return setup is not None and setup._download_main is REFUSED


def check():
    """After each test: None while the guard is in place, otherwise the
    problem, with the guard put back. A setup module imported afresh (its
    own real _download_main) is guarded the same way."""
    global REAL
    setup = sys.modules.get("workspace.setup")
    if setup is None or REAL is None or setup._download_main is REFUSED:
        return None
    if _is_real(setup._download_main):
        REAL = setup._download_main
    setup._download_main = REFUSED
    return REMOVED


def take():
    """The attempts recorded since the last call, as [(test id, how)]."""
    with _lock:
        found = list(_attempts)
        _attempts.clear()
    return found


@contextlib.contextmanager
def real_download_process():
    """The real setup._download_main, on purpose, for the length of the block,
    then the guard again. Only for tests that check the download process
    itself in this process, with no network (tests/test_download_process.py).
    Without the guard (a run not through tools/run_tests.py) it changes
    nothing."""
    from workspace import setup
    real = REAL if setup._download_main is REFUSED else setup._download_main
    with mock.patch.object(setup, "_download_main", real):
        yield real
