@echo off
chcp 65001 >nul
setlocal
cd /d %~dp0

net session >nul 2>&1
if errorlevel 1 (
    echo AoiTalkセットアップを管理者権限で再起動します...
    powershell.exe -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ===================================
echo AoiTalk セットアップ開始
echo ===================================

echo.
echo [1/8] .env生成、PostgreSQL導入、データベース設定中...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup_env_db.ps1
if errorlevel 1 (
    echo [エラー] .env生成またはPostgreSQL設定に失敗しました。
    pause
    exit /b 1
)

echo.
echo [2/8] Python 3.12以上の確認...
set "PYTHON_CMD="
for /f "usebackq delims=" %%P in (`py -3.12 scripts\python_312_gate.py --print-executable 2^>nul`) do set "PYTHON_CMD=%%P"
if not defined PYTHON_CMD (
    python scripts\python_312_gate.py >nul 2>&1
    if not errorlevel 1 (
        for /f "usebackq delims=" %%P in (`python scripts\python_312_gate.py --print-executable 2^>nul`) do set "PYTHON_CMD=%%P"
    )
)
if not defined PYTHON_CMD (
    echo Python 3.12をインストール中...
    winget install Python.Python.3.12 --exact --accept-package-agreements --accept-source-agreements --disable-interactivity
    if errorlevel 1 (
        echo [エラー] Pythonのインストールに失敗しました。
        pause
        exit /b 1
    )
    for /f "usebackq delims=" %%P in (`py -3.12 scripts\python_312_gate.py --print-executable 2^>nul`) do set "PYTHON_CMD=%%P"
)
if not defined PYTHON_CMD (
    set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not exist "%PYTHON_CMD%" (
    echo [エラー] Python 3.12のインストール先が見つかりません。新しいコンソールで setup.bat を再実行してください。
    pause
    exit /b 1
)
"%PYTHON_CMD%" scripts\python_312_gate.py >nul 2>&1
if errorlevel 1 (
    echo [エラー] Python 3.12以上を確認できません: %PYTHON_CMD%
    pause
    exit /b 1
)
echo Python 3.12以上を確認しました。

echo.
echo [3/8] Node.jsの確認...
node -e "process.exit(Number(process.versions.node.split('.')[0]) >= 22 ? 0 : 1)" >nul 2>&1
if errorlevel 1 (
    echo Node.jsをインストール中...
    winget install OpenJS.NodeJS.LTS --exact --accept-package-agreements --accept-source-agreements --disable-interactivity
    if errorlevel 1 (
        echo [エラー] Node.jsのインストールに失敗しました。
        pause
        exit /b 1
    )
    set "PATH=%PATH%;C:\Program Files\nodejs"
    node -e "process.exit(Number(process.versions.node.split('.')[0]) >= 22 ? 0 : 1)" >nul 2>&1
    if errorlevel 1 (
        echo [エラー] Node.js 22以上を確認できません。新しいコンソールで setup.bat を再実行してください。
        pause
        exit /b 1
    )
)
echo Node.js 22以上を確認しました。

echo.
echo [4/8] Gitの確認...
where git >nul 2>&1
if errorlevel 1 (
    echo Gitをインストール中...
    winget install Git.Git --exact --accept-package-agreements --accept-source-agreements --disable-interactivity
    if errorlevel 1 (
        echo [エラー] Gitのインストールに失敗しました。
        pause
        exit /b 1
    )
    set "PATH=%PATH%;C:\Program Files\Git\cmd"
    where git >nul 2>&1
    if errorlevel 1 (
        echo [エラー] Gitのインストール先が見つかりません。新しいコンソールで setup.bat を再実行してください。
        pause
        exit /b 1
    )
)

echo.
echo [5/8] Python仮想環境とパッケージをインストール中...
if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe scripts\python_312_gate.py >nul 2>&1
    if errorlevel 1 (
        echo 既存venvがPython 3.12未満のため再作成します...
        rmdir /s /q venv
        if exist "venv" (
            echo [エラー] 既存venvの削除に失敗しました。起動中のPythonプロセスを停止してから再実行してください。
            pause
            exit /b 1
        )
    )
)
if not exist "venv\Scripts\python.exe" "%PYTHON_CMD%" -m venv venv
if not exist "venv\Scripts\python.exe" (
    echo [エラー] Python仮想環境の作成に失敗しました。
    pause
    exit /b 1
)
call venv\Scripts\activate
python -m pip install --upgrade pip
pip install -e ".[audio,windows,test,irodori,yomi-linter]"
if errorlevel 1 (
    echo [エラー] Python依存パッケージのインストールに失敗しました。
    pause
    exit /b 1
)
pip install --no-deps "dacvae @ git+https://github.com/facebookresearch/dacvae" descript-audiotools argbind julius pystoi torch-stoi flatten-dict markdown2 randomname importlib-resources
if errorlevel 1 (
    echo [エラー] Irodori-TTS依存パッケージのインストールに失敗しました。
    pause
    exit /b 1
)

echo.
echo [6/8] フロントエンドの依存インストール中...
if not exist "frontend\package.json" (
    echo frontend\package.json が見つかりません。スキップしました。
    goto frontend_done
)
copy /y .env frontend\.env >nul
pushd frontend
call npm ci
if errorlevel 1 (
    popd
    echo [エラー] npm ci に失敗しました。
    pause
    exit /b 1
)
popd
echo 完了しました。
:frontend_done

echo.
echo [7/8] データベーススキーマ初期化/マイグレーション実行中...
call venv\Scripts\activate
python scripts\init_db_schema.py
if errorlevel 1 (
    echo [エラー] データベーススキーマ初期化に失敗しました。.env の POSTGRES_* 設定を確認してください。
    pause
    exit /b 1
)

echo.
echo [8/8] フロントエンドをビルド中...
if not exist "frontend\package.json" goto build_done
pushd frontend
call npm run build
if errorlevel 1 (
    popd
    echo [エラー] フロントエンドのビルドに失敗しました。
    pause
    exit /b 1
)
popd
:build_done

echo.
echo ===================================
echo セットアップ完了！
echo ===================================
echo.
echo 起動: run.bat
echo.
pause
