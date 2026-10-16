# Changelog

Release notes for Unminute (formerly Whisper Transcriber). The Release workflow reads the section
for the version being released, so keep one `## vX.Y.Z` heading per version.

## Unreleased

- **Skip speaker identification for a meeting.** For a recording of one or two people, select **Skip speakers** on the meeting's row or **Skip speaker identification** in its speaker panel. The meeting goes straight to review and hand off without speaker labels, and a pass already running stops at once. **Identify again…** still works later while the audio is kept.
- **Watched folders are checked as soon as the app opens.** Opened within 5 minutes of starting the computer, the app waited until then for its first check.
- **Security update: pytorch-lightning 2.6.6** (was 2.6.5), which fixes CVE-2026-58659, a code-execution flaw when loading a crafted model checkpoint. Unminute loads only its hash-checked speaker model, so exposure was low; the fix is taken anyway. Speaker identification results are unchanged.
- **Setup wording.** The welcome heading now reads "Unlimit your meeting minutes", the app's tagline, and the side panel is headed "Setup".
- **Less background work.** The recording display is worked out only when the Meetings page asks for it, not for every tenth of a second of audio, and the app no longer checks every second for a speech worker that is not running (#126).
- **An earlier dropout is no longer hidden by a later microphone disconnection.** If saving was behind when the microphone went, a dropout from earlier in the recording could be folded into the disconnection note instead of being reported on its own (#126).

Security hardening (#111, slice A5):

- **IT can manage the app's settings.** On managed computers the app reads policy values from `HKLM\SOFTWARE\Policies\Unminute` (at start and every 15 minutes), which standard users cannot change. IT can turn off model downloads, the graphics card component, microphone recording, watched folders and deleting originals; allow recordings on mapped network drives; set a starting folder for saved handoffs; allow the meeting title in handoff file names; and have the app clear its handoff copy from the clipboard after a set time. An administrative template for Intune is in `packaging/policy/`.
- **A bad policy value never grants a permission.** A value of the wrong type or out of range turns that permission off rather than on, and a policy key that cannot be read turns every permission off.
- **Settings shows what is managed.** A note at the top of Settings says some settings are managed by your organisation, and managed controls are disabled. Setup and Downloads show "Ask your IT team" instead of a Download button where downloads are off, or "Turned off by your organisation" for the graphics card component. The app enforces each value itself, whatever the page shows: for example, a recording in progress is stopped and saved when microphone recording is turned off.
- **Without a policy the app behaves as before.**

Security hardening (#111, slice A4):

- **The network block covers more.** Besides ordinary connections and name lookups, the app's Python processes now also refuse UDP datagrams and the Windows asyncio connection path, which went around the block before. `--self-test` checks all three against a reserved address that is never contacted.
- **Downloads can reach only their own sites.** The download process may contact only huggingface.co and hf.co (models), download-r2.pytorch.org (the optional graphics card component) and any proxy configured for you. Every request and every redirect is checked, also when downloads go through a proxy, and anything else is stopped with its own message. Hugging Face's native Xet downloader is switched off so every download goes through these checks.
- **Downloads work behind a firm's inspecting proxy.** Certificates are checked against the Windows certificate store (truststore), so a proxy certificate that IT has installed is trusted without any unsafe workaround. A fixed proxy is needed: proxies given only by an automatic configuration script (PAC or WPAD) are not used.
- **The speaker model is fully checked before every use.** Its files are re-hashed each time they are loaded, not just compared by size and date, because the model format can run code.

Security hardening (#111, slice A3):

- **Recordings are decoded in a separate, sandboxed process.** Each imported file is decoded by a short-lived helper that runs at Windows' Low integrity level (the level browsers use for web pages) inside a Windows job. Windows stops it writing your files, opening the app's process, starting programs directly or using more than 2 GB of memory, and normally also the clipboard (inside some managed sessions the session's own limits apply instead), and it ends when the app closes. It sends back only plain audio, which the app checks, writes and hashes itself. It can still read files you can read, so a decoder flaw would mainly be a privacy risk; the firewall rule for managed devices (#111 B7) closes the network side.
- **Only real recordings are opened.** The decoder accepts only the supported recording formats and local files, whatever a file claims to be, so playlists, stream descriptions and scripts disguised as recordings are refused.
- **Limits on what a file can do.** Decoding stops at 12 hours of audio and keeps at least 1 GB of disk space free.
- **Network drives are refused for imports.** Files on mapped network drives, or reached through a symbolic link, are refused with a message to copy them first. Synced folders such as OneDrive are unaffected.

Security hardening (#111, slice A2):

- **Settings are checked when the app starts.** A value in `settings.json` that the app would not accept from Settings (for example an unknown speech model) is replaced by its default.
- **The window refuses to open unsafely.** The app now stops with a message if Windows would fall back to the old Internet Explorer engine instead of WebView2, or if its window protections cannot be put in place.
- **Tighter limits on what the page can ask for.** The page can show only the app's own screen, drag and drop accepts only files Windows reports as dropped, and calls with the wrong arguments are refused before they run.
- **Less left behind.** The window's browser data is kept inside the workspace folder and cleared at the next start if the app did not close cleanly, and SQLite keeps its temporary data in memory rather than in the Windows temp folder.
- **The handoff file name no longer contains the meeting title.** Save suggests `Meeting transcript YYYY-MM-DD HH-MM.md` (when the meeting was imported), so client names in titles do not end up in file names by default.
- **A Hugging Face token left by the earlier app is no longer passed on** to the app's other processes. The "use the token from the earlier app" option still works.
- **The app refuses a missing, synced or network app data folder** instead of falling back to the current folder.
- **Only one copy runs at a time.** Opening Unminute again while it is open shows a message instead of starting a second copy on the same workspace.

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

- **New name: Unminute.** "Unlimit your meeting minutes." The app previously called Meeting Workspace is now Unminute: the window title, Start menu shortcut, Settings > Apps entry, installer messages, handoff source line and package name (`Unminute-<version>.zip`). Updating replaces the old "Meeting Workspace" Start menu shortcut, but only one that points into this install. Meetings, settings, models, the install folder, the uninstall registry key used for Intune detection and the taskbar identity are unchanged, so nothing moves and no migration is needed.

## v0.2.3

Discard, review layout and privacy improvements, and a new README. Install: download `WhisperWorkspace-0.2.3.zip` and its `.sha256` file, then follow the README or docs/PACKAGING.md. An installed v0.2.x updates and keeps your workspace. The known limitations listed for v0.2.0 and v0.2.1 still apply, except that unplugging a USB microphone mid-recording and dragging a recording onto the window have now been tested on hardware (#30, #27).

- **Discard a meeting you do not need.** Meetings in the Inbox, and in review before the transcript review is confirmed, have Discard…, for test or wrong recordings. One explicit confirmation deletes the meeting's audio, transcript, edits and speaker names, using the same secure cleanup as the review steps, and leaves only the usual minimal receipts. Work in progress on it stops first. It is refused while a retention hold is active or the recording is still importing, and the original recording file is never deleted. A reviewed meeting is still finished with the final confirmation.
- **Dismiss import errors.** Messages such as "The file is in cloud storage" now have a Dismiss link.
- **Separate scrolling in the meeting view.** In a wide window the transcript and the steps panel scroll separately, so the title, toolbar and search box stay in view. Narrow or zoomed windows keep one page scroll.
- **Long meeting titles** now use the available width instead of being cut short.
- **Clearer microphone disconnection message.** When a microphone is unplugged mid-recording, Windows also reports a brief overrun. That is now part of the disconnection message rather than a separate "could not keep up" gap.
- **ONNX Runtime telemetry off in the workspace.** The workspace now switches off ONNX Runtime's Windows telemetry events, as the earlier app already did, before any speech model loads.
- **New README** centred on Meeting Workspace, with current screenshots of a fictional sample meeting. The earlier app's documentation moved to docs/EARLIER_APP.md.

## v0.2.2

Fixes and Setup improvements. Install: download `WhisperWorkspace-0.2.2.zip` and its `.sha256` file, then follow docs/PACKAGING.md. An installed v0.2.x updates and keeps your workspace. The known limitations listed for v0.2.0 and v0.2.1 still apply.

- **Pause speaker identification.** Meetings in the Active list now show Pause while speakers are being identified. Speaker identification works on the whole meeting at once, so Pause stops the pass and Resume starts it again; the transcript is not affected. Previously it could not be paused, which also blocked downloads in Setup.
- **Graphics card component downloads while processing.** It is used only by graphics card speaker passes, so it can be downloaded or checked while a meeting is processed on the processor.
- **Remove models in Setup.** Each downloaded model, and the graphics card component, has a Remove button, with a confirmation. A model still needed is not removed: the selected speech model, one a meeting is still being transcribed with, and the speaker model while speaker identification is on. The voice detector shared by the Parakeet models goes with the last of them.

## v0.2.1

Adds optional graphics card acceleration for speaker identification. Install: download `WhisperWorkspace-0.2.1.zip` and its `.sha256` file, then follow docs/PACKAGING.md. An installed v0.2.0 updates and keeps your workspace.

The known limitations listed for v0.2.0 still apply. In addition, graphics card acceleration has been tested on one NVIDIA laptop GPU (RTX 500 Ada, 4 GB, driver 595.95) with synthetic audio. Agreement with several real speakers, smaller graphics cards and drivers older than 580 are untested; AMD and Intel graphics are not supported.

- **Graphics card for speaker identification (optional).** On computers with an NVIDIA card, Setup offers a one-time, verified download of the CUDA build of PyTorch (1.92 GB). With it turned on in Settings (Faster mode), speaker identification runs on the graphics card. On a 15-minute test recording on an RTX 500 laptop GPU it took 80 s instead of 435 s, with identical results. Transcription stays on the processor. If the card cannot be used, that pass runs on the processor and the meeting says so.

## v0.2.0

**Meeting Workspace**: a Windows app that transcribes, reviews and cleans up meeting recordings on your own computer. This first full release brings together the four previews (v0.2.0-preview.1 to preview.4).

**Install:** download `WhisperWorkspace-0.2.0.zip` and its `.sha256` file, then follow docs/PACKAGING.md. It installs for your Windows account without administrator rights. It is built for unattended deployment through Intune, which has not yet been tested on a managed device (see Known limitations). An installed preview updates and keeps your workspace.

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
- **Alternative models.** Parakeet v3 and Whisper large-v3 turbo were tested in the earlier app's engine, not in the workspace. In that test, Parakeet v3 dropped speech on some clips.
- **Microphone hardware.** Unplugging a USB or Bluetooth microphone mid-recording has not been tested on hardware (#30). Dragging a recording onto the window has not been tested on hardware either; the Import dialog has.
- **No GPU acceleration.** It is not included.

**The earlier source-installed app** (v0.1.0, `install.cmd`) remains in the repository. Its changes are listed under v0.2.0-preview.1.

## v0.2.0-preview.4

**Fourth preview** of the packaged meeting workspace, for testing. It installs per user without admin rights. An installed earlier preview updates in place. The open checks are the same as for preview.3 (see docs/RELEASE_CHECKLIST.md and docs/VALIDATION_RUNBOOK.md).

Download `WhisperWorkspace-0.2.0-preview.4.zip` and its `.sha256` file, then follow docs/PACKAGING.md.

- **App icon.** Meeting Workspace now shows the microphone icon in the Start menu, the window, the taskbar and Settings > Apps, instead of Python's. A pinned taskbar shortcut groups with the running window.

## v0.2.0-preview.3

**Third preview** of the packaged meeting workspace, for testing. It installs per user without admin rights. An installed preview.1 or preview.2 updates in place.

Not yet verified (see docs/RELEASE_CHECKLIST.md and docs/VALIDATION_RUNBOOK.md):
- a clean standard-user profile, and Intune deployment (#10, #11);
- transcription quality on representative business recordings (#12, #25);
- a screen-reader pass and testing with nontechnical users (#27);
- resource use on the lowest-supported laptop (#28);
- unplugging a USB or Bluetooth microphone during a recording (#30).

Download `WhisperWorkspace-0.2.0-preview.3.zip` and its `.sha256` file, then follow docs/PACKAGING.md.

- **Microphone recording (optional, off by default).** Record a meeting from a chosen microphone, with a 3-second microphone test, a level meter, and pause, resume and stop. Audio is saved to disk every few seconds. After a crash the next start imports what was saved and reports roughly what is missing. Overruns, a full disk and a disconnected microphone are reported, never hidden, and the recording never switches device. Other transcription waits while recording. Recordings then follow the usual review, hold and cleanup steps. Turn it on in Settings > Microphone (#30).

## v0.2.0-preview.2

**Second preview** of the packaged meeting workspace, for testing. It installs per user without admin rights. An installed preview.1 updates in place.

Not yet verified (see docs/RELEASE_CHECKLIST.md):
- a clean standard-user profile, and Intune deployment (#10, #11);
- transcription quality on representative business recordings (#12, #25);
- a screen-reader pass and testing with nontechnical users (#27);
- resource use on the lowest-supported laptop (#28).

Download `WhisperWorkspace-0.2.0-preview.2.zip` and its `.sha256` file, then follow docs/PACKAGING.md.

- **Zoom and accessibility fixes.** Ctrl+plus, Ctrl+minus, Ctrl+0 and Ctrl+mouse wheel now zoom the workspace window (50% to 300%); zoom had been turned off. At 400% zoom the pages and dialogs fit the window without sideways scrolling. The speech model list and the confirmation dialog now have names for screen readers.
- **Faster mode no longer slows Parakeet down.** On a test laptop with performance and efficiency cores (Intel Core Ultra 7 255H), Faster mode made Parakeet 3 to 4 times slower than Quiet mode. ONNX Runtime threads now wait without spinning. On that laptop, 5 minutes of speech now takes 19 s in both modes (previously 20 to 24 s in Quiet and 73 to 80 s in Faster). Faster mode still speeds up speaker identification.

## v0.2.0-preview.1

**Preview release** of the packaged meeting workspace, for testing. It installs per user without admin rights and runs alongside the existing app.

Not yet verified (see docs/RELEASE_CHECKLIST.md):
- a clean standard-user profile, and Intune deployment (#10, #11);
- transcription quality on representative business recordings (#12, #25);
- testing with nontechnical users (#27);
- resource use on the lowest-supported laptop (#28);
- the final fetch step for OneDrive online-only files (#24).

Download `WhisperWorkspace-0.2.0-preview.1.zip` and its `.sha256` file, then follow docs/PACKAGING.md. The earlier source app is unchanged and still installs with `install.cmd`.


- Docs: a business-user guide for the meeting workspace (docs/WORKSPACE_GUIDE.md) and a release checklist with tested compatibility (docs/RELEASE_CHECKLIST.md). The privacy wording now separates local processing from synced storage and from deliberate sharing with an AI assistant (#29, #8).
- Workspace updates: one gate for self-service and IT-managed installs. An update that changes how the workspace is stored waits, and explains why, until there are no meetings, retention holds or unfinished cleanup. Holds are never expired and no content backups are made. Compatible updates keep everything, and rollback to the previous version is supported (#26).
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
- Fix: Whisper large-v3-turbo could fail with "Invalid input features shape: expected (1, 128, 3000), got (1, 80, 3000)" when its cached `preprocessor_config.json` was missing, malformed or lacked `feature_size` (faster-whisper then falls back to 80 mel bands). Whisper models now have pinned SHA-256 manifests like Parakeet; a cache folder existing is no longer treated as a working model; preprocessing settings are validated; and the loaded feature width is checked against the model before "Model ready" or speaker identification.
- Update / repair (and Start queue) now repair damaged Whisper models instead of skipping any existing folder. A verified copy is downloaded beside the old cache, which is left untouched for diagnosis.
- Speaker identification model (pyannote speaker-diarization-community-1) is pinned to SHA-256 checksums and loaded only from a verified folder, because pyannote loads its weights with pickle, which can run code from a tampered file. `docs/MODEL_MANIFEST.md` lists every model's licence, revision and file hashes (#9).
- If speaker identification fails, the file is still transcribed without speaker labels instead of failing, and speaker identification is skipped for the rest of that run.
- Parakeet engine (issue #13): NVIDIA parakeet-tdt-0.6b-v2 (English) is the recommended model for new installs, with parakeet-tdt-0.6b-v3 (multilingual) as an alternative. Both run as INT8 ONNX on the CPU via onnx-asr. Existing Whisper models and saved model choices are unchanged.
- Parakeet model files are pinned to exact revisions and SHA-256 checksums. A folder that merely exists is not treated as installed; damaged copies are set aside (not deleted) and replaced by Start queue or Update / repair.
- All engines now share one timed-segment interface. Markdown transcripts record the engine, precision and model revision.
- Model-free unit tests run in CI.
- onnxruntime upgraded from 1.26.0 to 1.30.0 (onnx-asr does not support 1.26.0).
- Installer now requires Python 3.12 and installs it per-user if absent; 3.10 and 3.11 are no longer accepted because numpy 2.5, scipy 1.18, PyAV 18 and others no longer publish Windows wheels for them.
- CI: wheels-only resolve check, ruff and PSScriptAnalyzer added beside the syntax check; CodeQL weekly; Dependabot grouped monthly updates with the torch family limited to patch releases.

## v0.1.0

First public release. Local, offline speech-to-text for Windows with speaker
identification, built for a locked-down professional-services office.

### Features

- Transcribes audio and video files (mp3, m4a, wav, mp4, mov, mkv, flac, ogg, aac, wma, webm, avi) with OpenAI Whisper models via faster-whisper, running int8 on the CPU.
- Speaker identification with the pyannote speaker-diarization-community-1 pipeline. Speakers can be renamed after a file finishes.
- Fully offline once installed. Hugging Face offline mode is enforced at import time.
- File queue with pause, resume and stop. Stopping saves a partial transcript.
- Output as .txt or Markdown .md, with optional per-paragraph timestamps. Existing transcripts are never overwritten.
- Light and dark themes, drag and drop, Start Menu shortcut and taskbar icon.

### Installation

- One double-click installer (`install.cmd`) that needs no admin rights, no FFmpeg and no GPU. Everything lives under the user profile.
- Finds an existing CPython 3.10 to 3.12 or installs Python 3.12 per user.
- Fully pinned dependency set installed from wheels only.
- Optional Hugging Face token step for speaker identification.
- Standard (large-v3-turbo) or Full (adds large-v3) model download, with a default suggested from installed RAM.
- Idempotent: re-run at any time to repair packages or add the larger model. Unattended switches for rollout to several machines.

### Requirements

- Windows 10 or 11, 64-bit.
- About 8 GB free disk for a fresh install. 16 GB RAM for the standard model, 32 GB recommended for large-v3.
- Internet access during installation only.

### Install

Download the source zip below, extract it somewhere not synced by OneDrive, and double-click `install.cmd`. Full instructions are in the README.
