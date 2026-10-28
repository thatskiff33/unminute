# workspace/engines.py
# Load a speech engine for a worker, strictly offline (#15).
#
# Inference workers never download: a model that is missing or fails its
# checks raises ModelLoadError, and the setup/download step (#19) - a separate
# process with deliberate network permission - installs or repairs it.
# Engines come from asr_engines and share its verified manifests.

import importlib

from workspace import asr_engines
from workspace.asr_engines import ModelLoadError

TEST_PREFIX = "test:"      # "test:module:function" loads a test engine factory
# Where a user downloads or repairs a model once Setup is done (#244). A worker
# only loads models after Setup, so its messages point here, never to Setup.
DOWNLOADS = "Settings > Manage downloads"
OTHER_SPEECH = "choose another speech model in Settings"


def fix_speech(name, repair_only=False):
    """How a user fixes a speech model that cannot load: in Manage downloads,
    which offers every speech model (#246; a test keeps it so), or another
    model (#244)."""
    if repair_only:
        return f"Repair {asr_engines.label(name)} in {DOWNLOADS}, or {OTHER_SPEECH}."
    return f"Download or repair it in {DOWNLOADS}, or {OTHER_SPEECH}."


# The speaker model's advice (speaker_load.LOAD_FAILED ends with it too; a test
# keeps the two equal).
SPEAKER_FIX = f"Repair it in {DOWNLOADS}."
# With downloads turned off by managed policy (K3), Manage downloads cannot fix
# a model. A worker reads no other policy value than AllowUserModels (on a
# machine install only, asr_engines.user_models_allowed, #111 B7a), so the
# window process swaps the advice above for these before it stores a load error
# (#257), also when that value means a download would not be used. The speech
# wording matches the note Settings shows beside the picker.
MANAGED_SPEECH_FIX = ("Downloads are turned off by your organisation. Choose another speech "
                      "model in Settings, or ask your IT team to install it.")
MANAGED_SPEAKER_FIX = ("Downloads are turned off by your organisation. Ask your IT team to "
                       "install or repair it.")
# A machine install whose AllowUserModels is 0 while downloads are allowed (#111
# B7a, #451): downloads are not turned off there, but a model in the user's own
# cache would not be used, so the advice names the models IT installs, in the
# sense of app.USER_MODELS_OFF (which is USER_MODELS_SPEAKER_FIX).
USER_MODELS_ONLY = ("Your organisation uses only the models your IT team installs on this "
                    "computer.")
USER_MODELS_SPEECH_FIX = (f"{USER_MODELS_ONLY} Choose another speech model in Settings, or ask "
                          "your IT team to install or repair it.")
USER_MODELS_SPEAKER_FIX = f"{USER_MODELS_ONLY} Ask your IT team to install or repair it."


def advice_without_downloads(message, only_it_models=False):
    """A worker's model error with the Manage downloads advice replaced by the
    IT wording, for when managed policy turns downloads off (#257), or, with
    only_it_models, when a machine install uses only IT's models (#451). Only
    the fixed advice written here, in asr_engines and in speaker_load is
    replaced; the rest of the message is kept as it was. Every speech model
    gets the same wording (#246: Manage downloads offers them all)."""
    speech, speaker = ((USER_MODELS_SPEECH_FIX, USER_MODELS_SPEAKER_FIX) if only_it_models
                       else (MANAGED_SPEECH_FIX, MANAGED_SPEAKER_FIX))
    for name in (*asr_engines.PARAKEET_MODELS, *asr_engines.WHISPER_MODELS):
        for repair_only in (False, True):
            message = message.replace(fix_speech(name, repair_only), speech)
    message = message.replace(asr_engines.PARAKEET_FIX, speech)
    return message.replace(SPEAKER_FIX, speaker)


def _test_factory(name, allow_test):
    """The factory named by a "test:module:function" engine. Only tests set
    allow_test: otherwise any stored model name could import any module."""
    if not allow_test:
        raise ModelLoadError("Unknown speech model.")
    module, func = name[len(TEST_PREFIX):].split(":")
    return getattr(importlib.import_module(module), func)


