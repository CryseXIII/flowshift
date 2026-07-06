# FlowShift Manual Test Checklist

Automated coverage first (run these before any manual pass):

```
python -m py_compile src/python/tray.py src/python/gui.py src/python/e2e_test.py src/python/service.py src/python/test_service.py src/python/runtime_model.py src/python/reconnect_stress_test.py src/python/keymap.py src/python/input_events.py src/python/platform_capabilities.py src/python/input_backends/*.py
python src/python/test_service.py          # pure logic (any OS)
python src/python/e2e_test.py              # runtime handshake + input (Windows; skips cleanly on non-Windows)
python src/python/reconnect_stress_test.py 30   # reconnect churn + clean shutdown (Windows)
```

## Runtime lifecycle
- Start `src/python/tray.py --tray` on both devices.
- Confirm the tray icon appears and the control socket answers `status` on `127.0.0.1:45782`.
- Start the service twice from the GUI: the second start must NOT create a second runtime
  (singleton mutex) and the GUI must show a single "Läuft" state.
- Stop the runtime from the GUI: state goes `stopping` -> `stopped` only after the control
  socket is actually gone (not just because a process handle exists).
- Restart (stop, wait for stopped, start again) and confirm it comes back to `running`.
- Repeat Start/Stop 20x and Connect/Disconnect 20x; confirm no zombie processes remain
  (`Get-NetTCPConnection -LocalPort 45781,45782`).

## Peer link (stable identity)
- Use `Ping` for a known peer and confirm `display_name`, `device_id`, and RTT are reported.
- Disconnect the peer device and confirm the GUI status falls back to offline and the log
  shows a single "peer disconnected" line.
- Reconnect and confirm the peer returns without restarting the runtime.
- Force a duplicate connection (both sides dial each other): confirm the older socket of the
  same direction is closed ("replaced stale ... connection") and input is not injected twice.

## Connector reacts to address changes (same device_id)
- With a peer that has a `device_id`, edit its **host** in the GUI while the runtime runs.
- The log must show `peer device:<id> address changed <old> -> <new>, restarting connector`
  and a fresh `starting connector thread ... -> <new host>`. The old connector must NOT keep
  dialing the old address. Repeat for a **port** change.
  (Automated proxy: `diff_connectors` in `test_service.py`.)

## Hotkeys (no index drift, no invalid registration)
- With peers A and B and default hotkeys, set the forward hotkey for B.
- Delete peer A: the B hotkey must still forward to B; the A hotkey must show "(ungültig)".
- Rename B (keep it the same device): the hotkey must still forward to B and the label updates.
- Insert a new peer C: existing hotkeys must keep pointing at their original peers.
- Change a hotkey in the GUI while the runtime is running: confirm the log shows the OS hotkey
  being re-registered and the NEW combination works immediately (old one no longer triggers).
- An **invalid** hotkey (unresolved forward target, or `key == 0`) must NOT be registered:
  the log shows `skipping invalid hotkey ... reason=...` and the runtime does not crash.

## Profiles by stable identity (duplicate display names)
- Create two peers with the **same display name** but different `device_id`.
- In the Profile tab, activating peer B must activate exactly B (status marker on B), and
  `Ping` on B must ping B — renaming B must not change which peer is activated.
  (Automated proxy: identity/`index_by_identity` tests in `test_service.py`.)

## Input forwarding + cleanup
- Activate a peer profile and confirm keyboard `key`/`key_up` are forwarded and injected.
- Move the mouse and confirm absolute injection tracks the target screen (not stuck at 0/0).
- Test left/right/middle buttons and the wheel while forwarding.
- Hold a key/button down, then press Return-to-local (or disconnect the peer): confirm no key
  or button stays stuck on the target (synthetic key_up/mouseup are sent/injected).
- Kill switch `Ctrl+Alt+Shift+Win+F12` stops forwarding and quits the runtime.

## Logging
- Confirm `src/python/flowshift.log` receives lines (file logging is fixed).
- Confirm mouse-move and `status`-poll logs are rate-limited (no flooding).
- Confirm shutdown is logged once and all loops stop cleanly.

## Config
- Edit a peer and confirm `device_id` is preserved (identity, and thus its hotkey, is stable).
- Confirm canonical peer matching uses `device_id` first, then endpoint.
- Confirm `config.json` changes are written only when intended (device edit, peer edit, hotkey change).
