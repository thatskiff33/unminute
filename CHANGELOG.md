# Changelog

Release notes for Whisper Transcriber. The Release workflow reads the section
for the version being released, so keep one `## vX.Y.Z` heading per version.

## Unreleased

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
