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

---

# Third pass — live-readiness (CMD/UAC/gating/ping/inject/start-stop/live-test)

Date: 2026-07-06 (later). Scope: make two-device live tests (Laptop -> Surface)
actually possible. Rust untouched, viewer/Tauri untouched, clipboard NOT started.

## 1. No stray CMD windows
- Root cause: several `subprocess.run`/`Popen` calls (PowerShell IP/broadcast
  queries in `tray.py` + `gui.py`, the runtime spawn, the git calls) ran without
  `CREATE_NO_WINDOW`, flashing a console.
- Fix: a shared `version.CREATE_NO_WINDOW` (0x08000000 on Windows, 0 elsewhere)
  is now passed to **every** subprocess (PowerShell IP scans, broadcast targets,
  git in `version.py`, port lookup, taskkill, runtime spawn). The runtime is
  launched with `pythonw.exe`. No CMD window on start/ping/reconnect/status/live.

## 2. UAC not forced on every start
- Removed the per-start `ShellExecute(..., "runas", ...)` and its message box.
- Default start is now **user mode** (no UAC). For the elevated case there is a
  one-time **Scheduled Task** path (`elevated_task.py`, `schtasks /RL HIGHEST`):
  "Elevated Runtime installieren" prompts UAC **once**; afterwards the GUI starts
  the runtime via `schtasks /Run` with **no** prompt. "Elevated Runtime
  entfernen" removes it. GUI shows the mode (User / Elevated Task installed).

## 3. No message boxes for normal hints
- Removed the kill-switch info box and the admin info/confirm boxes. Hints and
  errors now go to the GUI status area + log only. (Only the destructive
  "Logdatei leeren" keeps a confirm, which is a real decision.)

## 4. Clearable log view
- GUI buttons **Logansicht leeren** (clears the visible Text widget) and
  **Logdatei leeren** (truncates `flowshift.log` after a confirm). Logging keeps
  working afterwards.

## 5. Network never swallows input without an active profile
- New fail-safe predicate `runtime_model.should_suppress_input(active, connected)`:
  input may be suppressed **only** when forwarding is active AND a peer is
  connected. `keyboard_proc`/`mouse_proc` now gate suppression on
  `forwarding_ready()` (active + live slot); if forwarding is on but the peer is
  not connected, input passes through locally (logged, rate-limited).
- `forward_loop` deactivates forwarding on a send failure or missing connection
  (fail-safe back to local). Hooks are only installed while a profile is active.
