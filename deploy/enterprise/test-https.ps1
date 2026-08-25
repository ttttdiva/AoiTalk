[CmdletBinding()]
param(
    [string]$ServerAddress = "192.168.250.100",
    [int]$Port = 6002,
    [string]$SiteHost = "localhost",
    [ValidateSet("auto", "locked", "unlocked")]
    [string]$ExpectedBootstrapState = "auto",
    [switch]$SkipBrowsers
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-CurlStatus([string[]]$Arguments) {
    $output = & curl.exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "curl failed ($LASTEXITCODE): curl.exe $($Arguments -join ' ')"
    }
    $status = ($output -join "").Trim()
    if ($status -notmatch '^[0-9]{3}$') {
        throw "curl did not return an HTTP status: $status"
    }
    return $status
}

function Invoke-OpenSsl([string[]]$Arguments, [string]$InputText = "") {
    if ($InputText) {
        $output = @($InputText | & openssl.exe @Arguments 2>&1)
    }
    else {
        # Pipe a single empty line instead of relying on a Unix-only /dev/null.
        $output = @("" | & openssl.exe @Arguments 2>&1)
    }
    $exitCode = $LASTEXITCODE
    $text = (($output | ForEach-Object { [string]$_ }) -join "`n").Trim()
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output   = $text
    }
}

function Assert-HostValue([string]$Value, [string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Length -gt 253) {
        throw "$Name must be a non-empty host name or IP address"
    }
    if ($Value -match '[\x00-\x20\x7f/\\?#@]') {
        throw "$Name contains an unsafe character: $Value"
    }
}

if ($Port -lt 1 -or $Port -gt 65535) {
    throw "Port must be between 1 and 65535: $Port"
}
Assert-HostValue $ServerAddress "ServerAddress"
Assert-HostValue $SiteHost "SiteHost"

if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
    throw "curl.exe is required"
}
if (-not (Get-Command openssl.exe -ErrorAction SilentlyContinue)) {
    throw "openssl.exe is required for the SNI-less TLS ClientHello smoke"
}

$ipUri = "https://${ServerAddress}:${Port}/"
$sniUri = "https://${SiteHost}:${Port}/"
$resolve = "${SiteHost}:${Port}:${ServerAddress}"

$ipStatus = Invoke-CurlStatus @(
    "--noproxy", "*", "-k", "--silent", "--show-error", "--output", "NUL", "--write-out", "%{http_code}", $ipUri
)
if ($ipStatus -eq "000") {
    throw "IP-address HTTPS curl did not complete TLS: $ipUri"
}
Write-Host "IP-address curl completed with HTTP ${ipStatus}: $ipUri"

$sniStatus = Invoke-CurlStatus @(
    "--noproxy", "*", "-k", "--silent", "--show-error", "--output", "NUL", "--write-out", "%{http_code}",
    "--resolve", $resolve, $sniUri
)
if ($sniStatus -notmatch "^(2|3|401|403)") {
    throw "SNI HTTPS smoke returned unexpected HTTP ${sniStatus}: $sniUri"
}

$invalidHostStatus = Invoke-CurlStatus @(
    "--noproxy", "*", "-k", "--silent", "--show-error", "--output", "NUL", "--write-out", "%{http_code}",
    "--resolve", $resolve, "-H", "Host: invalid-enterprise-host.invalid", $sniUri
)
if ($invalidHostStatus -ne "404") {
    throw "Invalid Host was not rejected by the Caddy catch-all: HTTP $invalidHostStatus"
}
Write-Host "Invalid Host catch-all returned HTTP 404"

