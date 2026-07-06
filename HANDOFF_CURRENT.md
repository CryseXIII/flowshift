# FlowShift — Current State (updated: sixth pass)

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

## What is hardware-verified (and on which commit) — honest split

**Hardware-verified on earlier commits (`d0bbee3` and before):**
- Automatic peer discovery (UDP broadcast).
- Bidirectional TCP connection (inbound + outbound slots per peer by identity).
- Reconnect without restart; connector detects host/port changes.
- Ping Laptop → Surface (17–42 ms).
- Keyboard `type_text` (Unicode + Enter/Tab) landing on the Surface.
- 5 poem cycles → `FlowShift_Gedichte.txt` on Surface, incl. 2 Laptop restarts;
  Ctrl+End / Ctrl+S forwarded; clean reconnect each cycle.
- Start/Stop/Restart (process actually exits, no mutex zombie).

**NOT yet hardware-verified (implemented, needs a two-device live run):**
- Relative mouse-delta smoothing / coalescing (the "feels smoother" claim) —
  new sender in commit after `e137af8`.
- Shift / Ctrl+Shift + Arrow/Home/End selection (extended-key fix) — code +
  unit tests only.
- `fwd_state` bidirectional direction label on both GUIs.
- NSSM service actually forwarding input (session-0 caveat — may need the
  runtime in the interactive user session instead).
- `remote_desktop_file_test.py` creating `FlowShift_Remote_Test.txt` end-to-end.
- Installer / uninstaller on a real (ideally fresh) Windows machine.

## Implemented behaviour (code complete, see verification split above)

### Networking
- Discovery, bidirectional TCP, reconnect, host/port-change detection, ping.
- `TCP_NODELAY` on every peer socket (accept + connect) to cut Nagle latency.
- Global frame-size limit `MAX_FRAME_SIZE = 28 MiB` (pack/recv/FramedReader).

### Forwarding
- Keyboard: keys + modifiers forwarded in order; extended keys (arrows, Home/End,
  Insert/Delete, PageUp/Down, right Ctrl/Alt, ...) injected with
  `KEYEVENTF_EXTENDEDKEY` (fixes Shift+Arrow selection). `type_text` for Unicode.
- Mouse: relative delta forwarding with a frozen anchor; a coalescing sender
  merges many small moves and flushes every ~6 ms (configurable) — hook never
  blocks on network I/O. Buttons + wheel sent immediately, in order; a pending
  move is flushed before a click. No key/click coalesced or dropped.
- **Per-profile mouse settings** (Item 4): each peer can carry a `mouse` block
  (sensitivity/flush/max_batch/subpixel) via `resolve_mouse_settings`; the active
  peer's settings drive the coalescer. Editable in the GUI peer editor.
- **Flying direction switch** (Item 1): activating a peer that forwards to us
  sends `fwd_control request_deactivate`, waits for `ok`, then activates — never
  both directions at once, never activates on timeout, never swallows input on a
  failed switch. Hotkey/menu activation runs async so the hook never blocks.
- No stuck keys/modifiers: source flushes key_up/mouseup on deactivate; target
  releases on disconnect/shutdown.
- Injected events carry `INJECTED_EXTRA_INFO` so the local hook ignores them.

### Clipboard (text layer working; images/files/GIF/Win+V next)
- Foundation: `clipboard_model.py` / `clipboard_store.py` / `clipboard_protocol.py`
  — per-profile history, content-addressed dedup, manifest sync (only-missing,
  order-preserving), FIFO+size eviction + pinning, chunked transfer with a
  resume/retry/hash-verify `ChunkAssembler`, ZIP-strategy, formatting, limits.
- **Text vertical slice DONE + tested:** `clipboard_win.py` (CF_UNICODETEXT
  read/set), `clipboard_runtime.py` `ClipboardManager` (capture, on-activation
  manifest exchange, request only-missing, chunked send/receive, dedup,
  manual-required + retry). Wired in `tray.py` (supervised `clipboard_watcher`,
  `clipboard_*` routing, activation hook, `clip_*` control API). GUI Clipboard tab
  has all settings + a per-profile history list (paste/pin/delete/retry/clear).
- Tests: `test_clipboard` (69), `test_clipboard_sync` (14, two-manager end-to-end),
  worker_smoke Test E (control API in the real runtime).
