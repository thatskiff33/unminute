<#
.SYNOPSIS
  Removes Unminute for the current Windows user (#11).

.DESCRIPTION
  Removes the installed program, its Start menu shortcut and its uninstall
  entry. It removes only what the installer put in the install folder
  (versions, current.json and this script); if the folder holds anything
  else, or is neither an install nor the folder the uninstall entry names, it
  removes nothing. When the uninstall entry names another folder that still
  holds an install, the shortcut and the entry are that install's, and are
  kept. It does NOT remove the workspace (meetings, holds, settings) or the
  downloaded models: meeting content is only removed through the app's own
  confirmed cleanup, which honours retention holds. If meetings remain, this
  says so.

  Before removing anything, it puts back the Windows lid setting if the app
  left it at "Do nothing" (#191): see Restore-LidSetting. That step never stops
  the uninstall.

  Exit codes: 0 removed, 1 failed, 3 the app is running.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Unminute"),
    [switch]$Silent
)

$ErrorActionPreference = "Stop"
# Parameters, read by the functions below.
$Options = @{ InstallDir = $InstallDir; Silent = [bool]$Silent }
$AppName = "Unminute"
$RegKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Unminute"
# The longest the lid restore may take before it is stopped (it needs about a second).
$LidRestoreSeconds = 60

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

# The current version's folder, if it can run the restore: a plain folder name from
# current.json (never a path), and a version.json that lists the feature, so an older
# version never takes the switch for an ordinary start and opens its window.
function Get-LidRestoreVersion {
    $currentFile = [IO.Path]::Combine($Options.InstallDir, "current.json")
    if (-not (Test-Path -LiteralPath $currentFile -PathType Leaf)) { return $null }
    $version = [string](Get-Content -LiteralPath $currentFile -Raw | ConvertFrom-Json).version
    if ($version -cnotmatch '^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$') { return $null }
    $target = [IO.Path]::Combine($Options.InstallDir, "versions", $version)
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
    $start.Arguments = "-I -B -X disable-remote-debug -m workspace --restore-lid"
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
    $own = @("versions", "current.json", "uninstall.ps1")
    if (Test-Path -LiteralPath $Options.InstallDir) {
        $other = @(Get-ChildItem -LiteralPath $Options.InstallDir -Force | Where-Object { $own -notcontains $_.Name })
        if ($other.Count) {
            Stop-Uninstall 1 "The install folder holds $($other.Count) item(s) that $AppName did not put there, so nothing was removed. Check -InstallDir."
        }
    }
    # Before anything is removed: the installed app puts the lid setting back (#191).
    Restore-LidSetting
    if (Test-Path -LiteralPath $Options.InstallDir) {
        # The script itself lives in InstallDir: remove the rest first.
        foreach ($name in "versions", "current.json") {
            $path = Join-Path $Options.InstallDir $name
            if (Test-Path -LiteralPath $path) { Invoke-LockRetry { Remove-Item -LiteralPath $path -Recurse -Force } }
        }
        Remove-Item -LiteralPath $Options.InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    # The shortcut and the entry stay only when the entry names another folder that
    # still holds an install: this one was an older or second install. Checked after
    # the removal, by what is on disk, so the registered folder named another way
    # (an 8.3 short name, a junction, a subst drive) is not taken for another
    # install (R13b round 3 review).
    $elsewhere = $location -and (-not $registered) -and (Test-Install $location)
    $programs = [Environment]::GetFolderPath("Programs")
    $link = Join-Path $programs "$AppName.lnk"
    if ((-not $elsewhere) -and (Test-Path -LiteralPath $link)) { Remove-Item -LiteralPath $link -Force }
    # Last: while any file remains, the app still shows as installed (Intune detection).
    if ($elsewhere) {
        Write-Step ("$AppName has been removed from this folder. The Start menu shortcut and the " +
            "uninstall entry belong to the install in another folder, so they were kept.")
    } else {
        if (Test-Path -LiteralPath $RegKey) { Remove-Item -LiteralPath $RegKey -Recurse -Force }
        Write-Step "$AppName has been removed."
    }
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
}
