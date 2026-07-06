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

---

# Second pass — remaining fixes + cross-platform preparation

Date: 2026-07-06 (later)
Scope: fix the concrete residual bugs from the first pass **without** rolling
back the working refactors, and prepare the protocol/input handling for
cross-platform (Linux↔Linux via evdev/uinput) without touching Rust.

## Residual bugs fixed

1. **Connector ignored peer host/port changes.** `peer_token(peer)` is stable
   for peers with a `device_id`, so editing host/port in the GUI kept the same
   token and the running connector thread kept using its stale `peer` copy
   (`connect_one` read host/port once at thread start). Fix:
   - Connectors are now tracked as `{"thread", "host", "port", "stop": Event}`.
   - `connect_to_peers` builds `desired = {token: (host, port)}` and reconciles
     against the running set via the new pure helper
     `runtime_model.diff_connectors` -> `(to_stop, to_start)`.
   - A changed address stops the old connector (`stop_event`, which also breaks
     the `peer_handler` read loop so its socket is dropped) and starts a fresh
     connector on the new address. `connect_one` also re-reads the peer by token
     each loop iteration. All transitions are logged
     (`address changed <old> -> <new>, restarting connector`).
   - **Verified live**: with a `device_id` peer, editing the host from
     `127.0.0.2` to `127.0.0.3` produced the restart log and a new connector to
     `.3`; the old one stopped. Same mechanism covers port changes.

2. **Invalid hotkeys were registered at OS level.** `register_runtime_hotkeys`
   registered every hotkey including `key == 0` and unresolved
   `forward_peer:<...>`. Fix: it now calls
   `runtime_model.hotkey_registration_error(config, hk)` and **skips**
   non-registrable hotkeys with a warning (label, hotkey text, action, reason).
   `return_local` and resolvable `forward_peer:<device>` still register.

3. **GUI profile area used display names.** `_render_profile_rows` mapped runtime
   peers by `name`, compared `active_peer == name`, and passed `name` to
   activate/ping. Fix: it maps runtime status rows by stable `identity`
   (`runtime_model.index_by_identity`), selects via `active_peer_identity`, and
   passes `peer_identity(peer)` to `_activate_profile` / `_ping_profile`.
   `find_config_peer` in `tray.py` now also matches by identity so
   ping-by-identity resolves. Two peers with the same display name but different
   `device_id` are handled correctly; renaming one does not change activation.

4. **e2e_test.py was not honestly Windows-only.** Rewritten:
   `is_supported()` (Windows + `ctypes.windll`) → on non-Windows it prints a
   skip line and returns exit code 0. On Windows it waits on the control socket,
   prints the runtime's captured output on failure, runs the peer handshake +
   input test, and shuts down cleanly via the control socket.

## Cross-platform preparation (Linux↔Linux groundwork)

Prepared, **not** productive. Windows path unchanged and still green.

- **Capability model / hello v1** — new `src/python/platform_capabilities.py`.
  The `hello` (and `ping`/`pong`) now carry `protocol_version`, `os`, `desktop`,
  `input_backend`, `screen` (with both `x/y` and `left/top`) and a
  `capabilities` block. `parse_hello` tolerates old peers (missing fields →
  conservative defaults; Windows assumed input-capable, unknown-OS assumed
  nothing). `tray.py` builds its hello from the Windows backend's real
  capabilities.
- **Input backend abstraction** — new `src/python/input_backends/`
  (`base.InputBackend`, `windows_win32`, `linux_stub`, `unsupported`,
  `get_backend`). Windows reports full capabilities + real screen info (native
  capture/inject stay in `tray.py`). `LinuxStubBackend` honestly reports
  **not implemented** (no capture/inject), declares `requires_uinput/evdev/
  privileged_helper`, and never crashes (`inject_event` raises a controlled
  `BackendUnavailable`, `release_all` is a safe no-op). `UnsupportedBackend`
  raises controlled errors. Importing the package never crashes on non-Windows.
