# workspace/mixdown.py
# The one mix of a two-track recording (#108): microphone and computer audio
# become one mono WAV at the microphone's rate, which is imported through the
# sandboxed decoder like any recording and transcribed once (the owner's
# decision 10). The decoder still makes the only 16 kHz conversion (I2).
#
#   1. Line up the tracks. Each track's timing marks ([frame, monotonic time,
#      segment], workspace/recorder.py) are split into segments at each resume
#      (and at a dropped block), and each segment gets a straight-line fit of
#      time against frame, which corrects the two devices' clock drift.
#   2. For every microphone frame, find the computer track's frame at the same
#      moment and read it by linear interpolation: this resamples the computer
#      track to the microphone's rate. Where the computer track has nothing
#      (paused, stopped by policy, before it started) it is silence.
#   3. The plain mix: the mean of the two tracks, as #220 mixes the channels of
#      an import. An echo-aware mix comes only if measurement shows the plain
#      mix doubles words (the owner's decision 3); that is a later slice.
#
# Lines that cannot be trusted (damaged marks, a drift beyond MAX_DRIFT) fall
# back to lining the tracks up by their start times, with an event: never a
# silent change. Works in blocks of BLOCK_S, so memory stays bounded for an
# 8-hour recording. numpy only.

import math
import wave
from pathlib import Path

import numpy as np

BLOCK_S = 10.0               # microphone seconds mixed at a time
MAX_DRIFT = 0.005            # a fitted clock more than 0.5 % off its nominal rate is not trusted
MIN_FIT_S = 20.0             # a segment shorter than this keeps the nominal rate


class MixError(ValueError):
    """A track's timing cannot be used (never shown: the mix falls back)."""


class Segment:
    """Frames [start, end) of one track, at time t0 + k * (frame - start)."""

    def __init__(self, start, end, t0, k):
        self.start, self.end, self.t0, self.k = int(start), int(end), float(t0), float(k)

    def time(self, frames):
        return self.t0 + self.k * (frames - self.start)

    def frame(self, times):
        return self.start + (times - self.t0) / self.k

    @property
    def t_end(self):
        return self.time(self.end)


def segments(marks, frames, rate):
    """The time line of one track from its marks. Raises MixError for marks
    that are not usable (out of order, beyond the track, not numbers)."""
    rate = float(rate)
    frames = int(frames)
    if rate <= 0 or frames < 0:
        raise MixError("rate")
    clean = []
    for m in marks or []:
        if not (isinstance(m, (list, tuple)) and len(m) == 3):
            raise MixError("mark")
        f, t, seg = m
        if (not isinstance(f, int) or isinstance(f, bool) or not isinstance(t, (int, float))
                or isinstance(t, bool) or not math.isfinite(t) or seg not in (0, 1)):
            raise MixError("mark")
        if not 0 <= f <= frames:
            continue                  # a mark for frames that were never saved
        if clean and (f < clean[-1][0] or t <= clean[-1][1]):
            raise MixError("order")
        clean.append((f, float(t), seg))
    if not clean:
        raise MixError("none")
    groups = [[clean[0]]]
    for mark in clean[1:]:
        if mark[2]:
            groups.append([mark])
        else:
            groups[-1].append(mark)
    out, fallback = [], False
    for i, group in enumerate(groups):
        start = 0 if i == 0 else group[0][0]
        end = groups[i + 1][0][0] if i + 1 < len(groups) else frames
        f = np.array([m[0] for m in group], dtype=np.float64)
        t = np.array([m[1] for m in group], dtype=np.float64)
        k = 1.0 / rate
        if len(group) >= 2 and f[-1] - f[0] >= MIN_FIT_S * rate:
            fitted = np.polyfit(f - f[0], t, 1)[0]
            if abs(fitted * rate - 1.0) <= MAX_DRIFT:
                k = float(fitted)
            else:
                fallback = True
        t0 = float(np.mean(t - k * (f - start)))
        if end > start:
            out.append(Segment(start, end, t0, k))
    for a, b in zip(out, out[1:], strict=False):
        if b.t0 < a.t_end - 1.0:          # a segment that starts before the last ended
            raise MixError("overlap")
    return out, fallback


