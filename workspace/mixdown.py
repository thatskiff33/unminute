# workspace/mixdown.py
# The one mix of a two-track recording (#108): microphone and computer audio
# become one mono 16-bit stream at the microphone's rate, which is streamed
# to the sandboxed decoder like any recording and transcribed once (the
# owner's decision 10). The decoder still makes the only 16 kHz conversion
# (I2). The mix is made in memory, block by block, from the encrypted chunks
# (workspace/recorder.TrackAudio, #111 C3b): nothing plain is written.
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


def computer_at(times, lines, reader):
    """The computer track's samples at these monotonic times (float32),
    read by linear interpolation; silence where it has none. reader: the
    track (.read(a, b) gives frames [a, b) as float32 sample values)."""
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


class Mix:
    """The one mix of a two-track recording, at the microphone's rate. The
    tracks are lined up when it is made (events about that go to notes at
    once, so they are known before the import); blocks() then mixes and
    yields little-endian int16 bytes, BLOCK_S at a time. mic and comp: the
    tracks (recorder.TrackAudio: .frames, .read(a, b))."""

    def __init__(self, journal, mic, comp, notes):
        from workspace import recorder
        self.mic, self.comp = mic, comp
        self.rate = rate = int(journal["rate"])
        crate = int(journal["tracks"][recorder.COMPUTER]["rate"])
        track = journal["tracks"][recorder.COMPUTER]
        self.frames = mic.frames
        try:
            self.mic_lines, mic_off = segments(journal.get("marks"), mic.frames, rate)
            self.comp_lines, comp_off = (segments(track.get("marks"), comp.frames, crate)
                                         if comp.frames else ([], False))
            if mic_off or comp_off:
                notes.append({"kind": "mix_drift", "at_s": 0.0, "seconds": None,
                              "note": "The clocks of the microphone and the computer audio "
                                      "could not be compared, so a slow drift between them "
                                      "was not corrected."})
        except MixError:
            self.mic_lines = nominal(mic.frames, rate)
            self.comp_lines = nominal(comp.frames, crate)
            notes.append({"kind": "mix_by_start", "at_s": 0.0, "seconds": None,
                          "note": "The microphone and computer audio were lined up by when "
                                  "they started only; they may be slightly out of step."})

    def blocks(self):
        step = max(1, int(BLOCK_S * self.rate))
        for seg in self.mic_lines:
            for a in range(seg.start, seg.end, step):
                b = min(seg.end, a + step)
                voice = self.mic.read(a, b)
                times = seg.time(np.arange(a, b, dtype=np.float64))
                other = computer_at(times, self.comp_lines, self.comp)
                mix = np.clip(np.round((voice + other) / 2.0), -32768, 32767)
                yield mix.astype("<i2").tobytes()
