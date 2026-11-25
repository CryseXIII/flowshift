# FlowShift uninstaller
#
# Stops FlowShift, removes the autostart task, the optional NSSM helper,
# shortcuts, the Start Menu folder and the program files in
# Program Files\FlowShift, including the WebGUI (Program Files\FlowShift\webgui).
#
# User data in ProgramData\FlowShift (config.json, clipboard history, logs) is
# kept unless -PurgeData is passed explicitly. Apart from the Windows UAC prompt
# nothing is asked; -Interactive restores the confirmation questions (data
# purge, removal of Python/Node.js that FlowShift installed).
#
# Run via uninstall_flowshift.bat (double-click). Self-elevates through UAC.

param([switch]$Elevated, [switch]$PurgeData, [switch]$Interactive)

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
        $argList = @('-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$PSCommandPath`"",'-Elevated')
        if ($PurgeData) { $argList += '-PurgeData' }
        if ($Interactive) { $argList += '-Interactive' }
        Start-Process -FilePath $shell -Verb RunAs -ArgumentList $argList
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

function Confirm-Choice {
    # Questions are opt-in (-Interactive); without it the safe answer is "no".
    param([string]$Question)
    if (-not $Interactive) {
        Log "skipped without -Interactive: $Question" 'INFO'
        return $false
    }
    return ((Read-Host $Question) -match '^[yY]')
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

# --- Install lock (same functions as in install_flowshift.ps1) ---------------
# The uninstaller waits for a running installation or update instead of
# removing files underneath it.
function Get-InstallLockPath {
    param([Parameter(Mandatory = $true)][string]$DataDir)
    return (Join-Path $DataDir 'updates\install.lock')
}
function Read-InstallLockOwner {
    param([Parameter(Mandatory = $true)][string]$DataDir)
    try {
        $stream = New-Object System.IO.FileStream((Get-InstallLockPath $DataDir), [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try {
            $text = (New-Object System.IO.StreamReader($stream, (New-Object System.Text.UTF8Encoding($false)))).ReadToEnd()
        } finally { $stream.Dispose() }
        if ([string]::IsNullOrWhiteSpace($text)) { return $null }
        return ($text | ConvertFrom-Json)
    } catch {
        return $null
    }
}
function Format-InstallLockOwner {
    param([object]$Owner)
    if ($null -eq $Owner) { return 'unknown owner' }
    return ('{0} {1}, process {2}, since {3}' -f $Owner.owner, $Owner.version, $Owner.pid, $Owner.since)
}
function Enter-InstallLock {
    param(
        [Parameter(Mandatory = $true)][string]$DataDir,
        [Parameter(Mandatory = $true)][string]$Owner,
        [string]$Version = '',
        [int]$TimeoutSec = 0,
        [scriptblock]$OnWait
    )
    $path = Get-InstallLockPath $DataDir
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $path)) | Out-Null
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds([Math]::Max($TimeoutSec, 0))
    $announced = $false
    while ($true) {
        try {
            $stream = New-Object System.IO.FileStream($path, [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::Read)
            break
        } catch {
            $inner = $_.Exception
            while ($null -ne $inner.InnerException) { $inner = $inner.InnerException }
            if ($inner -isnot [System.IO.IOException]) { throw }
            if (-not $announced -and $null -ne $OnWait) {
                & $OnWait (Read-InstallLockOwner $DataDir)
                $announced = $true
            }
            if ([DateTimeOffset]::UtcNow -ge $deadline) { return $null }
            Start-Sleep -Milliseconds 500
        }
    }
    $token = [guid]::NewGuid().ToString('N')
    $content = [ordered]@{
        schema_version = 1
        owner = $Owner
        version = $Version
        pid = $PID
        token = $token
        since = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes((($content | ConvertTo-Json -Compress) + "`n"))
    $stream.SetLength(0)
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush()
    return [pscustomobject]@{ Stream = $stream; Token = $token; Path = $path }
}
function Exit-InstallLock {
    param([object]$Lock)
    if ($null -eq $Lock -or $null -eq $Lock.Stream) { return }
    try { $Lock.Stream.SetLength(0) } catch { }
    try { $Lock.Stream.Dispose() } catch { }
    $Lock.Stream = $null
}
Write-Host ''
Write-Host '========================================' -ForegroundColor White
Write-Host '       FlowShift Uninstaller' -ForegroundColor White
Write-Host '========================================' -ForegroundColor White
Log 'uninstall started'

$LockWaitSec = 1200
$onLockWait = {
    param($Owner)
    Log ("a FlowShift installation or update is running ({0}); waiting until it has finished (up to {1} minutes)" -f
        (Format-InstallLockOwner $Owner), [int]($LockWaitSec / 60)) 'WARN'
}
$InstallLock = $null
try {
    $InstallLock = Enter-InstallLock -DataDir $DataDir -Owner 'uninstall' -TimeoutSec $LockWaitSec -OnWait $onLockWait
} catch {
    Log "could not take the FlowShift install lock: $($_.Exception.Message); continuing" 'WARN'
    $InstallLock = $true
}
if ($null -eq $InstallLock) {
    Log "a FlowShift installation or update is still running after $([int]($LockWaitSec / 60)) minutes; nothing was removed. Run the uninstaller again later." 'ERROR'
    if ($Interactive) { Read-Host 'Press Enter to close' | Out-Null }
    exit 1
}

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
    # Processes running from the program folder are FlowShift's own; they must
    # stop before the folder can be removed.
    foreach ($match in $matches) {
        try {
            Stop-Process -Id $match.Pid -Force -ErrorAction Stop
            Log "stopped FlowShift PID $($match.Pid): $($match.Exe) $($match.CmdLine)" 'OK'
        } catch {
            Log "could not stop PID $($match.Pid): $($_.Exception.Message)" 'WARN'
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
if (Test-Path -LiteralPath $InstallDir) {
    try {
        Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction Stop
        Log "removed $InstallDir" 'OK'
    } catch {
        Log "could not fully remove $InstallDir : $($_.Exception.Message)" 'WARN'
        Log 'a file may be in use; reboot and delete the folder manually if needed.' 'WARN'
    }
} else { Log 'program folder not present' 'OK' }
# The automatic updater keeps the previous version next to the program folder
# (Program Files\FlowShift.rollback-<version>) and unpacks development states
# to ProgramData\FlowShift\updates\source; neither is user data.
foreach ($rollback in @(Get-ChildItem -LiteralPath (Split-Path -Parent $InstallDir) -Directory -Filter 'FlowShift.rollback-*' -ErrorAction SilentlyContinue)) {
    try {
        Remove-Item -LiteralPath $rollback.FullName -Recurse -Force -ErrorAction Stop
        Log "removed update rollback copy $($rollback.FullName)" 'OK'
    } catch {
        Log "could not remove $($rollback.FullName) : $($_.Exception.Message)" 'WARN'
    }
}
# Staging and previous-version folders of install_flowshift.ps1 and the failed
# version the update runner moves aside during a rollback.
$programFilesRoot = Split-Path -Parent $InstallDir
foreach ($pattern in @('FlowShift.staging', 'FlowShift.previous', 'FlowShift.failed-*')) {
    foreach ($extra in @(Get-ChildItem -LiteralPath $programFilesRoot -Directory -Filter $pattern -ErrorAction SilentlyContinue)) {
        try {
            Remove-Item -LiteralPath $extra.FullName -Recurse -Force -ErrorAction Stop
            Log "removed $($extra.FullName)" 'OK'
        } catch {
            Log "could not remove $($extra.FullName) : $($_.Exception.Message)" 'WARN'
        }
    }
}
$manualMarker = Join-Path $DataDir 'updates\manual_install.json'
if (Test-Path -LiteralPath $manualMarker) { Remove-Item -LiteralPath $manualMarker -Force -ErrorAction SilentlyContinue }
# updates\usb-build: work folder of the USB package button (Software Update);
# the finished ZIP is in the user's Downloads folder and stays.
# updates\peer_outbox: package this PC offers to connected PCs with an older
# version; updates\peer_update.json: last update from a connected PC
# (peer_update.py). A package fetched from another PC is in updates\downloads.
foreach ($leftover in @((Join-Path $DataDir 'updates\source'), (Join-Path $DataDir 'updates\downloads'),
        (Join-Path $DataDir 'updates\usb-build'), (Join-Path $DataDir 'updates\peer_outbox'),
        (Join-Path $DataDir 'updates\peer_update.json'))) {
    if (Test-Path -LiteralPath $leftover) {
        Remove-Item -LiteralPath $leftover -Recurse -Force -ErrorAction SilentlyContinue
        Log "removed update leftovers in $leftover" 'OK'
    }
}

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
        if (Confirm-Choice 'Remove FlowShift-installed Python now? [y/N]') {
            if (Invoke-WingetUninstall -PackageId $pythonPkg) { Log "removed FlowShift-installed Python ($pythonPkg)" 'OK' } else { Log "could not remove Python automatically ($pythonPkg)" 'WARN' }
        }
    } elseif ($pyMethod -eq 'installer' -or $pyMethod -eq 'msi') {
        $uninstallString = [string](Get-PropValue $pythonState 'uninstall_string')
        if ($uninstallString) {
            Write-Host "Python was installed via MSI/installer fallback. Uninstall command found in registry." -ForegroundColor Yellow
            if (Confirm-Choice 'Run the Python uninstall command now? [y/N]') {
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
        if (Confirm-Choice 'Remove FlowShift-installed Node.js now? [y/N]') {
            if (Invoke-WingetUninstall -PackageId $nodePkg) { Log "removed FlowShift-installed Node.js ($nodePkg)" 'OK' } else { Log "could not remove Node.js automatically ($nodePkg)" 'WARN' }
        }
    } elseif ($nodeMethod -eq 'installer' -or $nodeMethod -eq 'msi') {
        $uninstallString = [string](Get-PropValue $nodeState 'uninstall_string')
        if (-not $uninstallString) { $uninstallString = Get-InstalledAppUninstallString 'Node.js' }
        if ($uninstallString) {
            Write-Host "Node.js was installed via MSI/installer fallback. Uninstall command found in registry." -ForegroundColor Yellow
            if (Confirm-Choice 'Run the Node.js uninstall command now? [y/N]') {
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
        if (Confirm-Choice 'Remove WebGUI source node_modules created by the installer? [y/N]') {
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
# Program files are gone; release the lock so -PurgeData can remove its file too.
if ($InstallLock -isnot [bool]) { Exit-InstallLock $InstallLock }
$purge = [bool]$PurgeData
if (-not $purge) {
    $purge = Confirm-Choice 'Also delete local config, logs AND clipboard history in ProgramData\FlowShift? (y/N)'
}
if ($purge) {
    # The Forgejo access of the development update channel lives in the Windows
    # credential store of the user, not in ProgramData; remove it with the data.
    try {
        & cmdkey.exe "/delete:FlowShift:forgejo-update" 2>&1 | Out-Null
        Log 'Forgejo update access removed from the credential store (if present)' 'OK'
    } catch { }
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
    # File-card previews are a regenerable cache, never user data.
    $clipPreviews = Join-Path $DataDir 'clipboard\previews'
    if (Test-Path $clipPreviews) {
        try { Remove-Item -Path $clipPreviews -Recurse -Force -ErrorAction Stop; Log 'clipboard preview cache removed' 'OK' } catch { }
    }
    Log "kept data (incl. clipboard history) in $DataDir" 'OK'
}

Write-Host ''
Write-Host '================ UNINSTALL COMPLETE ================' -ForegroundColor Green
Write-Host 'Autostart, shortcuts, program files and WebGUI removed.'
if (-not $purge) { Write-Host "Your config, clipboard history and logs were kept in: $DataDir (use -PurgeData to delete them)" }
Write-Host '===================================================' -ForegroundColor Green
Write-Host ''
if ($Interactive) { Read-Host 'Press Enter to close' | Out-Null } else { Start-Sleep -Seconds 10 }
exit 0
