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
- Alpha-Komposition mit weichen Kanten oder Schatten (eigener
  DirectComposition-Host); die Form entsteht über eine Fensterregion.
- Mausverfolgung des Overlays.

## 3. Verhaltensregeln (verbindlich)

- Overlay-Fenster spawnen an der Cursorposition und bleiben dort statisch.
  Das Command Wheel ist um den Cursor zentriert; muss es am Bildschirmrand
  nach innen rücken, wird der Cursor auf die Wheel-Mitte gesetzt.
- Sichtbar ist nur die Bedienfläche: beim Wheel die Ring-Sektoren der
  aktuellen Seite (plus die kleine Seitenanzeige in der Mitte bei mehreren
  Seiten), beim Clipboard das Panel mit runden Ecken. Mitte, Ecken und
  Zwischenräume sind durchsichtig und nicht klickbar (Klicks gehen an das
  Fenster darunter). Kein Rahmen, keine Titelleiste, kein Rechteckschatten.
- Das Overlay bleibt offen, bis eine Aktion gewählt, außerhalb der sichtbaren
  Fläche geklickt (`outside_click`), `Escape` gedrückt (`escape`) oder ein
  anderes Fenster aktiviert wird (`focus_lost`). Es schließt nie, nur weil
  Windows ihm den Vordergrund verweigert. Jedes Schließen wird mit Grund
  geloggt: `overlay_hidden reason=<grund> mode=<modus>`.
- Ein verstecktes Overlay fragt keine API mehr ab (React-Ansicht wird beim
  Verstecken entfernt).
- Command Wheel und Clipboard-Overlay sind funktionale Komponenten, keine
  Diagnoseansichten. Die Diagnosedarstellung bleibt nur erreichbar, wenn das
  Show-Payload `diagnostic: true` enthält (WebGUI-Diagnosepanel).
- Keine Overlay- oder Clipboard-Aktion darf den Runtime-Hauptthread
  (Window-Thread, Hook-Callbacks) oder den React-UI-Thread blockieren.
- Jedes lokale Copy-Ereignis erzeugt genau ein History-Item und damit genau eine
  Karte. Das gilt auch, wenn derselbe Inhalt erneut kopiert wird. Eine
  Mehrfachauswahl von Dateien bleibt ein gemeinsames `file_batch`-Item und wird
  nicht in einzelne Karten zerlegt.
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
- Zusätzliche Eingänge (nicht konfigurierbar):
  - `Ctrl+Rechtsklick` überall auf dem Desktop öffnet das Command Wheel
    (eigener, immer aktiver `WH_MOUSE_LL`-Hook; nur Rechtsklicks bei
    gehaltenem Ctrl werden konsumiert, Down und Up; normale Rechtsklicks und
    Klicks während aktivem Forwarding bleiben unberührt); zusätzlich
    `Ctrl+Rechtsklick` auf das Tray-Icon;
  - Clipboard-Overlay: einstellbares Kürzel `clipboard.overlay_hotkey`
    (Standard `Ctrl+Alt+V`) und `Ctrl+Win+V` immer bei aktivem Clipboard,
    `Win+V` weiterhin nur per `intercept_win_v`;
  - belegte Kürzel (`RegisterHotKey`-Fehler 1409) meldet die Runtime als
    `overlay_hotkeys` in `/api/status` mit Klartext und Ersatzkürzel; Tray,
    Dashboard und Einstellungen zeigen das an (`docs/hotkey-config.md`).

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
- Form: `overlay_geometry.overlay_shape_polygons` liefert die sichtbaren
  Flächen in physischen Pixeln; der Host schneidet das Fenster mit
  `SetWindowRgn` darauf zu (gilt auch für die WebView2-Kindfenster). Beim
  Blättern meldet React `overlay_event("wheel_page", {page})`, der Host passt
  die Region an. Die Wheel-Maße stehen identisch in `overlay_geometry.py` und
  `wheelGeometry.js`.
