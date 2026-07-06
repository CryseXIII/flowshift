# FlowShift Audit and Fix Report

Date: 2026-07-06
Scope: make the productive Python path (`src/python/tray.py --tray` + `gui.py`)
stable for connect / stop / reconnect, correct input forwarding + injection,
consistent GUI/tray state, honest docs, platform-independent tests. Rust handled
honestly (experimental).

## Summary

The productive runtime and GUI were refactored around a new shared,
platform-independent module `src/python/runtime_model.py`. The main architectural
defects (dead file logging, hotkey list-index drift, name-keyed peer registry,
stale OS hotkeys, stuck keys, process-handle-based GUI state) were fixed and are
covered by tests. Documentation no longer promises unimplemented video/viewer/
Tauri features. The Rust crates that do not compile are excluded from the
workspace and marked experimental; `cargo` is not installed in this environment,
so Rust could not be built or tested and is left unverified.

## Files Reviewed

Python:
- `src/python/tray.py` (productive runtime)
- `src/python/gui.py` (GUI)
- `src/python/service.py` (legacy wrapper)
- `src/python/test_service.py`, `src/python/e2e_test.py`

Rust:
- `src/shared/src/{lib,protocol,routing}.rs`
- `src/service/src/{main,config,network,hotkey,hooks,inject}.rs`
- `src/viewer/src/main.rs`
- `Cargo.toml`, `src/*/Cargo.toml`

Docs:
- `README.md`, `HANDOFF_CURRENT.md`, `MANUAL_TEST_CHECKLIST.md`
- `docs/{architecture,setup,protocol,hotkey-config}.md`
- `src/python/README.md`

## Verified Problems (confirmed real)

1. **Dead file logging** – `tray.py:log()` had a `return` before the file-write
   block, so `flowshift.log` was never written (only stdout).
2. **Hotkey index drift** – actions were `forward_<index>` in both `tray.py` and
   `gui.py`; deleting/inserting/renaming peers repointed hotkeys at the wrong
   peer. `keyboard_proc`/`wnd_proc`/`_handle_menu`/tray-click used
   `hk.action.split("_")[1]` + `peers[idx]`.
3. **GUI did not resync hotkeys on peer edits** – `_add/_edit/_remove_peer` and
   `PeerForm` dropped `device_id` on edit, so a rename changed the identity.
4. **Peer registry keyed by display name** – `istate.peers[name]`; inbound/
   outbound slots could be overwritten without closing the old socket; the
   server read loop used blocking `recv` without shutdown awareness.
5. **Shutdown did not close peer sockets** – blocking `recv` loops relied on
   daemon-thread death; sockets leaked on shutdown.
6. **Stale OS hotkeys** – config reload updated `istate.hotkeys` but never
   re-registered `RegisterHotKey`, so GUI hotkey changes did not take effect and
   old combos kept firing.
7. **Stuck keys/buttons** – no tracking of pressed keys/buttons; stop/disconnect
   while holding a key left it stuck on the target.
8. **GUI state from process handle** – service state partly derived from
   `service_proc` and a blocking `_runtime_alive()` call on the UI thread; no
   real start/stop/restart waiting.
9. **Tests Windows-only** – `test_service.py` imported `service.py` → `tray.py` →
   `ctypes.windll`, so they could not run on non-Windows.
10. **Docs overpromised** – README/setup/architecture/protocol claimed a working
    `flowshift-viewer` (video), auto-start per monitor, a Tauri/React GUI, and an
    mDNS protocol that do not exist.
11. **Rust `src/service` does not compile** – `network.rs` stores borrowed
    `stream.split()` halves in a struct (lifetime error) and passes an
    `OwnedWriteHalf` where `WriteHalf<TcpStream>` is expected (type error). Also
    logic bugs: inbound connections start no read loop; peers keyed by name;
    `hooks.rs`/`hotkey.rs` hardcode `Ctrl+Alt+0..9` and ignore `Config.hotkeys`;
    `hooks.rs` only sends `KeyDown` (never `KeyUp`); the exit flag is never
    checked in `main.rs`; `inject.rs` sets raw pixels with `MOUSEEVENTF_ABSOLUTE`
    instead of normalised 0..65535 and no injected-marker.
12. **`src/viewer`** is a stub (`println!` + TODO), not a video receiver.

## Refuted / non-issues

- **`e2e_test.py` "passing" hid nothing malicious** – it only exercises one
  hello + one event; it genuinely passes and still does after the refactor.
- **Runtime singleton mutex** works; the earlier "runtime not running after
  churn" during testing was a *leftover pre-existing tray.py process* (PID 67868)
  holding ports 45781/45782, not a code bug. After clearing it, the stress test
  passes and the freshly launched runtime shuts down cleanly with no zombies.
- **Mouse scaling maths** was essentially correct; it lacked clamping and a
  shared, tested implementation (now added).

## Fixed Problems

- File logging restored and made thread-safe (`tray.py:log`).
- New `runtime_model.py`: stable peer identity, `forward_peer:<identity>` action
  model, legacy migration, default/sync hotkeys, framing, `FramedReader`,
  `scale_mouse_point`/`normalize_absolute`, `PressTracker`. Used by both
  `tray.py` and `gui.py` (single source of truth).
- Hotkey activation everywhere goes through `resolve_peer_by_action`; no more
  index arithmetic. Invalid targets are logged/ignored, never repointed.
