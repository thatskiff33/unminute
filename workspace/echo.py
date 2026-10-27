# workspace/echo.py
# Echo cancellation for the one mix of a two-track recording (#376, the
# owner's decisions of 28 Oct 2026: our own frequency-domain canceller in
# numpy, at Stop, inside the mix; tracks only read; automatic for every
# two-track recording). On laptop speakers the microphone also hears the
# computer audio a moment later; this takes that echo off the microphone
# before the two tracks are mixed, using the computer track (already lined up
# and drift-corrected by workspace/mixdown.py) as the reference.
#
#   1. Both signals go through a short-time Fourier transform (frames of
#      2 * HOP_S with a square-root Hann window, half overlapping, so an
#      unchanged spectrum gives back the same samples). Only the bins from
#      LOW_HZ to HIGH_HZ are worked on: the decoder keeps nothing above 8 kHz
#      (it stores 16 kHz audio, workspace/media.SAMPLE_RATE), so the
#      microphone's own rate is kept and no resampling is added here.
#   2. The echo model, per bin: the microphone's echo in frame t is a sum of
#      the reference in TAPS frames around it, from AHEAD frame ahead (for a
#      small error in the line-up) to TAPS - AHEAD - 1 frames behind: about
#      0.25 s of room. Its weights are a least-squares fit, worked out for each
#      block of BLOCK_S from that block's frames together with earlier
#      blocks' (each earlier block counting FORGET times as much as the one
#      after it). Unlike an adaptive filter that learns sample by sample,
#      a least-squares fit is not pulled off course by the person talking over
#      the far side (double-talk): their voice is not correlated with the
#      reference, so it adds a little uncertainty, never a bias. A bin whose
#      reference has had less than MIN_ACTIVE_S of sound (a lone notification)
#      is not fitted, so a fit to a few frames cannot remove the person's
#      voice after it. A fit that explains no more of a bin than chance would
#      (SHRINK) is dropped, and unless at least MIN_SHARE of the fitted bins
#      stand above chance nothing is taken off at all: real echo shows across
#      most bins at once, a chance likeness of two voices in a few.
#   3. The fitted echo is subtracted, then a mild residual-echo suppressor
#      lowers each bin by at most RESIDUAL_FLOOR_DB where what is left is
#      mostly echo (a linear model leaves some behind, for example where
#      laptop speakers distort): gain = 1 - RESIDUAL * echo / what is left.
#   4. A block whose fitted echo is below MIN_ECHO_DB of the microphone's own
#      sound (no echo: a headset, nothing playing) is passed through exactly,
#      sample for sample.
#
# Deterministic (D1): the same tracks give the same samples on a retry, which
# runs on the same computer. The blocks are fixed by frame position from the
# start and worked in order, with numpy's FFT (single precision, plenty for
# 16-bit audio), element-wise arithmetic, small matrix products and one small
# solve per bin; no random numbers, no threads of its own. Memory is bounded:
# a block and its context at a time (a peak of about 60 MB at 48 kHz,
# measured), plus the fit's running sums (about 1 kB per bin). Nothing is
# written: this works on arrays in memory only. numpy only, like
# workspace/mixdown.py, and loaded only when a two-track recording is mixed.

import numpy as np

HOP_S = 0.032                # frame step; frames are twice this long, half overlapping
BLOCK_S = 10.0               # the fit is worked out once a block (a whole number of frames)
TAPS = 8                     # frames of the echo model per bin
AHEAD = 1                    # of which this many look ahead (a line-up error of up to a frame)
FORGET = 0.5                 # an earlier block's weight in the fit, per block passed
LOW_HZ = 60.0                # bins below this (mains hum, the DC offset) are left alone
HIGH_HZ = 8000.0             # and above this: the stored audio is 16 kHz
RIDGE = 1e-3                 # regularisation of the fit, relative to the reference's power
ACTIVE_DB = -70.0            # a reference bin quieter than this (dB of full scale) is silence
MIN_ACTIVE_S = 2.0           # a bin is fitted only after this much reference sound (weighted)
MIN_ECHO_DB = -30.0          # a block with less fitted echo than this is passed through
RESIDUAL = 0.1               # share of the fitted echo's power assumed left after subtraction
RESIDUAL_FLOOR_DB = -15.0    # the suppressor lowers a bin by at most this much
SHRINK = 4.0                 # a bin's fit must explain this many times chance to count
MIN_SHARE = 0.1              # and this share of the fitted bins must, for any echo at all
FULL = 32768.0


