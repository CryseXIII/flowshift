# FlowShift install diagnostics

$ErrorActionPreference = 'Continue'
$InstallDir = Join-Path $env:ProgramFiles 'FlowShift'
$DataDir = Join-Path $env:ProgramData 'FlowShift'
$ConfigPath = Join-Path $DataDir 'config.json'
$StatePath = Join-Path $DataDir 'install_state.json'
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WebSource = Join-Path $RepoDir 'webgui'
$WebDir = Join-Path $InstallDir 'webgui'
$WebIndex = Join-Path $WebDir 'index.html'
$WebOverlay = Join-Path $WebDir 'overlay.html'
$VenvPy = Join-Path $InstallDir '.venv\Scripts\python.exe'
$VenvPyw = Join-Path $InstallDir '.venv\Scripts\pythonw.exe'

function Out-Check {
    param([string]$Label, [string]$Status, [string]$Detail = '')
    if ($Detail) {
        Write-Host ("[{0}] {1}: {2}" -f $Status, $Label, $Detail)
    } else {
        Write-Host ("[{0}] {1}" -f $Status, $Label)
    }
}

function Test-UrlJson {
    param([string]$Url)
    try {
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
        $json = $resp.Content | ConvertFrom-Json
        return [pscustomobject]@{ Ok = $true; Code = $resp.StatusCode; Body = $resp.Content; Json = $json }
    } catch {
        return [pscustomobject]@{ Ok = $false; Code = 0; Body = $_.Exception.Message; Json = $null }
    }
}

function Test-UrlHtml {
    param([string]$Url)
    try {
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
        $body = [string]$resp.Content
        return [pscustomobject]@{ Ok = ($resp.StatusCode -eq 200 -and ($body -match '<html|id="root"|vite')); Code = $resp.StatusCode; Body = $body }
    } catch {
        return [pscustomobject]@{ Ok = $false; Code = 0; Body = $_.Exception.Message }
    }
}

function Read-State {
    if (Test-Path $StatePath) {
        try { return Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json } catch { }
    }
    return $null
}

function Get-CommandPath {
    param([string]$Name)
    try {
        $cmd = Get-Command $Name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) { return $cmd.Source }
    } catch { }
    return $null
}

function Get-VersionText {
    param([string]$Exe, [string]$Args = '--version')
    if (-not $Exe -or -not (Test-Path $Exe)) { return $null }
    try { return ((& $Exe $Args 2>&1) -join ' ').Trim() } catch { return $null }
}

function Resolve-LocalVite {
    $src = $null
    $state = Read-State
    if ($state -and $state.details -and $state.details.vite -and $state.details.vite.source_path) {
        $src = [string]$state.details.vite.source_path
    } elseif (Test-Path $WebSource) {
        $src = $WebSource
    }
    if ($src -and (Test-Path $src)) {
        $vite = Join-Path $src 'node_modules\.bin\vite.cmd'
        if (Test-Path $vite) { return $vite }
    }
    return $null
}

function Test-PortOpen {
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
        return [bool]$conn
    } catch {
        return $false
    }
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

Write-Host 'FlowShift Install Diagnostics'
Write-Host ''

if (Test-Path $InstallDir) { Out-Check 'InstallDir exists' 'PASS' $InstallDir } else { Out-Check 'InstallDir exists' 'FAIL' 'missing' }
if (Test-Path $DataDir) { Out-Check 'DataDir exists' 'PASS' $DataDir } else { Out-Check 'DataDir exists' 'WARN' 'missing' }
if (Test-Path (Join-Path $InstallDir 'src\python\tray.py')) { Out-Check 'tray.py' 'PASS' (Join-Path $InstallDir 'src\python\tray.py') } else { Out-Check 'tray.py' 'FAIL' 'missing' }
foreach ($overlayModule in @('overlay_host.py','overlay_controller.py','overlay_protocol.py','overlay_geometry.py')) {
    $overlayModulePath = Join-Path $InstallDir "src\python\$overlayModule"
    if (Test-Path -LiteralPath $overlayModulePath -PathType Leaf) { Out-Check $overlayModule 'PASS' $overlayModulePath } else { Out-Check $overlayModule 'FAIL' 'missing' }
}
if (Test-Path $VenvPyw) { Out-Check 'pythonw.exe' 'PASS' $VenvPyw } else { Out-Check 'pythonw.exe' 'WARN' 'missing or venv not created' }
if (Test-Path $WebIndex) { Out-Check 'webgui index.html' 'PASS' $WebIndex } else { Out-Check 'webgui index.html' 'FAIL' 'missing' }
if (Test-Path $WebOverlay) { Out-Check 'webgui overlay.html' 'PASS' $WebOverlay } else { Out-Check 'webgui overlay.html' 'FAIL' 'missing' }
if (Test-Path $ConfigPath) { Out-Check 'config.json' 'PASS' $ConfigPath } else { Out-Check 'config.json' 'WARN' 'missing' }
if (Test-Path $StatePath) { Out-Check 'install_state.json' 'PASS' $StatePath } else { Out-Check 'install_state.json' 'WARN' 'missing' }

