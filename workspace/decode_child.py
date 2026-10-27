# workspace/decode_child.py
# The contained decoder process (#111 slice A3; threats T2 to T4).
#
# workspace.media.decode_to_file starts this (`python -I -B -c BOOT`, not
# multiprocessing, so it holds no handle to the app) in a Windows AppContainer
# with no capabilities (workspace/appcontainer.py, #299), suspended; puts it in
# a Windows job (workspace/jobs.py); checks its token from outside; and only
# then lets it run. In the container Windows gives it no access to the user's
# files, the workspace or its key, and no network, loopback included: it reads
# the runtime and the app code (readable by ALL APPLICATION PACKAGES), the
# recording through one read-only handle the app opened for it, and its pipes.
# It runs at Low integrity (the container's level; lower_integrity() confirms
# it), so it cannot open the app's process or send messages to other windows,
# and the job stops it starting processes (docs/SECURITY_MODEL.md T2, T3).
#
# Protocol, on stdin and stdout only (nothing is ever unpickled):
#   app -> decoder  line 1: JSON list, the sys.path to use (read by BOOT)
#                   line 2, sent only after READY, one of:
#                     JSON {"handle": N}: decode the recording behind N, a
#                     read-only handle the app opened and this process
#                     inherited (the container cannot open it by its path);
#                     JSON {"stdin": "wav", "bytes": N} (#111 C3b): a microphone
#                     recording follows on stdin, a WAV stream of N bytes,
#                     read with the same "wav" demuxer and limits. No plain
#                     copy of the recording is written for it.
#                   Line 2 is read from the binary stream (sys.stdin.buffer):
#                   the text wrapper reads ahead and would take audio bytes.
#                   Line 1 is safe in text: nothing else is sent before READY.
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
# Decoder names (FFmpeg) for the audio codecs those types carry. Opening a
# file probes every stream, so without this list a video track, cover art in
# an MP3 or FLAC, or a subtitle track reached FFmpeg's video, image or
# subtitle decoders before the app chose the audio (D2 red team, part 2,
# finding 1). FFmpeg refuses any other decoder while probing, and decode()
# refuses an audio stream whose decoder is not listed. Parsers still run.
# The phone and VoIP speech decoders (G.729, G.723.1, GSM, iLBC, Speex) are
# listed by the owner's decision of 22 October 2026 (#302); TrueSpeech and
# QDM2 stay refused.
AUDIO_DECODERS = (
    "pcm_s16le,pcm_s16be,pcm_s24le,pcm_s24be,pcm_s32le,pcm_s32be,pcm_f32le,pcm_f32be,"
    "pcm_f64le,pcm_f64be,pcm_u8,pcm_s8,pcm_alaw,pcm_mulaw,adpcm_ima_wav,adpcm_ms,adpcm_ima_qt,"
    "gsm_ms,mp3float,mp3,mp2float,mp2,aac,aac_fixed,alac,flac,opus,libopus,vorbis,"
    "wmav1,wmav2,wmapro,wmavoice,wmalossless,ac3,ac3_fixed,eac3,amrnb,amrwb,"
    "g729,g723_1,gsm,ilbc,speex")
OPEN_OPTIONS = {"protocol_whitelist": "file", "format_whitelist": DEMUXERS,
                "codec_whitelist": AUDIO_DECODERS}
SAMPLE_RATE = 16000
MAX_DECODED_S = 12 * 3600
# Input sample rates outside this range are refused: a header that claims
# 1 Hz turned each stored sample into 16,000 output samples (finding 3).
MIN_INPUT_RATE, MAX_INPUT_RATE = 4000, 384000
# At most one second of decoded audio per 16 bytes of file (128 bit/s on
# average). No supported codec stores speech below about 6 kbit/s, and a
# whole file of digital silence measured at least 64 bytes a second (AAC at
# 8 kHz), so a real recording stays far inside it; a small crafted file can
# no longer claim hours (finding 3). The app enforces it too.
BYTES_PER_DECODED_S = 16
FRAME_SAMPLES = 1 << 16                  # about 4 s of audio per frame
MIX_SAMPLES = 1 << 14                    # samples per channel mixed at a time
LOW_INTEGRITY = 0x1000

E_SETUP, E_NO_AUDIO, E_TOO_LONG, E_UNREADABLE, E_MEMORY, E_TOO_DENSE = 1, 2, 3, 4, 5, 6


