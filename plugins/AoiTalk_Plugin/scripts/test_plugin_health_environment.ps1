$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\..\.."))
$sourcePath = Join-Path $PSScriptRoot "connect_tunnel.ps1"
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sourcePath, [ref]$tokens, [ref]$parseErrors)
Assert-True ($parseErrors.Count -eq 0) "Production launcher did not parse."
$productionTry = @($ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.TryStatementAst] })[-1]
$statements = @($productionTry.Body.Statements)
$environmentStart = -1
$environmentEnd = -1
for ($index = 0; $index -lt $statements.Count; $index++) {
    if ($statements[$index].Extent.Text -match '^\$envNames\s*=') { $environmentStart = $index }
    if ($environmentStart -ge 0 -and $statements[$index].Extent.Text -match '^\$script:StateLock\s*=') {
        $environmentEnd = $index - 1
        break
    }
}
Assert-True ($environmentStart -ge 0 -and $environmentEnd -ge $environmentStart) "Production environment setup was not found."
$environmentSetup = [scriptblock]::Create(($statements[$environmentStart..$environmentEnd] | ForEach-Object { $_.Extent.Text }) -join "`n")
$restore = @($productionTry.Finally.Statements | Where-Object { $_.Extent.Text -match '^foreach\s*\(\$name in \$SavedEnvironment.Keys\)' })
Assert-True ($restore.Count -eq 1) "Production finally environment restoration was not found."
$environmentRestore = [scriptblock]::Create($restore[0].Extent.Text)
$readyBranch = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
        $node.Extent.Text.StartsWith('if ($status -and $status.process_running -and $status.healthy -and $status.ready)')
}, $true)
Assert-True ($null -ne $readyBranch) "Official status readiness branch was not found."
$writeStatus = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq "Write-Status"
}, $true)
Assert-True ($null -ne $writeStatus) "Production status writer was not found."
. ([scriptblock]::Create($writeStatus.Extent.Text))

$scratchRoot = Join-Path $repositoryRoot (".local\plugin-health-environment-" + [guid]::NewGuid().ToString("N"))
$originalHealthFile = [Environment]::GetEnvironmentVariable("HEALTH_URL_FILE", "Process")
$originalHealthAddress = [Environment]::GetEnvironmentVariable("HEALTH_LISTEN_ADDR", "Process")
try {
    $fixtureScripts = Join-Path $scratchRoot "plugins\AoiTalk_Plugin\scripts"
    New-Item -ItemType Directory -Force -Path $fixtureScripts | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "plugin_launch_control_lib.ps1") -Destination $fixtureScripts

    # Execute the production initializer in an isolated checkout-shaped directory.
    $prefix = @()
    foreach ($statement in $ast.EndBlock.Statements) {
        $prefix += $statement.Extent.Text
        if ($statement.Extent.Text -match '^Initialize-PluginLaunchControl\s') { break }
    }
    $prefixPath = Join-Path $fixtureScripts "prefix.ps1"
    [IO.File]::WriteAllText($prefixPath, ($prefix -join "`n"), (New-Object Text.UTF8Encoding($false)))
    . $prefixPath
    Assert-True ($ControlEntry -eq (Join-Path $fixtureScripts "connect_tunnel.ps1")) "Control entry is not canonical connect."
    Assert-True ($Entry -eq (Join-Path $fixtureScripts "aoitalk_plugin_stdio.py")) "Runtime entry is not stdio."

    function Get-Setting([string]$Name, [string]$Default = "") { return $Default }
    $RuntimeKey = "fixture-only-value"
    $Stop = $false
    $HealthAddress = "127.0.0.1:8783"
    $EnvFile = Join-Path $PackageRoot ".env"
    $script:StatusPath = Join-Path $scratchRoot "status.json"
    $childPath = Join-Path $scratchRoot "child.ps1"
    $childSource = @'
