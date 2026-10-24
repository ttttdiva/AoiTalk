param(
    [string]$EnvFile = (Join-Path $PSScriptRoot "..\.env"),
    [switch]$Watch,
    [switch]$ValidateOnly,
    [switch]$Stop,
    [switch]$ControlInternal,
    [string]$InstanceNonce = "",
    [ValidateSet("console", "background-task")]
    [string]$OwnerMode = "console",
    [ValidateSet("watch-child", "watch-current")]
    [string]$ProcessKind = "watch-current",
    [ValidateRange(5, 300)]
    [int]$PollSeconds = 30
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$PackageRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PackageRoot "..\.."))
$Entry = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "aoitalk_plugin_stdio.py"))
$ControlLauncherEntry = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "connect_tunnel.ps1"))
$Python = Join-Path $PackageRoot "venv\Scripts\python.exe"
$RuntimeAlias = "aoitalk-41"
$ProfileName = "aoitalk-plugin-41"
$TaskName = "AoiTalk Plugin 41"
$DataRoot = Join-Path $RepoRoot "data\aoitalk_plugin"
$ProfileRoot = Join-Path $DataRoot "profiles"
$CacheRoot = Join-Path $RepoRoot "cache\aoitalk_plugin"
$LogRoot = Join-Path $RepoRoot "logs\aoitalk_plugin"
$StatusPath = Join-Path $LogRoot "status.json"
$SupervisorLog = Join-Path $LogRoot "supervisor.log"
$TunnelLog = Join-Path $LogRoot "tunnel-client.log"
$IdentityPath = Join-Path $DataRoot "runtime-identity.json"
$LockPath = Join-Path $LogRoot "watch.lock"
$RuntimeKeyName = "HOME_MCP_AOITALK_RUNTIME_API_KEY"
$script:StateLock = $null
$AllowedConfigKeys = @(
    "HOME_MCP_AOITALK_TUNNEL_ID", $RuntimeKeyName, "TUNNEL_CLIENT_PATH",
    "AOITALK_PLUGIN_TUNNEL_CLIENT_VERSION", "AOITALK_PLUGIN_HEALTH_LISTEN_ADDR",
    "HOME_MCP_AOITALK_ENV_FILE"
)
$SavedEnvironment = @{}
$LockStream = $null
$script:ControlStopAttempted = $false

. (Join-Path $PSScriptRoot "plugin_launch_control_lib.ps1")
Initialize-PluginLaunchControl $RepoRoot $ControlLauncherEntry

if (($Watch -and $Stop) -or (($Watch -or $Stop) -and -not $ControlInternal -and $ValidateOnly)) {
    throw "Choose one Plugin launcher mode."
}
if ($Watch -and -not $ControlInternal) {
    & (Join-Path $PSScriptRoot "plugin_launch_control.ps1") -Mode WatchCurrent -EnvFile $EnvFile
    return
}
if ($Stop -and -not $ControlInternal) {
    & (Join-Path $PSScriptRoot "plugin_launch_control.ps1") -Mode Stop -EnvFile $EnvFile
    return
}

$script:ControlInstanceNonce = $InstanceNonce
$script:ControlOwnerMode = $OwnerMode
$script:ControlProcessKind = $ProcessKind
$script:ControlProcessStartUtc = if ($InstanceNonce) { Get-ControlProcessStart $PID } else { "" }

function Read-LiteralEnv([string]$Path) {
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $values }
    $lineNumber = 0
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        $lineNumber++
        $item = $line.Trim().TrimStart([char]0xFEFF)
        if (-not $item -or $item.StartsWith("#")) { continue }
        if ($item -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            throw "Invalid literal KEY=VALUE assignment in plugin config at line $lineNumber."
        }
        $name = $Matches[1]
        if ($name -notin $AllowedConfigKeys) { continue }
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                                       ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if ($values.ContainsKey($name)) { throw "Duplicate plugin config key '$name'." }
        $values[$name] = $value
    }
    return $values
}

function Get-Setting([string]$Name, [string]$Default = "") {
    $processValue = [Environment]::GetEnvironmentVariable($Name, "Process")
    if (-not [string]::IsNullOrWhiteSpace($processValue)) { return $processValue.Trim() }
    if ($script:Config.ContainsKey($Name)) { return [string]$script:Config[$Name] }
    return $Default
}

