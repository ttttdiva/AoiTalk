param(
    [ValidateSet("Ensure", "Stop", "WatchChild", "WatchCurrent")]
    [string]$Mode = "Ensure",
    [string]$InstanceNonce = "",
    [string]$EnvFile = (Join-Path $PSScriptRoot "..\.env")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$PackageRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PackageRoot "..\.."))
$Launcher = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "connect_tunnel.ps1"))
$ControlScript = [IO.Path]::GetFullPath($MyInvocation.MyCommand.Path)
$WindowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$PowerShell = if (Test-Path -LiteralPath $WindowsPowerShell -PathType Leaf) { $WindowsPowerShell } else { (Get-Command powershell.exe -ErrorAction Stop).Source }
$script:StopWaitSeconds = 35
. (Join-Path $PSScriptRoot "plugin_launch_control_lib.ps1")
Initialize-PluginLaunchControl $RepoRoot $Launcher

function Read-PluginConfig([string]$Path) {
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $values }
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        $item = $line.Trim().TrimStart([char]0xFEFF)
        if (-not $item -or $item.StartsWith("#")) { continue }
        if ($item -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { throw "Invalid literal plugin .env assignment." }
        $key = $Matches[1]
        if ($key -notin @("HOME_MCP_AOITALK_TUNNEL_ID", "HOME_MCP_AOITALK_RUNTIME_API_KEY", "TUNNEL_CLIENT_PATH")) { continue }
        if ($values.ContainsKey($key)) { throw "Duplicate required plugin .env key." }
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$key] = $value
    }
    return $values
}

function Get-ConfiguredTunnelClient($Config) {
    $candidate = [Environment]::GetEnvironmentVariable("TUNNEL_CLIENT_PATH", "Process")
    if ([string]::IsNullOrWhiteSpace($candidate) -and $Config.ContainsKey("TUNNEL_CLIENT_PATH")) { $candidate = [string]$Config["TUNNEL_CLIENT_PATH"] }
    if ($candidate) {
        if (-not [IO.Path]::IsPathRooted($candidate)) { $candidate = Join-Path $PackageRoot $candidate }
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return [IO.Path]::GetFullPath($candidate) }
        return $null
    }
    $known = Join-Path $PackageRoot ".tools\tunnel-client.exe"
    if (Test-Path -LiteralPath $known -PathType Leaf) { return $known }
    $command = Get-Command tunnel-client -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Test-Configured([string]$Path, [switch]$Report) {
    $python = Join-Path $PackageRoot "venv\Scripts\python.exe"
    $config = Read-PluginConfig $Path
    $tunnelId = if ($config.ContainsKey("HOME_MCP_AOITALK_TUNNEL_ID")) { [string]$config["HOME_MCP_AOITALK_TUNNEL_ID"] } else { "" }
    $runtimeKey = if ($config.ContainsKey("HOME_MCP_AOITALK_RUNTIME_API_KEY")) { [string]$config["HOME_MCP_AOITALK_RUNTIME_API_KEY"] } else { "" }
    $client = Get-ConfiguredTunnelClient $config
    $validId = $tunnelId -match '^tunnel_[A-Za-z0-9_-]+$' -and $tunnelId -notmatch '(?i)replace|placeholder|example'
    $validKey = -not [string]::IsNullOrWhiteSpace($runtimeKey) -and $runtimeKey -notmatch '(?i)replace|placeholder|example'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not $validId -or -not $validKey -or -not $client) {
        if ($Report) { Write-Host "NOT_CONFIGURED: Plugin tunnel ID, runtime key, package venv, or official tunnel-client is missing." }
        return $false
    }
    return $true
}

function Test-OwnTaskDefinition($Task) {
    if (-not $Task) { return $false }
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) { return $false }
    $action = $actions[0]
    try {
        if (-not [IO.Path]::GetFullPath([string]$action.WorkingDirectory).Equals($RepoRoot, [StringComparison]::OrdinalIgnoreCase)) { return $false }
        if (-not [IO.Path]::GetFullPath([string]$action.Execute).Equals([IO.Path]::GetFullPath($PowerShell), [StringComparison]::OrdinalIgnoreCase)) { return $false }
    } catch { return $false }
    $values = @([regex]::Matches([string]$action.Arguments, '"(?:[^"]|"")*"|\S+') | ForEach-Object {
        $value = $_.Value
        if ($value.Length -ge 2 -and $value.StartsWith('"') -and $value.EndsWith('"')) { $value = $value.Substring(1, $value.Length - 2).Replace('""', '"') }
        $value
    })
    $file = ""
    for ($index = 0; $index -lt $values.Count; $index++) {
        if ([string]$values[$index] -iin @("-Command", "-CommandWithArgs", "-EncodedCommand")) { break }
        if ([string]$values[$index] -ieq "-File" -and $index + 1 -lt $values.Count) { $file = [string]$values[$index + 1]; break }
    }
    try { return ([IO.Path]::GetFullPath($file).Equals($Launcher, [StringComparison]::OrdinalIgnoreCase) -and $values -icontains "-Watch") }
    catch { return $false }
}

