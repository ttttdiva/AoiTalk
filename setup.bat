@echo off
chcp 65001 >nul
setlocal
cd /d %~dp0

echo ===================================
echo AoiTalk セットアップ開始
echo ===================================

echo.
echo [1/8] PostgreSQLの確認とインストール...
set "PGPATH=C:\Program Files\PostgreSQL\16\bin"
if exist "%PGPATH%\psql.exe" goto pg_ready
where psql >nul 2>&1
if %ERRORLEVEL% EQU 0 goto pg_ready
winget install PostgreSQL.PostgreSQL.16 --accept-package-agreements --accept-source-agreements
:pg_ready
if exist "%PGPATH%\psql.exe" set "PATH=%PATH%;%PGPATH%"
where psql >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [エラー] psql が見つかりません。PostgreSQLのインストールを確認し、新しいコンソールで setup.bat を再実行してください。
    pause
    exit /b 1
)

echo.
echo [2/8] PostgreSQLサービスを開始中...
net start postgresql-x64-16 >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo PostgreSQLサービスは既に起動しているか、開始できませんでした。
)

echo.
echo [3/8] Pythonの確認...
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Pythonをインストール中...
    winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    echo Pythonをインストールしました。PATH反映のため新しいコンソールで setup.bat を再実行してください。
    pause
    exit /b 1
)

echo.
echo [4/8] Node.jsの確認...
where node >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Node.jsをインストール中...
    winget install OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
    echo Node.jsをインストールしました。PATH反映のため新しいコンソールで setup.bat を再実行してください。
    pause
    exit /b 1
)
echo Node.jsは既にインストールされています。

echo.
echo [5/8] .env生成とPostgreSQLデータベースを設定中...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup_env_db.ps1
if %ERRORLEVEL% NEQ 0 (
    echo [エラー] .env生成またはデータベース設定に失敗しました。
    pause
    exit /b 1
)

echo.
echo [6/8] Python仮想環境とパッケージをインストール中...
if not exist "venv\Scripts\python.exe" python -m venv venv
call venv\Scripts\activate
python -m pip install --upgrade pip
pip install -e ".[audio,windows,test,irodori]"
if %ERRORLEVEL% NEQ 0 (
    echo [エラー] Python依存パッケージのインストールに失敗しました。
    pause
    exit /b 1
)
pip install --no-deps "dacvae @ git+https://github.com/facebookresearch/dacvae" descript-audiotools argbind julius pystoi torch-stoi flatten-dict markdown2 randomname importlib-resources

echo.
echo [7/8] フロントエンドの依存インストールとビルド中...
if not exist "frontend\package.json" (
    echo frontend\package.json が見つかりません。スキップしました。
    goto frontend_done
)
copy /y .env frontend\.env >nul
pushd frontend
call npm ci
if %ERRORLEVEL% NEQ 0 (
    popd
    echo [エラー] npm ci に失敗しました。
    pause
    exit /b 1
)
call npm run build
if %ERRORLEVEL% NEQ 0 (
    popd
    echo [エラー] フロントエンドのビルドに失敗しました。
    pause
    exit /b 1
)
popd
echo 完了しました。
:frontend_done

echo.
echo [8/8] データベーススキーマ初期化/マイグレーション実行中...
call venv\Scripts\activate
python scripts\init_db_schema.py
if %ERRORLEVEL% NEQ 0 (
    echo [エラー] データベーススキーマ初期化に失敗しました。.env の POSTGRES_* 設定を確認してください。
    pause
    exit /b 1
)

echo.
echo ===================================
echo セットアップ完了！
echo ===================================
echo.
echo 起動: run.bat
echo.
pause
