"""FlowShift clipboard transfer helpers.

Pure transfer-job state, disk-space checks and a small background queue for
clipboard blob sends. Standard library only.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import math
import os
from pathlib import Path
import queue
import shutil
import threading
import time
import uuid

import clipboard_model as cm
import clipboard_sources as csrc


STREAM_V2_STRATEGY = "stream_v2"
LEGACY_ZIP_V1_STRATEGY = "legacy_zip_v1"
TRANSFER_STRATEGIES = (STREAM_V2_STRATEGY, LEGACY_ZIP_V1_STRATEGY)
TRANSFER_DIRECTIONS = ("send", "receive", "outgoing", "incoming")
MAX_UINT64 = (1 << 64) - 1
SESSION_CHECKPOINT_BYTES = 8 * 1024 * 1024
SESSION_CHECKPOINT_SECONDS = 0.250


class TransferSessionState:
    created = "created"
    preflight = "preflight"
    accepted = "accepted"
    sending_manifest = "sending_manifest"
    transferring = "transferring"
    paused = "paused"
    waiting_reconnect = "waiting_reconnect"
    verifying = "verifying"
    finalizing = "finalizing"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"


TRANSFER_SESSION_STATES = (
    TransferSessionState.created,
    TransferSessionState.preflight,
    TransferSessionState.accepted,
    TransferSessionState.sending_manifest,
    TransferSessionState.transferring,
    TransferSessionState.paused,
    TransferSessionState.waiting_reconnect,
    TransferSessionState.verifying,
    TransferSessionState.finalizing,
    TransferSessionState.completed,
    TransferSessionState.cancelled,
    TransferSessionState.failed,
)
TERMINAL_TRANSFER_SESSION_STATES = frozenset((
    TransferSessionState.completed,
    TransferSessionState.cancelled,
    TransferSessionState.failed,
))
_ACTIVE_TRANSFER_SESSION_STATES = frozenset((
    TransferSessionState.preflight,
    TransferSessionState.accepted,
    TransferSessionState.sending_manifest,
    TransferSessionState.transferring,
    TransferSessionState.verifying,
    TransferSessionState.finalizing,
))
_SESSION_TRANSITIONS = {
    TransferSessionState.created: frozenset((TransferSessionState.preflight,)),
    TransferSessionState.preflight: frozenset((TransferSessionState.accepted,
                                               TransferSessionState.paused,
                                               TransferSessionState.waiting_reconnect)),
    TransferSessionState.accepted: frozenset((TransferSessionState.sending_manifest,
                                              TransferSessionState.paused,
                                              TransferSessionState.waiting_reconnect)),
    TransferSessionState.sending_manifest: frozenset((TransferSessionState.transferring,
                                                      TransferSessionState.paused,
                                                      TransferSessionState.waiting_reconnect)),
    TransferSessionState.transferring: frozenset((TransferSessionState.paused,
                                                  TransferSessionState.waiting_reconnect,
                                                  TransferSessionState.verifying)),
    TransferSessionState.paused: _ACTIVE_TRANSFER_SESSION_STATES | frozenset((
        TransferSessionState.waiting_reconnect,)),
    TransferSessionState.waiting_reconnect: _ACTIVE_TRANSFER_SESSION_STATES | frozenset((
        TransferSessionState.paused,)),
    TransferSessionState.verifying: frozenset((TransferSessionState.finalizing,
                                               TransferSessionState.paused,
                                               TransferSessionState.waiting_reconnect)),
    TransferSessionState.finalizing: frozenset((TransferSessionState.completed,
                                                TransferSessionState.paused,
                                                TransferSessionState.waiting_reconnect)),
}


def _session_string(value, field_name, maximum=256):
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f"invalid transfer session {field_name}")
    return value


def _session_uint(value, field_name):
    if (not isinstance(value, int) or isinstance(value, bool)
            or value < 0 or value > MAX_UINT64):
        raise ValueError(f"invalid transfer session {field_name}")
    return value


def _session_timestamp(value, field_name):
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value < 0):
        raise ValueError(f"invalid transfer session {field_name}")
    return float(value)


def _session_record(value, field_name, depth=0):
    """Validate and copy bounded JSON-like session detail records."""
    if depth > 8:
        raise ValueError(f"invalid transfer session {field_name}")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value < 0 or value > MAX_UINT64:
            raise ValueError(f"invalid transfer session {field_name}")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"invalid transfer session {field_name}")
        return value
    if isinstance(value, str):
        if len(value) > 4096 or "\x00" in value:
            raise ValueError(f"invalid transfer session {field_name}")
        return value
    if isinstance(value, list):
        if len(value) > 1024:
            raise ValueError(f"invalid transfer session {field_name}")
        return [_session_record(item, field_name, depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 128:
            raise ValueError(f"invalid transfer session {field_name}")
        result = {}
        for key, item in value.items():
            if (not isinstance(key, str) or not key or len(key) > 128
                    or any(ord(char) < 32 for char in key)):
                raise ValueError(f"invalid transfer session {field_name}")
            result[key] = _session_record(item, field_name, depth + 1)
        return result
    raise ValueError(f"invalid transfer session {field_name}")


def _session_mapping(value, field_name):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"invalid transfer session {field_name}")
    return _session_record(value, field_name)


def _session_error(value):
    if value is None:
        return None
    error = _session_mapping(value, "error")
    _session_string(error.get("code"), "error code", maximum=128)
    if "message" in error and error["message"] is not None:
        _session_string(error["message"], "error message", maximum=4096)
    if "retryable" in error and not isinstance(error["retryable"], bool):
        raise ValueError("invalid transfer session error retryable")
    return error


class TransferSession:
    """Thread-safe, validated state foundation for one clipboard transfer."""

    def __init__(self, *, transfer_id=None, direction, item_id, item_revision,
                 profile, peer_identity, provider, strategy, manifest_digest,
                 logical_bytes, remaining_bytes=None, file_count, created_at=None,
                 updated_at=None, state=TransferSessionState.created, retry_count=0,
                 progress=None, resume_state=None, preflight_state=None, error=None):
        if transfer_id is None:
            transfer_id = uuid.uuid4().hex
        if not cm.is_valid_item_id(transfer_id):
            raise ValueError("invalid transfer session transfer_id")
        if direction not in TRANSFER_DIRECTIONS:
            raise ValueError("invalid transfer session direction")
        if not cm.is_valid_item_id(item_id):
            raise ValueError("invalid transfer session item_id")
        item_revision = _session_uint(item_revision, "item_revision")
        profile = _session_string(profile, "profile")
        peer_identity = _session_string(peer_identity, "peer_identity")
        provider = _session_string(provider, "provider")
        if strategy not in TRANSFER_STRATEGIES:
            raise ValueError("invalid transfer session strategy")
        if not cm.is_valid_sha256(manifest_digest):
            raise ValueError("invalid transfer session manifest_digest")
        logical_bytes = _session_uint(logical_bytes, "logical_bytes")
        if remaining_bytes is None:
            remaining_bytes = logical_bytes
        remaining_bytes = _session_uint(remaining_bytes, "remaining_bytes")
        if remaining_bytes > logical_bytes:
            raise ValueError("invalid transfer session remaining_bytes")
        file_count = _session_uint(file_count, "file_count")
        retry_count = _session_uint(retry_count, "retry_count")
        if state not in TRANSFER_SESSION_STATES:
            raise ValueError("invalid transfer session state")
        if state == TransferSessionState.completed and remaining_bytes != 0:
            raise ValueError("completed transfer session has remaining bytes")
        now = time.time() if created_at is None else _session_timestamp(created_at, "created_at")
        updated = now if updated_at is None else _session_timestamp(updated_at, "updated_at")
        if updated < now:
            raise ValueError("transfer session updated_at precedes created_at")

        self._lock = threading.RLock()
        self._transfer_id = transfer_id
        self._direction = direction
        self._item_id = item_id
        self._item_revision = item_revision
        self._profile = profile
        self._peer_identity = peer_identity
        self._provider = provider
        self._strategy = strategy
        self._manifest_digest = manifest_digest.lower()
        self._logical_bytes = logical_bytes
        self._remaining_bytes = remaining_bytes
        self._file_count = file_count
        self._created_at = now
        self._updated_at = updated
        self._state = state
        self._retry_count = retry_count
        self._progress = _session_mapping(progress, "progress")
        self._resume_state = _session_mapping(resume_state, "resume_state")
        self._preflight_state = _session_mapping(preflight_state, "preflight_state")
        self._error = _session_error(error)

    @property
    def transfer_id(self):
        return self._transfer_id

    @property
    def direction(self):
        return self._direction

    @property
    def item_id(self):
        return self._item_id

    @property
    def item_revision(self):
        return self._item_revision

    @property
    def profile(self):
        return self._profile

    @property
    def peer_identity(self):
        return self._peer_identity

    @property
    def provider(self):
        return self._provider

    @property
    def strategy(self):
        return self._strategy

    @property
    def manifest_digest(self):
        return self._manifest_digest

    @property
    def logical_bytes(self):
        return self._logical_bytes

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def remaining_bytes(self):
        with self._lock:
            return self._remaining_bytes

    @property
    def retry_count(self):
        with self._lock:
            return self._retry_count

    @property
    def file_count(self):
        return self._file_count

    @property
    def created_at(self):
        return self._created_at

    @property
    def updated_at(self):
        with self._lock:
            return self._updated_at

    @property
    def progress(self):
        with self._lock:
            return self._progress_snapshot()

    @property
    def resume_state(self):
        with self._lock:
            return copy.deepcopy(self._resume_state)

    @property
    def preflight_state(self):
        with self._lock:
            return copy.deepcopy(self._preflight_state)

    @property
    def error(self):
        with self._lock:
            return copy.deepcopy(self._error)

    def _update_time(self, timestamp):
        if timestamp is None:
            return max(time.time(), self._updated_at)
        now = _session_timestamp(timestamp, "updated_at")
        if now < self._updated_at:
            raise ValueError("transfer session timestamp moved backwards")
        return now

    def _assert_mutable(self):
        if self._state in TERMINAL_TRANSFER_SESSION_STATES:
            raise ValueError(f"terminal transfer session cannot change: {self._state}")

    def _progress_snapshot(self):
        progress = copy.deepcopy(self._progress)
        transferred = self._logical_bytes - self._remaining_bytes
        progress.update({
            "transferred_bytes": transferred,
            "total_bytes": self._logical_bytes,
            "percent": (100.0 if self._logical_bytes == 0
                        else transferred * 100.0 / self._logical_bytes),
        })
        return progress

    def snapshot(self):
        with self._lock:
            return {
                "transfer_id": self._transfer_id,
                "direction": self._direction,
                "item_id": self._item_id,
                "item_revision": self._item_revision,
                "profile": self._profile,
                "peer_identity": self._peer_identity,
                "provider": self._provider,
                "strategy": self._strategy,
                "manifest_digest": self._manifest_digest,
                "logical_bytes": self._logical_bytes,
                "remaining_bytes": self._remaining_bytes,
                "file_count": self._file_count,
                "created_at": self._created_at,
                "updated_at": self._updated_at,
                "state": self._state,
                "retry_count": self._retry_count,
                "progress": self._progress_snapshot(),
                "resume_state": copy.deepcopy(self._resume_state),
                "preflight_state": copy.deepcopy(self._preflight_state),
                "error": copy.deepcopy(self._error),
            }

    @classmethod
    def from_snapshot(cls, snapshot):
        if not isinstance(snapshot, dict):
            raise ValueError("invalid transfer session snapshot")
        return cls(**copy.deepcopy(snapshot))

    def transition(self, new_state, *, error=None, timestamp=None):
        if new_state not in TRANSFER_SESSION_STATES:
            raise ValueError("invalid transfer session state")
        with self._lock:
            self._assert_mutable()
            allowed = set(_SESSION_TRANSITIONS.get(self._state, ()))
            allowed.update((TransferSessionState.cancelled, TransferSessionState.failed))
            if new_state not in allowed:
                raise ValueError(f"invalid transfer session transition: {self._state} -> {new_state}")
            if new_state == TransferSessionState.completed and self._remaining_bytes != 0:
                raise ValueError("completed transfer session has remaining bytes")
            validated_error = self._error if error is None else _session_error(error)
            if new_state == TransferSessionState.completed and error is None:
                validated_error = None
            updated_at = self._update_time(timestamp)
            self._state = new_state
            self._error = validated_error
            self._updated_at = updated_at
            return self.snapshot()

    def update_progress(self, *, remaining_bytes=None, progress=None, resume_state=None,
                        preflight_state=None, timestamp=None):
        with self._lock:
            self._assert_mutable()
            new_remaining = self._remaining_bytes
            if remaining_bytes is not None:
                new_remaining = _session_uint(remaining_bytes, "remaining_bytes")
                if new_remaining > self._remaining_bytes:
                    raise ValueError("transfer session remaining_bytes increased")
            new_progress = (self._progress if progress is None
                            else _session_mapping(progress, "progress"))
            new_resume = (self._resume_state if resume_state is None
                          else _session_mapping(resume_state, "resume_state"))
            new_preflight = (self._preflight_state if preflight_state is None
                             else _session_mapping(preflight_state, "preflight_state"))
            updated_at = self._update_time(timestamp)
            self._remaining_bytes = new_remaining
            self._progress = copy.deepcopy(new_progress)
            self._resume_state = copy.deepcopy(new_resume)
            self._preflight_state = copy.deepcopy(new_preflight)
            self._updated_at = updated_at
            return self.snapshot()

    def advance_progress(self, byte_count, *, progress=None, resume_state=None,
                         preflight_state=None, timestamp=None):
        byte_count = _session_uint(byte_count, "byte_count")
        with self._lock:
            self._assert_mutable()
            if byte_count > self._remaining_bytes:
                raise ValueError("transfer session progress exceeds remaining bytes")
            return self.update_progress(
                remaining_bytes=self._remaining_bytes - byte_count,
                progress=progress,
                resume_state=resume_state,
                preflight_state=preflight_state,
                timestamp=timestamp,
            )

    def increment_retry(self, *, error=None, timestamp=None):
        with self._lock:
            self._assert_mutable()
            if self._retry_count == MAX_UINT64:
                raise ValueError("transfer session retry_count overflow")
            validated_error = self._error if error is None else _session_error(error)
            updated_at = self._update_time(timestamp)
            self._retry_count += 1
            self._error = validated_error
            self._updated_at = updated_at
            return self.snapshot()

    def cancel(self, *, error=None, timestamp=None):
        if error is None:
            error = {"code": "cancelled", "message": "transfer cancelled",
                     "retryable": False}
        return self.transition(TransferSessionState.cancelled, error=error,
                               timestamp=timestamp)

    def fail(self, error, *, timestamp=None):
        return self.transition(TransferSessionState.failed, error=error,
                               timestamp=timestamp)


class TransferStatus:
    pending = "pending"
    running = "running"
    paused = "paused"
    waiting_manual = "waiting_manual"
    retrying = "retrying"
    awaiting_ack = "awaiting_ack"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


@dataclass
class TransferJob:
    transfer_id: str
    profile_id: str
    item_id: str
    direction: str
    kind: str
    display_name: str
    total_bytes: int
    received_bytes: int = 0
    sent_bytes: int = 0
    chunk_count: int = 0
    completed_chunk_count: int = 0
    missing_chunk_count: int = 0
    next_chunk_index: int = 0
    retry_count: int = 0
    max_retries: int = 5
    status: str = TransferStatus.pending
    error: str | None = None
    started_at: float | None = None
    updated_at: float | None = None
    bytes_per_second: float = 0.0
    eta_seconds: float | None = None
    manual_required: bool = False
    final_ack_requested: bool = False
    session: TransferSession | None = None
    session_persist: object | None = field(default=None, repr=False, compare=False)
    session_log: object | None = field(default=None, repr=False, compare=False)
    persistence_error: str | None = None
    persistence_failures: int = 0
    session_last_persisted_bytes: int = 0
    session_last_persisted_at: float = field(default_factory=time.monotonic)

    def to_progress(self):
        total = max(0, int(self.total_bytes))
        received = max(0, int(self.received_bytes))
        sent = max(0, int(self.sent_bytes))
        done = max(received, sent)
        pct = 100.0 if self.status == TransferStatus.completed else (
            0.0 if total <= 0 else max(0.0, min(100.0, done * 100.0 / total)))
        return {
            "transfer_id": self.transfer_id,
            "item_id": self.item_id,
            "strategy": (self.session.strategy if self.session else LEGACY_ZIP_V1_STRATEGY),
            "session_state": (self.session.state if self.session else None),
            "status": self.status,
            "received_bytes": received,
            "total_bytes": total,
            "percent": pct,
            "bytes_per_second": float(self.bytes_per_second or 0.0),
            "eta_seconds": self.eta_seconds,
            "retry_count": int(self.retry_count),
            "error": self.error,
        }


def _now():
    return time.monotonic()


def make_transfer_job(transfer_id, profile_id, item_id, direction, kind, display_name,
                      total_bytes, chunk_count=0, max_retries=5, manual_required=False,
                      status=None, final_ack_requested=False):
    total_bytes = max(0, int(total_bytes))
    manual_required = bool(manual_required)
    if status is None:
        status = TransferStatus.waiting_manual if manual_required else TransferStatus.pending
    now = _now()
    return TransferJob(
        transfer_id=str(transfer_id or uuid.uuid4().hex),
        profile_id=str(profile_id or ""),
        item_id=str(item_id or ""),
        direction=str(direction or ""),
        kind=str(kind or ""),
        display_name=str(display_name or ""),
        total_bytes=total_bytes,
        chunk_count=max(0, int(chunk_count)),
        max_retries=max(0, int(max_retries)),
        manual_required=manual_required,
        final_ack_requested=bool(final_ack_requested),
        status=status,
        started_at=now if status in (TransferStatus.running, TransferStatus.retrying) else None,
        updated_at=now,
    )


def missing_chunk_indices(chunk_count, completed_chunks=None, missing_chunks=None):
    chunk_count = max(0, int(chunk_count))
    if missing_chunks is not None:
        return sorted({int(i) for i in missing_chunks if 0 <= int(i) < chunk_count})
    completed = {int(i) for i in (completed_chunks or []) if 0 <= int(i) < chunk_count}
    return [i for i in range(chunk_count) if i not in completed]


def compute_rate(job, now=None):
    if job is None:
        return 0.0
    now = _now() if now is None else float(now)
    start = job.started_at if job.started_at is not None else job.updated_at
    if start is None:
        return 0.0
    elapsed = max(1e-6, now - float(start))
    done = max(int(job.received_bytes), int(job.sent_bytes))
    return done / elapsed if done > 0 else 0.0


def compute_eta(job, now=None):
    rate = compute_rate(job, now=now)
    total = max(0, int(job.total_bytes))
    done = max(int(job.received_bytes), int(job.sent_bytes))
    if rate <= 0 or total <= done:
        return None
    return (total - done) / rate


def update_progress(job, received_bytes=None, sent_bytes=None, completed_chunks=None,
                    missing_chunks=None, completed_chunk_count=None,
                    missing_chunk_count=None, next_chunk_index=None, status=None,
                    error=None, now=None):
    now = _now() if now is None else float(now)
    if received_bytes is not None:
        job.received_bytes = max(0, int(received_bytes))
    if sent_bytes is not None:
        job.sent_bytes = max(0, int(sent_bytes))
    if completed_chunks is not None:
        completed = sorted({int(i) for i in completed_chunks})
        job.completed_chunk_count = len(completed)
        if next_chunk_index is None:
            next_chunk_index = next((index for index, value in enumerate(completed)
                                     if index != value), len(completed))
    if missing_chunks is not None:
        missing = sorted({int(i) for i in missing_chunks})
        job.missing_chunk_count = len(missing)
        if next_chunk_index is None and missing:
            next_chunk_index = missing[0]
    if completed_chunk_count is not None:
        job.completed_chunk_count = max(0, min(job.chunk_count, int(completed_chunk_count)))
    if missing_chunk_count is not None:
        job.missing_chunk_count = max(0, min(job.chunk_count, int(missing_chunk_count)))
    if next_chunk_index is not None:
        job.next_chunk_index = max(0, min(job.chunk_count, int(next_chunk_index)))
    if error is not None:
        job.error = str(error)
    if status is not None:
        job.status = status
    done = max(job.received_bytes, job.sent_bytes)
    if job.status in (TransferStatus.running, TransferStatus.retrying):
        if job.started_at is None:
            job.started_at = now
        job.bytes_per_second = compute_rate(job, now=now)
        job.eta_seconds = compute_eta(job, now=now)
    elif job.status == TransferStatus.completed:
        job.bytes_per_second = compute_rate(job, now=now)
        job.eta_seconds = 0.0
        job.received_bytes = max(job.received_bytes, job.total_bytes)
        job.sent_bytes = max(job.sent_bytes, job.total_bytes)
    else:
        job.bytes_per_second = 0.0
        job.eta_seconds = None
    job.updated_at = now
    _sync_job_session(job)
    return job


def mark_retry(job, error=None, now=None):
    job.retry_count += 1
    if error is not None:
        job.error = str(error)
    if job.retry_count > job.max_retries:
        job.status = TransferStatus.failed
    else:
        job.status = TransferStatus.retrying
        if job.started_at is None:
            job.started_at = _now() if now is None else float(now)
    session = getattr(job, "session", None)
    if session is not None and session.state not in TERMINAL_TRANSFER_SESSION_STATES:
        session.increment_retry(error=_job_session_error(job, retryable=True))
    return update_progress(job, now=now)


def mark_failed(job, error=None, now=None):
    if error is not None:
        job.error = str(error)
    job.status = TransferStatus.failed
    return update_progress(job, now=now)


def mark_completed(job, now=None):
    job.error = None
    job.status = TransferStatus.completed
    return update_progress(job, received_bytes=job.total_bytes, sent_bytes=job.total_bytes,
                          now=now)


def mark_cancelled(job, error=None, now=None):
    if error is not None:
        job.error = str(error)
    job.status = TransferStatus.cancelled
    return update_progress(job, now=now)


def _job_session_error(job, retryable=False):
    return {"code": "transfer_failed", "message": str(job.error or "transfer failed")[:4096],
            "retryable": bool(retryable)}


def _advance_session_state(session, target):
    if session.state in TERMINAL_TRANSFER_SESSION_STATES or session.state == target:
        return
    routes = {
        TransferSessionState.preflight: (TransferSessionState.preflight,),
        TransferSessionState.transferring: (
            TransferSessionState.preflight, TransferSessionState.accepted,
            TransferSessionState.sending_manifest, TransferSessionState.transferring),
        TransferSessionState.waiting_reconnect: (
            TransferSessionState.preflight, TransferSessionState.waiting_reconnect),
        TransferSessionState.finalizing: (
            TransferSessionState.preflight, TransferSessionState.accepted,
            TransferSessionState.sending_manifest, TransferSessionState.transferring,
            TransferSessionState.verifying, TransferSessionState.finalizing),
        TransferSessionState.completed: (
            TransferSessionState.preflight, TransferSessionState.accepted,
            TransferSessionState.sending_manifest, TransferSessionState.transferring,
            TransferSessionState.verifying, TransferSessionState.finalizing,
            TransferSessionState.completed),
    }
    for state in routes.get(target, (target,)):
        if session.state == state:
            continue
        allowed = _SESSION_TRANSITIONS.get(session.state, frozenset())
        if state in allowed:
            session.transition(state)


def _persist_job_session(job, force=False):
    callback = getattr(job, "session_persist", None)
    session = getattr(job, "session", None)
    if callback is not None and session is not None:
        now = time.monotonic()
        done = session.logical_bytes - session.remaining_bytes
        terminal = session.state in TERMINAL_TRANSFER_SESSION_STATES
        if (not force and not terminal
                and done - job.session_last_persisted_bytes < SESSION_CHECKPOINT_BYTES
                and now - job.session_last_persisted_at < SESSION_CHECKPOINT_SECONDS):
            return False
        try:
            callback(session.snapshot())
            job.session_last_persisted_bytes = done
            job.session_last_persisted_at = now
            return True
        except Exception as exc:
            job.persistence_failures = min(MAX_UINT64, job.persistence_failures + 1)
            job.persistence_error = str(exc)[:4096]
            log = getattr(job, "session_log", None)
            if log is not None and job.persistence_failures == 1:
                try:
                    log("WARN", f"clipboard transfer session persistence failed: {exc}")
                except Exception:
                    pass
    return False


def advance_job_session(job, target):
    session = getattr(job, "session", None)
    if session is not None and session.state not in TERMINAL_TRANSFER_SESSION_STATES:
        _advance_session_state(session, target)
        _persist_job_session(job, force=True)


def _sync_job_session(job):
    session = getattr(job, "session", None)
    if session is None or session.state in TERMINAL_TRANSFER_SESSION_STATES:
        return
    done = min(session.logical_bytes, max(int(job.received_bytes), int(job.sent_bytes)))
    remaining = session.logical_bytes - done
    if remaining <= session.remaining_bytes:
        session.update_progress(remaining_bytes=remaining, progress={
            "received_bytes": max(0, int(job.received_bytes)),
            "sent_bytes": max(0, int(job.sent_bytes)),
            "chunk_count": max(0, int(job.chunk_count)),
            "completed_chunk_count": job.completed_chunk_count,
            "missing_chunk_count": job.missing_chunk_count,
            "next_chunk_index": job.next_chunk_index,
        })
    if job.status == TransferStatus.running:
        if session.state == TransferSessionState.created:
            _advance_session_state(session, TransferSessionState.preflight)
    elif job.status == TransferStatus.retrying:
        _advance_session_state(session, TransferSessionState.waiting_reconnect)
    elif job.status == TransferStatus.completed:
        session.update_progress(remaining_bytes=0)
        _advance_session_state(session, TransferSessionState.completed)
    elif job.status == TransferStatus.cancelled:
        session.cancel(error={"code": "cancelled", "message": str(job.error or "cancelled")[:4096],
                              "retryable": False})
    elif job.status == TransferStatus.failed:
        session.fail(_job_session_error(job))
    _persist_job_session(job, force=job.status in (
        TransferStatus.completed, TransferStatus.cancelled, TransferStatus.failed))


def should_retry(job):
    return job.retry_count < job.max_retries and job.status in (
        TransferStatus.failed, TransferStatus.retrying, TransferStatus.paused)


def should_require_manual_start(job, auto_limit_bytes):
    return int(job.total_bytes) > int(auto_limit_bytes)


def check_disk_space(path, required_bytes, safety_margin_bytes=None):
    required_bytes = max(0, int(required_bytes))
    margin = int(safety_margin_bytes) if safety_margin_bytes is not None else max(
        512 * 1024 * 1024, required_bytes // 10)
    probe = Path(path or ".")
    try:
        probe = probe.resolve()
    except Exception:
        pass
    while True:
        try:
            if probe.exists():
                usage = shutil.disk_usage(str(probe))
                free = int(usage.free)
                effective = required_bytes + margin
                return {
                    "ok": free >= effective,
                    "path": str(probe),
                    "free_bytes": free,
                    "required_bytes": required_bytes,
                    "margin_bytes": margin,
                    "missing_bytes": max(0, effective - free),
                }
        except Exception:
            break
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    return {
        "ok": False,
        "path": str(probe),
        "free_bytes": 0,
        "required_bytes": required_bytes,
        "margin_bytes": margin,
        "missing_bytes": required_bytes + margin,
    }


def progress_from_item(item, job=None, auto_limit_bytes=100 * 1024 * 1024):
    if job is not None:
        return job.to_progress()
    total = max(0, int(item.get("size", 0) or 0))
    available = bool(item.get("available"))
    md = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    status = md.get("transfer_status")
    error = md.get("transfer_error")
    if not status:
        if available:
            status = TransferStatus.completed
        elif total > int(auto_limit_bytes):
            status = TransferStatus.waiting_manual
        else:
            status = TransferStatus.pending
    received = total if status == TransferStatus.completed else 0
    return {
        "item_id": item.get("item_id"),
        "status": status,
        "received_bytes": received,
        "total_bytes": total,
        "percent": 100.0 if status == TransferStatus.completed else 0.0,
        "bytes_per_second": 0.0,
        "eta_seconds": 0.0 if status == TransferStatus.completed else None,
        "retry_count": int(md.get("retry_count", 0) or 0),
        "error": error,
    }


class TransferQueue:
    def __init__(self, max_parallel=1, maxsize=32, retry_delay_ms=500, log_fn=None):
        self.max_parallel = max(1, int(max_parallel))
        self.retry_delay_ms = max(0, int(retry_delay_ms))
        self.log = log_fn or (lambda level, msg: None)
        self._queue = queue.Queue(maxsize=max(1, int(maxsize)))
        self._jobs = {}
        self._tasks = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._accepting = True
        self._queued = set()
        self._active = set()
        self._retry_pending = set()
        self._retry_timers = {}
        self._workers = []
        for i in range(self.max_parallel):
            t = threading.Thread(target=self._worker, name=f"clipboard-transfer-{i}", daemon=True)
            self._workers.append(t)
            t.start()

    def submit(self, job, func, block=False, timeout=None):
        if job is None or func is None:
            return False
        deadline = None
        if block and timeout is not None:
            deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            if not self._accepting or self._stop.is_set():
                return False
            self._jobs[job.transfer_id] = job
            self._tasks[job.transfer_id] = func
        while True:
            with self._lock:
                if not self._accepting or self._stop.is_set():
                    self._tasks.pop(job.transfer_id, None)
                    return False
                try:
                    self._queue.put_nowait((job.transfer_id, func))
                except queue.Full:
                    pass
                else:
                    self._queued.add(job.transfer_id)
                    return True
            if not block:
                with self._lock:
                    self._tasks.pop(job.transfer_id, None)
                return False
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                with self._lock:
                    self._tasks.pop(job.transfer_id, None)
                return False
            self._stop.wait(min(0.05, remaining) if remaining is not None else 0.05)

    def get_job(self, transfer_id):
        with self._lock:
            return self._jobs.get(transfer_id)

    def cancel(self, transfer_id, reason="cancelled"):
        with self._lock:
            job = self._jobs.get(transfer_id)
            if not job:
                return False
            mark_cancelled(job, reason)
            self._retry_pending.discard(transfer_id)
            timer = self._retry_timers.pop(transfer_id, None)
        if timer is not None:
            timer.cancel()
        return True

    def activity_snapshot(self):
        """Return a thread-safe, non-mutating view of work that blocks shutdown."""
        with self._lock:
            queued = set(self._queued)
            active = set(self._active)
            retry_pending = set(self._retry_pending)
            activity_ids = queued | active | retry_pending
            statuses = {}
            for transfer_id in activity_ids:
                job = self._jobs.get(transfer_id)
                status = getattr(job, "status", None)
                if status in (TransferStatus.pending, TransferStatus.running,
                              TransferStatus.retrying, TransferStatus.paused,
                              TransferStatus.awaiting_ack):
                    statuses[status] = statuses.get(status, 0) + 1
            workers_alive = sum(thread.is_alive() for thread in self._workers)
            stopped = self._stop.is_set()
            return {
                "accepting": self._accepting,
                "stopped": stopped,
                "queued": len(queued),
                "active": len(active),
                "retry_pending": len(retry_pending),
                "blocking_job_statuses": statuses,
                "blocking": bool(queued or active or retry_pending or statuses),
                "workers": len(self._workers),
                "workers_alive": workers_alive,
            }

    def shutdown(self, timeout=5.0, cancel_pending=True):
        """Stop admission, cancel queued work, wake workers and join them boundedly."""
        timeout = max(0.0, float(timeout))
        deadline = time.monotonic() + timeout
        with self._lock:
            self._accepting = False
            self._stop.set()
            timers = list(self._retry_timers.values())
            self._retry_timers.clear()
            retry_ids = set(self._retry_pending)
            self._retry_pending.clear()
            if cancel_pending:
                for transfer_id in self._queued | retry_ids:
                    job = self._jobs.get(transfer_id)
                    if job is not None and job.status not in (
                            TransferStatus.completed, TransferStatus.failed,
                            TransferStatus.cancelled):
                        mark_cancelled(job, "transfer queue shut down")
        for timer in timers:
            timer.cancel()

        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                if item is not None:
                    transfer_id, _func = item
                    with self._lock:
                        self._queued.discard(transfer_id)
                        if cancel_pending:
                            job = self._jobs.get(transfer_id)
                            if job is not None and job.status not in (
                                    TransferStatus.completed, TransferStatus.failed,
                                    TransferStatus.cancelled):
                                mark_cancelled(job, "transfer queue shut down")
            finally:
                self._queue.task_done()

        # One sentinel per live worker. Idle workers consume these immediately;
        # active workers consume theirs after the current transfer returns.
        for _thread in [thread for thread in self._workers if thread.is_alive()]:
            while True:
                remaining = deadline - time.monotonic()
                try:
                    if remaining <= 0:
                        self._queue.put_nowait(None)
                    else:
                        self._queue.put(None, timeout=min(0.05, remaining))
                    break
                except queue.Full:
                    if remaining <= 0:
                        break

        current = threading.current_thread()
        for thread in self._workers:
            if thread is current:
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in self._workers)

    def _schedule_retry(self, transfer_id, func):
        with self._lock:
            if (self._stop.is_set() or not self._accepting
                    or transfer_id in self._retry_pending):
                return False
            self._retry_pending.add(transfer_id)

        def _requeue():
            while True:
                with self._lock:
                    if self._stop.is_set() or not self._accepting:
                        self._retry_pending.discard(transfer_id)
                        self._retry_timers.pop(transfer_id, None)
                        return
                    try:
                        self._queue.put_nowait((transfer_id, func))
                    except queue.Full:
                        pass
                    else:
                        self._retry_pending.discard(transfer_id)
                        self._retry_timers.pop(transfer_id, None)
                        self._queued.add(transfer_id)
                        return
                if self._stop.wait(0.05):
                    with self._lock:
                        self._retry_pending.discard(transfer_id)
                        self._retry_timers.pop(transfer_id, None)
                    return

        timer = threading.Timer(self.retry_delay_ms / 1000.0, _requeue)
        timer.daemon = True
        with self._lock:
            if self._stop.is_set() or not self._accepting:
                self._retry_pending.discard(transfer_id)
                return False
            self._retry_timers[transfer_id] = timer
        timer.start()
        return True

    def _worker(self):
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            if item is None:
                self._queue.task_done()
                return
            transfer_id, func = item
            try:
                with self._lock:
                    self._queued.discard(transfer_id)
                    job = self._jobs.get(transfer_id)
                    if (job is not None and self._stop.is_set()
                            and job.status != TransferStatus.cancelled):
                        mark_cancelled(job, "transfer queue shut down")
                    should_run = bool(
                        job is not None and job.status != TransferStatus.cancelled)
                    if should_run:
                        self._active.add(transfer_id)
                if not should_run:
                    continue
                if job.status in (TransferStatus.pending, TransferStatus.paused, TransferStatus.retrying):
                    update_progress(job, status=TransferStatus.running)
                try:
                    func(job)
                    if job.status not in (TransferStatus.completed, TransferStatus.failed,
                                          TransferStatus.cancelled,
                                          TransferStatus.awaiting_ack):
                        mark_completed(job)
                except Exception as e:
                    if self._stop.is_set():
                        mark_cancelled(job, "transfer queue shut down")
                    else:
                        mark_retry(job, error=e)
                    if not self._stop.is_set() and should_retry(job):
                        self.log("WARN", f"clipboard transfer retry: {transfer_id} ({job.retry_count}/{job.max_retries})")
                        self._schedule_retry(transfer_id, func)
                    elif job.status != TransferStatus.cancelled:
                        mark_failed(job, error=e)
                        self.log("WARN", f"clipboard transfer failed: {transfer_id}: {e}")
            finally:
                with self._lock:
                    self._active.discard(transfer_id)
                try:
                    self._queue.task_done()
                except Exception:
                    pass


def _hash_file(path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class DiskChunkAssembler:
    """Disk-backed chunk assembler for large clipboard transfers."""

    def __init__(self, total_size, chunk_count, expected_sha, temp_path):
        self.total_size = max(0, int(total_size))
        self.chunk_count = max(0, int(chunk_count))
        if self.chunk_count > cm.MAX_LEGACY_CHUNK_COUNT:
            raise ValueError("invalid legacy transfer chunk geometry")
        self.expected_sha = expected_sha
        self.temp_path = os.path.abspath(temp_path)
        self._received = 0
        self._received_indices = set()
        self._fh = None
        os.makedirs(os.path.dirname(self.temp_path) or ".", exist_ok=True)
        self._fh = open(self.temp_path, "w+b")
        self._fh.truncate(self.total_size)
        csrc.mark_active(self.temp_path)

    @property
    def next_index(self):
        i = 0
        while i in self._received_indices:
            i += 1
        return i

    def missing_indices(self):
        return [i for i in range(self.chunk_count) if i not in self._received_indices]

    @property
    def completed_chunk_count(self):
        return len(self._received_indices)

    def add_chunk(self, index, offset, data, chunk_sha=None):
        if (not isinstance(index, int) or isinstance(index, bool)
                or index < 0 or index >= self.chunk_count or not isinstance(data, bytes)
                or not isinstance(offset, int) or isinstance(offset, bool) or offset < 0):
            return "invalid"
        if index in self._received_indices:
            return "duplicate"
        if chunk_sha is not None and cm.sha256_bytes(data) != chunk_sha:
            return "hash_mismatch"
        if offset + len(data) > self.total_size:
            return "invalid"
        if self._fh is None:
            self._fh = open(self.temp_path, "r+b")
        self._fh.seek(offset)
        self._fh.write(data)
        self._fh.flush()
        self._received_indices.add(index)
        self._received += len(data)
        return "ok"

    @property
    def bytes_received(self):
        return self._received

    def is_complete(self):
        return len(self._received_indices) == self.chunk_count

    def finalize(self):
        if not self.is_complete():
            raise ValueError(
                f"incomplete: {self.chunk_count - len(self._received_indices)} chunks missing")
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except OSError:
                pass
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        actual = _hash_file(self.temp_path)
        if self.expected_sha and actual != self.expected_sha:
            raise ValueError("final hash mismatch")
        if os.path.getsize(self.temp_path) != self.total_size:
            raise ValueError(f"size mismatch: {os.path.getsize(self.temp_path)} != {self.total_size}")
        return {"path": self.temp_path, "sha256": actual, "size": self.total_size}

    def cleanup(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        csrc.clear_active(self.temp_path)
        try:
            os.remove(self.temp_path)
        except OSError:
            pass
