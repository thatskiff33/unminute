# workspace/drafts.py
# Live drafts while recording (#104): rough text a few seconds behind speech,
# replaced by the full transcript after Stop and save. Back end only so far;
# the page does not show it yet.
#
# Off unless the user turns on "live_drafts" (default off, the owner's
# decision of 20 Oct 2026), withheld when managed policy sets AllowLiveDrafts
# to 0, holds a bad value or cannot be read (K3), and paused on battery unless
# processing on battery is allowed. Off means no draft worker and no model.
#
#   - Memory only. Draft text is never written to disk, the database or a log
#     (P3): Stop, Discard, a self-stop, closing the app or a crash leave none.
#     Like any text the app holds, it can still reach the page file, the
#     hibernation file or a crash dump.
#   - The recorder's writer thread (never the audio callback) hands each block
#     to DraftSession.tap: a non-blocking put into a bounded queue. A full
#     queue loses draft audio only, never recorded audio, and an error in the
#     tap turns the tap off without touching the recording.
#   - A feeder thread sends the audio in batches of about BATCH_S to the draft
#     worker, its own process (worker.draft_main): below-normal priority,
#     DRAFT_THREADS threads, the same offline start-up as the main worker
#     (N1), and Parakeet v2 and Silero loaded from their verified files (I1).
#     It resamples to 16 kHz here in numpy (no PyAV in a model process),
#     scores 32 ms windows for speech, splits phrases (PhraseSplitter) and
#     transcribes each phrase on a second thread, so reading audio never
#     waits for a phrase. When more than BACKLOG_S of phrases are waiting,
#     the oldest are skipped and the draft says how much.
#   - A session is tied to one recording and a generation number. After stop()
#     nothing more is kept and a late result is dropped.
#   - A crashed draft worker is restarted once per session; a second crash, or
#     a model that will not load, ends drafts for that recording. Neither ever
#     touches the recording.
# Standard library and numpy only at module level: the window process imports
# this module (tests/test_import_hygiene.py).

import collections
import math
import queue
import threading
import time

import numpy as np

from workspace import asr_engines

DRAFT_MODEL = asr_engines.PARAKEET_V2     # Parakeet v2 only (owner decision 8, #104)
DRAFT_THREADS = 2
SAMPLE_RATE = 16000
WINDOW = 512                     # Silero's 32 ms step at 16 kHz
WINDOW_S = WINDOW / SAMPLE_RATE
SPEECH_ON = 0.5                  # Silero probability that starts speech (onnx-asr's default)
SPEECH_OFF = 0.35                # and below which a window counts as silence
PRE_ROLL_S = 0.3                 # audio kept from before the speech started
END_SILENCE_S = 0.6              # a phrase ends after this much silence
TAIL_S = 0.3                     # of which this much stays on the end of the phrase
MAX_PHRASE_S = 8.0               # unbroken speech is cut at about this length
CUT_SEARCH_S = 1.5               # at the quietest window within this much of the end
MIN_SPEECH_S = 0.25              # a phrase with less speech than this is a blip: dropped
TAP_S = 60.0                     # draft audio held while the worker loads or falls behind
BATCH_S = 0.5                    # audio sent to the worker at a time
BACKLOG_S = 30.0                 # phrases waiting beyond this are skipped, oldest first
MAX_BATCH_FRAMES = 192000 * 2    # 2 s at 192 kHz: the worker refuses a larger batch
MAX_RATE = 192000
MAX_TEXT = 2000                  # characters of one phrase kept
MAX_KEPT = 10000                 # phrases kept for one recording (about 8 hours)
MAX_RESTARTS = 1                 # automatic restarts of a crashed draft worker
LOAD_TIMEOUT_S = 300.0           # a worker that has not loaded by then is ended
STOP_JOIN_S = 5.0
LOG_NAME = "draft-worker"        # its log, crash file and process name (workspace.diag)
FLUSH = "flush"                  # tap queue marker: end the current phrase (pause)

# Session states (what the page part will show, #104 slice 3):
STARTING, RUNNING, BATTERY, FAILED, UNAVAILABLE, STOPPED = (
    "starting", "running", "battery", "failed", "unavailable", "stopped")
