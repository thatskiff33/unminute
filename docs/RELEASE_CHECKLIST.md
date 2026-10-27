# Release checklist: Unminute

This lists what a workspace release needs, what has been verified, and what still needs people or hardware.

- **Publishing** is a separate step. It needs explicit authorisation and follows [RELEASING.md](RELEASING.md).
- **Phase C (#111) ships as one release.** C7 set `PHASE_C_COMPLETE` in `workspace/keystore.py`, so a release can be prepared again; that was not release approval, and the first Phase C release, v0.5.0-preview.4, needed the owner's explicit decision, which was given and used for it ("Before cutting a release", step 4, in [RELEASING.md](RELEASING.md); #121). Its evidence must meet "Phase C release evidence" below.

## Every change (automated, in CI)

- [x] **Every job in `.github/workflows/ci.yml` and `codeql.yml`.** These include unit tests on Linux and Windows; the Windows job fails if a test is skipped for an unlisted reason. The required jobs are listed in [RELEASING.md](RELEASING.md).

[RELEASE_STANDARD.md](RELEASE_STANDARD.md), Part B, shows which gates CI runs and which are still manual.

## Verified on the development machine

Most of this evidence was gathered on the 0.2.0 previews (14 October 2026). It has not been re-run in full on later releases. Since #124 (performance) and #111 slice B3 (release evidence), each release carries its own evidence against its own package: the five evidence reports attached to every release from v0.5.0-preview.2 ([RELEASING.md](RELEASING.md)). This table has not been refreshed from them. Performance evidence is measured on whichever Windows machine prepares the release, with that machine's specification recorded in `evidence-perf.json` and the release notes; there is no named reference machine, and 8 cores and 16 GB is a design aim, not a gate (#121).

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

- [ ] **Packaged lifecycle** (`evidence-packaged.json`): the scanner's control found every pattern; nothing readable at rest; the audit log verified; the recording encrypted while it records (at least one finished chunk scanned) and its key erased after import and discard; the audio key erased with the text key kept; nothing blocking an update and the database compacted; only the workspace's own files left, with the logs listed; nothing left in TEMP, APPDATA or the user folder; nothing unexplained new in LocalLow or the model cache; no crash dumps. The window's WebView2 profile is not covered yet (#418, before the full release).
- [ ] **Crash checks** (`evidence-crash.json`): the import killed part-way was discarded with nothing of it left (no `audio.partial`, only the workspace's own files); each cleanup landing was consistent and then finished; the recording killed part-way was kept after the restart, with no partial chunk or plain audio left; nothing plain in the landing profile; nothing left in any profile's TEMP, APPDATA or user folder; nothing unexplained new in LocalLow or the model cache; no crash dumps. Where each cleanup kill landed is recorded in `landings`.
- [ ] **Explained files** (both reports): every `residue.EXPLAINED` entry a report relied on is listed (`left_explained`, `outside_explained`, the crash report's `outside.explained`); any entry added for this release had an independent review and is named in the release notes (the owner's decision DEC-057, 26 Oct 2026, #335; docs/RELEASING.md).
- [ ] **Performance** (`evidence-perf.json`): the store step's figures and `workspace_s` are present and within their budgets. Those budgets were set from the first Phase C release run, v0.5.0-preview.4's medians by `perf_check.propose()` (plus 20%, rounded up to 0.01, at least 0.05 above the median), never from a development-laptop run (#430). The drafts-on states (`recording-drafts`, `recording-drafts-minimised`; #104) are measured with public speech (`--speech`, `--draft-models`) and within the owner's budgets of 40% and 10% of one core. The first release run to measure them (the next one after #104 slice 4) states their figures in the release notes. The import step's `import_two_track_1h_s` (#376) is present; its budget is not set yet, so the release notes state the figure and the proposed budget for the owner to set (DEC-077).
- [ ] **Comparison:** run the performance check with `--compare` against a local copy of the previous release's `evidence-perf.json`, and say in the release notes whether the machines were comparable.

## Before a business release (needs people or hardware)

Step-by-step instructions for each item: [VALIDATION_RUNBOOK.md](VALIDATION_RUNBOOK.md). Each open item is tracked in validation tracker #189, by sitting; the closed issues it came from keep their earlier evidence.

- [ ] **#189 sitting 3 (from #11, closed):** a clean standard-user Windows profile, and an Intune deployment and detection on a managed device. Signing is not part of it: the owner dropped the signing requirement on 22 October 2026 (#111), so the installer scripts stay unsigned and run with `-ExecutionPolicy Bypass`, an accepted risk under [SECURITY_MODEL.md](SECURITY_MODEL.md) T22, which security plan B6 updates to say so.
- [ ] **#189 sitting 4 (from #12 and #25, closed):** transcription quality on consented, representative business recordings (anonymous aggregate results only).
- [ ] **#189 sitting 5 (from #27, closed):** a screen-reader pass (NVDA or Narrator), high-contrast themes, and an end-to-end task with three nontechnical users.
- [ ] **#189 sitting 2 (from #28, closed):** run `tools/packaged_check.py` and `tools/packaged_crash_check.py` on the lowest-supported laptop (idle and peak resources, lifecycle residue, crash recovery).
- [ ] **Copy and open (from #109, closed; logged as a sitting item in #189):** on the installed package, with synthetic content: one tab opens on the assistant's page with Edge as the default browser (then Chrome if installed), both with the browser already open and closed (a browser the app starts inherits its environment); paste works; Win+V history does not show the copy. Policy by registry: another name and address, an invalid address, only one of the two set, and `AllowHandoffAssistant` = 0 (button gone; Settings says why). Narrator and keyboard on the button and its messages.
- [ ] **First-start check (from #111 B5b; a sitting item in #189, with sitting 2's lowest-supported laptop):** on the lowest-supported laptop, in a clean per-user profile with Microsoft Defender's on-access scan on, install the package and at once time its first start, from the shortcut to the window, which includes the check; `--health` must then report `self_check` `ok`. The check must stay well under its 30 s limit: one run past it is run once more, and a second counts as a fault that opens the previous version or nothing. Repeat after an update. Development-laptop figures from copied files (8 to 14 s first, 3 to 6 s after) are in `docs/WORKSPACE_DESIGN.md`, "Launch", step 6.
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

Why only cuBLAS: CTranslate2 4.8.0's Windows wheels are built with CUDA 12.8.1, `-DWITH_CUDNN=OFF` and `-DCUDA_DYNAMIC_LOADING=ON` (`python/tools/prepare_build_environment_windows.sh` at its v4.8.0 tag). Its `ctranslate2.dll` imports no CUDA library: the only CUDA names in it are `cublas64_12.dll` and the driver's `nvcuda.dll`, loaded at run time, and it names no cuDNN library (checked on 20 October 2026 by reading its import table and strings). The wheel's own `cudnn64_9.dll` is loaded by its `__init__.py` but not used by `ctranslate2.dll`, so the package leaves it out (#176 D2; see "cuDNN not needed" below). cuBLAS 12.8.4.1 is the cuBLAS of CUDA 12.8 Update 1, and `cublas64_12.dll` imports `cublasLt64_12.dll`, which is in the same wheel. `tests/test_consistency.py` fails when the CTranslate2 pin moves, so this is checked again then. Checked again for CTranslate2 4.8.3 (the #180 re-lock, 22 October 2026): its build script differs from v4.8.0's only in a CMake policy flag and a cleanup step, its `ctranslate2.dll` still names only `cublas64_12.dll` and `nvcuda.dll`, and its `cudnn64_9.dll` and `libiomp5md.dll` are byte-identical to 4.8.0's. A 5-minute run through the app's workers was repeated with 4.8.3 on 24 October 2026 ("cuDNN not needed" below); the other figures below are from 4.8.0.

Measured on 20 October 2026 on the development laptop: NVIDIA RTX 500 Ada Laptop GPU, 4 GB, driver 595.95. The desktop runs on the integrated graphics, so the NVIDIA card started each run with 0 MiB in use; a card that also drives the desktop has less memory free. CTranslate2 4.8.0, CPython 3.14.8, the component installed into a scratch model cache. Audio: the public AMI ES2004a headset mix (17.5 minutes, CC BY 4.0) and its first 5 minutes. Before every GPU run a gate checked that no installed copy of Unminute was running and the card had no other compute process.

Standalone runs use the app's Whisper settings (`asr_engines.WhisperEngine`: English, voice detection, beam 5, word times), one call for the whole audio, outside the app's 45-second parts. The processor runs at int8 with 14 threads (Faster mode: all 16 logical processors but 2). GPU memory is from Windows' "GPU Process Memory" counters, sampled every second: dedicated is the card's own memory, and shared is what spilled into system memory (76 MiB was in shared use from the start of every run). Other sessions' test suites were running at times, so processor times are upper bounds; the times below are from runs with little else running, except where noted.

| Model, device, precision | 5 minutes | 17.5 minutes | Peak dedicated GPU memory | Spilled to system memory | Word error rate against AMI (17.5 min) |
| --- | --- | --- | --- | --- | --- |
| Turbo, processor, int8 | 94.1 s | 365.8 s | | | 23.02% |
| Turbo, GPU, float16 | 7.9 s (11.9x) | 47.4 s (7.7x) | 2,385 MiB | none | 20.75% |
| Large-v3, processor, int8 | 176.3 s | 1,708.6 s and 1,843.2 s in two runs, both with other work running | | | 22.54% to 25.52% in three runs |
| Large-v3, GPU, int8_float16 | 24.9 to 38.5 s in three runs (4.6x to 7.1x) | 173.0 s (9.9x to 10.7x) | 3,793 MiB | none | 21.27% and 21.35% in two runs |
| Large-v3, GPU, float16 | 174.7 s | 680.9 s (first pass; repeated on 25 October, below) | 3,860 MiB (5 min) | 1,612 MiB (5 min) | 22.42% (scored on 25 October 2026) |

Agreement, GPU against the processor (word differences on normalised text; start times of the words that align):

- **Turbo, float16, full file:** 170 differences in 2,099 words (8.1%); 96.7% of aligned words start within 0.25 s of the processor's. On the 5-minute excerpt the processor left out a passage the GPU kept, in both of its runs (word error rate against AMI 41.2% and 39.6% on the processor, 20.7% on the GPU).
- **Large-v3, int8_float16:** on the 5-minute excerpt, 3 to 10 differences in 324 words (0.9% to 3.1%) across three GPU runs, with 91.1% to 99.4% of starts within 0.25 s. On the full file, 210 differences in 2,173 words (9.7%), 93.5% of starts within 0.25 s, and a lower word error rate against AMI than the processor (21.3% against 22.5% to 25.5%).
- **Run to run:** in one call for the whole file, large-v3 is not exactly repeatable on either device. Identical full-file runs differed by 37 words in 2,215 on the GPU (two runs, word error rate 21.35% and 21.27%) and by 251 to 277 words on the processor (three runs, 23.02%, 22.54% and 25.52%). Turbo on the full file gave identical words in two runs on each device. So part of the difference between the devices in one call is this variation. Its cause is faster-whisper's temperature fallback, and through the app's 45-second parts large-v3 was identical from run to run ("Run-to-run variation" below, #374).
- **Word times** are close enough for speaker assignment: aligned words start within 0.25 s of AMI's own times as often on the GPU as on the processor (turbo 87.9% against 88.1%; large-v3 79.4% to 79.6% against 76.8% to 79.5%).

A long file, the public AMI ES2004c headset mix (38.9 minutes, CC BY 4.0), in one call for the whole file on the GPU: turbo at float16 took 115.6 s with 2,385 MiB dedicated and nothing spilled (word error rate against AMI 18.04%); large-v3 at int8_float16 took 485.4 s with 3,825 MiB dedicated and nothing spilled (21.76%). On the processor, turbo took 1,242.9 s (the machine was busy; 10.8 times the GPU's time) with a word error rate of 17.79%; the GPU's turbo text differed in 366 of 6,119 words (6.0%), and 95.8% of aligned words started within 0.25 s of the processor's. Large-v3 on the processor took 4,866.0 s (the machine was busy; 10.0 times the GPU's time) with a word error rate of 19.75%: lower than the GPU's 21.76% in one call and 20.02% through the app (below). The GPU's large-v3 text differed in 732 of 6,220 words (11.8%), and 94.1% of aligned words started within 0.25 s of the processor's. A 2-point difference is within the run-to-run variation large-v3 showed on the processor for ES2004a in one call (22.5% to 25.5%).

Through the app (a headless `Workspace` from this branch, with its own download process, workers and 45-second parts; Faster mode, "Use the graphics card" on, speaker identification on, the speaker GPU component not installed, so speakers ran on the processor):

- **Install:** the app's download process fetched the component from files.pythonhosted.org and installed it in 16.0 s; Check every file passed in 1.0 s.
- **Turbo:** all 24 parts ran on the GPU at float16 (the meeting's details: "Graphics card, float16"), with no part failing. The transcript was finished 54.1 s after the import started, including starting the worker and loading the model. Peak 2,385 MiB dedicated, nothing spilled. Speaker identification then found 5 speakers and labelled all 246 segments; the card held 0 MiB during it, so the Whisper GPU worker had ended first. 762.8 s in all. Word error rate against AMI 22.74%.
- **Large-v3:** all 24 parts on the GPU at int8_float16, no part failing, the transcript finished 142.2 s after the import started. Peak 2,385 MiB dedicated, nothing spilled: the app's bounded parts use much less than one call for the whole file. 5 speakers, all 204 segments labelled, 848.3 s in all. Word error rate against AMI 22.78%.
- **Long file, large-v3:** ES2004c (38.9 minutes) through the app: all 51 parts on the GPU at int8_float16, no part failing, the transcript finished 301.3 s after the import started. Peak 2,705 MiB dedicated, nothing spilled. 4 speakers, all 565 segments labelled. Word error rate against AMI 20.02%. The worker's CUDA libraries, read while it ran, were the component's two cuBLAS DLLs and CTranslate2's `cudnn64_9.dll` (before #176 D2 left it out).
- **Libraries:** in the app's own Whisper GPU worker (`WorkerHandle.whisper_gpu_worker`, a separate process), after loading each model and transcribing 30 s, the CUDA libraries loaded were `cublas64_12.dll` and `cublasLt64_12.dll` from the component's folder, and CTranslate2's own `cudnn64_9.dll` (before #176 D2 left it out). No PyTorch CUDA library and no CUDA 13 library was loaded; only the package's processor build of PyTorch (`torch_cpu.dll`), as in the processor's worker.
- **cuDNN not needed (#176 D2, 24 October 2026):** CTranslate2 4.8.3, in a scratch virtual environment with every package in `requirements.txt`, the component in a scratch model cache, the same laptop and card (driver 595.95), behind the same gate. Through the app's own workers, the first 5 minutes of ES2004a in the app's 45-second parts: turbo on the GPU at float16, large-v3 on the GPU at int8_float16, and turbo on the processor at int8. Each ran once with CTranslate2's `cudnn64_9.dll` in its folder and once with it removed. Without it every run finished on the same device and precision, the text was identical (same SHA-256 for each model and device, compared without reading it), and the only CUDA libraries the GPU worker loaded were the component's `cublas64_12.dll` and `cublasLt64_12.dll`. With it, the DLL was loaded in both workers but changed nothing. `ctranslate2.dll`, `_ext.pyd` and `libiomp5md.dll` in the 4.8.3 wheel contain no reference to cuDNN. So the package leaves the DLL out (`PRUNED_PATHS` in `tools/build_package.py`); a CTranslate2 upgrade must check this again (`tests/test_consistency.py` says so).

Precision: float16 against int8_float16 (large-v3, 25 October 2026). Does the shipped int8_float16 cost accuracy against full float16? Same laptop, card and driver (595.95), the card at 0 MiB before each run, behind the same gate, by the method above: the app's Whisper settings, one call for the whole ES2004a mix, scored against AMI with `tools/compare_engines.py`'s normalisation (the scoring reproduces the 21.27% and 21.35% of 20 October from those runs' results). A scratch virtual environment with `requirements.txt` (CTranslate2 4.8.3, faster-whisper 1.2.1, CPython 3.14.8), and the model and component in a scratch model cache, checked again in full against their pins. Three things differ from 20 October: CTranslate2 4.8.3 (was 4.8.0); the component loaded as the app's Whisper GPU worker now loads it (`HIP_PATH` removed, then `gpu.activate_whisper`); and the audio read straight from the WAV file, because faster-whisper 1.2.1's `decode_audio` passes an argument that PyAV 19.0.2 (the lock's) rejects. The file is 16 kHz mono 16-bit, and the samples were checked identical to that function's decoding. The runs alternated between the precisions. Other sessions' work was running at times (processor load 8% to 60% at the start of a run), so times are upper bounds.

The rule, set before the runs: float16 counts as clearly better only if both of its runs beat both int8_float16 runs by more than 0.5 points of word error rate.

| Run (order) | 17.5 minutes | Peak dedicated GPU memory | Peak shared (spilled) | Word error rate against AMI | Substitutions | Deletions | Insertions | Word starts within 0.25 s of AMI's |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| int8_float16 (1) | 230.7 s | 3,825 MiB | 76 MiB (none) | 27.86% | 179 | 475 | 48 | 76.1% |
| float16 (2) | 835.7 s | 3,860 MiB | 1,708 MiB | 26.94% | 108 | 527 | 44 | 76.4% |
| int8_float16 (3) | 173.2 s | 3,825 MiB | 76 MiB (none) | 21.23% | 108 | 379 | 48 | 79.5% |
| float16 (4) | 803.0 s | 3,860 MiB | 1,676 MiB | 29.05% | 120 | 555 | 57 | 73.9% |

The AMI reference has 2,520 words after normalisation. Shared memory is that of the run's own process; 76 MiB was in shared use from the start of every run, as on 20 October.

- **These runs cannot settle the question.** In one call for the whole file, large-v3 varies from run to run with faster-whisper's temperature fallback ("Run-to-run variation" below, found afterwards in #374): int8_float16 alone gave 21.23% and 27.86% in two identical runs, and float16 varied as widely (26.94%, 29.05%, and 22.42% for the 20 October run scored now). So the comparison is confounded by the sampling, and neither precision is shown to be more accurate by these four runs. The right test is through the app's parts, which are repeatable (below).
- **The spread is mostly deletions.** The worse runs left out more short passages: the AMI words that fall in stretches of over 5 s with no output were 91 and 143 at int8_float16 and 167 and 213 at float16 (92 in a 20 October int8_float16 run). Neither precision left out a stretch of over 20 s that the others kept, apart from one float16 run that missed a further 6 AMI words. The cause is the fallback sampling above.
- **Speed and memory as on 20 October:** float16 again filled the card and spilled 1.6 to 1.7 GB into system memory, taking 3.5 to 4.8 times as long as int8_float16, which stayed within the card.

Through the app's parts (26 October 2026). One run of each precision, int8_float16 first, through the app's 45-second parts as defined under "Run-to-run variation" below, with the app code of `main` at cff2bbc2. Same laptop, card, driver, scratch environment (CTranslate2 4.8.3), model and component cache, behind the same gate and under the shared heavy-work lock; processor load 9% and 8% at the start of the runs. Scored as above.

| Precision | 17.5 minutes | Peak dedicated GPU memory | Peak shared (spilled) | Word error rate against AMI | Substitutions | Deletions | Insertions | Word starts within 0.25 s of AMI's | Windows that fell back |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| int8_float16 | 96.3 s | 2,385 MiB | 76 MiB (none) | 22.78% | 90 | 464 | 20 | 75.9% | 0 of 42 |
| float16 | 294.2 s | 3,857 MiB | 428 MiB | 23.41% | 93 | 477 | 20 | 75.3% | 0 of 41 |

- **float16 is not more accurate.** It was 0.63 points worse than int8_float16, so under the rule above (applied to one run of each, because this path repeats) it is not clearly better. Its word starts agreed with AMI's as often (75.3% against 75.9%).
- **Repeatability.** The int8_float16 run's words and word times were identical (same SHA-256) to #374's three runs through the parts, which match 20 October's app run. No float16 window fell back, and decoding at temperature 0 is repeatable, so a repeat should give the same result; float16 through the parts was run once only, so this is expected, not measured.
- **Speed and memory:** through the parts float16 still filled the card and spilled about 350 MiB (428 MiB shared, less the 76 MiB in use from the start), and took 3.1 times as long as int8_float16, which stayed within the card at 2.4 GB.
- **So int8_float16 stays** (`gpu.WHISPER_PRECISION`). Only this 4 GB card and this one public meeting were measured through the parts: on a card with enough memory for float16 not to spill, and on other recordings, float16's speed and accuracy are untested (#189).

Run-to-run variation (large-v3, #374, 25 October 2026). Why identical one-call runs above gave 21.23% to 27.86%. Same laptop, card, driver and gate, under the shared heavy-work lock; public ES2004a and ES2004c only, scored as above; each run also recorded, for every 30-second decoding window, the temperature finally used, average log probability, compression ratio and no-speech probability (numbers only). Scratch virtual environments with `requirements.txt` (CTranslate2 4.8.3) and with CTranslate2 4.8.0 as the only difference. "Through the app's parts" means `chunking.plan_chunks`, `WhisperEngine` for each part and `chunking.owned`, as `workspace/runner.py` does, not a full headless `Workspace`: its words and word times were identical (same SHA-256) to the 20 October headless-`Workspace` runs above, for large-v3 on ES2004a and ES2004c and for turbo on ES2004a.

| Setup (ES2004a unless stated; GPU unless stated) | CTranslate2 | Runs | Word error rate | Same words every run? | Windows that fell back |
| --- | --- | --- | --- | --- | --- |
| One call, default | 4.8.3 | 3 | 25.00%, 21.55%, 22.58% | No: first differ at 355.8 to 356.1 s | 6, 1, 2 of 27 to 28 |
| One call, default | 4.8.0 | 3 | 21.51%, 22.50%, 21.79% | No | 1, 1, 3 of 27 to 28 |
| One call, fixed seed (`ctranslate2.set_random_seed`) | 4.8.3 | 2 | 25.28% both | Yes | 5, 5 |
| One call, temperature 0 only (no fallback) | 4.8.3 | 2 | 22.42% both | Yes | none; 6 windows kept repetitive text |
| One call, voice detection off | 4.8.3 | 2 | 21.67%, 21.11% | No: first differ at a fallback window near 146 s | 5, 2 of 36 |
| App's parts, default | 4.8.3 | 3 | 22.78% each | Yes, and identical to 20 October's app run (4.8.0) | 0 of 42 |
| App's parts, voice detection off | 4.8.3 | 1 | 20.56% | One run | 1 of 49 |
| App's parts, ES2004c | 4.8.3 | 1 | 20.02% | Identical to 20 October's app run (4.8.0) | 0 of 99 |
| Turbo, float16, one call | 4.8.3 | 2 | 20.75% both | Yes | 0 of 38 |
| Turbo, float16, app's parts | 4.8.3 | 2 | 22.74% both | Yes, and identical to 20 October's app run | 1 of 44 (no words from it either time) |
| Processor, int8, one call, first 420 s | 4.8.3 | 2 | 23.88%, 24.35% | No: first differ at 148.8 s | 3, 2 of 8 |
| Processor, int8, app's parts, first 420 s | 4.8.3 | 1 | 22.34% | One run | 0 of 14 |

- **Cause: faster-whisper's temperature fallback.** A 30-second window that fails faster-whisper's checks at temperature 0 is retried by random sampling at temperatures 0.2 to 1.0 (faster-whisper 1.2.1's defaults, which `WhisperEngine` keeps). CTranslate2 seeds its sampler from `std::random_device` unless `set_random_seed` is called (`src/random.cc` at v4.8.3), so each process samples differently. The sampled text sets where the next window starts and what it is prompted with, so every later window in the call can differ, and different short passages are left out further on. In one call for ES2004a, the window starting at 354.7 s failed at temperature 0 in every GPU run (compression ratio 3.79, a repetition loop).
- **Decoding at temperature 0 is repeatable:** a fixed seed or no fallback each gave identical words and word times in two runs.
- **Not voice detection:** it kept the same 796.5 s in every run, and with it off the runs still diverged at a fallback window.
- **Not new with CTranslate2 4.8.3:** 4.8.0 diverges at the same point (356.1 s in the 20 October runs). Five runs under each cannot show whether the version changes how often a run goes badly.
- **Through the app's parts, large-v3 was repeatable:** with the app's settings, no large-v3 window fell back on the GPU (0 of 141 over the two meetings) or on the processor (0 of 14). Each part restarts the prompt, which may make repetition loops less likely (not tested).
- **What remains:** a part that does fall back is still random (one window in 49 with voice detection off). Turning fallback off (temperature 0 only) would make every run repeatable but would keep a looping part's text instead of retrying it; through the parts it changed nothing on ES2004a (22.78%, identical).
- **Decided: the fallback stays as it is** (engineering decision, 26 October 2026, recorded on PR #373). The app's path through 45-second parts was repeatable in every measured run (two meetings on one card), so turning the fallback off would only trade retries for keeping looping text, with no measured benefit. Reopen this if a real recording shows a run-to-run difference through the parts.
- **How to measure Whisper from now on:** through the app's parts, or with the windows that fell back recorded. Never compare single one-call runs. The one-call figures in this section are single draws.

Findings:

- **Turbo** runs on the GPU at float16 in 2.4 GB, 8 to 12 times faster than on the processor here, and as accurate: its word error rate against AMI was lower than the processor's on ES2004a and within 0.3 points on ES2004c.
- **Large-v3 agrees** with the processor within its own run-to-run variation: its word error rate on the GPU was lower on ES2004a and up to 2 points higher on ES2004c (0.3 points through the app).
- **Large-v3 runs on the GPU at int8_float16 only.** At float16 it filled the 4 GB card and Windows spilled 1.6 GB into system memory, which made it 4.5 times slower than at int8_float16. At int8_float16 it stayed within the card: 3.8 GB at peak in one call for the whole file, 2.4 GB in the app's parts. Through the app's parts float16 was no more accurate (23.41% against 22.78%, one run each, 26 October, above). So it ships at that precision. On a 4 GB card that also drives the desktop it may still spill (slower, not wrong) or run out of memory, and then falls back to the processor. Other cards are to be validated in #189.
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

- [ ] **Straight after publishing:** in a docs pull request, set the "newest release" lines (README.md, docs/WORKSPACE_GUIDE.md, docs/IT_DEPLOYMENT.md, docs/PRIVACY_IMPACT_ASSESSMENT_DRAFT.md) to the published version, add it to docs/RELEASE_STANDARD.md's "built, attested and published" lines, remove each "Not in vX" marker and each "once X ships" phrase for what shipped and move the other markers to the new version, and fold into the release's CHANGELOG.md section any `changelog.d/` fragment that shipped in the tagged commit but is not in it, worded as the published notes are (#421). `tests/test_consistency.py` fails any marker that names a version other than the one the "newest release" lines name, and fails those lines once they are more than one changelog heading behind (#411). If this docs pull request is forgotten, nothing fails until the next release-prep pull request adds a newer changelog heading. That pull request then fails, and the docs are fixed in it.
