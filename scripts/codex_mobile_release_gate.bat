@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO_ROOT=%%~fI"
set "GATE_SCRIPT=%REPO_ROOT%\scripts\check_mobile_release_gate.ps1"

if not exist "%GATE_SCRIPT%" (
  echo [SKIP] release gate script not found: %GATE_SCRIPT%
  exit /b 2
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%GATE_SCRIPT%" %*
exit /b %ERRORLEVEL%
