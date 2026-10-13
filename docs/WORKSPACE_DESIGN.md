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

Both engines provide word times: Parakeet from token timestamps, and Whisper with `word_timestamps` enabled in the workspace. faster-whisper can occasionally return a segment without word times. Such a segment gets estimated word times, spread in proportion to word length, so it is still divided at a boundary rather than lost or failing the meeting. Parakeet words are assigned to sentences by token position, not by time, because TDT can give several tokens the same timestamp.

Residual risk: when there is no pause within ±5 s of a boundary, the two neighbouring decodes may time a word on either side of it differently, so one word at that boundary can be duplicated or dropped. Cutting in pauses makes this rare. The real-engine check found no differences on a 15-minute continuous-speech recording.

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

## Workers and resources (#15)

Heavy inference runs in a separate worker process (`workspace/worker.py`). The state owner keeps the database and the worker never opens it.

- **What the worker does:** it loads one model at a time; loading another replaces the first in the same process. It transcribes the bounded audio it is sent (at most 120 s per request) and returns plain segments with word times.
- **Network and priority:** before loading anything, the worker installs the offline network guard and, in Quiet mode, lowers its own priority to below normal.
- **No downloads:** the worker loads models offline only (`workspace/engines.py`). A missing or unverified model is an error for Setup (#19); downloads happen in a separate step.

Quiet defaults:

- threads are half the logical CPUs, capped at 4 and at least 1;
- below-normal priority;
- the worker exits after 120 s idle, releasing all model memory.

Faster mode uses all but two logical CPUs. Neither mode promises a speed; `tools/worker_check.py` measures them.

Pause and stop:

- **Pause** is persisted immediately and takes effect at the next chunk boundary. Progress stays truthful because it counts only committed chunks.
- **Stop now** terminates the worker at once and discards only the uncommitted chunk. The stop is recorded as an attempt with result `stopped`, not counted as a failure.

Recovery:

- A worker that dies is detected. Its chunk stays pending, and the next request starts a new worker.
- A chunk that fails `MAX_ATTEMPTS` (3) times stops the run with a message instead of being retried forever.

Battery: automatic processing waits while Windows reports battery power, unless the user overrides it (`resources.may_start_automatic_work`). Work the user starts explicitly is not blocked.

## Speakers after transcription (#16)

Speaker separation (`workspace/speakers.py`) runs only after every chunk of the active version is committed. It is one whole-meeting pyannote pass in the worker. The worker releases the speech model first, so only one heavy model is loaded at a time.

- **Text is never touched.** A pass writes `speaker_runs`, `speaker_turns` and `segment_speakers` only. If it fails, the transcript stays usable without labels, and a failed rerun keeps the previous good labels.
- **Two kinds of turns.** Full turns (`exclusive = 0`) keep overlapped speech as evidence. Exclusive turns (`exclusive = 1`) give each passage its main speaker.
- **Overlap flag.** A passage is flagged as overlap when two different people talk at the same time within it for at least 0.3 s. A handover from one speaker to the next is not overlap.
- **Interrupted passes restart.** A pass cannot resume. A stopped, hung (timeout: 3x the audio length, at least 10 minutes) or crashed pass is recorded as `interrupted`, with a note that it starts again from the beginning.
- **Stale results are refused.** A run records the meeting's content epoch when it starts. Its result is refused if the epoch has changed (cleanup) or the version is no longer active.
- **Known count.** A known-count rerun ("3 people spoke") creates a new run. Earlier runs are superseded only when the new one succeeds.

## Cleanup checkpoints (#17)

Cleanup (`workspace/cleanup.py`) follows exactly two explicit confirmations. Copying or exporting never counts as either.

1. **Transcript reviewed.** This needs every chunk committed and no speaker pass running.
   - The reviewed text is frozen in `reviewed_snapshots` first.
   - The app's audio folder is then removed.
   - An intake original is deleted only if all of these hold:
     - the user gave explicit authority (`sources.delete_original`, #24);
     - it is inside an authorised folder;
     - it is a plain local file, not a link, junction, network or device path;
     - its size, modification time and SHA-256 still match the import.
2. **Final deliverable reviewed and saved outside the app.**
   - This removes all transcript working content: segments, edits, edit history, speaker data, attempts, chunks, versions, snapshot, sources, released holds and the meeting row.
   - It then runs `secure_delete`, a WAL checkpoint (TRUNCATE) and `VACUUM`, so no removed text stays readable in the database files.
   - Leftover files are removed last.
   - Transcript cleanup waits until audio cleanup has finished, because it removes the source records that audio cleanup needs.

Each confirmation records a `cleanup_jobs` row before anything is removed, and `resume_pending` retries unfinished jobs at startup.

Before removal:

- the worker is quiesced;
- the meeting's `content_epoch` is bumped, so late chunk or speaker results are refused.

Holds (#18) are checked before an attempt, again in the transaction that bumps the epoch, and again in the transcript-delete transaction. A hold added while the worker is stopping still wins.

Failure reasons are stored without file paths.

## Holds and receipts (#18)

`workspace/holds.py`.

### Holds

- **Placing a hold.** Any user can hold a meeting's audio, its transcript or everything, with a reason. No approval is needed.
- **No expiry.** Holds are stored in the database and never expire.
- **What a hold blocks:**
  - An audio or "everything" hold blocks audio cleanup.
  - Any hold blocks transcript cleanup, because transcript cleanup removes everything that remains.
- **Releasing a hold never deletes anything by itself.**
  - The pending or failed jobs that the hold covered move to `awaiting_confirmation`.
  - They run only after the user confirms again, either with `confirm_pending_cleanup` or by repeating the original confirmation.
  - Job updates never overwrite `awaiting_confirmation`.
- **Review reminders.** `due_for_review` lists holds that have not been reviewed for 30 days. The app shows them while it is open. A closed app sends no reminders.
- **Unfinished work.** `unfinished_work` lists each meeting's age and next action. Ordinary unfinished work gets no scheduled reminders.

### Receipts

- A successful cleanup job is replaced in the same transaction by a receipt.
- A receipt holds only:
  - a fresh random id;
  - the time cleanup was requested and the time it completed;
  - a result such as "audio removed; Original kept: the file changed since it was imported".
- A receipt holds no meeting id, names, file names or hashes.
- `purge_expired_receipts` removes receipts 30 days after completion. The app calls it at startup and periodically while it is open.
- A failed job keeps its row, with a reason that contains no file paths, only until a retry succeeds.

## Workspace window (#20)

`python -m workspace` opens the workspace. It is a development preview until packaging (#11) and setup (#19) land.

### Process and threads

- **One state-owning process.** `workspace/__main__.py` installs the offline guard. It then creates a `Workspace` (`workspace/app.py`), which owns the Store, the media folder and the worker, and opens the WebView2 shell.
- **Processing thread.** One background thread finds work by scanning the database, in this order:
  1. due cleanup jobs;
  2. the oldest unpaused, unfinished transcription;
  3. speaker separation for a transcribed meeting.
- **Restarts.** Because work is found by scanning, a restart resumes exactly where processing stopped. Explicit pauses stay paused.
- **Intake thread.** Imports (hash and decode) run on a separate thread, so adding a recording never waits behind a transcription.
- **Battery.** Automatic work waits on battery unless the user overrides it in Settings.
- **Failed cleanup.** A failed cleanup is retried at most once a minute, or at once on Try again.
- **Receipts.** Expired receipts are purged at startup and hourly while the app is open.

### Page API

- The page calls only `workspace/api.py`. The shell's dispatch guard allows exactly `API_METHODS`.
- Methods return plain results and never stack traces or file paths.
- Recordings arrive through the native file dialog or a native drop. pywebview resolves the dropped file's path from the WebView2 drop. The page never supplies paths, and every path is validated again (`safe_paths`).

### Views

- **Inbox:** import, plus meetings still being processed, with progress, Pause, Stop now and Resume.
- **Active meetings:** meetings in review, handoff or cleanup. Each shows its age, next action and held status. No scheduled reminders.
- **Meeting:**
  - the transcript, with speaker labels and overlap notes;
  - search: Ctrl+F, Enter for the next match;
  - playback of bounded clips (30 s or less) from a passage's time;
  - autosaved edits;
  - undo: Ctrl+Z or the Undo button;
  - the checkpoint steps, with the cleanup limits shown in each confirmation;
  - holds;
  - versions.
- **Settings:**
  - Quiet or Faster;
  - battery override;
  - speech model;
  - speaker identification on or off;
  - cleanup receipts.

### Edits and versions

- Edits are stored over the generated text. `edit_history` records each change and the text it replaced, so undo survives restart.
- Transcribing again with a different model creates a new version. The previous version and its edits are kept, never overwritten, until transcript cleanup removes them.
- After the review confirmation, the text is locked. It is the text the snapshot was taken from. Playback and transcribing again are no longer available once the audio is removed, and the page explains why.

### Long transcripts

- Rows render in pages of 1000.
- `content-visibility: auto` lets the browser skip off-screen rows.
- On the UI check, a 5000-passage transcript rendered in under 0.5 s.

## Speaker names (#21)

`workspace/speaker_names.py`.

### Corrections log
- Corrections are an append-only log per speaker run (`speaker_corrections`), with three kinds of entry:
  - name: a name for a speaker;
  - merge: one speaker is the same person as another;
  - assign: one passage belongs to a given speaker, or to a new one.
- The view is the log replayed over the machine attribution. Undo removes the last entry, and everything survives restart.
- Machine attribution and transcript text are never changed.

### Default names
- Unnamed speakers are shown as "Speaker N".
- Machine speakers are numbered by first appearance. New speakers are numbered after them, so corrections do not renumber anyone.
- A name given to either side of a merge follows the merged person.

### Name speakers panel
- Each speaker gets three representative passages: clean (no overlap), 1.5 to 15 s long, and spread over the meeting.
- More examples pages through the rest.
- Each clip is exactly the passage shown: same start and end, at most 15 s. Clips play through the single player, so only one plays at a time.
- Selecting a speaker label in the transcript reassigns that passage.

### Scope and lifetime
- Names are meeting-only. There is no cross-meeting list, autocomplete or voiceprint, and no speaking-time chart.
- Naming is locked after the review confirmation. The reviewed snapshot is rendered with the names.
- Identifying again starts with an empty log. The old run keeps its corrections.
- Transcript cleanup removes the log.

## Lifecycle tables reserved for later issues

| Issue | Tables |
| --- | --- |
| #16 | `speaker_runs`, `speaker_turns`. Machine attribution, kept separate from user corrections. |
| #17 | `cleanup_jobs`. Intent is persisted before removal and retried safely. Source identity is re-checked before deleting originals. Workers are quiesced first. A per-meeting `content_epoch` lets late worker results be detected and discarded so they cannot recreate cleaned content. |
| #18 | `holds` (reason, created, reviewed, released) and `receipts` (random job id, timestamps and result only, expiring 30 days after success). |

`schema_version` records migrations. #26 gates schema-changing updates on an empty workspace.
