@echo off
REM FlowShift installer launcher (double-click this file).
REM Installs or upgrades FlowShift including the WebGUI in one run. A running
REM FlowShift is stopped and restarted; config.json and data are kept.
REM The PowerShell script self-elevates via UAC and asks nothing else.
REM Pass -Interactive to get confirmation questions.

setlocal
set "SCRIPT_DIR=%~dp0"

echo Starting FlowShift installer...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%install_flowshift.ps1" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo Installer exited with code %RC%. See C:\ProgramData\FlowShift\logs\install.log
    timeout /t 60
)
endlocal
exit /b %RC%
