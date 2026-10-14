# workspace/worker.py
# Heavy inference in a separate process (#15).
#
# The state owner (UI process) keeps the database; the worker only loads one
# model and transcribes the audio it is sent, then returns plain segments.
#   - One worker, one heavy model at a time. Loading a different model
#     replaces the previous one in the same process.
#   - The worker installs the offline network guard and lowers its own
#     priority (Quiet) before loading anything.
#   - Requests are bounded (MAX_REQUEST_SECONDS of audio) and time-limited;
#     results are text. Error messages never carry stack traces or paths.
#   - If the worker dies or hangs, the request raises WorkerCrashed; the chunk
#     it was on stays uncommitted and a new worker is started on the next
#     request (which reloads the model by itself).
#   - stop_now() terminates the worker at once (discarding only uncommitted
#     work). The stop stays in force - every request raises WorkerStopped -
#     until the next run calls begin_run(), so a stop that arrives between
#     two requests is not lost.
#   - Idle workers exit after IDLE_UNLOAD_S, releasing all model memory.

import multiprocessing as mp
import os
import re
import threading
import time

import numpy as np

SAMPLE_RATE = 16000
MAX_REQUEST_SECONDS = 120.0
IDLE_UNLOAD_S = 120.0
LOAD_TIMEOUT_S = 600.0
# A request may take this many times its audio length (plus a floor) before
# the worker is treated as hung. Generous: slow CPUs and Quiet threads.
TRANSCRIBE_TIMEOUT_FACTOR = 10.0
TRANSCRIBE_TIMEOUT_FLOOR_S = 120.0

# Quoted paths first (they may contain spaces, e.g. in a user name), then bare ones.
_PATHS = re.compile(r"'(?:[A-Za-z]:[\\/]|\\\\|/)[^']*'"
                    r'|"(?:[A-Za-z]:[\\/]|\\\\|/)[^"]*"'
                    r"|(?:[A-Za-z]:[\\/]|\\\\|/(?:home|Users|tmp)/)[^\s'\"<>|]*")


class WorkerCrashed(RuntimeError):
    """The worker process ended or hung; nothing from it was committed."""


class WorkerStopped(RuntimeError):
    """The worker was stopped on purpose (Stop now / cleanup)."""


class WorkerError(RuntimeError):
    """The worker reported an error (message is safe to show)."""


class EngineUnavailable(WorkerError):
    """The model could not be loaded (e.g. not installed yet). Not the chunk's fault."""


def safe_message(exc):
    """Exception text without file paths (they can contain the user's name)."""
    return _PATHS.sub("<path>", str(exc))[:500]


def _worker_main(conn, cache_dir, quiet, gpu_site=None):
    # Launched from pythonw there is no console: libraries that write to
    # stdout/stderr must not crash the worker.
    import sys
    if sys.stdout is None or sys.stderr is None:
        sys.stdout = sys.stderr = open(os.devnull, "w")
    # Before importing any model library: no telemetry, no network.
    import offline_guard
    offline_guard.disable_telemetry()
    offline_guard.install()
    if gpu_site:
        # The GPU worker (speaker identification only): the CUDA build of
        # PyTorch must be first on the path before anything imports torch.
        from workspace import gpu
        gpu.activate(gpu_site)
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
                    engine, loaded = None, None      # a failed load leaves nothing stale
                    engine = load_engine(name, cache_dir, threads)
                    loaded = (name, threads)
                conn.send(("ok", getattr(engine, "description", name),
                           getattr(engine, "device", None), getattr(engine, "precision", None)))
            elif op == "transcribe":
                if engine is None:
                    raise RuntimeError("No model is loaded.")
                segments, duration = engine.transcribe(msg[1])
                # Word times cross the process boundary: ownership is per word.
                conn.send(("ok", [(s.start, s.end, s.text,
                                   [tuple(w) for w in (getattr(s, "words", ()) or ())])
                                  for s in segments], duration))
            elif op == "diarize":
                _, model, audio_path, hint, threads, device = msg
                # One heavy model at a time: release the speech model first.
                had_asr = engine is not None
                engine, loaded = None, None
                import gc
                gc.collect()
                from workspace.engines import diarize_file
                turns, exclusive, device = diarize_file(model, cache_dir, audio_path, hint,
                                                        threads, device=device)
                gc.collect()
                conn.send(("ok", turns, exclusive, device, had_asr))
            elif op == "exit":
                conn.send(("ok",))
                return
            else:
                conn.send(("error", "UnknownRequest", op))
        except Exception as exc:
            # Message only: no stack traces or paths cross the process boundary.
            conn.send(("error", type(exc).__name__, safe_message(exc), op))


