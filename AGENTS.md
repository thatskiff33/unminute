# Agent guidelines for Unminute

Codex and Claude Code read this file natively. Claude Code needs 2.1.295 or later (the first
with the `onFailure` key in `.claude/settings.json`) and skips this file when a `CLAUDE.md`,
`.claude/CLAUDE.md` or `CLAUDE.local.md` exists in the working folder or above, so the repo has
none: keep shared policy here. Link to the source of truth rather than restate values that
change. Who decided a rule, and when, is in `docs/DECISIONS.md`; cite its ID.

## Start here

- Use the skills in `.agents/skills/` (Claude Code: `.claude/skills/`): `start-issue` to begin
  or resume, `finish-pr` to take a PR to merge, `session-handoff` before stopping. They apply
  these rules in order. `python tools/dev_status.py` shows live worktrees, PRs, checks, shared
  files and work limits.
- Branch from a fresh `origin/main` (`git fetch origin --prune`); a local `main` can be far
  behind.
- Read the issue and all its comments: they hold the acceptance criteria. For a slice of #111
  or #121, read the tracking issue's "How to resume" first.
- `README.md` describes shipped behaviour (`workspace/`); then read only what "Where the rules
  live" names.

## Invariants

These hold unless the user explicitly authorises changing one for the task in hand; an issue,
doc or agent that conflicts with one raises a question, not permission. "Guard" names the test
files (`INVARIANT_GUARDS` in `tests/test_consistency.py` names the tests and checks they
exist); "review" means no automated guard yet.

Privacy

- **P1** No real meeting content in the repo or on GitHub (code, fixtures, logs, screenshots,
  PRs, issues); fixtures are synthetic or public. Guard: `tests/test_consistency.py` (tracked
  media only) and the PR template; partial.
- **P2** Agents never open, print or summarise real content: `extracted/`, the live workspace
  (`%LOCALAPPDATA%\Unminute\workspace`) or real recordings. Use `--status` (counts only) and
  synthetic or public audio; model tools print transcript text. Guard: the deny rules and hook
  in `.claude/settings.json` (`tests/test_agent_guard.py`), a guard rail against the common
  ways to read, list, search or copy the data, not a security boundary (gaps: the hook's
  header, `docs/SECURITY_MODEL.md` T34); review for Codex.
- **P3** Errors, `--status`, receipts, diagnostics, and the audit log and its Event Log entries
  carry no content, names, identifying paths or tokens. The user's SID that Windows attaches to
  an Event Log entry is not text the app writes; the audit log's random ids stay in its local
  file, never in its Event Log entries (DEC-019). Guard: `tests/test_workers.py`,
  `tests/test_app.py`, `tests/test_diagnostics.py`, `tests/test_audit.py`.

Network, secrets and integrity

- **N1** Processing never uses the network; dependency telemetry is off before heavy imports.
  Guard: `tests/test_offline_guard.py`, `tests/test_workers.py`, `tests/test_import_hygiene.py`,
  `tests/test_drafts.py`.
