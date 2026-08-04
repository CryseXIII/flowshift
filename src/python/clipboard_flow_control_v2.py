"""Bounded flow control and cumulative ACK batching for clipboard stream V2."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import threading
import time
import uuid

import clipboard_protocol as protocol
from clipboard_framing_v2 import (
    MAX_BINARY_PAYLOAD_BYTES, MAX_ENTRY_INDEX, MAX_LOGICAL_OFFSET,
)


MIB = 1024 * 1024


class FlowControlError(ValueError):
    """Raised when V2 flow-control state violates the protocol contract."""


class FlowControlClosed(RuntimeError):
    """Raised when blocked flow-control work is closed or cancelled."""


class FlowControlTimeout(TimeoutError):
    """Raised when admission, queue, or ACK progress exceeds its deadline."""


def _positive_int(value, name):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_seconds(value, name):
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} must be positive and finite")
    return float(value)


def normalize_transfer_id(value):
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
    except (ValueError, AttributeError):
        parsed = None
    if parsed is None or parsed.int == 0:
        raise FlowControlError("transfer_id must be a non-null UUID")
    return parsed.hex


def _uint(value, name, maximum=(1 << 64) - 1):
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 0 <= value <= maximum):
        raise FlowControlError(f"{name} is outside its unsigned range")
    return value


def _deadline(timeout):
    if timeout is None:
        return None
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout < 0):
        raise ValueError("timeout must be non-negative and finite")
    return time.monotonic() + float(timeout)


def _remaining(deadline):
    if deadline is None:
        return None
    return deadline - time.monotonic()


@dataclass(frozen=True)
class FlowControlLimits:
    chunk_size: int = 2 * MIB
    max_active_transfers_global: int = 2
    max_active_transfers_per_peer: int = 1
    max_inflight_chunks_per_transfer: int = 4
    max_inflight_bytes_global: int = 16 * MIB
    max_send_queue_chunks: int = 4
    max_send_queue_bytes: int = 16 * MIB
    max_receive_queue_chunks: int = 4
    max_receive_queue_bytes: int = 16 * MIB
    ack_bytes: int = 8 * MIB
    ack_chunks: int = 4
    ack_interval_seconds: float = 0.250
    window_ack_timeout_seconds: float = 30.0

    def __post_init__(self):
        chunk_size = _positive_int(self.chunk_size, "chunk_size")
        if not MIB <= chunk_size <= MAX_BINARY_PAYLOAD_BYTES:
            raise ValueError("chunk_size must be between 1 and 4 MiB")
        for name in (
                "max_active_transfers_global", "max_active_transfers_per_peer",
                "max_inflight_chunks_per_transfer", "max_inflight_bytes_global",
                "max_send_queue_chunks", "max_send_queue_bytes",
                "max_receive_queue_chunks", "max_receive_queue_bytes",
                "ack_bytes", "ack_chunks"):
            _positive_int(getattr(self, name), name)
        if self.max_active_transfers_per_peer > self.max_active_transfers_global:
            raise ValueError("per-peer transfer limit exceeds global limit")
        _positive_seconds(self.ack_interval_seconds, "ack_interval_seconds")
        _positive_seconds(self.window_ack_timeout_seconds,
                          "window_ack_timeout_seconds")


class BoundedChunkQueue:
    """A FIFO that applies backpressure by both item count and payload bytes."""

    def __init__(self, max_chunks, max_bytes, *, size_fn=len, freeze_fn=None):
        self.max_chunks = _positive_int(max_chunks, "max_chunks")
        self.max_bytes = _positive_int(max_bytes, "max_bytes")
        if not callable(size_fn):
            raise ValueError("size_fn must be callable")
        if freeze_fn is not None and not callable(freeze_fn):
            raise ValueError("freeze_fn must be callable")
        if size_fn is not len and freeze_fn is None:
            raise ValueError("custom size_fn requires an immutable freeze_fn")
        self._size_fn = size_fn
        self._freeze_fn = freeze_fn
        self._items = deque()
        self._bytes = 0
        self._high_chunks = 0
        self._high_bytes = 0
        self._closed = False
        self._on_close = None
        self._waiting_producers = 0
        self._waiting_consumers = 0
        self._condition = threading.Condition()

    def put(self, item, timeout=None):
        if self._freeze_fn is not None:
            item = self._freeze_fn(item)
        elif isinstance(item, (bytearray, memoryview)):
            item = bytes(item)
        elif not isinstance(item, (bytes, str, int, float, bool, type(None))):
            raise FlowControlError("mutable or composite queue items require freeze_fn")
        size = _uint(self._size_fn(item), "chunk size")
        if size > self.max_bytes:
            raise FlowControlError("chunk exceeds queue byte capacity")
        deadline = _deadline(timeout)
        with self._condition:
            while (len(self._items) >= self.max_chunks
                   or self._bytes + size > self.max_bytes):
                if self._closed:
                    raise FlowControlClosed("chunk queue is closed")
                remaining = _remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise FlowControlTimeout("chunk queue put timed out")
                self._waiting_producers += 1
                try:
                    self._condition.wait(remaining)
                finally:
                    self._waiting_producers -= 1
            if self._closed:
                raise FlowControlClosed("chunk queue is closed")
            self._items.append((item, size))
            self._bytes += size
            self._high_chunks = max(self._high_chunks, len(self._items))
            self._high_bytes = max(self._high_bytes, self._bytes)
            self._condition.notify_all()

    def get(self, timeout=None):
        deadline = _deadline(timeout)
        with self._condition:
            while not self._items:
                if self._closed:
                    raise FlowControlClosed("chunk queue is closed")
                remaining = _remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise FlowControlTimeout("chunk queue get timed out")
                self._waiting_consumers += 1
                try:
                    self._condition.wait(remaining)
                finally:
                    self._waiting_consumers -= 1
            item, size = self._items.popleft()
            self._bytes -= size
            self._condition.notify_all()
            return item

    def close(self, *, discard=True):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            if discard:
                self._items.clear()
                self._bytes = 0
            self._condition.notify_all()
            on_close = self._on_close
            self._on_close = None
        if on_close is not None:
            on_close(self)

    def snapshot(self):
        with self._condition:
            return {
                "closed": self._closed,
                "queued_chunks": len(self._items),
                "queued_bytes": self._bytes,
                "high_water_chunks": self._high_chunks,
                "high_water_bytes": self._high_bytes,
                "waiting_producers": self._waiting_producers,
                "waiting_consumers": self._waiting_consumers,
            }


@dataclass(frozen=True)
class _InflightChunk:
    entry_index: int
    offset: int
    end_offset: int
    payload: object
    size: int
    sent_at: float


class SendWindow:
    """One transfer's bounded set of receiver-unacknowledged payload chunks."""

    def __init__(self, coordinator, transfer_id, peer_identity):
        self._coordinator = coordinator
        self.transfer_id = transfer_id
        self.peer_identity = peer_identity
        self._chunks = deque()
        self._next_offsets = {}
        self._acked_offsets = {}
        self._durable_offsets = {}
        self._resume_offsets = {}
        self._payload_entries = set()
        self._receiver_state = "transferring"
        self._closed = False
        self._error = None

    def initialize_resume(self, entry_index, durable_offset):
        entry_index = _uint(entry_index, "entry_index", MAX_ENTRY_INDEX)
        durable_offset = _uint(
            durable_offset, "durable_offset", MAX_LOGICAL_OFFSET)
        with self._coordinator._condition:
            self._raise_if_closed()
            if entry_index in self._payload_entries:
                raise FlowControlError(
                    "resume baseline must be initialized before payload")
            previous = self._resume_offsets.get(entry_index)
            if previous is not None:
                if durable_offset < previous:
                    raise FlowControlError("resume baseline cannot regress")
                if durable_offset != previous:
                    raise FlowControlError("resume baseline is already initialized")
                return
            if entry_index in self._next_offsets:
                raise FlowControlError(
                    "resume baseline must be initialized before payload")
            self._resume_offsets[entry_index] = durable_offset
            self._next_offsets[entry_index] = durable_offset
            self._acked_offsets[entry_index] = durable_offset
            self._durable_offsets[entry_index] = durable_offset

    def track_sent(self, entry_index, offset, payload, timeout=None):
        entry_index = _uint(entry_index, "entry_index", MAX_ENTRY_INDEX)
        offset = _uint(offset, "offset", MAX_LOGICAL_OFFSET)
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise FlowControlError("payload must be bytes-like")
        payload = bytes(payload)
        size = len(payload)
        if not 0 < size <= MAX_BINARY_PAYLOAD_BYTES or offset + size > MAX_LOGICAL_OFFSET:
            raise FlowControlError("payload geometry exceeds the V2 wire range")
        if size > self._coordinator.limits.max_inflight_bytes_global:
            raise FlowControlError("payload exceeds the global in-flight byte limit")
        deadline = _deadline(timeout)
        ack_deadline = time.monotonic() + self._coordinator.limits.window_ack_timeout_seconds
        deadline = ack_deadline if deadline is None else min(deadline, ack_deadline)
        condition = self._coordinator._condition
        with condition:
            if self._chunks:
                deadline = min(deadline, self._chunks[0].sent_at
                               + self._coordinator.limits.window_ack_timeout_seconds)
            expected = self._next_offsets.get(entry_index)
            if expected is not None and offset != expected:
                raise FlowControlError("payload offset is not contiguous")
            while (len(self._chunks) >= self._coordinator.limits.max_inflight_chunks_per_transfer
                   or self._coordinator._inflight_bytes + size
                   > self._coordinator.limits.max_inflight_bytes_global):
                self._raise_if_closed()
                remaining = _remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise FlowControlTimeout("send window is full")
                condition.wait(remaining)
            self._raise_if_closed()
            expected = self._next_offsets.get(entry_index)
            if expected is not None and offset != expected:
                raise FlowControlError("payload offset is not contiguous")
            now = time.monotonic()
            self._chunks.append(_InflightChunk(
                entry_index, offset, offset + size, payload, size, now))
            self._coordinator._inflight_bytes += size
            self._next_offsets[entry_index] = offset + size
            self._acked_offsets.setdefault(entry_index, offset)
            self._durable_offsets.setdefault(entry_index, 0)
            self._payload_entries.add(entry_index)

    def apply_ack(self, ack):
        parsed = protocol.parse_stream_v2_ack(ack)
        if parsed is None:
            raise FlowControlError("invalid cumulative ACK")
        if parsed["transfer_id"] != self.transfer_id:
            return {"released_chunks": 0, "released_bytes": 0,
                    "discarded_bytes": 0,
                    "receiver_state": self._receiver_state}
        entry_index = parsed["entry_index"]
        verified_offset = parsed["verified_offset"]
        durable_offset = parsed["durable_offset"]
        receiver_state = parsed["receiver_state"]
        condition = self._coordinator._condition
        with condition:
            self._raise_if_closed()
            current = self._acked_offsets.get(entry_index)
            if current is None:
                if receiver_state == "transferring":
                    return {"released_chunks": 0, "released_bytes": 0,
                            "discarded_bytes": 0, "receiver_state": receiver_state}
                raise FlowControlError("receiver state references an unknown entry")
            current_durable = self._durable_offsets[entry_index]
            if durable_offset < current_durable:
                raise FlowControlError("durable ACK offset cannot regress")
            if verified_offset < current and receiver_state == "transferring":
                self._durable_offsets[entry_index] = durable_offset
                return {"released_chunks": 0, "released_bytes": 0,
                        "discarded_bytes": 0, "receiver_state": receiver_state}
            if verified_offset < current:
                verified_offset = current
            candidates = [chunk for chunk in self._chunks
                          if chunk.entry_index == entry_index]
            if candidates and verified_offset > candidates[-1].end_offset:
                raise FlowControlError("ACK exceeds sent data")
            if not candidates and verified_offset > current:
                raise FlowControlError("ACK exceeds sent data")
            boundaries = {current} | {chunk.end_offset for chunk in candidates}
            if verified_offset not in boundaries:
                raise FlowControlError("ACK offset is not a chunk boundary")
            released_chunks = 0
            released_bytes = 0
            retained = deque()
            for chunk in self._chunks:
                if (chunk.entry_index == entry_index
                        and chunk.end_offset <= verified_offset):
                    released_chunks += 1
                    released_bytes += chunk.size
                else:
                    retained.append(chunk)
            if receiver_state == "completed" and retained:
                raise FlowControlError(
                    "completed ACK does not cover all in-flight payload")
            self._chunks = retained
            self._acked_offsets[entry_index] = verified_offset
            self._durable_offsets[entry_index] = durable_offset
            self._coordinator._inflight_bytes -= released_bytes
            self._receiver_state = receiver_state
            discarded_bytes = 0
            if receiver_state in ("paused", "completed", "cancelled", "failed"):
                self._coordinator._windows.pop(self.transfer_id, None)
                discarded_bytes = self._close_locked(
                    f"receiver entered {receiver_state}")
                self._coordinator._inflight_bytes -= discarded_bytes
                count = self._coordinator._peer_counts[self.peer_identity] - 1
                if count:
                    self._coordinator._peer_counts[self.peer_identity] = count
                else:
                    self._coordinator._peer_counts.pop(self.peer_identity, None)
            condition.notify_all()
            return {"released_chunks": released_chunks,
                    "released_bytes": released_bytes,
                    "discarded_bytes": discarded_bytes,
                    "receiver_state": receiver_state}

    def check_ack_timeout(self, now=None):
        now = time.monotonic() if now is None else float(now)
        with self._coordinator._condition:
            self._raise_if_closed()
            if (self._chunks and now - self._chunks[0].sent_at
                    >= self._coordinator.limits.window_ack_timeout_seconds):
                raise FlowControlTimeout("oldest unacknowledged chunk timed out")
            return False

    def wait_until_empty(self, timeout=None):
        deadline = _deadline(timeout)
        condition = self._coordinator._condition
        with condition:
            if self._chunks:
                ack_deadline = (self._chunks[0].sent_at
                                + self._coordinator.limits.window_ack_timeout_seconds)
                deadline = ack_deadline if deadline is None else min(deadline, ack_deadline)
            while self._chunks:
                self._raise_if_closed()
                remaining = _remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise FlowControlTimeout("send window did not drain")
                condition.wait(remaining)
                self._raise_if_closed()
                if self._chunks:
                    ack_deadline = (self._chunks[0].sent_at
                                    + self._coordinator.limits.window_ack_timeout_seconds)
                    deadline = min(deadline, ack_deadline) if deadline is not None else ack_deadline
            self._raise_if_closed()
            return True

    def abort(self, error=None):
        self._coordinator.close_transfer(self.transfer_id, error=error)

    def snapshot(self):
        with self._coordinator._condition:
            return {
                "transfer_id": self.transfer_id,
                "peer_identity": self.peer_identity,
                "closed": self._closed,
                "error": self._error,
                "receiver_state": self._receiver_state,
                "inflight_chunks": len(self._chunks),
                "inflight_bytes": sum(chunk.size for chunk in self._chunks),
                "acked_offsets": dict(self._acked_offsets),
                "durable_offsets": dict(self._durable_offsets),
            }

    def _raise_if_closed(self):
        if self._closed:
            raise FlowControlClosed(self._error or "send window is closed")

    def _close_locked(self, error=None):
        if self._closed:
            return 0
        released = sum(chunk.size for chunk in self._chunks)
        self._chunks.clear()
        self._closed = True
        self._error = str(error)[:4096] if error is not None else None
        return released


