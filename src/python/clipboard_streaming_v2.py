"""Sequential direct-file streaming and verified receiver staging for V2."""
from __future__ import annotations

import errno
import copy
import hashlib
import os
import shutil
import stat
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

import clipboard_files as clipboard_files
import clipboard_manifest_v2 as manifest_v2
import clipboard_paths as clipboard_paths
from clipboard_framing_v2 import (
    MAX_BINARY_PAYLOAD_BYTES, MAX_ENTRY_INDEX, MAX_LOGICAL_OFFSET,
)


DEFAULT_CHUNK_SIZE = 2 * 1024 * 1024
_SHA256_SIZE = hashlib.sha256().digest_size
_HEX_DIGITS = frozenset("0123456789abcdef")


class StreamV2Error(RuntimeError):
    """Path-free terminal direct-stream error suitable for state reporting."""

    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = bool(retryable)


def _transfer_id(value):
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
    except (ValueError, AttributeError):
        parsed = None
    if parsed is None or parsed.int == 0:
        raise StreamV2Error("invalid_chunk", "transfer_id must be a non-null UUID")
    return parsed.hex


def _uint(value, name, maximum):
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 0 <= value <= maximum):
        raise StreamV2Error("invalid_chunk", f"{name} is outside its unsigned range")
    return value


def _sha256_text(value, name):
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in _HEX_DIGITS for character in value)):
        raise StreamV2Error("source_hash_failed", f"{name} must be lowercase 64-hex")
    return value


@dataclass(frozen=True)
class PayloadChunk:
    transfer_id: str
    entry_index: int
    offset: int
    payload: bytes
    checksum: bytes

    def __post_init__(self):
        transfer_id = _transfer_id(self.transfer_id)
        entry_index = _uint(self.entry_index, "entry_index", MAX_ENTRY_INDEX)
        offset = _uint(self.offset, "offset", MAX_LOGICAL_OFFSET)
        if not isinstance(self.payload, (bytes, bytearray, memoryview)):
            raise StreamV2Error("invalid_chunk", "payload must be bytes-like")
        payload = bytes(self.payload)
        if not payload or len(payload) > MAX_BINARY_PAYLOAD_BYTES:
            raise StreamV2Error("invalid_chunk", "payload length is outside the V2 range")
        if offset + len(payload) > MAX_LOGICAL_OFFSET:
            raise StreamV2Error("invalid_chunk", "payload end exceeds the V2 range")
        if not isinstance(self.checksum, (bytes, bytearray, memoryview)):
            raise StreamV2Error("invalid_chunk", "checksum must be bytes-like")
        checksum = bytes(self.checksum)
        if len(checksum) != _SHA256_SIZE or checksum != hashlib.sha256(payload).digest():
            raise StreamV2Error("invalid_chunk", "payload checksum does not match")
        object.__setattr__(self, "transfer_id", transfer_id)
        object.__setattr__(self, "entry_index", entry_index)
        object.__setattr__(self, "offset", offset)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "checksum", checksum)


def freeze_payload_chunk(value):
    """Copy a chunk or decoded binary frame into an immutable queue item."""
    if isinstance(value, PayloadChunk):
        return PayloadChunk(
            value.transfer_id, value.entry_index, value.offset,
            value.payload, value.checksum)
    return PayloadChunk(
        value.transfer_id, value.entry_index, value.offset,
        value.payload, value.checksum)


def payload_chunk_size(value):
    return len(value.payload)


