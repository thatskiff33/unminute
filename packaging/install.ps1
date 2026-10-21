<#
.SYNOPSIS
  Installs Unminute for the current Windows user (#11).

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

  Updates (#26) use the same command. Before switching version, the NEW
  package's runtime checks the existing workspace (python -m workspace --gate):
  an update that changes how the workspace is stored waits until the workspace
  holds no meetings, retention holds or unfinished cleanup, and says why.
  Nothing is expired, deleted or backed up to let an update proceed.

  -Managed records that IT manages updates (Intune); -Rollback switches back to
  the previous installed version if it can still open the workspace.

  The install folder must be new, empty, an install (a current.json naming a
  valid version), or hold only what an install that stopped early leaves;
  anything else, or a link or junction where the installer would look, is
  refused before anything is written (#297).
  Only versions the installer made are removed, never through a link.

  Exit codes: 0 installed, 1 failed, 2 prerequisites missing or the install
  folder is not allowed, 3 the app is running, 4 the package failed
  verification (including a version that is not a plain folder name),
  5 the update waits for the workspace (see the message), 6 this version cannot
  open the existing workspace (it was created by a newer version) or its
  location is refused (missing, synced, on a network drive, or a data folder
  link that cannot be reached; #219, #266).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Silent
#>
[CmdletBinding()]
param(
    [string]$Package = "",
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Unminute"),
    [switch]$Silent,
    [switch]$SkipShortcut,
    [switch]$Managed,
    [switch]$SelfService,
    [switch]$Rollback
)

$ErrorActionPreference = "Stop"
# Parameters, read by the functions below.
$Options = @{ Package = $Package; InstallDir = $InstallDir; Silent = [bool]$Silent; SkipShortcut = [bool]$SkipShortcut
             Managed = [bool]$Managed; SelfService = [bool]$SelfService; Rollback = [bool]$Rollback }
$AppName = "Unminute"
$script:TempExtract = $null
$RegKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Unminute"
# Must match APP_ID in workspace/__main__.py (taskbar grouping and icon).
$AppId = "Unlimit.Unminute"
# How the app is started. -I: isolated (no PYTHON* variables, user site-packages or
# current folder); -B: write no bytecode into the install (it ships precompiled, #111
# B2). Python 3.14 lets another process attach a debugger and run code in a running
# Python process (PEP 768); -X disable-remote-debug turns that off (#111 B4). The
# app's workers inherit all three. Older runtimes ignore the -X option.
$LaunchArgs = "-I -B -X disable-remote-debug -m workspace"
$WebView2Client = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

function Write-Step([string]$Message) {
    if (-not $Options.Silent) { Write-Host $Message }
}

function Write-Warn([string]$Message) {
    if ($Options.Silent) { [Console]::Error.WriteLine("Warning: $Message") }
    else { Write-Host "Warning: $Message" -ForegroundColor Yellow }
}

function Stop-Install([int]$Code, [string]$Message) {
    if ($Options.Silent) { [Console]::Error.WriteLine($Message) } else { Write-Host $Message -ForegroundColor Red }
    exit $Code
}

function Get-FileSha256([string]$Path) {
    (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

# -- prerequisites -----------------------------------------------------------------

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

function Test-Prerequisite {
    # Package paths are at most 140 characters below <InstallDir>\versions\<version>\
    # and Windows PowerShell stops at 259; allow up to 16 characters of version.
    if ($Options.InstallDir.Length -gt 92) {
        Stop-Install 2 "The install folder path is too long for Windows (over 92 characters). Choose a shorter -InstallDir."
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
    Test-Hardware
}

# Minimum 4 processor cores and 8 GB of memory; recommended 8 cores and 16 GB
# (#28). Below the minimum the installer warns and, when someone is at the
# keyboard, asks before continuing; unattended installs record the warning and
# continue. Windows reports a little less memory than is fitted (an 8 GB laptop
# shows about 7.7 GB), hence the 7.5 and 15 GB thresholds.
function Test-Hardware {
    $cores = (Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum
    $ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
    $below = @()
    if ($cores -lt 4) { $below += "$cores processor cores (minimum 4)" }
    if ($ramGb -lt 7.5) { $below += "$ramGb GB of memory (minimum 8 GB)" }
    if ($below.Count) {
        $message = "This computer has " + ($below -join " and ") + ". $AppName may be slow " +
            "or run out of memory on long meetings. Recommended: 8 cores and 16 GB."
        if ($Options.Silent) {
            [Console]::Error.WriteLine("Warning: $message")
            return
        }
        Write-Host "Warning: $message" -ForegroundColor Yellow
        $answer = Read-Host "Install anyway? (y/N)"
        if ($answer -notmatch '^(y|yes)$') { Stop-Install 2 "Installation cancelled: the computer is below the minimum hardware." }
    } elseif ($cores -lt 8 -or $ramGb -lt 15) {
        Write-Step "Note: $cores processor cores and $ramGb GB of memory meet the minimum. 8 cores and 16 GB are recommended for long meetings."
    } else {
        Write-Step "Hardware: $cores processor cores, $ramGb GB of memory."
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
    $temp = Join-Path ([IO.Path]::GetTempPath()) ("um" + [guid]::NewGuid().ToString("N").Substring(0, 6))
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
    # Nothing may be added either: an extra file (a planted .pth or module)
    # would be loaded by the app.
    $listed = @{}
    foreach ($n in $names) { $listed[$n.ToLowerInvariant()] = $true }
    $rootFull = (Resolve-Path -LiteralPath $Root).Path.TrimEnd("\") + "\"
    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File -Force) {
        $rel = $file.FullName.Substring($rootFull.Length).Replace("\", "/")
        if ($rel -ne "package-manifest.json" -and -not $listed.ContainsKey($rel.ToLowerInvariant())) {
            Stop-Install 4 "The package contains a file that is not in its manifest: $rel"
        }
    }
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
        try { $_.Path -and $_.Path.StartsWith($Options.InstallDir.TrimEnd("\") + "\", [StringComparison]::OrdinalIgnoreCase) } catch { $false }
    }
    if ($running) { Stop-Install 3 "$AppName is running. Close it (and let any processing finish), then run this again." }
}

# -- the install folder (#297) ---------------------------------------------------------

# A version becomes a folder name under versions, so the package's version and the
# values in current.json must be plain names first: the uninstaller's pattern
# (tests/test_hardening.py keeps the two equal), no final dot (Windows drops it, so
# 0.6.0. would name the folder 0.6.0), and not a working folder's name. The patterns
# end in \z, not $, which also matches before a final newline ("1.0.0`n").
$VersionPattern = '^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\z'

function Test-VersionName([string]$Name) {
    return ($Name -cmatch $VersionPattern) -and -not $Name.EndsWith(".") -and
        $Name -notmatch '\.(partial|old)\z'
}

# A version from current.json, or $null when it is missing or not a plain name.
function Get-VersionValue($Value) {
    $name = [string]$Value
    if (Test-VersionName $name) { return $name }
    return $null
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

# A version the installer made: a plain name, not a link, holding a version.json
# (not a link) whose version is that name, exactly.
function Test-OwnVersion([IO.DirectoryInfo]$Dir) {
    if (-not (Test-VersionName $Dir.Name) -or (Test-Link $Dir)) { return $false }
    $file = [IO.FileInfo]::new([IO.Path]::Combine($Dir.FullName, "version.json"))
    if (-not $file.Exists -or (Test-Link $file)) { return $false }
    try { $info = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json } catch { return $false }
    return ([string]$info.version) -ceq $Dir.Name
}

# A working folder the installer made: <version>.partial (a copy in progress) or
# <version>.old (a version being replaced by itself), not a link.
function Test-OwnWorkingFolder([IO.DirectoryInfo]$Dir) {
    if ((Test-Link $Dir) -or $Dir.Name -notmatch '^(.+)\.(partial|old)\z') { return $false }
    return Test-VersionName $Matches[1]
}

# An install's record: a current.json that reads as a JSON object naming a plain
# version. Anything else there, such as another program's current.json, does not make
# the folder an install (#297 review).
function Test-InstallRecord([IO.FileInfo]$File) {
    try { $record = Get-Content -LiteralPath $File.FullName -Raw | ConvertFrom-Json } catch { return $false }
    if ($record -isnot [Management.Automation.PSCustomObject]) { return $false }
    return $null -ne (Get-VersionValue $record.version)
}

# The installer writes and removes only inside a folder that is new, empty, an install
# (its current.json is an install's record, above), or holds only what an install that
# stopped before writing current.json leaves: versions, holding only versions and
# working folders the installer made, and uninstall.ps1 beside a complete version
# (#245, #239). Any other current.json counts as someone else's file. So a wrong
# -InstallDir, such as a shared folder or a drive's root, loses nothing. A link or
# junction at current.json, uninstall.ps1, versions or directly inside versions is
# refused in every case. Checked before anything is written; the messages name no
# path. An install keeps whatever else is in it: the installer never removes that.
function Assert-InstallFolder([string]$Dir) {
    $notOurs = "The install folder already holds files that $AppName did not put there. Choose an empty or new -InstallDir."
    if ([IO.File]::Exists($Dir)) { Stop-Install 2 $notOurs }
    $folder = [IO.DirectoryInfo]::new($Dir)
    if (-not $folder.Exists) { return }
    $install = $false; $uninstaller = $false; $versions = $null
    $foreign = 0; $links = 0; $complete = 0; $strays = 0
    foreach ($item in $folder.GetFileSystemInfos()) {
        $isDir = $item -is [IO.DirectoryInfo]
        if (@("current.json", "uninstall.ps1", "versions") -notcontains $item.Name) { $foreign++ }
        elseif (Test-Link $item) { $links++ }
        elseif ($isDir -ne ($item.Name -eq "versions")) { $foreign++ }
        elseif ($item.Name -eq "current.json") {
            if (Test-InstallRecord $item) { $install = $true } else { $foreign++ }
        }
        elseif ($item.Name -eq "uninstall.ps1") { $uninstaller = $true }
        else { $versions = $item }
    }
    if ($versions) {
        foreach ($item in $versions.GetFileSystemInfos()) {
            if (Test-Link $item) { $links++ }
            elseif ($item -isnot [IO.DirectoryInfo]) { $strays++ }
            elseif (Test-OwnVersion $item) { $complete++ }
            elseif (-not (Test-OwnWorkingFolder $item)) { $strays++ }
        }
    }
    if ($links) {
        Stop-Install 2 ("The install folder holds a link or junction that $AppName did not put there, " +
            "so nothing was changed. Remove it, or choose an empty or new -InstallDir.")
    }
    if ($install) { return }
    if ($foreign -or $strays -or ($uninstaller -and -not $complete)) { Stop-Install 2 $notOurs }
}

# -- update gate (#26) ----------------------------------------------------------------

function Test-WorkspaceGate([string]$Root, [string]$Checker = "", [int]$Schema = 0) {
    # Normally run by the version that will take over the workspace, which
    # knows its own schema. For an older version that cannot check itself,
    # $Checker (a newer runtime) checks on its behalf with -Schema.
    $base = if ($Checker) { $Checker } else { $Root }
    $python = Join-Path $base "runtime\python.exe"
    # -B: write nothing into the package.
    $gateArgs = @("-I", "-B", "-X", "disable-remote-debug", "-m", "workspace", "--gate")
    if ($Schema -gt 0) { $gateArgs += @("--schema", "$Schema") }
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"                  # warnings on stderr must not abort
    Push-Location (Join-Path $base "app")
    try { $output = & $python @gateArgs 2>$null; $code = $LASTEXITCODE } finally {
        Pop-Location
        $ErrorActionPreference = $saved
    }
    $result = $null
    try { $result = ($output | Select-Object -Last 1) | ConvertFrom-Json } catch { }
    $message = if ($result) { $result.message } else { "The workspace check did not run." }
    switch ($code) {
        0 { Write-Step $message; return }
        5 { Stop-Install 5 ("The update is waiting. " + $message) }
        6 { Stop-Install 6 $message }
        default { Stop-Install 1 "Could not check the workspace before updating: $message" }
    }
}

# -- install --------------------------------------------------------------------------

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

function Install-Version([string]$Root, [string]$Version) {
    $versions = Join-Path $Options.InstallDir "versions"
    $target = Join-Path $versions $Version
    New-Item -ItemType Directory -Force -Path $versions | Out-Null
    $staging = "$target.partial"
    Remove-OwnFolder $staging
    Write-Step "Copying files..."
    Copy-Item -LiteralPath $Root -Destination $staging -Recurse
    # Files extracted by Explorer carry the "downloaded from the internet" mark;
    # the package was just verified, so clear it on the executable files.
    Get-ChildItem -LiteralPath $staging -Recurse -File -Include *.exe, *.dll, *.pyd, *.ps1 |
        Unblock-File -ErrorAction SilentlyContinue
    # Same version again: move the live copy aside first, so a locked file can
    # never leave a half-deleted version in use. Both renames wait out a file held
    # open for a moment, such as by a virus scan of the new files (#239).
    $old = "$target.old"
    Remove-OwnFolder $old
    if (Test-Path -LiteralPath $target) { Invoke-LockRetry { Rename-Item -LiteralPath $target -NewName (Split-Path $old -Leaf) } }
    Invoke-LockRetry { Rename-Item -LiteralPath $staging -NewName (Split-Path $target -Leaf) }
    try { Remove-OwnFolder $old } catch { }        # if it fails, the next install of this version removes it
    Copy-Item -LiteralPath (Join-Path $target "uninstall.ps1") -Destination (Join-Path $Options.InstallDir "uninstall.ps1") -Force
    return $target
}

function Set-CurrentVersion([string]$Target, [string]$Version) {
    $currentFile = Join-Path $Options.InstallDir "current.json"
    $previous = $null
    if (Test-Path -LiteralPath $currentFile) {
        $was = Get-Content -LiteralPath $currentFile -Raw | ConvertFrom-Json
        # A value that is not a plain version name is ignored (#297). Reinstalling the
        # same version keeps the existing rollback version.
        $wasVersion = Get-VersionValue $was.version
        $previous = if ($wasVersion -and $wasVersion -eq $Version) { Get-VersionValue $was.previous } else { $wasVersion }
    }
    $managed = [bool]$Options.Managed
    if (-not $managed -and -not $Options.SelfService -and (Test-Path -LiteralPath $currentFile)) {
        # An IT-managed installation stays managed across updates.
        $managed = [bool](Get-Content -LiteralPath $currentFile -Raw | ConvertFrom-Json).managed
    }
    @{ version = $Version; path = $Target; previous = $previous; managed = $managed } |
        ConvertTo-Json | Set-Content -LiteralPath $currentFile -Encoding UTF8
    # Keep the current and the previous version (for rollback, #26); remove older ones,
    # but only versions the installer made, never through a link (#297). Anything else
    # is left in place and counted, without names. Working folders (.partial, .old)
    # are left for the next install of that version.
    $left = 0
    foreach ($dir in [IO.DirectoryInfo]::new((Join-Path $Options.InstallDir "versions")).GetDirectories()) {
        if ($dir.Name -eq $Version -or ($previous -and $dir.Name -eq $previous) -or
            $dir.Name -like "*.partial" -or $dir.Name -like "*.old") { continue }
        if (Test-OwnVersion $dir) { Remove-OwnFolder $dir.FullName } else { $left++ }
    }
    if ($left) {
        Write-Warn "$left folder(s) in the versions folder were not made by $AppName, so they were left in place."
    }
}

# Stamp an explicit AppUserModelID onto a .lnk so a pinned shortcut groups with
# the running window (workspace/__main__.py sets the same ID at startup). The target is
# pythonw.exe, whose default identity is shared with every other Python app, so
# without this a taskbar pin shows as a second button while the app is running.
# Done via the shell property store (no admin needed).
function Set-ShortcutAppId([string]$LnkPath, [string]$AppId) {
    if (-not ([System.Management.Automation.PSTypeName]'ShellLnk.Helper').Type) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace ShellLnk {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PropertyKey { public Guid fmtid; public uint pid;
        public PropertyKey(Guid id, uint p) { fmtid = id; pid = p; } }
    [ComImport, Guid("0000010b-0000-0000-C000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPersistFile {
        void GetClassID(out Guid pClassID);
        [PreserveSig] int IsDirty();
        void Load([MarshalAs(UnmanagedType.LPWStr)] string n, int m);
        void Save([MarshalAs(UnmanagedType.LPWStr)] string n, [MarshalAs(UnmanagedType.Bool)] bool r);
        void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string n);
        void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string n); }
    [ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPropertyStore {
        void GetCount(out uint c);
        void GetAt(uint i, out PropertyKey k);
        void GetValue(ref PropertyKey k, out PropVariant v);
        void SetValue(ref PropertyKey k, ref PropVariant v);
        void Commit(); }
    [StructLayout(LayoutKind.Explicit)]
    public struct PropVariant { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr p; }
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")] public class CShellLink { }
    public static class Helper {
        static PropertyKey K = new PropertyKey(new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5);
        [DllImport("ole32.dll")] static extern int PropVariantClear(ref PropVariant pv);
        public static void SetAppId(string lnk, string appId) {
            IPersistFile pf = (IPersistFile)(new CShellLink());
            pf.Load(lnk, 2); // STGM_READWRITE
            IPropertyStore store = (IPropertyStore)pf;
            PropVariant pv = new PropVariant();
            pv.vt = 31; // VT_LPWSTR
            pv.p = Marshal.StringToCoTaskMemUni(appId);
            store.SetValue(ref K, ref pv);
            store.Commit();
            PropVariantClear(ref pv);
            pf.Save(lnk, true);
        }
    }
}
'@
    }
    [ShellLnk.Helper]::SetAppId($LnkPath, $AppId)
}

# The display name of an installed version, so a rollback to an older version
# recreates the shortcut and entry that version's own uninstaller expects.
function Get-VersionAppName([string]$Target) {
    try {
        $info = Get-Content -LiteralPath (Join-Path $Target "version.json") -Raw | ConvertFrom-Json
        if ($info.name) { return [string]$info.name }
    } catch { }
    return $AppName
}

function New-Shortcut([string]$Target) {
    Update-PinnedShortcut $Target
    if ($Options.SkipShortcut) { return }
    $name = Get-VersionAppName $Target
    $programs = [Environment]::GetFolderPath("Programs")
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Join-Path $programs "$name.lnk"))
    $link.TargetPath = Join-Path $Target "runtime\pythonw.exe"
    $link.Arguments = $LaunchArgs
    $link.WorkingDirectory = Join-Path $Target "app"
    $link.Description = "Transcribe and review meetings on this computer"
    $icon = Join-Path $Target "app\unminute.ico"
    if (Test-Path -LiteralPath $icon) { $link.IconLocation = "$icon,0" }
    $link.Save()
    try { Set-ShortcutAppId $link.FullName $AppId }
    catch { Write-Warning "Could not set the taskbar identity on the shortcut: $($_.Exception.Message)" }
}

# Pinning copies the Start menu shortcut, so a taskbar or Start pin keeps pointing at
# the version that was current when it was pinned, and breaks once that folder is
# removed. Repoint pins that run this install's pythonw.exe at the new version. Pins
# to anything else are left alone. Failures only warn.
function Update-PinnedShortcut([string]$Target, [string[]]$Folders = @()) {
    if (-not $Folders) {
        $pinned = Join-Path $env:APPDATA "Microsoft\Internet Explorer\Quick Launch\User Pinned"
        $Folders = @((Join-Path $pinned "TaskBar"), (Join-Path $pinned "StartMenu"))
    }
    $dir = $Options.InstallDir
    try { $dir = (Get-Item -LiteralPath $dir -ErrorAction Stop).FullName } catch { }
    $prefix = (Join-Path $dir.TrimEnd("\") "versions") + "\"
    $shell = New-Object -ComObject WScript.Shell
    foreach ($folder in $Folders) {
        if (-not (Test-Path -LiteralPath $folder)) { continue }
        foreach ($file in Get-ChildItem -LiteralPath $folder -Filter "*.lnk" -File) {
            $sc = $null
            try {
                $sc = $shell.CreateShortcut($file.FullName)
                $old = [string]$sc.TargetPath
                $ours = $old.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -and
                    $old.EndsWith("\runtime\pythonw.exe", [StringComparison]::OrdinalIgnoreCase)
                if (-not $ours) { continue }
                $sc.TargetPath = Join-Path $Target "runtime\pythonw.exe"
                $sc.Arguments = $LaunchArgs
                $sc.WorkingDirectory = Join-Path $Target "app"
                $icon = Join-Path $Target "app\unminute.ico"
                if (Test-Path -LiteralPath $icon) { $sc.IconLocation = "$icon,0" }
                $sc.Save()
                [void][Runtime.InteropServices.Marshal]::ReleaseComObject($sc); $sc = $null
                Set-ShortcutAppId $file.FullName $AppId
            } catch { Write-Warning "Could not update the pinned shortcut $($file.Name): $($_.Exception.Message)" }
            finally { if ($sc) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($sc) } }
        }
    }
}

function Register-Uninstall([string]$Target, [string]$Version) {
    $sizeKb = [int]((Get-ChildItem -LiteralPath $Target -Recurse -File | Measure-Object Length -Sum).Sum / 1KB)
    $uninstall = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $Options.InstallDir 'uninstall.ps1')`" -InstallDir `"$($Options.InstallDir)`""
    New-Item -Path $RegKey -Force | Out-Null
    $values = @{
        DisplayName = (Get-VersionAppName $Target); DisplayVersion = $Version; Publisher = "Unminute project"
        InstallLocation = $Options.InstallDir; UninstallString = $uninstall
        QuietUninstallString = "$uninstall -Silent"; EstimatedSize = $sizeKb
        NoModify = 1; NoRepair = 1
    }
    $icon = Join-Path $Target "app\unminute.ico"
    if (Test-Path -LiteralPath $icon) { $values.DisplayIcon = $icon }
    foreach ($k in $values.Keys) {
        $type = if ($values[$k] -is [int]) { "DWord" } else { "String" }
        New-ItemProperty -Path $RegKey -Name $k -Value $values[$k] -PropertyType $type -Force | Out-Null
    }
}

# -- main ---------------------------------------------------------------------------

function Invoke-Rollback {
    $currentFile = Join-Path $Options.InstallDir "current.json"
    if (-not (Test-Path -LiteralPath $currentFile)) { Stop-Install 1 "$AppName is not installed." }
    $current = Get-Content -LiteralPath $currentFile -Raw | ConvertFrom-Json
    # Both values become folder names, so each must be a plain version name (#297).
    $previous = Get-VersionValue $current.previous
    $version = Get-VersionValue $current.version
    if (-not $previous) { Stop-Install 1 "There is no previous version to go back to." }
    if (-not $version) { Stop-Install 1 "The install record does not name a valid current version, so there is nothing to go back from." }
    $target = Join-Path $Options.InstallDir "versions\$previous"
    if (-not (Test-OwnVersion ([IO.DirectoryInfo]::new($target)))) { Stop-Install 1 "The previous version is no longer installed." }
    Test-AppRunning
    # Can the older version open this workspace? Versions from before the update
    # gate cannot answer themselves; they all used schema 1.
    $info = Get-Content -LiteralPath (Join-Path $target "version.json") -Raw | ConvertFrom-Json
    if ($info.features -contains "gate") {
        Test-WorkspaceGate $target
    } else {
        Test-WorkspaceGate $target -Checker (Join-Path $Options.InstallDir "versions\$version") -Schema 1
    }
    Copy-Item -LiteralPath (Join-Path $target "uninstall.ps1") -Destination (Join-Path $Options.InstallDir "uninstall.ps1") -Force
    @{ version = $previous; path = $target; previous = $version
       managed = [bool]$current.managed } | ConvertTo-Json |
        Set-Content -LiteralPath $currentFile -Encoding UTF8
    New-Shortcut $target
    Register-Uninstall $target $previous
    Write-Step "$(Get-VersionAppName $target) is back on version $previous."
    exit 0
}

try {
    # An empty -InstallDir is refused, and a relative one is taken from PowerShell's
    # current folder, once, so every check, copy and removal, and the registered
    # uninstall command, use the same full path.
    if ([string]::IsNullOrWhiteSpace($Options.InstallDir)) {
        Stop-Install 2 "No install folder was given. Choose an -InstallDir."
    }
    $Options.InstallDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Options.InstallDir)
    # A network, device or long-path form (\\, //, \\?\) is refused: the checks
    # below compare paths as text, and the install belongs on this computer's own drive.
    if ($Options.InstallDir -match '^[\\/]{2}') {
        Stop-Install 2 "The install folder must be on this computer's own drive, not a network or device path (starting with \\ or //). Choose another -InstallDir."
    }
    $overlap = Get-DataOverlap $Options.InstallDir
    if ($overlap) {
        Stop-Install 2 "The install folder must not be in, or hold, $AppName's data folder ($overlap). Choose another -InstallDir."
    }
    # A trailing \ would end the registered uninstall command's quoted -InstallDir
    # with \", which Windows reads as a literal quote. A drive's root keeps its \.
    $driveRoot = [IO.Path]::GetPathRoot($Options.InstallDir)
    $Options.InstallDir = $driveRoot + $Options.InstallDir.Substring($driveRoot.Length).TrimEnd("\", "/")
    # Before anything is written: the folder must be one the installer may use (#297).
    Assert-InstallFolder $Options.InstallDir
    if ($Options.Rollback) { Invoke-Rollback }
    Test-Prerequisite
    $root = Get-PackageRoot
    $version = [string](Get-Content -LiteralPath (Join-Path $root "version.json") -Raw | ConvertFrom-Json).version
    if (-not (Test-VersionName $version)) {
        Stop-Install 4 "The package's version.json does not hold a valid version, so nothing was installed."
    }
    Write-Step "Installing $AppName $version for $env:USERNAME..."
    Test-PackageFile $root
    Test-AppRunning
    Test-WorkspaceGate $root
    $target = Install-Version $root $version
    Set-CurrentVersion $target $version
    New-Shortcut $target
    Register-Uninstall $target $version
    Write-Step "$AppName $version is installed. Open it from the Start menu and unlimit your meeting minutes."
    Write-Step "Your workspace and downloaded models are kept separately and are not affected by updates or uninstalling."
    exit 0
} catch {
    Stop-Install 1 "Installation failed: $($_.Exception.Message)"
} finally {
    if ($script:TempExtract -and (Test-Path -LiteralPath $script:TempExtract)) {
        Remove-Item -LiteralPath $script:TempExtract -Recurse -Force -ErrorAction SilentlyContinue
    }
}