class FlowControlCoordinator:
    """Global and stable-peer transfer admission plus shared byte accounting."""

    def __init__(self, limits=None):
        self.limits = limits or FlowControlLimits()
        if not isinstance(self.limits, FlowControlLimits):
            raise ValueError("limits must be FlowControlLimits")
        self._condition = threading.Condition()
        self._windows = {}
        self._peer_counts = {}
        self._inflight_bytes = 0
        self._closed = False
        self._queues = set()

    def open_transfer(self, transfer_id, peer_identity, timeout=0):
        transfer_id = normalize_transfer_id(transfer_id)
        if not isinstance(peer_identity, str) or not peer_identity or len(peer_identity) > 256:
            raise FlowControlError("invalid stable peer identity")
        deadline = _deadline(timeout)
        with self._condition:
            if transfer_id in self._windows:
                raise FlowControlError("duplicate active transfer_id")
            while (len(self._windows) >= self.limits.max_active_transfers_global
                   or self._peer_counts.get(peer_identity, 0)
                   >= self.limits.max_active_transfers_per_peer):
                if self._closed:
                    raise FlowControlClosed("flow-control coordinator is closed")
                remaining = _remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise FlowControlTimeout("transfer admission limit reached")
                self._condition.wait(remaining)
                if transfer_id in self._windows:
                    raise FlowControlError("duplicate active transfer_id")
            if self._closed:
                raise FlowControlClosed("flow-control coordinator is closed")
            window = SendWindow(self, transfer_id, peer_identity)
            self._windows[transfer_id] = window
            self._peer_counts[peer_identity] = self._peer_counts.get(peer_identity, 0) + 1
            return window

    def close_transfer(self, transfer_id, error=None):
        transfer_id = normalize_transfer_id(transfer_id)
        with self._condition:
            window = self._windows.pop(transfer_id, None)
            if window is None:
                return False
            released = window._close_locked(error)
            self._inflight_bytes -= released
            count = self._peer_counts[window.peer_identity] - 1
            if count:
                self._peer_counts[window.peer_identity] = count
            else:
                self._peer_counts.pop(window.peer_identity, None)
            self._condition.notify_all()
            return True

    def shutdown(self):
        with self._condition:
            self._closed = True
            for window in self._windows.values():
                window._close_locked("flow-control coordinator shut down")
            self._windows.clear()
            self._peer_counts.clear()
            self._inflight_bytes = 0
            queues = tuple(self._queues)
            self._queues.clear()
            self._condition.notify_all()
        for chunk_queue in queues:
            chunk_queue.close()

    def snapshot(self):
        with self._condition:
            return {
                "closed": self._closed,
                "active_transfers": len(self._windows),
                "active_per_peer": dict(self._peer_counts),
                "inflight_bytes": self._inflight_bytes,
            }

    def create_send_queue(self, *, size_fn=len, freeze_fn=None):
        chunk_queue = BoundedChunkQueue(
            self.limits.max_send_queue_chunks,
            self.limits.max_send_queue_bytes,
            size_fn=size_fn, freeze_fn=freeze_fn)
        return self._register_queue(chunk_queue)

    def create_receive_queue(self, *, size_fn=len, freeze_fn=None):
        chunk_queue = BoundedChunkQueue(
            self.limits.max_receive_queue_chunks,
            self.limits.max_receive_queue_bytes,
            size_fn=size_fn, freeze_fn=freeze_fn)
        return self._register_queue(chunk_queue)

    def _register_queue(self, chunk_queue):
        with self._condition:
            if self._closed:
                chunk_queue.close()
                raise FlowControlClosed("flow-control coordinator is closed")
            self._queues.add(chunk_queue)
            chunk_queue._on_close = self._forget_queue
        return chunk_queue

    def _forget_queue(self, chunk_queue):
        with self._condition:
            self._queues.discard(chunk_queue)


