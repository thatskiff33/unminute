# workspace/instant.py
# Instant live words while recording (#105): grey words on the recording card
# moments after people speak, from a streaming model in its own worker process,
# replaced by the live draft's phrase text (#104) as each phrase arrives.
#
# Part of live drafts: an InstantSession belongs to one DraftSession and lives
# and ends with it (workspace/drafts.py).
#   - Off unless ENABLED (#105's later pull request replaces this with the
#     setting, the policy key AllowInstantWords and AllowLiveDrafts) and the
#     model (asr_engines.INSTANT_MODEL) is downloaded and passes its checks.
#   - Two heavy models during a recording (D4's exception, the user's decision
#     DEC-092): the instant-words worker starts only once the draft
#     worker has loaded Parakeet v2, so the two load one at a time; both end
#     with the draft session, before any processing runs.
#   - Fed by the draft session's feeder thread, the same messages the draft
#     worker gets (so it follows the same audio: the microphone or the live
#     mix), through a bounded queue on its own sending thread: a slow or
#     loading instant-words worker never delays the draft or the recording.
#     While the queue holds more than QUEUE_S of audio, the oldest goes, so
#     the words keep up with now.
#   - Paused while the window is hidden or minimised (the user's decision
#     DEC-092): no audio is sent, so the worker uses no processor time; it
#     keeps its model, and starts a fresh stream when audio comes again.
#   - A crashed worker is restarted once; after a second crash, or a model
#     that will not load, instant words stop for that session (state FAILED or
#     UNAVAILABLE). The draft text and the recording carry on.
#   - Memory only, like drafts: words are never written to disk or a log (P3).
# Standard library and numpy only at module level: the window process imports
# this module (tests/test_import_hygiene.py).

import collections
import math
import threading
import time

from workspace import asr_engines, drafts

INSTANT_MODEL = asr_engines.INSTANT_MODEL.name
THREADS = 1                      # with 2, the trial's runtime busy-waited (#105)
LOG_NAME = "instant-worker"      # its log, crash file and process name (workspace.diag)
# Off until #105's last pull request brings the setting, the policy key and the
# page: no instant-words worker starts in the app until then. Tools and tests
# that measure or test it turn it on in their own process.
ENABLED = False
QUEUE_S = 3.0                    # audio waiting for the worker beyond this: the oldest goes
KEEP_S = 60.0                    # words older than this (behind the newest) are let go
MAX_WORDS = 1000                 # and never more than this many
MAX_PIECE = 64                   # characters of one token piece kept
MAX_TOKENS = 400                 # pieces in one reply
MAX_RESTARTS = 1
LOAD_TIMEOUT_S = 300.0
STOP_JOIN_S = 5.0

STARTING, RUNNING, FAILED, UNAVAILABLE, STOPPED, BATTERY = (
    "starting", "running", "failed", "unavailable", "stopped", "battery")


def clean_piece(text):
    """A token piece as plain printable text, its leading space kept (it
    starts a new word)."""
    text = "".join(ch for ch in str(text) if ch >= " ")
    return text[:MAX_PIECE]


# -- the instant-words worker's loop (runs in its own process) ----------------------

