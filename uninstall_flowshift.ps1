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
    $shell = if (Get-Command 'pwsh' -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell' }
    try {
        Start-Process -FilePath $shell -Verb RunAs -ArgumentList @(
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

function New-DefaultInstallState {
    return [ordered]@{
        installed_by_flowshift = [ordered]@{ python = $false; nodejs = $false; vite = $false }
        detected_before_install = [ordered]@{ python = $null; node = $null; npm = $null }
        used_tools = [ordered]@{ python = $null; node = $null; npm = $null; npx = $null; vite = $null }
        versions = [ordered]@{ python = $null; node = $null; npm = $null; npx = $null; vite = $null }
        details = [ordered]@{
            python = [ordered]@{ installed_by_flowshift = $false; install_method = $null; package_id = $null }
            nodejs = [ordered]@{ installed_by_flowshift = $false; install_method = $null; package_id = $null }
            vite = [ordered]@{ installed_by_flowshift = $false; install_method = $null; package_id = $null; scope = 'project-local' }
        }
    }
}

function Read-InstallState {
    $path = Join-Path $DataDir 'install_state.json'
    if (Test-Path $path) {
        try { return (Get-Content -LiteralPath $path -Raw -ErrorAction Stop | ConvertFrom-Json) } catch { }
    }
    return (New-DefaultInstallState | ConvertTo-Json -Depth 8 | ConvertFrom-Json)
}

function Get-PropValue {
    param($Obj, [string]$Name)
    try { return $Obj.$Name } catch { return $null }
}

function Invoke-WingetUninstall {
    param([string]$PackageId)
    if (-not $PackageId) { return $false }
    $wg = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $wg) { return $false }
    & winget uninstall --id $PackageId --silent --accept-source-agreements
    return ($LASTEXITCODE -eq 0)
}

function Get-InstalledAppUninstallString {
    param([string]$DisplayName)
    $root = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall'
    try {
        foreach ($key in Get-ChildItem -Path $root -ErrorAction SilentlyContinue) {
            try {
                $p = Get-ItemProperty -Path $key.PSPath -ErrorAction SilentlyContinue
                if ($p.DisplayName -eq $DisplayName -and $p.UninstallString) { return [string]$p.UninstallString }
            } catch { }
        }
    } catch { }
    return $null
}

function Invoke-UninstallString {
    param([string]$UninstallString)
    if (-not $UninstallString) { return $false }
    try {
        if ($UninstallString -match '^"([^"]+)"\s*(.*)$') {
            $file = $Matches[1]
            $args = $Matches[2]
        } else {
            $parts = $UninstallString.Split(' ', 2)
            $file = $parts[0]
            $args = if ($parts.Count -gt 1) { $parts[1] } else { '' }
        }
        if (-not (Test-Path $file)) { return $false }
        Start-Process -FilePath $file -ArgumentList $args -Wait
        return $true
    } catch {
        return $false
    }
}

function Invoke-FlowShiftShutdown {
    try {
        $sock = New-Object System.Net.Sockets.TcpClient
        $ar = $sock.BeginConnect('127.0.0.1', 45782, $null, $null)
        if ($ar.AsyncWaitHandle.WaitOne(400)) {
            $sock.EndConnect($ar)
            $stream = $sock.GetStream()
            $payload = [Text.Encoding]::UTF8.GetBytes((ConvertTo-Json @{ type = 'shutdown'; reason = 'uninstaller' } -Compress))
            $bw = New-Object System.IO.BinaryWriter($stream)
            $bw.Write([System.Net.IPAddress]::HostToNetworkOrder([int]$payload.Length))
            $bw.Write($payload)
            $bw.Flush()
            $stream.Close(); $sock.Close()
            return $true
        }
    } catch { }
    return $false
}

function Wait-FlowShiftPortsClosed {
    param([int[]]$Ports = @(45782, 45781), [int]$TimeoutSec = 10)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $open = @()
            foreach ($port in $Ports) {
                $open += Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
            }
            if (-not $open) { return $true }
        } catch {
            return $true
        }
        Start-Sleep -Milliseconds 250
    }
    return $false
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
        Log "could not enumerate overlay_host.py processes: $($_.Exception.Message)" 'WARN'
        return
    }

    if ($overlayProcesses.Count -eq 0) {
        Log 'no overlay_host.py processes were running' 'OK'
        return
    }

    foreach ($process in $overlayProcesses) {
        $commandLine = [string]$process.CommandLine
        $executable = [string]$process.ExecutablePath
        $ownedByExecutable = $executable.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)
        $ownedOverlayPathPattern = [regex]::Escape($rootPrefix) + '[^"\r\n]*overlay_host\.py(?:["\s]|$)'
        $ownedByCommandPath = $commandLine -match $ownedOverlayPathPattern
        if (-not ($ownedByExecutable -or $ownedByCommandPath)) {
            Log "left unrelated overlay_host.py PID $($process.ProcessId) running; no executable or command path is rooted under $root" 'WARN'
            continue
        }
        try {
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction Stop
            Log "stopped FlowShift-owned overlay_host.py PID $($process.ProcessId)" 'OK'
        } catch {
            Log "could not stop FlowShift-owned overlay_host.py PID $($process.ProcessId): $($_.Exception.Message)" 'WARN'
        }
    }
}

