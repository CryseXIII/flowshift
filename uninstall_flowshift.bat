@echo off
REM FlowShift uninstaller launcher (double-click this file).
REM Removes FlowShift including the WebGUI. Your config, clipboard history and
REM logs in ProgramData\FlowShift are kept unless you pass -PurgeData.
REM The PowerShell script self-elevates via UAC and asks nothing else.
REM Pass -Interactive to get confirmation questions.

setlocal
set "SCRIPT_DIR=%~dp0"

echo Starting FlowShift uninstaller...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%uninstall_flowshift.ps1" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo Uninstaller exited with code %RC%.
    timeout /t 60
)
endlocal
exit /b %RC%
