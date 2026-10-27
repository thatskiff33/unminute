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
from workspace.worker import GPU_FAILURE_WORDS as _GPU_FAILURE_WORDS
from workspace.worker import WorkerStopped, gpu_failure
from workspace.worker import diarize as worker_diarize

MAX_HINT = 20
# The meeting's audio did not open for a speaker pass (#111 C3a): its header,
# or its audio key. Fixed text (P3); only that pass fails (D4).
AUDIO_UNREADABLE = ("Speakers were not identified: the meeting audio could not be read. "
                    "It may be damaged. The transcript is unaffected.")


class SpeakerResult:
    DONE, FAILED, STOPPED, NO_AUDIO, NOT_TRANSCRIBED, BAD_HINT, SKIPPED = (
        "done", "failed", "stopped", "no_audio", "not_transcribed", "bad_hint", "skipped")


def run_speakers(store, mid, vid, handle, audio, *, threads, hint=None, model="pyannote",
                 device="cpu", gpu_site=None, skips_seen=None, downloads_allowed=True,
                 only_it_models=False):
    """Separate speakers for an already-transcribed version. Returns a SpeakerResult.
    audio: the meeting's media.AudioRef (the encrypted file, its key and
    context; the key reaches the worker only in the diarize request).
    skips_seen: the version's skip count when this pass was chosen; a skip
    since then means nothing starts (SKIPPED). downloads_allowed=False
    (managed policy, read by the caller) gives a speaker model that is not
    ready the IT wording instead of Manage downloads (#257); only_it_models (a
    machine install using only IT's models) words it so (#451)."""
    if hint is not None and (isinstance(hint, bool) or not isinstance(hint, int)
                             or not 1 <= hint <= MAX_HINT):
        return SpeakerResult.BAD_HINT
    if audio is None or not Path(audio.path).is_file():
        return SpeakerResult.NO_AUDIO       # cleaned up (#17): nothing to analyse
    if store.chunks(vid, status="pending"):
        return SpeakerResult.NOT_TRANSCRIBED
    handle.begin_run()               # a new run: clears any earlier Stop
    rid = store.start_speaker_run(vid, hint, skips_seen=skips_seen)
    if rid is None:
        return SpeakerResult.SKIPPED      # the user skipped after this pass was chosen
    try:
        turns, exclusive, device, _ = _diarize(handle, model, audio, hint, threads,
                                               device, gpu_site)
    except WorkerStopped:
        _end(store, rid, "interrupted", RESTART_NOTE)
        return SpeakerResult.STOPPED
    except Exception as exc:
        from workspace.worker import safe_message
        why = f"{type(exc).__name__}: {safe_message(exc)}"
        # IT's copy found damaged: Downloads shows it so (#451).
        from workspace.asr_engines import note_machine_damage
        note_machine_damage(why)
        if not downloads_allowed:
            from workspace.engines import advice_without_downloads
            why = advice_without_downloads(why, only_it_models)
        _end(store, rid, "failed", why[:300])
        return SpeakerResult.FAILED
    try:
        if not store.finish_speaker_run(rid, turns, exclusive, backend=device,
                                        precision="float32"):
            return SpeakerResult.STOPPED    # superseded by a newer run meanwhile
    except StoreError as exc:
        _end(store, rid, "failed", f"Could not save speakers: {exc}"[:300])
        return SpeakerResult.FAILED
    return SpeakerResult.DONE


def record_failure(store, vid, why, *, hint=None, skips_seen=None):
    """A speaker pass whose audio did not open before it started: recorded as
    a failed run, which waits for the user's Identify again like any failed
    pass. The transcript and the meeting's phase are untouched (D4), as when
    a block does not open during the pass. Returns a SpeakerResult."""
    rid = store.start_speaker_run(vid, hint, skips_seen=skips_seen)
    if rid is None:
        return SpeakerResult.SKIPPED      # the user skipped after this pass was chosen
    _end(store, rid, "failed", why[:300])
    return SpeakerResult.FAILED


def _diarize(handle, model, audio, hint, threads, device, gpu_site):
    """GPU first when asked; any GPU failure (driver, missing libraries, out of
    graphics memory, a crashed GPU worker) falls back to the CPU for this pass,
    and the result says so. A deliberate stop is never retried."""
    if device != "cuda":
        # "cpu", or "cpu (graphics card not used: why)" when the card was
        # wanted but refused before the pass (#153): recorded as it is.
        turns, exclusive, ran, had = worker_diarize(handle, model, audio, hint, threads)
        return turns, exclusive, device if device.startswith("cpu (") else ran, had
    from workspace.worker import WorkerCrashed, WorkerError, safe_message
    try:
        return worker_diarize(handle, model, audio, hint, threads, device="cuda",
                              gpu_site=gpu_site)
    except WorkerStopped:
        raise
    except (WorkerError, WorkerCrashed) as exc:
        if not _gpu_failure(exc):
            raise                      # not the graphics card's fault: no second run
        reason = safe_message(exc).split(": ", 1)[-1][:120]
        handle.release_gpu()           # stop the failed GPU worker and free its memory
    try:
        turns, exclusive, _, had = worker_diarize(handle, model, audio, hint, threads)
    except WorkerStopped:
        raise
    except Exception as exc:
        raise type(exc)(f"{exc} (after the graphics card could not be used: {reason})") \
            from exc
    return turns, exclusive, f"cpu (graphics card not used: {reason})"[:200], had


# One definition for speakers and for Whisper on the graphics card (#248).
GPU_FAILURE_WORDS = _GPU_FAILURE_WORDS
_gpu_failure = gpu_failure


def _end(store, rid, status, message):
    try:
        store.end_speaker_run(rid, status, message)
    except StoreError:
        pass
