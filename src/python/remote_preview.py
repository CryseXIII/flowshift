"""Remote screen preview, stage 1: settings, messages, lifecycle and the lock.

When control moves to another computer, the source asks that computer for a
small live preview of its screen (Kanban card 833). This module holds the
settings (``config["remote_preview"]``), the four control messages
``preview_request`` / ``preview_accept`` / ``preview_reject`` / ``preview_stop``,
the session lifecycle and the authorization rule. Capture and encoding are in
``preview_capture`` (stage 2), the picture transfer with flow control in
``preview_stream`` (stage 3), the window in ``preview_window`` (stage 4).
``preview_authorization`` lets only paired devices (device ID in the own
device list) see the screen; secured device pairing (Kanban card 722) replaces
that rule later.

Pure module (standard library only, no Windows imports), unit-testable on any OS.
"""
from __future__ import annotations

import json
import re
import threading
import uuid

# ── Settings ────────────────────────────────────────────────────────
FPS_MIN, FPS_MAX = 5, 30
MAX_WIDTH_MIN, MAX_WIDTH_MAX = 480, 1920
WINDOW_WIDTH_MIN, WINDOW_WIDTH_MAX = 200, 1200
MONITOR_INDEX_MAX = 63
MONITOR_NAMES = ("cursor", "primary")
WINDOW_CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")

DEFAULT_SETTINGS = {
    "enabled": False,
    "fps": 15,
    "max_width": 960,
    "monitor": "cursor",
    "cursor_crop": False,
    "window": {"corner": "bottom_right", "width_px": 480},
}


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _int_in(value, low, high):
    return _is_int(value) and low <= value <= high


def _valid_monitor(value):
    return value in MONITOR_NAMES or _int_in(value, 0, MONITOR_INDEX_MAX)


def normalize_settings(raw, errors=None):
    """Return complete preview settings; invalid values fall back to the default.

    Never raises. When ``errors`` is a list, the name of every given field that
    was invalid or unknown is appended, so the Web-API can refuse the request
    instead of silently storing a default.
    """
    def bad(name):
        if errors is not None:
            errors.append(name)

    if raw is not None and not isinstance(raw, dict):
        bad("remote_preview")
    raw = raw if isinstance(raw, dict) else {}
    out = {key: value for key, value in DEFAULT_SETTINGS.items() if key != "window"}
    for key in raw:
        if key not in DEFAULT_SETTINGS:
            bad(key)
    if "enabled" in raw:
        if isinstance(raw["enabled"], bool):
            out["enabled"] = raw["enabled"]
        else:
            bad("enabled")
    if "fps" in raw:
        if _int_in(raw["fps"], FPS_MIN, FPS_MAX):
            out["fps"] = raw["fps"]
        else:
            bad("fps")
    if "max_width" in raw:
        if _int_in(raw["max_width"], MAX_WIDTH_MIN, MAX_WIDTH_MAX):
            out["max_width"] = raw["max_width"]
        else:
            bad("max_width")
    if "monitor" in raw:
        if _valid_monitor(raw["monitor"]):
            out["monitor"] = raw["monitor"]
        else:
            bad("monitor")
    if "cursor_crop" in raw:
        if isinstance(raw["cursor_crop"], bool):
            out["cursor_crop"] = raw["cursor_crop"]
        else:
            bad("cursor_crop")
    window = dict(DEFAULT_SETTINGS["window"])
    raw_window = raw.get("window")
    if "window" in raw and not isinstance(raw_window, dict):
        bad("window")
    if isinstance(raw_window, dict):
        for key in raw_window:
            if key not in window:
                bad(f"window.{key}")
        if "corner" in raw_window:
            if raw_window["corner"] in WINDOW_CORNERS:
                window["corner"] = raw_window["corner"]
            else:
                bad("window.corner")
        if "width_px" in raw_window:
            if _int_in(raw_window["width_px"], WINDOW_WIDTH_MIN, WINDOW_WIDTH_MAX):
                window["width_px"] = raw_window["width_px"]
            else:
                bad("window.width_px")
    out["window"] = window
    return out


# ── Messages ────────────────────────────────────────────────────────
PREVIEW_REQUEST = "preview_request"
PREVIEW_ACCEPT = "preview_accept"
PREVIEW_REJECT = "preview_reject"
PREVIEW_STOP = "preview_stop"
MESSAGE_TYPES = (PREVIEW_REQUEST, PREVIEW_ACCEPT, PREVIEW_REJECT, PREVIEW_STOP)

