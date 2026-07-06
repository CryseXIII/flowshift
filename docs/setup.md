# Setup Guide (current Python prototype)

The productive FlowShift today is a **Python prototype for Windows**. There is
no packaged release, no auto-started video viewer, and no Tauri GUI yet.

## Requirements

- Windows 10/11
- Python 3.10+ (from [python.org](https://python.org))
- All devices on the same LAN (Ethernet recommended)
- Firewall must allow **TCP 45781** (peer link) and **UDP 45781** (discovery)

## Install

### Option 1 — one-click installer (recommended, Windows)

1. Copy the repo to the device.
2. Double-click `install_flowshift.bat` (self-elevates via UAC).
3. It installs to `%ProgramFiles%\FlowShift`, keeps config/logs in
   `%ProgramData%\FlowShift`, creates Desktop + Start Menu shortcuts, and
   registers a **user-session Scheduled Task** (`FlowShift`, AtLogOn) that runs
   the runtime in your interactive session (where input hooks + `SendInput`
   work). It starts the runtime immediately and verifies the control socket.

> **Important — Session 0 / services:** input forwarding must run in the
> interactive user session. A Windows *service* (session 0) CANNOT capture or
> inject interactive input. The installer therefore does NOT install a service by
> default; the primary path is the scheduled task above. `-WithNssm` adds an
> OPTIONAL helper service (manual start) that is explicitly NOT the input path.
> The GUI shows a red `Session: 0 (Dienst)` warning if the runtime ever runs in
> session 0, and refuses to run the live test there.

### Option 2 — manual / dev

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
- video/monitor matrix, packaged installers

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
