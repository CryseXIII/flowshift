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
import re
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

import autostart
import clipboard_model as cbm
import config_schema as config_store
import event_log
import flowshift_diagnostics as diag
import hotkey_status as hks
import input_trigger as it
import lan_scan
import overlay_actions as oact
import peer_discovery as pdisc
import remote_preview as rprev
import runtime_model as rm
import update_credentials
import update_model
import usb_package
import webgui_health
from update_state import DOWNLOADED

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

_UPDATE_OPERATION_RESULTS = {
    "queued": 202,
    "already_running": 409,
    "blocked": 409,
    "not_available": 409,
    "shutting_down": 503,
}
_UPDATE_SETTING_KEYS = frozenset({"enabled", "check_on_start", "channel", "policy"})
# Settings > Software Update > "Updates from connected devices" (peer_update.py).
_PEER_UPDATE_SETTING_KEYS = frozenset({"policy", "allow_development"})
_PEER_UPDATE_ACTIONS = frozenset({"download", "install", "cancel"})
_UPDATE_REQUEST_MAX_BYTES = 65_536
_CLIPBOARD_TEXT_REQUEST_MAX_BYTES = 1_024
# File-card previews: item ids as in the card contract, cached images only
# (size limit: file_preview_response_max_mb in the clipboard settings).
_FILE_PREVIEW_ITEM_ID = cbm._SAFE_ITEM_ID
_CLIPBOARD_DELETE_MAX_IDS = 10_000
_CLIPBOARD_DELETE_REQUEST_MAX_BYTES = 1_048_576
_LOCAL_REQUEST_MAX_BYTES = 65_536
_ACTION_REQUEST_MAX_BYTES = 16_384

def publish_event(event, keep_history=True):
    global _event_bus
    if _event_bus:
        _event_bus.publish(event, keep_history=keep_history)


def publish_log_entry(entry):
    """A recorded log line (``event_log``) to the open Event Log pages.

    Not kept in the bus history: the page loads earlier lines through
    ``GET /api/logs``, a replay would show them twice.
    """
    if entry:
        publish_event(event_log.log_event(entry), keep_history=False)

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

    def is_subscribed(self, q):
        with self._lock:
            return q in self._subs

    def unsubscribe(self, q):
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def publish(self, event, keep_history=True):
        dead = []
        with self._lock:
            if keep_history:
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


def _update_settings_snapshot():
    istate = _r("istate")
    if istate is None:
        value = None
    else:
        with istate.lock:
            value = istate.config.get("updates")
    normalized = config_store.normalize_updates(value)
    return {key: normalized[key] for key in _UPDATE_SETTING_KEYS}


def _update_install_safety():
    query = _r("update_install_safety")
    try:
        value = query() if query else None
    except Exception:
        value = None
    if not isinstance(value, dict) or type(value.get("safe")) is not bool:
        return {
            "safe": False,
            "reason": "runtime_state_unknown",
            "blockers": ["runtime_state_unknown"],
        }
    reason = value.get("reason")
    blockers = value.get("blockers")
    return {
        "safe": value["safe"],
        "reason": reason if isinstance(reason, str) and reason else (
            "safe" if value["safe"] else "runtime_state_unknown"),
        "blockers": [str(item) for item in blockers if item]
        if isinstance(blockers, list) else [],
    }


def _development_access_status():
    store = _r("update_credentials")
    if store is None:
        return {"configured": False, "secure_store": False, "error": "unavailable"}
    return update_credentials.access_status(store)


def _usb_package_payload():
    """USB package button state: only for an installed PC on the development channel."""
    job = _r("usb_package_job")
    manager = _r("update_manager")
    development_mode = False
    if manager is not None:
        try:
            development_mode = manager.snapshot().get("development_mode") is True
        except Exception:
            development_mode = False
    if job is None:
        blocked = "not_available"
    elif development_mode:
        blocked = "development_checkout"
    elif _update_settings_snapshot()["channel"] != update_model.CHANNEL_DEVELOPMENT:
        blocked = "channel_not_development"
    else:
        blocked = None
    return {
        "ok": True,
        "available": blocked is None,
        "blocked_reason": blocked,
        "package": job.status() if job is not None else None,
    }


def _update_status_payload(manager):
    snapshot = manager.snapshot()
    settings = _update_settings_snapshot()
    source = update_model.CHANNEL_SOURCES.get(
        settings["channel"], update_model.CHANNEL_SOURCES[update_model.CHANNEL_STABLE])
    safety = _update_install_safety()
    state = snapshot.get("state")
    downloaded_asset = snapshot.get("downloaded_asset")
    development_mode = snapshot.get("development_mode") is True
    manager_allows_install = snapshot.get("can_install") is True
    downloaded = state == DOWNLOADED and isinstance(downloaded_asset, dict)
    operation_active = bool(snapshot.get("active_operation") or snapshot.get("install_pending"))
    can_install = bool(
        downloaded
        and manager_allows_install
        and not development_mode
        and not operation_active
        and not snapshot.get("shutting_down")
        and safety["safe"]
    )
    if development_mode:
        blocked_reason = "development_checkout"
    elif snapshot.get("shutting_down"):
        blocked_reason = "runtime_shutting_down"
    elif not downloaded:
        blocked_reason = "update_not_downloaded"
    elif operation_active:
        blocked_reason = "update_operation_active"
    elif not manager_allows_install:
        blocked_reason = snapshot.get("blocked_reason") or "install_unavailable"
    elif not safety["safe"]:
        blocked_reason = safety["reason"]
    else:
        blocked_reason = None
    return {
        "state": state,
        "current_version": snapshot.get("current_version"),
        "latest_version": snapshot.get("latest_version"),
        "channel": settings["channel"],
        "source": dict(source),
        "checked_channel": snapshot.get("checked_channel"),
        "development_access": _development_access_status(),
        "policy": settings["policy"],
        "enabled": settings["enabled"],
        "check_on_start": settings["check_on_start"],
        "last_check_at": snapshot.get("last_check_at"),
        "last_successful_check_at": snapshot.get("last_successful_check_at"),
        "release_notes": snapshot.get("release_notes") or "",
        "release_url": snapshot.get("release_url"),
        "downloaded_asset": downloaded_asset,
        "progress": snapshot.get("download_progress"),
        "operation_active": operation_active,
        "can_install": can_install,
        "blocked_reason": blocked_reason,
        # The automatic policy does not repeat an update that just failed and
        # was rolled back; a click on Install still can.
        "automatic_install_held": snapshot.get("automatic_install_held") is True,
        "development_mode": development_mode,
        "last_error": snapshot.get("last_error"),
        "last_update_result": snapshot.get("last_update_result"),
        # Outcome of the last install run (versions, result, plain cause) for
        # the explanation in Software Update; the raw error stays in update.log.
        "last_install": snapshot.get("last_install")
        if isinstance(snapshot.get("last_install"), dict) else None,
        "recovery_notices": snapshot.get("recovery_notices")
        if isinstance(snapshot.get("recovery_notices"), list) else [],
    }


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


