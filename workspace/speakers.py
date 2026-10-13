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
    DONE, FAILED, STOPPED, NO_AUDIO, NOT_TRANSCRIBED, BAD_HINT = (
        "done", "failed", "stopped", "no_audio", "not_transcribed", "bad_hint")


def run_speakers(store, mid, vid, handle, audio_path, *, threads, hint=None, model="pyannote"):
    """Separate speakers for an already-transcribed version. Returns a SpeakerResult."""
    if hint is not None and (isinstance(hint, bool) or not isinstance(hint, int)
                             or not 1 <= hint <= MAX_HINT):
        return SpeakerResult.BAD_HINT
    if not Path(audio_path).is_file():
        return SpeakerResult.NO_AUDIO       # cleaned up (#17): nothing to analyse
    if store.chunks(vid, status="pending"):
        return SpeakerResult.NOT_TRANSCRIBED
    handle.begin_run()               # a new run: clears any earlier Stop
    rid = store.start_speaker_run(vid, hint)
    try:
        turns, exclusive, device, _ = worker_diarize(handle, model, audio_path, hint, threads)
    except WorkerStopped:
        _end(store, rid, "interrupted", RESTART_NOTE)
        return SpeakerResult.STOPPED
    except Exception as exc:
        from workspace.worker import safe_message
        _end(store, rid, "failed", f"{type(exc).__name__}: {safe_message(exc)}"[:300])
        return SpeakerResult.FAILED
    try:
        if not store.finish_speaker_run(rid, turns, exclusive, backend=device):
            return SpeakerResult.STOPPED    # superseded by a newer run meanwhile
    except StoreError as exc:
        _end(store, rid, "failed", f"Could not save speakers: {exc}"[:300])
        return SpeakerResult.FAILED
    return SpeakerResult.DONE


def _end(store, rid, status, message):
    try:
        store.end_speaker_run(rid, status, message)
    except StoreError:
        pass