@dataclass
class _AckEntry:
    verified_offset: int
    durable_offset: int
    pending_bytes: int = 0
    pending_chunks: int = 0
    pending_since: float | None = None
    last_emitted_offset: int | None = None
    file_complete: bool = False


class CumulativeAckBatcher:
    """Emit cumulative ACKs after byte, chunk, time, or file boundaries."""

    def __init__(self, transfer_id, limits=None):
        self.transfer_id = normalize_transfer_id(transfer_id)
        self.limits = limits or FlowControlLimits()
        self._entries = {}
        self._lock = threading.Lock()

    def restore_entry(self, entry_index, *, verified_offset, durable_offset):
        entry_index = _uint(entry_index, "entry_index", MAX_ENTRY_INDEX)
        verified_offset = _uint(
            verified_offset, "verified_offset", MAX_LOGICAL_OFFSET)
        durable_offset = _uint(
            durable_offset, "durable_offset", MAX_LOGICAL_OFFSET)
        if durable_offset > verified_offset:
            raise FlowControlError("durable offset exceeds verified progress")
        with self._lock:
            if entry_index in self._entries:
                raise FlowControlError("ACK entry already has progress")
            self._entries[entry_index] = _AckEntry(
                verified_offset, durable_offset)

    def record_verified(self, entry_index, offset, length, *, durable_offset=None,
                         file_complete=False, receiver_state="transferring", now=None):
        entry_index = _uint(entry_index, "entry_index", MAX_ENTRY_INDEX)
        offset = _uint(offset, "offset", MAX_LOGICAL_OFFSET)
        length = _uint(length, "length", MAX_BINARY_PAYLOAD_BYTES)
        if durable_offset is not None:
            durable_offset = _uint(
                durable_offset, "durable_offset", MAX_LOGICAL_OFFSET)
        if length == 0 and not file_complete:
            raise FlowControlError("zero-length progress requires file completion")
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            entry = self._entries.get(entry_index)
            if entry is None:
                entry = _AckEntry(offset, 0)
                self._entries[entry_index] = entry
            if entry.file_complete or offset != entry.verified_offset:
                raise FlowControlError("verified payload is not contiguous")
            new_offset = offset + length
            if new_offset > MAX_LOGICAL_OFFSET:
                raise FlowControlError("verified progress exceeds the V2 offset limit")
            if durable_offset is None:
                durable_offset = entry.durable_offset
            if durable_offset < entry.durable_offset or durable_offset > new_offset:
                raise FlowControlError("durable offset is outside verified progress")
            entry.verified_offset = new_offset
            entry.durable_offset = durable_offset
            entry.pending_bytes += length
            entry.pending_chunks += 1 if length else 0
            entry.pending_since = now if entry.pending_since is None else entry.pending_since
            entry.file_complete = bool(file_complete)
            if (file_complete or entry.pending_bytes >= self.limits.ack_bytes
                    or entry.pending_chunks >= self.limits.ack_chunks):
                return self._emit(entry_index, entry, receiver_state)
            return None

    def record_durable(self, entry_index, durable_offset, *,
                       receiver_state="transferring"):
        entry_index = _uint(entry_index, "entry_index", MAX_ENTRY_INDEX)
        durable_offset = _uint(
            durable_offset, "durable_offset", MAX_LOGICAL_OFFSET)
        with self._lock:
            entry = self._entries.get(entry_index)
            if entry is None:
                raise FlowControlError("durable progress references an unknown entry")
            if (durable_offset < entry.durable_offset
                    or durable_offset > entry.verified_offset):
                raise FlowControlError("durable offset is outside verified progress")
            entry.durable_offset = durable_offset
            return self._emit(entry_index, entry, receiver_state)

    def poll(self, *, receiver_state="transferring", now=None):
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            return [self._emit(index, entry, receiver_state)
                    for index, entry in self._entries.items()
                    if entry.pending_since is not None
                    and now - entry.pending_since >= self.limits.ack_interval_seconds]

    def flush(self, entry_index, *, receiver_state="transferring"):
        entry_index = _uint(entry_index, "entry_index", MAX_ENTRY_INDEX)
        with self._lock:
            entry = self._entries.get(entry_index)
            if entry is None or entry.pending_since is None:
                return None
            return self._emit(entry_index, entry, receiver_state)

    def _emit(self, entry_index, entry, receiver_state):
        ack = protocol.build_stream_v2_ack(
            self.transfer_id, entry_index, entry.verified_offset,
            durable_offset=entry.durable_offset, receiver_state=receiver_state)
        entry.pending_bytes = 0
        entry.pending_chunks = 0
        entry.pending_since = None
        entry.last_emitted_offset = entry.verified_offset
        return ack