class EchoError(RuntimeError):
    """The canceller could not run, or its result failed a check (never
    shown: the mix falls back to the plain mix, with an event)."""


def _window(m):
    """Square-root periodic Hann: its square, half overlapping, sums to one."""
    return np.sqrt(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(m) / m))


class Canceller:
    """Echo cancellation over one whole track, block by block, in order.
    rate: the microphone's rate. Feed run() two readers of the same length,
    in samples on the microphone's time line: mic(a, b) and ref(a, b) give
    frames [a, b) as float arrays, zeros outside the track."""

    def __init__(self, rate):
        rate = int(rate)
        if rate < 4000:
            raise EchoError("rate")
        self.rate = rate
        self.hop = hop = max(1, int(round(HOP_S * rate)))
        self.size = 2 * hop
        self.frames_per_block = max(TAPS, int(round(BLOCK_S * rate / hop)))
        self.block = self.frames_per_block * hop
        self.win = _window(self.size).astype(np.float32)
        freqs = np.fft.rfftfreq(self.size, 1.0 / rate)
        self.bins = np.flatnonzero((freqs >= LOW_HZ) & (freqs <= min(HIGH_HZ, rate / 2)))
        if not len(self.bins):
            raise EchoError("bins")
        k = len(self.bins)
        # A reference bin with less power than white noise at ACTIVE_DB is silence.
        self.floor = (FULL * 10 ** (ACTIVE_DB / 20)) ** 2 * float(np.sum(self.win ** 2))
        self.min_active = MIN_ACTIVE_S / HOP_S
        self.gram = np.zeros((k, TAPS, TAPS), dtype=np.complex128)
        self.cross = np.zeros((k, TAPS), dtype=np.complex128)
        self.power = np.zeros(k, dtype=np.float64)     # the microphone's, per bin
        self.active = np.zeros(k, dtype=np.float64)    # frames of reference sound, per bin
        self.count = 0.0                               # frames, weighted like the sums
        self.min_echo = 10 ** (MIN_ECHO_DB / 10)
        self.floor_gain = 10 ** (RESIDUAL_FLOOR_DB / 20)

    def _spectra(self, x, frames):
        """Frames 0..frames-1 of x (frame j starts at sample j * hop), only
        the bins worked on: complex (bins, frames)."""
        x = x[:(frames + 1) * self.hop].astype(np.float32)
        windows = np.lib.stride_tricks.sliding_window_view(x, self.size)[::self.hop]
        spec = np.fft.rfft(windows * self.win, axis=1)
        return np.ascontiguousarray(spec[:, self.bins[0]:self.bins[-1] + 1].T)

    def _fit(self, lagged, ref_now, y):
        """Add the block's frames to the running sums and solve: the echo
        weights, (bins, TAPS). lagged: (bins, frames, TAPS), the reference
        at each lag for each of the block's frames; ref_now: the reference
        at lag 0, (bins, frames); y: the microphone, (bins, frames)."""
        for total in (self.gram, self.cross, self.power, self.active):
            total *= FORGET
        self.count = self.count * FORGET + y.shape[1]
        conj = np.conj(lagged).transpose(0, 2, 1)
        self.gram += conj @ lagged
        self.cross += (conj @ y[:, :, None])[:, :, 0]
        self.power += np.sum(np.abs(y).astype(np.float64) ** 2, axis=1)
        self.active += np.sum(np.abs(ref_now) ** 2 > self.floor, axis=1)
        weights = np.zeros((len(self.bins), TAPS), dtype=np.complex128)
        fit = self.active >= self.min_active
        if not fit.any():
            return weights
        gram = self.gram[fit]
        load = np.real(np.trace(gram, axis1=1, axis2=2)) / TAPS
        gram = gram + (RIDGE * load + 1e-9)[:, None, None] * np.eye(TAPS)
        cross = self.cross[fit]
        w = np.linalg.solve(gram, cross[:, :, None])[:, :, 0]
        # How far each bin's fit stands above chance: with no echo, a fit of
        # TAPS weights to `count` frames of unrelated sound still explains
        # about TAPS / count of the microphone. A bin explaining no more than
        # SHRINK times that is taken as no echo, and one explaining twice that
        # or more is kept whole. Real echo shows across most bins at once;
        # a chance likeness (two voices' harmonics meeting) in a few, so with
        # fewer than MIN_SHARE of the fitted bins above chance, nothing is
        # taken off (a headset).
        explained = (np.real(np.sum(np.conj(cross) * w, axis=1))
                     / np.maximum(self.power[fit], 1e-12))
        above = explained * self.count / TAPS
        if float(np.mean(above > SHRINK)) < MIN_SHARE:
            return weights
        weights[fit] = w * np.clip(above / SHRINK - 1.0, 0.0, 1.0)[:, None]
        return weights

    def run(self, mic, ref, total):
        """Yield (raw, voice, other) for consecutive blocks of the track's
        total frames, from the start: raw is the microphone as it was, voice
        the microphone with the echo taken off and other the reference, over
        the same frames (float64 sample values). Raises EchoError if a result
        is not a finite number."""
        hop, size = self.hop, self.size
        back = TAPS - AHEAD - 1                      # reference frames needed behind
        for a in range(0, int(total), self.block):
            b = min(int(total), a + self.block)
            hops = -(-(b - a) // hop)                # output hops in this block
            count = hops + 1                         # frames: frame j starts at a + (j - 1) * hop
            start = a - hop
            x = np.asarray(mic(start, start + (count + 1) * hop), dtype=np.float64)
            rstart = start - back * hop
            r = np.asarray(ref(rstart, start + (count + AHEAD + 1) * hop), dtype=np.float64)
            voice = x[hop:hop + (b - a)]
            other = r[(back + 1) * hop:(back + 1) * hop + (b - a)]
            y = self._spectra(x, count)
            rs = self._spectra(r, count + back + AHEAD)
            # lagged[:, j, i]: the reference at frame j - (i - AHEAD) (a view).
            lagged = np.lib.stride_tricks.sliding_window_view(rs, TAPS, axis=1)[:, :count, ::-1]
            # The fit counts each frame once: the block's last frame is the
            # next block's first.
            weights = self._fit(lagged[:, :hops], rs[:, back:back + hops], y[:, :hops])
            echo = np.einsum("kti,ki->kt", lagged, weights.astype(np.complex64))
            e2 = np.abs(echo).astype(np.float64) ** 2
            own = float(np.sum(np.abs(y).astype(np.float64) ** 2))
            if not np.isfinite(e2).all():
                raise EchoError("result")
            if own <= 0 or float(np.sum(e2)) < self.min_echo * own:
                yield voice, voice.copy(), other           # no echo here: exactly as recorded
                continue
            left = y - echo
            gain = np.clip(1.0 - RESIDUAL * e2 / np.maximum(np.abs(left) ** 2, 1e-12),
                           self.floor_gain, 1.0)
            full = np.zeros((count, size // 2 + 1), dtype=np.complex64)
            full[:, self.bins] = (y - gain * left).T       # what is taken off
            taken = np.fft.irfft(full, n=size, axis=1) * self.win
            span = np.zeros((count + 1, hop))              # overlap-added, from a - hop
            span[:-1] += taken[:, :hop]
            span[1:] += taken[:, hop:]
            out = voice - span.reshape(-1)[hop:hop + (b - a)]
            if not np.isfinite(out).all():
                raise EchoError("result")
            yield voice, out, other
