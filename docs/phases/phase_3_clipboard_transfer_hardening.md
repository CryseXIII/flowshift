# Phase 3 – Clipboard Transfer Hardening

## 1. Ziel

Phase 3 ersetzt den bisherigen standardmäßigen ZIP-/Base64-Dateitransfer
zwischen modernen FlowShift-Peers durch einen direkten, persistent
fortsetzbaren und ressourcenschonenden Streaming-Transfer.

Gleichzeitig wird die Entwicklungs- und Release-Toolchain auf die jeweils
neuesten stabilen und unterstützten Versionen modernisiert.

Zielrelease:

`v0.6.0`

Produktiver Runtime-Einstieg:

`src/python/tray.py --tray`

Phase 3 gilt erst als abgeschlossen, wenn `v0.6.0` veröffentlicht wurde, der
tag-triggered GitHub-Actions-Workflow erfolgreich ist und alle Release-Assets
verifiziert wurden.

## 2. Nicht Bestandteil dieser Phase

Nicht implementieren:

- React Clipboard View
- endgültige Win+V-Overlay
- Command Wheel
- Action Registry
- Shell-Action-UI
- endgültige React-Transferverwaltung

Diese Funktionen folgen ab Phase 4.

Status- und Diagnose-APIs dürfen vorbereitet werden. Keine endgültige
Benutzeroberfläche bauen.

## 3. Toolchain- und Dependency-Modernisierung

Vor dem eigentlichen Transferumbau die verwendete Toolchain prüfen und
modernisieren.

Verbindliche Regeln:

- Nur stabile Releases verwenden.
- Keine Alpha-, Beta-, RC-, Nightly- oder Canary-Versionen.
- Produktionsbuilds verwenden die neueste aktive Node.js-LTS-Version.
- Zusätzlich einen Kompatibilitätstest mit der neuesten stabilen
  Node.js-Current-Version ausführen.
- CI verwendet die neueste stabile unterstützte CPython-Version.
- GitHub Actions auf aktuelle stabile Versionen beziehungsweise unveränderliche
  Commit-SHAs aktualisieren.
- Exakte getestete Dependency-Versionen locken.
- Keine Floating Dependencies im Stable Release.
- Der normale Endnutzer-Installer benötigt kein Node.js.
- Node.js wird nur für Entwicklung, Source-Builds und CI benötigt.
- Eine vorhandene kompatible Benutzer-Python-Installation nicht ungefragt
  ersetzen oder entfernen.
- Installer entfernt nur Komponenten, deren Installation FlowShift selbst
  dokumentiert besitzt.

Mindestens prüfen:

- `.github/workflows/*.yml`
- `requirements*.txt`
- Python-Lockdateien
- `webgui/package.json`
- `webgui/package-lock.json`
- PowerShell-Installer
- Packaging-Skripte
- Inno-Setup-Konfiguration
- Update- und Release-Workflow

Mindestens aktualisieren und testen:

- Python
- Pillow
- pywebview
- Node.js
- React
- React DOM
- Vite
- Vitest
- jsdom
- Testing Library
- GitHub Actions

Einrichten oder aktualisieren:

- `.github/dependabot.yml`
- Updates für GitHub Actions
- npm-Updates
- Python-Dependency-Updates
- regelmäßiger Dependency-/Security-Audit

Erstellen:

`docs/toolchain_policy.md`

Die Datei dokumentiert:

- Produktionsversionen
- unterstützte Versionen
- LTS-/Current-Strategie
- Lockstrategie
- Dependency-Update-Strategie
- Installer-Verhalten
- Reproduzierbarkeit des Releases

## 4. Ausgangsarchitektur analysieren

Vor dem Umbau den vollständigen produktiven Pfad verfolgen:

Clipboard-Auswahl  
→ Provider-Auswahl  
→ Preflight  
→ Transferstrategie  
→ Datenübertragung  
→ Verifikation  
→ Cache  
→ Materialisierung  
→ Windows-Clipboard  
→ Cleanup

Mindestens untersuchen:

- `clipboard_transfer.py`
- `clipboard_files.py`
- `clipboard_sources.py`
- `clipboard_protocol.py`
- `clipboard_runtime.py`
- `clipboard_store.py`
- `clipboard_model.py`
- `clipboard_win.py`
- Peer-Framing und Netzwerktransport
- Disconnect-/Reconnect-Pfade
- Transfer- und Clipboard-Tests

