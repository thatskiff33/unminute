# workspace/engines.py
# Load a speech engine for a worker, strictly offline (#15).
#
# Inference workers never download: a model that is missing or fails its
# checks raises ModelLoadError, and the setup/download step (#19) - a separate
# process with deliberate network permission - installs or repairs it.
# Engines come from asr_engines and share its verified manifests.

import importlib

import asr_engines
from asr_engines import ModelLoadError

TEST_PREFIX = "test:"      # "test:module:function" loads a test engine factory
# Where a user downloads or repairs a model once Setup is done (#244). A worker
# only loads models after Setup, so its messages always point here.
DOWNLOADS = "Settings > Manage downloads"
FIX_SPEECH = (f"Download or repair it in {DOWNLOADS}, or choose another speech model in "
              "Settings.")


def _test_factory(name, allow_test):
    """The factory named by a "test:module:function" engine. Only tests set
    allow_test: otherwise any stored model name could import any module."""
    if not allow_test:
        raise ModelLoadError("Unknown speech model.")
    module, func = name[len(TEST_PREFIX):].split(":")
    return getattr(importlib.import_module(module), func)


def load_engine(name, cache_dir, threads, allow_test=False):
    """Return a loaded engine with transcribe(waveform) -> (segments, duration)."""
    if name.startswith(TEST_PREFIX):
        return _test_factory(name, allow_test)(threads)

    if asr_engines.is_parakeet(name):
        state, detail = asr_engines.parakeet_status(name, cache_dir)
        if state != "ready":
            raise ModelLoadError(f"{asr_engines.label(name)} is not ready. {FIX_SPEECH} "
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
                                 f"verification. {FIX_SPEECH}")
        from faster_whisper import WhisperModel
        model = WhisperModel(str(folder), device="cpu", compute_type="int8",
                             cpu_threads=threads)
        problem = asr_engines.feature_width_problem(model, spec)
        if problem:
            raise ModelLoadError(f"{problem}. Repair {asr_engines.label(name)} in "
                                 f"{DOWNLOADS}, or choose another speech model in Settings.")
        # Word times are required: chunk ownership is decided per word.
        return asr_engines.WhisperEngine(model, name, "int8", "en")

    raise ModelLoadError(f"Unknown model: {asr_engines.label(name)}")


SPEAKER_MODEL = "pyannote"


class GpuUnavailable(RuntimeError):
    """The CUDA build or a usable NVIDIA card/driver is not available."""


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
                             f"verification. Repair it in {DOWNLOADS}.")
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
