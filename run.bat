@echo off
chcp 65001 >nul
title AoiTalk
cd /d %~dp0
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
