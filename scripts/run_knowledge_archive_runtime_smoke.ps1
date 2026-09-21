param(
    [switch]$KeepArtifacts
)

$ErrorActionPreference = "Stop"

function Read-DotEnvValue([string]$Name, [string]$Fallback = "") {
    if (-not (Test-Path ".env")) { return $Fallback }
    $line = Get-Content -Encoding UTF8 ".env" |
        Where-Object { $_ -match "^$([regex]::Escape($Name))=" } |
        Select-Object -First 1
    if (-not $line) { return $Fallback }
    return ($line -split "=", 2)[1].Trim().Trim('"')
}

$hostName = Read-DotEnvValue "POSTGRES_HOST" "127.0.0.1"
$port = Read-DotEnvValue "POSTGRES_PORT" "5432"
$database = Read-DotEnvValue "POSTGRES_DB" "aoitalk_memory"
$user = Read-DotEnvValue "POSTGRES_USER" "aoitalk"
$password = Read-DotEnvValue "POSTGRES_PASSWORD" ""
$schema = "aoitalk_gc_runtime_" + ([guid]::NewGuid().ToString("N"))
$portWeb = 3217
$psql = (Get-Command psql -ErrorAction Stop).Source
$repoItem = Get-Item (Join-Path $PSScriptRoot "..")
$repoRoot = if ($repoItem.Target) { $repoItem.Target } else { $repoItem.FullName }
$stdoutPath = Join-Path $env:TEMP "$schema-next.stdout.log"
$stderrPath = Join-Path $env:TEMP "$schema-next.stderr.log"
$server = $null

$env:PGPASSWORD = $password
$encodedUser = [uri]::EscapeDataString($user)
$encodedPassword = [uri]::EscapeDataString($password)
# The temporary schema is a hard isolation boundary.  Falling back to public
# would let a missing table silently read/write the normal runtime.
$encodedOptions = [uri]::EscapeDataString("-csearch_path=$schema")
$databaseUrl = "postgres://$encodedUser`:$encodedPassword@$hostName`:$port/$database`?options=$encodedOptions"

$ddl = @"
CREATE SCHEMA "$schema";
CREATE TABLE "$schema".docs_libraries (
  id uuid PRIMARY KEY
);
CREATE TABLE "$schema".knowledge_nodes (
  id uuid PRIMARY KEY,
  docs_library_id uuid NOT NULL,
  parent_id uuid REFERENCES "$schema".knowledge_nodes(id) ON DELETE CASCADE,
  archived_at timestamp NULL
);
CREATE TABLE "$schema".projects (
  id uuid PRIMARY KEY,
  knowledge_node_id uuid REFERENCES "$schema".knowledge_nodes(id) ON DELETE RESTRICT,
  deleted_at timestamp NULL,
  is_completed boolean NOT NULL DEFAULT false
);
CREATE TABLE "$schema".content_deletion_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_id uuid NOT NULL,
  entity_type varchar(32) NOT NULL,
  entity_id varchar(512) NOT NULL,
  root_entity_id varchar(512),
  project_id uuid,
  actor_user_id uuid,
  action varchar(32) NOT NULL,
  display_name varchar(255),
  source varchar(64),
  event_at timestamp NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE "$schema".conversation_sessions (
  id uuid PRIMARY KEY,
  project_id uuid,
  title text,
  deleted_at timestamp NULL
);
"@

$seed = @"
INSERT INTO "$schema".docs_libraries (id)
VALUES ('00000000-0000-4000-8000-000000000001');
INSERT INTO "$schema".knowledge_nodes (id, docs_library_id, archived_at)
VALUES
  ('00000000-0000-4000-8000-000000000002', '00000000-0000-4000-8000-000000000001', '2025-12-01T00:00:00Z'),
  ('00000000-0000-4000-8000-000000000003', '00000000-0000-4000-8000-000000000001', '2025-12-01T00:00:00Z');
INSERT INTO "$schema".projects (id, knowledge_node_id, deleted_at, is_completed)
VALUES ('00000000-0000-4000-8000-000000000004', '00000000-0000-4000-8000-000000000003', now(), true);
"@

