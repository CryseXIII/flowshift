# Setup Guide

*(folgt mit erstem Release)*

## Voraussetzungen

- Windows 10/11, Linux (Wayland/X11) oder Android 10+
- Gleiches lokales Netzwerk (Ethernet bevorzugt für Video)
- Optional: NVIDIA GPU (NVENC), AMD GPU (AMF), Intel iGPU (QSV) für hardwarebeschleunigtes Encoding

## Installation

### Windows
1. Lade das neueste Release von [GitHub Releases]() herunter
2. `flowshift-service.exe` als Admin installieren (wird als Windows Service registriert)
3. `flowshift-gui.exe` starten → erscheint im Tray
4. `flowshift-viewer.exe` wird automatisch pro Monitor gestartet

### Linux
```bash
# via package manager (bald)
sudo apt install flowshift

# oder manuell
tar xzf flowshift-linux-x64.tar.gz
./install.sh
systemctl --user enable flowshift
systemctl --user start flowshift
```

### Android
APK von [GitHub Releases]() installieren.

## Erstkonfiguration

1. FlowShift auf **allen Geräten** starten
2. GUI öffnen → Geräte werden automatisch erkannt
3. Profile anlegen: Hotkey + Routing + Input-Target festlegen
4. Profile testen

## Fehlerbehebung

| Problem | Lösung |
|---|---|
| Gerät wird nicht gefunden | Firewall prüfen (Port 5353, 45781, 49000-50000) |
| Hohe Latenz | Auf Ethernet wechseln, Bitrate reduzieren |
| Kein Video | GPU-Treiber aktualisieren, Hardware-Encoding prüfen |