MAX_MESSAGE_BYTES = 2048
MAX_SESSION_ID_LEN = 64
MAX_REASON_LEN = 64
# Session ids and reasons end up in logs and the dashboard: plain tokens only.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,%d}$" % MAX_SESSION_ID_LEN)
_REASON_RE = re.compile(r"^[a-z0-9_]{1,%d}$" % MAX_REASON_LEN)

REASON_PAIRING_REQUIRED = "pairing_required"
REASON_INVALID_REQUEST = "invalid_request"
REASON_INVALID_MESSAGE = "invalid_message"
REASON_STOPPED = "stopped"
REASON_UNAVAILABLE = "preview_unavailable"
REASON_DISABLED = "preview_disabled"
REASON_BUSY = "preview_busy"
REASON_CAPTURE_UNAVAILABLE = "capture_unavailable"
REASON_WINDOW_UNAVAILABLE = "window_unavailable"
REASON_PEER_TOO_OLD = "peer_too_old"


_REASON_TEXTS = {
    REASON_PAIRING_REQUIRED: "the other PC does not have this PC with its device ID in its "
                             "peer list (Scan Network there, or connect once after typing it in)",
    # Sent by versions up to 0.7.6, which had no picture transfer at all.
    REASON_UNAVAILABLE: "the other PC runs an older FlowShift version that cannot send the "
                        "picture (update it to 0.7.7 or newer)",
    REASON_PEER_TOO_OLD: "the other PC runs an older FlowShift version that cannot send the "
                         "picture (update it to 0.7.7 or newer)",
    REASON_DISABLED: "Remote Preview is switched off on the other PC (Settings > Remote Preview "
                     "there)",
    REASON_BUSY: "the other PC already sends its picture to another PC",
    REASON_CAPTURE_UNAVAILABLE: "the other PC cannot take pictures of its screen (see the "
                                "self-test in Settings > Remote Preview there)",
    REASON_WINDOW_UNAVAILABLE: "this PC cannot open the preview window (see the self-test in "
                               "Settings > Remote Preview)",
    REASON_INVALID_REQUEST: "the request had values outside the allowed ranges",
    REASON_INVALID_MESSAGE: "the message was not readable",
    REASON_STOPPED: "the preview was stopped",
    "stalled": "the pictures stopped because the controlling PC no longer confirmed them",
    "send_failed": "the connection ended while a picture was sent",
}


def reason_text(reason):
    """Plain sentence for a preview reason code (log, Event Log)."""
    return _REASON_TEXTS.get(reason, "no reason given")


def new_session_id():
    return uuid.uuid4().hex


def valid_session_id(value):
    return isinstance(value, str) and bool(_SESSION_ID_RE.match(value))


def build_request(session_id, settings):
    settings = normalize_settings(settings)
    return {
        "type": PREVIEW_REQUEST,
        "session_id": session_id,
        "fps": settings["fps"],
        "max_width": settings["max_width"],
        "monitor": settings["monitor"],
        "cursor_crop": settings["cursor_crop"],
    }


def build_reject(session_id, reason):
    return {"type": PREVIEW_REJECT, "session_id": session_id, "reason": reason}


def build_stop(session_id, reason=REASON_STOPPED):
    return {"type": PREVIEW_STOP, "session_id": session_id, "reason": reason}


