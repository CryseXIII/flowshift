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

param(
    [switch]$Elevated,
    [switch]$WithNssm,
    [bool]$InstallPythonIfMissing = $true,
    [bool]$UpgradePython = $false,
    [ValidateSet('LatestStable','Latest')][string]$PythonChannel = 'LatestStable',
    [switch]$SkipPythonInstall,
    [switch]$NonInteractive,
    [switch]$FlowUpdate
)

$ErrorActionPreference = 'Stop'
if ($FlowUpdate) { $NonInteractive = $true }
$RepoDir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir  = Join-Path $env:ProgramFiles 'FlowShift'
$DataDir     = Join-Path $env:ProgramData 'FlowShift'
$LogDir      = Join-Path $DataDir 'logs'
$InstallLog  = Join-Path $LogDir 'install.log'
$InstallStatePath = Join-Path $DataDir 'install_state.json'
$TaskName    = 'FlowShift'          # user-session autostart (primary path)
$ServiceName = 'FlowShiftRuntime'   # optional NSSM helper (NOT the input path)
$NssmDir     = Join-Path $InstallDir 'tools\nssm'
$NssmExe     = Join-Path $NssmDir 'nssm.exe'
$VenvDir     = Join-Path $InstallDir '.venv'
$PyDir       = Join-Path $InstallDir 'src\python'
$TotalSteps  = 13
$PythonMinor = 12
$VersionPath = Join-Path $RepoDir 'VERSION'

function Get-ProductVersion {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "VERSION file is missing: $Path"
    }
    $value = ([System.IO.File]::ReadAllText($Path)).Trim()
    $semVer = '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$'
    if ($value -notmatch $semVer) {
        throw "VERSION is not valid SemVer: '$value'"
    }
    return $value
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function New-DefaultInstallState {
    return [ordered]@{
        installed_by_flowshift = [ordered]@{ python = $false; nodejs = $false; vite = $false }
        detected_before_install = [ordered]@{ python = $null; node = $null; npm = $null }
        used_tools = [ordered]@{ python = $null; node = $null; npm = $null; npx = $null; vite = $null }
        versions = [ordered]@{ python = $null; node = $null; npm = $null; npx = $null; vite = $null }
        details = [ordered]@{
            python = [ordered]@{ installed_by_flowshift = $false; install_method = $null; package_id = $null; uninstall_string = $null }
            nodejs = [ordered]@{ installed_by_flowshift = $false; install_method = $null; package_id = $null; uninstall_string = $null }
            vite = [ordered]@{ installed_by_flowshift = $false; install_method = $null; package_id = $null; scope = 'project-local'; source_path = $null; node_modules = $null }
        }
    }
}

function Read-InstallState {
    if (Test-Path $InstallStatePath) {
        try {
            return (Get-Content -LiteralPath $InstallStatePath -Raw -ErrorAction Stop | ConvertFrom-Json)
        } catch { }
    }
    return (New-DefaultInstallState | ConvertTo-Json -Depth 8 | ConvertFrom-Json)
}

function Write-InstallState {
    param($State)
    try {
        New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
        ($State | ConvertTo-Json -Depth 12) | Set-Content -LiteralPath $InstallStatePath -Encoding UTF8
    } catch {
        Log "could not write install state: $($_.Exception.Message)" 'WARN'
    }
}

function Get-JsonLeaf {
    param($Obj, [string]$Name)
    if ($null -eq $Obj) { return $null }
    try { return $Obj.$Name } catch { return $null }
}

