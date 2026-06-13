param(
    [switch]$ConfigureOnly,
    [switch]$NonInteractive,
    [string]$EnvFile
)

# AoiTalk Windows セットアップ補助スクリプト
# - .env の初回対話設定と認証シークレット生成
# - PostgreSQL 16 の導入、起動待ち、ユーザー/DB初期化

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$rootDir = Split-Path -Parent $PSScriptRoot
Set-Location $rootDir

if ([string]::IsNullOrWhiteSpace($EnvFile)) {
    $EnvFile = Join-Path $rootDir ".env"
} elseif (-not [System.IO.Path]::IsPathRooted($EnvFile)) {
    $EnvFile = Join-Path $rootDir $EnvFile
}
$envFilePath = [System.IO.Path]::GetFullPath($EnvFile)
$sampleFile = Join-Path $rootDir ".env.sample"

function New-RandomSecret {
    param([int]$ByteCount = 32)

    $bytes = New-Object byte[] $ByteCount
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    return ([System.BitConverter]::ToString($bytes) -replace "-", "").ToLowerInvariant()
}

function ConvertTo-PlainText {
    param([Security.SecureString]$SecureValue)

    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Read-Value {
    param(
        [string]$Label,
        [string]$Default
    )

    if ($NonInteractive) {
        return $Default
    }
    $value = Read-Host "$Label [$Default]"
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $Default
    }
    return $value.Trim()
}

function Read-OptionalSecret {
    param([string]$Label)

    if ($NonInteractive) {
        return ""
    }
    return ConvertTo-PlainText (Read-Host -AsSecureString $Label)
}

function Read-ConfirmedSecret {
    param([string]$Label)

    if ($NonInteractive) {
        throw "$Label は非対話モードでは環境変数 PGPASSWORD で指定してください。"
    }

    while ($true) {
        $first = ConvertTo-PlainText (Read-Host -AsSecureString $Label)
        if ([string]::IsNullOrWhiteSpace($first)) {
            Write-Host "[入力エラー] 空のパスワードは使用できません。"
            continue
        }
        if ($first.Contains('"')) {
            Write-Host '[入力エラー] PostgreSQLのインストーラーへ安全に渡すため、ダブルクォート (") は使用できません。'
            continue
        }
        $second = ConvertTo-PlainText (Read-Host -AsSecureString "$Label (確認)")
        if ($first -ceq $second) {
            return $first
        }
        Write-Host "[入力エラー] パスワードが一致しません。再入力してください。"
    }
}

function Read-DotEnv {
    param([string]$Path)

    $values = @{}
    foreach ($line in [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)) {
        if ($line -match '^\s*#') { continue }
        if ($line -match '^\s*([A-Za-z0-9_]+)\s*=\s*(.*)$') {
            $value = $Matches[2].Trim()
            $hashIndex = $value.IndexOf(" #", [System.StringComparison]::Ordinal)
            if ($hashIndex -ge 0) {
                $value = $value.Substring(0, $hashIndex).Trim()
            }
            if ($value.Length -ge 2) {
                $first = $value[0]
                $last = $value[$value.Length - 1]
                if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                    $value = $value.Substring(1, $value.Length - 2)
                }
            }
            $values[$Matches[1]] = $value
        }
    }
    return $values
}

function Set-DotEnvValue {
    param(
        [string]$Content,
        [string]$Key,
        [string]$Value
    )

    if ($Value -match '[\r\n]') {
        throw "$Key に改行を含めることはできません。"
    }
    $pattern = "(?m)^" + [regex]::Escape($Key) + "=.*$"
    $replacement = $Key + "=" + $Value
    if ($Content -match $pattern) {
        return [regex]::Replace($Content, $pattern, { param($match) $replacement })
    }
    return $Content.TrimEnd("`r", "`n") + [Environment]::NewLine + $replacement + [Environment]::NewLine
}

function Assert-PostgresSettings {
    param(
        [string]$HostName,
        [string]$Port,
        [string]$Database,
        [string]$User
    )

    if ([string]::IsNullOrWhiteSpace($HostName) -or $HostName -match '\s') {
        throw "POSTGRES_HOST は空白を含まないホスト名またはIPアドレスで指定してください。"
    }
    $portNumber = 0
    if (-not [int]::TryParse($Port, [ref]$portNumber) -or $portNumber -lt 1 -or $portNumber -gt 65535) {
        throw "POSTGRES_PORT は 1 から 65535 の整数で指定してください。"
    }
    foreach ($item in @(@("POSTGRES_DB", $Database), @("POSTGRES_USER", $User))) {
        if ($item[1] -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
            throw "$($item[0]) は英数字とアンダースコアで指定し、先頭を数字にしないでください。"
        }
    }
}

function Find-PostgresTool {
    param([string]$Name)

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $pgRoot = "C:\Program Files\PostgreSQL"
    if (Test-Path -LiteralPath $pgRoot) {
        foreach ($dir in (Get-ChildItem -LiteralPath $pgRoot -Directory | Sort-Object Name -Descending)) {
            $candidate = Join-Path $dir.FullName "bin\$Name.exe"
            if (Test-Path -LiteralPath $candidate) {
                return $candidate
            }
        }
    }
    return $null
}

