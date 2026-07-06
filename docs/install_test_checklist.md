# FlowShift Install / Uninstall Test Checklist

These are **manual** tests. The installer touches machine-wide state (services,
Program Files, Program Data, shortcuts, and possibly a Python install), so it
cannot be exercised in the automated pure-logic test suite. Run these on real
Windows machines.

## Files under test

- `install_flowshift.bat` — double-click launcher (bypasses ExecutionPolicy).
- `install_flowshift.ps1` — the installer (self-elevates via UAC).
- `uninstall_flowshift.bat` / `uninstall_flowshift.ps1` — remover.

## Install targets

| What | Path |
|---|---|
| Program files | `%ProgramFiles%\FlowShift` |
| venv | `%ProgramFiles%\FlowShift\.venv` |
| NSSM | `%ProgramFiles%\FlowShift\tools\nssm\nssm.exe` |
| Config | `%ProgramData%\FlowShift\config.json` |
| Logs | `%ProgramData%\FlowShift\logs\` (install.log, runtime.out, runtime.err) |
| Service | `FlowShiftRuntime` |
| Desktop shortcut | `%Public%\Desktop\FlowShift.lnk` |
| Start Menu | `%ProgramData%\Microsoft\Windows\Start Menu\Programs\FlowShift\` |

## A. Fresh Windows WITHOUT Python

- [ ] Copy/clone the repo to the machine.
- [ ] Double-click `install_flowshift.bat`.
- [ ] UAC prompt appears; accept it.
- [ ] Step `[2/12] Checking Python` reports Python missing.
- [ ] Step `[3/12] Installing Python` installs Python (winget or python.org silent).
      **If this fails** (no internet / winget), installer stops with a clear
      message pointing to python.org and the log path. → note it and install
      Python manually, then re-run.
- [ ] Steps 4–12 complete; window shows `INSTALLATION COMPLETE`.

## B. Windows WITH Python already present

- [ ] Double-click `install_flowshift.bat`.
- [ ] Step 2 finds Python (>= 3.9); step 3 skips install.
- [ ] Remaining steps complete.

## C. Elevation behaviour

- [ ] Launch `install_flowshift.bat` as a normal (non-admin) user.
- [ ] The PowerShell script self-elevates (single UAC prompt).
- [ ] The elevated window shows all 12 numbered steps and stays open at the end.

## D. Progress + logging

- [ ] Each step is shown as `[n/12] ...`.
- [ ] On any failure the window stays open, shows the reason and the log path,
      and returns a non-zero exit code.
- [ ] `%ProgramData%\FlowShift\logs\install.log` contains the full run.

## E. Service

- [ ] `Get-Service FlowShiftRuntime` shows the service exists.
- [ ] Service `Status` is `Running` right after install (step 10).
- [ ] `nssm.exe` config: Application = venv python, Arguments = `tray.py --tray`,
      AppDirectory = `...\src\python`, stdout/stderr in ProgramData logs,
      env `FLOWSHIFT_CONFIG` + `FLOWSHIFT_LOG_DIR` set.
- [ ] `Stop-Service FlowShiftRuntime` then `Start-Service FlowShiftRuntime` works.
- [ ] No CMD window pops up during normal service start.
- [ ] No UAC prompt on normal service start (only installer/uninstaller elevate).

### E-caveat (MUST verify): session-0 input limitation

- [ ] **Known risk:** a service runs in session 0 and generally CANNOT capture or
      inject interactive input for the logged-on user. Verify whether input
      forwarding actually works with the service running:
  - [ ] If forwarding does NOT work from the service, run the runtime in the user
        session instead (GUI/Tray autostart, or a Scheduled Task with
        "run only when user is logged on", highest privileges). Document the
        outcome. The control socket + GUI status will still work either way.

## F. Control socket

- [ ] Step 11 reports `control socket reachable`, OR
- [ ] If not reachable, `runtime.err` in the logs explains why (session-0 note).

## G. GUI shortcut

- [ ] Desktop `FlowShift` icon exists.
- [ ] Double-click opens the GUI (no CMD console window stays open; uses pythonw).
- [ ] GUI shows service/runtime status (network / forwarding / capture separated).
- [ ] Start Menu `FlowShift\FlowShift GUI`, `FlowShift Logs`, `Uninstall FlowShift`
      exist and work.

## H. Reboot

- [ ] Reboot the machine.
- [ ] Service `FlowShiftRuntime` starts automatically (SERVICE_AUTO_START).
- [ ] GUI shows status after login.

## I. Uninstall

- [ ] Double-click `uninstall_flowshift.bat` (or Start Menu → Uninstall FlowShift).
- [ ] UAC prompt; accept.
- [ ] Steps 1–6 shown.
- [ ] Service `FlowShiftRuntime` is gone (`Get-Service` errors / not found).
- [ ] Desktop icon gone.
- [ ] Start Menu `FlowShift` folder gone.
- [ ] `%ProgramFiles%\FlowShift` gone.
- [ ] No FlowShift processes remain on ports 45781 / 45782.
- [ ] Prompt asks whether to delete `%ProgramData%\FlowShift`:
  - [ ] Answering "no" keeps config/logs.
  - [ ] Answering "yes" removes the data folder.

## J. Reinstall

- [ ] After uninstall, run `install_flowshift.bat` again.
- [ ] Install succeeds cleanly (idempotent: old service removed first).

## K. Repo hygiene during all of the above

- [ ] The repo working copy never gains `config.json`, `flowshift.log`,
      `flowshift_runtime.out`, or `__pycache__` as tracked files
      (runtime data lives in `%ProgramData%\FlowShift`, not in the repo).

---

**Status:** These tests require real Windows machines (ideally one truly fresh
without Python) and were NOT run in the development environment. The PowerShell
scripts pass the language parser; the service/session-0 behaviour, Python
auto-install, NSSM download, and shortcut creation must be verified on hardware.
