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
   - Require status checks to pass. Add every job from `.github/workflows/ci.yml`: `Syntax check`, `Unit tests (Windows)`, `Requirements resolve (wheels only)`, `Ruff`, `PSScriptAnalyzer` and `Workflow lint`. They only appear in the picker after the CI workflow has run once, so add them after the first pull request goes green.
5. Leave Bypass list empty. Admins are then held to the same rules, which is the point.
6. Click Create.

From then on, all changes reach `main` through a pull request, and nobody can force-push or delete the branch.

Already enabled at repository level (Settings > Advanced Security): Dependabot alerts, Dependabot security updates, secret scanning and push protection. Routine version bumps are configured in `.github/dependabot.yml` (monthly, grouped, both requirement files, a 7-day cooldown; torch, torchaudio and torchcodec limited to patch releases). CodeQL analyses the Python code and the workflows from `.github/workflows/codeql.yml`.

## Workflow supply-chain rules

The workflows run with the repository's token, so they are held to these rules. The `Workflow lint` job (zizmor) enforces the first three on every pull request.

- No permissions by default (`permissions: {}`); each job asks only for what it needs.
- Every action is pinned to a full commit SHA, with its version in a comment. To update one, resolve the new tag to its commit (`gh api repos/<owner>/<action>/git/ref/tags/<tag>`, and follow an annotated tag to its commit) and update both.
- Values from the event (tag names, inputs) reach scripts through environment variables, never through `${{ }}` inside a `run:` block.
- Checkouts use `persist-credentials: false`.
- CI tools come from `requirements/ci.txt`, installed with `--require-hashes`. PSScriptAnalyzer is pinned to one version and must carry a valid Microsoft signature.
- A new version of an action or tool is adopted only after it has been public for at least 7 days, so a compromised or broken release has time to be found.

## Before cutting a release

CI proves the code parses and the pins resolve. It does not run the models. Before tagging a release, run one real recording through the app with speaker identification on, using the pinned set from `requirements.txt` (Update / repair does this), and check the transcript looks right.

## Cutting a release

Releases are cut by the Release workflow in `.github/workflows/release.yml` when a version tag is pushed. There is no manual "Run workflow" route: the tag is the single record of what was released.

1. Add a `## vX.Y.Z` section to `CHANGELOG.md` on a branch and merge it to `main`.
2. Tag the merged commit and push the tag:

   ```
   git tag -a vX.Y.Z -m "Unminute vX.Y.Z" <commit on main>
   git push origin vX.Y.Z
   ```

The workflow accepts only `vX.Y.Z` or `vX.Y.Z-preview.N`, and only a commit that is already on `main`. It builds the release notes from the changelog section and publishes the release; a `-preview.N` tag becomes a pre-release that is not Latest. GitHub attaches the source zip and tarball automatically. If the changelog section is missing the workflow fails before anything is published.

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
   - Tag the commit you built (`version.json` in the zip records it) and push the tag, as above.
   - Attach the zip and its `.sha256`: `gh release upload vX.Y.Z dist/Unminute-X.Y.Z.zip dist/Unminute-X.Y.Z.zip.sha256`.
5. **Release type.** A `vX.Y.Z` tag publishes a full release, which becomes Latest, the default download. A `vX.Y.Z-preview.N` tag publishes a pre-release that does not.

## Why there is no PyPI package or installer executable

The decision for v0.1.0 is to ship source only. Reasons:

- The audience is non-technical staff on locked-down Windows laptops. They double-click `install.cmd`. A `pip install` step would be a regression for them.
- `requirements.txt` is a full 120-package freeze including torch, CTranslate2 and pyannote.audio, chosen because specific combinations break on CPU-only Windows without FFmpeg. A PyPI package would need loose version ranges or a hard pin on torch, and neither works well in someone else's environment.
- The installer does things a package cannot: finds or installs Python per user, sets `HF_TOKEN`, downloads gated models, creates a Start Menu shortcut, warns about OneDrive paths.
- A PyInstaller or MSIX executable would be several GB with torch and models bundled, would need code signing to pass Intune and antivirus cleanly, and would still need the Hugging Face token step.

Revisit this if people outside the office start using it. The natural first step would be a small `pyproject.toml` with a console entry point so developers can `pip install -e .`, without changing the double-click path for everyone else.
