<#
.SYNOPSIS
  Installs the speech and speaker models in a model package for everyone on this
  computer, beside a machine install of Unminute (#111 B7a).

.DESCRIPTION
  Run by IT as SYSTEM (an Intune Win32 app, System context) in a 64-bit, elevated
  Windows PowerShell, from the model package folder that tools\intune\New-ModelPackage.ps1
  made. Unminute must already be installed for everyone (install.ps1 -Machine): in
  Intune, make the model package depend on the Unminute app.

  - Checks the install folder (%ProgramFiles%\Unminute) as the installer does: its
    own permissions exactly, and nothing beneath it with permissions of its own
    (exit 4). Takes the installer's install.lock, so it never runs beside an install,
    update or uninstall of the app.
  - Checks the package: models.json, and every model file in it by exact name,
    size and SHA-256 (exit 4). A link anywhere stops it. A model that an installed
    version of the app pins (the same family, folder and revision in its
    model-manifest.json) must list exactly the files that version lists, by name, size
    and SHA-256 (exit 4).
  - For each model, into models\<family>\<folder>\<revision> under the install folder:
    a copy of the same revision already there that matches every name, size and
    SHA-256 is kept and only its record rewritten; otherwise the package's copy goes
    into models\.staging-<32 hex digits>, is checked again, and replaces the old folder
    by two renames. A folder in use (the app may hold a model open) is left as it is
    and the run exits 3 for Intune to try again. Access denied counts as in use only
    while the run can still write beside the folder; otherwise it is a permission
    fault and the run fails (exit 1), rather than Intune trying again without end.
  - Writes models.verified.json in each model folder: repository, revision, folder,
    "ok", and every file's size and modification time (nanoseconds since 1 January
    1970 UTC, as Python's st_mtime_ns). The app trusts a copy only through this record,
    or by hashing every file (docs/MODEL_MANIFEST.md).
  - Removes model revisions that neither this package nor any installed version of the
    app (its model-manifest.json) uses. Never through a link.
  - Writes models\package-<SHA-256 of models.json>.json (a copy of models.json) for
    Intune's detection rule, last, and removes an earlier package's.
  - Logs fixed words and numbers only to the install folder's logs\install.log.

  -Uninstall removes the models folder (links as links, never followed) and with it the
  detection file: first renamed aside in one step, so a file in use refuses it with
  nothing removed (exit 3), then removed. With Unminute no longer installed for everyone
  (its machine uninstall removes the models too), or no models folder, there is nothing
  to remove, and it exits 0.

  Exit codes: 0 done, 1 failed (including another install still running after 10
  minutes, or a permission fault), 2 not allowed (a 32-bit or non-elevated run, or, to
  install, Unminute not installed for everyone on this computer), 3 a model folder is in
  use (Intune tries again), 4 the package failed its checks (including against an
  installed version's model list), or the install folder's permissions have changed or
  it holds a link where models go.

.EXAMPLE
  %SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File Install-Models.ps1
.EXAMPLE
  %SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File Install-Models.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$Options = @{ InstallDir = ""; Package = $PSScriptRoot; Silent = $false; Uninstall = [bool]$Uninstall }
$AppName = "Unminute"
$MachineFolderName = "Unminute"
# Program Files from Windows' known folder, set once here (the tests redirect it).
$ProgramFilesFolder = [Environment]::GetFolderPath("ProgramFiles")
$LockName = "install.lock"
$LockWaitSeconds = 600
$LockRetrySeconds = 30
$LogNames = @("install.log", "install.log.1")
$LogLimitBytes = 256KB
$ManifestName = "models.json"
# The models folder in the install folder (install.ps1 and uninstall.ps1 name it too).
$ModelsFolderName = "models"
$RecordName = "models.verified.json"
# Plain names for a family and a folder, a pinned revision, and one part of a file's name.
$NamePattern = '^[0-9A-Za-z][0-9A-Za-z._-]{0,99}\z'
$RevisionPattern = '^[0-9a-f]{40}\z'
$PartPattern = '^[0-9A-Za-z_][0-9A-Za-z._-]{0,99}\z'
$WorkPattern = '^\.(staging|retired)-[0-9a-f]{32}\z'
$DetectPattern = '^package-[0-9a-f]{64}\.json\z'
$AdministratorsSid = "S-1-5-32-544"
$SystemSid = "S-1-5-18"
$UsersSid = "S-1-5-32-545"
$AppPackagesSid = "S-1-15-2-1"
$PermissionMessage = "The install folder's permissions have changed, so nothing was changed. Ask IT to check this computer."
$script:InstallLock = $null
$script:LogFile = $null
$script:LogStep = "start"
$script:LogClock = [Diagnostics.Stopwatch]::StartNew()

function Write-Step([string]$Message) {
    if (-not $Options.Silent) { Write-Host $Message }
}

function Write-Warn([string]$Message) {
    [Console]::Error.WriteLine("Warning: $Message")
}

function Stop-Install([int]$Code, [string]$Message) {
    Write-ModelLog "stopped" $Code
    [Console]::Error.WriteLine($Message)
    exit $Code
}

# One line per step in <install folder>\logs\install.log, as the installer writes it:
# the time (UTC), mode=models, the step, its result, the exit code and the time since
# the start. Never a user name, path or message (P3). A failure to write it never stops
# the run.
function Write-ModelLog([string]$Result, [int]$Code = -1) {
    if (-not $script:LogFile) { return }
    try {
        $time = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", [Globalization.CultureInfo]::InvariantCulture)
        $line = "$time mode=models step=$($script:LogStep) result=$Result"
        if ($Code -ge 0) { $line += " code=$Code" }
        $line += " elapsed_ms=$($script:LogClock.ElapsedMilliseconds)`r`n"
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

# -- the installer's own functions (tests/test_model_package.py keeps them equal) ------

function Get-FileSha256([string]$Path) {
    (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Test-TransientLock([Exception]$Exception, [string]$LockPath = "") {
    for ($e = $Exception; $e; $e = $e.InnerException) {
        if ($e -isnot [IO.IOException] -and $e -isnot [UnauthorizedAccessException]) { continue }
        # In use: a sharing violation (0x80070020) or a lock violation (0x80070021).
        if ($e -is [IO.IOException] -and @(0x80070020, 0x80070021) -contains $e.HResult) { return $true }
        # Access denied: an UnauthorizedAccessException, an IOException with the Windows
        # code (0x80070005), or Windows PowerShell's folder rename's plain IOException
        # without it (0x80131620, "Access to the path ... is denied"). Windows gives it
        # for a folder that holds an open file and for a running program, which pass, but
        # also for a permission fault, which does not (#451): it counts as in use only
        # when this run can still write and remove a file beside $LockPath.
        if ($e -is [UnauthorizedAccessException] -or $e.HResult -eq 0x80070005 -or
            ($e.GetType() -eq [IO.IOException] -and $e.HResult -eq 0x80131620)) {
            return (Test-WriteBeside $LockPath)
        }
    }
    return $false
}

# Whether this run can create, and delete, a file in the folder that holds $Path: a
# probe .probe-<32 hex digits>.tmp, deleted when closed (so even a run stopped part
# way leaves none). $false for no $Path, or when Windows refuses either.
function Test-WriteBeside([string]$Path) {
    if (-not $Path) { return $false }
    try {
        $parent = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path))
        if (-not $parent) { return $false }
        $probe = [IO.Path]::Combine($parent, ".probe-" + [guid]::NewGuid().ToString("N") + ".tmp")
        [IO.FileStream]::new($probe, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None, 1,
            [IO.FileOptions]::DeleteOnClose).Dispose()
        return $true
    } catch { return $false }
}

# Runs $Action (which must not use the names below) until it succeeds, it fails for
# any reason but a file in use (Test-TransientLock, for the item at $LockPath), or
# $LockRetrySeconds have passed since its first refusal.
function Invoke-LockRetry([scriptblock]$Action, [string]$LockPath = "") {
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
            if ($left -le 0 -or -not (Test-TransientLock $_.Exception $LockPath)) { throw }
            if (-not $said) {
                Write-Step "A file in the folder is in use, perhaps by a virus scan. Trying again for up to $LockRetrySeconds seconds..."
                $said = $true
            }
            Start-Sleep -Milliseconds ([Math]::Min($pause, $left))
            $pause = [Math]::Min($pause * 2, 4000)
        }
    }
}

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
            if (-not (Test-TransientLock $_.Exception $path)) { throw }
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

function Test-Elevated {
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Test-64BitProcess { return [Environment]::Is64BitProcess }
function Get-ProgramFilesFolder { return $ProgramFilesFolder }
function Get-FolderSecurity([string]$Path) { return [IO.DirectoryInfo]::new($Path).GetAccessControl() }

function Get-MachineRoot {
    $programFiles = [string](Get-ProgramFilesFolder)
    if ($programFiles -notmatch '^[A-Za-z]:\\') { return $null }
    $parent = [IO.DirectoryInfo]::new($programFiles)
    if (-not $parent.Exists -or (Test-Link $parent)) { return $null }
    return [IO.Path]::Combine($parent.FullName.TrimEnd("\"), $MachineFolderName)
}

function Get-MachineRule {
    $full = [Security.AccessControl.FileSystemRights]::FullControl
    $read = [Security.AccessControl.FileSystemRights]::ReadAndExecute
    return @(@($SystemSid, $full), @($AdministratorsSid, $full), @($UsersSid, $read), @($AppPackagesSid, $read))
}

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

function Test-AclChanged([IO.FileSystemInfo]$Item) {
    $security = $Item.GetAccessControl()
    return $security.AreAccessRulesProtected -or
        @($security.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier])).Count -gt 0
}

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

# -- the package and the models ----------------------------------------------------------

# Every file under $Dir, by its path relative to $Dir with / between parts, exactly as
# the folder listing spells it (an ordinal dictionary: a letter-case variant, a Kelvin
# sign for K or a capital sharp s is another name). $null when $Dir or anything in it
# is a link or junction: nothing behind one is listed or read.
function Get-FolderListing([string]$Dir) {
    $top = [IO.DirectoryInfo]::new($Dir)
    if (-not $top.Exists -or (Test-Link $top)) { return $null }
    $skip = $top.FullName.TrimEnd("\").Length + 1
    $files = [Collections.Generic.Dictionary[string, IO.FileInfo]]::new([StringComparer]::Ordinal)
    $todo = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
    $todo.Push($top)
    while ($todo.Count) {
        foreach ($item in $todo.Pop().GetFileSystemInfos()) {
            if (Test-Link $item) { return $null }
            if ($item -is [IO.DirectoryInfo]) { $todo.Push($item); continue }
            $files[$item.FullName.Substring($skip).Replace("\", "/")] = $item
        }
    }
    return , $files
}

# A file's name in a model: parts of plain names joined by /, nothing else.
function Test-FileName($Name) {
    if ($Name -isnot [string] -or $Name.Length -gt 200) { return $false }
    foreach ($part in $Name.Split("/")) {
        if ($part -cnotmatch $PartPattern -or $part -eq "." -or $part -eq "..") { return $false }
    }
    return $true
}

function Test-Count($Value) { return (($Value -is [int]) -or ($Value -is [long])) -and $Value -ge 0 }

# What is wrong with a package manifest (the parsed models.json), or $null: format 1,
# and each model with plain names, a pinned revision, and files with plain names
# (none twice, in any letter case, and none named as the record), sizes and SHA-256.
function Get-ManifestProblem($Manifest) {
    if ($Manifest -isnot [Management.Automation.PSCustomObject] -or $Manifest.format -ne 1 -or
        $Manifest.format -isnot [int]) { return "models.json is not a model package's manifest." }
    $models = @($Manifest.models)
    if (-not $models.Count) { return "models.json lists no model." }
    $places = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($model in $models) {
        if ($model -isnot [Management.Automation.PSCustomObject] -or $model.family -isnot [string] -or
            $model.folder -isnot [string] -or $model.revision -isnot [string] -or $model.repository -isnot [string] -or
            $model.family -cnotmatch $NamePattern -or $model.folder -cnotmatch $NamePattern -or
            $model.revision -cnotmatch $RevisionPattern -or $model.repository -cnotmatch '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\z') {
            return "models.json names a model that is not valid."
        }
        if (-not $places.Add("$($model.family)/$($model.folder)/$($model.revision)")) {
            return "models.json lists a model twice."
        }
        $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        $files = @($model.files)
        if (-not $files.Count) { return "models.json lists a model with no files." }
        foreach ($file in $files) {
            if (-not (Test-FileName $file.name) -or -not (Test-Count $file.size) -or
                $file.sha256 -isnot [string] -or $file.sha256 -cnotmatch '^[0-9a-f]{64}\z' -or
                $file.name -ieq $RecordName -or -not $names.Add($file.name)) {
                return "models.json lists a model file that is not valid."
            }
        }
    }
    return $null
}

# What is wrong with the files under $Dir for $Model, or $null: exactly the listed
# names (and $Allowed, such as the record), no link, and each file's size and SHA-256.
function Get-ModelProblem([string]$Dir, $Model, [string[]]$Allowed = @()) {
    $files = Get-FolderListing $Dir
    if ($null -eq $files) { return "missing, or holds a link" }
    $listed = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($file in $Model.files) { [void]$listed.Add([string]$file.name) }
    foreach ($rel in $files.Keys) {
        if (-not $listed.Contains($rel) -and $Allowed -cnotcontains $rel) { return "holds a file that is not the model's" }
    }
    foreach ($file in $Model.files) {
        $found = $null
        if (-not $files.TryGetValue([string]$file.name, [ref]$found)) { return "a file is missing" }
        if ($found.Length -ne [long]$file.size -or (Get-FileSha256 $found.FullName) -cne [string]$file.sha256) {
            return "a file does not match its checksum"
        }
    }
    return $null
}

# Each file's [size, modification time], the time in nanoseconds since 1 January 1970
# UTC, which is what Python's os.stat gives as st_mtime_ns on Windows.
function Get-ModelFingerprint([string]$Dir, $Model) {
    $found = [ordered]@{}
    foreach ($file in $Model.files) {
        $info = [IO.FileInfo]::new([IO.Path]::Combine($Dir, ([string]$file.name).Replace("/", "\")))
        # Computed first: in a list, PowerShell's comma binds before the multiplication.
        $time = (([long]$info.LastWriteTimeUtc.ToFileTimeUtc()) - 116444736000000000L) * 100L
        $found[[string]$file.name] = @([long]$info.Length, $time)
    }
    return $found
}

# models.verified.json in $Dir: written to a temporary name and renamed into place, so
# the app never reads half a record. UTF-8 without a byte-order mark (Python reads it).
function Write-ModelRecord([string]$Dir, $Model) {
    $record = [ordered]@{
        repo = [string]$Model.repository; revision = [string]$Model.revision; folder = [string]$Model.folder
        ok = $true; files = (Get-ModelFingerprint $Dir $Model)
    }
    $path = [IO.Path]::Combine($Dir, $RecordName)
    $temp = "$path.tmp"
    [IO.File]::WriteAllText($temp, ($record | ConvertTo-Json -Depth 5 -Compress), [Text.UTF8Encoding]::new($false))
    if ([IO.File]::Exists($path)) { [IO.File]::Delete($path) }
    [IO.File]::Move($temp, $path)
}

function Get-ModelDir([string]$Base, $Model) {
    return [IO.Path]::Combine($Base, $Model.family, $Model.folder, $Model.revision)
}

# One model into <models>\<family>\<folder>\<revision>. Returns "kept" or "installed";
# throws when a folder could not be renamed (Test-TransientLock tells a folder in use).
function Install-ModelFolder([string]$Models, $Model) {
    $target = Get-ModelDir $Models $Model
    if ([IO.Directory]::Exists($target) -and -not (Get-ModelProblem $target $Model @($RecordName))) {
        Write-ModelRecord $target $Model
        return "kept"
    }
    $staging = [IO.Path]::Combine($Models, ".staging-" + [guid]::NewGuid().ToString("N"))
    $source = Get-ModelDir ([IO.Path]::Combine($Options.Package, $ModelsFolderName)) $Model
    try {
        foreach ($file in $Model.files) {
            $to = [IO.Path]::Combine($staging, ([string]$file.name).Replace("/", "\"))
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($to))
            [IO.File]::Copy([IO.Path]::Combine($source, ([string]$file.name).Replace("/", "\")), $to, $false)
        }
        if (Get-ModelProblem $staging $Model) { Stop-Install 4 "A model file changed while it was copied, so it was not installed." }
        Write-ModelRecord $staging $Model
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
        $aside = $null
        if ([IO.Directory]::Exists($target)) {
            $aside = [IO.Path]::Combine($Models, ".retired-" + [guid]::NewGuid().ToString("N"))
            Invoke-LockRetry { [IO.Directory]::Move($target, $aside) } $target
        }
        try {
            Invoke-LockRetry { [IO.Directory]::Move($staging, $target) } $staging
        } catch {
            # The old copy goes back, so the model is not left missing until the next run.
            if ($aside -and -not [IO.Directory]::Exists($target)) { try { [IO.Directory]::Move($aside, $target) } catch { } }
            throw
        }
        if ($aside) { try { Remove-OwnFolder $aside } catch { } }    # else the next run removes it
    } finally {
        if ([IO.Directory]::Exists($staging)) { try { Remove-OwnFolder $staging } catch { } }
    }
    return "installed"
}

# The revisions to keep: this package's, and those in every installed version's
# model-manifest.json. $null when an installed version's list cannot be read, so
# nothing is removed.
function Get-PinnedRevision([string]$Root, $Manifest) {
    $pinned = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($model in $Manifest.models) { [void]$pinned.Add([string]$model.revision) }
    $versions = [IO.DirectoryInfo]::new([IO.Path]::Combine($Root, "versions"))
    if (-not $versions.Exists) { return , $pinned }
    foreach ($dir in $versions.GetDirectories()) {
        if ((Test-Link $dir) -or $dir.Name -match '\.(partial|old|retired)\z') { continue }
        $file = [IO.FileInfo]::new([IO.Path]::Combine($dir.FullName, "model-manifest.json"))
        if (-not $file.Exists -or (Test-Link $file)) { return $null }
        try { $list = [IO.File]::ReadAllText($file.FullName) | ConvertFrom-Json } catch { return $null }
        foreach ($model in @($list.models)) {
            if ($model.revision -is [string] -and $model.revision -cmatch $RevisionPattern) { [void]$pinned.Add($model.revision) }
        }
    }
    return , $pinned
}

# One model's files as one line: each "name|size|SHA-256", in name order.
function Get-FileLine($Model) {
    $lines = @(foreach ($file in @($Model.files)) { "$([string]$file.name)|$([string]$file.size)|$([string]$file.sha256)" })
    return (($lines | Sort-Object -CaseSensitive) -join "/")
}

# What is wrong with the package against the installed versions' own model lists, or
# $null (#451): a model that an installed version's model-manifest.json pins (the same
# family, folder and revision) must have the same repository and exactly the same files,
# by name, size and SHA-256. The app trusts IT's record for that revision without hashing
# (except the speaker model), so the record never vouches for files that version's own
# list does not name. A version whose list cannot be read is not compared here
# (Get-PinnedRevision then removes no older model).
function Get-PinProblem([string]$Root, $Manifest) {
    $versions = [IO.DirectoryInfo]::new([IO.Path]::Combine($Root, "versions"))
    if (-not $versions.Exists) { return $null }
    foreach ($dir in $versions.GetDirectories()) {
        if ((Test-Link $dir) -or $dir.Name -match '\.(partial|old|retired)\z') { continue }
        $file = [IO.FileInfo]::new([IO.Path]::Combine($dir.FullName, "model-manifest.json"))
        if (-not $file.Exists -or (Test-Link $file)) { continue }
        try { $list = [IO.File]::ReadAllText($file.FullName) | ConvertFrom-Json } catch { continue }
        foreach ($pin in @($list.models)) {
            foreach ($model in $Manifest.models) {
                if ([string]$pin.family -cne $model.family -or [string]$pin.folder -cne $model.folder -or
                    [string]$pin.revision -cne $model.revision) { continue }
                if ([string]$pin.repository -cne $model.repository -or (Get-FileLine $pin) -cne (Get-FileLine $model)) {
                    return "$($model.family)/$($model.folder) does not match the files that installed version $($dir.Name) pins for it."
                }
            }
        }
    }
    return $null
}

# Removes revision folders that nothing pins, and working folders left by a run that
# stopped: renamed aside first, then removed, never through a link. Only folders with
# the shapes this script makes; anything else is left. Returns how many are in use.
function Remove-UnpinnedModel([string]$Models, $Pinned) {
    $busy = 0
    foreach ($item in [IO.DirectoryInfo]::new($Models).GetDirectories()) {
        if (-not (Test-Link $item) -and $item.Name -cmatch $WorkPattern) {
            try { Remove-OwnFolder $item.FullName } catch { $busy++ }
        }
    }
    if ($null -eq $Pinned) {
        Write-Warn "An installed version's model list could not be read, so no older model was removed."
        return $busy
    }
    foreach ($family in [IO.DirectoryInfo]::new($Models).GetDirectories()) {
        if ((Test-Link $family) -or $family.Name -cnotmatch $NamePattern) { continue }
        foreach ($folder in $family.GetDirectories()) {
            if ((Test-Link $folder) -or $folder.Name -cnotmatch $NamePattern) { continue }
            foreach ($revision in $folder.GetDirectories()) {
                if ((Test-Link $revision) -or $revision.Name -cnotmatch $RevisionPattern -or
                    $Pinned.Contains($revision.Name)) { continue }
                $aside = [IO.Path]::Combine($Models, ".retired-" + [guid]::NewGuid().ToString("N"))
                try {
                    [IO.Directory]::Move($revision.FullName, $aside)
                    Remove-OwnFolder $aside
                } catch { $busy++ }
            }
        }
    }
    return $busy
}

# The detection file: package-<SHA-256 of models.json>.json, a copy of models.json,
# written last; an earlier package's is removed.
function Set-DetectionFile([string]$Models, [string]$Sha) {
    foreach ($file in [IO.DirectoryInfo]::new($Models).GetFiles()) {
        if (-not (Test-Link $file) -and $file.Name -cmatch $DetectPattern -and $file.Name -cne "package-$Sha.json") {
            $file.Delete()
        }
    }
    $path = [IO.Path]::Combine($Models, "package-$Sha.json")
    [IO.File]::Copy([IO.Path]::Combine($Options.Package, $ManifestName), "$path.tmp", $true)
    if ([IO.File]::Exists($path)) { [IO.File]::Delete($path) }
    [IO.File]::Move("$path.tmp", $path)
}

# -- the run ----------------------------------------------------------------------------

# The checks every run makes before it writes anything: 64-bit and elevated; a machine
# install of Unminute (its install folder with its record); that folder's permissions;
# then, holding the installer's lock, nothing beneath it with permissions of its own.
# -Removing (the uninstall): no install folder returns $null, as there is nothing to
# remove, and the folder's record is not needed (#451).
function Start-ModelRun([switch]$Removing) {
    if (-not (Test-64BitProcess)) {
        Stop-Install 2 ("The model package needs 64-bit Windows PowerShell, so nothing was changed. Start it with " +
            "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe.")
    }
    if (-not (Test-Elevated)) {
        Stop-Install 2 "The model package needs administrator rights (Intune's System context), so nothing was changed."
    }
    $root = Get-MachineRoot
    if ($Removing -and (-not $root -or (-not [IO.Directory]::Exists($root) -and -not [IO.File]::Exists($root)))) {
        return $null
    }
    if (-not $root -or -not [IO.Directory]::Exists($root) -or
        (-not $Removing -and -not [IO.File]::Exists([IO.Path]::Combine($root, "current.json")))) {
        Stop-Install 2 "$AppName is not installed for everyone on this computer. Install it first (install.ps1 -Machine), then the models."
    }
    if (Test-Link ([IO.DirectoryInfo]::new($root))) { Stop-Install 4 $PermissionMessage }
    if (-not (Test-MachineRootSecurity (Get-FolderSecurity $root))) { Stop-Install 4 $PermissionMessage }
    $Options.InstallDir = $root
    Enter-InstallLock
    if ((Get-AclChangeCount $root) -gt 0) { Stop-Install 4 $PermissionMessage }
    $logs = [IO.DirectoryInfo]::new([IO.Path]::Combine($root, "logs"))
    if (-not $logs.Exists) { $logs.Create(); $logs.Refresh() }   # a missing folder reads as every attribute
    if (-not (Test-Link $logs)) { $script:LogFile = [IO.Path]::Combine($logs.FullName, $LogNames[0]) }
    Write-ModelLog "begun"
    $models = [IO.DirectoryInfo]::new([IO.Path]::Combine($root, $ModelsFolderName))
    if ($models.Exists -and ($null -eq (Get-FolderListing $models.FullName))) {
        Stop-Install 4 "The models folder holds a link or junction, so nothing was changed. Ask IT to check this computer."
    }
    if ([IO.File]::Exists($models.FullName)) { Stop-Install 4 "The models folder is a file, so nothing was changed." }
    return $models.FullName
}

function Invoke-ModelInstall {
    $models = Start-ModelRun
    $script:LogStep = "check"
    $manifestFile = [IO.FileInfo]::new([IO.Path]::Combine($Options.Package, $ManifestName))
    $manifest = $null
    if ($manifestFile.Exists -and -not (Test-Link $manifestFile)) {
        try { $manifest = [IO.File]::ReadAllText($manifestFile.FullName) | ConvertFrom-Json } catch { $manifest = $null }
    }
    $problem = if ($null -eq $manifest) { "models.json is missing or cannot be read." } else { Get-ManifestProblem $manifest }
    if ($problem) { Stop-Install 4 "The model package failed its checks: $problem Nothing was changed." }
    $sha = Get-FileSha256 $manifestFile.FullName
    # The package's files: exactly the listed models' files, nothing else and no link.
    $packaged = Get-FolderListing ([IO.Path]::Combine($Options.Package, $ModelsFolderName))
    $expected = 0
    foreach ($model in $manifest.models) {
        $expected += @($model.files).Count
        if (Get-ModelProblem (Get-ModelDir ([IO.Path]::Combine($Options.Package, $ModelsFolderName)) $model) $model) {
            Stop-Install 4 "The model package failed its checks: a model's files do not match models.json. Nothing was changed."
        }
    }
    if ($null -eq $packaged -or $packaged.Count -ne $expected) {
        Stop-Install 4 "The model package failed its checks: it holds files that models.json does not list. Nothing was changed."
    }
    $problem = Get-PinProblem $Options.InstallDir $manifest
    if ($problem) { Stop-Install 4 "The model package failed its checks: $problem Nothing was changed." }
    $script:LogStep = "copy"
    Write-ModelLog "begun"
    [void][IO.Directory]::CreateDirectory($models)
    # No package counts as installed while models change: the detection file comes back
    # only once every model is in place (Set-DetectionFile).
    foreach ($file in [IO.DirectoryInfo]::new($models).GetFiles()) {
        if (-not (Test-Link $file) -and $file.Name -cmatch $DetectPattern) { $file.Delete() }
    }
    $busy = 0
    foreach ($model in $manifest.models) {
        try {
            $result = Install-ModelFolder $models $model
            Write-Step "$($model.name): $result."
        } catch {
            if (-not (Test-TransientLock $_.Exception (Get-ModelDir $models $model))) { throw }
            $busy++
        }
    }
    if ($busy) {
        Stop-Install 3 "$busy model folder(s) are in use, so they were not replaced yet. Run this again later (Intune tries again)."
    }
    $script:LogStep = "prune"
    $left = Remove-UnpinnedModel $models (Get-PinnedRevision $Options.InstallDir $manifest)
    if ($left) { Write-Warn "$left older model folder(s) are in use and were left; the next run removes them." }
    $script:LogStep = "detect"
    Set-DetectionFile $models $sha
    $script:LogStep = "done"
    Write-ModelLog "ok" 0
    Write-Step "The models are installed for everyone on this computer. Package $sha"
}

# -Uninstall finishes or refuses cleanly (#451). With Unminute's install folder gone (the
# app's machine uninstall removes the models with it) or no models folder, there is
# nothing to remove: done. Otherwise the models folder, with the detection file, is
# renamed aside in one step to staging-<32 hex digits> in the install folder, a name the
# installer and the machine uninstall remove as a leftover. A file in use refuses that
# rename, and nothing is removed (exit 3, Intune tries again). The app never sees part of
# the models, and Intune's detection rule finds none from that rename on. The renamed
# folder is then removed; what a scan still holds is left for the installer.
function Invoke-ModelUninstall {
    $models = Start-ModelRun -Removing
    if (-not $models -or -not [IO.Directory]::Exists($models)) {
        if ($models) { $script:LogStep = "done"; Write-ModelLog "ok" 0 }
        Write-Step "There are no models installed for everyone on this computer, so nothing was removed."
        return
    }
    $script:LogStep = "remove"
    $aside = [IO.Path]::Combine($Options.InstallDir, "staging-" + [guid]::NewGuid().ToString("N"))
    try {
        Invoke-LockRetry { [IO.Directory]::Move($models, $aside) } $models
    } catch {
        if (Test-TransientLock $_.Exception $models) {
            Stop-Install 3 "A model file is in use, so nothing was removed. Run this again later (Intune tries again)."
        }
        throw
    }
    try { Invoke-LockRetry { Remove-OwnFolder $aside } $aside }
    catch { Write-Warn "Some removed model files were still in use; the next install or update of $AppName clears them." }
    $script:LogStep = "done"
    Write-ModelLog "ok" 0
    Write-Step "The models installed for everyone on this computer were removed."
}

# Dot-sourced (the Pester tests in tests/powershell): the functions above are defined
# and nothing runs.
if ($MyInvocation.InvocationName -eq ".") { return }

try {
    if ($Options.Uninstall) { Invoke-ModelUninstall } else { Invoke-ModelInstall }
    exit 0
} catch {
    Stop-Install 1 "The model package failed: $($_.Exception.GetType().Name)"
} finally {
    Exit-InstallLock
}
