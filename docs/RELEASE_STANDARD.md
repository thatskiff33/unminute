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

Each budget covers CPU, memory, start-up time and the peak cost of heavy work. Budgets live in one file, set against the recommended hardware. A run on the minimum hardware is recorded for information.

**Check.** Measure the same way every time:
- **CPU:** process CPU time (kernel plus user) for the whole process tree, over 300 s after a 60 s settle. Take the median of three runs and express it as a percentage of one logical core.
- **Memory:** private bytes summed across the process tree, with private working set reported alongside. Never sum working sets, because shared pages are counted once per process.
- **Conditions:** mains power and the Balanced power plan, with antivirus idle. Record the machine, the app version and the versions of embedded runtimes such as the browser engine.
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

**Criterion.** WCAG 2.1 AA (whether to move to 2.2 AA is to confirm).

**Check.**
- Automated checks on the real window every release: names, keyboard reach, focus, contrast, reflow and zoom.
- A manual keyboard and screen-reader pass every minor release.

### 13. Security and privacy

**Principle.** Covered by each app's threat model and security plan.

**Criterion.** As stated in that plan.

**Check.** As stated in that plan. For Unminute: [SECURITY_MODEL.md](SECURITY_MODEL.md), [SECURITY.md](../SECURITY.md) and #111.

### 14. Release governance

**Principle.** A release is a decision, made on evidence, with a way back.

**Criterion.**
- **Definition of done:** in the pull request template.
- **Release criteria:** every gate in Part B passes, and the evidence is attached to the release.
- **Rings:** releases go to the maintainer, then a pilot group on a pre-release, then everyone. Proposed rule: at least 5 working days in pilot, with no open severity 1 or 2 issue.
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

### Reference machine and budgets