$state = Read-State
if ($state) {
    foreach ($k in @('python','nodejs','vite')) {
        $flag = $state.installed_by_flowshift.$k
        Out-Check "installed_by_flowshift.$k" 'PASS' ([string]$flag)
    }
    foreach ($k in @('python','node','npm','npx','vite')) {
        $tool = $state.used_tools.$k
        $ver = $state.versions.$k
        if ($tool) { Out-Check "used_tools.$k" 'PASS' "$tool $ver" } else { Out-Check "used_tools.$k" 'WARN' 'missing' }
    }
    if ($state.details -and $state.details.vite) {
        Out-Check 'vite.source_path' 'PASS' ([string]$state.details.vite.source_path)
        Out-Check 'vite.node_modules' 'PASS' ([string]$state.details.vite.node_modules)
    }
}

$pythonExe = $null
$nodeExe = $null
$npmCmd = $null
$npxCmd = $null
if ($state -and $state.used_tools) {
    if ($state.used_tools.python) { $pythonExe = [string]$state.used_tools.python }
    if ($state.used_tools.node) { $nodeExe = [string]$state.used_tools.node }
    if ($state.used_tools.npm) { $npmCmd = [string]$state.used_tools.npm }
    if ($state.used_tools.npx) { $npxCmd = [string]$state.used_tools.npx }
}
if (-not $pythonExe) { $pythonExe = Get-CommandPath 'python' }
if (-not $pythonExe -and (Test-Path $VenvPyw)) { $pythonExe = (Join-Path (Split-Path -Parent $VenvPyw) 'python.exe') }
if (-not $nodeExe) { $nodeExe = Get-CommandPath 'node' }
if (-not $npmCmd) { $npmCmd = Get-CommandPath 'npm' }
if ($npmCmd -and ([IO.Path]::GetExtension($npmCmd) -ieq '.ps1')) {
    $candidate = [IO.Path]::ChangeExtension($npmCmd, '.cmd')
    if (Test-Path $candidate) { $npmCmd = $candidate }
}
if (-not $npxCmd -and $npmCmd) { $npxCmd = $npmCmd }
$viteCmd = if ($state -and $state.used_tools -and $state.used_tools.vite) { [string]$state.used_tools.vite } else { Resolve-LocalVite }

