# FlowShift

**Software-KVM: steuere mehrere Geräte per Hotkey mit einem Satz Maus/Tastatur.**

FlowShift leitet Tastatur- und Mauseingaben per Hotkey an ein anderes Gerät im
lokalen Netz weiter und injiziert sie dort. Der **produktive Teil ist heute ein
Python-Prototyp** (Windows). Die weitergehende Video-/Monitor-Matrix ist ein
**geplantes Ziel**, aktuell noch nicht implementiert.

## Was heute wirklich funktioniert (Python-Prototyp)

- Hintergrund-Runtime + Tray-Icon: `src/python/tray.py --tray`
- Lokale React-WebGUI für Einstellungen, Clipboard-Verwaltung und Diagnose.
  Sie ist **die** Oberfläche für Einstellungen; neue Einstellungen kommen nur
  dorthin. Die Tkinter-GUI `src/python/gui.py` ist Altbestand (Legacy) und
  bleibt vorerst nur für Geräte/Profile/Runtime-Steuerung, bis die WebGUI das
  ebenfalls kann
- Peer-Discovery im LAN (UDP-Broadcast), Verbindung per TCP (Port 45781)
- Hotkey-gesteuertes Weiterleiten von Tastatur + Maus (`SendInput`) auf Windows
- Stabile Peer-Identität (`device_id`, sonst Endpoint) und Hotkey-Bindung ohne
  Listenindex-Drift
- Lokaler Control-Socket (`127.0.0.1:45782`) für GUI ↔ Runtime
- Lokale WebGUI mit Edge-Switching-Layout und Session-Status über `webgui/`
- Die lokale Schnittstelle (`127.0.0.1:5000`) nimmt Änderungen (jedes POST
  und DELETE) nur von der eigenen WebGUI an: lokaler Host-Kopf, Herkunft der
  WebGUI, Inhaltstyp JSON. Eine fremde Webseite im Browser kann darüber weder
  Einstellungen ändern noch Tasten einspielen oder Wheel-Aktionen auslösen
  (abgelehnt mit 403/415 und Logzeile `web_api rejected ... reason=...`).
  Jede Route hat zusätzlich eine Größengrenze für den Inhalt (Aktionen und
  Wheel 16 KiB, sonst 64 KiB; sonst 413), geprüft vor dem Lesen
- Profilbezogene Clipboard-Historie für Text, HTML, Dateien/Batches, Bilder und
  animierte GIF-Vorschauen; im Overlay und im WebGUI-Tab lassen sich mehrere
  Einträge markieren (Kästchen, Strg-/Umschalt-Klick, Alle/Umkehren/Keine)
  und sofort ohne Rückfrage löschen; „Alle“ nimmt jeden Eintrag mit,
  Recent und angepinnte. Das zuletzt Kopierte steht oben. Jeder Eintrag mit
  kopierten Dateien hat die Knöpfe „Full path“, „Folder path“, „File name“,
  „Root name“ und „Relative path“: sie legen genau das als Text in die
  Windows-Zwischenablage (je Datei eine Zeile, jeder Ordner einmal), ohne
  einen neuen Eintrag anzulegen; „Set“ legt wie bisher die Dateien selbst
  hinein. Voller Pfad und Ordnerpfad gibt es nur für Dateien, die auf diesem
  PC kopiert wurden.
  Dateikarten zeigen je Datei eine kleine Vorschau: Bilder als Miniatur,
  GIF/APNG/animiertes WebP bewegt (immer oder nur beim Überfahren,
  einstellbar), Videos mit Standbild und kurzer stummer Vorschau beim
  Überfahren, sonst das Windows-Symbol des Dateityps; mehrere Dateien als
  Reihe mit „+N“, fehlende oder gesperrte Dateien neutral. Die Vorschauen
  entstehen im Hintergrund und liegen in einem begrenzten Zwischenspeicher
  (`%ProgramData%\FlowShift\clipboard\previews`)
