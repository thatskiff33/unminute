# IT deployment guide

For IT teams deploying Unminute to managed Windows laptops. It brings together what IT needs and links to the source for each detail: [PACKAGING.md](PACKAGING.md) (installer, exit codes, Intune settings, policy table), [SECURITY_MODEL.md](SECURITY_MODEL.md) (threats and controls), [RELEASING.md](RELEASING.md) (releases and withdrawal) and the [user guide](WORKSPACE_GUIDE.md).

## Which version this describes

This guide describes the `main` branch. The newest release is v0.5.0-preview.4 (27 October 2026), a pre-release for testing. Anything described here that is on `main` but not in that release is marked **Not in v0.5.0-preview.4**. Encryption at rest (#111 phase C) is complete (C7, #335) and is in v0.5.0-preview.4.

Not yet tested through Intune on a managed device, on a clean standard-user profile or on Windows 10. Those checks are tracked in the validation tracker (#189). Treat the Intune steps below as the documented design until they are recorded there.

## Controls Unminute relies on IT for

Unminute assumes these are in place ([SECURITY_MODEL.md](SECURITY_MODEL.md), "Scope and assumptions"). It cannot check them itself.

| Control | Why | Threat |
| --- | --- | --- |
| BitLocker on every device, enforced by a compliance policy | Since v0.5.0-preview.4, meeting text in the database, the handoff templates, the app's working copy of each meeting's audio and microphone recordings waiting to be imported are encrypted, but settings, logs and a recording's journal (times, events and device names) are not. Earlier versions encrypt nothing themselves. BitLocker protects all of it when the laptop is off or hibernated, and also covers the page file and hibernation file | T6, T7 |
| Staff sign in as standard users, one Windows account per person | The install and the workspace are per user. Another standard user cannot read them. An administrator can read the files the app does not encrypt directly, and the encrypted content by running code as the user or with the domain's DPAPI backup key | T8, T9 |
| How Windows protects the user's keys | The workspace key is protected by Windows (DPAPI) for the user. With a roaming profile it can be unlocked from another computer the user signs in to; whoever holds the domain's DPAPI backup keys can unlock it; and if Windows loses the user's protection keys, for example when an administrator resets the password without the old one, the encrypted content cannot be opened again. Which of these applies to your devices is to confirm on a pilot laptop (#189). Keep the backup keys protected, and have users finish or clean up their meetings before such a reset ([ENCRYPTION.md](ENCRYPTION.md), "What it does not do") | T6, T8 |
| Microsoft Defender (or equivalent) active | Malware running as the user can read what the user can read | T9 |
| WebView2 Evergreen runtime, kept up to date | The window needs a runtime recent enough to report screen-capture requests, or it closes rather than open a page it cannot secure ([PACKAGING.md](PACKAGING.md), prerequisites) | T1, T23 |
| A fixed proxy, or models staged by IT | Downloads use a proxy set in Windows or `HTTPS_PROXY`, not one given only by a PAC or WPAD script. They go over HTTPS on its standard port (443) only: an address or redirect on another port is refused. Certificates are checked against the Windows certificate store, so a TLS-inspecting proxy works if its root is trusted there. With `AllowModelDownloads` set to 0, models have to be placed by IT | T12 |
| Windows Error Reporting local dumps for the runtime | Whether Windows still writes crash dumps for the app's processes is to confirm on a managed device; if it does, a dump could hold meeting content in memory, including live draft text while recording (#104) | T21 |
| Application control (AppLocker or App Control for Business) that stops programs running from user profiles | A user can install a per-user copy that runs from a folder the user can change and that IT does not update, even where the machine-wide install (B6b) is in place, because an older package's installer does not check for it. Unminute has no policy value for this. See [Stopping per-user copies on managed computers](#stopping-per-user-copies-on-managed-computers) below | T24 |
| Remote-desktop clipboard redirection | Any app in the session, and a redirected clipboard, can read what Unminute copies | T17 |
| The handoff assistant's account and connectors | Unminute only copies text and opens a fixed address. What the assistant receives, and what its connected tools can read or change, is set in the assistant's tenant: read-only connectors and approval for actions are the controls | T17, T30 |
| Synced folders | A recording in a watched folder that syncs (for example OneDrive) is already in that service. Deleting an original from a synced folder, where the user has granted that, syncs the deletion too (to confirm). Very broad folders cannot be watched at all: a whole drive, the user's folder and the Windows and program folders, and also the folder that holds every user's profile (such as `C:\Users`), `AppData` and every folder in it, and Unminute's own data folder ([INTAKE.md](INTAKE.md)) | T5, T28 |
| Forwarding the Windows Application log off the device | The app copies each audit record's hash to the Application log. On the device, a standard user can typically write to that log, and it overwrites its oldest events when full, so without forwarding the copy detects casual edits of the audit file only. Forwarded (Windows Event Forwarding or a SIEM), it is the stronger record. Standard users' rights to the log, and its size and retention on your baseline, are to confirm (#189). See "Audit log" below | T20 |

Available: a per-machine install that standard users cannot change (B6b; "Install modes" below). Planned, not available yet: machine-wide models staged by IT (B7). A firewall rule that blocks the app's runtime is part of B7; until models are staged by IT it would also block model downloads, which run in the same runtime.

## Install modes

Two install modes. Use one per computer.

- **Per user.** It goes into `%LOCALAPPDATA%\Programs\Unminute\versions\<version>` and needs no administrator rights. Exit codes and every check the installer makes are in [PACKAGING.md](PACKAGING.md), "Installing (self-service)". A current per-user installer refuses (exit code 2) once a machine install exists; an older package's installer does not check, so stop per-user installs on managed computers with application control (AppLocker or WDAC).
- **For everyone on the computer (machine mode, #111 B6b).** `install.ps1 -Machine -Package <zip>`, run as SYSTEM by Intune in a 64-bit Windows PowerShell, installs to `%ProgramFiles%\Unminute`, which only administrators can change: the app's code, including its policy, encryption and audit code, is then out of the user's reach (the model cache and GPU components stay in the user's profile until B7). Each user's workspace and models stay in their own `%LOCALAPPDATA%\Unminute`. The installer checks the folder's permissions at every run, never runs Python or reads a profile, and refuses while anyone has the app open (exit code 3, retry). It keeps the current and previous versions and the newest version of the storage format immediately before the current one, for users whose workspace still holds meetings in it; each user's start picks the version that can open their workspace. Details, exit codes and the Intune settings: [PACKAGING.md](PACKAGING.md), "Installing for everyone on a computer" and "Machine install in System context".

| Mode | How | Notes |
| --- | --- | --- |
| Self-service | Extract the zip and run `install.cmd`, or `install.ps1 -Package Unminute-<version>.zip` | `-Package` checks the zip against its `.sha256`; the extracted-folder route checks only the manifest inside the package |
| Unattended | `install.ps1 -Silent` | Below the minimum hardware it records the warning and continues |
| Intune Win32 app | Install behaviour **User**, `install.ps1 -Silent` | See "Intune app" below. System context would see no user's workspace |
| IT-managed updates | Add `-Managed` | The app then says "Updates are managed by your IT team". `-SelfService` switches back |
| Intune Win32 app, machine (B6b) | Install behaviour **System**, `install.ps1 -Machine -Package <zip> -Silent` from the 64-bit Windows PowerShell | See "Intune app" below. No install-time workspace check: each user's start decides |
| Roll back | `install.ps1 -Rollback` from the current version's folder | Refused if the previous version cannot open the workspace |

Users never install Python, Git, FFmpeg or GPU tools.

A custom `-InstallDir` must be new, empty or an existing Unminute install. A folder that already holds other files, or a link or junction where the installer looks, stops it with exit code 2, and nothing is changed; the installer and uninstaller remove only the version folders the installer made (#297; [PACKAGING.md](PACKAGING.md), "Installing (self-service)").

## Protected decoder

**Not in v0.5.0-preview.3** (#299). Recordings can come from anyone, so Unminute decodes each one in a separate process that runs in a Windows AppContainer with no capabilities. Windows gives that process no access to the user's files, the Unminute workspace or the network, and the app hands it the recording as one read-only handle. If a crafted recording took over the decoder, it could not read meetings or send anything out ([SECURITY_MODEL.md](SECURITY_MODEL.md) T2, T3).

What it needs on each computer:

- **Read access to the install.** The container reads the app's runtime and code through ALL APPLICATION PACKAGES (`S-1-15-2-1`). The installer grants it read and execute on `%LOCALAPPDATA%\Programs\Unminute\versions` at every install, update, repair and rollback ([PACKAGING.md](PACKAGING.md), "Installing (self-service)").
- **A per-user AppContainer profile.** Windows starts a container process only with a profile registered for the user. The app registers `Unminute.Decoder` at the first import, with no administrator rights: a key under `HKCU\Software\Classes\Local Settings` and the folder `%LOCALAPPDATA%\Packages\unminute.decoder`, which hold nothing of the user's. The uninstaller and `--offboard` remove it.

If either is missing, or Windows will not start the process in the container, Unminute refuses the import. It never decodes outside the container (the owner's decision, 24 October 2026). The user sees:

> This recording can't be opened safely on this computer: Unminute's protected decoder could not start. Ask your IT team to check "Protected decoder" in Unminute's IT guide. (code U-…)

A file stays where it was; a microphone recording keeps its audio and waits for **Try again**. The code finds the log record (in **Copy diagnostics**), which names only the failed step and the Windows error number.

To check and fix it:

1. **As the signed-in user**, from the installed version folder, run `runtime\python.exe -I -B -X disable-remote-debug -m workspace --check-decoder`. It decodes a second of synthetic silence in the container and prints `{"decoder": "ok"}` (exit 0), or `{"decoder": "failed", "step": ..., "winerror": ...}` (exit 1). Run as Local System it starts nothing.
2. **Read the step:**

   | `step` | What failed | What to do |
   | --- | --- | --- |
   | `profile` | Windows would not register the AppContainer profile for this user | Check whether security software or a policy blocks AppContainer profiles, and the user's registry hive |
   | `start` | Windows would not start the process in the container. Error 5 (access denied) usually means ALL APPLICATION PACKAGES cannot read the install, for example after its permissions were reset or its files copied in without the installer | Run the installer again for the same version (a repair: it grants the access), or redeploy it from Intune. To grant it by hand: `icacls "%LOCALAPPDATA%\Programs\Unminute\versions" /grant "*S-1-15-2-1:(OI)(CI)(RX)"` |
   | `ready` | The process started but the decoder never reported ready: it could not load its runtime (often the same missing read access as `start`), or took more than 3 minutes (for example, delayed by security software) | As for `start`; then check whether security software blocks or delays `runtime\python.exe` in a container |
   | `contain` | Windows would not put the process in its job (memory limit, no child processes) | Check what else runs the app inside a job (a launcher or security product) |
   | `check` | The process was not in the expected container with no capabilities at Low integrity | Not expected. Report it as a security issue ([SECURITY.md](../SECURITY.md)) |
   | `decode` | The container ran but the synthetic decode failed | Report it with the code |

3. **Run `--check-decoder` again**, then ask the user to import again (or choose **Try again** on a waiting recording).

Application control sees the decoder as the app's own `runtime\python.exe`, so rules that allow the app allow it ([Stopping per-user copies on managed computers](#stopping-per-user-copies-on-managed-computers)). After a rollback to a version from before #299, that version's decoder runs at Low integrity, without the container, until a #299 version is installed again. Whether the container blocks the internet as Windows documents (loopback was measured blocked) is to confirm on a managed laptop (#189).

## Stopping per-user copies on managed computers

A per-user installer from v0.5.0-preview.4 or later, the release that brings the machine-wide install (B6b), refuses to install when a machine-wide install exists (exit code 2). An older package's installer has no such check, and a user can always run one, so on a managed computer a user can still put a per-user copy in their own profile. That copy runs from a folder the user can change ([SECURITY_MODEL.md](SECURITY_MODEL.md) T24), and IT does not update it. Unminute has no policy value for this. Per-user copies on managed computers are a residual, and the control is IT's application control: AppLocker, or App Control for Business (formerly Windows Defender Application Control, WDAC). This was the owner's decision B6-12, 21 October 2026 (#111).

**Nothing in this section has been tested.** The machine-wide install (B6b) has not yet been run on a managed computer, and neither has any rule below. Every rule is marked "to confirm", and the checks are in the validation tracker (#189, sitting 3). Statements about Windows and Intune come from the Microsoft Learn pages listed under "Microsoft sources" at the end of this section, read on 22 October 2026, and are cited by number (M1 and so on).

### What to stop and what to allow

| Copy | Folder | What runs there |
| --- | --- | --- |
| Per-user copy: stop | `%LOCALAPPDATA%\Programs\Unminute`, with each version in `versions\<version>` | `runtime\pythonw.exe` and `runtime\python.exe` (the app, its child processes and command-line switches such as `--health`), the runtime's DLLs and `.pyd` modules, and `uninstall.ps1` |
| Per-user installer: stop | Wherever the user extracts the zip; with `-Package`, `%TEMP%` | `install.cmd` and `install.ps1`, and the package's own `runtime\python.exe`, which the installer runs from outside the install folder for its update check (T22) |
| Machine-wide install (B6): allow | `%ProgramFiles%\Unminute`, with each version in `versions\<version>` and the `current` junction pointing at one (B6b) | The same runtime and app files |
| GPU components | `%LOCALAPPDATA%\Unminute\models`, per user in both modes (#111, B7) | NVIDIA and PyTorch DLLs and `.pyd` modules, loaded by the app's GPU workers |

**Write rules by location or origin, not by file.** A per-user copy and the machine-wide install of the same version hold the same files: the package's, checked against its manifest. The runtime's `python.exe`, `pythonw.exe` and `python314.dll` carry the Python Software Foundation's signature ([PACKAGING.md](PACKAGING.md), "Building").

- A hash rule identifies one file by its content (M7, M14). A hash rule that allows the machine-wide install's files also allows the identical files in a per-user copy, and a hash rule that blocks a per-user copy also blocks the machine-wide install.
- A publisher rule identifies a file by its signature and the details in it, not by its folder (M8, M14). Allowing the Python Software Foundation's signature allows every copy of that Python, anywhere, Unminute's or not.
- What differs is where the files are and how they got there. Allow the machine-wide install by a path rule on a folder only administrators can change or, with App Control, by managed installer origin. Let the policy's default block stop everything in user profiles.

### AppLocker

AppLocker rules sit in rule collections: executables (`.exe` and `.com`, though executable rules apply to any portable executable file whatever its extension), Windows Installer files, scripts (`.ps1`, `.bat`, `.cmd`, `.vbs` and `.js`), DLLs (`.dll` and `.ocx`, not enabled by default) and packaged apps (M1, M2, M4, M5). Within a collection that has rules, a file no rule allows is blocked (M3). The default rules let everyone run files in the Windows folder and in Program Files, and let administrators run anything (M2, M4, M5). The `%PROGRAMFILES%` path variable covers both Program Files folders (M6).

An example approach (to confirm, #189):

1. **Executable rules: the three default rules, and no rule that allows the user profile folders.** Nothing then allows a per-user copy's `pythonw.exe` and `python.exe`, or the per-user installer's run of the package's `python.exe` from the extracted folder or `%TEMP%`, so neither starts. The machine-wide install in `%ProgramFiles%\Unminute` is covered by the default Program Files rule, with no rule for Unminute itself. To confirm.
2. **Script rules: the three default rules.** `install.cmd` and `install.ps1` in a download folder, and a per-user copy's `uninstall.ps1`, are not allowed. PowerShell does not refuse such a script: it runs it in Constrained Language Mode (M4). Whether `install.ps1` stops in that mode, and where, is to confirm. Rule 1 stops the copy from running either way.
3. **DLL rules (optional).** With the default rules only, a per-user copy's DLLs are not allowed either. Microsoft warns that DLL rules need an allow rule for every DLL each allowed app loads, Windows' own included, and can slow computers that are short of resources (M1, M5). Whether AppLocker checks Python's `.pyd` modules, which are DLLs with another extension, is to confirm. Rule 1 does not depend on it.
4. **Enforcement.** AppLocker enforces nothing while the Application Identity service is stopped (M9). Windows 10 version 2004 and later with KB5024351, and all Windows 11 versions, enforce AppLocker on any edition (M10). Intune deploys AppLocker policy through the AppLocker CSP, which Windows still supports but no longer adds features to (M18, M19).

If your policy already allows programs in user profiles, a deny rule on the per-user folder is the fallback, and a weaker one: a path deny rule can be got round by copying the files to another folder (M6). AppLocker path variables are not environment variables, and there is none for `%LOCALAPPDATA%` (M6), so the rule would name `%OSDRIVE%\Users\*\AppData\Local\Programs\Unminute\*`. Whether AppLocker accepts a wildcard in the middle of a path is to confirm.

### App Control for Business

App Control policies apply to the whole device and every user on it. Microsoft recommends App Control over AppLocker for organisations that can use it (M13). In Intune it is under Endpoint security > App Control for Business, with built-in controls (trust Windows components and Store apps, and optionally apps with a good reputation and apps from managed installers) or custom XML. A supplemental policy adds rules to a base policy (M18).

An example approach (to confirm, #189):

1. **Base policy: the built-in control "Enable trust of Windows components and store apps", set to Audit only first.** Once enforced, apps the policy does not trust are blocked (M18). Nothing in it trusts a per-user Unminute copy or the package's `python.exe` in a download folder, so neither starts. To confirm.
2. **Allow the machine-wide install (B6), in one of two ways.**
   - **Managed installer.** Add the Intune Management Extension as a managed installer, and select "Trust apps from managed installers". Files that an app deployed through Intune writes are tagged, and the policy allows them (M16, M18). Microsoft's limits: apps installed before the managed installer was set are not tagged; an app that updates itself loses the tag, so every update has to come through the managed installer (Unminute's machine-wide updates are a new version of the same Intune app, B6-11); and a managed installer that runs as a standard user may let standard users get round the policy (M16, M18). So, once the machine-wide install is deployed, stop assigning a per-user Unminute app (install behaviour User) to these devices. Whether Intune tags the files of a per-user install made in user context is to confirm. Turning on the managed installer also merges an AppLocker policy into the device's own, so a rule collection set to NotConfigured with no rules can then block apps or stop Windows starting or signing in (remove any such collection first), and it can turn back on AppLocker policies from Group Policy that were stopped or disabled (M18).
   - **Path rule, in a supplemental policy.** Allow `%OSDRIVE%\Program Files\Unminute\*`. App Control's path macros are `%OSDRIVE%`, `%WINDIR%` and `%SYSTEM32%`, with none for Program Files (M14). At run time App Control applies a path rule only to a folder that only administrators can write to, which B6's install folder is meant to be (T24). Do not set option 18 (Disabled:Runtime FilePath Rule Protection), which turns that check off (M14). Path rules need Windows 10 version 1903 or later, and on Windows 10 a path rule may hold one wildcard, at its start or its end (M14). To confirm.
3. **Scripts.** Script enforcement is on unless the policy turns it off. PowerShell runs a script the policy does not allow in Constrained Language Mode (M15). App Control does not control `cmd.exe` batch files such as `install.cmd`, only what they start (M15). B6 no longer waits for code signing (owner decision, 22 October 2026, #111); how to allow its installer scripts is to confirm (#189).

### What breaks

- **Per-user installs and updates.** Enforced, these rules stop every per-user copy of Unminute, its installer and its updates, so use them with the machine-wide install (B6b). With a per-user copy they also stop the `--health` detection script and the `--verify-audit` and `--status` commands above, which then run the per-user `runtime\python.exe`. Use audit mode only until the machine-wide install is deployed.
- **The GPU components.** They stay in the user's profile in both modes. Under App Control, and under AppLocker with DLL rules, their DLLs are expected to be blocked (to confirm), so the computer transcribes and identifies speakers on the processor. The app is designed to fall back to the processor and say so when a GPU component fails; that it does so when Windows blocks the load is to confirm. Setting `AllowGpuComponent` to 0 makes the choice explicit.
- **Uninstalling a per-user copy.** Under AppLocker script rules a per-user `uninstall.ps1` runs in Constrained Language Mode, and whether it completes is to confirm. Remove per-user copies before enforcing: the Intune uninstall command above, or Settings > Apps. The uninstaller's step that runs the app's runtime to put back the lid setting does not stop the uninstall when the runtime cannot run ([PACKAGING.md](PACKAGING.md), "Uninstalling"); whether a blocked runtime counts as that is to confirm.
- **Other programs in user profiles.** The same rules stop any program that runs from a user profile, not only Unminute. Audit mode shows which.
- **The WebView2 runtime.** The window needs it (`msedgewebview2.exe`). The AppLocker default rules allow Program Files, where it is normally installed; whether your App Control base policy trusts it, and where it is installed on your devices, is to confirm.

### Limits

- **An allowed Python runs any Python code.** Application control decides which programs start and, where configured, which DLLs load. It does not see the `.py` or `.pyc` files a Python runtime runs. App Control does not control scripts run through a script host that does not work with it, such as many Python engines, and allowing such a host allows every script run through it (M15). AppLocker script rules cover only `.ps1`, `.bat`, `.cmd`, `.vbs` and `.js` (M4). Once the machine-wide runtime is allowed, a user can run a per-user copy's code, or any other Python code, with it. These rules stop a per-user copy's own runtime and its installer, not a determined user: that part of the residual stays (T24). The owner accepted this limit on 22 October 2026 (#111).
- **Administrators.** The AppLocker default rules let administrators run anything (M2), and administrators may get round a managed installer (M16). These rules assume staff sign in as standard users, as the rest of this guide does.
- **AppLocker is not a security boundary.** Microsoft says it does not meet the servicing criteria for a security feature; App Control was designed as one (M13).
- **A blocked script may still run.** AppLocker and App Control both log a PowerShell script the policy does not allow as blocked, but PowerShell runs it in Constrained Language Mode (M4, M15). App Control does not control batch files (M15).
- **Path rules depend on folder permissions.** A path rule is only as strong as the folder's permissions (M6, M14). Managed installer origin is a heuristic, not an explicit rule (M16).
- **Audit mode logs; it does not block.** AppLocker's **Audit only** evaluates the rules and logs what they would block (M12): events 8003 (executables and DLLs) and 8006 (scripts and MSI files) under Applications and Services Logs\Microsoft\Windows\AppLocker (M11). App Control's audit mode logs event 3076 in CodeIntegrity - Operational, and 8028 for scripts in AppLocker - MSI and Script (M17). Some script hosts behave differently even in audit mode (M14, M15).

### Checking it

On a pilot laptop, signed in as a standard user, with the policy in audit mode (to confirm, #189):

1. Install a per-user copy with `install.cmd` and open it. Expected: AppLocker events 8003 and 8006, or App Control events 3076 and 8028, naming files in the extracted folder and under `%LOCALAPPDATA%\Programs\Unminute`.
2. Open the machine-wide install and run its `--self-test`. Expected: no such event for any file under `%ProgramFiles%\Unminute`. Note any event for `%LOCALAPPDATA%\Unminute\models` (the GPU components) and for other programs.
3. Enforce the policy and repeat. Expected: the per-user copy does not start (AppLocker 8004, App Control 3077), and the machine-wide install starts and passes `--self-test`.

Record the Windows version, the policy (AppLocker or App Control, and which rules) and each result in #189.

### Microsoft sources

Read on 22 October 2026. Each supports the statements marked with its number above.

| Ref | Page | Supports |
| --- | --- | --- |
| M1 | [Understanding AppLocker rule collections](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/applocker/understanding-applocker-rule-collections) | The five rule collections and their file types; DLL rules need a rule for every DLL, may slow some computers and are not enabled by default |
| M2 | [Executable rules in AppLocker](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/applocker/executable-rules-in-applocker) | Executable rules apply to any portable executable file whatever its extension; the default rules (administrators: all files; everyone: the Windows and Program Files folders) |
| M3 | [Understanding AppLocker allow and deny actions on rules](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/applocker/understanding-applocker-allow-and-deny-actions-on-rules) | A file no rule allows is blocked; deny rules take precedence |
| M4 | [Script rules in AppLocker](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/applocker/script-rules-in-applocker) | Script rules cover `.ps1`, `.bat`, `.cmd`, `.vbs` and `.js` only; the default script rules; PowerShell runs a "blocked" script in Constrained Language Mode |
| M5 | [DLL rules in AppLocker](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/applocker/dll-rules-in-applocker) | DLL rules cover `.dll` and `.ocx`; the default DLL rules; the performance caution |
| M6 | [Understanding the path rule condition in AppLocker](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/applocker/understanding-the-path-rule-condition-in-applocker) | Path deny rules can be got round by copying the file; avoid folders non-administrators can write to; path variables (`%PROGRAMFILES%` covers both Program Files folders) are not environment variables |
| M7 | [Understanding the file hash rule condition in AppLocker](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/applocker/understanding-the-file-hash-rule-condition-in-applocker) | A hash rule identifies one file by its Authenticode hash |
| M8 | [Understanding the publisher rule condition in AppLocker](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/applocker/understanding-the-publisher-rule-condition-in-applocker) | A publisher rule identifies a signed file by its signature, product, file name and version |
| M9 | [Configure the Application Identity service](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/applocker/configure-the-application-identity-service) | Stopping the Application Identity service stops AppLocker policies being enforced |
| M10 | [Requirements to use AppLocker](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/applocker/requirements-to-use-applocker) | Editions that enforce AppLocker (KB5024351) |
| M11 | [Using Event Viewer with AppLocker](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/applocker/using-event-viewer-with-applocker) | The AppLocker log location and events 8003, 8004, 8006 and 8007 |
| M12 | [Configure an AppLocker policy for audit only](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/applocker/configure-an-applocker-policy-for-audit-only) | Audit only evaluates the rules and logs the result |
| M13 | [App Control and AppLocker overview](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/appcontrol-and-applocker-overview) | App Control applies to the whole device; AppLocker does not meet the servicing criteria for a security feature; App Control is recommended where it can be used |
| M14 | [Understand App Control for Business policy rules and file rules](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/design/select-types-of-rules-to-create) | Audit mode (option 3); option 18; the Hash, Publisher and FilePath levels; the run-time check that a path is writable only by administrators; path macros and wildcards; FilePath from Windows 10 version 1903 |
| M15 | [Understand App Control script enforcement](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/design/script-enforcement) | Script enforcement on by default; PowerShell's Constrained Language Mode; `cmd.exe` batch files not controlled; script hosts that do not work with App Control, such as many Python engines; some hosts behave differently in audit mode |
| M16 | [Allow apps deployed with an App Control managed installer](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/design/configure-authorized-apps-deployed-with-a-managed-installer) | How managed installer tagging works; security considerations (administrators, a managed installer running as a standard user); no support for apps that update themselves |
| M17 | [Understanding App Control event IDs](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/event-id-explanations) | Events 3076, 3077 and 8028 and their logs |
| M18 | [Manage approved apps with App Control for Business policy and managed installers in Intune](https://learn.microsoft.com/en-us/intune/device-configuration/endpoint-security/manage-app-control) | Intune's built-in controls and supplemental policies; the Intune Management Extension as a managed installer; no tagging of apps deployed before it was set; turning on the managed installer merges an AppLocker policy into the device's, where a NotConfigured collection with no rules can block apps, start-up or sign-in, and can turn on stopped or disabled AppLocker policies from Group Policy; the AppLocker CSP still supported but no longer gaining features |
| M19 | [AppLocker CSP](https://learn.microsoft.com/en-us/windows/client-management/mdm/applocker-csp) | The configuration service provider that sets AppLocker policy through mobile device management |

## Verify a release before packaging

Run IT's verifier on every download before you package it. The `.sha256` beside a zip detects a damaged download, not a replaced one.

```
powershell -NoProfile -File tools\intune\Get-VerifiedRelease.ps1 -Tag v0.5.0-preview.4 -AllowPrerelease
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
4. Return codes: map 5 (the update is waiting for the workspace) to Retry, and 1, 2, 3, 4 and 6 to Failed. Code 3 (the app is running) is worth a retry. Code 6 means this version cannot open the user's existing workspace, for example a newer workspace after a downgrade, or one in a refused location such as OneDrive or a network drive (see "Updates and rollback" below, and the exit codes in [PACKAGING.md](PACKAGING.md)). For a failed install, the installer's log, `logs\install.log` in the install folder, gives the step it stopped at and its exit code, with no user name or path (#111 B6a).
5. Detection rule: the registry value `DisplayVersion` under `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Uninstall\Unminute` equals the version.
6. Requirements: Windows 10 1809 or later, x64, and the WebView2 runtime.

For the machine install (B6b), the full settings are in [PACKAGING.md](PACKAGING.md), "Machine install in System context". In short:

1. Verify the release, then package the zip itself with its `.sha256` file and that version's `install.ps1` (from inside the zip). Run it only from a folder that only administrators can change, such as the Intune Management Extension's content folder (its permissions are to confirm, #189): whoever can write there can replace the zip and its `.sha256` together before the run.
2. Install behaviour: **System**. Install command: `%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Machine -Package Unminute-<version>.zip -Silent`.
3. Uninstall command: `%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ProgramW6432%\Unminute\uninstall.ps1" -Machine -Silent`. It refuses (exit code 5) while a version kept for an earlier storage format is installed; add `-IncludeKeptSlots` only to decommission.
4. Return codes: map 3 (the app is open for someone) to Retry, and 1, 2 and 4 to Failed. Code 4 also means the install folder's permissions have changed: check the computer.
5. Detection rule: `DisplayVersion` under `HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Unminute` equals the version.
6. Updates: new content and detection version for the same Win32 app, or supersedence **without** "Uninstall previous version", which would remove the version kept for users still on the earlier storage format.

Both routes are still to be run through Intune on a managed device, including whether the Intune Management Extension starts a 32-bit Windows PowerShell (#189).

The scripts are not signed, and both routes run them with `-ExecutionPolicy Bypass`: an accepted risk (the owner's decision of 22 October 2026, SECURITY_MODEL.md T22). An environment that enforces `AllSigned` cannot run them as shipped; [PACKAGING.md](PACKAGING.md), "Signing and trust", lists the options for signing them yourself.

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
| `ReviewReminderDays` | Days before a meeting's next step shows "Review due" | Retention reminders | 30 days |
| `ActionRequiredDays` | Days before a meeting's next step shows "Action required" | Retention reminders | Off |
| `HandoffDefaultFolder` | Starting folder for saved handoffs | Handoff | The dialog's usual folder |
| `AllowHandoffAssistant` | Allow "Copy and open" on the Hand off page | Handoff | Allowed |
| `HandoffAssistantName` | Hand off assistant name | Handoff | ChatGPT |
| `HandoffAssistantUrl` | Hand off assistant address | Handoff | `https://chatgpt.com/` |

New in v0.5.0-preview.4: `AllowHandoffAssistant`, `HandoffAssistantName` and `HandoffAssistantUrl`, and the Copy and open button they control; `AllowComputerAudioRecording`; `AllowLiveDrafts`; and `ReviewReminderDays` and `ActionRequiredDays`, and the retention reminders they set (#111 C6). Earlier versions ignore them, as they ignore any value name they do not know. Users can turn on **Also record computer audio** (#108; off until each user turns it on): it records everything the computer plays, such as the other side of a Teams or Zoom call, and also notification sounds. Set `AllowComputerAudioRecording` to 0 to turn it off. It never applies when `AllowMicrophoneRecording` is 0. `AllowLiveDrafts` governs draft text while recording (#104): rough text of what is said, kept in memory only and replaced by the full transcript. The recording card shows it and Settings has the switch, off until the user turns it on. Set to 0, it cannot be turned on, and a draft in progress is stopped and cleared when the app next reads the policy, while the recording carries on. The template's revision number did not change when they were added, and Intune does not overwrite an imported template: to take the new values, delete the profiles that use the template and the old template, then import again ([PACKAGING.md](PACKAGING.md)).

Value names are not case-sensitive. A name the app does not recognise (a misspelling, a trailing space or a lookalike letter) grants and withholds nothing, so check names against the template. The app keeps the first 20 such names, never their data. Settings lists them near the top of the page (a trailing space shows as `\x20`, #300), and Copy diagnostics says how many there are. In a `HandoffDefaultFolder` set as an expandable string, the app expands only `%NAME%` variables, as Windows does ([PACKAGING.md](PACKAGING.md), "Managed settings (policy)").

The app enforces these values in its own code. In a per-user install that code sits in the user's profile, so someone able to change files there could remove the checks: policy is not enforced against a determined user. In a machine install (B6b) the code is in `%ProgramFiles%\Unminute`, which standard users cannot change; a user can still run their own code with the app's runtime, which application control cannot stop once the runtime is allowed (#304). The policy key itself stays admin-only.

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

For a machine install (B6b), read `current.json` from `%ProgramFiles%\Unminute` instead (`Join-Path $env:ProgramW6432 'Unminute\current.json'`), still running as the signed-in user.

| `--health` shows | What it means | What to do |
| --- | --- | --- |
| `"workspace": "none"` | This user has not opened the app yet | Nothing; it counts as healthy |
| `"workspace": "locked"` | At the app's last start Windows would not unlock the workspace key, so the workspace opened locked: no meeting is read or changed (only the problem is logged) and work waits. Read from what the app recorded, never by trying the key; it takes precedence over `damaged` | The user guide, [If Unminute cannot unlock its workspace](WORKSPACE_GUIDE.md#if-unminute-cannot-unlock-its-workspace) |
| `"workspace": "damaged"` | The last database check found damage | The user guide, [If Unminute finds damage](WORKSPACE_GUIDE.md#if-unminute-finds-damage) |
| `"workspace": "location"` | The data folder is missing, in OneDrive, on a network drive, or a link to one of those or to a folder that cannot be reached | Restore the folder or remove the link; only `Unminute\models` may be redirected ([PACKAGING.md](PACKAGING.md), "What the package contains") |
| `"workspace": "unreadable"` | The workspace cannot be read, or Windows cannot say which account the check runs as | Ask the user for **Copy diagnostics** (Help); see the user guide's troubleshooting table |
| `"workspace": "system_account"` | The script ran as SYSTEM | Set it to run with the logged-on credentials |
| `"last_run": "did_not_close"` or `crashes_7d` above 0 | The app or one of its processes ended unexpectedly | Ask the user for **Copy diagnostics** and any code shown (`U-…`); if many devices show it after an update, see "Incidents" below |
| `action_required` above 0 | Meetings whose next step (review, hand off, or confirm or finish cleanup) has waited `ActionRequiredDays` or more. `held_overdue` counts held meetings past the threshold, which show no reminder. Neither changes `healthy` or the exit code: a backlog is not an app fault | To alert on it, make the detection script also exit 1 when `action_required` is above 0 ([PACKAGING.md](PACKAGING.md), "Managed deployment (Intune)"). The user sees the same meetings marked "Action required" on the Meetings page; whoever manages holds reviews `held_overdue` |
| `"update_waiting": true` (machine mode) | An installed update is waiting: the user's workspace still holds meetings, holds or unfinished cleanup, so the version kept for it opens, for finishing them only. Still healthy | Nothing urgent; the update takes over once the workspace is empty. Intune reports the new version installed meanwhile, so this field is the only sign of it |
| `"slot": "fallback"` or `"self_check": "slot_fault"` | The installed version failed its first-start check on this computer (for example a file quarantined or WebView2 too old), so another kept version opened, or nothing did | Repair or reinstall the version; the check runs again once its install time changes. Ask the user for the code shown (`U-…`) |
| `"self_check": "env_fault"` | Windows Data Protection failed for this user, so the workspace opens locked, and in machine mode no update migrates it | The user guide, [If Unminute cannot unlock its workspace](WORKSPACE_GUIDE.md#if-unminute-cannot-unlock-its-workspace) |

When the app cannot unlock its encryption key, it opens locked rather than closing and shows only a locked screen: the reason, the codes, your steps and three buttons (**Copy details for IT**, **How to recover**, **Close Unminute**). Every other request is refused, and no meeting is read or changed (only the problem is logged). `--status` shows `"keys": "locked"` with the reason and a code. If Windows cannot set up a key for a new workspace at all, the app says so with a code and closes, creating nothing. The steps are in the user guide, [If Unminute cannot unlock its workspace](WORKSPACE_GUIDE.md#if-unminute-cannot-unlock-its-workspace).

## Audit log

The app keeps a content-free, hash-chained audit log in `%LOCALAPPDATA%\Unminute\workspace\audit\`. It records imports and recordings (with the recording confirmation's outcome, its wording version, and whether the notice named computer audio), the review and final confirmations, discards, holds, handoffs, watched folders, changes to delete-originals authority, and each cleanup. Each record carries the kind of action, the time, the app's own random ids, fixed words and counts, never text, titles, names, folders or hold reasons. It is always on, with no policy value (the owner's decision, 21 Oct 2026). It is capped at 16 MB: past that the oldest 1 MB segment is deleted, and a record says so. It is not in the database, so an update that waits for an empty workspace keeps it. The full description is in [PACKAGING.md](PACKAGING.md#the-audit-log-and-its-event-log-copy-111-c5) and SECURITY_MODEL.md T20.

- **The Event Log copy.** Each record's chain, sequence number, kind and SHA-256, never an id, go to the Windows Application log under the source `Unminute`, with the signed-in user's SID. A per-user install does not register the source, so Event Viewer shows the text without a description and a SIEM reads it from `EventData`; a machine install (B6b) registers it, so Event Viewer shows the text, and its uninstall removes the registration again. The event IDs and levels are in PACKAGING.md. Forward the log off the device for the stronger record (see "Controls Unminute relies on IT for" above).
- **Checking it.** Run, as the signed-in user (never as SYSTEM, as for `--health`), from the installed version folder:

  ```
  runtime\python.exe -I -B -m workspace --verify-audit --event-log
  ```

  It needs no key, reads no database and prints one line of JSON with counts and states only. `result` is `ok` when the chain is whole, or `broken` with the sequence number and a reason. `--event-log` compares the file with the signed-in user's `Unminute` entries in this computer's Application log; `--event-log-file <path>` compares it with an exported `.evtx` file instead, for example from the forwarded copy. It exits 0 when the chain is `ok` and, if compared, matches; 1 otherwise. `--status` also shows `audit_seq`, the newest record's sequence number.
- **What it cannot show.** The file alone cannot show whole lines removed from its end, a rebuilt chain or a deleted folder: those show only against the Event Log copy, while it still holds the entries. The SID on an entry is supplied by whoever writes it, so it attributes the app's own entries but does not prove who wrote one. A crash between an action and its record loses the record, and can leave the last few records without an Event Log copy.

## Updates and rollback

- **Same storage format:** installing the new version keeps everything.
- **New storage format:** a per-user update waits (exit code 5) until the user's workspace holds no meetings, active holds, unfinished cleanup, microphone recordings waiting to be imported or meeting audio. It never expires a hold, deletes content or backs content up to proceed. Map 5 to Retry in Intune; Settings > Updates in the app says what it is waiting for.
- **Encryption at rest is a storage-format change.** It came in one release, v0.5.0-preview.4 (schema 2), once #111 phase C was complete, and each user's workspace must be empty before that version takes it over. A per-user install checks at install time and waits (exit code 5) until the workspace is empty. A machine install has no install-time check: it installs, and each user's start decides ([PACKAGING.md](PACKAGING.md), "Launch-time hand-over"). At a later storage-format change, the version kept for the earlier format opens a workspace that still holds content, for finishing meetings only, until it is empty, then the new version takes it over. For v0.5.0-preview.4, the first machine-wide release, no earlier version can be kept: v0.5.0-preview.3 states no storage format, so `install.ps1 -Machine` refuses its package (exit code 2). A user whose workspace still holds anything the update gate counts therefore sees a message naming IT, with the counts, and nothing opens until that workspace is empty, for example after finishing its meetings in the user's earlier per-user copy, which the machine install leaves alone. A version before it cannot open a schema-2 workspace.
- **Rollback:** `install.ps1 -Rollback` from the current version's folder returns to the previous installed version, after the same check with that version. It has no minimum-version check (T26). It goes back only to a version folder the installer made (#297).
- **The first release after the rename (#136)** has no earlier version in its install folder to roll back to.

Details: [PACKAGING.md](PACKAGING.md), "Updates (#26)".

## Offboarding

Unminute removes what it keeps for one Windows user with a command run **as that user**, with the app closed (#111 B8a), or with the uninstaller's `-Offboard` switch, which then removes the program too (B8b, step 4). A plain uninstall still removes only the program, its shortcut and its uninstall entry, and deliberately keeps the workspace, the models and the Hugging Face token (T29). Run the commands in steps 2 and 3 from the user's installed version folder. When a person leaves or a laptop is reassigned:

1. **With the user, before they leave (the handover period):** hand off or copy what is still needed, confirm cleanup of finished meetings, and check holds with whoever manages them. Holds never expire, and releasing one deletes nothing.
2. **Check what is left:** `runtime\python.exe -I -B -X disable-remote-debug -m workspace --offboard-report`. It prints one line of JSON with counts and yes or no only, and changes nothing. Exit codes: 0 nothing to remove, or all of it removable; 2 run as Local System, or Windows cannot say who runs it; 3 the app is open (in any session of the user); 4 as 0, but other entries in `Unminute` stay (step 5); 5 blocked; 6 the location is refused (a junction or link at `Unminute` or `Unminute\workspace`, or a folder the app itself refuses). The fields are in [PACKAGING.md](PACKAGING.md), "Offboarding".
3. **Remove it:** close the app, then run the same command with `--offboard`. It removes nothing while anything the update gate counts is left (meetings, holds, unfinished cleanup, recordings waiting to be imported, meeting audio), in a locked workspace too: finish those in the app first. An empty workspace whose key Windows will not unlock is removed without the key. It puts the lid setting back, writes a last `offboarded` audit record and its Event Log copy (event ID 1700), erases the workspace's root key, then removes the workspace (with its logs, audit log, settings, templates and window data), the models (a models folder IT redirected: only the link; its target is kept), the token, the decoder's AppContainer profile ([Protected decoder](#protected-decoder)), and `%LOCALAPPDATA%\Unminute` if it is then empty. Exit 0 when done; 4 when done but other entries in `Unminute` were left (step 5); 1 when a step failed (`step` names it: for example a file still in use after 30 seconds, or an Event Log that would not take the record), and running it again finishes the job; 2, 3, 5 and 6 as above, with nothing removed.
4. **Uninstall:** Settings > Apps > Unminute, or the Intune uninstall command. **Or do steps 3 and 4 in one,** as the user: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\Programs\Unminute\uninstall.ps1" -Offboard -Silent`. It runs `--offboard-report` and `--offboard` first, and removes the program, its Start menu shortcut, its taskbar and Start pins and its uninstall entry only when offboarding finished. Other entries in `Unminute` (step 5) do not stop it: it warns with their count and carries on, and exits 0 (the owner's decision of 23 Oct 2026); its output's JSON line then says `others_left`. It runs only for the running account's own install (the folder its uninstall entry names, or one in its own `%LOCALAPPDATA%`), and exits 1 with nothing run otherwise. It offboards while it holds the install folder's `install.lock`, so no install or update of Unminute can start meanwhile; when one is already running it exits 3 with nothing run, so its exit 3 means an install or update is running or the app is open. Anything else stops it with `--offboard`'s exit code (steps 2 and 3; 1 also when the installed version is too old for the switch, or its files are incomplete while data is left) and keeps the program, so the person can still finish their meetings with it; run it again once they have. A rerun after a program removal that stopped part-way finishes it when nothing of offboarding's is left. Without `-Silent` it shows what will go and asks the person to type `REMOVE`. Do not make it the Win32 app's uninstall command: every uninstall, such as a supersedence, would then offboard too. Details: [PACKAGING.md](PACKAGING.md), "`uninstall.ps1 -Offboard`".
5. **What it cannot reach**, also printed as `residuals`: copies made outside the app (whole-disk or profile backups, Volume Shadow Copies, File History), which this account's or the domain's Windows keys can still open; plain text deleted before encryption at rest, which an SSD may keep in free space; and the audit log's Event Log copy, which stays in the Application log, and in any forwarded copy, under its own retention. The audit log is not exported first (the owner's decision B8-4): the Event Log copy is the lasting record. Offboarding does not look at anything version 0.4.3 or earlier left (the owner's decision of 22 Oct 2026); moving from that version is in [PACKAGING.md](PACKAGING.md), "Updates (#26)". Never copy a workspace to a share or another computer.
   **Other entries in `Unminute`**, such as a workspace renamed by the steps in [WORKSPACE_GUIDE.md](WORKSPACE_GUIDE.md) ("If Unminute finds damage", or when Windows will not unlock the key), are left alone, because one may still hold meetings. The output counts them in `others`, without names, and ends with `others_left` and exit 4 instead of a clean result. Check with whoever manages holds, delete them by hand once nothing in them is needed, then run `--offboard` again: it ends with exit 0 and removes `%LOCALAPPDATA%\Unminute`. `uninstall.ps1 -Offboard` does not stop for them: it warns with their count, removes the program and exits 0, so check its output for `others_left`.
6. **With Intune:** run `--offboard`, or `uninstall.ps1 -Offboard -Silent`, as a script or remediation **with the logged-on user's credentials**, as for `--health`. Run as Local System (or when Windows cannot say which account runs it), offboarding reads nothing and exits 2; the uninstaller exits 2 too, before it starts anything, and keeps the program. Never run either as Local System or as another account, such as an IT account naming a leaver's install folder: the install and the data are the user's own. The uninstaller refuses an install folder that is not the running account's own (the folder its uninstall entry names, or one in its own `%LOCALAPPDATA%`) with exit 1 and runs nothing. That check is a guard rail against naming the wrong folder, not a security boundary: it trusts the running account's own `%LOCALAPPDATA%` and uninstall entry.
7. **A leaver who is not there:** offboarding needs the person's own session (their profile and their Windows keys), so do it during the handover period while the account is active. If the account is already disabled, IT's fallback is deleting the Windows profile, which removes the workspace and that user's Windows Data Protection keys with it (domain backup keys are to confirm, #189). That is IT's process, outside the app's confirmations; Unminute never runs as Local System inside a profile.
8. **Reassigning the laptop:** a reset or wipe that does not keep user data removes the profile, and the workspace with it.

## Incidents

Start from the kind of incident. None of these steps needs meeting content: never ask a user to send a workspace, a recording or a transcript.

| Incident | First steps | Runbook |
| --- | --- | --- |
| A release has a severity 1 issue (content lost or exposed, the core task broken, updates blocked) | The maintainer withdraws it: the previous good release becomes the default download, and the bad release's notes carry a warning. IT stops assigning the bad version. A per-user install rolls back with `install.ps1 -Rollback`, unless the bad version changed the storage format, in which case the fix is forward. A machine install (B6b) goes back by assigning the previous version, or `install.ps1 -Machine -Rollback`; going back to an earlier storage format removes the newer version, and users whose workspace it changed see "ask your IT team" until a fix ships | [Withdrawing a bad release](RELEASING.md#withdrawing-a-bad-release); severity is defined in [RELEASE_STANDARD.md](RELEASE_STANDARD.md), section 14 |
| Many devices report unhealthy after an update | Read `--health` across devices: crashes, damage, a locked workspace or location. Ask affected users for **Copy diagnostics** | [Health monitoring](#health-monitoring-with---health) above; [PACKAGING.md](PACKAGING.md#managed-deployment-intune) for the fields |
| Imports are refused with "can't be opened safely on this computer" (**Not in v0.5.0-preview.3**) | As the user, run `--check-decoder`; the step and Windows error number say what failed | [Protected decoder](#protected-decoder) above |
| One user's workspace is damaged or will not open | Follow the user guide's recovery steps; there are no backups, by design | [If Unminute finds damage](WORKSPACE_GUIDE.md#if-unminute-finds-damage) |
| The app cannot unlock its key | `--health` shows `locked`. Ask for **Copy details for IT** from the locked screen (the same text as **Copy diagnostics**), which holds the code; `--status` still works | [If Unminute cannot unlock its workspace](WORKSPACE_GUIDE.md#if-unminute-cannot-unlock-its-workspace) |
| Someone needs to show what was exported or deleted, or the audit log may have been changed | As the user, run `--verify-audit --event-log`; or compare with the forwarded copy, exported as `.evtx`, with `--event-log-file`. It reports counts and states only | [Audit log](#audit-log) above; SECURITY_MODEL.md T20 for what it cannot show |
| A suspected security vulnerability in Unminute | Report it privately, with synthetic data only | [SECURITY.md](../SECURITY.md) |
| A laptop is lost or stolen, or content went somewhere it should not | Follow your organisation's own incident process. What Unminute keeps on the laptop, and what is encrypted, is in the privacy impact assessment draft | [PRIVACY_IMPACT_ASSESSMENT_DRAFT.md](PRIVACY_IMPACT_ASSESSMENT_DRAFT.md) |

## Not available yet

- Machine-wide models staged by IT, and the runtime firewall rule (B7).
- Code signing of the zip and the installer scripts. Not planned: running them unsigned with `-ExecutionPolicy Bypass` is an accepted risk (the owner's decision of 22 October 2026, T22). Releases from 0.5.0 carry signed build provenance and an SBOM attestation instead; signing options are in [PACKAGING.md](PACKAGING.md), "Signing and trust".
