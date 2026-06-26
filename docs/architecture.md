# Architecture

FlowShift ist ein **peer-to-peer System** – jedes Gerät installiert den gleichen Stack. Es gibt keinen festen Master; wer gerade Input liefert oder empfängt, wird dynamisch per Hotkey bestimmt.

## Überblick

```
┌──────────────────────────────────────────────────┐
│                flowshift-service                  │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌────────────────┐ │
│  │ Discovery│  │ Capabil- │  │  Routing Table  │ │
│  │ (mDNS)   │  │ ities    │  │  (active set)   │ │
│  └────┬─────┘  └────┬─────┘  └───────┬────────┘ │
│       │             │                │           │
│  ┌────┴─────┐  ┌────┴─────┐  ┌───────┴────────┐ │
│  │ Network  │  │ Screen   │  │ Input Router   │ │
│  │ (TCP/UDP)│  │ Capture  │  │ (global hooks) │ │
│  └──────────┘  └──────────┘  └────────────────┘ │
│       │             │                │           │
│       ▼             ▼                ▼           │
│  ┌─────────────────────────────────────────────┐ │
│  │            Protocol Layer                    │ │
│  │  StreamAnnounce | InputEvent | AudioPacket  │ │
│  └─────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────┐
│              flowshift-viewer                     │
│  Fullscreen DX11/Vulkan Renderer per Monitor     │
│  Empfängt H.264 Stream → dekodiert → rendert    │
└──────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────┐
│              flowshift-gui (Tauri 2)              │
│  Profil-Manager | Hotkey-Konfig | Live-Status    │
└──────────────────────────────────────────────────┘
```

## Komponenten im Detail

### flowshift-service

Systemweiter Hintergrunddienst (systemd/Linux, Service/Win). Läuft auf **jedem Gerät** im Netzwerk. Aufgaben:

- **Discovery**: mDNS-SD (Multicast DNS Service Discovery) zum Auffinden aller FlowShift-Geräte im lokalen Netz
- **Capability Exchange**: Jedes Gerät teilt mit: Display-Name, Auflösungen, unterstützte Codecs, Input-Fähigkeiten
- **Screen Capture**: DXGI Desktop Duplication (Win), PipeWire (Linux), MediaProjection (Android)
- **Hardware Encoding**: NVENC/AMF/QSV → H.264 + optional H.265/AV1
- **Input Router**: Global Hotkey Hooks + Capture Region. Empfängt Input-Events von entfernten Geräten und injiziert sie via SendInput (Win) / uinput (Linux)
- **Routing Table**: Thread-safe, atomar austauschbar. Legt fest: pro physischem Monitor → welche Quelle → welcher Modus

### flowshift-viewer

Pro physischem Monitor wird ein Viewer-Prozess gestartet. Empfängt via WebRTC einen H.264-Videostream und rendert ihn fullscreen. Steuert sich via IPC mit dem Service.

- DirectX 11 (Win) / Vulkan (Linux) für Low-Latency Rendering
- G-Sync/FreeSync-kompatibel
- Optional: Touch-Input Rücksendung (Tablet → PC)

### flowshift-gui

Tauri-2-Desktop-App. Wrapper um den Service. Bietet:

- Geräteliste (Discovery-Ergebnisse)
- Profil-Editor: Hotkey + Monitor-Routing + Input-Target + Modus
- Live-Vorschau: Welcher Stream läuft wo?
- Pro Export/Import (JSON)

## Routing Table

Die zentrale Datenstruktur:

```rust
struct RoutingEntry {
    // Welcher physische Monitor auf diesem Gerät?
    display_id: DisplayId,
    // Welches entfernte Gerät streamt hierher?
    source_id: DeviceId,
    // Nummer des virtuellen Monitors auf dem Quellgerät
    source_monitor: u8,
    // extended = Desktop erweitern, duplicated = spiegeln, input_only = kein Video
    mode: StreamMode,
}

struct RoutingTable {
    entries: Vec<RoutingEntry>,
    // Wohin geht aktuell der Input?
    input_target: DeviceId,
    // Soll nur Tastatur weitergeleitet werden?
    input_keyboard_only: bool,
}
```

## Netzwerk

- **Discovery**: mDNS-SD, Port 5353
- **Control (Routing, Config)**: TCP, Port 45781
- **Video Streams**: WebRTC (ICE/STUN für NAT), UDP mit adaptiver Bitrate
- **Input Events**: TCP (zuverlässig, sequentiell)
- **Audio**: UDP, Opus-Codec

## Sicherheit

- mDNS nur im lokalen Subnetz
- Keine offenen Ports nach aussen (ausser WebRTC ICE)
- TLS für Control-Verbindungen (self-signed, vertrauen via Fingerprint beim ersten Pairing)
