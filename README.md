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
- Peer-Discovery im LAN (UDP-Broadcast), Verbindung per TCP (Port 45781);
  über WireGuard/Tailscale fragt „Scan Network“ zusätzlich direkt die
  Tailscale-Geräte und die Adressen der VPN-Netze ab. Findet der Scan nichts,
  erklärt die Peers-Seite warum und bietet das Adressfeld an; der andere PC
  bietet danach „Add here too“ an, „This PC“ zeigt Adresse, Port und Geräte-ID
  zum Abtippen; ein fehlgeschlagener Ping nennt die wahrscheinliche Ursache
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
  (`%ProgramData%\FlowShift\clipboard\previews`). Kopierte Grafiken ohne
  Datei (im Browser „Bild kopieren“, Ausschnitt aus einem Programm) zeigen
  ihre Miniatur genauso, Transparenz bleibt erhalten; FlowShift bewahrt dafür
  das PNG der Zwischenablage (sonst die Bitmap) und legt beim Kopieren aus
  dem Clipboard wieder PNG und Bitmap ab, so dass auch Paint es einfügt.
  Liegt nur HTML mit einer Bildadresse vor, erscheint das neutrale Symbol;
  aus dem Netz wird nichts nachgeladen
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
  eine Command-Wheel-Kürzel (genau eines, Standard `Ctrl+Rechtsklick`; geleert
  öffnet nur noch `Ctrl+Rechtsklick` auf das Tray-Symbol das Wheel, die WebGUI
  sagt das und fragt vor dem Speichern; „Save Settings“ speichert eine
  Wheel-Änderung mit) öffnet das konfigurierbare Command Wheel
  als runden Ring um den Mauszeiger: nur die Felder sind sichtbar, Mitte und
  Ecken bleiben durchsichtig. Jedes Kürzel (Command Wheel, Clipboard-Overlay,
  Umschalten zu einem Peer, Rückkehr) ist eine Taste oder eine Maustaste
  (links, rechts, Mitte, Seitentasten, Mausrad) mit Strg/Alt/Umschalt/Win,
  lässt sich in der WebGUI per Drücken aufnehmen und leeren; doppelt
  vergebene Kürzel werden gemeldet (`docs/hotkey-config.md`). Ein gespeichertes
  Kürzel bleibt nach dem Neuladen stehen; belegt Windows oder ein anderes
  Programm die Kombination, bleibt es trotzdem gespeichert und die WebGUI sagt
  direkt beim Speichern-Knopf, dass und warum es nicht aktiv ist. Wie im Spiel:
  ein Wheel-Kürzel (Taste oder Maustaste) halten, in Richtung eines Feldes
  wischen und loslassen führt es aus (Loslassen in der Mitte schließt ohne
  Aktion); kurz antippen öffnet das Wheel zum Klicken. Schwelle und beide
  Arten sind unter Einstellungen > Command Wheel einstellbar. Beide Overlays bleiben offen, bis `Escape`, ein
  Klick außerhalb, ein Fensterwechsel oder eine Aktion sie schließt; der Grund
  steht als `overlay_hidden reason=...` im Log. Klicks im Clipboard-Fenster
  (auch in die Suche) schließen es nie; verschoben wird es an Griff, Kopf,
  Reiterzeile oder Fußzeile, und es bleibt liegen, wo die Maustaste
  losgelassen wird.
- Remote-Vorschau, Stufe 1 von 4 (ab `0.7.0-dev.48`): unter Einstellungen >
  Remote Preview lassen sich Schalter, Bildrate, Breite, Monitor, Ausschnitt um
  den Mauszeiger, Fensterecke und -breite speichern. Nach einem Wechsel (Hotkey
  oder Bildschirmrand) fragt FlowShift den anderen Rechner nach einer Vorschau,
  wenn der Schalter auf beiden an ist; der andere Rechner lehnt jede Anfrage
  mit `pairing_required` ab, bis das gesicherte Koppeln der Geräte gebaut ist.
  Es gibt noch kein Bild, kein Fenster und keinen zusätzlichen Port; das
  Dashboard zeigt den Zustand (Karte „Remote Preview“), `flowshift.log` die
  Ablehnung. Details: [docs/protocol.md](docs/protocol.md).

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

