# Agent guidelines for Unminute (formerly Whisper Transcriber)

## Read first
- Read `README.md` for currently shipped behaviour and `docs/WORKSPACE_PLAN.md` for the target product. Read `docs/IMPLEMENTATION_PLAN.md` for sequencing and the current issue plus its comments for acceptance criteria.
- Roadmap #7 and its child issues describe work to implement, not features already shipped. Preserve the existing app until a tested replacement is ready; its tkinter UI, Whisper-only engine and source installer are not permanent constraints on the approved migration.
- Read `docs/RELEASING.md` for dependency, repository-policy or release work.
- Use `.github/workflows/ci.yml` and `ruff.toml` as the source of truth for automated checks. Read the relevant implementation before changing it.

## Privacy and offline operation
- Keep recordings and transcripts local. Never commit client audio, transcript contents, tokens or other private data, including files under `extracted/`. Use non-sensitive fixtures for testing.
- Preserve offline-by-default transcription. Set offline/cache configuration and disable dependency telemetry before imports. Separate download workers from offline inference in the new architecture; do not toggle process-global network flags across concurrent threads. For legacy fixes, contain deliberate downloads in `allow_network()` and restore state even on failure.
- Preserve the documented installer and deliberate download/update network behaviour. Do not introduce background network calls, telemetry or remote transcription.
- Do not log or expose tokens. Preserve existing gated-model download support; do not set `HF_HUB_DISABLE_IMPLICIT_TOKEN` in the legacy path. New onboarding stores remembered tokens in Windows Credential Manager, supports removal and must process cached models without a live token.

## Windows installation and dependencies
- Preserve the existing CPython 3.12 source-install workflow while migrating. The target is a tested private Windows runtime/package for both self-service and managed deployment; users must not manually install Python, Git, FFmpeg or GPU tooling. Retain a working CPU baseline and standard-user installation.
- Keep `requirements.txt` fully pinned and installable with Windows wheels only. Validate dependency changes as a compatible set, especially torch, torchaudio, pyannote.audio and huggingface_hub.
- Keep the managed environment and model caches in their existing local, non-OneDrive locations. Do not modify unrelated Python installations or the user's environment as a side effect of verification.
- Preserve decoding through PyAV and the decoded waveform input to pyannote; do not add a requirement for separately installed FFmpeg DLLs. Preserve handling of pyannote's `speaker_diarization` result wrapper.

## Architecture and behaviour to preserve
- Keep inference independent of the UI. Legacy tkinter updates stay on its owning thread. Prove the selected pywebview bridge, bundled assets, bounded playback and packaging before replacing the shell.
- Use one state-owning process, SQLite outside synced folders and managed heavy workers in the target architecture. Commit stable chunk output and completion atomically; keep edits separate. Explicit pauses remain paused and retries must not duplicate committed text.
- Preserve queue locking and pause/stop coordination in legacy fixes. Target jobs persist their model, configuration and chunk revisions; changed sources/models create a separate working version.
- In legacy fixes, preserve collision-safe filenames, partial transcript saving on Stop, remaining queued files and continuation after an individual file fails. The target replaces partial-export recovery with durable chunks and separate edits; legacy partial exports are not inherently resumable. Do not overwrite existing outputs during migration.
- Keep heavy models lazy-loaded. The target runs one meeting and one heavy model at a time: transcribe, unload ASR, then run pyannote. Speaker failure preserves usable text. Retain optional speaker setup and cached processing without a live token.

## GPU and resource policy
- GPU acceleration is an optional, measured Faster-mode capability, not a first-release dependency. Evaluate NVIDIA first through #9/#12 across the planned Parakeet adapter, advanced Whisper alternative and separate pyannote phase. Do not implement a standalone legacy GPU UI or promise support from GPU detection alone.
- Keep Quiet defaults and battery rules from #15. Validate exact model/export/precision/runtime combinations; CPU INT8 support does not prove GPU compatibility. No manual CUDA setup for users.
- Use bounded chunks and test limited VRAM. On a recoverable GPU failure, stop/release the failed worker and retry only uncommitted work on a compatible CPU path. Never silently change model/precision contracts, erase edits or resume after authorised audio deletion. Report actual device use and fallback honestly.

