# FlowShift installer
#
# Installs FlowShift so that input forwarding runs in the INTERACTIVE USER
# SESSION (the only place low-level hooks + SendInput work). The primary autostart
# is a Scheduled Task (AtLogOn, interactive, highest privileges = no per-start UAC),
# NOT a Windows service.
#
# A session-0 Windows service (NSSM) CANNOT capture/inject interactive input, so
# it is NOT installed by default. Pass -WithNssm to additionally install an
# OPTIONAL helper service (control socket / watchdog only) — it is explicitly NOT
# the input-forwarding path and must not be relied on for it.
#
# Run via install_flowshift.bat (double-click). Self-elevates through UAC.

param([switch]$Elevated, [switch]$WithNssm)

$ErrorActionPreference = 'Stop'
$RepoDir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir  = Join-Path $env:ProgramFiles 'FlowShift'
$DataDir     = Join-Path $env:ProgramData 'FlowShift'
$LogDir      = Join-Path $DataDir 'logs'
$InstallLog  = Join-Path $LogDir 'install.log'
$TaskName    = 'FlowShift'          # user-session autostart (primary path)
$ServiceName = 'FlowShiftRuntime'   # optional NSSM helper (NOT the input path)
$NssmDir     = Join-Path $InstallDir 'tools\nssm'
$NssmExe     = Join-Path $NssmDir 'nssm.exe'
$VenvDir     = Join-Path $InstallDir '.venv'
$PyDir       = Join-Path $InstallDir 'src\python'
$TotalSteps  = 12
$PythonMinor = 12

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-InteractiveUser {
    # The user logged on to the interactive desktop (DOMAIN\user), even when the
    # installer runs elevated as a different admin account.
    try {
        $u = (Get-CimInstance -ClassName Win32_ComputerSystem).UserName
        if ($u) { return $u }
    } catch { }
    if ($env:USERDOMAIN -and $env:USERNAME) { return "$env:USERDOMAIN\$env:USERNAME" }
    return $env:USERNAME
}