Dokumentieren:

- wo derzeit ZIP-Dateien erzeugt werden;
- wo Base64 verwendet wird;
- wo Dateien mehrfach vollständig gelesen werden;
- wo dieselben Daten mehrfach vollständig geschrieben werden;
- welche Transferzustände nur im RAM existieren;
- was nach Disconnect oder Prozessneustart verloren geht;
- welche Queues oder Puffer unbounded sind;
- welche Pfade bereits durch Preflight, Cache und Provider-Semantik geschützt
  werden.

Erstellen:

`docs/clipboard_transfer_v2.md`

Die Datei ist Entwurf und laufende Architekturdokumentation. Noch nicht
implementierte Funktionen klar als geplant kennzeichnen.

## 5. Capability Negotiation

Eine explizite Fähigkeit einführen:

`clipboard_stream_v2`

Verhalten:

- Unterstützen beide Peers V2, ist Stream V2 der Standard.
- Unterstützt nur ein Peer V2, wird der bestehende Legacy-Transfer verwendet.
- V1- und V2-Nachrichten nicht still vermischen.
- Gewählte Strategie in Status und Logs ausgeben.

Strategien mindestens:

- `stream_v2`
- `legacy_zip_v1`

Der Legacy-Pfad bleibt größenbegrenzt, Preflight-geschützt und getestet.

## 6. Metadata-first Datei-Capture

Ein normaler Explorer-Copy darf große Dateien nicht vollständig lesen, nur um
sie in der Clipboard-History anzuzeigen.

Beim Capture zunächst nur schnelle Metadaten erfassen:

- Item-ID
- lokaler Source-Verweis
- Name
- relative Struktur
- Entry-Typ
- Größe
- Dateianzahl
- Gesamtgröße
- `mtime_ns`
- lokale File Identity, soweit verfügbar
- Source Availability
- Hashstatus

Keine vollständige SHA-256-Berechnung beim bloßen Copy-Ereignis.

Hashzustände mindestens logisch:

- `unhashed`
- `hashing`
- `verified`
- `changed`
- `invalid`

Item-ID und Content-Hash bleiben getrennt.

Bestehende bereits gehashte Items bleiben gültig. Schemaänderungen
versionieren und migrieren.

## 7. Batch-Manifest

Dateien und Verzeichnisse über ein versioniertes Manifest beschreiben.

Manifest mindestens:

- Schema- und Protokollversion
- Item-ID
- Item-Revision
- Manifest-Digest
- Gesamtgröße
- Dateianzahl
- stabile Entry-Indizes
- relative Pfade
- Entry-Typ
- Größe
- optionale Zeitmetadaten
- Source Fingerprint
- Hashstatus
- finaler SHA-256, sobald bekannt

Unterstützte Entry-Typen zunächst:

- Datei
- Verzeichnis

Symlinks, Junctions und sonstige Reparse Points standardmäßig nicht verfolgen.

Sie müssen erkannt und sicher abgelehnt oder ausdrücklich übersprungen werden.

Keine unkontrollierte Rekursion.

## 8. Pfadsicherheit

Alle Remote-Pfade als nicht vertrauenswürdig behandeln.

Ablehnen:

- absolute Pfade
- UNC-Pfade
- Laufwerksbuchstaben
- `..`
- Null-Bytes
- Alternate Data Streams
- reservierte Windows-Gerätenamen
- leere unerlaubte Komponenten
- Komponenten mit ungültigen Endungen
- Pfade außerhalb des Staging Roots
- unvertretbare Pfadlängen
- unerlaubte Reparse Points
- Windows-Case-Kollisionen wie `File.txt` und `file.txt`

Keine stillen Renames ohne dokumentierte Policy.

Zentrale Limits definieren für:

- Dateianzahl
- Verzeichnisanzahl
- Manifestgröße
- Pfadlänge
- Komponentenlänge
- Gesamtgröße
- Einzeldateigröße
- Framegröße

## 9. Transfer-Session

Jeder V2-Transfer erhält eine stabile `transfer_id`.

Eine Session enthält mindestens:

