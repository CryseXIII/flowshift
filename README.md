# FlowShift

**Software-Display-Matrix-Switch — steuere mehrere Geräte mit einem Satz Maus/Tastatur, ohne Kabel umzustecken.**

FlowShift ist ein System aus Hintergrunddienst + GUI, das dir erlaubt, per Hotkey den gesamten "Context" umzuschalten: welches Gerät Input bekommt, welche physischen Monitore welche virtuellen Desktops zeigen, und ob Extended oder Duplicated Mode.

## Konzept

Statt physikalische HDMI-Kabel umzustecken, definierst du **Hotkey-Profile**. Ein Profil enthält eine vollständige Routing-Tabelle:

```
Hotkey "Tablet Focus"
┌─────────┬─────────────┬──────────────┐
│ Monitor │ Zeigt Gerät │ Modus        │
├─────────┼─────────────┼──────────────┤
│ PC-Mon1 │ Tablet (VM1)│ Extended     │
│ PC-Mon2 │ Tablet (VM2)│ Extended     │
│ Tablet  │ Tablet      │ Normal       │
├─────────┴─────────────┴──────────────┤
│ Input geht an: Tablet                │
└──────────────────────────────────────┘

Hotkey "PC Focus"
┌─────────┬─────────────┬──────────────┐
│ PC-Mon1 │ PC          │ Normal       │
│ PC-Mon2 │ PC          │ Normal       │
│ Tablet  │ PC (stream) │ Duplicated   │
├─────────┴─────────────┴──────────────┤
│ Input geht an: PC                    │
└──────────────────────────────────────┘
```

Mehrere Profile parallel möglich: z.B. Monitor1 = Tablet, Monitor2 = Laptop.

## Quick Start

*(folgt mit erstem Release)*

## Architektur

| Komponente | Technologie | Zweck |
|---|---|---|
| `flowshift-shared` | Rust (lib) | Gemeinsame Typen & Protokoll |
| `flowshift-service` | Rust (daemon) | Läuft auf jedem Gerät: Discovery, Capture, Input, Routing |
| `flowshift-gui` | Tauri 2 + React | Desktop-Einstellungen & Profil-Manager |
| `flowshift-viewer` | Rust (renderer) | Fullscreen-Video-Empfänger pro Monitor |
| `flowshift-android` | Kotlin (app) | Companion-App für Android (future) |

Siehe [docs/architecture.md](docs/architecture.md).

## Lizenz

MIT
