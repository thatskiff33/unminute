# Unminute: user guide

Unminute turns a meeting recording into a reviewed transcript on your own computer. You then hand the transcript to your organisation's approved AI assistant to draft minutes, and clear the meeting out of the workspace.

It is a temporary workspace, not an archive. Your final documents live wherever your organisation files them.

This guide covers the packaged workspace. The earlier source-installed app (`install.cmd` at the top of the repository) still works and is described in [EARLIER_APP.md](EARLIER_APP.md). The two keep separate workspaces and outputs. They share the downloaded models: if a shared model fails verification, Repair (Settings > Manage downloads) downloads a fresh copy and keeps the damaged one aside. Nothing the earlier app created is ever deleted.

## What stays on your computer, and what does not

| | Where it is |
| --- | --- |
| **Recordings you import** | Read where they are. The app keeps its own working copy of the audio until you confirm the transcript is reviewed. |
| **Transcription and speaker identification** | Done on this computer. Nothing is uploaded. While processing, the app refuses its own Python network connections; for managed devices, an outbound firewall rule is the stronger control. |
| **Transcripts, edits, speaker names, holds** | In the workspace on this computer (`%LOCALAPPDATA%\whisper\workspace`). This is not a synced folder. |
| **Models** | Downloaded once, when you choose, from Hugging Face. Each file is checked against a published checksum. They are stored in `%LOCALAPPDATA%\whisper-models`. |
| **Handoff to an AI assistant** | Only when you copy or save it. What you paste or upload is then handled by that assistant under your organisation's account and policies, not by this app. |
| **Watched folders and synced storage** | If a watched folder is synced (for example OneDrive), its recordings are already in that cloud service. The app does not change that. |

## Getting started

1. **Install.** Your IT team may install the app for you. Otherwise, run `install.cmd` from the package. It installs for your Windows account only and needs no administrator rights.
2. **Set up.** On first start, Setup takes you through five steps:
   - **Speech model:** Parakeet v2 is recommended (about 0.7 GB). Other models are under Other options.
   - **Speaker names** (optional): speaker identification needs a free Hugging Face account and token, once. Follow the steps on the screen. The token can be remembered in Windows Credential Manager and you can remove it at any time. Already-downloaded models keep working without it.
   - **Microphone** (optional): turn on recording and test the microphone.
   - You can skip the optional steps and turn them on later in Settings. Settings > **Manage downloads** adds or repairs models later.
3. **Import a recording.** On the Meetings page, choose **Import a recording** or drop audio or video files anywhere on the window. You can also watch a folder (Settings > Watched folders, see [INTAKE.md](INTAKE.md)). Recordings on mapped network drives are refused: copy them to this computer first. OneDrive and other synced folders are fine. A file whose content does not match a supported type (for example AIFF audio renamed to .wav) is refused as unsupported.

## Recording with a microphone

Microphone recording is off until you turn it on, in Setup or in Settings > Recording and speakers. The Meetings page then shows **Record a meeting**, with the microphone in use and **Change or test** beneath it.

1. **Choose and test the microphone.** **Change or test** lists the microphones. **Test microphone** listens for 3 seconds and shows the level; nothing is kept.
2. **Tell everyone present that the meeting is being recorded.**
3. **Start recording.** The recording card shows the length and a live curve of the sound. Settings > Recording and speakers chooses the curve: Flowing wave, Spectrum or Sound wave. **Pause** and **Resume** leave a gap that is not recorded.
4. **Stop and save.** The recording becomes a meeting and is transcribed like an imported file. Review, holds and cleanup work the same way. To throw a recording away instead (a test, or the wrong meeting), use **Discard…** while recording or paused: after a confirmation the audio is deleted and nothing is transcribed.

How it handles problems:

