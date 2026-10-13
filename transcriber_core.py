# transcriber_core.py
# Engine for the local Whisper transcriber: model loading, speaker
# identification, transcript rendering/saving, and the queue worker.
# No tkinter in here - the UI (transcribe_ui.py) imports this module.
#
# Import this module BEFORE faster_whisper/pyannote anywhere else: it routes
# the Hugging Face cache off OneDrive and recovers HF_TOKEN at import time.

import contextlib
import gc
import os
import threading
import time
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")  # hide harmless torchcodec/symlink warnings

# --- Configuration -----------------------------------------------------------

# Model cache lives in LOCALAPPDATA so OneDrive never syncs it.
CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.getcwd()), "whisper-models")

# Route Hugging Face downloads (pyannote) into the same non-synced cache.
os.environ.setdefault("HF_HUB_CACHE", CACHE_DIR)
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", CACHE_DIR)

# Offline by default. Once a model is cached, the Hugging Face libraries must
# make NO network calls - no revision/etag checks, no telemetry. These must be
# set BEFORE faster_whisper/huggingface_hub are imported (the offline flag is
# read once, at import). A deliberate one-off download lifts this via
# allow_network(); steady-state transcription never touches the network.
for _k, _v in (("HF_HUB_OFFLINE", "1"),
               ("TRANSFORMERS_OFFLINE", "1")):
    os.environ.setdefault(_k, _v)

# No usage statistics, ever: force every telemetry switch off (pyannote sends
# OpenTelemetry metrics to otel.pyannote.ai by default), then refuse any
# non-loopback network access outside allow_network(). See offline_guard.py.
import offline_guard

offline_guard.disable_telemetry()
offline_guard.install()

# The HF token authorises downloads of the gated pyannote models (one-off).
# Read it from the user environment; fall back to the registry in case this
# process started before `setx HF_TOKEN ...` propagated.
if not os.environ.get("HF_TOKEN"):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as _key:
            os.environ["HF_TOKEN"] = winreg.QueryValueEx(_key, "HF_TOKEN")[0]
    except OSError:
        pass

from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio

# faster-whisper imports onnxruntime lazily (for its VAD); import it now so its
# telemetry events are off before first use (costs ~0.2 s at startup).
offline_guard.disable_onnxruntime_telemetry()

import asr_engines


@contextlib.contextmanager
def allow_network():
    """Lift offline mode for ONE deliberate download/update, then restore it.

    Everything else runs offline (see the env vars above). Only code inside this
    block may reach the network; offline mode is restored afterwards even on
    error. Used for the first-time model/speaker download and the Update button."""
    keys = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    saved_env = {k: os.environ.get(k) for k in keys}
    try:
        import huggingface_hub.constants as hub_const  # already imported by faster_whisper
    except Exception:
        hub_const = None
    saved_flag = getattr(hub_const, "HF_HUB_OFFLINE", None)
    try:
        for k in keys:
            os.environ[k] = "0"
        if hub_const is not None:
            hub_const.HF_HUB_OFFLINE = False   # the constant is what the library actually checks
        with offline_guard.permit():
            yield
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if hub_const is not None and saved_flag is not None:
            hub_const.HF_HUB_OFFLINE = saved_flag

# Transcripts are saved here, next to the scripts.
EXTRACT_DIR = Path(__file__).resolve().parent / "extracted"

# (model name, Hugging Face repo faster-whisper resolves it to - None for
# Parakeet, which asr_engines manages - UI hint). The first entry is the
# default for new users; a saved choice is kept.
MODEL_INFO = (
    ("parakeet-tdt-0.6b-v2", None, "recommended: fast, English"),
    ("large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "Whisper"),
    ("large-v3", "Systran/faster-whisper-large-v3", "Whisper, slowest"),
    ("parakeet-tdt-0.6b-v3", None, "fast, multilingual"),
)
MODELS = tuple(name for name, _, _ in MODEL_INFO)
MODEL_DOWNLOAD_SIZES = {"large-v3-turbo": "1.6 GB", "large-v3": "3 GB",
                        "parakeet-tdt-0.6b-v2": "0.7 GB", "parakeet-tdt-0.6b-v3": "0.7 GB"}
# Short form shown in the queue's Options column.
MODEL_SHORT = {"large-v3-turbo": "turbo", "large-v3": "v3",
               "parakeet-tdt-0.6b-v2": "parakeet", "parakeet-tdt-0.6b-v3": "parakeet v3"}

DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"
COMPUTE_TYPE = "int8"   # sensible CPU default; use "int8_float32" if a CPU rejects it
LANGUAGE = "en"         # set to None to auto-detect the language
SAMPLE_RATE = 16000
# Leave a couple of cores free so the laptop stays usable during long jobs.
CPU_THREADS = max(4, (os.cpu_count() or 8) - 2)

AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".mp4", ".mov", ".mkv", ".flac",
                    ".ogg", ".aac", ".wma", ".webm", ".avi")
