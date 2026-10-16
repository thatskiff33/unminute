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
- **Where they run:** `.github/workflows/quality.yml` (UI idle, JavaScript syntax, coverage) and `.github/workflows/changelog.yml` (changelog entry).
- **How they become required:** after 10 consecutive green runs.
  - A `quality.yml` job moves into `ci.yml` and joins the list above.
  - The changelog job stays in `changelog.yml`, because that workflow re-runs when labels change. It joins the list above, and `tests/test_consistency.py` is extended to include `changelog.yml`'s job, since it takes the required set from `ci.yml` today.
  - Adding a check to the ruleset needs the owner account.
- **The changelog check:** it looks for a new or changed fragment in `changelog.d/` whenever a pull request changes what is shipped. An edit to `CHANGELOG.md` does not count. Add the `no-changelog` label to a pull request whose change is not worth a release note.

Already enabled at repository level (Settings > Advanced Security): Dependabot alerts, Dependabot security updates, secret scanning and push protection. Routine version bumps are configured in `.github/dependabot.yml` (monthly, grouped, both requirement files, a 7-day cooldown; torch, torchaudio and torchcodec limited to patch releases). CodeQL analyses the Python code, the JavaScript and the workflows from `.github/workflows/codeql.yml`.

## Workflow supply-chain rules

The workflows run with the repository's token, so they are held to these rules. On every pull request the `Workflow lint` job (zizmor) flags unpinned actions, `${{ }}` expressions inside `run:` blocks, a missing default `permissions` block and checkouts that keep credentials. Asking for more permissions than a job needs, and the tool pinning, are checked in review.

- No permissions by default (`permissions: {}`); each job asks only for what it needs.
- Every action is pinned to a full commit SHA, with its version in a comment. To update one, resolve the new tag to its commit (`gh api repos/<owner>/<action>/git/ref/tags/<tag>`, and follow an annotated tag to its commit) and update both.
- Values from the event (tag names, inputs) reach scripts through environment variables, never through `${{ }}` inside a `run:` block.
- Checkouts use `persist-credentials: false`.
- CI tools come from `requirements/ci.txt` (and coverage from `requirements/ci-coverage.txt`), installed with `--require-hashes`. PSScriptAnalyzer is pinned to one version and must carry a valid Microsoft signature.
- A new version of an action or tool is adopted only after it has been public for at least 7 days, so a compromised or broken release has time to be found.

## Before cutting a release

CI proves the code parses, the unit tests pass and the pins resolve. It does not run the models or the packaged app.

1. Check the release against [RELEASE_STANDARD.md](RELEASE_STANDARD.md). Part B lists each gate, where it runs and whether it is automated yet.
2. Build and test the package as described in "Unminute package releases" below.
3. Run one real, non-sensitive recording through the installed package with speaker identification on, and check the transcript looks right.

## Cutting a release

Releases are cut by the Release workflow in `.github/workflows/release.yml` when a version tag is pushed. There is no manual "Run workflow" route: the tag is the single record of what was released.

1. On a branch, run `python tools/changelog.py vX.Y.Z` (for a preview, `vX.Y.Z-preview.N`). It moves the fragments in `changelog.d/` into a new `## vX.Y.Z` section at the top of `CHANGELOG.md` and deletes them; `--summary "One line"` adds a line under the heading. Review the section, then merge it to `main`.
2. Tag the merged commit and push the tag:

   ```
   git tag -a vX.Y.Z -m "Unminute vX.Y.Z" <commit on main>
   git push origin vX.Y.Z
   ```

The workflow accepts only `vX.Y.Z` or `vX.Y.Z-preview.N`, and only a commit that is already on `main`. It builds the release notes from the changelog section and publishes the release; a `-preview.N` tag becomes a pre-release that is not Latest. GitHub attaches the source zip and tarball automatically. If the changelog section is missing the workflow fails before anything is published.

## Unminute package releases

The workflow publishes the notes and the source archives only. The Windows package is built and attached by hand, from the commit that is tagged:

1. **Changelog.** Merge the `## vX.Y.Z` section made by `tools/changelog.py` (step 1 of "Cutting a release"), then check out that commit of `main`.
2. **Build.** Run `python tools/build_package.py --version X.Y.Z` on Windows with 64-bit CPython 3.12, from a clean working tree. The command prints the zip's SHA-256 and writes the `.sha256` file to `dist/`.
3. **Test the build.**
   - Install it with `install.ps1 -Package dist\Unminute-X.Y.Z.zip`.
   - Run `runtime\python.exe -m workspace --self-test` from the installed version's `app` folder. Expect PASS.
   - Measure it against the budgets in `docs/perf-budgets.json`. From a checkout of the tagged commit, run `<installed version>\runtime\python.exe -B tools\perf_check.py perf-X.Y.Z.json --app <installed version>\app --zip dist\Unminute-X.Y.Z.zip --leak-minutes 30`. Leave the laptop idle, on mains power and the Balanced power plan, with the window visible for about 90 minutes. Expect WITHIN BUDGET (exit 0), and attach the JSON to the release.
   - If a previous version is installed, check that the update keeps the workspace.
   - Work through docs/RELEASE_CHECKLIST.md. For a full release, decide explicitly on each open item in "Before a business release": either verified, or listed as a known limitation in the notes.
4. **Publish.**
   - Tag the commit you built (`version.json` in the zip records it) and push the tag, as above.
   - Attach the zip and its `.sha256`: `gh release upload vX.Y.Z dist/Unminute-X.Y.Z.zip dist/Unminute-X.Y.Z.zip.sha256`.
5. **Release type.** A `vX.Y.Z` tag publishes a full release, which becomes Latest, the default download. A `vX.Y.Z-preview.N` tag publishes a pre-release that does not.

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
- **The dependencies.** `requirements.txt` is a full freeze, including torch, CTranslate2 and pyannote.audio. It was chosen because specific combinations break on CPU-only Windows. A PyPI package would need loose version ranges or a hard pin on torch, and neither works well in someone else's environment.
- **The package does what a PyPI package cannot:** it carries its own runtime, checks the machine, keeps the previous version for rollback, and holds back storage-format updates until the workspace is empty.
