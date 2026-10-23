param(
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$PackageRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PackageRoot "..\.."))
$Launcher = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "connect_tunnel.ps1"))
$PowerShell = [IO.Path]::GetFullPath((Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"))
$TaskName = "AoiTalk Plugin 41"

if (-not (Test-Path -LiteralPath (Join-Path $PackageRoot ".env") -PathType Leaf)) {
    throw "Copy .env.example to plugins\AoiTalk_Plugin\.env and configure the issued AoiTalk tunnel first."
}

function Get-ExistingTask {
    return Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Test-OwnTask($Task) {
    if (-not $Task) { return $false }
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) { return $false }
    $action = $actions[0]
    $sameExecutable = $false
    $sameWorkingDirectory = $false
    try { $sameExecutable = [IO.Path]::GetFullPath([string]$action.Execute).Equals($PowerShell, [StringComparison]::OrdinalIgnoreCase) }
    catch { $sameExecutable = $false }
    try { $sameWorkingDirectory = [IO.Path]::GetFullPath([string]$action.WorkingDirectory).Equals($RepoRoot, [StringComparison]::OrdinalIgnoreCase) }
    catch { $sameWorkingDirectory = $false }
    $args = [string]$action.Arguments
    $sameEntry = $args.IndexOf($Launcher, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
                 $args -match '(?i)(?:^|\s)-Watch(?:\s|$)'
    return $sameExecutable -and $sameWorkingDirectory -and $sameEntry
}

$existing = Get-ExistingTask
if ($Unregister) {
    if (-not $existing) { Write-Output "No '$TaskName' task exists."; return }
    if (-not (Test-OwnTask $existing)) {
        throw "Task '$TaskName' belongs to another repository or entry; it was left untouched."
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Unregistered only '$TaskName'. The tunnel-client runtime was not stopped."
    return
}

# This is a validation-only launcher call. It checks settings, version and port,
# but does not connect to the tunnel or register/start a Task.
& $Launcher -ValidateOnly
if ($LASTEXITCODE -ne 0) { throw "AoiTalk Plugin configuration validation failed." }

if ($existing) {
    if (-not (Test-OwnTask $existing)) {
        throw "Task '$TaskName' belongs to another repository or entry; it was left untouched."
    }
    Write-Output "The matching '$TaskName' task is already registered. It was not restarted."
    return
}

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Watch' -f $Launcher
$action = New-ScheduledTaskAction -Execute $PowerShell -Argument $arguments -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$description = "Start and monitor the dedicated 41 AoiTalk Plugin Secure MCP Tunnel runtime."
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description $description | Out-Null
Write-Output "Registered '$TaskName' for the current user. Start it explicitly with Start-ScheduledTask -TaskName '$TaskName'."