- Schließ-Überwachung (`overlay_host.DismissPolicy`): Polling alle 30 ms
  von Vordergrund, `Escape` und Maustasten per `GetAsyncKeyState`, ohne
  globalen Hook. Schließgründe: `escape`, `outside_click`, `focus_lost`,
  dazu `close_button` aus React sowie `action` und `runtime` aus der Runtime
  (nach einer Wheel-Aktion schließt die Runtime, nicht React).
  Beim Öffnen schon gehaltene Tasten zählen nicht. Der Host meldet
  `overlay_hidden {reason, mode}`, der Controller loggt jede Meldung.
- Der JavaScript-Bridge bleibt auf `hide_overlay(reason)` und
  `overlay_event` begrenzt.

## 7. React Overlay

`webgui/src/overlay/`:

- `OverlayShell.jsx` rendert nach Modus `CommandWheel`, `ClipboardOverlay`
  oder (nur mit `diagnostic: true`) die Diagnosekarte.
- `CommandWheel.jsx`:
  - SVG-Ring aus bis zu 8 gleich großen Sektoren um den Mittelpunkt, ohne
    Mittelscheibe;
  - Mausrad blättert zyklisch durch die Seiten;
  - Punktreihe in der Mitte zeigt Seitenanzahl und aktive Seite (nur bei
    mehr als einer Seite, ab 9 Seiten als `n/m`); Fehler einer Aktion stehen
    im betroffenen Sektor;
  - Hover: Sektor voll opak und scheinwerferartig hervorgehoben, restliche
    Sektoren 50 % Opacity;
  - Klick führt die Aktion über `POST /api/actions/execute` aus.
- `ClipboardOverlay.jsx`:
  - feste Höhe, scrollbare Kartenliste des aktiven Profils;
  - getrennte Ansichten `Recent` (nur ungepinnte Items) und `Pinned` (nur
    gepinnte Items); Pin/Unpin verschiebt die Karte zwischen den Ansichten;
  - eine Karte pro History-Item mit typabhängigem Inhalt:
    - `file_batch`: Batch-Icon links; in der Mitte Root-/Ordnername mit
      Copy-Button sowie separat kopierbare Dateinamen- und relative
      Pfadlisten; rechts Retry/Get, Pin und Delete;
    - `file`: passende, begrenzte Vorschau beziehungsweise ein eindeutiger
      Dateityp-Fallback links, Dateiname und relative Pfadangabe in der Mitte;
      dieselben Aktionen rechts;
    - `text`: lesbares Textfeld und UTF-8-Textgröße; keine künstliche
      Dateivorschau;
    - übrige Typen erhalten eine sichere typgerechte Vorschau oder einen klaren
      Fallback, ohne beliebige aktive Inhalte auszuführen;
  - Batch-Karten zeigen Current-File-Text, Current-File-Fortschritt,
    Gesamttext und Gesamtfortschritt. Beide Balken laufen unabhängig von
    0 bis 100 Prozent; der Gesamtbalken basiert auf übertragenen Bytes. Dazu
    kommen Dateiordinal `N/X`, Größe, Rate, Restzeit als `HH:mm:ss` und, soweit
    bereits abgeschlossen, die Dauer der zuletzt übertragenen Datei;
  - Einzeldateien zeigen nur den einen Dateifortschritt, Text und andere
    Einzelitems einen Gesamtfortschritt ohne redundanten zweiten Balken;
  - Dateilisten und kopierbare Pfade stammen aus validierten relativen
    Manifestpfaden. Private absolute Quellpfade gelangen nicht in HTTP-Antworten;
  - Aktionen pro Item: in die Windows-Zwischenablage setzen, nachladen,
    pin/unpin, löschen;
  - Löschen entfernt das Item aus persistentem Store und sichtbarer Liste;
    Pinning bleibt über Neustarts im persistenten Store erhalten;
  - Transferfortschritt pro Item aus `/api/clipboard/progress` und
    Stream-V2-Status aus `/api/clipboard/status`;
  - Aktualisierung asynchron (Polling + SSE), Listencontainer wird nicht
    neu gemountet, Scrollposition bleibt erhalten.