function Get-ProcessCommandLine {
    param([int]$ProcessId)
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
        return [string]$process.CommandLine
    } catch {
        return ''
    }
}

function Get-ProcessExecutablePath {
    param([int]$ProcessId)
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
        return [string]$process.ExecutablePath
    } catch {
        return ''
    }
}

function Test-FlowShiftProcess {
    param([int]$ProcessId, [string]$RootDir)
    $root = ([string]$RootDir).TrimEnd('\')
    $commandLine = Get-ProcessCommandLine -ProcessId $ProcessId
    $executable = Get-ProcessExecutablePath -ProcessId $ProcessId
    foreach ($candidate in @($commandLine, $executable)) {
        if ($candidate -and ([string]$candidate) -match [regex]::Escape($root)) { return $true }
    }
    return $false
}

Write-Host ''
Write-Host '========================================' -ForegroundColor White
Write-Host '       FlowShift Uninstaller' -ForegroundColor White
Write-Host '========================================' -ForegroundColor White
Log 'uninstall started'

# Ask the productive runtime to shut down before removing any of its launchers.
if (Invoke-FlowShiftShutdown) {
    Log 'requested clean core runtime shutdown via control socket as the first shutdown action' 'OK'
} else {
    Log 'clean core runtime shutdown was unavailable; continuing with owned-process verification' 'WARN'
}
if (Wait-FlowShiftPortsClosed) {
    Log 'control/peer ports closed after the clean shutdown attempt' 'OK'
} else {
    Log 'control/peer ports remained open after the clean shutdown attempt' 'WARN'
}

# 1. Stop + remove the service.
Write-Host ''
Write-Host '[1/7] Stopping and removing the service' -ForegroundColor Cyan
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

# 2. Remove firewall rules added by the installer.
Write-Host ''
Write-Host '[2/7] Removing Windows Firewall rules' -ForegroundColor Cyan
try {
    Get-NetFirewallRule -DisplayName 'FlowShift*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
    Log 'firewall rules removed' 'OK'
} catch { Log "could not remove firewall rules: $($_.Exception.Message)" 'WARN' }

# 3. Remove the user-session autostart scheduled task.
Write-Host ''
Write-Host '[3/7] Removing autostart scheduled task (if present)' -ForegroundColor Cyan
try {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($t) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Log 'scheduled task removed' 'OK'
    } else { Log 'no scheduled task' 'OK' }
} catch { Log "scheduled task check failed: $($_.Exception.Message)" 'WARN' }
# Remove Apps & Features entries.
try {
    $coreKey = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FlowShift'
    $webKey = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FlowShift Web GUI'
    foreach ($k in @($coreKey, $webKey)) {
        if (Test-Path $k) { Remove-Item -Path $k -Recurse -Force -ErrorAction SilentlyContinue }
    }
    Log 'Apps & Features registry entries removed' 'OK'
} catch { Log "could not remove registry keys: $($_.Exception.Message)" 'WARN' }
# Remove machine env vars set by the installer.
try {
    $envReg = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
    Remove-ItemProperty -Path $envReg -Name 'FLOWSHIFT_CONFIG' -Force -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $envReg -Name 'FLOWSHIFT_LOG_DIR' -Force -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $envReg -Name 'FLOWSHIFT_WEBGUI_DIR' -Force -ErrorAction SilentlyContinue
    Log 'machine env FLOWSHIFT_CONFIG / FLOWSHIFT_LOG_DIR / FLOWSHIFT_WEBGUI_DIR cleared' 'OK'
} catch { Log "could not clear env vars: $($_.Exception.Message)" 'WARN' }