def nominal(frames, rate, t0=0.0):
    """The fallback time line: the whole track at its nominal rate from t0."""
    return [Segment(0, int(frames), t0, 1.0 / float(rate))] if frames else []


class TrackReader:
    """Reads frames [a, b) of a track from its chunk files, as float32; zeros
    beyond what was saved."""

    def __init__(self, folder, files):
        self.folder = Path(folder)
        self.files = []
        pos = 0
        for name, frames in files:
            self.files.append((name, pos, int(frames)))
            pos += int(frames)
        self.frames = pos

    def read(self, a, b):
        out = np.zeros(max(0, b - a), dtype=np.float32)
        for name, start, n in self.files:
            lo, hi = max(a, start), min(b, start + n)
            if lo >= hi:
                continue
            with open(self.folder / name, "rb") as f:
                f.seek((lo - start) * 2)
                data = np.frombuffer(f.read((hi - lo) * 2), dtype="<i2")
            out[lo - a: lo - a + len(data)] = data.astype(np.float32)
        return out


def computer_at(times, lines, reader):
    """The computer track's samples at these monotonic times (float32),
    read by linear interpolation; silence where it has none."""
    out = np.zeros(len(times), dtype=np.float32)
    if not len(times):
        return out
    lo_t, hi_t = float(times.min()), float(times.max())
    for seg in lines:
        if seg.t_end <= lo_t or seg.t0 > hi_t:
            continue
        g = seg.frame(times)
        inside = (g >= seg.start) & (g <= seg.end - 1)
        if not inside.any():
            continue
        gi = g[inside]
        first = int(math.floor(gi.min()))
        last = min(seg.end - 1, int(math.floor(gi.max())) + 1)
        x = reader.read(first, last + 1)
        idx = gi - first
        i0 = np.floor(idx).astype(np.int64)
        i1 = np.minimum(i0 + 1, len(x) - 1)
        frac = (idx - i0).astype(np.float32)
        out[inside] = x[i0] * (1.0 - frac) + x[i1] * frac
    return out


def write_mix(folder, journal, out, notes):
    """Write the one mix of a two-track recording to out (mono 16-bit WAV
    at the microphone's rate). Returns (path, seconds); events about the
    mix are appended to notes (a fallback is never silent)."""
    from workspace import recorder
    folder, out = Path(folder), Path(out)
    rate = int(journal["rate"])
    track = journal["tracks"][recorder.COMPUTER]
    crate = int(track["rate"])
    mic = TrackReader(folder, recorder.track_files(folder, journal.get("chunks", []), "chunk-"))
    comp = TrackReader(folder, recorder.track_files(folder, track.get("chunks", []),
                                                    recorder.COMPUTER_PREFIX))
    try:
        mic_lines, mic_off = segments(journal.get("marks"), mic.frames, rate)
        comp_lines, comp_off = (segments(track.get("marks"), comp.frames, crate)
                                if comp.frames else ([], False))
        if mic_off or comp_off:
            notes.append({"kind": "mix_drift", "at_s": 0.0, "seconds": None,
                          "note": "The clocks of the microphone and the computer audio could "
                                  "not be compared, so a slow drift between them was not "
                                  "corrected."})
    except MixError:
        mic_lines, comp_lines = nominal(mic.frames, rate), nominal(comp.frames, crate)
        notes.append({"kind": "mix_by_start", "at_s": 0.0, "seconds": None,
                      "note": "The microphone and computer audio were lined up by when they "
                              "started only; they may be slightly out of step."})
    step = max(1, int(BLOCK_S * rate))
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        for seg in mic_lines:
            for a in range(seg.start, seg.end, step):
                b = min(seg.end, a + step)
                voice = mic.read(a, b)
                times = seg.time(np.arange(a, b, dtype=np.float64))
                other = computer_at(times, comp_lines, comp)
                mix = np.clip(np.round((voice + other) / 2.0), -32768, 32767)
                w.writeframes(mix.astype("<i2").tobytes())
    return out, mic.frames / rate
