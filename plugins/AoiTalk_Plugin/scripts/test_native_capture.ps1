$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Assert-ThrowsLike([scriptblock]$Action, [string]$Pattern, [string]$Name) {
    $message = ""
    try { & $Action | Out-Null }
    catch { $message = [string]$_.Exception.Message }
    Assert-True ($message -like $Pattern) "$Name did not fail as expected: $message"
    return $message
}

function Invoke-HiddenProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory
    )
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = $Arguments
    $info.WorkingDirectory = $WorkingDirectory
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            StdOut = $stdout
            StdErr = $stderr
        }
    } finally {
        $process.Dispose()
    }
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\..\.."))
$localRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot ".local"))
$scratchRoot = [IO.Path]::GetFullPath((Join-Path $localRoot ("native-capture-" + [guid]::NewGuid().ToString("N"))))
$projectPath = Join-Path $scratchRoot "native_capture_fixture.csproj"
$sourcePath = Join-Path $scratchRoot "Program.cs"
$callsPath = Join-Path $scratchRoot "calls.log"
$identityPath = Join-Path $scratchRoot "runtime-identity.json"
$profileRoot = Join-Path $scratchRoot "profiles"
$entryPath = [IO.Path]::GetFullPath((Join-Path $repoRoot "plugins\AoiTalk_Plugin\scripts\aoitalk_plugin_stdio.py"))
$runtimeScriptPath = [IO.Path]::GetFullPath((Join-Path $repoRoot "plugins\AoiTalk_Plugin\scripts\runtime_ownership.ps1"))
$connectScriptPath = [IO.Path]::GetFullPath((Join-Path $repoRoot "plugins\AoiTalk_Plugin\scripts\connect_tunnel.ps1"))
$fixtureExe = $null
$originalGlobalNativePreference = Get-Variable -Name PSNativeCommandUseErrorActionPreference -Scope Global -ErrorAction SilentlyContinue
$originalGlobalNativePreferenceValue = if ($originalGlobalNativePreference) { $originalGlobalNativePreference.Value } else { $null }
$originalGlobalNativePreferencePresent = $null -ne $originalGlobalNativePreference

