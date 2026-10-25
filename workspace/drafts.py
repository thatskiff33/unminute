# workspace/drafts.py
# Live drafts while recording (#104): rough text a few seconds behind speech,
# replaced by the full transcript after Stop and save, shown on the recording
# card (workspace/ui, through the bridge method recording_draft; text only).
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
#     tap turns the tap off without touching the recording. The tap holds
#     audio from as soon as the app decides to start drafts, while the main
#     worker lets go of its model and the draft worker loads (up to TAP_S).
#     A pause reaches the tap from the writer too, after the blocks queued
#     before it, and ends the phrase in progress there.
#   - A feeder thread sends the audio in batches of about BATCH_S to the draft
#     worker, its own process (worker.draft_main): below-normal priority,
#     DRAFT_THREADS threads, the same offline start-up as the main worker
#     (N1), and Parakeet v2 and Silero loaded from their verified files (I1).
#     It resamples to 16 kHz here in numpy (no PyAV in a model process),
#     scores 32 ms windows for speech, splits phrases (PhraseSplitter) and
#     transcribes each phrase on a second thread, so reading audio never
#     waits for a phrase. When more than BACKLOG_S of phrases are waiting,
#     the oldest are skipped and the draft says how much.
#   - With computer audio recorded too (#108), the user chooses what the draft
#     follows (setting live_drafts_audio, the owner's decision of 22 Oct 2026):
#     the microphone (the default), or a live mix of both tracks. For the mix,
#     the writer thread also hands each computer block to tap_computer, placed
#     on the microphone's time line from the two tracks' arrival times; the
#     draft worker resamples it to the microphone's rate and mixes it as the
#     recording's own mix does (LiveMix: the mean of the two, as
#     workspace/mixdown.py). Draft only: the recording and its mix are unchanged.
#   - A session is tied to one recording and a generation number. After stop()
#     nothing more is kept and a late result is dropped.
#   - A crashed draft worker is restarted once per session; a second crash, or
#     a model that will not load, ends drafts for that recording. Neither ever
#     touches the recording.
#   - Its crash file is handled as the main worker's (#278): started afresh
#     past diag.CRASH_MAX while it runs, closed before the process ends by
#     itself, and an ended worker is waited for up to worker.END_WAIT_S before
#     the app reads and deletes the file.
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
MAX_PHRASE_S = 10.0              # unbroken speech is cut at about this length (owner, 24 Oct)
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
STOP_JOIN_S = 5.0                # a draft thread is waited for this long at a stop
LOG_NAME = "draft-worker"        # its log, crash file and process name (workspace.diag)
FLUSH = "flush"                  # tap queue marker: end the current phrase (pause)
MIX_OFF = "mix_off"              # computer tap marker: the draft follows the microphone again
MIX_HOLD_S = 1.0                 # microphone audio waits this long for the computer's (mix)
MIX_REACH_S = 5.0                # computer audio placed further than this from the
                                 # microphone's latest audio is not used (draft only)