- **Saved as it goes.** Audio is saved to disk every few seconds while recording. If the app or computer stops unexpectedly, the next start lists it as a recovered recording: choose **Keep and transcribe** (it then reports about how much at the end could not be saved) or **Delete…**.
- **Closing asks first.** Closing the window while recording asks whether to stop and save, discard, or keep recording.
- **Gaps are reported, never hidden.** If the computer falls behind or the disk fills up, the recording says when audio was lost and, where known, for how long.
- **The microphone never changes on its own.** If the microphone is disconnected, recording stops and keeps what was saved. It does not move to another microphone.
- **Other work waits.** Transcription of other meetings pauses while you record, so recording gets the computer's full attention. It continues afterwards.
- **Only the microphone.** Sound playing on the computer (for example the other side of a Teams call) is not recorded, and there is no live transcription.
- **Stays local.** Until it is imported, a recording is kept in the workspace folder. An update that changes the storage format waits for it, as it does for meetings.

## Working on a meeting

- **Transcription runs in the background.**
  - **Quiet** (the default) leaves most of the computer free. **Faster** uses more of it.
  - On battery, automatic work waits unless you allow it in Settings.
  - **Pause** stops at the next safe point. **Stop now** stops at once; the part in progress is redone later and nothing already transcribed is lost.
- **Find your meetings.** The Meetings page groups them by what they need next: **Needs attention**, **Ready for you**, **Transcribing** and **Cleaning up**. The number beside Meetings in the sidebar counts the meetings waiting for you.
- **Review.** Open a meeting (select its title or **Review**). Its steps (Transcribe, Review, Hand off, Clean up) run across the top, with the next action beside them.
  - Select a time to play from that passage. The player at the bottom carries on through the meeting and the transcript follows along. While the audio is kept, Space plays or pauses and J and K move between passages.
  - Select text, or press Enter, to correct it. Edits save automatically, and Ctrl+Z reverses them, even after a restart.
  - Search with Ctrl+F.
- **Skip speaker identification** when it is not needed, for example a recording of one or two people. Select **Skip speakers** on the meeting's row while it is working out who spoke, or **Skip speaker identification** in the meeting's speaker panel (also before the text is ready). Any pass in progress stops at once and the meeting goes straight to review and hand off, with no speaker labels. While the audio is kept you can still use **Identify again…** later. Transcribing the meeting again keeps the choice.
- **Name speakers.** **Name speakers** asks "Who's speaking?" and plays short examples for each detected speaker so you can name them. You can merge two labels that are the same person, or move a single passage to another speaker. Names stay with this meeting only.
- **Transcribe again.** The **Versions** tab can transcribe again with another model. Your earlier version and its edits are kept, and you can still open them read-only.
- **Shortcuts.** **Help and shortcuts** in the sidebar lists them all. Press ? (outside a text field) to open it.

## Handing off and cleaning up

**Recordings you do not need** (a test, or the wrong file) can be removed with **Discard…** on the meeting's row. This works only before the transcript review is confirmed. One confirmation deletes the meeting's audio, transcript, edits and speaker names, with the same secure cleanup as below. Any work on it stops first. Discard is refused while a hold is active, and your original recording file is never deleted. Once a review is confirmed, the meeting is finished with the final confirmation instead.

Each meeting shows its steps:

1. **Confirm the transcript is reviewed.**
   - The reviewed text is saved in the workspace.
   - The app then removes its own copy of the audio. It also removes the original recording, but only if you allowed that for the watched folder it came from.
   - Playback and editing are no longer available after this step.
2. **Hand off.**
   - **Hand off…** opens a page that shows exactly what will be copied, with three ways out: **Copy prompt and transcript**, **Copy prompt only**, and **Save Markdown…**.
   - The templates ask the assistant to keep decisions separate from suggestions, not to invent owners or dates, and to flag anything uncertain. **Edit template** and **New template** let you adjust them.
   - For this hand off only, you can replace names with Speaker 1, Speaker 2, redact words, or leave out passages. Select **Apply** to update the preview; copying waits until changes are applied.
   - Long transcripts are never cut short. The app suggests saving the file and attaching it instead.
3. **Create the final document** in your assistant, review it yourself, and file it where your organisation keeps such documents.
4. **Confirm the final document is saved.** The app then removes the meeting's transcript working content: text, edits, speakers and versions. A minimal receipt (a random number, times and the result) is kept for 30 days.

