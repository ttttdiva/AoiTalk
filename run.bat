@echo off
chcp 65001 >nul
title AoiTalk
cd /d %~dp0
if not defined AOITALK_PROFILE set "AOITALK_PROFILE=personal"
if not defined AIVTUBER_ENV set "AIVTUBER_ENV=personal"
if not defined AOITALK_WEB_PORT set "AOITALK_WEB_PORT=3000"
if not defined AOITALK_NEXT_PORT set "AOITALK_NEXT_PORT=3002"
if not defined AOITALK_CADDY_PORT set "AOITALK_CADDY_PORT=6002"
if not defined AOITALK_CADDY_FASTAPI_PORT set "AOITALK_CADDY_FASTAPI_PORT=%AOITALK_WEB_PORT%"
if not defined AOITALK_CADDY_NEXT_PORT set "AOITALK_CADDY_NEXT_PORT=%AOITALK_NEXT_PORT%"
set "AOITALK_PYTHON=%~dp0venv\Scripts\python.exe"
if not exist "%AOITALK_PYTHON%" (
    echo Python virtual environment not found: %AOITALK_PYTHON%
    exit /b 1
)
"%AOITALK_PYTHON%" scripts\python_312_gate.py >nul 2>&1
if errorlevel 1 (
    echo Python 3.12以上の仮想環境が必要です。setup.batでvenvを再作成してください。
    exit /b 1
)
copy /y .env frontend\.env >nul 2>&1
set AOITALK_WEB_AUTO_OPEN=false

:start
"%AOITALK_PYTHON%" main.py
if %ERRORLEVEL% equ 42 (
    echo.
    echo === Restarting AoiTalk ===
    echo.
    copy /y .env frontend\.env >nul 2>&1
    goto start
)
