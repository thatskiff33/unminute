# Pester 5 tests of the model package (#111 B7a): tools\intune\New-ModelPackage.ps1,
# which IT runs on its own computer, and packaging\models\Install-Models.ps1, which
# Intune runs as SYSTEM. Run them with tools/run_pester.ps1 under Windows PowerShell 5.1.
#
# Safety (tests/powershell/Isolation.ps1, as in Machine.Tests.ps1): nothing here needs
# administrator rights or can reach this computer's install. LOCALAPPDATA and APPDATA
# point into TestDrive, and a script is loaded only as
# ". (Confirm-Loadable $path); Set-ScratchTarget", which refuses it without its
# dot-source guard or when it reaches Program Files other than through
# $ProgramFilesFolder at its top. Each Install-Models test gets its own scratch
# Program Files (Set-ScratchMachineTarget). Elevation, bitness and the install folder's
# permissions as read are stood in by mocks; Stop-Install and Stop-Package throw. The
# models, zips and packages are synthetic. ASCII only.

BeforeAll {
    . (Join-Path $PSScriptRoot "Isolation.ps1")
    Enter-ScratchProfile $TestDrive
    $script:Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

    function New-TestFolder([string]$Prefix) {
        return [IO.Directory]::CreateDirectory((Join-Path $TestDrive ($Prefix + [guid]::NewGuid().ToString("N").Substring(0, 8)))).FullName
    }

    function Get-TextSha256([string]$Text) {
        $sha = [Security.Cryptography.SHA256]::Create()
        try { return [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))).Replace("-", "").ToLowerInvariant() }
        finally { $sha.Dispose() }
    }

    # Two synthetic models as a release's model-manifest.json lists them: a Parakeet
    # model with a file in a subfolder, and its speech detector. $Revision changes the
    # first one's revision; $Text its file contents.
    function New-OneEntry([string]$Name, [string]$Folder, [string]$Revision, $Contents) {
        return [pscustomobject][ordered]@{
            name = $Name; licence = "MIT"; attribution = "Synthetic $Name"; family = "parakeet"; folder = $Folder
            repository = "example/$Name"; revision = $Revision; gated = $false
            files = @(foreach ($n in $Contents.Keys) {
                [pscustomobject][ordered]@{ name = $n; size = [Text.Encoding]::UTF8.GetByteCount($Contents[$n]); sha256 = (Get-TextSha256 $Contents[$n]) }
            })
            contents = $Contents
        }
    }

    # Two synthetic models as a release's model-manifest.json lists them: a Parakeet
    # model with a file in a subfolder, and its speech detector.
    function New-TestEntry {
        $files = [ordered]@{ "config.json" = '{"m": "one"}'; "kv.bin" = "kv"; "sub/weights.bin" = "weights one" }
        return @((New-OneEntry "m-one" "m-one-int8" ("a" * 40) $files),
                 (New-OneEntry "silero-vad" "silero-vad" ("b" * 40) ([ordered]@{ "silero.onnx" = "detector" })))
    }

    # Writes each entry's files under $Root\<family>\<folder>[\<revision>].
    function Write-TestModel([string]$Root, $Entries, [switch]$WithRevision) {
        foreach ($entry in $Entries) {
            $dir = [IO.Path]::Combine($Root, $entry.family, $entry.folder)
            if ($WithRevision) { $dir = [IO.Path]::Combine($dir, $entry.revision) }
            foreach ($name in $entry.contents.Keys) {
                $path = [IO.Path]::Combine($dir, $name.Replace("/", "\"))
                [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path))
                [IO.File]::WriteAllText($path, $entry.contents[$name])
            }
        }
    }

    # The entries as the release lists them (without the test's contents).
    function Get-Listed($Entries, [switch]$NoPlace) {
        return @(foreach ($entry in $Entries) {
            $copy = $entry | Select-Object -Property * -ExcludeProperty contents
            if ($NoPlace) { $copy = $copy | Select-Object -Property * -ExcludeProperty family, folder }
            $copy
        })
    }

    # A synthetic release zip with version.json and model-manifest.json.
    # Entry names use /, as the release build (Python's zipfile) writes them; .NET
    # Framework's ZipFile.CreateFromDirectory would write \.
    function New-TestZip($Entries, $Release = $true, [switch]$NoPlace) {
        $manifest = @{ _about = "test"; models = @(Get-Listed $Entries -NoPlace:$NoPlace) }
        $files = [ordered]@{
            "Unminute-0.9.0/version.json" = (@{ name = "Unminute"; version = "0.9.0"; release = $Release } | ConvertTo-Json)
            "Unminute-0.9.0/model-manifest.json" = ($manifest | ConvertTo-Json -Depth 6)
            "Unminute-0.9.0/app/workspace/asr_engines.py" = "# synthetic"
        }
        $zip = Join-Path (New-TestFolder "z") "Unminute-0.9.0.zip"
        Add-Type -AssemblyName System.IO.Compression
        $stream = [IO.File]::Open($zip, "CreateNew", "ReadWrite", "None")
        try {
            $archive = [IO.Compression.ZipArchive]::new($stream, [IO.Compression.ZipArchiveMode]::Create)
            try {
                foreach ($name in $files.Keys) {
                    $writer = [IO.StreamWriter]::new($archive.CreateEntry($name).Open(), [Text.UTF8Encoding]::new($false))
                    try { $writer.Write($files[$name]) } finally { $writer.Dispose() }
                }
            } finally { $archive.Dispose() }
        } finally { $stream.Dispose() }
        return @{ Path = $zip; Sha = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant() }
    }

    # A model package folder as New-ModelPackage.ps1 writes it: models.json and the files.
    function New-TestPackage($Entries) {
        $pkg = New-TestFolder "p"
        Write-TestModel (Join-Path $pkg "models") $Entries -WithRevision
        $manifest = [ordered]@{ format = 1; release = "0.9.0"; release_zip_sha256 = ("c" * 64); models = @(Get-Listed $Entries) }
        [IO.File]::WriteAllText((Join-Path $pkg "models.json"), ($manifest | ConvertTo-Json -Depth 6))
        return $pkg
    }

    # The install folder's permissions as the installer creates them (owner
    # Administrators; the four entries), in memory only.
    function New-TestRootSecurity([string]$Owner = "S-1-5-32-544") {
        $security = New-Object Security.AccessControl.DirectorySecurity
        $security.SetAccessRuleProtection($true, $false)
        $security.SetOwner([Security.Principal.SecurityIdentifier]::new($Owner))
        $inherit = [Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit"
        $full = [Security.AccessControl.FileSystemRights]::FullControl
        $read = [Security.AccessControl.FileSystemRights]::ReadAndExecute
        foreach ($rule in @(@("S-1-5-18", $full), @("S-1-5-32-544", $full), @("S-1-5-32-545", $read), @("S-1-15-2-1", $read))) {
            $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                [Security.Principal.SecurityIdentifier]::new($rule[0]), $rule[1], $inherit,
                [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow))
        }
        return $security
    }

    # Every path under $Folder with each file's text (links shown as links).
    function Get-Snapshot([string]$Folder) {
        if (-not [IO.Directory]::Exists($Folder)) { return "" }
        $found = @()
        $skip = $Folder.TrimEnd("\").Length + 1
        $todo = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
        $todo.Push([IO.DirectoryInfo]::new($Folder))
        while ($todo.Count) {
            foreach ($item in $todo.Pop().GetFileSystemInfos()) {
                $rel = $item.FullName.Substring($skip)
                if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { $found += "$rel=<link>" }
                elseif ($item -is [IO.DirectoryInfo]) { $found += "$rel=<dir>"; $todo.Push($item) }
                else { $found += "$rel=" + [IO.File]::ReadAllText($item.FullName) }
            }
        }
        return (($found | Sort-Object) -join "|")
    }
}

AfterAll {
    Exit-ScratchProfile
}

Describe "New-ModelPackage.ps1" {
    BeforeAll {
        $script:New = Join-Path $script:Repo "tools\intune\New-ModelPackage.ps1"
        . (Confirm-Loadable $script:New); Set-ScratchTarget
        Mock Stop-Package { throw "STOP $Message" }
        Mock Write-Host { }
    }

    BeforeEach {
        $script:Entries = New-TestEntry
        $script:Source = New-TestFolder "s"
        Write-TestModel $script:Source $script:Entries
        $script:Zip = New-TestZip $script:Entries
        $script:Out = Join-Path (New-TestFolder "o") "package"
        $Options.ReleaseZip = $script:Zip.Path; $Options.ExpectedSha256 = $script:Zip.Sha
        $Options.Source = $script:Source; $Options.OutputFolder = $script:Out; $Options.Model = @()
    }

    It "builds a package from the verified zip's list and the downloaded models" {
        # Setup's own record beside a model is left out of the package.
        [IO.File]::WriteAllText([IO.Path]::Combine($script:Source, "parakeet", "m-one-int8", ".verified.json"), "{}")
        Invoke-NewModelPackage
        $manifest = Get-Content -LiteralPath (Join-Path $script:Out "models.json") -Raw | ConvertFrom-Json
        $manifest.format | Should -Be 1
        $manifest.release | Should -BeExactly "0.9.0"
        $manifest.release_zip_sha256 | Should -BeExactly $script:Zip.Sha
        (@($manifest.models | ForEach-Object name) -join ",") | Should -BeExactly "m-one,silero-vad"
        $one = [IO.Path]::Combine($script:Out, "models", "parakeet", "m-one-int8", ("a" * 40))
        [IO.File]::ReadAllText((Join-Path $one "sub\weights.bin")) | Should -BeExactly "weights one"
        Test-Path -LiteralPath (Join-Path $one ".verified.json") | Should -BeFalse
        $sha = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $script:Out "models.json")).Hash.ToLowerInvariant()
        [IO.File]::ReadAllText((Join-Path $script:Out "README.txt")) | Should -BeLike "*package-$sha.json*"
        [IO.File]::ReadAllText((Join-Path $script:Out "LICENCES.txt")) | Should -BeLike "*m-one: MIT. Synthetic m-one.*"
        (Get-FileHash -LiteralPath (Join-Path $script:Out "Install-Models.ps1")).Hash |
            Should -BeExactly (Get-FileHash -LiteralPath (Join-Path $script:Repo "packaging\models\Install-Models.ps1")).Hash
    }

    It "warns when this copy of the repository is not the release's version" {
        Mock Get-RepositoryVersion { "0.9.0" }
        Invoke-NewModelPackage
        Should -Invoke Write-Host -Times 0 -Exactly -ParameterFilter { "$Object" -like "WARNING*" }
        Mock Get-RepositoryVersion { "0.8.0" }
        Remove-Item -LiteralPath $script:Out -Recurse -Force
        Invoke-NewModelPackage
        Should -Invoke Write-Host -Times 1 -Exactly -ParameterFilter { "$Object" -like "WARNING*'0.8.0'*0.9.0*release's tag*" }
        Test-Path -LiteralPath (Join-Path $script:Out "models.json") | Should -BeTrue
    }

    It "brings the speech detector with a Parakeet model" {
        $Options.Model = @("m-one")
        Invoke-NewModelPackage
        $manifest = Get-Content -LiteralPath (Join-Path $script:Out "models.json") -Raw | ConvertFrom-Json
        (@($manifest.models | ForEach-Object name) -join ",") | Should -BeExactly "m-one,silero-vad"
    }

    It "credits the speaker model in LICENCES.txt, and only the speaker model" {
        # DEC-074 (#111): the credit, the CC BY 4.0 link, that the files are
        # unmodified, the gate, and the three citations, in UTF-8 with their accents.
        $script:Entries[0].repository = $SpeakerRepository
        $script:Entries[0].licence = "CC BY 4.0 (gated)"
        $script:Zip = New-TestZip $script:Entries
        $Options.ReleaseZip = $script:Zip.Path; $Options.ExpectedSha256 = $script:Zip.Sha
        Invoke-NewModelPackage
        $text = [IO.File]::ReadAllText((Join-Path $script:Out "LICENCES.txt"), [Text.Encoding]::UTF8)
        $text | Should -BeLike "*organisation's decision*"
        $text | Should -BeLike "*Licence: Creative Commons Attribution 4.0 International, https://creativecommons.org/licenses/by/4.0/*"
        $text | Should -BeLike "*Credit: pyannote speaker-diarization-community-1, by pyannoteAI.*"
        $text | Should -BeLike "*The files are unmodified*"
        $text | Should -BeLike "*their own Hugging Face account*"
        $text | Should -BeLike ("*Alexis Plaquet and Herv" + [char]0x00E9 + " Bredin.*INTERSPEECH 2023.*")
        $text | Should -BeLike "*Wespeaker: A research and production oriented speaker embedding*ICASSP 2023*"
        $text | Should -BeLike ("*Luk" + [char]0x00E1 + [char]0x0161 + " Burget.*Computer Speech & Language, 2022.*")
        ([regex]::Matches($text, "Credit: ")).Count | Should -Be 1
        ([regex]::Matches($text, "creativecommons.org")).Count | Should -Be 1
        # The app's "accept terms to download" line is not for a downloaded copy.
        $text | Should -BeLike "*m-one: CC BY 4.0 (gated). Downloaded for this package by the IT team; see Credit and Access below.*"
        $text | Should -Not -BeLike "*Synthetic m-one*"
        ([regex]::Matches($text, "Changes: none")).Count | Should -Be 1
    }

    It "says every CC BY model is unmodified, with no speaker credit for the others" {
        # The Parakeet models are CC BY 4.0 too: each says what changed (nothing).
        $script:Entries[0].licence = "CC BY 4.0"
        $script:Zip = New-TestZip $script:Entries
        $Options.ReleaseZip = $script:Zip.Path; $Options.ExpectedSha256 = $script:Zip.Sha
        Invoke-NewModelPackage
        $text = [IO.File]::ReadAllText((Join-Path $script:Out "LICENCES.txt"), [Text.Encoding]::UTF8)
        $text | Should -BeLike "*m-one: CC BY 4.0. Synthetic m-one.*"
        $text | Should -BeLike "*Licence: Creative Commons Attribution 4.0 International, https://creativecommons.org/licenses/by/4.0/*"
        $text | Should -BeLike "*Changes: none. The files are unmodified, byte for byte the files at the revision above,*each checked by its SHA-256.*"
        ([regex]::Matches($text, "Changes: none")).Count | Should -Be 1
        $text | Should -Not -BeLike "*Credit: *"
        # The MIT speech detector gets neither line.
        $text | Should -BeLike "*silero-vad: MIT. Synthetic silero-vad.*"
        ([regex]::Matches($text, "creativecommons.org")).Count | Should -Be 1
    }

    It "refuses a zip whose SHA-256 is not the one given, and writes nothing" {
        $Options.ExpectedSha256 = "0" * 64
        { Invoke-NewModelPackage } | Should -Throw "STOP the zip's SHA-256 is not the one given*"
        Test-Path -LiteralPath $script:Out | Should -BeFalse
    }

    It "refuses a development build" {
        $script:Zip = New-TestZip $script:Entries -Release $false
        $Options.ReleaseZip = $script:Zip.Path; $Options.ExpectedSha256 = $script:Zip.Sha
        { Invoke-NewModelPackage } | Should -Throw "STOP this is not a release build*"
    }

    It "refuses a release whose list does not say where models go" {
        $script:Zip = New-TestZip $script:Entries -NoPlace
        $Options.ReleaseZip = $script:Zip.Path; $Options.ExpectedSha256 = $script:Zip.Sha
        { Invoke-NewModelPackage } | Should -Throw "STOP*older than the model package*"
    }

    It "takes the list only from the zip given" {
        # A list beside the models naming other files changes nothing: the file
        # that does not match the zip's list is refused.
        $path = [IO.Path]::Combine($script:Source, "parakeet", "m-one-int8", "config.json")
        [IO.File]::WriteAllText($path, '{"m": "two"}')
        [IO.File]::WriteAllText((Join-Path $script:Source "model-manifest.json"), "{}")
        { Invoke-NewModelPackage } | Should -Throw "STOP m-one: config.json does not match its checksum*"
        @(Get-ChildItem -LiteralPath $script:Out -Recurse -File -ErrorAction SilentlyContinue).Count | Should -Be 0
    }

    It "refuses a file whose name differs only by <Label>" -TestCases @(
        @{ Label = "letter case"; From = "config.json"; To = "Config.json" }
        @{ Label = "a Kelvin sign for k"; From = "kv.bin"; To = ([string][char]0x212A + "v.bin") }
    ) {
        param($Label, $From, $To)
        $null = $Label
        $dir = [IO.Path]::Combine($script:Source, "parakeet", "m-one-int8")
        [IO.File]::Move((Join-Path $dir $From), (Join-Path $dir "renaming.tmp"))
        [IO.File]::Move((Join-Path $dir "renaming.tmp"), (Join-Path $dir $To))
        { Invoke-NewModelPackage } | Should -Throw "STOP m-one: its folder in -Source holds a file that is not the model's*"
    }

    It "refuses <Label>" -TestCases @(
        @{ Label = "an extra file"; Change = "extra" }
        @{ Label = "a missing file"; Change = "missing" }
        @{ Label = "a file of the wrong size"; Change = "size" }
    ) {
        param($Label, $Change)
        $null = $Label
        $dir = [IO.Path]::Combine($script:Source, "parakeet", "m-one-int8")
        switch ($Change) {
            "extra" { [IO.File]::WriteAllText((Join-Path $dir "planted.py"), "x") }
            "missing" { [IO.File]::Delete((Join-Path $dir "sub\weights.bin")) }
            "size" { [IO.File]::WriteAllText((Join-Path $dir "sub\weights.bin"), "weights one and more") }
        }
        { Invoke-NewModelPackage } | Should -Throw "STOP m-one:*"
    }

    It "refuses a link in a model's folder" {
        $outside = New-TestFolder "x"
        New-Item -ItemType Junction -Path ([IO.Path]::Combine($script:Source, "parakeet", "m-one-int8", "linked")) -Target $outside | Out-Null
        { Invoke-NewModelPackage } | Should -Throw "STOP m-one: its folder in -Source is missing or holds a link*"
    }

    It "refuses an output folder that is not empty" {
        [void][IO.Directory]::CreateDirectory($script:Out)
        [IO.File]::WriteAllText((Join-Path $script:Out "keep.txt"), "x")
        { Invoke-NewModelPackage } | Should -Throw "STOP -OutputFolder is not empty*"
        [IO.File]::ReadAllText((Join-Path $script:Out "keep.txt")) | Should -BeExactly "x"
    }
}

