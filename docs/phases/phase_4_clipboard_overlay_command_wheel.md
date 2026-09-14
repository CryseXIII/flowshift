# Phase 4 – Clipboard Overlay und Command Wheel

## 1. Ziel

Phase 4 macht das Clipboard und den Dateitransfer visuell und technisch
benutzbar und liefert ein funktionales Command Wheel. Beide erscheinen als
lokales React-Overlay (pywebview/WebView2) an der Mausposition.

Zielrelease: `v0.7.0`

Produktiver Runtime-Einstieg bleibt `src/python/tray.py --tray`.

Phase 4 gilt erst als abgeschlossen, wenn `v0.7.0` veröffentlicht wurde, der
tag-triggered GitHub-Actions-Workflow erfolgreich ist und alle Release-Assets
verifiziert wurden.

## 2. Nicht Bestandteil dieser Phase

- Linux-Backend, Rust-Runtime, Video/Display-Matrix.
- Remote-Overlay-Routing (Overlay auf dem Peer-Bildschirm).
- Änderungen am Transfer-Transport V2 oder am Legacy-Transfer, sofern nicht
  direkt für die UI-Anbindung nötig.
- Per-Pixel-Transparenz oder Click-through-Komposition des Hosts.
- Mausverfolgung des Overlays.

## 3. Verhaltensregeln (verbindlich)

- Overlay-Fenster spawnen an der Cursorposition und bleiben dort statisch.
- Ein Klick außerhalb des Overlays (Fokusverlust) oder `Escape` schließt es.
- Command Wheel und Clipboard-Overlay sind funktionale Komponenten, keine
  Diagnoseansichten. Die Diagnosedarstellung bleibt nur erreichbar, wenn das
  Show-Payload `diagnostic: true` enthält (WebGUI-Diagnosepanel).
- Keine Overlay- oder Clipboard-Aktion darf den Runtime-Hauptthread
  (Window-Thread, Hook-Callbacks) oder den React-UI-Thread blockieren.
- React führt keine OS-Aktionen direkt aus. Jede Aktion läuft über die
  Web-API und die validierte Action Registry der Runtime.
- Normale API-Antworten enthalten keine privaten absoluten Pfade.

## 4. Action Registry

Modul `src/python/overlay_actions.py` (pur, testbar):

- Feste, versionierte Menge eingebauter Aktionen mit `id`, `label`, `icon`,
  `kind` und Ausführungsparametern.
- Erste Aktionen:
  - `copy` (Ctrl+C), `paste` (Ctrl+V), `cut` (Ctrl+X), `delete` (Entf),
    `select_all` (Ctrl+A), `undo` (Ctrl+Z), `redo` (Ctrl+Y) als lokale
    Tastatur-Injektion in das zuvor fokussierte Fenster;
  - `open_clipboard` (Clipboard-Overlay öffnen);
  - `clipboard_sync` (Manifest an das aktive Profil senden).
- Unbekannte IDs, falsche Typen und übergroße Konfigurationen werden
  abgelehnt; nie stillschweigend korrigiert, außer dokumentiert.
- Wheel-Konfiguration `config["command_wheel"]`:
  - `pages`: Liste von Seiten, jede Seite bis zu 8 Aktions-IDs;
  - maximal 16 Seiten; leere Seiten werden entfernt; Default-Belegung, wenn
    nichts konfiguriert ist.
  - `hotkey`: `{ "mods": int, "vk": int }`, Default `Ctrl+Alt+Space`.

Ausführung in der Runtime (`tray.py`):

1. vor jedem Overlay-Show das aktuelle Vordergrundfenster merken;
2. beim Ausführen einer Tastatur-Aktion: Overlay verstecken, Vordergrund
   wiederherstellen, Tastenfolge in `istate.inject_queue` legen;
3. Ergebnis strukturiert zurückgeben (`ok`, `action_id`, `reason`).

## 5. Web-API

- `GET /api/actions` → `{ actions: [...], wheel: { pages, hotkey } }`.
- `POST /api/actions/wheel` → validiert und speichert `pages` (und optional
  `hotkey`) in der Konfiguration; Fehler 400 mit Grund.
