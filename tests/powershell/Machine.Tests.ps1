# Pester 5 tests of machine mode (#111 B6b): install.ps1 -Machine and uninstall.ps1
# -Machine. Run them with tools/run_pester.ps1 under Windows PowerShell 5.1.
#
# Nothing here needs administrator rights or touches this computer's install: each
# script is dot-sourced (after Assert-DotSourceGuard has found its guard, as in
# Installer.Tests.ps1), and the parts that would reach the computer are stood in by
# Pester mocks: elevation and bitness (Test-Elevated, Test-64BitProcess), Program
# Files and the all-users Start menu (folders in TestDrive), the HKLM uninstall entry
# and the Event Log source key (keys in Pester's TestRegistry, under HKCU), the
# folder's owner and permissions as read (Get-FolderSecurity, a list built in
# memory with Get-MachineRootSecurity), the folder's creation with them
# (New-MachineRoot, a plain folder), prerequisites and running processes.
# Stop-Install and Stop-Uninstall throw "STOP <code>". LOCALAPPDATA and APPDATA point
# into TestDrive. Packages are synthetic. ASCII only (Windows PowerShell 5.1 reads a
# script without a byte-order mark in the system code page).

BeforeAll {
    $script:SavedLocal = $env:LOCALAPPDATA
    $script:SavedRoaming = $env:APPDATA
    $env:LOCALAPPDATA = [IO.Directory]::CreateDirectory((Join-Path $TestDrive "Local")).FullName
    $env:APPDATA = [IO.Directory]::CreateDirectory((Join-Path $TestDrive "Roaming")).FullName
    $script:Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

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

    # A new, unique folder in TestDrive.
    function New-TestFolder([string]$Prefix) {
        return [IO.Directory]::CreateDirectory((Join-Path $TestDrive ($Prefix + [guid]::NewGuid().ToString("N").Substring(0, 8)))).FullName
    }

    # A synthetic package zip, Unminute-<version>.zip with its .sha256 file, as the
    # build makes one: version.json (with the storage format, unless $Schema is 0),
    # the uninstaller, the icon, a stand-in runtime and package-manifest.json.
    # $Break: "hash" lists a wrong SHA-256 for one file.
    function New-TestZip([string]$Version, [int]$Schema = 2, [string]$Break = "") {
        $build = New-TestFolder "b"
        $folder = Join-Path $build "Unminute-$Version"
        $info = [ordered]@{ name = "Unminute"; version = $Version }
        if ($Schema) { $info.schema = $Schema }
        $files = [ordered]@{
            "version.json" = ($info | ConvertTo-Json)
            "uninstall.ps1" = "# the uninstaller of $Version"
            "app/unminute.ico" = "icon"
            "app/workspace/__main__.py" = "# $Version"
            "runtime/pythonw.exe" = "synthetic $Version"
        }
        $manifest = [ordered]@{}
        foreach ($name in $files.Keys) {
            $path = Join-Path $folder ($name -replace "/", "\")
            [void][IO.Directory]::CreateDirectory((Split-Path -Parent $path))
            [IO.File]::WriteAllText($path, $files[$name])
            $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
            if ($Break -eq "hash" -and $name -eq "runtime/pythonw.exe") { $hash = "0" * 64 }
            $manifest[$name] = @((Get-Item -LiteralPath $path).Length, $hash)
        }
        [IO.File]::WriteAllText((Join-Path $folder "package-manifest.json"), ($manifest | ConvertTo-Json -Depth 3))
        $zip = Join-Path (New-TestFolder "z") "Unminute-$Version.zip"
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::CreateFromDirectory($build, $zip)
        $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
        [IO.File]::WriteAllText("$zip.sha256", "$digest  Unminute-$Version.zip`n")
        return $zip
    }

    # Every path under $Folder (never through a link), with each file's text.
    function Get-Snapshot([string]$Folder) {
        $found = [ordered]@{}
        if (-not [IO.Directory]::Exists($Folder)) { return "" }
        $todo = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
        $todo.Push([IO.DirectoryInfo]::new($Folder))
        $skip = $Folder.TrimEnd("\").Length + 1
        while ($todo.Count) {
            foreach ($item in ($todo.Pop().GetFileSystemInfos() | Sort-Object FullName)) {
                $rel = $item.FullName.Substring($skip)
                if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { $found[$rel] = "<link>" }
                elseif ($item -is [IO.DirectoryInfo]) { $found[$rel] = "<dir>"; $todo.Push($item) }
                else { $found[$rel] = [IO.File]::ReadAllText($item.FullName) }
            }
        }
        return (($found.Keys | Sort-Object | ForEach-Object { "$_=$($found[$_])" }) -join "|")
    }
}

AfterAll {
    $env:LOCALAPPDATA = $script:SavedLocal
    $env:APPDATA = $script:SavedRoaming
}

Describe "install.ps1 -Machine" {
    BeforeAll {
        $script:Install = Join-Path $script:Repo "packaging\install.ps1"
        Assert-DotSourceGuard $script:Install
        . $script:Install
        Mock Stop-Install { throw "STOP $Code" }
        Mock Test-Elevated { $true }
        Mock Test-64BitProcess { $true }
        Mock Test-Prerequisite { }
        Mock Get-ProgramFilesFolder { $script:ProgramFiles }
        Mock Get-CommonProgramsFolder { $script:CommonPrograms }
        Mock Get-MachineRegKey { $script:RegPath }
        Mock Get-EventSourceKey { $script:EventPath }
        Mock New-MachineRoot { [void][IO.Directory]::CreateDirectory($Root) }
        Mock Get-FolderSecurity { Get-MachineRootSecurity }
        Mock Get-Process { }
        Mock Test-WorkspaceGate { throw "Python ran" }

        # One machine install of $Zip (the main part's machine branch and its finally).
        function Invoke-TestInstall([string]$Zip, [hashtable]$With = @{}) {
            $Options.Package = $Zip
            foreach ($k in $With.Keys) { $Options[$k] = $With[$k] }
            try { Invoke-MachineInstall } finally { Clear-InstallRun }
        }

        function Get-Record { Get-Content -LiteralPath (Join-Path $script:Root "current.json") -Raw | ConvertFrom-Json }

        function Get-Kept {
            return ((@([IO.DirectoryInfo]::new((Join-Path $script:Root "versions")).GetDirectories() |
                ForEach-Object Name) | Sort-Object) -join ",")
        }

        function Get-Current { return (Split-Path -Leaf (Get-LinkTarget (Join-Path $script:Root "current"))) }
    }

    BeforeEach {
        $script:ProgramFiles = New-TestFolder "pf"
        $script:CommonPrograms = New-TestFolder "cp"
        $script:Root = Join-Path $script:ProgramFiles "Unminute"
        $suffix = [guid]::NewGuid().ToString("N").Substring(0, 8)
        $script:RegPath = "TestRegistry:\u$suffix"
        $script:EventPath = "TestRegistry:\e$suffix"
        $Options.Machine = $true; $Options.Silent = $true; $Options.SkipShortcut = $false
        $Options.InstallDirGiven = $false; $Options.SelfService = $false; $Options.Managed = $false
        $Options.Rollback = $false; $Options.RemoveSlot = ""; $Options.Package = ""
        $script:LogFile = $null; $script:LogVersion = "-"; $script:LogError = $null
    }

    AfterEach {
        Clear-InstallRun
        $Options.Machine = $false
    }

    Context "where and how it may run" {
        It "refuses <Label> with exit 2 before anything is written" -TestCases @(
            @{ Label = "a 32-bit Windows PowerShell"; Kind = "32bit" }
            @{ Label = "a run without administrator rights"; Kind = "standard" }
            @{ Label = "-InstallDir"; Kind = "installdir" }
            @{ Label = "-SelfService"; Kind = "selfservice" }
            @{ Label = "no package zip"; Kind = "nopackage" }
            @{ Label = "-Rollback with -RemoveSlot"; Kind = "both" }
        ) {
            param($Label, $Kind)
            $null = $Label
            $zip = New-TestZip "1.0.0"
            switch ($Kind) {
                "32bit" { Mock Test-64BitProcess { $false } }
                "standard" { Mock Test-Elevated { $false } }
                "installdir" { $Options.InstallDirGiven = $true }
                "selfservice" { $Options.SelfService = $true }
                "nopackage" { $zip = "" }
                "both" { $Options.Rollback = $true; $Options.RemoveSlot = "1.0.0"; $zip = "" }
            }
            { Invoke-TestInstall $zip } | Should -Throw "STOP 2"
            [IO.Directory]::Exists($script:Root) | Should -BeFalse
            Test-Path -LiteralPath $script:RegPath | Should -BeFalse
        }

        It "names the 64-bit Windows PowerShell to use when the run is 32-bit" {
            Mock Test-64BitProcess { $false }
            Mock Stop-Install { throw "STOP $Code $Message" }
            { Invoke-TestInstall (New-TestZip "1.0.0") } | Should -Throw "*Sysnative\WindowsPowerShell\v1.0\powershell.exe*"
        }

        It "refuses a package that states no storage format (exit 2) and keeps nothing of it" {
            { Invoke-TestInstall (New-TestZip "1.0.0" -Schema 0) } | Should -Throw "STOP 2"
            [IO.Directory]::Exists((Join-Path $script:Root "versions\1.0.0")) | Should -BeFalse
            @([IO.DirectoryInfo]::new($script:Root).GetDirectories("staging-*")).Count | Should -Be 0
        }

        It "fits Windows PowerShell's path limit while it unpacks in Program Files" {
            # The real folder's longest form: staging, the package's folder with a
            # 16-character version, and the build's longest path (140 characters).
            $longest = "C:\Program Files\Unminute\staging-" + ("0" * 32) + "\Unminute-" + ("9" * 16) + "\" + ("x" * 140)
            $longest.Length | Should -BeLessThan 260
            ("C:\Program Files\Unminute\versions\" + ("9" * 16) + ".partial\" + ("x" * 140)).Length | Should -BeLessThan 260
        }
    }

    Context "a first install" {
        It "installs to Program Files and registers everything there, without Python or a user profile" {
            Invoke-TestInstall (New-TestZip "1.0.0")
            Should -Invoke New-MachineRoot -Times 1 -Exactly
            Should -Invoke Test-WorkspaceGate -Times 0 -Exactly
            Get-Kept | Should -BeExactly "1.0.0"
            Get-Current | Should -BeExactly "1.0.0"
            (Get-LinkTarget (Join-Path $script:Root "current")) | Should -BeExactly (Join-Path $script:Root "versions\1.0.0")
            $record = Get-Record
            $record.format | Should -Be 2
            $record.mode | Should -BeExactly "machine"
            $record.managed | Should -BeTrue
            $record.version | Should -BeExactly "1.0.0"
            $record.slots."1.0.0".schema | Should -Be 2
            $record.slots."1.0.0".seq | Should -Be 1
            [IO.File]::ReadAllText((Join-Path $script:Root "uninstall.ps1")) | Should -BeExactly "# the uninstaller of 1.0.0"
            # Nothing unpacked is left, and nothing went to a temporary folder.
            @([IO.DirectoryInfo]::new($script:Root).GetDirectories("staging-*")).Count | Should -Be 0
            # The all-users Start menu entry, through the junction.
            $shell = New-Object -ComObject WScript.Shell
            $link = $shell.CreateShortcut((Join-Path $script:CommonPrograms "Unminute.lnk"))
            $link.TargetPath | Should -BeExactly (Join-Path $script:Root "current\runtime\pythonw.exe")
            $link.Arguments | Should -BeExactly $LaunchArgs
            $link.WorkingDirectory | Should -BeExactly (Join-Path $script:Root "current\app")
            # The uninstall entry and the Event Log source.
            $entry = Get-ItemProperty -LiteralPath $script:RegPath
            $entry.DisplayVersion | Should -BeExactly "1.0.0"
            $entry.InstallLocation | Should -BeExactly $script:Root
            $entry.UninstallString | Should -BeExactly "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script:Root\uninstall.ps1`" -Machine"
            $entry.QuietUninstallString | Should -BeExactly "$($entry.UninstallString) -Silent"
            $entry.DisplayIcon | Should -BeExactly (Join-Path $script:Root "current\app\unminute.ico")
            $source = Get-ItemProperty -LiteralPath $script:EventPath
            Split-Path -Leaf $source.EventMessageFile | Should -BeExactly "EventLogMessages.dll"
            (Get-Item -LiteralPath $script:EventPath).GetValueKind("EventMessageFile") | Should -Be "ExpandString"
            # The log says machine, and names no user or path.
            $log = [IO.File]::ReadAllText((Join-Path $script:Root "logs\install.log"))
            $log | Should -Match "mode=machine version=1\.0\.0 step=done result=ok code=0"
            $log | Should -Not -Match ([regex]::Escape($env:USERNAME))
            $log | Should -Not -Match '\\'
        }

        It "unpacks, checks and installs in that order, and never clears the Mark of the Web" {
            $tokens = $null; $errors = $null
            $ast = [Management.Automation.Language.Parser]::ParseFile($script:Install, [ref]$tokens, [ref]$errors)
            $body = $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and
                $n.Name -eq "Invoke-MachineInstall" }, $true)[0]
            [string[]]$calls = @($body.Body.FindAll({ param($n) $n -is [Management.Automation.Language.CommandAst] }, $true) |
                ForEach-Object { $_.Extent.Text })
            $order = @('Assert-MachineContext', 'Assert-InstallFolder $root', 'Start-MachineRun "start"', 'Assert-NoLink $package',
                       'New-MachineStagedCopy $package $version', 'Install-Version $staging $version', 'Set-CurrentLink $version',
                       'Set-MachineVersion $version $previous $version')
            $at = @($order | ForEach-Object { [array]::IndexOf($calls, $_) })
            $at | Should -Not -Contain -1
            ($at | Sort-Object) -join "," | Should -BeExactly ($at -join ",")
            $staged = $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and
                $n.Name -eq "New-MachineStagedCopy" }, $true)[0].Body.EndBlock.Statements
            $staged[$staged.Count - 2].Extent.Text | Should -BeExactly 'Test-PackageFile $staging'
            foreach ($name in "Invoke-MachineInstall", "New-MachineStagedCopy", "Invoke-MachineRollback", "Set-MachineVersion") {
                $function = $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and
                    $n.Name -eq $name }, $true)[0].Extent.Text
                $function | Should -Not -Match "Unblock-File|Test-WorkspaceGate|GetTempPath|python\.exe"
            }
        }

        It "does not use a staging folder that already exists (exit 1), and leaves it as it is" {
            Mock New-RandomName { "0" * 32 }
            Mock Remove-StagingLeftover { }
            [void][IO.Directory]::CreateDirectory($script:Root)
            $taken = [IO.Directory]::CreateDirectory((Join-Path $script:Root ("staging-" + ("0" * 32)))).FullName
            [IO.File]::WriteAllText((Join-Path $taken "sentinel.txt"), "someone's")
            { Invoke-TestInstall (New-TestZip "1.0.0") } | Should -Throw "STOP 1"
            [IO.File]::ReadAllText((Join-Path $taken "sentinel.txt")) | Should -BeExactly "someone's"
            [IO.Directory]::Exists((Join-Path $script:Root "versions\1.0.0")) | Should -BeFalse
        }

        It "removes the staging folders a stopped run left" {
            [void][IO.Directory]::CreateDirectory($script:Root)
            $left = [IO.Directory]::CreateDirectory((Join-Path $script:Root ("staging-" + ("a" * 32)))).FullName
            [IO.File]::WriteAllText((Join-Path $left "half.txt"), "x")
            Invoke-TestInstall (New-TestZip "1.0.0")
            [IO.Directory]::Exists($left) | Should -BeFalse
        }

        It "removes a copy that fails its manifest (exit 4) and changes nothing else" {
            Invoke-TestInstall (New-TestZip "1.0.0")
            $before = Get-Snapshot $script:Root
            { Invoke-TestInstall (New-TestZip "2.0.0" -Break "hash") } | Should -Throw "STOP 4"
            Clear-InstallRun
            (Get-Snapshot $script:Root) -replace '\|?logs\\install\.log=[^|]*', '' |
                Should -BeExactly ($before -replace '\|?logs\\install\.log=[^|]*', '')
            Get-Current | Should -BeExactly "1.0.0"
        }

        It "refuses with exit 3 while the app runs for anyone, and changes nothing" {
            Invoke-TestInstall (New-TestZip "1.0.0")
            $before = Get-Snapshot $script:Root
            Mock Get-Process { [pscustomobject]@{ Path = "$script:Root\versions\1.0.0\runtime\pythonw.exe" } }
            { Invoke-TestInstall (New-TestZip "2.0.0") } | Should -Throw "STOP 3"
            (Get-Snapshot $script:Root) -replace '\|?logs\\install\.log=[^|]*', '' |
                Should -BeExactly ($before -replace '\|?logs\\install\.log=[^|]*', '')
        }
    }

    Context "the install folder's permissions" {
        It "builds the folder's permissions as specified" {
            $security = Get-MachineRootSecurity
            $security.GetOwner([Security.Principal.SecurityIdentifier]).Value | Should -BeExactly "S-1-5-32-544"
            $security.AreAccessRulesProtected | Should -BeTrue
            $rules = @($security.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
                ForEach-Object { "$($_.IdentityReference.Value) $($_.FileSystemRights) $($_.InheritanceFlags) $($_.AccessControlType)" } |
                Sort-Object)
            $rules -join "|" | Should -BeExactly (@(
                "S-1-15-2-1 ReadAndExecute, Synchronize ContainerInherit, ObjectInherit Allow",
                "S-1-5-18 FullControl ContainerInherit, ObjectInherit Allow",
                "S-1-5-32-544 FullControl ContainerInherit, ObjectInherit Allow",
                "S-1-5-32-545 ReadAndExecute, Synchronize ContainerInherit, ObjectInherit Allow") -join "|")
            Test-MachineRootSecurity $security | Should -BeTrue
        }

        It "reads back as specified when a folder is created with them" {
            # Owned by this test's own account, which it may set; Windows then holds
            # exactly the four entries.
            $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
            $folder = Join-Path (New-TestFolder "acl") "Unminute"
            [void][IO.Directory]::CreateDirectory($folder, (Get-MachineRootSecurity $me))
            try {
                $read = [IO.DirectoryInfo]::new($folder).GetAccessControl()
                Test-MachineRootSecurity $read -Owners @($me) | Should -BeTrue
                Test-MachineRootSecurity $read | Should -BeFalse        # not Administrators or SYSTEM
            } finally {
                # Give this account its access back, so TestDrive can be removed.
                $undo = [IO.DirectoryInfo]::new($folder).GetAccessControl()
                $undo.SetAccessRuleProtection($false, $false)
                $undo.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                    [Security.Principal.SecurityIdentifier]::new($me), "FullControl", "ContainerInherit, ObjectInherit", "None", "Allow"))
                [IO.DirectoryInfo]::new($folder).SetAccessControl($undo)
            }
        }

        It "refuses <Label> (exit 4) before anything is written" -TestCases @(
            @{ Label = "another owner"; Kind = "owner" }
            @{ Label = "an extra entry"; Kind = "extra" }
            @{ Label = "a list that inherits"; Kind = "inherits" }
            @{ Label = "an explicit entry on a folder beneath it"; Kind = "beneath" }
            @{ Label = "a protected list on a folder beneath it"; Kind = "protected" }
            # Files too (review of 4c0daf2): the uninstaller SYSTEM runs, and a kept version's files.
            @{ Label = "an explicit entry on the uninstaller in the install folder"; Kind = "topfile" }
            @{ Label = "an explicit entry on a file of a kept version"; Kind = "keptfile" }
            @{ Label = "a protected list on a file of a kept version"; Kind = "keptprotected" }
        ) {
            param($Label, $Kind)
            $null = $Label
            Invoke-TestInstall (New-TestZip "1.0.0")
            $me = [Security.Principal.WindowsIdentity]::GetCurrent().User
            $sub = [IO.DirectoryInfo]::new((Join-Path $script:Root "versions\1.0.0\app"))
            switch ($Kind) {
                "owner" { Mock Get-FolderSecurity { Get-MachineRootSecurity "S-1-5-32-545" } }
                "extra" {
                    Mock Get-FolderSecurity {
                        $s = Get-MachineRootSecurity
                        $s.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                            [Security.Principal.SecurityIdentifier]::new("S-1-5-11"), "Modify", "ContainerInherit, ObjectInherit", "None", "Allow"))
                        $s
                    }
                }
                "inherits" { Mock Get-FolderSecurity { $s = Get-MachineRootSecurity; $s.SetAccessRuleProtection($false, $true); $s } }
                "beneath" {
                    $acl = $sub.GetAccessControl()
                    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($me, "Modify", "Allow"))
                    $sub.SetAccessControl($acl)
                }
                "protected" {
                    $acl = $sub.GetAccessControl()
                    $acl.SetAccessRuleProtection($true, $true)
                    $sub.SetAccessControl($acl)
                }
                { $_ -in "topfile", "keptfile" } {
                    $rel = if ($Kind -eq "topfile") { "uninstall.ps1" } else { "versions\1.0.0\runtime\pythonw.exe" }
                    $file = [IO.FileInfo]::new((Join-Path $script:Root $rel))
                    $acl = $file.GetAccessControl()
                    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($me, "Modify", "Allow"))
                    $file.SetAccessControl($acl)
                }
                "keptprotected" {
                    $file = [IO.FileInfo]::new((Join-Path $script:Root "versions\1.0.0\app\unminute.ico"))
                    $acl = $file.GetAccessControl()
                    $acl.SetAccessRuleProtection($true, $true)
                    $file.SetAccessControl($acl)
                }
            }
            $before = Get-Snapshot $script:Root
            { Invoke-TestInstall (New-TestZip "2.0.0") } | Should -Throw "STOP 4"
            Get-Snapshot $script:Root | Should -BeExactly $before
        }

        It "refuses a file of the copy with permissions of its own (exit 4)" {
            $folder = New-TestFolder "own"
            [IO.File]::WriteAllText((Join-Path $folder "a.txt"), "a")
            $file = [IO.FileInfo]::new((Join-Path $folder "a.txt"))
            Get-PackageListing $folder | Should -Not -BeNullOrEmpty
            $acl = $file.GetAccessControl()
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                [Security.Principal.WindowsIdentity]::GetCurrent().User, "Modify", "Allow"))
            $file.SetAccessControl($acl)
            { Get-PackageListing $folder } | Should -Throw "STOP 4"
            $Options.Machine = $false
            Get-PackageListing $folder | Should -Not -BeNullOrEmpty     # per-user mode does not check
        }
    }

    Context "versions, the junction and current.json" {
        It "keeps the current, the previous and the newest version of the format immediately before" {
            $steps = @(
                @("1.0.0", 1, "1.0.0", ""),
                @("1.1.0", 1, "1.0.0,1.1.0", "1.0.0"),
                @("2.0.0", 2, "1.1.0,2.0.0", "1.1.0"),
                @("2.1.0", 2, "1.1.0,2.0.0,2.1.0", "2.0.0"),
                # The next storage-format change replaces the kept version (bounded disk).
                @("3.0.0", 3, "2.1.0,3.0.0", "2.1.0"))
            foreach ($step in $steps) {
                Invoke-TestInstall (New-TestZip $step[0] -Schema $step[1])
                Get-Kept | Should -BeExactly $step[2] -Because "after $($step[0])"
                Get-Current | Should -BeExactly $step[0]
                $record = Get-Record
                [string]$record.previous | Should -BeExactly $step[3]
                ((@($record.slots.PSObject.Properties.Name) | Sort-Object) -join ",") | Should -BeExactly $step[2]
            }
            $record = Get-Record
            $record.slots."2.1.0".seq | Should -Be 4
            $record.slots."3.0.0".seq | Should -Be 5
            $record.slots."2.1.0".schema | Should -Be 2
        }

        It "keeps a kept version of an older format that is not the previous one" {
            Invoke-TestInstall (New-TestZip "1.0.0" -Schema 1)
            Invoke-TestInstall (New-TestZip "2.0.0" -Schema 2)
            Invoke-TestInstall (New-TestZip "2.1.0" -Schema 2)
            Invoke-TestInstall (New-TestZip "2.2.0" -Schema 2)
            Get-Kept | Should -BeExactly "1.0.0,2.1.0,2.2.0"
        }

        It "keeps no older version when the format immediately before the current one is not installed" {
            # The owner's decision of 22 October 2026, confirmed 23 October: current
            # minus one only, never an older format in its place.
            Invoke-TestInstall (New-TestZip "2.0.0" -Schema 2)
            Invoke-TestInstall (New-TestZip "2.1.0" -Schema 2)
            Invoke-TestInstall (New-TestZip "4.0.0" -Schema 4)
            Get-Kept | Should -BeExactly "2.1.0,4.0.0"          # 2.1.0 stays only as the previous version
            (Get-Record).previous | Should -BeExactly "2.1.0"
            Invoke-TestInstall (New-TestZip "4.1.0" -Schema 4)
            Get-Kept | Should -BeExactly "4.0.0,4.1.0"
            ((@((Get-Record).slots.PSObject.Properties.Name) | Sort-Object) -join ",") | Should -BeExactly "4.0.0,4.1.0"
            # With format 3 installed, its newest version is the one kept.
            Invoke-TestInstall (New-TestZip "3.0.0" -Schema 3)
            Invoke-TestInstall (New-TestZip "4.2.0" -Schema 4)
            Get-Kept | Should -BeExactly "3.0.0,4.2.0"
        }

        It "removes every newer storage format when an older one is installed (strict downgrade)" {
            Invoke-TestInstall (New-TestZip "2.0.0" -Schema 2)
            Invoke-TestInstall (New-TestZip "3.0.0" -Schema 3)
            Invoke-TestInstall (New-TestZip "2.0.1" -Schema 2)
            Get-Kept | Should -BeExactly "2.0.0,2.0.1"
            Get-Current | Should -BeExactly "2.0.1"
            # 3.0.0 cannot be the previous version, so the earlier previous one stays.
            (Get-Record).previous | Should -BeExactly "2.0.0"
            ((@((Get-Record).slots.PSObject.Properties.Name) | Sort-Object) -join ",") | Should -BeExactly "2.0.0,2.0.1"
        }

        It "renames a version it no longer keeps before removing it, and keeps one it cannot rename for the next install" {
            Invoke-TestInstall (New-TestZip "1.0.0")
            Invoke-TestInstall (New-TestZip "1.1.0")
            $held = [IO.File]::Open((Join-Path $script:Root "versions\1.0.0\runtime\pythonw.exe"), "Open", "Read", "None")
            Set-Variable -Name LockRetrySeconds -Value 1     # this test's wait, read by Invoke-LockRetry
            try { Invoke-TestInstall (New-TestZip "1.2.0") } finally { $held.Dispose() }
            Get-Kept | Should -BeExactly "1.0.0,1.1.0,1.2.0"
            ((@((Get-Record).slots.PSObject.Properties.Name) | Sort-Object) -join ",") | Should -BeExactly "1.1.0,1.2.0"
            Invoke-TestInstall (New-TestZip "1.3.0")
            Get-Kept | Should -BeExactly "1.2.0,1.3.0"
        }

        It "repairs the same version without copying when it still matches its manifest" {
            Invoke-TestInstall (New-TestZip "1.0.0")
            Remove-Item -LiteralPath (Join-Path $script:CommonPrograms "Unminute.lnk")
            Mock New-MachineStagedCopy { throw "copied" }
            Invoke-TestInstall (New-TestZip "1.0.0")
            Test-Path -LiteralPath (Join-Path $script:CommonPrograms "Unminute.lnk") | Should -BeTrue
            (Get-Record).slots."1.0.0".seq | Should -Be 1
            Get-Current | Should -BeExactly "1.0.0"
        }

        It "replaces the same version through a verified copy when it no longer matches" {
            Invoke-TestInstall (New-TestZip "1.0.0")
            [IO.File]::WriteAllText((Join-Path $script:Root "versions\1.0.0\runtime\pythonw.exe"), "changed")
            Invoke-TestInstall (New-TestZip "1.0.0")
            [IO.File]::ReadAllText((Join-Path $script:Root "versions\1.0.0\runtime\pythonw.exe")) | Should -BeExactly "synthetic 1.0.0"
            Get-PackageProblem (Join-Path $script:Root "versions\1.0.0") | Should -BeNullOrEmpty
        }

        It "puts right a switch that stopped part way, from current.json, removing links only as links" {
            Invoke-TestInstall (New-TestZip "1.0.0")
            Invoke-TestInstall (New-TestZip "2.0.0")
            $Options.InstallDir = $script:Root
            # Stopped after current became current.old, with current.new made.
            [IO.Directory]::Move((Join-Path $script:Root "current"), (Join-Path $script:Root "current.old"))
            New-Item -ItemType Junction -Path (Join-Path $script:Root "current.new") -Target (Join-Path $script:Root "versions\1.0.0") | Out-Null
            Repair-CurrentLink
            Get-Current | Should -BeExactly "2.0.0"
            Test-Path -LiteralPath (Join-Path $script:Root "current.old") | Should -BeFalse
            Test-Path -LiteralPath (Join-Path $script:Root "current.new") | Should -BeFalse
            Get-PackageProblem (Join-Path $script:Root "versions\1.0.0") | Should -BeNullOrEmpty
            Get-PackageProblem (Join-Path $script:Root "versions\2.0.0") | Should -BeNullOrEmpty
            # And a junction pointing at the wrong version is pointed back.
            Set-CurrentLink "1.0.0"
            Repair-CurrentLink
            Get-Current | Should -BeExactly "2.0.0"
        }

        It "switches the junction in order: current.new, current to current.old, current.new to current" {
            Invoke-TestInstall (New-TestZip "1.0.0")
            Invoke-TestInstall (New-TestZip "2.0.0")
            $Options.InstallDir = $script:Root
            $script:Seen = @()
            Mock Remove-Junction { $script:Seen += "remove " + (Split-Path -Leaf $Path); if ([IO.Directory]::Exists($Path)) { [IO.Directory]::Delete($Path, $false) } }
            Set-CurrentLink "1.0.0"
            $script:Seen -join "," | Should -BeExactly "remove current.new,remove current.old,remove current.old"
            Get-Current | Should -BeExactly "1.0.0"
            @([IO.DirectoryInfo]::new($script:Root).GetDirectories("current*") | ForEach-Object Name) -join "," | Should -BeExactly "current"
        }

        It "refuses a link it did not make where its junctions go (exit 2)" {
            Invoke-TestInstall (New-TestZip "1.0.0")
            $outside = New-TestFolder "out"
            [IO.Directory]::Delete((Join-Path $script:Root "current"), $false)
            New-Item -ItemType Junction -Path (Join-Path $script:Root "current") -Target $outside | Out-Null
            { Invoke-TestInstall (New-TestZip "2.0.0") } | Should -Throw "STOP 2"
            Get-LinkTarget (Join-Path $script:Root "current") | Should -BeExactly $outside
        }
    }

    Context "rollback and removing a kept version" {
        It "-Rollback makes the previous version current again, keeping both when the format is the same" {
            Invoke-TestInstall (New-TestZip "1.0.0")
            Invoke-TestInstall (New-TestZip "1.1.0")
            Invoke-TestInstall "" @{ Rollback = $true }
            Get-Current | Should -BeExactly "1.0.0"
            $record = Get-Record
            $record.version | Should -BeExactly "1.0.0"
            $record.previous | Should -BeExactly "1.1.0"
            $record.mode | Should -BeExactly "machine"
            $record.format | Should -Be 2
            Get-Kept | Should -BeExactly "1.0.0,1.1.0"
            (Get-ItemProperty -LiteralPath $script:RegPath).DisplayVersion | Should -BeExactly "1.0.0"
        }

        It "-Rollback to an older storage format removes the newer one (strict)" {
            Invoke-TestInstall (New-TestZip "1.0.0" -Schema 1)
            Invoke-TestInstall (New-TestZip "2.0.0" -Schema 2)
            Invoke-TestInstall "" @{ Rollback = $true }
            Get-Kept | Should -BeExactly "1.0.0"
            (Get-Record).previous | Should -BeNullOrEmpty
        }

        It "-Rollback refuses a previous version that no longer matches its manifest (exit 4)" {
            Invoke-TestInstall (New-TestZip "1.0.0")
            Invoke-TestInstall (New-TestZip "1.1.0")
            [IO.File]::WriteAllText((Join-Path $script:Root "versions\1.0.0\app\planted.pth"), "import os")
            { Invoke-TestInstall "" @{ Rollback = $true } } | Should -Throw "STOP 4"
            Get-Current | Should -BeExactly "1.1.0"
        }

        It "-RemoveSlot removes a kept version and refuses the current one" {
            Invoke-TestInstall (New-TestZip "1.0.0" -Schema 1)
            Invoke-TestInstall (New-TestZip "2.0.0" -Schema 2)
            Invoke-TestInstall (New-TestZip "2.1.0" -Schema 2)
            Get-Kept | Should -BeExactly "1.0.0,2.0.0,2.1.0"
            { Invoke-TestInstall "" @{ RemoveSlot = "2.1.0" } } | Should -Throw "STOP 2"
            { Invoke-TestInstall "" @{ RemoveSlot = "..\x" } } | Should -Throw "STOP 2"
            { Invoke-TestInstall "" @{ RemoveSlot = "9.9.9" } } | Should -Throw "STOP 1"
            Invoke-TestInstall "" @{ RemoveSlot = "1.0.0" }
            Get-Kept | Should -BeExactly "2.0.0,2.1.0"
            ((@((Get-Record).slots.PSObject.Properties.Name) | Sort-Object) -join ",") | Should -BeExactly "2.0.0,2.1.0"
            Invoke-TestInstall "" @{ RemoveSlot = "2.0.0" }
            (Get-Record).previous | Should -BeNullOrEmpty
            Get-Kept | Should -BeExactly "2.1.0"
        }
    }

    Context "a per-user install on a computer with a machine install" {
        It "finds the machine uninstall entry in the 64-bit view" {
            $key = Get-Item -LiteralPath "TestRegistry:\"
            $sub = $key.Name -replace '^HKEY_CURRENT_USER\\', ''
            Test-MachineInstalled -Hive CurrentUser -SubKey "$sub\Uninstall\Unminute" | Should -BeFalse
            New-Item -Path "TestRegistry:\Uninstall\Unminute" -Force | Out-Null
            Test-MachineInstalled -Hive CurrentUser -SubKey "$sub\Uninstall\Unminute" | Should -BeTrue
        }
    }
}

