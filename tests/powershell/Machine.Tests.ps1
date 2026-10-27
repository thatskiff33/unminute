# Pester 5 tests of machine mode (#111 B6b): install.ps1 -Machine and uninstall.ps1
# -Machine. Run them with tools/run_pester.ps1 under Windows PowerShell 5.1.
#
# Safety (tests/powershell/Isolation.ps1, as in Installer.Tests.ps1): nothing here
# needs administrator rights or can reach this computer's install, per user or
# machine. LOCALAPPDATA and APPDATA point into TestDrive, and a script is loaded only
# as ". (Confirm-Loadable $path); Set-ScratchTarget": refused without its dot-source
# guard, or when it reaches the Start menu, Program Files, the uninstall keys or the
# Event Log source other than through the variables at its top. Each test then gets
# its own scratch places from Set-ScratchMachineTarget: a Program Files and an
# all-users Start menu folder in TestDrive, and throwaway HKCU keys for the machine
# uninstall entry and the Event Log source; "machine places" below proves it. What
# remains is stood in by Pester mocks: elevation and bitness (Test-Elevated,
# Test-64BitProcess), the folder's owner and permissions as read (Get-FolderSecurity,
# a list built in memory with Get-MachineRootSecurity), the folder's creation with
# them (New-MachineRoot, a plain folder), prerequisites and running processes.
# Stop-Install and Stop-Uninstall throw "STOP <code>". Packages are synthetic. ASCII
# only (Windows PowerShell 5.1 reads a script without a byte-order mark in the system
# code page).

BeforeAll {
    . (Join-Path $PSScriptRoot "Isolation.ps1")
    Enter-ScratchProfile $TestDrive
    $script:Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

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
    Exit-ScratchProfile
}

