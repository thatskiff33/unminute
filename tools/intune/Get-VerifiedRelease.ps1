<#
.SYNOPSIS
    Downloads an Unminute release and verifies it was built by the project's
    Release workflow from its tag, before it is packaged for Intune (#111 B3).

.DESCRIPTION
    Downloads the release's zip, its .sha256 file, the SBOM and the attestation
    bundle over HTTPS from the published release, then checks, in order:
      - the release is published (not a draft), its tag is exactly -Tag, and
        unless -AllowPrerelease is given it is not a pre-release;
      - the zip matches its .sha256 file;
      - the zip's build provenance and SBOM attestations verify with the GitHub
        CLI against the downloaded bundle: signed by exactly this repository's
        .github/workflows/release.yml at refs/tags/<Tag>, from the commit the
        tag points at, on a GitHub-hosted runner.
    Needs the GitHub CLI (gh) on PATH. No GitHub sign-in is needed: gh checks
    the bundle against Sigstore's public trust root, which it fetches itself.
    Prints VERIFIED and the zip's SHA-256 and exits 0. Any failure prints NOT
    VERIFIED and exits 1; the files it downloaded must not be used. The .sha256
    file alone detects a damaged download, not a replaced one: always run this
    script before packaging.

.PARAMETER Tag
    The release: vX.Y.Z, or vX.Y.Z-preview.N with -AllowPrerelease. Case matters.

.PARAMETER Repository
    owner/name on GitHub. The default is the project's repository. It changes
    when the repository moves, because the signer identity names it.

.PARAMETER OutputFolder
    A new or empty folder for the downloaded files. The default is
    .\Unminute-<version>-verified.

.PARAMETER AllowPrerelease
    Accept a pre-release (a vX.Y.Z-preview.N tag).

.EXAMPLE
    powershell -NoProfile -File tools\intune\Get-VerifiedRelease.ps1 -Tag v0.5.0
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ $_ -cmatch '^v[0-9]+\.[0-9]+\.[0-9]+(-preview\.[0-9]+)?$' })]
    [string]$Tag,

    [ValidatePattern('^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$')]
    [string]$Repository = 'thatskiff33/unminute',

    [string]$OutputFolder,

    [switch]$AllowPrerelease
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'      # Windows PowerShell downloads slowly with a progress bar
Set-StrictMode -Version Latest

function Stop-Verification([string]$Message) {
    Write-Host "NOT VERIFIED: $Message" -ForegroundColor Red
    exit 1
}

$name = "Unminute-$($Tag.Substring(1))"
if ($Tag -cmatch '-preview\.' -and -not $AllowPrerelease) {
    Stop-Verification "$Tag is a preview. Add -AllowPrerelease to accept it."
}
if (-not $OutputFolder) { $OutputFolder = Join-Path (Get-Location) "$name-verified" }
if ((Test-Path -LiteralPath $OutputFolder) -and
    @(Get-ChildItem -LiteralPath $OutputFolder -Force).Count -gt 0) {
    Stop-Verification "$OutputFolder is not empty. Give a new or empty folder."
}
New-Item -ItemType Directory -Force -Path $OutputFolder | Out-Null

$gh = Get-Command gh -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $gh) { Stop-Verification "the GitHub CLI (gh) is not installed or not on PATH." }

# 1. The published release, and the commit its tag points at. GitHub's public
#    API does not show drafts.
$headers = @{ Accept = 'application/vnd.github+json'; 'User-Agent' = 'Unminute-Get-VerifiedRelease' }
$api = "https://api.github.com/repos/$Repository"
try {
    $release = Invoke-RestMethod -Uri "$api/releases/tags/$Tag" -Headers $headers -UseBasicParsing
    $ref = Invoke-RestMethod -Uri "$api/git/ref/tags/$Tag" -Headers $headers -UseBasicParsing
    $commit = $ref.object.sha
    if ($ref.object.type -eq 'tag') {
        $commit = (Invoke-RestMethod -Uri "$api/git/tags/$commit" -Headers $headers -UseBasicParsing).object.sha
    }
} catch {
    Stop-Verification "there is no published release $Tag in $Repository ($($_.Exception.Message))."
}
if ($release.tag_name -cne $Tag) { Stop-Verification "the release's tag is $($release.tag_name), not $Tag." }
if ($release.draft) { Stop-Verification "$Tag is still a draft." }
if ($release.prerelease -and -not $AllowPrerelease) {
    Stop-Verification "$Tag is a pre-release. Add -AllowPrerelease to accept it."
}
if ($commit -cnotmatch '^[0-9a-f]{40}$') { Stop-Verification "could not read the commit of $Tag." }

# 2. The release's four files, by their exact names.
$files = "$name.zip", "$name.zip.sha256", "$name.cdx.json", "$name.attestations.jsonl"
foreach ($file in $files) {
    $asset = @($release.assets | Where-Object { $_.name -ceq $file })
    if ($asset.Count -ne 1) { Stop-Verification "the release has no $file." }
    try {
        Invoke-WebRequest -Uri $asset[0].browser_download_url -OutFile (Join-Path $OutputFolder $file) `
            -UseBasicParsing
    } catch {
        Stop-Verification "$file could not be downloaded ($($_.Exception.Message))."
    }
}
$zip = Join-Path $OutputFolder "$name.zip"
$bundle = Join-Path $OutputFolder "$name.attestations.jsonl"

# 3. The zip against its .sha256 file.
$sha = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
$listed = (Get-Content -LiteralPath (Join-Path $OutputFolder "$name.zip.sha256") -Raw).Trim()
if ($listed -cne "$sha  $name.zip") { Stop-Verification "$name.zip does not match its .sha256 file." }

# 4. Build provenance, then the SBOM: each signed by exactly this repository's
#    Release workflow at this tag, from the tag's commit, on a GitHub-hosted runner.
$identity = "https://github.com/$Repository/.github/workflows/release.yml@refs/tags/$Tag"
foreach ($predicate in 'https://slsa.dev/provenance/v1', 'https://cyclonedx.org/bom') {
    & $gh.Source attestation verify $zip --repo $Repository --bundle $bundle `
        --predicate-type $predicate --cert-identity $identity --signer-digest $commit `
        --source-ref "refs/tags/$Tag" --source-digest $commit --deny-self-hosted-runners
    if ($LASTEXITCODE -ne 0) {
        Stop-Verification ("the $predicate attestation of $name.zip did not verify. If gh says " +
            "its verifier was not available, check the network and try again.")
    }
}

$immutable = if ($release.PSObject.Properties['immutable']) { $release.immutable } else { 'unknown' }
Write-Host "VERIFIED: $name.zip" -ForegroundColor Green
Write-Host "  SHA-256 $sha"
Write-Host "  Built by $identity from commit $commit, on a GitHub-hosted runner."
Write-Host "  Build provenance and SBOM attestations verified (the SBOM is inside the attestation;"
Write-Host "  $name.cdx.json beside the zip is a copy for reading)."
Write-Host "  Release immutable: $immutable"
Write-Host "  Files: $OutputFolder"
exit 0
