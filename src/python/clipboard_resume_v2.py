"""Transport-neutral persistent resume journals for clipboard transfer V2."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import ntpath
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType

import clipboard_manifest_v2 as manifest_v2
import clipboard_paths


SCHEMA_VERSION = 1
PROTOCOL_MAJOR = 2
MAX_JOURNAL_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = manifest_v2.MAX_ENTRIES
MAX_FINGERPRINT_BYTES = 64 * 1024
MAX_FINGERPRINT_VALUES = 256
MAX_STRING_BYTES = 1024
UINT64_MAX = (1 << 64) - 1

_UUID_HEX = re.compile(r"^[0-9a-f]{32}$")
_ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_JOURNAL_NAME = re.compile(r"^([0-9a-f]{32})\.json$")
_DIRECTIONS = frozenset(("incoming", "outgoing"))
_STATES = frozenset((
    "created", "preflight", "accepted", "sending_manifest", "transferring",
    "paused", "waiting_reconnect", "verifying", "finalizing", "completed",
    "cancelled", "failed", "purging",
))
_INCOMING_STORAGE = frozenset(("missing", "partial", "verified", "directory"))
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_STATE_TRANSITIONS = {
    "created": frozenset(("created", "preflight", "accepted", "sending_manifest",
                           "transferring", "paused", "waiting_reconnect", "finalizing",
                           "cancelled", "failed", "purging")),
    "preflight": frozenset(("preflight", "accepted", "transferring", "paused",
                             "waiting_reconnect", "cancelled", "failed", "purging")),
    "accepted": frozenset(("accepted", "sending_manifest", "transferring", "paused",
                            "waiting_reconnect", "cancelled", "failed", "purging")),
    "sending_manifest": frozenset(("sending_manifest", "transferring", "paused",
                                    "waiting_reconnect", "cancelled", "failed", "purging")),
    "transferring": frozenset(("transferring", "paused", "waiting_reconnect", "verifying",
                                "finalizing", "cancelled", "failed", "purging")),
    "paused": frozenset(("paused", "waiting_reconnect", "transferring", "finalizing",
                          "cancelled", "failed", "purging")),
    "waiting_reconnect": frozenset(("waiting_reconnect", "paused", "transferring",
                                     "finalizing", "cancelled", "failed", "purging")),
    "verifying": frozenset(("verifying", "finalizing", "paused", "waiting_reconnect",
                             "cancelled", "failed", "purging")),
    "finalizing": frozenset(("finalizing", "completed", "paused", "waiting_reconnect",
                              "cancelled", "failed", "purging")),
    "completed": frozenset(("completed", "purging")),
    "cancelled": frozenset(("cancelled", "purging")),
    "failed": frozenset(("failed", "transferring", "paused", "waiting_reconnect",
                          "cancelled", "purging")),
    "purging": frozenset(("purging",)),
}
_TOP_FIELDS = frozenset((
    "schema_version", "protocol_major", "direction", "transfer_id", "generation",
    "peer_id", "profile_id", "provider_id", "item_id", "item_revision",
    "strategy", "manifest_digest", "entry_set_digest", "total_size", "file_count",
    "directory_count", "created_ns", "updated_ns", "state", "retry_count",
    "entries", "journal_digest",
))
_COMMON_ENTRY_FIELDS = frozenset((
    "index", "type", "size", "source_fingerprint", "expected_sha256",
))
_INCOMING_ENTRY_FIELDS = _COMMON_ENTRY_FIELDS | frozenset((
    "verified_offset", "durable_offset", "prefix_sha256", "completed",
    "receiver_sha256", "storage_state",
))
_OUTGOING_ENTRY_FIELDS = _COMMON_ENTRY_FIELDS | frozenset((
    "receiver_verified_offset", "receiver_durable_offset",
))
_IDENTITY_FIELDS = (
    "schema_version", "protocol_major", "direction", "transfer_id", "peer_id",
    "profile_id", "provider_id", "item_id", "item_revision", "strategy",
    "manifest_digest", "entry_set_digest", "total_size", "file_count",
    "directory_count", "created_ns",
)


class ResumeJournalError(ValueError):
    """Path-free journal failure with a stable code and retry classification."""

    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code = str(code)
        self.retryable = bool(retryable)


@dataclass(frozen=True)
class CheckpointPolicy:
    byte_interval: int = 8 * 1024 * 1024
    time_interval: float = 0.25

    def __post_init__(self):
        if (not isinstance(self.byte_interval, int) or isinstance(self.byte_interval, bool)
                or not 1 <= self.byte_interval <= MAX_JOURNAL_BYTES):
            raise ValueError("byte_interval must be between 1 byte and 64 MiB")
        if (not isinstance(self.time_interval, (int, float))
                or isinstance(self.time_interval, bool)
                or not math.isfinite(self.time_interval)
                or not 0 < self.time_interval <= 60):
            raise ValueError("time_interval must be finite and between 0 and 60 seconds")
        object.__setattr__(self, "time_interval", float(self.time_interval))


@dataclass(frozen=True)
class ResumeJournal:
    schema_version: int
    protocol_major: int
    direction: str
    transfer_id: str
    generation: int
    peer_id: str
    profile_id: str
    provider_id: str
    item_id: str
    item_revision: int
    strategy: str
    manifest_digest: str
    entry_set_digest: str
    total_size: int
    file_count: int
    directory_count: int
    created_ns: int
    updated_ns: int
    state: str
    retry_count: int
    entries: tuple
    journal_digest: str

    def __post_init__(self):
        normalized = _validate_journal_object(_journal_to_plain(self))
        for field, value in normalized.items():
            if field == "entries":
                value = tuple(_freeze(entry) for entry in value)
            object.__setattr__(self, field, value)

    def to_dict(self):
        """Return an independent JSON-compatible representation."""
        return _journal_to_plain(self)


@dataclass(frozen=True)
class ResumeFile:
    entry_index: int
    size: int
    durable_offset: int
    prefix_sha256: str | None
    completed: bool


@dataclass(frozen=True)
class ResumePlan:
    """Immutable, path-free durable receiver evidence for restarting a sender."""

    transfer_id: str
    peer_id: str
    profile_id: str
    provider_id: str
    item_id: str
    item_revision: int
    manifest_digest: str
    entry_set_digest: str
    files: tuple[ResumeFile, ...]

    def __post_init__(self):
        object.__setattr__(self, "transfer_id", _uuid_hex(self.transfer_id, "transfer_id"))
        object.__setattr__(self, "peer_id", _identity_text(self.peer_id, "peer_id"))
        object.__setattr__(self, "profile_id", _identity_text(self.profile_id, "profile_id"))
        object.__setattr__(self, "provider_id", _identity_text(
            self.provider_id, "provider_id"))
        object.__setattr__(self, "item_id", _item_id(self.item_id))
        object.__setattr__(self, "item_revision", _uint(self.item_revision, "item_revision"))
        object.__setattr__(self, "manifest_digest", _sha256(
            self.manifest_digest, "manifest_digest"))
        object.__setattr__(self, "entry_set_digest", _sha256(
            self.entry_set_digest, "entry_set_digest"))
        files = tuple(self.files)
        previous_index = -1
        for item in files:
            if not isinstance(item, ResumeFile):
                _error("resume_mismatch", "resume plan file entries are invalid")
            _uint(item.entry_index, "resume entry index", MAX_ENTRIES - 1)
            if item.entry_index <= previous_index:
                _error("resume_mismatch", "resume plan file entries are not ordered")
            previous_index = item.entry_index
            _uint(item.size, "resume file size")
            _uint(item.durable_offset, "resume durable offset")
            if not isinstance(item.completed, bool):
                _error("resume_mismatch", "resume completion evidence is invalid")
            if item.durable_offset > item.size:
                _error("resume_mismatch", "resume plan durable offset exceeds file size")
            if item.durable_offset > 0:
                _sha256(item.prefix_sha256, "resume prefix_sha256")
            elif item.completed and item.size == 0 and item.prefix_sha256 != _EMPTY_SHA256:
                _error("resume_mismatch", "completed empty file lacks durable hash evidence")
            elif not item.completed and item.prefix_sha256 is not None:
                _error("resume_mismatch", "empty resume offset has unexpected hash evidence")
            if item.completed and item.durable_offset != item.size:
                _error("resume_mismatch", "resume completion does not match durable geometry")
            if not item.completed and item.size > 0 and item.durable_offset == item.size:
                _error("resume_mismatch", "fully durable resume file lacks completion evidence")
        object.__setattr__(self, "files", files)

    @property
    def durable_bytes(self):
        return sum(item.durable_offset for item in self.files)


@dataclass(frozen=True)
class RecoveryInventory:
    incoming: tuple[ResumeJournal, ...] = ()
    outgoing: tuple[ResumeJournal, ...] = ()
    future: tuple[str, ...] = ()
    quarantined: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "incoming", tuple(self.incoming))
        object.__setattr__(self, "outgoing", tuple(self.outgoing))
        object.__setattr__(self, "future", tuple(self.future))
        object.__setattr__(self, "quarantined", tuple(self.quarantined))


def _error(code, message, *, retryable=False, cause=None):
    error = ResumeJournalError(code, message, retryable=retryable)
    if cause is not None:
        raise error from cause
    raise error


def _uint(value, name, maximum=UINT64_MAX):
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 0 <= value <= maximum):
        _error("invalid_journal", f"{name} must be an unsigned bounded integer")
    return value


def _uuid_hex(value, name):
    if not isinstance(value, str) or not _UUID_HEX.fullmatch(value):
        _error("invalid_journal", f"{name} must be canonical lowercase UUID hex")
    try:
        parsed = uuid.UUID(hex=value)
    except ValueError as exc:
        _error("invalid_journal", f"{name} must be canonical lowercase UUID hex", cause=exc)
    if parsed.int == 0 or parsed.hex != value:
        _error("invalid_journal", f"{name} must be a non-null canonical UUID")
    return value


def _identity_text(value, name, *, maximum=256):
    value = _bounded_text(value, name, maximum=maximum)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _error("invalid_journal", f"{name} contains control characters")
    return value


def _item_id(value):
    if not isinstance(value, str) or not _ITEM_ID.fullmatch(value):
        _error("invalid_journal", "item_id does not match the manifest contract")
    return value


def _sha256(value, name, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        _error("invalid_journal", f"{name} must be lowercase 64-hex")
    return value


def _bounded_text(value, name, *, maximum=64):
    try:
        encoded = value.encode("utf-8", "strict") if isinstance(value, str) else b""
    except UnicodeEncodeError as exc:
        _error("invalid_journal", f"{name} is not valid UTF-8", cause=exc)
    if not isinstance(value, str) or not value or len(encoded) > maximum:
        _error("invalid_journal", f"{name} is not a bounded non-empty UTF-8 string")
    return value


def _looks_absolute(value):
    return (value.startswith(("/", "\\")) or ntpath.isabs(value)
            or bool(ntpath.splitdrive(value)[0]))


def _path_key(value):
    key = value.casefold().replace("-", "_")
    return key in {"path", "paths", "abspath", "source_path", "staging_path"} \
        or key.endswith(("_path", "_paths"))


def _validate_fingerprint(value):
    if not isinstance(value, Mapping):
        _error("invalid_journal", "source_fingerprint must be an object")
    count = 0

    def visit(current, depth):
        nonlocal count
        if depth > 8:
            _error("invalid_journal", "source_fingerprint exceeds its nesting limit")
        count += 1
        if count > MAX_FINGERPRINT_VALUES:
            _error("invalid_journal", "source_fingerprint exceeds its value count limit")
        if current is None or isinstance(current, bool):
            return current
        if isinstance(current, int) and not isinstance(current, bool):
            return _uint(current, "source_fingerprint integer")
        if isinstance(current, str):
            try:
                encoded = current.encode("utf-8", "strict")
            except UnicodeEncodeError as exc:
                _error("invalid_journal", "source_fingerprint is not valid UTF-8", cause=exc)
            if len(encoded) > MAX_STRING_BYTES:
                _error("invalid_journal", "source_fingerprint string exceeds its size limit")
            if _looks_absolute(current):
                _error("private_path", "source_fingerprint cannot contain an absolute path")
            return current
        if isinstance(current, (list, tuple)):
            return [visit(item, depth + 1) for item in current]
        if isinstance(current, Mapping):
            result = {}
            for key, nested in current.items():
                if not isinstance(key, str) or not key or _path_key(key):
                    _error("private_path", "source_fingerprint cannot contain path fields")
                try:
                    key_bytes = key.encode("utf-8", "strict")
                except UnicodeEncodeError as exc:
                    _error("invalid_journal", "source_fingerprint key is not UTF-8", cause=exc)
                if len(key_bytes) > 128 or key in result:
                    _error("invalid_journal", "source_fingerprint key is invalid")
                result[key] = visit(nested, depth + 1)
            return result
        _error("invalid_journal", "source_fingerprint contains unsupported JSON data")

    normalized = visit(value, 0)
    if len(_canonical_json(normalized)) > MAX_FINGERPRINT_BYTES:
        _error("invalid_journal", "source_fingerprint exceeds its byte limit")
    return normalized


def _canonical_json(value):
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        _error("invalid_journal", "journal is not canonical UTF-8 JSON data", cause=exc)


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(nested) for key, nested in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(nested) for nested in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(nested) for key, nested in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(nested) for nested in value]
    return copy.deepcopy(value)


def _journal_to_plain(journal):
    return {
        "schema_version": journal.schema_version,
        "protocol_major": journal.protocol_major,
        "direction": journal.direction,
        "transfer_id": journal.transfer_id,
        "generation": journal.generation,
        "peer_id": journal.peer_id,
        "profile_id": journal.profile_id,
        "provider_id": journal.provider_id,
        "item_id": journal.item_id,
        "item_revision": journal.item_revision,
        "strategy": journal.strategy,
        "manifest_digest": journal.manifest_digest,
        "entry_set_digest": journal.entry_set_digest,
        "total_size": journal.total_size,
        "file_count": journal.file_count,
        "directory_count": journal.directory_count,
        "created_ns": journal.created_ns,
        "updated_ns": journal.updated_ns,
        "state": journal.state,
        "retry_count": journal.retry_count,
        "entries": [_thaw(entry) for entry in journal.entries],
        "journal_digest": journal.journal_digest,
    }


def _digest_object(value, field):
    digest_input = dict(value)
    digest_input.pop(field, None)
    return hashlib.sha256(_canonical_json(digest_input)).hexdigest()


def _entry_identity(entry):
    return {key: _thaw(entry[key]) for key in (
        "index", "type", "size", "source_fingerprint", "expected_sha256")}


def _validate_entries(entries, direction):
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        _error("invalid_journal", "entries must be a bounded array")
    expected_fields = (_INCOMING_ENTRY_FIELDS if direction == "incoming"
                       else _OUTGOING_ENTRY_FIELDS)
    normalized = []
    files = directories = total = 0
    blocked = False
    for expected_index, source in enumerate(entries):
        if not isinstance(source, dict) or frozenset(source) != expected_fields:
            _error("invalid_journal", "journal entry has missing or unknown fields")
        index = _uint(source["index"], "entry index", MAX_ENTRIES - 1)
        if index != expected_index:
            _error("invalid_journal", "journal entry indices must be contiguous and ordered")
        entry_type = source["type"]
        if entry_type not in ("file", "directory"):
            _error("invalid_journal", "journal entry type is invalid")
        size = _uint(source["size"], "entry size")
        if entry_type == "directory" and size != 0:
            _error("invalid_journal", "directory entry size must be zero")
        fingerprint = _validate_fingerprint(source["source_fingerprint"])
        expected_hash = _sha256(source["expected_sha256"], "expected_sha256", nullable=True)
        if entry_type == "directory" and expected_hash is not None:
            _error("invalid_journal", "directory entry cannot have an expected hash")
        entry = {
            "index": index, "type": entry_type, "size": size,
            "source_fingerprint": fingerprint, "expected_sha256": expected_hash,
        }
        if entry_type == "file":
            files += 1
            total += size
            if total > UINT64_MAX:
                _error("invalid_journal", "journal total size exceeds uint64")
        else:
            directories += 1

        if direction == "outgoing":
            verified = _uint(source["receiver_verified_offset"],
                             "receiver_verified_offset")
            durable = _uint(source["receiver_durable_offset"],
                            "receiver_durable_offset")
            if durable > verified or verified > size:
                _error("invalid_geometry", "outgoing offsets violate durable geometry")
            if entry_type == "directory" and (verified or durable):
                _error("invalid_geometry", "directory offsets must be zero")
            if entry_type == "file":
                if blocked and (verified or durable):
                    _error("invalid_geometry", "outgoing file progress is not sequential")
                if durable != size or verified != size:
                    blocked = True
            entry.update(receiver_verified_offset=verified,
                         receiver_durable_offset=durable)
        else:
            verified = _uint(source["verified_offset"], "verified_offset")
            durable = _uint(source["durable_offset"], "durable_offset")
            prefix_hash = _sha256(source["prefix_sha256"], "prefix_sha256", nullable=True)
            receiver_hash = _sha256(
                source["receiver_sha256"], "receiver_sha256", nullable=True)
            completed = source["completed"]
            storage = source["storage_state"]
            if (not isinstance(completed, bool) or storage not in _INCOMING_STORAGE
                    or durable > verified or verified > size):
                _error("invalid_geometry", "incoming progress geometry is invalid")
            if entry_type == "directory":
                if (verified or durable or prefix_hash is not None or receiver_hash is not None
                        or not completed or storage != "directory"):
                    _error("invalid_geometry", "directory progress geometry is invalid")
            else:
                if completed:
                    if (verified != size or durable != size or receiver_hash is None
                            or prefix_hash != receiver_hash
                            or storage not in ("partial", "verified")):
                        _error("invalid_geometry", "completed file lacks durable hash evidence")
                    if expected_hash is not None and expected_hash != receiver_hash:
                        _error("invalid_geometry", "receiver hash differs from expected hash")
                else:
                    if receiver_hash is not None or storage == "verified":
                        _error("invalid_geometry", "incomplete file has completion evidence")
                    expected_storage = "partial" if verified or durable else "missing"
                    if storage != expected_storage:
                        _error("invalid_geometry", "incoming storage state disagrees with offsets")
                    if (durable > 0) != (prefix_hash is not None):
                        _error("invalid_geometry", "durable prefix hash evidence is incomplete")
                if completed and size == 0 and prefix_hash != _EMPTY_SHA256:
                    _error("invalid_geometry", "completed empty file lacks durable hash evidence")
                if blocked and (verified or durable or completed):
                    _error("invalid_geometry", "incoming file progress is not sequential")
                if not completed:
                    blocked = True
            entry.update(
                verified_offset=verified, durable_offset=durable,
                prefix_sha256=prefix_hash, completed=completed,
                receiver_sha256=receiver_hash, storage_state=storage,
            )
        normalized.append(entry)
    return normalized, total, files, directories


def _validate_journal_object(value):
    if not isinstance(value, dict):
        _error("invalid_journal", "journal must be an object")
    schema = value.get("schema_version")
    if schema != SCHEMA_VERSION or isinstance(schema, bool):
        code = "future_schema" if isinstance(schema, int) and not isinstance(
            schema, bool) and schema > SCHEMA_VERSION else "unsupported_schema"
        _error(code, "unsupported resume journal schema")
    if frozenset(value) != _TOP_FIELDS:
        _error("invalid_journal", "journal has missing or unknown fields")
    if (value.get("protocol_major") != PROTOCOL_MAJOR
            or isinstance(value.get("protocol_major"), bool)):
        _error("unsupported_protocol", "unsupported resume journal protocol")
    direction = value.get("direction")
    if direction not in _DIRECTIONS:
        _error("invalid_journal", "journal direction is invalid")
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "protocol_major": PROTOCOL_MAJOR,
        "direction": direction,
        "transfer_id": _uuid_hex(value.get("transfer_id"), "transfer_id"),
        "generation": _uint(value.get("generation"), "generation"),
        "peer_id": _identity_text(value.get("peer_id"), "peer_id"),
        "profile_id": _identity_text(value.get("profile_id"), "profile_id"),
        "provider_id": _identity_text(value.get("provider_id"), "provider_id"),
        "item_id": _item_id(value.get("item_id")),
        "item_revision": _uint(value.get("item_revision"), "item_revision"),
        "strategy": _bounded_text(value.get("strategy"), "strategy"),
        "manifest_digest": _sha256(value.get("manifest_digest"), "manifest_digest"),
        "entry_set_digest": _sha256(value.get("entry_set_digest"), "entry_set_digest"),
        "total_size": _uint(value.get("total_size"), "total_size"),
        "file_count": _uint(value.get("file_count"), "file_count", MAX_ENTRIES),
        "directory_count": _uint(
            value.get("directory_count"), "directory_count", MAX_ENTRIES),
        "created_ns": _uint(value.get("created_ns"), "created_ns"),
        "updated_ns": _uint(value.get("updated_ns"), "updated_ns"),
        "state": value.get("state"),
        "retry_count": _uint(value.get("retry_count"), "retry_count"),
    }
    if normalized["strategy"] != "stream_v2":
        _error("invalid_journal", "journal strategy must be stream_v2")
    if normalized["state"] not in _STATES:
        _error("invalid_journal", "journal state is invalid")
    if normalized["updated_ns"] < normalized["created_ns"]:
        _error("invalid_journal", "updated_ns precedes created_ns")
    entries, total, files, directories = _validate_entries(value.get("entries"), direction)
    if (total, files, directories) != (
            normalized["total_size"], normalized["file_count"],
            normalized["directory_count"]):
        _error("invalid_journal", "journal aggregate counts do not match entries")
    if direction == "incoming" and normalized["state"] == "completed":
        if any(entry["type"] == "file" and (
                not entry["completed"] or entry["storage_state"] != "verified")
                for entry in entries):
            _error("invalid_geometry", "completed journal has unpublished files")
    if normalized["entry_set_digest"] != hashlib.sha256(_canonical_json([
            _entry_identity(entry) for entry in entries])).hexdigest():
        _error("digest_mismatch", "journal entry-set digest mismatch")
    normalized["entries"] = entries
    normalized["journal_digest"] = _sha256(value.get("journal_digest"), "journal_digest")
    if normalized["journal_digest"] != _digest_object(normalized, "journal_digest"):
        _error("digest_mismatch", "resume journal digest mismatch")
    if len(_canonical_json(normalized)) > MAX_JOURNAL_BYTES:
        _error("journal_too_large", "resume journal exceeds 64 MiB")
    return normalized


def validate_journal(value):
    """Validate and detach a journal object or mapping."""
    if isinstance(value, ResumeJournal):
        value = value.to_dict()
    normalized = _validate_journal_object(_thaw(value))
    return ResumeJournal(**normalized)


def _without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _error("duplicate_key", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_json(raw):
    try:
        text = raw.decode("utf-8", "strict")
        return json.loads(
            text, object_pairs_hook=_without_duplicate_keys,
            parse_constant=lambda constant: _error(
                "invalid_json", f"invalid JSON constant: {constant}"),
        )
    except ResumeJournalError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        _error("invalid_json", "journal is not valid UTF-8 JSON", cause=exc)


def parse_journal(payload):
    """Parse strict canonical UTF-8 JSON after enforcing the 64 MiB bound."""
    if isinstance(payload, str):
        try:
            raw = payload.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            _error("invalid_json", "journal is not valid UTF-8", cause=exc)
    elif isinstance(payload, (bytes, bytearray, memoryview)):
        raw = bytes(payload)
    else:
        _error("invalid_json", "journal payload must be UTF-8 bytes or text")
    if len(raw) > MAX_JOURNAL_BYTES:
        _error("journal_too_large", "resume journal exceeds 64 MiB")
    decoded = _decode_json(raw)
    journal = validate_journal(decoded)
    if raw != canonical_journal_bytes(journal):
        _error("noncanonical_json", "resume journal JSON is not canonical")
    return journal


def canonical_journal_bytes(journal):
    """Return the validated canonical UTF-8 encoding of a journal."""
    validated = validate_journal(journal)
    return _canonical_json(validated.to_dict())


def journal_digest(journal):
    """Return SHA-256 over canonical journal data with its digest omitted."""
    value = journal.to_dict() if isinstance(journal, ResumeJournal) else _thaw(journal)
    if not isinstance(value, dict):
        _error("invalid_journal", "journal must be an object")
    return _digest_object(value, "journal_digest")


def _new_journal(direction, *, transfer_id, peer_id, profile_id, provider_id,
                 manifest, strategy, state, created_ns):
    try:
        manifest = manifest_v2.validate_manifest(manifest)
    except manifest_v2.ManifestValidationError as exc:
        _error("manifest_mismatch", "manifest is invalid for resume", cause=exc)
    now = time.time_ns() if created_ns is None else created_ns
    entries = []
    for source in manifest["entries"]:
        entry = {
            "index": source["index"], "type": source["type"], "size": source["size"],
            "source_fingerprint": copy.deepcopy(source["source_fingerprint"]),
            "expected_sha256": source["sha256"],
        }
        if direction == "outgoing":
            entry.update(receiver_verified_offset=0, receiver_durable_offset=0)
        elif source["type"] == "directory":
            entry.update(
                verified_offset=0, durable_offset=0, prefix_sha256=None,
                completed=True, receiver_sha256=None, storage_state="directory")
        else:
            entry.update(
                verified_offset=0, durable_offset=0, prefix_sha256=None,
                completed=False, receiver_sha256=None, storage_state="missing")
        entries.append(entry)
    value = {
        "schema_version": SCHEMA_VERSION, "protocol_major": PROTOCOL_MAJOR,
        "direction": direction, "transfer_id": transfer_id, "generation": 0,
        "peer_id": peer_id, "profile_id": profile_id, "provider_id": provider_id,
        "item_id": manifest["item_id"], "item_revision": manifest["item_revision"],
        "strategy": strategy, "manifest_digest": manifest["manifest_digest"],
        "entry_set_digest": hashlib.sha256(_canonical_json([
            _entry_identity(entry) for entry in entries])).hexdigest(),
        "total_size": manifest["total_size"], "file_count": manifest["file_count"],
        "directory_count": manifest["directory_count"], "created_ns": now,
        "updated_ns": now, "state": state, "retry_count": 0, "entries": entries,
        "journal_digest": "0" * 64,
    }
    value["journal_digest"] = _digest_object(value, "journal_digest")
    return validate_journal(value)


def _next_journal(journal, entries, *, state=None, retry_count=None, updated_ns=None):
    prior = validate_journal(journal)
    value = prior.to_dict()
    if prior.generation == UINT64_MAX:
        _error("generation_overflow", "journal generation cannot be incremented")
    value["generation"] += 1
    value["entries"] = entries
    value["state"] = prior.state if state is None else state
    value["retry_count"] = prior.retry_count if retry_count is None else retry_count
    value["updated_ns"] = max(prior.updated_ns, time.time_ns()) \
        if updated_ns is None else updated_ns
    value["journal_digest"] = _digest_object(value, "journal_digest")
    return validate_journal(value)


def update_incoming_progress(journal, entry_index, *, verified_offset, durable_offset,
                             prefix_sha256=None, completed=False, receiver_sha256=None,
                             storage_state=None, state=None, retry_count=None,
                             updated_ns=None):
    """Return a validated next incoming generation without mutating its predecessor."""
    prior = validate_journal(journal)
    if prior.direction != "incoming":
        _error("direction_mismatch", "incoming progress requires an incoming journal")
    index = _uint(entry_index, "entry_index", len(prior.entries) - 1)
    entries = prior.to_dict()["entries"]
    entry = entries[index]
    if entry["type"] != "file":
        _error("invalid_geometry", "directory progress cannot be updated")
    entry.update(
        verified_offset=verified_offset, durable_offset=durable_offset,
        prefix_sha256=prefix_sha256, completed=completed,
        receiver_sha256=receiver_sha256,
        storage_state=(storage_state if storage_state is not None else
                       ("verified" if completed else
                        ("partial" if verified_offset or durable_offset else "missing"))),
    )
    return _next_journal(
        prior, entries, state=state, retry_count=retry_count, updated_ns=updated_ns)


def update_outgoing_progress(journal, entry_index, *, receiver_verified_offset,
                             receiver_durable_offset, state=None, retry_count=None,
                             updated_ns=None):
    """Return a validated next outgoing generation with receiver-observed offsets."""
    prior = validate_journal(journal)
    if prior.direction != "outgoing":
        _error("direction_mismatch", "outgoing progress requires an outgoing journal")
    index = _uint(entry_index, "entry_index", len(prior.entries) - 1)
    entries = prior.to_dict()["entries"]
    entry = entries[index]
    if entry["type"] != "file":
        _error("invalid_geometry", "directory progress cannot be updated")
    entry.update(receiver_verified_offset=receiver_verified_offset,
                 receiver_durable_offset=receiver_durable_offset)
    return _next_journal(
        prior, entries, state=state, retry_count=retry_count, updated_ns=updated_ns)


def update_journal_state(journal, state, *, retry_count=None, updated_ns=None):
    """Return a next generation changing only session-level state."""
    prior = validate_journal(journal)
    return _next_journal(
        prior, prior.to_dict()["entries"], state=state,
        retry_count=retry_count, updated_ns=updated_ns)


def validate_resume_match(journal, *, peer_id, profile_id, provider_id, manifest):
    """Reject resume against a different peer, profile, provider, item, or manifest."""
    journal = validate_journal(journal)
    try:
        manifest = manifest_v2.validate_manifest(manifest)
    except manifest_v2.ManifestValidationError as exc:
        _error("manifest_mismatch", "resume manifest is invalid", cause=exc)
    identities = (_identity_text(peer_id, "peer_id"),
                  _identity_text(profile_id, "profile_id"),
                  _identity_text(provider_id, "provider_id"))
    if identities != (journal.peer_id, journal.profile_id, journal.provider_id):
        _error("peer_mismatch", "resume session identity does not match")
    common = [{
        "index": entry["index"], "type": entry["type"], "size": entry["size"],
        "source_fingerprint": entry["source_fingerprint"],
        "expected_sha256": entry["sha256"],
    } for entry in manifest["entries"]]
    entry_digest = hashlib.sha256(_canonical_json(common)).hexdigest()
    if (manifest["item_id"] != journal.item_id
            or manifest["item_revision"] != journal.item_revision
            or manifest["manifest_digest"] != journal.manifest_digest
            or entry_digest != journal.entry_set_digest):
        _error("manifest_mismatch", "resume manifest does not match journal")
    return True


def _resume_entry_digest(manifest):
    common = [{
        "index": entry["index"], "type": entry["type"], "size": entry["size"],
        "source_fingerprint": entry["source_fingerprint"],
        "expected_sha256": entry["sha256"],
    } for entry in manifest["entries"]]
    return hashlib.sha256(_canonical_json(common)).hexdigest()


def validate_resume_pair(outgoing_journal, incoming_journal, manifest):
    """Bind both peer journals and return only mutually durable file evidence."""
    outgoing = validate_journal(outgoing_journal)
    incoming = validate_journal(incoming_journal)
    if outgoing.direction != "outgoing" or incoming.direction != "incoming":
        _error("direction_mismatch", "resume journals have the wrong direction")
    try:
        manifest = manifest_v2.validate_manifest(manifest)
    except manifest_v2.ManifestValidationError as exc:
        _error("resume_mismatch", "resume manifest is invalid", cause=exc)
    identity_fields = (
        "transfer_id", "peer_id", "profile_id", "provider_id", "item_id",
        "item_revision", "strategy", "manifest_digest", "entry_set_digest",
        "total_size", "file_count", "directory_count",
    )
    if any(getattr(outgoing, field) != getattr(incoming, field)
           for field in identity_fields):
        _error("resume_mismatch", "resume journal identities do not match")
    if (manifest["item_id"] != incoming.item_id
            or manifest["item_revision"] != incoming.item_revision
            or manifest["manifest_digest"] != incoming.manifest_digest
            or _resume_entry_digest(manifest) != incoming.entry_set_digest):
        _error("resume_mismatch", "resume journals do not match the manifest")
    files = []
    for wire, sent, received in zip(manifest["entries"], outgoing.entries, incoming.entries):
        if _entry_identity(sent) != _entry_identity(received):
            _error("resume_mismatch", "resume journal entry identities do not match")
        if wire["type"] == "directory":
            continue
        durable = received["durable_offset"]
        prefix = received["prefix_sha256"]
        completed = received["completed"]
        if sent["receiver_durable_offset"] > durable:
            _error("non_durable_resume", "sender durable evidence exceeds receiver state")
        if durable > 0 and prefix is None:
            _error("non_durable_resume", "durable resume prefix hash is missing")
        if completed and durable != wire["size"]:
            _error("non_durable_resume", "completed resume evidence is not fully durable")
        if wire["size"] == 0 and completed and prefix != _EMPTY_SHA256:
            _error("non_durable_resume", "completed empty file hash evidence is missing")
        files.append(ResumeFile(
            wire["index"], wire["size"], durable, prefix, completed))
    return ResumePlan(
        outgoing.transfer_id, outgoing.peer_id, outgoing.profile_id,
        outgoing.provider_id, outgoing.item_id, outgoing.item_revision,
        outgoing.manifest_digest, outgoing.entry_set_digest, tuple(files))


def validate_resume_plan(plan, transfer_id, manifest):
    """Validate a path-free plan against the exact transfer and manifest."""
    if not isinstance(plan, ResumePlan):
        _error("resume_mismatch", "resume plan has an invalid type")
    try:
        manifest = manifest_v2.validate_manifest(manifest)
    except manifest_v2.ManifestValidationError as exc:
        _error("resume_mismatch", "resume manifest is invalid", cause=exc)
    file_entries = tuple(entry for entry in manifest["entries"] if entry["type"] == "file")
    if (_uuid_hex(transfer_id, "transfer_id") != plan.transfer_id
            or manifest["item_id"] != plan.item_id
            or manifest["item_revision"] != plan.item_revision
            or manifest["manifest_digest"] != plan.manifest_digest
            or _resume_entry_digest(manifest) != plan.entry_set_digest
            or len(file_entries) != len(plan.files)):
        _error("resume_mismatch", "resume plan does not match transfer manifest")
    for wire, item in zip(file_entries, plan.files):
        if (item.entry_index != wire["index"] or item.size != wire["size"]
                or item.durable_offset > wire["size"]):
            _error("resume_mismatch", "resume plan file geometry does not match manifest")
    return plan


def _is_reparse(path, path_stat=None):
    try:
        path_stat = os.lstat(path) if path_stat is None else path_stat
    except OSError:
        return True
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(path_stat.st_mode) or bool(attributes & reparse_flag)


def durable_replace(source, target):
    """Atomically replace in-place and request write-through on Windows."""
    if os.name == "nt":
        import ctypes
        move = ctypes.windll.kernel32.MoveFileExW
        move.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        move.restype = ctypes.c_int
        if not move(os.fspath(source), os.fspath(target), 0x1 | 0x8):
            raise ctypes.WinError()
    else:
        os.replace(source, target)


@contextmanager
def _process_lock(path, timeout=5.0):
    """Cooperatively serialize journal CAS across store instances/processes."""
    handle = open(path, "a+b")
    try:
        if os.path.getsize(path) == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    _error("store_busy", "resume journal lock timed out", retryable=True)
                time.sleep(0.01)
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


class ResumeJournalStore:
    """Atomic journal storage split by direction with isolated quarantine."""

    def __init__(self, root):
        self._lock = threading.RLock()
        self.root = os.path.abspath(os.fspath(root))
        self.incoming_directory = os.path.join(self.root, "incoming")
        self.outgoing_directory = os.path.join(self.root, "outgoing")
        self.quarantine_directory = os.path.join(self.root, "quarantine")
        try:
            clipboard_paths.ensure_safe_directory_root(self.root)
            self._require_safe_directory(self.root)
            for directory in (self.incoming_directory, self.outgoing_directory,
                              self.quarantine_directory):
                try:
                    os.mkdir(directory)
                except FileExistsError:
                    pass
                self._require_safe_directory(directory)
        except (OSError, clipboard_paths.PathValidationError) as exc:
            _error("store_io", "resume journal store cannot be initialized", cause=exc)

    @staticmethod
    def _require_safe_directory(path):
        path_stat = os.lstat(path)
        if not stat.S_ISDIR(path_stat.st_mode) or _is_reparse(path, path_stat):
            _error("unsafe_store", "resume journal store contains an unsafe directory")

    def _directory(self, direction):
        if direction not in _DIRECTIONS:
            _error("invalid_direction", "journal direction must be incoming or outgoing")
        return self.incoming_directory if direction == "incoming" else self.outgoing_directory

    def _path(self, direction, transfer_id):
        return os.path.join(self._directory(direction),
                            f"{_uuid_hex(transfer_id, 'transfer_id')}.json")

    def create_incoming(self, *, transfer_id, peer_id, profile_id, provider_id,
                        manifest, strategy="stream_v2", state="created", created_ns=None):
        journal = _new_journal(
            "incoming", transfer_id=transfer_id, peer_id=peer_id,
            profile_id=profile_id, provider_id=provider_id, manifest=manifest,
            strategy=strategy, state=state, created_ns=created_ns)
        return self._create(journal)

    def create_outgoing(self, *, transfer_id, peer_id, profile_id, provider_id,
                        manifest, strategy="stream_v2", state="created", created_ns=None):
        journal = _new_journal(
            "outgoing", transfer_id=transfer_id, peer_id=peer_id,
            profile_id=profile_id, provider_id=provider_id, manifest=manifest,
            strategy=strategy, state=state, created_ns=created_ns)
        return self._create(journal)

    def _create(self, journal):
        with self._lock:
            target = self._path(journal.direction, journal.transfer_id)
            lock = target + ".lock"
            with _process_lock(lock):
                if os.path.lexists(target):
                    _error("already_exists", "resume journal already exists")
                return self._atomic_replace(journal)

    @staticmethod
    def _read_bounded(path):
        try:
            path_stat = os.lstat(path)
            if not stat.S_ISREG(path_stat.st_mode) or _is_reparse(path, path_stat):
                _error("unsafe_journal", "resume journal is not a regular file")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                handle = os.fdopen(descriptor, "rb", closefd=True)
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            with handle:
                return handle.read(MAX_JOURNAL_BYTES + 1)
        except ResumeJournalError:
            raise
        except OSError as exc:
            code = "not_found" if isinstance(exc, FileNotFoundError) else "store_io"
            _error(code, "resume journal cannot be read", retryable=code == "store_io", cause=exc)

    def load(self, direction, transfer_id):
        transfer_id = _uuid_hex(transfer_id, "transfer_id")
        path = self._path(direction, transfer_id)
        raw = self._read_bounded(path)
        journal = parse_journal(raw)
        if journal.direction != direction or journal.transfer_id != transfer_id:
            _error("identity_mismatch", "journal identity does not match its location")
        return journal

    @staticmethod
    def _validate_transition(old, new):
        if any(getattr(old, field) != getattr(new, field) for field in _IDENTITY_FIELDS):
            _error("identity_mismatch", "journal identity cannot change across generations")
        if new.generation != old.generation + 1:
            _error("generation_conflict", "next journal generation is not contiguous")
        if new.updated_ns < old.updated_ns or new.retry_count < old.retry_count:
            _error("invalid_transition", "journal timestamp or retry count regressed")
        if new.state not in _STATE_TRANSITIONS[old.state]:
            _error("invalid_transition", "journal state transition is invalid")
        for before, after in zip(old.entries, new.entries):
            if _entry_identity(before) != _entry_identity(after):
                _error("identity_mismatch", "journal entry identity cannot change")
            if old.direction == "incoming":
                if after["durable_offset"] < before["durable_offset"]:
                    _error("invalid_transition", "incoming durable progress regressed")
                if before["completed"] and not after["completed"]:
                    _error("invalid_transition", "completed incoming file regressed")
                if (before["completed"] and after["receiver_sha256"]
                        != before["receiver_sha256"]):
                    _error("invalid_transition", "completed incoming hash changed")
                if (after["durable_offset"] == before["durable_offset"]
                        and after["prefix_sha256"] != before["prefix_sha256"]
                        and not (not before["completed"] and after["completed"]
                                 and after["size"] == 0)):
                    _error("invalid_transition", "durable prefix evidence changed")
                storage_order = {"missing": 0, "partial": 1, "verified": 2,
                                 "directory": 3}
                if storage_order[after["storage_state"]] < storage_order[before["storage_state"]]:
                    _error("invalid_transition", "incoming storage state regressed")
                if (before["storage_state"] == "verified"
                        and after["storage_state"] != "verified"):
                    _error("invalid_transition", "verified incoming storage regressed")
            elif after["receiver_durable_offset"] < before["receiver_durable_offset"]:
                _error("invalid_transition", "receiver durable progress regressed")

    def commit(self, journal, expected_generation):
        journal = validate_journal(journal)
        expected_generation = _uint(expected_generation, "expected_generation")
        with self._lock:
            target = self._path(journal.direction, journal.transfer_id)
            with _process_lock(target + ".lock"):
                current = self.load(journal.direction, journal.transfer_id)
                if current.generation != expected_generation:
                    _error("generation_conflict", "resume journal generation changed",
                           retryable=True)
                self._validate_transition(current, journal)
                return self._atomic_replace(journal)

    def _atomic_replace(self, journal):
        directory = self._directory(journal.direction)
        target = self._path(journal.direction, journal.transfer_id)
        temp = os.path.join(directory, f".{journal.transfer_id}.{uuid.uuid4().hex}.tmp")
        payload = canonical_journal_bytes(journal)
        try:
            with open(temp, "xb") as handle:
                written = handle.write(payload)
                if written != len(payload):
                    _error("store_io", "resume journal write was incomplete", retryable=True)
                handle.flush()
                os.fsync(handle.fileno())
            durable_replace(temp, target)
            if os.name == "posix":
                descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            return self.load(journal.direction, journal.transfer_id)
        except ResumeJournalError:
            raise
        except OSError as exc:
            _error("store_io", "resume journal atomic commit failed", retryable=True, cause=exc)
        finally:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def quarantine(self, direction, transfer_id):
        transfer_id = _uuid_hex(transfer_id, "transfer_id")
        with self._lock:
            source = self._path(direction, transfer_id)
            try:
                source_stat = os.lstat(source)
                if not stat.S_ISREG(source_stat.st_mode) or _is_reparse(source, source_stat):
                    _error("unsafe_journal", "unsafe journal cannot be quarantined")
                name = f"{direction}-{transfer_id}-{uuid.uuid4().hex}.json"
                os.replace(source, os.path.join(self.quarantine_directory, name))
                return name
            except ResumeJournalError:
                raise
            except OSError as exc:
                _error("store_io", "resume journal cannot be quarantined", cause=exc)

    def purge(self, direction, transfer_id):
        """Delete only the selected journal; payload and staging data are untouched."""
        with self._lock:
            path = self._path(direction, transfer_id)
            try:
                path_stat = os.lstat(path)
                if not stat.S_ISREG(path_stat.st_mode) or _is_reparse(path, path_stat):
                    _error("unsafe_journal", "unsafe journal cannot be purged")
                os.unlink(path)
                return True
            except FileNotFoundError:
                return False
            except ResumeJournalError:
                raise
            except OSError as exc:
                _error("store_io", "resume journal cannot be purged", cause=exc)

    purge_journal = purge

    def inventory(self):
        return self.scan_startup()

    def scan_startup(self):
        valid = {"incoming": [], "outgoing": []}
        future = []
        with self._lock:
            for direction in ("incoming", "outgoing"):
                directory = self._directory(direction)
                try:
                    candidates = sorted(os.scandir(directory), key=lambda entry: entry.name)
                except OSError as exc:
                    _error("store_io", "resume journal directory cannot be scanned", cause=exc)
                for candidate in candidates:
                    match = _JOURNAL_NAME.fullmatch(candidate.name)
                    if match is None:
                        continue
                    transfer_id = match.group(1)
                    try:
                        candidate_stat = candidate.stat(follow_symlinks=False)
                        if (not stat.S_ISREG(candidate_stat.st_mode)
                                or _is_reparse(candidate.path, candidate_stat)):
                            continue
                        raw = self._read_bounded(candidate.path)
                        if len(raw) > MAX_JOURNAL_BYTES:
                            _error("journal_too_large", "resume journal exceeds 64 MiB")
                        decoded = _decode_json(raw)
                        if (isinstance(decoded, dict)
                                and isinstance(decoded.get("schema_version"), int)
                                and not isinstance(decoded.get("schema_version"), bool)
                                and decoded["schema_version"] > SCHEMA_VERSION):
                            future.append(f"{direction}/{candidate.name}")
                            continue
                        journal = parse_journal(raw)
                        if journal.direction != direction or journal.transfer_id != transfer_id:
                            _error("identity_mismatch",
                                   "journal identity does not match its location")
                        valid[direction].append(journal)
                    except (ResumeJournalError, OSError):
                        try:
                            self.quarantine(direction, transfer_id)
                        except ResumeJournalError:
                            pass
            try:
                quarantined = tuple(sorted(
                    entry.name for entry in os.scandir(self.quarantine_directory)
                    if entry.is_file(follow_symlinks=False)))
            except OSError as exc:
                _error("store_io", "quarantine directory cannot be scanned", cause=exc)
        return RecoveryInventory(
            incoming=tuple(valid["incoming"]), outgoing=tuple(valid["outgoing"]),
            future=tuple(sorted(future)), quarantined=quarantined)
