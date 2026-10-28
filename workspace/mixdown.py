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
#   3. The plain mix: the two tracks loudness-matched, then added at full
#      level (the owner's decision 2, 25 Oct 2026: the mean put each source
#      about 6 dB lower). Matching (an engineering decision after the owner's
#      check of 26 Oct 2026, where the microphone played much quieter than the
#      computer audio): each track's level is measured over its sound only
#      (gated_level_db, as playback normalisation measures), and the quieter
#      track is raised to the louder one's, by at most LEVEL_MAX_GAIN_DB; the
#      louder is never turned down. A track with less than LEVEL_MIN_ACTIVE_S
#      of sound above LEVEL_GATE_DB (silence, hiss, one notification) is left
#      as it is, so it is never blown up. Then a soft limiter (soft_limit)
#      that changes nothing below LIMIT_KNEE of full scale and bends louder
#      sums smoothly under full scale instead of clipping them. Only the mix
#      is matched and limited: the saved tracks are read, never changed
#      (D1).
#   4. Echo cancellation (#376, the owner's decisions of 28 Oct 2026): before
#      it is measured and mixed, the microphone has the computer audio's echo
#      taken off (workspace/echo.py, with the computer track lined up by steps
#      1 and 2 as its reference), automatically whenever there is a computer
#      track. That takes two passes over the tracks, both in memory: the
#      first cancels to check the result and measure the cleaned microphone's
#      level for step 3, the second cancels again, the same way to the sample,
#      as it mixes. If the canceller fails or its result fails a check (a
#      sample that is not a number, or a microphone made louder overall than
#      it was by more than ECHO_MAX_GAIN_DB), the mix is the plain mix of
#      steps 1 to 3, with a mix_echo_skipped event of fixed wording: never a
#      silent change. The one mix is what plays back and what is transcribed.
#   5. While mixing, workspace/activity.Tracker keeps both tracks' levels in
#      10 ms steps (before matching), for the track-activity file and the
#      "You" suggestion (#108 slice 5). It is fed the microphone as recorded,
#      before echo cancellation, because it measures the echo itself
#      (activity.flags). A failure there never stops the mix.
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
LIMIT_KNEE = 0.8             # the limiter leaves samples below this share of full scale alone
LEVEL_MAX_GAIN_DB = 15.0     # the quieter track is raised at most this much
LEVEL_GATE_DB = -50.0        # level windows quieter than this are not counted as sound
LEVEL_MIN_ACTIVE_S = 5.0     # a track with less sound than this is not measured (left as it is)
LEVEL_WINDOW_S = 0.256       # level windows
LEVEL_BLOCK_S = 10.0         # the level is measured over the whole track, this much at a time
ECHO_MAX_GAIN_DB = 0.5       # cancelling may not make the microphone louder than this
ECHO_SKIPPED = {"kind": "mix_echo_skipped", "at_s": 0.0, "seconds": None,
                "note": "Echo from the computer audio could not be taken off the "
                        "microphone, so the recording is the plain mix and may have an echo."}
_RELATIVE_GATE_DB = -10.0    # then windows this far below the mean of the rest are left out


def gated_level_db(powers, gate_db):
    """The level, in dB of full scale, of mean-square window powers (each
    already divided by full scale squared), gated like EBU R128 without its
    frequency weighting: windows below gate_db are left out, then those more
    than 10 dB below the mean of the rest. None when no window is left."""
    p = np.asarray(powers, dtype=np.float64)
    p = p[p > 10 ** (gate_db / 10)]
    if not len(p):
        return None
    p = p[p >= 10 ** (_RELATIVE_GATE_DB / 10) * np.mean(p)]
    return float(10 * np.log10(np.mean(p)))


