# workspace/joins.py
# Joined lines (#561, the owner's decision DEC-100): a sentence that the
# speech engine or a chunk boundary split into two stored passages is shown,
# copied and handed off as one line. Display only: the stored passages, their
# edits, speakers and commits never change (D1), and each passage is still
# edited, undone, searched and played on its own.
#
# One rule, used by the bridge's transcript reply (app.transcript, a `joined`
# flag per item) and by the handoff text (handoff._build). A passage joins the
# one before it when all of these hold:
#   - the earlier passage's text as shown (its edit, if any) does not end a
#     sentence: ".", "?" or "!", possibly followed by closing quotes or
#     brackets and spaces (1a: an edit that adds a full stop splits the line);
#   - it starts within JOIN_GAP_S of the earlier one's end (4a);
#   - both have the same final speaker, after corrections and merges, or
#     neither has one (2a: reassigning a passage's speaker splits the line);
#   - neither carries the overlapping-speech mark, so the mark always belongs
#     to a line of its own.
# Pure and standard library only: the window process imports it.

import re

JOIN_GAP_S = 1.0

# Closing quotes and brackets that may follow a sentence's end: ASCII, and the
# typographic closing single and double quotes and the closing guillemet.
_CLOSERS = "\"')]}\u2019\u201d\u00bb"
_TAIL = re.compile("[\\s" + re.escape(_CLOSERS) + "]+$")
_ENDS = (".", "?", "!")


def ends_sentence(text):
    """Whether text ends a sentence: ., ? or !, possibly followed by closing
    quotes or brackets and spaces. Empty text ends none."""
    return _TAIL.sub("", text or "").endswith(_ENDS)


def joins_previous(prev, cur):
    """Whether passage cur is shown joined to passage prev, the one before it
    in transcript order. Each is a dict with "text" (as shown), "start" and
    "end" (seconds), "label" (the final speaker label, or None) and
    "overlap" (bool)."""
    return (not ends_sentence(prev["text"])
            and cur["start"] - prev["end"] <= JOIN_GAP_S
            and (prev["label"] or None) == (cur["label"] or None)
            and not prev["overlap"] and not cur["overlap"])


def lines(pieces):
    """pieces (dicts as joins_previous takes, in transcript order) grouped
    into the lines shown: a list of lists, each in order, every piece once."""
    out = []
    for piece in pieces:
        if out and joins_previous(out[-1][-1], piece):
            out[-1].append(piece)
        else:
            out.append([piece])
    return out
