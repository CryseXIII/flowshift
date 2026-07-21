# Setup Guide (current Python prototype)

The productive FlowShift today is a **Python prototype for Windows** distributed
through a packaged Windows setup. There is no video viewer or Tauri GUI yet.

## Requirements

- Windows 10/11
- Python 3.12 (the installer can install it when missing)
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
2. Double-click `install_flowshift.bat`, then `install_webgui.bat`.

### Option 3 - manual / dev

1. Copy the `src/python/` folder to **every** device.
2. Optionally edit `config.json` (or `config.example.json` as a template), or
   configure everything from the GUI.
3. Set `FLOWSHIFT_CONFIG` / `FLOWSHIFT_LOG_DIR` to keep runtime data out of the
   repo (the installer does this automatically).

## Run

Settings GUI:
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
2. Open the GUI → devices are discovered on the LAN (or add a peer by IP).
3. Assign hotkeys (Tab "Hotkeys"). Each peer gets a `Ctrl+Alt+<n>` forward hotkey;
   `Ctrl+Alt+0` returns input to the local machine.
4. Press a peer's hotkey to forward keyboard/mouse to it; press it again or
   `Ctrl+Alt+0` to return.

## Emergency stop

Kill switch: **Ctrl+Alt+Shift+Win+F12** stops forwarding and quits the runtime.

## Updates and rollback

The WebGUI Software Update section can notify, download automatically, or
download and install automatically. Only stable releases from
`CryseXIII/flowshift` are accepted. FlowShift verifies release metadata,
installer size, and SHA-256 before installation. Automatic installation waits
for forwarding, input, overlay, and clipboard activity to become idle.

Installation runs in a separate elevated process. It backs up user JSON and the
Scheduled Task, keeps the previous `%ProgramFiles%\FlowShift` directory as a
temporary rollback, starts the new runtime, and checks its control socket, API,
version, and WebGUI. A setup or health failure restores the prior installation.
`%ProgramData%\FlowShift` remains the persistent user-data location.

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