- `POST /api/actions/execute` → `{ action_id, context }`; nur registrierte
  Aktionen; Antwort `{ ok, action_id, reason }`.
- Bestehende Clipboard-Routen bleiben unverändert und tragen das
  Clipboard-Overlay.

## 6. Overlay-Host

- Fenstergröße pro Modus: `command_wheel` quadratisch, um den Cursor
  zentriert; `clipboard` hochkant, an den Cursor angelehnt; beide in den
  Arbeitsbereich des Monitors geklemmt.
- Fokusverlust-Überwachung: nach dem Sichtbarwerden schließt der Host das
  Overlay, sobald ein anderes Fenster den Vordergrund erhält, und meldet
  `overlay_hidden`.
- Der JavaScript-Bridge bleibt auf `hide_overlay` und `overlay_event`
  begrenzt.

## 7. React Overlay

`webgui/src/overlay/`:

- `OverlayShell.jsx` rendert nach Modus `CommandWheel`, `ClipboardOverlay`
  oder (nur mit `diagnostic: true`) die Diagnosekarte.
- `CommandWheel.jsx`:
  - SVG-Kreis aus bis zu 8 gleich großen Sektoren um den Mittelpunkt;
  - Mausrad blättert zyklisch durch die Seiten;
  - Punktreihe zeigt Seitenanzahl und aktive Seite;
  - Hover: Sektor voll opak und scheinwerferartig hervorgehoben, restliche
    Sektoren 50 % Opacity;
  - Klick führt die Aktion über `POST /api/actions/execute` aus.
- `ClipboardOverlay.jsx`:
  - feste Höhe, scrollbare Liste des aktiven Profils;
  - Aktionen pro Item: in die Windows-Zwischenablage setzen, nachladen,
    pin/unpin, löschen;
  - Transferfortschritt pro Item aus `/api/clipboard/progress` und
    Stream-V2-Status aus `/api/clipboard/status`;
  - Aktualisierung asynchron (Polling + SSE), Listencontainer wird nicht
    neu gemountet, Scrollposition bleibt erhalten.

## 8. Runtime-Integration

- `Ctrl+Alt+V` und (bei `intercept_win_v`) `Win+V` öffnen das
  Clipboard-Overlay statt des Tkinter-Fensters.
- Der Wheel-Hotkey öffnet das Command Wheel.
- Hotkey-Handler nur enqueue, kein Warten auf Host/IPC.
- `build_status_snapshot()["overlay"]` bleibt unverändert.

## 9. WebGUI

- Einstellungen: Command-Wheel-Belegung (Seiten, Slots, Hotkey) editierbar.
- Clipboard-Tab bleibt Verwaltungs- und Transferansicht; Scrollposition bleibt
  bei Refresh erhalten.
- Der Tkinter-Clipboard-Tab und das Tkinter-Clipboard-Fenster werden
  abgelöst: Hotkeys und Tray öffnen das Overlay bzw. die WebGUI.

## 10. Tests

- `test_overlay_actions.py`: Registry, Wheel-Normalisierung (Limits,
  Defaults, Ablehnung), Tastenfolgen der Aktionen.
- `test_web_api_actions.py`: alle drei Routen offline über HTTP, inkl.
  Ablehnung unbekannter Aktionen und ungültiger Seiten, Persistenz.
- Overlay-Lifecycle-Tests erweitern: Modusgrößen, Payload mit Wheel-Daten.
- Vitest: `CommandWheel` (Sektoranzahl, zyklisches Blättern, Punkte,
  Hover-Klassen, Execute-Aufruf), `ClipboardOverlay` (Scrollposition bleibt
  nach Refresh, Aktionen rufen API).
- Bestehende Overlay-Stresstests bleiben grün.

## 11. Slices

1. Spezifikation, Action Registry, Web-API-Vertrag.
2. Overlay-Host: Modusgrößen, Fokusverlust-Dismiss, Wheel-Payload.
3. React Command Wheel.
4. React Clipboard Overlay.
5. Runtime-Hotkeys und Aktionsausführung in `tray.py`.
6. WebGUI-Verwaltung, Tkinter-Ablösung, Doku, Release.

Jeder eigene Commit verwendet die nächste freie `0.7.0-dev.N`-Version.
