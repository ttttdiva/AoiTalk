<#
.SYNOPSIS
  Poll GitHub Actions runs for a commit until all complete or timeout.

.DESCRIPTION
  Lightweight helper to wait for GitHub Actions on a specific commit (typically after push to main).

  Exit codes:
    0 = PASS        — all runs succeeded
    1 = FAIL        — CI ran but at least one job failed, or failure cause is not explicitly infrastructure
    2 = UNAVAILABLE — GitHub explicitly reported billing / spending limit / quota / infrastructure
    3 = TIMEOUT     — no runs found or runs did not complete within timeout

  UNAVAILABLE requires an explicit GitHub message (check-run annotations or check-run output).
  Empty steps + short duration alone is NOT UNAVAILABLE.

  Note: ci.yml triggers on push to main OR pull_request.
  On main, push to main is enough to start CI. Feature-branch push without PR does not start CI.

.PARAMETER Commit
  Git commit SHA to check (default: HEAD).

.PARAMETER Repo
  GitHub repository in owner/name form (default: ttttdiva/41_AoiTalk).

.PARAMETER TimeoutMinutes
  Maximum wait time in minutes (default: 30).
#>
[CmdletBinding()]
param(
    [string]$Commit = "HEAD",
    [string]$Repo = "ttttdiva/41_AoiTalk",
    [int]$TimeoutMinutes = 30
)

$ErrorActionPreference = "Stop"

# Exit code constants
$EXIT_PASS = 0
$EXIT_FAIL = 1
$EXIT_UNAVAILABLE = 2
$EXIT_TIMEOUT = 3

$script:InfrastructurePatterns = @(
    'payments have failed',
    'payments failed',
    'spending limit',
    'account payments',
    'usage quota',
    'quota exceeded',
    'billing & plans'
)

function Test-InfrastructureMessage {
    param([string]$Text)

    if ([string]::IsNullOrWhiteSpace($Text)) {
        return $false
    }

    $lower = $Text.ToLowerInvariant()
    foreach ($pattern in $script:InfrastructurePatterns) {
        if ($lower.Contains($pattern)) {
            return $true
        }
    }
    return $false
}

function Get-CheckRunInfrastructureText {
    param(
        [string]$RepoName,
        [object]$Job
    )

    $id = $Job.databaseId
    if (-not $id) { $id = $Job.id }
    if (-not $id) { return "" }

    $parts = [System.Collections.Generic.List[string]]::new()
    try {
        $annJson = gh api "repos/$RepoName/check-runs/$id/annotations" 2>$null
        if ($annJson) {
            $annotations = $annJson | ConvertFrom-Json
            foreach ($ann in @($annotations)) {
                $parts.Add("$($ann.message) $($ann.title) $($ann.raw_details)")
            }
        }
    }
    catch {
        # Annotation fetch failed; try check-run output below
    }

    try {
        $cr = gh api "repos/$RepoName/check-runs/$id" 2>$null | ConvertFrom-Json
        if ($cr -and $cr.output) {
            $parts.Add("$($cr.output.title) $($cr.output.summary) $($cr.output.text)")
        }
    }
    catch {
        # Check-run fetch failed
    }

    return ($parts -join " ")
}

function Test-JobInfrastructureFailure {
    param(
        [string]$RepoName,
        [object]$Job,
        [string]$EvidenceText
    )

    if ($Job.conclusion -ne 'failure') {
        return $false
    }

    $text = $EvidenceText
    if (-not $PSBoundParameters.ContainsKey('EvidenceText')) {
        $text = Get-CheckRunInfrastructureText -RepoName $RepoName -Job $Job
    }
    return (Test-InfrastructureMessage -Text $text)
}

function Test-RunInfrastructureFailure {
    param(
        [string]$RepoName,
        [long]$RunId
    )

    $runDetail = gh run view $RunId --repo $RepoName --json jobs 2>$null | ConvertFrom-Json
    if (-not $runDetail -or -not $runDetail.jobs) {
        return $false
    }

    $failedJobs = @($runDetail.jobs | Where-Object { $_.conclusion -eq 'failure' })
    if ($failedJobs.Count -eq 0) {
        return $false
    }

    # UNAVAILABLE only when EVERY failed job has an explicit infrastructure message
    foreach ($job in $failedJobs) {
        if (-not (Test-JobInfrastructureFailure -RepoName $RepoName -Job $job)) {
            return $false
        }
    }

    return $true
}

function Get-WaitCiOutcome {
    param(
        [object[]]$Runs,
        [scriptblock]$IsRunUnavailable
    )

    if (-not $Runs -or @($Runs).Count -eq 0) {
        return 'EMPTY'
    }

    $pending = @($Runs | Where-Object { $_.status -ne "completed" })
    if ($pending.Count -gt 0) {
        return 'PENDING'
    }

    $failed = @($Runs | Where-Object { $_.conclusion -ne "success" })
    if ($failed.Count -eq 0) {
        return 'PASS'
    }

    if (-not $IsRunUnavailable) {
        return 'FAIL'
    }

    foreach ($run in $failed) {
        $isInfra = & $IsRunUnavailable $run
        if (-not $isInfra) {
            return 'FAIL'
        }
    }
    return 'UNAVAILABLE'
}