function Write-SafeLog([string]$Message) {
    $line = "{0} {1}" -f [DateTimeOffset]::Now.ToString("o"), $Message
    Add-Content -LiteralPath $script:SupervisorLog -Value $line -Encoding UTF8
}

. (Join-Path $PSScriptRoot "runtime_ownership.ps1")

function Write-Status($Status, [string]$State) {
    $runtimePid = $null
    if ($Status -and $Status.process -and $Status.process.pid) { $runtimePid = [int]$Status.process.pid }
    $snapshot = [ordered]@{
        checked_at = [DateTimeOffset]::Now.ToString("o")
        state = $State
        launcher_pid = $PID
        runtime_pid = $runtimePid
        alias = $script:RuntimeAlias
        profile = $script:ProfileName
        task_name = $script:TaskName
        process_running = [bool]($Status -and $Status.process_running)
        healthy = [bool]($Status -and $Status.healthy)
        ready = [bool]($Status -and $Status.ready)
        ui_url = if ($Status) { $Status.ui_url } else { $null }
    }
    $snapshot | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $script:StatusPath -Encoding UTF8
}

function Stop-AndVerifyOwnRuntime([ValidateRange(0, 300)][int]$TimeoutSeconds = 20) {
    $null = Stop-OwnRuntime
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $status = Get-ClientStatus
        if ($status) {
            Assert-StatusOwnership $status -RequireTunnelId
            if (Test-ObjectHasProperty $status "process_running") {
                $running = Get-ObjectValue $status "process_running"
                if ($null -ne $running -and -not [bool]$running) { return $true }
            }
        } else {
            $listCapture = Invoke-NativeCapture -Executable $script:TunnelClient -ArgumentList @(
                "runtimes", "list", "--json"
            )
            if ($listCapture.ExitCode -eq 0) {
                try {
                    $list = ($listCapture.StdOut -join "`n") | ConvertFrom-Json -ErrorAction Stop
                    if (-not (Assert-ListedAliasOwnership $list)) { return $true }
                } catch { }
            }
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "STOP_UNCONFIRMED: the verified runtime did not report stopped before timeout."
}

function Invoke-RequestedStop {
    $request = Get-ControlStopRequest
    if (-not $request) { return $false }
    $script:ControlStopAttempted = $true
    Set-ControlStopState $request "received"
    try {
        $stopped = Stop-AndVerifyOwnRuntime
        Write-Status $null "stopped"
        Set-ControlStopState $request "stopped"
        return $true
    } catch {
        Write-Status $null "stop_unconfirmed"
        Set-ControlStopState $request "stop_unconfirmed" ($_.Exception.Message -replace '[\r\n]+', ' ')
        throw
    }
}

function Test-ClientVersion {
    $capture = Invoke-NativeCapture -Executable $script:TunnelClient -ArgumentList @("--version")
    if ($capture.ExitCode -ne 0 -or (($capture.StdOut -join " ") -notmatch ("(?<![0-9.])" + [regex]::Escape($script:ExpectedClientVersion) + "(?:\+|\s|$)"))) {
        throw "Expected official tunnel-client version $script:ExpectedClientVersion. Run tunnel-client --version and inspect the official release page."
    }
}

function Test-AoiTalkSettings {
    $oldEnvPath = [Environment]::GetEnvironmentVariable("HOME_MCP_AOITALK_ENV_FILE", "Process")
    $oldPluginConfigPath = [Environment]::GetEnvironmentVariable("AOITALK_PLUGIN_CONFIG_FILE", "Process")
    $oldPluginRoot = [Environment]::GetEnvironmentVariable("AOITALK_PLUGIN_ROOT", "Process")
    $oldRepositoryRoot = [Environment]::GetEnvironmentVariable("AOITALK_REPOSITORY_ROOT", "Process")
    $identityPath = Get-Setting "HOME_MCP_AOITALK_ENV_FILE"
    if (-not [string]::IsNullOrWhiteSpace($identityPath)) {
        if ([IO.Path]::IsPathRooted($identityPath)) { $identityPath = [IO.Path]::GetFullPath($identityPath) }
        else { $identityPath = [IO.Path]::GetFullPath((Join-Path $PackageRoot $identityPath)) }
        [Environment]::SetEnvironmentVariable("HOME_MCP_AOITALK_ENV_FILE", $identityPath, "Process")
    }
    [Environment]::SetEnvironmentVariable("AOITALK_PLUGIN_CONFIG_FILE", $EnvFile, "Process")
    [Environment]::SetEnvironmentVariable("AOITALK_PLUGIN_ROOT", $PackageRoot, "Process")
    [Environment]::SetEnvironmentVariable("AOITALK_REPOSITORY_ROOT", $RepoRoot, "Process")
    Push-Location $PackageRoot
    try {
        $capture = Invoke-NativeCapture -Executable $Python -ArgumentList @(
            "-c", "from aoitalk_plugin.aoitalk import load_settings; load_settings()"
        )
        if ($capture.ExitCode -ne 0) {
            throw "AoiTalk API configuration is incomplete. Set the existing bootstrap admin username/password in the plugin .env or point HOME_MCP_AOITALK_ENV_FILE at the 41 .env."
        }
    } finally {
        Pop-Location
        [Environment]::SetEnvironmentVariable("HOME_MCP_AOITALK_ENV_FILE", $oldEnvPath, "Process")
        [Environment]::SetEnvironmentVariable("AOITALK_PLUGIN_CONFIG_FILE", $oldPluginConfigPath, "Process")
        [Environment]::SetEnvironmentVariable("AOITALK_PLUGIN_ROOT", $oldPluginRoot, "Process")
        [Environment]::SetEnvironmentVariable("AOITALK_REPOSITORY_ROOT", $oldRepositoryRoot, "Process")
    }
}

try {
    $EnvFile = [IO.Path]::GetFullPath($EnvFile)
    $script:Config = Read-LiteralEnv $EnvFile
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Run plugins\AoiTalk_Plugin\scripts\setup.ps1 first." }

    $TunnelId = if ($script:Config.ContainsKey("HOME_MCP_AOITALK_TUNNEL_ID")) {
        [string]$script:Config["HOME_MCP_AOITALK_TUNNEL_ID"]
    } else { "" }
    $script:TunnelId = $TunnelId
    if ([string]::IsNullOrWhiteSpace($TunnelId) -or $TunnelId -match 'replace|placeholder|example' -or
        $TunnelId -notmatch '^tunnel_[A-Za-z0-9_-]+$') {
        throw "Set HOME_MCP_AOITALK_TUNNEL_ID in the 41 plugin .env after a separate AoiTalk tunnel is issued; there is no home-tunnel fallback."
    }
    $homeId = [Environment]::GetEnvironmentVariable("CONTROL_PLANE_TUNNEL_ID", "Process")
    if ($homeId -and $TunnelId -eq $homeId) { throw "The AoiTalk plugin tunnel ID must differ from the home CONTROL_PLANE_TUNNEL_ID." }

    $RuntimeKey = if ($script:Config.ContainsKey($RuntimeKeyName)) {
        [string]$script:Config[$RuntimeKeyName]
    } else { "" }
    if (-not $Stop -and ([string]::IsNullOrWhiteSpace($RuntimeKey) -or $RuntimeKey -match 'replace|placeholder|example')) {
        throw "Set $RuntimeKeyName in the 41 plugin .env. The value is passed only by env reference and is never put in argv or logs."
    }

    $ClientPath = Get-Setting "TUNNEL_CLIENT_PATH"
    if ($ClientPath) {
        if (-not [IO.Path]::IsPathRooted($ClientPath)) { $ClientPath = Join-Path $PackageRoot $ClientPath }
    } else {
        $known = Join-Path $PackageRoot ".tools\tunnel-client.exe"
        if (Test-Path -LiteralPath $known -PathType Leaf) { $ClientPath = $known }
        else {
            $command = Get-Command tunnel-client -ErrorAction SilentlyContinue
            if ($command) { $ClientPath = $command.Source }
        }
    }
    if (-not $ClientPath -or -not (Test-Path -LiteralPath $ClientPath -PathType Leaf)) {
        throw "Install the official tunnel-client v0.0.14 Windows release, then set TUNNEL_CLIENT_PATH or place tunnel-client.exe under plugins\AoiTalk_Plugin\.tools."
    }
    $script:TunnelClient = [IO.Path]::GetFullPath($ClientPath)
    $script:ExpectedClientVersion = Get-Setting "AOITALK_PLUGIN_TUNNEL_CLIENT_VERSION" "0.0.14"
    Test-ClientVersion

    $HealthAddress = Get-Setting "AOITALK_PLUGIN_HEALTH_LISTEN_ADDR" "127.0.0.1:8783"
    $script:RepoRoot = $RepoRoot
    $script:Entry = $Entry
    $script:RuntimeAlias = $RuntimeAlias
    $script:ProfileName = $ProfileName
    $script:IdentityPath = $IdentityPath
    $script:TaskName = $TaskName
    $script:StatusPath = $StatusPath
    $script:SupervisorLog = $SupervisorLog
    $script:TunnelLog = $TunnelLog
    $script:ProfileRoot = $ProfileRoot

    $identity = Read-RuntimeIdentity
    if ($identity) { Assert-OwnIdentity }
    elseif ($Stop) { throw "This checkout has no verified runtime identity marker; no alias was stopped." }
    elseif (Test-Path -LiteralPath $ProfileRoot) {
        $existing = @(Get-ChildItem -LiteralPath $ProfileRoot -Force -ErrorAction SilentlyContinue)
        if ($existing.Count -gt 0) { throw "AoiTalk Plugin profile directory already has content without this checkout's identity marker; it was left untouched." }
    }

    if (-not $Stop) { Test-AoiTalkSettings }
    if ($ValidateOnly) {
        Assert-LocalAliasOwnership
        Test-PortAvailable $HealthAddress -AllowOwnedRuntime
        Write-Output "Configuration valid: alias=$RuntimeAlias profile=$ProfileName task=$TaskName health=$HealthAddress"
        return
    }

    New-Item -ItemType Directory -Force -Path $DataRoot,$ProfileRoot,$CacheRoot,$LogRoot | Out-Null

    $envNames = @(
        $RuntimeKeyName,"CONTROL_PLANE_API_KEY","OPENAI_ADMIN_KEY","OPENAI_ADMIN_API_KEY","OPENAI_API_KEY",
        "CONTROL_PLANE_TUNNEL_ID","HOME_MCP_AOITALK_ENV_FILE","AOITALK_PLUGIN_CONFIG_FILE","AOITALK_PLUGIN_ROOT","AOITALK_REPOSITORY_ROOT",
        "HEALTH_LISTEN_ADDR","HEALTH_URL_FILE","PID_FILE",
        "LOG_FILE","XDG_CACHE_HOME","TUNNEL_CLIENT_PROFILE_DIR"
    )
    foreach ($name in $envNames) {
        $SavedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }
    foreach ($name in @("CONTROL_PLANE_API_KEY","OPENAI_ADMIN_KEY","OPENAI_ADMIN_API_KEY","OPENAI_API_KEY","CONTROL_PLANE_TUNNEL_ID")) {
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    $runtimeKeyForProcess = if ($Stop) { $null } else { $RuntimeKey }
    [Environment]::SetEnvironmentVariable($RuntimeKeyName, $runtimeKeyForProcess, "Process")
    $envFileSetting = Get-Setting "HOME_MCP_AOITALK_ENV_FILE"
    if ($envFileSetting) {
        if ([IO.Path]::IsPathRooted($envFileSetting)) { $appEnv = [IO.Path]::GetFullPath($envFileSetting) }
        else { $appEnv = [IO.Path]::GetFullPath((Join-Path $PackageRoot $envFileSetting)) }
        [Environment]::SetEnvironmentVariable("HOME_MCP_AOITALK_ENV_FILE", $appEnv, "Process")
    }
    [Environment]::SetEnvironmentVariable("AOITALK_PLUGIN_CONFIG_FILE", $EnvFile, "Process")
    [Environment]::SetEnvironmentVariable("AOITALK_PLUGIN_ROOT", $PackageRoot, "Process")
    [Environment]::SetEnvironmentVariable("AOITALK_REPOSITORY_ROOT", $RepoRoot, "Process")
    [Environment]::SetEnvironmentVariable("HEALTH_LISTEN_ADDR", $HealthAddress, "Process")
    # Managed connect owns the per-alias health file; do not inherit a Home override.
    [Environment]::SetEnvironmentVariable("HEALTH_URL_FILE", $null, "Process")
    [Environment]::SetEnvironmentVariable("PID_FILE", (Join-Path $LogRoot "tunnel-client.pid"), "Process")
    [Environment]::SetEnvironmentVariable("LOG_FILE", $null, "Process")
    [Environment]::SetEnvironmentVariable("XDG_CACHE_HOME", $CacheRoot, "Process")
    [Environment]::SetEnvironmentVariable("TUNNEL_CLIENT_PROFILE_DIR", $ProfileRoot, "Process")

    $script:StateLock = $null
    try { $script:StateLock = [IO.File]::Open($LockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw "Another AoiTalk Plugin watcher owns $LockPath; it was left untouched." }
    if ($Watch -and $ControlInternal) {
        if ($InstanceNonce -notmatch '^[0-9a-f]{32}$' -or -not $script:ControlProcessStartUtc) {
            throw "AoiTalk Plugin watcher instance identity is incomplete."
        }
        Set-ControlReceiptState "watching" $true
    }

    if (-not $identity) {
        Assert-LocalAliasOwnership
        $marker = [ordered]@{
            repo_root = $RepoRoot
            entry = $Entry
            alias = $RuntimeAlias
            profile = $ProfileName
            tunnel_id = $TunnelId
            profile_dir = $ProfileRoot
            task_name = $TaskName
        }
        $marker | ConvertTo-Json | Set-Content -LiteralPath $IdentityPath -Encoding UTF8
        $identity = Read-RuntimeIdentity
        Assert-OwnIdentity
    }

    if ($Stop) {
        $null = Stop-AndVerifyOwnRuntime
        Write-Status $null "stopped"
        Write-Output "STOPPED: verified runtime alias '$RuntimeAlias' is not running."
        return
    }

    # The dedicated port is checked only when the verified runtime is stopped.
    # A running but not-ready instance gets a bounded grace period before a watch
    # restart, so startup health does not trigger an immediate reconnect loop.
    $failures = 0
    while ($true) {
        if ($Watch -and (Invoke-RequestedStop)) { return }
        $status = $null
        try {
            $status = Get-ClientStatus
            if ($status) { Assert-StatusOwnership $status -RequireTunnelId }
            if (-not ($status -and $status.process_running)) {
                if ($Watch -and (Invoke-RequestedStop)) { return }
                Test-PortAvailable $HealthAddress
                Connect-OwnRuntime $TunnelId $RuntimeKeyName $Python $ProfileRoot $TunnelLog
                $status = Get-ClientStatus
                if ($status) { Assert-StatusOwnership $status -RequireTunnelId }
            }

            if ($status -and $status.process_running -and $status.healthy -and $status.ready) {
                Write-Status $status "ready"
                $failures = 0
                if (-not $Watch) {
                    Write-Output "AoiTalk Plugin tunnel runtime is healthy and ready (alias '$RuntimeAlias')."
                    return
                }
                if (Wait-ControlInterval $PollSeconds) { continue }
                continue
            }
            $state = if ($status -and $status.process_running) { "starting" } else { "retrying" }
            Write-Status $status $state
            if (-not $Watch) { throw "Tunnel runtime is not ready. Inspect logs\aoitalk_plugin\status.json and the displayed launcher error." }
            $failures++
            if ($status -and $status.process_running -and $failures -ge 3) { Stop-OwnRuntime }
            Write-SafeLog "Runtime is not ready; the dedicated alias will be checked again."
            if (Wait-ControlInterval $PollSeconds) { continue }
        } catch {
            if ($_.Exception.Message -like "AoiTalk runtime ownership mismatch:*") { throw }
            if (-not $Watch) { throw }
            if (Get-ControlStopRequest) { throw }
            Write-SafeLog "Runtime check/connect failed; retrying after the configured interval."
            try {
                $status = Get-ClientStatus
                if ($status) { Assert-StatusOwnership $status -RequireTunnelId }
                if ($status -and $status.process_running) {
                    $failures++
                    if ($failures -ge 3) { Stop-OwnRuntime }
                }
            } catch {
                if ($_.Exception.Message -like "AoiTalk runtime ownership mismatch:*") { throw }
            }
            Write-Status $status "retrying"
            if (Wait-ControlInterval $PollSeconds) { continue }
        }
    }
} finally {
    if ($Watch -and $script:StateLock -and $InstanceNonce -and -not $script:ControlStopAttempted) {
        $request = Get-ControlStopRequest
        if ($request) {
            try { Invoke-RequestedStop | Out-Null } catch { }
        } else {
            $script:ControlStopAttempted = $true
            try {
                if (Stop-AndVerifyOwnRuntime) { Write-Status $null "stopped_on_watcher_exit" }
            } catch {
                try { Write-Status $null "stop_unconfirmed" } catch { }
            }
        }
        try { Set-ControlReceiptState "exited" $false } catch { }
    }
    if ($script:StateLock) { $script:StateLock.Dispose() }
    foreach ($name in $SavedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $SavedEnvironment[$name], "Process")
    }
}
