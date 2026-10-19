# Workspace processing design (#14 to #18)

Technical design for Unminute's durable core. It defines state ownership, chunk identity and the lifecycle that the worker (#15), speaker (#16), cleanup (#17) and hold (#18) issues build on. Product behaviour is in [WORKSPACE_GUIDE.md](WORKSPACE_GUIDE.md); the original product plan, no longer maintained, is in [history/WORKSPACE_PLAN.md](history/WORKSPACE_PLAN.md).

## Ownership

- One state-owning process opens the database: `%LOCALAPPDATA%\Unminute\workspace\workspace.db`. That is per user, never synced, and in the same family of locations as the model cache. Only the owner writes. Workers (#15) send results back and never touch the database.
- Meeting media lives in `%LOCALAPPDATA%\Unminute\workspace\media\<meeting id>\`. The meeting id is random; no names or paths from the source appear in any app path.
- SQLite runs in WAL mode with `synchronous=FULL` and foreign keys on. Each state change that must be atomic is one transaction.

## Source identity

On import, the original file is hashed (SHA-256) and decoded once to 16 kHz mono float32, stored as `audio.f32` (raw little-endian). A file with several channels is mixed to mono with equal weights, the mean of all channels (#220), whatever layout it declares: FFmpeg's surround matrix would treat the channels as speaker positions, drop one it took for a low-frequency channel and let a mix of similar channels exceed full scale. On a recorder that gives each person a track, that could lose a person. Engines read slices through a memory map, so memory stays bounded however long the meeting is.

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

## Content columns (#111 C2)

The columns that hold meeting content are listed in `store.CONTENT_COLUMNS`: titles, error texts, the original's path, passages, words, edits and their history, speaker run errors, speaker corrections, handoff options, hold reasons and the reviewed snapshot. Watched-folder paths are listed too, because a folder's name can name a client, though they are configuration rather than a meeting's content: they are carried over by a migration and never make an update wait (`update.NON_CONTENT`). Only `workspace/store.py` names these columns in SQL. Every value it writes or reads passes through the store's codec, together with its meeting (none for watched folders) and its place (table, column, row id). Rows that hold content get their ids before they are inserted, so each value can be bound to its row.

Other modules keep their rules (holds, cleanup, speaker names, handoff, intake) and use the store's methods for content: `meetings()`, `transcript()`, `passage()`, `speaker_runs()`, `corrections()`, `holds()`, `folders()`, `handoff_options()` and so on. Their own SQL touches plain columns only.

Each value opened will cost a decryption once C2b-2 seals it, so the store opens only what a caller uses (C2b-1):

* State checks use `meeting_state()`, or `meetings(content=False)`, which read the meeting's plain columns (`store.MEETING_STATE`) and open nothing. `meetings(content=("title",))` opens only the columns named. The runner's per-chunk pause check, `holds.add_hold`, the processing loop and the app's checks of a meeting id use the plain state; the Meetings list opens each title once.
* The transcript is paged in SQL (`passage_count()`, `transcript_page()`), so a page opens only its own passages. `transcript(vid, words=False)` reads no word timings; search, the reviewed snapshot and the handoff use it. `search()` makes one pass that opens each passage's shown text once and returns ids. `passage_ids()` and `current_run()` open nothing beyond what they return.
* Every deletion of a meeting's rows is a store method: `delete_meeting_row()` (the sources, then the meeting row), `delete_meeting_content()` (transcript cleanup: every row of the meeting, inside cleanup's transaction after its hold check), `discard_unfinished_import()` (an import that did not finish, #193) and `delete_new_meeting()`; also `undo_correction()` and `remove_folder()`. `delete_meeting_row()` refuses while an active hold covers the meeting, whatever its caller checked; `delete_new_meeting()` (a failed import) and `discard_unfinished_import()` keep a meeting that has ever had a hold, active or released (#193). C2b-2 deletes the meeting's keys in the same place. A condition given to `meetings()`, `holds()` or `folders()` is a fixed expression on plain columns, with values passed as arguments: `store.plain_condition` refuses one with a quote, a comment, a second statement, a subquery, `*`, an `IN` not followed by `(` (SQLite reads `x IN holds` or `x IN json_each(?)` as a subquery) or a content column's name (a subquery could compare a row of content columns without naming them). Since C2a the codec stores values as they are (schema 1); C2b seals them (`docs/ENCRYPTION.md`).

`tests/test_content_access.py` checks this three ways.

* **A static check** reads the Python in `workspace/` (except `store.py`) and `tools/`. It joins string pieces put together with `+`, and a name built up with `+=` or `q = q + ...`; a name assigned a string earlier in the same function, or at module level, stands for its text (in f-string placeholders too). An expression with a literal piece is also checked with each name as a piece known only at run time, so with a content table written in it, a name outside an `IN (...)` slot is flagged whatever text it held. A text counts as SQL if it has a statement's shape or names a content table after `FROM`, `JOIN`, `INTO` or `UPDATE`, so half a query held in one name is still seen. It reads a child-process script held in a string as code. It ignores case and unquotes quoted and schema-qualified names, including a quoted or bracketed slot for a table. It flags SQL that names a content column of a table written into it (in any clause, a comma join or a subquery), selects or returns `*` with such a table, inserts into one without a column list (including `REPLACE`), or is built at run time around one (an f-string, `%` or `str.format` slot, with any flags, width, conversion or spec, other than `IN (...)`). A table named only at run time, in a slot straight after `FROM`, `JOIN`, `INTO`, `UPDATE` or `TABLE`, counts as any table with content, so a column slot or a positional insert with it is flagged too; `SELECT COUNT(*) FROM {t}` is not. It also flags anything that removes rows from a table with content, or from a table named at run time: `DELETE FROM`, a replacing conflict resolution (`INSERT OR REPLACE INTO`, `REPLACE INTO`, `UPDATE OR REPLACE`) and `DROP TABLE`, with comments allowed between the words and before the table, so only the store deletes a meeting's rows. That deletion check does not see rows removed by a table's own `ON CONFLICT REPLACE` constraint or by a trigger (the schema has neither). In `tools/`, the sites still to move would be listed exactly (`TOOLS_RAW_SQL`); since C2b-1 there are none. It follows names in line order within one function only, so of a name assigned on two branches it uses the later text. It does not see a run-time table slot after a comma, `(` or a comment, or after `UPDATE OR <conflict>` (`UPDATE OR REPLACE {t} SET ...`), though the deletion check sees the last two; a name assigned by tuple unpacking; the earlier text of a name assigned on two branches, where the expression is made only of names, or where the name is then extended with `+=` (only the later text is extended); a `str.format` field whose index holds characters other than letters, digits, `_`, `.`, `[` and `]` (such as `{0[a-b]}`, which it does not take as a slot); or SQL passed between functions as arguments or return values, held in attributes or containers, read from data, or put together with `str.join`. It does not read `tests/` or PowerShell. The backstop for what it misses is C2b, which is planned to rename the sealed columns (#111), so a raw read of one will fail loudly.
* **A run with a codec that scrambles every value** goes through a meeting's whole life, in the store and in the app with fake engines: nothing readable is left in the database file, and everything the app shows is still right.
* **An authorizer** (test only) is set on every SQLite connection during those runs. It refuses any statement not written in `store.py` that reads or changes a content column, or inserts into a table that has one. It covers only the paths those runs take.

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

Sleep while recording (`workspace/power.py`, `workspace/recorder.py`):

- While a recording is open (paused included), the app holds a system-required power request, so the idle sleep timer does not end it. `powercfg /requests` lists it with the reason "Unminute is recording a meeting." Windows can ignore it, without saying so, under the policy "Allow applications to prevent automatic sleep" (`AllowSystemPowerRequestAC` and `AllowSystemPowerRequestDC`; Microsoft's policy page says when disabled or not configured, which is to confirm with `powercfg /requests` on a managed laptop). The request starts and ends with the recording indicator (`Workspace._indicate`), on every way a recording ends. A refused request never stops a recording.
- Windows ends every power request when the user starts sleep (the lid with its action set to Sleep, the power button, Start > Sleep). On Modern Standby laptops on battery, it also ends five minutes after the sleep timeout. Recording with the lid closed therefore needs the Windows lid action "Do nothing".
- The lid switch (#172, opt-in setting `lid_override`, policy `AllowLidOverride`): while a recording is open, `power.LidSwitch` sets the active scheme's lid action to "Do nothing", plugged in and on battery, through the power API (no elevation). Before changing anything it writes the original values and the scheme to `power_restore.json` in the workspace (flushed, then renamed into place); if that cannot be saved, nothing changes. It reads the values back and undoes a change that did not take. When the recording ends, by any route, and at every start (straight after the workspace folder is made, before the database opens), it writes the saved values back to the saved scheme, re-applying it only if it is still the active one, except a side the user has changed since (no longer "Do nothing"). It removes the file only once that worked. A restore that fails, or a file that cannot be read or deleted just now, keeps the file: it is tried again about once a minute while nothing is recording, and the user is told once. A record that is damaged or names a scheme that is not a GUID cannot say what to put back: the lid action is left as it is and the user is told once to check it in Windows Settings (the user's decision on #188, rather than falling back to Sleep). A new recording never overwrites originals still waiting to go back. The record is read only up to 4 KB (a real one is about 80 bytes); anything larger, or nested too deeply to parse, counts as damaged. The originals are also kept in memory for the recording, so a record deleted by something else is still put back. The idle retry runs under the same lock that sets the lid for a recording, so it never undoes a recording that has just started. None of this can stop a recording starting or ending, or the app starting. Turning the setting off, or the policy turning it off, puts it back at once. Uninstalling puts it back too (#191, the user's decision of 18 October 2026): before removing anything, the uninstaller runs the installed app once with `--restore-lid` (`power.restore_only`), which applies `LidSwitch.restore()` with the same rules to the record in the workspace, and nothing else: no window, database or heavy import. It stands aside while the app is open (its single-instance mutex), and the uninstall carries on whatever the result (docs/PACKAGING.md). A rollback needs no restore of its own: every version from the rename on (#136) has the same restore file and rules, so the version rolled back to puts it back at its next start. If Unminute never starts again after a crash and is not uninstalled, the lid stays at "Do nothing" until the user or IT changes it (documented). It stands aside where Group Policy (`PowerSettings\5CA83367-...`, `ACSettingIndex` or `DCSettingIndex`) or Intune (`PolicyManager\current\device\Power`, `SelectLidCloseAction*`) sets the lid action, and when it cannot tell, including a registry it is not allowed to read.
- The lid notice (#172): while recording, `power.LidWatch` subscribes to lid open and close notifications. A lid closed for 5 s or more (`LID_NOTE_S`) while recording gives a `lid` event with its length when it opens, and the recording card says how long the recording carried on with the lid closed, because its marks could not be seen meanwhile.
- Sleep freezes the whole app. A watchdog thread in the recorder beats every 0.2 s (`ALIVE_S`); a beat later than `SLEEP_S` (15 s) on the monotonic clock means the app was frozen by sleep. Changing the time cannot fake it, and a slow save or a stalled microphone does not delay the watchdog. A shorter late beat, beyond the silence limit (`SILENT_DEVICE_S`), is noted as a `stall` event with its length, and the recording carries on (the lost-device check still decides if the audio has gone). After a sleep, the recording stops with state `slept` and a `sleep` event, keeps everything recorded before, and joins no audio from after the wake. The watchdog, the audio callback and the lost-device check each read the beat before writing their own mark, so whichever runs first after the wake notices.
- A sleep while paused leaves the recording paused, unless the driver ends the microphone stream on waking: that stops it as `slept`.
- Silence, the heartbeat and the 8-hour limit are timed on the monotonic clock, so setting the time back or forward cannot pause or trigger them; the wall clock is used only for journal times and the "last seen" time that crash recovery compares. After a stall the silence check waits for the watchdog's next beat and counts from the stall's end, so a microphone that is fine gets a full silence period to catch up. A microphone that goes quiet for longer than the silence limit and comes back before the silence check runs is noted as a `gap` event with its length.
- Residuals: the app held up for more than 15 s without sleeping (very heavy paging, for example) is reported as sleep and stops the recording; a sleep shorter than 15 s is reported as a stall or a lost microphone, not as sleep.
- Tested with simulated freezes only. Real sleep, the lid and Modern Standby behaviour are documented by Microsoft, not yet tested on hardware.

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
   - This removes all transcript working content: segments, edits, edit history, speaker data, attempts, chunks, versions, snapshot, sources, released holds and the meeting row. The store's `delete_meeting_content()` does it, inside the transaction that re-checks holds.
   - It then runs `secure_delete`, a WAL checkpoint (TRUNCATE) and `VACUUM`, so no removed text stays readable in the database files.
   - Leftover files are removed last.
   - Transcript cleanup waits until audio cleanup has finished, because it removes the source records that audio cleanup needs.

Each confirmation records a `cleanup_jobs` row before anything is removed. The processing loop runs due cleanup jobs before any other work, whenever it looks for work: a pending job at once, a failed one again after `FAILED_RETRY_S`, and a held one only once its hold is released. So unfinished jobs resume after a restart.

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
- **Database check (R11).** When the app last ended without closing (its run marker, `logs\app.running`, is still there: `__main__` builds the `Workspace` before `diag.install` replaces it) or the last check found damage, `store.quick_check_file` runs SQLite's `PRAGMA quick_check(1)` on its own read-only connection before the store opens, so nothing has written to the database yet. Damage is what the check reports, or one of SQLite's damage codes when it cannot run: corrupt and not a database at once, and an I/O error if it comes back when tried again a second later (another program can lock part of the file for a moment). A check that cannot run (busy, out of memory) is logged and kept as skipped, holds nothing new, and runs again at the next start. On damage, the store opens without marking interrupted work, and the start-up upkeep that can delete (finishing or removing imports cut short, re-queuing watched-folder imports) waits. So does every job, cleanup included (holds are read from the damaged database), every import, folder scan and receipt purge (the user's choices, 17 Oct 2026, #132). A recording recovered after a crash still asks Keep or Delete, which writes nothing. The page shows a message with the code, **How to recover** (the guide, in the user's browser) and **Continue anyway** (`continue_despite_damage`), which runs the waiting upkeep and releases the hold for this run. In that run no import that looks unfinished is removed (damaged pages can make a finished meeting look unfinished); a start whose check passes does that. The user can remove such a meeting with Discard (#181), during the hold or after Continue anyway; during the hold its cleanup jobs wait: `discard_meeting` accepts a meeting with no version only while the intake queue has no unfinished task, queued or in progress (only the intake thread creates meetings, adds sources or sets an original's deletion authority, so no import can still give it either), after the usual confirmation; no new state. Start-up upkeep plans nothing for a meeting Discard has marked; its cleanup jobs remove it. Start-up upkeep never removes an unfinished import that has, or has ever had, a hold (#193, C2: releasing a hold deletes nothing): it stays listed (`unfinished`, "Import did not finish") and only the user's Discard removes it, once any hold is released. The hold check, the row deletion and the folder removal run under the store's lock, so no hold can be added in between; the folder is removed inside the transaction, so a folder that cannot be removed keeps the meeting for the next start, never an orphaned folder. On a damaged database (SQLite's CORRUPT or NOTADB codes, `store.DAMAGE_CODES`; an I/O error at compaction retries) the transcript job removes the rows and files and confirms the write-ahead log is emptied, but cannot rebuild the file (#198): it finishes with the receipt "transcript removed; database compaction deferred until the workspace is repaired" and leaves `compact.pending` beside the database; the next start whose database check passes runs the compaction and removes the marker. A busy or locked database still fails the job and retries it. Every media path is built by `media.meeting_folder`, which accepts only the app's own ids (32 lowercase hex characters, #194): any other id, such as one from a damaged or edited database, is refused before a path exists, and a cleanup job for it fails with "Meeting not found". A schema migration is refused while the check holds work, because damaged tables can read as empty. The hold is in memory, so it adds no write. The result goes to the log (`integrity`: ok, failed, skipped, continued) and `logs\integrity.json` (result, code, time, no content), read by the next start, Copy diagnostics, `--status` and `--health`. A database too damaged to open ends at a message box with a code, recorded the same way (once); any other failure to open gives an incident code in the log. A database error while finishing an interrupted import never discards it.
- **Failed cleanup.** A failed cleanup is retried at most once a minute, or at once on Try again.
- **Receipts.** Expired receipts are purged at startup and hourly while the app is open.

### Diagnostics (R9, #130)

- **Local and content-free.** Each process writes its own log in `workspace\logs\`: `app.log`, `worker.log`, `gpu-worker.log`, `setup.log` (downloads). A record is one JSON line with a fixed kind (start, stop, timing, incident, crash, worker exit, recovered native error, console output). Each field has its own rule in `workspace/diag.py`: fixed words, an incident code, a version, an exception or warning type name (an incident raised from another also keeps the cause's type name), reduced stack frames, or a number. Frames keep only the file within the app, a library or the standard library (also in the package's layout), the line and the function. Exception messages, folders and the user's own file names are never written; a value that breaks its field's rule is written as `?`.
- **What the records show.** When the app started and stopped and when each incident happened (usage metadata), the version, start-up timings, and the type and code location of each unexpected error. No meeting content.
- **Capped.** Each log rotates at 512 KB and keeps three files, so at most 1.5 MB per process. A rotation that cannot move the current file (held open by a viewer) changes nothing and is tried again; meanwhile the current file grows to at most 1 MB, then new records are dropped until it can rotate (so about 3 MB in the worst case, if that happens twice in a row).
- **Incidents.** An unexpected error at the bridge, in processing, in an import (including a recording's) or in a worker is recorded with its type and frames under a code such as `U-1A2B3C`, and the user sees the code: "Something went wrong (code U-1A2B3C)", a meeting's error, an import's error. The same incident (area, type, frames and fields) is written at most 3 times a run and then keeps its last code, and at most 200 incidents are written a run, so a failure that repeats (a page calling a broken method again and again) cannot push a crash out of the log. Past 200, the user still sees a code, but it is not in the log.
- **Crashes.** Uncaught errors on any thread, and a worker that fails to start, are recorded. Native crashes are written by `faulthandler` to `<name>.crash`. That raw file can hold program folders (with the user's name), so it is never shared or read as a log: it is started afresh past 256 KB, only its last 64 KB is read, and it is turned into a record and deleted at the next start, when a worker ends, or at a clean close. It counts as a crash only if the process did not end cleanly (`app.running`; a worker's exit code): on Windows `faulthandler` also reports native errors that another component recovers from (ctypes, .NET), and those are counted as recovered. A run of the app that ended without closing and left no native crash (ended from Task Manager's Details tab, or the power was lost) is recorded as a crash (`did_not_close`) at the next start. `app.running` holds the running process's id and start time (no names), so `--health` can ask Windows whether that process still runs, from any session. A worker that ends by itself, even while idle, is recorded in `app.log` with its exit code and crash; a hung worker is noted. Workers and the download process turn off the Windows Error Reporting dialog (`SetErrorMode`), so a crash ends them at once; whether this also stops Windows crash dumps is to confirm on a managed device (T21).
- **Console output, summarised.** Console output (the app's own under `pythonw`, the workers', the download process's) is free text that could hold content (P3), so it is never written. Only the error and warning type names in the two places Python writes them are counted (an exception's last line, a warning line: `UserWarning`, `ImportError`), the user's choice of 17 Oct 2026.
- **Start-up timings.** Imports, opening the workspace, and the page first loaded, in seconds.
- **Page errors.** The page's `error` and `unhandledrejection` events call `report_ui_error(kind, line, column)`: a fixed kind and two numbers, never a message (which could quote meeting text). At most 20 a run and one a second are recorded. The first error before the bridge is ready (in setting up the page, say) is kept and sent once it is.
- **Copy diagnostics** (Help, started by the user): versions, the install type, setting values (each only if it is one of that setting's known values), which policy keys are managed and their values (a folder shows as "set", a bad value as "invalid"), workspace counts, model states, the graphics card's driver state, crashes in the last 7 days, and the newest 40 log records. The records come from the app's own logs only (`app`, `worker`, `gpu-worker`, `setup`), reading no more of each than a log can hold, and pass the field rules again: only the fields the app writes are kept, each to its rule, numbers within range and nothing shaped like a token. That keeps a record to the shapes the app writes; it cannot tell them from the same shapes written by another program that can already write to the user's logs folder. Never the raw crash files. Copied like any copy the app makes (kept out of clipboard history and cloud sync).
- **`--health`** for device management: see `docs/PACKAGING.md`.
- **Checked by** `tests/test_diagnostics.py`: the whole lifecycle, with marker words with and without spaces in titles, file names, transcript text, speaker names, a token and error messages (at the bridge, in imports and in the worker), leaves none of them, and none of this computer's folders or the user's name, in any log, in the import errors shown, or in the Copy diagnostics text.

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
  - speech model: each choice is marked "(not downloaded)" or "(damaged)" from the same state the Downloads page shows (`setup.status`, kept with the Downloads cards for `DISPLAY_CACHE_S`). A model that is not ready may still be chosen; a note beside the picker says so, and Manage downloads is highlighted only when it can fix the model: it offers the model and managed policy allows downloads (`AllowModelDownloads`, K3). Otherwise the note names another model or the IT team (#244);
  - speaker identification on or off;
  - cleanup receipts.

  `settings.json` carries `settings_version` (R11), the newest version that wrote it. Settings this version does not know, and a newer version's value for a setting it cannot use, are kept and written back unchanged, so going back a version and then forward loses nothing. A key's meaning never changes between versions: a new meaning takes a new key. A value the app refuses falls back to its default, and Settings names it ("on their defaults"); a file that cannot be read (damaged, not an object, larger than 64 KB, or nested too deeply to write back) means all defaults, Settings says so, and the file is kept aside as `settings.unreadable.json` before the first save replaces it. Saves run one at a time; each is flushed to disk before it replaces the old file, and the app uses the new values only once the file is saved. A file that would grow past 64 KB is written compactly, and kept values that still do not fit are dropped.

### Edits and versions

- Edits are stored over the generated text. `edit_history` records each change and the text it replaced, so undo survives restart.
- Transcribing again with a different model creates a new version. The previous version and its edits are kept, never overwritten, until transcript cleanup removes them.
- A meeting that failed (phase `error`, still in the inbox) can be transcribed again too (#244). Try again retries the active version with its own model; Transcribe again (the Versions tab, or **Transcribe with another model…** on the failed meeting's row) makes a new version with the model now chosen in Settings and clears the error, so processing picks it up. `can_retranscribe` in the meeting list and detail is the one rule for both buttons: audio kept, review not confirmed, and not still being transcribed unless failed. Transcribe again refuses a model that is not ready before anything changes, with the note Settings shows, and the new version uses the model it checked (read once). A model whose files change in the cache window can still fail at load; the earlier version and its edits stay as they were (D1). The same model again on a failed meeting is refused with a pointer to Try again. `tools/workspace_ui_check.py` ("failed meeting") runs this in a real window: a synthetic meeting stops because its chosen model is not downloaded (an empty model folder, so nothing is loaded), the row offers **Transcribe with another model…**, the not-ready dialog shows for the same model, and after a ready model is chosen the confirmed Transcribe again takes it out of Needs attention with the failed version kept.
- A model that cannot load stops the meeting with the worker's error (`ModelLoadError`), which names Settings > Manage downloads when that page offers the model. The worker never reads managed policy, so the window process reads `AllowModelDownloads` (K3: an unreadable policy counts as off) and, with downloads off, replaces that advice with the IT wording before it stores the meeting's error or the speaker pass's note (`engines.advice_without_downloads`, #257). Only the fixed advice in `engines` and `speaker_load` is replaced; a test fails if another message names Manage downloads.
- After the review confirmation, the text is locked. It is the text the snapshot was taken from. Playback and transcribing again are no longer available once the audio is removed, and the page explains why.

### Long transcripts

- Rows render in pages of 1000.
- The store pages in SQL (`transcript_page()`, with `LIMIT` and `OFFSET` in transcript order), so a page reads and opens only its own passages, without their word timings; search makes one pass over the text and returns passage ids.
- `content-visibility: auto` lets the browser skip off-screen rows.
- On the UI check, a 5000-passage transcript rendered in under 0.5 s.
- **While transcribing (#127).** When only the progress has moved, the page fetches from the last row it drew onwards (the existing `transcript(mid, start, count)` call, no new bridge method) and appends the new rows. Passages are only ever added to a version, never removed or moved, so if that last row is still at its place with the same speaker, every row drawn is still there in order. The whole list is redrawn instead when that cannot be shown: an earlier passage added (a chunk done after a later one), a different version, speakers identified or changed, a search open, totals that do not line up while paging, or an error. While a passage is being edited nothing is drawn; the rows follow once the edit ends. An append given up because an edit, another meeting or version, or a newer draw started while it loaded draws nothing, not even a full redraw (that would be thrown away too), and the next refresh tries the append again. Measured by `tools/ui_idle_check.py` (transcribing run, ratchets in `tests/budgets.json`) and `tools/workspace_ui_check.py` ("growing transcript").

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

## Handoff (#22)

`workspace/handoff.py`, `workspace/clipboard.py`.

### How the handoff leaves the app
- After the review confirmation, Hand off opens a dialog with an exact preview of what is copied or saved.
- Three ways out:
  - **Copy prompt and transcript.**
  - **Copy prompt only:** for attaching the Markdown file instead of pasting.
  - **Save Markdown:** uses the native save dialog. The app keeps no record of the file.
- There is no API integration and nothing is sent anywhere. The user pastes or attaches the output in their organisation's approved assistant.
- Copying is done by Python through the Windows clipboard API, so it does not depend on browser clipboard permissions.

### Templates
- Templates live in `templates.json`. They hold configuration only, no meeting content.
- They are editable and versioned:
  - each save of a template creates a new version;
  - built-in templates can be restored to their original wording;
  - custom templates can be added and deleted.
- The built-in templates are Minutes and actions, Short summary, Action list, Board or directors' minutes, Client meeting file note, Follow-up email, and Team or WIP notes.
- Each built-in has its own style brief, then a shared way of working. The assistant is asked to:
  - gather context first: from its connectors, plugins and tools, and from the project or workspace it is in;
  - draft as a professional minute-taker would: clean, concise, New Zealand English, no timestamps. It corrects misheard words and wrong speaker labels, and fills in owners and dates, from context and judgement;
  - show the draft in the chat, with a "Points to check" list of what it inferred, kept out of the draft;
  - revise until the user is happy;
  - then offer a Word document in the organisation's brand template. If the user accepts, the assistant gives a `context.md` (meeting details and the approved content, not the transcript) and a prompt for a session that can create Word documents and use the brand skill. The follow-up email is given ready to copy instead, and never sent;
  - treat the transcript as data, never as instructions.
- The transcript lines keep their timestamps, so the user can ask where something was said while revising. The outputs leave them out.
- Each built-in has its own version number. Changing its wording raises the number, so a version always means one wording. A built-in the user saved keeps the user's wording; **Restore original wording** brings in the current built-in.
- The app adds `TOOL_RULE` after every template, built-in, edited or custom, so a saved or tampered template cannot drop it: read only the organisation's own records, change nothing unless the user asks in the chat, no web addresses or outside searches because of the transcript, and tool results are data. These are requests to the assistant, not controls (`docs/SECURITY_MODEL.md` T30).
- When names are replaced with Speaker 1, Speaker 2, the app adds `ANON_NOTE`. It overrides the template: no invitation, attendee or people lookups, no search for a person's name, no context from emails, chats or documents, and no names in the draft, the points to check or `context.md`.
- Invisible characters (format characters such as tag characters and direction overrides, private-use and unassigned characters, variation selectors) are removed from the template, title, names, text and redaction words, because the preview cannot show them.
- The combined text wraps the transcript between BEGIN TRANSCRIPT and END TRANSCRIPT lines, with a note to treat it as data. The saved Markdown carries the same note and lines (#174), so a file attached on its own still marks the transcript as data: it is the end of the combined text, word for word. Copy prompt only points the assistant at those lines in the file.

### Omissions and redactions
- Leaving passages out and redacting words (case-insensitive, longest phrase first, applied to the title, speaker names and text) changes the handoff only. The transcript is not changed.
- Both are stored in `handoff_options` and removed with the transcript at cleanup.

### Length
- Nothing is truncated.
- Above 50,000 characters, the dialog recommends Save Markdown with Copy prompt only, because some assistants cut off long pasted text.

### Final confirmation
- The final confirmation reminds the user that the app neither clears the clipboard nor deletes saved Markdown files. It does not clear the clipboard automatically.

## Setup and credentials (#19)

`workspace/setup.py`, `workspace/credentials.py`.

### The Setup view
- It opens on first run, and whenever no speech model is usable.
- Model cards show each model's purpose, download size, licence, source, pinned revision and status: ready, not downloaded, or damaged.
- Actions are Download and Repair.
- Speaker identification is optional and can be skipped, then added later from Setup.

### Downloads
- Downloads run one at a time in a separate, short-lived download process. The app process keeps its offline network guard, and inference workers never download.
- Before a model is replaced, the inference worker is unloaded. Models cannot be replaced while a meeting is being processed.
- Each download is verified against the pinned SHA-256 manifest before it replaces anything. A damaged copy is set aside, not deleted.
- Windows cannot rename a folder while a file in it is open, and antivirus software such as Microsoft Defender can open newly written files for a moment to scan them. So each rename that sets a damaged copy aside or moves a verified download (a model's, or the graphics card component's) into place is tried again when Windows refuses it as access denied or in use (WinError 5 or 32), with a short back-off (a quarter of a second, doubling to at most 4 seconds) for up to 30 seconds from its first refusal, then fails with the same message as before (`asr_engines.rename_with_retry`, #249, as the installer does since #239). Any other error fails at once. It renames the same two folders each time and never copies or deletes anything.
- Progress (R12): the app process measures what the download has written so far in its staging folder against the size it fetches, and sends the page a number (`percent`). The graphics card component's wheels arrive one at a time and each is deleted once unpacked, so finished ones count at their pinned size. While a downloaded file is checked or unpacked, the bar shows how much has arrived; a check of an installed copy (Check every file) shows none. Only files written by this attempt count, so a cancelled attempt's leftovers do not show as progress. Links and junctions in a download folder are never followed. The page sets the bar in place on each poll; the list around it is rebuilt only when something else changes, so focus and open Details stay. The download process itself is unchanged, and the page never reads a number out of a message.
- An interrupted or failed download is retried by starting it again. Errors are explained in plain language, for example:
  - terms not accepted;
  - token not accepted;
  - no connection.

### The Hugging Face token
- The token is needed only for the gated speaker model. Public models never receive it.
- It reaches the download process through a pipe, never on a command line or in the environment.
- By default it is remembered in the user's Windows Credential Manager. The user can untick Remember to use it once. Remove the remembered token removes it.
- It is never written to settings, logs or the database, and never returned to the page. The page learns only whether a token is remembered, and clears the input after use.
- Removing the token does not affect models already downloaded: they load offline.
- An `HF_TOKEN` user variable, an older way to give Hugging Face tools a token, stays in the user's Windows environment until the user removes it. At start the app removes it (and `HUGGING_FACE_HUB_TOKEN`, an older name the Hugging Face library also reads) from its own environment (`credentials.drop_environment_token`), so its worker, download and WebView2 processes do not inherit it (#111 A2), and never uses it. New tokens never go into the environment.
- A token is remembered only after a download has shown that it works.
- Model work waits while a model is being downloaded or checked. The two are coordinated with the processing thread under one lock, and processing resumes when the download ends.
- A download can be cancelled.
- Check every file re-verifies an installed model against its SHA-256 manifest. For the graphics card component a failure is remembered for the rest of the app session: the component is not used, and shows as damaged, until Check every file passes or Repair replaces it, and speaker passes run on the processor with the reason recorded (#153).

### Help pages
- Help links (sign-up, model terms, tokens) are a fixed allowlist, opened in the user's own browser.

### Pinned revisions
- A meeting keeps the model revision it was started with. If an app update changes the pinned revision, its unfinished transcription stops with an explanation ("Transcribe again"), instead of silently mixing revisions.
- A model that is missing or fails its checks when a worker loads it stops the meeting with a message that names Settings > Manage downloads (`workspace/engines.py`, `workspace/speaker_load.py`): a worker only loads models after Setup. A speech model the Downloads page does not offer (`engines.OFFERED_SPEECH`, kept equal to `setup.SPEECH` by a test) names another model or the IT team instead.

## Watched folders (#23) and cloud sources (#24)

`workspace/intake.py`. The user guide is [INTAKE.md](INTAKE.md).

### Scanning
- Folders are watched explicitly. Existing contents are imported only if the user chooses.
- A scanner thread reconciles each folder 30 seconds after launch (so start-up is not slowed), every 5 minutes while the app is open, and at once on Check now or when a folder is added. Between scans it sleeps until the next scan is due. Sleep counts as elapsed time, so a scan that fell due while the computer slept runs within a minute of waking.
- Pausing a folder, removing it, or policy turning watched folders off (#152) stops a scan of it before the next file is read, and nothing more is queued from it. Imports already queued are put back to waiting when they come up (dropped if the folder was removed), so the first scan after the folder is on again imports them. Content lined up from another file counts as a duplicate only once that import is done: until then the copy waits, so a held or failed import elsewhere does not stop it. A folder paused part-way through its first scan is not marked as having a baseline, so the baseline is done again when it is resumed; a finished baseline stays.
- When nothing is new, a scan writes one database transaction per folder, plus one to keep the suppression data bounded. Changes are committed early only before an import is queued, and never held in a transaction while a file is hashed.
- Polling was chosen over change notifications because notifications are lost while the app is closed and are unreliable on synced folders. A reconciliation scan is needed anyway.
- Files are identified by keyed path hash, file ID, size and modification time, never by date.
- A file is imported once it has been stable across two scans. Its content is hashed once per new identity.

### Suppression data
- **`intake_seen`:** keyed path hashes, sizes, times and states per folder. Rows go when a file leaves the folder.
- **`intake_content`:** SHA-256 of imported content, kept for 120 days and at most 20,000 rows.
- Neither holds names or meeting content.
- Manual imports are never deduplicated.

### Bounds
- Drive roots and the user profile root are refused as watched folders.
- Scans go at most 2 levels deep and look at most at 5,000 files per folder per scan.

### Cloud files and deletion authority
- **Cloud-only placeholders** wait by default. With a per-folder setting, the app reads them so the sync client fetches them.
- **Paused sync, offline and unreadable files** are recorded and retried. No progress is invented.
- **Deletion authority** is a separate, explicit per-folder setting (`watched_folders.delete_originals`).
  - It marks sources imported from that folder (`sources.delete_original`).
  - Cleanup receives the authorised folders at deletion time, so revoking the setting keeps originals.
  - The identity and link checks from #17 still apply.

## Lifecycle tables reserved for later issues

| Issue | Tables |
| --- | --- |
| #16 | `speaker_runs`, `speaker_turns`. Machine attribution, kept separate from user corrections. |
| #17 | `cleanup_jobs`. Intent is persisted before removal and retried safely. Source identity is re-checked before deleting originals. Workers are quiesced first. A per-meeting `content_epoch` lets late worker results be detected and discarded so they cannot recreate cleaned content. |
| #18 | `holds` (reason, created, reviewed, released) and `receipts` (random job id, timestamps and result only, expiring 30 days after success). |

`schema_version` records migrations. #26 gates schema-changing updates on an empty workspace.
