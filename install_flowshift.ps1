# FlowShift installer
#
# Installs FlowShift on a Windows machine:
#   - ensures Python is present (installs it if missing, best-effort)
#   - creates a venv and installs dependencies (stdlib only today)
#   - installs NSSM and registers the FlowShift runtime as a Windows service
#   - creates config + log folders in %ProgramData%\FlowShift
#   - creates Desktop + Start Menu shortcuts for the GUI
#   - starts the service and verifies the control socket
#
# IMPORTANT (session-0 caveat): a Windows service runs in session 0 and CANNOT
# capture or inject interactive input for the logged-on user. The service here
# provides the runtime + control socket, but for ACTUAL input forwarding the
# runtime must run in the interactive user session (GUI/Tray autostart). This is
# documented in FLOWSHIFT_AUDIT_AND_FIX_REPORT.md and needs live verification.
#
# Run via install_flowshift.bat (double-click). Self-elevates through UAC.

param([switch]$Elevated)

$ErrorActionPreference = 'Stop'
$RepoDir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir  = Join-Path $env:ProgramFiles 'FlowShift'
$DataDir     = Join-Path $env:ProgramData 'FlowShift'
$LogDir      = Join-Path $DataDir 'logs'
$InstallLog  = Join-Path $LogDir 'install.log'
$ServiceName = 'FlowShiftRuntime'
$NssmDir     = Join-Path $InstallDir 'tools\nssm'
$NssmExe     = Join-Path $NssmDir 'nssm.exe'
$VenvDir     = Join-Path $InstallDir '.venv'
$PyDir       = Join-Path $InstallDir 'src\python'
$TotalSteps  = 12
$PythonMinor = 12   # target Python 3.x minor for auto-install

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# --- Self-elevate ----------------------------------------------------------
if (-not (Test-Admin)) {
    Write-Host ''
    Write-Host 'FlowShift installation needs administrator rights' -ForegroundColor Yellow
    Write-Host '(to install a Windows service, write to Program Files, etc.).'
    Write-Host 'A UAC prompt will appear now...' -ForegroundColor Yellow
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

# --- Logging ---------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$script:StepNo = 0
function Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = ('[{0}] [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message)
    Add-Content -Path $InstallLog -Value $line
    $color = switch ($Level) { 'ERROR' {'Red'} 'WARN' {'Yellow'} 'OK' {'Green'} default {'Gray'} }
    Write-Host $line -ForegroundColor $color
}
function Step {
    param([string]$Message)
    $script:StepNo++
    $hdr = ('[{0}/{1}] {2}' -f $script:StepNo, $TotalSteps, $Message)
    Add-Content -Path $InstallLog -Value ''
    Add-Content -Path $InstallLog -Value $hdr
    Write-Host ''
    Write-Host $hdr -ForegroundColor Cyan
}
function Fail {
    param([string]$Message)
    Log $Message 'ERROR'
    Write-Host ''
    Write-Host '================ INSTALLATION FAILED ================' -ForegroundColor Red
    Write-Host "Reason : $Message" -ForegroundColor Red
    Write-Host "Log    : $InstallLog" -ForegroundColor Red
    Write-Host '====================================================' -ForegroundColor Red
    Write-Host ''
    Read-Host 'Press Enter to close'
    exit 1
}

Write-Host ''
Write-Host '========================================' -ForegroundColor White
Write-Host '        FlowShift Installer' -ForegroundColor White
Write-Host '========================================' -ForegroundColor White
Log "installer started; repo=$RepoDir install=$InstallDir data=$DataDir"

try {
    # --- 1. Admin -----------------------------------------------------------
    Step 'Checking administrator rights'
    Log 'running elevated' 'OK'

    # --- 2. Python ----------------------------------------------------------
    Step 'Checking Python'
    $pyCmd = $null
    foreach ($cand in @(
        @{ exe = 'py';     args = @('-3','--version') },
        @{ exe = 'python'; args = @('--version') }
    )) {
        try {
            $v = & $cand.exe $cand.args 2>&1
            if ($LASTEXITCODE -eq 0 -and "$v" -match 'Python 3\.(\d+)') {
                if ([int]$Matches[1] -ge 9) { $pyCmd = $cand; Log "found $v" 'OK'; break }
                else { Log "found $v (too old, need >= 3.9)" 'WARN' }
            }
        } catch { }
    }

    # --- 3. Install Python if missing --------------------------------------
    Step 'Installing Python (if missing)'
    if ($pyCmd) {
        Log 'Python already present, skipping install' 'OK'
    } else {
        Log 'Python not found; attempting automatic install'
        $installed = $false
        # Try winget first (present on most Windows 10/11).
        try {
            $wg = Get-Command winget -ErrorAction SilentlyContinue
            if ($wg) {
                Log "installing via winget: Python.Python.3.$PythonMinor"
                & winget install --id "Python.Python.3.$PythonMinor" --silent `
                    --accept-package-agreements --accept-source-agreements --scope machine
                if ($LASTEXITCODE -eq 0) { $installed = $true; Log 'winget install ok' 'OK' }
            }
        } catch { Log "winget install failed: $($_.Exception.Message)" 'WARN' }

        # Fallback: direct download of the official installer.
        if (-not $installed) {
            $ver = "3.$PythonMinor.7"
            $url = "https://www.python.org/ftp/python/$ver/python-$ver-amd64.exe"
            $tmp = Join-Path $env:TEMP "python-$ver-amd64.exe"
            Log "downloading Python from $url"
            try {
                Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
                Log 'running silent Python installer (InstallAllUsers=1 PrependPath=1)'
                Start-Process -FilePath $tmp -Wait -ArgumentList @(
                    '/quiet','InstallAllUsers=1','PrependPath=1','Include_launcher=1'
                )
                $installed = $true
                Log 'Python installer finished' 'OK'
            } catch {
                Fail "could not install Python automatically: $($_.Exception.Message). Please install Python 3.$PythonMinor from python.org and re-run."
            }
        }
        # Re-resolve.
        $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
                    [System.Environment]::GetEnvironmentVariable('Path','User')
        foreach ($cand in @(
            @{ exe = 'py';     args = @('-3','--version') },
            @{ exe = 'python'; args = @('--version') }
        )) {
            try {
                $v = & $cand.exe $cand.args 2>&1
                if ($LASTEXITCODE -eq 0 -and "$v" -match 'Python 3\.') { $pyCmd = $cand; break }
            } catch { }
        }
        if (-not $pyCmd) { Fail 'Python still not found after install. Re-run the installer after a reboot.' }
    }

    # --- 4. Copy app files + create venv -----------------------------------
    Step 'Installing application files and creating venv'
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Log "copying source tree to $InstallDir"
    Copy-Item -Path (Join-Path $RepoDir 'src')  -Destination $InstallDir -Recurse -Force
    if (Test-Path (Join-Path $RepoDir 'docs')) {
        Copy-Item -Path (Join-Path $RepoDir 'docs') -Destination $InstallDir -Recurse -Force
    }
    foreach ($f in @('requirements.txt','README.md','uninstall_flowshift.bat','uninstall_flowshift.ps1')) {
        $src = Join-Path $RepoDir $f
        if (Test-Path $src) { Copy-Item -Path $src -Destination $InstallDir -Force }
    }
    # Remove any dev config/logs that might have been copied.
    foreach ($junk in @('config.json','flowshift.log','flowshift_runtime.out')) {
        $p = Join-Path $PyDir $junk
        if (Test-Path $p) { Remove-Item -Path $p -Force }
    }
    Get-ChildItem -Path $InstallDir -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    if ($pyCmd.exe -eq 'py') { $pyBase = @('py','-3') } else { $pyBase = @('python') }
    Log "creating venv at $VenvDir"
    & $pyBase[0] ($pyBase[1..($pyBase.Length-1)] + @('-m','venv',$VenvDir))
    if ($LASTEXITCODE -ne 0) { Fail 'venv creation failed' }
    $VenvPy  = Join-Path $VenvDir 'Scripts\python.exe'
    $VenvPyw = Join-Path $VenvDir 'Scripts\pythonw.exe'
    if (-not (Test-Path $VenvPy)) { Fail "venv python not found at $VenvPy" }
    Log 'venv created' 'OK'

    # --- 5. Dependencies ----------------------------------------------------
    Step 'Installing Python dependencies'
    & $VenvPy -m pip install --upgrade pip 2>&1 | Out-Null
    $req = Join-Path $InstallDir 'requirements.txt'
    if (Test-Path $req) {
        & $VenvPy -m pip install -r $req 2>&1 | Out-Null
        Log 'dependencies installed (stdlib only; requirements.txt has no packages)' 'OK'
    } else {
        Log 'no requirements.txt; stdlib only' 'OK'
    }

    # --- 6. NSSM ------------------------------------------------------------
    Step 'Checking / installing NSSM'
    New-Item -ItemType Directory -Force -Path $NssmDir | Out-Null
    if (-not (Test-Path $NssmExe)) {
        # Prefer a bundled copy in the repo.
        $bundled = @(
            (Join-Path $RepoDir 'tools\nssm\win64\nssm.exe'),
            (Join-Path $RepoDir 'tools\nssm\nssm.exe')
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($bundled) {
            Copy-Item -Path $bundled -Destination $NssmExe -Force
            Log "using bundled NSSM: $bundled" 'OK'
        } else {
            $zipUrl = 'https://nssm.cc/release/nssm-2.24.zip'
            $zip    = Join-Path $env:TEMP 'nssm-2.24.zip'
            $ex     = Join-Path $env:TEMP 'nssm-2.24'
            Log "downloading NSSM from $zipUrl"
            try {
                Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
                if (Test-Path $ex) { Remove-Item -Recurse -Force $ex }
                Expand-Archive -Path $zip -DestinationPath $ex -Force
                $found = Get-ChildItem -Path $ex -Recurse -Filter 'nssm.exe' |
                         Where-Object { $_.FullName -match 'win64' } | Select-Object -First 1
                if (-not $found) { $found = Get-ChildItem -Path $ex -Recurse -Filter 'nssm.exe' | Select-Object -First 1 }
                if (-not $found) { Fail 'nssm.exe not found in downloaded archive' }
                Copy-Item -Path $found.FullName -Destination $NssmExe -Force
                Log 'NSSM downloaded and extracted' 'OK'
            } catch {
                Fail "could not obtain NSSM automatically: $($_.Exception.Message). Place nssm.exe in tools\nssm\win64\ in the repo and re-run."
            }
        }
    } else {
        Log 'NSSM already present' 'OK'
    }

    # --- 7. Config / data folders ------------------------------------------
    Step 'Creating local config.json'
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    New-Item -ItemType Directory -Force -Path $LogDir  | Out-Null
    $cfgPath = Join-Path $DataDir 'config.json'
    if (-not (Test-Path $cfgPath)) {
        $devId = -join ((0..7) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })
        $cfg = [ordered]@{
            device_name = $env:COMPUTERNAME
            device_id   = $devId
            port        = 45781
            peers       = @()
            hotkeys     = @()
            mouse       = [ordered]@{ flush_interval_ms = 6; max_batch_ms = 12; sensitivity = 1.0; accumulate_subpixel = $true }
        }
        ($cfg | ConvertTo-Json -Depth 6) | Set-Content -Path $cfgPath -Encoding UTF8
        Log "created fresh config.json (device_id=$devId)" 'OK'
    } else {
        Log 'config.json already exists, keeping it' 'OK'
    }

    # --- 8. Install service via NSSM ---------------------------------------
    Step 'Installing FlowShift runtime as a Windows service'
    # Remove a pre-existing service first (idempotent installs).
    & $NssmExe stop   $ServiceName 2>&1 | Out-Null
    & $NssmExe remove $ServiceName confirm 2>&1 | Out-Null

    $trayPy = Join-Path $PyDir 'tray.py'
    & $NssmExe install $ServiceName $VenvPy "`"$trayPy`" --tray"
    if ($LASTEXITCODE -ne 0) { Fail 'nssm install failed' }
    & $NssmExe set $ServiceName AppDirectory $PyDir
    & $NssmExe set $ServiceName AppStdout (Join-Path $LogDir 'runtime.out')
    & $NssmExe set $ServiceName AppStderr (Join-Path $LogDir 'runtime.err')
    & $NssmExe set $ServiceName AppRotateFiles 1
    & $NssmExe set $ServiceName AppRotateBytes 1048576
    & $NssmExe set $ServiceName Start SERVICE_AUTO_START
    & $NssmExe set $ServiceName AppExit Default Restart
    & $NssmExe set $ServiceName AppRestartDelay 3000
    & $NssmExe set $ServiceName AppStopMethodConsole 5000
    & $NssmExe set $ServiceName AppEnvironmentExtra `
        "FLOWSHIFT_CONFIG=$cfgPath" "FLOWSHIFT_LOG_DIR=$LogDir"
    & $NssmExe set $ServiceName DisplayName 'FlowShift Runtime'
    & $NssmExe set $ServiceName Description 'FlowShift software KVM runtime (input forwarding).'
    Log 'service installed and configured' 'OK'

    # --- 9. Shortcuts -------------------------------------------------------
    Step 'Creating Desktop and Start Menu shortcuts'
    $guiPy   = Join-Path $PyDir 'gui.py'
    $iconPy  = Join-Path $PyDir 'flowshift.ico'
    $wsh = New-Object -ComObject WScript.Shell
    function New-Shortcut($path, $target, $args, $workdir, $icon, $desc) {
        $sc = $wsh.CreateShortcut($path)
        $sc.TargetPath = $target
        $sc.Arguments = $args
        $sc.WorkingDirectory = $workdir
        if ($icon -and (Test-Path $icon)) { $sc.IconLocation = $icon }
        $sc.Description = $desc
        $sc.Save()
    }
    $desktop   = Join-Path $env:PUBLIC 'Desktop\FlowShift.lnk'
    $startDir  = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\FlowShift'
    New-Item -ItemType Directory -Force -Path $startDir | Out-Null
    $guiArgs = "`"$guiPy`""
    New-Shortcut $desktop $VenvPyw $guiArgs $PyDir $iconPy 'Open FlowShift settings'
    New-Shortcut (Join-Path $startDir 'FlowShift GUI.lnk') $VenvPyw $guiArgs $PyDir $iconPy 'Open FlowShift settings'
    New-Shortcut (Join-Path $startDir 'FlowShift Logs.lnk') 'explorer.exe' "`"$LogDir`"" $LogDir $null 'Open FlowShift log folder'
    $uninBat = Join-Path $InstallDir 'uninstall_flowshift.bat'
    if (Test-Path $uninBat) {
        New-Shortcut (Join-Path $startDir 'Uninstall FlowShift.lnk') $uninBat '' $InstallDir $null 'Uninstall FlowShift'
    }
    Log 'shortcuts created (Desktop + Start Menu)' 'OK'

    # --- 10. Start service --------------------------------------------------
    Step 'Starting the FlowShift service'
    & $NssmExe start $ServiceName 2>&1 | Out-Null
    Start-Sleep -Seconds 2
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq 'Running') { Log 'service running' 'OK' }
    else { Log "service status: $($svc.Status)" 'WARN' }

    # --- 11. Verify control socket -----------------------------------------
    Step 'Verifying the control socket (127.0.0.1:45782)'
    $ok = $false
    for ($i = 0; $i -lt 15; $i++) {
        try {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $tcp.Connect('127.0.0.1', 45782)
            if ($tcp.Connected) { $ok = $true; $tcp.Close(); break }
        } catch { Start-Sleep -Seconds 1 }
    }
    if ($ok) {
        Log 'control socket reachable' 'OK'
    } else {
        Log 'control socket NOT reachable within 15s.' 'WARN'
        Log 'NOTE: a session-0 service cannot capture/inject interactive input.' 'WARN'
        Log "Check $LogDir\runtime.err for details." 'WARN'
    }

    # --- 12. Done -----------------------------------------------------------
    Step 'Finishing up'
    Write-Host ''
    Write-Host '================ INSTALLATION COMPLETE ================' -ForegroundColor Green
    Write-Host "Program files : $InstallDir"
    Write-Host "Data / config : $DataDir"
    Write-Host "Logs          : $LogDir"
    Write-Host "Service       : $ServiceName"
    Write-Host "GUI shortcut  : Desktop\FlowShift + Start Menu\FlowShift"
    Write-Host ''
    Write-Host 'IMPORTANT: input forwarding needs the runtime in the interactive' -ForegroundColor Yellow
    Write-Host 'user session. A session-0 service may not capture/inject input.' -ForegroundColor Yellow
    Write-Host 'See FLOWSHIFT_AUDIT_AND_FIX_REPORT.md (session-0 caveat).' -ForegroundColor Yellow
    Write-Host '======================================================' -ForegroundColor Green
    Log 'installation complete' 'OK'
}
catch {
    Fail "unhandled error: $($_.Exception.Message)"
}

Write-Host ''
Read-Host 'Press Enter to close'
exit 0