def max_samples_for(size, rate=SAMPLE_RATE):
    """The most output samples a file of `size` bytes may decode to: the
    12-hour cap or BYTES_PER_DECODED_S, whichever is lower."""
    return min(MAX_DECODED_S, max(0, int(size)) // BYTES_PER_DECODED_S) * rate


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


def process_integrity(pid):
    """The integrity level of another process (Windows), read by the app so it
    does not rely on the decoder's own report."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.HANDLE)]
    process = k32.OpenProcess(0x1000, False, int(pid))         # QUERY_LIMITED_INFORMATION
    if not process:
        raise OSError(ctypes.get_last_error(), "process not readable")
    try:
        token = wintypes.HANDLE()
        if not adv.OpenProcessToken(process, 0x0008, ctypes.byref(token)):   # TOKEN_QUERY
            raise OSError(ctypes.get_last_error(), "token not readable")
        try:
            return _integrity_level(token)
        finally:
            k32.CloseHandle(token)
    finally:
        k32.CloseHandle(process)


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


MAX_REQUEST_BYTES = 1 << 16             # line 2 (a handle, or the stream's size)
MIN_WAV_BYTES = 44                        # a WAV header with no audio
MAX_HANDLE = 0xFFFFFFFF                   # Windows handles fit in 32 bits


def read_request(stream):
    """Line 2 from the binary stdin: ("handle", n) or ("stdin", size).
    Raises ValueError for anything else."""
    import json
    line = stream.readline(MAX_REQUEST_BYTES)
    if not line.endswith(b"\n"):
        raise ValueError("request")
    req = json.loads(line)
    if not isinstance(req, dict):
        raise ValueError("request")
    handle = req.get("handle")
    if (set(req) == {"handle"} and isinstance(handle, int) and not isinstance(handle, bool)
            and 0 < handle <= MAX_HANDLE):
        return "handle", handle
    size = req.get("bytes")
    if (set(req) == {"stdin", "bytes"} and req["stdin"] == "wav" and isinstance(size, int)
            and not isinstance(size, bool) and MIN_WAV_BYTES <= size <= 0xFFFFFFFF + 8):
        return "stdin", size
    raise ValueError("request")


def open_handle(handle):
    """The inherited read-only handle `handle` as a binary file (Windows: an
    OS handle; elsewhere, for the Linux test job, a file descriptor)."""
    import os
    if sys.platform == "win32":
        import msvcrt
        return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), "rb")
    return os.fdopen(handle, "rb")


def decode(src, send, max_samples=MAX_DECODED_S * SAMPLE_RATE, size=None):
    """Decode `src` (a path or an open binary file, or with size a binary
    stream holding a WAV of size bytes: the recorder's stdin form) to 16 kHz
    mono float32 and pass the bytes to send() in frames. Raises DecodeFailed
    with an E_ code.

    Channels are mixed with equal weights (the arithmetic mean, #220), not
    by FFmpeg's surround matrix. That matrix treats channels as speaker
    positions, guessing a layout when a file declares none: it drops a
    low-frequency channel and halves or boosts others, so a recorder that
    puts one person on each track could lose a person, and a mix of similar
    channels could exceed full scale. A mean keeps every track and adds no
    gain: before resampling it never exceeds the loudest channel. The 16 kHz
    resampler that follows can still overshoot full scale slightly on
    full-scale input, exactly as it does for a mono file."""
    import os
    if size is None:
        if hasattr(src, "read"):
            # The file's own size bounds how much audio it may decode to.
            return _decode(src, os.fstat(src.fileno()).st_size, None, send, max_samples)
        with open(src, "rb") as fh:
            return _decode(fh, os.fstat(fh.fileno()).st_size, None, send, max_samples)
    # A stream is not seekable and carries no name: the demuxer is named
    # (wav, already on the list), never guessed from the data.
    return _decode(_Stream(src), size, "wav", send, max_samples)


class _Stream:
    """A pipe as FFmpeg should see it: read only, never seekable. Windows
    reports a pipe as seekable (the C runtime's lseek does not fail on one),
    and PyAV would then seek it and fail."""

    def __init__(self, raw):
        self._raw = raw

    def read(self, n=-1):
        return self._raw.read(n)

    def seekable(self):
        return False


def _decode(fh, size, fmt, send, max_samples):
    from fractions import Fraction

    import av
    import numpy as np
    pending, total = bytearray(), 0
    with av.open(fh, format=fmt, options=OPEN_OPTIONS) as container:
        dense_limit = max_samples_for(size)
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise DecodeFailed(E_NO_AUDIO)
        # No decoder for the stream (an unknown format tag, or 0 bits per
        # sample): PyAV 17 leaves codec_context as None, and decoding it
        # crashed the process (finding 4). A decoder outside the list is
        # refused here too, since the probe's list does not bind PyAV.
        codec = stream.codec_context
        if codec is None or codec.name not in AUDIO_DECODERS.split(","):
            raise DecodeFailed(E_UNREADABLE)
        if codec.sample_rate and not MIN_INPUT_RATE <= codec.sample_rate <= MAX_INPUT_RATE:
            raise DecodeFailed(E_UNREADABLE)
        if container.duration and container.duration / 1e6 > MAX_DECODED_S + 60:
            raise DecodeFailed(E_TOO_LONG)
        # A mono frame goes straight to the 16 kHz resampler, as before. Any
        # other is first converted to interleaved float at its own layout and
        # rate, in blocks of MIX_SAMPLES (no channel mixing), and its mean is
        # resampled. Interleaved, not planar: PyAV 17 miscounts the planes of
        # an 8-channel planar frame and reads past them (found in #220). Each
        # resampler takes its input settings from the first frame it gets and
        # refuses a frame that differs, as the single resampler did before.
        interleaved = av.AudioResampler(format="flt", frame_size=MIX_SAMPLES)
        to_rate = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
        mixed = 0                                   # samples mixed so far, for timestamps

        def mix(frames):
            nonlocal mixed
            out = []
            for fr in frames:
                block = fr.to_ndarray().reshape(-1, len(fr.layout.channels))
                # Summed in float64 and divided once, so N identical channels
                # give back exactly that channel.
                acc = block[:, 0].astype(np.float64)
                for c in range(1, block.shape[1]):
                    acc += block[:, c]
                mean = (acc / block.shape[1]).astype(np.float32)
                mono = av.AudioFrame.from_ndarray(mean.reshape(1, -1), format="flt",
                                                  layout="mono")
                mono.sample_rate = fr.sample_rate
                mono.time_base = Fraction(1, fr.sample_rate)
                mono.pts = mixed
                mixed += mean.shape[0]
                out += to_rate.resample(mono)
            return out

        def take(frames):
            nonlocal total
            for fr in frames:
                data = np.ascontiguousarray(fr.to_ndarray().reshape(-1), dtype="<f4").tobytes()
                total += len(data) // 4
                if total > max_samples:
                    raise DecodeFailed(E_TOO_LONG)
                if total > dense_limit:
                    raise DecodeFailed(E_TOO_DENSE)
                pending.extend(data)
                while len(pending) >= FRAME_SAMPLES * 4:
                    send(b"A", bytes(pending[:FRAME_SAMPLES * 4]))
                    del pending[:FRAME_SAMPLES * 4]

        for frame in container.decode(stream):
            if not MIN_INPUT_RATE <= frame.sample_rate <= MAX_INPUT_RATE:
                raise DecodeFailed(E_UNREADABLE)
            if len(frame.layout.channels) == 1:
                take(to_rate.resample(frame))
            else:
                take(mix(interleaved.resample(frame)))
        take(mix(interleaved.resample(None)))
        take(to_rate.resample(None))
    if pending:
        send(b"A", bytes(pending))


class DecodeFailed(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def main():
    import os
    # numpy's maths library reserves memory per thread when it loads; one
    # thread is plenty for resampling and keeps the job's memory limit honest.
    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[var] = "1"
    # The protocol gets a private copy of the output handle; file descriptor 1
    # and sys.stdout go to the null device, so a library that prints cannot
    # corrupt the frames. That is the null device the app gave as stderr
    # (file descriptor 2): this process never opens it by name, which Windows
    # may refuse in the AppContainer (Windows Server 2025 does).
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = open(1, "w", closefd=False)

    def send(kind, payload=b""):
        out.write(kind + struct.pack("<I", len(payload)) + payload)
        out.flush()

    try:
        level = lower_integrity()
        from workspace import offline_guard
        offline_guard.disable_telemetry()
        offline_guard.install()
        import av  # noqa: F401  (loaded now, at Low integrity, before the file)
        import numpy  # noqa: F401
    except Exception:
        send(b"E", bytes([E_SETUP]))
        return
    send(b"R", struct.pack("<I", level or 0))
    try:
        kind, what = read_request(sys.stdin.buffer)
    except Exception:
        send(b"E", bytes([E_SETUP]))
        return
    try:
        if kind == "handle":
            with open_handle(what) as fh:
                decode(fh, send)
        else:
            decode(sys.stdin.buffer, send, size=what)
        send(b"D")
    except DecodeFailed as exc:
        send(b"E", bytes([exc.code]))
    except MemoryError:
        send(b"E", bytes([E_MEMORY]))
    except Exception:                    # FFmpeg errors carry paths: only a code goes back
        send(b"E", bytes([E_UNREADABLE]))
