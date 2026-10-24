param([string]$EnsureWorkerRoot = "")

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\..\.."))
$ControlLibrary = Join-Path $PSScriptRoot "plugin_launch_control_lib.ps1"
$ControlScriptPath = Join-Path $PSScriptRoot "plugin_launch_control.ps1"
$TestScriptPath = [IO.Path]::GetFullPath($MyInvocation.MyCommand.Path)
. $ControlScriptPath
$script:OriginalWaitForWatcher = (Get-Item Function:Wait-ForWatcher).ScriptBlock
$script:RealGetCimInstance = Get-Command Get-CimInstance -CommandType Cmdlet

$script:FakeTask = $null
$script:FakeTaskProcess = $null
$script:SpawnLog = ""
$script:FixtureRoot = ""
$script:FixtureEnvFile = ""
$script:FixtureLauncher = ""
$script:FixtureControlScript = ""
$script:FixtureClient = ""
$script:PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

function Get-ScheduledTask {
    return $script:FakeTask
}

function Get-CimInstance {
    param([string]$ClassName, [string]$Filter, [string]$ErrorAction, [string]$ErrorVariable)
    if ($script:FakeTask -and -not $Filter -and $ClassName -eq "Win32_Process") { return $script:FakeTaskProcess }
    $parameters = @{ClassName=$ClassName}
    if ($Filter) { $parameters.Filter = $Filter }
    if ($ErrorAction) { $parameters.ErrorAction = $ErrorAction }
    if ($ErrorVariable) { $parameters.ErrorVariable = $ErrorVariable }
    return & $script:RealGetCimInstance @parameters
}

function Start-Process {
    param([string]$FilePath, [string]$ArgumentList, [string]$WorkingDirectory, [string]$WindowStyle, [switch]$PassThru)
    Add-Content -LiteralPath $script:SpawnLog -Value "spawn" -Encoding UTF8
    return Microsoft.PowerShell.Management\Start-Process -FilePath $FilePath -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -PassThru
}

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Test-WaitForWatcherNegativeCases {
    $script:WaitFixtureReceipt = $null
    $script:WaitFixtureOwnerMatches = $true
    $script:WaitFixtureIdentityMatches = $false
    $script:WaitFixtureIdentityCalls = 0
    $script:WaitFixtureSleepCalls = 0

    function Read-ControlJson {
        param([string]$Path)
        return $script:WaitFixtureReceipt
    }
    function Test-ControlReceiptOwner {
        param($Receipt)
        return $script:WaitFixtureOwnerMatches
    }
    function Test-ControlProcessIdentity {
        param($Receipt, [switch]$RequireWatchLock)
        $script:WaitFixtureIdentityCalls++
        return ($script:WaitFixtureIdentityMatches -and
            (-not $RequireWatchLock -or [bool]$Receipt.watch_lock_acquired))
    }
    function Start-Sleep {
        param([int]$Milliseconds)
        $script:WaitFixtureSleepCalls++
    }

    $nonce = [guid]::NewGuid().ToString("N")
    $receipt = [pscustomobject]@{
        instance_nonce = $nonce
        state = "watching"
        watch_lock_acquired = $true
    }
    $script:WaitFixtureReceipt = $receipt

    $pending = Wait-ForWatcher $nonce 0
    Assert-True ($null -eq $pending -and $script:WaitFixtureIdentityCalls -eq 1) `
        "A matching owner without verified process identity and watch lock was accepted."

    $script:WaitFixtureIdentityMatches = $true
    $started = Wait-ForWatcher $nonce 0
    Assert-True ([object]::ReferenceEquals($started, $receipt)) `
        "A matching owner, process identity, and watch lock did not return its receipt."

    $script:WaitFixtureOwnerMatches = $false
    $script:WaitFixtureIdentityCalls = 0
    $rejected = Wait-ForWatcher $nonce 0
    Assert-True ($null -eq $rejected -and $script:WaitFixtureIdentityCalls -eq 0) `
        "A receipt with foreign ownership was accepted or evaluated as an owned process."

    $script:WaitFixtureOwnerMatches = $true
    $script:WaitFixtureReceipt = [pscustomobject]@{
        instance_nonce = $nonce
        state = "launching"
        watch_lock_acquired = $false
    }
    $launching = Wait-ForWatcher $nonce 0
    Assert-True ($null -eq $launching) `
        "A launching receipt without an acquired watch lock was accepted as started."

    Write-Output "PASS: production Wait-ForWatcher requires owner, process identity, and acquired watch lock"
    Write-Output "PASS: launching receipt without watch lock is not accepted as started or ALREADY_RUNNING"
}

