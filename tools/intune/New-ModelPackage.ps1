<#
.SYNOPSIS
    Builds a model package for Intune from models downloaded with Unminute's own Setup,
    checked against a verified release (#111 B7a).

.DESCRIPTION
    IT downloads the models once with the app's Setup on any computer (the speaker
    model needs IT's Hugging Face token), verifies the release with
    Get-VerifiedRelease.ps1, which prints the zip's SHA-256, then runs this script with
    that zip and hash. No network is used. The script:
      - hashes the zip and stops unless it matches -ExpectedSha256;
      - reads version.json and model-manifest.json from inside that zip, in memory,
        without unpacking it, and stops unless it is a release build ("release": true)
        whose model list says where each model goes (releases from #111 B7a on);
      - for each model chosen (-Model, or every model whose folder is in -Source),
        checks its folder in -Source (<Source>\<family>\<folder>, as Setup makes it):
        exactly the listed files, by exact name (only Setup's own .verified.json may be
        there too), no link anywhere;
      - copies the listed files only and checks every copy's size and SHA-256;
      - writes models.json (the chosen models from the zip's list, the release and the
        zip's SHA-256), Install-Models.ps1 (from packaging\models beside this script's
        repository folder), LICENCES.txt and README.txt (the Intune settings, with the
        detection file's name).
    Prints PACKAGE READY and the SHA-256 of models.json and exits 0. Any problem prints
    NOT READY, removes what it wrote, and exits 1.

.PARAMETER ReleaseZip
    The release zip Get-VerifiedRelease.ps1 verified.

.PARAMETER ExpectedSha256
    The SHA-256 Get-VerifiedRelease.ps1 printed for that zip.

.PARAMETER Source
    The model folder Setup downloaded into: %LOCALAPPDATA%\Unminute\models.

.PARAMETER OutputFolder
    A new or empty folder for the package.

.PARAMETER Model
    The models to include, by the names in the release's model-manifest.json (such as
    parakeet-tdt-0.6b-v2). Parakeet's speech detector, silero-vad, comes with any
    Parakeet model. Default: every model whose folder is in -Source.

.EXAMPLE
    powershell -NoProfile -File tools\intune\New-ModelPackage.ps1 -ReleaseZip .\Unminute-<version>-verified\Unminute-<version>.zip -ExpectedSha256 <hash> -Source $env:LOCALAPPDATA\Unminute\models -OutputFolder .\Unminute-models-<version>
#>
[CmdletBinding()]
param(
    [string]$ReleaseZip = "",
    [string]$ExpectedSha256 = "",
    [string]$Source = "",
    [string]$OutputFolder = "",
    [string[]]$Model = @()
)

$ErrorActionPreference = "Stop"
$Options = @{ ReleaseZip = $ReleaseZip; ExpectedSha256 = $ExpectedSha256; Source = $Source
             OutputFolder = $OutputFolder; Model = $Model }
$NamePattern = '^[0-9A-Za-z][0-9A-Za-z._-]{0,99}\z'
$RevisionPattern = '^[0-9a-f]{40}\z'
$PartPattern = '^[0-9A-Za-z_][0-9A-Za-z._-]{0,99}\z'
$SetupMarker = ".verified.json"
$Detector = "silero-vad"

# packaging\models in the repository this script is in (tools\intune\..\..).
function Get-ScriptsFolder {
    return Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "packaging\models"
}

# The version of the repository copy this script runs from (its VERSION file), whose
# Install-Models.ps1 goes into the package; "" when it cannot be read.
function Get-RepositoryVersion {
    $path = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "VERSION"
    try { return ([IO.File]::ReadAllText($path)).Trim() } catch { return "" }
}

function Stop-Package([string]$Message) {
    [Console]::Error.WriteLine("NOT READY: $Message")
    exit 1
}

function Test-Link([IO.FileSystemInfo]$Item) {
    return [bool]($Item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

# Removes what this run wrote in the output folder (it was new or empty), never
# through a link.
function Remove-PackageOutput([string]$Dir) {
    foreach ($item in [IO.DirectoryInfo]::new($Dir).GetFileSystemInfos()) {
        if ($item -is [IO.DirectoryInfo] -and -not (Test-Link $item)) { Remove-PackageOutput $item.FullName; $item.Delete(); continue }
        $item.Delete()
    }
}

function Get-StreamSha256([IO.Stream]$Stream) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($Stream)).Replace("-", "").ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-FileSha256([string]$Path) {
    (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

# version.json and model-manifest.json from the zip: one handle that shares reading
# only, so the bytes read are the bytes hashed. Each by its exact entry name under the
# zip's one top folder.
function Read-Release([string]$Zip, [string]$Expected) {
    $stream = [IO.FileStream]::new($Zip, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read, 1MB)
    try {
        $actual = Get-StreamSha256 $stream
        if ($actual -cne $Expected.ToLowerInvariant()) {
            Stop-Package "the zip's SHA-256 is not the one given. Verify the release again with Get-VerifiedRelease.ps1."
        }
        Add-Type -AssemblyName System.IO.Compression
        $stream.Position = 0
        $archive = [IO.Compression.ZipArchive]::new($stream, [IO.Compression.ZipArchiveMode]::Read, $true)
        try {
            $tops = @($archive.Entries | ForEach-Object { $_.FullName.Split("/")[0] } | Sort-Object -Unique -CaseSensitive)
            if ($tops.Count -ne 1) { Stop-Package "the zip does not hold one folder." }
            $found = @{}
            foreach ($name in "version.json", "model-manifest.json") {
                $entries = @($archive.Entries | Where-Object { $_.FullName -ceq "$($tops[0])/$name" })
                if ($entries.Count -ne 1) { Stop-Package "the zip has no $name." }
                $reader = [IO.StreamReader]::new($entries[0].Open(), [Text.UTF8Encoding]::new($false))
                try { $found[$name] = $reader.ReadToEnd() } finally { $reader.Dispose() }
            }
        } finally { $archive.Dispose() }
    } finally { $stream.Dispose() }
    try {
        $version = ConvertFrom-Json -InputObject $found["version.json"]
        $manifest = ConvertFrom-Json -InputObject $found["model-manifest.json"]
    } catch { Stop-Package "the zip's version.json or model-manifest.json cannot be read." }
    if ($version.release -isnot [bool] -or -not $version.release) {
        Stop-Package "this is not a release build. Use a release verified by Get-VerifiedRelease.ps1."
    }
    if ($version.version -isnot [string] -or $version.version -cnotmatch '^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\z') {
        Stop-Package "the zip's version.json names no valid version."
    }
    foreach ($entry in @($manifest.models)) {
        if ($entry.family -isnot [string] -or $entry.folder -isnot [string] -or $entry.name -isnot [string] -or
            $entry.family -cnotmatch $NamePattern -or $entry.folder -cnotmatch $NamePattern -or
            $entry.revision -isnot [string] -or $entry.revision -cnotmatch $RevisionPattern) {
            Stop-Package "this release's model list does not say where each model goes: it is older than the model package. Use a newer release."
        }
        foreach ($file in @($entry.files)) {
            $ok = $file.name -is [string] -and $file.sha256 -is [string] -and $file.sha256 -cmatch '^[0-9a-f]{64}\z' -and
                (($file.size -is [int]) -or ($file.size -is [long]))
            if ($ok) { foreach ($part in $file.name.Split("/")) { if ($part -cnotmatch $PartPattern) { $ok = $false } } }
            if (-not $ok) { Stop-Package "this release's model list names a file that is not valid." }
        }
    }
    return @{ Version = $version.version; Sha256 = $actual; Models = @($manifest.models) }
}

# Every file under $Dir by its relative path with / (an ordinal dictionary), or $null
# when $Dir or anything in it is a link or junction.
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

# The models to package: those named, or every one whose folder is in -Source; with
# the speech detector whenever a Parakeet model comes.
function Select-Model($Models, [string]$From, [string[]]$Names) {
    $chosen = @()
    foreach ($entry in $Models) {
        $wanted = if ($Names.Count) { $Names -ccontains $entry.name }
                  else { [IO.Directory]::Exists([IO.Path]::Combine($From, $entry.family, $entry.folder)) }
        if ($wanted) { $chosen += $entry }
    }
    foreach ($name in $Names) {
        if (@($Models | Where-Object { $_.name -ceq $name }).Count -ne 1) { Stop-Package "the release has no model named $name." }
    }
    $parakeet = @($chosen | Where-Object { $_.family -ceq "parakeet" -and $_.name -cne $Detector }).Count
    if ($parakeet -and -not @($chosen | Where-Object { $_.name -ceq $Detector }).Count) {
        $chosen += @($Models | Where-Object { $_.name -ceq $Detector })
    }
    if (-not $chosen.Count) { Stop-Package "no model was found in -Source. Download the models with Unminute's Setup first." }
    return $chosen
}

# One model's folder in -Source: exactly its files (and Setup's record), no link;
# the files copied, then each copy's size and SHA-256 checked.
function Copy-Model($Entry, [string]$From, [string]$To) {
    $dir = [IO.Path]::Combine($From, $Entry.family, $Entry.folder)
    $files = Get-FolderListing $dir
    if ($null -eq $files) { Stop-Package "$($Entry.name): its folder in -Source is missing or holds a link." }
    $listed = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($file in $Entry.files) { [void]$listed.Add($file.name) }
    foreach ($rel in $files.Keys) {
        if (-not $listed.Contains($rel) -and $rel -cne $SetupMarker) {
            Stop-Package "$($Entry.name): its folder in -Source holds a file that is not the model's: $rel"
        }
    }
    $target = [IO.Path]::Combine($To, "models", $Entry.family, $Entry.folder, $Entry.revision)
    foreach ($file in $Entry.files) {
        $found = $null
        if (-not $files.TryGetValue($file.name, [ref]$found)) { Stop-Package "$($Entry.name): $($file.name) is missing in -Source." }
        $copy = [IO.Path]::Combine($target, $file.name.Replace("/", "\"))
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($copy))
        [IO.File]::Copy($found.FullName, $copy, $false)
        if ([IO.FileInfo]::new($copy).Length -ne [long]$file.size -or (Get-FileSha256 $copy) -cne $file.sha256) {
            Stop-Package "$($Entry.name): $($file.name) does not match its checksum. Repair it in Unminute's Setup, then run this again."
        }
    }
}

function Write-Text([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text.Replace("`r`n", "`n").Replace("`n", "`r`n"), [Text.UTF8Encoding]::new($false))
}

# The speaker model's attribution under CC BY 4.0 (the owner's decision DEC-074 of 28
# October 2026, #111): the credit, the licence's link, that the files are unmodified,
# and the three citations on its model card (huggingface.co, read 28 October 2026).
# Kept equal to docs/PACKAGING.md by tests/test_model_package.py. ASCII only: the
# accented letters are written by their code (e acute, a acute, s caron).
$SpeakerRepository = "pyannote/speaker-diarization-community-1"
function Get-SpeakerCredit {
    $e = [string][char]0x00E9; $a = [string][char]0x00E1; $s = [string][char]0x0161
    return @(
        "  Credit: pyannote speaker-diarization-community-1, pyannote (pyannoteAI).",
        "  Changes: none. The files are unmodified, byte for byte the files at the revision above,",
        "  each checked by its SHA-256.",
        "  Access: the files are gated on Hugging Face. Whoever downloads them accepts the",
        "  conditions there with their own Hugging Face account; for this package, the IT team",
        "  that downloaded them did so.",
        "  Citations (from the model card):",
        "    Alexis Plaquet and Herv$($e) Bredin. Powerset multi-class cross entropy loss for neural",
        "    speaker diarization. Proc. INTERSPEECH 2023.",
        "    Hongji Wang, Chengdong Liang, Shuai Wang, Zhengyang Chen, Binbin Zhang, Xu Xiang, Yanlei",
        "    Deng and Yanmin Qian. Wespeaker: A research and production oriented speaker embedding",
        "    learning toolkit. ICASSP 2023, IEEE International Conference on Acoustics, Speech and",
        "    Signal Processing (ICASSP), pages 1-5. IEEE, 2023.",
        "    Federico Landini, J$($a)n Profant, Mireia Diez and Luk$($a)$($s) Burget. Bayesian HMM clustering of",
        "    x-vector sequences (VBx) in speaker diarization: theory, implementation and analysis on",
        "    standard tasks. Computer Speech & Language, 2022.")
}

# The licence and credit for each model, from the release's list (docs/MODEL_MANIFEST.md).
function Get-LicenceText([string]$Version, $Chosen) {
    $lines = @("Models in this package (Unminute $Version)", "",
        "Each model keeps its own licence. Redistributing them inside your organisation is your",
        "organisation's decision; the gated speaker model's terms are on its Hugging Face page.", "")
    foreach ($entry in $Chosen) {
        $speaker = [string]$entry.repository -ceq $SpeakerRepository
        if ($speaker) {
            # The app's own line for it says to accept its terms to download; this
            # package's copy is already downloaded (Access, below).
            $lines += "$($entry.name): $($entry.licence). Downloaded for this package by the IT team; see Credit and Access below."
        } else {
            $lines += "$($entry.name): $($entry.licence). $($entry.attribution)."
        }
        $lines += "  Source: https://huggingface.co/$($entry.repository) (revision $($entry.revision))"
        if ([string]$entry.licence -like "CC BY 4.0*") {
            $lines += "  Licence: Creative Commons Attribution 4.0 International, https://creativecommons.org/licenses/by/4.0/"
            # Every CC BY model says what changed (nothing); the speaker model's
            # credit below says so with the rest of its attribution.
            if (-not $speaker) {
                $lines += "  Changes: none. The files are unmodified, byte for byte the files at the revision above,"
                $lines += "  each checked by its SHA-256."
            }
        }
        if ($speaker) { $lines += Get-SpeakerCredit }
        $lines += ""
    }
    return ($lines -join "`n")
}

function Get-ReadmeText([string]$Version, [string]$Sha) {
    return @"
Unminute model package, for Unminute $Version installed for everyone (install.ps1 -Machine)

Intune (Win32 app; wrap this folder with the Microsoft Win32 Content Prep Tool):
  Install command:   %SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File Install-Models.ps1
  Uninstall command: %SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File Install-Models.ps1 -Uninstall
  Install behaviour: System
  Detection rule:    File. Path %ProgramFiles%\Unminute\models, file package-$Sha.json, exists;
                     associated with a 32-bit app on 64-bit clients: No
  Dependency:       the Unminute app (install it first)
  Return codes:      0 success; 3 retry (a model folder was in use); 1, 2 and 4 failed

Then set the policy AllowUserModels to 0 if staff should use only these models.
"@
}

function Invoke-NewModelPackage {
    foreach ($name in "ReleaseZip", "Source", "OutputFolder") {
        if ([string]::IsNullOrWhiteSpace($Options[$name])) { Stop-Package "-$name is needed." }
    }
    if ($Options.ExpectedSha256 -cnotmatch '^[0-9A-Fa-f]{64}\z') { Stop-Package "-ExpectedSha256 must be the 64-character SHA-256 Get-VerifiedRelease.ps1 printed." }
    $zip = [IO.Path]::GetFullPath($Options.ReleaseZip)
    $from = [IO.Path]::GetFullPath($Options.Source)
    $out = [IO.Path]::GetFullPath($Options.OutputFolder)
    if ([IO.File]::Exists($out) -or ([IO.Directory]::Exists($out) -and
            [IO.DirectoryInfo]::new($out).GetFileSystemInfos().Count)) {
        Stop-Package "-OutputFolder is not empty. Give a new or empty folder."
    }
    $scripts = Get-ScriptsFolder
    if (-not [IO.File]::Exists([IO.Path]::Combine($scripts, "Install-Models.ps1"))) {
        Stop-Package "Install-Models.ps1 was not found in packaging\models. Run this script from a copy of the repository at the release's tag."
    }
    $release = Read-Release $zip $Options.ExpectedSha256
    # Install-Models.ps1 comes from this copy of the repository, which should be the
    # release's tag (a warning only: the package's checks do not depend on it).
    $repository = Get-RepositoryVersion
    if ($repository -cne [string]$release.Version) {
        Write-Host "WARNING: this copy of the repository is version '$repository', not the release's $($release.Version). Install-Models.ps1 is taken from it; use a copy at the release's tag."
    }
    $chosen = Select-Model $release.Models $from @($Options.Model)
    [void][IO.Directory]::CreateDirectory($out)
    # Whatever stops the run from here (a refusal, an error), what it wrote goes: the
    # folder was new or empty. An exit runs this finally too.
    $done = $false
    try {
        foreach ($entry in $chosen) {
            Write-Host "Copying and checking $($entry.name)..."
            Copy-Model $entry $from $out
        }
        $manifest = [ordered]@{ format = 1; release = $release.Version; release_zip_sha256 = $release.Sha256; models = @($chosen) }
        Write-Text ([IO.Path]::Combine($out, "models.json")) ($manifest | ConvertTo-Json -Depth 8)
        $sha = Get-FileSha256 ([IO.Path]::Combine($out, "models.json"))
        [IO.File]::Copy([IO.Path]::Combine($scripts, "Install-Models.ps1"), [IO.Path]::Combine($out, "Install-Models.ps1"), $false)
        Write-Text ([IO.Path]::Combine($out, "LICENCES.txt")) (Get-LicenceText $release.Version $chosen)
        Write-Text ([IO.Path]::Combine($out, "README.txt")) (Get-ReadmeText $release.Version $sha)
        $done = $true
    } finally {
        if (-not $done) { try { Remove-PackageOutput $out } catch { } }
    }
    Write-Host "PACKAGE READY: $(@($chosen).Count) model(s) for Unminute $($release.Version). Detection file: package-$sha.json"
    Write-Host $sha
}

# Dot-sourced (the Pester tests in tests/powershell): the functions above are defined
# and nothing runs.
if ($MyInvocation.InvocationName -eq ".") { return }

try {
    Invoke-NewModelPackage
    exit 0
} catch {
    Stop-Package "$($_.Exception.Message)"
}
