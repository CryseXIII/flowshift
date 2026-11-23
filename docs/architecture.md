# Architecture

FlowShift is a **peer-to-peer** input-sharing tool: every device runs the same
runtime; who forwards or receives input is chosen dynamically via hotkey.

This document separates **what exists today** from the **planned target design**.

---

## Part 1 — Current implementation (productive)

The productive stack is **Python on Windows**:

```
┌──────────────────────────────────────────────────────────┐
│  src/python/tray.py (productive runtime, --tray)          │
│  discovery | peer links | hooks | injection | clipboard  │
│  control :45782 | local HTTP/WebGUI :5000                │
│                         │                                 │
│                 OverlayController                        │
└─────────────────────────┼─────────────────────────────────┘
          ▲               │ authenticated AF_PIPE JSON
          │               ▼
┌─────────┴──────┐  ┌──────────────────────────────────────┐
│ Tk GUI(legacy)│  │ overlay_host.py (separate process)   │
│ React WebGUI  │  │ pywebview + WebView2 + React shell   │
└────────────────┘  └──────────────────────────────────────┘
          ▲
          │ peer TCP :45781 / discovery UDP :45781
┌─────────┴────────────────────────────────────────────────┐
│ another device running the same tray.py runtime          │
└──────────────────────────────────────────────────────────┘
```

Shared, platform-independent logic lives in `src/python/runtime_model.py`
(peer identity, interaction-target routing, hotkey model + migration, protocol
framing, mouse scaling, pressed-key cleanup) and is imported by both `tray.py`
and `gui.py`.
The local HTTP API in `src/python/web_api.py` serves the WebGUI from `webgui/`
and exposes the canonical edge-switching layout, current edge-session state,
clipboard operations, and overlay diagnostics.

