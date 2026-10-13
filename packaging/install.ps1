<#
.SYNOPSIS
  Installs the Meeting Workspace for the current Windows user (#11).

.DESCRIPTION
  Run from the extracted package folder (or pass -Package with the zip and its
  .sha256 file). No administrator rights, Python, Git or GPU tools are needed.

  - Checks prerequisites: 64-bit Windows 10/11, the WebView2 runtime, free
    disk space and memory.
  - Verifies the package: the zip against its .sha256 file, and every file
    against package-manifest.json, before anything is installed.
  - Installs side by side under <InstallDir>\versions\<version> and keeps the
    previous version for rollback; workspace data and models are separate and
    never touched.
  - Registers a per-user uninstall entry (used for Intune detection) and a
    Start menu shortcut.

  Exit codes: 0 installed, 1 failed, 2 prerequisites missing,
  3 the app is running, 4 the package failed verification.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Silent
#>
[CmdletBinding()]
param(
    [string]$Package = "",
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\WhisperWorkspace"),
    [switch]$Silent,
    [switch]$SkipShortcut
)

$ErrorActionPreference = "Stop"
# Parameters, read by the functions below.
$Options = @{ Package = $Package; InstallDir = $InstallDir; Silent = [bool]$Silent; SkipShortcut = [bool]$SkipShortcut }
$AppName = "Meeting Workspace"
$script:TempExtract = $null
$RegKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\WhisperWorkspace"
$WebView2Client = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

function Write-Step([string]$Message) {
    if (-not $Options.Silent) { Write-Host $Message }
}

function Stop-Install([int]$Code, [string]$Message) {
    if ($Options.Silent) { [Console]::Error.WriteLine($Message) } else { Write-Host $Message -ForegroundColor Red }
    exit $Code
}

function Get-FileSha256([string]$Path) {
    (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

# -- prerequisites -----------------------------------------------------------------

function Test-Prerequisite {
    # Package paths are at most 140 characters below versions\<version>\.
    if ($Options.InstallDir.Length -gt 100) {
        Stop-Install 2 "The install folder path is too long for Windows (over 100 characters). Choose a shorter -InstallDir."
    }
    if (-not [Environment]::Is64BitOperatingSystem) {
        Stop-Install 2 "$AppName needs 64-bit Windows 10 or 11."
    }
    $build = [Environment]::OSVersion.Version.Build
    if ($build -lt 17763) {
        Stop-Install 2 "$AppName needs Windows 10 version 1809 or later (this is build $build)."
    }
    $webview = $null
    foreach ($key in @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$WebView2Client",
                       "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$WebView2Client",
                       "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$WebView2Client")) {
        try { $webview = (Get-ItemProperty -LiteralPath $key -Name pv).pv } catch { }
        if ($webview -and $webview -ne "0.0.0.0") { break }
    }
    if (-not $webview -or $webview -eq "0.0.0.0") {
        Stop-Install 2 ("The Microsoft Edge WebView2 Runtime is not installed. It is part of " +
            "Windows 11 and most managed Windows 10 devices; ask IT, or install the Evergreen " +
            "runtime from https://developer.microsoft.com/microsoft-edge/webview2/ and run this again.")
    }
    Write-Step "WebView2 runtime $webview found."
    $drive = Get-PSDrive -Name ([IO.Path]::GetPathRoot($Options.InstallDir).Substring(0, 1))
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    if ($freeGb -lt 6) {
        Stop-Install 2 ("$AppName needs about 6 GB free (the app about 2 GB, models about " +
            "3 GB, plus working space). Only $freeGb GB is free.")
    }
    $ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
    if ($ramGb -lt 8) {
        Write-Step "Warning: this computer has $ramGb GB of memory; 8 GB or more is recommended."
    }
}

# -- package -------------------------------------------------------------------------

function Get-PackageRoot {
    if (-not $Options.Package) { return $PSScriptRoot }
    $zip = (Resolve-Path -LiteralPath $Options.Package).Path
    $sidecar = "$zip.sha256"
    if (-not (Test-Path -LiteralPath $sidecar)) {
        Stop-Install 4 "The checksum file $sidecar is missing; download it with the package."
    }
    $expected = ((Get-Content -LiteralPath $sidecar -Raw).Trim() -split "\s+")[0].ToLowerInvariant()
    Write-Step "Checking the package checksum..."
    if ((Get-FileSha256 $zip) -ne $expected) {
        Stop-Install 4 "The package does not match its checksum. Download it again."
    }
    # Short folder: Windows PowerShell cannot handle paths over 260 characters.
    $temp = Join-Path ([IO.Path]::GetTempPath()) ("ww" + [guid]::NewGuid().ToString("N").Substring(0, 6))
    $script:TempExtract = $temp
    Write-Step "Extracting..."
    Add-Type -AssemblyName System.IO.Compression.FileSystem     # much faster than Expand-Archive
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $temp)
    $root = Get-ChildItem -LiteralPath $temp -Directory | Select-Object -First 1
    return $root.FullName
}

function Test-PackageFile([string]$Root) {
    $manifestPath = Join-Path $Root "package-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath)) { Stop-Install 4 "package-manifest.json is missing." }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $names = @($manifest.PSObject.Properties.Name)
    Write-Step "Verifying $($names.Count) files..."
    foreach ($name in $names) {
        $path = Join-Path $Root ($name -replace "/", "\")
        if (-not (Test-Path -LiteralPath $path)) { Stop-Install 4 "A package file is missing: $name" }
        $entry = $manifest.$name
        if ((Get-Item -LiteralPath $path).Length -ne [long]$entry[0] -or (Get-FileSha256 $path) -ne $entry[1]) {
            Stop-Install 4 "A package file does not match its checksum: $name"
        }
    }
}

function Test-AppRunning {
    $running = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($Options.InstallDir, [StringComparison]::OrdinalIgnoreCase) } catch { $false }
    }
    if ($running) { Stop-Install 3 "$AppName is running. Close it (and let any processing finish), then run this again." }
}

