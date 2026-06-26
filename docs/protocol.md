# FlowShift Protocol

## Discovery (mDNS-SD)

Jeder Service registriert sich als `_flowshift._tcp.local`. TXT-Records:

| Key | Value |
|---|---|
| `name` | Benutzerfreundlicher Gerätename |
| `os` | `windows`, `linux`, `android` |
| `caps` | Bitfield: `1=video`, `2=input`, `4=audio` |
| `ver` | Protokollversion (aktuell `1`) |

## Control TCP (Port 45781)

Nach Verbindungsaufbau: **Frame-Length-Encoding** (4 Bytes Big-Endian Länge, dann Payload).

### Message Types

Alle Messages sind JSON (UTF-8).

#### `Hello`
```json
{
  "type": "hello",
  "device_id": "uuid-v4",
  "display_name": "Viktor-Tablet"
}
```

#### `RoutingUpdate`
```json
{
  "type": "routing_update",
  "sender": "uuid",
  "table": {
    "entries": [
      {
        "display_id": "monitor-uuid-1",
        "source_id": "tablet-uuid",
        "source_monitor": 0,
        "mode": "extended"
      }
    ],
    "input_target": "tablet-uuid",
    "input_keyboard_only": false
  }
}
```

#### `InputEvent`
```json
{
  "type": "input_event",
  "sender": "uuid",
  "events": [
    { "kind": "mouse_move", "x": 1920, "y": 540 },
    { "kind": "key_down", "code": 42, "modifiers": 0 }
  ]
}
```

#### `StreamAnnounce`
```json
{
  "type": "stream_announce",
  "sender": "uuid",
  "monitor": 0,
  "sdp": "webrtc-sdp-offer",
  "ice_candidates": ["candidate:..."]
}
```

## Video Streaming (WebRTC)

Standard WebRTC mit:

- Codec: H.264 (constrained baseline, für Low-Latency)
- Encoder: NVENC/AMF/QSV hardwarebeschleunigt
- Bitrate: dynamisch 10-100 Mbps (lokal konstant hoch)
- Auflösung: native Monitorauflösung
- Framerate: 60 fps oder Monitor-Refresh
- B-Frames: deaktiviert (minimiert Latenz)

## Input Forwarding

TCP-Verbindung vom Controller zum Target. Events werden sofort serialisiert und gesendet:

```rust
enum InputKind {
    MouseMove { x: i32, y: i32 },
    MouseButton { button: u8, down: bool },
    MouseWheel { delta: i32, horizontal: bool },
    KeyDown { code: u16, modifiers: u8 },
    KeyUp { code: u16, modifiers: u8 },
}
```

Auf dem Target werden Events via plattformspezifische API injiziert:
- **Windows**: `SendInput`
- **Linux**: `uinput` (virtuelles Eingabegerät)
- **Android**: `AccessibilityService` / `InputManager`

## Audio Streaming (UDP)

Opus-Codec, 48 kHz, mono, variable Bitrate (64-192 kbps). Pakete einzeln via UDP, kein Backchannel.
