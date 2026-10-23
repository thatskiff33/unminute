<#
.SYNOPSIS
  Runs the installer's Pester tests (tests/powershell) with a pinned Pester (#111 B6a).

.DESCRIPTION
  Pester 5.9.1 comes from the PowerShell Gallery as its .nupkg file and is used only
  when the file's SHA-256 equals the one recorded below (the owner's decision B6-5,
  21 October 2026: pinned by version and hash). The package is downloaded into
  -ModuleRoot unless a copy is already there, checked, unpacked into a new folder,
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
  changed, 2 the Pester package
  did not match its recorded SHA-256 (nothing was unpacked or run).
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
if (-not (Test-Path -LiteralPath $package -PathType Leaf)) {
    Write-Host "Downloading Pester $PesterVersion from the PowerShell Gallery..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $PesterUrl -OutFile $package -UseBasicParsing
}
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $package).Hash.ToLowerInvariant()
if ($actual -ne $PesterSha256) {
    [Console]::Error.WriteLine("The Pester package does not match its recorded SHA-256 ($actual), so nothing was run.")
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
