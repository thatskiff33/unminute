# Release checklist: Unminute

This lists what a workspace release needs, what has been verified, and what still needs people or hardware.

- **Publishing** is a separate step. It needs explicit authorisation and follows [RELEASING.md](RELEASING.md).
- **Phase C (#111) is not releasable until C7** sets `PHASE_C_COMPLETE` in `workspace/keystore.py` (schema 2 merged in C2b-2; "Before cutting a release", step 4, in [RELEASING.md](RELEASING.md)).

## Every change (automated, in CI)

- [x] **Every job in `.github/workflows/ci.yml` and `codeql.yml`.** These include unit tests on Linux and Windows; the Windows job fails if a test is skipped for an unlisted reason. The required jobs are listed in [RELEASING.md](RELEASING.md).

[RELEASE_STANDARD.md](RELEASE_STANDARD.md), Part B, shows which gates CI runs and which are still manual.

## Verified on the development machine

Most of this evidence was gathered on the 0.2.0 previews (14 October 2026). It has not been re-run in full on later releases. #124 (performance) and #111 slice B3 (release evidence) will make each release produce its own evidence against its own package. Performance evidence is measured on whichever Windows machine prepares the release, with that machine's specification recorded in `evidence-perf.json` and the release notes; there is no named reference machine, and 8 cores and 16 GB is a design aim, not a gate (#121).

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
| Accessibility (technical) | `tools/accessibility_check.py` (PR #68) found no problems in 8 states of the real window: names, keyboard reach, focus rings, WCAG AA contrast, colour-only statuses and reflow at 640 and 320 CSS px. Keyboard and Ctrl+wheel zoom work (50% to 300%). Not covered: screen-reader use, high-contrast themes and pilot users (now tracked in #189). |
| Microphone on real hardware | A Logitech C920 USB microphone was disconnected mid-recording on the installed package, by unplugging the dock it was connected to. Recording stopped at that moment, kept the 6.7 s already captured and reported the lost device. The overrun Windows reports at the moment of disconnection is now part of that message (PR #83). Bluetooth headsets are untested (#30, closed). |
| Real mouse and keyboard | A recording dragged onto the window of the installed package was imported, and Ctrl+plus zoomed with the physical keyboard. Ctrl+0 did not reset the zoom at the first attempt and worked afterwards, probably because the window did not have focus (recorded on #27). |
| Packaged lifecycle and resources | `tools/packaged_check.py` (PR #57) collects these from an installed package. Development laptop run of the installed 0.2.0-preview.1 package (16 logical CPUs, 32 GB, CPU only, 14 October 2026). Self-test PASS. 15 minutes of synthetic audio transcribed with speakers in 673 s, with a worker peak of 2,418 MB and 0 network attempts. After review and final-saved confirmation, no meeting or marker text remained and there were no backup files. The idle window used 1.0% of one core and 480 MB. Reference measurements still belong on the lowest-supported laptop (#28). |
| Idle performance (provisional) | `tools/perf_check.py --quick` (#124) on source at 0fb525f with 3 synthetic meetings, 16 October 2026. This was the development laptop on the Performance power plan, with other apps running.<br>- Start-up to interactive: 1.7 s.<br>- Meetings view: 1.2% of one core, 754 MB private bytes.<br>- Settings view: 3.2% of one core, 773 MB.<br>- Minimised: 0.4% of one core, 763 MB.<br>- Process tree: 8 processes, 6 of them WebView2.<br>- The worker unloaded 121 s after its last use.<br>These figures set the provisional budgets in `docs/perf-budgets.json`. Private bytes are not comparable with the 480 MB above, which summed working sets. |

## Phase C release evidence (#111 C7)

For the first release with schema 2 and every release after it. Each item is part of a report's own pass criteria; this list says what to look at before approving.

- [ ] **Packaged lifecycle** (`evidence-packaged.json`): the scanner's control found every pattern; nothing readable at rest; the audit log verified; the recording encrypted while it records and its key erased after import and discard; the audio key erased with the text key kept; nothing blocking an update and the database compacted; only the workspace's own files left, with the logs listed; no crash dumps.
- [ ] **Crash checks** (`evidence-crash.json`): the import killed part-way was discarded with nothing of it left; each cleanup landing was consistent and then finished; the recording killed part-way was kept after the restart, with no partial chunk or plain audio left; no crash dumps. Where each cleanup kill landed is recorded in `landings`.
- [ ] **Performance** (`evidence-perf.json`): the store step's figures and `workspace_s` are present. On the first Phase C release, open a pull request after publishing that sets their budgets from these medians plus 20% (never from a development-laptop run).
- [ ] **Comparison:** run the performance check with `--compare` against a local copy of the previous release's `evidence-perf.json`, and say in the release notes whether the machines were comparable.

## Before a business release (needs people or hardware)

Step-by-step instructions for each item: [VALIDATION_RUNBOOK.md](VALIDATION_RUNBOOK.md).

- [ ] **#11:** a clean standard-user Windows profile, and an Intune deployment and detection on a managed device. Choose the signing route.
- [ ] **#12 and #25:** transcription quality on consented, representative business recordings (anonymous aggregate results only).
- [ ] **Validation tracker #189 (carried from #27, closed):** a screen-reader pass (NVDA or Narrator), high-contrast themes, and an end-to-end task with three nontechnical users.
- [ ] **#28:** run `tools/packaged_check.py` and `tools/packaged_crash_check.py` on the lowest-supported laptop (idle and peak resources, lifecycle residue, crash recovery).
- [ ] **#109:** Copy and open on the installed package, with synthetic content (sitting logged in #189): one tab opens on the assistant's page with Edge as the default browser (then Chrome if installed), both with the browser already open and closed (a browser the app starts inherits its environment); paste works; Win+V history does not show the copy. Policy by registry: another name and address, an invalid address, only one of the two set, and `AllowHandoffAssistant` = 0 (button gone; Settings says why). Narrator and keyboard on the button and its messages.
- [x] **#30:** unplug a USB microphone during a recording (runbook section 6). Done with a USB microphone; see the table above.
- [x] **#20:** importing by dragging a recording onto the window. Done; see the table above.

## Dated reminders (every release)

- **The Python runtime (#178, closed with this reminder).** The package ships CPython 3.14 (`PYTHON_VERSION` in `tools/build_package.py`). [PEP 745](https://peps.python.org/pep-0745/) plans 3.14's last Windows binary release, 3.14.14, for 5 October 2027. After that only source releases follow, so the shipped runtime would stop getting security fixes. Every release:
  - confirms that date in PEP 745, and records any change on #178;
  - from 1 April 2027 (or sooner, if PEP 745 brings the date forward), while the runtime is still 3.14, reopens #178 so the move starts. The move is to a Python that still gets Windows binary releases, by the same steps as B4 (#166): wheel spike, re-lock, A/B on public audio, packaged checks. It must land well before October 2027.

  Until then, the monthly re-lock adopts each 3.14.x patch release ([RELEASING.md](RELEASING.md), "Workflow supply-chain rules").

## Supported configuration (this release)

| | Status |
| --- | --- |
| Windows 11 x64, CPU | Tested on the development machine |
| Windows 10 x64 (1809 or later) | Supported by the installer checks, not yet tested |
| Edge WebView2 Runtime | Required. The installer checks for it |
| Parakeet v2 (recommended) | Tested in the workspace (real-engine checks), and offline Parakeet inference from an earlier package build |
| Parakeet v3 | Tested only in the retired source app's engine (it dropped speech on some clips) |
| Whisper large-v3 turbo, Whisper large-v3 | Tested in the workspace on 20 October 2026 (#248), on the processor and on the RTX 500 Ada GPU, with public AMI meetings: see "Whisper on the graphics card". Large-v3 is offered in Setup and Manage downloads since #246 |
| pyannote community-1 speaker identification | Tested from source and from the package |
| GPU acceleration | Optional, two components (Manage downloads, Faster mode): speaker identification, and Whisper transcription (#248). Tested on an NVIDIA RTX 500 Ada laptop GPU (4 GB, driver 595.95). Parakeet stays on the CPU. Other NVIDIA cards are untested (#189). See below. |

## GPU for speaker identification

The optional component is the PyTorch 2.14.1 and torchaudio 2.11.0 CUDA 13.0 wheels for CPython 3.14 (`workspace/gpu.py`). They are pinned by SHA-256 and every file is checked against the wheels' RECORD. They are loaded only by a separate speaker-identification worker.

Measured through the app's worker on 17 October 2026 (#111 B4; RTX 500 Ada, 4 GB, driver 595.95; Faster mode), on a public 4-person meeting (AMI ES2004a headset mix, 17.5 minutes, CC BY 4.0):

| Runtime | Transcription (Parakeet v2) | Speaker identification | Output |
| --- | --- | --- | --- |
| CPython 3.12.10, torch 2.13.0, CPU (before B4) | 49 s | 499 s | 2,117 words; 298 turns, 5 speakers |
| CPython 3.14.8, torch 2.14.1, CPU | 47 s | 503 s | the same words (0 edits); the same turns (every 10 ms frame agrees) |
| CPython 3.14.8, torch 2.14.1 CUDA, GPU | not run (stays on the CPU) | 100 s (5.0x) | the same turns as the CPU (every 10 ms frame agrees) |

The component's install through the app's own `gpu.install` and download code took 63 s, and the full file check passed. Peak GPU memory was not measured in this run.

Rechecked on 19 October 2026 after the speaker model moved to weights-only loading from the hash-checked bytes (#111 B9, #218), through the app's workers (`worker.diarize`, CPU worker against GPU worker) on the same laptop, with the component installed by `gpu.install` into a scratch cache (165 s; full file check passed). On the full ES2004a mix and on the first 120 s of ES2004a and 300 s of IS1009a (public AMI, CC BY 4.0), the GPU gave the same turns and exclusive turns as the CPU, and every 10 ms frame agrees (298 turns and 5 speakers on the full mix, as above). Run times are not performance evidence: the machine was shared with other sessions' test runs.

Earlier, with PyTorch 2.13.0 on CPython 3.12 (14 October 2026, synthetic audio):

| Synthetic audio | CPU (14 threads) | GPU | Speed-up | Peak GPU memory (incl. desktop) | Output |
| --- | --- | --- | --- | --- | --- |
| 2 minutes | 53.7 s | 17.3 s | 3.1x | 2.1 GB | identical turns |
| 15 minutes | 435 s | 80.5 s | 5.4x | 2.1 GB | identical turns (150) |

Other results:

- **Install:** the real install through Setup's download process took 132 s, and the full file check passed.
- **Fallback:** forcing a failure ran the pass on the CPU and labelled it.
- **Multi-speaker agreement:** the synthetic audio was identified as one speaker; the public AMI meeting above (5 speakers found) agrees exactly between CPU and GPU. A real New Zealand business recording is still to test (#12, #25).

## Whisper on the graphics card

The optional "Graphics card for Whisper" component (#248) is NVIDIA's cuBLAS for CUDA 12, `nvidia-cublas-cu12` 12.8.4.1, from files.pythonhosted.org (`workspace/gpu.py`). It is pinned by SHA-256 and every file is checked against the wheel's RECORD, and only the Whisper GPU worker loads it.

Why only cuBLAS: CTranslate2 4.8.0's Windows wheels are built with CUDA 12.8.1, `-DWITH_CUDNN=OFF` and `-DCUDA_DYNAMIC_LOADING=ON` (`python/tools/prepare_build_environment_windows.sh` at its v4.8.0 tag). Its `ctranslate2.dll` imports no CUDA library: the only CUDA names in it are `cublas64_12.dll` and the driver's `nvcuda.dll`, loaded at run time, and it names no cuDNN library (checked on 20 October 2026 by reading its import table and strings). The wheel's own `cudnn64_9.dll` is loaded by its `__init__.py` but not used by `ctranslate2.dll`. cuBLAS 12.8.4.1 is the cuBLAS of CUDA 12.8 Update 1, and `cublas64_12.dll` imports `cublasLt64_12.dll`, which is in the same wheel. `tests/test_consistency.py` fails when the CTranslate2 pin moves, so this is checked again then.

Measured on 20 October 2026 on the development laptop: NVIDIA RTX 500 Ada Laptop GPU, 4 GB, driver 595.95. The desktop runs on the integrated graphics, so the NVIDIA card started each run with 0 MiB in use; a card that also drives the desktop has less memory free. CTranslate2 4.8.0, CPython 3.14.8, the component installed into a scratch model cache. Audio: the public AMI ES2004a headset mix (17.5 minutes, CC BY 4.0) and its first 5 minutes. Before every GPU run a gate checked that no installed copy of Unminute was running and the card had no other compute process.

Standalone runs use the app's Whisper settings (`asr_engines.WhisperEngine`: English, voice detection, beam 5, word times), one call for the whole audio, outside the app's 45-second parts. The processor runs at int8 with 14 threads (Faster mode: all 16 logical processors but 2). GPU memory is from Windows' "GPU Process Memory" counters, sampled every second: dedicated is the card's own memory, and shared is what spilled into system memory (76 MiB was in shared use from the start of every run). Other sessions' test suites were running at times, so processor times are upper bounds; the times below are from runs with little else running, except where noted.

| Model, device, precision | 5 minutes | 17.5 minutes | Peak dedicated GPU memory | Spilled to system memory | Word error rate against AMI (17.5 min) |
| --- | --- | --- | --- | --- | --- |
| Turbo, processor, int8 | 94.1 s | 365.8 s | | | 23.02% |
| Turbo, GPU, float16 | 7.9 s (11.9x) | 47.4 s (7.7x) | 2,385 MiB | none | 20.75% |
| Large-v3, processor, int8 | 176.3 s | 1,708.6 s and 1,843.2 s in two runs, both with other work running | | | 22.54% to 25.52% in three runs |
| Large-v3, GPU, int8_float16 | 24.9 to 38.5 s in three runs (4.6x to 7.1x) | 173.0 s (9.9x to 10.7x) | 3,793 MiB | none | 21.27% and 21.35% in two runs |
| Large-v3, GPU, float16 | 174.7 s | 680.9 s (first pass, not repeated) | 3,860 MiB (5 min) | 1,612 MiB (5 min) | not scored |

Agreement, GPU against the processor (word differences on normalised text; start times of the words that align):

- **Turbo, float16, full file:** 170 differences in 2,099 words (8.1%); 96.7% of aligned words start within 0.25 s of the processor's. On the 5-minute excerpt the processor left out a passage the GPU kept, in both of its runs (word error rate against AMI 41.2% and 39.6% on the processor, 20.7% on the GPU).
- **Large-v3, int8_float16:** on the 5-minute excerpt, 3 to 10 differences in 324 words (0.9% to 3.1%) across three GPU runs, with 91.1% to 99.4% of starts within 0.25 s. On the full file, 210 differences in 2,173 words (9.7%), 93.5% of starts within 0.25 s, and a lower word error rate against AMI than the processor (21.3% against 22.5% to 25.5%).
- **Run to run:** large-v3 is not exactly repeatable on either device. Identical full-file runs differed by 37 words in 2,215 on the GPU (two runs, word error rate 21.35% and 21.27%) and by 251 to 277 words on the processor (three runs, 23.02%, 22.54% and 25.52%). Turbo on the full file gave identical words in two runs on each device. So part of the difference between the devices is this ordinary variation.
- **Word times** are close enough for speaker assignment: aligned words start within 0.25 s of AMI's own times as often on the GPU as on the processor (turbo 87.9% against 88.1%; large-v3 79.4% to 79.6% against 76.8% to 79.5%).

A long file, the public AMI ES2004c headset mix (38.9 minutes, CC BY 4.0), in one call for the whole file on the GPU: turbo at float16 took 115.6 s with 2,385 MiB dedicated and nothing spilled (word error rate against AMI 18.04%); large-v3 at int8_float16 took 485.4 s with 3,825 MiB dedicated and nothing spilled (21.76%). On the processor, turbo took 1,242.9 s (the machine was busy; 10.8 times the GPU's time) with a word error rate of 17.79%; the GPU's turbo text differed in 366 of 6,119 words (6.0%), and 95.8% of aligned words started within 0.25 s of the processor's. Large-v3 on the processor took 4,866.0 s (the machine was busy; 10.0 times the GPU's time) with a word error rate of 19.75%: lower than the GPU's 21.76% in one call and 20.02% through the app (below). The GPU's large-v3 text differed in 732 of 6,220 words (11.8%), and 94.1% of aligned words started within 0.25 s of the processor's. A 2-point difference is within the run-to-run variation large-v3 showed on the processor for ES2004a (22.5% to 25.5%).

Through the app (a headless `Workspace` from this branch, with its own download process, workers and 45-second parts; Faster mode, "Use the graphics card" on, speaker identification on, the speaker GPU component not installed, so speakers ran on the processor):

- **Install:** the app's download process fetched the component from files.pythonhosted.org and installed it in 16.0 s; Check every file passed in 1.0 s.
- **Turbo:** all 24 parts ran on the GPU at float16 (the meeting's details: "Graphics card, float16"), with no part failing. The transcript was finished 54.1 s after the import started, including starting the worker and loading the model. Peak 2,385 MiB dedicated, nothing spilled. Speaker identification then found 5 speakers and labelled all 246 segments; the card held 0 MiB during it, so the Whisper GPU worker had ended first. 762.8 s in all. Word error rate against AMI 22.74%.
- **Large-v3:** all 24 parts on the GPU at int8_float16, no part failing, the transcript finished 142.2 s after the import started. Peak 2,385 MiB dedicated, nothing spilled: the app's bounded parts use much less than one call for the whole file. 5 speakers, all 204 segments labelled, 848.3 s in all. Word error rate against AMI 22.78%.
- **Long file, large-v3:** ES2004c (38.9 minutes) through the app: all 51 parts on the GPU at int8_float16, no part failing, the transcript finished 301.3 s after the import started. Peak 2,705 MiB dedicated, nothing spilled. 4 speakers, all 565 segments labelled. Word error rate against AMI 20.02%. The worker's CUDA libraries, read while it ran, were the component's two cuBLAS DLLs and CTranslate2's `cudnn64_9.dll`.
- **Libraries:** in the app's own Whisper GPU worker (`WorkerHandle.whisper_gpu_worker`, a separate process), after loading each model and transcribing 30 s, the CUDA libraries loaded were `cublas64_12.dll` and `cublasLt64_12.dll` from the component's folder, and CTranslate2's own `cudnn64_9.dll`. No PyTorch CUDA library and no CUDA 13 library was loaded; only the package's processor build of PyTorch (`torch_cpu.dll`), as in the processor's worker.

Findings:

- **Turbo** runs on the GPU at float16 in 2.4 GB, 8 to 12 times faster than on the processor here, and as accurate: its word error rate against AMI was lower than the processor's on ES2004a and within 0.3 points on ES2004c.
- **Large-v3 agrees** with the processor within its own run-to-run variation: its word error rate on the GPU was lower on ES2004a and up to 2 points higher on ES2004c (0.3 points through the app).
- **Large-v3 runs on the GPU at int8_float16 only.** At float16 it filled the 4 GB card and Windows spilled 1.6 GB into system memory, which made it 4.5 times slower than at int8_float16. At int8_float16 it stayed within the card: 3.8 GB at peak in one call for the whole file, 2.4 GB in the app's parts. So it ships at that precision. On a 4 GB card that also drives the desktop it may still spill (slower, not wrong) or run out of memory, and then falls back to the processor. Other cards are to be validated in #189.
- **Not tested on real hardware:** a real out-of-memory error or driver failure on the card. The fallback is covered by tests with fake engines (`tests/test_gpu.py`), not by a forced failure on this laptop. Windows' shared-memory spill makes a real out-of-memory error less likely than slowness.

## GPU trial (engines)

`tools/gpu_trial.py` runs the same audio on the CPU and on the GPU, verifies which ONNX Runtime provider was really used, and compares the outputs. It loads the speaker model as the app does (the hash-checked bytes through `workspace/speaker_load.py`, weights only), so its speaker figures cover the app's own path. Results for 5 minutes of synthetic speech, CPU against an NVIDIA RTX 500 Ada laptop GPU (4 GB), measured on 14 October 2026:

| Engine | CPU | GPU | Speed-up | Peak GPU memory | CPU vs GPU output |
| --- | --- | --- | --- | --- | --- |
| Whisper large-v3 turbo (CTranslate2, float16 on GPU) | 428 s | 89 s | 4.8x | 2.4 GB | 1 word different of 749 |
| pyannote community-1 (torch CUDA 13) | 297 s | 25 s | 11.8x | 2.1 GB | identical (50 turns) |
| Parakeet v2 (ONNX Runtime CUDA 13) | 250 s | 396 s | 0.6x (slower) | 0.3 GB | identical (750 words) |

Findings:

- **Speaker identification gains most** from a GPU, then Whisper.
- **Parakeet** really ran on CUDA, but it was slower than the CPU in this configuration, where it is already fast. GPU support would not help the recommended model as it stands.
- **The GPU builds need different CUDA versions.** CTranslate2 4.8 (Whisper) needs CUDA 12 libraries, while ONNX Runtime 1.30 and torch 2.13 here use CUDA 13. Their cuDNN libraries share one folder and clash in a single environment. ONNX Runtime has to share torch's cuDNN; installing a separate cuDNN causes a version mismatch.
- **A GPU-enabled package** therefore needs separate worker environments, or matched CUDA versions. Detection alone never enables a GPU. Since #248 each GPU use has its own component and worker: speaker identification (PyTorch CUDA 13, above) and Whisper (NVIDIA's cuBLAS for CUDA 12, see "Whisper on the graphics card"). Parakeet stays on the CPU.

## Publishing (when authorised)

Follow "Before cutting a release" and "Cutting a release" in [RELEASING.md](RELEASING.md). That is the only copy of the steps. If a release has to be withdrawn, the runbook is in the same file.
