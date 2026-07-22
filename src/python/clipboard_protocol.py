"""FlowShift clipboard protocol — sync + chunked transfer messages.

Layer 1 (wire) of the clipboard feature: builders/parsers for the manifest-based
sync and the chunked file/blob transfer, plus a pure ``ChunkAssembler`` that
reassembles chunks in order and verifies the final SHA-256. All message payloads
respect ``runtime_model.MAX_FRAME_SIZE`` (chunk size chosen so a base64 chunk +
JSON envelope stays under the frame limit). No sockets here — the runtime sends
these dicts over the existing framed peer link.
"""
from __future__ import annotations

import base64

import clipboard_model as cm
from runtime_model import MAX_FRAME_SIZE

# Transfer message types.
T_MANIFEST = "clipboard_manifest"
T_ANNOUNCEMENT = "clipboard_announcement"
T_ANNOUNCEMENT_ACK = "clipboard_announcement_ack"
T_REQUEST = "clipboard_request_items"
T_SYNC_RESULT = "clipboard_sync_result"
T_START = "clipboard_transfer_start"
T_CHUNK = "clipboard_transfer_chunk"
T_ACK = "clipboard_transfer_ack"
T_COMPLETE = "clipboard_transfer_complete"
T_ERROR = "clipboard_transfer_error"
T_RESUME = "clipboard_transfer_resume"
T_PREFLIGHT = "clipboard_transfer_preflight"
T_PREFLIGHT_RESPONSE = "clipboard_transfer_preflight_response"

# Error codes.
ERR_DISK_FULL = "disk_full"
ERR_HASH_MISMATCH = "hash_mismatch"
ERR_TOO_LARGE = "too_large"
ERR_NOT_FOUND = "not_found"
ERR_TIMEOUT = "timeout"
ERR_ABORTED = "aborted"


def safe_chunk_size():
    return cm.default_chunk_size(MAX_FRAME_SIZE)


def build_announcement(announcement_id, profile_id, device_id, revision,
                       current_item_id, item):
    return {
        "type": T_ANNOUNCEMENT,
        "schema_version": cm.ITEM_SCHEMA_VERSION,
        "announcement_id": announcement_id,
        "profile_id": profile_id,
        "device_id": device_id,
        "history_revision": int(revision),
        "current_item_id": current_item_id,
        "item": cm.manifest_item(item),
    }


