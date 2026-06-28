@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "WORKTREE_ROOT=%%~fI"

if defined AOITALK_VENV_PYTHON (
  set "AOITALK_PYTHON=%AOITALK_VENV_PYTHON%"
) else (
  set "AOITALK_PYTHON=%WORKTREE_ROOT%\venv\Scripts\python.exe"
)

if not exist "%AOITALK_PYTHON%" (
  echo [ERROR] Python executable not found: %AOITALK_PYTHON%
  echo Set AOITALK_VENV_PYTHON to an existing venv\Scripts\python.exe.
  exit /b 1
)

if defined PYTHONPATH (
  set "PYTHONPATH=%WORKTREE_ROOT%;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%WORKTREE_ROOT%"
)

pushd "%WORKTREE_ROOT%" >nul || exit /b 1
"%AOITALK_PYTHON%" %*
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul
exit /b %EXIT_CODE%
