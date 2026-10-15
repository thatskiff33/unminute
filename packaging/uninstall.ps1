<#
.SYNOPSIS
  Removes Unminute for the current Windows user (#11).

.DESCRIPTION
  Removes the installed program, its Start menu shortcut and its uninstall
  entry. It does NOT remove the workspace (meetings, holds, settings) or the
  downloaded models: meeting content is only removed through the app's own
  confirmed cleanup, which honours retention holds. If meetings remain, this
  says so.

  Exit codes: 0 removed, 1 failed, 3 the app is running.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\WhisperWorkspace"),
    [switch]$Silent
)

$ErrorActionPreference = "Stop"
# Parameters, read by the functions below.
$Options = @{ InstallDir = $InstallDir; Silent = [bool]$Silent }
$AppName = "Unminute"
# Start menu names used by earlier versions (see install.ps1).
$LegacyShortcutNames = @("Meeting Workspace")
$RegKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\WhisperWorkspace"

function Write-Step([string]$Message) {
    if (-not $Options.Silent) { Write-Host $Message }
}

try {
    $running = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($Options.InstallDir.TrimEnd("\") + "\", [StringComparison]::OrdinalIgnoreCase) } catch { $false }
    }
    if ($running) {
        Write-Step "$AppName is running. Close it, then run this again."
        exit 3
    }
    $programs = [Environment]::GetFolderPath("Programs")
    $link = Join-Path $programs "$AppName.lnk"
    if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link -Force }
    # An old-name shortcut is removed only if it points into this install.
    # Long form: a .lnk stores its target as a long path, so a short (8.3) InstallDir
    # would otherwise never match.
    $dir = $Options.InstallDir
    try { $dir = (Get-Item -LiteralPath $dir -ErrorAction Stop).FullName } catch { }
    $prefix = $dir.TrimEnd("\") + "\"
    $shell = New-Object -ComObject WScript.Shell
    foreach ($name in $LegacyShortcutNames) {
        $old = Join-Path $programs "$name.lnk"
        if (-not (Test-Path -LiteralPath $old)) { continue }
        # Release the shortcut object first: while alive it keeps the .lnk open.
        $sc = $shell.CreateShortcut($old)
        $target = $sc.TargetPath
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($sc)
        if ($target -and $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $old -Force
        }
    }
    if (Test-Path -LiteralPath $Options.InstallDir) {
        # The script itself lives in InstallDir: remove everything else first.
        Get-ChildItem -LiteralPath $Options.InstallDir | Where-Object { $_.Name -ne "uninstall.ps1" } |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
        Remove-Item -LiteralPath $Options.InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    # Last: while any file remains, the app still shows as installed (Intune detection).
    if (Test-Path -LiteralPath $RegKey) { Remove-Item -LiteralPath $RegKey -Recurse -Force }
    Write-Step "$AppName has been removed."
    $db = Join-Path $env:LOCALAPPDATA "whisper\workspace\workspace.db"
    if (Test-Path -LiteralPath $db) {
        Write-Step ("Your workspace was kept ($([IO.Path]::GetDirectoryName($db))). It may still " +
            "contain meetings or retention holds. To remove meeting content, reinstall and use " +
            "the app's cleanup steps; nothing is deleted automatically.")
    }
    Write-Step "Downloaded models were kept in $(Join-Path $env:LOCALAPPDATA 'whisper-models') (shared with the earlier app)."
    exit 0
} catch {
    Write-Step "Removal failed: $($_.Exception.Message)"
    exit 1
}
