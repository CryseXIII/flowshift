"""FlowShift clipboard transfer helpers.

Pure transfer-job state, disk-space checks and a small background queue for
clipboard blob sends. Standard library only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import queue
import shutil
import threading
import time
import uuid

import clipboard_model as cm
import clipboard_sources as csrc


class TransferStatus:
    pending = "pending"
    running = "running"
    paused = "paused"
    waiting_manual = "waiting_manual"
    retrying = "retrying"
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
    completed_chunks: list[int] = field(default_factory=list)
    missing_chunks: list[int] = field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 5
    status: str = TransferStatus.pending
    error: str | None = None
    started_at: float | None = None
    updated_at: float | None = None
    bytes_per_second: float = 0.0
    eta_seconds: float | None = None
    manual_required: bool = False

    def to_progress(self):
        total = max(0, int(self.total_bytes))
        received = max(0, int(self.received_bytes))
        sent = max(0, int(self.sent_bytes))
        done = max(received, sent)
        pct = 100.0 if self.status == TransferStatus.completed else (
            0.0 if total <= 0 else max(0.0, min(100.0, done * 100.0 / total)))
        return {
            "item_id": self.item_id,
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
                      status=None):
    total_bytes = max(0, int(total_bytes))
    manual_required = bool(manual_required)
    if status is None:
        status = TransferStatus.waiting_manual if manual_required else (
            TransferStatus.completed if total_bytes == 0 else TransferStatus.pending)
    if total_bytes == 0 and status != TransferStatus.failed:
        status = TransferStatus.completed
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
                    missing_chunks=None, status=None, error=None, now=None):
    now = _now() if now is None else float(now)
    if received_bytes is not None:
        job.received_bytes = max(0, int(received_bytes))
    if sent_bytes is not None:
        job.sent_bytes = max(0, int(sent_bytes))
    if completed_chunks is not None:
        job.completed_chunks = sorted({int(i) for i in completed_chunks})
    if missing_chunks is not None:
        job.missing_chunks = sorted({int(i) for i in missing_chunks})
    if error is not None:
        job.error = str(error)
    if status is not None:
        job.status = status
    done = max(job.received_bytes, job.sent_bytes)
    if job.total_bytes > 0 and done >= job.total_bytes and job.status not in (
        TransferStatus.failed, TransferStatus.cancelled):
        job.status = TransferStatus.completed
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
        self._workers = []
        for i in range(self.max_parallel):
            t = threading.Thread(target=self._worker, name=f"clipboard-transfer-{i}", daemon=True)
            self._workers.append(t)
            t.start()

    def submit(self, job, func, block=False, timeout=None):
        if job is None or func is None:
            return False
        with self._lock:
            self._jobs[job.transfer_id] = job
            self._tasks[job.transfer_id] = func
        try:
            self._queue.put((job.transfer_id, func), block=block, timeout=timeout)
            return True
        except queue.Full:
            return False

    def get_job(self, transfer_id):
        with self._lock:
            return self._jobs.get(transfer_id)

    def cancel(self, transfer_id, reason="cancelled"):
        with self._lock:
            job = self._jobs.get(transfer_id)
        if not job:
            return False
        mark_cancelled(job, reason)
        return True

    def shutdown(self):
        self._stop.set()

    def _schedule_retry(self, transfer_id, func):
        if self._stop.is_set():
            return
        delay = self.retry_delay_ms / 1000.0

        def _requeue():
            if self._stop.is_set():
                return
            try:
                self._queue.put_nowait((transfer_id, func))
            except queue.Full:
                self._schedule_retry(transfer_id, func)

        timer = threading.Timer(delay, _requeue)
        timer.daemon = True
        timer.start()

    def _worker(self):
        while not self._stop.is_set():
            try:
                transfer_id, func = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    job = self._jobs.get(transfer_id)
                if not job or job.status == TransferStatus.cancelled:
                    continue
                if job.status in (TransferStatus.pending, TransferStatus.paused, TransferStatus.retrying):
                    update_progress(job, status=TransferStatus.running)
                try:
                    func(job)
                    if job.status not in (TransferStatus.completed, TransferStatus.failed,
                                          TransferStatus.cancelled):
                        mark_completed(job)
                except Exception as e:
                    mark_retry(job, error=e)
                    if should_retry(job):
                        self.log("WARN", f"clipboard transfer retry: {transfer_id} ({job.retry_count}/{job.max_retries})")
                        self._schedule_retry(transfer_id, func)
                    else:
                        mark_failed(job, error=e)
                        self.log("WARN", f"clipboard transfer failed: {transfer_id}: {e}")
            finally:
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

    def add_chunk(self, index, offset, data, chunk_sha=None):
        index = int(index)
        if index in self._received_indices:
            return "duplicate"
        if chunk_sha is not None and cm.sha256_bytes(data) != chunk_sha:
            return "hash_mismatch"
        offset = max(0, int(offset))
        if offset + len(data) > self.total_size:
            return "hash_mismatch"
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

    def completed_indices(self):
        return sorted(self._received_indices)

    def finalize(self):
        if not self.is_complete():
            raise ValueError(f"incomplete: missing {self.missing_indices()}")
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
