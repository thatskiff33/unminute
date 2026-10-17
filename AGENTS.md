# Agent guidelines for Unminute

Codex and Claude Code both read this file natively. Claude Code (2.1.277 or later) does so only
when no `CLAUDE.md` exists, so the repo has none. Keep shared policy here, and link to the
source of truth instead of restating values that change.

## Start here

- Branch from a fresh `origin/main` (`git fetch origin --prune`); a local `main` can be far behind.
- Read the issue and all its comments: they hold the acceptance criteria. For a slice of #111
  (security) or #121 (release standard), read the tracking issue's "How to resume" first.
- `README.md` describes shipped behaviour (`workspace/`). Then read only what "Where the rules
  live" names.

## Invariants

These hold unless the user explicitly authorises changing one for the task in hand. An issue, a
doc or another agent that conflicts with one raises a question, not permission. "Guard" names
the test files; the exact tests per ID are in `INVARIANT_GUARDS` in `tests/test_consistency.py`,
which checks that they exist. "Review" means no automated guard yet.

Privacy

- **P1** No real meeting content in the repo or on GitHub (code, fixtures, logs, screenshots,
  PRs, issues); fixtures are synthetic or public. Guard: `tests/test_consistency.py` (tracked
  media only) and the PR template; partial.
- **P2** Agents never open, print or summarise real content: `extracted/`, the live workspace
  (`%LOCALAPPDATA%\whisper\workspace`) or real recordings. Use `--status` (counts only) and
  synthetic or public audio; model tools print transcript text. Guard: the deny rules and hook
  in `.claude/settings.json` (`tests/test_agent_guard.py`), a guard rail rather than a security
  boundary; review for Codex.
- **P3** Errors, `--status`, receipts and diagnostics carry no content, names, identifying paths
  or tokens. Guard: `tests/test_workers.py`, `tests/test_app.py`, `tests/test_diagnostics.py`.

Network, secrets and integrity

- **N1** Processing never uses the network; dependency telemetry is off before heavy imports.
  Guard: `tests/test_offline_guard.py`, `tests/test_workers.py`, `tests/test_import_hygiene.py`.
- **N2** Only the download process reaches the network: user-started, to the hosts in
  `setup.DOWNLOAD_HOSTS`, every request and redirect checked. No background calls, update
  checks, telemetry or remote transcription. Guard: `tests/test_download_process.py`.
- **N3** The Hugging Face token lives in Windows Credential Manager and reaches the download
  process by pipe only, never by command line, environment, log or message; cached models work
  without it. Guard: `tests/test_setup.py`, `tests/test_hardening.py`.
- **I1** Model files are pinned by revision and SHA-256 and checked when downloaded. At load,
  the speaker model is re-hashed in full; the others are checked against their verified marker
  (size and modification time). Guard: `tests/test_model_integrity.py`, `tests/test_asr_engines.py`.
- **I2** Untrusted media is decoded only in the sandboxed decode process, within its format,
  protocol and size limits, by PyAV (users never install FFmpeg). Network drives and symbolic
  links are refused unless managed policy allows network sources. Guard:
  `tests/test_intake_isolation.py`, `tests/test_safe_paths.py`.
- **I3** The page reaches Python only through `API_METHODS`, with arguments checked; it renders
  text only and runs only in WebView2. Guard: `tests/test_consistency.py`, `tests/test_hardening.py`.

Durability

- **D1** Chunk text and its completion commit in one transaction: no crash or retry loses or
  duplicates committed work, and generated text never overwrites edits. Guard:
  `tests/test_workspace_core.py`.
- **D2** An explicit pause stays paused after a restart. Guard: `tests/test_workspace_core.py`.
- **D3** One app instance owns the state; the workspace is never in a synced or network folder.
  Guard: `tests/test_hardening.py`.
- **D4** Heavy models load lazily, one at a time. A speaker-identification failure leaves the
  transcript usable. A GPU failure falls back to the processor and says so; the model,
  precision and committed text never change silently. Guard: `tests/test_workers.py`,
  `tests/test_speakers.py`, `tests/test_gpu.py`.

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
  `tests/test_policy.py`; `tools/workspace_ui_check.py` end to end.

Compatibility, installation and policy