| Item | Value |
| --- | --- |
| Reference machine (budgets) | Recommended spec: 8 cores, 16 GB. A named, managed laptop is still to be chosen (#121). |
| Information-only machine | Installer minimum: 4 cores, 8 GB. |
| Budget numbers | `docs/perf-budgets.json`, the only place they live, with the baseline they came from and the targets. Provisional until the reference laptop is measured. |
| Measurement tool | `tools/perf_check.py`: start-up, idle CPU and private bytes per state, worker unload time and the leak check, measured as section 2 describes. The worker's peak memory and speed need real models and come from `tools/packaged_check.py`, whose idle figures sum working sets and are not comparable. |

### Gates

Status is one of:
- **In place:** runs today.
- **In place, not yet required:** runs in CI on every pull request, but does not block a merge yet (docs/RELEASING.md says how it becomes required).
- **Manual:** a tool exists, but it is run by hand.
- **Planned:** a slice of #121 will build it.
- **Security plan:** a slice of #111 will build it.
- **Not yet possible:** it needs a decision or resource first; the issue is named.

| Gate | Standard | Where it runs | Status |
| --- | --- | --- | --- |
| Bridge methods: the allowlist matches the implementation | 1 | Unit test (`tests/test_app.py`) | In place |
| Bridge methods: the page and the design fixtures call only allowlisted names | 1 | Unit test (`tests/test_consistency.py`) | In place |
| Model IDs and hashes: one manifest, docs generated from it | 1 | Unit test (`tests/test_asr_engines.py`) | In place |
| GPU wheel pins match the requirements pins | 1 | Unit test (`tests/test_consistency.py`) | In place |
| Install identity (taskbar ID, exit codes, name, install folder, registry key) agrees across app, build and installer | 1 | Unit test (`tests/test_consistency.py`); later `packaging/identity.json` with Pester tests | In place; security plan B6 replaces the copies |
| Docs' required-checks lists match the CI job names; no partial job lists; no hard-coded test counts | 1, 10 | Unit test (`tests/test_consistency.py`) | In place |
| Raw colours in `app.css` at or below the ratchet | 1 | Unit test (`tests/test_consistency.py`, ratchet in `tests/budgets.json`) | In place; reaches 0 in R12 (#133) |
| One version source; tag, `VERSION` and dated changelog heading agree | 1, 8 | Build and release job | Planned: R10 (#131); security plan: B3 |
| Minimum NVIDIA driver, model names and progress fields from one source | 1 | Unit test | Planned: R12 (#133) |
| UI process imports only allowlisted packages, and no heavy library | 3 | Unit test on Linux and Windows (`tests/test_import_hygiene.py`); module count budget on Windows | In place |
| Worker starts on demand and unloads after idle | 3 | Unit tests (`tests/test_workers.py`) | In place |
| Python background wake-ups and query counts bounded | 3 | Unit tests | Planned: R5 (#126), R7 (#128) |
| UI idle behaviour: bridge calls per minute, timers, redraws; visible and hidden | 3 | `tools/ui_idle_check.py` in headless Edge with the design fixtures; the "UI idle (headless Edge)" job in `.github/workflows/quality.yml`; ratchets in `tests/budgets.json` | In place, not yet required; tightened in R6 (#127) |
| WebView2 hidden, suspended and low-memory when minimised | 2, 3 | Release-time performance run | Planned: R8 (#129) |
| Idle CPU, private bytes, start-up, worker unload, leak check | 2, 3 | `tools/perf_check.py` against the release package on the reference machine; budget logic unit-tested (`tests/test_perf_check.py`) | Manual, provisional budgets; enforced by security plan B3 |
| Worker peak memory and speed on real audio | 2 | `tools/packaged_check.py` | Manual; no budget until the reference laptop is measured |
| Durable processing; crash and resume give identical results | 4 | Unit tests; `tools/packaged_crash_check.py` | Unit tests in place; packaged check manual |
| Integrity check after an unclean shutdown | 4 | Unit tests; `--status` | Planned: R11 (#132) |
| Settings validated on load | 4, 9 | Unit tests (`tests/test_hardening.py`) | In place |
| Settings versioned; unknown keys kept on rollback | 9 | Unit tests | Planned: R11 (#132) |
| Local content-free log, crash hooks, Copy diagnostics, `--health` | 5, 6, 14 | Unit tests and the marker test | Planned: R9 (#130) |
| Every job in `.github/workflows/ci.yml` (the required ones are listed in [RELEASING.md](RELEASING.md)) | 7 | CI | In place |
| CodeQL for Python, JavaScript and workflows | 7 | CI (`.github/workflows/codeql.yml`) | In place |
| JavaScript syntax | 7 | The "JavaScript syntax" job in `.github/workflows/quality.yml` | In place, not yet required |
| Coverage reported, then a rising floor | 7 | The "Coverage (report only)" job in `.github/workflows/quality.yml` | Report in place, not yet required; no floor yet |
| Changelog fragment for shipped changes | 8, 10 | `.github/workflows/changelog.yml` (a `changelog.d/` fragment; the `no-changelog` label opts out); fragments checked by `tools/changelog.py --check` in the unit tests | In place, not yet required |
| Manual keyboard and screen-reader pass, every minor release | 12 | [VALIDATION_RUNBOOK.md](VALIDATION_RUNBOOK.md) | Manual; not yet done (#27) |
| Real-window UI and accessibility checks | 7, 12 | `tools/workspace_ui_check.py`, `tools/accessibility_check.py` | Manual; evidence enforced by security plan B3 |
| Packaged lifecycle and residue | 7 | `tools/packaged_check.py` | Manual; extended by security plan C7 |
| Accuracy on the public clips in [ENGINE_EVALUATION.md](ENGINE_EVALUATION.md) (proposed: WER no worse than +0.5 points) | 7 | `tools/compare_engines.py` when engines or dependencies change | Manual |
| Offline guard blocks network access | 13 | Unit tests (`tests/test_offline_guard.py`); `--self-test` on the installed package | Unit tests in place; `--self-test` manual |
| Hash-locked dependencies, vulnerability audit | 8 | CI | Security plan: B1 |
| Reproducible, pruned build with SBOM and third-party notices; size and file-count budget | 8, 11 | `tools/build_package.py` | Security plan: B2 |
| CI-built, attested release behind an approval; evidence present and naming the zip SHA-256 | 8, 14 | Release job | Security plan: B3 |
| Update gate, rollback and migration | 9 | Unit tests (`tests/test_update.py`); installer end to end | Unit tests in place; installer check manual |
| First-launch fallback to the previous version; atomic version pointer; installer log | 9 | Installer and launcher tests | Security plan: B5, B6 |
| Screenshots match the shipped app | 10 | Pull request template and review | In place |
| Known limitations stated in each full release's notes | 10, 14 | [RELEASING.md](RELEASING.md), "Unminute package releases", step 3 | Manual |
| Pilot ring before a full release | 14 | Release decision | Not yet possible: no pilot group chosen (#121) |
| Device-management health check without telemetry | 14 | `--status` (in place); `--health` for Intune Remediation | `--status` in place; `--health` planned: R9 (#130) |
| Withdrawal runbook | 14 | [RELEASING.md](RELEASING.md) | In place |
| Pull request definition of done and bug template | 10, 14 | `.github/pull_request_template.md`, `.github/ISSUE_TEMPLATE/` | In place |

### Exceptions

- **Packaged evidence predates current releases.** Most evidence in [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) comes from the 0.2.0 previews. It is not re-run in full for each release until R3 (#124) and security plan B3 automate it.
- **Human and hardware checks are open.** The checks that need people or hardware are listed in [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md), "Before a business release". From now on, a full release lists those still open as known limitations in its notes.
