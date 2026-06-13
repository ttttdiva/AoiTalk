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
}