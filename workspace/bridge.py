# workspace/bridge.py
# The only Python surface the web UI can call (pywebview js_api).
#
# Two layers keep that surface exactly as small as intended:
#   1. workspace.shell installs a dispatch guard: pywebview resolves calls
#      from the page with getattr over any dotted name, so without the guard
#      page script could reach "__init__", private attributes and their
#      methods (e.g. numpy's ndarray.tofile -> arbitrary file write). The
#      guard allows only the exact names in Bridge.ALLOWED.
#   2. Defence in depth: the object pywebview sees holds no state at all.
#      Data lives in a closure, and every method returns plain JSON-able data
#      ({"ok": True, ...} or {"ok": False, "error": ...}) instead of raising,
#      because pywebview sends Python stack traces (with file paths) to the
#      page when a method raises.

import base64
import io
import math
import wave

import numpy as np

SAMPLE_RATE = 16000
MAX_CLIP_SECONDS = 30.0   # bounded playback: no whole-recording blobs in the page

# The complete set of names the page may call (enforced by the shell's guard).
BRIDGE_METHODS = frozenset({"transcript", "clip", "ping"})


class BridgeError(ValueError):
    """A refused request. The message is safe to show in the UI."""


def _wav_bytes(samples):
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BridgeError("Expected a number.")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise BridgeError("Expected a non-negative number.")
    return value


def _int(value, lo, hi):
    value = _num(value)
    if value != int(value) or not lo <= value <= hi:
        raise BridgeError("Out of range.")
    return int(value)


def _safe(fn):
    """Run fn; turn any failure into a plain error result (no stack, no paths)."""
    try:
        return {"ok": True, "value": fn()}
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        return {"ok": False, "error": "Something went wrong. Please try again."}


def make_bridge(audio=None, paragraphs=None):
    """Return the js_api facade. It has no attributes; the methods close over
    the data, which therefore cannot be reached by attribute lookup."""
    samples = np.asarray(audio if audio is not None else np.zeros(0), dtype=np.float32)
    paras = list(paragraphs or [])

    # Methods take (self, ...) because pywebview drops the first parameter
    # when it generates the JavaScript stubs; self is never used.
    def transcript(self, start=0, count=500):
        def run():
            s, c = _int(start, 0, 10**7), _int(count, 1, 2000)
            page = paras[s:s + c]
            return {"total": len(paras), "start": s,
                    "items": [{"t": float(p[0]), "who": str(p[1]), "text": str(p[2])}
                              for p in page]}
        return _safe(run)

    def clip(self, start, end):
        def run():
            a_s, b_s = _num(start), _num(end)
            if b_s <= a_s:
                raise BridgeError("The clip end must be after its start.")
            if b_s - a_s > MAX_CLIP_SECONDS:
                raise BridgeError(f"Clips are limited to {MAX_CLIP_SECONDS:.0f} seconds.")
            a, b = int(a_s * SAMPLE_RATE), int(b_s * SAMPLE_RATE)
            if a >= len(samples):
                raise BridgeError("The clip starts after the end of the recording.")
            data = _wav_bytes(samples[a:min(b, len(samples))])
            return "data:audio/wav;base64," + base64.b64encode(data).decode("ascii")
        return _safe(run)

    def ping(self):
        return {"ok": True, "value": "ok"}

    # Facade seen by pywebview: methods only, no instance or class state.
    Bridge = type("Bridge", (), {"__slots__": (), "transcript": transcript,
                                 "clip": clip, "ping": ping})
    return Bridge()
