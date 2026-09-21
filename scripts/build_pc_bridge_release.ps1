param(
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [string]$Name = "AoiTalk-PC-Bridge",
    [string]$PythonExe = "",
    [string[]]$PythonPrefixArguments = @(),
    [string]$TempRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "Portable PC Bridge release builds require Windows."
}
if ($Name -notmatch '^[A-Za-z][A-Za-z0-9_.-]{0,95}$') {
    throw "Name contains unsupported characters: $Name"
}
$sourceFull = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $SourceRoot).Path)
$outputFull = [IO.Path]::GetFullPath($OutputPath)
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    foreach ($candidate in @(Get-Command python.exe -CommandType Application -All -ErrorAction SilentlyContinue)) {
        $candidateFull = [IO.Path]::GetFullPath($candidate.Source)
        if (-not (Test-Path -LiteralPath $candidateFull -PathType Leaf)) { continue }
        $item = Get-Item -LiteralPath $candidateFull -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
        & $candidateFull -X utf8 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" *> $null
        if ($LASTEXITCODE -eq 0) { $PythonExe = $candidateFull; break }
    }
}
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    throw "Python 3.12+ was not found for the PC Bridge release build."
}
$pythonFull = [IO.Path]::GetFullPath($PythonExe)
if (-not (Test-Path -LiteralPath $pythonFull -PathType Leaf)) { throw "Python executable is missing: $pythonFull" }
& $pythonFull @PythonPrefixArguments -X utf8 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" *> $null
if ($LASTEXITCODE -ne 0) { throw "Python 3.12+ is required for the PC Bridge release build." }
foreach ($required in @(
    "scripts/build_pc_bridge.py",
    "scripts/pc_bridge_main.py",
    "pc_bridge/requirements.txt",
    "resources/edge-browser-extension/manifest.json"
)) {
    if (-not (Test-Path -LiteralPath (Join-Path $sourceFull $required) -PathType Leaf)) {
        throw "PC Bridge release input is missing: $required"
    }
}
if ([string]::IsNullOrWhiteSpace($TempRoot)) {
    $TempRoot = [IO.Path]::GetTempPath()
}
$tempParent = [IO.Path]::GetFullPath($TempRoot)
New-Item -ItemType Directory -Force -Path $tempParent | Out-Null
$buildRoot = Join-Path $tempParent ("pc-bridge-release-" + [guid]::NewGuid().ToString("N"))
$venv = Join-Path $buildRoot "venv"
$dist = Join-Path $buildRoot "dist"
$work = Join-Path $buildRoot "work"
try {
    New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
    & $pythonFull @PythonPrefixArguments -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the isolated PC Bridge build environment." }
    $buildPython = Join-Path $venv "Scripts/python.exe"
    & $buildPython -m pip install --disable-pip-version-check --no-input -r (Join-Path $sourceFull "pc_bridge/requirements.txt") "pyinstaller==6.22.3"
    if ($LASTEXITCODE -ne 0) { throw "Could not install pinned PC Bridge build dependencies." }
    & $buildPython (Join-Path $sourceFull "scripts/build_pc_bridge.py") --name $Name --dist-root $dist --work-root $work --no-install
    if ($LASTEXITCODE -ne 0) { throw "PC Bridge executable build/self-test failed." }
    $built = Join-Path $dist "$Name.exe"
    if (-not (Test-Path -LiteralPath $built -PathType Leaf) -or (Get-Item -LiteralPath $built).Length -lt 1MB) {
        throw "PC Bridge executable was not produced correctly: $built"
    }
    $outputParent = Split-Path -Parent $outputFull
    if ($outputParent) { New-Item -ItemType Directory -Force -Path $outputParent | Out-Null }
    Copy-Item -LiteralPath $built -Destination $outputFull -Force
    $hash = (Get-FileHash -LiteralPath $outputFull -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "[OK] PC Bridge release executable: $outputFull"
    Write-Host "[OK] PC Bridge SHA-256: $hash"
} finally {
    if (Test-Path -LiteralPath $buildRoot) {
        Remove-Item -LiteralPath $buildRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