function Get-RunningOwnTaskProcess {
    $task = Get-ScheduledTask -TaskName "AoiTalk Plugin 41" -ErrorAction SilentlyContinue
    if (-not $task -or [string]$task.State -ne "Running" -or -not (Test-OwnTaskDefinition $task)) { return $null }
    $processes = @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -in @("powershell.exe", "pwsh.exe") -and
        $_.CommandLine -match '(?i)connect_tunnel\.ps1' -and $_.CommandLine -match '(?i)-Watch' -and
        $_.CommandLine -match [regex]::Escape($RepoRoot)
    })
    if ($processes.Count -eq 1) { return $processes[0] }
    if ($processes.Count -gt 1) { throw "LAUNCH_UNCERTAIN: multiple processes match the dedicated background Task watcher." }
    return $null
}

function Get-PotentialControlWatchers {
    $lookupErrors = @()
    $allProcesses = @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue -ErrorVariable lookupErrors)
    if ($lookupErrors.Count -gt 0) { throw "LAUNCH_UNCERTAIN: process identity inventory could not be verified; no Plugin action was taken." }
    return @($allProcesses | Where-Object {
        if ($_.Name -notin @("powershell.exe", "pwsh.exe") -or -not $_.CommandLine -or
            $_.CommandLine -notmatch [regex]::Escape($RepoRoot)) { return $false }
        $command = [string]$_.CommandLine
        $connectWatcher = $command -match [regex]::Escape($Launcher) -and $command -match '(?i)-Watch'
        $childWatcher = $command -match [regex]::Escape($ControlScript) -and
            $command -match '(?i)-Mode\s+WatchChild'
        return ($connectWatcher -or $childWatcher)
    })
}

function Test-ControlIdentityAbsent($Identity) {
    if (-not $Identity -or [int]$Identity.pid -le 0 -or [string]::IsNullOrWhiteSpace([string]$Identity.process_start_utc)) { return $false }
    $lookupErrors = @()
    $processInfo = Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId = {0}" -f [int]$Identity.pid) -ErrorAction SilentlyContinue -ErrorVariable lookupErrors
    if ($lookupErrors.Count -gt 0) { return $false }
    if (-not $processInfo) { return $true }
    $actual = Get-ControlProcessStart ([int]$Identity.pid)
    if (-not $actual) { return $false }
    try { return ((Get-ControlStartTicks $actual) -ne (Get-ControlStartTicks $Identity.process_start_utc)) }
    catch { return $false }
}

function Remove-StaleControlInstance($Receipt) {
    if (-not (Test-ControlIdentityAbsent $Receipt)) { throw "STOP_UNCONFIRMED: watcher identity cannot be proven absent; no process was killed." }
    Remove-Item -LiteralPath $script:ControlReceiptPath -Force -ErrorAction SilentlyContinue
    $request = Read-ControlJson $script:ControlStopRequestPath
    if ($request -and [string]$request.instance_nonce -eq [string]$Receipt.instance_nonce) { Remove-Item -LiteralPath $script:ControlStopRequestPath -Force -ErrorAction SilentlyContinue }
}

function New-LaunchRequest([string]$Nonce) {
    $request = [ordered]@{
        state = "launching"
        instance_nonce = $Nonce
        pid = 0
        process_start_utc = ""
        repo_root = $RepoRoot
        entry = $Launcher
        alias = $script:ControlAlias
        profile = $script:ControlProfile
        mode = "console"
        created_at = [DateTimeOffset]::UtcNow.ToString("o")
    }
    Write-ControlJson $script:ControlLaunchRequestPath $request
}

function Set-LaunchProcess($Process, [string]$Nonce) {
    $request = Read-ControlJson $script:ControlLaunchRequestPath
    if (-not $request -or [string]$request.instance_nonce -ne $Nonce) { throw "LAUNCH_UNCERTAIN: launch request identity changed during start." }
    $request.pid = [int]$Process.Id
    $request.process_start_utc = ""
    $request.state = "launching"
    Write-ControlJson $script:ControlLaunchRequestPath $request
    $start = Get-ControlProcessStart ([int]$Process.Id)
    if (-not $start) {
        $request.state = "launch_uncertain"
        Write-ControlJson $script:ControlLaunchRequestPath $request
        throw "LAUNCH_UNCERTAIN: the watcher process start time could not be verified."
    }
    $request.process_start_utc = $start
    Write-ControlJson $script:ControlLaunchRequestPath $request
}

