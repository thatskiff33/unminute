# workspace/worker.py
# Heavy inference in a separate process (#15).
#
# The state owner (UI process) keeps the database; the worker only loads one
# model and transcribes the audio it is sent, then returns plain segments.
#   - One worker, one heavy model at a time. Loading a different model
#     replaces the previous one in the same process.
#   - The worker installs the offline network guard, makes every torch.load
#     weights-only (WEIGHTS_ONLY) and lowers its own priority (Quiet) before
#     loading anything.
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
import signal
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


GPU_TIMEOUT_FLOOR_S = 300
GPU_TIMEOUT_FACTOR = 1.0

# Every torch.load in a worker unpickles weights only, even where a library
# passes weights_only=False (#111 B9, #218): a second layer behind
# workspace/speaker_load.py, which already loads that way, and cover for any
# torch.load a dependency adds later. Forced, like the telemetry switches; the
# opposite switch, if inherited, is removed (with both set, torch refuses
# every load).
WEIGHTS_ONLY = {"TORCH_FORCE_WEIGHTS_ONLY_LOAD": "1"}
NOT_WEIGHTS_ONLY = "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"

# Environment variables a locked speech library reads when it loads to add a
# folder outside the app to the DLL search path (T25, #357). CTranslate2
# 4.8.3's __init__ adds %HIP_PATH%\bin when no ROCm wheel is installed, so a
# DLL placed there could load into the worker (in the Whisper GPU worker that
# path is all Windows searches by name). Removed from every worker before any
# model library is imported; the app's own process keeps them. ONNX Runtime
# 1.30.0 reads none. DLL_PATH_CHECKED_CTRANSLATE2 is the CTranslate2 version
# this list was checked against: tests/test_consistency.py fails when the lock
# moves CTranslate2 until its __init__ is read again, this list updated and
# that version moved with it.
DLL_PATH_VARIABLES = ("HIP_PATH",)
DLL_PATH_CHECKED_CTRANSLATE2 = "4.8.3"


# A worker whose pipe has just closed is ending: wait this long for it, so its
# own exit code (a crash's) is kept rather than replaced by terminate()'s.
CRASH_GRACE_S = 2.0

# A worker told to end is waited for this long before its crash file is read:
# until it has ended, Windows keeps that file open, so it cannot be deleted
# (nor the folder holding it). A loaded machine can take more than a few
# seconds to end a process (#278).
END_WAIT_S = 30.0
# Stop now waits at most this long, all its workers together: it can run while
# the app holds its own lock (pause, discard). A worker still ending then is
# left to end by itself, and cleanup that finds a file still open retries (C1).
STOP_WAIT_S = 5.0


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


