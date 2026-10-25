# Privacy impact assessment: draft for the deploying organisation

**Status: draft.** This is a starting point for an organisation that deploys Unminute to write its own privacy impact assessment. It records facts about the software: what it stores, where, how it is protected, how long it is kept, who can reach it and what leaves the device. It does not assess those facts against any law, standard or policy, and it reaches no conclusions. That assessment, and every decision in the last section, belongs to the deploying organisation.

It describes the `main` branch as at 22 October 2026. Where the newest release, v0.5.0-preview.3 (20 October 2026), differs, the difference is stated. The technical detail behind each statement is in [SECURITY_MODEL.md](SECURITY_MODEL.md) (threats, controls and the data inventory) and [ENCRYPTION.md](ENCRYPTION.md); when they differ from this draft, they are the source.

## What Unminute does

Unminute turns a meeting recording into a reviewed transcript on the user's own Windows laptop. Transcription and speaker identification run on the laptop. The user reviews and corrects the transcript, then copies or saves it to hand to the organisation's AI assistant, which drafts the minutes. The app then deletes its own copies when the user confirms each cleanup step. It is a temporary workspace, not an archive.

## Personal information it handles

| Category | Examples | Comes from |
| --- | --- | --- |
| Voices and speech | The audio of everyone who speaks in a recorded or imported meeting | A recording the user imports, a watched folder, or the microphone |
| What was said | Transcript passages, word timings, edits and their history, the reviewed snapshot; rough draft text while recording, if the user turns it on (#104; on `main`, not in a release yet) | Made on the laptop from the audio, then edited by the user |
| Names and labels typed by the user | Speaker names, meeting titles, hold reasons, redaction words, handoff templates | The user |
| File locations | The path of an imported recording, watched-folder paths (either can include a person's or client's name) | The user's choice of file or folder |
| Recording details | Microphone name, when recording started, paused and stopped, and a record that the user confirmed everyone was told (confirmed, wording version, time, and the sources it named: the microphone, or the microphone and computer audio; no names). With computer audio (#108, off by default), also the output device's name and timing marks | The app, during a recording |
| Usage metadata | When the app started and stopped, when errors happened, error codes and versions; raw crash files hold program folders, which include the Windows user name | The app's own logs, in the workspace's `logs` folder |
| A record of what was done (**not in v0.5.0-preview.3**) | When each meeting was imported or recorded (with the confirmation's outcome, and whether computer audio was recorded), reviewed, handed off, held, cleaned up or discarded, and when watched folders and delete-originals authority changed, by the app's own random ids, with fixed words and counts; no text, titles, names, folders or hold reasons. A copy of each record's hash goes to the Windows Application log with the user's Windows account (SID) | The app's audit log, in the workspace's `audit` folder |
| A credential | The user's Hugging Face access token, if they choose to remember it | The user, in Setup |

The app does not collect contact details and keeps no account of its own. Speaker identification separates the voices in a recording; it does not work out who the speakers are. Names come only from what the user types.

## Where it is stored, and how it is protected

Everything the app keeps is on the laptop, under the signed-in user's profile; the table also lists where the originals and the handoff go, which may be elsewhere. `%LOCALAPPDATA%` is per user and is not synced by OneDrive; the app refuses to open its workspace if it has been redirected into OneDrive or onto a network drive.

| Data | Where | Encrypted by Unminute on `main` | In v0.5.0-preview.3 | Kept until |
| --- | --- | --- | --- | --- |
| Original recording | Wherever the user keeps it | No: the app reads it in place | Same | The user deletes it. The app deletes an original only from a watched folder whose deletion the user has authorised, after review is confirmed |
| Working copy of the audio (`audio.uma`) | `%LOCALAPPDATA%\Unminute\workspace\media\` | Yes: AES-256-GCM, block by block, under the meeting's own audio key; no plain copy is written | No (a plain `audio.f32`) | The user confirms the transcript is reviewed, or discards the meeting; longer while a hold applies |
| A recording's details (meeting title, microphone name, recording events, confirmation record) | `workspace.db` on `main`; `recording.json` in `workspace\media\` in v0.5.0-preview.3 | Yes: sealed in the database under the meeting's key, in place of `recording.json` | No | With the working copy of the audio |
| Microphone recording in progress or not yet imported | `workspace\recordings\` | Yes: AES-256-GCM, a few seconds at a time, under the recording's own key, wrapped by the key Windows protects for the user; no plain copy is written, even for the decoder. The key is deleted before the files once the recording is a meeting, or when the user discards or deletes it | No | Imported into a meeting; after a crash, until the user keeps or deletes it |
| A recording's journal (times, state, events, the microphone's and, with computer audio, the recorded output devices' names, the confirmation record without names) | `workspace\recordings\` | No: a device name can name a person, a limit the owner accepted | Same | With the recording's folder |
| Live draft text while recording (#104; on `main`, off by default, not in a release yet) | The memory of the app, of its draft worker process and of the page, which shows it on the recording card | Never written to disk, the database or a log; Windows can still write it to the page file or hibernation file, or a crash dump could hold it | Not present | Cleared at Stop and save, Discard, when the recording stops itself, when the user or policy turns it off, and when the app closes; nothing is left after a crash |
| Transcript, edits, history, snapshot, speaker names, titles, hold reasons, redaction words, file paths, and the error and restart notes the app writes about a meeting | `workspace\workspace.db` | Yes: AES-256-GCM under per-meeting keys, wrapped by a key Windows protects for this user (DPAPI). Times, states, counts and approximate lengths stay readable | No | The user confirms the final document is saved, or discards the meeting; longer while a hold applies |
| Handoff templates | `workspace\templates.sealed` | Yes, under the workspace key | No (`templates.json`, plain) | The user changes them |
| Fingerprints of recordings, to recognise a file already imported | `workspace.db` | Kept as keyed fingerprints (HMAC), not plain hashes | Plain hashes | A meeting's own: until its final cleanup. A watched folder's: 120 days |
| Cleanup receipts (a random number, times and the result) | `workspace.db` | Content-free | Same | 30 days |
| Settings | `workspace\settings.json` | No; settings only | Same | The user changes them |
| Queued model downloads (**not in v0.5.0-preview.3**) | `workspace\download_queue.json` (written through `download_queue.tmp`, renamed into place) | No; model names only | Not present | The queued downloads finish, fail or are cancelled |
| Logs, crash files and the key state (`keys.json`: ok or locked, a reason, a Windows error number, a code, a time) | `workspace\logs\` | No; content-free by design | Same | Size-capped and rotated; crash files read and deleted at the next start |
| Audit log (each action's kind and time, the app's random ids for the meeting, hold, cleanup job, watched folder or recording, fixed words and counts; no content, names, paths or reasons). A meeting's id links its import, handoffs and deletion in this file, and maps to nothing once the meeting is cleaned up | `workspace\audit\` | No; content-free by design, and hash-chained so a change shows | Not present | At most 16 MB, after which the oldest 1 MB segment is deleted; kept across an update that waits for an empty workspace; removed with the workspace |
| The audit log's Event Log copy (chain id, sequence number, kind and the record's SHA-256; no meeting, hold, job, folder or recording id, and no content), with the user's Windows account (SID) | The Windows Application log, and wherever IT forwards it | No | Not present | The Application log's size and retention, and those of any forwarded copy |
| Hugging Face token | Windows Credential Manager | Protected by Windows (DPAPI) | Same | The user removes it; uninstalling keeps it |
| Copied handoff | The Windows clipboard | No. The Copy buttons ask Windows to keep it out of clipboard history and cloud clipboard (best effort: if that request fails, the copy still goes ahead) | Same | Replaced, or cleared after a set time if IT sets `ClipboardClearMinutes` |
| Saved handoff | Wherever the user saves it | No | Same | Not tracked by the app |
| Models | `%LOCALAPPDATA%\Unminute\models\` | Not personal information | Same | Removed in the app; uninstalling keeps them |

Disk encryption (BitLocker) is the organisation's control. While the laptop is off or hibernated it protects everything above that is stored on the laptop's disk, including what the app does not encrypt. It does not protect copies elsewhere, such as an original or a saved handoff in a synced or network folder.

## Who can reach it

- **The signed-in user** can see everything in their own workspace through the app.
- **Other standard users of the same laptop** cannot read the workspace: it inherits the user's profile permissions.
- **Administrators, SYSTEM services and backup agents** can read files the app does not encrypt (settings, logs, the audit log, a recording's journal; in v0.5.0-preview.3 also microphone recordings waiting to be imported and `recording.json`). On `main` they cannot read the encrypted database content, the working copy of the audio or microphone recordings waiting to be imported without the user's Windows key protection, unless they run code as the user (an administrator of a device the user is signed in to can usually do this) or hold the domain's DPAPI backup key. In v0.5.0-preview.3 nothing is encrypted by the app.
- **Software running as the user**, including malware, can read what the user can read, encrypted or not.
- **IT** sets managed policy (below). Policy gives IT no access to content, and `--status`, `--health`, `--verify-audit` and Copy diagnostics report counts, states and codes, never content. On `main`, whoever can read the Windows Application log, or IT's forwarded copy of it, sees the audit log's Event Log copy: when and how often the user took each kind of action, with their Windows account, and no meeting, hold, job, folder or recording id, and no content.
- **The Unminute project** receives nothing: the app has no telemetry and no update check.

## What leaves the device

The app sends no meeting content itself, and every flow below needs a user action, apart from IT forwarding the Windows Application log. The limits of the network controls are under "Known limits".

| Flow | Started by | What goes, and where |
| --- | --- | --- |
| Copy (handoff) | The user | The previewed prompt and transcript, to the Windows clipboard. Any app in the session, and a redirected remote-desktop clipboard, can read it |
| Copy and open (**not in v0.5.0-preview.3**) | The user | The same copy, then Windows opens one fixed address (chatgpt.com by default, or one IT sets) in the default browser. The address carries no content. The user pastes and sends |
| Save Markdown | The user | A file wherever the user chooses |
| Selecting text and copying it | The user | Ordinary clipboard copy, without the Copy button's protections |
| Pasting into an AI assistant | The user, outside the app | Whatever the user pastes or uploads goes to that assistant's provider, under the organisation's account. The built-in templates ask the assistant to gather more context with its connected tools, so what those tools read also goes to that provider |
| Model downloads | The user, in Setup or Manage downloads | HTTPS requests (standard port 443 only on `main`) for fixed model files to huggingface.co and its download hosts, download-r2.pytorch.org (graphics card component) and files.pythonhosted.org (graphics card component for Whisper). The Hugging Face token is sent for the speaker model download. No meeting content. The hosts see the requests and the network address they come from |
| Help pages | The user | Windows opens a fixed help address in the browser; nothing from the meeting is in it |
| Copy diagnostics | The user | Versions, settings, counts and error locations, with no content, names or folders, to the clipboard for a support request |
| The audit log's Event Log copy (**not in v0.5.0-preview.3**) | IT, if it forwards the Application log (Windows Event Forwarding or a SIEM) | Each record's chain id, sequence number, kind and hash, with the user's Windows account; no meeting, hold, job, folder or recording id, and no content. The app writes it to the local Application log only; forwarding is IT's |
| Deleting a watched original | The user, after authorising that folder and confirming review | The original file is deleted. In a synced folder, the deletion syncs to the cloud and to everyone the folder is shared with |

Recordings already in a synced folder (for example OneDrive, where Teams saves them) are in that service before Unminute reads them.

## Retention and deletion

- **Two confirmations.** The audio goes when the user confirms the transcript is reviewed. The transcript, edits, speaker names and versions go when the user confirms the final document is saved. A minimal receipt is kept for 30 days.
- **Discard.** Before review is confirmed, a meeting can be discarded in one confirmation, which deletes its audio and text.
- **Holds** stop the matching cleanup until released, never expire, and are listed for review every 30 days while the app is open. Releasing a hold deletes nothing by itself.
- **Reminders** (**not in v0.5.0-preview.3**). On `main`, a meeting whose next step (review, hand off, or confirm or finish cleanup) has waited 30 days or more shows "Review due" on the Meetings page, with a banner, while the app is open; IT can change that number (`ReviewReminderDays`) and set a second one, "Action required" (`ActionRequiredDays`, off by default). Nothing is blocked or deleted, and a step a hold blocks shows no reminder. In v0.5.0-preview.3 there are no reminders for meetings waiting to be reviewed or handed off; the Meetings page shows each meeting's age and next step, and its only reminders are for holds due for review and cleanups waiting for a confirmation.
- **How deletion works.** Deleted database rows are overwritten, and final cleanup compacts the database. On `main`, a meeting's keys are destroyed at cleanup, so a leftover copy of its encrypted text cannot be opened once every copy of the key is gone. On `main` the audio key is destroyed when the audio is cleaned up, so the encrypted working copy cannot be opened afterwards either. On `main` a microphone recording's key is destroyed before its files are deleted, once it has become a meeting or when the user discards or deletes it. Audio files are still removed by ordinary deletion; on an SSD, old blocks are not guaranteed to be erased, but on `main` what is left is encrypted under a key that is gone.
- **What cleanup cannot reach:** copies in synced or backed-up folders, the original on a phone or recorder, cloud recycle bins, saved handoff files, the clipboard, and anything shared with an AI assistant.
- **The audit log** (on `main`) is not removed by cleanup: it keeps the record that a meeting was imported, handed off and deleted, with no content, up to its 16 MB cap. Its Event Log copy follows the Application log's retention.
- **Uninstalling** keeps the workspace, the models and the token. On `main`, IT can offboard a person as that user before they leave (`--offboard`, #111 B8a): it removes the workspace (with its logs and audit log), the models and the token, but only once no meeting, hold, unfinished cleanup, recording or meeting audio is left, so it never deletes content that the app's confirmations have not cleared. Anything else in the app's data folder, such as a workspace set aside after damage, is left for IT to deal with, and the command says how many such entries remain. It does not look at anything version 0.4.3 or earlier left. It erases the workspace's key first and records the offboarding in the Windows Event Log. It cannot reach backups or other copies made outside the app. On `main`, the uninstaller's `-Offboard` switch (`uninstall.ps1 -Offboard`, B8b), run as the person, offboards first in the same way and removes the program, its shortcut, its taskbar and Start pins and its uninstall entry only when offboarding finished; if offboarding is blocked, it keeps the program so the person can finish with it. It never runs as Local System, and runs only for an install folder the running account names as its own (the folder its uninstall entry names, or one in its own `%LOCALAPPDATA%`). [IT_DEPLOYMENT.md](IT_DEPLOYMENT.md), "Offboarding", has the steps, including what happens when the person has already left.

## Settings the organisation controls

Managed policy (`HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Unminute`) overrides the user's settings. The values that bear on personal information:

- `AllowMicrophoneRecording`: whether staff can record with the microphone at all.
- `AllowComputerAudioRecording` (**not in v0.5.0-preview.3**): whether staff may record the sound playing on the computer, such as the other side of a call, beside the microphone. Allowed when not set, but each user's switch is off by default (#108). With it on, everything the computer plays is recorded, including notification sounds, and the question before each recording names people on the call.
- `RequireRecordingConsent`: whether each recording needs the user to confirm, in the app's own window, that everyone has been told it is being recorded, and why. On unless set to 0.
- `AllowWatchedFolders` and `AllowDeleteOriginals`: whether folders can be watched, and whether originals can be deleted from them. Whatever the policy, very broad folders cannot be watched: a whole drive, the user's folder and the Windows and program folders, and on `main` also the folder that holds every user's profile (such as `C:\Users`), `AppData` and every folder in it, and Unminute's own data folder.
- `AllowNetworkSources`: whether recordings on mapped network drives can be imported.
- `ClipboardClearMinutes`: clears the app's own copy from the clipboard after a set time.
- `HandoffFilenameIncludesTitle` and `HandoffDefaultFolder`: the saved file's suggested name and starting folder.
- `AllowHandoffAssistant`, `HandoffAssistantName`, `HandoffAssistantUrl` (**not in v0.5.0-preview.3**): whether Copy and open is offered, and which assistant it opens.
- `AllowModelDownloads` and `AllowGpuComponent`: whether staff can download models and the graphics card components.
- `ReviewReminderDays` and `ActionRequiredDays` (**not in v0.5.0-preview.3**): after how many days a meeting's next step shows "Review due" (30 when not set) and "Action required" (off when not set). They remind; they delete nothing.
- `AllowLiveDrafts` (**not in v0.5.0-preview.3**): whether draft text while recording may be turned on (#104). On `main` it is off until the user turns it on in Settings. Set to 0, a draft in progress is stopped and cleared; the recording carries on.

A value name the app does not recognise, such as a misspelling, grants and withholds nothing; on `main` the app keeps the first 20 such names, never their data. On `main`, Settings lists them (#300) and Copy diagnostics says how many there are. The audit log has no policy value: it is always on. The full table is in [PACKAGING.md](PACKAGING.md), "Managed settings (policy)". Users can also replace speaker names with Speaker 1, Speaker 2, redact words and leave out passages for a single handoff.

## Known limits

From [SECURITY_MODEL.md](SECURITY_MODEL.md), "What the controls do not cover":

- Malware running as the user can unlock the encrypted content as the app does (in v0.5.0-preview.3 it can also read the unencrypted microphone recordings).
- A recording's journal stays unencrypted while the recording waits to be imported. It holds no audio or words, but its device names can name a person ("Alex's headset"). The owner accepted this as a documented limit.
- The decoder process runs at Low integrity, which stops it writing the user's files but not reading them. A recording crafted to exploit the decoder could read what the user can read, including the workspace and the key Windows protects for the user (SECURITY_MODEL.md T2; a decoder that cannot read the workspace is #299).
- Someone with administrator rights who runs code in the user's session can read what the user can read.
- Windows' protection of the key (DPAPI) is not tied to one computer in every set-up: with a roaming profile the key can be unlocked from another computer, whoever holds the domain's DPAPI backup keys can unlock it, and a disk image or profile backup together with the user's password may be enough. If Windows loses the user's protection keys (for example an administrator resets the password without the old one), the encrypted content cannot be opened again: on `main` the workspace then opens locked, reading and changing no meeting. Which of these applies to the organisation's laptops is to confirm on a pilot laptop (#189; [ENCRYPTION.md](ENCRYPTION.md), "What it does not do").
- The app's network guard covers its own Python code, not the native libraries it loads or WebView2's own processes. Whether WebView2 honours the switch that stops it resolving host names is not yet verified. A firewall rule for the app's runtime is planned (#111 B7).
- Whether Windows Error Reporting still writes or sends crash dumps of the app's processes, which could hold meeting content from memory, is to confirm on a managed device (SECURITY_MODEL.md T21; [IT_DEPLOYMENT.md](IT_DEPLOYMENT.md)).
- Once content is on the clipboard, in a saved file or with an assistant, the app no longer controls it.
- Windows may write memory to the page file or hibernation file; disk encryption protects these.
- Ordinary deletion is not forensic erasure.
- The user whose actions the audit log records can rebuild its local file, and on the device can push its Event Log copy out by filling the Application log. The copy is a stronger record only once IT forwards it off the device (SECURITY_MODEL.md T20).
- Computer audio, once a version offers it, records every sound played through every active output device (each speaker and headset that is on or connected, not only the Windows default one), such as notifications or a video in another tab. The recording support can also take one app's sound only (Windows 11); in a trial with the new Teams (25 October 2026) that gave silence, so the app does not use it (SECURITY_MODEL.md T35).
- The window can be seen in a screen share. With live draft text on (#104), that includes the draft; the recording card warns about this and offers Hide draft (SECURITY_MODEL.md T31).

## For the deploying organisation to complete

These are the organisation's decisions. The software has no view on them.

- The purpose of using Unminute, and which meetings it will and will not be used for.
- How people in a meeting are told it is being recorded or transcribed, and whether to keep `RequireRecordingConsent` on.
- Which AI assistant staff hand off to, under which account, where its provider keeps what it receives, and which connectors it may use.
- How long final documents are kept and where they are filed (outside Unminute).
- Who manages holds, and how often held content is reviewed.
- Which managed policy values to set, including whether staff may record computer audio, once a version offers it, and use draft text while recording.
- Whether to forward the Windows Application log off the device, who may read the audit log's Event Log copy, and how long it is kept.
- Whether recordings may come from synced or shared folders, and whether originals may be deleted from them.
- The device controls Unminute relies on: disk encryption, standard-user accounts, endpoint protection ([IT_DEPLOYMENT.md](IT_DEPLOYMENT.md)).
- How a person's request about information held about them is handled, given that content lives only in each user's own workspace until cleanup.
- The incident process for a lost laptop or content sent somewhere it should not go.
