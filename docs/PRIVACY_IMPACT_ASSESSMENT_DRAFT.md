# Privacy impact assessment: draft for the deploying organisation

**Status: draft.** This is a starting point for an organisation that deploys Unminute to write its own privacy impact assessment. It records facts about the software: what it stores, where, how it is protected, how long it is kept, who can reach it and what leaves the device. It does not assess those facts against any law, standard or policy, and it reaches no conclusions. That assessment, and every decision in the last section, belongs to the deploying organisation.

It describes the `main` branch as at 21 October 2026. Where the newest release, v0.5.0-preview.3 (20 October 2026), differs, the difference is stated. The technical detail behind each statement is in [SECURITY_MODEL.md](SECURITY_MODEL.md) (threats, controls and the data inventory) and [ENCRYPTION.md](ENCRYPTION.md); when they differ from this draft, they are the source.

## What Unminute does

Unminute turns a meeting recording into a reviewed transcript on the user's own Windows laptop. Transcription and speaker identification run on the laptop. The user reviews and corrects the transcript, then copies or saves it to hand to the organisation's AI assistant, which drafts the minutes. The app then deletes its own copies when the user confirms each cleanup step. It is a temporary workspace, not an archive.

## Personal information it handles

| Category | Examples | Comes from |
| --- | --- | --- |
| Voices and speech | The audio of everyone who speaks in a recorded or imported meeting | A recording the user imports, a watched folder, or the microphone |
| What was said | Transcript passages, word timings, edits and their history, the reviewed snapshot | Made on the laptop from the audio, then edited by the user |
| Names and labels typed by the user | Speaker names, meeting titles, hold reasons, redaction words, handoff templates | The user |
| File locations | The path of an imported recording, watched-folder paths (either can include a person's or client's name) | The user's choice of file or folder |
| Recording details | Microphone name, when recording started, paused and stopped, and a record that the user confirmed everyone was told (confirmed, wording version, time; no names) | The app, during a microphone recording |
| Usage metadata | When the app started and stopped, when errors happened, error codes and versions; raw crash files hold program folders, which include the Windows user name | The app's own logs, in the workspace's `logs` folder |
| A credential | The user's Hugging Face access token, if they choose to remember it | The user, in Setup |

The app does not collect contact details and keeps no account of its own. Speaker identification separates the voices in a recording; it does not work out who the speakers are. Names come only from what the user types.

## Where it is stored, and how it is protected

Everything the app keeps is on the laptop, under the signed-in user's profile; the table also lists where the originals and the handoff go, which may be elsewhere. `%LOCALAPPDATA%` is per user and is not synced by OneDrive; the app refuses to open its workspace if it has been redirected into OneDrive or onto a network drive.

