@echo off
chcp 65001 >nul
setlocal
title AoiTalk launcher (run.bat) - starting
cd /d "%~dp0"
if not defined AOITALK_PROFILE set "AOITALK_PROFILE=personal"
if not defined AIVTUBER_ENV set "AIVTUBER_ENV=personal"
if not defined AOITALK_WEB_PORT set "AOITALK_WEB_PORT=3000"
if not defined AOITALK_NEXT_PORT set "AOITALK_NEXT_PORT=3002"
if not defined AOITALK_CADDY_PORT set "AOITALK_CADDY_PORT=6002"
if not defined AOITALK_CADDY_FASTAPI_PORT set "AOITALK_CADDY_FASTAPI_PORT=%AOITALK_WEB_PORT%"
if not defined AOITALK_CADDY_NEXT_PORT set "AOITALK_CADDY_NEXT_PORT=%AOITALK_NEXT_PORT%"
if not defined AOITALK_SKIP_CADDY set "AOITALK_SKIP_CADDY=true"
set "AOITALK_HEADLESS=false"
if not defined AOITALK_PUBLIC_BASE_PATH set "AOITALK_PUBLIC_BASE_PATH=/at"
if not defined NEXT_PUBLIC_BASE_PATH set "NEXT_PUBLIC_BASE_PATH=%AOITALK_PUBLIC_BASE_PATH%"
set "AOITALK_LAUNCH_SOURCE=run.bat"
set "AOITALK_PYTHON=%~dp0venv\Scripts\python.exe"
if not exist "%AOITALK_PYTHON%" (
    echo Python virtual environment not found: %AOITALK_PYTHON%
    pause
    exit /b 1
)
set "AOITALK_PYTHON_HOME="
for /f "tokens=1,* delims==" %%A in ('findstr /b /c:"home =" "%~dp0venv\pyvenv.cfg" 2^>nul') do set "AOITALK_PYTHON_HOME=%%B"
for /f "tokens=* delims= " %%A in ("%AOITALK_PYTHON_HOME%") do set "AOITALK_PYTHON_HOME=%%A"
set "AOITALK_PYTHONW=%AOITALK_PYTHON_HOME%\pythonw.exe"
if not exist "%AOITALK_PYTHONW%" (
    echo Python GUI runtime not found: %AOITALK_PYTHONW%
    pause
    exit /b 1
)
"%AOITALK_PYTHONW%" "%~dp0scripts\ensure_visible_console.py" "%~f0" %*
set "AOITALK_CONSOLE_CHECK_EXIT=%ERRORLEVEL%"
if "%AOITALK_CONSOLE_CHECK_EXIT%"=="10" exit /b 0
if not "%AOITALK_CONSOLE_CHECK_EXIT%"=="0" (
    echo Visible AoiTalk console could not be started.
    pause
    exit /b 1
)
echo Python実行環境を確認しています...
"%AOITALK_PYTHON%" scripts\python_312_gate.py --print-executable
if errorlevel 1 (
    echo Python 3.12以上の仮想環境が必要です。setup.batでvenvを再作成してください。
    pause
    exit /b 1
)
if exist ".env" (
    copy /y .env frontend\.env >nul
    if errorlevel 1 echo [警告] .envをfrontend\.envへコピーできませんでした。
) else (
    echo [警告] .envが見つかりません。必要な設定を確認してください。
)
set AOITALK_WEB_AUTO_OPEN=false
echo AoiTalkを起動しています。
call "%~dp0run_plugin.bat"
set "AOITALK_PLUGIN_LAUNCH_EXIT=%ERRORLEVEL%"
if not "%AOITALK_PLUGIN_LAUNCH_EXIT%"=="0" if not "%AOITALK_PLUGIN_LAUNCH_EXIT%"=="2" echo [警告] AoiTalk Pluginの起動に失敗しました（終了コード %AOITALK_PLUGIN_LAUNCH_EXIT%）。AoiTalk本体は続けて起動します。

:start
if not exist "data" mkdir "data" >nul 2>&1
> "data\launcher-generation.txt" echo %RANDOM%-%RANDOM%-%DATE%-%TIME%
"%AOITALK_PYTHON%" main.py
set "AOITALK_EXIT_CODE=%ERRORLEVEL%"
if "%AOITALK_EXIT_CODE%"=="42" (
    echo.
    echo === Restarting AoiTalk ===
    echo.
    if exist ".env" copy /y .env frontend\.env >nul
    goto start
)
if not "%AOITALK_EXIT_CODE%"=="0" (
    echo.
    echo AoiTalkが終了コード %AOITALK_EXIT_CODE% で終了しました。上のエラーを確認してください。
    pause
)
exit /b %AOITALK_EXIT_CODE%
