# FlowShift Web GUI installer
#
# Installs the React web frontend into the existing FlowShift installation
# directory (Program Files\FlowShift\webgui\). Separates the heavy
# node_modules download from the core FlowShift installer.
#
# Run via install_webgui.bat (double-click). Self-elevates through UAC.

param(
    [switch]$Elevated,
    [switch]$UsePrebuilt,
    [bool]$InstallNodeIfMissing = $true,
    [bool]$UpgradeNode = $false,
    [ValidateSet('LTS','Latest')][string]$NodeChannel = 'LTS',
    [switch]$SkipNodeInstall
)

$ErrorActionPreference = 'Stop'
$RepoDir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir  = Join-Path $env:ProgramFiles 'FlowShift'
$WebTarget   = Join-Path $InstallDir 'webgui'
$WebSource   = Join-Path $RepoDir 'webgui'
$DistSource  = Join-Path $WebSource 'dist'
$LogDir      = Join-Path $env:ProgramData 'FlowShift\logs'
$InstallLog  = Join-Path $LogDir 'install_webgui.log'
$InstallStatePath = Join-Path $env:ProgramData 'FlowShift\install_state.json'
$TotalSteps  = 9
$MinNodeMajor = 18

# ---- Helpers ----------------------------------------------------------------
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
        Add-Content -Path $InstallLog -Value $line -ErrorAction SilentlyContinue
    } catch {}
    $color = @{INFO='White'; OK='Green'; WARN='Yellow'; ERR='Red'}[$Level]
    if (-not $color) { $color = 'White' }
    Write-Host "  $Msg" -ForegroundColor $color
}

function New-DefaultInstallState {
    return [ordered]@{
        installed_by_flowshift = [ordered]@{ python = $false; nodejs = $false; vite = $false }
        detected_before_install = [ordered]@{ python = $null; node = $null; npm = $null }
        used_tools = [ordered]@{ python = $null; node = $null; npm = $null; npx = $null; vite = $null }
        versions = [ordered]@{ python = $null; node = $null; npm = $null; npx = $null; vite = $null }
        details = [ordered]@{
            python = [ordered]@{ installed_by_flowshift = $false; install_method = $null; package_id = $null }
            nodejs = [ordered]@{ installed_by_flowshift = $false; install_method = $null; package_id = $null; uninstall_string = $null }
            vite = [ordered]@{ installed_by_flowshift = $false; install_method = 'npm ci'; package_id = 'webgui/package.json' ; scope = 'project-local'; source_path = $null; node_modules = $null }
        }
    }
}

function Read-InstallState {
    if (Test-Path $InstallStatePath) {
        try { return (Get-Content -LiteralPath $InstallStatePath -Raw -ErrorAction Stop | ConvertFrom-Json) } catch { }
    }
    return (New-DefaultInstallState | ConvertTo-Json -Depth 8 | ConvertFrom-Json)
}

function Write-InstallState {
    param($State)
    try {
        $dir = Split-Path -Parent $InstallStatePath
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        ($State | ConvertTo-Json -Depth 12) | Set-Content -LiteralPath $InstallStatePath -Encoding UTF8
    } catch {
        Log "could not write install state: $($_.Exception.Message)" 'WARN'
    }
}