def _message_size(msg):
    try:
        return len(json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return MAX_MESSAGE_BYTES + 1


def parse_message(msg):
    """Validate one preview message from a peer.

    Returns ``(True, fields)`` with only the known, checked fields, or
    ``(False, reason)``. Never raises; unknown fields are ignored.
    """
    if not isinstance(msg, dict):
        return False, REASON_INVALID_MESSAGE
    kind = msg.get("type")
    if kind not in MESSAGE_TYPES:
        return False, REASON_INVALID_MESSAGE
    if _message_size(msg) > MAX_MESSAGE_BYTES:
        return False, REASON_INVALID_MESSAGE
    session_id = msg.get("session_id")
    if not valid_session_id(session_id):
        return False, REASON_INVALID_MESSAGE
    fields = {"type": kind, "session_id": session_id}
    if kind in (PREVIEW_REQUEST, PREVIEW_ACCEPT):
        if not _int_in(msg.get("fps"), FPS_MIN, FPS_MAX):
            return False, REASON_INVALID_REQUEST
        if not _int_in(msg.get("max_width"), MAX_WIDTH_MIN, MAX_WIDTH_MAX):
            return False, REASON_INVALID_REQUEST
        fields["fps"] = msg["fps"]
        fields["max_width"] = msg["max_width"]
    if kind == PREVIEW_REQUEST:
        if not _valid_monitor(msg.get("monitor")):
            return False, REASON_INVALID_REQUEST
        if not isinstance(msg.get("cursor_crop"), bool):
            return False, REASON_INVALID_REQUEST
        fields["monitor"] = msg["monitor"]
        fields["cursor_crop"] = msg["cursor_crop"]
    if kind == PREVIEW_REJECT:
        reason = msg.get("reason")
        if not (isinstance(reason, str) and _REASON_RE.match(reason)):
            return False, REASON_INVALID_MESSAGE
        fields["reason"] = reason
    if kind == PREVIEW_STOP:
        reason = msg.get("reason", REASON_STOPPED)
        if not (isinstance(reason, str) and _REASON_RE.match(reason)):
            reason = REASON_STOPPED
        fields["reason"] = reason
    return True, fields


# ── Authorization ───────────────────────────────────────────────────
def preview_authorization(peer_meta, paired_peer=None):
    """Decide whether a peer may receive a preview of this screen.

    This is the single exchange point for pairing. Paired is the same rule the
    update from a connected PC uses (``peer_update.trusted_peer``): the link
    identity is ``device:<id>`` and this PC's own device list has an entry with
    that device ID (found by Scan Network, or typed in by address and filled in
    at the first connection; Viktor 26.11.). ``paired_peer`` is that entry,
    looked up by the runtime, never a claim from the message. Secured pairing
    (Kanban card 722) will replace the rule here.
    """
    if not isinstance(peer_meta, dict) or not isinstance(paired_peer, dict):
        return False, REASON_PAIRING_REQUIRED
    identity = str(peer_meta.get("identity") or "").strip().lower()
    device_id = str(paired_peer.get("device_id") or "").strip().lower()
    if not device_id or identity != f"device:{device_id}":
        return False, REASON_PAIRING_REQUIRED
    return True, ""


# ── Session lifecycle ───────────────────────────────────────────────
IDLE = "idle"
REQUESTED = "requested"
STREAMING = "streaming"
STOPPING = "stopping"
# Shown instead of ``idle`` while the last request was refused and not stopped.
REJECTED = "rejected"


class PreviewSession:
    """One preview session per connection: idle -> requested -> streaming -> stopping -> idle.

    This is the controlling PC's side: ``streaming`` after the other PC's
    ``preview_accept``, while its pictures come. Duplicate requests and
    messages with a foreign session id are discarded (the method returns
    ``False``).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.state = IDLE
        self.session_id = None
        self.reason = ""

    def request(self, session_id):
        with self._lock:
            if self.state != IDLE or not valid_session_id(session_id):
                return False
            self.state = REQUESTED
            self.session_id = session_id
            self.reason = ""
            return True

    def accept(self, session_id):
        with self._lock:
            if self.state != REQUESTED or session_id != self.session_id:
                return False
            self.state = STREAMING
            return True

    def reject(self, session_id, reason):
        with self._lock:
            if self.state != REQUESTED or session_id != self.session_id:
                return False
            self.state = IDLE
            self.session_id = None
            self.reason = reason if isinstance(reason, str) and _REASON_RE.match(reason) \
                else REASON_INVALID_MESSAGE
            return True

    def stop(self, session_id=None):
        """Begin stopping. Returns the session id the peer must be told about, or ``None``.

        ``session_id=None`` stops whatever runs (local cause: return, cancel,
        disconnect, shutdown); a peer's ``preview_stop`` passes its id and is
        discarded when it is foreign. A stop from ``idle`` only clears the
        last rejection.
        """
        with self._lock:
            if session_id is not None and session_id != self.session_id:
                return None
            if self.state in (REQUESTED, STREAMING):
                self.state = STOPPING
                return self.session_id
            if self.state == IDLE:
                self.reason = ""
            return None

    def stopped(self):
        """Finish a stop: back to ``idle`` without a reason."""
        with self._lock:
            if self.state != STOPPING:
                return False
            self.state = IDLE
            self.session_id = None
            self.reason = ""
            return True

    def snapshot(self):
        with self._lock:
            state = REJECTED if self.state == IDLE and self.reason else self.state
            return {"state": state, "reason": self.reason}