- Datei-Transfer V2 (`clipboard_stream_v2`, ab 0.6.0 zwischen zwei 0.6.0-Peers):
  Dateien und Ordner werden ohne ZIP/Base64 als Rohdaten über einen eigenen
  Datenkanal gestreamt, mit persistenten Journalen und Fortsetzung nach
  Verbindungsabbruch oder Neustart (Sender, Empfänger oder beide) sowie einem
  gemeinsamen, pro Datei SHA-256-adressierten Objektspeicher; ältere Peers
  nutzen weiterhin den bisherigen ZIP-Pfad
- Isolierter, wiederverwendbarer React-Overlay-Host (`pywebview` + WebView2) mit
  authentifizierter Named-Pipe-IPC, DPI-/Multi-Monitor-Platzierung und
  Crash-Recovery. `Ctrl+Alt+V` (in den Einstellungen änderbar) bzw. `Ctrl+Win+V`
  öffnen die Clipboard-History am Cursor; ist ein Kürzel schon von Windows oder
  einem anderen Programm belegt, meldet FlowShift das im Tray, im Dashboard und
  bei der jeweiligen Einstellung samt Kürzel, das stattdessen geht (rot nur bei
  eingeschalteten Funktionen; das zusätzliche `Ctrl+Win+V` erscheint als
  neutraler grauer Hinweis, Win+V nur bei eingeschaltetem Abfangen); das
  Command-Wheel-Kürzel (Standard `Ctrl+Rechtsklick`) öffnet das konfigurierbare Command Wheel
  als runden Ring um den Mauszeiger: nur die Felder sind sichtbar, Mitte und
  Ecken bleiben durchsichtig. Jedes Kürzel (Command Wheel, Clipboard-Overlay,
  Umschalten zu einem Peer, Rückkehr) ist eine Taste oder eine Maustaste
  (links, rechts, Mitte, Seitentasten, Mausrad) mit Strg/Alt/Umschalt/Win,
  lässt sich in der WebGUI per Drücken aufnehmen und leeren; doppelt
  vergebene Kürzel werden gemeldet (`docs/hotkey-config.md`). Wie im Spiel:
  ein Wheel-Kürzel (Taste oder Maustaste) halten, in Richtung eines Feldes
  wischen und loslassen führt es aus (Loslassen in der Mitte schließt ohne
  Aktion); kurz antippen öffnet das Wheel zum Klicken. Schwelle und beide
  Arten sind unter Einstellungen > Command Wheel einstellbar. Beide Overlays bleiben offen, bis `Escape`, ein
  Klick außerhalb, ein Fensterwechsel oder eine Aktion sie schließt; der Grund
  steht als `overlay_hidden reason=...` im Log. Klicks im Clipboard-Fenster
  (auch in die Suche) schließen es nie; verschoben wird es an Griff, Kopf,
  Reiterzeile oder Fußzeile, und es bleibt liegen, wo die Maustaste
  losgelassen wird.

Details: [src/python/README.md](src/python/README.md).

> Entwickler-Build der WebGUI: `cd webgui && npm ci --include=dev && npm run build`.
> Das Release-Setup enthält die vorgebauten Assets; Node.js und Vite werden auf
> Zielsystemen nicht benötigt.

## Installation (Windows)

