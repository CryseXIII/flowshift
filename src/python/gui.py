"""FlowShift GUI – Tkinter config tool (zero external dependencies)"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {
        "device_name": os.environ.get("COMPUTERNAME", "unknown"),
        "device_id": __import__("uuid").uuid4().hex[:8],
        "port": 45781,
        "peers": [],
    }


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


class FlowShiftGUI:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.service_proc: subprocess.Popen | None = None
        self.status_data: dict = {}

        self.root = tk.Tk()
        self.root.title("FlowShift Config")
        self.root.geometry("680x520")
        self.root.resizable(True, True)

        style = ttk.Style()
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")

        self._build_ui()
        self._refresh_peer_list()
        self._start_status_poller()

    def _build_ui(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        # ── Tab 1: Devices ──────────────────────────────────────
        dev_frame = ttk.Frame(nb)
        nb.add(dev_frame, text="Devices")

        top = ttk.Frame(dev_frame)
        top.pack(fill="x", pady=4)

        ttk.Label(top, text="Device Name:").pack(side="left")
        self.name_var = tk.StringVar(value=self.cfg.get("device_name", ""))
        ttk.Entry(top, textvariable=self.name_var, width=30).pack(side="left", padx=4)

        ttk.Label(top, text="Port:").pack(side="left", padx=(12, 0))
        self.port_var = tk.StringVar(value=str(self.cfg.get("port", 45781)))
        ttk.Entry(top, textvariable=self.port_var, width=6).pack(side="left", padx=4)

        ttk.Button(top, text="Save", command=self._save_device).pack(side="left", padx=8)

        # Peer list
        cols = ("Name", "Host", "Port")
        self.peer_tree = ttk.Treeview(dev_frame, columns=cols, show="headings", height=8)
        for c in cols:
            self.peer_tree.heading(c, text=c)
            self.peer_tree.column(c, width=150)
        self.peer_tree.pack(fill="both", expand=True, pady=4)

        peer_btns = ttk.Frame(dev_frame)
        peer_btns.pack(fill="x")
        ttk.Button(peer_btns, text="+ Add Peer", command=self._add_peer).pack(side="left", padx=2)
        ttk.Button(peer_btns, text="✎ Edit", command=self._edit_peer).pack(side="left", padx=2)
        ttk.Button(peer_btns, text="✕ Remove", command=self._remove_peer).pack(side="left", padx=2)

        # ── Tab 2: Service ──────────────────────────────────────
        srv_frame = ttk.Frame(nb)
        nb.add(srv_frame, text="Service")

        self.status_label = ttk.Label(srv_frame, text="Status: Stopped", font=("", 12, "bold"))
        self.status_label.pack(pady=12)

        self.btn_start = ttk.Button(srv_frame, text="Start Service", command=self._toggle_service)
        self.btn_start.pack(pady=4)

        log_frame = ttk.LabelFrame(srv_frame, text="Log")
        log_frame.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_text = tk.Text(log_frame, height=12, state="disabled", bg="#1e1e1e", fg="#d4d4d4")
        self.log_text.pack(fill="both", expand=True)

        # ── Tab 3: Hotkeys ──────────────────────────────────────
        hot_frame = ttk.Frame(nb)
        nb.add(hot_frame, text="Hotkeys")

        msg = (
            "Hotkeys are auto-assigned:\n\n"
            "  Ctrl+Alt+1  → Forward input to Peer 1\n"
            "  Ctrl+Alt+2  → Forward input to Peer 2\n"
            "  Ctrl+Alt+3  → Forward input to Peer 3\n"
            "  ...         \n"
            "  Ctrl+Alt+0  → Return to local input\n\n"
            "Custom hotkey bindings coming in v0.2.0"
        )
        ttk.Label(hot_frame, text=msg, justify="left").pack(padx=16, pady=16, anchor="nw")

    def _save_device(self) -> None:
        self.cfg["device_name"] = self.name_var.get().strip() or "unknown"
        try:
            self.cfg["port"] = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("Error", "Port must be a number")
            return
        save_config(self.cfg)
        messagebox.showinfo("Saved", "Device config saved.")

    def _refresh_peer_list(self) -> None:
        for row in self.peer_tree.get_children():
            self.peer_tree.delete(row)
        for p in self.cfg.get("peers", []):
            self.peer_tree.insert("", "end", values=(p["name"], p["host"], p.get("port", 45781)))

    def _add_peer(self) -> None:
        dlg = PeerDialog(self.root, "Add Peer")
        self.root.wait_window(dlg)
        if dlg.result:
            peers = self.cfg.setdefault("peers", [])
            peers.append(dlg.result)
            save_config(self.cfg)
            self._refresh_peer_list()

    def _edit_peer(self) -> None:
        sel = self.peer_tree.selection()
        if not sel:
            return
        idx = self.peer_tree.index(sel[0])
        peer = self.cfg["peers"][idx]
        dlg = PeerDialog(self.root, "Edit Peer", peer)
        self.root.wait_window(dlg)
        if dlg.result:
            self.cfg["peers"][idx] = dlg.result
            save_config(self.cfg)
            self._refresh_peer_list()

    def _remove_peer(self) -> None:
        sel = self.peer_tree.selection()
        if not sel:
            return
        idx = self.peer_tree.index(sel[0])
        del self.cfg["peers"][idx]
        save_config(self.cfg)
        self._refresh_peer_list()

    def _toggle_service(self) -> None:
        if self.service_proc is not None:
            self.service_proc.terminate()
            self.service_proc = None
            self.status_label.config(text="Status: Stopped")
            self.btn_start.config(text="Start Service")
            self._log("Service stopped")
        else:
            cfg = load_config()
            save_config(cfg)
            try:
                svc_path = os.path.join(os.path.dirname(__file__), "service.py")
                self.service_proc = subprocess.Popen(
                    [sys.executable, svc_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                self.status_label.config(text="Status: Running")
                self.btn_start.config(text="Stop Service")
                self._log("Service started")

                def reader() -> None:
                    for line in self.service_proc.stdout or []:
                        self._log(line.rstrip())

                threading.Thread(target=reader, daemon=True).start()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to start: {e}")

    def _log(self, msg: str) -> None:
        self.root.after(0, lambda: self._append_log(msg))

    def _append_log(self, msg: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _start_status_poller(self) -> None:
        def poll() -> None:
            while True:
                try:
                    cfg = load_config()
                    port = cfg.get("port", 45781)
                    sock = socket.create_connection(("127.0.0.1", port), timeout=1)
                    data = json.dumps({"type": "ping"}).encode()
                    sock.sendall(struct.pack("!I", len(data)) + data)
                    sock.close()
                    self.root.after(0, lambda: self.status_label.config(
                        text="Status: Service Reachable" if self.service_proc else "Status: Stopped"
                    ))
                except (ConnectionError, OSError):
                    pass
                __import__("time").sleep(3)

        threading.Thread(target=poll, daemon=True).start()

    def run(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self) -> None:
        if self.service_proc is not None:
            self.service_proc.terminate()
        self.root.destroy()


class PeerDialog(tk.Toplevel):
    def __init__(self, parent: tk.Widget, title: str, defaults: dict | None = None) -> None:
        super().__init__(parent)
        self.title(title)
        self.result: dict | None = None
        self.geometry("320x160")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="Name:").grid(row=0, column=0, sticky="w", pady=2)
        self.name_var = tk.StringVar(value=(defaults or {}).get("name", ""))
        ttk.Entry(f, textvariable=self.name_var, width=30).grid(row=0, column=1, pady=2)

        ttk.Label(f, text="Host:").grid(row=1, column=0, sticky="w", pady=2)
        self.host_var = tk.StringVar(value=(defaults or {}).get("host", ""))
        ttk.Entry(f, textvariable=self.host_var, width=30).grid(row=1, column=1, pady=2)

        ttk.Label(f, text="Port:").grid(row=2, column=0, sticky="w", pady=2)
        self.port_var = tk.StringVar(value=str((defaults or {}).get("port", 45781)))
        ttk.Entry(f, textvariable=self.port_var, width=10).grid(row=2, column=1, sticky="w", pady=2)

        btn_f = ttk.Frame(f)
        btn_f.grid(row=3, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btn_f, text="OK", command=self._ok).pack(side="left", padx=4)
        ttk.Button(btn_f, text="Cancel", command=self.destroy).pack(side="left", padx=4)

    def _ok(self) -> None:
        name = self.name_var.get().strip()
        host = self.host_var.get().strip()
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            import tkinter.messagebox as mb
            mb.showerror("Error", "Port must be a number", parent=self)
            return
        if not name or not host:
            import tkinter.messagebox as mb
            mb.showerror("Error", "Name and Host are required", parent=self)
            return
        self.result = {"name": name, "host": host, "port": port}
        self.destroy()


if __name__ == "__main__":
    FlowShiftGUI().run()
