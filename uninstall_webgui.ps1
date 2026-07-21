# FlowShift Web GUI uninstaller
# Removes the web GUI from the FlowShift installation directory.
# Run via uninstall_webgui.bat (double-click). Self-elevates through UAC.

param([switch]$Elevated)

$ErrorActionPreference = 'Stop'
$InstallDir = Join-Path $env:ProgramFiles 'FlowShift'
$WebTarget  = Join-Path $InstallDir 'webgui'
$LogDir     = Join-Path $env:ProgramData 'FlowShift\logs'
$logFile    = Join-Path $LogDir 'uninstall_webgui.log'

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Log {
    param([string]$Msg, [string]$Level = 'INFO')
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [$Level] $Msg"
    try {
        if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
        Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
    } catch {}
    $color = @{INFO='White'; OK='Green'; WARN='Yellow'; ERR='Red'}[$Level]
    if (-not $color) { $color = 'White' }
    Write-Host "  $Msg" -ForegroundColor $color
}

function Stop-FlowShiftOwnedOverlayHosts {
    $root = ([string]$InstallDir).TrimEnd('\')
    $rootPrefix = $root + '\'
    $overlayProcesses = @()
    try {
        $overlayProcesses = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop | Where-Object {
            [string]$_.CommandLine -match '(?i)(?:^|[\\/"\s])overlay_host\.py(?:["\s]|$)'
        })
    } catch {
        Log "Could not enumerate overlay_host.py processes: $($_.Exception.Message)" 'WARN'
        return
    }

    if ($overlayProcesses.Count -eq 0) {
        Log 'No overlay_host.py processes were running' 'OK'
        return
    }

    foreach ($process in $overlayProcesses) {
        $commandLine = [string]$process.CommandLine
        $executable = [string]$process.ExecutablePath
        $ownedByExecutable = $executable.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)
        $ownedOverlayPathPattern = [regex]::Escape($rootPrefix) + '[^"\r\n]*overlay_host\.py(?:["\s]|$)'
        $ownedByCommandPath = $commandLine -match $ownedOverlayPathPattern
        if (-not ($ownedByExecutable -or $ownedByCommandPath)) {
            Log "Left unrelated overlay_host.py PID $($process.ProcessId) running; no executable or command path is rooted under $root" 'WARN'
            continue
        }
        try {
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction Stop
            Log "Stopped FlowShift-owned overlay_host.py PID $($process.ProcessId)" 'OK'
        } catch {
            Log "Could not stop FlowShift-owned overlay_host.py PID $($process.ProcessId): $($_.Exception.Message)" 'WARN'
        }
    }
}

# ---- Self-elevate -----------------------------------------------------------
if (-not (Test-Admin)) {
    Write-Host 'FlowShift Web GUI uninstaller needs administrator rights.' -ForegroundColor Yellow
    Write-Host 'A UAC prompt will appear now...' -ForegroundColor Yellow
    $argList = @('-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$PSCommandPath`"",'-Elevated')
    $shell = if (Get-Command 'pwsh' -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell' }
    try {
        Start-Process -FilePath $shell -Verb RunAs -ArgumentList $argList
    } catch {
        Write-Host "Self-elevation failed: $_" -ForegroundColor Red
        pause
        exit 1
    }
    exit 0
}

Write-Host ''
Write-Host '============================================' -ForegroundColor Cyan
Write-Host '   FlowShift Web GUI Uninstaller             ' -ForegroundColor Cyan
Write-Host '============================================' -ForegroundColor Cyan
Write-Host ''

# ---- 1. Stop the owned overlay host, then remove webgui directory ------------
# This component-only uninstall does not stop the core runtime or scheduled task.
Stop-FlowShiftOwnedOverlayHosts
Log 'Core runtime and scheduled task were left running' 'OK'
Log "Removing $WebTarget ..." 'INFO'
if (Test-Path $WebTarget) {
    Remove-Item -Recurse -Force $WebTarget
    Log 'Web GUI directory removed' 'OK'
} else {
    Log 'Web GUI directory not found, skipping' 'WARN'
}

try {
    $envReg = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
    Remove-ItemProperty -Path $envReg -Name 'FLOWSHIFT_WEBGUI_DIR' -Force -ErrorAction SilentlyContinue
    Log 'machine env FLOWSHIFT_WEBGUI_DIR cleared' 'OK'
} catch {
    Log "Could not clear FLOWSHIFT_WEBGUI_DIR: $_" 'WARN'
}

# ---- 2. Remove uninstaller registry key -------------------------------------
$uninKey = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FlowShift Web GUI'
Log 'Removing uninstaller registry key...' 'INFO'
try {
    if (Test-Path $uninKey) {
        Remove-Item -Path $uninKey -Force -Recurse
        Log 'Uninstaller registry key removed' 'OK'
    } else {
        Log 'Registry key not found, skipping' 'INFO'
    }
} catch {
    Log "Could not remove registry key: $_" 'WARN'
}

# ---- 3. Remove shortcuts ----------------------------------------------------
Log 'Removing shortcuts...' 'INFO'
$startDir = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\FlowShift'
$filesToRemove = @(
    (Join-Path $startDir 'FlowShift Web GUI.lnk'),
    (Join-Path $startDir 'Uninstall FlowShift Web GUI.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'FlowShift Web GUI.lnk'),
    (Join-Path $env:PUBLIC 'Desktop\FlowShift Web GUI.lnk')
)
foreach ($f in $filesToRemove) {
    if (Test-Path $f) {
        Remove-Item -Force $f
        Log "Removed $f" 'OK'
    }
}

Log 'Shortcuts removed' 'OK'
Log 'WebView2 and Python dependencies are shared/core dependencies and were not uninstalled' 'OK'

# ---- Done -------------------------------------------------------------------
Write-Host "`n============================================" -ForegroundColor Green
Write-Host "  FlowShift Web GUI has been uninstalled     " -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host "  The FlowShift core (Python backend) was NOT removed." -ForegroundColor White
Write-Host ''
pause
