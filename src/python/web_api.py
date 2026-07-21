"""
FlowShift Web API — localhost HTTP/SSE server for the React GUI.
"""
from __future__ import annotations

import base64
import io
import ipaddress
import json
import mimetypes
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

import clipboard_model as cbm
import flowshift_diagnostics as diag
import runtime_model as rm

API_PORT = 5000
API_HOST = "127.0.0.1"
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent.parent.resolve()
WEBGUI_ROOT = (_PROJECT_ROOT / "webgui").resolve()
WEBGUI_CONFIG = WEBGUI_ROOT / "config.json"
_SPA_FALLBACKS = {"", "/", "/index.html", "/display", "/settings", "/peers", "/dashboard", "/clipboard", "/diagnostics", "/log"}
GUI_DIR = WEBGUI_ROOT

_event_bus = None
_refs = {}
_server = None
_server_shutdown_event = threading.Event()

def publish_event(event):
    global _event_bus
    if _event_bus:
        _event_bus.publish(event)

class EventBus:
    MAX_HISTORY = 200

    def __init__(self):
        self._lock = threading.Lock()
        self._subs = []
        self._history = []

    def subscribe(self):
        q = queue.Queue(maxsize=256)
        with self._lock:
            for ev in self._history:
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    break
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def publish(self, event):
        dead = []
        with self._lock:
            self._history.append(event)
            if len(self._history) > self.MAX_HISTORY:
                self._history = self._history[-self.MAX_HISTORY:]
            for q in self._subs:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                try:
                    self._subs.remove(q)
                except ValueError:
                    pass


def init(**kwargs):
    global _refs
    _refs.update(kwargs)
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()


def _r(key):
    return _refs.get(key)


def _path_or_none(p):
    return str(p) if p is not None else None


def _webgui_port():
    try:
        if WEBGUI_CONFIG.is_file():
            with WEBGUI_CONFIG.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
            return int(cfg.get("port", 5000))
    except Exception:
        pass
    return 5000


def webgui_config_path():
    return str(WEBGUI_CONFIG)