function Resolve-PythonTool {
    $candidates = @()
    foreach ($cmdName in @('py', 'python')) {
        try {
            $cmd = Get-Command $cmdName -ErrorAction SilentlyContinue
            if ($cmd -and $cmd.Source) { $candidates += $cmd.Source }
        } catch { }
    }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($base -and (Test-Path $base)) {
            $candidates += (Get-ChildItem -Path (Join-Path $base 'Python*') -Filter 'python.exe' -Recurse -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
        }
    }
    $local = Join-Path $env:LOCALAPPDATA 'Programs\Python'
    if (Test-Path $local) {
        $candidates += (Get-ChildItem -Path (Join-Path $local 'Python*') -Filter 'python.exe' -Recurse -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    }
    $unique = @()
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c) -and ($unique -notcontains $c)) { $unique += $c }
    }
    foreach ($py in $unique) {
        try {
            $ver = & $py --version 2>&1
            if ($LASTEXITCODE -eq 0 -and "$ver" -match 'Python\s+3\.(\d+)') {
                return [pscustomobject]@{ Exe = $py; Version = "$ver"; MajorMinor = [int]$Matches[1] }
            }
        } catch { }
    }
    return $null
}

function Install-Python {
    param([string]$Channel = 'LatestStable')
    $packageId = if ($Channel -eq 'Latest') { 'Python.Python.3.12' } else { 'Python.Python.3.12' }
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Log "installing tested Python 3.12 via winget: $packageId" 'INFO'
        & winget install --id $packageId --scope machine --silent --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) { return [pscustomobject]@{ Method='winget'; PackageId=$packageId } }
        Log 'winget Python install failed; falling back to official installer' 'WARN'
    }
    $ver = '3.12.9'
    $url = "https://www.python.org/ftp/python/$ver/python-$ver-amd64.exe"
    $tmp = Join-Path $env:TEMP "python-$ver-amd64.exe"
    try {
        Enable-Tls12
        Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
    } catch {
        if (-not (Get-Command 'curl.exe' -ErrorAction SilentlyContinue)) { throw }
        & curl.exe -fL $url -o $tmp
        if ($LASTEXITCODE -ne 0) { throw }
    }
    Start-Process -FilePath $tmp -Wait -ArgumentList @('/quiet','InstallAllUsers=1','PrependPath=1','Include_launcher=1')
    return [pscustomobject]@{ Method='installer'; PackageId=$null; UninstallString=$null }
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
    if (-not $InstallPythonIfMissing) { $argList += '-InstallPythonIfMissing:$false' }
    if ($UpgradePython) { $argList += '-UpgradePython:$true' }
    $argList += @('-PythonChannel', $PythonChannel)
    if ($SkipPythonInstall) { $argList += '-SkipPythonInstall' }
    if ($NonInteractive) { $argList += '-NonInteractive' }
    if ($FlowUpdate) { $argList += '-FlowUpdate' }
    $shell = if (Get-Command 'pwsh' -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell' }
    try {
        Start-Process -FilePath $shell -Verb RunAs -ArgumentList $argList
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
function Enable-Tls12 {
    try {
        $tls12 = [Net.SecurityProtocolType]::Tls12
        if (([Net.ServicePointManager]::SecurityProtocol -band $tls12) -eq 0) {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor $tls12
        }
    } catch {}
}
function Get-WebView2Runtime {
    $applicationDirs = @()
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramW6432, $env:ProgramFiles)) {
        if ($base) { $applicationDirs += (Join-Path $base 'Microsoft\EdgeWebView\Application') }
    }
    if ($env:LOCALAPPDATA) {
        $applicationDirs += (Join-Path $env:LOCALAPPDATA 'Microsoft\EdgeWebView\Application')
    }

    foreach ($applicationDir in ($applicationDirs | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $applicationDir)) { continue }
        $executables = @(Get-ChildItem -LiteralPath $applicationDir -Filter 'msedgewebview2.exe' -File -Recurse -ErrorAction SilentlyContinue)
        foreach ($exe in ($executables | Sort-Object FullName -Descending)) {
            $version = $null
            try { $version = $exe.VersionInfo.ProductVersion } catch { }
            if (-not $version) { $version = $exe.Directory.Name }
            return [pscustomobject]@{ Path = $exe.FullName; Version = [string]$version }
        }
    }
    return $null
}
function Ensure-WebView2Runtime {
    $runtime = Get-WebView2Runtime
    if ($runtime) {
        Log "WebView2 Evergreen detected: $($runtime.Path) [$($runtime.Version)]" 'OK'
        return $runtime
    }

    $bootstrapperUrl = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
    $bootstrapper = Join-Path $env:TEMP "MicrosoftEdgeWebview2Setup-$PID.exe"
    Log 'WebView2 Evergreen is missing; downloading the official Microsoft bootstrapper' 'INFO'
    try {
        Enable-Tls12
        try {
            Invoke-WebRequest -Uri $bootstrapperUrl -OutFile $bootstrapper -UseBasicParsing
        } catch {
            if (-not (Get-Command 'curl.exe' -ErrorAction SilentlyContinue)) { throw }
            & curl.exe -fL $bootstrapperUrl -o $bootstrapper
            if ($LASTEXITCODE -ne 0) { throw "curl.exe exited with code $LASTEXITCODE" }
        }
        if (-not (Test-Path -LiteralPath $bootstrapper)) { throw 'bootstrapper download did not create a file' }
        $process = Start-Process -FilePath $bootstrapper -ArgumentList @('/silent', '/install') -Wait -PassThru
        if ($process.ExitCode -notin @(0, 1641, 3010)) {
            throw "WebView2 bootstrapper exited with code $($process.ExitCode)"
        }
        Log "WebView2 bootstrapper completed with exit code $($process.ExitCode)" 'OK'
    } finally {
        Remove-Item -LiteralPath $bootstrapper -Force -ErrorAction SilentlyContinue
    }

    $runtime = $null
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        $runtime = Get-WebView2Runtime
        if ($runtime) { break }
        Start-Sleep -Seconds 1
    }
    if (-not $runtime) { throw 'WebView2 Evergreen is still not detectable after bootstrapper installation' }
    Log "WebView2 Evergreen detected after install: $($runtime.Path) [$($runtime.Version)]" 'OK'
    return $runtime
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
    if (-not $NonInteractive) { Read-Host 'Press Enter to close' }
    exit 1
}

