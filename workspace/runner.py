# workspace/runner.py
# Transcribe the pending chunks of a version, committing each atomically (#14).
#
# Recovery contract (docs/WORKSPACE_DESIGN.md):
#   - only `pending` chunks are processed; `done` chunks are never re-run;
#   - a chunk's segments and its completion are committed together, so an
#     interruption before the commit leaves nothing partial, and after the
#     commit the chunk is finished;
#   - each attempt is recorded with the backend/precision the engine reports;
#   - an explicit pause is persisted on the meeting and honoured between
#     chunks; reported progress comes from committed chunks only;
#   - the decoded audio is checked against its recorded identity before a
#     run, so changed audio is never transcribed into an existing version;
#   - a database error (e.g. disk full) rolls the chunk back and stops the run.
#
# The engine is anything with transcribe(waveform) -> (segments, duration)
# (asr_engines.WhisperEngine / ParakeetEngine). #15 runs it in a worker process.

from asr_engines import Segment
from workspace import chunking
from workspace.store import StoreError

SAMPLE_RATE = 16000


class RunResult:
    DONE, PAUSED, FAILED, STORE_ERROR, SOURCE_CHANGED, SUPERSEDED = (
        "done", "paused", "failed", "store_error", "source_changed", "superseded")


def _note_error(store, mid, message):
    """Best effort: if the disk is full this can fail too; the chunk is still pending."""
    try:
        store.set_phase(mid, "error", message)
    except StoreError:
        pass


def _finish(store, attempt_id, result, engine=None):
    try:
        store.finish_attempt(attempt_id, result, getattr(engine, "device", None),
                             getattr(engine, "precision", None))
    except StoreError:
        pass


def _shift(s, offset):
    words = tuple((a + offset, b + offset, t) for a, b, t in (getattr(s, "words", ()) or ()))
    return Segment(s.start + offset, s.end + offset, s.text, words)


def run_version(store, mid, vid, reader, engine, *, on_progress=None, hooks=None):
    """Process pending chunks until done, paused or failed. Returns a RunResult."""
    try:
        return _run(store, mid, vid, reader, engine, on_progress, hooks or {})
    except StoreError as exc:     # a read failed (e.g. database unavailable)
        _note_error(store, mid, f"Could not read progress: {exc}")
        return RunResult.STORE_ERROR


def _run(store, mid, vid, reader, engine, on_progress, hooks):
    version = store.version(vid)
    source = store.source(version["source_id"])
    total = source["samples"]
    if version["state"] != "active":
        return RunResult.SUPERSEDED
    if not _audio_matches(reader, source):
        _note_error(store, mid, "The meeting audio has changed or is damaged; it was not "
                                "transcribed. Import the recording again.")
        return RunResult.SOURCE_CHANGED
    epoch = store.meeting(mid)["content_epoch"]
    store.set_phase(mid, "transcribing")
    for chunk in store.chunks(vid, status="pending"):
        if store.meeting(mid)["paused"]:
            return RunResult.PAUSED
        idx = chunk["idx"]
        try:
            attempt = store.start_attempt(vid, idx)
        except StoreError as exc:
            _note_error(store, mid, f"Could not save progress: {exc}")
            return RunResult.STORE_ERROR
        try:
            audio = reader.read(chunk["ctx_start"], chunk["ctx_end"])
            segments, _ = engine.transcribe(audio)
            offset = chunk["ctx_start"] / SAMPLE_RATE
            absolute = [_shift(s, offset) for s in segments if s.text.strip()]
            mine = chunking.owned(absolute, chunk["start_sample"], chunk["end_sample"], total)
            if "before_commit" in hooks:
                hooks["before_commit"](idx)
            committed = store.commit_chunk(
                vid, idx, mine, epoch=epoch, attempt_id=attempt,
                backend=getattr(engine, "device", None),
                precision=getattr(engine, "precision", None))
        except StoreError as exc:
            _finish(store, attempt, "failed", engine)
            _note_error(store, mid, f"Could not save progress: {exc}")
            return RunResult.STORE_ERROR
        except Exception as exc:
            _finish(store, attempt, "failed", engine)
            if hooks.get("reraise"):
                raise
            _note_error(store, mid, f"Transcription failed: {type(exc).__name__}: {exc}"[:300])
            return RunResult.FAILED
        if not committed:
            # The version was superseded or its content cleaned while this
            # chunk ran: the result is dropped and must not be retried.
            _finish(store, attempt, "stopped")
            return RunResult.SUPERSEDED
        if "after_commit" in hooks:
            hooks["after_commit"](idx)
        if on_progress:
            on_progress(*store.progress(vid))
    store.set_phase(mid, "transcribed")
    return RunResult.DONE


def _audio_matches(reader, source):
    """Length always; full SHA-256 when the reader is backed by a file."""
    if len(reader) != source["samples"]:
        return False
    path = getattr(reader, "path", None)
    if path is None:
        return True
    from workspace.media import sha256_file
    return sha256_file(path) == source["audio_sha256"]
