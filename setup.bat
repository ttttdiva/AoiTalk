@echo off
REM AoiTalk Setup for Windows

echo ===================================
echo AoiTalk セットアップ開始
echo ===================================

REM PostgreSQLのインストール
echo.
echo [1/5] PostgreSQLをインストール中...
winget install PostgreSQL.PostgreSQL.16 --accept-package-agreements --accept-source-agreements

REM PostgreSQLのパスを追加（新規インストールの場合）
set "PGPATH=C:\Program Files\PostgreSQL\16\bin"
if "%AOITALK_POSTGRES_PASSWORD%"=="" (
    echo AOITALK_POSTGRES_PASSWORD を設定してから再実行してください。
    echo 例: set AOITALK_POSTGRES_PASSWORD=your-secure-password
    exit /b 1
)
if exist "%PGPATH%\psql.exe" (
    set "PATH=%PATH%;%PGPATH%"
)

REM PostgreSQLサービスの開始
echo.
echo [2/5] PostgreSQLサービスを開始中...
net start postgresql-x64-16 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo PostgreSQLサービスは既に起動しているか、開始できませんでした。
)

REM PostgreSQLのデータベース初期化
echo.
echo [3/5] PostgreSQLデータベースを設定中...
where psql >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo PostgreSQL初期化SQLを実行中...
    psql -U postgres -c "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_catalog.pg_user WHERE usename = 'aoitalk') THEN CREATE USER aoitalk WITH PASSWORD '%AOITALK_POSTGRES_PASSWORD%'; END IF; END $$;" 2>nul
    psql -U postgres -c "SELECT 'CREATE DATABASE aoitalk_memory OWNER aoitalk' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'aoitalk_memory')\gexec" 2>nul
    psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE aoitalk_memory TO aoitalk;" 2>nul
    psql -U postgres -d aoitalk_memory -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>nul
    psql -U postgres -d aoitalk_memory -c "GRANT USAGE ON SCHEMA public TO aoitalk;" 2>nul
    psql -U postgres -d aoitalk_memory -c "GRANT CREATE ON SCHEMA public TO aoitalk;" 2>nul
    echo PostgreSQLデータベースの設定が完了しました。
) else (
    echo psqlが見つかりません。PostgreSQLのbinディレクトリをPATHに追加してから再実行するか、
    echo 手動でデータベースを設定してください。
)

REM Node.jsのインストール（Gemini CLI用）
echo.
echo [4/5] Node.jsの確認とインストール...
where node >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Node.jsが見つかりません。インストール中...
    winget install OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
) else (
    echo Node.jsは既にインストールされています。
)

REM Gemini CLIのインストール
echo.
echo [5/5] Gemini CLIをインストール中...
where gemini >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Gemini CLIをグローバルインストール中...
    call npm install -g @google/gemini-cli
) else (
    echo Gemini CLIは既にインストールされています。
)

REM Python仮想環境とパッケージのインストール
echo.
echo Python仮想環境とパッケージをインストール中...
python -m venv venv
call venv\Scripts\activate
pip install -e ".[audio,windows,test,qwen3]"

echo.
echo ===================================
echo セットアップ完了！
echo ===================================
echo.
echo 次のコマンドで起動できます:
echo   python main.py
echo.
pause
