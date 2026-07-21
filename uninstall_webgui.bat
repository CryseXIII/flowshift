@echo off
REM FlowShift Web GUI Uninstaller — double-click entry point
REM Elevates to admin, then runs uninstall_webgui.ps1
setlocal
cd /d "%~dp0"
set "_ps1=%~dpn0.ps1"
if not exist "%_ps1%" (
    echo ERROR: %_ps1% not found
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%_ps1%"
if errorlevel 1 (
    echo.
    pause
)