param([string]$OutputPath)
[ordered]@{
    health_file = [Environment]::GetEnvironmentVariable("HEALTH_URL_FILE", "Process")
    health_address = [Environment]::GetEnvironmentVariable("HEALTH_LISTEN_ADDR", "Process")
} | ConvertTo-Json | Set-Content -LiteralPath $OutputPath -Encoding UTF8
'@
    [IO.File]::WriteAllText($childPath, $childSource, (New-Object Text.UTF8Encoding($false)))
    $shellPath = Join-Path $PSHOME $(if ($PSVersionTable.PSEdition -eq "Core") { "pwsh.exe" } else { "powershell.exe" })
    foreach ($failAfterChild in @($false, $true)) {
        $inheritedFile = Join-Path $scratchRoot "home-sentinel.url"
        [Environment]::SetEnvironmentVariable("HEALTH_URL_FILE", $inheritedFile, "Process")
        [Environment]::SetEnvironmentVariable("HEALTH_LISTEN_ADDR", "parent-sentinel", "Process")
        $SavedEnvironment = @{}
        $childOutput = Join-Path $scratchRoot "child.json"
        $caught = $false
        try {
            . $environmentSetup
            $info = New-Object Diagnostics.ProcessStartInfo
            $info.FileName = $shellPath
            $info.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -OutputPath "{1}"' -f $childPath, $childOutput
            $info.UseShellExecute = $false
            $info.CreateNoWindow = $true
            $process = New-Object Diagnostics.Process
            $process.StartInfo = $info
            try {
                [void]$process.Start()
                $process.WaitForExit()
                Assert-True ($process.ExitCode -eq 0) "Isolated child failed."
            } finally { $process.Dispose() }
            $child = Get-Content -LiteralPath $childOutput -Raw -Encoding UTF8 | ConvertFrom-Json
            Assert-True ([string]::IsNullOrEmpty([string]$child.health_file)) "Parent HEALTH_URL_FILE leaked into the managed child."
            Assert-True ($child.health_address -eq "127.0.0.1:8783") "Dedicated child health address was lost."
            if ($failAfterChild) { throw "fixture-child-finished" }
        } catch {
            if ($_.Exception.Message -ne "fixture-child-finished") { throw }
            $caught = $true
        } finally { . $environmentRestore }
        Assert-True ($caught -eq $failAfterChild) "Exception restoration fixture did not execute."
        foreach ($name in @("HEALTH_URL_FILE", "HEALTH_LISTEN_ADDR")) {
            $restoredValue = [Environment]::GetEnvironmentVariable($name, "Process")
            Assert-True ($restoredValue -ceq $SavedEnvironment[$name]) "Parent environment was not restored for $name."
        }
    }
    Write-Output "PASS: inherited health file is absent in the real hidden child; 127.0.0.1:8783 is preserved; parent environment is restored on success and exception"

    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
    Set-Content -LiteralPath (Join-Path $LogRoot "health-url.txt") -Value "http://127.0.0.1:8783/health" -Encoding UTF8
    $status = [pscustomobject]@{process=[pscustomobject]@{pid=4242};process_running=$true;healthy=$false;ready=$false;ui_url=$null}
    $Watch = $false
    $readyOutput = & ([scriptblock]::Create($readyBranch.Extent.Text))
    Assert-True ($null -eq $readyOutput) "A repo-local health file promoted official false status to Ready."
    Write-Status $status "starting"
    $snapshot = Get-Content -LiteralPath $script:StatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-True (-not $snapshot.healthy -and -not $snapshot.ready -and $snapshot.state -eq "starting") "Status did not preserve official false readiness."
    Assert-True (Test-Path -LiteralPath (Join-Path $LogRoot "health-url.txt")) "Existing repo-local health evidence was removed."
    Write-Output "PASS: canonical control/runtime stdio entries preserved; repo health file cannot override official false healthy/ready"
} finally {
    [Environment]::SetEnvironmentVariable("HEALTH_URL_FILE", $originalHealthFile, "Process")
    [Environment]::SetEnvironmentVariable("HEALTH_LISTEN_ADDR", $originalHealthAddress, "Process")
    $resolvedScratch = [IO.Path]::GetFullPath($scratchRoot)
    $allowedRoot = [IO.Path]::GetFullPath((Join-Path $repositoryRoot ".local")) + [IO.Path]::DirectorySeparatorChar
    if (-not $resolvedScratch.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Fixture cleanup escaped .local."
    }
    Remove-Item -LiteralPath $resolvedScratch -Recurse -Force
}
