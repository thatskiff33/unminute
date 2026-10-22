# IT deployment guide

For IT teams deploying Unminute to managed Windows laptops. It brings together what IT needs and links to the source for each detail: [PACKAGING.md](PACKAGING.md) (installer, exit codes, Intune settings, policy table), [SECURITY_MODEL.md](SECURITY_MODEL.md) (threats and controls), [RELEASING.md](RELEASING.md) (releases and withdrawal) and the [user guide](WORKSPACE_GUIDE.md).

## Which version this describes

This guide describes the `main` branch. The newest release is v0.5.0-preview.3 (20 October 2026), a pre-release for testing. Some of what is described here is on `main` but not in that release; each such item is marked **Not in v0.5.0-preview.3**. The next release comes only once encryption at rest (#111 phase C) is complete: until then no release can be prepared from `main` ([RELEASING.md](RELEASING.md), "Before cutting a release").

Not yet tested through Intune on a managed device, on a clean standard-user profile or on Windows 10. Those checks are tracked in the validation tracker (#189). Treat the Intune steps below as the documented design until they are recorded there.

## Controls Unminute relies on IT for

Unminute assumes these are in place ([SECURITY_MODEL.md](SECURITY_MODEL.md), "Scope and assumptions"). It cannot check them itself.

| Control | Why | Threat |
| --- | --- | --- |
| BitLocker on every device, enforced by a compliance policy | On `main`, meeting text in the database, the handoff templates, the app's working copy of each meeting's audio and microphone recordings waiting to be imported are encrypted, but settings, logs and a recording's journal (times, events and device names) are not. In v0.5.0-preview.3 nothing is encrypted by the app. BitLocker protects all of it when the laptop is off or hibernated, and also covers the page file and hibernation file | T6, T7 |
| Staff sign in as standard users, one Windows account per person | The install and the workspace are per user. Another standard user cannot read them. An administrator can read the files the app does not encrypt directly, and the encrypted content by running code as the user or with the domain's DPAPI backup key | T8, T9 |
| How Windows protects the user's keys (**Not in v0.5.0-preview.3**) | On `main` the workspace key is protected by Windows (DPAPI) for the user. With a roaming profile it can be unlocked from another computer the user signs in to; whoever holds the domain's DPAPI backup keys can unlock it; and if Windows loses the user's protection keys, for example when an administrator resets the password without the old one, the encrypted content cannot be opened again. Which of these applies to your devices is to confirm on a pilot laptop (#189). Keep the backup keys protected, and have users finish or clean up their meetings before such a reset ([ENCRYPTION.md](ENCRYPTION.md), "What it does not do") | T6, T8 |
| Microsoft Defender (or equivalent) active | Malware running as the user can read what the user can read | T9 |
| WebView2 Evergreen runtime, kept up to date | The window needs a runtime recent enough to report screen-capture requests, or it closes rather than open a page it cannot secure ([PACKAGING.md](PACKAGING.md), prerequisites) | T1, T23 |
| A fixed proxy, or models staged by IT | Downloads use a proxy set in Windows or `HTTPS_PROXY`, not one given only by a PAC or WPAD script. They go over HTTPS on its standard port (443) only: an address or redirect on another port is refused (**Not in v0.5.0-preview.3**). Certificates are checked against the Windows certificate store, so a TLS-inspecting proxy works if its root is trusted there. With `AllowModelDownloads` set to 0, models have to be placed by IT | T12 |
| Windows Error Reporting local dumps for the runtime | Whether Windows still writes crash dumps for the app's processes is to confirm on a managed device; if it does, a dump could hold meeting content in memory, including live draft text once a version offers it (#104) | T21 |
| Remote-desktop clipboard redirection | Any app in the session, and a redirected clipboard, can read what Unminute copies | T17 |
| The handoff assistant's account and connectors | Unminute only copies text and opens a fixed address. What the assistant receives, and what its connected tools can read or change, is set in the assistant's tenant: read-only connectors and approval for actions are the controls | T17, T30 |
| Synced folders | A recording in a watched folder that syncs (for example OneDrive) is already in that service. Deleting an original from a synced folder, where the user has granted that, syncs the deletion too (to confirm). Very broad folders cannot be watched at all: a whole drive, the user's folder and the Windows and program folders, and on `main` also the folder that holds every user's profile (such as `C:\Users`), `AppData` and every folder in it, and Unminute's own data folder (**Not in v0.5.0-preview.3**; [INTAKE.md](INTAKE.md)) | T5, T28 |
| Forwarding the Windows Application log off the device (**Not in v0.5.0-preview.3**) | The app copies each audit record's hash to the Application log. On the device, a standard user can typically write to that log, and it overwrites its oldest events when full, so without forwarding the copy detects casual edits of the audit file only. Forwarded (Windows Event Forwarding or a SIEM), it is the stronger record. Standard users' rights to the log, and its size and retention on your baseline, are to confirm (#189). See "Audit log" below | T20 |

Planned, not available yet: a per-machine install that standard users cannot change (B6), machine-wide models staged by IT (B7), and offboarding support (B8). A firewall rule that blocks the app's runtime is part of B7; until models are staged by IT it would also block model downloads, which run in the same runtime.

## Install modes

Only a per-user install exists. It goes into `%LOCALAPPDATA%\Programs\Unminute\versions\<version>` and needs no administrator rights. Exit codes and every check the installer makes are in [PACKAGING.md](PACKAGING.md), "Installing (self-service)".

| Mode | How | Notes |
| --- | --- | --- |
| Self-service | Extract the zip and run `install.cmd`, or `install.ps1 -Package Unminute-<version>.zip` | `-Package` checks the zip against its `.sha256`; the extracted-folder route checks only the manifest inside the package |
| Unattended | `install.ps1 -Silent` | Below the minimum hardware it records the warning and continues |
| Intune Win32 app | Install behaviour **User**, `install.ps1 -Silent` | See "Intune app" below. System context would see no user's workspace |
| IT-managed updates | Add `-Managed` | The app then says "Updates are managed by your IT team". `-SelfService` switches back |
| Roll back | `install.ps1 -Rollback` from the current version's folder | Refused if the previous version cannot open the workspace |

Users never install Python, Git, FFmpeg or GPU tools.

A custom `-InstallDir` must be new, empty or an existing Unminute install. A folder that already holds other files, or a link or junction where the installer looks, stops it with exit code 2, and nothing is changed; the installer and uninstaller remove only the version folders the installer made (**Not in v0.5.0-preview.3**, #297; [PACKAGING.md](PACKAGING.md), "Installing (self-service)").

## Verify a release before packaging

Run IT's verifier on every download before you package it. The `.sha256` beside a zip detects a damaged download, not a replaced one.

```
powershell -NoProfile -File tools\intune\Get-VerifiedRelease.ps1 -Tag v0.5.0-preview.3 -AllowPrerelease
```

- `-AllowPrerelease` is needed for any `vX.Y.Z-preview.N` tag; without it the script refuses a pre-release. Omit it for a full release.
- It downloads the zip, its `.sha256`, the SBOM and the attestation bundle into a new folder, checks the checksum, and verifies both attestations against the repository's Release workflow at that tag, from the tag's commit, on a GitHub-hosted runner.
- It prints VERIFIED and the zip's SHA-256, or NOT VERIFIED and the reason. Package only a verified zip.
- It needs the GitHub CLI (`gh`) on the path, and no GitHub sign-in while the repository is public.
- Releases are published from `thatskiff33/unminute`. The script's default `-Repository` names it, and so does the signer identity it checks ([RELEASING.md](RELEASING.md), "Protecting releases").

Details: [PACKAGING.md](PACKAGING.md), "Verifying a release before packaging".

## Intune app

The full settings are in [PACKAGING.md](PACKAGING.md), "Managed deployment (Intune)". In short:

1. Verify the release (above), then wrap the extracted folder with the Microsoft Win32 Content Prep Tool, with `install.cmd` as the setup file.
2. Install behaviour: **User**. Install command: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Silent` (add `-Managed` if IT manages updates).
3. Uninstall command: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\Programs\Unminute\uninstall.ps1" -Silent`.
4. Return codes: map 5 (the update is waiting for the workspace) to Retry, and 1, 2, 3, 4 and 6 to Failed. Code 3 (the app is running) is worth a retry. Code 6 means this version cannot open the user's existing workspace, for example a newer workspace after a downgrade, or one in a refused location such as OneDrive or a network drive (see "Updates and rollback" below, and the exit codes in [PACKAGING.md](PACKAGING.md)).
5. Detection rule: the registry value `DisplayVersion` under `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Uninstall\Unminute` equals the version.
6. Requirements: Windows 10 1809 or later, x64, and the WebView2 runtime.

Coming from 0.4.3 or earlier is not an update in place: users finish with the earlier version first. The order, and how to supersede the earlier app in Intune, are in [PACKAGING.md](PACKAGING.md), "Updates".

## Managed policy

Policy values live under `HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Unminute` and override the user's settings. The administrative template is `packaging/policy/Unminute.admx` with `packaging/policy/en-US/Unminute.adml`: import both under Devices > Manage devices > Configuration > Import ADMX, then build a profile from the "Unminute" category. Most values grant something when not set: every `Allow` value except `AllowNetworkSources` is allowed until it is set to 0, and `RequireRecordingConsent` is on unless it is set to 0. The "Not set" column says what each one does when IT leaves it unset. What each value does when set, and its type, are in the table in [PACKAGING.md](PACKAGING.md), "Managed settings (policy)". This list is checked against the template, and its "Not set" column against PACKAGING.md, by `DeploymentGuideTests` in `tests/test_consistency.py`.

| Value | Setting name in the template | Area | Not set |
| --- | --- | --- | --- |
| `AllowModelDownloads` | Allow model downloads in the app | Downloads | Allowed |
| `AllowGpuComponent` | Allow the graphics card components | GPU | Allowed |
| `AllowMicrophoneRecording` | Allow microphone recording | Microphone | Allowed |
| `AllowComputerAudioRecording` | Allow recording computer audio | Computer audio | Allowed (each user's switch is still off by default) |
| `AllowLidOverride` | Allow keeping a recording going with the lid closed | Microphone | Allowed |
| `AllowLiveDrafts` | Allow draft text while recording | Live drafts | Allowed |
| `RequireRecordingConsent` | Confirm before each recording that everyone has been told | Microphone | Required |
| `AllowWatchedFolders` | Allow watched folders | Watched folders | Allowed |
| `AllowDeleteOriginals` | Allow deleting original recordings from watched folders | Watched folders | Allowed |
| `AllowNetworkSources` | Allow recordings on network drives | Network drives | Refused |
| `HandoffFilenameIncludesTitle` | Suggest the meeting title as the handoff file name | Handoff | Date and time only |
| `ClipboardClearMinutes` | Clear the app's handoff copy from the clipboard | Clipboard | Never cleared |
| `HandoffDefaultFolder` | Starting folder for saved handoffs | Handoff | The dialog's usual folder |
| `AllowHandoffAssistant` | Allow "Copy and open" on the Hand off page | Handoff | Allowed |
| `HandoffAssistantName` | Hand off assistant name | Handoff | ChatGPT |
| `HandoffAssistantUrl` | Hand off assistant address | Handoff | `https://chatgpt.com/` |

**Not in v0.5.0-preview.3:** `AllowHandoffAssistant`, `HandoffAssistantName` and `HandoffAssistantUrl`, and the Copy and open button they control; `AllowComputerAudioRecording`; and `AllowLiveDrafts`. That release ignores them, as it ignores any value name it does not know. On `main`, `AllowComputerAudioRecording` and the recording support for computer audio are in place, but no version shows the switch yet (#108): computer audio cannot be recorded until a later version adds it. It never applies when `AllowMicrophoneRecording` is 0. `AllowLiveDrafts` governs draft text while recording (#104): rough text of what is said, kept in memory only and replaced by the full transcript. On `main` only its back end is in place, and it is off: the page does not show it, there is no Settings switch, and the app refuses to turn it on, so draft text cannot run until a later version adds the page part. That version will leave it off until the user turns it on. Set to 0, it cannot be turned on, and a draft in progress is stopped and cleared when the app next reads the policy, while the recording carries on. The template's revision number did not change when they were added, and Intune does not overwrite an imported template: to take the new values, delete the profiles that use the template and the old template, then import again ([PACKAGING.md](PACKAGING.md)).

Value names are not case-sensitive. A name the app does not recognise (a misspelling, a trailing space or a lookalike letter) grants and withholds nothing, so check names against the template. On `main` the app keeps the first 20 such names, never their data, for Settings, which does not show them yet (#300), and Copy diagnostics says how many there are (**Not in v0.5.0-preview.3**). In a `HandoffDefaultFolder` set as an expandable string, `main` expands only `%NAME%` variables, as Windows does ([PACKAGING.md](PACKAGING.md), "Managed settings (policy)").

The app enforces these values in its own code. Until the per-machine install (B6), that code sits in the user's profile, so someone able to change files there could remove the checks: policy is not enforced against a determined user. The policy key itself stays admin-only.

## Health monitoring with `--health`

`--health` is a one-line, content-free health report for an Intune Remediation detection script. Its fields and exit codes are in [PACKAGING.md](PACKAGING.md), "Managed deployment (Intune)".

- **Run it as the signed-in user:** set **Run this script using the logged-on credentials** to **Yes**. Never run it as SYSTEM: the install is the user's own, so a SYSTEM run would run the user's code as SYSTEM. Run as SYSTEM, it reports `"workspace": "system_account"` and exits 1.
- **Exit 0** is healthy; **exit 1** is Intune's "issue detected".
- **Unminute ships no remediation script.** Every state that needs attention needs a person, as below.

An example detection script, not yet run on a managed device (#189). It reads the installed version's folder from `current.json`, which the installer writes:

```powershell
# Unminute health: detection only. Run with the logged-on credentials.
$current = Join-Path $env:LOCALAPPDATA 'Programs\Unminute\current.json'
if (-not (Test-Path -LiteralPath $current)) { Write-Output 'not installed'; exit 0 }
$version = (Get-Content -LiteralPath $current -Raw | ConvertFrom-Json).path
& (Join-Path $version 'runtime\python.exe') -I -B -m workspace --health
exit $LASTEXITCODE
```

| `--health` shows | What it means | What to do |
| --- | --- | --- |
| `"workspace": "none"` | This user has not opened the app yet | Nothing; it counts as healthy |
| `"workspace": "locked"` (**Not in v0.5.0-preview.3**) | At the app's last start Windows would not unlock the workspace key, so the workspace opened locked: no meeting is read or changed (only the problem is logged) and work waits. Read from what the app recorded, never by trying the key; it takes precedence over `damaged` | The user guide, [If Unminute cannot unlock its workspace](WORKSPACE_GUIDE.md#if-unminute-cannot-unlock-its-workspace) |
| `"workspace": "damaged"` | The last database check found damage | The user guide, [If Unminute finds damage](WORKSPACE_GUIDE.md#if-unminute-finds-damage) |
| `"workspace": "location"` | The data folder is missing, in OneDrive, on a network drive, or a link to one of those or to a folder that cannot be reached (**Not in v0.5.0-preview.3:** the link to a folder that cannot be reached; that release does not report it as `location`) | Restore the folder or remove the link; only `Unminute\models` may be redirected ([PACKAGING.md](PACKAGING.md), "What the package contains") |
| `"workspace": "unreadable"` | The workspace cannot be read, or Windows cannot say which account the check runs as | Ask the user for **Copy diagnostics** (Help); see the user guide's troubleshooting table |
| `"workspace": "system_account"` | The script ran as SYSTEM | Set it to run with the logged-on credentials |
| `"last_run": "did_not_close"` or `crashes_7d` above 0 | The app or one of its processes ended unexpectedly | Ask the user for **Copy diagnostics** and any code shown (`U-…`); if many devices show it after an update, see "Incidents" below |

When the app cannot unlock its encryption key (**Not in v0.5.0-preview.3**), it opens locked rather than closing: the page can still copy diagnostics, open help and close, every other request is refused, and no meeting is read or changed (only the problem is logged). `--status` shows `"keys": "locked"` with the reason and a code. If Windows cannot set up a key for a new workspace at all, the app says so with a code and closes, creating nothing. The steps are in the user guide, [If Unminute cannot unlock its workspace](WORKSPACE_GUIDE.md#if-unminute-cannot-unlock-its-workspace).

## Audit log

**Not in v0.5.0-preview.3.** The app keeps a content-free, hash-chained audit log in `%LOCALAPPDATA%\Unminute\workspace\audit\`. It records imports and recordings (with the recording confirmation's outcome and wording version), the review and final confirmations, discards, holds, handoffs, watched folders, changes to delete-originals authority, and each cleanup. Each record carries the kind of action, the time, the app's own random ids, fixed words and counts, never text, titles, names, folders or hold reasons. It is always on, with no policy value (the owner's decision, 21 Oct 2026). It is capped at 16 MB: past that the oldest 1 MB segment is deleted, and a record says so. It is not in the database, so an update that waits for an empty workspace keeps it. The full description is in [PACKAGING.md](PACKAGING.md#the-audit-log-and-its-event-log-copy-111-c5) and SECURITY_MODEL.md T20.

- **The Event Log copy.** Each record's chain, sequence number, kind and SHA-256, never an id, go to the Windows Application log under the source `Unminute`, with the signed-in user's SID. The source is not registered (installs are per user), so Event Viewer shows the text without a description and a SIEM reads it from `EventData`. The event IDs and levels are in PACKAGING.md. Forward the log off the device for the stronger record (see "Controls Unminute relies on IT for" above).
- **Checking it.** Run, as the signed-in user (never as SYSTEM, as for `--health`), from the installed version folder:

  ```
  runtime\python.exe -I -B -m workspace --verify-audit --event-log
  ```

  It needs no key, reads no database and prints one line of JSON with counts and states only. `result` is `ok` when the chain is whole, or `broken` with the sequence number and a reason. `--event-log` compares the file with the signed-in user's `Unminute` entries in this computer's Application log; `--event-log-file <path>` compares it with an exported `.evtx` file instead, for example from the forwarded copy. It exits 0 when the chain is `ok` and, if compared, matches; 1 otherwise. `--status` also shows `audit_seq`, the newest record's sequence number.
- **What it cannot show.** The file alone cannot show whole lines removed from its end, a rebuilt chain or a deleted folder: those show only against the Event Log copy, while it still holds the entries. The SID on an entry is supplied by whoever writes it, so it attributes the app's own entries but does not prove who wrote one. A crash between an action and its record loses the record, and can leave the last few records without an Event Log copy.

## Updates and rollback

- **Same storage format:** installing the new version keeps everything.
- **New storage format:** the update waits (exit code 5) until the user's workspace holds no meetings, active holds, unfinished cleanup, microphone recordings waiting to be imported or meeting audio. It never expires a hold, deletes content or backs content up to proceed. Map 5 to Retry in Intune; Settings > Updates in the app says what it is waiting for.
- **Encryption at rest is a storage-format change.** The release that brings it (schema 2) ships as one release once #111 phase C is complete, and the workspace must be empty before it installs. A version before it cannot open a schema-2 workspace.
- **Rollback:** `install.ps1 -Rollback` from the current version's folder returns to the previous installed version, after the same check with that version. It has no minimum-version check (T26). It goes back only to a version folder the installer made (**Not in v0.5.0-preview.3**, #297).
- **The first release after the rename (#136)** has no earlier version in its install folder to roll back to.

Details: [PACKAGING.md](PACKAGING.md), "Updates (#26)".

## Offboarding (as it is today)

Unminute has no offboarding command yet (B8, planned). Uninstalling removes the program, its shortcut and its uninstall entry, and deliberately keeps the workspace, the models and the Hugging Face token (T29). When a person leaves or a laptop is reassigned:

1. **With the user, before they leave:** hand off or copy what is still needed, confirm cleanup of finished meetings, and check holds with whoever manages them. Holds never expire, and releasing one deletes nothing.
2. **Check what is left:** from the installed version folder, as the user, `runtime\python.exe -I -B -m workspace --status` prints counts only: meetings, holds, pending cleanup and recordings not yet in a meeting.
3. **Remove the token:** Settings > Manage downloads > **Remove the remembered token**. After uninstalling, it can be removed in Windows Credential Manager (Windows Credentials, Generic) as `Unminute/HuggingFaceToken`. A token saved by 0.4.3 or earlier is under `WhisperTranscriber/HuggingFaceToken`.
4. **Uninstall:** Settings > Apps > Unminute, or the Intune uninstall command.
5. **Remove the data**, once nothing in it is needed and whoever manages holds agrees: delete `%LOCALAPPDATA%\Unminute` (the workspace with its logs and audit log, and the models). The audit log's Event Log copy stays in the Application log, and in any forwarded copy, under its own retention. Unminute has no export step for the audit log yet: whether it gets one before removal is decided in B8. A version 0.4.3 or earlier leaves `%LOCALAPPDATA%\whisper\workspace` and `%LOCALAPPDATA%\whisper-models` too. This is ordinary deletion: on an SSD, old blocks are not guaranteed to be erased, and BitLocker is what protects them. Never copy a workspace to a share or another computer.
6. **Reassigning the laptop:** a reset or wipe that does not keep user data removes the profile, and the workspace with it.

## Incidents

Start from the kind of incident. None of these steps needs meeting content: never ask a user to send a workspace, a recording or a transcript.

| Incident | First steps | Runbook |
| --- | --- | --- |
| A release has a severity 1 issue (content lost or exposed, the core task broken, updates blocked) | The maintainer withdraws it: the previous good release becomes the default download, and the bad release's notes carry a warning. IT stops assigning the bad version. Installs are per user until B6, so each one rolls back with `install.ps1 -Rollback`, unless the bad version changed the storage format, in which case the fix is forward | [Withdrawing a bad release](RELEASING.md#withdrawing-a-bad-release); severity is defined in [RELEASE_STANDARD.md](RELEASE_STANDARD.md), section 14 |
| Many devices report unhealthy after an update | Read `--health` across devices: crashes, damage, a locked workspace or location. Ask affected users for **Copy diagnostics** | [Health monitoring](#health-monitoring-with---health) above; [PACKAGING.md](PACKAGING.md#managed-deployment-intune) for the fields |
| One user's workspace is damaged or will not open | Follow the user guide's recovery steps; there are no backups, by design | [If Unminute finds damage](WORKSPACE_GUIDE.md#if-unminute-finds-damage) |
| The app cannot unlock its key (**Not in v0.5.0-preview.3**) | `--health` shows `locked`. Ask for **Copy diagnostics**, which still works while locked, and the code; `--status` still works | [If Unminute cannot unlock its workspace](WORKSPACE_GUIDE.md#if-unminute-cannot-unlock-its-workspace) |
| Someone needs to show what was exported or deleted, or the audit log may have been changed (**Not in v0.5.0-preview.3**) | As the user, run `--verify-audit --event-log`; or compare with the forwarded copy, exported as `.evtx`, with `--event-log-file`. It reports counts and states only | [Audit log](#audit-log) above; SECURITY_MODEL.md T20 for what it cannot show |
| A suspected security vulnerability in Unminute | Report it privately, with synthetic data only | [SECURITY.md](../SECURITY.md) |
| A laptop is lost or stolen, or content went somewhere it should not | Follow your organisation's own incident process. What Unminute keeps on the laptop, and what is encrypted, is in the privacy impact assessment draft | [PRIVACY_IMPACT_ASSESSMENT_DRAFT.md](PRIVACY_IMPACT_ASSESSMENT_DRAFT.md) |

## Not available yet

- A per-machine install in Program Files that standard users cannot change (B6), with a launch-time update gate and first-launch fallback (B5).
- Machine-wide models staged by IT, and the runtime firewall rule (B7).
- An offboarding report and removal (B8), which also decides whether the audit log is exported before the workspace is removed.
- The switch to record computer audio beside the microphone (#108). The recording support and `AllowComputerAudioRecording` are on `main`, but no version shows the switch yet.
- Draft text while recording (#104). The back end and `AllowLiveDrafts` are on `main`, off; the page part (the draft on the recording card, a warning about screen sharing with Hide draft, and the Settings switch) comes later.
- A locked screen in the window when the key cannot be unlocked (C4b; until then the usual window opens with every request refused).
- A decoder process that cannot read the workspace (#299). Today it runs at Low integrity, which stops it writing the user's files but not reading them.
- Code signing of the zip and the installer scripts. Releases from 0.5.0 carry signed build provenance and an SBOM attestation instead; signing options are in [PACKAGING.md](PACKAGING.md), "Signing and trust".
