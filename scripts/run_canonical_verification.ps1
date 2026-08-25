<#
.SYNOPSIS
  Run local canonical verification gates that mirror .github/workflows/ci.yml on Windows.

.DESCRIPTION
  Run local canonical verification gates that mirror .github/workflows/ci.yml on Windows.

  **Manual use only.** Codex/agents must NOT auto-run this in normal work or on CI UNAVAILABLE.
  Use when the user explicitly requests local full-CI parity, for CI infrastructure investigation,
  or special local reproduction.

  Each gate reports PASS / FAIL / SKIP with a reason. Fail-fast: stops after the first gate FAIL.
  Exit 0 only when every executed gate passes (no SKIP).

  Gates (ci.yml parity):
    - mobile release gate
    - Mobile Product Contract, route inventory, and shared OpenAPI JSON parity
    - frontend: lint, typecheck, typegen drift, vitest, build, build:production, BUILD_ID
      (including Mobile generated API typegen drift)
    - python: MCP import smoke, ruff, security boundary tests, wheel smoke
    - backend: full pytest
    - schema drift: alembic upgrade head, check_schema_drift.py, login-log postgres vitest
    - frontend E2E Playwright (same specs as CI; requires PostgreSQL)

  PostgreSQL: requires ephemeral Docker containers (postgres:16), one per CI postgres job
  (schema-drift and E2E each get a fresh container). Local existing databases are not used.

.PARAMETER Base
  Git ref for mobile release gate base (default: origin/main).

.PARAMETER Target
  Git ref for mobile release gate target (default: HEAD).

.PARAMETER SkipE2E
  Skip Playwright E2E gates. Documented escape hatch only; full parity requires E2E.

.PARAMETER KeepPostgres
  Do not stop Docker container or drop temporary database after completion (debugging).

.EXAMPLE
  .\scripts\run_canonical_verification.ps1
#>
[CmdletBinding()]
param(
    [string]$Base = "origin/main",
    [string]$Target = "HEAD",
    [switch]$SkipE2E,
    [switch]$KeepPostgres
)

$ErrorActionPreference = "Stop"

$EXIT_PASS = 0
$EXIT_FAIL = 1

$Script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Script:FrontendDir = Join-Path $RepoRoot "frontend"
$Script:PythonExe = Join-Path $RepoRoot "venv\Scripts\python.exe"
$Script:GateResults = [System.Collections.Generic.List[object]]::new()
$Script:FailFastAbort = $false
$Script:PostgresContext = $null
$Script:StartedProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()

$CiFrontendEnv = @{
    DATABASE_URL       = "postgres://ci:ci@127.0.0.1:5432/ci"
    NEXTAUTH_SECRET    = "ci-dummy-secret-not-real"
    AUTH_SECRET        = "ci-dummy-secret-not-real"
    PYTHON_API_URL     = "http://127.0.0.1:3000"
}

$E2ESpecs = @(
    "e2e/docs-clip-ingest.spec.ts",
    "e2e/login.spec.ts",
    "e2e/reset-password.spec.ts",
    "e2e/navigation.spec.ts",
    "e2e/chat.spec.ts",
    "e2e/tasks.spec.ts",
    "e2e/remediation-smoke.spec.ts"
)

function Write-GateLine {
    param(
        [string]$Name,
        [ValidateSet("PASS", "FAIL", "SKIP")]
        [string]$Status,
        [string]$Reason = ""
    )

    $color = switch ($Status) {
        "PASS" { "Green" }
        "FAIL" { "Red" }
        "SKIP" { "Yellow" }
    }
    $suffix = if ($Reason) { " — $Reason" } else { "" }
    Write-Host ("[{0}] {1}{2}" -f $Status, $Name, $suffix) -ForegroundColor $color
    $Script:GateResults.Add([pscustomobject]@{ Name = $Name; Status = $Status; Reason = $Reason })
    if ($Status -eq "FAIL") {
        $Script:FailFastAbort = $true
    }
}

function Read-DotEnv {
    param([string]$Path)

    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $map
    }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match '^\s*#' -or $line -match '^\s*$') { continue }
        if ($line -match '^([^=]+)=(.*)$') {
            $map[$Matches[1].Trim()] = $Matches[2].Trim()
        }
    }
    return $map
}

function Test-CommandAvailable {
    param([string]$Name)
    $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-TcpPortOpen {
    param(
        [string]$HostName = "127.0.0.1",
        [int]$Port
    )
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        $ok = $async.AsyncWaitHandle.WaitOne(1000, $false)
        if ($ok -and $client.Connected) {
            $client.EndConnect($async)
            $client.Close()
            return $true
        }
        $client.Close()
        return $false
    }
    catch {
        return $false
    }
}

