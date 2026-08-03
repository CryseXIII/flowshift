param([string]$BuildRoot)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $BuildRoot) {
    $BuildRoot = Join-Path $env:TEMP ("flowshift-release-test-" + [guid]::NewGuid().ToString('N'))
}

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

try {
    $version = ([System.IO.File]::ReadAllText((Join-Path $RepoRoot 'VERSION'))).Trim()
    if ($version -match '-') {
        $rejected = $false
        try {
            & (Join-Path $PSScriptRoot 'build_release.ps1') -Tag "v$version" -BuildRoot $BuildRoot -StageOnly
        } catch {
            $rejected = $true
        }
        Assert-True $rejected 'Release builder accepted a development VERSION as stable'
        Write-Host 'Development VERSION correctly rejected by stable release packaging.'
        & (Join-Path $PSScriptRoot 'build_release.ps1') -Tag "v$version" -BuildRoot $BuildRoot `
            -StageOnly -AllowDevelopmentStage
    }
    if ($version -notmatch '-') {
        $rejected = $false
        try {
            & (Join-Path $PSScriptRoot 'build_release.ps1') -Tag 'v99.0.0' -BuildRoot $BuildRoot -StageOnly
        } catch {
            $rejected = $true
        }
        Assert-True $rejected 'Release builder accepted a tag that does not match VERSION'

        $rejected = $false
        try {
            & (Join-Path $PSScriptRoot 'build_release.ps1') -Tag "v$version" `
                -MinimumUpdaterVersion '99.0.0' -BuildRoot $BuildRoot -StageOnly
        } catch {
            $rejected = $true
        }
        Assert-True $rejected 'Release builder accepted a future minimum updater version'

        & (Join-Path $PSScriptRoot 'build_release.ps1') -Tag "v$version" -BuildRoot $BuildRoot -StageOnly

    }
    $payload = Join-Path $BuildRoot 'payload'
    foreach ($relative in @(
        'VERSION',
        'LICENSE',
        'install_flowshift.ps1',
        'install_webgui.ps1',
        'update_flowshift.ps1',
        'src\python\clipboard_events.py',
        'src\python\clipboard_framing_v2.py',
        'src\python\clipboard_flow_control_v2.py',
        'src\python\tray.py',
        'src\python\web_api.py',
        'src\python\update_manager.py',
        'src\python\input_backends\windows_win32.py',
        'webgui\dist\index.html',
        'webgui\dist\overlay.html'
    )) {
        Assert-True (Test-Path -LiteralPath (Join-Path $payload $relative) -PathType Leaf) "Missing payload file: $relative"
    }
    foreach ($relative in @(
        'src\python\service.py',
        'src\python\worker_smoke_test.py',
        'src\python\test_update_client.py',
        'webgui\src',
        'webgui\node_modules'
    )) {
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $payload $relative))) "Development file leaked into payload: $relative"
    }

    $payloadPython = Join-Path $payload 'src\python'
    $previousPythonPath = $env:PYTHONPATH
    $previousConfigPath = $env:FLOWSHIFT_CONFIG
    $previousLogDir = $env:FLOWSHIFT_LOG_DIR
    $previousErrorAction = $ErrorActionPreference
    try {
        $env:PYTHONPATH = $payloadPython
        $env:FLOWSHIFT_CONFIG = Join-Path $BuildRoot 'import-smoke-config.json'
        $env:FLOWSHIFT_LOG_DIR = Join-Path $BuildRoot 'import-smoke-logs'
        $ErrorActionPreference = 'Continue'
        $importOutput = & python -c 'import clipboard_flow_control_v2, clipboard_framing_v2, tray, web_api, gui' 2>&1
        Assert-True ($LASTEXITCODE -eq 0) "Productive staged imports failed: $importOutput"
    } finally {
        $ErrorActionPreference = $previousErrorAction
        if ($null -eq $previousPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
        else { $env:PYTHONPATH = $previousPythonPath }
        if ($null -eq $previousConfigPath) { Remove-Item Env:FLOWSHIFT_CONFIG -ErrorAction SilentlyContinue }
        else { $env:FLOWSHIFT_CONFIG = $previousConfigPath }
        if ($null -eq $previousLogDir) { Remove-Item Env:FLOWSHIFT_LOG_DIR -ErrorAction SilentlyContinue }
        else { $env:FLOWSHIFT_LOG_DIR = $previousLogDir }
    }

    $coreInstaller = [System.IO.File]::ReadAllText((Join-Path $RepoRoot 'install_flowshift.ps1'))
    $webInstaller = [System.IO.File]::ReadAllText((Join-Path $RepoRoot 'install_webgui.ps1'))
    $inno = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'FlowShift.iss'))
    Assert-True ($coreInstaller -match '\[switch\]\$NonInteractive') 'Core installer lacks NonInteractive mode'
    Assert-True ($coreInstaller -match '\[switch\]\$FlowUpdate') 'Core installer lacks FlowUpdate mode'
    Assert-True ($coreInstaller -match "'update_flowshift\.ps1'") 'Core installer does not deploy the update runner'
    Assert-True ($coreInstaller -match '--require-hashes') 'Core installer does not enforce the Python lock hashes'
    Assert-True ($coreInstaller -notmatch 'Python\.Python\.3\.12|3\.12\.9') 'Core installer still hard-codes Python 3.12'
    Assert-True ($webInstaller -match '\[switch\]\$FlowUpdate') 'WebGUI installer lacks FlowUpdate mode'
    Assert-True ($webInstaller -match 'UsePrebuilt') 'WebGUI installer lacks prebuilt mode'
    Assert-True ($webInstaller -match '\$MinNodeMajor = 24') 'Source installer does not require the production Node LTS major'
    Assert-True ($inno -match 'OutputBaseFilename=FlowShift-Setup') 'Inno output name is not fixed'
    Assert-True ($inno -match "HasCommandLineParameter\('/FLOWUPDATE'\)") 'Inno setup does not recognize FLOWUPDATE'
    Assert-True ($inno -match "install_flowshift\.ps1") 'Inno setup does not run the core installer'
    Assert-True ($inno -match "install_webgui\.ps1") 'Inno setup does not run the WebGUI installer'

    Write-Host 'Release packaging contract tests passed.'
} finally {
    if (Test-Path -LiteralPath $BuildRoot) {
        Remove-Item -LiteralPath $BuildRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