function Test-ProductionConnectControlEntry {
    $sourcePath = Join-Path $RepositoryRoot "plugins\AoiTalk_Plugin\scripts\connect_tunnel.ps1"
    $runtimeOwnershipPath = Join-Path $RepositoryRoot "plugins\AoiTalk_Plugin\scripts\runtime_ownership.ps1"
    $sourceText = [IO.File]::ReadAllText($sourcePath)
    $runtimeOwnershipText = [IO.File]::ReadAllText($runtimeOwnershipPath)
    Assert-True ($sourceText.Contains('$Entry = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "aoitalk_plugin_stdio.py"))')) `
        "The runtime marker entry no longer points at aoitalk_plugin_stdio.py."
    Assert-True ($runtimeOwnershipText.Contains('" -m aoitalk_plugin.stdio')) `
        "The runtime command no longer starts the stdio module."

    $tokens = $null
    $parseErrors = $null
    $sourceAst = [System.Management.Automation.Language.Parser]::ParseFile($sourcePath, [ref]$tokens, [ref]$parseErrors)
    Assert-True ($parseErrors.Count -eq 0) "connect_tunnel.ps1 did not parse for the control-entry fixture."
    $prefixStatements = @()
    $initializeFound = $false
    foreach ($statement in @($sourceAst.EndBlock.Statements)) {
        $prefixStatements += $statement.Extent.Text
        $initializeCommand = $statement.Find({
            param($node)
            $node -is [System.Management.Automation.Language.CommandAst] -and
                $node.GetCommandName() -eq "Initialize-PluginLaunchControl"
        }, $true)
        if ($initializeCommand) {
            $initializeFound = $true
            break
        }
    }
    Assert-True $initializeFound "connect_tunnel.ps1 did not expose its production control initializer."

    $runtimeTokens = $null
    $runtimeParseErrors = $null
    $runtimeAst = [System.Management.Automation.Language.Parser]::ParseFile($runtimeOwnershipPath, [ref]$runtimeTokens, [ref]$runtimeParseErrors)
    Assert-True ($runtimeParseErrors.Count -eq 0) "runtime_ownership.ps1 did not parse for the command fixture."
    $connectRuntimeFunction = $runtimeAst.Find({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq "Connect-OwnRuntime"
    }, $true)
    Assert-True ($null -ne $connectRuntimeFunction) "runtime_ownership.ps1 did not expose Connect-OwnRuntime for the command fixture."
    $markerAssignment = $sourceAst.Find({
        param($node)
        $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $node.Left.Extent.Text -eq '$marker'
    }, $true)
    $markerWrite = $sourceAst.Find({
        param($node)
        $node -is [System.Management.Automation.Language.PipelineAst] -and
            $node.Extent.Text -match '(?m)^\$marker\s*\|\s*ConvertTo-Json'
    }, $true)
    Assert-True ($null -ne $markerAssignment -and $null -ne $markerWrite) `
        "connect_tunnel.ps1 did not expose its production runtime marker statements."

    $fixtureRoot = Join-Path $script:FixtureRoot "production-control-entry"
    $fixtureScripts = Join-Path $fixtureRoot "plugins\AoiTalk_Plugin\scripts"
    New-Item -ItemType Directory -Force -Path $fixtureScripts | Out-Null
    Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $fixtureScripts "connect_tunnel.ps1") -Force
    Copy-Item -LiteralPath $ControlLibrary -Destination (Join-Path $fixtureScripts "plugin_launch_control_lib.ps1") -Force
    New-Item -ItemType File -Force -Path (Join-Path $fixtureScripts "aoitalk_plugin_stdio.py") | Out-Null

    $prefixPath = Join-Path $fixtureScripts "production-prefix.ps1"
    $markerSetup = @'
$TunnelId = "tunnel_fixture"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $IdentityPath) | Out-Null
'@
    $postfix = @'
$script:ControlInstanceNonce = [guid]::NewGuid().ToString("N")
$script:ControlOwnerMode = "console"
$script:ControlProcessKind = "watch-child"
$script:ControlProcessStartUtc = [DateTimeOffset]::UtcNow.ToString("o")
Set-ControlReceiptState "watching" $true
$receipt = Read-ControlJson $script:ControlReceiptPath
$productionControlEntry = [string]$script:ControlEntry
$expectedParentEntry = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "connect_tunnel.ps1"))
Initialize-PluginLaunchControl $RepoRoot $expectedParentEntry
$script:FixtureIdentityCommand = ('powershell.exe -File plugin_launch_control.ps1 -Mode WatchChild {0}' -f $script:ControlInstanceNonce)
function Get-ControlProcessStart([int]$ProcessId) {
    if ($ProcessId -eq $PID) { return $script:ControlProcessStartUtc }
    return $null
}
function Get-CimInstance {
    param([string]$ClassName, [string]$Filter, [string]$ErrorAction)
    return [pscustomobject]@{ CommandLine = $script:FixtureIdentityCommand }
}
$script:CapturedRuntimeArguments = @()
function Assert-LocalAliasOwnership { return $true }
function Invoke-NativeCapture {
    param([string]$Executable, [string[]]$ArgumentList)
    $script:CapturedRuntimeArguments = @($ArgumentList)
    return [pscustomobject]@{ ExitCode = 0; StdOut = @(); StdErr = @() }
}
$fixturePython = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
$script:TunnelClient = Join-Path $PSScriptRoot "fixture-tunnel-client.exe"
$script:RuntimeAlias = "aoitalk-41"
$script:ProfileName = "aoitalk-plugin-41"
Connect-OwnRuntime "tunnel_fixture" "HOME_MCP_AOITALK_RUNTIME_API_KEY" $fixturePython (Join-Path $PSScriptRoot "profiles") (Join-Path $PSScriptRoot "tunnel.log")
$runtimeCommand = ""
for ($index = 0; $index -lt $script:CapturedRuntimeArguments.Count; $index++) {
    if ([string]$script:CapturedRuntimeArguments[$index] -eq "--mcp-command" -and $index + 1 -lt $script:CapturedRuntimeArguments.Count) {
        $runtimeCommand = [string]$script:CapturedRuntimeArguments[$index + 1]
        break
    }
}
$markerOnDisk = Get-Content -LiteralPath $IdentityPath -Raw -Encoding UTF8 | ConvertFrom-Json
$foreignEntry = $receipt | ConvertTo-Json -Depth 12 | ConvertFrom-Json
$foreignEntry.entry = Join-Path $PSScriptRoot "foreign-entry.ps1"
$foreignNonce = $receipt | ConvertTo-Json -Depth 12 | ConvertFrom-Json
$foreignNonce.instance_nonce = [guid]::NewGuid().ToString("N")
$foreignPid = $receipt | ConvertTo-Json -Depth 12 | ConvertFrom-Json
$foreignPid.pid = $PID + 100000
[ordered]@{
    receipt_entry = [string]$receipt.entry
    production_control_entry = $productionControlEntry
    parent_entry = [string]$script:ControlEntry
    runtime_marker_entry = [string]$markerOnDisk.entry
    runtime_command = $runtimeCommand
    parent_owner_pass = [bool](Test-ControlReceiptOwner $receipt)
    parent_identity_pass = [bool](Test-ControlProcessIdentity $receipt)
    foreign_entry_rejected = -not (Test-ControlReceiptOwner $foreignEntry)
    foreign_nonce_rejected = -not (Test-ControlProcessIdentity $foreignNonce)
    foreign_pid_rejected = -not (Test-ControlProcessIdentity $foreignPid)
} | ConvertTo-Json -Compress
'@
    [IO.File]::WriteAllText(
        $prefixPath,
        (($prefixStatements -join [Environment]::NewLine) + [Environment]::NewLine +
            $connectRuntimeFunction.Extent.Text + [Environment]::NewLine + $markerSetup + [Environment]::NewLine +
            $markerAssignment.Extent.Text + [Environment]::NewLine + $markerWrite.Extent.Text + [Environment]::NewLine + $postfix),
        (New-Object System.Text.UTF8Encoding($false))
    )

    $currentShell = if ($PSVersionTable.PSEdition -eq "Core") {
        Join-Path $PSHOME "pwsh.exe"
    } else {
        Join-Path $PSHOME "powershell.exe"
    }
    Assert-True (Test-Path -LiteralPath $currentShell -PathType Leaf) "Could not locate the current PowerShell executable for the isolated fixture."
    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = $currentShell
    $processInfo.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $prefixPath
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $processInfo
    [void]$process.Start()
    $process.WaitForExit()
    $output = $process.StandardOutput.ReadToEnd() + $process.StandardError.ReadToEnd()
    Assert-True ($process.ExitCode -eq 0) ("Production control-entry fixture failed: " + $output)
    $lines = @($output -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    Assert-True ($lines.Count -gt 0) "Production control-entry fixture produced no result."
    try { $result = $lines[-1] | ConvertFrom-Json -ErrorAction Stop }
    catch { throw "Production control-entry fixture did not emit JSON: $output" }

    $expectedConnect = [IO.Path]::GetFullPath((Join-Path $fixtureScripts "connect_tunnel.ps1"))
    $expectedStdio = [IO.Path]::GetFullPath((Join-Path $fixtureScripts "aoitalk_plugin_stdio.py"))
    $expectedPython = (Join-Path $fixtureScripts "venv\Scripts\python.exe").Replace('\', '/')
    $expectedRuntimeCommand = '"{0}" -m aoitalk_plugin.stdio' -f $expectedPython
    Assert-True ([IO.Path]::GetFullPath([string]$result.receipt_entry).Equals($expectedConnect, [StringComparison]::OrdinalIgnoreCase)) `
        "The production connect initializer wrote a non-connect control receipt entry."
    Assert-True ([IO.Path]::GetFullPath([string]$result.production_control_entry).Equals($expectedConnect, [StringComparison]::OrdinalIgnoreCase)) `
        "The production control entry variable is not the isolated connect_tunnel.ps1 path."
    Assert-True ([IO.Path]::GetFullPath([string]$result.parent_entry).Equals($expectedConnect, [StringComparison]::OrdinalIgnoreCase)) `
        "The parent control entry did not resolve to the isolated connect_tunnel.ps1 path."
    Assert-True ([IO.Path]::GetFullPath([string]$result.runtime_marker_entry).Equals($expectedStdio, [StringComparison]::OrdinalIgnoreCase)) `
        "The production runtime marker entry was changed away from aoitalk_plugin_stdio.py."
    Assert-True ([string]$result.runtime_command -eq $expectedRuntimeCommand) `
        "The production runtime command was changed away from aoitalk_plugin.stdio."
    Assert-True ([bool]$result.parent_owner_pass -and [bool]$result.parent_identity_pass) `
        "The production control receipt did not pass its owner and process identity checks."
    Assert-True ([bool]$result.foreign_entry_rejected -and [bool]$result.foreign_nonce_rejected -and [bool]$result.foreign_pid_rejected) `
        "A foreign control receipt entry, nonce, or PID was accepted."
    Write-Output "PASS: production connect initializer writes connect receipt entry while runtime remains stdio; owner and foreign identity checks hold"
}