### USB-Stick-Paket für andere PCs (ab `0.7.0-dev.53`)

Für PCs ohne Git, ohne Node.js und ohne Zugang zu Forgejo:

1. Auf dem eigenen PC (Kanal „Development“) in der WebGUI unter
   **Settings → Software Update** auf **Create USB package** klicken. Nach ein
   paar Minuten steht dort „Saved: …\Downloads\FlowShift-0.7.0-dev.53-usb.zip“.
   Das ZIP enthält genau die hier installierte Version samt fertiger WebGUI.
2. Das ZIP auf den USB-Stick kopieren.
3. Auf dem anderen PC das ZIP entpacken und im entpackten Ordner
   `install_flowshift.bat` doppelklicken, die Windows-Abfrage bestätigen.
   Fertig. Ist dort schon ein älteres FlowShift installiert, wird es mit
   demselben Doppelklick aktualisiert (gleiche Sperre, gleicher Rückfall auf die
   alte Version bei einem Fehler wie bei jeder Installation; Einstellungen
   bleiben).

Was im Paket steckt und warum: Das Paket enthält außer Programm und WebGUI den
offiziellen Python-Installer (dieselbe Version und SHA-256 wie im Installer),
pip und alle Python-Pakete als fertige Windows-Dateien für jede unterstützte
Python-Version, für die es sie gibt. So braucht der Ziel-PC **kein Internet**:
Hat er kein passendes Python, installiert der Installer das mitgelieferte;
Pakete kommen nur aus dem Paket (`--no-index`), jede Datei wird vorher gegen
die SHA-256 in `offline\usb_package.json` geprüft. Das macht das ZIP größer
(rund 70–100 MB), ist aber auf einem Stick egal und verhindert Abbrüche
mitten in der Installation. Nicht im Paket ist die Microsoft-WebView2-Laufzeit
(150 MB, in Windows 11 und aktuellem Windows 10 schon vorhanden); fehlt sie,
versucht der Installer sie herunterzuladen und sagt sonst klar, dass der PC
einmal ans Internet muss.

Wenn der Python-Download beim Erstellen scheitert (ab `0.7.0-dev.55`): FlowShift
lädt den Python-Installer automatisch ein zweites Mal. Klappt auch das nicht,
nennt die Meldung die Ursache (Abbruch der Verbindung, Webseite eines
Proxys/Filters statt der Datei, Virenscanner hat die Datei verändert, falsche
Bytes), die erwartete und die erhaltene Größe und SHA-256, HTTP-Status und
Dateityp sowie den Pfad zu `flowshift.log` mit allen Einzelheiten. Ausweg ohne
Abschalten der Prüfung: den Link `python-3.15.1-amd64.exe` unter der Meldung
anklicken, die Datei im Browser in den Ordner „Downloads“ speichern und
**Use downloaded Python installer** klicken. FlowShift prüft die Datei mit
derselben SHA-256 (auch „python-3.15.1-amd64 (1).exe“ wird gefunden) und lehnt
eine falsche ab. pip und die Python-Pakete werden bei einem Fehler ebenfalls
einmal neu geladen; eine Proxy-Seite unter Paketnamen wird erkannt.

Pakete, die es nur als Quellcode gibt (ab `0.7.0-dev.56`): FlowShift fragt vor
dem Download bei PyPI nach, ob es für jedes Paket aus `requirements.txt` eine
fertige Windows-Datei für jede unterstützte Python-Version gibt. `proxy-tools`
(braucht pywebview) gibt es nur als Quellcode; es wird beim Erstellen auf dem
Entwicklungs-PC einmal zu einer fertigen Datei gebaut (der Quellcode wird
vorher gegen die SHA-256 in `requirements.txt` geprüft) und in jedes
Python-Paketset gelegt. Ihre SHA-256 steht zusätzlich in
`offline\requirements-offline.txt`, mit der der Ziel-PC ohne Internet und ohne
Compiler installiert. Fehlt für ein Paket wirklich etwas, nennt die Meldung das
Paket mit Version und sagt, ob ein neuer Versuch hilft (Netz) oder ob die
Meldung gemeldet werden muss (FlowShift braucht eine andere Version).