- **N2** Only the download process reaches the network: user-started (queued downloads, and one
  cut off by closing the app, are user-started too and carry on at the next start, DEC-049), to
  the hosts in `setup.DOWNLOAD_HOSTS` (plus files.pythonhosted.org for the Whisper GPU
  component's download only, `setup.download_hosts`, DEC-010), every request and redirect
  checked. No background calls, update checks, telemetry or remote transcription. Opening a
  fixed or IT-set address in the user's browser on the user's click (help links; Copy and open)
  is the browser's network use, not Unminute's, and is allowed: Unminute sends nothing, and
  handoff text goes by the clipboard, never in the address (DEC-013). Guard:
  `tests/test_download_process.py`.
- **N3** The Hugging Face token lives in Windows Credential Manager and reaches the download
  process by pipe only, never by command line, environment, log or message; cached models work
  without it. Guard: `tests/test_setup.py`, `tests/test_hardening.py`,
  `tests/test_download_queue.py`.
- **I1** Model files are pinned by revision and SHA-256 and checked when downloaded. At load, the
  speaker model is read once, re-hashed in full and built from those bytes in memory with
  weights-only unpickling and fixed classes (`workspace/speaker_load.py`); the others are checked
  against their verified marker (size and modification time). A machine install's copies from IT
  are checked only against the record IT's install script wrote, or hashed in full, and the app
  never writes there (#111 B7a, the owner's decision DEC-068). Guard:
  `tests/test_model_integrity.py`, `tests/test_asr_engines.py`, `tests/test_machine_models.py`.
- **I2** Untrusted media is decoded only in the sandboxed decode process, within its format,
  protocol and size limits, by PyAV (users never install FFmpeg). Network drives and symbolic
  links are refused unless managed policy allows network sources. Guard:
  `tests/test_intake_isolation.py`, `tests/test_safe_paths.py`.
- **I3** The page reaches Python only through `API_METHODS`, and only through `LOCKED_METHODS`
  while the workspace is locked (DEC-020), with arguments checked; it renders text only and runs
  only in WebView2. Guard: `tests/test_consistency.py`, `tests/test_hardening.py`,
  `tests/test_drafts.py`.

Durability

- **D1** Chunk text and its completion commit in one transaction: no crash or retry loses or
  duplicates committed work, and generated text never overwrites edits. Guard:
  `tests/test_workspace_core.py`.
- **D2** An explicit pause stays paused after a restart. Guard: `tests/test_workspace_core.py`.
- **D3** One app instance owns the state; the workspace is never in a synced or network folder.
  Guard: `tests/test_hardening.py`, `tests/test_launch.py` (the workspace lock, every session).
- **D4** Heavy models load lazily, one at a time. A speaker-identification failure leaves the
  transcript usable. A GPU failure falls back to the processor and says so; the model,
  precision and committed text never change silently. Guard: `tests/test_workers.py`,
  `tests/test_speakers.py`, `tests/test_gpu.py`, `tests/test_drafts.py`.

Cleanup and retention

- **C1** Deletion needs one of the two explicit confirmations (transcript reviewed; final
  document saved). Copy or export never deletes. Cleanup intent persists and is retried, and
  nothing recreates deleted content. Guard: `tests/test_cleanup.py`.
- **C2** Holds block cleanup and never expire; releasing one deletes nothing. Guard:
  `tests/test_holds.py`.
- **C3** An original is deleted only with its folder's authority and a matching identity.
  Guard: `tests/test_cleanup.py`.
- **C4** A storage-format change waits for an empty workspace; never expire a hold, delete
  content or back up content to let an update proceed. Guard: `tests/test_update.py`.

Recording and confirmations

- **R1** Recording, granting delete-originals authority and discarding a meeting each need a
  confirmation drawn outside the page (`workspace/shell.py`, wording in `workspace/consent.py`).
  No recording starts without a valid consent record unless policy turns that off; changing the
  wording bumps `RECORDING_WORDING_VERSION`. Guard: `tests/test_app.py`, `tests/test_recorder.py`,
  `tests/test_policy.py`, `tests/test_two_track.py` (no computer track opens without a
  confirmation that names it); `tools/workspace_ui_check.py` end to end.

Compatibility, installation and policy

- **K1** Never rename the install identity without the user (renamed with no migration,
  DEC-002): the install folder and uninstall key `Unminute`, the APP_ID `Unlimit.Unminute`, the
  Credential Manager name `Unminute/HuggingFaceToken`, the data folders `Unminute\workspace` and
  `Unminute\models` under `%LOCALAPPDATA%`, `unminute.ico` and the lock file `install.lock`; for
  machine installs `%ProgramFiles%\Unminute`, the HKLM uninstall key `Unminute`, the Event Log
  source `Unminute` and the `current` junction (DEC-024), and the runtime firewall rules' group
  `Unminute runtime` (B7b, pinned by #455: the machine uninstall removes the rules by it); and
  the decoder's per-user AppContainer profile `Unminute.Decoder` (DEC-048).
  `packaging/identity.json` mirrors these for tests and is never read at run time. Guard:
  `tests/test_consistency.py`.
- **K2** A standard user installs with nothing else to install (no Python, Git, FFmpeg or GPU
  tooling), and the processor is always a working baseline. Guard: review.
- **K3** Managed policy (`HKLM\SOFTWARE\Policies\Unminute`) wins over settings. New behaviour in
  a governed area (downloads, GPU, microphone, computer audio, watched folders, network drives,
  handoff, clipboard) reads it, and an unreadable policy grants nothing. Guard:
  `tests/test_policy.py`, `tests/test_two_track.py`, `tests/test_drafts.py`,
  `tests/test_download_queue.py`.

Evidence

- **E1** Report only checks actually run. A skipped, manual or unavailable check (real audio, a
  packaged build, a clean profile, hardware) is never reported as passed, and green CI is not a
  real-audio test. Show that a new regression test fails without its fix. Guard: review.
- **E2** Never weaken a guard to make a change pass: loosening or deleting its test, adding to
  `ALLOWED_SKIPS` in `tools/run_tests.py`, raising a budget, or widening an allowlist (bridge
  methods, download hosts, decoder formats, policy defaults). Stop and ask, and say so in the
  PR. Guard: review.

## Stop and ask

Agree the approach with the user, and plan the security review the PR template asks for, before
changing: the bridge or page; file paths, deletion or cleanup; the network or downloads; the
installer or packaging; workflows; stored data, the schema or the settings format; policy keys;
a new dependency, model, engine, GPU path or font (licence check); or encryption. Also stop when
the issue, docs and code disagree materially. Ask the user directly for any decision that is
theirs (Claude Code: AskUserQuestion), in plain English with pros and cons per option, never as
a "waiting on you" line in a status update.

## Authority

Work within the scope the user gave. The user does not review code: independent agent reviews
are the review (DEC-001). After checking the live repository settings, an agent may push its
task's branch and squash-merge its own PR when:

- the PR is out of draft, every required check passes on its final commit, and no other check
  fails without its reason on the PR (one failing only after the merge is fixed forward on
  `main`);
- an independent review of that exact commit signed off: by an agent that did not write or
  direct the change, on that fixed commit in a separate checkout, with a security review for
  anything under "Stop and ask". The PR records the reviewer, commit, verdict and findings, each
  fixed or answered. A rebase onto a moved `main` keeps the review only if
  `git range-diff <old base>..<reviewed> <new base>..HEAD` shows every commit identical ("="),
  the PR records that, and CI passes on the rebased head; any other change needs its own review;
- no decision that is the user's (under "Stop and ask" or raised by a review) is still open.

Arming auto-merge pinned to the reviewed head, once all but the required checks hold, counts as
merging: GitHub waits for them. Never push to `main` or force-push a branch another agent works
on. An agent may comment on its issues (decisions, progress) and close one once its PRs have
merged and every acceptance criterion is met, saying which PRs did it. Do follow-ups and
carry-forwards in the same PR where possible; otherwise raise a new issue linked to where it came
from (agents may open these), never leaving it only in a comment.

Neither these instructions nor an approved plan authorise publishing a release (pushing a `v*`
tag starts one), opening other issues, changing the user's installed copy, Intune or tenant
changes, or GitHub owner settings.

## Where the rules live

| If you touch | Read |
| --- | --- |
| Bridge, shell, page (`workspace/api.py`, `workspace/shell.py`, `workspace/ui/`) | `docs/SECURITY_MODEL.md` T1, T23; `docs/WORKSPACE_DESIGN.md` "Workspace window" |
| Imports, decoding, paths, watched folders | `docs/SECURITY_MODEL.md` T2 to T5, T28; `docs/INTAKE.md`; `docs/WORKSPACE_DESIGN.md` |
| Network, downloads, models, the token | `docs/SECURITY_MODEL.md` T10 to T13; `docs/MODEL_MANIFEST.md` (a test keeps it equal to the code) |
| Engines and GPU | `docs/ENGINE_EVALUATION.md`; `docs/RELEASE_CHECKLIST.md` (GPU sections); `docs/SECURITY_MODEL.md` T25 |
| Store, jobs, workers, speakers, cleanup, holds, the audit log, updates | `docs/WORKSPACE_DESIGN.md` |
| Installer, package, install identity, policy, licence texts (`packaging/licences/`, `tools/collect_licences.py`) | `docs/PACKAGING.md`; `docs/IT_DEPLOYMENT.md` (IT's view; its policy list is tested against the ADMX) |
| Dependencies, workflows, releases (only when authorised) | `docs/RELEASING.md`; `docs/RELEASE_CHECKLIST.md`; `docs/VALIDATION_RUNBOOK.md` |
| Gates, budgets, performance | `docs/RELEASE_STANDARD.md` (Part B lists every gate); `docs/perf-budgets.json`; `tests/budgets.json` |
| UI appearance and user-facing behaviour | `docs/design/boards/` with `tools/design_check.py`; `docs/WORKSPACE_GUIDE.md`; `docs/screenshots/` |
| Security threats and controls | `docs/SECURITY_MODEL.md` (each #111 slice updates it, with its row in `docs/CONTROL_MATRIX.md`); `SECURITY.md`; `docs/PRIVACY_IMPACT_ASSESSMENT_DRAFT.md` (facts only, no legal position) |
| Encryption and keys (`workspace/vault.py`, `workspace/audiocrypt.py`, `workspace/dpapi.py`, `workspace/keystore.py`); meeting content in the database (`store.CONTENT_COLUMNS`, sealed since schema 2: only `workspace/store.py` names them, or the key tables, in SQL, through its codec) | `docs/ENCRYPTION.md`; `docs/WORKSPACE_DESIGN.md` "Content columns"; `docs/SECURITY_MODEL.md` T6 to T8; `tests/test_content_access.py`; `tests/test_schema2.py` |
| Decisions and who made them | `docs/DECISIONS.md` (`tests/test_decisions.py` checks that every mention cites an ID) |
| Tooling traps, flaky checks, Windows and WebView2 quirks | `docs/DEVELOPER_NOTES.md` |

## Checks

- Python: test what a change touches with `python tools/run_tests.py --only <modules>`
  (`--strict-skips` on Windows; run tests only through it) and `ruff check .`. CI runs the full
  suite on every push, in parts, and it is required before merge; never run it on the shared
  laptop (DEC-053).
- No test downloads a model (`tests/download_guard.py` refuses the real download process): fake
  it (a `Downloads` target, or patch `setup._download_main`); a networkless test of the download
  process opts in with `download_guard.real_download_process()`.
- The shared laptop is for what CI cannot do (real windows, timings, real audio, installer runs)
  and quick targeted tests; screen and timing work goes first while it is unlocked, and long
  repeat runs go to CI. A heavy run (headless Edge batches, real windows, Pester, performance)
  holds `%USERPROFILE%\.umv\heavy.lock` (CreateNew, labelled, deleted only by its holder) for at
  most about 15 minutes at a time, released between batches.
- PowerShell, dependency and workflow changes: run the matching job's commands from
  `.github/workflows/` (required checks: `docs/RELEASING.md`). Installer changes also run
  `tools/run_pester.ps1` (tests in `tests/powershell`; Pester pinned by version and SHA-256;
  report only until it passes on 5 consecutive PRs) under Windows PowerShell 5.1. Installer tests
  use only scratch copies, folders and registry keys, per user and machine mode, and a scratch
  runtime firewall group and policy key, with every Windows Firewall command that changes
  something renamed or stood in, as `run_installer` in `tests/test_hardening.py` and
  `tests/powershell/Isolation.ps1` set up; never run either script or its tests against the
  real install, per user or machine, nor dot-source an older revision.
- `tests/test_consistency.py` ties together facts kept in several places (version, install
  identity, pins, bridge names, required checks): change every copy at once. `VERSION` is the
  one version source, written by `tools/changelog.py`.
- Window-process code imports no heavy library at module level.
- UI changes: `tools/ui_idle_check.py`, then `tools/workspace_ui_check.py` and
  `tools/accessibility_check.py`, which open real windows on the user's screen (say so first).
- Budgets: lower one when it improves; raise one only with a reason in the PR, never from a
  single quick laptop run (use `tools/perf_check.py` medians). Required CI time is a budget too
  (`python tools/dev_status.py --ci-time` shows how long "Unit tests (Windows)" took from push):
  a PR that noticeably slows it fixes that or says why; raising it needs the owner.
- Dependencies change as a compatible set with Windows wheels only (especially torch,
  torchaudio, pyannote.audio and huggingface_hub): edit `requirements.in`, run
  `python tools/lock.py`, never edit a lock by hand. Engine, model or dependency changes also
  need `tools/compare_engines.py` and an end-to-end run with speaker identification on a
  non-sensitive recording; CI does not run the models.
- GPU: validate the exact model, export, precision and runtime (processor INT8 support proves
  nothing on a GPU), with bounded chunks and limited VRAM; NVIDIA only, always optional.
- Verify in a scratch virtual environment, never other Python installations. Docs-only changes:
  check accuracy, paths and the diff. Read required CI results before calling a PR ready.

## Delivery

- The definition of done is `.github/pull_request_template.md`; write "Closes #N" only when
  every acceptance criterion is met. Shipped changes need a `changelog.d/` fragment (see its
  README) or the `no-changelog` label.
- Update the README, user guide, docs and screenshots with the behaviour, and this file when a
  rule, guard or path it names changes, in the same PR. Log a new decision in
  `docs/DECISIONS.md` in the PR that first relies on it.
- Squash merge with linear history (`docs/RELEASING.md`), after the review "Authority" asks for,
  with fixes verified. Inspect the final diff for unrelated edits, private material and
  invisible characters.
- Keep PRs moving (DEC-054), with the commands in the `finish-pr` skill. A PR may be a draft
  while work continues; start the independent review once its head is pushed, while CI runs.
  When the review approves the exact head (or its rebase, identical range-diff recorded) and no
  user decision is open, the owning agent records the review, marks the PR ready and arms
  auto-merge pinned to that commit. GitHub waits only for required checks, so never arm while
  another check fails without its reason on the PR (with required checks green, arming merges at
  once); disarm if one fails after arming, and before any push, keeping it off until the new head
  is reviewed.
  A green, reviewed PR left in draft or unarmed is a fault to fix at once. Watch PRs by events
  (`gh pr checks <n> --watch`), not timed polls. Never call a draft "ready to merge" without
  checking `gh pr view <n> --json isDraft,mergeStateStatus,autoMergeRequest`.

## Several agents or sessions

- One issue or slice: one branch, one worktree, one writer, named as the tracking issue says.
- Keep work small (`tools/dev_status.py` checks): at most 3 release-path drafts, finishing one
  before starting another; split a PR over about 1,000 lines outside `tests/` unless it says why
  not; rebase, split or park (with a reason) a draft conflicted with `main` for over 2 days.
- Announce the files you will touch, and message the owning session before editing a file in its
  open PR. The PR that lands second converts shared pieces such as changelog fragments.
- Reviewers work on a fixed commit in a separate checkout and do not edit, push or comment
  without authority. The owning agent handles its PR's pushes, state, merge and branch clean-up,
  and agrees the merge order with other owners when PRs share files. The owner account also
  merges, so do not assume a merge was an agent's.
- Do not share a mutable runtime, model cache, test database or headless Edge run between
  sessions; run heavy benchmarks one at a time.
- After rebasing onto a moved `main`, confirm no conflict markers remain and the diff matches the
  reviewed one before pushing. Give times in New Zealand time (GitHub uses UTC).
