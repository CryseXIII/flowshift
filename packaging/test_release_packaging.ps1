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
        'install_flowshift.bat',
        'uninstall_flowshift.ps1',
        'update_flowshift.ps1',
        'src\python\webgui_health.py',
        'src\python\clipboard_events.py',
        'src\python\clipboard_file_preview.py',
        'src\python\clipboard_framing_v2.py',
        'src\python\clipboard_flow_control_v2.py',
        'src\python\clipboard_materialize_v2.py',
        'src\python\clipboard_object_store_v2.py',
        'src\python\clipboard_preflight_v2.py',
        'src\python\clipboard_resume_v2.py',
        'src\python\clipboard_streaming_v2.py',
        'src\python\clipboard_transfer_control_v2.py',
        'src\python\clipboard_transport_v2.py',
        'src\python\overlay_actions.py',
        'src\python\action_handlers.py',
        'src\python\hotkey_status.py',
        'src\python\input_trigger.py',
        'src\python\wheel_gesture.py',
        'src\python\lan_scan.py',
        'src\python\event_log.py',
        'src\python\autostart.py',
        'src\python\overlay_native_wheel.py',
        'src\python\remote_preview.py',
        'src\python\tray.py',
        'src\python\web_api.py',
        'src\python\update_manager.py',
        'src\python\install_lock.py',
        'src\python\update_credentials.py',
        'src\python\usb_package.py',
        'src\python\peer_update.py',
        'src\python\peer_discovery.py',
        'src\python\input_backends\windows_win32.py',
        'webgui\dist\index.html',
        'webgui\dist\overlay.html',
        'webgui\dist\VERSION'
    )) {
        Assert-True (Test-Path -LiteralPath (Join-Path $payload $relative) -PathType Leaf) "Missing payload file: $relative"
    }
    foreach ($relative in @(
        'src\python\service.py',
        'src\python\gui.py',
        'src\python\worker_smoke_test.py',
        'src\python\test_update_client.py',
        'webgui\src',
        'webgui\node_modules',
        'install_webgui.ps1',
        'install_webgui.bat',
        'uninstall_webgui.ps1',
        'uninstall_webgui.bat'
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
        $importOutput = & python -c 'import clipboard_file_preview, clipboard_flow_control_v2, clipboard_framing_v2, clipboard_materialize_v2, clipboard_object_store_v2, clipboard_preflight_v2, clipboard_resume_v2, clipboard_streaming_v2, clipboard_transfer_control_v2, clipboard_transport_v2, overlay_actions, action_handlers, hotkey_status, input_trigger, wheel_gesture, overlay_native_wheel, overlay_host, remote_preview, usb_package, peer_update, event_log, autostart, tray, web_api' 2>&1
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
    $webBuilder = [System.IO.File]::ReadAllText((Join-Path $RepoRoot 'install_webgui.ps1'))
    $inno = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'FlowShift.iss'))
    Assert-True ($coreInstaller -match '\[switch\]\$NonInteractive') 'Core installer lacks NonInteractive mode'
    Assert-True ($coreInstaller -match '\[switch\]\$FlowUpdate') 'Core installer lacks FlowUpdate mode'
    Assert-True ($coreInstaller -match "'update_flowshift\.ps1'") 'Core installer does not deploy the update runner'
    Assert-True ($coreInstaller -match '--require-hashes') 'Core installer does not enforce the Python lock hashes'
    Assert-True ($coreInstaller -match "'install_flowshift\.ps1'") 'Core installer does not keep itself in the program folder (USB package)'
    Assert-True ($coreInstaller -match '\$script:UsbPackage = Read-UsbPackage') 'Core installer does not check the offline files of a USB package'
    Assert-True ($coreInstaller -notmatch 'Python\.Python\.3\.12|3\.12\.9') 'Core installer still hard-codes Python 3.12'
    Assert-True ($coreInstaller -match 'Install-WebGuiAssets -Source') 'Core installer does not deploy the WebGUI'
    Assert-True ($coreInstaller -match 'Test-InstalledFlowShift') 'Core installer does not verify program and WebGUI'
    Assert-True ($coreInstaller -match '\[switch\]\$Interactive') 'Core installer lacks the opt-in question switch'
    Assert-True ($webBuilder -match '\$MinNodeMajor = 24') 'WebGUI build helper does not require the production Node LTS major'
    Assert-True ($webBuilder -notmatch 'winget|msiexec') 'WebGUI build helper must not install Node.js'
    $stagedVersion = ([System.IO.File]::ReadAllText((Join-Path $payload 'webgui\dist\VERSION'))).Trim()
    Assert-True ($stagedVersion -ceq $version) "Staged WebGUI VERSION '$stagedVersion' does not match '$version'"
    Assert-True ($inno -match 'OutputBaseFilename=FlowShift-Setup') 'Inno output name is not fixed'
    Assert-True ($inno -match "HasCommandLineParameter\('/FLOWUPDATE'\)") 'Inno setup does not recognize FLOWUPDATE'
    Assert-True ($inno -match "install_flowshift\.ps1") 'Inno setup does not run the core installer'
    Assert-True ($inno -notmatch "install_webgui\.ps1") 'Inno setup must not run a separate WebGUI installer'

    Write-Host 'Release packaging contract tests passed.'
} finally {
    if (Test-Path -LiteralPath $BuildRoot) {
        Remove-Item -LiteralPath $BuildRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