def serve(conn, load, log=None):
    """Load the engine, then turn audio into token pieces until the pipe closes
    or "exit" comes. load() returns an engine with stream(); a stream has
    accept(16 kHz float32) -> [(seconds from its start, piece)]. Messages: the
    draft worker's ("start", rate), ("audio", start, pcm), ("computer", at,
    rate, pcm), (MIX_OFF,), (FLUSH,), ("exit",). Replies: ("ready",
    description, load_s), ("tokens", [(seconds in the recording, piece)]),
    ("error", type, message, op, code). Audio that does not follow on from the
    last (a pause, audio dropped, the window hidden) starts a fresh stream at
    its own place, so word times always match the recording."""
    from workspace import diag
    from workspace.worker import safe_message

    def send(*msg):
        try:
            conn.send(msg)
        except (OSError, EOFError, ValueError):
            pass

    try:
        first = conn.recv()
    except (EOFError, OSError):
        return
    if not (isinstance(first, tuple) and len(first) == 2 and first[0] == "start"
            and drafts._whole(first[1], 8000, drafts.MAX_RATE)):
        return
    rate = first[1]
    started = time.monotonic()
    try:
        engine = load()
    except Exception as exc:
        code = diag.incident("worker", exc, op="load")
        send("error", type(exc).__name__, safe_message(exc), "load", code)
        return
    send("ready", str(getattr(engine, "description", "instant engine"))[:200],
         round(time.monotonic() - started, 2))
    mix = drafts.LiveMix(rate)
    state = {"next": None, "stream": None, "resampler": None, "base": 0.0}

    def feed(pieces):
        out = []
        for start, pcm in pieces:
            if state["next"] != start:              # a fresh stream at its own place
                state["stream"] = engine.stream()
                state["resampler"] = drafts.Resampler(rate)
                state["base"] = start / rate
            state["next"] = start + len(pcm)
            audio = state["resampler"].feed(pcm)
            if len(audio):
                out += [(round(state["base"] + t, 3), clean_piece(p))
                        for t, p in state["stream"].accept(audio)]
        for i in range(0, len(out), MAX_TOKENS):
            send("tokens", out[i:i + MAX_TOKENS])

    while True:
        if log is not None:
            log.limit_crash_file()                # recovered native errors stay small (#278)
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if not isinstance(msg, tuple) or not msg:
            continue
        try:
            if msg[0] == "audio" and len(msg) == 3 and drafts._whole(msg[1], 0, drafts.MAX_FRAME) \
                    and drafts._sized(msg[2]):
                feed(mix.mic(msg[1], drafts._pcm(msg[2])))
            elif msg[0] == "computer" and len(msg) == 4 \
                    and drafts._whole(msg[1], -drafts.MAX_FRAME, drafts.MAX_FRAME) \
                    and drafts._whole(msg[2], 8000, drafts.MAX_RATE) and drafts._sized(msg[3]):
                feed(mix.computer(msg[1], msg[2], drafts._pcm(msg[3])))
            elif msg[0] == drafts.MIX_OFF:
                feed(mix.off())
            elif msg[0] == drafts.FLUSH:
                feed(mix.flush())
            elif msg[0] == "exit":
                return
        except Exception as exc:
            # Decoding failed: recorded with its frames, and the app told, so
            # instant words end for this session (not restarted).
            code = diag.incident("worker", exc, op="instant")
            send("error", type(exc).__name__, safe_message(exc), "instant", code)
            return


# -- the app's side: one per draft session ------------------------------------------

def _seconds(msg, rate):
    """Seconds of audio in a message the draft worker gets (none for markers)."""
    if msg[0] == "audio":
        return len(msg[2]) / 2 / rate
    if msg[0] == "computer":
        return len(msg[3]) / 2 / msg[2]
    return 0.0