class WorkerHandle:
    """Owned by the state-owning process. One meeting at a time uses it."""

    def __init__(self, cache_dir, quiet=True, idle_unload_s=IDLE_UNLOAD_S, gpu_site=None):
        self.cache_dir, self.quiet, self.idle_unload_s = str(cache_dir), quiet, idle_unload_s
        self.gpu_site = gpu_site
        self._gpu = None             # companion GPU worker (speaker identification only)
        self._ctx = mp.get_context("spawn")
        self._proc = self._conn = None
        self._loaded = None          # what the CURRENT worker process has loaded
        self._wanted = None          # what the caller last asked for (reload after restart)
        self._info = None
        self._last_used = time.monotonic()
        self._lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._closed = False
        self._timer = threading.Thread(target=self._idle_loop, daemon=True)
        self._timer.start()

    # -- lifecycle ------------------------------------------------------------------

    @property
    def pid(self):
        proc = self._proc            # read once: _kill() may clear it concurrently
        return proc.pid if proc is not None and proc.is_alive() else None

    def begin_run(self):
        """Start of a new run (transcription or speakers): clears an earlier stop."""
        self._stop_requested.clear()

    def _start(self):
        parent, child = self._ctx.Pipe()
        self._proc = self._ctx.Process(target=_worker_main,
                                       args=(child, self.cache_dir, self.quiet, self.gpu_site),
                                       daemon=True, name="whisper-gpu-worker" if self.gpu_site
                                       else "whisper-worker")
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

    def gpu_worker(self, site):
        """The companion worker that loads the CUDA build of PyTorch. It is
        stopped, shut down and closed together with this one."""
        if self._gpu is None or self._gpu.gpu_site != str(site):
            if self._gpu is not None:
                self._gpu.close()
            self._gpu = WorkerHandle(self.cache_dir, quiet=self.quiet,
                                     idle_unload_s=self.idle_unload_s, gpu_site=str(site))
        self._gpu.quiet = self.quiet
        if self._stop_requested.is_set():
            self._gpu._stop_requested.set()
        else:
            self._gpu.begin_run()
        return self._gpu

    def shutdown(self):
        if self._gpu is not None:
            self._gpu.shutdown()
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
        if self._gpu is not None:
            self._gpu.close()
        self._closed = True
        self.stop_now()              # do not wait for an in-flight chunk
        with self._lock:
            self._kill()

    def stop_now(self):
        """Terminate immediately (e.g. user Stop or cleanup). In-flight work is lost
        by design; it was never committed. Safe from any thread: it flags the
        stop and terminates the process; the thread running a request (if any)
        cleans up and raises WorkerStopped, otherwise cleanup happens here.
        The stop stays in force until begin_run()."""
        self._stop_requested.set()
        if self._gpu is not None:
            self._gpu.stop_now()
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

    def _request(self, msg, timeout):
        with self._lock:
            if self._stop_requested.is_set():
                raise WorkerStopped("Stopped.")
            if self._proc is None or not self._proc.is_alive():
                self._start()
            try:
                self._conn.send(msg)
            except (OSError, EOFError) as exc:
                self._kill()
                raise WorkerCrashed("The worker could not be reached.") from exc
            deadline = time.monotonic() + timeout
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
                        cls = EngineUnavailable if reply[3:4] == ("load",) else WorkerError
                        raise cls(f"{reply[1]}: {reply[2]}")
                    return reply
                if not self._proc.is_alive():
                    self._kill()
                    if self._stop_requested.is_set():
                        raise WorkerStopped("Stopped.")
                    raise WorkerCrashed("The worker ended unexpectedly.")
                if time.monotonic() > deadline:
                    self._kill()
                    raise WorkerCrashed("The worker stopped responding.")

    def load(self, name, threads):
        """Load (or keep) a model; returns (description, device, precision)."""
        with self._lock:
            self._wanted = (name, threads)
            if self._loaded == (name, threads) and self.pid:
                return self._info
            try:
                reply = self._request(("load", name, threads), LOAD_TIMEOUT_S)
            except WorkerError:
                self._loaded = None            # the worker dropped its old model too
                raise
            self._loaded, self._info = (name, threads), tuple(reply[1:4])
            return self._info

    def transcribe(self, audio, timeout=None):
        from asr_engines import Segment
        audio = np.asarray(audio, dtype=np.float32)
        if len(audio) > MAX_REQUEST_SECONDS * SAMPLE_RATE:
            raise ValueError("Audio request too long for one worker call.")
        if timeout is None:
            timeout = max(TRANSCRIBE_TIMEOUT_FLOOR_S,
                          TRANSCRIBE_TIMEOUT_FACTOR * len(audio) / SAMPLE_RATE)
        with self._lock:
            # A worker restarted (idle unload, crash) since load(): reload first.
            if (self._loaded is None or not self.pid) and self._wanted:
                self.load(*self._wanted)
            reply = self._request(("transcribe", audio), timeout)
        return [Segment(a, b, t, tuple(tuple(w) for w in ws)) for a, b, t, ws in reply[1]], \
            reply[2]


