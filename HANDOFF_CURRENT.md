# FlowShift — Current State (commit e137af8)

## Productive path

- `src/python/tray.py --tray` — productive runtime (Windows).
- `src/python/gui.py` — settings GUI.
- `src/python/runtime_model.py` — shared, platform-independent logic.
- `src/python/service.py` — legacy wrapper only (not used in production).
- Rust (`src/service`, `src/viewer`) — experimental, excluded from the Cargo
  workspace, does NOT compile, NOT worked on. Windows-Python is the focus.

## Architecture summary

```
[GUI gui.py]  --control-socket 45782-->  [Runtime tray.py --tray]
                                            |
                               TCP 45781  <--->  [Peer tray.py --tray]
```

- Runtime listens on TCP 45781 (peer input) and UDP 45781 (discovery).
- Local control socket 127.0.0.1:45782 is the GUI's single source of truth.
- Config is `config.json` in the same directory as tray.py.
- Logging: `flowshift.log` (text) + `flowshift_runtime.out` (crash output only).

## Key design decisions

- **Active profile = "verbunden"**. The background TCP connection between
  FlowShift instances is an implementation detail, invisible to the user.
  The UI shows direction (`Laptop → Surface`) only when forwarding is active.
- Peer identity is stable: `device:<device_id>` when known, else
  `endpoint:<host>:<port>`. Hotkeys and forwarding targets are identity-based,
  not list-index-based.
- Control socket 127.0.0.1:45782 is the source of truth for GUI state.
- Protocol: 4-byte BE length + JSON, same-OS wire uses Windows VK codes
  (neutral/canonical model implemented and tested, not yet the live wire).
- Rust is explicitly experimental and excluded from the workspace.
- Clipboard: not started.
- Linux: scaffolding only (platform_capabilities, input_backends, keymap,
  input_events). No evdev capture, no uinput injection. Not functional.

## What works (hardware-verified on Laptop + Surface, commit e137af8)

### Networking
- Automatic peer discovery (UDP broadcast).
- Bidirectional TCP connection (each side dials the other; inbound + outbound
  slots per peer in the registry, keyed by stable identity).
- Reconnect without restart; connector detects host/port changes and restarts.
- Ping (one-shot RTT check) verified Laptop → Surface, 17–42 ms.

### Forwarding
- Keyboard: individual keys forwarded and injected on target. Modifier keys
  (Shift, Ctrl, Alt) forwarded. `type_text` injects Unicode + Enter/Tab.
- Mouse: relative delta forwarding (source cursor frozen at activation point;
  hardware delta = `ms.pt − anchor`). Buttons and wheel forwarded.
- Wheel with Ctrl/Shift modifiers: works.
- No stuck keys: source flushes key_up/mouseup on deactivate; target releases
  on disconnect/shutdown.
- Injected events carry `INJECTED_EXTRA_INFO` so the local hook ignores them
  (prevents forwarding loops even if both sides activate simultaneously).

### GUI / Tray
- Tray icon: double-click = open settings, right-click = menu.
  Single-click does nothing (no accidental profile activation).
- Tooltip: `FlowShift | Laptop → Surface` only when a profile is active.
  `FlowShift` when nothing is active.
- Profile rows: show direction (`Laptop → Surface` / `Surface → Laptop`) only
  when forwarding is active. Empty otherwise (no "verbunden", no "offline").
- `fwd_state` protocol message: when Laptop activates forwarding to Surface,
  it notifies Surface; Surface's GUI then also shows `Laptop → Surface`.
  Both peers show the same correct direction.
- Circular forwarding prevention: while a profile is active, all other
  "Aktivieren" buttons are disabled in the GUI. To switch, deactivate first.
- "Forwarding starten" button shows target peer: `Forwarding starten → Surface`.
  When active: `Forwarding stoppen (Surface-Viktor)`.
- Status panel separates: network / forwarding / capture (never misleads).
- Start/Stop runtime: `stopping → stopped` / `starting → running` via control
  socket, with timeouts. "Hängende Runtime beenden" kills zombie processes.
- No CMD popups, no UAC on normal start. Elevated path via Scheduled Task
  (one-time UAC, then no prompts).

### Live test results (5 automated poem cycles, commit d0bbee3 / e137af8)
- 5 connection cycles including 2 Laptop service restarts.
- Gedichte 1–5 all written to `FlowShift_Gedichte.txt` on Surface.
- Ctrl+End and Ctrl+S worked via remote forwarding.
- Ping OK all 5 cycles, reconnect clean after restarts.

## What needs live testing (next session)

- **Mouse movement**: delta-based forwarding is new (commit e137af8). Needs
  real cursor-move verification on Surface after pull+restart on both devices.
- **Shift+Ctrl+Arrow** (text selection): code analysis shows it should work
  (modifier VKs are forwarded). Needs physical test.
- **"Forwarding starten" button**: verify it targets the right peer.
- **"Status aktualisieren" / profile rows**: verify direction labels update on
  both peers after activate/deactivate.
- **Tray double-click → settings**: verify opens GUI.

## Automated test coverage (all green)

```
python -m py_compile src/python/*.py src/python/input_backends/*.py  # EXIT 0
python src/python/test_service.py   # 128 checks PASS
python src/python/e2e_test.py       # EXIT 0
python src/python/reconnect_stress_test.py 30  # EXIT 0 (30 rounds + clean shutdown)
```

128 checks cover: hotkey text helpers, peer identity, default/legacy hotkeys,
index-drift (delete/rename/insert), mouse scaling, `normalize_absolute`,
protocol framing + `FramedReader`, `PressTracker` cleanup, hotkey registration
validity, connector reconciliation, GUI identity mapping, capability model,
input backends (Win/Linux/Unsupported), key mapping + neutral events (incl.
relative mouse move roundtrip), gating fail-safe, version info, elevated task
command builders, ping/pong shape, `type_text` classification, `e2e_test`
platform guard.

## Source files (productive Python)

| File | Role |
|---|---|
| `tray.py` | Runtime: hooks, forwarding, injection, network, tray, control socket |
| `gui.py` | GUI: settings, profiles, hotkeys, status, live test |
| `runtime_model.py` | Shared logic: identity, hotkeys, framing, mouse scaling, PressTracker |
| `platform_capabilities.py` | hello v1 + capabilities; tolerant parse |
| `input_backends/` | Backend abstraction (Win/Linux/Unsupported) |
| `keymap.py` | VK ↔ canonical ↔ evdev mapping |
| `input_events.py` | Win event dict ↔ platform-neutral event conversion |
| `version.py` | App/git version + `CREATE_NO_WINDOW` |
| `elevated_task.py` | Scheduled Task command builders |
| `live_network_test.py` | Live-test gating + runner |
| `poem_live_test.py` | Poem-per-cycle live test |
| `test_service.py` | 128 pure-logic checks (any OS) |
| `reconnect_stress_test.py` | Reconnect churn + process-exit verification |
| `e2e_test.py` | Runtime handshake + input (Windows only, skips clean off-Win) |

## Open / not started

- Linux input (evdev/uinput): scaffolding exists, nothing functional.
- Clipboard sync: not started.
- Rust: experimental, excluded, does not compile, not worked on.
- Multi-hop forwarding (A → B → C): not designed.
