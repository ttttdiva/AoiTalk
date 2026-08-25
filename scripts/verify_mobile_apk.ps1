param(
    [Parameter(Mandatory = $true)]
    [string]$ApkPath,
    [string]$ExpectedVersion = "",
    [string]$ExpectedVersionCode = ""
)

$ErrorActionPreference = "Stop"

$resolvedApk = (Resolve-Path -LiteralPath $ApkPath).Path
$sdkRoots = @(
    $env:ANDROID_SDK_ROOT,
    $env:ANDROID_HOME,
    $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "Android\Sdk" })
) | Where-Object { $_ } | Select-Object -Unique

$apkAnalyzer = $null
foreach ($sdkRoot in $sdkRoots) {
    $latest = Join-Path $sdkRoot "cmdline-tools\latest\bin\apkanalyzer.bat"
    if (Test-Path -LiteralPath $latest) {
        $apkAnalyzer = $latest
        break
    }

    $cmdlineTools = Join-Path $sdkRoot "cmdline-tools"
    if (Test-Path -LiteralPath $cmdlineTools) {
        $apkAnalyzer = Get-ChildItem -LiteralPath $cmdlineTools -Recurse -File -Filter "apkanalyzer.bat" |
            Sort-Object FullName -Descending |
            Select-Object -First 1 -ExpandProperty FullName
        if ($apkAnalyzer) {
            break
        }
    }
}

if (-not $apkAnalyzer) {
    throw "apkanalyzer.bat was not found under the configured Android SDK."
}

$savedJavaOptions = $env:_JAVA_OPTIONS
try {
    # The build sets _JAVA_OPTIONS for Gradle. Java prints that setting to stderr,
    # which Windows PowerShell 5 promotes to NativeCommandError under Stop mode.
    Remove-Item Env:\_JAVA_OPTIONS -ErrorAction SilentlyContinue

    $packages = @(& $apkAnalyzer dex packages $resolvedApk 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "apkanalyzer failed to inspect DEX packages (exit=$LASTEXITCODE)."
    }
    if (($packages -join "`n") -notmatch [regex]::Escape("expo.modules.apkinstaller.ApkInstallerModule")) {
        throw "APK verification failed: ApkInstallerModule is missing from the DEX packages."
    }

    $permissions = @(& $apkAnalyzer manifest permissions $resolvedApk 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "apkanalyzer failed to inspect manifest permissions (exit=$LASTEXITCODE)."
    }
    if (($permissions -join "`n") -notmatch [regex]::Escape("android.permission.REQUEST_INSTALL_PACKAGES")) {
        throw "APK verification failed: REQUEST_INSTALL_PACKAGES permission is missing."
    }

    $actualVersion = (& $apkAnalyzer manifest version-name $resolvedApk 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "apkanalyzer failed to inspect the version name (exit=$LASTEXITCODE)."
    }
    if ($ExpectedVersion -and $actualVersion -ne $ExpectedVersion) {
        throw "APK verification failed: expected version $ExpectedVersion, found $actualVersion."
    }

    $actualVersionCode = (& $apkAnalyzer manifest version-code $resolvedApk 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "apkanalyzer failed to inspect the version code (exit=$LASTEXITCODE)."
    }
    if ($ExpectedVersionCode -and $actualVersionCode -ne $ExpectedVersionCode) {
        throw "APK verification failed: expected versionCode $ExpectedVersionCode, found $actualVersionCode."
    }

    Write-Output "APK_VERIFIED=True"
    Write-Output "APK_PATH=$resolvedApk"
    Write-Output "APK_VERSION=$actualVersion"
    Write-Output "APK_VERSION_CODE=$actualVersionCode"
    Write-Output "APK_INSTALLER_MODULE=True"
    Write-Output "REQUEST_INSTALL_PACKAGES=True"
} finally {
    if ($null -eq $savedJavaOptions) {
        Remove-Item Env:\_JAVA_OPTIONS -ErrorAction SilentlyContinue
    } else {
        $env:_JAVA_OPTIONS = $savedJavaOptions
    }
}
