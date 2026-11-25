param(
    [string]$PlanPath,
    [switch]$Elevated,
    [switch]$LibraryOnly
)

$ErrorActionPreference = 'Stop'
# Stable X.Y.Z and development prereleases (X.Y.Z-dev.N) from the Forgejo
# development channel; build metadata stays allowed as before.
$script:SemVerPattern = '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-((?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$'
$script:TaskName = 'FlowShift'
$script:ControlPort = 45782
$script:PeerPort = 45781
$script:ApiPort = 5000
$script:MaxUserJsonBytes = 10MB
# Moving C:\Program Files\FlowShift aside fails with "access denied" while any
# process still has a file or its working directory inside it: retry a few
# times and stop leftover FlowShift processes (overlay host, WebView2, the
# legacy settings window) before each attempt.
$script:MoveAttempts = 5
$script:ProcessGraceSec = 10
$script:InUseMarker = 'program folder in use'
# Marker in the update error when another installation held the install lock.
$script:BusyMarker = 'another FlowShift installation is running'

# --- Install lock -----------------------------------------------------------
# One lock for every way FlowShift is installed or removed: install_flowshift.ps1
# (by hand, Setup.exe or as part of an update), this update runner, the updater
# inside the running program (it checks the lock before the handoff) and
# uninstall_flowshift.ps1. ProgramData\FlowShift\updates\install.lock is held
# open for the whole run and shared for reading only, so a second run cannot
# open it for writing. Windows closes the handle when the process ends: a
# crashed run never leaves a stale lock, the content only says who holds it.
# The same functions are in install_flowshift.ps1 and uninstall_flowshift.ps1.
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
    # Returns the held lock (Stream, Token) or $null when another run still
    # holds it after TimeoutSec. OnWait gets the current owner once.
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
            # Only a sharing violation means "held by another run".
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

