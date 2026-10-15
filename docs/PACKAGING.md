# Windows package for Unminute

This guide covers the preassembled package from #11:

- building it;
- installing it per user, either interactively or unattended;
- managed deployment with Intune;
- signing and trust.

Updates and the empty-workspace migration gate are #26.

The legacy source app (`install.cmd` at the repository root) is unchanged and still works as before.

## What the package contains

One versioned zip, `Unminute-<version>.zip`, and its checksum file, `Unminute-<version>.zip.sha256`. Releases up to v0.2.3 used the earlier name, `WhisperWorkspace-<version>.zip`; the installer treats both the same.

| Path | Contents |
| --- | --- |
| `runtime/` | A private CPython 3.12 runtime: the official embeddable package from python.org, with the pinned wheels from `requirements.txt` and `workspace/requirements.txt` installed. A `._pth` file fixes the import path, so the runtime ignores `PYTHONPATH`, user site-packages and any other Python on the machine. |
| `app/` | `asr_engines.py`, `offline_guard.py` and `workspace/`. Tests are not included. |
| `install.cmd`, `install.ps1`, `uninstall.ps1` | The per-user installer and uninstaller. |
| `version.json` | The version, build time, Python version and source commit. |
| `package-manifest.json` | The size and SHA-256 of every file. The installer checks all of them. |

Models are not in the package. They are separate, verified downloads in the app's Setup view (#19), stored in `%LOCALAPPDATA%\whisper-models` and shared with the legacy app.

Users never need Python, Git, FFmpeg or GPU tools.

## Building

On a Windows build machine with Python 3.12 and network access:

```
python tools/build_package.py --version 0.2.0 --out dist
```

The build:

1. Downloads the embeddable runtime, checks it against the pinned SHA-256, and checks that `python.exe`, `pythonw.exe` and `python312.dll` carry a valid Python Software Foundation Authenticode signature.
2. Installs the pinned requirements as wheels only, for `win_amd64` and CPython 3.12. The build refuses to run on any other host.
   - `proxy_tools` (MIT) publishes no wheel. Its wheel was built once from the PyPI sdist and is committed in `packaging/wheels/`, and the build checks it against a pinned SHA-256.
   - Other wheels are pinned by version but not yet by hash. Adding `--require-hashes` is a future hardening step.
   - Console launchers written by pip (`runtime\lib\bin`) are removed, because they embed the build machine's paths.
   - `runtime\sitecustomize.py` registers `runtime\lib` as a site directory, so packages that rely on `.pth` files work.
3. Copies the app and installer files, then writes `version.json` and `package-manifest.json`.
4. Zips the result and writes the `.sha256` file.

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
  - the Microsoft Edge WebView2 Runtime, which is built into Windows 11 and present on most managed Windows 10 devices;
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
- **Requirements:** Windows 10 1809 or later, x64. If WebView2 cannot be assumed, deploy the WebView2 Evergreen runtime as a dependency.

For support, `runtime\python.exe -m workspace --self-test` (in the installed version folder) imports every dependency and confirms the network guard. `--status` prints workspace counts only, never content.

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
