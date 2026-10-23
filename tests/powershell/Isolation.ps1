# Keeps the installer's Pester tests off this computer's real install (#111 B6a, B6b).
#
# On 23 October 2026 a local Pester run dot-sourced an older uninstall.ps1 that had
# no dot-source guard: its main part ran with its default -InstallDir and uninstalled
# the per-user copy on the development computer. So a test loads install.ps1 or
# uninstall.ps1 only like this, and the tests check that every load does:
#
#   . (Confirm-Loadable $path); Set-ScratchTarget
#
# Enter-ScratchProfile (once, first) points LOCALAPPDATA and APPDATA into the scratch
# folder, so the scripts' default -InstallDir is scratch too. Confirm-Loadable refuses,
# before anything in the script runs, a script without its dot-source guard, a script
# that reaches the Start menu, Program Files, the uninstall keys or the Event Log
# source other than through the variables each sets once at its top ($ProgramsFolder,
# $RegKey; for machine mode $ProgramFilesFolder, $CommonProgramsFolder, $MachineRegKey,
# $EventSourceKey), and a run whose LOCALAPPDATA or APPDATA is not scratch.
# Set-ScratchTarget then points both keys and the Event Log source at throwaway HKCU
# keys and the three folders at scratch ones, and Assert-ScratchTarget checks every
# place the script would write. Set-ScratchMachineTarget gives one test its own
# machine places, checked the same way. The real places come from the shell's known
# folders, which do not read LOCALAPPDATA or APPDATA, so pointing those variables
# elsewhere cannot hide them. tools/run_pester.ps1 also runs the tests with scratch
# variables and fails the run if the real install, per user or machine, changed.
#
# ASCII only, like the scripts it loads (docs/DEVELOPER_NOTES.md).

$script:RealUninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Unminute"
$script:RealMachineKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Unminute"
$script:RealEventSourceKey = "HKLM:\SYSTEM\CurrentControlSet\Services\EventLog\Application\Unminute"
$script:ScratchKeyPrefix = "HKCU:\Software\Unminute-PesterTest-"

# This account's real install folder, data folder, Start menu Programs folder and
# roaming folder, and this computer's Program Files (both views) and all-users Start
# menu Programs folder, from the shell, never from LOCALAPPDATA or APPDATA.
function Get-RealPlace {
    $local = [Environment]::GetFolderPath("LocalApplicationData")
    if (-not $local) { throw "The shell did not give this account's local folder, so no test ran." }
    $places = @(
        (Join-Path $local "Programs\Unminute"),
        (Join-Path $local "Unminute"),
        [Environment]::GetFolderPath("Programs"),
        [Environment]::GetFolderPath("ApplicationData"),
        [Environment]::GetFolderPath("ProgramFiles"),
        [Environment]::GetFolderPath("ProgramFilesX86"),
        [Environment]::GetFolderPath("CommonPrograms")
    )
    return @($places | Where-Object { $_ })
}

