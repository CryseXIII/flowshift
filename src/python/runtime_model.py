"""FlowShift shared runtime logic (platform independent).

This module contains the pure, Windows-API-free logic that is shared between
the productive runtime (`tray.py`) and the GUI (`gui.py`):

* peer identity + hotkey action model (stable identity, no list index)
* hotkey migration from the legacy ``forward_<index>`` format
* protocol framing (4-byte big-endian length + JSON payload)
* mouse coordinate scaling between source and target screens
* pressed key / button tracking for clean release on stop/disconnect

Nothing in here may import ``ctypes`` / Windows APIs so it can be imported and
unit tested on any operating system.
"""
from __future__ import annotations

import json
import re
import socket
import struct
import threading

# ── Modifier bit layout (tray internal) ─────────────────────────────
MOD_CTRL = 1
MOD_SHIFT = 2
MOD_ALT = 4
MOD_WIN = 8

MOD_NAMES = {MOD_CTRL: "Ctrl", MOD_SHIFT: "Shift", MOD_ALT: "Alt", MOD_WIN: "Win"}

# Virtual-key codes that are pure modifiers (never forwarded as content keys)
MODIFIER_VKS = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}

# Virtual-key codes that MUST be injected with KEYEVENTF_EXTENDEDKEY on Windows.
# Without the extended flag, SendInput maps arrow/nav keys to their numpad scan
# codes; with Shift held this makes Windows toggle NumLock behaviour instead of
# extending the selection, which is exactly why Shift+Arrow selection failed.
EXTENDED_KEY_VKS = {
    0x21,  # VK_PRIOR  (Page Up)
    0x22,  # VK_NEXT   (Page Down)
    0x23,  # VK_END
    0x24,  # VK_HOME
    0x25,  # VK_LEFT
    0x26,  # VK_UP
    0x27,  # VK_RIGHT
    0x28,  # VK_DOWN
    0x2C,  # VK_SNAPSHOT (Print Screen)
    0x2D,  # VK_INSERT
    0x2E,  # VK_DELETE
    0x5B,  # VK_LWIN
    0x5C,  # VK_RWIN
    0x6F,  # VK_DIVIDE (numpad /)
    0x90,  # VK_NUMLOCK
    0xA3,  # VK_RCONTROL
    0xA5,  # VK_RMENU (right Alt)
}


def is_extended_key(vk):
    """True if this virtual-key must carry KEYEVENTF_EXTENDEDKEY when injected."""
    try:
        return int(vk) in EXTENDED_KEY_VKS
    except (TypeError, ValueError):
        return False

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

DEFAULT_PORT = 45781

# Hard upper bound for a single protocol frame (defence against a peer announcing
# a huge length and forcing unbounded memory allocation). 28 MiB leaves head-room
# for a future clipboard item cap of ~20 MiB plus JSON/base64 overhead.
MAX_FRAME_SIZE = 28 * 1024 * 1024


# ── Hotkey text helpers ─────────────────────────────────────────────
def vk_name(vk):
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    return VK_NAMES.get(vk, f"VK_0x{vk:02X}")


def mods_name(mods):
    parts = [name for bit, name in sorted(MOD_NAMES.items()) if mods & bit]
    return "+".join(parts) if parts else ""


def format_hotkey(mods, vk):
    prefix = mods_name(mods)
    key = vk_name(vk)
    return f"{prefix}+{key}" if prefix else key


# ── Peer identity + hotkey action model ─────────────────────────────
# Actions:
#   "return_local"                     -> return input to local machine
#   "forward_peer:<peer_identity>"     -> forward input to a specific peer
#
# peer_identity is a STABLE string, never the list index:
#   "device:<device_id>"     preferred, uses the remote device_id
#   "endpoint:<host>:<port>" fallback when no device_id is known yet
FORWARD_PREFIX = "forward_peer:"
RETURN_ACTION = "return_local"
UNRESOLVED_IDENTITY = "__unresolved__"
UNRESOLVED_ACTION = FORWARD_PREFIX + UNRESOLVED_IDENTITY

_LEGACY_FORWARD_RE = re.compile(r"^forward_(\d+)$")


