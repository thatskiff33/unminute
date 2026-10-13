# workspace/worker.py
# Heavy inference in a separate process (#15).
#
# The state owner (UI process) keeps the database; the worker only loads one
# model and transcribes the audio it is sent, then returns plain segments.
#   - One worker, one heavy model at a time. Loading a different model
#     replaces the previous one in the same process.
#   - The worker installs the offline network guard and lowers its own
#     priority (Quiet) before loading anything.
#   - Requests are bounded (MAX_REQUEST_SECONDS of audio); results are text.
#   - If the worker dies, the request raises WorkerCrashed; the chunk it was
#     on stays uncommitted and a new worker is started on the next request.
#   - stop_now() terminates the worker at once (discarding only uncommitted
#     work); idle workers exit after IDLE_UNLOAD_S, releasing all model memory.

import multiprocessing as mp
import threading
import time

import numpy as np

SAMPLE_RATE = 16000
MAX_REQUEST_SECONDS = 120.0
IDLE_UNLOAD_S = 120.0


class WorkerCrashed(RuntimeError):
    """The worker process ended unexpectedly; nothing from it was committed."""


class WorkerStopped(RuntimeError):
    """The worker was stopped on purpose (Stop now / cleanup)."""


class WorkerError(RuntimeError):
    """The worker reported an error (message is safe to show)."""


def _worker_main(conn, cache_dir, quiet):
    # Before importing any model library: no telemetry, no network.
    import offline_guard
    offline_guard.disable_telemetry()
    offline_guard.install()
    if quiet:
        from workspace.resources import set_priority
        set_priority()
    from workspace.engines import load_engine

    engine, loaded = None, None
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        op = msg[0]
        try:
            if op == "load":
                _, name, threads = msg
                if loaded != (name, threads):
                    engine = None
                    engine = load_engine(name, cache_dir, threads)
                    loaded = (name, threads)
                conn.send(("ok", getattr(engine, "description", name),
                           getattr(engine, "device", None), getattr(engine, "precision", None)))
            elif op == "transcribe":
                segments, duration = engine.transcribe(msg[1])
                # Word times cross the process boundary: ownership is per word.
                conn.send(("ok", [(s.start, s.end, s.text,
                                   [tuple(w) for w in (getattr(s, "words", ()) or ())])
                                  for s in segments], duration))
            elif op == "exit":
                conn.send(("ok",))
                return
            else:
                conn.send(("error", "UnknownRequest", op))
        except Exception as exc:
            # Message only: no stack traces or paths cross the process boundary.
            conn.send(("error", type(exc).__name__, str(exc)[:500]))


