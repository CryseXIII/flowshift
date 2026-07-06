# FlowShift Protocol

This document describes the **protocol that the productive Python runtime
(`src/python/tray.py`) actually speaks today**. The Rust `flowshift-shared`
protocol enum is a *different, experimental* design and is NOT what runs in
production (see the note at the bottom).

## Transport

- **Control / input link**: TCP, default port **45781**.
- **Discovery**: UDP broadcast on the same port (45781).
- **Local control** (GUI ↔ local runtime): TCP on **127.0.0.1:45782**.

## Framing

Every message on the TCP links is length-prefixed JSON:

```
[ 4 bytes big-endian unsigned length N ][ N bytes UTF-8 JSON payload ]
```

The reader is timeout-tolerant: partial frames are buffered and never desync the
stream (`runtime_model.FramedReader`).

## Peer messages (TCP 45781)

### `hello` (both directions on connect) — protocol v1 + capabilities

```json
{
  "type": "hello",
  "protocol_version": 1,
  "device_id": "a1b2c3d4",
  "display_name": "Laptop-Viktor",
  "os": "windows",
  "desktop": "win32",
  "input_backend": "win32",
  "port": 45781,
  "screen": { "x": 0, "y": 0, "left": 0, "top": 0, "width": 1920, "height": 1080 },
  "capabilities": {
    "keyboard_capture": true,
    "mouse_capture": true,
    "keyboard_inject": true,
    "mouse_inject": true,
    "screen_info": true,
    "requires_privileged_helper": false,
    "requires_uinput": false,
    "requires_evdev": false
  }
}
```

Both sides send a `hello` and read the peer's `hello`. A connection whose
`device_id` equals the local one is rejected (self-connection guard).

Fields:
- `os`: `windows` | `linux` | `macos` | `unknown`.
- `desktop`: `win32` | `x11` | `wayland` | `unknown`.
- `input_backend`: `win32` | `evdev_uinput` | `unsupported`.
- `capabilities`: what the peer can actually do and what it still requires.
- `screen`: carries **both** `x/y` (canonical) and `left/top` (kept for the
  existing mouse-scaling code) plus `width/height`.

**Backward compatibility:** old peers send a minimal `hello` (just
`device_id`/`display_name`/`os`/`screen`). `platform_capabilities.parse_hello`
fills conservative defaults (a Windows peer without a `capabilities` block is
assumed input-capable; an unknown-OS peer is assumed to be able to do nothing),
so old peers are handled tolerantly and never crash the runtime.

### `ping` / `pong` (one-shot reachability check)
A `ping` client connects, sends `ping` (same v1 shape as `hello`), receives
`pong` (same shape), and the socket is closed.

### `input` (event batch, source → target)
```json
{
  "type": "input",
  "events": [
    { "type": "mousemove", "dx": 12, "dy": -3, "mode": "relative" },
    { "type": "mousedown", "button": 0 },
    { "type": "mouseup",   "button": 0 },
    { "type": "wheel",     "delta": 120 },
    { "type": "key",       "code": 65 },
    { "type": "key_up",    "code": 65 }
  ]
}
```

#### Event types
| `type`      | Fields                              | Meaning                              |
|-------------|-------------------------------------|--------------------------------------|
| `mousemove` | `dx`, `dy`, `mode:"relative"`       | relative hardware delta (default)    |
| `mousemove` | `x`, `y`, `source_screen`, `mode:"absolute"` | absolute src-screen position (GUI synthetic only) |
| `mousedown` | `button` (0=L,1=R,2=M)             | button pressed                       |
| `mouseup`   | `button`                            | button released                      |
| `wheel`     | `delta` (±120 per notch)           | vertical wheel                       |
| `key`       | `code` (Windows VK)                | key down                             |
| `key_up`    | `code`                             | key up                               |

#### Mouse movement — relative delta (commit e137af8)
The source cursor is **frozen** at the activation anchor position. The hook reads
`ms.pt` (intended new position = anchor + hardware_delta) and forwards
`dx = ms.pt.x − anchor.x`, `dy = ms.pt.y − anchor.y`. The anchor stays fixed
(never updated), so every event carries the correct physical hardware delta
regardless of how many events have been suppressed.

The target injects with `MOUSEEVENTF_MOVE` only (no `MOUSEEVENTF_ABSOLUTE`),
which moves the target cursor by the raw delta.

Absolute `mousemove` (with `x`,`y`,`source_screen`) is still accepted for
synthetic/control events sent via the GUI Live Test tab.

#### Screen scaling (absolute mode only)
- The source attaches `source_screen` (its virtual desktop rect).
- The target attaches `target_screen` (its own virtual desktop rect) on receive.
- `mousemove` absolute is scaled from source to target rect and clamped, then
  normalised to the `0..65535` absolute `SendInput` range
  (`runtime_model.scale_mouse_point` + `normalize_absolute`).
- Relative moves bypass scaling entirely.

#### Pressed-state safety
The source tracks keys/buttons it forwarded; on stop / disconnect / shutdown it
sends synthetic `key_up` / `mouseup` for anything still held. The target tracks
what it injected and releases it if the peer vanishes. So no key/button stays
stuck.