function Wait-ForWatcher([string]$Nonce, [int]$TimeoutSeconds = 25) {
    $until = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $receipt = Read-ControlJson $script:ControlReceiptPath
        if ($receipt -and [string]$receipt.instance_nonce -eq $Nonce) {
            if ((Test-ControlReceiptOwner $receipt) -and (Test-ControlProcessIdentity $receipt -RequireWatchLock)) { return $receipt }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $until)
    return $null
}

function Invoke-Ensure {
    if (-not (Test-Configured $EnvFile -Report)) { return 2 }
    $gate = Wait-ForLaunchGate
    try {
        $existing = Get-VerifiedControlWatcher
        if ($existing) {
            $message = if ([string]$existing.mode -eq "background-task") { "ALREADY_RUNNING: Plugin watcher is owned by the verified background Task." } else { "ALREADY_RUNNING: Plugin watcher is active in its existing window." }
            Write-Host $message
            return 0
        }
        $receipt = Read-ControlJson $script:ControlReceiptPath
        if ($receipt) {
            if (-not (Test-ControlReceiptOwner $receipt)) { throw "BLOCKED: watcher receipt has unknown ownership; no new window was started." }
            if (-not (Test-ControlIdentityAbsent $receipt)) {
                $pending = Wait-ForWatcher ([string]$receipt.instance_nonce) 15
                if ($pending) { Write-Host "ALREADY_RUNNING: Plugin watcher launch completed in its existing window."; return 0 }
                throw "LAUNCH_UNCERTAIN: a watcher process identity is present but its control lock is not verified; no duplicate was started."
            }
            Remove-StaleControlInstance $receipt
        }

        $pendingRequest = Read-ControlJson $script:ControlLaunchRequestPath
        if ($pendingRequest) {
            if (-not (Test-ControlReceiptOwner $pendingRequest)) { throw "BLOCKED: pending launch belongs to an unknown repository or instance." }
            if ([int]$pendingRequest.pid -le 0) { throw "LAUNCH_UNCERTAIN: a previous launch has no process receipt; no duplicate was started." }
            if (-not (Test-ControlIdentityAbsent $pendingRequest)) {
                $pending = Wait-ForWatcher ([string]$pendingRequest.instance_nonce) 15
                if ($pending) { Remove-Item -LiteralPath $script:ControlLaunchRequestPath -Force -ErrorAction SilentlyContinue; Write-Host "ALREADY_RUNNING: Plugin watcher launch completed in its existing window."; return 0 }
                throw "LAUNCH_UNCERTAIN: a previous launch process is alive but has not acquired the watcher lock; no duplicate was started."
            }
            Remove-Item -LiteralPath $script:ControlLaunchRequestPath -Force -ErrorAction SilentlyContinue
        }

        $taskProcess = Get-RunningOwnTaskProcess
        if ($taskProcess) {
            Write-Host "ALREADY_RUNNING: the verified AoiTalk Plugin background Task owns the watcher."
            return 0
        }
        $unownedWatchers = @(Get-PotentialControlWatchers)
        if ($unownedWatchers.Count -gt 0) {
            throw "LAUNCH_UNCERTAIN: a Plugin watcher-shaped process exists without a matching live instance receipt; no duplicate was started."
        }

        $nonce = [guid]::NewGuid().ToString("N")
        New-LaunchRequest $nonce
        $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Mode WatchChild -InstanceNonce {1} -EnvFile "{2}"' -f $ControlScript, $nonce, $EnvFile
        try {
            $process = Start-Process -FilePath $PowerShell -ArgumentList $arguments -WorkingDirectory $RepoRoot -WindowStyle Normal -PassThru
            Set-LaunchProcess $process $nonce
        } catch {
            $request = Read-ControlJson $script:ControlLaunchRequestPath
            if ($request -and [string]$request.instance_nonce -eq $nonce -and [int]$request.pid -le 0) {
                Remove-Item -LiteralPath $script:ControlLaunchRequestPath -Force -ErrorAction SilentlyContinue
            }
            throw
        }
        $started = Wait-ForWatcher $nonce
        if (-not $started) {
            $request = Read-ControlJson $script:ControlLaunchRequestPath
            if ($request -and [string]$request.instance_nonce -eq $nonce) { $request.state = "launch_uncertain"; Write-ControlJson $script:ControlLaunchRequestPath $request }
            Write-Host "LAUNCH_UNCERTAIN: watcher process was accepted but its instance and lock were not verified; no retry was started."
            return 1
        }
        Remove-Item -LiteralPath $script:ControlLaunchRequestPath -Force -ErrorAction SilentlyContinue
        Write-Host "STARTED: Plugin watcher launch accepted in a new AoiTalk Plugin window; runtime readiness is pending."
        return 0
    } finally { $gate.Dispose() }
}

