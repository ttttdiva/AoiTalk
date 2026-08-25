[CmdletBinding()]
param(
    [string]$Database = "aoitalk_memory",
    [string]$HostName = "localhost",
    [int]$Port = 5432,
    [string]$User = "postgres",
    [string]$OutputDirectory = (
        Join-Path ([Environment]::GetFolderPath("Desktop")) "AoiTalk_DB_Dumps"
    )
)

$ErrorActionPreference = "Stop"

function Find-PgDump {
    # PATH にある pg_dump を最優先
    $command = Get-Command "pg_dump.exe" -ErrorAction SilentlyContinue

    if ($command) {
        return $command.Source
    }

    # PATH に無ければ標準インストール先を探す
    $postgresRoot = Join-Path $env:ProgramFiles "PostgreSQL"

    if (Test-Path $postgresRoot) {
        $versionDirs = Get-ChildItem $postgresRoot -Directory |
            Where-Object { $_.Name -match '^\d+$' } |
            Sort-Object { [int]$_.Name } -Descending

        foreach ($dir in $versionDirs) {
            $candidate = Join-Path $dir.FullName "bin\pg_dump.exe"

            if (Test-Path $candidate) {
                return $candidate
            }
        }
    }

    throw @"
pg_dump.exe が見つかりませんでした。

PostgreSQL の bin フォルダを PATH に追加するか、
PostgreSQL が正しくインストールされているか確認してください。
"@
}


# ------------------------------------------------------------
# pg_dump 検出
# ------------------------------------------------------------

$pgDump = Find-PgDump

# ------------------------------------------------------------
# 出力先
# ------------------------------------------------------------

New-Item `
    -ItemType Directory `
    -Force `
    -Path $OutputDirectory |
    Out-Null

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"

$outputFile = Join-Path `
    $OutputDirectory `
    "${Database}_${timestamp}.sql"


# ------------------------------------------------------------
# 情報表示
# ------------------------------------------------------------

Write-Host ""
Write-Host "AoiTalk PostgreSQL Full Export"
Write-Host "----------------------------------------"
Write-Host "pg_dump : $pgDump"
Write-Host "Host    : $HostName"
Write-Host "Port    : $Port"
Write-Host "Database: $Database"
Write-Host "User    : $User"
Write-Host "Output  : $outputFile"
Write-Host "----------------------------------------"
Write-Host ""


# ------------------------------------------------------------
# FULL DUMP
#
# schema
# data
# sequences
# indexes
# constraints
# functions
# triggers
# extensions
# large objects
# database-level metadata (--create)
#
# を可能な限りそのまま保存する。
#
# --no-owner / --no-privileges は意図的に指定しない。
# 調査用なので所有者・権限情報も残す。
# ------------------------------------------------------------

$arguments = @(
    "--host=$HostName"
    "--port=$Port"
    "--username=$User"
    "--format=plain"
    "--create"
    "--large-objects"
    "--verbose"
    "--file=$outputFile"
    $Database
)

& $pgDump @arguments

$exitCode = $LASTEXITCODE


# ------------------------------------------------------------
# エラーチェック
# ------------------------------------------------------------

if ($exitCode -ne 0) {

    # 失敗した不完全なダンプを残さない
    if (Test-Path $outputFile) {
        Remove-Item -Force $outputFile
    }

    throw "pg_dump に失敗しました。Exit code: $exitCode"
}


# ------------------------------------------------------------
# 完了情報
# ------------------------------------------------------------

$file = Get-Item $outputFile
$sizeMB = [math]::Round($file.Length / 1MB, 2)

$hash = Get-FileHash `
    -Algorithm SHA256 `
    -Path $outputFile

Write-Host ""
Write-Host "========================================"
Write-Host "Export completed."
Write-Host "========================================"
Write-Host "File   : $outputFile"
Write-Host "Size   : $sizeMB MB"
Write-Host "SHA256 : $($hash.Hash)"
Write-Host ""

# エクスプローラーで完成ファイルを選択して表示
Start-Process `
    "explorer.exe" `
    "/select,`"$outputFile`""