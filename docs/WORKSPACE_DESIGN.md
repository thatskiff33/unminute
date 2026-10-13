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

**Ownership rule:** the engine runs on the decode range. A segment is kept only if its start time lies in the owned range, and the first chunk also owns any segment starting before 0. Context therefore never duplicates text. Repeated phrases are never deduplicated by text, only by time ownership.

## Atomic chunk completion

Committing a chunk is one transaction: insert its segments, then mark the chunk `done` with the backend, precision and attempt number. Effects of an interruption:

- **Before the commit:** the chunk stays `pending`. On resume it is processed again from scratch, and nothing partial exists to duplicate.
- **After the commit:** the chunk is never processed again.
- **Disk full or any database error during the commit:** the transaction rolls back, the chunk stays `pending`, and the run stops with an error state rather than continuing past it.

Progress is reported only from committed chunks, so a pause or crash never claims work that was not saved.

## Edits

Generated segments live in `segments` and are written only by chunk commits. Human edits live in `edits` (segment id, text, time) and are read on top of the generated text. Nothing that writes segments touches `edits`. A new version starts without edits, and the old version's edits stay with it.

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