FILETYPES = [
    ("Audio/video", " ".join("*" + ext for ext in AUDIO_EXTENSIONS)),
    ("All files", "*.*"),
]

# Per-user UI settings (window size, last-used options) live here.
SETTINGS_FILE = Path(os.environ.get("LOCALAPPDATA", os.getcwd())) / "whisper" / "ui-settings.json"


# --- Model availability (drives what the UI offers) ----------------------------

def _hub_dir(repo):
    """Hugging Face hub cache folder for a repo id."""
    return Path(CACHE_DIR) / ("models--" + repo.replace("/", "--"))


def model_downloaded(name):
    """True if the model's weights are already in the local cache.

    Parakeet must also have every pinned file at its exact size; it is fully
    checksummed when loaded. Whisper keeps its existing folder check."""
    if asr_engines.is_parakeet(name):
        return model_status(name)[0] == "ready"
    for model_name, repo, _ in MODEL_INFO:
        if model_name == name:
            return _hub_dir(repo).is_dir()
    return False


def model_status(name):
    """('ready' | 'missing' | 'invalid', detail) for the UI."""
    if asr_engines.is_parakeet(name):
        return asr_engines.parakeet_status(name, CACHE_DIR)
    return ("ready", "") if model_downloaded(name) else ("missing", "not installed")


def diarizer_available():
    """Speaker ID works if the pyannote model is cached (offline) or a token
    allows downloading it. Returns (available, reason-if-not)."""
    if _hub_dir(DIARIZATION_MODEL).is_dir():
        return True, ""
    if os.environ.get("HF_TOKEN"):
        return True, ""
    return False, ("Needs a Hugging Face token - re-run install.cmd and follow "
                   "the token step.")


# --- Model loading (shared between threads) ----------------------------------

_models = {}            # at most ONE entry: name -> engine (see asr_engines)
_model_lock = threading.Lock()


def _load_whisper(name, log):
    kwargs = dict(device="cpu", compute_type=COMPUTE_TYPE,
                  cpu_threads=CPU_THREADS, download_root=CACHE_DIR)
    if model_downloaded(name):
        model = WhisperModel(name, **kwargs)        # offline, from cache
    else:
        log(f"Model '{name}' is not cached yet - downloading once (needs internet)...")
        with allow_network():
            model = WhisperModel(name, **kwargs)
    return asr_engines.WhisperEngine(model, name, COMPUTE_TYPE, LANGUAGE)


def _load_parakeet(name, log):
    state, detail = model_status(name)
    if state != "ready":
        if state == "invalid":
            log(f"Model '{name}' needs repair: {detail}")
        log(f"Downloading verified '{name}' files once (needs internet)...")
        with allow_network():
            asr_engines.install_parakeet(name, CACHE_DIR, log=lambda m: log("  " + m))
    engine = asr_engines.ParakeetEngine(name, CACHE_DIR, CPU_THREADS, decode_audio)
    return engine.load()   # checksums the files, then loads offline


def get_model(name, log):
    """Return a loaded engine with a transcribe(audio) -> (segments, duration)
    method. Only one model is kept in memory at a time: switching models
    evicts the old one first (a reload takes ~15s, holding both costs
    ~2.5 GB of RAM - bad on 16 GB laptops)."""
    with _model_lock:
        if name not in _models:
            if _models:
                old = next(iter(_models))
                log(f"Releasing model '{old}' to free memory...")
                _models.pop(old).close()
                gc.collect()
            log(f"Loading model '{name}' into memory...")
            if asr_engines.is_parakeet(name):
                _models[name] = _load_parakeet(name, log)
            else:
                _models[name] = _load_whisper(name, log)
            log(f"Model '{name}' ready: {_models[name].description}.")
        return _models[name]


_diarizer = None
_diarizer_lock = threading.Lock()


