# Keeps the installer's Pester tests off this computer's real install (#111 B6a).
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
# that reaches the Start menu or the uninstall key other than through $ProgramsFolder
# and $RegKey, and a run whose LOCALAPPDATA or APPDATA is not scratch. Set-ScratchTarget
# then points $RegKey at a throwaway HKCU key and $ProgramsFolder at a scratch folder,
# and Assert-ScratchTarget checks every place the script would write. The real places
# come from the shell's known folders, which do not read LOCALAPPDATA or APPDATA, so
# pointing those variables elsewhere cannot hide them. tools/run_pester.ps1 also runs
# the tests with scratch variables and fails the run if the real install changed.
#
# ASCII only, like the scripts it loads (docs/DEVELOPER_NOTES.md).

$script:RealUninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Unminute"
$script:ScratchKeyPrefix = "HKCU:\Software\Unminute-PesterTest-"

# This account's real install folder, data folder, Start menu Programs folder and
# roaming folder, from the shell, never from LOCALAPPDATA or APPDATA.
function Get-RealPlace {
    $local = [Environment]::GetFolderPath("LocalApplicationData")
    if (-not $local) { throw "The shell did not give this account's local folder, so no test ran." }
    return @(
        (Join-Path $local "Programs\Unminute"),
        (Join-Path $local "Unminute"),
        [Environment]::GetFolderPath("Programs"),
        [Environment]::GetFolderPath("ApplicationData")
    )
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

# Called once, before any test: a scratch folder under $Root (Pester's TestDrive) with
# a scratch LOCALAPPDATA, APPDATA and Start menu Programs folder, and the name of a
# throwaway HKCU key. Exit-ScratchProfile puts the variables back and removes the key.
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
    $script:ScratchKey = $script:ScratchKeyPrefix + [guid]::NewGuid().ToString("N") + "\Uninstall\Unminute"
}

function Exit-ScratchProfile {
    $env:LOCALAPPDATA = $script:SavedLocal
    $env:APPDATA = $script:SavedRoaming
    if ($script:ScratchKey -and $script:ScratchKey.StartsWith($script:ScratchKeyPrefix, [StringComparison]::Ordinal)) {
        $top = $script:ScratchKey.Substring(0, $script:ScratchKey.IndexOf("\Uninstall\Unminute"))
        if (Test-Path -LiteralPath $top) { Remove-Item -LiteralPath $top -Recurse -Force }
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

# The script reaches the Start menu and the uninstall key only through $ProgramsFolder
# and $RegKey, each set once at the top, so Set-ScratchTarget can redirect both. An
# older script (which looked up the Programs folder inside its functions) is refused.
function Assert-RedirectableTarget([string]$Path) {
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
    $text = $ast.Extent.Text
    $top = @($ast.EndBlock.Statements | ForEach-Object { $_.Extent.Text })
    $programs = '$ProgramsFolder = [Environment]::GetFolderPath("Programs")'
    $key = '$RegKey = "' + $script:RealUninstallKey + '"'
    $reach = @(
        @("GetFolderPath", $programs),
        @("CurrentVersion\Uninstall", $key),
        @("Start Menu\", $null),
        @("Registry::", $null)
    )
    foreach ($pair in $reach) {
        $count = ([regex]::Matches($text, [regex]::Escape($pair[0]), "IgnoreCase")).Count
        $allowed = if ($pair[1] -and $top -ccontains $pair[1]) { 1 } else { 0 }
        if ($count -ne $allowed) {
            throw "$Path reaches this computer's Start menu or uninstall key other than through `$ProgramsFolder and `$RegKey, so it was not loaded."
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

# Right after the dot-source, in the same scope: the uninstall key and the Start menu
# folder become scratch ones, then every place the script would write is checked.
function Set-ScratchTarget {
    Set-Variable -Name RegKey -Value $script:ScratchKey -Scope 1
    Set-Variable -Name ProgramsFolder -Value $script:ScratchPrograms -Scope 1
    Assert-ScratchTarget
}

# Reads the loaded script's variables as the script's own functions would see them.
function Assert-ScratchTarget {
    $places = [ordered]@{
        "LOCALAPPDATA" = $env:LOCALAPPDATA; "APPDATA" = $env:APPDATA
        "the default -InstallDir" = $InstallDir; "the install folder" = $Options.InstallDir
        "the Start menu folder" = $ProgramsFolder
    }
    foreach ($name in $places.Keys) {
        if (-not (Test-ScratchPath $places[$name])) { throw "Not scratch: $name. Nothing was run." }
    }
    if (-not ([string]$RegKey).StartsWith($script:ScratchKeyPrefix, [StringComparison]::Ordinal) -or
        ([string]$RegKey).Contains("..")) {
        throw "Not scratch: the uninstall key. Nothing was run."
    }
}
