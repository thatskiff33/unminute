# Release standard

What a release must meet before it reaches staff, and how each requirement is checked.

- **Part A** is the standard itself. It is written so any in-house app can adopt it.
- **Part B** is Unminute's profile: each gate, where it runs and its status.

The work that builds the missing gates is tracked in #121. Security and privacy controls are in [SECURITY_MODEL.md](SECURITY_MODEL.md) and #111, and are not repeated here.

## How to use this standard

- **For a new app:** adopt Part A as written. Write the app's own Part B: one row per gate, saying where it runs (a unit test, a CI job, the build, or a release-time run) and its status.
- **When a criterion cannot be met:** record the exception in the app's Part B, with the reason and the issue that will close it. An exception without an issue is a failed gate.
- **When changing a budget:** lowering one needs no reason. Raising one needs a stated reason in the pull request.

## Part A. The standard

Each section gives the principle, the criterion and how it is checked.

### 1. Single source of truth

**Principle.** Every fact has one owner. Every other copy is either generated from it or checked against it by a test.

**Criterion.** No unchecked copies of these facts:
- the version;
- install identity (name, folders, registry keys, IDs, exit codes);
- dependency pins;
- limits and thresholds;
- names shared between the UI and the backend;
- budgets.

**Check.** Consistency tests that fail when two copies disagree. Docs link to the source rather than restating a number that changes, such as test counts, package counts or required-check lists.

### 2. Performance budgets

**Principle.** The app has a stated cost at rest and at work, and a release that exceeds it fails.

**Criterion.** A budget for each state:
- each main screen visible;
- minimised;
- recording or other foreground activity;
- background work running.

Each budget covers CPU, memory, start-up time and the peak cost of heavy work. Budgets live in one file. They apply to whichever machine measures a release, and that machine's specification is recorded with the figures and stated in the release notes. The recommended hardware is a design aim, not a condition of measuring. A run on the minimum hardware is recorded for information.

