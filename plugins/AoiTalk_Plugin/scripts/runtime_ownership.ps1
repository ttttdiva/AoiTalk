function Read-RuntimeIdentity {
    if (-not (Test-Path -LiteralPath $script:IdentityPath -PathType Leaf)) { return $null }
    try { return (Get-Content -LiteralPath $script:IdentityPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop) }
    catch { throw "AoiTalk runtime ownership mismatch: identity marker is malformed; refusing to touch alias '$script:RuntimeAlias'." }
}

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [object[]]$ArgumentList = @()
    )

    $savedErrorActionPreference = $ErrorActionPreference
    $localNativePreference = Get-Variable -Name PSNativeCommandUseErrorActionPreference -Scope Local -ErrorAction SilentlyContinue
    $savedNativePreference = if ($localNativePreference) { $localNativePreference.Value } else { $null }
    $hadLocalNativePreference = $null -ne $localNativePreference
    $missingExecutableMessage = "AoiTalk native executable is missing or not launchable."
    try {
        $ErrorActionPreference = "Continue"
        Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $false -Scope Local -Force
        try { $extension = [IO.Path]::GetExtension($Executable) }
        catch { throw $missingExecutableMessage }
        if ([string]::IsNullOrWhiteSpace($Executable) -or
            $extension -notin @(".exe", ".com", ".cmd", ".bat") -or
            -not (Test-Path -LiteralPath $Executable -PathType Leaf -ErrorAction SilentlyContinue)) {
            throw $missingExecutableMessage
        }

        # Clear the automatic value so a failed invocation can never reuse a prior exit code.
        $global:LASTEXITCODE = [int]::MinValue
        try {
            $stdout = @(& $Executable @ArgumentList 2>$null)
        } catch {
            throw $missingExecutableMessage
        }
        $exitCode = $global:LASTEXITCODE
        if ($exitCode -eq [int]::MinValue) { throw $missingExecutableMessage }
        return [pscustomobject]@{
            StdOut = @($stdout)
            ExitCode = [int]$exitCode
        }
    } finally {
        $ErrorActionPreference = $savedErrorActionPreference
        if ($hadLocalNativePreference) {
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $savedNativePreference -Scope Local -Force
        } else {
            Remove-Variable -Name PSNativeCommandUseErrorActionPreference -Scope Local -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-ClientStatus {
    $capture = Invoke-NativeCapture -Executable $script:TunnelClient -ArgumentList @(
        "runtimes", "status", $script:RuntimeAlias, "--json"
    )
    if ($capture.ExitCode -ne 0) { return $null }
    try { return (($capture.StdOut -join "`n") | ConvertFrom-Json -ErrorAction Stop) }
    catch { throw "tunnel-client returned an unreadable status for alias '$script:RuntimeAlias'." }
}

function Get-ObjectValue($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($property) { return $property.Value }
    return $null
}

function Test-ObjectHasProperty($Object, [string]$Name) {
    if ($null -eq $Object) { return $false }
    if ($Object -is [System.Collections.IDictionary]) { return $Object.Contains($Name) }
    return $null -ne $Object.PSObject.Properties[$Name]
}

function Test-ExpectedPath([string]$Actual, [string]$Expected) {
    if ([string]::IsNullOrWhiteSpace($Actual)) { return $false }
    try {
        return [IO.Path]::GetFullPath($Actual).Equals(
            [IO.Path]::GetFullPath($Expected), [StringComparison]::OrdinalIgnoreCase)
    } catch { return $false }
}

function Test-PathWithinProfile([string]$Actual) {
    if ([string]::IsNullOrWhiteSpace($Actual)) { return $false }
    try {
        $actualPath = [IO.Path]::GetFullPath($Actual).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
        $profilePath = [IO.Path]::GetFullPath($script:ProfileRoot).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
        return $actualPath.Equals($profilePath, [StringComparison]::OrdinalIgnoreCase) -or
            $actualPath.StartsWith($profilePath + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
            $actualPath.StartsWith($profilePath + [IO.Path]::AltDirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
    } catch { return $false }
}

function Assert-ReportedProfilePath($Object, [string]$Field, [string]$Owner) {
    if (Test-ObjectHasProperty $Object $Field) {
        $value = Get-ObjectValue $Object $Field
        if (-not [string]::IsNullOrWhiteSpace([string]$value) -and -not (Test-PathWithinProfile ([string]$value))) {
            throw "AoiTalk runtime ownership mismatch: tunnel-client $Owner.$Field is outside this checkout's profile directory; runtime was left untouched."
        }
    }
}

function Assert-OwnIdentity {
    $identity = Read-RuntimeIdentity
    if (-not $identity -or
        -not (Test-ExpectedPath ([string](Get-ObjectValue $identity "repo_root")) $script:RepoRoot) -or
        -not (Test-ExpectedPath ([string](Get-ObjectValue $identity "entry")) $script:Entry) -or
        -not (Test-ExpectedPath ([string](Get-ObjectValue $identity "profile_dir")) $script:ProfileRoot) -or
        [string](Get-ObjectValue $identity "alias") -ne $script:RuntimeAlias -or
        [string](Get-ObjectValue $identity "profile") -ne $script:ProfileName) {
        throw "AoiTalk runtime ownership mismatch: marker does not match this 41 checkout, entry, alias, and profile; runtime was left untouched."
    }
    $markerTunnelId = [string](Get-ObjectValue $identity "tunnel_id")
    if (-not [string]::IsNullOrWhiteSpace($markerTunnelId) -and
        -not [string]::IsNullOrWhiteSpace([string]$script:TunnelId) -and
        $markerTunnelId -ne [string]$script:TunnelId) {
        throw "AoiTalk runtime ownership mismatch: identity marker is bound to another tunnel ID; runtime was left untouched."
    }
}

function Assert-StatusOwnership($Status, [switch]$CheckTunnelId, [switch]$RequireTunnelId) {
    if ($null -eq $Status) { throw "AoiTalk runtime ownership mismatch: status is unavailable." }
    $alias = Get-ObjectValue $Status "alias"
    $profileDir = Get-ObjectValue $Status "profile_dir"
    $profileName = Get-ObjectValue $Status "profile_name"
    if ([string]$alias -ne $script:RuntimeAlias -or
        -not (Test-ExpectedPath ([string]$profileDir) $script:ProfileRoot) -or
        [string]$profileName -ne $script:ProfileName) {
        throw "AoiTalk runtime ownership mismatch: tunnel-client status alias/profile does not match '$script:RuntimeAlias' and '$script:ProfileName'; runtime was left untouched."
    }
    Assert-ReportedProfilePath $Status "profile_path" "status"

    $process = Get-ObjectValue $Status "process"
    Assert-ReportedProfilePath $process "profile_path" "process"
    foreach ($field in @("alias", "profile_dir", "profile_name")) {
        if (Test-ObjectHasProperty $process $field) {
            $actual = Get-ObjectValue $process $field
            $matches = switch ($field) {
                "alias" { [string]$actual -eq $script:RuntimeAlias }
                "profile_dir" { Test-ExpectedPath ([string]$actual) $script:ProfileRoot }
                "profile_name" { [string]$actual -eq $script:ProfileName }
            }
            if (-not $matches) {
                throw "AoiTalk runtime ownership mismatch: tunnel-client process.$field does not match this checkout; runtime was left untouched."
            }
        }
    }

    $expectedTunnelId = [string]$script:TunnelId
    if (($CheckTunnelId -or $RequireTunnelId) -and -not [string]::IsNullOrWhiteSpace($expectedTunnelId)) {
        $actualTunnelId = Get-ObjectValue $Status "tunnel_id"
        if ([string]::IsNullOrWhiteSpace([string]$actualTunnelId)) {
            if ($RequireTunnelId) {
                throw "AoiTalk runtime ownership mismatch: tunnel-client status has no verifiable tunnel_id; runtime was left untouched."
            }
        } elseif ([string]$actualTunnelId -ne $expectedTunnelId) {
            throw "AoiTalk runtime ownership mismatch: tunnel-client status tunnel_id does not match the configured AoiTalk tunnel; runtime was left untouched."
        }
        if (Test-ObjectHasProperty $process "tunnel_id") {
            $processTunnelId = Get-ObjectValue $process "tunnel_id"
            if ([string]$processTunnelId -ne $expectedTunnelId) {
                throw "AoiTalk runtime ownership mismatch: tunnel-client process.tunnel_id does not match the configured AoiTalk tunnel; runtime was left untouched."
            }
        }
    }
}

function Get-ClientAliasEntries($List) {
    if (-not (Test-ObjectHasProperty $List "aliases")) {
        throw "Could not parse the local tunnel-client alias registry; refusing to create or replace an alias."
    }
    # Read the property directly so an official empty array is not unrolled to $null
    # by the output pipeline of Get-ObjectValue.
    if ($List -is [System.Collections.IDictionary]) {
        $aliases = $List["aliases"]
    } else {
        $aliases = $List.PSObject.Properties["aliases"].Value
    }
    if ($null -eq $aliases -or $aliases -is [string] -or
        -not ($aliases -is [System.Collections.IEnumerable])) {
        throw "Could not parse the local tunnel-client alias registry; refusing to create or replace an alias."
    }
    return @($aliases)
}

function Assert-ListedAliasOwnership($List) {
    $entries = @(Get-ClientAliasEntries $List)
    foreach ($entry in $entries) {
        if ($null -eq $entry -or
            -not (Test-ObjectHasProperty $entry "alias")) {
            throw "Could not parse the local tunnel-client alias registry; refusing to create or replace an alias."
        }
        $listedAlias = Get-ObjectValue $entry "alias"
        if ($listedAlias -isnot [string] -or [string]::IsNullOrWhiteSpace($listedAlias)) {
            throw "Could not parse the local tunnel-client alias registry; refusing to create or replace an alias."
        }
    }
    $entries = @($entries | Where-Object {
        [string](Get-ObjectValue $_ "alias") -eq $script:RuntimeAlias
    })
    foreach ($entry in $entries) {
        if (-not (Test-ExpectedPath ([string](Get-ObjectValue $entry "profile_dir")) $script:ProfileRoot) -or
            [string](Get-ObjectValue $entry "profile_name") -ne $script:ProfileName) {
            throw "AoiTalk runtime ownership mismatch: tunnel-client alias registry assigns '$script:RuntimeAlias' to another profile; runtime was left untouched."
        }
        Assert-ReportedProfilePath $entry "profile_path" "alias registry"
        $expectedTunnelId = [string]$script:TunnelId
        $listedTunnelId = Get-ObjectValue $entry "tunnel_id"
        if (-not [string]::IsNullOrWhiteSpace($expectedTunnelId) -and
            -not [string]::IsNullOrWhiteSpace([string]$listedTunnelId) -and
            [string]$listedTunnelId -ne $expectedTunnelId) {
            throw "AoiTalk runtime ownership mismatch: tunnel-client alias registry tunnel_id does not match the configured AoiTalk tunnel; runtime was left untouched."
        }
    }
    return ($entries.Count -gt 0)
}

function Assert-LocalAliasOwnership {
    $identity = Read-RuntimeIdentity
    if ($identity) { Assert-OwnIdentity }
    $status = Get-ClientStatus
    if ($status) {
        if (-not $identity) {
            throw "AoiTalk runtime ownership mismatch: local tunnel-client alias '$script:RuntimeAlias' exists without this 41 checkout's identity marker; it was left untouched."
        }
        Assert-StatusOwnership $status -RequireTunnelId
        return
    }

    $listCapture = Invoke-NativeCapture -Executable $script:TunnelClient -ArgumentList @(
        "runtimes", "list", "--json"
    )
    if ($listCapture.ExitCode -ne 0) { throw "Could not verify the local tunnel-client alias registry; refusing to create or replace an alias." }
    try { $list = ($listCapture.StdOut -join "`n") | ConvertFrom-Json -ErrorAction Stop }
    catch { throw "Could not parse the local tunnel-client alias registry; refusing to create or replace an alias." }
    $hasAlias = Assert-ListedAliasOwnership $list
    if ($hasAlias -and -not $identity) {
        throw "AoiTalk runtime ownership mismatch: local tunnel-client alias '$script:RuntimeAlias' exists without this 41 checkout's identity marker; it was left untouched."
    }
}

function Stop-OwnRuntime {
    Assert-OwnIdentity
    $status = Get-ClientStatus
    if (-not $status) {
        $listCapture = Invoke-NativeCapture -Executable $script:TunnelClient -ArgumentList @(
            "runtimes", "list", "--json"
        )
        if ($listCapture.ExitCode -ne 0) { throw "Could not verify whether the AoiTalk Plugin runtime is running; no runtime was stopped." }
        try { $list = ($listCapture.StdOut -join "`n") | ConvertFrom-Json -ErrorAction Stop }
        catch { throw "Could not parse the local tunnel-client alias registry; no runtime was stopped." }
        if (Assert-ListedAliasOwnership $list) {
            throw "The owned runtime alias exists but status is unavailable; no runtime was stopped."
        }
        return $false
    }
    Assert-StatusOwnership $status -RequireTunnelId
    if (Get-ObjectValue $Status "process_running") {
        $capture = Invoke-NativeCapture -Executable $script:TunnelClient -ArgumentList @(
            "runtimes", "stop", $script:RuntimeAlias
        )
        if ($capture.ExitCode -ne 0) {
            throw "Could not stop the verified AoiTalk Plugin runtime alias '$script:RuntimeAlias' (exit code $($capture.ExitCode))."
        }
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
        if (Test-Path -LiteralPath $script:IdentityPath -PathType Leaf) {
            Assert-OwnIdentity
            $status = Get-ClientStatus
            if ($status) {
                Assert-StatusOwnership $status -RequireTunnelId
                $ui = $null
                if ((Get-ObjectValue $status "process_running") -and (Get-ObjectValue $status "healthy") -and
                    (Get-ObjectValue $status "ready") -and
                    [Uri]::TryCreate([string](Get-ObjectValue $status "ui_url"), [UriKind]::Absolute, [ref]$ui) -and
                    $ui.Port -eq $port -and $ui.Host -in @("127.0.0.1", "localhost")) {
                    return
                }
            }
        }
    }
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Parse("127.0.0.1"), $port)
    try { $listener.Start() }
    catch { throw "Dedicated tunnel health port $Address is already in use; change AOITALK_PLUGIN_HEALTH_LISTEN_ADDR and retry." }
    finally { $listener.Stop() }
}

function Connect-OwnRuntime([string]$TunnelId, [string]$RuntimeKeyName, [string]$Python, [string]$ProfileRoot, [string]$TunnelLog) {
    Assert-LocalAliasOwnership
    $pythonArg = '"' + $Python.Replace('\','/') + '" -m aoitalk_plugin.stdio'
    $capture = Invoke-NativeCapture -Executable $script:TunnelClient -ArgumentList @(
        "runtimes", "connect", "--alias", $script:RuntimeAlias, "--tunnel-id", $TunnelId,
        "--profile", $script:ProfileName, "--profile-dir", ($ProfileRoot.Replace('\','/')),
        "--runtime-api-key", ("env:" + $RuntimeKeyName), "--mcp-command", $pythonArg
    )
    if ($capture.ExitCode -ne 0) { throw "tunnel-client runtimes connect failed with exit code $($capture.ExitCode)." }
}
