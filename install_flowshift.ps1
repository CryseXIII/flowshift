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
# The WebGUI is a mandatory part of FlowShift: this installer always installs or
# replaces Program Files\FlowShift\webgui from the prebuilt, version-stamped
# webgui\dist (no Node.js needed). Only a checkout without a matching dist is
# rebuilt through the internal helper install_webgui.ps1, and only if Node.js is
# already installed.
#
# Installing over an older installation is an in-place upgrade in one run: a
# running FlowShift is stopped and started again, old program and WebGUI files
# are replaced, ProgramData\FlowShift (config.json, clipboard history, logs) is
# kept. Apart from the Windows UAC prompt the installer asks nothing; pass
# -Interactive to get confirmation questions and a final "press Enter".
#
# Safety: the run holds the FlowShift install lock (no update or second
# installation at the same time), copies and checks the new program files in
# Program Files\FlowShift.staging first and only then swaps the program folder.
# The previous folder stays as FlowShift.previous until the new version runs
# and is moved back if anything fails.
#
# USB package (Viktor 24.11.): a folder unpacked from FlowShift-<version>-usb.zip
# (Settings > Software Update on the development PC) carries the folder
# offline with the python.org installer, pip and the hash-locked dependency
# wheels, plus the prebuilt webgui/dist. Such a folder installs without Git,
# Node.js, Forgejo and internet (only a missing WebView2 runtime still needs a
# download), and the update channel of the installed FlowShift is set to stable.
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
    [switch]$Interactive,
    [switch]$FlowUpdate
)

$ErrorActionPreference = 'Stop'
if ($FlowUpdate) { $NonInteractive = $true }
# Questions only on explicit request; Setup.exe and the updater pass -NonInteractive.
$Ask = [bool]($Interactive -and -not $NonInteractive)
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
$WebGuiDir   = Join-Path $InstallDir 'webgui'
$ApiBase     = 'http://127.0.0.1:5000'
$TotalSteps  = 17
$MinPythonMinor = 11
$MaxPythonMinor = 15
$PythonVersion = '3.15.1'
$PythonInstallerUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
$PythonInstallerSha256 = '8b23839c6b2f3076fdff44c78d219a5393c7205f666cd95bd0c9a562bf8ea5db'
# Size of that file in bytes; only for the error message on a wrong download.
$PythonInstallerSize = 45555248
$VersionPath = Join-Path $RepoDir 'VERSION'
# New program files are copied and checked here first; only then is the
# program folder replaced (21.11.: the folder was emptied before the source
# turned out to be gone). A manual run keeps the replaced folder as
# FlowShift.previous until the new version runs and puts it back on a failure;
# during an update the update runner keeps FlowShift.rollback-<version>.
$StagingDir  = Join-Path (Split-Path -Parent $InstallDir) 'FlowShift.staging'
$PreviousDir = Join-Path (Split-Path -Parent $InstallDir) 'FlowShift.previous'
$UpdatesDir  = Join-Path $DataDir 'updates'
# Read once by the FlowShift that this run starts: it waits for its next
# regular update check instead of updating right away.
$ManualInstallMarker = Join-Path $UpdatesDir 'manual_install.json'
$UpdateStatePath = Join-Path $DataDir 'update_state.json'
# Offline files of a USB package (see the header); $null for a normal folder.
$OfflineDir = Join-Path $RepoDir 'offline'
$UsbManifestPath = Join-Path $OfflineDir 'usb_package.json'
$script:UsbPackage = $null
# How long a manual run waits for a running update or installation.
$LockWaitSec = 1200
# Files without which the copied program cannot run; checked in the source
# and again in the staged copy before the program folder is touched.
$RequiredProgramFiles = @(
    'VERSION', 'requirements.txt', 'update_flowshift.ps1', 'uninstall_flowshift.ps1', 'uninstall_flowshift.bat',
    'src\python\tray.py', 'src\python\web_api.py', 'src\python\version.py', 'src\python\install_lock.py',
    'src\python\lan_scan.py', 'src\python\event_log.py', 'src\python\autostart.py',
    'src\python\overlay_host.py', 'src\python\overlay_controller.py', 'src\python\overlay_protocol.py',
    'src\python\overlay_geometry.py', 'src\python\overlay_native_wheel.py', 'src\python\wheel_gesture.py',
    'src\python\action_handlers.py', 'src\python\input_trigger.py', 'src\python\remote_preview.py',
    'src\python\peer_update.py', 'src\python\peer_discovery.py', 'src\python\peer_reach.py',
    'src\python\preview_capture.py', 'src\python\runtime_singleton.py', 'src\python\flowshift.ico'
)

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

# The probe runs as its own process (System.Diagnostics.Process), not via
# "& $py -c ... 2>&1": Windows PowerShell 5.1 strips the double quotes of such an
# argument (the old f-string probe reached Python as a SyntaxError) and, with
# $ErrorActionPreference = 'Stop', turns any stderr line into a terminating
# error that the old code swallowed. The probe text therefore contains no
# quotes or backslashes; it reaches Python unchanged however it is passed.
# -I ignores PYTHONHOME/PYTHONPATH/user site; stdin is closed so nothing can
# wait for input; every probe has a time limit.
$PythonProbeCode = 'import sys,struct;v=sys.version_info;print(v[0],v[1],v[2],struct.calcsize(chr(80))*8,sys.executable,sep=chr(124))'
$PythonProbeArguments = "-I -c `"$PythonProbeCode`""
$PythonProbeTimeoutSec = 20
$PythonDownloadTimeoutSec = 300
$PythonSetupTimeoutSec = 600

function Invoke-PythonProbe {
    param([string]$Exe, [string]$Arguments = $PythonProbeArguments, [int]$TimeoutSec = $PythonProbeTimeoutSec)
    $result = [pscustomobject]@{ Exe = $Exe; ExitCode = $null; Output = ''; Error = ''; TimedOut = $false; Seconds = 0.0 }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $process = $null
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $Exe
        $psi.Arguments = $Arguments
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardInput = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        foreach ($name in @('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'VIRTUAL_ENV')) {
            if ($psi.EnvironmentVariables.ContainsKey($name)) { $psi.EnvironmentVariables.Remove($name) }
        }
        $process = [System.Diagnostics.Process]::Start($psi)
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        try { $process.StandardInput.Close() } catch { }
        if ($process.WaitForExit($TimeoutSec * 1000)) {
            $process.WaitForExit()
            $result.ExitCode = $process.ExitCode
        } else {
            $result.TimedOut = $true
            try { $process.Kill() } catch { }
        }
        if ($stdout.Wait(2000)) { $result.Output = ([string]$stdout.Result).Trim() }
        if ($stderr.Wait(2000)) { $result.Error = ([string]$stderr.Result).Trim() }
    } catch {
        $result.Error = "could not start: $($_.Exception.Message)"
    } finally {
        if ($process) { $process.Dispose() }
        $result.Seconds = [math]::Round($watch.Elapsed.TotalSeconds, 1)
    }
    return $result
}

function ConvertTo-LogText {
    param([string]$Text, [int]$Max = 300)
    $flat = ([string]$Text -replace '\s*[\r\n]+\s*', ' / ').Trim()
    if ($flat.Length -gt $Max) { $flat = $flat.Substring(0, $Max) + '...' }
    if (-not $flat) { return '(none)' }
    return $flat
}

function Get-PythonCandidates {
    # Explicit places only, no recursive search through Program Files.
    $candidates = @()
    foreach ($cmdName in @('py', 'python', 'python3')) {
        try {
            foreach ($cmd in @(Get-Command $cmdName -All -CommandType Application -ErrorAction SilentlyContinue)) {
                if ($cmd.Source) { $candidates += $cmd.Source }
            }
        } catch { }
    }
    if ($env:SystemRoot) { $candidates += (Join-Path $env:SystemRoot 'py.exe') }
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Launcher\py.exe') }
    # PEP 514 registrations written by the python.org installers.
    foreach ($root in @('HKLM:\SOFTWARE\Python\PythonCore', 'HKCU:\SOFTWARE\Python\PythonCore', 'HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore')) {
        try {
            foreach ($key in @(Get-ChildItem -Path $root -ErrorAction SilentlyContinue)) {
                $installKey = Join-Path $key.PSPath 'InstallPath'
                $props = Get-ItemProperty -Path $installKey -ErrorAction SilentlyContinue
                if (-not $props) { continue }
                if ($props.ExecutablePath) { $candidates += [string]$props.ExecutablePath }
                elseif ($props.'(default)') { $candidates += (Join-Path ([string]$props.'(default)') 'python.exe') }
            }
        } catch { }
    }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Programs\Python' }))) {
        if (-not $base -or -not (Test-Path -LiteralPath $base)) { continue }
        foreach ($dir in @(Get-ChildItem -LiteralPath $base -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue)) {
            $candidates += (Join-Path $dir.FullName 'python.exe')
        }
    }
    $installPrefix = ([string]$InstallDir).TrimEnd('\') + '\'
    $unique = @()
    foreach ($c in $candidates) {
        if (-not $c) { continue }
        # The FlowShift venv is replaced by this installer; never build on it.
        if (([string]$c).StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) { continue }
        if (-not (Test-Path -LiteralPath $c -PathType Leaf)) { continue }
        if (@($unique | Where-Object { $_ -ieq $c }).Count -eq 0) { $unique += $c }
    }
    return $unique
}

function Test-PythonProbeResult {
    # Turns a probe result into a verdict. Output format: 3|13|2|64|<sys.executable>
    param($Probe)
    if ($Probe.TimedOut) { return [pscustomobject]@{ Usable = $false; Reason = "no answer within $PythonProbeTimeoutSec s (stopped)" } }
    if ($null -eq $Probe.ExitCode) { return [pscustomobject]@{ Usable = $false; Reason = 'could not be started' } }
    if ($Probe.ExitCode -ne 0) { return [pscustomobject]@{ Usable = $false; Reason = "exit code $($Probe.ExitCode)" } }
    $line = @(([string]$Probe.Output) -split "`r?`n" | Where-Object { $_ -match '^\d+\|' } | Select-Object -Last 1)
    if ($line.Count -eq 0 -or $line[0] -notmatch '^(\d+)\|(\d+)\|(\d+)\|(\d+)\|(.+)$') {
        return [pscustomobject]@{ Usable = $false; Reason = 'unexpected output' }
    }
    $major = [int]$Matches[1]; $minor = [int]$Matches[2]; $micro = [int]$Matches[3]; $bits = [int]$Matches[4]
    $exe = $Matches[5].Trim()
    $version = [version]("{0}.{1}.{2}" -f $major, $minor, $micro)
    if ($bits -ne 64) { return [pscustomobject]@{ Usable = $false; Reason = "Python $version is $bits-bit, 64-bit is required"; Version = $version } }
    if ($major -ne 3 -or $minor -lt $MinPythonMinor -or $minor -gt $MaxPythonMinor) {
        return [pscustomobject]@{ Usable = $false; Reason = "Python $version is outside the supported 3.$MinPythonMinor-3.$MaxPythonMinor"; Version = $version }
    }
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { $exe = $Probe.Exe }
    return [pscustomobject]@{ Usable = $true; Reason = "usable Python $version 64-bit"; Version = $version; Exe = $exe }
}

function Resolve-PythonTool {
    # Probes every candidate and logs path, exit code and output of each one, so
    # a failed search is explainable from install.log.
    $candidates = @(Get-PythonCandidates)
    if ($candidates.Count -eq 0) {
        Log 'no Python candidates found (py/python on PATH, PEP 514 registry, Program Files\Python3*, LocalAppData\Programs\Python\Python3*)' 'INFO'
        return $null
    }
    Log ("checking {0} Python candidate(s), at most {1} s each" -f $candidates.Count, $PythonProbeTimeoutSec) 'INFO'
    $compatible = @()
    foreach ($py in $candidates) {
        $probe = Invoke-PythonProbe -Exe $py
        $verdict = Test-PythonProbeResult $probe
        $exit = if ($probe.TimedOut) { 'timeout' } elseif ($null -eq $probe.ExitCode) { 'none' } else { [string]$probe.ExitCode }
        $level = if ($verdict.Usable) { 'OK' } else { 'INFO' }
        Log ("python candidate {0}: exit={1} time={2}s stdout={3} stderr={4} -> {5}" -f $py, $exit, $probe.Seconds, (ConvertTo-LogText $probe.Output), (ConvertTo-LogText $probe.Error), $verdict.Reason) $level
        if ($verdict.Usable -and @($compatible | Where-Object { $_.Exe -ieq $verdict.Exe }).Count -eq 0) {
            $compatible += [pscustomobject]@{ Exe = $verdict.Exe; Version = "Python $($verdict.Version)"; ParsedVersion = $verdict.Version; MajorMinor = $verdict.Version.Minor; Candidate = $py }
        }
    }
    return ($compatible | Sort-Object ParsedVersion -Descending | Select-Object -First 1)
}

function Invoke-NativeCapture {
    # Runs a console program and returns exit code and combined output as text.
    # Windows PowerShell 5.1 would turn each stderr line (pip warnings, for
    # example) into a terminating error under $ErrorActionPreference = 'Stop'.
    param([string]$FilePath, [string[]]$ArgumentList)
    $ErrorActionPreference = 'Continue'
    $lines = @(& $FilePath @ArgumentList 2>&1 | ForEach-Object { [string]$_ })
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = ($lines -join "`n") }
}