def load_engine(name, cache_dir, threads, allow_test=False, device="cpu"):
    """Return a loaded engine with transcribe(waveform) -> (segments, duration).
    device="cuda" only in the Whisper GPU worker (#248): a Whisper model on
    the graphics card at its pinned precision (gpu.WHISPER_PRECISION).
    Anything that stops that raises GpuUnavailable or CTranslate2's own CUDA
    error, and the caller falls back to the processor."""
    if name.startswith(TEST_PREFIX):
        factory = _test_factory(name, allow_test)
        # Test factories used on the graphics card take the device.
        return factory(threads, device="cuda") if device == "cuda" else factory(threads)

    if device == "cuda":
        return _load_whisper_gpu(name, cache_dir, threads)

    if asr_engines.is_parakeet(name):
        state, detail = asr_engines.parakeet_status(name, cache_dir)
        if state != "ready":
            raise ModelLoadError(f"{asr_engines.label(name)} is not ready. "
                                 f"{_parakeet_fix(name)} ({detail})")
        # No decoder: the worker passes decoded arrays. faster_whisper's
        # decoder would load faster_whisper, and CTranslate2 with it, into
        # every Parakeet worker (R12, #133).
        return asr_engines.ParakeetEngine(name, cache_dir, threads).load()

    if asr_engines.is_whisper(name):
        spec = asr_engines.WHISPER_MODELS[name]
        model = _whisper_model(spec, cache_dir, threads)
        problem = asr_engines.feature_width_problem(model, spec)
        if problem:
            raise ModelLoadError(f"{problem}. {fix_speech(name, repair_only=True)}")
        # Word times are required: chunk ownership is decided per word.
        return asr_engines.WhisperEngine(model, name, "int8", "en")

    raise ModelLoadError(f"Unknown model: {asr_engines.label(name)}")


def _parakeet_fix(name):
    """How to fix a Parakeet model that is not ready: IT, when IT's copy of it
    or of its speech detector is there but not usable (#451), or Manage
    downloads."""
    for spec in (asr_engines.PARAKEET_MODELS[name], asr_engines.SILERO_VAD):
        if asr_engines.machine_unusable(spec):
            return asr_engines.machine_damaged(spec)
    return fix_speech(name)


def _whisper_fix(spec):
    """How to fix a Whisper model that cannot be used: IT, when IT installed it
    (#451; only IT can repair that copy), or Manage downloads."""
    if asr_engines.machine_copy(spec) is not None:
        return asr_engines.machine_damaged(spec)
    return fix_speech(spec.name)


def _whisper_model(spec, cache_dir, threads):
    """faster-whisper's model on the processor from the first verified copy.
    IT's copy passes on its record; one that then fails to load and fails a
    full check is kept as failed (asr_engines.check_machine), and the next
    copy is tried once: the user's own, where AllowUserModels allows one (#451)."""
    for again in (False, True):
        folder = asr_engines.find_verified(spec, cache_dir)
        if folder is None:
            raise ModelLoadError(f"{asr_engines.label(spec.name)} is not installed or failed "
                                 f"verification. {_whisper_fix(spec)}")
        # Imported only once a verified copy is found: a missing copy names the fix
        # even where faster-whisper is not installed (review of 98d22c1, B2).
        from faster_whisper import WhisperModel
        try:
            return WhisperModel(str(folder), device="cpu", compute_type="int8",
                                cpu_threads=threads)
        except Exception as exc:
            if not (asr_engines._is_machine(spec, folder)
                    and asr_engines.check_machine(spec, folder, full=True)):
                raise
            if again:
                raise ModelLoadError(f"{asr_engines.label(spec.name)} failed to load. "
                                     f"{asr_engines.machine_damaged(spec)} "
                                     f"({type(exc).__name__})") from exc


def load_draft_engine(name, cache_dir, threads, allow_test=False):
    """The draft worker's engine (#104): Parakeet v2 on phrases with a
    streaming Silero scorer, checked and loaded offline like the full pass
    (I1). Only Parakeet v2 writes drafts (owner decision 8)."""
    if name.startswith(TEST_PREFIX):
        return _test_factory(name, allow_test)(threads)
    if name != asr_engines.PARAKEET_V2:
        raise ModelLoadError(f"Draft text uses {asr_engines.label(asr_engines.PARAKEET_V2)} "
                             "only.")
    state, detail = asr_engines.parakeet_status(name, cache_dir)
    if state != "ready":
        # IT, when IT's copy is there but not usable (#464), as the full pass says.
        raise ModelLoadError(f"{asr_engines.label(name)} is not ready. {_parakeet_fix(name)} "
                             f"({detail})")
    return asr_engines.DraftEngine(name, cache_dir, threads).load()


class GpuUnavailable(RuntimeError):
    """The CUDA build or a usable NVIDIA card/driver is not available."""