def _start_up(log_dir, log_name, quiet, gpu_site=None, gpu_component=None):
    """A worker process's start-up, before any model library is imported:
    its log, no telemetry, the offline guard (N1), weights-only loading, no
    DLL folder named by the environment (T25) and, if quiet, below-normal
    priority. Shared by the main, GPU and draft
    workers. Returns (log, the device it runs speech models on)."""
    # Launched from pythonw there is no console: libraries that write to
    # stdout/stderr must not crash the worker. What they write is free text,
    # which can include meeting content (P3), so it is never kept: with a log,
    # only the error and warning type names are counted (R9). Unexpected
    # errors and native crashes go to the worker's own log.
    import sys
    from workspace import diag
    log = diag.Log(log_dir, log_name) if log_dir else None
    if sys.stdout is None or sys.stderr is None:
        if log is not None:
            sys.stdout, sys.stderr = diag.OutputSummary(log, "stdout"), \
                diag.OutputSummary(log, "stderr")
        else:
            sys.stdout = sys.stderr = open(os.devnull, "w")
    if log is not None:
        diag.use(log)
    diag.quiet_crashes()                 # no Windows Error Reporting dialog (T21)
    try:
        # Before importing any model library: no telemetry, no network.
        from workspace import offline_guard
        offline_guard.disable_telemetry()
        offline_guard.install()
        os.environ.update(WEIGHTS_ONLY)
        os.environ.pop(NOT_WEIGHTS_ONLY, None)
        for name in DLL_PATH_VARIABLES:  # no DLL folder named by the environment (#357)
            os.environ.pop(name, None)
        # ONNX Runtime (Parakeet, and the voice detection Whisper uses) has its own
        # Windows telemetry events, switched off by a call rather than a variable.
        offline_guard.disable_onnxruntime_telemetry()
        asr_device = "cpu"         # where this worker runs speech models; never rebound
        if gpu_site:
            from workspace import gpu
            if gpu_component == gpu.WHISPER_COMPONENT:
                # The Whisper GPU worker (#248): the CUDA 12 cuBLAS component
                # only, loaded by full path; never the PyTorch CUDA 13 folder.
                gpu.activate_whisper(gpu_site)
                asr_device = "cuda"
            else:
                # The speaker GPU worker: the CUDA build of PyTorch must be
                # first on the path before anything imports torch.
                gpu.activate(gpu_site)
        if quiet:
            from workspace.resources import set_priority
            set_priority()
    except BaseException as exc:
        # multiprocessing prints a failed start to stderr, never to excepthook.
        diag.incident("worker_start", exc)
        raise
    return log, asr_device


def _worker_main(conn, cache_dir, quiet, gpu_site=None, allow_test_engines=False,
                 log_dir=None, log_name="worker", gpu_component=None):
    log, asr_device = _start_up(log_dir, log_name, quiet, gpu_site, gpu_component)
    from workspace import diag
    try:
        from workspace.engines import load_engine
    except BaseException as exc:
        diag.incident("worker_start", exc)
        raise

    engine, loaded = None, None
    while True:
        if log is not None:
            log.limit_crash_file()           # recovered native errors stay small
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            engine = loaded = None           # released while crashes are still captured
            _end_crash_capture(log)
            return
        op = msg[0]
        try:
            if op == "load":
                _, name, threads = msg
                if loaded != (name, threads):
                    engine, loaded = None, None      # a failed load leaves nothing stale
                    engine = load_engine(name, cache_dir, threads,
                                         allow_test=allow_test_engines,
                                         device=asr_device)
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
                _, model, audio, hint, threads, wanted = msg
                del msg                      # holds the meeting's audio key
                # One heavy model at a time: release the speech model first.
                had_asr = engine is not None
                engine, loaded = None, None
                import gc
                gc.collect()
                try:
                    waveform = _read_audio(audio)
                finally:
                    del audio                # the key is not kept (#111 C3a)
                from workspace.engines import diarize_audio
                turns, exclusive, ran_on = diarize_audio(model, cache_dir, waveform, hint,
                                                         threads, device=wanted,
                                                         allow_test=allow_test_engines)
                del waveform
                gc.collect()
                conn.send(("ok", turns, exclusive, ran_on, had_asr))
            elif op == "exit":
                # The model is released, and the crash file closed, before the
                # answer: a crash in the model's teardown is still captured,
                # and the app reads and deletes the file however long this
                # process then takes to end.
                engine = loaded = None
                _end_crash_capture(log)
                conn.send(("ok",))
                return
            else:
                conn.send(("error", "UnknownRequest", op))
        except Exception as exc:
            # Message only: no stack traces or paths cross the process boundary.
            # The type and frames (never the message) go to the worker's log,
            # under a code that reaches the user with the message.
            code = diag.incident("worker", exc, op=op)
            conn.send(("error", type(exc).__name__, safe_message(exc), op, code))


def _end_crash_capture(log):
    """The worker's work is done and its model released: free what the model
    left (native teardown, still under faulthandler), then close the crash
    file (#278)."""
    import gc
    gc.collect()
    if log is not None:
        log.stop_crash_capture()


