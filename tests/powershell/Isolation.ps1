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
# The runtime firewall rule (#111 B7b): the real rules' group and the policy key that
# install.ps1 reads. The tests point both at throwaway ones (Set-ScratchTarget) and
# stand in every Windows Firewall command (Enter-ScratchProfile), so no test adds,
# changes or removes a firewall rule, even on CI's elevated runner.
$script:RealFirewallGroup = "Unminute runtime"
$script:RealPolicyKey = "HKLM:\SOFTWARE\Policies\Unminute"
# The functions through which the scripts reach Windows Firewall, and only they.
$script:FirewallFunctions = @("Get-RuntimeRule", "Remove-RuntimeRule", "Add-RuntimeRule", "Get-FirewallProfileState")
# The Windows Firewall commands a test could run by mistake; each throws in a test
# unless the test stands it in with Mock.
$script:FirewallCommands = @("New-NetFirewallRule", "Remove-NetFirewallRule", "Set-NetFirewallRule",
    "Enable-NetFirewallRule", "Disable-NetFirewallRule", "Copy-NetFirewallRule", "Rename-NetFirewallRule",
    "Get-NetFirewallRule", "Get-NetFirewallApplicationFilter", "Get-NetFirewallProfile",
    "Set-NetFirewallProfile")
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

# This account's real TaskBar and StartMenu pin folders, inside its roaming folder from
# the shell. uninstall.ps1 -Offboard removes the pins of Unminute there (#111 B8b).
function Get-RealPinFolder {
    $roaming = (Get-RealPlace)[3]
    $pinned = Join-Path $roaming "Microsoft\Internet Explorer\Quick Launch\User Pinned"
    return @((Join-Path $pinned "TaskBar"), (Join-Path $pinned "StartMenu"))
}

