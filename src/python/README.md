# FlowShift – Python Prototype

Runs on any Windows PC with Python 3.10+.

This is the **productive** part of FlowShift (input forwarding). Video / monitor
matrix is not implemented here.
The local WebGUI lives in `../webgui/` and is built separately with
`npm ci --include=dev && npm run build`.

## Files

| File | Role |
|---|---|
| `tray.py` | **Productive runtime** + tray icon (`python tray.py --tray`) |
| `gui.py` | Settings GUI (`python gui.py`) |
| `runtime_model.py` | Shared, platform-independent logic (identity, hotkeys, framing, scaling) |
| `service.py` | Legacy compatibility wrapper (imports from `tray.py`) |
| `test_service.py` | Pure-logic unit tests (any OS) |
| `e2e_test.py` | Runtime handshake + input end-to-end test (Windows) |
| `reconnect_stress_test.py` | Reconnect churn + clean-shutdown test (Windows) |

## Configuration (`config.json`)

```json
{
  "device_name": "Mein-PC",
  "device_id": "a1b2c3d4",
  "port": 45781,
  "peers": [
    { "name": "PC2",    "host": "192.168.1.100", "port": 45781, "device_id": "b5c6d7e8" },
    { "name": "Tablet", "host": "192.168.1.101", "port": 45781 }
  ]
}
```
`device_id` is generated automatically and is the stable peer identity. Peers may
be added without a `device_id`; they are then identified by `host:port` until one
is learned.

## Run

```cmd
python gui.py          # settings + start/stop
python tray.py --tray  # productive runtime directly
```

## Hotkeys

| Hotkey | Action |
|---|---|
| `Ctrl + Alt + 1` | forward input to peer 1 |
| `Ctrl + Alt + N` | forward input to peer N |
| `Ctrl + Alt + 0` | return to local input |
| `Ctrl+Alt+Shift+Win+F12` | kill switch (stop + quit) |

Hotkeys bind to a **stable peer identity**, not a list index, so editing the
peer list never makes a hotkey point at the wrong device. See
[../../docs/hotkey-config.md](../../docs/hotkey-config.md).

## How it works

1. Each device runs `tray.py --tray`.
2. Devices discover each other on the LAN (UDP 45781) and connect via TCP 45781.
3. Pressing `Ctrl+Alt+N` forwards keyboard + mouse to peer N; they are injected
   there with `SendInput`. Held keys/buttons are released cleanly on stop.
4. `Ctrl+Alt+0` returns control to the local machine.

Wire protocol: [../../docs/protocol.md](../../docs/protocol.md).

## Tests

```cmd
python -m py_compile tray.py gui.py e2e_test.py service.py test_service.py runtime_model.py reconnect_stress_test.py
python test_service.py
python e2e_test.py
python reconnect_stress_test.py 30
```

## Notes

- Same LAN required (no port forwarding).
- Firewall must allow TCP 45781 and UDP 45781.
- Run as Administrator to inject into elevated windows.
- `service.py` is only a legacy wrapper; use `tray.py`.
