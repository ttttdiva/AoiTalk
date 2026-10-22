@echo off
setlocal EnableExtensions

for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
set "BUILD_HELPER=%PROJECT_DIR%\scripts\build_mobile_apk.ps1"

rem build_mobile_apk.ps1 owns snapshot pinning, short workspace setup,
rem transactional artifact replacement, and the explicit publish-only path.
if /I "%~1"=="__AOITALK_SNAPSHOT_BUILD" goto snapshot_build
if not exist "%BUILD_HELPER%" (
  echo [ERROR] Mobile APK launcher helper is missing: %BUILD_HELPER%
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%BUILD_HELPER%" -ProjectRoot "%PROJECT_DIR%" %*
exit /b %ERRORLEVEL%

:snapshot_build
if /I not "%AOITALK_RELEASE_OWNER%"=="AoiTalk-build_mobile_apk/v1" (
  echo [ERROR] Snapshot build is private to build_mobile_apk.ps1.
  exit /b 1
)
if not defined AOITALK_RELEASE_SOURCE_COMMIT (
  echo [ERROR] Snapshot source commitment is missing.
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%BUILD_HELPER%" -ProjectRoot "%PROJECT_DIR%" -InternalBuild
exit /b %ERRORLEVEL%