### `fwd_state` (forwarding direction notification, source → target)

Sent by the machine that activates or deactivates forwarding so the **peer's
GUI** can display the correct direction label without polling.

```json
{ "type": "fwd_state", "active": true, "source_name": "Laptop-Viktor" }
```

- `active: true` → this machine is now forwarding input **to you**.
- `active: false` → forwarding stopped.
- `source_name` → display name of the forwarder (shown in the peer's profile row).

The peer stores `remote_forwarding_active` + `remote_forwarding_source` in its
link state and includes them in the status snapshot. The GUI shows e.g.
`Laptop-Viktor → Surface-Viktor` (Ziel) on Surface when Laptop is the source.

A `fwd_state` is also sent on clean deactivation, on service shutdown, and
implicitly (link cleared) when the TCP connection drops.

## Platform-neutral event model (cross-platform target)

The current productive wire above carries **Windows virtual-key codes** in
`code`. To prepare a cross-platform agent (Linux `evdev`/`uinput`), FlowShift
defines a platform-neutral event shape and a mapping layer so the protocol is
not bound to Windows codes:

```json
{
  "kind": "key_down|key_up|mouse_move|mouse_down|mouse_up|wheel",
  "code": "KeyA",
  "native_code": 65,
  "os": "windows",
  "button": "left|right|middle|x1|x2",
  "mode": "absolute|relative",
  "x": 100, "y": 200,
  "dx": 0, "dy": 0,
  "delta": 120,
  "source_screen": { "x": 0, "y": 0, "width": 1920, "height": 1080 },
  "modifiers": { "ctrl": false, "alt": false, "shift": false, "meta": false }
}
```

- `code` is a canonical key name (W3C `KeyboardEvent.code` style: `KeyA`,
  `Digit1`, `ArrowLeft`, `ControlLeft` …).
- `native_code` is the original OS code (Windows VK **or** Linux evdev code).
- **Injection rule:** if the receiver's OS equals the event's `os` and a
  `native_code` is present, use it directly (loss-less same-OS path); otherwise
  map via the canonical `code`. So Windows↔Windows stays loss-less and a future
  Linux backend plugs in without a protocol break.
- `mouse_move` distinguishes **absolute** (`x`,`y`) and **relative** (`dx`,`dy`).

### Mapping layer

`src/python/keymap.py` holds the tables:

```
Windows VK   <->  canonical key name  <->  Linux evdev code
```

with `win_vk_to_canonical` / `canonical_to_win_vk` /
`evdev_to_canonical` / `canonical_to_evdev` and the OS-generic
`from_native(code, os)` / `to_native(name, os)`. `src/python/input_events.py`
converts between the current Windows event dicts and the neutral shape
(`win_event_to_neutral` / `neutral_to_win_event`).

> Status: the mapping + neutral model are **implemented and unit-tested**; the
> productive Windows wire still sends Windows VK events. Migrating the wire to
> the neutral shape (and adding the Linux backend) is Phase 2/3 — see
> [linux_backend_plan.md](linux_backend_plan.md).

## Discovery (UDP broadcast 45781)

Request (broadcast):
```json
{ "type": "discover", "device_id": "a1b2c3d4", "display_name": "Laptop-Viktor", "port": 45781 }
```
Reply (unicast back to sender):
```json
{ "type": "discover_reply", "device_id": "b5c6d7e8", "display_name": "Surface-Viktor",
  "port": 45781, "screen": { ... } }
```

## Local control (TCP 127.0.0.1:45782)

Same framing. Commands (GUI → runtime):

| Request `type` | Extra fields | Response |
|---|---|---|
| `status` | – | `{ "type": "status", "status": { ...snapshot... } }` |
| `activate` | `profile` | `{ "type": "ok", "status": {...} }` or `{ "type": "error", "error": "..." }` |
| `deactivate` | – | `{ "type": "ok", "status": {...} }` |
| `toggle` | `profile` | `{ "type": "ok", "status": {...} }` |
| `ping_peer` | `profile` | `{ "type": "ok", "ping": {...} }` |
| `shutdown` | – | `{ "type": "ok" }` then the runtime exits |
| `send_synthetic` | `events` | `{ "type": "ok", "queued": N }` — pushes events into the forward pipeline |
| `type_text` | `text` | `{ "type": "ok", "queued": N }` — Unicode text via remote keyboard |

The status snapshot includes `running`, `shutting_down`, `active`,
`active_peer`, `active_peer_identity`, `hook_running`, `network_connected`,
`forwarding_active`, `capture_active`, connection labels, and the peer list
(with per-peer `identity`, `connected_in`/`connected_out`, `link_label`,
`direction`, `remote_forwarding_active`, `remote_forwarding_source`).

---

## Experimental Rust protocol (NOT productive)

`src/shared/src/protocol.rs` defines a separate, richer enum
(`Message::{Hello, RoutingUpdate, InputEvent, StreamAnnounce}` and
`InputEventKind::{MouseMove, MouseButton, MouseWheel, KeyDown, KeyUp}`). It is
**not wire-compatible** with the Python protocol above and is not used in
production. Unifying the two is future work; until then the Python protocol in
this document is the single source of truth.
