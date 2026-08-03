"""Strict typed framing for the dedicated clipboard transfer V2 channel."""
from __future__ import annotations

import hashlib
import json
import math
import select
import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass


PROTOCOL_MAJOR = 2
FRAME_KIND_JSON_CONTROL = 1
FRAME_KIND_BINARY_PAYLOAD = 2
CHECKSUM_NONE = 0
CHECKSUM_SHA256 = 1

UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1
MAX_JSON_CONTROL_BYTES = 16 * 1024 ** 2
MAX_BINARY_PAYLOAD_BYTES = 4 * 1024 ** 2
MAX_ENTRY_INDEX = 200_000 - 1
MAX_LOGICAL_OFFSET = 100 * 1024 ** 4
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 2_000_000
MAX_JSON_INTEGER_DIGITS = 20

_COMMON_HEADER = struct.Struct("!BBH")
_BINARY_HEADER = struct.Struct("!16sIQIB")
_LENGTH_PREFIX = struct.Struct("!I")
_SHA256_BYTES = hashlib.sha256().digest_size
_MAX_JSON_FRAME_BYTES = _COMMON_HEADER.size + MAX_JSON_CONTROL_BYTES
_MAX_BINARY_FRAME_BYTES = (
    _COMMON_HEADER.size + _BINARY_HEADER.size + _SHA256_BYTES
    + MAX_BINARY_PAYLOAD_BYTES
)
MAX_TYPED_FRAME_BYTES = max(_MAX_JSON_FRAME_BYTES, _MAX_BINARY_FRAME_BYTES)
_READ_SIZE = 64 * 1024


class TypedFrameError(ValueError):
    """Raised when a typed V2 frame violates the wire contract."""


@dataclass(frozen=True)
class JsonControlFrame:
    message: dict


@dataclass(frozen=True)
class BinaryPayloadFrame:
    transfer_id: str
    entry_index: int
    offset: int
    payload: bytes
    checksum_kind: int
    checksum: bytes | None


def _checked_uint(value, maximum, name):
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        raise TypedFrameError(f"{name} is outside its unsigned wire range")
    return value


def _transfer_uuid(value):
    if isinstance(value, uuid.UUID):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError) as exc:
            raise TypedFrameError("transfer_id must be a valid UUID") from exc
    else:
        raise TypedFrameError("transfer_id must be a valid UUID")
    if parsed.int == 0:
        raise TypedFrameError("transfer_id must not be the null UUID")
    return parsed