$installState = Read-InstallState

# 4. Stop any remaining FlowShift-owned runtime/overlay processes.
Write-Host ''
Write-Host '[4/7] Stopping lingering FlowShift processes' -ForegroundColor Cyan
Stop-FlowShiftOwnedOverlayHosts
if (-not (Wait-FlowShiftPortsClosed -TimeoutSec 2)) {
    $processIds = @()
    foreach ($port in @(45782, 45781)) {
        try {
            $processIds += Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique
        } catch { }
    }
    $processIds = $processIds | Where-Object { $_ } | Sort-Object -Unique
    $matches = @()
    foreach ($processId in $processIds) {
        $commandLine = Get-ProcessCommandLine -ProcessId ([int]$processId)
        $executable = Get-ProcessExecutablePath -ProcessId ([int]$processId)
        if (Test-FlowShiftProcess -ProcessId ([int]$processId) -RootDir $InstallDir) {
            $matches += [pscustomobject]@{
                Pid = [int]$processId
                CmdLine = $commandLine
                Exe = $executable
            }
        } else {
            Log ("leaving PID {0} alone (not under {1}): {2} {3}" -f $processId, $InstallDir, $executable, $commandLine) 'WARN'
        }
    }
    if ($matches.Count -gt 0) {
        Write-Host 'The following FlowShift processes are still running:' -ForegroundColor Yellow
        $matches | ForEach-Object {
            Write-Host ("  PID {0}: {1} {2}" -f $_.Pid, $_.Exe, $_.CmdLine) -ForegroundColor Yellow
        }
        $answer = Read-Host 'Stop these FlowShift processes now? [y/N]'
        if ($answer -match '^[yY]') {
            foreach ($match in $matches) {
                try {
                    Stop-Process -Id $match.Pid -ErrorAction Stop
                    Log "stopped PID $($match.Pid) after confirmation" 'OK'
                } catch {
                    Log "could not stop PID $($match.Pid): $($_.Exception.Message)" 'WARN'
                }
            }
        } else {
            Log 'user chose not to stop lingering FlowShift processes' 'WARN'
        }
    }
}
if (Wait-FlowShiftPortsClosed -TimeoutSec 2) {
    Log 'control/peer ports are free before program-file removal' 'OK'
} else {
    Log 'control/peer ports are still in use after the owned-process check' 'WARN'
}

# 5. Remove shortcuts + Start Menu folder.
Write-Host ''
Write-Host '[5/7] Removing shortcuts' -ForegroundColor Cyan
$desktop  = Join-Path $env:PUBLIC 'Desktop\FlowShift.lnk'
$webDesktopUser = Join-Path ([Environment]::GetFolderPath('Desktop')) 'FlowShift Web GUI.lnk'
$webDesktopPublic = Join-Path $env:PUBLIC 'Desktop\FlowShift Web GUI.lnk'
$startDir = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\FlowShift'
if (Test-Path $desktop)  { Remove-Item -Path $desktop -Force -ErrorAction SilentlyContinue; Log 'desktop shortcut removed' }
foreach ($f in @(
    (Join-Path $startDir 'FlowShift Web GUI.lnk'),
    (Join-Path $startDir 'Uninstall FlowShift Web GUI.lnk'),
    $webDesktopUser,
    $webDesktopPublic
)) {
    if (Test-Path $f) { Remove-Item -Path $f -Force -ErrorAction SilentlyContinue; Log "removed $f" 'OK' }
}
if (Test-Path $startDir) { Remove-Item -Path $startDir -Recurse -Force -ErrorAction SilentlyContinue; Log 'start menu folder removed' }

