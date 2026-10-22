param(
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [string]$Name = "AoiTalk-PC-Bridge",
    [string]$PythonExe = "",
    [string[]]$PythonPrefixArguments = @(),
    [string]$TempRoot = "",
    [string]$AgentBrowserBundle = "",
    [string]$DistributionNotices = "",
    [string]$WindowsRuntimeRedist = "",
    [string]$EnterpriseLegacyBrand = "",
    [switch]$AuditOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-PortableFileSha256([string]$Path) {
    $stream = $null
    $algorithm = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $algorithm = [Security.Cryptography.SHA256]::Create()
        $digest = $algorithm.ComputeHash($stream)
        return ([BitConverter]::ToString($digest).Replace("-", "")).ToLowerInvariant()
    } finally {
        if ($null -ne $algorithm) { $algorithm.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

if ($env:OS -ne "Windows_NT") {
    throw "Portable PC Bridge release builds require Windows."
}
if ($Name -notmatch '^[A-Za-z][A-Za-z0-9_.-]{0,95}$') {
    throw "Name contains unsupported characters: $Name"
}
$sourceFull = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $SourceRoot).Path)
$outputFull = [IO.Path]::GetFullPath($OutputPath)
$noticesArguments = @()
if (-not [string]::IsNullOrWhiteSpace($EnterpriseLegacyBrand)) {
    if (-not [string]::IsNullOrWhiteSpace($DistributionNotices) -or $AuditOnly) {
        throw "Enterprise builds do not accept additional distribution notice bundles or audit mode."
    }
} else {
    if ([string]::IsNullOrWhiteSpace($DistributionNotices)) {
        throw "Distribution builds require -DistributionNotices with a reviewed notice bundle."
    }
    $noticesFull = [IO.Path]::GetFullPath($DistributionNotices)
    if (-not (Test-Path -LiteralPath (Join-Path $noticesFull "manifest.json") -PathType Leaf)) {
        throw "Reviewed PC Bridge distribution notices are missing: $noticesFull"
    }
    $noticesArguments = @("--distribution-notices", $noticesFull)
}
$runtimeArguments = @()
if (-not [string]::IsNullOrWhiteSpace($WindowsRuntimeRedist)) {
    $runtimeArguments = @("--windows-runtime-redist", [IO.Path]::GetFullPath($WindowsRuntimeRedist))
}
$bundleArguments = @()
if ([string]::IsNullOrWhiteSpace($AgentBrowserBundle)) {
    throw "PC Bridge distribution candidates require -AgentBrowserBundle."
}
$distributionArguments = @("--require-distribution-notices")
if (-not [string]::IsNullOrWhiteSpace($EnterpriseLegacyBrand)) {
    $distributionArguments = @("--enterprise-legacy-brand", $EnterpriseLegacyBrand)
}
if ($AuditOnly) { $distributionArguments = @("--audit-distribution") }
if (-not [string]::IsNullOrWhiteSpace($AgentBrowserBundle)) {
    # GetFullPath preserves links for the Python bundle verifier to reject.
    $bundleFull = [IO.Path]::GetFullPath($AgentBrowserBundle)
    if (-not (Test-Path -LiteralPath $bundleFull -PathType Container)) {
        throw "Agent-browser build bundle is missing: $bundleFull"
    }
    $bundleArguments = @("--agent-browser-bundle", $bundleFull)
}
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
$previousDontWriteBytecode = [Environment]::GetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", "Process")
try {
    $env:PYTHONDONTWRITEBYTECODE = "1"
    New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
    & $pythonFull @PythonPrefixArguments -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the isolated PC Bridge build environment." }
    $buildPython = Join-Path $venv "Scripts/python.exe"
    & $buildPython -m pip install --disable-pip-version-check --no-input -r (Join-Path $sourceFull "pc_bridge/requirements.txt") "pyinstaller==6.22.3"
    if ($LASTEXITCODE -ne 0) { throw "Could not install pinned PC Bridge build dependencies." }
    & $buildPython -B (Join-Path $sourceFull "scripts/build_pc_bridge.py") --name $Name --dist-root $dist --work-root $work --no-install @noticesArguments @distributionArguments @bundleArguments @runtimeArguments
    if ($LASTEXITCODE -ne 0) { throw "PC Bridge executable build/self-test failed." }
    $built = Join-Path $dist "$Name.exe"
    if (-not (Test-Path -LiteralPath $built -PathType Leaf) -or (Get-Item -LiteralPath $built).Length -lt 1MB) {
        throw "PC Bridge executable was not produced correctly: $built"
    }
    $outputParent = Split-Path -Parent $outputFull
    if ($outputParent) { New-Item -ItemType Directory -Force -Path $outputParent | Out-Null }
    Copy-Item -LiteralPath $built -Destination $outputFull -Force
    if ($AuditOnly) {
        Copy-Item -LiteralPath (Join-Path $dist "$Name.distribution-audit.json") -Destination "$outputFull.distribution-audit.json" -Force
        Write-Host "[AUDIT ONLY] Component inventory requires review; this is not a cleared distribution build."
    }
    $hash = Get-PortableFileSha256 -Path $outputFull
    Write-Host "[OK] PC Bridge release executable: $outputFull"
    Write-Host "[OK] PC Bridge SHA-256: $hash"
} finally {
    if (Test-Path -LiteralPath $buildRoot) {
        Remove-Item -LiteralPath $buildRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($null -eq $previousDontWriteBytecode) {
        Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONDONTWRITEBYTECODE = $previousDontWriteBytecode
    }
}