Describe "install.ps1 -Machine" {
    BeforeAll {
        $script:Install = Join-Path $script:Repo "packaging\install.ps1"
        . (Confirm-Loadable $script:Install); Set-ScratchTarget
        Mock Stop-Install { throw "STOP $Code" }
        Mock Test-Elevated { $true }
        Mock Test-64BitProcess { $true }
        Mock Test-Prerequisite { }
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
        Set-ScratchMachineTarget
        $script:ProgramFiles = $ProgramFilesFolder
        $script:CommonPrograms = $CommonProgramsFolder
        $script:RegPath = $MachineRegKey
        $script:EventPath = $EventSourceKey
        $script:Root = Join-Path $script:ProgramFiles "Unminute"
        $Options.Machine = $true; $Options.Silent = $true; $Options.SkipShortcut = $false
        $Options.InstallDirGiven = $false; $Options.SelfService = $false; $Options.Managed = $false
        $Options.Rollback = $false; $Options.RemoveSlot = ""; $Options.Package = ""
        $Options.BlockRuntimeNetwork = $false; $script:RuntimeBlock = $false
        $script:LogFile = $null; $script:LogVersion = "-"; $script:LogError = $null
    }

    AfterEach {
        Clear-InstallRun
        $Options.Machine = $false
    }

    Context "machine places (the tests' own safety check)" {
        It "gives every test its own scratch places, and refuses the real ones" {
            Test-ScratchPath $ProgramFilesFolder | Should -BeTrue
            Test-ScratchPath (Get-MachineRoot) | Should -BeTrue
            Get-MachineRoot | Should -BeExactly $script:Root
            Get-CommonProgramsFolder | Should -BeExactly $script:CommonPrograms
            Test-ScratchKey (Get-MachineRegKey) | Should -BeTrue
            Test-ScratchKey (Get-EventSourceKey) | Should -BeTrue
            # Not the ones Set-ScratchTarget gave the loaded script: this test's own.
            $ProgramFilesFolder | Should -Not -BeExactly $script:ScratchProgramFiles
            $MachineRegKey | Should -Not -BeExactly $script:ScratchMachineKey
            foreach ($case in @(
                    @{ Name = "ProgramFilesFolder"; Value = [Environment]::GetFolderPath("ProgramFiles") }
                    @{ Name = "CommonProgramsFolder"; Value = [Environment]::GetFolderPath("CommonPrograms") }
                    @{ Name = "MachineRegKey"; Value = $script:RealMachineKey }
                    @{ Name = "EventSourceKey"; Value = $script:RealEventSourceKey })) {
                $saved = Get-Variable -Name $case.Name -ValueOnly
                Set-Variable -Name $case.Name -Value $case.Value
                { Assert-ScratchTarget } | Should -Throw "Not scratch*"
                Set-Variable -Name $case.Name -Value $saved
            }
            { Assert-ScratchTarget } | Should -Not -Throw
        }

        It "installs, rolls back and removes a version writing only this test's scratch places" {
            $before = Get-RealMachineState
            Invoke-TestInstall (New-TestZip "1.0.0")
            Invoke-TestInstall (New-TestZip "1.1.0")
            Invoke-TestInstall "" @{ Rollback = $true }
            Invoke-TestInstall "" @{ Rollback = $false; RemoveSlot = "1.1.0" }
            $Options.RemoveSlot = ""
            Test-Path -LiteralPath (Join-Path $script:CommonPrograms "Unminute.lnk") -PathType Leaf | Should -BeTrue
            (Get-ItemProperty -LiteralPath $script:RegPath).DisplayVersion | Should -BeExactly "1.0.0"
            Test-Path -LiteralPath $script:EventPath | Should -BeTrue
            Get-RealMachineState | Should -BeExactly $before
        }
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
            # The first name is the run's temporary folder (Enter-MachineTemp), the next
            # the package's.
            $script:Named = 0
            Mock New-RandomName { $script:Named++; if ($script:Named -eq 1) { "1" * 32 } else { "0" * 32 } }
            Mock Remove-StagingLeftover { }
            # The message, so the test tells this refusal from the run's temporary
            # folder's, which also stops with exit 1 (#406).
            Mock Stop-Install { throw "STOP $Code $Message" }
            [void][IO.Directory]::CreateDirectory($script:Root)
            $taken = [IO.Directory]::CreateDirectory((Join-Path $script:Root ("staging-" + ("0" * 32)))).FullName
            [IO.File]::WriteAllText((Join-Path $taken "sentinel.txt"), "someone's")
            { Invoke-TestInstall (New-TestZip "1.0.0") } |
                Should -Throw -ExpectedMessage "STOP 1 The temporary folder for extracting the package already exists, so nothing was installed. Run this again."
            $script:Named | Should -Be 2
            [IO.File]::ReadAllText((Join-Path $taken "sentinel.txt")) | Should -BeExactly "someone's"
            [IO.Directory]::Exists((Join-Path $script:Root "versions\1.0.0")) | Should -BeFalse
        }

        It "does not use a temporary folder for the run that already exists (exit 1), and leaves it as it is (#406)" {
            # The first name is the run's temporary folder (Enter-MachineTemp): taken, so
            # the run stops there, before the package is opened.
            $script:Named = 0
            Mock New-RandomName { $script:Named++; "1" * 32 }
            Mock Remove-StagingLeftover { }
            Mock Stop-Install { throw "STOP $Code $Message" }
            [void][IO.Directory]::CreateDirectory($script:Root)
            $taken = [IO.Directory]::CreateDirectory((Join-Path $script:Root ("staging-" + ("1" * 32)))).FullName
            [IO.File]::WriteAllText((Join-Path $taken "sentinel.txt"), "someone's")
            $saved = @($env:TMP, $env:TEMP)
            { Invoke-TestInstall (New-TestZip "1.0.0") } |
                Should -Throw -ExpectedMessage "STOP 1 The temporary folder for this run already exists, so nothing was installed. Run this again."
            $script:Named | Should -Be 1
            # TMP and TEMP never pointed at it, and it is left as it was.
            @($env:TMP, $env:TEMP) -join "|" | Should -BeExactly ($saved -join "|")
            [IO.File]::ReadAllText((Join-Path $taken "sentinel.txt")) | Should -BeExactly "someone's"
            @([IO.DirectoryInfo]::new($script:Root).GetDirectories("staging-*") | ForEach-Object Name) -join "," |
                Should -BeExactly ("staging-" + ("1" * 32))
            [IO.Directory]::Exists((Join-Path $script:Root "versions")) | Should -BeFalse
        }

        It "checks and unpacks the zip through one handle, so nobody can change it in between (review of a3ab563, L3)" {
            $script:Zip = New-TestZip "1.0.0"
            $script:Other = New-TestZip "6.6.6"
            $script:Tries = @()
            # Between the checksum and the unpacking: overwrite the zip with another
            # package, rename it away, delete it.
            Mock Write-Step {
                if ($Message -ne "Extracting...") { return }
                try { [IO.File]::Copy($script:Other, $script:Zip, $true); $script:Tries += "overwritten" } catch { $script:Tries += "refused" }
                try { [IO.File]::Move($script:Zip, "$($script:Zip).moved"); $script:Tries += "renamed" } catch { $script:Tries += "refused" }
                try { [IO.File]::Delete($script:Zip); $script:Tries += "deleted" } catch { $script:Tries += "refused" }
            }
            Invoke-TestInstall $script:Zip
            $script:Tries -join "," | Should -BeExactly "refused,refused,refused"
            Get-Current | Should -BeExactly "1.0.0"
            Get-Kept | Should -BeExactly "1.0.0"
        }

        It "puts the run's temporary files in the install folder, never in TEMP (review of a3ab563, L1)" {
            # SYSTEM's temporary folder (C:\Windows\Temp) stood in by a folder that does
            # not exist: anything written there fails, and the test sees it. The
            # registration step's Set-ShortcutAppId compiles C# with Add-Type, once a
            # process; here each run compiles a new type, as that first run does.
            $zips = @((New-TestZip "1.0.0"), (New-TestZip "1.1.0"))
            $missing = Join-Path $TestDrive ("systemp" + [guid]::NewGuid().ToString("N").Substring(0, 8))
            $script:Seen = @()
            Mock Set-ShortcutAppId {
                $where = [IO.Path]::GetTempPath()
                try {
                    Add-Type -TypeDefinition ("public static class UmProbe" + [guid]::NewGuid().ToString("N") + " { }")
                    $script:Seen += "compiled in $where"
                } catch { $script:Seen += "failed in $where" }
            }
            $saved = @($env:TMP, $env:TEMP)
            $env:TMP = $missing; $env:TEMP = $missing
            try {
                Invoke-TestInstall $zips[0]
                Invoke-TestInstall $zips[1]
                Invoke-TestInstall "" @{ Rollback = $true }
                $Options.Rollback = $false
                # Each run puts TMP and TEMP back as they were.
                @($env:TMP, $env:TEMP) -join "|" | Should -BeExactly "$missing|$missing"
            } finally { $env:TMP = $saved[0]; $env:TEMP = $saved[1] }
            $script:Seen.Count | Should -Be 3
            foreach ($line in $script:Seen) {
                $line | Should -Match ("^compiled in " + [regex]::Escape("$script:Root\staging-") + "[0-9a-f]{32}\\$")
            }
            [IO.Directory]::Exists($missing) | Should -BeFalse
            @([IO.DirectoryInfo]::new($script:Root).GetDirectories("staging-*")).Count | Should -Be 0
            Get-Current | Should -BeExactly "1.0.0"
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

        It "checks what is beneath the folder only once it holds install.lock, so another run's write does not stop it (review of a3ab563, N3)" {
            Invoke-TestInstall (New-TestZip "1.0.0")
            $zip = New-TestZip "1.1.0"
            # Another run, part way through writing current.json over in place: it holds
            # the lock, and the file has an entry of its own until that run resets it
            # (Set-InheritedOnly) and lets the lock go, which happens while this run waits.
            $file = [IO.FileInfo]::new((Join-Path $script:Root "current.json"))
            $acl = $file.GetAccessControl()
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                [Security.Principal.WindowsIdentity]::GetCurrent().User, "Modify", "Allow"))
            $file.SetAccessControl($acl)
            $script:OtherRun = [IO.FileStream]::new((Join-Path $script:Root "install.lock"), [IO.FileMode]::OpenOrCreate,
                [IO.FileAccess]::ReadWrite, [IO.FileShare]::None, 1, [IO.FileOptions]::DeleteOnClose)
            Mock Start-Sleep {
                if ($script:OtherRun) {
                    Set-InheritedOnly (Join-Path $script:Root "current.json")
                    $script:OtherRun.Dispose(); $script:OtherRun = $null
                }
            }
            try { Invoke-TestInstall $zip }
            finally { if ($script:OtherRun) { $script:OtherRun.Dispose(); $script:OtherRun = $null } }
            Should -Invoke Start-Sleep -Times 1
            Get-Current | Should -BeExactly "1.1.0"
            Get-AclChangeCount $script:Root | Should -Be 0
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

        It "leaves nothing with permissions of its own after installs and a rollback" {
            # CI's elevated runner (23 October 2026): a file written over in place
            # (current.json, uninstall.ps1) came out with permissions of its own, so
            # the third run stopped with exit 4. Set-InheritedOnly resets the two.
            $me = [Security.Principal.WindowsIdentity]::GetCurrent().User
            Invoke-TestInstall (New-TestZip "1.0.0")
            foreach ($name in "current.json", "uninstall.ps1") {
                $file = [IO.FileInfo]::new((Join-Path $script:Root $name))
                $acl = $file.GetAccessControl()
                $acl.SetAccessRuleProtection($true, $true)
                $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($me, "Modify", "Allow"))
                $file.SetAccessControl($acl)
                Set-InheritedOnly $file.FullName
                Test-AclChanged ([IO.FileInfo]::new($file.FullName)) | Should -BeFalse -Because $name
            }
            Invoke-TestInstall (New-TestZip "1.1.0")
            Invoke-TestInstall (New-TestZip "1.2.0")
            Invoke-TestInstall "" @{ Rollback = $true }
            $Options.Rollback = $false
            Get-Current | Should -BeExactly "1.1.0"
            Get-AclChangeCount $script:Root | Should -Be 0
            Invoke-TestInstall (New-TestZip "1.3.0")
            Get-AclChangeCount $script:Root | Should -Be 0
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

    Context "the runtime firewall rule (#111 B7b)" {
        # Windows Firewall is stood in: the group's rules live in $script:Rules (name to
        # program), the network profiles in $script:Profiles and the policy values in
        # $script:Policy. No rule is ever added to this computer's firewall (and the
        # real commands throw in a test, Isolation.ps1).
        BeforeEach {
            $script:Rules = @{}
            $script:AddedAt = @()
            $script:Warnings = @()
            $script:Policy = @{ AllowModelDownloads = 0; AllowGpuComponent = 0 }
            $script:Profiles = @(foreach ($name in "Domain", "Private", "Public") {
                [pscustomobject]@{ Name = $name; Enabled = "True"; AllowLocalFirewallRules = "NotConfigured" } })
            Mock Get-RuntimeRule { $copy = @{}; foreach ($k in $script:Rules.Keys) { $copy[$k] = $script:Rules[$k] }; $copy }
            Mock Add-RuntimeRule {
                $script:Rules[$Name] = $Program
                $at = Get-LinkTarget (Join-Path $script:Root "current")
                $where = if ($at) { Split-Path -Leaf $at } else { "none" }
                $script:AddedAt += "$Name@$where"
            }
            Mock Remove-RuntimeRule { $script:Rules.Remove($Name) }
            Mock Get-FirewallProfileState { $script:Profiles }
            Mock Get-PolicyValue { $script:Policy[$Name] }
            Mock Write-Warn { $script:Warnings += $Message }

            # The group's rule names, sorted, without the throwaway group's name.
            function Get-RuleList {
                return ((@($script:Rules.Keys | ForEach-Object { $_.Substring($FirewallGroup.Length + 1) }) | Sort-Object) -join ",")
            }
        }

        It "adds an outbound block rule for each runtime program of the new version, by its real path, before it becomes current" {
            Invoke-TestInstall (New-TestZip "1.0.0") @{ BlockRuntimeNetwork = $true }
            Get-RuleList | Should -BeExactly "1.0.0 python.exe,1.0.0 pythonw.exe"
            $runtime = Join-Path $script:Root "versions\1.0.0\runtime"
            $script:Rules["$FirewallGroup 1.0.0 python.exe"] | Should -BeExactly (Join-Path $runtime "python.exe")
            $script:Rules["$FirewallGroup 1.0.0 pythonw.exe"] | Should -BeExactly (Join-Path $runtime "pythonw.exe")
            # Added before the junction pointed anywhere, so the version never ran unblocked.
            $script:AddedAt | Should -Be @("$FirewallGroup 1.0.0 python.exe@none", "$FirewallGroup 1.0.0 pythonw.exe@none")
            (Get-Record).block_runtime_network | Should -BeTrue
            Get-Current | Should -BeExactly "1.0.0"
            $script:Warnings | Should -BeNullOrEmpty
            $log = [IO.File]::ReadAllText((Join-Path $script:Root "logs\install.log"))
            $log | Should -Match "mode=machine version=1\.0\.0 step=firewall result=ok"
            $log | Should -Match "step=done result=ok code=0"
        }

        It "keeps the choice without the switch, and the rules in step with the versions kept" {
            Invoke-TestInstall (New-TestZip "1.0.0" -Schema 1) @{ BlockRuntimeNetwork = $true }
            Invoke-TestInstall (New-TestZip "2.0.0" -Schema 2) @{ BlockRuntimeNetwork = $false }
            Get-Kept | Should -BeExactly "1.0.0,2.0.0"
            Get-RuleList | Should -BeExactly "1.0.0 python.exe,1.0.0 pythonw.exe,2.0.0 python.exe,2.0.0 pythonw.exe"
            Invoke-TestInstall (New-TestZip "3.0.0" -Schema 3) @{ BlockRuntimeNetwork = $false }
            Get-Kept | Should -BeExactly "2.0.0,3.0.0"
            Get-RuleList | Should -BeExactly "2.0.0 python.exe,2.0.0 pythonw.exe,3.0.0 python.exe,3.0.0 pythonw.exe"
            # Each new version's rules came while the one before was still current.
            @($script:AddedAt | Where-Object { $_ -like "*3.0.0 *" }) | Should -Be @(
                "$FirewallGroup 3.0.0 python.exe@2.0.0", "$FirewallGroup 3.0.0 pythonw.exe@2.0.0")
            (Get-Record).block_runtime_network | Should -BeTrue
        }

        It "reaches no firewall rule, and records no choice, when it was never made" {
            Invoke-TestInstall (New-TestZip "1.0.0")
            Invoke-TestInstall (New-TestZip "1.1.0")
            Should -Invoke Get-RuntimeRule -Times 0 -Exactly
            Should -Invoke Add-RuntimeRule -Times 0 -Exactly
            Should -Invoke Get-FirewallProfileState -Times 0 -Exactly
            @((Get-Record).PSObject.Properties.Name) | Should -Not -Contain "block_runtime_network"
        }

        It "puts back a rule that names another program, and removes the group's other rules" {
            $script:Rules["$FirewallGroup 1.0.0 python.exe"] = "C:\elsewhere\python.exe"
            $script:Rules["$FirewallGroup 0.1.0 pythonw.exe"] = "C:\gone\pythonw.exe"
            Invoke-TestInstall (New-TestZip "1.0.0") @{ BlockRuntimeNetwork = $true }
            Get-RuleList | Should -BeExactly "1.0.0 python.exe,1.0.0 pythonw.exe"
            $script:Rules["$FirewallGroup 1.0.0 python.exe"] | Should -BeExactly (Join-Path $script:Root "versions\1.0.0\runtime\python.exe")
        }

        It "stops before the switch (exit 1) when a rule cannot be added" {
            Invoke-TestInstall (New-TestZip "1.0.0") @{ BlockRuntimeNetwork = $true }
            Mock Add-RuntimeRule { throw "denied" }
            { Invoke-TestInstall (New-TestZip "1.1.0") @{ BlockRuntimeNetwork = $false } } | Should -Throw "STOP 1"
            Get-Current | Should -BeExactly "1.0.0"
            (Get-Record).version | Should -BeExactly "1.0.0"
            (Get-ItemProperty -LiteralPath $script:RegPath).DisplayVersion | Should -BeExactly "1.0.0"
        }

        It "warns while managed policy still allows downloads: <Label>" -TestCases @(
            @{ Label = "neither value set"; Policy = @{}; Says = "AllowModelDownloads, AllowGpuComponent not set to 0" }
            @{ Label = "GPU components allowed"; Policy = @{ AllowModelDownloads = 0; AllowGpuComponent = 1 }; Says = "AllowGpuComponent not set to 0" }
            @{ Label = "a value that is text, not a number"; Policy = @{ AllowModelDownloads = "0"; AllowGpuComponent = 0 }; Says = "AllowModelDownloads not set to 0" }
            @{ Label = "user models off but GPU components allowed (#111 B7a)"; Policy = @{ AllowUserModels = 0 }; Says = "AllowGpuComponent not set to 0" }
        ) {
            param($Label, $Policy, $Says)
            $null = $Label
            $script:Policy = $Policy
            Invoke-TestInstall (New-TestZip "1.0.0") @{ BlockRuntimeNetwork = $true }
            @($script:Warnings).Count | Should -Be 1
            $script:Warnings[0] | Should -BeLike "*($Says)*"
            [IO.File]::ReadAllText((Join-Path $script:Root "logs\install.log")) | Should -Match "step=firewall result=downloads_allowed"
            Get-Current | Should -BeExactly "1.0.0"
        }

        It "does not warn about downloads when user models are off and GPU components are too" {
            $script:Policy = @{ AllowUserModels = 0; AllowGpuComponent = 0 }
            Invoke-TestInstall (New-TestZip "1.0.0") @{ BlockRuntimeNetwork = $true }
            $script:Warnings | Should -BeNullOrEmpty
        }

        It "warns, and logs, that the rules are not in effect when <Label>" -TestCases @(
            @{ Label = "a profile ignores this computer's own rules"; Kind = "merge"; Says = "Firewall rules not in effect*: Public. *" }
            @{ Label = "the firewall is off for a profile"; Kind = "off"; Says = "Firewall rules not in effect*: Domain. *" }
            @{ Label = "the profiles cannot be read"; Kind = "unreadable"; Says = "*could not be checked*" }
        ) {
            param($Label, $Kind, $Says)
            $null = $Label
            switch ($Kind) {
                "merge" { $script:Profiles[2].AllowLocalFirewallRules = "False" }
                "off" { $script:Profiles[0].Enabled = "False" }
                "unreadable" { Mock Get-FirewallProfileState { throw "unavailable" } }
            }
            Invoke-TestInstall (New-TestZip "1.0.0") @{ BlockRuntimeNetwork = $true }
            @($script:Warnings).Count | Should -Be 1
            $script:Warnings[0] | Should -BeLike $Says
            $result = if ($Kind -eq "unreadable") { "unchecked" } else { "not_in_effect" }
            [IO.File]::ReadAllText((Join-Path $script:Root "logs\install.log")) | Should -Match "step=firewall result=$result"
            # The rules are still added, and the install completes.
            Get-RuleList | Should -BeExactly "1.0.0 python.exe,1.0.0 pythonw.exe"
            Get-Current | Should -BeExactly "1.0.0"
        }

        It "-Rollback keeps the rules of the versions still installed, and removes a newer format's" {
            Invoke-TestInstall (New-TestZip "1.0.0") @{ BlockRuntimeNetwork = $true }
            Invoke-TestInstall (New-TestZip "1.1.0") @{ BlockRuntimeNetwork = $false }
            Invoke-TestInstall "" @{ Rollback = $true }
            Get-Current | Should -BeExactly "1.0.0"
            Get-RuleList | Should -BeExactly "1.0.0 python.exe,1.0.0 pythonw.exe,1.1.0 python.exe,1.1.0 pythonw.exe"
            (Get-Record).block_runtime_network | Should -BeTrue
            Invoke-TestInstall (New-TestZip "2.0.0" -Schema 3) @{ Rollback = $false }
            Invoke-TestInstall "" @{ Rollback = $true }
            Get-Current | Should -BeExactly "1.0.0"
            Get-Kept | Should -BeExactly "1.0.0"
            Get-RuleList | Should -BeExactly "1.0.0 python.exe,1.0.0 pythonw.exe"
        }

        It "-RemoveSlot removes that version's rules and keeps the choice" {
            Invoke-TestInstall (New-TestZip "1.0.0" -Schema 1) @{ BlockRuntimeNetwork = $true }
            Invoke-TestInstall (New-TestZip "2.0.0" -Schema 2) @{ BlockRuntimeNetwork = $false }
            Invoke-TestInstall (New-TestZip "2.1.0" -Schema 2)
            Get-RuleList | Should -BeExactly ("1.0.0 python.exe,1.0.0 pythonw.exe,2.0.0 python.exe,2.0.0 pythonw.exe," +
                "2.1.0 python.exe,2.1.0 pythonw.exe")
            Invoke-TestInstall "" @{ RemoveSlot = "1.0.0" }
            Get-RuleList | Should -BeExactly "2.0.0 python.exe,2.0.0 pythonw.exe,2.1.0 python.exe,2.1.0 pythonw.exe"
            (Get-Record).block_runtime_network | Should -BeTrue
        }

        It "refuses -BlockRuntimeNetwork with <Label> (exit 2), changing nothing" -TestCases @(
            @{ Label = "-Rollback"; With = @{ Rollback = $true; BlockRuntimeNetwork = $true } }
            @{ Label = "-RemoveSlot"; With = @{ RemoveSlot = "1.0.0"; BlockRuntimeNetwork = $true } }
        ) {
            param($Label, $With)
            $null = $Label
            Invoke-TestInstall (New-TestZip "1.0.0")
            Invoke-TestInstall (New-TestZip "1.1.0")
            $before = Get-Snapshot $script:Root
            $stopped = $null
            try { Invoke-TestInstall "" $With } catch { $stopped = $_.Exception.Message }
            $stopped | Should -BeExactly "STOP 2"
            Get-Snapshot $script:Root | Should -BeExactly $before
            Should -Invoke Add-RuntimeRule -Times 0 -Exactly
        }
    }

    Context "the firewall commands the rule uses (#111 B7b)" {
        It "asks Windows Firewall for an outbound block rule for that one program, in the group, in this computer's own rules" {
            Mock New-NetFirewallRule { $script:Asked = @{} + $PesterBoundParameters }
            $program = Join-Path $script:Root "versions\1.0.0\runtime\pythonw.exe"
            Add-RuntimeRule "$FirewallGroup 1.0.0 pythonw.exe" $program
            Should -Invoke New-NetFirewallRule -Times 1 -Exactly
            $script:Asked.Name | Should -BeExactly "$FirewallGroup 1.0.0 pythonw.exe"
            $script:Asked.DisplayName | Should -BeExactly "$FirewallGroup 1.0.0 pythonw.exe"
            $script:Asked.Group | Should -BeExactly $FirewallGroup
            $script:Asked.Group | Should -BeLike "Unminute-PesterTest-*"
            $script:Asked.Program | Should -BeExactly $program
            "$($script:Asked.Direction)" | Should -BeExactly "Outbound"
            "$($script:Asked.Action)" | Should -BeExactly "Block"
            "$($script:Asked.Profile)" | Should -BeExactly "Any"
            "$($script:Asked.Enabled)" | Should -BeExactly "True"
            $script:Asked.PolicyStore | Should -BeExactly "PersistentStore"
        }

        It "reads the group's rules and their programs, and an empty group as none" {
            Mock Get-NetFirewallRule {
                $script:AskedGroup = $Group; $script:AskedStore = $PolicyStore
                @([pscustomobject]@{ Name = "$FirewallGroup 1.0.0 python.exe" })
            }
            # A stand-in rule is not the CIM object the real command takes.
            Mock Get-NetFirewallApplicationFilter -RemoveParameterType AssociatedNetFirewallRule {
                [pscustomobject]@{ Program = "C:\x\python.exe" }
            }
            $found = Get-RuntimeRule
            $found.Count | Should -Be 1
            $found["$FirewallGroup 1.0.0 python.exe"] | Should -BeExactly "C:\x\python.exe"
            "$($script:AskedGroup)" | Should -BeExactly $FirewallGroup
            $script:AskedStore | Should -BeExactly "PersistentStore"
            Mock Get-NetFirewallRule {
                throw [Management.Automation.ErrorRecord]::new([Exception]::new("none"), "NotFound",
                    [Management.Automation.ErrorCategory]::ObjectNotFound, $null)
            }
            (Get-RuntimeRule).Count | Should -Be 0
            Mock Get-NetFirewallRule { throw "the service is stopped" }
            { Get-RuntimeRule } | Should -Throw "*service is stopped*"
        }

        It "removes a rule by its name from this computer's own rules" {
            Mock Remove-NetFirewallRule { $script:Asked = @{} + $PesterBoundParameters }
            Remove-RuntimeRule "$FirewallGroup 1.0.0 python.exe"
            "$($script:Asked.Name)" | Should -BeExactly "$FirewallGroup 1.0.0 python.exe"
            $script:Asked.PolicyStore | Should -BeExactly "PersistentStore"
        }
    }

    Context "a per-user install on a computer with a machine install" {
        It "finds the machine uninstall entry in the 64-bit view" {
            # $MachineRegKey is this test's throwaway HKCU key (Set-ScratchMachineTarget).
            Test-MachineInstalled | Should -BeFalse
            New-Item -Path $MachineRegKey -Force | Out-Null
            Test-MachineInstalled | Should -BeTrue
        }
    }
}

