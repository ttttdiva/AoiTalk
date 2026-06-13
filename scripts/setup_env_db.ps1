# AoiTalk セットアップ補助スクリプト (setup.bat から呼び出される)
# 1. .env が無ければ .env.sample から生成し、空の認証シークレットを自動生成する
# 2. .env の POSTGRES_* 設定に従って PostgreSQL のユーザー/データベース/pgvector を初期化する
#
# superuser (postgres) のパスワードは環境変数 PGPASSWORD があればそれを使い、
# 無ければプロンプトで入力を求める。

$ErrorActionPreference = "Stop"

# リポジトリルート (このスクリプトは scripts/ 配下に置かれる)
$rootDir = Split-Path -Parent $PSScriptRoot
Set-Location $rootDir

$envFile = Join-Path $rootDir ".env"
$sampleFile = Join-Path $rootDir ".env.sample"

function New-RandomSecret {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ([System.BitConverter]::ToString($bytes) -replace '-', '').ToLower()
}

# ---------------------------------------------------------------------------
# 1. .env 生成
# ---------------------------------------------------------------------------
if (-not (Test-Path -LiteralPath $envFile)) {
    if (-not (Test-Path -LiteralPath $sampleFile)) {
        throw ".env.sample が見つかりません: $sampleFile"
    }
    Copy-Item -LiteralPath $sampleFile -Destination $envFile
    Write-Host "[env] .env を .env.sample から作成しました。APIキー等は必要に応じて .env を編集してください。"
} else {
    Write-Host "[env] 既存の .env を使用します。"
}

# 空の認証シークレットを自動生成する (手動で用意した .env でも空なら補完する)
$content = [System.IO.File]::ReadAllText($envFile, [System.Text.Encoding]::UTF8)
$contentChanged = $false
foreach ($key in @("NEXTAUTH_SECRET", "AOITALK_WEB_AUTH_SECRET", "AOITALK_JWT_SECRET", "INTERNAL_API_KEY")) {
    # CRLF 改行でも一致するように行末は lookahead で判定する
    $pattern = "(?m)^" + [regex]::Escape($key) + "=[ \t]*(?=\r?$)"
    if ($content -match $pattern) {
        $content = [regex]::Replace($content, $pattern, ($key + "=" + (New-RandomSecret)))
        $contentChanged = $true
        Write-Host "[env] $key を自動生成しました。"
    }
}
if ($contentChanged) {
    [System.IO.File]::WriteAllText($envFile, $content, (New-Object System.Text.UTF8Encoding($false)))
}

# ---------------------------------------------------------------------------
# 2. .env から POSTGRES_* を読み取り
# ---------------------------------------------------------------------------
$envValues = @{}
foreach ($line in [System.IO.File]::ReadAllLines($envFile, [System.Text.Encoding]::UTF8)) {
    if ($line -match '^\s*#') { continue }
    if ($line -match '^\s*([A-Za-z0-9_]+)\s*=\s*(.*)$') {
        $value = $Matches[2].Trim()
        # 行末コメントを除去 (値にスペース+#が含まれる場合)
        $hashIndex = $value.IndexOf(" #")
        if ($hashIndex -ge 0) { $value = $value.Substring(0, $hashIndex).Trim() }
        $envValues[$Matches[1]] = $value
    }
}

function Get-EnvValue([string]$Key, [string]$Default) {
    if ($envValues.ContainsKey($Key) -and -not [string]::IsNullOrWhiteSpace($envValues[$Key])) {
        return $envValues[$Key]
    }
    return $Default
}

$pgHost = Get-EnvValue "POSTGRES_HOST" "127.0.0.1"
$pgPort = Get-EnvValue "POSTGRES_PORT" "5432"
$dbName = Get-EnvValue "POSTGRES_DB" "aoitalk_memory"
$dbUser = Get-EnvValue "POSTGRES_USER" "aoitalk"
$dbPassword = Get-EnvValue "POSTGRES_PASSWORD" "aoitalk_password"

