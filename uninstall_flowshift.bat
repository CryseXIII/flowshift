@echo off
REM FlowShift uninstaller launcher (double-click this file).
REM Runs the PowerShell uninstaller with an execution-policy bypass.
REM The PowerShell script self-elevates via UAC.

setlocal
set "SCRIPT_DIR=%~dp0"

echo Starting FlowShift uninstaller...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%uninstall_flowshift.ps1"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo Uninstaller exited with code %RC%.
)

echo.
echo You can close this window.
pause
endlocal
