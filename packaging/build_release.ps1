param(
    [string]$Tag,
    [string]$BuildRoot,
    [string]$IsccPath,
    [string]$MinimumUpdaterVersion = '0.4.0',
    [switch]$StageOnly
)

$ErrorActionPreference = 'Stop'
$script:StableSemVer = '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
$script:InstallerName = 'FlowShift-Setup.exe'
$script:MaxInstallerBytes = 2GB
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Require-File {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required release input is missing: $Path"
    }
    return (Get-Item -LiteralPath $Path).FullName
}

function Copy-PayloadFile {
    param([string]$RelativePath, [string]$PayloadRoot)
    $source = Join-Path $RepoRoot $RelativePath
    Require-File $source | Out-Null
    $destination = Join-Path $PayloadRoot $RelativePath
    $parent = Split-Path -Parent $destination
    [System.IO.Directory]::CreateDirectory($parent) | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Resolve-Iscc {
    param([string]$RequestedPath)
    $candidates = @()
    if ($RequestedPath) { $candidates += $RequestedPath }
    if ($env:ISCC_PATH) { $candidates += $env:ISCC_PATH }
    if (${env:ProgramFiles(x86)}) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe')
    }
    if ($env:ProgramFiles) {
        $candidates += (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    }
    if ($env:LOCALAPPDATA) {
        $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
    }
    $command = Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { $candidates += $command.Source }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Get-Item -LiteralPath $candidate).FullName
        }
    }
    throw 'Inno Setup 6 ISCC.exe was not found. Set -IsccPath or ISCC_PATH.'
}

$versionPath = Require-File (Join-Path $RepoRoot 'VERSION')
$version = ([System.IO.File]::ReadAllText($versionPath)).Trim()
if ($version -cnotmatch $script:StableSemVer) {
    throw "VERSION must be canonical stable SemVer for a release build: '$version'"
}
$expectedTag = "v$version"
if ($Tag -and $Tag -cne $expectedTag) {
    throw "Release tag '$Tag' does not match VERSION '$version'"
}
$Tag = $expectedTag
if ($MinimumUpdaterVersion -cnotmatch $script:StableSemVer) {
    throw "MinimumUpdaterVersion must be canonical stable SemVer: '$MinimumUpdaterVersion'"
}
if ([version]$MinimumUpdaterVersion -gt [version]$version) {
    throw 'MinimumUpdaterVersion cannot be newer than the release version'
}

if (-not $BuildRoot) { $BuildRoot = Join-Path $RepoRoot 'build' }
$BuildRoot = [System.IO.Path]::GetFullPath($BuildRoot)
$payloadRoot = Join-Path $BuildRoot 'payload'
$releaseRoot = Join-Path $BuildRoot 'release'
foreach ($directory in @($payloadRoot, $releaseRoot)) {
    if (Test-Path -LiteralPath $directory) {
        Remove-Item -LiteralPath $directory -Recurse -Force
    }
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
}

$rootFiles = @(
    'VERSION',
    'LICENSE',
    'README.md',
    'requirements.txt',
    'install_flowshift.ps1',
    'install_webgui.ps1',
    'uninstall_flowshift.bat',
    'uninstall_flowshift.ps1',
    'uninstall_webgui.bat',
    'uninstall_webgui.ps1',
    'update_flowshift.ps1'
)
foreach ($file in $rootFiles) { Copy-PayloadFile $file $payloadRoot }

$pythonFiles = @(
    'clipboard_files.py',
    'clipboard_html.py',
    'clipboard_image.py',
    'clipboard_model.py',
    'clipboard_preview.py',
    'clipboard_protocol.py',
    'clipboard_runtime.py',
    'clipboard_sources.py',
    'clipboard_store.py',
    'clipboard_transfer.py',
    'clipboard_win.py',
    'config_schema.py',
    'elevated_task.py',
    'flowshift_diagnose.py',
    'flowshift_diagnostics.py',
    'flowshift.ico',
    'gui.py',
    'input_events.py',
    'keymap.py',
    'overlay_controller.py',
    'overlay_geometry.py',
    'overlay_host.py',
    'overlay_protocol.py',
    'platform_capabilities.py',
    'runtime_model.py',
    'tray.py',
    'update_client.py',
    'update_download.py',
    'update_handoff.py',
    'update_manager.py',
    'update_model.py',
    'update_runtime.py',
    'update_safety.py',
    'update_state.py',
    'version.py'
)
foreach ($file in $pythonFiles) {
    Copy-PayloadFile (Join-Path 'src\python' $file) $payloadRoot
}