def _read_audio(audio):
    """The whole decoded meeting audio for speaker identification, from the
    encrypted file named in a diarize request: (path, key, context), checked
    here. Read block by block (each authenticated) into one float32 array;
    the file is closed before the model runs. An AudioCryptError goes back
    as an ordinary error: its message never names the file or the key. The
    context holds text only, as the meeting audio's does; a context with
    other parts (C3b's recorder files) widens this check deliberately."""
    if not (isinstance(audio, tuple) and len(audio) == 3):
        raise ValueError("The speaker identification request is not valid.")
    path, key, context = audio
    if not (isinstance(path, str) and isinstance(key, bytes) and len(key) == 32
            and isinstance(context, tuple) and context
            and all(isinstance(p, str) for p in context)):
        raise ValueError("The speaker identification request is not valid.")
    from workspace import audiocrypt
    with audiocrypt.AudioReader(path, key, context) as reader:
        if (reader.sample_format, reader.channels, reader.rate) != ("float32", 1, SAMPLE_RATE):
            raise audiocrypt.AudioCryptError("The audio file is damaged (header).")
        waveform = np.empty(reader.frames, dtype=np.float32)
        reader.readinto(waveform, 0)
    return waveform


def draft_main(conn, cache_dir, model, threads, allow_test_engines=False, log_dir=None):
    """The draft worker (#104): live drafts while recording, in its own
    process with the same start-up as the main worker, always at
    below-normal priority. workspace.drafts.serve runs its loop."""
    from workspace import diag, drafts
    log, _ = _start_up(log_dir, drafts.LOG_NAME, quiet=True)
    try:
        from workspace.engines import load_draft_engine
    except BaseException as exc:
        diag.incident("worker_start", exc)
        raise
    drafts.serve(conn, lambda: load_draft_engine(model, cache_dir, threads,
                                                 allow_test=allow_test_engines), log)
    # The pipe closed or "exit" came and serve has returned. Its transcribe
    # thread (a daemon) may still be inside the engine with a phrase; it ends
    # with the process. The crash file is closed before the process ends, as
    # the main worker's is (#278).
    _end_crash_capture(log)