function Initialize-Fixture([string]$Root, [switch]$Existing) {
    $script:FixtureRoot = [IO.Path]::GetFullPath($Root)
    $package = Join-Path $script:FixtureRoot "plugins\AoiTalk_Plugin"
    $scripts = Join-Path $package "scripts"
    $script:FixtureLauncher = Join-Path $scripts "connect_tunnel.ps1"
    $script:FixtureControlScript = Join-Path $scripts "plugin_launch_control.ps1"
    $script:FixtureEnvFile = Join-Path $package ".env"
    $script:FixtureClient = Join-Path $package ".tools\tunnel-client.exe"
    if (-not $Existing) {
        New-Item -ItemType Directory -Force -Path $scripts,(Join-Path $package "venv\Scripts"),(Join-Path $package ".tools") | Out-Null
        New-Item -ItemType Directory -Force -Path (Join-Path $script:FixtureRoot "data\aoitalk_plugin"),(Join-Path $script:FixtureRoot "logs\aoitalk_plugin") | Out-Null
        New-Item -ItemType File -Force -Path $script:FixtureLauncher,(Join-Path $package "venv\Scripts\python.exe"),$script:FixtureClient | Out-Null
        Set-Content -LiteralPath $script:FixtureEnvFile -Value @(
            "HOME_MCP_AOITALK_TUNNEL_ID=tunnel_fixture_41",
            "HOME_MCP_AOITALK_RUNTIME_API_KEY=fixture-only-value",
            "TUNNEL_CLIENT_PATH=$script:FixtureClient"
        ) -Encoding UTF8

        $fakeLauncher = @'
param(
    [switch]$Stop,
    [switch]$ControlInternal,
    [string]$EnvFile
)
$ErrorActionPreference = "Stop"
if (-not $Stop -or -not $ControlInternal) { throw "Fixture launcher received an unexpected command." }
Write-Output "STOPPING: fixture launcher emitted its first state line."
if ($env:AOITALK_LAUNCH_FIXTURE_STOP_ERROR -eq "1") { throw "Fixture launcher terminating stop error." }
Write-Output "STOPPED: fixture launcher emitted its second state line."
'@
        Set-Content -LiteralPath $script:FixtureLauncher -Value $fakeLauncher -Encoding UTF8

        $fakeWatcher = @'
param([string]$Mode, [string]$InstanceNonce, [string]$EnvFile)
$ErrorActionPreference = "Stop"
$repo = $env:AOITALK_LAUNCH_FIXTURE_ROOT
if ($env:AOITALK_LAUNCH_FIXTURE_NO_RECEIPT -eq "1") {
    $release = Join-Path $repo "logs\aoitalk_plugin\release.fixture"
    while (-not (Test-Path -LiteralPath $release)) { Start-Sleep -Milliseconds 100 }
    exit 0
}
$entry = Join-Path $repo "plugins\AoiTalk_Plugin\scripts\connect_tunnel.ps1"
. $env:AOITALK_LAUNCH_CONTROL_LIBRARY
Initialize-PluginLaunchControl $repo $entry
$script:ControlInstanceNonce = $InstanceNonce
$script:ControlOwnerMode = "console"
$script:ControlProcessKind = "watch-child"
$script:ControlProcessStartUtc = Get-ControlProcessStart $PID
$script:ControlOwnerMode = "console"
$lock = [IO.File]::Open($script:ControlWatchLockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
Set-ControlReceiptState "watching" $true
Add-Content -LiteralPath (Join-Path $script:ControlLogRoot "events.txt") -Value "started" -Encoding UTF8
try {
    while ($true) {
        $request = Get-ControlStopRequest
        if ($request) {
            Set-ControlStopState $request "received"
            if ($env:AOITALK_LAUNCH_FIXTURE_IGNORE_STOP -eq "1") {
                while (-not (Test-Path -LiteralPath (Join-Path $script:ControlLogRoot "release.fixture"))) { Start-Sleep -Milliseconds 100 }
                Set-ControlReceiptState "exited" $false
                $lock.Dispose()
                exit 0
            }
            Add-Content -LiteralPath (Join-Path $script:ControlLogRoot "runtime-stops.txt") -Value "stop" -Encoding UTF8
            Set-ControlStopState $request "stopped"
            Set-ControlReceiptState "exited" $false
            $lock.Dispose()
            exit 0
        }
        if (Test-Path -LiteralPath (Join-Path $script:ControlLogRoot "release.fixture")) {
            Set-ControlReceiptState "exited" $false
            $lock.Dispose()
            exit 0
        }
        Add-Content -LiteralPath (Join-Path $script:ControlLogRoot "reconnects.txt") -Value "check" -Encoding UTF8
        Start-Sleep -Milliseconds 200
    }
} finally { if ($lock) { $lock.Dispose() } }
'@
        Set-Content -LiteralPath $script:FixtureControlScript -Value $fakeWatcher -Encoding UTF8
    }
    $script:SpawnLog = Join-Path $script:FixtureRoot "spawns.txt"
    $script:PackageRoot = $package
    $script:RepoRoot = $script:FixtureRoot
    $script:Launcher = $script:FixtureLauncher
    $script:ControlScript = $script:FixtureControlScript
    $script:EnvFile = $script:FixtureEnvFile
    $script:PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    Initialize-PluginLaunchControl $script:RepoRoot $script:Launcher
    $env:AOITALK_LAUNCH_FIXTURE_ROOT = $script:FixtureRoot
    $env:AOITALK_LAUNCH_CONTROL_LIBRARY = $ControlLibrary
    $env:TUNNEL_CLIENT_PATH = ""
    Remove-Item Env:AOITALK_LAUNCH_FIXTURE_IGNORE_STOP -ErrorAction SilentlyContinue
    Remove-Item Env:AOITALK_LAUNCH_FIXTURE_STOP_ERROR -ErrorAction SilentlyContinue
}

function Start-HiddenPowerShell([string]$Arguments, [string]$FilePath = $script:PowerShell) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = $Arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    [void]$process.Start()
    return $process
}

