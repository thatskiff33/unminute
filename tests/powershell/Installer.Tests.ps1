# Pester 5 tests of packaging/install.ps1 and packaging/uninstall.ps1 (#111 B6a).
# Run them with tools/run_pester.ps1 under Windows PowerShell 5.1, as the installer
# runs. Each script is dot-sourced: its functions are defined and its main part does
# not run. Stop-Install and Stop-Uninstall are replaced by stand-ins that throw
# "STOP <code>", so a refusal shows without ending the run. Everything written goes
# under Pester's TestDrive. This file is ASCII only: Windows PowerShell 5.1 reads a
# script without a byte-order mark in the system code page, so other characters are
# built from their code points.
#
# Safety: a script is dot-sourced only after Assert-DotSourceGuard has found its
# guard (a top-level "if ($MyInvocation.InvocationName -eq ".") { return }" before
# the main part), because without it the main part would install or uninstall for
# real. While the tests run, LOCALAPPDATA and APPDATA point into TestDrive.

BeforeAll {
    $script:SavedLocal = $env:LOCALAPPDATA
    $script:SavedRoaming = $env:APPDATA
    $env:LOCALAPPDATA = [IO.Directory]::CreateDirectory((Join-Path $TestDrive "Local")).FullName
    $env:APPDATA = [IO.Directory]::CreateDirectory((Join-Path $TestDrive "Roaming")).FullName

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
}

AfterAll {
    $env:LOCALAPPDATA = $script:SavedLocal
    $env:APPDATA = $script:SavedRoaming
}