def webgui_health_report(app_version):
    """Health of the WebGUI this server actually serves (see ``webgui_health``)."""
    return webgui_health.check_webgui(resolve_webgui_root(), app_version)


def _origin_allowed(origin, server_port=None):
    origin = str(origin or "").strip()
    if not origin:
        return False
    try:
        parsed = urlparse(origin)
        host = (parsed.hostname or "").lower()
        port = parsed.port or (80 if parsed.scheme == "http" else 443)
        allowed_ports = {API_PORT, _webgui_port(), 5173}
        if server_port:
            allowed_ports.add(int(server_port))
        if (parsed.scheme == "http" and host in {"127.0.0.1", "localhost"}
                and port in allowed_ports):
            return True
    except Exception:
        pass
    return False


def _local_host_header_allowed(value, expected_port):
    try:
        parsed = urlparse(f"//{str(value or '').strip()}")
        return (
            parsed.username is None
            and parsed.password is None
            and (parsed.hostname or "").lower() in {"127.0.0.1", "localhost"}
            and parsed.port == int(expected_port)
        )
    except (TypeError, ValueError):
        return False


def _write_request_policy(path):
    """Message label and body limit of a writing request.

    Every POST and DELETE passes ``request_rejection`` first and then this
    limit, both before its body is read, so a web page in the user's browser
    cannot trigger actions, input injection or configuration changes on the
    local API and the action routes only take small bodies.
    """
    if path.startswith("/api/update/"):
        return "update", _UPDATE_REQUEST_MAX_BYTES
    if path == "/api/clipboard/delete":
        return "clipboard", _CLIPBOARD_DELETE_REQUEST_MAX_BYTES
    if path.startswith("/api/clipboard/item/") and path.endswith("/copy-text"):
        return "clipboard", _CLIPBOARD_TEXT_REQUEST_MAX_BYTES
    if path.startswith("/api/clipboard/"):
        return "clipboard", _LOCAL_REQUEST_MAX_BYTES
    if path in {"/api/actions/execute", "/api/actions/wheel"}:
        return "action", _ACTION_REQUEST_MAX_BYTES
    return "local API", _LOCAL_REQUEST_MAX_BYTES


# One guard for every request that changes state or triggers something: all
# POST and DELETE requests pass it before any route runs, so a new route cannot
# skip it. A web page open in the browser can otherwise reach this loopback API
# (cross-site request, DNS rebinding) and inject keys or run wheel actions.
GUARDED_METHODS = frozenset({"POST", "DELETE"})


def request_rejection(method, path, headers, server_port):
    """``(status, error_code, message)`` when the request is refused, else ``None``.

    Every ``/api/`` request needs the local Host header (DNS rebinding).
    POST and DELETE additionally need the Origin of the own WebGUI (browsers
    always send it for these methods; the native Command Wheel sends its
    overlay origin) and a JSON body type, which a foreign page can only send
    after a CORS preflight that this server never grants it.
    """
    method = str(method or "").upper()
    if method not in GUARDED_METHODS and not str(path or "").startswith("/api/"):
        return None
    if not _local_host_header_allowed(headers.get("Host"), server_port):
        return 403, "host_not_allowed", "requests require the local FlowShift host"
    if method not in GUARDED_METHODS:
        return None
    origin = headers.get("Origin")
    if not str(origin or "").strip():
        return 403, "origin_required", "request origin is required"
    if not _origin_allowed(origin, server_port):
        return 403, "origin_not_allowed", "request origin is not allowed"
    if headers.get("Transfer-Encoding"):
        return 400, "chunked_not_supported", "chunked requests are not supported"
    if headers.get_content_type().lower() != "application/json":
        return 415, "json_required", "requests require application/json"
    try:
        length = int(headers.get("Content-Length", 0))
    except (TypeError, ValueError):
        length = -1
    if length < 0:
        return 400, "invalid_content_length", "invalid Content-Length"
    return None


def _log_safe_path(path):
    """Route path for log lines: no query, no control characters, bounded."""
    text = str(path or "").split("?", 1)[0]
    return "".join(ch if ch.isprintable() else "?" for ch in text)[:120]


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
        "message": "The FlowShift WebGUI is not installed.",
        "action": webgui_health.REPAIR_ACTION,
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
            identity = rm.peer_identity(peer)
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


def _log_folder_info():
    """Folder of ``flowshift.log`` for the Event Log page (user asked for it)."""
    folder = _r("log_dir")
    return {"log_folder": str(folder) if folder else "", "log_file": "flowshift.log"}


def _log_entries_payload(after=0):
    buffer = _r("log_buffer") or event_log.BUFFER
    return {
        "ok": True,
        "entries": buffer.entries(after),
        "capacity": buffer.capacity,
        **_log_folder_info(),
    }


def _quiet_request(method, code):
    """A successful read (GET/HEAD/OPTIONS below 400) gets no access-log line."""
    try:
        status = int(getattr(code, "value", code))
    except (TypeError, ValueError):
        return False
    return str(method or "").upper() in {"GET", "HEAD", "OPTIONS"} and status < 400


def _clipboard_profile_rows(istate):
    """Local history first, then configured peers — identities only, no paths."""
    rows = [{"identity": cbm.LOCAL_CLIPBOARD_IDENTITY,
             "label": cbm.LOCAL_CLIPBOARD_LABEL, "connected": True}]
    rows.extend(_normalize_runtime_peers(istate))
    return rows


def _resolve_peer(params):
    ident = params.get("profile", [None])[0]
    if ident:
        return ident
    istate = _r("istate")
    if istate:
        with istate.lock:
            if istate.active_peer:
                return istate.active_peer
    # Local history is always available, even with no peer configured.
    return cbm.LOCAL_CLIPBOARD_IDENTITY


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _with_display(trigger):
    return None if trigger is None else dict(trigger, display=it.display(trigger))


def _wheel_response(cfg):
    wheel = oact.wheel_config_from(cfg)
    return {
        "pages": wheel["pages"],
        # ``hotkey`` is the one trigger (key or mouse button), null when cleared.
        "hotkey": _with_display(wheel["hotkey"]),
        "trigger": dict(wheel["trigger"]),
        "trigger_limits": {"hold_ms_min": oact.HOLD_MS_MIN, "hold_ms_max": oact.HOLD_MS_MAX,
                           "mouse_buttons": list(it.MOUSE_BUTTONS)},
    }


# Config blocks with their own save route. ``POST /api/settings`` ignores them:
# the Settings page posts back everything it loaded, and a stale copy must not
# undo a change saved through the other route in the meantime.
SETTINGS_FOREIGN_KEYS = frozenset({"command_wheel", "peers", "display_layout", "updates"})