try {
    # Keep schema creation inside the same finally as the runtime process so
    # a failed DDL/seed command cannot leave a disposable schema behind.
    & $psql -h $hostName -p $port -U $user -d $database -X -v ON_ERROR_STOP=1 -P pager=off -c $ddl
    if ($LASTEXITCODE -ne 0) { throw "分離runtimeスキーマの作成に失敗しました" }
    & $psql -h $hostName -p $port -U $user -d $database -X -v ON_ERROR_STOP=1 -P pager=off -c $seed
    if ($LASTEXITCODE -ne 0) { throw "分離runtime fixtureの作成に失敗しました" }

    $serverCommand = "set DATABASE_URL=$databaseUrl&&set INTERNAL_API_KEY=knowledge-gc-smoke&&set NEXT_DIST_DIR=.next-gc-smoke&&npm --prefix frontend run dev -- -p $portWeb"
    $server = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", $serverCommand -WorkingDirectory $repoRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru

    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    $response = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest `
                -Method Post `
                -Uri "http://127.0.0.1:$portWeb/api/internal/content-retention" `
                -Headers @{ "x-internal-auth" = "knowledge-gc-smoke" } `
                -ContentType "application/json" `
                -Body "{}" `
                -UseBasicParsing `
                -TimeoutSec 15
            break
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $response) { throw "runtime endpointが60秒以内に応答しませんでした" }
    if ($response.StatusCode -ne 200) { throw "runtime endpoint status=$($response.StatusCode) body=$($response.Content)" }

    $body = $response.Content | ConvertFrom-Json
    if ([int]$body.docs_purged -ne 1) {
        throw "runtime endpointのdocs_purgedが1ではありません: $($response.Content)"
    }

    $second = Invoke-WebRequest `
        -Method Post `
        -Uri "http://127.0.0.1:$portWeb/api/internal/content-retention" `
        -Headers @{ "x-internal-auth" = "knowledge-gc-smoke" } `
        -ContentType "application/json" `
        -Body "{}" `
        -UseBasicParsing `
        -TimeoutSec 15
    $secondBody = $second.Content | ConvertFrom-Json
    if ([int]$secondBody.docs_purged -ne 0) {
        throw "runtime endpointの2回目docs_purgedが0ではありません: $($second.Content)"
    }

    $verify = & $psql -h $hostName -p $port -U $user -d $database -X -v ON_ERROR_STOP=1 -P pager=off -At -c @"
SELECT
  (SELECT count(*) FROM "$schema".knowledge_nodes WHERE id = '00000000-0000-4000-8000-000000000002') AS free_remaining,
  (SELECT count(*) FROM "$schema".knowledge_nodes WHERE id = '00000000-0000-4000-8000-000000000003') AS referenced_remaining,
  (SELECT count(*) FROM "$schema".projects WHERE knowledge_node_id = '00000000-0000-4000-8000-000000000003') AS pointer_count,
  (SELECT count(*) FROM "$schema".content_deletion_events WHERE source = 'web.docs.archive_cleanup') AS purge_events;
"@
    if ($LASTEXITCODE -ne 0) { throw "runtime後のDB確認に失敗しました" }
    Write-Output "runtime_response=$($response.Content)"
    Write-Output "runtime_second_response=$($second.Content)"
    Write-Output "runtime_db=$verify"
    $logText = ""
    if (Test-Path $stdoutPath) { $logText += Get-Content -Raw -Encoding UTF8 $stdoutPath }
    if (Test-Path $stderrPath) { $logText += Get-Content -Raw -Encoding UTF8 $stderrPath }
    if ($logText) {
        if ($logText -match "23503|ResponseAborted") {
            throw "runtime smoke logに23503またはResponseAbortedが残っています: $stdoutPath / $stderrPath"
        }
    }
}
finally {
    if ($server) {
        try { & taskkill.exe /PID $server.Id /T /F *> $null } catch { }
    }
    if ($KeepArtifacts) {
        Write-Output "runtime_stdout_log=$stdoutPath"
        Write-Output "runtime_stderr_log=$stderrPath"
    }
    else {
        if (Test-Path $stdoutPath) { Remove-Item -LiteralPath $stdoutPath -Force -ErrorAction SilentlyContinue }
        if (Test-Path $stderrPath) { Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue }
    }
    $distPath = Join-Path $repoRoot "frontend\.next-gc-smoke"
    if (Test-Path $distPath) {
        Remove-Item -LiteralPath $distPath -Recurse -Force -ErrorAction SilentlyContinue
    }
    $dropSql = 'DROP SCHEMA IF EXISTS "' + $schema + '" CASCADE;'
    & $psql -h $hostName -p $port -U $user -d $database -X -v ON_ERROR_STOP=1 -P pager=off -c $dropSql
}