def _json_bytes(message):
    if not isinstance(message, dict):
        raise TypedFrameError("JSON control payload must be an object")

    _validate_json_value(message)
    try:
        payload = json.dumps(
            message, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise TypedFrameError("JSON control payload is not valid JSON data") from exc
    if len(payload) > MAX_JSON_CONTROL_BYTES:
        raise TypedFrameError("JSON control payload exceeds the protocol limit")
    return payload


def _validate_json_value(value):
    pending = [(value, 1)]
    visited = 0
    while pending:
        current, depth = pending.pop()
        visited += 1
        if visited > MAX_JSON_NODES:
            raise TypedFrameError("JSON control payload contains too many values")
        if depth > MAX_JSON_DEPTH:
            raise TypedFrameError("JSON control payload nesting is too deep")
        if isinstance(current, dict):
            if any(not isinstance(key, str) for key in current):
                raise TypedFrameError("JSON control object keys must be strings")
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend((child, depth + 1) for child in current)
        elif isinstance(current, float) and not math.isfinite(current):
            raise TypedFrameError("JSON control payload contains a non-finite number")
        elif isinstance(current, int) and not isinstance(current, bool):
            if current < -UINT64_MAX or current > UINT64_MAX:
                raise TypedFrameError("JSON control integer exceeds the protocol range")


def _pack_frame(kind, body):
    following_length = _COMMON_HEADER.size + len(body)
    return (
        _LENGTH_PREFIX.pack(following_length)
        + _COMMON_HEADER.pack(kind, PROTOCOL_MAJOR, 0)
        + body
    )


def encode_json_control_frame(message):
    """Encode one bounded JSON-object control frame."""
    return _pack_frame(FRAME_KIND_JSON_CONTROL, _json_bytes(message))


def encode_binary_payload_frame(
        transfer_id, entry_index, offset, payload, *,
        checksum_kind=CHECKSUM_SHA256, checksum=None):
    """Encode one bounded raw clipboard payload frame."""
    transfer_uuid = _transfer_uuid(transfer_id)
    entry_index = _checked_uint(entry_index, MAX_ENTRY_INDEX, "entry_index")
    offset = _checked_uint(offset, MAX_LOGICAL_OFFSET, "offset")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypedFrameError("payload must be bytes-like")
    payload = bytes(payload)
    if len(payload) > MAX_BINARY_PAYLOAD_BYTES:
        raise TypedFrameError("binary payload exceeds the protocol limit")
    if offset + len(payload) > MAX_LOGICAL_OFFSET:
        raise TypedFrameError("payload end offset exceeds the protocol limit")

    if checksum_kind == CHECKSUM_NONE:
        if checksum is not None:
            raise TypedFrameError("checksum bytes require a checksum kind")
        checksum_bytes = b""
    elif checksum_kind == CHECKSUM_SHA256:
        actual = hashlib.sha256(payload).digest()
        if checksum is not None and not isinstance(checksum, (bytes, bytearray, memoryview)):
            raise TypedFrameError("checksum must be bytes-like")
        checksum_bytes = actual if checksum is None else bytes(checksum)
        if len(checksum_bytes) != _SHA256_BYTES or checksum_bytes != actual:
            raise TypedFrameError("binary payload checksum does not match")
    else:
        raise TypedFrameError("unsupported checksum kind")

    body = _BINARY_HEADER.pack(
        transfer_uuid.bytes, entry_index, offset, len(payload), checksum_kind
    ) + checksum_bytes + payload
    return _pack_frame(FRAME_KIND_BINARY_PAYLOAD, body)


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TypedFrameError("JSON control payload contains duplicate keys")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise TypedFrameError(f"JSON control payload contains invalid constant {value}")


def _parse_json_int(value):
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise TypedFrameError("JSON control integer exceeds the protocol range")
    parsed = int(value)
    if parsed < -UINT64_MAX or parsed > UINT64_MAX:
        raise TypedFrameError("JSON control integer exceeds the protocol range")
    return parsed


def _parse_json_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise TypedFrameError("JSON control payload contains a non-finite number")
    return parsed


def _decode_json_control(body):
    if len(body) > MAX_JSON_CONTROL_BYTES:
        raise TypedFrameError("JSON control payload exceeds the protocol limit")
    try:
        message = json.loads(
            body.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant, parse_int=_parse_json_int,
            parse_float=_parse_json_float,
        )
    except TypedFrameError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise TypedFrameError("malformed JSON control payload") from exc
    if not isinstance(message, dict):
        raise TypedFrameError("JSON control payload must be an object")
    _validate_json_value(message)
    return JsonControlFrame(message)


def _decode_binary_payload(body):
    if len(body) < _BINARY_HEADER.size:
        raise TypedFrameError("binary payload frame header is truncated")
    raw_uuid, entry_index, offset, payload_length, checksum_kind = (
        _BINARY_HEADER.unpack(body[:_BINARY_HEADER.size])
    )
    transfer_uuid = uuid.UUID(bytes=raw_uuid)
    if transfer_uuid.int == 0:
        raise TypedFrameError("transfer_id must not be the null UUID")
    if payload_length > MAX_BINARY_PAYLOAD_BYTES:
        raise TypedFrameError("binary payload exceeds the protocol limit")
    if entry_index > MAX_ENTRY_INDEX:
        raise TypedFrameError("entry_index exceeds the protocol limit")
    if offset > MAX_LOGICAL_OFFSET or offset + payload_length > MAX_LOGICAL_OFFSET:
        raise TypedFrameError("payload end offset exceeds the protocol limit")
    if checksum_kind == CHECKSUM_NONE:
        checksum_length = 0
    elif checksum_kind == CHECKSUM_SHA256:
        checksum_length = _SHA256_BYTES
    else:
        raise TypedFrameError("unsupported checksum kind")
    expected_length = _BINARY_HEADER.size + checksum_length + payload_length
    if len(body) != expected_length:
        raise TypedFrameError("binary payload length does not match frame length")
    checksum_start = _BINARY_HEADER.size
    payload_start = checksum_start + checksum_length
    checksum = bytes(body[checksum_start:payload_start]) or None
    payload = bytes(body[payload_start:])
    if checksum is not None and checksum != hashlib.sha256(payload).digest():
        raise TypedFrameError("binary payload checksum does not match")
    return BinaryPayloadFrame(
        transfer_id=transfer_uuid.hex,
        entry_index=entry_index,
        offset=offset,
        payload=payload,
        checksum_kind=checksum_kind,
        checksum=checksum,
    )


class TypedFrameReader:
    """Timeout-tolerant incremental reader for a switched V2 channel."""

    def __init__(self, sock):
        self.sock = sock
        self._buf = bytearray()
        self._failed = False

    def _try_parse(self):
        if self._failed:
            raise TypedFrameError("typed frame reader is unusable after a protocol error")
        try:
            return self._try_parse_buffer()
        except TypedFrameError:
            self._failed = True
            raise

    def _try_parse_buffer(self):
        if len(self._buf) < _LENGTH_PREFIX.size:
            return None
        following_length = _LENGTH_PREFIX.unpack(self._buf[:_LENGTH_PREFIX.size])[0]
        if following_length < _COMMON_HEADER.size:
            raise TypedFrameError("typed frame is shorter than its common header")
        if following_length > MAX_TYPED_FRAME_BYTES:
            raise TypedFrameError("typed frame exceeds the protocol limit")
        if len(self._buf) < _LENGTH_PREFIX.size + _COMMON_HEADER.size:
            return None
        header_start = _LENGTH_PREFIX.size
        header_end = header_start + _COMMON_HEADER.size
        kind, major, flags = _COMMON_HEADER.unpack(self._buf[header_start:header_end])
        if kind not in (FRAME_KIND_JSON_CONTROL, FRAME_KIND_BINARY_PAYLOAD):
            raise TypedFrameError("unknown typed frame kind")
        if major != PROTOCOL_MAJOR:
            raise TypedFrameError("unsupported typed frame protocol major")
        if flags != 0:
            raise TypedFrameError("unsupported typed frame flags")
        kind_limit = (
            _MAX_JSON_FRAME_BYTES
            if kind == FRAME_KIND_JSON_CONTROL else _MAX_BINARY_FRAME_BYTES
        )
        if following_length > kind_limit:
            raise TypedFrameError("typed frame kind exceeds its protocol limit")
        complete_length = _LENGTH_PREFIX.size + following_length
        if len(self._buf) < complete_length:
            return None
        body = bytes(self._buf[header_end:complete_length])
        del self._buf[:complete_length]
        if kind == FRAME_KIND_JSON_CONTROL:
            return _decode_json_control(body)
        return _decode_binary_payload(body)

    def read_frame(self, timeout):
        """Return a frame, or ``None`` on timeout while retaining partial data."""
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 0:
            raise ValueError("timeout must be a non-negative number")
        frame = self._try_parse()
        if frame is not None:
            return frame
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            readable, _, _ = select.select((self.sock,), (), (), remaining)
            if not readable:
                return None
            try:
                chunk = self.sock.recv(_READ_SIZE)
            except (BlockingIOError, socket.timeout):
                continue
            if not chunk:
                raise ConnectionError("connection closed")
            self._buf.extend(chunk)
            frame = self._try_parse()
            if frame is not None:
                return frame


class TypedFrameWriter:
    """Per-socket writer that prevents concurrent typed frames interleaving."""

    def __init__(self, sock):
        self.sock = sock
        self._lock = threading.Lock()

    def send_json_control(self, message):
        with self._lock:
            frame = encode_json_control_frame(message)
            self.sock.sendall(frame)

    def send_binary_payload(
            self, transfer_id, entry_index, offset, payload, *,
            checksum_kind=CHECKSUM_SHA256, checksum=None):
        with self._lock:
            frame = encode_binary_payload_frame(
                transfer_id, entry_index, offset, payload,
                checksum_kind=checksum_kind, checksum=checksum,
            )
            self.sock.sendall(frame)
