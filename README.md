# FlowShift

**Software-KVM: steuere mehrere Geräte per Hotkey mit einem Satz Maus/Tastatur.**

FlowShift leitet Tastatur- und Mauseingaben per Hotkey an ein anderes Gerät im
lokalen Netz weiter und injiziert sie dort. Der **produktive Teil ist heute ein
Python-Prototyp** (Windows). Die weitergehende Video-/Monitor-Matrix ist ein
**geplantes Ziel**, aktuell noch nicht implementiert.

## Was heute wirklich funktioniert (Python-Prototyp)

- Hintergrund-Runtime + Tray-Icon: `src/python/tray.py --tray`
- Einstellungs-GUI: `src/python/gui.py`
- Peer-Discovery im LAN (UDP-Broadcast), Verbindung per TCP (Port 45781)
- Hotkey-gesteuertes Weiterleiten von Tastatur + Maus (`SendInput`) auf Windows
- Stabile Peer-Identität (`device_id`, sonst Endpoint) und Hotkey-Bindung ohne
  Listenindex-Drift
- Lokaler Control-Socket (`127.0.0.1:45782`) für GUI ↔ Runtime

Details: [src/python/README.md](src/python/README.md).

## Plattformen

- **Produktiv heute:** Windows↔Windows (Python-Pfad).
- **Zielarchitektur:** plattformübergreifend – Linux↔Linux, Windows↔Linux,
  Linux↔Windows. Dafür ist bereits **vorbereitet** (aber noch nicht produktiv):
  ein plattformneutrales Protokoll mit Capability-Handshake
  (`platform_capabilities.py`), eine Input-Backend-Abstraktion
  (`input_backends/`) und ein Win-VK ↔ canonical ↔ Linux-evdev Key-Mapping
  (`keymap.py`, `input_events.py`).
- **Linux-Input** soll über **evdev/uinput** laufen (nicht primär X11/Wayland).
  Das Linux-Backend ist derzeit ein ehrlicher **Stub** und braucht später
  Geräte-Berechtigungen. Es gilt erst als funktionierend, wenn evdev/uinput
  wirklich implementiert und manuell getestet sind. Plan:
  [docs/linux_backend_plan.md](docs/linux_backend_plan.md).

## Konzept / Zielbild (noch NICHT implementiert)

Langfristig soll FlowShift eine komplette Monitor-Matrix per Profil umschalten
(welcher Monitor zeigt welches Gerät, Extended/Duplicated, Video-Stream). Das
ist das Zielbild, nicht der aktuelle Funktionsstand:

```
Hotkey "Tablet Focus"
┌─────────┬─────────────┬──────────────┐
│ Monitor │ Zeigt Gerät │ Modus        │
├─────────┼─────────────┼──────────────┤
│ PC-Mon1 │ Tablet (VM1)│ Extended     │
│ Tablet  │ Tablet      │ Normal       │
├─────────┴─────────────┴──────────────┤
│ Input geht an: Tablet                │
└──────────────────────────────────────┘
```

## Architektur

| Komponente | Technologie | Status |
|---|---|---|
| `src/python/tray.py` | Python (Windows API via ctypes) | **produktiv** – Runtime + Tray |
| `src/python/gui.py` | Python (tkinter) | **produktiv** – Einstellungen/Profile |
| `src/python/runtime_model.py` | Python (rein, plattformunabhängig) | **produktiv** – geteilte Logik |
| `flowshift-shared` | Rust (lib) | experimentell – gemeinsame Typen |
| `flowshift-service` | Rust (daemon) | **experimentell, baut derzeit nicht** |
| `flowshift-viewer` | Rust (renderer) | **Stub** – nur Platzhalter, kein Video |
| Tauri/React-GUI | – | **nicht vorhanden** (nur konzeptionell) |

> Hinweis: Es gibt aktuell **keinen** funktionierenden Video-/Fullscreen-Empfänger
> und **keine** Tauri/React-GUI im Repo. `flowshift-viewer` ist ein Stub;
> `flowshift-service` ist experimenteller Code, der derzeit nicht kompiliert und
> daher aus dem Cargo-Workspace ausgeschlossen ist. Siehe
> [FLOWSHIFT_AUDIT_AND_FIX_REPORT.md](FLOWSHIFT_AUDIT_AND_FIX_REPORT.md).

Siehe [docs/architecture.md](docs/architecture.md) und [docs/protocol.md](docs/protocol.md).

## Lizenz

MIT