- **NOT yet:** image/GIF/file/batch capture + CF_HDROP/CF_DIB, the rich clipboard
  window (drag splitter/thumbnails/GIF), Win+V interception. See docs/clipboard.md.

### GUI / Tray
- Tray icon: double-click = open settings, right-click = menu.
  Single-click does nothing (no accidental profile activation).
- Tooltip: `FlowShift | Laptop → Surface` only when a profile is active.
  `FlowShift` when nothing is active. No "verbunden" state is ever shown.
- Profile rows: show direction only when forwarding is active; empty otherwise.
- `fwd_state` protocol message so both peers show the same direction.
- Circular forwarding prevention: while a profile is active, all other
  "Aktivieren" buttons are disabled.
- "Forwarding starten → <peer>" / "Forwarding stoppen (<peer>)".
- Status panel separates network / forwarding / capture.
- Start/Stop via control socket with timeouts; "Hängende Runtime beenden".
- No CMD popups, no UAC on normal start.

### Install / packaging
- One-click `install_flowshift.bat` → `install_flowshift.ps1` (self-elevates):
  Python check/auto-install, venv, deps (stdlib only), config+logs in
  `%ProgramData%\FlowShift`, Desktop + Start Menu shortcuts, 12 numbered steps.
- **Primary autostart = user-session Scheduled Task `FlowShift`** (AtLogOn,
  interactive, highest privileges → no per-start UAC), running
  `pythonw tray.py --tray` where hooks + SendInput actually work. Started
  immediately + control-socket verified.
- **NSSM is NOT installed by default.** `-WithNssm` adds an OPTIONAL helper
  service (manual start, session 0), explicitly NOT the input path.
- `uninstall_flowshift.bat` → `.ps1`: remove task + env vars + (optional)
  service + shortcuts + program files, optional data purge.
- `tray.py`/`gui.py` honour `FLOWSHIFT_CONFIG` + `FLOWSHIFT_LOG_DIR`.

### Runtime health / diagnostics (seventh pass)
- All workers run through `run_worker` (logs full traceback on crash, marks
  failed); `run()` uses `start_worker`. A dead `forward_loop` makes
  `forwarding_ready()` return False (input kept local, not swallowed).
- Status exposes `workers`, `critical_workers_down`, `runtime_healthy`,
  `pipeline` (queued/forwarded/send_failed/received/injected/inject_failed +
  queue sizes) and `session` (session_id, interactive, is_service_session).
- GUI shows green/red `Runtime:` health, `Session:` context (red on Session 0)
  and a live `Pipeline:` line. Live Test refuses to run in Session 0.
- `worker_smoke_test.py` starts a real runtime and verifies forward_loop/
  inject_loop alive + a fake peer actually RECEIVES forwarded input (would have
  caught the `DEFAULT_MOUSE_SETTINGS` crash).

## What needs live testing (next session)

- **Mouse movement + smoothing**: cursor moves on Surface, feels smoother, no
  jitter-in-place. Verify after pull+restart on both devices.
- **Shift / Ctrl+Shift + Arrow/Home/End** selection in Notepad/Notepad++.
- **`fwd_state`** direction label on both GUIs.
- **`remote_desktop_file_test.py`**: file created on Surface desktop.
- **Installer/uninstaller** on a fresh machine (see
  `docs/install_test_checklist.md`), incl. the session-0 service question.
- **"Status aktualisieren" / profile rows**: verify direction labels update on
  both peers after activate/deactivate.
- **Tray double-click → settings**: verify opens GUI.

## Automated test coverage (all green)

```
python -m py_compile src/python/*.py src/python/input_backends/*.py  # EXIT 0
python src/python/test_service.py   # 166 checks PASS
python src/python/test_clipboard.py # 69 checks PASS (clipboard foundation)
python src/python/test_clipboard_sync.py  # 14 checks PASS (two-manager text sync)
python src/python/worker_smoke_test.py  # Test A/B/C/D/E (workers + forwarding + switch + clipboard)
python src/python/e2e_test.py       # EXIT 0 (Windows; skips off-Win)
python src/python/reconnect_stress_test.py 30  # EXIT 0 (Win; skips off-Win)
```