Empfohlen: [FlowShift-Setup.exe](https://github.com/CryseXIII/flowshift/releases/latest/download/FlowShift-Setup.exe)
aus dem neuesten stabilen GitHub Release herunterladen und starten. Das Setup
fragt einmal UAC, installiert nach `%ProgramFiles%\FlowShift`, hält Config/Logs
unter `%ProgramData%\FlowShift`, legt Verknüpfungen an und registriert einen
**Autostart in der interaktiven Nutzer-Session** (Scheduled Task, kein
Session-0-Dienst).

Das erste Release ist nicht code-signiert und kann deshalb eine Windows
SmartScreen-Warnung auslösen. SHA-256 steht im Release unter `SHA256SUMS.txt`.

64-Bit-CPython 3.11 bis 3.15 wird unterstützt; bei Bedarf lädt das Setup den
offiziellen Python-3.15.1-Installer herunter, prüft dessen SHA-256 und installiert
ihn oder verwendet eine vorhandene kompatible Installation. Ein vorhandenes
passendes Python wird immer zuerst genommen; heruntergeladen wird nur, wenn
keines antwortet, und vorher steht im Fenster, was wohin installiert wird. Jeder
geprüfte Python-Kandidat steht mit Pfad, Exit-Code und Ausgabe in
`%ProgramData%\FlowShift\logs\install.log`. Bricht die Installation ab, bevor
Programmdateien ersetzt werden, läuft die alte Version unverändert weiter; das
steht am Ende in der Zeile `Status :`.
Die venv liegt unter
`%ProgramFiles%\FlowShift\.venv` und die Scheduled Task nutzt `pythonw.exe` aus
dieser venv. Das Setup deployt nur die vorgebaute WebGUI nach
`%ProgramFiles%\FlowShift\webgui`. Die Python-Abhängigkeiten enthalten Pillow und
`pywebview==6.2.1`; der Installer prüft außerdem den Microsoft WebView2 Evergreen
Runtime und installiert ihn bei Bedarf. WebView2 ist eine geteilte
Systemkomponente und wird von FlowShift nicht deinstalliert.

Alternativ installiert `install_flowshift.bat` aus einem Source-Checkout alles in
einem Rutsch: Programm **und** WebGUI (Pflichtbestandteil, kein Zusatz). Die
WebGUI kommt aus dem vorgebauten, mit `VERSION` gestempelten `webgui/dist`; nur
wenn es fehlt oder zu einer anderen Version gehört und Node.js 24+ schon
installiert ist, baut der interne Baustein `install_webgui.ps1` es neu.

Installieren über eine ältere Installation ist ein Upgrade ohne vorheriges
Deinstallieren: ein laufendes FlowShift wird beendet und danach wieder
gestartet, alte Programm- und WebGUI-Dateien werden ersetzt (die alte WebGUI
wird komplett entfernt), `config.json`, Clipboard-Verlauf und Logs in
`%ProgramData%\FlowShift` bleiben. Außer der Windows-Abfrage für Adminrechte
fragt der Installer nichts; `-Interactive` schaltet Rückfragen ein. Am Ende
prüft er, dass die laufende Runtime dieselbe `VERSION` meldet wie die WebGUI und
`index.html` sowie `overlay.html` ausliefert, und nennt sonst genau, was fehlt.

Passt die WebGUI nicht zum Programm (fehlende `overlay.html`, andere oder keine
`VERSION`), meldet die Runtime das selbst per Tray-Hinweis und im
WebGUI-Dashboard. Ohne `overlay.html` startet sie den Overlay-Host gar nicht
erst; scheitert der Host-Start aus anderen Gründen, wächst der Abstand zwischen
den Versuchen (1 s bis 60 s), nach fünf Fehlversuchen pausiert sie zehn Minuten
und meldet das.

## Updates

Unter WebGUI → Settings → Software Update stehen drei Policies bereit:
`notify`, `download` und `install`, dazu zwei Kanäle:

- **Stable**: nur stabile Versionen aus GitHub `releases/latest`.
- **Development**: jeder gemergte Stand aus dem eigenen Forgejo
  (`private-platform/flowshift`, Zweig `main`). Es gibt kein eigenes Paket:
  FlowShift liest alle 15 Minuten (und sofort nach einem Kanalwechsel) den
  neuesten Commit von `main` und dessen `VERSION`. Ist sie neuer, lädt es das
  Quellarchiv genau dieses Commits, prüft es (Commit-ID im ZIP, keine Pfade
  außerhalb des Archivs, `VERSION`) und installiert es nach der gewählten
  Policy mit dem darin enthaltenen `install_flowshift.ps1`, mit Rückfall auf
  die alte Version bei Fehlern. Kein Git, kein Herunterladen oder Entpacken von
  Hand. Weil `webgui/dist` nicht im Repo liegt, baut die Installation die WebGUI
  selbst; dafür muss Node.js 24 oder neuer (LTS) auf dem PC sein, sonst meldet
  die Seite das, bevor FlowShift angehalten wird. Forgejo ist nur im eigenen
  Netz oder per VPN erreichbar; ohne Verbindung wird die Prüfung still
  übersprungen und die Seite zeigt „Forgejo is not reachable“ mit der letzten
  erfolgreichen Prüfung. Der Zugang (ein Forgejo-Token mit Lesezugriff auf das
  Repo) wird einmal in der WebGUI eingegeben und liegt in der
  Windows-Anmeldeinformationsverwaltung (`FlowShift:forgejo-update`), nie in
  `config.json`, im Repo oder in Skripten. Ein Stand, dessen Installation
  fehlschlug und zurückgerollt wurde, wird nicht automatisch erneut versucht.
  Protokoll: `%ProgramData%\FlowShift\logs\update.log` und `install.log`.
  Scheitert eine Installation, läuft die bisherige Version weiter und
  „Software Update“ erklärt in einfachen Worten, was passiert ist und was zu
  tun ist. FlowShift `0.7.0-dev.29`/`dev.30` scheitern beim Ersetzen von
  `C:\Program Files\FlowShift` („Zugriff verweigert“, der Updater lief im
  Programmordner): einmal `0.7.0-dev.31` oder neuer von Hand installieren,
  danach läuft das Update von selbst.
- Nie zwei Installationen gleichzeitig (ab `0.7.0-dev.35`): Handinstallation,
  Update und Deinstallation teilen sich eine Sperre
  (`%ProgramData%\FlowShift\updates\install.lock`). Eine Handinstallation
  während eines Updates wartet, bis es fertig ist; ein Update während einer
  Handinstallation ändert nichts und versucht es bei der nächsten regulären
  Prüfung. Der Installer kopiert und prüft die neuen Dateien zuerst in
  `C:\Program Files\FlowShift.staging` und tauscht den Programmordner erst
  danach; der alte Ordner bleibt (`FlowShift.previous` bzw.
  `FlowShift.rollback-<version>`), bis die neue Version läuft, und kommt bei
  einem Fehler zurück. Geht auch das nicht, sagt eine Windows-Meldung, wo die
  alte Version liegt und was zu tun ist. Nach einer Handinstallation
  aktualisiert sich FlowShift erst bei der nächsten regulären Prüfung (15
  Minuten später); installiert man aus einem älteren Ordner, meldet der
  Installer die neuere Version.
- Umstieg eines älteren FlowShift (vor `0.7.0-dev.29`) auf den Kanal: einmal den
  aktuellen Stand aus Forgejo als ZIP laden, entpacken, `install_flowshift.bat`
  doppelklicken; danach in der WebGUI Kanal „Development“ wählen und den
  Zugang eintragen. Ab dann kommen neue Stände von selbst.

Die Seite zeigt installierte Version, Kanal, Quelle, letzte Prüfung und letzte
erfolgreiche Prüfung. Installergröße und SHA-256 werden vor dem externen
Installations-Handoff geprüft. Automatische Installation wartet, bis Forwarding,
Input, Overlay und Clipboard-Transfers idle sind; bei fehlgeschlagener
Installation oder Health-Prüfung wird das vorherige Programmverzeichnis
zurückgerollt. In einem Development Checkout bleiben Prüfung und Download
verfügbar, Installation ist dort absichtlich gesperrt.

> **Wichtig:** Input-Forwarding braucht die interaktive User-Session. Ein
> Windows-**Dienst** (Session 0) kann Maus/Tastatur **nicht** capturen/injizieren.
> Deshalb wird standardmäßig **kein Dienst** installiert; NSSM ist nur optional
> (`-WithNssm`) und ausdrücklich nicht der Input-Pfad. Die GUI warnt rot, falls
> die Runtime je in Session 0 läuft. Deinstallation: `uninstall_flowshift.bat`
> (entfernt Programm und WebGUI; Einstellungen und Daten bleiben, außer mit
> `-PurgeData`).

Manuell/Dev: `src/python/` kopieren, `python tray.py --tray` starten (oder GUI).

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
| `src/python/gui.py` | Python (tkinter) | **Altbestand (Legacy)** – Geräte/Profile/Runtime-Steuerung (kein Clipboard-UI); keine neuen Einstellungen, Ausbau-Plan in Stufen in `TODO_CURRENT.md` |
| `src/python/runtime_model.py` | Python (rein, plattformunabhängig) | **produktiv** – geteilte Logik |
| `src/python/overlay_controller.py` | Python | **produktiv** – isolierter Overlay-Lifecycle + Routing |
| `src/python/overlay_host.py` | pywebview/WebView2 | **produktiv** – lokaler React-Overlay-Host |
| `src/python/overlay_native_wheel.py` | ctypes, GDI+, `UpdateLayeredWindow` | **produktiv** – Command Wheel als nativ gezeichnetes, echt durchsichtiges Fenster (ab dev.36; WebView2-Wheel nur als Rückfall) |
| `src/python/action_handlers.py` | Python (plattformunabhängig) | **produktiv** – Handler-Tabelle mit Risikoklasse und zentrale Prüfstelle der Command-Wheel-Aktionen (Kanal `wheel_swipe`/`web_api`, nur vom Tray gesetzt) |
| `webgui/` | React/Vite | **produktiv** – Einstellungen, Clipboard-Verwaltung sowie funktionale Clipboard-/Command-Wheel-Overlays |
| `flowshift-shared` | Rust (lib) | experimentell – gemeinsame Typen |
| `flowshift-service` | Rust (daemon) | **experimentell, baut derzeit nicht** |
| `flowshift-viewer` | Rust (renderer) | **Stub** – nur Platzhalter, kein Video |
| Tauri-GUI | – | **nicht vorhanden** (React läuft über Browser/WebView2) |

> Hinweis: Es gibt aktuell **keinen** funktionierenden Video-/Fullscreen-Empfänger
> und **keine** Tauri-GUI im Repo. Die vorhandene React/Vite-Oberfläche läuft
> lokal im Browser beziehungsweise im WebView2-Overlay. `flowshift-viewer` ist ein Stub;
> `flowshift-service` ist experimenteller Code, der derzeit nicht kompiliert und
> daher aus dem Cargo-Workspace ausgeschlossen ist. Siehe
> [FLOWSHIFT_AUDIT_AND_FIX_REPORT.md](FLOWSHIFT_AUDIT_AND_FIX_REPORT.md).

Siehe [docs/architecture.md](docs/architecture.md) und [docs/protocol.md](docs/protocol.md).

## Entwicklung und Veröffentlichung

- Entwickelt wird in Forgejo, Repo `private-platform/flowshift`, Zweig
  `main`. Dort liegen der aktuelle Stand, die Prüfläufe und die Merges.
- GitHub (`CryseXIII/flowshift`) bekommt nur ab und zu eine freigegebene neue
  Version; dort wird nicht entwickelt. Der Kanal „Stable“ liest GitHub
  `releases/latest`, eine stabile Version kommt also erst nach einer
  freigegebenen Veröffentlichung an. Der Kanal „Development“ bekommt jeden
  gemergten Stand aus Forgejo `main` direkt als Quellarchiv; es wird nichts
  gebaut oder hochgeladen (kein Runner, kein Inno Setup).
- Jede Änderung an dem, was installiert wird (Dateien, Abhängigkeiten,
  Autostart, Registry, Ports, WebGUI, Daten unter `%ProgramData%\FlowShift`),
  zieht Installer, Updater und Uninstaller im selben Schritt nach; die Doku
  wird immer mitgepflegt. Die Regeln dazu stehen in [AGENTS.md](AGENTS.md).

## Lizenz

MIT

