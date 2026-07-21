@echo off
REM FlowShift Web GUI Installer — double-click entry point
REM Elevates to admin, then runs install_webgui.ps1
setlocal
cd /d "%~dp0"
set "_ps1=%~dpn0.ps1"
if not exist "%_ps1%" (
    echo ERROR: %_ps1% not found
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%_ps1%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    pause
)
if "%RC%"=="0" (
    echo.
    pause
)
exit /b %RC%
