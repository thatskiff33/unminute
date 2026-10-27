# Test-only engines loaded inside the worker process via "test:fake_worker_engines:<name>".
# Like the real engines they return segments with word times (ownership is per word).
import os
import socket
import time

from workspace.asr_engines import Segment

SR = 16000


def _seg(start, end, text):
    return Segment(start, end, text, ((start, end, " " + text.split()[0]),))


class _Engine:
    description = "fake engine (test)"
    device, precision = "cpu", "test"

    def __init__(self, fn):
        self.fn = fn

    def transcribe(self, audio):
        return self.fn(audio), len(audio) / SR


def echo(threads):
    return _Engine(lambda a: [_seg(0.0, len(a) / SR, f"n={len(a)} threads={threads} "
                                                     f"pid={os.getpid()}")])


def _sentences(word):
    def run(a):
        out, t, dur = [], 0.5, len(a) / SR
        while t + 3.0 <= dur:
            out.append(Segment(t, t + 3.0, f"{word} {t:.1f}", ((t, t + 3.0, " " + word),)))
            t += 4.0
        return out
    return run


def sentences(threads):
    """A 3 s sentence every 4 s, like a steady speaker."""
    return _Engine(_sentences("sentence"))


def sentences_v2(threads):
    """Same timing, different words: a different model for retranscription."""
    return _Engine(_sentences("second"))


def crash_always(threads):
    def run(a):
        os._exit(3)
    return _Engine(run)


def crash_once(threads):
    marker = os.environ["UNMINUTE_TEST_MARKER"]

    def run(a):
        if not os.path.exists(marker):
            open(marker, "w").close()
            os._exit(3)
        return [_seg(0.0, 0.5, "recovered")]
    return _Engine(run)


MARKER = "Zqmarker"           # tests/test_diagnostics.py: must never reach a log


def marker_words(threads):
    """Transcript text carrying the marker word (diagnostics tests)."""
    return _Engine(_sentences(MARKER))


def raises_marker(threads):
    """Fails with the marker in its message and a path, as content could."""
    def run(a):
        raise RuntimeError(f"{MARKER} secret words from C:\\Users\\{MARKER}\\{MARKER}.wav")
    return _Engine(run)


def _access_violation():
    """Read address 0 in C: a real access violation, as a broken native
    library causes. (faulthandler._sigsegv raises a C signal instead, which
    Windows delivers only to the main thread's handler; ctypes.string_at(0)
    is caught by ctypes and becomes a Python error.)"""
    import faulthandler
    faulthandler._read_null()


def native_crash(threads):
    """A native crash (access violation), as a broken library would cause."""
    def run(a):
        _access_violation()
    return _Engine(run)


def crash_after_reply(threads):
    """Replies, then crashes natively a moment later, while idle."""
    def run(a):
        import threading

        def later():
            time.sleep(0.3)
            _access_violation()
        threading.Thread(target=later, daemon=True).start()
        return [_seg(0.0, 0.5, "replied")]
    return _Engine(run)


def sleepy(threads):
    def run(a):
        time.sleep(30)
        return []
    return _Engine(run)


def slow_exit(threads):
    """Like echo, but its process takes 60 s to end once told to exit (a
    thread that is not a daemon keeps it running), as a loaded machine's
    can take a while (#278)."""
    import threading
    threading.Thread(target=time.sleep, args=(60,), daemon=False).start()
    return echo(threads)


class _CrashesOnRelease(_Engine):
    def __del__(self):
        _access_violation()


def crash_on_release(threads):
    """Like echo, but releasing it crashes natively, as a model's native
    teardown (a graphics card context) can (#278)."""
    return _CrashesOnRelease(echo(threads).fn)


def load_fails(threads):
    raise RuntimeError("model files failed verification")


def speech_not_ready(threads):
    """The real load of Parakeet v2 from an empty model folder: the worker's own
    "not ready" error and its advice (#257)."""
    import tempfile

    from workspace import asr_engines
    from workspace.engines import load_engine
    with tempfile.TemporaryDirectory() as empty:
        return load_engine(asr_engines.PARAKEET_V2, empty, threads)


def speech_cannot_be_used(threads):
    """Parakeet's own load from an empty model folder: its "cannot be used"
    error and advice, as a copy that fails its full hash check gets (#260)."""
    import tempfile

    from workspace import asr_engines
    with tempfile.TemporaryDirectory() as empty:
        return asr_engines.ParakeetEngine(asr_engines.PARAKEET_V2, empty, threads).load()


