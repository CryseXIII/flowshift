"""Canonical schema-2 batch manifests for clipboard transfer V2."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass

from clipboard_paths import PathValidationError, validate_path_entries


SCHEMA_VERSION = 2
PROTOCOL_MAJOR = 2
UINT64_MAX = (1 << 64) - 1
HARD_MAX_LOGICAL_BYTES = 100 * 1024 ** 4
MAX_FILES = 100_000
MAX_DIRECTORIES = 100_000
MAX_ENTRIES = 200_000
MAX_MANIFEST_BYTES = 16 * 1024 ** 2

_ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HASH_STATES = frozenset(("unhashed", "hashing", "verified", "changed", "invalid"))
_MANIFEST_FIELDS = frozenset((
    "schema_version", "protocol_major", "item_id", "item_revision",
    "manifest_digest", "total_size", "file_count", "directory_count", "entries",
))
_ENTRY_FIELDS = frozenset((
    "index", "path", "type", "size", "mtime_ns", "source_fingerprint",
    "hash_state", "sha256",
))
_REQUIRED_ENTRY_FIELDS = _ENTRY_FIELDS - {"mtime_ns"}


class ManifestValidationError(ValueError):
    """Raised when a V2 batch manifest violates its wire contract."""


@dataclass(frozen=True)
class ManifestLimits:
    """Fixed protocol ceilings plus runtime-configurable logical size caps."""

    max_total_size: int = HARD_MAX_LOGICAL_BYTES
    max_file_size: int = HARD_MAX_LOGICAL_BYTES
    max_files: int = MAX_FILES
    max_directories: int = MAX_DIRECTORIES
    max_entries: int = MAX_ENTRIES
    max_manifest_bytes: int = MAX_MANIFEST_BYTES

    def __post_init__(self):
        ceilings = {
            "max_total_size": (self.max_total_size, HARD_MAX_LOGICAL_BYTES),
            "max_file_size": (self.max_file_size, HARD_MAX_LOGICAL_BYTES),
            "max_files": (self.max_files, MAX_FILES),
            "max_directories": (self.max_directories, MAX_DIRECTORIES),
            "max_entries": (self.max_entries, MAX_ENTRIES),
            "max_manifest_bytes": (self.max_manifest_bytes, MAX_MANIFEST_BYTES),
        }
        for name, (value, ceiling) in ceilings.items():
            if (not isinstance(value, int) or isinstance(value, bool)
                    or value < 0 or value > ceiling):
                raise ValueError(f"invalid {name}")


DEFAULT_LIMITS = ManifestLimits()


def canonical_json_bytes(value) -> bytes:
    """Encode JSON using the schema-2 canonical UTF-8 representation."""
    try:
        text = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return text.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ManifestValidationError("manifest is not canonical JSON data") from exc


def manifest_digest(manifest: dict) -> str:
    """Return SHA-256 over canonical manifest bytes with the digest omitted."""
    if not isinstance(manifest, dict):
        raise ManifestValidationError("manifest must be an object")
    digest_input = dict(manifest)
    digest_input.pop("manifest_digest", None)
    return hashlib.sha256(canonical_json_bytes(digest_input)).hexdigest()


def content_identity(manifest: dict, *, limits=None) -> str:
    """Return finalized logical batch identity, independent of capture metadata."""
    if limits is None:
        limits = DEFAULT_LIMITS
    validated = validate_manifest(manifest, limits=limits)
    identity_entries = []
    for entry in validated["entries"]:
        if entry["type"] == "file" and (entry["hash_state"] != "verified"
                                          or not _SHA256.fullmatch(entry.get("sha256") or "")):
            raise ManifestValidationError("finalized file entry requires a lowercase sha256")
        if entry["type"] == "directory" and entry.get("sha256") is not None:
            raise ManifestValidationError("finalized directory entry cannot have a sha256")
        identity_entries.append({
            "path": entry["path"], "type": entry["type"], "size": entry["size"],
            "sha256": entry.get("sha256"),
        })
    return hashlib.sha256(canonical_json_bytes({"entries": identity_entries})).hexdigest()


def finalize_manifest(manifest: dict, file_hashes, *, limits=DEFAULT_LIMITS) -> dict:
    """Return the canonical next revision with every file hash verified."""
    validated = validate_manifest(manifest, limits=limits)
    if not isinstance(file_hashes, dict):
        raise ManifestValidationError("file_hashes must be an object")
    expected_indices = {
        entry["index"] for entry in validated["entries"] if entry["type"] == "file"
    }
    if set(file_hashes) != expected_indices:
        raise ManifestValidationError("file_hashes must exactly cover manifest files")
    normalized_hashes = {}
    for index, digest in file_hashes.items():
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ManifestValidationError("file hash must be lowercase 64-hex")
        normalized_hashes[index] = digest

    already_final = True
    for entry in validated["entries"]:
        if entry["type"] != "file":
            continue
        digest = normalized_hashes[entry["index"]]
        if entry["hash_state"] == "verified":
            if entry["sha256"] != digest:
                raise ManifestValidationError("finalized manifest hash conflicts with replay")
        else:
            already_final = False
    if already_final:
        return validated
    if validated["item_revision"] == UINT64_MAX:
        raise ManifestValidationError("item_revision cannot be incremented")

    finalized = copy.deepcopy(validated)
    finalized["item_revision"] += 1
    for entry in finalized["entries"]:
        if entry["type"] == "file":
            entry["hash_state"] = "verified"
            entry["sha256"] = normalized_hashes[entry["index"]]
    finalized["manifest_digest"] = manifest_digest(finalized)
    return validate_manifest(finalized, limits=limits)


def _uint64(value, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= UINT64_MAX:
        raise ManifestValidationError(f"{field} must be an unsigned 64-bit integer")
    return value


def _validate_json_value(value, field: str, depth: int = 0):
    if depth > 16:
        raise ManifestValidationError(f"{field} exceeds the nesting limit")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        _uint64(value, field)
        return
    if isinstance(value, list):
        for nested in value:
            _validate_json_value(nested, field, depth + 1)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ManifestValidationError(f"{field} keys must be strings")
        for nested in value.values():
            _validate_json_value(nested, field, depth + 1)
        return
    raise ManifestValidationError(f"{field} contains an unsupported JSON value")


def _normalize_entry(entry: dict, *, require_index: bool) -> dict:
    if not isinstance(entry, dict):
        raise ManifestValidationError("manifest entry must be an object")
    fields = frozenset(entry)
    required = _REQUIRED_ENTRY_FIELDS if require_index else _REQUIRED_ENTRY_FIELDS - {"index"}
    allowed = _ENTRY_FIELDS if require_index else _ENTRY_FIELDS - {"index"}
    if not required <= fields or not fields <= allowed:
        raise ManifestValidationError("manifest entry has missing or unknown fields")

    entry_type = entry.get("type")
    if entry_type not in ("file", "directory"):
        raise ManifestValidationError("manifest entry type must be file or directory")
    size = _uint64(entry.get("size"), "entry size")
    if entry_type == "directory" and size != 0:
        raise ManifestValidationError("directory entry size must be zero")
    hash_state = entry.get("hash_state")
    if hash_state not in _HASH_STATES:
        raise ManifestValidationError("invalid entry hash_state")
    sha256 = entry.get("sha256")
    if sha256 is not None and (not isinstance(sha256, str) or not _SHA256.fullmatch(sha256)):
        raise ManifestValidationError("entry sha256 must be lowercase 64-hex or null")
    if entry_type == "directory" and sha256 is not None:
        raise ManifestValidationError("directory entry cannot have a sha256")
    if hash_state == "verified" and sha256 is None:
        raise ManifestValidationError("verified entry requires a sha256")

    fingerprint = entry.get("source_fingerprint")
    if not isinstance(fingerprint, dict):
        raise ManifestValidationError("entry source_fingerprint must be an object")
    _validate_json_value(fingerprint, "source_fingerprint")

    normalized = {
        "path": entry.get("path"),
        "type": entry_type,
        "size": size,
        "source_fingerprint": copy.deepcopy(fingerprint),
        "hash_state": hash_state,
        "sha256": sha256,
    }
    if "mtime_ns" in entry:
        normalized["mtime_ns"] = _uint64(entry["mtime_ns"], "entry mtime_ns")
    if require_index:
        normalized["index"] = _uint64(entry.get("index"), "entry index")
    return normalized


def _validate_entries(entries, limits: ManifestLimits, *, require_indices: bool, require_order: bool):
    if not isinstance(entries, list):
        raise ManifestValidationError("manifest entries must be an array")
    if len(entries) > limits.max_entries:
        raise ManifestValidationError("manifest exceeds the entry count limit")

    normalized = [_normalize_entry(entry, require_index=require_indices) for entry in entries]
    try:
        canonical_paths = validate_path_entries(normalized)
    except PathValidationError as exc:
        raise ManifestValidationError(str(exc)) from exc
    for entry, path in zip(normalized, canonical_paths):
        if require_order and entry["path"] != path:
            raise ManifestValidationError("manifest path is not in canonical wire form")
        entry["path"] = path

    expected_order = sorted(normalized, key=lambda entry: entry["path"].encode("utf-8"))
    if require_order and normalized != expected_order:
        raise ManifestValidationError("manifest entries are not sorted by canonical path")
    if not require_order:
        normalized = expected_order

    for index, entry in enumerate(normalized):
        if require_indices:
            if entry["index"] != index:
                raise ManifestValidationError("manifest entry indices must be contiguous and zero-based")
        else:
            entry["index"] = index

    file_count = sum(entry["type"] == "file" for entry in normalized)
    directory_count = len(normalized) - file_count
    if file_count > limits.max_files:
        raise ManifestValidationError("manifest exceeds the file count limit")
    if directory_count > limits.max_directories:
        raise ManifestValidationError("manifest exceeds the directory count limit")

    total_size = 0
    for entry in normalized:
        if entry["type"] == "file" and entry["size"] > limits.max_file_size:
            raise ManifestValidationError("manifest file exceeds the configured size limit")
        total_size += entry["size"]
        if total_size > UINT64_MAX or total_size > limits.max_total_size:
            raise ManifestValidationError("manifest exceeds the configured total size limit")
    return normalized, total_size, file_count, directory_count


def build_manifest(item_id: str, item_revision: int, entries, *, limits=DEFAULT_LIMITS) -> dict:
    """Build, sort, index, digest, and validate a canonical schema-2 manifest."""
    if not isinstance(limits, ManifestLimits):
        raise TypeError("limits must be ManifestLimits")
    if not isinstance(item_id, str) or not _ITEM_ID.fullmatch(item_id):
        raise ManifestValidationError("invalid manifest item_id")
    revision = _uint64(item_revision, "item_revision")

    bounded_entries = []
    try:
        iterator = iter(entries)
    except TypeError as exc:
        raise ManifestValidationError("manifest entries must be iterable") from exc
    for entry in iterator:
        if len(bounded_entries) >= limits.max_entries:
            raise ManifestValidationError("manifest exceeds the entry count limit")
        bounded_entries.append(entry)
    normalized, total_size, file_count, directory_count = _validate_entries(
        bounded_entries, limits, require_indices=False, require_order=False
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "protocol_major": PROTOCOL_MAJOR,
        "item_id": item_id,
        "item_revision": revision,
        "total_size": total_size,
        "file_count": file_count,
        "directory_count": directory_count,
        "entries": normalized,
    }
    manifest["manifest_digest"] = manifest_digest(manifest)
    validate_manifest(manifest, limits=limits)
    return manifest


def validate_manifest(manifest: dict, *, limits=DEFAULT_LIMITS) -> dict:
    """Validate and return an independent schema-2 manifest object."""
    if not isinstance(limits, ManifestLimits):
        raise TypeError("limits must be ManifestLimits")
    if not isinstance(manifest, dict) or frozenset(manifest) != _MANIFEST_FIELDS:
        raise ManifestValidationError("manifest has missing or unknown fields")
    if (not isinstance(manifest.get("schema_version"), int)
            or isinstance(manifest.get("schema_version"), bool)
            or manifest["schema_version"] != SCHEMA_VERSION):
        raise ManifestValidationError("unsupported manifest schema_version")
    if (not isinstance(manifest.get("protocol_major"), int)
            or isinstance(manifest.get("protocol_major"), bool)
            or manifest["protocol_major"] != PROTOCOL_MAJOR):
        raise ManifestValidationError("unsupported manifest protocol_major")
    item_id = manifest.get("item_id")
    if not isinstance(item_id, str) or not _ITEM_ID.fullmatch(item_id):
        raise ManifestValidationError("invalid manifest item_id")
    revision = _uint64(manifest.get("item_revision"), "item_revision")

    normalized, total_size, file_count, directory_count = _validate_entries(
        manifest.get("entries"), limits, require_indices=True, require_order=True
    )
    expected_counts = (
        _uint64(manifest.get("total_size"), "total_size"),
        _uint64(manifest.get("file_count"), "file_count"),
        _uint64(manifest.get("directory_count"), "directory_count"),
    )
    if expected_counts != (total_size, file_count, directory_count):
        raise ManifestValidationError("manifest aggregate counts do not match its entries")

    digest = manifest.get("manifest_digest")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ManifestValidationError("manifest_digest must be lowercase 64-hex")
    if digest != manifest_digest(manifest):
        raise ManifestValidationError("manifest digest mismatch")

    result = {
        "schema_version": SCHEMA_VERSION,
        "protocol_major": PROTOCOL_MAJOR,
        "item_id": item_id,
        "item_revision": revision,
        "manifest_digest": digest,
        "total_size": total_size,
        "file_count": file_count,
        "directory_count": directory_count,
        "entries": normalized,
    }
    if len(canonical_json_bytes(result)) > limits.max_manifest_bytes:
        raise ManifestValidationError("canonical manifest exceeds the size limit")
    return result


def canonical_manifest_bytes(manifest: dict, *, limits=DEFAULT_LIMITS) -> bytes:
    """Validate and serialize a complete manifest in canonical wire form."""
    return canonical_json_bytes(validate_manifest(manifest, limits=limits))


def _object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_manifest(payload, *, limits=DEFAULT_LIMITS) -> dict:
    """Parse canonical UTF-8 bytes and validate the complete manifest."""
    if not isinstance(limits, ManifestLimits):
        raise TypeError("limits must be ManifestLimits")
    if isinstance(payload, str):
        try:
            raw = payload.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ManifestValidationError("manifest is not valid UTF-8") from exc
    elif isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    else:
        raise ManifestValidationError("manifest payload must be UTF-8 bytes or text")
    if len(raw) > limits.max_manifest_bytes:
        raise ManifestValidationError("canonical manifest exceeds the size limit")
    try:
        text = raw.decode("utf-8")
        decoded = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ManifestValidationError(f"invalid JSON constant: {value}")
            ),
        )
    except ManifestValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestValidationError("manifest is not valid canonical UTF-8 JSON") from exc
    manifest = validate_manifest(decoded, limits=limits)
    if raw != canonical_json_bytes(manifest):
        raise ManifestValidationError("manifest JSON is not canonical")
    return manifest
