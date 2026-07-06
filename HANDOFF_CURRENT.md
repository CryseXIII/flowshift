# FlowShift Current State

## Productive path
- `src/python/tray.py --tray` is the productive runtime.
- `src/python/gui.py` is the settings GUI.
- `src/python/runtime_model.py` holds the shared, platform-independent logic.
- `src/python/service.py` is a legacy wrapper only.
- Rust (`src/service`, `src/viewer`) is experimental, excluded from the Cargo
  workspace, and NOT productive. `src/viewer` is a stub; `src/service` does not
  currently compile.

## What changed in the last audit/fix pass
- Fixed dead file logging in `tray.py` (`flowshift.log` is written again).
- Removed hotkey index drift: hotkeys bind to a stable peer identity
  (`device:<id>` / `endpoint:<host>:<port>`), with one-time migration from the
  legacy `forward_<index>` format. Shared logic in `runtime_model.py`.
- OS hotkeys (`RegisterHotKey`) are now cleanly unregistered + re-registered on
  config change, on the window thread (message `WM_RELOAD_HOTKEYS`).
- Peer connections are stored by stable identity, not display name; replacing a
  connection closes the old socket; `request_shutdown` closes all peer sockets;
  peer read loops are timeout-based and shutdown-aware (`FramedReader`).
- Input forwarding tracks pressed keys/buttons and releases them on stop /
  disconnect / shutdown (no stuck keys). Mouse scaling is clamped and normalised.
- GUI service state machine (`stopped/starting/running/stopping/error`) driven by
  the control socket, not by a process handle; start/stop have timeouts.
- Tests are platform-independent: `test_service.py` imports only `runtime_model`.
  Added `reconnect_stress_test.py`.
- Docs corrected to stop promising video/viewer/Tauri features that do not exist.

## Important decisions
- `tray.py --tray` is the only productive runtime entrypoint.
- Canonical peer identity: `device_id` first, then `endpoint:host:port`.
- Hotkeys are identity-based; peer-list edits never silently repoint a hotkey.
- Control socket `127.0.0.1:45782` is the single source of truth for GUI state.

## Verification (this environment)
- `python -m py_compile ...` – OK.
- `python src/python/test_service.py` – OK (53 checks).
- `python src/python/e2e_test.py` – OK.
- `python src/python/reconnect_stress_test.py 30` – OK.
- `cargo check/test` – NOT RUN: cargo/rustc are not installed here. Rust is
  therefore unverified and kept experimental/excluded.

## Next manual checks (Windows, two machines)
- Start/stop/restart 20x; connect/disconnect 20x; no zombie processes.
- Ping both directions; reconnect after disconnect without restart.
- Forwarding: keys, mouse move/buttons/wheel, return-to-local, no stuck keys.
- Hotkey edit while running takes effect immediately.
- See `MANUAL_TEST_CHECKLIST.md` and `FLOWSHIFT_AUDIT_AND_FIX_REPORT.md`.
