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

### Frame size limit

A single frame may not exceed **`runtime_model.MAX_FRAME_SIZE` = 28 MiB**. This
is a hard defence against a peer announcing a huge 4-byte length and forcing
unbounded memory allocation:

- `pack_frame(msg)` raises `ValueError` if the serialised payload exceeds the cap
  (a bug/oversized message is never put on the wire).
- `recv_msg(sock)` reads the 4-byte length and raises `ValueError` **before**
  reading the body if it exceeds the cap.
- `FramedReader._try_parse()` raises `ValueError` on an oversized announced
  length instead of buffering it; `peer_handler` treats the raised error like any
  other read failure and drops the connection.

The 28 MiB cap leaves head-room for a future clipboard item limit of ~20 MiB
plus JSON/base64 overhead.

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

#### Mouse coalescing (sender side, smoothing)
The low-level mouse hook never touches the network: it only enqueues relative
delta events. A dedicated sender thread (`tray.forward_loop`) accumulates those
deltas in a `runtime_model.MouseCoalescer` and flushes an integer `(dx, dy)`
batch on a fixed interval. This turns a flood of tiny hardware moves into a small
number of network sends + `SendInput` calls (the main jitter source) while
preserving total travel; sub-pixel remainders are carried across flushes so slow
moves never vanish. Keyboard, mouse-button and wheel events are sent immediately
and in order; a pending movement is always flushed **before** such an event so a
click lands at the right position. No key/click is ever coalesced or dropped.

`TCP_NODELAY` is set on every peer socket so small frames are not delayed by
Nagle's algorithm.

Configurable via the optional `"mouse"` block in `config.json`:

```json
"mouse": {
  "flush_interval_ms": 6,
  "max_batch_ms": 12,
  "sensitivity": 1.0,
  "accumulate_subpixel": true
}
```

- `flush_interval_ms` (default 6) — how often accumulated moves are flushed
  (~166 Hz). Bounds move latency.
- `max_batch_ms` (default 12) — hard upper bound on move latency under load.
- `sensitivity` (default 1.0) — multiplier on raw hardware deltas (0.1–10.0).
- `accumulate_subpixel` (default true) — keep fractional remainders so
  scaled/slow moves are not lost.

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
stuck. This includes modifiers (Shift/Ctrl/Alt/Win), so a held modifier is never
left down on the target after Return-to-local or a disconnect.

#### Extended-key injection (Shift+Arrow selection)
Navigation keys — arrows, Home/End, Insert/Delete, PageUp/PageDown, right
Ctrl/Alt, Win, PrintScreen, numpad `/`, NumLock — MUST be injected with
`KEYEVENTF_EXTENDEDKEY`. Without it, `SendInput` maps them to numpad scan codes
and, with Shift held, Windows toggles NumLock behaviour instead of extending the
selection — which is why Shift+Arrow / Ctrl+Shift+Arrow / Shift+Home/End
selection previously failed remotely. The extended-key set lives in
`runtime_model.EXTENDED_KEY_VKS` / `is_extended_key(vk)` and is applied in
`tray.inject`. Modifiers themselves are forwarded as normal `key`/`key_up`
events (never filtered), so held-Shift + arrow arrives correctly on the target.

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

### `fwd_control` / `fwd_control_result` (flying direction switch)

To switch forwarding direction without ever having both directions active at
once, the side that wants to start forwarding asks the peer to stop first:

```json
{ "type": "fwd_control", "action": "request_deactivate",
  "requested_by": "<device_id>", "reason": "switch-direction" }
```

Reply:

```json
{ "type": "fwd_control_result", "action": "request_deactivate",
  "status": "ok|rejected|timeout|failed", "message": "..." }
```

Flow: A→B is active. On B, activating B→A detects (via the `fwd_state` it holds
for A) that A forwards to it, sends `fwd_control request_deactivate` to A, and
**waits** for the result. A stops forwarding and replies `ok`; only then does B
activate B→A. If the result is not `ok` (timeout/rejected/failed), B does **not**
activate (and never swallows input). The waiter is registered before sending so a
fast reply is never missed. During a switch the old direction releases held
keys/buttons and drains its queue (clean cutover).

### Edge switching (screen-edge auto switch)

Canonical layout stored by the WebGUI:

```json
{
  "enabled": true,
  "threshold_px": 3,
  "inset_px": 24,
  "cooldown_ms": 600,
  "return_cooldown_ms": 400,
  "edges": {
    "north": null,
    "south": null,
    "east": { "peer_identity": "device:abc123", "target_entry_edge": "west" },
    "west": null
  }
}
```

Messages:

- `edge_enter` source -> target, sent after the edge trigger and after the peer
  connection is ready.
- `edge_enter_ack` target -> source, sent after the target cursor is positioned
  and the session is accepted.
- `edge_enter_reject` target -> source, sent for invalid/busy/cursor errors.
- `edge_return` target -> source, sent when the cursor crosses the return edge.
- `edge_cancel` either direction, sent on disconnect/shutdown/manual cleanup.

Behaviour:

- The source activates forwarding only after `edge_enter_ack`.
- `target_entry_edge` comes from the layout entry and only falls back to the
  opposite edge when not configured.
- On return, the target clears its session locally and asks the source to turn
  forwarding off and warp the cursor back to the source edge.

### Clipboard messages (see [clipboard.md](clipboard.md))

The model, persistent store, runtime manager and chunked transfer path are
implemented and tested for text, HTML, file/batch, image and GIF-backed items.
Message shapes:

- `clipboard_manifest` — metadata of a profile's history (no data): `profile_id`,
  `device_id`, `history_revision`, `items[]` (item_id, sha256, kind, mime, size,
  created_at, seq, display_name, preview_text, preview_hash, file_count,
  total_file_size, available).
