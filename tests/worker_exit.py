# Tests: every worker process this test process started has ended before a
# test removes its temporary folder (#278). A worker still running keeps its
# crash file (logs/worker.crash) open, and Windows will not delete an open
# file, so the folder's removal would fail in tear-down instead.
import multiprocessing as mp

WORKER_NAMES = ("unminute-worker", "unminute-gpu-worker")


def join_workers(test, timeout=60.0):
    """Wait (up to timeout each) for every worker process still running, then
    fail the test if one has not ended. Call it after the workspace or handle
    is closed and before its folder is removed."""
    left = []
    for proc in mp.active_children():                # also reaps ended ones
        if proc.name in WORKER_NAMES:
            proc.join(timeout)
            if proc.is_alive():
                left.append(proc.pid)
    test.assertEqual([], left, "a worker process was still running after close")