Describe "the tests' own safety check" {
    It "refuses a script whose main part would run when dot-sourced" {
        $unsafe = Join-Path $TestDrive "unsafe.ps1"
        [IO.File]::WriteAllText($unsafe, "function Get-One { 1 }`r`ntry { throw 'main ran' } finally { }`r`n")
        { Assert-DotSourceGuard $unsafe } | Should -Throw "*no dot-source guard*"
        $late = Join-Path $TestDrive "late.ps1"
        [IO.File]::WriteAllText($late, "try { } finally { }`r`nif (`$MyInvocation.InvocationName -eq `".`") { return }`r`n")
        { Assert-DotSourceGuard $late } | Should -Throw "*no dot-source guard*"
    }
}

Describe "packaging/identity.json mirrors the scripts" {
    BeforeAll {
        $script:Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
        $script:Identity = Get-Content -LiteralPath (Join-Path $script:Repo "packaging\identity.json") -Raw |
            ConvertFrom-Json

        # Every exit code a script can give: Stop-Install/Stop-Uninstall <n> and exit <n>.
        function Get-ExitCode([string]$Path) {
            $tokens = $null; $errors = $null
            $ast = [Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
            $codes = @()
            foreach ($node in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.CommandAst] }, $true)) {
                if (@("Stop-Install", "Stop-Uninstall") -contains $node.GetCommandName() -and
                    $node.CommandElements.Count -gt 1 -and $node.CommandElements[1].Extent.Text -match '^\d$') {
                    $codes += $node.CommandElements[1].Extent.Text
                }
            }
            foreach ($node in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.ExitStatementAst] }, $true)) {
                if ($node.Pipeline -and $node.Pipeline.Extent.Text -match '^\d$') { $codes += $node.Pipeline.Extent.Text }
            }
            return @($codes | Sort-Object -Unique)
        }
    }

    It "install.ps1 uses the identity's names and exit codes" {
        Assert-DotSourceGuard (Join-Path $script:Repo "packaging\install.ps1")
        . (Join-Path $script:Repo "packaging\install.ps1")
        $user = $script:Identity.modes.user
        $AppName | Should -BeExactly $script:Identity.name
        $AppId | Should -BeExactly $script:Identity.app_id
        $RegKey | Should -BeExactly ($user.uninstall_key -replace '^HKCU\\', 'HKCU:\')
        $InstallDir | Should -BeExactly ([Environment]::ExpandEnvironmentVariables($user.install_folder))
        $OwnEntries -join "," | Should -BeExactly ($script:Identity.install_root_entries -join ",")
        $LockName | Should -BeExactly $script:Identity.install_lock
        (@($LogNames | ForEach-Object { "logs\$_" }) -join ",") | Should -BeExactly ($script:Identity.install_log -join ",")
        # Every exit code the script can give, in either mode, and no other.
        $codes = @(@($script:Identity.exit_codes."install.ps1".user.PSObject.Properties.Name) +
            @($script:Identity.exit_codes."install.ps1".machine.PSObject.Properties.Name) | Sort-Object -Unique)
        (Get-ExitCode (Join-Path $script:Repo "packaging\install.ps1")) -join "," | Should -BeExactly ($codes -join ",")
    }

    It "both scripts use the identity's machine names (#111 B6b)" {
        $named = $script:Identity.modes.machine
        foreach ($name in "install.ps1", "uninstall.ps1") {
            Assert-DotSourceGuard (Join-Path $script:Repo "packaging\$name")
            . (Join-Path $script:Repo "packaging\$name")
            $MachineRegKey | Should -BeExactly ($named.uninstall_key -replace '^HKLM\\', 'HKLM:\')
            $EventSourceKey | Should -BeExactly ($named.event_source_key -replace '^HKLM\\', 'HKLM:\')
            (Split-Path -Leaf $EventSourceKey) | Should -BeExactly $named.event_source
            $MachineFolderName | Should -BeExactly (Split-Path -Leaf $named.install_folder)
            $JunctionName | Should -BeExactly $named.launch_junction
            ($JunctionNames -join ",") | Should -BeExactly ((@($named.launch_junction) + @($named.launch_junction_working)) -join ",")
            ("staging-" + ("a" * 32)) | Should -MatchExactly $StagingPattern
            ("staging-" + ("a" * 31)) | Should -Not -MatchExactly $StagingPattern
        }
        Get-MachineRoot | Should -BeExactly ([Environment]::ExpandEnvironmentVariables($named.install_folder))
        $MachineRegSubKey | Should -BeExactly ($named.uninstall_key -replace '^HKLM\\', '')
    }

    It "uninstall.ps1 uses the identity's names and exit codes" {
        Assert-DotSourceGuard (Join-Path $script:Repo "packaging\uninstall.ps1")
        . (Join-Path $script:Repo "packaging\uninstall.ps1")
        $user = $script:Identity.modes.user
        $AppName | Should -BeExactly $script:Identity.name
        $RegKey | Should -BeExactly ($user.uninstall_key -replace '^HKCU\\', 'HKCU:\')
        $InstallDir | Should -BeExactly ([Environment]::ExpandEnvironmentVariables($user.install_folder))
        $OwnEntries -join "," | Should -BeExactly ($script:Identity.install_root_entries -join ",")
        $LockName | Should -BeExactly $script:Identity.install_lock
        (@($LogNames | ForEach-Object { "logs\$_" }) -join ",") | Should -BeExactly ($script:Identity.install_log -join ",")
        $codes = @(@($script:Identity.exit_codes."uninstall.ps1".user.PSObject.Properties.Name) +
            @($script:Identity.exit_codes."uninstall.ps1".machine.PSObject.Properties.Name) | Sort-Object -Unique)
        (Get-ExitCode (Join-Path $script:Repo "packaging\uninstall.ps1")) -join "," | Should -BeExactly ($codes -join ",")
    }

    It "both scripts keep clear of the data folder the identity names" {
        $data = [Environment]::ExpandEnvironmentVariables($script:Identity.data_folders.root)
        foreach ($name in "install.ps1", "uninstall.ps1") {
            Assert-DotSourceGuard (Join-Path $script:Repo "packaging\$name")
            . (Join-Path $script:Repo "packaging\$name")
            Get-DataOverlap $data | Should -BeExactly $data
            Get-DataOverlap ([Environment]::ExpandEnvironmentVariables($script:Identity.data_folders.workspace)) |
                Should -BeExactly $data
            Get-DataOverlap ([Environment]::ExpandEnvironmentVariables($script:Identity.modes.user.install_folder)) |
                Should -BeNullOrEmpty
        }
    }
}

Describe "install.ps1 in both modes" {
    BeforeAll {
        $script:Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
        $script:Install = Join-Path $script:Repo "packaging\install.ps1"
        Assert-DotSourceGuard $script:Install
        . $script:Install
        Mock Stop-Install { throw "STOP $Code" }

        # A synthetic package: the files given (relative name = text), and its manifest.
        function New-TestPackage([string]$Folder, [hashtable]$Files) {
            $manifest = [ordered]@{}
            foreach ($name in ($Files.Keys | Sort-Object)) {
                $path = Join-Path $Folder ($name -replace "/", "\")
                [void][IO.Directory]::CreateDirectory((Split-Path -Parent $path))
                [IO.File]::WriteAllText($path, $Files[$name])
                $manifest[$name] = @((Get-Item -LiteralPath $path).Length, (Get-FileSha256 $path))
            }
            [IO.File]::WriteAllText((Join-Path $Folder "package-manifest.json"), ($manifest | ConvertTo-Json -Depth 3))
        }

        $script:Kelvin = [string][char]0x212A
        $script:SharpS = [string][char]0x00DF
        $script:CapitalSharpS = [string][char]0x1E9E
        $script:Ligature = [string][char]0xFB01
    }

    Context "the manifest check of the staged copy" {
        BeforeEach {
            $script:Package = Join-Path $TestDrive ("p" + [guid]::NewGuid().ToString("N").Substring(0, 8))
            New-TestPackage $script:Package @{
                "app/held.txt" = "held"; "app/kelvin.txt" = "k"; "app/file.txt" = "f"
                ("app/stra" + $script:SharpS + "e.txt") = "s"; "runtime/python.exe" = "synthetic"
            }
        }

        It "accepts the package as built" {
            { Test-PackageFile $script:Package } | Should -Not -Throw
        }

        It "refuses <Label>" -TestCases @(
            @{ Label = "a letter-case variant of a listed name"; Kind = "case" }
            @{ Label = "a Kelvin sign for K"; Kind = "kelvin" }
            @{ Label = "a capital sharp s"; Kind = "sharp-s" }
            @{ Label = "a ligature"; Kind = "ligature" }
            @{ Label = "an extra file"; Kind = "extra" }
            @{ Label = "a missing file"; Kind = "missing" }
            @{ Label = "a changed byte"; Kind = "changed" }
            @{ Label = "a manifest with another letter case"; Kind = "manifest-case" }
        ) {
            param($Label, $Kind)
            $null = $Label
            $app = Join-Path $script:Package "app"
            switch ($Kind) {
                "case" { Rename-Item -LiteralPath (Join-Path $app "held.txt") -NewName "Held.txt" }
                "kelvin" { [IO.File]::WriteAllText((Join-Path $app ($script:Kelvin + "elvin.txt")), "planted") }
                "sharp-s" { [IO.File]::WriteAllText((Join-Path $app ("STRA" + $script:CapitalSharpS + "E.txt")), "planted") }
                "ligature" { [IO.File]::WriteAllText((Join-Path $app ($script:Ligature + "le.txt")), "planted") }
                "extra" { [IO.File]::WriteAllText((Join-Path $app "planted.pth"), "import os") }
                "missing" { Remove-Item -LiteralPath (Join-Path $app "file.txt") }
                "changed" { [IO.File]::WriteAllText((Join-Path $script:Package "runtime\python.exe"), "synthetiC") }
                "manifest-case" {
                    Rename-Item -LiteralPath (Join-Path $script:Package "package-manifest.json") -NewName "Package-Manifest.json"
                }
            }
            { Test-PackageFile $script:Package } | Should -Throw "STOP 4"
        }

        It "refuses a link or junction anywhere in the package, before listing what it points to" {
            $outside = Join-Path $TestDrive "outside"
            [void][IO.Directory]::CreateDirectory($outside)
            [IO.File]::WriteAllText((Join-Path $outside "sentinel.txt"), "someone's file")
            New-Item -ItemType Junction -Path (Join-Path $script:Package "app\linked") -Target $outside | Out-Null
            { Assert-NoLink $script:Package } | Should -Throw "STOP 4"
            { Test-PackageFile $script:Package } | Should -Throw "STOP 4"
            [IO.Directory]::Delete((Join-Path $script:Package "app\linked"), $false)
            Get-Content -LiteralPath (Join-Path $outside "sentinel.txt") | Should -BeExactly "someone's file"
        }
    }

    Context "the order of a run" {
        It "verifies the copy before the workspace check runs from it, and before the swap" {
            $tokens = $null; $errors = $null
            $ast = [Management.Automation.Language.Parser]::ParseFile($script:Install, [ref]$tokens, [ref]$errors)
            [string[]]$calls = @($ast.EndBlock.Statements | Where-Object { $_ -is [Management.Automation.Language.TryStatementAst] } |
                ForEach-Object { $_.Body.FindAll({ param($n) $n -is [Management.Automation.Language.CommandAst] }, $true) } |
                ForEach-Object { $_.Extent.Text })
            $order = @('Assert-NoLink $root', 'New-StagedCopy $root $version', 'Test-WorkspaceGate $staging',
                       'Install-Version $staging $version')
            $at = @($order | ForEach-Object { [array]::IndexOf($calls, $_) })
            $at | Should -Not -Contain -1
            ($at | Sort-Object) -join "," | Should -BeExactly ($at -join ",")
            # New-StagedCopy's last step is the manifest check of the copy.
            $body = $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and
                $n.Name -eq "New-StagedCopy" }, $true)[0].Body.EndBlock.Statements
            $body[$body.Count - 2].Extent.Text | Should -BeExactly 'Test-PackageFile $staging'
        }

        It "starts the app, its pins and the workspace check with the one list of options" {
            $tokens = $null; $errors = $null
            $ast = [Management.Automation.Language.Parser]::ParseFile($script:Install, [ref]$tokens, [ref]$errors)
            $arguments = @($ast.FindAll({ param($n) $n -is [Management.Automation.Language.AssignmentStatementAst] -and
                $n.Left.Extent.Text -match '\.Arguments$' }, $true) | ForEach-Object { $_.Right.Extent.Text })
            $arguments.Count | Should -Be 2                  # the shortcut and the pins
            $arguments | ForEach-Object { $_ | Should -BeExactly '$LaunchArgs' }
            $gate = @($ast.FindAll({ param($n) $n -is [Management.Automation.Language.AssignmentStatementAst] -and
                $n.Left.Extent.Text -eq '$gateArgs' -and $n.Operator -eq "Equals" }, $true))
            $gate.Count | Should -Be 1
            $gate[0].Right.Extent.Text | Should -BeExactly '@($LaunchArgs -split " ") + "--gate"'
        }
    }

    Context "current.json" {
        BeforeEach {
            $Options.InstallDir = Join-Path $TestDrive ("i" + [guid]::NewGuid().ToString("N").Substring(0, 8))
            [void][IO.Directory]::CreateDirectory($Options.InstallDir)
            $script:Record = Join-Path $Options.InstallDir "current.json"
            [IO.File]::WriteAllText($script:Record, '{"version": "0.9.0"}')
        }

        It "is unchanged when the run stops between the temporary file and the replace" {
            $before = [IO.File]::ReadAllBytes($script:Record)
            Mock Move-RecordFile { throw "injected" }
            { Save-RecordFile $script:Record '{"version": "1.0.0"}' } | Should -Throw "injected"
            [IO.File]::ReadAllBytes($script:Record) | Should -Be $before
            Test-Path -LiteralPath "$($script:Record).tmp" | Should -BeTrue
            Remove-LeftoverRecord
            Test-Path -LiteralPath "$($script:Record).tmp" | Should -BeFalse
        }

        It "is replaced whole, with a byte-order mark and a final line break" {
            Save-RecordFile $script:Record '{"version": "1.0.0"}'
            $bytes = [IO.File]::ReadAllBytes($script:Record)
            ($bytes[0..2] -join ",") | Should -BeExactly "239,187,191"
            [Text.Encoding]::UTF8.GetString($bytes, 3, $bytes.Length - 3) | Should -BeExactly "{`"version`": `"1.0.0`"}`r`n"
            Test-Path -LiteralPath "$($script:Record).tmp" | Should -BeFalse
        }
    }

    Context "the installer log" {
        BeforeEach {
            $logs = Join-Path $TestDrive ("l" + [guid]::NewGuid().ToString("N").Substring(0, 8))
            [void][IO.Directory]::CreateDirectory($logs)
            $script:LogFile = Join-Path $logs "install.log"
            $script:LogVersion = "1.0.0"
            $script:LogStep = "copy"
            $script:LogError = $null
        }

        It "writes fixed words and numbers only: no user name, no path, no message" {
            Write-InstallLog "begun"
            $script:LogError = "IOException"
            Write-InstallLog "stopped" 1
            $text = [IO.File]::ReadAllText($script:LogFile)
            $line = '\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ mode=user version=1\.0\.0 step=copy result=(begun|stopped)( code=\d)? elapsed_ms=\d+( error=\w+)?'
            $text | Should -Match "\A$line\r\n$line\r\n\z"
            $text | Should -Match "result=stopped code=1 elapsed_ms=\d+ error=IOException\r\n\z"
            $text | Should -Not -Match ([regex]::Escape($env:USERNAME))
            $text | Should -Not -Match '\\'
        }

        It "keeps one earlier file once it reaches its limit" {
            Set-Variable -Name LogLimitBytes -Value 1024     # this test's limit, read by Write-InstallLog
            foreach ($n in 1..60) { Write-InstallLog "begun" }
            (Get-Item -LiteralPath $script:LogFile).Length | Should -BeLessOrEqual 1024
            (Get-Item -LiteralPath "$($script:LogFile).1").Length | Should -BeLessOrEqual 1024
            @(Get-ChildItem -LiteralPath (Split-Path -Parent $script:LogFile)).Count | Should -Be 2
        }
    }

    Context "one run at a time" {
        It "waits for a run that holds install.lock, then stops with exit 1" {
            $Options.InstallDir = Join-Path $TestDrive "locked"
            [void][IO.Directory]::CreateDirectory($Options.InstallDir)
            $path = Join-Path $Options.InstallDir "install.lock"
            Set-Variable -Name LockWaitSeconds -Value 1      # this test's wait, read by Enter-InstallLock
            $held = [IO.File]::Open($path, "OpenOrCreate", "ReadWrite", "None")
            try { { Enter-InstallLock } | Should -Throw "STOP 1" } finally { $held.Dispose() }
            Enter-InstallLock
            Test-Path -LiteralPath $path | Should -BeTrue
            Exit-InstallLock
            Test-Path -LiteralPath $path | Should -BeFalse      # deleted when it closes
        }
    }
}

