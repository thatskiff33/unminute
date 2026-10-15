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

  Exit codes: 0 installed, 1 failed, 2 prerequisites missing,
  3 the app is running, 4 the package failed verification,
  5 the update waits for the workspace (see the message), 6 this version cannot
  open the existing workspace (it was created by a newer version).

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
# Every Start menu / Settings > Apps name a version has used. Each version records its
# own in version.json ("name"); versions from before that used "Meeting Workspace".
$KnownAppNames = @("Unminute", "Meeting Workspace")
$NameBeforeRecorded = "Meeting Workspace"
$script:TempExtract = $null
$RegKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\WhisperWorkspace"
# Must match APP_ID in workspace/__main__.py (taskbar grouping and icon). Kept from the
# earlier name on purpose: changing it would split existing taskbar pins.
$AppId = "WhisperTranscriber.MeetingWorkspace"
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

# -- update gate (#26) ----------------------------------------------------------------

function Test-WorkspaceGate([string]$Root, [string]$Checker = "", [int]$Schema = 0) {
    # Normally run by the version that will take over the workspace, which
    # knows its own schema. For an older version that cannot check itself,
    # $Checker (a newer runtime) checks on its behalf with -Schema.
    $base = if ($Checker) { $Checker } else { $Root }
    $python = Join-Path $base "runtime\python.exe"
    $gateArgs = @("-B", "-m", "workspace", "--gate")      # -B: write nothing into the package
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

function Install-Version([string]$Root, [string]$Version) {
    $versions = Join-Path $Options.InstallDir "versions"
    $target = Join-Path $versions $Version
    New-Item -ItemType Directory -Force -Path $versions | Out-Null
    $staging = "$target.partial"
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    Write-Step "Copying files..."
    Copy-Item -LiteralPath $Root -Destination $staging -Recurse
    # Files extracted by Explorer carry the "downloaded from the internet" mark;
    # the package was just verified, so clear it on the executable files.
    Get-ChildItem -LiteralPath $staging -Recurse -File -Include *.exe, *.dll, *.pyd, *.ps1 |
        Unblock-File -ErrorAction SilentlyContinue
    # Same version again: move the live copy aside first, so a locked file can
    # never leave a half-deleted version in use.
    $old = "$target.old"
    if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }
    if (Test-Path -LiteralPath $target) { Rename-Item -LiteralPath $target -NewName (Split-Path $old -Leaf) }
    Rename-Item -LiteralPath $staging -NewName (Split-Path $target -Leaf)
    if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue }
    Copy-Item -LiteralPath (Join-Path $target "uninstall.ps1") -Destination (Join-Path $Options.InstallDir "uninstall.ps1") -Force
    return $target
}

function Set-CurrentVersion([string]$Target, [string]$Version) {
    $currentFile = Join-Path $Options.InstallDir "current.json"
    $previous = $null
    if (Test-Path -LiteralPath $currentFile) {
        $was = Get-Content -LiteralPath $currentFile -Raw | ConvertFrom-Json
        # Reinstalling the same version keeps the existing rollback version.
        $previous = if ($was.version -eq $Version) { $was.previous } else { $was.version }
    }
    $managed = [bool]$Options.Managed
    if (-not $managed -and -not $Options.SelfService -and (Test-Path -LiteralPath $currentFile)) {
        # An IT-managed installation stays managed across updates.
        $managed = [bool](Get-Content -LiteralPath $currentFile -Raw | ConvertFrom-Json).managed
    }
    @{ version = $Version; path = $Target; previous = $previous; managed = $managed } |
        ConvertTo-Json | Set-Content -LiteralPath $currentFile -Encoding UTF8
    # Keep the current and the previous version (for rollback, #26); remove older ones.
    Get-ChildItem -LiteralPath (Join-Path $Options.InstallDir "versions") -Directory | Where-Object {
        $_.Name -ne $Version -and $_.Name -ne $previous -and $_.Name -notlike "*.partial" -and $_.Name -notlike "*.old"
    } | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
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
    return $NameBeforeRecorded
}

