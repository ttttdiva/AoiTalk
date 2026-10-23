function Initialize-PluginLaunchControl([string]$RepositoryRoot, [string]$LauncherEntry) {
    $script:ControlRepoRoot = [IO.Path]::GetFullPath($RepositoryRoot)
    $script:ControlEntry = [IO.Path]::GetFullPath($LauncherEntry)
    $script:ControlPackageRoot = [IO.Path]::GetFullPath((Join-Path $script:ControlRepoRoot "plugins\AoiTalk_Plugin"))
    $script:ControlDataRoot = Join-Path $script:ControlRepoRoot "data\aoitalk_plugin"
    $script:ControlLogRoot = Join-Path $script:ControlRepoRoot "logs\aoitalk_plugin"
    $script:ControlReceiptPath = Join-Path $script:ControlLogRoot "watcher-instance.json"
    $script:ControlLaunchRequestPath = Join-Path $script:ControlLogRoot "launch-request.json"
    $script:ControlStopRequestPath = Join-Path $script:ControlLogRoot "stop-request.json"
    $script:ControlStopStatePath = Join-Path $script:ControlLogRoot "stop-state.json"
    $script:ControlStopAckPath = Join-Path $script:ControlLogRoot "stop-ack.json"
    $script:ControlLaunchGatePath = Join-Path $script:ControlLogRoot "launch.gate"
    $script:ControlWatchLockPath = Join-Path $script:ControlLogRoot "watch.lock"
    $script:ControlAlias = "aoitalk-41"
    $script:ControlProfile = "aoitalk-plugin-41"
}

function Write-ControlJson([string]$Path, $Value) {
    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $temporary = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    $json = ConvertTo-Json -InputObject $Value -Depth 12
    [IO.File]::WriteAllText($temporary, $json, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Read-ControlJson([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop) }
    catch { throw "BLOCKED: Plugin control metadata is malformed or unreadable; no process or runtime was changed." }
}

function Get-ControlProcessStart([int]$ProcessId) {
    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        return $process.StartTime.ToUniversalTime().ToString("o")
    } catch { return $null }
}

function Get-ControlStartTicks($Value) {
    if ($Value -is [DateTimeOffset]) { return $Value.UtcTicks }
    if ($Value -is [DateTime]) { return $Value.ToUniversalTime().Ticks }
    return [DateTimeOffset]::Parse([string]$Value).UtcTicks
}

function Test-ControlProcessIdentity($Receipt, [switch]$RequireWatchLock) {
    if (-not $Receipt -or [int]$Receipt.pid -le 0 -or [string]::IsNullOrWhiteSpace([string]$Receipt.process_start_utc)) { return $false }
    $start = Get-ControlProcessStart ([int]$Receipt.pid)
    if (-not $start) { return $false }
    try {
        if ((Get-ControlStartTicks $start) -ne (Get-ControlStartTicks $Receipt.process_start_utc)) { return $false }
    } catch { return $false }

    $processInfo = Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId = {0}" -f [int]$Receipt.pid) -ErrorAction SilentlyContinue
    if (-not $processInfo -or -not $processInfo.CommandLine) { return $false }
    $commandLine = [string]$processInfo.CommandLine
    $instanceNonce = [string]$Receipt.instance_nonce
    if ([string]$Receipt.process_kind -eq "watch-child") {
        if ($commandLine -notmatch '(?i)plugin_launch_control\.ps1' -or
            $commandLine -notmatch '(?i)-Mode\s+WatchChild' -or
            $commandLine -notmatch [regex]::Escape($instanceNonce)) { return $false }
    } else {
        if ($commandLine -notmatch '(?i)connect_tunnel\.ps1' -or $commandLine -notmatch '(?i)-Watch') { return $false }
    }

    if ($RequireWatchLock) {
        if ([string]$Receipt.state -ne "watching" -or -not [bool]$Receipt.watch_lock_acquired) { return $false }
        $probe = $null
        try {
            $probe = [IO.File]::Open($script:ControlWatchLockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
            return $false
        } catch {
            return $true
        } finally { if ($probe) { $probe.Dispose() } }
    }
    return $true
}

function Test-ControlReceiptOwner($Receipt) {
    if (-not $Receipt) { return $false }
    try {
        return ([IO.Path]::GetFullPath([string]$Receipt.repo_root).Equals($script:ControlRepoRoot, [StringComparison]::OrdinalIgnoreCase) -and
            [IO.Path]::GetFullPath([string]$Receipt.entry).Equals($script:ControlEntry, [StringComparison]::OrdinalIgnoreCase) -and
            [string]$Receipt.alias -eq $script:ControlAlias -and [string]$Receipt.profile -eq $script:ControlProfile -and
            [string]$Receipt.instance_nonce -match '^[0-9a-f]{32}$')
    } catch { return $false }
}

function Get-VerifiedControlWatcher {
    $receipt = Read-ControlJson $script:ControlReceiptPath
    if (-not $receipt) { return $null }
    if (-not (Test-ControlReceiptOwner $receipt)) { throw "BLOCKED: watcher receipt belongs to an unknown repository, entry, alias, profile, or instance." }
    if (-not (Test-ControlProcessIdentity $receipt)) { return $null }
    if (-not (Test-ControlProcessIdentity $receipt -RequireWatchLock)) { return $null }
    return $receipt
}

function Get-LaunchGate {
    $parent = Split-Path -Parent $script:ControlLaunchGatePath
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    try { return [IO.File]::Open($script:ControlLaunchGatePath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { return $null }
}

function Wait-ForLaunchGate([int]$TimeoutSeconds = 45) {
    $until = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $gate = Get-LaunchGate
        if ($gate) { return $gate }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $until)
    throw "BLOCKED: another Plugin start/stop operation holds the launch gate."
}

function Set-ControlReceiptState([string]$State, [bool]$WatchLockAcquired) {
    $receipt = [ordered]@{
        schema = 1
        state = $State
        instance_nonce = $script:ControlInstanceNonce
        pid = $PID
        process_start_utc = $script:ControlProcessStartUtc
        repo_root = $script:ControlRepoRoot
        entry = $script:ControlEntry
        alias = $script:ControlAlias
        profile = $script:ControlProfile
        mode = $script:ControlOwnerMode
        process_kind = $script:ControlProcessKind
        watch_lock_acquired = $WatchLockAcquired
        updated_at = [DateTimeOffset]::UtcNow.ToString("o")
    }
    Write-ControlJson $script:ControlReceiptPath $receipt
}

function Get-ControlStopRequest {
    $request = Read-ControlJson $script:ControlStopRequestPath
    if (-not $request -or [string]$request.instance_nonce -ne [string]$script:ControlInstanceNonce) { return $null }
    if ([string]::IsNullOrWhiteSpace([string]$request.request_id)) { return $null }
    return $request
}

function Set-ControlStopState($Request, [string]$State, [string]$Detail = "") {
    $ack = [ordered]@{
        instance_nonce = [string]$Request.instance_nonce
        request_id = [string]$Request.request_id
        state = $State
        detail = $Detail
        updated_at = [DateTimeOffset]::UtcNow.ToString("o")
    }
    if ($State -eq "stopped") { Write-ControlJson $script:ControlStopAckPath $ack }
    Write-ControlJson $script:ControlStopStatePath $ack
}

function Wait-ControlInterval([int]$Seconds) {
    for ($index = 0; $index -lt $Seconds; $index++) {
        if (Get-ControlStopRequest) { return $true }
        Start-Sleep -Seconds 1
    }
    return [bool](Get-ControlStopRequest)
}