function Resolve-NodeToolsOrNull {
    $node = $null
    $npm = $null
    $npx = $null

    foreach ($cand in @(
        (Join-Path $env:ProgramFiles 'nodejs'),
        (Join-Path ${env:ProgramFiles(x86)} 'nodejs')
    )) {
        if ($cand -and (Test-Path (Join-Path $cand 'node.exe'))) {
            $node = Join-Path $cand 'node.exe'
            $npm = Join-Path $cand 'npm.cmd'
            $npx = Join-Path $cand 'npx.cmd'
            break
        }
    }

    if (-not $node) {
        $cmd = Get-Command node -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) {
            $node = $cmd.Source
            $nodeDir = Split-Path -Parent $node
            $npm = Join-Path $nodeDir 'npm.cmd'
            $npx = Join-Path $nodeDir 'npx.cmd'
        }
    }

    if (-not $npm) {
        $cmd = Get-Command npm -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) { $npm = $cmd.Source }
    }
    if (-not $npx) {
        $cmd = Get-Command npx -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) { $npx = $cmd.Source }
    }

    if ($npm -and ([IO.Path]::GetExtension($npm) -ieq '.ps1')) {
        $cmd = [IO.Path]::ChangeExtension($npm, '.cmd')
        if (Test-Path $cmd) { $npm = $cmd }
    }
    if ($npx -and ([IO.Path]::GetExtension($npx) -ieq '.ps1')) {
        $cmd = [IO.Path]::ChangeExtension($npx, '.cmd')
        if (Test-Path $cmd) { $npx = $cmd }
    }

    if (-not $node -or -not (Test-Path $node) -or -not $npm -or -not (Test-Path $npm)) {
        return $null
    }

    if (-not $npx -or -not (Test-Path $npx)) { $npx = $npm }

    try { $nodeVersion = (& $node --version 2>&1).Trim() } catch { $nodeVersion = $null }
    try { $npmVersion = (& $npm --version 2>&1).Trim() } catch { $npmVersion = $null }

    return [pscustomobject]@{
        Node = $node
        Npm = $npm
        Npx = $npx
        NodeVersion = $nodeVersion
        NpmVersion = $npmVersion
    }
}

function Require-NodeTools {
    $tools = Resolve-NodeToolsOrNull
    if (-not $tools) { throw 'Node.js node.exe/npm.cmd not found' }
    return $tools
}

function Install-Node {
    param([string]$Channel = 'LTS')
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        $pkg = if ($Channel -eq 'Latest') { 'OpenJS.NodeJS' } else { 'OpenJS.NodeJS.LTS' }
        Log "installing Node.js $Channel via winget: $pkg" 'INFO'
        & winget install --id $pkg --scope machine --silent --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) { return [pscustomobject]@{ Method='winget'; PackageId=$pkg; UninstallString=$null } }
        Log 'winget Node install failed; falling back to MSI download' 'WARN'
    }
    Enable-Tls12
    $arch = switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()) {
        'Arm64' { 'arm64' }
        'X64'   { 'x64' }
        default { if ([Environment]::Is64BitOperatingSystem) { 'x64' } else { 'x86' } }
    }
    $versionData = Invoke-RestMethod -Uri 'https://nodejs.org/dist/index.json' -TimeoutSec 10
    $release = if ($Channel -eq 'Latest') {
        $versionData | Where-Object { $_.files -contains "win-$arch-msi" } | Select-Object -First 1
    } else {
        $versionData | Where-Object { $_.lts -and $_.files -contains "win-$arch-msi" } | Select-Object -First 1
    }
    if (-not $release) { throw 'No suitable Node.js release found.' }
    $latest = $release.version
    $msiUrl = "https://nodejs.org/dist/$latest/node-$latest-$arch.msi"
    $msiPath = Join-Path $env:TEMP 'node-install.msi'
    Invoke-WebRequest -Uri $msiUrl -OutFile $msiPath -UseBasicParsing
    $msi = Start-Process -Wait -PassThru -FilePath msiexec -ArgumentList "/i `"$msiPath`" /qn /norestart"
    if ($msi.ExitCode -notin @(0, 1641, 3010)) { throw "msiexec exited with code $($msi.ExitCode)" }
    Remove-Item $msiPath -Force -ErrorAction SilentlyContinue
    return [pscustomobject]@{ Method='msi'; PackageId=$null; UninstallString=$null }
}

function Resolve-ViteCmd {
    $vite = Join-Path $WebSource 'node_modules\.bin\vite.cmd'
    if (Test-Path $vite) { return $vite }
    return $null
}

function Step {
    param([int]$Num, [string]$Desc)
    Write-Host "`n[$Num/$TotalSteps] $Desc" -ForegroundColor Cyan
}

function Enable-Tls12 {
    try {
        $tls12 = [Net.SecurityProtocolType]::Tls12
        if (([Net.ServicePointManager]::SecurityProtocol -band $tls12) -eq 0) {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor $tls12
        }
    } catch {}
}

