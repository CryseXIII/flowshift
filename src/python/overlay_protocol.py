"""Validated JSON-byte protocol for the local FlowShift overlay process."""
from __future__ import annotations

import json
import math
import threading
import uuid


PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 65536
MAX_REQUEST_ID_LENGTH = 128
VALID_MESSAGE_TYPES = frozenset({
    "hello",
    "ready",
    "ping",
    "pong",
    "show_overlay",
    "hide_overlay",
    "overlay_visible",
    "overlay_hidden",
    "overlay_event",
    "shutdown",
    "error",
})

_TOP_LEVEL_FIELDS = {"protocol", "type", "request_id", "payload"}


class OverlayProtocolError(Exception):
    """A controlled protocol, transport, or request-correlation failure."""

    def __init__(self, code, message, request_id=None):
        self.code = str(code)
        self.message = str(message)
        self.request_id = request_id if isinstance(request_id, str) else None
        super().__init__(self.message)


def _fail(code, message, request_id=None):
    raise OverlayProtocolError(code, message, request_id)


def _request_id_from(value):
    if not isinstance(value, dict):
        return None
    request_id = value.get("request_id")
    if isinstance(request_id, str) and len(request_id) <= MAX_REQUEST_ID_LENGTH:
        return request_id
    return None


def _validate_request_id(request_id, allow_none=True):
    if request_id is None and allow_none:
        return
    if not isinstance(request_id, str):
        _fail("invalid_request_id", "request_id must be a string or null")
    if not request_id or len(request_id) > MAX_REQUEST_ID_LENGTH:
        _fail(
            "invalid_request_id",
            f"request_id must contain 1 to {MAX_REQUEST_ID_LENGTH} characters",
            request_id,
        )


def _require_exact_fields(payload, required, request_id, context):
    fields = set(payload)
    if fields != set(required):
        _fail(
            "invalid_payload",
            f"{context} payload fields must be exactly {sorted(required)}",
            request_id,
        )


def _require_int(value, name, request_id):
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("invalid_payload", f"{name} must be an integer", request_id)


def _require_nonempty_string(value, name, request_id):
    if not isinstance(value, str) or not value.strip():
        _fail("invalid_payload", f"{name} must be a non-empty string", request_id)


def _validate_payload(message_type, payload, request_id):
    if message_type == "hello":
        _require_exact_fields(payload, {"role", "pid"}, request_id, "hello")
        if payload["role"] not in ("runtime", "host"):
            _fail("invalid_payload", "hello role must be runtime or host", request_id)
        _require_int(payload["pid"], "hello pid", request_id)
    elif message_type == "ready":
        _require_exact_fields(payload, {"pid"}, request_id, "ready")
        _require_int(payload["pid"], "ready pid", request_id)
    elif message_type == "show_overlay":
        _require_exact_fields(
            payload, {"mode", "target", "x", "y", "data"}, request_id,
            "show_overlay",
        )
        if payload["mode"] not in ("clipboard", "command_wheel"):
            _fail("invalid_payload", "unsupported overlay mode", request_id)
        target = payload["target"]
        if not isinstance(target, dict):
            _fail("invalid_payload", "show_overlay target must be an object", request_id)
        _require_exact_fields(target, {"kind", "identity"}, request_id, "target")
        kind = target["kind"]
        identity = target["identity"]
        if kind not in ("local", "remote"):
            _fail("invalid_payload", "target kind must be local or remote", request_id)
        _require_nonempty_string(identity, "target identity", request_id)
        if identity != identity.strip():
            _fail("invalid_payload", "target identity must be trimmed", request_id)
        if kind == "local" and identity != "local":
            _fail("invalid_payload", "local target identity must be local", request_id)
        if kind == "remote" and identity == "local":
            _fail("invalid_payload", "remote target identity must identify a peer", request_id)
        _require_int(payload["x"], "show_overlay x", request_id)
        _require_int(payload["y"], "show_overlay y", request_id)
        if not isinstance(payload["data"], dict):
            _fail("invalid_payload", "show_overlay data must be an object", request_id)
    elif message_type == "overlay_event":
        fields = set(payload)
        if not {"event"}.issubset(fields) or not fields.issubset({"event", "data"}):
            _fail(
                "invalid_payload",
                "overlay_event payload requires event and optional data",
                request_id,
            )
        _require_nonempty_string(payload["event"], "overlay event", request_id)
        if "data" in payload and not isinstance(payload["data"], dict):
            _fail("invalid_payload", "overlay_event data must be an object", request_id)
    elif message_type == "error":
        _require_exact_fields(payload, {"code", "message"}, request_id, "error")
        _require_nonempty_string(payload["code"], "error code", request_id)
        _require_nonempty_string(payload["message"], "error message", request_id)


def _validate_message(message):
    request_id = _request_id_from(message)
    if not isinstance(message, dict):
        _fail("invalid_message", "message must be an object")
    if set(message) != _TOP_LEVEL_FIELDS:
        _fail(
            "invalid_message",
            f"message fields must be exactly {sorted(_TOP_LEVEL_FIELDS)}",
            request_id,
        )
    protocol = message["protocol"]
    if isinstance(protocol, bool) or protocol != PROTOCOL_VERSION:
        _fail("invalid_protocol", f"protocol must be {PROTOCOL_VERSION}", request_id)
    message_type = message["type"]
    if not isinstance(message_type, str) or message_type not in VALID_MESSAGE_TYPES:
        _fail("unknown_type", "unknown overlay message type", request_id)
    _validate_request_id(message["request_id"])
    request_id = message["request_id"]
    payload = message["payload"]
    if not isinstance(payload, dict):
        _fail("invalid_payload", "payload must be an object", request_id)
    _validate_payload(message_type, payload, request_id)
    return message


