# The earlier app (source-installed Whisper Transcriber)

This is the original desktop app: a file queue with a tkinter window, installed from source with `install.cmd`. It still works and is kept in the repository, but [Unminute](../README.md) (previously called Meeting Workspace) is the current app. The two keep separate outputs and share the downloaded models.

![Whisper Transcriber, light theme, mid-run](screenshots/main-light.png)

## What it does

- Transcribes audio and video files (mp3, m4a, wav, mp4, mov, mkv, flac, ogg, aac, wma, webm, avi) on the CPU, using NVIDIA Parakeet (recommended for English) or OpenAI Whisper models.
- Works out who said what (Speaker 1, Speaker 2, ...) and lets you rename the speakers afterwards.
- Runs fully offline once installed. The only thing that ever goes online is the installer and a one-off model download.
- Queues many files and processes them one after another. Pause, resume and stop are safe. Stopping saves what has been transcribed so far.
- Saves plain .txt or Markdown .md with optional timestamps on each paragraph. Never overwrites an existing transcript.
- Needs no admin rights, no FFmpeg, no GPU. Everything lives under your own user profile.
- Light and dark themes, drag and drop, a Start Menu shortcut and a proper taskbar icon.

## Screenshots

Screenshots show sample data, not real recordings.

| First launch | Dark theme |
| --- | --- |
| ![Idle window](screenshots/main-idle.png) | ![Dark theme](screenshots/main-dark.png) |

Rename the detected speakers once a file has finished. The first line each person spoke is shown to help you work out who is who.

![Rename speakers dialog](screenshots/rename-speakers.png)

## How it works