function Test-LocalPostgresHost {
    param([string]$HostName)

    return $HostName -in @("localhost", "127.0.0.1", "::1")
}

function Ensure-PostgresInstalled {
    param(
        [string]$SuperuserPassword,
        [string]$Port
    )

    $psql = Find-PostgresTool "psql"
    if ($psql) {
        Write-Host "[db] PostgreSQL クライアントを検出しました: $psql"
        return $psql
    }

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "winget が見つかりません。Microsoft App Installer を導入してから再実行してください。"
    }

    Write-Host "[db] PostgreSQL 16 をインストールします。数分かかる場合があります。"
    $override = "--mode unattended --unattendedmodeui none --serverport $Port --superpassword `"$SuperuserPassword`""
    & $winget.Source install --id PostgreSQL.PostgreSQL.16 --exact `
        --accept-package-agreements --accept-source-agreements --disable-interactivity `
        --override $override
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL 16 のインストールに失敗しました (exit=$LASTEXITCODE)。"
    }

    $psql = Find-PostgresTool "psql"
    if (-not $psql) {
        throw "PostgreSQLのインストール後も psql が見つかりません。"
    }
    return $psql
}

function Wait-PostgresReady {
    param(
        [string]$PsqlPath,
        [string]$HostName,
        [string]$Port
    )

    if (Test-LocalPostgresHost $HostName) {
        $service = Get-Service -Name "postgresql-x64-16" -ErrorAction SilentlyContinue
        if (-not $service) {
            $service = Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                Select-Object -First 1
        }
        if ($service -and $service.Status -ne "Running") {
            Write-Host "[db] PostgreSQLサービスを開始します: $($service.Name)"
            Start-Service -Name $service.Name
        }
    }

    $pgIsReady = Join-Path (Split-Path -Parent $PsqlPath) "pg_isready.exe"
    if (-not (Test-Path -LiteralPath $pgIsReady)) {
        throw "pg_isready.exe が見つかりません: $pgIsReady"
    }

    Write-Host "[db] PostgreSQLの起動を待っています..."
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        & $pgIsReady -h $HostName -p $Port *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[db] PostgreSQLの接続受付を確認しました。"
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "PostgreSQLが30秒以内に接続可能になりませんでした (${HostName}:${Port})。"
}

# ---------------------------------------------------------------------------
# 1. .env の作成と初回設定
# ---------------------------------------------------------------------------
$isNewEnv = -not (Test-Path -LiteralPath $envFilePath)
if ($isNewEnv) {
    if (-not (Test-Path -LiteralPath $sampleFile)) {
        throw ".env.sample が見つかりません: $sampleFile"
    }
    $parent = Split-Path -Parent $envFilePath
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Copy-Item -LiteralPath $sampleFile -Destination $envFilePath
    Write-Host "[env] 初回セットアップ: PostgreSQL接続情報を設定します。"

    $content = [System.IO.File]::ReadAllText($envFilePath, [System.Text.Encoding]::UTF8)
    $pgHost = Read-Value "PostgreSQL ホスト" "localhost"
    $pgPort = Read-Value "PostgreSQL ポート" "5432"
    $dbName = Read-Value "AoiTalk データベース名" "aoitalk_memory"
    $dbUser = Read-Value "AoiTalk データベースユーザー" "aoitalk"
    $dbPassword = New-RandomSecret 24
    Assert-PostgresSettings $pgHost $pgPort $dbName $dbUser

    $content = Set-DotEnvValue $content "POSTGRES_HOST" $pgHost
    $content = Set-DotEnvValue $content "POSTGRES_PORT" $pgPort
    $content = Set-DotEnvValue $content "POSTGRES_DB" $dbName
    $content = Set-DotEnvValue $content "POSTGRES_USER" $dbUser
    $content = Set-DotEnvValue $content "POSTGRES_PASSWORD" $dbPassword

    if (-not $NonInteractive) {
        Write-Host "[env] 標準LLMは Gemini です。APIキーは空欄のままEnterで後から設定できます。"
        $geminiApiKey = Read-OptionalSecret "Gemini APIキー"
        if (-not [string]::IsNullOrWhiteSpace($geminiApiKey)) {
            $content = Set-DotEnvValue $content "GEMINI_API_KEY" $geminiApiKey
        }
    }
    [System.IO.File]::WriteAllText(
        $envFilePath,
        $content,
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-Host "[env] .env を作成し、アプリ用DBパスワードを自動生成しました。"
} else {
    Write-Host "[env] 既存の .env を使用します。"
}

$content = [System.IO.File]::ReadAllText($envFilePath, [System.Text.Encoding]::UTF8)
foreach ($key in @("NEXTAUTH_SECRET", "AOITALK_WEB_AUTH_SECRET", "AOITALK_JWT_SECRET", "INTERNAL_API_KEY")) {
    $values = Read-DotEnv $envFilePath
    if (-not $values.ContainsKey($key) -or [string]::IsNullOrWhiteSpace([string]$values[$key])) {
        $content = Set-DotEnvValue $content $key (New-RandomSecret)
        [System.IO.File]::WriteAllText(
            $envFilePath,
            $content,
            [System.Text.UTF8Encoding]::new($false)
        )
        Write-Host "[env] $key を自動生成しました。"
    }
}

$envValues = Read-DotEnv $envFilePath
function Get-EnvValue {
    param(
        [string]$Key,
        [string]$Default
    )

    if ($envValues.ContainsKey($Key) -and -not [string]::IsNullOrWhiteSpace([string]$envValues[$Key])) {
        return [string]$envValues[$Key]
    }
    return $Default
}

$pgHost = Get-EnvValue "POSTGRES_HOST" "localhost"
$pgPort = Get-EnvValue "POSTGRES_PORT" "5432"
$dbName = Get-EnvValue "POSTGRES_DB" "aoitalk_memory"
$dbUser = Get-EnvValue "POSTGRES_USER" "aoitalk"
$dbPassword = Get-EnvValue "POSTGRES_PASSWORD" ""
Assert-PostgresSettings $pgHost $pgPort $dbName $dbUser
if ([string]::IsNullOrWhiteSpace($dbPassword)) {
    $dbPassword = New-RandomSecret 24
    $content = Set-DotEnvValue $content "POSTGRES_PASSWORD" $dbPassword
    [System.IO.File]::WriteAllText(
        $envFilePath,
        $content,
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-Host "[env] POSTGRES_PASSWORD を自動生成しました。"
}

Write-Host "[env] 接続設定: ${pgHost}:${pgPort} / DB=$dbName / USER=$dbUser"
if ($ConfigureOnly) {
    Write-Host "[env] 設定ファイルの準備が完了しました。"
    exit 0
}

# ---------------------------------------------------------------------------
# 2. PostgreSQL の導入とDB初期化
# ---------------------------------------------------------------------------
$postgresPassword = $env:PGPASSWORD
if ([string]::IsNullOrWhiteSpace($postgresPassword)) {
    $postgresPassword = Read-ConfirmedSecret "PostgreSQL管理者 postgres のパスワード"
}

try {
    $psql = Ensure-PostgresInstalled $postgresPassword $pgPort
    Wait-PostgresReady $psql $pgHost $pgPort
    $env:PGPASSWORD = $postgresPassword

    function Invoke-PsqlQuery {
        param(
            [string]$Database,
            [string]$Sql
        )

        $result = & $psql -v ON_ERROR_STOP=1 -h $pgHost -p $pgPort -U postgres -d $Database -tAc $Sql
        if ($LASTEXITCODE -ne 0) {
            throw "psql の実行に失敗しました (DB=$Database)。postgres パスワードと接続設定を確認してください。"
        }
        return $result
    }

    $dbPasswordSql = $dbPassword -replace "'", "''"
    $roleExists = Invoke-PsqlQuery "postgres" "SELECT 1 FROM pg_roles WHERE rolname='$dbUser';"
    if ("$roleExists" -notmatch "1") {
        Invoke-PsqlQuery "postgres" "CREATE USER `"$dbUser`" WITH PASSWORD '$dbPasswordSql';" | Out-Null
        Write-Host "[db] ユーザー $dbUser を作成しました。"
    } else {
        Invoke-PsqlQuery "postgres" "ALTER USER `"$dbUser`" WITH PASSWORD '$dbPasswordSql';" | Out-Null
        Write-Host "[db] ユーザー $dbUser のパスワードを .env と同期しました。"
    }

    $dbExists = Invoke-PsqlQuery "postgres" "SELECT 1 FROM pg_database WHERE datname='$dbName';"
    if ("$dbExists" -notmatch "1") {
        Invoke-PsqlQuery "postgres" "CREATE DATABASE `"$dbName`" OWNER `"$dbUser`";" | Out-Null
        Write-Host "[db] データベース $dbName を作成しました。"
    } else {
        Write-Host "[db] データベース $dbName は既に存在します。"
    }

    Invoke-PsqlQuery "postgres" "GRANT ALL PRIVILEGES ON DATABASE `"$dbName`" TO `"$dbUser`";" | Out-Null
    Invoke-PsqlQuery $dbName "GRANT USAGE, CREATE ON SCHEMA public TO `"$dbUser`";" | Out-Null

    try {
        Invoke-PsqlQuery $dbName "CREATE EXTENSION IF NOT EXISTS vector;" | Out-Null
        Write-Host "[db] pgvector 拡張を有効化しました。"
    } catch {
        Write-Host "[警告] pgvector 拡張は利用できませんでした。AoiTalkはQdrantを使用するためセットアップを継続します。"
    }
    Write-Host "[db] PostgreSQLデータベース初期化が完了しました。"
} finally {
    $env:PGPASSWORD = $null
    $postgresPassword = $null
}
