param(
    [string]$EnvFile = (Join-Path $PSScriptRoot "..\.env"),
    [switch]$Watch,
    [switch]$ValidateOnly,
    [switch]$Stop,
    [ValidateRange(5, 300)]
    [int]$PollSeconds = 30
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$PackageRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PackageRoot "..\.."))
$Entry = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "aoitalk_plugin_stdio.py"))
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

function Get-ClientStatus {
    $output = @(& $script:TunnelClient runtimes status $script:RuntimeAlias --json 2>$null)
    $code = $LASTEXITCODE
    if ($code -ne 0) { return $null }
    try { return (($output -join "`n") | ConvertFrom-Json -ErrorAction Stop) }
    catch { throw "tunnel-client returned an unreadable status for alias '$script:RuntimeAlias'." }
}

function Test-ClientListHasAlias($Value) {
    if ($null -eq $Value) { return $false }
    if ($Value -is [System.Collections.IDictionary]) {
        if (($Value.Contains("alias") -and $Value["alias"] -eq $script:RuntimeAlias) -or
            ($Value.Contains("name") -and $Value["name"] -eq $script:RuntimeAlias)) { return $true }
        foreach ($key in $Value.Keys) { if (Test-ClientListHasAlias $Value[$key]) { return $true } }
    } elseif ($Value -is [PSCustomObject]) {
        $aliasProperty = $Value.PSObject.Properties["alias"]
        $nameProperty = $Value.PSObject.Properties["name"]
        if (($aliasProperty -and $aliasProperty.Value -eq $script:RuntimeAlias) -or
            ($nameProperty -and $nameProperty.Value -eq $script:RuntimeAlias)) { return $true }
        foreach ($property in $Value.PSObject.Properties) {
            if (Test-ClientListHasAlias $property.Value) { return $true }
        }
    } elseif ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        foreach ($item in $Value) { if (Test-ClientListHasAlias $item) { return $true } }
    }
    return $false
}

function Read-RuntimeIdentity {
    if (-not (Test-Path -LiteralPath $script:IdentityPath -PathType Leaf)) { return $null }
    try { return (Get-Content -LiteralPath $script:IdentityPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop) }
    catch { throw "AoiTalk Plugin runtime identity file is malformed; refusing to touch alias '$script:RuntimeAlias'." }
}

function Assert-OwnIdentity {
    $identity = Read-RuntimeIdentity
    if (-not $identity -or
        -not [IO.Path]::GetFullPath([string]$identity.repo_root).Equals($script:RepoRoot, [StringComparison]::OrdinalIgnoreCase) -or
        -not [IO.Path]::GetFullPath([string]$identity.entry).Equals($script:Entry, [StringComparison]::OrdinalIgnoreCase) -or
        -not [IO.Path]::GetFullPath([string]$identity.profile_dir).Equals($script:ProfileRoot, [StringComparison]::OrdinalIgnoreCase) -or
        [string]$identity.alias -ne $script:RuntimeAlias -or [string]$identity.profile -ne $script:ProfileName) {
        throw "Runtime alias/profile identity does not match this 41 checkout and entry; it was left untouched."
    }
}

function Assert-LocalAliasOwnership {
    $identity = Read-RuntimeIdentity
    if ($identity) { Assert-OwnIdentity }
    $status = Get-ClientStatus
    if ($status -and -not $identity) {
        throw "Local tunnel-client alias '$script:RuntimeAlias' already exists without this 41 checkout's identity marker; it was left untouched."
    }
    if (-not $identity) {
        $listOutput = @(& $script:TunnelClient runtimes list --json 2>$null)
        if ($LASTEXITCODE -ne 0) { throw "Could not verify the local tunnel-client alias registry; refusing to create or replace an alias." }
        try { $list = ($listOutput -join "`n") | ConvertFrom-Json -ErrorAction Stop }
        catch { throw "Could not parse the local tunnel-client alias registry; refusing to create or replace an alias." }
        if (Test-ClientListHasAlias $list) {
            throw "Local tunnel-client alias '$script:RuntimeAlias' belongs to another profile; it was left untouched."
        }
    }
}

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