# How long a save waits for the window thread to re-register the hotkeys.
REGISTRATION_WAIT_S = 1.5


def _hotkey_report_seq():
    get = _r("hotkey_report")
    if get is None:
        return None
    try:
        return (get() or {}).get("seq", 0)
    except Exception:
        return None


def _reload_and_report(before_seq, slots, timeout=None):
    """Re-register the hotkeys and return what Windows said for ``slots``.

    ``{"pending": bool, "entries": [...], "inactive": [message, ...]}``;
    ``None`` without a runtime report (offline tests, legacy runtime). The
    stored choice is never changed here: an inactive hotkey stays saved and
    the WebGUI says in plain text why it does not work.
    """
    reload_hotkeys = _r("reload_hotkeys")
    started = False
    if reload_hotkeys:
        try:
            started = reload_hotkeys() is not False
        except Exception as exc:  # pragma: no cover - runtime only
            _log("WARN", f"hotkey reload failed: {exc}")
    get = _r("hotkey_report")
    if get is None or before_seq is None:
        return None
    report = {}
    deadline = time.monotonic() + (REGISTRATION_WAIT_S if timeout is None else timeout)
    while started:
        report = get() or {}
        if report.get("seq", 0) != before_seq:
            break
        if time.monotonic() >= deadline:
            started = False
            break
        time.sleep(0.05)
    if not started:
        return {"pending": True, "entries": [], "inactive": []}
    entries = [dict(e) for e in report.get("entries", []) if e.get("slot") in slots]
    return {"pending": False, "entries": entries,
            "inactive": [e.get("message", "") for e in entries if not e.get("active")]}


def _trigger_conflicts(cfg):
    """Triggers assigned twice in ``cfg`` (wheel, clipboard overlay, peer hotkeys)."""
    cs = cbm.clipboard_settings(cfg)
    return it.find_conflicts(hks.collect_assignments(
        oact.wheel_triggers(oact.wheel_config_from(cfg)), hks.clipboard_overlay_hotkey(cs),
        cfg.get("hotkeys") or [], clipboard_enabled=cs.get("enabled", True) is not False))


_CREATE_NO_WINDOW = 0x08000000

def _merge_remote_preview(current, value):
    """``(settings, invalid_fields)`` for a ``remote_preview`` part of ``POST /api/settings``.

    Given fields are merged onto the stored settings (``window`` per key); any
    invalid or unknown field refuses the whole request instead of falling back
    to a default.
    """
    stored = rprev.normalize_settings(current)
    if not isinstance(value, dict):
        return stored, ["remote_preview"]
    merged = dict(stored)
    merged.update(value)
    if isinstance(value.get("window"), dict):
        merged["window"] = {**stored["window"], **value["window"]}
    errors = []
    settings = rprev.normalize_settings(merged, errors)
    return settings, errors


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

def _scan_adapters():
    """IPv4 adapters with prefix length and name (``lan_scan.network_entry`` rows)."""
    fn = _r("get_local_ipv4_networks")
    if fn:
        try:
            adapters = lan_scan.adapter_networks(fn())
            if adapters:
                return adapters
        except Exception:
            pass
    # Without the mask a /24 is assumed; the result says so (mask_known).
    return lan_scan.adapter_networks(_get_local_ips())

def _scan_broadcast(port, timeout=lan_scan.DEFAULT_TIMEOUT, local_device_id="",
                    unicast_targets=(), searched=None):
    """Broadcast scan, one socket per adapter, plus a direct request to ``unicast_targets``.

    ``unicast_targets`` are ``{"host", "via"}`` (Tailscale devices, VPN network
    addresses, ``peer_discovery.collect_unicast_targets``), because broadcasts
    do not pass a VPN tunnel. A device answering several ways is listed once;
    ``via`` says how it answered. Appends the adapter rows to ``searched``.
    """
    found, rows = lan_scan.scan(port, _scan_adapters(), timeout, local_device_id,
                                unicast=unicast_targets)
    if searched is not None:
        searched.extend(rows)
    return found


def _unicast_targets():
    """Direct scan targets and what was asked (patched in tests)."""
    try:
        return pdisc.collect_unicast_targets()
    except Exception:
        return [], {"tailscale": {"available": False, "asked": 0},
                    "vpn": {"interfaces": [], "asked": 0}}


_last_tailscale_self = {}


def _this_pc(istate):
    """Name, device ID, port and addresses of this PC, for typing in on the other PC."""
    cfg = istate.config if istate else {}
    addresses = []
    for ip in _get_local_ips():
        ip = str(ip).strip()
        address = pdisc._ipv4(ip)
        if address is None:
            continue
        addresses.append({"address": ip,
                          "kind": "tailscale" if address in pdisc._CGNAT else "local"})
    info = {"name": cfg.get("device_name", ""), "device_id": cfg.get("device_id", ""),
            "port": int(cfg.get("port", 45781) or 45781), "addresses": addresses}
    if _last_tailscale_self.get("dns_name"):
        info["tailscale_name"] = _last_tailscale_self["dns_name"]
    return info


def _add_peer_to_config(peer):
    """Append ``peer`` to the configured peers; ``(peer, None)`` or ``(None, (error, status))``."""
    istate = _r("istate")
    save_cfg = _r("save_config")
    if not istate or not save_cfg:
        return None, ("settings not available", 400)
    local_cfg = rm.normalize_peer(istate.config)
    normalized = rm.normalize_peer(peer)
    if _is_local_host_value(peer.get("host", "")) or normalized.get("device_id") == local_cfg.get("device_id") or normalized.get("identity") == local_cfg.get("identity"):
        return None, ("peer points to the local device", 400)
    with istate.lock:
        cfg = dict(istate.config)
        peers = list(cfg.get("peers", []))
        peers.append(peer)
        cfg["peers"] = peers
        istate.config = cfg
    save_cfg(cfg)
    publish_event({"type": "status_update"})
    return peer, None


# Settings > Management > "Auto-start with Windows" and the tray tick share
# autostart.py: both read the real Windows state and switch it at once.
def _auto_start_set(enabled):
    """Switch autostart; raises autostart.AutoStartError when Windows did not take it."""
    autostart.set_enabled(enabled)
    _log("INFO", f"auto-start with Windows switched {'on' if enabled else 'off'} in the WebGUI")

