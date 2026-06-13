param(
  [switch]$Apply
)

$ErrorActionPreference = "Stop"

function Import-DotEnv($Path) {
  if (-not (Test-Path -LiteralPath $Path)) { return }
  foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
    if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
      continue
    }
    $name = $Matches[1]
    if ([Environment]::GetEnvironmentVariable($name, "Process")) {
      continue
    }
    $value = $Matches[2].Trim()
    if (
      ($value.StartsWith('"') -and $value.EndsWith('"')) -or
      ($value.StartsWith("'") -and $value.EndsWith("'"))
    ) {
      $value = $value.Substring(1, $value.Length - 2)
    }
    [Environment]::SetEnvironmentVariable($name, $value, "Process")
  }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Import-DotEnv (Join-Path $repoRoot ".env")

$psql = (Get-Command psql -ErrorAction SilentlyContinue)
if (-not $psql) {
  $defaultPsql = "C:\Program Files\PostgreSQL\16\bin\psql.exe"
  if (Test-Path -LiteralPath $defaultPsql) {
    $psqlPath = $defaultPsql
  } else {
    throw "psql was not found."
  }
} else {
  $psqlPath = $psql.Source
}

$env:PGHOST = $env:POSTGRES_HOST
if (-not $env:PGHOST) { $env:PGHOST = "127.0.0.1" }
$env:PGPORT = $env:POSTGRES_PORT
if (-not $env:PGPORT) { $env:PGPORT = "5432" }
$env:PGUSER = $env:POSTGRES_USER
if (-not $env:PGUSER) { $env:PGUSER = "aoitalk" }
$env:PGDATABASE = $env:POSTGRES_DB
if (-not $env:PGDATABASE) { $env:PGDATABASE = "aoitalk_memory" }
if ($env:POSTGRES_PASSWORD) { $env:PGPASSWORD = $env:POSTGRES_PASSWORD }

$previewSql = @"
with task_candidates as (
  select
    id,
    start_at,
    end_at,
    case when start_at is null then null else date_trunc('day', start_at + interval '9 hours') end as next_start_at,
    case when end_at is null then null else date_trunc('day', end_at + interval '9 hours') end as next_end_at
  from tasks
  where all_day is true
),
occurrence_candidates as (
  select
    id,
    start_at,
    end_at,
    case when start_at is null then null else date_trunc('day', start_at + interval '9 hours') end as next_start_at,
    case when end_at is null then null else date_trunc('day', end_at + interval '9 hours') end as next_end_at
  from task_occurrences
  where all_day is true
)
select 'tasks' as table_name, count(*) as rows_to_update
from task_candidates
where start_at is distinct from next_start_at or end_at is distinct from next_end_at
union all
select 'task_occurrences' as table_name, count(*) as rows_to_update
from occurrence_candidates
where start_at is distinct from next_start_at or end_at is distinct from next_end_at;
"@

& $psqlPath -v ON_ERROR_STOP=1 -c $previewSql

if (-not $Apply) {
  Write-Host "Dry-run only. Re-run with -Apply to update date-only task timestamps."
  exit 0
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupTasks = "task_date_only_repair_tasks_$stamp"
$backupOccurrences = "task_date_only_repair_occurrences_$stamp"

$applySql = @"
begin;

create table $backupTasks as
select * from tasks;

create table $backupOccurrences as
select * from task_occurrences;

with candidates as (
  select
    id,
    case when start_at is null then null else date_trunc('day', start_at + interval '9 hours') end as next_start_at,
    case when end_at is null then null else date_trunc('day', end_at + interval '9 hours') end as next_end_at
  from tasks
  where all_day is true
)
update tasks as t
set
  start_at = c.next_start_at,
  end_at = c.next_end_at,
  updated_at = now()
from candidates as c
where t.id = c.id
  and (t.start_at is distinct from c.next_start_at or t.end_at is distinct from c.next_end_at);

with candidates as (
  select
    id,
    case when start_at is null then null else date_trunc('day', start_at + interval '9 hours') end as next_start_at,
    case when end_at is null then null else date_trunc('day', end_at + interval '9 hours') end as next_end_at
  from task_occurrences
  where all_day is true
)
update task_occurrences as o
set
  start_at = c.next_start_at,
  end_at = c.next_end_at,
  updated_at = now()
from candidates as c
where o.id = c.id
  and (o.start_at is distinct from c.next_start_at or o.end_at is distinct from c.next_end_at);

commit;
"@

& $psqlPath -v ON_ERROR_STOP=1 -c $applySql
Write-Host "Backups created: $backupTasks, $backupOccurrences"