# -- install --------------------------------------------------------------------------

function Install-Version([string]$Root, [string]$Version) {
    $versions = Join-Path $Options.InstallDir "versions"
    $target = Join-Path $versions $Version
    New-Item -ItemType Directory -Force -Path $versions | Out-Null
    $staging = "$target.partial"
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    Write-Step "Copying files..."
    Copy-Item -LiteralPath $Root -Destination $staging -Recurse
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    Rename-Item -LiteralPath $staging -NewName (Split-Path $target -Leaf)
    Copy-Item -LiteralPath (Join-Path $target "uninstall.ps1") -Destination (Join-Path $Options.InstallDir "uninstall.ps1") -Force
    return $target
}

function Set-CurrentVersion([string]$Target, [string]$Version) {
    $currentFile = Join-Path $Options.InstallDir "current.json"
    $previous = $null
    if (Test-Path -LiteralPath $currentFile) {
        $previous = (Get-Content -LiteralPath $currentFile -Raw | ConvertFrom-Json).version
    }
    @{ version = $Version; path = $Target; previous = $previous } | ConvertTo-Json |
        Set-Content -LiteralPath $currentFile -Encoding UTF8
    # Keep the current and the previous version (for rollback, #26); remove older ones.
    Get-ChildItem -LiteralPath (Join-Path $Options.InstallDir "versions") -Directory | Where-Object {
        $_.Name -ne $Version -and $_.Name -ne $previous -and $_.Name -notlike "*.partial"
    } | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
}

function New-Shortcut([string]$Target) {
    if ($Options.SkipShortcut) { return }
    $programs = [Environment]::GetFolderPath("Programs")
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Join-Path $programs "$AppName.lnk"))
    $link.TargetPath = Join-Path $Target "runtime\pythonw.exe"
    $link.Arguments = "-m workspace"
    $link.WorkingDirectory = Join-Path $Target "app"
    $link.Description = "Transcribe and review meetings on this computer"
    $link.Save()
}

function Register-Uninstall([string]$Target, [string]$Version) {
    $sizeKb = [int]((Get-ChildItem -LiteralPath $Target -Recurse -File | Measure-Object Length -Sum).Sum / 1KB)
    $uninstall = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $Options.InstallDir 'uninstall.ps1')`""
    New-Item -Path $RegKey -Force | Out-Null
    $values = @{
        DisplayName = $AppName; DisplayVersion = $Version; Publisher = "Whisper Transcriber project"
        InstallLocation = $Options.InstallDir; UninstallString = $uninstall
        QuietUninstallString = "$uninstall -Silent"; EstimatedSize = $sizeKb
        NoModify = 1; NoRepair = 1
    }
    foreach ($k in $values.Keys) {
        $type = if ($values[$k] -is [int]) { "DWord" } else { "String" }
        New-ItemProperty -Path $RegKey -Name $k -Value $values[$k] -PropertyType $type -Force | Out-Null
    }
}

# -- main ---------------------------------------------------------------------------

try {
    Test-Prerequisite
    $root = Get-PackageRoot
    $version = (Get-Content -LiteralPath (Join-Path $root "version.json") -Raw | ConvertFrom-Json).version
    Write-Step "Installing $AppName $version for $env:USERNAME..."
    Test-PackageFile $root
    Test-AppRunning
    $target = Install-Version $root $version
    Set-CurrentVersion $target $version
    New-Shortcut $target
    Register-Uninstall $target $version
    Write-Step "$AppName $version is installed. Open it from the Start menu."
    Write-Step "Your workspace and downloaded models are kept separately and are not affected by updates or uninstalling."
    exit 0
} catch {
    Stop-Install 1 "Installation failed: $($_.Exception.Message)"
} finally {
    if ($script:TempExtract -and (Test-Path -LiteralPath $script:TempExtract)) {
        Remove-Item -LiteralPath $script:TempExtract -Recurse -Force -ErrorAction SilentlyContinue
    }
}
