@echo off
setlocal
cd /d "%~dp0"
set "AOITALK_PLUGIN_CONTROL=%~dp0plugins\AoiTalk_Plugin\scripts\plugin_launch_control.ps1"
if not exist "%AOITALK_PLUGIN_CONTROL%" (
    echo FAILED: Plugin launcher helper is missing.
    exit /b 1
)
if "%~1"=="" goto ensure
if /I "%~1"=="--stop" goto stop
:usage
echo Usage: run_plugin.bat [--stop]
exit /b 64

:ensure
if not "%~2"=="" goto usage
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%AOITALK_PLUGIN_CONTROL%" -Mode Ensure
exit /b %ERRORLEVEL%

:stop
if not "%~2"=="" goto usage
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%AOITALK_PLUGIN_CONTROL%" -Mode Stop
exit /b %ERRORLEVEL%