Copying or saving never deletes anything by itself. The app does not clear your clipboard or delete files you saved. Replace the clipboard and delete saved handoff files yourself when you no longer need them.

**What cleanup cannot reach.** Cleanup removes the app's own copies on this computer. It cannot remove:

- copies in synced or backed-up folders;
- the original on a phone or recorder;
- copies in a cloud recycle bin;
- anything you shared with an AI assistant or another service.

Ordinary deletion is also not forensic erasure of the disk.

## Keeping material (holds)

Anyone can **hold** a meeting's audio, transcript or everything, with a reason, from the meeting's **Holds** tab. A hold:

- stops the matching cleanup until it is released;
- never expires by itself;
- is listed for review every 30 days while the app is open (a closed app sends no reminders).

Releasing a hold deletes nothing by itself. The cleanup it was blocking waits for you to confirm it again.

Ordinary unfinished work shows its age and next step on the Meetings page. There are no scheduled reminders.

## Updates

Self-service updates and IT-managed updates follow the same rule: an update that changes how the workspace is stored waits until the workspace is empty. That means:

- no meetings in progress;
- no active holds;
- no unfinished cleanup;
- no microphone recording still waiting to be imported.

The installer explains what it is waiting for, and so does Settings > Updates in the app. No update expires a hold, deletes your work or makes a backup copy of it. Updates that keep the same storage format keep everything.

## Troubleshooting and support

| Problem | What to do |
| --- | --- |
| A model shows "Damaged" or "Needs attention" | Settings > **Manage downloads** (the Downloads page) > **Repair**. The damaged copy is kept aside, not deleted. **Check every file** re-checks a model that looks ready. |
| Speaker identification did not work | The transcript is still usable. Check the speaker model in Settings > **Manage downloads**, then use **Identify again…** in the meeting's Speakers tab. |
| A watched recording is not imported | Look at the folder in Settings > Watched folders. It shows files waiting for copying to finish, cloud-only files and unreadable files. |
| The app says the computer is on battery | Plug in, or allow processing on battery in Settings. |
| An update is waiting | Settings > Updates lists what it is waiting for. |

For IT support:

- `runtime\python.exe -m workspace --self-test` in the installed version folder imports every dependency and confirms the network block. It does not check models: use Settings > Manage downloads for those.
- `--status` prints counts only (meetings, holds, pending cleanup), never content.

Deployment, detection and signing are covered in [PACKAGING.md](PACKAGING.md).

## Supported configuration

These configurations have been tested:

- **Windows:** 64-bit Windows 11 on an x64 processor, CPU processing (Quiet and Faster), with the Edge WebView2 Runtime.
- **Hardware:** the minimum is 4 processor cores and 8 GB of memory, and 8 cores and 16 GB are recommended. So far it has only been tested on a faster laptop (#28).
- **Speech models:** Parakeet v2 (recommended) was tested in the workspace. Parakeet v3 and Whisper large-v3 turbo were tested in the earlier app's engine only; in that comparison Parakeet v3 dropped speech on some clips.
- **Speaker identification:** pyannote community-1.

**Graphics card (optional, speaker identification only).** On a computer with an NVIDIA card, Settings > Manage downloads offers "Graphics card acceleration". This is a one-time 1.92 GB download of the CUDA build of PyTorch from the PyTorch download site, checked against published checksums. Once it is downloaded, turn on Settings > Recording and speakers > "Use the graphics card".

- **When it is used:** only in Faster mode. Transcription always runs on the processor.
- **What it needs:** an NVIDIA driver from version 580. Nothing else is installed on the computer and drivers are never changed.
- **If the card cannot be used** (an old driver, too little graphics memory or a damaged component), that speaker pass runs on the processor instead, and the meeting says so.
- **Each meeting shows** whether speaker identification ran on the graphics card.

Detecting a graphics card does not enable anything on its own.

See [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) for what is verified for a release and what still needs people or hardware.

For how the workspace is built and planned, see [WORKSPACE_PLAN.md](WORKSPACE_PLAN.md), [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) and [WORKSPACE_DESIGN.md](WORKSPACE_DESIGN.md).
