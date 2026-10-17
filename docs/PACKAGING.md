# Windows package for Unminute

This guide covers the preassembled package from #11:

- building it;
- installing it per user, either interactively or unattended;
- managed deployment with Intune;
- signing and trust.

Updates and the empty-workspace migration gate are #26.

The earlier source app (Whisper Transcriber) was removed from the repository by #111 slice A7; see "If the earlier app is still installed" in [WORKSPACE_GUIDE.md](WORKSPACE_GUIDE.md).

## What the package contains

One versioned zip, `Unminute-<version>.zip`, and its checksum file, `Unminute-<version>.zip.sha256`. Releases up to v0.2.3 used the earlier name, `WhisperWorkspace-<version>.zip`; the installer treats both the same.

| Path | Contents |
| --- | --- |
| `runtime/` | A private CPython 3.14 runtime: the official embeddable package from python.org, with the hash-locked wheels from `requirements.txt` installed, less what the app never uses (step 3 below), and every module precompiled. A `._pth` file fixes the import path, and the app starts with `-I`, so the runtime ignores `PYTHON*` variables, user site-packages and any other Python on the machine. |
| `app/` | `asr_engines.py`, `offline_guard.py` and `workspace/`, precompiled. Tests are not included. |
| `install.cmd`, `install.ps1`, `uninstall.ps1` | The per-user installer and uninstaller. |
| `version.json` | The version, build time (the source commit's time, so a rebuild is identical), Python version and source commit; whether it is a release build (`release`) and, for a development build, whether the working tree had no changes (`clean`). |
| `package-manifest.json` | The size and SHA-256 of every file. The installer checks all of them. |
| `sbom.cdx.json` | A CycloneDX software bill of materials: the runtime and every bundled package, with the exact wheel file, its SHA-256 and its licence. The same file is published beside the zip as `Unminute-<version>.cdx.json`. |
| `THIRD_PARTY_NOTICES.txt` | Every bundled component, its licence, where its licence texts are in the package, and the native libraries (DLLs) it bundles, with their signers. The build fails if it names a file that is not in the package. Licence texts for some bundled native libraries are not included yet (#111). |
| `binary-inventory.json` | Every executable file (by suffix, or by its Windows, Linux or macOS executable header), with its size, SHA-256 and Authenticode signature: whether it is signed, the signer, the certificate thumbprint and whether the signature is time-stamped. |
| `model-manifest.json` | The pinned model files, by size and SHA-256, for checking a copy staged by IT. The models themselves are not in the package. |

Models are not in the package. They are separate, verified downloads in the app's Setup view (#19), stored in `%LOCALAPPDATA%\whisper-models` (shared with the earlier app where it is still installed). To keep models elsewhere, redirect that whole folder, not folders inside it: downloading, repairing or removing refuses a model folder that is a link or junction.

Users never need Python, Git, FFmpeg or GPU tools.

## Building

On a Windows build machine with 64-bit CPython 3.14 and network access:

```
python tools/build_package.py --out dist             (a development build)
python -I tools/build_package.py --out dist --release   (a release build)
```

`--release` refuses a working tree with any change or untracked file, a file hidden from `git status` (assume-unchanged or skip-worktree), and a HEAD that is not the commit of tag `vX.Y.Z` (a tag, not a branch of that name), whose `VERSION` must match. It resolves that commit once and reads what it packages and obeys from the commit itself: the app and installer files, the lock and the binary allowlist, as git stores them, with no git filter, attribute file or line-ending setting applied. `.cmd` and `.ps1` files get CRLF line endings, as `.gitattributes` says; every other file is written as stored. So no ignored, untracked, locally changed or hidden file can ship, and no git setting on the build machine changes a byte. The build script and the vendored wheel must also match the commit, and the build stops without writing the zip if HEAD or the tree changes while it runs. It must run with `python -I` (it refuses otherwise), so no `PYTHON*` variable, user site-packages or stray file beside the script can change it. A file name in the commit that Windows would read as another path (a backslash, a colon or a control character) stops the build.

A development build copies the app from the working folder: tracked and untracked files, never ignored ones (such as a recording or log left in `workspace/`). `version.json` says it is not a release, and whether the working tree had changes.

A release build is reproducible: the same commit and lock, built with the same Python, pip and zlib versions (recorded in the bill of materials), give the same zip, byte for byte. Every file in the zip carries one time stamp, `SOURCE_DATE_EPOCH` if set or else the source commit's time, and entries are written in a fixed order with fixed compression. A build outside a git checkout needs `SOURCE_DATE_EPOCH`.

The version comes from the `VERSION` file, which `tools/changelog.py` sets when a release is prepared (see [RELEASING.md](RELEASING.md)). A `--version` that differs from it is refused.

The build:

1. Downloads the embeddable runtime, checks it against the pinned SHA-256, and checks that `python.exe`, `pythonw.exe` and `python314.dll` carry a valid Python Software Foundation Authenticode signature.
2. Downloads the hash-locked requirements (`requirements.txt`, generated by `tools/lock.py`) into the build cache, then installs them from there with no index, as wheels only, for `win_amd64` and CPython 3.14. Every file must match a SHA-256 in the lock (`--require-hashes`), and nothing outside the lock is installed (`--no-deps`). pip's install report names the exact wheel used for each package. The build refuses to run on any other host.
   - `proxy_tools` (MIT) publishes no wheel. Its wheel was built once from the PyPI sdist and is committed in `packaging/wheels/`, and the build checks it against a pinned SHA-256.
   - Console launchers, C headers and man pages written by pip (`runtime\lib\bin`, `include` and `share`) are removed; the launchers embed the build machine's paths. Each package's `RECORD` is then trimmed to the files that ship, and licence files moved out of very deep folders are recorded at their new place.
   - pip runs with `--isolated`, so no `PIP_*` variable or pip configuration file can change what it installs.
   - `runtime\sitecustomize.py` registers `runtime\lib` as a site directory, so packages that rely on `.pth` files work.
3. Leaves out what the app never uses (#111 B2; `PRUNED_*` in `tools/build_package.py`). An import trace of the window process, transcription and speaker identification on CPython 3.14 loaded none of it. Each entry must still be there to remove, so a dependency change cannot leave a stale one.
   - Whole packages: `grpcio` (only opentelemetry's gRPC exporter uses it, and telemetry is off), `pyannoteai-sdk` (the client for pyannote's cloud service) and `setuptools` (a packaging tool, whose `.pth` hook ran at every start). They leave the bill of materials and the notices, which record that they were removed.
   - Every folder named `tests` or `test` inside a package (licence files in `.dist-info` stay).
   - Files needed only to compile or link against a package, or to type-check it: C and C++ headers (including `torch\include`), `.lib`, `.pdb`, `.pyi`, Cython and C sources.
   - `torch\bin\protoc.exe`, `clr_loader`'s 32-bit library, pywebview's 32-bit Internet Explorer interop (only a 32-bit Python loads it), and `libtommath.dll` from the runtime. pywebview's 32-bit and Arm WebView2 loader folders stay: its WebView2 module looks them all up when it is imported, so the window cannot open without them (`--self-test` imports that module).
4. Copies the app and installer files (a release build takes them from the commit) and precompiles every module in `runtime\lib`, `app\` and `runtime\sitecustomize.py` with the package's own Python (`compileall` in one process, unchecked-hash `.pyc`, hash seed 0, paths recorded relative to the package), so the same commit gives the same bytes. The build fails if a module has no `.pyc` or a `.pyc` holds the build folder's path. The app starts with `-I -B -X disable-remote-debug -m workspace`: `-B` means it never writes bytecode into its folder, and unchecked-hash means Python uses the shipped `.pyc` without reading the source first. Support commands below also use `-I -B`. The GPU component, unpacked from wheels without bytecode, is the one place Python still caches it (beside the component), so torch is not compiled again at every start. Then it writes `version.json` and `model-manifest.json` (from the `asr_engines.py` being packaged).
5. Reads the Authenticode signature of every executable file and checks each against `packaging/binary-allowlist.json`, which lists every file with its signer. A file that is not listed, a listed one that is missing, a changed signer, a signature that is neither valid nor absent (for example a hash mismatch), or a signature that Windows found in this machine's catalogs rather than in the file fails the build. "Valid" depends on the build machine's certificate store and clock: a rebuild of an old version may stop if a bundled file's certificate has since been revoked, or has expired without a time stamp. When a dependency change adds, renames or re-signs binaries, rebuild with `--update-binary-allowlist` and review the list's diff in the pull request; a release build cannot rewrite it.
6. Writes `binary-inventory.json`, `THIRD_PARTY_NOTICES.txt`, `sbom.cdx.json` and `package-manifest.json`.
7. Zips the result and checks its size (MB, one decimal) and file count against `package_zip_mb` and `package_files` in `tests/budgets.json` (a release build reads them from the commit); over either, the zip is deleted and the build fails. Then it writes the `.sha256` file and `Unminute-<version>.cdx.json`.

Publishing the zip is part of release publication. Follow [RELEASING.md](RELEASING.md), and only with release authorisation.

## Installing (self-service)

1. Download the zip and its `.sha256` file.
2. Extract the zip.
3. Double-click `install.cmd`.

Alternatively, without extracting first:

```
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Package Unminute-<version>.zip
```

The installer runs as the current user and needs no administrator rights. It:

- **Checks prerequisites:**
  - 64-bit Windows 10 1809 or later, or Windows 11;
  - the Microsoft Edge WebView2 Runtime, which is built into Windows 11 and present on most managed Windows 10 devices. It must be recent enough to report screen-capture requests (added in WebView2 SDK 1.0.2903.40), which the Evergreen runtime is when Windows keeps it updated; with an older runtime the app closes its window rather than open a page it cannot secure;
  - about 6 GB free disk space;
  - processor cores and memory: the minimum is 4 cores and 8 GB, and 8 cores and 16 GB are recommended (#28). Below the minimum, the installer warns and asks before continuing; an unattended (`-Silent`) install records the warning and continues.
- **Verifies the package.**
  - With `-Package`, it checks the zip against its `.sha256` file. That is the check that detects tampering, provided the `.sha256` comes from the release page.
  - In every mode it checks every file against `package-manifest.json`, and refuses files that are missing, different or not listed.
  - The manifest travels inside the package. In the extracted-folder route it therefore detects corruption and accidental changes, not deliberate tampering. Prefer `-Package`, or verify the zip's `.sha256` before extracting it.
  - Nothing is installed if any check fails.
- **Installs side by side** to `%LOCALAPPDATA%\Programs\WhisperWorkspace\versions\<version>`. It keeps the previous version for rollback and removes older ones.
- **Refuses to install while the app is running.**
- **Creates a Start menu shortcut,** "Unminute", and a per-user uninstall entry in Settings > Apps. An update from a version that used the earlier name, "Meeting Workspace", replaces that shortcut. The install folder and uninstall registry key keep their earlier name (`WhisperWorkspace`), so Intune detection rules keep working, and the taskbar identity (`WhisperTranscriber.MeetingWorkspace`) is unchanged, so taskbar pins still group with the window. A rollback to a version from before the rename restores its "Meeting Workspace" shortcut and entry.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Installed |
| 1 | Failed |
| 2 | Prerequisites missing |
| 3 | The app is running |
| 4 | The package failed verification |
| 5 | The update is waiting for the workspace (see the message) |
| 6 | This version cannot open the existing workspace |

## Updates (#26)

Self-service users and IT use the same route: run the new package's installer (`install.cmd`, or `install.ps1 -Silent` from Intune). Installing with `-Managed` records that IT manages updates. The app then shows "Updates are managed by your IT team" instead of self-service instructions, and later updates keep the setting.

Before switching version, the installer runs the **new** version's own check, `python -m workspace --gate`, against the existing workspace:

| Workspace | Result |
| --- | --- |
| None yet | Installed. |
| Same storage format (schema) | Installed. Everything is kept: meetings, edits, holds, settings. |
| New storage format, and the workspace holds no meetings, no active retention holds, no unfinished cleanup and no microphone recording waiting to be imported | Installed. Settings, watched folders and the minimal cleanup receipts are carried over when the app next opens. |
| New storage format, and the workspace still holds any of those | **Waits.** Exit code 5, with the reasons, for example "2 retention holds are active. Holds never expire on their own…". Nothing is changed. |
| Older storage format than the workspace (a downgrade) | Refused. Exit code 6. |

Rules:

- No update ever expires a hold, deletes content or makes a backup of meeting content to let itself proceed.
- Every update waits while the app is running, so nothing is processing or cleaning up during an update.
- In Intune, map exit code 5 to "Retry". The device gets the update once the people responsible have finished their meetings and released their holds.
- Deploy in **user** context. The gate checks the signed-in user's workspace, and in System context it would see none.
- A workspace that exists but cannot be read (damaged or locked) also makes the update wait, with exit code 5. It is never treated as a fresh install.
- `-SelfService` switches an IT-managed installation back to self-service updates.

Settings > Updates in the app shows the deployment mode and anything that would hold up a storage-format update.

To go back to the previous installed version, run:

```
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Rollback
```

It runs the same check with the previous version, so it refuses if that version cannot open the workspace (exit code 6). This happens, for example, after a newer version has changed the storage format.

Use `-Rollback` from the current version to go back, rather than running an older package's installer over a newer install. Installers from before the rename to Unminute only know the "Meeting Workspace" shortcut, so an older installer run that way can leave an extra Start menu shortcut.

## Uninstalling

Use Settings > Apps > Unminute, or `uninstall.ps1` (with `-Silent` for unattended removal).

Uninstalling removes the program, the shortcut and the uninstall entry. It does not remove:

- **The workspace** (`%LOCALAPPDATA%\whisper\workspace`): meetings, holds, settings and receipts. Meeting content is only ever removed through the app's confirmed cleanup steps, which honour retention holds. The uninstaller says when a workspace remains.
- **Downloaded models** (`%LOCALAPPDATA%\whisper-models`).

## Managed deployment (Intune)

Package the extracted folder as a Win32 app with the Microsoft Win32 Content Prep Tool, using `install.cmd` as the setup file. Then configure:

- **Install behaviour:** User. The app installs per user, under the signed-in user's profile.
- **Install command:**
  ```
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Silent
  ```
  Before wrapping the folder, verify the zip's `.sha256` in the packaging pipeline.
- **Uninstall command:**
  ```
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\Programs\WhisperWorkspace\uninstall.ps1" -Silent
  ```
- **Return codes:** map 2, 3 and 4 to Failed. Code 3 (app running) is worth a retry.
- **Detection rule:** Registry; key path `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Uninstall\WhisperWorkspace`; value name `DisplayVersion`; detection method "String comparison", equals, `<version>`. Associated with a 32-bit app on 64-bit clients: No.
- **Requirements:** Windows 10 1809 or later, x64. If WebView2 cannot be assumed, deploy the WebView2 Evergreen runtime as a dependency, and keep it updated (see the prerequisites above).

For support, `runtime\python.exe -I -B -m workspace --self-test` (in the installed version folder) imports every dependency and confirms the network guard. `--status` prints workspace counts only, never content, plus `integrity` (the last database check after an unclean close: `ok`, `failed`, `skipped` when it could not run, which the next start retries, or `unchecked`), `readable` (false when the database cannot be read at all) and `recordings` (recordings not yet in a meeting). See "If Unminute finds damage" in [WORKSPACE_GUIDE.md](WORKSPACE_GUIDE.md).

For an Intune Remediation detection script, run `runtime\python.exe -I -B -m workspace --health` from the user's installed version folder, with **Run this script using the logged-on credentials** set to **Yes**. Never run it as SYSTEM: the install is the user's own and the user can change its files, so a SYSTEM run would run the user's code as SYSTEM, and SYSTEM sees no user's workspace. Run as SYSTEM, it reports `"workspace": "system_account"` and exits 1.

It prints one line of JSON (well within Intune's 2,048-character output limit) and exits 0 when healthy, 1 when it needs attention (Intune's "issue detected"). It never prints an error message or a traceback. Fields:

- `version`.
- `workspace`: `ok`; `none` (this user has not opened the app yet; still healthy); `damaged` (the last database check found damage: see "If Unminute finds damage" in [WORKSPACE_GUIDE.md](WORKSPACE_GUIDE.md)); `location` (the app data location is missing or synced); `unreadable` (also when Windows cannot say which account it runs as); `system_account`.
- `running`: the app is open, asked of Windows through its single-instance lock and the process it recorded at start, so from any session.
- `last_run`: `clean`, or `did_not_close` when the app last ended without closing and has not been opened since (its next start records that as a crash).
- `crashes_7d`: crashes (including runs that ended without closing) and worker or download processes that ended by themselves in the last 7 days, each counted once, from the content-free logs.
- `schema`: the workspace's storage version, or null.
- `healthy`: `workspace` `ok` or `none`, `last_run` `clean`, and no crashes.

It reads counts and the logs only, never content. It writes nothing of its own, though opening the database read-only can leave SQLite's `-wal` and `-shm` files beside it. A user can add the detail with Help > **Copy diagnostics**.

### Verifying a release before packaging

Each release is built by the project's Release workflow on GitHub, from its tagged commit, and signed (build provenance and an SBOM attestation). Before packaging a release for Intune, check the download:

```
powershell -NoProfile -File tools\intune\Get-VerifiedRelease.ps1 -Tag vX.Y.Z
```

- **What it downloads:** the release's zip, its `.sha256` file, the SBOM and the attestation bundle, into a new folder.
- **What it checks:** that the release is published, its tag is exactly the one given (case matters), and it is not a preview unless you add `-AllowPrerelease`; that the zip matches its checksum; and that both attestations verify. They must be signed by exactly the repository's Release workflow at that tag, from the commit the tag points at, on a GitHub-hosted runner.
- **The result:** it prints VERIFIED and the zip's SHA-256, or NOT VERIFIED and the reason. Package only a verified zip.
- **Always run it.** The `.sha256` file beside a zip detects a damaged download, not a replaced one: anyone who can replace the zip on the release page can replace its checksum too. The signature check is what proves the zip was built by the project's Release workflow from that tag.
- **What it needs:** the GitHub CLI (`gh`) on the path. No GitHub account or sign-in is needed while the repository is public: `gh` checks the bundle against Sigstore's public trust root.

### Managed settings (policy)

From the first release after v0.4.3, the app reads policy values from `HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Unminute` (64-bit view): at start, every 15 minutes, on each refresh while Settings or Downloads is open, and again just before it would delete an original recording. Standard users cannot change this key. Policy overrides the user's settings: Settings says some settings are managed and disables those controls, and the app enforces each value itself.

The administrative template is in `packaging/policy/` (`Unminute.admx` and `en-US/Unminute.adml`; it needs no other ADMX file). In Intune, import both files under Devices > Manage devices > Configuration > Import ADMX, then create a Windows configuration profile from Templates > Imported Administrative templates and choose the "Unminute" category. Microsoft marks custom ADMX import as a preview feature, and Intune does not overwrite an imported template: to take a later version, delete the profiles that use it and the old template, then import again. The values can also be set directly in the registry.

| Value | Type | Not set | Effect when set |
| --- | --- | --- | --- |
| `AllowModelDownloads` | DWORD 0/1 | Allowed | 0: the app refuses model downloads and repairs; Setup and Downloads show "Ask your IT team" instead of a Download button |
| `AllowGpuComponent` | DWORD 0/1 | Allowed | 0: the graphics card component cannot be downloaded or used; speaker identification runs on the processor |
| `AllowMicrophoneRecording` | DWORD 0/1 | Allowed | 0: microphone recording is off and cannot be turned on; a recording in progress is stopped and saved when the app reads the change; imports still work |
| `AllowLidOverride` | DWORD 0/1 | Allowed | 0: "Keep recording when the lid is closed" cannot be turned on, and a lid action the app set to "Do nothing" is put back when the app reads the change. The app never changes a lid action that Group Policy or Intune sets (#172) |
| `RequireRecordingConsent` | DWORD 0/1 | Required | 0: recordings start without the confirmation that everyone has been told. A bad value keeps the confirmation |
| `AllowWatchedFolders` | DWORD 0/1 | Allowed | 0: no folders can be added, existing watched folders are not checked, and recordings already found in them wait, from when the app next reads the policy (within 15 minutes; the one being copied in at that moment finishes); imports by hand still work |
| `AllowDeleteOriginals` | DWORD 0/1 | Allowed | 0: no folder can be given authority to delete originals, and originals are kept even where authority was given before. Independent of `AllowWatchedFolders`: with watched folders off, originals that already have authority are still deleted after the user confirms cleanup (agreed 17 Oct 2026, #152) |
| `AllowNetworkSources` | DWORD 0/1 | Refused | 1: recordings on mapped network drives, or reached through a symbolic link, can be imported. `\\server\share` paths and watched folders on network drives stay refused |
| `HandoffFilenameIncludesTitle` | DWORD 0/1 | Date and time only | 1: Save suggests the meeting title as the file name, except when speaker names are replaced |
| `ClipboardClearMinutes` | DWORD 0 to 1440 | Never cleared | N: a handoff copied with the app's Copy button is cleared after N minutes (time asleep counts) and when the app closes, unless something else was copied since. The Copy message tells the user |
| `HandoffDefaultFolder` | REG_SZ or REG_EXPAND_SZ | The dialog's usual folder | The Save dialog starts here. A full drive-letter path (a mapped drive is fine); `\\server\share` paths are not accepted. A folder that does not exist is ignored |

A value of the wrong type or out of range is treated as not allowed for the `Allow` values, and as not set for the others. If the key exists but cannot be read, every `Allow` value is treated as not allowed. Settings then says some settings are managed. Value names are not case-sensitive; a name the app does not know (for example a misspelling) is ignored, so check names against the table. Policy values are machine-wide: they apply to every user of the computer.

The app enforces these values in its own code. Until the per-machine install (#111 slice B6), that code sits in the user's profile, so someone able to change files there could remove the checks; the policy key itself stays admin-only.

## Signing and trust

The build does not sign anything, and this guide does not assume a particular signing provider. Integrity currently rests on two checks:

- the published `.sha256` of the zip, published through the release page over HTTPS and checked by `-Package` installs or by the pipeline that uploads to Intune;
- the per-file manifest that the installer verifies, which detects corruption, missing files and added files.

The Python runtime's own executables keep their Python Software Foundation signatures.

Options, depending on what the organisation has:

1. **An organisational code-signing certificate** (for example from an internal CA trusted by managed devices). Sign `install.ps1` and `uninstall.ps1` with `Set-AuthenticodeSignature`, so `AllSigned` execution policies accept them, and publish the zip's SHA-256 alongside.
2. **A public code-signing service** (for example Azure Trusted Signing or a commercial OV/EV certificate). The same files are signed. This also helps SmartScreen reputation for self-service downloads.
3. **Intune only, unsigned.** Intune delivers the package from the tenant, so the main remaining exposure is the source of the zip. Keep the `.sha256` check in the build pipeline that uploads it.

Whichever route is chosen, record it in the release notes. A changed signature or checksum must stop a deployment.

## Limits

- Only per-user installation is provided. A per-machine (Program Files) installation is not needed for the target audience, and was not built.
- On the development machine (13 October 2026, Windows 11, while another heavy app was running), the 0.2.0 build was checked end to end:
  - **Build:** a 330 MB zip containing 27,746 files.
  - **Install:** `-Package` mode, verifying the checksum and every file, took 244 s.
  - **Self-test:** every dependency imported from the private runtime, `PYTHONPATH` was ignored, and the network guard blocked outside connections.
  - **Inference:** Parakeet transcribed and pyannote separated two speakers, offline, through the managed worker.
  - **Window:** the app opened ("Meeting workspace", with its WebView2 child) using an isolated profile.
  - **Uninstall:** removed the program and its uninstall entry, and kept the models.
- Testing on a clean standard-user Windows profile, and on a managed Intune device, is still to be done before release. It is tracked in #11 and #28.
- Paths inside the package are kept to 140 characters or fewer. The default install folder has room for user names of up to about 30 characters, and the installer refuses install folders longer than 100 characters. Windows PowerShell 5.1 cannot handle paths over 260 characters.
