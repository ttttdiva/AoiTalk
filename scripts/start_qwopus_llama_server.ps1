param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8080,
    [string]$ModelPath = $env:QWOPUS_MODEL_PATH,
    [string]$ModelAlias = "qwopus3.6-35b-a3b",
    [string]$LlamaServerExe = $env:LLAMA_SERVER_EXE,
    [string]$LogRoot = "",
    [switch]$Stop,
    [switch]$Restart,
    [switch]$Foreground,
    [switch]$Hidden
)

$ErrorActionPreference = "Stop"

function Resolve-Executable {
    param([Parameter(Mandatory = $true)][string]$NameOrPath)

    if (Test-Path -LiteralPath $NameOrPath) {
        return (Resolve-Path -LiteralPath $NameOrPath).Path
    }

    $command = Get-Command $NameOrPath -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $wingetPackages = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    $wingetCandidates = @()
    if (Test-Path -LiteralPath $wingetPackages) {
        $wingetCandidates = Get-ChildItem -LiteralPath $wingetPackages -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty FullName
    }

    foreach ($candidate in $wingetCandidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    throw "llama-server executable not found. Pass -LlamaServerExe or add llama-server.exe to PATH."
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

function Stop-Qwopus {
    param([int]$Port)

    $listeners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($ownerPid in $listeners) {
        Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
    }

    Start-Sleep -Seconds 2
}

if ($Stop) {
    Stop-Qwopus -Port $Port
    Write-Output "Qwopus llama-server stopped on port $Port"
    exit 0
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$defaultAiRoot = Join-Path ([System.IO.Path]::GetPathRoot($projectRoot)) "AI"

if (-not $ModelPath) {
    $ModelPath = Join-Path $defaultAiRoot "models\qwopus\models\Qwopus3.6-35B-A3B-v1-Q4_K_M.gguf"
}
if (-not $LlamaServerExe) { $LlamaServerExe = "llama-server" }
if (-not $LogRoot) { $LogRoot = Join-Path $env:LOCALAPPDATA "AoiTalk\logs\qwopus" }

Assert-Path -Path $ModelPath -Label "Qwopus GGUF"
$serverExe = Resolve-Executable -NameOrPath $LlamaServerExe

if ($Restart) {
    Stop-Qwopus -Port $Port
}

$env:PYTHONIOENCODING = "utf-8"

$args = @(
    "--host", $HostAddress,
    "--port", [string]$Port,
    "--model", $ModelPath,
    "--alias", $ModelAlias,
    "--n-gpu-layers", "999"
)

if ($Foreground -or -not $Hidden) {
    & $serverExe @args
    exit $LASTEXITCODE
}

New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
$stdout = Join-Path $LogRoot "server.out.log"
$stderr = Join-Path $LogRoot "server.err.log"
Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue

$process = Start-Process `
    -FilePath $serverExe `
    -ArgumentList $args `
    -WorkingDirectory $LogRoot `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 3
Write-Output "Qwopus llama-server pid=$($process.Id) url=http://$HostAddress`:$Port/v1 model=$ModelAlias"
Write-Output "logs: $stdout / $stderr"