# --- Self-elevate ----------------------------------------------------------
if (-not (Test-Admin)) {
    Write-Host ''
    Write-Host 'FlowShift installation needs administrator rights' -ForegroundColor Yellow
    Write-Host '(install to Program Files, register an autostart task, etc.).'
    Write-Host 'A UAC prompt will appear now...' -ForegroundColor Yellow
    $argList = @('-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$PSCommandPath`"",'-Elevated')
    if ($WithNssm) { $argList += '-WithNssm' }
    try {
        Start-Process -FilePath 'powershell' -Verb RunAs -ArgumentList $argList
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
Log "installer started; repo=$RepoDir install=$InstallDir data=$DataDir WithNssm=$WithNssm"

try {
    # --- 1. Admin -----------------------------------------------------------
    Step 'Checking administrator rights'
    Log 'running elevated' 'OK'
    $interactiveUser = Get-InteractiveUser
    Log "interactive user (autostart target): $interactiveUser"

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
        try {
            $wg = Get-Command winget -ErrorAction SilentlyContinue
            if ($wg) {
                Log "installing via winget: Python.Python.3.$PythonMinor"
                & winget install --id "Python.Python.3.$PythonMinor" --silent `
                    --accept-package-agreements --accept-source-agreements --scope machine
                if ($LASTEXITCODE -eq 0) { $installed = $true; Log 'winget install ok' 'OK' }
            }
        } catch { Log "winget install failed: $($_.Exception.Message)" 'WARN' }
        if (-not $installed) {
            $ver = "3.$PythonMinor.7"
            $url = "https://www.python.org/ftp/python/$ver/python-$ver-amd64.exe"
            $tmp = Join-Path $env:TEMP "python-$ver-amd64.exe"
            Log "downloading Python from $url"
            try {
                Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
                Log 'running silent Python installer (InstallAllUsers=1 PrependPath=1)'
                Start-Process -FilePath $tmp -Wait -ArgumentList @('/quiet','InstallAllUsers=1','PrependPath=1','Include_launcher=1')
                $installed = $true
                Log 'Python installer finished' 'OK'
            } catch {
                Fail "could not install Python automatically: $($_.Exception.Message). Install Python 3.$PythonMinor from python.org and re-run."
            }
        }
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
        if (-not $pyCmd) { Fail 'Python still not found after install. Re-run after a reboot.' }
    }

    # --- 4. Copy app files + create venv -----------------------------------
    Step 'Installing application files and creating venv'
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Log "copying source tree to $InstallDir"
    Copy-Item -Path (Join-Path $RepoDir 'src') -Destination $InstallDir -Recurse -Force
    if (Test-Path (Join-Path $RepoDir 'docs')) {
        Copy-Item -Path (Join-Path $RepoDir 'docs') -Destination $InstallDir -Recurse -Force
    }
    foreach ($f in @('requirements.txt','README.md','uninstall_flowshift.bat','uninstall_flowshift.ps1')) {
        $src = Join-Path $RepoDir $f
        if (Test-Path $src) { Copy-Item -Path $src -Destination $InstallDir -Force }
    }
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

    # --- 6. Config / data folders + machine env vars ------------------------
    Step 'Creating config + data folders and environment'
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    New-Item -ItemType Directory -Force -Path $LogDir  | Out-Null
    # Clipboard store directories (feature is off by default; dirs are harmless).
    foreach ($cd in @(
        (Join-Path $DataDir 'clipboard'),
        (Join-Path $DataDir 'clipboard\profiles'),
        (Join-Path $DataDir 'clipboard\objects'),
        (Join-Path $DataDir 'clipboard\temp'),
        (Join-Path $DataDir 'clipboard\temp\incoming'),
        (Join-Path $DataDir 'clipboard\temp\outgoing')
    )) { New-Item -ItemType Directory -Force -Path $cd | Out-Null }
    Log 'clipboard store directories created' 'OK'
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
    # Machine-wide env so the runtime + GUI use ProgramData for config/logs.
    [System.Environment]::SetEnvironmentVariable('FLOWSHIFT_CONFIG', $cfgPath, 'Machine')
    [System.Environment]::SetEnvironmentVariable('FLOWSHIFT_LOG_DIR', $LogDir, 'Machine')
    $env:FLOWSHIFT_CONFIG = $cfgPath
    $env:FLOWSHIFT_LOG_DIR = $LogDir
    Log 'machine env FLOWSHIFT_CONFIG / FLOWSHIFT_LOG_DIR set' 'OK'

    # --- 7. Primary autostart: user-session Scheduled Task ------------------
    Step 'Registering user-session autostart (Scheduled Task, interactive)'
    $trayPy = Join-Path $PyDir 'tray.py'
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue } catch { }
    $action    = New-ScheduledTaskAction -Execute $VenvPyw -Argument "`"$trayPy`" --tray" -WorkingDirectory $PyDir
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $interactiveUser
    $principal = New-ScheduledTaskPrincipal -UserId $interactiveUser -LogonType Interactive -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Log "scheduled task '$TaskName' registered (AtLogOn, interactive, highest = no per-start UAC)" 'OK'

    # --- 8. Optional NSSM helper (NOT the input path) -----------------------
    Step 'Optional NSSM helper service'
    if ($WithNssm) {
        Log 'WithNssm set: installing OPTIONAL helper service (session 0, NOT for input forwarding)' 'WARN'
        New-Item -ItemType Directory -Force -Path $NssmDir | Out-Null
        if (-not (Test-Path $NssmExe)) {
            $bundled = @((Join-Path $RepoDir 'tools\nssm\win64\nssm.exe'),(Join-Path $RepoDir 'tools\nssm\nssm.exe')) |
                       Where-Object { Test-Path $_ } | Select-Object -First 1
            if ($bundled) { Copy-Item -Path $bundled -Destination $NssmExe -Force; Log "bundled NSSM: $bundled" 'OK' }
            else {
                try {
                    $zip = Join-Path $env:TEMP 'nssm-2.24.zip'; $ex = Join-Path $env:TEMP 'nssm-2.24'
                    Invoke-WebRequest -Uri 'https://nssm.cc/release/nssm-2.24.zip' -OutFile $zip -UseBasicParsing
                    if (Test-Path $ex) { Remove-Item -Recurse -Force $ex }
                    Expand-Archive -Path $zip -DestinationPath $ex -Force
                    $found = Get-ChildItem -Path $ex -Recurse -Filter 'nssm.exe' | Where-Object { $_.FullName -match 'win64' } | Select-Object -First 1
                    if (-not $found) { $found = Get-ChildItem -Path $ex -Recurse -Filter 'nssm.exe' | Select-Object -First 1 }
                    if ($found) { Copy-Item -Path $found.FullName -Destination $NssmExe -Force; Log 'NSSM downloaded' 'OK' }
                    else { Log 'nssm.exe not found in archive; skipping helper' 'WARN' }
                } catch { Log "NSSM download failed: $($_.Exception.Message); skipping helper" 'WARN' }
            }
        }
        if (Test-Path $NssmExe) {
            & $NssmExe stop $ServiceName 2>&1 | Out-Null
            & $NssmExe remove $ServiceName confirm 2>&1 | Out-Null
            & $NssmExe install $ServiceName $VenvPy "`"$trayPy`" --tray"
            & $NssmExe set $ServiceName AppDirectory $PyDir
            & $NssmExe set $ServiceName AppStdout (Join-Path $LogDir 'runtime.out')
            & $NssmExe set $ServiceName AppStderr (Join-Path $LogDir 'runtime.err')
            & $NssmExe set $ServiceName Start SERVICE_DEMAND_START   # manual: NOT auto, avoids a session-0 'healthy' illusion
            & $NssmExe set $ServiceName AppEnvironmentExtra "FLOWSHIFT_CONFIG=$cfgPath" "FLOWSHIFT_LOG_DIR=$LogDir"
            & $NssmExe set $ServiceName DisplayName 'FlowShift Helper (session 0 - NOT input forwarding)'
            & $NssmExe set $ServiceName Description 'Optional helper only. Session-0 service CANNOT capture/inject interactive input.'
            Log 'NSSM helper installed (manual start, not the input path)' 'OK'
        }
    } else {
        Log 'skipping NSSM (default). Input forwarding runs in the user session via the scheduled task.' 'OK'
    }

    # --- 9. Shortcuts -------------------------------------------------------
    Step 'Creating Desktop and Start Menu shortcuts'
    $guiPy  = Join-Path $PyDir 'gui.py'
    $iconPy = Join-Path $PyDir 'flowshift.ico'
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
    $desktop  = Join-Path $env:PUBLIC 'Desktop\FlowShift.lnk'
    $startDir = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\FlowShift'
    New-Item -ItemType Directory -Force -Path $startDir | Out-Null
    $guiArgs = "`"$guiPy`""
    New-Shortcut $desktop $VenvPyw $guiArgs $PyDir $iconPy 'Open FlowShift settings'
    New-Shortcut (Join-Path $startDir 'FlowShift GUI.lnk') $VenvPyw $guiArgs $PyDir $iconPy 'Open FlowShift settings'
    New-Shortcut (Join-Path $startDir 'FlowShift Logs.lnk') 'explorer.exe' "`"$LogDir`"" $LogDir $null 'Open FlowShift log folder'
    $uninBat = Join-Path $InstallDir 'uninstall_flowshift.bat'
    if (Test-Path $uninBat) { New-Shortcut (Join-Path $startDir 'Uninstall FlowShift.lnk') $uninBat '' $InstallDir $null 'Uninstall FlowShift' }
    Log 'shortcuts created (Desktop + Start Menu)' 'OK'

    # --- 10. Start the runtime now (in the interactive session) -------------
    Step 'Starting the FlowShift runtime (interactive session)'
    try {
        Start-ScheduledTask -TaskName $TaskName
        Log 'scheduled task started (runs pythonw tray.py --tray in the user session)' 'OK'
    } catch {
        Log "could not start the scheduled task now: $($_.Exception.Message). It will start at next logon." 'WARN'
    }
    Start-Sleep -Seconds 2

    # --- 11. Verify control socket + session --------------------------------
    Step 'Verifying the control socket (127.0.0.1:45782)'
    $ok = $false
    for ($i = 0; $i -lt 15; $i++) {
        try {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $tcp.Connect('127.0.0.1', 45782)
            if ($tcp.Connected) { $ok = $true; $tcp.Close(); break }
        } catch { Start-Sleep -Seconds 1 }
    }
    if ($ok) { Log 'control socket reachable (runtime running in user session)' 'OK' }
    else {
        Log 'control socket not reachable within 15s.' 'WARN'
        Log 'If you started the installer from a different (elevated) account, log on as the' 'WARN'
        Log 'interactive user; the task starts the runtime at logon. Check runtime.err in logs.' 'WARN'
    }

    # --- 12. Done -----------------------------------------------------------
    Step 'Finishing up'
    Write-Host ''
    Write-Host '================ INSTALLATION COMPLETE ================' -ForegroundColor Green
    Write-Host "Program files : $InstallDir"
    Write-Host "Data / config : $DataDir"
    Write-Host "Logs          : $LogDir"
    Write-Host "Autostart     : Scheduled Task '$TaskName' (interactive user session)"
    if ($WithNssm) { Write-Host "NSSM helper   : $ServiceName (manual, NOT the input path)" }
    Write-Host "GUI shortcut  : Desktop\FlowShift + Start Menu\FlowShift"
    Write-Host ''
    Write-Host 'Input forwarding runs in your INTERACTIVE session via the scheduled task.' -ForegroundColor Green
    Write-Host 'The GUI shows a red warning if the runtime ever runs in Session 0.' -ForegroundColor Green
    Write-Host '======================================================' -ForegroundColor Green
    Log 'installation complete' 'OK'
}
catch {
    Fail "unhandled error: $($_.Exception.Message)"
}

Write-Host ''
Read-Host 'Press Enter to close'
exit 0
