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

