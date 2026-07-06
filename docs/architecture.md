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

The long-term goal is a full display-matrix switch with video. None of the
following is implemented yet; it is the direction, not the current state:

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
