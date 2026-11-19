# Windows check for the Python search of install_flowshift.ps1.
#
# Loads the real functions from install_flowshift.ps1 (via the PowerShell
# parser, the installer itself is not run) and executes them in the order of
# the installer: first the WebGUI build step, then the Python search, all under
# $ErrorActionPreference = 'Stop' like the installer.
#
#   .\test_install_python_probe.ps1                    simulated WebGUI build (child
#                                                      PowerShell writing to stdout/stderr)
#   .\test_install_python_probe.ps1 -RealWebGuiBuild   runs install_webgui.ps1 (needs Node.js 24+)
#
# Run it with Windows PowerShell 5.1 (powershell.exe, what install_flowshift.bat
# uses) and, if installed, with PowerShell 7 (pwsh.exe).

param([switch]$RealWebGuiBuild)

$ErrorActionPreference = 'Stop'
$RepoDir = $PSScriptRoot
$installerPath = Join-Path $RepoDir 'install_flowshift.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($installerPath, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw "install_flowshift.ps1 does not parse: $($errors[0].Message)" }

$wantedVariables = @('MinPythonMinor', 'MaxPythonMinor', 'PythonVersion', 'PythonProbeCode', 'PythonProbeArguments', 'PythonProbeTimeoutSec')
$wantedFunctions = @('Invoke-PythonProbe', 'ConvertTo-LogText', 'Get-PythonCandidates', 'Test-PythonProbeResult', 'Resolve-PythonTool', 'Invoke-NativeCapture')
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $statement.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
            $statement.Left.VariablePath.UserPath -in $wantedVariables) {
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}
$functionAsts = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $wantedFunctions }, $false))
foreach ($functionAst in $functionAsts) { . ([scriptblock]::Create($functionAst.Extent.Text)) }
if ($functionAsts.Count -ne $wantedFunctions.Count) { throw "expected $($wantedFunctions.Count) installer functions, found $($functionAsts.Count)" }

$InstallDir = Join-Path ([System.IO.Path]::GetTempPath()) ('flowshift-python-probe-' + [guid]::NewGuid().ToString('N'))
$script:LogLines = New-Object System.Collections.Generic.List[string]
function Log {
    param([string]$Message, [string]$Level = 'INFO')
    $script:LogLines.Add("[$Level] $Message")
    Write-Host "  [$Level] $Message"
}

$script:Passed = 0
$script:Failed = 0
function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}
function Invoke-Case {
    param([string]$Name, [scriptblock]$Body)
    Write-Host "CASE $Name"
    try {
        & $Body
        $script:Passed++
        Write-Host "PASS $Name" -ForegroundColor Green
    } catch {
        $script:Failed++
        Write-Host "FAIL $Name : $($_.Exception.Message)" -ForegroundColor Red
    }
}

$hostExe = (Get-Process -Id $PID).Path
Write-Host ("host: PowerShell {0} ({1})" -f $PSVersionTable.PSVersion, $hostExe)
$script:FoundPython = $null