DIARIZE_TIMEOUT_FACTOR = 3.0      # x audio length, plus a floor: generous for slow CPUs
DIARIZE_TIMEOUT_FLOOR_S = 600.0


def diarize(handle, model, audio_path, hint, threads, device="cpu", gpu_site=None):
    """Whole-meeting speaker separation in the worker. Returns
    (turns, exclusive_turns, device, asr_was_loaded_before). A stop or a hang
    ends the pass (it restarts from the beginning next time). With
    device="cuda" it runs in the companion GPU worker (gpu_site)."""
    from pathlib import Path
    seconds = Path(audio_path).stat().st_size / 4 / SAMPLE_RATE
    timeout = max(DIARIZE_TIMEOUT_FLOOR_S, DIARIZE_TIMEOUT_FACTOR * seconds)
    if device == "cuda":
        # One heavy model at a time: release the speech model in the main
        # worker before the GPU worker loads pyannote.
        handle.shutdown()
        target = handle.gpu_worker(gpu_site)
    else:
        target = handle
    with target._lock:
        try:
            reply = target._request(("diarize", model, str(audio_path), hint, threads,
                                     device), timeout)
        finally:
            # The worker released the speech model before trying, so the next
            # transcription must reload it even if speaker separation failed.
            handle._loaded = None
        return reply[1], reply[2], reply[3], reply[4]


class WorkerEngine:
    """Adapter so workspace.runner can drive a worker like an in-process engine.
    device/precision report what the worker's engine actually uses. Creating
    one starts a new run (clears any earlier Stop)."""

    def __init__(self, handle, name, threads):
        self.handle, self.name, self.threads = handle, name, threads
        self.description = self.device = self.precision = None
        handle.begin_run()

    def transcribe(self, audio):
        self.description, self.device, self.precision = self.handle.load(self.name, self.threads)
        return self.handle.transcribe(audio)