Describe "uninstall.ps1 -Machine" {
    BeforeAll {
        $script:Uninstall = Join-Path $script:Repo "packaging\uninstall.ps1"
        . (Confirm-Loadable $script:Uninstall); Set-ScratchTarget
        Mock Stop-Uninstall { throw "STOP $Code" }
        Mock Test-Elevated { $true }
        Mock Test-64BitProcess { $true }
        Mock Get-FolderSecurity { Get-MachineRootSecurity }
        Mock Get-Process { }
        Mock Invoke-LidRestore { throw "a profile was read" }
        # Windows Firewall stood in (#111 B7b): no runtime rule unless a test adds one.
        Mock Get-RuntimeRule { @{} }
        Mock Remove-RuntimeRule { throw "no rule to remove" }

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
        Set-ScratchMachineTarget
        $script:ProgramFiles = $ProgramFilesFolder
        $script:CommonPrograms = $CommonProgramsFolder
        $script:RegPath = $MachineRegKey
        $script:EventPath = $EventSourceKey
        $Options.Machine = $true; $Options.Silent = $true; $Options.InstallDirGiven = $false
        $Options.IncludeKeptSlots = $false; $Options.Offboard = $false
    }

    AfterEach { Exit-InstallLock }

    It "removes only this test's scratch places, and nothing of the real machine install" {
        Test-ScratchPath (Get-MachineRoot) | Should -BeTrue
        Test-ScratchPath (Get-CommonProgramsFolder) | Should -BeTrue
        Test-ScratchKey (Get-MachineRegKey) | Should -BeTrue
        Test-ScratchKey (Get-EventSourceKey) | Should -BeTrue
        $before = Get-RealMachineState
        $root = New-TestMachineInstall @{ "1.0.0" = 2 } "1.0.0"
        Invoke-TestUninstall
        [IO.Directory]::Exists($root) | Should -BeFalse
        Test-Path -LiteralPath $script:RegPath | Should -BeFalse
        Get-RealMachineState | Should -BeExactly $before
    }

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
        @{ Label = "-Offboard, which is each person's own (#111 B8b)"; Kind = "offboard" }
    ) {
        param($Label, $Kind)
        $null = $Label
        $root = New-TestMachineInstall @{ "1.0.0" = 2 } "1.0.0"
        switch ($Kind) {
            "32bit" { Mock Test-64BitProcess { $false } }
            "standard" { Mock Test-Elevated { $false } }
            "installdir" { $Options.InstallDirGiven = $true }
            "offboard" { $Options.Offboard = $true }
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

    It "says an install is running (exit 3), not that the permissions changed, while an install holds the lock part way through a write (review of a3ab563, N3)" {
        $root = New-TestMachineInstall @{ "1.0.0" = 2 } "1.0.0"
        $file = [IO.FileInfo]::new((Join-Path $root "current.json"))
        $acl = $file.GetAccessControl()
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.WindowsIdentity]::GetCurrent().User, "Modify", "Allow"))
        $file.SetAccessControl($acl)
        $before = Get-Snapshot $root
        # Typed: a string in the last place would pick the useAsync overload, and the
        # lock would outlive this test.
        $held = [IO.FileStream]::new((Join-Path $root "install.lock"), [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite, [IO.FileShare]::None, 1, [IO.FileOptions]::DeleteOnClose)
        try { { Invoke-TestUninstall } | Should -Throw "STOP 3" } finally { $held.Dispose() }
        Get-Snapshot $root | Should -BeExactly $before
        Test-Path -LiteralPath $script:RegPath | Should -BeTrue
    }

    It "removes every rule in the runtime firewall group, whether or not current.json records the choice (#111 B7b)" {
        $script:Removed = @()
        Mock Get-RuntimeRule { @{ "$FirewallGroup 1.0.0 python.exe" = "x"; "$FirewallGroup 0.9.0 pythonw.exe" = $null } }
        Mock Remove-RuntimeRule { $script:Removed += $Name }
        $root = New-TestMachineInstall @{ "1.0.0" = 2 } "1.0.0"
        Invoke-TestUninstall
        [IO.Directory]::Exists($root) | Should -BeFalse
        ((@($script:Removed) | Sort-Object) -join ",") | Should -BeExactly "$FirewallGroup 0.9.0 pythonw.exe,$FirewallGroup 1.0.0 python.exe"
        $FirewallGroup | Should -BeLike "Unminute-PesterTest-*"
        # And when the folder has already gone.
        $script:Removed = @()
        New-Item -Path $script:RegPath -Force | Out-Null
        Invoke-TestUninstall
        @($script:Removed).Count | Should -Be 2
    }

    It "still uninstalls, and warns, when the runtime firewall rules cannot be removed (#111 B7b)" {
        $script:Warnings = @()
        Mock Get-RuntimeRule { @{ "$FirewallGroup 1.0.0 python.exe" = "x" } }
        Mock Remove-RuntimeRule { throw "denied" }
        Mock Write-Warn { $script:Warnings += $Message }
        $root = New-TestMachineInstall @{ "1.0.0" = 2 } "1.0.0"
        Invoke-TestUninstall
        [IO.Directory]::Exists($root) | Should -BeFalse
        Test-Path -LiteralPath $script:RegPath | Should -BeFalse
        @($script:Warnings) | Should -Be @("The runtime firewall rules could not be removed; remove the rules in their group by hand.")
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
