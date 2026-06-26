# Hotkey Configuration

## Profil-Struktur

Ein Profil definiert einen vollständigen Context-Switch. Es wird in der GUI per Mausklick zusammengeklickt und als JSON gespeichert.

```json
{
  "name": "Tablet Focus",
  "hotkey": {
    "modifiers": ["Ctrl", "Alt"],
    "key": "1"
  },
  "input": {
    "target": "tablet-uuid",
    "keyboard_only": false
  },
  "display_routing": [
    {
      "display": "monitor-pc-left",
      "source": "tablet-uuid",
      "source_monitor": 0,
      "mode": "extended"
    },
    {
      "display": "monitor-pc-right",
      "source": "tablet-uuid",
      "source_monitor": 1,
      "mode": "extended"
    }
  ],
  "audio": {
    "source": "tablet-uuid",
    "enabled": false
  }
}
```

## Konfigurierbare Hotkeys

| Modulation | Beschreibung |
|---|---|
| `Ctrl+Alt+1..9` | Profil 1-9 direkt anwählen |
| `Ctrl+Alt+0` | Zurück zum "Normal"-Profil (lokaler Desktop) |
| `Win+~` | Temporär Input-only (letztes Profil, aber kein Display-Routing) |
| `Ctrl+Shift+Esc` | Capture Region verlassen (Maus entsperren) |

Alle Hotkeys sind in der GUI frei konfigurierbar.

## Capture Region

Wenn ein Profil aktiv ist und die Maus auf ein Zielgerät routet:
- Optional: Maus in einer definierten Bildschirmregion "einsperren"
- Escape-Taste löst die Sperre
- Visualisierung: dünner farbiger Rahmen um die aktive Region

## Profile Storage

```jsonc
// ~/.config/flowshift/profiles.json
{
  "profiles": [
    { ... profile 1 ... },
    { ... profile 2 ... }
  ],
  "default_profile": "tablet-uuid"
}
```
