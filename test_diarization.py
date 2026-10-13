# test_diarization.py
# End-to-end check: pyannote speaker diarisation + faster-whisper transcription,
# fully local. Audio is decoded in-memory with PyAV so no FFmpeg is needed.
# Usage: python test_diarization.py "path\to\file.wav"

import os
import sys
import warnings

warnings.filterwarnings("ignore")  # hide the harmless torchcodec/symlink warnings

CACHE_DIR = os.path.join(os.environ["LOCALAPPDATA"], "whisper-models")
# Send pyannote/HF downloads to the same non-synced cache faster-whisper uses.
os.environ.setdefault("HF_HUB_CACHE", CACHE_DIR)
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", CACHE_DIR)

# No usage statistics (pyannote sends OpenTelemetry metrics by default). This
# script may download models, so it does not install the network guard.
import offline_guard

offline_guard.disable_telemetry()

# Token comes from the user environment (setx HF_TOKEN ...). Fall back to the
# registry in case this process started before the variable propagated.
if not os.environ.get("HF_TOKEN"):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            os.environ["HF_TOKEN"] = winreg.QueryValueEx(key, "HF_TOKEN")[0]
    except OSError:
        pass

import torch
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio
from pyannote.audio import Pipeline

audio_path = sys.argv[1]

print("Loading diarisation pipeline (first run downloads weights)...")
pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1")
print("Pipeline loaded OK")

# Decode with PyAV (bundled with faster-whisper); avoids torchcodec/FFmpeg.
wav = decode_audio(audio_path, sampling_rate=16000)
waveform = torch.from_numpy(wav).unsqueeze(0)
print(f"Audio decoded: {wav.shape[0] / 16000:.1f}s")

print("Running diarisation...")
output = pipeline({"waveform": waveform, "sample_rate": 16000})
# pyannote 4.x wraps the annotation in a DiarizeOutput; 3.x returned it directly.
diarization = getattr(output, "speaker_diarization", output)
if not hasattr(diarization, "itertracks"):
    print("Unexpected output type:", type(output), dir(output))
    sys.exit(1)
turns = [(t.start, t.end, lbl) for t, _, lbl in diarization.itertracks(yield_label=True)]
print(f"Speaker turns found: {len(turns)}")
for s, e, lbl in turns:
    print(f"  {s:6.1f}s - {e:6.1f}s  {lbl}")

print("\nTranscribing with large-v3-turbo...")
model = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8",
                     download_root=CACHE_DIR)
segments, _ = model.transcribe(audio_path, language="en", vad_filter=True, beam_size=5)


def speaker_for(start, end):
    """Pick the diarisation label with the largest time overlap for a segment."""
    best, best_overlap = None, 0.0
    for s, e, lbl in turns:
        overlap = min(end, e) - max(start, s)
        if overlap > best_overlap:
            best, best_overlap = lbl, overlap
    return best


# Number speakers in order of first appearance: SPEAKER_00 -> Speaker 1, etc.
names = {}
print("\nLabelled transcript:")
for seg in segments:
    lbl = speaker_for(seg.start, seg.end)
    if lbl is not None and lbl not in names:
        names[lbl] = f"Speaker {len(names) + 1}"
    who = names.get(lbl, "Unknown")
    print(f"  {who}: {seg.text.strip()}")

print("\nEND-TO-END TEST OK")