NOTES = {
    STARTING: "Getting draft text ready.",
    BATTERY: "Draft text is paused while on battery. The recording carries on.",
    FAILED: "Draft text stopped. The recording is not affected.",
    UNAVAILABLE: "Draft text is not available. The recording is not affected.",
}


# -- audio: resampling, phrase splitting (pure; tested without a model) -------------

def _lowpass(rate, cutoff_hz=7200.0, half=32):
    """A windowed-sinc low-pass filter for rate, below the 8 kHz Nyquist
    limit of 16 kHz audio (2 * half + 1 taps, unit gain)."""
    n = np.arange(-half, half + 1, dtype=np.float64)
    fc = cutoff_hz / rate
    taps = 2 * fc * np.sinc(2 * fc * n) * np.blackman(len(n))
    return (taps / taps.sum()).astype(np.float32)


class Resampler:
    """Device-rate 16-bit PCM in, 16 kHz float32 out, block by block, with
    the same result as resampling the whole stream at once: a low-pass
    filter, then linear interpolation at 16 kHz positions. Draft quality,
    in numpy, so no media library is loaded into the model process."""

    def __init__(self, rate):
        if not isinstance(rate, int) or not 8000 <= rate <= MAX_RATE:
            raise ValueError("Unsupported sample rate.")
        self.rate = rate
        self.step = rate / SAMPLE_RATE
        self._taps = None if rate == SAMPLE_RATE else _lowpass(rate)
        if self._taps is not None:
            self._hist = np.zeros(len(self._taps) - 1, dtype=np.float32)
        self._buf = np.zeros(0, dtype=np.float32)    # filtered, not yet interpolated
        self._pos = 0.0                              # next output position in _buf

    def feed(self, pcm):
        x = np.asarray(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if self._taps is None:
            return x
        joined = np.concatenate([self._hist, x])
        self._hist = joined[len(joined) - len(self._hist):]
        self._buf = np.concatenate([self._buf, np.convolve(joined, self._taps, mode="valid")])
        last = len(self._buf) - 1                    # interpolation needs the next sample
        if last <= self._pos:
            return np.zeros(0, dtype=np.float32)
        count = int(math.ceil((last - self._pos) / self.step))
        at = self._pos + np.arange(count) * self.step
        i = at.astype(np.int64)
        frac = (at - i).astype(np.float32)
        out = self._buf[i] * (1 - frac) + self._buf[i + 1] * frac
        nxt = self._pos + count * self.step
        drop = int(nxt)
        self._buf, self._pos = self._buf[drop:], nxt - drop
        return out.astype(np.float32)


def _windows(seconds):
    return max(1, int(math.ceil(seconds / WINDOW_S - 1e-9)))


class PhraseSplitter:
    """Splits 16 kHz audio, scored 32 ms at a time, into phrases. A phrase
    starts at a window scored SPEECH_ON or more, with PRE_ROLL_S before it;
    ends after END_SILENCE_S of windows below SPEECH_OFF, keeping TAIL_S of
    them; and unbroken speech is cut at MAX_PHRASE_S, at the quietest window
    in the last CUT_SEARCH_S. A phrase with less than MIN_SPEECH_S of speech
    is dropped. No window is ever in two phrases. push() and flush() return
    [(start_sample, audio)], start_sample counted from the first window."""

    def __init__(self):
        self.pre_w, self.end_w = _windows(PRE_ROLL_S), _windows(END_SILENCE_S)
        self.tail_w, self.max_w = _windows(TAIL_S), _windows(MAX_PHRASE_S)
        self.cut_w, self.min_w = _windows(CUT_SEARCH_S), _windows(MIN_SPEECH_S)
        self._pre = collections.deque(maxlen=self.pre_w)
        self._cur, self._probs = [], []
        self._start = None               # window index where the current phrase starts
        self._silence = 0                # windows of silence at its end
        self._index = 0                  # windows pushed so far

    def push(self, window, prob):
        index = self._index
        self._index += 1
        if self._start is None:
            if prob < SPEECH_ON:
                self._pre.append(window)
                return []
            self._start = index - len(self._pre)
            self._cur, self._probs = list(self._pre), [0.0] * len(self._pre)
            self._pre.clear()
            self._silence = 0
        self._cur.append(window)
        self._probs.append(float(prob))
        self._silence = self._silence + 1 if prob < SPEECH_OFF else 0
        if self._silence >= self.end_w:
            return self._end()
        if len(self._cur) >= self.max_w:
            return self._cut()
        return []

    def flush(self):
        """End the current phrase now (a pause, a gap, the end): the next
        audio starts afresh, with no pre-roll from before."""
        out = self._end() if self._start is not None else []
        self._pre.clear()
        return out

    def _emit(self, count):
        probs = self._probs[:count]
        if sum(p >= SPEECH_ON for p in probs) < self.min_w:
            return []
        return [(self._start * WINDOW, np.concatenate(self._cur[:count]))]

    def _end(self):
        keep = len(self._cur) - max(0, self._silence - self.tail_w)
        out = self._emit(keep)
        for window in self._cur[keep:]:           # silence after the phrase: pre-roll
            self._pre.append(window)
        self._cur, self._probs, self._start, self._silence = [], [], None, 0
        return out

    def _cut(self):
        first = max(1, len(self._cur) - self.cut_w)
        energy = [float(np.mean(np.square(w))) for w in self._cur[first:]]
        cut = first + int(np.argmin(energy)) + 1          # the quiet window ends this phrase
        out = self._emit(cut)
        self._start += cut
        self._cur, self._probs = self._cur[cut:], self._probs[cut:]
        silence = 0
        for p in reversed(self._probs):
            if p >= SPEECH_OFF:
                break
            silence += 1
        self._silence = silence
        if not self._cur:
            self._start = None
        return out


class DraftPipeline:
    """Device-rate audio in, phrases out: resample, score, split. Each batch
    says where it starts in the recording (frames at the device rate); a
    batch that does not follow on (draft audio lost, or drafts paused on
    battery) ends the current phrase and starts afresh there, so phrase times
    always match the recording. Returns [(start_s, end_s, audio)]."""

    def __init__(self, rate, score, reset=None):
        self.rate, self._score, self._reset = int(rate), score, reset
        self._next = None
        self._new(0)

    def _new(self, start_frame):
        self._resampler = Resampler(self.rate)
        self._splitter = PhraseSplitter()
        self._rest = np.zeros(0, dtype=np.float32)
        self._base = start_frame / self.rate
        if self._reset is not None:
            self._reset()

    def _timed(self, phrases):
        return [(self._base + s / SAMPLE_RATE, self._base + (s + len(a)) / SAMPLE_RATE, a)
                for s, a in phrases]

    def feed(self, start_frame, pcm):
        out = []
        if self._next is None or start_frame != self._next:
            if self._next is not None:
                out += self.flush()
            self._new(start_frame)
        self._next = start_frame + len(pcm)
        audio = np.concatenate([self._rest, self._resampler.feed(pcm)])
        whole = len(audio) // WINDOW * WINDOW
        found = []
        for i in range(0, whole, WINDOW):
            window = audio[i:i + WINDOW]
            found += self._splitter.push(window, self._score(window))
        self._rest = audio[whole:]
        return out + self._timed(found)

    def flush(self):
        return self._timed(self._splitter.flush())


# -- the draft worker's loop (runs in the draft worker process) ---------------------

def clean_text(text):
    """A phrase's text as plain, printable text of bounded length."""
    text = "".join(ch for ch in str(text) if ch >= " " or ch == "\t")
    return " ".join(text.split())[:MAX_TEXT]


def serve(conn, load, log=None):
    """The draft worker's loop: load the engine, then turn audio batches into
    phrases until the pipe closes or "exit" arrives. load() returns an engine
    with speech_prob(window), reset() and transcribe_phrase(audio). Replies:
    ("ready", description, load_s), ("phrase", start_s, end_s, text),
    ("skipped", seconds), ("error", type, message, op, code)."""
    from workspace import diag
    from workspace.worker import safe_message
    send_lock = threading.Lock()

    def send(*msg):
        with send_lock:
            try:
                conn.send(msg)
            except (OSError, EOFError, ValueError):
                pass                     # the app has gone or stopped the drafts

    try:
        first = conn.recv()
    except (EOFError, OSError):
        return
    if not (isinstance(first, tuple) and len(first) == 2 and first[0] == "start"
            and isinstance(first[1], int) and 8000 <= first[1] <= MAX_RATE):
        return
    rate = first[1]
    started = time.monotonic()
    try:
        engine = load()
    except Exception as exc:
        code = diag.incident("worker", exc, op="load")
        send("error", type(exc).__name__, safe_message(exc), "load", code)
        return
    send("ready", str(getattr(engine, "description", "draft engine"))[:200],
         round(time.monotonic() - started, 2))

    waiting = collections.deque()         # (start_s, end_s, audio) to transcribe
    cond = threading.Condition()
    state = {"waiting_s": 0.0, "done": False}

    def transcribe_loop():
        while True:
            with cond:
                while not waiting and not state["done"]:
                    cond.wait()
                if state["done"]:
                    return
                start, end, audio = waiting.popleft()
                state["waiting_s"] -= end - start
            try:
                text = clean_text(engine.transcribe_phrase(audio))
            except Exception as exc:     # one phrase failed: say it was skipped, carry on
                diag.incident("worker", exc, op="draft")
                send("skipped", round(end - start, 2))
                continue
            if text:
                send("phrase", round(start, 3), round(end, 3), text)

    threading.Thread(target=transcribe_loop, name="draft-transcribe", daemon=True).start()

    def queue_phrases(phrases):
        skipped = 0.0
        with cond:
            for phrase in phrases:
                waiting.append(phrase)
                state["waiting_s"] += phrase[1] - phrase[0]
            while state["waiting_s"] > BACKLOG_S and len(waiting) > 1:
                start, end, _ = waiting.popleft()     # the oldest goes: keep up with now
                state["waiting_s"] -= end - start
                skipped += end - start
            cond.notify()
        if skipped:
            send("skipped", round(skipped, 2))

    pipeline = DraftPipeline(rate, engine.speech_prob, getattr(engine, "reset", None))
    try:
        while True:
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                return
            if not isinstance(msg, tuple) or not msg:
                continue
            if msg[0] == "audio" and len(msg) == 3 and isinstance(msg[1], int) \
                    and isinstance(msg[2], bytes) and 0 < len(msg[2]) <= 2 * MAX_BATCH_FRAMES:
                pcm = np.frombuffer(msg[2][: len(msg[2]) // 2 * 2], dtype="<i2")
                queue_phrases(pipeline.feed(msg[1], pcm))
            elif msg[0] == FLUSH:
                queue_phrases(pipeline.flush())
            elif msg[0] == "exit":
                return
    finally:
        with cond:
            state["done"] = True
            cond.notify()


# -- the app's side: one session per recording -----------------------------------------

class DraftSession:
    """Live drafts for one recording, in the app process. tap(start, block) is
    called by the recorder's writer thread for each block; it never blocks
    or raises. start() starts the draft worker; stop() ends it and forgets
    the text. suspend() ends the worker but keeps the text (battery);
    resume() starts it again. Thread-safe."""

    def __init__(self, recording_id, rate, cache_dir, *, generation=0, model=DRAFT_MODEL,
                 threads=DRAFT_THREADS, log=None, allow_test_engines=False, target=None):
        self.recording_id, self.rate, self.generation = recording_id, int(rate), generation
        self.cache_dir, self.model, self.threads = str(cache_dir), model, threads
        self.log, self.allow_test_engines = log, bool(allow_test_engines)
        self._target = target            # tests: another process target
        self._lock = threading.Lock()
        self._tap = queue.Queue(maxsize=max(1, int(TAP_S / 0.1)))
        self._feeding = False            # the tap queues audio (not stopped or suspended)
        self._phrases = []               # [{kind, text, start, end} | {kind, seconds}]
        self._first = 0                  # index of _phrases[0] (older ones were let go)
        self.state, self.reason = STARTING, None
        self.restarts = 0
        self.dropped_s = self.skipped_s = 0.0
        self.load_s = None
        self.description = None
        self._proc = self._conn = None
        self._stopped = False
        self._ready = threading.Event()
        self._threads = []

    # -- recorder side (writer thread): never blocks, never raises ---------------------

    def tap(self, start, block):
        """start: where the block begins in the recording, in frames at the
        device rate (paused time is not recorded, so not counted)."""
        if not self._feeding:
            return
        try:
            self._tap.put_nowait((start, block))
        except queue.Full:
            self.dropped_s += len(block) / self.rate

    # -- control --------------------------------------------------------------------

    def start(self):
        with self._lock:
            if self._stopped:
                return False
            self._start_process()
            self._feeding = True
        feeder = threading.Thread(target=self._feed_loop, name="draft-feeder", daemon=True)
        feeder.start()
        self._threads.append(feeder)
        return True

    def _start_process(self):
        """Call with _lock held."""
        import multiprocessing as mp
        from workspace import worker
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe()
        log_dir = str(self.log.folder) if self.log is not None else None
        proc = ctx.Process(target=self._target or worker.draft_main,
                           args=(child, self.cache_dir, self.model, self.threads,
                                 self.allow_test_engines, log_dir),
                           daemon=True, name="unminute-draft-worker")
        proc.start()
        child.close()
        parent.send(("start", self.rate))
        self._proc, self._conn = proc, parent
        self._ready.clear()
        self.state = STARTING
        reader = threading.Thread(target=self._read_loop, args=(proc, parent),
                                  name="draft-reader", daemon=True)
        reader.start()
        self._threads.append(reader)

    def pause(self):
        """The recording paused: end the phrase in progress once the audio
        queued before the pause has been sent."""
        if self._feeding:
            try:
                self._tap.put_nowait((FLUSH, None))
            except queue.Full:
                pass

    def suspend(self, state=BATTERY):
        """End the worker (memory freed) but keep the text: on battery."""
        with self._lock:
            if self._stopped or self.state not in (STARTING, RUNNING):
                return                   # a worker that gave up is not revived
            self._feeding = False
            self.state = state
            proc, conn = self._proc, self._conn
            self._proc = self._conn = None
        self._end_process(proc, conn)
        self._drain()

    def resume(self):
        """Start the worker again after suspend() (mains power is back)."""
        with self._lock:
            if self._stopped or self.state != BATTERY or self._proc is not None:
                return
            self._start_process()
            self._feeding = True

    def stop(self):
        """End drafts for good: the worker is ended and the text forgotten."""
        with self._lock:
            if self._stopped:
                return
            self._stopped, self._feeding = True, False
            self.state = STOPPED
            proc, conn = self._proc, self._conn
            self._proc = self._conn = None
            kept = self.count
            self._phrases, self._first = [], 0
        self._end_process(proc, conn)
        self._drain()
        try:
            self._tap.put_nowait((None, None))       # wake the feeder to end
        except queue.Full:
            pass
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(STOP_JOIN_S)
        if self.log is not None:
            self.log.write("drafts", phrases=kept, restarts=self.restarts,
                           skipped_s=round(self.skipped_s, 1),
                           dropped_s=round(self.dropped_s, 1), load_s=self.load_s)

    def _end_process(self, proc, conn, died=False):
        if proc is not None:
            if proc.is_alive():
                proc.terminate()
            proc.join(STOP_JOIN_S)
            if self.log is not None:
                crash = self.log.folder / f"{LOG_NAME}.crash"
                code = self.log.collect_crash(died, crash_path=crash, process=LOG_NAME)
                if died:
                    self.log.write("worker_exit", process=LOG_NAME, exitcode=proc.exitcode,
                                   code=code)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def _drain(self):
        while True:
            try:
                self._tap.get_nowait()
            except queue.Empty:
                return

    @property
    def pid(self):
        proc = self._proc
        return proc.pid if proc is not None and proc.is_alive() else None

    @property
    def alive(self):
        proc = self._proc
        return proc is not None and proc.is_alive()

    @property
    def count(self):
        return self._first + len(self._phrases)

    # -- threads --------------------------------------------------------------------

    def _feed_loop(self):
        batch, batch_start, batch_frames = [], None, 0
        limit = max(1, int(self.rate * BATCH_S))
        while True:
            try:
                start, block = self._tap.get(timeout=0.2)
            except queue.Empty:
                start = block = None
                if self._stopped:
                    return
                if not batch:
                    continue
            if start is None and block is None and self._stopped:
                return
            flush = start == FLUSH
            if block is not None:
                if batch and start != batch_start + batch_frames:
                    self._send_batch(batch_start, batch)     # not contiguous: send apart
                    batch, batch_frames = [], 0
                if not batch:
                    batch_start = start
                batch.append(block)
                batch_frames += len(block)
            if batch and (batch_frames >= limit or flush or block is None):
                self._send_batch(batch_start, batch)
                batch, batch_frames = [], 0
            if flush:
                self._send((FLUSH,))

    def _send_batch(self, start, blocks):
        pcm = np.concatenate(blocks).astype("<i2")
        for i in range(0, len(pcm), MAX_BATCH_FRAMES):
            part = pcm[i:i + MAX_BATCH_FRAMES]
            if not self._send(("audio", start + i, part.tobytes())):
                self.dropped_s += len(part) / self.rate

    def _send(self, msg):
        # Not before the worker is ready (it reads nothing while it loads), and
        # never after a stop. A worker that is gone loses this audio, not the
        # recording.
        while not self._ready.wait(0.2):
            if self._stopped or not self._feeding:
                return False
        conn = self._conn
        if conn is None or self._stopped or not self._feeding:
            return False
        try:
            conn.send(msg)
            return True
        except (OSError, EOFError, ValueError):
            return False

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
                    return                   # stopped or replaced: a late result is dropped
                self._take(msg)
                if self.state not in (UNAVAILABLE, FAILED):
                    continue
                self._proc = self._conn = None      # it reported it cannot go on: end it
            self._end_process(proc, conn)
            return

    def _take(self, msg):
        """One reply from the worker (call with _lock held). Checked: the
        worker is ours, but only text and numbers in a known shape are kept."""
        if not isinstance(msg, tuple) or not msg:
            return
        kind = msg[0]
        if kind == "ready" and len(msg) == 3:
            self.description = str(msg[1])[:200]
            self.load_s = msg[2] if isinstance(msg[2], (int, float)) else None
            self.state = RUNNING
            self._ready.set()
        elif kind == "phrase" and len(msg) == 4 and isinstance(msg[3], str) \
                and all(isinstance(x, (int, float)) and math.isfinite(x) for x in msg[1:3]):
            self._keep({"kind": "phrase", "text": clean_text(msg[3]),
                        "start": float(msg[1]), "end": float(msg[2])})
        elif kind == "skipped" and len(msg) == 2 and isinstance(msg[1], (int, float)) \
                and math.isfinite(msg[1]) and msg[1] > 0:
            self.skipped_s += float(msg[1])
            self._keep({"kind": "skipped", "seconds": round(float(msg[1]), 1)})
        elif kind == "error":
            self.state = UNAVAILABLE if msg[3:4] == ("load",) else FAILED
            self.reason = "load" if self.state == UNAVAILABLE else "error"
            self._feeding = False

    def _keep(self, item):
        self._phrases.append(item)
        if len(self._phrases) > MAX_KEPT:
            drop = len(self._phrases) - MAX_KEPT
            del self._phrases[:drop]
            self._first += drop

    def _failed(self, proc, conn, state, reason):
        with self._lock:
            if self._stopped or self._conn is not conn:
                return
            self.state, self.reason, self._feeding = state, reason, False
            self._proc = self._conn = None
        self._end_process(proc, conn)

    def _crashed(self, proc, conn):
        """The worker ended by itself: restart it once, then give up (the
        recording is never affected)."""
        with self._lock:
            if self._stopped or self._conn is not conn:
                return
            self._proc = self._conn = None
        self._end_process(proc, conn, died=True)
        with self._lock:
            if self._stopped or self._proc is not None:
                return
            if self.restarts < MAX_RESTARTS:
                self.restarts += 1
                self._start_process()
            else:
                self.state, self.reason, self._feeding = FAILED, "crashed", False

    # -- reading (for the page, #104 slice 3) -----------------------------------------

    def snapshot(self, since=0, limit=200):
        """State and the phrases after index since (at most limit): text and
        times only, never a path."""
        with self._lock:
            start = max(int(since), self._first)
            items = self._phrases[start - self._first: start - self._first + limit]
            return {"state": self.state, "note": NOTES.get(self.state),
                    "generation": self.generation, "count": self.count,
                    "since": start, "phrases": [dict(p) for p in items]}