# 6. Remove program files.
Write-Host ''
Write-Host '[6/7] Removing program files' -ForegroundColor Cyan
if (Test-Path $InstallDir) {
    try {
        Remove-Item -Path $InstallDir -Recurse -Force -ErrorAction Stop
        Log "removed $InstallDir" 'OK'
    } catch {
        Log "could not fully remove $InstallDir : $($_.Exception.Message)" 'WARN'
        Log 'a file may be in use; reboot and delete the folder manually if needed.' 'WARN'
    }
} else { Log 'program folder not present' 'OK' }

# 6b. Remove FlowShift-owned prerequisites only if we installed them.
Write-Host ''
Write-Host '[6b/7] Optional prerequisite removal' -ForegroundColor Cyan
Log 'WebView2 Evergreen is shared and will not be uninstalled' 'OK'
$details = Get-PropValue $installState 'details'
$pythonState = if ($details) { Get-PropValue $details 'python' } else { $null }
$nodeState = if ($details) { Get-PropValue $details 'nodejs' } else { $null }
$ownedPython = [bool](Get-PropValue $pythonState 'installed_by_flowshift')
$ownedNode = [bool](Get-PropValue $nodeState 'installed_by_flowshift')
$pythonPkg = Get-PropValue $pythonState 'package_id'
$nodePkg = Get-PropValue $nodeState 'package_id'
if ($ownedPython) {
    Write-Host 'FlowShift installed Python during setup.' -ForegroundColor Yellow
    $pyMethod = [string](Get-PropValue $pythonState 'install_method')
    $pyDisplay = 'Python'
    if ($pyMethod -eq 'winget' -and $pythonPkg) {
        $ans = Read-Host 'Remove FlowShift-installed Python now? [y/N]'
        if ($ans -match '^[yY]') {
            if (Invoke-WingetUninstall -PackageId $pythonPkg) { Log "removed FlowShift-installed Python ($pythonPkg)" 'OK' } else { Log "could not remove Python automatically ($pythonPkg)" 'WARN' }
        }
    } elseif ($pyMethod -eq 'installer' -or $pyMethod -eq 'msi') {
        $uninstallString = [string](Get-PropValue $pythonState 'uninstall_string')
        if (-not $uninstallString) { $uninstallString = Get-InstalledAppUninstallString 'Python 3.12.9 (64-bit)' }
        if ($uninstallString) {
            Write-Host "Python was installed via MSI/installer fallback. Uninstall command found in registry." -ForegroundColor Yellow
            $ans = Read-Host 'Run the Python uninstall command now? [y/N]'
            if ($ans -match '^[yY]') {
                if (Invoke-UninstallString -UninstallString $uninstallString) { Log 'Python uninstall command executed' 'OK' } else { Log 'Python uninstall command failed' 'WARN' }
            }
        } else {
            Log 'Python was installed via MSI/installer fallback. Automatic removal is not implemented for this method. Please remove it via Windows Apps & Features.' 'WARN'
        }
    } else {
        Log 'Python was installed by FlowShift, but no package id is recorded for automatic removal.' 'WARN'
    }
} else {
    Log 'Python was already installed before FlowShift; it will not be removed.' 'OK'
}
if ($ownedNode) {
    Write-Host 'FlowShift installed Node.js during setup.' -ForegroundColor Yellow
    $nodeMethod = [string](Get-PropValue $nodeState 'install_method')
    if ($nodeMethod -eq 'winget' -and $nodePkg) {
        $ans = Read-Host 'Remove FlowShift-installed Node.js now? [y/N]'
        if ($ans -match '^[yY]') {
            if (Invoke-WingetUninstall -PackageId $nodePkg) { Log "removed FlowShift-installed Node.js ($nodePkg)" 'OK' } else { Log "could not remove Node.js automatically ($nodePkg)" 'WARN' }
        }
    } elseif ($nodeMethod -eq 'installer' -or $nodeMethod -eq 'msi') {
        $uninstallString = [string](Get-PropValue $nodeState 'uninstall_string')
        if (-not $uninstallString) { $uninstallString = Get-InstalledAppUninstallString 'Node.js' }
        if ($uninstallString) {
            Write-Host "Node.js was installed via MSI/installer fallback. Uninstall command found in registry." -ForegroundColor Yellow
            $ans = Read-Host 'Run the Node.js uninstall command now? [y/N]'
            if ($ans -match '^[yY]') {
                if (Invoke-UninstallString -UninstallString $uninstallString) { Log 'Node.js uninstall command executed' 'OK' } else { Log 'Node.js uninstall command failed' 'WARN' }
            }
        } else {
            Log 'Node.js was installed via MSI/installer fallback. Automatic removal is not implemented for this method. Please remove it via Windows Apps & Features.' 'WARN'
        }
    } else {
        Log 'Node.js was installed by FlowShift, but no package id is recorded for automatic removal.' 'WARN'
    }
} else {
    Log 'Node.js was already installed before FlowShift; it will not be removed.' 'OK'
}

