# Unminute

**Unlimit your meeting minutes.**

Unminute turns meeting recordings into reviewed transcripts on your own Windows computer. You review the transcript against the audio, name the speakers, and hand the text to your organisation's approved AI assistant to draft minutes. Then the app clears its working copies away. Transcription and speaker identification run on your computer, and nothing is uploaded.

![The Meetings page, with meetings ready to review and hand off, and one transcribing](docs/screenshots/workspace-meetings.png)

It was built for a professional-services office where recordings are confidential, laptops are locked down (no administrator rights, Intune managed) and nobody wants to learn a command line. It installs per user, without administrator rights.

The screenshots show a fictional sample meeting, spoken by the Windows built-in text-to-speech voices. No real people or recordings appear in them.

Unminute was previously called Meeting Workspace, and this repository Whisper Transcriber. Only the name changed: an installed copy updates in place and keeps its meetings, settings and models.

## What it does

- **Transcribes on your computer.** Uses NVIDIA Parakeet (recommended for English) or OpenAI Whisper, on the processor. Accepts mp3, m4a, wav, mp4, mov, mkv, flac, ogg, aac, wma, webm and avi.
- **Works out who said what.** Optional speaker identification with pyannote. You name the speakers after listening to short clips.
- **Review against the audio.** Play the meeting from any passage, with the transcript following along. Correct text in place. Edits save automatically and can be undone, even after a restart.
- **Hands off safely.** Copies a prompt and the reviewed transcript for your AI assistant, or saves Markdown. For the hand off only, you can replace names with Speaker 1, Speaker 2, leave out passages or redact words.
- **Cleans up in two confirmed steps.** The audio goes once you confirm the transcript is reviewed. The transcript goes once you confirm the final document is saved elsewhere. Holds stop cleanup for as long as you need.
- **Fits around your work.** Recordings come from Import, drag and drop, watched folders or the microphone. Quiet mode leaves most of the computer free. Pause and resume are safe, and nothing already transcribed is lost after a crash.
- **Stays offline.** Models are downloaded once, when you choose, and checked against published checksums. No usage statistics are ever sent.

![A meeting in review: the steps across the top, the transcript on the left and the speakers on the right](docs/screenshots/workspace-meeting.png)

## Requirements

- **Windows:** 64-bit Windows 11, or Windows 10 version 1809 or later, on an x64 processor. Testing so far has been on Windows 11 only.
- **Microsoft Edge WebView2 Runtime:** built into Windows 11 and present on most managed Windows 10 devices.
- **Disk space:** about 6 GB free.
- **Processor and memory:** the minimum is 4 cores and 8 GB of memory, and 8 cores and 16 GB are recommended. Below the minimum, the installer warns and asks before continuing (an unattended install records the warning and continues).
- **Graphics card (optional):** an NVIDIA card with driver version 580 or later can speed up speaker identification. It is not needed.

You do not need Python, Git, FFmpeg or any GPU tools. The package brings its own private runtime.

## Install

