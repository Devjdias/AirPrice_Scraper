$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
$distRoot = Join-Path $projectRoot "dist"
$stageRoot = Join-Path $distRoot "package"
$packageRoot = Join-Path $stageRoot "AirPrice"
$zipPath = Join-Path $distRoot "airprice-software.zip"

function Assert-UnderRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $fullRoot = [System.IO.Path]::GetFullPath($Root)
    if (-not $fullPath.StartsWith($fullRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe path outside project root: $fullPath"
    }
}

$requiredFiles = @(
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "config/config.example.ini",
    "config/requirements.txt",
    "docs/DOCUMENTACAO_TECNICA.md",
    "src/coleta_tcc.py",
    "src/latam_scraper.py",
    "src/scraper_passagens.py",
    "src/scrapers_utils.py",
    "scripts/build_submission_zip.ps1"
)

$dataDir = Join-Path $projectRoot "data"
if (Test-Path $dataDir) {
    Get-ChildItem -Path $dataDir -File |
        Where-Object { $_.Extension.ToLowerInvariant() -in @(".jpeg", ".jpg", ".png", ".pdf", ".xlsx") } |
        ForEach-Object {
            $relative = $_.FullName.Substring($projectRoot.Length).TrimStart([char[]]@("\", "/"))
            $requiredFiles += $relative.Replace("\", "/")
        }
}

New-Item -ItemType Directory -Force -Path $distRoot | Out-Null

if (Test-Path $stageRoot) {
    Assert-UnderRoot -Path $stageRoot -Root $projectRoot
    Remove-Item -LiteralPath $stageRoot -Recurse -Force
}

if (Test-Path $zipPath) {
    Assert-UnderRoot -Path $zipPath -Root $projectRoot
    Remove-Item -LiteralPath $zipPath -Force
}

New-Item -ItemType Directory -Force -Path $packageRoot | Out-Null

$copiedFiles = @()
foreach ($relativePath in $requiredFiles) {
    $source = Join-Path $projectRoot $relativePath
    if (-not (Test-Path $source)) {
        throw "Required file not found: $relativePath"
    }

    $destination = Join-Path $packageRoot $relativePath
    $destinationDir = Split-Path -Parent $destination
    New-Item -ItemType Directory -Force -Path $destinationDir | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
    $copiedFiles += $relativePath
}

$manifest = @(
    "AirPrice Software Submission Package",
    "",
    "This ZIP is generated from a whitelist of source code, documentation,",
    "installation instructions, license text, and public configuration examples.",
    "",
    "Excluded by design:",
    "- config/config.ini",
    "- credentials/",
    "- DADOS_BRUTOS/",
    "- LOGS/",
    "- __pycache__/",
    "- virtual environments",
    "- generated spreadsheets, screenshots, HTML dumps, and local secrets",
    "",
    "Included files:"
) + ($copiedFiles | Sort-Object | ForEach-Object { "- $_" })

Set-Content -Path (Join-Path $packageRoot "PACKAGE_CONTENTS.txt") -Value $manifest -Encoding UTF8

Compress-Archive -Path $packageRoot -DestinationPath $zipPath -Force

Assert-UnderRoot -Path $stageRoot -Root $projectRoot
Remove-Item -LiteralPath $stageRoot -Recurse -Force

Write-Host "Created: $zipPath"
