param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'update_flowshift.ps1') -LibraryOnly

$script:Passed = 0
$script:Failed = 0
$script:OriginalProgramFiles = $env:ProgramFiles
$script:OriginalProgramData = $env:ProgramData

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function New-TestFixture {
    param(
        [int]$InstallerExitCode = 0,
        [bool]$NewHealth = $true,
        [bool]$OldHealth = $true,
        [bool]$RuntimeStuck = $false,
        [bool]$FailRollbackMove = $false,
        [bool]$ModifyUserData = $false
    )
    $root = Join-Path ([System.IO.Path]::GetTempPath()) ('flowshift-updater-test-' + [guid]::NewGuid().ToString('N'))
    $programFiles = Join-Path $root 'Program Files'
    $programData = Join-Path $root 'ProgramData'
    $installDir = Join-Path $programFiles 'FlowShift'
    $dataDir = Join-Path $programData 'FlowShift'
    $downloadDir = Join-Path $dataDir 'updates\downloads'
    [System.IO.Directory]::CreateDirectory($installDir) | Out-Null
    [System.IO.Directory]::CreateDirectory($downloadDir) | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $installDir 'VERSION'), "0.4.0`n")
    [System.IO.File]::WriteAllText((Join-Path $dataDir 'config.json'), '{"device_id":"preserve-me"}')
    [System.IO.File]::WriteAllText((Join-Path $dataDir 'install_state.json'), '{"installed_by_flowshift":{"python":false,"nodejs":true,"vite":true}}')
    [System.IO.File]::WriteAllText((Join-Path $dataDir 'update_state.json'), '{"state":"downloaded"}')
    [System.IO.Directory]::CreateDirectory((Join-Path $dataDir 'clipboard\objects')) | Out-Null
    [System.IO.Directory]::CreateDirectory((Join-Path $dataDir 'logs')) | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $dataDir 'clipboard\objects\object.json'), '{"large":"untouched"}')
    [System.IO.File]::WriteAllText((Join-Path $dataDir 'logs\runtime.json'), '{"log":"untouched"}')

    $installer = Join-Path $downloadDir 'FlowShift-Setup-0.5.0.exe'
    [System.IO.File]::WriteAllBytes($installer, [Text.Encoding]::UTF8.GetBytes('mock setup payload'))
    $digest = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    $planPath = Join-Path $dataDir 'updates\update_plan.json'
    $plan = [ordered]@{
        schema_version = 1
        from_version = '0.4.0'
        to_version = '0.5.0'
        installer_path = $installer
        installer_size = [long](Get-Item -LiteralPath $installer).Length
        installer_sha256 = $digest
        install_dir = $installDir
        data_dir = $dataDir
        created_at = '2026-07-21T12:00:00Z'
        request_id = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        runtime_pid = 4242
        task_name = 'FlowShift'
        control_port = 45782
        peer_port = 45781
        api_port = 5000
    }
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $planPath)) | Out-Null
    [System.IO.File]::WriteAllText($planPath, ($plan | ConvertTo-Json -Depth 8))
    $env:ProgramFiles = $programFiles
    $env:ProgramData = $programData

    $state = @{
        InstallerExitCode = $InstallerExitCode
        NewHealth = $NewHealth
        OldHealth = $OldHealth
        RuntimeStuck = $RuntimeStuck
        FailRollbackMove = $FailRollbackMove
        ModifyUserData = $ModifyUserData
        InstallerCalls = 0
        Starts = 0
        Stops = 0
        Exports = 0
        Imports = 0
        InstallerArguments = $null
    }
    $capturedState = $state
    $capturedInstall = $installDir
    $capturedData = $dataDir
    $operations = @{
        ProcessExists = { param($ProcessId) return [bool]$capturedState.RuntimeStuck }.GetNewClosure()
        PortOpen = { param($Port) return [bool]$capturedState.RuntimeStuck }.GetNewClosure()
        ExportTaskXml = {
            param($Name)
            $capturedState.Exports++
            return '<Task version="1.2"><RegistrationInfo /></Task>'
        }.GetNewClosure()
        ImportTaskXml = {
            param($Name, $XmlPath)
            if (-not (Test-Path -LiteralPath $XmlPath -PathType Leaf)) { throw 'task XML missing' }
            $capturedState.Imports++
        }.GetNewClosure()
        TaskExists = { param($Name) return $true }.GetNewClosure()
        StopTask = { param($Name) $capturedState.Stops++ }.GetNewClosure()
        StartTask = { param($Name) $capturedState.Starts++ }.GetNewClosure()
        RemoveDirectory = {
            param($Path)
            if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
        }.GetNewClosure()
        MoveDirectory = {
            param($Source, $Destination)
            if ($capturedState.FailRollbackMove -and $Source -like '*.rollback-*') {
                throw 'simulated rollback move failure'
            }
            [System.IO.Directory]::Move([System.IO.Path]::GetFullPath($Source), [System.IO.Path]::GetFullPath($Destination))
        }.GetNewClosure()
        RunInstaller = {
            param($InstallerPath, $Arguments)
            $capturedState.InstallerCalls++
            $capturedState.InstallerArguments = @($Arguments)
            if ($capturedState.InstallerExitCode -eq 0) {
                [System.IO.Directory]::CreateDirectory($capturedInstall) | Out-Null
                [System.IO.File]::WriteAllText((Join-Path $capturedInstall 'VERSION'), "0.5.0`n")
                if ($capturedState.ModifyUserData) {
                    [System.IO.File]::WriteAllText((Join-Path $capturedData 'config.json'), '{"changed":true}')
                }
                [System.IO.File]::WriteAllText((Join-Path $capturedData 'install_state.json'), '{"installed_by_flowshift":{"python":true}}')
            }
            return [int]$capturedState.InstallerExitCode
        }.GetNewClosure()
        TestHealth = {
            param($ValidatedPlan, $Version, $TimeoutSec)
            if ($Version -eq '0.5.0') { return [bool]$capturedState.NewHealth }
            return [bool]$capturedState.OldHealth
        }.GetNewClosure()
        Sleep = { param($Milliseconds) }.GetNewClosure()
    }
    return [pscustomobject]@{
        Root = $root
        ProgramFiles = $programFiles
        ProgramData = $programData
        InstallDir = $installDir
        DataDir = $dataDir
        Installer = $installer
        PlanPath = $planPath
        State = $state
        Operations = $operations
    }
}