Invoke-Case 'WebGUI build, then Python search (installer order)' {
    if ($RealWebGuiBuild) {
        $version = ([System.IO.File]::ReadAllText((Join-Path $RepoDir 'VERSION'))).Trim()
        & $hostExe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoDir 'install_webgui.ps1') `
            -WebSource (Join-Path $RepoDir 'webgui') -ExpectedVersion $version | Out-Host
        Assert-True ($LASTEXITCODE -eq 0) "install_webgui.ps1 exited with $LASTEXITCODE"
    } else {
        # Like npm during the build: output on stdout and warnings on stderr.
        & $hostExe -NoProfile -Command "Write-Output 'npm notice build'; [Console]::Error.WriteLine('npm warn deprecated attrappe'); exit 0" | Out-Host
        Assert-True ($LASTEXITCODE -eq 0) "simulated WebGUI build exited with $LASTEXITCODE"
    }
    # Hostile leftovers in the environment must not break the search.
    $saved = @{}
    foreach ($name in @('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV')) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
    try {
        $env:PYTHONHOME = 'C:\flowshift-attrappe\nowhere'
        $env:PYTHONPATH = 'C:\flowshift-attrappe\nowhere'
        $env:VIRTUAL_ENV = 'C:\flowshift-attrappe\venv'
        $script:LogLines.Clear()
        $script:FoundPython = Resolve-PythonTool
    } finally {
        foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
    }
    Assert-True ($null -ne $script:FoundPython) 'Resolve-PythonTool found no Python although one is installed (see the candidate lines above)'
    Assert-True (Test-Path -LiteralPath $script:FoundPython.Exe -PathType Leaf) "reported executable does not exist: $($script:FoundPython.Exe)"
    $candidateLines = @($script:LogLines | Where-Object { $_ -match 'python candidate .*: exit=.* time=.*s stdout=.* stderr=.* -> ' })
    Assert-True ($candidateLines.Count -ge 1) 'no per-candidate log line (path, exit code, output)'
}

Invoke-Case 'old probe style on this host (information only)' {
    if (-not $script:FoundPython) { Write-Host '  skipped: no Python found'; return }
    $oldResult = $null
    try {
        $oldResult = & $script:FoundPython.Exe -c 'import struct,sys; print(f"{sys.version_info.major}.{sys.version_info.minor}|{struct.calcsize(chr(80))*8}")' 2>&1
        Write-Host "  old probe: exit=$LASTEXITCODE output=$oldResult"
    } catch {
        Write-Host "  old probe threw: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

Invoke-Case 'probe has a time limit' {
    if (-not $script:FoundPython) { throw 'no Python found' }
    $probe = Invoke-PythonProbe -Exe $script:FoundPython.Exe -Arguments '-I -c "import time;time.sleep(60)"' -TimeoutSec 2
    Assert-True $probe.TimedOut 'a hanging candidate was not stopped'
    Assert-True ($probe.Seconds -lt 15) "stopping took $($probe.Seconds) s"
    $verdict = Test-PythonProbeResult $probe
    Assert-True (-not $verdict.Usable) 'a timed-out candidate counted as usable'
}

Invoke-Case 'failing candidate reports exit code and stderr' {
    $probe = Invoke-PythonProbe -Exe $env:ComSpec -Arguments '/c echo broken-attrappe 1>&2 & exit 7'
    Assert-True ($probe.ExitCode -eq 7) "exit code was $($probe.ExitCode)"
    Assert-True ($probe.Error -match 'broken-attrappe') "stderr was '$($probe.Error)'"
    $verdict = Test-PythonProbeResult $probe
    Assert-True ((-not $verdict.Usable) -and $verdict.Reason -eq 'exit code 7') "verdict was '$($verdict.Reason)'"
}

Invoke-Case 'unsupported version is rejected with a reason' {
    if (-not $script:FoundPython) { throw 'no Python found' }
    $probe = Invoke-PythonProbe -Exe $script:FoundPython.Exe -Arguments '-I -c "print(chr(51),8,1,64,chr(120),sep=chr(124))"'
    $verdict = Test-PythonProbeResult $probe
    Assert-True ((-not $verdict.Usable) -and $verdict.Reason -match 'outside the supported') "verdict was '$($verdict.Reason)'"
}

Invoke-Case 'stderr of a console program does not abort under Stop' {
    if (-not $script:FoundPython) { throw 'no Python found' }
    $run = Invoke-NativeCapture $script:FoundPython.Exe @('-I', '-c', 'import sys;sys.stderr.write(chr(119)*3)')
    Assert-True ($run.ExitCode -eq 0) "exit code was $($run.ExitCode)"
    Assert-True ($run.Output -match 'www') "output was '$($run.Output)'"
}

Write-Host ''
Write-Host "Python probe checks: $($script:Passed) passed, $($script:Failed) failed"
if ($script:Failed -gt 0) { exit 1 }
exit 0