def peer_identity(peer):
    """Stable identity for a peer config/connection dict."""
    device_id = str(peer.get("device_id", "")).strip().lower()
    if device_id:
        return f"device:{device_id}"
    host = str(peer.get("host", "")).strip()
    try:
        port = int(peer.get("port", DEFAULT_PORT) or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    return f"endpoint:{host}:{port}"


def make_forward_action(peer):
    return FORWARD_PREFIX + peer_identity(peer)


def is_forward_action(action):
    return isinstance(action, str) and action.startswith(FORWARD_PREFIX)


def is_return_action(action):
    return action == RETURN_ACTION


def parse_forward_action(action):
    """Return the peer identity string of a forward action, else None."""
    if not isinstance(action, str) or not action.startswith(FORWARD_PREFIX):
        return None
    ident = action[len(FORWARD_PREFIX):].strip()
    if not ident or ident == UNRESOLVED_IDENTITY:
        return None
    return ident


def peer_display_name(peer):
    return str(peer.get("name") or peer.get("display_name") or peer.get("host") or "peer")


def resolve_peer_by_action(config, action):
    """Resolve a forward action to a concrete peer dict, or None."""
    ident = parse_forward_action(action)
    if not ident:
        return None
    peers = config.get("peers", []) if isinstance(config, dict) else list(config or [])

    for p in peers:
        if peer_identity(p) == ident:
            return p

    if ident.startswith("device:"):
        did = ident.split(":", 1)[1]
        for p in peers:
            if str(p.get("device_id", "")).strip().lower() == did:
                return p
    elif ident.startswith("endpoint:"):
        rest = ident.split(":", 1)[1]
        host, _, port = rest.rpartition(":")
        for p in peers:
            try:
                p_port = str(int(p.get("port", DEFAULT_PORT) or DEFAULT_PORT))
            except (TypeError, ValueError):
                p_port = str(DEFAULT_PORT)
            if str(p.get("host", "")) == host and p_port == port:
                return p
    return None


# ── Hotkey list construction / migration ────────────────────────────
def default_hotkeys(peers):
    hk = []
    for i, p in enumerate(peers):
        if i < 9:
            hk.append({
                "action": make_forward_action(p),
                "mods": MOD_CTRL | MOD_ALT,
                "key": 0x31 + i,
                "label": f"Forward to {peer_display_name(p)}",
            })
    hk.append({
        "action": RETURN_ACTION,
        "mods": MOD_CTRL | MOD_ALT,
        "key": 0x30,
        "label": "Return to local",
    })
    return hk


def migrate_hotkeys(config):
    """Convert legacy ``forward_<index>`` actions to stable identities.

    Returns True if anything changed.
    """
    changed = False
    peers = config.get("peers", [])
    for hk in config.get("hotkeys", []) or []:
        action = hk.get("action", "")
        m = _LEGACY_FORWARD_RE.match(action or "")
        if not m:
            continue
        idx = int(m.group(1))
        if idx < len(peers):
            hk["action"] = make_forward_action(peers[idx])
            hk["label"] = f"Forward to {peer_display_name(peers[idx])}"
        else:
            hk["action"] = UNRESOLVED_ACTION
            base = hk.get("label", "Forward")
            hk["label"] = base if "ungültig" in base else f"{base} (ungültig)"
        changed = True
    return changed


def ensure_return_local(config):
    hotkeys = config.setdefault("hotkeys", [])
    if any(is_return_action(hk.get("action")) for hk in hotkeys):
        return False
    hotkeys.append({
        "action": RETURN_ACTION,
        "mods": MOD_CTRL | MOD_ALT,
        "key": 0x30,
        "label": "Return to local",
    })
    return True


def add_missing_peer_hotkeys(config):
    """Give every peer without a forward hotkey a fresh one on a free number key."""
    changed = False
    peers = config.get("peers", [])
    hotkeys = config.setdefault("hotkeys", [])

    existing = set()
    used_keys = set()
    for hk in hotkeys:
        ident = parse_forward_action(hk.get("action", ""))
        if ident:
            existing.add(ident)
        if isinstance(hk.get("key"), int):
            used_keys.add(hk["key"])

    for p in peers:
        ident = peer_identity(p)
        if ident in existing:
            continue
        free = next((k for k in range(0x31, 0x3A) if k not in used_keys), None)
        hotkeys.append({
            "action": make_forward_action(p),
            "mods": MOD_CTRL | MOD_ALT,
            "key": free if free is not None else 0,
            "label": f"Forward to {peer_display_name(p)}",
        })
        existing.add(ident)
        if free is not None:
            used_keys.add(free)
        changed = True
    return changed


def refresh_hotkey_labels(config):
    """Keep labels in sync with the resolved peer (rename support)."""
    changed = False
    for hk in config.get("hotkeys", []) or []:
        action = hk.get("action", "")
        if not is_forward_action(action):
            continue
        peer = resolve_peer_by_action(config, action)
        if peer is not None:
            new_label = f"Forward to {peer_display_name(peer)}"
        else:
            new_label = "Forward (ungültiges Ziel)"
        if hk.get("label") != new_label:
            hk["label"] = new_label
            changed = True
    return changed


def sync_hotkeys(config):
    """Full normalisation pass. Returns True if the config was modified."""
    if not isinstance(config.get("hotkeys"), list) or not config.get("hotkeys"):
        config["hotkeys"] = default_hotkeys(config.get("peers", []))
        return True
    changed = False
    changed |= migrate_hotkeys(config)
    changed |= add_missing_peer_hotkeys(config)
    changed |= ensure_return_local(config)
    changed |= refresh_hotkey_labels(config)
    return changed


def hotkey_is_valid(config, hk):
    action = hk.get("action", "")
    if is_return_action(action):
        return True
    if is_forward_action(action):
        return resolve_peer_by_action(config, action) is not None
    return False


def hotkey_registration_error(config, hk):
    """Return ``None`` if a hotkey may be registered at OS level, else a reason.

    A hotkey is only registrable when it has a real key AND its action resolves:
    ``return_local`` is always valid; ``forward_peer:<id>`` is valid only when the
    identity resolves to a current peer; ``key == 0`` (or non-int) is never valid.
    """
    key = hk.get("key", 0)
    if not isinstance(key, int) or key == 0:
        return "no key assigned"
    action = hk.get("action", "")
    if is_return_action(action):
        return None
    if is_forward_action(action):
        if resolve_peer_by_action(config, action) is None:
            return "forward target does not resolve to a peer"
        return None
    return f"unknown action: {action!r}"


# ── Connector reconciliation (host/port change detection) ───────────
def diff_connectors(current, desired):
    """Reconcile running connectors against the desired peer set.

    ``current`` and ``desired`` map a stable connector token (peer identity) to
    an ``(host, port)`` tuple. A token whose address changed must be stopped and
    restarted so the connector never keeps dialing a stale host/port.

    Returns ``(to_stop, to_start)`` sets of tokens.
    """
    to_stop = set()
    to_start = set()
    for token, addr in current.items():
        if token not in desired:
            to_stop.add(token)          # peer removed
        elif tuple(desired[token]) != tuple(addr):
            to_stop.add(token)          # host/port changed -> restart
            to_start.add(token)
    for token in desired:
        if token not in current:
            to_start.add(token)         # new peer
    return to_stop, to_start


def index_by_identity(rows):
    """Index runtime peer-status rows by their stable ``identity`` field.
    Rows without an identity are skipped. Used by the GUI so profile rows map to
    live connections by identity, never by (possibly duplicated) display name.
    """
    out = {}
    for row in rows or []:
        ident = row.get("identity") if isinstance(row, dict) else None
        if ident:
            out[ident] = row
    return out


def should_suppress_input(forwarding_active, peer_connected):
    """Whether a local input event may be suppressed (swallowed).

    Fail-safe rule: local input may only be swallowed when forwarding is active
    AND a peer is actually connected. In every other case (no profile active,
    peer disconnected, or a send failure that deactivates forwarding) the event
    must stay local so the machine never becomes unusable.
    """
    return bool(forwarding_active and peer_connected)


# ── HotkeyBinding ───────────────────────────────────────────────────
class HotkeyBinding:
    __slots__ = ("action", "mods", "key", "label")

    def __init__(self, action, mods, key, label=""):
        self.action = action
        self.mods = mods
        self.key = key
        self.label = label

    def matches(self, mods, vk):
        return self.mods == mods and self.key == vk

    def display(self):
        return format_hotkey(self.mods, self.key)

    def __repr__(self):
        return f"HotkeyBinding(action={self.action!r}, hotkey={self.display()!r})"


def load_hotkeys(cfg):
    raw = cfg.get("hotkeys")
    if not raw:
        raw = default_hotkeys(cfg.get("peers", []))
        cfg["hotkeys"] = raw
    return [
        HotkeyBinding(
            h["action"],
            h.get("mods", MOD_CTRL | MOD_ALT),
            h["key"],
            h.get("label", h["action"]),
        )
        for h in raw
    ]


# ── Mouse scaling ───────────────────────────────────────────────────
def scale_mouse_point(x, y, source_spec, target_spec):
    """Map an absolute source-screen point onto the target screen.

    Both specs are dicts with left/top/width/height. When either is missing the
    original coordinates are returned unchanged.
    """
    if not isinstance(source_spec, dict) or not isinstance(target_spec, dict):
        return x, y

    src_left = int(source_spec.get("left", 0))
    src_top = int(source_spec.get("top", 0))
    src_width = max(1, int(source_spec.get("width", 1)))
    src_height = max(1, int(source_spec.get("height", 1)))

    tgt_left = int(target_spec.get("left", 0))
    tgt_top = int(target_spec.get("top", 0))
    tgt_width = max(1, int(target_spec.get("width", 1)))
    tgt_height = max(1, int(target_spec.get("height", 1)))

    rel_x = (x - src_left) / max(1, src_width - 1)
    rel_y = (y - src_top) / max(1, src_height - 1)
    # Clamp into the [0, 1] range so events can never leave the target screen.
    rel_x = min(1.0, max(0.0, rel_x))
    rel_y = min(1.0, max(0.0, rel_y))
    return (
        tgt_left + rel_x * max(1, tgt_width - 1),
        tgt_top + rel_y * max(1, tgt_height - 1),
    )


def normalize_absolute(value, origin, size):
    """Normalise an absolute coordinate to the 0..65535 SendInput range."""
    span = max(1, int(size) - 1)
    norm = round((value - origin) * 65535 / span)
    return int(max(0, min(65535, norm)))


# ── Mouse smoothing / coalescing ────────────────────────────────────
DEFAULT_MOUSE_SETTINGS = {
    "flush_interval_ms": 6,      # how often accumulated moves are flushed
    "max_batch_ms": 12,          # hard upper bound on move latency under load
    "sensitivity": 1.0,          # multiplier on raw hardware deltas
    "accumulate_subpixel": True, # keep fractional remainders so slow moves survive
}


def mouse_settings(config):
    """Return normalised mouse settings merged with defaults.

    Reads the optional ``"mouse"`` block from the config and clamps values into
    sane ranges so a bad config can never make the sender spin or stall.
    """
    raw = {}
    if isinstance(config, dict):
        raw = config.get("mouse") or {}
    out = dict(DEFAULT_MOUSE_SETTINGS)
    if isinstance(raw, dict):
        for k in out:
            if k in raw and raw[k] is not None:
                out[k] = raw[k]
    # Clamp.
    try:
        out["flush_interval_ms"] = max(1, min(100, int(out["flush_interval_ms"])))
    except (TypeError, ValueError):
        out["flush_interval_ms"] = DEFAULT_MOUSE_SETTINGS["flush_interval_ms"]
    try:
        out["max_batch_ms"] = max(out["flush_interval_ms"], min(200, int(out["max_batch_ms"])))
    except (TypeError, ValueError):
        out["max_batch_ms"] = DEFAULT_MOUSE_SETTINGS["max_batch_ms"]
    try:
        out["sensitivity"] = max(0.1, min(10.0, float(out["sensitivity"])))
    except (TypeError, ValueError):
        out["sensitivity"] = DEFAULT_MOUSE_SETTINGS["sensitivity"]
    out["accumulate_subpixel"] = bool(out["accumulate_subpixel"])
    return out


class MouseCoalescer:
    """Accumulates relative mouse deltas and flushes integer (dx, dy) batches.

    Many small hardware moves within one flush interval are merged into a single
    ``mousemove`` event, drastically reducing the number of network sends and
    SendInput calls (the main jitter source) while preserving the total travel.
    Sub-pixel remainders are carried over so slow / high-sensitivity-scaled moves
    never silently vanish.
    """

    def __init__(self, sensitivity=1.0, accumulate_subpixel=True):
        self.sensitivity = float(sensitivity)
        self.accumulate_subpixel = bool(accumulate_subpixel)
        self._ax = 0.0   # accumulated (scaled) delta not yet emitted
        self._ay = 0.0
        self._rx = 0.0   # sub-pixel remainder carried across flushes
        self._ry = 0.0
        self.pending = False

    def add(self, dx, dy):
        self._ax += dx * self.sensitivity
        self._ay += dy * self.sensitivity
        self.pending = True

    def flush(self):
        """Return (dx, dy) integer delta to send, or None if nothing to move."""
        if not self.pending:
            return None
        fx = self._ax + self._rx
        fy = self._ay + self._ry
        idx = int(fx)   # truncation toward zero (keeps sign correct)
        idy = int(fy)
        if self.accumulate_subpixel:
            self._rx = fx - idx
            self._ry = fy - idy
        else:
            self._rx = 0.0
            self._ry = 0.0
        self._ax = 0.0
        self._ay = 0.0
        self.pending = False
        if idx == 0 and idy == 0:
            return None
        return (idx, idy)

    def clear(self):
        self._ax = self._ay = 0.0
        self._rx = self._ry = 0.0
        self.pending = False


# ── Protocol framing (4-byte BE length + JSON) ──────────────────────
def pack_frame(msg):
    data = json.dumps(msg).encode("utf-8")
    if len(data) > MAX_FRAME_SIZE:
        raise ValueError(f"frame too large to send: {len(data)} > {MAX_FRAME_SIZE} bytes")
    return struct.pack("!I", len(data)) + data


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf.extend(chunk)
    return bytes(buf)


def send_msg(sock, msg):
    sock.sendall(pack_frame(msg))


def recv_msg(sock):
    n = struct.unpack("!I", recv_exact(sock, 4))[0]
    if n > MAX_FRAME_SIZE:
        raise ValueError(f"frame too large to receive: {n} > {MAX_FRAME_SIZE} bytes")
    return json.loads(recv_exact(sock, n).decode("utf-8"))


class FramedReader:
    """Timeout-tolerant framed message reader.

    ``read_message(timeout)`` returns a decoded message, ``None`` on timeout
    (partial data is preserved across calls so the framing never desyncs), and
    raises ``ConnectionError`` when the socket is closed.
    """

    def __init__(self, sock):
        self.sock = sock
        self._buf = bytearray()

    def _try_parse(self):
        if len(self._buf) < 4:
            return None
        n = struct.unpack("!I", bytes(self._buf[:4]))[0]
        if n > MAX_FRAME_SIZE:
            raise ValueError(f"frame too large: {n} > {MAX_FRAME_SIZE} bytes")
        if len(self._buf) < 4 + n:
            return None
        payload = bytes(self._buf[4:4 + n])
        del self._buf[:4 + n]
        return json.loads(payload.decode("utf-8"))

    def read_message(self, timeout):
        msg = self._try_parse()
        if msg is not None:
            return msg
        self.sock.settimeout(timeout)
        while True:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("connection closed")
            self._buf.extend(chunk)
            msg = self._try_parse()
            if msg is not None:
                return msg


# ── Pressed key / button tracking for clean release ─────────────────
class PressTracker:
    """Tracks currently-pressed keys and mouse buttons so they can be released.

    Used on the source side (release events are *sent* to the peer) and on the
    target side (release events are *injected* locally) to guarantee that no key
    or button stays stuck after stop / disconnect / shutdown.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.keys = set()
        self.buttons = set()

    def apply(self, ev):
        t = ev.get("type")
        with self._lock:
            if t == "key":
                self.keys.add(ev["code"])
            elif t == "key_up":
                self.keys.discard(ev["code"])
            elif t == "mousedown":
                self.buttons.add(ev["button"])
            elif t == "mouseup":
                self.buttons.discard(ev["button"])

    def release_events(self):
        """Return synthetic release events and clear the tracked state."""
        with self._lock:
            evs = [{"type": "key_up", "code": c} for c in sorted(self.keys)]
            evs += [{"type": "mouseup", "button": b} for b in sorted(self.buttons)]
            self.keys.clear()
            self.buttons.clear()
        return evs

    def clear(self):
        with self._lock:
            self.keys.clear()
            self.buttons.clear()
