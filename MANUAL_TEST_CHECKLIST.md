# FlowShift Manual Test Checklist

## Runtime
- Start `src/python/tray.py --tray` on both devices.
- Confirm the tray icon appears and the control socket answers `status` on `127.0.0.1:45782`.
- Stop the runtime from the GUI and confirm it exits without killing unrelated processes.
- Start it again and confirm only one runtime instance stays alive.

## Peer Link
- Use `Ping` for a known peer and confirm `display_name`, `device_id`, and RTT are reported.
- Disconnect the peer device and confirm the GUI status falls back to offline.
- Reconnect and confirm the peer returns without manual restart.

## Input Forwarding
- Activate a peer profile and confirm keyboard events are forwarded.
- Move the mouse and confirm absolute injection still tracks the target screen.
- Test mouse buttons and wheel while forwarding is active.
- Return to local input and confirm forwarding stops immediately.

## Logging
- Confirm mouse-move logs do not spam continuously.
- Confirm repeated `status` polls do not flood the log.
- Confirm shutdown is logged once and loops stop cleanly.

## Config
- Edit a peer and confirm `device_id` is preserved.
- Confirm canonical peer matching uses `device_id` first, then endpoint.
- Confirm `config.json` changes are written only when intended.
