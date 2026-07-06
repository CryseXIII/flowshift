# FlowShift uninstaller
#
# Stops and removes the FlowShift Windows service (NSSM), removes shortcuts,
# the Start Menu folder, program files and (optionally) the local config/logs.
#
# Run via uninstall_flowshift.bat (double-click). Self-elevates through UAC.

param([switch]$Elevated, [switch]$PurgeData)

$ErrorActionPreference = 'Continue'
$InstallDir  = Join-Path $env:ProgramFiles 'FlowShift'
$DataDir     = Join-Path $env:ProgramData 'FlowShift'
$LogDir      = Join-Path $DataDir 'logs'
$UninstLog   = Join-Path $LogDir 'uninstall.log'
$ServiceName = 'FlowShiftRuntime'
$NssmExe     = Join-Path $InstallDir 'tools\nssm\nssm.exe'
$TaskName    = 'FlowShift'   # legacy elevated scheduled task (if it exists)

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Write-Host ''
    Write-Host 'FlowShift uninstall needs administrator rights. A UAC prompt will appear...' -ForegroundColor Yellow
    try {
        Start-Process -FilePath 'powershell' -Verb RunAs -ArgumentList @(
            '-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$PSCommandPath`"",'-Elevated'
        )
    } catch {
        Write-Host "Elevation cancelled or failed: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
    exit 0
}

# Best-effort log (data dir may still exist).
try { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null } catch { }
function Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = ('[{0}] [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message)
    try { Add-Content -Path $UninstLog -Value $line } catch { }
    $color = switch ($Level) { 'ERROR' {'Red'} 'WARN' {'Yellow'} 'OK' {'Green'} default {'Gray'} }
    Write-Host $line -ForegroundColor $color
}

Write-Host ''
Write-Host '========================================' -ForegroundColor White
Write-Host '       FlowShift Uninstaller' -ForegroundColor White
Write-Host '========================================' -ForegroundColor White
Log 'uninstall started'

# 1. Stop + remove the service.
Write-Host ''
Write-Host '[1/6] Stopping and removing the service' -ForegroundColor Cyan
if (Test-Path $NssmExe) {
    & $NssmExe stop   $ServiceName 2>&1 | Out-Null
    & $NssmExe remove $ServiceName confirm 2>&1 | Out-Null
    Log 'service removed via NSSM' 'OK'
} else {
    # Fall back to sc.exe if NSSM is already gone.
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc) {
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        & sc.exe delete $ServiceName | Out-Null
        Log 'service removed via sc.exe' 'OK'
    } else {
        Log 'service not present' 'OK'
    }
}

# 2. Remove the user-session autostart scheduled task.
Write-Host ''
Write-Host '[2/6] Removing autostart scheduled task (if present)' -ForegroundColor Cyan
try {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($t) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Log 'scheduled task removed' 'OK'
    } else { Log 'no scheduled task' 'OK' }
} catch { Log "scheduled task check failed: $($_.Exception.Message)" 'WARN' }
# Remove machine env vars set by the installer.
try {
    [System.Environment]::SetEnvironmentVariable('FLOWSHIFT_CONFIG', $null, 'Machine')
    [System.Environment]::SetEnvironmentVariable('FLOWSHIFT_LOG_DIR', $null, 'Machine')
    Log 'machine env FLOWSHIFT_CONFIG / FLOWSHIFT_LOG_DIR cleared' 'OK'
} catch { Log "could not clear env vars: $($_.Exception.Message)" 'WARN' }

# 3. Kill any lingering runtime processes on the control/peer ports.
Write-Host ''
Write-Host '[3/6] Stopping lingering FlowShift processes' -ForegroundColor Cyan
foreach ($port in @(45782, 45781)) {
    try {
        Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object {
                try { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue; Log "killed PID $_ on port $port" } catch { }
            }
    } catch { }
}

# 4. Remove shortcuts + Start Menu folder.
Write-Host ''
Write-Host '[4/6] Removing shortcuts' -ForegroundColor Cyan
$desktop  = Join-Path $env:PUBLIC 'Desktop\FlowShift.lnk'
$startDir = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\FlowShift'
if (Test-Path $desktop)  { Remove-Item -Path $desktop -Force -ErrorAction SilentlyContinue; Log 'desktop shortcut removed' }
if (Test-Path $startDir) { Remove-Item -Path $startDir -Recurse -Force -ErrorAction SilentlyContinue; Log 'start menu folder removed' }

# 5. Remove program files.
Write-Host ''
Write-Host '[5/6] Removing program files' -ForegroundColor Cyan
if (Test-Path $InstallDir) {
    try {
        Remove-Item -Path $InstallDir -Recurse -Force -ErrorAction Stop
        Log "removed $InstallDir" 'OK'
    } catch {
        Log "could not fully remove $InstallDir : $($_.Exception.Message)" 'WARN'
        Log 'a file may be in use; reboot and delete the folder manually if needed.' 'WARN'
    }
} else { Log 'program folder not present' 'OK' }

# 6. Optionally remove data (config + logs).
Write-Host ''
Write-Host '[6/6] Local config and logs' -ForegroundColor Cyan
$purge = $PurgeData
if (-not $purge) {
    $ans = Read-Host 'Also delete local config and logs in ProgramData\FlowShift? (y/N)'
    if ($ans -match '^[yY]') { $purge = $true }
}
if ($purge) {
    if (Test-Path $DataDir) {
        try { Remove-Item -Path $DataDir -Recurse -Force -ErrorAction Stop; Write-Host "Removed $DataDir" -ForegroundColor Green }
        catch { Write-Host "Could not remove $DataDir : $($_.Exception.Message)" -ForegroundColor Yellow }
    }
} else {
    Log "kept data in $DataDir" 'OK'
}

Write-Host ''
Write-Host '================ UNINSTALL COMPLETE ================' -ForegroundColor Green
Write-Host 'Service, shortcuts and program files removed.'
if (-not $purge) { Write-Host "Local config/logs kept in: $DataDir" }
Write-Host '===================================================' -ForegroundColor Green
Write-Host ''
Read-Host 'Press Enter to close'
exit 0
