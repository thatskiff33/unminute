# workspace/recorder.py
# Interruption-safe microphone recording (#30). Off unless switched on in
# Settings. One recording at a time, from one chosen input device.
#
#   - Capture runs in PortAudio's callback (via sounddevice). The callback only
#     copies each block into a bounded queue; a writer thread does all disk work.
#   - Live drafts (#104, off by default) take a copy of each block from the
#     writer thread through `tap`, never from the callback. The tap must not
#     block; if it raises, it is turned off and the recording carries on.
#   - Audio is written in durable chunks (CHUNK_S seconds of 16-bit mono PCM):
#     each chunk is written to a temporary file, flushed to disk and renamed,
#     then the journal (journal.json) is replaced atomically. A crash loses at
#     most the chunk being filled plus what was queued.
#   - Nothing is ever silently dropped or switched: an overrun, a full queue,
#     a lost device or a full disk is recorded in the journal with its time and,
#     where known, its length, and shown to the user. A lost device stops the
#     recording; it never moves to another device.
#   - On stop, the chunks become one WAV file that is imported like any other
#     recording (same review, cleanup and hold lifecycle), then the recording
#     folder is removed. If the app closed while recording, the next start
#     recovers what was saved and reports the interval that may be missing.
#
# Computer audio (#108, groundwork; off unless the app asks for it): a second
# track, captured by workspace/loopback.py, with its own queue and its own
# chunk files (sys-NNNNNN.pcm) saved by the same writer thread. The journal
# then has "format": 2, a "tracks" entry for it, and timing marks for both
# tracks; a microphone-only journal is unchanged (format 1, no format field).
# Silence on the computer track is normal (nothing playing), so the lost-device
# rule is the microphone's alone. Unlike the microphone, the computer track
# follows the Windows default output when it changes, with an event (the
# owner's decision 6); a computer track that fails stops on its own and the
# microphone carries on. On stop the two tracks become ONE mix
# (workspace/mixdown.py), which is transcribed once (decision 10).
#
# No audio leaves the computer; the recording folder lives inside the workspace.

import collections
import json
import os
import queue
import shutil
import threading
import time
import uuid
import wave
from pathlib import Path

import numpy as np

CHUNK_S = 5                  # durable unit; also the most a crash can lose (plus the queue)
QUEUE_S = 20                 # seconds of audio buffered between callback and writer
BLOCK_S = 0.1                # PortAudio block length
SILENT_DEVICE_S = 3.0        # no audio at all for this long while recording: device lost
# Sleep (the lid closed, the power button) freezes the whole app. A watchdog
# thread beats every ALIVE_S; a beat later than SLEEP_S on the monotonic clock
# means the app was frozen by sleep. Changing the time cannot fake it,
# and a slow save or a microphone that stalls does not delay the watchdog. The
# recording stops with state "slept", keeping what came before; audio after
# the wake is not joined on. Every check reads the beat before writing its own
# mark, so whichever thread runs first after the wake notices.
ALIVE_S = 0.2
# A late beat is sleep only beyond SLEEP_S: the app can be held up for a few
# seconds without sleeping (heavy paging, a busy computer). A shorter late beat
# beyond SILENT_DEVICE_S is noted as a "stall" with its length, and the
# recording carries on; the lost-device check still decides if audio is gone.
SLEEP_S = 15.0
HEARTBEAT_S = 1.0            # how often the "still recording" time is noted
LID_NOTE_S = 5.0             # a lid closed this long while recording is noted on opening
MAX_HOURS = 8                # a forgotten recording stops itself
# Journal formats this version reads: 1 (no "format" field: the microphone
# only) and 2 (with a computer track). A newer one is kept, never half-imported.
JOURNAL_FORMAT = 2
MARK_EVERY_S = 30            # a timing mark at least this often, per track
COMPUTER = "computer"
COMPUTER_PREFIX = "sys-"     # the computer track's chunk files
# Live drafts (#104): put in the writer's queue at a pause, after the blocks
# captured before it, so the draft's phrase in progress ends at the pause.
_PAUSED = object()


class RecorderError(ValueError):
    """A refused request; the message is safe to show."""


