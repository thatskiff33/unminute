# Windows package for Unminute

This guide covers the preassembled package from #11:

- building it;
- installing it per user, either interactively or unattended;
- managed deployment with Intune;
- signing and trust.

Updates and the empty-workspace migration gate are #26.

For IT, [IT_DEPLOYMENT.md](IT_DEPLOYMENT.md) brings this guide together with health monitoring, offboarding and incidents, and links back here for each detail.

## What the package contains

One versioned zip, `Unminute-<version>.zip`, and its checksum file, `Unminute-<version>.zip.sha256`.

| Path | Contents |
| --- | --- |
| `runtime/` | A private CPython 3.14 runtime: the official embeddable package from python.org, with the hash-locked wheels from `requirements.txt` installed, less what the app never uses (step 3 below), and every module precompiled. A `._pth` file fixes the import path, and the app starts with `-I`, so the runtime ignores `PYTHON*` variables, user site-packages and any other Python on the machine. |
| `app/` | `workspace/` (including `asr_engines.py` and `offline_guard.py`), precompiled. Tests are not included. |
| `install.cmd`, `install.ps1`, `uninstall.ps1` | The per-user installer and uninstaller. |
| `version.json` | The version, build time (the source commit's time, so a rebuild is identical), Python version and source commit; whether it is a release build (`release`) and, for a development build, whether the working tree had no changes (`clean`); the workspace storage format it uses (`schema`, since #111 B5a); and the switches installers may rely on (`features`: `gate`, `restore-lid`, and `launch-gate` for the launcher pass and `--slot`). |
| `package-manifest.json` | The size and SHA-256 of every file. The installer checks all of them. |
| `sbom.cdx.json` | A CycloneDX software bill of materials: the runtime and every bundled package, with the exact wheel file, its SHA-256 and its licence. The same file is published beside the zip as `Unminute-<version>.cdx.json`. |
| `THIRD_PARTY_NOTICES.txt` | Every bundled component, its licence, where its licence texts are in the package, and the native libraries (DLLs) it bundles, with their signers and, where collected, the component each DLL comes from and its licence texts. The build fails if it names a file that is not in the package. Licence texts for some wheels and bundled native libraries are not included yet (#176); the notices say so until they are. |
| `licences/` | Licence texts collected from each project's own release for wheels that ship none and for libraries bundled inside wheels (#176), copied byte for byte from `packaging/licences/` (see "Licence texts" below). |
| `binary-inventory.json` | Every executable file (by suffix, or by its Windows, Linux or macOS executable header), with its size, SHA-256 and Authenticode signature: whether it is signed, the signer, the certificate thumbprint and whether the signature is time-stamped. |
| `model-manifest.json` | The pinned model files, by size and SHA-256, for checking a copy staged by IT. The models themselves are not in the package. |

Models are not in the package. They are separate, verified downloads in the app's Setup view (#19), stored in `%LOCALAPPDATA%\Unminute\models`. To keep models elsewhere, redirect that whole folder, not folders inside it: downloading, repairing or removing refuses a model folder that is a link or junction. Redirect only `models`, never `%LOCALAPPDATA%\Unminute` itself or `%LOCALAPPDATA%\Unminute\workspace`: the workspace must stay out of synced and network folders (D3). Since #219 the app follows a junction or link at either folder to its real location, and refuses to open the workspace when that is inside OneDrive (a folder the `OneDrive`, `OneDriveCommercial` or `OneDriveConsumer` variable names) or on a network drive. It says so without naming a folder and asks the user to contact IT; the installer's workspace check (run on every install and update) stops with exit code 6 and the same message, and `--health` reports `location`. Since #266 the same holds, with its own message, when either folder is a junction or link whose target cannot be reached (removed, or on a drive that is not connected): restore the target or remove the link. Redirecting `models` alone is allowed. Recordings in OneDrive are unaffected: they still import and can still be watched ([INTAKE.md](INTAKE.md)).

Users never need Python, Git, FFmpeg or GPU tools.

## Building

On a Windows build machine with 64-bit CPython 3.14 and network access:

```
python tools/build_package.py --out dist             (a development build)
python -I tools/build_package.py --out dist --release   (a release build)
```

`--release` refuses a working tree with any change or untracked file, a file hidden from `git status` (assume-unchanged or skip-worktree), an ignored file under `tools/` (such as `tools/__pycache__`, whose `.pyc` files Python could run instead of their source), and a HEAD that is not the commit of tag `vX.Y.Z` (a tag, not a branch of that name), whose `VERSION` must match. It resolves that commit once and reads what it packages and obeys from the commit itself: the app and installer files, the lock and the binary allowlist, as git stores them, with no git filter, attribute file or line-ending setting applied. `.cmd` and `.ps1` files get CRLF line endings, as `.gitattributes` says; every other file is written as stored. So no ignored, untracked, locally changed or hidden file can ship, and no git setting on the build machine changes a byte. The build script, `tools/windows_powershell.py` (which starts the Windows PowerShell that checks signatures) and the vendored wheel must also match the commit. The build loads that helper by its path beside the script and compiles it from the bytes it checks, never from `sys.path` or a cached `.pyc`, and the build stops without writing the zip if HEAD or the tree changes while it runs. It must run with `python -I` (it refuses otherwise), so no `PYTHON*` variable, user site-packages or stray file beside the script can change it. A file name in the commit that Windows would read as another path (a backslash, a colon or a control character) stops the build.

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
4. Copies the app and installer files (a release build takes them from the commit; `packaging/app-allowlist.json` lists the app's: the build stops if the copied app folder differs, and `tests/test_package.py` keeps the two the same) and precompiles every module in `runtime\lib`, `app\` and `runtime\sitecustomize.py` with the package's own Python (`compileall` in one process, unchecked-hash `.pyc`, hash seed 0, paths recorded relative to the package), so the same commit gives the same bytes. The build fails if a module has no `.pyc` or a `.pyc` holds the build folder's path. The app starts with `-I -B -X disable-remote-debug -m workspace`: `-B` means it never writes bytecode into its folder, and unchecked-hash means Python uses the shipped `.pyc` without reading the source first. Support commands below also use `-I -B`. The GPU component, unpacked from wheels without bytecode, is the one place Python still caches it (beside the component), so torch is not compiled again at every start: the GPU worker caches only the component's own modules and keeps `-B` for every other one (#206). Then it writes `version.json` and `model-manifest.json` (from the `workspace/asr_engines.py` being packaged).
5. Reads the Authenticode signature of every executable file and checks each against `packaging/binary-allowlist.json`, which lists every file with its signer. A file that is not listed, a listed one that is missing, a changed signer, a signature that is neither valid nor absent (for example a hash mismatch), or a signature that Windows found in this machine's catalogs rather than in the file fails the build. "Valid" depends on the build machine's certificate store and clock: a rebuild of an old version may stop if a bundled file's certificate has since been revoked, or has expired without a time stamp. When a dependency change adds, renames or re-signs binaries, rebuild with `--update-binary-allowlist` and review the list's diff in the pull request; a release build cannot rewrite it. This check and the runtime check in step 1 run Windows PowerShell 5.1 by its path in the Windows system folder, with modules only from the system and Program Files module folders and a minimal, allowlisted environment (no .NET profiler or `COMPlus_*` setting from the parent reaches it), whatever shell started the build (#235, #237).
6. Checks the licence texts in `packaging/licences/` (a release build reads them from the commit) and copies them to `licences/`. Then it writes `binary-inventory.json`, `THIRD_PARTY_NOTICES.txt`, `sbom.cdx.json` and `package-manifest.json`.
7. Zips the result and checks its size (MB, one decimal) and file count against `package_zip_mb` and `package_files` in `tests/budgets.json` (a release build reads them from the commit); over either, the zip is deleted and the build fails. Then it writes the `.sha256` file and `Unminute-<version>.cdx.json`.

### Licence texts (#176)

Some wheels ship no licence text, and some bundle libraries from other projects whose texts they leave out. Those texts are collected from each project's own release into `packaging/licences/`, one folder per component and version (`<id>-<version>/`), and listed in `packaging/licences/manifest.json`. Git stores them byte for byte (`.gitattributes`, `-text`).

Each manifest entry gives:

- `id`, `name`, `version`: the component, and the version the texts are for.
- `licence`: the licence as the component or its package states it, recorded as upstream says it (#176).
- `wheel`, `wheel_version`: the wheel that carries the component in the package, and its locked version.
- `covers`: empty for texts of the wheel's own code; otherwise the DLLs in that wheel the texts are for, as paths under `runtime\lib`. Each entry is a pattern or an exact path. In a pattern, a `*` may stand for part of a file name only (for example a delvewheel hash suffix), after at least three fixed characters, so no pattern can cover a whole folder. An exact path has no `*` and names one file, so it may be a short name or sit in a folder that starts with a dot (`torch/lib/uv.dll`, `sklearn/.libs/vcomp140.dll`; owner decision, 22 October 2026).
- `source`: the repository and the tag or version, with the full commit SHA where the texts come from a commit.
- `files`: each text's path, its SHA-256, and where it came from: a `url` (a GitHub raw URL must name the commit), or an `archive` (a source tarball, wheel or NuGet package, with its published SHA-256 and the `member` read from it).
- `in_wheel` (optional): `true` when the wheel's own texts already cover the DLLs in `covers`; such an entry has no files.
- `note` (optional): how the component was identified.

Any build fails if a text is missing, unlisted or differs from its SHA-256; if an entry's `wheel_version` differs from the version installed, so a re-lock that moves a wheel must check its components again (#180); or if a `covers` entry matches no DLL. A release also fails if any wheel or bundled DLL has no text (`LICENCE_COVERAGE_REQUIRED` in `tools/build_package.py`, on since the #180 re-lock; owner decision, 22 October 2026). A development build lists those gaps in its notices and on the console instead. After the #180 re-lock three DLLs are still without a text, so a release stops on them until they are resolved: NVIDIA's `cudnn64_9.dll` in CTranslate2 (to be removed after a GPU run shows it unused, #176 D2), and the Microsoft C++ runtime copies in `numpy.libs` and `pandas.libs` (#176 L8). Statically linked components are a follow-up (#176 D1).

`tools/collect_licences.py` collects the texts. It is a developer tool: the build and the app never run it, and it is not in the package. Edit the manifest entry first, then:

1. `python -I tools/collect_licences.py --list` lists the entries (no network).
2. `python -I tools/collect_licences.py <id> ...` fetches each text over https from the hosts in `LICENCE_HOSTS` only, with no credentials or port (every redirect included), checks an archive against its published SHA-256 before reading the member in memory (nothing is unpacked to disk or run), refuses anything that is not text (an HTML page, NUL bytes), and writes the texts and their SHA-256 only after every one has passed. Review the diff.
3. `python -I tools/collect_licences.py --verify [<id> ...]` fetches the texts again and compares them with the manifest and the committed files, writing nothing. An independent reviewer uses it to re-fetch a sample.

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
- **Installs side by side** to `%LOCALAPPDATA%\Programs\Unminute\versions\<version>`. It keeps the previous version for rollback and removes older ones, but only folders it made: a version name (below) holding a `version.json` that names that same version, never through a link (#297). It removes a junction or symbolic link found inside such a folder as a link and leaves what it points to. Any other folder in `versions` is left in place, and the installer says how many it left ("Warning: 2 folder(s) in the versions folder were not made by Unminute, so they were left in place.", on standard error with `-Silent`), without naming them. It copies the files into `versions\<version>.partial` and renames that folder to `versions\<version>` as its last step; installing the same version again first renames the live copy to `versions\<version>.old`, and removes that copy afterwards. A failed install does not change `current.json`, and the next run removes any `.partial` or `.old` folder left behind.
- **Waits for a file that is in use for a moment (#239).** Windows refuses to rename a folder while any program has a file in it open, and antivirus software such as Microsoft Defender can open newly written `.exe`, `.dll` and `.pyd` files for a moment to scan them. When either rename above is refused as access denied or in use, the installer tries again with a short back-off (a quarter of a second, doubling to at most 4 seconds) for up to 30 seconds from its first refusal, for each step, saying so unless `-Silent`, then fails with the same message as before (for example "Installation failed: Access to the path ... is denied.", exit 1). Any other error fails at once. It never falls back to copying (the owner's decision of 20 October 2026, #239). The uninstaller does the same when it removes `versions` and `current.json`.
- **Refuses to install while the app is running.**
- **Uses only an install folder that is its own (#297).** Before anything is written or removed, an existing `-InstallDir` must be empty, be an install (its `current.json` is a JSON object, not a list, whose `version` is a JSON string holding a plain version name, below, and whose `path`, if it has one, is `<InstallDir>\versions\<version>` as the installer writes it, letter case aside), or hold only what an install that stopped before writing `current.json` leaves: a `versions` folder holding only versions and `.partial` or `.old` folders the installer made, and `uninstall.ps1` beside a complete version. That last case lets a rerun finish a first install that was interrupted (#245, #239). Any other `current.json`, such as another program's, one that is not JSON, a list, or one that names no plain version or another folder's `path`, counts as a file the installer did not put there. Anything else stops the installer with exit code 2 and "The install folder already holds files that Unminute did not put there. Choose an empty or new -InstallDir.", and nothing is changed. A link or junction at `current.json`, `uninstall.ps1` or `versions`, or directly inside `versions`, stops it with exit code 2 in every case ("The install folder holds a link or junction that Unminute did not put there, so nothing was changed. Remove it, or choose an empty or new -InstallDir."). Neither message names a folder. An existing install keeps anything else in its folder: the installer does not remove it, though the uninstaller then removes nothing (below). An IT script that installs into a folder holding other files therefore now stops instead of installing; choose an empty or new folder.
- **Uses only plain version names as folder names (#297).** The package's version (from its `version.json`), and the `version` and `previous` values in `current.json`, must match the uninstaller's pattern `^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\z` (letters and digits, then also `.`, `+`, `_` or `-`, at most 64 characters; `\z` rather than `$`, which also matches before a final newline), must not end in a dot (Windows drops a final dot, so `0.6.0.` would name the folder `0.6.0`) and must not end in `.partial` or `.old`. A package whose version fails stops with exit code 4 before anything is written. A `current.json` whose `version` fails is not an install's record, so the folder is refused as above (exit code 2). A `previous` that fails is ignored: an update keeps no previous version for it, and `-Rollback` refuses with exit code 1.
- **Creates a Start menu shortcut,** "Unminute", and a per-user uninstall entry in Settings > Apps (registry key `Uninstall\Unminute`). The shortcut carries the taskbar identity `Unlimit.Unminute`, so taskbar pins group with the window. The install folder must not be, hold or sit inside `%LOCALAPPDATA%\Unminute` (the workspace and models), because uninstalling deletes the install folder: the installer and the uninstaller both refuse one that overlaps it, an empty path, and one starting with `\\` or `//` (a network, device or long-path form). A relative `-InstallDir` is taken from PowerShell's current folder. The uninstaller removes only what the installer put in the install folder (`versions`, `current.json` and `uninstall.ps1`): if the folder holds anything else, it removes nothing and exits 1. It also removes nothing, and exits 1, when the folder is neither an install (no `current.json` or `versions`) nor the folder its uninstall entry names. When the uninstall entry names another folder that still holds an install, it removes only this folder's own entries and keeps the shortcut and the uninstall entry, which belong to that other install. It decides this after removing the folder, by what is on disk, so the registered folder named another way (an 8.3 short name or a junction) is treated as the registered install. So a wrong `-InstallDir`, or an older install folder, cannot take the real install's shortcut and uninstall entry while leaving the program in place.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Installed |
| 1 | Failed |
| 2 | Prerequisites missing, or the install folder is not allowed (it overlaps `%LOCALAPPDATA%\Unminute`, starts with `\\` or `//`, is empty or is too long; or it already holds files the installer did not put there, or a link or junction where the installer would look, #297) |
| 3 | The app is running |
| 4 | The package failed verification, including a version that is not a plain folder name (#297) |
| 5 | The update is waiting for the workspace (see the message) |
| 6 | This version cannot open the existing workspace |

## Updates (#26)

Self-service users and IT use the same route: run the new package's installer (`install.cmd`, or `install.ps1 -Silent` from Intune).

**From 0.4.3 or earlier, this is not an update in place (#136).** The rename gave the app new data folders, a new install folder, uninstall entry and taskbar identity, and a new name for the saved token, with no migration, so the new version starts with an empty workspace and the installer's workspace check does not see the earlier one. In order:

1. In the earlier version: hand off or copy what is still needed, confirm the cleanup of finished meetings, check holds with whoever manages them, and remove the Hugging Face token, because no later version can reach it (in 0.4, Settings > Manage downloads > Remove the remembered token; in 0.2 or 0.3, Setup > Remove the remembered token). Settings, handoff templates and watched folders (with each folder's delete-originals authority) do not carry over either: note the settings and watched folders, and copy the text of any handoff template that was edited or written.
2. Uninstall the earlier version.
3. Install the new version. If IT manages updates, pass `-Managed` again on this first install: the setting was kept in the earlier version's install folder, which the new version does not use.
4. In the new version's Setup, download the models again and enter the token again. Set up the settings, handoff templates and watched folders again; when re-adding a watched folder, leave "Also import recordings already in it" unticked, so recordings finished in the earlier version are not imported again. If the graphics card was used, download the components that were used ("Graphics card acceleration", "Graphics card for Whisper" or both) again and turn "Use the graphics card" back on.

The earlier version's workspace and models stay in their old folders, `%LOCALAPPDATA%\whisper\workspace` and `%LOCALAPPDATA%\whisper-models`, until deleted; the workspace may hold real meetings and holds, so delete it only once nothing in it is still needed. A token left behind (for example after uninstalling first) is under the earlier name, `WhisperTranscriber/HuggingFaceToken`: remove it in Windows Credential Manager (Windows Credentials, Generic). Installed the other way round, Settings > Apps shows two Unminute entries, an old taskbar pin still starts the earlier version on its own workspace, and uninstalling the earlier version afterwards removes the new Start menu shortcut: install the new version again to restore it, and pin it again.

Installing with `-Managed` records that IT manages updates. The app then shows "Updates are managed by your IT team" instead of self-service instructions, and later updates keep the setting.

Before switching version, the installer runs the **new** version's own check, `python -m workspace --gate`, against the existing workspace:

| Workspace | Result |
| --- | --- |
| None yet | Installed. |
| Same storage format (schema) | Installed. Everything is kept: meetings, edits, holds, settings. |
| New storage format, and the workspace holds no meetings, no active retention holds, no unfinished cleanup, no microphone recording waiting to be imported and no meeting audio files | Installed. Settings, watched folders, the workspace's encryption key and the minimal cleanup receipts are carried over when the app next opens. |
| New storage format, and the workspace still holds any of those (or a meeting's key or recording key left without its meeting) | **Waits.** Exit code 5, with the reasons, for example "2 retention holds are active. Holds never expire on their own…" or "Meeting audio files are still in the workspace.". Nothing is changed. A meeting audio folder (`workspace\media\<id>`) that belongs to no meeting, for example one an earlier version left, is never removed by the app; the reason then says so and gives IT's step: if nothing requires keeping it (no retention hold or other reason applies), delete it from the workspace's `media` folder and run the update again. |
| Older storage format than the workspace (a downgrade) | Refused. Exit code 6. |
| A workspace that cannot be read (damaged, locked, or Windows cannot tell whether it is there) | **Waits.** Exit code 5. It is never treated as "none yet". |
| A workspace location the app refuses: `%LOCALAPPDATA%` missing, in OneDrive or on a network drive, or `%LOCALAPPDATA%\Unminute` or its `workspace` folder redirected there (#219) or linked to a folder that cannot be reached (#266) | Refused. Exit code 6, with the app's message. |

Rules:

- No update ever expires a hold, deletes content or makes a backup of meeting content to let itself proceed.
- Every update waits while the app is running, so nothing is processing or cleaning up during an update.
- In Intune, map exit code 5 to "Retry". The device gets the update once the people responsible have finished their meetings and released their holds.
- Deploy in **user** context. The gate checks the signed-in user's workspace, and in System context it would see none.
- A workspace that exists but cannot be read (damaged or locked) also makes the update wait, with exit code 5. It is never treated as a fresh install.
- The check reads only plain counts and needs no encryption key (#111 C2b-2), so it works, and gives the same answer, when Windows cannot unlock the user's key. A first start whose key cannot be protected leaves no workspace, so the check sees none.
- Schema 2 (encryption at rest, #111 phase C) ships as one release, once phase C is complete (through C7; [RELEASING.md](RELEASING.md), "Before cutting a release"). A version before it cannot open a schema-2 workspace (exit code 6, as for any downgrade); going back means moving the workspace folder aside first.
- `-SelfService` switches an IT-managed installation back to self-service updates.

Settings > Updates in the app shows the deployment mode and anything that would hold up a storage-format update.

### The install record, `current.json` (#111 B5a)

Every install and rollback writes `current.json` in the install folder. Since B5a it is format 2:

```
{"format": 2, "version": "0.7.0", "path": "<InstallDir>\\versions\\0.7.0", "previous": "0.6.1", "managed": true,
 "slots": {"0.6.1": {"schema": 2, "seq": 8, "installed": "2027-01-10T22:00:00Z"},
           "0.7.0": {"schema": 3, "seq": 9, "installed": "2027-02-02T01:00:00Z"}}}
```

- `version`, `path`, `previous` and `managed` are as before. The uninstaller and the app's deployment check read only these, so they read either format.
- `slots` lists each kept version the installer made. `schema` is the storage format the version's own `version.json` states: a package built since B5a carries it (read from `workspace/store.py` at build time, never by importing the app), and a version from before B5a states none, so it is recorded without one and is never handed a workspace. `seq` is its install order: one more than the highest so far when the version is first installed, and kept when it is installed again, so "newest" never depends on comparing version names. `installed` is when it was last installed or repaired (UTC).
- A `current.json` without `format` (written before B5a) is read as before. The next install or rollback writes format 2 and numbers the previous version first.
- `-Rollback` writes format 2 too, keeping each version's number and install time.
- Per-user retention is unchanged: the current and the previous version.
- It is still written in place. Writing it atomically is part of B6a.

### Launch-time hand-over (machine mode, #111 B5a)

Per-user installs keep the install-time gate above (exit code 5; the owner's decision B5-1 of 21 October 2026). Machine mode (B6, not built yet) installs as SYSTEM, which sees no user's workspace, so there the decision moves to each user's start: the app reads `current.json` and the storage format and blockers of the user's own workspace (plain values, no key), and opens the current version, or, while an older workspace still holds anything the gate counts, the newest kept version of that workspace's format (finish-only mode follows in B5b, which lands before B6b turns machine mode on). A workspace from a newer version is never handed to one (strict withdrawal, the owner's decision B5-8): the user sees "ask your IT team" until a fixed version is installed. A workspace that cannot be read always opens the current version. Another version is started only by its real path, as a plain folder directly inside `versions` whose `version.json` matches the record; anything else starts nothing and shows a message naming IT. The rules are in [WORKSPACE_DESIGN.md](WORKSPACE_DESIGN.md), "Launch". In a per-user or source install the pass does nothing.

Machine-mode retention (applied by B6b, the owner's decisions B5-3 and B5-8 of 21 October 2026, "bounded disk"): besides the current and previous versions, one older version is kept for an earlier storage format: the newest version of the storage format immediately before the current one (the owner's decision of 22 October 2026). It is replaced at the next storage-format change rather than kept for every format. A user whose workspace is still on a format no kept version uses, with content in it, therefore has no version to open it, and sees the IT message with the blockers' counts. A downgrade or withdrawal removes the newer version. The uninstall refusal for the one kept older version (B6-11) still applies.

To go back to the previous installed version, run:

```
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Rollback
```

It runs the same check with the previous version, so it refuses if that version cannot open the workspace (exit code 6). This happens, for example, after a newer version has changed the storage format. It also refuses, with exit code 1 and before running anything, when `current.json`'s `previous` is not a plain version name (a `version` that is not one stops it earlier, with exit code 2, as above), or the previous version's folder is not one the installer made (a link, or no `version.json` naming that version) (#297).

Use `-Rollback` from the current version to go back, rather than running an older package's installer over a newer install.

## Uninstalling

Use Settings > Apps > Unminute, or `uninstall.ps1` (with `-Silent` for unattended removal).

In order, the uninstaller:

1. Checks the install folder (see "Creates a Start menu shortcut" above) and that the app is not running (exit 3 if it is). A link or junction at `versions`, `current.json` or `uninstall.ps1` also stops it, with exit code 1 (#297). A refusal removes nothing.
2. Puts the Windows lid setting back if the app left it at "Do nothing" (#191). This runs only when `power_restore.json` is in the workspace, which happens only if the app closed unexpectedly during a recording with **Keep recording when the lid is closed** on, and did not start again. It runs the current version's own runtime once, as the installer runs the app (`runtime\python.exe -I -B -X disable-remote-debug -m workspace --restore-lid`, from its `app` folder), with no window and a 60-second limit. The app applies its usual restore rules: only a valid record, only a side still at "Do nothing", and only the scheme's own value, never a policy setting. It runs only when the version's `version.json` lists the `restore-lid` feature and `current.json` names a plain version folder; an older version would take the switch for an ordinary start. The switch prints `{"lid": state}`, which the uninstaller reads but does not show. It exits 0 when the setting was put back or nothing was waiting, 1 when it could not be put back now (the record is kept, and the app puts it back at its next start if it is installed again), 2 when the record was damaged (the setting is left as it is) and 3 when the app is open. Whatever the result, even if the runtime is missing or the limit is reached, the uninstall carries on, and its exit code does not change. Any result other than 0 adds one fixed line ("Warning: Unminute could not put back what closing the lid does...", on standard error with `-Silent`) telling the user to check the setting in Windows Settings > System > Power & battery > Lid & power button controls.
3. Removes the program, then the shortcut and the uninstall entry. It removes the program's folders with the installer's own walk, which never follows a link: a junction or symbolic link inside a version is removed as a link, and what it points to is kept (#297). A file in the program's folder that is in use for a moment, such as by a virus scan, is waited for, for up to 30 seconds from the first refusal for each of `versions` and `current.json` (see "Waits for a file that is in use" above). If it is still in use, the uninstall stops with "Removal failed: ..." (exit 1), keeps `current.json`, the shortcut and the uninstall entry, and can be run again.

A rollback needs no restore of its own: every version from the rename on (#136) has the same restore file and rules, so the version rolled back to puts the setting back at its next start (the user's decision of 18 October 2026, #191).

Uninstalling removes the program, the shortcut and the uninstall entry. It does not remove:

- **The workspace** (`%LOCALAPPDATA%\Unminute\workspace`): meetings, holds, settings and receipts. Meeting content is only ever removed through the app's confirmed cleanup steps, which honour retention holds. The uninstaller says when a workspace remains.
- **Downloaded models** (`%LOCALAPPDATA%\Unminute\models`).
- **The Hugging Face token** (`Unminute/HuggingFaceToken` in Windows Credential Manager).

To remove those for a person who leaves, offboard first (next section).

## Offboarding (#111 B8a)

**Not in v0.5.0-preview.3.** Two options of the installed runtime, run **as the user** from the current version's `app` folder, as the uninstaller runs `--restore-lid`: `runtime\python.exe -I -B -X disable-remote-debug -m workspace --offboard-report`, then `--offboard`. The owner's decisions of 21 Oct 2026 (B8-1 to B8-8, with B8-3 replaced on 22 Oct 2026) set what they do. They do not look at anything version 0.4.3 or earlier left (its folders or its token). An uninstall switch that runs `--offboard` and then removes the program, its uninstall entry, its Start menu shortcut and its pins (`uninstall.ps1 -Offboard`) is B8b. For the IT steps around them, see [IT_DEPLOYMENT.md](IT_DEPLOYMENT.md), "Offboarding".

`--offboard-report` changes nothing and prints one line of JSON, counts and yes or no only (no path, name or content):

- `result`: `ok` (nothing to remove, or all of it removable), `others_left` (as `ok`, but other entries in `Unminute` stay, below), `blocked`, `running`, `refused`, `system_account` or `account_unknown`. The last three print nothing else: nothing was read.
- `workspace`: `none`, `empty`, `locked_empty` (the key was locked at the app's last start; removed without the key) or `blocked`.
- `blockers`: the update gate's kinds with their counts (`holds`, `meetings`, `cleanup`, `recordings`, `media`, `content`), or `unreadable` when the database cannot be read (never treated as empty).
- `leftover`: folders a removal that stopped part-way left (`workspace.offboarding-<32 hex>`), finished first by the next `--offboard`.
- `others`: how many other entries are in `%LOCALAPPDATA%\Unminute` (anything but `workspace`, `models` and the leftovers above), for example a workspace renamed by the steps in [WORKSPACE_GUIDE.md](WORKSPACE_GUIDE.md) ("If Unminute finds damage"). Offboarding never opens, names or removes them: one may still hold meetings (C1). `null` when `Unminute` cannot be listed.
- `models_mb` (rounded up; 0 for a folder IT redirected), `models_link`, `token`, `lid_record` (`power_restore.json` is waiting), `running`.

Exit codes: 0, 2 (Local System, or Windows cannot say who runs it), 3 (the app is open: its single-instance mutex in this session, the run marker's process in any session, or B5's workspace lock), 4 (`others_left`: `others` is above 0 or `null`), 5 (blocked) and 6 (a junction or link, or a file, at `Unminute` or `Unminute\workspace`, or a location the app itself refuses: synced, on a network drive, or a broken link).

`--offboard` prints the same fields and exits with the same codes, removing nothing for 2, 3, 5 and 6. Other entries in `Unminute` do not stop it: it removes what is its own, leaves them, and ends with `others_left` and exit 4 instead of `offboarded` and 0. When nothing blocks, in order:

1. Finishes any `workspace.offboarding-*` folder a stopped run left.
2. Takes B5's workspace lock (`instance.lock`) and holds this session's single-instance mutex, so the app cannot open the workspace meanwhile: since B5a the app takes this lock at its start, in any Windows session of the user, before it opens the database. A version from before B5a does not take it; the last look in step 5 covers that.
3. Puts the lid setting back with the app's own rules (`power.restore_only`). Exit 1 (`step` `lid`) when it cannot now; a damaged record is left as it is, as the uninstaller does.
4. Writes an `offboarded` audit record (event ID 1700; `locked`, `models` and `token`, each true or false) and its Event Log copy. Exit 1 (`step` `audit`) if either fails, including a copy still being reported when the audit log closes (it then records a `mirror_gap`): the Event Log copy is the lasting record once the audit file is gone (B8-4).
5. Looks again for an open app or anything new to block it, then erases the root key: the `crypto_meta` row is deleted with SQLite's secure delete and the write-ahead log is checkpointed and truncated (`store.erase_root_key`, needing no key). From here a stopped run leaves nothing that can be opened, and the app would open the workspace locked. Exit 1 (`step` `key`) if the log stays busy. Then it releases the workspace lock, because Windows will not rename a folder with a file open in it. An app started in that moment finds no root key, opens the workspace locked and holds its files, so the rename below fails (`step` `rename`) and a rerun with the app closed finishes.
6. Renames the workspace to `workspace.offboarding-<32 hex>`, then deletes it with a walk that removes junctions and links as links and never follows them. A file in use is waited for, for up to 30 seconds (as the installer does, #239), then exit 1 (`step` `rename` or `workspace`); running it again finishes.
7. Removes the models folder (a junction or link there: the link only, its target kept), then the token, then `%LOCALAPPDATA%\Unminute` if it is empty.
8. Prints the result (`offboarded`, or `others_left` with exit 4 when `others` is above 0 or `null`), what was removed (`removed`: `workspace`, `models`, `token`) and `residuals`: fixed sentences on what it cannot reach (copies outside the app, plain text an SSD may keep from before encryption at rest, the Event Log copy).

With Intune, run it as a script or remediation with the logged-on user's credentials, as for `--health`; whether such scripts run with the user's profile loaded on the managed baseline is to confirm (#189). Machine mode (B6) runs it from the machine install's current version the same way, never as Local System.

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
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\Programs\Unminute\uninstall.ps1" -Silent
  ```
- **Return codes:** map 5 (the update is waiting for the workspace, see "Updates") to Retry, and 1, 2, 3, 4 and 6 to Failed. Code 3 (app running) is worth a retry.
- **Detection rule:** Registry; key path `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Uninstall\Unminute`; value name `DisplayVersion`; detection method "String comparison", equals, `<version>`. Associated with a 32-bit app on 64-bit clients: No. The key is new with the rename (#136), so a Win32 app for 0.4.3 or earlier still counts as installed after the new one is. Supersede it with the option to uninstall the previous version. That Intune then runs the earlier uninstall before the new install is still to be confirmed on a managed device (tracked in #189). Do not deploy the earlier version's uninstall as a separate assignment: two assignments run in no set order, and if the new version installs first, the 0.4.3 uninstaller removes the new Start menu shortcut while detection still reports the new version installed, so the shortcut only comes back by installing the new version again. Either way, have users finish step 1 under "Updates" (hand off, cleanup, holds, token) before the earlier version's uninstall runs. If the earlier version was installed with `-Managed`, add `-Managed` to this release's install command too: the setting does not carry over to the new install folder.
- **Requirements:** Windows 10 1809 or later, x64. If WebView2 cannot be assumed, deploy the WebView2 Evergreen runtime as a dependency, and keep it updated (see the prerequisites above).

For support, `runtime\python.exe -I -B -m workspace --self-test` (in the installed version folder) imports every dependency, confirms the network guard and checks that the speaker model loader refuses a synthetic code-running checkpoint and ones that change a shared Enum member's attributes or its class (`workspace/speaker_load.py`), and that Windows can protect and unlock a random key for this user (`dpapi round trip`, the way the workspace's root key is kept). `--status` prints workspace counts only, never content, plus `integrity` (the last database check after an unclean close: `ok`, `failed`, `skipped` when it could not run, which the next start retries, or `unchecked`), `readable` (false when the database cannot be read at all), `recordings` (recordings not yet in a meeting), `keys` (the workspace key's state at the app's last start: `ok`, `locked` or `unchecked`, read from `logs\keys.json`, never by unlocking the key), while locked `lock_reason` (`windows`, `key`, `protector` or `missing`) and `lock_code` (the Windows error number), `audit_seq` (the audit log's newest sequence number, read from its end only, or null; #111 C5), and the retention reminders (#111 C6): `reminders_due` and `action_required` (meetings showing "Review due" or "Action required" under the thresholds in force) and `held_overdue` (held meetings whose step is past the threshold and quiet only because of the hold). These counts need no key, so they work while the workspace is locked. See "If Unminute cannot unlock its workspace" in [WORKSPACE_GUIDE.md](WORKSPACE_GUIDE.md). See "If Unminute finds damage" in [WORKSPACE_GUIDE.md](WORKSPACE_GUIDE.md).

For an Intune Remediation detection script, run `runtime\python.exe -I -B -m workspace --health` from the user's installed version folder, with **Run this script using the logged-on credentials** set to **Yes**. Never run it as SYSTEM: the install is the user's own and the user can change its files, so a SYSTEM run would run the user's code as SYSTEM, and SYSTEM sees no user's workspace. Run as SYSTEM, it reports `"workspace": "system_account"` and exits 1.

It prints one line of JSON (well within Intune's 2,048-character output limit) and exits 0 when healthy, 1 when it needs attention (Intune's "issue detected"). It never prints an error message or a traceback. Fields:

- `version`.
- `workspace`: `ok`; `none` (this user has not opened the app yet; still healthy); `locked` (Windows would not unlock the workspace key at the app's last start, read from `logs\keys.json`, never by unlocking the key; it takes precedence over `damaged`: see "If Unminute cannot unlock its workspace" in [WORKSPACE_GUIDE.md](WORKSPACE_GUIDE.md)); `damaged` (the last database check found damage: see "If Unminute finds damage" in [WORKSPACE_GUIDE.md](WORKSPACE_GUIDE.md)); `location` (the app data location is missing, synced or on a network drive, or the `Unminute` or `workspace` folder is redirected there or is a link to a folder that cannot be reached); `unreadable` (also when Windows cannot say which account it runs as); `system_account`.
- `running`: the app is open, asked of Windows through its single-instance lock and the process it recorded at start, so from any session.
- `last_run`: `clean`, or `did_not_close` when the app last ended without closing and has not been opened since (its next start records that as a crash).
- `crashes_7d`: crashes (including runs that ended without closing) and worker or download processes that ended by themselves in the last 7 days, each counted once, from the content-free logs.
- `schema`: the workspace's storage version, or null.
- `action_required`: meetings whose next step has waited `ActionRequiredDays` or more (0 when that policy is not set), and `held_overdue`: held meetings past the threshold (`ActionRequiredDays`, or `ReviewReminderDays` when action required is off) that a hold keeps quiet (#111 C6). Both are null when they were not counted (for example run as SYSTEM, or when the workspace could not be read), never 0 by default. They do not change `healthy`: a backlog of meetings is not an app fault. To alert on them, make the detection script also exit 1 when `action_required` is above 0, and keep **Run this script using the logged-on credentials** set to **Yes**: run as SYSTEM, `--health` reports `system_account` and counts nothing.
- `healthy`: `workspace` `ok` or `none`, `last_run` `clean`, and no crashes.

It reads counts and the logs only, never content. It writes nothing of its own, though opening the database read-only can leave SQLite's `-wal` and `-shm` files beside it. A user can add the detail with Help > **Copy diagnostics**.

### The audit log and its Event Log copy (#111 C5)

The app keeps a content-free, hash-chained audit log in `workspace\audit\` (what it records: "Audit log" in [WORKSPACE_DESIGN.md](WORKSPACE_DESIGN.md)) and copies each record's hash to the Windows **Application** log:

- **Source** `Unminute`, not registered (installs are per user), so Windows shows the text without a description and a SIEM reads it from `EventData`. B6 (per-machine installer) can register the source with a message file.
- **The entry's text**: `Unminute audit v1 chain=<32 hex> seq=<n> kind=<kind> sha256=<64 hex>`. No meeting, hold, job, folder or recording id, and no content.
- **User**: the signed-in user's SID, which the app passes with each entry and Event Viewer shows as the account name. Whoever writes an entry supplies its SID, so it attributes the app's own entries but does not prove who wrote one.
- **Event IDs and levels**: 1000 `chain_start` (a warning when the chain restarted because the folder was missing or unreadable), 1001 `app_start`, 1002 `locked`, 1003 `damage_continued`, 1100 `meeting_imported`, 1110 to 1112 `recording_started`, `recording_ended`, `recording_deleted`, 1200 `transcript_reviewed`, 1201 `final_saved`, 1210 `meeting_discarded`, 1300 to 1303 `cleanup_started`, `cleanup_done`, `cleanup_confirmed`, `cleanup_retry`, 1400 to 1402 `hold_placed`, `hold_released`, `hold_kept`, 1500 to 1502 `handoff_copied`, `handoff_saved`, `assistant_opened`, 1600 to 1602 `folder_added`, `delete_authority`, `folder_removed`, 1700 `offboarded` (B8a, the last record before offboarding removes the workspace), all information; and warnings 1900 `tail_damaged`, 1901 `write_failed`, 1902 `segment_dropped`, 1903 `mirror_gap`, 1904 `suppressed`.
- **Always on**, with no policy key (the owner's decision, 21 Oct 2026).
- **For the stronger record, forward the Application log off the device** (Windows Event Forwarding or a SIEM; the D1 IT guide). On the device, a standard user can typically write to the Application log, and its default retention overwrites the oldest events when it is full, so the user whose actions are recorded could push the entries out by filling it: without forwarding, the copy detects casual edits of the file only. Standard users' rights to the Application log and its size and retention on the managed baseline are to confirm (#189).

To check the log, run `runtime\python.exe -I -B -m workspace --verify-audit` from the user's installed version folder, **as the signed-in user** (as for `--health`). It needs no key and reads no database. It prints one line of JSON with counts and states only:

- `result`: `ok`; `broken` (with `broken_at`, the sequence number, and `reason`: `link`, `sequence`, `format`, `torn`, `chain_start` or `front_truncated`, when old segments were deleted by hand rather than dropped at the 16 MB cap: the drop records' segment numbers skip one, or do not reach the oldest kept segment); `empty`; `unreadable`; `location` (as `--health`); `system_account` (run as SYSTEM, nothing is read); `account_unknown` (Windows could not say who runs it, nothing is read).
- `records`, `chains` (chains started in the kept log), `starts` (those chain starts by reason: `first_use`, `missing`, `unreadable`; treat `missing` or `unreadable` as history that is gone, the folder deleted or its end unreadable, except one `missing` on a workspace made before the audit log existed), `segments`, `first_seq`, `last_seq`, `head` (the newest line's SHA-256) and `tail` (`unfinished` when the newest line has no line end yet: the app may be writing it).
- With `--event-log`, `event_log` compares the file with the signed-in user's `Unminute` entries in this computer's Application log (`wevtutil`, from the system folder; entries with another account's SID, or none, are left out and counted in `other_accounts`); with `--event-log-file <path>`, with an exported `.evtx` file instead, for example from a forwarded copy, compared whole. Its `result` is `match`, `differs`, `no_log` (no entry names a chain in the file) or `unreadable`, with counts; or, with no counts, `no_file` (the `--event-log-file` path is not a file) or `account_unknown` (with `--event-log`, the signed-in user's SID could not be read, so the log is not read rather than compared with every account's entries). The account is read once, for both the SYSTEM refusal and this filter. The counts: `entries`, `missing_in_log` (records with no entry, outside a recorded gap, from the oldest record that has one, across chains, so records made up under a new chain count; after a crash or ending the app in Task Manager the last few records may have no entry), `missing_in_file` (entries for records the file no longer has: a tail cut by N lines gives N), `mismatched` (same number, another hash: an edited or rebuilt chain) and `unmatched` (entries that do not parse: events someone else wrote under the source name), any of which makes it `differs`; `other_chains` (well-formed entries naming a chain the file does not have) and `other_accounts`. With `--event-log` the remaining entries are the signed-in user's own, so `other_chains` also counts as `unexplained_chains`, which makes it `differs`: the audit folder deleted (and no app start since), or replaced by a fresh chain that starts `first_use`. They are not counted as `unexplained_chains` when the file holds a chain start of `missing` or `unreadable` (the app recorded that the history before it was lost; read it with the chain check's `starts`). A workspace deleted whole and made again also starts `first_use`, so the earlier chain's entries make it `differs` too: the history went without the app recording why. With `--event-log-file`, which may hold other devices' chains, `other_chains` never makes it `differs`: read it with the chain check's `result` and `starts` (a deleted folder shows as a `missing` start, deleted earlier segments as `front_truncated`).

`verify` alone cannot see whole lines removed from the end of the log, a rebuilt chain, or the whole folder deleted (only a `missing` start): those show only against the Event Log copy, while it still holds the entries. The comparison does not see a file rebuilt by hand that itself records a `missing` or `unreadable` start (the chain check's `starts` still shows that history as lost), other chains in an exported file, or entries that are no longer in the log. A `missing` or `unreadable` start that was itself later dropped at the 16 MB cap no longer explains the earlier chain's entries, so those then make the local comparison `differs`; so do a packaged check's entries when it ran the app as the same account (under its own throwaway `LOCALAPPDATA`, with its own chain).

It exits 0 when the chain is `ok` (and, if compared, matches), 1 otherwise.

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
| `AllowModelDownloads` | DWORD 0/1 | Allowed | 0: the app refuses model downloads and repairs; Setup and Downloads show "Ask your IT team" instead of a Download button, and a model that is not ready (in Settings, Transcribe again, or a meeting or speaker identification that stopped because of it) names the IT team instead of Manage downloads |
| `AllowGpuComponent` | DWORD 0/1 | Allowed | 0: neither graphics card component (for speaker identification, and for Whisper, #248) can be downloaded or used; speaker identification and transcription run on the processor |
| `AllowMicrophoneRecording` | DWORD 0/1 | Allowed | 0: microphone recording is off and cannot be turned on; a recording in progress is stopped and saved when the app reads the change; imports still work |
| `AllowComputerAudioRecording` | DWORD 0/1 | Allowed (each user's switch is still off by default) | 0: computer audio (the sound playing on the computer, such as the other side of a call) cannot be recorded or turned on; a recording in progress carries on with the microphone only from when the app reads the change. Never applies when `AllowMicrophoneRecording` is 0. Captured with Windows loopback, with no driver, virtual cable or Stereo Mix. The recording support is in place but no version yet shows the switch (#108) |
| `AllowLidOverride` | DWORD 0/1 | Allowed | 0: "Keep recording when the lid is closed" cannot be turned on, and a lid action the app set to "Do nothing" is put back when the app reads the change. The app never changes a lid action that Group Policy or Intune sets (#172) |
| `AllowLiveDrafts` | DWORD 0/1 | Allowed | 0: draft text while recording (#104, off unless the user turns it on) cannot be turned on; a draft in progress is stopped and its text cleared when the app reads the change, and the recording carries on. Not yet shown on the page: until it is, the setting has no Settings switch and the app refuses to turn it on |
| `RequireRecordingConsent` | DWORD 0/1 | Required | 0: recordings start without the confirmation that everyone has been told. A bad value keeps the confirmation |
| `AllowWatchedFolders` | DWORD 0/1 | Allowed | 0: no folders can be added, existing watched folders are not checked, and recordings already found in them wait, from when the app next reads the policy (within 15 minutes; the one being copied in at that moment finishes); imports by hand still work |
| `AllowDeleteOriginals` | DWORD 0/1 | Allowed | 0: no folder can be given authority to delete originals, and originals are kept even where authority was given before. Independent of `AllowWatchedFolders`: with watched folders off, originals that already have authority are still deleted after the user confirms cleanup (agreed 17 Oct 2026, #152) |
| `AllowNetworkSources` | DWORD 0/1 | Refused | 1: recordings on mapped network drives, or reached through a symbolic link, can be imported. `\\server\share` paths and watched folders on network drives stay refused |
| `HandoffFilenameIncludesTitle` | DWORD 0/1 | Date and time only | 1: Save suggests the meeting title as the file name, except when speaker names are replaced |
| `ClipboardClearMinutes` | DWORD 0 to 1440 | Never cleared | N: a handoff copied with the app's Copy button is cleared after N minutes (time asleep counts) and when the app closes, unless something else was copied since. The Copy message tells the user |
| `ReviewReminderDays` | DWORD 0 to 365 | 30 days | N: a meeting whose next step has waited N days or more shows "Review due" on the Meetings page, with a banner, while the app is open; 0: off. Each step counts from when it began (review from the import, handoff from the review confirmation, a released hold's cleanup from the release, an unfinished cleanup from its confirmation). Nothing is blocked or deleted. A step whose cleanup a hold blocks is not shown. Disabling the policy in Intune gives the 30-day default; to turn it off, set 0 |
| `ActionRequiredDays` | DWORD 0 to 365 | Off | N: a meeting whose next step has waited N days or more shows "Action required" and is listed first, and the banner says the organisation asks for each step to be finished within N days. Nothing is blocked or deleted. If N is less than `ReviewReminderDays`, "Review due" is never shown. `--status` and `--health` count these meetings (`action_required`) and held ones past N (`held_overdue`) |
| `HandoffDefaultFolder` | REG_SZ or REG_EXPAND_SZ | The dialog's usual folder | The Save dialog starts here. A full drive-letter path (a mapped drive is fine); `\\server\share` paths are not accepted. A folder that does not exist is ignored. In a REG_EXPAND_SZ value, `%NAME%` variables are expanded as Windows expands them (`$NAME` is left as it is), and a variable that is not set is left as written |
| `AllowHandoffAssistant` | DWORD 0/1 | Allowed | 0: the Hand off page has no "Copy and open" button and looks as it did before #109 (Copy prompt and transcript is the main button). When allowed, the main button copies the prompt and transcript, then asks Windows to open the assistant's address in the default browser; the user pastes and sends. Nothing is put in the address or sent by the app |
| `HandoffAssistantName` | REG_SZ, 1 to 30 characters | ChatGPT | The name in "Copy and open [name]" and its messages. Letters, digits, single spaces and `. - & ' ( ) +`. Set together with `HandoffAssistantUrl` |
| `HandoffAssistantUrl` | REG_SZ, at most 300 characters | `https://chatgpt.com/` | The address opened after copying: `https://` in lower case, then a lower-case host name of at least two parts (not an IP address or a single name such as `localhost`, no trailing dot; write internationalised names in their `xn--` form), with no user name, password or port other than 443, then an optional path and query. Each part of the host name is 1 to 63 letters, digits and hyphens, and does not start or end with a hyphen (so no underscore), and the whole host name is at most 253 characters. A port, if given, is written exactly as `:443` (not `:0443`). The app fails closed: any of these anywhere in the address makes it not valid, and the button is not shown: an `@` (also in the path or query, so a link that carries an email address cannot be used), `#`, a space, tab, line break or other control character, a non-ASCII character, `"` `'` `<` `>` `\` `^` `` ` `` `{` `\|` `}`, a `%` not followed by two hexadecimal digits, `%00` to `%1F` and `%7F`, a `:` with no port number after the host, and a `?` with nothing after it. Settings shows the host. Set together with `HandoffAssistantName` |

A value of the wrong type or out of range is treated as not allowed for the `Allow` values, and as not set for the others, with one exception: `HandoffAssistantName` and `HandoffAssistantUrl` are set together, and if only one is set, or either is not valid, the "Copy and open" button is not shown (the app never falls back to ChatGPT when IT meant another assistant; Settings says the setting is not valid, and a click that finds it so says to ask IT). Setting either policy to Disabled in Intune is expected to remove the value, which counts as not set (to confirm). If the key exists but cannot be read, every `Allow` value is treated as not allowed. Settings then says some settings are managed, and that the Hand off assistant setting could not be read (ask IT), rather than that it was turned off. Value names are not case-sensitive; a name the app does not know (for example a misspelling, a trailing space or a lookalike letter from another alphabet) grants and withholds nothing, so check names against the table. The app keeps the first 20 such names, never their data, for Settings (each character other than printable ASCII shown as an escape, so a trailing space reads `\x20`); Copy diagnostics gives only how many there are. Settings does not show the names yet (#300). Policy values are machine-wide: they apply to every user of the computer.

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

- Only per-user installation is provided. A per-machine (Program Files) installation that standard users cannot change is planned (#111 B6; [IT_DEPLOYMENT.md](IT_DEPLOYMENT.md), "Not available yet").
- A user can always install a per-user copy. Once the per-machine install (B6) ships, a current per-user installer refuses when a machine-wide install exists, but an older package's installer has no such check. On managed computers, IT stops per-user copies with application control (AppLocker or App Control for Business): example rules, not yet tested, are in [IT_DEPLOYMENT.md](IT_DEPLOYMENT.md#stopping-per-user-copies-on-managed-computers) (#304).
- On the development machine (13 October 2026, Windows 11, while another heavy app was running), the 0.2.0 build was checked end to end:
  - **Build:** a 330 MB zip containing 27,746 files.
  - **Install:** `-Package` mode, verifying the checksum and every file, took 244 s.
  - **Self-test:** every dependency imported from the private runtime, `PYTHONPATH` was ignored, and the network guard blocked outside connections.
  - **Inference:** Parakeet transcribed and pyannote separated two speakers, offline, through the managed worker.
  - **Window:** the app opened ("Unminute", with its WebView2 child) using an isolated profile.
  - **Uninstall:** removed the program and its uninstall entry, and kept the models.
- Testing on a clean standard-user Windows profile, and on a managed Intune device, is still to be done. It is tracked in the validation tracker (#189).
- Paths inside the package are kept to 140 characters or fewer. The default install folder has room for user names of up to about 50 characters, and the installer refuses install folders longer than 92 characters. Windows PowerShell 5.1 cannot handle paths over 260 characters.