class WorkerHandle:
    """Owned by the state-owning process. One meeting at a time uses it."""

    def __init__(self, cache_dir, quiet=True, idle_unload_s=IDLE_UNLOAD_S, gpu_site=None,
                 allow_test_engines=False, log=None, gpu_component=None):
        self.cache_dir, self.quiet, self.idle_unload_s = str(cache_dir), quiet, idle_unload_s
        self.gpu_site = gpu_site
        # Which component a GPU worker loads: None (speaker identification,
        # PyTorch CUDA 13) or gpu.WHISPER_COMPONENT (Whisper, cuBLAS 12).
        self.gpu_component = gpu_component
        # The app's diagnostics log (workspace.diag.Log): the worker writes its
        # own log beside it, and a worker that dies is recorded here.
        self.log = log
        # Both GPU workers write gpu-worker.log (diag.LOG_NAME): they never run
        # at the same time, since each starts only after the other is shut down.
        self._log_name = "gpu-worker" if gpu_site else "worker"
        self.allow_test_engines = bool(allow_test_engines)   # tests only (engines.TEST_PREFIX)
        self._gpu = None             # companion GPU worker (speaker identification only)
        self._gpu_whisper = None     # companion GPU worker (Whisper only, #248)
        self._ctx = mp.get_context("spawn")
        self._proc = self._conn = None
        self._loaded = None          # what the CURRENT worker process has loaded
        self._wanted = None          # what the caller last asked for (reload after restart)
        self._info = None
        self._last_used = time.monotonic()
        self._lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._closed = False
        self._wake = threading.Event()   # the idle loop re-reads its deadline when set
        self.idle_wakeups = 0            # idle loop iterations (tests, diagnostics)
        self._timer = threading.Thread(target=self._idle_loop, daemon=True)
        self._timer.start()

    # -- lifecycle ------------------------------------------------------------------

    @property
    def pid(self):
        proc = self._proc            # read once: _kill() may clear it concurrently
        return proc.pid if proc is not None and proc.is_alive() else None

    def begin_run(self):
        """Start of a new run (transcription or speakers): clears an earlier stop.
        Once the handle is closed the stop stays: a run that begins while the
        app closes never starts a worker that nothing would end (#278)."""
        if not self._closed:
            self._stop_requested.clear()

    def _start(self):
        parent, child = self._ctx.Pipe()
        log_dir = str(self.log.folder) if self.log is not None else None
        self._proc = self._ctx.Process(target=_worker_main,
                                       args=(child, self.cache_dir, self.quiet, self.gpu_site,
                                             self.allow_test_engines, log_dir, self._log_name,
                                             self.gpu_component),
                                       daemon=True, name="unminute-gpu-worker" if self.gpu_site
                                       else "unminute-worker")
        self._proc.start()
        child.close()
        self._conn, self._loaded = parent, None
        self._wake.set()                 # the idle loop now has a worker to watch

    def _kill(self, grace=0.0, wait=END_WAIT_S):
        """End the worker. grace: when it looks to be dying already, let it
        finish for that long first, so its own exit code (and crash file) are
        kept rather than replaced by ours. wait: how long to wait for it to
        end once terminated."""
        if self._proc is not None:
            if grace and self._proc.is_alive():
                self._proc.join(grace)
            if self._proc.is_alive():
                self._proc.terminate()
            self._proc.join(max(0.0, wait))
            # Ended by itself: not by terminate() (-SIGTERM), a stop or a clean exit.
            self._note_exit(died=not self._stop_requested.is_set()
                            and self._proc.exitcode not in (0, None, -signal.SIGTERM))
        if self._conn is not None:
            self._conn.close()
        self._proc = self._conn = None
        self._loaded = None

    def _note_exit(self, died):
        """A worker that ended by itself (not stopped, unloaded or closed) is
        recorded in the app's log, with its native crash if it left one."""
        if self.log is None:
            return
        crash = self.log.folder / f"{self._log_name}.crash"
        code = self.log.collect_crash(died, crash_path=crash, process=self._log_name)
        if died:
            self.log.write("worker_exit", process=self._log_name,
                           exitcode=self._proc.exitcode, code=code)

    def gpu_worker(self, site):
        """The companion worker that loads the CUDA build of PyTorch. It is
        stopped, shut down and closed together with this one."""
        return self._companion("_gpu", site, None)

    def whisper_gpu_worker(self, site, component):
        """The companion worker that runs Whisper on the graphics card with
        the cuBLAS component (component: gpu.WHISPER_COMPONENT). It is
        stopped, shut down and closed together with this one."""
        return self._companion("_gpu_whisper", site, component)

    def _companion(self, attr, site, component):
        current = getattr(self, attr)
        if current is None or current.gpu_site != str(site):
            if current is not None:
                current.close()
            current = WorkerHandle(self.cache_dir, quiet=self.quiet,
                                   idle_unload_s=self.idle_unload_s, gpu_site=str(site),
                                   allow_test_engines=self.allow_test_engines, log=self.log,
                                   gpu_component=component)
            setattr(self, attr, current)
            if self._closed:
                current.close()          # nothing would close it later (#278)
        current.quiet = self.quiet
        # Clear the companion's old stop first, then carry over a stop that is
        # in force now, so a stop that lands meanwhile is never lost.
        current.begin_run()
        if self._stop_requested.is_set():
            current._stop_requested.set()
        return current

    def _companions(self):
        return [c for c in (self._gpu, self._gpu_whisper) if c is not None]

    def release_gpu(self, which=None):
        """Stop a GPU worker and free its graphics memory (after a GPU
        failure, or before its component is replaced or removed). which:
        "speakers", "whisper", or None for both."""
        if which in (None, "speakers"):
            companion, self._gpu = self._gpu, None
            if companion is not None:
                companion.close()
        if which in (None, "whisper"):
            companion, self._gpu_whisper = self._gpu_whisper, None
            if companion is not None:
                companion.close()

    def shutdown_whisper_gpu(self):
        """End the Whisper GPU worker's process (it restarts when next used),
        so its model leaves graphics and main memory before another heavy
        model loads (D4: one heavy model at a time)."""
        companion = self._gpu_whisper
        if companion is not None:
            companion.shutdown()

    def shutdown_for_whisper_gpu(self):
        """Before Whisper loads on the graphics card: end this worker and the
        speaker GPU worker, so their models leave memory (D4), and keep the
        Whisper GPU worker and the model it may already hold."""
        companion = self._gpu
        if companion is not None:
            companion.shutdown()
        self._shutdown_own()

    def shutdown(self):
        for companion in self._companions():   # read once: release_gpu may clear them
            companion.shutdown()
        self._shutdown_own()

    def _shutdown_own(self):
        with self._lock:
            grace = 0.0
            if self._proc is not None and self._proc.is_alive():
                try:
                    self._conn.send(("exit",))
                    if self._conn.poll(5):
                        self._conn.recv()
                except (OSError, EOFError):
                    # It ended without answering (a crash while releasing its
                    # model): keep its own exit code, as a request does.
                    grace = CRASH_GRACE_S
            self._kill(grace=grace)

    def close(self, deadline=None):
        """End this worker and its companions for good. Every process is told
        to end first, then all are waited for: END_WAIT_S at most in all, not
        for each worker. An in-flight chunk is not waited for."""
        if deadline is None:
            deadline = time.monotonic() + END_WAIT_S
        self._closed = True
        self._wake.set()
        self._terminate()
        for companion in self._companions():
            companion.close(deadline)
        with self._lock:
            self._kill(wait=deadline - time.monotonic())

    def _terminate(self):
        """Flag the stop and terminate this worker and its companions, waiting
        for none of them."""
        self._stop_requested.set()
        for companion in self._companions():
            companion._terminate()
        proc = self._proc
        if proc is not None and proc.is_alive():
            proc.terminate()

    def stop_now(self, deadline=None):
        """Terminate immediately (e.g. user Stop or cleanup). In-flight work is lost
        by design; it was never committed. Safe from any thread: it flags the
        stop and terminates the process; the thread running a request (if any)
        cleans up and raises WorkerStopped, otherwise cleanup happens here,
        waiting STOP_WAIT_S at most for this worker and its companions together.
        The stop stays in force until begin_run()."""
        if deadline is None:
            deadline = time.monotonic() + STOP_WAIT_S
        self._terminate()
        for companion in self._companions():
            companion.stop_now(deadline)
        if self._lock.acquire(blocking=False):      # no request in flight
            try:
                self._kill(wait=deadline - time.monotonic())
            finally:
                self._lock.release()

    def _idle_loop(self):
        # Sleeps until the worker has been idle for idle_unload_s, and not at all
        # while there is no worker process: _start() and close() wake it. The
        # event is cleared before any state is read, _closed included, so a
        # wake-up is never lost.
        while True:
            self._wake.clear()
            if self._closed:
                return
            self.idle_wakeups += 1
            if self._proc is None:
                self._wake.wait()
                continue
            due = self._last_used + self.idle_unload_s - time.monotonic()
            if due > 0:
                self._wake.wait(min(due + 0.05, threading.TIMEOUT_MAX))
            elif self._lock.acquire(blocking=False):
                # Never interrupt a request: only unload if no request holds the lock.
                try:
                    if (self._proc is not None
                            and time.monotonic() - self._last_used > self.idle_unload_s):
                        self.shutdown()
                finally:
                    self._lock.release()
            else:
                # A request is running: wait for it to end (a stop or close ends
                # it too), then look again with its new last-used time.
                with self._lock:
                    pass

    # -- requests ---------------------------------------------------------------------

    def _request(self, msg, timeout):
        with self._lock:
            if self._stop_requested.is_set():
                raise WorkerStopped("Stopped.")
            if self._proc is not None and not self._proc.is_alive():
                self._kill()                 # it ended while idle: noted before a new one
            if self._proc is None:
                if self._closed:
                    # A run that began just as the handle closed (begin_run read
                    # _closed before close() set it): never start a worker then.
                    raise WorkerStopped("Closed.")
                self._start()
            try:
                self._conn.send(msg)
            except (OSError, EOFError) as exc:
                self._kill(grace=CRASH_GRACE_S)
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
                    self._kill(grace=CRASH_GRACE_S)
                    if self._stop_requested.is_set():
                        raise WorkerStopped("Stopped.") from None
                    raise WorkerCrashed("The worker ended unexpectedly.") from exc
                if ready:
                    self._last_used = time.monotonic()
                    if reply[0] == "error":
                        cls = EngineUnavailable if reply[3:4] == ("load",) else WorkerError
                        code = f" (code {reply[4]})" if len(reply) > 4 else ""
                        raise cls(f"{reply[1]}: {reply[2]}{code}")
                    return reply
                if not self._proc.is_alive():
                    self._kill(grace=CRASH_GRACE_S)
                    if self._stop_requested.is_set():
                        raise WorkerStopped("Stopped.")
                    raise WorkerCrashed("The worker ended unexpectedly.")
                if time.monotonic() > deadline:
                    if self.log is not None:
                        self.log.write("worker_exit", process=self._log_name, hung=True,
                                       op=msg[0])
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
        from workspace.asr_engines import Segment
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