function Read-UsbPackage {
    # Returns the checked offline part of a USB package, $null for a normal
    # folder. Every listed file must have the SHA-256 from usb_package.json;
    # a damaged copy stops the run before anything is changed.
    if (-not (Test-Path -LiteralPath $UsbManifestPath -PathType Leaf)) { return $null }
    try {
        $manifest = [System.IO.File]::ReadAllText($UsbManifestPath) | ConvertFrom-Json
    } catch {
        Fail "the USB package is damaged ($UsbManifestPath is unreadable). Copy the ZIP to the stick again and unpack it once more."
    }
    if ([string]$manifest.version -cne $ProductVersion) {
        Fail "the offline files of this USB package belong to FlowShift $($manifest.version), the program is $ProductVersion. Unpack the ZIP into an empty folder and try again."
    }
    if ([string]$manifest.python.version -cne $PythonVersion -or [string]$manifest.python.sha256 -cne $PythonInstallerSha256) {
        Fail 'the Python installer of this USB package does not match this installer. Build the USB package again.'
    }
    $files = @([pscustomobject]@{ file = [string]$manifest.python.file; sha256 = [string]$manifest.python.sha256 })
    $files += @($manifest.pip.files)
    # requirements.txt plus the SHA-256 of the wheels the build made from
    # source (proxy-tools exists on PyPI only as source); checked like the rest.
    $offlineRequirements = $null
    if ($manifest.requirements -and [string]$manifest.requirements.file) {
        $files += @([pscustomobject]@{ file = [string]$manifest.requirements.file; sha256 = [string]$manifest.requirements.sha256 })
        $offlineRequirements = Join-Path $OfflineDir ([string]$manifest.requirements.file)
    }
    $wheelDirs = @{}
    foreach ($set in @($manifest.wheel_sets.PSObject.Properties)) {
        $files += @($set.Value)
        $first = [string](@($set.Value)[0].file)
        $wheelDirs[[string]$set.Name] = Join-Path $OfflineDir (Split-Path -Parent ($first.Replace('/', '\')))
    }
    foreach ($entry in $files) {
        $relative = [string]$entry.file
        if (-not $relative -or @($relative.Split('/') | Where-Object { $_ -eq '..' }).Count -gt 0 -or
                $relative.Contains(':') -or $relative.StartsWith('/')) {
            Fail "the USB package lists an invalid file ($relative). Build the USB package again."
        }
        $path = Join-Path $OfflineDir ($relative.Replace('/', '\'))
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            Fail "the USB package is incomplete (offline/$relative is missing). Copy the ZIP to the stick again and unpack it once more."
        }
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -cne [string]$entry.sha256) {
            Fail "the USB package is damaged (offline/$relative has the wrong SHA-256). Copy the ZIP to the stick again and unpack it once more."
        }
    }
    $sets = @($wheelDirs.Keys | Sort-Object)
    Log ("USB package {0}: offline Python {1}, pip and Python packages for Python {2} checked ({3} files); nothing is downloaded from Forgejo, python.org or PyPI" -f $ProductVersion, $PythonVersion, ($sets -join ', '), $files.Count) 'OK'
    return [pscustomobject]@{
        PythonInstaller = Join-Path $OfflineDir ([string]$manifest.python.file)
        PipDir = Join-Path (Join-Path $OfflineDir 'wheels') 'common'
        PipVersion = [string]$manifest.pip.version
        WheelDirs = $wheelDirs
        Requirements = $offlineRequirements
    }
}

function Get-UsbWheelDir {
    # Folder with the dependency wheels of the USB package for this Python, else $null.
    param([object]$PyInfo)
    if (-not $script:UsbPackage -or -not $PyInfo) { return $null }
    $key = '3.{0}' -f $PyInfo.ParsedVersion.Minor
    if ($script:UsbPackage.WheelDirs.ContainsKey($key)) { return $script:UsbPackage.WheelDirs[$key] }
    return $null
}

function Get-ReusableVenv {
    # Updates between PCs that already have FlowShift (Viktor 25.11.): the
    # Python packages are only installed again when requirements.txt changed.
    # Returns the .venv of the previous installation when its requirements.txt
    # is byte for byte the new one and it was built on the same Python; else
    # $null (then a fresh venv is created and filled as before). Candidates:
    # the copy the update runner keeps for the rollback
    # (FLOWSHIFT_PREVIOUS_INSTALL) and FlowShift.previous of a manual run.
    param([object]$PyInfo)
    if ($script:UsbPackage -or -not $PyInfo) { return $null }
    $newReq = Join-Path $InstallDir 'requirements.txt'
    if (-not (Test-Path -LiteralPath $newReq -PathType Leaf)) { return $null }
    $newHash = (Get-FileHash -LiteralPath $newReq -Algorithm SHA256).Hash
    $pythonHome = (Split-Path -Parent ([string]$PyInfo.Exe)).TrimEnd('\')
    $candidates = @()
    if ($env:FLOWSHIFT_PREVIOUS_INSTALL) { $candidates += [string]$env:FLOWSHIFT_PREVIOUS_INSTALL }
    if ($script:PreviousMovedAside) { $candidates += $PreviousDir }
    foreach ($dir in $candidates) {
        $oldReq = Join-Path $dir 'requirements.txt'
        $oldVenv = Join-Path $dir '.venv'
        $cfg = Join-Path $oldVenv 'pyvenv.cfg'
        if (-not (Test-Path -LiteralPath $oldReq -PathType Leaf) -or -not (Test-Path -LiteralPath $cfg -PathType Leaf) -or
                -not (Test-Path -LiteralPath (Join-Path $oldVenv 'Scripts\python.exe') -PathType Leaf) -or
                -not (Test-Path -LiteralPath (Join-Path $oldVenv 'Scripts\pythonw.exe') -PathType Leaf)) { continue }
        if ((Get-FileHash -LiteralPath $oldReq -Algorithm SHA256).Hash -ne $newHash) {
            Log "requirements.txt changed since the previous version; the Python packages are installed again" 'INFO'
            return $null
        }
        $homeLine = @(Get-Content -LiteralPath $cfg -ErrorAction SilentlyContinue | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1)
        if ($homeLine.Count -eq 0) { continue }
        $venvHome = ([string]$homeLine[0] -replace '^\s*home\s*=\s*', '').Trim().TrimEnd('\')
        if ($venvHome -ine $pythonHome) {
            Log "the previous venv was built on $venvHome, this installation uses $pythonHome; the Python packages are installed again" 'INFO'
            continue
        }
        return $oldVenv
    }
    return $null
}

function Install-Python {
    param([string]$Channel = 'LatestStable')
    $installer = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"
    $setupLog = Join-Path $LogDir 'python-setup.log'
    $bundled = [bool]($script:UsbPackage -and (Test-Path -LiteralPath $script:UsbPackage.PythonInstaller -PathType Leaf))
    try {
        if ($bundled) {
            # USB package: the same official installer, checked again below.
            $installer = $script:UsbPackage.PythonInstaller
            Log "using python-$PythonVersion-amd64.exe from the USB package (no download)" 'OK'
        } else {
            Log "downloading python-$PythonVersion-amd64.exe from python.org (at most $PythonDownloadTimeoutSec s)" 'INFO'
            Enable-Tls12
            $watch = [Diagnostics.Stopwatch]::StartNew()
            $previousProgress = $ProgressPreference
            # The Windows PowerShell progress bar slows Invoke-WebRequest down a lot.
            $ProgressPreference = 'SilentlyContinue'
            try {
                Invoke-WebRequest -UseBasicParsing -Uri $PythonInstallerUrl -OutFile $installer -TimeoutSec $PythonDownloadTimeoutSec
            } finally {
                $ProgressPreference = $previousProgress
            }
            $sizeMb = [math]::Round((Get-Item -LiteralPath $installer).Length / 1MB, 1)
            Log ("download finished: {0} MB in {1:N0} s" -f $sizeMb, $watch.Elapsed.TotalSeconds) 'OK'
        }
        $actualHash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -cne $PythonInstallerSha256) {
            $actualSize = (Get-Item -LiteralPath $installer).Length
            throw "Python installer SHA-256 mismatch: expected $PythonInstallerSha256 ($PythonInstallerSize bytes), got $actualHash ($actualSize bytes)"
        }
        Log 'SHA-256 of the Python installer verified' 'OK'
        Log "running the Python $PythonVersion setup silently (at most $PythonSetupTimeoutSec s, setup log: $setupLog)" 'INFO'
        $process = Start-Process -FilePath $installer -ArgumentList @(
            '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_test=0', '/log', "`"$setupLog`""
        ) -PassThru
        $watch = [Diagnostics.Stopwatch]::StartNew()
        while (-not $process.WaitForExit(15000)) {
            if ($watch.Elapsed.TotalSeconds -ge $PythonSetupTimeoutSec) {
                try { $process.Kill() } catch { }
                throw "the Python setup did not finish within $PythonSetupTimeoutSec s and was stopped (see $setupLog)"
            }
            Log ("Python setup still running ({0:N0} s)" -f $watch.Elapsed.TotalSeconds) 'INFO'
        }
        $process.WaitForExit()
        if ($process.ExitCode -eq 3010) {
            Log 'Python setup finished and asks for a reboot later; continuing' 'WARN'
        } elseif ($process.ExitCode -ne 0) {
            throw "Python installer failed with exit code $($process.ExitCode) (see $setupLog)"
        }
        $displayName = "Python $PythonVersion (64-bit)"
        return [pscustomobject]@{
            Method = 'installer'
            PackageId = $null
            UninstallString = Get-InstalledAppUninstallString $displayName
        }
    } finally {
        if (-not $bundled) { Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue }
    }
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
    if ($Interactive) { $argList += '-Interactive' }
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
# --- Install lock (same functions as in update_flowshift.ps1) ---------------
# One lock for every way FlowShift is installed or removed: this installer,
# update_flowshift.ps1, the updater inside the running program and the
# uninstaller. ProgramData\FlowShift\updates\install.lock is held open for the
# whole run and shared for reading only; Windows closes it when the process
# ends, so a crashed run never leaves a stale lock.
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
function Compare-FlowShiftVersion {
    # -1/0/1 by SemVer precedence (dev.10 > dev.9, 0.7.0 > 0.7.0-dev.N);
    # $null when one side is not a version.
    param([string]$Left, [string]$Right)
    $pattern = '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$'
    if ($Left -notmatch $pattern) { return $null }
    $l = @([uint64]$Matches[1], [uint64]$Matches[2], [uint64]$Matches[3]); $lPre = $Matches[4]
    if ($Right -notmatch $pattern) { return $null }
    $r = @([uint64]$Matches[1], [uint64]$Matches[2], [uint64]$Matches[3]); $rPre = $Matches[4]
    for ($i = 0; $i -lt 3; $i++) {
        if ($l[$i] -lt $r[$i]) { return -1 }
        if ($l[$i] -gt $r[$i]) { return 1 }
    }
    if (-not $lPre -and -not $rPre) { return 0 }
    if (-not $lPre) { return 1 }
    if (-not $rPre) { return -1 }
    $lIds = $lPre.Split('.'); $rIds = $rPre.Split('.')
    for ($i = 0; $i -lt [Math]::Min($lIds.Count, $rIds.Count); $i++) {
        $a = $lIds[$i]; $b = $rIds[$i]
        if ($a -ceq $b) { continue }
        $aNum = $a -match '^[0-9]+$'; $bNum = $b -match '^[0-9]+$'
        if ($aNum -and $bNum) { if ([uint64]$a -lt [uint64]$b) { return -1 } else { return 1 } }
        if ($aNum) { return -1 }
        if ($bNum) { return 1 }
        if ([string]::CompareOrdinal($a, $b) -lt 0) { return -1 } else { return 1 }
    }
    if ($lIds.Count -lt $rIds.Count) { return -1 }
    if ($lIds.Count -gt $rIds.Count) { return 1 }
    return 0
}
function Get-KnownNewerVersion {
    # The newest FlowShift version this PC knows of (installed now or found by
    # the last update check) when it is newer than this checkout; else $null.
    $candidates = @()
    if ($script:PreviousVersion) { $candidates += $script:PreviousVersion }
    try {
        if (Test-Path -LiteralPath $UpdateStatePath -PathType Leaf) {
            $state = [System.IO.File]::ReadAllText($UpdateStatePath) | ConvertFrom-Json
            if ($state.latest_version -is [string]) { $candidates += [string]$state.latest_version }
        }
    } catch { }
    $newest = $null
    foreach ($candidate in $candidates) {
        if ((Compare-FlowShiftVersion $candidate $ProductVersion) -ne 1) { continue }
        if ($null -eq $newest -or (Compare-FlowShiftVersion $candidate $newest) -eq 1) { $newest = $candidate }
    }
    return $newest
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
function Close-InstallerWindow {
    param([int]$Seconds)
    if ($Ask) { Read-Host 'Press Enter to close' | Out-Null; return }
    if ($NonInteractive) { return }
    # Leave the result readable without asking anything.
    Write-Host "This window closes automatically in $Seconds seconds."
    Start-Sleep -Seconds $Seconds
}
$script:RuntimeStopped = $false
# Set right before the first file of the existing installation is replaced.
# Until then a failure leaves the previous installation exactly as it was.
$script:InstallationChanged = $false
$script:PreviousVersion = $null
function Get-RunningFlowShiftVersion {
    param([int]$TimeoutSec = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
        try {
            $response = Invoke-WebRequest -Uri "$ApiBase/api/status" -UseBasicParsing -TimeoutSec 3
            $status = [string]$response.Content | ConvertFrom-Json
            if ($status.app_version) { return [string]$status.app_version }
        } catch { }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    return $null
}
$script:PreviousMovedAside = $false
function Stop-ProcessesInInstallDir {
    # Leftovers of the failed new version (pip, venv python, overlay host).
    $rootPattern = '(?i)' + [regex]::Escape(([string]$InstallDir).TrimEnd('\')) + '(?=[\\/"\s]|$)'
    try {
        foreach ($process in @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)) {
            if ([int]$process.ProcessId -eq $PID) { continue }
            if ([string]$process.ExecutablePath -match $rootPattern -or [string]$process.CommandLine -match $rootPattern) {
                Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
            }
        }
    } catch { }
    Start-Sleep -Milliseconds 500
}
function Move-InstallDirAside {
    # Manual run: the previous installation becomes FlowShift.previous. A busy
    # folder ("access denied") is retried after stopping what still runs in it.
    for ($attempt = 1; ; $attempt++) {
        try {
            [System.IO.Directory]::Move($InstallDir, $PreviousDir)
            return
        } catch {
            if ($attempt -ge 5) { throw }
            Log "moving the program folder aside failed (attempt $attempt of 5): $($_.Exception.Message)" 'WARN'
            Stop-ProcessesInInstallDir
            Start-Sleep -Seconds 1
        }
    }
}
function Restore-PreviousInstallation {
    # Manual run after the program folder was replaced: remove the failed new
    # files and move FlowShift.previous back. Task, shortcuts, environment and
    # WebGUI use the same paths, so the previous version runs as before.
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
    Stop-ProcessesInInstallDir
    if (Test-Path -LiteralPath $InstallDir) {
        Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction Stop
    }
    [System.IO.Directory]::Move($PreviousDir, $InstallDir)
    $script:PreviousMovedAside = $false
    try {
        Set-ItemProperty -Path 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FlowShift' -Name 'DisplayVersion' -Value $script:PreviousVersion -ErrorAction Stop
    } catch { }
    Log "previous installation $($script:PreviousVersion) moved back from $PreviousDir" 'OK'
}
function Get-FailureStateNote {
    # One sentence for the end of a failed run: what happened to the old installation.
    if ($script:InstallationChanged) {
        if ($FlowUpdate) {
            return 'Program files were already being replaced when the error happened; the update runner puts the previous version back.'
        }
        if (-not $script:PreviousMovedAside) {
            return 'Program files were already being replaced when the error happened. Run install_flowshift.bat again to finish the installation.'
        }
        try {
            Restore-PreviousInstallation
        } catch {
            Log "could not put the previous installation back: $($_.Exception.Message)" 'ERROR'
            return "Program files were already being replaced when the error happened and the previous version could not be put back automatically; it is kept in $PreviousDir. Run install_flowshift.bat again."
        }
        $script:RuntimeStopped = $true
        $note = "The previous FlowShift $($script:PreviousVersion) was put back"
    } elseif (-not $script:PreviousVersion) {
        return 'Nothing was installed or changed.'
    } else {
        $note = "Nothing was changed: FlowShift $($script:PreviousVersion) is still installed as before"
    }
    if ($FlowUpdate) { return "$note; the update runner takes care of restarting it." }
    if (-not $script:RuntimeStopped) { return "$note." }
    try {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    } catch {
        Log "could not restart the FlowShift autostart task: $($_.Exception.Message)" 'WARN'
        return "$note, but it could not be started again automatically. Log off and on again (or start FlowShift from the desktop shortcut)."
    }
    Log 'restarted the FlowShift autostart task after the failure' 'WARN'
    $running = Get-RunningFlowShiftVersion -TimeoutSec 30
    if ($running) { return "$note and is running again (version $running)." }
    return "$note and was started again; it did not answer within 30 s yet."
}
function Fail {
    param([string]$Message)
    Log $Message 'ERROR'
    $stateNote = Get-FailureStateNote
    if (Test-Path -LiteralPath $StagingDir) {
        Remove-Item -LiteralPath $StagingDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    Log "previous installation: $stateNote" 'WARN'
    Write-Host ''
    Write-Host '================ INSTALLATION FAILED ================' -ForegroundColor Red
    Write-Host "Reason : $Message" -ForegroundColor Red
    Write-Host "Log    : $InstallLog" -ForegroundColor Red
    Write-Host "Status : $stateNote" -ForegroundColor Yellow
    Write-Host '====================================================' -ForegroundColor Red
    Write-Host ''
    Close-InstallerWindow 120
    exit 1
}

try {
    $ProductVersion = Get-ProductVersion -Path $VersionPath
    Log "product version: $ProductVersion" 'OK'
} catch {
    Fail $_.Exception.Message
}

# --- Never two installations at once -----------------------------------------
# Taken before anything is checked or changed and held until this process
# ends. A manual run waits for a running update; an update run inherits the
# lock of its update runner (FLOWSHIFT_INSTALL_LOCK_TOKEN) and stops without
# changes when somebody else holds it.
$script:InstallLock = $null
try {
    if ($FlowUpdate) {
        $script:InstallLock = Enter-InstallLock -DataDir $DataDir -Owner 'update' -Version $ProductVersion -TimeoutSec 0
        if ($null -eq $script:InstallLock) {
            $holder = Read-InstallLockOwner $DataDir
            $runnerToken = [string]$env:FLOWSHIFT_INSTALL_LOCK_TOKEN
            if ($runnerToken -and $null -ne $holder -and [string]$holder.token -ceq $runnerToken) {
                Log 'running under the install lock of the update runner' 'OK'
            } else {
                Fail "another FlowShift installation is running ($(Format-InstallLockOwner $holder)); this update run stops before changing anything"
            }
        }
    } else {
        $onWait = {
            param($Owner)
            Log ("another FlowShift installation or update is running ({0}); waiting until it has finished (up to {1} minutes), nothing is changed meanwhile" -f
                (Format-InstallLockOwner $Owner), [int]($LockWaitSec / 60)) 'WARN'
        }
        $script:InstallLock = Enter-InstallLock -DataDir $DataDir -Owner 'install' -Version $ProductVersion -TimeoutSec $LockWaitSec -OnWait $onWait
        if ($null -eq $script:InstallLock) {
            Fail "another FlowShift installation or update is still running after $([int]($LockWaitSec / 60)) minutes ($(Format-InstallLockOwner (Read-InstallLockOwner $DataDir))). Run install_flowshift.bat again later."
        }
    }
} catch {
    Fail "could not take the FlowShift install lock: $($_.Exception.Message)"
}
if ($null -ne $script:InstallLock) {
    Log 'install lock taken: no update or other installation can start until this run has finished' 'OK'
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

function Stop-FlowShiftOwnedOverlayHosts {
    $rootPrefix = ([string]$InstallDir).TrimEnd('\') + '\'
    $overlayProcesses = @()
    try {
        $overlayProcesses = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop | Where-Object {
            [string]$_.CommandLine -match '(?i)(?:^|[\\/"\s])overlay_host\.py(?:["\s]|$)'
        })
    } catch {
        Log "could not enumerate overlay_host.py processes: $($_.Exception.Message)" 'WARN'
        return
    }
    foreach ($process in $overlayProcesses) {
        $commandLine = [string]$process.CommandLine
        $executable = [string]$process.ExecutablePath
        $owned = $executable.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase) -or
            ($commandLine -match ([regex]::Escape($rootPrefix) + '[^"\r\n]*overlay_host\.py(?:["\s]|$)'))
        if (-not $owned) { continue }
        try {
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction Stop
            Log "stopped leftover overlay host PID $($process.ProcessId)" 'OK'
        } catch {
            Log "could not stop overlay host PID $($process.ProcessId): $($_.Exception.Message)" 'WARN'
        }
    }
}

function Stop-RunningFlowShift {
    # An installation over a running FlowShift is an upgrade: stop it here and
    # start it again at the end. Only processes running from $InstallDir are
    # stopped; anything else (for example a developer checkout) is left alone.
    if (Invoke-FlowShiftShutdown) {
        $script:RuntimeStopped = $true
        Log 'running FlowShift asked to shut down via the control socket' 'OK'
    } else {
        Log 'control socket not reachable; FlowShift was not running or does not answer' 'INFO'
    }
    if (Wait-FlowShiftPortsClosed -TimeoutSec 15) {
        Log 'control/peer ports are free' 'OK'
    } else {
        Log 'ports still in use after the shutdown request; checking owning processes' 'WARN'
        $pids = @()
        foreach ($port in @(45782, 45781, 5000)) {
            try {
                $pids += Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
                    Select-Object -ExpandProperty OwningProcess -Unique
            } catch { }
        }
        $pids = $pids | Where-Object { $_ } | Sort-Object -Unique
        $owned = @()
        foreach ($processId in $pids) {
            $cmd = Get-ProcessCommandLine -ProcessId ([int]$processId)
            $exe = Get-ProcessExecutablePath -ProcessId ([int]$processId)
            if (Test-FlowShiftProcess -ProcessId ([int]$processId) -RootDir $InstallDir) {
                $owned += [pscustomobject]@{ Pid = [int]$processId; CmdLine = $cmd; Exe = $exe }
            } else {
                Log ("leaving PID {0} alone (not under {1}): {2} {3}" -f $processId, $InstallDir, $exe, $cmd) 'WARN'
            }
        }
        if ($owned.Count -gt 0) {
            $stop = $true
            if ($Ask) {
                $owned | ForEach-Object { Write-Host ("  PID {0}: {1}" -f $_.Pid, $_.CmdLine) -ForegroundColor Yellow }
                $stop = ((Read-Host 'Stop these FlowShift processes now? [Y/n]') -notmatch '^[nN]')
            }
            if (-not $stop) { Fail 'FlowShift is still running and was not stopped; close it from the tray and run the installer again' }
            foreach ($m in $owned) {
                try {
                    Stop-Process -Id $m.Pid -Force -ErrorAction Stop
                    $script:RuntimeStopped = $true
                    Log "stopped FlowShift PID $($m.Pid) for the upgrade" 'OK'
                } catch {
                    Log "could not stop PID $($m.Pid): $($_.Exception.Message)" 'WARN'
                }
            }
        }
    }
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
    Stop-FlowShiftOwnedOverlayHosts
    if (-not (Wait-FlowShiftPortsClosed -TimeoutSec 5)) {
        Fail 'FlowShift ports 45781/45782 are still used by another program. Quit FlowShift (tray icon > Exit) or any FlowShift started from a different folder, then run the installer again.'
    }
}

function Test-WebGuiAssets {
    param([string]$Root)
    $missing = @()
    foreach ($name in @('index.html', 'overlay.html')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $name) -PathType Leaf)) { $missing += $name }
    }
    $stamp = $null
    $versionFile = Join-Path $Root 'VERSION'
    if (Test-Path -LiteralPath $versionFile -PathType Leaf) {
        $stamp = ([System.IO.File]::ReadAllText($versionFile)).Trim().TrimStart([char]0xFEFF)
        if (-not $stamp) { $stamp = $null }
    }
    return [pscustomobject]@{ Missing = $missing; Version = $stamp; Complete = ($missing.Count -eq 0) }
}

function Resolve-WebGuiSource {
    # Returns the prebuilt WebGUI to deploy. Prefers webgui\dist stamped with the
    # program VERSION; otherwise builds it via install_webgui.ps1 if Node.js is
    # present. Fails before anything is changed when no usable WebGUI exists.
    $dist = Join-Path $RepoDir 'webgui\dist'
    $assets = Test-WebGuiAssets $dist
    if ($assets.Complete -and $assets.Version -ceq $ProductVersion) {
        Log "prebuilt WebGUI $($assets.Version) found: $dist" 'OK'
        return [pscustomobject]@{ Path = $dist; Version = $assets.Version }
    }
    $reason = if (-not $assets.Complete) { "missing $($assets.Missing -join ', ')" }
              elseif (-not $assets.Version) { 'no version stamp' }
              else { "built for $($assets.Version)" }
    Log "prebuilt WebGUI in $dist does not match $ProductVersion ($reason); trying to build it" 'WARN'
    $helper = Join-Path $RepoDir 'install_webgui.ps1'
    $code = 3
    if ((Test-Path -LiteralPath $helper -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $RepoDir 'webgui\package.json') -PathType Leaf)) {
        $hostExe = (Get-Process -Id $PID).Path
        # Out-Host: the helper's console output must not become part of this
        # function's return value.
        & $hostExe -NoProfile -ExecutionPolicy Bypass -File $helper -WebSource (Join-Path $RepoDir 'webgui') `
            -ExpectedVersion $ProductVersion -LogPath $InstallLog | Out-Host
        $code = $LASTEXITCODE
    }
    $assets = Test-WebGuiAssets $dist
    if ($code -eq 0 -and $assets.Complete -and $assets.Version -ceq $ProductVersion) {
        Log "WebGUI $ProductVersion built from source" 'OK'
        return [pscustomobject]@{ Path = $dist; Version = $assets.Version }
    }
    $why = if ($code -eq 3) { 'Node.js 24+ is not installed to rebuild it' } else { 'rebuilding it failed (see log)' }
    if ($assets.Complete -and -not $assets.Version) {
        # Builds made before the version stamp existed carry no VERSION file.
        Log "using the unversioned prebuilt WebGUI from $dist ($why); stamping it as $ProductVersion" 'WARN'
        return [pscustomobject]@{ Path = $dist; Version = $ProductVersion }
    }
    if ($assets.Complete) {
        Fail "The prebuilt WebGUI in $dist belongs to $($assets.Version), but the program is $ProductVersion, and $why. Use FlowShift-Setup.exe $ProductVersion (it contains the matching WebGUI) or install Node.js 24+ and run install_flowshift.bat again."
    }
    Fail "The WebGUI is missing ($dist lacks $($assets.Missing -join ', ')) and $why. Use FlowShift-Setup.exe $ProductVersion (it contains the WebGUI) or install Node.js 24+ and run install_flowshift.bat again."
}

function Install-WebGuiAssets {
    param([object]$Source)
    # Stage next to the target, verify, then swap: no file of the old WebGUI survives.
    $staging = Join-Path $InstallDir 'webgui.new'
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    Get-ChildItem -LiteralPath $Source.Path -Force | Copy-Item -Destination $staging -Recurse -Force
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText((Join-Path $staging 'VERSION'), "$($Source.Version)`n", $utf8NoBom)
    $icon = Join-Path $RepoDir 'src\python\flowshift.ico'
    if (Test-Path -LiteralPath $icon) { Copy-Item -LiteralPath $icon -Destination (Join-Path $staging 'flowshift.ico') -Force }
    $portConfig = Join-Path $WebGuiDir 'config.json'
    if (Test-Path -LiteralPath $portConfig -PathType Leaf) {
        Copy-Item -LiteralPath $portConfig -Destination (Join-Path $staging 'config.json') -Force
        Log 'kept the existing WebGUI port setting (webgui\config.json)' 'OK'
    }
    $staged = Test-WebGuiAssets $staging
    if (-not $staged.Complete -or $staged.Version -cne $Source.Version) {
        Fail "staged WebGUI is incomplete (missing: $($staged.Missing -join ', '))"
    }
    if (Test-Path -LiteralPath $WebGuiDir) {
        $oldStamp = (Test-WebGuiAssets $WebGuiDir).Version
        $oldCount = @(Get-ChildItem -LiteralPath $WebGuiDir -Recurse -File -ErrorAction SilentlyContinue).Count
        try {
            Remove-Item -LiteralPath $WebGuiDir -Recurse -Force -ErrorAction Stop
        } catch {
            Fail "could not remove the old WebGUI in ${WebGuiDir}: $($_.Exception.Message)"
        }
        $label = if ($oldStamp) { $oldStamp } else { 'unversioned' }
        Log "removed the previous WebGUI ($label, $oldCount files)" 'OK'
    }
    Rename-Item -LiteralPath $staging -NewName 'webgui'
    $installed = Test-WebGuiAssets $WebGuiDir
    if (-not $installed.Complete -or $installed.Version -cne $Source.Version) {
        Fail "installed WebGUI in $WebGuiDir is incomplete (missing: $($installed.Missing -join ', '))"
    }
    Log "WebGUI $($installed.Version) installed to $WebGuiDir (index.html + overlay.html)" 'OK'
}

function Remove-LegacyWebGuiRegistration {
    # The WebGUI used to be a separate "FlowShift Web GUI" app; it is part of FlowShift now.
    $legacyKey = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FlowShift Web GUI'
    if (Test-Path $legacyKey) {
        Remove-Item -Path $legacyKey -Recurse -Force -ErrorAction SilentlyContinue
        Log 'removed the separate "FlowShift Web GUI" entry from Apps & Features' 'OK'
    }
    $startDir = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\FlowShift'
    foreach ($legacy in @(
        (Join-Path $startDir 'Uninstall FlowShift Web GUI.lnk'),
        (Join-Path ([Environment]::GetFolderPath('Desktop')) 'FlowShift Web GUI.lnk')
    )) {
        if (Test-Path -LiteralPath $legacy) { Remove-Item -LiteralPath $legacy -Force -ErrorAction SilentlyContinue }
    }
}

function Invoke-LocalHttp {
    param([string]$Path)
    try {
        $response = Invoke-WebRequest -Uri ($ApiBase + $Path) -UseBasicParsing -TimeoutSec 5
        $body = $response.Content
        if ($body -is [byte[]]) { $body = [Text.Encoding]::UTF8.GetString($body) }
        return [pscustomobject]@{ Status = [int]$response.StatusCode; Body = [string]$body }
    } catch {
        $code = 0
        try { $code = [int]$_.Exception.Response.StatusCode } catch { }
        return [pscustomobject]@{ Status = $code; Body = $_.Exception.Message }
    }
}

function Test-InstalledFlowShift {
    # Returns a list of problems; empty means program and WebGUI match and the
    # running runtime serves index.html and overlay.html.
    param([int]$TimeoutSec = 45)
    $problems = @()
    $status = $null
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
        $response = Invoke-LocalHttp '/api/status'
        if ($response.Status -eq 200) {
            try { $status = $response.Body | ConvertFrom-Json } catch { $status = $null }
            if ($status -and $status.app_version) { break }
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    if (-not $status -or -not $status.app_version) {
        return @("the FlowShift runtime did not answer on $ApiBase within $TimeoutSec s (it starts for the logged-on user $interactiveUser; see $LogDir\flowshift.log)")
    }
    if ([string]$status.app_version -cne $ProductVersion) {
        $problems += "the running runtime reports version $($status.app_version) instead of $ProductVersion (an older FlowShift is still running)"
    }
    $web = $status.webgui
    if (-not $web) {
        $problems += 'the running runtime does not report the WebGUI state (an older FlowShift is still running)'
    } elseif (-not $web.ok) {
        $problems += "WebGUI check of the runtime: $($web.message)"
    }
    $index = Invoke-LocalHttp '/index.html'
    if ($index.Status -ne 200 -or $index.Body -notmatch 'id="root"') {
        $problems += "GET /index.html returned HTTP $($index.Status) instead of the WebGUI dashboard"
    }
    $overlay = Invoke-LocalHttp '/overlay.html'
    if ($overlay.Status -ne 200 -or $overlay.Body -notmatch 'overlay-root') {
        $problems += "GET /overlay.html returned HTTP $($overlay.Status); command wheel and clipboard overlay would not open"
    }
    return $problems
}

function Test-ProgramFiles {
    # Relative paths of required program files missing under $Root.
    param([string]$Root)
    $missing = @()
    foreach ($relative in $RequiredProgramFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $relative) -PathType Leaf)) { $missing += $relative }
    }
    return $missing
}

function Get-SourceFileCount {
    param([string]$Root)
    return @(Get-ChildItem -LiteralPath $Root -Recurse -File -Force -ErrorAction Stop |
        Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' }).Count
}

function New-StagedProgramFiles {
    # Copies the program files into FlowShift.staging and checks the copy:
    # required files present, VERSION matches, as many files under src as in
    # the source. Fails before the program folder is touched.
    if (Test-Path -LiteralPath $StagingDir) { Remove-Item -LiteralPath $StagingDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $StagingDir | Out-Null
    # -LiteralPath: folders with spaces, brackets or other wildcard characters
    # (e.g. "C:\Users\Viktor\Downloads\flowshift [2]") are copied as they are.
    Copy-Item -LiteralPath (Join-Path $RepoDir 'src') -Destination $StagingDir -Recurse -Force
    Copy-Item -LiteralPath $VersionPath -Destination (Join-Path $StagingDir 'VERSION') -Force
    if (Test-Path -LiteralPath (Join-Path $RepoDir 'docs')) {
        Copy-Item -LiteralPath (Join-Path $RepoDir 'docs') -Destination $StagingDir -Recurse -Force
    }
    foreach ($f in @(
        'requirements.txt',
        'README.md',
        'LICENSE',
        'uninstall_flowshift.bat',
        'uninstall_flowshift.ps1',
        'update_flowshift.ps1',
        # Kept in the program folder so Software Update can pack this version
        # as a USB package (usb_package.py).
        'install_flowshift.bat',
        'install_flowshift.ps1',
        'install_webgui.ps1'
    )) {
        $src = Join-Path $RepoDir $f
        if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination $StagingDir -Force }
    }
    $stagedPy = Join-Path $StagingDir 'src\python'
    foreach ($junk in @('config.json','flowshift.log','flowshift_runtime.out')) {
        $p = Join-Path $stagedPy $junk
        if (Test-Path $p) { Remove-Item -Path $p -Force }
    }
    Get-ChildItem -Path $StagingDir -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    $missing = @(Test-ProgramFiles $StagingDir)
    if ($missing.Count -gt 0) {
        Fail "the staged program files are incomplete (missing: $($missing -join ', ')). Nothing was changed."
    }
    $stagedVersion = ([System.IO.File]::ReadAllText((Join-Path $StagingDir 'VERSION'))).Trim().TrimStart([char]0xFEFF)
    if ($stagedVersion -cne $ProductVersion) {
        Fail "the staged VERSION $stagedVersion does not match $ProductVersion. Nothing was changed."
    }
    $sourceCount = Get-SourceFileCount (Join-Path $RepoDir 'src')
    # The staged copy lacks only the local junk files removed above.
    foreach ($junk in @('config.json','flowshift.log','flowshift_runtime.out')) {
        if (Test-Path -LiteralPath (Join-Path $RepoDir "src\python\$junk") -PathType Leaf) { $sourceCount-- }
    }
    $stagedCount = Get-SourceFileCount (Join-Path $StagingDir 'src')
    if ($stagedCount -ne $sourceCount) {
        Fail "the staged program files differ from $RepoDir ($stagedCount of $sourceCount files under src). Nothing was changed."
    }
    Log "new program files $ProductVersion staged and checked in $StagingDir ($stagedCount files under src)" 'OK'
}

Write-Host ''
Write-Host '========================================' -ForegroundColor White
Write-Host '        FlowShift Installer' -ForegroundColor White
Write-Host '========================================' -ForegroundColor White
Log "installer started; repo=$RepoDir install=$InstallDir data=$DataDir WithNssm=$WithNssm"

try {
    # --- Admin -----------------------------------------------------------
    Step 'Checking administrator rights'
    Log 'running elevated' 'OK'
    $interactiveUser = Get-InteractiveUser
    Log "interactive user (autostart target): $interactiveUser"
    $previousVersionPath = Join-Path $InstallDir 'VERSION'
    if (Test-Path -LiteralPath $previousVersionPath -PathType Leaf) {
        $script:PreviousVersion = ([System.IO.File]::ReadAllText($previousVersionPath)).Trim()
        Log "existing installation $($script:PreviousVersion) found; upgrading in place to $ProductVersion (config.json and data are kept)" 'OK'
    } else {
        Log "fresh installation of $ProductVersion" 'OK'
    }
    Log ("installer host: PowerShell {0} ({1})" -f $PSVersionTable.PSVersion, (Get-Process -Id $PID).Path) 'INFO'
    $script:NewerVersion = $null
    if (-not $FlowUpdate) {
        # 21.11.: a manual install from an older checkout started FlowShift,
        # which updated itself again at once while the first update still ran.
        # Now the manual run says so, installs what was asked for, and the
        # automatic update waits for its next regular check.
        $script:NewerVersion = Get-KnownNewerVersion
        if ($script:NewerVersion) {
            Log "this folder contains FlowShift $ProductVersion, but FlowShift $($script:NewerVersion) is already installed or available. Installing $ProductVersion as requested; the automatic update installs $($script:NewerVersion) again at its next regular check (not right after this installation). For the newest version use the newest FlowShift-Setup.exe from the GitHub releases page (or the newest FlowShift folder)." 'WARN'
            if ($Ask -and ((Read-Host "Install the older FlowShift $ProductVersion anyway? [y/N]") -notmatch '^[yYjJ]')) {
                Fail "cancelled: FlowShift $($script:PreviousVersion) stays installed"
            }
        }
    }

    # --- Check and stage the new program files -------------------------------
    # Copied next to the program folder and checked while the old FlowShift
    # keeps running; the program folder is only replaced once the copy is
    # complete.
    Step 'Checking and staging the new program files'
    $missingSource = @(Test-ProgramFiles $RepoDir)
    if ($missingSource.Count -gt 0) {
        Fail "the FlowShift files in $RepoDir are incomplete (missing: $($missingSource -join ', ')). Nothing was changed."
    }
    New-StagedProgramFiles
    $script:UsbPackage = Read-UsbPackage

    # Nothing of the existing installation is touched before the step
    # "Installing application files": the WebGUI build and Python are checked
    # (and Python installed if needed) while the old FlowShift keeps running.

    # --- WebGUI build (checked before anything is changed) ---------------
    Step 'Checking the WebGUI build'
    $webGuiSource = Resolve-WebGuiSource

    $installState = Read-InstallState

    # --- Python ----------------------------------------------------------
    Step 'Checking Python'
    $pyInfo = Resolve-PythonTool
    if ($pyInfo) {
        Log "found Python: $($pyInfo.Exe) [$($pyInfo.Version)] (via $($pyInfo.Candidate))" 'OK'
        $installState.detected_before_install.python = $pyInfo.Exe
    } else {
        Log "no usable Python 3.$MinPythonMinor-3.$MaxPythonMinor (64-bit) found; every checked candidate is listed above with exit code and output" 'WARN'
        $installState.detected_before_install.python = $null
    }
    if ($pyInfo -and $script:UsbPackage -and -not (Get-UsbWheelDir $pyInfo)) {
        # The USB package has no Python packages for this Python; the bundled
        # Python is installed next to it and used for FlowShift instead.
        Log "the USB package has no Python packages for $($pyInfo.Version); installing the bundled Python $PythonVersion for FlowShift instead" 'WARN'
        $pyInfo = $null
    }

    # --- Install Python if missing --------------------------------------
    Step 'Installing Python (if missing)'
    $pythonTargetDir = Join-Path $env:ProgramFiles ('Python' + (($PythonVersion -split '\.')[0..1] -join ''))
    $pythonTargetExe = Join-Path $pythonTargetDir 'python.exe'
    if ($pyInfo -and -not $UpgradePython) {
        Log 'Python already present, using existing Python; nothing is downloaded' 'OK'
        $installState.details.python.install_method = 'existing'
    } elseif (-not $pyInfo) {
        if ($SkipPythonInstall) { Fail 'Python is required but installation is disabled by -SkipPythonInstall' }
        if (-not $InstallPythonIfMissing) { Fail 'Python is required but automatic installation is disabled' }
        if (Test-Path -LiteralPath $pythonTargetExe -PathType Leaf) {
            # Installing the same version again would not change the result.
            Fail "Python $PythonVersion is already installed in $pythonTargetDir, but it did not answer the check (exit code and output are in the log). No second copy is downloaded."
        }
        Log "No usable Python was found. The installer now downloads Python $PythonVersion (64-bit, official python.org installer, SHA-256 checked) and installs it for all users in $pythonTargetDir. This usually takes 1-3 minutes; progress is shown below." 'WARN'
        try {
            $pyInstall = Install-Python -Channel $PythonChannel
            $installState.installed_by_flowshift.python = $true
            $installState.details.python.installed_by_flowshift = $true
            $installState.details.python.install_method = $pyInstall.Method
            $installState.details.python.package_id = $pyInstall.PackageId
            $installState.details.python.uninstall_string = $pyInstall.UninstallString
            Log 'Python installation finished' 'OK'
        } catch {
            Fail "could not install Python automatically: $($_.Exception.Message). Install supported 64-bit CPython 3.$MinPythonMinor-3.$MaxPythonMinor and re-run."
        }
        $pyInfo = Resolve-PythonTool
        if (-not $pyInfo) { Fail "Python $PythonVersion was installed, but it does not answer the check; exit code and output of every candidate are in the log." }
        Log "using the new Python: $($pyInfo.Exe) [$($pyInfo.Version)]" 'OK'
    } elseif ($UpgradePython) {
        try {
            $pyInstall = Install-Python -Channel $PythonChannel
            # An explicit update does not transfer ownership of user-managed Python.
            $installState.installed_by_flowshift.python = $false
            $installState.details.python.installed_by_flowshift = $false
            $installState.details.python.install_method = 'existing-updated'
            $installState.details.python.package_id = $null
        } catch {
            Fail "could not upgrade Python automatically: $($_.Exception.Message)"
        }
        $pyInfo = Resolve-PythonTool
        if (-not $pyInfo) { Fail 'Python still not found after upgrade; exit code and output of every candidate are in the log.' }
        Log 'Python upgrade finished' 'OK'
    }

    $installState.used_tools.python = $pyInfo.Exe
    $installState.versions.python = $pyInfo.Version

    # --- Stop a running FlowShift ----------------------------------------
    Step 'Stopping a running FlowShift (it is started again at the end)'
    Stop-RunningFlowShift
    Write-InstallState $installState

    # --- Copy app files + create venv -----------------------------------
    Step 'Installing application files and creating venv'
    # The staged copy is complete and checked; only now is the program folder
    # replaced, by moving folders instead of overwriting files in place.
    if ($FlowUpdate) {
        # The update runner has moved the previous version aside to
        # FlowShift.rollback-<version> and puts it back on a failure.
        if (Test-Path -LiteralPath $InstallDir) {
            Fail "the program folder $InstallDir still exists although the update runner moves it aside first. Nothing was changed."
        }
    } elseif ((Test-Path -LiteralPath $InstallDir) -and (Test-Path -LiteralPath $PreviousDir) -and
            @(Test-ProgramFiles $InstallDir).Count -gt 0) {
        # An earlier manual run failed and could not put the previous version
        # back: the program folder is incomplete, FlowShift.previous is the
        # good copy and stays the fallback.
        Log "the program folder is incomplete; keeping $PreviousDir as the previous installation" 'WARN'
        Stop-ProcessesInInstallDir
        Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction Stop
        $script:PreviousMovedAside = $true
        try { $script:PreviousVersion = ([System.IO.File]::ReadAllText((Join-Path $PreviousDir 'VERSION'))).Trim() } catch { }
    } elseif (Test-Path -LiteralPath $InstallDir) {
        if (Test-Path -LiteralPath $PreviousDir) {
            # Left by an earlier manual run that ended without cleaning up;
            # the complete program folder is the newer state.
            Remove-Item -LiteralPath $PreviousDir -Recurse -Force -ErrorAction Stop
        }
        try {
            Move-InstallDirAside
        } catch {
            Fail "the program folder could not be moved aside ($($_.Exception.Message)); a program still has it open. Nothing was changed."
        }
        $script:PreviousMovedAside = $true
        Log "previous installation $($script:PreviousVersion) kept in $PreviousDir until the new version runs" 'OK'
    } elseif (Test-Path -LiteralPath $PreviousDir) {
        Log "the program folder is missing; the copy in $PreviousDir is left as it is" 'WARN'
    }
    $script:InstallationChanged = $true
    [System.IO.Directory]::Move($StagingDir, $InstallDir)
    Log "new program files $ProductVersion moved into $InstallDir" 'OK'
    if ($script:PreviousMovedAside) {
        # Kept from the previous installation: the optional NSSM helper and
        # the WebGUI port setting (Install-WebGuiAssets carries it over).
        foreach ($keep in @('tools', 'webgui\config.json')) {
            $from = Join-Path $PreviousDir $keep
            if (Test-Path -LiteralPath $from) {
                $to = Join-Path $InstallDir $keep
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
                Copy-Item -LiteralPath $from -Destination $to -Recurse -Force
            }
        }
    }
    $missingInstalled = @(Test-ProgramFiles $InstallDir)
    if ($missingInstalled.Count -gt 0) {
        Fail "installed program files are incomplete after the move (missing: $($missingInstalled -join ', '))"
    }

    $VenvPy  = Join-Path $VenvDir 'Scripts\python.exe'
    $VenvPyw = Join-Path $VenvDir 'Scripts\pythonw.exe'
    # Same requirements.txt as the previous version: its venv is copied (the
    # previous folder stays untouched for the rollback) and nothing is
    # downloaded. Any doubt means a fresh venv as before.
    $venvReused = $false
    $reuseVenv = Get-ReusableVenv $pyInfo
    if ($reuseVenv) {
        try {
            Log "requirements.txt is unchanged; copying the Python packages from $reuseVenv (no download)"
            Copy-Item -LiteralPath $reuseVenv -Destination $VenvDir -Recurse -Force -ErrorAction Stop
            $checkRun = Invoke-NativeCapture $VenvPy @('-I', '-c', 'import webview, pip')
            if ($checkRun.ExitCode -ne 0) { throw "the copied venv does not start: $(ConvertTo-LogText $checkRun.Output 400)" }
            $venvReused = $true
            Log 'venv of the previous version reused' 'OK'
        } catch {
            Log "reusing the previous venv failed ($($_.Exception.Message)); creating a new one" 'WARN'
            Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    if (-not $venvReused) {
        Log "creating venv at $VenvDir"
        $venvRun = Invoke-NativeCapture $pyInfo.Exe @('-I', '-m', 'venv', $VenvDir)
        if ($venvRun.ExitCode -ne 0) { Fail "venv creation with $($pyInfo.Exe) failed (exit code $($venvRun.ExitCode)): $(ConvertTo-LogText $venvRun.Output 800)" }
        if (-not (Test-Path $VenvPy)) { Fail "venv python not found at $VenvPy" }
        if (-not (Test-Path $VenvPyw)) { Fail "venv pythonw not found at $VenvPyw" }
        Log 'venv created' 'OK'
    }

    # --- Dependencies ----------------------------------------------------
    Step 'Installing Python dependencies'
    # USB package: pip and the dependencies come only from its offline folder.
    $wheelDir = Get-UsbWheelDir $pyInfo
    $offlineArgs = @()
    $pipSourceArgs = @()
    if ($script:UsbPackage) {
        if (-not $wheelDir) { Fail "the USB package has no Python packages for $($pyInfo.Version)" }
        $offlineArgs = @('--no-index', '--find-links', $wheelDir)
        $pipSourceArgs = @('--no-index', '--find-links', $script:UsbPackage.PipDir)
        Log "installing pip and the dependencies from the USB package ($wheelDir), no internet needed" 'INFO'
    }
    $req = Join-Path $InstallDir 'requirements.txt'
    if ($script:UsbPackage -and $script:UsbPackage.Requirements) { $req = $script:UsbPackage.Requirements }
    if ($venvReused) {
        Log 'dependencies unchanged (same requirements.txt); kept from the previous version' 'OK'
    } else {
        $pipRun = Invoke-NativeCapture $VenvPy (@('-m', 'pip', 'install') + $pipSourceArgs + @('pip==26.2'))
        if ($pipRun.ExitCode -ne 0) { Fail "locked pip installation failed: $($pipRun.Output)" }
        if (Test-Path $req) {
            $pipRun = Invoke-NativeCapture $VenvPy (@('-m', 'pip', 'install') + $offlineArgs + @('--require-hashes', '-r', $req))
            if ($pipRun.ExitCode -eq 0) {
                Log 'dependencies installed from requirements.txt' 'OK'
            } else {
                Fail "pip install failed: $($pipRun.Output)"
            }
        } else {
            Log 'no requirements.txt; skipping dependency install' 'OK'
        }
    }

    try {
        $null = Ensure-WebView2Runtime
        Log 'WebView2 is a shared system dependency and will not be removed by FlowShift uninstallers' 'INFO'
    } catch {
        if ($script:UsbPackage) {
            Fail "WebView2 Evergreen is missing on this PC and could not be downloaded ($($_.Exception.Message)). The USB package does not contain it (Windows 11 and current Windows 10 already have it). Connect this PC to the internet once and double-click install_flowshift.bat again."
        }
        Fail "WebView2 Evergreen installation or verification failed: $($_.Exception.Message)"
    }

    $previousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $PyDir
        $smokeRun = Invoke-NativeCapture $VenvPy @('-c', 'import webview; import overlay_host; import overlay_controller; import web_api; import tray')
        if ($smokeRun.ExitCode -ne 0) { Fail "installed overlay import smoke test failed: $($smokeRun.Output)" }
        Log 'installed productive runtime import smoke passed' 'OK'
    } finally {
        if ($null -eq $previousPythonPath) {
            Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONPATH = $previousPythonPath
        }
    }

    $installState.used_tools.python = $VenvPy
    $installState.versions.python = (Invoke-NativeCapture $VenvPy @('--version')).Output
    Write-InstallState $installState

    # --- WebGUI (mandatory part of FlowShift) -------------------------------
    Step 'Installing the WebGUI (replacing the previous one)'
    Install-WebGuiAssets -Source $webGuiSource
    Remove-LegacyWebGuiRegistration

    # --- Config / data folders + machine env vars ---------------------------
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
                rearm_distance_px = 40
                rearm_ms = 400
                edges = [ordered]@{ north = $null; south = $null; east = $null; west = $null }
            }
        }
        $utf8NoBom = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json -Depth 6), $utf8NoBom)
        Log "created fresh config.json (device_id=$devId)" 'OK'
    } else {
        Log 'config.json already exists, keeping it' 'OK'
    }
    if ($script:UsbPackage) {
        # A PC installed from a USB package has no Forgejo access: it follows
        # the published versions on GitHub (stable), every other setting stays.
        $channelRun = Invoke-NativeCapture $VenvPy @('-I', (Join-Path $PyDir 'usb_package.py'), '--stable-channel', $cfgPath)
        if ($channelRun.ExitCode -ne 0) { Fail "could not set the update channel to stable: $(ConvertTo-LogText $channelRun.Output 800)" }
        Log "update channel: stable (GitHub) for an installation from a USB package ($(ConvertTo-LogText $channelRun.Output 200))" 'OK'
    }
    # Machine-wide env via registry (faster than SetEnvironmentVariable which
    # broadcasts WM_SETTINGCHANGE to ALL windows and can hang on some systems).
    $envReg = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
    Set-ItemProperty -Path $envReg -Name 'FLOWSHIFT_CONFIG' -Value $cfgPath -Force
    Set-ItemProperty -Path $envReg -Name 'FLOWSHIFT_LOG_DIR' -Value $LogDir -Force
    Set-ItemProperty -Path $envReg -Name 'FLOWSHIFT_WEBGUI_DIR' -Value $WebGuiDir -Force
    $env:FLOWSHIFT_CONFIG = $cfgPath
    $env:FLOWSHIFT_LOG_DIR = $LogDir
    $env:FLOWSHIFT_WEBGUI_DIR = $WebGuiDir
    Log 'machine env FLOWSHIFT_CONFIG / FLOWSHIFT_LOG_DIR / FLOWSHIFT_WEBGUI_DIR set' 'OK'

    # --- Primary autostart: user-session Scheduled Task ------------------
    Step 'Registering user-session autostart (Scheduled Task, interactive)'
    $trayPy = Join-Path $PyDir 'tray.py'
    # "Auto-start with Windows" off (tray menu or WebGUI, autostart.py) is a
    # disabled logon trigger; the task itself stays enabled so the desktop
    # shortcut and the updater can still start it with schtasks /Run. An
    # update keeps that choice instead of switching autostart back on.
    $keepAutostartOff = $false
    try {
        $previousTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($previousTask) {
            $previousTriggers = @($previousTask.Triggers)
            $keepAutostartOff = ($previousTriggers.Count -eq 0) -or
                (@($previousTriggers | Where-Object { $_.Enabled }).Count -eq 0) -or
                ($previousTask.Settings.Enabled -eq $false)
        }
    } catch { }
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue } catch { }
    $action    = New-ScheduledTaskAction -Execute $VenvPyw -Argument "`"$trayPy`" --tray" -WorkingDirectory $PyDir
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $interactiveUser
    if ($keepAutostartOff) { $trigger.Enabled = $false }
    $principal = New-ScheduledTaskPrincipal -UserId $interactiveUser -LogonType Interactive -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Log "scheduled task '$TaskName' registered (AtLogOn, interactive, highest = no per-start UAC)" 'OK'
    if ($keepAutostartOff) { Log 'Auto-start with Windows stays off as chosen before (logon trigger disabled)' 'OK' }

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

    # --- Optional NSSM helper (NOT the input path) -----------------------
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
            $null = Invoke-NativeCapture $NssmExe @('stop', $ServiceName)
            $null = Invoke-NativeCapture $NssmExe @('remove', $ServiceName, 'confirm')
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

    # --- Shortcuts -------------------------------------------------------
    Step 'Creating Desktop and Start Menu shortcuts'
    $iconPy = Join-Path $PyDir 'flowshift.ico'
    $wsh = New-Object -ComObject WScript.Shell
    function New-Shortcut($path, $target, $scArgs, $workdir, $icon, $desc, $windowStyle = 1) {
        $sc = $wsh.CreateShortcut($path)
        $sc.TargetPath = $target
        $sc.Arguments = $scArgs
        $sc.WorkingDirectory = $workdir
        $sc.WindowStyle = $windowStyle
        if ($icon -and (Test-Path $icon)) { $sc.IconLocation = $icon }
        $sc.Description = $desc
        $sc.Save()
    }
    $desktop  = Join-Path $env:PUBLIC 'Desktop\FlowShift.lnk'
    $startDir = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\FlowShift'
    New-Item -ItemType Directory -Force -Path $startDir | Out-Null
    # The legacy Tk settings window (gui.py) is gone since 0.7.2-dev.4; an
    # upgrade removes its old Start Menu shortcut. Settings are in the WebGUI.
    Remove-Item -LiteralPath (Join-Path $startDir 'FlowShift GUI.lnk') -Force -ErrorAction SilentlyContinue
    # The desktop shortcut starts the autostart task instead of pythonw directly:
    # the runtime then always has the highest privileges of the task, so the
    # automatic update can replace the program folder without a UAC prompt.
    # Window style 7 = minimized (no console flash from schtasks.exe).
    $schtasks = Join-Path $env:SystemRoot 'System32\schtasks.exe'
    New-Shortcut $desktop $schtasks "/Run /TN `"$TaskName`"" $DataDir $iconPy 'Start FlowShift (autostart task)' 7
    New-Shortcut (Join-Path $startDir 'FlowShift Logs.lnk') 'explorer.exe' "`"$LogDir`"" $LogDir $null 'Open FlowShift log folder'
    $webIcon = Join-Path $WebGuiDir 'flowshift.ico'
    New-Shortcut (Join-Path $startDir 'FlowShift Web GUI.lnk') 'explorer.exe' "$ApiBase/" $InstallDir $webIcon "Open the FlowShift settings ($ApiBase)"
    New-Shortcut (Join-Path $env:PUBLIC 'Desktop\FlowShift Web GUI.lnk') 'explorer.exe' "$ApiBase/" $InstallDir $webIcon "Open the FlowShift settings ($ApiBase)"
    $uninBat = Join-Path $InstallDir 'uninstall_flowshift.bat'
    if (Test-Path $uninBat) { New-Shortcut (Join-Path $startDir 'Uninstall FlowShift.lnk') $uninBat '' $InstallDir $null 'Uninstall FlowShift' }
    Log 'shortcuts created (Desktop + Start Menu)' 'OK'

    # --- Firewall rules --------------------------------------------------
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

    # --- Start the runtime now (in the interactive session) ----------------
    Step 'Starting the FlowShift runtime (interactive session)'
    if ($FlowUpdate) {
        Log 'FlowUpdate mode: runtime start is deferred to the external update runner' 'OK'
    } else {
        # Read once by the FlowShift started now: it does not update itself
        # right away but at its next regular check (development channel: one
        # check interval later), after this run has released the install lock.
        try {
            $marker = [ordered]@{
                schema_version = 1
                version = $ProductVersion
                newer_known = $script:NewerVersion
                finished_at = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
            }
            New-Item -ItemType Directory -Force -Path $UpdatesDir | Out-Null
            [System.IO.File]::WriteAllText($ManualInstallMarker, ($marker | ConvertTo-Json), (New-Object System.Text.UTF8Encoding $false))
            Log 'FlowShift waits for its next regular update check before it updates itself' 'OK'
        } catch {
            Log "could not write $ManualInstallMarker : $($_.Exception.Message)" 'WARN'
        }
        try {
            Start-ScheduledTask -TaskName $TaskName
            Log 'scheduled task started (runs pythonw tray.py --tray in the user session)' 'OK'
        } catch {
            Log "could not start the scheduled task now: $($_.Exception.Message)" 'WARN'
        }
    }

    # --- Verify that program and WebGUI belong together ---------------------
    Step 'Verifying program and WebGUI (same VERSION, index.html + overlay.html served)'
    $installedWeb = Test-WebGuiAssets $WebGuiDir
    $installedProgram = ([System.IO.File]::ReadAllText((Join-Path $InstallDir 'VERSION'))).Trim()
    if (-not $installedWeb.Complete) {
        Fail "WebGUI files are missing in ${WebGuiDir}: $($installedWeb.Missing -join ', ')"
    }
    if ($installedWeb.Version -cne $installedProgram) {
        Fail "WebGUI version $($installedWeb.Version) does not match program version $installedProgram"
    }
    Log "installed files match: program $installedProgram, WebGUI $($installedWeb.Version)" 'OK'
    if ($FlowUpdate) {
        Log 'FlowUpdate mode: the external update runner checks the running runtime' 'OK'
    } else {
        $problems = @(Test-InstalledFlowShift)
        if ($problems.Count -gt 0) {
            foreach ($problem in $problems) { Log "verification: $problem" 'ERROR' }
            Fail ("FlowShift was installed, but the final check failed: " + ($problems -join '; '))
        }
        Log "running runtime $ProductVersion serves index.html and overlay.html; WebGUI matches" 'OK'
        # The new version runs: the previous installation is no longer needed.
        if ($script:PreviousMovedAside -and (Test-Path -LiteralPath $PreviousDir)) {
            try {
                Remove-Item -LiteralPath $PreviousDir -Recurse -Force -ErrorAction Stop
                Log "removed the previous installation $PreviousDir" 'OK'
            } catch {
                Log "could not remove $PreviousDir yet ($($_.Exception.Message)); the next installation removes it" 'WARN'
            }
            $script:PreviousMovedAside = $false
        }
    }

    # --- Done ---------------------------------------------------------------
    Step 'Finishing up'
    Write-Host ''
    Write-Host '================ INSTALLATION COMPLETE ================' -ForegroundColor Green
    Write-Host "Version       : $ProductVersion (program and WebGUI)"
    Write-Host "Program files : $InstallDir"
    Write-Host "WebGUI        : $ApiBase/  (Start Menu\FlowShift\FlowShift Web GUI)"
    Write-Host "Data / config : $DataDir (kept on upgrades)"
    Write-Host "Logs          : $LogDir"
    Write-Host "Autostart     : Scheduled Task '$TaskName' (interactive user session)"
    if ($WithNssm) { Write-Host "NSSM helper   : $ServiceName (manual, NOT the input path)" }
    Write-Host ''
    Write-Host 'Command wheel: Ctrl+Right click. Clipboard overlay: Ctrl+Win+V.' -ForegroundColor Green
    if ($script:NewerVersion) {
        Write-Host ''
        Write-Host "Note: FlowShift $($script:NewerVersion) is newer than this folder ($ProductVersion)." -ForegroundColor Yellow
        Write-Host 'The automatic update installs it again at its next regular check.' -ForegroundColor Yellow
    }
    Write-Host '======================================================' -ForegroundColor Green
    Log 'installation complete' 'OK'
}
catch {
    Fail "unhandled error: $($_.Exception.Message)"
}

Write-Host ''
Close-InstallerWindow 15
exit 0