if ($MyInvocation.InvocationName -eq '.') {
    return
}

# Resolve commit SHA
$sha = (git rev-parse $Commit 2>$null)
if (-not $sha) {
    Write-Error "Could not resolve commit: $Commit"
    exit $EXIT_FAIL
}

$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
$pollIntervalSec = 15

Write-Host "Waiting for CI on $Repo @ $($sha.Substring(0, 7)) (timeout: ${TimeoutMinutes}m)..."
Write-Host "CI triggers on push to main or pull_request." -ForegroundColor DarkGray

while ($true) {
    $json = gh run list --repo $Repo --commit $sha --json databaseId,status,conclusion,name,event --limit 10 2>$null
    if (-not $json) {
        Write-Host "No runs found yet for commit $($sha.Substring(0, 7)). Retrying..."
        if ((Get-Date) -ge $deadline) {
            Write-Host ""
            Write-Host "TIMEOUT: no CI runs found for commit $($sha.Substring(0, 7))." -ForegroundColor Yellow
            Write-Host "On feature branches, CI requires pull_request (or merge to main)." -ForegroundColor Yellow
            exit $EXIT_TIMEOUT
        }
        Start-Sleep -Seconds $pollIntervalSec
        continue
    }

    $runs = @($json | ConvertFrom-Json)
    if ($runs.Count -eq 0) {
        Write-Host "No runs found yet. Retrying..."
        if ((Get-Date) -ge $deadline) {
            Write-Host ""
            Write-Host "TIMEOUT: no CI runs found for commit $($sha.Substring(0, 7))." -ForegroundColor Yellow
            Write-Host "On feature branches, CI requires pull_request (or merge to main)." -ForegroundColor Yellow
            exit $EXIT_TIMEOUT
        }
        Start-Sleep -Seconds $pollIntervalSec
        continue
    }

    $pending = @($runs | Where-Object { $_.status -ne "completed" })
    if ($pending.Count -gt 0) {
        $names = ($pending | ForEach-Object { $_.name }) -join ", "
        Write-Host "[$((Get-Date).ToString('HH:mm:ss'))] In progress: $names"
        if ((Get-Date) -ge $deadline) {
            Write-Host ""
            Write-Host "TIMEOUT: CI still running after ${TimeoutMinutes} minutes." -ForegroundColor Yellow
            exit $EXIT_TIMEOUT
        }
        Start-Sleep -Seconds $pollIntervalSec
        continue
    }

    $outcome = Get-WaitCiOutcome -Runs $runs -IsRunUnavailable {
        param($run)
        Test-RunInfrastructureFailure -RepoName $Repo -RunId $run.databaseId
    }

    if ($outcome -eq 'PASS') {
        Write-Host ""
        Write-Host "CI PASS: All $(@($runs).Count) run(s) succeeded for commit $($sha.Substring(0, 7))." -ForegroundColor Green
        exit $EXIT_PASS
    }

    if ($outcome -eq 'UNAVAILABLE') {
        Write-Host ""
        Write-Host "CI UNAVAILABLE: GitHub reported billing/quota/infrastructure failure (not a code failure)." -ForegroundColor Yellow
        foreach ($run in @($runs | Where-Object { $_.conclusion -ne "success" })) {
            Write-Host "  run $($run.databaseId) [$($run.name)]: $($run.conclusion) (infrastructure)"
            Write-Host "    gh run view $($run.databaseId) --repo $Repo"
            Write-Host "    gh api repos/$Repo/check-runs/<job-id>/annotations"
        }
        Write-Host ""
        Write-Host "Exit code: $EXIT_UNAVAILABLE (UNAVAILABLE)." -ForegroundColor Yellow
        Write-Host "Do not start local full CI automatically. If targeted verification passed, report COMPLETE_CI_UNAVAILABLE (not a blocker; not CI PASS)." -ForegroundColor Yellow
        Write-Host ""
        Write-Host "TIMEOUT (exit $EXIT_TIMEOUT) is NOT treated as UNAVAILABLE — extend timeout or investigate CI queue."
        exit $EXIT_UNAVAILABLE
    }

    Write-Host ""
    Write-Host "CI FAIL: Real failure(s) detected for commit $($sha.Substring(0, 7)):" -ForegroundColor Red
    foreach ($run in @($runs | Where-Object { $_.conclusion -ne "success" })) {
        Write-Host "  run $($run.databaseId) [$($run.name)]: $($run.conclusion)" -ForegroundColor Red
        Write-Host "    gh run view $($run.databaseId) --repo $Repo --log-failed"
    }
    exit $EXIT_FAIL
}