# The public listener is fail-closed until the bootstrap administrator has
# completed the password reset.  Once unlocked, the same request reaches the
# application and must not remain a 403/503.  This check intentionally exposes
# only the state category, never credentials or response bodies.
$bootstrapStatus = Invoke-CurlStatus @(
    "--noproxy", "*", "-k", "--silent", "--show-error", "--output", "NUL", "--write-out", "%{http_code}",
    "--resolve", $resolve, ($sniUri + "api/auth/status")
)
switch ($ExpectedBootstrapState) {
    "locked" {
        if ($bootstrapStatus -ne "403") {
            throw "Expected bootstrap locked (HTTP 403), received HTTP $bootstrapStatus"
        }
        Write-Host "Bootstrap gate is locked (HTTP 403)"
    }
    "unlocked" {
        if ($bootstrapStatus -in @("403", "503")) {
            throw "Expected bootstrap unlocked, received HTTP $bootstrapStatus"
        }
        Write-Host "Bootstrap gate is unlocked (HTTP $bootstrapStatus)"
    }
    default {
        if ($bootstrapStatus -eq "403") {
            Write-Host "Bootstrap gate is locked (HTTP 403)"
        }
        elseif ($bootstrapStatus -match "^5") {
            throw "Bootstrap gate returned a server error: HTTP $bootstrapStatus"
        }
        else {
            Write-Host "Bootstrap gate is unlocked (HTTP $bootstrapStatus)"
        }
    }
}

# Verify a ClientHello with no server_name extension.  A certificate trust
# warning is acceptable because the Enterprise deployment may use an internal
# CA; rejecting the handshake before certificate delivery is not.
$sniLessHandshake = Invoke-OpenSsl @(
    "s_client", "-connect", "${ServerAddress}:$Port", "-noservername", "-brief"
)
if ($sniLessHandshake.Output -match '(?i)internal error|unrecognized_name|handshake failure|no peer certificate') {
    throw "SNI-less TLS ClientHello was rejected before certificate delivery: $($sniLessHandshake.Output)"
}
if ($sniLessHandshake.Output -notmatch '(?im)Protocol([\s:]+version)?[\s:]|Cipher(suite)?[\s:]') {
    throw "SNI-less TLS ClientHello did not complete a TLS handshake: $($sniLessHandshake.Output)"
}
Write-Host "SNI-less TLS ClientHello completed certificate delivery"

# Send one HTTP request over the same SNI-less connection.  This is a useful
# regression check for Caddy's default_sni/catch-all behavior, while allowing
# either the configured route or its intentional 404 catch-all response.
$sniLessRequest = "GET /login HTTP/1.1`r`nHost: $ServerAddress`r`nConnection: close`r`n`r`n"
$sniLessHttp = Invoke-OpenSsl @(
    "s_client", "-connect", "${ServerAddress}:$Port", "-noservername", "-quiet"
) $sniLessRequest
$sniLessMatch = [regex]::Match($sniLessHttp.Output, '(?im)^HTTP/[0-9.]+\s+(?<status>[0-9]{3})')
if (-not $sniLessMatch.Success) {
    throw "SNI-less HTTPS request did not return an HTTP response: $($sniLessHttp.Output)"
}
$sniLessStatus = $sniLessMatch.Groups["status"].Value
if ($sniLessStatus -match '^5') {
    throw "SNI-less HTTPS request returned server error HTTP $sniLessStatus"
}
Write-Host "SNI-less HTTPS request returned HTTP $sniLessStatus"

if (-not $SkipBrowsers) {
    $browserCandidates = [ordered]@{
        Chrome = @(
            "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
            "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
        )
        Edge = @(
            "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
            "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
            "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe"
        )
    }
    foreach ($browser in $browserCandidates.Keys) {
        $executable = $browserCandidates[$browser] | Where-Object {
            $_ -and (Test-Path -LiteralPath $_ -PathType Leaf)
        } | Select-Object -First 1
        if (-not $executable) {
            throw "$browser executable was not found; install both Chrome and Edge or pass -SkipBrowsers for non-browser CI"
        }
        $arguments = @(
            "--headless=new", "--disable-gpu", "--ignore-certificate-errors", "--no-sandbox",
            "--dump-dom", $ipUri
        )
        $process = Start-Process -FilePath $executable -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
        if ($process.ExitCode -ne 0) {
            throw "$browser could not open the IP-address HTTPS URL (exit $($process.ExitCode)): $ipUri"
        }
        Write-Host "$browser opened the IP-address HTTPS URL: $ipUri"
    }
}

Write-Host "Enterprise HTTPS smoke passed. Certificate warnings are acceptable; TLS internal errors are not."