def get_webgui_candidates():
    candidates = []
    env_dir = os.environ.get("FLOWSHIFT_WEBGUI_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    install_dir = _SCRIPT_DIR.parent.parent / "webgui"
    candidates.append(install_dir)
    candidates.append(_PROJECT_ROOT / "webgui" / "dist")
    candidates.append(_PROJECT_ROOT / "webgui")
    unique = []
    seen = set()
    for cand in candidates:
        try:
            rp = cand.resolve()
        except Exception:
            rp = cand
        key = str(rp).lower()
        if key not in seen:
            seen.add(key)
            unique.append(Path(rp))
    return unique


def resolve_webgui_root():
    for cand in get_webgui_candidates():
        try:
            if (cand / "index.html").is_file():
                return cand
        except Exception:
            continue
    return None


def _origin_allowed(origin):
    origin = str(origin or "").strip()
    if not origin:
        return False
    try:
        parsed = urlparse(origin)
        host = (parsed.hostname or "").lower()
        port = parsed.port or (80 if parsed.scheme == "http" else 443)
        allowed_ports = {API_PORT, _webgui_port()}
        if host in {"127.0.0.1", "localhost"} and port in allowed_ports:
            return True
    except Exception:
        pass
    return False


def _is_local_host_value(host):
    value = str(host or "").strip().lower()
    if not value:
        return False
    if value in {"localhost", "::1", "127.0.0.1", "0.0.0.0"}:
        return True
    if value.startswith("127."):
        return True
    if value in {str(os.environ.get("COMPUTERNAME", "")).strip().lower(), str(socket.gethostname()).strip().lower()}:
        return True
    try:
        local_ips = set()
        for ip in _get_local_ips():
            local_ips.add(str(ip).strip().lower())
        if value in local_ips:
            return True
    except Exception:
        pass
    return False


def safe_static_path(gui_dir, request_path: str):
    gui_dir = Path(gui_dir).resolve()
    raw = unquote(request_path or "")
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    if not raw or raw == "/":
        raw = "/index.html"
    rel = raw.lstrip("/")
    candidate = (gui_dir / rel).resolve()
    try:
        if gui_dir not in candidate.parents and candidate != gui_dir:
            return None
    except Exception:
        return None
    return candidate


def _webgui_not_installed_response(handler):
    handler._json({
        "ok": False,
        "error": "webgui_not_installed",
        "expected_paths": [str(p) for p in get_webgui_candidates()],
    }, 404)


def _normalize_runtime_peers(istate):
    if not istate:
        return []
    with istate.lock:
        peers_cfg = [rm.normalize_peer(p) for p in list(istate.config.get("peers", []))]
        live_identities = set()
        for link in istate.peers.values():
            if isinstance(link, dict):
                live_identities.add(str(link.get("identity", "")).strip())
                live_identities.update({str(a).strip() for a in link.get("aliases", set())})
        peer_rows = []
        for peer in peers_cfg:
            identity = peer_identity(peer)
            connected = identity in live_identities
            peer_rows.append({
                "identity": identity,
                "device_id": peer.get("device_id", ""),
                "display_name": peer.get("display_name", peer.get("name", peer.get("host", ""))),
                "name": peer.get("name", peer.get("display_name", peer.get("host", ""))),
                "host": peer.get("host", ""),
                "port": peer.get("port", 45781),
                "connected": connected,
            })
        return peer_rows


def _display_layout_snapshot(istate):
    if not istate:
        return rm.default_display_layout(), []
    with istate.lock:
        peers_cfg = [rm.normalize_peer(p) for p in list(istate.config.get("peers", []))]
        layout, warnings = rm.normalize_display_layout(istate.config.get("display_layout"), peers_cfg)
    return layout, warnings


def _log(level, msg):
    log_fn = _r("log")
    if log_fn:
        log_fn(level, msg)


def _resolve_peer(params):
    ident = params.get("profile", [None])[0]
    if ident:
        return ident
    istate = _r("istate")
    if istate:
        with istate.lock:
            return istate.active_peer
    return None


_CREATE_NO_WINDOW = 0x08000000

def _get_local_ips():
    fn = _r("get_local_ipv4s")
    if fn:
        try:
            return fn()
        except Exception:
            pass
    try:
        hostname = socket.gethostname()
        ips = list(set(socket.gethostbyname_ex(hostname)[2]))
        return [ip for ip in ips if not ip.startswith("127.")]
    except Exception:
        return []

def _get_broadcast_targets():
    targets = []
    seen = set()
    for ip in _get_local_ips():
        try:
            parts = ip.split(".")
            if len(parts) == 4:
                targets.append(f"{parts[0]}.{parts[1]}.{parts[2]}.255")
        except Exception:
            pass
    targets.append("255.255.255.255")
    targets = [t for t in targets if t not in seen and not seen.add(t)]
    return targets

def _scan_broadcast(port, timeout=1.5, local_device_id=""):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind(("0.0.0.0", 0))
    except Exception:
        pass
    msg = json.dumps({"type": "discover"}).encode("utf-8")
    targets = _get_broadcast_targets()
    for t in targets:
        try:
            sock.sendto(msg, (t, port))
        except Exception:
            pass
    found = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(4096)
            reply = json.loads(data.decode("utf-8"))
            if reply.get("type") == "discover_reply":
                remote_id = str(reply.get("device_id", "") or "").strip().lower()
                if remote_id and local_device_id and remote_id == str(local_device_id).strip().lower():
                    continue
                entry = {
                    "name": reply.get("display_name", ""),
                    "host": addr[0],
                    "port": reply.get("port", port),
                    "device_id": remote_id,
                }
                if entry not in found:
                    found.append(entry)
        except socket.timeout:
            break
        except Exception:
            pass
    sock.close()
    return found

def _auto_start_set(enabled):
    try:
        import ctypes
        advapi = ctypes.windll.advapi32
        KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
        AUTO_START_NAME = "FlowShift"
        hkey = ctypes.c_void_p()
        if enabled:
            script = os.path.abspath(os.path.join(os.path.dirname(__file__), "tray.py"))
            exe = sys.executable
            if exe.lower().endswith('python.exe'):
                exe = exe[:-4] + 'w.exe'
            if not os.path.exists(exe):
                exe = sys.executable
            cmd = f'"{exe}" "{script}" --tray'
            ret = advapi.RegOpenKeyExW(0x80000001, KEY, 0, 0x20006, ctypes.byref(hkey))
            if ret == 0:
                advapi.RegSetValueExW(hkey, AUTO_START_NAME, 0, 1, cmd, len(cmd)*2)
                advapi.RegCloseKey(hkey)
        else:
            ret = advapi.RegOpenKeyExW(0x80000001, KEY, 0, 0x20006, ctypes.byref(hkey))
            if ret == 0:
                advapi.RegDeleteValueW(hkey, AUTO_START_NAME)
                advapi.RegCloseKey(hkey)
    except Exception:
        pass

def _auto_start_get():
    try:
        import ctypes
        advapi = ctypes.windll.advapi32
        hkey = ctypes.c_void_p()
        ret = advapi.RegOpenKeyExW(0x80000001, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, 0x20001, ctypes.byref(hkey))
        if ret != 0:
            return False
        buf = ctypes.create_unicode_buffer(512)
        size = ctypes.c_ulong(ctypes.sizeof(buf))
        ret = advapi.RegQueryValueExW(hkey, "FlowShift", None, None, buf, ctypes.byref(size))
        advapi.RegCloseKey(hkey)
        return ret == 0
    except Exception:
        return False


def make_api_handler():
    class APIHandler(BaseHTTPRequestHandler):
        server_version = "FlowShiftAPI/1.0"

        def _cors(self):
            origin = self.headers.get("Origin")
            if _origin_allowed(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, msg, status=400):
            self._json({"error": msg}, status)

        def _read_body(self):
            length = int(self.headers.get("Content-Length", 0))
            if length:
                return json.loads(self.rfile.read(length))
            return {}

        def _params(self):
            return parse_qs(urlparse(self.path).query)

        def _path_parts(self):
            return [p for p in urlparse(self.path).path.split("/") if p]

        def log_message(self, fmt, *args):
            _log("DEBUG", f"web_api: {fmt % args}")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            parts = self._path_parts()
            params = self._params()

            try:
                if path == "/api/status":
                    bs = _r("build_status")
                    self._json(bs() if bs else {"error": "not ready"})

                elif path == "/api/settings":
                    istate = _r("istate")
                    cfg = dict(istate.config) if istate else {}
                    clip = cfg.pop("clipboard", None) or {}
                    if isinstance(clip, dict):
                        cfg.update(clip)
                    self._json(cfg)

                elif path == "/api/peers":
                    istate = _r("istate")
                    self._json({"ok": True, "peers": _normalize_runtime_peers(istate) if istate else []})

                elif path == "/api/clipboard/items":
                    ident = _resolve_peer(params)
                    if not ident:
                        self._error("no active profile")
                        return
                    mgr = _r("clip_mgr")
                    if not mgr:
                        self._error("clipboard manager not ready")
                        return
                    items = mgr.list_items(ident)
                    self._json({
                        "items": items,
                        "total_size": mgr.store(ident).total_size(),
                    })

                elif path.startswith("/api/clipboard/item/") and parts[-1] != "items":
                    item_id = parts[-1]
                    ident = _resolve_peer(params)
                    if not ident or not item_id:
                        self._error("missing profile or item_id")
                        return
                    mgr = _r("clip_mgr")
                    if not mgr:
                        self._error("clipboard manager not ready")
                        return
                    kind = mgr.item_kind(ident, item_id)
                    item = mgr.store(ident).get_item(item_id)
                    text = None
                    html_b64 = None
                    image_b64 = None
                    if kind in (cbm.KIND_TEXT,):
                        text = mgr.get_text(ident, item_id)
                    elif kind == cbm.KIND_HTML:
                        raw = mgr.get_html(ident, item_id)
                        if raw:
                            html_b64 = base64.b64encode(raw).decode("ascii")
                        text = (item or {}).get("preview_text", "")
                    elif kind in (cbm.KIND_IMAGE, cbm.KIND_GIF):
                        data = mgr.store(ident).get_data(item_id)
                        if data:
                            image_b64 = base64.b64encode(data).decode("ascii")
                    elif kind in (cbm.KIND_FILE, cbm.KIND_FILE_BATCH):
                        text = mgr.get_text(ident, item_id)
                    self._json({
                        "item": item,
                        "kind": kind,
                        "text": text,
                        "html_b64": html_b64,
                        "image_b64": image_b64,
                    })

                elif path == "/api/events":
                    self._handle_sse()

                elif path == "/api/clipboard/progress":
                    mgr = _r("clip_mgr")
                    self._json(mgr.progress_snapshot() if mgr else {})

                elif path == "/api/clipboard/preview":
                    ident = _resolve_peer(params)
                    item_id = params.get("item_id", [None])[0]
                    mgr = _r("clip_mgr")
                    if not ident or not item_id or not mgr:
                        self._error("missing params")
                        return
                    ppm = mgr.thumbnail_ppm(ident, item_id, 96)
                    if ppm:
                        self._json({"ppm_b64": base64.b64encode(ppm).decode("ascii")})
                    else:
                        self._error("no preview", 404)

                elif path.startswith("/api/clipboard/thumbnail/"):
                    item_id = parts[-1]
                    ident = _resolve_peer(params)
                    mgr = _r("clip_mgr")
                    if not ident or not item_id or not mgr:
                        self._error("missing params")
                        return
                    ppm = mgr.thumbnail_ppm(ident, item_id, 96)
                    if ppm:
                        self._json({"ppm_b64": base64.b64encode(ppm).decode("ascii")})
                    else:
                        self._error("no thumbnail", 404)

                elif path == "/api/hotkeys":
                    istate = _r("istate")
                    if istate:
                        hks = [
                            {"label": hk.label, "display": hk.display(), "action": hk.action}
                            for hk in istate.hotkeys
                        ]
                        self._json({"hotkeys": hks})
                    else:
                        self._json({"hotkeys": []})

                elif path == "/api/display/layout":
                    istate = _r("istate")
                    peers = _normalize_runtime_peers(istate) if istate else []
                    layout, warnings = _display_layout_snapshot(istate)
                    self._json({"ok": True, "layout": layout, "peers": peers, "warnings": warnings})

                elif path == "/api/auto-start":
                    self._json({"enabled": _auto_start_get()})

                elif path == "/api/webgui/config":
                    cfg = {}
                    if os.path.isfile(WEBGUI_CONFIG):
                        try:
                            with open(WEBGUI_CONFIG, "r", encoding="utf-8") as f:
                                cfg = json.load(f)
                        except Exception:
                            pass
                    self._json({"config": cfg, "path": WEBGUI_CONFIG})

                elif path.startswith("/api/"):
                    self._error("not found", 404)

                else:
                    self._serve_static(path)

            except Exception as e:
                _log("ERROR", f"web_api GET {path}: {e}\n{traceback.format_exc()}")
                self._error(str(e), 500)

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            parts = self._path_parts()
            params = self._params()

            try:
                if path == "/api/forwarding/activate":
                    body = self._read_body()
                    ident = body.get("profile", params.get("profile", [None])[0])
                    if not ident:
                        self._error("missing profile")
                        return
                    ap = _r("apply_profile")
                    if not ap:
                        self._error("forwarding not available")
                        return
                    ok, err = ap(ident, True)
                    if ok:
                        self._json({"ok": True})
                    else:
                        self._error(err)

                elif path == "/api/forwarding/deactivate":
                    de = _r("deactivate_forward")
                    if de:
                        de("web-api")
                    self._json({"ok": True})

                elif path == "/api/forwarding/toggle":
                    body = self._read_body()
                    ident = body.get("profile", params.get("profile", [None])[0])
                    if not ident:
                        self._error("missing profile")
                        return
                    ap = _r("apply_profile")
                    de = _r("deactivate_forward")
                    istate = _r("istate")
                    if not ap or not de or not istate:
                        self._error("forwarding not available")
                        return
                    with istate.lock:
                        active = istate.active
                    if active:
                        de("web-api")
                    else:
                        ok, err = ap(ident, True)
                        if not ok:
                            self._error(err)
                            return
                    self._json({"ok": True, "active": not active})

                elif path == "/api/settings":
                    body = self._read_body()
                    istate = _r("istate")
                    load_cfg = _r("load_config")
                    save_cfg = _r("save_config")
                    if not istate or not load_cfg or not save_cfg:
                        self._error("settings not available")
                        return
                    clip_keys = set(cbm.DEFAULT_CLIPBOARD_SETTINGS.keys()) if hasattr(cbm, 'DEFAULT_CLIPBOARD_SETTINGS') else set()
                    with istate.lock:
                        cfg = dict(istate.config)
                        clip_cfg = dict(cfg.get("clipboard") or {})
                        for k, v in body.items():
                            if v is None:
                                continue
                            if k in clip_keys:
                                clip_cfg[k] = v
                            else:
                                cfg[k] = v
                        cfg["clipboard"] = clip_cfg
                        istate.config = cfg
                    save_cfg(cfg)
                    self._json({"ok": True, "config": cfg})

                elif path == "/api/display/layout":
                    body = self._read_body()
                    new_layout = body.get("layout", {})
                    istate = _r("istate")
                    save_cfg = _r("save_config")
                    if istate and save_cfg:
                        with istate.lock:
                            cfg = dict(istate.config)
                            peers = list(cfg.get("peers", []))
                            normalized_peers = [rm.normalize_peer(p) for p in peers]
                            layout, warnings = rm.normalize_display_layout(new_layout, normalized_peers)
                            for edge_name, edge_cfg in (layout.get("edges") or {}).items():
                                if not edge_cfg or not edge_cfg.get("peer_identity"):
                                    continue
                                peer_identity = edge_cfg.get("peer_identity")
                                resolved = rm.find_peer_by_identity(peer_identity, normalized_peers)
                                if resolved is None:
                                    self._json({
                                        "ok": False,
                                        "error": "unknown_peer_identity",
                                        "details": {"edge": edge_name, "peer_identity": peer_identity},
                                    }, 400)
                                    return
                            cfg["display_layout"] = layout
                            istate.config = cfg
                        save_cfg(cfg)
                        publish_event({"type": "status_update"})
                        self._json({"ok": True, "layout": layout, "warnings": warnings})
                    else:
                        self._error("settings not available")

                elif path == "/api/peers/add":
                    body = self._read_body()
                    name = body.get("name", "").strip()
                    host = body.get("host", "").strip()
                    port = int(body.get("port", 45781))
                    if not host:
                        self._error("host is required")
                        return
                    if not name:
                        name = host
                    peer = {"name": name, "host": host, "port": port}
                    if body.get("device_id"):
                        peer["device_id"] = body["device_id"]
                    istate = _r("istate")
                    save_cfg = _r("save_config")
                    if not istate or not save_cfg:
                        self._error("settings not available")
                        return
                    local_cfg = rm.normalize_peer(istate.config)
                    if _is_local_host_value(host) or rm.normalize_peer(peer).get("device_id") == local_cfg.get("device_id") or rm.normalize_peer(peer).get("identity") == local_cfg.get("identity"):
                        self._error("peer points to the local device", 400)
                        return
                    with istate.lock:
                        cfg = dict(istate.config)
                        peers = list(cfg.get("peers", []))
                        peers.append(peer)
                        cfg["peers"] = peers
                        istate.config = cfg
                    save_cfg(cfg)
                    publish_event({"type": "status_update"})
                    self._json({"ok": True, "peer": peer})

                elif path == "/api/peers/remove":
                    body = self._read_body()
                    idx = body.get("index", -1)
                    name = body.get("name", "")
                    host = body.get("host", "")
                    istate = _r("istate")
                    save_cfg = _r("save_config")
                    if not istate or not save_cfg:
                        self._error("settings not available")
                        return
                    with istate.lock:
                        cfg = dict(istate.config)
                        peers = list(cfg.get("peers", []))
                        removed = None
                        if idx >= 0 and idx < len(peers):
                            removed = peers.pop(idx)
                        elif name:
                            peers = [p for p in peers if p.get("name") != name]
                            removed = True
                        elif host:
                            peers = [p for p in peers if p.get("host") != host]
                            removed = True
                        cfg["peers"] = peers
                        istate.config = cfg
                    save_cfg(cfg)
                    self._json({"ok": True, "removed": bool(removed)})

                elif path == "/api/peers/scan":
                    port = int(_r("istate").config.get("port", 45781)) if _r("istate") else 45781
                    local_device_id = _r("istate").config.get("device_id", "") if _r("istate") else ""
                    body = self._read_body()
                    timeout = float(body.get("timeout", 2.0))
                    found = _scan_broadcast(port, timeout, local_device_id)
                    self._json({"peers": found})

                elif path == "/api/auto-start":
                    body = self._read_body()
                    enabled = body.get("enabled")
                    if enabled is not None:
                        _auto_start_set(bool(enabled))
                    self._json({"enabled": _auto_start_get()})

                elif path == "/api/webgui/config":
                    body = self._read_body()
                    port = body.get("port")
                    if port is not None:
                        cfg = {}
                        if os.path.isfile(WEBGUI_CONFIG):
                            try:
                                with open(WEBGUI_CONFIG, "r", encoding="utf-8") as f:
                                    cfg = json.load(f)
                            except Exception:
                                pass
                        cfg["port"] = int(port)
                        os.makedirs(os.path.dirname(WEBGUI_CONFIG), exist_ok=True)
                        with open(WEBGUI_CONFIG, "w", encoding="utf-8") as f:
                            json.dump(cfg, f, indent=2)
                        self._json({"ok": True, "config": cfg})
                    else:
                        self._error("port is required")

                elif path in {"/api/restart", "/api/runtime/restart"}:
                    self._error("restart not available in runtime", 501)

                elif path in {"/api/shutdown", "/api/runtime/stop"}:
                    req = _r("request_shutdown")
                    if req:
                        self._json({"ok": True, "shutting_down": True})
                        threading.Thread(target=req, args=("web_gui",), daemon=True).start()
                    else:
                        self._error("shutdown not available")

                elif path == "/api/peers/edit":
                    body = self._read_body()
                    idx = body.get("index", -1)
                    istate = _r("istate")
                    save_cfg = _r("save_config")
                    if not istate or not save_cfg:
                        self._error("settings not available")
                        return
                    with istate.lock:
                        cfg = dict(istate.config)
                        peers = list(cfg.get("peers", []))
                        if idx < 0 or idx >= len(peers):
                            self._error("invalid index")
                            return
                        peer = dict(peers[idx])
                        if "name" in body:
                            peer["name"] = str(body["name"]).strip() or peer.get("host", "")
                        if "host" in body:
                            peer["host"] = str(body["host"]).strip()
                        if "port" in body:
                            peer["port"] = int(body["port"])
                        if "device_id" in body and body["device_id"]:
                            peer["device_id"] = str(body["device_id"])
                        if "mouse" in body and isinstance(body["mouse"], dict):
                            peer["mouse"] = body["mouse"]
                        if _is_local_host_value(peer.get("host", "")) or rm.normalize_peer(peer).get("device_id") == rm.normalize_peer(istate.config).get("device_id") or rm.normalize_peer(peer).get("identity") == rm.normalize_peer(istate.config).get("identity"):
                            self._error("peer points to the local device", 400)
                            return
                        peers[idx] = peer
                        cfg["peers"] = peers
                        istate.config = cfg
                    save_cfg(cfg)
                    publish_event({"type": "status_update"})
                    self._json({"ok": True, "peer": peer})

                elif path == "/api/peers/ping":
                    body = self._read_body()
                    peer_ref = body.get("peer", "")
                    if not peer_ref:
                        self._error("peer reference required")
                        return
                    ping_fn = _r("ping_peer")
                    if not ping_fn:
                        self._error("ping not available")
                        return
                    try:
                        result = ping_fn(peer_ref)
                        self._json(result)
                    except ValueError as e:
                        self._error(str(e))
                    except Exception as e:
                        self._error(f"ping failed: {e}")

                elif path == "/api/diagnostics":
                    istate = _r("istate")
                    bs_fn = _r("build_status")
                    mgr = _r("clip_mgr")
                    snapshot = diag.collect_environment_snapshot(
                        config=istate.config if istate else None,
                        runtime_state={
                            "status": bs_fn() if bs_fn else None,
                            "clipboard_progress": mgr.progress_snapshot() if mgr else {},
                            "source": "web_gui",
                        } if istate else None,
                    )
                    ok, problems = diag.diagnostics_ok(snapshot)
                    report = diag.format_diagnostics_report(snapshot)
                    self._json({
                        "ok": ok,
                        "problems": problems,
                        "report": report,
                        "diagnostics": snapshot,
                    })

                elif path == "/api/overlay/show":
                    body = self._read_body()
                    if not isinstance(body, dict):
                        self._error("request body must be an object")
                        return
                    payload = body.get("payload", {})
                    if not isinstance(payload, dict):
                        self._error("payload must be an object")
                        return
                    show = _r("request_overlay")
                    if not show:
                        self._json({"ok": False, "error": "overlay is not initialized"}, 503)
                        return
                    result = show(body.get("mode", "clipboard"), payload=payload, wait=True)
                    if isinstance(result, dict) and result.get("type") == "overlay_visible":
                        self._json({"ok": True, "result": result})
                    else:
                        reason = result.get("reason") if isinstance(result, dict) else None
                        self._json({"ok": False, "error": reason or "overlay unavailable"}, 503)

                elif path == "/api/overlay/hide":
                    hide = _r("hide_overlay")
                    if not hide:
                        self._json({"ok": False, "error": "overlay is not initialized"}, 503)
                        return
                    result = hide()
                    if isinstance(result, dict) and result.get("type") == "overlay_hidden":
                        self._json({"ok": True, "result": result})
                    else:
                        reason = result.get("reason") if isinstance(result, dict) else None
                        self._json({"ok": False, "error": reason or "overlay unavailable"}, 503)

                elif path == "/api/overlay/ping":
                    ping = _r("ping_overlay")
                    if not ping:
                        self._json({"ok": False, "error": "overlay is not initialized"}, 503)
                        return
                    result = ping()
                    if result is True:
                        self._json({"ok": True})
                    else:
                        self._json({"ok": False, "error": "overlay host unavailable"}, 503)

                elif path == "/api/inject/type":
                    body = self._read_body()
                    text = body.get("text", "")
                    if not text:
                        self._error("text is required")
                        return
                    istate = _r("istate")
                    if istate and hasattr(istate, "inject_queue"):
                        istate.inject_queue.put({"type": "type_text", "text": text})
                        self._json({"ok": True, "chars": len(text)})
                    else:
                        self._error("inject queue not available")

                elif path == "/api/inject/key":
                    body = self._read_body()
                    vk = int(body.get("vk", 0))
                    action = body.get("action", "tap")
                    if not vk:
                        self._error("vk code is required")
                        return
                    istate = _r("istate")
                    if istate and hasattr(istate, "inject_queue"):
                        ev = {"type": "key_up" if action == "up" else "key", "code": vk}
                        if action == "tap":
                            istate.inject_queue.put({"type": "key", "code": vk})
                            istate.inject_queue.put({"type": "key_up", "code": vk})
                        else:
                            istate.inject_queue.put(ev)
                        self._json({"ok": True})
                    else:
                        self._error("inject queue not available")

                elif path == "/api/clipboard/sync":
                    ident = _resolve_peer(params)
                    if not ident:
                        self._error("missing profile")
                        return
                    mgr = _r("clip_mgr")
                    if mgr:
                        mgr.send_manifest(ident)
                    self._json({"ok": True})

                elif path == "/api/clipboard/clear":
                    ident = _resolve_peer(params)
                    if not ident:
                        self._error("missing profile")
                        return
                    mgr = _r("clip_mgr")
                    if mgr:
                        mgr.clear(ident)
                    self._json({"ok": True})

                elif path.startswith("/api/clipboard/item/") and path.endswith("/paste"):
                    item_id = parts[-2]
                    ident = _resolve_peer(params)
                    if not ident or not item_id:
                        self._error("missing params")
                        return
                    mgr = _r("clip_mgr")
                    istate = _r("istate")
                    if not mgr:
                        self._error("clipboard manager not ready")
                        return
                    kind = mgr.item_kind(ident, item_id)
                    if kind in (cbm.KIND_FILE, cbm.KIND_FILE_BATCH):
                        dest_root = _r("clipboard_root")
                        import os as _os
                        dest_root = dest_root or _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "temp")
                        result = mgr.materialize_files_result(ident, item_id, dest_root)
                        paths = result.get("paths") if result.get("ok") else None
                        if paths:
                            import clipboard_win
                            ok_set = clipboard_win.set_files(paths)
                            self._json({"set": bool(ok_set), "kind": kind, "count": len(paths)})
                        else:
                            self._error(result.get("error") or "file data not present")
                    elif kind in (cbm.KIND_IMAGE, cbm.KIND_GIF):
                        data = mgr.store(ident).get_data(item_id)
                        if data:
                            import clipboard_win
                            ok_set = clipboard_win.set_image(data)
                            self._json({"set": bool(ok_set), "kind": kind})
                        else:
                            self._error("image not present")
                    elif kind == cbm.KIND_HTML:
                        raw = mgr.get_html(ident, item_id)
                        if raw:
                            import clipboard_win
                            item = mgr.store(ident).get_item(item_id)
                            preview_text = (item.get("preview_text") if item else "") or ""
                            ok_set = clipboard_win.set_html(raw, preview_text or None)
                            self._json({"set": bool(ok_set), "kind": "html"})
                        else:
                            self._error("html not present")
                    else:
                        text = mgr.get_text(ident, item_id)
                        if text is not None:
                            import clipboard_win
                            ok_set = clipboard_win.set_text(text)
                            self._json({"set": bool(ok_set), "kind": "text"})
                        else:
                            self._error("no data")

                elif path.startswith("/api/clipboard/item/") and path.endswith("/pin"):
                    item_id = parts[-2]
                    body = self._read_body()
                    pinned = body.get("pinned", True)
                    ident = _resolve_peer(params)
                    if not ident or not item_id:
                        self._error("missing params")
                        return
                    mgr = _r("clip_mgr")
                    if mgr:
                        ok_pin = mgr.set_pinned(ident, item_id, bool(pinned))
                        self._json({"pinned": bool(ok_pin)})
                    else:
                        self._error("clipboard manager not ready")

                elif path.startswith("/api/clipboard/item/") and path.endswith("/request"):
                    item_id = parts[-2]
                    ident = _resolve_peer(params)
                    if not ident or not item_id:
                        self._error("missing params")
                        return
                    mgr = _r("clip_mgr")
                    if mgr:
                        mgr.request_items(ident, [item_id], reason="web_gui")
                        self._json({"ok": True})
                    else:
                        self._error("clipboard manager not ready")

                elif path.startswith("/api/clipboard/item/"):
                    item_id = parts[-1]
                    ident = _resolve_peer(params)
                    if not ident or not item_id:
                        self._error("missing params")
                        return
                    mgr = _r("clip_mgr")
                    if mgr:
                        ok_del = mgr.delete_item(ident, item_id)
                        self._json({"deleted": bool(ok_del)})
                    else:
                        self._error("clipboard manager not ready")

                elif path.startswith("/api/"):
                    self._error("not found", 404)

                else:
                    self._error("not found", 404)

            except Exception as e:
                _log("ERROR", f"web_api POST {path}: {e}\n{traceback.format_exc()}")
                self._error(str(e), 500)

        def do_DELETE(self):
            self.do_POST()

        def _handle_sse(self):
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            eb = _event_bus
            if eb:
                sub = eb.subscribe()
            else:
                sub = None
            try:
                while not _server_shutdown_event.is_set():
                    if sub is not None:
                        try:
                            event = sub.get(timeout=15)
                            line = f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                            self.wfile.write(line.encode("utf-8"))
                            self.wfile.flush()
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                    else:
                        time.sleep(1)
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                if sub is not None and eb:
                    eb.unsubscribe(sub)

        def _serve_static(self, path):
            root = resolve_webgui_root()
            file_path = safe_static_path(root, path) if root else None
            if file_path is None and root is not None:
                self._error("forbidden", 403)
                return
            if file_path and file_path.is_file():
                content_type, _ = mimetypes.guess_type(str(file_path))
                if content_type is None:
                    content_type = "application/octet-stream"
                with file_path.open("rb") as f:
                    data = f.read()
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
            else:
                if root is None:
                    _webgui_not_installed_response(self)
                    return
                is_spa_route = path in _SPA_FALLBACKS or not Path(str(path).lstrip("/")).suffix
                index = root / "index.html"
                if is_spa_route and index.is_file():
                    with index.open("rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self._cors()
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._error("not found", 404)

    return APIHandler


def start_api_server(host=API_HOST, port=API_PORT):
    global _server
    handler = make_api_handler()
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer((host, port), handler)
    _server = server
    _server_shutdown_event.clear()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        try:
            server.server_close()
        finally:
            if _server is server:
                _server = None


def shutdown_api_server():
    srv = _server
    if not srv:
        return False
    _server_shutdown_event.set()

    def _stop():
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass

    threading.Thread(target=_stop, daemon=True).start()
    return True
