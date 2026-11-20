# Setup Guide (current Python prototype)

The productive FlowShift today is a **Python prototype for Windows** distributed
through a packaged Windows setup. There is no video viewer or Tauri GUI yet.

## Requirements

- Windows 10/11
- 64-bit CPython 3.11-3.15 (the installer can securely install Python 3.15.1
  from python.org when missing)
- All devices on the same LAN (Ethernet recommended)
- Firewall must allow **TCP 45781** (peer link) and **UDP 45781** (discovery)

## Install

### Option 1 - packaged setup (recommended, Windows x64)

1. Download the latest stable
   [FlowShift-Setup.exe](https://github.com/CryseXIII/flowshift/releases/latest/download/FlowShift-Setup.exe).
2. Run it and accept the UAC prompt.
3. It installs to `%ProgramFiles%\FlowShift`, keeps config/logs in
   `%ProgramData%\FlowShift`, creates Desktop + Start Menu shortcuts, and
   registers a **user-session Scheduled Task** (`FlowShift`, AtLogOn) that runs
   the runtime in your interactive session (where input hooks + `SendInput`
   work). It starts the runtime immediately and verifies the control socket.

The initial release is not code-signed and may trigger Windows SmartScreen.
Verify its SHA-256 against `SHA256SUMS.txt` from the same GitHub Release.

> **Important — Session 0 / services:** input forwarding must run in the
> interactive user session. A Windows *service* (session 0) CANNOT capture or
> inject interactive input. The installer therefore does NOT install a service by
> default; the primary path is the scheduled task above. `-WithNssm` adds an
> OPTIONAL helper service (manual start) that is explicitly NOT the input path.
> The GUI shows a red `Session: 0 (Dienst)` warning if the runtime ever runs in
> session 0, and refuses to run the live test there.

The packaged setup contains prebuilt WebGUI assets. Node.js, npm, and Vite are
build-time dependencies only and are not required on the target machine.

### Option 2 - source installer

1. Clone or copy the repository.
2. Double-click `install_flowshift.bat`. It installs or upgrades the program
   and the WebGUI together (the WebGUI is mandatory). Run it over an older
   installation without uninstalling first: it stops a running FlowShift,
   replaces program and WebGUI files (no old WebGUI assets remain), keeps
   `%ProgramData%\FlowShift` (config.json, clipboard history, logs), starts
   FlowShift again and verifies that the running runtime reports the same
   `VERSION` as the WebGUI and serves `index.html` and `overlay.html`.
   It asks nothing except the Windows UAC prompt; `-Interactive` enables
   confirmation questions.

   The WebGUI is taken from the prebuilt, version-stamped `webgui/dist` (no
   Node.js needed). Only if that build is missing or belongs to another
   version and Node.js 24+ is already installed, the internal helper
   `install_webgui.ps1` rebuilds it. There is no separate WebGUI installer or
   uninstaller anymore.

### Option 3 - manual / dev

1. Copy the `src/python/` folder to **every** device.
2. Optionally edit `config.json` (or `config.example.json` as a template), or
   configure everything from the GUI.
3. Set `FLOWSHIFT_CONFIG` / `FLOWSHIFT_LOG_DIR` to keep runtime data out of the
   repo (the installer does this automatically).

## Run

Settings: use the **WebGUI** (tray menu → Web GUI, served locally on
`127.0.0.1:5000`). It is the settings interface of FlowShift; new settings are
added only there.

Legacy Tkinter GUI (kept until the WebGUI covers the remaining tabs, see the
staged plan in `TODO_CURRENT.md`; no new settings are added here):
```cmd
python gui.py
```
- Tab **Geräte**: this device (name/port) and peers (add/edit/remove/scan)
- Tab **Profile**: activate/deactivate forwarding, ping, live status
- Tab **Hotkeys**: configure the key combinations
- Tab **Steuerung**: start/stop the runtime + log + capture region

Productive runtime directly (this is what actually forwards input):
```cmd
python tray.py --tray
```

> Input hooks and `SendInput` injection generally require the runtime to run
> with the **same privilege level** as the target apps. Run as Administrator if
> forwarding into elevated windows.

## First configuration

1. Start FlowShift on **all** devices (`tray.py --tray`, or via the GUI).
2. Open the WebGUI → devices are discovered on the LAN (or add a peer by IP).
3. Assign hotkeys (WebGUI Settings; the legacy Tkinter tab "Hotkeys" still
   works). Each peer gets a `Ctrl+Alt+<n>` forward hotkey;
   `Ctrl+Alt+0` returns input to the local machine.
4. Press a peer's hotkey to forward keyboard/mouse to it; press it again or
   `Ctrl+Alt+0` to return.

## Emergency stop

Kill switch: **Ctrl+Alt+Shift+Win+F12** stops forwarding and quits the runtime.

## Updates and rollback

The WebGUI Software Update section can notify, download automatically, or
download and install automatically. It shows the installed version, the
channel, the source, the last check and the last successful check.

- Channel **Stable** accepts only stable releases from `CryseXIII/flowshift`
  (GitHub `releases/latest`).
- Channel **Development** follows branch `main` of `private-platform/flowshift`
  in the own Forgejo (`http://forgejo.thegoodhunter.vps`, own network or VPN
  only). There is no package build: FlowShift reads the newest `main` commit
  and its `VERSION` every 15 minutes and right after switching. For a newer
  version it downloads the source archive of exactly that commit, checks it
  (commit ID in the ZIP comment, no paths outside the archive, matching
  `VERSION`, size limits), and installs it with the `install_flowshift.ps1`
  it contains (`-FlowUpdate -NonInteractive`). No Git and no manual download
  or unpacking on the PC.
- Because `webgui\dist` is not in the repository, a development update builds
  the WebGUI itself: Node.js 24 or newer (LTS) must be installed. Without it
  the page says so before FlowShift is stopped.
- The channel needs a Forgejo access token with read access to the repository,
  entered once in the WebGUI and kept in the Windows Credential Manager
  (`FlowShift:forgejo-update`), never in `config.json`, the repository or a
  script. It is only sent to the Forgejo address. Without network or VPN the
  check is skipped quietly and shown as "Forgejo is not reachable" with the
  last successful check.
- A development state whose installation failed and was rolled back is not
  retried automatically; Install retries it, the next merged state is
  installed normally.
- One-time switch from FlowShift older than `0.7.0-dev.29`: download the
  current `main` from Forgejo as ZIP once, unpack, double-click
  `install_flowshift.bat`, then choose channel Development and enter the
  access in the WebGUI.

Switching from a development build back to Stable installs nothing until a
stable release newer than the installed version exists. FlowShift verifies release metadata,
installer size, and SHA-256 before installation. Automatic installation waits
for forwarding, input, overlay, and clipboard activity to become idle.

Installation runs in a separate elevated process. It backs up user JSON and the
Scheduled Task, keeps the previous `%ProgramFiles%\FlowShift` directory as a
temporary rollback, starts the new runtime, and checks its control socket, API,
version, and WebGUI. A setup or health failure restores the prior installation.
`%ProgramData%\FlowShift` remains the persistent user-data location. Every
update run is logged to `%ProgramData%\FlowShift\logs\update.log`; the steps of
a development (source) installation are in `install.log`.

The update runs from `%ProgramData%\FlowShift\updates`, never from inside the
program folder, and stops processes still running from
`%ProgramFiles%\FlowShift` (overlay host, legacy settings window) before it
moves the folder aside; a folder that stays busy is retried a few times. If it
still fails, the previous version keeps running and Software Update says in
plain words what happened and what to do. Updates need the administrator
rights of the `FlowShift` autostart task; the desktop shortcut starts FlowShift
through that task, so no UAC prompt appears. FlowShift `0.7.0-dev.29` and
`0.7.0-dev.30` cannot update themselves past this point ("access denied"):
install `0.7.0-dev.31` or newer once with `install_flowshift.bat`.

Development checkouts may check and download releases, but cannot install them.

## Troubleshooting

| Problem | Fix |
|---|---|
| Device not found | Check firewall (UDP/TCP 45781), same subnet |
| Cannot connect | Confirm the peer runs `tray.py --tray`, correct IP/port |
| Hotkey shows "(ungültig)" | Its target peer was removed; reassign or reset hotkeys |
| Input not injected into elevated app | Run the runtime as Administrator |

## Not available yet

- `flowshift-viewer.exe` (no video receiver; `src/viewer` is a stub)
- `flowshift-service.exe` (Rust service is experimental and does not build)
- video/monitor matrix

## Linux (planned, not working yet)

Cross-platform support (Linux↔Linux, Windows↔Linux) is **prepared but not
functional**. The protocol, capability handshake, backend abstraction and key
mapping are in place, but the Linux input backend is still a stub
(`src/python/input_backends/linux_stub.py`).

When implemented, the Linux backend will:
- capture via **evdev** (`/dev/input/event*`) and inject via **uinput**
  (`/dev/uinput`) — desktop-agnostic, working under both X11 and Wayland;
- require **device permissions**: read access to `/dev/input/event*`, read/write
  to `/dev/uinput` (via a udev rule + group, or a small privileged helper /
  systemd service).

Do not expect Linux forwarding to work until this is actually implemented and
tested. Full plan: [linux_backend_plan.md](linux_backend_plan.md).