Request guard: every `/api/` request must carry the local `Host`
(`127.0.0.1` or `localhost` with the server's port; DNS rebinding). Every POST
and DELETE (all routes that change state or trigger input, actions, overlay or
updates) additionally passes `request_rejection()` before any route runs:
`Origin` must be the own WebGUI (`http://127.0.0.1|localhost` on the API port,
the WebGUI port or the Vite dev port 5173; a missing `Origin` is refused), the
body type must be `application/json` (no simple cross-site request without a
CORS preflight, which is only granted to those origins) and chunked bodies are
refused. Refusals answer `{"ok": false, "error", "error_code"}` with 403
(`host_not_allowed`, `origin_required`, `origin_not_allowed`), 415
(`json_required`) or 400, and log `web_api rejected <METHOD> <path>
reason=<code>` without query or body. The WebGUI uses no extra token; the
native Command Wheel sends its overlay origin. `test_web_api_request_guard.py`
fails when a POST/DELETE route or a GET route is added without being listed
and checked. After the guard, `_authorize_local_write` applies the body limit
of the route from `_write_request_policy` before the body is read (413
`<label> request body is too large`): 16 KiB for `actions/execute` and
`actions/wheel`, 1 KiB clipboard copy-text, 1 MiB clipboard delete, else
64 KiB.

The runtime owns one `OverlayController`, which lazily starts one hidden
`overlay_host.py` child. The child keeps `overlay.html` preloaded in WebView2 and
is reused across show/hide cycles. Runtime-to-host traffic uses authenticated
Windows Named Pipes carrying bounded JSON bytes; overlay failures do not change
core runtime health. See [overlay_architecture.md](overlay_architecture.md).

### Cross-platform scaffolding (prepared, not yet productive)

To prepare Linux↔Linux (and Windows↔Linux) sharing, three platform-neutral,
unit-tested modules exist and are already used by the runtime handshake:

- **`platform_capabilities.py`** – OS/desktop/backend detection + the protocol-v1
  `hello` with a `capabilities` block (see [protocol.md](protocol.md)).
- **`input_backends/`** – an `InputBackend` abstraction with `WindowsWin32Backend`
  (productive capture/inject still native in `tray.py`), a `LinuxStubBackend`
  (honest "not implemented", never crashes) and an `UnsupportedBackend`.
- **`keymap.py` + `input_events.py`** – Windows-VK ↔ canonical ↔ Linux-evdev
  mapping and platform-neutral event conversion.

These do **not** change the productive Windows behaviour; they make the protocol
and input handling OS-independent so a Linux backend can be added without a
protocol break. See [linux_backend_plan.md](linux_backend_plan.md).

### Components that exist
- **tray.py** – background runtime + tray icon. TCP server, outbound connector,
  UDP discovery, global keyboard/mouse hooks, `SendInput` injection, OS hotkeys,
  local control socket.
- **gui.py** – legacy tkinter app for device/peer management, forwarding
  hotkeys, profile activation, live status and runtime start/stop. Clipboard
  settings and history have moved to the React WebGUI. No new settings are
  added here; the WebGUI is the settings interface (staged retirement plan in
  `TODO_CURRENT.md`).
- **runtime_model.py** – pure logic, unit-tested on any OS.
- **overlay_controller.py** – bounded command queue, request correlation,
  supervision, restart backoff, local/remote target routing and clean shutdown.
- **overlay_host.py** – separate pywebview/WebView2 process, physical-coordinate
  placement, Per-Monitor-V2 DPI handling, show/hide and Escape handling.
- **webgui/** – React/Vite multi-entry application: `index.html` for settings,
  Clipboard management and diagnostics, and `overlay.html` for the preloaded
  functional Clipboard/Command-Wheel shell.

Phase 4 implements local Clipboard interaction and the Command Wheel, including
global Ctrl+RightClick, OS-hotkey triggers and focus-loss dismissal. Remote
overlay routing and click-through composition remain unimplemented.

### What is capture / video?
There is **no** screen capture, **no** video encoding and **no** video viewer
in the productive path today. FlowShift currently forwards **input only**.

---

## Part 2 — Target design (planned, NOT implemented)

The long-term goal is a **cross-platform** FlowShift agent (Windows↔Windows,
Linux↔Linux, and mixed) plus, eventually, a full display-matrix switch with
video. None of the video part is implemented; the cross-platform *scaffolding*
(capabilities, backends, key mapping) exists but the Linux input backend itself
is still a stub.

### Cross-platform roadmap

```
Windows-Python runtime stabilisieren.                  (ongoing)
Plattformneutrales Protokoll + Capabilities.           (prepared in this repo)
Linux evdev/uinput backend als eigener Agent.          (planned)
Optionaler Rust-Agent für Windows/Linux.               (planned)
```

The Linux backend will capture via **evdev** (`/dev/input/event*`) and inject via
**uinput** (`/dev/uinput`) — not X11/xdotool or Wayland/wtype, which may only be
optional fallbacks. It needs device permissions (udev rule / group / privileged
helper). Full plan: [linux_backend_plan.md](linux_backend_plan.md).

### Video / display-matrix (further out)

None of the following is implemented; it is the direction, not the current state:

- **Discovery** via mDNS-SD instead of UDP broadcast.
- **Screen capture** (DXGI/PipeWire/MediaProjection) + hardware H.264 encoding.
- **flowshift-viewer**: per-monitor fullscreen WebRTC/H.264 receiver.
  *(Today `src/viewer` is a stub that only prints its version.)*
- **flowshift-service** (Rust): the same responsibilities as tray.py but native.
  *(Today `src/service` is experimental and does not compile; it is excluded
  from the Cargo workspace.)*
- **Tauri 2 + React GUI**: *(does not exist in the repo.)*
- **Routing table / profiles**: per-monitor source + mode (extended/duplicated/
  input-only). The Rust types exist in `src/shared` but are unused experimentally.

### Planned routing table (design sketch, Rust types in `src/shared`)
```rust
struct RoutingEntry { display_id: String, source_id: String, source_monitor: u8, mode: StreamMode }
struct RoutingTable { entries: Vec<RoutingEntry>, input_target: String, input_keyboard_only: bool }
```

---

## Network (current)

- **Peer control + input**: TCP, port 45781 (see [protocol.md](protocol.md)).
- **Discovery**: UDP broadcast, port 45781.
- **Local control**: TCP, 127.0.0.1:45782.
- **Local WebGUI/API**: HTTP, 127.0.0.1:5000 by default.
- **Local overlay IPC**: authenticated Windows Named Pipe (`AF_PIPE`), random
  endpoint and authentication key per host lifetime; no LAN listener.

Video/audio ports and WebRTC are part of the target design only.