def diarize(handle, model, audio, hint, threads, device="cpu", gpu_site=None):
    """Whole-meeting speaker separation in the worker. audio: a
    media.AudioRef (the encrypted file, its key and context, its length).
    Returns (turns, exclusive_turns, device, asr_was_loaded_before). A stop
    or a hang ends the pass (it restarts from the beginning next time). With
    device="cuda" it runs in the companion GPU worker (gpu_site). The audio
    key goes to the worker only here, inside the request over its pipe
    (#111 C3a): never in the process's arguments, environment or command
    line, and never logged; transcription sends samples, never a key."""
    seconds = audio.frames / SAMPLE_RATE
    timeout = max(DIARIZE_TIMEOUT_FLOOR_S, DIARIZE_TIMEOUT_FACTOR * seconds)
    # One heavy model at a time: Whisper on the graphics card holds its model
    # in its own worker until that worker ends (#248).
    handle.shutdown_whisper_gpu()
    if device == "cuda":
        # A hung GPU gives up sooner, so the CPU fallback is not delayed for
        # long (15 minutes of audio took 80 s on a 4 GB laptop GPU).
        timeout = max(GPU_TIMEOUT_FLOOR_S, GPU_TIMEOUT_FACTOR * seconds)
        # One heavy model at a time: release the speech model in the main
        # worker before the GPU worker loads pyannote.
        handle.shutdown()
        target = handle.gpu_worker(gpu_site)
    else:
        target = handle
    with target._lock:
        try:
            reply = target._request(("diarize", model,
                                     (str(audio.path), audio.key, tuple(audio.context)),
                                     hint, threads, device), timeout)
        finally:
            # The worker released the speech model before trying, so the next
            # transcription must reload it even if speaker separation failed.
            handle._loaded = None
        return reply[1], reply[2], reply[3], reply[4]


