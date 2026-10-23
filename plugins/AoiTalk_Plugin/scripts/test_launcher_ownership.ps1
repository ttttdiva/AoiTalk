$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

foreach ($sourceFile in @("connect_tunnel.ps1", "install_autostart.ps1", "runtime_ownership.ps1", "test_launcher_ownership.ps1")) {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $PSScriptRoot $sourceFile), [ref]$tokens, [ref]$parseErrors) | Out-Null
    if ($parseErrors.Count) {
        throw "PowerShell parse failed in $sourceFile`: $($parseErrors[0].Message)"
    }
}

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Assert-OwnershipFailure([scriptblock]$Action, [string]$Name) {
    $failedAsExpected = $false
    try { & $Action }
    catch { $failedAsExpected = $_.Exception.Message -like "AoiTalk runtime ownership mismatch:*" }
    Assert-True $failedAsExpected "$Name did not fail with a concrete ownership mismatch."
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\..\.."))
$scratchRoot = Join-Path $repoRoot (".local\launcher-ownership-" + [guid]::NewGuid().ToString("N"))
$dataRoot = Join-Path $scratchRoot "data"
$profileRoot = Join-Path $dataRoot "profiles"
$identityPath = Join-Path $dataRoot "runtime-identity.json"
$statusPath = Join-Path $scratchRoot "status.json"
$listPath = Join-Path $scratchRoot "list.json"
$callsPath = Join-Path $scratchRoot "calls.txt"
$clientPath = Join-Path $scratchRoot "fake-tunnel-client.cmd"
$pythonFixturePath = Join-Path $scratchRoot "python-settings-fixture.cmd"
$settingsOutputPath = Join-Path $scratchRoot "settings-output.json"
$launcherPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "connect_tunnel.ps1"))
$profileName = "aoitalk-plugin-41"
$runtimeAlias = "aoitalk-41"
$tunnelId = "tunnel_fixture_41"