$viteScope = if ($details) { Get-PropValue (Get-PropValue $details 'vite') 'scope' } else { $null }
if ($viteScope -eq 'project-local') {
    $viteSource = [string](Get-PropValue (Get-PropValue $details 'vite') 'source_path')
    $viteNodeModules = [string](Get-PropValue (Get-PropValue $details 'vite') 'node_modules')
    Log "Vite was installed project-locally in $viteNodeModules" 'OK'
    if ($viteSource) { Log "WebGUI source path: $viteSource" 'OK' }
    if ($viteNodeModules -and (Test-Path $viteNodeModules)) {
        $ans = Read-Host 'Remove WebGUI source node_modules created by the installer? [y/N]'
        if ($ans -match '^[yY]') {
            try {
                Remove-Item -Path $viteNodeModules -Recurse -Force -ErrorAction Stop
                Log "removed project-local Vite node_modules at $viteNodeModules" 'OK'
            } catch {
                Log ("could not remove project-local Vite node_modules at {0}: {1}" -f $viteNodeModules, $_.Exception.Message) 'WARN'
            }
        }
    }
}

$statePath = Join-Path $DataDir 'install_state.json'
if (Test-Path $statePath) {
    try {
        Remove-Item -Path $statePath -Force -ErrorAction SilentlyContinue
        Log 'install_state.json removed' 'OK'
    } catch { }
}

# 7. Optionally remove data (config + logs + clipboard history).
Write-Host ''
Write-Host '[7/7] Local config, logs and clipboard history' -ForegroundColor Cyan
$purge = $PurgeData
if (-not $purge) {
    $ans = Read-Host 'Also delete local config, logs AND clipboard history in ProgramData\FlowShift? (y/N)'
    if ($ans -match '^[yY]') { $purge = $true }
}
if ($purge) {
    if (Test-Path $DataDir) {
        try { Remove-Item -Path $DataDir -Recurse -Force -ErrorAction Stop; Write-Host "Removed $DataDir (incl. clipboard history)" -ForegroundColor Green }
        catch { Write-Host "Could not remove $DataDir : $($_.Exception.Message)" -ForegroundColor Yellow }
    }
} else {
    # Even when keeping config/logs, always clean transient clipboard temp files.
    $clipTemp = Join-Path $DataDir 'clipboard\temp'
    if (Test-Path $clipTemp) {
        try { Get-ChildItem -Path $clipTemp -Recurse -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue; Log 'clipboard temp cleaned' 'OK' } catch { }
    }
    Log "kept data (incl. clipboard history) in $DataDir" 'OK'
}

Write-Host ''
Write-Host '================ UNINSTALL COMPLETE ================' -ForegroundColor Green
Write-Host 'Service, shortcuts and program files removed.'
if (-not $purge) { Write-Host "Local config/logs kept in: $DataDir" }
Write-Host '===================================================' -ForegroundColor Green
Write-Host ''
Read-Host 'Press Enter to close'
exit 0
