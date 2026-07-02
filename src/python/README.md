# FlowShift – Python Prototype

**Zero external dependencies.** Läuft auf jedem Windows-PC mit Python 3.

## Installation

1. Python 3.10+ installieren (von [python.org](https://python.org))
2. Dieses Verzeichnis auf **jedes** Gerät kopieren
3. `config.json` anpassen (oder GUI nutzen)

## Konfiguration

### config.json
```json
{
  "device_name": "Mein-PC",
  "device_id": "a1b2c3d4",
  "port": 45781,
  "peers": [
    { "name": "PC2", "host": "192.168.1.100", "port": 45781 },
    { "name": "Tablet", "host": "192.168.1.101", "port": 45781 }
  ]
}
```

### GUI starten
```cmd
python gui.py
```
- Tab "Profile": Profile, Ping, Zustand
- Tab "Steuerung": Tray/runtime starten/stoppen + Log
- Tab "Hotkeys": Übersicht der Tastenkürzel

### Produktive Runtime direkt starten
```cmd
python tray.py --tray
```

## Hotkeys

| Hotkey | Aktion |
|---|---|
| `Ctrl + Alt + 1` | Input zu Peer 1 leiten |
| `Ctrl + Alt + 2` | Input zu Peer 2 leiten |
| `Ctrl + Alt + N` | Input zu Peer N leiten |
| `Ctrl + Alt + 0` | Zurück zu lokalem Input |

## Funktionsweise

1. **Jedes Gerät** startet `tray.py --tray`
2. Die Services verbinden sich per TCP (Port 45781)
3. Druckt der User `Ctrl+Alt+N`, werden alle Tastatur- und Mauseingaben
   an Peer N weitergeleitet und dort per `SendInput` injiziert
4. `Ctrl+Alt+0` gibt die Kontrolle zurück

### Legacy
- `service.py` ist nur noch ein Kompatibilitäts-Wrapper für ältere Skripte.

## Hinweise

- Alle Geräte müssen im selben Netzwerk sein (gleicher Router/Switch)
- Keine Portweiterleitung nötig – nur LAN
- Firewall muss TCP 45781 erlauben
- Bei WLAN: Latenz spürbar, Ethernet empfohlen
