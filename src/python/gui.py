"""FlowShift GUI – Hotkey-Editor + Capture-Region + Peer-Verwaltung"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import ipaddress
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runtime_model as rm
import version
import elevated_task
from version import CREATE_NO_WINDOW
from runtime_model import (
    MOD_CTRL, MOD_SHIFT, MOD_ALT, MOD_WIN, MOD_NAMES, MODIFIER_VKS, VK_NAMES,
    vk_name, mods_name, format_hotkey,
    default_hotkeys, sync_hotkeys, hotkey_is_valid,
    peer_identity, index_by_identity,
    send_msg, recv_msg, recv_exact,
)

_BASE = os.path.dirname(__file__)
_DATA_DIR = os.environ.get("FLOWSHIFT_LOG_DIR") or _BASE
try:
    if os.environ.get("FLOWSHIFT_LOG_DIR"):
        os.makedirs(_DATA_DIR, exist_ok=True)
except OSError:
    _DATA_DIR = _BASE
CONFIG_FILE = os.environ.get("FLOWSHIFT_CONFIG") or os.path.join(_DATA_DIR, "config.json")
SERVICE_FILE = os.path.join(_BASE, "tray.py")
LOG_FILE = os.path.join(_DATA_DIR, "flowshift.log")
RUNTIME_OUT = os.path.join(_DATA_DIR, "flowshift_runtime.out")
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 45782


def vk_name(vk):  # noqa: F811 - keep GUI extended F13-F20 names
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    return VK_NAMES.get(vk, f"VK_0x{vk:02X}")


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
            cfg = json.load(f)
    else:
        cfg = {
            "device_name": os.environ.get("COMPUTERNAME", "Unbekannt"),
            "device_id": "",
            "port": 45781,
            "peers": [],
            "hotkeys": [],
        }

    needs_save = False
    device_id = str(cfg.get("device_id", "")).strip().lower()
    if len(device_id) != 8 or any(c not in "0123456789abcdef" for c in device_id):
        cfg["device_id"] = __import__("uuid").uuid4().hex[:8]
        needs_save = True

    # Keep hotkeys consistent with the peer list (migrate legacy indexes etc.).
    if sync_hotkeys(cfg):
        needs_save = True

    if needs_save or not os.path.exists(CONFIG_FILE):
        try:
            os.makedirs(os.path.dirname(CONFIG_FILE) or ".", exist_ok=True)
        except OSError:
            pass
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)

    return cfg


def save_config(cfg):
    if not cfg.get("device_id"):
        cfg["device_id"] = __import__("uuid").uuid4().hex[:8]
    sync_hotkeys(cfg)
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE) or ".", exist_ok=True)
    except OSError:
        pass
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)



# send_msg / recv_exact / recv_msg are imported from runtime_model.


def control_request(payload, timeout=0.5):
    with socket.create_connection((CONTROL_HOST, CONTROL_PORT), timeout=timeout) as sock:
        sock.settimeout(timeout)
        send_msg(sock, payload)
        return recv_msg(sock)


def _is_ipv4(ip):
    try:
        socket.inet_aton(ip)
        return ip.count(".") == 3
    except OSError:
        return False


def get_local_ipv4s():
    ips = []
    seen = set()

    def add(ip):
        if not ip:
            return
        ip = str(ip).strip()
        if not _is_ipv4(ip):
            return
        if ip.startswith("127.") or ip.startswith("169.254."):
            return
        if ip in seen:
            return
        seen.add(ip)
        ips.append(ip)

    ps_cmd = (
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object { $_.IPAddress -and $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | "
        "Select-Object IPAddress | ConvertTo-Json -Compress"
    )
    for shell in ("powershell", "pwsh"):
        try:
            proc = subprocess.run(
                [shell, "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=CREATE_NO_WINDOW,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                if isinstance(data, dict):
                    data = [data]
                for item in data:
                    add(item.get("IPAddress"))
                if ips:
                    return ips
        except FileNotFoundError:
            continue
        except Exception:
            pass

    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM):
            add(info[4][0])
    except Exception:
        pass

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        add(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    return ips or ["127.0.0.1"]


def get_scan_bases():
    bases = []
    seen = set()

    for ip in get_local_ipv4s():
        parts = ip.rsplit(".", 1)
        if len(parts) != 2:
            continue
        base = parts[0]
        if base in seen:
            continue
        seen.add(base)
        bases.append(base)
    return bases


def get_broadcast_targets():
    targets = []
    seen = set()

    ps_cmd = (
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object { $_.IPAddress -and $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | "
        "Select-Object IPAddress, PrefixLength | ConvertTo-Json -Compress"
    )

    for shell in ("powershell", "pwsh"):
        try:
            proc = subprocess.run(
                [shell, "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=CREATE_NO_WINDOW,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                if isinstance(data, dict):
                    data = [data]
                for item in data:
                    ip = (item.get("IPAddress") or "").strip()
                    prefix = item.get("PrefixLength")
                    try:
                        net = ipaddress.ip_network(f"{ip}/{int(prefix)}", strict=False)
                        target = str(net.broadcast_address)
                    except Exception:
                        continue
                    if target not in seen:
                        seen.add(target)
                        targets.append(target)
                if targets:
                    return targets
        except FileNotFoundError:
            continue
        except Exception:
            pass

    for ip in get_local_ipv4s():
        parts = ip.rsplit(".", 1)
        if len(parts) != 2:
            continue
        target = f"{parts[0]}.255"
        if target not in seen:
            seen.add(target)
            targets.append(target)

    if "255.255.255.255" not in seen:
        targets.append("255.255.255.255")

    return targets


# default_hotkeys is imported from runtime_model.


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

        # Preserve identity-relevant fields (device_id) across edits so a rename
        # keeps the peer's stable identity and its hotkey mapping.
        self._defaults = dict(defaults or {})

        ttk.Label(f, text="Anzeigename (optional):").grid(row=0, column=0, sticky="w", pady=2)
        self.name_var = tk.StringVar(value=(defaults or {}).get("name", (defaults or {}).get("host", "")))
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
        if not host:
            messagebox.showerror("Fehler", "IP-Adresse ist Pflicht", parent=self)
            return
        if not name:
            name = host
        result = {"name": name, "host": host, "port": port}
        # Keep the stable device_id so editing (e.g. renaming) does not change
        # the peer identity and thus its hotkey binding.
        did = str(self._defaults.get("device_id", "")).strip()
        if did:
            result["device_id"] = did
        self.result = result
        self.destroy()


# ── Peer Scanner ────────────────────────────────────────────────────
class PeerScanner:
    def __init__(self, callback, local_name, local_device_id, local_ips, port=45781):
        self.callback = callback
        self.local_name = local_name
        self.local_device_id = local_device_id
        self.local_ips = set(local_ips)
        self.port = int(port)
        self._stop = False

    def stop(self):
        self._stop = True

    def _discover_broadcast(self, timeout: float):
        found = {}
        probe = json.dumps({
            "type": "discover",
            "device_id": self.local_device_id,
            "display_name": self.local_name,
            "port": self.port,
        }).encode("utf-8")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(0.2)
            sock.bind(("", 0))

            for target in get_broadcast_targets():
                try:
                    sock.sendto(probe, (target, self.port))
                except Exception:
                    pass

            deadline = time.monotonic() + timeout
            while not self._stop and time.monotonic() < deadline:
                try:
                    raw, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break

                try:
                    resp = json.loads(raw.decode("utf-8"))
                except Exception:
                    continue

                if resp.get("type") != "discover_reply":
                    continue

                host = addr[0]
                if host in self.local_ips:
                    continue

                key = resp.get("device_id") or host
                if key in found:
                    continue
                found[key] = {
                    "name": (resp.get("display_name") or host).strip(),
                    "host": host,
                    "port": int(resp.get("port", self.port)),
                    "device_id": resp.get("device_id", ""),
                }
        finally:
            sock.close()

        return list(found.values())

    def _scan_tcp(self, base_ips, timeout: float = 2.0):
        found = []
        seen_hosts = set()
        seen_lock = threading.Lock()

        def try_host(host: str):
            if self._stop:
                return
            with seen_lock:
                if host in self.local_ips or host in seen_hosts:
                    return
                seen_hosts.add(host)
            try:
                with socket.create_connection((host, self.port), timeout=timeout) as s:
                    s.settimeout(timeout)
                    data = json.dumps({"type": "ping"}).encode("utf-8")
                    s.sendall(struct.pack("!I", len(data)) + data)
                    try:
                        resp = recv_msg(s)
                        if resp.get("type") not in ("pong", "hello"):
                            return
                    except Exception:
                        return

                found.append({
                    "name": host,
                    "host": host,
                    "port": self.port,
                })
            except Exception:
                pass

        hosts = []
        for base_ip in base_ips:
            parts = base_ip.rsplit(".", 1)
            if len(parts) != 2:
                continue
            subnet = parts[0]
            for i in range(1, 255):
                host = f"{subnet}.{i}"
                if host not in self.local_ips:
                    hosts.append(host)

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=64) as pool:
            list(pool.map(try_host, hosts))

        return found

    def scan(self, base_ips, timeout: float = 2.0):
        if isinstance(base_ips, str):
            base_ips = [base_ips]

        found_by_key = {}

        for peer in self._discover_broadcast(max(0.5, min(1.5, timeout))):
            key = peer.get("device_id") or peer.get("host")
            if key not in found_by_key:
                found_by_key[key] = peer

        for peer in self._scan_tcp(base_ips, timeout):
            key = peer.get("host")
            if key not in found_by_key:
                found_by_key[key] = peer

        if not self._stop:
            self.callback(list(found_by_key.values()))


# ── Main GUI ────────────────────────────────────────────────────────
class FlowShiftGUI:
    def __init__(self):
        self.cfg = load_config()
        self.service_proc = None
        self.service_state = "stopped"
        self.scanner = None
        self.runtime = None
        self.last_profile_name = None
        self.last_profile_identity = None
        self._last_runtime_summary = None
        self._status_polling = False
        self._state_deadline = 0.0        # monotonic deadline for start/stop
        self._transition_timeout = 15.0
        self._live_targets = {}
        self._local_vi = None
        self._git_pushed = None
        self._git_dirty = None


        self.root = tk.Tk()
        self.root.title("FlowShift")
        try:
            ico = os.path.join(os.path.dirname(__file__), "flowshift.ico")
            if os.path.exists(ico):
                self.root.iconbitmap(default=ico)
        except Exception:
            pass
        self.root.geometry("760x640")
        self.root.minsize(640, 500)
        style = ttk.Style()
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")

        self._build_ui()
        self._refresh()
        self._check_first_run()
        self._refresh_elevated_status()
        self._refresh_live_versions()
        self.refresh_runtime_status()
        self._schedule_runtime_poll()

    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=4)

        self._build_device_tab(nb)
        self._build_profile_tab(nb)
        self._build_hotkeys_tab(nb)
        self._build_control_tab(nb)
        self._build_live_tab(nb)
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

        ip_label = ttk.Label(
            f1,
            text=f"Eigene IPs: {', '.join(get_local_ipv4s())} – auf anderen Geräten diese Adresse(n) angeben",
            foreground="gray",
        )
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

    # ── TAB 2: Profile ──────────────────────────────────────────
    def _build_profile_tab(self, nb):
        prof = ttk.Frame(nb)
        nb.add(prof, text="Profile", padding=8)

        summary = ttk.LabelFrame(prof, text="Aktiver Zustand", padding=8)
        summary.pack(fill="x", pady=(0, 8))

        self.current_profile_var = tk.StringVar(value="Netzwerk: -")
        self.connection_state_var = tk.StringVar(value="Forwarding: inaktiv")
        self.direction_state_var = tk.StringVar(value="Capture: aus")
        self.hook_state_var = tk.StringVar(value="Hook: -")
        self.flow_state_var = tk.StringVar(value="Übertragen: -")
        self.capture_state_var = tk.StringVar(value="Capture-Region: -")

        for var in (
            self.current_profile_var,
            self.connection_state_var,
            self.direction_state_var,
            self.hook_state_var,
            self.flow_state_var,
            self.capture_state_var,
        ):
            ttk.Label(summary, textvariable=var).pack(anchor="w")

        # Health / session / pipeline diagnostics (red when unhealthy / Session 0).
        self.health_var = tk.StringVar(value="Runtime: -")
        self.health_label = ttk.Label(summary, textvariable=self.health_var)
        self.health_label.pack(anchor="w", pady=(4, 0))
        self.session_var = tk.StringVar(value="Session: -")
        self.session_label = ttk.Label(summary, textvariable=self.session_var)
        self.session_label.pack(anchor="w")
        self.pipeline_var = tk.StringVar(value="Pipeline: -")
        ttk.Label(summary, textvariable=self.pipeline_var, foreground="gray").pack(anchor="w")

        btn_row = ttk.Frame(summary)
        btn_row.pack(anchor="w", pady=(8, 0))
        ttk.Button(btn_row, text="Status aktualisieren", command=self.refresh_runtime_status).pack(side="left", padx=2)
        self.forward_toggle_btn = ttk.Button(btn_row, text="Forwarding starten", command=self._toggle_forwarding)
        self.forward_toggle_btn.pack(side="left", padx=2)

        peers_lf = ttk.LabelFrame(prof, text="Profile auswählen", padding=8)
        peers_lf.pack(fill="both", expand=True)

        header = ttk.Frame(peers_lf)
        header.pack(fill="x", pady=(0, 6))
        ttk.Label(header, text="Profil", width=24).pack(side="left")
        ttk.Label(header, text="Verbindung", width=22).pack(side="left")
        ttk.Label(header, text="Rolle", width=12).pack(side="left")
        ttk.Label(header, text="Aktion", width=18).pack(side="left")

        self.profile_rows = ttk.Frame(peers_lf)
        self.profile_rows.pack(fill="both", expand=True)

        ttk.Label(
            prof,
            text="Ein Klick auf 'Aktivieren' schaltet das aktive Steuerprofil um.",
            foreground="gray",
        ).pack(anchor="w", pady=(8, 0))

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

        # Runtime elevation (Scheduled Task) — one-time UAC at install, no prompt after.
        elev = ttk.LabelFrame(ctrl, text="Ausführungsmodus", padding=8)
        elev.pack(fill="x", pady=(0, 8))
        self.elevated_status_var = tk.StringVar(value="Modus: User (kein Admin)")
        ttk.Label(elev, textvariable=self.elevated_status_var, foreground="gray").pack(anchor="w")
        elev_btns = ttk.Frame(elev)
        elev_btns.pack(fill="x", pady=(6, 0))
        ttk.Button(elev_btns, text="Elevated Runtime installieren", command=self._install_elevated).pack(side="left", padx=2)
        ttk.Button(elev_btns, text="Elevated Runtime entfernen", command=self._remove_elevated).pack(side="left", padx=2)
        ttk.Button(elev_btns, text="Hängende Runtime beenden", command=self._kill_hanging_runtime).pack(side="right", padx=2)

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
        log_btns = ttk.Frame(log_lf)
        log_btns.pack(fill="x", pady=(0, 4))
        ttk.Button(log_btns, text="Logansicht leeren", command=self._clear_log_view).pack(side="left", padx=2)
        ttk.Button(log_btns, text="Logdatei leeren", command=self._clear_log_file).pack(side="left", padx=2)
        self.log_text = tk.Text(log_lf, height=8, state="disabled", bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

    # ── TAB: Live Test ──────────────────────────────────────────
    def _build_live_tab(self, nb):
        live = ttk.Frame(nb)
        nb.add(live, text="Live Test", padding=8)

        ver = ttk.LabelFrame(live, text="Version & Git (Gleichheit erforderlich)", padding=8)
        ver.pack(fill="x", pady=(0, 8))
        self.local_ver_var = tk.StringVar(value="Lokal: -")
        self.remote_ver_var = tk.StringVar(value="Remote: -")
        self.match_var = tk.StringVar(value="Match: -")
        self.git_var = tk.StringVar(value="Git: -")
        for v in (self.local_ver_var, self.remote_ver_var, self.match_var, self.git_var):
            ttk.Label(ver, textvariable=v).pack(anchor="w")
        ttk.Button(ver, text="Version/Git neu prüfen", command=self._refresh_live_versions).pack(anchor="w", pady=(6, 0))

        tgt = ttk.LabelFrame(live, text="Live Test: dieses Gerät -> Ziel", padding=8)
        tgt.pack(fill="both", expand=True)
        ttk.Label(tgt, text="Ziel-Peer (verbunden):").pack(anchor="w")
        self.live_target_var = tk.StringVar(value="")
        self.live_target_combo = ttk.Combobox(tgt, textvariable=self.live_target_var, state="readonly")
        self.live_target_combo.pack(fill="x", pady=(0, 6))
        self.live_target_combo.bind("<<ComboboxSelected>>", lambda e: self._update_live_button())
        ttk.Label(tgt, text=(
            "Ablauf: 1) Auf dem Ziel einen Editor (Notepad/Notepad++) öffnen und fokussieren.\n"
            "2) 'Live Test starten' klicken. Der Test aktiviert Forwarding, bewegt die Maus,\n"
            "   klickt links und tippt den Test-Text per Remote-Tastatur.\n"
            "3) Datei auf dem Ziel manuell speichern (Strg+S) als FlowShift_Remote_Test.txt.\n"
            "Der Live-Test startet NUR auf Klick und nur bei gleicher Version (oder Override)."
        ), foreground="gray", justify="left").pack(anchor="w")
        self.live_btn = ttk.Button(tgt, text="Live Test starten", command=self._run_live_test, state="disabled")
        self.live_btn.pack(anchor="w", pady=(8, 0))
        self.force_live_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tgt, text="Trotz Versions-/Push-Warnung erlauben (manuelle Bestätigung)",
                        variable=self.force_live_var, command=self._update_live_button).pack(anchor="w")

    LIVE_TEXT = ("FlowShift ist wach,\ndie Maus zieht leis,\n"
                 "vom Laptop zum Surface,\nein kleiner Beweis.\n")

    def _refresh_live_versions(self):
        def worker():
            vi = version.version_info()
            dirty = version.git_dirty()
            pushed = version.git_pushed()
            self.root.after(0, lambda: self._apply_live_versions(vi, dirty, pushed))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_live_versions(self, vi, dirty, pushed):
        self._local_vi = vi
        self._git_pushed = pushed
        self._git_dirty = dirty
        self.local_ver_var.set(
            f"Lokal: v{vi['app_version']} commit={vi['git_commit'][:12]} branch={vi['git_branch']} "
            f"proto={vi['protocol_version']}")
        parts = ["dirty (uncommitted!)" if dirty else ("clean" if dirty is False else "git unbekannt"),
                 "pushed" if pushed else ("NICHT gepusht" if pushed is False else "push unbekannt")]
        self.git_var.set("Git: " + ", ".join(parts))
        self._update_live_button()

    def _refresh_live_info(self, status):
        peers = (status or {}).get("peers", [])
        connected = [p for p in peers if p.get("connected")]
        self._live_targets = {}
        labels = []
        for p in connected:
            label = f"{p['name']}  [{p['identity']}]"
            self._live_targets[label] = (p["identity"], p.get("remote_version"))
            labels.append(label)
        if hasattr(self, "live_target_combo"):
            self.live_target_combo["values"] = labels
            if labels and self.live_target_var.get() not in labels:
                self.live_target_var.set(labels[0])
            elif not labels:
                self.live_target_var.set("")
        self._update_live_button()

    def _versions_match(self):
        local = getattr(self, "_local_vi", None)
        tgt = self._live_targets.get(self.live_target_var.get()) if hasattr(self, "_live_targets") else None
        if not (local and tgt and tgt[1]):
            return False
        rc = tgt[1].get("git_commit")
        lc = local.get("git_commit")
        return bool(rc and lc and rc != "unknown" and lc != "unknown" and rc == lc)

    def _update_live_button(self):
        if not hasattr(self, "live_btn"):
            return
        tgt = self._live_targets.get(self.live_target_var.get()) if hasattr(self, "_live_targets") else None
        remote = tgt[1] if tgt else None
        if remote:
            self.remote_ver_var.set(
                f"Remote: v{remote.get('app_version','?')} commit={str(remote.get('git_commit','?'))[:12]} "
                f"branch={remote.get('git_branch','?')} proto={remote.get('protocol_version','?')}")
        else:
            self.remote_ver_var.set("Remote: - (kein verbundener Peer)")
        match = self._versions_match()
        pushed_ok = (getattr(self, "_git_pushed", None) is True) and (getattr(self, "_git_dirty", None) is False)
        self.match_var.set(
            f"Match: {'ja' if match else 'nein'}" +
            ("" if match else "  – Versionen unterscheiden sich. Erst auf beiden Geräten aktualisieren."))
        allow = (match and pushed_ok) or bool(getattr(self, "force_live_var", None) and self.force_live_var.get())
        self.live_btn.config(state="normal" if allow else "disabled")

    def _run_live_test(self):
        tgt = self._live_targets.get(self.live_target_var.get()) if hasattr(self, "_live_targets") else None
        if not tgt:
            self._log("Live Test: kein verbundener Ziel-Peer ausgewählt", "WARN")
            return
        # Never run a live test when the local runtime is in Session 0 (a service):
        # interactive capture/injection cannot work there.
        sess = (self.runtime or {}).get("session") or {}
        if sess.get("is_service_session"):
            self._log("Live Test abgebrochen: lokale Runtime läuft in Session 0 (Dienst) — "
                      "kein interaktiver Input möglich. Runtime in User-Session starten.", "ERROR")
            return
        identity = tgt[0]
        if not self._versions_match() and not self.force_live_var.get():
            self._log("Live Test abgebrochen: Versionen unterscheiden sich (Override nicht gesetzt)", "ERROR")
            return
        if getattr(self, "_git_pushed", None) is not True and not self.force_live_var.get():
            self._log("Live Test abgebrochen: bitte committen und 'git push' ausführen (oder Override setzen)", "ERROR")
            return

        def worker():
            try:
                self._log("=== Live Test start ===", "INFO")
                r = control_request({"type": "activate", "profile": identity}, timeout=1.5)
                if r.get("type") != "ok":
                    self._log(f"Live Test: Aktivierung fehlgeschlagen: {r.get('error')}", "ERROR")
                    return
                self._log("Live Test: Forwarding aktiviert", "INFO")
                time.sleep(0.6)
                moves = [{"type": "mousemove", "x": x, "y": 400} for x in range(200, 1500, 130)]
                control_request({"type": "send_synthetic", "events": moves}, timeout=1.5)
                self._log("Live Test: Mausbewegung gesendet", "INFO")
                control_request({"type": "send_synthetic", "events": [
                    {"type": "mousedown", "button": 0}, {"type": "mouseup", "button": 0}]}, timeout=1.5)
                self._log("Live Test: Linksklick gesendet", "INFO")
                time.sleep(0.4)
                control_request({"type": "type_text", "text": self.LIVE_TEXT}, timeout=3.0)
                self._log("Live Test: Test-Text getippt (Remote-Tastatur)", "INFO")
                time.sleep(0.4)
                control_request({"type": "deactivate"}, timeout=1.5)
                self._log("Live Test: Forwarding deaktiviert. Bitte Datei auf dem Ziel speichern (Strg+S).", "INFO")
                self._log("=== Live Test fertig ===", "INFO")
            except Exception as e:
                self._log(f"Live Test Fehler: {e}", "ERROR")

        threading.Thread(target=worker, daemon=True).start()

    # ── TAB 4: Info ─────────────────────────────────────────────
    def _build_info_tab(self, nb):
        info_tab = ttk.Frame(nb)
        nb.add(info_tab, text="Info", padding=16)

        ttk.Label(info_tab, text=f"FlowShift v{version.APP_VERSION}", font=("", 16, "bold")).pack(anchor="w")
        ttk.Label(info_tab, text=(
            "Software-KVM für mehrere Geräte.\n\n"
            "So funktioniert's:\n"
            "1. Pro Gerät den Dienst starten\n"
            "2. Geräte werden automatisch per LAN-Discovery gefunden\n"
            "3. Profile im Tab 'Profile' aktivieren/deaktivieren\n"
            "4. Custom-Hotkeys unter dem Tab 'Hotkeys' konfigurieren\n"
            "5. Capture-Region eingrenzen für Maus während Weiterleitung\n\n"
            "Die Konfiguration wird lokal erzeugt und gespeichert.\n"
            "Alle Geräte müssen im selben Netzwerk sein.\n"
            "Firewall muss TCP-Port 45781 erlauben."
        ), justify="left").pack(anchor="w", pady=8)

    # ── First-Run Wizard ────────────────────────────────────────
    def _check_first_run(self):
        return

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
        # Migrate legacy actions, add hotkeys for new peers, refresh labels.
        sync_hotkeys(self.cfg)

    def _refresh(self):
        for row in self.peer_tree.get_children():
            self.peer_tree.delete(row)
        for p in self.cfg.get("peers", []):
            self.peer_tree.insert("", "end", values=(p["name"], p["host"], p.get("port", 45781), ""))

        # Refresh hotkey tree
        self._refresh_hotkeys()
        self._render_profile_rows()
        self._update_status()
        self._sync_forwarding_button()

    def _refresh_hotkeys(self):
        for row in self.hotkey_tree.get_children():
            self.hotkey_tree.delete(row)
        self._ensure_hotkeys()
        for h in self.cfg["hotkeys"]:
            disp = format_hotkey(h.get("mods", 0), h.get("key", 0))
            label = h.get("label", h["action"])
            if not hotkey_is_valid(self.cfg, h):
                label = f"{label}  (ungültig)"
            self.hotkey_tree.insert("", "end", values=(label, disp))

    def _schedule_runtime_poll(self):
        if self._status_polling:
            return
        self._status_polling = True

        def loop():
            while self._status_polling:
                self.refresh_runtime_status()
                time.sleep(1.0)

        threading.Thread(target=loop, daemon=True).start()

    def _runtime_alive(self):
        try:
            resp = control_request({"type": "status"}, timeout=0.3)
            if resp.get("type") != "status":
                return False
            status = resp.get("status") or {}
            if "running" in status:
                return bool(status.get("running"))
            return True
        except Exception:
            return False

    def _render_profile_rows(self):
        for child in self.profile_rows.winfo_children():
            child.destroy()

        peers = self.cfg.get("peers", [])
        # Map runtime status rows by STABLE identity, never by display name, so
        # two peers with the same name are told apart correctly.
        runtime_peers = index_by_identity((self.runtime or {}).get("peers", []))
        active_identity = (self.runtime or {}).get("active_peer_identity")

        if not peers:
            ttk.Label(self.profile_rows, text="Noch keine Profile angelegt.", foreground="gray").pack(anchor="w")
            return

        for peer in peers:
            identity = peer_identity(peer)
            row = ttk.Frame(self.profile_rows)
            row.pack(fill="x", pady=2)

            rt = runtime_peers.get(identity, {})
            selected = bool(active_identity and active_identity == identity)
            connected = bool(rt.get("connected"))
            any_active = bool(active_identity)
            # Show direction label only when forwarding is active; empty otherwise.
            conn_text = rt.get("link_label") or ""
            role = rt.get("direction") or ""

            name_text = peer["name"] + ("  ●" if selected else "")
            ttk.Label(row, text=name_text, width=24).pack(side="left")
            ttk.Label(row, text=conn_text, width=28).pack(side="left")
            ttk.Label(row, text=role, width=12).pack(side="left")

            if selected:
                btn_text = "Aktiv"
                btn_state = "disabled"
            elif any_active:
                # Another peer is already active: block to prevent circular forwarding.
                # User must first deactivate the active profile.
                btn_text = "Aktivieren"
                btn_state = "disabled"
            else:
                btn_text = "Aktivieren"
                btn_state = "normal"
            ttk.Button(row, text=btn_text, state=btn_state,
                       command=lambda ident=identity: self._activate_profile(ident)).pack(side="left", padx=(4, 2))
            ttk.Button(row, text="Ping",
                       command=lambda ident=identity: self._ping_profile(ident)).pack(side="left", padx=2)

    def _sync_forwarding_button(self):
        if not hasattr(self, "forward_toggle_btn"):
            return
        runtime = self.runtime or {}
        active = bool(runtime.get("active"))
        if active:
            active_peer = runtime.get("active_peer") or "?"
            self.forward_toggle_btn.config(
                text=f"Forwarding stoppen  ({active_peer})"
            )
        else:
            # Show which peer would be activated.
            peers = self.cfg.get("peers", [])
            target_name = None
            if self.last_profile_identity:
                for p in peers:
                    if peer_identity(p) == self.last_profile_identity:
                        target_name = p.get("name")
                        break
            if target_name is None and peers:
                target_name = peers[0].get("name")
            label = (f"Forwarding starten \u2192 {target_name}"
                     if target_name else "Forwarding starten")
            self.forward_toggle_btn.config(text=label)

    def refresh_runtime_status(self):
        def worker():
            status = None
            try:
                resp = control_request({"type": "status"}, timeout=0.4)
                if resp.get("type") == "status":
                    status = resp.get("status")
            except Exception:
                status = None
            self.root.after(0, lambda: self._apply_runtime_status(status))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_runtime_status(self, status):
        self.runtime = status
        now = time.monotonic()
        if not status:
            # Runtime not reachable via the control socket (the source of truth).
            if self.service_state == "stopping":
                self.service_state = "stopped"
            elif self.service_state == "starting":
                if now >= self._state_deadline:
                    self.service_state = "error"
                    self._log("Service-Start abgelaufen (Runtime nicht erreichbar)", "ERROR")
                # else: keep waiting
            else:
                self.service_state = "stopped"
            self.current_profile_var.set("Netzwerk: -")
            self.connection_state_var.set("Forwarding: -")
            self.direction_state_var.set("Capture: -")
            self.hook_state_var.set("Hook: -")
            self.flow_state_var.set("Übertragen: keyboard, mouse move, mouse buttons, mouse wheel")
            self.capture_state_var.set("Capture-Region: -")
            self.health_var.set("Runtime: nicht erreichbar")
            try:
                self.health_label.config(foreground="gray")
                self.session_label.config(foreground="gray")
            except Exception:
                pass
            self.session_var.set("Session: -")
            self.pipeline_var.set("Pipeline: -")
            if self._last_runtime_summary != "service-unreachable":
                self._last_runtime_summary = "service-unreachable"
                self._log("Runtime: service unreachable", "WARN")
        else:
            # Reachable: control socket is the truth.
            if status.get("shutting_down"):
                self.service_state = "stopping"
            elif self.service_state == "stopping":
                # Still reachable while we asked it to stop.
                if now >= self._state_deadline:
                    self.service_state = "error"
                    self._log("Service-Stop abgelaufen (Runtime weiter erreichbar)", "ERROR")
            else:
                self.service_state = "running"
            # STRICTLY separate: network vs forwarding vs capture, so the UI never
            # implies forwarding when only a network connection exists.
            network_connected = bool(status.get("network_connected"))
            network_peer = status.get("network_peer") or "-"
            forwarding_active = bool(status.get("forwarding_active"))
            forwarding_target = status.get("forwarding_target") or "-"
            capture_active = bool(status.get("capture_active"))
            hook_running = bool(status.get("hook_running"))
            device = status.get("device_name") or "dieses Gerät"

            active_peer = status.get("active_peer") or "-"
            if active_peer != "-":
                self.last_profile_name = active_peer
            active_ident = status.get("active_peer_identity")
            if active_ident:
                self.last_profile_identity = active_ident

            if network_connected:
                self.current_profile_var.set(f"Netzwerk: verbunden mit {network_peer}")
            else:
                self.current_profile_var.set("Netzwerk: getrennt")
            if forwarding_active:
                self.connection_state_var.set(f"Forwarding aktiv: {device} -> {forwarding_target}")
            else:
                self.connection_state_var.set("Forwarding: inaktiv")
            self.direction_state_var.set(f"Capture: {'aktiv' if capture_active else 'aus'}")
            self.hook_state_var.set(f"Hook: {'online' if hook_running else 'offline'}")

            flow = status.get("forwarding") or []
            self.flow_state_var.set(f"Übertragen: {', '.join(flow) if flow else '-'}")
            cap = status.get("capture_region")
            if cap:
                self.capture_state_var.set(
                    f"Capture-Region: {cap['x']},{cap['y']} {cap['width']}x{cap['height']}"
                )
            else:
                self.capture_state_var.set("Capture-Region: ganzer Bildschirm")

            summary = (f"net={'on' if network_connected else 'off'}({network_peer}) "
                       f"fwd={'on' if forwarding_active else 'off'}({forwarding_target}) "
                       f"capture={'on' if capture_active else 'off'}")
            if summary != self._last_runtime_summary:
                self._last_runtime_summary = summary
                self._log(f"Runtime: {summary}", "DEBUG")

            # Worker health.
            down = status.get("critical_workers_down") or []
            healthy = status.get("runtime_healthy", True)
            if healthy and not down:
                self.health_var.set("Runtime: gesund (alle Worker aktiv)")
                try:
                    self.health_label.config(foreground="green")
                except Exception:
                    pass
            else:
                self.health_var.set(f"Runtime: FEHLER — Worker tot: {', '.join(down) or '?'}")
                try:
                    self.health_label.config(foreground="red")
                except Exception:
                    pass
            health_key = "ok" if (healthy and not down) else ("down:" + ",".join(down))
            if health_key != getattr(self, "_last_health_key", None):
                self._last_health_key = health_key
                if health_key != "ok":
                    self._log(f"CRITICAL: kritische Worker tot: {', '.join(down)}", "ERROR")

            # Session context (Session 0 = service, no interactive input).
            sess = status.get("session") or {}
            sid = sess.get("session_id")
            is_svc = bool(sess.get("is_service_session"))
            if is_svc:
                self.session_var.set(
                    "Session: 0 (Dienst) — Input-Forwarding NICHT möglich!")
                try:
                    self.session_label.config(foreground="red")
                except Exception:
                    pass
            else:
                self.session_var.set(
                    f"Session: {sid} interaktiv, User={sess.get('username') or '-'}")
                try:
                    self.session_label.config(foreground="green")
                except Exception:
                    pass
            if is_svc != getattr(self, "_last_session_svc", None):
                self._last_session_svc = is_svc
                if is_svc:
                    self._log("Runtime läuft in Session 0 (Dienst) — Input-Forwarding "
                              "kann nicht funktionieren. Runtime in User-Session starten.", "ERROR")

            # Pipeline counters.
            p = status.get("pipeline") or {}
            self.pipeline_var.set(
                "Pipeline: queued={q} forwarded={f} send_fail={sf} "
                "recv={rb} injected={inj} inj_fail={jf} q_size={qs}/{is_}".format(
                    q=p.get("events_queued", 0), f=p.get("events_forwarded", 0),
                    sf=p.get("events_send_failed", 0), rb=p.get("input_batches_received", 0),
                    inj=p.get("events_injected", 0), jf=p.get("inject_failed", 0),
                    qs=p.get("event_queue_size", 0), is_=p.get("inject_queue_size", 0)))

        self._render_profile_rows()
        self._sync_forwarding_button()
        self._refresh_live_info(status)
        self._update_status()

    def _activate_profile(self, name):
        def worker():
            try:
                self._log(f"Aktiviere Profil: {name}", "INFO")
                resp = control_request({"type": "activate", "profile": name}, timeout=0.6)
                ok = resp.get("type") == "ok"
            except Exception as e:
                ok = False
                resp = {"error": str(e)}
            self.root.after(0, lambda: self._after_profile_command(ok, name, resp))

        threading.Thread(target=worker, daemon=True).start()

    def _deactivate_profile(self):
        def worker():
            try:
                self._log("Deaktiviere Forwarding", "INFO")
                resp = control_request({"type": "deactivate"}, timeout=0.6)
                ok = resp.get("type") == "ok"
            except Exception as e:
                ok = False
                resp = {"error": str(e)}
            self.root.after(0, lambda: self._after_profile_command(ok, None, resp))

        threading.Thread(target=worker, daemon=True).start()

    def _ping_profile(self, name):
        def worker():
            started = time.monotonic()
            try:
                self._log(f"Ping starte: {name}", "INFO")
                resp = control_request({"type": "ping_peer", "profile": name}, timeout=6.0)
                ok = resp.get("type") == "ok"
            except Exception as e:
                ok = False
                resp = {"error": str(e)}
            elapsed_ms = round((time.monotonic() - started) * 1000)
            self.root.after(0, lambda: self._after_ping_command(ok, name, resp, elapsed_ms))

        threading.Thread(target=worker, daemon=True).start()

    def _toggle_forwarding(self):
        runtime = self.runtime or {}
        if runtime.get("active"):
            self._deactivate_profile()
            return

        # Prefer the last active peer's stable identity, else the first peer.
        target = self.last_profile_identity
        if not target:
            peers = self.cfg.get("peers", [])
            if peers:
                target = peer_identity(peers[0])
        if not target:
            self._log("Kein Profil vorhanden, das aktiviert werden kann", "WARN")
            messagebox.showinfo("Hinweis", "Bitte zuerst mindestens ein Profil anlegen.")
            return
        self._activate_profile(target)

    def _after_profile_command(self, ok, name, resp):
        if ok:
            msg = f"Profil aktiviert: {name}" if name else "Forwarding gestoppt"
            self._log(msg)
            self.refresh_runtime_status()
        else:
            self._log(f"Profil-Aktion fehlgeschlagen: {resp.get('error', 'unknown error')}", "ERROR")
            messagebox.showerror("Fehler", resp.get("error", "Profil konnte nicht geändert werden"))

    def _after_ping_command(self, ok, name, resp, elapsed_ms):
        if ok:
            ping = resp.get("ping") or {}
            reply = ping.get("reply") or {}
            peer = reply.get("display_name") or ping.get("peer") or name
            remote_id = reply.get("device_id") or "-"
            self._log(
                f"Ping OK: {name} -> {peer} rtt={elapsed_ms}ms device_id={remote_id}",
                "INFO",
            )
        else:
            self._log(f"Ping fehlgeschlagen: {name} / {resp.get('error', 'unknown error')}", "ERROR")

    # ── Actions: Peers ──────────────────────────────────────────
    def _add_peer(self):
        self._log("Peer-Dialog geöffnet", "DEBUG")
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
        self._log(f"Peer bearbeiten: {peer['name']}", "DEBUG")
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
        del self.cfg["peers"][idx]
        save_config(self.cfg)
        self._refresh()
        self._log(f"Peer entfernt ohne Rückfrage: {name}", "WARN")

    def _scan_network(self):
        self.scan_btn.config(state="disabled", text="Scanne...")
        self._log("Scanne Netzwerk nach FlowShift-Geräten...", "INFO")

        def done(found):
            self.scan_btn.config(state="normal", text="Netzwerk scannen")
            if not found:
                self._log("Keine weiteren Geräte gefunden", "WARN")
                messagebox.showinfo("Scan abgeschlossen",
                    "Keine weiteren FlowShift-Geräte gefunden.\n"
                    "Stelle sicher, dass der Service auf dem anderen Gerät läuft.")
                return
            for p in found:
                existing = any(e["host"] == p["host"] for e in self.cfg.get("peers", []))
                if not existing:
                    self.cfg.setdefault("peers", []).append(p)
                    self._log(f"Gefunden: {p['host']} ({p.get('name', 'unbekannt')})", "INFO")
            save_config(self.cfg)
            self._refresh()
            self._log(f"{len(found)} Gerät(e) gefunden und hinzugefügt", "INFO")
            messagebox.showinfo("Scan abgeschlossen",
                f"{len(found)} Gerät(e) gefunden und zur Liste hinzugefügt.")

        local_ips = get_local_ipv4s()
        self.scanner = PeerScanner(done, self.cfg.get("device_name", ""), self.cfg.get("device_id", ""), local_ips, self.cfg.get("port", 45781))
        bases = get_scan_bases()
        self._log(f"Scan-Basen: {', '.join(bases) if bases else '-'}", "DEBUG")
        threading.Thread(target=self.scanner.scan, args=(bases, 2.0), daemon=True).start()

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
        # Never act while a transition is in flight.
        if self.service_state in ("starting", "stopping"):
            self._log("Service-Übergang läuft bereits, bitte warten", "WARN")
            return
        if self._runtime_alive():
            self._begin_stop()
        else:
            self._begin_start()

    def _begin_stop(self):
        self.service_state = "stopping"
        self._state_deadline = time.monotonic() + self._transition_timeout
        self._update_status()
        self._log("Sende Shutdown an Runtime", "INFO")
        try:
            control_request({"type": "shutdown"}, timeout=1.0)
        except Exception as e:
            self._log(f"Shutdown-Anfrage fehlgeschlagen: {e}", "ERROR")
        # The status poll drives stopping -> stopped once the socket is gone.

    def _begin_start(self):
        self._save_device()
        if self._runtime_alive():
            self.service_state = "running"
            self._update_status()
            self._log("Runtime lief bereits", "INFO")
            return

        # Detect a half-dead runtime holding the control port.
        zombie = self._pid_on_port(CONTROL_PORT)
        if zombie:
            self._log(f"Hängende Runtime erkannt (PID {zombie}) – Control-Socket antwortet nicht. "
                      f"Bitte 'Hängende Runtime beenden' klicken.", "WARN")
            self.service_state = "error"
            self._update_status()
            return

        self.service_state = "starting"
        self._state_deadline = time.monotonic() + self._transition_timeout
        self._update_status()

        try:
            if elevated_task.is_installed():
                self._log("Starte Runtime über Elevated Scheduled Task (kein UAC-Prompt)", "INFO")
                ok, msg = elevated_task.run_task()
                self._log(f"Elevated Task Start: {msg}", "INFO" if ok else "ERROR")
                if not ok:
                    self.service_state = "error"
                    self._update_status()
                return
            self._spawn_user_runtime()
        except Exception as e:
            self.service_state = "error"
            self._update_status()
            self._log(f"Start fehlgeschlagen: {e}", "ERROR")

    def _pythonw(self):
        exe = sys.executable
        if exe.lower().endswith("python.exe"):
            w = exe[:-len("python.exe")] + "pythonw.exe"
            if os.path.exists(w):
                return w
        return exe

    def _spawn_user_runtime(self):
        exe = self._pythonw()
        # Redirect the child's stdout/stderr to a file so a startup crash is
        # captured (instead of vanishing in a CMD window or DEVNULL).
        try:
            out = open(RUNTIME_OUT, "a", encoding="utf-8")
        except Exception:
            out = subprocess.DEVNULL
        self.service_proc = subprocess.Popen(
            [exe, SERVICE_FILE, "--tray"],
            stdout=out, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
            cwd=os.path.dirname(os.path.abspath(SERVICE_FILE)),
        )
        self._log(f"Runtime-Prozess gestartet (user mode) pid={self.service_proc.pid}; "
                  f"Ausgabe -> {RUNTIME_OUT}", "INFO")

    def _pid_on_port(self, port):
        """Return the PID listening on a local TCP port, or None (no CMD window)."""
        if sys.platform != "win32":
            return None
        ps = (f"(Get-NetTCPConnection -State Listen -LocalPort {int(port)} "
              f"-ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, text=True, timeout=3,
                               creationflags=CREATE_NO_WINDOW)
            out = r.stdout.strip()
            return int(out) if out.isdigit() else None
        except Exception:
            return None

    def _kill_hanging_runtime(self):
        self._log("Versuche hängende Runtime zu beenden…", "WARN")
        try:
            control_request({"type": "shutdown"}, timeout=1.0)
        except Exception:
            pass
        time.sleep(1.0)
        killed = False
        for port in (CONTROL_PORT, self.cfg.get("port", 45781)):
            pid = self._pid_on_port(port)
            if pid:
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                   capture_output=True, text=True, timeout=5,
                                   creationflags=CREATE_NO_WINDOW)
                    self._log(f"Hängende Runtime beendet (PID {pid}, Port {port})", "WARN")
                    killed = True
                except Exception as e:
                    self._log(f"Kill fehlgeschlagen PID {pid}: {e}", "ERROR")
        if not killed:
            self._log("Keine hängende Runtime gefunden", "INFO")
        self.service_state = "stopped"
        self._update_status()

    # ── Elevated runtime (Scheduled Task) ───────────────────────────
    def _install_elevated(self):
        self._log("Installiere Elevated Runtime (einmalige Admin-Bestätigung)…", "INFO")
        ok, msg = elevated_task.install_task_elevated(SERVICE_FILE)
        self._log(f"Elevated Runtime installieren: {msg}", "INFO" if ok else "ERROR")
        self.root.after(1500, self._refresh_elevated_status)

    def _remove_elevated(self):
        self._log("Entferne Elevated Runtime…", "INFO")
        ok, msg = elevated_task.remove_task_elevated()
        self._log(f"Elevated Runtime entfernen: {msg}", "INFO" if ok else "ERROR")
        self.root.after(1500, self._refresh_elevated_status)

    def _refresh_elevated_status(self):
        try:
            installed = elevated_task.is_installed()
        except Exception:
            installed = False
        if installed:
            self.elevated_status_var.set("Modus: Elevated Task installiert (Start ohne UAC-Prompt)")
        else:
            self.elevated_status_var.set("Modus: User (kein Admin) – kein UAC-Prompt beim Start")

    # ── Log view ────────────────────────────────────────────────────
    def _clear_log_view(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self._log("Logansicht geleert", "INFO")

    def _clear_log_file(self):
        if not messagebox.askyesno("Logdatei leeren",
                                   f"Logdatei wirklich leeren?\n{LOG_FILE}"):
            return
        try:
            open(LOG_FILE, "w", encoding="utf-8").close()
            self._log("Logdatei geleert (Runtime-Logging läuft weiter)", "INFO")
        except Exception as e:
            self._log(f"Logdatei leeren fehlgeschlagen: {e}", "ERROR")

    def _update_status(self):
        # Derive purely from the state machine (fed by the control-socket poll);
        # never do a blocking socket call on the UI thread here.
        state = self.service_state
        if state == "running":
            self.status_label.config(text=" Läuft", foreground="green")
            self.active_label.config(text="Drücke Hotkey zum Umschalten")
            self.btn_start.config(text="⏹ Service stoppen")
        elif state == "starting":
            self.status_label.config(text=" Startet...", foreground="orange")
            self.active_label.config(text="Warte auf Runtime")
            self.btn_start.config(text="⏳ Service startet")
        elif state == "stopping":
            self.status_label.config(text=" Stoppt...", foreground="orange")
            self.active_label.config(text="Warte auf Shutdown")
            self.btn_start.config(text="⏳ Service stoppt")
        elif state == "error":
            self.status_label.config(text=" Fehler", foreground="red")
            self.active_label.config(text="Aktion fehlgeschlagen")
            self.btn_start.config(text="▶ Service starten")
        else:
            self.status_label.config(text=" Gestoppt", foreground="black")
            self.active_label.config(text="")
            self.btn_start.config(text="▶ Service starten")

    def _log(self, msg, level="INFO"):
        self.root.after(0, lambda: self._do_log(msg, level))

    def _do_log(self, msg, level="INFO"):
        self.log_text.config(state="normal")
        stamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{stamp}] [{level}] {msg}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        self._status_polling = False
        self.root.destroy()


if __name__ == "__main__":
    FlowShiftGUI().run()