def _load_whisper_gpu(name, cache_dir, threads):
    """A Whisper model on the graphics card, in the Whisper GPU worker, whose
    cuBLAS the worker loaded by full path before this (gpu.activate_whisper)."""
    from workspace import gpu
    precision = gpu.WHISPER_PRECISION.get(name)
    if precision is None:
        raise GpuUnavailable(f"{asr_engines.label(name)} is not offered on the graphics card")
    spec = asr_engines.WHISPER_MODELS[name]
    folder = asr_engines.find_verified(spec, cache_dir)
    if folder is None:
        raise ModelLoadError(f"{asr_engines.label(name)} is not installed or failed "
                             f"verification. {_whisper_fix(spec)}")
    import ctranslate2
    if ctranslate2.get_cuda_device_count() < 1:
        raise GpuUnavailable(f"graphics card unavailable (needs NVIDIA driver "
                             f"{gpu.MIN_DRIVER_TEXT} or later and the graphics card "
                             f"component for Whisper)")
    # A card CTranslate2 cannot run at the pinned precision (float16 needs
    # compute capability 7.0 or later, so Pascal and older cannot) is the
    # card's limit, not the model's: say so, and the processor takes over (D4).
    if precision not in ctranslate2.get_supported_compute_types("cuda"):
        raise GpuUnavailable(f"the graphics card cannot run {asr_engines.label(name)} at "
                             f"{precision}")
    from faster_whisper import WhisperModel
    try:
        model = WhisperModel(str(folder), device="cuda", compute_type=precision,
                             cpu_threads=threads)
    except (ValueError, RuntimeError, OSError) as exc:
        # The model files were verified above, so a failure to load them onto
        # the card is the card's (CTranslate2 refusing the precision, the
        # driver, graphics memory). The processor then loads the same model
        # and reports any fault of the model's own (D4).
        raise GpuUnavailable(f"the graphics card could not load {asr_engines.label(name)}: "
                             f"{exc}") from exc
    problem = asr_engines.feature_width_problem(model, spec)
    if problem:
        raise ModelLoadError(f"{problem}. {fix_speech(name, repair_only=True)}")
    engine = asr_engines.WhisperEngine(model, name, precision, "en")
    engine.device = "cuda"
    return engine


SPEAKER_MODEL = "pyannote"


def diarize_audio(model, cache_dir, audio, hint, threads, device="cpu", allow_test=False):
    """Speaker separation over a meeting's decoded audio (16 kHz mono
    float32, the whole waveform: the worker reads it from the encrypted file,
    #111 C3a). Returns (turns, exclusive_turns, device); turns are
    (start, end, label). Offline only: builds pyannote from the hash-checked
    bytes in memory."""
    import numpy as np
    audio = np.ascontiguousarray(audio, dtype=np.float32)
    if model.startswith(TEST_PREFIX):
        factory = _test_factory(model, allow_test)
        result = factory(audio, hint)
        if device == "cuda":              # tests: the fake reports where it "ran"
            return result[0], result[1], getattr(importlib.import_module(factory.__module__),
                                                 "GPU_LABEL", "cuda")
        return result

    # Every file (about 33 MB) is read once and hashed in full before each
    # load, not checked by size and date, and the pipeline is built from those
    # same bytes: no model file is opened again after its check (#111 B9).
    blobs = asr_engines.read_verified(asr_engines.SPEAKER_MODEL, cache_dir)
    if blobs is None:
        # Only IT can repair IT's copy, which comes first (#451).
        fix = (asr_engines.machine_damaged(asr_engines.SPEAKER_MODEL)
               if asr_engines.machine_copy(asr_engines.SPEAKER_MODEL) is not None else SPEAKER_FIX)
        raise ModelLoadError("The speaker identification model is not installed or failed "
                             f"verification. {fix}")
    import torch
    torch.set_num_threads(threads)
    label = "cpu"
    if device == "cuda":
        if "+cu" not in torch.__version__ or not torch.cuda.is_available():
            from workspace import gpu
            raise GpuUnavailable(f"graphics card unavailable (needs NVIDIA driver "
                                 f"{gpu.MIN_DRIVER_TEXT} or later and the graphics card "
                                 f"component)")
        label = f"cuda ({torch.cuda.get_device_name(0)})"[:80]
    from workspace import speaker_load
    pipeline = speaker_load.build_pipeline(blobs)
    del blobs
    if device == "cuda":
        pipeline.to(torch.device("cuda"))
    kwargs = {"num_speakers": int(hint)} if hint else {}
    out = pipeline({"waveform": torch.from_numpy(audio).unsqueeze(0), "sample_rate": 16000},
                   **kwargs)
    full = getattr(out, "speaker_diarization", out)
    exclusive = getattr(out, "exclusive_speaker_diarization", full)

    def as_list(annotation):
        return [(float(t.start), float(t.end), str(lbl))
                for t, _, lbl in annotation.itertracks(yield_label=True)]
    turns, excl = as_list(full), as_list(exclusive)
    del pipeline
    if device == "cuda":
        torch.cuda.empty_cache()
    return turns, excl, label          # pyannote runs in float32 on either device