def make_message(message_type, payload=None, request_id=None):
    """Build and validate a protocol message."""
    message = {
        "protocol": PROTOCOL_VERSION,
        "type": message_type,
        "request_id": request_id,
        "payload": {} if payload is None else payload,
    }
    return _validate_message(message)


def make_error(code, message, request_id=None):
    """Build a correlated error response."""
    return make_message(
        "error", {"code": code, "message": message}, request_id=request_id,
    )


def encode_message(message):
    """Validate and encode one message as bounded UTF-8 JSON bytes."""
    try:
        validated = _validate_message(message)
        data = json.dumps(
            validated,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except OverlayProtocolError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        _fail("invalid_json", f"message is not JSON serializable: {exc}", _request_id_from(message))
    if len(data) > MAX_MESSAGE_BYTES:
        _fail(
            "message_too_large",
            f"message exceeds {MAX_MESSAGE_BYTES} bytes",
            _request_id_from(message),
        )
    return data


def decode_message(data):
    """Decode and validate one bounded UTF-8 JSON byte string."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        _fail("invalid_message", "message input must be bytes")
    raw = bytes(data)
    if len(raw) > MAX_MESSAGE_BYTES:
        _fail("message_too_large", f"message exceeds {MAX_MESSAGE_BYTES} bytes")
    try:
        message = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        _fail("invalid_json", f"invalid JSON message: {exc}")
    try:
        return _validate_message(message)
    except OverlayProtocolError:
        raise
    except Exception as exc:
        _fail("invalid_message", f"invalid message: {exc}", _request_id_from(message))


def send_message(connection, message_type, payload=None, request_id=None):
    """Encode and send one message without invoking pickle APIs."""
    if isinstance(message_type, dict):
        if payload is not None or request_id is not None:
            _fail("invalid_message", "payload/request_id cannot accompany a message object")
        message = message_type
    else:
        message = make_message(message_type, payload, request_id)
    data = encode_message(message)
    try:
        connection.send_bytes(data)
    except Exception as exc:
        _fail("transport_error", f"could not send overlay message: {exc}", request_id)


def receive_message(connection):
    """Receive one bounded JSON-byte message without invoking pickle APIs."""
    try:
        data = connection.recv_bytes(MAX_MESSAGE_BYTES)
    except (OSError, BufferError) as exc:
        if "bad message length" in str(exc).lower():
            _fail("message_too_large", f"could not receive bounded overlay message: {exc}")
        _fail("transport_error", f"could not receive overlay message: {exc}")
    except Exception as exc:
        _fail("transport_error", f"could not receive overlay message: {exc}")
    return decode_message(data)


def new_request_id():
    """Return a compact, process-independent correlation identifier."""
    return uuid.uuid4().hex


class _PendingRequest:
    __slots__ = ("event", "response", "error")

    def __init__(self):
        self.event = threading.Event()
        self.response = None
        self.error = None


class RequestTracker:
    """Bounded thread-safe request/response correlation by request_id."""

    def __init__(self, max_pending=128):
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending <= 0:
            raise ValueError("max_pending must be a positive integer")
        self.max_pending = max_pending
        self._lock = threading.Lock()
        self._requests = {}

    @property
    def pending_count(self):
        with self._lock:
            return sum(1 for item in self._requests.values() if not item.event.is_set())

    def register(self, request_id):
        _validate_request_id(request_id, allow_none=False)
        with self._lock:
            if request_id in self._requests:
                _fail("duplicate_request", "request_id is already registered", request_id)
            pending = sum(1 for item in self._requests.values() if not item.event.is_set())
            if pending >= self.max_pending:
                _fail("too_many_pending", "pending request limit reached", request_id)
            self._requests[request_id] = _PendingRequest()
        return request_id

    def resolve(self, response):
        if not isinstance(response, dict):
            _fail("invalid_response", "response must be an object")
        request_id = response.get("request_id")
        _validate_request_id(request_id, allow_none=False)
        with self._lock:
            item = self._requests.get(request_id)
            if item is None:
                _fail("unknown_request", "response has no pending request", request_id)
            if item.event.is_set():
                _fail("duplicate_response", "request was already resolved", request_id)
            item.response = response
            item.event.set()
        return True

    def wait(self, request_id, timeout):
        _validate_request_id(request_id, allow_none=False)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout < 0):
            _fail("invalid_timeout", "timeout must be a non-negative number", request_id)
        with self._lock:
            item = self._requests.get(request_id)
            if item is None:
                _fail("unknown_request", "request_id is not registered", request_id)
        signaled = item.event.wait(timeout)
        with self._lock:
            current = self._requests.get(request_id)
            if current is not item:
                _fail("unknown_request", "request_id is no longer registered", request_id)
            if not signaled and not item.event.is_set():
                del self._requests[request_id]
                _fail("timeout", "request timed out", request_id)
            del self._requests[request_id]
            error = item.error
            response = item.response
        if error is not None:
            raise error
        return response

    def cancel_all(self, message):
        text = str(message).strip() or "pending requests cancelled"
        with self._lock:
            count = 0
            for request_id, item in self._requests.items():
                if item.event.is_set():
                    continue
                item.error = OverlayProtocolError("cancelled", text, request_id)
                item.event.set()
                count += 1
        return count
