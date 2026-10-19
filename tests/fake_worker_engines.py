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


def _sentences(word):
    def run(a):
        out, t, dur = [], 0.5, len(a) / SR
        while t + 3.0 <= dur:
            out.append(Segment(t, t + 3.0, f"{word} {t:.1f}", ((t, t + 3.0, " " + word),)))
            t += 4.0
        return out
    return run


def sentences(threads):
    """A 3 s sentence every 4 s, like a steady speaker."""
    return _Engine(_sentences("sentence"))


def sentences_v2(threads):
    """Same timing, different words: a different model for retranscription."""
    return _Engine(_sentences("second"))


def crash_always(threads):
    def run(a):
        os._exit(3)
    return _Engine(run)


def crash_once(threads):
    marker = os.environ["UNMINUTE_TEST_MARKER"]

    def run(a):
        if not os.path.exists(marker):
            open(marker, "w").close()
            os._exit(3)
        return [_seg(0.0, 0.5, "recovered")]
    return _Engine(run)


MARKER = "Zqmarker"           # tests/test_diagnostics.py: must never reach a log


def marker_words(threads):
    """Transcript text carrying the marker word (diagnostics tests)."""
    return _Engine(_sentences(MARKER))


def raises_marker(threads):
    """Fails with the marker in its message and a path, as content could."""
    def run(a):
        raise RuntimeError(f"{MARKER} secret words from C:\\Users\\{MARKER}\\{MARKER}.wav")
    return _Engine(run)


def _access_violation():
    """Read address 0 in C: a real access violation, as a broken native
    library causes. (faulthandler._sigsegv raises a C signal instead, which
    Windows delivers only to the main thread's handler; ctypes.string_at(0)
    is caught by ctypes and becomes a Python error.)"""
    import faulthandler
    faulthandler._read_null()


def native_crash(threads):
    """A native crash (access violation), as a broken library would cause."""
    def run(a):
        _access_violation()
    return _Engine(run)


def crash_after_reply(threads):
    """Replies, then crashes natively a moment later, while idle."""
    def run(a):
        import threading

        def later():
            time.sleep(0.3)
            _access_violation()
        threading.Thread(target=later, daemon=True).start()
        return [_seg(0.0, 0.5, "replied")]
    return _Engine(run)


def sleepy(threads):
    def run(a):
        time.sleep(30)
        return []
    return _Engine(run)


def load_fails(threads):
    raise RuntimeError("model files failed verification")


def speech_not_ready(threads):
    """The real load of Parakeet v2 from an empty model folder: the worker's own
    "not ready" error and its advice (#257)."""
    import tempfile

    import asr_engines
    from workspace.engines import load_engine
    with tempfile.TemporaryDirectory() as empty:
        return load_engine(asr_engines.PARAKEET_V2, empty, threads)


def weights_only_env(threads):
    """Reports the worker's torch.load switches (#218)."""
    seen = {k: os.environ.get(k) for k in ("TORCH_FORCE_WEIGHTS_ONLY_LOAD",
                                           "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD")}
    return _Engine(lambda a: [_seg(0.0, 0.1, f"env {seen}")])


# -- fake diarizers: diarize_file("test:fake_worker_engines:<name>", ...) --------

def fake_diarizer(audio, hint):
    """Speakers alternate every 10 s (A, B, A, ...); with a hint of 3 they
    rotate A, B, C. 25-27 s also has B talking over A (overlap evidence)."""
    labels = ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"][: (hint or 2)]
    dur = len(audio) / SR
    exclusive = [(t, min(t + 10.0, dur), labels[int(t // 10) % len(labels)])
                 for t in range(0, int(dur), 10)]
    turns = list(exclusive) + [(25.0, 27.0, labels[1])]
    return turns, exclusive, "cpu"


def diarizer_fails(audio, hint):
    raise RuntimeError("speaker model failed verification")


def speaker_not_ready(audio, hint):
    """The real speaker identification load from an empty model folder: the
    worker's own error and its advice (#257)."""
    import tempfile
    from pathlib import Path

    from workspace.engines import diarize_file
    with tempfile.TemporaryDirectory() as empty:
        path = Path(empty) / "audio.f32"
        audio.astype("<f4").tofile(path)
        return diarize_file("pyannote", empty, path, hint, 1)


def diarizer_sleepy(audio, hint):
    time.sleep(30)
    return [], [], "cpu"


def network(threads):
    def run(a):
        try:
            socket.create_connection(("192.0.2.1", 443), timeout=2)
            return [_seg(0.0, 0.1, "connected")]
        except OSError as exc:
            return [_seg(0.0, 0.1, f"blocked: {type(exc).__name__}")]
    return _Engine(run)


# -- fake download process (workspace.setup.Downloads target) ----------------------

def fake_download(conn, name, cache_dir):
    """Reports what it received; downloads nothing. The token arrives by pipe."""
    import sys
    token = conn.recv()
    conn.send(("log", "Downloading (test)..."))
    in_argv = bool(token) and any(token in a for a in sys.argv)
    in_env = bool(token) and any(token in v for v in os.environ.values())
    conn.send(("done", {"token_received": bool(token), "in_argv": in_argv, "in_env": in_env}))


def fake_download_crashes(conn, name, cache_dir):
    """Starts, then crashes natively part-way (diagnostics tests)."""
    conn.recv()
    conn.send(("log", "Downloading (test)..."))
    _access_violation()


def fake_download_fails(conn, name, cache_dir):
    conn.recv()
    conn.send(("error", "Could not reach Hugging Face. Check the internet connection and "
                        "try again."))
