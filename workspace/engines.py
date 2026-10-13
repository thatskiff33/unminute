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


SPEAKER_MODEL = "pyannote"


def diarize_file(model, cache_dir, audio_path, hint, threads):
    """Speaker separation over a meeting's decoded audio file (16 kHz float32).
    Returns (turns, exclusive_turns, device); turns are (start, end, label).
    Offline only: loads the hash-verified pyannote folder by path."""
    import numpy as np
    mm = np.memmap(audio_path, dtype="<f4", mode="r")
    audio = np.array(mm)            # pyannote needs the whole waveform anyway
    mm._mmap.close()                # release the file handle now (cleanup, #17)
    del mm
    if model.startswith(TEST_PREFIX):
        module, func = model[len(TEST_PREFIX):].split(":")
        return getattr(importlib.import_module(module), func)(audio, hint)

    spec = asr_engines.SPEAKER_MODEL
    folder = asr_engines.find_verified(spec, cache_dir)
    if folder is None:
        raise ModelLoadError("The speaker identification model is not installed or failed "
                             "verification. Repair it in Setup.")
    import torch
    torch.set_num_threads(threads)
    from pyannote.audio import Pipeline
    pipeline = Pipeline.from_pretrained(str(folder))
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
    return turns, excl, "cpu"          # pyannote runs in float32 on the CPU here