- Transfer-ID
- Item-ID
- Item-Revision
- Profil
- Provider
- Empfänger
- Strategie
- Manifest-Digest
- logische Gesamtgröße
- noch zu übertragende Größe
- Dateianzahl
- Erstellungszeit
- Zustand
- Retry-Zähler
- Progress
- Resume-Zustand

Zustände mindestens logisch:

- `created`
- `preflight`
- `accepted`
- `sending_manifest`
- `transferring`
- `paused`
- `waiting_reconnect`
- `verifying`
- `finalizing`
- `completed`
- `cancelled`
- `failed`

Session-Zustand thread-safe und persistent abbilden.

Keine lose Ansammlung unverbundener Dictionaries als einzige Zustandsquelle.

## 10. Binäres Framing

V2-Payload nicht als Base64 in JSON übertragen.

Kontrollnachrichten dürfen strukturiertes JSON bleiben.

Payload wird über eindeutig typisierte Binärframes übertragen.

Ein Binärframe enthält mindestens:

- Frame-Typ
- Protokollversion
- Transfer-ID
- Dateiindex
- Offset
- Payloadlänge
- Flags
- optionale Chunk-Prüfsumme
- rohe Payloadbytes

Framing muss eindeutig unterscheiden:

- JSON-Control-Frame
- Binary-Clipboard-Payload-Frame

Keine heuristische Erkennung.

Testen:

- fragmentierte Socket-Reads
- mehrere Frames in einem Read
- ungültige Framegrößen
- negative oder übergroße Offsets
- falsche Transfer-IDs
- unbekannte Major-Versionen
- malformed Frames

Ungültige Frames dürfen keinen Netzwerk- oder Runtime-Thread crashen.

## 11. Direkter Stream ohne Standard-ZIP

Zwischen V2-Peers:

- kein vollständiges ZIP vor dem Transfer;
- kein Base64-Payload;
- keine vollständige Datei im RAM;
- kein zweiter vollständiger Read nur für Vorab-Hashing.

Standardpfad:

Quelldatei sequenziell lesen  
→ Sender-Hash aktualisieren  
→ rohe Chunks übertragen  
→ Empfänger sequenziell in Staging schreiben  
→ Empfänger-Hash aktualisieren  
→ Größe und Hash prüfen  
→ atomar finalisieren

ZIP bleibt nur Legacy-Fallback.

## 12. Source Fingerprint und Hashing

Vor dem Transfer einen Source Snapshot erfassen:

- Größe
- `mtime_ns`
- File Identity
- Volume/File Index, soweit verfügbar

Nach dem Transfer erneut prüfen.

Wurde die Quelle während des Transfers geändert:

- Transfer nicht als erfolgreich markieren;
- Partial nicht als gültigen Payload behandeln;
- Item als geändert oder stale markieren;
- Resume mit dem alten Fingerprint ablehnen.

Hash während des eigentlichen Streams berechnen.

Einen bestehenden finalen Hash nur wiederverwenden, wenn der Source Fingerprint
unverändert ist.

Nach erstem erfolgreichen vollständigen Hash:

- Item-Metadaten atomar aktualisieren;
- Revision erhöhen;
- endgültige Content Identity speichern;
- Peers über die neue Revision informieren;
- keine doppelte History-Zeile erzeugen.

## 13. Chunking, Flow Control und Backpressure

Konfigurierbare Chunkgröße verwenden, technisch begründet beispielsweise im
Bereich 1–4 MiB.

Begrenzen:

- globale gleichzeitige Transfers
- Transfers pro Peer
- In-flight Chunks
- In-flight Bytes
- Send Queue
- Receive Queue

Sender darf nicht schneller unbegrenzt puffern, als Netzwerk oder Empfänger
schreiben können.

Langsame Zielplatte muss kontrolliertes Backpressure erzeugen.

Keine Memory Explosion.

## 14. ACK-Strategie

Kumulative oder gebatchte ACKs verwenden.

ACK mindestens:

- Transfer-ID
- Dateiindex
- höchster zusammenhängend verifizierter Offset
- optional fehlende Bereiche
- Receiver-Zustand

ACK auslösen nach:

- definierter Byte-Menge
- definierter Chunkanzahl
- maximalem Zeitintervall
- Dateiabschluss

Sender hält nur bounded unbestätigte Daten.