## Temporary workspace and cleanup
- Follow the two explicit cleanup confirmations in `docs/WORKSPACE_PLAN.md`: reviewed transcript saved before authorised audio deletion; final external deliverable reviewed/saved before transcript cleanup. Export alone never authorises deletion.
- Honour reasoned retention holds (#18), source identity/deletion authority and durable cleanup retries. Release worker file handles before cleanup; do not recreate deleted content through recovery or GPU fallback. Preserve all legacy outputs/settings/models without retroactive deletion.
- Schema-changing updates require an empty workspace with no held content or pending cleanup (#26). Never expire holds or create content-bearing backups to make an update proceed. Configuration and minimal completed receipts alone do not block migration.

## Validation
- Scale validation to changed behaviour. For documentation-only changes, check accuracy, referenced paths and the diff; model execution is unnecessary.
- For Python changes, compile the tracked Python sources and run `ruff check .` using the project Python environment. In PowerShell, use `git ls-files '*.py' | ForEach-Object { python -m py_compile $_; if ($LASTEXITCODE -ne 0) { throw "Syntax check failed: $_" } }`; this avoids traversing private output folders or virtual environments.
- For installer changes, run the PowerShell parser and PSScriptAnalyzer with the same severity and exclusions as `.github/workflows/ci.yml`.
- For dependency changes, verify strict pins and run the CI-equivalent Windows Python 3.12 resolution check: `python -m pip install --dry-run --ignore-installed --only-binary=:all: --disable-pip-version-check -r requirements.txt`.
- For changes to transcription, diarisation, output handling or queue controls, verify the affected behaviour with a non-sensitive recording and relevant UI checks. Dependency upgrades also require the end-to-end speaker-identification check described in the README.
- `test_diarization.py` is a manual integration script: run `python test_diarization.py "path/to/non-sensitive-recording.wav"` with the installed dependencies and model access. It may download missing models and prints transcript text; it is not a conventional unit-test suite or proof that offline mode works.
- Current CI checks syntax, model-free unit tests, lint and dependency resolution; it does not execute the models. Before a source-app release, run a real recording with speaker identification using the pins and verify cached offline transcription. Before a new workspace release, run the corresponding shipped-engine, packaged workflow and lifecycle checks in the issue acceptance criteria; the legacy script alone is insufficient.
- Report checks actually run and their results. If models, fixtures or Windows tooling are unavailable, state precisely what remains unverified; never equate green CI with a successful real-audio test.
- Add meaningful recovery, cleanup, adapter and packaged-runtime tests with each new subsystem. Existing CI is the legacy baseline, not sufficient evidence for the new architecture. Publish only synthetic fixtures and anonymous aggregate evaluation results.

## Documentation, review and delivery
- Update relevant README guidance and documentation alongside behaviour or setup changes; add changelog entries for release-worthy changes. Keep screenshots current when changed UI makes them misleading.
- Review substantive changes for concrete regressions, verify fixes, and inspect the final diff for unrelated edits, private material and generated noise. Scale review to risk; routine work does not require multiple agents or fixed reviewer counts.
- Follow the PR and linear-history policy documented in `docs/RELEASING.md`; do not import a merge-commit-only policy. Check actual repository settings before an authorised merge rather than assuming the setup guide proves enforcement.
- Keep PR validation evidence and remaining limitations clear. When PR delivery is in scope, inspect required CI results and resolve attributable failures before declaring the change ready.
- Release publication is separate from completing code. Follow `docs/RELEASING.md` and `.github/workflows/release.yml` only when release publication is authorised: pushing a `v*` tag publishes a release.
- These instructions do not grant automatic authority to push, merge, publish, comment on or close issues. Use the scope and authority established by the user for the task.
- For authorised multi-CLI work, follow `docs/IMPLEMENTATION_PLAN.md`: one writer per issue/worktree, independent review of a fixed commit, one integration owner and serial GPU benchmarks. Do not let concurrent agents change a shared runtime, model cache or working database. Claude Code imports this file through `CLAUDE.md`; keep project policy here rather than duplicating it.