class WorkerHandle:
    """Owned by the state-owning process. Not thread-safe across callers:
    one meeting at a time uses it."""

    def __init__(self, cache_dir, quiet=True, idle_unload_s=IDLE_UNLOAD_S):
        self.cache_dir, self.quiet, self.idle_unload_s = str(cache_dir), quiet, idle_unload_s
        self._ctx = mp.get_context("spawn")
        self._proc = self._conn = None
        self._loaded = None
        self._last_used = time.monotonic()
        self._lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._closed = False
        self._timer = threading.Thread(target=self._idle_loop, daemon=True)
        self._timer.start()

    # -- lifecycle ------------------------------------------------------------------

    @property
    def pid(self):
        return self._proc.pid if self._proc is not None and self._proc.is_alive() else None

    def _start(self):
        parent, child = self._ctx.Pipe()
        self._proc = self._ctx.Process(target=_worker_main,
                                       args=(child, self.cache_dir, self.quiet),
                                       daemon=True, name="whisper-worker")
        self._proc.start()
        child.close()
        self._conn, self._loaded = parent, None

    def _kill(self):
        if self._proc is not None:
            if self._proc.is_alive():
                self._proc.terminate()
            self._proc.join(5)
        if self._conn is not None:
            self._conn.close()
        self._proc = self._conn = None
        self._loaded = None

    def shutdown(self):
        with self._lock:
            if self._proc is not None and self._proc.is_alive():
                try:
                    self._conn.send(("exit",))
                    if self._conn.poll(5):
                        self._conn.recv()
                except (OSError, EOFError):
                    pass
            self._kill()

    def close(self):
        self._closed = True
        self.shutdown()

    def stop_now(self):
        """Terminate immediately (e.g. user Stop or cleanup). In-flight work is lost
        by design; it was never committed. Safe from any thread: it only flags
        the stop and terminates the process; the thread running a request (if
        any) cleans up and raises WorkerStopped, otherwise cleanup happens here."""
        self._stop_requested.set()
        proc = self._proc
        if proc is not None and proc.is_alive():
            proc.terminate()
        if self._lock.acquire(blocking=False):      # no request in flight
            try:
                self._kill()
            finally:
                self._lock.release()

    def _idle_loop(self):
        while not self._closed:
            time.sleep(min(1.0, max(0.05, self.idle_unload_s / 4)))
            if self._proc is None or time.monotonic() - self._last_used <= self.idle_unload_s:
                continue
            # Never interrupt a request: only unload if no request holds the lock.
            if self._lock.acquire(blocking=False):
                try:
                    if time.monotonic() - self._last_used > self.idle_unload_s:
                        self.shutdown()
                finally:
                    self._lock.release()

    # -- requests ---------------------------------------------------------------------

    def _request(self, msg, timeout=None):
        with self._lock:
            self._stop_requested.clear()
            if self._proc is None or not self._proc.is_alive():
                self._start()
            try:
                self._conn.send(msg)
            except (OSError, EOFError) as exc:
                self._kill()
                raise WorkerCrashed("The worker could not be reached.") from exc
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                # This thread owns the pipe: stop_now() only flags the stop and
                # terminates the process, and all cleanup happens here.
                if self._stop_requested.is_set():
                    self._kill()
                    raise WorkerStopped("Stopped.")
                try:
                    ready = self._conn.poll(0.2)
                    reply = self._conn.recv() if ready else None
                except (OSError, EOFError) as exc:
                    self._kill()
                    if self._stop_requested.is_set():
                        raise WorkerStopped("Stopped.") from None
                    raise WorkerCrashed("The worker ended unexpectedly.") from exc
                if ready:
                    self._last_used = time.monotonic()
                    if reply[0] == "error":
                        raise WorkerError(f"{reply[1]}: {reply[2]}")
                    return reply
                if not self._proc.is_alive():
                    self._kill()
                    if self._stop_requested.is_set():
                        raise WorkerStopped("Stopped.")
                    raise WorkerCrashed("The worker ended unexpectedly.")
                if deadline is not None and time.monotonic() > deadline:
                    self._kill()
                    raise WorkerCrashed("The worker stopped responding.")

    def load(self, name, threads):
        """Load (or keep) a model; returns (description, device, precision)."""
        with self._lock:
            if self._loaded == (name, threads) and self.pid:
                return self._info
            reply = self._request(("load", name, threads))
            self._loaded, self._info = (name, threads), tuple(reply[1:4])
            return self._info

    def transcribe(self, audio):
        from asr_engines import Segment
        audio = np.asarray(audio, dtype=np.float32)
        if len(audio) > MAX_REQUEST_SECONDS * SAMPLE_RATE:
            raise ValueError("Audio request too long for one worker call.")
        reply = self._request(("transcribe", audio))
        return [Segment(a, b, t, tuple(tuple(w) for w in ws)) for a, b, t, ws in reply[1]], \
            reply[2]


class WorkerEngine:
    """Adapter so workspace.runner can drive a worker like an in-process engine.
    device/precision report what the worker's engine actually uses."""

    def __init__(self, handle, name, threads):
        self.handle, self.name, self.threads = handle, name, threads
        self.description = self.device = self.precision = None

    def transcribe(self, audio):
        self.description, self.device, self.precision = self.handle.load(self.name, self.threads)
        return self.handle.transcribe(audio)
