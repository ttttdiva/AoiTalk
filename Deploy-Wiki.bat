@echo off
setlocal EnableExtensions

set "WIKI_ENTRY=%~dp0deploy\az-vnetspace-wiki\Deploy-Wiki.bat"
if not exist "%WIKI_ENTRY%" (
    echo [ERROR] Wiki deploy entrypoint was not found: %WIKI_ENTRY%
    exit /b 1
)

call "%WIKI_ENTRY%" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
