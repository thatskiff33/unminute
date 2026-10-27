# workspace/loopback.py
# Computer audio for two-track recording (#108): Windows loopback capture of
# what the computer plays, through WASAPI with ctypes (no new dependency).
#
# Three sources. The app records ALL (the owner's decision 1, 25 Oct 2026,
# after real-sound trials: the default output alone missed a call on a
# headset's hands-free output, and the meeting app's process gave silence).
#   - ALL: every active output device at once, each on its own reader thread
#     with its own Pacer, summed by time into one track (the Mixer). Outputs
#     that appear are added and outputs that go are dropped, each with an
#     event, within about ALL_CHECK_S. An output that is interrupted but still
#     listed is reopened at once, then tried every ALL_RETRY_S, and reported
#     gone only after ALL_GRACE_S, so a device that is settling is one
#     "reopened", never a removal followed by a return. With no output at the
#     start the recording does not start; with none left mid-recording,
#     silence is recorded until one appears; if outputs are listed but none
#     records for REOPEN_LIMIT_S, the computer track ends.
#   - ENDPOINT (kept; the app no longer uses it): everything playing on the
#     Windows default output device
#     (eConsole). When Windows changes the default output, or the device goes,
#     capture follows the new default and says so (decision 6). With no output
#     device at the start, the recording does not start; if the output goes
#     away mid-recording, it records silence until one appears. A reopen that
#     fails (a device still settling) is retried, with silence meanwhile, for
#     up to REOPEN_LIMIT_S before the computer track stops.
#   - PROCESS: only one process and its child processes (the meeting app),
#     through ActivateAudioInterfaceAsync with process loopback parameters.
#     Windows 10 build 20348 or later (Windows 11 on client PCs).
#
# Capture runs on its own reader thread, which owns every COM object it makes
# (initialised multithreaded on that thread; nothing here touches the window's
# apartment). It never plays sound: loopback only reads what other programs play.
# Every block is converted at once to 16-bit mono (the mean of the channels, as
# #220 mixes an import) at LOOPBACK_RATE; Windows converts the device's own
# format (AUTOCONVERTPCM). Nothing is written to disk here: the recorder saves
# the blocks it is given, exactly as it saves the microphone's.
#
# Loopback delivers no data while nothing plays, so silence is not a lost
# device: the Pacer fills those gaps with silence by the monotonic clock, so the
# track stays continuous in time and lines up with the microphone afterwards.
# A gap between blocks longer than GAP_TOL_S, or any gap where Windows flagged
# lost data, is filled the same way; shorter gaps are within the jitter of
# estimating a block's start from when it arrived, and the mix's fit absorbs them.
#
# Imported only when a recording with computer audio starts, or by the probe
# (tools/capture_probe.py): never at window start-up. Stdlib (ctypes) and numpy.

import math
import sys
import threading
import time

import numpy as np

from workspace.mixdown import soft_limit    # numpy only, like this module

LOOPBACK_RATE = 48000            # every computer track is saved at this rate
LOOPBACK_CHANNELS = 2            # asked of Windows; mixed to mono at once
POLL_S = 0.05                    # the reader drains WASAPI this often
BUFFER_S = 1.0                   # WASAPI's own buffer: the reader may be this late
IDLE_PAD_S = 0.2                 # no data for this long: silence is filled in
GAP_TOL_S = 0.03                 # a gap between blocks longer than this is filled in
DEFAULT_CHECK_S = 1.0            # how often the endpoint reader checks the default output
REOPEN_LIMIT_S = 30.0            # the default output cannot be reopened this long: track ends
ACTIVATE_TIMEOUT_S = 5.0         # process loopback activation is asynchronous
ALL_CHECK_S = 1.0                # all outputs: how often the list of outputs is checked
ALL_RETRY_S = 2.0                # all outputs: an output that would not open is tried again
ALL_GRACE_S = 10.0               # all outputs: listed but not recording this long is "removed"
MIX_HOLD_S = BUFFER_S            # the mix waits this long for a slow output (as long as
                                 # Windows keeps its audio), then goes without it
MIX_GAP_S = 0.1                  # a block this far from where its output had reached is a
                                 # gap or a jump, not drift: it is placed at its stamp
MIX_FIT_S = 5.0                  # each output's lag behind its stamps is averaged over about this
MIX_SLACK_S = 0.005              # a lag within this is left alone: samples pass unchanged
MIX_MAX_RATE = 0.001             # drift is corrected by at most 1000 ppm (1.7 cents of pitch)
MIX_PIECE_S = 0.5                # the mix is handed on in pieces no longer than this
NAME_LIMIT = 120                 # an output's name (or all the outputs' names) is cut here

ENDPOINT = "endpoint"
PROCESS = "process"
ALL = "all"


class LoopbackError(RuntimeError):
    """Computer audio could not be captured; the message is safe to show.
    hresult is Windows' error code, when there is one (never shown)."""

    def __init__(self, message, hresult=None):
        super().__init__(message)
        self.hresult = hresult


# -- pure helpers (tested on every platform) -----------------------------------------

def to_mono_int16(data, channels, sample_format):
    """Interleaved samples (bytes) to 16-bit mono: the mean of the channels,
    with float samples beyond full scale clipped. sample_format is "float32"
    or "int16"."""
    if sample_format == "float32":
        x = np.frombuffer(data, dtype="<f4")
    elif sample_format == "int16":
        x = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    else:
        raise LoopbackError("Computer audio uses a sample format the app cannot read.")
    channels = max(1, int(channels))
    x = x[: len(x) - len(x) % channels].reshape(-1, channels).mean(axis=1)
    return np.clip(np.round(x * 32767.0), -32768, 32767).astype(np.int16)


