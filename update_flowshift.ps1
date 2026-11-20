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
    $packageKind = if ($null -eq $plan.package_kind) { 'installer' } else { [string]$plan.package_kind }
    if ($packageKind -cne 'installer' -and $packageKind -cne 'source') { throw 'Update plan package_kind is invalid' }
    $plan | Add-Member -NotePropertyName package_kind -NotePropertyValue $packageKind -Force
    $installerPath = Assert-StringProperty $plan 'installer_path'
    $packageName = if ($packageKind -ceq 'source') { "FlowShift-Source-$toVersion.zip" } else { "FlowShift-Setup-$toVersion.exe" }
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
    return $plan
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
    param([object]$Plan)
    Write-AtomicJson -Path $Plan.ack_path -Value ([ordered]@{
        schema_version = 1
        request_id = $Plan.request_id
        status = 'accepted'
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

function Remove-ValidatedRollbackSiblings {
    param([object]$Plan, [hashtable]$Operations)
    $parent = Get-CanonicalPath (Split-Path -Parent $Plan.install_dir)
    foreach ($directory in @(Get-ChildItem -LiteralPath $parent -Directory -Filter 'FlowShift.rollback-*' -ErrorAction SilentlyContinue)) {
        if ($directory.Name -notmatch '^FlowShift\.rollback-(.+)$') {
            throw 'Unvalidated FlowShift rollback sibling blocks the update'
        }
        $candidateVersion = $Matches[1]
        if (-not (Test-StrictSemVer $candidateVersion) -or
                -not (Test-SamePath $directory.Parent.FullName $parent)) {
            throw 'Unvalidated FlowShift rollback sibling blocks the update'
        }
        Invoke-UpdateOperation $Operations 'RemoveDirectory' @($directory.FullName) | Out-Null
    }
}

function Prepare-FlowShiftRollback {
    param([object]$Plan, [hashtable]$Operations)
    if (-not (Test-Path -LiteralPath $Plan.install_dir -PathType Container)) {
        throw 'Existing installation directory is missing'
    }
    Remove-ValidatedRollbackSiblings -Plan $Plan -Operations $Operations
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
    $errors = New-Object System.Collections.Generic.List[string]
    try { Invoke-UpdateOperation $Operations 'StopTask' @($Plan.task_name) | Out-Null } catch { $errors.Add($_.Exception.Message) }
    try { $null = Invoke-UpdateOperation $Operations 'StopInstallProcesses' @($Plan.install_dir, $script:ProcessGraceSec) } catch { }
    try { Invoke-UpdateOperation $Operations 'RemoveDirectory' @($Plan.install_dir) | Out-Null } catch { $errors.Add($_.Exception.Message) }
    try { Invoke-UpdateOperation $Operations 'MoveDirectory' @($Plan.rollback_dir, $Plan.install_dir) | Out-Null } catch { $errors.Add($_.Exception.Message) }
    try { Restore-FlowShiftState -Plan $Plan -Backup $Backup -Operations $Operations } catch { $errors.Add($_.Exception.Message) }
    try { Invoke-UpdateOperation $Operations 'StartTask' @($Plan.task_name) | Out-Null } catch { $errors.Add($_.Exception.Message) }
    if ($errors.Count -eq 0) {
        try {
            if (-not [bool](Invoke-UpdateOperation $Operations 'TestHealth' @($Plan, $Plan.from_version, $HealthTimeoutSec))) {
                $errors.Add('Restored runtime health validation timed out')
            }
        } catch { $errors.Add($_.Exception.Message) }
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
        [int]$HealthTimeoutSec = 45
    )
    $plan = Read-ValidatedUpdatePlan -Path $PlanPath
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
        if ($plan.package_kind -ceq 'source') {
            # Unpacked and checked before the installation is touched.
            $installScript = [string](Invoke-UpdateOperation $Operations 'ExpandSource' @($plan))
            Write-UpdateLog -Plan $plan -Message "source $($plan.to_version) unpacked: $installScript"
        }
        $backup = Backup-FlowShiftState -Plan $plan -Operations $Operations
        Prepare-FlowShiftRollback -Plan $plan -Operations $Operations
        $rollbackPrepared = $true
        Write-UpdateLog -Plan $plan -Message "previous version kept for rollback in $($plan.rollback_dir)"
        if ($plan.package_kind -ceq 'source') {
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
    Write-UpdateResult -Plan $plan -StartedAt $startedAt -Result $result -ErrorMessage $errorMessage
    $resultLevel = if ($result -eq 'success') { 'OK' } else { 'WARN' }
    Write-UpdateLog -Plan $plan -Message "result: $result" -Level $resultLevel
    if ($plan.package_kind -ceq 'source') {
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
