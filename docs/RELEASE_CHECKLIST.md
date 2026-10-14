# Release checklist: meeting workspace

This lists what a workspace release needs, what has been verified, and what still needs people or hardware.

- **Publishing** is a separate step. It needs explicit authorisation and follows [RELEASING.md](RELEASING.md).
- **The earlier source app** keeps its own release process (README, `install.cmd`).

## Every change (automated, in CI)

- [x] **Syntax check and unit tests** on Linux, about 270 tests. Some Windows-only tests are skipped there.
- [x] **Ruff, CodeQL, requirements resolution** (wheels only, Windows, Python 3.12) and PSScriptAnalyzer on all installer scripts.

## Verified on the development machine

Machine: Windows 11, 16 logical CPUs. For much of the testing another heavy transcription app was running, so the timings in PR #47 and #56 are pessimistic.

| Area | Evidence |
| --- | --- |
| Offline processing and no telemetry | Telemetry switches are forced. Under the network guard, a real transcription made 0 connection attempts. PR #38, #8. |
| Model integrity | Every model is pinned to a revision and SHA-256 manifest. Damaged copies are set aside, and the Whisper large-v3 turbo 80/128 mel-band failure was reproduced and repaired. PR #39, #45, `docs/MODEL_MANIFEST.md`. |
| Durable processing | Chunked versus whole-recording output: 0 word differences over 2,300 words. Crash and resume gave identical results. PR #46. |
| Workers and Quiet defaults | Below-normal priority, idle unload, Stop and pause behaviour. PR #47. Idle-machine timings (Intel Core Ultra 7 255H, 5 minutes of synthetic speech, 14 October 2026): Faster mode was 3 to 4 times slower than Quiet for Parakeet because ONNX Runtime threads spun. After the fix (PR #66), both modes take 19 s, with 0 word differences. Faster mode speeds up speaker identification: 58 s against 85 s on 2 minutes. |
| Speakers, cleanup, holds, receipts | PRs #48 to #50. Cleanup leaves no transcript text readable in the database files (a marker-word test). |
| Workspace window, review, naming, handoff | Automated checks in a real WebView2 window. PRs #51 to #53. |
| Setup and token | A real gated download, with the token passed by pipe only. Windows Credential Manager round trip. PR #54. |
| Watched folders | PR #55. A real OneDrive online-only file was detected and not downloaded. With the cloud provider unavailable, it was kept for retry. With OneDrive running and fetching turned on, it was downloaded and queued for import (#24, closed). |
| Package | Build, tamper refusal, install, reinstall, self-test and uninstall on the 0.2.0 build. Offline Parakeet and pyannote inference and the app window were run on an earlier build of the same package. PR #56. |
| Updates and migration gate | The real installer, end to end with two versions: a held workspace waits (exit 5) with the reason, an emptied workspace migrates and keeps its receipts, rollback after migration is refused (exit 6), a compatible reinstall works, and uninstall. PR #58. |
| Handoff clipboard | A real copy round-tripped exactly and carried the formats that keep it out of clipboard history and cloud clipboard sync (#22, closed). |
| Packaged crash tests | `tools/packaged_crash_check.py` (PR #65) on the installed 0.2.0-preview.1 package passed all 12 criteria. The app was killed after chunk commits, during speaker identification, after the review and final confirmations, and with an active hold. Each time it restarted with an identical transcript, no duplicates, correct cleanup and no residue. A hold survived a crash, and releasing it removed nothing without confirmation. A test token was removed, and the clipboard was unchanged. |
| Import dialog | `tools/import_dialog_check.ps1` (PR #65): the native Import dialog of the installed package imported a recording. |
| Accessibility (technical) | `tools/accessibility_check.py` (PR #68) found no problems in 8 states of the real window: names, keyboard reach, focus rings, WCAG AA contrast, colour-only statuses and reflow at 640 and 320 CSS px. Keyboard and Ctrl+wheel zoom work (50% to 300%). Not covered: screen-reader use, high-contrast themes and pilot users (#27). |
| Microphone on real hardware | A Logitech C920 USB microphone was disconnected mid-recording on the installed package, by unplugging the dock it was connected to. Recording stopped at that moment, kept the 6.7 s already captured and reported the lost device. The overrun Windows reports at the moment of disconnection is now part of that message (PR #83). Bluetooth headsets are untested (#30, closed). |
| Real mouse and keyboard | A recording dragged onto the window of the installed package was imported, and Ctrl+plus zoomed with the physical keyboard. Ctrl+0 did not reset the zoom at the first attempt and worked afterwards, probably because the window did not have focus (recorded on #27). |
| Packaged lifecycle and resources | `tools/packaged_check.py` (PR #57) collects these from an installed package. Development laptop run of the installed 0.2.0-preview.1 package (16 logical CPUs, 32 GB, CPU only, 14 October 2026). Self-test PASS. 15 minutes of synthetic audio transcribed with speakers in 673 s, with a worker peak of 2,418 MB and 0 network attempts. After review and final-saved confirmation, no meeting or marker text remained and there were no backup files. The idle window used 1.0% of one core and 480 MB. Reference measurements still belong on the lowest-supported laptop (#28). |

## Before a business release (needs people or hardware)

Step-by-step instructions for each item: [VALIDATION_RUNBOOK.md](VALIDATION_RUNBOOK.md).

- [ ] **#11:** a clean standard-user Windows profile, and an Intune deployment and detection on a managed device. Choose the signing route.
- [ ] **#12 and #25:** transcription quality on consented, representative business recordings (anonymous aggregate results only).
- [ ] **#27:** a screen-reader pass (NVDA or Narrator), high-contrast themes, and an end-to-end task with three nontechnical users.
- [ ] **#28:** run `tools/packaged_check.py` and `tools/packaged_crash_check.py` on the lowest-supported laptop (idle and peak resources, lifecycle residue, crash recovery).
- [x] **#30:** unplug a USB microphone during a recording (runbook section 6). Done with a USB microphone; see the table above.
- [x] **#20:** importing by dragging a recording onto the window. Done; see the table above.

## Supported configuration (this release)

| | Status |
| --- | --- |
| Windows 11 x64, CPU | Tested on the development machine |
| Windows 10 x64 (1809 or later) | Supported by the installer checks, not yet tested |
| Edge WebView2 Runtime | Required. The installer checks for it |
| Parakeet v2 (recommended) | Tested in the workspace (real-engine checks), and offline Parakeet inference from an earlier package build |
| Parakeet v3, Whisper large-v3 turbo | Tested in the earlier app's engine only (Parakeet v3 dropped speech on some clips) |
| pyannote community-1 speaker identification | Tested from source and from the package |
| GPU acceleration | Optional, for speaker identification only (Setup component, Faster mode). Tested on an NVIDIA RTX 500 Ada laptop GPU (4 GB, driver 595.95). Transcription stays on the CPU. See below. |

## GPU for speaker identification

The optional component is the PyTorch 2.13.0 and torchaudio 2.11.0 CUDA 13.0 wheels (`workspace/gpu.py`). They are pinned by SHA-256 and every file is checked against the wheels' RECORD. They are loaded only by a separate speaker-identification worker. Measured through the app's worker on 14 October 2026 (RTX 500 Ada, 4 GB):

| Synthetic audio | CPU (14 threads) | GPU | Speed-up | Peak GPU memory (incl. desktop) | Output |
| --- | --- | --- | --- | --- | --- |
| 2 minutes | 53.7 s | 17.3 s | 3.1x | 2.1 GB | identical turns |
| 15 minutes | 435 s | 80.5 s | 5.4x | 2.1 GB | identical turns (150) |

Other results:

- **Install:** the real install through Setup's download process took 132 s, and the full file check passed.
- **Fallback:** forcing a failure ran the pass on the CPU and labelled it.
- **Not yet tested:** the synthetic audio was identified as one speaker, so multi-speaker agreement still needs a real recording (#12, #25).

## GPU trial (engines)

`tools/gpu_trial.py` runs the same audio on the CPU and on the GPU, verifies which ONNX Runtime provider was really used, and compares the outputs. Results for 5 minutes of synthetic speech, CPU against an NVIDIA RTX 500 Ada laptop GPU (4 GB), measured on 14 October 2026:

| Engine | CPU | GPU | Speed-up | Peak GPU memory | CPU vs GPU output |
| --- | --- | --- | --- | --- | --- |
| Whisper large-v3 turbo (CTranslate2, float16 on GPU) | 428 s | 89 s | 4.8x | 2.4 GB | 1 word different of 749 |
| pyannote community-1 (torch CUDA 13) | 297 s | 25 s | 11.8x | 2.1 GB | identical (50 turns) |
| Parakeet v2 (ONNX Runtime CUDA 13) | 250 s | 396 s | 0.6x (slower) | 0.3 GB | identical (750 words) |

Findings:

- **Speaker identification gains most** from a GPU, then Whisper.
- **Parakeet** really ran on CUDA, but it was slower than the CPU in this configuration, where it is already fast. GPU support would not help the recommended model as it stands.
- **The GPU builds need different CUDA versions.** CTranslate2 4.8 (Whisper) needs CUDA 12 libraries, while ONNX Runtime 1.30 and torch 2.13 here use CUDA 13. Their cuDNN libraries share one folder and clash in a single environment. ONNX Runtime has to share torch's cuDNN; installing a separate cuDNN causes a version mismatch.
- **A GPU-enabled package** therefore needs separate worker environments, or matched CUDA versions. That is future work. Detection alone never enables a GPU.

## Publishing (when authorised)

1. Merge a `## vX.Y.Z` section into `CHANGELOG.md`.
2. Build the package on a clean Windows build machine: `python tools/build_package.py --version X.Y.Z`. Record its SHA-256.
3. Install it once with `-Package` and run `--self-test`.
4. Follow [RELEASING.md](RELEASING.md) to tag and publish. Attach the zip and the `.sha256` file to the release.