class Pacer:
    """Keeps a computer track continuous in time.

    data(frames, now) is called for each block that arrives. It returns
    (lead, stamp): the stamp is the monotonic time of the block's first frame,
    estimated from when it arrived (so the stamps carry the device clock's
    drift: the ENDPOINT and PROCESS sources hand them on, and the mix at Stop
    corrects the drift smoothly; the ALL source's Mixer corrects it smoothly
    as it sums the outputs, see there); lead is the silence, in frames, to put
    before the block when it is the first audio after an idle spell (or the
    start), or when audio was lost between blocks (a gap past GAP_TOL_S, or
    any gap Windows flagged), so the frames keep pace with the clock. While
    audio flows, no silence is added. idle(now) is called on every pass of the reader;
    once nothing has arrived for IDLE_PAD_S it returns (frames, stamp) of the
    silence that fills the time since the audio last reached, else None."""

    def __init__(self, rate, now):
        self.rate = int(rate)
        self.end = now               # the time the frames delivered so far reach
        self.idle_spell = True       # nothing has arrived yet: the start is a lead

    def data(self, frames, now, lost=False):
        """lost: Windows flagged a discontinuity (it dropped data), so any gap
        is real; otherwise a gap counts only past GAP_TOL_S, the jitter of
        estimating a block's start from when it arrived."""
        stamp = now - frames / self.rate
        lead = 0
        tolerance = 0.0 if (self.idle_spell or lost) else GAP_TOL_S
        if stamp - self.end > tolerance:
            lead = int(round((stamp - self.end) * self.rate))
        self.idle_spell = False
        self.end = now
        return lead, stamp

    def idle(self, now):
        if now - self.end < IDLE_PAD_S:
            return None
        frames = int((now - self.end) * self.rate)
        if frames <= 0:
            return None
        stamp = self.end
        self.end = stamp + frames / self.rate
        self.idle_spell = True
        return frames, stamp


def deliver(pacer, blocks, now, callback, lost=False):
    """One pass of a reader: the blocks that arrived (joined into one), with
    any lead-in silence first, or the silence that fills an idle spell. lost:
    Windows flagged data it dropped before these blocks."""
    block = (blocks[0] if len(blocks) == 1 else np.concatenate(blocks)) if blocks else None
    if block is not None and len(block):
        lead, stamp = pacer.data(len(block), now, lost)
        if lead:
            callback(np.zeros(lead, dtype=np.int16), stamp - lead / pacer.rate)
        callback(block, stamp)
        return
    gap = pacer.idle(now)
    if gap is not None:
        callback(np.zeros(gap[0], dtype=np.int16), gap[1])


class _MixInput:
    """One output in the Mixer. pos: the output frame (fractional) its next
    sample goes to; reach: the next output frame it writes; last: (position,
    value) of its sample before pos, so a block is interpolated on from the
    one before; behind: how far its samples run behind their stamps, in
    seconds, averaged over about MIX_FIT_S (negative: ahead)."""

    __slots__ = ("pos", "reach", "last", "behind")

    def __init__(self, frame):
        self.pos = float(frame)
        self.reach = int(frame)
        self.last = None
        self.behind = 0.0


def _spacing(behind):
    """The output frames each of an output's samples takes: exactly 1 while
    it is within MIX_SLACK_S of its stamps; else a little more (it runs
    behind: a slow device clock) or less (ahead: a fast one), by at most
    MIX_MAX_RATE. In proportion to the averaged lag, with the gain that
    critically damps it (1 / (4 MIX_FIT_S) per second): a steady drift of d
    leaves it MIX_SLACK_S + 4 MIX_FIT_S d behind (9 ms at 200 ppm)."""
    excess = abs(behind) - MIX_SLACK_S
    if excess <= 0:
        return 1.0
    return 1.0 + math.copysign(min(MIX_MAX_RATE, excess / (4 * MIX_FIT_S)), behind)


