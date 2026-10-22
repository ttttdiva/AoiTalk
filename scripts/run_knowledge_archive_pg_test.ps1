param(
    [string]$FixtureDatabaseUrl = "",
    [switch]$KeepSchema
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

if ($FixtureDatabaseUrl) {
    $fixture = [uri]$FixtureDatabaseUrl
    if ($fixture.Scheme -notin @("postgres", "postgresql") -or -not $fixture.Host) {
        throw "FixtureDatabaseUrl must be a PostgreSQL connection URL"
    }
    $hostName = $fixture.Host
    $port = if ($fixture.Port -gt 0) { $fixture.Port } else { 5432 }
    $database = [uri]::UnescapeDataString($fixture.AbsolutePath.TrimStart('/'))
    $credentials = $fixture.UserInfo -split ':', 2
    $user = [uri]::UnescapeDataString($credentials[0])
    $password = if ($credentials.Count -gt 1) { [uri]::UnescapeDataString($credentials[1]) } else { "" }
    if (-not $database -or -not $user) { throw "FixtureDatabaseUrl requires a database and user" }
} else {
    $hostName = Read-DotEnvValue "POSTGRES_HOST" "127.0.0.1"
    $port = Read-DotEnvValue "POSTGRES_PORT" "5432"
    $database = Read-DotEnvValue "POSTGRES_DB" "aoitalk_memory"
    $user = Read-DotEnvValue "POSTGRES_USER" "aoitalk"
    $password = Read-DotEnvValue "POSTGRES_PASSWORD" ""
}
$schema = "aoitalk_gc_" + ([guid]::NewGuid().ToString("N"))
$psql = (Get-Command psql -ErrorAction Stop).Source

$env:PGPASSWORD = $password
$env:AOITALK_PG_INTEGRATION = "1"
$env:AOITALK_PG_TEST_SCHEMA = $schema
$env:POSTGRES_SCHEMA = $schema

# postgres.js applies libpq's `options` query parameter when opening each
# pooled connection.  The schema is created before the first application
# query, so every connection used by the real purge function sees the same
# isolated tables instead of the user's public application data.
$encodedUser = [uri]::EscapeDataString($user)
$encodedPassword = [uri]::EscapeDataString($password)
$encodedOptions = [uri]::EscapeDataString("-csearch_path=$schema")
$env:DATABASE_URL = "postgres://$encodedUser`:$encodedPassword@$hostName`:$port/$database`?options=$encodedOptions"

$ddl = @"
CREATE SCHEMA "$schema";
CREATE TABLE "$schema".knowledge_nodes (
  id uuid PRIMARY KEY,
  project_id uuid NULL,
  docs_library_id uuid NOT NULL,
  parent_id uuid,
  archived_at timestamp NULL,
  system_key varchar(255) NULL,
  display_props jsonb NOT NULL DEFAULT '{}'::jsonb
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
CREATE TABLE "$schema".knowledge_revisions (
  id uuid PRIMARY KEY,
  node_id uuid NOT NULL
);
CREATE TABLE "$schema".knowledge_capture_candidates (
  id uuid PRIMARY KEY,
  status varchar(32) NOT NULL DEFAULT 'published',
  published_node_id uuid CONSTRAINT fk_knowledge_capture_candidates_published_node
    REFERENCES "$schema".knowledge_nodes(id) ON DELETE SET NULL,
  target_node_id uuid CONSTRAINT fk_knowledge_capture_candidates_target_node
    REFERENCES "$schema".knowledge_nodes(id) ON DELETE SET NULL,
  target_revision_id uuid CONSTRAINT fk_knowledge_capture_candidates_target_revision
    REFERENCES "$schema".knowledge_revisions(id) ON DELETE SET NULL,
  published_revision_id uuid CONSTRAINT fk_knowledge_capture_candidates_published_revision
    REFERENCES "$schema".knowledge_revisions(id) ON DELETE SET NULL
);
-- Match the observed production trigger order: capture SET NULL triggers run
-- before structural/revision CASCADE triggers. Generated RI trigger names are
-- sorted lexically, so migration OID digit changes can alter this ordering.
ALTER TABLE "$schema".knowledge_nodes ADD FOREIGN KEY (parent_id)
  REFERENCES "$schema".knowledge_nodes(id) ON DELETE CASCADE;
ALTER TABLE "$schema".knowledge_revisions ADD FOREIGN KEY (node_id)
  REFERENCES "$schema".knowledge_nodes(id) ON DELETE CASCADE;
"@

try {
    & $psql -h $hostName -p $port -U $user -d $database -X -v ON_ERROR_STOP=1 -P pager=off -c $ddl
    if ($LASTEXITCODE -ne 0) { throw "分離PostgreSQLスキーマの作成に失敗しました" }
    Push-Location (Join-Path $PSScriptRoot "..")
    try {
        & npm --prefix frontend test -- --run src/lib/server/__tests__/knowledge-docs-archive-gc.pg.test.ts
        if ($LASTEXITCODE -ne 0) { throw "Knowledge archive PostgreSQL regression test failed" }
    }
    finally {
        Pop-Location
    }
}
finally {
    if (-not $KeepSchema) {
        $dropSql = 'DROP SCHEMA IF EXISTS "' + $schema + '" CASCADE;'
        & $psql -h $hostName -p $port -U $user -d $database -X -v ON_ERROR_STOP=1 -P pager=off -c $dropSql
        if ($LASTEXITCODE -ne 0) { throw "分離PostgreSQLスキーマの削除に失敗しました" }
    }
}
