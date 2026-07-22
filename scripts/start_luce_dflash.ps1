param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8080,
    [string]$RepoRoot = $env:LUCE_DFLASH_ROOT,
    [string]$TargetModel = $env:LUCE_DFLASH_TARGET_MODEL,
    [string]$DraftModel = $env:LUCE_DFLASH_DRAFT_MODEL,
    [string]$PythonExe = "",
    [string]$CudaRoot = $env:CUDA_PATH,
    [switch]$Stop,
    [switch]$Restart,
    [switch]$Foreground,
    [switch]$Hidden
)

$ErrorActionPreference = "Stop"

function Load-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Key
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match ("^" + [regex]::Escape($Key) + "=(.*)$")) {
            return $Matches[1].Trim()
        }
    }
    return $null
}

function Assert-Path {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Label not found: $Path"
    }
}

function Stop-DFlash {
    param([int]$Port)

    $listeners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($ownerPid in $listeners) {
        Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
    }

    Get-Process -Name "test_dflash" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue

    Start-Sleep -Seconds 2
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$driveRoot = [System.IO.Path]::GetPathRoot($projectRoot)
$defaultAiRoot = Join-Path $driveRoot "AI"
$defaultDevRoot = Join-Path $driveRoot "Dev"
$defaultHotLlmRoot = Join-Path $defaultAiRoot "models\Hot\llm"
$repoEnv = Join-Path $projectRoot ".env"
$hfHome = Load-DotEnvValue -Path $repoEnv -Key "HF_HOME"
$hfCache = Load-DotEnvValue -Path $repoEnv -Key "HF_HUB_CACHE"
$hfToken = Load-DotEnvValue -Path $repoEnv -Key "HUGGINGFACE_API_KEY"

if (-not $hfHome) { $hfHome = Join-Path $env:USERPROFILE ".cache\huggingface" }
if (-not $hfCache) { $hfCache = Join-Path $hfHome "hub" }

if ($Stop) {
    Stop-DFlash -Port $Port
    Write-Output "Luce DFlash server stopped on port $Port"
    exit 0
}

if (-not $RepoRoot) { $RepoRoot = Join-Path $defaultDevRoot "67_lucebox-hub\dflash" }
if (-not $TargetModel) {
    $TargetModel = Join-Path $defaultHotLlmRoot "luce-dflash\models\Qwen3.6-27B-Q4_K_M.gguf"
}
if (-not $DraftModel) {
    $DraftModel = Join-Path $defaultHotLlmRoot "luce-dflash\models\draft\dflash-draft-3.6-q8_0.gguf"
}
if (-not $PythonExe) { $PythonExe = Join-Path $projectRoot "venv\Scripts\python.exe" }
if (-not $CudaRoot) { throw "Pass -CudaRoot or set CUDA_PATH." }

$serverScript = Join-Path $RepoRoot "scripts\server.py"
$dflashExe = Join-Path $RepoRoot "build\test_dflash.exe"

Assert-Path -Path $PythonExe -Label "Python"
Assert-Path -Path $serverScript -Label "Luce DFlash server.py"
Assert-Path -Path $dflashExe -Label "test_dflash.exe"
Assert-Path -Path $TargetModel -Label "Target GGUF"
Assert-Path -Path $DraftModel -Label "Draft model path"
Assert-Path -Path (Join-Path $CudaRoot "bin\x64\cublas64_13.dll") -Label "CUDA 13.2 cublas DLL"

New-Item -ItemType Directory -Force -Path $hfHome, $hfCache | Out-Null

if ($Restart) {
    Stop-DFlash -Port $Port
}

$env:PYTHONIOENCODING = "utf-8"
$env:HF_HOME = $hfHome
$env:HF_HUB_CACHE = $hfCache
if ($hfToken) { $env:HF_TOKEN = $hfToken }
$env:CUDA_PATH = $CudaRoot
$env:PATH = @(
    (Join-Path $CudaRoot "bin\x64"),
    (Join-Path $CudaRoot "bin"),
    (Join-Path $RepoRoot "build"),
    $env:PATH
) -join ";"

$args = @(
    $serverScript,
    "--host", $HostAddress,
    "--port", [string]$Port,
    "--target", $TargetModel,
    "--draft", $DraftModel,
    "--bin", $dflashExe,
    "--cache-type-k", "q4_0",
    "--cache-type-v", "q8_0",
    "--prefix-cache-slots", "0",
    "--prefill-cache-slots", "0"
)

if ($Foreground -or -not $Hidden) {
    & $PythonExe @args
    exit $LASTEXITCODE
}

$stdout = Join-Path $RepoRoot "server.out.log"
$stderr = Join-Path $RepoRoot "server.err.log"
Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue

$process = Start-Process `
    -FilePath $PythonExe `
    -ArgumentList $args `
    -WorkingDirectory $RepoRoot `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 3
Write-Output "Luce DFlash server pid=$($process.Id) url=http://$HostAddress`:$Port/v1"
Write-Output "logs: $stdout / $stderr"