class Mixer:
    """Sums the ALL source's outputs into one track by time. Thread-safe:
    each output's reader calls add(); the stream's thread calls mix().

    join(key, start) adds an input whose first frame is due at monotonic
    time start; drop(key) removes it (its audio already added stays in).
    add(key, block, stamp) places a block where that input's last block
    ended, unless its stamp is more than MIX_GAP_S away (a gap the Pacer did
    not fill, or a jump): then at the stamp, never writing a frame of that
    input twice. mix(now) returns [(block, stamp)] up to the time every
    input has reached, or to now - MIX_HOLD_S when an input is slow (its late
    audio is then left out); with no inputs, silence up to now. Each output
    keeps its own level: where one plays alone its samples pass unchanged
    (unless its clock drifts, below), and where two or more play at once
    their sum goes through the same soft limiter as the mix at Stop
    (mixdown.soft_limit). The blocks are at most MIX_PIECE_S long.

    Clock drift (#379, replacing the stepped resync of #346 review F2): the
    track is stamped on the app's own clock (start + frames / rate), so each
    output's device clock drift is corrected here, smoothly, as it is summed.
    Each block's stamp says where it belongs; how far the output's samples
    run behind (or ahead of) their stamps is averaged over about MIX_FIT_S,
    which smooths out the arrival-time jitter of the stamps. While that is
    within MIX_SLACK_S the samples are placed one per frame (unchanged while
    they still fall on whole frames); past it they are spaced a little wider
    (behind) or closer (ahead), at most MIX_MAX_RATE, and read off by linear
    interpolation from one block into the next, so the output is resampled
    by a few hundred ppm and drawn back to its stamps without a skip or a
    frame of silence. A steady drift of d then leaves it about MIX_SLACK_S +
    4 MIX_FIT_S d behind (9 ms at 200 ppm). Only a block more than MIX_GAP_S
    from where its output had reached is placed at its stamp, as a gap or a
    jump; drift up to MIX_MAX_RATE never gets that far. The mix at Stop then
    lines up a computer track already on the app's clock."""

    def __init__(self, rate, start):
        self.rate = int(rate)
        self.start = start                       # monotonic time of output frame 0
        self.done = 0                            # output frames handed on
        self.acc = np.zeros(0, dtype=np.int32)   # summed frames from self.done on
        self.count = np.zeros(0, dtype=np.uint8)  # how many inputs wrote each of them
        self.inputs = {}                         # key -> its _MixInput
        self._lock = threading.Lock()

    def _frame(self, t):
        return int(round((t - self.start) * self.rate))

    def join(self, key, start):
        with self._lock:
            self.inputs[key] = _MixInput(max(self.done, self._frame(start)))

    def drop(self, key):
        with self._lock:
            self.inputs.pop(key, None)

    def add(self, key, block, stamp):
        block = np.asarray(block, dtype=np.int16).reshape(-1)
        n = len(block)
        if not n:
            return
        tail = float(block[-1])
        with self._lock:
            inp = self.inputs.get(key)
            if inp is None:
                return                           # dropped: its late audio is not wanted
            at = (stamp - self.start) * self.rate
            off = at - inp.pos
            if abs(off) > MIX_GAP_S * self.rate:
                # A gap or a jump: at the stamp, and its lag measured afresh
                # from there (the lag before was the old position's).
                inp.pos, inp.last, inp.behind = float(round(at)), None, 0.0
            else:
                weight = 1.0 - math.exp(-n / (self.rate * MIX_FIT_S))
                inp.behind += weight * (off / self.rate - inp.behind)
            spacing = _spacing(inp.behind)
            first = max(self.done, inp.reach)    # never a frame of this input twice
            if (spacing == 1.0 and inp.pos.is_integer()
                    and (inp.last is None or inp.last[0] == inp.pos - 1)):
                # On time: each sample is one frame, unchanged.
                at = int(inp.pos)
                end = at + n
                lo = max(first, at)
                block = block[lo - at:]
                inp.pos = float(end)
            else:
                # Drifting: the samples spaced by `spacing`, and each frame
                # read off them (and the last sample before) by interpolation.
                where = inp.pos + spacing * np.arange(n)
                xs, ys = where, block.astype(np.float64)
                if inp.last is not None:
                    xs = np.concatenate(([inp.last[0]], xs))
                    ys = np.concatenate(([inp.last[1]], ys))
                lo = max(first, math.ceil(xs[0]))
                end = math.floor(where[-1]) + 1
                block = np.round(np.interp(np.arange(lo, max(lo, end)), xs, ys)).astype(np.int32)
                inp.pos = float(where[-1] + spacing)
            inp.last = (inp.pos - spacing, tail)
            inp.reach = max(inp.reach, end)
            if not len(block):
                return
            off = lo - self.done
            if off + len(block) > len(self.acc):
                grown = np.zeros(off + len(block), dtype=np.int32)
                grown[:len(self.acc)] = self.acc
                self.acc = grown
                counted = np.zeros(off + len(block), dtype=np.uint8)
                counted[:len(self.count)] = self.count
                self.count = counted
            self.acc[off:off + len(block)] += block
            span = self.count[off:off + len(block)]
            np.minimum(span.astype(np.uint16) + 1, 255, out=span, casting="unsafe")

    def mix(self, now):
        with self._lock:
            floor = self._frame(now - MIX_HOLD_S)
            reach = (min(i.reach for i in self.inputs.values()) if self.inputs
                     else self._frame(now))
            end = max(reach, floor)
            n = end - self.done
            if n <= 0:
                return []
            out = np.zeros(n, dtype=np.int32)
            many = np.zeros(n, dtype=bool)
            have = min(n, len(self.acc))
            out[:have] = self.acc[:have]
            many[:have] = self.count[:have] > 1
            self.acc = self.acc[have:]
            self.count = self.count[have:]
            stamp = self.start + self.done / self.rate
            self.done = end
        if many.any():
            out[many] = np.round(soft_limit(out[many].astype(np.float64)))
        out = np.clip(out, -32768, 32767).astype(np.int16)
        # In pieces of at most MIX_PIECE_S: after a hold, or at Stop, a mix
        # can cover more than a second, and the live drafts' tap cuts a
        # computer block longer than drafts.COMPUTER_BLOCK_S (#375).
        step = max(1, int(MIX_PIECE_S * self.rate))
        return [(out[i:i + step], stamp + i / self.rate) for i in range(0, n, step)]


def all_name(idents, names):
    """The outputs being recorded, as one name: their names in order, at
    most NAME_LIMIT characters. Kept with the recording's details and shown
    to the user like a single output's name; never in diagnostics."""
    return ", ".join(sorted(names.get(i) or "?" for i in idents))[:NAME_LIMIT]


def check_source(source):
    """A source as the recorder stores it: {"kind": "all"}, {"kind":
    "endpoint"} or {"kind": "process", "pid": N}. Raises LoopbackError
    otherwise."""
    if not isinstance(source, dict):
        raise LoopbackError("Unknown computer audio source.")
    kind = source.get("kind")
    if kind in (ALL, ENDPOINT) and set(source) == {"kind"}:
        return {"kind": kind}
    pid = source.get("pid")
    if (kind == PROCESS and set(source) == {"kind", "pid"} and isinstance(pid, int)
            and not isinstance(pid, bool) and 0 < pid < 2 ** 32):
        return {"kind": PROCESS, "pid": pid}
    raise LoopbackError("Unknown computer audio source.")


# -- WASAPI through ctypes (Windows only) ----------------------------------------------

S_OK = 0
E_NOINTERFACE = 0x80004002
E_NOTFOUND = 0x80070490                       # HRESULT_FROM_WIN32(ERROR_NOT_FOUND)
AUDCLNT_S_BUFFER_EMPTY = 0x08890001
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
WAVE_FORMAT_IEEE_FLOAT = 3
CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0x0
E_RENDER, E_CONSOLE = 0, 0                    # EDataFlow eRender, ERole eConsole
DEVICE_STATE_ACTIVE = 0x1
STGM_READ = 0
VT_LPWSTR, VT_BLOB = 31, 65
PROCESS_LOOPBACK_ACTIVATION = 1               # AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
INCLUDE_TARGET_PROCESS_TREE = 0               # PROCESS_LOOPBACK_MODE
VIRTUAL_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

# vtable slots (IUnknown takes 0 to 2)
_ENUM_ENDPOINTS, _ENUM_DEFAULT, _ENUM_GET = 3, 4, 5   # IMMDeviceEnumerator
_COLL_COUNT, _COLL_ITEM = 3, 4                        # IMMDeviceCollection
_DEV_ACTIVATE, _DEV_PROPS, _DEV_ID = 3, 4, 5          # IMMDevice
_PROPS_GET = 5                                        # IPropertyStore::GetValue
_AC_INITIALIZE, _AC_MIX_FORMAT = 3, 8                 # IAudioClient
_AC_START, _AC_STOP, _AC_SET_EVENT, _AC_SERVICE = 10, 11, 13, 14
_CC_GET, _CC_RELEASE, _CC_NEXT = 3, 4, 5              # IAudioCaptureClient
_OP_RESULT = 3                                        # IActivateAudioInterfaceAsyncOperation

_api = None
_api_lock = threading.Lock()
# Completion handlers that Windows may still call after a timed-out
# activation: kept for the life of the process so a late call finds them.
_handlers = []


def _hresult(hr):
    return int(hr) & 0xFFFFFFFF