def get_diarizer(log):
    """Return the pyannote diarisation pipeline, loading it on first use."""
    global _diarizer
    with _diarizer_lock:
        if _diarizer is None:
            log("Loading speaker identification pipeline...")
            from pyannote.audio import Pipeline  # heavy import, deferred
            if _hub_dir(DIARIZATION_MODEL).is_dir():
                _diarizer = Pipeline.from_pretrained(DIARIZATION_MODEL)   # offline, from cache
            else:
                log("Speaker model is not cached yet - downloading once (needs internet)...")
                with allow_network():
                    _diarizer = Pipeline.from_pretrained(DIARIZATION_MODEL)
            log("Speaker identification ready.")
        return _diarizer


# --- Helpers ------------------------------------------------------------------

def format_ts(seconds):
    """Seconds -> H:MM:SS (or M:SS under an hour) for display."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def unique_path(out):
    """Avoid overwriting an existing transcript: meeting.txt -> meeting (2).txt."""
    if not out.exists():
        return out
    i = 2
    while True:
        candidate = out.with_name(f"{out.stem} ({i}){out.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def diarize(wav, log, hook=None):
    """Run diarisation on a decoded waveform; return (start, end, label) turns."""
    import torch
    pipeline = get_diarizer(log)
    waveform = torch.from_numpy(wav).unsqueeze(0)
    output = pipeline({"waveform": waveform, "sample_rate": SAMPLE_RATE}, hook=hook)
    # pyannote 4.x wraps the annotation in a DiarizeOutput; 3.x returned it directly.
    annotation = getattr(output, "speaker_diarization", output)
    return [(t.start, t.end, lbl)
            for t, _, lbl in annotation.itertracks(yield_label=True)]


def speaker_for(turns, start, end):
    """Pick the diarisation label with the largest time overlap for a segment."""
    best, best_overlap = None, 0.0
    for s, e, lbl in turns:
        overlap = min(end, e) - max(start, s)
        if overlap > best_overlap:
            best, best_overlap = lbl, overlap
    return best


def render_transcript(entries, timestamps=False):
    """Render (speaker, text, start_seconds) entries. Consecutive same-speaker
    lines group into one paragraph; entries without speakers render as plain
    lines. With timestamps on, each paragraph/line is prefixed [H:MM:SS]."""
    def stamp(start):
        return f"[{format_ts(start)}] " if timestamps else ""

    if not any(who for who, _, _ in entries):
        return "\n".join(f"{stamp(start)}{text}" for _, text, start in entries) + "\n"
    blocks = []  # (speaker, first start, [texts])
    for who, text, start in entries:
        if blocks and blocks[-1][0] == who:
            blocks[-1][2].append(text)
        else:
            blocks.append((who, start, [text]))
    return "\n\n".join(f"{stamp(start)}{who}: {' '.join(texts)}"
                       for who, start, texts in blocks) + "\n"


def save_transcript(out, entries, fmt, src, model_name, timestamps=False):
    """Write the transcript file, with a small metadata header for .md."""
    body = render_transcript(entries, timestamps=timestamps)
    if fmt == ".md":
        header = (
            f"# Transcript: {src.name}\n\n"
            f"- Source: {src}\n"
            f"- Model: {model_name}\n"
            f"- Transcribed: {datetime.now():%Y-%m-%d %H:%M}\n\n"
        )
        body = header + body
    out.write_text(body, encoding="utf-8")


def rename_speakers(result, mapping):
    """Rewrite a finished transcript with real names ("Speaker 1" -> "Jordan").
    `result` is the dict the worker emitted via the item_result event; its
    entries are updated in place so a second rename chains correctly."""
    result["entries"] = [
        (mapping.get(who, who) if who else who, text, start)
        for who, text, start in result["entries"]
    ]
    save_transcript(Path(result["out"]), result["entries"], result["fmt"],
                    Path(result["src"]), result["model"],
                    timestamps=result["timestamps"])


# --- Queue worker (runs off the UI thread) -------------------------------------

class CancelledJob(Exception):
    """Raised inside the worker when the user clicks Stop."""


def queue_worker(pending, pending_lock, settings, emit, cancel_event, pause_event):
    """Drain the shared queue one file at a time, one transcript per file.
    `settings` (model/fmt/speakers/timestamps) was snapshotted when Start was
    clicked and applies to every file in this run. Pause/Stop are honoured at
    chunk boundaries (every few seconds). An error on one file marks it failed
    and moves on to the next."""
    log = lambda msg: emit("log", msg)

    def checkpoint():
        # Pause/stop point, reached between processing chunks. Sits idle while
        # paused (CPU drops to zero); a Stop click breaks out immediately.
        while pause_event.is_set() and not cancel_event.is_set():
            time.sleep(0.2)
        if cancel_event.is_set():
            raise CancelledJob()

    def diarization_hook(step_name, step_artifact=None, file=None,
                         total=None, completed=None):
        # pyannote calls this between its internal steps: our pause/stop point,
        # plus rough progress for the status line.
        checkpoint()
        if total:
            # pyannote's counters can briefly overshoot the total mid-step.
            pct = min(100, 100 * completed // total)
            emit("phase", f"identifying speakers ({step_name} {pct}%)")

    model_label = settings["model"]   # replaced by the engine's full description once loaded
    load_error = None                 # set if the model fails to load in this run

    def finish(item, src, entries, out_name, status):
        """Save the transcript and tell the UI about it (enables renaming)."""
        out = unique_path(EXTRACT_DIR / out_name)
        save_transcript(out, entries, settings["fmt"], src, model_label,
                        timestamps=settings["timestamps"])
        emit("item_result", (item["id"], {
            "out": str(out), "entries": entries, "fmt": settings["fmt"],
            "src": str(src), "model": model_label,
            "timestamps": settings["timestamps"],
            "speakers": any(who for who, _, _ in entries),
        }))
        emit("item_status", (item["id"], status))
        return out

    stopped = False
    try:
        EXTRACT_DIR.mkdir(exist_ok=True)
        while True:
            if cancel_event.is_set():
                stopped = True
                break
            with pending_lock:
                item = pending.pop(0) if pending else None
            if item is None:
                break  # queue drained

            src = Path(item["path"])
            emit("item_status", (item["id"], "working"))
            emit("file_start", src.name)
            log(f"\nTranscribing {src.name} with {settings['model']}...")

            entries = []  # (speaker name or None, text, start seconds)
            try:
                # A model that failed to load fails the rest of this run the
                # same way: no re-hashing per file, and no repair download the
                # user did not accept at Start.
                if load_error is not None:
                    raise asr_engines.ModelLoadError(str(load_error))
                try:
                    model = get_model(settings["model"], log)
                except asr_engines.ModelLoadError as exc:
                    load_error = exc
                    raise
                model_label = model.description

                # Optional speaker identification first: decode the audio once
                # with PyAV, reuse the waveform for both steps.
                turns = None
                audio = str(src)
                if settings["speakers"]:
                    emit("phase", "identifying speakers")
                    wav = decode_audio(str(src), sampling_rate=SAMPLE_RATE)
                    turns = diarize(wav, log, hook=diarization_hook)
                    log(f"  Found {len({lbl for _, _, lbl in turns})} speaker(s).")
                    audio = wav

                emit("phase", "transcribing")
                segments, duration = model.transcribe(audio)
                # Total audio length drives the percentage bar.
                emit("duration", duration)

                # Segments are generated as transcription progresses: label each
                # with its speaker, log it live, and report progress (seg.end).
                names = {}        # SPEAKER_00 -> "Speaker 1", by first appearance
                last_label = None
                for seg in segments:
                    checkpoint()  # pause/stop between segments
                    text = seg.text.strip()
                    who = None
                    if turns is not None:
                        label = speaker_for(turns, seg.start, seg.end)
                        if label is None:
                            label = last_label  # carry speaker through short gaps
                        if label is not None and label not in names:
                            names[label] = f"Speaker {len(names) + 1}"
                        last_label = label
                        who = names.get(label, "Unknown speaker")
                    prefix = f"{who}: " if who else ""
                    log(f"  [{format_ts(seg.start)} - {format_ts(seg.end)}] {prefix}{text}")
                    entries.append((who, text, seg.start))
                    emit("progress", seg.end)
            except CancelledJob:
                stopped = True
                if entries:
                    out = finish(item, src, entries,
                                 f"{src.stem} (partial){settings['fmt']}", "stopped")
                    log(f"\nStopped. Partial transcript saved: {out}")
                else:
                    log("\nStopped before any text was produced.")
                    emit("item_status", (item["id"], "stopped"))
                break
            except Exception as exc:
                log(f"\nERROR on {src.name}: {exc}")
                emit("item_error", (item["id"], str(exc)))
                emit("item_status", (item["id"], "failed"))
                continue  # carry on with the rest of the queue

            emit("file_done", None)
            out = finish(item, src, entries, src.stem + settings["fmt"], "done")
            log(f"Saved: {out}")

        log("\nStopped by user." if stopped else "\nQueue finished.")
        emit("done", "stopped" if stopped else "ok")
    except Exception as exc:
        log(f"\nERROR: {exc}")
        emit("done", "error")
