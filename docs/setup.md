# Setup Guide (current Python prototype)

The productive FlowShift today is a **Python prototype for Windows**. There is
no packaged release, no auto-started video viewer, and no Tauri GUI yet.

## Requirements

- Windows 10/11
- Python 3.10+ (from [python.org](https://python.org))
- All devices on the same LAN (Ethernet recommended)
- Firewall must allow **TCP 45781** (peer link) and **UDP 45781** (discovery)

## Install

1. Copy the `src/python/` folder to **every** device.
2. Optionally edit `config.json`, or configure everything from the GUI.

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
- Linux/Android clients, video/monitor matrix, packaged installers