**Check.** Measure the same way every time:
- **CPU:** process CPU time (kernel plus user) for the whole process tree, over 300 s after a 60 s settle. Take the median of three runs and express it as a percentage of one logical core.
- **Memory:** private bytes summed across the process tree, with private working set reported alongside. Never sum working sets, because shared pages are counted once per process.
- **Conditions:** mains power and the Balanced power plan, with antivirus idle (for every v0.5.0 preview the owner chose (DEC-062) the Performance plan, the one the firm's staff laptops use; 19 Oct 2026, #111). Record the machine's specification (processor, cores and logical processors, memory, operating system build, power plan, mains or battery, graphics adapters and drivers, the display's refresh rate), the app version and the versions of embedded runtimes such as the browser engine. Record nothing that identifies the machine or its user.
- **Visible means drawn:** a visible state is measured only while the display is on and the window is being drawn, since drawing is much of its cost. The check keeps the display on for its own run without changing the power plan, records that it did, and counts a window measured in a visible state only if the display stayed on, the session was unlocked and the page drew. A window that was not drawn stops the run rather than passing (#292).
- **Setting a budget:** start at the measured baseline plus 20%. After each improvement, lower it to the new figure plus 15%.

### 3. Just-in-time loading

**Principle.** Code, data, models, processes, timers and screen updates exist only while they are needed and visible.

**Criterion.**
- Heavy libraries (machine learning, media, GPU) load only in the process that uses them, and only when it needs them.
- Heavy worker processes start on demand, and are gone within their stated idle time plus 5 s.
- While the window is hidden or minimised, nothing polls faster than once every 30 s, except active recording or processing.
- The UI redraws only what changed.
- Start-up does only what the first screen needs; other work is deferred.

**Check.**
- A test that importing the UI process's modules does not import the heavy libraries.
- Deterministic counts in CI: bridge calls per minute, live timers and redraws while idle, both visible and hidden.
- The release-time budgets in section 2.

### 4. Reliability and durability

**Principle.** A crash, power loss or full disk never loses committed work or leaves a half-written file in use.

**Criterion.**
- Every write is atomic (write to a temporary file, flush, then rename) or transactional.
- Committed work survives a hard kill at any point.
- An unclean shutdown is detected, and triggers an integrity check at the next start.
- A damaged store fails with a plain message and a documented recovery path, never silently.

**Check.** Unit tests for each write path. A packaged crash test that kills the app at each stage and compares the results. A test with a deliberately corrupted store.

### 5. Diagnostics without telemetry

**Principle.** When something goes wrong on a user's machine, there is a local record that helps fix it. That record never contains the user's content, and nothing is sent anywhere unless the user chooses to share it.

**Criterion.**
- A local log, capped by size and number of files.
- Records hold only event codes, stack frames (file, line, function), counts, durations and versions. They hold no exception messages, content, names or paths that could identify content.
- Crashes in every process (UI, workers, page script) are recorded.
- A "Copy diagnostics" action, started by the user, gathers what support needs.

**Check.** A marker test runs the full lifecycle with marker words in every content field and path. It must find zero markers in the logs and the diagnostics output. Forced crashes in each process must each leave a record.

### 6. Error handling

**Principle.** Every failure is caught at a boundary, recorded and explained.

**Criterion.**
- Each process, thread, bridge call and installer step catches unexpected errors.
- Each failure records an incident code, and shows the user plain language that includes that code.
- No error output is discarded (for example, a child process's error stream sent to a null device).
- Errors never pass stack traces or internal detail to the user interface.

**Check.** Unit tests for the boundary handlers. The diagnostics marker test in section 5. Review of every new boundary.

### 7. Testing

**Principle.** Each kind of risk has a test layer, with a stated trigger and pass rule.

| Layer | Trigger | Pass rule |
| --- | --- | --- |
| Unit tests | Every pull request, on each supported OS | All pass; no unexplained skips |
| Integration with fake engines | Every pull request | All pass |
| Headless UI harness | Every pull request | Idle and behaviour counts within ratchets |
| Real-window UI and accessibility checks | Every release | No failures |
| Packaged end-to-end and crash checks | Every release, against the release package | All criteria pass |
| Performance | Every release | Within budget (section 2) |
| Accuracy on fixed public data | When engines, models or their dependencies change | No worse than the stated tolerance |

Coverage is measured and reported first. Then it gets a floor that can only rise.

### 8. Build and release

**Principle.** A release can be traced from its source to the file users install, and rebuilt to the same result.

**Criterion.**
- **Version:** Semantic Versioning, with the version kept in one place.
- **Changelog:** one dated section per version. Pull requests add a fragment in `changelog.d/`, and the release assembles the fragments into the version's section.
- **Build:** made by CI from a tagged commit on the main branch. Dependencies are hash-locked, and the build is reproducible.
- **Each release carries:** a checksum, an SBOM, third-party notices, build provenance, and the release-time evidence from sections 2, 4, 7 and 12, each naming the package's checksum.

**Check.** The release job refuses to publish if any of these is missing or failing.

### 9. Update, rollback and compatibility

**Principle.** Updating never loses work. Going back one version is always possible unless the storage format changed, and the app says so.

**Criterion.**
- The previous version is kept.
- A failed first launch of a new version falls back to the previous one.
- Settings carry a version number, and keep keys the running version does not recognise.
- Pointer files (such as the current version) are replaced atomically.
- A storage-format change is gated, and never forced on held or unfinished work.

**Check.** Installer tests for install, update, rollback and uninstall. Unit tests for settings round trips across versions. An end-to-end update test with two real packages.

### 10. Documentation and support

**Principle.** Docs describe how things behave now, and support starts from evidence rather than guesswork.

**Criterion.**
- The user guide, the administrator guide and the release notes are updated in the same pull request as the behaviour.
- Screenshots match the shipped app.
- Docs link to the source of truth instead of restating values that change.
- Bug reports ask for Copy diagnostics and forbid content.
- Each release states its known limitations.

**Check.** The pull request template's definition of done. The consistency tests in section 1. Review.

### 11. Licensing

**Principle.** Every component's licence is known and honoured.

**Criterion.** Third-party notices and the SBOM are generated at build time. A new dependency, model or font needs a licence check before it is added.

**Check.** The build fails if notices cannot be generated. The pull request template asks about new dependencies.

### 12. Accessibility

**Principle.** Everyone who needs the app can use it with a keyboard, a screen reader, zoom and high contrast.

**Criterion.** WCAG 2.2 AA (owner decision DEC-014, 21 October 2026, #121).

**Check.**
- Automated checks on the real window every release, against the WCAG 2.2 AA criteria that can be measured in the page: names, keyboard reach and traps, focus (visible, not obscured, its contrast), text and non-text contrast, target size, label in name, text spacing, reflow and zoom.
- A manual keyboard and screen-reader pass every minor release.
- Each app's Part B says which success criteria its automated check covers, in full or in part, and which are left to the manual pass. An automated pass is never reported as conformance.
- For Unminute, the automated check of the WCAG 2.2 AA criteria it can test (#298) is a gate for the next release (owner decision DEC-027, 22 October 2026).
- A departure from WCAG 2.2 AA is allowed only by the owner's decision, named with its criterion, what it covers and the decision in the app's Part B, and reported by the check rather than failed. Unminute's three, for the brand look (owner decision DEC-047, 25 October 2026, #298), are in "Accessibility against WCAG 2.2 AA", "Owner exceptions".

### 13. Security and privacy

**Principle.** Covered by each app's threat model and security plan.

**Criterion.** As stated in that plan.

**Check.** As stated in that plan. For Unminute: [SECURITY_MODEL.md](SECURITY_MODEL.md), [SECURITY.md](../SECURITY.md) and #111.

### 14. Release governance

**Principle.** A release is a decision, made on evidence, with a way back.

**Criterion.**
- **Definition of done:** in the pull request template.
- **Release criteria:** every gate in Part B passes, and the evidence is attached to the release.
- **Rollout:** the repository makes each release available to everyone. Where and how it is installed and rolled out, including any pilot group, is the owner's to decide and is not part of the repository (the owner's decision DEC-015, 21 October 2026, #121, which withdrew the pilot group and ring rule).
- **After release:** a status or health command lets device management spot failures without telemetry.
- **Severity:**
  - **1:** user content can be lost or exposed, the app's core task does not work, or updates are blocked. A severity 1 issue in a release means withdrawing it.
  - **2:** a main task fails or gives wrong results, short of severity 1.
  - **3:** anything else, such as wording, layout or a minor inconvenience.

  Each app's Part B says what its content and core task are.
- **Withdrawal:** a written runbook. A version number is never reused.
- **Publishing:** always needs explicit approval, separate from merging code.

**Check.** The release job and the runbook. The release record shows who approved it.

## Part B. Unminute profile

### Severity in Unminute

- **Content:** recordings, transcripts, edits, speaker names, meeting titles and handoff text.
- **Core task:** recording, importing and transcribing a meeting.
- **Main tasks:** also speaker identification, review, handoff, holds and cleanup.

### Measuring machine and budgets

| Item | Value |
| --- | --- |
| Measuring machine | No named reference machine (owner decision DEC-005, 18 October 2026, #121). A release's performance check runs on whichever Windows machine prepares the release. `tools/perf_check.py` records that machine's specification in `evidence-perf.json` (`machine`), `tools/evidence.py check` refuses a report without it in full, and the release notes state it (`tools/evidence.py machine`). |
| Recommended specification | 8 cores, 16 GB: a design aim and the installer's recommendation, not a gate. |
| Information-only machine | Installer minimum: 4 cores, 8 GB. |
| Budget numbers | Release-time measured budgets: `docs/perf-budgets.json`, with the baseline they came from and the targets. They were set on the development laptop and apply to whichever machine measures. The idle, start-up and memory budgets are provisional until a release run's medians lower them; the recording budgets came from a release-mode run (#216); the sealed store's and `workspace_s` budgets came from the first Phase C release run, v0.5.0-preview.4's medians by `perf_check.propose()` (#430; the rule is in the gate row below). Deterministic ratchets (package size and file count, imported modules, CSS colours, idle page work): `tests/budgets.json`. |
| Measurement tool | `tools/perf_check.py`: start-up, idle CPU and private bytes per state, worker unload time and the leak check, measured as section 2 describes. Since #111 C7 it also measures the sealed store once per release run, with no window (opening a workspace with 0 and 500 meetings, listing them, a page and a search in a 5,000-passage meeting, and that database's size), and reports `workspace_s`, the time to create each run's new workspace (its schema and its root key, protected by Windows Data Protection; unlocking an existing key is `open_0_meetings_s`); both have budgets, and `tools/evidence.py` requires the store figures. `--compare` sets the figures beside a local copy of an earlier release's report, or says they are not comparable when the machines differ. The worker's peak memory and speed need real models and come from `tools/packaged_check.py`, whose idle figures sum working sets and are not comparable. |

### Gates

Status is one of:
- **In place:** runs today.
- **In place, not yet required:** runs in CI on every pull request, but does not block a merge yet (docs/RELEASING.md says how it becomes required).
- **Manual:** a tool exists, but it is run by hand.
- **Planned:** a slice of #121 will build it.
- **Security plan:** a slice of #111 will build it.
- **Not yet possible:** it needs a decision or resource first; the issue is named.
- **Withdrawn:** the owner decided it is not part of the repository; the decision is named.

| Gate | Standard | Where it runs | Status |
| --- | --- | --- | --- |
| Bridge methods: the allowlist matches the implementation | 1 | Unit test (`tests/test_app.py`) | In place |
| Bridge methods: the page and the design fixtures call only allowlisted names | 1 | Unit test (`tests/test_consistency.py`) | In place |
| Model IDs and hashes: one manifest, docs generated from it | 1 | Unit test (`tests/test_asr_engines.py`) | In place |
| GPU wheel pins match the lock (`requirements.txt`) | 1 | Unit test (`tests/test_consistency.py`) | In place |
| Install identity (taskbar ID, exit codes, name, install folder, registry key) agrees across app, build and installer | 1 | Unit tests (`tests/test_consistency.py`) against `packaging/identity.json`, a test-only mirror that pins the names and is never read at run time; Pester tests in `tests/powershell` (`tools/run_pester.ps1`) | In place (B6a); the Pester job is report only until it has passed on 5 consecutive pull requests (owner's decision DEC-040, 23 Oct 2026; [RELEASING.md](RELEASING.md)) |
| Docs' required-checks lists match the CI job names; no partial job lists; no hard-coded test counts | 1, 10 | Unit test (`tests/test_consistency.py`) | In place |
| Raw colours in `app.css` at or below the ratchet | 1 | Unit test (`tests/test_consistency.py`, ratchet in `tests/budgets.json`; data URIs counted too) | In place; 0 since R12c (#186): the toast tick is a mask filled from `--success` |
| One version source; tag, `VERSION` and dated changelog heading agree | 1, 8 | `tools/changelog.py` sets `VERSION` and the dated heading together; `tools/build_package.py` builds from `VERSION`; the Release workflow refuses a tag that differs; a unit test ties `VERSION` to the newest heading (`tests/test_consistency.py`) | In place; the CI-built release keeps the checks, and its build checks that the zip's `version.json` names the tag and its commit (B3b) |
| Minimum NVIDIA driver, model names and progress fields from one source | 1 | `gpu.MIN_DRIVER` only (Python messages and the page take it from there; README and the guide are tested against it); speech model names and labels in `asr_engines` (`LABELS`); the page shows Python's `label`, `model_label` and download `percent` and reads nothing out of a title or message; unit tests (`tests/test_single_source.py`) | In place (R12a) |
| UI process imports only allowlisted packages, and no heavy library | 3 | Unit test on Linux and Windows (`tests/test_import_hygiene.py`); module count budget on Windows | In place |
| Worker starts on demand and unloads after idle | 3 | Unit tests (`tests/test_workers.py`) | In place |
| Python background wake-ups and query counts bounded | 3 | Unit tests: the worker's idle thread (`tests/test_workers.py`); the watch and policy threads under a fake clock (`tests/test_idle_wakeups.py`); one transaction per watched folder when nothing is new (`tests/test_intake.py`); the Meetings list in a fixed number of queries once speaker counts are known, and Settings and Setup checks kept between polls (`tests/test_queries.py`); the page still fetches the lists on a change, every 6th poll and when shown again (`tools/ui_idle_check.py`) | In place (R5, R7) |
| UI idle behaviour: bridge calls per minute, timers, redraws; visible and hidden | 3 | `tools/ui_idle_check.py` in headless Edge with the design fixtures; the "UI idle (headless Edge)" job in `.github/workflows/ci.yml`; ratchets in `tests/budgets.json` | In place; ratchets lowered by R6 (#127) |
| Meeting view while transcribing: transcript rows fetched and drawn, whole-list redraws; each passage shown once, in order | 3 | `tools/ui_idle_check.py`'s transcribing run (a long meeting growing, with an earlier chunk done again, an edit, speakers and a search), in the same job; ratchets in `tests/budgets.json` (`ui_transcribing`); real-window counterpart in `tools/workspace_ui_check.py` ("growing transcript") | In place (#127) |
| Meetings page while recording: bridge calls per minute by method, meeting-list rebuilds (none, #348), timers, the level meter's draws a second, live-region text changes (none in a steady recording, #346; also none in the idle Meetings page and, visible, in a recording with computer audio (with and without its hint shown) or draft text, a reviewed meeting, a meeting whose transcription stopped and Setup's speaker download, `ui_live_regions`); visible and hidden | 3 | `tools/ui_idle_check.py`'s recording runs (the design fixtures' recording scenario on a simulated 120 Hz display), in the same job; ratchets in `tests/budgets.json` (`ui_recording`); the meter must draw about `METER_HZ` a second while visible (about 60, #216), nothing while hidden, and again within 1 s of being shown; real-window CPU in `tools/perf_check.py`'s recording states | In place (#340) |
| Locked screen: the status asked for, only `api.LOCKED_METHODS` called, then no bridge call, timer, interval or frame; no meeting title shown | 3 | `tools/ui_idle_check.py`'s locked runs (one where the first status fails, so the next one must still end on the locked screen), in the same job (fixed limits of 0, not ratchets); `tests/test_app.py` (`LockedPageTests`, from the page's source); real-window counterparts in `tools/workspace_ui_check.py` and `tools/accessibility_check.py` (a second window on a temporary workspace opened locked) | In place (#111 C4b) |
| WebView2 hidden, suspended and low-memory when minimised | 2, 3 | `workspace/shell.py` (`Minimise`, unit-tested in `tests/test_minimise.py`); the effect is measured by `tools/perf_check.py`'s minimised state | In place |
| Idle CPU, private bytes, start-up, worker unload, leak check | 2, 3 | `tools/perf_check.py` against the release package on the machine that prepares the release; budget logic and the machine's specification unit-tested (`tests/test_perf_check.py`, `tests/test_evidence.py`) | Manual, provisional budgets; the report names the package (`tools/evidence.py`, B3a) and the machine's specification (#121), and is required before a release is published (B3b) |
| CPU and private bytes while recording, visible and minimised | 2 | `tools/perf_check.py`'s recording states: synthetic audio at real-time pace in place of the microphone, started through the app's own recording question; the stand-in and the order of states unit-tested (`tests/test_perf_check.py`) | Budgets in `docs/perf-budgets.json` for CPU, visible and minimised, set by the owner on 23 Oct 2026 from a release-mode run (#216); private bytes while recording not budgeted yet. With microphone recording off by policy these states count as not measured, so the run fails. Recording with live drafts on has its own CPU budgets, set by the owner on 25 Oct 2026 (#104): 40% of one core visible and about 10% minimised. Since #104 slice 4 they are in `budgets` and gate the release run: `perf_check`'s drafts-on states (`recording-drafts`, visible, and `recording-drafts-minimised`) record public speech (`--speech`, such as the AMI meeting ES2004a, CC BY 4.0) with live drafts on and Parakeet v2 copied from `--draft-models`, and a window counts only if live drafts ran through it and made draft text (`draft_phrases`, a count). A release run needs both options; with live drafts off by policy these states count as not measured. Unit-tested with a scripted child (`tests/test_perf_check.py`); not yet measured by a release run, so the first figures come with the next release's evidence |
| Visible states measured while drawn: display on, session unlocked, page drawing | 2 | `tools/perf_check.py` keeps the display on for its run and checks each window measured in a visible state (the session's display power state, the lock and screen saver, a counter of the page's frames and the level display's draws in its own window, with the expected draw rate taken from the display's refresh rate: at least half the level display's own rate, which is about 60 a second (#294; the owner's decision DEC-041 of 23 Oct 2026, #216), so 30 a second on a 60 Hz or faster display); a window not drawn stops the run (exit 2). Unit-tested with a fake display and page (`tests/test_perf_check.py`); `tools/evidence.py check` refuses a report whose runs do not show each visible window drawn (`tests/test_evidence.py`) | In place (#292); passed in the release-mode run of 23 Oct 2026 (#216), at the earlier bar of 15 draws a second; not yet run at the bar of 30 |
| Sealed store and new-workspace time: opening a workspace with 0 and 500 meetings, listing 500, a page and a search in a 5,000-passage meeting, that database's size, and `workspace_s` | 2 | `tools/perf_check.py`'s store step, once per release run with no window, and its start-up figures; budgets in `docs/perf-budgets.json` (`store`, and `workspace_s` under `startup`) from v0.5.0-preview.4's release medians by `perf_check.propose()` (plus 20%, rounded up to 0.01, at least 0.05 above the median, so for the millisecond store timings the 0.05 floor sets the budget; the owner's decision DEC-019 of 21 Oct 2026, #111; #430), compared as measured; the budget logic unit-tested (`tests/test_perf_check.py`), and `tools/evidence.py check` refuses a performance report without the store figures (`tests/test_evidence.py`) | Manual, with the release's performance check; `--quick` has no store step and does not compare it |
| Import time of a two-track recording with echo cancellation (#376) | 2 | `tools/perf_check.py`'s import step, once per release run with no window: a synthetic 1-hour recording (both tracks at 48 kHz, the computer audio's echo on the microphone) saved as the recorder saves one and imported by the app's own code (`import_two_track_1h_s`); a step whose mix fell back to the plain mix stops the run. Unit-tested (`tests/test_perf_check.py`) | Manual, with the release's performance check; `--quick` has no import step. Budget not set yet (`null` in `docs/perf-budgets.json`): the owner's decision DEC-077 of 28 Oct 2026 sets it from a release run's figure, never from a development-laptop run |
| Worker peak memory and speed on real audio | 2 | `tools/packaged_check.py` | Manual; no budget yet (set from release runs, with the measuring machine recorded) |
| Durable processing; crash and resume give identical results | 4 | Unit tests; `tools/packaged_crash_check.py`. Since #111 C7 it also kills the app during an encrypted import (once its `audio.partial` holds the first 12 blocks, so the scan windows from the first 8 s were all decoded and written), at two landings around audio cleanup (each landing's state must be the audio key and audio both there, or the key gone and the audio recorded as removed) and while recording synthetic audio (kept after the restart), placed by what it sees on disk, not by a seam in the package. The landing profile, its meeting kept with its audio removed, must hold none of the marker or the audio windows, and every file left in each profile's redirected `TEMP`, `APPDATA` and `USERPROFILE` fails the report unless `residue.EXPLAINED` names it. A kill at every step of audio cleanup and a kill during the first start (`workspace.db.new`) are unit tests (`tests/test_cleanup.py`, `tests/test_schema2.py`); a locked start is C4a's unit tests and the #189 password-reset check | Unit tests in place; packaged check manual, and its report names the package (B3a) and is required before a release is published (B3b) |
| Integrity check after an unclean shutdown | 4 | `PRAGMA quick_check` at start after an unclean close; damage holds new work until Continue anyway; `--status` and `--health` report it; unit tests (`tests/test_integrity.py`) | In place (R11) |
| Settings validated on load | 4, 9 | Unit tests (`tests/test_hardening.py`) | In place |
| Settings versioned; unknown keys kept on rollback | 9 | `settings_version`; kept keys and newer values; reset settings named in Settings; fsync before the swap; unit tests (`tests/test_integrity.py`) | In place (R11) |
| Local content-free log, crash hooks, incident codes | 5, 6 | `workspace/diag.py`; unit tests and the marker test (`tests/test_diagnostics.py`); forced crashes in the app and the worker | In place (R9a) |
| Page errors, Copy diagnostics, `--health` | 5, 6, 14 | `report_ui_error` (fixed kind and numbers, rate-limited); Help > Copy diagnostics; `--health`; unit tests and the marker test (`tests/test_diagnostics.py`); a page error before the bridge is ready, kept and reported once it is (`tools/ui_idle_check.py`, in the UI idle job) | In place (R9b); the early-error check (#171) runs in the UI idle job |
| Every job in `.github/workflows/ci.yml` (the required ones are listed in [RELEASING.md](RELEASING.md)) | 7 | CI | In place |
| CodeQL for Python, JavaScript and workflows | 7 | CI (`.github/workflows/codeql.yml`) | In place |
| OpenSSF Scorecard: the repository's supply-chain practices | 8, 13 | `.github/workflows/scorecard.yml`, weekly and on pushes to `main`, results in the Security tab; its triggers and permissions held by `tests/test_consistency.py` (`ScorecardTests`) | Report only (the owner's decision DEC-069, 27 Oct 2026, #415): never on a pull request and never required; Code-Review and Signed-Releases are expected to score low, accepted |
| JavaScript syntax | 7 | The "JavaScript syntax" job in `.github/workflows/ci.yml` | In place |
| Coverage reported, then a rising floor | 7 | The "Coverage (report only)" job in `.github/workflows/quality.yml`: lines and branches, with the Python processes the tests start (`.coveragerc`, #413) | Report in place, not yet required; no floor yet (a floor needs the owner) |
| Static type check | 7 | The "Type check (report only)" job in `.github/workflows/quality.yml`: mypy over `workspace/` (`mypy.ini`), compared with `tools/mypy_baseline.txt` by `tools/type_check.py` (#413) | Report only (the owner's decision DEC-059, 27 Oct 2026); the page has no type check yet |
| Changelog fragment for shipped changes | 8, 10 | `.github/workflows/changelog.yml` (a `changelog.d/` fragment; the `no-changelog` label opts out); fragments checked by `tools/changelog.py --check` in the unit tests | In place |
| Manual keyboard and screen-reader pass, every minor release, covering the WCAG 2.2 AA criteria the automated check leaves out ("Accessibility against WCAG 2.2 AA" below) | 12 | [VALIDATION_RUNBOOK.md](VALIDATION_RUNBOOK.md) | Manual; not yet done (validation tracker #189) |
| Real-window UI and accessibility checks | 7, 12 | `tools/workspace_ui_check.py`, `tools/accessibility_check.py` (the automatable WCAG 2.2 AA criteria, #298; "Accessibility against WCAG 2.2 AA" below), against the package with its own Python (`--app`) | Manual; the reports name the package (B3a) and are required before a release is published (B3b). The automatable WCAG 2.2 AA checks (#298) are a gate for the next release (owner decision DEC-027, 22 October 2026). Three owner exceptions for the brand look (1.4.11 for control borders and the meeting title at rest; the done tick as a graphic rather than text under 1.4.3; owner decision DEC-047, 25 October 2026) are reported, not failed: "Owner exceptions" below. The accessibility checks themselves are unit-tested on synthetic fragments in headless Edge (`tests/test_accessibility_check.py`) |
| Packaged lifecycle and residue | 7 | `tools/packaged_check.py` (pass criteria in its `verdict`), with `tools/residue.py` (unit-tested in `tests/test_residue.py`). Since #111 C7: a positive control (the scanner must find each pattern in a planted file); the marker and windows of the decoded audio absent at rest while the meeting exists, and `--verify-audit` ok; a synthetic recording through the shipped confirmation, absent while it records (the scan includes at least one finished chunk), its key gone after import and discard; the audio key gone and the text key kept after audio cleanup; nothing blocking an update, no free pages and an empty write-ahead log after final cleanup; only the workspace's own files left (built from the app's constants), the logs listed; no crash dump under the throwaway base. `TEMP`, and the children's `APPDATA` and `USERPROFILE`, are redirected into that base and scanned, and every file left there is named and fails the report unless `residue.EXPLAINED` names it; the real LocalLow and the model cache are listed before and after (names, sizes and times only, never opened), and every new or changed file there must be explained. Since #418, before the at-rest scan, the marked meeting is opened in the real window through the real start (the shortcut's flags), its transcript shown and nothing played, through UI Automation, then the window is closed normally; the model cache is lent to that start (hard links and copies in the throwaway profile) and taken back, and any file it added there must be explained. The at-rest and after-cleanup scans then cover the WebView2 profile (`workspace\webview`): what the close left is counted and scanned, then removed as the next start removes it, so a profile file left after final cleanup fails; `instance.lock` and `logs\launch.json` must be new from that start; and anything else left in the redirected `LOCALAPPDATA` is scanned and named, failing unless explained. The children keep the audit log in its file only (no Event Log entries); the two windows, started as the app is, write their own, and the report counts those written for its workspace, with why. A report that would carry the Windows user name anywhere, a file-name list included, fails and names only where (`residue.guarded`). It needs an idle, unlocked desktop | Manual; the report names the package (B3a) and is required before a release is published (B3b) |
| Accuracy on the public clips in [ENGINE_EVALUATION.md](ENGINE_EVALUATION.md) (proposed: WER no worse than +0.5 points) | 7 | `tools/compare_engines.py` when engines or dependencies change | Manual |
| Offline guard blocks network access | 13 | Unit tests (`tests/test_offline_guard.py`); `--self-test` on the installed package | Unit tests in place; `--self-test` manual |
| Hash-locked dependencies | 8 | `tools/lock.py --check` (each lock resolved again, with hashes taken afresh), `tools/lock.py --compare` (version changes against the base branch) and `pip download --require-hashes` in `ci.yml` (Requirements resolve); `tools/build_package.py` installs with `--require-hashes --no-deps`; `tests/test_dependency_locks.py` | In place |
| Vulnerability audit | 8 | `tools/audit.py` (pip-audit over every lock) in `audit.yml` on every pull request and daily, with expiring ignores in `requirements/audit-ignore.json` | In place |
| Reproducible build with SBOM, third-party notices, model manifest and binary inventory; binaries and their signers match an allowlist; a release builds only the tagged commit's committed files from a clean tree | 8, 11 | `tools/build_package.py` (`--release`; `packaging/binary-allowlist.json`); `tests/test_package.py` | In place: the Release workflow builds every published package (B3b; it built v0.5.0-preview.2, v0.5.0-preview.3 and v0.5.0-preview.4) |
| The package's app folder holds exactly the source files in `packaging/app-allowlist.json` (plus their `.pyc`): no trial page, tool, test or design asset | 8 | `tools/build_package.py` (`check_app_files`, before precompiling); unit test (`tests/test_package.py`) | In place |
| Dead code: vulture over the shipped code, with reviewed exceptions in `tools/vulture_whitelist.py` | 7 | CI (`quality.yml`, Dead code) | Report only; a ratchet in `tests/budgets.json` once stable |
| Code size and complexity: each code file's lines and growth, the largest modules, and functions over ruff's C901 threshold, against `tools/quality_baseline.json` | 7 | `tools/quality_report.py` in CI (`quality.yml`, Quality report) | Report only (#365): complexity new since the baseline is a warning on the pull request; a gate needs the owner |
| Security lint: ruff's S rules (flake8-bandit), each accepted finding triaged with its reason in `tools/security_baseline.json` | 7, 13 | `tools/security_lint.py` in CI (`quality.yml`, Quality report, on every pull request); unit-tested (`tests/test_security_lint.py`) | Report only (#415 item 2; the owner's decision DEC-081 of 28 Oct 2026): a file and rule over the baseline is a warning on the pull request; not in the required "Ruff" check (`ruff.toml`); a gate needs the owner |
| Pruned, precompiled build; size and file-count budget | 8, 11 | `tools/build_package.py` (`PRUNED_*`, `precompile`; the zip checked against `package_zip_mb` and `package_files` in `tests/budgets.json`); `tests/test_package.py`; `--verify-precompiled` in the Release workflow | In place: built by the Release workflow, which also checks each shipped `.pyc` against its source (B3b) |
| CI-built, attested release behind an approval; evidence present and naming the zip SHA-256 | 8, 14 | `.github/workflows/release.yml`: built on a GitHub-hosted runner, provenance and SBOM attestations, a draft, then the `release` environment's approval; the publish job checks the draft's files, the attestations (this workflow, tag and commit) and the evidence (`tools/evidence.py check --commit`); `tests/test_release_workflow.py`, `tests/test_evidence.py`. IT verifies a download with `tools/intune/Get-VerifiedRelease.ps1` | In place (B3a, B3b): the first run, v0.5.0-preview.1, failed at the build and published nothing (fixed in #235); v0.5.0-preview.2 (19 Oct 2026), v0.5.0-preview.3 (20 Oct 2026) and v0.5.0-preview.4 (27 Oct 2026) were built, attested and published by the workflow. Releases stay at this repository (owner decision DEC-017, 21 Oct 2026, #111) |
| Update gate, rollback and migration | 9 | Unit tests (`tests/test_update.py`); installer end to end | Unit tests in place; installer check manual |
| First-launch fallback to the previous version; atomic version pointer; installer log | 9 | Installer tests (`tests/test_hardening.py`, `tests/powershell`); launcher tests (`tests/test_launch.py`) | Atomic `current.json` and the installer log in place (B6a); first-launch check and fallback in place for per-user installs and machine mode (B5b; machine mode itself is B6b) |
| Screenshots match the shipped app | 10 | Pull request template and review | In place |
| Known limitations stated in each full release's notes | 10, 14 | [RELEASING.md](RELEASING.md), "Cutting a release", step 3 | Manual |
| Pilot ring before a full release | 14 | Release decision | Withdrawn: rollout, including any pilot group, is the owner's and not part of the repository (the owner's decision DEC-015, 21 October 2026, #121) |
| Device-management health check without telemetry | 14 | `--status`; `--health` for Intune Remediation (`docs/PACKAGING.md`) | In place |
| Withdrawal runbook | 14 | [RELEASING.md](RELEASING.md) | In place |
| Pull request definition of done and bug template | 10, 14 | `.github/pull_request_template.md`, `.github/ISSUE_TEMPLATE/` | In place |
| Agent instructions: the paths and invariant guard tests that `AGENTS.md` names exist; no `CLAUDE.md`; no tracked media | 1, 10 | Unit test (`tests/test_consistency.py`); Claude Code guard tested in `tests/test_agent_guard.py` | In place |

### Accessibility against WCAG 2.2 AA

The target is WCAG 2.2 AA (section 12). `tools/accessibility_check.py` is the automated half of the check: it opens the real window on a throwaway workspace with synthetic audio and fake engines, visits a fixed list of views and dialogs (Setup's steps, Downloads, Meetings with the microphone chooser, the recording card with draft text (#370), Settings, Help, a meeting with its Versions, Holds and Speakers tabs, the names and confirmation dialogs, and the Hand off page), and fails on any finding that a named owner exception (below) does not cover. Before each audit it waits up to 3 s for the page's finite animations and transitions to finish, so no colour is measured mid-change (endless ones, such as the spinner, are measured as they are), and a view that has not settled by then fails. These checks (#298) are a gate for the next release (owner decision DEC-027, 22 October 2026). By default it checks the page in the PC's own colour mode; `--colour-mode light` or `dark` draws the page in that mode instead, and `--colour-mode both` audits each view in light and then in dark, reported as separate views. The checks are the same in each mode. The mode is set for the check's own window only, through the browser's media emulation and WebView2's preferred colour scheme, so it works whatever mode Windows is in and never changes Windows' setting (#328). A mode the page does not show, in both its media query and its styles, stops the run. In the PC's own mode the page is read once, with no switch to wait for, and the run stops just the same if its media query and its styles disagree. Release evidence runs it with `both` ([RELEASING.md](RELEASING.md)): every release is checked in light and dark mode (the owner's decision DEC-032, 22 October 2026, #111), and `tools/evidence.py check` refuses an accessibility report that does not show every view on that list audited in both (`tests/test_evidence.py`). Passing it is not conformance: much of WCAG 2.2 AA needs the manual keyboard and screen-reader pass ([VALIDATION_RUNBOOK.md](VALIDATION_RUNBOOK.md), section 5; validation tracker #189, sitting 5). `tests/test_accessibility_check.py` runs the audit in headless Edge on synthetic page fragments, and shows each check failing on a fragment that breaks its criterion.

What it checks, and fails on:

- **4.1.2 Name, Role, Value (names only):** every visible control and open dialog has an accessible name. Roles, states and values are not checked.
- **2.1.1 Keyboard (in part):** no visible, enabled control is taken out of the tab order, and nothing with a pointer cursor is unreachable by keyboard. It does not operate each control from the keyboard.
- **2.1.2 No Keyboard Trap (in part):** on each focusable control (the first 80 in a view), the page does not swallow Tab or Shift+Tab while focus stays put, and focus moved on to the next control is not pulled back. The key presses are simulated, so the browser's own movement of focus, and leaving a dialog with Escape, are left to the manual pass.
- **2.4.7 Focus Visible:** each focusable control (the first 80 in a view) shows an outline or shadow on keyboard focus. While the window does not have keyboard focus (another window in front), the browser draws no focus style, so the check stops that pass and fails the view as `focus_not_measured` instead of reporting each control. It does the same when the browser does not treat the focus as keyboard focus (`:focus-visible` does not match).
- **2.4.11 Focus Not Obscured (Minimum):** each of those controls, once focused and scrolled into view, is not entirely hidden: at least one of nine points across its visible part shows the control or its label on top. Content that lets the pointer through (`pointer-events: none`) is not seen.
- **1.4.3 Contrast (Minimum):** text against its effective background, 4.5:1, or 3:1 for large text; disabled controls are exempt. Text the style draws (generated content) is not measured; the page draws only the tick on a done step that way, as a graphic (below). Backgrounds are taken as the nearest solid colour, so text over an image or gradient is not measured.
- **1.4.11 Non-text Contrast (in part):** the focus outline (or, without one, the focus shadow) against the colour it is drawn on, 3:1, unless the browser draws it; and the boundary of each field, and of each checkbox or switch the page draws itself, 3:1 against what is around it: its border or its fill, or those of a wrapper that holds no other control. Other state indicators (the selected tab, a switch's position, icons) and graphics are not measured.
- **2.5.8 Target Size (Minimum):** each pointer target is at least 24 by 24 CSS px, counting a label that operates the control as part of its target, or meets the spacing exception (a 24 px circle on its centre meets no other target and no other undersized target's circle). It recognises the inline exception (a control within a line of text) and the user agent exception (a checkbox or radio button at the browser's own size). It cannot judge the equivalent or essential exceptions: a target that relies on one is reported for a person to decide.
- **2.5.3 Label in Name:** a control named with `aria-label` or `aria-labelledby` has its visible text, or its label's, in that name as whole words, comparing letters and digits only and ignoring case ("Delete" is not in "Undelete"). A label of symbols only (such as ×) is not compared. Visible text near a control but not tied to it is not seen.
- **1.4.12 Text Spacing (in part):** with line height 1.5, spacing after paragraphs of twice the font size, letter spacing 0.12 and word spacing 0.16 times the font size applied to the whole page, no text is newly cut off by a box that hides its overflow. Overlapping text, and text already cut off before the spacing, are not measured.
- **1.4.10 Reflow:** at 640 and 320 CSS px wide (200% and 400% zoom of a 1280 px window), no horizontal scrolling or cut-off content.
- **1.4.4 Resize Text (in part):** Ctrl+plus zooms the page and Ctrl+0 resets it, checked once, not per view.
- **1.4.1 Use of Color (in part):** status badges carry text or a name, not colour alone.
- **1.1.1 Non-text Content (in part):** every image has a text alternative (an empty one for decoration). Whether the text is right is not judged.
- **3.2.1 On Focus (in part):** focusing a control opens no dialog and changes no address.
- **3.1.1 Language of Page, 2.4.2 Page Titled and 4.1.3 Status Messages (in part):** checked once: the page has a language tag, the window has a title, and the status region is live (polite or assertive). Whether the title describes the page, and whether each message is announced, are left to the manual pass.

Not checked by the tool, so left to the manual pass:

- **New in WCAG 2.2:** 2.5.7 Dragging Movements, 3.2.6 Consistent Help, 3.3.7 Redundant Entry and 3.3.8 Accessible Authentication (Minimum), and the equivalent and essential exceptions to 2.5.8 for any target the tool reports.
- **From 2.1 and earlier:** screen-reader output, reading and focus order (1.3.1, 1.3.2, 2.4.3), real Tab, Shift+Tab and Escape through each view and dialog (2.1.2), timing such as the confirmation button's delay (2.2.1), pausing moving content such as the live recording curve (2.2.2), non-text contrast of state indicators and graphics (1.4.11), overlapping text with the text spacing applied (1.4.12), content on hover or focus (1.4.13), headings and labels (2.4.6), changes of context on input (3.2.2), status messages as announced (4.1.3), and error identification and suggestions (3.3.1, 3.3.3).
- **Windows contrast themes** (forced colours). Expected, not yet checked: the theme's colours replace the page's, so borders and focus rings take the theme's colours. The meeting title's 1 px border, transparent at rest in the page's own colours, takes the theme's colour at rest too, and the 2 px focus ring with keyboard focus is thicker, so focus still shows by thickness; the hover change of colour may not show.
- **The confirmation windows the app draws outside the page** (recording, deletion authority, discard; `workspace/consent.py`): they are not part of the page the tool audits.
- **States the tool does not visit:** a recording in progress, a recovered recording, error and damage messages, and the Downloads page while a download runs.

#### Owner exceptions

The owner puts the brand look before visible accessibility changes: accessibility is met wherever it does not visibly change the look (labels, target sizes by invisible padding, focus rings), and a visible change to colours, borders or chrome for accessibility needs the owner's approval first. Having seen #319's changes in a real window, the owner accepted (DEC-047) three departures from WCAG 2.2 AA on 25 October 2026 (NZ time; [#298](https://github.com/thatskiff33/unminute/issues/298#issuecomment-6391966468)). Each is named in `OWNER_EXCEPTIONS` in `tools/accessibility_check.py`, which reports a finding it covers as an owner exception, not a failure. It covers only a finding its pattern matches in full: any other finding of the same check still fails, and `tests/test_ui_contrast.py`, `tests/test_accessibility_check.py` and `tests/test_ui_page_audit.py` keep the record, the page and the boards agreeing. A new or wider exception needs the owner's decision first.

| Exception | WCAG 2.2 AA | What it covers | Why |
| --- | --- | --- | --- |
| Control borders | 1.4.11 Non-text Contrast | The border of fields, lists, buttons and the off position of switches (`--control-line`): rgba(20, 41, 102, 0.28) light, about 1.8:1 on white, and rgba(255, 255, 255, 0.32) dark, below 3:1. The token's other uses are below 3:1 too: the border of a paused recording card, of the pop-up for moving a passage to another speaker, and of the setup steps not yet reached. The check excuses a field boundary finding only in exactly the token's colour for the mode it audited (the light value in light, the dark value in dark) | The brand look; #319's darker borders (3:1) made the app look off-brand |
| Meeting title at rest | 1.4.11 Non-text Contrast | The meeting title, a field drawn as a heading (`.title-edit textarea`), shows no border at rest; hovered, its border takes the text colour (at least 3:1), and keyboard focus draws the 2 px focus ring. The finding for `textarea#m-title` with a transparent border | The brand look; that it can be edited shows on hover and focus |
| Done tick | 1.4.3 Contrast (Minimum), treated under 1.4.11 | The white tick on a done step's brand green #00A275 (3.27:1). It is a graphic, not text: the style draws it (`.done-tick`) with an empty text alternative, in a marker hidden from assistive technology, and the step says "done" in words. As a graphic it meets 1.4.11's 3:1. No finding is excused: the tick is not text for the 1.4.3 check to find, and `tests/test_ui_page_audit.py` measures it at 3:1 | The brand green; #319's darker green (4.58:1) changed the look |

### Exceptions

- **Packaged evidence predates current releases.** Most evidence in [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) comes from the 0.2.0 previews. From security plan B3, every release needs the five evidence reports run against its own package (docs/RELEASING.md).
- **Human and hardware checks are open.** The checks that need people or hardware are listed in [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md), "Before a business release". From now on, a full release lists those still open as known limitations in its notes.
