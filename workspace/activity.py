# workspace/activity.py
# The track-activity file of a two-track recording (#108 slice 5): which
# side of a call was speaking when, kept so that, after speaker
# identification, the Name speakers panel can suggest which speaker is the
# person recording ("You"; the owner's decision 5 of 20 Oct 2026). The
# suggestion is only offered: the user confirms it, and nothing is ever
# named or changed without them (#16 rule: text is never touched).
#
#   1. While the mix is made (workspace/mixdown.Mix), the Tracker takes each
#      block of the microphone and of the computer track lined up with it,
#      and keeps the level of each in every 10 ms step (HOP_S) of the
#      meeting's time line (the mix's time is the microphone's, so step i is
#      the meeting's audio from i * 10 ms).
#   2. At the end (flags): a track is active in a step that is more than
#      ACTIVE_OVER_FLOOR_DB above its own noise floor (and above ABS_GATE_DB).
#      On laptop speakers the microphone also hears the other side, a little
#      later and quieter (echo), so a loud microphone alone is not "you". The
#      echo is measured: the delay (0 to MAX_LAG_S) at which the computer
#      track's level best follows the microphone's, and how much quieter the
#      microphone hears it (the median difference while the computer plays).
#      The microphone carries its own voice in a step where it is active and
#      at least ECHO_MARGIN_DB louder than that echo could be (the loudest
#      computer level within LAG_SPREAD steps of the delay, less the
#      difference). On a headset the echo measured is far below the voice,
#      so every active microphone step counts. Levels only: no words.
#   3. The file (FILE, in the meeting's media folder beside audio.uma):
#      every step's two levels (whole dB, int8) and its flags, sealed with
#      AES-256-GCM under a key derived from the meeting's audio key and the
#      file's random id (docs/ENCRYPTION.md, "Track-activity file"). It is
#      part of the meeting's audio: written once, at the import, and removed
#      with the media folder at audio cleanup, discard or a failed import;
#      the audio key's destruction makes any copy unreadable. Never written
#      again once the audio is gone (C1).
#   4. The suggestion (suggest): for each speaker, the share of the steps in
#      their passages where either track is active in which the microphone
#      carried its own voice. Exactly one speaker at or above YOU_SHARE, with
#      at least YOU_MIN_S of own voice, is suggested; none when several are
#      (more than one person in the room) or none is.
#
# Content-free (P3): the levels and flags are never logged, shown or
# reported; only the suggested speaker label reaches the page. numpy only:
# imported when a mix is made or a panel asks (window-process budget).

import math
import os
import secrets
import struct
from pathlib import Path

import numpy as np

HOP_S = 0.01                 # one step of the time line
FLOOR_PERCENTILE = 5         # a track's noise floor: this percentile of its step levels
ACTIVE_OVER_FLOOR_DB = 10.0  # a step this far above the floor is active
ABS_GATE_DB = -65.0          # and never at or below this
MAX_LAG_S = 0.3              # the echo's delay is looked for up to this
LAG_SPREAD = 2               # steps either side of the delay the echo may be in
ECHO_MARGIN_DB = 6.0         # own voice: this much louder than the echo could be
MIN_ECHO_STEPS = 200         # fewer computer-active steps than this: no echo measured
YOU_SHARE = 0.8              # the share of a speaker's active steps that is own voice
YOU_MIN_S = 5.0              # and at least this much own voice
MIN_DB = -127                # the quietest level kept (silence)

OWN, MIC_ON, COMP_ON = 1, 2, 4                       # flag bits
FILE = "activity.sealed"
MAGIC = b"UAC1"
VERSION = 1
FILE_ID_BYTES = 32
_HEAD = struct.Struct("<BII")                        # version, steps, step in ms
MAX_STEPS = 12 * 3600 * 100                          # 12 hours: far past the longest recording
_FULL = 32768.0


class ActivityError(Exception):
    """The file could not be read (never shown: no suggestion is offered)."""