class WorkerEngine:
    """Adapter so workspace.runner can drive a worker like an in-process engine.
    device/precision report what the worker's engine actually uses. Creating
    one starts a new run (clears any earlier Stop). note: recorded as the
    device instead of the worker's "cpu" when the graphics card was wanted but
    refused before the run ("cpu (graphics card not used: why)", #153)."""

    def __init__(self, handle, name, threads, note=None):
        self.handle, self.name, self.threads, self.note = handle, name, threads, note
        self.description = self.device = self.precision = None
        handle.begin_run()
        # One heavy model at a time: Whisper on the graphics card lets go of
        # its model before this worker loads one (#248).
        handle.shutdown_whisper_gpu()

    def transcribe(self, audio):
        self.description, device, self.precision = self.handle.load(self.name, self.threads)
        self.device = self.note or device
        return self.handle.transcribe(audio)


GPU_FAILURE_WORDS = ("gpuunavailable", "cuda", "cudnn", "cublas", "out of memory",
                     "outofmemory")


def gpu_failure(exc):
    """A failure the graphics card caused: the GPU worker crashed or hung, or
    the error names CUDA, cuDNN, cuBLAS or graphics memory."""
    return isinstance(exc, WorkerCrashed) or any(
        w in str(exc).lower() for w in GPU_FAILURE_WORDS)