- Status now **separates** network / forwarding / capture (see #11).

## 6. Ping/pong
- `ping`/`pong` use the v1 message (`protocol_version`, `os`, `capabilities`,
  `timestamp`, `app_version`, `git_commit`). Local logs
  `ping start/sent`, `pong received rtt=...`; remote logs `ping received`,
  `pong sent`. Ping uses a short-lived socket, never activates forwarding and
  never touches input. Verified: gating check shows ping/synthetic paths do not
  enable forwarding.

## 7. Mouse injection
- `SendInput` now has explicit argtypes and its return value is checked;
  `_send_input` logs `inserted`/`GetLastError` on failure. Mouse-move logging is
  rate-limited and now prints src, source/target screen, scaled and normalized
  (0..65535) coords + result. Absolute + `MOUSEEVENTF_VIRTUALDESK` retained.
  (Real cursor motion needs the two-device manual test; the math is unit-tested.)

## 8. Keyboard + type_text
- Key down/up unchanged (with pressed-key cleanup). Added a reliable
  **`type_text`** Unicode inject path (`KEYEVENTF_UNICODE`) for the live test, and
  a control command `type_text` + `send_synthetic` that push events into the
  forward pipeline so they are genuinely sent to the peer and injected there
  (proves forwarding, not a remote file-write).

## 9. Start/Stop/Restart
- Root cause reproduced: a **zombie process holding the runtime mutex/port**
  makes a new start log "another instance already running" -> the control socket
  never comes up -> "Service-Start abgelaufen". (Observed a stale PID holding the
  mutex during testing.)
- Fixes: runtime spawned with `pythonw` + `CREATE_NO_WINDOW`, stdout/stderr
  redirected to `flowshift_runtime.out` (start crashes are now visible, not
  swallowed). `_begin_start` detects a hanging runtime (PID on the control port)
  and refuses with a clear message; **Hängende Runtime beenden** kills the PID on
  ports 45782/45781. Status snapshot exposes `runtime_started_at` + version.

## 10. Live-test mode (user-triggered only)
- GUI **Live Test** tab + `live_network_test.py` CLI. Version equality gate: the
  hello now carries `app_version`/`git_commit`/`git_branch`; the runtime status
  exposes local version and per-peer `remote_version`. The `Live Test starten`
  button stays **disabled** until local and remote git commits match (or an
  explicit override), and it warns when git is dirty / not pushed. The scenario
  (activate -> synthetic mouse move + click -> `type_text` -> deactivate) runs
  **only on click**. Nothing runs automatically.

## 11. Network vs forwarding vs capture shown separately
- Status snapshot adds `network_connected`, `network_peer`, `forwarding_active`,
  `forwarding_target`, `capture_active`. The GUI Profile tab now shows
  "Netzwerk: verbunden mit X" / "Forwarding: inaktiv" / "Capture: aus" so a mere
  network connection is never shown as active forwarding.

## 12. Fail-safe (no lost events)
- Covered by #5: `should_suppress_input`, `forwarding_ready`, and `forward_loop`
  deactivation on send failure guarantee local input is never silently dropped
  when the peer is absent or sending fails.

## New / changed files (third pass)
- New: `src/python/version.py`, `src/python/elevated_task.py`,
  `src/python/live_network_test.py`.
- Changed: `tray.py` (SendInput checks, type_text, hello version/timestamp,
  gating + fail-safe, status separation + version fields, send_synthetic/type_text
  control commands, NO_WINDOW subprocess, remote version storage),
  `gui.py` (no runas/messageboxes, user-mode start + scheduled-task path, log
  clear buttons, hanging-runtime kill, Live Test tab, separated status),
  `runtime_model.py` (`should_suppress_input`), `test_service.py` (+20 checks),
  `MANUAL_TEST_CHECKLIST.md`.

## Tests
- `test_service.py`: **126 checks**, incl. gating fail-safe (4 cases),
  version_info + `CREATE_NO_WINDOW`, elevated-task command builders, ping/pong
  message shape, and that `type_text` is not a hardware event.
- `e2e_test.py` OK; `reconnect_stress_test.py 30` OK; clean shutdown, no leftover
  listeners.
- **Live control-path integration** (throwaway, synthetic connected peer):
  before activation `send_synthetic` is refused (gating); after activation
  mouse move + click + `type_text` are really forwarded to the peer; clean
  shutdown. Passed.

## Still requires the real two-device manual run (Laptop + Surface)
- Actual cursor movement + clicks + typing landing on the Surface (hooks +
  SendInput can only be proven on hardware).
- 3x Laptop stop/start/reconnect while the Surface runtime stays up.
- File `FlowShift_Remote_Test.txt` created on the Surface desktop via remote
  input. Editor used (Notepad vs Notepad++) to be noted in the run.
- See the "Live Test Laptop -> Surface" checklist in `MANUAL_TEST_CHECKLIST.md`.

## Not started
- Clipboard sync is intentionally **not** begun (this task precedes it).

---

# Fourth pass — the real start/stop/restart root cause (hardware-proven)

Date: 2026-07-06 (later). Found while running **real** start/stop cycles on the
laptop (not just the loopback reconnect test).

## Root cause
`request_shutdown` (and the kill-switch / watchdog paths) called
`user32.PostQuitMessage(0)` from a **worker/hook thread**. `PostQuitMessage`
only posts `WM_QUIT` to the **calling thread's** queue, but the message loop runs
in the **main window thread**. So `GetMessageW` never received `WM_QUIT`: the
control socket closed (control thread saw `_shutdown_event`) — making the runtime
*look* stopped — but the **process kept running** in `GetMessageW`, holding the
singleton mutex. The next start then logged "another instance already running"
and its control socket never came up → the GUI's "Service-Start abgelaufen".

This is exactly the reported "after Stop the service won't start again".

## Fix
- New `signal_main_quit()`: posts a custom `WM_APP_QUIT` to the window; `wnd_proc`
  (which runs on the main thread) then calls `PostQuitMessage(0)`, so the main
  `GetMessageW` loop actually exits and the process terminates.
- All off-main-thread quit calls now use `signal_main_quit()` (shutdown control
  handler, keyboard kill-switch, watchdog). The main-thread `WM_APP_QUIT` handler
  and the no-window fallback still call `PostQuitMessage` directly.

## Hardware proof (this laptop)
- 6/6 real start → status(version) → shutdown → **process-exit** cycles pass, no
  leftover listeners, no mutex zombie (spawned exactly like the GUI:
  `pythonw tray.py --tray`, `CREATE_NO_WINDOW`).
- `reconnect_stress_test.py` now **asserts the process actually exits** (it
  previously only checked the control socket, which masked this bug). 20 rounds
  pass incl. process exit (code 0).
- Live control-path recheck still passes (gating + synthetic mouse/click +
  `type_text` forwarded; clean shutdown).
- `type_text` now injects `\n` as a real Enter key (VK_RETURN) and `\t` as Tab,
  so multi-line poems land as proper lines on the remote.

## Live driver added
- `src/python/poem_live_test.py`: one connection-test cycle = activate forwarding,
  `Ctrl+End`, type one poem, `Ctrl+S`. Run once per connection test to append a
  poem to the same file on the remote (see live checklist).

---

# Fifth pass — mouse delta, tray UX, fwd_state protocol, direction labels

Date: 2026-07-06 (later). Commit `e137af8`.
Scope: fix the remaining live bugs found during the first real two-device session.

## 1. Mouse movement frozen at connection origin (root cause + fix)

**Root cause.** The previous approach forwarded mouse moves as absolute
coordinates (`x`, `y`). When the source hook suppresses a movement (returns 1),
the OS does NOT move the cursor. So `ms.pt` on the NEXT event is
`actual_cursor_pos + hardware_delta`, not a running accumulation. The code was
updating `_mouse_last_pos = ms.pt` (the intended, suppressed position) after
each event. On the next event, `prev = ms.pt_prev` (suppressed) while
`ms.pt_new = actual_pos + next_delta`. The computed delta was
`next_delta − prev_delta` instead of `next_delta`. Result: deltas were wrong or
zero, and the target cursor never moved.

**Fix.** Switched to relative delta forwarding with a **frozen anchor**:
- At activation, `_mouse_last_pos` is set to `GetCursorPos()` (the real cursor
  position).
- In the hook, the anchor is **never updated**. Every event computes
  `dx = ms.pt.x − anchor.x`, `dy = ms.pt.y − anchor.y`, which equals the true
  hardware delta because `ms.pt = anchor + hardware_delta` (cursor is always at
  the anchor due to suppression).
- Target injects with `MOUSEEVENTF_MOVE` (no `MOUSEEVENTF_ABSOLUTE`).
- Absolute `mousemove` (with `x`,`y`) is still supported for GUI synthetic events.

## 2. Tray single-click accidentally activated a profile

**Root cause.** `wnd_proc` handled `WM_LBUTTONUP` by toggling forwarding.
A user clicking the tray icon to open the menu inadvertently activated or
deactivated a profile.

**Fix.**
- Single left-click: no action.
- Double left-click (`WM_LBUTTONDBLCLK`): opens settings GUI.
- Right-click: shows menu (unchanged).

## 3. Tray tooltip showed wrong direction before profile activation

**Root cause.** `update_tray` called `build_connection_summary()` which
returned a direction label based on the background TCP connection, not on
active forwarding. E.g. `Surface-Viktor → Laptop-Viktor` even with no profile
active.

**Fix.** Tooltip shows:
- `FlowShift | Laptop → Surface` — only when a profile is active.
- `FlowShift` — in all other states.
The concept of "verbunden" (connected) is not surfaced in the tooltip at all.

## 4. "verbunden" removed as a user-visible state

**Design decision.** From the user's perspective, "verbunden" means an active
forwarding profile, not a background TCP link. Multiple FlowShift instances on
the same LAN all maintain TCP connections to each other silently; this is an
implementation detail. Before a profile is activated, nothing is "connected".

**Changes.**
- Profile rows: no direction label, no "verbunden", no "offline" when no profile
  is active. Columns are empty.
- Tooltip: just `FlowShift` when inactive.
- `build_status_snapshot`: `direction = ""` for peers with no active forwarding.
- `_render_profile_rows`: `conn_text = rt.get("link_label") or ""`

## 5. Bidirectional direction display — `fwd_state` protocol message

**Problem.** When Laptop activates Laptop→Surface, only Laptop's GUI showed
the direction. Surface's GUI had no way to know it was the forwarding target.

**Fix.** New peer message `{"type": "fwd_state", "active": bool, "source_name": str}`:
- Sent by the activating machine to the peer immediately after activation.
- Sent (with `active: false`) on deactivation.
- Received in `peer_handler` read loop; stored in the link as
  `remote_forwarding_active` + `remote_forwarding_source`.
- Included in the status snapshot per-peer; GUI profile rows read it to show
  `Laptop → Surface` (Ziel) on Surface when Laptop is the source.

## 6. Circular forwarding prevention (GUI)

**Problem.** If Laptop→Surface is active, a click on "Aktivieren" for a second
peer (or Surface trying to activate Surface→Laptop in its GUI) could start a
second active forwarding profile. Injected-flag filtering prevents a true loop,
but the UX was confusing.

**Fix.** In `_render_profile_rows`: when `active_identity` is set, all
"Aktivieren" buttons for non-active peers are disabled. User must deactivate the
active profile before switching.
Note: The injected-flag filter (`LLMHF_INJECTED` / `INJECTED_EXTRA_INFO`) in
the hook callback already breaks any forwarding loop at the protocol level.

## 7. Forwarding toggle button shows target

**Fix.** `_sync_forwarding_button` now shows:
- `Forwarding starten → Surface-Viktor` (inactive).
- `Forwarding stoppen (Surface-Viktor)` (active).

## 8. Live test results (5 poem cycles, verified on hardware)

Run on commit `d0bbee3` (preceding this pass), hardware Laptop + Surface:
- 5 connection cycles, 2 including Laptop service restarts.
- Ping 16–42 ms all cycles.
- Gedichte 1–5 written correctly to `FlowShift_Gedichte.txt` on Surface.
- Ctrl+S (save), Ctrl+End (cursor to end) forwarded via send_synthetic.
- All cycles: reconnect clean after Laptop restarts.
- Individual key forwarding and `type_text` confirmed working.

## Files changed (fifth pass)

- `src/python/tray.py`: mouse delta anchor, `WM_LBUTTONDBLCLK`, tooltip, `fwd_state`
  notify + receive handler, `remote_forwarding_active/source` link fields,
  `build_status_snapshot` direction computation, `_notify_fwd_state` function.
- `src/python/gui.py`: profile rows ("verbunden" removed, circular-prevention disable,
  `fwd_state` labels), `_sync_forwarding_button` target name.
- `src/python/input_events.py`: relative mouse move (`dx`, `dy`, `mode:"relative"`)
  in `win_event_to_neutral` and `neutral_to_win_event`.
- `src/python/test_service.py`: 2 new checks for relative mouse move roundtrip.

## Tests (fifth pass)

`test_service.py`: **128 checks**, all pass. Includes:
- `win mousemove relative → neutral relative`
- `neutral relative mouse_move → win mousemove`

`py_compile` all files: EXIT 0.
`reconnect_stress_test.py 30`: EXIT 0.

## Still needs live verification (after pull + restart on both devices)

- Mouse movement: cursor must move on Surface proportionally to Laptop hardware input.
- Shift+Ctrl+Arrow text selection (code analysis: should work, needs physical test).
- `fwd_state` bidirectional label: Surface GUI must show `Laptop → Surface` when
  Laptop activates.
- Tray double-click, single-click-no-action, tooltip accuracy.

---

# Sixth pass — keyboard selection, mouse smoothing, installer, repo hygiene

Scope (priority order): fix Shift/Ctrl+Shift selection, reduce mouse jitter,
clean the repo, add a template config, ship a one-click NSSM installer +
uninstaller, add a global frame-size limit, make the reconnect stress test
platform-clean, add a remote desktop-file live test, and correct the docs so
they do not overstate what has been hardware-verified. Clipboard NOT started.

## 1. Shift / Ctrl+Shift selection — root cause + fix

**Root cause (confirmed by code analysis):** `tray.inject` injected key events
with `SendInput` using only `wVk` and no `KEYEVENTF_EXTENDEDKEY`. For navigation
keys (arrows, Home/End, Insert/Delete, PageUp/Down, right Ctrl/Alt, ...) Windows
then derives the *numpad* scan code. With Shift held, Windows uses Shift to
temporarily toggle NumLock semantics instead of registering "extend selection",
so Shift+Arrow / Ctrl+Shift+Arrow / Shift+Home/End did not select text remotely.

**Fix:**
- New `runtime_model.EXTENDED_KEY_VKS` + `is_extended_key(vk)` (pure, unit-tested).
- `tray.inject` (both the `key`/`key_up` path and `_inject_vk_tap`) now OR-in
  `KEYEVENTF_EXTENDEDKEY` for extended keys.
- Modifiers (Shift/Ctrl/Alt/Win) were already forwarded as normal `key`/`key_up`
  events (no filtering), and event order is preserved (single ordered queue →
  single sender), so held-Shift + arrow arrives correctly on the target.
- Held modifiers are released on Return-to-local / disconnect / shutdown via the
  existing `PressTracker` cleanup (documented in protocol.md).

**Status:** implemented + unit-tested (extended-key classification). Real remote
selection in Notepad/Notepad++ still needs a two-device hardware test (listed
below) — SendInput's extended-key behaviour cannot be proven headlessly.

## 2. Mouse jitter — analysis + smoothing

**Analysis of the previous behaviour:**
- The hook already enqueued events and a separate `forward_loop` sent them, so
  the hook never blocked on network I/O (good). But every single hardware
  mouse-move was sent as its own frame and injected with its own `SendInput`
  call — hundreds per second — which is the dominant jitter/stutter source.
- `TCP_NODELAY` was **not** set, so Nagle's algorithm could batch/delay these
  tiny frames, adding latency.
- No coalescing, no sub-pixel accumulation.

**Fix:**
- `runtime_model.MouseCoalescer` (pure, unit-tested): accumulates relative
  deltas, flushes an integer `(dx, dy)` batch, carries sub-pixel remainders.
- `tray.forward_loop` rewritten as a coalescing sender: mouse moves are
  accumulated and flushed every `flush_interval_ms` (default 6 ms ≈ 166 Hz);
  keyboard / mouse-button / wheel events are sent immediately and in order; a
  pending move is flushed BEFORE any such event (clicks land correctly). No
  key/click is ever coalesced or dropped; only moves are merged.
- `TCP_NODELAY` set on every peer socket (accept + connect) via
  `_set_tcp_nodelay`.
- Move logging stays rate-limited (no line per event).
- Configurable via a `"mouse"` block: `flush_interval_ms` (6), `max_batch_ms`
  (12), `sensitivity` (1.0), `accumulate_subpixel` (true), clamped by
  `runtime_model.mouse_settings`.

**Status:** implemented + unit-tested (coalescing sums, sub-pixel preservation,
clamping). Whether it *feels* smoother must be confirmed on hardware (listed
below). The hook is guaranteed not to block on network I/O (enqueue only).

## 3. Repo hygiene + sensitive-data status (honest)

- **Current HEAD is clean:** `git ls-files` shows NO `config.json`,
  `flowshift.log`, `flowshift_runtime.out`, `__pycache__` or `*.pyc` tracked.
  `.gitignore` already covered them; extended it with `.venv/ venv/ env/`,
  `install.log`, `uninstall.log`, `tools/nssm/**/nssm.exe`, `build/`.
- **History leak (IMPORTANT, not hidden):** `config.json` WAS committed in older
  history and **still exists in the remote history**. Those old versions contain
  real data: device names (`Surface-Viktor`, `Stealth-17-VP`, `Viktor-PC`),
  private LAN IPs (`192.168.8.x`, `192.168.1.x`) and device IDs. Commits include
  `a3deed1`, `b5a8b6d`, `d732e99`, `a7443091`, `e7ab6a3`.
- Removing this from history requires a destructive rewrite
  (`git filter-repo` / BFG + force-push), which was **NOT** performed — it needs
  explicit approval and coordination (it rewrites SHAs for everyone). Recommended
  if the private LAN IPs / device names matter. Until then, treat the history as
  containing that data.

## 4. Template config

- Added `src/python/config.example.json`: placeholder device name/id, an example
  peer (`192.168.1.50`, generic), hotkeys, and blocks for `mouse`, `service`,
  `elevated_task`, `logging`, and a disabled `clipboard` placeholder. No real
  IPs / device IDs / private names.
- The app still auto-creates a local `config.json` on first start when missing
  (with a fresh random `device_id` and the machine name).

## 5. One-click installer + uninstaller (NSSM)

- `install_flowshift.bat` (double-click) → `install_flowshift.ps1` (self-elevates
  via UAC). 12 numbered steps, full logging to
  `%ProgramData%\FlowShift\logs\install.log`, window stays open, non-zero exit on
  failure with the reason + log path.
- Installs to `%ProgramFiles%\FlowShift`; data/config/logs in
  `%ProgramData%\FlowShift`. Creates a venv, installs deps (stdlib only — see
  `requirements.txt`), obtains NSSM (bundled `tools\nssm\win64\nssm.exe` if
  present, else downloads `nssm-2.24.zip`), registers the `FlowShiftRuntime`
  service (`<venv>\pythonw.exe "<...>\tray.py" --tray`, AppDirectory, stdout/
  stderr to ProgramData, auto-start, restart-on-failure, graceful stop, env
  `FLOWSHIFT_CONFIG` + `FLOWSHIFT_LOG_DIR`), creates Desktop + Start Menu
  shortcuts (GUI via `pythonw`, Logs folder, Uninstall), starts the service and
  verifies the control socket.
- Python auto-install: tries `winget` (`Python.Python.3.12 --silent`), else the
  official python.org silent installer. Best-effort with clear failure messaging.
- `tray.py` / `gui.py` now honour `FLOWSHIFT_CONFIG` and `FLOWSHIFT_LOG_DIR`
  (with `makedirs`) so an installed, read-only Program Files location never
  writes runtime data into the app folder / repo.
- `uninstall_flowshift.bat` → `uninstall_flowshift.ps1` (self-elevates): stops +
  removes the service (NSSM, `sc.exe` fallback), removes a legacy scheduled task,
  kills lingering PIDs on 45781/45782, removes shortcuts + Start Menu folder +
  Program Files, and optionally (prompt) removes `%ProgramData%\FlowShift`.
- Both PS1 scripts pass the PowerShell language parser.

### 5-caveat: session-0 input limitation (MUST be verified on hardware)

A Windows service runs in **session 0** and generally **cannot capture or inject
interactive input** for the logged-on user (low-level hooks + `SendInput` operate
per-session). The NSSM service provides the runtime lifecycle + control socket,
but ACTUAL input forwarding likely requires the runtime in the interactive user
session (GUI/Tray autostart, or a Scheduled Task "run only when user is logged
on", highest privileges). This is flagged in `install_flowshift.ps1`, the
install checklist, and must be confirmed on hardware. The installer does not
falsely claim forwarding works from session 0.

## 6. Global frame-size limit (pre-clipboard)

- `runtime_model.MAX_FRAME_SIZE = 28 * 1024 * 1024`.
- Enforced in `pack_frame` (raises on oversize), `recv_msg` (rejects oversize
  announced length before reading the body) and `FramedReader._try_parse`
  (rejects instead of buffering). Documented in `docs/protocol.md`.
- Unit-tested: normal frame under limit OK; oversize pack rejected; `recv_msg`
  and `FramedReader` reject oversize announced lengths.

## 7. reconnect_stress_test platform-clean

- Added `is_supported()` (Windows + `ctypes.windll`); on non-Windows it prints
  `[SKIP] ...` and exits 0 (no misleading FAIL). On Windows it also dumps the
  tail of `flowshift_runtime.out` if the control socket never comes up.

## 8. Remote desktop-file live test (real remote input)

- New `src/python/remote_desktop_file_test.py`: proves the actual goal by
  creating `FlowShift_Remote_Test.txt` on the Surface desktop purely via
  forwarded input (Option A: Win+R → `notepad` → type poem → Ctrl+S → type
  `%USERPROFILE%\Desktop\...` → Enter → confirm overwrite). Version-gated
  (refuses unless local/remote git commits match, `--force` to override),
  user-triggered only, `--repeat N` for indexed files, `--check` for a dry
  report. Nothing is written over the network / SMB / remote command.
- Existing `poem_live_test.py` (Notepad++ append) kept for the poem-per-cycle
  flow.

## Files changed / added (sixth pass)

- Changed: `runtime_model.py` (MAX_FRAME_SIZE + framing guards, EXTENDED_KEY_VKS
  + `is_extended_key`, `mouse_settings` + `DEFAULT_MOUSE_SETTINGS`,
  `MouseCoalescer`), `tray.py` (extended-key inject flag, `_set_tcp_nodelay` on
  accept+connect, coalescing `forward_loop`, `FLOWSHIFT_CONFIG`/`FLOWSHIFT_LOG_DIR`
  env + `makedirs`), `gui.py` (same env/config-path support + `makedirs`),
  `reconnect_stress_test.py` (platform guard + runtime-output tail),
  `test_service.py` (+~24 checks), `.gitignore` (venv/installer artefacts),
  `docs/protocol.md` (frame limit, coalescing, extended keys).
- Added: `src/python/config.example.json`, `requirements.txt`,
  `install_flowshift.bat`, `install_flowshift.ps1`, `uninstall_flowshift.bat`,
  `uninstall_flowshift.ps1`, `docs/install_test_checklist.md`,
  `src/python/remote_desktop_file_test.py`.

## Tests (sixth pass, this environment: Windows, Python, no admin)

- `python -m py_compile` all Python + PowerShell parser check: OK.
- `python src/python/test_service.py`: **152 checks**, all pass (adds mouse
  coalescing, sub-pixel, mouse_settings clamping, extended-key classification,
  frame-size limit for pack/recv/FramedReader).
- `python src/python/reconnect_stress_test.py 5`: OK on Windows (control socket
  up, reconnect, clean shutdown, process exit); skips cleanly on non-Windows.
- Installer / uninstaller: PowerShell **parse-checked only**. Not executed (needs
  admin + a real/fresh machine). See `docs/install_test_checklist.md`.

## Honest verification status (corrects the earlier over-claim)

- Hardware-verified on the PREVIOUS relevant commits: keyboard `type_text`,
  reconnect, 5 poem cycles (`FlowShift_Gedichte.txt`), start/stop/restart.
- **NOT yet hardware-verified on the current commit:**
  - the relative mouse-delta smoothing (feels-smoother claim),
  - Shift/Ctrl+Shift extended-key selection,
  - the NSSM service actually forwarding input (session-0 caveat),
  - `remote_desktop_file_test.py` creating the file end-to-end,
  - installer/uninstaller on a fresh machine.
- These are explicitly listed as "needs live verification"; no claim of
  hardware success is made for them.

## Not started (unchanged)

- Clipboard (model/protocol/store/GUI/history) — deliberately NOT begun. The
  frame-size limit was added as the only clipboard-preparatory change.
- Rust — still experimental, excluded, not relevant.

---

# Seventh pass — forward_loop crash, worker supervision, session-0 hardening

Scope: fix the runtime bug where mouse/keyboard events were captured + logged
locally but never reached the receiver; make worker crashes impossible to miss;
add pipeline + session diagnostics; stop selling the NSSM session-0 service as
the input path; clean a private launcher out of the repo. Clipboard NOT started.

## 1. Root cause: forward_loop crashed on a missing import

`tray.forward_loop` referenced `DEFAULT_MOUSE_SETTINGS` but the sixth-pass import
only pulled in `is_extended_key, MouseCoalescer, mouse_settings`. On first
iteration the thread raised `NameError: name 'DEFAULT_MOUSE_SETTINGS' is not
defined` and **died silently** (bare `threading.Thread(...).start()` swallowed the
exception into nothing but the default excepthook, visible only in
`flowshift_runtime.out`). Hooks kept enqueuing events; nothing drained/sent them.

**Fix:** added `DEFAULT_MOUSE_SETTINGS` to the `from runtime_model import (...)`
block in `tray.py`.

## 2. Why the 152 tests did not catch it

`py_compile` and the pure-logic `test_service.py` never start the runtime
threads, so a `NameError` inside a worker's loop body is invisible to them. The
tests exercised `MouseCoalescer`/`mouse_settings` in isolation, not `forward_loop`.

## 3. Workers can no longer die silently (supervision)

New infrastructure in `tray.py`:
- `run_worker(name, target)` wraps every worker: logs `worker started`, catches
  any exception with the **full traceback** to `flowshift.log` +
  `flowshift_runtime.out`, marks the worker failed, and logs `worker exited`.
- `start_worker(name, target)` registers the thread in `_workers` and starts it.
- `worker_health()` → per-worker `{alive, failed, last_error, started_at}`.
- `critical_workers_down()` (ignored during shutdown) and `forward_loop_healthy()`.
- `CRITICAL_WORKERS = forward_loop, inject_loop, network_thread, connect_to_peers,
  local_control_thread`.
- `run()` now launches all workers via `start_worker` (incl. discovery + watchdog).
- **Fail-safe:** `forwarding_ready()` returns False if `forward_loop` is not
  healthy, logging `CRITICAL: forward_loop is not running; forwarding disabled,
  keeping input local` — so a dead sender never causes input to be swallowed.
- Status snapshot exposes `workers`, `critical_workers_down`, `runtime_healthy`.
- GUI shows a red `Runtime: FEHLER — Worker tot: ...` line (green when healthy).

## 4. Event-pipeline diagnostics

`tray.py` counters (`pipe_inc`, `pipeline_snapshot`), surfaced in status as
`pipeline`: `events_queued`, `events_forwarded`, `events_send_failed`,
`input_batches_received`, `events_injected`, `inject_failed`, plus live
`event_queue_size` / `inject_queue_size`. The GUI shows a `Pipeline: ...` line, so
if events do not reach the receiver you can see exactly which stage they stall at.

## 5. Session context (Session 0 detection)

- `session_info()` uses `ProcessIdToSessionId`: reports `session_id`,
  `interactive`, `username`, `is_service_session`.
- On startup, if in Session 0, the runtime logs
  `CRITICAL: FlowShift is running in Session 0 ...`.
- Status carries `session`; the GUI shows a red
  `Session: 0 (Dienst) — Input-Forwarding NICHT möglich!` when applicable.
- The GUI Live Test refuses to start when the local runtime is in Session 0.

## 6. NSSM is no longer the primary input path (installer reworked)

- **Primary autostart is now a user-session Scheduled Task** (`FlowShift`,
  AtLogOn, `LogonType Interactive`, `RunLevel Highest` = no per-start UAC),
  running `<venv>\pythonw.exe tray.py --tray` in the interactive session where
  hooks + SendInput actually work. The installer starts it immediately
  (`Start-ScheduledTask`) and verifies the control socket.
- The NSSM service is **not installed by default**. `-WithNssm` installs it only
  as an OPTIONAL helper, set to **manual start** (`SERVICE_DEMAND_START`) and
  clearly labelled "session 0 — NOT input forwarding", so there is never a state
  where only a session-0 service runs and the GUI/health looks fine while input
  cannot work (the GUI would show the red Session-0 warning anyway).
- Machine env `FLOWSHIFT_CONFIG` / `FLOWSHIFT_LOG_DIR` set so runtime + GUI use
  `%ProgramData%\FlowShift`. Uninstaller removes the task, env vars, service,
  shortcuts, program files (optional data purge).

## 7. Repo hygiene: private launcher removed

- `src/python/start_flowshift.vbs` was tracked and contained a **hardcoded
  developer path** (`C:\Users\Vikto\...\Schule Test\...`). Removed from tracking
  (`git rm --cached`) and deleted from the working tree; added to `.gitignore`
  (`src/python/start_flowshift.vbs`, `*.local.vbs`). Added a safe
  `src/python/start_flowshift.example.vbs` template with placeholder paths.
- **History note (honest):** this VBS with the dev path remains in git history
  (commit `c777cff`), like the old `config.json` versions (device names, LAN IPs,
  IDs). Current HEAD is clean. Removing them from history needs an approved,
  destructive rewrite — NOT performed.

## 8. New test: worker + forwarding smoke test

`src/python/worker_smoke_test.py` (Windows; skips off-Windows) runs a real
runtime process and checks:
- **Test A:** `status.workers.forward_loop.alive` and `inject_loop.alive` are
  true, `runtime_healthy` true, `critical_workers_down` empty, status carries
  `pipeline` + `session`.
- **Test B:** a fake peer connects, the profile is activated, `send_synthetic` is
  sent, and the fake peer **actually receives** an `input` message (proves the
  full hook→queue→forward_loop→socket path, not just "queued");
  `events_forwarded` increments.
- **Test C:** the runtime log contains no `Exception in thread`, `NameError`, or
  `worker crashed`.

This exact test fails if `forward_loop` dies on startup — i.e. it would have
caught the `DEFAULT_MOUSE_SETTINGS` bug.

## Files changed / added (seventh pass)

- Changed: `tray.py` (DEFAULT_MOUSE_SETTINGS import; worker supervision;
  pipeline counters; session_info; Session-0 startup warning; forwarding_ready
  fail-safe on dead forward_loop; status fields workers/pipeline/session),
  `gui.py` (health/session/pipeline display + red warnings, Live-Test Session-0
  guard), `install_flowshift.ps1` (user-session Scheduled Task primary; NSSM
  optional/manual), `uninstall_flowshift.ps1` (remove task + env vars),
  `.gitignore` (private vbs).
- Added: `src/python/worker_smoke_test.py`,
  `src/python/start_flowshift.example.vbs`.
- Removed from tracking: `src/python/start_flowshift.vbs`.

## Tests (seventh pass, this environment: Windows, Python, no admin)

- `py_compile` all Python: OK.
- `test_service.py`: 152 checks pass (unchanged).
- `worker_smoke_test.py`: all checks pass (forward_loop alive, fake peer RECEIVES
  forwarded input, no exceptions in log).
- `reconnect_stress_test.py 3`: OK; skips off-Windows.

## Still needs hardware verification (unchanged intent, restated)

- Mouse move + smoothing, clicks, wheel on Surface.
- Shift / Ctrl+Shift + Arrow/Home/End selection.
- Start/Stop/Reconnect cycles in the user session.
- Installer's user-session Scheduled Task actually starting the runtime
  interactively on a real/fresh machine (and the GUI showing green Session +
  healthy workers).
- `remote_desktop_file_test.py` end-to-end.


---

# Eighth pass — flying switch, per-profile mouse, clipboard foundation

Scope (layered per instruction: model/store/manifest first, then transfer logic,
then settings; interactive UI is the next layer). Delivers the two pre-clipboard
prerequisites fully + the clipboard FOUNDATION with tests. Honest status matrix
in `docs/clipboard.md`. Clipboard interactive layer NOT claimed done.

## 1. Flying forwarding-direction switch (Item 1)

Replaced the hard "block the opposite direction" rule with a clean switch that
never allows both directions at once.

- New pure planner `runtime_model.plan_activation(active, active_peer,
  target_identity, target_remote_forwarding)` + `fwd_switch_ok(status)`.
- New protocol `fwd_control` / `fwd_control_result` (request_deactivate). When
  activating a peer that is currently forwarding TO us, the runtime sends
  `request_deactivate`, WAITS for `ok`, then activates. On timeout/rejected it
  does NOT activate (and never swallows input). On switching away from another
  peer it deactivates locally first, releases held keys/buttons and drains the
  queue (clean cutover).
- Hotkey / tray-menu activation now runs on a worker thread
  (`activate_forward_action_async`) so the hook/window thread never blocks on the
  up-to-3s network handshake. GUI activation (control socket) blocks acceptably
  in its own thread; the GUI logs "Wechsle Richtung..." and shows failures in the
  status log (no popup).
- GUI: the hard "disabled" buttons are gone; other peers show "Wechseln".

**Race bug found + fixed by the new integration test:** the fwd_control waiter
was registered AFTER sending the request, so a fast peer reply was delivered
before the waiter existed -> guaranteed timeout under low latency. Fixed by
registering the waiter BEFORE sending. Verified by worker_smoke_test Test D.

## 2. Per-profile mouse settings (Item 4)

- `runtime_model.resolve_mouse_settings(config, peer)` overlays a peer's `mouse`
  block on the global one (defaults < global < peer), re-clamped.
- On activation the runtime stores the resolved settings and `forward_loop`
  builds its coalescer from them, so each profile can be tuned independently
  (e.g. a slower/smoother Surface).
- GUI: the peer editor (centred, transient) gained a Mausgeschwindigkeit slider
  (0.25–3.0), a Smoothness selector (Direkt/Normal/Smooth/Sehr Smooth ->
  flush_interval) and advanced flush/max_batch/subpixel controls. Saved per peer
  as `peer["mouse"]`. No JSON hand-edit.

## 3. Fewer popups (Item 2, partial)

- Removed the error/`showerror` popups from profile activation; failures go to
  the status log. The peer editor is centred over the main window (transient +
  grab_set) rather than top-left. A full right-side sideboard/drawer refactor is
  a follow-up; this pass reduces the popup flow and centres the remaining dialog.

## 4. Clipboard FOUNDATION (Items 5-12,15,18 logic layer)

Built pure + tested, per the "model/store/manifest first" instruction:

- `clipboard_model.py`: item kinds, sha256, item construction, manifest
  build/parse, **manifest-based sync diff** (dedup by content, only-missing,
  manual-required by size, order preserved), **eviction** (FIFO + size +
  pinning), **byte/rate/ETA/progress formatting** (unit choices),
  **ZIP-strategy decision**, **chunk planning**, disk-space guard, settings +
  clamping.
- `clipboard_store.py`: per-profile persistent store (`index.json` +
  content-addressed `objects/<sha256>`), add/list/get, delete-one, delete-all,
  pin/unpin, size accounting, FIFO+size eviction, persistence across reopen,
  manifest from store, temp cleanup, refcounted blob deletion (dedup-safe).
- `clipboard_protocol.py`: manifest/request/sync_result + transfer
  start/chunk/ack/complete/error/resume builders, base64 chunks under
  `MAX_FRAME_SIZE`, and a pure `ChunkAssembler` (in-order reassembly, resume via
  next-index, duplicate + hash-mismatch detection, final SHA-256 verify).
- Config: full `clipboard` block in `config.example.json`; GUI **Clipboard** tab
  with all settings (enable, limits, units radio groups, direction, ZIP strategy,
  Win+V, paste hotkey, thumbnail size) -> normalised via the model, no JSON edit.
- Installer creates the clipboard store dirs; uninstaller asks about deleting the
  history and always cleans temp.

**NOT yet (next layers, honestly listed in docs/clipboard.md):** the Windows
clipboard watcher + live text sync wiring, file/batch chunk transfer threads in
the runtime, Windows CF read/set (text/HTML/image/CF_HDROP), the clipboard
history window (list/drag-splitter/item-resize/thumbnails), animated GIF preview,
and Win+V interception. The foundation exists so these do not break on missing
plumbing.

## Files added / changed (eighth pass)

- Added: `src/python/clipboard_model.py`, `clipboard_store.py`,
  `clipboard_protocol.py`, `src/python/test_clipboard.py`, `docs/clipboard.md`.
- Changed: `runtime_model.py` (plan_activation, fwd_switch_ok,
  resolve_mouse_settings), `tray.py` (fwd_control protocol + waiter (race-fixed),
  flying-switch activation, async hotkey/menu activation, per-profile mouse in
  forward_loop, drain-on-switch), `gui.py` (flying-switch buttons + status,
  no activation popup, per-peer mouse editor, Clipboard settings tab),
  `worker_smoke_test.py` (Test D flying switch), `test_service.py` (per-profile
  mouse + plan_activation + fwd_switch_ok checks), `config.example.json`
  (per-peer mouse + full clipboard block), `install_flowshift.ps1` /
  `uninstall_flowshift.ps1` (clipboard dirs + history prompt),
  `docs/protocol.md`.

## Tests (eighth pass, this environment: Windows, Python, no admin)

- `py_compile` all: OK.
- `test_service.py`: 166 checks pass (adds per-profile mouse + flying-switch plan).
- `test_clipboard.py`: 69 checks pass (model + store + protocol + assembler).
- `worker_smoke_test.py`: Test A/B/C/D pass — incl. the flying switch end-to-end
  (fake peer forwards to us -> runtime requests deactivate -> activates; exactly
  one direction active). This caught + verified the fwd_control race fix.
- `e2e_test.py`, `reconnect_stress_test.py 3`: OK.

## Still needs hardware verification

- Flying switch between two real devices (both directions, timeout behaviour).
- Per-profile mouse feel (slower/smoother Surface).
- Everything from the seventh pass still pending (mouse smoothing feel,
  Shift/Ctrl+Shift selection, installer on a fresh machine).
- All clipboard interactive behaviour once the next layers are wired.

## Not started

- Clipboard interactive layer (watcher/CF/transfer threads/history window/GIF/
  Win+V) — foundation only this pass.
- Auto-update (Item 16) — comes after the clipboard feature.
- Rust — still experimental, excluded, not relevant.

---

# Ninth pass — clipboard TEXT layer (watcher + CF text + live sync)

Scope: the next vertical slice on the clipboard foundation — capture local text,
sync only-missing text items per profile on activation, and paste a selected item
back into the Windows clipboard. Files/images/GIF/Win+V remain the next layers
(honest matrix in docs/clipboard.md). Clipboard is NOT over-claimed.

## What was built

- `clipboard_win.py`: Windows clipboard access via ctypes — `CF_UNICODETEXT`
  read/set + `GetClipboardSequenceNumber`. Import-safe on any OS (no-ops off
  Windows). Image (`CF_DIB`), files (`CF_HDROP`) and HTML are explicit stubs, not
  fake "working" paths.
- `clipboard_runtime.py` — `ClipboardManager` (transport-agnostic): one
  `ClipboardStore` per peer identity (lazy), `capture_text[_all]`,
  `on_profile_activated` -> send manifest, manifest diff -> request only-missing
  (auto) / placeholder + manual-required (oversize), request handling -> chunked
  transfer, transfer receive -> reassemble (hash-verified) + store, plus
  list/get_text/delete/pin/clear for the GUI. Uses a `send_fn(identity, msg)`
  callback so it is unit-testable with no sockets.
- `tray.py` wiring: a `ClipboardManager` instance (store root
  `%ProgramData%\FlowShift\clipboard`), a supervised `clipboard_watcher` worker
  that polls the clipboard sequence number and captures new text into every
  configured peer's store (only when clipboard is enabled), routing of all
  `clipboard_*` peer messages to the manager, a manifest send on profile
  activation, and control commands `clip_list/capture/get/request/pin/delete/
  clear/sync`. A guard skips re-capturing text we just set ourselves (paste).
- `gui.py`: the Clipboard tab gained a per-profile history viewer (type / name /
  size / status), with buttons to set an item back to the Windows clipboard,
  manual retry/download, pin/unpin, delete and clear.

## Tests

- `test_clipboard_sync.py` (new, pure, two managers via in-memory routing):
  A captures 3 texts -> activate -> B pulls exactly 3 in source order with correct
  bytes; adding 2 more transfers only 2 (dedup); nothing-new transfers 0;
  bidirectional pull; pin + delete; oversize item -> placeholder (manual
  required) -> manual retry transfers + verifies. All pass.
- `worker_smoke_test.py` Test E (new, real runtime process): `clip_capture` ->
  `clip_list` shows the item, duplicate capture deduped, `clip_pin` and
  `clip_delete` work via the control socket. All pass.
- Existing suites unchanged and green: `test_service` 166, `test_clipboard` 69,
  worker_smoke A/B/C/D/E, `e2e`, `reconnect`.

## Honest status

- **Text clipboard works and is tested** end-to-end (manager pipeline) and inside
  the runtime (control API). Real two-device paste still needs a hardware run
  (the CF read/set path can only be proven on Windows hardware), but the sync
  logic, transfer, dedup, ordering and store are verified.
- **NOT yet:** image/GIF/file/batch capture + CF_HDROP/CF_DIB, the rich clipboard
  window (drag splitter / thumbnails / animated GIF), and Win+V interception —
  the next layers, listed in docs/clipboard.md.

## Files added / changed (ninth pass)

- Added: `src/python/clipboard_win.py`, `src/python/clipboard_runtime.py`,
  `src/python/test_clipboard_sync.py`.
- Changed: `tray.py` (manager + watcher + clipboard routing + activation hook +
  control API), `gui.py` (history viewer), `worker_smoke_test.py` (Test E),
  `docs/clipboard.md`, `docs/protocol.md`.

---

# Tenth pass — clipboard FILE / BATCH layer (CF_HDROP + zip bundle)

Scope: the next vertical slice after text — copy files or many files, sync only
the missing ones per profile, and paste them back as a real file list. Rides the
already-tested chunked-transfer path. Images/GIF/rich-window/Win+V remain the
next layers (honest matrix in docs/clipboard.md).

## What was built

- `clipboard_files.py` (pure + filesystem, tested): scan drop paths (dirs walked)
  with per-file sha256 + relative paths + common base; a stable **content
  identity** hash (sorted rel + file-hash) for cross-copy dedup; a
  **deterministic ZIP bundle** (fixed timestamps; ZIP_STORED for already-
  compressed sets, DEFLATED otherwise) so identical file sets produce identical
  bytes; unpack with a path-traversal guard; `make_file_item` (KIND_FILE vs
  KIND_FILE_BATCH, keeps source paths for lazy bundling + copy-free local paste).
- `clipboard_win.py`: real `CF_HDROP` `read_files` (DragQueryFileW) and
  `set_files` (DROPFILES + double-null wide path list) so Explorer/apps can paste.
  Image/HTML remain explicit stubs.
- `clipboard_runtime.py`: `capture_files[_all]` (stores metadata, no blob — the
  zip is built lazily), `_blob_for` (lazy zip on request), `_send_transfer` now
  verifies the **blob** sha (decoupled from the item's content identity, so the
  received zip is stored under the stable content id for dedup), and
  `materialize_files` (local items paste original paths; received items unpack the
  bundle to `temp/incoming/<profile>/<item>` and return the paths).
- `tray.py`: the watcher now captures `CF_HDROP` files (precedence over text) and
  ignores files it just set (paste loop guard); control commands
  `clip_capture_files` and `clip_get` handling for file kinds (materialise +
  `set_files`).

## Tests

- `test_clipboard_files.py` (new, 20 checks): scan/hash, content identity
  (order-independent, changes on edit), deterministic zip (same bytes), unpack
  round-trip + structure, single vs batch item, lazy bundle, dedup by content id,
  path-traversal guard.
- `test_clipboard_sync.py` (extended): A captures a 3-file batch -> activate -> B
  pulls it (available, 3 files) -> B materialises + content matches; re-capturing
  the same files transfers nothing (dedup); local item pastes original source
  paths (no copy).
- `worker_smoke_test.py` Test F (real runtime): `clip_capture_files` -> the file
  batch item is listed with the right kind + file_count.
- All prior suites green: test_service 166, test_clipboard 69, worker_smoke A-F,
  reconnect, e2e.

## Honest status

- **Text and file/batch clipboard work and are tested** end-to-end (manager
  pipeline) and in the runtime (control API). Real two-device paste still needs a
  hardware run (CF_HDROP/CF_UNICODETEXT can only be proven on Windows hardware),
  but the sync, bundling, dedup, ordering, transfer, unpack and store are verified.
- **NOT yet:** image/GIF capture (CF_DIB), the rich clipboard window (drag
  splitter / thumbnails / animated GIF), and Win+V interception — the next layers.

## Files added / changed (tenth pass)

- Added: `src/python/clipboard_files.py`, `src/python/test_clipboard_files.py`.
- Changed: `clipboard_win.py` (CF_HDROP read/set), `clipboard_runtime.py`
  (capture_files, lazy blob, blob-sha transfer, materialize_files), `tray.py`
  (watcher file capture + clip_capture_files + clip_get file kinds),
  `worker_smoke_test.py` (Test F), `test_clipboard_sync.py` (file roundtrip),
  `docs/clipboard.md`, `docs/protocol.md`.