class InstantSession:
    """Instant words for one draft session, in the app process. put(msg) is
    called by the draft session's feeder thread with each message it sends the
    draft worker; it never blocks. start() starts the worker (the draft
    session calls it once its own worker is ready); stop() ends it and forgets
    the words; suspend() and resume() follow the draft session on battery.
    set_visible(False) pauses it while the window is hidden. Thread-safe."""

    def __init__(self, rate, cache_dir, *, model=INSTANT_MODEL, threads=THREADS, log=None,
                 allow_test_engines=False, target=None, visible=True):
        self.rate, self.cache_dir, self.model, self.threads = int(rate), str(cache_dir), model, \
            threads
        self.log, self.allow_test_engines, self._target = log, bool(allow_test_engines), target
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._queue = collections.deque()          # messages waiting to be sent
        self._queued_s = 0.0
        self._visible = bool(visible)
        self._words = []                           # [[start_s, text, number]]
        self._latest = -math.inf
        self.word_count = 0
        self.dropped_s = 0.0                       # audio let go to keep up (not while hidden)
        self.hidden_s = 0.0                        # audio not sent while the window was hidden
        self.state, self.reason = STARTING, None
        self.restarts = 0
        self.load_s = None
        self.description = None
        self._proc = self._conn = None
        self._ready = threading.Event()
        self._stopped = False
        self._threads = []

    # -- draft session side ---------------------------------------------------------------

    def put(self, msg):
        """Queue one of the draft worker's messages for this worker too. Never
        blocks: while hidden, after a stop or a failure, it is let go."""
        with self._lock:
            if self._stopped or self.state not in (STARTING, RUNNING):
                return
            seconds = _seconds(msg, self.rate)
            if not self._visible:
                self.hidden_s += seconds
                return
            self._queue.append((msg, seconds))
            self._queued_s += seconds
            while self._queued_s > QUEUE_S and len(self._queue) > 1:
                _, gone = self._queue.popleft()      # the oldest goes: keep up with now
                self._queued_s -= gone
                self.dropped_s += gone
            self._wake.notify()

    def set_visible(self, visible):
        """The window was shown (True) or hidden or minimised (False). Hidden,
        nothing is sent, so the worker idles; what was waiting goes too."""
        with self._lock:
            self._visible = bool(visible)
            if not self._visible:
                self._queue.clear()
                self._queued_s = 0.0

    @property
    def visible(self):
        return self._visible

    # -- control ------------------------------------------------------------------------

    def start(self):
        """Start the worker (again after resume). False once stopped or failed."""
        with self._lock:
            if self._stopped or self.state not in (STARTING, BATTERY) or self._proc is not None:
                return False
            self.state = STARTING
            self._start_process()
            if not any(t.name == "instant-sender" and t.is_alive() for t in self._threads):
                sender = threading.Thread(target=self._send_loop, name="instant-sender",
                                          daemon=True)
                sender.start()
                self._threads.append(sender)
            return True

    def _start_process(self):
        """Call with _lock held."""
        import multiprocessing as mp
        from workspace import worker
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe()
        log_dir = str(self.log.folder) if self.log is not None else None
        proc = ctx.Process(target=self._target or worker.instant_main,
                           args=(child, self.cache_dir, self.model, self.threads,
                                 self.allow_test_engines, log_dir),
                           daemon=True, name="unminute-instant-worker")
        proc.start()
        child.close()
        parent.send(("start", self.rate))
        self._ready.clear()
        self._proc, self._conn = proc, parent
        reader = threading.Thread(target=self._read_loop, args=(proc, parent),
                                  name="instant-reader", daemon=True)
        reader.start()
        self._threads.append(reader)

    def suspend(self):
        """On battery with the draft session: the worker ends (memory freed),
        the words are kept."""
        with self._lock:
            if self._stopped or self.state not in (STARTING, RUNNING):
                return
            self.state = BATTERY
            proc, conn = self._proc, self._conn
            self._proc = self._conn = None
            self._queue.clear()
            self._queued_s = 0.0
        self._end_process(proc, conn)

    def stop(self):
        """End instant words for good: the worker is ended and waited for, and
        the words forgotten."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            if self.state not in (FAILED, UNAVAILABLE):
                self.state = STOPPED
            proc, conn = self._proc, self._conn
            self._proc = self._conn = None
            words = self.word_count
            self._words, self._queue = [], collections.deque()
            self._queued_s = 0.0
            self._wake.notify_all()
        self._end_process(proc, conn)
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(STOP_JOIN_S)
        if self.log is not None:
            self.log.write("instant", words=words, restarts=self.restarts,
                           dropped_s=round(self.dropped_s, 1), load_s=self.load_s)

    def _end_process(self, proc, conn, died=False):
        if proc is not None:
            if proc.is_alive():
                proc.terminate()
            from workspace.worker import END_WAIT_S
            proc.join(END_WAIT_S)                 # its crash file is closed once it has ended
            if self.log is not None:
                from workspace import diag
                crash = self.log.folder / f"{LOG_NAME}{diag.CRASH_SUFFIX}"
                code = self.log.collect_crash(died, crash_path=crash, process=LOG_NAME)
                if died:
                    self.log.write("worker_exit", process=LOG_NAME, exitcode=proc.exitcode,
                                   code=code)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    @property
    def pid(self):
        proc = self._proc
        return proc.pid if proc is not None and proc.is_alive() else None

    @property
    def alive(self):
        proc = self._proc
        return proc is not None and proc.is_alive()

    # -- threads --------------------------------------------------------------------------

    def _send_loop(self):
        while True:
            with self._lock:
                while not self._stopped and not (self._queue and self._ready.is_set()
                                                 and self._conn is not None):
                    self._wake.wait(0.2)
                if self._stopped:
                    return
                msg, seconds = self._queue.popleft()
                self._queued_s -= seconds
                conn = self._conn
            try:
                conn.send(msg)
            except (OSError, EOFError, ValueError):
                pass                              # the reader sees the worker gone

    def _read_loop(self, proc, conn):
        loading_since = time.monotonic()
        while True:
            try:
                if not conn.poll(0.5):
                    if not self._ready.is_set() and \
                            time.monotonic() - loading_since > LOAD_TIMEOUT_S:
                        self._failed(proc, conn, UNAVAILABLE, "load_timeout")
                        return
                    if proc.is_alive():
                        continue
                    raise EOFError
                msg = conn.recv()
            except (EOFError, OSError):
                self._crashed(proc, conn)
                return
            with self._lock:
                if self._stopped or self._conn is not conn:
                    return
                self._take(msg)
                if self.state not in (UNAVAILABLE, FAILED):
                    if self._ready.is_set():
                        self._wake.notify()
                    continue
                self._proc = self._conn = None
            self._end_process(proc, conn)
            return

    def _take(self, msg):
        """One reply (call with _lock held): only text and numbers in a known
        shape are kept."""
        if not isinstance(msg, tuple) or not msg:
            return
        kind = msg[0]
        if kind == "ready" and len(msg) == 3:
            self.description = str(msg[1])[:200]
            self.load_s = msg[2] if isinstance(msg[2], (int, float)) \
                and not isinstance(msg[2], bool) and math.isfinite(msg[2]) else None
            self.state = RUNNING
            self._ready.set()
        elif kind == "tokens" and len(msg) == 2 and isinstance(msg[1], list):
            for item in msg[1][:MAX_TOKENS]:
                if (isinstance(item, (tuple, list)) and len(item) == 2
                        and isinstance(item[0], (int, float)) and not isinstance(item[0], bool)
                        and math.isfinite(item[0]) and isinstance(item[1], str)):
                    self._add(float(item[0]), clean_piece(item[1]))
        elif kind == "error":
            self.state = UNAVAILABLE if msg[3:4] == ("load",) else FAILED
            self.reason = "load" if self.state == UNAVAILABLE else "error"

    def _add(self, at, piece):
        """A token piece: a leading space starts a word at its time; otherwise
        it goes on the newest word (call with _lock held)."""
        text = piece.strip()
        if not text:
            return
        if piece[:1] == " " or not self._words:
            self.word_count += 1
            self._words.append([at, text, self.word_count])     # the number: tools only
        else:
            self._words[-1][1] = (self._words[-1][1] + text)[:drafts.MAX_TEXT]
        self._latest = max(self._latest, at)
        cut = 0
        while cut < len(self._words) and (self._words[cut][0] < self._latest - KEEP_S
                                          or len(self._words) - cut > MAX_WORDS):
            cut += 1
        if cut:
            del self._words[:cut]

    def _failed(self, proc, conn, state, reason):
        with self._lock:
            if self._stopped or self._conn is not conn:
                return
            self.state, self.reason = state, reason
            self._proc = self._conn = None
        self._end_process(proc, conn)

    def _crashed(self, proc, conn):
        """The worker ended by itself: restart it once, then stop (the draft
        and the recording are never affected)."""
        with self._lock:
            if self._stopped or self._conn is not conn:
                return
            self._proc = self._conn = None
            self._ready.clear()
        self._end_process(proc, conn, died=True)
        with self._lock:
            if self._stopped or self._proc is not None or self.state == BATTERY:
                return
            if self.restarts < MAX_RESTARTS:
                self.restarts += 1
                self.state = STARTING
                self._start_process()
            else:
                self.state, self.reason = FAILED, "crashed"
                self._queue.clear()
                self._queued_s = 0.0

    # -- reading (for the page, #105's last pull request) ---------------------------------

    def words_after(self, after_s):
        """The words that start at or after after_s (seconds in the recording:
        the end of the draft's newest phrase, which replaces the words before
        it), as one line of text, oldest first."""
        with self._lock:
            return " ".join(w[1] for w in self._words if w[0] >= after_s)