function New-Shortcut {
    param([string]$Path, [string]$Target, [string]$Args, [string]$WorkDir, [string]$Icon, [string]$Desc)
    try {
        $wshell = New-Object -ComObject WScript.Shell
        $s      = $wshell.CreateShortcut($Path)
        $s.TargetPath = $Target
        if ($Args) { $s.Arguments = $Args }
        if ($WorkDir) { $s.WorkingDirectory = $WorkDir }
        if ($Icon) { $s.IconLocation = $Icon }
        if ($Desc) { $s.Description = $Desc }
        $s.Save()
    } catch { Log "could not create shortcut $Path : $_" 'WARN' }
}


function Test-WebGuiHttp {
    param([int]$Port = 5000)
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 5
        $body = [string]$resp.Content
        return [ordered]@{
            Ok = ($resp.StatusCode -eq 200 -and ($body -match '<html|id="root"|vite'))
            StatusCode = $resp.StatusCode
            Body = $body
        }
    } catch {
        return [ordered]@{ Ok = $false; StatusCode = 0; Body = $_.Exception.Message }
    }
}

function Test-WebGuiStatus {
    param([int]$Port = 5000)
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/status" -UseBasicParsing -TimeoutSec 5
        $body = [string]$resp.Content
        $json = $null
        try { $json = $body | ConvertFrom-Json } catch { }
        return [ordered]@{
            Ok = ($resp.StatusCode -eq 200 -and $null -ne $json)
            StatusCode = $resp.StatusCode
            Body = $body
            Json = $json
        }
    } catch {
        return [ordered]@{ Ok = $false; StatusCode = 0; Body = $_.Exception.Message; Json = $null }
    }
}

function Fail-WebGuiInstall {
    param([string]$Reason)
    Write-Host "`nFlowShift Web GUI installation incomplete" -ForegroundColor Red
    Write-Host "Reason: $Reason" -ForegroundColor Red
    Write-Host 'Exit code 1' -ForegroundColor Red
    pause
    exit 1
}

function Restart-FlowShiftRuntimeIfNeeded {
    param([string]$Reason = 'webgui-install')
    try {
        $sock = New-Object System.Net.Sockets.TcpClient
        $ar = $sock.BeginConnect('127.0.0.1', 45782, $null, $null)
        if ($ar.AsyncWaitHandle.WaitOne(500)) {
            $sock.EndConnect($ar)
            $stream = $sock.GetStream()
            $writer = New-Object System.IO.BinaryWriter($stream)
            $reader = New-Object System.IO.BinaryReader($stream)
            $payload = [Text.Encoding]::UTF8.GetBytes((ConvertTo-Json @{ type = 'shutdown'; reason = $Reason } -Compress))
            $writer.Write([System.Net.IPAddress]::HostToNetworkOrder([int]$payload.Length))
            $writer.Write($payload)
            $writer.Flush()
            $stream.Close(); $sock.Close()
        }
    } catch { }
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        try {
            if (-not (Get-NetTCPConnection -LocalPort 45782 -ErrorAction SilentlyContinue)) { break }
        } catch { break }
        Start-Sleep -Milliseconds 250
    }
    try {
        Start-ScheduledTask -TaskName 'FlowShift' | Out-Null
    } catch { }
}

