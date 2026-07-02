# FlowShift Handoff

## Kurzstand
- Projekt: LAN Mouse/Keyboard Forwarding zwischen Laptop und Surface
- Status: laufender Stand ist inkonsistent zwischen GUI, Tray und Dienst
- Hauptproblem: Klick auf "Service stoppen" meldet teils "FlowShift läuft bereits"
- Beobachtung: mehrere Runtime-Pfade und Einzelinstanz-Logik greifen aktuell gegeneinander

## Relevante Source-Dateien
- `src/python/gui.py`
- `src/python/tray.py`
- `src/python/service.py`
- `src/python/test_service.py`
- `src/python/e2e_test.py`

## Aktuelle Runtime-/Verbindungslogik
- GUI startet `tray.py --tray`
- Tray übernimmt Discovery, Forwarding, Local Control, Ping/Pong und Exit
- `service.py` ist jetzt ein Legacy-Wrapper auf `tray.py`
- Es gibt eine Runtime-Singleton-Idee per Mutex für den produktiven Runtime-Pfad

## Aktuelles Fehlerbild
- Beim Klick auf `Service stoppen` wurde die Runtime-Logik zuletzt fälschlich als "bereits laufend" behandelt
- Das deutet darauf hin, dass die GUI den Runtime-Zustand über `status`/Control-Socket statt über den echten `service_proc`-Lebenszyklus bewertet
- Ziel ist jetzt: `shutdown` über den Control-Socket, nicht zweiter Startversuch

## Gewünschtes Verhalten
- Nur eine FlowShift-Instanz darf laufen
- `Exit` im Tray beendet alle FlowShift-Prozesse und Nachfolger sauber
- Ping/Pong pro Profil soll per UI auslösbar sein
- Jeder wichtige Zustand soll geloggt werden:
  - Profil aktiviert/deaktiviert
  - Verbindung aufgebaut/getrennt
  - Ping gesendet/empfangen
  - Pong gesendet/empfangen
  - Input-Events empfangen/injiziert

## Wichtig für die nächste Bearbeitung
- Vor Änderungen prüfen, welcher Entry-Point wirklich die produktive Runtime ist
- `tray.py` ist der zentrale Pfad
- `service.py` ist nur noch ein Wrapper und soll nicht mehr als eigenständiger Runtime-Pfad behandelt werden
- GUI sollte den echten Runtime-Zustand aus dem Control-Socket spiegeln, nicht nur lokale Prozessobjekte

## Offene Punkte
- Stop-Button semantisch sauber machen
- Single-instance wirklich systemweit erzwingen
- Exit soll alle FlowShift-Prozesse beenden
- Konflikt zwischen `gui.py` und `tray.py`/`service.py` auflösen