@dataclass(frozen=True)
class SourceStreamCompletion:
    transfer_id: str
    manifest_digest: str
    total_bytes: int
    file_hashes: object
    source_fingerprints: object

    def __post_init__(self):
        object.__setattr__(self, "transfer_id", _transfer_id(self.transfer_id))
        object.__setattr__(self, "manifest_digest", _sha256_text(
            self.manifest_digest, "manifest_digest"))
        total_bytes = _uint(self.total_bytes, "total_bytes", MAX_LOGICAL_OFFSET)
        if not isinstance(self.file_hashes, Mapping):
            raise StreamV2Error("source_hash_failed", "file_hashes must be an object")
        file_hashes = {}
        for index, digest in self.file_hashes.items():
            index = _uint(index, "file hash index", MAX_ENTRY_INDEX)
            file_hashes[index] = _sha256_text(digest, "file hash")
        if not isinstance(self.source_fingerprints, Mapping):
            raise StreamV2Error(
                "source_hash_failed", "source_fingerprints must be an object")
        fingerprints = {}
        for index, value in self.source_fingerprints.items():
            index = _uint(index, "source fingerprint index", MAX_ENTRY_INDEX)
            if not isinstance(value, Mapping):
                raise StreamV2Error(
                    "source_hash_failed", "source fingerprint must be an object")
            fingerprints[index] = MappingProxyType(copy.deepcopy(dict(value)))
        object.__setattr__(self, "total_bytes", total_bytes)
        object.__setattr__(self, "file_hashes", MappingProxyType(file_hashes))
        object.__setattr__(self, "source_fingerprints", MappingProxyType(
            fingerprints))


@dataclass(frozen=True)
class VerifiedWrite:
    entry_index: int
    offset: int
    length: int
    verified_offset: int
    file_complete: bool


@dataclass(frozen=True)
class StagedFile:
    entry_index: int
    size: int
    sha256: str
    path: str


@dataclass(frozen=True)
class StagedTransferResult:
    transfer_id: str
    provisional_manifest_digest: str
    finalized_manifest: dict
    files: tuple[StagedFile, ...]