function Get-CanonicalPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function Test-SamePath {
    param([string]$Left, [string]$Right)
    try {
        return [string]::Equals((Get-CanonicalPath $Left), (Get-CanonicalPath $Right),
            [System.StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Test-PathWithin {
    param([string]$Path, [string]$Root)
    $candidate = (Get-CanonicalPath $Path) + [System.IO.Path]::DirectorySeparatorChar
    $parent = (Get-CanonicalPath $Root) + [System.IO.Path]::DirectorySeparatorChar
    return $candidate.StartsWith($parent, [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-StrictSemVer {
    param([object]$Value)
    return ($Value -is [string] -and $Value -cmatch $script:SemVerPattern)
}

function Compare-SemVer {
    param([string]$Left, [string]$Right)
    if (-not (Test-StrictSemVer $Left) -or -not (Test-StrictSemVer $Right)) {
        throw 'Cannot compare invalid semantic versions'
    }
    $null = $Left -cmatch $script:SemVerPattern
    $leftParts = @([uint64]$Matches[1], [uint64]$Matches[2], [uint64]$Matches[3])
    $leftPre = $Matches[4]
    $null = $Right -cmatch $script:SemVerPattern
    $rightParts = @([uint64]$Matches[1], [uint64]$Matches[2], [uint64]$Matches[3])
    $rightPre = $Matches[4]
    for ($index = 0; $index -lt 3; $index++) {
        if ($leftParts[$index] -lt $rightParts[$index]) { return -1 }
        if ($leftParts[$index] -gt $rightParts[$index]) { return 1 }
    }
    # SemVer 2.0 precedence: a stable version ranks above its prereleases;
    # numeric identifiers compare numerically (dev.10 > dev.9).
    if (-not $leftPre -and -not $rightPre) { return 0 }
    if (-not $leftPre) { return 1 }
    if (-not $rightPre) { return -1 }
    $leftIds = $leftPre.Split('.')
    $rightIds = $rightPre.Split('.')
    $count = [Math]::Min($leftIds.Count, $rightIds.Count)
    for ($index = 0; $index -lt $count; $index++) {
        $l = $leftIds[$index]; $r = $rightIds[$index]
        if ($l -ceq $r) { continue }
        $lNum = $l -cmatch '^[0-9]+$'; $rNum = $r -cmatch '^[0-9]+$'
        if ($lNum -and $rNum) { if ([uint64]$l -lt [uint64]$r) { return -1 } else { return 1 } }
        if ($lNum) { return -1 }
        if ($rNum) { return 1 }
        if ([string]::CompareOrdinal($l, $r) -lt 0) { return -1 } else { return 1 }
    }
    if ($leftIds.Count -lt $rightIds.Count) { return -1 }
    if ($leftIds.Count -gt $rightIds.Count) { return 1 }
    return 0
}

function Write-AtomicText {
    param([string]$Path, [string]$Text)
    $directory = Split-Path -Parent $Path
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
    $temporary = Join-Path $directory ('.{0}.{1}.tmp' -f ([System.IO.Path]::GetFileName($Path)), [guid]::NewGuid().ToString('N'))
    $encoding = New-Object System.Text.UTF8Encoding($false)
    try {
        [System.IO.File]::WriteAllText($temporary, $Text, $encoding)
        if ([System.IO.File]::Exists($Path)) {
            $replaced = $temporary + '.replaced'
            [System.IO.File]::Replace($temporary, $Path, $replaced, $true)
            [System.IO.File]::Delete($replaced)
        } else {
            [System.IO.File]::Move($temporary, $Path)
        }
    } finally {
        if ([System.IO.File]::Exists($temporary)) { [System.IO.File]::Delete($temporary) }
    }
}

function Write-AtomicJson {
    param([string]$Path, [object]$Value)
    Write-AtomicText -Path $Path -Text (($Value | ConvertTo-Json -Depth 12) + "`n")
}

function Assert-StringProperty {
    param([object]$Object, [string]$Name)
    $value = $Object.$Name
    if ($value -isnot [string] -or [string]::IsNullOrWhiteSpace($value)) {
        throw "Update plan property '$Name' must be a non-empty string"
    }
    return [string]$value
}

function Read-ValidatedUpdatePlan {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not $env:ProgramFiles -or -not $env:ProgramData) { throw 'Official Windows paths are unavailable' }
    $expectedData = Get-CanonicalPath (Join-Path $env:ProgramData 'FlowShift')
    $expectedInstall = Get-CanonicalPath (Join-Path $env:ProgramFiles 'FlowShift')
    $expectedPlan = Get-CanonicalPath (Join-Path $expectedData 'updates\update_plan.json')
    if (-not (Test-SamePath $Path $expectedPlan)) { throw 'Only the fixed FlowShift update plan path is accepted' }
    if (-not (Test-Path -LiteralPath $expectedPlan -PathType Leaf)) { throw 'Update plan is missing' }

    try {
        $plan = [System.IO.File]::ReadAllText($expectedPlan) | ConvertFrom-Json
    } catch {
        throw "Update plan is not valid JSON: $($_.Exception.Message)"
    }
    if ($null -eq $plan -or $plan -isnot [pscustomobject]) { throw 'Update plan must be a JSON object' }
    if ($plan.schema_version -isnot [int] -or $plan.schema_version -ne 1) { throw 'Update plan schema must be 1' }

    $fromVersion = Assert-StringProperty $plan 'from_version'
    $toVersion = Assert-StringProperty $plan 'to_version'
    if (-not (Test-StrictSemVer $fromVersion) -or -not (Test-StrictSemVer $toVersion)) {
        throw 'Update plan versions must be canonical SemVer'
    }
    if ((Compare-SemVer $toVersion $fromVersion) -le 0) { throw 'Update target must be newer than installed version' }

    $installDir = Assert-StringProperty $plan 'install_dir'
    $dataDir = Assert-StringProperty $plan 'data_dir'
    if (-not (Test-SamePath $installDir $expectedInstall)) { throw 'Update plan install_dir is not official' }
    if (-not (Test-SamePath $dataDir $expectedData)) { throw 'Update plan data_dir is not official' }

    # 'installer' = FlowShift-Setup.exe (stable, GitHub); 'source' = source
    # archive of Forgejo main (development channel), installed with the
    # install_flowshift.ps1 it contains. Plans without the field are 'installer'.
    # 'peer' = package of a connected FlowShift with a newer version
    # (peer_update.py, checked file by file before the handoff), installed
    # like a source archive but with its prebuilt WebGUI (no Node.js).
    $packageKind = if ($null -eq $plan.package_kind) { 'installer' } else { [string]$plan.package_kind }
    if ($packageKind -cne 'installer' -and $packageKind -cne 'source' -and $packageKind -cne 'peer') {
        throw 'Update plan package_kind is invalid'
    }
    $plan | Add-Member -NotePropertyName package_kind -NotePropertyValue $packageKind -Force
    $installerPath = Assert-StringProperty $plan 'installer_path'
    $packageName = switch -CaseSensitive ($packageKind) {
        'source' { "FlowShift-Source-$toVersion.zip" }
        'peer' { "FlowShift-Peer-$toVersion.zip" }
        default { "FlowShift-Setup-$toVersion.exe" }
    }
    $expectedInstaller = Join-Path $expectedData "updates\downloads\$packageName"
    if (-not (Test-SamePath $installerPath $expectedInstaller)) { throw 'Installer path is outside managed update storage' }
    if (-not (Test-Path -LiteralPath $expectedInstaller -PathType Leaf)) { throw 'Installer is missing' }
    if ($plan.installer_size -isnot [int] -and $plan.installer_size -isnot [long]) {
        throw 'Installer size must be an integer'
    }
    $installerSize = [long]$plan.installer_size
    if ($installerSize -le 0) { throw 'Installer size is invalid' }
    $installerHash = Assert-StringProperty $plan 'installer_sha256'
    if ($installerHash -cnotmatch '^[0-9a-f]{64}$') { throw 'Installer SHA-256 is invalid' }

    $requestId = Assert-StringProperty $plan 'request_id'
    if ($requestId -cnotmatch '^[0-9a-f]{32}$') { throw 'Update request ID is invalid' }
    $createdAt = Assert-StringProperty $plan 'created_at'
    $parsedDate = [DateTimeOffset]::MinValue
    if (-not $createdAt.EndsWith('Z') -or -not [DateTimeOffset]::TryParse(
            $createdAt, [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$parsedDate)) {
        throw 'Update plan timestamp is invalid'
    }
    if (($plan.runtime_pid -isnot [int] -and $plan.runtime_pid -isnot [long]) -or [long]$plan.runtime_pid -le 0) {
        throw 'Runtime PID is invalid'
    }
    if ($plan.task_name -cne $script:TaskName -or $plan.control_port -ne $script:ControlPort -or
            $plan.peer_port -ne $script:PeerPort -or $plan.api_port -ne $script:ApiPort) {
        throw 'Update plan task or ports are not fixed FlowShift values'
    }
    if ($plan.task_name -isnot [string] -or $plan.control_port -isnot [int] -or
            $plan.peer_port -isnot [int] -or $plan.api_port -isnot [int]) {
        throw 'Update plan task and ports have invalid types'
    }

    $installedVersionPath = Join-Path $expectedInstall 'VERSION'
    if (-not (Test-Path -LiteralPath $installedVersionPath -PathType Leaf) -or
            ([System.IO.File]::ReadAllText($installedVersionPath)).Trim() -cne $fromVersion) {
        throw 'Installed VERSION does not match update plan from_version'
    }

    $file = Get-Item -LiteralPath $expectedInstaller
    if ($file.Length -ne $installerSize) { throw 'Installer size does not match update plan' }
    $actualHash = (Get-FileHash -LiteralPath $expectedInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -cne $installerHash) { throw 'Installer SHA-256 does not match update plan' }

    $plan.install_dir = $expectedInstall
    $plan.data_dir = $expectedData
    $plan.installer_path = Get-CanonicalPath $expectedInstaller
    $plan | Add-Member -NotePropertyName plan_path -NotePropertyValue $expectedPlan -Force
    $plan | Add-Member -NotePropertyName ack_path -NotePropertyValue (
        Join-Path $expectedData "updates\acks\update_ack-$requestId.json") -Force
    $plan | Add-Member -NotePropertyName result_path -NotePropertyValue (
        Join-Path $expectedData 'updates\last_update_result.json') -Force
    $plan | Add-Member -NotePropertyName log_path -NotePropertyValue (
        Join-Path $expectedData 'logs\update.log') -Force
    $plan | Add-Member -NotePropertyName source_dir -NotePropertyValue (
        Join-Path $expectedData 'updates\source') -Force
    $plan | Add-Member -NotePropertyName rollback_dir -NotePropertyValue (
        Join-Path (Split-Path -Parent $expectedInstall) "FlowShift.rollback-$fromVersion") -Force
    # A rollback first moves the failed new version here (instead of deleting
    # the program folder), so the rollback copy is never the only thing touched.
    $plan | Add-Member -NotePropertyName failed_dir -NotePropertyValue (
        Join-Path (Split-Path -Parent $expectedInstall) "FlowShift.failed-$toVersion") -Force
    return $plan
}

function Test-ArchivePackage {
    # Source archives and peer packages are ZIPs with one top folder that are
    # unpacked and installed with their own install_flowshift.ps1.
    param([Parameter(Mandatory = $true)][object]$Plan)
    return ($Plan.package_kind -ceq 'source' -or $Plan.package_kind -ceq 'peer')
}

function Write-UpdateLog {
    # Readable log of every update run: ProgramData\FlowShift\logs\update.log.
    # The install script of a source update writes its own steps to install.log.
    param([object]$Plan, [string]$Message, [string]$Level = 'INFO')
    try {
        $directory = Split-Path -Parent $Plan.log_path
        [System.IO.Directory]::CreateDirectory($directory) | Out-Null
        $line = '[{0}] [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
        [System.IO.File]::AppendAllText($Plan.log_path, $line + [Environment]::NewLine,
            (New-Object System.Text.UTF8Encoding($false)))
    } catch { }
}

function Expand-FlowShiftSource {
    # Unpacks the verified source archive into ProgramData\FlowShift\updates\source\<version>
    # entry by entry; every target must stay inside that folder (no '..' or
    # absolute paths). Returns the path of install_flowshift.ps1 to run.
    param([Parameter(Mandatory = $true)][object]$Plan)
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path -LiteralPath $Plan.source_dir) { Remove-Item -LiteralPath $Plan.source_dir -Recurse -Force }
    $target = Join-Path $Plan.source_dir $Plan.to_version
    [System.IO.Directory]::CreateDirectory($target) | Out-Null
    $targetRoot = (Get-CanonicalPath $target) + [System.IO.Path]::DirectorySeparatorChar
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Plan.installer_path)
    try {
        foreach ($entry in $archive.Entries) {
            $destination = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($target, $entry.FullName))
            if (-not $destination.StartsWith($targetRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw 'Source archive contains an unsafe path'
            }
            if ($entry.FullName.EndsWith('/')) {
                [System.IO.Directory]::CreateDirectory($destination) | Out-Null
                continue
            }
            [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($destination)) | Out-Null
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $destination, $true)
        }
    } finally {
        $archive.Dispose()
    }
    $tops = @(Get-ChildItem -LiteralPath $target -Directory)
    if ($tops.Count -ne 1) { throw 'Source archive must contain exactly one top folder' }
    $sourceRoot = $tops[0].FullName
    $versionPath = Join-Path $sourceRoot 'VERSION'
    if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) { throw 'Source archive has no VERSION' }
    $sourceVersion = ([System.IO.File]::ReadAllText($versionPath)).Trim().TrimStart([char]0xFEFF)
    if ($sourceVersion -cne $Plan.to_version) {
        throw "Source archive VERSION $sourceVersion does not match the update target $($Plan.to_version)"
    }
    $installScript = Join-Path $sourceRoot 'install_flowshift.ps1'
    if (-not (Test-Path -LiteralPath $installScript -PathType Leaf)) { throw 'Source archive has no install_flowshift.ps1' }
    return $installScript
}

function Enter-RunnerDirectory {
    # The runner inherits the working directory of whoever started it; FlowShift
    # runs in C:\Program Files\FlowShift\src\python, and a working directory
    # inside the program folder alone blocks moving that folder ("access
    # denied", 0.7.0-dev.29 -> dev.30). Work from ProgramData instead, both for
    # PowerShell and for the process (child processes inherit the latter).
    param([Parameter(Mandatory = $true)][string]$Directory)
    [System.IO.Directory]::CreateDirectory($Directory) | Out-Null
    Set-Location -LiteralPath $Directory
    [System.IO.Directory]::SetCurrentDirectory($Directory)
}

function Get-FlowShiftInstallProcesses {
    # Processes that keep the program folder busy: started from it (venv
    # python, Node), with it in their command line (tray.py, overlay_host.py,
    # gui.py under the base interpreter a venv launcher starts) and their
    # children (WebView2 of the overlay host). The runner and its parents are
    # never included.
    param([Parameter(Mandatory = $true)][string]$InstallDir)
    $root = Get-CanonicalPath $InstallDir
    $pattern = '(?i)' + [regex]::Escape($root) + '(?=[\\/"\s]|$)'
    $all = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
    $byId = @{}
    foreach ($process in $all) { $byId[[int]$process.ProcessId] = $process }
    $protected = @{}
    $current = [int]$PID
    while ($current -gt 0 -and $byId.ContainsKey($current) -and -not $protected.ContainsKey($current)) {
        $protected[$current] = $true
        $current = [int]$byId[$current].ParentProcessId
    }
    $selected = @{}
    foreach ($process in $all) {
        $id = [int]$process.ProcessId
        # Explorer (the tray's "open install folder") is the user's shell and
        # does not block the move; it is never stopped.
        if ($protected.ContainsKey($id) -or [string]$process.Name -ieq 'explorer.exe') { continue }
        if ([string]$process.ExecutablePath -match $pattern -or [string]$process.CommandLine -match $pattern) {
            $selected[$id] = $process
        }
    }
    do {
        $added = $false
        foreach ($process in $all) {
            $id = [int]$process.ProcessId
            if ($selected.ContainsKey($id) -or $protected.ContainsKey($id)) { continue }
            if ($selected.ContainsKey([int]$process.ParentProcessId)) {
                $selected[$id] = $process
                $added = $true
            }
        }
    } while ($added)
    return @($selected.Values | ForEach-Object { [pscustomobject]@{ Id = [int]$_.ProcessId; Name = [string]$_.Name } })
}

function Stop-FlowShiftInstallProcesses {
    # Gives leftover processes GraceSec to end on their own (the overlay host
    # follows its parent), then ends the rest. Returns 'name (pid)' entries of
    # what was still running before the forced stop, for update.log.
    param([Parameter(Mandatory = $true)][string]$InstallDir, [int]$GraceSec = 10)
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds([Math]::Max($GraceSec, 0))
    do {
        $remaining = @(Get-FlowShiftInstallProcesses -InstallDir $InstallDir)
        if ($remaining.Count -eq 0) { return @() }
        if ([DateTimeOffset]::UtcNow -ge $deadline) { break }
        Start-Sleep -Milliseconds 250
    } while ($true)
    foreach ($process in $remaining) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
    return @($remaining | ForEach-Object { '{0} ({1})' -f $_.Name, $_.Id })
}

function Write-UpdateAck {
    # 'accepted': the runner takes over and FlowShift shuts down for the update.
    # 'busy': another installation holds the install lock; nothing is done and
    # FlowShift keeps running (the program retries at its next regular check).
    param([object]$Plan, [string]$Status = 'accepted')
    Write-AtomicJson -Path $Plan.ack_path -Value ([ordered]@{
        schema_version = 1
        request_id = $Plan.request_id
        status = $Status
        runner_pid = $PID
        accepted_at = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    })
}

function Get-DefaultUpdateOperations {
    return @{
        ProcessExists = {
            param([int]$ProcessId)
            return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
        }
        PortOpen = {
            param([int]$Port)
            $client = New-Object System.Net.Sockets.TcpClient
            try {
                $pending = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
                if (-not $pending.AsyncWaitHandle.WaitOne(300)) { return $false }
                $client.EndConnect($pending)
                return $client.Connected
            } catch {
                return $false
            } finally {
                $client.Close()
            }
        }
        ExportTaskXml = {
            param([string]$Name)
            return Export-ScheduledTask -TaskName $Name -ErrorAction Stop
        }
        ImportTaskXml = {
            param([string]$Name, [string]$XmlPath)
            Register-ScheduledTask -TaskName $Name -Xml ([System.IO.File]::ReadAllText($XmlPath)) -Force | Out-Null
        }
        TaskExists = {
            param([string]$Name)
            return $null -ne (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue)
        }
        StopTask = {
            param([string]$Name)
            Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        }
        StartTask = {
            param([string]$Name)
            Start-ScheduledTask -TaskName $Name -ErrorAction Stop
        }
        RemoveDirectory = {
            param([string]$Path)
            if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
        }
        MoveDirectory = {
            param([string]$Source, [string]$Destination)
            [System.IO.Directory]::Move((Get-CanonicalPath $Source), (Get-CanonicalPath $Destination))
        }
        StopInstallProcesses = {
            param([string]$InstallDir, [int]$GraceSec)
            return Stop-FlowShiftInstallProcesses -InstallDir $InstallDir -GraceSec $GraceSec
        }
        RunInstaller = {
            param([string]$Installer, [string[]]$Arguments)
            $process = Start-Process -FilePath $Installer -ArgumentList $Arguments -Wait -PassThru `
                -WorkingDirectory (Split-Path -Parent $Installer)
            return [int]$process.ExitCode
        }
        ExpandSource = {
            param([object]$Plan)
            return Expand-FlowShiftSource -Plan $Plan
        }
        RunSourceInstall = {
            param([string]$Script)
            # Same PowerShell host (5.1 or 7) as this runner; already elevated,
            # so install_flowshift.ps1 does not ask for UAC again. Start-Process
            # joins the arguments with spaces: the path is quoted for spaces.
            $hostExecutable = (Get-Process -Id $PID).Path
            $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                ('"' + $Script.Replace('"', '""') + '"'), '-FlowUpdate', '-NonInteractive')
            $process = Start-Process -FilePath $hostExecutable -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden `
                -WorkingDirectory (Split-Path -Parent $Script)
            return [int]$process.ExitCode
        }
        TestHealth = {
            param([object]$Plan, [string]$Version, [int]$TimeoutSec)
            return Test-FlowShiftHealth -Plan $Plan -ExpectedVersion $Version -TimeoutSec $TimeoutSec
        }
        Sleep = {
            param([int]$Milliseconds)
            Start-Sleep -Milliseconds $Milliseconds
        }
        NotifyUser = {
            param([string]$Title, [string]$Text)
            # FlowShift (and with it the WebGUI) may be gone at this point, so
            # the runner tells the logged-on user itself. Shown last, after the
            # result is written and the install lock is released.
            (New-Object -ComObject WScript.Shell).Popup($Text, 0, $Title, 48) | Out-Null
        }
    }
}

function Invoke-UpdateOperation {
    param([hashtable]$Operations, [string]$Name, [object[]]$Arguments = @())
    if (-not $Operations.ContainsKey($Name) -or $Operations[$Name] -isnot [scriptblock]) {
        throw "Update operation '$Name' is not available"
    }
    return & $Operations[$Name] @Arguments
}

function Backup-FlowShiftState {
    param([object]$Plan, [hashtable]$Operations)
    $stamp = [DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    $backupDir = Join-Path $Plan.data_dir "backups\update-$stamp-$($Plan.from_version)"
    [System.IO.Directory]::CreateDirectory($backupDir) | Out-Null
    $files = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $Plan.data_dir -Filter '*.json' -File -ErrorAction SilentlyContinue)) {
        if ($file.Length -gt $script:MaxUserJsonBytes) { continue }
        $target = Join-Path $backupDir $file.Name
        [System.IO.File]::Copy($file.FullName, $target, $true)
        $files += [ordered]@{
            name = $file.Name
            size = [long]$file.Length
            sha256 = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    $taskXml = [string](Invoke-UpdateOperation $Operations 'ExportTaskXml' @($Plan.task_name))
    if ([string]::IsNullOrWhiteSpace($taskXml)) { throw 'Scheduled Task export was empty' }
    $taskPath = Join-Path $backupDir 'scheduled-task.xml'
    Write-AtomicText -Path $taskPath -Text $taskXml
    $manifestPath = Join-Path $backupDir 'backup_manifest.json'
    Write-AtomicJson -Path $manifestPath -Value ([ordered]@{
        schema_version = 1
        from_version = $Plan.from_version
        created_at = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
        task_name = $Plan.task_name
        task_xml = 'scheduled-task.xml'
        files = $files
    })
    return [pscustomobject]@{ Directory = $backupDir; ManifestPath = $manifestPath; TaskPath = $taskPath }
}

function Restore-FlowShiftState {
    param([object]$Plan, [object]$Backup, [hashtable]$Operations)
    $manifest = [System.IO.File]::ReadAllText($Backup.ManifestPath) | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or $manifest.task_name -cne $script:TaskName) {
        throw 'Backup manifest is invalid'
    }
    foreach ($entry in @($manifest.files)) {
        if ($entry.name -isnot [string] -or [System.IO.Path]::GetFileName($entry.name) -cne $entry.name -or
                $entry.name -cnotmatch '^[^\\/:*?"<>|]+\.json$') {
            throw 'Backup manifest contains an unsafe file name'
        }
        $source = Join-Path $Backup.Directory $entry.name
        if (-not (Test-PathWithin $source $Backup.Directory) -or -not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw 'Backed-up user data file is missing'
        }
        [System.IO.File]::Copy($source, (Join-Path $Plan.data_dir $entry.name), $true)
    }
    Invoke-UpdateOperation $Operations 'ImportTaskXml' @($Plan.task_name, $Backup.TaskPath) | Out-Null
}

function Restore-InstallOwnershipState {
    param([object]$Plan, [object]$Backup)
    $source = Join-Path $Backup.Directory 'install_state.json'
    if (Test-Path -LiteralPath $source -PathType Leaf) {
        [System.IO.File]::Copy($source, (Join-Path $Plan.data_dir 'install_state.json'), $true)
    }
}

function Wait-FlowShiftRuntimeExit {
    param([object]$Plan, [hashtable]$Operations, [int]$TimeoutSec = 30)
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds([Math]::Max($TimeoutSec, 0))
    do {
        $processAlive = [bool](Invoke-UpdateOperation $Operations 'ProcessExists' @([int]$Plan.runtime_pid))
        $controlOpen = [bool](Invoke-UpdateOperation $Operations 'PortOpen' @([int]$Plan.control_port))
        if (-not $processAlive -and -not $controlOpen) { return $true }
        if ([DateTimeOffset]::UtcNow -ge $deadline) { return $false }
        Invoke-UpdateOperation $Operations 'Sleep' @(200) | Out-Null
    } while ($true)
}

function Get-ValidatedRollbackSiblings {
    param([object]$Plan)
    $parent = Get-CanonicalPath (Split-Path -Parent $Plan.install_dir)
    $siblings = @()
    foreach ($directory in @(Get-ChildItem -LiteralPath $parent -Directory -Filter 'FlowShift.rollback-*' -ErrorAction SilentlyContinue)) {
        if ($directory.Name -notmatch '^FlowShift\.rollback-(.+)$') {
            throw 'Unvalidated FlowShift rollback sibling blocks the update'
        }
        $candidateVersion = $Matches[1]
        if (-not (Test-StrictSemVer $candidateVersion) -or
                -not (Test-SamePath $directory.Parent.FullName $parent)) {
            throw 'Unvalidated FlowShift rollback sibling blocks the update'
        }
        $siblings += $directory.FullName
    }
    return $siblings
}

function Clear-RollbackTarget {
    # Runs right before the program folder is moved aside. A rollback copy with
    # the same name holds the version that is installed and was running a
    # moment ago, so it is redundant and replaced. Copies of other versions are
    # kept until the new version is verified (Remove-OlderRollbackCopies); a
    # rollback copy is never removed before that.
    param([object]$Plan, [hashtable]$Operations)
    $null = Get-ValidatedRollbackSiblings -Plan $Plan
    if (Test-Path -LiteralPath $Plan.rollback_dir) {
        Write-UpdateLog -Plan $Plan -Message "replacing the earlier rollback copy of the installed version $($Plan.from_version)"
        Invoke-UpdateOperation $Operations 'RemoveDirectory' @($Plan.rollback_dir) | Out-Null
    }
    if (Test-Path -LiteralPath $Plan.failed_dir) {
        Invoke-UpdateOperation $Operations 'RemoveDirectory' @($Plan.failed_dir) | Out-Null
    }
}

function Remove-OlderRollbackCopies {
    # Only after the new version answered the health check: keeps the copy of
    # the version that was just replaced, removes copies of older versions.
    param([object]$Plan, [hashtable]$Operations)
    foreach ($path in @(Get-ValidatedRollbackSiblings -Plan $Plan)) {
        if (Test-SamePath $path $Plan.rollback_dir) { continue }
        try {
            Invoke-UpdateOperation $Operations 'RemoveDirectory' @($path) | Out-Null
            Write-UpdateLog -Plan $Plan -Message "removed the older rollback copy $path"
        } catch {
            Write-UpdateLog -Plan $Plan -Message "could not remove the older rollback copy ${path}: $($_.Exception.Message)" -Level 'WARN'
        }
    }
}

function Prepare-FlowShiftRollback {
    param([object]$Plan, [hashtable]$Operations)
    if (-not (Test-Path -LiteralPath $Plan.install_dir -PathType Container)) {
        throw 'Existing installation directory is missing'
    }
    Clear-RollbackTarget -Plan $Plan -Operations $Operations
    for ($attempt = 1; ; $attempt++) {
        $stopped = @()
        try {
            $stopped = @(Invoke-UpdateOperation $Operations 'StopInstallProcesses' @($Plan.install_dir, $script:ProcessGraceSec))
        } catch {
            Write-UpdateLog -Plan $Plan -Message "could not list processes in the program folder: $($_.Exception.Message)" -Level 'WARN'
        }
        if ($stopped.Count -gt 0) {
            Write-UpdateLog -Plan $Plan -Message ("stopped processes still running from the program folder: " + ($stopped -join ', ')) -Level 'WARN'
        }
        try {
            Invoke-UpdateOperation $Operations 'MoveDirectory' @($Plan.install_dir, $Plan.rollback_dir) | Out-Null
            return
        } catch {
            $moveError = $_.Exception.Message
            if ($attempt -ge $script:MoveAttempts) {
                throw "The $($script:InUseMarker) could not be moved aside after $attempt attempts: $moveError"
            }
            Write-UpdateLog -Plan $Plan -Message "moving the program folder failed (attempt $attempt of $($script:MoveAttempts)): $moveError" -Level 'WARN'
            Invoke-UpdateOperation $Operations 'Sleep' @(1000) | Out-Null
        }
    }
}

function Test-FlowShiftHealth {
    param([object]$Plan, [string]$ExpectedVersion, [int]$TimeoutSec = 45)
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds([Math]::Max($TimeoutSec, 1))
    do {
        try {
            $versionPath = Join-Path $Plan.install_dir 'VERSION'
            $installedVersion = ([System.IO.File]::ReadAllText($versionPath)).Trim()
            $task = Get-ScheduledTask -TaskName $Plan.task_name -ErrorAction Stop
            if ($null -eq $task -or $installedVersion -cne $ExpectedVersion) { throw 'version or task mismatch' }

            $client = New-Object System.Net.Sockets.TcpClient
            try {
                $pending = $client.BeginConnect('127.0.0.1', [int]$Plan.control_port, $null, $null)
                if (-not $pending.AsyncWaitHandle.WaitOne(500)) { throw 'control timeout' }
                $client.EndConnect($pending)
                if (-not $client.Connected) { throw 'control unavailable' }
            } finally {
                $client.Close()
            }

            $statusResponse = Invoke-WebRequest -Uri "http://127.0.0.1:$($Plan.api_port)/api/status" -UseBasicParsing -TimeoutSec 2
            $status = $statusResponse.Content | ConvertFrom-Json
            $reportedVersion = if ($status.status.app_version) { [string]$status.status.app_version } elseif ($status.app_version) { [string]$status.app_version } elseif ($status.version.app_version) { [string]$status.version.app_version } else { '' }
            if ($reportedVersion -cne $ExpectedVersion) { throw 'API version mismatch' }
            $root = Invoke-WebRequest -Uri "http://127.0.0.1:$($Plan.api_port)/" -UseBasicParsing -TimeoutSec 2
            if ([string]$root.Content -notmatch '(?i)<html') { throw 'Web root is not HTML' }
            if ($ExpectedVersion -cne $Plan.to_version) {
                # Rollback to the previous release: it may predate overlay.html
                # and the WebGUI version report.
                return $true
            }
            # The WebGUI is part of the update: the overlay entry must be served
            # and the runtime must confirm that the WebGUI has its own VERSION.
            $overlay = Invoke-WebRequest -Uri "http://127.0.0.1:$($Plan.api_port)/overlay.html" -UseBasicParsing -TimeoutSec 2
            if ([string]$overlay.Content -notmatch 'overlay-root') { throw 'overlay.html is not served' }
            $webgui = if ($status.webgui) { $status.webgui } elseif ($status.status.webgui) { $status.status.webgui } else { $null }
            if ($null -eq $webgui -or -not $webgui.ok -or [string]$webgui.webgui_version -cne $ExpectedVersion) {
                throw 'WebGUI does not match the installed version'
            }
            return $true
        } catch {
            if ([DateTimeOffset]::UtcNow -ge $deadline) { return $false }
            Start-Sleep -Milliseconds 500
        }
    } while ($true)
}

function Invoke-FlowShiftRollback {
    param([object]$Plan, [object]$Backup, [hashtable]$Operations, [int]$HealthTimeoutSec)
    # The rollback copy is only ever moved back, never deleted: if anything
    # here fails it stays where it is and the user is told about it.
    $errors = New-Object System.Collections.Generic.List[string]
    if (-not (Test-Path -LiteralPath $Plan.rollback_dir -PathType Container)) {
        # Without a copy the program folder (whatever it holds now) is the best
        # that is left; it is not touched.
        $errors.Add("the rollback copy $($Plan.rollback_dir) is missing; the program folder was left as it is")
        return [pscustomobject]@{ Success = $false; Error = ($errors -join '; ') }
    }
    try { Invoke-UpdateOperation $Operations 'StopTask' @($Plan.task_name) | Out-Null } catch { $errors.Add($_.Exception.Message) }
    try { $null = Invoke-UpdateOperation $Operations 'StopInstallProcesses' @($Plan.install_dir, $script:ProcessGraceSec) } catch { }
    $failedMovedAside = $false
    if (Test-Path -LiteralPath $Plan.install_dir) {
        try {
            if (Test-Path -LiteralPath $Plan.failed_dir) {
                Invoke-UpdateOperation $Operations 'RemoveDirectory' @($Plan.failed_dir) | Out-Null
            }
            Invoke-UpdateOperation $Operations 'MoveDirectory' @($Plan.install_dir, $Plan.failed_dir) | Out-Null
            $failedMovedAside = $true
        } catch {
            Write-UpdateLog -Plan $Plan -Message "could not move the failed version aside ($($_.Exception.Message)); removing it" -Level 'WARN'
            try { Invoke-UpdateOperation $Operations 'RemoveDirectory' @($Plan.install_dir) | Out-Null } catch { $errors.Add($_.Exception.Message) }
        }
    }
    $restored = $false
    if (Test-Path -LiteralPath $Plan.install_dir) {
        $errors.Add('the program folder of the failed version could not be cleared')
    } else {
        try {
            Invoke-UpdateOperation $Operations 'MoveDirectory' @($Plan.rollback_dir, $Plan.install_dir) | Out-Null
            $restored = $true
        } catch {
            $errors.Add($_.Exception.Message)
            if ($failedMovedAside) {
                # Do not leave an empty program folder behind; the rollback
                # copy stays untouched next to it.
                try { Invoke-UpdateOperation $Operations 'MoveDirectory' @($Plan.failed_dir, $Plan.install_dir) | Out-Null } catch { }
            }
        }
    }
    if ($restored) {
        Write-UpdateLog -Plan $Plan -Message "previous version $($Plan.from_version) moved back from $($Plan.rollback_dir)"
        try { Restore-FlowShiftState -Plan $Plan -Backup $Backup -Operations $Operations } catch { $errors.Add($_.Exception.Message) }
        try { Invoke-UpdateOperation $Operations 'StartTask' @($Plan.task_name) | Out-Null } catch { $errors.Add($_.Exception.Message) }
        if ($errors.Count -eq 0) {
            try {
                if (-not [bool](Invoke-UpdateOperation $Operations 'TestHealth' @($Plan, $Plan.from_version, $HealthTimeoutSec))) {
                    $errors.Add('Restored runtime health validation timed out')
                }
            } catch { $errors.Add($_.Exception.Message) }
        }
        if ($errors.Count -eq 0 -and $failedMovedAside) {
            try { Invoke-UpdateOperation $Operations 'RemoveDirectory' @($Plan.failed_dir) | Out-Null } catch { }
        }
    }
    return [pscustomobject]@{ Success = ($errors.Count -eq 0); Error = ($errors -join '; ') }
}

function Write-UpdateResult {
    param([object]$Plan, [string]$StartedAt, [string]$Result, [string]$ErrorMessage)
    Write-AtomicJson -Path $Plan.result_path -Value ([ordered]@{
        schema_version = 1
        from_version = $Plan.from_version
        to_version = $Plan.to_version
        started_at = $StartedAt
        finished_at = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
        result = $Result
        error = if ([string]::IsNullOrWhiteSpace($ErrorMessage)) { $null } else { $ErrorMessage }
    })
}

function Invoke-FlowShiftUpdate {
    param(
        [Parameter(Mandatory = $true)][string]$PlanPath,
        [hashtable]$Operations = (Get-DefaultUpdateOperations),
        [int]$RuntimeExitTimeoutSec = 30,
        [int]$HealthTimeoutSec = 45,
        [int]$LockTimeoutSec = 5
    )
    $plan = Read-ValidatedUpdatePlan -Path $PlanPath
    # Never two installations at once (21.11.: two update runs and a manual
    # install ran together, one removed the other's source and rollback copy).
    # Waiting would keep FlowShift waiting for the ack, so a busy lock ends this
    # run before anything is touched; FlowShift keeps running and tries again
    # at its next regular check.
    $lock = Enter-InstallLock -DataDir $plan.data_dir -Owner 'update' -Version $plan.to_version -TimeoutSec $LockTimeoutSec
    if ($null -eq $lock) {
        $holder = Format-InstallLockOwner (Read-InstallLockOwner $plan.data_dir)
        Write-UpdateAck -Plan $plan -Status 'busy'
        try { Remove-Item -LiteralPath $plan.plan_path -Force -ErrorAction Stop } catch { }
        Write-UpdateLog -Plan $plan -Message ("update {0} -> {1} not started: {2} ({3}); nothing was changed, FlowShift keeps running" -f
            $plan.from_version, $plan.to_version, $script:BusyMarker, $holder) -Level 'WARN'
        return [pscustomobject]@{ Result = 'busy'; Error = "$($script:BusyMarker) ($holder)"; Plan = $plan; Backup = $null }
    }
    try {
        $outcome = Invoke-LockedFlowShiftUpdate -Plan $plan -Lock $lock -Operations $Operations `
            -RuntimeExitTimeoutSec $RuntimeExitTimeoutSec -HealthTimeoutSec $HealthTimeoutSec
    } finally {
        Exit-InstallLock $lock
    }
    $notice = Get-UserNotice -Plan $plan -Result $outcome.Result
    if ($notice) {
        Write-UpdateLog -Plan $plan -Message 'FlowShift is not running after the failed update; the user was told what to do' -Level 'ERROR'
        try { Invoke-UpdateOperation $Operations 'NotifyUser' @('FlowShift update failed', $notice) | Out-Null } catch { }
    }
    $outcome | Add-Member -NotePropertyName Notice -NotePropertyValue $notice -Force
    return $outcome
}

function Get-UserNotice {
    # Plain words for the logged-on user when FlowShift is not running after
    # the update; $null when FlowShift (old or new) is running again.
    param([object]$Plan, [string]$Result)
    if ($Result -eq 'success' -or $Result -eq 'rollback_success') { return $null }
    $versionPath = Join-Path $Plan.install_dir 'VERSION'
    $installed = Test-Path -LiteralPath $versionPath -PathType Leaf
    if ($Result -ne 'rollback_failed' -and $installed) { return $null }
    $text = "The FlowShift update from $($Plan.from_version) to $($Plan.to_version) failed and FlowShift could not be started again automatically.`n`n"
    if (Test-Path -LiteralPath $Plan.rollback_dir -PathType Container) {
        $text += "The previous version is kept in:`n$($Plan.rollback_dir)`n`n"
    }
    $text += "What to do: run install_flowshift.bat from your FlowShift folder once more. Your settings and clipboard history are kept.`n`nDetails: $($Plan.log_path)"
    return $text
}

function Invoke-LockedFlowShiftUpdate {
    param(
        [Parameter(Mandatory = $true)][object]$Plan,
        [Parameter(Mandatory = $true)][object]$Lock,
        [hashtable]$Operations,
        [int]$RuntimeExitTimeoutSec,
        [int]$HealthTimeoutSec
    )
    $plan = $Plan
    Write-UpdateAck -Plan $plan
    # The validated plan is kept in memory from here on. Removing the file now
    # means a FlowShift that starts again (after success or a failure) never
    # finds a stale update_plan.json.
    try { Remove-Item -LiteralPath $plan.plan_path -Force -ErrorAction Stop } catch { }
    $startedAt = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    Write-UpdateLog -Plan $plan -Message ("update {0} -> {1} started ({2} package, PowerShell {3}, administrator {4}, working directory {5})" -f
        $plan.from_version, $plan.to_version, $plan.package_kind, $PSVersionTable.PSVersion,
        (Test-Administrator), [System.IO.Directory]::GetCurrentDirectory())
    $backup = $null
    $rollbackPrepared = $false
    $runtimeExited = $false
    $result = 'failed'
    $errorMessage = $null
    try {
        if (-not (Wait-FlowShiftRuntimeExit -Plan $plan -Operations $Operations -TimeoutSec $RuntimeExitTimeoutSec)) {
            throw 'Runtime PID or control port did not exit before the timeout'
        }
        $runtimeExited = $true
        Write-UpdateLog -Plan $plan -Message 'FlowShift has stopped'
        $installScript = $null
        if ((Test-ArchivePackage $plan)) {
            # Unpacked and checked before the installation is touched.
            $installScript = [string](Invoke-UpdateOperation $Operations 'ExpandSource' @($plan))
            Write-UpdateLog -Plan $plan -Message "source $($plan.to_version) unpacked: $installScript"
        }
        $backup = Backup-FlowShiftState -Plan $plan -Operations $Operations
        # install_flowshift.ps1 (directly or inside Setup.exe) inherits the
        # token and runs under this run's install lock instead of waiting for it.
        $env:FLOWSHIFT_INSTALL_LOCK_TOKEN = $Lock.Token
        Prepare-FlowShiftRollback -Plan $plan -Operations $Operations
        $rollbackPrepared = $true
        Write-UpdateLog -Plan $plan -Message "previous version kept for rollback in $($plan.rollback_dir)"
        # install_flowshift.ps1 copies the Python packages from there when
        # requirements.txt did not change (no download, Viktor 25.11.).
        $env:FLOWSHIFT_PREVIOUS_INSTALL = $plan.rollback_dir
        if ((Test-ArchivePackage $plan)) {
            $exitCode = [int](Invoke-UpdateOperation $Operations 'RunSourceInstall' @($installScript))
            if ($exitCode -ne 0) { throw "install_flowshift.ps1 exited with code $exitCode (details in install.log)" }
        } else {
            $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/FLOWUPDATE')
            $exitCode = [int](Invoke-UpdateOperation $Operations 'RunInstaller' @($plan.installer_path, $arguments))
            if ($exitCode -ne 0) { throw "Setup exited with code $exitCode" }
        }
        Write-UpdateLog -Plan $plan -Message "files of $($plan.to_version) installed; starting FlowShift"
        Restore-InstallOwnershipState -Plan $plan -Backup $backup
        Invoke-UpdateOperation $Operations 'StartTask' @($plan.task_name) | Out-Null
        if (-not [bool](Invoke-UpdateOperation $Operations 'TestHealth' @($plan, $plan.to_version, $HealthTimeoutSec))) {
            throw 'Post-install health validation timed out'
        }
        $result = 'success'
        try { Remove-OlderRollbackCopies -Plan $plan -Operations $Operations } catch {
            Write-UpdateLog -Plan $plan -Message "older rollback copies were kept: $($_.Exception.Message)" -Level 'WARN'
        }
    } catch {
        $errorMessage = $_.Exception.Message
        Write-UpdateLog -Plan $plan -Message "update failed: $errorMessage" -Level 'ERROR'
        if ($rollbackPrepared -and $null -ne $backup) {
            $rollback = Invoke-FlowShiftRollback -Plan $plan -Backup $backup -Operations $Operations -HealthTimeoutSec $HealthTimeoutSec
            if ($rollback.Success) {
                $result = 'rollback_success'
            } else {
                $result = 'rollback_failed'
                $errorMessage = "$errorMessage; rollback: $($rollback.Error)"
            }
        } elseif ($runtimeExited) {
            try {
                Invoke-UpdateOperation $Operations 'StartTask' @($plan.task_name) | Out-Null
                if (-not [bool](Invoke-UpdateOperation $Operations 'TestHealth' @($plan, $plan.from_version, $HealthTimeoutSec))) {
                    $errorMessage = "$errorMessage; old runtime restart health validation timed out"
                }
            } catch {
                $errorMessage = "$errorMessage; old runtime restart failed: $($_.Exception.Message)"
            }
        }
    }
    Remove-Item Env:FLOWSHIFT_INSTALL_LOCK_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:FLOWSHIFT_PREVIOUS_INSTALL -ErrorAction SilentlyContinue
    Write-UpdateResult -Plan $plan -StartedAt $startedAt -Result $result -ErrorMessage $errorMessage
    $resultLevel = if ($result -eq 'success') { 'OK' } else { 'WARN' }
    Write-UpdateLog -Plan $plan -Message "result: $result" -Level $resultLevel
    if ((Test-ArchivePackage $plan)) {
        try { Remove-Item -LiteralPath $plan.source_dir -Recurse -Force -ErrorAction Stop } catch { }
    }
    try { Remove-Item -LiteralPath $plan.plan_path -Force -ErrorAction Stop } catch { }
    return [pscustomobject]@{ Result = $result; Error = $errorMessage; Plan = $plan; Backup = $backup }
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-ExternalRunnerLocation {
    param([object]$Plan)
    $expected = Join-Path $Plan.data_dir 'updates\runner\update_flowshift.ps1'
    if (-not (Test-SamePath $PSCommandPath $expected)) { throw 'Updater is not running from its fixed external runner path' }
    if (Test-PathWithin $PSCommandPath $Plan.install_dir) { throw 'Updater must run outside InstallDir' }
}

if (-not $LibraryOnly) {
    if ([string]::IsNullOrWhiteSpace($PlanPath)) {
        $PlanPath = Join-Path $env:ProgramData 'FlowShift\updates\update_plan.json'
    }
    try {
        $runnerHome = Join-Path $env:ProgramData 'FlowShift\updates'
        Enter-RunnerDirectory -Directory $runnerHome
        $validated = Read-ValidatedUpdatePlan -Path $PlanPath
        Assert-ExternalRunnerLocation -Plan $validated
        if (-not (Test-Administrator)) {
            $hostExecutable = (Get-Process -Id $PID).Path
            $quotedScript = '"' + $PSCommandPath.Replace('"', '""') + '"'
            $quotedPlan = '"' + $validated.plan_path.Replace('"', '""') + '"'
            $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $quotedScript,
                '-PlanPath', $quotedPlan, '-Elevated')
            Start-Process -FilePath $hostExecutable -Verb RunAs -ArgumentList $arguments -WorkingDirectory $runnerHome | Out-Null
            exit 0
        }
        $outcome = Invoke-FlowShiftUpdate -PlanPath $validated.plan_path
        if ($outcome.Result -eq 'success') { exit 0 }
        exit 1
    } catch {
        Write-Error $_.Exception.Message
        exit 1
    }
}
