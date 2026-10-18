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

function Write-Step([string]$Message) {
    if (-not $Options.Silent) { Write-Host $Message }
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
    if (Test-Path -LiteralPath $Options.InstallDir) {
        # The script itself lives in InstallDir: remove the rest first.
        foreach ($name in "versions", "current.json") {
            $path = Join-Path $Options.InstallDir $name
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
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