def _com():
    """The ctypes definitions, built once on first use (Windows only)."""
    global _api
    with _api_lock:
        if _api is not None:
            return _api
        if sys.platform != "win32":
            raise LoopbackError("Computer audio can be recorded only on Windows.")
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        def guid(text):
            g = GUID()
            ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p("{" + text + "}"),
                                                ctypes.byref(g))
            return g

        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

        class BLOB(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("pBlobData", ctypes.c_void_p)]

        class PROPVALUE(ctypes.Union):
            # The union starts at byte 8: a VT_LPWSTR's pointer is there, while
            # a BLOB's data pointer follows its size (byte 16 on x64).
            _fields_ = [("pwszVal", ctypes.c_void_p), ("blob", BLOB)]

        class PROPVARIANT(ctypes.Structure):
            # vt, three reserved words, then the union.
            _anonymous_ = ("value",)
            _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                        ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                        ("value", PROPVALUE)]

        class WAVEFORMATEX(ctypes.Structure):
            _pack_ = 1
            _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                        ("nSamplesPerSec", wintypes.DWORD),
                        ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD),
                        ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]

        class ACTIVATION_PARAMS(ctypes.Structure):
            _fields_ = [("ActivationType", ctypes.c_int),
                        ("TargetProcessId", wintypes.DWORD),
                        ("ProcessLoopbackMode", ctypes.c_int)]

        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole32.CoCreateInstance.restype = ctypes.c_long
        ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(GUID),
                                           ctypes.POINTER(ctypes.c_void_p)]
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None
        ole32.PropVariantClear.argtypes = [ctypes.POINTER(PROPVARIANT)]
        ole32.PropVariantClear.restype = ctypes.c_long
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                                          ctypes.c_void_p]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        try:
            mmdevapi = ctypes.WinDLL("mmdevapi")
            activate_async = mmdevapi.ActivateAudioInterfaceAsync
            activate_async.restype = ctypes.c_long
            activate_async.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(GUID),
                                       ctypes.POINTER(PROPVARIANT), ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p)]
        except (OSError, AttributeError):
            activate_async = None

        def call(ptr, slot, *args, argtypes=()):
            """Call vtable slot of a COM pointer; returns the HRESULT (0..2^32)."""
            vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[slot])
            return _hresult(fn(ptr, *args))

        def release(ptr):
            if ptr:
                vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
                ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(ptr)

        _api = type("ComApi", (), {})()
        _api.__dict__.update(
            ctypes=ctypes, wintypes=wintypes, GUID=GUID, PROPERTYKEY=PROPERTYKEY,
            BLOB=BLOB, PROPVALUE=PROPVALUE, PROPVARIANT=PROPVARIANT, WAVEFORMATEX=WAVEFORMATEX,
            ACTIVATION_PARAMS=ACTIVATION_PARAMS, ole32=ole32, kernel32=kernel32,
            activate_async=activate_async, call=call, release=release,
            CLSID_MMDeviceEnumerator=guid("BCDE0395-E52F-467C-8E3D-C4579291692E"),
            IID_IMMDeviceEnumerator=guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),
            IID_IAudioClient=guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2"),
            IID_IAudioCaptureClient=guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317"),
            IID_IUnknown=guid("00000000-0000-0000-C000-000000000046"),
            IID_IAgileObject=guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90"),
            IID_Completion=guid("41D949AB-9862-444A-80F6-C261334DA5EB"),
            PKEY_FriendlyName=PROPERTYKEY(guid("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 14))
        return _api


def _check(hr, message):
    if hr & 0x80000000:
        raise LoopbackError(message, hresult=hr)
    return hr


def _co_initialize(api):
    """COM for this thread, multithreaded. Returns True if it must be
    uninitialised (a thread already in another apartment is left as it is)."""
    hr = _hresult(api.ole32.CoInitializeEx(None, COINIT_MULTITHREADED))
    return hr in (0, 1)          # S_OK or S_FALSE; RPC_E_CHANGED_MODE is not ours to undo


def _enumerator(api):
    ct = api.ctypes
    out = ct.c_void_p()
    hr = _hresult(api.ole32.CoCreateInstance(ct.byref(api.CLSID_MMDeviceEnumerator), None,
                                             CLSCTX_ALL, ct.byref(api.IID_IMMDeviceEnumerator),
                                             ct.byref(out)))
    _check(hr, "Windows audio is not available.")
    return out.value


def _device_id(api, device):
    ct = api.ctypes
    text = ct.c_void_p()
    _check(api.call(device, _DEV_ID, ct.byref(text), argtypes=(ct.c_void_p,)),
           "Windows audio is not available.")
    try:
        return ct.wstring_at(text.value)
    finally:
        api.ole32.CoTaskMemFree(text.value)


def _friendly_name(api, device):
    """The output device's name as Windows shows it, or "" (shown to the user
    and kept with the recording's details only, never in diagnostics)."""
    ct = api.ctypes
    store = ct.c_void_p()
    if api.call(device, _DEV_PROPS, STGM_READ, ct.byref(store),
                argtypes=(api.wintypes.DWORD, ct.c_void_p)) & 0x80000000:
        return ""
    try:
        value = api.PROPVARIANT()
        hr = api.call(store.value, _PROPS_GET, ct.byref(api.PKEY_FriendlyName),
                      ct.byref(value), argtypes=(ct.c_void_p, ct.c_void_p))
        try:
            return "" if hr & 0x80000000 else _propvariant_text(api, value)
        finally:
            api.ole32.PropVariantClear(ct.byref(value))
    finally:
        api.release(store.value)


def _propvariant_text(api, value):
    """A VT_LPWSTR PROPVARIANT's text, whitespace tidied and at most 120
    characters; "" for any other type or a null pointer."""
    if value.vt != VT_LPWSTR or not value.pwszVal:
        return ""
    return " ".join(api.ctypes.wstring_at(value.pwszVal).split())[:NAME_LIMIT]


def _default_id(api, enumerator):
    """The default output device's id, or None when there is none."""
    default = _default_render(api, enumerator)
    if default is None:
        return None
    try:
        return _device_id(api, default)
    finally:
        api.release(default)


def _default_render(api, enumerator):
    """The default output device (IMMDevice pointer), or None when there is none."""
    ct = api.ctypes
    device = ct.c_void_p()
    hr = api.call(enumerator, _ENUM_DEFAULT, E_RENDER, E_CONSOLE, ct.byref(device),
                  argtypes=(ct.c_int, ct.c_int, ct.c_void_p))
    if hr == E_NOTFOUND:
        return None
    _check(hr, "Windows audio is not available.")
    return device.value


def list_outputs():
    """Active output devices as [{"name", "default"}], for the probe and,
    later, Settings. Opens nothing and plays nothing."""
    api = _com()
    owned = _co_initialize(api)
    enumerator = None
    try:
        enumerator = _enumerator(api)
        default_id = _default_id(api, enumerator)
        return [{"name": name, "default": ident == default_id}
                for ident, name in _active_outputs(api, enumerator)]
    finally:
        api.release(enumerator)
        if owned:
            api.ole32.CoUninitialize()


def _active_outputs(api, enumerator):
    """Every active output device as [(device id, name)], in Windows' order."""
    ct = api.ctypes
    collection = ct.c_void_p()
    _check(api.call(enumerator, _ENUM_ENDPOINTS, E_RENDER, DEVICE_STATE_ACTIVE,
                    ct.byref(collection),
                    argtypes=(ct.c_int, api.wintypes.DWORD, ct.c_void_p)),
           "Windows audio is not available.")
    out = []
    try:
        count = ct.c_uint()
        _check(api.call(collection.value, _COLL_COUNT, ct.byref(count),
                        argtypes=(ct.c_void_p,)), "Windows audio is not available.")
        for i in range(count.value):
            device = ct.c_void_p()
            if api.call(collection.value, _COLL_ITEM, i, ct.byref(device),
                        argtypes=(ct.c_uint, ct.c_void_p)) & 0x80000000:
                continue
            try:
                out.append((_device_id(api, device.value), _friendly_name(api, device.value)))
            finally:
                api.release(device.value)
    finally:
        api.release(collection.value)
    return out


def _capture_format(api):
    """What the app asks Windows for: 48 kHz stereo float. Windows converts
    the device's own mix format to it (AUTOCONVERTPCM)."""
    fmt = api.WAVEFORMATEX()
    fmt.wFormatTag = WAVE_FORMAT_IEEE_FLOAT
    fmt.nChannels = LOOPBACK_CHANNELS
    fmt.nSamplesPerSec = LOOPBACK_RATE
    fmt.wBitsPerSample = 32
    fmt.nBlockAlign = fmt.nChannels * fmt.wBitsPerSample // 8
    fmt.nAvgBytesPerSec = fmt.nSamplesPerSec * fmt.nBlockAlign
    fmt.cbSize = 0
    return fmt


class _Client:
    """One initialised loopback IAudioClient and its capture client."""

    def __init__(self, api, audio_client, event=None):
        ct = api.ctypes
        self.api, self.client, self.capture, self.event = api, audio_client, None, event
        fmt = _capture_format(api)
        flags = (AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
                 | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
        if event is not None:
            flags |= AUDCLNT_STREAMFLAGS_EVENTCALLBACK
        try:
            _check(api.call(audio_client, _AC_INITIALIZE, AUDCLNT_SHAREMODE_SHARED, flags,
                            ct.c_longlong(int(BUFFER_S * 10_000_000)), ct.c_longlong(0),
                            ct.byref(fmt), None,
                            argtypes=(ct.c_int, api.wintypes.DWORD, ct.c_longlong,
                                      ct.c_longlong, ct.c_void_p, ct.c_void_p)),
                   "Computer audio could not be opened.")
            if event is not None:
                _check(api.call(audio_client, _AC_SET_EVENT, event,
                                argtypes=(api.wintypes.HANDLE,)),
                       "Computer audio could not be opened.")
            capture = ct.c_void_p()
            _check(api.call(audio_client, _AC_SERVICE, ct.byref(api.IID_IAudioCaptureClient),
                            ct.byref(capture), argtypes=(ct.c_void_p, ct.c_void_p)),
                   "Computer audio could not be opened.")
            self.capture = capture.value
            self.block_align = fmt.nBlockAlign
            _check(api.call(audio_client, _AC_START), "Computer audio could not be started.")
        except Exception:
            self.close()
            raise

    def read(self):
        """Every packet waiting, as ([mono int16 block], lost): lost is True
        when Windows flagged a discontinuity (data dropped because the reader
        fell behind). Raises LoopbackError with the HRESULT when the device has gone."""
        api, ct = self.api, self.api.ctypes
        blocks, lost = [], False
        size = ct.c_uint32()
        while True:
            hr = api.call(self.capture, _CC_NEXT, ct.byref(size), argtypes=(ct.c_void_p,))
            _check(hr, "Computer audio stopped.")
            if size.value == 0:
                return blocks, lost
            data, frames, flags = ct.c_void_p(), ct.c_uint32(), ct.c_uint32()
            hr = api.call(self.capture, _CC_GET, ct.byref(data), ct.byref(frames),
                          ct.byref(flags), None, None,
                          argtypes=(ct.c_void_p,) * 5)
            _check(hr, "Computer audio stopped.")
            if hr == AUDCLNT_S_BUFFER_EMPTY:
                return blocks, lost
            try:
                if flags.value & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY:
                    lost = True        # the pacer fills the lost time before this pass's audio
                n = frames.value
                if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data.value:
                    blocks.append(np.zeros(n, dtype=np.int16))
                else:
                    raw = ct.string_at(data.value, n * self.block_align)
                    blocks.append(to_mono_int16(raw, LOOPBACK_CHANNELS, "float32"))
            finally:
                api.call(self.capture, _CC_RELEASE, frames.value, argtypes=(ct.c_uint32,))

    def close(self):
        api = self.api
        if self.client:
            try:
                api.call(self.client, _AC_STOP)
            except Exception:
                pass
        api.release(self.capture)
        api.release(self.client)
        self.capture = self.client = None


def _endpoint_client(api, enumerator):
    """(client, name, device id) for the default output, or (None, None, None)
    when there is none. The name and id are read before the stream starts,
    so an error there leaves nothing running."""
    device = _default_render(api, enumerator)
    if device is None:
        return None, None, None
    try:
        name, ident = _friendly_name(api, device), _device_id(api, device)
        return _activate(api, device), name, ident
    finally:
        api.release(device)


def _activate(api, device):
    """A started loopback client on an output device (IMMDevice pointer)."""
    ct = api.ctypes
    audio_client = ct.c_void_p()
    _check(api.call(device, _DEV_ACTIVATE, ct.byref(api.IID_IAudioClient), CLSCTX_ALL,
                    None, ct.byref(audio_client),
                    argtypes=(ct.c_void_p, api.wintypes.DWORD, ct.c_void_p, ct.c_void_p)),
           "Computer audio could not be opened.")
    return _Client(api, audio_client.value)


def _output_client(api, enumerator, ident):
    """A started loopback client on the output device with this id (ALL)."""
    ct = api.ctypes
    device = ct.c_void_p()
    _check(api.call(enumerator, _ENUM_GET, ident, ct.byref(device),
                    argtypes=(ct.c_wchar_p, ct.c_void_p)),
           "Computer audio could not be opened.")
    try:
        return _activate(api, device.value)
    finally:
        api.release(device.value)


class _OutputReader:
    """One output device's loopback for the ALL source, on its own reader
    thread with its own COM objects and Pacer, adding its blocks to the
    mixer (keyed by this reader, so a reopened output is a new input). It is
    opening, then live (open and reading), then gone (it would not open, the
    device went, or it was stopped). was_live stays True once it has been
    live. A gone reader has left the mixer."""

    def __init__(self, ident, mixer, tick):
        self.ident, self.mixer, self._tick = ident, mixer, tick
        self.began = tick()
        self.live = self.gone = self.was_live = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="loopback-output",
                                        daemon=True)

    def start(self):
        self._thread.start()
        return self

    @property
    def opening(self):
        return not (self.live or self.gone)

    @property
    def alive(self):
        """Its thread is still running (stopped readers too, until they end)."""
        return self._thread.is_alive()

    def stop(self, wait=True):
        self._stop.set()
        if wait and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(5)

    def _run(self):
        api = client = enumerator = None
        owned = False
        try:
            api = _com()
            owned = _co_initialize(api)
            enumerator = _enumerator(api)
            client = _output_client(api, enumerator, self.ident)
            if self._stop.is_set():
                return                         # given up on while it was opening
            pacer = Pacer(LOOPBACK_RATE, self._tick())
            self.mixer.join(self, pacer.end)
            self.live = self.was_live = True

            def add(block, stamp):
                self.mixer.add(self, block, stamp)

            while not self._stop.wait(POLL_S):
                try:
                    blocks, lost = client.read()
                except LoopbackError:
                    break                      # the device went (or Windows audio restarted)
                deliver(pacer, blocks, self._tick(), add, lost)
        except Exception:
            pass                               # would not open: the stream tries it again
        finally:
            self.mixer.drop(self)
            self.gone = True                   # first, so it never looks "opening" as it ends
            self.live = False
            if client is not None:
                client.close()
            if enumerator is not None:
                api.release(enumerator)
            if owned:
                api.ole32.CoUninitialize()


def _process_client(api, pid, event):
    """An IAudioClient capturing one process tree, through the asynchronous
    activation Windows requires for process loopback."""
    ct = api.ctypes
    if api.activate_async is None:
        raise LoopbackError("Recording one app's sound needs Windows 11.")
    done = threading.Event()
    me = ct.c_void_p()

    QI = ct.WINFUNCTYPE(ct.c_long, ct.c_void_p, ct.POINTER(api.GUID), ct.POINTER(ct.c_void_p))
    REF = ct.WINFUNCTYPE(ct.c_ulong, ct.c_void_p)
    DONE = ct.WINFUNCTYPE(ct.c_long, ct.c_void_p, ct.c_void_p)

    def same(a, b):
        return bytes(a) == bytes(b)

    def query(this, iid, out):
        g = iid.contents
        if any(same(g, x) for x in (api.IID_IUnknown, api.IID_Completion,
                                    api.IID_IAgileObject)):
            out[0] = this
            return S_OK
        out[0] = None
        return ct.c_long(E_NOINTERFACE - 2 ** 32).value

    def completed(this, operation):
        done.set()
        return S_OK

    # A minimal COM object: a pointer to this vtable. AddRef and Release are
    # no-ops because Python owns it (kept alive in _handlers). It answers for
    # IAgileObject, as Windows requires of this handler.
    callbacks = [QI(query), REF(lambda this: 1), REF(lambda this: 1), DONE(completed)]
    vtable = (ct.c_void_p * 4)(*[ct.cast(c, ct.c_void_p) for c in callbacks])
    obj = (ct.c_void_p * 1)(ct.cast(vtable, ct.c_void_p))
    _handlers.append((callbacks, vtable, obj))       # Windows may call it after a timeout
    me.value = ct.addressof(obj)

    params = api.ACTIVATION_PARAMS(PROCESS_LOOPBACK_ACTIVATION, pid,
                                   INCLUDE_TARGET_PROCESS_TREE)
    blob = api.PROPVARIANT()
    blob.vt, blob.blob.cbSize = VT_BLOB, ct.sizeof(params)
    blob.blob.pBlobData = ct.addressof(params)
    operation = ct.c_void_p()
    _check(_hresult(api.activate_async(VIRTUAL_PROCESS_LOOPBACK,
                                       ct.byref(api.IID_IAudioClient), ct.byref(blob),
                                       me, ct.byref(operation))),
           "That app's sound could not be opened.")
    try:
        if not done.wait(ACTIVATE_TIMEOUT_S):
            raise LoopbackError("That app's sound could not be opened in time.")
        result, unknown = ct.c_long(), ct.c_void_p()
        _check(api.call(operation.value, _OP_RESULT, ct.byref(result), ct.byref(unknown),
                        argtypes=(ct.c_void_p, ct.c_void_p)),
               "That app's sound could not be opened.")
        if _hresult(result.value) & 0x80000000:
            api.release(unknown.value)
            raise LoopbackError("That app's sound could not be opened.",
                                hresult=_hresult(result.value))
        return _Client(api, unknown.value, event=event)
    finally:
        api.release(operation.value)


class LoopbackStream:
    """Computer audio from one source. callback(block, stamp) gets each
    16-bit mono block at LOOPBACK_RATE with the monotonic time of its first
    frame; notify(kind, name) is told "switched" (ENDPOINT: now following a
    new default output), "output_added" (ALL: an output not recorded before
    in this recording is recorded too), "output_removed" (ALL: an output
    went; the others carry on), "reopened" (an output recorded earlier is
    back after it was interrupted, or after a spell with none), "no_output"
    (no output device: silence until one appears) and "failed" (capture
    stopped; nothing more arrives). The name given is the output now
    recorded (ALL: every output now recorded). Both are called on the
    stream's own thread. start()
    returns once capture is running, or raises LoopbackError; stop() and
    close() may be called more than once."""

    def __init__(self, source, callback, notify, tick=time.monotonic):
        self.source = check_source(source)
        self._callback, self._notify, self._tick = callback, notify, tick
        self._stop = threading.Event()
        self._opened = threading.Event()
        self._error = None
        self._thread = None
        self.device_name = None

    def start(self):
        if sys.platform != "win32":
            raise LoopbackError("Computer audio can be recorded only on Windows.")
        self._thread = threading.Thread(target=self._run, name="loopback-reader", daemon=True)
        self._thread.start()
        if not self._opened.wait(ACTIVATE_TIMEOUT_S + 5):
            self._stop.set()
            raise LoopbackError("Computer audio did not start in time.")
        if self._error is not None:
            raise self._error

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(5)

    def close(self):
        self.stop()

    # -- reader thread ---------------------------------------------------------------

    def _run(self):
        try:
            api = _com()
            owned = _co_initialize(api)
        except Exception as exc:
            self._error = exc if isinstance(exc, LoopbackError) else \
                LoopbackError("Computer audio could not be opened.")
            self._opened.set()
            return
        try:
            if self.source["kind"] == PROCESS:
                self._run_process(api)
            elif self.source["kind"] == ALL:
                self._run_all(api)
            else:
                self._run_endpoint(api)
        except Exception as exc:
            if not self._opened.is_set():
                self._error = exc if isinstance(exc, LoopbackError) else \
                    LoopbackError("Computer audio could not be opened.")
            elif not self._stop.is_set():
                self._say("failed", None)
        finally:
            self._opened.set()
            if owned:
                api.ole32.CoUninitialize()

    def _say(self, kind, name):
        try:
            self._notify(kind, name)
        except Exception:
            pass

    def _deliver(self, pacer, blocks, lost=False):
        deliver(pacer, blocks, self._tick(), self._callback, lost)

    def _run_process(self, api):
        event = api.kernel32.CreateEventW(None, False, False, None)
        client = None
        try:
            client = _process_client(api, self.source["pid"], event)
            pacer = Pacer(LOOPBACK_RATE, self._tick())
            self._opened.set()
            wait_ms = int(POLL_S * 1000)
            # Windows signals every packet (about 10 ms); the blocks are handed
            # on once per POLL_S, as the endpoint reader does, so the recorder's
            # queue holds the same length of audio whichever the source.
            pending, lost, due = [], False, self._tick() + POLL_S
            while not self._stop.is_set():
                api.kernel32.WaitForSingleObject(event, wait_ms)
                blocks, dropped = client.read()
                pending.extend(blocks)
                lost = lost or dropped
                if self._tick() >= due:
                    self._deliver(pacer, pending, lost)
                    pending, lost, due = [], False, self._tick() + POLL_S
            if pending:
                # Read before the stop but not yet handed on: delivered, not
                # dropped (the recorder still takes blocks until its stream
                # is closed).
                self._deliver(pacer, pending, lost)
        finally:
            if client is not None:
                client.close()
            api.kernel32.CloseHandle(event)

    def _run_endpoint(self, api):
        enumerator = _enumerator(api)
        client = None
        try:
            client, self.device_name, current = _endpoint_client(api, enumerator)
            if client is None:
                raise LoopbackError("This computer has no sound output to record.")
            last = current            # the output last opened, kept while there is none
            pacer = Pacer(LOOPBACK_RATE, self._tick())
            self._opened.set()
            checked = self._tick()
            failing = None            # when reopening the output began to fail
            while not self._stop.wait(POLL_S):
                blocks, dropped, gone = [], False, False
                if client is not None:
                    try:
                        blocks, dropped = client.read()
                    except LoopbackError:
                        gone = True        # the device went (or Windows audio restarted)
                self._deliver(pacer, blocks, dropped)
                now = self._tick()
                if not gone and now - checked < DEFAULT_CHECK_S:
                    continue
                checked = now
                if gone:
                    client.close()
                    client = None
                # Follow the Windows default output (decision 6): reopen when it
                # changed, the device went or an earlier reopen failed; with
                # none, silence until one appears. A reopen that fails (a device
                # still settling) is retried here with silence meanwhile, and
                # ends the track only after REOPEN_LIMIT_S.
                try:
                    new_id = _default_id(api, enumerator)
                    if client is not None and new_id == current:
                        continue
                    if client is not None:
                        client.close()
                        client = None
                    if new_id is not None:
                        client, name, opened = _endpoint_client(api, enumerator)
                        new_id = opened if client is not None else None
                except LoopbackError:
                    failing = now if failing is None else failing
                    if now - failing >= REOPEN_LIMIT_S:
                        raise
                    continue
                failing = None
                if new_id is None:
                    if current is not None:
                        self._say("no_output", None)
                    current = None
                    continue
                self.device_name = name
                # The same output back (after an interruption, or after a spell
                # with none) is reopened; any other is a switch.
                self._say("reopened" if new_id == last else "switched", name)
                current = last = new_id
        finally:
            if client is not None:
                client.close()
            api.release(enumerator)

    def _run_all(self, api):
        """Every active output, each on its own reader, summed by time
        (Mixer). This thread checks the list of outputs every ALL_CHECK_S (at
        once when a reader has gone), opens new ones, drops those that went,
        hands the mix on every POLL_S and tells the recorder what changed."""
        enumerator = _enumerator(api)
        readers = {}                  # device id -> its current _OutputReader
        try:
            mixer = Mixer(LOOPBACK_RATE, self._tick())
            names = dict(_active_outputs(api, enumerator))
            if not names:
                raise LoopbackError("This computer has no sound output to record.")
            for ident in names:
                readers[ident] = _OutputReader(ident, mixer, self._tick).start()
            deadline = time.monotonic() + ACTIVATE_TIMEOUT_S
            while (any(r.opening for r in readers.values())
                   and time.monotonic() < deadline):
                time.sleep(0.01)
            # Recorded from the start: every output that has been live, even
            # one that has already dropped out again (it is reopened below).
            recorded = {i for i, r in readers.items() if r.was_live}
            if not recorded:
                raise LoopbackError("Computer audio could not be opened.")
            self.device_name = all_name(recorded, names)
            self._opened.set()
            self._supervise(api, enumerator, mixer, readers, names, recorded)
            # The readers stop first (each leaves the mixer as it ends); what
            # they read before the stop is then mixed and handed on, not dropped.
            self._stop_readers(readers)
            for block, stamp in mixer.mix(self._tick()):
                self._callback(block, stamp)
        finally:
            self._stop_readers(readers)
            api.release(enumerator)

    @staticmethod
    def _stop_readers(readers):
        """Tell every reader to stop, then wait for those that are reading
        (each closes its client and leaves the mixer). One still opening is
        let go without waiting: a driver that never answers must not hold up
        Stop, and the reader ends, releasing what it holds, when it does."""
        for r in readers.values():
            r.stop(wait=False)
        for r in readers.values():
            if not r.opening:
                r.stop()
        readers.clear()

    def _supervise(self, api, enumerator, mixer, readers, names, recorded):
        """The ALL source's loop, until stop() or a failure (raised).

        recorded: the outputs the recorder has been told are recorded. ever:
        every output recorded at some point in this recording; one of these
        back is "reopened", any other "output_added". interrupted: recorded
        outputs whose reader went while Windows still lists them (reopened at
        once, then every ALL_RETRY_S). down: when each recorded output stopped
        recording; it is "output_removed" when Windows no longer lists it or
        after ALL_GRACE_S, unless no other output is recording: then the track
        carries on with silence, and ends after REOPEN_LIMIT_S."""
        ever, interrupted, down, retry_at = set(recorded), set(), {}, {}
        # Readers told to stop before they had ended (stuck opening, or their
        # output went), by device: no new reader starts for a device while
        # one of these is still running, so a driver whose activation never
        # returns holds one thread, not one more every ALL_RETRY_S (1c03917
        # review F1). They are let go at the end, and end when Windows answers.
        stopped = {}
        listed = set(names)
        checked = self._tick()
        failing = None                # when listing the outputs began to fail
        none_since = None             # when outputs were listed but none recorded
        while not self._stop.wait(POLL_S):
            now = self._tick()
            for block, stamp in mixer.mix(now):
                self._callback(block, stamp)
            # Readers that went: one that was live is reopened at once; one
            # that would not open (a reopen too), or is stuck opening, is
            # tried again after ALL_RETRY_S.
            went = False
            for ident, r in list(readers.items()):
                stuck = r.opening and now - r.began > ACTIVATE_TIMEOUT_S
                if not (r.gone or stuck):
                    continue
                went = True
                if stuck:
                    r.stop(wait=False)     # it leaves the mixer itself when it returns
                    stopped.setdefault(ident, []).append(r)
                if r.was_live:
                    retry_at[ident] = now
                    if ident in recorded:
                        interrupted.add(ident)
                else:
                    retry_at[ident] = now + ALL_RETRY_S
                del readers[ident]
            if went or now - checked >= ALL_CHECK_S:
                checked = now
                try:
                    found = dict(_active_outputs(api, enumerator))
                except LoopbackError:
                    failing = now if failing is None else failing
                    if now - failing >= REOPEN_LIMIT_S:
                        raise
                    found = None
                if found is not None:
                    failing = None
                    names.update(found)
                    listed = set(found)
                    for ident in [i for i in readers if i not in listed]:
                        r = readers.pop(ident)
                        r.stop(wait=False)
                        stopped.setdefault(ident, []).append(r)
                    for ident in list(stopped):
                        stopped[ident] = [r for r in stopped[ident] if r.alive]
                        if not stopped[ident]:
                            del stopped[ident]
                    for ident in listed:
                        if ident in stopped:
                            continue           # its earlier reader has not ended yet
                        if ident not in readers and now >= retry_at.get(ident, now):
                            retry_at.pop(ident, None)
                            readers[ident] = _OutputReader(ident, mixer, self._tick).start()
            # Tell the recorder only once no reader is opening, so a reopen
            # is seen as one change, never as a removal and then a return.
            if any(r.opening for r in readers.values()):
                continue
            live = {i for i, r in readers.items() if r.live}
            if not listed:
                none_since = None
                interrupted.clear()
                if recorded:
                    recorded.clear()
                    down.clear()
                    self._say("no_output", None)
                continue
            if live:
                none_since = None
            elif none_since is None:
                none_since = now
            elif now - none_since >= REOPEN_LIMIT_S:
                raise LoopbackError("Computer audio stopped.")
            for ident in recorded - live:
                down.setdefault(ident, now)
            gone = {i for i in recorded - live
                    if i not in listed or now - down[i] >= ALL_GRACE_S}
            if gone and live:
                recorded -= gone
                interrupted -= gone
                for ident in gone:
                    down.pop(ident, None)
                self.device_name = all_name(live, names)
                self._say("output_removed", self.device_name)
            new = live - recorded
            back = (new & ever) | (live & interrupted)
            added = new - ever
            if not (back or added):
                continue
            recorded |= new
            ever |= new
            interrupted -= live
            for ident in live:
                down.pop(ident, None)
            self.device_name = all_name(live, names)
            if added:
                self._say("output_added", self.device_name)
            if back:
                self._say("reopened", self.device_name)


def open_loopback(source, callback, notify):
    """The recorder's seam for computer audio (tests pass a fake with the same
    shape): an object with start(), stop() and close()."""
    return LoopbackStream(source, callback, notify)


def probe(source, seconds=1.0):
    """Open a loopback stream for at most a second and count what arrived.
    Numbers only: no audio is kept and nothing is played."""
    seconds = min(1.0, max(0.1, float(seconds)))
    got = {"blocks": 0, "frames": 0, "nonzero": 0, "events": []}

    def callback(block, stamp):
        got["blocks"] += 1
        got["frames"] += len(block)
        got["nonzero"] += int(np.count_nonzero(block))

    stream = LoopbackStream(source, callback, lambda kind, name: got["events"].append(kind))
    started = time.monotonic()
    stream.start()
    try:
        time.sleep(seconds)
    finally:
        stream.close()
    got["seconds"] = round(time.monotonic() - started, 2)
    got["rate"] = LOOPBACK_RATE
    return got