if ($pythonExe) { $pyVer = Get-VersionText $pythonExe '--version'; Out-Check 'Python global path/version' 'PASS' "$(Split-Path -Leaf $pythonExe) $(if ($pyVer) { $pyVer } else { 'unknown' })" } else { Out-Check 'Python global path/version' 'WARN' 'not found' }
if (Test-Path $VenvPyw) { Out-Check 'venv pythonw.exe' 'PASS' $VenvPyw } else { Out-Check 'venv pythonw.exe' 'FAIL' 'missing' }
if (Test-Path (Join-Path (Split-Path -Parent $VenvPyw) 'python.exe')) { Out-Check 'venv python.exe' 'PASS' (Join-Path (Split-Path -Parent $VenvPyw) 'python.exe') } else { Out-Check 'venv python.exe' 'FAIL' 'missing' }
if (Test-Path -LiteralPath $VenvPy -PathType Leaf) {
    & $VenvPy -c 'import webview' 2>$null
    if ($LASTEXITCODE -eq 0) { Out-Check 'venv pywebview import' 'PASS' 'import webview succeeded' } else { Out-Check 'venv pywebview import' 'FAIL' "python exited with code $LASTEXITCODE" }
} else {
    Out-Check 'venv pywebview import' 'FAIL' 'venv python.exe missing'
}
$webView2 = Get-WebView2Runtime
if ($webView2) { Out-Check 'WebView2 Evergreen runtime' 'PASS' "$($webView2.Path) [$($webView2.Version)]" } else { Out-Check 'WebView2 Evergreen runtime' 'FAIL' 'msedgewebview2.exe not found in machine or user Evergreen locations' }
if ($nodeExe) { $nodeVer = Get-VersionText $nodeExe; Out-Check 'Node.js path/version' 'PASS' "$(Split-Path -Leaf $nodeExe) $(if ($nodeVer) { $nodeVer } else { 'unknown' })" } else { Out-Check 'Node.js path/version' 'WARN' 'not found' }
if ($npmCmd) { $npmVer = Get-VersionText $npmCmd; Out-Check 'npm path/version' 'PASS' "$(Split-Path -Leaf $npmCmd) $(if ($npmVer) { $npmVer } else { 'unknown' })" } else { Out-Check 'npm path/version' 'WARN' 'not found' }
if ($npxCmd) { $npxVer = Get-VersionText $npxCmd; Out-Check 'npx path/version' 'PASS' "$(Split-Path -Leaf $npxCmd) $(if ($npxVer) { $npxVer } else { 'unknown' })" } else { Out-Check 'npx path/version' 'WARN' 'not found' }
if ($viteCmd) { $viteVer = Get-VersionText $viteCmd; Out-Check 'Vite local path/version' 'PASS' "$(Split-Path -Leaf $viteCmd) $(if ($viteVer) { $viteVer } else { 'unknown' })" } else { Out-Check 'Vite local path/version' 'WARN' 'not found' }

foreach ($port in @(45781,45782,5000)) {
    if (Test-PortOpen $port) { Out-Check "port $port" 'PASS' 'open' } else { Out-Check "port $port" 'WARN' 'closed' }
}

try {
    $task = Get-ScheduledTask -TaskName 'FlowShift' -ErrorAction SilentlyContinue
    if ($task) { Out-Check 'Scheduled Task FlowShift' 'PASS' 'present' } else { Out-Check 'Scheduled Task FlowShift' 'WARN' 'not found' }
} catch {
    Out-Check 'Scheduled Task FlowShift' 'WARN' $_.Exception.Message
}

foreach ($reg in @(
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FlowShift',
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FlowShift Web GUI'
)) {
    if (Test-Path $reg) { Out-Check "registry $reg" 'PASS' 'present' } else { Out-Check "registry $reg" 'WARN' 'missing' }
}

$machineEnv = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
foreach ($name in @('FLOWSHIFT_CONFIG','FLOWSHIFT_LOG_DIR','FLOWSHIFT_WEBGUI_DIR')) {
    try {
        $value = [Environment]::GetEnvironmentVariable($name, 'Machine')
        if (-not $value) { $value = (Get-ItemProperty -Path $machineEnv -Name $name -ErrorAction SilentlyContinue).$name }
        if ($value) { Out-Check $name 'PASS' $value } else { Out-Check $name 'WARN' 'not set' }
    } catch {
        Out-Check $name 'WARN' $_.Exception.Message
    }
}

$html = Test-UrlHtml 'http://127.0.0.1:5000/'
if ($html.Ok) {
    Out-Check 'GET /' 'PASS' "HTTP $($html.Code)"
} else {
    if ($html.Body -match 'not found|webgui_not_installed|"error"') {
        Out-Check 'GET /' 'FAIL' 'WebAPI is running, but static WebGUI frontend is not being served.'
    } else {
        Out-Check 'GET /' 'FAIL' $html.Body
    }
}

$status = Test-UrlJson 'http://127.0.0.1:5000/api/status'
if ($status.Ok) { Out-Check 'GET /api/status' 'PASS' "HTTP $($status.Code)" } else { Out-Check 'GET /api/status' 'FAIL' $status.Body }

$overlayHtml = Test-UrlHtml 'http://127.0.0.1:5000/overlay.html'
if ($overlayHtml.Ok -and $overlayHtml.Body -match 'overlay-root' -and $overlayHtml.Body -match 'FlowShift Overlay') {
    Out-Check 'GET /overlay.html' 'PASS' "HTTP $($overlayHtml.Code), overlay markers present"
} else {
    Out-Check 'GET /overlay.html' 'FAIL' 'missing overlay-root/FlowShift Overlay markers or endpoint unavailable'
}