Describe "Install-Models.ps1" {
    BeforeAll {
        $script:Script = Join-Path $script:Repo "packaging\models\Install-Models.ps1"
        . (Confirm-Loadable $script:Script); Set-ScratchTarget
        Mock Stop-Install { throw "STOP $Code" }
        Mock Test-Elevated { $true }
        Mock Test-64BitProcess { $true }
        Mock Get-FolderSecurity { New-TestRootSecurity }

        function Invoke-TestInstall([switch]$Remove) {
            try { if ($Remove) { Invoke-ModelUninstall } else { Invoke-ModelInstall } } finally { Exit-InstallLock }
        }
        function Get-Record([string]$Dir) {
            return Get-Content -LiteralPath (Join-Path $Dir "models.verified.json") -Raw | ConvertFrom-Json
        }
    }

    BeforeEach {
        Set-ScratchMachineTarget
        $script:Root = [IO.Directory]::CreateDirectory((Join-Path $ProgramFilesFolder "Unminute")).FullName
        [IO.File]::WriteAllText((Join-Path $script:Root "current.json"), '{"version": "1.0.0", "mode": "machine"}')
        $script:Slot = [IO.Directory]::CreateDirectory((Join-Path $script:Root "versions\1.0.0")).FullName
        [IO.File]::WriteAllText((Join-Path $script:Slot "model-manifest.json"), '{"models": [{"revision": "' + ("d" * 40) + '"}]}')
        $script:Entries = New-TestEntry
        $Options.Package = New-TestPackage $script:Entries
        $Options.Silent = $true; $Options.InstallDir = ""
        # A short wait, so a brief hold on a file just written (Defender, the indexer)
        # is waited out as in a real run; the test that holds a file open on purpose
        # sets 0 itself (the review of 7461e04 found 0 here made 4 of 5 runs fail).
        Set-Variable -Name LockRetrySeconds -Value 3
        $script:LogFile = $null; $script:LogStep = "start"
        $script:Models = Join-Path $script:Root "models"
        $script:One = [IO.Path]::Combine($script:Models, "parakeet", "m-one-int8", ("a" * 40))
    }

    AfterEach {
        Exit-InstallLock
    }

    It "has only scratch places, and refuses the real Program Files" {
        Test-ScratchPath $ProgramFilesFolder | Should -BeTrue
        Get-MachineRoot | Should -BeExactly $script:Root
        $saved = $ProgramFilesFolder
        $ProgramFilesFolder = [Environment]::GetFolderPath("ProgramFiles")
        { Assert-ScratchTarget } | Should -Throw "*Program Files*"
        $ProgramFilesFolder = $saved
        { Assert-ScratchTarget } | Should -Not -Throw
    }

    It "installs each model, with a record and the detection file" {
        Invoke-TestInstall
        [IO.File]::ReadAllText((Join-Path $script:One "sub\weights.bin")) | Should -BeExactly "weights one"
        $record = Get-Record $script:One
        $record.repo | Should -BeExactly "example/m-one"
        $record.revision | Should -BeExactly ("a" * 40)
        $record.folder | Should -BeExactly "m-one-int8"
        $record.ok | Should -BeTrue
        $info = [IO.FileInfo]::new((Join-Path $script:One "sub\weights.bin"))
        $wanted = ($info.LastWriteTimeUtc.ToFileTimeUtc() - 116444736000000000L) * 100L
        @($record.files."sub/weights.bin") -join "," | Should -BeExactly "$($info.Length),$wanted"
        # UTF-8 without a byte-order mark: Python's json.loads refuses one.
        [IO.File]::ReadAllBytes((Join-Path $script:One "models.verified.json"))[0] | Should -Be ([byte][char]"{")
        $sha = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $Options.Package "models.json")).Hash.ToLowerInvariant()
        Test-Path -LiteralPath (Join-Path $script:Models "package-$sha.json") -PathType Leaf | Should -BeTrue
        @([IO.DirectoryInfo]::new($script:Models).GetDirectories() | Where-Object { $_.Name.StartsWith(".") }).Count | Should -Be 0
        [IO.File]::ReadAllText((Join-Path $script:Root "logs\install.log")) | Should -BeLike "*mode=models step=done result=ok code=0*"
        Test-Path -LiteralPath (Join-Path $script:Root "install.lock") | Should -BeFalse
    }

    It "keeps a verified copy of the same revision as it is" {
        Invoke-TestInstall
        $before = [IO.FileInfo]::new((Join-Path $script:One "config.json")).CreationTimeUtc.Ticks
        Start-Sleep -Milliseconds 50
        Invoke-TestInstall
        [IO.FileInfo]::new((Join-Path $script:One "config.json")).CreationTimeUtc.Ticks | Should -Be $before
    }

    It "replaces a damaged copy" {
        Invoke-TestInstall
        [IO.File]::WriteAllText((Join-Path $script:One "sub\weights.bin"), "weights 0ne")
        Invoke-TestInstall
        [IO.File]::ReadAllText((Join-Path $script:One "sub\weights.bin")) | Should -BeExactly "weights one"
        @([IO.DirectoryInfo]::new($script:Models).GetDirectories() | Where-Object { $_.Name.StartsWith(".") }).Count | Should -Be 0
    }

    It "leaves a folder in use and exits 3, without the detection file" {
        Invoke-TestInstall
        @(Get-ChildItem -LiteralPath $script:Models -Filter "package-*.json").Count | Should -Be 1
        [IO.File]::WriteAllText((Join-Path $script:One "sub\weights.bin"), "weights 0ne")
        # As the app holds a model open: read, sharing reading only. Refused at once.
        Set-Variable -Name LockRetrySeconds -Value 0
        $open = [IO.File]::Open((Join-Path $script:One "config.json"), "Open", "Read", "Read")
        try {
            { Invoke-TestInstall } | Should -Throw "STOP 3"
        } finally { $open.Dispose() }
        [IO.File]::ReadAllText((Join-Path $script:One "sub\weights.bin")) | Should -BeExactly "weights 0ne"
        @(Get-ChildItem -LiteralPath $script:Models -Filter "package-*.json").Count | Should -Be 0
        @([IO.DirectoryInfo]::new($script:Models).GetDirectories() | Where-Object { $_.Name.StartsWith(".") }).Count | Should -Be 0
    }

    It "puts the old copy back when the new one cannot be moved into place" {
        Invoke-TestInstall
        [IO.File]::WriteAllText((Join-Path $script:One "sub\weights.bin"), "weights 0ne")
        # The first rename (the old copy aside) works; the second (the new copy into
        # place) is refused as a file in use.
        $script:Moves = 0
        Mock Invoke-LockRetry {
            $script:Moves++
            if ($script:Moves -ge 2) { throw [IO.IOException]::new("in use", -2147024864) }
            $Action.Invoke()
        }
        { Invoke-TestInstall } | Should -Throw "STOP 3"
        [IO.File]::ReadAllText((Join-Path $script:One "sub\weights.bin")) | Should -BeExactly "weights 0ne"
        @([IO.DirectoryInfo]::new($script:Models).GetDirectories() | Where-Object { $_.Name.StartsWith(".") }).Count | Should -Be 0
    }

    It "removes only revisions nothing pins" {
        $folder = [IO.Path]::Combine($script:Models, "parakeet", "m-one-int8")
        foreach ($name in ("d" * 40), ("e" * 40), "not-a-revision") {
            [void][IO.Directory]::CreateDirectory((Join-Path $folder $name))
        }
        Invoke-TestInstall
        # The package's own revision (no installed version pins it yet), the one an
        # installed version pins, and a folder of another shape stay.
        (@([IO.Directory]::GetDirectories($folder) | ForEach-Object { Split-Path -Leaf $_ } | Sort-Object) -join ",") |
            Should -BeExactly ((@(("a" * 40), ("d" * 40), "not-a-revision") | Sort-Object) -join ",")
    }

    It "removes nothing when an installed version's model list cannot be read" {
        [IO.File]::WriteAllText((Join-Path $script:Slot "model-manifest.json"), "{not json")
        $old = [IO.Path]::Combine($script:Models, "parakeet", "m-one-int8", ("e" * 40))
        [void][IO.Directory]::CreateDirectory($old)
        Mock Write-Warn { }
        Invoke-TestInstall
        [IO.Directory]::Exists($old) | Should -BeTrue
    }

    It "refuses a link where models go: <Label>" -TestCases @(
        @{ Label = "the models folder"; Where = "" }
        @{ Label = "a family folder"; Where = "parakeet" }
    ) {
        param($Label, $Where)
        $null = $Label
        $outside = New-TestFolder "x"
        if ($Where) { [void][IO.Directory]::CreateDirectory($script:Models) }
        New-Item -ItemType Junction -Path (Join-Path $script:Models $Where) -Target $outside | Out-Null
        { Invoke-TestInstall } | Should -Throw "STOP 4"
        @(Get-ChildItem -LiteralPath $outside -Force).Count | Should -Be 0
    }

    It "refuses a package that does not match models.json: <Label>" -TestCases @(
        @{ Label = "an extra file"; Change = "extra" }
        @{ Label = "a changed file"; Change = "changed" }
        @{ Label = "a link"; Change = "link" }
        @{ Label = "a manifest that is not valid"; Change = "manifest" }
    ) {
        param($Label, $Change)
        $null = $Label
        $dir = [IO.Path]::Combine($Options.Package, "models", "parakeet", "m-one-int8", ("a" * 40))
        switch ($Change) {
            "extra" { [IO.File]::WriteAllText([IO.Path]::Combine($Options.Package, "models", "planted.bin"), "x") }
            "changed" { [IO.File]::WriteAllText((Join-Path $dir "config.json"), '{"m": "0ne"}') }
            "link" { New-Item -ItemType Junction -Path (Join-Path $dir "linked") -Target (New-TestFolder "x") | Out-Null }
            "manifest" { [IO.File]::WriteAllText((Join-Path $Options.Package "models.json"), '{"format": 2, "models": []}') }
        }
        { Invoke-TestInstall } | Should -Throw "STOP 4"
        Test-Path -LiteralPath $script:Models | Should -BeFalse
    }

    It "refuses without a machine install of Unminute: <Label>" -TestCases @(
        @{ Label = "no install record"; Change = "record" }
        @{ Label = "a 32-bit run"; Change = "bits" }
        @{ Label = "no administrator rights"; Change = "elevation" }
    ) {
        param($Label, $Change)
        $null = $Label
        switch ($Change) {
            "record" { [IO.File]::Delete((Join-Path $script:Root "current.json")) }
            "bits" { Mock Test-64BitProcess { $false } }
            "elevation" { Mock Test-Elevated { $false } }
        }
        { Invoke-TestInstall } | Should -Throw "STOP 2"
        Test-Path -LiteralPath $script:Models | Should -BeFalse
    }

    It "refuses an install folder whose permissions have changed" {
        Mock Get-FolderSecurity { New-TestRootSecurity "S-1-5-32-545" }
        { Invoke-TestInstall } | Should -Throw "STOP 4"
        Test-Path -LiteralPath $script:Models | Should -BeFalse
    }

    It "-Uninstall removes the models and the detection file, nothing else" {
        Invoke-TestInstall
        $before = Get-Snapshot $script:Slot
        Invoke-TestInstall -Remove
        Test-Path -LiteralPath $script:Models | Should -BeFalse
        Get-Snapshot $script:Slot | Should -BeExactly $before
        Test-Path -LiteralPath (Join-Path $script:Root "current.json") | Should -BeTrue
    }

    It "refuses a package whose model differs from what an installed version pins" {
        # #451: the same family, folder and revision as an installed version's own
        # list, but another file (or size, or SHA-256): IT's record would vouch for it.
        $pinned = @(Get-Listed $script:Entries)
        $pinned[0].files[2].sha256 = "f" * 64
        [IO.File]::WriteAllText((Join-Path $script:Slot "model-manifest.json"), (@{ models = $pinned } | ConvertTo-Json -Depth 6))
        { Invoke-TestInstall } | Should -Throw "STOP 4"
        Test-Path -LiteralPath $script:Models | Should -BeFalse
    }

    It "installs a package that matches what an installed version pins" {
        [IO.File]::WriteAllText((Join-Path $script:Slot "model-manifest.json"),
            (@{ models = @(Get-Listed $script:Entries) } | ConvertTo-Json -Depth 6))
        Invoke-TestInstall
        [IO.File]::ReadAllText((Join-Path $script:One "sub\weights.bin")) | Should -BeExactly "weights one"
    }

    It "tells a permission fault from a file in use" {
        # #451: a sharing violation is in use; access denied is in use only where the
        # run can still write. The scratch folder denies this user creating files.
        $open = New-TestFolder "w"
        $closed = New-TestFolder "d"
        $me = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($me, "CreateFiles, WriteData", "Deny")
        $acl = [IO.DirectoryInfo]::new($closed).GetAccessControl()
        $acl.AddAccessRule($rule)
        [IO.DirectoryInfo]::new($closed).SetAccessControl($acl)
        try {
            $inUse = [IO.IOException]::new("in use", -2147024864)
            $denied = [UnauthorizedAccessException]::new("denied")
            $folderDenied = [IO.IOException]::new("denied", -2147024891)
            Test-TransientLock $inUse (Join-Path $closed "x") | Should -BeTrue
            Test-TransientLock $inUse | Should -BeTrue
            Test-TransientLock $denied (Join-Path $open "x") | Should -BeTrue
            Test-TransientLock $folderDenied (Join-Path $open "x") | Should -BeTrue
            Test-TransientLock $denied (Join-Path $closed "x") | Should -BeFalse
            Test-TransientLock $folderDenied (Join-Path $closed "x") | Should -BeFalse
            Test-TransientLock $denied | Should -BeFalse
            Test-TransientLock ([IO.IOException]::new("other", -2147024784)) (Join-Path $open "x") | Should -BeFalse
            @(Get-ChildItem -LiteralPath $open -Force).Count | Should -Be 0     # the probe never stays
        } finally {
            [void]$acl.RemoveAccessRule($rule)
            [IO.DirectoryInfo]::new($closed).SetAccessControl($acl)
        }
    }

    It "fails, rather than asking to be run again, when it cannot change a model folder" {
        # #451: the old copy cannot be moved aside because this run may not change the
        # folder (a permission fault), not because a file is in use: not exit 3, which
        # Intune would try again without end. Nothing is lost.
        Invoke-TestInstall
        [IO.File]::WriteAllText((Join-Path $script:One "sub\weights.bin"), "weights 0ne")
        Set-Variable -Name LockRetrySeconds -Value 0
        Mock Get-AclChangeCount { 0 }
        $parent = Split-Path -Parent $script:One
        $me = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $rules = @(
            @($parent, [Security.AccessControl.FileSystemAccessRule]::new($me, "CreateFiles, WriteData, DeleteSubdirectoriesAndFiles", "Deny")),
            @($script:One, [Security.AccessControl.FileSystemAccessRule]::new($me, "Delete", "Deny")))
        foreach ($r in $rules) {
            $acl = [IO.DirectoryInfo]::new($r[0]).GetAccessControl(); $acl.AddAccessRule($r[1]); [IO.DirectoryInfo]::new($r[0]).SetAccessControl($acl)
        }
        try {
            $caught = $null
            try { Invoke-TestInstall } catch { $caught = $_ }
            "$caught" | Should -Not -BeExactly "STOP 3"
            $caught.Exception.GetBaseException() | Should -BeOfType [IO.IOException]
        } finally {
            foreach ($r in $rules) {
                $acl = [IO.DirectoryInfo]::new($r[0]).GetAccessControl(); [void]$acl.RemoveAccessRule($r[1]); [IO.DirectoryInfo]::new($r[0]).SetAccessControl($acl)
            }
        }
        [IO.File]::ReadAllText((Join-Path $script:One "sub\weights.bin")) | Should -BeExactly "weights 0ne"
    }

    It "-Uninstall removes nothing when a model file is in use, and exits 3" {
        Invoke-TestInstall
        $before = Get-Snapshot $script:Models
        Set-Variable -Name LockRetrySeconds -Value 0
        $open = [IO.File]::Open((Join-Path $script:One "sub\weights.bin"), "Open", "Read", "Read")
        try {
            { Invoke-TestInstall -Remove } | Should -Throw "STOP 3"
        } finally { $open.Dispose() }
        Get-Snapshot $script:Models | Should -BeExactly $before
        @([IO.DirectoryInfo]::new($script:Root).GetDirectories() | Where-Object { $_.Name -like "staging-*" }).Count | Should -Be 0
    }

    It "-Uninstall finishes with nothing to remove: <Label>" -TestCases @(
        @{ Label = "Unminute is no longer installed for everyone"; Change = "gone" }
        @{ Label = "no models folder"; Change = "none" }
        @{ Label = "the app's record is gone"; Change = "record" }
    ) {
        param($Label, $Change)
        $null = $Label
        $programFiles = Split-Path -Parent $script:Root
        switch ($Change) {
            "gone" { Remove-Item -LiteralPath $script:Root -Recurse -Force }
            "record" { Invoke-TestInstall; [IO.File]::Delete((Join-Path $script:Root "current.json")) }
        }
        $before = Get-Snapshot $programFiles
        Invoke-TestInstall -Remove
        if ($Change -eq "record") {
            # The models of an install whose record went still go.
            Test-Path -LiteralPath $script:Models | Should -BeFalse
        } else {
            (Get-Snapshot $programFiles) -replace "\|?Unminute\\logs[^|]*", "" |
                Should -BeExactly ($before -replace "\|?Unminute\\logs[^|]*", "")
        }
        Test-Path -LiteralPath (Join-Path $script:Root "install.lock") | Should -BeFalse
    }
}
