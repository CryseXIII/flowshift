@echo off
REM FlowShift installer launcher (double-click this file).
REM Runs the PowerShell installer with an execution-policy bypass.
REM The PowerShell script self-elevates via UAC.

setlocal
set "SCRIPT_DIR=%~dp0"

echo Starting FlowShift installer...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%install_flowshift.ps1"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo Installer exited with code %RC%.
)

echo.
echo You can close this window.
pause
endlocal
