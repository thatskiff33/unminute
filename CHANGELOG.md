# Changelog

Release notes for Whisper Transcriber. The Release workflow reads the section
for the version being released, so keep one `## vX.Y.Z` heading per version.

## Unreleased

- Privacy (issue #8): pyannote.audio's default usage metrics (OpenTelemetry to otel.pyannote.ai) are now forced off, together with Hugging Face, OpenTelemetry and ONNX Runtime telemetry. The app process also refuses all non-local network access outside deliberate model downloads, so no usage statistics can be sent.

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