New-Item -ItemType Directory -Force -Path $scratchRoot,$dataRoot,$profileRoot | Out-Null
try {
    @'
@echo off
if /I "%~1 %~2"=="runtimes status" goto status
if /I "%~1 %~2"=="runtimes list" goto list
if /I "%~1 %~2"=="runtimes stop" goto stop
if /I "%~1 %~2"=="runtimes connect" goto connect
exit /b 0
:status
if not exist "%AOITALK_TEST_STATUS%" exit /b 1
type "%AOITALK_TEST_STATUS%"
exit /b 0
:list
type "%AOITALK_TEST_LIST%"
exit /b 0
:stop
echo stop>>"%AOITALK_TEST_CALLS%"
exit /b 0
:connect
echo connect>>"%AOITALK_TEST_CALLS%"
exit /b 0
'@ | Set-Content -LiteralPath $clientPath -Encoding ASCII

    $script:RepoRoot = $repoRoot
    $script:Entry = $launcherPath
    $script:ProfileRoot = $profileRoot
    $script:IdentityPath = $identityPath
    $script:RuntimeAlias = $runtimeAlias
    $script:ProfileName = $profileName
    $script:TunnelId = $tunnelId
    $script:TunnelClient = $clientPath
    $script:TunnelLog = Join-Path $scratchRoot "tunnel.log"
    New-Item -ItemType File -Force -Path $script:TunnelLog | Out-Null
    $env:AOITALK_TEST_STATUS = $statusPath
    $env:AOITALK_TEST_LIST = $listPath
    $env:AOITALK_TEST_CALLS = $callsPath
    . (Join-Path $PSScriptRoot "runtime_ownership.ps1")

    # The official v0.0.14 list has top-level metadata and an aliases array;
    # it does not put alias/name at the top level.
    [ordered]@{
        admin_profile = "synthetic-admin"
        admin_profile_path = Join-Path $scratchRoot "admin-profile"
        aliases = @()
        state_root = Join-Path $scratchRoot "state"
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $listPath -Encoding UTF8
    Remove-Item -LiteralPath $identityPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $statusPath -Force -ErrorAction SilentlyContinue
    $initialPortProbe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Parse("127.0.0.1"), 0)
    $initialPortProbe.Start()
    $initialHealthAddress = "127.0.0.1:$($initialPortProbe.LocalEndpoint.Port)"
    $initialPortProbe.Stop()
    Assert-LocalAliasOwnership
    Test-PortAvailable $initialHealthAddress -AllowOwnedRuntime
    Assert-True (-not (Test-Path -LiteralPath $identityPath)) "Initial validation created a runtime identity marker."
    $initialCalls = @()
    if (Test-Path -LiteralPath $callsPath) { $initialCalls = @(Get-Content -LiteralPath $callsPath) }
    Assert-True ($initialCalls.Count -eq 0) "Initial validation called tunnel-client connect or stop."
    '{ invalid marker' | Set-Content -LiteralPath $identityPath -Encoding UTF8
    $malformedMarkerRejected = $false
    try { Assert-LocalAliasOwnership; Test-PortAvailable $initialHealthAddress -AllowOwnedRuntime }
    catch { $malformedMarkerRejected = $_.Exception.Message -like "*identity marker is malformed*" }
    Assert-True $malformedMarkerRejected "An existing malformed identity marker was ignored."
    Remove-Item -LiteralPath $identityPath -Force
    Write-Output "PASS: initial validation accepts an empty official-shape alias list and free port without marker or runtime mutation"

    $identityEnv = Join-Path $scratchRoot "identity.env"
    $selectedConfig = Join-Path $scratchRoot "selected-plugin.env"
    Set-Content -LiteralPath $identityEnv -Value @(
        "AOITALK_BOOTSTRAP_ADMIN_USERNAME=fixture-user",
        "AOITALK_BOOTSTRAP_ADMIN_PASSWORD='literal-`${PASSWORD}-#-fixture'"
    ) -Encoding UTF8
    Set-Content -LiteralPath $selectedConfig -Value @(
        "AOITALK_BOOTSTRAP_ADMIN_USERNAME=",
        "AOITALK_BOOTSTRAP_ADMIN_PASSWORD=",
        "HOME_MCP_AOITALK_ENV_FILE=$identityEnv",
        "HOME_MCP_AOITALK_URL=http://127.0.0.1:34112",
        "HOME_MCP_AOITALK_TARGET_NODE_ID=11111111-2222-3333-4444-555555555555"
    ) -Encoding UTF8
    @'
@echo off
if /I not "%AOITALK_PLUGIN_CONFIG_FILE%"=="%AOITALK_TEST_SELECTED_CONFIG%" exit /b 41
if /I not "%HOME_MCP_AOITALK_ENV_FILE%"=="%AOITALK_TEST_SELECTED_IDENTITY%" exit /b 42
"%AOITALK_TEST_REAL_PYTHON%" -c "import json; from aoitalk_plugin.aoitalk import load_settings; settings=load_settings(); print(json.dumps({'base_url': settings.base_url, 'target_node_id': settings.target_node_id}))" >"%AOITALK_TEST_SETTINGS_OUTPUT%"
exit /b %ERRORLEVEL%
'@ | Set-Content -LiteralPath $pythonFixturePath -Encoding ASCII
    $connectPath = Join-Path $PSScriptRoot "connect_tunnel.ps1"
    $tokens = $null
    $parseErrors = $null
    $connectAst = [System.Management.Automation.Language.Parser]::ParseFile($connectPath, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count) { throw "Could not parse connect_tunnel.ps1 for the selected config fixture." }
    foreach ($functionName in @("Get-Setting", "Test-AoiTalkSettings", "Stop-AndVerifyOwnRuntime")) {
        $functionAst = $connectAst.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName }, $true)
        if (-not $functionAst) { throw "Could not find $functionName in connect_tunnel.ps1." }
        Invoke-Expression $functionAst.Extent.Text
    }
    $script:Config = @{ HOME_MCP_AOITALK_ENV_FILE = $identityEnv }
    $PackageRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    $script:PackageRoot = $PackageRoot
    $RepoRoot = [IO.Path]::GetFullPath((Join-Path $PackageRoot "..\.."))
    $script:RepoRoot = $RepoRoot
    $EnvFile = $selectedConfig
    $script:EnvFile = $selectedConfig
    $script:Python = $pythonFixturePath
    $script:RealPython = Join-Path $PackageRoot "venv\Scripts\python.exe"
    $script:SelectedConfig = $selectedConfig
    $script:SelectedIdentity = $identityEnv
    $script:ObservedSettingsJson = ""
    $env:AOITALK_TEST_SELECTED_CONFIG = $selectedConfig
    $env:AOITALK_TEST_SELECTED_IDENTITY = $identityEnv
    $env:AOITALK_TEST_SETTINGS_OUTPUT = $settingsOutputPath
    $env:AOITALK_TEST_REAL_PYTHON = $script:RealPython
    foreach ($name in @(
        "HOME_MCP_AOITALK_ENV_FILE", "AOITALK_PLUGIN_CONFIG_FILE",
        "AOITALK_BOOTSTRAP_ADMIN_USERNAME", "AOITALK_BOOTSTRAP_ADMIN_PASSWORD",
        "HOME_MCP_AOITALK_URL", "HOME_MCP_AOITALK_TARGET_NODE_ID"
    )) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
    $env:PYTHONPATH = $PackageRoot
    Test-AoiTalkSettings
    $script:ObservedSettingsJson = Get-Content -LiteralPath $settingsOutputPath -Raw -Encoding UTF8
    $selectedSettings = $script:ObservedSettingsJson | ConvertFrom-Json -ErrorAction Stop
    Assert-True ($selectedSettings.base_url -eq "http://127.0.0.1:34112") "Selected -EnvFile API URL did not reach settings validation."
    Assert-True ($selectedSettings.target_node_id -eq "11111111-2222-3333-4444-555555555555") "Selected -EnvFile target did not reach settings validation."
    $launcherText = [IO.File]::ReadAllText($connectPath)
    $configPassCount = [regex]::Matches($launcherText, 'SetEnvironmentVariable\("AOITALK_PLUGIN_CONFIG_FILE", \$EnvFile, "Process"\)').Count
    Assert-True ($configPassCount -eq 2) "The same selected -EnvFile must be passed to both validation and the runtime process."
    Write-Output "PASS: selected -EnvFile identity fallback, API URL, target, and validation/runtime propagation"

    $ownStatus = [ordered]@{
        alias = $runtimeAlias
        profile_dir = $profileRoot
        profile_name = $profileName
        profile_path = Join-Path $profileRoot $profileName
        tunnel_id = $tunnelId
        process_running = $true
        healthy = $true
        ready = $true
        ui_url = "http://127.0.0.1:58374"
        process = [ordered]@{
            alias = $runtimeAlias
            profile_dir = $profileRoot
            profile_name = $profileName
            profile_path = Join-Path $profileRoot $profileName
            pid = 4241
        }
    }
    [ordered]@{
        repo_root = $repoRoot
        entry = $launcherPath
        alias = $runtimeAlias
        profile = $profileName
        profile_dir = $profileRoot
        task_name = "AoiTalk Plugin 41"
    } | ConvertTo-Json | Set-Content -LiteralPath $identityPath -Encoding UTF8
    $ownStatus | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
    Assert-LocalAliasOwnership
    Test-PortAvailable "127.0.0.1:58374" -AllowOwnedRuntime
    Write-Output "PASS: own ready status, configured tunnel ID, and port allow validation"

    $busyListener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Parse("127.0.0.1"), 0)
    $busyListener.Start()
    try {
        $busyPort = $busyListener.LocalEndpoint.Port
        $busyPortRejected = $false
        try { Test-PortAvailable "127.0.0.1:$busyPort" }
        catch { $busyPortRejected = $_.Exception.Message -like "Dedicated tunnel health port *already in use*" }
        Assert-True $busyPortRejected "An occupied foreign port was not rejected."
    } finally { $busyListener.Stop() }
    Write-Output "PASS: occupied foreign loopback port is rejected"

    $processMismatch = $ownStatus | ConvertTo-Json -Depth 8 | ConvertFrom-Json
    $processMismatch.process.profile_name = "other-profile"
    $processMismatch | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
    Assert-OwnershipFailure { Assert-LocalAliasOwnership } "nested process profile"
    $tunnelMismatch = $ownStatus | ConvertTo-Json -Depth 8 | ConvertFrom-Json
    $tunnelMismatch.tunnel_id = "tunnel_other_fixture"
    $tunnelMismatch | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
    Assert-OwnershipFailure { Test-PortAvailable "127.0.0.1:58374" -AllowOwnedRuntime } "configured tunnel ID"
    Write-Output "PASS: nested process identity and configured tunnel ID mismatches fail"

    $otherStatus = $ownStatus | ConvertTo-Json -Depth 8 | ConvertFrom-Json
    $otherStatus.profile_dir = Join-Path $scratchRoot "other-profile"
    $otherStatus.profile_path = Join-Path $otherStatus.profile_dir "other"
    $otherStatus.process = [ordered]@{
        alias = $runtimeAlias
        profile_dir = $otherStatus.profile_dir
        profile_name = "other"
        profile_path = $otherStatus.profile_path
        pid = 5252
    }
    $otherStatus | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
    Remove-Item -LiteralPath $callsPath -Force -ErrorAction SilentlyContinue
    Assert-OwnershipFailure { Connect-OwnRuntime $tunnelId "HOME_MCP_AOITALK_RUNTIME_API_KEY" "python.exe" $profileRoot $script:TunnelLog } "connect"
    Assert-OwnershipFailure { Stop-OwnRuntime } "stop"
    $calls = @()
    if (Test-Path -LiteralPath $callsPath) { $calls = @(Get-Content -LiteralPath $callsPath) }
    Assert-True ($calls.Count -eq 0) "A stale own marker caused a connect or stop client call."
    Write-Output "PASS: stale marker plus same alias on another profile calls neither connect nor stop"

    $installPath = Join-Path $PSScriptRoot "install_autostart.ps1"
    $tokens = $null
    $parseErrors = $null
    $installAst = [System.Management.Automation.Language.Parser]::ParseFile($installPath, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count) { throw "Could not parse install_autostart.ps1 for the task fixture." }
    $taskFunction = $installAst.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq "Test-OwnTask" }, $true)
    if (-not $taskFunction) { throw "Could not find Test-OwnTask in install_autostart.ps1." }
    Invoke-Expression $taskFunction.Extent.Text
    $script:Launcher = $launcherPath
    $script:PowerShell = [IO.Path]::GetFullPath((Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"))
    $script:RepoRoot = $repoRoot
    $validTask = [pscustomobject]@{ Actions = @([pscustomobject]@{
        Execute = $script:PowerShell
        WorkingDirectory = $repoRoot
        Arguments = ('-NoProfile -NonInteractive -File "{0}" -Watch' -f $launcherPath)
    }) }
    $falsePositiveTask = [pscustomobject]@{ Actions = @([pscustomobject]@{
        Execute = $script:PowerShell
        WorkingDirectory = $repoRoot
        Arguments = ('-NoProfile -Command "-File {0} -Watch" -File "{1}" -Watch' -f $launcherPath, (Join-Path $scratchRoot "other.ps1"))
    }) }
    Assert-True (Test-OwnTask $validTask) "The task with the actual canonical -File value was not recognized."
    Assert-True (-not (Test-OwnTask $falsePositiveTask)) "A path in another task argument falsely satisfied -File ownership."
    Write-Output "PASS: Scheduled Task ownership checks the actual -File value"

    $ownStatus.process_running = $true
    $ownStatus.tunnel_id = $tunnelId
    $ownStatus | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
    Remove-Item -LiteralPath $callsPath -Force -ErrorAction SilentlyContinue
    $stopUnconfirmed = ""
    $stopOutput = @()
    try { $stopOutput = @(Stop-AndVerifyOwnRuntime -TimeoutSeconds 0) }
    catch { $stopUnconfirmed = $_.Exception.Message }
    $calls = @(Get-Content -LiteralPath $callsPath)
    Assert-True ($stopUnconfirmed -like "STOP_UNCONFIRMED:*" -and $calls.Count -eq 1 -and $calls[0] -eq "stop") `
        "A successful stop command with a still-running status was reported as stopped or retried the stop command."
    Assert-True (-not (($stopOutput -join "`n") -match '(?i)STOPPED|No running runtime')) `
        "An unconfirmed stop emitted a success message."
    Assert-True ($launcherText -notmatch '(?i)\bStop-Process\b|\btaskkill\b|\.Kill\s*\(|\.Terminate\s*\(') `
        "The launcher contains a force-kill path."
    Write-Output "PASS: persistent owned runtime reports STOP_UNCONFIRMED after one stop command without success output or force-kill"

    $script:StopTransitionStatus = $ownStatus | ConvertTo-Json -Depth 8 | ConvertFrom-Json
    $script:StopTransitionStatusCalls = 0
    function Invoke-StopTransitionFixture {
        function Get-ClientStatus {
            $script:StopTransitionStatusCalls++
            $status = $script:StopTransitionStatus | ConvertTo-Json -Depth 8 | ConvertFrom-Json
            $status.process_running = ($script:StopTransitionStatusCalls -eq 1)
            return $status
        }
        return (Stop-AndVerifyOwnRuntime -TimeoutSeconds 0)
    }
    Remove-Item -LiteralPath $callsPath -Force -ErrorAction SilentlyContinue
    $confirmedStopped = Invoke-StopTransitionFixture
    $calls = @(Get-Content -LiteralPath $callsPath)
    Assert-True ($confirmedStopped -and $script:StopTransitionStatusCalls -eq 2) `
        "A later canonical process_running=false status was not accepted as stopped."
    Assert-True ($calls.Count -eq 1 -and $calls[0] -eq "stop") `
        "A runtime that later reported stopped did not receive exactly one stop command."

    Remove-Item -LiteralPath $statusPath -Force
    Remove-Item -LiteralPath $callsPath -Force -ErrorAction SilentlyContinue
    $confirmedAbsent = Stop-AndVerifyOwnRuntime -TimeoutSeconds 0
    Assert-True $confirmedAbsent "An absent owned alias in the canonical registry was not accepted as stopped."
    Assert-True (-not (Test-Path -LiteralPath $callsPath)) "An absent runtime alias issued a stop command."
    Assert-True ($launcherText -match '(?s)if \(\$Stop\) \{\s*\$null = Stop-AndVerifyOwnRuntime\s*Write-Status \$null "stopped"\s*Write-Output "STOPPED:') `
        "The direct -Stop branch can display STOPPED before canonical stop verification."
    Write-Output "PASS: later process_running=false and absent owned alias canonically confirm STOPPED"

    $ownStatus.process_running = $true
    $ownStatus.tunnel_id = $tunnelId
    $ownStatus | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
    Remove-Item -LiteralPath $callsPath -Force -ErrorAction SilentlyContinue
    $stopped = Stop-OwnRuntime
    $calls = @(Get-Content -LiteralPath $callsPath)
    Assert-True $stopped "Verified runtime stop did not report success."
    Assert-True ($calls.Count -eq 1 -and $calls[0] -eq "stop") "Verified self runtime did not produce exactly one stop call."
    Write-Output "PASS: checkout/profile-owned runtime stop calls tunnel-client once without a key"

    $foreignIdStatus = $ownStatus | ConvertTo-Json -Depth 8 | ConvertFrom-Json
    $foreignIdStatus.tunnel_id = "tunnel_foreign_fixture"
    $foreignIdStatus | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
    Remove-Item -LiteralPath $callsPath -Force -ErrorAction SilentlyContinue
    $foreignIdBlocked = $false
    try { Stop-OwnRuntime }
    catch { $foreignIdBlocked = $_.Exception.Message -like "AoiTalk runtime ownership mismatch: tunnel-client status tunnel_id*" }
    $calls = @()
    if (Test-Path -LiteralPath $callsPath) { $calls = @(Get-Content -LiteralPath $callsPath) }
    Assert-True ($foreignIdBlocked -and $calls.Count -eq 0) "A foreign tunnel ID was stopped under the local alias marker."
    Write-Output "PASS: configured tunnel ID mismatch prevents runtime stop"
}
finally {
    Remove-Item Env:AOITALK_TEST_STATUS,Env:AOITALK_TEST_LIST,Env:AOITALK_TEST_CALLS,
        Env:AOITALK_TEST_SELECTED_CONFIG,Env:AOITALK_TEST_SELECTED_IDENTITY,
        Env:AOITALK_TEST_SETTINGS_OUTPUT,Env:AOITALK_TEST_REAL_PYTHON -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $scratchRoot -Recurse -Force -ErrorAction SilentlyContinue
}
