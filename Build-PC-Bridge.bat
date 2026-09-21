@echo off
cd /d "%~dp0"
if not exist ".local\portable-bridge\build-venv\Scripts\python.exe" (
  venv\Scripts\python.exe -m venv .local\portable-bridge\build-venv
  if errorlevel 1 exit /b 1
)
.local\portable-bridge\build-venv\Scripts\python.exe -m pip install -r pc_bridge\requirements.txt "pyinstaller==6.22.3"
if errorlevel 1 exit /b 1
.local\portable-bridge\build-venv\Scripts\python.exe scripts\build_pc_bridge.py --onedir
if errorlevel 1 exit /b 1
.local\portable-bridge\build-venv\Scripts\python.exe scripts\build_pc_bridge.py
exit /b %errorlevel%
