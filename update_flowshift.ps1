param(
    [string]$PlanPath,
    [switch]$Elevated,
    [switch]$LibraryOnly
)

$ErrorActionPreference = 'Stop'
$script:SemVerPattern = '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$'
$script:TaskName = 'FlowShift'
$script:ControlPort = 45782
$script:PeerPort = 45781
$script:ApiPort = 5000
$script:MaxUserJsonBytes = 10MB

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
    $null = $Left -match $script:SemVerPattern
    $leftParts = @([uint64]$Matches[1], [uint64]$Matches[2], [uint64]$Matches[3])
    $null = $Right -match $script:SemVerPattern
    $rightParts = @([uint64]$Matches[1], [uint64]$Matches[2], [uint64]$Matches[3])
    for ($index = 0; $index -lt 3; $index++) {
        if ($leftParts[$index] -lt $rightParts[$index]) { return -1 }
        if ($leftParts[$index] -gt $rightParts[$index]) { return 1 }
    }
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
        throw 'Update plan versions must be canonical stable SemVer'
    }
    if ((Compare-SemVer $toVersion $fromVersion) -le 0) { throw 'Update target must be newer than installed version' }

    $installDir = Assert-StringProperty $plan 'install_dir'
    $dataDir = Assert-StringProperty $plan 'data_dir'
    if (-not (Test-SamePath $installDir $expectedInstall)) { throw 'Update plan install_dir is not official' }
    if (-not (Test-SamePath $dataDir $expectedData)) { throw 'Update plan data_dir is not official' }

    $installerPath = Assert-StringProperty $plan 'installer_path'
    $expectedInstaller = Join-Path $expectedData "updates\downloads\FlowShift-Setup-$toVersion.exe"
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
    $plan | Add-Member -NotePropertyName rollback_dir -NotePropertyValue (
        Join-Path (Split-Path -Parent $expectedInstall) "FlowShift.rollback-$fromVersion") -Force
    return $plan
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
        RunInstaller = {
            param([string]$Installer, [string[]]$Arguments)
            $process = Start-Process -FilePath $Installer -ArgumentList $Arguments -Wait -PassThru
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
    Invoke-UpdateOperation $Operations 'MoveDirectory' @($Plan.install_dir, $Plan.rollback_dir) | Out-Null
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
            try {
                $null = Invoke-WebRequest -Uri "http://127.0.0.1:$($Plan.api_port)/overlay.html" -UseBasicParsing -TimeoutSec 2
            } catch { }
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
    $startedAt = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
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
        $backup = Backup-FlowShiftState -Plan $plan -Operations $Operations
        Prepare-FlowShiftRollback -Plan $plan -Operations $Operations
        $rollbackPrepared = $true
        $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/FLOWUPDATE')
        $exitCode = [int](Invoke-UpdateOperation $Operations 'RunInstaller' @($plan.installer_path, $arguments))
        if ($exitCode -ne 0) { throw "Setup exited with code $exitCode" }
        Restore-InstallOwnershipState -Plan $plan -Backup $backup
        Invoke-UpdateOperation $Operations 'StartTask' @($plan.task_name) | Out-Null
        if (-not [bool](Invoke-UpdateOperation $Operations 'TestHealth' @($plan, $plan.to_version, $HealthTimeoutSec))) {
            throw 'Post-install health validation timed out'
        }
        $result = 'success'
    } catch {
        $errorMessage = $_.Exception.Message
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
        $validated = Read-ValidatedUpdatePlan -Path $PlanPath
        Assert-ExternalRunnerLocation -Plan $validated
        if (-not (Test-Administrator)) {
            $hostExecutable = (Get-Process -Id $PID).Path
            $quotedScript = '"' + $PSCommandPath.Replace('"', '""') + '"'
            $quotedPlan = '"' + $validated.plan_path.Replace('"', '""') + '"'
            $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $quotedScript,
                '-PlanPath', $quotedPlan, '-Elevated')
            Start-Process -FilePath $hostExecutable -Verb RunAs -ArgumentList $arguments | Out-Null
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
