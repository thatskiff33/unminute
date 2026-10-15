# Releasing and repository settings

This page covers the repository housekeeping that is not code: protecting `main`, cutting a release and the decision not to publish a package.

The source-only packaging instructions below describe the current release. The
[workspace roadmap](WORKSPACE_PLAN.md) supersedes that packaging choice for the planned
business release: #11 delivers a private runtime/package, and #26 controls managed versus
self-service updates and schema migration. Until those changes are implemented and
verified, the existing source-release workflow remains in use. Publishing a release
requires separate authorisation from merging roadmap or implementation changes.

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
   - Require status checks to pass. Add the four jobs from `.github/workflows/ci.yml`: `Syntax check`, `Requirements resolve (wheels only)`, `Ruff` and `PSScriptAnalyzer`. They only appear in the picker after the CI workflow has run once, so add them after the first pull request goes green.
5. Leave Bypass list empty. Admins are then held to the same rules, which is the point.
6. Click Create.

From then on, all changes reach `main` through a pull request, and nobody can force-push or delete the branch.

Already enabled at repository level (Settings > Advanced Security): Dependabot alerts, Dependabot security updates, secret scanning and push protection. Routine version bumps are configured in `.github/dependabot.yml` (monthly, grouped; torch, torchaudio and torchcodec limited to patch releases). CodeQL runs from `.github/workflows/codeql.yml`.

## Before cutting a release

CI proves the code parses and the pins resolve. It does not run the models. Before tagging a release, run one real recording through the app with speaker identification on, using the pinned set from `requirements.txt` (Update / repair does this), and check the transcript looks right.

## Cutting a release

Releases are cut by the Release workflow in `.github/workflows/release.yml`.

1. Add a `## vX.Y.Z` section to `CHANGELOG.md` on a branch and merge it to `main`.
2. Open https://github.com/thatskiff33/unminute/actions/workflows/release.yml and click Run workflow.
3. Enter the version without the leading v, for example `0.1.0`, and run it against `main`.

The workflow tags the current `main` commit, builds the release notes from the changelog section, and publishes the release. GitHub attaches the source zip and tarball automatically. If the changelog section is missing the workflow fails before anything is tagged.

Pushing a `vX.Y.Z` tag by hand also triggers the workflow and publishes the release for that tag.

## Unminute package releases

The workflow publishes the notes and the source archives only. The Windows package is built and attached by hand, from the same commit the workflow tags:

1. **Changelog.** Merge the `## vX.Y.Z` section, then check out that commit of `main`.
2. **Build.** Run `python tools/build_package.py --version X.Y.Z` on Windows with 64-bit CPython 3.12, from a clean working tree. The command prints the zip's SHA-256 and writes the `.sha256` file to `dist/`.
3. **Test the build.**
   - Install it with `install.ps1 -Package dist\Unminute-X.Y.Z.zip`.
   - Run `runtime\python.exe -m workspace --self-test` from the installed version's `app` folder. Expect PASS.
   - If a previous version is installed, check that the update keeps the workspace.
   - Work through docs/RELEASE_CHECKLIST.md. For a full release, decide explicitly on each open item in "Before a business release": either verified, or listed as a known limitation in the notes.
4. **Publish.**
   - Before running the workflow, check that `main` is still at the commit you built. The workflow tags the current `main`, and `version.json` in the zip records the commit built.
   - Run the Release workflow for `X.Y.Z`.
   - Attach the zip and its `.sha256`: `gh release upload vX.Y.Z dist/Unminute-X.Y.Z.zip dist/Unminute-X.Y.Z.zip.sha256`.
5. **Release type.** The workflow publishes every release as a full release, so a full release becomes Latest, the default download. For a preview, run `gh release edit vX.Y.Z --prerelease --latest=false`.

## Why there is no PyPI package or installer executable

The decision for v0.1.0 is to ship source only. Reasons:

- The audience is non-technical staff on locked-down Windows laptops. They double-click `install.cmd`. A `pip install` step would be a regression for them.
- `requirements.txt` is a full 120-package freeze including torch, CTranslate2 and pyannote.audio, chosen because specific combinations break on CPU-only Windows without FFmpeg. A PyPI package would need loose version ranges or a hard pin on torch, and neither works well in someone else's environment.
- The installer does things a package cannot: finds or installs Python per user, sets `HF_TOKEN`, downloads gated models, creates a Start Menu shortcut, warns about OneDrive paths.
- A PyInstaller or MSIX executable would be several GB with torch and models bundled, would need code signing to pass Intune and antivirus cleanly, and would still need the Hugging Face token step.

Revisit this if people outside the office start using it. The natural first step would be a small `pyproject.toml` with a console entry point so developers can `pip install -e .`, without changing the double-click path for everyone else.
