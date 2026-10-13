# Workspace processing design (#14 to #18)

Technical design for the durable core of the meeting workspace. It defines state ownership, chunk identity and the lifecycle that the worker (#15), speaker (#16), cleanup (#17) and hold (#18) issues build on. Product behaviour is in [WORKSPACE_PLAN.md](WORKSPACE_PLAN.md); this document does not change it.

## Ownership

- One state-owning process opens the database: `%LOCALAPPDATA%\whisper\workspace\workspace.db`. That is per user, never synced, and in the same family of locations as the model cache. Only the owner writes. Workers (#15) send results back and never touch the database.
- Meeting media lives in `%LOCALAPPDATA%\whisper\workspace\media\<meeting id>\`. The meeting id is random; no names or paths from the source appear in any app path.
- SQLite runs in WAL mode with `synchronous=FULL` and foreign keys on. Each state change that must be atomic is one transaction.

## Source identity

On import, the original file is hashed (SHA-256) and decoded once to 16 kHz mono float32, stored as `audio.f32` (raw little-endian). Engines read slices through a memory map, so memory stays bounded however long the meeting is.

Each source row records:

- the original file's SHA-256, size and modification time;
- the decoded audio's SHA-256, sample count and sample rate;
- the original path, used only for #24 deletion authority, never for processing.

## Working versions

A version is the combination of source identity, engine, model name, model revision, precision and a hash of the engine settings. Changing any of these creates a new version. The previous version and its edits are kept, marked superseded, and never overwritten. Exactly one version per meeting is active.

## Chunks

Chunk ranges are planned once per version and persisted as exact sample ranges:

| Field | Meaning |
| --- | --- |
| `start`, `end` | Owned range. Chunks tile the recording exactly, with no gaps or overlaps. |
| `ctx_start`, `ctx_end` | Decode range: the owned range plus up to 2 s of context each side, so words near a boundary are recognised with surrounding audio. |

Boundaries aim for about 45 s of owned audio. Each sits at the quietest 30 ms window within ±5 s of the nominal point, so a boundary normally falls in a pause rather than mid-word.

**Ownership rule:** the engine runs on the decode range, and ownership is decided per word, not per segment. Real engines return sentence-length segments, and a sentence that starts in a chunk's owned range can run past its decode range. Owning whole segments by their start time would drop that sentence's tail, because the next chunk would discard the sentence as starting before its range. An independent review reproduced exactly that.

So each word belongs to the chunk whose owned range contains the word's start time. A sentence crossing a boundary is split there, and each chunk keeps only its own words. Words cut by the edge of a decode range lie outside the owned range, so they are dropped. The first chunk also owns anything starting before 0, and the last chunk anything after the end.

Both engines provide word times: Parakeet from token timestamps, and Whisper with `word_timestamps` enabled in the workspace. A segment without word times is accepted only if it is short enough that the context guarantees it is complete; a longer one stops the run rather than being silently truncated.

Repeated phrases are never deduplicated by text, only by time ownership. Word times are stored with each segment for audio-linked review (#20).

## Atomic chunk completion

Committing a chunk is one transaction: insert its segments and mark the chunk `done`.

Every attempt gets its own row in `attempts`, with the backend and precision the engine reported and the result (`committed`, `failed` or `stopped`). Failures count against the chunk; deliberate pauses and stops do not.

The version's `precision` is the requested output contract. If the actual precision would change the output contract, that is a different working version, never a silent fallback inside the same one.

A commit is refused if:

- the chunk is already done;
- the version is no longer active; or
- the meeting's `content_epoch` has changed since the run started. Cleanup (#17) bumps it.

A late result from a stale worker therefore cannot recreate superseded or cleaned content. Effects of an interruption:

- **Before the commit:** the chunk stays `pending`. On resume it is processed again from scratch, and nothing partial exists to duplicate.
- **After the commit:** the chunk is never processed again.
- **Disk full or any database error during the commit:** the transaction rolls back, the chunk stays `pending`, and the run stops with an error state rather than continuing past it.

Progress is reported only from committed chunks, so a pause or crash never claims work that was not saved.

## Source checks

Before each run, the decoded audio's length, and its SHA-256 when it is file-backed, are checked against the source record. Changed or damaged audio is never transcribed into an existing version; re-importing creates a new source and version.

A failed import removes its own half-created meeting and media folder. An original that changes while it is being hashed and decoded is refused.

## Edits

Generated segments live in `segments` and are written only by chunk commits. Human edits live in `edits` (segment id, text, time) and are read on top of the generated text. Nothing that writes segments touches `edits`.

A new version starts without edits, and the old version's edits stay with it. Carrying edits forward to a new version, by aligning text over time, is a policy for #20 to decide; it is not automatic.

## Threads

The UI bridge and the processing thread share one `Store` object. Every operation holds one lock, because a single SQLite connection cannot nest transactions and readers must not see another thread's uncommitted writes. Workers (#15) run in other processes and never open the database.

## Run state

Meeting `phase`: `new`, then `transcribing`, then `transcribed`, then (#16) speakers.

`paused` is a separate, persisted flag set only by an explicit user pause, so it survives restart. A run checks it between chunks and stops at the next safe boundary. Pause is acknowledged immediately; reported progress stays truthful because it counts committed chunks only.

## Lifecycle tables reserved for later issues

| Issue | Tables |
| --- | --- |
| #16 | `speaker_runs`, `speaker_turns`. Machine attribution, kept separate from user corrections. |
| #17 | `cleanup_jobs`. Intent is persisted before removal and retried safely. Source identity is re-checked before deleting originals. Workers are quiesced first. A per-meeting `content_epoch` lets late worker results be detected and discarded so they cannot recreate cleaned content. |
| #18 | `holds` (reason, created, reviewed, released) and `receipts` (random job id, timestamps and result only, expiring 30 days after success). |

`schema_version` records migrations. #26 gates schema-changing updates on an empty workspace.