def list_devices():
    """Input devices as [{index, name, default, rate}]; empty if none.
    Raises RecorderError if the audio library itself cannot load, so a broken
    installation is not mistaken for "no microphone"."""
    try:
        import sounddevice as sd
    except Exception as exc:         # ImportError, or PortAudio failing to load (OSError)
        raise RecorderError("Microphone support could not start in this installation. "
                            "Reinstall the app, or contact support.") from exc
    try:
        with _streams_lock:
            if _open_streams == 0 and hasattr(sd, "_terminate"):
                # PortAudio lists devices once; refresh so a plugged or
                # unplugged microphone is seen (only when nothing is open).
                sd._terminate()
                sd._initialize()
            devices = sd.query_devices()
            default_in = sd.default.device[0]
            hostapis = sd.query_hostapis()
    except Exception:
        return []
    # MME cuts names at 31 characters ("Logitech C920 Microphone (HD Pr"); the
    # other Windows APIs list the same device in full. The MME name stays the
    # key (it is what is opened and matched); the full one is shown.
    full = [_clean_name(d["name"]) for d in devices
            if d.get("max_input_channels", 0) > 0 and hostapis
            and hostapis[d["hostapi"]]["name"] != "MME"]
    out = []
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) < 1:
            continue
        api = hostapis[d["hostapi"]]["name"] if hostapis else ""
        # One entry per physical input: prefer the Windows default API (MME)
        # entries, which every device has and which convert formats for us.
        if api and api != "MME":
            continue
        # The Sound Mapper follows the Windows default input, so it could
        # change device mid-recording; only real devices are offered.
        if str(d["name"]).startswith("Microsoft Sound Mapper"):
            continue
        name = str(d["name"])[:120]
        out.append({"index": i, "name": name, "label": _full_name(name, full),
                    "default": i == default_in,
                    "rate": int(d.get("default_samplerate") or 44100)})
    return out


def _clean_name(name):
    """Without the replacement characters PortAudio leaves for symbols such as ®."""
    return " ".join(str(name).replace("\ufffd", " ").split())


def _full_name(name, full):
    """The untruncated name for a (possibly cut) MME name, or the name itself."""
    short = _clean_name(name)
    if len(str(name)) < 31:
        return short[:120]
    longer = {f for f in full if f.startswith(short) and len(f) > len(short)}
    # Only when it is unambiguous: two devices can share the first 31 characters.
    return (longer.pop() if len(longer) == 1 else short)[:120]


# Open PortAudio streams in this process; the device list is only refreshed
# when there are none.
_streams_lock = threading.Lock()
_open_streams = 0


def _stream_opened(delta):
    global _open_streams
    with _streams_lock:
        _open_streams = max(0, _open_streams + delta)


def _retrying(fn, attempts=3, wait=0.5):
    """A short-lived Windows lock (antivirus, indexer) must not end a
    recording: retry a write briefly. A full disk is not retried."""
    for i in range(attempts):
        try:
            return fn()
        except OSError as exc:
            if getattr(exc, "errno", None) == 28 or i == attempts - 1:
                raise
            time.sleep(wait)


def measure_level(device, rate, seconds=3.0, open_stream=None):
    """Microphone test: listen for a few seconds and return the loudest and
    average level in dBFS. Nothing is stored."""
    levels, done = [], threading.Event()

    def callback(indata, frames, time_info, status):
        levels.append(_level_db(np.array(indata, dtype=np.int16).reshape(-1)))

    _stream_opened(+1)
    try:
        stream = (open_stream or _sounddevice_stream)(device, rate, callback, done.set)
        try:
            stream.start()
            done.wait(seconds)
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
    finally:
        _stream_opened(-1)
    if not levels:
        raise RecorderError("No sound arrived from that microphone. Check that it is "
                            "connected and allowed in Settings > Privacy > Microphone.")
    peak = float(max(levels))
    return {"peak_db": round(peak, 1), "average_db": round(float(np.mean(levels)), 1),
            "heard": bool(peak > -50)}


def _sounddevice_stream(device, rate, callback, finished):
    import sounddevice as sd
    return sd.InputStream(device=device, channels=1, samplerate=rate, dtype="int16",
                          blocksize=int(rate * BLOCK_S), callback=callback,
                          finished_callback=finished)