1. Download `Unminute-<version>.zip` and `Unminute-<version>.zip.sha256` from the [latest release](https://github.com/thatskiff33/unminute/releases/latest).
2. Extract the zip.
3. Double-click `install.cmd`.

The installer checks the requirements and every file in the package, then installs for your Windows account only. It adds an **Unminute** shortcut to the Start menu. Installation takes a few minutes, mostly checking files.

Before extracting, you can check the zip against its `.sha256` file. In PowerShell, in the download folder, the two values printed must match:

```powershell
(Get-FileHash Unminute-<version>.zip -Algorithm SHA256).Hash
Get-Content Unminute-<version>.zip.sha256
```

Unattended and Intune deployment, exit codes, detection rules and signing are covered in [docs/PACKAGING.md](docs/PACKAGING.md).

### First start: Setup

The first start opens Setup, five short steps. Each model is a one-off download that the app checks before use.

1. **Welcome:** how a meeting moves through the app.
2. **Speech model:** Parakeet v2 is recommended (0.66 GB). Parakeet v3 and Whisper large-v3 turbo are under Other options.
3. **Speaker names (optional, 0.03 GB):** the speaker identification model's publisher asks you to accept its terms with a free Hugging Face account and a "Read" token, once. Setup links to each page. The token can be remembered in Windows Credential Manager and removed at any time. Downloaded models keep working without it.
4. **Microphone (optional):** turn on recording and test the microphone.
5. **Ready:** a summary of what is set up.

You can skip the optional steps and turn them on later in Settings. **Graphics card acceleration** is offered only on computers with an NVIDIA card. It is a 1.92 GB download of the CUDA build of PyTorch, used for speaker identification only, and is in Settings > Manage downloads. That page also checks, repairs and removes downloaded models.

![Setup, step 2: downloading the recommended speech model](docs/screenshots/workspace-setup.png)

## Everyday use

### Bring in a recording

- **Import a recording** on the Meetings page, or drop audio or video files anywhere on the window. Recordings must be on this computer: files on mapped network drives, or reached through a symbolic link, are refused (copy them across first). Synced folders such as OneDrive are fine.
- **Watched folders** (Settings) pick up new recordings automatically. See [docs/INTAKE.md](docs/INTAKE.md) for synced and cloud folders.
- **Record a meeting** appears once microphone recording is on (Setup, or Settings > Recording and speakers). Tell everyone present that the meeting is being recorded. Only the microphone is recorded, not sound playing on the computer.

Transcription runs in the background, one meeting at a time. The Meetings page groups meetings by what they need next: **Needs attention**, **Ready for you**, **Transcribing** and **Cleaning up**. Transcribing meetings show progress, with **Pause** and **Stop now**.

![Recording a meeting, with the level meter and timer](docs/screenshots/workspace-recording.png)

### Review and name speakers

Open a meeting to review it. The steps (Transcribe, Review, Hand off, Clean up) run across the top, with the next action beside them.

- Select a time to play from that passage; the player at the bottom carries on through the meeting. While the audio is kept, Space plays or pauses, and J and K move between passages.
- Select text, or press Enter, to correct it. Ctrl+Z undoes the last edit.
- Search with Ctrl+F. In a wide window the transcript and the side panel scroll separately.
- **Name speakers** plays short examples for each detected speaker. You can merge two labels that are the same person, or move a passage to another speaker. Names stay with this meeting only.
- The side panel's tabs show **Speakers**, **Versions** (Transcribe again with another model keeps your earlier version and its edits) and **Holds**.
- **Help and shortcuts** in the sidebar lists every keyboard shortcut. Press ? (outside a text field) to open it.

![Naming speakers from short examples](docs/screenshots/workspace-names.png)

### Hand off and clean up

Each meeting shows its steps:

1. **Transcript reviewed…** saves the reviewed text and removes the app's copy of the audio. If you allowed it for the watched folder a recording came from, the original there is removed too. Playback and editing end here.
2. **Hand off…** opens a page that shows exactly what will be copied. Choose a template, then remove details for this hand off only: replace names with Speaker 1, Speaker 2, redact words, or leave out passages, and select **Apply**. Then **Copy prompt and transcript**, **Copy prompt only** or **Save Markdown…**.
3. **Create the final document** in your assistant, review it yourself, and file it where your organisation keeps such documents.
4. **Final document saved…** removes the meeting's transcript, edits and speaker names. A minimal receipt (a random number, times and the result) is kept for 30 days.

![Hand off: the template, details to remove and a preview of what will be copied](docs/screenshots/workspace-handoff.png)

Copying or saving never deletes anything by itself, and the app does not clear your clipboard. Cleanup cannot reach copies in synced folders, on a phone or recorder, or anything you shared with another service.

- **Holds.** Hold a meeting's audio, transcript or everything, with a reason. A hold stops the matching cleanup and never expires by itself. Releasing it deletes nothing: the cleanup waits for you to confirm it again.
- **Discard….** A test or wrong recording can be discarded before its review is confirmed. Discard is refused while a hold is active, and your original recording file is never deleted.

The full walkthrough is in the [user guide](docs/WORKSPACE_GUIDE.md).

## Settings

- **Processing:** Quiet (the default) leaves most of the computer free. Faster uses more of it. On battery, automatic work waits unless you allow it.
- **Speech model:** which model new meetings use, and **Manage downloads** for models and the graphics card component.
- **Recording and speakers:** microphone recording, naming speakers after transcription, and the graphics card for it (Faster mode only).
- **Watched folders, Updates, Cleanup receipts.**

![Settings](docs/screenshots/workspace-settings.png)

## Privacy and network behaviour

- **Transcription and speaker identification never use the network.** While processing, the app refuses its own Python network connections and DNS lookups to anything outside the computer. This guard is not a firewall: native code opening its own sockets would not be caught. For managed devices, an outbound firewall rule is the stronger control.
- **No usage statistics are sent.** Before its libraries load, the app forces their telemetry switches off: Hugging Face, pyannote's usage metrics and OpenTelemetry. A setting in your environment cannot turn them back on. It also switches off ONNX Runtime's Windows telemetry events in the processes that use it.
- **Downloads happen only in Setup or Manage downloads, when you choose.** Models come from Hugging Face (huggingface.co and its download hosts under huggingface.co and hf.co) and the optional graphics card component from download-r2.pytorch.org. The download process can reach only those hosts and any proxy configured for you: every request and every redirect is checked, also through a proxy. Certificates are checked against the Windows certificate store, so downloads work behind a firm's TLS-inspecting proxy. A fixed proxy (set in Windows or with HTTPS_PROXY) is used; a proxy given only by an automatic configuration script (PAC or WPAD) is not, so IT may need to set a fixed one or pre-stage the models. Each file is checked against a published checksum.
- **The Hugging Face token** is used only for the speaker model download. It is passed to the download process privately, never on its command line or in its environment.
- **Outside the app's control:** recordings in synced folders such as OneDrive are already in that service, and anything you paste or upload to an AI assistant is handled by its provider under your organisation's account and policies.

The threat model, the controls in place and the remaining risks are in [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md). To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Where things live

| What | Where |
| --- | --- |
| Program | `%LOCALAPPDATA%\Programs\WhisperWorkspace\versions\<version>` |
| Workspace (meetings, edits, holds, settings) | `%LOCALAPPDATA%\whisper\workspace` |
| Models, and the optional graphics card component | `%LOCALAPPDATA%\whisper-models` |
| Hugging Face token (if remembered) | Windows Credential Manager |
| Start menu shortcut | Unminute |

`%LOCALAPPDATA%` is per user, needs no administrator rights and is not synced by OneDrive. The folder names keep the app's earlier names so that existing installs, meetings and models carry over without being moved.

## Updates, rollback and uninstall

- **Update:** run the new package's `install.cmd` with the app closed. An update that keeps the storage format keeps everything. One that changes it waits until the workspace is empty (no meetings in the workspace, active holds, unfinished cleanup or unimported microphone recordings), and says what it is waiting for. No update expires a hold, deletes your work or makes a backup copy of it.
- **Rollback:** the previous version is kept. To return to it, run `install.ps1 -Rollback` from the current version's folder (`versions\<version>`). Rollback is refused if the previous version cannot open your workspace, for example after a storage-format change.
- **Uninstall:** Settings > Apps > Unminute. The workspace and downloaded models are kept, because meeting content is only removed through the app's confirmed cleanup steps.

Details are in [docs/PACKAGING.md](docs/PACKAGING.md).

## Troubleshooting

| Problem | What to do |
| --- | --- |
| A model shows "Damaged" or "Needs attention" | Settings > Manage downloads (the Downloads page) > Repair. The damaged copy is kept aside, not deleted. |
| Speaker identification did not work | The transcript is still usable. Check the speaker model in Settings > Manage downloads, then use Identify again in the meeting's Speakers tab. |
| A watched recording is not imported | Settings > Watched folders lists files still copying, cloud-only files and unreadable files. |
| Nothing is processing on battery | Plug in, or allow processing on battery in Settings. |
| The installer says the app is running | Close Unminute and run it again. |
| An update is waiting | Settings > Updates lists what it is waiting for. |

For IT support, `runtime\python.exe -m workspace --self-test` in the installed version folder checks that the main dependencies load and that the network block works. `--status` prints counts only, never content.

## Measured performance

On an otherwise idle Intel Core Ultra 7 255H laptop (32 GB), with 5 minutes of synthetic speech:

| Engine | Processor, Quiet (4 threads) | NVIDIA RTX 500 Ada laptop GPU |
| --- | --- | --- |
| Parakeet v2 | 12.2 s | 32.4 s (slower) |
| Whisper large-v3 turbo | 199 s | 47.6 s |

Word error rate on 148 public clips (21.5 minutes): Parakeet v2 15.9%, Whisper turbo 14.8%, Parakeet v3 18.9%. That is a small public sample, not New Zealand business audio. Parakeet v2 on the processor is the default. The graphics card is used for speaker identification only: on a 15-minute test recording it took 80 s instead of 435 s. Details are in issue #12 and [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md).

On eight whole public meetings (AMI, 254 minutes), Parakeet v2 had a word error rate of 24.0% at 20 times real time; Qwen3-ASR-1.7B, a candidate engine not yet in the app, had 19.4% at 3 times real time but needed about 11 GB of memory. The NPU on the test laptop could not run any engine reliably. Method and full results: [docs/ENGINE_EVALUATION.md](docs/ENGINE_EVALUATION.md).

## Known limitations

- Not yet tested on a clean standard-user Windows profile, through Intune on a managed device, or on Windows 10 (#10, #11).
- The installer scripts are not code-signed. Integrity rests on the published `.sha256` and the installer's per-file check.
- Transcription quality has been measured on public and synthetic audio only, not on representative business recordings (#12, #25).
- Not yet tested with a screen reader, Windows contrast themes or pilot users (#27).
- Tested on one laptop only. The minimum and recommended hardware levels are untested (#28).
- Graphics card acceleration has been tested on one NVIDIA laptop GPU with synthetic audio. AMD and Intel graphics are not supported.

What a release must meet is in [docs/RELEASE_STANDARD.md](docs/RELEASE_STANDARD.md). The evidence recorded so far is in [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md).

## The earlier app

The original source-installed app, Whisper Transcriber (a file queue with a tkinter window, installed with `install.cmd` at the top of this repository) still works. It is documented in [docs/EARLIER_APP.md](docs/EARLIER_APP.md). The two keep separate outputs and share downloaded models, and the workspace never deletes anything the earlier app created.

## For developers

Both Codex and Claude Code use the shared [agent guidelines](AGENTS.md).

| Path | Purpose |
| --- | --- |
| `workspace/` | Unminute: SQLite state, durable chunked processing, managed workers, cleanup and holds, intake, recording, the pywebview shell (`workspace/ui/`, with Archivo bundled under its OFL licence) |
| `asr_engines.py` | Shared engine interface for Parakeet and Whisper; model manifests, verified install and repair |
| `offline_guard.py` | Forces library telemetry off and refuses non-local network access outside deliberate downloads |
| `packaging/` | Per-user installer and uninstaller for the package |
| `tools/build_package.py` | Builds the versioned zip with a private CPython 3.12 runtime |
| `tools/` | Real-engine checks, UI and accessibility checks, the design comparison (`tools/design_check.py`), benchmarks |
| `tests/` | Unit tests: `python -m unittest discover -s tests -v` |
| `transcribe_ui.py`, `transcriber_core.py`, `install.ps1` | The earlier app |
| `CHANGELOG.md` | Release notes, one section per version. The Release workflow reads it. |

Design and plans:

- [docs/WORKSPACE_PLAN.md](docs/WORKSPACE_PLAN.md): the target product.
- [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md): coding and integration sequence.
- [docs/WORKSPACE_DESIGN.md](docs/WORKSPACE_DESIGN.md): the durable processing core.
- [docs/design/boards/](docs/design/boards/): the approved UI design boards that `tools/design_check.py` compares the app against.
- [docs/WEBVIEW_TRIAL.md](docs/WEBVIEW_TRIAL.md): the desktop shell trial and its security findings.
- [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md): threat model, trust boundaries, controls and residual risks.
- [docs/RELEASE_STANDARD.md](docs/RELEASE_STANDARD.md), [docs/RELEASING.md](docs/RELEASING.md) and [docs/VALIDATION_RUNBOOK.md](docs/VALIDATION_RUNBOOK.md): release criteria, release process and hands-on checks.

Dependencies are pinned on purpose in `requirements.txt` and `workspace/requirements.txt`. If you upgrade torch, torchaudio, pyannote.audio or huggingface_hub, re-test speaker identification end to end with a non-sensitive recording: `python test_diarization.py "path\to\recording.wav"` checks Whisper with pyannote speaker identification, and `tools/durable_core_check.py` checks the workspace's processing core. CI runs the jobs in `.github/workflows/ci.yml` and `codeql.yml` on pull requests and pushes to main; [docs/RELEASE_STANDARD.md](docs/RELEASE_STANDARD.md) lists every gate and where it runs. CI does not run the models.

## Licence

This project is released under the [MIT Licence](LICENSE).

The repository contains this project's own code, plus one third-party wheel (`proxy_tools`, MIT) in `packaging/wheels/` because its publisher provides no wheel. No model weights are redistributed. The release package includes the official embeddable CPython 3.12 runtime (Python Software Foundation Licence) and the pinned packages from PyPI, each under its own licence. Models are downloaded in Setup from Hugging Face. The main components are listed below, verified on 17 September 2026 unless noted.

| Component | Licence | Notes |
| --- | --- | --- |
| OpenAI Whisper (model architecture and original weights) | MIT | |
| dropbox-dash/faster-whisper-large-v3-turbo (formerly mobiuslabsgmbh) | MIT | CTranslate2 conversion of openai/whisper-large-v3-turbo |
| Systran/faster-whisper-large-v3 | MIT | CTranslate2 conversion of openai/whisper-large-v3 |
| faster-whisper, CTranslate2 | MIT | |
| pyannote.audio (library) | MIT | Copyright CNRS |
| pyannote/speaker-diarization-community-1 (model) | CC BY 4.0 | Gated: you accept sharing your contact details with pyannote to download it. Attribution required, commercial use permitted. |
| PyTorch | BSD-3-Clause | |
| sv-ttk, tkinterdnd2 | MIT | Earlier app only |
| pywebview | BSD-3-Clause | The workspace window. Verified 15 October 2026. |
| pythonnet, clr-loader, proxy_tools | MIT | Used by pywebview on Windows. Verified 15 October 2026. |
| nvidia/parakeet-tdt-0.6b-v2 and -v3 (original models) | CC BY 4.0 | Attribution required, commercial use permitted. Verified 13 October 2026. |
| istupakov/parakeet-tdt-0.6b-v2-onnx and -v3-onnx | CC BY 4.0 | ONNX exports of the NVIDIA models, used as INT8. Verified 13 October 2026. |
| istupakov/silero-vad-onnx | MIT | Silero voice-activity detection. Verified 13 October 2026. |
| onnx-asr | MIT | Verified 13 October 2026. |
| ONNX Runtime | MIT | |
| PyTorch CUDA 13.0 build (optional download) | BSD-3-Clause; bundled NVIDIA CUDA runtime and cuDNN under NVIDIA's licences | Downloaded by the user in Setup from download-r2.pytorch.org, for speaker identification on an NVIDIA graphics card. Not redistributed here. Verified 14 October 2026. |
| sounddevice | MIT | Optional microphone recording in the meeting workspace. Verified 14 October 2026. |
| PortAudio | MIT | Bundled with sounddevice. The workspace package keeps only the 64-bit Windows library without ASIO. Verified 14 October 2026. |

Attribution for the speaker identification model: the pyannote speaker-diarization-community-1 pipeline by pyannoteAI, released under CC BY 4.0. If you publish transcripts or derived work that relied on speaker identification, keep that attribution.

Attribution for the Parakeet models: parakeet-tdt-0.6b-v2 and parakeet-tdt-0.6b-v3 by NVIDIA, released under CC BY 4.0, used here as ONNX exports by istupakov. If you publish transcripts or derived work produced with Parakeet, keep that attribution.

## Acknowledgements

Thanks to the faster-whisper, CTranslate2, onnx-asr, ONNX Runtime, pyannote, pywebview, sv-ttk and tkinterdnd2 maintainers, to NVIDIA for releasing the Parakeet weights under CC BY 4.0, and to OpenAI for releasing the Whisper weights under MIT.
