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

    $payload = Join-Path $BuildRoot 'payload'
    foreach ($relative in @(
        'VERSION',
        'LICENSE',
        'install_flowshift.ps1',
        'install_webgui.ps1',
        'update_flowshift.ps1',
        'src\python\clipboard_events.py',
        'src\python\tray.py',
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

    $coreInstaller = [System.IO.File]::ReadAllText((Join-Path $RepoRoot 'install_flowshift.ps1'))
    $webInstaller = [System.IO.File]::ReadAllText((Join-Path $RepoRoot 'install_webgui.ps1'))
    $inno = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'FlowShift.iss'))
    Assert-True ($coreInstaller -match '\[switch\]\$NonInteractive') 'Core installer lacks NonInteractive mode'
    Assert-True ($coreInstaller -match '\[switch\]\$FlowUpdate') 'Core installer lacks FlowUpdate mode'
    Assert-True ($coreInstaller -match "'update_flowshift\.ps1'") 'Core installer does not deploy the update runner'
    Assert-True ($webInstaller -match '\[switch\]\$FlowUpdate') 'WebGUI installer lacks FlowUpdate mode'
    Assert-True ($webInstaller -match 'UsePrebuilt') 'WebGUI installer lacks prebuilt mode'
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
