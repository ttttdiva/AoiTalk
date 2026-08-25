@echo off
chcp 65001 >nul
setlocal
title AoiTalk
cd /d "%~dp0"
if not defined AOITALK_PROFILE set "AOITALK_PROFILE=personal"
if not defined AIVTUBER_ENV set "AIVTUBER_ENV=personal"
if not defined AOITALK_WEB_PORT set "AOITALK_WEB_PORT=3000"
if not defined AOITALK_NEXT_PORT set "AOITALK_NEXT_PORT=3002"
if not defined AOITALK_CADDY_PORT set "AOITALK_CADDY_PORT=6002"
if not defined AOITALK_CADDY_FASTAPI_PORT set "AOITALK_CADDY_FASTAPI_PORT=%AOITALK_WEB_PORT%"
if not defined AOITALK_CADDY_NEXT_PORT set "AOITALK_CADDY_NEXT_PORT=%AOITALK_NEXT_PORT%"
if not defined AOITALK_SKIP_CADDY set "AOITALK_SKIP_CADDY=false"
set "AOITALK_PYTHON=%~dp0venv\Scripts\python.exe"
if not exist "%AOITALK_PYTHON%" (
    echo Python virtual environment not found: %AOITALK_PYTHON%
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
echo AoiTalkを起動しています。初回はサービス準備に時間がかかる場合があります。

:start
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
