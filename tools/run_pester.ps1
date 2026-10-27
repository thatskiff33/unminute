<#
.SYNOPSIS
  Runs the installer's Pester tests (tests/powershell) with a pinned Pester (#111 B6a).

.DESCRIPTION
  Pester 5.9.1 comes from the PowerShell Gallery as its .nupkg file and is used only
  when the file's SHA-256 equals the one recorded below (the owner's decision B6-5,
  21 October 2026: pinned by version and hash). The package is downloaded into
  -ModuleRoot unless a copy matching the hash is already there (a copy that does not
  match is removed), with up to 3 attempts (#427), checked, unpacked into a new folder,
  and imported from that folder by path, so no other Pester (Windows ships 3.4.0)
  is used. Then every *.Tests.ps1 file under tests/powershell runs, with LOCALAPPDATA
  and APPDATA in a new scratch folder under -ModuleRoot; the run fails if this
  computer's real install, per user or machine (its folder, Start menu shortcut,
  uninstall entry, pins or Event Log source), changed meanwhile. Never run the tests
  any other way (docs/DEVELOPER_NOTES.md).

  Run it under Windows PowerShell 5.1, which runs the installer:
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/run_pester.ps1 -ModuleRoot <folder>

  Pester is licensed under the Apache License 2.0 (its package's licenseUrl and
  copyright line, checked 23 October 2026). It is a development tool only: it is not
  in the package.

  Exit codes: 0 every test passed, 1 a test failed, none ran or the real install
  changed, 2 no Pester package matching its recorded SHA-256 could be obtained
  (nothing was unpacked or run).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ModuleRoot
)

$ErrorActionPreference = "Stop"
$PesterVersion = "5.9.1"
$PesterSha256 = "8dd4060fc3bc895f05bd655e4ab82abe346de54a4e6415dfb727cc8378672b69"
$PesterUrl = "https://www.powershellgallery.com/api/v2/package/Pester/$PesterVersion"

# -- the pinned package ------------------------------------------------------------------

$ModuleRoot = [IO.Path]::GetFullPath($ModuleRoot)
[void][IO.Directory]::CreateDirectory($ModuleRoot)
$package = Join-Path $ModuleRoot "pester.$PesterVersion.nupkg"
function Test-PesterPackage([string]$Path) {
    (Test-Path -LiteralPath $Path -PathType Leaf) -and
        (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash -eq $PesterSha256
}
# A copy already there (an earlier run's, or in CI the Actions cache's) is used only if
# it matches; any other file is removed and the package downloaded afresh (#427).
if ((Test-Path -LiteralPath $package) -and -not (Test-PesterPackage $package)) {
    Write-Host "The Pester package in -ModuleRoot does not match its recorded SHA-256; downloading it again."
    Remove-Item -LiteralPath $package -Force
}
# The PowerShell Gallery fails now and then: up to 3 attempts, waiting longer each time.
# A download is kept only if it matches.
$part = "$package.part"
for ($attempt = 1; $attempt -le 3 -and -not (Test-Path -LiteralPath $package); $attempt++) {
    if ($attempt -gt 1) { Start-Sleep -Seconds (15 * ($attempt - 1)) }
    Write-Host "Downloading Pester $PesterVersion from the PowerShell Gallery (attempt $attempt of 3)..."
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $PesterUrl -OutFile $part -UseBasicParsing -TimeoutSec 300
        if (Test-PesterPackage $part) { Move-Item -LiteralPath $part -Destination $package }
        else { Write-Host "The download does not match the recorded SHA-256." }
    } catch {
        Write-Host "The download failed: $($_.Exception.Message)"
    }
    Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
}
if (-not (Test-PesterPackage $package)) {
    [Console]::Error.WriteLine("No Pester package matching its recorded SHA-256 could be obtained, so nothing was run.")
    exit 2
}

# A new folder each run, so nothing left from an earlier run is imported. A short
# name: Windows PowerShell stops at 260 characters, and Pester's package has deep paths.
$module = Join-Path $ModuleRoot ("p" + [guid]::NewGuid().ToString("N").Substring(0, 8))
if (Test-Path -LiteralPath $module) { throw "The folder for unpacking Pester already exists; run this again." }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::ExtractToDirectory($package, $module)
Remove-Module Pester -ErrorAction SilentlyContinue
Import-Module (Join-Path $module "Pester.psd1") -Force
$loaded = Get-Module Pester
if ($loaded.Version -ne [version]$PesterVersion -or
    -not $loaded.ModuleBase.StartsWith($module, [StringComparison]::OrdinalIgnoreCase)) {
    [Console]::Error.WriteLine("Pester $PesterVersion did not load from the checked package.")
    exit 1
}

# -- the tests ---------------------------------------------------------------------------

# The tests must never reach this computer's real install (on 23 October 2026 a local
# run uninstalled it). They load the scripts only through tests/powershell/Isolation.ps1;
# on top of that, they run here with LOCALAPPDATA and APPDATA in a new scratch folder,
# and the run fails if the real install's folder, Start menu shortcut, uninstall entry
# or pins of Unminute (TaskBar and StartMenu, which uninstall.ps1 -Offboard removes)
# changed while they ran, or, since B6b, the install for the whole computer (Program
# Files\Unminute, the all-users shortcut, the HKLM uninstall entry and the Event Log
# source, read in the 64-bit registry view; reading HKLM needs no administrator
# rights): Get-RealInstallState in Isolation.ps1, which only defines functions and
# reads. The real places come from the shell, not those variables. Only names, sizes,
# times, attributes and values are compared, in memory; nothing is printed.
. (Join-Path (Split-Path -Parent $PSScriptRoot) "tests\powershell\Isolation.ps1")

$scratch = Join-Path $ModuleRoot ("s" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$saved = @{ LOCALAPPDATA = $env:LOCALAPPDATA; APPDATA = $env:APPDATA }
$before = Get-RealInstallState
$env:LOCALAPPDATA = [IO.Directory]::CreateDirectory((Join-Path $scratch "Local")).FullName
$env:APPDATA = [IO.Directory]::CreateDirectory((Join-Path $scratch "Roaming")).FullName
try {
    $tests = Join-Path (Split-Path -Parent $PSScriptRoot) "tests\powershell"
    $config = New-PesterConfiguration
    $config.Run.Path = $tests
    $config.Run.PassThru = $true
    $config.Output.Verbosity = "Detailed"
    $result = Invoke-Pester -Configuration $config
} finally {
    $env:LOCALAPPDATA = $saved.LOCALAPPDATA
    $env:APPDATA = $saved.APPDATA
}
$changed = (Get-RealInstallState) -cne $before
try { Remove-Item -LiteralPath $module -Recurse -Force } catch { }
try { Remove-Item -LiteralPath $scratch -Recurse -Force } catch { }
if ($changed) {
    [Console]::Error.WriteLine("A test changed this computer's real install (its folder, Start menu shortcut, uninstall entry, pins or Event Log source). Check it.")
    exit 1
}
if ($result.TotalCount -eq 0 -or $result.FailedCount -gt 0 -or $result.Result -ne "Passed") { exit 1 }
exit 0
