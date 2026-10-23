<#
.SYNOPSIS
  Removes Unminute for the current Windows user (#11).

.DESCRIPTION
  Removes the installed program, its Start menu shortcut and its uninstall
  entry. It removes only what the installer put in the install folder
  (versions, current.json and its temporary copy, the logs folder with the
  installer's log, install.lock and this script; #111 B6a); if the folder holds
  anything else, or is neither an install nor the folder the uninstall entry
  names, it removes nothing. It holds install.lock while it removes, so it never
  runs beside an install or update. It never removes through a link or junction: one at
  any of those entries stops it before anything is removed,
  and one inside a version is removed as a link, leaving what it points to
  (#297). When the uninstall entry names another folder that still
  holds an install, the shortcut and the entry are that install's, and are
  kept. It does NOT remove the workspace (meetings, holds, settings) or the
  downloaded models: meeting content is only removed through the app's own
  confirmed cleanup, which honours retention holds. If meetings remain, this
  says so.

  Before removing anything, it puts back the Windows lid setting if the app
  left it at "Do nothing" (#191): see Restore-LidSetting. That step never stops
  the uninstall.

  Exit codes: 0 removed, 1 failed, 3 the app, or an install or update, is running.

  MACHINE MODE (-Machine, #111 B6b): removes the install for everyone on this
  computer from %ProgramFiles%\Unminute, run by IT as SYSTEM in a 64-bit,
  elevated Windows PowerShell. It checks the folder's permissions as the
  installer does, refuses while the app runs for anyone on the computer, and
  removes only the installer's own entries (the versions, the current junction
  as a link, current.json, the logs, install.lock, staging folders and this
  script), the Start menu entry for all users, the Event Log source and, last,
  the machine-wide uninstall entry. While a version kept for people whose workspace has
  not moved to the current storage format is installed, it removes nothing
  unless -IncludeKeptSlots is given (exit 5), so a misconfigured "uninstall
  previous version" in Intune fails safe (#111 B6-11). It never reads or writes
  a user's profile: workspaces, models, tokens and the lid setting are left as
  they are (#111 B6-9).
  Machine exit codes: 0 removed, 1 failed (including anything in the folder that
  the installer did not put there), 2 not allowed (a 32-bit or non-elevated run,
  or -InstallDir with -Machine), 3 the app, or an install or update, is running,
  4 the install folder's permissions have changed, 5 a version kept for an
  earlier storage format is still installed.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Unminute"),
    [switch]$Silent,
    [switch]$Machine,
    [switch]$IncludeKeptSlots
)

$ErrorActionPreference = "Stop"
# Parameters, read by the functions below.
$Options = @{ InstallDir = $InstallDir; Silent = [bool]$Silent; Machine = [bool]$Machine
             IncludeKeptSlots = [bool]$IncludeKeptSlots; InstallDirGiven = $PSBoundParameters.ContainsKey("InstallDir") }
$AppName = "Unminute"
$RegKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Unminute"
# What the installer may put in the install folder, and so all this removes there
# (#297, #111 B6a). install.ps1 and uninstall.ps1 list the same names, and
# packaging/identity.json mirrors them (tests keep the three equal).
$OwnEntries = @("versions", "current.json", "current.json.tmp", "uninstall.ps1", "logs", "install.lock")
$LockName = "install.lock"
$LogNames = @("install.log", "install.log.1")
# How the installer starts the app (install.ps1 has the same; tests keep them equal):
# isolated, no bytecode written, no remote debugging (#111 B4).
$LaunchArgs = "-I -B -X disable-remote-debug -m workspace"
$script:InstallLock = $null
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
# current.json (never a path; \z, unlike $, refuses a final newline), and a
# version.json that lists the feature, so an older version never takes the switch
# for an ordinary start and opens its window.
function Get-LidRestoreVersion {
    $currentFile = [IO.Path]::Combine($Options.InstallDir, "current.json")
    if (-not (Test-Path -LiteralPath $currentFile -PathType Leaf)) { return $null }
    $version = [string](Get-Content -LiteralPath $currentFile -Raw | ConvertFrom-Json).version
    if ($version -cnotmatch '^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\z') { return $null }
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
    $start.Arguments = "$LaunchArgs --restore-lid"
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

# The number of entries in the logs folder that are not the installer's log files
# (install.log, install.log.1) or are links (#111 B6a).
function Get-LogStray([string]$Dir) {
    $logs = [IO.DirectoryInfo]::new([IO.Path]::Combine($Dir, "logs"))
    if (-not $logs.Exists -or (Test-Link $logs)) { return 0 }
    return @($logs.GetFileSystemInfos() | Where-Object {
        $_ -is [IO.DirectoryInfo] -or (Test-Link $_) -or $LogNames -notcontains $_.Name }).Count
}

# install.lock, as the installer holds it (#111 B6a): no sharing, deleted when closed.
# Held by an install or update now: stop with exit 3 before anything is removed.
function Enter-InstallLock {
    try {
        $script:InstallLock = [IO.FileStream]::new([IO.Path]::Combine($Options.InstallDir, $LockName),
            [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None, 1,
            [IO.FileOptions]::DeleteOnClose)
    } catch {
        if (-not (Test-TransientLock $_.Exception)) { throw }
        Stop-Uninstall 3 "An install or update of $AppName is running. Run this again when it has finished."
    }
}

function Exit-InstallLock {
    if ($script:InstallLock) {
        $script:InstallLock.Dispose()
        $script:InstallLock = $null
    }
}

# -- machine mode, shared (#111 B6b) ----------------------------------------------------
# install.ps1 and uninstall.ps1 have this whole section, unchanged
# (tests/test_hardening.py keeps the two equal), and packaging/identity.json mirrors
# its names. Every name stays a literal here: no file is read to decide what is
# installed, checked or removed (the owner's decision B6-10).

# %ProgramFiles%\Unminute, from Windows' known folder in a 64-bit process, never from
# an environment variable; its machine-wide uninstall entry, in the 64-bit
# registry view.
$MachineFolderName = "Unminute"
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

# What the run and the computer are. The Pester tests stand these in.
function Test-Elevated {
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Test-64BitProcess { return [Environment]::Is64BitProcess }
function Get-ProgramFilesFolder { return [Environment]::GetFolderPath("ProgramFiles") }
function Get-CommonProgramsFolder { return [Environment]::GetFolderPath("CommonPrograms") }
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

# -- machine mode: the uninstall (#111 B6b) ---------------------------------------------

# How many versions current.json keeps beside the current one with an earlier
# storage format: the version kept for people whose workspace has not moved to the
# current format yet. Only versions the record keeps (its slots and previous) count,
# so a version an earlier install could not remove does not (review of 4c0daf2).
# Each format is read from the version's own version.json; none when the current
# version or its format cannot be told.
function Get-KeptOlderSlotCount([string]$Root) {
    $currentFile = [IO.Path]::Combine($Root, "current.json")
    if (-not [IO.File]::Exists($currentFile)) { return 0 }
    try { $record = Get-Content -LiteralPath $currentFile -Raw | ConvertFrom-Json } catch { return 0 }
    $current = [string]$record.version
    $listed = @([string]$record.previous)
    if ($record.slots -is [Management.Automation.PSCustomObject]) { $listed += @($record.slots.PSObject.Properties.Name) }
    $versions = [IO.DirectoryInfo]::new([IO.Path]::Combine($Root, "versions"))
    if (-not $versions.Exists) { return 0 }
    $schemas = @{}
    foreach ($dir in $versions.GetDirectories()) {
        if ((Test-Link $dir) -or -not ($dir.Name -cmatch '^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\z') -or
            $dir.Name.EndsWith(".") -or $dir.Name -match '\.(partial|old|retired)\z') { continue }
        if ($dir.Name -cne $current -and $listed -cnotcontains $dir.Name) { continue }
        try { $info = Get-Content -LiteralPath ([IO.Path]::Combine($dir.FullName, "version.json")) -Raw | ConvertFrom-Json }
        catch { continue }
        $schema = $info.schema
        if (([string]$info.version) -ceq $dir.Name -and ($schema -is [int] -or $schema -is [long]) -and $schema -gt 0) {
            $schemas[$dir.Name] = [long]$schema
        }
    }
    if (-not $schemas.ContainsKey($current)) { return 0 }
    return @($schemas.Keys | Where-Object { $_ -cne $current -and $schemas[$_] -lt $schemas[$current] }).Count
}

# Removes the Event Log source the install registered (the owner's decision of 21
# October 2026). A failure only warns.
function Unregister-EventSource {
    try {
        $key = Get-EventSourceKey
        if (Test-Path -LiteralPath $key) { Remove-Item -LiteralPath $key -Recurse -Force }
    } catch { Write-Warn "The Event Log source could not be removed." }
}

function Invoke-MachineUninstall {
    if (-not (Test-64BitProcess)) {
        Stop-Uninstall 2 ("A machine uninstall needs 64-bit Windows PowerShell, so nothing was removed. Start it with " +
            "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe (from a 32-bit host) or " +
            "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe.")
    }
    if (-not (Test-Elevated)) {
        Stop-Uninstall 2 "A machine uninstall needs administrator rights (Intune's System context), so nothing was removed."
    }
    if ($Options.InstallDirGiven) {
        Stop-Uninstall 2 "A machine install is always in Program Files, so -InstallDir cannot be used with -Machine."
    }
    $root = Get-MachineRoot
    if (-not $root) { Stop-Uninstall 1 "The Program Files folder could not be found, so nothing was removed." }
    $Options.InstallDir = $root
    $regKey = Get-MachineRegKey
    $link = Join-Path (Get-CommonProgramsFolder) "$AppName.lnk"
    $folder = [IO.DirectoryInfo]::new($root)
    if ([IO.File]::Exists($root) -or ($folder.Exists -and (Test-Link $folder))) { Stop-Uninstall 4 $PermissionMessage }
    if (-not $folder.Exists -and -not (Test-Path -LiteralPath $regKey)) {
        Stop-Uninstall 1 "$AppName is not installed for everyone on this computer, so nothing was removed."
    }
    if ($folder.Exists) {
        # The installer's checks: the folder's own permissions, exactly, and nothing
        # of their own on any folder or file beneath it.
        if (-not (Test-MachineRootSecurity (Get-FolderSecurity $root)) -or (Get-AclChangeCount $root) -gt 0) {
            Stop-Uninstall 4 $PermissionMessage
        }
        $running = Get-Process -ErrorAction SilentlyContinue | Where-Object {
            try { $_.Path -and $_.Path.StartsWith($root.TrimEnd("\") + "\", [StringComparison]::OrdinalIgnoreCase) } catch { $false }
        }
        if ($running) {
            Stop-Uninstall 3 "$AppName is running on this computer, so nothing was removed. Run this again once nobody has it open."
        }
        # Only the installer's own entries; anything else stops it before anything is
        # removed. A link is allowed only as one of the junctions, pointing at a version.
        $strays = 0; $links = 0
        foreach ($item in $folder.GetFileSystemInfos()) {
            $isDir = $item -is [IO.DirectoryInfo]
            if ($JunctionNames -ccontains $item.Name) {
                if (-not (Test-OwnJunction $item $root)) { $links++ }
            } elseif ($item.Name -cmatch $StagingPattern) {
                if (Test-Link $item) { $links++ } elseif (-not $isDir) { $strays++ }
            } elseif ($OwnEntries -cnotcontains $item.Name) {
                $strays++
            } elseif (Test-Link $item) {
                $links++
            } elseif ($isDir -ne (@("versions", "logs") -contains $item.Name)) {
                $strays++
            }
        }
        $strays += Get-LogStray $root
        if ($links) {
            Stop-Uninstall 1 "The install folder holds a link or junction that $AppName did not put there, so nothing was removed."
        }
        if ($strays) {
            Stop-Uninstall 1 "The install folder holds $strays item(s) that $AppName did not put there, so nothing was removed."
        }
        # Not beside an install or update (#111 B6a).
        Enter-InstallLock
        if (-not $Options.IncludeKeptSlots -and (Get-KeptOlderSlotCount $root) -gt 0) {
            Stop-Uninstall 5 ("Older versions are still kept for people whose workspace has not moved to the " +
                "current storage format, so nothing was removed. Check with them first, then remove the kept " +
                "version with install.ps1 -Machine -RemoveSlot, or run this again with -IncludeKeptSlots.")
        }
        # The junctions first, as links, so nothing is removed through them; then
        # the rest, never through a link (Remove-OwnFolder).
        # The launch junction first, so nothing starts the app meanwhile, then the two
        # names a switch of version uses for a moment.
        Remove-Junction ([IO.Path]::Combine($root, $JunctionName))
        foreach ($name in $JunctionNames) { Remove-Junction ([IO.Path]::Combine($root, $name)) }
        foreach ($dir in $folder.GetDirectories()) {
            if ($dir.Name -cmatch $StagingPattern) { Invoke-LockRetry { Remove-OwnFolder $dir.FullName } }
        }
        foreach ($entry in @("versions", "current.json", "current.json.tmp")) {
            $path = [IO.Path]::Combine($root, $entry)
            if ([IO.Directory]::Exists($path)) { Invoke-LockRetry { Remove-OwnFolder $path } }
            elseif ([IO.File]::Exists($path)) { Invoke-LockRetry { Remove-Item -LiteralPath $path -Force } }
        }
        $logs = [IO.Path]::Combine($root, "logs")
        foreach ($name in $LogNames) {
            $path = [IO.Path]::Combine($logs, $name)
            if ([IO.File]::Exists($path)) { Remove-Item -LiteralPath $path -Force }
        }
        if ([IO.Directory]::Exists($logs)) { [IO.Directory]::Delete($logs, $false) }
        Exit-InstallLock                 # closing it deletes install.lock
        try { Remove-OwnFolder $root } catch { }
    }
    if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link -Force }
    Unregister-EventSource
    # Last: while any of it remains, the app still shows as installed (Intune detection).
    if (Test-Path -LiteralPath $regKey) { Remove-Item -LiteralPath $regKey -Recurse -Force }
    Write-Step ("$AppName has been removed for everyone on this computer. Each person's workspace and " +
        "downloaded models are kept in their own profile.")
}

# Dot-sourced (the Pester tests in tests/powershell): the functions above are defined
# and nothing runs.
if ($MyInvocation.InvocationName -eq ".") { return }

# Machine mode (#111 B6b): Program Files, its own checks and entries; never the
# lid restore below, which would read a user's profile as SYSTEM (B6-9).
if ($Options.Machine) {
    try {
        Invoke-MachineUninstall
        exit 0
    } catch {
        Stop-Uninstall 1 "Removal failed: $($_.Exception.Message)"
    } finally {
        Exit-InstallLock
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
    if (Test-Path -LiteralPath $Options.InstallDir) {
        $items = @(Get-ChildItem -LiteralPath $Options.InstallDir -Force)
        $other = @($items | Where-Object { $OwnEntries -notcontains $_.Name })
        # logs is a folder; current.json.tmp and install.lock are files.
        $kind = @($items | Where-Object { @("logs", "current.json.tmp", "install.lock") -contains $_.Name -and
            (($_.Name -eq "logs") -ne ($_ -is [IO.DirectoryInfo])) })
        $strays = $other.Count + $kind.Count + (Get-LogStray $Options.InstallDir)
        if ($strays) {
            Stop-Uninstall 1 "The install folder holds $strays item(s) that $AppName did not put there, so nothing was removed. Check -InstallDir."
        }
        # Nothing is removed through a link or junction, or run from one (#297).
        if (@($items | Where-Object { Test-Link $_ }).Count) {
            Stop-Uninstall 1 "The install folder holds a link or junction that $AppName did not put there, so nothing was removed. Remove it, then run this again."
        }
        # Not beside an install or update (#111 B6a).
        Enter-InstallLock
    }
    # Before anything is removed: the installed app puts the lid setting back (#191).
    Restore-LidSetting
    if (Test-Path -LiteralPath $Options.InstallDir) {
        # The script itself lives in InstallDir: remove the rest first, never through
        # a link (#297): one inside a version is removed as a link.
        foreach ($name in "versions", "current.json", "current.json.tmp") {
            $path = [IO.Path]::Combine($Options.InstallDir, $name)
            if ([IO.Directory]::Exists($path)) { Invoke-LockRetry { Remove-OwnFolder $path } }
            elseif ([IO.File]::Exists($path)) { Invoke-LockRetry { Remove-Item -LiteralPath $path -Force } }
        }
        # The installer's log files, then their folder once empty (#111 B6a).
        $logs = [IO.Path]::Combine($Options.InstallDir, "logs")
        foreach ($name in $LogNames) {
            $path = [IO.Path]::Combine($logs, $name)
            if ([IO.File]::Exists($path)) { Remove-Item -LiteralPath $path -Force }
        }
        if ([IO.Directory]::Exists($logs)) { [IO.Directory]::Delete($logs, $false) }
        Exit-InstallLock                 # closing it deletes install.lock
        try { Remove-OwnFolder $Options.InstallDir } catch { }
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
} finally {
    Exit-InstallLock
}