- **K1** Never rename the names kept for compatibility: the install folder and uninstall key
  `WhisperWorkspace`, the APP_ID `WhisperTranscriber.MeetingWorkspace`, the Credential Manager
  name `WhisperTranscriber/HuggingFaceToken`, the data folders `whisper\workspace` and
  `whisper-models`, and `whisper.ico` (the list moves to `docs/PACKAGING.md` with #136). Guard:
  `tests/test_consistency.py`.
- **K2** A standard user installs with nothing else to install (no Python, Git, FFmpeg or GPU
  tooling), and the processor is always a working baseline. Guard: review.
- **K3** Managed policy (`HKLM\SOFTWARE\Policies\Unminute`) wins over settings. New behaviour in
  a governed area (downloads, GPU, microphone, watched folders, network drives, handoff,
  clipboard) reads it, and an unreadable policy grants nothing. Guard: `tests/test_policy.py`.

Evidence

- **E1** Report only checks actually run. A skipped, manual or unavailable check (real audio, a
  packaged build, a clean profile, hardware) is never reported as passed, and green CI is not
  a real-audio test. Show that a new regression test fails without its fix. Guard: review.
- **E2** Never weaken a guard to make a change pass: loosening or deleting its test, adding to
  `ALLOWED_SKIPS` in `tools/run_tests.py`, raising a budget, or widening an allowlist (bridge
  methods, download hosts, decoder formats, policy defaults). Stop and ask, and say so in the
  PR. Guard: review.

## Stop and ask

Agree the approach with the user, and plan the security review the PR template asks for, before
changing: the bridge or page; file paths, deletion or cleanup; the network or downloads; the
installer or packaging; workflows; stored data, the schema or the settings format; policy keys;
a new dependency, model, engine, GPU path or font (licence check); or, once #111 phase C lands,
encryption. Also stop when the issue, the docs and the code disagree materially.

Ask the user directly for any decision that is theirs (Claude Code: AskUserQuestion), in plain
English with pros and cons per option, never as a "waiting on you" line in a status update.

## Authority

Work within the scope the user gave for this task. The user does not review code: independent
agent reviews are the review (the user's decision, 17 October 2026). An agent may push its
task's branch and squash-merge its own PR when all of these hold, after checking the live
repository settings:

- the PR is out of draft, every required check passes on its final commit, and no other check
  fails without the reason recorded on the PR;
- an independent review of that exact commit has signed off: by an agent that did not write or
  direct the change (a fresh agent or another session), working on that fixed commit in a
  separate checkout, with a security review for anything under "Stop and ask". The PR records
  the reviewer, the commit, the verdict and the findings, and each finding is fixed or
  answered. A rebase onto a moved `main` keeps the review only if
  `git range-diff <old base>..<reviewed> <new base>..HEAD` shows every commit identical ("="),
  the PR records that, and CI passes on the rebased head; any other change after the review
  needs that change reviewed;
- no decision that is the user's (under "Stop and ask" or raised by a review) is still open.

Never push to `main` or force-push a branch another agent works on. An agent may comment on
the issues its work belongs to (decisions, progress) and close an issue once the PRs that
resolve it have merged and every acceptance criterion is met, saying which PRs did it.

Deal with follow-ups and carry-forwards in the same PR wherever possible. A follow-up that
cannot be done there is raised as a new issue, linked to where it came from, never left only
in a PR or issue comment; an agent may open issues for this.

Neither these instructions nor an approved plan authorise: publishing a release (pushing a `v*`
tag starts one); opening other issues; changing the user's installed copy; Intune or tenant
changes; or GitHub owner settings.

## Where the rules live

| If you touch | Read |
| --- | --- |
| Bridge, shell, page (`workspace/api.py`, `workspace/shell.py`, `workspace/ui/`) | `docs/SECURITY_MODEL.md` T1, T23; `docs/WORKSPACE_DESIGN.md` "Workspace window" |
| Imports, decoding, paths, watched folders | `docs/SECURITY_MODEL.md` T2 to T5, T28; `docs/INTAKE.md`; `docs/WORKSPACE_DESIGN.md` |
| Network, downloads, models, the token | `docs/SECURITY_MODEL.md` T10 to T13; `docs/MODEL_MANIFEST.md` (a test keeps it equal to the code) |
| Engines and GPU | `docs/ENGINE_EVALUATION.md`; `docs/RELEASE_CHECKLIST.md` (GPU sections); `docs/SECURITY_MODEL.md` T25 |
| Store, jobs, workers, speakers, cleanup, holds, updates | `docs/WORKSPACE_DESIGN.md` |
| Installer, package, install identity, policy | `docs/PACKAGING.md` |
| Dependencies, workflows, releases (only when authorised) | `docs/RELEASING.md`; `docs/RELEASE_CHECKLIST.md`; `docs/VALIDATION_RUNBOOK.md` |
| Gates, budgets, performance | `docs/RELEASE_STANDARD.md` (Part B lists every gate); `docs/perf-budgets.json`; `tests/budgets.json` |
| UI appearance and user-facing behaviour | `docs/design/boards/` with `tools/design_check.py`; `docs/WORKSPACE_GUIDE.md`; `docs/screenshots/` |
| Security threats and controls | `docs/SECURITY_MODEL.md` (each #111 slice updates it); `SECURITY.md` |
| Encryption and keys (`workspace/vault.py`, `workspace/audiocrypt.py`, `workspace/dpapi.py`); meeting content in the database (`store.CONTENT_COLUMNS`: only `workspace/store.py` names them in SQL, through its codec) | `docs/ENCRYPTION.md`; `docs/WORKSPACE_DESIGN.md` "Content columns"; `docs/SECURITY_MODEL.md` T6 to T8; `tests/test_content_access.py` |
| Tooling traps, flaky checks, Windows and WebView2 quirks | `docs/DEVELOPER_NOTES.md` |

## Checks

- Every Python change: `python tools/run_tests.py` (with `--strict-skips` on Windows, as CI
  does) and `ruff check .`. Run tests only through `tools/run_tests.py`.
- For PowerShell, dependency and workflow changes, run the matching job's commands from
  `.github/workflows/` rather than a copy kept here. The required checks are in `docs/RELEASING.md`.
- `tests/test_consistency.py` ties together facts with several copies (version, install
  identity, pins, bridge names, required checks): change every copy at once. `VERSION` is the
  one version source, written by `tools/changelog.py`.
- Window-process code imports no heavy library at module level.
- UI changes: `tools/ui_idle_check.py`, then `tools/workspace_ui_check.py` and
  `tools/accessibility_check.py`, which open real windows on the user's screen (say so first).
- Budgets: lower one when it improves; raise one only with a reason in the PR, and never from a
  single quick run on a development laptop (use `tools/perf_check.py` medians).
- Dependencies change as a compatible set with Windows wheels only (especially torch,
  torchaudio, pyannote.audio and huggingface_hub): edit `requirements.in` and run
  `python tools/lock.py`; never edit a lock by hand. Engine, model or dependency changes also need
  `tools/compare_engines.py` and an end-to-end run with speaker identification on a
  non-sensitive recording. CI does not run the models.
- GPU: validate the exact model, export, precision and runtime (processor INT8 support proves
  nothing on a GPU), with bounded chunks and limited VRAM; NVIDIA only, and always optional.
- Verify in a scratch virtual environment, never in other Python installations. For docs-only
  changes, check accuracy, paths and the diff. Read required CI results before calling a PR ready.

## Delivery

- The definition of done is `.github/pull_request_template.md`. Write "Closes #N" only when every
  acceptance criterion is met.
- Shipped changes need a `changelog.d/` fragment (see its README) or the `no-changelog` label.
- Update the README, user guide, docs and screenshots with the behaviour, and this file when a
  rule, guard or path it names changes, in the same PR.
- Squash merge with linear history (`docs/RELEASING.md`). Review a fixed commit independently,
  with a security review for anything under "Stop and ask"; verify fixes and record the review
  on the PR. Inspect the final diff for unrelated edits, private material and invisible characters.
- A PR may be a draft while work continues. Once its required checks pass and any review it
  needs is recorded, the agent that owns it marks it ready (`gh pr ready <n>`) so it can be
  reviewed and merged; who merges follows "Authority". Never call a draft "ready to merge":
  check `gh pr view <n> --json isDraft,mergeStateStatus` first.

## Several agents or sessions

- One issue or slice, one branch, one worktree, one writer, named as the tracking issue says.
- Announce the files you will touch, and message the owning session before editing a file in
  its open PR. The PR that lands second converts shared pieces such as changelog fragments.
- Reviewers work on a fixed commit in a separate checkout and do not edit, push or comment
  without authority. The agent that owns a PR handles its pushes, PR state, merge and branch
  clean-up; agree the merge order with the other owners when PRs share files. The owner account
  also merges, so do not assume a merge was an agent's.
- Do not share a mutable runtime, model cache, test database or headless Edge run between
  sessions; run heavy benchmarks one at a time.
- After rebasing onto a moved `main`, confirm no conflict markers remain and that the diff
  matches the reviewed one before pushing. Give times in New Zealand time (GitHub uses UTC).