# True when $Path is inside the scratch folder and is not, does not hold and does not
# sit inside any real place.
function Test-ScratchPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path) -or
        $Path -match '^[\\/]{2}') { return $false }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd("\") + "\"
    $root = [IO.Path]::GetFullPath($script:ScratchRoot).TrimEnd("\") + "\"
    if (-not $full.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { return $false }
    foreach ($place in Get-RealPlace) {
        $real = [IO.Path]::GetFullPath($place).TrimEnd("\") + "\"
        if ($full.StartsWith($real, [StringComparison]::OrdinalIgnoreCase) -or
            $real.StartsWith($full, [StringComparison]::OrdinalIgnoreCase)) { return $false }
    }
    return $true
}

# True when $Key is one of the throwaway HKCU keys these tests make.
function Test-ScratchKey([string]$Key) {
    return $Key.StartsWith($script:ScratchKeyPrefix, [StringComparison]::Ordinal) -and
        -not $Key.Contains("..") -and -not $Key.Contains("/")
}

# Called once, before any test: a scratch folder under $Root (Pester's TestDrive) with
# a scratch LOCALAPPDATA, APPDATA, Start menu Programs folder, Program Files and
# all-users Start menu folder, and the names of throwaway HKCU keys, all under one
# key. Exit-ScratchProfile puts the variables back and removes that key.
function Enter-ScratchProfile([string]$Root) {
    $script:ScratchRoot = [IO.Directory]::CreateDirectory((Join-Path $Root "scratch")).FullName
    foreach ($place in Get-RealPlace) {
        $real = [IO.Path]::GetFullPath($place).TrimEnd("\") + "\"
        $mine = $script:ScratchRoot.TrimEnd("\") + "\"
        if ($mine.StartsWith($real, [StringComparison]::OrdinalIgnoreCase) -or
            $real.StartsWith($mine, [StringComparison]::OrdinalIgnoreCase)) {
            throw "The scratch folder overlaps a real place, so no test ran."
        }
    }
    $script:SavedLocal = $env:LOCALAPPDATA
    $script:SavedRoaming = $env:APPDATA
    $env:LOCALAPPDATA = [IO.Directory]::CreateDirectory((Join-Path $script:ScratchRoot "Local")).FullName
    $env:APPDATA = [IO.Directory]::CreateDirectory((Join-Path $script:ScratchRoot "Roaming")).FullName
    $script:ScratchPrograms = [IO.Directory]::CreateDirectory((Join-Path $script:ScratchRoot "Programs")).FullName
    $script:ScratchProgramFiles = [IO.Directory]::CreateDirectory((Join-Path $script:ScratchRoot "ProgramFiles")).FullName
    $script:ScratchCommonPrograms = [IO.Directory]::CreateDirectory((Join-Path $script:ScratchRoot "CommonPrograms")).FullName
    $script:ScratchTop = $script:ScratchKeyPrefix + [guid]::NewGuid().ToString("N")
    $script:ScratchKey = $script:ScratchTop + "\Uninstall\Unminute"
    $script:ScratchMachineKey = $script:ScratchTop + "\Machine\Uninstall\Unminute"
    $script:ScratchEventSourceKey = $script:ScratchTop + "\Machine\EventLog\Unminute"
}

function Exit-ScratchProfile {
    $env:LOCALAPPDATA = $script:SavedLocal
    $env:APPDATA = $script:SavedRoaming
    if ($script:ScratchTop -and (Test-ScratchKey $script:ScratchTop) -and (Test-Path -LiteralPath $script:ScratchTop)) {
        Remove-Item -LiteralPath $script:ScratchTop -Recurse -Force
    }
}

# A script may be dot-sourced only when it has the guard (a top-level
# "if ($MyInvocation.InvocationName -eq ".") { return }" before the main part), since
# without it the main part would install or uninstall for real.
function Assert-DotSourceGuard([string]$Path) {
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
    $statements = @($ast.EndBlock.Statements)
    $guard = -1; $main = -1
    for ($i = 0; $i -lt $statements.Count; $i++) {
        $text = $statements[$i].Extent.Text
        if ($guard -lt 0 -and $text -ceq 'if ($MyInvocation.InvocationName -eq ".") { return }') { $guard = $i }
        if ($main -lt 0 -and $statements[$i] -is [Management.Automation.Language.TryStatementAst]) { $main = $i }
    }
    if ($errors.Count -or $guard -lt 0 -or $main -lt 0 -or $guard -gt $main) {
        throw "$Path has no dot-source guard before its main part, so it was not loaded."
    }
}

# The script reaches the places outside its install folder only through the variables
# it sets once at its top, so Set-ScratchTarget can redirect every one. Counted in the
# script's code without its comments: each way to reach a place may appear only in
# those top-level lines, and HKLM otherwise only in the WebView2 check's two reads.
# The registry is never written through .NET (CreateSubKey, DeleteSubKey...), and a
# key is opened only to read it. An older script (which looked up the Programs folder
# inside its functions, or Program Files before B6b's review) is refused.
function Assert-RedirectableTarget([string]$Path) {
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
    # The script's text with every comment blanked out (same length, so offsets hold).
    $start = $ast.Extent.StartOffset
    $text = [Text.StringBuilder]::new($ast.Extent.Text)
    foreach ($token in $tokens) {
        if ($token.Kind -ne [Management.Automation.Language.TokenKind]::Comment) { continue }
        $at = $token.Extent.StartOffset - $start
        $length = $token.Extent.EndOffset - $token.Extent.StartOffset
        if ($at -lt 0 -or $at + $length -gt $text.Length) { continue }
        [void]$text.Remove($at, $length).Insert($at, (" " * $length))
    }
    $code = $text.ToString()
    $top = @($ast.EndBlock.Statements | ForEach-Object { $_.Extent.Text })
    $lines = @(
        '$ProgramsFolder = [Environment]::GetFolderPath("Programs")',
        ('$RegKey = "' + $script:RealUninstallKey + '"'),
        '$ProgramFilesFolder = [Environment]::GetFolderPath("ProgramFiles")',
        '$CommonProgramsFolder = [Environment]::GetFolderPath("CommonPrograms")',
        ('$MachineRegKey = "' + $script:RealMachineKey + '"'),
        ('$EventSourceKey = "' + $script:RealEventSourceKey + '"')
    )
    $present = @($lines | Where-Object { $top -ccontains $_ })
    $reads = @('"HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$WebView2Client"',
               '"HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$WebView2Client"',
               '$base.OpenSubKey($subKey)')
    $reach = @("GetFolderPath", "SpecialFolder", "CurrentVersion\Uninstall", "Services\EventLog", "HKLM:",
               "HKEY_", "Registry::", "Start Menu\", "env:Program", "env:ALLUSERSPROFILE", "env:PUBLIC",
               "CreateSubKey", "DeleteSubKey", "DeleteValue", "OpenSubKey", "EventSource(", "New-EventLog",
               "Remove-EventLog")
    foreach ($word in $reach) {
        $pattern = [regex]::Escape($word)
        $count = ([regex]::Matches($code, $pattern, "IgnoreCase")).Count
        $allowed = 0
        foreach ($line in $present) { $allowed += ([regex]::Matches($line, $pattern, "IgnoreCase")).Count }
        foreach ($read in $reads) {
            $times = ([regex]::Matches($code, [regex]::Escape($read), "IgnoreCase")).Count
            $allowed += $times * ([regex]::Matches($read, $pattern, "IgnoreCase")).Count
        }
        if ($count -ne $allowed) {
            throw "$Path reaches this computer's Start menu, Program Files, uninstall keys or Event Log other than through the variables at its top, so it was not loaded."
        }
    }
}

# Every check before a script is dot-sourced; returns its path for the dot-source.
function Confirm-Loadable([string]$Path) {
    if (-not (Test-ScratchPath $env:LOCALAPPDATA) -or -not (Test-ScratchPath $env:APPDATA)) {
        throw "LOCALAPPDATA or APPDATA is not scratch, so $Path was not loaded."
    }
    Assert-DotSourceGuard $Path
    Assert-RedirectableTarget $Path
    return $Path
}

# Right after the dot-source, in the same scope: the uninstall keys, the Event Log
# source and the three folders become scratch ones, then every place the script
# would write is checked.
function Set-ScratchTarget {
    Set-Variable -Name RegKey -Value $script:ScratchKey -Scope 1
    Set-Variable -Name ProgramsFolder -Value $script:ScratchPrograms -Scope 1
    Set-Variable -Name ProgramFilesFolder -Value $script:ScratchProgramFiles -Scope 1
    Set-Variable -Name CommonProgramsFolder -Value $script:ScratchCommonPrograms -Scope 1
    Set-Variable -Name MachineRegKey -Value $script:ScratchMachineKey -Scope 1
    Set-Variable -Name EventSourceKey -Value $script:ScratchEventSourceKey -Scope 1
    Assert-ScratchTarget
}

# One test's own machine places (#111 B6b), in the caller's scope (a BeforeEach, which
# shares its scope with the test): a new scratch Program Files and all-users Start menu
# folder, and new throwaway keys for the machine uninstall entry and the Event Log
# source. Then every place is checked, as after Set-ScratchTarget.
function Set-ScratchMachineTarget {
    $id = [guid]::NewGuid().ToString("N").Substring(0, 8)
    Set-Variable -Name ProgramFilesFolder -Scope 1 -Value (
        [IO.Directory]::CreateDirectory((Join-Path $script:ScratchRoot "pf$id")).FullName)
    Set-Variable -Name CommonProgramsFolder -Scope 1 -Value (
        [IO.Directory]::CreateDirectory((Join-Path $script:ScratchRoot "cp$id")).FullName)
    Set-Variable -Name MachineRegKey -Value "$($script:ScratchTop)\m$id\Uninstall\Unminute" -Scope 1
    Set-Variable -Name EventSourceKey -Value "$($script:ScratchTop)\m$id\EventLog\Unminute" -Scope 1
    Assert-ScratchTarget
}

# This computer's real machine install as it stands (#111 B6b), read only, to compare
# before and after a test: Program Files\Unminute and the all-users shortcut (times and
# attributes), and the HKLM uninstall entry and Event Log source (values and subkeys,
# in the 64-bit view). Never shown.
function Get-RealMachineState {
    $state = @(foreach ($path in @(
            (Join-Path ([Environment]::GetFolderPath("ProgramFiles")) "Unminute"),
            (Join-Path ([Environment]::GetFolderPath("CommonPrograms")) "Unminute.lnk"))) {
        $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        if ($item) { "{0} {1}" -f $item.LastWriteTimeUtc.Ticks, $item.Attributes } else { "absent" }
    })
    foreach ($key in $script:RealMachineKey, $script:RealEventSourceKey) {
        $found = "unreadable"
        try {
            $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey("LocalMachine", [Microsoft.Win32.RegistryView]::Registry64)
            try {
                $open = $base.OpenSubKey($key.Substring(6))
                if ($null -eq $open) { $found = "absent" } else {
                    try {
                        $found = (@($open.GetValueNames() | Sort-Object | ForEach-Object { "$_=$($open.GetValue($_))" }) +
                            @($open.GetSubKeyNames() | Sort-Object)) -join "|"
                    } finally { $open.Dispose() }
                }
            } finally { $base.Dispose() }
        } catch { $found = "unreadable" }
        $state += $found
    }
    return ($state -join "`n")
}

# Reads the loaded script's variables as the script's own functions would see them.
function Assert-ScratchTarget {
    $places = [ordered]@{
        "LOCALAPPDATA" = $env:LOCALAPPDATA; "APPDATA" = $env:APPDATA
        "the default -InstallDir" = $InstallDir; "the install folder" = $Options.InstallDir
        "the Start menu folder" = $ProgramsFolder; "Program Files" = $ProgramFilesFolder
        "the all-users Start menu folder" = $CommonProgramsFolder
    }
    foreach ($name in $places.Keys) {
        if (-not (Test-ScratchPath $places[$name])) { throw "Not scratch: $name. Nothing was run." }
    }
    $keys = [ordered]@{
        "the uninstall key" = [string]$RegKey; "the machine uninstall key" = [string]$MachineRegKey
        "the Event Log source key" = [string]$EventSourceKey
    }
    foreach ($name in $keys.Keys) {
        if (-not (Test-ScratchKey $keys[$name])) { throw "Not scratch: $name. Nothing was run." }
    }
}