# ---- Self-elevate -----------------------------------------------------------
if (-not (Test-Admin)) {
    Write-Host ''
    Write-Host 'FlowShift Web GUI installer needs administrator rights' -ForegroundColor Yellow
    Write-Host '(install to Program Files, register uninstaller, etc.).' -ForegroundColor Yellow
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
Write-Host '   FlowShift Web GUI Installer              ' -ForegroundColor Cyan
Write-Host '============================================' -ForegroundColor Cyan
Write-Host ''

# ---- 1. Check / Install Node.js ---------------------------------------------
Step 1 'Checking Node.js'
$installState = Read-InstallState
$nodeTools = Resolve-NodeToolsOrNull
if ($nodeTools) {
    Log "Node.js detected: $($nodeTools.Node) [$($nodeTools.NodeVersion)]" 'OK'
    $installState.detected_before_install.node = $nodeTools.Node
    $installState.detected_before_install.npm = $nodeTools.Npm
} else {
    Log 'Node.js not found' 'WARN'
    $installState.detected_before_install.node = $null
    $installState.detected_before_install.npm = $null
}

if (-not $nodeTools -or $UpgradeNode) {
    if ($SkipNodeInstall) { throw 'Node.js is required but installation is disabled by -SkipNodeInstall' }
    if (-not $InstallNodeIfMissing -and -not $UpgradeNode) { throw 'Node.js is required but installation is disabled' }
    try {
        $nodeInstall = Install-Node -Channel $NodeChannel
        $installState.installed_by_flowshift.nodejs = $true
        $installState.details.nodejs.installed_by_flowshift = $true
        $installState.details.nodejs.install_method = $nodeInstall.Method
        $installState.details.nodejs.package_id = $nodeInstall.PackageId
        $installState.details.nodejs.uninstall_string = $nodeInstall.UninstallString
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ";" + [Environment]::GetEnvironmentVariable('Path', 'User')
        $nodeTools = Require-NodeTools
        Log 'Node.js installation finished' 'OK'
    } catch {
        Log "Failed to install Node.js: $_" 'ERR'
        Write-Host "`n  Please install Node.js manually from https://nodejs.org and re-run.`n" -ForegroundColor Yellow
        pause
        exit 1
    }
}
else {
    $installState.details.nodejs.install_method = 'existing'
}

Log "node: $($nodeTools.Node)" 'OK'
Log "npm : $($nodeTools.Npm)" 'OK'
Log "npx : $($nodeTools.Npx)" 'OK'

# ---- 2. Check FlowShift installation ----------------------------------------
Step 2 "Checking FlowShift installation ($InstallDir)"
if (Test-Path $InstallDir) {
    Log "FlowShift found at $InstallDir" 'OK'
} else {
    Log "FlowShift not installed at $InstallDir." 'WARN'
    Log 'Install FlowShift first (run install_flowshift.bat), then re-run this installer.' 'WARN'
    $yn = Read-Host 'Continue anyway (create target directory)? [y/N]'
    if ($yn -ne 'y' -and $yn -ne 'Y') {
        Log 'Aborted by user' 'ERR'
        exit 1
    }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Log "Created $InstallDir" 'OK'
}

# ---- 3. Check webgui source -------------------------------------------------
Step 3 'Checking web GUI source'
if (-not (Test-Path $WebSource)) {
    Log "Web GUI source not found at $WebSource" 'ERR'
    Log 'Run this installer from the FlowShift repository root (where webgui/ lives).' 'ERR'
    pause
    exit 1
}
if (-not (Test-Path (Join-Path $WebSource 'package.json'))) {
    Log "package.json not found in $WebSource" 'ERR'
    pause
    exit 1
}
Log "Web GUI source found at $WebSource" 'OK'

# ---- 4. npm ci (refresh dependencies) ---------------------------------------
Step 4 'Installing npm dependencies'
$nodeModules = Join-Path $WebSource 'node_modules'
if (Test-Path $nodeModules) {
    Remove-Item -Recurse -Force $nodeModules
    Log 'removed existing node_modules to force a clean install' 'INFO'
}
Push-Location $WebSource
try {
    $oldNodeEnv = $env:NODE_ENV
    Remove-Item Env:NODE_ENV -ErrorAction SilentlyContinue
    $output = & $nodeTools.Npm ci --include=dev --no-audit --no-fund 2>&1
    if ($null -ne $oldNodeEnv) { $env:NODE_ENV = $oldNodeEnv }
    if ($LASTEXITCODE -ne 0) {
        Log "npm ci failed (exit $LASTEXITCODE)" 'ERR'
        foreach ($line in $output) { Log $line 'ERR' }
        throw "npm ci failed"
    }
    Log 'npm ci completed' 'OK'
} catch {
    if ($null -ne $oldNodeEnv) { $env:NODE_ENV = $oldNodeEnv } else { Remove-Item Env:NODE_ENV -ErrorAction SilentlyContinue }
    if ($_.Exception.Message -ne 'npm ci failed') { Log "npm ci: $_" 'ERR' }
    pause
    exit 1
} finally { Pop-Location }

# ---- 5. Build ---------------------------------------------------------------
Step 5 'Building the web GUI'
$distDir  = $DistSource
$distIndex = Join-Path $distDir 'index.html'
if (-not $UsePrebuilt) {
    Remove-Item -Recurse -Force $distDir -ErrorAction SilentlyContinue
    Log 'removed existing dist to force a fresh build' 'INFO'
}

if ($UsePrebuilt -and (Test-Path $distIndex)) {
    Log 'UsePrebuilt specified and dist/index.html exists; skipping build' 'OK'
} else {
    Push-Location $WebSource
    try {
        $oldNodeEnv = $env:NODE_ENV
        $env:NODE_ENV = 'production'
        $output = & $nodeTools.Npm run build 2>&1
        if ($null -ne $oldNodeEnv) { $env:NODE_ENV = $oldNodeEnv } else { Remove-Item Env:NODE_ENV -ErrorAction SilentlyContinue }
        if ($LASTEXITCODE -ne 0) {
            Log "Build failed (exit $LASTEXITCODE)" 'ERR'
            foreach ($line in $output) { Log $line 'ERR' }
            throw "build failed"
        }
        Log 'Build completed' 'OK'
    } catch {
        if ($null -ne $oldNodeEnv) { $env:NODE_ENV = $oldNodeEnv } else { Remove-Item Env:NODE_ENV -ErrorAction SilentlyContinue }
        if ($_.Exception.Message -ne 'build failed') { Log "Build: $_" 'ERR' }
        pause
        exit 1
    } finally { Pop-Location }
}

# ---- 6. Copy built files ----------------------------------------------------
Step 6 "Deploying web GUI to $WebTarget"
if (-not (Test-Path $distDir)) {
    Log "dist/ not found at $distDir - build may have failed." 'ERR'
    pause
    exit 1
}

# Remove old webgui target, copy fresh
if (Test-Path $WebTarget) {
    Remove-Item -Recurse -Force $WebTarget
    Log 'Removed previous webgui installation' 'INFO'
}
New-Item -ItemType Directory -Force -Path $WebTarget | Out-Null
Copy-Item -Path (Join-Path $distDir '*') -Destination $WebTarget -Recurse -Force
Log "Copied dist contents -> $WebTarget" 'OK'

$installedIndex = Join-Path $WebTarget 'index.html'
if (-not (Test-Path $installedIndex)) {
    Fail-WebGuiInstall "Installed index.html missing at $installedIndex"
}

$viteCmd = Resolve-ViteCmd
if (-not $viteCmd) {
    Fail-WebGuiInstall 'Local Vite command not found after npm ci'
}

$installState.used_tools.node = $nodeTools.Node
$installState.used_tools.npm = $nodeTools.Npm
$installState.used_tools.npx = $nodeTools.Npx
$installState.used_tools.vite = $viteCmd
$installState.versions.node = $nodeTools.NodeVersion
$installState.versions.npm = $nodeTools.NpmVersion
$installState.versions.npx = (& $nodeTools.Npx --version 2>&1)
$installState.versions.vite = ((& $viteCmd --version 2>&1) -join ' ')
$installState.used_tools.npx = $nodeTools.Npx
$installState.installed_by_flowshift.vite = $true
$installState.details.vite.installed_by_flowshift = $true
$installState.details.vite.source_path = $WebSource
$installState.details.vite.node_modules = (Join-Path $WebSource 'node_modules')
Write-InstallState $installState

# Also copy the icon for shortcuts
$icoSrc = Join-Path $RepoDir 'flowshift.ico'
if (Test-Path $icoSrc) {
    Copy-Item -Path $icoSrc -Destination (Join-Path $WebTarget 'flowshift.ico') -Force
}

$envReg = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
try {
    Set-ItemProperty -Path $envReg -Name 'FLOWSHIFT_WEBGUI_DIR' -Value $WebTarget -Force
    $env:FLOWSHIFT_WEBGUI_DIR = $WebTarget
    Log 'machine env FLOWSHIFT_WEBGUI_DIR set' 'OK'
} catch {
    Log "Could not set FLOWSHIFT_WEBGUI_DIR: $_" 'WARN'
}

# ---- 6b. Create default webgui config ---------------------------------------
$webguiCfg = Join-Path $WebTarget 'config.json'
if (-not (Test-Path $webguiCfg)) {
    $defaultCfg = @{ port = 5000 } | ConvertTo-Json
    Set-Content -Path $webguiCfg -Value $defaultCfg -Encoding UTF8
    Log "Created default webgui config (port 5000)" 'OK'
} else {
    Log "Webgui config already exists, keeping as-is" 'OK'
}

# ---- 7. Register uninstaller ------------------------------------------------
Step 7 'Registering uninstaller'
$uninKey  = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FlowShift Web GUI'
$uninBat  = Join-Path $WebTarget 'uninstall_webgui.bat'
$uninPs1  = Join-Path $WebTarget 'uninstall_webgui.ps1'

# Copy uninstaller alongside the webgui
Copy-Item -Path (Join-Path $RepoDir 'uninstall_webgui.ps1') -Destination $uninPs1 -Force
Copy-Item -Path (Join-Path $RepoDir 'uninstall_webgui.bat') -Destination $uninBat -Force

try {
    if (-not (Test-Path $uninKey)) { New-Item -Path $uninKey -Force | Out-Null }
    Set-ItemProperty -Path $uninKey -Name 'DisplayName' -Value 'FlowShift Web GUI'
    Set-ItemProperty -Path $uninKey -Name 'DisplayVersion' -Value '1.0.0'
    Set-ItemProperty -Path $uninKey -Name 'Publisher' -Value 'FlowShift'
    Set-ItemProperty -Path $uninKey -Name 'UninstallString' -Value "`"$uninBat`""
    Set-ItemProperty -Path $uninKey -Name 'InstallLocation' -Value "`"$WebTarget`""
    Set-ItemProperty -Path $uninKey -Name 'NoModify' -Value 1
    Set-ItemProperty -Path $uninKey -Name 'NoRepair' -Value 1
    Log 'Uninstaller registered (Settings -> Apps)' 'OK'
} catch {
    Log "Could not register uninstaller: $_" 'WARN'
}

# ---- 8. Shortcuts -----------------------------------------------------------
Step 8 'Creating shortcuts'
$startDir = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\FlowShift'
New-Item -ItemType Directory -Force -Path $startDir | Out-Null

$webUrl   = 'http://127.0.0.1:5000/'
$icoPath  = Join-Path $WebTarget 'flowshift.ico'

New-Shortcut (Join-Path $startDir 'FlowShift Web GUI.lnk') 'explorer.exe' $webUrl '' $icoPath 'Open FlowShift Web GUI (http://127.0.0.1:5000)'

# Desktop shortcut
$desktop = [Environment]::GetFolderPath('Desktop')
New-Shortcut (Join-Path $desktop 'FlowShift Web GUI.lnk') 'explorer.exe' $webUrl '' $icoPath 'Open FlowShift Web GUI (http://127.0.0.1:5000)'

# Uninstall shortcut
if (Test-Path $uninBat) {
    New-Shortcut (Join-Path $startDir 'Uninstall FlowShift Web GUI.lnk') $uninBat '' $WebTarget $icoPath 'Uninstall FlowShift Web GUI'
}

Log 'Shortcuts created (Desktop + Start Menu)' 'OK'

$test = Test-WebGuiHttp
if (-not $test.Ok) {
    Log "Web GUI not responding yet: $($test.Body)" 'WARN'
    Restart-FlowShiftRuntimeIfNeeded 'webgui-install'
    Start-Sleep -Seconds 2
    $test = Test-WebGuiHttp
}
if ($test.Ok) {
    $statusTest = Test-WebGuiStatus
    if (-not $statusTest.Ok) {
        Fail-WebGuiInstall 'WebAPI running but frontend not served'
    }
    Log "Web GUI responded with HTTP $($test.StatusCode); API status OK" 'OK'
} else {
    Fail-WebGuiInstall 'WebAPI running but frontend not served'
}

# ---- Done -------------------------------------------------------------------
Write-Host "`n============================================" -ForegroundColor Green
Write-Host "    FlowShift Web GUI installation complete  " -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host "`n  Installed to: $WebTarget" -ForegroundColor White
Write-Host "  Open in browser: $webUrl" -ForegroundColor White
Write-Host "  (after starting FlowShift)`n"
