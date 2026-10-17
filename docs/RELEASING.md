# Releasing and repository settings

This page covers the repository settings, how a release is cut and published, and how a bad release is withdrawn.

- **What a release must meet:** [RELEASE_STANDARD.md](RELEASE_STANDARD.md).
- **The evidence recorded so far:** [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).
- **Authorisation:** publishing a release needs its own approval, separate from merging changes.

## Protecting main

These settings cannot be applied from a script that only has push access, so set them once in the GitHub UI.

1. Open https://github.com/thatskiff33/unminute/settings/rules and click New ruleset, then New branch ruleset.
2. Name it `Protect main`. Set Enforcement status to Active.
3. Under Target branches, click Add target and choose Include default branch.
4. Tick these rules:
   - Restrict deletions
   - Block force pushes
   - Require linear history
   - Require a pull request before merging. Leave Required approvals at 0 (a solo maintainer cannot approve their own pull request), tick Dismiss stale pull request approvals when new commits are pushed, and tick Require conversation resolution before merging.
   - Require status checks to pass. Add every job from `.github/workflows/ci.yml`: `Syntax check`, `Unit tests (Windows)`, `Requirements resolve (wheels only)`, `Ruff`, `PSScriptAnalyzer` and `Workflow lint`. They only appear in the picker after the CI workflow has run once, so add them after the first pull request goes green.
5. Leave Bypass list empty. Admins are then held to the same rules, which is the point.
6. Click Create.

From then on, all changes reach `main` through a pull request, and nobody can force-push or delete the branch.

Newer checks start outside that list:
- **Where they run:** `.github/workflows/quality.yml` (UI idle, JavaScript syntax, coverage), `.github/workflows/changelog.yml` (changelog entry) and `.github/workflows/audit.yml` (dependency audit).
- **How they become required:** after 10 consecutive green runs.
  - A `quality.yml` job moves into `ci.yml` and joins the list above.
  - The changelog job stays in `changelog.yml`, because that workflow re-runs when labels change. It joins the list above, and `tests/test_consistency.py` is extended to include `changelog.yml`'s job, since it takes the required set from `ci.yml` today.
  - The dependency audit stays in `audit.yml`, because it also runs daily, and joins the list the same way.
  - Adding a check to the ruleset needs the owner account.
- **The changelog check:** it looks for a new or changed fragment in `changelog.d/` whenever a pull request changes what is shipped. An edit to `CHANGELOG.md` does not count. Add the `no-changelog` label to a pull request whose change is not worth a release note.

Already enabled at repository level (Settings > Advanced Security): Dependabot alerts, Dependabot security updates, secret scanning and push protection. Routine version bumps in `.github/dependabot.yml` cover GitHub Actions only (monthly, grouped, a 7-day cooldown); Python dependencies are re-locked instead (see "Workflow supply-chain rules"). CodeQL analyses the Python code, the JavaScript and the workflows from `.github/workflows/codeql.yml`.

## Protecting releases

The Release workflow relies on these owner settings. They were read back on 17 Oct 2026:

