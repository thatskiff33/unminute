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

# The final path Windows opens for an existing file or folder: links, junctions and
# 8.3 short names resolved (GetFinalPathNameByHandle), or $null when it cannot be
# opened. Compiled once per session.
if (-not ("UnminuteTest.FinalPath" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
namespace UnminuteTest {
    public static class FinalPath {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern SafeFileHandle CreateFileW(string name, uint access, uint share,
            IntPtr security, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern uint GetFinalPathNameByHandleW(SafeFileHandle file, StringBuilder path,
            uint size, uint flags);
        public static string Of(string path) {
            // No access, any sharing, open existing, folders too (backup semantics).
            using (SafeFileHandle file = CreateFileW(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {
                if (file.IsInvalid) { return null; }
                StringBuilder found = new StringBuilder(32768);
                uint length = GetFinalPathNameByHandleW(file, found, (uint)found.Capacity, 0);
                if (length == 0 || length >= found.Capacity) { return null; }
                return found.ToString();
            }
        }
    }
}
'@
}

# $Path in one spelling, as the Python tests' is_scratch reads it (realpath): the
# nearest part that exists resolved to its final path, the rest added as written.
# $null when a part that exists, or a link that leads nowhere, cannot be resolved, so
# a junction in the scratch folder that leads to a real place counts as that place.
function Resolve-FinalPath([string]$Path) {
    $probe = [IO.Path]::GetFullPath($Path).TrimEnd("\")
    $rest = @()
    while ($probe) {
        $item = Get-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        if ($item) {
            $final = [UnminuteTest.FinalPath]::Of($probe)
            if (-not $final) { return $null }
            if ($final.StartsWith("\\?\UNC\", [StringComparison]::OrdinalIgnoreCase)) {
                $final = "\\" + $final.Substring(8)
            } elseif ($final.StartsWith("\\?\", [StringComparison]::Ordinal)) {
                $final = $final.Substring(4)
            }
            return ((@($final.TrimEnd("\")) + $rest) -join "\")
        }
        $rest = @([IO.Path]::GetFileName($probe)) + $rest
        $probe = [IO.Path]::GetDirectoryName($probe)
    }
    return [IO.Path]::GetFullPath($Path).TrimEnd("\")
}

# True when $Path is inside the scratch folder and is not, does not hold and does not
# sit inside any real place, after links, junctions and short names are resolved.
function Test-ScratchPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path) -or
        $Path -match '^[\\/]{2}') { return $false }
    $full = Resolve-FinalPath $Path
    $root = Resolve-FinalPath $script:ScratchRoot
    if (-not $full -or -not $root -or $full -match '^[\\/]{2}') { return $false }
    $full = $full + "\"
    if (-not $full.StartsWith($root + "\", [StringComparison]::OrdinalIgnoreCase)) { return $false }
    foreach ($place in Get-RealPlace) {
        foreach ($form in @([IO.Path]::GetFullPath($place).TrimEnd("\"), (Resolve-FinalPath $place))) {
            if (-not $form) { continue }
            $real = $form + "\"
            if ($full.StartsWith($real, [StringComparison]::OrdinalIgnoreCase) -or
                $real.StartsWith($full, [StringComparison]::OrdinalIgnoreCase)) { return $false }
        }
    }
    return $true
}

# Called once, before any test: a scratch folder under $Root (Pester's TestDrive) with
# a scratch LOCALAPPDATA, APPDATA and Start menu Programs folder, and the name of a
# throwaway HKCU key. Exit-ScratchProfile puts the variables back and removes the key.
function Enter-ScratchProfile([string]$Root) {
    $script:ScratchRoot = [IO.Directory]::CreateDirectory((Join-Path $Root "scratch")).FullName
    $mine = Resolve-FinalPath $script:ScratchRoot
    if (-not $mine) { throw "The scratch folder could not be resolved, so no test ran." }
    $mine = $mine + "\"
    foreach ($place in Get-RealPlace) {
        foreach ($form in @([IO.Path]::GetFullPath($place).TrimEnd("\"), (Resolve-FinalPath $place))) {
            if (-not $form) { continue }
            $real = $form + "\"
            if ($mine.StartsWith($real, [StringComparison]::OrdinalIgnoreCase) -or
                $real.StartsWith($mine, [StringComparison]::OrdinalIgnoreCase)) {
                throw "The scratch folder overlaps a real place, so no test ran."
            }
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
# without it the main part would install or uninstall for real. Everything above the
# guard runs when the script is dot-sourced, so it may only define functions and
# assign values that run no command and call no .NET method but two known reads, and
# the parameters' default values may run only Join-Path; anything else there is
# refused too (#332 review).
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
    # The parameters' default values run too: there, Join-Path is the only command.
    $parameters = @()
    if ($ast.ParamBlock) { $parameters = @($ast.ParamBlock.Parameters | Where-Object { $_.DefaultValue }) }
    foreach ($parameter in $parameters) {
        if (-not (Test-ReadOnlyValue $parameter.DefaultValue @("Join-Path"))) {
            throw "$Path runs something above its dot-source guard, so it was not loaded."
        }
    }
    for ($i = 0; $i -lt $guard; $i++) {
        $statement = $statements[$i]
        if ($statement -is [Management.Automation.Language.FunctionDefinitionAst]) { continue }
        $plain = $statement -is [Management.Automation.Language.AssignmentStatementAst] -and
            (Test-ReadOnlyValue $statement.Right @())
        if (-not $plain) {
            throw "$Path runs something above its dot-source guard, so it was not loaded."
        }
    }
}

# True when evaluating $Value runs no command but those in $Commands, makes no script
# block and calls no .NET method but the reads the scripts use at the top
# ([Environment]::GetFolderPath, [Diagnostics.Stopwatch]::StartNew).
function Test-ReadOnlyValue($Value, [string[]]$Commands) {
    $reads = @("[Environment]::GetFolderPath", "[Diagnostics.Stopwatch]::StartNew")
    $found = $Value.Find({ param($n)
        ($n -is [Management.Automation.Language.CommandAst] -and $Commands -notcontains $n.GetCommandName()) -or
        $n -is [Management.Automation.Language.ScriptBlockExpressionAst] -or
        ($n -is [Management.Automation.Language.InvokeMemberExpressionAst] -and
         $reads -cnotcontains ($n.Expression.Extent.Text + "::" + $n.Member.Extent.Text)) }, $true)
    return -not $found
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

# Every place in a test file that could run a script other than by the checked load
# (#332 review), as "<line>: <text>": a dot-source other than
# ". (Confirm-Loadable ...)" followed by Set-ScratchTarget, any "&" call, a command
# named by its path (.\uninstall.ps1), and anything that starts a process or another
# PowerShell (powershell, pwsh, cmd, Start-Process, Invoke-Expression, jobs,
# Invoke-Command, Invoke-Item, [Diagnostics.Process], [PowerShell], script blocks made
# from text, InvokeScript). The file is parsed only, never run.
function Find-UncheckedRun([string]$Path) {
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { return @("0: the file does not parse") }
    $starters = @("powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe",
        "Start-Process", "saps", "start", "Invoke-Expression", "iex", "Start-Job", "sajb",
        "Start-ThreadJob", "Invoke-Command", "icm", "Invoke-Item", "ii")
    $found = @()
    foreach ($node in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.CommandAst] }, $true)) {
        $text = $node.Extent.Text
        $name = $node.GetCommandName()
        $bad = $false
        if ($node.InvocationOperator -eq [Management.Automation.Language.TokenKind]::Dot) {
            if ($text -ceq '. (Join-Path $PSScriptRoot "Isolation.ps1")') { continue }
            $bad = $true
            if ($text -clike ". (Confirm-Loadable *)") {
                $pipeline = $node.Parent
                $statements = @($pipeline.Parent.Statements)
                $at = [array]::IndexOf($statements, $pipeline)
                $bad = $at -lt 0 -or $at -ge ($statements.Count - 1) -or
                    $statements[$at + 1].Extent.Text -cne "Set-ScratchTarget"
            }
        } elseif ($node.InvocationOperator -eq [Management.Automation.Language.TokenKind]::Ampersand) {
            $bad = $true
        } elseif ($name -and ($starters -contains $name -or $name -match '[\\/]|\.(ps1|exe|cmd|bat)$')) {
            $bad = $true
        } elseif ($name -eq "New-Object" -and $text -match 'Process|PowerShell|Runspace') {
            $bad = $true
        }
        if ($bad) { $found += "$($node.Extent.StartLineNumber): $text" }
    }
    $types = '(^|\.)(Process|ProcessStartInfo|PowerShell|Runspace|RunspaceFactory|ScriptBlock)$'
    foreach ($node in $ast.FindAll({ param($n)
            ($n -is [Management.Automation.Language.TypeExpressionAst] -or
             $n -is [Management.Automation.Language.TypeConstraintAst]) -and
            $n.TypeName.FullName -match $types }, $true)) {
        $found += "$($node.Extent.StartLineNumber): $($node.Extent.Text)"
    }
    foreach ($node in $ast.FindAll({ param($n)
            $n -is [Management.Automation.Language.MemberExpressionAst] -and
            $n.Member.Extent.Text -match '^(InvokeScript|InvokeCommand|NewScriptBlock)$' }, $true)) {
        $found += "$($node.Extent.StartLineNumber): $($node.Extent.Text)"
    }
    return $found
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
