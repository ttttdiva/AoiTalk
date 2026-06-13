@echo off
title AoiTalk
cd /d %~dp0
set "AOITALK_PYTHON=%~dp0venv\Scripts\python.exe"
if not exist "%AOITALK_PYTHON%" (
    echo Python virtual environment not found: %AOITALK_PYTHON%
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
