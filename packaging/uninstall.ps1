<#
.SYNOPSIS
  Removes Unminute for the current Windows user (#11).

.DESCRIPTION
  Removes the installed program, its Start menu shortcut and its uninstall
  entry. It removes only what the installer put in the install folder
  (versions, current.json and its temporary copy, the logs folder with the
  installer's log, install.lock and this script; #111 B6a); if the folder holds
  anything else, or is neither an install nor the folder the uninstall entry
  names, it removes nothing. It holds install.lock while it removes, so it never
  runs beside an install or update. It never removes through a link or junction: one at
  any of those entries stops it before anything is removed,
  and one inside a version is removed as a link, leaving what it points to
  (#297). When the uninstall entry names another folder that still
  holds an install, the shortcut and the entry are that install's, and are
  kept. It does NOT remove the workspace (meetings, holds, settings) or the
  downloaded models: meeting content is only removed through the app's own
  confirmed cleanup, which honours retention holds. If meetings remain, this
  says so.

  Before removing anything, it puts back the Windows lid setting if the app
  left it at "Do nothing" (#191): see Restore-LidSetting. That step never stops
  the uninstall.

  With -Offboard (#111 B8b), run as the person, it first has the installed app
  remove what Unminute keeps for them (python -m workspace --offboard: the
  workspace, the downloaded models and the token), and removes the program,
  its shortcut, its taskbar and Start pins and its uninstall entry only when
  that finished. Other items left in the data folder (a workspace set aside,
  say) are reported with their count and the uninstall carries on (the
  owner's decision of 23 Oct 2026). If anything else stops offboarding
  (meetings, holds or cleanup left, the app open, Local System), the
  uninstall stops too and the program is kept, so the person can still
  finish with it. It runs only for an install that is the running account's
  own (the folder its uninstall entry names, or one in its own
  %LOCALAPPDATA%), and never as Local System: either stops it before anything
  is run. See docs/PACKAGING.md, "Offboarding". Without -Silent it shows what
  will go and asks for REMOVE first.

  Exit codes: 0 removed, 1 failed (or, with -Offboard, not confirmed, or not
  this account's install), 3 the app, or an install or update, is running.
  With -Offboard, also --offboard's own codes when it stops the uninstall: 1 a
  step failed, 2 Local System (or Windows could not say which account; the
  uninstaller refuses this itself, before starting anything), 3 the app is
  open, 5 blocked, 6 the data folder's location is refused.

  MACHINE MODE (-Machine, #111 B6b): removes the install for everyone on this
  computer from %ProgramFiles%\Unminute, run by IT as SYSTEM in a 64-bit,
  elevated Windows PowerShell. It checks the folder's permissions as the
  installer does, refuses while the app runs for anyone on the computer, and
  removes only the installer's own entries (the versions, the current junction
  as a link, current.json, the logs, install.lock, staging folders and this
  script), the Start menu entry for all users, the Event Log source and, last,
  the machine-wide uninstall entry. While a version kept for people whose workspace has
  not moved to the current storage format is installed, it removes nothing
  unless -IncludeKeptSlots is given (exit 5), so a misconfigured "uninstall
  previous version" in Intune fails safe (#111 B6-11). It never reads or writes
  a user's profile: workspaces, models, tokens and the lid setting are left as
  they are (#111 B6-9), so -Offboard, which is each person's own, is refused.
  Machine exit codes: 0 removed, 1 failed (including anything in the folder that
  the installer did not put there), 2 not allowed (a 32-bit or non-elevated run,
  or -InstallDir or -Offboard with -Machine), 3 the app, or an install or update,
  is running, 4 the install folder's permissions have changed, 5 a version kept
  for an earlier storage format is still installed.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Unminute"),
    [switch]$Silent,
    [switch]$Offboard,
    [switch]$Machine,
    [switch]$IncludeKeptSlots
)

$ErrorActionPreference = "Stop"
# Parameters, read by the functions below.
$Options = @{ InstallDir = $InstallDir; Silent = [bool]$Silent; Offboard = [bool]$Offboard; Machine = [bool]$Machine
             IncludeKeptSlots = [bool]$IncludeKeptSlots; InstallDirGiven = $PSBoundParameters.ContainsKey("InstallDir") }
$AppName = "Unminute"
$RegKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Unminute"
# The Start menu's Programs folder, for the shortcut. Read once, here, like $RegKey, so
# the tests can point both at scratch places (tests/powershell/Isolation.ps1).
$ProgramsFolder = [Environment]::GetFolderPath("Programs")
# What the installer may put in the install folder, and so all this removes there
# (#297, #111 B6a). install.ps1 and uninstall.ps1 list the same names, and
# packaging/identity.json mirrors them (tests keep the three equal).
$OwnEntries = @("versions", "current.json", "current.json.tmp", "uninstall.ps1", "logs", "install.lock")
$LockName = "install.lock"
$LogNames = @("install.log", "install.log.1")
# How the installer starts the app (install.ps1 has the same; tests keep them equal):
# isolated, no bytecode written, no remote debugging (#111 B4).
$LaunchArgs = "-I -B -X disable-remote-debug -m workspace"
$script:InstallLock = $null
# The longest the lid restore may take before it is stopped (it needs about a second).
$LidRestoreSeconds = 60
# The longest --offboard-report or --offboard may take before it is stopped. Removing
# several GB of models takes about a minute, and a file in use is waited for 30 s.
$OffboardSeconds = 900

function Write-Step([string]$Message) {
    if (-not $Options.Silent) { Write-Host $Message }
}

function Write-Warn([string]$Message) {
    if ($Options.Silent) { [Console]::Error.WriteLine("Warning: $Message") }
    else { Write-Host "Warning: $Message" -ForegroundColor Yellow }
}

# -- the lid setting (#191) -----------------------------------------------------------
# While recording, the app may set what closing the lid does to "Do nothing", having
# saved the original values in power_restore.json in the workspace. It puts them back
# when the recording ends and at every start. If it never starts again after a crash,
# this puts them back before the program is removed, by running the installed app once
# with --restore-lid: the app's own rules (only a valid record, only sides still at
# "Do nothing"), which never touch IT's policy settings. Whatever happens, the
# uninstall carries on; anything but success only says to check the setting.

# The current version's folder: a plain folder name from current.json (never a path;
# \z, unlike $, refuses a final newline), or $null. Also used by -Offboard (#111 B8b).
function Get-CurrentVersionFolder {
    $currentFile = [IO.Path]::Combine($Options.InstallDir, "current.json")
    if (-not (Test-Path -LiteralPath $currentFile -PathType Leaf)) { return $null }
    $version = [string](Get-Content -LiteralPath $currentFile -Raw | ConvertFrom-Json).version
    if ($version -cnotmatch '^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\z') { return $null }
    return [IO.Path]::Combine($Options.InstallDir, "versions", $version)
}

# The current version's folder, if it can run the restore: a version.json that lists
# the feature, so an older version never takes the switch for an ordinary start and
# opens its window.
function Get-LidRestoreVersion {
    $target = Get-CurrentVersionFolder
    if (-not $target) { return $null }
    $info = Get-Content -LiteralPath ([IO.Path]::Combine($target, "version.json")) -Raw | ConvertFrom-Json
    if (@($info.features) -notcontains "restore-lid") { return $null }
    if (-not (Test-Path -LiteralPath ([IO.Path]::Combine($target, "runtime", "python.exe")) -PathType Leaf)) { return $null }
    return $target
}

# Runs the restore with a hard time limit. Returns its exit code, or -1 if it was stopped.
function Invoke-LidRestore([string]$Target) {
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = [IO.Path]::Combine($Target, "runtime", "python.exe")
    # As the installer starts the app: isolated, no bytecode written, no remote debugging.
    $start.Arguments = "$LaunchArgs --restore-lid"
    $start.WorkingDirectory = [IO.Path]::Combine($Target, "app")
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    # Its output ({"lid": state}) is read so a full pipe never blocks it, and not shown.
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($start)
    try {
        [void]$process.StandardOutput.ReadToEndAsync()
        [void]$process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($LidRestoreSeconds * 1000)) {
            try { $process.Kill() } catch { }
            [void]$process.WaitForExit(10000)        # so no file in the install stays open
            return -1
        }
        return $process.ExitCode
    } finally {
        $process.Dispose()
    }
}

function Restore-LidSetting {
    try {
        $local = $env:LOCALAPPDATA
        # A missing, relative or network LOCALAPPDATA: the app refuses it too, so it never
        # left a record there.
        if ([string]::IsNullOrWhiteSpace($local) -or -not [IO.Path]::IsPathRooted($local) -or
            $local -match '^[\\/]{2}') { return }
        $record = [IO.Path]::Combine($local, $AppName, "workspace", "power_restore.json")
        if (-not (Test-Path -LiteralPath $record -PathType Leaf)) { return }   # nothing waiting
    } catch { return }
    $code = -1
    try {
        $target = Get-LidRestoreVersion
        if ($target) { $code = Invoke-LidRestore $target }
    } catch { $code = -1 }
    if ($code -eq 0) {
        Write-Step "The lid setting that $AppName changed was put back."
    } else {
        Write-Warn ("$AppName could not put back what closing the lid does. Check it in Windows " +
            "Settings > System > Power & battery > Lid & power button controls.")
    }
}

function Stop-Uninstall([int]$Code, [string]$Message) {
    if ($Options.Silent) { [Console]::Error.WriteLine($Message) } else { Write-Host $Message }
    exit $Code
}

# The data (workspace and models) is in %LOCALAPPDATA%\Unminute, and uninstalling
# deletes the install folder, so the two must never overlap. Returns the data folder
# when the install folder is, holds or sits inside it. install.ps1 and uninstall.ps1
# have the same function (tests/test_hardening.py keeps them equal).
function Get-DataOverlap([string]$Dir) {
    $data = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Unminute")).TrimEnd("\") + "\"
    $full = [IO.Path]::GetFullPath($Dir).TrimEnd("\") + "\"
    if ($full.StartsWith($data, [StringComparison]::OrdinalIgnoreCase) -or
        $data.StartsWith($full, [StringComparison]::OrdinalIgnoreCase)) {
        return $data.TrimEnd("\")
    }
    return $null
}

# True when the folder holds an install (current.json or versions). Built with
# [IO.Path]::Combine, which unlike Join-Path does not fail on a drive that is gone.
function Test-Install([string]$Dir) {
    try {
        return (Test-Path -LiteralPath ([IO.Path]::Combine($Dir, "current.json"))) -or
            (Test-Path -LiteralPath ([IO.Path]::Combine($Dir, "versions")))
    } catch { return $false }
}

# A folder cannot be renamed or removed while any process has a file in it open, and
# antivirus software (such as Microsoft Defender) opens newly written .exe, .dll and
# .pyd files for a moment to scan them (#239). So a rename or removal refused as access
# denied or in use is tried again, with a short back-off, for up to $LockRetrySeconds
# from its first refusal; then its error stands. The wait starts at the first refusal,
# not at the start of the step, because removing a large folder can take longer than
# that before it reaches the file in use. Any other error is not retried. install.ps1
# and uninstall.ps1 have the same functions (tests/test_hardening.py keeps them equal).
$LockRetrySeconds = 30

function Test-TransientLock([Exception]$Exception) {
    for ($e = $Exception; $e; $e = $e.InnerException) {
        if ($e -is [UnauthorizedAccessException]) { return $true }
        if ($e -isnot [IO.IOException]) { continue }
        # Access denied (0x80070005) or in use, a sharing violation (0x80070020).
        if (@(0x80070005, 0x80070020) -contains $e.HResult) { return $true }
        # Windows PowerShell's folder rename reports access denied as a plain
        # IOException without the Windows code (0x80131620, "Access to the path ... is
        # denied"). Other failures of that rename carry their own code.
        if ($e.GetType() -eq [IO.IOException] -and $e.HResult -eq 0x80131620) { return $true }
    }
    return $false
}

# Runs $Action (which must not use the names below) until it succeeds, it fails for
# any reason but a file in use, or $LockRetrySeconds have passed since its first refusal.
function Invoke-LockRetry([scriptblock]$Action) {
    $clock = $null
    $pause = 250
    $said = $false
    while ($true) {
        try {
            & $Action
            return
        } catch {
            if ($null -eq $clock) { $clock = [Diagnostics.Stopwatch]::StartNew() }
            $left = $LockRetrySeconds * 1000 - $clock.ElapsedMilliseconds
            if ($left -le 0 -or -not (Test-TransientLock $_.Exception)) { throw }
            if (-not $said) {
                Write-Step "A file in the folder is in use, perhaps by a virus scan. Trying again for up to $LockRetrySeconds seconds..."
                $said = $true
            }
            Start-Sleep -Milliseconds ([Math]::Min($pause, $left))
            $pause = [Math]::Min($pause * 2, 4000)
        }
    }
}

# Removes a folder the installer made without following links (#297): a junction or
# symbolic link found inside it is removed as a link, and what it points to is left
# alone. A folder that is itself a link is never removed. Windows PowerShell's own
# Remove-Item -Recurse is not relied on for this. install.ps1 and uninstall.ps1 have
# the same functions (tests/test_hardening.py keeps them equal).
function Test-Link([IO.FileSystemInfo]$Item) {
    return [bool]($Item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Remove-OwnFolder([string]$Path) {
    $dir = [IO.DirectoryInfo]::new($Path)
    if (-not $dir.Exists) { return }
    if (Test-Link $dir) { throw "A folder to remove is a link or junction, so it was left in place." }
    Remove-FolderTree $dir
}

function Remove-FolderTree([IO.DirectoryInfo]$Dir) {
    foreach ($item in $Dir.GetFileSystemInfos()) {
        if ($item -is [IO.DirectoryInfo] -and -not (Test-Link $item)) { Remove-FolderTree $item; continue }
        if (-not (Test-Link $item) -and ($item.Attributes -band [IO.FileAttributes]::ReadOnly)) {
            $item.Attributes = $item.Attributes -bxor [IO.FileAttributes]::ReadOnly
        }
        $item.Delete()                  # a file, or the link itself
    }
    if ($Dir.Attributes -band [IO.FileAttributes]::ReadOnly) {
        $Dir.Attributes = $Dir.Attributes -bxor [IO.FileAttributes]::ReadOnly
    }
    $Dir.Delete()
}

# -- offboarding, with -Offboard (#111 B8b) ---------------------------------------------
# The installed app removes what Unminute keeps for this Windows user (python -m workspace
# --offboard; docs/PACKAGING.md, "Offboarding"), run as this user, as the lid restore is
# run, and only for an install that is this user's own (Assert-OwnInstall): never as
# Local System, which the app refuses too. It first reports (--offboard-report), then
# removes (--offboard). A clean result ("ok", then "offboarded", exit 0) lets the
# uninstall carry on, and so does one that left only other entries in the data folder
# ("others_left", exit 4: reported with their count, the owner's decision of 23 Oct
# 2026). Anything else stops it with the app's own exit code and the program kept, so
# a person with meetings, holds or cleanup left can still finish them with it (the
# owner's decision B8-2, 21 Oct 2026). The app's output is one line of counts and
# states (no path, name or content), passed on to standard output.

# Who runs this: the account's SID, or $null when Windows cannot say.
function Get-AccountSid {
    return [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
}

# True when $Dir is inside this user's %LOCALAPPDATA% and no folder on the way down
# from it is a link or junction (so it cannot lead into another profile).
function Test-UnderLocalAppData([string]$Dir) {
    try {
        $local = $env:LOCALAPPDATA
        if ([string]::IsNullOrWhiteSpace($local) -or -not [IO.Path]::IsPathRooted($local) -or
            $local -match '^[\\/]{2}') { return $false }
        $base = [IO.Path]::GetFullPath($local).TrimEnd("\")
        $full = [IO.Path]::GetFullPath($Dir).TrimEnd("\")
        if (-not $full.StartsWith($base + "\", [StringComparison]::OrdinalIgnoreCase)) { return $false }
        for ($at = $full; $at.Length -gt $base.Length; $at = [IO.Path]::GetDirectoryName($at)) {
            # (Not $dir: PowerShell names are not case-sensitive, and $Dir is a string.)
            $step = [IO.DirectoryInfo]::new($at)
            if ($step.Exists -and (Test-Link $step)) { return $false }
        }
        return $true
    } catch { return $false }
}

# With -Offboard, before anything is run: the install must belong to the account
# running this, as offboarding does ("as the user", the owner's decision B8-5). It is
# the folder this account's uninstall entry names, or one in its own %LOCALAPPDATA%.
# Otherwise (an IT account naming a leaver's folder, say) it would start that person's
# runtime as this account, offboard this account's own data, and remove their program,
# leaving their data with nothing to offboard it. Local System, or an account Windows
# cannot name, stops with exit 2 (as the app's own refusal) and never starts the app.
# A guard rail against naming another account's folder, not a security boundary: it
# trusts this account's own %LOCALAPPDATA% value and uninstall entry, which it can set.
function Assert-OwnInstall([bool]$Registered) {
    $sid = $null
    try { $sid = [string](Get-AccountSid) } catch { $sid = $null }
    if ([string]::IsNullOrWhiteSpace($sid) -or $sid -ceq "S-1-5-18") {
        Stop-Uninstall 2 ("Offboarding runs only as the person whose data it is: this was run as " +
            "Local System, or Windows could not say which account it runs as. Nothing was run or " +
            "removed, and the program was kept. Run this in their own session (with Intune, with " +
            "the logged-on user's credentials).")
    }
    if ($Registered -or (Test-UnderLocalAppData $Options.InstallDir)) { return }
    Stop-Uninstall 1 ("With -Offboard the install must be this Windows user's own: the folder their " +
        "uninstall entry names, or one in their own %LOCALAPPDATA%. Nothing was run or removed. " +
        "Run this as the person whose install it is, in their own session.")
}

# The current version's folder, if it has the switch: as Get-LidRestoreVersion, and
# never a version folder that is a link (#297). An older version would take --offboard
# for an ordinary start and open its window. Returns @{ Why; Target }: Why is "ready",
# "old" (its version.json lacks the feature), "incomplete" (current.json, the version
# folder, its version.json, its package-manifest.json or any file that lists is
# missing, as a removal that stopped part-way leaves them) or "unusable" (a link, or
# files that cannot be read). Every listed file is looked for, not only the runtime and
# __main__.py: the removal walks app before runtime, and the module files before
# __main__.py, so a part-removed version can keep those two and still fail to run.
function Get-OffboardVersion {
    try {
        $currentFile = [IO.Path]::Combine($Options.InstallDir, "current.json")
        if (-not (Test-Path -LiteralPath $currentFile -PathType Leaf)) { return @{ Why = "incomplete" } }
        $target = Get-CurrentVersionFolder
        if (-not $target) { return @{ Why = "unusable" } }
        $folder = [IO.DirectoryInfo]::new($target)
        if (-not $folder.Exists) { return @{ Why = "incomplete" } }
        if (Test-Link $folder) { return @{ Why = "unusable" } }
        $file = [IO.Path]::Combine($target, "version.json")
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { return @{ Why = "incomplete" } }
        $info = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
        if (@($info.features) -notcontains "offboard") { return @{ Why = "old" } }
        $listing = [IO.Path]::Combine($target, "package-manifest.json")
        if (-not (Test-Path -LiteralPath $listing -PathType Leaf)) { return @{ Why = "incomplete" } }
        $manifest = Get-Content -LiteralPath $listing -Raw | ConvertFrom-Json
        if (-not ($manifest -is [Management.Automation.PSCustomObject])) { return @{ Why = "unusable" } }
        $names = @($manifest.PSObject.Properties.Name)
        foreach ($part in @("runtime/python.exe", "app/workspace/__main__.py")) {
            if ($names -notcontains $part) { return @{ Why = "unusable" } }
        }
        foreach ($name in $names) {
            # Relative names with forward slashes, as the build writes them; nothing
            # rooted, no drive, no . or .. part. (Two matches, not a pipeline: a package
            # lists thousands of files, and this stays near a second.)
            # (-not -cmatch: tests/test_hardening.py finds the version pattern as the
            # one -cnotmatch here.)
            if (-not ($name -cmatch '\A[^\\/:*?"<>|\x00-\x1f]+(/[^\\/:*?"<>|\x00-\x1f]+)*\z') -or
                $name -cmatch '(\A|/)\.{1,2}(/|\z)') {
                return @{ Why = "unusable" }
            }
            if (-not [IO.File]::Exists([IO.Path]::Combine($target, $name.Replace("/", "\")))) {
                return @{ Why = "incomplete" }
            }
        }
        return @{ Why = "ready"; Target = $target }
    } catch { return @{ Why = "unusable" } }
}

# For a rerun after a program removal that stopped part-way: is anything of
# offboarding's still in the data folder (the workspace, the models, a part-removed
# workspace, or a data folder that is a link or a file)? Returns @{ Left; Others }:
# Left is $true also when it cannot tell; Others counts the other entries (offboarding's
# others_left), never named or opened.
function Get-OffboardLeft {
    try {
        $local = $env:LOCALAPPDATA
        if ([string]::IsNullOrWhiteSpace($local) -or -not [IO.Path]::IsPathRooted($local) -or
            $local -match '^[\\/]{2}') { return @{ Left = $true } }
        $path = [IO.Path]::Combine($local, $AppName)
        $base = [IO.DirectoryInfo]::new($path)
        if (-not $base.Exists) { return @{ Left = [IO.File]::Exists($path); Others = 0 } }
        if (Test-Link $base) { return @{ Left = $true } }
        $others = 0
        foreach ($item in $base.GetFileSystemInfos()) {
            if ($item.Name -ieq "workspace" -or $item.Name -ieq "models" -or
                $item.Name -match '^workspace\.offboarding-[0-9a-f]{32}\z') { return @{ Left = $true } }
            $others++
        }
        return @{ Left = $false; Others = $others }
    } catch { return @{ Left = $true } }
}

# Runs the installed app with one switch and a hard time limit, as Invoke-LidRestore
# does. Returns @{ Code; Output }, Code -1 if it was stopped.
function Invoke-AppSwitch([string]$Target, [string]$Switch) {
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = [IO.Path]::Combine($Target, "runtime", "python.exe")
    $start.Arguments = "$LaunchArgs $Switch"
    $start.WorkingDirectory = [IO.Path]::Combine($Target, "app")
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($start)
    try {
        $out = $process.StandardOutput.ReadToEndAsync()
        [void]$process.StandardError.ReadToEndAsync()       # read so a full pipe never blocks it
        if (-not $process.WaitForExit($OffboardSeconds * 1000)) {
            try { $process.Kill() } catch { }
            [void]$process.WaitForExit(10000)        # so no file in the install stays open
            return @{ Code = -1; Output = "" }
        }
        $process.WaitForExit()                       # and its output read to the end
        return @{ Code = $process.ExitCode; Output = [string]$out.Result }
    } finally {
        $process.Dispose()
    }
}

# The results --offboard-report and --offboard give, by exit code (workspace/offboard.py
# and workspace/__main__.py's offboard). A result word must match its code.
$OffboardResults = @{
    0 = @("ok", "offboarded"); 1 = @("failed"); 2 = @("system_account", "account_unknown")
    3 = @("running"); 4 = @("others_left"); 5 = @("blocked"); 6 = @("refused")
}

function Test-Word($Value) { return ($Value -is [string]) -and ($Value -cmatch '^[a-z_]{1,32}\z') }
function Test-Count($Value) { return (($Value -is [int]) -or ($Value -is [long])) -and $Value -ge 0 }

# True when every field of the app's line has the shape the app gives it: fixed words,
# counts, yes or no, and the fixed residual sentences. Any other field, or a field of
# another kind, and the line is not trusted (P3: nothing else reaches the output).
function Test-OffboardShape($Result) {
    foreach ($p in $Result.PSObject.Properties) {
        $v = $p.Value
        $name = $p.Name
        if (@("result", "step", "workspace") -ccontains $name) { $ok = Test-Word $v }
        elseif (@("leftover", "models_mb") -ccontains $name) { $ok = Test-Count $v }
        elseif ($name -ceq "others") { $ok = ($null -eq $v) -or (Test-Count $v) }
        elseif (@("models_link", "token", "lid_record", "running") -ccontains $name) { $ok = $v -is [bool] }
        elseif (@("blockers", "removed") -ccontains $name) {
            $ok = $v -is [Management.Automation.PSCustomObject]
            if ($ok) {
                foreach ($q in $v.PSObject.Properties) {
                    if ($name -ceq "blockers") { $kind = Test-Count $q.Value } else { $kind = $q.Value -is [bool] }
                    if (-not ((Test-Word $q.Name) -and $kind)) { $ok = $false }
                }
            }
        }
        elseif ($name -ceq "residuals") {
            $ok = ($v -is [object[]]) -and $v.Count -le 10
            if ($ok) {
                foreach ($s in $v) {
                    if (-not (($s -is [string]) -and $s.Length -le 400 -and $s -cmatch '^[ -~]+\z')) { $ok = $false }
                }
            }
        }
        else { $ok = $false }
        if (-not $ok) { return $false }
    }
    return $true
}

# The app's result: its last line, one JSON object of the app's shape whose result word
# matches the exit code (for exit 0, the run's own: $Expected), or $null.
function Read-OffboardResult([string]$Output, [int]$Code, [string]$Expected) {
    $lines = @($Output -split "\r?\n" | Where-Object { $_.Trim() })
    if (-not $lines.Count) { return $null }
    $line = $lines[-1].Trim()
    if ($line.Length -gt 65536 -or -not $line.StartsWith("{")) { return $null }
    try { $result = $line | ConvertFrom-Json } catch { return $null }
    if (-not ($result -is [Management.Automation.PSCustomObject])) { return $null }
    if (-not (Test-Word $result.result) -or -not (Test-OffboardShape $result)) { return $null }
    if (-not $OffboardResults.ContainsKey($Code) -or $OffboardResults[$Code] -cnotcontains $result.result -or
        ($Code -eq 0 -and $result.result -cne $Expected)) {
        return $null
    }
    return @{ Line = $line; Result = $result }
}

# "2 other item(s)", or "Other items" when the app could not count them.
function Format-OtherCount($Result) {
    if ($Result -and (Test-Count $Result.others)) { return "$($Result.others) other item(s)" }
    return "Other items"
}

# Other entries left in the data folder are reported, and the uninstall carries on
# (the owner's decision of 23 Oct 2026): offboarding never removes them (C1).
function Write-OthersLeft([string]$Count) {
    Write-Warn ("$Count in $AppName's data folder were left: they are not offboarding's to " +
        "remove (for example a workspace set aside after damage). Check them with whoever " +
        "manages holds, and delete them by hand once nothing in them is needed.")
}

# "meetings: 2, holds: 1": the blocker kinds (fixed words) and their counts only.
function Format-BlockerCount($Result) {
    $parts = @()
    if ($Result -and $Result.blockers) {
        foreach ($p in $Result.blockers.PSObject.Properties) {
            if ($p.Name -cmatch '^[a-z_]{1,32}\z' -and "$($p.Value)" -match '^\d{1,9}\z') {
                $parts += "$($p.Name): $($p.Value)"
            }
        }
    }
    if ($parts.Count) { return $parts -join ", " }
    return "meetings, holds, cleanup or audio"
}

# Returns the result when the run was clean ($Expected, exit 0) or left only other
# entries (others_left, exit 4, the owner's decision of 23 Oct 2026); otherwise stops
# the uninstall. A line of another shape counts as no line: exit 0 or 4 without one stops.
function Assert-Offboarded($Run, [string]$Expected) {
    if ($Run.Code -eq -1) {
        Stop-Uninstall 1 ("Offboarding did not finish within $OffboardSeconds seconds and was stopped, " +
            "so the program was kept. Run this again: it finishes what was started.")
    }
    $read = Read-OffboardResult $Run.Output $Run.Code $Expected
    $result = $null
    if ($read) {
        [Console]::Out.WriteLine($read.Line)
        $result = $read.Result
    }
    if ($result -and ($Run.Code -eq 0 -or $Run.Code -eq 4)) {
        return $result
    }
    $kept = "so the program was kept"
    switch ($Run.Code) {
        5 { Stop-Uninstall 5 ("Offboarding is blocked: $(Format-BlockerCount $result) left. Finish them in " +
            "$AppName (confirm cleanup of finished meetings; holds are released by whoever manages " +
            "them), then run this again. Nothing was removed, $kept.") }
        3 { Stop-Uninstall 3 ("$AppName is open, in this or another session of this user, $kept. " +
            "Close it, then run this again.") }
        2 { Stop-Uninstall 2 ("Offboarding runs only as the person whose data it is: it was run as " +
            "Local System, or Windows could not say which account it runs as. Nothing was removed " +
            "and the program was kept. Run this in their own session (with Intune, with the " +
            "logged-on user's credentials).") }
        6 { Stop-Uninstall 6 ("Offboarding refused $AppName's data folder: it is a link, a file, or a " +
            "location $AppName itself refuses. Nothing was removed, $kept. IT removes that folder by hand.") }
        1 {
            $step = "unknown"
            if ($result -and "$($result.step)" -cmatch '^[a-z_]{1,32}\z') { $step = $result.step }
            # An error the app did not expect, or no readable step: its files may be
            # damaged, and running it again alone would only repeat this.
            $again = ""
            if (@("unexpected", "unknown") -ccontains $step) {
                $again = " If it stops at this step again, install the current version again, then run this again."
            }
            Stop-Uninstall 1 ("Offboarding stopped at the step '$step', $kept. Run this again: it " +
                "finishes what was started.$again")
        }
        default { Stop-Uninstall 1 "Offboarding gave a result this uninstaller does not know, $kept." }
    }
}

# Offboards, or stops the uninstall. Without -Silent, shows what will go and asks.
function Invoke-Offboard {
    $version = Get-OffboardVersion
    if ($version.Why -eq "incomplete") {
        # A rerun after a program removal that stopped part-way: offboarding finished
        # before that removal started, unless something of offboarding's is still there.
        $left = Get-OffboardLeft
        if ($left.Left) {
            Stop-Uninstall 1 ("This install's program files are incomplete (an earlier removal may have " +
                "stopped part-way), so offboarding cannot run, nothing was removed and the program " +
                "was kept. Install the current version again, then run this again.")
        }
        Write-Warn ("This install's program files were incomplete (an earlier removal may have stopped part-way), " +
            "so offboarding was not run again: no workspace or models are left. If a Hugging Face " +
            "token for $AppName is still saved, remove it in Windows Credential Manager.")
        if ($left.Others) { Write-OthersLeft "$($left.Others) other item(s)" }
        return
    }
    if ($version.Why -ne "ready") {
        $why = "it is older than the switch"
        if ($version.Why -eq "unusable") {
            $why = "its version folder is a link, or its files or their list cannot be read"
        }
        Stop-Uninstall 1 ("This version of $AppName cannot offboard ($why), so nothing was removed and " +
            "the program was kept. Install the current version, then run this again.")
    }
    $target = $version.Target
    $facts = Assert-Offboarded (Invoke-AppSwitch $target "--offboard-report") "ok"
    if (-not $Options.Silent) {
        Write-Host "This removes, for this Windows user:"
        if ("empty", "locked_empty" -contains $facts.workspace) {
            Write-Host "  - $AppName's workspace: settings, logs, the audit log and window data (no meetings, holds or cleanup are left)"
        }
        if ($facts.models_link -eq $true) { Write-Host "  - the link to the downloaded models (what it points to is kept)" }
        elseif ("$($facts.models_mb)" -match '^\d{1,9}\z' -and [int]$facts.models_mb -gt 0) {
            Write-Host "  - the downloaded models ($($facts.models_mb) MB)"
        }
        if ($facts.token -eq $true) { Write-Host "  - the saved Hugging Face token" }
        Write-Host "  - then the program, its Start menu shortcut, its pins and its uninstall entry."
        if ($facts.result -ceq "others_left") {
            Write-Host ("It keeps $((Format-OtherCount $facts).ToLower()) in $AppName's data folder: " +
                "they are not offboarding's to remove.")
        }
        Write-Host "Type REMOVE to continue, or anything else to stop:"
        if ([Console]::In.ReadLine() -cne "REMOVE") {
            Stop-Uninstall 1 "Nothing was removed, and the program was kept."
        }
    }
    $done = Assert-Offboarded (Invoke-AppSwitch $target "--offboard") "offboarded"
    Write-Step "What $AppName kept for this Windows user was removed."
    if ($done.result -ceq "others_left") { Write-OthersLeft (Format-OtherCount $done) }
}

# Taskbar and Start pins are copies of the Start menu shortcut in these folders, which
# install.ps1's Update-PinnedShortcut repoints at each update. Each is given as the
# folders on its path from the roaming profile folder down (that folder, Microsoft,
# Internet Explorer, Quick Launch, User Pinned, then TaskBar or StartMenu), so a link
# anywhere on it is seen. None when the roaming folder is unknown or not a full path.
function Get-PinFolder {
    $roaming = [string]$env:APPDATA
    if ([string]::IsNullOrWhiteSpace($roaming) -or -not [IO.Path]::IsPathRooted($roaming)) { return @() }
    $pinned = @($roaming)
    foreach ($name in "Microsoft", "Internet Explorer", "Quick Launch", "User Pinned") {
        $pinned += [IO.Path]::Combine($pinned[-1], $name)
    }
    return @("TaskBar", "StartMenu" | ForEach-Object {
        , @($pinned + [IO.Path]::Combine($pinned[-1], $_))
    })
}

# What a pin runs, read without resolving or changing the shortcut (the shell's own
# shortcut object, as install.ps1's Set-ShortcutAppId uses it).
function Get-ShortcutTarget([string]$Path) {
    if (-not ([System.Management.Automation.PSTypeName]'UnminuteUninstall.ShortcutTarget').Type) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
namespace UnminuteUninstall {
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder file, int size,
                     IntPtr data, uint flags);
    }
    [ComImport, Guid("0000010b-0000-0000-C000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPersistFile {
        void GetClassID(out Guid id);
        [PreserveSig] int IsDirty();
        void Load([MarshalAs(UnmanagedType.LPWStr)] string name, int mode);
    }
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")] class CShellLink { }
    public static class ShortcutTarget {
        public static string Read(string lnk) {
            // Load reads the file and closes it (STGM_READ): nothing is resolved or written.
            object link = new CShellLink();
            ((IPersistFile)link).Load(lnk, 0);
            StringBuilder file = new StringBuilder(32768);
            ((IShellLinkW)link).GetPath(file, file.Capacity, IntPtr.Zero, 0);
            return file.ToString();
        }
    }
}
'@
    }
    return [UnminuteUninstall.ShortcutTarget]::Read($Path)
}

# The start of every path this install's versions have: the folder as given, and as
# Windows names it (Update-PinnedShortcut writes pins with the latter). Read before
# the install folder is removed.
function Get-PinPrefix {
    $dirs = @($Options.InstallDir)
    try { $dirs += (Get-Item -LiteralPath $Options.InstallDir -Force -ErrorAction Stop).FullName } catch { }
    return @($dirs | ForEach-Object { [IO.Path]::Combine($_.TrimEnd("\"), "versions") + "\" })
}

# Removes pins that run this install's pythonw.exe (Update-PinnedShortcut's test); pins
# to anything else are kept. A pin, or any folder on a pin folder's path, that is a link
# or junction is neither read nor removed through (#297), and warns: a pin may be left.
# A pin that cannot be read or removed only warns.
function Remove-OwnPin([string[]]$Prefixes) {
    $left = 0
    foreach ($path in Get-PinFolder) {
        try {
            $dir = $null
            foreach ($folder in $path) {
                $dir = [IO.DirectoryInfo]::new($folder)
                if (-not $dir.Exists -or (Test-Link $dir)) { break }
            }
            if (-not $dir.Exists) { continue }
            if (Test-Link $dir) { $left++; continue }
            foreach ($file in $dir.GetFiles("*.lnk")) {
                if ($file.Extension -ine ".lnk") { continue }
                if (Test-Link $file) { $left++; continue }
                try {
                    $target = [string](Get-ShortcutTarget $file.FullName)
                    $ours = $target.EndsWith("\runtime\pythonw.exe", [StringComparison]::OrdinalIgnoreCase) -and
                        @($Prefixes | Where-Object { $target.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) }).Count
                    if (-not $ours) { continue }
                    if ($file.Attributes -band [IO.FileAttributes]::ReadOnly) {
                        $file.Attributes = $file.Attributes -bxor [IO.FileAttributes]::ReadOnly
                    }
                    Invoke-LockRetry { $file.Delete() }
                } catch { $left++ }
            }
        } catch { $left++ }
    }
    if ($left) {
        Write-Warn ("$left pin(s) or pin folder(s) could not be checked or removed (an error, or a link " +
            "or junction, which is never followed). Unpin $AppName by hand if it still shows.")
    }
}

# The number of entries in the logs folder that are not the installer's log files
# (install.log, install.log.1) or are links (#111 B6a).
function Get-LogStray([string]$Dir) {
    $logs = [IO.DirectoryInfo]::new([IO.Path]::Combine($Dir, "logs"))
    if (-not $logs.Exists -or (Test-Link $logs)) { return 0 }
    return @($logs.GetFileSystemInfos() | Where-Object {
        $_ -is [IO.DirectoryInfo] -or (Test-Link $_) -or $LogNames -notcontains $_.Name }).Count
}

# install.lock, as the installer holds it (#111 B6a): no sharing, deleted when closed.
# Held by an install or update now: stop with exit 3 before anything is removed.
function Enter-InstallLock {
    try {
        $script:InstallLock = [IO.FileStream]::new([IO.Path]::Combine($Options.InstallDir, $LockName),
            [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None, 1,
            [IO.FileOptions]::DeleteOnClose)
    } catch {
        if (-not (Test-TransientLock $_.Exception)) { throw }
        Stop-Uninstall 3 "An install or update of $AppName is running. Run this again when it has finished."
    }
}

function Exit-InstallLock {
    if ($script:InstallLock) {
        $script:InstallLock.Dispose()
        $script:InstallLock = $null
    }
}

# -- machine mode, shared (#111 B6b) ----------------------------------------------------
# install.ps1 and uninstall.ps1 have this whole section, unchanged
# (tests/test_hardening.py keeps the two equal), and packaging/identity.json mirrors
# its names. Every name stays a literal here: no file is read to decide what is
# installed, checked or removed (the owner's decision B6-10).

# %ProgramFiles%\Unminute, from Windows' known folder in a 64-bit process, never from
# an environment variable; its machine-wide uninstall entry, in the 64-bit
# registry view.
$MachineFolderName = "Unminute"
$MachineRegKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Unminute"
# The Event Log source, registered by the install with .NET's EventLogMessages.dll (a
# Windows file: no new binary) and removed by the uninstall (the owner's decision of
# 21 October 2026). workspace/audit.py writes as this source.
$EventSourceKey = "HKLM:\SYSTEM\CurrentControlSet\Services\EventLog\Application\Unminute"
# The stable launch path (a junction to the current version), and the two names a
# switch of version uses for a moment.
$JunctionName = "current"
$JunctionNames = @("current", "current.new", "current.old")
$StagingPattern = '^staging-[0-9a-f]{32}\z'
# The install folder's permissions: owner Administrators (or SYSTEM); full control
# for SYSTEM and Administrators; read and run for Users and ALL APPLICATION
# PACKAGES; nothing inherited from Program Files; everything beneath inherits them.
$AdministratorsSid = "S-1-5-32-544"
$SystemSid = "S-1-5-18"
$UsersSid = "S-1-5-32-545"
$AppPackagesSid = "S-1-15-2-1"
$PermissionMessage = "The install folder's permissions have changed, so nothing was changed. Ask IT to check this computer."

# What the run and the computer are. The Pester tests stand these in.
function Test-Elevated {
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Test-64BitProcess { return [Environment]::Is64BitProcess }
function Get-ProgramFilesFolder { return [Environment]::GetFolderPath("ProgramFiles") }
function Get-CommonProgramsFolder { return [Environment]::GetFolderPath("CommonPrograms") }
function Get-MachineRegKey { return $MachineRegKey }
function Get-EventSourceKey { return $EventSourceKey }
function Get-FolderSecurity([string]$Path) { return [IO.DirectoryInfo]::new($Path).GetAccessControl() }

# The machine install folder: Unminute in Program Files, which must be a folder on a
# drive letter and not a link; $null otherwise.
function Get-MachineRoot {
    $programFiles = [string](Get-ProgramFilesFolder)
    if ($programFiles -notmatch '^[A-Za-z]:\\') { return $null }
    $parent = [IO.DirectoryInfo]::new($programFiles)
    if (-not $parent.Exists -or (Test-Link $parent)) { return $null }
    return [IO.Path]::Combine($parent.FullName.TrimEnd("\"), $MachineFolderName)
}

# The four entries every machine install folder has, each for the folder, its
# subfolders and files.
function Get-MachineRule {
    $full = [Security.AccessControl.FileSystemRights]::FullControl
    $read = [Security.AccessControl.FileSystemRights]::ReadAndExecute
    return @(@($SystemSid, $full), @($AdministratorsSid, $full), @($UsersSid, $read), @($AppPackagesSid, $read))
}

# Those permissions as a list to create the folder with, owned by $OwnerSid
# (Administrators; the tests pass their own account, which they may set).
function Get-MachineRootSecurity([string]$OwnerSid = $AdministratorsSid) {
    $security = New-Object Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    $security.SetOwner([Security.Principal.SecurityIdentifier]::new($OwnerSid))
    $inherit = [Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit"
    foreach ($rule in Get-MachineRule) {
        $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($rule[0]), $rule[1], $inherit,
            [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow))
    }
    return $security
}

# True only when $Security is exactly those permissions: owned by one of $Owners,
# protected (nothing inherited), and the four entries with nothing else.
function Test-MachineRootSecurity([Security.AccessControl.DirectorySecurity]$Security,
                                  [string[]]$Owners = @($AdministratorsSid, $SystemSid)) {
    $owner = $Security.GetOwner([Security.Principal.SecurityIdentifier])
    if (-not $owner -or $Owners -notcontains $owner.Value -or -not $Security.AreAccessRulesProtected) { return $false }
    $sync = [Security.AccessControl.FileSystemRights]::Synchronize
    $inherit = [Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit"
    $found = @(foreach ($rule in $Security.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.IsInherited -or $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $rule.InheritanceFlags -ne $inherit -or $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None) {
            "other"
        } else {
            "$($rule.IdentityReference.Value)=$([int]($rule.FileSystemRights -bor $sync))"
        }
    })
    $wanted = @(foreach ($rule in Get-MachineRule) { "$($rule[0])=$([int]($rule[1] -bor $sync))" })
    return (($found | Sort-Object) -join ",") -ceq (($wanted | Sort-Object) -join ",")
}

# True when $Item has permissions of its own: an explicit entry, or a protected list
# that inherits nothing from the install folder.
function Test-AclChanged([IO.FileSystemInfo]$Item) {
    $security = $Item.GetAccessControl()
    return $security.AreAccessRulesProtected -or
        @($security.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier])).Count -gt 0
}

# How many folders beneath $Root have permissions of their own (never walking
# through a link): every one should only inherit the install folder's.
function Get-AclChangeCount([string]$Root) {
    $count = 0
    $todo = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
    $todo.Push([IO.DirectoryInfo]::new($Root))
    while ($todo.Count) {
        foreach ($dir in $todo.Pop().GetDirectories()) {
            if (Test-Link $dir) { continue }
            if (Test-AclChanged $dir) { $count++ }
            $todo.Push($dir)
        }
    }
    return $count
}

# The folder a junction points at, or $null for anything that is not a junction.
function Get-LinkTarget([string]$Path) {
    try {
        $item = Get-Item -LiteralPath $Path -Force
        if ($item.LinkType -ne "Junction") { return $null }
        return [string]@($item.Target)[0]
    } catch { return $null }
}

# One of machine mode's junctions: a junction (not a symbolic link) to a folder
# directly inside <Root>\versions with a plain version name.
function Test-OwnJunction([IO.FileSystemInfo]$Item, [string]$Root) {
    if ($Item -isnot [IO.DirectoryInfo] -or -not (Test-Link $Item)) { return $false }
    $target = Get-LinkTarget $Item.FullName
    if (-not $target) { return $false }
    $target = $target.TrimEnd("\")
    $name = [IO.Path]::GetFileName($target)
    $versions = [IO.Path]::Combine($Root.TrimEnd("\"), "versions")
    return [string]::Equals([IO.Path]::GetDirectoryName($target), $versions, [StringComparison]::OrdinalIgnoreCase) -and
        ($name -cmatch '^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\z') -and -not $name.EndsWith(".") -and
        $name -notmatch '\.(partial|old|retired)\z'
}

# Removes a junction as a link, with [IO.Directory]::Delete($Path, $false): what it
# points to is kept. Never Remove-Item -Recurse. Anything at that name that is not
# a link stops the run, untouched.
function Remove-Junction([string]$Path) {
    $item = [IO.DirectoryInfo]::new($Path)
    if (-not $item.Exists -and -not [IO.File]::Exists($Path)) { return }
    if (-not (Test-Link $item)) { throw "A launch link is not a link, so it was left in place." }
    [IO.Directory]::Delete($Path, $false)
}

# The Event Log's own message file in .NET Framework, which shows each entry's text.
function Get-EventMessageFile {
    $path = [IO.Path]::Combine([Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory(), "EventLogMessages.dll")
    if ([IO.File]::Exists($path)) { return $path }
    return $null
}

# -- end of the shared machine section ----------------------------------------------------

# -- machine mode: the uninstall (#111 B6b) ---------------------------------------------

# How many versions are installed beside the current one with an earlier storage
# format: the version kept for people whose workspace has not moved to the current
# format yet. Read from each version's own version.json; none when the current
# version or its format cannot be told.
function Get-KeptOlderSlotCount([string]$Root) {
    $currentFile = [IO.Path]::Combine($Root, "current.json")
    if (-not [IO.File]::Exists($currentFile)) { return 0 }
    try { $record = Get-Content -LiteralPath $currentFile -Raw | ConvertFrom-Json } catch { return 0 }
    $current = [string]$record.version
    $versions = [IO.DirectoryInfo]::new([IO.Path]::Combine($Root, "versions"))
    if (-not $versions.Exists) { return 0 }
    $schemas = @{}
    foreach ($dir in $versions.GetDirectories()) {
        if ((Test-Link $dir) -or -not ($dir.Name -cmatch '^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\z') -or
            $dir.Name.EndsWith(".") -or $dir.Name -match '\.(partial|old|retired)\z') { continue }
        try { $info = Get-Content -LiteralPath ([IO.Path]::Combine($dir.FullName, "version.json")) -Raw | ConvertFrom-Json }
        catch { continue }
        $schema = $info.schema
        if (([string]$info.version) -ceq $dir.Name -and ($schema -is [int] -or $schema -is [long]) -and $schema -gt 0) {
            $schemas[$dir.Name] = [long]$schema
        }
    }
    if (-not $schemas.ContainsKey($current)) { return 0 }
    return @($schemas.Keys | Where-Object { $_ -cne $current -and $schemas[$_] -lt $schemas[$current] }).Count
}

# Removes the Event Log source the install registered (the owner's decision of 21
# October 2026). A failure only warns.
function Unregister-EventSource {
    try {
        $key = Get-EventSourceKey
        if (Test-Path -LiteralPath $key) { Remove-Item -LiteralPath $key -Recurse -Force }
    } catch { Write-Warn "The Event Log source could not be removed." }
}

function Invoke-MachineUninstall {
    if (-not (Test-64BitProcess)) {
        Stop-Uninstall 2 ("A machine uninstall needs 64-bit Windows PowerShell, so nothing was removed. Start it with " +
            "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe (from a 32-bit host) or " +
            "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe.")
    }
    if (-not (Test-Elevated)) {
        Stop-Uninstall 2 "A machine uninstall needs administrator rights (Intune's System context), so nothing was removed."
    }
    if ($Options.InstallDirGiven) {
        Stop-Uninstall 2 "A machine install is always in Program Files, so -InstallDir cannot be used with -Machine."
    }
    # Offboarding is each person's own, run as them (#111 B8b); a machine uninstall
    # never reads or writes a user's profile (B6-9), so it refuses -Offboard.
    if ($Options.Offboard) {
        Stop-Uninstall 2 ("Offboarding runs as each person, so -Offboard cannot be used with -Machine, and " +
            "nothing was removed.")
    }
    $root = Get-MachineRoot
    if (-not $root) { Stop-Uninstall 1 "The Program Files folder could not be found, so nothing was removed." }
    $Options.InstallDir = $root
    $regKey = Get-MachineRegKey
    $link = Join-Path (Get-CommonProgramsFolder) "$AppName.lnk"
    $folder = [IO.DirectoryInfo]::new($root)
    if ([IO.File]::Exists($root) -or ($folder.Exists -and (Test-Link $folder))) { Stop-Uninstall 4 $PermissionMessage }
    if (-not $folder.Exists -and -not (Test-Path -LiteralPath $regKey)) {
        Stop-Uninstall 1 "$AppName is not installed for everyone on this computer, so nothing was removed."
    }
    if ($folder.Exists) {
        # The installer's checks: the folder's own permissions, exactly, and nothing
        # of their own on any folder beneath it.
        if (-not (Test-MachineRootSecurity (Get-FolderSecurity $root)) -or (Get-AclChangeCount $root) -gt 0) {
            Stop-Uninstall 4 $PermissionMessage
        }
        $running = Get-Process -ErrorAction SilentlyContinue | Where-Object {
            try { $_.Path -and $_.Path.StartsWith($root.TrimEnd("\") + "\", [StringComparison]::OrdinalIgnoreCase) } catch { $false }
        }
        if ($running) {
            Stop-Uninstall 3 "$AppName is running on this computer, so nothing was removed. Run this again once nobody has it open."
        }
        # Only the installer's own entries; anything else stops it before anything is
        # removed. A link is allowed only as one of the junctions, pointing at a version.
        $strays = 0; $links = 0
        foreach ($item in $folder.GetFileSystemInfos()) {
            $isDir = $item -is [IO.DirectoryInfo]
            if ($JunctionNames -ccontains $item.Name) {
                if (-not (Test-OwnJunction $item $root)) { $links++ }
            } elseif ($item.Name -cmatch $StagingPattern) {
                if (Test-Link $item) { $links++ } elseif (-not $isDir) { $strays++ }
            } elseif ($OwnEntries -cnotcontains $item.Name) {
                $strays++
            } elseif (Test-Link $item) {
                $links++
            } elseif ($isDir -ne (@("versions", "logs") -contains $item.Name)) {
                $strays++
            }
        }
        $strays += Get-LogStray $root
        if ($links) {
            Stop-Uninstall 1 "The install folder holds a link or junction that $AppName did not put there, so nothing was removed."
        }
        if ($strays) {
            Stop-Uninstall 1 "The install folder holds $strays item(s) that $AppName did not put there, so nothing was removed."
        }
        # Not beside an install or update (#111 B6a).
        Enter-InstallLock
        if (-not $Options.IncludeKeptSlots -and (Get-KeptOlderSlotCount $root) -gt 0) {
            Stop-Uninstall 5 ("Older versions are still kept for people whose workspace has not moved to the " +
                "current storage format, so nothing was removed. Check with them first, then remove the kept " +
                "version with install.ps1 -Machine -RemoveSlot, or run this again with -IncludeKeptSlots.")
        }
        # The junctions first, as links, so nothing is removed through them; then
        # the rest, never through a link (Remove-OwnFolder).
        # The launch junction first, so nothing starts the app meanwhile, then the two
        # names a switch of version uses for a moment.
        Remove-Junction ([IO.Path]::Combine($root, $JunctionName))
        foreach ($name in $JunctionNames) { Remove-Junction ([IO.Path]::Combine($root, $name)) }
        foreach ($dir in $folder.GetDirectories()) {
            if ($dir.Name -cmatch $StagingPattern) { Invoke-LockRetry { Remove-OwnFolder $dir.FullName } }
        }
        foreach ($entry in @("versions", "current.json", "current.json.tmp")) {
            $path = [IO.Path]::Combine($root, $entry)
            if ([IO.Directory]::Exists($path)) { Invoke-LockRetry { Remove-OwnFolder $path } }
            elseif ([IO.File]::Exists($path)) { Invoke-LockRetry { Remove-Item -LiteralPath $path -Force } }
        }
        $logs = [IO.Path]::Combine($root, "logs")
        foreach ($name in $LogNames) {
            $path = [IO.Path]::Combine($logs, $name)
            if ([IO.File]::Exists($path)) { Remove-Item -LiteralPath $path -Force }
        }
        if ([IO.Directory]::Exists($logs)) { [IO.Directory]::Delete($logs, $false) }
        Exit-InstallLock                 # closing it deletes install.lock
        try { Remove-OwnFolder $root } catch { }
    }
    if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link -Force }
    Unregister-EventSource
    # Last: while any of it remains, the app still shows as installed (Intune detection).
    if (Test-Path -LiteralPath $regKey) { Remove-Item -LiteralPath $regKey -Recurse -Force }
    Write-Step ("$AppName has been removed for everyone on this computer. Each person's workspace and " +
        "downloaded models are kept in their own profile.")
}

# Dot-sourced (the Pester tests in tests/powershell): the functions above are defined
# and nothing runs.
if ($MyInvocation.InvocationName -eq ".") { return }

# Machine mode (#111 B6b): Program Files, its own checks and entries; never the
# lid restore below, which would read a user's profile as SYSTEM (B6-9).
if ($Options.Machine) {
    try {
        Invoke-MachineUninstall
        exit 0
    } catch {
        Stop-Uninstall 1 "Removal failed: $($_.Exception.Message)"
    } finally {
        Exit-InstallLock
    }
}

try {
    # An empty -InstallDir is refused, and a relative one is taken from PowerShell's
    # current folder, once, so the checks and the removals below use the same path.
    if ([string]::IsNullOrWhiteSpace($Options.InstallDir)) {
        Stop-Uninstall 1 "No install folder was given, so nothing was removed."
    }
    $Options.InstallDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Options.InstallDir)
    # A network, device or long-path form (\\, //, \\?\) is refused, as the installer
    # refuses it: the overlap check below compares paths as text.
    if ($Options.InstallDir -match '^[\\/]{2}') {
        Stop-Uninstall 1 "The install folder must be on this computer's own drive, not a network or device path (starting with \\ or //), so nothing was removed."
    }
    $overlap = Get-DataOverlap $Options.InstallDir
    if ($overlap) {
        Stop-Uninstall 1 "The install folder is in, or holds, $AppName's data folder ($overlap), so nothing was removed."
    }
    # The folder must be an install (current.json or versions), or the one the
    # uninstall entry names (its files already gone). A wrong -InstallDir would
    # otherwise remove the real install's shortcut and uninstall entry, and report
    # success, while leaving the program itself in place.
    $isInstall = Test-Install $Options.InstallDir
    $registered = $false
    $location = $null
    if (Test-Path -LiteralPath $RegKey) {
        $location = (Get-ItemProperty -LiteralPath $RegKey -ErrorAction SilentlyContinue).InstallLocation
        $registered = $location -and
            ($location.TrimEnd("\") -ieq $Options.InstallDir.TrimEnd("\"))
    }
    if (-not ($isInstall -or $registered)) {
        Stop-Uninstall 1 "No $AppName install was found in the install folder, so nothing was removed. Check -InstallDir."
    }
    $running = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($Options.InstallDir.TrimEnd("\") + "\", [StringComparison]::OrdinalIgnoreCase) } catch { $false }
    }
    if ($running) {
        Write-Step "$AppName is running. Close it, then run this again."
        exit 3
    }
    # Only the installer's own entries are removed. Anything else in the folder (a
    # wrong -InstallDir, or one that reaches other data through a junction or a
    # mapped drive) stops the uninstall before anything is removed.
    if (Test-Path -LiteralPath $Options.InstallDir) {
        $items = @(Get-ChildItem -LiteralPath $Options.InstallDir -Force)
        $other = @($items | Where-Object { $OwnEntries -notcontains $_.Name })
        # versions and logs are folders; the installer's other entries are files, so a
        # folder named current.json, for example, is not the installer's. (A link at
        # any of them is refused below, as a link.)
        $kind = @($items | Where-Object { $OwnEntries -contains $_.Name -and -not (Test-Link $_) -and
            ((@("versions", "logs") -contains $_.Name) -ne ($_ -is [IO.DirectoryInfo])) })
        $strays = $other.Count + $kind.Count + (Get-LogStray $Options.InstallDir)
        if ($strays) {
            Stop-Uninstall 1 "The install folder holds $strays item(s) that $AppName did not put there, so nothing was removed. Check -InstallDir."
        }
        # Nothing is removed through a link or junction, or run from one (#297).
        if (@($items | Where-Object { Test-Link $_ }).Count) {
            Stop-Uninstall 1 "The install folder holds a link or junction that $AppName did not put there, so nothing was removed. Remove it, then run this again."
        }
        # Not beside an install or update (#111 B6a).
        Enter-InstallLock
    }
    # With -Offboard (#111 B8b): the person's data goes first, through the installed app.
    # Anything but a clean result stops here, with the program kept (B8-2). First, the
    # install must be this account's own, and this never Local System: nothing is run
    # otherwise.
    if ($Options.Offboard) {
        Assert-OwnInstall $registered
        Invoke-Offboard
        $pinPrefixes = Get-PinPrefix
    }
    # Before anything is removed: the installed app puts the lid setting back (#191).
    # After offboarding there is no record left (it was put back first), so this does nothing.
    Restore-LidSetting
    if (Test-Path -LiteralPath $Options.InstallDir) {
        # The script itself lives in InstallDir: remove the rest first, never through
        # a link (#297): one inside a version is removed as a link.
        foreach ($name in "versions", "current.json", "current.json.tmp") {
            $path = [IO.Path]::Combine($Options.InstallDir, $name)
            if ([IO.Directory]::Exists($path)) { Invoke-LockRetry { Remove-OwnFolder $path } }
            elseif ([IO.File]::Exists($path)) { Invoke-LockRetry { Remove-Item -LiteralPath $path -Force } }
        }
        # The installer's log files, then their folder once empty (#111 B6a).
        $logs = [IO.Path]::Combine($Options.InstallDir, "logs")
        foreach ($name in $LogNames) {
            $path = [IO.Path]::Combine($logs, $name)
            if ([IO.File]::Exists($path)) { Remove-Item -LiteralPath $path -Force }
        }
        if ([IO.Directory]::Exists($logs)) { [IO.Directory]::Delete($logs, $false) }
        Exit-InstallLock                 # closing it deletes install.lock
        try { Remove-OwnFolder $Options.InstallDir } catch { }
    }
    # The shortcut and the entry stay only when the entry names another folder that
    # still holds an install: this one was an older or second install. Checked after
    # the removal, by what is on disk, so the registered folder named another way
    # (an 8.3 short name, a junction, a subst drive) is not taken for another
    # install (R13b round 3 review).
    $elsewhere = $location -and (-not $registered) -and (Test-Install $location)
    $link = Join-Path $ProgramsFolder "$AppName.lnk"
    if ((-not $elsewhere) -and (Test-Path -LiteralPath $link)) { Remove-Item -LiteralPath $link -Force }
    # Pins that run this install's versions would only break: with -Offboard they go too.
    if ($Options.Offboard) { Remove-OwnPin $pinPrefixes }
    # Last: while any file remains, the app still shows as installed (Intune detection).
    if ($elsewhere) {
        Write-Step ("$AppName has been removed from this folder. The Start menu shortcut and the " +
            "uninstall entry belong to the install in another folder, so they were kept.")
    } else {
        if (Test-Path -LiteralPath $RegKey) { Remove-Item -LiteralPath $RegKey -Recurse -Force }
        Write-Step "$AppName has been removed."
    }
    if ($Options.Offboard) { exit 0 }               # the data went first
    $db = Join-Path $env:LOCALAPPDATA "Unminute\workspace\workspace.db"
    if (Test-Path -LiteralPath $db) {
        Write-Step ("Your workspace was kept ($([IO.Path]::GetDirectoryName($db))). It may still " +
            "contain meetings or retention holds. To remove meeting content, reinstall and use " +
            "the app's cleanup steps; nothing is deleted automatically.")
    }
    Write-Step "Downloaded models were kept in $(Join-Path $env:LOCALAPPDATA 'Unminute\models')."
    exit 0
} catch {
    Stop-Uninstall 1 "Removal failed: $($_.Exception.Message)"
} finally {
    Exit-InstallLock
}
