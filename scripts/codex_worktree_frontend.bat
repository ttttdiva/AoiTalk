@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "WORKTREE_ROOT=%%~fI"
set "FRONTEND_ROOT=%WORKTREE_ROOT%\frontend"

if "%~1"=="" (
  echo Usage: scripts\codex_worktree_frontend.bat ^<npm-script^> [args...]
  exit /b 1
)

if not exist "%FRONTEND_ROOT%\package.json" (
  echo [SKIP] frontend/package.json not found.
  exit /b 2
)

if not exist "%FRONTEND_ROOT%\.env" if exist "%WORKTREE_ROOT%\.env" (
  copy /y "%WORKTREE_ROOT%\.env" "%FRONTEND_ROOT%\.env" >nul
)

pushd "%FRONTEND_ROOT%" >nul || exit /b 1

if not exist "%FRONTEND_ROOT%\node_modules" (
  if not exist "%FRONTEND_ROOT%\package-lock.json" (
    echo [SKIP] frontend/node_modules not found and package-lock.json is missing.
    popd >nul
    exit /b 2
  )
  echo [INFO] frontend/node_modules not found; running npm ci for this worktree.
  call npm ci
  if errorlevel 1 (
    set "EXIT_CODE=%ERRORLEVEL%"
    popd >nul
    exit /b %EXIT_CODE%
  )
)

call npm run %*
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul
exit /b %EXIT_CODE%