class Tracker:
    """Levels of both tracks in 10 ms steps, fed block by block in time order
    (frames of the microphone's time line, at its rate). Bounded memory: two
    bytes per step (about 0.7 MB an hour) plus the step being filled."""

    def __init__(self, rate, frames):
        self.rate = int(rate)
        self.frames = int(frames)
        n = self.steps_for(self.frames)
        self.mic = np.full(n, MIN_DB, dtype=np.int8)
        self.comp = np.full(n, MIN_DB, dtype=np.int8)
        self.next = 0                                # the next frame expected
        self._carry = None                           # (step, mic sum, comp sum, count)

    def steps_for(self, frames):
        return (int(frames) * 100 + self.rate - 1) // self.rate if self.rate > 0 else 0

    def _put(self, first, sm, sc, count):
        """Steps first, first + 1, ...: mean powers (sums over counts) as
        whole dB of full scale."""
        stop = min(len(self.mic), first + len(count))
        if stop <= first or first < 0:
            return
        k = stop - first
        self.mic[first:stop] = _db(sm[:k], count[:k])
        self.comp[first:stop] = _db(sc[:k], count[:k])

    def add(self, start, mic, comp):
        """Frames [start, start + len(mic)): the microphone's samples and the
        computer track's at the same moments (sample values, full scale
        32768). Blocks come in time order; a jump finishes the open step."""
        n = min(len(mic), len(comp))
        if n <= 0:
            return
        start = int(start)
        first = start * 100 // self.rate
        last = (start + n - 1) * 100 // self.rate
        # Where each later step starts in this block: its first frame is
        # ceil(step * rate / 100).
        later = np.arange(first + 1, last + 1, dtype=np.int64)
        cuts = np.concatenate(([0], (later * self.rate + 99) // 100 - start))
        x = np.asarray(mic[:n], dtype=np.float64)
        y = np.asarray(comp[:n], dtype=np.float64)
        sm = np.add.reduceat(x * x, cuts)
        sc = np.add.reduceat(y * y, cuts)
        cnt = np.diff(np.append(cuts, n)).astype(np.float64)
        if self._carry is not None:
            step, a, b, c = self._carry
            if step == first and start == self.next:
                sm[0] += a
                sc[0] += b
                cnt[0] += c
            else:
                self._put(step, np.array([a]), np.array([b]), np.array([c]))
        self._put(first, sm[:-1], sc[:-1], cnt[:-1])
        self._carry = (last, sm[-1], sc[-1], cnt[-1])
        self.next = start + n

    def finish(self):
        """(microphone levels, computer levels, flags) for every step, or None
        when the blocks did not reach the end of the recording (the mix was
        not read to its end)."""
        if self._carry is not None:
            step, a, b, c = self._carry
            self._put(step, np.array([a]), np.array([b]), np.array([c]))
            self._carry = None
        if self.next < self.frames or not len(self.mic):
            return None
        return self.mic, self.comp, flags(self.mic, self.comp)


def _db(sums, counts):
    """Mean powers (sums of squared sample values over counts) as whole dB
    of full scale, int8, MIN_DB for silence."""
    power = np.asarray(sums, dtype=np.float64) / np.maximum(counts, 1) / (_FULL * _FULL)
    with np.errstate(divide="ignore"):
        db = 10 * np.log10(power)
    db = np.nan_to_num(db, nan=MIN_DB, neginf=MIN_DB)
    return np.clip(np.round(db), MIN_DB, 0).astype(np.int8)


def _active(db):
    db = np.asarray(db, dtype=np.float64)
    floor = float(np.percentile(db, FLOOR_PERCENTILE)) if len(db) else MIN_DB
    return db > max(ABS_GATE_DB, floor + ACTIVE_OVER_FLOOR_DB)


def _shift(x, lag, fill):
    """x delayed by lag steps (x[t - lag] at t), fill where there is none."""
    if lag <= 0:
        return x.copy()
    out = np.full_like(x, fill)
    if lag < len(x):
        out[lag:] = x[:-lag]
    return out


def echo_lag(mic, comp, comp_on):
    """The delay, in steps, at which the computer track's level best
    follows the microphone's (0 to MAX_LAG_S), over the steps the computer
    plays. 0 when it cannot be told."""
    best, best_r = 0, -2.0
    for lag in range(int(round(MAX_LAG_S / HOP_S)) + 1):
        on = _shift(comp_on, lag, False)
        if np.count_nonzero(on) < MIN_ECHO_STEPS:
            continue
        a, b = mic[on], _shift(comp, lag, MIN_DB)[on]
        if np.std(a) == 0 or np.std(b) == 0:
            continue
        r = float(np.corrcoef(a, b)[0, 1])
        if r > best_r:
            best, best_r = lag, r
    return best


def flags(mic_db, comp_db):
    """Per step: OWN (the microphone carries its own voice, not echo of the
    computer track), MIC_ON and COMP_ON (the track is active)."""
    mic = np.asarray(mic_db, dtype=np.float64)
    comp = np.asarray(comp_db, dtype=np.float64)
    mic_on, comp_on = _active(mic), _active(comp)
    echo = np.full(len(mic), -np.inf)
    if np.count_nonzero(comp_on) >= MIN_ECHO_STEPS:
        lag = echo_lag(mic, comp, comp_on)
        on = _shift(comp_on, lag, False)
        loss = float(np.median(mic[on] - _shift(comp, lag, MIN_DB)[on]))
        near = np.full(len(comp), float(MIN_DB))
        for d in range(max(0, lag - LAG_SPREAD), lag + LAG_SPREAD + 1):
            near = np.maximum(near, _shift(comp, d, MIN_DB))
        echo = near + loss
    own = mic_on & (mic > echo + ECHO_MARGIN_DB)
    return (own * OWN | mic_on * MIC_ON | comp_on * COMP_ON).astype(np.uint8)


def suggest(spans, flag):
    """The speaker label to suggest as "You", or None. spans: {label:
    [(start_s, end_s), ...]} (their passages in the meeting's time); flag:
    the per-step flags. Exactly one label whose active steps are at least
    YOU_SHARE own voice, with at least YOU_MIN_S of it."""
    flag = np.asarray(flag, dtype=np.uint8)
    own = np.concatenate(([0], np.cumsum((flag & OWN) > 0)))
    active = np.concatenate(([0], np.cumsum((flag & (MIC_ON | COMP_ON)) > 0)))
    n = len(flag)
    found = []
    for label, passages in spans.items():
        o = a = 0
        for start, end in passages:
            i = min(n, max(0, int(math.floor(start / HOP_S))))
            j = min(n, max(i, int(math.ceil(end / HOP_S))))
            o += int(own[j] - own[i])
            a += int(active[j] - active[i])
        if a and o / a >= YOU_SHARE and o * HOP_S >= YOU_MIN_S:
            found.append(label)
    return found[0] if len(found) == 1 else None


# -- the sealed file ----------------------------------------------------------------------

def _sealer(key, file_id, mid):
    from workspace import vault
    return vault.Sealer(vault.derive(key, "activity", (file_id, mid)), kind="activity")


def context(mid):
    """The context the file's content is sealed under. Pinned by a test."""
    return ("activity", mid)


def encode(mic, comp, flag):
    n = len(flag)
    if not (len(mic) == len(comp) == n) or n > MAX_STEPS:
        raise ValueError("activity")
    return (_HEAD.pack(VERSION, n, int(HOP_S * 1000))
            + np.asarray(mic, dtype=np.int8).tobytes()
            + np.asarray(comp, dtype=np.int8).tobytes()
            + np.asarray(flag, dtype=np.uint8).tobytes())


def decode(data):
    if len(data) < _HEAD.size:
        raise ActivityError("short")
    version, n, hop_ms = _HEAD.unpack_from(data)
    if version != VERSION or hop_ms != int(HOP_S * 1000) or n > MAX_STEPS \
            or len(data) != _HEAD.size + 3 * n:
        raise ActivityError("format")
    body = memoryview(data)[_HEAD.size:]
    mic = np.frombuffer(body[:n], dtype=np.int8)
    comp = np.frombuffer(body[n:2 * n], dtype=np.int8)
    flag = np.frombuffer(body[2 * n:], dtype=np.uint8)
    return mic, comp, flag


def write(folder, key, mid, result):
    """Seal result ((microphone levels, computer levels, flags)) into the
    meeting's folder as FILE: written as a new ".partial" file, flushed and
    renamed. A failure leaves no file."""
    folder = Path(folder)
    tmp = folder / (FILE + ".partial")
    tmp.unlink(missing_ok=True)
    file_id = secrets.token_bytes(FILE_ID_BYTES)
    blob = MAGIC + file_id + _sealer(key, file_id, mid).seal(encode(*result), context(mid))
    ok = False
    try:
        with open(tmp, "xb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, folder / FILE)
        ok = True
    finally:
        if not ok:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass                           # goes with the folder
    return file_id


def read(folder, key, mid):
    """(file id, microphone levels, computer levels, flags) from the
    meeting's FILE. OSError when there is none; ActivityError when it is
    damaged, too large or not this meeting's."""
    from workspace import vault
    path = Path(folder) / FILE
    limit = len(MAGIC) + FILE_ID_BYTES + _HEAD.size + 3 * MAX_STEPS + 64
    with open(path, "rb") as f:
        blob = f.read(limit + 1)
    if len(blob) > limit or blob[:len(MAGIC)] != MAGIC:
        raise ActivityError("format")
    file_id = blob[len(MAGIC):len(MAGIC) + FILE_ID_BYTES]
    if len(file_id) != FILE_ID_BYTES:
        raise ActivityError("short")
    try:
        data = _sealer(key, file_id, mid).unseal(blob[len(MAGIC) + FILE_ID_BYTES:], context(mid))
    except vault.VaultError:
        raise ActivityError("sealed") from None
    return (file_id, *decode(data))
