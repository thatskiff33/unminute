<#
.SYNOPSIS
  Installs Unminute for the current Windows user (#11).

.DESCRIPTION
  Run from the extracted package folder (or pass -Package with the zip and its
  .sha256 file). No administrator rights, Python, Git or GPU tools are needed.

  - Checks prerequisites: 64-bit Windows 10/11, the WebView2 runtime, free
    disk space and memory.
  - Verifies the package: the zip against its .sha256 file, and the package
    folder for links or junctions, before anything is copied; then every file of
    the copy in <InstallDir>\versions\<version>.partial against
    package-manifest.json, by exact name, size and SHA-256, so the bytes checked
    are the bytes that run (#111 B6a). A failure removes the copy.
  - Installs side by side under <InstallDir>\versions\<version> and keeps the
    previous version for rollback; workspace data and models are separate and
    never touched.
  - Runs one install at a time (install.lock in the install folder; a second
    run waits up to 10 minutes), writes current.json by a temporary file and a
    replace, and keeps a short log in <InstallDir>\logs\install.log with no user
    name, path or message text (#111 B6a).
  - Registers a per-user uninstall entry (used for Intune detection) and a
    Start menu shortcut.

  Updates (#26) use the same command. Before switching version, the NEW
  version's runtime, from its verified copy in the install folder, checks the
  existing workspace (python -m workspace --gate):
  an update that changes how the workspace is stored waits until the workspace
  holds no meetings, retention holds or unfinished cleanup, and says why.
  Nothing is expired, deleted or backed up to let an update proceed.

  -Managed records that IT manages updates (Intune); -Rollback switches back to
  the previous installed version if it can still open the workspace.

  The install folder must be new, empty, an install (a current.json object
  naming a valid version and, if it gives a path, that version's own folder),
  or hold only what an install that stopped early leaves; anything else, or a
  link or junction where the installer would look, is refused before anything
  is written (#297).
  Only versions the installer made are removed, never through a link.

  Exit codes: 0 installed, 1 failed (including another install still running
  after 10 minutes), 2 prerequisites missing or the install
  folder is not allowed, 3 the app is running, 4 the package failed
  verification (including a version that is not a plain folder name, or a link
  or junction in the package),
  5 the update waits for the workspace (see the message), 6 this version cannot
  open the existing workspace (it was created by a newer version) or its
  location is refused (missing, synced, on a network drive, or a data folder
  link that cannot be reached; #219, #266).

  A per-user install is refused (exit 2) once Unminute is installed for
  everyone on this computer (the 64-bit HKLM uninstall entry; #111 B6-1).

  MACHINE MODE (-Machine, #111 B6b): run by IT as SYSTEM (Intune, System
  context) in a 64-bit, elevated Windows PowerShell, with -Package only (never a
  loose folder) and never -InstallDir. It installs to %ProgramFiles%\Unminute,
  a folder only administrators can change: created with its own permissions
  (owner Administrators; full control for SYSTEM and Administrators; read and
  run for Users and ALL APPLICATION PACKAGES), and checked at every run, with
  every folder and file beneath it, for another owner, an explicit entry or a
  protected list (exit 4; never repaired silently); its junctions are checked by
  their target, not their own permissions. The package is unpacked inside that
  folder (staging-<32 hex digits>), never in a temporary folder, TMP and TEMP
  point at another such folder for the run, and no Python runs during the
  install. The Start menu entry for all users starts
  %ProgramFiles%\Unminute\current, a junction to the current version, which
  the app leaves at once for the version's real folder (workspace/launch.py).
  Kept versions: the current one, the previous one, and the newest version of
  the storage format immediately before the current one (current minus one; no
  older format in its place), for people whose workspace still holds meetings
  in that format (the owner's decisions of 21, 22 and 23 October 2026).
  Installing a version with an older storage format removes every newer one.
  It refuses while the app runs for anyone on the computer (exit 3; Intune
  retries). It registers the HKLM uninstall entry
  (Intune detection), the Start menu entry and the Event Log source, and never
  reads or writes a user's profile. -Machine -Rollback switches back to the
  previous version; -Machine -RemoveSlot <version> removes a kept version that
  is not current.
  Machine exit codes: 0 installed, 1 failed, 2 not allowed (a 32-bit or
  non-elevated run, a refused switch, a package that states no storage format,
  prerequisites missing, or an install folder holding entries or a link the
  installer did not put there), 3 the app is running, 4 the package failed
  verification or the install folder's permissions have changed.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Silent
.EXAMPLE
  %SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Machine -Package Unminute-0.7.0.zip -Silent
#>
[CmdletBinding()]
param(
    [string]$Package = "",
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Unminute"),
    [switch]$Silent,
    [switch]$SkipShortcut,
    [switch]$Managed,
    [switch]$SelfService,
    [switch]$Rollback,
    [switch]$Machine,
    [string]$RemoveSlot = ""
)

$ErrorActionPreference = "Stop"
# Parameters, read by the functions below.
$Options = @{ Package = $Package; InstallDir = $InstallDir; Silent = [bool]$Silent; SkipShortcut = [bool]$SkipShortcut
             Managed = [bool]$Managed; SelfService = [bool]$SelfService; Rollback = [bool]$Rollback
             Machine = [bool]$Machine; RemoveSlot = $RemoveSlot; InstallDirGiven = $false }
$AppName = "Unminute"
$script:TempExtract = $null
$script:StagedCopy = $null           # versions\<version>.partial while this run owns it
$script:InstallLock = $null
$script:MachineTemp = $null          # machine mode's temporary folder, and TMP and TEMP before it
$RegKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Unminute"
# The Start menu's Programs folder, for the shortcut. Read once, here, like $RegKey, so
# the tests can point both at scratch places (tests/powershell/Isolation.ps1).
$ProgramsFolder = [Environment]::GetFolderPath("Programs")
# What the installer may put in the install folder, and so all the uninstaller
# removes there (#297, #111 B6a): the versions, the record and its temporary copy,
# the uninstaller, the log folder and the lock. install.ps1 and uninstall.ps1 list
# the same names, and packaging/identity.json mirrors them (tests keep the three equal).
$OwnEntries = @("versions", "current.json", "current.json.tmp", "uninstall.ps1", "logs", "install.lock")
$LockName = "install.lock"
$LogNames = @("install.log", "install.log.1")
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
    Write-InstallLog "stopped" $Code
    if ($Options.Silent) { [Console]::Error.WriteLine($Message) } else { Write-Host $Message -ForegroundColor Red }
    exit $Code
}

# -- the installer log (#111 B6a) ------------------------------------------------------

# <InstallDir>\logs\install.log: one line per step, for IT and support. Each line holds
# only fixed words and numbers: the time (UTC), the mode, the version once it is known
# to be a plain name, the step, its result, the exit code, the time since the start
# and, for an unexpected error, its type name. Never a user name, a path or an error's
# message (P3). Written only once the install folder is known to be the installer's
# own; at most $LogLimitBytes, then the file becomes install.log.1 (one copy kept).
# A failure to write it never stops the install.
$LogLimitBytes = 256KB
$script:LogFile = $null
$script:LogVersion = "-"
$script:LogStep = "start"
$script:LogError = $null
$script:LogClock = [Diagnostics.Stopwatch]::StartNew()

function Write-InstallLog([string]$Result, [int]$Code = -1) {
    if (-not $script:LogFile) { return }
    try {
        $time = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", [Globalization.CultureInfo]::InvariantCulture)
        $mode = if ($Options.Machine) { "machine" } else { "user" }
        $line = "$time mode=$mode version=$($script:LogVersion) step=$($script:LogStep) result=$Result"
        if ($Code -ge 0) { $line += " code=$Code" }
        $line += " elapsed_ms=$($script:LogClock.ElapsedMilliseconds)"
        if ($script:LogError) { $line += " error=$($script:LogError)" }
        $line += "`r`n"
        $file = [IO.FileInfo]::new($script:LogFile)
        if ($file.Exists -and (Test-Link $file)) { return }
        if ($file.Exists -and $file.Length + $line.Length -gt $LogLimitBytes) {
            $kept = [IO.FileInfo]::new("$($script:LogFile).1")
            if ($kept.Exists) {
                if (Test-Link $kept) { return }
                $kept.Delete()
            }
            $file.MoveTo($kept.FullName)
        }
        [IO.File]::AppendAllText($script:LogFile, $line, [Text.UTF8Encoding]::new($false))
    } catch { }
}

# Records a step as started; the log line says it began.
function Set-InstallStep([string]$Step) {
    $script:LogStep = $Step
    Write-InstallLog "begun"
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
    # Machine mode needs the machine-wide runtime: a copy in one user's profile
    # serves only that user (#111 B6b).
    $keys = @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$WebView2Client",
              "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$WebView2Client")
    if (-not $Options.Machine) { $keys += "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$WebView2Client" }
    foreach ($key in $keys) {
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
        # Nobody is at the keyboard of a machine install (SYSTEM): warn only.
        if ($Options.Silent -or $Options.Machine) {
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

# A whole random GUID as 32 hex digits (the tests stand it in).
function New-RandomName { return [guid]::NewGuid().ToString("N") }

function Get-PackageRoot {
    if (-not $Options.Package) { return $PSScriptRoot }
    $zip = (Resolve-Path -LiteralPath $Options.Package).Path
    $sidecar = "$zip.sha256"
    if (-not (Test-Path -LiteralPath $sidecar)) {
        Stop-Install 4 "The checksum file $sidecar is missing; download it with the package."
    }
    $expected = ((Get-Content -LiteralPath $sidecar -Raw).Trim() -split "\s+")[0].ToLowerInvariant()
    # The zip is checked and unpacked through one handle that shares reading only, so
    # from the check to the end of the unpacking nobody can write, replace, rename or
    # delete it, and the open fails if anyone already has it open to write (review of
    # a3ab563, L3): the bytes unpacked are the bytes checked.
    $stream = [IO.FileStream]::new($zip, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read, 1MB)
    try {
        Write-Step "Checking the package checksum..."
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $actual = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace("-", "").ToLowerInvariant() }
        finally { $sha.Dispose() }
        if ($actual -ne $expected) {
            Stop-Install 4 "The package does not match its checksum. Download it again."
        }
        # A new folder named by a whole random GUID (#111 B6a); one that already exists is
        # someone else's, so the run stops and neither uses nor removes it. Short enough
        # for Windows PowerShell's 260 characters: um and 32 hex digits, the package's
        # folder and its paths of at most 140 characters under a user's temporary folder.
        # Nothing runs from here: the files are copied into the install folder and
        # verified there. Machine mode unpacks inside its own folder, which only
        # administrators can change, as staging-<32 hex digits>: never in SYSTEM's
        # temporary folder, C:\Windows\Temp or Intune's cache (#111 B6b, T22).
        $temp = if ($Options.Machine) {
            Join-Path $Options.InstallDir ("staging-" + (New-RandomName))
        } else {
            Join-Path ([IO.Path]::GetTempPath()) ("um" + (New-RandomName))
        }
        if ([IO.Directory]::Exists($temp) -or [IO.File]::Exists($temp)) {
            Stop-Install 1 "The temporary folder for extracting the package already exists, so nothing was installed. Run this again."
        }
        $script:TempExtract = $temp
        Write-Step "Extracting..."
        # Much faster than Expand-Archive.
        Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
        $stream.Position = 0
        $archive = [IO.Compression.ZipArchive]::new($stream, [IO.Compression.ZipArchiveMode]::Read, $true)
        try { [IO.Compression.ZipFileExtensions]::ExtractToDirectory($archive, $temp) }
        finally { $archive.Dispose() }
    } finally { $stream.Dispose() }
    $root = @([IO.DirectoryInfo]::new($temp).GetDirectories())
    if ($root.Count -ne 1) { Stop-Install 4 "The package does not hold one folder, so nothing was installed." }
    return $root[0].FullName
}

$NoLinkMessage = "The package contains a link or junction, so nothing was installed."

# Every file under $Root, by its path relative to $Root with / between parts, exactly
# as the folder listing spells it. Walked with [IO.DirectoryInfo], which reports each
# entry's own attributes: a link or junction anywhere (the folder itself included)
# stops the run with exit 4, and nothing behind it is listed or read (#111 B6a).
# Windows PowerShell's Get-ChildItem -Recurse is not relied on for this.
# In machine mode the same walk also checks each entry's permissions: an explicit
# entry or a protected list on any file or folder of the copy (anything but what it
# inherits from the install folder) stops the run with exit 4 (#111 B6b, B6-7).
# -NoStop: return $null instead of stopping (a same-version repair's check).
function Get-PackageListing([string]$Root, [switch]$NoStop) {
    $script:ListingProblem = $null
    $top = [IO.DirectoryInfo]::new($Root)
    if (Test-Link $top) { $script:ListingProblem = $NoLinkMessage }
    $skip = $top.FullName.TrimEnd("\").Length + 1
    $files = [Collections.Generic.Dictionary[string, IO.FileInfo]]::new([StringComparer]::Ordinal)
    $todo = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
    if (-not $script:ListingProblem) { $todo.Push($top) }
    while ($todo.Count -and -not $script:ListingProblem) {
        foreach ($item in $todo.Pop().GetFileSystemInfos()) {
            if (Test-Link $item) { $script:ListingProblem = $NoLinkMessage; break }
            if ($Options.Machine -and (Test-AclChanged $item)) { $script:ListingProblem = $PermissionMessage; break }
            if ($item -is [IO.DirectoryInfo]) { $todo.Push($item); continue }
            $files[$item.FullName.Substring($skip).Replace("\", "/")] = $item
        }
    }
    if ($script:ListingProblem) {
        if ($NoStop) { return $null }
        Stop-Install 4 $script:ListingProblem
    }
    return , $files
}

# The package folder holds no link or junction, checked before anything is copied:
# copying would otherwise follow one and read what it points to (#111 B6a).
function Assert-NoLink([string]$Root) {
    [void](Get-PackageListing $Root)
}

# Checks the files under $Root (the staged copy) against package-manifest.json:
# every file listed, nothing else, each with its size and SHA-256 (#111 B6a, B6-8).
# Names compare exactly, by ordinal comparison of the names in the folder listing: a
# letter-case variant, a Kelvin sign for K or a capital sharp s of a listed name is
# not that name, and Test-Path, which ignores case, is not used. Nothing may be added
# either: an extra file (a planted .pth or module) would be loaded by the app.
function Test-PackageFile([string]$Root) {
    $problem = Get-PackageProblem $Root
    if ($problem) { Stop-Install 4 $problem }
}

# What Test-PackageFile finds wrong with the files under $Root, or $null when they
# match the manifest. Machine mode also asks it of an installed version (a
# same-version repair, a rollback) without stopping.
function Get-PackageProblem([string]$Root) {
    $files = Get-PackageListing $Root -NoStop
    if ($null -eq $files) { return $script:ListingProblem }
    $manifestFile = $null
    if (-not $files.TryGetValue("package-manifest.json", [ref]$manifestFile)) {
        return "package-manifest.json is missing."
    }
    try { $manifest = [IO.File]::ReadAllText($manifestFile.FullName) | ConvertFrom-Json }
    catch { return "package-manifest.json cannot be read." }
    $entries = @($manifest.PSObject.Properties)
    $listed = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($entry in $entries) { [void]$listed.Add($entry.Name) }
    foreach ($rel in $files.Keys) {
        if ($rel -cne "package-manifest.json" -and -not $listed.Contains($rel)) {
            return "The package contains a file that is not in its manifest: $rel"
        }
    }
    Write-Step "Verifying $($entries.Count) files..."
    foreach ($entry in $entries) {
        $file = $null
        if (-not $files.TryGetValue($entry.Name, [ref]$file)) { return "A package file is missing: $($entry.Name)" }
        $expected = @($entry.Value)
        if ($file.Length -ne [long]$expected[0] -or (Get-FileSha256 $file.FullName) -ne [string]$expected[1]) {
            return "A package file does not match its checksum: $($entry.Name)"
        }
    }
    return $null
}

function Test-AppRunning {
    $running = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($Options.InstallDir.TrimEnd("\") + "\", [StringComparison]::OrdinalIgnoreCase) } catch { $false }
    }
    if (-not $running) { return }
    # Machine mode: a process of any user, in any session, started from the install
    # folder (the owner's decision of 21 October 2026: refuse while the app runs).
    if ($Options.Machine) {
        Stop-Install 3 "$AppName is running on this computer, so nothing was changed. Run this again once nobody has it open (Intune retries)."
    }
    Stop-Install 3 "$AppName is running. Close it (and let any processing finish), then run this again."
}

# -- the install folder (#297) ---------------------------------------------------------

# A version becomes a folder name under versions, so the package's version and the
# values in current.json must be plain names first: the uninstaller's pattern
# (tests/test_hardening.py keeps the two equal), no final dot (Windows drops it, so
# 0.6.0. would name the folder 0.6.0), and not a working folder's name (.retired:
# a version machine mode is removing, #111 B6b). The patterns end in \z, not $,
# which also matches before a final newline ("1.0.0`n").
$VersionPattern = '^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\z'

function Test-VersionName([string]$Name) {
    return ($Name -cmatch $VersionPattern) -and -not $Name.EndsWith(".") -and
        $Name -notmatch '\.(partial|old|retired)\z'
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

# A working folder the installer made: <version>.partial (a copy in progress),
# <version>.old (a version being replaced by itself) or <version>.retired (a version
# machine mode no longer keeps, being removed), not a link.
function Test-OwnWorkingFolder([IO.DirectoryInfo]$Dir) {
    if ((Test-Link $Dir) -or $Dir.Name -notmatch '^(.+)\.(partial|old|retired)\z') { return $false }
    return Test-VersionName $Matches[1]
}

# An install's record: a current.json that is a JSON object (its text starts with {,
# since PowerShell 7's ConvertFrom-Json reads a one-item list as its item) whose
# version is a JSON string with a plain name, and whose path, if it has one, is
# <InstallDir>\versions\<version>, as the installer writes it (letter case aside).
# Anything else there, such as another program's current.json, does not make the
# folder an install (#297 reviews).
function Test-InstallRecord([IO.FileInfo]$File) {
    try {
        $text = Get-Content -LiteralPath $File.FullName -Raw
        if ($text -isnot [string] -or $text -notmatch '\A[ \t\r\n]*\{') { return $false }
        $record = ConvertFrom-Json -InputObject $text
    } catch { return $false }
    if ($record -isnot [Management.Automation.PSCustomObject]) { return $false }
    $version = $record.version
    if ($version -isnot [string] -or -not (Test-VersionName $version)) { return $false }
    if ($null -eq $record.PSObject.Properties["path"]) { return $true }
    $path = $record.path
    if ($path -isnot [string]) { return $false }
    $own = [IO.Path]::Combine($File.DirectoryName, "versions", $version)
    try { $path = [IO.Path]::GetFullPath($path) } catch { return $false }
    return [string]::Equals($path, $own, [StringComparison]::OrdinalIgnoreCase)
}

# The installer writes and removes only inside a folder that is new, empty, an install
# (its current.json is an install's record, above), or holds only what an install that
# stopped before writing current.json leaves: versions, holding only versions and
# working folders the installer made, and uninstall.ps1 beside a complete version
# (#245, #239). Any other current.json counts as someone else's file. The installer's
# other entries ($OwnEntries: the log folder holding only its logs, current.json.tmp
# and install.lock, #111 B6a) may be there in any case. So a wrong -InstallDir, such
# as a shared folder or a drive's root, loses nothing. A link or junction at any of
# those names, directly inside versions or inside logs is refused in every case.
# Checked before anything is written; the messages name no path. An install keeps
# whatever else is in it: the installer never removes that.
# Machine mode also accepts its own entries (#111 B6b): the junctions current,
# current.new and current.old, each pointing at a version folder inside versions
# (Test-OwnJunction), and staging-<32 hex digits> folders left by a stopped run.
function Assert-InstallFolder([string]$Dir) {
    $notOurs = "The install folder already holds files that $AppName did not put there. Choose an empty or new -InstallDir."
    if ([IO.File]::Exists($Dir)) { Stop-Install 2 $notOurs }
    $folder = [IO.DirectoryInfo]::new($Dir)
    if (-not $folder.Exists) { return }
    $install = $false; $uninstaller = $false; $versions = $null; $logs = $null
    $foreign = 0; $links = 0; $complete = 0; $strays = 0
    foreach ($item in $folder.GetFileSystemInfos()) {
        $isDir = $item -is [IO.DirectoryInfo]
        if ($Options.Machine -and $JunctionNames -ccontains $item.Name) {
            if (-not (Test-OwnJunction $item $folder.FullName)) { $links++ }
        }
        elseif ($Options.Machine -and $item.Name -cmatch $StagingPattern) {
            if (Test-Link $item) { $links++ } elseif (-not $isDir) { $foreign++ }
        }
        elseif ($OwnEntries -notcontains $item.Name) { $foreign++ }
        elseif (Test-Link $item) { $links++ }
        elseif ($isDir -ne (@("versions", "logs") -contains $item.Name)) { $foreign++ }
        elseif ($item.Name -eq "current.json") {
            if (Test-InstallRecord $item) { $install = $true } else { $foreign++ }
        }
        elseif ($item.Name -eq "uninstall.ps1") { $uninstaller = $true }
        elseif ($item.Name -eq "versions") { $versions = $item }
        elseif ($item.Name -eq "logs") { $logs = $item }
    }
    if ($versions) {
        foreach ($item in $versions.GetFileSystemInfos()) {
            if (Test-Link $item) { $links++ }
            elseif ($item -isnot [IO.DirectoryInfo]) { $strays++ }
            elseif (Test-OwnVersion $item) { $complete++ }
            elseif (-not (Test-OwnWorkingFolder $item)) { $strays++ }
        }
    }
    if ($logs) {
        foreach ($item in $logs.GetFileSystemInfos()) {
            if (Test-Link $item) { $links++ }
            elseif ($item -is [IO.DirectoryInfo] -or $LogNames -notcontains $item.Name) { $strays++ }
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
    # As the app starts ($LaunchArgs): -B writes nothing into the version's folder.
    $gateArgs = @($LaunchArgs -split " ") + "--gate"
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

# Copies the package folder (already checked for links, Assert-NoLink) into
# versions\<version>.partial and verifies that copy against the manifest, once
# (#111 B6a, the owner's decision B6-8): the bytes checked are the bytes that will
# run. A failure here, or in any later step before the rename, removes the copy
# (the main part's finally) and changes nothing else. Returns the copy's folder.
function New-StagedCopy([string]$Root, [string]$Version) {
    $versions = Join-Path $Options.InstallDir "versions"
    New-Item -ItemType Directory -Force -Path $versions | Out-Null
    $staging = Join-Path $versions "$Version.partial"
    Remove-OwnFolder $staging
    $script:StagedCopy = $staging
    Write-Step "Copying files..."
    Copy-Item -LiteralPath $Root -Destination $staging -Recurse
    # Files extracted by Explorer carry the "downloaded from the internet" mark;
    # clear it on the executable files of the copy, which is verified next.
    Get-ChildItem -LiteralPath $staging -Recurse -File -Include *.exe, *.dll, *.pyd, *.ps1 |
        Unblock-File -ErrorAction SilentlyContinue
    Test-PackageFile $staging
    return $staging
}

# Makes the verified copy the installed version: renames versions\<version>.partial
# to versions\<version>, the last step.
function Install-Version([string]$Staging, [string]$Version) {
    $target = Join-Path (Join-Path $Options.InstallDir "versions") $Version
    # Same version again: move the live copy aside first, so a locked file can
    # never leave a half-deleted version in use. Both renames wait out a file held
    # open for a moment, such as by a virus scan of the new files (#239).
    $old = "$target.old"
    Remove-OwnFolder $old
    if (Test-Path -LiteralPath $target) { Invoke-LockRetry { Rename-Item -LiteralPath $target -NewName (Split-Path $old -Leaf) } }
    Invoke-LockRetry { Rename-Item -LiteralPath $staging -NewName (Split-Path $target -Leaf) }
    $script:StagedCopy = $null
    try { Remove-OwnFolder $old } catch { }        # if it fails, the next install of this version removes it
    Copy-Item -LiteralPath (Join-Path $target "uninstall.ps1") -Destination (Join-Path $Options.InstallDir "uninstall.ps1") -Force
    if ($Options.Machine) { Set-InheritedOnly (Join-Path $Options.InstallDir "uninstall.ps1") }
    return $target
}

# -- current.json, format 2 (#111 B5) ---------------------------------------------------

# current.json records, beside version, path, previous and managed (all that older
# readers use), each kept version's storage format and install order:
#   {"format": 2, "version": ..., "path": ..., "previous": ..., "managed": ...,
#    "slots": {"<version>": {"schema": 2, "seq": 3, "installed": "<UTC time>"}, ...}}
# schema comes from the version's own version.json; a version from before B5 states
# none, and is then recorded without one (unknown: the launcher never hands a workspace
# to it). seq is a whole number given when a version is first installed, one more than
# the highest so far, and kept when it is installed again (installed then changes), so
# "newest" is the highest seq and nothing compares version names. workspace/update.py
# (read_slots) reads it.

# The storage format a version's version.json states, or $null when it states none.
function Get-VersionSchema([string]$Folder) {
    try { $info = Get-Content -LiteralPath (Join-Path $Folder "version.json") -Raw | ConvertFrom-Json } catch { return $null }
    $schema = $info.schema
    if (($schema -is [int] -or $schema -is [long]) -and $schema -gt 0) { return [long]$schema }
    return $null
}

# The slots to record for the versions in $Order (oldest first), from the existing
# record $Was. A version it already lists keeps its seq and installed time, except
# $Fresh (the version being installed now), whose installed time is now. Any other is
# given the next seq, in that order: a format 1 record (before B5) lists none, so its
# previous version, then its current one, are numbered first. Only versions the
# installer made are recorded.
function Get-SlotRecord($Was, [string[]]$Order, [string]$Fresh) {
    $known = @{}
    if ($Was -and ($Was.format -is [int] -or $Was.format -is [long]) -and $Was.format -eq 2 -and
        $Was.slots -is [Management.Automation.PSCustomObject]) {
        foreach ($p in $Was.slots.PSObject.Properties) {
            $seq = $p.Value.seq
            if ((Test-VersionName $p.Name) -and ($seq -is [int] -or $seq -is [long]) -and $seq -gt 0) {
                $known[$p.Name] = $p.Value
            }
        }
    }
    $high = [long]0
    foreach ($entry in $known.Values) { if ([long]$entry.seq -gt $high) { $high = [long]$entry.seq } }
    $now = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", [Globalization.CultureInfo]::InvariantCulture)
    $slots = [ordered]@{}
    foreach ($name in $Order) {
        if (-not $name -or $slots.Contains($name)) { continue }
        $folder = [IO.DirectoryInfo]::new((Join-Path $Options.InstallDir "versions\$name"))
        if (-not (Test-OwnVersion $folder)) { continue }
        $entry = [ordered]@{}
        $schema = Get-VersionSchema $folder.FullName
        if ($schema) { $entry.schema = $schema }
        if ($known.ContainsKey($name)) {
            $entry.seq = [long]$known[$name].seq
            $installed = [string]$known[$name].installed
            $entry.installed = if ($name -ceq $Fresh -or -not $installed) { $now } else { $installed }
        } else {
            $high++
            $entry.seq = $high
            $entry.installed = $now
        }
        $slots[$name] = $entry
    }
    return $slots
}

# Machine mode adds "mode": "machine". The app believes it only when its machine
# trust test passes (workspace/update.py, machine_trusted): a per-user install that
# writes it into its own current.json gains nothing (#111 B6b).
function Write-CurrentRecord([string]$File, [string]$Version, [string]$Target, $Previous, [bool]$Managed, $Slots) {
    $record = [ordered]@{ format = 2; version = $Version; path = $Target; previous = $Previous; managed = $Managed }
    if ($Options.Machine) { $record.mode = "machine" }
    $record.slots = $Slots
    Save-RecordFile $File ($record | ConvertTo-Json -Depth 5)
}

# current.json is replaced whole or not at all (#111 B6a): the new text goes to
# current.json.tmp, which is flushed to the disk, then replaces current.json
# ([IO.File]::Replace; a move when there is none yet). A stop between the two leaves
# current.json as it was, and the next run removes the temporary file
# (Remove-LeftoverRecord). The bytes are what Set-Content -Encoding UTF8 wrote before:
# UTF-8 with a byte-order mark and a final line break, which Windows PowerShell 5.1's
# Get-Content (in this and earlier installers) needs to read a path that is not ASCII.
function Save-RecordFile([string]$File, [string]$Text) {
    $temp = "$File.tmp"
    $bytes = [byte[]]([Text.UTF8Encoding]::new($true).GetPreamble() + [Text.UTF8Encoding]::new($false).GetBytes($Text + "`r`n"))
    $stream = [IO.FileStream]::new($temp, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
    Move-RecordFile $temp $File
}

function Move-RecordFile([string]$Temp, [string]$File) {
    if ([IO.File]::Exists($File)) { [IO.File]::Replace($Temp, $File, [NullString]::Value) }
    else { [IO.File]::Move($Temp, $File) }
    if ($Options.Machine) { Set-InheritedOnly $File }
}

# A current.json.tmp left by a run that stopped before its replace: never read, removed.
# A link there was refused with the install folder (Assert-InstallFolder).
function Remove-LeftoverRecord {
    $temp = [IO.FileInfo]::new([IO.Path]::Combine($Options.InstallDir, "current.json.tmp"))
    if ($temp.Exists -and -not (Test-Link $temp)) { $temp.Delete() }
}

# -- one run at a time (#111 B6a) -------------------------------------------------------

# install.lock in the install folder, open with no sharing for the whole run and
# deleted when it closes (also when the process ends). A second run, or the
# uninstaller, cannot open it meanwhile: the installer waits up to $LockWaitSeconds,
# then stops with exit 1. Only someone who can write the install folder can hold it.
$LockWaitSeconds = 600

function Enter-InstallLock {
    if ($script:InstallLock) { return }         # this run holds it already (Start-MachineRun)
    $path = [IO.Path]::Combine($Options.InstallDir, $LockName)
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $said = $false
    while ($true) {
        try {
            $script:InstallLock = [IO.FileStream]::new($path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite,
                [IO.FileShare]::None, 1, [IO.FileOptions]::DeleteOnClose)
            return
        } catch {
            if (-not (Test-TransientLock $_.Exception)) { throw }
            if ($clock.Elapsed.TotalSeconds -ge $LockWaitSeconds) {
                Stop-Install 1 "Another install or update of $AppName is still running. Run this again when it has finished."
            }
            if (-not $said) {
                Write-Step "Another install or update of $AppName is running. Waiting for it to finish (up to $([int]($LockWaitSeconds / 60)) minutes)..."
                $said = $true
            }
            Start-Sleep -Milliseconds 500
        }
    }
}

function Exit-InstallLock {
    if ($script:InstallLock) {
        $script:InstallLock.Dispose()
        $script:InstallLock = $null
    }
}

function Set-CurrentVersion([string]$Target, [string]$Version) {
    $currentFile = Join-Path $Options.InstallDir "current.json"
    $previous = $null
    $was = $null
    if (Test-Path -LiteralPath $currentFile) {
        $was = Get-Content -LiteralPath $currentFile -Raw | ConvertFrom-Json
        # A value that is not a plain version name is ignored (#297). Reinstalling the
        # same version keeps the existing rollback version.
        $wasVersion = Get-VersionValue $was.version
        $previous = if ($wasVersion -and $wasVersion -eq $Version) { Get-VersionValue $was.previous } else { $wasVersion }
    }
    $managed = [bool]$Options.Managed
    if (-not $managed -and -not $Options.SelfService -and $was) {
        # An IT-managed installation stays managed across updates.
        $managed = [bool]$was.managed
    }
    $slots = Get-SlotRecord $was @($previous, $Version) $Version
    Write-CurrentRecord $currentFile $Version $Target $previous $managed $slots
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
    Save-AppShortcut (Join-Path $ProgramsFolder "$name.lnk") $Target
}

# The Start menu entry at $LnkPath, starting the app from $Target (a version's
# folder, or machine mode's current junction) with $LaunchArgs.
function Save-AppShortcut([string]$LnkPath, [string]$Target) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($LnkPath)
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

# The uninstall entry (Intune's detection reads DisplayVersion). Machine mode
# (#111 B6b): the HKLM entry in the 64-bit view (the run is 64-bit, Assert-MachineContext),
# an uninstall command with -Machine, and the icon through the current junction,
# which never moves.
function Register-Uninstall([string]$Target, [string]$Version) {
    $sizeKb = [int]((Get-ChildItem -LiteralPath $Target -Recurse -File | Measure-Object Length -Sum).Sum / 1KB)
    $script = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $Options.InstallDir 'uninstall.ps1')`""
    if ($Options.Machine) {
        $key = Get-MachineRegKey
        $uninstall = "$script -Machine"
        $icon = Join-Path (Join-Path $Options.InstallDir $JunctionName) "app\unminute.ico"
    } else {
        $key = $RegKey
        $uninstall = "$script -InstallDir `"$($Options.InstallDir)`""
        $icon = Join-Path $Target "app\unminute.ico"
    }
    New-Item -Path $key -Force | Out-Null
    $values = @{
        DisplayName = (Get-VersionAppName $Target); DisplayVersion = $Version; Publisher = "Unminute project"
        InstallLocation = $Options.InstallDir; UninstallString = $uninstall
        QuietUninstallString = "$uninstall -Silent"; EstimatedSize = $sizeKb
        NoModify = 1; NoRepair = 1
    }
    if (Test-Path -LiteralPath $icon) { $values.DisplayIcon = $icon }
    foreach ($k in $values.Keys) {
        $type = if ($values[$k] -is [int]) { "DWord" } else { "String" }
        New-ItemProperty -Path $key -Name $k -Value $values[$k] -PropertyType $type -Force | Out-Null
    }
}

# -- machine mode, shared (#111 B6b) ----------------------------------------------------
# install.ps1 and uninstall.ps1 have this whole section, unchanged
# (tests/test_hardening.py keeps the two equal), and packaging/identity.json mirrors
# its names. Every name stays a literal here: no file is read to decide what is
# installed, checked or removed (the owner's decision B6-10).

# %ProgramFiles%\Unminute, from Windows' known folder in a 64-bit process, never from
# an environment variable; the all-users Start menu folder; the machine-wide
# uninstall entry, in the 64-bit registry view. Each place outside the install
# folder that machine mode writes is set once, here, so the tests can point every
# one at scratch (tests/powershell/Isolation.ps1, scratch_text in
# tests/test_hardening.py).
$MachineFolderName = "Unminute"
$ProgramFilesFolder = [Environment]::GetFolderPath("ProgramFiles")
$CommonProgramsFolder = [Environment]::GetFolderPath("CommonPrograms")
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

# What the run and the computer are. The Pester tests stand in elevation, bitness
# and the folder's permissions as read; the places come from the variables above.
function Test-Elevated {
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Test-64BitProcess { return [Environment]::Is64BitProcess }
function Get-ProgramFilesFolder { return $ProgramFilesFolder }
function Get-CommonProgramsFolder { return $CommonProgramsFolder }
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

# How many folders and files beneath $Root have permissions of their own (never
# walking through a link): every one should only inherit the install folder's. Files
# too: uninstall.ps1, which SYSTEM runs at uninstall, and every kept version's files,
# which the launcher may start (review of 4c0daf2). About 10 seconds a kept version
# (27,746 files) on a development laptop, measured 23 October 2026.
function Get-AclChangeCount([string]$Root) {
    $count = 0
    $todo = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
    $todo.Push([IO.DirectoryInfo]::new($Root))
    while ($todo.Count) {
        foreach ($item in $todo.Pop().GetFileSystemInfos()) {
            if (Test-Link $item) { continue }
            if (Test-AclChanged $item) { $count++ }
            if ($item -is [IO.DirectoryInfo]) { $todo.Push($item) }
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

# -- machine mode: the install (#111 B6b) ---------------------------------------------

# Unminute installed for everyone on this computer: the machine uninstall entry
# ($MachineRegKey) in the 64-bit view of HKLM, read through RegistryView.Registry64
# so a 32-bit Windows PowerShell sees it too (#111 B6-1). Read only. The tests point
# $MachineRegKey at a throwaway key in HKCU.
function Test-MachineInstalled {
    $hive, $subKey = $MachineRegKey -split ':\\', 2
    $view = [Microsoft.Win32.RegistryView]::Registry64
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey(@{ HKLM = "LocalMachine"; HKCU = "CurrentUser" }[$hive], $view)
    try {
        $key = $base.OpenSubKey($subKey)
        if ($null -eq $key) { return $false }
        $key.Dispose()
        return $true
    } finally { $base.Dispose() }
}

# A machine install runs in a 64-bit, elevated Windows PowerShell (Intune's System
# context), from a package zip, into Program Files only. Anything else stops it
# with exit 2 before anything is read or written.
function Assert-MachineContext {
    if (-not (Test-64BitProcess)) {
        Stop-Install 2 ("A machine install needs 64-bit Windows PowerShell, so nothing was installed. Start it with " +
            "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe (from a 32-bit host) or " +
            "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe.")
    }
    if (-not (Test-Elevated)) {
        Stop-Install 2 "A machine install needs administrator rights (Intune's System context), so nothing was installed."
    }
    if ($Options.InstallDirGiven) {
        Stop-Install 2 "A machine install always goes to Program Files, so -InstallDir cannot be used with -Machine."
    }
    if ($Options.SelfService) {
        Stop-Install 2 "A machine install is managed by IT, so -SelfService cannot be used with -Machine."
    }
    if ($Options.Rollback -and $Options.RemoveSlot) {
        Stop-Install 2 "Use -Rollback or -RemoveSlot, not both."
    }
    if (($Options.Rollback -or $Options.RemoveSlot) -and $Options.Package) {
        Stop-Install 2 "-Rollback and -RemoveSlot use what is installed, so -Package cannot be used with them."
    }
    if (-not ($Options.Rollback -or $Options.RemoveSlot) -and -not $Options.Package) {
        Stop-Install 2 "A machine install takes a package zip with -Package (and its .sha256 file), never a folder."
    }
}

# The install folder's permissions, at every run before anything is written: the
# folder's own (owner and entries, exactly) and those of every folder and file
# beneath it (only what they inherit). Anything else stops the run with exit 4:
# something with administrator rights changed it, so it is never repaired silently
# (#111 B6-7).
function Assert-MachinePermission([string]$Root) {
    if (-not (Test-MachineRootSecurity (Get-FolderSecurity $Root)) -or (Get-AclChangeCount $Root) -gt 0) {
        Stop-Install 4 $PermissionMessage
    }
}

# The install folder's own permissions only: checked before the run writes its lock
# there. Everything beneath it is checked once the run holds the lock
# (Start-MachineRun), so another run part way through writing a file over in place
# (Set-InheritedOnly) cannot make this one stop with exit 4 (review of a3ab563, N3).
function Assert-MachineRootPermission([string]$Root) {
    if (-not (Test-MachineRootSecurity (Get-FolderSecurity $Root))) { Stop-Install 4 $PermissionMessage }
}

# A file the installer has just written over in the install folder (current.json,
# uninstall.ps1) keeps only what it inherits from the folder: no protected list and
# no entry of its own. On CI's elevated runner (23 October 2026) such a file came
# out with permissions of its own after File.Replace or Copy-Item -Force, and the
# next run's check refused it with exit 4; the cause is not confirmed. Only files
# this run wrote are reset; anything else is checked, never repaired
# (docs/PACKAGING.md, "The folder's permissions").
function Set-InheritedOnly([string]$Path) {
    $file = [IO.FileInfo]::new($Path)
    $security = $file.GetAccessControl([Security.AccessControl.AccessControlSections]::Access)
    $security.SetAccessRuleProtection($false, $false)
    foreach ($rule in @($security.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) {
        [void]$security.RemoveAccessRuleSpecific($rule)
    }
    $file.SetAccessControl($security)
}

# Creates the install folder with its permissions (Get-MachineRootSecurity).
function New-MachineRoot([string]$Root) {
    [void][IO.Directory]::CreateDirectory($Root, (Get-MachineRootSecurity))
}

function Read-InstallRecord([string]$File) {
    if (-not [IO.File]::Exists($File)) { return $null }
    try { return Get-Content -LiteralPath $File -Raw | ConvertFrom-Json } catch { return $null }
}

# Unpacked packages left by a run that stopped (staging-<32 hex digits>): removed,
# never through a link. Only administrators can write in the install folder.
function Remove-StagingLeftover {
    foreach ($dir in [IO.DirectoryInfo]::new($Options.InstallDir).GetDirectories()) {
        if ($dir.Name -cmatch $StagingPattern -and -not (Test-Link $dir)) { Remove-OwnFolder $dir.FullName }
    }
}

# Points the current junction at versions\<Version>: a new junction current.new;
# current renamed to current.old; current.new renamed to current; current.old
# removed as a link. A stop at any step is put right by the next run
# (Repair-CurrentLink), from current.json, which is written after this.
function Set-CurrentLink([string]$Version) {
    $root = $Options.InstallDir
    $new = [IO.Path]::Combine($root, "current.new")
    $old = [IO.Path]::Combine($root, "current.old")
    $current = [IO.Path]::Combine($root, $JunctionName)
    Remove-Junction $new
    Remove-Junction $old
    New-Item -ItemType Junction -Path $new -Target ([IO.Path]::Combine($root, "versions", $Version)) | Out-Null
    $item = [IO.DirectoryInfo]::new($current)
    if ($item.Exists -or [IO.File]::Exists($current)) {
        if (-not (Test-Link $item)) { throw "The launch link is not a link, so it was left in place." }
        [IO.Directory]::Move($current, $old)
    }
    [IO.Directory]::Move($new, $current)
    Remove-Junction $old
}

# A switch that stopped part way: current.new goes, current points at the version
# current.json names (if that version is installed), then current.old goes.
function Repair-CurrentLink {
    $root = $Options.InstallDir
    Remove-Junction ([IO.Path]::Combine($root, "current.new"))
    $record = Read-InstallRecord ([IO.Path]::Combine($root, "current.json"))
    $version = if ($record) { Get-VersionValue $record.version } else { $null }
    if ($version) {
        $slot = [IO.Path]::Combine($root, "versions", $version)
        $target = Get-LinkTarget ([IO.Path]::Combine($root, $JunctionName))
        if ((Test-OwnVersion ([IO.DirectoryInfo]::new($slot))) -and
            -not ($target -and [string]::Equals($target.TrimEnd("\"), $slot, [StringComparison]::OrdinalIgnoreCase))) {
            Set-CurrentLink $version
        }
    }
    Remove-Junction ([IO.Path]::Combine($root, "current.old"))
}

# Unpacks into the install folder (Get-PackageRoot), then moves the package's folder
# to versions\<version>.partial (the same drive: a rename, not a second copy) and
# verifies it there, permissions included (Get-PackageListing). Returns the folder.
function New-MachineStagedCopy([string]$Root, [string]$Version) {
    $versions = [IO.Path]::Combine($Options.InstallDir, "versions")
    [void][IO.Directory]::CreateDirectory($versions)
    $staging = [IO.Path]::Combine($versions, "$Version.partial")
    Remove-OwnFolder $staging
    $script:StagedCopy = $staging
    # A virus scan of the files just unpacked holds them open for a moment (#239).
    Invoke-LockRetry { [IO.Directory]::Move($Root, $staging) }
    Test-PackageFile $staging
    return $staging
}

# Removes a version machine mode no longer keeps: renamed to <version>.retired
# first, so a version removed part way is never taken for an installed one, then
# removed without following links. A rename refused because a file is in use
# throws, and the version stays for the next install to try again.
function Remove-RetiredSlot([string]$Name) {
    $dir = [IO.Path]::Combine($Options.InstallDir, "versions", $Name)
    $retired = "$dir.retired"
    Remove-OwnFolder $retired
    Invoke-LockRetry { [IO.Directory]::Move($dir, $retired) }
    Remove-OwnFolder $retired
}

# The versions installed, with their storage format (null when a version states none).
function Get-InstalledSlot {
    $found = [ordered]@{}
    $versions = [IO.DirectoryInfo]::new([IO.Path]::Combine($Options.InstallDir, "versions"))
    if (-not $versions.Exists) { return $found }
    foreach ($dir in $versions.GetDirectories()) {
        if (Test-OwnVersion $dir) { $found[$dir.Name] = Get-VersionSchema $dir.FullName }
    }
    return $found
}

# Each version's seq in the record $Was (format 2), as a hashtable.
function Get-SlotSeq($Was) {
    $seq = @{}
    if ($Was -and ($Was.format -is [int] -or $Was.format -is [long]) -and $Was.format -eq 2 -and
        $Was.slots -is [Management.Automation.PSCustomObject]) {
        foreach ($p in $Was.slots.PSObject.Properties) {
            $value = $p.Value.seq
            if (($value -is [int] -or $value -is [long]) -and $value -gt 0) { $seq[$p.Name] = [long]$value }
        }
    }
    return $seq
}

# Makes $Version current in current.json (format 2, "mode": "machine") and keeps
# (the owner's decisions of 21 and 22 October 2026, "bounded disk"):
#   - the current version;
#   - the previous one ($Previous), unless its storage format is newer;
#   - the newest version (highest seq) of the storage format immediately before the
#     current one: exactly the current format minus one (the owner's decision of 22
#     October 2026, confirmed 23 October). When no version of that format is
#     installed, none is kept in its place: an older format is not a fallback. The
#     launcher hands a workspace that still holds meetings in that format to it
#     (workspace/launch.py); it is replaced at the next storage-format change.
# A downgrade is strict: every version with a newer storage format than the current
# one is removed, and people whose workspace it already changed are told to ask IT.
# Every other version is removed (Remove-RetiredSlot), only versions the installer
# made and never through a link; working folders are removed; anything else is left
# and counted, without names (P3). $Fresh: the version installed now (its installed
# time changes).
function Set-MachineVersion([string]$Version, [string]$Previous, [string]$Fresh) {
    $versions = [IO.Path]::Combine($Options.InstallDir, "versions")
    $currentFile = [IO.Path]::Combine($Options.InstallDir, "current.json")
    $was = Read-InstallRecord $currentFile
    $seq = Get-SlotSeq $was
    $slots = Get-InstalledSlot
    $schema = $slots[$Version]
    $kept = @($Version)
    # The previous version: $Previous, or, when that one goes (a newer storage
    # format), the one the record already kept as previous.
    $candidates = @($Previous)
    if ($was) { $candidates += Get-VersionValue $was.previous }
    $Previous = $null
    foreach ($name in $candidates) {
        if ($name -and $name -cne $Version -and $slots.Contains($name) -and
            -not ($schema -and $slots[$name] -and $slots[$name] -gt $schema)) {
            $Previous = $name
            $kept += $name
            break
        }
    }
    $older = @($slots.Keys | Where-Object { $_ -cne $Version -and $schema -and $slots[$_] -and $slots[$_] -eq $schema - 1 })
    if ($older.Count) {
        $newest = $older | Sort-Object { if ($seq.ContainsKey($_)) { $seq[$_] } else { [long]0 } } | Select-Object -Last 1
        if ($kept -cnotcontains $newest) { $kept += $newest }
    }
    # Oldest first, so a version the record does not list yet (the new one) is
    # numbered after the others.
    $order = @($kept | Sort-Object { if ($seq.ContainsKey($_)) { $seq[$_] } else { [long]::MaxValue } })
    $record = Get-SlotRecord $was $order $Fresh
    Write-CurrentRecord $currentFile $Version ([IO.Path]::Combine($versions, $Version)) $Previous $true $record
    $left = 0; $busy = 0
    foreach ($dir in [IO.DirectoryInfo]::new($versions).GetDirectories()) {
        if ($kept -ccontains $dir.Name) { continue }
        if (Test-OwnVersion $dir) {
            try { Remove-RetiredSlot $dir.Name } catch { $busy++ }
        } elseif (Test-OwnWorkingFolder $dir) {
            try { Remove-OwnFolder $dir.FullName } catch { $busy++ }
        } else { $left++ }
    }
    if ($busy) { Write-Warn "$busy earlier version(s) could not be removed now; the next install tries again." }
    if ($left) { Write-Warn "$left folder(s) in the versions folder were not made by $AppName, so they were left in place." }
}

# The Event Log source (#111 C5, B6-6): the Application log's key for Unminute,
# naming .NET's EventLogMessages.dll, so Event Viewer shows each entry's text.
# A failure only warns: the app writes its entries either way.
function Register-EventSource {
    try {
        $file = Get-EventMessageFile
        if (-not $file) { Write-Warn "The Event Log message file was not found, so the Event Log source was not registered."; return }
        $key = Get-EventSourceKey
        New-Item -Path $key -Force | Out-Null
        New-ItemProperty -Path $key -Name EventMessageFile -Value $file -PropertyType ExpandString -Force | Out-Null
    } catch { Write-Warn "The Event Log source could not be registered." }
}

# The Start menu entry for all users (through the current junction, so pins made
# from it keep working across updates and SYSTEM never writes in a profile), the
# HKLM uninstall entry and the Event Log source.
function Register-Machine([string]$Target, [string]$Version) {
    if (-not $Options.SkipShortcut) {
        Save-AppShortcut (Join-Path (Get-CommonProgramsFolder) "$AppName.lnk") ([IO.Path]::Combine($Options.InstallDir, $JunctionName))
    }
    Register-Uninstall $Target $Version
    Register-EventSource
}

# The run's own temporary folder (review of a3ab563, L1): a new staging-<32 hex
# digits> folder inside the install folder, which only administrators can change,
# with the folder's permissions. TMP and TEMP point at it for the rest of the run, so
# whatever the run puts in a temporary folder (the C# compiler that Add-Type runs for
# the shortcut's taskbar identity, Set-ShortcutAppId, writes its source and the
# compiled library there, and the library is then loaded) goes there and never into
# SYSTEM's temporary folder, C:\Windows\Temp. Removed when the run ends
# (Clear-InstallRun), and otherwise by the next run (Remove-StagingLeftover) or the
# uninstall.
function Enter-MachineTemp {
    $dir = [IO.Path]::Combine($Options.InstallDir, "staging-" + (New-RandomName))
    if ([IO.Directory]::Exists($dir) -or [IO.File]::Exists($dir)) {
        Stop-Install 1 "The temporary folder for this run already exists, so nothing was installed. Run this again."
    }
    [void][IO.Directory]::CreateDirectory($dir)
    $script:MachineTemp = @{ Path = $dir; TMP = $env:TMP; TEMP = $env:TEMP }
    $env:TMP = $dir
    $env:TEMP = $dir
}

# TMP and TEMP as they were, and the run's temporary folder removed (never through
# a link; anything still in use stays for the next run).
function Exit-MachineTemp {
    if (-not $script:MachineTemp) { return }
    $env:TMP = $script:MachineTemp.TMP
    $env:TEMP = $script:MachineTemp.TEMP
    try { Remove-OwnFolder $script:MachineTemp.Path } catch { }
    $script:MachineTemp = $null
}

# From here the run writes in the install folder. First the lock, so one run at a
# time; then, under it, the permissions of everything beneath the folder (before
# anything else is written); then the log, leftovers removed, the run's temporary
# folder, and a switch that stopped part way put right. Nothing changes while the
# app runs for anyone (exit 3).
function Start-MachineRun([string]$Step) {
    Enter-InstallLock
    Assert-MachinePermission $Options.InstallDir
    Start-InstallRun $Step
    Test-AppRunning
    Remove-StagingLeftover
    Enter-MachineTemp
    Repair-CurrentLink
}

function Invoke-MachineInstall {
    Assert-MachineContext
    $root = Get-MachineRoot
    if (-not $root) { Stop-Install 2 "The Program Files folder could not be found, so nothing was installed." }
    $Options.InstallDir = $root
    $Options.Managed = $true
    $folder = [IO.DirectoryInfo]::new($root)
    if ([IO.File]::Exists($root) -or ($folder.Exists -and (Test-Link $folder))) { Stop-Install 4 $PermissionMessage }
    # The folder's own permissions now; everything beneath it under the lock
    # (Start-MachineRun), before anything else is written.
    if ($folder.Exists) { Assert-MachineRootPermission $root }
    Assert-InstallFolder $root
    if ($Options.Rollback) { Invoke-MachineRollback; return }
    if ($Options.RemoveSlot) { Invoke-MachineRemoveSlot; return }
    Test-Prerequisite
    if (-not $folder.Exists) {
        New-MachineRoot $root
        Assert-MachineRootPermission $root
    }
    Start-MachineRun "start"
    $package = Get-PackageRoot
    # Before anything in the package is read or moved: no link or junction in it.
    Assert-NoLink $package
    $version = [string](Get-Content -LiteralPath (Join-Path $package "version.json") -Raw | ConvertFrom-Json).version
    if (-not (Test-VersionName $version)) {
        Stop-Install 4 "The package's version.json does not hold a valid version, so nothing was installed."
    }
    $script:LogVersion = $version
    if (-not (Get-VersionSchema $package)) {
        Stop-Install 2 "This package does not state its storage format, so it cannot be installed for everyone on this computer. Use a newer package."
    }
    Write-Step "Installing $AppName $version for everyone on this computer..."
    Set-InstallStep "copy"
    $target = [IO.Path]::Combine($root, "versions", $version)
    if ((Test-OwnVersion ([IO.DirectoryInfo]::new($target))) -and -not (Get-PackageProblem $target)) {
        # The same version, intact: nothing is copied, the rest is registered again.
        Copy-Item -LiteralPath (Join-Path $target "uninstall.ps1") -Destination (Join-Path $root "uninstall.ps1") -Force
        Set-InheritedOnly (Join-Path $root "uninstall.ps1")
    } else {
        $staging = New-MachineStagedCopy $package $version
        Set-InstallStep "switch"
        $target = Install-Version $staging $version
    }
    Set-InstallStep "switch"
    $was = Read-InstallRecord ([IO.Path]::Combine($root, "current.json"))
    $wasVersion = if ($was) { Get-VersionValue $was.version } else { $null }
    $previous = if ($wasVersion -and $wasVersion -ceq $version) { Get-VersionValue $was.previous } else { $wasVersion }
    Set-CurrentLink $version
    Set-MachineVersion $version $previous $version
    Set-InstallStep "register"
    Register-Machine $target $version
    $script:LogStep = "done"
    Write-InstallLog "ok" 0
    Write-Step "$AppName $version is installed for everyone on this computer."
}

# -Machine -Rollback: the previous version becomes current again if it still
# matches its manifest. No Python runs (each user's start decides about their
# workspace, workspace/launch.py); a newer storage format left behind is removed.
function Invoke-MachineRollback {
    $currentFile = [IO.Path]::Combine($Options.InstallDir, "current.json")
    if (-not [IO.File]::Exists($currentFile)) { Stop-Install 1 "$AppName is not installed for everyone on this computer." }
    Start-MachineRun "rollback"
    $record = Read-InstallRecord $currentFile
    $previous = if ($record) { Get-VersionValue $record.previous } else { $null }
    $version = if ($record) { Get-VersionValue $record.version } else { $null }
    if (-not $previous) { Stop-Install 1 "There is no previous version to go back to." }
    if (-not $version) { Stop-Install 1 "The install record does not name a valid current version, so there is nothing to go back from." }
    $target = [IO.Path]::Combine($Options.InstallDir, "versions", $previous)
    if (-not (Test-OwnVersion ([IO.DirectoryInfo]::new($target)))) { Stop-Install 1 "The previous version is no longer installed." }
    $script:LogVersion = $previous
    Test-PackageFile $target
    Copy-Item -LiteralPath (Join-Path $target "uninstall.ps1") -Destination (Join-Path $Options.InstallDir "uninstall.ps1") -Force
    Set-InheritedOnly (Join-Path $Options.InstallDir "uninstall.ps1")
    Set-CurrentLink $previous
    Set-MachineVersion $previous $version $null
    Register-Machine $target $previous
    $script:LogStep = "done"
    Write-InstallLog "ok" 0
    Write-Step "$AppName is back on version $previous for everyone on this computer."
}

# -Machine -RemoveSlot <version>: IT removes a kept version, never the current one.
function Invoke-MachineRemoveSlot {
    $name = [string]$Options.RemoveSlot
    if (-not (Test-VersionName $name)) { Stop-Install 2 "-RemoveSlot needs a version name, such as 0.6.1." }
    $currentFile = [IO.Path]::Combine($Options.InstallDir, "current.json")
    if (-not [IO.File]::Exists($currentFile)) { Stop-Install 1 "$AppName is not installed for everyone on this computer." }
    Start-MachineRun "remove-slot"
    $record = Read-InstallRecord $currentFile
    $version = if ($record) { Get-VersionValue $record.version } else { $null }
    if (-not $version) { Stop-Install 1 "The install record does not name a valid current version, so nothing was removed." }
    if ($name -ceq $version) { Stop-Install 2 "Version $name is the current version, so it was not removed." }
    if (-not (Test-OwnVersion ([IO.DirectoryInfo]::new([IO.Path]::Combine($Options.InstallDir, "versions", $name))))) {
        Stop-Install 1 "Version $name is not installed."
    }
    $script:LogVersion = $name
    $previous = Get-VersionValue $record.previous
    if ($previous -ceq $name) { $previous = $null }
    $seq = Get-SlotSeq $record
    $order = @($seq.Keys | Where-Object { $_ -cne $name } | Sort-Object { $seq[$_] })
    if ($order -cnotcontains $version) { $order += $version }
    $slots = Get-SlotRecord $record $order $null
    Write-CurrentRecord $currentFile $version ([IO.Path]::Combine($Options.InstallDir, "versions", $version)) $previous $true $slots
    try { Remove-RetiredSlot $name }
    catch {
        if (Test-TransientLock $_.Exception) { Stop-Install 3 "A file of version $name is in use, so it was not removed yet. Run this again later." }
        throw
    }
    $script:LogStep = "done"
    Write-InstallLog "ok" 0
    Write-Step "Version $name was removed."
}

# -- main ---------------------------------------------------------------------------

# From here the install folder is the installer's own (Assert-InstallFolder) and
# the run writes in it: one run at a time, a record left half-written by a stopped
# run removed, and the log started (#111 B6a). Until then nothing is written there.
function Start-InstallRun([string]$Step) {
    [void][IO.Directory]::CreateDirectory($Options.InstallDir)
    Enter-InstallLock
    Remove-LeftoverRecord
    $logs = [IO.Directory]::CreateDirectory([IO.Path]::Combine($Options.InstallDir, "logs"))
    $script:LogFile = [IO.Path]::Combine($logs.FullName, $LogNames[0])
    Set-InstallStep $Step
}

function Invoke-Rollback {
    $currentFile = Join-Path $Options.InstallDir "current.json"
    if (-not (Test-Path -LiteralPath $currentFile)) { Stop-Install 1 "$AppName is not installed." }
    Start-InstallRun "rollback"
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
    # Format 2 (#111 B5): both versions keep their seq and installed time.
    $slots = Get-SlotRecord $current @($previous, $version) $null
    Write-CurrentRecord $currentFile $previous $target $version ([bool]$current.managed) $slots
    New-Shortcut $target
    Register-Uninstall $target $previous
    $script:LogVersion = $previous
    $script:LogStep = "done"
    Write-InstallLog "ok" 0
    Write-Step "$(Get-VersionAppName $target) is back on version $previous."
    exit 0
}

# Whatever a run leaves when it ends, finished or not: a copy that did not become
# the installed version and the unpacked package are removed (never through a
# link), and the lock is let go (#111 B6a).
function Clear-InstallRun {
    if ($script:StagedCopy) { try { Remove-OwnFolder $script:StagedCopy } catch { } }
    $script:StagedCopy = $null
    if ($script:TempExtract) { try { Remove-OwnFolder $script:TempExtract } catch { } }
    $script:TempExtract = $null
    Exit-MachineTemp
    Exit-InstallLock
}

# Dot-sourced (the Pester tests in tests/powershell): the functions above are defined
# and nothing runs.
if ($MyInvocation.InvocationName -eq ".") { return }

# Whether -InstallDir was given (machine mode refuses it, #111 B6b). Read here, after
# the dot-source guard, because nothing above the guard may call a method (#332 review).
$Options.InstallDirGiven = $PSBoundParameters.ContainsKey("InstallDir")

try {
    # Machine mode (#111 B6b) has its own checks and folder, and never runs Python.
    if ($Options.Machine) {
        Invoke-MachineInstall
        exit 0
    }
    if ($Options.RemoveSlot) { Stop-Install 2 "-RemoveSlot is for a machine install (-Machine) only." }
    # Not beside a machine install (#111 B6-1): one computer has one Unminute. A
    # failure to read the registry does not stop the install (this is a convenience,
    # not a control: an older package's installer has no such check).
    $everyone = $false
    try { $everyone = Test-MachineInstalled } catch { }
    if ($everyone) {
        Stop-Install 2 ("$AppName is already installed for everyone on this computer by your IT team, so it " +
            "was not installed for you as well. Open it from the Start menu.")
    }
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
    # Before anything in the package is read or copied: no link or junction in it.
    Assert-NoLink $root
    $version = [string](Get-Content -LiteralPath (Join-Path $root "version.json") -Raw | ConvertFrom-Json).version
    if (-not (Test-VersionName $version)) {
        Stop-Install 4 "The package's version.json does not hold a valid version, so nothing was installed."
    }
    $script:LogVersion = $version
    Start-InstallRun "start"
    Write-Step "Installing $AppName $version for $env:USERNAME..."
    Test-AppRunning
    # Copy, then verify the copy; then the copy's own runtime checks the workspace
    # (never from %TEMP% or the package's unpacked folder); then the swap (#111 B6a).
    Set-InstallStep "copy"
    $staging = New-StagedCopy $root $version
    Set-InstallStep "gate"
    Test-WorkspaceGate $staging
    Set-InstallStep "switch"
    $target = Install-Version $staging $version
    Set-CurrentVersion $target $version
    Set-InstallStep "register"
    New-Shortcut $target
    Register-Uninstall $target $version
    $script:LogStep = "done"
    Write-InstallLog "ok" 0
    Write-Step "$AppName $version is installed. Open it from the Start menu and unlimit your meeting minutes."
    Write-Step "Your workspace and downloaded models are kept separately and are not affected by updates or uninstalling."
    exit 0
} catch {
    # The log keeps the error's type only; the screen shows its message, as before.
    $script:LogError = $_.Exception.GetType().Name
    Stop-Install 1 "Installation failed: $($_.Exception.Message)"
} finally {
    # A copy that did not become the installed version is removed (never through a
    # link), so a failed run changes nothing else (#111 B6a).
    Clear-InstallRun
}
