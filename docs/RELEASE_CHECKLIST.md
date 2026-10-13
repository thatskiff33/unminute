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
| Workers and Quiet defaults | Below-normal priority, idle unload, Stop and pause behaviour. PR #47. Idle-machine timings are not yet measured. |
| Speakers, cleanup, holds, receipts | PRs #48 to #50. Cleanup leaves no transcript text readable in the database files (a marker-word test). |
| Workspace window, review, naming, handoff | Automated checks in a real WebView2 window. PRs #51 to #53. |
| Setup and token | A real gated download, with the token passed by pipe only. Windows Credential Manager round trip. PR #54. |
| Watched folders | PR #55. A real OneDrive online-only file was detected and not downloaded; with the cloud provider unavailable it was kept for retry. The fetch-and-import step is still to be completed (#24). |
| Package | Build, tamper refusal, install, reinstall, self-test and uninstall on the 0.2.0 build. Offline Parakeet and pyannote inference and the app window were run on an earlier build of the same package. PR #56. |
| Updates and migration gate | The real installer, end to end with two versions: a held workspace waits (exit 5) with the reason, an emptied workspace migrates and keeps its receipts, rollback after migration is refused (exit 6), a compatible reinstall works, and uninstall. PR #58. |
| Handoff clipboard | A real copy round-tripped exactly and carried the formats that keep it out of clipboard history and cloud clipboard sync (#22, closed). |
| Packaged lifecycle and resources | `tools/packaged_check.py` (PR #57) collects these from an installed package. A full run on the development machine was stopped part-way because memory ran low while another app was using it; the reference measurements belong on the lowest-supported laptop (#28). |

## Before a business release (needs people or hardware)

- [ ] **#11:** a clean standard-user Windows profile, and an Intune deployment and detection on a managed device. Choose the signing route.
- [ ] **#12 and #25:** transcription quality on consented, representative business recordings (anonymous aggregate results only).
- [ ] **#24:** the fetch-and-import step for a OneDrive online-only file with OneDrive syncing normally, and how the deploying organisation's tenant stores Teams recordings.
- [ ] **#27:** accessibility and an end-to-end task with nontechnical users.
- [ ] **#28:** run `tools/packaged_check.py` on the lowest-supported laptop (idle and peak resources, lifecycle residue), crash tests around deletions on the packaged build, and an unchanged clipboard.
- [ ] **#20:** importing by drag and drop, and through the native Import dialog.
- [ ] **#15:** Quiet versus Faster timings on an idle machine.

## Supported configuration (this release)

| | Status |
| --- | --- |
| Windows 11 x64, CPU | Tested on the development machine |
| Windows 10 x64 (1809 or later) | Supported by the installer checks, not yet tested |
| Edge WebView2 Runtime | Required. The installer checks for it |
| Parakeet v2 (recommended) | Tested in the workspace (real-engine checks), and offline Parakeet inference from an earlier package build |
| Parakeet v3, Whisper large-v3 turbo | Tested in the earlier app's engine only (Parakeet v3 dropped speech on some clips) |
| pyannote community-1 speaker identification | Tested from source and from the package |
| GPU acceleration | Not included. A detected graphics card does not enable it. See the GPU trial below (#9, #12) |

## GPU trial (not in this release)

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
