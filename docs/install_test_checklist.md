# FlowShift Install / Uninstall Test Checklist

These are **manual** tests. The installer touches machine-wide state (services,
Program Files, Program Data, shortcuts, and possibly a Python install), so it
cannot be exercised in the automated pure-logic test suite. Run these on real
Windows machines.

Every change to installed files, dependencies, folders, autostart, registry,
ports, WebGUI assets or `%ProgramData%\FlowShift` data must update the
installer, updater and uninstaller in the same task (rule in `AGENTS.md`).
Such a task always lists at least section **J2** (install over an older
installation) and section **I** (clean uninstall) as checks.

## Files under test

- `install_flowshift.bat` — double-click launcher (bypasses ExecutionPolicy).
- `install_flowshift.ps1` — the one installer for program **and** WebGUI
  (self-elevates via UAC, asks nothing else unless `-Interactive`).
- `install_webgui.ps1` — internal WebGUI build helper only (no user entry point).
- `FlowShift-Setup.exe` - packaged setup with a curated payload and prebuilt WebGUI.
- `update_flowshift.ps1` - external update, health-check, and rollback runner.
- `uninstall_flowshift.bat` / `uninstall_flowshift.ps1` — remover.
- `FlowShift-<version>-usb.zip` - USB package of an installed version
  (`src/python/usb_package.py`, Settings > Software Update), section N.

## Install targets

| What | Path |
|---|---|
| Program files | `%ProgramFiles%\FlowShift` |
| venv | `%ProgramFiles%\FlowShift\.venv` |
| NSSM | `%ProgramFiles%\FlowShift\tools\nssm\nssm.exe` |
| WebGUI | `%ProgramFiles%\FlowShift\webgui` (`index.html`, `overlay.html`, `VERSION`) |
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
- [ ] `install.log` step `[3/16] Checking Python` reports no usable Python
      (every candidate, if any, is listed as `python candidate ...`).
- [ ] `install.log` step `[4/16] Installing Python` first announces version,
      target folder and expected duration, then downloads the official
      CPython 3.15.1 x64 installer from python.org (`download finished: N MB`),
      verifies its pinned SHA-256, and installs it machine-wide without the
      test suite, logging `Python setup still running (N s)` every 15 s
      (limits: download 300 s, setup 600 s; setup log `python-setup.log`).
      **If this fails** (no internet or hash mismatch), the installer stops with
      a clear message asking for a supported 64-bit CPython 3.11-3.15, plus the
      log path. Install Python manually, then re-run.
- [ ] `install.log` records steps 4-13 and Inno setup completes successfully.
- [ ] WebGUI and `overlay.html` are installed without Node.js/npm on the VM.

## B. Windows WITH Python already present

- [ ] Double-click `install_flowshift.bat`.
- [ ] Step 3 finds a 64-bit CPython 3.11-3.15 (`found Python: ... (via ...)`);
      step 4 logs `nothing is downloaded`.
- [ ] Remaining steps complete.
- [ ] Repeat with `FlowShift-Setup.exe` (always Windows PowerShell 5.1): same
      result; the log line `installer host: PowerShell 5.1...` names the host.

## C. Elevation behaviour

- [ ] Launch `install_flowshift.bat` as a normal (non-admin) user.
- [ ] The PowerShell script self-elevates (single UAC prompt).
- [ ] The elevated window shows all 16 numbered steps, asks no question and
      closes by itself 15 seconds after `INSTALLATION COMPLETE`.

## D. Progress + logging

- [ ] Each step is shown as `[n/16] ...`.
- [ ] On any failure the window stays open for two minutes, shows the reason
      (what is missing) and the log path, and returns a non-zero exit code.
