# workspace/speakers.py
# Speaker separation after transcription (#16).
#
# - Runs only after transcription is committed, as one whole-meeting pass in
#   the worker; the worker releases the speech model before loading pyannote
#   (one heavy model at a time).
# - Never touches committed text: it writes speaker runs, turns and machine
#   attribution only. If it fails or is interrupted, the transcript stays
#   usable without speaker labels.
# - An interrupted pass is NOT resumable: it restarts from the beginning of
#   the meeting next time (pyannote clusters the whole recording at once).
# - A known-count rerun ("3 people spoke") creates a new run; only the speaker
#   phase changes, and only while the meeting audio still exists.

from pathlib import Path

from workspace.store import SPEAKER_RESTART_NOTE as RESTART_NOTE
from workspace.store import StoreError
from workspace.worker import WorkerStopped
from workspace.worker import diarize as worker_diarize

MAX_HINT = 20


class SpeakerResult:
    DONE, FAILED, STOPPED, NO_AUDIO, NOT_TRANSCRIBED, BAD_HINT, SKIPPED = (
        "done", "failed", "stopped", "no_audio", "not_transcribed", "bad_hint", "skipped")


def run_speakers(store, mid, vid, handle, audio_path, *, threads, hint=None, model="pyannote",
                 device="cpu", gpu_site=None, skips_seen=None):
    """Separate speakers for an already-transcribed version. Returns a SpeakerResult.
    skips_seen: the version's skip count when this pass was chosen; a skip
    since then means nothing starts (SKIPPED)."""
    if hint is not None and (isinstance(hint, bool) or not isinstance(hint, int)
                             or not 1 <= hint <= MAX_HINT):
        return SpeakerResult.BAD_HINT
    if not Path(audio_path).is_file():
        return SpeakerResult.NO_AUDIO       # cleaned up (#17): nothing to analyse
    if store.chunks(vid, status="pending"):
        return SpeakerResult.NOT_TRANSCRIBED
    handle.begin_run()               # a new run: clears any earlier Stop
    rid = store.start_speaker_run(vid, hint, skips_seen=skips_seen)
    if rid is None:
        return SpeakerResult.SKIPPED      # the user skipped after this pass was chosen
    try:
        turns, exclusive, device, _ = _diarize(handle, model, audio_path, hint, threads,
                                               device, gpu_site)
    except WorkerStopped:
        _end(store, rid, "interrupted", RESTART_NOTE)
        return SpeakerResult.STOPPED
    except Exception as exc:
        from workspace.worker import safe_message
        _end(store, rid, "failed", f"{type(exc).__name__}: {safe_message(exc)}"[:300])
        return SpeakerResult.FAILED
    try:
        if not store.finish_speaker_run(rid, turns, exclusive, backend=device,
                                        precision="float32"):
            return SpeakerResult.STOPPED    # superseded by a newer run meanwhile
    except StoreError as exc:
        _end(store, rid, "failed", f"Could not save speakers: {exc}"[:300])
        return SpeakerResult.FAILED
    return SpeakerResult.DONE


def _diarize(handle, model, audio_path, hint, threads, device, gpu_site):
    """GPU first when asked; any GPU failure (driver, missing libraries, out of
    graphics memory, a crashed GPU worker) falls back to the CPU for this pass,
    and the result says so. A deliberate stop is never retried."""
    if device != "cuda":
        # "cpu", or "cpu (graphics card not used: why)" when the card was
        # wanted but refused before the pass (#153): recorded as it is.
        turns, exclusive, ran, had = worker_diarize(handle, model, audio_path, hint, threads)
        return turns, exclusive, device if device.startswith("cpu (") else ran, had
    from workspace.worker import WorkerCrashed, WorkerError, safe_message
    try:
        return worker_diarize(handle, model, audio_path, hint, threads, device="cuda",
                              gpu_site=gpu_site)
    except WorkerStopped:
        raise
    except (WorkerError, WorkerCrashed) as exc:
        if not _gpu_failure(exc):
            raise                      # not the graphics card's fault: no second run
        reason = safe_message(exc).split(": ", 1)[-1][:120]
        handle.release_gpu()           # stop the failed GPU worker and free its memory
    try:
        turns, exclusive, _, had = worker_diarize(handle, model, audio_path, hint, threads)
    except WorkerStopped:
        raise
    except Exception as exc:
        raise type(exc)(f"{exc} (after the graphics card could not be used: {reason})") \
            from exc
    return turns, exclusive, f"cpu (graphics card not used: {reason})"[:200], had


GPU_FAILURE_WORDS = ("gpuunavailable", "cuda", "cudnn", "cublas", "out of memory",
                     "outofmemory")


def _gpu_failure(exc):
    """A failure the graphics card caused: the GPU worker crashed or hung, or
    the error names CUDA, cuDNN, cuBLAS or graphics memory."""
    from workspace.worker import WorkerCrashed
    return isinstance(exc, WorkerCrashed) or any(
        w in str(exc).lower() for w in GPU_FAILURE_WORDS)


def _end(store, rid, status, message):
    try:
        store.end_speaker_run(rid, status, message)
    except StoreError:
        pass