Timeouts müssen in Tests verkürzbar sein.

## 15. Receiver Staging und atomare Finalisierung

Eingehende Transfers unter einem dedizierten FlowShift-Staging-Pfad speichern.

Beispiel:

`%ProgramData%\FlowShift\clipboard\temp\incoming\<transfer_id>\`

Unvollständige Dateien zunächst als `.part`.

Keine direkte Sichtbarkeit unter finalem Namen.

Nicht nach jedem kleinen Chunk `fsync` ausführen.

Checkpoints gebündelt persistieren.

Eine Datei gilt erst als vollständig, wenn:

- erwartete Größe erreicht;
- Sender- und Receiver-Hash übereinstimmen;
- Source Fingerprint gültig;
- Daten geflusht;
- Journal aktualisiert;
- atomare Finalisierung erfolgreich.

Disk Full, Schreibfehler, Flush-Fehler und Rename-Fehler sauber behandeln.

## 16. Persistentes Resume-Journal

Resume darf nicht nur im RAM existieren.

Pro Transfer ein versioniertes Journal speichern.

Mindestens:

- Schema-Version
- Transfer-ID
- Item-ID
- Item-Revision
- Manifest-Digest
- Provider
- Strategie
- Dateieinträge
- abgeschlossene Dateien
- höchster bestätigter Offset je Datei
- verifizierte Bytes
- erwartete Größen
- Fingerprints
- Zeitstempel
- Zustand

Journal atomar schreiben.

Beschädigte oder veraltete Journals dürfen die Runtime nicht crashen.

## 17. Resume

Bei Disconnect:

- Transfer pausieren;
- Zustand persistieren;
- Partial-Dateien behalten;
- In-flight Buffer freigeben;
- Transfer nicht als erfolgreich markieren.

Bei Reconnect:

1. stabile Peer-Identity prüfen;
2. Manifest-Digest vergleichen;
3. Source Fingerprint vergleichen;
4. Receiver-Resume-State übertragen;
5. ab bestätigtem Offset fortsetzen.

Bereits sicher verifizierte Bytes nicht erneut übertragen.

Resume muss ebenfalls nach Runtime-Neustart funktionieren:

- Sender-Neustart
- Empfänger-Neustart
- beide Seiten neu gestartet

Bei beschädigtem Partial oder Journal:

- sicher revalidieren oder neu beginnen;
- niemals unvollständige Daten als vollständig akzeptieren.

## 18. Preflight V2

Bestehenden Preflight strategiebewusst erweitern.

Für Stream V2 berücksichtigen:

- verbleibende Staging-/Payloadbytes
- kleinen Journal-/Metadaten-Overhead
- Safety Margin
- bereits sicher vorhandene Resume-Bytes

Nicht automatisch ZIP plus entpackte Zweitkopie rechnen.

Für Legacy ZIP weiterhin den realen Worst Case berücksichtigen.

Kein Payload-Chunk vor bestätigtem Preflight.

Disk Full während des Transfers trotz erfolgreichem Preflight separat
behandeln.

## 19. Content-addressed Store und Cache

Verifizierte Dateien bevorzugt als einzelne content-addressed Objekte
speichern.

Ein Batch-Manifest referenziert diese Objekte und bewahrt Namen und
Verzeichnisstruktur.

Gleicher physischer Inhalt wird nicht unnötig mehrfach gespeichert.

Provider erst als verfügbar markieren, nachdem der Payload vollständig
verifiziert und tatsächlich lieferbar ist.

Partial-Dateien und bloße Journals machen das Gerät nicht zum Provider.

Cache-Eviction aktualisiert Providerstatus korrekt.

Bestehende ZIP-Batches bleiben über Legacy-/Lazy-Migration nutzbar.

Keine vollständige Zwangsmigration aller Benutzerdaten beim Update.

## 20. Materialisierung

Für Windows `CF_HDROP` eine stabile benannte Verzeichnisstruktur
materialisieren.

Bevorzugt:

- NTFS-Hardlinks aus dem Object Store, wenn auf demselben Volume möglich;
- sonst sicherer Copy-Fallback.

Testen:

- Hardlink erfolgreich
- anderes Volume
- unsupported Filesystem
- fehlende Berechtigung
- Copy-Fallback

Materialization Lease bleibt bestehen, solange Windows Clipboard die Pfade
benötigt.

Bei deaktiviertem Received Cache darf direkt in eine Lease-gebundene
Materialisierung geschrieben und nach Lease-Ende sicher entfernt werden.

Kein verstecktes persistentes ZIP zurücklassen.

## 21. Cancellation und Timeouts

Cancel muss in jeder Phase funktionieren:

- Preflight
- Manifest
- Transfer
- Verification
- Finalization

Cancel:

- Sender stoppen;
- Empfänger informieren;
- Buffer freigeben;
- Worker beenden;
- Journal konsistent auf resumable oder cancelled setzen;
- Cleanup-Policy anwenden.

Timeouts mindestens:

- Preflight
- Manifest ACK
- Window ACK
- No Progress
- Reconnect Wait
- Final Complete ACK

Keine unbegrenzten Waits.

## 22. Progress und Diagnose

Bestehende Status-API erweitern.

Keine endgültige React-UI bauen.

Status mindestens:

- Transfer-ID
- Item-ID
- Strategie
- Zustand
- aktuelle Datei
- Dateiindex
- Dateianzahl
- aktuelle Dateibytes
- Gesamtbytes
- Prozent
- geglättete Übertragungsrate
- ETA
- Resume-Bytes
- Retry-Zähler
- Provider
- Preflight-Zustand
- Fehlercode

Keine absoluten privaten Source-Pfade in normalen APIs oder Logs.

Rate mit gleitendem Fenster oder EWMA berechnen.

Pausen dürfen Rate und ETA nicht verfälschen.

## 23. Update Idle Gate

Auto-Update darf nicht während kritischer Transferzustände installieren:

- Preflight
- Manifest
- Transfer
- Verification
- Finalization
- aktives Resume

Für `paused` und `waiting_reconnect` eine bewusste dokumentierte Policy
festlegen.

Update darf Journals und Partial-Dateien nicht beschädigen.

## 24. Logging

Strukturiert und rate-limited loggen:

- Capability Negotiation
- Strategiewahl
- Manifest
- Preflight
- Resume Journal
- Streamstart
- Dateiabschluss
- Pause
- Disconnect
- Resume
- Verifikation
- Source Changed
- Disk Full
- Finalisierung
- Legacy Fallback
- Cleanup

Keine Logs pro kleinem Chunk.

Keine Clipboard-Inhalte.

Keine absoluten privaten Pfade.

## 25. Tests während der Implementierung

Gemäß `AGENTS.md` während einzelner Slices nur fokussierte und angrenzende Tests
ausführen.

Vollständige Regression erst vor dem Stable Release.

Tests müssen konkrete Endzustände prüfen und den produktiven Pfad möglichst
vollständig benutzen.

Ein Test ist unzureichend, wenn er zentrale Semantik nur über schwache
Assertions wie `result is not None` oder `processed > 0` bestätigt.

## 26. Erforderliche Unit-Tests

Mindestens:

- Manifest-Validierung
- Pfad-Traversal
- reservierte Windows-Namen
- Case-Kollisionen
- Reparse Points
- Source Fingerprint
- Source Changed
- Hash während Stream
- Binärframe Encode/Decode
- fragmentierte Frames
- mehrere Frames pro Read
- Frame-Limits
- Flow-Control-Fenster
- ACK-Batching
- Resume-Offset
- Journal-Atomizität
- beschädigtes Journal
- stale Journal
- V2-Preflight
- Legacy-Preflight
- Disk Full
- atomare Finalisierung
- Zero-byte File
- leeres Verzeichnis
- Unicode-Pfade
- Providertransition
- Rate und ETA

## 27. Erforderliche Integrationstests

Zwei über einen instrumentierten In-Memory- oder Loopback-Transport verbundene
produktive Runtime-Komponenten verwenden.

Mindestens:

- einzelne Datei
- 150-Datei-Batch
- verschachtelte Verzeichnisse
- gemischte Dateitypen
- Metadata Announcement
- On-demand-Auswahl
- Provider-Auswahl
- Preflight
- Manifest
- Binärchunks
- Backpressure
- Verifikation
- Object Store
- Materialisierung
- Windows-Clipboard-Adapter-Mock
- Provider Availability
- kein Legacy-ZIP bei V2
- kein Base64-Payload bei V2

## 28. Resume- und Fehler-Tests

Mindestens:

- Disconnect bei 1 %
- Disconnect bei 50 %
- Disconnect kurz vor Abschluss
- Sender-Restart
- Empfänger-Restart
- beide Restarts
- gleicher Peer mit neuem Endpoint
- unveränderte Source → Resume
- geänderte Source → Resume abgelehnt
- beschädigtes Partial
- beschädigtes Journal
- Source Read Error
- Target Write Error
- Disk Full
- Hash Mismatch
- Rename Error
- Send Failure
- Receive Timeout
- Duplicate Frame
- Out-of-order Frame
- stale ACK
- Cancel Storm

Fault Injection muss testbar und produktiv standardmäßig deaktiviert sein.

## 29. Last- und Ressourcen-Tests

Dedizierte Phase-3-Stresstests:

- 10.000 Binary Frames durch Parser
- 1.000 ACK-/Window-Zyklen
- 100 Disconnect-/Resume-Zyklen
- mehrere hundert Dateien
- wiederholte Runtime-Restarts
- paralleles Status-Polling
- langsamer Empfänger
- Queue Pressure
- simulated Disk Full
- malformed Frames
- Cancellation Storms

Prüfen:

- keine Deadlocks
- keine Thread Explosion
- keine Queue Explosion
- keine Zombie Worker
- keine verwaisten `.part`-Dateien ohne Journal

HDD-I/O-Amplification instrumentieren.

Für Payloadgröße `N` im normalen V2-Pfad ungefähr:

- Sender Payload Reads ≈ `N`
- Receiver Payload Writes ≈ `N`

Kein zusätzliches Full-Size-ZIP und kein zusätzlicher Full-Size-Extract-Write.

RAM-Nutzung muss hauptsächlich von Chunkgröße und In-flight Window abhängen,
nicht von der Gesamtdateigröße.

64-Bit-Offets und logische Größen über 4 GiB testen, ohne unnötig riesige
physische Testdateien anzulegen.

## 30. Installer, Packaging und Migration

Upgrade von `v0.5.4` auf `v0.6.0` muss erhalten:

- Konfiguration
- Clipboard-History-Metadaten
- bestehende Cacheobjekte
- bestehende Leases
- Providerstatus
- Updatezustand

Neue Dateien, Lockdateien und Runtime-Abhängigkeiten vollständig in Installer
und Packaging aufnehmen.

Alte ZIP-Batches weiterhin paste-fähig halten oder restart-sicher lazy
migrieren.

Keine Benutzerdaten löschen.

## 31. Manuelle Testmatrix

`MANUAL_TEST_CHECKLIST.md` ergänzen um:

- kleine Datei
- große Datei
- 150-Datei-Batch
- verschachtelte Ordner
- Unicode
- Zero-byte File
- leeres Verzeichnis
- Abbruch während Transfer
- LAN-Trennung bei 10 %, 50 %, 90 %
- Sender-Neustart
- Empfänger-Neustart
- beide Neustarts
- Source während Transfer ändern
- Source während Transfer löschen
- Zielplatte fast voll
- Zielplatte während Transfer voll
- Cache an
- Cache aus
- Providerwechsel
- V2 zu V2
- V2 zu Legacy
- Update während Transfer
- RAM- und HDD-Beobachtung
- Upgrade `v0.5.4` auf `v0.6.0`

## 32. Dokumentation

Mindestens aktualisieren:

- `README.md`
- `TODO_CURRENT.md`
- `HANDOFF_CURRENT.md`
- `MANUAL_TEST_CHECKLIST.md`
- `docs/architecture.md`
- `docs/protocol.md`
- `docs/clipboard.md`
- `docs/clipboard_semantics.md`
- `docs/clipboard_transfer_v2.md`
- `docs/toolchain_policy.md`
- `docs/install_test_checklist.md`

Keine veralteten Toolchain- oder ZIP-Standard-Aussagen stehen lassen.

## 33. Empfohlene Implementierungsslices

Die tatsächliche Aufteilung darf technisch angepasst werden.

Sinnvolle Reihenfolge:

1. Toolchain und Dependencies
2. Transfer-V2-Architekturdokument
3. Capability, Sessionmodell und Manifest
4. Binärframing
5. Bounded Flow Control und ACKs
6. Staging und direkte Dateistreams
7. persistentes Resume
8. Object Store und Materialisierung
9. Provider-, Cache-, Preflight- und Updateintegration
10. Fault Injection und Stresstests
11. Gesamtreview und Dokumentationsabschluss
12. Stable Release

Jeder eigene Commit verwendet gemäß `AGENTS.md` die nächste freie
`0.6.0-dev.N`-Version.

Nach jedem getesteten Slice committen, pushen und selbstständig fortfahren.

## 34. Vollständige Regression vor Release

Vor `v0.6.0` mindestens:

- vollständige Python-Kompilierung
- vollständige Python-Test-Discovery
- alle Phase-2-Tests
- alle Phase-3-Tests
- alle Phase-3-Stresstests
- Worker Smoke
- End-to-End
- Reconnect Stress
- Overlay Stress
- Updater Tests
- PowerShell Parse Checks
- Packaging Tests
- WebGUI Tests
- WebGUI Production Build
- npm Security Audit
- Python Dependency Security Audit
- Node-LTS-Build
- Node-Current-Kompatibilitätstest

Keine Teilmenge als vollständige Regression ausgeben.

## 35. Stable Release

Erst nach vollständig erfüllter Definition of Done:

- `VERSION` auf `0.6.0` setzen;
- Release-Commit erstellen;
- pushen;
- unveränderlichen Tag `v0.6.0` erstellen und pushen;
- GitHub Actions vollständig abwarten.

Danach tatsächlich prüfen:

- Workflow `SUCCESS`
- Release vorhanden
- `FlowShift-Setup.exe` vorhanden
- `update-manifest.json` vorhanden
- `SHA256SUMS.txt` vorhanden
- Manifest-Version `0.6.0`
- Installer-Hash korrekt
- Latest Stable zeigt auf `v0.6.0`
- Update Discovery für `0.6.0` meldet `up_to_date`
- Stable-Updater ignoriert Prereleases

Nach erfolgreichem Release stoppen.

Phase 4 nicht automatisch beginnen.

## 36. Definition of Done

Phase 3 ist nur abgeschlossen, wenn mindestens Folgendes erfüllt ist:

### Toolchain

- aktuelle stabile GitHub Actions;
- keine veraltete Node-20-Action-Runtime;
- Release-Build mit aktueller aktiver Node-LTS;
- Kompatibilitätstest mit Node Current;
- CI mit aktueller stabiler Python-Version;
- Installer ohne hart kodierte Python-3.12-Installation;
- aktuelle gelockte Python- und npm-Abhängigkeiten;
- Dependency-Automatisierung;
- Security-Audits erfolgreich.

### Transfer V2

- Capability Negotiation;
- V2 Standard zwischen V2-Peers;
- Legacy-Fallback;
- metadata-first Capture;
- provisional Hash State;
- Hashing während Stream;
- Source Fingerprint;
- Batch-Manifest;
- vollständige Pfadvalidierung;
- Raw Binary Frames;
- keine Payload-Base64 im V2-Pfad;
- bounded Chunking und In-flight Bytes;
- Backpressure;
- ACK-Batching;
- direkte sequentielle Reads und Writes;
- kein Standard-ZIP;
- persistentes Resume;
- Resume nach Disconnect und Neustart;
- Disk-Full-Behandlung;
- atomare Finalisierung;
- Zero-byte Files;
- leere Verzeichnisse;
- Unicode-Pfade;
- korrekte Provider- und Cacheintegration;
- Materialization Leases;
- Update Idle Gate;
- echter Progress, Rate und ETA.

### Qualität

- Unit-Tests;
- produktive Integrationstests;
- Resume-Tests;
- Fault-Injection-Tests;
- Ressourcen-Tests;
- 10.000-Frame-Stress;
- 100 Resume-Zyklen;
- gesamte Regression erfolgreich;
- TODO enthält nur offene Aufgaben;
- Handoff entspricht dem tatsächlichen Stand;
- Release-Workflow erfolgreich;
- alle Release-Assets vorhanden.

Fehlt ein Punkt, darf Phase 3 nicht als abgeschlossen bezeichnet werden.