- **Tag ruleset** "Protect release tags": active. It stops a `v*` tag being created, moved or deleted by anyone but its bypass list. This is the control that keeps a collaborator from releasing: with it, only the owner can start a release. Confirm that the bypass list is the owner alone. Tags are matched by pattern, and whether that ignores case is not documented, so add `refs/tags/V*` too: IT's script refuses such a tag, but the workflow's own trigger may not.
- **`release` environment:** the repository owner is the required reviewer, and only `v*` tags can deploy to it. The publish job waits here for approval. Administrators can bypass it (GitHub's default): turn that off after the move to the organisation.
- **The owner's sign-in on machines where agents run.** An agent, or malware running as the user, could use a signed-in owner account to create release tags and approve the deployment. Recommended: sign the owner out of `gh` and Git Credential Manager on those machines, and tag and approve on GitHub's website with two-factor sign-in.
- **Releases before v0.5.0** are not immutable, and their tags' `release.yml` still has a manual "Run workflow" route (with write access only; it has no signing rights).
- **Immutable releases** (Settings > General > Releases): turned on by the owner on 16 Oct 2026. This could not be read back with a collaborator's access, so confirm it before the first signed release. Once on, a published release's files and tag cannot change.
- **Rehearse the workflow first.** The hosted-runner build, the attestations, the draft and the publish job have not run yet. Before the first real release, run them end to end on a test tag in a scratch copy of the repository.
- **Moving the repository** to the Unlimit organisation changes the identity the attestations name. Do it before the first signed release, then change the default `-Repository` in `tools/intune/Get-VerifiedRelease.ps1`; a test fails in CI until they agree.

## Workflow supply-chain rules

The workflows run with the repository's token, so they are held to these rules. On every pull request the `Workflow lint` job (zizmor) flags unpinned actions, `${{ }}` expressions inside `run:` blocks, a missing default `permissions` block and checkouts that keep credentials. Asking for more permissions than a job needs, and the tool pinning, are checked in review.

- No permissions by default (`permissions: {}`); each job asks only for what it needs.
- Every action is pinned to a full commit SHA, with its version in a comment. To update one, resolve the new tag to its commit (`gh api repos/<owner>/<action>/git/ref/tags/<tag>`, and follow an annotated tag to its commit) and update both.
- Values from the event (tag names, inputs) reach scripts through environment variables, never through `${{ }}` inside a `run:` block.
- Checkouts use `persist-credentials: false`.
- CI tools come from `requirements/ci.txt` (and coverage from `requirements/ci-coverage.txt`, vulture from `requirements/ci-vulture.txt`, uv from `requirements/ci-lock.txt`, pip-audit from `requirements/ci-audit.txt`), installed with `--require-hashes`. All are generated by `tools/lock.py` from their `.in` files. PSScriptAnalyzer is pinned to one version and must carry a valid Microsoft signature.
- **The app's dependencies are hash-locked (#111 B1).** `requirements.in` lists the direct ones. `tools/lock.py` (uv, at the version pinned in `requirements/ci-lock.in`) writes `requirements.txt` with every package and its SHA-256 hashes, for Windows x64 and CPython 3.14, and the CI tool locks the same way. It keeps the versions already locked unless asked, refuses to move a package to an older version, only chooses versions public for 7 days (the cut-off time, in UTC, is recorded in each lock), and always takes hashes afresh from PyPI. Never edit a lock by hand: CI's `tools/lock.py --check` resolves each input again to the locked versions, with fresh hashes, and fails if a hash or a line differs. It takes the locked versions as given while they still resolve, so on a pull request `tools/lock.py --compare` also lists every version change against the base branch, for review, and fails on a downgrade the `.in` file does not pin. The package build installs only the lock, with `--require-hashes --no-deps`.
- **Monthly re-lock** (in place of Dependabot's routine Python updates; by an agent or by hand, with no automatic reminder): `python tools/lock.py --outdated` lists newer versions past the cooldown. Change a direct dependency's pin in `requirements.in` (most are pinned with `==`), or move an indirect one with `python tools/lock.py --upgrade-package <name>`, then run the usual checks and open one pull request. The torch family (torch, torchaudio, torchcodec, pyannote.audio) moves only as a set, with an end-to-end run on a real recording. Dependabot still raises security alerts and updates GitHub Actions.
- **A security fix inside the cooldown:** `python tools/lock.py --allow-newer <name>` (after changing the pin in `requirements.in`, for a direct dependency) moves that package to its newest version and records a dated exception in the lock, which lapses once the cooldown covers that version. `--check` lists every active exception.
- **A rollback:** pin the older version with `==` in the `.in` file; a re-lock otherwise refuses to move any package to an older version.
- **A Dependabot security pull request** for a Python package edits `requirements.txt` only, so it fails the lock check: treat it as an alert and re-lock instead.
- **Vulnerability audit.** `tools/audit.py` runs pip-audit over every lock on every pull request and daily (`.github/workflows/audit.yml`). A finding that cannot be fixed yet may be ignored in `requirements/audit-ignore.json`, with the package, a reason and an `until` date at most 90 days ahead; an expired entry fails the audit.
- A new version of an action or tool is adopted only after it has been public for at least 7 days, so a compromised or broken release has time to be found.

## Before cutting a release

CI proves the code parses, the unit tests pass and the pins resolve. It does not run the models or the packaged app.

1. Check the release against [RELEASE_STANDARD.md](RELEASE_STANDARD.md). Part B lists each gate, where it runs and whether it is automated yet.
2. The package is tested from its draft release, before anything is public ("Cutting a release", step 3).

## Cutting a release

The Release workflow (`.github/workflows/release.yml`, #111 B3) builds, signs and publishes a release when a version tag is pushed. There is no manual "Run workflow" route: the tag is the single record of what was released. The workflow publishes only after the repository owner approves it. That approval gates the workflow, not GitHub itself: anyone with write access can still edit or publish a draft by hand. So the publish job checks the published files afterwards, and IT verifies every download with `tools/intune/Get-VerifiedRelease.ps1`.

1. **Changelog and version.** On a branch, run `python tools/changelog.py vX.Y.Z` (for a preview, `vX.Y.Z-preview.N`).
   - It moves the fragments in `changelog.d/` into a new `## vX.Y.Z (YYYY-MM-DD)` section at the top of `CHANGELOG.md`, sets `VERSION` to `X.Y.Z` (or `X.Y.Z-preview.N`), and deletes the fragments.
   - `--summary "One line"` adds a line under the heading. `--date` sets the date if it should not be today.
   - Review both files, then merge them to `main`. `VERSION` is the one source of the version: the package build reads it, and the workflow refuses a tag that differs.
   - **Promoting a preview to a full release:** run `python tools/changelog.py vX.Y.Z` as usual. The full section brings in the notes of every `## vX.Y.Z-preview.N` section (oldest first, each group's notes kept together) and any fragments added since, so users on Latest see everything since the last full release, as long as the previews were numbered `vX.Y.Z-preview.N` (notes from an abandoned preview of another version are not brought in: add a fragment if they still apply). The preview sections stay as they were; leave them as the script wrote them, and put install notes in `--summary`. A preview's summary is not carried over; give the full release its own. `python tools/changelog.py --preview vX.Y.Z` shows the notes first.
2. **Tag the merged commit and push the tag.** The owner does this: the tag ruleset lets only its bypass list create release tags, and publishing a release needs its own approval (AGENTS.md).
   - **Before tagging, the owner reviews what changed in the release machinery since the last release.** An attestation proves where and from which commit a package was built, not that the code is benign, and agents merge their own pull requests:

     ```
     git diff <last release tag>..<commit> -- .github/workflows tools/build_package.py tools/evidence.py packaging requirements.txt requirements
     ```


   ```
   git tag -a vX.Y.Z -m "Unminute vX.Y.Z" <commit on main>
   git push origin vX.Y.Z
   ```

   The workflow then runs four jobs:
   - **Check** (Linux): the tag is `vX.Y.Z` or `vX.Y.Z-preview.N`, points at a commit on `main` and matches `VERSION`; the notes come from the dated changelog section; every lock passes the vulnerability audit at that commit.
   - **Build** (a GitHub-hosted Windows runner, nothing cached): `python -I tools/build_package.py --release` with the pinned Python (`PYTHON_VERSION` in `tools/build_package.py`). It checks that the zip's `version.json` names the tag's version and commit, runs the packaged `--self-test`, and checks that every shipped `.pyc` is what its source compiles to (`--verify-precompiled`). Then it signs build provenance and an SBOM attestation for the zip (Sigstore, `actions/attest`).
   - **Draft** (Linux): a draft release, visible only to people with write access, holding the zip, its `.sha256`, the SBOM (`.cdx.json`) and the attestation bundle (`.attestations.jsonl`).
   - **Publish** (the protected `release` environment): waits for the owner's approval (step 4).

   If the check or the build fails, nothing is drafted. A version number is never reused, and the tag ruleset stops a release tag being moved or deleted: fix the cause and release the next patch version.
3. **Test the draft and attach the evidence.**
   - **Download and verify it.** In a new, empty folder, run `gh release download vX.Y.Z --repo <owner>/unminute --pattern "Unminute-X.Y.Z.*"`. Then run `gh attestation verify Unminute-X.Y.Z.zip --repo <owner>/unminute --bundle Unminute-X.Y.Z.attestations.jsonl --cert-identity https://github.com/<owner>/unminute/.github/workflows/release.yml@refs/tags/vX.Y.Z --source-digest <the tag's commit> --deny-self-hosted-runners`. `<owner>/unminute` is the repository, as in `tools/intune/Get-VerifiedRelease.ps1`, which checks published releases only.
   - **Rebuild it (recommended).** From a clean checkout of the tag, `python -I tools/build_package.py --release` should give the same SHA-256 ("Building the package locally" below), with the same Python, pip and zlib versions as the draft's SBOM records. Until #180 pins pip in the Release workflow, install that pip version first.
   - **Install it** with `install.ps1 -Package <folder>\Unminute-X.Y.Z.zip`, with its `.sha256` beside it. Run `runtime\python.exe -I -B -m workspace --self-test` from the installed version's `app` folder. Expect PASS.
   - **Release evidence** (docs/RELEASE_STANDARD.md, section 8). Run the five checks against the installed package from a clean checkout of the tagged commit, with the installed version's own Python. Run `git checkout vX.Y.Z`, then `git clean -ndX -- tools tests docs` to see which ignored files (such as `__pycache__`) would go, and `git clean -fdX -- tools tests docs` to remove them. It deletes them for good, so move anything else first. Clean means no change, no extra or ignored file, and nothing hidden from `git status` under `tools`, `tests` or `docs`. Start every check with `-B` so it writes no bytecode there. `<v>` is the installed version folder (`%LOCALAPPDATA%\Programs\WhisperWorkspace\versions\X.Y.Z`). Use a copy of the models, never the live workspace, and a non-sensitive recording, such as a public meeting. The window checks need an unlocked screen.
     - **What each report records** (`tools/evidence.py`): the zip given with `--zip`; the checkout's commit, and whether it was clean; and whether the version folder held exactly that zip's files before and after the run. Names are compared exactly, and nothing may be missing, changed or added, with no link or extra folder.
     - **What stops a check before it starts:** a `--zip` that is not a package, a version folder that already differs from it, or a checkout that is not clean. The UI, accessibility and performance checks also stop under any other Python, without the app's flags (`-I -B -X disable-remote-debug`), or with app code from outside `<v>`.
     - **Exit code:** with `--zip`, a check exits 1 unless its evidence passes.

     ```
     <v>\runtime\python.exe -I -B -X disable-remote-debug tools\perf_check.py evidence-perf.json --app <v>\app --zip <folder>\Unminute-X.Y.Z.zip --leak-minutes 30
     <v>\runtime\python.exe -B tools\packaged_check.py <v> <model cache> <recording.wav> evidence-packaged.json --zip <folder>\Unminute-X.Y.Z.zip
     <v>\runtime\python.exe -B tools\packaged_crash_check.py <v> <model cache> <recording.wav> evidence-crash.json --zip <folder>\Unminute-X.Y.Z.zip
     <v>\runtime\python.exe -I -B -X disable-remote-debug tools\workspace_ui_check.py evidence-ui.json --app <v>\app --zip <folder>\Unminute-X.Y.Z.zip
     <v>\runtime\python.exe -I -B -X disable-remote-debug tools\accessibility_check.py evidence-accessibility.json --app <v>\app --zip <folder>\Unminute-X.Y.Z.zip
     python -I tools\evidence.py check <the zip's SHA-256> --commit <the tag's commit> evidence-perf.json evidence-packaged.json evidence-crash.json evidence-ui.json evidence-accessibility.json
     gh release upload vX.Y.Z evidence-perf.json evidence-packaged.json evidence-crash.json evidence-ui.json evidence-accessibility.json
     ```

     - The performance check measures against `docs/perf-budgets.json`. Leave the laptop idle, on mains power and the Balanced power plan, with the window visible for about 90 minutes. It counts as evidence only as a full release run within budget: at least 3 runs, 60 s settle and 300 s windows (the defaults), with the leak check (`--leak-minutes 30`), not `--quick`.
     - The UI and accessibility checks open a window for about a minute each, and `packaged_check` opens one at the end. Do not copy anything during the crash check.
     - Each check prints `EVIDENCE <kind>: PASS`. `tools/evidence.py check` prints `EVIDENCE COMPLETE` only when there is one passing report of each kind that names that SHA-256 and commit. Each report must also come from a release build, be made by a clean checkout of that commit, and have run on the unchanged package. Anything else is a failed gate: fix it and run the check again. Never edit a report. A failing report may hold error text with local paths, so do not attach one.
   - Run one real, non-sensitive recording through the installed package with speaker identification on, and check the transcript looks right.
   - If a previous version is installed, check that the update keeps the workspace.
   - Work through docs/RELEASE_CHECKLIST.md. For a full release, decide explicitly on each open item in "Before a business release": either verified, or listed as a known limitation in the notes. The notes come from the changelog at the tag, so put known limitations there before tagging (`--summary`), or edit the draft's notes before approving.
   - Attach only the five `evidence-*.json` reports: the publish job refuses a draft holding any other file.
4. **Approve.** When the five reports are attached, the owner opens the workflow run on GitHub, chooses Review deployments and approves `release`. Approve only after the evidence is attached. The publish job then checks:
   - the draft holds exactly the zip, its `.sha256`, the SBOM, the attestation bundle and the five evidence reports, with nothing missing or added;
   - the zip and the SBOM are the ones the build job made;
   - both attestations verify against this repository's Release workflow, the tag and the tag's commit, on a GitHub-hosted runner;
   - the evidence is complete and names that zip and commit (`tools/evidence.py check --commit`).

   Then it publishes, and checks that the published release holds exactly the files it checked, by name and by the digest GitHub records for each. It warns if the release is not immutable. With immutable releases on, the files and the tag can no longer change. If a check fails before publishing, fix the cause (for example, attach a missing report) and re-run the failed job, which asks for approval again. If the check after publishing says the files differ, someone changed the draft's files: withdraw the release ("Withdrawing a bad release") and release the next patch version. If it says it could not read them, compare by hand: `gh api repos/<owner>/unminute/releases/tags/vX.Y.Z --jq '.assets[] | "\(.name) \(.digest)"'` against the SHA-256 of the files the job checked, which are the draft's files you downloaded.
5. **Release type.** A `vX.Y.Z` tag publishes a full release, which becomes Latest, the default download. A `vX.Y.Z-preview.N` tag publishes a pre-release that does not.

## Building the package locally

The Release workflow builds every published package. The same command builds it locally, to check a release or for development:

- `python -I tools/build_package.py --release` on Windows with 64-bit CPython 3.14 (without `-I` it refuses to run). It refuses a working tree with any change, untracked file or file hidden from `git status`, and a HEAD that is not the commit of tag `vX.Y.Z`. It packages that commit's files as git stores them, never the working folder, and takes the version from `VERSION` (a `--version` that differs is refused; see PACKAGING.md).
- The command prints the zip's SHA-256 and writes the `.sha256` file and the bill of materials (`Unminute-X.Y.Z.cdx.json`) to `dist/`. The zip's `version.json` says `"release": true`; a build without `--release` says `false` and is never published.
- The build is reproducible: building the same commit again with the same Python, pip and zlib versions (listed in the bill of materials) gives the same SHA-256.
- `python -I tools/build_package.py --verify-precompiled <zip>` checks any package zip, with this machine's CPython of the package's version (not the package's own runtime). Every `.pyc` the package ships outside its standard library must be what its `.py` beside it compiles to, and a `.pyc` without its source is refused. The standard library inside `python3XX.zip` is not checked here: the build checks the official runtime against its pinned SHA-256, and for a downloaded zip the attestation covers it.

## Withdrawing a bad release

**When to withdraw.** Withdraw a release that has a severity 1 issue (defined in [RELEASE_STANDARD.md](RELEASE_STANDARD.md), section 14). Anything less serious is fixed forward in the next patch.

1. **Stop new downloads.**
   - Make the previous good release the default download: `gh release edit vPREVIOUS --latest`.
   - Add a warning at the top of the bad release's notes, saying what is wrong and which version to use instead.
   - Do not delete the release or its tag. A version number is never reused, and deleting it loses the record of what was shipped.
   - Once immutable releases are turned on (#111), the files cannot change. Whether the notes can still be edited is to confirm at that point.
2. **Managed devices.** Once the per-machine Intune installer exists (#111, slice B6): in Intune, supersede or unassign the bad version and assign the previous one. Until then, installs are per user, and step 3 applies.
3. **People who installed it.**
   - Ask them to roll back, as described in [PACKAGING.md](PACKAGING.md): from `%LOCALAPPDATA%\Programs\WhisperWorkspace\versions\<version>` (the bad version), run `powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Rollback`.
   - Rollback is refused if the bad version changed the storage format. In that case, fix forward instead.
4. **Fix forward.**
   - Release the next patch version through the normal process.
   - Its changelog entry says what was wrong and who was affected, without meeting content.
5. **Record it.**
   - Open an issue with the timeline, the cause, why the gates did not catch it, and the gate added so it cannot happen again.
   - Include no meeting content.

## Why there is no PyPI package

v0.1.0 shipped as source only. From v0.2.0 the release is a Windows package: a zip with a private Python runtime, installed by its `install.cmd` (see [PACKAGING.md](PACKAGING.md)). There is still no PyPI package, because:

- **The audience** is non-technical staff on managed Windows laptops. They run an installer, and a `pip install` step would be a regression for them.
- **The dependencies.** `requirements.txt` is a full hash-locked set, including torch, CTranslate2 and pyannote.audio. It was chosen because specific combinations break on CPU-only Windows. A PyPI package would need loose version ranges or a hard pin on torch, and neither works well in someone else's environment.
- **The package does what a PyPI package cannot:** it carries its own runtime, checks the machine, keeps the previous version for rollback, and holds back storage-format updates until the workspace is empty.