$backendFiles = @('__init__.py', 'base.py', 'linux_stub.py', 'unsupported.py', 'windows_win32.py')
foreach ($file in $backendFiles) {
    Copy-PayloadFile (Join-Path 'src\python\input_backends' $file) $payloadRoot
}

$docsSource = Join-Path $RepoRoot 'docs'
if (-not (Test-Path -LiteralPath $docsSource -PathType Container)) {
    throw "Required release input is missing: $docsSource"
}
Copy-Item -LiteralPath $docsSource -Destination $payloadRoot -Recurse -Force

$distSource = Join-Path $RepoRoot 'webgui\dist'
Require-File (Join-Path $distSource 'index.html') | Out-Null
Require-File (Join-Path $distSource 'overlay.html') | Out-Null
$distTarget = Join-Path $payloadRoot 'webgui\dist'
[System.IO.Directory]::CreateDirectory($distTarget) | Out-Null
Copy-Item -Path (Join-Path $distSource '*') -Destination $distTarget -Recurse -Force

$forbidden = @(
    (Join-Path $payloadRoot 'src\python\service.py'),
    (Join-Path $payloadRoot 'src\python\test_update_client.py'),
    (Join-Path $payloadRoot 'webgui\src'),
    (Join-Path $payloadRoot 'node_modules')
)
foreach ($path in $forbidden) {
    if (Test-Path -LiteralPath $path) { throw "Forbidden development payload entry was staged: $path" }
}

Write-Host "Curated FlowShift $version payload staged at $payloadRoot"
if ($StageOnly) { return }

$compiler = Resolve-Iscc $IsccPath
$definition = Join-Path $PSScriptRoot 'FlowShift.iss'
Require-File $definition | Out-Null
& $compiler "/DMyAppVersion=$version" "/DSourceRoot=$payloadRoot" "/DOutputDir=$releaseRoot" $definition
if ($LASTEXITCODE -ne 0) { throw "ISCC.exe failed with exit code $LASTEXITCODE" }

$installerPath = Require-File (Join-Path $releaseRoot $script:InstallerName)
$installer = Get-Item -LiteralPath $installerPath
if ($installer.Length -le 0 -or $installer.Length -gt $script:MaxInstallerBytes) {
    throw "Installer size is outside the update contract: $($installer.Length)"
}
$installerHash = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
$manifestPath = Join-Path $releaseRoot 'update-manifest.json'
$manifest = [ordered]@{
    schema_version = 1
    version = $version
    tag = $Tag
    channel = 'stable'
    installer = [ordered]@{
        name = $script:InstallerName
        size = [long]$installer.Length
        sha256 = $installerHash
    }
    minimum_updater_version = $MinimumUpdaterVersion
}
Write-Utf8NoBom $manifestPath (($manifest | ConvertTo-Json -Depth 5) + "`n")

$manifestHash = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
$checksumsPath = Join-Path $releaseRoot 'SHA256SUMS.txt'
Write-Utf8NoBom $checksumsPath (
    "$installerHash  $script:InstallerName`n$manifestHash  update-manifest.json`n")

$verified = [System.IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
if ($verified.schema_version -isnot [int] -or $verified.schema_version -ne 1 -or
        $verified.version -cne $version -or $verified.tag -cne $Tag -or
        $verified.channel -cne 'stable' -or
        $verified.installer.name -cne $script:InstallerName -or
        [long]$verified.installer.size -ne $installer.Length -or
        $verified.installer.sha256 -cne $installerHash -or
        $verified.minimum_updater_version -cne $MinimumUpdaterVersion) {
    throw 'Generated update manifest failed release-contract verification'
}
foreach ($asset in @($installerPath, $manifestPath, $checksumsPath)) {
    Require-File $asset | Out-Null
}
Write-Host "Release artifacts created at $releaseRoot"