def gpu_load_failure(exc):
    """A model that would not load on the graphics card. The Whisper GPU
    worker checks the model's files before it touches the card and reports a
    fault of theirs as ModelLoadError; any other load failure there (for
    example CTranslate2 refusing a precision the card cannot run) is the
    card's, so the processor is tried (D4)."""
    return gpu_failure(exc) or (isinstance(exc, EngineUnavailable)
                                and not str(exc).startswith("ModelLoadError:"))


class GpuWhisperEngine:
    """Whisper on the graphics card in its own worker (#248), with the
    processor as the fallback (D4). A failure the card causes (the component
    missing or damaged, the driver, out of graphics memory, a crashed or hung
    GPU worker) moves the rest of this run to the processor: the chunk in hand
    is transcribed again there, and nothing from the failed attempt was
    committed (D1). device then records why ("cpu (graphics card not used:
    ...)") and precision what the processor used, so the meeting says so; the
    model never changes. A deliberate stop is never retried, and an error that
    is not the card's is raised as it is."""

    def __init__(self, handle, name, threads, site, component):
        self.handle, self.name, self.threads = handle, name, threads
        self.description = self.device = self.precision = None
        self.reason = None                    # why the card was given up, once it was
        handle.begin_run()
        # One heavy model at a time: the processor's worker and the speaker
        # GPU worker let go of their models before Whisper loads on the card.
        handle.shutdown_for_whisper_gpu()
        self._gpu = handle.whisper_gpu_worker(site, component)

    def transcribe(self, audio):
        if self.reason is None:
            loading = True
            try:
                info = self._gpu.load(self.name, self.threads)
                loading = False
                # A hung card gives up sooner, so the fallback is not delayed long.
                timeout = max(GPU_TIMEOUT_FLOOR_S, GPU_TIMEOUT_FACTOR * len(audio) / SAMPLE_RATE)
                result = self._gpu.transcribe(audio, timeout=timeout)
                self.description, self.device, self.precision = info
                return result
            except WorkerStopped:
                raise
            except (WorkerError, WorkerCrashed) as exc:
                if not (gpu_load_failure(exc) if loading else gpu_failure(exc)):
                    raise                     # not the graphics card's fault: no second run
                self.reason = safe_message(exc).split(": ", 1)[-1][:120]
                self.handle.release_gpu("whisper")    # stop it and free its memory
        try:
            self.description, _, self.precision = self.handle.load(self.name, self.threads)
            result = self.handle.transcribe(audio)
        except (WorkerError, WorkerCrashed) as exc:
            # The worker's own error types (EngineUnavailable among them) keep
            # their type, so the runner treats the failure as it would anyway.
            raise type(exc)(f"{exc} (after the graphics card could not be used: "
                            f"{self.reason})") from exc
        self.device = f"cpu (graphics card not used: {self.reason})"[:200]
        return result
