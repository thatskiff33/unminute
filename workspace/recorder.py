# workspace/recorder.py
# Interruption-safe microphone recording (#30). Off unless switched on in
# Settings. One recording at a time, from one chosen input device.
#
#   - Capture runs in PortAudio's callback (via sounddevice). The callback only
#     copies each block into a bounded queue; a writer thread does all disk work.
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
HEARTBEAT_S = 1.0            # how often the "still recording" time is noted
MAX_HOURS = 8                # a forgotten recording stops itself


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
        out.append({"index": i, "name": str(d["name"])[:120], "default": i == default_in,
                    "rate": int(d.get("default_samplerate") or 44100)})
    return out


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
                 clock=time.time):
        self.folder = Path(folder)
        self.rate = int(rate)
        self._clock = clock
        self._open_stream = open_stream
        self._queue = queue.Queue(maxsize=max(1, int(QUEUE_S / BLOCK_S)))
        self._buffer = []
        self._buffered = 0
        self._lock = threading.Lock()
        self.paused = False
        self.level_db = -96.0
        self._levels = collections.deque(maxlen=50)   # recent [block number, level], for the meter
        self._level_seq = 0                           # blocks measured so far
        self._last_audio = clock()
        self._stop = threading.Event()          # the writer's signal to finish
        self._stopping = threading.Event()      # a deliberate stop (not a lost device)
        self._writer = None
        self._stream = None
        self.journal = {
            "id": self.folder.name, "device": device, "device_name": device_name,
            "rate": self.rate, "started_at": clock(), "state": "recording",
            "chunks": [], "frames": 0, "events": [], "last_seen": clock()}

    # -- capture (PortAudio callback thread: no disk work, no blocking) --------------

    def _callback(self, indata, frames, time_info, status):
        now = self._clock()
        self._last_audio = now
        if status and getattr(status, "input_overflow", False):
            self._event("overrun", note="The computer could not keep up; a short piece of "
                                        "audio was lost here.")
        if self.paused:
            self.level_db = -96.0
            return
        block = np.array(indata, dtype=np.int16).reshape(-1)
        self.level_db = _level_db(block)
        self._level_seq += 1
        self._levels.append((self._level_seq, round(self.level_db, 1)))
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            self._event("dropped", seconds=len(block) / self.rate,
                        note="Saving fell behind; this audio was lost.")

    def _finished(self):
        # PortAudio also calls this when we stop the stream ourselves.
        if not self._stopping.is_set() and not self._stop.is_set():
            self._device_lost()

    # -- journal ---------------------------------------------------------------------

    def _event(self, kind, seconds=None, note=""):
        with self._lock:
            at = (self.journal["frames"] + self._buffered) / self.rate \
                + self._queue.qsize() * BLOCK_S
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
        self._save_journal()

    def _write_loop(self):
        last_beat = 0.0
        try:
            while True:
                try:
                    block = self._queue.get(timeout=0.2)
                    self._buffer.append(block)
                    self._buffered += len(block)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                if self._buffered >= CHUNK_S * self.rate:
                    self._flush_chunk()
                now = self._clock()
                if now - last_beat >= HEARTBEAT_S:
                    last_beat = now
                    self.journal["last_seen"] = now
                    try:                 # only an estimate: never end a recording over it
                        (self.folder / "heartbeat").write_text(str(now), encoding="ascii")
                    except OSError:
                        pass
                    if (not self.paused and not self._stop.is_set()
                            and now - self._last_audio > SILENT_DEVICE_S):
                        self._device_lost()
                    if now - self.journal["started_at"] > MAX_HOURS * 3600:
                        self._event("time_limit", note=f"Stopped after {MAX_HOURS} hours.")
                        self._stop.set()
            self._flush_chunk()
        except OSError as exc:
            # Disk full or unwritable: keep every chunk already saved, stop
            # capture, and say how much could not be saved.
            lost = self._buffered / self.rate + self._queue.qsize() * BLOCK_S
            self._event("disk_full" if getattr(exc, "errno", None) == 28 else "write_failed",
                        seconds=lost, note="The recording could not be saved any further: "
                                           "the disk is full or not writable.")
            self.journal["state"] = "failed"
            self._stopping.set()
            self._stop.set()
            self._close_stream()
            try:
                self._save_journal()
            except OSError:
                pass        # the journal on disk still lists every saved chunk

    def _device_lost(self):
        if self.journal["state"] != "recording":
            return
        # Windows reports an overrun at the moment a device disappears; that is
        # part of the disconnection, not the computer falling behind.
        with self._lock:
            events = self.journal["events"]
            at_now = (self.journal["frames"] + self._buffered) / self.rate
            during = bool(events and events[-1]["kind"] == "overrun"
                          and at_now - events[-1]["at_s"] <= 1.0)
            if during:
                events.pop()
        self._event("device_lost", note="The microphone stopped responding or was "
                                        "disconnected. Recording stopped; it did not "
                                        "switch to another device."
                                        + (" A fraction of a second may be missing at the "
                                           "moment it disconnected." if during else ""))
        self.journal["state"] = "device_lost"
        self._stop.set()

    def _close_stream(self):
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
        self._last_audio = self._clock()
        self._writer = threading.Thread(target=self._write_loop, name="recorder-writer",
                                        daemon=True)
        self._writer.start()

    def pause(self):
        if not self.paused:
            self.paused = True
            self._event("paused")

    def resume(self):
        if self.paused:
            self._last_audio = self._clock()
            self.paused = False
            self._event("resumed")

    def stop(self):
        """Stop capture and save everything queued. Returns the journal."""
        self._stopping.set()          # a deliberate stop: not reported as a lost device
        self._close_stream()          # no more blocks arrive...
        self._stop.set()              # ...then the writer drains the queue and finishes
        if self._writer is not None:
            self._writer.join(60)
            if self._writer.is_alive():
                raise RecorderError("Saving the recording did not finish. It is kept and "
                                    "will be imported when the app next starts.")
        if self.journal["state"] == "recording":
            self.journal["state"] = "stopped"
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
            events = [dict(e) for e in self.journal["events"]]
        return {"state": "paused" if self.paused and self.journal["state"] == "recording"
                else self.journal["state"], "seconds": round(seconds, 1),
                "level_db": round(self.level_db, 1), "id": self.journal["id"],
                "levels": [list(x) for x in list(self._levels)],
                "device": self.journal["device_name"],
                "events": events[-10:]}


# -- after recording: assemble, recover ---------------------------------------------

def read_journal(folder):
    try:
        return json.loads((Path(folder) / "journal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def assemble_wav(folder, journal):
    """Join the saved chunks into recording.wav. Chunk files that were renamed
    into place (so fully written) but not yet listed in the journal are
    included; temporary files are not. Returns (path, seconds)."""
    folder = Path(folder)
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


def remove_folder(folder):
    """Remove a recording folder, journal last: if this stops part-way, the
    journal still marks the folder for the next start."""
    folder = Path(folder)
    for p in folder.iterdir():
        if p.name != "journal.json":
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
    (folder / "journal.json").unlink(missing_ok=True)
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