class SequentialFileStream:
    """Read captured files once, in manifest order, while calculating hashes."""

    def __init__(self, transfer_id, manifest, local_entries, *,
                 chunk_size=DEFAULT_CHUNK_SIZE, cancelled=None,
                 hash_factory=hashlib.sha256):
        self.transfer_id = _transfer_id(transfer_id)
        self.manifest = manifest_v2.validate_manifest(manifest)
        if (not isinstance(chunk_size, int) or isinstance(chunk_size, bool)
                or not 0 < chunk_size <= MAX_BINARY_PAYLOAD_BYTES):
            raise ValueError("chunk_size must be between 1 byte and 4 MiB")
        if cancelled is not None and not callable(cancelled):
            raise ValueError("cancelled must be callable")
        if not callable(hash_factory):
            raise ValueError("hash_factory must be callable")
        self.chunk_size = chunk_size
        self._cancelled = cancelled
        self._hash_factory = hash_factory
        self._entries = self._match_entries(local_entries)
        self._state = "ready"
        self._completion = None
        self._active_file = None
        for entry in self._entries:
            self._validate_source(entry)

    def _match_entries(self, local_entries):
        if not isinstance(local_entries, (list, tuple)):
            raise StreamV2Error("source_unavailable", "local source entries are invalid")
        by_path = {}
        for entry in local_entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("rel"), str):
                raise StreamV2Error("source_unavailable", "local source entries are invalid")
            if entry["rel"] in by_path:
                raise StreamV2Error("source_unavailable", "local source entries are ambiguous")
            by_path[entry["rel"]] = entry
        matched = []
        for wire_entry in self.manifest["entries"]:
            local = by_path.pop(wire_entry["path"], None)
            try:
                local_size = int(local.get("size", -1)) if local is not None else -1
            except (TypeError, ValueError, OverflowError):
                local_size = -1
            if (local is None or local.get("type", "file") != wire_entry["type"]
                    or local_size != wire_entry["size"]
                    or local.get("source_fingerprint") != wire_entry["source_fingerprint"]):
                raise StreamV2Error("source_unavailable", "local sources do not match manifest")
            matched.append(local)
        if by_path:
            raise StreamV2Error("source_unavailable", "local sources do not match manifest")
        return matched

    def _check_cancelled(self):
        if self._cancelled and self._cancelled():
            raise StreamV2Error("cancelled", "direct file stream cancelled", retryable=True)

    @staticmethod
    def _validate_source(entry, *, opened_stat=None):
        try:
            return clipboard_files.validate_source_snapshot(entry, opened_stat=opened_stat)
        except clipboard_files.CaptureLimitError as exc:
            raise StreamV2Error(
                "source_changed", "clipboard source changed during direct streaming",
                retryable=True) from exc

    def iter_chunks(self):
        if self._state != "ready":
            raise StreamV2Error("source_unavailable", "direct stream is not reusable")
        self._state = "streaming"
        file_hashes = {}
        fingerprints = {}
        total = 0
        try:
            for wire_entry, local_entry in zip(self.manifest["entries"], self._entries):
                self._check_cancelled()
                if wire_entry["type"] == "directory":
                    fingerprints[wire_entry["index"]] = self._validate_source(local_entry)
                    continue
                self._validate_source(local_entry)
                try:
                    source = open(local_entry["abspath"], "rb", buffering=0)
                except OSError as exc:
                    raise StreamV2Error(
                        "source_unavailable", "clipboard source cannot be opened",
                        retryable=True) from exc
                self._active_file = source
                try:
                    try:
                        opened_stat = os.fstat(source.fileno())
                    except OSError as exc:
                        raise StreamV2Error(
                            "source_unavailable", "clipboard source handle is unavailable",
                            retryable=True) from exc
                    self._validate_source(local_entry, opened_stat=opened_stat)
                    try:
                        hasher = self._hash_factory()
                    except Exception as exc:
                        raise StreamV2Error(
                            "source_hash_failed", "source hash cannot be initialized") from exc
                    offset = 0
                    while offset < wire_entry["size"]:
                        self._check_cancelled()
                        amount = min(self.chunk_size, wire_entry["size"] - offset)
                        try:
                            payload = source.read(amount)
                        except OSError as exc:
                            raise StreamV2Error(
                                "source_read_failed", "clipboard source read failed",
                                retryable=True) from exc
                        if not payload:
                            raise StreamV2Error(
                                "source_changed", "clipboard source size changed",
                                retryable=True)
                        try:
                            hasher.update(payload)
                        except Exception as exc:
                            raise StreamV2Error(
                                "source_hash_failed", "source hash update failed") from exc
                        yield PayloadChunk(
                            self.transfer_id, wire_entry["index"], offset, payload,
                            hashlib.sha256(payload).digest())
                        offset += len(payload)
                        total += len(payload)
                    try:
                        grew = source.read(1)
                    except OSError as exc:
                        raise StreamV2Error(
                            "source_read_failed", "clipboard source read failed",
                            retryable=True) from exc
                    if grew:
                        raise StreamV2Error(
                            "source_changed", "clipboard source size changed", retryable=True)
                    try:
                        opened_stat = os.fstat(source.fileno())
                    except OSError as exc:
                        raise StreamV2Error(
                            "source_unavailable", "clipboard source handle is unavailable",
                            retryable=True) from exc
                    fingerprint = self._validate_source(
                        local_entry, opened_stat=opened_stat)
                    try:
                        digest = hasher.hexdigest()
                    except Exception as exc:
                        raise StreamV2Error(
                            "source_hash_failed", "source hash finalization failed") from exc
                    known_hash = wire_entry.get("sha256")
                    if known_hash is not None and known_hash != digest:
                        raise StreamV2Error(
                            "source_changed", "clipboard source hash changed", retryable=True)
                    file_hashes[wire_entry["index"]] = digest
                    fingerprints[wire_entry["index"]] = fingerprint
                finally:
                    active_error = sys.exc_info()[0] is not None
                    try:
                        source.close()
                    except OSError:
                        if not active_error:
                            raise StreamV2Error(
                                "source_read_failed", "clipboard source close failed",
                                retryable=True)
                    self._active_file = None
            for wire_entry, local_entry in zip(self.manifest["entries"], self._entries):
                fingerprints[wire_entry["index"]] = self._validate_source(local_entry)
            self._completion = SourceStreamCompletion(
                self.transfer_id, self.manifest["manifest_digest"], total,
                file_hashes, fingerprints)
            self._state = "completed"
        except BaseException:
            self._state = "failed"
            raise

    def completion(self):
        if self._state != "completed" or self._completion is None:
            raise StreamV2Error("source_unavailable", "direct stream is not complete")
        return self._completion

    def close(self):
        if self._active_file is not None:
            self._active_file.close()
            self._active_file = None
        if self._state not in ("completed", "failed"):
            self._state = "closed"