Describe "uninstall.ps1 in both modes" {
    BeforeAll {
        $script:Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
        $script:Uninstall = Join-Path $script:Repo "packaging\uninstall.ps1"
        Assert-DotSourceGuard $script:Uninstall
        . $script:Uninstall
        Mock Stop-Uninstall { throw "STOP $Code" }
    }

    It "stops with exit 3 while an install holds install.lock" {
        $Options.InstallDir = Join-Path $TestDrive "u-locked"
        [void][IO.Directory]::CreateDirectory($Options.InstallDir)
        $held = [IO.File]::Open((Join-Path $Options.InstallDir "install.lock"), "OpenOrCreate", "ReadWrite", "None")
        try { { Enter-InstallLock } | Should -Throw "STOP 3" } finally { $held.Dispose() }
    }

    It "counts anything in the log folder but the installer's two log files" {
        $dir = Join-Path $TestDrive "u-logs"
        [void][IO.Directory]::CreateDirectory((Join-Path $dir "logs"))
        foreach ($name in "install.log", "install.log.1") { [IO.File]::WriteAllText((Join-Path $dir "logs\$name"), "x") }
        Get-LogStray $dir | Should -Be 0
        [IO.File]::WriteAllText((Join-Path $dir "logs\notes.txt"), "someone's notes")
        Get-LogStray $dir | Should -Be 1
    }

    It "starts the app with the installer's list of options" {
        $LaunchArgs | Should -BeExactly "-I -B -X disable-remote-debug -m workspace"
        $tokens = $null; $errors = $null
        $ast = [Management.Automation.Language.Parser]::ParseFile($script:Uninstall, [ref]$tokens, [ref]$errors)
        $arguments = @($ast.FindAll({ param($n) $n -is [Management.Automation.Language.AssignmentStatementAst] -and
            $n.Left.Extent.Text -match '\.Arguments$' }, $true) | ForEach-Object { $_.Right.Extent.Text })
        $arguments -join "," | Should -BeExactly '"$LaunchArgs --restore-lid"'
    }
}