- `clipboard_request_items` — `profile_id`, `item_ids[]`, `include_data`,
  `reason` (auto_sync|manual_retry|paste_request).
- `clipboard_sync_result` — `received`, `skipped_existing`, `manual_required`,
  `failed`.
- `clipboard_transfer_start` — `transfer_id`, `item_id`, `sha256`, `total_size`,
  `chunk_size`, `chunk_count`, `kind`, `mime`, `file_count`, `display_name`.
- `clipboard_transfer_chunk` — `transfer_id`, `item_id`, `chunk_index`, `offset`,
  `size`, `sha256` (optional per-chunk), `data` (base64). Chunk size stays under
  `MAX_FRAME_SIZE` after base64 + envelope.
- `clipboard_transfer_ack` — `transfer_id`, `chunk_index`, `status`.
- `clipboard_transfer_complete` — `transfer_id`, `item_id`, `sha256`, `status`.
- `clipboard_transfer_error` — `transfer_id`, `item_id`, `code`
  (disk_full|hash_mismatch|too_large|not_found|timeout|aborted), `message`.
- `clipboard_transfer_resume` — `transfer_id`, `item_id`, `next_index`.

## Local overlay IPC (Windows Named Pipe)

The productive runtime owns an isolated overlay host through
`multiprocessing.connection` with the `AF_PIPE` transport. Each host lifetime
uses a random pipe name and random authentication key. This transport is local
to Windows and is not exposed on TCP, UDP or the LAN.

Messages use `send_bytes`/`recv_bytes` with UTF-8 JSON; the pickle-based
`send`/`recv` APIs are never used. A message is limited to 65,536 bytes and has
exactly this envelope:

```json
{
  "protocol": 1,
  "type": "show_overlay",
  "request_id": "request-uuid",
  "payload": {
    "mode": "clipboard",
    "target": {"kind": "local", "identity": "local"},
    "x": 120,
    "y": 240,
    "data": {}
  }
}
```

Protocol-v1 message types are `hello`, `ready`, `ping`, `pong`,
`show_overlay`, `hide_overlay`, `overlay_visible`, `overlay_hidden`,
`overlay_event`, `shutdown`, and `error`. Requests are correlated by
`request_id`; malformed, oversized, unsupported or unknown messages produce a
controlled error where possible. The only accepted modes are `clipboard` and
`command_wheel`.

Coordinates are physical Windows virtual-desktop pixels and may be negative.
Targets are either `{ "kind": "local", "identity": "local" }` or a remote peer
identity. Phase 1 renders local diagnostic overlays only; remote targets return
an explicit unsupported result rather than silently opening a local overlay.

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
| `overlay_show` | `mode`, optional `x`, `y`, `payload` | starts/reuses the local host and returns `overlay_visible`; remote interaction targets return an error in Phase 1 |
| `overlay_hide` | – | returns `overlay_hidden` and keeps the host preloaded |
| `overlay_ping` | – | starts/reuses the host and returns its health result |
| `shutdown` | – | `{ "type": "ok" }` then the runtime exits |
| `send_synthetic` | `events` | `{ "type": "ok", "queued": N }` — pushes events into the forward pipeline |
| `type_text` | `text` | `{ "type": "ok", "queued": N }` — Unicode text via remote keyboard |
| `clip_list` | `profile` | `{ "type": "ok", "items": [...], "total_size": N }` |
| `clip_capture` | `profile`, `text` | `{ "type": "ok", "item": {...} }` — manual add / capture a text |
| `clip_capture_files` | `profile`, `paths` | `{ "type": "ok", "item": {...} }` — capture a file/batch item |
| `clip_capture_image` | `profile`, `bmp_b64` | `{ "type": "ok", "item": {...} }` — capture an image (base64 BMP) |
| `clip_capture_html` | `profile`, `cf_html_b64` | `{ "type": "ok", "item": {...} }` — capture Windows CF_HTML bytes |
| `clip_thumbnail` | `profile`, `item_id`, `max_px` | `{ "type": "ok", "ppm_b64": "..." }` — image thumbnail as base64 PPM |
| `clip_preview_frames` | `profile`, `item_id`, `max_px`, `max_frames` | `{ "type": "ok", "frames": [...], "truncated": bool }` — animated GIF preview frames |
| `clip_progress` | – | `{ "type": "ok", "progress": { item_id: {received,total,percent,rate,active} } }` |
| `clip_get` | `profile`, `item_id` | `{ "type": "ok", "set": bool, "kind": "text\|html\|file\|file_batch\|image\|gif", "count"? }` — set item to the Windows clipboard (text=CF_UNICODETEXT, HTML=`HTML Format` + text fallback, files=CF_HDROP, image/GIF=CF_DIB) |
| `clip_request` | `profile`, `item_ids` | `{ "type": "ok", "requested": N }` — manual retry/download |
| `clip_pin` | `profile`, `item_id`, `pinned` | `{ "type": "ok", "pinned": bool }` |
| `clip_delete` | `profile`, `item_id` | `{ "type": "ok", "deleted": bool }` |
| `clip_clear` | `profile` | `{ "type": "ok" }` |
| `clip_sync` | `profile` | `{ "type": "ok" }` — resend our manifest to the peer |

The status snapshot includes `running`, `shutting_down`, `active`,
`active_peer`, `active_peer_identity`, `hook_running`, `network_connected`,
`forwarding_active`, `capture_active`, `interaction_target`, `overlay`
(`enabled`, `process_alive`, `ipc_connected`, `ready`, `mode`, `visible`,
`restart_count`, `last_error`), connection labels, and the peer list
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