- `PeerForm` preserves `device_id`; GUI peer edits call `sync_hotkeys` and show
  invalid hotkeys as "(ungültig)".
- OS hotkeys: `register_runtime_hotkeys` / `unregister_runtime_hotkeys`, id→binding
  map, return values logged; config reload posts `WM_RELOAD_HOTKEYS` so
  re-registration runs on the window thread.
- Peer registry keyed by stable identity with alias set; `install_peer_connection`
  closes replaced sockets; `remove_peer_connection` / `close_all_peer_connections`;
  `request_shutdown` closes all peer sockets; read loops are `FramedReader`-based,
  timeout-tolerant and check `_shutdown_event`; self-connections rejected.
- Pressed-state cleanup: source flushes synthetic `key_up`/`mouseup` to the peer
  on deactivate; target releases injected keys/buttons on peer disconnect/shutdown.
- Mouse injection normalised via `normalize_absolute`, clamped into target screen.
- GUI service state machine (`stopped/starting/running/stopping/error`) driven by
  the control socket with start/stop timeouts; `_update_status` no longer blocks
  the UI thread.
- Local-IP cache given a 30s TTL (avoids stale results after network changes).
- Tests moved to pure logic; `test_service.py` rewritten to import only
  `runtime_model`; added `reconnect_stress_test.py`.
- Rust: `src/service` + `src/viewer` excluded from the Cargo workspace and marked
  experimental in code-adjacent docs; docs corrected.

## Files Changed

- `src/python/runtime_model.py` (new)
- `src/python/tray.py` (logging, identity registry, hotkey activation, OS-hotkey
  re-registration, forwarding + cleanup, dedup against runtime_model)
- `src/python/gui.py` (runtime_model imports, peer/hotkey sync, PeerForm device_id,
  service state machine)
- `src/python/test_service.py` (rewritten, platform independent)
- `src/python/reconnect_stress_test.py` (new)
- `Cargo.toml` (exclude experimental service/viewer, keep shared)
- `README.md`, `HANDOFF_CURRENT.md`, `MANUAL_TEST_CHECKLIST.md`
- `docs/architecture.md`, `docs/setup.md`, `docs/protocol.md`, `docs/hotkey-config.md`
- `src/python/README.md`

## Tests Added

- `src/python/test_service.py` (53 pure-logic checks): hotkey text helpers, peer
  identity, default/legacy hotkeys, **index-drift (delete/rename/insert)**,
  **legacy migration incl. out-of-range → invalid, no crash**, `return_local`
  unchanged, endpoint-only peers, mouse scaling (corners, clamp, negative origin,
  identity), `normalize_absolute`, protocol framing + `FramedReader` (timeout,
  split frame, back-to-back), `PressTracker` cleanup.
- `src/python/reconnect_stress_test.py`: launches the runtime, N reconnect cycles
  (hello + input each), verifies still-running, then clean shutdown via control
  socket and process exit.

## Commands Run (this environment: Windows, pwsh, Python present, cargo absent)

```
python -m py_compile src/python/tray.py src/python/gui.py src/python/e2e_test.py \
    src/python/service.py src/python/test_service.py src/python/runtime_model.py \
    src/python/reconnect_stress_test.py      # EXIT 0
python src/python/test_service.py             # EXIT 0  (all 53 checks PASS)
python src/python/e2e_test.py                 # EXIT 0  ("All E2E tests passed!")
python src/python/reconnect_stress_test.py 30 # EXIT 0  (30 rounds + clean shutdown)
cargo check --workspace                       # NOT RUN – cargo not installed
cargo test  --workspace                       # NOT RUN – cargo not installed
```

`cargo`/`rustc` are not available on this machine (`Get-Command cargo` → not
found). These commands were therefore **not executed**; no Rust build or test
result is claimed.

## Results

- Productive Python path: compiles, unit tests pass, end-to-end handshake+input
  passes, 30x reconnect + clean shutdown passes, no leftover listeners/zombies
  after shutdown (verified via `Get-NetTCPConnection` on 45781/45782).
- File logging verified writing to `src/python/flowshift.log`.
- Docs and code no longer contradict each other about video/viewer/Tauri.

## Manual Checks Still Required (need two real Windows machines)

- Real keyboard/mouse forwarding incl. modifiers, buttons, wheel across devices.
- No stuck keys after stop/disconnect/crash under real hooks.
- Mouse lands correctly on multi-monitor / mixed-resolution targets.
- Start/Stop 20x and Connect/Disconnect 20x on hardware.
- Admin-elevated start path and injection into elevated windows.
- See `MANUAL_TEST_CHECKLIST.md`.

## Remaining Risks

- Input hooks / `SendInput` cannot be exercised headlessly; the automated tests
  cover logic + networking + lifecycle, not the actual OS hook path.
- Two-machine behaviour (real LAN latency, firewall, symmetric connect races) is
  only partially provable locally; loopback reconnect is covered, cross-host is
  a manual check.
- Rust is **unverified** (no compiler here). `src/service` is known not to
  compile and is excluded; a future rewrite must align it with the Python wire
  protocol in `docs/protocol.md` before it can be re-included and built.
- If a peer never advertises a `device_id`, inbound-only connections fall back to
  a best-effort `name:`/`endpoint:` identity; the productive connector always
  also dials outbound, which is the path used for forwarding, so this is low risk.