- [ ] With `install_flowshift.bat -Interactive` the installer asks before
      stopping processes and waits for Enter at the end.
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
- [ ] Start Menu `FlowShift\FlowShift GUI`, `FlowShift Web GUI`, `FlowShift Logs`,
      `Uninstall FlowShift` exist and work; `FlowShift Web GUI` opens
      `http://127.0.0.1:5000/`.

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
- [ ] `%ProgramFiles%\FlowShift` gone, including `webgui`.
- [ ] No FlowShift processes remain on ports 45781 / 45782 / 5000.
- [ ] No question is asked; `%ProgramData%\FlowShift` (config.json, clipboard
      history, logs) is kept.
- [ ] The regenerable caches `clipboard\temp` and `clipboard\previews` (file
      previews, `0.7.0-dev.44`) are emptied/removed even when the data is kept.
- [ ] `uninstall_flowshift.bat -PurgeData` removes the data folder;
      `-Interactive` asks before deleting it.

## J. Reinstall

- [ ] After uninstall, run `install_flowshift.bat` again.
- [ ] Install succeeds cleanly (idempotent: old task/service removed first).

## J2. Upgrade over an older installation (no uninstall first)

- [ ] With an older FlowShift version installed and running, and recognizable
      settings plus clipboard history present, run the new
      `install_flowshift.bat` (or `FlowShift-Setup.exe`) without uninstalling.
- [ ] Install succeeds; afterwards the new version runs (tray, WebGUI and
      `overlay.html` report the new `VERSION`).
- [ ] No leftover files of the old version break the new one (Scheduled Task
      points to the new installation, exactly one set of firewall rules and
      shortcuts).

- [ ] Start from an older installation whose `webgui` has no `overlay.html`
      (e.g. the July build) while FlowShift is running; note the content of
      `%ProgramData%\FlowShift\config.json`.
- [ ] Double-click `install_flowshift.bat`. Only the UAC prompt appears; the
      log shows `upgrading in place`, the running FlowShift is stopped and
      started again, and `removed the previous WebGUI (unversioned, N files)`.
- [ ] `%ProgramFiles%\FlowShift\webgui` contains only the new build (no old
      hashed assets) plus `VERSION` equal to the program `VERSION`.
- [ ] The final step reports `serves index.html and overlay.html; WebGUI matches`.
- [ ] `config.json` is byte-for-byte unchanged; clipboard history is still listed.
- [ ] Immediately afterwards WebGUI, command wheel (Ctrl+Right click) and
      clipboard overlay (Ctrl+Win+V) open.
- [ ] Apps & Features shows a single `FlowShift` entry (the old separate
      `FlowShift Web GUI` entry is gone).

## J3. Python search after the WebGUI build (upgrade, Python present)

Case from 19.11.2026: `0.7.0-dev.22` installed, Python 3.13.2 (`C:\Windows\py.exe`)
and 3.15.1 present, checkout without a matching `webgui\dist`, Node.js present.

- [ ] Double-click `install_flowshift.bat`. Order in the log: `[2/16] Checking
      the WebGUI build` (built from source), `[3/16] Checking Python`,
      `[4/16] Installing Python (if missing)`, then `[5/16] Stopping a running
      FlowShift`.
- [ ] Step 3 lists every candidate as `python candidate <path>: exit=0 time=..s
      stdout=3|15|1|64|... stderr=(none) -> usable Python 3.15.1 64-bit` and
      ends with `found Python`; step 4 says `nothing is downloaded`.
- [ ] `installer host: PowerShell ...` is logged; repeat once from an already
      elevated `powershell.exe` window (no self-elevation, Windows PowerShell 5.1).
- [ ] Failure path: run the installer from a copy of the checkout without
      `webgui\dist` and with Node.js hidden from PATH. It stops in step 2, the
      running FlowShift is never stopped, and the red box ends with
      `Status : Nothing was changed: FlowShift <old version> is still installed
      as before.` If Python checks fail while `Program Files\Python315\python.exe`
      exists, the installer stops with `No second copy is downloaded`.
