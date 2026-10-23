param(
    [string]$PythonLauncher = "py",
    [string]$PythonVersion = "3.12"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$PackageRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PackageRoot "..\.."))
$VenvPython = Join-Path $PackageRoot "venv\Scripts\python.exe"
$LockFile = Join-Path $PackageRoot "requirements.lock"
$LocalRoot = Join-Path $RepoRoot ".local"
New-Item -ItemType Directory -Force -Path $LocalRoot | Out-Null
$ScratchRoot = [IO.Path]::GetFullPath((Join-Path $LocalRoot ("aoitalk-plugin-setup-" + [guid]::NewGuid().ToString("N"))))
$ScratchPrefix = $LocalRoot.TrimEnd([char]92, [char]47) + [IO.Path]::DirectorySeparatorChar
if (-not $ScratchRoot.StartsWith($ScratchPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Setup scratch path escaped the repository .local directory."
}
New-Item -ItemType Directory -Force -Path $ScratchRoot | Out-Null
$OldTemp = $env:TEMP; $OldTmp = $env:TMP; $OldTmpDir = $env:TMPDIR
$OldPluginLogRoot = $env:AOITALK_PLUGIN_LOG_ROOT
$OldPluginRoot = $env:AOITALK_PLUGIN_ROOT; $OldRepositoryRoot = $env:AOITALK_REPOSITORY_ROOT
$env:TEMP = $ScratchRoot; $env:TMP = $ScratchRoot; $env:TMPDIR = $ScratchRoot
$env:AOITALK_PLUGIN_LOG_ROOT = Join-Path $ScratchRoot "logs"
$env:AOITALK_PLUGIN_ROOT = $PackageRoot; $env:AOITALK_REPOSITORY_ROOT = $RepoRoot

try {
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        $launcher = Get-Command $PythonLauncher -ErrorAction SilentlyContinue
        if (-not $launcher) { throw "Python launcher '$PythonLauncher' was not found." }
        & $launcher.Source ("-" + $PythonVersion) -m venv (Join-Path $PackageRoot "venv")
        if ($LASTEXITCODE -ne 0) { throw "Could not create the plugin-only venv with Python $PythonVersion." }
    }

    if (-not (Test-Path -LiteralPath $LockFile -PathType Leaf)) {
        throw "Dedicated dependency lock is missing: $LockFile"
    }

    & $VenvPython -c "import sys; assert sys.version_info >= (3, 12), sys.version"
    if ($LASTEXITCODE -ne 0) { throw "The existing plugin venv must use Python 3.12 or newer; it was left untouched." }
    & $VenvPython -m pip install --disable-pip-version-check --no-cache-dir -r $LockFile
    if ($LASTEXITCODE -ne 0) { throw "Could not install the AoiTalk Plugin runtime lock." }
    & $VenvPython -m pip install --disable-pip-version-check --no-cache-dir --no-deps $PackageRoot
    if ($LASTEXITCODE -ne 0) { throw "Could not install the standalone aoitalk_plugin package." }
    & $VenvPython (Join-Path $PackageRoot "scripts\smoke.py")
    if ($LASTEXITCODE -ne 0) { throw "The standalone MCP initialize/list_tools smoke failed." }

    Write-Host "AoiTalk Plugin setup is ready. No tunnel runtime or Windows Task was started or registered."
} finally {
    $env:TEMP = $OldTemp; $env:TMP = $OldTmp; $env:TMPDIR = $OldTmpDir
    if ($null -eq $OldPluginLogRoot) { Remove-Item Env:AOITALK_PLUGIN_LOG_ROOT -ErrorAction SilentlyContinue }
    else { $env:AOITALK_PLUGIN_LOG_ROOT = $OldPluginLogRoot }
    if ($null -eq $OldPluginRoot) { Remove-Item Env:AOITALK_PLUGIN_ROOT -ErrorAction SilentlyContinue }
    else { $env:AOITALK_PLUGIN_ROOT = $OldPluginRoot }
    if ($null -eq $OldRepositoryRoot) { Remove-Item Env:AOITALK_REPOSITORY_ROOT -ErrorAction SilentlyContinue }
    else { $env:AOITALK_REPOSITORY_ROOT = $OldRepositoryRoot }
    $resolvedScratch = [IO.Path]::GetFullPath($ScratchRoot)
    if ($resolvedScratch.StartsWith($ScratchPrefix, [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolvedScratch -PathType Container)) {
        Remove-Item -LiteralPath $resolvedScratch -Recurse -Force
    }
}