## 8. Runtime-Integration

- Jedes Copy-Ereignis wird immer in die lokale History (`local`, Label
  `This PC`) geschrieben, zusätzlich in die aktiven/konfigurierten
  Peer-Profile. Das Clipboard ist damit ohne konfigurierten Peer benutzbar.
- Das Overlay öffnet per Default das aktive Peer-Profil, sonst `local`;
  `clipboard_sync` auf `local` ist ein ok-noop (kein Manifest an Peers).
- `GET /api/clipboard/profiles` listet `local` zuerst, danach die Peers;
  fehlende `profile`-Parameter fallen auf `local` zurück.
- `Ctrl+Alt+V`, `Ctrl+Win+V` und (bei `intercept_win_v`) `Win+V` öffnen das
  Clipboard-Overlay statt des früheren Tkinter-Fensters.
- Der Wheel-Hotkey und globales `Ctrl+Rechtsklick` öffnen das Command Wheel.
- Hotkey-Handler nur enqueue, kein Warten auf Host/IPC.
- `build_status_snapshot()["overlay"]` bleibt unverändert.

## 9. WebGUI

- Einstellungen: Command-Wheel-Belegung (Seiten, Slots, Hotkey) editierbar.
- Clipboard-Tab bleibt Verwaltungs- und Transferansicht; Scrollposition bleibt
  bei Refresh erhalten und verwendet dieselben Karten- und Fortschrittsregeln
  wie das Clipboard-Overlay.
- Der Tkinter-Clipboard-Tab und das Tkinter-Clipboard-Fenster werden
  abgelöst: Hotkeys und Tray öffnen das Overlay bzw. die WebGUI.

## 10. Tests

- `test_overlay_actions.py`: Registry, Wheel-Normalisierung (Limits,
  Defaults, Ablehnung), Tastenfolgen der Aktionen.
- `test_web_api_actions.py`: alle drei Routen offline über HTTP, inkl.
  Ablehnung unbekannter Aktionen und ungültiger Seiten, Persistenz.
- Overlay-Lifecycle-Tests erweitern: Modusgrößen, Payload mit Wheel-Daten.
- `test_overlay_modes.py`: Ringform (Sektoren, durchsichtige Mitte/Lücken/
  Ecken, Seitenanzeige), Skalierung 100 bis 200 %, Zentrierung am Rand und
  auf negativen Zweitmonitoren, Gleichstand der Maße mit `wheelGeometry.js`.
- `test_overlay_dismiss.py`: bleibt offen ohne Vordergrund, schließt bei
  Escape, Klick außerhalb und Fensterwechsel, genau eine `overlay_hidden`-
  Meldung mit Grund und Modus.
- Vitest: `CommandWheel` (Sektoranzahl, zyklisches Blättern, Punkte,
  Hover-Klassen, Execute-Aufruf), `ClipboardOverlay` (Scrollposition bleibt
  nach Refresh, eine Karte pro Copy-Item, Batch-/Single-/Textdarstellung,
  Pin-Ansichten, kopierbare Listen, Aktionen und konkrete Fortschrittswerte).
- Web-API-Tests prüfen die öffentliche Clipboard-Projektion einschließlich
  relativer Batch-Manifeste und schließen interne beziehungsweise absolute
  lokale Pfade aus.
- Bestehende Overlay-Stresstests bleiben grün.

## 11. Slices

1. Spezifikation, Action Registry, Web-API-Vertrag.
2. Overlay-Host: Modusgrößen, Fokusverlust-Dismiss, Wheel-Payload.
3. React Command Wheel.
4. React Clipboard Overlay.
5. Runtime-Hotkeys und Aktionsausführung in `tray.py`.
6. WebGUI-Verwaltung, Tkinter-Ablösung, Doku, Release.

Jeder eigene Commit verwendet die nächste freie `0.7.0-dev.N`-Version.