New-Item -ItemType Directory -Force -Path $scratchRoot,$profileRoot | Out-Null
try {
    @'
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net9.0</TargetFramework>
    <RuntimeIdentifier>win-x64</RuntimeIdentifier>
    <ImplicitUsings>disable</ImplicitUsings>
    <Nullable>disable</Nullable>
    <InvariantGlobalization>true</InvariantGlobalization>
  </PropertyGroup>
</Project>
'@ | Set-Content -LiteralPath $projectPath -Encoding UTF8

    @'
using System;
using System.IO;

internal static class Program
{
    private static string Env(string name, string fallback = "")
    {
        return Environment.GetEnvironmentVariable(name) ?? fallback;
    }

    private static string Quote(string value)
    {
        return "\"" + (value ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
    }

    private static void Record(string value)
    {
        var path = Env("AOITALK_NATIVE_FIXTURE_CALLS");
        if (!String.IsNullOrEmpty(path)) File.AppendAllText(path, value + Environment.NewLine);
    }

    private static string StatusJson()
    {
        var alias = Env("AOITALK_NATIVE_FIXTURE_ALIAS", "aoitalk-41");
        var profile = Env("AOITALK_NATIVE_FIXTURE_PROFILE", "aoitalk-plugin-41");
        var profileDir = Env("AOITALK_NATIVE_FIXTURE_PROFILE_DIR");
        var tunnel = Env("AOITALK_NATIVE_FIXTURE_TUNNEL", "tunnel_fixture_41");
        var profilePath = Path.Combine(profileDir, "runtime");
        return "{\"alias\":" + Quote(alias) +
            ",\"profile_dir\":" + Quote(profileDir) +
            ",\"profile_name\":" + Quote(profile) +
            ",\"profile_path\":" + Quote(profilePath) +
            ",\"tunnel_id\":" + Quote(tunnel) +
            ",\"process_running\":true,\"healthy\":true,\"ready\":true" +
            ",\"ui_url\":\"http://127.0.0.1:58374\"" +
            ",\"process\":{\"alias\":" + Quote(alias) +
            ",\"profile_dir\":" + Quote(profileDir) +
            ",\"profile_name\":" + Quote(profile) +
            ",\"profile_path\":" + Quote(profilePath) +
            ",\"tunnel_id\":" + Quote(tunnel) + "}}";
    }

    private static string AliasEntry(string profileDir, string profileName, string tunnelId)
    {
        var alias = Env("AOITALK_NATIVE_FIXTURE_ALIAS", "aoitalk-41");
        return "{\"alias\":" + Quote(alias) +
            ",\"profile_dir\":" + Quote(profileDir) +
            ",\"profile_name\":" + Quote(profileName) +
            ",\"profile_path\":" + Quote(Path.Combine(profileDir, "runtime")) +
            ",\"tunnel_id\":" + Quote(tunnelId) + "}";
    }

    private static int Main(string[] args)
    {
        var mode = Env("AOITALK_NATIVE_FIXTURE_MODE");
        Console.Error.WriteLine("fixture-warning");
        if (args.Length > 0 && args[0] == "--version")
        {
            Record("version");
            if (mode == "version-fail") return 31;
            Console.WriteLine("tunnel-client 0.0.14");
            return 0;
        }
        if (args.Length > 0 && args[0] == "capture")
        {
            Record("capture");
            if (mode == "capture-fail") return 32;
            Console.WriteLine("fixture-stdout");
            return 0;
        }
        if (args.Length > 0 && args[0] == "-c")
        {
            Record("python-settings");
            return mode == "settings-fail" ? 33 : 0;
        }
        if (args.Length < 2 || args[0] != "runtimes") return 0;

        var command = args[1];
        Record(command);
        if (command == "status")
        {
            if (mode == "status-nonzero" || mode.StartsWith("list-", StringComparison.Ordinal)) return 1;
            if (mode == "status-invalid") { Console.WriteLine("{"); return 0; }
            Console.WriteLine(StatusJson());
            return 0;
        }
        if (command == "list")
        {
            if (mode == "list-fail") return 35;
            if (mode == "list-invalid") { Console.WriteLine("{"); return 0; }
            if (mode == "list-malformed") { Console.WriteLine("{}"); return 0; }
            if (mode == "list-malformed-entry") { Console.WriteLine("{\"aliases\":[{}]}"); return 0; }
            var profileDir = Env("AOITALK_NATIVE_FIXTURE_PROFILE_DIR");
            var profile = Env("AOITALK_NATIVE_FIXTURE_PROFILE", "aoitalk-plugin-41");
            var tunnel = Env("AOITALK_NATIVE_FIXTURE_TUNNEL", "tunnel_fixture_41");
            if (mode == "list-foreign")
            {
                Console.WriteLine("{\"aliases\":[" + AliasEntry(Path.Combine(profileDir, "foreign"), "foreign-profile", tunnel) + "]}");
            }
            else if (mode == "list-no-marker")
            {
                Console.WriteLine("{\"aliases\":[" + AliasEntry(profileDir, profile, tunnel) + "]}");
            }
            else if (mode == "list-tunnel-mismatch")
            {
                Console.WriteLine("{\"aliases\":[" + AliasEntry(profileDir, profile, "tunnel_other_fixture") + "]}");
            }
            else
            {
                Console.WriteLine("{\"admin_profile\":\"synthetic-admin\",\"aliases\":[],\"state_root\":\"fixture\"}");
            }
            return 0;
        }
        if (command == "stop") return mode == "stop-fail" ? 36 : 0;
        if (command == "connect") return mode == "connect-fail" ? 37 : 0;
        return 0;
    }
}
'@ | Set-Content -LiteralPath $sourcePath -Encoding UTF8

    $dotnetCommand = Get-Command dotnet -ErrorAction Stop
    $buildArguments = 'build --disable-build-servers -p:UseSharedCompilation=false -nodeReuse:false "{0}" --configuration Release --nologo --verbosity quiet' -f $projectPath
    $build = Invoke-HiddenProcess -FilePath $dotnetCommand.Source -Arguments $buildArguments -WorkingDirectory $scratchRoot
    if ($build.ExitCode -ne 0) {
        throw "Native fixture build failed: $($build.StdErr.Trim()) $($build.StdOut.Trim())"
    }
    $fixtureExe = Get-ChildItem -LiteralPath $scratchRoot -Recurse -File -Filter "native_capture_fixture.exe" |
        Select-Object -First 1 -ExpandProperty FullName
    Assert-True (-not [string]::IsNullOrWhiteSpace($fixtureExe)) "Native fixture build did not produce a Windows executable."

    $script:RepoRoot = $repoRoot
    $script:Entry = $entryPath
    $script:ProfileRoot = $profileRoot
    $script:IdentityPath = $identityPath
    $script:RuntimeAlias = "aoitalk-41"
    $script:ProfileName = "aoitalk-plugin-41"
    $script:TunnelId = "tunnel_fixture_41"
    $script:TunnelClient = $fixtureExe
    $script:TunnelLog = Join-Path $scratchRoot "tunnel.log"
    $script:ExpectedClientVersion = "0.0.14"
    $script:PackageRoot = $scratchRoot
    $script:EnvFile = Join-Path $scratchRoot "plugin.env"
    $script:Python = $fixtureExe
    $script:Config = @{}
    Set-Content -LiteralPath $script:EnvFile -Value "" -Encoding UTF8
    New-Item -ItemType File -Force -Path $callsPath | Out-Null

    $env:AOITALK_NATIVE_FIXTURE_MODE = ""
    $env:AOITALK_NATIVE_FIXTURE_CALLS = $callsPath
    $env:AOITALK_NATIVE_FIXTURE_ALIAS = $script:RuntimeAlias
    $env:AOITALK_NATIVE_FIXTURE_PROFILE = $script:ProfileName
    $env:AOITALK_NATIVE_FIXTURE_PROFILE_DIR = $profileRoot
    $env:AOITALK_NATIVE_FIXTURE_TUNNEL = $script:TunnelId

    . $runtimeScriptPath
    $tokens = $null
    $parseErrors = $null
    $connectAst = [System.Management.Automation.Language.Parser]::ParseFile(
        $connectScriptPath, [ref]$tokens, [ref]$parseErrors)
    Assert-True ($parseErrors.Count -eq 0) "connect_tunnel.ps1 did not parse for the native capture fixture."
    foreach ($functionName in @("Get-Setting", "Test-ClientVersion", "Test-AoiTalkSettings", "Stop-AndVerifyOwnRuntime")) {
        $functionAst = $connectAst.Find({
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName
        }, $true)
        Assert-True ($null -ne $functionAst) "Could not load $functionName for the native capture fixture."
        Invoke-Expression $functionAst.Extent.Text
    }

    $ErrorActionPreference = "Stop"
    $env:AOITALK_NATIVE_FIXTURE_MODE = "capture-warning"
    $global:PSNativeCommandUseErrorActionPreference = $true
    $script:LASTEXITCODE = 123
    $capture = Invoke-NativeCapture -Executable $fixtureExe -ArgumentList @("capture")
    Assert-True ($capture.ExitCode -eq 0 -and ($capture.StdOut -join "`n").Trim() -eq "fixture-stdout") `
        "Native stdout capture or stderr suppression failed with PSNativeCommandUseErrorActionPreference=true."
    Assert-True ($PSNativeCommandUseErrorActionPreference -eq $true -and $ErrorActionPreference -eq "Stop") `
        "Native preference or EAP was not restored after capture."
    $env:AOITALK_NATIVE_FIXTURE_MODE = "status-nonzero"
    Assert-True ($null -eq (Get-ClientStatus)) "A status stderr warning with exit 1 was not treated as unavailable."
    Assert-True ($PSNativeCommandUseErrorActionPreference -eq $true -and $ErrorActionPreference -eq "Stop") `
        "Native preference or EAP was not restored after the nonzero status path."
    Assert-ThrowsLike {
        Invoke-NativeCapture -Executable (Join-Path $scratchRoot "missing-native-preference.exe") -ArgumentList @("capture")
    } "AoiTalk native executable is missing or not launchable." "missing executable with native preference true" | Out-Null
    Assert-True ($PSNativeCommandUseErrorActionPreference -eq $true -and $ErrorActionPreference -eq "Stop") `
        "Native preference or EAP was not restored after the missing executable path."
    Remove-Variable -Name PSNativeCommandUseErrorActionPreference -Scope Global -Force
    $capture = Invoke-NativeCapture -Executable $fixtureExe -ArgumentList @("capture")
    Assert-True ($capture.ExitCode -eq 0 -and -not (Get-Variable -Name PSNativeCommandUseErrorActionPreference -Scope Global -ErrorAction SilentlyContinue)) `
        "Native preference absent-state was not restored after capture."
    Write-Output "PASS: native stdout and fresh exit code capture suppresses stderr and restores PS5/PS7 preferences"

    $env:AOITALK_NATIVE_FIXTURE_MODE = "status-nonzero"
    $status = Get-ClientStatus
    Assert-True ($null -eq $status) "A nonzero status command was not treated as unavailable."
    $env:AOITALK_NATIVE_FIXTURE_MODE = "list-empty"
    Remove-Item -LiteralPath $identityPath -Force -ErrorAction SilentlyContinue
    Assert-LocalAliasOwnership
    Write-Output "PASS: status exit 1 remains null and a successful empty alias list permits first validation"

    foreach ($mode in @("list-fail", "list-invalid", "list-malformed", "list-malformed-entry")) {
        $env:AOITALK_NATIVE_FIXTURE_MODE = $mode
        Assert-ThrowsLike { Assert-LocalAliasOwnership } "*alias registry*" "list mode $mode" | Out-Null
    }
    Write-Output "PASS: nonzero, invalid JSON, and malformed parseable alias registries fail closed"

    foreach ($mode in @("list-foreign", "list-no-marker", "list-tunnel-mismatch")) {
        $env:AOITALK_NATIVE_FIXTURE_MODE = $mode
        Remove-Item -LiteralPath $identityPath -Force -ErrorAction SilentlyContinue
        Assert-ThrowsLike { Assert-LocalAliasOwnership } "AoiTalk runtime ownership mismatch:*" "ownership list mode $mode" | Out-Null
    }
    Write-Output "PASS: foreign alias, missing marker, and mismatched tunnel ID fail closed"

    $identity = [ordered]@{
        repo_root = $repoRoot
        entry = $entryPath
        alias = $script:RuntimeAlias
        profile = $script:ProfileName
        tunnel_id = $script:TunnelId
        profile_dir = $profileRoot
        task_name = "AoiTalk Plugin 41"
    }
    $identity | ConvertTo-Json | Set-Content -LiteralPath $identityPath -Encoding UTF8
    $env:AOITALK_NATIVE_FIXTURE_MODE = "status-json-warning"
    Assert-LocalAliasOwnership
    Test-ClientVersion
    Write-Output "PASS: valid status and version JSON/stdout survive stderr warnings"

    $env:AOITALK_NATIVE_FIXTURE_MODE = "stop-fail"
    $stopFailure = Assert-ThrowsLike { Stop-OwnRuntime } "Could not stop the verified AoiTalk Plugin runtime alias*" "stop nonzero"
    Assert-True ($stopFailure -notmatch "fixture-warning|FIXTURE_RUNTIME") "Stop failure exposed stderr or credential material."
    $env:AOITALK_NATIVE_FIXTURE_MODE = "stop-unconfirmed"
    Set-Content -LiteralPath $callsPath -Value "" -Encoding UTF8
    $unconfirmed = Assert-ThrowsLike { Stop-AndVerifyOwnRuntime -TimeoutSeconds 0 } "STOP_UNCONFIRMED:*" "unconfirmed stop"
    $stopCalls = @(Get-Content -LiteralPath $callsPath | Where-Object { $_ -eq "stop" })
    Assert-True ($stopCalls.Count -eq 1) "Unconfirmed stop issued more than one native stop command."
    Write-Output "PASS: stop nonzero remains failure and a still-running runtime remains STOP_UNCONFIRMED"

    $env:AOITALK_NATIVE_FIXTURE_MODE = "connect-fail"
    $connectFailure = Assert-ThrowsLike {
        Connect-OwnRuntime $script:TunnelId "FIXTURE_RUNTIME_KEY_NAME" $fixtureExe $profileRoot $script:TunnelLog
    } "tunnel-client runtimes connect failed with exit code *" "connect nonzero"
    Assert-True ($connectFailure -notmatch "FIXTURE_RUNTIME|fixture-warning|--runtime-api-key") `
        "Connect failure exposed a credential name, stderr, or argv."
    Write-Output "PASS: connect nonzero remains fixed failure without credential or argv output"

    $env:AOITALK_NATIVE_FIXTURE_MODE = "settings-fail"
    $settingsFailure = Assert-ThrowsLike { Test-AoiTalkSettings } "AoiTalk API configuration is incomplete.*" "Python settings"
    Assert-True ($settingsFailure -notmatch "fixture-warning|FIXTURE_RUNTIME|--") "Python settings failure exposed stderr or argv."
    Write-Output "PASS: Python settings invocation uses native capture and emits its fixed failure"

    $global:LASTEXITCODE = 123
    $missingFailure = Assert-ThrowsLike {
        Invoke-NativeCapture -Executable (Join-Path $scratchRoot "missing-native.exe") -ArgumentList @("capture")
    } "AoiTalk native executable is missing or not launchable." "missing executable"
    Assert-True ($missingFailure -eq "AoiTalk native executable is missing or not launchable.") `
        "Missing executable failure was not fixed and credential-free."
    $notLaunchablePath = Join-Path $scratchRoot "not-launchable.exe"
    [IO.File]::WriteAllBytes($notLaunchablePath, [byte[]](0x00, 0x01, 0x02, 0x03))
    Assert-ThrowsLike {
        Invoke-NativeCapture -Executable $notLaunchablePath -ArgumentList @("capture")
    } "AoiTalk native executable is missing or not launchable." "not launchable executable" | Out-Null
    $malformedPath = $scratchRoot + [char]0 + "malformed-native.exe"
    Assert-ThrowsLike {
        Invoke-NativeCapture -Executable $malformedPath -ArgumentList @("capture")
    } "AoiTalk native executable is missing or not launchable." "malformed executable path" | Out-Null
    Assert-True (-not (Get-Variable -Name PSNativeCommandUseErrorActionPreference -Scope Global -ErrorAction SilentlyContinue) -and
        $ErrorActionPreference -eq "Stop") "Native preference or EAP was not restored after an absent-state failure."
    Write-Output "PASS: missing and not-launchable paths use a fixed error without stale exit-code reuse"
}
finally {
    if ($originalGlobalNativePreferencePresent) {
        Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $originalGlobalNativePreferenceValue -Scope Global -Force
    } else {
        Remove-Variable -Name PSNativeCommandUseErrorActionPreference -Scope Global -Force -ErrorAction SilentlyContinue
    }
    Remove-Item Env:AOITALK_NATIVE_FIXTURE_MODE,Env:AOITALK_NATIVE_FIXTURE_CALLS,
        Env:AOITALK_NATIVE_FIXTURE_ALIAS,Env:AOITALK_NATIVE_FIXTURE_PROFILE,
        Env:AOITALK_NATIVE_FIXTURE_PROFILE_DIR,Env:AOITALK_NATIVE_FIXTURE_TUNNEL -ErrorAction SilentlyContinue
    $scratchAbsolute = [IO.Path]::GetFullPath($scratchRoot)
    $localAbsolute = $localRoot.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $localPrefix = $localAbsolute + [IO.Path]::DirectorySeparatorChar
    if (-not $scratchAbsolute.StartsWith($localPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove native fixture scratch outside repository .local."
    }
    Remove-Item -LiteralPath $scratchAbsolute -Recurse -Force -ErrorAction SilentlyContinue
}

