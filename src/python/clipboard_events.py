"""Bounded clipboard event delivery and one-shot write suppression."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import os
import threading
import time


def bytes_digest(data):
    return hashlib.sha256(bytes(data)).hexdigest()


def text_digest(text):
    return bytes_digest(str(text).encode("utf-8"))


def file_list_digest(paths):
    normalized = sorted(os.path.normcase(os.path.abspath(str(path))) for path in paths)
    return text_digest("\n".join(normalized))


class BoundedClipboardEvents:
    """Small coalescing FIFO that keeps WindowProc work bounded."""

    def __init__(self, capacity=8):
        self.capacity = max(1, int(capacity))
        self._events = deque()
        self._condition = threading.Condition()
        self._closed = False
        self.submitted = 0
        self.coalesced = 0
        self.dropped = 0

    def submit(self, sequence, source="listener"):
        event = {"sequence": int(sequence or 0), "source": str(source),
                 "created_at": time.time()}
        with self._condition:
            if self._closed:
                return False
            self.submitted += 1
            if self._events:
                self.coalesced += len(self._events)
                self._events.clear()
            if len(self._events) >= self.capacity:
                self._events.popleft()
                self.dropped += 1
            self._events.append(event)
            self._condition.notify()
            return True

    def get(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while not self._events and not self._closed:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._events.popleft() if self._events else None

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            return {
                "capacity": self.capacity,
                "depth": len(self._events),
                "submitted": self.submitted,
                "coalesced": self.coalesced,
                "dropped": self.dropped,
                "closed": self._closed,
            }


@dataclass
class _SuppressionToken:
    token_id: int
    item_id: str
    formats: frozenset
    primary_format: str
    digest: str
    before_sequence: int
    created_at: float
    sequence: int | None = None
    committed: bool = False


class ClipboardWriteSuppressor:
    """Sequence-bound suppression that cannot hide a later equal user copy."""

    def __init__(self, ttl_seconds=2.0, max_tokens=32, clock=time.monotonic):
        self.ttl_seconds = max(0.1, float(ttl_seconds))
        self.max_tokens = max(1, int(max_tokens))
        self._clock = clock
        self._condition = threading.Condition()
        self._tokens = deque()
        self._next_id = 0
        self._consumed = 0
        self._expired = 0
        self._failed = 0

    def prepare(self, item_id, formats, primary_format, digest, before_sequence):
        normalized_formats = frozenset(str(value) for value in formats)
        if primary_format not in normalized_formats or not digest:
            raise ValueError("invalid clipboard suppression token")
        with self._condition:
            self._purge_locked()
            self._next_id += 1
            token = _SuppressionToken(
                token_id=self._next_id,
                item_id=str(item_id),
                formats=normalized_formats,
                primary_format=str(primary_format),
                digest=str(digest),
                before_sequence=int(before_sequence or 0),
                created_at=self._clock(),
            )
            self._tokens.append(token)
            while len(self._tokens) > self.max_tokens:
                self._tokens.popleft()
                self._expired += 1
            return token

    def finish(self, token, success, resulting_sequence):
        with self._condition:
            if token not in self._tokens:
                return False
            if not success:
                self._tokens.remove(token)
                self._failed += 1
                self._condition.notify_all()
                return False
            resulting_sequence = int(resulting_sequence or 0)
            if resulting_sequence == 0 or resulting_sequence == token.before_sequence:
                self._tokens.remove(token)
                self._failed += 1
                self._condition.notify_all()
                return False
            token.sequence = resulting_sequence
            token.committed = True
            self._condition.notify_all()
            return True

    def consume(self, sequence, observed_formats, primary_format, digest):
        sequence = int(sequence or 0)
        observed = frozenset(str(value) for value in observed_formats)
        deadline = time.monotonic() + 0.1
        with self._condition:
            while True:
                self._purge_locked()
                pending_match = False
                for token in list(self._tokens):
                    content_match = (token.primary_format == primary_format
                                     and token.digest == digest
                                     and token.formats.issubset(observed))
                    if token.committed and token.sequence == sequence and content_match:
                        self._tokens.remove(token)
                        self._consumed += 1
                        return True
                    if (not token.committed and sequence != token.before_sequence
                            and content_match):
                        pending_match = True
                remaining = deadline - time.monotonic()
                if not pending_match or remaining <= 0:
                    return False
                self._condition.wait(remaining)

    def _purge_locked(self):
        cutoff = self._clock() - self.ttl_seconds
        while self._tokens and self._tokens[0].created_at < cutoff:
            self._tokens.popleft()
            self._expired += 1

    def snapshot(self):
        with self._condition:
            self._purge_locked()
            committed = sum(1 for token in self._tokens if token.committed)
            return {
                "pending": len(self._tokens) - committed,
                "committed": committed,
                "consumed": self._consumed,
                "expired": self._expired,
                "failed": self._failed,
                "ttl_seconds": self.ttl_seconds,
            }