# True when a pin's name, or its first megabyte read as ASCII or UTF-16 (either byte
# alignment), names Unminute in any case: a pin of the real install names its folder.
# Read only; nothing is shown.
function Test-UnminutePin([IO.FileInfo]$File) {
    if ($File.Name.IndexOf("unminute", [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
    try {
        $stream = [IO.File]::Open($File.FullName, "Open", "Read", "ReadWrite")
        try {
            $bytes = New-Object byte[] ([Math]::Min($stream.Length, 1MB))
            $count = $stream.Read($bytes, 0, $bytes.Length)
        } finally { $stream.Dispose() }
    } catch { return $false }
    $texts = @([Text.Encoding]::GetEncoding(28591).GetString($bytes, 0, $count),
               [Text.Encoding]::Unicode.GetString($bytes, 0, $count))
    if ($count -gt 1) { $texts += [Text.Encoding]::Unicode.GetString($bytes, 1, $count - 1) }
    foreach ($text in $texts) {
        if ($text.IndexOf("unminute", [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
    }
    return $false
}

# The real install as tools/run_pester.ps1 compares it before and after the tests (#332
# review): the install folder's and Start menu shortcut's times and attributes, the
# uninstall entry's values, and each pin of Unminute's name, size, time and attributes
# (Test-UnminutePin; other apps' pins are left out, so the user pinning something
# meanwhile is no change), then the machine install's (Get-RealMachineState, #111
# B6b). Compared in memory; never printed.
function Get-RealInstallState {
    $places = Get-RealPlace
    $state = @(foreach ($path in @($places[0], (Join-Path $places[2] "Unminute.lnk"))) {
        $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        if ($item) { "{0} {1}" -f $item.LastWriteTimeUtc.Ticks, $item.Attributes } else { "absent" }
    })
    $entry = Get-ItemProperty -LiteralPath $script:RealUninstallKey -ErrorAction SilentlyContinue
    if ($entry) {
        $state += (@($entry.PSObject.Properties | Where-Object { $_.Name -notlike "PS*" } |
            ForEach-Object { "$($_.Name)=$($_.Value)" }) -join "|")
    } else { $state += "absent" }
    foreach ($folder in Get-RealPinFolder) {
        $dir = [IO.DirectoryInfo]::new($folder)
        if (-not $dir.Exists) { $state += "absent"; continue }
        try {
            $pins = @(foreach ($file in $dir.GetFiles("*.lnk")) {
                if ($file.Extension -ieq ".lnk" -and (Test-UnminutePin $file)) {
                    "{0} {1} {2} {3}" -f $file.Name, $file.Length, $file.LastWriteTimeUtc.Ticks, $file.Attributes
                }
            })
            $state += (@($pins | Sort-Object) -join "|")
        } catch { $state += "unreadable" }
    }
    $state += Get-RealMachineState
    return ($state -join "`n")
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
    $script:ScratchProgramFiles = [IO.Directory]::CreateDirectory((Join-Path $script:ScratchRoot "ProgramFiles")).FullName
    $script:ScratchCommonPrograms = [IO.Directory]::CreateDirectory((Join-Path $script:ScratchRoot "CommonPrograms")).FullName
    $script:ScratchTop = $script:ScratchKeyPrefix + [guid]::NewGuid().ToString("N")
    $script:ScratchKey = $script:ScratchTop + "\Uninstall\Unminute"
    $script:ScratchMachineKey = $script:ScratchTop + "\Machine\Uninstall\Unminute"
    $script:ScratchEventSourceKey = $script:ScratchTop + "\Machine\EventLog\Unminute"
    $script:ScratchPolicyKey = $script:ScratchTop + "\Policies\Unminute"
    $script:ScratchFirewallGroup = "Unminute-PesterTest-" + [guid]::NewGuid().ToString("N")
    # Every Windows Firewall command throws until the tests end, unless a test stands it
    # in with Mock (#111 B7b). Each stand-in takes the real command's parameters (so a
    # Mock of it sees them as the script passes them) and runs nothing. Global, so the
    # scripts' functions find these first; removed by Exit-ScratchProfile.
    # Get-RealMachineState reads the real rules another way.
    foreach ($command in $script:FirewallCommands) {
        $binding = "[CmdletBinding()]"; $params = ""
        try {
            $meta = [Management.Automation.CommandMetadata]::new((Get-Command -Name "NetSecurity\$command" -ErrorAction Stop))
            $binding = [Management.Automation.ProxyCommand]::GetCmdletBindingAttribute($meta)
            $params = [Management.Automation.ProxyCommand]::GetParamBlock($meta)
        } catch { }
        Set-Item -Path "function:global:$command" -Value ("$binding param($params) " +
            "throw 'A test reached Windows Firewall without standing it in, so nothing was changed.'")
    }
}

function Exit-ScratchProfile {
    foreach ($command in $script:FirewallCommands) {
        if (Test-Path -LiteralPath "function:global:$command") { Remove-Item -LiteralPath "function:global:$command" }
    }
    $env:LOCALAPPDATA = $script:SavedLocal
    $env:APPDATA = $script:SavedRoaming
    if ($script:ScratchTop -and (Test-ScratchKey $script:ScratchTop) -and (Test-Path -LiteralPath $script:ScratchTop)) {
        Remove-Item -LiteralPath $script:ScratchTop -Recurse -Force
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
        ('$EventSourceKey = "' + $script:RealEventSourceKey + '"'),
        ('$FirewallGroup = "' + $script:RealFirewallGroup + '"'),
        ('$PolicyKey = "' + $script:RealPolicyKey + '"')
    )
    $present = @($lines | Where-Object { $top -ccontains $_ })
    # Windows Firewall (#111 B7b): its commands are named directly, each inside one of
    # $script:FirewallFunctions, which the tests stand in, and nowhere else (not even
    # in a string), and the rules' group only in its line at the top. Each is one of
    # $script:FirewallCommands by its plain name (#455): a name through its module
    # (NetSecurity\New-NetFirewallRule) would pass the stand-ins by, and a command not
    # on that list has none. netsh and the firewall's COM interface (HNetCfg) are
    # refused below.
    $firewall = @($ast.FindAll({ param($n)
        $n -is [Management.Automation.Language.CommandAst] -and [string]$n.GetCommandName() -match 'NetFirewall' }, $true))
    foreach ($command in $firewall) {
        if ($script:FirewallCommands -notcontains [string]$command.GetCommandName()) {
            throw "$Path reaches Windows Firewall other than through the commands the tests stand in, so it was not loaded."
        }
        $owner = $command.Parent
        while ($owner -and $owner -isnot [Management.Automation.Language.FunctionDefinitionAst]) { $owner = $owner.Parent }
        if (-not $owner -or $script:FirewallFunctions -cnotcontains $owner.Name) {
            throw "$Path reaches Windows Firewall other than through its firewall functions, so it was not loaded."
        }
    }
    # Each command's name once (a verb, then -NetFirewall); a parameter such as
    # -AssociatedNetFirewallRule is not a command.
    if (([regex]::Matches($code, "[a-z]-NetFirewall", "IgnoreCase")).Count -ne $firewall.Count) {
        throw "$Path reaches Windows Firewall other than through its firewall functions, so it was not loaded."
    }
    $reads = @('"HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$WebView2Client"',
               '"HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$WebView2Client"',
               '$base.OpenSubKey($subKey)')
    $reach = @("GetFolderPath", "SpecialFolder", "CurrentVersion\Uninstall", "Services\EventLog", "HKLM:",
               "HKEY_", "Registry::", "Start Menu\", "env:Program", "env:ALLUSERSPROFILE", "env:PUBLIC",
               "CreateSubKey", "DeleteSubKey", "DeleteValue", "OpenSubKey", "EventSource(", "New-EventLog",
               "Remove-EventLog", $script:RealFirewallGroup, "netsh", "HNetCfg")
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

# Which places a script has, by its name, for Assert-ScratchTarget (#111 B7a): the
# installer and uninstaller have every one; the model package's Install-Models.ps1
# reaches only Program Files; New-ModelPackage.ps1, IT's own tool, reaches none. Any
# other name is the installer's kind, so it gets every check.
function Get-ScriptKind([string]$Path) {
    switch -CaseSensitive ([IO.Path]::GetFileName($Path)) {
        "Install-Models.ps1" { return "models" }
        "New-ModelPackage.ps1" { return "tool" }
        default { return "installer" }
    }
}

# Every check before a script is dot-sourced; returns its path for the dot-source.
function Confirm-Loadable([string]$Path) {
    if (-not (Test-ScratchPath $env:LOCALAPPDATA) -or -not (Test-ScratchPath $env:APPDATA)) {
        throw "LOCALAPPDATA or APPDATA is not scratch, so $Path was not loaded."
    }
    Assert-DotSourceGuard $Path
    Assert-RedirectableTarget $Path
    $script:LoadedKind = Get-ScriptKind $Path
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
    Set-Variable -Name PolicyKey -Value $script:ScratchPolicyKey -Scope 1
    Set-Variable -Name FirewallGroup -Value $script:ScratchFirewallGroup -Scope 1
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
    # The runtime firewall rules (#111 B7b): each rule in their real group, its name,
    # program and whether it is on, read through Windows Firewall's COM interface (a
    # tenth of a second, where Get-NetFirewallRule takes seconds, and past the tests'
    # stand-ins for the firewall commands).
    try {
        $policy = New-Object -ComObject HNetCfg.FwPolicy2
        $rules = @($policy.Rules | Where-Object { $_.Grouping -eq $script:RealFirewallGroup } |
            ForEach-Object { "$($_.Name) $($_.ApplicationName) $($_.Enabled)" } | Sort-Object)
        $state += ($rules -join "|")
    } catch { $state += "unreadable" }
    return ($state -join "`n")
}

# Reads the loaded script's variables as the script's own functions would see them,
# every place the kind of script loaded last has (Get-ScriptKind).
function Assert-ScratchTarget {
    $places = [ordered]@{ "LOCALAPPDATA" = $env:LOCALAPPDATA; "APPDATA" = $env:APPDATA }
    # Set-ScratchTarget makes the policy key and the firewall group scratch for every
    # kind, so both are checked for every kind.
    $keys = [ordered]@{ "the policy key" = [string]$PolicyKey }
    if ($script:LoadedKind -ne "tool") { $places["Program Files"] = $ProgramFilesFolder }
    if ($script:LoadedKind -notin @("tool", "models")) {
        $places["the default -InstallDir"] = $InstallDir
        $places["the install folder"] = $Options.InstallDir
        $places["the Start menu folder"] = $ProgramsFolder
        $places["the all-users Start menu folder"] = $CommonProgramsFolder
        $keys["the uninstall key"] = [string]$RegKey
        $keys["the machine uninstall key"] = [string]$MachineRegKey
        $keys["the Event Log source key"] = [string]$EventSourceKey
    }
    foreach ($name in $places.Keys) {
        if (-not (Test-ScratchPath $places[$name])) { throw "Not scratch: $name. Nothing was run." }
    }
    foreach ($name in $keys.Keys) {
        if (-not (Test-ScratchKey $keys[$name])) { throw "Not scratch: $name. Nothing was run." }
    }
    # The runtime firewall rules' group (#111 B7b): this run's throwaway one.
    if ([string]$FirewallGroup -cne $script:ScratchFirewallGroup -or
        -not ([string]$FirewallGroup).StartsWith("Unminute-PesterTest-", [StringComparison]::Ordinal)) {
        throw "Not scratch: the firewall group. Nothing was run."
    }
}
