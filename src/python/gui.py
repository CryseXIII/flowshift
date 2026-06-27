"""FlowShift GUI – Hotkey-Editor + Capture-Region + Peer-Verwaltung"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
SERVICE_FILE = os.path.join(os.path.dirname(__file__), "service.py")

MOD_CTRL = 1
MOD_SHIFT = 2
MOD_ALT = 4
MOD_WIN = 8

MOD_NAMES = {MOD_CTRL: "Ctrl", MOD_SHIFT: "Shift", MOD_ALT: "Alt", MOD_WIN: "Win"}
MODIFIER_VKS = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}

VK_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Escape",
    0x20: "Space", 0x2D: "Insert", 0x2E: "Delete", 0x24: "Home",
    0x23: "End", 0x21: "PageUp", 0x22: "PageDown",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x2C: "PrintScreen", 0x13: "Pause", 0x91: "ScrollLock",
    0x70: "F1", 0x71: "F2", 0x72: "F3", 0x73: "F4",
    0x74: "F5", 0x75: "F6", 0x76: "F7", 0x77: "F8",
    0x78: "F9", 0x79: "F10", 0x7A: "F11", 0x7B: "F12",
    0x7C: "F13", 0x7D: "F14", 0x7E: "F15", 0x7F: "F16",
    0x80: "F17", 0x81: "F18", 0x82: "F19", 0x83: "F20",
    0x5B: "Win", 0x5C: "Win",
    0xA0: "LShift", 0xA1: "RShift",
    0xA2: "LCtrl", 0xA3: "RCtrl",
    0xA4: "LAlt", 0xA5: "RAlt",
}


def vk_name(vk):
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    return VK_NAMES.get(vk, f"VK_0x{vk:02X}")


def mods_name(mods):
    parts = []
    for bit, name in sorted(MOD_NAMES.items()):
        if mods & bit:
            parts.append(name)
    return "+".join(parts) if parts else ""


def format_hotkey(mods, vk):
    prefix = mods_name(mods)
    key = vk_name(vk)
    return f"{prefix}+{key}" if prefix else key


def get_mods_async():
    u = ctypes.windll.user32
    m = 0
    if u.GetAsyncKeyState(0x11) & 0x8000 or u.GetAsyncKeyState(0xA2) & 0x8000 or u.GetAsyncKeyState(0xA3) & 0x8000:
        m |= MOD_CTRL
    if u.GetAsyncKeyState(0x10) & 0x8000 or u.GetAsyncKeyState(0xA0) & 0x8000 or u.GetAsyncKeyState(0xA1) & 0x8000:
        m |= MOD_SHIFT
    if u.GetAsyncKeyState(0x12) & 0x8000 or u.GetAsyncKeyState(0xA4) & 0x8000 or u.GetAsyncKeyState(0xA5) & 0x8000:
        m |= MOD_ALT
    if u.GetAsyncKeyState(0x5B) & 0x8000 or u.GetAsyncKeyState(0x5C) & 0x8000:
        m |= MOD_WIN
    return m


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {
        "device_name": os.environ.get("COMPUTERNAME", "Unbekannt"),
        "device_id": "",
        "port": 45781,
        "peers": [],
        "hotkeys": [],
    }


def save_config(cfg):
    if not cfg.get("device_id"):
        cfg["device_id"] = __import__("uuid").uuid4().hex[:8]
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def default_hotkeys(peers):
    hk = []
    for i, p in enumerate(peers):
        if i < 9:
            hk.append({"action": f"forward_{i}", "mods": MOD_CTRL | MOD_ALT, "key": 0x31 + i, "label": f"Forward to {p['name']}"})
    hk.append({"action": "return_local", "mods": MOD_CTRL | MOD_ALT, "key": 0x30, "label": "Return to local"})
    return hk


# ── Hotkey Capture Dialog (like ShareX) ─────────────────────────────
class HotkeyCapture(tk.Toplevel):
    def __init__(self, parent, current_mods=0, current_key=0):
        super().__init__(parent)
        self.title("Hotkey festlegen")
        self.geometry("360x160")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result_mods = current_mods
        self.result_key = current_key
        self.result_display = format_hotkey(current_mods, current_key) if current_key else "(none)"
        self.confirmed = False

        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="Tastenkombination drücken:", font=("", 10)).pack(anchor="w")

        self.display_var = tk.StringVar(value=self.result_display if current_key else "…")
        self.display_label = ttk.Label(f, textvariable=self.display_var, font=("", 16, "bold"),
                                        foreground="#0078D4")
        self.display_label.pack(pady=16)

        self.status_var = tk.StringVar(value="Tasten drücken…")
        ttk.Label(f, textvariable=self.status_var, foreground="gray").pack()

        btn_f = ttk.Frame(f)
        btn_f.pack(pady=(12, 0))
        self.ok_btn = ttk.Button(btn_f, text="OK", command=self._ok, state="disabled")
        self.ok_btn.pack(side="left", padx=4)
        ttk.Button(btn_f, text="Abbrechen", command=self.destroy).pack(side="left", padx=4)

        self.bind("<KeyPress>", self._on_key)
        self.bind("<KeyRelease>", self._on_release)
        self.focus_set()

        self._poll_mods()

    def _poll_mods(self):
        if not self.confirmed:
            mods = get_mods_async()
            if mods != self.result_mods and not self.result_key:
                self.result_mods = mods
                self.display_var.set(mods_name(mods) + "+…" if mods else "…")
            self.after(50, self._poll_mods)

    def _on_key(self, event):
        vk = event.keycode
        if vk == 0x1B:
            self.destroy()
            return
        if vk in MODIFIER_VKS:
            return

        mods = get_mods_async()
        self.result_mods = mods
        self.result_key = vk
        self.result_display = format_hotkey(mods, vk)
        self.display_var.set(self.result_display)
        self.status_var.set("Kombination erkannt! OK klicken zum Bestätigen.")
        self.ok_btn.config(state="normal")

    def _on_release(self, event):
        pass

    def _ok(self):
        self.confirmed = True
        self.destroy()


# ── Region Capture Overlay ──────────────────────────────────────────
class RegionCapture(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("")
        self.overrideredirect(True)
        self.attributes("-fullscreen", True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.35)
        self.config(cursor="crosshair", bg="black")
        self.transient(parent)
        self.focus_set()

        self.result = None
        self.start_x = None
        self.start_y = None
        self.rect = None

        self.canvas = tk.Canvas(self, highlightthickness=0, bg="black")
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self._on_down)
        self.canvas.bind("<B1-Motion>", self._on_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_up)
        self.bind("<Escape>", self._on_escape)
        self.bind("<KeyPress-Escape>", self._on_escape)

        width = self.winfo_screenwidth()
        height = self.winfo_screenheight()

        instr = self.canvas.create_text(
            width // 2, 30,
            text="Klicken und ziehen um Region auszuwählen. ESC = Abbrechen",
            fill="white", font=("", 12), tags="instr"
        )

    def _on_down(self, event):
        self.start_x = self.canvas.canvasx(event.x)
        self.start_y = self.canvas.canvasy(event.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            self.start_x, self.start_y, self.start_x, self.start_y,
            outline="#00FF00", width=3, fill="", tags="selection"
        )

    def _on_move(self, event):
        if not self.rect:
            return
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        self.canvas.coords(self.rect, self.start_x, self.start_y, cx, cy)
        self.canvas.delete("dim")
        x1, y1, x2, y2 = self.start_x, self.start_y, cx, cy
        w = abs(x2 - x1)
        h = abs(y2 - y1)
        mx = min(x1, x2) + w / 2
        my = min(y1, y2) + h / 2
        self.canvas.create_text(
            mx, my - 20, text=f"{int(w)} × {int(h)}",
            fill="#00FF00", font=("", 14, "bold"), tags="dim"
        )

    def _on_up(self, event):
        if not self.start_x:
            return
        ex = self.canvas.canvasx(event.x)
        ey = self.canvas.canvasy(event.y)
        x = min(self.start_x, ex)
        y = min(self.start_y, ey)
        w = abs(ex - self.start_x)
        h = abs(ey - self.start_y)
        if w < 10 or h < 10:
            return
        self.result = {"x": int(x), "y": int(y), "width": int(w), "height": int(h)}
        self.destroy()

    def _on_escape(self, event=None):
        self.result = None
        self.destroy()


# ── Peer Form (unchanged) ───────────────────────────────────────────
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
        ttk.Entry(f, textvariable=self.name_var, width=30).grid(row=0, column=1, pady=2, padx=(4, 0))

        ttk.Label(f, text="IP-Adresse:").grid(row=1, column=0, sticky="w", pady=2)
        self.host_var = tk.StringVar(value=(defaults or {}).get("host", ""))
        ttk.Entry(f, textvariable=self.host_var, width=30).grid(row=1, column=1, pady=2, padx=(4, 0))

        ttk.Label(f, text="Port:").grid(row=2, column=0, sticky="w", pady=2)
        self.port_var = tk.StringVar(value=str((defaults or {}).get("port", 45781)))
        ttk.Entry(f, textvariable=self.port_var, width=8).grid(row=2, column=1, sticky="w", pady=2, padx=(4, 0))

        btn_f = ttk.Frame(f)
        btn_f.grid(row=3, column=0, columnspan=2, pady=(16, 0))
        ttk.Button(btn_f, text="OK", command=self._ok).pack(side="left", padx=4)
        ttk.Button(btn_f, text="Abbrechen", command=self.destroy).pack(side="left", padx=4)

    def _ok(self):
        name = self.name_var.get().strip()
        host = self.host_var.get().strip()
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("Fehler", "Port muss eine Zahl sein", parent=self)
            return
        if not name or not host:
            messagebox.showerror("Fehler", "Name und IP-Adresse sind Pflicht", parent=self)
            return
        self.result = {"name": name, "host": host, "port": port}
        self.destroy()


# ── Peer Scanner ────────────────────────────────────────────────────
class PeerScanner:
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


# ── Main GUI ────────────────────────────────────────────────────────
class FlowShiftGUI:
    def __init__(self):
        self.cfg = load_config()
        self.service_proc = None
        self.scanner = None

        self.root = tk.Tk()
        self.root.title("FlowShift")
        self.root.geometry("760x640")
        self.root.minsize(640, 500)
        style = ttk.Style()
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")

        self._build_ui()
        self._refresh()
        self._check_first_run()

    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=4)

        self._build_device_tab(nb)
        self._build_hotkeys_tab(nb)
        self._build_control_tab(nb)
        self._build_info_tab(nb)

    # ── TAB 1: Geräte ───────────────────────────────────────────
    def _build_device_tab(self, nb):
        dev = ttk.Frame(nb)
        nb.add(dev, text="Geräte", padding=8)

        f1 = ttk.LabelFrame(dev, text="Dieses Gerät", padding=8)
        f1.pack(fill="x", pady=(0, 8))

        row = ttk.Frame(f1)
        row.pack(fill="x")
        ttk.Label(row, text="Name:").pack(side="left")
        self.name_var = tk.StringVar(value=self.cfg.get("device_name", ""))
        ttk.Entry(row, textvariable=self.name_var, width=28).pack(side="left", padx=4)
        ttk.Label(row, text="Port:").pack(side="left", padx=(12, 0))
        self.port_var = tk.StringVar(value=str(self.cfg.get("port", 45781)))
        ttk.Entry(row, textvariable=self.port_var, width=6).pack(side="left", padx=4)
        ttk.Button(row, text="Speichern", command=self._save_device).pack(side="left", padx=8)

        ip_label = ttk.Label(f1, text=f"Eigene IP: {get_local_ip()} – auf anderen Geräten hier angeben", foreground="gray")
        ip_label.pack(anchor="w", pady=(4, 0))

        f2 = ttk.LabelFrame(dev, text="Andere Geräte (Peers)", padding=8)
        f2.pack(fill="both", expand=True)

        cols = ("Name", "Adresse", "Port", "Status")
        self.peer_tree = ttk.Treeview(f2, columns=cols, show="headings", height=6)
        for c in cols:
            self.peer_tree.heading(c, text=c)
            self.peer_tree.column(c, width=140)
        self.peer_tree.pack(fill="both", expand=True, pady=(0, 6))

        btn_row = ttk.Frame(f2)
        btn_row.pack(fill="x")

        ttk.Button(btn_row, text="+ Hinzufügen", command=self._add_peer).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Bearbeiten", command=self._edit_peer).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Entfernen", command=self._remove_peer).pack(side="left", padx=2)
        self.scan_btn = ttk.Button(btn_row, text="Netzwerk scannen", command=self._scan_network)
        self.scan_btn.pack(side="right", padx=2)

    # ── TAB 2: Hotkeys ──────────────────────────────────────────
    def _build_hotkeys_tab(self, nb):
        hk_frame = ttk.Frame(nb)
        nb.add(hk_frame, text="Hotkeys", padding=8)

        ttk.Label(hk_frame, text="Tastenkombinationen für Aktionen (wie in ShareX):", font=("", 10)).pack(anchor="w", pady=(0, 8))

        self.hotkey_tree = ttk.Treeview(hk_frame, columns=("action", "hotkey"), show="headings", height=12)
        self.hotkey_tree.heading("action", text="Aktion")
        self.hotkey_tree.heading("hotkey", text="Tastenkombination")
        self.hotkey_tree.column("action", width=280)
        self.hotkey_tree.column("hotkey", width=250)
        self.hotkey_tree.pack(fill="both", expand=True, pady=(0, 6))

        btn_row = ttk.Frame(hk_frame)
        btn_row.pack(fill="x")

        self.hk_change_btn = ttk.Button(btn_row, text="Ändern", command=self._change_hotkey)
        self.hk_change_btn.pack(side="left", padx=2)
        self.hk_reset_btn = ttk.Button(btn_row, text="Zurücksetzen", command=self._reset_hotkeys)
        self.hk_reset_btn.pack(side="left", padx=2)

        info = ttk.LabelFrame(hk_frame, text="Hinweis", padding=6)
        info.pack(fill="x", pady=(8, 0))
        ttk.Label(info, text=(
            "Klicke auf eine Aktion, dann auf 'Ändern', um eine neue Tastenkombination zu lernen.\n"
            "Mindestens eine Modifier-Taste (Ctrl/Alt/Shift/Win) wird empfohlen.\n"
            "Änderungen werden sofort in der config.json gespeichert."
        ), foreground="gray", justify="left").pack(anchor="w")

    # ── TAB 3: Steuerung ────────────────────────────────────────
    def _build_control_tab(self, nb):
        ctrl = ttk.Frame(nb)
        nb.add(ctrl, text="Steuerung", padding=8)

        self.status_label = ttk.Label(ctrl, text=" Gestoppt", font=("", 14, "bold"))
        self.status_label.pack(pady=8)

        self.btn_start = ttk.Button(ctrl, text="▶ Service starten", command=self._toggle_service)
        self.btn_start.pack(pady=4)

        self.active_label = ttk.Label(ctrl, text="", font=("", 11))
        self.active_label.pack(pady=4)

        # Capture Region
        cap_frame = ttk.LabelFrame(ctrl, text="Capture-Region (Maus-Eingrenzung)", padding=8)
        cap_frame.pack(fill="x", pady=8)

        self.region_var = tk.StringVar(value="Keine Region (ganzer Bildschirm)")
        ttk.Label(cap_frame, textvariable=self.region_var, foreground="gray").pack(anchor="w")

        btn_cap = ttk.Frame(cap_frame)
        btn_cap.pack(fill="x", pady=(6, 0))
        ttk.Button(btn_cap, text="Region auswählen (klicken & ziehen)", command=self._select_region).pack(side="left", padx=2)
        ttk.Button(btn_cap, text="Zurücksetzen (ganzer Bildschirm)", command=self._clear_region).pack(side="left", padx=2)

        log_lf = ttk.LabelFrame(ctrl, text="Log", padding=4)
        log_lf.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text = tk.Text(log_lf, height=8, state="disabled", bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

    # ── TAB 4: Info ─────────────────────────────────────────────
    def _build_info_tab(self, nb):
        info_tab = ttk.Frame(nb)
        nb.add(info_tab, text="Info", padding=16)

        ttk.Label(info_tab, text="FlowShift v0.2.0", font=("", 16, "bold")).pack(anchor="w")
        ttk.Label(info_tab, text=(
            "Software-KVM für mehrere Geräte.\n\n"
            "So funktioniert's:\n"
            "1. Auf jedem Gerät die gleiche config.json verwenden\n"
            "2. Service auf allen Geräten starten\n"
            "3. Mit Hotkey umschalten, welches Gerät gesteuert wird\n"
            "4. Custom-Hotkeys unter dem Tab 'Hotkeys' konfigurieren\n"
            "5. Capture-Region eingrenzen für Maus während Weiterleitung\n\n"
            "Alle Geräte müssen im selben Netzwerk sein.\n"
            "Firewall muss TCP-Port 45781 erlauben."
        ), justify="left").pack(anchor="w", pady=8)

    # ── First-Run Wizard ────────────────────────────────────────
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

        ttk.Label(frame, text="Willkommen bei FlowShift!", font=("", 14, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Wir richten kurz dein Gerät ein.\nWas ist das für ein Gerät?", justify="left").pack(anchor="w", pady=8)

        ttk.Label(frame, text="Gerätename:").pack(anchor="w")
        name_var = tk.StringVar(value=self.name_var.get())
        ttk.Entry(frame, textvariable=name_var, width=40).pack(fill="x", pady=(0, 12))

        ttk.Label(frame, text=(
            "Nach dem Speichern kannst du auf dem Tab 'Geräte'\n"
            "deine anderen Geräte hinzufügen.\n\n"
            "Tipp: Starte danach den Service unter 'Steuerung'.\n"
            "Und unter 'Hotkeys' kannst du eigene Tastenkombis festlegen!"
        ), justify="left", foreground="gray").pack(anchor="w")

        def on_done():
            self.name_var.set(name_var.get().strip())
            self._save_device()
            wiz.destroy()

        ttk.Button(frame, text="Fertig", command=on_done).pack(pady=16)

    # ── Actions: Device ─────────────────────────────────────────
    def _save_device(self):
        self.cfg["device_name"] = self.name_var.get().strip() or "Unbekannt"
        try:
            self.cfg["port"] = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("Fehler", "Port muss eine Zahl sein")
            return
        self._ensure_hotkeys()
        save_config(self.cfg)
        self._log("Geräte-Einstellungen gespeichert")

    def _ensure_hotkeys(self):
        if not self.cfg.get("hotkeys"):
            self.cfg["hotkeys"] = default_hotkeys(self.cfg.get("peers", []))

    def _refresh(self):
        for row in self.peer_tree.get_children():
            self.peer_tree.delete(row)
        for p in self.cfg.get("peers", []):
            self.peer_tree.insert("", "end", values=(p["name"], p["host"], p.get("port", 45781), ""))

        # Refresh hotkey tree
        self._refresh_hotkeys()
        self._update_status()

    def _refresh_hotkeys(self):
        for row in self.hotkey_tree.get_children():
            self.hotkey_tree.delete(row)
        self._ensure_hotkeys()
        for h in self.cfg["hotkeys"]:
            disp = format_hotkey(h.get("mods", 0), h.get("key", 0))
            self.hotkey_tree.insert("", "end", values=(h.get("label", h["action"]), disp))

    # ── Actions: Peers ──────────────────────────────────────────
    def _add_peer(self):
        dlg = PeerForm(self.root, "Peer hinzufügen")
        self.root.wait_window(dlg)
        if dlg.result:
            self.cfg.setdefault("peers", []).append(dlg.result)
            self._ensure_hotkeys()
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
        self.scan_btn.config(state="disabled", text="Scanne...")
        self._log("Scanne Netzwerk nach FlowShift-Geräten...")

        def done(found):
            self.scan_btn.config(state="normal", text="Netzwerk scannen")
            if not found:
                self._log("Keine weiteren Geräte gefunden")
                messagebox.showinfo("Scan abgeschlossen",
                    "Keine weiteren FlowShift-Geräte gefunden.\n"
                    "Stelle sicher, dass der Service auf dem anderen Gerät läuft.")
                return
            for p in found:
                existing = any(e["host"] == p["host"] for e in self.cfg.get("peers", []))
                if not existing:
                    self.cfg.setdefault("peers", []).append(p)
                    self._log(f"Gefunden: {p['host']}")
            save_config(self.cfg)
            self._refresh()
            self._log(f"{len(found)} Gerät(e) gefunden und hinzugefügt")
            messagebox.showinfo("Scan abgeschlossen",
                f"{len(found)} Gerät(e) gefunden und zur Liste hinzugefügt.")

        self.scanner = PeerScanner(done)
        base = get_local_ip()
        threading.Thread(target=self.scanner.scan, args=(base, 2.0), daemon=True).start()

    # ── Actions: Hotkeys ────────────────────────────────────────
    def _change_hotkey(self):
        sel = self.hotkey_tree.selection()
        if not sel:
            messagebox.showinfo("Hinweis", "Bitte zuerst eine Aktion auswählen")
            return
        idx = self.hotkey_tree.index(sel[0])
        hk = self.cfg["hotkeys"][idx]

        dlg = HotkeyCapture(self.root, hk.get("mods", 0), hk.get("key", 0))
        self.root.wait_window(dlg)

        if dlg.confirmed and dlg.result_key:
            self.cfg["hotkeys"][idx]["mods"] = dlg.result_mods
            self.cfg["hotkeys"][idx]["key"] = dlg.result_key
            save_config(self.cfg)
            self._refresh_hotkeys()
            self._log(f"Hotkey geändert: {hk['label']} -> {format_hotkey(dlg.result_mods, dlg.result_key)}")

    def _reset_hotkeys(self):
        if messagebox.askyesno("Zurücksetzen", "Alle Hotkeys auf Standard zurücksetzen?"):
            self.cfg["hotkeys"] = default_hotkeys(self.cfg.get("peers", []))
            save_config(self.cfg)
            self._refresh_hotkeys()
            self._log("Hotkeys auf Standard zurückgesetzt")

    # ── Actions: Capture Region ─────────────────────────────────
    def _select_region(self):
        dlg = RegionCapture(self.root)
        self.root.wait_window(dlg)
        if dlg.result:
            self.cfg["capture_region"] = dlg.result
            save_config(self.cfg)
            r = dlg.result
            self.region_var.set(f"Region: {r['x']},{r['y']} – {r['width']}×{r['height']}")
            self._log(f"Capture-Region gesetzt: {r['x']},{r['y']} {r['width']}×{r['height']}")

    def _clear_region(self):
        if "capture_region" in self.cfg:
            del self.cfg["capture_region"]
            save_config(self.cfg)
        self.region_var.set("Keine Region (ganzer Bildschirm)")
        self._log("Capture-Region zurückgesetzt (ganzer Bildschirm)")

    # ── Actions: Service ────────────────────────────────────────
    def _toggle_service(self):
        if self.service_proc is not None:
            self.service_proc.terminate()
            self.service_proc = None
            self._update_status()
            self.btn_start.config(text="▶ Service starten")
            self._log("Service gestoppt")
            return

        self._save_device()

        if not self._elevate_as_admin():
            return

        try:
            self.service_proc = subprocess.Popen(
                [sys.executable, SERVICE_FILE],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
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

        msg = "Der Service muss als Administrator laufen.\nFlowShift jetzt neu starten mit Admin-Rechten?"
        if not messagebox.askyesno("Admin-Rechte benötigt", msg):
            self._log("Service-Start abgebrochen – Admin-Rechte fehlen")
            return False

        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{SERVICE_FILE}"', None, 1)
        self._log("Service als Administrator gestartet")
        messagebox.showinfo("Info",
            "Der Service läuft jetzt in einem eigenen Admin-Fenster.\n"
            "Du kannst es minimieren, aber nicht schliessen.")
        return True

    def _update_status(self):
        if self.service_proc and self.service_proc.poll() is None:
            self.status_label.config(text=" Läuft", foreground="green")
            self.active_label.config(text="Drücke Hotkey zum Umschalten")
        else:
            self.status_label.config(text=" Gestoppt", foreground="black")
            self.active_label.config(text="")

    def _log(self, msg):
        self.root.after(0, lambda: self._do_log(msg))

    def _do_log(self, msg):
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


if __name__ == "__main__":
    FlowShiftGUI().run()