function Remove-TestFixture {
    param([object]$Fixture)
    if ($null -ne $Fixture -and (Test-Path -LiteralPath $Fixture.Root)) {
        Remove-Item -LiteralPath $Fixture.Root -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Get-TestResult {
    param([object]$Fixture)
    return ([System.IO.File]::ReadAllText((Join-Path $Fixture.DataDir 'updates\last_update_result.json')) | ConvertFrom-Json)
}

function Invoke-UpdaterTest {
    param([string]$Name, [scriptblock]$Body)
    try {
        & $Body
        $script:Passed++
        Write-Host "[PASS] $Name"
    } catch {
        $script:Failed++
        Write-Host "[FAIL] $Name - $($_.Exception.Message)" -ForegroundColor Red
    }
}

Invoke-UpdaterTest 'successful install keeps rollback and preserves user data' {
    $fixture = New-TestFixture
    try {
        $outcome = Invoke-FlowShiftUpdate -PlanPath $fixture.PlanPath -Operations $fixture.Operations -RuntimeExitTimeoutSec 0 -HealthTimeoutSec 0
        Assert-True ($outcome.Result -eq 'success') 'success result expected'
        $installedVersion = [System.IO.File]::ReadAllText((Join-Path $fixture.InstallDir 'VERSION'))
        Assert-True ($installedVersion.Trim() -eq '0.5.0') 'target VERSION missing'
        Assert-True (Test-Path -LiteralPath (Join-Path $fixture.ProgramFiles 'FlowShift.rollback-0.4.0')) 'rollback directory missing'
        Assert-True (([System.IO.File]::ReadAllText((Join-Path $fixture.DataDir 'config.json'))) -eq '{"device_id":"preserve-me"}') 'config changed'
        Assert-True (([System.IO.File]::ReadAllText((Join-Path $fixture.DataDir 'install_state.json'))) -eq '{"installed_by_flowshift":{"python":false,"nodejs":true,"vite":true}}') 'install_state ownership changed'
        Assert-True (([System.IO.File]::ReadAllText((Join-Path $fixture.DataDir 'clipboard\objects\object.json'))) -eq '{"large":"untouched"}') 'clipboard object changed'
        Assert-True (($fixture.State.InstallerArguments -join ' ') -eq '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /FLOWUPDATE') 'installer arguments were not fixed'
        $manifestPath = @(Get-ChildItem -LiteralPath (Join-Path $fixture.DataDir 'backups') -Filter 'backup_manifest.json' -Recurse)[0].FullName
        $manifest = [System.IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
        Assert-True (@($manifest.files.name) -contains 'config.json') 'root config was not backed up'
        Assert-True (@($manifest.files.name) -notcontains 'object.json') 'clipboard object was duplicated'
        Assert-True (@($manifest.files.name) -notcontains 'runtime.json') 'logs were duplicated'
    } finally { Remove-TestFixture $fixture }
}

Invoke-UpdaterTest 'installer nonzero restores old install and task' {
    $fixture = New-TestFixture -InstallerExitCode 7
    try {
        $outcome = Invoke-FlowShiftUpdate -PlanPath $fixture.PlanPath -Operations $fixture.Operations -RuntimeExitTimeoutSec 0 -HealthTimeoutSec 0
        Assert-True ($outcome.Result -eq 'rollback_success') 'rollback_success expected'
        $installedVersion = [System.IO.File]::ReadAllText((Join-Path $fixture.InstallDir 'VERSION'))
        Assert-True ($installedVersion.Trim() -eq '0.4.0') 'old VERSION not restored'
        Assert-True ($fixture.State.Imports -eq 1) 'scheduled task was not restored'
        Assert-True ((Get-TestResult $fixture).result -eq 'rollback_success') 'rollback result was not persisted'
    } finally { Remove-TestFixture $fixture }
}

Invoke-UpdaterTest 'new health timeout restores backed-up JSON' {
    $fixture = New-TestFixture -NewHealth $false -ModifyUserData $true
    try {
        $outcome = Invoke-FlowShiftUpdate -PlanPath $fixture.PlanPath -Operations $fixture.Operations -RuntimeExitTimeoutSec 0 -HealthTimeoutSec 0
        Assert-True ($outcome.Result -eq 'rollback_success') 'health failure should roll back'
        Assert-True (([System.IO.File]::ReadAllText((Join-Path $fixture.DataDir 'config.json'))) -eq '{"device_id":"preserve-me"}') 'backed-up config not restored'
        Assert-True ($fixture.State.Stops -eq 1) 'partial runtime was not stopped'
    } finally { Remove-TestFixture $fixture }
}

Invoke-UpdaterTest 'rollback failure is recorded' {
    $fixture = New-TestFixture -InstallerExitCode 9 -FailRollbackMove $true
    try {
        $outcome = Invoke-FlowShiftUpdate -PlanPath $fixture.PlanPath -Operations $fixture.Operations -RuntimeExitTimeoutSec 0 -HealthTimeoutSec 0
        Assert-True ($outcome.Result -eq 'rollback_failed') 'rollback_failed expected'
        Assert-True ((Get-TestResult $fixture).result -eq 'rollback_failed') 'rollback failure was not persisted'
        Assert-True ($outcome.Error -match 'simulated rollback move failure') 'rollback error missing'
    } finally { Remove-TestFixture $fixture }
}

Invoke-UpdaterTest 'missing and corrupt installer are rejected before ack' {
    $fixture = New-TestFixture
    try {
        Remove-Item -LiteralPath $fixture.Installer -Force
        $missingRejected = $false
        try { $null = Invoke-FlowShiftUpdate -PlanPath $fixture.PlanPath -Operations $fixture.Operations } catch { $missingRejected = $true }
        Assert-True $missingRejected 'missing installer accepted'
        [System.IO.File]::WriteAllBytes($fixture.Installer, [Text.Encoding]::UTF8.GetBytes('tampered payload!!'))
        $corruptRejected = $false
        try { $null = Invoke-FlowShiftUpdate -PlanPath $fixture.PlanPath -Operations $fixture.Operations } catch { $corruptRejected = $true }
        Assert-True $corruptRejected 'corrupt installer accepted'
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $fixture.DataDir 'updates\acks\update_ack-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json'))) 'ack written for invalid installer'
    } finally { Remove-TestFixture $fixture }
}

