# workspace/decode_child.py
# The contained decoder process (#111 slice A3; threats T2 to T4).
#
# workspace.media.decode_to_file starts this as a plain child process
# (`python -I -B -c BOOT`, not multiprocessing, so it holds no handle to the
# app), puts it in a Windows job (workspace/jobs.py) and only then gives it the
# recording. Before it touches the file it lowers its own integrity level to
# Low, the level browsers use for web content: from then on Windows stops it
# writing the user's files, opening the app's process or sending messages to
# other windows. It can still read what the user can read; the network guard
# covers its Python code and the runtime firewall rule (B7) the rest.
#
# Protocol, on stdin and stdout only (nothing is ever unpickled):
#   app -> decoder  line 1: JSON list, the sys.path to use (read by BOOT)
#                   line 2: JSON {"src": "<path>"}, sent only after READY
#   decoder -> app  frames of 1 type byte + 4-byte little-endian length + payload:
#                   b"R" ready, payload: 4-byte integrity level (0x1000 = Low)
#                   b"A" audio, payload: float32 little-endian 16 kHz mono samples
#                   b"D" done (empty)
#                   b"E" error, payload: 1 byte, one of the E_ codes below
# The app checks every frame, writes and hashes the audio itself and enforces
# the length and disk limits; this process is never trusted.
#
# Only stdlib modules are imported at the top: numpy and PyAV are loaded after
# the integrity level is lowered and the thread counts are capped.

import struct
import sys

BOOT = ("import sys,json;sys.path[:]=json.loads(sys.stdin.readline());"
        "import workspace.decode_child as d;d.main()")

# Container demuxer names (FFmpeg) for the supported recording types
# (AUDIO_EXTENSIONS in workspace/app.py). Playlists (HLS, DASH), stream
# descriptions (SDP, RTP, RTSP), concat scripts and every other demuxer are
# refused whatever the file claims to be.
DEMUXERS = "wav,mp3,mov,mp4,m4a,matroska,webm,flac,ogg,aac,asf,avi"
OPEN_OPTIONS = {"protocol_whitelist": "file", "format_whitelist": DEMUXERS}
SAMPLE_RATE = 16000
MAX_DECODED_S = 12 * 3600
FRAME_SAMPLES = 1 << 16                  # about 4 s of audio per frame
LOW_INTEGRITY = 0x1000

E_SETUP, E_NO_AUDIO, E_TOO_LONG, E_UNREADABLE, E_MEMORY = 1, 2, 3, 4, 5


def _integrity_level(token):
    import ctypes
    from ctypes import wintypes
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    adv.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    buf = ctypes.create_string_buffer(256)
    size = wintypes.DWORD()
    if not adv.GetTokenInformation(token, 25, buf, 256, ctypes.byref(size)):  # IntegrityLevel
        raise OSError(ctypes.get_last_error(), "integrity level not readable")
    sid = ctypes.c_void_p.from_buffer(buf).value                 # TOKEN_MANDATORY_LABEL.Sid
    count = adv.GetSidSubAuthorityCount(sid)[0]
    return adv.GetSidSubAuthority(sid, count - 1)[0]


def lower_integrity():
    """Lower this process's own integrity level to Low (Windows) and return
    the level now in force; None elsewhere. A process may lower, never raise,
    its own level; afterwards it can no longer reopen its token for writing."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.HANDLE)]
    adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                        wintypes.DWORD]
    adv.GetLengthSid.argtypes = [ctypes.c_void_p]

    class Label(ctypes.Structure):           # TOKEN_MANDATORY_LABEL
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008 | 0x0080,   # QUERY, ADJUST_DEFAULT
                                ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "token not available")
    try:
        sid = ctypes.c_void_p()
        if not adv.ConvertStringSidToSidW("S-1-16-4096", ctypes.byref(sid)):  # Low
            raise OSError(ctypes.get_last_error(), "low integrity SID")
        label = Label(sid, 0x20)                        # SE_GROUP_INTEGRITY
        if not adv.SetTokenInformation(token, 25, ctypes.byref(label),
                                       ctypes.sizeof(label) + adv.GetLengthSid(sid)):
            raise OSError(ctypes.get_last_error(), "integrity level not lowered")
        return _integrity_level(token)
    finally:
        k32.CloseHandle(token)


def decode(src, send, max_samples=MAX_DECODED_S * SAMPLE_RATE):
    """Decode `src` to 16 kHz mono float32 and pass the bytes to send() in
    frames. Raises DecodeFailed with an E_ code."""
    import av
    import numpy as np
    pending, total = bytearray(), 0
    with open(src, "rb") as fh, av.open(fh, options=OPEN_OPTIONS) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise DecodeFailed(E_NO_AUDIO)
        if container.duration and container.duration / 1e6 > MAX_DECODED_S + 60:
            raise DecodeFailed(E_TOO_LONG)
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)

        def take(frames):
            nonlocal total
            for fr in frames:
                data = np.ascontiguousarray(fr.to_ndarray().reshape(-1), dtype="<f4").tobytes()
                total += len(data) // 4
                if total > max_samples:
                    raise DecodeFailed(E_TOO_LONG)
                pending.extend(data)
                while len(pending) >= FRAME_SAMPLES * 4:
                    send(b"A", bytes(pending[:FRAME_SAMPLES * 4]))
                    del pending[:FRAME_SAMPLES * 4]

        for frame in container.decode(stream):
            take(resampler.resample(frame))
        take(resampler.resample(None))
    if pending:
        send(b"A", bytes(pending))


class DecodeFailed(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def main():
    import json
    import os
    # numpy's maths library reserves memory per thread when it loads; one
    # thread is plenty for resampling and keeps the job's memory limit honest.
    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[var] = "1"
    out = sys.stdout.buffer

    def send(kind, payload=b""):
        out.write(kind + struct.pack("<I", len(payload)) + payload)
        out.flush()

    try:
        level = lower_integrity()
        import offline_guard
        offline_guard.disable_telemetry()
        offline_guard.install()
        import av  # noqa: F401  (loaded now, at Low integrity, before the file)
        import numpy  # noqa: F401
    except Exception:
        send(b"E", bytes([E_SETUP]))
        return
    send(b"R", struct.pack("<I", level or 0))
    try:
        src = json.loads(sys.stdin.readline())["src"]
        if not isinstance(src, str):
            raise ValueError
    except Exception:
        send(b"E", bytes([E_SETUP]))
        return
    try:
        decode(src, send)
        send(b"D")
    except DecodeFailed as exc:
        send(b"E", bytes([exc.code]))
    except MemoryError:
        send(b"E", bytes([E_MEMORY]))
    except Exception:                    # FFmpeg errors carry paths: only a code goes back
        send(b"E", bytes([E_UNREADABLE]))
