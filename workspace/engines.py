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


def load_engine(name, cache_dir, threads):
    """Return a loaded engine with transcribe(waveform) -> (segments, duration)."""
    if name.startswith(TEST_PREFIX):
        module, func = name[len(TEST_PREFIX):].split(":")
        return getattr(importlib.import_module(module), func)(threads)

    if asr_engines.is_parakeet(name):
        state, detail = asr_engines.parakeet_status(name, cache_dir)
        if state != "ready":
            raise ModelLoadError(f"{name} is not ready ({detail}). Download or repair it "
                                 f"in Setup.")
        from faster_whisper.audio import decode_audio
        return asr_engines.ParakeetEngine(name, cache_dir, threads, decode_audio).load()

    if asr_engines.is_whisper(name):
        spec = asr_engines.WHISPER_MODELS[name]
        folder = asr_engines.find_verified(spec, cache_dir)
        if folder is None:
            raise ModelLoadError(f"{name} is not installed or failed verification. "
                                 f"Download or repair it in Setup.")
        from faster_whisper import WhisperModel
        model = WhisperModel(str(folder), device="cpu", compute_type="int8",
                             cpu_threads=threads)
        problem = asr_engines.feature_width_problem(model, spec)
        if problem:
            raise ModelLoadError(f"{problem}. Repair {name} in Setup.")
        # Word times are required: chunk ownership is decided per word.
        return asr_engines.WhisperEngine(model, name, "int8", "en", word_timestamps=True)

    raise ModelLoadError(f"Unknown model: {name}")
