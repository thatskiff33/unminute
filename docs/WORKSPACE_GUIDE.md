# Unminute: user guide

Unminute turns a meeting recording into a reviewed transcript on your own computer. You then hand the transcript to your organisation's approved AI assistant to draft minutes, and clear the meeting out of the workspace.

It is a temporary workspace, not an archive. Your final documents live wherever your organisation files them.

This guide covers the packaged workspace, as it is in the project's latest code. The newest release is 0.5.0-preview.3 (20 October 2026). These parts of the guide are not in that release yet, and arrive with the next one (see [CHANGELOG.md](../CHANGELOG.md)):

- **Copy and open ChatGPT** on the Hand off page, and IT's option to rename it, point it at another assistant or turn it off. In 0.5.0-preview.3 the main button is **Copy prompt and transcript**.
- **Encryption of the workspace.** Transcripts, edits, speaker names and other meeting text in the workspace database, your handoff templates, the app's working copy of each meeting's audio and microphone recordings as they are saved are encrypted with a key Windows keeps for your account. In 0.5.0-preview.3 the app encrypts nothing itself; your organisation's disk encryption (BitLocker) protects the workspace. With encryption come the "[Could not be read]" marker and the locked workspace in [If Unminute cannot unlock its workspace](#if-unminute-cannot-unlock-its-workspace).
- **The message for a workspace folder linked to a folder that cannot be reached.** In 0.5.0-preview.3 Unminute closed at start without saying why.
- **The log of what was done with each meeting**, in [What Unminute records about your work](#what-unminute-records-about-your-work). 0.5.0-preview.3 keeps only the short cleanup receipts, for 30 days.
- **The message when Unminute is already open in another Windows session of yours**, in [Troubleshooting and support](#troubleshooting-and-support). 0.5.0-preview.3 does not check for this.
- **Stricter checks on recordings.** Recordings are decoded with audio decoders only, and a file with a sample rate below 4 kHz or above 384 kHz, or one that would decode to far more audio than its size can hold, is refused (see step 3 of [Getting started](#getting-started)). 0.5.0-preview.3 does not refuse these.
- **Stricter watched folders.** The folder that holds every user's profile (such as `C:\Users`), `AppData` and every folder in it, and Unminute's own data folder can no longer be watched, and a recording whose path is too long for Windows to open is counted in the folder's note while the rest of the folder is imported. In 0.5.0-preview.3 those folders can be watched, and one path that is too long can stop a folder from importing anything.
- **The recording display redraws about 60 times a second**, rather than on every refresh of the screen, to use less of the processor. On a 120 Hz screen that is every second refresh, and it moves as before. On a 60 Hz screen it now settles about twice as quickly as in 0.5.0-preview.3, as it does on a 120 Hz screen.

## What stays on your computer, and what does not

| | Where it is |
| --- | --- |
| **Recordings you import** | Read where they are. The app keeps its own working copy of the audio until you confirm the transcript is reviewed. |
| **Transcription and speaker identification** | Done on this computer. Nothing is uploaded. While processing, the app refuses its own Python network connections; for managed devices, an outbound firewall rule is the stronger control. |
| **Transcripts, edits, speaker names, holds** | In the workspace on this computer (`%LOCALAPPDATA%\Unminute\workspace`), encrypted with a key that Windows keeps for your account on this computer (not in 0.5.0-preview.3, see above). This is not a synced folder. The app's working copy of each meeting's audio, and each microphone recording as it is saved, are encrypted the same way. Your settings are not encrypted. |
| **Models** | Downloaded once, when you choose, from Hugging Face. Each file is checked against a published checksum. They are stored in `%LOCALAPPDATA%\Unminute\models`. |
| **Handoff to an AI assistant** | Only when you copy or save it. **Copy and open** also opens the assistant's web page in your browser, but sends nothing: you paste and send. What you paste or upload is then handled by that assistant under your organisation's account and policies, not by this app. |
| **Watched folders and synced storage** | If a watched folder is synced (for example OneDrive), its recordings are already in that cloud service. The app does not change that. |

## What Unminute records about your work

Unminute keeps a log of what is done with each meeting: when it was imported or recorded (and that you confirmed everyone was told), when you confirmed the transcript review and the final save, when you copied, saved or opened a handoff, when holds were placed, kept or released, when watched folders and permission to delete originals changed, and when anything was deleted. Each entry says what kind of action it was and when, with the app's own reference numbers. It never holds titles, names, transcript text, file or folder names, or hold reasons. Imports, recordings and deletions are always recorded one by one. If the same action in the window is repeated very quickly (more than 20 times a minute), the extra ones are kept only as a count. If a deletion keeps failing, the log records its first attempt and the first after you choose Try again, not every automatic retry. A fingerprint of each entry, with your Windows account, is also written to the Windows event log, which your organisation's IT team may collect.

## Getting started

1. **Install.** Your IT team may install the app for you. Otherwise, run `install.cmd` from the package. It installs for your Windows account only and needs no administrator rights.
2. **Set up.** On first start, Setup takes you through five steps:
   - **Speech model:** Parakeet v2 is recommended (about 0.7 GB). Parakeet v3, Whisper large-v3 turbo and Whisper large-v3 are under Other options. Whisper large-v3 is the largest download, so expect it to be the slowest on a processor.
   - **Speaker names** (optional): speaker identification needs a free Hugging Face account and token, once. Follow the steps on the screen. The token can be remembered in Windows Credential Manager and you can remove it at any time. Already-downloaded models keep working without it.
   - **Microphone** (optional): turn on recording and test the microphone.
   - You can skip the optional steps and turn them on later in Settings. Settings > **Manage downloads** adds or repairs models later. Settings > **Speech model** marks a model that is not downloaded or is damaged.
3. **Import a recording.** On the Meetings page, choose **Import a recording** or drop audio or video files anywhere on the window. You can also watch a folder (Settings > Watched folders, see [INTAKE.md](INTAKE.md)). Recordings on mapped network drives are refused: copy them to this computer first. OneDrive and other synced folders are fine. A file whose content does not match a supported type (for example AIFF audio renamed to .wav) is refused as unsupported, and so is one whose audio uses a codec Unminute does not decode or a sample rate below 4 kHz or above 384 kHz. A file that would decode to far more audio than its size can hold (more than a second per 16 bytes) is refused as possibly damaged.

## Recording with a microphone

Microphone recording is off until you turn it on, in Setup or in Settings > Recording and speakers. The Meetings page then shows **Record a meeting**, with the microphone in use and **Change or test** beneath it.

1. **Choose and test the microphone.** **Change or test** lists the microphones. **Test microphone** listens for 3 seconds and shows the level; nothing is kept.
2. **Tell everyone present that the meeting is being recorded, and why.** When you start, Unminute asks you to tick that everyone has been told, then select **Start recording** (it becomes available after a moment; with the keyboard, Tab to it, pause briefly, then press Enter; Enter anywhere else means Cancel). Your organisation can turn this question off.
3. **Start recording.** The recording card shows the length and a live curve of the sound. While recording, the window title starts with **● Recording** and the app's taskbar button shows a red dot. Settings > Recording and speakers chooses the curve: Flowing wave, Spectrum or Sound wave. **Pause** and **Resume** leave a gap that is not recorded.
4. **Stop and save.** The recording becomes a meeting and is transcribed like an imported file. Review, holds and cleanup work the same way. To throw a recording away instead (a test, or the wrong meeting), use **Discard…** while recording or paused: after a confirmation the audio is deleted and nothing is transcribed.

How it handles problems:

- **Saved as it goes.** Audio is saved to disk every few seconds while recording, encrypted with a key that Windows keeps for your account. Once the recording has become a meeting ready to transcribe, or when you discard or delete it, that key is deleted before the saved files, so anything left on disk cannot be read. If the app or computer stops unexpectedly, the next start lists it as a recovered recording: choose **Keep and transcribe** (it then reports about how much at the end could not be saved) or **Delete…**.
- **Closing asks first.** Closing the window while recording asks whether to stop and save, discard, or keep recording.
- **Gaps are reported, never hidden.** If the computer falls behind or the disk fills up, the recording says when audio was lost and, where known, for how long.
- **The microphone never changes on its own.** If the microphone is disconnected, recording stops and keeps what was saved. It does not move to another microphone.
- **Stays awake while recording.** The computer does not go to sleep on its idle timer while a recording is open, including while it is paused, even if nobody touches it. This applies unless your organisation's power settings prevent it. On some laptops running on battery, Windows still sleeps a few minutes after its usual sleep time.
- **Closing the lid.** Windows decides what closing the lid does.
  - If the lid is set to Sleep, closing it stops the recording. The app keeps what was recorded and says the computer went to sleep.
  - To keep recording with the lid closed, turn on **Keep recording when the lid is closed** in Settings > Recording and speakers. While a recording is open, Unminute then sets Windows to do nothing when the lid is closed, and puts your setting back when the recording ends (or when the app next starts, if it closed unexpectedly). Uninstalling Unminute also puts it back, before anything is removed; if it cannot, the uninstaller says to check the setting. If Unminute does not start again after closing unexpectedly, set the lid back yourself in Windows Settings > System > Power & battery > Lid & power button controls. If Unminute cannot put your setting back, it tells you. If your organisation sets what closing the lid does, Unminute leaves it alone and the setting is unavailable; ask your IT team. You can also set Windows to do nothing yourself (Settings > System > Power & battery > Lid & power button controls). Some laptops switch off their built-in microphone when the lid is closed; use an external microphone for those.
  - With the lid set to do nothing, the recording carries on until you stop it, and its marks cannot be seen while the lid is shut. When you open the lid again, the recording card says how long the recording carried on with the lid closed. When the meeting ends, open the lid and stop it in Unminute, and never put a laptop that is still running in a bag.
  - These lid and sleep behaviours come from Windows' documentation and have not yet been tested on a real laptop.
- **Other work waits.** Transcription of other meetings pauses while you record, so recording gets the computer's full attention. It continues afterwards.
- **Recording uses more processor time with the window visible.** It uses about 23% of one processor core while the window is visible, mostly to draw the moving recording display, and under 1% minimised (measured on an Intel Core Ultra 7 255H laptop with a 60 Hz screen on 23 October 2026). Minimise the window while you record if you do not need to watch it.
- **Only the microphone.** Sound playing on the computer (for example the other side of a Teams call) is not recorded.

### Draft text while recording

Unminute can show rough text of what is said while you record, a few seconds behind. It is off until you turn on **Show draft text while recording** in Settings > Recording and speakers, and your organisation can turn it off.

- **Rough, and replaced.** The draft is made on this computer with the Parakeet v2 speech model (download it in Settings > Manage downloads if it is not there). It is never saved: when you select **Stop and save**, it goes, and the meeting is transcribed in full as usual. Discarding the recording, or the app closing, also clears it.
- **Where it shows.** Under the recording controls, in grey. You cannot select or copy it: check the full transcript instead.
- **Screen sharing.** Draft text shows on your screen. Select **Hide draft** before you share your screen; it stays hidden for the rest of that recording unless you select **Show draft**.
- **On battery** it pauses, unless **Process automatically on battery power** is on; it carries on when you plug in again. The recording itself is never affected.
- **It costs some memory and processor time** while recording. If it falls behind, it skips the oldest part and says so; the recording is still complete.
- **With computer audio** (a later version records it beside the microphone), you choose whether the draft follows your microphone only or both mixed. The full transcript always uses both.
- **When there is no draft**, a line in the draft area says why: Parakeet v2 is not downloaded or needs repair; draft text waits while a speech model is downloaded or removed; draft text stopped for this recording because a speech model was downloaded or removed; or your organisation has turned it off. The recording is not affected.
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
- **Skip speaker identification** when it is not needed, for example a recording of one or two people. Select **Skip speakers** on the meeting's row while it is working out who spoke (or paused there), or **Skip speaker identification** in the meeting's speaker panel (also before the text is ready). Any pass in progress, or about to start, stops at once and the meeting goes straight to review and hand off, with no speaker labels. Skip is not offered once speakers are identified. While the audio is kept you can still use **Identify again…** later, and Skip stops that too. Transcribing the meeting again keeps the choice.
- **Name speakers.** **Name speakers** asks "Who's speaking?" and plays short examples for each detected speaker so you can name them. You can merge two labels that are the same person, or move a single passage to another speaker. Names stay with this meeting only.
- **Transcribe again.** The **Versions** tab can transcribe again with another model. Your earlier version and its edits are kept, and you can still open them read-only.
- **A meeting that stopped.** A meeting in **Needs attention** shows why it stopped. **Try again** retries with the same model, which suits a passing problem or a model you have since downloaded or repaired. **Transcribe with another model…** transcribes it again with the model now chosen in Settings > **Speech model**: choose the model there first. Parts already finished are kept as an earlier version. If the chosen model is not downloaded or is damaged, the app says so and offers **Manage downloads** instead.
- **Shortcuts.** **Help and shortcuts** in the sidebar lists them all. Press ? (outside a text field) to open it.

## Handing off and cleaning up

**Recordings you do not need** (a test, or the wrong file) can be removed with **Discard…** on the meeting's row. This works only before the transcript review is confirmed. One confirmation deletes the meeting's audio, transcript, edits and speaker names, with the same secure cleanup as below. Any work on it stops first. Discard is refused while a hold is active, and your original recording file is never deleted. A meeting whose import never finished shows **Import did not finish** (for example after **Continue anyway** on a damaged workspace, or when a hold kept it) and can be discarded once no import is running and any hold is released. Releasing a hold on it removes nothing by itself. If the workspace is damaged, a removal's receipt may say the database compaction is deferred: the meeting's text and audio are removed, and the space is reclaimed at the next start once the workspace is sound. Once a review is confirmed, the meeting is finished with the final confirmation instead.

Each meeting shows its steps:

1. **Confirm the transcript is reviewed.**
   - The reviewed text is saved in the workspace.
   - The app then removes its own copy of the audio. First it deletes the audio's key, so the copy cannot be read any more, even if another program is still holding the file; then it deletes the files. It also removes the original recording, but only if you allowed that for the watched folder it came from.
   - Playback and editing are no longer available after this step.
2. **Hand off.**
   - **Hand off…** opens a page that shows exactly what will be copied, with four ways out: **Copy and open ChatGPT** (the main button), **Copy prompt and transcript**, **Copy prompt only**, and **Save Markdown…**.
   - **Copy and open ChatGPT** copies the prompt and transcript, then opens chatgpt.com in your default browser. Sign in with your organisation's business account, not a personal one (Unminute cannot see which account is signed in), paste with Ctrl+V, check it, then send. Unminute sends nothing, puts nothing from the meeting in the web address and does not type into the browser. If no browser appears, open chatgpt.com yourself and paste. Your IT team may have renamed the button for another assistant (for example "Copy and open Copilot"), or turned it off; Settings then says so.
   - Templates: Minutes and actions, Short summary, Action list, Board or directors' minutes, Client meeting file note, Follow-up email, and Team or WIP notes. **Edit template** and **New template** let you adjust them.
   - Each built-in template asks the assistant to:
     - gather what else it can about the meeting from its connected tools and the project it is in;
     - write a clean document as a minute-taker would, with no timestamps, correcting misheard words from context;
     - show the draft in the chat with a short "Points to check" list of anything it inferred;
     - revise it with you.
   - When the draft is final, the assistant offers a Word document in your organisation's brand template. It gives you a `context.md` file and a prompt to use in a session that can create Word documents. The follow-up email is given ready to copy instead.
   - Every hand off, including your own templates, also asks the assistant to use its tools only to read, to change nothing unless you ask in the chat, and to treat what its tools return as information, not instructions. These are requests, not guarantees: check what the assistant did before relying on it.
   - If you replace names with Speaker 1, Speaker 2, the prompt also asks the assistant to work from the transcript alone: not to look up attendees, emails, chats or documents, or work out who the speakers are, and to leave names out.
   - For this hand off only, you can replace names with Speaker 1, Speaker 2, redact words, or leave out passages. Select **Apply** to update the preview; copying waits until changes are applied.
   - Long transcripts are never cut short. The app suggests saving the file and attaching it instead.
3. **Create the final document** in your assistant. Check the points it lists, review the document yourself, and file it where your organisation keeps such documents.
4. **Confirm the final document is saved.** The app then removes the meeting's transcript working content: text, edits, speakers and versions. A minimal receipt (a random number, times and the result) is kept for 30 days.

Copying or saving never deletes anything by itself. The app does not delete files you saved, and it clears your clipboard only if your organisation has set it to remove the app's own copies after a set time. Replace the clipboard and delete saved handoff files yourself when you no longer need them.

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

**From version 0.4.3 or earlier, this is not an update in place.** The new version starts with an empty workspace and does not see your meetings in the earlier one. So, in order:

1. In the earlier version: hand off or copy anything you still need, confirm the cleanup of finished meetings, check any holds with whoever manages them, and remove the Hugging Face token (in 0.4, Settings > Manage downloads > Remove the remembered token; in 0.2 or 0.3, Setup > Remove the remembered token). Settings, handoff templates and watched folders do not carry over either, so note your settings and watched folders (including which ones may delete originals), and copy the text of any handoff template you edited or wrote.
2. Uninstall the earlier version in Settings > Apps.
3. Install the new version.
4. In the new version's Setup, download the models again and enter the token again. Then set up your settings, handoff templates and watched folders again. When you add a watched folder again, leave "Also import recordings already in it" unticked, so recordings you finished in the earlier version are not imported again. If you used the graphics card, download the components you used ("Graphics card acceleration", "Graphics card for Whisper" or both) again in Settings > Manage downloads and turn "Use the graphics card" back on.

The earlier version's workspace and models stay on the computer until they are deleted. [PACKAGING.md](PACKAGING.md) ("Updates") says where they are, and what to do if you installed in the other order.

## Troubleshooting and support

| Problem | What to do |
| --- | --- |
| A model shows "Damaged" or "Needs attention" | Settings > **Manage downloads** (the Downloads page) > **Repair**. The damaged copy is kept aside, not deleted. **Check every file** re-checks a model that looks ready. |
| Settings > Speech model marks a model "(not downloaded)" or "(damaged)" | It is not ready to use, and the note beside the picker says so. Your choice is saved: download or repair the model in **Manage downloads**, or choose a model that is ready. When your organisation has turned downloads off, any model has to be installed by your IT team. |
| A meeting stopped because its speech model is not ready | Choose a downloaded model in Settings > **Speech model** and select **Transcribe with another model…** on the meeting. Or download or repair the model in Settings > **Manage downloads**, then select **Try again**. When your organisation has turned downloads off, any model has to be installed by your IT team. |
| Speaker identification did not work | The transcript is still usable. Check the speaker model in Settings > **Manage downloads**, then use **Identify again…** in the meeting's Speakers tab. When your organisation has turned downloads off, the message says so: ask your IT team to install or repair the speaker model. |
| A watched recording is not imported | Look at the folder in Settings > Watched folders. It shows whether the folder is paused (select **Resume**), and files waiting for copying to finish, cloud-only files and unreadable files. Your organisation may also have turned watched folders off. If the folder's note says Unminute started a new record of the files in it, recordings added since the folder was last checked may not have been imported: use **Import** to add any that are missing. The note stays until a recording from that folder is next imported. If the note says recordings have paths too long for Windows to open, move them to a folder with a shorter path. |
| A folder cannot be watched | A whole drive, your user folder or a folder that holds it, and the Windows, program and app data folders cannot be watched. Choose the specific folder your recordings are saved in. A folder you watched with an earlier version that is now one of these shows this message as its note and is no longer checked: remove it and watch the specific folder instead. |
| A title, passage, name or note shows "[Could not be read]" | That part of the workspace is damaged, or was changed outside Unminute. The rest of the meeting is shown, and you can still discard it or finish it. Anything made from it is refused, so nothing is produced with the gap in it: the handoff (preview, copy and save), confirming the transcript review, editing that passage and changing speaker names. The marker itself is ordinary text you could select and copy by hand. Tell your IT team. |
| A recording says "The recording could not be read" | Its saved audio, or the key it was saved with, is damaged or missing (for example after a disk fault, or files changed by another program). The files are kept: select **Delete** if you do not need them. |
| A recording's report says "A piece of the recording could not be read (damaged) and was left out" | That piece is silent in the meeting; the rest was transcribed, and later times still match the recording. |
| A meeting says its audio is damaged, or playback says the audio could not be read | The app's encrypted copy of the audio was changed or damaged on disk (for example by a disk fault, or by a program other than Unminute). Nothing damaged is transcribed, and the transcript so far is kept. If you still have the recording, import it again as a new meeting; when the meeting says "Import the recording again", its original is still where it was imported from. Tell your IT team if it happens again. |
| A message says Windows could not unlock Unminute's encryption key | See [If Unminute cannot unlock its workspace](#if-unminute-cannot-unlock-its-workspace) below. |
| A message says Unminute is already open in another Windows session of yours | You are signed in to Windows more than once (for example at the computer and by remote desktop), and Unminute is open in the other session. Only one copy can have your workspace open, so close Unminute there first, or work in that session. |
| The app says the computer is on battery | Plug in, or allow processing on battery in Settings. |
| An update is waiting | Settings > Updates lists what it is waiting for. |
| Settings says some settings are on their defaults | A saved setting could not be used: the settings file was damaged, or a newer version of Unminute saved a value this one does not know (after going back to an earlier version). Check the settings it names. A newer version's value you leave alone is kept and comes back when you update again; one you set again here replaces it. A settings file that could not be read at all is kept aside as `settings.unreadable.json` the first time a setting is saved. |
| A message says Unminute found damage in its workspace database, or that it cannot open its workspace | See [If Unminute finds damage](#if-unminute-finds-damage) below. |
| A message says Unminute's workspace folder is redirected into OneDrive or to a network drive | Ask your IT team. The workspace must stay on this computer, outside synced folders, so Unminute does not open it there. IT may move only Unminute's models folder (see [What the package contains](PACKAGING.md#what-the-package-contains)). Recordings in OneDrive are fine: they still import and can be watched. |
| A message says Unminute's workspace folder is linked to a folder that cannot be reached | Ask your IT team. `%LOCALAPPDATA%\Unminute`, or its `workspace` folder, is a junction or link to a folder that has been removed or is on a drive that is not connected. Unminute does not create a workspace in its place. IT can restore the folder (or reconnect its drive), and Unminute opens it as before. Or IT can remove the link: Unminute then starts a new, empty workspace in the usual place. If the link removed is `%LOCALAPPDATA%\Unminute` itself, its models folder was behind it too, so the models must be downloaded again. Meetings in the old folder come back only by restoring that folder and the link, which then takes the place of the new workspace and anything in it. |
| A message says "Something went wrong (code U-…)", or the app closed by itself | Open **Help and shortcuts**, select **Copy diagnostics**, and paste them into your support request with the code. They hold versions, settings, counts and error locations in the app's code, never meeting content, names or folders. |

For IT support:

- `runtime\python.exe -I -B -m workspace --self-test` in the installed version folder imports every dependency, confirms the network block, checks that the speaker model loader refuses a model file that would run code (a test file made in memory) and that Windows can protect and unlock a key for this account (`dpapi round trip`). It does not check your models: use Settings > Manage downloads for those.
- `--status` prints counts and states only (meetings, holds, pending cleanup, the key's state at the last start), never content.
- `--health` prints a one-line health report for device management (see [PACKAGING.md](PACKAGING.md)).
- `--verify-audit` checks the log in [What Unminute records about your work](#what-unminute-records-about-your-work), and with `--event-log` compares it with its Windows event log copy. Run it as the signed-in user (see [IT_DEPLOYMENT.md](IT_DEPLOYMENT.md#audit-log)).
- The app keeps small content-free logs in `%LOCALAPPDATA%\Unminute\workspace\logs`. **Copy diagnostics** in Help copies their newest records with versions and settings; ask for that rather than the files.

Deployment, detection and signing are covered in [PACKAGING.md](PACKAGING.md). [IT_DEPLOYMENT.md](IT_DEPLOYMENT.md) brings together what IT needs: install modes, managed policy, health monitoring, updates, offboarding and incidents.

## If Unminute cannot unlock its workspace

Unminute encrypts the meetings in its workspace with a key that Windows keeps for your account on this computer. If Windows will not unlock that key, Unminute opens locked: it does not read your meetings, change your meetings or settings (it only records the problem in its logs), make a new key over it or show anything unencrypted, and processing, imports, recording and cleanup wait. Your meetings are kept as they were. Close Unminute, sign out of Windows and back in, then open Unminute again: if Windows can unlock the key by then, everything carries on, including any cleanup you had confirmed.

Windows can refuse when the workspace was made by another Windows account or on another computer, after an administrator reset your password without the old one, or when your Windows profile is damaged. Settings, the model downloads and the Hugging Face token are not affected.

For IT support:

1. Ask the user for Help > **Copy diagnostics**, which still works while locked. Its "Key" line gives the reason, the Windows error number and a code, and it shows the number of active holds and of meetings whose cleanup was confirmed but not finished. The record with that code, in `%LOCALAPPDATA%\Unminute\workspace\logs\app.log`, holds the same reason and number only.
2. `runtime\python.exe -m workspace --status` shows `"keys": "locked"` with `lock_reason` and `lock_code`, and `--health` reports `"workspace": "locked"`. Both read what the app recorded at its last start and never try the key. The installer's check (`--gate`) still works: it reads counts only.
3. Whether Windows can still unlock the key (for example after a password reset) depends on how the laptop and account are set up, which is being checked (validation tracker #189). If it cannot, the meetings in this workspace cannot be read by anyone. Rename the workspace folder (as in step 3 of [If Unminute finds damage](#if-unminute-finds-damage)) so Unminute can start a new one. The renamed folder still holds every meeting, including those confirmed for cleanup that did not finish, and they become readable again if Windows later unlocks the key. Delete the renamed folder once no hold applies to anything in it. The new workspace starts with the built-in handoff templates.

If Windows cannot set up a key for a new workspace at all, Unminute says so with the Windows error number and a code, and closes; nothing is created. This version opens its usual window while locked, with every request refused; a later version shows a locked screen with these steps.

## If Unminute finds damage

If Unminute last closed unexpectedly (a crash, a power cut, or ending it from Task Manager's **Details** tab), it checks its workspace database before it opens the workspace at the next start. If it finds damage, it shows a message with a code and holds processing, imports, cleanup and its own upkeep, so as little as possible is written to the damaged database. You can still read, copy and hand off meetings, and changes you make (edits, names, confirmations) are still saved. **How to recover** in the message opens this section. While the damage stays, the check runs again at every start and the hold comes back. If the damage is so bad that the workspace cannot open at all, Unminute says so, with a code, and closes.

What to do:

1. Hand off anything you need now: open each meeting you still need and copy its text.
2. Tell your IT team, with the code from the message.
3. Select **Continue anyway** only if IT agrees, or if you must carry on working. Processing and cleanup then write to the damaged database, which can make the damage worse, and cleanup relies on hold records that may be damaged. Unminute never removes an import that looks unfinished in that run: damaged records can make a finished meeting look unfinished.

For IT support (there are no backups, by design):

1. Ask the user to hand off what they need, then close Unminute. Check that no Unminute processes are left in Task Manager before going on.
2. Run `runtime\python.exe -m workspace --status` in the installed version folder. It shows `"integrity": "failed"` (or `"readable": false` if the database cannot be read at all), the number of active holds, `pending_cleanup` and `recordings`. The holds and cleanup counts come from the damaged database and may be wrong: a hold can be missing from them. `recordings` counts recording folders on disk, including any the user discarded that are waiting to be removed.
3. Rename the workspace folder `%LOCALAPPDATA%\Unminute\workspace` (for example to `workspace-damaged`). Leave it in the user's profile on this computer: it holds meeting content, so never copy it to a share or another computer, and never email or upload it.
4. Start Unminute. It creates a new, empty workspace. Models and the Hugging Face token are kept, but guided setup, settings, handoff templates and watched folders start again.
5. Tell the user what did not happen: cleanup they confirmed in the old workspace (`pending_cleanup`) has not run, so audio, transcripts or originals in watched folders they asked to delete may still be there. `recordings` counts recordings that never became meetings; their audio is in the renamed folder's `recordings` folder.
6. Before deleting the renamed folder, check with whoever manages holds in your organisation, whatever `--status` showed: hold records in a damaged database can be lost, and holds never expire. Delete it once nothing in it is needed.

To get meetings back that were not handed off, SQLite's own recovery tool can sometimes rebuild a damaged database. It is not part of Unminute, and SQLite warns that a recovered database is usually defective: some content can be lost, altered, or come back after it was deleted (see SQLite's guide, [Recovering data from a corrupt SQLite database](https://sqlite.org/recovery.html)). Work only on a copy kept in the same profile, and delete the copy and the SQL text the tool writes when you are done: both hold meeting content. Ask the Unminute maintainers before putting a recovered database back in place: this has not been tested with an Unminute workspace.

## Supported configuration

These configurations have been tested:

- **Windows:** 64-bit Windows 11 on an x64 processor, CPU processing (Quiet and Faster), with the Edge WebView2 Runtime.
- **Hardware:** the minimum is 4 processor cores and 8 GB of memory, and 8 cores and 16 GB are recommended. So far it has only been tested on a faster laptop (#28).
- **Speech models:** Parakeet v2 (recommended) was tested in the workspace. Whisper large-v3 turbo and Whisper large-v3 were tested in the workspace on 20 October 2026, on the processor and on an NVIDIA graphics card, with public meeting recordings. Parakeet v3 was tested only in the retired source app's engine, where it dropped speech on some clips.
- **Speaker identification:** pyannote community-1.
- **Graphics card:** one NVIDIA laptop card, an RTX 500 Ada with 4 GB. Speaker identification on it was tested with synthetic audio (14 October 2026) and a public meeting recording (17 and 19 October 2026), and Whisper large-v3 turbo and Whisper large-v3 with public meeting recordings (20 October 2026). Other NVIDIA cards are untested.

**Graphics card (optional).** On a computer with an NVIDIA card, Settings > Manage downloads offers two components. You can download either or both:

- **"Graphics card acceleration"** speeds up speaker identification. It is a one-time 2 GB download of the CUDA build of PyTorch from the PyTorch download site.
- **"Graphics card for Whisper"** runs Whisper large-v3 turbo and Whisper large-v3 on the card. It is a one-time 0.57 GB download of NVIDIA's cuBLAS library, from NVIDIA's release on PyPI (files.pythonhosted.org). Parakeet stays on the processor: on the card it was slower.

Each is checked against published checksums. Once one is downloaded, turn on Settings > Recording and speakers > "Use the graphics card".

- **When it is used:** only in Faster mode.
- **What it needs:** an NVIDIA driver from version 580. For Whisper, the card must also run float16 (compute capability 7.0 or later, from 2017's Volta and later cards); an older card leaves Whisper on the processor. Nothing else is installed on the computer and drivers are never changed.
- **If the card cannot be used** (an old driver or card, too little graphics memory or a damaged component), that work runs on the processor instead, and the meeting says so. A transcript already part-done carries on on the processor with the same model; nothing finished is lost.
- **Large-v3 on the card can give slightly different text** from large-v3 on the processor. On the card it runs at int8_float16 to fit in the card's memory, and large-v3's text also varies a little from run to run. On a 39-minute public meeting (AMI ES2004c), 11.8% of words differed, and the word error rate was 21.76% on the card in one direct run and 20.02% through the app, against 19.75% on the processor. Turbo was within 0.3 points on the same meeting.
- **Each meeting shows** whether speaker identification ran on the graphics card, and its **Versions** tab shows where each transcript was made and at what precision (for example "Graphics card, float16" or "Processor, int8").

Detecting a graphics card does not enable anything on its own.

See [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) for what is verified for a release and what still needs people or hardware.

For how the workspace is built, see [WORKSPACE_DESIGN.md](WORKSPACE_DESIGN.md). The original product plan and coding sequence are kept for history, no longer maintained: [WORKSPACE_PLAN.md](history/WORKSPACE_PLAN.md) and [IMPLEMENTATION_PLAN.md](history/IMPLEMENTATION_PLAN.md).
