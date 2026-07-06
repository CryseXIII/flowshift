# Architecture

FlowShift is a **peer-to-peer** input-sharing tool: every device runs the same
runtime; who forwards or receives input is chosen dynamically via hotkey.

This document separates **what exists today** from the **planned target design**.

---

## Part 1 — Current implementation (productive)

The productive stack is **Python on Windows**:

```
┌───────────────────────────────────────────────┐
│  src/python/tray.py   (runtime, --tray)        │
│                                                │
│  Discovery (UDP)   TCP server/connector        │
│  Low-level hooks   Input router (hotkeys)      │
│  Inject (SendInput)  Control socket :45782     │
│                                                │
│  Wire protocol: 4-byte length + JSON           │
│  hello | ping/pong | input | discover          │
└───────────────────────────────────────────────┘
        ▲                         ▲
        │ control :45782          │ peer TCP :45781
        │                         │
┌───────────────┐        ┌────────────────────────┐
│ src/python/   │        │ another device running │
│ gui.py (tk)   │        │ the same tray.py        │
└───────────────┘        └────────────────────────┘
```

Shared, platform-independent logic lives in `src/python/runtime_model.py`
(peer identity, hotkey model + migration, protocol framing, mouse scaling,
pressed-key cleanup) and is imported by both `tray.py` and `gui.py`.

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
- **gui.py** – tkinter settings app: device/peer management, hotkey editor,
  profile activation, live status via the control socket, service start/stop.
- **runtime_model.py** – pure logic, unit-tested on any OS.

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
Phase 1: Windows-Python stabilisieren.                 (done / ongoing)
Phase 2: Plattformneutrales Protokoll + Capabilities.  (prepared in this repo)
Phase 3: Linux evdev/uinput backend als eigener Agent. (planned)
Phase 4: Optionaler Rust-Agent für Windows/Linux.      (planned)
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

Video/audio ports and WebRTC are part of the target design only.