MIX_PIECES = 400                 # computer pieces the live mix holds at most (see below)
MIX_HELD_S = 10.0                # and computer audio, at the microphone's rate (see below)
TAP_BLOCK_S = 0.1                # a microphone block (recorder.BLOCK_S; a test keeps them equal)
COMPUTER_TAP_FRAMES = int(TAP_S * 48000)   # the computer tap's ceiling (see below)
COMPUTER_TAP_BLOCKS = int(TAP_S / 0.01)    # and the blocks it holds at most
COMPUTER_BLOCK_S = 1.0           # a longer computer block is cut to this (see below)
# The live mix's memory (#371), end to end. Draft only: the saved tracks are
# untouched, and no microphone audio is lost to make room for computer audio.
#   - The computer tap holds TAP_S at the computer track's rate, and never
#     more than COMPUTER_TAP_FRAMES (2,880,000 frames: TAP_S at 48 kHz, the
#     loopback's rate; about 5.8 MB of 16-bit mono) or COMPUTER_TAP_BLOCKS
#     blocks (each costs a little memory of its own), whatever the rate or
#     block size. A block longer than COMPUTER_BLOCK_S (only silence filled
#     in after a stall gets that long) is cut to it. Once full the tap loses
#     the newest computer audio, as the microphone tap (a full Queue of
#     TAP_S / TAP_BLOCK_S blocks; a pause's marker takes one) loses its
#     newest: so while the worker loads, or after it falls behind, the two
#     hold the same stretch of time. Losing the oldest instead would keep
#     computer audio for a stretch whose microphone audio was lost.
#   - The feeder takes computer audio only up to BATCH_S past the microphone
#     audio it has taken, so after a backlog the two reach the worker in
#     step and the mix of the held stretch has both (sent all at once, most
#     of it would be further than MIX_REACH_S from the microphone's audio and
#     not used). It holds at most BATCH_S plus one block (1.5 s) of computer
#     audio, in up to four copies while a piece is sent (the blocks, the
#     joined audio, a part's bytes and the pipe's pickled copy): about
#     0.6 MB at 48 kHz, 2.3 MB at 192 kHz.
#   - The worker's LiveMix holds at most MIX_HELD_S of computer audio as
#     float32 at the microphone's rate (about 1.9 MB at 48 kHz, 7.7 MB at
#     192 kHz), plus the piece being resampled; pieces the microphone audio
#     has passed are let go.
# So at the loopback's 48 kHz the app holds at most about 6.3 MB of computer
# audio for the mix (8.1 MB at 192 kHz), plus a little per held block and
# the pipe's own buffer. Computed from these constants, not measured.
# The page shows draft text (#104 slice 3), so the switch can be turned on.
# False keeps drafts off whatever the settings file says (fail closed): no
# worker starts whose text nobody can see, and set_settings refuses the switch.
ON_PAGE = True

# What the draft follows when computer audio is recorded too (setting
# live_drafts_audio; the owner's decision of 22 Oct 2026). The microphone only
# while computer audio is off, whatever the choice.
MICROPHONE, MIX = "microphone", "mix"
AUDIO_CHOICES = (MICROPHONE, MIX)

# Session states (what the page part will show, #104 slice 3):
STARTING, RUNNING, BATTERY, FAILED, UNAVAILABLE, STOPPED = (
    "starting", "running", "battery", "failed", "unavailable", "stopped")