152 checks cover: hotkey text helpers, peer identity, default/legacy hotkeys,
index-drift (delete/rename/insert), mouse scaling, `normalize_absolute`,
**mouse coalescing (sum, sub-pixel, clamping)**, **extended-key classification**,
protocol framing + `FramedReader`, **frame-size limit (pack/recv/FramedReader)**,
`PressTracker` cleanup, hotkey registration validity, connector reconciliation,
GUI identity mapping, capability model, input backends (Win/Linux/Unsupported),
key mapping + neutral events (incl. relative mouse move roundtrip), gating
fail-safe, version info, elevated task command builders, ping/pong shape,
`type_text` classification, `e2e_test` platform guard.

## Source files (productive Python)

| File | Role |
|---|---|
| `tray.py` | Runtime: hooks, forwarding (coalescing sender), injection, network, tray, control socket |
| `gui.py` | GUI: settings, profiles, hotkeys, status, live test |
| `runtime_model.py` | Shared logic: identity, hotkeys, framing (+size limit), mouse scaling + `MouseCoalescer` + per-profile resolve, extended keys, flying-switch planner, `PressTracker` |
| `platform_capabilities.py` | hello v1 + capabilities; tolerant parse |
| `input_backends/` | Backend abstraction (Win/Linux/Unsupported) |
| `keymap.py` | VK ↔ canonical ↔ evdev mapping |
| `input_events.py` | Win event dict ↔ platform-neutral event conversion |
| `version.py` | App/git version + `CREATE_NO_WINDOW` |
| `elevated_task.py` | Scheduled Task command builders |
| `live_network_test.py` | Live-test gating + runner |
| `poem_live_test.py` | Poem-per-cycle live test (Notepad++ append) |
| `remote_desktop_file_test.py` | Remote desktop-file creation via forwarded input (Notepad) |
| `worker_smoke_test.py` | Runtime worker health + real forwarding + flying-switch smoke test (Windows) |
| `clipboard_model.py` | Clipboard pure logic: kinds, sha256, manifest, sync diff, eviction, formatting, zip strategy, chunk planning |
| `clipboard_store.py` | Per-profile clipboard store (index.json + content-addressed objects, dedup, eviction, persistence) |
| `clipboard_protocol.py` | Clipboard sync + chunked transfer messages + `ChunkAssembler` (resume/retry/hash) |
| `clipboard_runtime.py` | ClipboardManager: capture, manifest sync, chunked transfer, per-profile stores |
| `clipboard_win.py` | Windows clipboard CF_UNICODETEXT read/set + sequence number (image/files stubbed) |
| `test_clipboard.py` | 69 clipboard foundation checks (any OS) |
| `test_clipboard_sync.py` | 14 two-manager end-to-end text sync checks (any OS) |
| `test_service.py` | 166 pure-logic checks (any OS) |
| `reconnect_stress_test.py` | Reconnect churn + process-exit (Win; skips off-Win) |
| `e2e_test.py` | Runtime handshake + input (Windows only, skips clean off-Win) |
| `config.example.json` | Template config (no real data) |

## Install / packaging files (repo root)

| File | Role |
|---|---|
| `install_flowshift.bat` / `.ps1` | One-click installer (NSSM service, venv, shortcuts) |
| `uninstall_flowshift.bat` / `.ps1` | Uninstaller |
| `requirements.txt` | Python deps (stdlib only today) |
| `docs/install_test_checklist.md` | Manual install/uninstall test checklist |

## Open / not started

- Clipboard next layers: image/GIF/file/batch capture (CF_DIB/CF_HDROP), the rich
  history window (drag splitter/thumbnails/animated GIF), Win+V interception.
  Text layer is done + tested; see `docs/clipboard.md`.
- Auto-update (Item 16): after clipboard.
- Full right-side sideboard refactor (Item 2): popups reduced/centred, drawer TBD.
- Linux input (evdev/uinput): scaffolding exists, nothing functional.
- Rust: experimental, excluded, does not compile, not worked on.
- Multi-hop forwarding (A → B → C): not designed.
- **Git history contains old real `config.json`** (device names, LAN IPs, IDs)
  and an old `start_flowshift.vbs` with a hardcoded dev path (commit `c777cff`).
  Current HEAD is clean; history rewrite needs explicit approval (see report).
