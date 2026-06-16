param(
    [string]$SourceEnvPath = "",
    [string]$OutputRoot = "D:\Publish\AoiTalk_Enterprise",
    [string]$DatabaseName = "aoitalk_demo",
    [string]$DatabaseUser = "aoitalk_demo",
    [string]$DemoAssetsRoot = "D:\Publish\AoiTalk_Demo_Assets",
    [string]$DemoProjectId = "e28e143b-73d6-49cf-bdce-ea2064c3940e",
    [switch]$SkipDemoAssets,
    [switch]$ConfigureOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function New-RandomSecret {
    param([int]$ByteCount = 32)

    $bytes = New-Object byte[] $ByteCount
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    return ([System.BitConverter]::ToString($bytes) -replace "-", "").ToLowerInvariant()
}

function Read-DotEnv {
    param([string]$Path)

    $values = @{}
    foreach ($line in [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)) {
        if ($line -match '^\s*#') { continue }
        if ($line -match '^\s*([A-Za-z0-9_]+)\s*=\s*(.*)$') {
            $values[$Matches[1]] = $Matches[2].Trim()
        }
    }
    return $values
}

function Set-DotEnvValue {
    param(
        [string]$Content,
        [string]$Key,
        [string]$Value
    )

    if ($Value -match '[\r\n]') {
        throw "$Key must not contain a newline."
    }
    $pattern = "(?m)^" + [regex]::Escape($Key) + "=.*$"
    $replacement = $Key + "=" + $Value
    if ($Content -match $pattern) {
        return [regex]::Replace($Content, $pattern, { param($match) $replacement })
    }
    return $Content.TrimEnd("`r", "`n") + [Environment]::NewLine + $replacement + [Environment]::NewLine
}

function Assert-Identifier {
    param(
        [string]$Name,
        [string]$Value
    )

    if ($Value -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
        throw "$Name must be a valid PostgreSQL identifier."
    }
}

if ([string]::IsNullOrWhiteSpace($SourceEnvPath)) {
    $sourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $SourceEnvPath = Join-Path $sourceRoot ".env"
}

$sourceEnvFull = [System.IO.Path]::GetFullPath($SourceEnvPath)
$outputRootFull = [System.IO.Path]::GetFullPath($OutputRoot)
$destinationEnv = Join-Path $outputRootFull ".env"
$setupScript = Join-Path $outputRootFull "scripts\setup_env_db.ps1"

if (-not (Test-Path -LiteralPath $sourceEnvFull -PathType Leaf)) {
    throw "Source .env was not found: $sourceEnvFull"
}
if (-not (Test-Path -LiteralPath $outputRootFull -PathType Container)) {
    throw "Enterprise output directory was not found: $outputRootFull"
}
if (-not (Test-Path -LiteralPath $setupScript -PathType Leaf)) {
    throw "Enterprise database setup script was not found: $setupScript"
}

Assert-Identifier "DatabaseName" $DatabaseName
Assert-Identifier "DatabaseUser" $DatabaseUser
$parsedDemoProjectId = [Guid]::Empty
if (-not [Guid]::TryParse($DemoProjectId, [ref]$parsedDemoProjectId)) {
    throw "DemoProjectId must be a UUID."
}
$DemoProjectId = $parsedDemoProjectId.ToString()

$sourceValues = Read-DotEnv $sourceEnvFull
$content = [System.IO.File]::ReadAllText($sourceEnvFull, [System.Text.Encoding]::UTF8)
$databasePassword = New-RandomSecret 24

$hostName = if ($sourceValues.ContainsKey("POSTGRES_HOST")) { [string]$sourceValues["POSTGRES_HOST"] } else { "localhost" }
$port = if ($sourceValues.ContainsKey("POSTGRES_PORT")) { [string]$sourceValues["POSTGRES_PORT"] } else { "5432" }

$content = Set-DotEnvValue $content "AIVTUBER_ENV" "enterprise"
$content = Set-DotEnvValue $content "POSTGRES_HOST" $hostName
$content = Set-DotEnvValue $content "POSTGRES_PORT" $port
$content = Set-DotEnvValue $content "POSTGRES_DB" $DatabaseName
$content = Set-DotEnvValue $content "POSTGRES_USER" $DatabaseUser
$content = Set-DotEnvValue $content "POSTGRES_PASSWORD" $databasePassword
$content = Set-DotEnvValue $content "USE_POSTGRESQL" "true"
$content = Set-DotEnvValue $content "DATABASE_URL" "postgres://${DatabaseUser}:${databasePassword}@${hostName}:${port}/${DatabaseName}"

foreach ($key in @("NEXTAUTH_SECRET", "AOITALK_WEB_AUTH_SECRET", "AOITALK_JWT_SECRET", "INTERNAL_API_KEY")) {
    $content = Set-DotEnvValue $content $key (New-RandomSecret)
}

[System.IO.File]::WriteAllText(
    $destinationEnv,
    $content,
    [System.Text.UTF8Encoding]::new($false)
)
Write-Host "[env] Regenerated the Enterprise demo .env: $destinationEnv"
Write-Host "[env] DB=${DatabaseName} / USER=${DatabaseUser} / HOST=${hostName}:${port}"

if (-not $SkipDemoAssets) {
    $demoAssetsRootFull = [System.IO.Path]::GetFullPath($DemoAssetsRoot)
    if (Test-Path -LiteralPath $demoAssetsRootFull -PathType Container) {
        $sourceCandidates = Get-ChildItem -LiteralPath $demoAssetsRootFull -Directory -Recurse |
            Where-Object {
                @(Get-ChildItem -LiteralPath $_.FullName -Directory -ErrorAction SilentlyContinue).Count -ge 5
            } |
            Sort-Object { $_.FullName.Length } -Descending
        $demoSource = $sourceCandidates | Select-Object -First 1
        if ($null -eq $demoSource) {
            Write-Warning "Could not find the demo project file root under: $demoAssetsRootFull"
        } else {
            $projectFilesFolder = -join ([char[]](0x6848, 0x4EF6, 0x8CC7, 0x6599))
            $workspaceRoot = Join-Path $outputRootFull "workspaces"
            $projectRoot = Join-Path $workspaceRoot "_projects\project_$DemoProjectId"
            $targetRoot = Join-Path $projectRoot $projectFilesFolder
            $workspaceRootFull = [System.IO.Path]::GetFullPath($workspaceRoot)
            $targetRootFull = [System.IO.Path]::GetFullPath($targetRoot)
            if (-not $targetRootFull.StartsWith($workspaceRootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Demo project target escaped the Enterprise workspace root."
            }

            $copiedFiles = 0
            foreach ($file in Get-ChildItem -LiteralPath $demoSource.FullName -File -Recurse) {
                if ($file.Name -eq "Thumbs.db" -or $file.Name.StartsWith("~$")) {
                    continue
                }
                $relativePath = $file.FullName.Substring($demoSource.FullName.Length).TrimStart("\", "/")
                $destination = Join-Path $targetRootFull $relativePath
                $destinationParent = Split-Path -Parent $destination
                if (-not (Test-Path -LiteralPath $destinationParent -PathType Container)) {
                    New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
                }
                Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
                $copiedFiles += 1
            }
            Write-Host "[files] Restored $copiedFiles demo project files: $targetRootFull"
        }
    } else {
        Write-Warning "Demo assets directory was not found; workspace restore was skipped: $demoAssetsRootFull"
    }
}

if ($ConfigureOnly) {
    Write-Host "[env] ConfigureOnly: skipped PostgreSQL role password synchronization."
    exit 0
}

$previousPgPassword = $env:PGPASSWORD
$adminPasswordFromSource = $sourceValues.ContainsKey("POSTGRES_PASSWORD") `
    -and -not [string]::IsNullOrWhiteSpace([string]$sourceValues["POSTGRES_PASSWORD"])
if ([string]::IsNullOrWhiteSpace($env:PGPASSWORD) -and $adminPasswordFromSource) {
    $env:PGPASSWORD = [string]$sourceValues["POSTGRES_PASSWORD"]
}
if ([string]::IsNullOrWhiteSpace($env:PGPASSWORD)) {
    throw "PostgreSQL administrator password is required. Set PGPASSWORD and retry."
}

try {
    & $setupScript -NonInteractive -EnvFile $destinationEnv
    Write-Host "[db] Synchronized the Enterprise demo database credentials."
} catch {
    throw "Could not synchronize demo database credentials. Set PGPASSWORD to the postgres administrator password and retry. Detail: $($_.Exception.Message)"
} finally {
    $env:PGPASSWORD = $previousPgPassword
    $databasePassword = $null
}