NOTES = {
    STARTING: "Getting draft text ready.",
    BATTERY: "Draft text is paused while on battery. The recording carries on.",
    FAILED: "Draft text stopped. The recording is not affected.",
    UNAVAILABLE: "Draft text is not available. The recording is not affected.",
}
# Why the app has no draft session for this recording (Workspace._draft_why),
# in the page's words. "off" (the switch) shows nothing.
REASONS = {
    "managed": "Draft text is turned off by your organisation. The recording is not affected.",
    "battery": NOTES[BATTERY],
    "busy": "Draft text waits while a speech model is downloaded or removed.",
    "unavailable": (f"Draft text is not available: {asr_engines.label(DRAFT_MODEL)} is not "
                    "downloaded or needs repair (Settings, Manage downloads). The recording "
                    "is not affected."),
    "ended": ("Draft text stopped for this recording because a speech model was downloaded "
              "or removed. The recording is not affected."),
    "failed": NOTES[FAILED],
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


class LiveMix:
    """The draft's live mix of the two tracks (#104 with #108), made in the
    draft worker. Microphone audio arrives with its place in the recording;
    computer audio arrives placed on the microphone's time line (at: the
    microphone frame of its first sample, from the tracks' arrival times)
    and is resampled to the microphone's rate by linear interpolation. Each
    stretch of microphone audio waits until the computer audio for it has
    arrived, or MIX_HOLD_S at most, then goes on mixed as the recording's own
    mix is (the mean of the two, workspace/mixdown.py). Where no computer
    audio covers it, the microphone goes on alone and unchanged. Draft
    quality: the recording and its mix are never touched. Returns
    [(start_frame, int16 pcm)] in order."""

    def __init__(self, rate):
        self.rate = int(rate)
        self._hold = max(1, int(round(MIX_HOLD_S * self.rate)))
        self._reach = int(round(MIX_REACH_S * self.rate))
        self._mic = collections.deque()      # (start, pcm) waiting for the computer's
        self._comp = collections.deque()     # (at, float32 at the mic rate), by arrival
        self._held = 0                       # frames in _comp
        self._max_held = max(1, int(MIX_HELD_S * self.rate))
        self._comp_end = None                # mic frame the computer audio has reached
        self._mic_end = None                 # end of the newest microphone audio

    @property
    def mixing(self):
        return self._comp_end is not None

    def computer(self, at, rate, pcm):
        """A piece of computer audio at its place on the microphone's time
        line. Returns the microphone audio it lets go on."""
        x = np.asarray(pcm, dtype=np.int16).astype(np.float32)
        rate, at = int(rate), int(at)
        if not len(x) or (self._mic_end is not None
                          and abs(at - self._mic_end) > self._reach):
            return []                        # empty, or nowhere near the microphone's audio
        if rate != self.rate:
            n = max(1, int(round(len(x) * self.rate / rate)))
            x = np.interp(np.arange(n) * (rate / self.rate),
                          np.arange(len(x)), x).astype(np.float32)
        room = self._max_held - self._held
        if len(x) > room:                    # bounded by frames (#371): the newest goes
            x = x[:max(0, room)].copy()
            if not len(x):
                return self._ready(False)
        self._comp.append((at, x))
        self._held += len(x)
        while len(self._comp) > MIX_PIECES:
            self._let_go()
        end = at + len(x)
        self._comp_end = end if self._comp_end is None else max(self._comp_end, end)
        return self._ready(False)

    def mic(self, start, pcm):
        start, pcm = int(start), np.asarray(pcm, dtype=np.int16)
        self._mic_end = start + len(pcm)
        if not self.mixing:
            return [(start, pcm)]
        self._mic.append((start, pcm))
        return self._ready(False)

    def flush(self):
        """Everything held goes on now (a pause)."""
        return self._ready(True)

    def off(self):
        """The draft follows the microphone again: what is held goes on, and
        computer audio is no longer waited for."""
        out = self._ready(True)
        self._comp.clear()
        self._held = 0
        self._comp_end = None
        return out

    def _let_go(self):
        self._held -= len(self._comp.popleft()[1])

    def _ready(self, force):
        out = []
        while self._mic:
            start, pcm = self._mic[0]
            end = start + len(pcm)
            if not force and self._comp_end < end and self._mic_end - end < self._hold:
                break                        # the computer audio for it may still come
            self._mic.popleft()
            out.append((start, self._mixed(start, pcm)))
        # Computer audio that ends before the microphone audio still to come
        # is done with.
        upto = self._mic[0][0] if self._mic else (out[-1][0] + len(out[-1][1]) if out
                                                  else None)
        while upto is not None and self._comp and \
                self._comp[0][0] + len(self._comp[0][1]) <= upto:
            self._let_go()
        return out

    def _mixed(self, start, pcm):
        n = len(pcm)
        other = np.zeros(n, dtype=np.float32)
        covered = np.zeros(n, dtype=bool)
        for at, x in self._comp:             # a later piece wins where two overlap
            a, b = max(at, start), min(at + len(x), start + n)
            if a < b:
                other[a - start:b - start] = x[a - at:b - at]
                covered[a - start:b - start] = True
        if not covered.any():
            return pcm
        mixed = pcm.astype(np.float32)
        mixed[covered] = (mixed[covered] + other[covered]) / 2.0
        return np.clip(np.round(mixed), -32768, 32767).astype(np.int16)


# -- the draft worker's loop (runs in the draft worker process) ---------------------

MAX_FRAME = MAX_RATE * 3600 * 24    # a place in a recording (frames): under a day at most


def _whole(value, lo, hi):
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _sized(data):
    return isinstance(data, bytes) and 0 < len(data) <= 2 * MAX_BATCH_FRAMES


def _pcm(data):
    return np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2")


def clean_text(text):
    """A phrase's text as plain, printable text of bounded length."""
    text = "".join(ch for ch in str(text) if ch >= " " or ch == "\t")
    return " ".join(text.split())[:MAX_TEXT]


def serve(conn, load, log=None):
    """The draft worker's loop: load the engine, then turn audio batches into
    phrases until the pipe closes or "exit" arrives. load() returns an engine
    with speech_prob(window), reset() and transcribe_phrase(audio). Messages:
    ("audio", start, pcm bytes) from the microphone, ("computer", at, rate,
    pcm bytes) for the live mix, (MIX_OFF,), (FLUSH,) and ("exit",); any
    other is ignored. Replies: ("ready", description, load_s), ("phrase",
    start_s, end_s, text), ("skipped", seconds), ("error", type, message, op,
    code)."""
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
    mix = LiveMix(rate)

    def feed(pieces):
        for start, pcm in pieces:
            queue_phrases(pipeline.feed(start, pcm))
    try:
        while True:
            if log is not None:
                log.limit_crash_file()       # recovered native errors stay small (#278)
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                return
            if not isinstance(msg, tuple) or not msg:
                continue
            try:
                if msg[0] == "audio" and len(msg) == 3 and _whole(msg[1], 0, MAX_FRAME) \
                        and _sized(msg[2]):
                    feed(mix.mic(msg[1], _pcm(msg[2])))
                elif msg[0] == "computer" and len(msg) == 4 \
                        and _whole(msg[1], -MAX_FRAME, MAX_FRAME) \
                        and _whole(msg[2], 8000, MAX_RATE) and _sized(msg[3]):
                    feed(mix.computer(msg[1], msg[2], _pcm(msg[3])))
                elif msg[0] == MIX_OFF:
                    feed(mix.off())
                elif msg[0] == FLUSH:
                    feed(mix.flush())
                    queue_phrases(pipeline.flush())
                elif msg[0] == "exit":
                    return
            except Exception as exc:
                # Scoring or splitting failed: recorded with its frames, and the
                # app told, so drafts end for this recording (not restarted).
                code = diag.incident("worker", exc, op="draft")
                send("error", type(exc).__name__, safe_message(exc), "draft", code)
                return
    finally:
        with cond:
            state["done"] = True
            cond.notify()


# -- the app's side: one session per recording -----------------------------------------

class _ComputerTap:
    """The live mix's computer audio on its way to the feeder: (at, block)
    in arrival order, with (MIX_OFF, None) markers. Bounded by frames and
    blocks (see COMPUTER_TAP_FRAMES): a block longer than COMPUTER_BLOCK_S,
    or one that does not fit, is cut, keeping its start, and once full the
    newest audio is lost. A marker always goes in, so the worker always
    hears that the mix is off, and audio put after it (a writer already in
    tap_computer when the mix ended) is refused until open() again. put()
    and mark() never block. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = collections.deque()
        self._frames = 0
        self._open = False
        self.limit = COMPUTER_TAP_FRAMES     # frames; set by open()
        self.block_limit = 1                 # frames; set by open()

    def open(self, rate):
        """Take computer audio at rate from now on (DraftSession.mix)."""
        with self._lock:
            self.limit = min(int(TAP_S * rate), COMPUTER_TAP_FRAMES)
            self.block_limit = max(1, int(COMPUTER_BLOCK_S * rate))
            self._open = True

    def put(self, at, block):
        """Returns the frames that did not fit (lost for the draft only)."""
        n = len(block)
        with self._lock:
            if not self._open:
                return 0                     # after the mix ended: not draft audio now
            room = 0 if len(self._items) >= COMPUTER_TAP_BLOCKS \
                else min(self.block_limit, max(0, self.limit - self._frames))
            if n > room:
                # A copy, so the rest of a large block is not kept alive.
                block = block[:room].copy() if room else block[:0]
            if len(block):
                self._items.append((at, block))
                self._frames += len(block)
        return n - len(block)

    def mark(self):
        with self._lock:
            self._open = False
            self._items.append((MIX_OFF, None))

    def get_nowait(self, upto=math.inf):
        """The next item, but audio only if it starts at or before upto (a
        microphone frame); queue.Empty otherwise."""
        with self._lock:
            if not self._items or (self._items[0][1] is not None
                                   and self._items[0][0] > upto):
                raise queue.Empty
            at, block = self._items.popleft()
            if block is not None:
                self._frames -= len(block)
            return at, block

    def clear(self):
        with self._lock:
            self._items.clear()
            self._frames = 0


class _ComputerBatch:
    """The feeder's computer audio for the live mix, sent in pieces of about
    BATCH_S. Blocks follow on in the computer's own time (the loopback fills
    silence), so a piece starts at its first block's place on the
    microphone's time line; a block more than MIX_JUMP_S from where the piece
    says it falls starts a new piece. A piece is sent once it reaches
    BATCH_S, so it holds at most BATCH_S plus one block."""

    MIX_JUMP_S = 0.1

    def __init__(self, session):
        self.session = session
        self.blocks, self.at, self.frames = [], None, 0

    def take(self, upto=math.inf):
        """Take computer audio that starts at or before upto (a microphone
        frame: about the microphone audio taken so far), and the markers
        after it. The rest stays in the bounded tap, so after a backlog it
        reaches the worker in step with the microphone audio (#371)."""
        s = self.session
        while True:
            try:
                at, block = s._ctap.get_nowait(upto)
            except queue.Empty:
                return
            if at == MIX_OFF:
                self.send()
                s._send((MIX_OFF,))
                continue
            rate = s.computer_rate
            if rate is None or block is None or not len(block):
                continue
            if self.blocks:
                expect = self.at + self.frames * s.rate / rate
                if abs(at - expect) > self.MIX_JUMP_S * s.rate:
                    self.send()
            if not self.blocks:
                self.at = int(at)
            self.blocks.append(block)
            self.frames += len(block)
            if self.full():
                self.send()

    def full(self):
        rate = self.session.computer_rate
        return bool(self.blocks) and rate is not None and self.frames >= BATCH_S * rate

    def send(self):
        if self.blocks and self.session.computer_rate is not None:
            self.session._send_computer(self.at, self.session.computer_rate, self.blocks)
        self.blocks, self.at, self.frames = [], None, 0


class DraftSession:
    """Live drafts for one recording, in the app process. tap(start, block) is
    called by the recorder's writer thread for each block; it never blocks
    or raises. With the live mix on (mix(rate)), tap_computer(at, block) is
    called the same way for each computer block. start() starts the draft
    worker; stop() ends it and forgets the text. suspend() ends the worker
    but keeps the text (battery); resume() starts it again. Thread-safe."""

    def __init__(self, recording_id, rate, cache_dir, *, generation=0, model=DRAFT_MODEL,
                 threads=DRAFT_THREADS, log=None, allow_test_engines=False, target=None):
        self.recording_id, self.rate, self.generation = recording_id, int(rate), generation
        self.cache_dir, self.model, self.threads = str(cache_dir), model, threads
        self.log, self.allow_test_engines = log, bool(allow_test_engines)
        self._target = target            # tests: another process target
        self._lock = threading.Lock()
        # TAP_S of the recorder's blocks (TAP_BLOCK_S); a pause's marker takes one.
        self._tap = queue.Queue(maxsize=max(1, int(round(TAP_S / TAP_BLOCK_S))))
        # Computer audio for the live mix, bounded by frames and losing the
        # newest when full, as _tap does (see COMPUTER_TAP_FRAMES).
        self._ctap = _ComputerTap()
        self._feeding = False            # the tap queues audio (not stopped or suspended)
        self._mixing = False             # tap_computer queues computer audio (live mix)
        self.computer_rate = None
        self._phrases = []               # [{kind, text, start, end} | {kind, seconds}]
        self._first = 0                  # index of _phrases[0] (older ones were let go)
        self.state, self.reason = STARTING, None
        self.restarts = 0
        self.skipped_s = 0.0
        # Draft audio lost: by the tap (the writer thread only) and by the
        # feeder (its thread only), each counted by its own thread.
        self._tap_dropped_s = self._send_dropped_s = 0.0
        # Computer audio the full computer tap lost (writer thread only): the
        # draft of that time follows the microphone alone, so it is counted
        # apart from dropped_s and is not a skipped note (no text is missing).
        self._computer_dropped_s = 0.0
        self._phrase_count = 0          # phrases kept, not counting skipped notes
        self.load_s = None
        self.description = None
        self._proc = self._conn = None
        self._stopped = False
        self._ended = threading.Event()  # stop() has finished: the worker has exited
        self._ready = threading.Event()
        self._threads = []

    # -- recorder side (writer thread): never blocks, never raises ---------------------

    def tap(self, start, block):
        """start: where the block begins in the recording, in frames at the
        device rate (paused time is not recorded, so not counted). block None:
        the recording paused here (after the blocks before it), so the phrase
        in progress ends."""
        if not self._feeding:
            return
        try:
            self._tap.put_nowait((FLUSH, None) if block is None else (start, block))
        except queue.Full:
            if block is not None:
                self._tap_dropped_s += len(block) / self.rate

    def tap_computer(self, at, block):
        """A computer block for the live mix. at: where it begins on the
        microphone's time line, in microphone frames. Draft only: a full
        tap loses it (the newest first) for the draft, never for the
        recording."""
        if not (self._feeding and self._mixing):
            return
        lost = self._ctap.put(at, block)
        rate = self.computer_rate
        if lost and rate:
            self._computer_dropped_s += lost / rate

    @property
    def dropped_s(self):
        """Seconds of microphone draft audio lost (a full tap, or a worker
        that was gone)."""
        return self._tap_dropped_s + self._send_dropped_s

    @property
    def computer_dropped_s(self):
        """Seconds of computer audio a full computer tap lost (live mix)."""
        return self._computer_dropped_s

    @property
    def mixing(self):
        return self._mixing

    def mix(self, rate):
        """Follow a live mix of the microphone and the computer track (at
        its rate) from now on. The recorder's tap_computer is set after."""
        if not (isinstance(rate, int) and not isinstance(rate, bool)
                and 8000 <= rate <= MAX_RATE):
            raise ValueError("Unsupported sample rate.")
        with self._lock:
            if self._stopped:
                return False
            self.computer_rate = rate
            self._ctap.open(rate)
            self._mixing = True
            return True

    def unmix(self):
        """Follow the microphone alone again (the setting changed, or the
        computer track stopped). The recorder's tap_computer is cleared
        first; the worker is told after the computer audio already queued."""
        with self._lock:
            if not self._mixing:
                return
            self._mixing = False
        self._ctap.mark()                # never lost to a full tap

    # -- control --------------------------------------------------------------------

    def open(self):
        """Hold audio from the tap from now on (up to TAP_S), before the
        worker starts: the first words are not missed while the main worker
        lets go of its model and the draft worker loads. False once stopped."""
        with self._lock:
            if self._stopped:
                return False
            self._feeding = True
            return True

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
        self._ready.clear()
        self.state = STARTING
        self._proc, self._conn = proc, parent
        reader = threading.Thread(target=self._read_loop, args=(proc, parent),
                                  name="draft-reader", daemon=True)
        reader.start()
        self._threads.append(reader)

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
        """End drafts for good: the worker is ended and the text forgotten.
        Returns once the worker has exited, also when another thread's stop
        is still under way (D4: processing waits for that)."""
        with self._lock:
            already = self._stopped
        if already:
            self._ended.wait(self._end_wait() + STOP_JOIN_S * (len(self._threads) + 1))
            return
        with self._lock:
            if self._stopped:
                return
            self._stopped, self._feeding = True, False
            self.state = STOPPED
            proc, conn = self._proc, self._conn
            self._proc = self._conn = None
            kept = self._phrase_count
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
                           dropped_s=round(self.dropped_s, 1),
                           computer_dropped_s=round(self.computer_dropped_s, 1),
                           load_s=self.load_s)
        self._ended.set()

    @staticmethod
    def _end_wait():
        from workspace.worker import END_WAIT_S
        return END_WAIT_S

    def _end_process(self, proc, conn, died=False):
        if proc is not None:
            if proc.is_alive():
                proc.terminate()
            # Until it has ended, Windows keeps its crash file open, so it
            # could not be read and deleted (#278): waited for as long as the
            # main worker is.
            proc.join(self._end_wait())
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
                break
        self._ctap.clear()

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
        comp = _ComputerBatch(self)
        taken = -math.inf                # end of the microphone audio taken (frames)
        while True:
            try:
                start, block = self._tap.get(timeout=0.2)
            except queue.Empty:
                start = block = None
                if self._stopped:
                    return
                if not batch:
                    comp.take()          # no microphone audio just now (a pause)
                    comp.send()
                    continue
            if start is None and block is None and self._stopped:
                return
            flush = start == FLUSH
            if block is not None:
                taken = start + len(block)
            # The computer audio for about the microphone audio taken so far
            # (live mix), the rest left in the bounded tap (#371). At a pause
            # too: during a backlog the tap already holds the audio from after
            # the resume, placed straight after the pause (paused time is not
            # counted), which must not go ahead of its microphone audio. An
            # idle feeder takes the rest (above).
            comp.take(taken + limit)
            if block is not None:
                if batch and start != batch_start + batch_frames:
                    comp.send()
                    self._send_batch(batch_start, batch)     # not contiguous: send apart
                    batch, batch_frames = [], 0
                if not batch:
                    batch_start = start
                batch.append(block)
                batch_frames += len(block)
            if batch and (batch_frames >= limit or flush or block is None):
                # The computer audio for the same time first, so the worker's
                # mix need not wait for it.
                comp.send()
                self._send_batch(batch_start, batch)
                batch, batch_frames = [], 0
            elif comp.full() or (block is None and not flush):
                comp.send()
            if flush:
                comp.send()
                self._send((FLUSH,))

    def _send_batch(self, start, blocks):
        pcm = np.concatenate(blocks).astype("<i2")
        for i in range(0, len(pcm), MAX_BATCH_FRAMES):
            part = pcm[i:i + MAX_BATCH_FRAMES]
            if not self._send(("audio", start + i, part.tobytes())):
                self._send_dropped_s += len(part) / self.rate

    def _send_computer(self, at, rate, blocks):
        pcm = np.concatenate(blocks).astype("<i2")
        for i in range(0, len(pcm), MAX_BATCH_FRAMES):
            part = pcm[i:i + MAX_BATCH_FRAMES]
            self._send(("computer", at + int(round(i * self.rate / rate)), rate,
                        part.tobytes()))

    def _send(self, msg):
        # Not before the worker is ready (it reads nothing while it loads), and
        # never after a stop. A worker that is gone loses this audio, not the
        # recording.
        # A worker being restarted (after a crash, or on mains power again)
        # is waited for: the tap holds the audio meanwhile.
        while True:
            if self._stopped or not self._feeding:
                return False
            if self._ready.wait(0.2):
                conn = self._conn
                if conn is not None:
                    break
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
            self.load_s = msg[2] if isinstance(msg[2], (int, float)) \
                and not isinstance(msg[2], bool) and math.isfinite(msg[2]) else None
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
        """Call with _lock held. MAX_KEPT counts phrases and skipped notes."""
        if item["kind"] == "phrase":
            self._phrase_count += 1
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
            self._ready.clear()              # the feeder waits for the next worker
            self.state = STARTING
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