def track_level_db(track, rate, full=32768.0):
    """A track's level over its sound (gated_level_db at LEVEL_GATE_DB);
    None when it has less than LEVEL_MIN_ACTIVE_S of sound. Every window of
    the whole track is measured, whatever its length, read LEVEL_BLOCK_S at
    a time (a whole number of windows) so memory stays bounded: no sample, so
    one notification can never pass for a call, nor sparse sound be missed
    (the af9ab66 review, F1). Only a final part shorter than a window is left
    out. track: .frames, .read(a, b) (sample values, full scale `full`)."""
    rate, frames = int(rate), int(track.frames)
    window = max(1, int(LEVEL_WINDOW_S * rate))
    step = max(window, int(LEVEL_BLOCK_S * rate) // window * window)
    powers = []
    for a in range(0, frames, step):
        x = np.asarray(track.read(a, min(frames, a + step)), dtype=np.float64) / full
        n = len(x) - len(x) % window
        if n:
            w = x[:n].reshape(-1, window)
            powers.append(np.mean(w * w, axis=1))
    if not powers:
        return None
    p = np.concatenate(powers)
    if np.count_nonzero(p > 10 ** (LEVEL_GATE_DB / 10)) * window / rate < LEVEL_MIN_ACTIVE_S:
        return None
    return gated_level_db(p, LEVEL_GATE_DB)


def match_gains(mic_db, comp_db):
    """(microphone gain, computer gain), linear: the quieter track raised to
    the louder one's level, by at most LEVEL_MAX_GAIN_DB; (1, 1) when either
    level is unknown."""
    if mic_db is None or comp_db is None:
        return 1.0, 1.0
    raise_db = min(LEVEL_MAX_GAIN_DB, abs(mic_db - comp_db))
    gain = float(10 ** (raise_db / 20))
    return (gain, 1.0) if mic_db < comp_db else (1.0, gain)


def soft_limit(x, full=32767.0):
    """Samples (float array) through a soft limiter: unchanged up to
    LIMIT_KNEE * full; above it, bent smoothly (tanh, no jump in level or
    slope at the knee) so no sample is ever past full scale (a sample driven
    far over comes out at full scale exactly). The same both ways round
    and in order: a louder sample is never made quieter than a softer one.
    A float array of the same shape is returned."""
    x = np.asarray(x, dtype=np.float64)
    knee = LIMIT_KNEE * full
    room = full - knee
    mag = np.abs(x)
    over = mag > knee
    if not over.any():
        return x
    out = x.copy()
    out[over] = np.sign(x[over]) * (knee + room * np.tanh((mag[over] - knee) / room))
    return out


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


class _Stream:
    """Consecutive blocks from a generator, read as a track: each read(a, b)
    must follow on from the last (track_level_db reads that way)."""

    def __init__(self, blocks, frames):
        self._blocks, self.frames = blocks, int(frames)
        self._held, self.pos = np.zeros(0), 0

    def read(self, a, b):
        if a != self.pos:
            raise ValueError("out of order")
        while len(self._held) < b - a:
            try:
                self._held = np.concatenate([self._held, next(self._blocks)])
            except StopIteration:
                break
        out, self._held = self._held[:b - a], self._held[b - a:]
        self.pos = b
        return out

    def drain(self):
        """Read what is left (the part shorter than a level window)."""
        for _ in self._blocks:
            pass


class Mix:
    """The one mix of a two-track recording, at the microphone's rate. The
    tracks are lined up when it is made (events about that go to notes at
    once, so they are known before the import), and the echo canceller's
    first pass runs (its fallback event, if any, goes to notes too); blocks()
    then mixes and yields little-endian int16 bytes, a block at a time. mic
    and comp: the tracks (recorder.TrackAudio: .frames, .read(a, b)).
    echo_skipped: None, or why the plain mix was used ("check": the result
    failed a check; "error": the canceller failed)."""

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
        # Echo cancellation's first pass (see the header), which measures the
        # cleaned microphone's level. A chunk that cannot be read is not a
        # canceller failure: it stops the import as it would at the mix.
        self.echo, self.echo_skipped = bool(comp.frames and mic.frames), None
        mic_db = None
        if self.echo:
            from workspace import echo, media
            try:
                mic_db = self._first_pass()
            except Exception as exc:
                if isinstance(exc, OSError) or media.damaged_audio(exc):
                    raise
                self.echo = False
                self.echo_skipped = "check" if isinstance(exc, echo.EchoError) else "error"
                notes.append(dict(ECHO_SKIPPED))
        # Loudness matching (see the header). A track that cannot be read
        # here is not matched; reading it for the mix reports the problem.
        try:
            if not self.echo:
                mic_db = track_level_db(mic, rate)
            self.gains = match_gains(mic_db,
                                     track_level_db(comp, crate) if comp.frames else None)
        except Exception:
            self.gains = (1.0, 1.0)
        # The track-activity levels (header, step 5): a tracker that cannot
        # be made (memory for a very long recording) only means no "You"
        # suggestion, never a failed mix (#461).
        try:
            from workspace import activity      # numpy only, like this module
            self.tracker = activity.Tracker(rate, mic.frames)
        except Exception:
            self.tracker = None

    def activity(self):
        """The track-activity result (activity.Tracker.finish) once blocks()
        has been read to the end; None before then or if tracking failed."""
        try:
            return self.tracker.finish() if self.tracker is not None else None
        except Exception:
            return None

    def _track(self, a, voice, other):
        if self.tracker is None:
            return
        try:
            self.tracker.add(a, voice, other)
        except Exception:
            self.tracker = None                  # no suggestion; the mix carries on

    def _voice(self, a, b):
        """The microphone at frames [a, b) (float32), zeros outside the track."""
        out = np.zeros(max(0, b - a), dtype=np.float32)
        lo, hi = max(0, a), min(self.frames, b)
        if lo < hi:
            out[lo - a:hi - a] = self.mic.read(lo, hi)
        return out

    def _other(self, a, b):
        """The computer track at microphone frames [a, b) (float32), lined up
        by the microphone's segments; zeros outside the microphone track and
        where the computer track has nothing."""
        out = np.zeros(max(0, b - a), dtype=np.float32)
        for seg in self.mic_lines:
            lo, hi = max(a, seg.start), min(b, seg.end)
            if lo < hi:
                times = seg.time(np.arange(lo, hi, dtype=np.float64))
                out[lo - a:hi - a] = computer_at(times, self.comp_lines, self.comp)
        return out

    def _cancelled(self):
        """(raw, voice, other) blocks over the whole track: the microphone
        as recorded and with the echo taken off (workspace/echo.py), and the
        computer track lined up; a new canceller each time, so both
        passes give the same samples."""
        from workspace import echo
        return echo.Canceller(self.rate).run(self._voice, self._other, self.frames)

    def _first_pass(self):
        """Cancel once over the whole track: check the result and return the
        cleaned microphone's level (track_level_db). Raises echo.EchoError
        when the result fails the check."""
        from workspace import echo
        energy = {"before": 0.0, "after": 0.0}

        def voices():
            for raw, voice, _ in self._cancelled():
                energy["before"] += float(np.dot(raw, raw))
                energy["after"] += float(np.dot(voice, voice))
                yield voice

        stream = _Stream(voices(), self.frames)
        level = track_level_db(stream, self.rate)
        stream.drain()
        before, after = energy["before"], energy["after"]
        if not (math.isfinite(before) and math.isfinite(after)) \
                or after > before * 10 ** (ECHO_MAX_GAIN_DB / 10):
            raise echo.EchoError("check")
        return level

    def blocks(self):
        g_mic, g_comp = self.gains
        if self.echo:
            parts = self._cancelled()
        else:
            step = max(1, int(BLOCK_S * self.rate))

            def plain():
                for a in range(0, self.frames, step):
                    b = min(self.frames, a + step)
                    voice = self._voice(a, b)
                    yield voice, voice, self._other(a, b)
            parts = plain()
        a = 0
        for raw, voice, other in parts:
            # The tracker takes the microphone as recorded (header, step 5).
            self._track(a, raw, other)
            a += len(raw)
            both = voice.astype(np.float64) * g_mic + other.astype(np.float64) * g_comp
            mix = np.clip(np.round(soft_limit(both)), -32768, 32767)
            yield mix.astype("<i2").tobytes()