Describe "uninstall.ps1 -Machine" {
    BeforeAll {
        $script:Uninstall = Join-Path $script:Repo "packaging\uninstall.ps1"
        Assert-DotSourceGuard $script:Uninstall
        . $script:Uninstall
        Mock Stop-Uninstall { throw "STOP $Code" }
        Mock Test-Elevated { $true }
        Mock Test-64BitProcess { $true }
        Mock Get-ProgramFilesFolder { $script:ProgramFiles }
        Mock Get-CommonProgramsFolder { $script:CommonPrograms }
        Mock Get-MachineRegKey { $script:RegPath }
        Mock Get-EventSourceKey { $script:EventPath }
        Mock Get-FolderSecurity { Get-MachineRootSecurity }
        Mock Get-Process { }
        Mock Invoke-LidRestore { throw "a profile was read" }

        # A machine install as install.ps1 -Machine leaves it: versions (each with its
        # storage format), the current junction, current.json, the log, the
        # uninstaller, the Start menu entry, the uninstall entry and the Event Log source.
        function New-TestMachineInstall([hashtable]$Versions, [string]$Current) {
            $root = Join-Path $script:ProgramFiles "Unminute"
            foreach ($v in $Versions.Keys) {
                $slot = Join-Path $root "versions\$v"
                [void][IO.Directory]::CreateDirectory((Join-Path $slot "app"))
                [IO.File]::WriteAllText((Join-Path $slot "version.json"), (@{ version = $v; schema = $Versions[$v] } | ConvertTo-Json))
            }
            New-Item -ItemType Junction -Path (Join-Path $root "current") -Target (Join-Path $root "versions\$Current") | Out-Null
            $slots = [ordered]@{}
            $seq = 0
            foreach ($v in $Versions.Keys) { $seq++; $slots[$v] = [ordered]@{ schema = $Versions[$v]; seq = $seq } }
            [IO.File]::WriteAllText((Join-Path $root "current.json"),
                ([ordered]@{ format = 2; version = $Current; mode = "machine"; slots = $slots } | ConvertTo-Json -Depth 4))
            [void][IO.Directory]::CreateDirectory((Join-Path $root "logs"))
            [IO.File]::WriteAllText((Join-Path $root "logs\install.log"), "x")
            [IO.File]::WriteAllText((Join-Path $root "uninstall.ps1"), "# uninstaller")
            [IO.File]::WriteAllText((Join-Path $script:CommonPrograms "Unminute.lnk"), "shortcut")
            New-Item -Path $script:RegPath -Force | Out-Null
            New-Item -Path $script:EventPath -Force | Out-Null
            return $root
        }

        function Invoke-TestUninstall([hashtable]$With = @{}) {
            foreach ($k in $With.Keys) { $Options[$k] = $With[$k] }
            try { Invoke-MachineUninstall } finally { Exit-InstallLock }
        }
    }

    BeforeEach {
        $script:ProgramFiles = New-TestFolder "pf"
        $script:CommonPrograms = New-TestFolder "cp"
        $suffix = [guid]::NewGuid().ToString("N").Substring(0, 8)
        $script:RegPath = "TestRegistry:\u$suffix"
        $script:EventPath = "TestRegistry:\e$suffix"
        $Options.Machine = $true; $Options.Silent = $true; $Options.InstallDirGiven = $false
        $Options.IncludeKeptSlots = $false
    }

    AfterEach { Exit-InstallLock }

    It "removes the install, its Start menu entry, Event Log source and uninstall entry, never through a link" {
        $root = New-TestMachineInstall @{ "1.0.0" = 2 } "1.0.0"
        $outside = New-TestFolder "out"
        [IO.File]::WriteAllText((Join-Path $outside "sentinel.txt"), "keep")
        New-Item -ItemType Junction -Path (Join-Path $root "versions\1.0.0\app\linked") -Target $outside | Out-Null
        Invoke-TestUninstall
        [IO.Directory]::Exists($root) | Should -BeFalse
        [IO.File]::ReadAllText((Join-Path $outside "sentinel.txt")) | Should -BeExactly "keep"
        Test-Path -LiteralPath (Join-Path $script:CommonPrograms "Unminute.lnk") | Should -BeFalse
        Test-Path -LiteralPath $script:EventPath | Should -BeFalse
        Test-Path -LiteralPath $script:RegPath | Should -BeFalse
        Should -Invoke Invoke-LidRestore -Times 0 -Exactly
    }

    It "refuses <Label> with exit 2" -TestCases @(
        @{ Label = "a 32-bit Windows PowerShell"; Kind = "32bit" }
        @{ Label = "a run without administrator rights"; Kind = "standard" }
        @{ Label = "-InstallDir"; Kind = "installdir" }
    ) {
        param($Label, $Kind)
        $null = $Label
        $root = New-TestMachineInstall @{ "1.0.0" = 2 } "1.0.0"
        switch ($Kind) {
            "32bit" { Mock Test-64BitProcess { $false } }
            "standard" { Mock Test-Elevated { $false } }
            "installdir" { $Options.InstallDirGiven = $true }
        }
        $before = Get-Snapshot $root
        { Invoke-TestUninstall } | Should -Throw "STOP 2"
        Get-Snapshot $root | Should -BeExactly $before
    }

    It "keeps a version kept for an earlier storage format unless -IncludeKeptSlots (exit 5)" {
        $root = New-TestMachineInstall @{ "1.0.0" = 1; "2.0.0" = 2 } "2.0.0"
        $before = Get-Snapshot $root
        { Invoke-TestUninstall } | Should -Throw "STOP 5"
        Get-Snapshot $root | Should -BeExactly $before
        Test-Path -LiteralPath $script:RegPath | Should -BeTrue
        Exit-InstallLock
        Invoke-TestUninstall @{ IncludeKeptSlots = $true }
        [IO.Directory]::Exists($root) | Should -BeFalse
    }

    It "counts only the versions current.json keeps, not one an earlier install could not remove" {
        # Review of 4c0daf2: a version left on disk after its .retired rename failed is
        # not kept, so it does not stop the uninstall (exit 5).
        $root = New-TestMachineInstall @{ "1.0.0" = 1; "2.0.0" = 2 } "2.0.0"
        $record = Get-Content -LiteralPath (Join-Path $root "current.json") -Raw | ConvertFrom-Json
        $record.slots.PSObject.Properties.Remove("1.0.0")
        [IO.File]::WriteAllText((Join-Path $root "current.json"), ($record | ConvertTo-Json -Depth 4))
        Invoke-TestUninstall
        [IO.Directory]::Exists($root) | Should -BeFalse
    }

    It "removes a previous version of the same storage format without -IncludeKeptSlots" {
        $root = New-TestMachineInstall @{ "2.0.0" = 2; "2.1.0" = 2 } "2.1.0"
        Invoke-TestUninstall
        [IO.Directory]::Exists($root) | Should -BeFalse
    }

    It "removes nothing when <Label> (exit <Code>)" -TestCases @(
        @{ Label = "the folder holds something else"; Kind = "stray"; Code = 1 }
        @{ Label = "a junction points outside the versions"; Kind = "link"; Code = 1 }
        @{ Label = "the folder's permissions have changed"; Kind = "acl"; Code = 4 }
        @{ Label = "a file in it has permissions of its own"; Kind = "fileacl"; Code = 4 }
        @{ Label = "the app runs for anyone"; Kind = "running"; Code = 3 }
    ) {
        param($Label, $Kind, $Code)
        $null = $Label
        $root = New-TestMachineInstall @{ "1.0.0" = 2 } "1.0.0"
        switch ($Kind) {
            "stray" { [IO.File]::WriteAllText((Join-Path $root "notes.txt"), "someone's") }
            "link" {
                [IO.Directory]::Delete((Join-Path $root "current"), $false)
                New-Item -ItemType Junction -Path (Join-Path $root "current") -Target (New-TestFolder "out") | Out-Null
            }
            "acl" { Mock Get-FolderSecurity { Get-MachineRootSecurity "S-1-5-32-545" } }
            "fileacl" {
                $file = [IO.FileInfo]::new((Join-Path $root "uninstall.ps1"))
                $acl = $file.GetAccessControl()
                $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                    [Security.Principal.WindowsIdentity]::GetCurrent().User, "Modify", "Allow"))
                $file.SetAccessControl($acl)
            }
            "running" { Mock Get-Process { [pscustomobject]@{ Path = "$root\current\runtime\pythonw.exe" } } }
        }
        $before = Get-Snapshot $root
        { Invoke-TestUninstall } | Should -Throw "STOP $Code"
        Get-Snapshot $root | Should -BeExactly $before
        Test-Path -LiteralPath $script:RegPath | Should -BeTrue
        Test-Path -LiteralPath $script:EventPath | Should -BeTrue
    }

    It "removes what is left when the folder has gone, and says there is nothing to remove otherwise" {
        New-Item -Path $script:RegPath -Force | Out-Null
        New-Item -Path $script:EventPath -Force | Out-Null
        Invoke-TestUninstall
        Test-Path -LiteralPath $script:RegPath | Should -BeFalse
        Test-Path -LiteralPath $script:EventPath | Should -BeFalse
        { Invoke-TestUninstall } | Should -Throw "STOP 1"
    }
}
