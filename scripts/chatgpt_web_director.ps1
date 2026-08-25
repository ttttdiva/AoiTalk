$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "chatgpt_web_director.py"
$root = if ($env:AOITALK_ROOT) { $env:AOITALK_ROOT } else { Split-Path $PSScriptRoot -Parent }
$python = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = Join-Path $root ".venv\Scripts\python.exe"
}
if (-not (Test-Path $python)) {
    $python = "python"
}
& $python $script @args
exit $LASTEXITCODE