Write-Host "[db] 接続先: ${pgHost}:${pgPort} / DB=$dbName / USER=$dbUser"

# ---------------------------------------------------------------------------
# 3. psql を探す
# ---------------------------------------------------------------------------
$psql = $null
$cmd = Get-Command psql -ErrorAction SilentlyContinue
if ($cmd) { $psql = $cmd.Source }
if (-not $psql) {
    $pgRoot = "C:\Program Files\PostgreSQL"
    if (Test-Path -LiteralPath $pgRoot) {
        foreach ($dir in (Get-ChildItem -LiteralPath $pgRoot -Directory | Sort-Object Name -Descending)) {
            $candidate = Join-Path $dir.FullName "bin\psql.exe"
            if (Test-Path -LiteralPath $candidate) { $psql = $candidate; break }
        }
    }
}
if (-not $psql) {
    throw "psql が見つかりません。PostgreSQLのインストールを確認してください。"
}

# ---------------------------------------------------------------------------
# 4. superuser パスワード
# ---------------------------------------------------------------------------
if ([string]::IsNullOrEmpty($env:PGPASSWORD)) {
    Write-Host "[db] PostgreSQL superuser (postgres) のパスワードを入力してください。"
    $secure = Read-Host -AsSecureString "postgres password"
    $env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}

function Invoke-PsqlQuery([string]$Database, [string]$Sql) {
    $result = & $psql -v ON_ERROR_STOP=1 -h $pgHost -p $pgPort -U postgres -d $Database -tAc $Sql
    if ($LASTEXITCODE -ne 0) {
        throw "psql の実行に失敗しました (DB=$Database): $Sql"
    }
    return $result
}

# SQL文字列リテラル用エスケープ
$dbPasswordSql = $dbPassword -replace "'", "''"

# ---------------------------------------------------------------------------
# 5. ユーザー/データベース/権限/pgvector を初期化 (冪等)
# ---------------------------------------------------------------------------
$roleExists = Invoke-PsqlQuery "postgres" "SELECT 1 FROM pg_roles WHERE rolname='$dbUser';"
if ("$roleExists" -notmatch "1") {
    Invoke-PsqlQuery "postgres" "CREATE USER `"$dbUser`" WITH PASSWORD '$dbPasswordSql';" | Out-Null
    Write-Host "[db] ユーザー $dbUser を作成しました。"
} else {
    Write-Host "[db] ユーザー $dbUser は既に存在します。"
}

$dbExists = Invoke-PsqlQuery "postgres" "SELECT 1 FROM pg_database WHERE datname='$dbName';"
if ("$dbExists" -notmatch "1") {
    Invoke-PsqlQuery "postgres" "CREATE DATABASE `"$dbName`" OWNER `"$dbUser`";" | Out-Null
    Write-Host "[db] データベース $dbName を作成しました。"
} else {
    Write-Host "[db] データベース $dbName は既に存在します。"
}

Invoke-PsqlQuery "postgres" "GRANT ALL PRIVILEGES ON DATABASE `"$dbName`" TO `"$dbUser`";" | Out-Null
Invoke-PsqlQuery $dbName "GRANT USAGE ON SCHEMA public TO `"$dbUser`";" | Out-Null
Invoke-PsqlQuery $dbName "GRANT CREATE ON SCHEMA public TO `"$dbUser`";" | Out-Null

# pgvector はあれば有効化する (無くてもアプリは動作するため警告のみ)
try {
    Invoke-PsqlQuery $dbName "CREATE EXTENSION IF NOT EXISTS vector;" | Out-Null
    Write-Host "[db] pgvector 拡張を有効化しました。"
} catch {
    Write-Host "[警告] pgvector 拡張を有効化できませんでした (未インストールの可能性)。セットアップは継続します。"
    Write-Host "       参考: https://github.com/pgvector/pgvector (Windows は EDB StackBuilder か手動ビルド)"
}

Write-Host "[db] データベース初期化が完了しました。"
