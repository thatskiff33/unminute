# tools/gpu_trial.py
# #9/#12 GPU trial: the same audio on CPU and on an NVIDIA GPU for each
# engine, reporting speed, agreement between CPU and GPU output, and peak GPU
# memory. Measurements only: nothing here enables GPU use in the app.
#
#   python tools/gpu_trial.py <model cache> <recording> [seconds]
#
# Needs a test environment with onnxruntime-gpu (CUDA + cuDNN) and a CUDA
# build of torch; never install those into the app's environment.

import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import asr_engines  # noqa: E402
from compare_engines import edit_distance, normalise  # noqa: E402

SR = 16000


def add_cuda_dll_dirs():
    """pip-installed NVIDIA libraries are not on the DLL search path."""
    import os
    import site
    for sp in site.getsitepackages():
        root = Path(sp) / "nvidia"
        if root.is_dir():
            for d in {p.parent for p in root.rglob("*.dll")}:
                os.add_dll_directory(str(d))
                os.environ["PATH"] = str(d) + os.pathsep + os.environ["PATH"]


class VramPeak:
    """Peak GPU memory in use (MiB), sampled from nvidia-smi."""

    def __init__(self):
        self.peak, self._stop = 0, threading.Event()

    def _run(self):
        while not self._stop.is_set():
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                                      "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=5).stdout
                self.peak = max(self.peak, int(out.split()[0]))
            except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                pass
            self._stop.wait(0.5)

    def __enter__(self):
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()


def idle_vram():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True)
    return int(out.stdout.split()[0])


def parakeet(cache, audio, providers):
    try:
        # ONNX Runtime's documented way to share torch's CUDA/cuDNN libraries:
        # load torch first, so only one cuDNN is in the process.
        import torch  # noqa: F401
    except ImportError:
        pass
    import onnx_asr
    spec = asr_engines.PARAKEET_MODELS[asr_engines.RECOMMENDED_PARAKEET]
    asr = onnx_asr.load_model(spec.onnx_asr_name, asr_engines.model_dir(spec, cache),
                              quantization=spec.quantization, providers=providers)
    vad = onnx_asr.load_vad(asr_engines.SILERO_VAD.onnx_asr_name,
                            asr_engines.model_dir(asr_engines.SILERO_VAD, cache),
                            providers=["CPUExecutionProvider"])
    model = asr.with_vad(vad, batch_size=1, max_speech_duration_s=20)
    # Report what ONNX Runtime really used (it falls back to CPU silently).
    import onnxruntime as rt
    enc = next(asr_engines.model_dir(spec, cache).glob("encoder*.onnx"))
    used = rt.InferenceSession(str(enc), providers=providers).get_providers()
    print(f"  Parakeet {providers[0]}: session providers {used}")
    t = time.perf_counter()
    text = " ".join(seg.text for seg in model.recognize(audio, sample_rate=SR))
    return text, time.perf_counter() - t


def whisper(cache, audio, device):
    from faster_whisper import WhisperModel
    spec = asr_engines.WHISPER_MODELS["large-v3-turbo"]
    folder = asr_engines.find_verified(spec, cache)
    model = WhisperModel(str(folder), device=device,
                         compute_type="float16" if device == "cuda" else "int8")
    t = time.perf_counter()
    segs, _ = model.transcribe(audio, language="en")
    text = " ".join(s.text for s in segs)
    return text, time.perf_counter() - t


def pyannote(cache, audio, device):
    import torch
    from pyannote.audio import Pipeline
    folder = asr_engines.find_verified(asr_engines.SPEAKER_MODEL, cache, full=True)
    pipe = Pipeline.from_pretrained(str(folder)).to(torch.device(device))
    t = time.perf_counter()
    out = pipe({"waveform": torch.from_numpy(audio).unsqueeze(0), "sample_rate": SR})
    ann = getattr(out, "speaker_diarization", out)
    turns = [(round(s.start, 1), round(s.end, 1), lbl)
             for s, _, lbl in ann.itertracks(yield_label=True)]
    return turns, time.perf_counter() - t


def main():
    cache, wav = sys.argv[1], sys.argv[2]
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 300
    # Which engines to run: the GPU builds of ONNX Runtime/torch and of
    # CTranslate2 need different CUDA/cuDNN versions, so they may need
    # separate environments.
    engines = set((sys.argv[4] if len(sys.argv) > 4 else "parakeet,whisper,pyannote").split(","))
    add_cuda_dll_dirs()
    import offline_guard
    offline_guard.disable_telemetry()
    offline_guard.install()
    from faster_whisper.audio import decode_audio
    audio = decode_audio(wav, sampling_rate=SR)[: int(seconds * SR)].astype("float32")
    print(f"audio {len(audio) / SR:.0f} s; GPU memory in use before: {idle_vram()} MiB")

    rows = []
    candidates = (("parakeet", "Parakeet v2", lambda d: parakeet(
                      cache, audio, ["CUDAExecutionProvider"] if d == "cuda"
                      else ["CPUExecutionProvider"])),
                  ("whisper", "Whisper large-v3 turbo", lambda d: whisper(cache, audio, d)))
    for key, name, run in candidates:
        if key not in engines:
            continue
        cpu_text, cpu_s = run("cpu")
        with VramPeak() as v:
            gpu_text, gpu_s = run("cuda")
        words = normalise(cpu_text)
        rows.append((name, cpu_s, gpu_s, v.peak,
                     f"{edit_distance(words, normalise(gpu_text))} word edits of {len(words)}"))
    if "pyannote" in engines:
        cpu_turns, cpu_s = pyannote(cache, audio, "cpu")
        with VramPeak() as v:
            gpu_turns, gpu_s = pyannote(cache, audio, "cuda")
        rows.append(("pyannote community-1", cpu_s, gpu_s, v.peak,
                 f"{len({t[2] for t in cpu_turns})} vs {len({t[2] for t in gpu_turns})} speakers, "
                     f"{len(cpu_turns)} vs {len(gpu_turns)} turns"))
    print("| Engine | CPU s | GPU s | Speed-up | Peak GPU MiB | CPU vs GPU output |")
    print("| --- | --- | --- | --- | --- | --- |")
    for name, c, g, peak, agree in rows:
        print(f"| {name} | {c:.0f} | {g:.0f} | {c / g:.1f}x | {peak} | {agree} |")
    print("blocked network attempts:", len(offline_guard.blocked))


if __name__ == "__main__":
    main()