def parse_announcement(msg):
    if not isinstance(msg, dict) or msg.get("type") != T_ANNOUNCEMENT:
        return None
    if msg.get("schema_version") != cm.ITEM_SCHEMA_VERSION:
        return None
    announcement_id = msg.get("announcement_id")
    profile_id = msg.get("profile_id")
    device_id = msg.get("device_id")
    revision = msg.get("history_revision")
    current = msg.get("current_item_id")
    if (not cm.is_valid_item_id(announcement_id)
            or not isinstance(profile_id, str) or not profile_id or len(profile_id) > 256
            or not isinstance(device_id, str) or not device_id or len(device_id) > 128
            or not isinstance(revision, int) or isinstance(revision, bool) or revision < 0
            or (current is not None and not cm.is_valid_item_id(current))):
        return None
    try:
        def contains_private(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key in ("data", "files", "abspath", "base", "cache_path",
                               "materialization_path", "source_path"):
                        return True
                    if contains_private(nested):
                        return True
            elif isinstance(value, list):
                return any(contains_private(nested) for nested in value)
            return False

        if contains_private(msg.get("item")):
            raise ValueError("announcement contains payload or local path fields")
        item = cm.version_item(msg.get("item"))
        if msg.get("item") != cm.manifest_item(item):
            raise ValueError("announcement item contains non-metadata fields")
        if not any(provider.get("device_id") == device_id
                   and provider.get("state") == "available"
                   for provider in item.get("providers", [])):
            raise ValueError("announcing device is not an available provider")
        payload = item.get("payload") or {}
        for provider in item.get("providers", []):
            if (provider.get("state") == "available" and payload.get("sha256") is not None
                    and (provider.get("payload_sha256") is None
                         or provider.get("payload_size") is None)):
                raise ValueError("available provider lacks verified payload identity")
            if (provider.get("payload_sha256") is not None
                    and provider.get("payload_sha256") != payload.get("sha256")):
                raise ValueError("provider payload hash mismatch")
            if (provider.get("payload_size") is not None
                    and provider.get("payload_size") != payload.get("size")):
                raise ValueError("provider payload size mismatch")
    except (TypeError, ValueError):
        return None
    return {
        "announcement_id": announcement_id,
        "profile_id": profile_id,
        "device_id": device_id,
        "history_revision": revision,
        "current_item_id": current,
        "item": item,
    }


def build_announcement_ack(announcement_id, status="accepted", reason=""):
    return {
        "type": T_ANNOUNCEMENT_ACK,
        "schema_version": cm.ITEM_SCHEMA_VERSION,
        "announcement_id": announcement_id,
        "status": status,
        "reason": str(reason)[:128],
    }


def parse_announcement_ack(msg):
    if (not isinstance(msg, dict) or msg.get("type") != T_ANNOUNCEMENT_ACK
            or msg.get("schema_version") != cm.ITEM_SCHEMA_VERSION
            or not cm.is_valid_item_id(msg.get("announcement_id"))
            or msg.get("status") not in ("accepted", "duplicate", "rejected")
            or not isinstance(msg.get("reason", ""), str)
            or len(msg.get("reason", "")) > 128):
        return None
    return {"announcement_id": msg["announcement_id"], "status": msg["status"],
            "reason": msg.get("reason", "")}


# ── Sync ────────────────────────────────────────────────────────────
def build_request_items(profile_id, item_ids, include_data=True, reason="auto_sync"):
    return {
        "type": T_REQUEST,
        "profile_id": profile_id,
        "item_ids": list(item_ids),
        "include_data": bool(include_data),
        "reason": reason,
    }


def parse_request_items(msg):
    if not isinstance(msg, dict) or msg.get("type") != T_REQUEST:
        return None
    return {
        "profile_id": msg.get("profile_id"),
        "item_ids": list(msg.get("item_ids", [])),
        "include_data": bool(msg.get("include_data", True)),
        "reason": msg.get("reason", "auto_sync"),
    }


# ── Transfer control ────────────────────────────────────────────────
def build_transfer_start(transfer_id, item_id, sha256, total_size, chunk_size,
                         kind=cm.KIND_BINARY, mime="application/octet-stream",
                         file_count=0, display_name=""):
    return {
        "type": T_START,
        "transfer_id": transfer_id,
        "item_id": item_id,
        "sha256": sha256,
        "total_size": int(total_size),
        "chunk_size": int(chunk_size),
        "chunk_count": cm.chunk_count(total_size, chunk_size),
        "kind": kind,
        "mime": mime,
        "file_count": int(file_count),
        "display_name": display_name,
    }


def build_transfer_chunk(transfer_id, item_id, index, offset, data_bytes, chunk_sha=None):
    return {
        "type": T_CHUNK,
        "transfer_id": transfer_id,
        "item_id": item_id,
        "chunk_index": int(index),
        "offset": int(offset),
        "size": len(data_bytes),
        "sha256": chunk_sha,
        "data": base64.b64encode(data_bytes).decode("ascii"),
    }


def decode_chunk_data(msg):
    return base64.b64decode(msg["data"])


def build_transfer_ack(transfer_id, index, status="ok"):
    return {"type": T_ACK, "transfer_id": transfer_id, "chunk_index": int(index),
            "status": status}


def build_transfer_complete(transfer_id, item_id, sha256, status="ok"):
    return {"type": T_COMPLETE, "transfer_id": transfer_id, "item_id": item_id,
            "sha256": sha256, "status": status}


def build_transfer_error(transfer_id, item_id, code, message=""):
    return {"type": T_ERROR, "transfer_id": transfer_id, "item_id": item_id,
            "code": code, "message": message}


def build_preflight(profile_id, item_id, payload_sha256, payload_size, encoding="raw",
                    logical_size=None, file_count=0, known_transfer_size=None,
                    materialized_size=0):
    if not cm.is_valid_item_id(item_id):
        raise ValueError("invalid preflight item_id")
    if not cm.is_valid_sha256(payload_sha256):
        raise ValueError("invalid preflight payload_sha256")
    return {
        "type": T_PREFLIGHT,
        "schema_version": cm.ITEM_SCHEMA_VERSION,
        "profile_id": profile_id,
        "item_id": item_id,
        "payload_sha256": payload_sha256,
        "payload_size": int(payload_size),
        "encoding": encoding,
        "logical_size": int(logical_size) if logical_size is not None else None,
        "file_count": int(file_count),
        "known_transfer_size": int(known_transfer_size) if known_transfer_size is not None else None,
        "materialized_size": int(materialized_size),
    }


def parse_preflight(msg):
    if not isinstance(msg, dict) or msg.get("type") != T_PREFLIGHT:
        return None
    item_id = msg.get("item_id")
    payload_sha = msg.get("payload_sha256")
    size = msg.get("payload_size")
    if not cm.is_valid_item_id(item_id) or not cm.is_valid_sha256(payload_sha):
        return None
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return None
    encoding = msg.get("encoding", "raw")
    if encoding not in ("raw", "deterministic_zip"):
        return None
    logical = msg.get("logical_size")
    if logical is not None and (not isinstance(logical, int) or isinstance(logical, bool) or logical < 0):
        return None
    return {
        "profile_id": msg.get("profile_id"),
        "item_id": item_id,
        "payload_sha256": payload_sha,
        "payload_size": size,
        "encoding": encoding,
        "logical_size": logical,
        "file_count": max(0, int(msg.get("file_count", 0) or 0)),
        "known_transfer_size": msg.get("known_transfer_size"),
        "materialized_size": max(0, int(msg.get("materialized_size", 0) or 0)),
    }


def build_preflight_response(profile_id, item_id, allowed, reason=None, detail=None):
    result = {
        "type": T_PREFLIGHT_RESPONSE,
        "schema_version": cm.ITEM_SCHEMA_VERSION,
        "profile_id": profile_id,
        "item_id": item_id,
        "allowed": bool(allowed),
    }
    if reason:
        result["reason"] = reason
    if detail:
        result["detail"] = dict(detail)
    return result


def parse_preflight_response(msg):
    if not isinstance(msg, dict) or msg.get("type") != T_PREFLIGHT_RESPONSE:
        return None
    if not cm.is_valid_item_id(msg.get("item_id")):
        return None
    return {
        "item_id": msg.get("item_id"),
        "profile_id": msg.get("profile_id"),
        "allowed": bool(msg.get("allowed")),
        "reason": msg.get("reason"),
        "detail": msg.get("detail"),
    }


def build_transfer_resume(transfer_id, item_id, next_index):
    return {"type": T_RESUME, "transfer_id": transfer_id, "item_id": item_id,
            "next_index": int(next_index)}


def iter_chunk_messages(transfer_id, item_id, data, chunk_size=None, hash_chunks=False):
    """Yield chunk messages for ``data`` bytes (helper for the sender)."""
    if chunk_size is None:
        chunk_size = safe_chunk_size()
    for c in cm.chunk_plan(len(data), chunk_size):
        piece = data[c["offset"]:c["offset"] + c["length"]]
        sha = cm.sha256_bytes(piece) if hash_chunks else None
        yield build_transfer_chunk(transfer_id, item_id, c["index"], c["offset"], piece, sha)


class ChunkAssembler:
    """Reassembles chunks in order and verifies the final SHA-256.

    Detects out-of-order / missing / duplicate chunks and hash-mismatched chunks
    so the receiver can request a resume/retry. Keeps data in memory here (the
    runtime's disk-backed receiver uses the same index bookkeeping).
    """

    def __init__(self, total_size, chunk_count, expected_sha=None):
        self.total_size = int(total_size)
        self.chunk_count = int(chunk_count)
        self.expected_sha = expected_sha
        self._chunks = {}      # index -> bytes
        self._received = 0
        self.error = None

    @property
    def next_index(self):
        """Lowest index not yet received (for resume)."""
        i = 0
        while i in self._chunks:
            i += 1
        return i

    def missing_indices(self):
        return [i for i in range(self.chunk_count) if i not in self._chunks]

    def add_chunk(self, index, data, chunk_sha=None):
        """Add a chunk. Returns 'ok', 'duplicate', or 'hash_mismatch'."""
        if index in self._chunks:
            return "duplicate"
        if chunk_sha is not None and cm.sha256_bytes(data) != chunk_sha:
            return "hash_mismatch"
        self._chunks[index] = data
        self._received += len(data)
        return "ok"

    @property
    def bytes_received(self):
        return self._received

    def is_complete(self):
        return len(self._chunks) == self.chunk_count

    def assemble(self):
        """Return the assembled bytes, or raise ValueError on gap / hash mismatch."""
        if not self.is_complete():
            raise ValueError(f"incomplete: missing {self.missing_indices()}")
        data = b"".join(self._chunks[i] for i in range(self.chunk_count))
        if len(data) != self.total_size:
            raise ValueError(f"size mismatch: {len(data)} != {self.total_size}")
        if self.expected_sha and cm.sha256_bytes(data) != self.expected_sha:
            raise ValueError("sha256 mismatch")
        return data