class IncomingTransferStage:
    """Write one validated transfer sequentially to private index-based staging."""

    def __init__(self, incoming_root, transfer_id, manifest, *,
                 hash_factory=hashlib.sha256):
        self.transfer_id = _transfer_id(transfer_id)
        self.manifest = manifest_v2.validate_manifest(manifest)
        if not callable(hash_factory):
            raise ValueError("hash_factory must be callable")
        self._hash_factory = hash_factory
        self._state = "receiving"
        self._file = None
        self._file_index = None
        self._offset = 0
        self._hasher = None
        self._hashes = {}
        self._stage_dir = None
        self._stage_created = False
        try:
            root = clipboard_paths.ensure_safe_directory_root(incoming_root)
            self._stage_dir = root / self.transfer_id
            os.mkdir(self._stage_dir)
            self._stage_created = True
            stage_stat = os.lstat(self._stage_dir)
            if (not stat.S_ISDIR(stage_stat.st_mode)
                    or clipboard_paths._is_reparse_point(self._stage_dir, stage_stat)):
                raise OSError("unsafe transfer staging directory")
            self._next_entry = 0
            self._advance_non_payload_entries()
        except StreamV2Error:
            self._cleanup()
            raise
        except (OSError, clipboard_paths.PathValidationError) as exc:
            self._cleanup()
            raise StreamV2Error(
                self._io_code(exc, "staging_create_failed"),
                "incoming staging cannot be created") from exc

    @property
    def stage_directory(self):
        return os.fspath(self._stage_dir)

    def _part_path(self, index):
        return self._stage_dir / f"{index}.part"

    def _verified_path(self, index):
        return self._stage_dir / f"{index}.verified"

    def _validate_stage_directory(self):
        try:
            stage_stat = os.lstat(self._stage_dir)
        except OSError as exc:
            self._fail("staging_create_failed", "incoming staging is unavailable", exc)
        if (not stat.S_ISDIR(stage_stat.st_mode)
                or clipboard_paths._is_reparse_point(self._stage_dir, stage_stat)):
            self._fail("staging_create_failed", "incoming staging is unsafe")

    def _new_hasher(self):
        try:
            return self._hash_factory()
        except Exception as exc:
            self._fail("receiver_hash_failed", "receiver hash cannot be initialized", exc)

    def _open_file(self, entry):
        self._validate_stage_directory()
        try:
            self._file = open(self._part_path(entry["index"]), "xb", buffering=0)
        except OSError as exc:
            self._fail(self._io_code(exc, "target_write_failed"),
                       "incoming staging file cannot be created", exc)
        self._file_index = entry["index"]
        self._offset = 0
        self._hasher = self._new_hasher()

    def _advance_non_payload_entries(self):
        entries = self.manifest["entries"]
        while self._next_entry < len(entries):
            entry = entries[self._next_entry]
            if entry["type"] == "directory":
                self._next_entry += 1
                continue
            if entry["size"] != 0:
                break
            self._open_file(entry)
            self._complete_file(entry)
            self._next_entry += 1

    @staticmethod
    def _io_code(exc, default):
        if (getattr(exc, "errno", None) in (errno.ENOSPC, getattr(errno, "EDQUOT", -1))
                or getattr(exc, "winerror", None) in (39, 112)):
            return "disk_full"
        return default

    def _complete_file(self, entry):
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
            physical_size = os.fstat(self._file.fileno()).st_size
        except OSError as exc:
            self._fail(self._io_code(exc, "target_flush_failed"),
                       "incoming staging flush failed", exc)
        if self._offset != entry["size"] or physical_size != entry["size"]:
            self._fail("size_mismatch", "incoming staged file size does not match")
        try:
            digest = self._hasher.hexdigest()
        except Exception as exc:
            self._fail("receiver_hash_failed", "receiver hash finalization failed", exc)
        try:
            self._file.close()
        except OSError as exc:
            self._fail("target_flush_failed", "incoming staging close failed", exc)
        self._file = None
        self._hashes[entry["index"]] = digest
        self._file_index = None
        self._offset = 0
        self._hasher = None

    def accept(self, chunk):
        if self._state != "receiving":
            raise StreamV2Error("invalid_chunk", "incoming stage is not receiving")
        try:
            chunk = freeze_payload_chunk(chunk)
        except StreamV2Error as exc:
            self._terminal_failure(exc)
            raise
        if chunk.transfer_id != self.transfer_id:
            self._fail("invalid_chunk", "chunk transfer_id does not match")
        entries = self.manifest["entries"]
        if self._next_entry >= len(entries):
            self._fail("invalid_chunk", "payload arrived after all files")
        entry = entries[self._next_entry]
        if (entry["type"] != "file" or chunk.entry_index != entry["index"]
                or chunk.offset != self._offset
                or self._offset + len(chunk.payload) > entry["size"]):
            self._fail("invalid_chunk", "payload is not the next expected file range")
        if self._file is None:
            self._open_file(entry)
        try:
            written = self._file.write(chunk.payload)
        except OSError as exc:
            self._fail(self._io_code(exc, "target_write_failed"),
                       "incoming staging write failed", exc)
        if written != len(chunk.payload):
            self._fail("short_write", "incoming staging write was incomplete")
        try:
            self._hasher.update(chunk.payload)
        except Exception as exc:
            self._fail("receiver_hash_failed", "receiver hash update failed", exc)
        start = self._offset
        self._offset += written
        complete = self._offset == entry["size"]
        if complete:
            self._complete_file(entry)
            self._next_entry += 1
            self._advance_non_payload_entries()
        return VerifiedWrite(entry["index"], start, written, start + written, complete)

    def finalize(self, completion):
        if self._state != "receiving":
            raise StreamV2Error("invalid_chunk", "incoming stage cannot be finalized")
        if not isinstance(completion, SourceStreamCompletion):
            self._fail("invalid_chunk", "sender completion evidence is invalid")
        entries = self.manifest["entries"]
        expected_files = {entry["index"] for entry in entries if entry["type"] == "file"}
        expected_entries = {entry["index"] for entry in entries}
        if (self._next_entry != len(entries) or self._file is not None
                or completion.transfer_id != self.transfer_id
                or completion.manifest_digest != self.manifest["manifest_digest"]
                or completion.total_bytes != self.manifest["total_size"]
                or set(completion.file_hashes) != expected_files
                or set(completion.source_fingerprints) != expected_entries):
            self._fail("invalid_chunk", "sender completion evidence does not match transfer")
        for entry in entries:
            if completion.source_fingerprints[entry["index"]] != entry["source_fingerprint"]:
                self._fail("source_changed", "sender source fingerprint changed")
            if entry["type"] == "file":
                digest = completion.file_hashes[entry["index"]]
                if self._hashes.get(entry["index"]) != digest:
                    self._fail("hash_mismatch", "sender and receiver file hashes differ")
        try:
            finalized = manifest_v2.finalize_manifest(
                self.manifest, dict(completion.file_hashes))
        except manifest_v2.ManifestValidationError as exc:
            self._fail("hash_mismatch", "sender file hashes are invalid", exc)
        self._state = "finalizing"
        staged_files = []
        try:
            for entry in entries:
                if entry["type"] != "file":
                    continue
                self._validate_stage_directory()
                source = self._part_path(entry["index"])
                target = self._verified_path(entry["index"])
                os.replace(source, target)
                staged_files.append(StagedFile(
                    entry["index"], entry["size"], self._hashes[entry["index"]],
                    os.fspath(target)))
        except OSError as exc:
            self._fail(self._io_code(exc, "finalize_rename_failed"),
                       "incoming staging finalization failed", exc)
        self._state = "completed"
        return StagedTransferResult(
            self.transfer_id, self.manifest["manifest_digest"], finalized,
            tuple(staged_files))

    def _fail(self, code, message, cause=None):
        error = StreamV2Error(code, message, retryable=code in {
            "disk_full", "source_changed", "target_write_failed",
            "target_flush_failed", "finalize_rename_failed",
        })
        self._terminal_failure(error)
        if cause is not None:
            raise error from cause
        raise error

    def _terminal_failure(self, error):
        self._state = "failed"
        self._cleanup()

    def _cleanup(self):
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
        if self._stage_created and self._stage_dir is not None:
            shutil.rmtree(self._stage_dir, ignore_errors=True)
            self._stage_created = False

    def abort(self):
        if self._state != "completed":
            self._state = "cancelled"
            self._cleanup()

    def close(self):
        if self._state not in ("completed", "failed", "cancelled"):
            self.abort()
