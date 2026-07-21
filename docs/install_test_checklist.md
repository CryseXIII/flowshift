# FlowShift Install / Uninstall Test Checklist

These are **manual** tests. The installer touches machine-wide state (services,
Program Files, Program Data, shortcuts, and possibly a Python install), so it
cannot be exercised in the automated pure-logic test suite. Run these on real
Windows machines.

## Files under test

- `install_flowshift.bat` — double-click launcher (bypasses ExecutionPolicy).
- `install_flowshift.ps1` — the installer (self-elevates via UAC).
- `FlowShift-Setup.exe` - packaged setup with a curated payload and prebuilt WebGUI.
- `update_flowshift.ps1` - external update, health-check, and rollback runner.
- `uninstall_flowshift.bat` / `uninstall_flowshift.ps1` — remover.

## Install targets

| What | Path |
|---|---|
| Program files | `%ProgramFiles%\FlowShift` |
| venv | `%ProgramFiles%\FlowShift\.venv` |
| NSSM | `%ProgramFiles%\FlowShift\tools\nssm\nssm.exe` |
| Config | `%ProgramData%\FlowShift\config.json` |
| Logs | `%ProgramData%\FlowShift\logs\` (install.log, runtime.out, runtime.err) |
| Autostart (primary) | Scheduled Task `FlowShift` (AtLogOn, interactive user session) |
| NSSM helper (optional, `-WithNssm`) | Service `FlowShiftRuntime` (manual start, NOT input path) |
| Desktop shortcut | `%Public%\Desktop\FlowShift.lnk` |
| Start Menu | `%ProgramData%\Microsoft\Windows\Start Menu\Programs\FlowShift\` |

## A. Fresh Windows WITHOUT Python

- [ ] Download and run `FlowShift-Setup.exe` on a clean Windows x64 VM.
- [ ] UAC prompt appears; accept it.
- [ ] Inno setup remains responsive; no PowerShell prompt or console blocks it.
- [ ] `install.log` step `[2/13] Checking Python` reports Python missing.
- [ ] `install.log` step `[3/13] Installing Python` records Python installation
      through winget or the python.org fallback.
      **If this fails** (no internet / winget), installer stops with a clear
      message pointing to python.org and the log path. → note it and install
      Python manually, then re-run.
- [ ] `install.log` records steps 4-13 and Inno setup completes successfully.
- [ ] WebGUI and `overlay.html` are installed without Node.js/npm on the VM.

## B. Windows WITH Python already present

- [ ] Double-click `install_flowshift.bat`.
- [ ] Step 2 finds Python (>= 3.9); step 3 skips install.
- [ ] Remaining steps complete.

## C. Elevation behaviour

- [ ] Launch `install_flowshift.bat` as a normal (non-admin) user.
- [ ] The PowerShell script self-elevates (single UAC prompt).
- [ ] The elevated window shows all 13 numbered steps and stays open at the end.

## D. Progress + logging

- [ ] Each step is shown as `[n/13] ...`.
- [ ] On any failure the window stays open, shows the reason and the log path,
      and returns a non-zero exit code.
- [ ] `%ProgramData%\FlowShift\logs\install.log` contains the full run.

## E. Autostart in the interactive user session (primary path)

- [ ] After install, the Scheduled Task `FlowShift` exists
      (`Get-ScheduledTask -TaskName FlowShift`), trigger AtLogOn, principal =
      the interactive user, RunLevel Highest, LogonType Interactive.
- [ ] The installer started the runtime now (`Start-ScheduledTask FlowShift`),
      and the control socket is reachable (step 12).
- [ ] The runtime process runs in the **interactive session** (session id != 0),
      NOT session 0. Verify in the GUI: `Session: <id> interaktiv` (green),
      and `Runtime: gesund (alle Worker aktiv)` (green).
- [ ] No CMD window pops up (pythonw). No per-start UAC (task RunLevel Highest).
- [ ] Log off / log on: the task auto-starts the runtime in the user session.

### E-worker-health (regression guard for the forward_loop crash)

- [ ] GUI status shows `Runtime: gesund (alle Worker aktiv)` (green).
- [ ] `status.workers.forward_loop.alive == true`, `inject_loop.alive == true`.
- [ ] The `Pipeline:` line updates while forwarding (queued/forwarded/injected
      counters move).
- [ ] Automated: `python src/python/worker_smoke_test.py` passes (Test A/B/C).

### E-NSSM (optional, only if installed with `-WithNssm`)

- [ ] The `FlowShiftRuntime` service exists but is set to **manual** start
      (NOT auto). It is explicitly NOT the input-forwarding path.
- [ ] If someone starts the service, the GUI shows the red
      `Session: 0 (Dienst) — Input-Forwarding NICHT möglich!` warning, so a
      session-0 runtime can never masquerade as healthy for forwarding.

## F. Control socket

- [ ] Step 12 reports `control socket reachable`, OR
- [ ] If not reachable, `runtime.err` in the logs explains why (session-0 note).

## G. GUI shortcut

- [ ] Desktop `FlowShift` icon exists.
- [ ] Double-click opens the GUI (no CMD console window stays open; uses pythonw).
- [ ] GUI shows service/runtime status (network / forwarding / capture separated).
- [ ] Start Menu `FlowShift\FlowShift GUI`, `FlowShift Logs`, `Uninstall FlowShift`
      exist and work.

## H. Reboot / logon

- [ ] Reboot the machine and log on.
- [ ] The `FlowShift` scheduled task auto-starts the runtime in the user session.
- [ ] GUI shows status after login: green Session (interactive) + healthy workers.

## I. Uninstall

- [ ] Double-click `uninstall_flowshift.bat` (or Start Menu → Uninstall FlowShift).
- [ ] UAC prompt; accept.
- [ ] Steps 1–6 shown.
- [ ] Scheduled task `FlowShift` is gone (`Get-ScheduledTask` not found).
- [ ] Machine env `FLOWSHIFT_CONFIG` / `FLOWSHIFT_LOG_DIR` cleared.
- [ ] NSSM service `FlowShiftRuntime` gone (if it had been installed).
- [ ] Desktop icon gone.
- [ ] Start Menu `FlowShift` folder gone.
- [ ] `%ProgramFiles%\FlowShift` gone.
- [ ] No FlowShift processes remain on ports 45781 / 45782.
- [ ] Prompt asks whether to delete `%ProgramData%\FlowShift`:
  - [ ] Answering "no" keeps config/logs.
  - [ ] Answering "yes" removes the data folder.

## J. Reinstall

- [ ] After uninstall, run `install_flowshift.bat` again.
- [ ] Install succeeds cleanly (idempotent: old task/service removed first).

## K. Repo hygiene during all of the above

- [ ] The repo working copy never gains `config.json`, `flowshift.log`,
      `flowshift_runtime.out`, `__pycache__`, or `start_flowshift.vbs` as tracked
      files (runtime data + private launchers stay out of the repo).

## L. Packaged update and rollback

- [ ] Install an older test build into `%ProgramFiles%\FlowShift` and keep
      recognizable settings in `%ProgramData%\FlowShift\config.json`.
- [ ] Publish or locally simulate a newer release with exactly
      `FlowShift-Setup.exe`, `update-manifest.json`, and `SHA256SUMS.txt`.
- [ ] Confirm notify and download policies do not start installation.
- [ ] With install policy, keep forwarding or a clipboard transfer active.
      Status must remain `waiting_for_idle` and must not terminate the activity.
- [ ] Once idle, confirm the external runner uses `/FLOWUPDATE`, the setup does
      not prompt, and only the runner starts the new Scheduled Task.
- [ ] Confirm installed `VERSION`, `/api/status`, control socket, WebGUI root,
      overlay entry point, settings, and Scheduled Task are healthy afterward.
- [ ] Force setup failure and health-check failure separately. Both must restore
      the prior program directory, task, version, and user JSON.
- [ ] Confirm `%ProgramData%\FlowShift\updates\last_update_result.json` records
      success or rollback truthfully and is reflected after runtime restart.

---

**Status:** These tests require real Windows machines (ideally one truly fresh
without Python) and were NOT run in the development environment. Payload staging,
Inno compilation, manifest/checksum binding, PowerShell parsing and update-runner
simulations are automated; actual UAC, dependency installation, shortcuts,
Scheduled Task behavior, upgrade and rollback must be verified on a disposable VM.
