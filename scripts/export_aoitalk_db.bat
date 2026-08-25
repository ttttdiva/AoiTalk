@echo off
setlocal

powershell.exe ^
  -NoProfile ^
  -ExecutionPolicy Bypass ^
  -File "%~dp0export_aoitalk_db.ps1" %*

set "EXITCODE=%ERRORLEVEL%"

echo.

if not "%EXITCODE%"=="0" (
    echo [ERROR] PostgreSQL export failed.
    echo Exit code: %EXITCODE%
) else (
    echo PostgreSQL export completed successfully.
)

echo.
pause

exit /b %EXITCODE%