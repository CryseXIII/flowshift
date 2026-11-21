"""Checks the Python search of install_flowshift.ps1 without Windows.

The installer runs the WebGUI build first and then looks for Python. On
Windows PowerShell 5.1 the old probe (``& $py -c '...print(f"...")' 2>&1``)
could never succeed: PowerShell 5.1 passes the argument with its double quotes
unescaped, so Python receives a broken f-string and exits with a SyntaxError,
and the swallowed error became "Python not found". These tests reproduce that
argument passing, prove that the new probe text survives it unchanged, run the
real probe text with this Python, and check the installer's step order.
What only Windows can run lives in test_install_python_probe.ps1.
"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = (ROOT / "install_flowshift.ps1").read_text(encoding="utf-8")
OLD_PROBE = 'import struct,sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}|{struct.calcsize(chr(80))*8}")'


def ps51_legacy_quote(argument):
    """Windows PowerShell 5.1: wrap in quotes when there is whitespace, never escape inner quotes."""
    if argument and not re.search(r"\s", argument):
        return argument
    return '"' + argument + '"'


def windows_argv(command_line):
    """Split a command line like the Microsoft C runtime (CommandLineToArgvW rules)."""
    args, current, in_quotes, has_arg, i = [], [], False, False, 0
    while i < len(command_line):
        char = command_line[i]
        if char == "\\":
            j = i
            while j < len(command_line) and command_line[j] == "\\":
                j += 1
            count = j - i
            if j < len(command_line) and command_line[j] == '"':
                current.append("\\" * (count // 2))
                if count % 2:
                    current.append('"')
                    i = j + 1
                else:
                    i = j
                has_arg = True
                continue
            current.append("\\" * count)
            i = j
            has_arg = True
            continue
        if char == '"':
            if in_quotes and i + 1 < len(command_line) and command_line[i + 1] == '"':
                current.append('"')
                i += 2
                has_arg = True
                continue
            in_quotes = not in_quotes
            has_arg = True
            i += 1
            continue
        if char in " \t" and not in_quotes:
            if has_arg:
                args.append("".join(current))
                current, has_arg = [], False
            i += 1
            continue
        current.append(char)
        has_arg = True
        i += 1
    if has_arg:
        args.append("".join(current))
    return args


def installer_string(name):
    match = re.search(r"^\$" + name + r" = '([^']*)'\s*$", INSTALLER, re.M)
    if not match:
        raise AssertionError(f"${name} not found in install_flowshift.ps1")
    return match.group(1)


def probe_result_regex():
    body = INSTALLER[INSTALLER.index("function Test-PythonProbeResult"):]
    match = re.search(r"-notmatch '(\^\(\\d\+\)[^']*)'", body)
    if not match:
        raise AssertionError("probe output pattern not found in Test-PythonProbeResult")
    return match.group(1)


def main_flow():
    return INSTALLER[INSTALLER.index('Log "installer started'):]


class OldProbeExplainsTheFailure(unittest.TestCase):
    def test_old_probe_is_broken_by_powershell_51_argument_passing(self):
        received = windows_argv("py.exe -c " + ps51_legacy_quote(OLD_PROBE))[2:]
        self.assertNotEqual(received, [OLD_PROBE])
        result = subprocess.run([sys.executable, "-I", "-c", " ".join(received)], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SyntaxError", result.stderr)


class NewProbeTests(unittest.TestCase):
    def setUp(self):
        self.code = installer_string("PythonProbeCode")

    def test_probe_text_has_no_quotes_or_backslashes(self):
        self.assertNotRegex(self.code, r"[\"'`$\\\t\r\n]")

    def test_probe_survives_every_argument_passing_mode(self):
        arguments = re.search(r'^\$PythonProbeArguments = "(.*)"\s*$', INSTALLER, re.M).group(1)
        self.assertEqual(arguments, '-I -c `"$PythonProbeCode`"')
        command_line = "python.exe " + arguments.replace("`", "").replace("$PythonProbeCode", self.code)
        self.assertEqual(windows_argv(command_line)[1:], ["-I", "-c", self.code])
        self.assertEqual(windows_argv("py.exe -I -c " + ps51_legacy_quote(self.code))[1:], ["-I", "-c", self.code])

    def test_probe_output_matches_the_installer_pattern(self):
        env = dict(os.environ, PYTHONHOME="/flowshift-attrappe/nowhere", PYTHONPATH="/flowshift-attrappe/nowhere")
        result = subprocess.run([sys.executable, "-I", "-c", self.code], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        line = result.stdout.strip().splitlines()[-1]
        match = re.match(probe_result_regex(), line)
        self.assertIsNotNone(match, line)
        major, minor, micro, bits, exe = match.groups()
        self.assertEqual((int(major), int(minor), int(micro)), tuple(sys.version_info[:3]))
        self.assertIn(int(bits), (32, 64))
        self.assertEqual(Path(exe.strip()).resolve(), Path(sys.executable).resolve())

    def test_probe_runs_as_own_process_with_limits(self):
        body = INSTALLER[INSTALLER.index("function Invoke-PythonProbe"):INSTALLER.index("function ConvertTo-LogText")]
        for needle in ("RedirectStandardInput = $true", "StandardInput.Close()", "WaitForExit($TimeoutSec * 1000)",
                       "$process.Kill()", "'PYTHONHOME', 'PYTHONPATH'", "ReadToEndAsync()"):
            self.assertIn(needle, body)
        self.assertNotIn("2>&1", body)
        self.assertRegex(INSTALLER, r"(?m)^\$PythonProbeTimeoutSec = \d+$")

    def test_no_recursive_search_through_program_files(self):
        body = INSTALLER[INSTALLER.index("function Get-PythonCandidates"):INSTALLER.index("function Test-PythonProbeResult")]
        self.assertNotIn("-Recurse", body)
        self.assertIn(r"SOFTWARE\Python\PythonCore", body)
        self.assertIn("StartsWith($installPrefix", body)

    def test_each_candidate_is_logged_with_path_exit_code_and_output(self):
        body = INSTALLER[INSTALLER.index("function Resolve-PythonTool"):INSTALLER.index("function Invoke-NativeCapture")]
        self.assertRegex(body, r'python candidate \{0\}: exit=\{1\} time=\{2\}s stdout=\{3\} stderr=\{4\} -> \{5\}')
        self.assertNotIn("catch { }", body)


class InstallerFlowTests(unittest.TestCase):
    def test_steps_run_in_the_installer_order(self):
        flow = main_flow()
        steps = re.findall(r"^\s*Step '([^']+)'", flow, re.M)
        self.assertEqual(len(steps), int(re.search(r"\$TotalSteps\s+= (\d+)", INSTALLER).group(1)))
        order = [
            "Checking the WebGUI build",
            "Checking Python",
            "Installing Python (if missing)",
            "Stopping a running FlowShift (it is started again at the end)",
            "Installing application files and creating venv",
        ]
        self.assertEqual([s for s in steps if s in order], order)
        self.assertLess(flow.index("$webGuiSource = Resolve-WebGuiSource"), flow.index("$pyInfo = Resolve-PythonTool"))
        self.assertLess(flow.index("$pyInfo = Resolve-PythonTool"), flow.index("Stop-RunningFlowShift"))

    def test_nothing_is_changed_before_the_application_files_step(self):
        flow = main_flow()
        changed = flow.index("$script:InstallationChanged = $true")
        self.assertLess(flow.index("Step 'Installing application files and creating venv'"), changed)
        self.assertLess(changed, flow.index("Copy-Item"))
        # 21.11.: the program folder was emptied before the new source turned
        # out to be gone. The new files are staged and checked before the old
        # FlowShift is stopped; the folder is swapped only afterwards.
        staged = flow.index("New-StagedProgramFiles")
        self.assertLess(flow.index("Test-ProgramFiles $RepoDir"), staged)
        self.assertLess(staged, flow.index("Stop-RunningFlowShift"))
        self.assertLess(flow.index("Stop-RunningFlowShift"), flow.index("Move-InstallDirAside"))
        self.assertLess(flow.index("Move-InstallDirAside"), changed)
        self.assertLess(changed, flow.index("[System.IO.Directory]::Move($StagingDir, $InstallDir)"))
        self.assertNotIn("Remove-Item -Path $VenvDir", flow)

    def test_install_lock_is_taken_before_anything_is_checked(self):
        lock = INSTALLER.index("$script:InstallLock = Enter-InstallLock -DataDir $DataDir -Owner 'install'")
        self.assertLess(lock, INSTALLER.index("Step 'Checking administrator rights'"))
        self.assertIn("FLOWSHIFT_INSTALL_LOCK_TOKEN", INSTALLER)
        self.assertIn("[System.IO.FileShare]::Read)", INSTALLER)

    def test_manual_install_marker_is_written_before_the_runtime_starts(self):
        flow = main_flow()
        start = flow.index("Step 'Starting the FlowShift runtime (interactive session)'")
        marker = flow.index("WriteAllText($ManualInstallMarker", start)
        self.assertLess(marker, flow.index("Start-ScheduledTask -TaskName $TaskName", start))

    def test_previous_installation_is_removed_only_after_the_final_check(self):
        flow = main_flow()
        self.assertLess(flow.index("$problems = @(Test-InstalledFlowShift)"),
                        flow.index("Remove-Item -LiteralPath $PreviousDir -Recurse -Force -ErrorAction Stop\n                Log \"removed the previous installation"))
        fail = INSTALLER[INSTALLER.index("function Get-FailureStateNote"):INSTALLER.index("function Fail")]
        self.assertIn("Restore-PreviousInstallation", fail)

    def test_no_download_when_python_is_present_and_announcement_before_it(self):
        flow = main_flow()
        install_step = flow[flow.index("Step 'Installing Python (if missing)'"):flow.index("Step 'Stopping a running FlowShift")]
        present, missing = install_step.split("} elseif (-not $pyInfo) {", 1)
        self.assertNotIn("Install-Python", present)
        self.assertIn("nothing is downloaded", present)
        self.assertLess(missing.index("Test-Path -LiteralPath $pythonTargetExe"), missing.index("Install-Python"))
        self.assertLess(missing.index("now downloads Python $PythonVersion"), missing.index("Install-Python"))
        self.assertNotIn("Re-run after a reboot", INSTALLER)

    def test_python_setup_has_timeout_and_progress(self):
        body = INSTALLER[INSTALLER.index("function Install-Python"):INSTALLER.index("function Get-InstalledAppUninstallString")]
        self.assertNotIn("-Wait", body)
        self.assertIn("-TimeoutSec $PythonDownloadTimeoutSec", body)
        self.assertIn("WaitForExit(15000)", body)
        self.assertIn("Python setup still running", body)
        self.assertIn("$PythonSetupTimeoutSec", body)

    def test_failure_reports_state_of_previous_installation(self):
        fail = INSTALLER[INSTALLER.index("function Get-FailureStateNote"):INSTALLER.index("try {\n    $ProductVersion")]
        self.assertIn("Nothing was changed: FlowShift $($script:PreviousVersion) is still installed as before", fail)
        self.assertIn("Start-ScheduledTask -TaskName $TaskName", fail)
        self.assertIn("is running again (version $running)", fail)
        self.assertIn('Write-Host "Status : $stateNote"', fail)

    def test_webgui_helper_output_does_not_leak_into_return_value(self):
        body = INSTALLER[INSTALLER.index("function Resolve-WebGuiSource"):INSTALLER.index("function Install-WebGuiAssets")]
        self.assertIn("-LogPath $InstallLog | Out-Host", body)

    def test_native_calls_with_stderr_do_not_use_bare_redirection(self):
        flow = main_flow()
        self.assertNotIn("2>&1", flow)

    def test_windows_check_runs_in_ci(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn(r".\test_install_python_probe.ps1", workflow)
        self.assertTrue((ROOT / "test_install_python_probe.ps1").is_file())


if __name__ == "__main__":
    unittest.main()
