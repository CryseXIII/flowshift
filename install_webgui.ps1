# FlowShift WebGUI build helper (internal building block)
#
# NOT a user entry point. install_flowshift.ps1 installs and updates the WebGUI
# together with the program; it calls this helper only when the checkout has no
# prebuilt webgui\dist for the current VERSION and Node.js is already present.
# Release payloads (FlowShift-Setup.exe) always ship a prebuilt, version-stamped
# webgui\dist and never need Node.js.
#
# The helper never installs Node.js and never touches Program Files. It runs
# `npm ci` and `npm run build` in the checkout and verifies webgui\dist.
#
# Exit codes: 0 = dist built and verified, 1 = build failed, 3 = Node.js 24+
# (node.exe + npm) is not available.

param(
    [string]$WebSource,
    [string]$ExpectedVersion,
    [string]$LogPath
)

$ErrorActionPreference = 'Stop'
$MinNodeMajor = 24
if (-not $WebSource) { $WebSource = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'webgui' }
$DistDir = Join-Path $WebSource 'dist'

function Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = ('[{0}] [{1}] webgui-build: {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message)
    if ($LogPath) { try { Add-Content -Path $LogPath -Value $line } catch { } }
    $color = switch ($Level) { 'ERROR' {'Red'} 'WARN' {'Yellow'} 'OK' {'Green'} default {'Gray'} }
    Write-Host $line -ForegroundColor $color
}

function Resolve-NodeTools {
    $node = $null
    foreach ($candidate in @(
        (Join-Path $env:ProgramFiles 'nodejs\node.exe'),
        $(if (${env:ProgramFiles(x86)}) { Join-Path ${env:ProgramFiles(x86)} 'nodejs\node.exe' })
    )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { $node = $candidate; break }
    }
    if (-not $node) {
        $cmd = Get-Command node -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) { $node = $cmd.Source }
    }
    if (-not $node) { return $null }
    $npm = Join-Path (Split-Path -Parent $node) 'npm.cmd'
    if (-not (Test-Path -LiteralPath $npm -PathType Leaf)) {
        $cmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
        if (-not ($cmd -and $cmd.Source)) { return $null }
        $npm = $cmd.Source
    }
    $nodeVersion = $null
    try { $nodeVersion = ((& $node --version 2>&1) | Out-String).Trim() } catch { }
    if ($nodeVersion -notmatch '^v?(\d+)' -or [int]$Matches[1] -lt $MinNodeMajor) {
        Log "Node.js $nodeVersion at $node is older than the required major $MinNodeMajor" 'WARN'
        return $null
    }
    return [pscustomobject]@{ Node = $node; Npm = $npm; NodeVersion = $nodeVersion }
}

function Invoke-Npm {
    param([object]$Tools, [string[]]$Arguments, [string]$NodeEnv)
    $oldNodeEnv = $env:NODE_ENV
    # npm prints warnings on stderr; Windows PowerShell must not turn them into errors.
    $ErrorActionPreference = 'Continue'
    try {
        if ($NodeEnv) { $env:NODE_ENV = $NodeEnv } else { Remove-Item Env:NODE_ENV -ErrorAction SilentlyContinue }
        $output = & $Tools.Npm @Arguments 2>&1
        $code = $LASTEXITCODE
        if ($code -ne 0) {
            foreach ($line in @($output | Select-Object -Last 40)) { Log ([string]$line) 'ERROR' }
            throw "npm $($Arguments -join ' ') failed with exit code $code"
        }
    } finally {
        if ($null -ne $oldNodeEnv) { $env:NODE_ENV = $oldNodeEnv } else { Remove-Item Env:NODE_ENV -ErrorAction SilentlyContinue }
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $WebSource 'package.json') -PathType Leaf)) {
    Log "WebGUI sources (package.json) not found in $WebSource" 'ERROR'
    exit 1
}
$tools = Resolve-NodeTools
if (-not $tools) {
    Log "Node.js $MinNodeMajor+ with npm is not installed; cannot build the WebGUI from source" 'WARN'
    exit 3
}
Log "building WebGUI with Node.js $($tools.NodeVersion) ($($tools.Node))"

Push-Location $WebSource
try {
    Invoke-Npm -Tools $tools -Arguments @('ci', '--include=dev', '--no-audit', '--no-fund')
    if (Test-Path -LiteralPath $DistDir) { Remove-Item -LiteralPath $DistDir -Recurse -Force }
    Invoke-Npm -Tools $tools -Arguments @('run', 'build') -NodeEnv 'production'
} catch {
    Log $_.Exception.Message 'ERROR'
    exit 1
} finally {
    Pop-Location
}

foreach ($name in @('index.html', 'overlay.html', 'VERSION')) {
    if (-not (Test-Path -LiteralPath (Join-Path $DistDir $name) -PathType Leaf)) {
        Log "build output is missing $name in $DistDir" 'ERROR'
        exit 1
    }
}
$builtVersion = ([System.IO.File]::ReadAllText((Join-Path $DistDir 'VERSION'))).Trim()
if ($ExpectedVersion -and $builtVersion -cne $ExpectedVersion) {
    Log "build output VERSION '$builtVersion' does not match '$ExpectedVersion'" 'ERROR'
    exit 1
}
Log "WebGUI build verified ($builtVersion)" 'OK'
exit 0
