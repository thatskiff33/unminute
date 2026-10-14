# Changelog

Release notes for Whisper Transcriber. The Release workflow reads the section
for the version being released, so keep one `## vX.Y.Z` heading per version.

## Unreleased

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