Ein so installierter PC steht auf dem Kanal **Stable (GitHub)** und fragt nie
Forgejo. Seine installierte Version ist oft neuer als die letzte
GitHub-Version; dann ist nichts zu tun, FlowShift installiert nie eine ältere
Version (steht so auch unter Software Update).

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
`notify` (nur melden), `download` (selbst herunterladen) und `install` (selbst
herunterladen und installieren), dazu zwei Kanäle:

- **Stable** (Voreinstellung jeder neuen Installation): nur stabile Versionen
  aus GitHub `releases/latest`. Das ist der Weg für alle Rechner außer Viktors
  Entwicklungs-Laptops: Der Rechner braucht dafür **kein Git, kein Node.js und
  keinen Forgejo-Zugang**, nur Internet. Ein laufendes FlowShift schaut beim
  Start und danach alle 6 Stunden bei GitHub nach, also auch ohne Neustart.
  Wer `install` gewählt hat, bekommt die neue Version von selbst; bei `notify`
  geht man auf Settings → Software Update und klickt nacheinander **Check**,
  **Download**, **Install**. Die drei Schritte stehen dort sichtbar mit
  Haken (fertig), Kreuz (Fehler) oder Pfeil (als Nächstes dran); nach jedem
  Klick erscheint, was herausgekommen ist (z. B. „Check finished: FlowShift
  0.7.0 is available“) oder in einfachen Worten, was schiefging und was zu tun
  ist (z. B. kein Internet). Beim Installieren schließt sich FlowShift, startet
  nach etwa einer Minute neu und die Seite verbindet sich von selbst wieder.
  Ein Rechner, der einmal eine Entwicklungsversion hatte (`0.7.0-dev.N`),
  kommt über Stable ebenfalls auf die nächste stabile Version (`0.7.0`).
  Vorabversionen (Pre-Releases) installiert Stable nie.