Invoke-UpdaterTest 'runtime shutdown timeout does not alter installation' {
    $fixture = New-TestFixture -RuntimeStuck $true
    try {
        $outcome = Invoke-FlowShiftUpdate -PlanPath $fixture.PlanPath -Operations $fixture.Operations -RuntimeExitTimeoutSec 0 -HealthTimeoutSec 0
        Assert-True ($outcome.Result -eq 'failed') 'plain failure expected before rollback preparation'
        Assert-True ($fixture.State.InstallerCalls -eq 0) 'installer ran while runtime was alive'
        $installedVersion = [System.IO.File]::ReadAllText((Join-Path $fixture.InstallDir 'VERSION'))
        Assert-True ($installedVersion.Trim() -eq '0.4.0') 'installation changed during timeout'
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $fixture.ProgramFiles 'FlowShift.rollback-0.4.0'))) 'rollback created before runtime exit'
    } finally { Remove-TestFixture $fixture }
}

Invoke-UpdaterTest 'scheduled task XML is backed up and restored' {
    $fixture = New-TestFixture -InstallerExitCode 5
    try {
        $outcome = Invoke-FlowShiftUpdate -PlanPath $fixture.PlanPath -Operations $fixture.Operations -RuntimeExitTimeoutSec 0 -HealthTimeoutSec 0
        Assert-True ($outcome.Result -eq 'rollback_success') 'rollback_success expected'
        Assert-True ($fixture.State.Exports -eq 1) 'scheduled task was not exported once'
        Assert-True ($fixture.State.Imports -eq 1) 'scheduled task was not imported once'
        Assert-True (Test-Path -LiteralPath $outcome.Backup.TaskPath -PathType Leaf) 'scheduled task XML backup missing'
    } finally { Remove-TestFixture $fixture }
}

$env:ProgramFiles = $script:OriginalProgramFiles
$env:ProgramData = $script:OriginalProgramData
Write-Host "PowerShell updater tests: $script:Passed passed, $script:Failed failed"
if ($script:Failed -ne 0) { exit 1 }