def _write_atomic(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# The recording display: a few numbers per block for the page to draw. Only the
# latest 100 ms block is held for it, while recording, and it is dropped on
# pause, stop and device loss. Bands are log-spaced across the speech range; the
# wave is a short, thinned slice of the block for the "Sound wave" display.
VIZ_BANDS = 40
VIZ_LOW_HZ, VIZ_HIGH_HZ = 80.0, 8000.0
VIZ_WAVE_POINTS = 128
_band_cache = {}


def _band_edges(n, rate):
    """Bin index ranges of the display bands for an n-sample block."""
    key = (n, rate)
    if key not in _band_cache:
        freqs = np.fft.rfftfreq(n, 1.0 / rate)
        edges = np.geomspace(VIZ_LOW_HZ, min(VIZ_HIGH_HZ, rate / 2), VIZ_BANDS + 1)
        idx = np.searchsorted(freqs, edges)
        spans = [(int(a), max(int(a) + 1, int(b))) for a, b in zip(idx[:-1], idx[1:], strict=True)]
        _band_cache[key] = (spans, np.hanning(n).astype(np.float32), freqs)
    return _band_cache[key]


def visual(block, rate):
    """Display data for one block: band levels (dBFS of the loudest bin in each
    band), the dominant pitch in Hz (0 when quiet) and a thinned wave (-1..1)."""
    x = block.astype(np.float32) / 32768.0
    n = len(x)
    if n < 64:
        return {"bands": [-96.0] * VIZ_BANDS, "pitch_hz": 0, "wave": [0.0] * VIZ_WAVE_POINTS}
    spans, window, freqs = _band_edges(n, rate)
    mags = np.abs(np.fft.rfft(x * window)) / (n / 4.0)      # a full-scale sine reads 0 dBFS
    db = 20.0 * np.log10(np.maximum(mags, 1e-5))
    bands = [round(float(db[a:b].max()), 1) for a, b in spans]
    voice = (freqs >= VIZ_LOW_HZ) & (freqs <= 1000.0)
    peak = int(np.argmax(np.where(voice, mags, 0.0)))
    pitch = round(float(freqs[peak])) if db[peak] > -60.0 else 0
    tail = x[-min(n, VIZ_WAVE_POINTS * 8):]
    step = max(1, len(tail) // VIZ_WAVE_POINTS)
    wave = [round(float(v), 3) for v in tail[::step][:VIZ_WAVE_POINTS]]
    return {"bands": bands, "pitch_hz": pitch, "wave": wave}


def _level_db(block):
    if block.size == 0:
        return -96.0
    rms = float(np.sqrt(np.mean((block.astype(np.float32) / 32768.0) ** 2)))
    return max(-96.0, float(20.0 * np.log10(rms))) if rms > 0 else -96.0


class Recording:
    """One recording session. `open_stream(device, rate, callback, finished)`
    returns an object with start(), stop() and close(): sounddevice by default,
    a fake in tests."""

    def __init__(self, folder, device, device_name, rate, open_stream=_sounddevice_stream,
                 clock=time.time, tick=time.monotonic, computer=None):
        """computer: None (the microphone only), or a dict for a second
        track: {"open": open_loopback(source, callback, notify), "source":
        the loopback source, "rate": its rate, "block_s": how often it
        delivers a block (sizes its queue to QUEUE_S, as the microphone's)}."""
        self.folder = Path(folder)
        self.rate = int(rate)
        self._clock = clock               # wall clock: journal times only
        self._tick = tick                 # gaps and silence: changing the time cannot move it
        self._open_stream = open_stream
        self._queue = queue.Queue(maxsize=max(1, int(QUEUE_S / BLOCK_S)))
        self._buffer = []
        self._buffered = 0
        self._lock = threading.Lock()
        self.paused = False
        self.level_db = -96.0
        self._levels = collections.deque(maxlen=50)   # recent [block number, level], for the meter
        self._level_seq = 0                           # blocks measured so far
        self._display_block = None                    # (block number, latest block) to draw
        self._viz_cache = None                        # (block number, its display data)
        self._last_audio = tick()
        self._alive = tick()                    # the watchdog's latest beat
        self._froze_at = None                   # when the watchdog last found a sleep
        self._stall_end = float("-inf")         # when the watchdog last found a stall
        self._started_tick = tick()             # for the time limit (set again at start)
        self._lid_closed_at = None              # when the lid was closed, while recording
        self._watchdog = None
        self._close_lock = threading.Lock()     # one thread closes the stream after a sleep
        self._stop = threading.Event()         # the writer's signal to finish
        self._stopping = threading.Event()      # a deliberate stop (not a lost device)
        self._writer = None
        self._stream = None
        # Live drafts (#104): tap(start, block) for each block the writer
        # takes, start being where it begins in the recording (frames).
        self.tap = None
        self.journal = {
            "id": self.folder.name, "device": device, "device_name": device_name,
            "rate": self.rate, "started_at": clock(), "state": "recording",
            "chunks": [], "frames": 0, "events": [], "last_seen": clock()}
        self._mic = _TrackTiming(self.rate)
        self._computer = None
        if computer is not None:
            block_s = float(computer.get("block_s", BLOCK_S))
            self._computer = _ComputerTrack(computer, max(1, int(QUEUE_S / block_s)))
            self.journal.update(format=2, marks=[], tracks={COMPUTER: {
                "source": dict(computer["source"]), "rate": self._computer.rate,
                "chunks": [], "frames": 0, "marks": [], "state": "recording",
                "device_name": None}})

    # -- capture (PortAudio callback thread: no disk work, no blocking) --------------

    def _callback(self, indata, frames, time_info, status):
        now = self._tick()
        frozen = self._frozen(now)          # read before writing our own mark
        held_up = now - self._alive > SILENT_DEVICE_S
        gap, previous = now - self._last_audio, self._last_audio
        self._last_audio = now
        if self._stop.is_set():
            return                 # stopped (or slept, or lost): nothing more is captured
        if frozen and not self.paused:
            self._slept()          # the first audio after a sleep: not joined on
            return
        # The microphone went quiet for longer than the silence limit and came
        # back before the silence check ran: the gap is reported, never hidden.
        # (A hold-up of the whole app is the watchdog's "stall" instead.)
        if (gap > SILENT_DEVICE_S and not self.paused and not held_up
                and self._stall_end < previous):
            self._event("gap", seconds=gap, note="No audio came from the microphone here.")
        if status and getattr(status, "input_overflow", False):
            self._event("overrun", note="The computer could not keep up; a short piece of "
                                        "audio was lost here.")
        if self.paused:
            self.level_db = -96.0
            self._display_block = None
            return
        block = np.array(indata, dtype=np.int16).reshape(-1)
        self.level_db = _level_db(block)
        self._level_seq += 1
        self._levels.append((self._level_seq, round(self.level_db, 1)))
        # Display data is worked out only when the page asks (status), not for
        # every block; replaced whole, so a reader sees one block.
        self._display_block = (self._level_seq, block)
        try:
            # With its timing (the block's first frame, by when it arrived),
            # used only to line up a computer track.
            self._queue.put_nowait((block, now - len(block) / self.rate,
                                    self._mic.take_segment()))
        except queue.Full:
            self._mic.new_segment()       # a gap in the saved track: time jumps here
            self._event("dropped", seconds=len(block) / self.rate,
                        note="Saving fell behind; this audio was lost.")

    def _finished(self):
        # PortAudio also calls this when we stop the stream ourselves.
        if not self._stopping.is_set() and not self._stop.is_set():
            self._device_lost()

    # -- the computer track (its reader thread) -------------------------------------

    def _computer_callback(self, block, stamp):
        """A block of computer audio (16-bit mono) and the monotonic time of
        its first frame. Never decides that a device is lost: silence is normal."""
        track = self._computer
        if (track is None or not track.on or self._stop.is_set() or self.paused
                or self._frozen(self._tick())):
            return
        block = np.asarray(block, dtype=np.int16).reshape(-1)
        track.level_db = _level_db(block)
        try:
            track.queue.put_nowait((block, stamp, track.timing.take_segment()))
        except queue.Full:
            track.timing.new_segment()
            self._event("computer_dropped", seconds=len(block) / track.rate,
                        note="Saving fell behind; this computer audio was lost.")

    def _computer_notify(self, kind, name):
        """The computer track's device changed (decision 6), or it failed."""
        track = self._computer
        if track is None or not track.on or self._stop.is_set():
            return
        if kind == "switched":
            with self._lock:
                self.journal["tracks"][COMPUTER]["device_name"] = name
            self._event("computer_switched", note="Computer audio now records the new "
                                                  "Windows default output.")
        elif kind == "reopened":
            with self._lock:
                self.journal["tracks"][COMPUTER]["device_name"] = name
            self._event("computer_reopened", note="Computer audio was interrupted and "
                                                  "carries on from the same output.")
        elif kind == "no_output":
            self._event("computer_no_output", note="Computer audio has no output device to "
                                                   "record. Silence is recorded until one is "
                                                   "available.")
        elif kind == "failed":
            self._end_computer("failed", "computer_failed",
                               "Computer audio stopped working. The microphone carries on.",
                               close=False)

    def _end_computer(self, state, kind, note, close=True):
        """Stop the computer track only; the microphone carries on."""
        track = self._computer
        with self._lock:
            if track is None or not track.on:
                return False
            track.on = False
            track.level_db = -96.0
            self.journal["tracks"][COMPUTER]["state"] = state
        self._event(kind, note=note)
        if close:
            track.close()
        return True

    def stop_computer(self, note):
        """Policy turned computer audio off mid-recording: that track stops,
        with an event; the microphone carries on. False if it was not on."""
        return self._end_computer("policy_stop", "computer_policy_stop", note)

    @property
    def computer_on(self):
        return self._computer is not None and self._computer.on

    # -- journal ---------------------------------------------------------------------

    def _position_s(self):
        """Seconds captured so far: saved, buffered and still queued for the
        writer. Call with _lock held."""
        return (self.journal["frames"] + self._buffered) / self.rate \
            + self._queue.qsize() * BLOCK_S

    def _event(self, kind, seconds=None, note=""):
        with self._lock:
            at = self._position_s()
            self.journal["events"].append({"kind": kind, "at_s": round(at, 1),
                                           "t": self._clock(),
                                           "seconds": None if seconds is None
                                           else round(seconds, 2), "note": note})

    def _save_journal(self):
        with self._lock:
            data = json.dumps(self.journal, indent=1).encode("utf-8")
        _retrying(lambda: _write_atomic(self.folder / "journal.json", data))

    # -- writer thread -----------------------------------------------------------------

    def _flush_chunk(self):
        """Write the buffered audio as the next durable chunk."""
        if not self._buffered:
            return
        pcm = np.concatenate(self._buffer).astype("<i2").tobytes()
        name = f"chunk-{len(self.journal['chunks']):06d}.pcm"
        _retrying(lambda: _write_atomic(self.folder / name, pcm))
        with self._lock:
            self.journal["chunks"].append({"file": name, "frames": self._buffered})
            self.journal["frames"] += self._buffered
            self._buffer, self._buffered = [], 0
            marks = self._mic.take_marks()
            if self._computer is not None:      # only a two-track journal keeps them
                self.journal["marks"].extend(marks)
        self._save_journal()

    def _flush_computer(self):
        track = self._computer
        if track is None or not track.buffered:
            return
        entry = self.journal["tracks"][COMPUTER]
        pcm = np.concatenate(track.buffer).astype("<i2").tobytes()
        name = f"{COMPUTER_PREFIX}{len(entry['chunks']):06d}.pcm"
        _retrying(lambda: _write_atomic(self.folder / name, pcm))
        with self._lock:
            entry["chunks"].append({"file": name, "frames": track.buffered})
            entry["frames"] += track.buffered
            track.buffer, track.buffered = [], 0
            entry["marks"].extend(track.timing.take_marks())
        self._save_journal()

    def _take_computer(self):
        """Move what the computer track queued into its buffer (writer thread)."""
        track = self._computer
        if track is None:
            return
        entry = self.journal["tracks"][COMPUTER]
        while True:
            try:
                block, stamp, segment = track.queue.get_nowait()
            except queue.Empty:
                break
            track.timing.note(entry["frames"] + track.buffered, stamp, segment)
            track.buffer.append(block)
            track.buffered += len(block)
            if track.buffered >= CHUNK_S * track.rate:
                self._flush_computer()

    def _write_loop(self):
        last_beat = float("-inf")       # monotonic, like every interval here
        try:
            while True:
                self._close_after_sleep()        # if the callback noticed it
                try:
                    block = self._queue.get(timeout=0.2)
                except queue.Empty:
                    block = None
                if block is _PAUSED:
                    self._tap_block(None)        # live drafts: the phrase ends here
                elif block is not None:
                    block, stamp, segment = block
                    self._mic.note(self.journal["frames"] + self._buffered, stamp, segment)
                    self._tap_block(block)       # live drafts: the block alone, not the tuple
                    self._buffer.append(block)
                    self._buffered += len(block)
                elif self._stop.is_set():
                    break
                if self._buffered >= CHUNK_S * self.rate:
                    self._flush_chunk()
                self._take_computer()
                # Intervals on the monotonic clock, so setting the time back
                # cannot pause these checks; the wall clock is written only as
                # the "last seen" time that recovery after a crash compares.
                tick = self._tick()
                if tick - last_beat >= HEARTBEAT_S:
                    last_beat = tick
                    now = self._clock()
                    self.journal["last_seen"] = now
                    try:                 # only an estimate: never end a recording over it
                        (self.folder / "heartbeat").write_text(str(now), encoding="ascii")
                    except OSError:
                        pass
                    # Silence counts from the last audio or the end of a stall,
                    # whichever is later, and not while the watchdog's beat is
                    # late (the app was just held up and audio has not caught up).
                    if (not self.paused and not self._stop.is_set()
                            and tick - self._alive <= SILENT_DEVICE_S
                            and tick - max(self._last_audio, self._stall_end) > SILENT_DEVICE_S):
                        self._device_lost()
                    if tick - self._started_tick > MAX_HOURS * 3600:
                        self._event("time_limit", note=f"Stopped after {MAX_HOURS} hours.")
                        self._stop.set()
            self._close_after_sleep()
            self._flush_chunk()
            self._take_computer()
            self._flush_computer()
        except OSError as exc:
            # Disk full or unwritable: keep every chunk already saved, stop
            # capture, and say how much could not be saved.
            lost = self._buffered / self.rate + self._queue.qsize() * BLOCK_S
            track = self._computer
            if track is not None:
                # Both tracks cover the same time: the longer unsaved stretch is lost.
                with track.queue.mutex:          # the reader thread may still be adding
                    queued = sum(len(b) for b, _, _ in track.queue.queue)
                lost = max(lost, (track.buffered + queued) / track.rate)
            self._event("disk_full" if getattr(exc, "errno", None) == 28 else "write_failed",
                        seconds=lost, note="The recording could not be saved any further: "
                                           "the disk is full or not writable."
                                           + (" Both tracks stopped." if track is not None
                                              else ""))
            self.journal["state"] = "failed"
            if track is not None:
                with self._lock:
                    track.on = False
                    self.journal["tracks"][COMPUTER]["state"] = "failed"
            self._stopping.set()
            self._stop.set()
            self._close_stream()
            try:
                self._save_journal()
            except OSError:
                pass        # the journal on disk still lists every saved chunk

    def _tap_block(self, block):
        """Hand the block to live drafts (None: the recording paused here).
        Drafts never affect the recording: a tap that fails is turned off,
        with nothing in the journal."""
        tap = self.tap
        if tap is None:
            return
        try:
            tap(self.journal["frames"] + self._buffered, block)
        except Exception:
            self.tap = None

    def _frozen(self, tick):
        """The watchdog has missed its beat by more than SLEEP_S: the whole
        app is frozen, or has just woken from sleep."""
        return self._watchdog is not None and tick - self._alive > SLEEP_S

    def _watch_loop(self):
        """Beats every ALIVE_S. A beat later than SLEEP_S means the computer
        slept; later than SILENT_DEVICE_S, that the app was held up (a stall,
        noted with its length). Checked before the beat is written, so another
        thread reading the beat after the wake either finds it late or finds
        the recording already stopped."""
        while not self._stop.wait(ALIVE_S):
            tick = self._tick()
            late = tick - self._alive
            if late > SLEEP_S:
                self._froze_at = tick
                if not self.paused:       # paused: nothing was being recorded
                    self._slept()
                    self._close_after_sleep()
            elif (late > SILENT_DEVICE_S and not self.paused
                  and self.journal["state"] == "recording"):
                self._stall_end = tick            # audio gets a full silence period again
                self._event("stall", seconds=late,
                            note="The computer was held up; audio from this moment may "
                                 "be missing.")
            self._alive = tick

    def _close_after_sleep(self):
        """After a sleep, the stream is closed at once, by whichever thread
        gets here first (never the audio callback: PortAudio forbids it)."""
        with self._close_lock:
            if self.journal["state"] != "slept" or self._stopping.is_set():
                return
            self._stopping.set()
        self._close_stream()

    def _device_lost(self):
        # The app is or was just frozen: the computer was asleep, and the audio
        # stopped because of that, not because the microphone went. (The
        # driver may end the stream on waking, even while paused.)
        tick = self._tick()
        if self._frozen(tick) or (self._froze_at is not None
                                  and tick - self._froze_at <= 2 * SILENT_DEVICE_S):
            # (froze_at is set only for a sleep-length freeze, never a stall)
            self._slept(paused_too=True)
            return
        # Windows reports an overrun at the moment a device disappears; that is
        # part of the disconnection, not the computer falling behind.
        with self._lock:
            if self.journal["state"] != "recording":
                return                   # already stopped, slept or lost (checked once)
            self.journal["state"] = "device_lost"
            events = self.journal["events"]
            # The same clock the events use: audio the writer has not saved yet
            # still counts, or a writer that is behind makes an old overrun look
            # like part of the disconnection.
            at_now = self._position_s()
            during = bool(events and events[-1]["kind"] == "overrun"
                          and at_now - events[-1]["at_s"] <= 1.0)
            if during:
                events.pop()
        self._event("device_lost", note="The microphone stopped responding or was "
                                        "disconnected. Recording stopped; it did not "
                                        "switch to another device."
                                        + (" A fraction of a second may be missing at the "
                                           "moment it disconnected." if during else ""))
        self._display_block = None
        self._stop.set()

    def _slept(self, paused_too=False):
        with self._lock:
            if self.journal["state"] != "recording" or (self.paused and not paused_too):
                return
            self.journal["state"] = "slept"
        self._event("sleep", note="The computer went to sleep, for example because the lid "
                                  "was closed. Recording stopped; what was recorded before "
                                  "that is kept.")
        self._display_block = None
        self._stop.set()

    def _close_stream(self):
        if self._computer is not None:
            self._computer.close()
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass
            _stream_opened(-1)

    # -- controls -------------------------------------------------------------------

    def start(self):
        try:
            self.folder.mkdir(parents=True, exist_ok=False)
            self._save_journal()
            self._stream = self._open_stream(self.journal["device"], self.rate,
                                             self._callback, self._finished)
            _stream_opened(+1)
            self._stream.start()
        except Exception as exc:
            self._close_stream()
            shutil.rmtree(self.folder, ignore_errors=True)
            raise RecorderError("The microphone could not be started. Check that it is "
                                "connected and that Windows allows apps to use it "
                                "(Settings > Privacy > Microphone).") from exc
        if self._computer is not None:
            # Both tracks were asked for: if computer audio cannot start, the
            # recording does not start (never silently the microphone only).
            try:
                self._computer.open(self._computer_callback, self._computer_notify)
                self.journal["tracks"][COMPUTER]["device_name"] = self._computer.device_name
                self._save_journal()
            except Exception as exc:
                self._close_stream()
                shutil.rmtree(self.folder, ignore_errors=True)
                raise RecorderError("Computer audio could not be started, so the recording "
                                    "did not start. Turn computer audio off to record the "
                                    "microphone only.") from exc
        self._last_audio = self._alive = self._started_tick = self._tick()
        self._writer = threading.Thread(target=self._write_loop, name="recorder-writer",
                                        daemon=True)
        self._writer.start()
        self._watchdog = threading.Thread(target=self._watch_loop, name="recorder-watchdog",
                                          daemon=True)
        self._watchdog.start()

    def pause(self):
        if not self.paused:
            self.paused = True
            self._display_block = None
            self._event("paused")
            if self.tap is not None:     # live drafts: only while they take audio
                try:
                    self._queue.put_nowait(_PAUSED)
                except queue.Full:
                    pass                 # the draft's phrase then runs on past the pause

    def lid_changed(self, open_):
        """The lid closed or opened while recording (#172). On opening, a
        recording that carried on with the lid closed for LID_NOTE_S or more
        says so, with how long: its marks could not be seen meanwhile."""
        tick = self._tick()
        if not open_:
            if self._lid_closed_at is None:
                self._lid_closed_at = tick
            return
        closed, self._lid_closed_at = self._lid_closed_at, None
        if closed is not None and self.journal["state"] == "recording" \
                and tick - closed >= LID_NOTE_S:
            self._event("lid", seconds=tick - closed,
                        note="The lid was closed; the recording carried on.")

    def resume(self):
        if self.paused:
            self._last_audio = self._tick()
            self._mic.new_segment()       # the tracks are lined up again from here
            if self._computer is not None:
                self._computer.timing.new_segment()
            self.paused = False
            self._event("resumed")

    def stop(self):
        """Stop capture and save everything queued. Returns the journal."""
        self._stopping.set()          # a deliberate stop: not reported as a lost device
        self._close_stream()          # no more blocks arrive...
        self._stop.set()              # ...then the writer drains the queue and finishes
        self._display_block = self._viz_cache = None    # nothing of the sound kept for display
        if self._writer is not None:
            self._writer.join(60)
            if self._writer.is_alive():
                raise RecorderError("Saving the recording did not finish. It is kept and "
                                    "will be imported when the app next starts.")
        self._display_block = self._viz_cache = None    # nothing of the sound is kept
        if self.journal["state"] == "recording":
            self.journal["state"] = "stopped"
        if self._computer is not None:
            with self._lock:
                self._computer.on = False
                entry = self.journal["tracks"][COMPUTER]
                if entry["state"] == "recording":
                    entry["state"] = "stopped"
        try:
            self._save_journal()
        except OSError:
            pass
        return self.journal

    @property
    def writing(self):
        return self._writer is not None and self._writer.is_alive()

    @property
    def stopped(self):
        return self._stop.is_set()

    def status(self):
        with self._lock:
            seconds = (self.journal["frames"] + self._buffered) / self.rate
            events = [dict(e) for e in self.journal["events"][-10:]]
        return {"state": "paused" if self.paused and self.journal["state"] == "recording"
                else self.journal["state"], "seconds": round(seconds, 1),
                "level_db": round(self.level_db, 1), "id": self.journal["id"],
                "levels": [list(x) for x in list(self._levels)],
                "viz": self._display(),
                "device": self.journal["device_name"],
                "computer": None if self._computer is None else {
                    "on": self._computer.on,
                    "level_db": round(self._computer.level_db, 1)
                    if self._computer.on and not self.paused else -96.0},
                "events": events}

    def _display(self):
        """Display data for the latest block, worked out when asked for: the page
        asks while the recording is on screen, not for every block."""
        latest = self._display_block           # read once: the callback replaces it whole
        if latest is None:
            return None
        cached = self._viz_cache
        if cached is not None and cached[0] == latest[0]:
            return cached[1]
        try:
            viz = visual(latest[1], self.rate)
        except Exception:                      # the display must never break status
            viz = None
        self._viz_cache = (latest[0], viz)
        return viz


class _TrackTiming:
    """Timing marks for one track, to line the tracks up afterwards: [frame,
    stamp, segment] at a track's first block, at least every MARK_EVERY_S,
    and at the first block after each resume (segment 1: the time jumps
    there). frame is the block's first frame in the saved track; stamp the
    monotonic time of that frame. Marks wait here until the chunk holding
    their frames is saved, so the journal never names unsaved frames."""

    def __init__(self, rate):
        self.rate = int(rate)
        self._segment = True          # the next block starts a segment
        self._last = None             # frame of the latest mark
        self._marks = []

    def new_segment(self):
        self._segment = True

    def take_segment(self):
        """For the capture thread: does the block being queued start a segment?"""
        seg, self._segment = self._segment, False
        return seg

    def note(self, frame, stamp, segment):
        """For the writer thread, as each block is buffered."""
        if segment or self._last is None or frame - self._last >= MARK_EVERY_S * self.rate:
            self._marks.append([int(frame), round(float(stamp), 4), 1 if segment else 0])
            self._last = frame

    def take_marks(self):
        marks, self._marks = self._marks, []
        return marks


class _ComputerTrack:
    """The second track's capture stream, queue and unsaved buffer."""

    def __init__(self, spec, queue_blocks):
        self._open = spec["open"]
        self.source = spec["source"]
        self.rate = int(spec["rate"])
        self.queue = queue.Queue(maxsize=queue_blocks)
        self.buffer, self.buffered = [], 0
        self.timing = _TrackTiming(self.rate)
        self.level_db = -96.0
        self.on = False
        self.stream = None
        self.device_name = None
        self._close_lock = threading.Lock()

    def open(self, callback, notify):
        self.on = True
        try:
            self.stream = self._open(self.source, callback, notify)
            self.stream.start()
        except Exception:
            self.on = False
            self.close()
            raise
        self.device_name = getattr(self.stream, "device_name", None)

    def close(self):
        with self._close_lock:
            s, self.stream = self.stream, None
        if s is not None:
            for step in (s.stop, s.close):
                try:
                    step()
                except Exception:
                    pass


# -- after recording: assemble, recover ---------------------------------------------

JOURNAL_READ_TRIES = 5      # a journal locked for a moment (an antivirus scan, a save)
JOURNAL_READ_WAIT_S = 0.1


def read_journal(folder):
    """The journal, or None if it is missing, damaged or stays locked. A
    PermissionError (Windows: another process has the file open, briefly) is
    tried again a few times (#148); damaged JSON is not."""
    path = Path(folder) / "journal.json"
    for attempt in range(JOURNAL_READ_TRIES):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt + 1 < JOURNAL_READ_TRIES:
                time.sleep(JOURNAL_READ_WAIT_S)
        except (OSError, ValueError):
            return None
    return None


def journal_format(journal):
    """1 for a journal with no format field (the microphone only); 0 for a
    format field that is not a whole number (never imported)."""
    value = journal.get("format", 1)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def track_files(folder, chunks, prefix):
    """[(file name, frames)] for a track: the chunks the journal lists, then
    chunk files renamed into place (so fully written) but not yet listed.
    Temporary files are not included."""
    folder = Path(folder)
    listed = [(c["file"], int(c["frames"])) for c in chunks]
    names = {name for name, _ in listed}
    extra = sorted(p.name for p in folder.glob(f"{prefix}*.pcm") if p.name not in names)
    return listed + [(name, (folder / name).stat().st_size // 2) for name in extra]


def assemble_wav(folder, journal, notes=None):
    """Join the saved chunks into recording.wav. Chunk files that were renamed
    into place (so fully written) but not yet listed in the journal are
    included; temporary files are not. Returns (path, seconds). With a
    computer track (format 2), recording.wav is the ONE mix of both tracks
    that is transcribed (workspace/mixdown.py); events about the mix are
    appended to notes."""
    folder = Path(folder)
    if journal_format(journal) == 2 and COMPUTER in (journal.get("tracks") or {}):
        from workspace import mixdown
        return mixdown.write_mix(folder, journal, folder / "recording.wav",
                                 notes if notes is not None else [])
    listed = [c["file"] for c in journal.get("chunks", [])]
    extra = sorted(p.name for p in folder.glob("chunk-*.pcm") if p.name not in listed)
    out = folder / "recording.wav"
    frames = 0
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(journal["rate"]))
        for name in listed + extra:
            data = (folder / name).read_bytes()
            data = data[: len(data) - len(data) % 2]
            w.writeframes(data)
            frames += len(data) // 2
    return out, frames / int(journal["rate"])


def unfinished(root):
    """Recording folders left by a previous run (crash, power loss, or an import
    that failed): [(folder, journal)]."""
    base = Path(root)
    if not base.is_dir():
        return []
    return [(d, read_journal(d)) for d in sorted(p for p in base.iterdir() if p.is_dir())]


DISCARDED = "discarded"        # marker: the user discarded it; delete, never import


def mark_discarded(folder):
    (Path(folder) / DISCARDED).write_text("Discarded by the user. Delete, do not import.\n",
                                          encoding="utf-8")


def is_discarded(folder):
    return (Path(folder) / DISCARDED).exists()


def remove_folder(folder):
    """Remove a recording folder, journal (and any discard marker) last: if this
    stops part-way, they still mark the folder for the next start."""
    folder = Path(folder)
    for p in folder.iterdir():
        if p.name not in ("journal.json", DISCARDED):
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
    (folder / "journal.json").unlink(missing_ok=True)
    (folder / DISCARDED).unlink(missing_ok=True)
    folder.rmdir()


def missing_tail_seconds(folder, journal, saved_seconds):
    """For an interrupted recording: about how long it ran beyond what was
    saved, from the last heartbeat, leaving out time spent paused."""
    try:
        last = float((Path(folder) / "heartbeat").read_text(encoding="ascii"))
    except (OSError, ValueError):
        last = journal.get("last_seen") or journal.get("started_at")
    paused, since = 0.0, None
    for e in journal.get("events", []):
        if e["kind"] == "paused":
            since = e.get("t")
        elif e["kind"] == "resumed" and since is not None:
            paused += max(0.0, e.get("t", since) - since)
            since = None
    if since is not None:                 # still paused when it stopped
        paused += max(0.0, last - since)
    lost = sum(e.get("seconds") or 0 for e in journal.get("events", [])
               if e["kind"] in ("dropped",))
    ran = max(0.0, last - journal["started_at"] - paused)
    return max(0.0, ran - saved_seconds - lost)


def new_folder(root):
    return Path(root) / uuid.uuid4().hex