- Parakeet models are NVIDIA's parakeet-tdt-0.6b, run as INT8 ONNX exports through [onnx-asr](https://github.com/istupakov/onnx-asr) and ONNX Runtime. Silero voice-activity detection splits recordings into speech pieces of at most 20 seconds, which avoids the export's hard failure on very long single inputs, and each piece is split into sentences using token timings.
- Every model's files, speech and speaker identification alike, are pinned to SHA-256 checksums, listed in [docs/MODEL_MANIFEST.md](MODEL_MANIFEST.md) with their licences. A model only counts as installed when every expected file is present at the right size (and, for Whisper, `preprocessor_config.json` asks for the right number of mel bands); it is fully checksummed before first use. A folder merely existing is never taken as proof. This matters for the speaker model in particular: pyannote loads its weights with Python's pickle format, which can run code, so only verified files are loaded.
- If speaker identification fails, the file is still transcribed, without speaker labels, and the log says why.
- Damaged models are never deleted. A damaged Parakeet copy is moved aside as `<folder>.invalid-<date>`. A damaged Whisper copy in the Hugging Face cache is left untouched, and a verified copy is downloaded beside it into `whisper-models\whisper`.
- After a Whisper model loads, the app checks that its audio preprocessing produces the number of mel bands the model expects (128 for large-v3 and turbo) before transcribing or identifying speakers. A mismatch stops with a repair instruction instead of failing part-way.
- Whisper models use [faster-whisper](https://github.com/SYSTRAN/faster-whisper), a CTranslate2 port of OpenAI Whisper. All models run in int8 on the CPU, and a couple of cores are left free so the laptop stays usable while a long job runs.
- Speaker identification uses the [pyannote.audio](https://github.com/pyannote/pyannote-audio) speaker-diarization-community-1 pipeline. Each transcript segment is assigned to the speaker whose turn overlaps it most.
- Audio is decoded in memory with PyAV, which ships inside the faster-whisper wheel. That is why FFmpeg is not required.
- Only one speech model is held in memory at a time, and nothing heavy loads until you click Start queue. Idle memory use is a few hundred MB. This is what makes it workable on 16 GB laptops.
- Hugging Face offline mode is switched on at import time, so once a model is cached the libraries make no network calls at all, not even version checks. A deliberate download temporarily lifts the flag and restores it afterwards.

## Requirements

- Windows 10 or 11, 64-bit. The app uses Windows-only APIs (registry, shell, taskbar identity) and has not been tested elsewhere.
- About 8 GB of free disk for a fresh install (Python environment plus models).
- 16 GB RAM for the standard model. 32 GB recommended if you want the larger, slower large-v3 model as well.
- Internet access during installation only.
- No admin rights. If Python 3.12 is not already present the installer fetches a per-user copy. Other Python versions on the machine are left alone and not used, because several pinned packages no longer ship Windows wheels for 3.10 or 3.11.

## Install

### 1. Get the code

Download the source zip for the latest release from https://github.com/thatskiff33/unminute/releases and extract it. Releases are tagged and tested; the green Code button gives you whatever is on `main` right now. Developers can clone instead:

```bash
git clone https://github.com/thatskiff33/unminute.git
```

Put the folder somewhere that is not synced by OneDrive, for example `C:\Users\<you>\Local_Repos\whisper-transcriber`. Transcripts are saved into an `extracted` folder next to the app, and you probably do not want those syncing to the cloud. The installer warns you if it detects a OneDrive path.

### 2. Run the installer

Double-click `install.cmd`. It runs `install.ps1` with a process-scoped execution policy bypass, which works on locked-down machines because nothing system-wide changes.

The installer walks through eight steps and prints what it is doing at each one:

1. Preflight. Checks free disk space, warns about OneDrive, reports installed RAM.
2. Python. Finds an existing CPython 3.12, or installs Python 3.12 per-user.
3. Virtual environment. Creates `%LOCALAPPDATA%\whisper\.venv`.
4. Packages. Installs the pinned dependency set from `requirements.txt`, wheels only, about 1.2 GB.
5. Hugging Face token. Optional, only needed for speaker identification. See the next section.
6. Speech models. Asks whether you want Parakeet (English, about 0.7 GB, recommended), Standard (Whisper large-v3-turbo, about 1.6 GB) or Full (turbo plus large-v3, about 4.6 GB). Every run (unless `-SkipModels` is used) also checks every speech model already installed and repairs any whose files do not match their checksums.
7. Start Menu shortcut. Adds Whisper Transcriber to the Start Menu with the app icon.
8. Self-check. Imports every component to confirm the install works.

A completed run looks like this (this example skipped the token and shortcut steps):

![Installer output](screenshots/installer.png)

The installer is idempotent. Run it again at any time to repair packages or add the larger model. It re-verifies every installed speech model against its checksums and preprocessing settings, so Update / repair fixes a damaged model rather than skipping it because its folder exists. Checking large models takes a minute.

### 3. Launch

Start Menu, search for Whisper Transcriber. Or double-click `whisper-ui.cmd` in the app folder.

### Unattended install

For rolling out to several machines, `install.ps1` accepts switches so it never stops to ask:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Models turbo -SkipToken
```

| Switch | Effect |
| --- | --- |
| `-Models parakeet` | Parakeet English (parakeet-tdt-0.6b-v2) only |
| `-Models turbo` | Standard install, large-v3-turbo only |
| `-Models full` | Turbo plus large-v3 |
| `-SkipModels` | Do not download models now. The app offers to download on first use. |
| `-SkipToken` | Skip the Hugging Face token step. Speaker identification stays unavailable until a token is set. |
| `-SkipShortcut` | Do not create the Start Menu shortcut |

## Speaker identification setup (optional, free)

Telling speakers apart uses a pyannote model that is gated on Hugging Face. You need a free account and a read token. Plain transcription works without any of this.

1. Create a Hugging Face account at https://huggingface.co/join
2. Open https://huggingface.co/pyannote/speaker-diarization-community-1 and accept the terms. It is one click and access is instant. The installer also lists https://huggingface.co/pyannote/speaker-diarization-3.1. Accepting that one too is harmless, but only community-1 is used.
3. Create a token of type Read at https://huggingface.co/settings/tokens
4. Paste the token into the installer when it asks. If you skipped that step, run `install.cmd` again.

The installer stores the token as the `HF_TOKEN` user environment variable, verifies it against Hugging Face, confirms the gated model is accessible, and downloads and verifies the model (about 30 MB). After that the token is not used for anything. Your audio never leaves the machine.

## Choosing a model

| Model | Disk | Notes |
| --- | --- | --- |
| parakeet-tdt-0.6b-v2 (recommended) | about 0.7 GB | English only. Much faster than Whisper on the CPU. |
| large-v3-turbo | about 1.6 GB | Whisper. Excellent accuracy, reasonable speed. |
| large-v3 | about 3 GB | Whisper. Several times slower than turbo; only worth it for difficult audio on a powerful laptop. 32 GB RAM recommended. |
| parakeet-tdt-0.6b-v3 | about 0.7 GB | Multilingual Parakeet. Detects the language itself; the English model is the better choice for English. |

Parakeet as the recommended English model is provisional. NVIDIA reports a slightly lower average English word error rate for v2 (6.05%) than v3 (6.34%) on the Hugging Face Open ASR Leaderboard, but those figures are for NVIDIA's original models, not these INT8 exports, and they are not a measurement on business meeting audio. A comparison on consented representative recordings is tracked in issue #12. Check transcripts of important meetings against the audio whichever model you use.

Rough guide from our own use: a one-hour recording takes about 15 to 30 minutes with turbo on a recent i5 or i7, faster on higher-end CPUs. large-v3 takes several times longer. Speaker identification adds a few minutes per hour of audio.

Parakeet test figures (13 October 2026, Intel Core Ultra 7 255H, 32 GB RAM, synthetic speech, so treat these as indicative only): a 15-minute recording transcribed in about 4 minutes with peak memory of 1.2 GB. On the same 51-second two-speaker clip, Parakeet took 12 seconds to transcribe and turbo 56 seconds. Parakeet sometimes writes numbers as words ("fifty seven") rather than digits, which matters if you search transcripts.

If you select a model that is not installed yet, the app offers to download it rather than failing. If an installed model is damaged, its label says "needs repair" and Start queue offers to replace it.

## Using the app

1. Drag audio or video files onto the window, or click Add files.
2. Pick the model, the output format (.txt or .md), and whether you want timestamps and speaker identification. Options are read when you click Start queue and apply to that whole run.
3. Click Start queue. The first run loads the model into memory, which takes around 15 seconds on a recent laptop. It stays loaded for later runs.
4. Watch the status line for the current file, the phase (identifying speakers, transcribing), elapsed time, percent complete and estimated time left. Flip on Show details to see the transcript being produced live.
5. When a file finishes, double-click its row to open the transcript, or right-click for Open transcript, Show in folder, Rename speakers and Remove from list.

Other behaviour worth knowing:

- Transcripts go into the `extracted` folder next to the app. Name clashes get a " (2)" suffix.
- Pause drops CPU use to zero until you resume. Stop saves a "(partial)" transcript of whatever has been done, and leaves the remaining files queued for the next Start.
- A failed file is marked failed and the queue moves on. Right-click it to see the error.
- Your options, window size, theme and whether the details log is open are remembered between sessions.

A Markdown transcript with speakers and timestamps looks like this:

```markdown
# Transcript: Board meeting 2026-09-08.m4a

- Source: C:\Recordings\Board meeting 2026-09-08.m4a
- Model: large-v3-turbo
- Transcribed: 2026-09-10 14:32

[0:00] Speaker 1: Thanks everyone for joining. Let us start with the finance update.

[0:06] Speaker 2: Revenue for August came in slightly ahead of budget.
```

Consecutive lines from the same speaker are merged into one paragraph. Timestamps are M:SS under an hour and H:MM:SS above it.

## Where things live

| What | Where |
| --- | --- |
| App code | The folder you extracted or cloned |
| Transcripts | `extracted\` inside the app folder |
| Python environment | `%LOCALAPPDATA%\whisper\.venv` |
| UI settings | `%LOCALAPPDATA%\whisper\ui-settings.json` |
| Model weights | `%LOCALAPPDATA%\whisper-models` |
| Hugging Face token | `HF_TOKEN` user environment variable (optional) |
| Start Menu shortcut | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Whisper Transcriber.lnk` |

`%LOCALAPPDATA%` is chosen deliberately: it is per-user, needs no admin, and is never synced by OneDrive.

## Privacy and network behaviour

- Transcription never uses the network. Offline mode is enforced at the library level, so cached models load without any revision or telemetry calls.
- No usage statistics are sent. pyannote.audio 4.x sends anonymous usage metrics to otel.pyannote.ai by default; the app forces this off (`PYANNOTE_METRICS_ENABLED=false`, `OTEL_SDK_DISABLED=true`), along with Hugging Face and ONNX Runtime telemetry, even if your environment says otherwise.
- As a backstop, the app process refuses Python network connections and DNS lookups to anything other than the local machine (including a proxy running on the local machine), apart from deliberate model downloads. It guards the standard Python socket functions that the app's libraries use; it is not a firewall, and native code opening its own sockets would not be caught. For managed deployments, an outbound firewall rule for the app's Python is the stronger control.
- The installer goes online to fetch Python, packages and models.
- The Update / repair button in the app re-runs the installer in a separate window to reinstall packages at their pinned versions and fetch any missing models. It does not pull new code.
- If you select a model that is not yet downloaded, the app asks before downloading it.
- The Hugging Face token authorises one gated download. It is never sent anywhere else.

Local processing is not the whole picture. These are outside the app's control:

- **Synced or backed-up folders.** A recording or transcript saved into OneDrive, SharePoint or a backed-up folder is copied to that service by its sync client. The app does not change that.
- **Anything you paste or upload to an AI assistant.** That text is then handled by the assistant's provider, under your organisation's account and policies.

To verify for yourself, run a transcription and check that the venv Python has no connections to huggingface.co:

```powershell
Get-Process python, pythonw -ErrorAction SilentlyContinue | ForEach-Object { Get-NetTCPConnection -OwningProcess $_.Id -ErrorAction SilentlyContinue } | Where-Object State -eq Established
```

An empty result means no open connections.

## Uninstall

Delete these and you are back to a clean machine:

- `%LOCALAPPDATA%\whisper`
- `%LOCALAPPDATA%\whisper-models`
- The Whisper Transcriber Start Menu shortcut
- The app folder (move your `extracted` transcripts out first)
- Optionally the `HF_TOKEN` user environment variable and the per-user Python 3.12 if the installer added it

## Troubleshooting

- Running scripts is disabled. Always start via `install.cmd` or `whisper-ui.cmd`, not the .ps1 directly. They bypass the execution policy for that one process, which is allowed.
- Speakers option is greyed out. The token step was skipped or the gated model terms are not accepted yet. Re-run `install.cmd` and follow the token step.
- Install fails downloading packages. Usually network or VPN. Re-run `install.cmd`, it resumes where it left off.
- Model not installed dialog on Start queue. You picked a model that was not part of your install choice. Say yes to download it once, or pick an installed model.
- A model says "needs repair". One or more of its files is missing, the wrong size, or (for Whisper) its preprocessing settings are missing or wrong. Say yes when Start queue offers a fresh copy, or click Update / repair. Damaged copies are kept for diagnosis: a damaged Hugging Face cache copy stays in its original `models--...` folder, and a damaged copy the app installed itself is renamed with `.invalid-` and a date (under `whisper-models\parakeet` or `whisper-models\whisper`). Delete them once you no longer need them.
- "Invalid input features shape: expected (1, 128, 3000), got (1, 80, 3000)". Earlier versions showed this when a Whisper model's `preprocessor_config.json` was missing, malformed or lacked `feature_size`, which made faster-whisper fall back to 80 mel bands. This version detects the problem before transcribing; update the app, then run Update / repair.
- App feels slow or fans are loud. Normal while transcribing. It leaves a couple of cores free so you can keep working. Pause if you need full speed elsewhere.
- Window opens off-screen after undocking. The app checks the saved position is on a live monitor and drops it if not. If you still cannot see it, delete `ui-settings.json`.
- Antivirus or Intune warnings. The app is plain Python running locally. Nothing needs admin. Point IT at this README.


## Files (earlier app and shared code)

| File | Purpose |
| --- | --- |
| `transcribe_ui.py` | The tkinter UI (sv-ttk theme, drag and drop, queue, rename dialog, update button) |
| `transcriber_core.py` | Engine: model loading, diarisation, transcript rendering, queue worker. No tkinter. |
| `offline_guard.py` | Forces dependency telemetry off and refuses non-local network access outside deliberate downloads |
| `asr_engines.py` | Shared timed-segment interface for Whisper and Parakeet; model manifests for all speech and speaker models; verified install and repair. `python asr_engines.py ensure --present` is what the installer runs; `python asr_engines.py manifest --out docs/MODEL_MANIFEST.md` regenerates the manifest document. |
| `tests/` | Unit tests: `python -m unittest discover -s tests -v`. The queue-worker tests need the installed app environment and skip without it. |
| `install.ps1` | Idempotent per-user installer. `install.cmd` is the double-click wrapper. |
| `whisper-ui.cmd` | Launches the UI with the venv pythonw.exe, no console window |
| `requirements.txt` | Full pinned freeze of a known-good environment. Install with wheels only. |
| `test_diarization.py` | End-to-end command-line check of diarisation plus transcription on one file |
| `make_icon.py` | Regenerates `whisper.ico` with Pillow |
| `CHANGELOG.md` | Release notes, one section per version. The Release workflow reads it. |
| `.github/workflows/` | `ci.yml` runs the automated checks on pull requests and pushes to main; `codeql.yml` scans on pull requests, pushes to main and weekly; `release.yml` tags and publishes a release. [docs/RELEASING.md](RELEASING.md) lists the required checks. |
| `ruff.toml`, `.github/dependabot.yml` | Lint rule set and Dependabot schedule (monthly, grouped; torch family patch-only) |

Run from source without the shortcut:

```powershell
& "$env:LOCALAPPDATA\whisper\.venv\Scripts\python.exe" transcribe_ui.py
```

Dependencies are pinned on purpose. pyannote.audio 4.x, torch and huggingface_hub have moved quickly and several combinations break on CPU-only Windows without FFmpeg. If you upgrade, re-test speaker identification end to end with `test_diarization.py`.

Notes on pyannote 4.x that cost time to learn:

- The pipeline returns a DiarizeOutput wrapper. Use its `speaker_diarization` attribute, not the object itself.
- torchcodec cannot load without FFmpeg DLLs. Pass a decoded waveform dict instead of a file path.
- Do not set `HF_HUB_DISABLE_IMPLICIT_TOKEN`. The gated download relies on the token being picked up implicitly.
