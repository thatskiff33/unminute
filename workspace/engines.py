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
# a model. A worker never reads the policy, so the window process, which does,
# swaps the advice above for these before it stores a load error (#257). The
# speech wording matches the note Settings shows beside the picker.
MANAGED_SPEECH_FIX = ("Downloads are turned off by your organisation. Choose another speech "
                      "model in Settings, or ask your IT team to install it.")
MANAGED_SPEAKER_FIX = ("Downloads are turned off by your organisation. Ask your IT team to "
                       "install or repair it.")


def advice_without_downloads(message):
    """A worker's model error with the Manage downloads advice replaced by the
    IT wording, for when managed policy turns downloads off (#257). Only the
    fixed advice written here, in asr_engines and in speaker_load is replaced;
    the rest of the message is kept as it was. Every speech model gets the
    same wording (#246: Manage downloads offers them all)."""
    for name in (*asr_engines.PARAKEET_MODELS, *asr_engines.WHISPER_MODELS):
        for repair_only in (False, True):
            message = message.replace(fix_speech(name, repair_only), MANAGED_SPEECH_FIX)
    message = message.replace(asr_engines.PARAKEET_FIX, MANAGED_SPEECH_FIX)
    return message.replace(SPEAKER_FIX, MANAGED_SPEAKER_FIX)


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
            raise ModelLoadError(f"{asr_engines.label(name)} is not ready. {fix_speech(name)} "
                                 f"({detail})")
        # No decoder: the worker passes decoded arrays. faster_whisper's
        # decoder would load faster_whisper, and CTranslate2 with it, into
        # every Parakeet worker (R12, #133).
        return asr_engines.ParakeetEngine(name, cache_dir, threads).load()

    if asr_engines.is_whisper(name):
        spec = asr_engines.WHISPER_MODELS[name]
        folder = asr_engines.find_verified(spec, cache_dir)
        if folder is None:
            raise ModelLoadError(f"{asr_engines.label(name)} is not installed or failed "
                                 f"verification. {fix_speech(name)}")
        from faster_whisper import WhisperModel
        model = WhisperModel(str(folder), device="cpu", compute_type="int8",
                             cpu_threads=threads)
        problem = asr_engines.feature_width_problem(model, spec)
        if problem:
            raise ModelLoadError(f"{problem}. {fix_speech(name, repair_only=True)}")
        # Word times are required: chunk ownership is decided per word.
        return asr_engines.WhisperEngine(model, name, "int8", "en")

    raise ModelLoadError(f"Unknown model: {asr_engines.label(name)}")


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
                             f"verification. {fix_speech(name)}")
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


def diarize_file(model, cache_dir, audio_path, hint, threads, device="cpu", allow_test=False):
    """Speaker separation over a meeting's decoded audio file (16 kHz float32).
    Returns (turns, exclusive_turns, device); turns are (start, end, label).
    Offline only: builds pyannote from the hash-checked bytes in memory."""
    import numpy as np
    mm = np.memmap(audio_path, dtype="<f4", mode="r")
    audio = np.array(mm)            # pyannote needs the whole waveform anyway
    mm._mmap.close()                # release the file handle now (cleanup, #17)
    del mm
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
        raise ModelLoadError("The speaker identification model is not installed or failed "
                             f"verification. {SPEAKER_FIX}")
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
