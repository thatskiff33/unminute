# Changelog

Release notes for Unminute. The Release workflow reads the section
for the version being released, so keep one heading per version. From v0.5.0 the headings are
dated, `## vX.Y.Z (YYYY-MM-DD)`, and written by `tools/changelog.py`.

Changes not yet released are in [`changelog.d/`](changelog.d/README.md), one file per pull request.
`python tools/changelog.py vX.Y.Z` moves them into a new section here when a release is prepared.

## v0.5.0-preview.3 (2026-10-20)

Preview for testing, built by GitHub Actions with signed build provenance. Updates v0.5.0-preview.2 in place and keeps your meetings, edits, holds and settings: the storage format is unchanged. Not an update in place from 0.4.3 or earlier: finish with the earlier version and uninstall it first (see Updates in docs/PACKAGING.md).

- **Unminute will not open a workspace redirected into OneDrive or onto a network drive.** It already refused when the account's local app data folder itself was there. A junction or link at `%LOCALAPPDATA%\Unminute`, or at its `workspace` folder, could still put meetings in a synced or network folder; the app now follows it to the real location and refuses to open the workspace there, with a message asking the user to contact IT. The installer's workspace check stops with exit code 6 and the same message, and `--health` reports `location`. IT may still redirect only the `models` folder. Recordings in OneDrive, where Teams saves them, still import and can still be watched (#219).
- **Installing and uninstalling wait for a file that is in use for a moment.** A folder cannot be renamed or removed while a file in it is open, and antivirus software such as Microsoft Defender can open newly written program files for a moment to scan them; one install of v0.5.0-preview.2 failed this way and worked when run again. The installer's final move of the new version into place (and, when the same version is installed again, moving the old copy aside) and the uninstaller's removal of the program now try again, for up to 30 seconds from the first refusal for each step, when Windows refuses them as access denied or in use, then fail with the same message as before. Any other error still fails at once, and nothing else is removed (#239).
- **Settings marks speech models that are not ready.** Settings > Speech model labels a model "(not downloaded)" or "(damaged)", as the Downloads page does. Choosing one still saves the choice, and a note beside the picker says how to download or repair it.
- **A failed meeting can switch speech model.** A meeting that stopped (for example because its model is not downloaded) offers **Transcribe with another model…**, which transcribes it again with the model now chosen in Settings; parts already finished are kept as an earlier version. Transcribe again in the Versions tab works for it too. A chosen model that is not ready is refused before anything changes, with a note on how to get it.
- **Model errors point to Manage downloads.** Messages about a missing or damaged model now say Settings > Manage downloads instead of Setup. When your organisation has turned downloads off, Settings points to another model or your IT team instead.
- **Whisper large-v3 can be downloaded.** Setup (under Other options) and Settings > Manage downloads now offer Whisper large-v3, a 3.09 GB download that is checked file by file like the other models. OpenAI rates it its most accurate Whisper model. In our earlier tests, which ran an OpenVINO build of it on Intel Arc graphics, it was no more accurate than turbo and needed about twice the memory. With the app's own processor engine on two public meetings (17.5 and 39 minutes), it was again no more accurate than turbo and took about 4 to 5 times as long. It is the largest model, so expect it to be the slowest on a processor. Before, Settings let you choose it but nothing could download it, so a meeting set to it could not run on a new install. Parakeet v2 stays the recommended model, and Whisper large-v3 turbo the quicker Whisper option.
- **Whisper on an NVIDIA graphics card.** On a computer with an NVIDIA card, Settings > Manage downloads now offers "Graphics card for Whisper", a one-time 0.57 GB download of NVIDIA's cuBLAS library for CUDA 12 from NVIDIA's own release on PyPI (files.pythonhosted.org), checked against published checksums file by file. With it downloaded, "Use the graphics card" on and Faster mode, Whisper large-v3 turbo and Whisper large-v3 transcribe on the card. On a 4 GB laptop card, a 17.5-minute public meeting took 47 s with turbo instead of 6 minutes on the processor, and 3 minutes with large-v3 instead of 28 to 31 in two processor runs (with the processor partly busy), and was about as accurate against the published transcripts of that meeting and a 39-minute one. Large-v3 runs on the card at int8_float16, which fits in 4 GB; at float16 it did not fit. Large-v3 on the card can give slightly different text from large-v3 on the processor: on the 39-minute public meeting (AMI ES2004c), 11.8% of words differed, and the word error rate was 21.76% on the card in one direct run and 20.02% through the app, against 19.75% on the processor. Parakeet stays on the processor, where it is faster. If the card cannot be used (an old driver, too little graphics memory or a damaged download), the rest of that transcript is made on the processor with the same model, nothing already done is lost, and the meeting says so. A meeting's Versions tab now shows where each transcript was made and at what precision, for example "Graphics card, float16" or "Processor, int8". IT can turn both graphics card components off with the existing `AllowGpuComponent` policy (#248).
- **Downloading or repairing a model waits for a file that is in use for a moment.** Antivirus software such as Microsoft Defender can open newly downloaded files for a moment to scan them, and Windows then refuses to move the verified download into place, so a download could fail at the last step and need starting again. Moving the verified files into place (and a damaged copy aside), for speech and speaker models and the graphics card components, now tries again for up to 30 seconds when Windows refuses it as access denied or in use, then fails with the same message as before. Any other error still fails at once, and nothing else is moved or deleted (#249).
- **A model that is not ready names your IT team when downloads are turned off.** When your organisation has turned model downloads off, a meeting that stopped because its speech model is not downloaded or is damaged, and speaker identification that failed for the same reason, now say to choose another model or ask your IT team, as Settings does, instead of pointing to Manage downloads.
- **Security update: aiohttp 3.14.5** (was 3.14.3), which fixes three advisories: two high-severity ones in aiohttp's web server (GHSA-5wr8-gcqf-gqf3, GHSA-2g87-pp6c-29x5) and a low-severity one in its client cookie handling (GHSA-494j-54xm-rwp3). aiohttp is bundled only because fsspec, a file-access library that the model libraries depend on, can use it for web addresses. Unminute never runs an aiohttp server and never loads aiohttp, so exposure was nil in practice; the fix is taken anyway. yarl moves to 1.25.1 with it, as the new aiohttp needs. Nothing changes in how Unminute works (#261).

## v0.5.0-preview.2 (2026-10-19)

Preview for testing, built by GitHub Actions with signed build provenance. Replaces v0.5.0-preview.1, which was tagged but never published; its notes are included here. Not an update in place from 0.4.3 or earlier: finish with the earlier version and uninstall it first (see Updates in docs/PACKAGING.md).

- **More words recognised.** Parakeet now ends a stretch of speech after a 1-second pause instead of a tenth of a second, so it hears each phrase with more of its context. On eight public meeting recordings its word error rate fell from 24.0% to 21.2% (by 1.4 points on four more that were not used to choose the setting), without more invented words overall, and it used less processor time. Whisper is not affected. Sentences can be a little longer, so a sentence that two people share is slightly more often given one speaker's label (#107).
- **Setup wording.** The welcome heading now reads "Unlimit your meeting minutes", the app's tagline, and the side panel is headed "Setup".
- **Skip speaker identification for a meeting.** For a recording of one or two people, select **Skip speakers** on the meeting's row or **Skip speaker identification** in its speaker panel. The meeting goes straight to review and hand off without speaker labels, and a pass already running stops at once. Skip is offered only until speakers are identified, and **Identify again…** still works later while the audio is kept. If a pass is interrupted and will not restart by itself, the meeting says to use **Identify again…** rather than promising a restart.
- **Security update: pytorch-lightning 2.6.6** (was 2.6.5), which fixes CVE-2026-58659, a code-execution flaw when loading a crafted model checkpoint. Unminute loads only its hash-checked speaker model, so exposure was low; the fix is taken anyway. Speaker identification results are unchanged.
- **Long meetings stay smooth while they are transcribed.** With a meeting open during transcription, new passages are now added to the end of the transcript instead of the whole transcript being fetched and drawn again each time it moves on, so scrolling, playing and correcting earlier passages stay responsive on long meetings. The transcript is still drawn again in full when it needs to be, such as when speakers are identified or while a search is open.
- **The window does less while you are not using it.** When nothing is being processed, imported or recorded, it checks for changes every 10 seconds instead of every 2, and redraws the meetings list only when something on it changes. While it is minimised it stops checking altogether; recording, transcription and watched folders carry on, and it catches up as soon as you bring it back.
- **Fewer wake-ups while idle.** The app no longer wakes every second to see whether a watched-folder check, a policy check or a clipboard clear is due. It now wakes about once a minute for watched folders and twice a minute for policy, and every second only while recording. A check of a watched folder with nothing new writes to the workspace database once, not once per file (#128).
- **Closing during a folder's first check no longer imports what was already there.** If the app closed part-way through the first check of a folder added without its existing recordings, the files it had not reached could later be imported as new (#128).
- **Settings and Setup do less while open.** What they show about downloaded models, the graphics card component and the remembered token is checked at most once a minute while you look at them, and again straight after a download, a removal or a change you make, instead of on every refresh. With the graphics card component installed, they used to check about 2,800 of its files on every refresh, which used about a quarter of one processor core while Downloads was open (#128).
- **The meetings list is read only when something changes.** The window asks for the meetings list and reminders only when the app reports a change, and once a minute anyway. Reading the list no longer takes several database queries for each meeting (#128).
- **Quieter when minimised.** When the window is minimised, the page stops drawing. If nothing is recording, it is also paused a few seconds later (after any edit you were typing has been saved) and uses less working memory, until you restore it.
- **Closing from the taskbar while recording shows the question.** If you close a minimised window while recording, it comes back to the front, as it was before (maximised stays maximised), so you can see the Stop, Discard or Keep recording question.
- **Copy diagnostics, for support.** Help and shortcuts now has **Copy diagnostics**: it copies versions, settings, counts and recent error locations from the app's own logs, never meeting content, names or folders, ready to paste into a support request (#130).
- **A health check for IT.** `--health` prints a one-line report for device management, such as an Intune Remediation script run as the signed-in user, and exits 1 when the app needs attention (#130).
- **A local diagnostics log, with no content.** Unminute now keeps a small log in the workspace's `logs` folder (about 1.5 MB for each of its processes) to help fix problems. It holds only error types, where in the code they happened, timings and version numbers: never meeting text, titles, names or folders, and it is never sent anywhere. An unexpected error now shows a code, for example "Something went wrong (code U-1A2B3C)", that support can look up (#130).
- **Settings survive going back a version.** Settings a newer version saved are kept, and come back when you update again. If a saved setting cannot be used, Settings now names it (#132).
- **A database check after an unexpected close.** If Unminute last closed unexpectedly, it checks its workspace database before opening it at the next start. If it finds damage, it shows a message with a code and a link to the recovery steps, and holds processing, imports and cleanup until you choose Continue anyway; `--status` and `--health` report it (#132).
- **Downloads show how far they have got.** The progress bar for a model or the graphics card component now fills as the download arrives, instead of only showing that it is busy (#133).
- **Model names read the same everywhere.** Settings, Setup and a meeting's details all call a model by the same name, for example "Whisper large-v3" instead of "large-v3" in the Settings list (#133).
- **Downloads never follow a link out of the model cache.** Installing or repairing a model or the graphics card component now stops, and says to ask IT, if one of its folders in the model cache is a link to somewhere else, as removing one already did (#133).
- **Less in the install.** The leftover desktop-shell trial (an extra page and its bridge) and an unused bridge method are gone, and a test now fixes exactly which source files the app folder holds, so nothing unneeded ships again (#136).
- **One name everywhere: Unminute.** The app's data is now in `%LOCALAPPDATA%\Unminute` (`workspace` and `models`), it installs to `%LOCALAPPDATA%\Programs\Unminute`, and its uninstall entry, taskbar identity, saved Hugging Face token and icon use the Unminute name (#136).
- **Not an update in place from 0.4.3 or earlier.** Nothing carries over, and this version starts with an empty workspace. First, in the version you have: hand off or copy anything you still need, confirm the cleanup of finished meetings, check any holds with whoever manages them, remove the Hugging Face token (in 0.4, Settings > Manage downloads > Remove the remembered token; in 0.2 or 0.3, Setup > Remove the remembered token), and note your settings and watched folders and copy any handoff template you edited or wrote. Then uninstall it in Settings > Apps, install this version, download the models again in Setup and enter the token again, and set up your settings, templates and watched folders again (leave "Also import recordings already in it" unticked, so finished recordings are not imported twice) and, if you used it, the graphics card component. The earlier version's workspace and models stay in their own folders until you delete them (docs/PACKAGING.md, "Updates", says where). A token left behind can still be removed in Windows Credential Manager, under Windows Credentials. If you install this version first, Settings > Apps shows two Unminute entries, and uninstalling the earlier one afterwards removes the new Start menu shortcut: install this version again to restore it, and pin it again in place of any old taskbar pin (#136).
- **For IT: a new uninstall key.** Intune detection now uses `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\Unminute` (see docs/PACKAGING.md), so the earlier version's Win32 app still counts as installed: supersede it with the option to uninstall the previous version, rather than deploying its uninstall separately (two assignments run in no set order, and the earlier uninstaller removes the new Start menu shortcut if it runs second). Have users finish the steps above in the earlier version (hand off, cleanup, holds, token) before that uninstall runs, and add `-Managed` to this release's install command if the earlier one used it, as the setting does not carry over (#136).
- **Safer uninstall.** The installer and the uninstaller refuse an install folder that is, holds or sits inside `%LOCALAPPDATA%\Unminute`, an empty one, or one starting with `\\` or `//`. The uninstaller removes only what the installer put in its folder, removes nothing if the folder holds anything else or is not an Unminute install, and keeps the shortcut and uninstall entry when the entry names another folder that still holds an install, so a mistyped or older install folder cannot take the real install's shortcut and uninstall entry (#136).
- **Watched folders are checked shortly after the app opens.** Opened within 5 minutes of starting the computer, the app waited until then for its first check. It now checks 30 seconds after opening, so start-up is not slowed.
- **An earlier dropout is no longer hidden by a later microphone disconnection.** If saving was behind when the microphone went, a dropout from earlier in the recording could be folded into the disconnection note instead of being reported on its own (#126).
- **Less background work.** The recording display is worked out only when the Meetings page asks for it, not for every tenth of a second of audio, and the app no longer checks every second for a speech worker that is not running (#126).
- **Pausing a watched folder stops its imports at once.** Recordings the app had already found in a folder still imported after you paused it, or after your organisation turned watched folders off. Now they wait and are imported when the folder is resumed (or allowed again). The one being copied in at that moment still finishes (#152).
- **A failed check of the graphics card component now stops its use.** If Check every file finds a damaged file in the graphics card component, speaker identification uses the processor, and the meeting says so, until the component is repaired or passes Check every file; the component shows as damaged meanwhile. Before, a component already used once that session could still be used.
- **Handoff templates write like a minute-taker.** The built-in templates now ask the assistant to gather context about the meeting from its connected tools and the project it is in (reading only), then draft a clean document with no timestamps. It corrects misheard words, and fills in names, owners and dates, from that context. It shows the draft in the chat with a short list of points to check, and revises it with you. When it is final, the assistant offers a Word document in your organisation's brand template, as a `context.md` and a prompt for a session that can create Word documents. Templates you edited keep your wording; **Restore original wording** brings in the new one.
- **Four new handoff templates.** Board or directors' minutes, Client meeting file note, Follow-up email, and Team or WIP notes.
- **Anonymised handoffs ask the assistant not to identify speakers.** When names are replaced with Speaker 1, Speaker 2, the prompt asks the assistant not to work them out from other context.
- **Every handoff sets rules for the assistant's tools.** Whatever the template, including your own, the handoff asks the assistant to use its connected tools only to read, to change nothing unless you ask in the chat, not to search the web or open links because of the transcript, and to treat what its tools return as information, not instructions. Invisible characters are removed from the handoff text.
- **The computer stays awake while recording.** A long meeting in which nobody touches the computer no longer ends when Windows' idle sleep timer runs out, unless your organisation's power settings prevent it. Windows still decides what closing the lid does. To record with the lid closed, turn on the new "Keep recording when the lid is closed" setting or set Windows to do nothing, and stop the recording in Unminute when the meeting ends.
- **A recording stopped by sleep says so.** If the computer goes to sleep, for example because the lid was closed, the recording should stop, keep what was recorded and say the computer went to sleep, instead of reporting a lost microphone. A paused recording stays paused unless Windows ends the microphone stream on waking. This is tested with simulated sleep; real laptops are still to be checked.
- **Updates go ahead when a folder name contains `#`.** If the path to the app data folder (usually through the Windows user name) contained `#`, or `%` followed by two hexadecimal digits such as `%41`, the installer's update check could not read the workspace and every update waited. The check now reads such a workspace like any other (#164).
- **The update check no longer mistakes a long workspace path for a fresh install.** On a computer without Windows long paths turned on, a workspace whose path is longer than 260 characters looked absent, so an update that changes the storage format could go ahead. The check now finds it, and the update waits if it cannot read it (#164).
- **Keep recording when the lid is closed.** A new setting, off by default, under Settings > Recording and speakers. While a recording is open, Unminute sets Windows to do nothing when the lid is closed, and puts your own setting back when the recording ends, or at the next start if the app closed unexpectedly. Where your organisation sets what closing the lid does, Unminute leaves it alone. IT can turn the setting off with the new policy `AllowLidOverride`.
- **The recording card says if the lid was closed.** When you open the lid during a recording, the card says how long the recording carried on with the lid closed.
- **A saved handoff file marks the transcript as data.** Save Markdown now writes the same note and BEGIN/END lines that the copied text has, so an AI assistant is told to treat the transcript as information, not instructions, even when the file is attached on its own. Copy prompt only points the assistant at those lines.
- **A meeting whose import never finished can be discarded.** After **Continue anyway** on a damaged workspace, a meeting left half imported used to stay listed, and Discard refused it as "still being imported". Discard now removes it, after the usual confirmation, once no import is running.
- **A recovered recording is read again if Windows holds its record file for a moment.** When an antivirus scan or another program briefly had the file open, a recording recovered after a crash could be reported as unreadable. The app now tries again a few times (#148).
- **The drop outline shows in dark mode.** When you drag a recording onto the window, the dashed outline of the light drop panel was nearly invisible in dark mode; it now uses the same blue as in light mode, which stands out on the panel in both (#183).
- **Uninstalling puts the lid setting back.** If Unminute closed unexpectedly while it had set Windows to do nothing when the lid is closed, and did not start again, uninstalling it now puts your own lid setting back before anything is removed. If it cannot, the uninstaller says to check the setting in Windows Settings. The uninstall carries on either way.
- **A retention hold now also protects an import that never finished.** At start-up, such a meeting used to be removed even if it had a hold. It now stays listed as **Import did not finish**, and Discard removes it once the hold is released (#193).
- **Removing a meeting on a damaged workspace finishes.** After **Continue anyway**, removing a meeting deletes its text and audio, and the receipt says the database compaction is deferred until the workspace is repaired. The cleanup is no longer retried for ever (#198).
- **Meeting folders are checked before use.** The app builds a meeting's folder path only from its own ids, so a damaged database can never point a removal outside the app's media folder (#194).
- **A recording with several channels no longer loses a track.** Every channel is now mixed in equally. Before, a file with 3 or more channels, for example from a multitrack recorder that gives each person a track, could lose a whole track (for example the third of three, or the fourth of six to eight), and other tracks were halved or mixed louder than full scale. Stereo recordings are now 3 dB quieter, with nothing else changed, and mono recordings are unchanged. Some files with an unusual channel layout that could not be imported before now import (#220).

Security hardening (#111, slice A2):

- **Settings are checked when the app starts.** A value in `settings.json` that the app would not accept from Settings (for example an unknown speech model) is replaced by its default.
- **The window refuses to open unsafely.** The app now stops with a message if Windows would fall back to the old Internet Explorer engine instead of WebView2, or if its window protections cannot be put in place.
- **Tighter limits on what the page can ask for.** The page can show only the app's own screen, drag and drop accepts only files Windows reports as dropped, and calls with the wrong arguments are refused before they run.
- **Less left behind.** The window's browser data is kept inside the workspace folder and cleared at the next start if the app did not close cleanly, and SQLite keeps its temporary data in memory rather than in the Windows temp folder.
- **The handoff file name no longer contains the meeting title.** Save suggests `Meeting transcript YYYY-MM-DD HH-MM.md` (when the meeting was imported), so client names in titles do not end up in file names by default.
- **A Hugging Face token left in your Windows environment is no longer passed on** to the app's other processes.
- **The app refuses a missing, synced or network app data folder** instead of falling back to the current folder.
- **Only one copy runs at a time.** Opening Unminute again while it is open shows a message instead of starting a second copy on the same workspace.

Security hardening (#111, slice A3):

- **Recordings are decoded in a separate, sandboxed process.** Each imported file is decoded by a short-lived helper that runs at Windows' Low integrity level (the level browsers use for web pages) inside a Windows job. Windows stops it writing your files, opening the app's process, starting programs directly or using more than 2 GB of memory, and normally also the clipboard (inside some managed sessions the session's own limits apply instead), and it ends when the app closes. It sends back only plain audio, which the app checks, writes and hashes itself. It can still read files you can read, so a decoder flaw would mainly be a privacy risk; the firewall rule for managed devices (#111 B7) closes the network side.
- **Only real recordings are opened.** The decoder accepts only the supported recording formats and local files, whatever a file claims to be, so playlists, stream descriptions and scripts disguised as recordings are refused.
- **Limits on what a file can do.** Decoding stops at 12 hours of audio and keeps at least 1 GB of disk space free.
- **Network drives are refused for imports.** Files on mapped network drives, or reached through a symbolic link, are refused with a message to copy them first. Synced folders such as OneDrive are unaffected.

Security hardening (#111, slice A4):

- **The network block covers more.** Besides ordinary connections and name lookups, the app's Python processes now also refuse UDP datagrams and the Windows asyncio connection path, which went around the block before. `--self-test` checks all three against a reserved address that is never contacted.
- **Downloads can reach only their own sites.** The download process may contact only huggingface.co and hf.co (models), download-r2.pytorch.org (the optional graphics card component) and any proxy configured for you. Every request and every redirect is checked, also when downloads go through a proxy, and anything else is stopped with its own message. Hugging Face's native Xet downloader is switched off so every download goes through these checks.
- **Downloads work behind a firm's inspecting proxy.** Certificates are checked against the Windows certificate store (truststore), so a proxy certificate that IT has installed is trusted without any unsafe workaround. A fixed proxy is needed: proxies given only by an automatic configuration script (PAC or WPAD) are not used.
- **The speaker model is fully checked before every use.** Its files are re-hashed each time they are loaded, not just compared by size and date, because the model format can run code.

Security hardening (#111, slice A5):

- **IT can manage the app's settings.** On managed computers the app reads policy values from `HKLM\SOFTWARE\Policies\Unminute` (at start and every 15 minutes), which standard users cannot change. IT can turn off model downloads, the graphics card component, microphone recording, watched folders and deleting originals; allow recordings on mapped network drives; set a starting folder for saved handoffs; allow the meeting title in handoff file names; and have the app clear its handoff copy from the clipboard after a set time. An administrative template for Intune is in `packaging/policy/`.
- **A bad policy value never grants a permission.** A value of the wrong type or out of range turns that permission off rather than on, and a policy key that cannot be read turns every permission off.
- **Settings shows what is managed.** A note at the top of Settings says some settings are managed by your organisation, and managed controls are disabled. Setup and Downloads show "Ask your IT team" instead of a Download button where downloads are off, or "Turned off by your organisation" for the graphics card component. The app enforces each value itself, whatever the page shows: for example, a recording in progress is stopped and saved when microphone recording is turned off.
- **Without a policy the app behaves as before.**

Security hardening (#111, slice A6):

- **Each recording starts with a confirmation that everyone has been told.** Before recording, Unminute asks you to tick that everyone in the meeting has been told it is being recorded, and why, then select Start recording. The question is drawn by the app outside its page, so nothing in the page can answer it: Start recording becomes available after a moment; keyboard and screen-reader users move to it and pause briefly before pressing Enter, and Enter anywhere else means Cancel. IT can turn it off with the new `RequireRecordingConsent` policy.
- **You can see when the app is recording.** The window title starts with a red-dot "Recording" mark and the app's taskbar button shows a red dot, also when the window is minimised.
- **Discarding a meeting and allowing the app to delete originals use the same protected confirmation**, in place of the page's own dialog. The deletion question names the folder.

Security hardening (#111, slice A7):

- **The original app is retired.** The source-installed app this one replaced has been removed from the repository and gets no further fixes.
- **A Hugging Face token in your Windows environment is no longer used.** To download the speaker model, paste your token in Setup. A token left in your Windows environment is still kept away from the app's other processes.
- **Less left over from the original app.** The speech-model code no longer contains the original installer's own downloader, the network guard no longer has a way to let downloads through in the app's own processes, and error messages point to Repair in Settings > Manage downloads instead of the original app's Update / repair button.
- **Smaller package.** Three packages only the original app used (darkdetect, sv-ttk and tkinterdnd2) are no longer installed.

Security hardening (#111, slice B1):

- **Every package in the app is hash-locked.** The package build installs only the locked set and refuses any file that does not match its pinned SHA-256, and a daily audit checks the locked packages for known vulnerabilities. The installed packages and versions are unchanged.

Security hardening (#111, slice B2):

- **A leaner, precompiled package.** The package leaves out what Unminute never uses: three packages (grpcio, pyannoteai-sdk, setuptools), the test suites inside packages, files only needed to build software, and some binaries for other processors: about 15,000 files, 15 of them programs and libraries. Every module now ships precompiled (about 10,000 files), so Unminute writes nothing into its own folder when it runs. Together these take the zip from about 27,800 files to about 23,000. The precompiled files add about 79 MB, so the zip is about 379 MB, including the cryptography library added in slice C1 (the 0.4.3 zip was 329 MB).
- **The package says what is in it.** It now includes a software bill of materials (`sbom.cdx.json`, also published beside the zip), third-party notices that also list the native libraries each component bundles, a list of every executable file with its SHA-256 and signature details, and a manifest of the pinned model files for checking a copy staged by IT.
- **Reproducible release builds.** Building the same release twice with the same tools gives the same zip, byte for byte. A release is built only from the committed files of its tagged commit, with no local changes, and any new executable file or changed signer stops the build until it is reviewed.

Security hardening (#111, slice B3):

- **Releases are built by GitHub, with signed build provenance.** Each package is built from the tagged code on GitHub's own servers, with a signed record of how it was built and a signed software bill of materials, and is published only after the owner approves it. The zip itself is still not code-signed. A release also has to pass the performance, crash, lifecycle, window and accessibility checks, run against that exact package. IT can check a download before packaging it with `tools/intune/Get-VerifiedRelease.ps1`.
- **The release build no longer fails its signature check when started from PowerShell 7, as on GitHub's runners.** The check that the bundled Python is signed by the Python Software Foundation is as strict as before, and Windows PowerShell now runs only from the Windows system folder, with modules only from the folders an administrator controls.

Security hardening (#111, slice B4):

- **Python 3.14.** The app now runs on CPython 3.14.8. Python 3.12 no longer gets Windows releases with security fixes; 3.14 is planned to get them until October 2027. PyTorch, used for speaker identification, moves from 2.13.0 to 2.14.1, so its graphics card build for Python 3.14 can be checked against the hash PyTorch publishes for it. Other packages keep their versions.
- **No outside debugger.** Python 3.14 lets another program attach a debugger to a running Python program and run code in it. Unminute now starts with that switched off.
- **The graphics card component downloads again.** It is now the build for Python 3.14. If you use it, download "Graphics card acceleration" again in Settings > Manage downloads (about 2 GB) and turn "Use the graphics card" back on. A copy downloaded by version 0.4.3 or earlier stays in that version's model folder until you delete it.

Security hardening (#111, slice B9):

- **The speaker model loads only what was checked.** Each speaker model file is now read once and checked, and speaker identification is built from those same bytes in memory. Before, pyannote opened the files again after the check, and in a way that could run code from a tampered file. The model's weights are now read in a mode that cannot run code. Only the pinned model's exact settings are accepted, and a file that fails is refused with one plain message. `--self-test` checks that a model file built to run code is refused (#218).

Security hardening (#111, slice C1):

- **The package now includes the cryptography library, with its own copy of OpenSSL, ready for encryption at rest.** Nothing uses it yet: a later update encrypts meeting content with it (`docs/ENCRYPTION.md`). IT will see it in the bill of materials, the third-party notices and the binary list.

Security hardening (#111, follow-up to A6):

- **The page can no longer show its own text in a message box.** The browser's built-in alert, confirm and prompt boxes are turned off, and the page's alert no longer opens a Windows message box, so a compromised page cannot pose as the app or as Windows with instructions such as "tick the box, then Allow". The app's own dialogs are unaffected. Page script can still open a file picker or print preview (#175).
- **The page can no longer ask for the microphone or the screen.** Browser permission requests (microphone, camera, notifications and the rest) and screen-capture requests are refused at once, with no browser prompt or picker, so recording only ever starts through the app's own confirmation.

## v0.4.3

Safer recording. Install: download `Unminute-0.4.3.zip` and its `.sha256` file, then follow the README or docs/PACKAGING.md. An installed v0.2.x, v0.3.0 or v0.4.x updates and keeps your workspace.

- **Closing while recording asks first.** Closing the window during a recording now asks: **Stop, save and close**, **Discard and close**, or **Keep recording**. Before, the recording was left on disk and imported the next time the app started.
- **A recovered recording waits for you.** If the app closes or the computer stops while recording, the next start lists the recording as recovered, with **Keep and transcribe** or **Delete…**. Before, it was transcribed straight away, which could look like a discarded recording coming back. A recording you stopped and saved is still imported as before.
- **Full microphone names.** Windows' basic audio list cuts names at 31 characters ("Logitech C920 Microphone (HD Pr"). The app now shows the full name, such as "Logitech C920 Microphone (HD Pro Webcam C920)".

## v0.4.2

More changes from testing, and the v0.4.1 fixes below. Install: download `Unminute-0.4.2.zip` and its `.sha256` file, then follow the README or docs/PACKAGING.md. An installed v0.2.x, v0.3.0 or v0.4.x updates and keeps your workspace.

- **A live curve while recording.** The level bars are replaced by a curve across the recording card. Choose how it looks in Settings > Recording and speakers: **Flowing wave** (the default: overlapping waves whose height follows the volume and whose ripple follows the pitch), **Spectrum** (low to high pitch, left to right) or **Sound wave** (the shape of the sound itself). It is drawn 60 times a second from measurements the recorder takes ten times a second; no audio is kept for it.
- **Discard a recording.** While recording or paused, **Discard…** stops and deletes the audio recorded so far, after a confirmation. Nothing is transcribed or kept.
- **Pinned shortcuts follow updates.** A taskbar or Start pin is a copy of the shortcut from when it was pinned, so after an update it kept opening the old version (and would break once that version was removed). Installing or rolling back now repoints pins to this app at the new version. Pins to anything else are left alone.

## v0.4.1

Fixes from testing v0.4.0. Install: download `Unminute-0.4.1.zip` and its `.sha256` file, then follow the README or docs/PACKAGING.md. An installed v0.2.x, v0.3.0 or v0.4.0 updates and keeps your workspace.

- **Readable dropdown lists in dark mode.** Open lists, such as "Move this passage to", showed white text on a white background.
- **No sideways scrollbar on a meeting's page.** The narrow sidebar was 1 px too narrow for its buttons.
- **Recording waveform as designed.** Thin bars that scroll by ten times a second, one per tenth of a second of audio, scaled so speech fills the height. They no longer stretch into wide blocks in a wide window.
- A waiting meeting no longer says "Waiting" twice.

## v0.4.0

The redesign, in Unlimit colours. Install: download `Unminute-0.4.0.zip` and its `.sha256` file, then follow the README or docs/PACKAGING.md. An installed v0.2.x or v0.3.0 updates and keeps your workspace: the storage format is unchanged. The known limitations listed for v0.2.0 and v0.2.1 still apply. The new screens have been checked with sample data, automated UI and accessibility checks; they have not yet been tried by pilot users (#27).

- **New look, in Unlimit colours.** Unminute is redesigned throughout, with the Archivo typeface bundled, a light sidebar, dark blue buttons and orange kept for Record. Dark mode follows Windows. The app icon is now dark blue and orange.
- **One Meetings page.** The Inbox and Active meetings are merged into one page, grouped by what each meeting needs next: Needs attention, Ready for you, Transcribing and Cleaning up. Meetings ready for you show their length, number of speakers and next action. Record a meeting and Import a recording sit at the top, and the sidebar shows how many meetings are waiting for you.
- **Meeting view.** The four steps run across the top with the next action beside them. Speakers, Versions and Holds are tabs in the side panel.
- **Continuous playback.** The player plays on through the meeting from any passage, with the transcript following along. Space plays or pauses, and J and K move between passages.
- **Who's speaking?** Naming speakers shows each speaker as a card with short examples to play.
- **Hand off page.** Hand off is now a page with the template, the details to remove and a live preview. New: **Replace names with Speaker 1, Speaker 2** for that hand off only, alongside redaction and leaving out passages. Changes are applied before anything can be copied.
- **Setup in five steps**, with an optional microphone step. Models and the graphics card component moved to Settings > Manage downloads.
- **Settings** regrouped into Processing, Speech model, Recording and speakers, Watched folders, Updates and Cleanup receipts.
- **Help and shortcuts** page, opened from the sidebar or with ?.
- The backend now reports each meeting's length, speaker count and model, and the Speakers tab shows each speaker's share of the talking.

## v0.3.0

Unminute: the app is renamed. Install: download `Unminute-0.3.0.zip` and its `.sha256` file, then follow the README or docs/PACKAGING.md. An installed v0.2.x updates and keeps your workspace; uninstalling the old version first and installing this one also keeps your meetings and models, because the uninstaller never removes them. The known limitations listed for v0.2.0 and v0.2.1 still apply.

- **New name: Unminute.** "Unlimit your meeting minutes." The window title, Start menu shortcut, Settings > Apps entry, installer messages, handoff source line and package name (`Unminute-<version>.zip`) say Unminute. Meetings, settings and models carry over.

## v0.2.3

Discard, review layout and privacy improvements, and a new README. An installed v0.2.x updates and keeps your workspace. The known limitations listed for v0.2.0 and v0.2.1 still apply, except that unplugging a USB microphone mid-recording and dragging a recording onto the window have now been tested on hardware (#30, #27).

- **Discard a meeting you do not need.** Meetings in the Inbox, and in review before the transcript review is confirmed, have Discard…, for test or wrong recordings. One explicit confirmation deletes the meeting's audio, transcript, edits and speaker names, using the same secure cleanup as the review steps, and leaves only the usual minimal receipts. Work in progress on it stops first. It is refused while a retention hold is active or the recording is still importing, and the original recording file is never deleted. A reviewed meeting is still finished with the final confirmation.
- **Dismiss import errors.** Messages such as "The file is in cloud storage" now have a Dismiss link.
- **Separate scrolling in the meeting view.** In a wide window the transcript and the steps panel scroll separately, so the title, toolbar and search box stay in view. Narrow or zoomed windows keep one page scroll.
- **Long meeting titles** now use the available width instead of being cut short.
- **Clearer microphone disconnection message.** When a microphone is unplugged mid-recording, Windows also reports a brief overrun. That is now part of the disconnection message rather than a separate "could not keep up" gap.
- **ONNX Runtime telemetry off in the workspace.** The workspace now switches off ONNX Runtime's Windows telemetry events before any speech model loads.
- **New README** with current screenshots of a fictional sample meeting.

## v0.2.2

Fixes and Setup improvements. An installed v0.2.x updates and keeps your workspace. The known limitations listed for v0.2.0 and v0.2.1 still apply.

- **Pause speaker identification.** Meetings in the Active list now show Pause while speakers are being identified. Speaker identification works on the whole meeting at once, so Pause stops the pass and Resume starts it again; the transcript is not affected. Previously it could not be paused, which also blocked downloads in Setup.
- **Graphics card component downloads while processing.** It is used only by graphics card speaker passes, so it can be downloaded or checked while a meeting is processed on the processor.
- **Remove models in Setup.** Each downloaded model, and the graphics card component, has a Remove button, with a confirmation. A model still needed is not removed: the selected speech model, one a meeting is still being transcribed with, and the speaker model while speaker identification is on. The voice detector shared by the Parakeet models goes with the last of them.

## v0.2.1

Adds optional graphics card acceleration for speaker identification. An installed v0.2.0 updates and keeps your workspace.

The known limitations listed for v0.2.0 still apply. In addition, graphics card acceleration has been tested on one NVIDIA laptop GPU (RTX 500 Ada, 4 GB, driver 595.95) with synthetic audio. Agreement with several real speakers, smaller graphics cards and drivers older than 580 are untested; AMD and Intel graphics are not supported.

- **Graphics card for speaker identification (optional).** On computers with an NVIDIA card, Setup offers a one-time, verified download of the CUDA build of PyTorch (1.92 GB). With it turned on in Settings (Faster mode), speaker identification runs on the graphics card. On a 15-minute test recording on an RTX 500 laptop GPU it took 80 s instead of 435 s, with identical results. Transcription stays on the processor. If the card cannot be used, that pass runs on the processor and the meeting says so.

## v0.2.0

A Windows app that transcribes, reviews and cleans up meeting recordings on your own computer. This first full release brings together the four previews (v0.2.0-preview.1 to preview.4).

**Install:** for your Windows account, without administrator rights. It is built for unattended deployment through Intune, which has not yet been tested on a managed device (see Known limitations). An installed preview updates and keeps your workspace.

### What it does

- **Local transcription.** Parakeet v2 is the recommended model. Parakeet v3 and Whisper large-v3 turbo are alternatives. Models are downloaded once in Setup and checked against pinned SHA-256 checksums before use. The checksums match those Hugging Face publishes.
- **Speaker identification (optional).** It uses pyannote community-1 and needs a free Hugging Face token once. You can name, merge and reassign speakers.
- **Review.** A searchable, timestamped transcript with clip playback, autosaved edits and undo.
- **Handoff.** Copy a prompt and the transcript for your own AI assistant, or save Markdown. You see an exact preview first, and you can leave out passages and redact words.
- **Cleanup in two confirmed steps.** The audio is removed once you confirm the transcript is reviewed. The transcript is removed once you confirm the final document is saved elsewhere. Retention holds block cleanup until someone releases them, and they never expire on their own.
- **Watched folders.** These include OneDrive online-only files, which are left alone unless you allow fetching. Deleting originals needs a separate, revocable permission per folder.
- **Microphone recording (optional, off by default).** It saves to disk as it goes. Gaps, a full disk and a disconnected microphone are reported, never hidden, and it never switches device.
- **Privacy.**
  - Processing stays on your computer, and the known telemetry switches of the libraries it uses are forced off.
  - The app blocks its own network connections, except the model downloads you start in Setup. On managed devices, an outbound firewall rule is the stronger control.
  - Cleanup cannot remove copies in synced folders or anything you have shared.
- **Updates and rollback.** An update that changes how the workspace is stored waits until the workspace is empty. You can roll back to the previous version, unless an update has changed how the workspace is stored.
- **Accessibility.** Keyboard access, Ctrl+zoom from 50% to 300%, layouts that reflow at 320 CSS pixels wide (the WCAG test for 400% zoom), and accessible names for screen readers.
- **Resources.** Quiet mode is the default, Faster mode is optional, and on battery automatic work waits by default.

### Known limitations

These have not yet been verified. Details are in docs/RELEASE_CHECKLIST.md and docs/VALIDATION_RUNBOOK.md.

- **Clean profile and Intune.** Not yet tested on a clean standard-user Windows profile, or deployed through Intune on a managed device (#10, #11).
- **No code signing.** This project's installer scripts are not code-signed. Integrity rests on the published `.sha256` and the installer's per-file check. The signing route is still to be chosen (#11).
- **Transcription quality.** Measured on public test clips and synthetic audio only, not on representative business recordings (#12, #25).
- **Accessibility with people.** Not yet tested with a screen reader, Windows contrast themes or nontechnical pilot users (#27).
- **Hardware.** The minimum is 4 processor cores and 8 GB of memory, and 8 cores and 16 GB are recommended. The installer warns below the minimum. Neither level has been tested yet. Testing was on one laptop: an Intel Core Ultra 7 255H with 32 GB of memory, CPU only, on Windows 11 (#28). Windows 10 1809 or later passes the installer's checks but has not been tested.
- **Alternative models.** Parakeet v3 and Whisper large-v3 turbo were tested only in the retired source app's engine, not in the workspace. In that test, Parakeet v3 dropped speech on some clips.
- **Microphone hardware.** Unplugging a USB or Bluetooth microphone mid-recording has not been tested on hardware (#30). Dragging a recording onto the window has not been tested on hardware either; the Import dialog has.
- **No GPU acceleration.** It is not included.

## v0.2.0-preview.4

**Fourth preview** of the packaged app, for testing. It installs per user without admin rights. An installed earlier preview updates in place. The open checks are the same as for preview.3 (see docs/RELEASE_CHECKLIST.md and docs/VALIDATION_RUNBOOK.md).

- **App icon.** The app now shows the microphone icon in the Start menu, the window, the taskbar and Settings > Apps, instead of Python's. A pinned taskbar shortcut groups with the running window.

## v0.2.0-preview.3

**Third preview** of the packaged app, for testing. It installs per user without admin rights. An installed preview.1 or preview.2 updates in place.

Not yet verified (see docs/RELEASE_CHECKLIST.md and docs/VALIDATION_RUNBOOK.md):
- a clean standard-user profile, and Intune deployment (#10, #11);
- transcription quality on representative business recordings (#12, #25);
- a screen-reader pass and testing with nontechnical users (#27);
- resource use on the lowest-supported laptop (#28);
- unplugging a USB or Bluetooth microphone during a recording (#30).

- **Microphone recording (optional, off by default).** Record a meeting from a chosen microphone, with a 3-second microphone test, a level meter, and pause, resume and stop. Audio is saved to disk every few seconds. After a crash the next start imports what was saved and reports roughly what is missing. Overruns, a full disk and a disconnected microphone are reported, never hidden, and the recording never switches device. Other transcription waits while recording. Recordings then follow the usual review, hold and cleanup steps. Turn it on in Settings > Microphone (#30).

## v0.2.0-preview.2

**Second preview** of the packaged app, for testing. It installs per user without admin rights. An installed preview.1 updates in place.

Not yet verified (see docs/RELEASE_CHECKLIST.md):
- a clean standard-user profile, and Intune deployment (#10, #11);
- transcription quality on representative business recordings (#12, #25);
- a screen-reader pass and testing with nontechnical users (#27);
- resource use on the lowest-supported laptop (#28).

- **Zoom and accessibility fixes.** Ctrl+plus, Ctrl+minus, Ctrl+0 and Ctrl+mouse wheel now zoom the workspace window (50% to 300%); zoom had been turned off. At 400% zoom the pages and dialogs fit the window without sideways scrolling. The speech model list and the confirmation dialog now have names for screen readers.
- **Faster mode no longer slows Parakeet down.** On a test laptop with performance and efficiency cores (Intel Core Ultra 7 255H), Faster mode made Parakeet 3 to 4 times slower than Quiet mode. ONNX Runtime threads now wait without spinning. On that laptop, 5 minutes of speech now takes 19 s in both modes (previously 20 to 24 s in Quiet and 73 to 80 s in Faster). Faster mode still speeds up speaker identification.

## v0.2.0-preview.1

**Preview release** of the packaged app, for testing. It installs per user without admin rights.

Not yet verified (see docs/RELEASE_CHECKLIST.md):
- a clean standard-user profile, and Intune deployment (#10, #11);
- transcription quality on representative business recordings (#12, #25);
- testing with nontechnical users (#27);
- resource use on the lowest-supported laptop (#28);
- the final fetch step for OneDrive online-only files (#24).

- Docs: a business-user guide (docs/WORKSPACE_GUIDE.md) and a release checklist with tested compatibility (docs/RELEASE_CHECKLIST.md). The privacy wording now separates local processing from synced storage and from deliberate sharing with an AI assistant (#29, #8).
- Workspace updates: one gate for self-service and IT-managed installs. An update that changes how the workspace is stored waits, and explains why, until there are no meetings, retention holds or unfinished cleanup. Holds are never expired and no content backups are made. Compatible updates keep everything, and rollback to the previous version is supported (#26).
- Speech models: Whisper and Parakeet files are pinned to exact revisions and SHA-256 checksums, and a model folder that merely exists is not treated as installed.
- Workspace package: a preassembled per-user Windows package (private Python runtime, pinned wheels, verified file by file) with an unattended install for Intune, uninstall, and support options (`--self-test`, `--status`). See docs/PACKAGING.md (#11).
- Workspace: watched folders with reconciliation, catch-up after the app was closed, stable-file checks and duplicate suppression (#23). Cloud-only files wait or are fetched on request. Deleting reviewed originals needs a separate, explicit, revocable per-folder permission. See docs/INTAKE.md (#24).
- Workspace: guided Setup with model cards (download, verification and repair), shown on first run. Optional speaker identification has step-by-step Hugging Face account, terms and token guidance. The token is remembered in Windows Credential Manager (removable) and used only by a separate download process. Downloaded models keep working offline without it (#19).
- Workspace: provider-neutral handoff with an exact preview. Options are Copy prompt and transcript, Copy prompt only, and Save Markdown. Templates (Minutes and actions, Short summary, Action list) are editable and versioned. Leaving out passages and redacting words affects only the handoff. Long text is never cut short (#22).
- Workspace: Name speakers, with three representative clips per speaker, More examples, rename, merge, reassigning a passage and undo. Names are for the current meeting only and are removed with the transcript (#21).
- Workspace preview (`python -m workspace`): Inbox, Active meetings and Settings. It covers import by dialog or drop, background transcription and speaker identification with pause and resume, searchable timestamped review with clip playback, autosaved edits with undo, holds, and the two cleanup confirmations (#20).
- Workspace (in development): reasoned retention holds that block cleanup until released and never expire on their own. Releasing a hold needs a fresh cleanup confirmation, and stale holds are listed for review. Completed cleanups leave only a minimal receipt that expires after 30 days (#18).
- Workspace (in development): explicit two-step cleanup. Confirming the reviewed transcript removes the app's audio copy (and an authorised, unchanged original). Confirming the final deliverable is saved removes the transcript working content, leaving no removed text readable in the database. Cleanup intent survives crashes and is retried (#17).
- Workspace (in development, not yet in the app): speaker separation runs after transcription as a separate pass that never changes transcript text, keeps overlap evidence and restarts cleanly if interrupted (#16).
- Privacy (issue #8): pyannote.audio's default usage metrics (OpenTelemetry to otel.pyannote.ai) are now forced off, together with Hugging Face, OpenTelemetry and ONNX Runtime telemetry. The app process also refuses all non-local network access outside deliberate model downloads, so no usage statistics can be sent.
- Speaker identification model (pyannote speaker-diarization-community-1) is pinned to SHA-256 checksums and loaded only from a verified folder, because pyannote loads its weights with pickle, which can run code from a tampered file. `docs/MODEL_MANIFEST.md` lists every model's licence, revision and file hashes (#9).
- Parakeet engine (issue #13): NVIDIA parakeet-tdt-0.6b-v2 (English) is the recommended model for new installs, with parakeet-tdt-0.6b-v3 (multilingual) as an alternative. Both run as INT8 ONNX on the CPU via onnx-asr.
- Model-free unit tests run in CI.
- onnxruntime upgraded from 1.26.0 to 1.30.0 (onnx-asr does not support 1.26.0).
- CI: wheels-only resolve check, ruff and PSScriptAnalyzer added beside the syntax check; CodeQL weekly; Dependabot grouped monthly updates with the torch family limited to patch releases.