function Stop-OwnRuntime {
    Assert-OwnIdentity
    $status = Get-ClientStatus
    if (-not $status) {
        $output = @(& $script:TunnelClient runtimes list --json 2>$null)
        if ($LASTEXITCODE -ne 0) { throw "Could not verify whether the AoiTalk Plugin runtime is running; no runtime was stopped." }
        try { $list = ($output -join "`n") | ConvertFrom-Json -ErrorAction Stop }
        catch { throw "Could not parse the local tunnel-client alias registry; no runtime was stopped." }
        if (Test-ClientListHasAlias $list) {
            throw "The owned runtime alias exists but status is unavailable; no runtime was stopped."
        }
        return $false
    }
    if ($status -and $status.process_running) {
        & $script:TunnelClient runtimes stop $script:RuntimeAlias 1>> $script:TunnelLog 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Could not stop the verified AoiTalk Plugin runtime alias '$script:RuntimeAlias'." }
        return $true
    }
    return $false
}

function Test-PortAvailable([string]$Address, [switch]$AllowOwnedRuntime) {
    if ($Address -notmatch '^(127\.0\.0\.1|localhost):([0-9]{1,5})$') {
        throw "AOITALK_PLUGIN_HEALTH_LISTEN_ADDR must be a loopback IPv4 address and fixed port, for example 127.0.0.1:8783."
    }
    $port = [int]$Matches[2]
    if ($port -lt 1 -or $port -gt 65535) { throw "The dedicated tunnel health port is outside 1..65535." }
    if ($AllowOwnedRuntime) {
        try {
            Assert-OwnIdentity
            $status = Get-ClientStatus
            $ui = $null
            if ($status -and $status.process_running -and $status.healthy -and $status.ready -and
                [Uri]::TryCreate([string]$status.ui_url, [UriKind]::Absolute, [ref]$ui) -and
                $ui.Port -eq $port -and $ui.Host -in @("127.0.0.1", "localhost")) {
                return
            }
        } catch { }
    }
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Parse("127.0.0.1"), $port)
    try { $listener.Start() }
    catch { throw "Dedicated tunnel health port $Address is already in use; change AOITALK_PLUGIN_HEALTH_LISTEN_ADDR and retry." }
    finally { $listener.Stop() }
}

function Test-ClientVersion {
    $result = @(& $script:TunnelClient --version 2>$null)
    if ($LASTEXITCODE -ne 0 -or (($result -join " ") -notmatch ("(?<![0-9.])" + [regex]::Escape($script:ExpectedClientVersion) + "(?:\+|\s|$)"))) {
        throw "Expected official tunnel-client version $script:ExpectedClientVersion. Run tunnel-client --version and inspect the official release page."
    }
}