def _auto_start_get():
    return autostart.is_enabled()


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

        def _read_object_body(self):
            try:
                body = self._read_body()
            except (json.JSONDecodeError, UnicodeError, ValueError):
                self._error("request body must be valid JSON", 400)
                return None
            if not isinstance(body, dict):
                self._error("request body must be an object", 400)
                return None
            return body

        def _guard_request(self, method):
            """Run ``request_rejection``; answer and log a refusal without content."""
            path = urlparse(self.path).path
            rejection = request_rejection(
                method, path, self.headers, self.server.server_address[1])
            if rejection is None:
                return True
            status, code, message = rejection
            _log("WARN", f"web_api rejected {method} {_log_safe_path(path)} reason={code}")
            self.close_connection = True
            self._json({"ok": False, "error": message, "error_code": code}, status)
            return False

        def _authorize_local_write(self, label, max_bytes):
            """Body limit of a writing request; runs before the body is read.

            Host, Origin, Transfer-Encoding and content type were checked by
            ``_guard_request``; this adds the per-route limit from
            ``_write_request_policy`` (small for execute and wheel).
            """
            if int(self.headers.get("Content-Length", 0)) > max_bytes:
                self._error(f"{label} request body is too large", 413)
                return False
            return True

        def _update_manager(self, operation=None):
            manager = _r("update_manager")
            if manager is None:
                response = {"ok": False, "status": "not_available",
                            "message": "Update manager is not available"}
                if operation:
                    response["operation"] = operation
                self._json(response, 503)
                return None
            return manager

        def _empty_update_operation_body(self, operation):
            body = self._read_object_body()
            if body is None:
                return False
            if body:
                self._json({
                    "ok": False,
                    "status": "blocked",
                    "operation": operation,
                    "message": "Update operation request body must be empty",
                    "error_code": "request_body_not_allowed",
                }, 400)
                return False
            return True

        def _send_update_result(self, result, operation):
            value = result.to_dict() if hasattr(result, "to_dict") else result
            if not isinstance(value, dict):
                self._json({
                    "ok": False,
                    "status": "blocked",
                    "operation": operation,
                    "message": "Update manager returned an invalid result",
                }, 500)
                return
            status = value.get("status")
            if status not in _UPDATE_OPERATION_RESULTS:
                self._json({
                    "ok": False,
                    "status": "blocked",
                    "operation": operation,
                    "message": "Update manager returned an unknown result",
                }, 500)
                return
            response = {
                "ok": status == "queued",
                "status": status,
                "operation": value.get("operation") or operation,
                "message": value.get("message") or "",
            }
            for key in ("error_code", "blocker"):
                if value.get(key) is not None:
                    response[key] = value[key]
            self._json(response, _UPDATE_OPERATION_RESULTS[status])

        def _file_preview_item(self, params, item_id):
            """Stored file item plus the preview service, or an error response."""
            ident = _resolve_peer(params)
            mgr = _r("clip_mgr")
            previews = _r("file_previews")
            if not ident or not mgr or previews is None:
                self._error("file previews not available", 503)
                return None, None
            if not _FILE_PREVIEW_ITEM_ID.fullmatch(item_id or ""):
                self._error("invalid item_id", 400)
                return None, None
            stored = mgr.store(ident).get_item(item_id)
            if not stored or stored.get("kind") not in (
                    cbm.KIND_FILE, cbm.KIND_FILE_BATCH, cbm.KIND_IMAGE, cbm.KIND_GIF):
                self._error("no file or image item", 404)
                return None, None
            return stored, previews

        def _clip_image_loader(self, params, item_id):
            """Reads a picture item's stored bytes later, on the preview thread."""
            ident = _resolve_peer(params)
            mgr = _r("clip_mgr")
            return lambda: mgr.store(ident).get_data(item_id)

        def _serve_file_preview(self, params, item_id, index, variant):
            # Only cached thumbnails and, for videos, the copied file itself
            # (muted hover clip). URLs carry item id + index, never a path.
            if variant not in ("still", "anim", "video"):
                self._error("unknown preview variant", 404)
                return
            stored, previews = self._file_preview_item(params, item_id)
            if stored is None:
                return
            if variant == "video":
                source = previews.video_source(stored, index)
                if not source:
                    self._error("no video preview", 404)
                    return
                self._send_file_range(*source)
                return
            found = previews.resolve(stored, index, variant, params.get("v", [None])[0])
            if not found:
                self._error("no preview", 404)
                return
            path, mime = found
            max_bytes = previews.settings()["response_max_bytes"]
            with open(path, "rb") as handle:
                body = handle.read(max_bytes + 1)
            if len(body) > max_bytes:
                self._error("preview too large", 500)
                return
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            # The URL names the cache key, so the image never changes under it.
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            self.wfile.write(body)

        def _send_file_range(self, path, mime, size):
            """Stream a byte range of a local file (HTTP Range, one range)."""
            start, end = 0, size - 1
            status = 200
            header = self.headers.get("Range") or ""
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
            if match and size > 0 and (match.group(1) or match.group(2)):
                if match.group(1):
                    start = int(match.group(1))
                    end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
                else:
                    start = max(0, size - int(match.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self._cors()
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            length = max(0, end - start + 1)
            try:
                handle = open(path, "rb")
            except OSError:
                self._error("video not readable", 404)
                return
            with handle:
                handle.seek(start)
                self.send_response(status)
                self._cors()
                self.send_header("Content-Type", mime)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                remaining = length
                try:
                    while remaining > 0:
                        chunk = handle.read(min(256 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass                     # the clip stopped on mouse leave

        def _params(self):
            return parse_qs(urlparse(self.path).query)

        def _path_parts(self):
            return [p for p in urlparse(self.path).path.split("/") if p]

        def log_message(self, fmt, *args):
            _log("DEBUG", f"web_api: {fmt % args}")

        def log_request(self, code="-", size="-"):
            # The WebGUI polls status, hotkeys and more every few seconds: a
            # successful read is not logged (it filled flowshift.log with 19 MB).
            # Changes (POST/DELETE) and every error still are.
            if _quiet_request(self.command, code):
                return
            super().log_request(code, size)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            if not self._guard_request("GET"):
                return
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            parts = self._path_parts()
            params = self._params()

            try:
                if path == "/api/status":
                    bs = _r("build_status")
                    self._json(bs() if bs else {"error": "not ready"})

                elif path == "/api/update/status":
                    manager = self._update_manager()
                    if manager is not None:
                        self._json({"ok": True, "update": _update_status_payload(manager)})

                elif path == "/api/update/usb-package":
                    self._json(_usb_package_payload())

                elif path == "/api/update/peer":
                    controller = _r("peer_update")
                    if controller is None:
                        self._json({"ok": False, "error": "not_available"}, 503)
                    else:
                        self._json({"ok": True, "peer_update": controller.snapshot()})

                elif path == "/api/settings":
                    istate = _r("istate")
                    cfg = dict(istate.config) if istate else {}
                    clip = cfg.pop("clipboard", None) or {}
                    if isinstance(clip, dict):
                        cfg.update(clip)
                    # Read by both clipboard views, also before it was ever saved.
                    normalized = cbm.clipboard_settings(
                        {"clipboard": clip if isinstance(clip, dict) else {}})
                    for key in ("file_preview_animation",
                                "file_preview_video_hover", "file_preview_cache_mb"):
                        cfg[key] = normalized[key]
                    cfg["remote_preview"] = rprev.normalize_settings(cfg.get("remote_preview"))
                    self._json(cfg)

                elif path == "/api/peers":
                    istate = _r("istate")
                    requests = _r("pair_requests")
                    configured = (istate.config.get("peers") or []) if istate else []
                    self._json({"ok": True, "peers": _normalize_runtime_peers(istate) if istate else [],
                                "pair_requests": requests.pending(configured) if requests is not None else [],
                                "this_pc": _this_pc(istate)})

                elif path == "/api/clipboard/profiles":
                    istate = _r("istate")
                    self._json({"ok": True, "profiles": _clipboard_profile_rows(istate)})

                elif path == "/api/clipboard/status":
                    ident = _resolve_peer(params)
                    if not ident:
                        self._error("no active profile")
                        return
                    mgr = _r("clip_mgr")
                    if not mgr:
                        self._error("clipboard manager not ready")
                        return
                    self._json({"ok": True, "diagnostics": mgr.diagnostics(ident)})

                elif path == "/api/clipboard/items":
                    ident = _resolve_peer(params)
                    if not ident:
                        self._error("no active profile")
                        return
                    mgr = _r("clip_mgr")
                    if not mgr:
                        self._error("clipboard manager not ready")
                        return
                    raw = mgr.list_items(ident)
                    try:
                        current_item_id = mgr.store(ident).current_item_id
                    except Exception:
                        current_item_id = None
                    items = []
                    for raw_item in cbm.newest_first(raw):
                        try:
                            items.append(cbm.public_card_item(raw_item, current_item_id=current_item_id))
                        except ValueError:
                            continue
                    self._json({
                        "items": items,
                        "total_size": mgr.store(ident).total_size(),
                        "current_item_id": current_item_id,
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
                    stored = mgr.store(ident).get_item(item_id)
                    try:
                        current_item_id = mgr.store(ident).current_item_id
                    except Exception:
                        current_item_id = None
                    item = None
                    file_manifest = None
                    if stored:
                        try:
                            item = cbm.public_card_item(stored, current_item_id=current_item_id)
                        except ValueError:
                            item = None
                        if stored.get("kind") in (cbm.KIND_FILE, cbm.KIND_FILE_BATCH):
                            file_manifest = cbm.public_file_entries(
                                stored,
                                offset=params.get("manifest_offset", [0])[0],
                                limit=params.get("manifest_limit", [cbm.PUBLIC_DETAIL_MANIFEST_DEFAULT])[0],
                            )
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
                        text = "\n".join(
                            entry["path"] for entry in (file_manifest or {}).get("entries", [])
                            if entry.get("type") == "file")
                    self._json({
                        "item": item,
                        "kind": kind,
                        "text": text,
                        "html_b64": html_b64,
                        "image_b64": image_b64,
                        "file_manifest": file_manifest,
                    })

                elif path == "/api/events":
                    self._handle_sse()

                elif path == "/api/logs":
                    try:
                        after = max(0, int(params.get("after", ["0"])[0]))
                    except (TypeError, ValueError):
                        after = 0
                    self._json(_log_entries_payload(after))

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

                elif path.startswith("/api/clipboard/file-previews/") and len(parts) == 4:
                    stored, previews = self._file_preview_item(params, parts[3])
                    if stored is not None:
                        result = previews.describe(
                            stored, params.get("count", [None])[0],
                            data_fn=self._clip_image_loader(params, parts[3]))
                        self._json(dict(result, ok=True, item_id=parts[3]))

                elif path.startswith("/api/clipboard/file-preview/") and len(parts) == 6:
                    self._serve_file_preview(params, parts[3], parts[4], parts[5])

                elif path == "/api/hotkeys":
                    istate = _r("istate")
                    if istate:
                        rows = [
                            {"label": hk.label, "display": it.display(hk.trigger()) or "not set",
                             "action": hk.action}
                            for hk in istate.hotkeys
                        ]
                        self._json({"hotkeys": rows})
                    else:
                        self._json({"hotkeys": []})

                elif path == "/api/actions":
                    istate = _r("istate")
                    with istate.lock if istate else _NullLock():
                        cfg = dict(istate.config) if istate else {}
                    self._json({
                        "ok": True,
                        "registry_version": oact.REGISTRY_VERSION,
                        "actions": oact.public_actions(),
                        "wheel": _wheel_response(cfg),
                        "limits": {"max_slots_per_page": oact.MAX_SLOTS_PER_PAGE,
                                   "max_pages": oact.MAX_PAGES},
                    })

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
            if self._guard_request("POST"):
                self._route_post()

        def do_DELETE(self):
            if self._guard_request("DELETE"):
                self._route_post()

        def _route_post(self):
            # Only reached through do_POST/do_DELETE after _guard_request.
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            parts = self._path_parts()
            params = self._params()

            try:
                if not self._authorize_local_write(*_write_request_policy(path)):
                    return
                if path in {"/api/update/check", "/api/update/download", "/api/update/install"}:
                    operation = path.rsplit("/", 1)[-1]
                    manager = self._update_manager(operation)
                    if manager is None or not self._empty_update_operation_body(operation):
                        return
                    methods = {
                        "check": manager.check_for_updates,
                        "download": manager.download_update,
                        "install": manager.install_update,
                    }
                    self._send_update_result(methods[operation](), operation)

                elif path == "/api/update/settings":
                    body = self._read_object_body()
                    if body is None:
                        return
                    if set(body) != _UPDATE_SETTING_KEYS:
                        missing = sorted(_UPDATE_SETTING_KEYS - set(body))
                        unknown = sorted(set(body) - _UPDATE_SETTING_KEYS)
                        self._json({
                            "ok": False,
                            "error": "update settings must contain exactly enabled, check_on_start, channel, and policy",
                            "missing": missing,
                            "unknown": unknown,
                        }, 400)
                        return
                    if type(body["enabled"]) is not bool or type(body["check_on_start"]) is not bool:
                        self._error("enabled and check_on_start must be booleans", 400)
                        return
                    if body["channel"] not in config_store.UPDATE_CHANNELS:
                        self._error("channel must be stable or development", 400)
                        return
                    if body["policy"] not in config_store.UPDATE_POLICIES:
                        self._error("policy must be notify, download, or install", 400)
                        return
                    istate = _r("istate")
                    save_cfg = _r("save_config")
                    if istate is None or save_cfg is None:
                        self._error("settings not available", 503)
                        return
                    with istate.lock:
                        cfg = dict(istate.config)
                        updates = dict(cfg.get("updates")) if isinstance(cfg.get("updates"), dict) else {}
                    previous_channel = config_store.normalize_updates(updates)["channel"]
                    updates.update(body)
                    cfg["updates"] = updates
                    saved = save_cfg(cfg)
                    if isinstance(saved, dict):
                        cfg = saved
                    with istate.lock:
                        istate.config = cfg
                    if body["channel"] != previous_channel:
                        # A channel switch shows the new source's answer at once.
                        manager = _r("update_manager")
                        if manager is not None:
                            try:
                                manager.check_for_updates()
                            except Exception:
                                pass
                    publish_event({"type": "status_update"})
                    self._json({"ok": True, "settings": _update_settings_snapshot()})

                elif path == "/api/update/peer/settings":
                    body = self._read_object_body()
                    if body is None:
                        return
                    if (set(body) != _PEER_UPDATE_SETTING_KEYS
                            or body["policy"] not in config_store.UPDATE_POLICIES
                            or type(body["allow_development"]) is not bool):
                        self._error("peer update settings must be exactly policy "
                                    "(notify, download, install) and allow_development (boolean)", 400)
                        return
                    istate = _r("istate")
                    save_cfg = _r("save_config")
                    if istate is None or save_cfg is None:
                        self._error("settings not available", 503)
                        return
                    with istate.lock:
                        cfg = dict(istate.config)
                        updates = dict(cfg.get("updates")) if isinstance(cfg.get("updates"), dict) else {}
                    updates["from_peers"] = {"policy": body["policy"],
                                             "allow_development": body["allow_development"]}
                    cfg["updates"] = updates
                    saved = save_cfg(cfg)
                    if isinstance(saved, dict):
                        cfg = saved
                    with istate.lock:
                        istate.config = cfg
                    controller = _r("peer_update")
                    if controller is not None:
                        try:
                            controller.settings_changed()
                        except Exception:
                            pass
                    publish_event({"type": "status_update"})
                    self._json({"ok": True, "settings": config_store.normalize_updates(
                        cfg.get("updates"))["from_peers"]})

                elif path.startswith("/api/update/peer/") and path.rsplit("/", 1)[-1] in _PEER_UPDATE_ACTIONS:
                    action = path.rsplit("/", 1)[-1]
                    if not self._empty_update_operation_body(f"peer-{action}"):
                        return
                    controller = _r("peer_update")
                    if controller is None:
                        self._json({"ok": False, "error": "not_available"}, 503)
                        return
                    if action == "cancel":
                        result = "cancelled" if controller.cancel() else "nothing_to_cancel"
                    else:
                        result = getattr(controller, action)()
                    ok = result in {"started", "cancelled"}
                    publish_event({"type": "status_update"})
                    response = {"ok": ok, "status": result, "peer_update": controller.snapshot()}
                    if not ok:
                        response["error"] = result
                    self._json(response, 202 if ok else 409)

                elif path == "/api/update/usb-package":
                    # Builds FlowShift-<version>-usb.zip of the installed version
                    # in the background; GET on the same path reports progress.
                    # The only accepted body is {"python_installer": "downloads"}:
                    # use the python.org installer the user saved with the
                    # browser in Downloads (fixed folder, no path from the
                    # request; the SHA-256 is checked all the same).
                    body = self._read_object_body()
                    if body is None:
                        return
                    if body and body != {"python_installer": usb_package.PYTHON_SOURCE_DOWNLOADS_FOLDER}:
                        self._json({
                            "ok": False, "status": "blocked", "operation": "usb-package",
                            "message": 'Only {"python_installer": "downloads"} is accepted',
                            "error_code": "request_body_not_allowed",
                        }, 400)
                        return
                    source = body.get("python_installer", usb_package.PYTHON_SOURCE_DOWNLOAD)
                    payload = _usb_package_payload()
                    if not payload["available"]:
                        payload.update(ok=False, error=payload["blocked_reason"])
                        self._json(payload, 503 if payload["blocked_reason"] == "not_available" else 409)
                        return
                    started, status = _r("usb_package_job").start(python_source=source)
                    payload["package"] = status
                    if not started:
                        payload.update(ok=False, error="already_running")
                        self._json(payload, 409)
                        return
                    publish_event({"type": "status_update"})
                    self._json(payload, 202)

                elif path == "/api/update/development-access":
                    # Write-only: the token goes to the secure store and is never
                    # returned, logged or written to config.json.
                    body = self._read_object_body()
                    if body is None:
                        return
                    if set(body) != {"token"} or not (
                            body["token"] is None or isinstance(body["token"], str)):
                        self._error("body must contain only token (text or null)", 400)
                        return
                    store = _r("update_credentials")
                    if store is None:
                        self._error("secure credential store is not available", 503)
                        return
                    try:
                        if body["token"] is None:
                            store.delete()
                        else:
                            store.write(body["token"])
                    except update_credentials.CredentialError as exc:
                        self._json({"ok": False, "error": exc.code}, 400
                                   if exc.code == "invalid_token" else 500)
                        return
                    manager = _r("update_manager")
                    if (body["token"] is not None and manager is not None
                            and _update_settings_snapshot()["channel"]
                            == update_model.CHANNEL_DEVELOPMENT):
                        try:
                            manager.check_for_updates()
                        except Exception:
                            pass
                    publish_event({"type": "status_update"})
                    self._json({"ok": True, "development_access": _development_access_status()})

                elif path == "/api/actions/wheel":
                    body = self._read_object_body()
                    if body is None:
                        return
                    # A page loaded before 0.7.0-dev.51 still sends ``extra_hotkeys``;
                    # an empty list is harmless, more hotkeys no longer exist.
                    if body.get("extra_hotkeys") in ([], None) and "extra_hotkeys" in body:
                        body = {k: v for k, v in body.items() if k != "extra_hotkeys"}
                    allowed = {"pages", "hotkey", "trigger"}
                    if not body or not set(body).issubset(allowed):
                        message = ("the command wheel has one hotkey only; reload the page (F5)"
                                   if "extra_hotkeys" in body
                                   else "body allows only pages, hotkey and trigger")
                        self._json({"ok": False, "error": "invalid_request",
                                    "message": message}, 400)
                        return
                    istate = _r("istate")
                    save_cfg = _r("save_config")
                    if istate is None or save_cfg is None:
                        self._error("settings not available", 503)
                        return
                    with istate.lock:
                        cfg = dict(istate.config)
                    current = oact.wheel_config_from(cfg)
                    try:
                        if "pages" in body:
                            current["pages"] = oact.normalize_pages(body["pages"])
                        # ``"hotkey": null`` clears the main trigger.
                        if "hotkey" in body:
                            current["hotkey"] = oact.normalize_hotkey(body["hotkey"])
                        if "trigger" in body:
                            current["trigger"] = oact.normalize_trigger(body["trigger"],
                                                                        current["trigger"])
                        current = oact.normalize_wheel_config(current)
                    except oact.ActionError as exc:
                        self._json({"ok": False, "error": exc.code, "message": str(exc)}, 400)
                        return
                    candidate = dict(cfg, command_wheel=current)
                    conflicts = _trigger_conflicts(candidate)
                    if conflicts:
                        self._json({"ok": False, "error": "hotkey_conflict",
                                    "message": conflicts[0]["message"],
                                    "conflicts": conflicts}, 400)
                        return
                    cfg["command_wheel"] = current
                    saved = save_cfg(cfg)
                    if isinstance(saved, dict):
                        cfg = saved
                    with istate.lock:
                        istate.config = cfg
                    before_seq = _hotkey_report_seq()
                    registration = _reload_and_report(before_seq, hks.WHEEL_SLOTS)
                    publish_event({"type": "status_update"})
                    _log("INFO", "command wheel saved hotkey="
                                 f"{it.display(current['hotkey']) if current['hotkey'] else 'none'}"
                                 + (f" inactive={len(registration['inactive'])}"
                                    if registration else ""))
                    self._json({"ok": True, "wheel": _wheel_response(cfg),
                                "registration": registration})

                elif path == "/api/actions/execute":
                    body = self._read_object_body()
                    if body is None:
                        return
                    try:
                        action, context = oact.validate_execute_request(body)
                    except oact.ActionError as exc:
                        self._json({"ok": False, "error": exc.code, "message": str(exc)}, 400)
                        return
                    execute = _r("execute_action")
                    if execute is None:
                        self._json({"ok": False, "action_id": action["id"],
                                    "error": "not_available",
                                    "message": "action execution is not available"}, 503)
                        return
                    # The tray registered execute_action with channel web_api
                    # bound; the body never chooses the channel.
                    result = execute(action, context)
                    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                        self._json({"ok": False, "action_id": action["id"],
                                    "error": "runtime_error",
                                    "message": "runtime returned an invalid result"}, 500)
                        return
                    payload = {"ok": result["ok"], "action_id": action["id"],
                               "reason": result.get("reason")}
                    self._json(payload, 200 if result["ok"] else 409)

                elif path == "/api/forwarding/activate":
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
                    save_cfg = _r("save_config")
                    if not istate or not save_cfg:
                        self._error("settings not available")
                        return
                    if not isinstance(body, dict):
                        self._json({"ok": False, "error": "invalid_settings",
                                    "message": "settings body must be an object"}, 400)
                        return
                    clip_keys = set(cbm.DEFAULT_CLIPBOARD_SETTINGS.keys()) if hasattr(cbm, 'DEFAULT_CLIPBOARD_SETTINGS') else set()
                    # ``"overlay_hotkey": null`` clears the clipboard overlay hotkey.
                    clear_overlay_hotkey = ("overlay_hotkey" in body
                                            and body["overlay_hotkey"] is None)
                    try:
                        if body.get("overlay_hotkey") is not None:
                            body["overlay_hotkey"] = oact.normalize_hotkey(body["overlay_hotkey"])
                        if body.get("hotkeys") is not None:
                            body["hotkeys"] = it.normalize_peer_hotkeys(body["hotkeys"])
                    except (oact.ActionError, it.TriggerError) as exc:
                        self._json({"ok": False, "error": exc.code, "message": str(exc)}, 400)
                        return
                    preview_was_enabled = None
                    if body.get("remote_preview") is not None:
                        with istate.lock:
                            stored_preview = istate.config.get("remote_preview")
                        preview, invalid = _merge_remote_preview(stored_preview,
                                                                 body["remote_preview"])
                        if invalid:
                            self._json({"ok": False, "error": "invalid_remote_preview",
                                        "message": "invalid remote preview settings: "
                                                   + ", ".join(invalid),
                                        "fields": invalid}, 400)
                            return
                        body = dict(body)
                        body["remote_preview"] = preview
                        preview_was_enabled = rprev.normalize_settings(stored_preview)["enabled"]
                    with istate.lock:
                        cfg = dict(istate.config)
                        clip_cfg = dict(cfg.get("clipboard") or {})
                        for k, v in body.items():
                            # Wheel, peers, display layout and update settings have
                            # their own routes; the Settings page posts back what it
                            # loaded, which must not undo a change saved there since.
                            if v is None or k in SETTINGS_FOREIGN_KEYS:
                                continue
                            if k in clip_keys:
                                clip_cfg[k] = v
                            else:
                                cfg[k] = v
                        if clear_overlay_hotkey:
                            clip_cfg["overlay_hotkey"] = None
                        cfg["clipboard"] = cbm.clipboard_settings({"clipboard": clip_cfg})
                        conflicts = _trigger_conflicts(cfg)
                        if not conflicts:
                            istate.config = cfg
                        if hasattr(istate, "hotkeys") and not conflicts:
                            istate.hotkeys = rm.load_hotkeys(cfg)
                    if conflicts:
                        self._json({"ok": False, "error": "hotkey_conflict",
                                    "message": conflicts[0]["message"],
                                    "conflicts": conflicts}, 400)
                        return
                    save_cfg(cfg)
                    registration = _reload_and_report(_hotkey_report_seq(),
                                                      hks.CLIPBOARD_SLOTS)
                    preview_changed = _r("remote_preview_changed")
                    if (preview_was_enabled is not None and preview_changed
                            and cfg["remote_preview"]["enabled"] != preview_was_enabled):
                        try:
                            preview_changed()
                        except Exception as exc:  # pragma: no cover - runtime only
                            _log("WARN", f"remote preview announce failed: {exc}")
                    publish_event({"type": "status_update"})
                    self._json({"ok": True, "config": cfg, "registration": registration})

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
                    added, failure = _add_peer_to_config(peer)
                    if failure:
                        self._error(*failure)
                        return
                    self._json({"ok": True, "peer": added})

                elif path in {"/api/peers/requests/accept", "/api/peers/requests/dismiss"}:
                    # A PC that has this PC in its list dialed in (PairRequests).
                    body = self._read_body()
                    key = str(body.get("key", "") if isinstance(body, dict) else "")
                    requests = _r("pair_requests")
                    if requests is None:
                        self._error("not available", 503)
                        return
                    if path == "/api/peers/requests/dismiss":
                        self._json({"ok": True, "dismissed": requests.dismiss(key)})
                        return
                    item = requests.take(key)
                    if item is None:
                        self._error("unknown request", 404)
                        return
                    peer = {"name": item["name"], "host": item["host"], "port": item["port"]}
                    if item.get("device_id"):
                        peer["device_id"] = item["device_id"]
                    added, failure = _add_peer_to_config(peer)
                    if failure:
                        self._error(*failure)
                        return
                    self._json({"ok": True, "peer": added})

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

                elif path == "/api/logs/open-folder":
                    if self._read_object_body() is None:
                        return
                    opener = _r("open_log_folder")
                    opened = False
                    if opener:
                        try:
                            opened = bool(opener())
                        except Exception as e:
                            _log("WARN", f"log folder not opened: {e!r}")
                    self._json({"ok": opened, "opened": opened, **_log_folder_info()})

                elif path == "/api/peers/scan":
                    port = int(_r("istate").config.get("port", 45781)) if _r("istate") else 45781
                    local_device_id = _r("istate").config.get("device_id", "") if _r("istate") else ""
                    body = self._read_body()
                    timeout = min(max(float(body.get("timeout", lan_scan.DEFAULT_TIMEOUT)), 0.1), 10.0)
                    targets, summary = _unicast_targets()
                    if (summary.get("tailscale") or {}).get("self"):
                        _last_tailscale_self.update(summary["tailscale"]["self"])
                    searched = []
                    found = _scan_broadcast(port, timeout, local_device_id, targets, searched=searched)
                    self._json({"peers": found, "searched": summary,
                                "hint": pdisc.scan_hint(found, summary, port),
                                "this_pc": _this_pc(_r("istate")),
                                "adapters": searched,
                                "adapters_text": lan_scan.summary(searched)})

                elif path == "/api/auto-start":
                    body = self._read_body()
                    enabled = body.get("enabled")
                    if enabled is not None:
                        try:
                            _auto_start_set(bool(enabled))
                        except autostart.AutoStartError as exc:
                            # The switch snaps back to the real state with the reason.
                            self._json({"error": str(exc), "enabled": _auto_start_get()}, 409)
                            return
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
                    except Exception as e:
                        # ping_peer marks failures of the connection itself
                        # with ping_target; other ValueErrors are bad requests.
                        target = getattr(e, "ping_target", None)
                        if target is None and isinstance(e, ValueError):
                            self._error(str(e))
                            return
                        host, port = target or (None, None)
                        self._json({"error": f"ping failed: {e}",
                                    "message": pdisc.ping_failure_hint(e, host, port)}, 502)

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

                elif path == "/api/overlay/probe":
                    # Opens the overlay with its real content at the cursor;
                    # the host then measures the screen and writes the
                    # "overlay pixel" lines to flowshift.log.
                    body = self._read_body()
                    mode = body.get("mode", "command_wheel") if isinstance(body, dict) else None
                    if mode not in ("command_wheel", "clipboard"):
                        self._error("mode must be command_wheel or clipboard")
                        return
                    probe = _r("probe_overlay")
                    if not probe:
                        self._json({"ok": False, "error": "overlay is not initialized"}, 503)
                        return
                    result = probe(mode)
                    if isinstance(result, dict) and result.get("type") == "overlay_visible":
                        self._json({"ok": True, "result": result,
                                    "log": "overlay pixel check"})
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

                elif path == "/api/clipboard/delete":
                    # Delete the selected items at once, without confirmation.
                    # Pinned items are deleted when their id is passed.
                    ident = _resolve_peer(params)
                    if not ident:
                        self._error("missing profile")
                        return
                    body = self._read_object_body()
                    if body is None:
                        return
                    item_ids = body.get("item_ids")
                    if (not isinstance(item_ids, list) or len(item_ids) > _CLIPBOARD_DELETE_MAX_IDS
                            or not all(isinstance(i, str) and i for i in item_ids)):
                        self._error("item_ids must be a list of item ids")
                        return
                    mgr = _r("clip_mgr")
                    if not mgr:
                        self._error("clipboard manager not ready")
                        return
                    deleted = mgr.delete_items(ident, item_ids)
                    _log("INFO", f"clipboard delete selected profile={ident} "
                                 f"requested={len(item_ids)} deleted={len(deleted)}")
                    self._json({"deleted": len(deleted), "item_ids": deleted})

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
                            ok_set = mgr.perform_windows_write(
                                ident, item_id, {"files"}, "files", mgr.file_list_digest(paths),
                                lambda: clipboard_win.set_files(paths, return_sequence=True),
                                clipboard_win.get_sequence_number)
                            self._json({"set": bool(ok_set), "kind": kind, "count": len(paths)})
                        else:
                            self._error(result.get("error") or "file data not present")
                    elif kind in (cbm.KIND_IMAGE, cbm.KIND_GIF):
                        data = mgr.store(ident).get_data(item_id)
                        if data:
                            import clipboard_win
                            ok_set = mgr.perform_windows_write(
                                ident, item_id, {"image"}, "image", mgr.bytes_digest(data),
                                lambda: clipboard_win.set_image(data, return_sequence=True),
                                clipboard_win.get_sequence_number)
                            self._json({"set": bool(ok_set), "kind": kind})
                        else:
                            self._error("image not present")
                    elif kind == cbm.KIND_HTML:
                        raw = mgr.get_html(ident, item_id)
                        if raw:
                            import clipboard_win
                            item = mgr.store(ident).get_item(item_id)
                            preview_text = (item.get("preview_text") if item else "") or ""
                            formats = {"html", "text"} if preview_text else {"html"}
                            ok_set = mgr.perform_windows_write(
                                ident, item_id, formats, "html", mgr.bytes_digest(raw),
                                lambda: clipboard_win.set_html(
                                    raw, preview_text or None, return_sequence=True),
                                clipboard_win.get_sequence_number)
                            self._json({"set": bool(ok_set), "kind": "html"})
                        else:
                            self._error("html not present")
                    else:
                        text = mgr.get_text(ident, item_id)
                        if text is not None:
                            import clipboard_win
                            ok_set = mgr.perform_windows_write(
                                ident, item_id, {"text"}, "text", mgr.text_digest(text),
                                lambda: clipboard_win.set_text(text, return_sequence=True),
                                clipboard_win.get_sequence_number)
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

                elif path.startswith("/api/clipboard/item/") and path.endswith("/copy-text"):
                    item_id = parts[-2]
                    ident = _resolve_peer(params)
                    body = self._read_object_body()
                    if body is None:
                        return
                    mgr = _r("clip_mgr")
                    write_text = _r("write_clipboard_text")
                    if not ident or not item_id:
                        self._error("missing params")
                        return
                    if not mgr or not callable(write_text):
                        self._error("clipboard writer not ready")
                        return
                    stored = mgr.store(ident).get_item(item_id)
                    if not stored:
                        self._error("clipboard item not found", 404)
                        return
                    try:
                        text, count, truncated = cbm.public_card_copy_text(
                            stored, body.get("field"))
                    except ValueError as exc:
                        self._error(str(exc))
                        return
                    # FlowShift's own write: the watcher must not record it as
                    # a new copy (no duplicate entry in the history).
                    if not write_text(text, item_id):
                        self._error("failed to set clipboard", 500)
                        return
                    self._json({"copied": True, "count": count, "truncated": truncated})

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
                            if not eb.is_subscribed(sub):
                                # Dropped after a full queue (a burst of log
                                # lines): end the stream, the browser
                                # reconnects and the page reloads /api/logs.
                                break
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