- **Development**: jeder gemergte Stand aus dem eigenen Forgejo
  (`private-platform/flowshift`, Zweig `main`). Es gibt kein eigenes Paket:
  FlowShift liest alle 15 Minuten (und sofort nach einem Kanalwechsel) den
  neuesten Commit von `main` und dessen `VERSION`. Ist sie neuer, lädt es das
  Quellarchiv genau dieses Commits, prüft es (Commit-ID im ZIP, keine Pfade
  außerhalb des Archivs, `VERSION`) und installiert es nach der gewählten
  Policy mit dem darin enthaltenen `install_flowshift.ps1`, mit Rückfall auf
  die alte Version bei Fehlern. Kein Git, kein Herunterladen oder Entpacken von
  Hand. Nur für Viktors eigene Rechner gedacht. Wechselt man zurück auf
  Stable, zeigt die Seite keine Forgejo-Fehler mehr, sondern prüft sofort
  GitHub. Weil `webgui/dist` nicht im Repo liegt, baut die Installation die WebGUI
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
- **Updates von verbundenen Geräten** (ab `0.7.1-dev.2`, Viktor 25.11.): Sind
  zwei gekoppelte FlowShift-PCs verbunden, bietet der PC mit der höheren
  Version dem anderen sein installiertes Programm an (Programmdateien und
  fertige WebGUI, mit Liste aller Dateien samt SHA-256). Der PC mit der
  niedrigeren Version entscheidet unter Settings → Software Update →
  **Updates from connected devices**, mit denselben drei Stufen:
  `notify` (**Voreinstellung**: nur anzeigen, übertragen wird erst nach Klick
  auf den Pfeil-nach-unten-Knopf (herunterladen) oder den Ein/Aus-Knopf
  (installieren und neu starten); jeder Knopf zeigt beim Darueberfahren,
  was er tut), `download` (selbst holen und
  prüfen, Installieren per Klick) und `install` (selbst holen, prüfen,
  installieren und FlowShift neu starten). Wer Viktors Wunsch „wird
  geupdatet und neu gestartet“ will, stellt diese eine Auswahl auf
  „Download, install and restart automatically“ und speichert. Regeln:
  nur Geräte aus der eigenen Geräteliste (mit Geräte-ID) zählen, das Paket
  wird über die dort eingetragene Adresse geholt und das Gegenüber muss mit
  derselben Geräte-ID antworten; Angebote von Geräten ohne Eintrag mit
  Geräte-ID werden nie geholt, die Seite sagt dann, dass das Gerät per
  „Scan Network“ unter Peers (auf beiden PCs) eingetragen werden muss. Nur echt neuere Versionen, nie ein Downgrade. Ein PC auf
  Stable nimmt eine Entwicklungsversion (`X.Y.Z-dev.N`) nur mit dem Haken
  „Also accept development versions“ an (für Viktors eigene Zweitrechner,
  nicht für fremde PCs); auf Development immer. Die Übertragung läuft über
  eine zweite kurze Verbindung zum vorhandenen FlowShift-Port (kein neuer
  Port), setzt nach einem Abbruch dort fort, wo sie stand, und lässt sich
  abbrechen. Vor der Installation wird geprüft: ganze Datei mit Größe und
  SHA-256 aus dem Angebot, jede Datei im ZIP gegen `peer_package.json`
  (nichts fehlt, nichts zusätzlich, keine unsicheren Pfade), `VERSION` und
  WebGUI-Stempel gleich der angebotenen Version. Installiert wird über
  denselben Updater wie jedes andere Update (Sperre, Rückfall auf die alte
  Version). Bei `install` sagt FlowShift eine Minute vorher per
  Tray-Meldung und auf der Seite Bescheid (abbrechbar) und startet nie
  während einer Übertragung, Zwischenablage-Aktion oder Weiterleitung neu.
  Was passiert ist, steht danach auf der Seite („What happened“, letzte
  Aktualisierung von welchem Gerät) sowie in `flowshift.log` und
  `update.log`. Grenzen: Die Prüfsummen beweisen, dass das Paket vollständig
  und unverändert ankam, nicht wer es gebaut hat; bis zur abgesicherten
  Kopplung ([Karte 722](http://kanban.thegoodhunter.vps/tasks/722)) könnte ein
  Gerät im selben Netz, das Adresse und Geräte-ID eines gekoppelten PCs
  vortäuscht, ein Paket anbieten. Deshalb ist `notify` voreingestellt. Ein PC
  bekommt diese Funktion selbst erst, wenn er einmal `0.7.1-dev.2` oder neuer
  hat (USB-Paket oder Update); danach ist nie wieder ein Stick nötig.
- **Ablauf und Anzeige beim Update eines verbundenen PCs** (ab `0.7.1-dev.4`,
  Viktor 25.11. 17:34), in einfachen Worten am Beispiel PC 1 (neue Version)
  und PC 2 (ältere Version):
  1. Sobald sich PC 1 und PC 2 sehen (beim Verbinden, nicht erst am
     Bildschirmrand), vergleichen sie die Versionen. Hat sich PC 1 gerade
     selbst aktualisiert, bietet er PC 2 die neue Version nach dem
     Wiederverbinden von selbst an; was PC 2 damit macht, bestimmt dessen
     Einstellung (siehe oben).
  2. Auf PC 1 steht im Dashboard bei PC 2 jeweils der Zustand: „Older version
     X“, „Update is being transferred (N %)“, „Update waits until you are back
     on this PC“, „Is being installed“, „Restarts (connection gone for about
     90 s)“, „Connected again, version Y“ oder „Update failed: Grund; runs on
     with X“. Dazu eine kurze Tray-Meldung, wenn das Update beginnt und wenn
     PC 2 wieder da ist.
  3. Solange du von PC 1 aus PC 2 steuerst, startet PC 2 nicht neu: die
     Installation wartet, bis du wieder auf PC 1 bist. Willst du während der
     Installation oder des Neustarts zu PC 2 wechseln, kommt die Meldung „PC 2
     is being updated …“ statt eines stillen Fehlschlags; sobald PC 2 wieder
     verbunden ist, geht der Wechsel wieder.
  4. Nach dem Neustart verbindet sich PC 2 von selbst wieder (kein Handgriff,
     kein neues Koppeln); PC 1 zeigt die gleiche Version.
  5. Schlägt die Installation fehl, setzt der Updater PC 2 auf die alte
     Version zurück; PC 2 verbindet sich wieder und meldet den Grund an PC 1.
     Nach zwei Fehlversuchen mit derselben Version pausieren die
     automatischen Versuche (Meldung auf beiden PCs); ein Klick auf Install
     an PC 2 oder eine neuere Version versucht es wieder.
  6. Übertragen werden nur Programm und WebGUI (rund 2 bis 3 MB statt des
     86-MB-USB-Pakets). Ist `requirements.txt` gleich geblieben, übernimmt der
     Installer die Python-Pakete der bisherigen Version (Kopie, kein
     Download); nur wenn sie sich geändert hat, lädt PC 2 die Pakete wie
     bisher aus PyPI (Internet nötig).
  7. PCs mit verschiedenen Versionen verbinden sich weiter. Sprechen sie eine
     andere Verbindungs-Protokollversion, bleiben Handshake und Update
     möglich, nur das Weiterleiten von Maus und Tastatur wartet, bis beide
     gleich sind.
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
| `src/python/remote_preview.py` | Python (plattformunabhängig) | **produktiv, Stufe 1** – Einstellungen, Nachrichten, Lebenszyklus und Sperre der Remote-Vorschau; lehnt jede Anfrage bis zum Pairing ab, kein Bild |
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
- Veröffentlichen (Forgejo-Stand → GitHub-Release) ist **ein** Schritt und
  läuft nur mit Viktors ausdrücklicher Freigabe für genau diese Version:
  `python3 scripts/publish_github_release.py plan` zeigt ohne etwas zu ändern,
  welcher Stand als welche Version rausginge, was seit der letzten
  GitHub-Version neu ist und was dagegen spricht.
  `publish --version X.Y.Z --freigabe "<wer, wann, wo>"` setzt `VERSION` auf
  `X.Y.Z`, legt Commit und Tag `vX.Y.Z` an, schiebt beides nach Forgejo (Zweig
  `release/vX.Y.Z`) und dann nach GitHub (`master`, nur vorspulen, nie
  erzwingen). Gebaut wird **ohne GitHub Actions** auf dem eigenen Server:
  `assets --version X.Y.Z --freigabe "..."` (über
  `/srv/opencode/autopilot/bin/flowshift-github release`, das den Token
  liefert) baut aus dem Tag `FlowShift-Setup.exe` (Inno Setup unter Wine),
  WebGUI, `update-manifest.json` und `SHA256SUMS.txt`, prüft sie wie der
  Updater, lädt sie in einen Entwurf, liest sie zurück und veröffentlicht erst
  dann. Ein roter `npm test` stoppt den Bau; nur die ausdrückliche Option
  `--rote-webgui-tests-nur-protokollieren` (nie Standard, nur auf Viktors
  Entscheidung, wie für `v0.7.0` am 25.11.) schreibt die Zahl der roten
  WebGUI-Tests ins Bau-Protokoll und baut weiter; alle anderen Prüfungen
  bleiben streng. `verify --version X.Y.Z` prüft `releases/latest` genau so, wie es ein
  installiertes FlowShift tut. Ablauf
  und Checkliste: [docs/setup.md](docs/setup.md) „Publishing a new version for
  all PCs“.
- Jede Änderung an dem, was installiert wird (Dateien, Abhängigkeiten,
  Autostart, Registry, Ports, WebGUI, Daten unter `%ProgramData%\FlowShift`),
  zieht Installer, Updater und Uninstaller im selben Schritt nach; die Doku
  wird immer mitgepflegt. Die Regeln dazu stehen in [AGENTS.md](AGENTS.md).

## Lizenz

MIT