function Invoke-Stop {
    $gate = Wait-ForLaunchGate
    try {
        $watcher = Get-VerifiedControlWatcher
        if ($watcher) {
            $requestId = [guid]::NewGuid().ToString("N")
            $request = [ordered]@{
                instance_nonce = [string]$watcher.instance_nonce
                request_id = $requestId
                state = "requested"
                requested_at = [DateTimeOffset]::UtcNow.ToString("o")
            }
            Write-ControlJson $script:ControlStopRequestPath $request
            $until = [DateTime]::UtcNow.AddSeconds($script:StopWaitSeconds)
            do {
                $ack = Read-ControlJson $script:ControlStopAckPath
                if ($ack -and [string]$ack.instance_nonce -eq [string]$watcher.instance_nonce -and [string]$ack.request_id -eq $requestId -and [string]$ack.state -eq "stopped") {
                    if (Test-ControlIdentityAbsent $watcher) {
                        Write-Host "STOPPED: verified Plugin runtime stop was acknowledged and the watcher exited."
                        return 0
                    }
                }
                $state = Read-ControlJson $script:ControlStopStatePath
                if ($state -and [string]$state.instance_nonce -eq [string]$watcher.instance_nonce -and
                    [string]$state.request_id -eq $requestId -and [string]$state.state -eq "stop_unconfirmed") {
                    Write-Host "STOP_UNCONFIRMED: watcher could not verify runtime shutdown; no force-kill was attempted."
                    return 3
                }
                Start-Sleep -Milliseconds 250
            } while ([DateTime]::UtcNow -lt $until)
            $ack = Read-ControlJson $script:ControlStopAckPath
            if ($ack -and [string]$ack.instance_nonce -eq [string]$watcher.instance_nonce -and [string]$ack.request_id -eq $requestId) {
                Write-Host "STOP_UNCONFIRMED: watcher did not verify runtime shutdown and exit before timeout; no force-kill was attempted."
            } else {
                Write-Host "STOP_UNCONFIRMED: watcher did not acknowledge the request before timeout; no force-kill was attempted."
            }
            return 3
        }

        $receipt = Read-ControlJson $script:ControlReceiptPath
        if ($receipt) {
            if (-not (Test-ControlReceiptOwner $receipt)) { throw "BLOCKED: watcher identity is not owned by this repository, alias, and profile." }
            Remove-StaleControlInstance $receipt
        }
        $launch = Read-ControlJson $script:ControlLaunchRequestPath
        if ($launch) {
            if (-not (Test-ControlReceiptOwner $launch)) { throw "BLOCKED: pending watcher launch has unknown ownership." }
            if (-not (Test-ControlIdentityAbsent $launch)) { throw "STOP_UNCONFIRMED: a launch process is still active without a verified watcher receipt; no force-kill was attempted." }
            Remove-Item -LiteralPath $script:ControlLaunchRequestPath -Force -ErrorAction SilentlyContinue
        }
        $unownedWatchers = @(Get-PotentialControlWatchers)
        if ($unownedWatchers.Count -gt 0) {
            throw "STOP_UNCONFIRMED: a Plugin watcher-shaped process has no matching live control receipt; no runtime was stopped."
        }
        & $Launcher -Stop -ControlInternal -EnvFile $EnvFile | ForEach-Object { Write-Host $_ }
        Write-Host "STOPPED: only the runtime verified by the existing ownership marker was stopped."
        return 0
    } finally { $gate.Dispose() }
}