function Test-StopModeProcessExit {
    $modeStopRoot = Join-Path $script:FixtureRoot "mode-stop"
    $modeStopPackage = Join-Path $modeStopRoot "plugins\AoiTalk_Plugin"
    $modeStopScripts = Join-Path $modeStopPackage "scripts"
    $modeStopControl = Join-Path $modeStopScripts "plugin_launch_control.ps1"
    $modeStopLauncher = Join-Path $modeStopScripts "connect_tunnel.ps1"
    $modeStopEnv = Join-Path $modeStopPackage ".env"
    New-Item -ItemType Directory -Force -Path $modeStopScripts | Out-Null
    Copy-Item -LiteralPath $ControlScriptPath -Destination $modeStopControl -Force
    Copy-Item -LiteralPath $ControlLibrary -Destination (Join-Path $modeStopScripts "plugin_launch_control_lib.ps1") -Force
    Copy-Item -LiteralPath $script:FixtureLauncher -Destination $modeStopLauncher -Force
    Copy-Item -LiteralPath $script:FixtureEnvFile -Destination $modeStopEnv -Force

    $currentShell = if ($PSVersionTable.PSEdition -eq "Core") {
        Join-Path $PSHOME "pwsh.exe"
    } else {
        Join-Path $PSHOME "powershell.exe"
    }
    Assert-True (Test-Path -LiteralPath $currentShell -PathType Leaf) "Could not locate the current PowerShell executable for the isolated stop fixture."
    $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Mode Stop -EnvFile "{1}"' -f $modeStopControl,$modeStopEnv

    $normalProcess = Start-HiddenPowerShell $arguments $currentShell
    $normalProcess.WaitForExit()
    $normalOutput = $normalProcess.StandardOutput.ReadToEnd() + $normalProcess.StandardError.ReadToEnd()
    Assert-True ($normalProcess.ExitCode -eq 0) ("Mode Stop did not preserve the scalar success exit: " + $normalOutput)
    Assert-True ($normalOutput.Contains("STOPPING: fixture launcher emitted its first state line.") -and
        $normalOutput.Contains("STOPPED: fixture launcher emitted its second state line.")) `
        "Mode Stop did not keep the fake Launcher's state lines human-visible."

    $env:AOITALK_LAUNCH_FIXTURE_STOP_ERROR = "1"
    try {
        $errorProcess = Start-HiddenPowerShell $arguments $currentShell
        $errorProcess.WaitForExit()
        $errorOutput = $errorProcess.StandardOutput.ReadToEnd() + $errorProcess.StandardError.ReadToEnd()
    } finally {
        Remove-Item Env:AOITALK_LAUNCH_FIXTURE_STOP_ERROR -ErrorAction SilentlyContinue
    }
    Assert-True ($errorProcess.ExitCode -ne 0) ("A terminating fake Launcher error produced a zero exit: " + $errorOutput)
    Write-Output "PASS: Mode Stop preserves exit 0 after multiple Launcher state lines and returns nonzero for a terminating Launcher error"
}

if ($EnsureWorkerRoot) {
    Initialize-Fixture $EnsureWorkerRoot -Existing
    $result = Invoke-Ensure
    exit [int]$result
}

$scratchRoot = Join-Path $RepositoryRoot (".local\plugin-launch-control-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $scratchRoot | Out-Null
$watcherPid = 0
try {
    Test-WaitForWatcherNegativeCases
    Initialize-Fixture $scratchRoot
    Test-ProductionConnectControlEntry

    $missingEnv = Join-Path $script:FixtureRoot "plugins\AoiTalk_Plugin\missing.env"
    $script:EnvFile = $missingEnv
    $notConfigured = Invoke-Ensure
    Assert-True ($notConfigured -eq 2 -and -not (Test-Path -LiteralPath $script:SpawnLog)) "An unconfigured Plugin must report NOT_CONFIGURED without spawning a watcher."
    $script:EnvFile = $script:FixtureEnvFile
    Write-Output "PASS: missing Plugin configuration is a non-blocking NOT_CONFIGURED result"

    $oldInstance = [guid]::NewGuid().ToString("N")
    Write-ControlJson $script:ControlStopRequestPath ([ordered]@{instance_nonce=$oldInstance;request_id="stale-request"})
    $workerArgs = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -EnsureWorkerRoot "{1}"' -f $TestScriptPath,$scratchRoot
    $first = Start-HiddenPowerShell $workerArgs
    $second = Start-HiddenPowerShell $workerArgs
    $first.WaitForExit()
    $second.WaitForExit()
    $firstOutput = $first.StandardOutput.ReadToEnd() + $first.StandardError.ReadToEnd()
    $secondOutput = $second.StandardOutput.ReadToEnd() + $second.StandardError.ReadToEnd()
    Assert-True ($first.ExitCode -eq 0 -and $second.ExitCode -eq 0) ("Concurrent ensure failed: " + $firstOutput + $secondOutput)
    $spawns = @(Get-Content -LiteralPath $script:SpawnLog)
    Assert-True ($spawns.Count -eq 1) "Concurrent ensure started more than one watcher process."
    $receipt = Read-ControlJson $script:ControlReceiptPath
    Assert-True ($receipt -and $receipt.state -eq "watching" -and $receipt.watch_lock_acquired) "The first start did not produce a verified watcher receipt and lock."
    Assert-True ($receipt.mode -eq "console" -and -not (Select-String -LiteralPath $script:ControlReceiptPath -Pattern 'fixture-only-value')) "Watcher identity metadata contains a credential or wrong launch mode."
    $watcherPid = [int]$receipt.pid
    Write-Output "PASS: concurrent first ensure starts one isolated watcher; stale stop nonce is ignored"

    $testTask = [pscustomobject]@{Actions=@([pscustomobject]@{
        Execute=$script:PowerShell;WorkingDirectory=$script:RepoRoot;
        Arguments=('-NoProfile -NonInteractive -File "{0}" -Watch' -f $script:Launcher)
    })}
    Assert-True (Test-OwnTaskDefinition $testTask) "The exact dedicated Task action was not recognized."
    $foreignTask = [pscustomobject]@{Actions=@([pscustomobject]@{
        Execute=$script:PowerShell;WorkingDirectory=$script:RepoRoot;
        Arguments='-NoProfile -Command "-File other.ps1 -Watch" -File connect_tunnel.ps1 -Watch'
    })}
    Assert-True (-not (Test-OwnTaskDefinition $foreignTask)) "A foreign task command accidentally matched the Plugin watcher."

    $verified = Get-VerifiedControlWatcher
    $receiptDescription = ConvertTo-Json -InputObject $receipt -Compress -Depth 8
    $ownerCheck = Test-ControlReceiptOwner $receipt
    $processCheck = Test-ControlProcessIdentity $receipt
    $lockCheck = Test-ControlProcessIdentity $receipt -RequireWatchLock
    $processDetails = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f [int]$receipt.pid)
    $command = [string]$processDetails.CommandLine
    $startMatches = (Get-ControlStartTicks (Get-ControlProcessStart ([int]$receipt.pid))) -eq (Get-ControlStartTicks $receipt.process_start_utc)
    $pathMatches = $command -match '(?i)plugin_launch_control\.ps1'
    $modeMatches = $command -match '(?i)-Mode\s+WatchChild'
    $nonceMatches = $command -match [regex]::Escape([string]$receipt.instance_nonce)
    Assert-True ($verified -and $verified.instance_nonce -eq $receipt.instance_nonce) ("The live fake watcher lost its process, receipt, or lock identity before stop. Receipt=" + $receiptDescription + "; owner=" + $ownerCheck + "; process=" + $processCheck + "; locked=" + $lockCheck + "; startMatch=" + $startMatches + "; pathMatch=" + $pathMatches + "; modeMatch=" + $modeMatches + "; nonceMatch=" + $nonceMatches + "; root=" + $script:ControlRepoRoot + "; entry=" + $script:ControlEntry + "; start=" + (Get-ControlProcessStart ([int]$receipt.pid)) + "; command=" + $command)

    $result = Invoke-Stop
    Assert-True ($result -eq 0) "A normal stop request was not acknowledged."
    $stops = @(Get-Content -LiteralPath (Join-Path $script:ControlLogRoot "runtime-stops.txt"))
    Assert-True ($stops.Count -eq 1) "Normal stop did not stop its runtime exactly once."
    $reconnectCount = @(Get-Content -LiteralPath (Join-Path $script:ControlLogRoot "reconnects.txt")).Count
    Start-Sleep -Milliseconds 500
    Assert-True (@(Get-Content -LiteralPath (Join-Path $script:ControlLogRoot "reconnects.txt")).Count -eq $reconnectCount) "The watcher retried after stop acknowledgement."
    $watcherPid = 0
    Write-Output "PASS: stop request is instance-bound, stops once, acknowledges, exits, and does not reconnect"

    $result = Invoke-Stop
    Assert-True (($result -is [int]) -and $result -eq 0) "A no-watcher stop returned Launcher output instead of scalar Int32 0."
    Assert-True (-not (Test-Path -LiteralPath $script:ControlReceiptPath)) "A completed stale watcher receipt remained after the no-watcher stop."
    Write-Output "PASS: no-watcher stop consumes multiple Launcher state lines and returns scalar Int32 0 after stale cleanup"
    Test-StopModeProcessExit

    $script:StopWaitSeconds = 2
    $env:AOITALK_LAUNCH_FIXTURE_NO_RECEIPT = "1"
    function Wait-ForWatcher([string]$Nonce, [int]$TimeoutSeconds = 25) { return $null }
    $beforeUncertainSpawns = @(Get-Content -LiteralPath $script:SpawnLog).Count
    $result = Invoke-Ensure
    Assert-True ($result -eq 1) "A launch without a watcher receipt did not return launch_uncertain."
    $uncertainRequest = Read-ControlJson $script:ControlLaunchRequestPath
    $watcherPid = [int]$uncertainRequest.pid
    $uncertainBlocked = $false
    try { Invoke-Ensure | Out-Null }
    catch { $uncertainBlocked = $_.Exception.Message -like "LAUNCH_UNCERTAIN:*" }
    $afterUncertainSpawns = @(Get-Content -LiteralPath $script:SpawnLog).Count
    Assert-True ($uncertainBlocked -and $beforeUncertainSpawns + 1 -eq $afterUncertainSpawns) "An unknown launch result caused a duplicate spawn."
    Set-Content -LiteralPath (Join-Path $script:ControlLogRoot "release.fixture") -Value "release" -Encoding UTF8
    $until = [DateTime]::UtcNow.AddSeconds(10)
    while ((Get-ControlProcessStart $watcherPid) -and [DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 100 }
    Assert-True (-not (Get-ControlProcessStart $watcherPid)) "The launch-uncertain fake child did not exit after test release."
    $watcherPid = 0
    Remove-Item -LiteralPath (Join-Path $script:ControlLogRoot "release.fixture") -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $script:ControlLaunchRequestPath -Force -ErrorAction SilentlyContinue
    Remove-Item Function:Wait-ForWatcher -Force
    Set-Item Function:Wait-ForWatcher -Value $script:OriginalWaitForWatcher
    Remove-Item Env:AOITALK_LAUNCH_FIXTURE_NO_RECEIPT -ErrorAction SilentlyContinue
    Write-Output "PASS: launch timeout remains uncertain and blocks duplicate spawn"

    $script:StopWaitSeconds = 2
    $env:AOITALK_LAUNCH_FIXTURE_IGNORE_STOP = "1"
    $result = Invoke-Ensure
    Assert-True ($result -eq 0) "A new ensure after a stopped instance did not start."
    $receipt = Read-ControlJson $script:ControlReceiptPath
    $watcherPid = [int]$receipt.pid
    $result = Invoke-Stop
    Assert-True ($result -eq 3) "A stop timeout did not return STOP_UNCONFIRMED."
    $stillAlive = $null -ne (Get-ControlProcessStart $watcherPid)
    Assert-True $stillAlive "Stop timeout force-killed or lost the watcher process."
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $script:ControlLogRoot "runtime-stops.txt")) -or
        @(Get-Content -LiteralPath (Join-Path $script:ControlLogRoot "runtime-stops.txt")).Count -eq 1) "Stop timeout invoked runtime stop more than once."
    Set-Content -LiteralPath (Join-Path $script:ControlLogRoot "release.fixture") -Value "release" -Encoding UTF8
    $until = [DateTime]::UtcNow.AddSeconds(10)
    while ((Get-ControlProcessStart $watcherPid) -and [DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 100 }
    Assert-True (-not (Get-ControlProcessStart $watcherPid)) "The fake watcher did not exit after its test-only graceful release."
    $watcherPid = 0
    Write-Output "PASS: stop timeout reports unconfirmed and does not kill the watcher"

    $reusedIdentity = [pscustomobject]@{pid=$PID;process_start_utc=[DateTimeOffset]::UtcNow.AddDays(-1).ToString("o")}
    Assert-True (Test-ControlIdentityAbsent $reusedIdentity) "A PID reused with a different process start time was treated as the old watcher."
    Write-Output "PASS: PID reuse is separated by process start time"

    $script:FakeTask = [pscustomobject]@{State="Running";Actions=$testTask.Actions}
    $script:FakeTaskProcess = [pscustomobject]@{
        Name="powershell.exe";ProcessId=4241;
        CommandLine=('powershell.exe -File "{0}" -Watch' -f $script:Launcher)
    }
    $taskProcess = Get-RunningOwnTaskProcess
    Assert-True ($taskProcess -and [int]$taskProcess.ProcessId -eq 4241) "The verified background Task process was not recognized."
    $beforeTaskSpawns = @(Get-Content -LiteralPath $script:SpawnLog).Count
    $result = Invoke-Ensure
    $afterTaskSpawns = @(Get-Content -LiteralPath $script:SpawnLog).Count
    Assert-True ($result -eq 0 -and $beforeTaskSpawns -eq $afterTaskSpawns) "An active verified Task watcher caused a second console process."
    Write-Output "PASS: verified background Task owner suppresses a second window"

    $output = [IO.File]::ReadAllText((Join-Path $RepositoryRoot "run.bat"))
    $pluginCallCount = [regex]::Matches($output, '(?im)^call\s+"%~dp0run_plugin\.bat"\s*$').Count
    $startIndex = $output.IndexOf(":start", [StringComparison]::OrdinalIgnoreCase)
    $pluginIndex = $output.IndexOf('call "%~dp0run_plugin.bat"', [StringComparison]::OrdinalIgnoreCase)
    Assert-True ($pluginCallCount -eq 1 -and $pluginIndex -ge 0 -and $pluginIndex -lt $startIndex) "run.bat must call run_plugin.bat exactly once before :start."
    Assert-True ($output -notmatch '(?im)^\s*call\s+"%~dp0run_plugin\.bat".*:start') "run.bat can relaunch Plugin from the exit-42 loop."
    Assert-True ($output -notmatch '(?im)^\s*call\s+"%~dp0run_plugin\.bat"\s+--stop') "Normal AoiTalk shutdown must not stop the independent Plugin watcher."
    Assert-True ($output.Contains('if not "%AOITALK_PLUGIN_LAUNCH_EXIT%"=="0"') -and
        -not $output.Contains('exit /b %AOITALK_PLUGIN_LAUNCH_EXIT%')) "Plugin launch failure must not become the AoiTalk process exit code."
    $productionHelper = [IO.File]::ReadAllText($ControlScriptPath)
    $productionLauncher = [IO.File]::ReadAllText($ControlScriptPath.Replace("plugin_launch_control.ps1", "connect_tunnel.ps1"))
    $runPluginBatch = [IO.File]::ReadAllText((Join-Path $RepositoryRoot "run_plugin.bat"))
    Assert-True ($productionHelper -match '-WindowStyle Normal' -and $productionHelper -match 'WindowTitle\s*=\s*"AoiTalk Plugin"') "The production watcher must open one titled visible console."
    Assert-True ($productionLauncher.Contains('AoiTalk Plugin tunnel runtime is healthy and ready') -and $productionLauncher.Contains('if (-not $Watch)')) "Non-Watch direct connect must keep its ready-and-return path."
    Assert-True ($productionLauncher.Contains('plugin_launch_control.ps1") -Mode WatchCurrent') -and $productionLauncher.Contains('plugin_launch_control.ps1") -Mode Stop')) "Direct -Watch and -Stop must share the launcher control contract."
    Assert-True ($runPluginBatch.Contains('[--stop]') -and $runPluginBatch.Contains('exit /b %ERRORLEVEL%')) "run_plugin.bat must expose only the ensure/--stop entrance and preserve exit codes."
    Assert-True (-not $productionHelper.Contains('Register-ScheduledTask') -and -not $productionHelper.Contains('Start-ScheduledTask')) "The launch path must not register or start a Task."
    Write-Output "PASS: run.bat entry is outside :start and exit-42 loop"
}
finally {
    if ($watcherPid -le 0) {
        $lastReceipt = Read-ControlJson $script:ControlReceiptPath
        if ($lastReceipt) { $watcherPid = [int]$lastReceipt.pid }
    }
    if ($watcherPid -gt 0) {
        try { Set-Content -LiteralPath (Join-Path $script:ControlLogRoot "release.fixture") -Value "release" -Encoding UTF8 } catch { }
        $until = [DateTime]::UtcNow.AddSeconds(10)
        while ((Get-ControlProcessStart $watcherPid) -and [DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 100 }
    }
    Remove-Item Env:AOITALK_LAUNCH_FIXTURE_ROOT,Env:AOITALK_LAUNCH_CONTROL_LIBRARY,Env:AOITALK_LAUNCH_FIXTURE_IGNORE_STOP,Env:AOITALK_LAUNCH_FIXTURE_NO_RECEIPT,Env:AOITALK_LAUNCH_FIXTURE_STOP_ERROR,Env:TUNNEL_CLIENT_PATH -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $scratchRoot -Recurse -Force -ErrorAction SilentlyContinue
}
