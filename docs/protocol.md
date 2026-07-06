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

### `hello` (both directions on connect)
```json
{
  "type": "hello",
  "device_id": "a1b2c3d4",
  "display_name": "Laptop-Viktor",
  "os": "windows",
  "screen": { "left": 0, "top": 0, "width": 1920, "height": 1080 }
}
```
Both sides send a `hello` and read the peer's `hello`. A connection whose
`device_id` equals the local one is rejected (self-connection guard).

### `ping` / `pong` (one-shot reachability check)
A `ping` client connects, sends `ping`, receives `pong`, and the socket is closed.
```json
{ "type": "ping", "device_id": "a1b2c3d4", "display_name": "Laptop-Viktor",
  "os": "windows", "screen": { ... } }
```
```json
{ "type": "pong", "device_id": "b5c6d7e8", "display_name": "Surface-Viktor",
  "os": "windows", "screen": { ... } }
```

### `input` (event batch, source → target)
```json
{
  "type": "input",
  "events": [
    { "type": "mousemove", "x": 500, "y": 300,
      "source_screen": { "left": 0, "top": 0, "width": 1920, "height": 1080 } },
    { "type": "mousedown", "button": 0 },
    { "type": "mouseup",   "button": 0 },
    { "type": "wheel",     "delta": 120 },
    { "type": "key",       "code": 65 },
    { "type": "key_up",    "code": 65 }
  ]
}
```

#### Event types
| `type`      | Fields                    | Meaning                          |
|-------------|---------------------------|----------------------------------|
| `mousemove` | `x`, `y`                  | absolute source-screen position  |
| `mousedown` | `button` (0=L,1=R,2=M)    | button pressed                   |
| `mouseup`   | `button`                  | button released                  |
| `wheel`     | `delta` (±120 per notch)  | vertical wheel                   |
| `key`       | `code` (Windows VK)       | key down                         |
| `key_up`    | `code`                    | key up                           |

#### Screen scaling
- The source attaches `source_screen` (its virtual desktop rect).
- The target attaches `target_screen` (its own virtual desktop rect) on receive.
- `mousemove` is scaled from source to target rect and clamped into the target,
  then normalised to the `0..65535` absolute `SendInput` range
  (`runtime_model.scale_mouse_point` + `normalize_absolute`).
- Injected events carry a marker (`dwExtraInfo`) so the local hook ignores them.

#### Pressed-state safety
The source tracks keys/buttons it forwarded; on stop / disconnect / shutdown it
sends synthetic `key_up` / `mouseup` for anything still held. The target tracks
what it injected and releases it if the peer vanishes. So no key/button stays
stuck.

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

The status snapshot includes `running`, `shutting_down`, `active`,
`active_peer`, `active_peer_identity`, `hook_running`, connection labels, the
peer list (with per-peer `identity`, `connected_in`/`connected_out`) and the
hotkey list (with `action`, `display`, `valid`).

---

## Experimental Rust protocol (NOT productive)

`src/shared/src/protocol.rs` defines a separate, richer enum
(`Message::{Hello, RoutingUpdate, InputEvent, StreamAnnounce}` and
`InputEventKind::{MouseMove, MouseButton, MouseWheel, KeyDown, KeyUp}`). It is
**not wire-compatible** with the Python protocol above and is not used in
production. Unifying the two is future work; until then the Python protocol in
this document is the single source of truth.
