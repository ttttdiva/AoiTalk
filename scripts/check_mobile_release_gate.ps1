param(
    [string]$Base = "",
    [string]$Target = "HEAD",
    [switch]$ReleaseDisabled
)

$ErrorActionPreference = "Stop"

if (-not $Base) {
    $upstream = & git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null
    if ($LASTEXITCODE -eq 0 -and $upstream -and $upstream -ne '@{u}') {
        $Base = $upstream.Trim()
    } else {
        & git show-ref --verify --quiet refs/remotes/origin/main
        if ($LASTEXITCODE -eq 0) {
            $Base = "origin/main"
        } else {
            & git show-ref --verify --quiet refs/remotes/origin/master
            if ($LASTEXITCODE -eq 0) {
                $Base = "origin/master"
            } else {
                $Base = "HEAD~1"
            }
        }
    }
}

$files = @(& git diff --name-only "$Base..$Target")
$mobile = @($files | Where-Object { $_ -match '^(mobile/|mobile\\)' })
$required = ($mobile.Count -gt 0 -and -not $ReleaseDisabled)

function Read-MobileExpoConfig([string]$Ref) {
    $raw = @(& git show "${Ref}:mobile/app.json" 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $raw) {
        return $null
    }
    try {
        return (($raw -join [Environment]::NewLine) | ConvertFrom-Json).expo
    } catch {
        return $null
    }
}

$versionBumpValid = $true
$versionBumpError = ""
if ($required) {
    $baseExpo = Read-MobileExpoConfig $Base
    $targetExpo = Read-MobileExpoConfig $Target
    if (-not $baseExpo -or -not $targetExpo) {
        $versionBumpValid = $false
        $versionBumpError = "mobile/app.json could not be read at both refs."
    } else {
        try {
            $baseVersion = [version]$baseExpo.version
            $targetVersion = [version]$targetExpo.version
            $baseVersionCode = [int]$baseExpo.android.versionCode
            $targetVersionCode = [int]$targetExpo.android.versionCode
            if ($targetVersion -le $baseVersion) {
                $versionBumpValid = $false
                $versionBumpError = "expo.version must increase ($baseVersion -> $targetVersion)."
            } elseif ($targetVersionCode -le $baseVersionCode) {
                $versionBumpValid = $false
                $versionBumpError = "expo.android.versionCode must increase ($baseVersionCode -> $targetVersionCode)."
            }
        } catch {
            $versionBumpValid = $false
            $versionBumpError = "expo.version/versionCode is not a valid numeric release version."
        }
    }
}

"BASE=$Base"
"TARGET=$Target"
"MOBILE_CHANGED=$($mobile.Count -gt 0)"
"RELEASE_REQUIRED=$required"
if ($mobile.Count) {
    "MOBILE_FILES:"
    $mobile | ForEach-Object { "- $_" }
}
if ($required) {
    "REQUIRED_ACTIONS:"
    "- typecheck"
    "- lint"
    "- version/versionCode bump"
    "- APK build"
    "- GitHub Release upload"
    "- latest.json update"
    "VERSION_BUMP_VALID=$versionBumpValid"
    if (-not $versionBumpValid) {
        "VERSION_BUMP_ERROR=$versionBumpError"
        exit 1
    }
}