function Test-AoiTalkSettings {
    $oldEnvPath = [Environment]::GetEnvironmentVariable("HOME_MCP_AOITALK_ENV_FILE", "Process")
    $oldPluginRoot = [Environment]::GetEnvironmentVariable("AOITALK_PLUGIN_ROOT", "Process")
    $oldRepositoryRoot = [Environment]::GetEnvironmentVariable("AOITALK_REPOSITORY_ROOT", "Process")
    $configuredPath = Get-Setting "HOME_MCP_AOITALK_ENV_FILE"
    if ([string]::IsNullOrWhiteSpace($configuredPath)) { $configuredPath = $EnvFile }
    if ([IO.Path]::IsPathRooted($configuredPath)) { $apiEnvPath = [IO.Path]::GetFullPath($configuredPath) }
    else { $apiEnvPath = [IO.Path]::GetFullPath((Join-Path $PackageRoot $configuredPath)) }
    [Environment]::SetEnvironmentVariable("HOME_MCP_AOITALK_ENV_FILE", $apiEnvPath, "Process")
    [Environment]::SetEnvironmentVariable("AOITALK_PLUGIN_ROOT", $PackageRoot, "Process")
    [Environment]::SetEnvironmentVariable("AOITALK_REPOSITORY_ROOT", $RepoRoot, "Process")
    Push-Location $PackageRoot
    try {
        & $Python -c "from aoitalk_plugin.aoitalk import load_settings; load_settings()" 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "AoiTalk API configuration is incomplete. Set the existing bootstrap admin username/password in the plugin .env or point HOME_MCP_AOITALK_ENV_FILE at the 41 .env."
        }
    } finally {
        Pop-Location
        [Environment]::SetEnvironmentVariable("HOME_MCP_AOITALK_ENV_FILE", $oldEnvPath, "Process")
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
    if (-not $Stop -and ([string]::IsNullOrWhiteSpace($TunnelId) -or $TunnelId -match 'replace|placeholder|example' -or
        $TunnelId -notmatch '^tunnel_[A-Za-z0-9_-]+$')) {
        throw "Set HOME_MCP_AOITALK_TUNNEL_ID in the 41 plugin .env after a separate AoiTalk tunnel is issued; there is no home-tunnel fallback."
    }
    $homeId = [Environment]::GetEnvironmentVariable("CONTROL_PLANE_TUNNEL_ID", "Process")
    if (-not $Stop -and $homeId -and $TunnelId -eq $homeId) { throw "The AoiTalk plugin tunnel ID must differ from the home CONTROL_PLANE_TUNNEL_ID." }

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
        "CONTROL_PLANE_TUNNEL_ID","HOME_MCP_AOITALK_ENV_FILE","AOITALK_PLUGIN_ROOT","AOITALK_REPOSITORY_ROOT",
        "HEALTH_LISTEN_ADDR","HEALTH_URL_FILE","PID_FILE",
        "LOG_FILE","XDG_CACHE_HOME","TUNNEL_CLIENT_PROFILE_DIR"
    )
    foreach ($name in $envNames) {
        $SavedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }
    foreach ($name in @("CONTROL_PLANE_API_KEY","OPENAI_ADMIN_KEY","OPENAI_ADMIN_API_KEY","OPENAI_API_KEY","CONTROL_PLANE_TUNNEL_ID")) {
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    if (-not $Stop) { [Environment]::SetEnvironmentVariable($RuntimeKeyName, $RuntimeKey, "Process") }
    $envFileSetting = Get-Setting "HOME_MCP_AOITALK_ENV_FILE"
    if ($envFileSetting) {
        if ([IO.Path]::IsPathRooted($envFileSetting)) { $appEnv = [IO.Path]::GetFullPath($envFileSetting) }
        else { $appEnv = [IO.Path]::GetFullPath((Join-Path $PackageRoot $envFileSetting)) }
        [Environment]::SetEnvironmentVariable("HOME_MCP_AOITALK_ENV_FILE", $appEnv, "Process")
    } else {
        [Environment]::SetEnvironmentVariable("HOME_MCP_AOITALK_ENV_FILE", $EnvFile, "Process")
    }
    [Environment]::SetEnvironmentVariable("AOITALK_PLUGIN_ROOT", $PackageRoot, "Process")
    [Environment]::SetEnvironmentVariable("AOITALK_REPOSITORY_ROOT", $RepoRoot, "Process")
    [Environment]::SetEnvironmentVariable("HEALTH_LISTEN_ADDR", $HealthAddress, "Process")
    [Environment]::SetEnvironmentVariable("HEALTH_URL_FILE", (Join-Path $LogRoot "health-url.txt"), "Process")
    [Environment]::SetEnvironmentVariable("PID_FILE", (Join-Path $LogRoot "tunnel-client.pid"), "Process")
    [Environment]::SetEnvironmentVariable("LOG_FILE", $TunnelLog, "Process")
    [Environment]::SetEnvironmentVariable("XDG_CACHE_HOME", $CacheRoot, "Process")
    [Environment]::SetEnvironmentVariable("TUNNEL_CLIENT_PROFILE_DIR", $ProfileRoot, "Process")

    $script:StateLock = $null
    try { $script:StateLock = [IO.File]::Open($LockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw "Another AoiTalk Plugin watcher owns $LockPath; it was left untouched." }

    if (-not $identity) {
        Assert-LocalAliasOwnership
        $marker = [ordered]@{
            repo_root = $RepoRoot
            entry = $Entry
            alias = $RuntimeAlias
            profile = $ProfileName
            profile_dir = $ProfileRoot
            task_name = $TaskName
        }
        $marker | ConvertTo-Json | Set-Content -LiteralPath $IdentityPath -Encoding UTF8
        $identity = Read-RuntimeIdentity
        Assert-OwnIdentity
    }

    if ($Stop) {
        $wasRunning = Stop-OwnRuntime
        Write-Status $null $(if ($wasRunning) { "stopped" } else { "not_running" })
        if ($wasRunning) { Write-Output "Stopped only verified runtime alias '$RuntimeAlias'." }
        else { Write-Output "No running runtime was present under verified alias '$RuntimeAlias'." }
        return
    }

    # The dedicated port is checked only when the verified runtime is stopped.
    # A running but not-ready instance gets a bounded grace period before a watch
    # restart, so startup health does not trigger an immediate reconnect loop.
    $failures = 0
    while ($true) {
        $status = $null
        try {
            $status = Get-ClientStatus
            if (-not ($status -and $status.process_running)) {
                Test-PortAvailable $HealthAddress
                $pythonArg = '"' + $Python.Replace('\','/') + '" -m aoitalk_plugin.stdio'
                & $script:TunnelClient runtimes connect --alias $RuntimeAlias --tunnel-id $TunnelId --profile $ProfileName `
                    --profile-dir ($ProfileRoot.Replace('\','/')) --runtime-api-key ("env:" + $RuntimeKeyName) `
                    --mcp-command $pythonArg 1>> $TunnelLog 2>&1
                if ($LASTEXITCODE -ne 0) { throw "tunnel-client runtimes connect failed with exit code $LASTEXITCODE." }
                $status = Get-ClientStatus
            }

            if ($status -and $status.process_running -and $status.healthy -and $status.ready) {
                Write-Status $status "ready"
                $failures = 0
                if (-not $Watch) {
                    Write-Output "AoiTalk Plugin tunnel runtime is healthy and ready (alias '$RuntimeAlias')."
                    return
                }
                Start-Sleep -Seconds $PollSeconds
                continue
            }
            $state = if ($status -and $status.process_running) { "starting" } else { "retrying" }
            Write-Status $status $state
            if (-not $Watch) { throw "Tunnel runtime is not ready. Inspect logs\aoitalk_plugin\status.json and tunnel-client.log." }
            $failures++
            if ($status -and $status.process_running -and $failures -ge 3) { Stop-OwnRuntime }
            Write-SafeLog "Runtime is not ready; the dedicated alias will be checked again."
            Start-Sleep -Seconds $PollSeconds
        } catch {
            if (-not $Watch) { throw }
            Write-SafeLog "Runtime check/connect failed; retrying after the configured interval."
            try {
                $status = Get-ClientStatus
                if ($status -and $status.process_running) {
                    $failures++
                    if ($failures -ge 3) { Stop-OwnRuntime }
                }
            } catch { }
            Write-Status $status "retrying"
            Start-Sleep -Seconds $PollSeconds
        }
    }
} finally {
    if ($script:StateLock) { $script:StateLock.Dispose() }
    foreach ($name in $SavedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $SavedEnvironment[$name], "Process")
    }
}
