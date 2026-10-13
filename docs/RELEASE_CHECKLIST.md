# Release checklist: meeting workspace

This lists what a workspace release needs, what has been verified, and what still needs people or hardware.

- **Publishing** is a separate step. It needs explicit authorisation and follows [RELEASING.md](RELEASING.md).
- **The earlier source app** keeps its own release process (README, `install.cmd`).

## Every change (automated, in CI)

- [x] **Syntax check and unit tests** on Linux, about 270 tests. Some Windows-only tests are skipped there.
- [x] **Ruff, CodeQL, requirements resolution** (wheels only, Windows, Python 3.12) and PSScriptAnalyzer on all installer scripts.

## Verified on the development machine

Machine: Windows 11, 16 logical CPUs. Another heavy transcription app was running throughout, so timings are pessimistic.

| Area | Evidence |
| --- | --- |
| Offline processing and no telemetry | Telemetry switches are forced. Under the network guard, a real transcription made 0 connection attempts. PR #38, #8. |
| Model integrity | Every model is pinned to a revision and SHA-256 manifest. Damaged copies are set aside, and the Whisper large-v3 turbo 80/128 mel-band failure was reproduced and repaired. PR #39, #45, `docs/MODEL_MANIFEST.md`. |
| Durable processing | Chunked versus whole-recording output: 0 word differences over 2,300 words. Crash and resume gave identical results. PR #46. |
| Workers and Quiet defaults | Below-normal priority, idle unload, Stop and pause behaviour. PR #47. Idle-machine timings are not yet measured. |
| Speakers, cleanup, holds, receipts | PRs #48 to #50. Cleanup leaves no transcript text readable in the database files (a marker-word test). |
| Workspace window, review, naming, handoff | Automated checks in a real WebView2 window. PRs #51 to #53. |
| Setup and token | A real gated download, with the token passed by pipe only. Windows Credential Manager round trip. PR #54. |
| Watched folders | PR #55. Real OneDrive placeholders are not yet tested (#24). |
| Package | Build, tamper refusal, install, reinstall, self-test, offline inference, the window and uninstall. PR #56. |
| Updates and migration gate | The real installer, end to end with two versions: a held workspace waits (exit 5) with the reason, an emptied workspace migrates and keeps its receipts, rollback after migration is refused (exit 6), a compatible reinstall works, and uninstall. PR #58. |
| Packaged lifecycle and resources | `tools/packaged_check.py` (PR #57) collects these from an installed package. A full run on the development machine was stopped part-way because memory ran low while another app was using it; the reference measurements belong on the lowest-supported laptop (#28). |

## Before a business release (needs people or hardware)

- [ ] **#11:** a clean standard-user Windows profile, and an Intune deployment and detection on a managed device. Choose the signing route.
- [ ] **#12 and #25:** transcription quality on consented, representative business recordings (anonymous aggregate results only).
- [ ] **#22:** a real clipboard copy and paste. Check that copies are excluded from Win+V history, and save once through the native dialog.
- [ ] **#24:** a real OneDrive online-only file, and how this tenant stores Teams recordings.
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
| Parakeet v2 (recommended), Parakeet v3, Whisper large-v3 turbo | Tested from source; Parakeet v2 tested from the package |
| pyannote community-1 speaker identification | Tested from source and from the package |
| GPU acceleration | Not included. A detected graphics card does not enable it (#9, #12) |

## Publishing (when authorised)

1. Merge a `## vX.Y.Z` section into `CHANGELOG.md`.
2. Build the package on a clean Windows build machine: `python tools/build_package.py --version X.Y.Z`. Record its SHA-256.
3. Install it once with `-Package` and run `--self-test`.
4. Follow [RELEASING.md](RELEASING.md) to tag and publish. Attach the zip and the `.sha256` file to the release.
