# FlowShift Current State

## Productive path
- `src/python/tray.py --tray` is the productive runtime (Windows).
- `src/python/gui.py` is the settings GUI.
- `src/python/runtime_model.py` holds the shared, platform-independent logic.
- `src/python/service.py` is a legacy wrapper only.
- Rust (`src/service`, `src/viewer`) is experimental, excluded from the Cargo
  workspace, and NOT productive. `src/viewer` is a stub; `src/service` does not
  currently compile. **Not being worked on** (Windows-Python is the focus).

## Cross-platform scaffolding (prepared, NOT productive)
- `platform_capabilities.py` – OS/desktop/backend detection + protocol-v1 `hello`
  with a `capabilities` block; `parse_hello` tolerates old peers.
- `input_backends/` – `InputBackend` abstraction: `WindowsWin32Backend` (real
  caps; capture/inject still native in tray.py), `LinuxStubBackend` (honest
  "not implemented", never crashes), `UnsupportedBackend`.
- `keymap.py` + `input_events.py` – Windows-VK ↔ canonical ↔ Linux-evdev mapping
  and platform-neutral event conversion. Unit-tested, wire still uses Win VK.
- Linux target: evdev capture + uinput injection, needs device permissions.
  Plan in `docs/linux_backend_plan.md`. **Not functional yet.**

## What changed in the latest pass (remaining fixes + cross-platform prep)
- **Connector detects host/port changes**: connectors are tracked with a
  `(token, host, port)` signature; on an address change (same `device_id`) the
  old connector is stopped (`stop_event`, drops its socket) and a fresh one dials
  the new address. Reconciliation via `runtime_model.diff_connectors`. Verified
  live in the log (`address changed ... restarting connector`).
- **Invalid hotkeys are not registered**: `register_runtime_hotkeys` skips any
  hotkey with `key == 0` or an unresolved `forward_peer` target and logs a
  warning (`hotkey_registration_error`).
- **GUI profiles use stable identity**: profile rows map runtime status by
  `identity` (not display name), select via `active_peer_identity`, and pass the
  identity to activate/ping. Two same-named peers are told apart.
  `find_config_peer` now also matches by identity so ping-by-identity works.
- **hello enriched** with `protocol_version`, `os`, `desktop`, `input_backend`,
  `capabilities` (backward compatible).
- **e2e_test.py**: skips cleanly on non-Windows (`is_supported()`), waits on the
  control socket, prints runtime output on failure, shuts down via control socket.

## Earlier pass (still in effect)
- Fixed dead file logging in `tray.py` (`flowshift.log` is written again).
- Removed hotkey index drift: hotkeys bind to a stable peer identity
  (`device:<id>` / `endpoint:<host>:<port>`), with one-time migration from the
  legacy `forward_<index>` format. Shared logic in `runtime_model.py`.
- OS hotkeys (`RegisterHotKey`) are cleanly unregistered + re-registered on
  config change, on the window thread (message `WM_RELOAD_HOTKEYS`).
- Peer connections are stored by stable identity, not display name; replacing a
  connection closes the old socket; `request_shutdown` closes all peer sockets;
  peer read loops are timeout-based and shutdown-aware (`FramedReader`).
- Input forwarding tracks pressed keys/buttons and releases them on stop /
  disconnect / shutdown (no stuck keys). Mouse scaling is clamped and normalised.
- GUI service state machine (`stopped/starting/running/stopping/error`) driven by
  the control socket, not by a process handle; start/stop have timeouts.

## Important decisions
- `tray.py --tray` is the only productive runtime entrypoint (Windows).
- Canonical peer identity: `device_id` first, then `endpoint:host:port`.
- Hotkeys are identity-based; peer-list edits never silently repoint a hotkey.
- Control socket `127.0.0.1:45782` is the single source of truth for GUI state.
- Protocol is being made platform-neutral (canonical keys + native_code); Linux
  input is evdev/uinput, not X11/Wayland as fundament.

## Verification (this environment, Windows)
- `python -m py_compile ...` (incl. new modules) – OK.
- `python src/python/test_service.py` – OK (106 checks).
- `python src/python/e2e_test.py` – OK.
- `python src/python/reconnect_stress_test.py 30` – OK.
- Connector address-change restart – verified live via the log.
- `cargo check/test` – NOT RUN: cargo/rustc are not installed here. Rust is
  therefore unverified and kept experimental/excluded.

## Next manual checks (Windows, two machines)
- Start/stop/restart 20x; connect/disconnect 20x; no zombie processes.
- Ping both directions; reconnect after disconnect without restart.
- Forwarding: keys, mouse move/buttons/wheel, return-to-local, no stuck keys.
- Hotkey edit while running takes effect immediately; invalid hotkeys skipped.
- Two same-named peers: activation/ping hit the right one.
- See `MANUAL_TEST_CHECKLIST.md` and `FLOWSHIFT_AUDIT_AND_FIX_REPORT.md`.
