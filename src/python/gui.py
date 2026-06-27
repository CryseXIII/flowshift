"""FlowShift GUI – vollständige Konfiguration per Mausklick"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk, simpledialog

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
SERVICE_FILE = os.path.join(os.path.dirname(__file__), "service.py")


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {
        "device_name": os.environ.get("COMPUTERNAME", "Unbekannt"),
        "device_id": "",
        "port": 45781,
        "peers": [],
    }


def save_config(cfg: dict) -> None:
    if not cfg.get("device_id"):
        cfg["device_id"] = __import__("uuid").uuid4().hex[:8]
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class PeerScanner:
    """Scannt lokales Netz nach FlowShift-Peers (Port 45781)."""

    def __init__(self, callback):
        self.callback = callback
        self._stop = False

    def stop(self):
        self._stop = True

    def scan(self, base_ip: str, timeout: float = 2.0):
        parts = base_ip.rsplit(".", 1)
        if len(parts) != 2:
            return
        subnet = parts[0]
        found = []

        def try_host(host: str, name: str):
            if self._stop:
                return
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect((host, 45781))
                data = json.dumps({"type": "ping"}).encode()
                s.sendall(struct.pack("!I", len(data)) + data)
                s.close()
                found.append({"name": name, "host": host, "port": 45781})
            except Exception:
                pass

        threads = []
        for i in range(1, 255):
            host = f"{subnet}.{i}"
            t = threading.Thread(target=try_host, args=(host, host), daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=timeout + 1)

        if not self._stop:
            self.callback(found)


class FlowShiftGUI:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.service_proc: subprocess.Popen | None = None
        self.scanner: PeerScanner | None = None

        self.root = tk.Tk()
        self.root.title("FlowShift")
        self.root.geometry("720x560")
        self.root.minsize(600, 400)
        style = ttk.Style()
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")

        self._build_ui()
        self._refresh()
        self._check_first_run()

    # ── UI Aufbau ──────────────────────────────────────────────
    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=4)

        # ── TAB 1: Geräte ──────────────────────────────────────
        dev = ttk.Frame(nb)
        nb.add(dev, text="Geräte", padding=8)

        f1 = ttk.LabelFrame(dev, text="Dieses Gerät", padding=8)
        f1.pack(fill="x", pady=(0, 8))

        row = ttk.Frame(f1)
        row.pack(fill="x")
        ttk.Label(row, text="Name:").pack(side="left")
        self.name_var = tk.StringVar(value=self.cfg.get("device_name", ""))
        ttk.Entry(row, textvariable=self.name_var, width=28).pack(
            side="left", padx=4
        )
        ttk.Label(row, text="Port:").pack(side="left", padx=(12, 0))
        self.port_var = tk.StringVar(value=str(self.cfg.get("port", 45781)))
        ttk.Entry(row, textvariable=self.port_var, width=6).pack(side="left", padx=4)
        ttk.Button(row, text="Speichern", command=self._save_device).pack(
            side="left", padx=8
        )

        ip_label = ttk.Label(
            f1,
            text=f"Eigene IP: {get_local_ip()} – auf anderen Geräten hier angeben",
            foreground="gray",
        )
        ip_label.pack(anchor="w", pady=(4, 0))

        # ── Peers ──────────────────────────────────────────────
        f2 = ttk.LabelFrame(dev, text="Andere Geräte (Peers)", padding=8)
        f2.pack(fill="both", expand=True)

        cols = ("Name", "Adresse", "Port", "Status")
        self.peer_tree = ttk.Treeview(
            f2, columns=cols, show="headings", height=6
        )
        for c in cols:
            self.peer_tree.heading(c, text=c)
            self.peer_tree.column(c, width=140)
        self.peer_tree.pack(fill="both", expand=True, pady=(0, 6))

        btn_row = ttk.Frame(f2)
        btn_row.pack(fill="x")

        ttk.Button(btn_row, text="+ Hinzufügen", command=self._add_peer).pack(
            side="left", padx=2
        )
        ttk.Button(btn_row, text="Bearbeiten", command=self._edit_peer).pack(
            side="left", padx=2
        )
        ttk.Button(btn_row, text="Entfernen", command=self._remove_peer).pack(
            side="left", padx=2
        )
        self.scan_btn = ttk.Button(
            btn_row, text="🔍 Netzwerk scannen", command=self._scan_network
        )
        self.scan_btn.pack(side="right", padx=2)

        # ── TAB 2: Steuerung ───────────────────────────────────
        ctrl = ttk.Frame(nb)
        nb.add(ctrl, text="Steuerung", padding=8)

        self.status_label = ttk.Label(
            ctrl, text="⚫ Gestoppt", font=("", 14, "bold")
        )
        self.status_label.pack(pady=8)

        self.btn_start = ttk.Button(
            ctrl, text="▶ Service starten", command=self._toggle_service
        )
        self.btn_start.pack(pady=4)

        self.active_label = ttk.Label(ctrl, text="", font=("", 11))
        self.active_label.pack(pady=4)

        info = ttk.LabelFrame(ctrl, text="Hotkeys", padding=8)
        info.pack(fill="x", pady=8)
        ttk.Label(
            info,
            text=(
                "Ctrl + Alt + 1 … 9  →  Eingaben zu Peer 1…9 leiten\n"
                "Ctrl + Alt + 0       →  zurück zu lokalem Eingabegerät"
            ),
            justify="left",
        ).pack(anchor="w")

        log_lf = ttk.LabelFrame(ctrl, text="Log", padding=4)
        log_lf.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text = tk.Text(
            log_lf, height=8, state="disabled", bg="#1e1e1e", fg="#d4d4d4",
            font=("Consolas", 9)
        )
        self.log_text.pack(fill="both", expand=True)

        # ── TAB 3: Info ─────────────────────────────────────────
        info_tab = ttk.Frame(nb)
        nb.add(info_tab, text="Info", padding=16)

        ttk.Label(info_tab, text="FlowShift v0.1.0", font=("", 16, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            info_tab,
            text=(
                "Software-KVM für mehrere Geräte.\n\n"
                "So funktioniert's:\n"
                "1. Auf jedem Gerät die gleiche config.json verwenden\n"
                "2. Service auf allen Geräten starten\n"
                "3. Mit Hotkey umschalten, welches Gerät gesteuert wird\n\n"
                "Alle Geräte müssen im selben Netzwerk sein.\n"
                "Firewall muss TCP-Port 45781 erlauben."
            ),
            justify="left",
        ).pack(anchor="w", pady=8)

    # ── First-Run Wizard ───────────────────────────────────────
    def _check_first_run(self):
        if not self.cfg.get("peers") and not os.environ.get("FLOWSHIFT_SKIP_WIZARD"):
            self.root.after(300, self._show_wizard)

    def _show_wizard(self):
        wiz = tk.Toplevel(self.root)
        wiz.title("FlowShift – Ersteinrichtung")
        wiz.geometry("500x400")
        wiz.transient(self.root)
        wiz.grab_set()
        wiz.resizable(False, False)

        frame = ttk.Frame(wiz, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame, text="Willkommen bei FlowShift!",
            font=("", 14, "bold")
        ).pack(anchor="w")

        ttk.Label(
            frame, text=(
                "Wir richten kurz dein Gerät ein.\n"
                "Was ist das für ein Gerät?"
            ),
            justify="left",
        ).pack(anchor="w", pady=8)

        ttk.Label(frame, text="Gerätename:").pack(anchor="w")
        name_var = tk.StringVar(value=self.name_var.get())
        ttk.Entry(frame, textvariable=name_var, width=40).pack(fill="x", pady=(0, 12))

        ttk.Label(
            frame,
            text=(
                "Nach dem Speichern kannst du auf dem Tab 'Geräte'\n"
                "deine anderen Geräte hinzufügen.\n\n"
                "Tipp: Starte danach den Service unter 'Steuerung'."
            ),
            justify="left",
            foreground="gray",
        ).pack(anchor="w")

        def on_done():
            self.name_var.set(name_var.get().strip())
            self._save_device()
            wiz.destroy()

        ttk.Button(frame, text="Fertig", command=on_done).pack(pady=16)

    # ── Aktionen ────────────────────────────────────────────────
    def _save_device(self):
        self.cfg["device_name"] = self.name_var.get().strip() or "Unbekannt"
        try:
            self.cfg["port"] = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("Fehler", "Port muss eine Zahl sein")
            return
        save_config(self.cfg)
        self._log("Geräte-Einstellungen gespeichert")

    def _refresh(self):
        for row in self.peer_tree.get_children():
            self.peer_tree.delete(row)
        for p in self.cfg.get("peers", []):
            self.peer_tree.insert(
                "", "end",
                values=(p["name"], p["host"], p.get("port", 45781), ""),
            )
        self._update_status()

    def _add_peer(self):
        dlg = PeerForm(self.root, "Peer hinzufügen")
        self.root.wait_window(dlg)
        if dlg.result:
            self.cfg.setdefault("peers", []).append(dlg.result)
            save_config(self.cfg)
            self._refresh()
            self._log(f"Peer hinzugefügt: {dlg.result['name']}")

    def _edit_peer(self):
        sel = self.peer_tree.selection()
        if not sel:
            messagebox.showinfo("Hinweis", "Bitte zuerst einen Peer auswählen")
            return
        idx = self.peer_tree.index(sel[0])
        peer = self.cfg["peers"][idx]
        dlg = PeerForm(self.root, "Peer bearbeiten", defaults=peer)
        self.root.wait_window(dlg)
        if dlg.result:
            self.cfg["peers"][idx] = dlg.result
            save_config(self.cfg)
            self._refresh()
            self._log(f"Peer aktualisiert: {dlg.result['name']}")

    def _remove_peer(self):
        sel = self.peer_tree.selection()
        if not sel:
            return
        idx = self.peer_tree.index(sel[0])
        name = self.cfg["peers"][idx]["name"]
        if messagebox.askyesno("Entfernen", f"{name} wirklich entfernen?"):
            del self.cfg["peers"][idx]
            save_config(self.cfg)
            self._refresh()
            self._log(f"Peer entfernt: {name}")

    def _scan_network(self):
        self.scan_btn.config(state="disabled", text="🔍 Scanne...")
        self._log("Scanne Netzwerk nach FlowShift-Geräten...")

        def done(found):
            self.scan_btn.config(state="normal", text="🔍 Netzwerk scannen")
            if not found:
                self._log("Keine weiteren Geräte gefunden")
                messagebox.showinfo(
                    "Scan abgeschlossen",
                    "Keine weiteren FlowShift-Geräte gefunden.\n"
                    "Stelle sicher, dass der Service auf dem anderen Gerät läuft."
                )
                return
            for p in found:
                existing = any(
                    e["host"] == p["host"] for e in self.cfg.get("peers", [])
                )
                if not existing:
                    self.cfg.setdefault("peers", []).append(p)
                    self._log(f"Gefunden: {p['host']}")
            save_config(self.cfg)
            self._refresh()
            self._log(f"{len(found)} Gerät(e) gefunden und hinzugefügt")
            messagebox.showinfo(
                "Scan abgeschlossen",
                f"{len(found)} Gerät(e) gefunden und zur Liste hinzugefügt.",
            )

        self.scanner = PeerScanner(done)
        base = get_local_ip()
        threading.Thread(
            target=self.scanner.scan, args=(base, 2.0), daemon=True
        ).start()

    def _toggle_service(self):
        if self.service_proc is not None:
            self.service_proc.terminate()
            self.service_proc = None
            self._update_status()
            self.btn_start.config(text="▶ Service starten")
            self._log("Service gestoppt")
            return

        # Config speichern vor Start
        self._save_device()

        if not self._elevate_as_admin():
            return

        try:
            self.service_proc = subprocess.Popen(
                [sys.executable, SERVICE_FILE],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self._update_status()
            self.btn_start.config(text="⏹ Service stoppen")
            self._log("Service gestartet")

            def reader():
                for line in self.service_proc.stdout or []:
                    self._log(line.rstrip())

            threading.Thread(target=reader, daemon=True).start()
        except Exception as e:
            messagebox.showerror("Fehler", str(e))

    def _elevate_as_admin(self) -> bool:
        import ctypes
        try:
            is_admin = ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            is_admin = False

        if is_admin:
            return True

        msg = (
            "Der Service muss als Administrator laufen.\n"
            "FlowShift jetzt neu starten mit Admin-Rechten?"
        )
        if not messagebox.askyesno("Admin-Rechte benötigt", msg):
            self._log("Service-Start abgebrochen – Admin-Rechte fehlen")
            return False

        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{SERVICE_FILE}"', None, 1
        )
        self._log("Service als Administrator gestartet")
        messagebox.showinfo(
            "Info",
            "Der Service läuft jetzt in einem eigenen Admin-Fenster.\n"
            "Du kannst es minimieren, aber nicht schliessen.",
        )
        return True

    def _update_status(self):
        if self.service_proc and self.service_proc.poll() is None:
            self.status_label.config(text="🟢 Läuft", foreground="green")
            self.active_label.config(
                text="Drücke Ctrl+Alt+1…9 zum Umschalten"
            )
        else:
            self.status_label.config(text="⚫ Gestoppt", foreground="black")
            self.active_label.config(text="")

    def _log(self, msg: str):
        self.root.after(0, lambda: self._do_log(msg))

    def _do_log(self, msg: str):
        self.log_text.config(state="normal")
        stamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{stamp}] {msg}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        if self.service_proc is not None:
            self.service_proc.terminate()
        self.root.destroy()


class PeerForm(tk.Toplevel):
    def __init__(self, parent, title, defaults=None):
        super().__init__(parent)
        self.title(title)
        self.result = None
        self.geometry("340x180")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="Anzeigename:").grid(row=0, column=0, sticky="w", pady=2)
        self.name_var = tk.StringVar(value=(defaults or {}).get("name", ""))
        ttk.Entry(f, textvariable=self.name_var, width=30).grid(
            row=0, column=1, pady=2, padx=(4, 0)
        )

        ttk.Label(f, text="IP-Adresse:").grid(row=1, column=0, sticky="w", pady=2)
        self.host_var = tk.StringVar(value=(defaults or {}).get("host", ""))
        ttk.Entry(f, textvariable=self.host_var, width=30).grid(
            row=1, column=1, pady=2, padx=(4, 0)
        )

        ttk.Label(f, text="Port:").grid(row=2, column=0, sticky="w", pady=2)
        self.port_var = tk.StringVar(
            value=str((defaults or {}).get("port", 45781))
        )
        ttk.Entry(f, textvariable=self.port_var, width=8).grid(
            row=2, column=1, sticky="w", pady=2, padx=(4, 0)
        )

        btn_f = ttk.Frame(f)
        btn_f.grid(row=3, column=0, columnspan=2, pady=(16, 0))
        ttk.Button(btn_f, text="OK", command=self._ok).pack(side="left", padx=4)
        ttk.Button(btn_f, text="Abbrechen", command=self.destroy).pack(
            side="left", padx=4
        )

    def _ok(self):
        name = self.name_var.get().strip()
        host = self.host_var.get().strip()
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("Fehler", "Port muss eine Zahl sein", parent=self)
            return
        if not name or not host:
            messagebox.showerror(
                "Fehler", "Name und IP-Adresse sind Pflicht", parent=self
            )
            return
        self.result = {"name": name, "host": host, "port": port}
        self.destroy()


if __name__ == "__main__":
    FlowShiftGUI().run()