def weights_only_env(threads):
    """Reports the worker's torch.load switches (#218)."""
    seen = {k: os.environ.get(k) for k in ("TORCH_FORCE_WEIGHTS_ONLY_LOAD",
                                           "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD")}
    return _Engine(lambda a: [_seg(0.0, 0.1, f"env {seen}")])


# -- fake diarizers: diarize_audio("test:fake_worker_engines:<name>", ...) -------

def fake_diarizer(audio, hint):
    """Speakers alternate every 10 s (A, B, A, ...); with a hint of 3 they
    rotate A, B, C. 25-27 s also has B talking over A (overlap evidence)."""
    labels = ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"][: (hint or 2)]
    dur = len(audio) / SR
    exclusive = [(t, min(t + 10.0, dur), labels[int(t // 10) % len(labels)])
                 for t in range(0, int(dur), 10)]
    turns = list(exclusive) + [(25.0, 27.0, labels[1])]
    return turns, exclusive, "cpu"


def torch_dll_folders():
    """The folders PyTorch 2.14.1's __init__ (_load_dll_libraries) and
    TorchCodec 0.17.0's _core/ops.py add to the DLL search path from the
    environment when they load in a speaker worker (#358): the user base's
    Library\\bin (sysconfig, which reads PYTHONUSERBASE), %NVTOOLSEXT_PATH%\\bin\\x64,
    %CUDA_PATH_V<version>%\\bin (any version here), their defaults under
    %ProgramFiles% (NVIDIA Corporation\\NvToolsExt\\bin\\x64, and NVIDIA GPU
    Computing Toolkit\\CUDA\\v<version>\\bin for any version here), and the
    folder of the first ffmpeg shutil.which finds. Each only when it exists,
    as they check; PyTorch takes a variable's folder or its default, this
    takes both. A stand-in for those checks, read as the imports are, that
    adds nothing, so the tests need neither library nor a graphics card."""
    import glob
    import shutil
    import sysconfig
    userbase = sysconfig.get_config_var("userbase")
    found = [os.path.join(userbase, "Library", "bin")] if userbase else []
    if os.environ.get("NVTOOLSEXT_PATH"):
        found.append(os.path.join(os.environ["NVTOOLSEXT_PATH"], "bin", "x64"))
    found += [os.path.join(value, "bin") for name, value in os.environ.items()
              if name.upper().startswith("CUDA_PATH_V")]
    pfiles = os.environ.get("ProgramFiles", "C:\\Program Files")
    found.append(os.path.join(pfiles, "NVIDIA Corporation", "NvToolsExt", "bin", "x64"))
    found += glob.glob(os.path.join(glob.escape(pfiles), "NVIDIA GPU Computing Toolkit",
                                    "CUDA", "v*", "bin"))
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        found.append(os.path.dirname(os.path.abspath(ffmpeg)))
    return [os.path.abspath(f) for f in found if os.path.isdir(f)]


def planted_dll_environment(root):
    """Scratch folders under root that PyTorch and TorchCodec would add to a
    speaker worker's DLL search path (#358), and the environment that names
    them: a user base, NVTOOLSEXT, a CUDA toolkit, a Program Files folder
    holding PyTorch's default NVTOOLSEXT and CUDA folders (named by
    ProgramFiles and by ProgramW6432, from which Windows sets a 64-bit
    child's ProgramFiles), and a folder holding an (empty) ffmpeg.exe, put
    first on PATH. Returns (environment, folders)."""
    from pathlib import Path
    root = Path(root)
    pfiles = root / "Program Files"
    folders = {"userbase": root / "userbase" / "Library" / "bin",
               "nvtx": root / "nvtx" / "bin" / "x64",
               "cuda": root / "cuda" / "bin",
               "pfiles_nvtx": pfiles / "NVIDIA Corporation" / "NvToolsExt" / "bin" / "x64",
               "pfiles_cuda": pfiles / "NVIDIA GPU Computing Toolkit" / "CUDA" / "v13.0" / "bin",
               "ffmpeg": root / "ffmpeg"}
    for folder in folders.values():
        folder.mkdir(parents=True)
    (folders["ffmpeg"] / "ffmpeg.exe").write_bytes(b"")
    env = {"PYTHONUSERBASE": str(root / "userbase"),
           "NVTOOLSEXT_PATH": str(root / "nvtx"),
           "CUDA_PATH_V13_0": str(root / "cuda"),
           "ProgramFiles": str(pfiles),
           "ProgramW6432": str(pfiles),
           "PATH": os.pathsep.join([str(folders["ffmpeg"]), os.environ.get("PATH", "")])}
    return env, {name: os.path.abspath(f) for name, f in folders.items()}


def dll_folders_diarizer(audio, hint):
    """Speaker identification that reports, as its one speaker's label, the
    folders torch_dll_folders finds in this worker (a JSON list)."""
    import json
    return [(0.0, 1.0, json.dumps(torch_dll_folders()))], [], "cpu"


def diarizer_fails(audio, hint):
    raise RuntimeError("speaker model failed verification")


def speaker_not_ready(audio, hint):
    """The real speaker identification load from an empty model folder: the
    worker's own error and its advice (#257)."""
    import tempfile

    from workspace.engines import diarize_audio
    with tempfile.TemporaryDirectory() as empty:
        return diarize_audio("pyannote", empty, audio, hint, 1)


def diarizer_sleepy(audio, hint):
    time.sleep(30)
    return [], [], "cpu"


# -- fake engines for the Whisper GPU worker (#248) --------------------------------
# load_engine passes device="cuda" to a test factory only in the Whisper GPU
# worker; on the processor it is called as factory(threads).

def _whisper_like(fn, device):
    e = _Engine(fn)
    e.device, e.precision = ("cuda", "float16") if device == "cuda" else ("cpu", "int8")
    return e


def gpu_sentences(threads, device="cpu"):
    """sentences, reporting the device and precision a Whisper model would."""
    return _whisper_like(_sentences("sentence"), device)


def gpu_oom_after_first(threads, device="cpu"):
    """On the graphics card the first part works, then CUDA runs out of
    memory, as CTranslate2 reports it. On the processor it always works."""
    calls = []

    def run(a):
        calls.append(1)
        if device == "cuda" and len(calls) > 1:
            raise RuntimeError("CUDA failed with error out of memory")
        return _sentences("sentence")(a)
    return _whisper_like(run, device)


# CTranslate2 4.8's own words when a card cannot run float16 (below compute
# capability 7.0), raised by WhisperModel(device="cuda") at load.
PRECISION_REFUSED = ("Requested float16 compute type, but the target device or backend do "
                     "not support efficient float16 computation.")


def gpu_precision_refused(threads, device="cpu"):
    """A card CTranslate2 cannot run at the pinned precision: the load on the
    card fails with CTranslate2's ValueError. On the processor it works."""
    if device == "cuda":
        raise ValueError(PRECISION_REFUSED)
    return _whisper_like(_sentences("sentence"), device)


def gpu_probe(threads, device="cpu"):
    """Reports where cublas64_12.dll was loaded from in this worker, and the
    first import path entry (the speaker component puts its folder there)."""
    import ctypes
    import json
    import sys
    path = None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetModuleHandleW.restype = ctypes.c_void_p
    k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    k32.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
    handle = k32.GetModuleHandleW("cublas64_12.dll")
    if handle:
        buf = ctypes.create_unicode_buffer(1024)
        k32.GetModuleFileNameW(handle, buf, 1024)
        path = buf.value
    report = json.dumps({"cublas": path, "path0": sys.path[0], "pid": os.getpid()})
    return _whisper_like(lambda a: [_seg(0.0, 0.5, "probe " + report)], device)


def dll_folders(threads, device="cpu"):
    """The folder CTranslate2 4.8.3's __init__ adds to the DLL search path
    from the environment when it loads (#357): %HIP_PATH%\\bin, when HIP_PATH
    is set and no ROCm wheel is installed (its lines 15 to 24). A stand-in
    for that check, read at load as the import is, that adds nothing, so the
    test needs neither CTranslate2 nor a graphics card."""
    hip = os.environ.get("HIP_PATH")
    folders = [os.path.join(hip, "bin")] if hip else []
    return _whisper_like(lambda a: [_seg(0.0, 0.1, f"folders {folders}")], device)


def network(threads):
    def run(a):
        try:
            socket.create_connection(("192.0.2.1", 443), timeout=2)
            return [_seg(0.0, 0.1, "connected")]
        except OSError as exc:
            return [_seg(0.0, 0.1, f"blocked: {type(exc).__name__}")]
    return _Engine(run)


# -- fake download process (workspace.setup.Downloads target) ----------------------

def fake_download(conn, name, cache_dir):
    """Reports what it received; downloads nothing. The token arrives by pipe."""
    import sys
    token = conn.recv()
    conn.send(("log", "Downloading (test)..."))
    in_argv = bool(token) and any(token in a for a in sys.argv)
    in_env = bool(token) and any(token in v for v in os.environ.values())
    conn.send(("done", {"token_received": bool(token), "in_argv": in_argv, "in_env": in_env}))


def fake_download_crashes(conn, name, cache_dir):
    """Starts, then crashes natively part-way (diagnostics tests)."""
    conn.recv()
    conn.send(("log", "Downloading (test)..."))
    _access_violation()


def fake_download_held(conn, name, cache_dir):
    """Runs until the test releases it (#366): a file "release-<name>" in
    cache_dir holding "ok" or "fail". Each start is appended to "order.txt",
    so a test can see the queue's order. Gives up after 60 s."""
    token = conn.recv()
    conn.send(("log", "Downloading (test)..."))
    os.makedirs(cache_dir, exist_ok=True)
    safe = name.replace("/", "_")
    with open(os.path.join(cache_dir, "order.txt"), "a", encoding="utf-8") as f:
        f.write(name + "\n")
    flag = os.path.join(cache_dir, "release-" + safe)
    end = time.monotonic() + 60
    while time.monotonic() < end:
        if os.path.exists(flag):
            with open(flag, encoding="utf-8") as f:
                how = f.read()
            if how == "fail":
                conn.send(("error", "Could not reach Hugging Face. Check the internet "
                                    "connection and try again."))
            else:
                conn.send(("done", {"token_received": bool(token),
                                    "token_matches": token == "hf_TESTtokenNOTreal0123456789"}))
            return
        time.sleep(0.05)
    conn.send(("error", "The test did not release this download."))


def fake_download_fails(conn, name, cache_dir):
    conn.recv()
    conn.send(("error", "Could not reach Hugging Face. Check the internet connection and "
                        "try again."))


# -- fake draft engines (#104): load_draft_engine("test:fake_worker_engines:<name>") --
# Speech is any 32 ms window louder than DRAFT_SPEECH_RMS (synthetic tone bursts);
# the "text" names the phrase's number and length, never anything heard.

DRAFT_SPEECH_RMS = 0.02


class _DraftEngine:
    description = "fake draft engine (test)"

    def __init__(self, word="phrase", delay=0.0, fail=None):
        self.word, self.delay, self.fail, self.count = word, delay, fail, 0
        self.description = f"fake draft engine (test) pid={os.getpid()}"

    def reset(self):
        pass

    def speech_prob(self, window):
        import numpy as np
        return 1.0 if float(np.sqrt(np.mean(np.square(window)))) > DRAFT_SPEECH_RMS else 0.0

    def transcribe_phrase(self, audio):
        self.count += 1
        if self.fail:
            self.fail(self.count)
        if self.delay:
            time.sleep(self.delay)
        return f"{self.word} {self.count} {len(audio) / SR:.1f}s"


def draft_phrases(threads):
    return _DraftEngine()


def draft_marker(threads):
    """Draft text carrying the marker word (diagnostics tests)."""
    return _DraftEngine(word=MARKER)


def draft_slow(threads):
    """Takes 4 s a phrase: falls behind real time."""
    return _DraftEngine(delay=4.0)


def draft_raises_marker(threads):
    """Every phrase fails with the marker and a path in its message."""
    def fail(n):
        raise RuntimeError(f"{MARKER} words from C:\\Users\\{MARKER}\\{MARKER}.wav")
    return _DraftEngine(fail=fail)


def draft_crash_once(threads):
    """The first draft worker crashes at its first phrase; the next one works."""
    marker = os.environ["UNMINUTE_TEST_MARKER"]

    def fail(n):
        if not os.path.exists(marker):
            open(marker, "w").close()
            os._exit(3)
    return _DraftEngine(fail=fail)


def draft_crash_always(threads):
    def fail(n):
        os._exit(3)
    return _DraftEngine(fail=fail)


def draft_load_fails(threads):
    raise RuntimeError("model files failed verification")


class _DraftNetwork(_DraftEngine):
    def transcribe_phrase(self, audio):
        try:
            socket.create_connection(("192.0.2.1", 443), timeout=2)
            got = "connected"
        except OSError as exc:
            got = f"blocked {type(exc).__name__}"
        return f"{got} weights={os.environ.get('TORCH_FORCE_WEIGHTS_ONLY_LOAD')}"


def draft_network(threads):
    """Each phrase says whether a connection got out of the draft worker
    (N1), and its torch.load switch."""
    return _DraftNetwork()


class _DraftScoringFails(_DraftEngine):
    def speech_prob(self, window):
        raise RuntimeError("speech scoring failed")


def draft_scoring_fails(threads):
    """Scoring the audio for speech fails (not a phrase): an error in the
    draft worker's own loop."""
    return _DraftScoringFails()
