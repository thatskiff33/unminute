<#
.SYNOPSIS
  Removes the Meeting Workspace for the current Windows user (#11).

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
$AppName = "Meeting Workspace"
$RegKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\WhisperWorkspace"

function Write-Step([string]$Message) {
    if (-not $Options.Silent) { Write-Host $Message }
}

try {
    $running = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($Options.InstallDir, [StringComparison]::OrdinalIgnoreCase) } catch { $false }
    }
    if ($running) {
        Write-Step "$AppName is running. Close it, then run this again."
        exit 3
    }
    $link = Join-Path ([Environment]::GetFolderPath("Programs")) "$AppName.lnk"
    if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link -Force }
    if (Test-Path -LiteralPath $RegKey) { Remove-Item -LiteralPath $RegKey -Recurse -Force }
    if (Test-Path -LiteralPath $Options.InstallDir) {
        # The script itself lives in InstallDir: remove everything else first.
        Get-ChildItem -LiteralPath $Options.InstallDir | Where-Object { $_.Name -ne "uninstall.ps1" } |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
        Remove-Item -LiteralPath $Options.InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    }
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
