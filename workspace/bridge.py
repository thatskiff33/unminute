# workspace/bridge.py
# The only Python surface the web UI can call (pywebview js_api).
#
# pywebview exposes every attribute of this object whose name does not start
# with "_" - recursively, including attributes of nested objects. So every
# piece of state lives in an underscore attribute and the public methods are
# the complete, deliberately small API. Each method validates its arguments,
# returns plain data (never paths or objects) and never executes input.

import base64
import io
import math
import wave

import numpy as np

SAMPLE_RATE = 16000
MAX_CLIP_SECONDS = 30.0   # bounded playback: no whole-recording blobs in the page


class BridgeError(ValueError):
    """A refused request. The message is safe to show in the UI."""


def _wav_bytes(samples):
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class Bridge:
    """Narrow UI API. Construct with the decoded audio and transcript the
    backend owns; the UI only ever refers to them by position."""

    def __init__(self, audio=None, paragraphs=None):
        self._audio = np.asarray(audio if audio is not None else np.zeros(0), dtype=np.float32)
        self._paragraphs = list(paragraphs or [])

    def transcript(self, start=0, count=500):
        """A page of transcript paragraphs as plain dicts (text is data)."""
        start, count = self._int(start, 0, 10**7), self._int(count, 1, 2000)
        page = self._paragraphs[start:start + count]
        return {"total": len(self._paragraphs), "start": start,
                "items": [{"t": float(p[0]), "who": str(p[1]), "text": str(p[2])} for p in page]}

    def clip(self, start, end):
        """A bounded audio excerpt as a WAV data URL (no file paths in the page)."""
        start, end = self._num(start), self._num(end)
        if end <= start:
            raise BridgeError("The clip end must be after its start.")
        if end - start > MAX_CLIP_SECONDS:
            raise BridgeError(f"Clips are limited to {MAX_CLIP_SECONDS:.0f} seconds.")
        a, b = int(start * SAMPLE_RATE), int(end * SAMPLE_RATE)
        if a >= len(self._audio):
            raise BridgeError("The clip starts after the end of the recording.")
        data = _wav_bytes(self._audio[a:min(b, len(self._audio))])
        return "data:audio/wav;base64," + base64.b64encode(data).decode("ascii")

    def ping(self):
        return "ok"

    # -- validation helpers (private: not exposed to the page) -------------------

    @staticmethod
    def _num(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BridgeError("Expected a number.")
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise BridgeError("Expected a non-negative number.")
        return value

    @classmethod
    def _int(cls, value, lo, hi):
        value = cls._num(value)
        if value != int(value) or not lo <= value <= hi:
            raise BridgeError("Out of range.")
        return int(value)