function Invoke-Watcher([string]$Nonce, [switch]$CurrentProcess) {
    if ($CurrentProcess) {
        $gate = Wait-ForLaunchGate
        try {
            $existing = Get-VerifiedControlWatcher
            if ($existing) { Write-Host "ALREADY_RUNNING: Plugin watcher already owns this runtime."; return 0 }
            $receipt = Read-ControlJson $script:ControlReceiptPath
            if ($receipt) {
                if (-not (Test-ControlReceiptOwner $receipt)) { throw "BLOCKED: an existing watcher receipt has unknown ownership." }
                if (-not (Test-ControlIdentityAbsent $receipt)) {
                    $pending = Wait-ForWatcher ([string]$receipt.instance_nonce) 15
                    if ($pending) { Write-Host "ALREADY_RUNNING: Plugin watcher already owns this runtime."; return 0 }
                    throw "LAUNCH_UNCERTAIN: a watcher process is starting but has not acquired its control lock; no duplicate was started."
                }
                Remove-StaleControlInstance $receipt
            }
            $taskProcess = Get-RunningOwnTaskProcess
            if ($taskProcess -and [int]$taskProcess.ProcessId -ne $PID) {
                Write-Host "ALREADY_RUNNING: the verified AoiTalk Plugin background Task owns the watcher."
                return 0
            }
            $ownerMode = if ($taskProcess -and [int]$taskProcess.ProcessId -eq $PID) { "background-task" } else { "console" }
            $script:ControlOwnerMode = $ownerMode
            $script:ControlProcessKind = "watch-current"
            $script:ControlInstanceNonce = [guid]::NewGuid().ToString("N")
            $script:ControlProcessStartUtc = Get-ControlProcessStart $PID
            if (-not $script:ControlProcessStartUtc) { throw "Could not verify the current watcher process start time." }
            Set-ControlReceiptState "launching" $false
            $Nonce = $script:ControlInstanceNonce
        } finally { $gate.Dispose() }
    } else {
        if ($Nonce -notmatch '^[0-9a-f]{32}$') { throw "Invalid internal watcher instance token." }
        $request = $null
        $receiptDeadline = [DateTime]::UtcNow.AddSeconds(15)
        do {
            $request = Read-ControlJson $script:ControlLaunchRequestPath
            if ($request -and [string]$request.instance_nonce -eq $Nonce -and [int]$request.pid -eq $PID -and
                -not [string]::IsNullOrWhiteSpace([string]$request.process_start_utc)) { break }
            Start-Sleep -Milliseconds 100
        } while ([DateTime]::UtcNow -lt $receiptDeadline)
        if (-not $request -or [string]$request.instance_nonce -ne $Nonce -or [int]$request.pid -ne $PID -or
            [string]::IsNullOrWhiteSpace([string]$request.process_start_utc)) {
            throw "LAUNCH_UNCERTAIN: no matching parent launch receipt was received."
        }
        $currentStart = Get-ControlProcessStart $PID
        if (-not $currentStart -or (Get-ControlStartTicks $currentStart) -ne (Get-ControlStartTicks $request.process_start_utc)) {
            throw "LAUNCH_UNCERTAIN: watcher process start time does not match its launch receipt."
        }
        $script:ControlOwnerMode = "console"
        $script:ControlProcessKind = "watch-child"
        $script:ControlInstanceNonce = $Nonce
        $script:ControlProcessStartUtc = $currentStart
        Set-ControlReceiptState "launching" $false
    }

    try {
        try { $Host.UI.RawUI.WindowTitle = "AoiTalk Plugin" } catch { }
        Write-Host "AoiTalk Plugin watcher is active. Use run_plugin.bat --stop to stop this watcher and its verified runtime."
        & $Launcher -Watch -ControlInternal -InstanceNonce $Nonce -OwnerMode $script:ControlOwnerMode `
            -ProcessKind $script:ControlProcessKind -EnvFile $EnvFile | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { throw "Plugin watcher ended with exit code $LASTEXITCODE." }
        return 0
    } finally {
        try { Set-ControlReceiptState "exited" $false } catch { }
    }
}

if ($MyInvocation.InvocationName -eq ".") { return }

try {
    $EnvFile = [IO.Path]::GetFullPath($EnvFile)
    if ($Mode -eq "WatchCurrent") {
        $result = Invoke-Watcher "" -CurrentProcess
        $global:AOITALK_PLUGIN_CONTROL_RESULT = [int]$result
        return
    }
    switch ($Mode) {
        "Ensure" { $result = Invoke-Ensure }
        "Stop" { $result = Invoke-Stop }
        "WatchChild" { $result = Invoke-Watcher $InstanceNonce }
    }
    exit [int]$result
} catch {
    $message = [string]$_.Exception.Message
    if ($message -match '^(BLOCKED|STOP_UNCONFIRMED|LAUNCH_UNCERTAIN):') { Write-Output $message }
    else { Write-Output ("FAILED: " + ($message -replace '(?i)(key|secret|credential)\s*[:=]\s*\S+', '$1=[redacted]')) }
    exit 1
}