- **Platform-neutral events + key mapping** — new `src/python/keymap.py`
  (Windows-VK ↔ canonical name ↔ Linux-evdev code, accurate subset) and
  `src/python/input_events.py` (convert current Windows event dicts ↔ neutral
  `kind`-based events). Design rule: same-OS transport uses `native_code`
  loss-lessly, cross-OS uses the canonical `code`. The productive wire still
  sends Windows VK events; the neutral model is implemented + unit-tested and is
  the migration target (documented, not silently switched).
- **Docs** — `docs/linux_backend_plan.md` (evdev capture, uinput injection,
  permissions via udev/group/helper, X11+Wayland via uinput, cleanup, tests,
  phasing). `protocol.md` documents hello v1 + the neutral event model + mapping.
  `architecture.md`, `README.md`, `setup.md`, `HANDOFF_CURRENT.md`,
  `MANUAL_TEST_CHECKLIST.md` updated. All clearly state Linux is **prepared, not
  working**, and Linux input is evdev/uinput (not X11/Wayland as fundament).
- **Rust** — deliberately untouched. Still experimental, excluded from the Cargo
  workspace, not claimed to be fixed. A future cross-platform native agent would
  be a clean `flowshift-agent`, not a patch on the old stub.

## New / changed files (second pass)

- New: `src/python/keymap.py`, `src/python/input_events.py`,
  `src/python/platform_capabilities.py`, `src/python/input_backends/` (5 files),
  `docs/linux_backend_plan.md`.
- Changed: `src/python/runtime_model.py` (`diff_connectors`,
  `hotkey_registration_error`, `index_by_identity`), `src/python/tray.py`
  (connector reconciliation + stop events, hotkey registration validation,
  identity-aware `find_config_peer`, capability-rich hello, `peer_handler`
  `stop_event`), `src/python/gui.py` (identity-based profile rows + toggle),
  `src/python/e2e_test.py` (skip + robust), `src/python/test_service.py`
  (+53 checks), docs (protocol/architecture/README/setup/HANDOFF/checklist).

## Tests added (second pass)

In `test_service.py` (pure logic, any OS), now **106 checks total**:
- hotkey registration validity (return_local, valid forward, unresolved forward,
  unknown-device forward, `key == 0`);
- connector reconciliation (`diff_connectors`): host change, port change,
  unchanged, removed, new;
- GUI identity mapping: two same-named peers get distinct identities, forward to
  device B resolves to B, `index_by_identity` distinguishes rows, rename keeps
  identity stable;
- capability model: hello v1 fields, screen carries `x/y`+`left/top`, tolerant
  parsing of old/unknown-OS hellos, all capability keys present;
- input backends: Windows caps True, Linux stub does not claim inject +
  declares uinput/evdev + `inject_event` raises controlled + `release_all` safe,
  unsupported backend raises controlled;
- key mapping + neutral events: VK↔canonical↔evdev, button id↔name, win↔neutral
  round trips for key/mouse/wheel/buttons, linux-sourced neutral key maps to a
  Windows VK;
- `e2e_test.is_supported()` reflects the platform (clean skip off-Windows).

## Commands run (this environment: Windows, pwsh, Python present, cargo absent)

```
python -m py_compile src/python/*.py src/python/input_backends/*.py   # EXIT 0
python src/python/test_service.py             # EXIT 0 (106 checks PASS)
python src/python/e2e_test.py                 # EXIT 0
python src/python/reconnect_stress_test.py 30 # EXIT 0 (30 rounds + clean shutdown)
# live connector address-change check: host 127.0.0.2 -> 127.0.0.3 restarted the
# connector (log: "address changed ... restarting connector"); config restored.
cargo check/test                              # NOT RUN - cargo/rustc not installed
```

## Honest status after second pass

- Windows↔Windows Python path: unchanged behaviour, all automated tests green,
  connector now reacts to address edits, no invalid OS hotkeys, GUI profiles are
  identity-correct.
- Linux↔Linux: **prepared only** — protocol, capabilities, backend abstraction
  and key mapping exist and are tested, but **no evdev capture and no uinput
  injection are implemented**. Linux input must not be considered working until
  that code exists and is manually tested on X11, Wayland/KDE and Wayland/GNOME.
- Rust: still experimental/excluded, not touched, not claimed fixed.
