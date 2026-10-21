# workspace/loopback.py
# Computer audio for two-track recording (#108): Windows loopback capture of
# what the computer plays, through WASAPI with ctypes (no new dependency).
#
# Two sources, as the design names them (owner's decisions 1 and 2, 20 Oct 2026):
#   - ENDPOINT: everything playing on the Windows default output device
#     (eConsole). When Windows changes the default output, or the device goes,
#     capture follows the new default and says so (decision 6); with no output
#     device at all it records silence until one appears.
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
#
# Imported only when a recording with computer audio starts, or by the probe
# (tools/capture_probe.py): never at window start-up. Stdlib (ctypes) and numpy.

import sys
import threading
import time

import numpy as np

LOOPBACK_RATE = 48000            # every computer track is saved at this rate
LOOPBACK_CHANNELS = 2            # asked of Windows; mixed to mono at once
POLL_S = 0.05                    # the reader drains WASAPI this often
BUFFER_S = 1.0                   # WASAPI's own buffer: the reader may be this late
IDLE_PAD_S = 0.2                 # no data for this long: silence is filled in
DEFAULT_CHECK_S = 1.0            # how often the endpoint reader checks the default output
ACTIVATE_TIMEOUT_S = 5.0         # process loopback activation is asynchronous

ENDPOINT = "endpoint"
PROCESS = "process"


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
    drift, which the mix corrects); lead is the silence, in frames, to put
    before the block when it is the first audio after an idle spell (or the
    start), so the frames keep pace with the clock. While audio flows, no
    silence is ever added. idle(now) is called on every pass of the reader;
    once nothing has arrived for IDLE_PAD_S it returns (frames, stamp) of the
    silence that fills the time since the audio last reached, else None."""

    def __init__(self, rate, now):
        self.rate = int(rate)
        self.end = now               # the time the frames delivered so far reach
        self.idle_spell = True       # nothing has arrived yet: the start is a lead

    def data(self, frames, now):
        stamp = now - frames / self.rate
        lead = 0
        if self.idle_spell and stamp > self.end:
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


def deliver(pacer, blocks, now, callback):
    """One pass of a reader: the blocks that arrived (joined into one), with
    any lead-in silence first, or the silence that fills an idle spell."""
    block = (blocks[0] if len(blocks) == 1 else np.concatenate(blocks)) if blocks else None
    if block is not None and len(block):
        lead, stamp = pacer.data(len(block), now)
        if lead:
            callback(np.zeros(lead, dtype=np.int16), stamp - lead / pacer.rate)
        callback(block, stamp)
        return
    gap = pacer.idle(now)
    if gap is not None:
        callback(np.zeros(gap[0], dtype=np.int16), gap[1])


def check_source(source):
    """A source as the recorder stores it: {"kind": "endpoint"} or
    {"kind": "process", "pid": N}. Raises LoopbackError otherwise."""
    if not isinstance(source, dict):
        raise LoopbackError("Unknown computer audio source.")
    kind = source.get("kind")
    if kind == ENDPOINT and set(source) == {"kind"}:
        return {"kind": ENDPOINT}
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
_ENUM_ENDPOINTS, _ENUM_DEFAULT = 3, 4                 # IMMDeviceEnumerator
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

        class PROPVARIANT(ctypes.Structure):
            # vt, three reserved words, then a 16-byte union: as a pointer
            # (VT_LPWSTR) or a BLOB {ULONG cbSize; BYTE *pBlobData}.
            _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                        ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                        ("size", ctypes.c_ulong), ("pad", ctypes.c_ulong),
                        ("ptr", ctypes.c_void_p)]

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
            PROPVARIANT=PROPVARIANT, WAVEFORMATEX=WAVEFORMATEX,
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
            if hr & 0x80000000 or value.vt != VT_LPWSTR or not value.ptr:
                return ""
            return " ".join(ct.wstring_at(value.ptr).split())[:120]
        finally:
            api.ole32.PropVariantClear(ct.byref(value))
    finally:
        api.release(store.value)


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
    ct = api.ctypes
    owned = _co_initialize(api)
    enumerator = None
    try:
        enumerator = _enumerator(api)
        default = _default_render(api, enumerator)
        default_id = _device_id(api, default) if default else None
        api.release(default)
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
                    out.append({"name": _friendly_name(api, device.value),
                                "default": _device_id(api, device.value) == default_id})
                finally:
                    api.release(device.value)
        finally:
            api.release(collection.value)
        return out
    finally:
        api.release(enumerator)
        if owned:
            api.ole32.CoUninitialize()


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
        """Every packet waiting, as [(mono int16 block)]; raises LoopbackError
        with the HRESULT when the device has gone."""
        api, ct = self.api, self.api.ctypes
        blocks = []
        size = ct.c_uint32()
        while True:
            hr = api.call(self.capture, _CC_NEXT, ct.byref(size), argtypes=(ct.c_void_p,))
            _check(hr, "Computer audio stopped.")
            if size.value == 0:
                return blocks
            data, frames, flags = ct.c_void_p(), ct.c_uint32(), ct.c_uint32()
            hr = api.call(self.capture, _CC_GET, ct.byref(data), ct.byref(frames),
                          ct.byref(flags), None, None,
                          argtypes=(ct.c_void_p,) * 5)
            _check(hr, "Computer audio stopped.")
            if hr == AUDCLNT_S_BUFFER_EMPTY:
                return blocks
            try:
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
    """(client, name) for the default output, or (None, None) when there is none."""
    ct = api.ctypes
    device = _default_render(api, enumerator)
    if device is None:
        return None, None, None
    try:
        audio_client = ct.c_void_p()
        _check(api.call(device, _DEV_ACTIVATE, ct.byref(api.IID_IAudioClient), CLSCTX_ALL,
                        None, ct.byref(audio_client),
                        argtypes=(ct.c_void_p, api.wintypes.DWORD, ct.c_void_p, ct.c_void_p)),
               "Computer audio could not be opened.")
        return _Client(api, audio_client.value), _friendly_name(api, device), \
            _device_id(api, device)
    finally:
        api.release(device)


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
    blob.vt, blob.size = VT_BLOB, ct.sizeof(params)
    blob.ptr = ct.addressof(params)
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
    frame; notify(kind, name) is told "switched" (now following a new default
    output, name given), "no_output" (no output device: silence until one
    appears) and "failed" (capture stopped; nothing more arrives). Both are
    called on the reader thread. start() returns once capture is running, or
    raises LoopbackError; stop() and close() may be called more than once."""

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

    def _deliver(self, pacer, blocks):
        deliver(pacer, blocks, self._tick(), self._callback)

    def _run_process(self, api):
        event = api.kernel32.CreateEventW(None, False, False, None)
        client = None
        try:
            client = _process_client(api, self.source["pid"], event)
            pacer = Pacer(LOOPBACK_RATE, self._tick())
            self._opened.set()
            wait_ms = int(POLL_S * 1000)
            while not self._stop.is_set():
                api.kernel32.WaitForSingleObject(event, wait_ms)
                self._deliver(pacer, client.read())
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
            pacer = Pacer(LOOPBACK_RATE, self._tick())
            self._opened.set()
            checked = self._tick()
            while not self._stop.wait(POLL_S):
                blocks, lost = [], False
                if client is not None:
                    try:
                        blocks = client.read()
                    except LoopbackError:
                        lost = True        # the device went (or Windows audio restarted)
                self._deliver(pacer, blocks)
                now = self._tick()
                if not lost and now - checked < DEFAULT_CHECK_S:
                    continue
                checked = now
                # Follow the Windows default output (decision 6): reopen when it
                # changed or the device went; with none, silence until one appears.
                default = _default_render(api, enumerator)
                new_id = _device_id(api, default) if default else None
                api.release(default)
                if new_id == current and not lost:
                    continue
                if client is not None:
                    client.close()
                    client = None
                if new_id is None:
                    if current is not None:
                        self._say("no_output", None)
                    current = None
                    continue
                client, name, current = _endpoint_client(api, enumerator)
                if client is not None:
                    self.device_name = name
                    self._say("switched", name)
        finally:
            if client is not None:
                client.close()
            api.release(enumerator)


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
