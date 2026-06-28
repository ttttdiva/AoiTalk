param(
    [string]$Base = "main",
    [int[]]$Pr = @(),
    [ValidateSet("merge", "squash", "rebase")]
    [string]$Method = "merge",
    [switch]$Execute,
    [switch]$SkipChecks
)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param(
        [string]$Command,
        [string[]]$Arguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $Command $($Arguments -join ' ')"
    }
}

function Read-GhJson {
    param([string[]]$Arguments)

    $text = & gh @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: gh $($Arguments -join ' ')"
    }
    if (-not $text) {
        return @()
    }
    return $text | ConvertFrom-Json
}

function Test-ReleaseRequired {
    param(
        [int]$PrNumber,
        [string]$BaseBranch
    )

    $gate = Join-Path (Get-Location) "scripts/check_mobile_release_gate.ps1"
    if (-not (Test-Path -LiteralPath $gate)) {
        "RELEASE_GATE=missing"
        return $false
    }

    $tmpRef = "refs/codex/pr-$PrNumber"
    try {
        Invoke-Checked git @("fetch", "--quiet", "origin", "pull/$PrNumber/head:$tmpRef", "--force")
        $gateOutput = & powershell -NoProfile -ExecutionPolicy Bypass -File $gate -Base "origin/$BaseBranch" -Target $tmpRef
        if ($LASTEXITCODE -ne 0) {
            throw "Release gate failed for PR #$PrNumber"
        }
        $gateOutput | ForEach-Object { $_ }
        return [bool]($gateOutput | Select-String -SimpleMatch "RELEASE_REQUIRED=True")
    }
    finally {
        & git update-ref -d $tmpRef 2>$null
    }
}

$null = Get-Command gh -ErrorAction Stop
$null = Get-Command git -ErrorAction Stop
Invoke-Checked git @("fetch", "--quiet", "origin", $Base)

if ($Pr.Count -gt 0) {
    $prs = foreach ($number in $Pr) {
        Read-GhJson @(
            "pr", "view", "$number",
            "--json", "number,title,isDraft,mergeStateStatus,headRefName,baseRefName"
        )
    }
} else {
    $prs = Read-GhJson @(
        "pr", "list",
        "--state", "open",
        "--base", $Base,
        "--json", "number,title,isDraft,mergeStateStatus,headRefName,baseRefName"
    )
}

if (-not $prs -or $prs.Count -eq 0) {
    "OPEN_PR_COUNT=0"
    exit 0
}

"OPEN_PR_COUNT=$($prs.Count)"

foreach ($prInfo in $prs) {
    $number = [int]$prInfo.number
    "PR=#$number"
    "TITLE=$($prInfo.title)"
    "HEAD=$($prInfo.headRefName)"
    "BASE=$($prInfo.baseRefName)"
    "MERGE_STATE=$($prInfo.mergeStateStatus)"

    if ($prInfo.isDraft) {
        throw "PR #$number is draft"
    }

    $releaseRequired = Test-ReleaseRequired -PrNumber $number -BaseBranch $Base
    if ($releaseRequired) {
        throw "PR #$number requires release handling; do not use fast-path gh merge"
    }

    if (-not $SkipChecks) {
        Invoke-Checked gh @("pr", "checks", "$number", "--watch=false")
    }

    if ($Execute) {
        Invoke-Checked gh @("pr", "merge", "$number", "--$Method", "--delete-branch")
        "MERGED=#$number"
    } else {
        "DRY_RUN=gh pr merge $number --$Method --delete-branch"
    }
}

if ($Execute) {
    Invoke-Checked git @("fetch", "--quiet", "origin", $Base)
}
