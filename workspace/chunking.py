# workspace/chunking.py
# Plan chunk boundaries and decide which chunk owns each segment (#14).
#
# Owned ranges tile the recording exactly. Each boundary sits at the quietest
# 30 ms window within +/- SEARCH_S of its nominal position, so it normally
# falls in a pause. The engine decodes the owned range plus CONTEXT_S each
# side; a segment is kept only by the chunk whose owned range contains its
# start time - at word level, so a sentence crossing a boundary is divided
# between the two chunks rather than lost. Text is never deduplicated by
# content (repeated phrases are real speech); ownership is purely by time.

import numpy as np

from asr_engines import Segment, join_tokens

SAMPLE_RATE = 16000
TARGET_S = 45.0
SEARCH_S = 5.0
CONTEXT_S = 2.0
WINDOW_S = 0.03


def quietest_point(reader, lo, hi, sr=SAMPLE_RATE):
    """Sample index at the centre of the lowest-energy window in [lo, hi)."""
    win = max(1, int(WINDOW_S * sr))
    audio = reader.read(lo, hi)
    n = len(audio) // win
    if n == 0:
        return (lo + hi) // 2
    energy = (audio[:n * win].reshape(n, win) ** 2).mean(axis=1)
    return lo + int(np.argmin(energy)) * win + win // 2


def plan_chunks(reader, total, sr=SAMPLE_RATE, target_s=TARGET_S, search_s=SEARCH_S,
                context_s=CONTEXT_S):
    """List of (start, end, ctx_start, ctx_end) sample ranges covering [0, total)."""
    target, search, ctx = int(target_s * sr), int(search_s * sr), int(context_s * sr)
    bounds = [0]
    nominal = target
    while nominal + search < total - target // 4:      # avoid a tiny last chunk
        lo = max(bounds[-1] + target // 2, nominal - search)
        cut = quietest_point(reader, lo, nominal + search, sr)
        bounds.append(cut)
        nominal = cut + target
    bounds.append(total)
    return [(a, b, max(0, a - ctx), min(total, b + ctx))
            for a, b in zip(bounds[:-1], bounds[1:], strict=True)]


class MissingWordTimes(ValueError):
    """An engine returned a long segment without word times, so a sentence
    crossing a chunk boundary could not be divided safely."""


def owned(segments, start, end, total, sr=SAMPLE_RATE):
    """The part of each segment (absolute seconds) that this chunk owns.

    Ownership is by WORD start time: real engines return sentence-length
    segments, and a sentence can run past the chunk's context, so owning
    whole segments by their start would drop sentence tails. Each word belongs
    to the chunk whose owned range contains its start (the first chunk also
    owns anything before 0, the last anything after the end). A segment that
    crosses a boundary is split there; each side keeps only its own words.

    Segments without word times are owned by their start only if they are
    short enough (<= CONTEXT_S) that the context guarantees they are whole."""
    lo = -float("inf") if start == 0 else start / sr
    hi = float("inf") if end >= total else end / sr
    out = []
    for s in segments:
        words = getattr(s, "words", ()) or ()
        if not words:
            if s.end - s.start > CONTEXT_S and lo <= s.start < hi:
                raise MissingWordTimes("Engine segment without word times is too long "
                                       "to place safely at a chunk boundary.")
            if lo <= s.start < hi:
                out.append(s)
            continue
        mine = tuple(w for w in words if lo <= w[0] < hi)
        if not mine:
            continue
        if len(mine) == len(words):
            out.append(s)
        else:
            out.append(Segment(mine[0][0], mine[-1][1], join_tokens([w[2] for w in mine]), mine))
    return out
