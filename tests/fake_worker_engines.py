# Test-only engines loaded inside the worker process via "test:fake_worker_engines:<name>".
# Like the real engines they return segments with word times (ownership is per word).
import os
import socket
import time

from asr_engines import Segment

SR = 16000


def _seg(start, end, text):
    return Segment(start, end, text, ((start, end, " " + text.split()[0]),))


class _Engine:
    description = "fake engine (test)"
    device, precision = "cpu", "test"

    def __init__(self, fn):
        self.fn = fn

    def transcribe(self, audio):
        return self.fn(audio), len(audio) / SR


def echo(threads):
    return _Engine(lambda a: [_seg(0.0, len(a) / SR, f"n={len(a)} threads={threads} "
                                                     f"pid={os.getpid()}")])


def crash_always(threads):
    def run(a):
        os._exit(3)
    return _Engine(run)


def crash_once(threads):
    marker = os.environ["WHISPER_TEST_MARKER"]

    def run(a):
        if not os.path.exists(marker):
            open(marker, "w").close()
            os._exit(3)
        return [_seg(0.0, 0.5, "recovered")]
    return _Engine(run)


def sleepy(threads):
    def run(a):
        time.sleep(30)
        return []
    return _Engine(run)


def load_fails(threads):
    raise RuntimeError("model files failed verification")


def network(threads):
    def run(a):
        try:
            socket.create_connection(("192.0.2.1", 443), timeout=2)
            return [_seg(0.0, 0.1, "connected")]
        except OSError as exc:
            return [_seg(0.0, 0.1, f"blocked: {type(exc).__name__}")]
    return _Engine(run)