function New-Shortcut([string]$Target) {
    if ($Options.SkipShortcut) { return }
    $name = Get-VersionAppName $Target
    $programs = [Environment]::GetFolderPath("Programs")
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Join-Path $programs "$name.lnk"))
    $link.TargetPath = Join-Path $Target "runtime\pythonw.exe"
    $link.Arguments = "-m workspace"
    $link.WorkingDirectory = Join-Path $Target "app"
    $link.Description = "Transcribe and review meetings on this computer"
    $icon = Join-Path $Target "app\whisper.ico"
    if (Test-Path -LiteralPath $icon) { $link.IconLocation = "$icon,0" }
    $link.Save()
    try { Set-ShortcutAppId $link.FullName $AppId }
    catch { Write-Warning "Could not set the taskbar identity on the shortcut: $($_.Exception.Message)" }
    Remove-OtherShortcut $programs $shell $name
}

# Remove Start menu shortcuts this app made under its other names (after a rename or a
# rollback), but only ones that point into this install: a same-named shortcut to
# anything else is not ours and is left alone. Failures only warn.
function Remove-OtherShortcut([string]$Programs, $Shell, [string]$Keep) {
    # Long form: a .lnk stores its target as a long path, so a short (8.3) InstallDir
    # would otherwise never match.
    $dir = $Options.InstallDir
    try { $dir = (Get-Item -LiteralPath $dir -ErrorAction Stop).FullName } catch { }
    $prefix = $dir.TrimEnd("\") + "\"
    foreach ($name in $KnownAppNames) {
        if ($name -eq $Keep) { continue }
        $path = Join-Path $Programs "$name.lnk"
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $sc = $null
        try {
            $sc = $Shell.CreateShortcut($path)
            $old = $sc.TargetPath
            # Release the shortcut object before deleting: while alive it keeps the .lnk open.
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($sc); $sc = $null
            if ($old -and $old.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                Remove-Item -LiteralPath $path -Force
            }
        } catch { Write-Warning "Could not remove the old shortcut $($name): $($_.Exception.Message)" }
        finally { if ($sc) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($sc) } }
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
    $icon = Join-Path $Target "app\whisper.ico"
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
    if (-not $current.previous) { Stop-Install 1 "There is no previous version to go back to." }
    $target = Join-Path $Options.InstallDir "versions\$($current.previous)"
    if (-not (Test-Path -LiteralPath $target)) { Stop-Install 1 "The previous version is no longer installed." }
    Test-AppRunning
    # Can the older version open this workspace? Versions from before the update
    # gate cannot answer themselves; they all used schema 1.
    $info = Get-Content -LiteralPath (Join-Path $target "version.json") -Raw | ConvertFrom-Json
    if ($info.features -contains "gate") {
        Test-WorkspaceGate $target
    } else {
        Test-WorkspaceGate $target -Checker (Join-Path $Options.InstallDir "versions\$($current.version)") -Schema 1
    }
    Copy-Item -LiteralPath (Join-Path $target "uninstall.ps1") -Destination (Join-Path $Options.InstallDir "uninstall.ps1") -Force
    @{ version = $current.previous; path = $target; previous = $current.version
       managed = [bool]$current.managed } | ConvertTo-Json |
        Set-Content -LiteralPath $currentFile -Encoding UTF8
    New-Shortcut $target
    Register-Uninstall $target $current.previous
    Write-Step "$(Get-VersionAppName $target) is back on version $($current.previous)."
    exit 0
}

try {
    if ($Options.Rollback) { Invoke-Rollback }
    Test-Prerequisite
    $root = Get-PackageRoot
    $version = (Get-Content -LiteralPath (Join-Path $root "version.json") -Raw | ConvertFrom-Json).version
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