function Find-FreeTcpPort {
    param([int]$StartPort = 55432)
    for ($port = $StartPort; $port -lt ($StartPort + 200); $port++) {
        if (-not (Test-TcpPortOpen -Port $port)) {
            return $port
        }
    }
    throw "No free TCP port found near $StartPort"
}

function Wait-PostgresReady {
    param(
        [string]$HostName,
        [int]$Port,
        [string]$User,
        [string]$Database,
        [string]$Password,
        [int]$TimeoutSec = 120
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $envBackup = $env:PGPASSWORD
    $env:PGPASSWORD = $Password
    try {
        while ((Get-Date) -lt $deadline) {
            if (Test-CommandAvailable "psql") {
                & psql -h $HostName -p $Port -U $User -d $Database -tAc "SELECT 1" 2>$null | Out-Null
                if ($LASTEXITCODE -eq 0) { return }
            }
            elseif (Test-TcpPortOpen -HostName $HostName -Port $Port) {
                try {
                    & $Script:PythonExe -c @"
import psycopg2
conn = psycopg2.connect(host='$HostName', port=$Port, user='$User', password='$Password', dbname='$Database')
conn.close()
"@
                    if ($LASTEXITCODE -eq 0) { return }
                }
                catch { }
            }
            Start-Sleep -Seconds 2
        }
        throw "PostgreSQL not ready on ${HostName}:${Port} within ${TimeoutSec}s"
    }
    finally {
        if ($null -eq $envBackup) {
            Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
        }
        else {
            $env:PGPASSWORD = $envBackup
        }
    }
}

function Invoke-PsqlAdmin {
    param(
        [string]$HostName,
        [int]$Port,
        [string]$AdminUser,
        [string]$AdminPassword,
        [string]$Database,
        [string]$Sql
    )

    $envBackup = $env:PGPASSWORD
    $env:PGPASSWORD = $AdminPassword
    try {
        if (Test-CommandAvailable "psql") {
            & psql -v ON_ERROR_STOP=1 -h $HostName -p $Port -U $AdminUser -d $Database -tAc $Sql
            if ($LASTEXITCODE -ne 0) {
                throw "psql failed: $Sql"
            }
            return
        }

        $escaped = $Sql.Replace("'", "''")
        & $Script:PythonExe -c @"
import psycopg2
conn = psycopg2.connect(host='$HostName', port=$Port, user='$AdminUser', password='$AdminPassword', dbname='$Database')
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute('$escaped')
conn.close()
"@
        if ($LASTEXITCODE -ne 0) {
            throw "psql-equivalent failed: $Sql"
        }
    }
    finally {
        if ($null -eq $envBackup) {
            Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
        }
        else {
            $env:PGPASSWORD = $envBackup
        }
    }
}

function Test-VersionAtLeast {
    param(
        [string]$VersionText,
        [int]$Major,
        [int]$Minor = 0
    )
    if ($VersionText -match '(\d+)\.(\d+)') {
        $vMajor = [int]$Matches[1]
        $vMinor = [int]$Matches[2]
        return ($vMajor -gt $Major) -or ($vMajor -eq $Major -and $vMinor -ge $Minor)
    }
    return $false
}

function Test-WorkflowsChanged {
    param([string]$Base = "origin/main")
    $files = @(& git -C $Script:RepoRoot diff --name-only "$Base..HEAD" 2>$null)
    return @($files | Where-Object { $_ -match '^\.github/workflows/' }).Count -gt 0
}

function Start-CanonicalPostgres {
    if (Test-CommandAvailable "docker") {
        try {
            & docker info 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                $port = Find-FreeTcpPort
                $container = "aoitalk-canonical-verify-$PID-$([guid]::NewGuid().ToString('N').Substring(0, 6))"
                & docker run -d --name $container `
                    -e POSTGRES_USER=aoitalk `
                    -e POSTGRES_PASSWORD=ci `
                    -e POSTGRES_DB=aoitalk_memory `
                    -p "${port}:5432" `
                    postgres:16 | Out-Null
                if ($LASTEXITCODE -ne 0) {
                    throw "docker run postgres:16 failed"
                }

                Wait-PostgresReady -HostName "127.0.0.1" -Port $port -User "aoitalk" -Database "aoitalk_memory" -Password "ci"
                return [pscustomobject]@{
                    Mode            = "docker"
                    ContainerName   = $container
                    Host            = "127.0.0.1"
                    Port            = $port
                    User            = "aoitalk"
                    Password        = "ci"
                    Database        = "aoitalk_memory"
                    TempDatabase    = $null
                    AdminUser       = "aoitalk"
                    AdminPassword   = "ci"
                    StartedByScript = $true
                }
            }
        }
        catch {
            Write-Host "Docker postgres failed — trying local ephemeral database." -ForegroundColor Yellow
        }
    }

    $dotenv = Read-DotEnv (Join-Path $Script:RepoRoot ".env")
    $hostName = if ($dotenv.POSTGRES_HOST) { $dotenv.POSTGRES_HOST } else { "127.0.0.1" }
    $port = if ($dotenv.POSTGRES_PORT) { [int]$dotenv.POSTGRES_PORT } else { 5432 }
    $adminUser = "postgres"
    $adminPassword = $env:PGPASSWORD
    if ([string]::IsNullOrWhiteSpace($adminPassword) -and $dotenv.PGPASSWORD) {
        $adminPassword = $dotenv.PGPASSWORD
    }
    if ([string]::IsNullOrWhiteSpace($adminPassword)) {
        throw "PGPASSWORD required to create ephemeral local database (set env or use Docker)."
    }
    $appUser = if ($dotenv.POSTGRES_USER) { $dotenv.POSTGRES_USER } else { "aoitalk" }
    $appPassword = if ($dotenv.POSTGRES_PASSWORD) { $dotenv.POSTGRES_PASSWORD } else { "ci" }
    $tempDb = "aoitalk_canonical_$([guid]::NewGuid().ToString('N').Substring(0, 8))"

    if (-not (Test-TcpPortOpen -HostName $hostName -Port $port)) {
        throw "Neither Docker nor local PostgreSQL is available for canonical verification."
    }

    Invoke-PsqlAdmin -HostName $hostName -Port $port -AdminUser $adminUser -AdminPassword $adminPassword -Database "postgres" -Sql "CREATE DATABASE `"$tempDb`" OWNER `"$appUser`";"

    return [pscustomobject]@{
        Mode            = "local-temp-db"
        ContainerName   = $null
        Host            = $hostName
        Port            = $port
        User            = $appUser
        Password        = $appPassword
        Database        = $tempDb
        TempDatabase    = $tempDb
        AdminUser       = $adminUser
        AdminPassword   = $adminPassword
        StartedByScript = $true
    }
}

function Stop-CanonicalPostgres {
    param([object]$Context)

    if (-not $Context -or -not $Context.StartedByScript) {
        return
    }
    if ($KeepPostgres) {
        Write-Host "KeepPostgres set — leaving PostgreSQL resources in place." -ForegroundColor Yellow
        if ($Context.ContainerName) {
            Write-Host "  Docker container: $($Context.ContainerName)"
        }
        if ($Context.TempDatabase) {
            Write-Host "  Temp database: $($Context.TempDatabase)"
        }
        return
    }

    if ($Context.Mode -eq "docker" -and $Context.ContainerName) {
        & docker rm -f $Context.ContainerName 2>$null | Out-Null
        return
    }

    if ($Context.Mode -eq "local-temp-db" -and $Context.TempDatabase) {
        try {
            Invoke-PsqlAdmin -HostName $Context.Host -Port $Context.Port `
                -AdminUser $Context.AdminUser -AdminPassword $Context.AdminPassword `
                -Database "postgres" -Sql "DROP DATABASE IF EXISTS `"$($Context.TempDatabase)`" WITH (FORCE);"
        }
        catch {
            Write-Host "Warning: failed to drop temp database $($Context.TempDatabase): $_" -ForegroundColor Yellow
        }
    }
}

function Set-PostgresEnv {
    param([object]$Context)

    $url = "postgres://$($Context.User):$($Context.Password)@$($Context.Host):$($Context.Port)/$($Context.Database)"
    $env:POSTGRES_HOST = $Context.Host
    $env:POSTGRES_PORT = "$($Context.Port)"
    $env:POSTGRES_USER = $Context.User
    $env:POSTGRES_PASSWORD = $Context.Password
    $env:POSTGRES_DB = $Context.Database
    $env:DATABASE_URL = $url
    return $url
}

function Invoke-Gate {
    param(
        [string]$Name,
        [scriptblock]$Action,
        [string]$SkipReason = ""
    )

    # PASS iff the scriptblock completes without throwing.
    # Do not inspect $LASTEXITCODE here — a prior native command can leave a stale value.
    # Wrappers (Invoke-FrontendNpm, Invoke-PythonGate, and each gate's own checks) throw on failure.

    if ($SkipReason) {
        Write-GateLine -Name $Name -Status "SKIP" -Reason $SkipReason
        return $true
    }

    if ($Script:FailFastAbort) {
        Write-GateLine -Name $Name -Status "SKIP" -Reason "earlier gate failed (fail-fast)"
        return $false
    }

    try {
        & $Action
        Write-GateLine -Name $Name -Status "PASS"
        return $true
    }
    catch {
        Write-GateLine -Name $Name -Status "FAIL" -Reason $_.Exception.Message
        return $false
    }
}

function Invoke-FrontendNpm {
    param(
        [string[]]$Arguments,
        [hashtable]$ExtraEnv = @{}
    )

    Push-Location $Script:FrontendDir
    $saved = @{}
    try {
        foreach ($key in $ExtraEnv.Keys) {
            $saved[$key] = (Get-Item -Path "Env:$key" -ErrorAction SilentlyContinue).Value
            Set-Item -Path "Env:$key" -Value $ExtraEnv[$key]
        }
        & npm @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "npm $($Arguments -join ' ') failed with exit $LASTEXITCODE"
        }
    }
    finally {
        foreach ($key in $saved.Keys) {
            if ($null -eq $saved[$key]) {
                Remove-Item "Env:$key" -ErrorAction SilentlyContinue
            }
            else {
                Set-Item -Path "Env:$key" -Value $saved[$key]
            }
        }
        Pop-Location
    }
}

function Invoke-PythonGate {
    param([string[]]$Arguments)

    Push-Location $Script:RepoRoot
    try {
        & $Script:PythonExe @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "python $($Arguments -join ' ') failed with exit $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

function Install-CanonicalPythonDeps {
    param([string[]]$Packages)

    & $Script:PythonExe -m pip install --disable-pip-version-check -q @Packages
    if ($LASTEXITCODE -ne 0) {
        throw "pip install failed"
    }
}

function Write-Summary {
    $failed = @($Script:GateResults | Where-Object { $_.Status -eq "FAIL" })
    $skipped = @($Script:GateResults | Where-Object { $_.Status -eq "SKIP" })
    $passed = @($Script:GateResults | Where-Object { $_.Status -eq "PASS" })

    Write-Host ""
    Write-Host "=== Canonical verification summary ==="
    Write-Host ("PASS: {0}  FAIL: {1}  SKIP: {2}" -f $passed.Count, $failed.Count, $skipped.Count)
    if ($failed.Count -gt 0) {
        Write-Host ""
        Write-Host "Failed gates:" -ForegroundColor Red
        foreach ($gate in $failed) {
            Write-Host ("  - {0}: {1}" -f $gate.Name, $gate.Reason) -ForegroundColor Red
        }
    }
    if ($skipped.Count -gt 0) {
        Write-Host ""
        Write-Host "Skipped gates:" -ForegroundColor Yellow
        foreach ($gate in $skipped) {
            Write-Host ("  - {0}: {1}" -f $gate.Name, $gate.Reason) -ForegroundColor Yellow
        }
    }
}

if ($MyInvocation.InvocationName -eq '.') {
    return
}

# --- Preconditions ---
if (-not (Test-Path -LiteralPath $Script:PythonExe)) {
    Write-Error "venv not found at $Script:PythonExe — create venv and install project deps first."
    exit $EXIT_FAIL
}
if (-not (Test-Path -LiteralPath (Join-Path $Script:FrontendDir "package.json"))) {
    Write-Error "frontend/package.json not found."
    exit $EXIT_FAIL
}

$allPassed = $true

$Script:SchemaPostgresContext = $null
$Script:E2ePostgresContext = $null

try {
    Write-Host "Canonical verification @ $Script:RepoRoot"
    Write-Host "Base=$Base Target=$Target"
    Write-Host ""

    # --- Preflight ---
    $ok = Invoke-Gate -Name "preflight working tree clean" -Action {
        $status = & git -C $Script:RepoRoot status --porcelain
        if ($LASTEXITCODE -ne 0) { throw "git status failed" }
        if ($status) { throw "working tree is not clean" }
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "preflight git available" -Action {
        if (-not (Test-CommandAvailable "git")) { throw "git not found" }
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "preflight npm available" -Action {
        if (-not (Test-CommandAvailable "npm")) { throw "npm not found" }
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "preflight PostgreSQL backend" -Action {
        $dockerOk = $false
        if (Test-CommandAvailable "docker") {
            & docker info 2>$null | Out-Null
            $dockerOk = ($LASTEXITCODE -eq 0)
        }
        if ($dockerOk) { return }
        $dotenv = Read-DotEnv (Join-Path $Script:RepoRoot ".env")
        $hostName = if ($dotenv.POSTGRES_HOST) { $dotenv.POSTGRES_HOST } else { "127.0.0.1" }
        $port = if ($dotenv.POSTGRES_PORT) { [int]$dotenv.POSTGRES_PORT } else { 5432 }
        if (-not (Test-TcpPortOpen -HostName $hostName -Port $port)) {
            throw "Docker unavailable and local PostgreSQL not reachable at ${hostName}:${port}"
        }
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "preflight Python 3.12" -Action {
        $ver = & $Script:PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        if (-not (Test-VersionAtLeast $ver 3 12)) { throw "Python $ver (need >= 3.12)" }
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "preflight Node 24" -Action {
        $ver = (& node --version).TrimStart('v')
        if (-not (Test-VersionAtLeast $ver 24 0)) { throw "Node $ver (need >= 24)" }
    }
    if (-not $ok) { $allPassed = $false }

    if (Test-WorkflowsChanged -Base $Base) {
        $ok = Invoke-Gate -Name "preflight actionlint" -Action {
            if (-not (Test-CommandAvailable "actionlint")) {
                throw "actionlint not installed (.github/workflows changed)"
            }
            & actionlint (Join-Path $Script:RepoRoot ".github\workflows\ci.yml")
            if ($LASTEXITCODE -ne 0) { throw "actionlint failed" }
        }
        if (-not $ok) { $allPassed = $false }
    }

    # --- mobile release gate ---
    $ok = Invoke-Gate -Name "mobile release gate" -Action {
        Push-Location $Script:RepoRoot
        try {
            & (Join-Path $Script:RepoRoot "scripts\check_mobile_release_gate.ps1") -Base $Base -Target $Target
            if ($LASTEXITCODE -ne 0) { throw "mobile release gate failed with exit $LASTEXITCODE" }
        }
        finally {
            Pop-Location
        }
    }
    if (-not $ok) { $allPassed = $false }

    # --- mobile conformance (ci.yml mobile-product-contract job) ---
    $ok = Invoke-Gate -Name "mobile product contract and shared OpenAPI" -Action {
        Invoke-PythonGate @("scripts/validate_mobile_product_contract.py")

        $jsonRouteCheck = @'
import json
from pathlib import Path

root = Path.cwd()

def load_json(relative: str):
    path = root / relative
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise SystemExit(f"missing JSON artifact: {relative}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON artifact {relative}: {exc}") from exc

shared = load_json("contracts/openapi/fastapi.json")
frontend = load_json("frontend/openapi.json")
if shared != frontend:
    raise SystemExit(
        "shared OpenAPI drift: contracts/openapi/fastapi.json != frontend/openapi.json"
    )
if not isinstance(shared.get("paths"), dict) or not shared["paths"]:
    raise SystemExit("shared OpenAPI must contain a non-empty paths object")

contract = load_json("contracts/product-contract.json")
route_entries = contract.get("navigation", {}).get("routes", [])
declared = [entry.get("path") for entry in route_entries if isinstance(entry, dict)]
if len(declared) != len(set(declared)):
    raise SystemExit("Mobile Product Contract contains duplicate route paths")

app_root = root / "mobile" / "src" / "app"
physical = sorted(
    path.relative_to(app_root).with_suffix("").as_posix()
    for path in app_root.rglob("*")
    if path.is_file()
    and path.suffix in {".ts", ".tsx"}
    and "__tests__" not in path.parts
    and path.stem != "_layout"
)
if sorted(declared) != physical:
    missing = sorted(set(physical) - set(declared))
    stale = sorted(set(declared) - set(physical))
    raise SystemExit(
        f"Mobile route inventory drift: missing declarations={missing}, stale declarations={stale}"
    )
print(f"Shared OpenAPI JSON and {len(physical)} mobile routes: PASS")
'@
        Invoke-PythonGate @("-c", $jsonRouteCheck)
    }
    if (-not $ok) { $allPassed = $false }

    # --- python lightweight (ci.yml python job) ---
    $ok = Invoke-Gate -Name "python install check deps" -Action {
        Install-CanonicalPythonDeps @(
            "ruff", "pytest", "pytest-asyncio",
            "fastapi", "httpx", "sqlalchemy", "python-multipart",
            "certifi", "beautifulsoup4", "PyYAML", "cryptography",
            "nest-asyncio", "google-generativeai", "spotipy", "yt-dlp", "py7zr",
            "python-dateutil", "croniter", "tzdata", "itsdangerous", "PyJWT", "bcrypt", "aiosqlite",
            "mcp[cli]>=1.0.0,<2.0.0"
        )
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "python MCP import smoke" -Action {
        Invoke-PythonGate @(
            "-c",
            "from importlib.metadata import version; from mcp.server.fastmcp import FastMCP; print(version('mcp')); FastMCP('compat-smoke')"
        )
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "python ruff check" -Action {
        & $Script:PythonExe -m ruff check src tests scripts
        if ($LASTEXITCODE -ne 0) { throw "ruff check failed" }
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "python security boundary tests" -Action {
        Invoke-PythonGate @(
            "-m", "pytest", "-q", "-o", "addopts=",
            "tests/test_egress_security_routes.py",
            "tests/test_video_http_server.py",
            "tests/test_notification_webhook_security.py"
        )
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "python wheel import smoke" -Action {
        $wheelhouse = Join-Path $env:TEMP "aoitalk-wheelhouse-$PID"
        $smokeVenv = Join-Path $env:TEMP "aoitalk-wheel-smoke-$PID"
        if (Test-Path $wheelhouse) { Remove-Item -Recurse -Force $wheelhouse }
        if (Test-Path $smokeVenv) { Remove-Item -Recurse -Force $smokeVenv }
        New-Item -ItemType Directory -Path $wheelhouse -Force | Out-Null

        Push-Location $Script:RepoRoot
        try {
            & $Script:PythonExe -m pip wheel --no-deps --wheel-dir $wheelhouse .
            if ($LASTEXITCODE -ne 0) { throw "pip wheel failed" }
            & $Script:PythonExe -m venv $smokeVenv
            if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
            $wheel = Get-ChildItem -Path $wheelhouse -Filter "aoitalk-*.whl" | Select-Object -First 1
            if (-not $wheel) { throw "aoitalk wheel not found" }
            $smokePy = (Resolve-Path (Join-Path $smokeVenv "Scripts\python.exe")).Path
            & $smokePy -m pip install --disable-pip-version-check -q --no-deps $wheel.FullName
            if ($LASTEXITCODE -ne 0) { throw "wheel install failed" }
            & $smokePy -I -c "import src; import src.api; print(src.__file__); print(src.api.__file__)"
            if ($LASTEXITCODE -ne 0) { throw "isolated import failed" }
        }
        finally {
            Pop-Location
            if (-not $KeepPostgres) {
                Remove-Item -Recurse -Force $wheelhouse -ErrorAction SilentlyContinue
                Remove-Item -Recurse -Force $smokeVenv -ErrorAction SilentlyContinue
            }
        }
    }
    if (-not $ok) { $allPassed = $false }

    # --- backend full pytest ---
    $ok = Invoke-Gate -Name "backend install test deps" -Action {
        & $Script:PythonExe -m pip install --disable-pip-version-check -q ".[test]"
        if ($LASTEXITCODE -ne 0) { throw "pip install .[test] failed" }
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "backend full pytest" -Action {
        Invoke-PythonGate @("-m", "pytest", "-q", "-o", "addopts=")
    }
    if (-not $ok) { $allPassed = $false }

    # --- frontend (ci.yml frontend job) ---
    $ok = Invoke-Gate -Name "frontend npm ci" -Action {
        Invoke-FrontendNpm -Arguments @("ci")
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "frontend lint" -Action {
        Invoke-FrontendNpm -Arguments @("run", "lint")
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "frontend typecheck" -Action {
        Invoke-FrontendNpm -Arguments @("exec", "tsc", "--noEmit")
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "frontend typegen drift" -Action {
        Push-Location $Script:FrontendDir
        try {
            & npm run typegen
            if ($LASTEXITCODE -ne 0) { throw "npm run typegen failed" }
            & git diff --exit-code -- src/lib/api-types.gen.ts openapi.json
            if ($LASTEXITCODE -ne 0) { throw "typegen drift detected" }
        }
        finally {
            Pop-Location
        }
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "mobile generated API typegen drift" -Action {
        Push-Location $Script:FrontendDir
        try {
            # Reuse frontend's npm ci installation; do not install a second
            # dependency tree solely for the native generated contract.
            & node (Join-Path $Script:RepoRoot "mobile\scripts\generate-api-types.mjs")
            if ($LASTEXITCODE -ne 0) { throw "mobile API typegen failed" }
            Push-Location $Script:RepoRoot
            try {
                & git diff --exit-code -- "mobile/src/types/api-types.gen.ts"
                if ($LASTEXITCODE -ne 0) { throw "mobile generated API typegen drift detected" }
            }
            finally {
                Pop-Location
            }
        }
        finally {
            Pop-Location
        }
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "frontend vitest" -Action {
        Invoke-FrontendNpm -Arguments @("exec", "vitest", "run")
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "frontend build" -Action {
        Invoke-FrontendNpm -Arguments @("run", "build") -ExtraEnv $CiFrontendEnv
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "frontend build:production" -Action {
        Invoke-FrontendNpm -Arguments @("run", "build:production") -ExtraEnv $CiFrontendEnv
    }
    if (-not $ok) { $allPassed = $false }

    $ok = Invoke-Gate -Name "frontend verify BUILD_ID" -Action {
        $buildId = Join-Path $Script:FrontendDir ".next\BUILD_ID"
        if (-not (Test-Path -LiteralPath $buildId)) {
            throw ".next/BUILD_ID missing"
        }
        if ((Get-Item -LiteralPath $buildId).Length -le 0) {
            throw ".next/BUILD_ID is empty"
        }
    }
    if (-not $ok) { $allPassed = $false }

    # --- schema drift (fresh PostgreSQL, ci.yml schema-drift job) ---
    if ($Script:FailFastAbort) {
        $null = Invoke-Gate -Name "schema drift (skipped)" -SkipReason "earlier gate failed (fail-fast)"
    }
    else {
    $schemaPgUrl = $null
    try {
        $Script:SchemaPostgresContext = Start-CanonicalPostgres
        $schemaPgUrl = Set-PostgresEnv -Context $Script:SchemaPostgresContext
        Write-Host ("Schema drift PostgreSQL ready at $schemaPgUrl") -ForegroundColor Cyan

        $ok = Invoke-Gate -Name "schema drift install migration deps" -Action {
            Install-CanonicalPythonDeps @(
                "alembic", "sqlalchemy>=2", "psycopg2-binary", "python-dotenv", "cryptography"
            )
        }
        if (-not $ok) { $allPassed = $false }

        $ok = Invoke-Gate -Name "schema drift alembic upgrade head" -Action {
            Push-Location $Script:RepoRoot
            try {
                & $Script:PythonExe -m alembic upgrade head
                if ($LASTEXITCODE -ne 0) { throw "alembic upgrade head failed" }
            }
            finally {
                Pop-Location
            }
        }
        if (-not $ok) { $allPassed = $false }

        $ok = Invoke-Gate -Name "schema drift check_schema_drift.py" -Action {
            Invoke-PythonGate @((Join-Path $Script:RepoRoot "scripts\check_schema_drift.py"))
        }
        if (-not $ok) { $allPassed = $false }

        $ok = Invoke-Gate -Name "schema drift login-log postgres vitest" -Action {
            Invoke-FrontendNpm -Arguments @("exec", "vitest", "run", "src/lib/server/__tests__/login-log.postgres.test.ts") -ExtraEnv @{
                AOITALK_TEST_POSTGRES = "true"
                DATABASE_URL          = $schemaPgUrl
            }
        }
        if (-not $ok) { $allPassed = $false }
    }
    catch {
        Write-GateLine -Name "schema drift PostgreSQL" -Status "FAIL" -Reason $_.Exception.Message
        $allPassed = $false
    }
    finally {
        Stop-CanonicalPostgres -Context $Script:SchemaPostgresContext
        $Script:SchemaPostgresContext = $null
    }
    }

    # --- frontend E2E (separate fresh PostgreSQL, ci.yml frontend-e2e job) ---
    if ($SkipE2E) {
        $null = Invoke-Gate -Name "frontend E2E Playwright" -SkipReason "-SkipE2E specified (not full CI parity)"
    }
    elseif ($Script:FailFastAbort) {
        $null = Invoke-Gate -Name "frontend E2E (skipped)" -SkipReason "earlier gate failed (fail-fast)"
    }
    else {
        $e2ePgUrl = $null
        try {
            $Script:E2ePostgresContext = Start-CanonicalPostgres
            $e2ePgUrl = Set-PostgresEnv -Context $Script:E2ePostgresContext
            Write-Host ("E2E PostgreSQL ready at $e2ePgUrl") -ForegroundColor Cyan

            $ok = Invoke-Gate -Name "frontend E2E alembic upgrade head" -Action {
                Push-Location $Script:RepoRoot
                try {
                    & $Script:PythonExe -m alembic upgrade head
                    if ($LASTEXITCODE -ne 0) { throw "alembic upgrade head failed" }
                }
                finally {
                    Pop-Location
                }
            }
            if (-not $ok) { $allPassed = $false }

            $ok = Invoke-Gate -Name "frontend E2E build:production" -Action {
                Invoke-FrontendNpm -Arguments @("run", "build:production") -ExtraEnv @{
                    DATABASE_URL             = $e2ePgUrl
                    NEXTAUTH_SECRET          = "ci-e2e-secret-not-real"
                    AUTH_SECRET              = "ci-e2e-secret-not-real"
                    PYTHON_API_URL           = "http://127.0.0.1:3000"
                    PLAYWRIGHT_PORT          = "3020"
                    PLAYWRIGHT_NEXT_DIST_DIR = ".next"
                    NEXT_DIST_DIR            = ".next"
                }
            }
            if (-not $ok) { $allPassed = $false }

            $ok = Invoke-Gate -Name "frontend E2E playwright install chromium" -Action {
                Invoke-FrontendNpm -Arguments @("exec", "playwright", "install", "chromium")
            }
            if (-not $ok) { $allPassed = $false }

            $e2eArgs = @("run", "test:e2e", "--") + $E2ESpecs + @("--grep-invert", "Python API起動時")
            $ok = Invoke-Gate -Name "frontend E2E Playwright" -Action {
                $prevCi = $env:CI
                $prevReuse = $env:PLAYWRIGHT_REUSE_EXISTING_SERVER
                try {
                    $env:CI = "true"
                    $env:PLAYWRIGHT_REUSE_EXISTING_SERVER = "0"
                    Invoke-FrontendNpm -Arguments $e2eArgs -ExtraEnv @{
                        POSTGRES_HOST            = $Script:E2ePostgresContext.Host
                        POSTGRES_PORT            = "$($Script:E2ePostgresContext.Port)"
                        POSTGRES_USER            = $Script:E2ePostgresContext.User
                        POSTGRES_PASSWORD        = $Script:E2ePostgresContext.Password
                        POSTGRES_DB              = $Script:E2ePostgresContext.Database
                        DATABASE_URL             = $e2ePgUrl
                        NEXTAUTH_SECRET          = "ci-e2e-secret-not-real"
                        AUTH_SECRET              = "ci-e2e-secret-not-real"
                        PYTHON_API_URL           = "http://127.0.0.1:3000"
                        PLAYWRIGHT_PORT          = "3020"
                        PLAYWRIGHT_NEXT_DIST_DIR = ".next"
                        NEXT_DIST_DIR            = ".next"
                    }
                }
                finally {
                    if ($null -eq $prevCi) { Remove-Item Env:CI -ErrorAction SilentlyContinue } else { $env:CI = $prevCi }
                    if ($null -eq $prevReuse) { Remove-Item Env:PLAYWRIGHT_REUSE_EXISTING_SERVER -ErrorAction SilentlyContinue } else { $env:PLAYWRIGHT_REUSE_EXISTING_SERVER = $prevReuse }
                }
            }
            if (-not $ok) { $allPassed = $false }
        }
        catch {
            Write-GateLine -Name "frontend E2E PostgreSQL" -Status "FAIL" -Reason $_.Exception.Message
            $allPassed = $false
        }
        finally {
            Stop-CanonicalPostgres -Context $Script:E2ePostgresContext
            $Script:E2ePostgresContext = $null
        }
    }
}
finally {
    Stop-CanonicalPostgres -Context $Script:SchemaPostgresContext
    Stop-CanonicalPostgres -Context $Script:E2ePostgresContext
}

Write-Summary

if ($allPassed) {
    $skipped = @($Script:GateResults | Where-Object { $_.Status -eq "SKIP" })
    if ($skipped.Count -gt 0) {
        Write-Host ""
        Write-Host "All executed gates passed, but some gates were skipped — not full CI parity." -ForegroundColor Yellow
        exit $EXIT_FAIL
    }
    Write-Host ""
    Write-Host "Canonical verification PASS — all gates passed." -ForegroundColor Green
    exit $EXIT_PASS
}

Write-Host ""
Write-Host "Canonical verification FAIL — fix failed gates (remaining gates skipped after first FAIL)." -ForegroundColor Red
exit $EXIT_FAIL