| Data | Where | Encrypted by Unminute on `main` | In v0.5.0-preview.3 | Kept until |
| --- | --- | --- | --- | --- |
| Original recording | Wherever the user keeps it | No: the app reads it in place | Same | The user deletes it. The app deletes an original only from a watched folder whose deletion the user has authorised, after review is confirmed |
| Working copy of the audio, and `recording.json` (meeting title, microphone name, recording events, confirmation record) | `%LOCALAPPDATA%\Unminute\workspace\media\` | No (planned: #111 C3) | No | The user confirms the transcript is reviewed, or discards the meeting; longer while a hold applies |
| Microphone recording in progress or not yet imported | `workspace\recordings\` | No (planned: #111 C3) | No | Imported into a meeting; after a crash, until the user keeps or deletes it |
| Transcript, edits, history, snapshot, speaker names, titles, hold reasons, redaction words, file paths, and the error and restart notes the app writes about a meeting | `workspace\workspace.db` | Yes: AES-256-GCM under per-meeting keys, wrapped by a key Windows protects for this user (DPAPI). Times, states, counts and approximate lengths stay readable | No | The user confirms the final document is saved, or discards the meeting; longer while a hold applies |
| Handoff templates | `workspace\templates.sealed` | Yes, under the workspace key | No (`templates.json`, plain) | The user changes them |
| Fingerprints of recordings, to recognise a file already imported | `workspace.db` | Kept as keyed fingerprints (HMAC), not plain hashes | Plain hashes | A meeting's own: until its final cleanup. A watched folder's: 120 days |
| Cleanup receipts (a random number, times and the result) | `workspace.db` | Content-free | Same | 30 days |
| Settings | `workspace\settings.json` | No; settings only | Same | The user changes them |
| Logs and crash files | `workspace\logs\` | No; content-free by design | Same | Size-capped and rotated; crash files read and deleted at the next start |
| Hugging Face token | Windows Credential Manager | Protected by Windows (DPAPI) | Same | The user removes it; uninstalling keeps it |
| Copied handoff | The Windows clipboard | No. The Copy buttons ask Windows to keep it out of clipboard history and cloud clipboard (best effort: if that request fails, the copy still goes ahead) | Same | Replaced, or cleared after a set time if IT sets `ClipboardClearMinutes` |
| Saved handoff | Wherever the user saves it | No | Same | Not tracked by the app |
| Models | `%LOCALAPPDATA%\Unminute\models\` | Not personal information | Same | Removed in the app; uninstalling keeps them |

Disk encryption (BitLocker) is the organisation's control. While the laptop is off or hibernated it protects everything above that is stored on the laptop's disk, including what the app does not encrypt. It does not protect copies elsewhere, such as an original or a saved handoff in a synced or network folder.

## Who can reach it

- **The signed-in user** can see everything in their own workspace through the app.
- **Other standard users of the same laptop** cannot read the workspace: it inherits the user's profile permissions.
- **Administrators, SYSTEM services and backup agents** can read files the app does not encrypt (the working copy of the audio, recordings, settings, logs). On `main` they cannot read the encrypted database content without the user's Windows key protection, unless they run code as the user (an administrator of a device the user is signed in to can usually do this) or hold the domain's DPAPI backup key. In v0.5.0-preview.3 nothing is encrypted by the app.
- **Software running as the user**, including malware, can read what the user can read, encrypted or not.
- **IT** sets managed policy (below). Policy gives IT no access to content, and `--status`, `--health` and Copy diagnostics report counts, states and codes, never content.
- **The Unminute project** receives nothing: the app has no telemetry and no update check.

## What leaves the device

The app sends no meeting content itself, and every flow below needs a user action. The limits of the network controls are under "Known limits".

| Flow | Started by | What goes, and where |
| --- | --- | --- |
| Copy (handoff) | The user | The previewed prompt and transcript, to the Windows clipboard. Any app in the session, and a redirected remote-desktop clipboard, can read it |
| Copy and open (**not in v0.5.0-preview.3**) | The user | The same copy, then Windows opens one fixed address (chatgpt.com by default, or one IT sets) in the default browser. The address carries no content. The user pastes and sends |
| Save Markdown | The user | A file wherever the user chooses |
| Selecting text and copying it | The user | Ordinary clipboard copy, without the Copy button's protections |
| Pasting into an AI assistant | The user, outside the app | Whatever the user pastes or uploads goes to that assistant's provider, under the organisation's account. The built-in templates ask the assistant to gather more context with its connected tools, so what those tools read also goes to that provider |
| Model downloads | The user, in Setup or Manage downloads | HTTPS requests for fixed model files to huggingface.co and its download hosts, download-r2.pytorch.org (graphics card component) and files.pythonhosted.org (graphics card component for Whisper). The Hugging Face token is sent for the speaker model download. No meeting content. The hosts see the requests and the network address they come from |
| Help pages | The user | Windows opens a fixed help address in the browser; nothing from the meeting is in it |
| Copy diagnostics | The user | Versions, settings, counts and error locations, with no content, names or folders, to the clipboard for a support request |
| Deleting a watched original | The user, after authorising that folder and confirming review | The original file is deleted. In a synced folder, the deletion syncs to the cloud and to everyone the folder is shared with |

Recordings already in a synced folder (for example OneDrive, where Teams saves them) are in that service before Unminute reads them.

## Retention and deletion

- **Two confirmations.** The audio goes when the user confirms the transcript is reviewed. The transcript, edits, speaker names and versions go when the user confirms the final document is saved. A minimal receipt is kept for 30 days.
- **Discard.** Before review is confirmed, a meeting can be discarded in one confirmation, which deletes its audio and text.
- **Holds** stop the matching cleanup until released, never expire, and are listed for review every 30 days while the app is open. Releasing a hold deletes nothing by itself.
- **No reminders** for unreviewed meetings yet (planned: #111 C6). The Meetings page shows each meeting's age and next step.
- **How deletion works.** Deleted database rows are overwritten, and final cleanup compacts the database. On `main`, a meeting's keys are destroyed at cleanup, so a leftover copy of its encrypted text cannot be opened once every copy of the key is gone. Audio files are removed by ordinary deletion; on an SSD, old blocks are not guaranteed to be erased.
- **What cleanup cannot reach:** copies in synced or backed-up folders, the original on a phone or recorder, cloud recycle bins, saved handoff files, the clipboard, and anything shared with an AI assistant.
- **Uninstalling** keeps the workspace, the models and the token. There is no offboarding command yet (planned: #111 B8); [IT_DEPLOYMENT.md](IT_DEPLOYMENT.md) has today's steps.

## Settings the organisation controls

Managed policy (`HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Unminute`) overrides the user's settings. The values that bear on personal information:

- `AllowMicrophoneRecording`: whether staff can record with the microphone at all.
- `RequireRecordingConsent`: whether each recording needs the user to confirm, in the app's own window, that everyone has been told it is being recorded, and why. On unless set to 0.
- `AllowWatchedFolders` and `AllowDeleteOriginals`: whether folders can be watched, and whether originals can be deleted from them.
- `AllowNetworkSources`: whether recordings on mapped network drives can be imported.
- `ClipboardClearMinutes`: clears the app's own copy from the clipboard after a set time.
- `HandoffFilenameIncludesTitle` and `HandoffDefaultFolder`: the saved file's suggested name and starting folder.
- `AllowHandoffAssistant`, `HandoffAssistantName`, `HandoffAssistantUrl` (**not in v0.5.0-preview.3**): whether Copy and open is offered, and which assistant it opens.
- `AllowModelDownloads` and `AllowGpuComponent`: whether staff can download models and the graphics card components.

The full table is in [PACKAGING.md](PACKAGING.md), "Managed settings (policy)". Users can also replace speaker names with Speaker 1, Speaker 2, redact words and leave out passages for a single handoff.

## Known limits

From [SECURITY_MODEL.md](SECURITY_MODEL.md), "What the controls do not cover":

- Malware running as the user can unlock the encrypted content as the app does, and can read the unencrypted audio.
- Someone with administrator rights who runs code in the user's session can read what the user can read.
- Windows' protection of the key (DPAPI) is not tied to one computer in every set-up: with a roaming profile the key can be unlocked from another computer, whoever holds the domain's DPAPI backup keys can unlock it, and a disk image or profile backup together with the user's password may be enough. If Windows loses the user's protection keys (for example an administrator resets the password without the old one), the encrypted content cannot be opened again. Which of these applies to the organisation's laptops is to confirm on a pilot laptop (#189; [ENCRYPTION.md](ENCRYPTION.md), "What it does not do").
- The app's network guard covers its own Python code, not the native libraries it loads or WebView2's own processes. Whether WebView2 honours the switch that stops it resolving host names is not yet verified. A firewall rule for the app's runtime is planned (#111 B7).
- Whether Windows Error Reporting still writes or sends crash dumps of the app's processes, which could hold meeting content from memory, is to confirm on a managed device (SECURITY_MODEL.md T21; [IT_DEPLOYMENT.md](IT_DEPLOYMENT.md)).
- Once content is on the clipboard, in a saved file or with an assistant, the app no longer controls it.
- Windows may write memory to the page file or hibernation file; disk encryption protects these.
- Ordinary deletion is not forensic erasure.
- The window can be seen in a screen share.

## For the deploying organisation to complete

These are the organisation's decisions. The software has no view on them.

- The purpose of using Unminute, and which meetings it will and will not be used for.
- How people in a meeting are told it is being recorded or transcribed, and whether to keep `RequireRecordingConsent` on.
- Which AI assistant staff hand off to, under which account, where its provider keeps what it receives, and which connectors it may use.
- How long final documents are kept and where they are filed (outside Unminute).
- Who manages holds, and how often held content is reviewed.
- Which managed policy values to set.
- Whether recordings may come from synced or shared folders, and whether originals may be deleted from them.
- The device controls Unminute relies on: disk encryption, standard-user accounts, endpoint protection ([IT_DEPLOYMENT.md](IT_DEPLOYMENT.md)).
- How a person's request about information held about them is handled, given that content lives only in each user's own workspace until cleanup.
- The incident process for a lost laptop or content sent somewhere it should not go.
