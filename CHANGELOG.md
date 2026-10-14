# Changelog

Release notes for Whisper Transcriber. The Release workflow reads the section
for the version being released, so keep one `## vX.Y.Z` heading per version.

## Unreleased

- **Zoom and accessibility fixes.** Ctrl+plus, Ctrl+minus and Ctrl+mouse wheel now zoom the workspace window, which pywebview had turned off. At 400% zoom the pages and dialogs fit the window without sideways scrolling. The speech model list and the confirmation dialog now have names for screen readers.
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