try {
    $ProductVersion = Get-ProductVersion -Path $VersionPath
    Log "product version: $ProductVersion" 'OK'
} catch {
    Fail $_.Exception.Message
}

function Invoke-FlowShiftShutdown {
    try {
        $sock = New-Object System.Net.Sockets.TcpClient
        $ar = $sock.BeginConnect('127.0.0.1', 45782, $null, $null)
        if ($ar.AsyncWaitHandle.WaitOne(400)) {
            $sock.EndConnect($ar)
            $stream = $sock.GetStream()
            $payload = [Text.Encoding]::UTF8.GetBytes((ConvertTo-Json @{ type = 'shutdown'; reason = 'installer' } -Compress))
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

function Get-ProcessCommandLine {
    param([int]$ProcessId)
    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
        return [string]($proc.CommandLine)
    } catch {
        return ''
    }
}

function Get-ProcessExecutablePath {
    param([int]$ProcessId)
    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
        return [string]($proc.ExecutablePath)
    } catch {
        return ''
    }
}

function Test-FlowShiftProcess {
    param([int]$ProcessId, [string]$RootDir)
    $root = ([string]$RootDir).TrimEnd('\')
    $cmd = (Get-ProcessCommandLine -ProcessId $ProcessId)
    $exe = (Get-ProcessExecutablePath -ProcessId $ProcessId)
    foreach ($candidate in @($cmd, $exe)) {
        if ($candidate) {
            $norm = [string]$candidate
            if ($norm -match [regex]::Escape($root)) { return $true }
        }
    }
    return $false
}

function Test-FlowShiftCommandLine {
    param([string]$CmdLine)
    return ([string]$CmdLine -match 'FlowShift|tray\.py|service\.py')
}

# --- Ask FlowShift to stop cleanly before touching files --------------------
Write-Host ''
Write-Host 'Checking for lingering FlowShift processes...' -ForegroundColor Cyan
if (Invoke-FlowShiftShutdown) {
    Log 'requested clean shutdown via control socket' 'OK'
} else {
    Log 'control socket not reachable; continuing with process check' 'WARN'
}
if (Wait-FlowShiftPortsClosed) {
    Log 'control/peer ports are free' 'OK'
} else {
    Log 'ports still in use after shutdown request; checking owning processes' 'WARN'
    $pids = @()
    foreach ($port in @(45782, 45781)) {
        try {
            $pids += Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique
        } catch { }
    }
    $pids = $pids | Where-Object { $_ } | Sort-Object -Unique
    $matches = @()
    foreach ($processId in $pids) {
        $cmd = Get-ProcessCommandLine -ProcessId ([int]$processId)
        $exe = Get-ProcessExecutablePath -ProcessId ([int]$processId)
        if (Test-FlowShiftProcess -ProcessId ([int]$processId) -RootDir $InstallDir) {
            $matches += [pscustomobject]@{ Pid = [int]$processId; CmdLine = $cmd; Exe = $exe }
        } else {
            Log ("leaving PID {0} alone (not under {1}): {2} {3}" -f $processId, $InstallDir, $exe, $cmd) 'WARN'
        }
    }
    if ($matches.Count -gt 0) {
        Write-Host 'The following FlowShift processes are still running:' -ForegroundColor Yellow
        $matches | ForEach-Object { Write-Host ("  PID {0}: {1}" -f $_.Pid, $_.CmdLine) -ForegroundColor Yellow }
        if ($NonInteractive) {
            Fail 'FlowShift processes are still running; refusing to stop them without confirmation'
        }
        $ans = Read-Host 'Stop these FlowShift processes now? [y/N]'
        if ($ans -match '^[yY]') {
            foreach ($m in $matches) {
                try {
                    Stop-Process -Id $m.Pid -ErrorAction SilentlyContinue
                    Log "stopped PID $($m.Pid) after confirmation" 'OK'
                } catch {
                    Log "could not stop PID $($m.Pid): $($_.Exception.Message)" 'WARN'
                }
            }
        } else {
            Log 'user chose not to stop lingering FlowShift processes' 'WARN'
        }
    }
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

    $installState = Read-InstallState

    # --- 2. Python ----------------------------------------------------------
    Step 'Checking Python'
    $pyInfo = Resolve-PythonTool
    if ($pyInfo) {
        Log "found Python: $($pyInfo.Exe) [$($pyInfo.Version)]" 'OK'
        $installState.detected_before_install.python = $pyInfo.Exe
    } else {
        Log 'Python not found' 'WARN'
        $installState.detected_before_install.python = $null
    }

    # --- 3. Install Python if missing --------------------------------------
    Step 'Installing Python (if missing)'
    if ($pyInfo -and -not $UpgradePython) {
        Log 'Python already present, using existing Python' 'OK'
        $installState.details.python.install_method = 'existing'
    } elseif (-not $pyInfo) {
        if ($SkipPythonInstall) { Fail 'Python is required but installation is disabled by -SkipPythonInstall' }
        if (-not $InstallPythonIfMissing) { Fail 'Python is required but automatic installation is disabled' }
        try {
            $pyInstall = Install-Python -Channel $PythonChannel
            $installState.installed_by_flowshift.python = $true
            $installState.details.python.installed_by_flowshift = $true
            $installState.details.python.install_method = $pyInstall.Method
            $installState.details.python.package_id = $pyInstall.PackageId
            Log 'Python installation finished' 'OK'
        } catch {
            Fail "could not install Python automatically: $($_.Exception.Message). Install tested Python 3.12 or enable winget and re-run."
        }
        $pyInfo = Resolve-PythonTool
        if (-not $pyInfo) { Fail 'Python still not found after install. Re-run after a reboot.' }
    } elseif ($UpgradePython) {
        try {
            $pyInstall = Install-Python -Channel $PythonChannel
            $installState.installed_by_flowshift.python = $true
            $installState.details.python.installed_by_flowshift = $true
            $installState.details.python.install_method = $pyInstall.Method
            $installState.details.python.package_id = $pyInstall.PackageId
            $pyInfo = Resolve-PythonTool
            if (-not $pyInfo) { Fail 'Python still not found after upgrade' }
            Log 'Python upgrade finished' 'OK'
        } catch {
            Fail "could not upgrade Python automatically: $($_.Exception.Message)"
        }
    }

    $installState.used_tools.python = $pyInfo.Exe
    $installState.versions.python = $pyInfo.Version
    Write-InstallState $installState

    # --- 4. Copy app files + create venv -----------------------------------
    Step 'Installing application files and creating venv'
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Log "copying source tree to $InstallDir"
    Copy-Item -Path (Join-Path $RepoDir 'src') -Destination $InstallDir -Recurse -Force
    Copy-Item -LiteralPath $VersionPath -Destination (Join-Path $InstallDir 'VERSION') -Force
    if (Test-Path (Join-Path $RepoDir 'docs')) {
        Copy-Item -Path (Join-Path $RepoDir 'docs') -Destination $InstallDir -Recurse -Force
    }
    foreach ($f in @(
        'requirements.txt',
        'README.md',
        'LICENSE',
        'uninstall_flowshift.bat',
        'uninstall_flowshift.ps1',
        'update_flowshift.ps1'
    )) {
        $src = Join-Path $RepoDir $f
        if (Test-Path $src) { Copy-Item -Path $src -Destination $InstallDir -Force }
    }
    foreach ($overlayModule in @('overlay_host.py','overlay_controller.py','overlay_protocol.py','overlay_geometry.py')) {
        $installedModule = Join-Path $PyDir $overlayModule
        if (-not (Test-Path -LiteralPath $installedModule -PathType Leaf)) {
            Fail "installed overlay module missing after source copy: $installedModule"
        }
        Log "verified installed overlay module: $installedModule" 'OK'
    }
    foreach ($junk in @('config.json','flowshift.log','flowshift_runtime.out')) {
        $p = Join-Path $PyDir $junk
        if (Test-Path $p) { Remove-Item -Path $p -Force }
    }
    Get-ChildItem -Path $InstallDir -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    if (Test-Path $VenvDir) {
        Log "removing existing venv at $VenvDir" 'INFO'
        Remove-Item -Path $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    Log "creating venv at $VenvDir"
    & $pyInfo.Exe -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Fail 'venv creation failed' }
    $VenvPy  = Join-Path $VenvDir 'Scripts\python.exe'
    $VenvPyw = Join-Path $VenvDir 'Scripts\pythonw.exe'
    if (-not (Test-Path $VenvPy)) { Fail "venv python not found at $VenvPy" }
    if (-not (Test-Path $VenvPyw)) { Fail "venv pythonw not found at $VenvPyw" }
    Log 'venv created' 'OK'

    # --- 5. Dependencies ----------------------------------------------------
    Step 'Installing Python dependencies'
    $pipOut = & $VenvPy -m pip install --upgrade pip 2>&1
    if ($LASTEXITCODE -ne 0) { Log "pip upgrade issue (non-fatal): $pipOut" 'WARN' }
    $req = Join-Path $InstallDir 'requirements.txt'
    if (Test-Path $req) {
        $pipOut = & $VenvPy -m pip install -r $req 2>&1
        if ($LASTEXITCODE -eq 0) {
            Log 'dependencies installed from requirements.txt' 'OK'
        } else {
            Fail "pip install failed: $pipOut"
        }
    } else {
        Log 'no requirements.txt; skipping dependency install' 'OK'
    }

    try {
        $null = Ensure-WebView2Runtime
        Log 'WebView2 is a shared system dependency and will not be removed by FlowShift uninstallers' 'INFO'
    } catch {
        Fail "WebView2 Evergreen installation or verification failed: $($_.Exception.Message)"
    }

    $previousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $PyDir
        $smokeOut = & $VenvPy -c 'import webview; import overlay_host; import overlay_controller' 2>&1
        if ($LASTEXITCODE -ne 0) { Fail "installed overlay import smoke test failed: $smokeOut" }
        Log 'installed venv import smoke passed: webview, overlay_host, overlay_controller' 'OK'
    } finally {
        if ($null -eq $previousPythonPath) {
            Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONPATH = $previousPythonPath
        }
    }

    $installState.used_tools.python = $VenvPy
    $installState.versions.python = (& $VenvPy --version 2>&1)
    Write-InstallState $installState

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
            config_schema_version = 1
            device_name = $env:COMPUTERNAME
            device_id   = $devId
            port        = 45781
            peers       = @()
            hotkeys     = @()
            updates     = [ordered]@{ enabled = $true; check_on_start = $true; channel = 'stable'; policy = 'notify' }
            mouse       = [ordered]@{ flush_interval_ms = 6; max_batch_ms = 12; sensitivity = 1.0; accumulate_subpixel = $true }
            display_layout = [ordered]@{
                enabled = $false
                threshold_px = 3
                inset_px = 24
                cooldown_ms = 600
                return_cooldown_ms = 400
                edges = [ordered]@{ north = $null; south = $null; east = $null; west = $null }
            }
        }
        $utf8NoBom = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json -Depth 6), $utf8NoBom)
        Log "created fresh config.json (device_id=$devId)" 'OK'
    } else {
        Log 'config.json already exists, keeping it' 'OK'
    }
    # Machine-wide env via registry (faster than SetEnvironmentVariable which
    # broadcasts WM_SETTINGCHANGE to ALL windows and can hang on some systems).
    $envReg = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
    Set-ItemProperty -Path $envReg -Name 'FLOWSHIFT_CONFIG' -Value $cfgPath -Force
    Set-ItemProperty -Path $envReg -Name 'FLOWSHIFT_LOG_DIR' -Value $LogDir -Force
    Set-ItemProperty -Path $envReg -Name 'FLOWSHIFT_WEBGUI_DIR' -Value (Join-Path $InstallDir 'webgui') -Force
    $env:FLOWSHIFT_CONFIG = $cfgPath
    $env:FLOWSHIFT_LOG_DIR = $LogDir
    $env:FLOWSHIFT_WEBGUI_DIR = (Join-Path $InstallDir 'webgui')
    Log 'machine env FLOWSHIFT_CONFIG / FLOWSHIFT_LOG_DIR / FLOWSHIFT_WEBGUI_DIR set' 'OK'

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

    # Register the core uninstall entry in Apps & Features.
    $coreUninstallKey = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FlowShift'
    try {
        if (-not (Test-Path $coreUninstallKey)) { New-Item -Path $coreUninstallKey -Force | Out-Null }
        Set-ItemProperty -Path $coreUninstallKey -Name 'DisplayName' -Value 'FlowShift'
        Set-ItemProperty -Path $coreUninstallKey -Name 'DisplayVersion' -Value $ProductVersion
        Set-ItemProperty -Path $coreUninstallKey -Name 'Publisher' -Value 'FlowShift'
        Set-ItemProperty -Path $coreUninstallKey -Name 'InstallLocation' -Value $InstallDir
        Set-ItemProperty -Path $coreUninstallKey -Name 'UninstallString' -Value "`"$InstallDir\uninstall_flowshift.bat`""
        Set-ItemProperty -Path $coreUninstallKey -Name 'NoModify' -Value 1
        Set-ItemProperty -Path $coreUninstallKey -Name 'NoRepair' -Value 1
        Log 'core uninstaller registered (Apps & Features)' 'OK'
    } catch {
        Log "could not register core uninstaller: $($_.Exception.Message)" 'WARN'
    }

    $installState.details.python.install_method = if ($installState.details.python.install_method) { $installState.details.python.install_method } else { 'existing' }
    Write-InstallState $installState

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
                    Enable-Tls12
                    try {
                        Invoke-WebRequest -Uri 'https://nssm.cc/release/nssm-2.24.zip' -OutFile $zip -UseBasicParsing
                    } catch {
                        if (-not (Get-Command 'curl.exe' -ErrorAction SilentlyContinue)) { throw }
                        & curl.exe -fL 'https://nssm.cc/release/nssm-2.24.zip' -o $zip
                        if ($LASTEXITCODE -ne 0) { throw }
                    }
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
    function New-Shortcut($path, $target, $scArgs, $workdir, $icon, $desc) {
        $sc = $wsh.CreateShortcut($path)
        $sc.TargetPath = $target
        $sc.Arguments = $scArgs
        $sc.WorkingDirectory = $workdir
        if ($icon -and (Test-Path $icon)) { $sc.IconLocation = $icon }
        $sc.Description = $desc
        $sc.Save()
    }
    $desktop  = Join-Path $env:PUBLIC 'Desktop\FlowShift.lnk'
    $startDir = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\FlowShift'
    New-Item -ItemType Directory -Force -Path $startDir | Out-Null
    $trayArgs = "`"$trayPy`" --tray"
    $guiArgs = "`"$guiPy`""
    New-Shortcut $desktop $VenvPyw $trayArgs $PyDir $iconPy 'Start FlowShift tray'
    New-Shortcut (Join-Path $startDir 'FlowShift GUI.lnk') $VenvPyw $guiArgs $PyDir $iconPy 'Open FlowShift settings'
    New-Shortcut (Join-Path $startDir 'FlowShift Logs.lnk') 'explorer.exe' "`"$LogDir`"" $LogDir $null 'Open FlowShift log folder'
    $uninBat = Join-Path $InstallDir 'uninstall_flowshift.bat'
    if (Test-Path $uninBat) { New-Shortcut (Join-Path $startDir 'Uninstall FlowShift.lnk') $uninBat '' $InstallDir $null 'Uninstall FlowShift' }
    Log 'shortcuts created (Desktop + Start Menu)' 'OK'

    # --- 10. Firewall rules --------------------------------------------------
    Step 'Adding Windows Firewall rules (port 45781)'
    $fwOk = $true
    try {
        Get-NetFirewallRule -DisplayName 'FlowShift*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
        $null = New-NetFirewallRule -DisplayName 'FlowShift (TCP)' -Direction Inbound -Protocol TCP -LocalPort 45781 -Action Allow -Profile Any -ErrorAction Stop
        $null = New-NetFirewallRule -DisplayName 'FlowShift (UDP)' -Direction Inbound -Protocol UDP -LocalPort 45781 -Action Allow -Profile Any -ErrorAction Stop
        Log 'firewall rules added for TCP+UDP 45781' 'OK'
    } catch {
        Log "could not add firewall rules: $($_.Exception.Message). Add them manually (Control Panel\Windows Defender Firewall)." 'WARN'
        $fwOk = $false
    }
    if ($fwOk) { Log 'FlowShift ports are accessible on the LAN' 'OK' }

    # --- 11. Start the runtime now (in the interactive session) -------------
    Step 'Starting the FlowShift runtime (interactive session)'
    if ($FlowUpdate) {
        Log 'FlowUpdate mode: runtime start is deferred to the external update runner' 'OK'
    } else {
        try {
            Start-ScheduledTask -TaskName $TaskName
            Log 'scheduled task started (runs pythonw tray.py --tray in the user session)' 'OK'
        } catch {
            Log "could not start the scheduled task now: $($_.Exception.Message). It will start at next logon." 'WARN'
        }
        Start-Sleep -Seconds 2
    }

    # --- 12. Verify control socket + session --------------------------------
    Step 'Verifying the control socket (127.0.0.1:45782)'
    if ($FlowUpdate) {
        Log 'FlowUpdate mode: runtime health verification is deferred to the external update runner' 'OK'
    } else {
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
    }

    # --- 13. Done -----------------------------------------------------------
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
if (-not $NonInteractive) { Read-Host 'Press Enter to close' }
exit 0
