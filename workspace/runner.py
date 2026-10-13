# workspace/runner.py
# Transcribe the pending chunks of a version, committing each atomically (#14).
#
# Recovery contract (docs/WORKSPACE_DESIGN.md):
#   - only `pending` chunks are processed; `done` chunks are never re-run;
#   - a chunk's segments and its completion are committed together, so an
#     interruption before the commit leaves nothing partial, and after the
#     commit the chunk is finished;
#   - an explicit pause is persisted on the meeting and honoured between
#     chunks; reported progress comes from committed chunks only;
#   - a database error (e.g. disk full) rolls the chunk back and stops the run.
#
# The engine is anything with transcribe(waveform) -> (segments, duration)
# (asr_engines.WhisperEngine / ParakeetEngine). #15 runs it in a worker process.

from dataclasses import dataclass

from workspace import chunking
from workspace.store import StoreError

SAMPLE_RATE = 16000


@dataclass(frozen=True)
class Seg:
    start: float
    end: float
    text: str


def _note_error(store, mid, message):
    """Best effort: if the disk is full this can fail too; the chunk is still pending."""
    try:
        store.set_phase(mid, "error", message)
    except StoreError:
        pass


class RunResult:
    DONE, PAUSED, FAILED, STORE_ERROR = "done", "paused", "failed", "store_error"


def run_version(store, mid, vid, reader, engine, *, backend="cpu", precision="int8",
                on_progress=None, hooks=None):
    """Process pending chunks until done, paused or failed. Returns a RunResult."""
    hooks = hooks or {}
    total = store.source(store.version(vid)["source_id"])["samples"]
    store.set_phase(mid, "transcribing")
    for chunk in store.chunks(vid, status="pending"):
        if store.meeting(mid)["paused"]:
            return RunResult.PAUSED
        idx = chunk["idx"]
        try:
            store.note_attempt(vid, idx, backend, precision)
            audio = reader.read(chunk["ctx_start"], chunk["ctx_end"])
            segments, _ = engine.transcribe(audio)
            offset = chunk["ctx_start"] / SAMPLE_RATE
            absolute = [Seg(s.start + offset, s.end + offset, s.text) for s in segments
                        if s.text.strip()]
            mine = chunking.owned(absolute, chunk["start_sample"], chunk["end_sample"], total)
            if "before_commit" in hooks:
                hooks["before_commit"](idx)
            store.commit_chunk(vid, idx, mine)
        except StoreError as exc:
            _note_error(store, mid, f"Could not save progress: {exc}")
            return RunResult.STORE_ERROR
        except Exception as exc:
            if hooks.get("reraise"):
                raise
            _note_error(store, mid, f"Transcription failed: {type(exc).__name__}")
            return RunResult.FAILED
        if "after_commit" in hooks:
            hooks["after_commit"](idx)
        if on_progress:
            on_progress(*store.progress(vid))
    store.set_phase(mid, "transcribed")
    return RunResult.DONE