- [ ] Automated on Windows: `powershell -File .\test_install_python_probe.ps1`
      and `pwsh -File .\test_install_python_probe.ps1` (add `-RealWebGuiBuild`
      when Node.js 24+ is installed) report `0 failed`.

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
      overlay entry point, settings, and Scheduled Task are healthy afterward;
      `/api/status` reports `webgui.ok = true` with `webgui_version` equal to the
      new `VERSION` (the runner treats anything else as a failed update).
- [ ] Force setup failure and health-check failure separately. Both must restore
      the prior program directory, task, version, and user JSON.
- [ ] Confirm `%ProgramData%\FlowShift\updates\last_update_result.json` records
      success or rollback truthfully and is reflected after runtime restart.
- [ ] Stable channel without Git, Node.js and Forgejo (`0.7.0-dev.52`, Viktor
      24.11.): on a PC without `git`/`node`/`npm` and without VPN, install the
      GitHub `FlowShift-Setup.exe`, then update to the next GitHub release with
      each policy (notify: Check/Download/Install by hand; download; install)
      and once from an installed `0.7.0-dev.N` switched to Stable. Every run
      ends with `update.log` `result: success`; the Software Update page shows
      no Forgejo text; a running FlowShift finds a new release within 6 hours
      without a restart (`MANUAL_TEST_CHECKLIST.md` "Stable update channel on
      a PC without Git, Node.js and Forgejo").
- [ ] Development channel, one-time switch: on a PC with FlowShift older than
      `0.7.0-dev.29`, unpack the Forgejo ZIP of `main` into a folder with a
      space in its name, double-click `install_flowshift.bat` (Windows
      PowerShell 5.1 and, if installed, PowerShell 7): one UAC prompt, running
      FlowShift stopped and started again, `config.json` unchanged. In the
      WebGUI choose channel Development and enter the access: Channel
      "Development", Source "Forgejo private-platform/flowshift (main)" and a
      fresh "Last check" are shown; `config.json` contains no token.
- [ ] Development channel update `0.7.0-dev.N` -> next merged `main` through
      the external runner with policy install: `update.log` shows unpack to
      `ProgramData\FlowShift\updates\source\<version>`, rollback copy, the
      archive's `install_flowshift.ps1`, start and result `success`; WebGUI
      shows the new version; `updates\source` is removed afterwards.
- [ ] Program folder in use (dev.31; dev.29 -> dev.30 failed with "access
      denied" on `C:\Program Files\FlowShift`): first install dev.31 by hand
      (older runners still have the bug). With the legacy settings window
      (`FlowShift GUI`) and an Explorer window on `C:\Program Files\FlowShift`
      open, the next merge installs by itself: the first `update.log` line
      shows `administrator True` and a working directory under
      `ProgramData\FlowShift\updates`, leftover processes are listed as
      `stopped processes still running from the program folder`, result
      `success`; no `updates\update_plan.json` remains. If the folder stays
      busy, the result is `failed` with `program folder in use`, the previous
      version runs again and Software Update explains it in plain words.
- [ ] Desktop shortcut `FlowShift` (after quitting FlowShift from the tray):
      starts FlowShift through the `FlowShift` task without a UAC prompt; the
      tray runs elevated (Task Manager, column "Elevated" = Yes).
- [ ] Force a failure of the archive's install script (for example rename
      `node.exe` after the check): result `rollback_success`, the previous
      version runs again, the WebGUI shows that the failed version is not
      retried automatically.
- [ ] Without Node.js 24+: the WebGUI shows the Node.js hint and FlowShift is
      not stopped.
- [ ] VPN off: the check reports "Forgejo is not reachable" without a popup;
      "Last successful check" keeps its time.
- [ ] Never two installations at once (`0.7.0-dev.35`; 21.11. two update runs
      and a manual install ran together and left no program folder): install
      dev.35 once by hand. Later, with a newer FlowShift running, start
      `install_flowshift.bat` from the dev.35 folder (an older checkout).
      `install.log` shows `this folder contains FlowShift 0.7.0-dev.35, but
      FlowShift <newer> is already installed or available`, `install lock
      taken`, `new program files ... staged and checked`, `previous
      installation ... kept in ...FlowShift.previous`, `installation complete`
      and `removed the previous installation`; FlowShift dev.35 runs,
      `C:\Program Files\FlowShift` is complete, no `FlowShift.staging` or
      `FlowShift.previous` is left. `flowshift.log` shows `installed by hand;
      automatic updates wait for the next regular check`; about 15 minutes
      later the next check installs the newer version, `update.log` ends with
      `result: success`.
- [ ] Update running, manual install started meanwhile: the window shows
      `another FlowShift installation or update is running ...; waiting` and
      continues only after `update.log` shows its `result:` line.
- [ ] Manual install running, update due meanwhile: Software Update shows
      "Another FlowShift installation or update was running, so nothing was
      changed"; `update.log` shows `not started: another FlowShift
      installation is running`.
- [ ] Manual install fails after the swap (for example pull the network
      cable before `Installing Python dependencies`): `install.log` shows
      `previous installation ... moved back` and the previous version runs.
- [ ] Update rollback fails (for example lock `FlowShift.rollback-<version>`
      open in a command prompt during the update and force a failure): a
      Windows message names the rollback copy and `install_flowshift.bat`; the
      rollback copy still exists.
- [ ] Uninstall afterwards: `Program Files\FlowShift.rollback-*`,
      `FlowShift.staging`, `FlowShift.previous`, `FlowShift.failed-*`,
      `ProgramData\FlowShift\updates\source`, `updates\downloads` and
      `updates\manual_install.json` are gone. Uninstalling during an update
      waits for the update to finish first.
- [ ] Upgrade `v0.5.4` -> `v0.6.3` on a machine with existing clipboard history:
      after the update `%ProgramData%\FlowShift\config.json`, the per-profile
      history (`clipboard\profiles\<id>\index.json` + objects) and, if present,
      `clipboard\objects` and `clipboard\journals` are unchanged and the
      history is still listed in the WebGUI.

## M. Clipboard stream_v2 between two 0.6.3 peers

- [ ] With both machines on `v0.6.3` and connected, `GET /api/status` on either
      side lists the peer with `clipboard_transfer_strategy: "stream_v2"` (the
      runtime log shows `capability negotiation ... clipboard_strategy=stream_v2`);
      copy a folder with files and confirm `GET /api/clipboard/status` shows the
      running transfer under `stream_v2` with `strategy: "stream_v2"` and the
      receiver pastes the exact tree.
- [ ] Set `clipboard_transfer_v2_force_legacy: true` on one side, reconnect:
      the strategy falls back to `legacy_zip_v1` and file copy still works.

## N. USB stick package (`0.7.0-dev.53`)

Automated (Linux, `src/python/test_usb_package.py`): the package is built from
an installed-layout copy, unpacked into an empty folder, checked with
`usb_package.py --check-package` and installed and started with a PATH that
holds only Python (no git, node, npm); `/api/status` reports the package
VERSION, `index.html`/`overlay.html` come from the unpacked `webgui/dist`, the
channel becomes stable. The real downloads and the PowerShell run need Windows:

- [ ] Development PC (channel Development, installed `0.7.0-dev.53`, installed
      once with `install_flowshift.bat` or the update so the program folder has
      `install_flowshift.ps1`/`.bat`): Settings > Software Update shows
      "USB package for other PCs"; "Create USB package" shows the steps and
      ends with `Saved: ...\Downloads\FlowShift-0.7.0-dev.53-usb.zip (.. MB)`.
      `flowshift.log` has `usb package ready file=... python=3.1x,...`;
      `%ProgramData%\FlowShift\updates\usb-build` is gone afterwards.
- [ ] The ZIP has one top folder `FlowShift-0.7.0-dev.53-usb` with
      `install_flowshift.bat`, `webgui\dist\VERSION` = `0.7.0-dev.53`,
      `offline\python-3.15.1-amd64.exe`, `offline\wheels\common\pip-*.whl`,
      `offline\wheels\cp315\...` and `offline\usb_package.json`; no
      `webgui\dist\config.json`, no `src\python\config.json`.
- [ ] Clean Windows VM without Python, Git and Node.js, network cable
      unplugged (WebView2 present, as on Windows 11): unpack, double-click
      `install_flowshift.bat`. `install.log` shows `USB package ...: offline
      Python ... checked`, `using python-3.15.1-amd64.exe from the USB package
      (no download)`, `installing pip and the dependencies from the USB package`,
      `update channel: stable (GitHub) ...`; no download line. FlowShift runs,
      the WebGUI shows `0.7.0-dev.53`, channel Stable, Source GitHub, no Forgejo
      message, the sentence "The installed version can be newer than the latest
      GitHub version ... never installs an older version".
- [ ] Same VM with Python 3.13 installed: the packages for 3.13 are used (no
      second Python). With a Python the stick has no packages for: the bundled
      3.15.1 is installed and used, `install.log` says so.
- [ ] PC with an older FlowShift (channel Development in its config.json):
      double-click `install_flowshift.bat` of the package, no uninstall first:
      upgrade in place, settings kept, channel now Stable. During a running
      update the double-click waits for the lock; a failure puts the previous
      version back (`FlowShift.previous`).
- [ ] Change one byte of a file under `offline\wheels`: the installer stops
      before changing anything with "the USB package is damaged ...".
- [ ] `uninstall_flowshift.bat` removes program and WebGUI; the ZIP in
      Downloads stays.

### N2. Failed downloads while creating the package (`0.7.0-dev.55`)

Automated (Linux): `default_fetch_python` against a local server (whole file
through a redirect, connection cut before `Content-Length`, HTTP 404, HTML
filter page; deep work path), second try, virus-scanner case, browser-saved
file (also `(1)`, wrong file refused), pip and wheel retries, proxy page as
wheel, job message with log path, API body (`test_usb_package.py`,
`test_web_api_updates.UsbPackageApiTests`). On Windows:

- [ ] Development PC, normal network: "Create USB package" works as in N;
      `flowshift.log` has no `python download try` line.
- [ ] Same PC with the network cable pulled after "Downloading Python
      3.15.1 from python.org" appears: the page shows "Downloading Python
      3.15.1 again (try 2 of 2)" and then a message with "Cause: ...",
      "Expected 45.555.248 bytes, SHA-256 8b23839c...8ea5db", what arrived,
      HTTP status/type and "Details: C:\ProgramData\FlowShift\flowshift.log";
      the log has two `usb package python download try` lines.
- [ ] Under the message: the link `python-3.15.1-amd64.exe` opens/saves the
      python.org file in Downloads; "Use downloaded Python installer" builds the
      ZIP without downloading Python. A renamed other `.exe` saved as
      `python-3.15.1-amd64.exe` is refused ("is not the official file") and
      nothing is packed.
- Viktor's PC (where dev.53 said "wrong SHA-256" at 22:29): the second try at
  22:38 on the same PC got the Python installer through, so the first error
  was an incomplete or disturbed download (cause `incomplete`), not a wrong pin
  or a virus scanner. The build then stopped at proxy-tools, see N3.
- [ ] WebGUI: `npm test` (`SoftwareUpdateSection.test.jsx`, fallback case)
      and `npm run build`.

### N3. Packages that PyPI has only as source (`0.7.0-dev.56`)

Automated against the real PyPI (Linux, `src/python/test_usb_package_pypi.py`,
needs internet; without any network path to PyPI, as in the sandbox of the
automatic test run, it reports itself skipped with the reason, and once PyPI
answers every error fails; `FLOWSHIFT_SKIP_PYPI_TESTS=1` skips it on purpose;
a Python without its own pip runs the pinned pip wheel from PyPI via
`PYTHONPATH=<wheel> python -m pip`): every
line of `requirements.txt` has a Windows amd64 wheel or a locked sdist for the
bundled Python; `build_package` with the real pip builds `proxy-tools` into a
`py3-none-any` wheel and downloads the wheel sets 3.11-3.15; the unpacked
package passes `--check-package` and installs with `pip install --no-index
--find-links offline/wheels/cp3XY --require-hashes -r
offline/requirements-offline.txt --platform win_amd64 --python-version 3.XY
--target <empty>` for every set while every network path points nowhere; the
same with the plain `requirements.txt` is refused (only the sdist hash of
proxy-tools). Stand-in tests (`test_usb_package.py`
`SourceOnlyPackageTests`, `WheelTagTests`): failed build, source that needs a
compiler, PyPI not reachable, changed `requirements-offline.txt`. On Windows:

- [ ] Viktor's PC, normal network: "Create USB package" shows "Checking on
      PyPI which Windows packages exist ..." and "Building the Python package
      proxy-tools 0.1.0 from its source ..." and saves
      `Downloads\FlowShift-0.7.0-dev.56-usb.zip` (about 90 MB);
      `offline\requirements-offline.txt` and
      `offline\wheels\cp315\proxy_tools-0.1.0-py3-none-any.whl` are inside.
- [ ] Clean Windows VM without Python, network cable unplugged: install from
      the unpacked ZIP as in N; `install.log` shows `dependencies installed
      from requirements.txt` after `installing pip and the dependencies from
      the USB package`, FlowShift and the overlay start (pywebview imports
      proxy-tools).

## O. Update from a connected device (`0.7.0-dev.57`)

Automated (Linux, `src/python/test_peer_update.py`): two instances with
different versions over real TCP sockets (the sender is an installed-layout
copy of this repository with a higher VERSION and serves its package the way
the FlowShift port does): `install` fetches, checks, waits until idle and
hands `FlowShift-Peer-<version>.zip` to the updater (plan `package_kind =
peer`); `notify` transfers nothing before the click; `download` stops before
the install; unpaired devices, older or equal versions and development
versions on Stable (without the tick) are refused; the sender refuses devices
that are not in its list; an address that answers with another device ID is
refused; cancel during the transfer keeps nothing, cancel during the
announcement keeps the package; a broken transfer resumes at the saved byte;
changed, extra, missing and unsafe files, a wrong VERSION, a downgrade and
bytes that differ from the offer are refused and deleted. On Windows (both
PCs on `0.7.0-dev.57` or newer, each in the other's device list):

- [ ] PC A newer than PC B, B on "Notify only": B's Settings > Software Update
      > "Updates from connected devices" shows "A offers FlowShift <version>"
      and a tray message; nothing appears in
      `%ProgramData%\FlowShift\updates\downloads` before the click.
- [ ] B: "Install and restart": progress bar, then FlowShift closes and is back
      after about a minute with the new version; `update.log` shows
      `(peer package ...)` and `result: success`; the panel shows "Last update
      from a connected device: ... installed and running". On A,
      `flowshift.log` shows `peer update package request from device ...: sent`.
- [ ] B on "Download, install and restart automatically": a tray message a
      minute before; with a clipboard file transfer running at that moment,
      the restart waits ("Waiting because: ...") until it is done; "Cancel"
      during the minute keeps the old version running.
- [ ] Unplug the network during the transfer, plug it in again: the next
      connection continues the transfer (A's log: `resumed`).
- [ ] B on Stable without the tick, A on a `-dev` version: B shows nothing
      to install (log: `development_version_not_allowed`); with the tick the
      offer appears.
- [ ] Uninstall on A removes `%ProgramData%\FlowShift\updates\peer_outbox`
      and `updates\peer_update.json`; no new firewall rule or port appears
      (`netstat -ano` shows only 45781/45782/5000 as before).

---

**Status:** These tests require real Windows machines (ideally one truly fresh
without Python) and were NOT run in the development environment. Payload staging,
Inno compilation, manifest/checksum binding, PowerShell parsing and update-runner
simulations are automated; actual UAC, dependency installation, shortcuts,
Scheduled Task behavior, upgrade and rollback must be verified on a disposable VM.
