"""FlowShift clipboard store — per-profile persistent history (filesystem).

Layer 1 (store) of the clipboard feature. Each profile/peer gets its own history
directory with an ``index.json`` and content-addressed ``objects/<sha256>`` blobs
(so identical content is stored once and dedup is automatic). Supports add, list,
get, delete-one, delete-all, pin/unpin, size accounting, FIFO + size eviction and
persistence across restarts. Pure filesystem + ``clipboard_model`` logic; no
Windows APIs and no network, so it is testable in a temp dir on any OS.

Layout::

    <root>/profiles/<profile_id>/
        index.json
        objects/<sha256>
        previews/<item_id>.png
        temp/
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import tempfile
import time
import threading

import clipboard_model as cm
import clipboard_files as cfiles
import clipboard_sources as csrc
import clipboard_transfer as ctt


STORE_SCHEMA_VERSION = 2
MAX_TRANSFER_SESSION_SNAPSHOTS = 256
MAX_INTEGRITY_TOMBSTONES = 256
_OBJECT_DELIVERABLE_PAYLOAD_STATES = frozenset({
    "source_available", "cached", "materialized",
})


def schema_backup_path(index_path, from_version=0, to_version=STORE_SCHEMA_VERSION):
    stem, ext = os.path.splitext(index_path)
    return f"{stem}.backup-schema-{from_version}-to-{to_version}{ext}"


def _atomic_write_bytes(path, payload):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.",
                                     suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _atomic_write_json(path, document):
    payload = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_write_bytes(path, payload)


class ClipboardStore:
    def __init__(self, root_dir, profile_id):
        self.root = root_dir
        self.profile_id = profile_id
        self.dir = os.path.join(root_dir, "profiles", profile_id)
        self.objects_dir = os.path.join(self.dir, "objects")
        self.previews_dir = os.path.join(self.dir, "previews")
        self.temp_dir = os.path.join(self.dir, "temp")
        self.index_path = os.path.join(self.dir, "index.json")
        self._lock = threading.RLock()
        self._items = []          # list of item dicts (chronological by seq)
        self._revision = 0
        self._seq = 0
        self._current_item_id = None
        self._received_cache = {}
        self._materialization_leases = {}
        self._transfer_sessions = {}
        self._integrity_tombstones = {}
        self._integrity_failures = set()
        self._index_extra = {}
        self._read_only = False
        self._load_error = None
        self._ensure_dirs()
        self._load()

    # ── setup / persistence ────────────────────────────────────────
    def _ensure_dirs(self):
        for d in (self.dir, self.objects_dir, self.previews_dir, self.temp_dir):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass

    def _load(self):
        with self._lock:
            if not os.path.exists(self.index_path):
                return
            try:
                with open(self.index_path, "rb") as handle:
                    raw = handle.read()
                data = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(data, dict):
                    raise ValueError("clipboard index root must be an object")
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                self._recover_corrupt(exc)
                return

            version = data.get("schema_version", 0)
            if not isinstance(version, int) or isinstance(version, bool) or version < 0:
                self._recover_corrupt(ValueError("invalid schema_version"))
                return
            if version > STORE_SCHEMA_VERSION:
                self._read_only = True
                self._load_error = f"future_schema: {version}"
                future_items = data.get("items", [])
                self._items = ([copy.deepcopy(item) for item in future_items
                                if isinstance(item, dict)]
                               if isinstance(future_items, list) else [])
                future_revision = data.get("revision", 0)
                self._revision = (future_revision if isinstance(future_revision, int)
                                  and not isinstance(future_revision, bool)
                                  and future_revision >= 0 else 0)
                future_current = data.get("current_item_id")
                self._current_item_id = future_current if isinstance(future_current, str) else None
                future_cache = data.get("received_cache", {})
                self._received_cache = (copy.deepcopy(future_cache)
                                        if isinstance(future_cache, dict) else {})
                future_leases = data.get("materialization_leases", {})
                self._materialization_leases = (copy.deepcopy(future_leases)
                                                 if isinstance(future_leases, dict) else {})
                future_sessions = data.get("transfer_sessions", {})
                self._transfer_sessions = (copy.deepcopy(future_sessions)
                                           if isinstance(future_sessions, dict) else {})
                future_tombstones = data.get("integrity_tombstones", {})
                self._integrity_tombstones = (copy.deepcopy(future_tombstones)
                                              if isinstance(future_tombstones, dict) else {})
                self._seq = max([item.get("seq", 0) for item in self._items
                                 if isinstance(item.get("seq", 0), int)
                                 and not isinstance(item.get("seq", 0), bool)] + [0])
                return

            revision = data.get("revision", 0)
            items = data.get("items", [])
            cache = data.get("received_cache", {})
            leases = data.get("materialization_leases", {})
            sessions = data.get("transfer_sessions", {})
            tombstones = data.get("integrity_tombstones", {})
            if (not isinstance(revision, int) or isinstance(revision, bool) or revision < 0
                    or not isinstance(items, list) or not isinstance(cache, dict)
                    or not isinstance(leases, dict) or not isinstance(sessions, dict)
                    or not isinstance(tombstones, dict)):
                self._recover_corrupt(ValueError("invalid clipboard index structure"))
                return

            known = {"schema_version", "revision", "current_item_id", "items",
                     "received_cache", "materialization_leases", "transfer_sessions",
                     "integrity_tombstones"}
            self._index_extra = {key: copy.deepcopy(value) for key, value in data.items()
                                 if key not in known}
            migrated = version < STORE_SCHEMA_VERSION
            loaded_tombstones = self._normalize_integrity_tombstones(tombstones)
            tombstoned_hashes = set(loaded_tombstones)
            tombstoned_hashes = self._expand_hash_references(
                tombstoned_hashes, items, cache)
            tombstoned_item_ids = {
                item.get("item_id") for item in items if isinstance(item, dict)
                and self._item_reference_hashes(item) & tombstoned_hashes
            }
            loaded_items = []
            loaded_sessions = {}
            try:
                for item in items:
                    if not isinstance(item, dict) or not item.get("item_id"):
                        raise ValueError("invalid clipboard index item")
                    local_sources = self._local_sources_available(item)
                    if item.get("kind") in (cm.KIND_FILE, cm.KIND_FILE_BATCH) and item.get("files"):
                        item = copy.deepcopy(item)
                        item["source_available"] = local_sources
                        if not local_sources and item.get("hash_state") in ("unhashed", "hashing"):
                            item["hash_state"] = "changed"
                    previous_state = item.get("payload_state")
                    if self._item_reference_hashes(item) & tombstoned_hashes:
                        item = copy.deepcopy(item)
                        state = "missing"
                        for provider in item.get("providers", []):
                            if isinstance(provider, dict):
                                provider["state"] = "unavailable"
                    elif local_sources:
                        state = "source_available"
                    elif (self.has_object(item.get("sha256", ""))
                          and (migrated
                               or previous_state in _OBJECT_DELIVERABLE_PAYLOAD_STATES)):
                        state = "cached"
                    elif previous_state in ("failed", "metadata_only", "receiving"):
                        state = previous_state
                    else:
                        state = "missing"
                    loaded_items.append(cm.version_item(item, payload_state=state))
            except (TypeError, ValueError, OSError) as exc:
                self._recover_corrupt(exc)
                return

            for transfer_id, snapshot in sessions.items():
                try:
                    session = ctt.TransferSession.from_snapshot(snapshot)
                    if transfer_id != session.transfer_id:
                        continue
                    if (session.item_id in tombstoned_item_ids
                            or self._session_reference_hashes(snapshot) & tombstoned_hashes):
                        continue
                    if session.state not in ctt.TERMINAL_TRANSFER_SESSION_STATES:
                        session.fail({
                            "code": "restart_without_resume_journal",
                            "message": "transfer interrupted by restart; durable resume is unavailable",
                            "retryable": True,
                        })
                    loaded_sessions[transfer_id] = session.snapshot()
                except (TypeError, ValueError):
                    continue
            loaded_sessions = dict(sorted(
                loaded_sessions.items(), key=lambda pair: pair[1]["updated_at"], reverse=True
            )[:MAX_TRANSFER_SESSION_SNAPSHOTS])

            if migrated:
                backup = schema_backup_path(self.index_path, version, STORE_SCHEMA_VERSION)
                if not os.path.exists(backup):
                    _atomic_write_bytes(backup, raw)
            self._items = loaded_items
            self._revision = revision
            self._seq = max([int(it.get("seq", 0) or 0) for it in self._items] + [0])
            item_ids = {item.get("item_id") for item in self._items}
            current = data.get("current_item_id")
            if migrated and current is None and self._items:
                current = max(self._items, key=lambda item: int(item.get("seq", 0) or 0))["item_id"]
            self._current_item_id = current if current in item_ids else None
            self._received_cache = {
                key: copy.deepcopy(entry) for key, entry in cache.items()
                if not (self._cache_reference_hashes(key, entry) & tombstoned_hashes)
            }
            self._materialization_leases = copy.deepcopy(leases)
            self._transfer_sessions = loaded_sessions
            self._integrity_tombstones = loaded_tombstones
            self._integrity_failures = tombstoned_hashes
            if migrated or self._document() != data:
                self._save()

    def _recover_corrupt(self, exc):
        self._load_error = f"corrupt_index: {exc}"
        self._items = []
        self._revision = 0
        self._seq = 0
        self._current_item_id = None
        self._received_cache = {}
        self._materialization_leases = {}
        self._transfer_sessions = {}
        self._integrity_tombstones = {}
        self._integrity_failures = set()
        self._index_extra = {}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        stem, ext = os.path.splitext(self.index_path)
        backup = f"{stem}.backup-corrupt-{stamp}{ext}"
        try:
            os.replace(self.index_path, backup)
            self._save()
        except OSError:
            self._read_only = True

    def _save(self):
        with self._lock:
            self._ensure_writable()
            _atomic_write_json(self.index_path, self._document())

    @staticmethod
    def _local_sources_available(item):
        return cfiles.local_sources_available(item)

    @staticmethod
    def _item_reference_hashes(item):
        if not isinstance(item, dict):
            return set()
        payload = item.get("payload")
        values = {item.get("sha256"), item.get("content_sha256")}
        if isinstance(payload, dict):
            values.update({payload.get("sha256"), payload.get("content_sha256")})
        return {value for value in values if cm.is_valid_sha256(value)}

    @staticmethod
    def _cache_reference_hashes(key, entry):
        values = {key}
        if isinstance(entry, dict):
            values.update({entry.get("content_sha256"), entry.get("object_sha256"),
                           entry.get("payload_sha256")})
        return {value for value in values if cm.is_valid_sha256(value)}

    @staticmethod
    def _session_reference_hashes(snapshot):
        if not isinstance(snapshot, dict):
            return set()
        progress = snapshot.get("progress")
        if not isinstance(progress, dict):
            return set()
        return {value for value in (progress.get("payload_sha256"),
                                     progress.get("content_sha256"),
                                     progress.get("object_sha256"))
                if cm.is_valid_sha256(value)}

    @classmethod
    def _expand_hash_references(cls, hashes, items, cache):
        expanded = set(hashes)
        changed = True
        while changed:
            changed = False
            for item in items:
                references = cls._item_reference_hashes(item)
                if references & expanded and not references <= expanded:
                    expanded.update(references)
                    changed = True
            for key, entry in cache.items():
                references = cls._cache_reference_hashes(key, entry)
                if references & expanded and not references <= expanded:
                    expanded.update(references)
                    changed = True
        return expanded

    @staticmethod
    def _normalize_integrity_tombstones(tombstones):
        normalized = []
        for object_sha256, metadata in tombstones.items():
            if not cm.is_valid_sha256(object_sha256) or not isinstance(metadata, dict):
                continue
            detected_at = metadata.get("detected_at")
            reason = metadata.get("reason")
            if (not isinstance(detected_at, (int, float)) or isinstance(detected_at, bool)
                    or detected_at < 0 or not isinstance(reason, str)
                    or not reason or len(reason) > 128):
                continue
            try:
                if len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) > 2048:
                    continue
            except (TypeError, ValueError):
                continue
            normalized.append((object_sha256, copy.deepcopy(metadata)))
        normalized.sort(key=lambda pair: pair[1]["detected_at"], reverse=True)
        return dict(normalized[:MAX_INTEGRITY_TOMBSTONES])

    def _item_tombstoned_locked(self, item):
        return bool(self._item_reference_hashes(item) & self._integrity_failures)

    def _reject_integrity_evidence_locked(self, affected_hashes, local_device_id=None):
        affected_item_ids = set()
        for item in self._items:
            if not (self._item_reference_hashes(item) & affected_hashes):
                continue
            affected_item_ids.add(item.get("item_id"))
            item["payload_state"] = "missing"
            item["available"] = False
            for provider in item.get("providers", []):
                if isinstance(provider, dict):
                    provider["state"] = "unavailable"

        removed_sessions = set()
        for transfer_id, session in list(self._transfer_sessions.items()):
            if (session.get("item_id") in affected_item_ids
                    or self._session_reference_hashes(session) & affected_hashes):
                removed_sessions.add(transfer_id)
                self._transfer_sessions.pop(transfer_id, None)
        for key, entry in list(self._received_cache.items()):
            if self._cache_reference_hashes(key, entry) & affected_hashes:
                self._received_cache.pop(key, None)
        return affected_item_ids, removed_sessions

    def _record_integrity_tombstones_locked(self, affected_hashes):
        detected_at = time.time()
        for object_sha256 in affected_hashes:
            self._integrity_tombstones[object_sha256] = {
                "detected_at": detected_at,
                "reason": "object_integrity_failure",
            }
        self._integrity_tombstones = self._normalize_integrity_tombstones(
            self._integrity_tombstones)
        self._integrity_failures.update(affected_hashes)

    def _document(self):
        document = copy.deepcopy(self._index_extra)
        document.update({
            "schema_version": STORE_SCHEMA_VERSION,
            "revision": self._revision,
            "current_item_id": self._current_item_id,
            "items": self._items,
            "received_cache": self._received_cache,
            "materialization_leases": self._materialization_leases,
            "transfer_sessions": self._transfer_sessions,
            "integrity_tombstones": self._integrity_tombstones,
        })
        return document

    def _ensure_writable(self):
        if self._read_only:
            raise RuntimeError(self._load_error or "clipboard store is read-only")

    def _snapshot_locked(self):
        return (copy.deepcopy(self._items), self._revision, self._seq,
                self._current_item_id, copy.deepcopy(self._received_cache),
                copy.deepcopy(self._materialization_leases),
                copy.deepcopy(self._transfer_sessions),
                copy.deepcopy(self._integrity_tombstones),
                set(self._integrity_failures))

    def _restore_locked(self, snapshot):
        (self._items, self._revision, self._seq, self._current_item_id,
         self._received_cache, self._materialization_leases,
         self._transfer_sessions, self._integrity_tombstones,
         self._integrity_failures) = snapshot

    # ── accessors ──────────────────────────────────────────────────
    @property
    def revision(self):
        with self._lock:
            return self._revision

    @property
    def current_item_id(self):
        with self._lock:
            return self._current_item_id

    @property
    def read_only(self):
        return self._read_only

    @property
    def load_error(self):
        return self._load_error

    @property
    def remote_revision(self):
        value = self._index_extra.get("remote_revision", -1)
        return value if isinstance(value, int) and not isinstance(value, bool) else -1

    def current_item(self):
        current = self.current_item_id
        return self.get_item(current) if current else None

    def list_items(self):
        with self._lock:
            return copy.deepcopy(self._items)

    def get_item(self, item_id):
        with self._lock:
            for it in self._items:
                if it.get("item_id") == item_id:
                    return copy.deepcopy(it)
        return None

    def known_hashes(self):
        with self._lock:
            return {item.get("sha256") for item in self._items
                    if item.get("sha256") and self._item_payload_available_locked(item)}

    def total_size(self):
        with self._lock:
            return sum(int(it.get("size", 0) or 0) for it in self._items)

    def _object_path(self, sha256):
        if not cm.is_valid_sha256(sha256):
            raise ValueError("invalid clipboard object sha256")
        return os.path.join(self.objects_dir, sha256)

    def object_path(self, sha256):
        return self._object_path(sha256)

    def get_object_path_for_item(self, item_id):
        with self._lock:
            it = next((item for item in self._items
                       if item.get("item_id") == item_id), None)
            if (not it or it.get("payload_state") not in _OBJECT_DELIVERABLE_PAYLOAD_STATES
                    or self._item_tombstoned_locked(it)):
                return None
            path = self._object_path(it.get("sha256", ""))
            return path if os.path.exists(path) else None

    def get_data(self, item_id):
        with self._lock:
            it = next((item for item in self._items
                       if item.get("item_id") == item_id), None)
            if (not it or it.get("payload_state") not in _OBJECT_DELIVERABLE_PAYLOAD_STATES
                    or self._item_tombstoned_locked(it)):
                return None
            path = self._object_path(it["sha256"])
            if not os.path.exists(path):
                return None
            try:
                with open(path, "rb") as f:
                    return f.read()
            except OSError:
                return None

    def has_object(self, sha256):
        return cm.is_valid_sha256(sha256) and os.path.exists(self._object_path(sha256))

    def _item_payload_available_locked(self, item):
        if self._item_tombstoned_locked(item):
            return False
        if self._local_sources_available(item):
            return True
        return (item.get("payload_state") in _OBJECT_DELIVERABLE_PAYLOAD_STATES
                and item.get("sha256") not in self._integrity_failures
                and self.has_object(item.get("sha256", "")))

    def has_committed_object(self, sha256):
        """Return whether a physical object has persisted deliverability evidence."""
        with self._lock:
            if sha256 in self._integrity_failures or not self.has_object(sha256):
                return False
            if any(item.get("sha256") == sha256
                   and item.get("payload_state") in _OBJECT_DELIVERABLE_PAYLOAD_STATES
                   for item in self._items):
                return True
            return any(
                key == sha256 or (isinstance(entry, dict) and sha256 in {
                    entry.get("content_sha256"), entry.get("object_sha256"),
                    entry.get("payload_sha256"),
                })
                for key, entry in self._received_cache.items()
            )

    @staticmethod
    def _file_matches_payload(path, expected_size, expected_sha256):
        if (not isinstance(expected_size, int) or isinstance(expected_size, bool)
                or expected_size < 0 or not cm.is_valid_sha256(expected_sha256)):
            return False
        digest = hashlib.sha256()
        total = 0
        try:
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    digest.update(chunk)
        except OSError:
            return False
        return total == expected_size and digest.hexdigest() == expected_sha256.lower()

    def verify_object(self, object_sha256, expected_size, expected_sha256,
                      local_device_id=None):
        """Verify a physical object while coordinated with store mutations."""
        with self._lock:
            try:
                path = self._object_path(object_sha256)
            except ValueError:
                return False
            verified = self._file_matches_payload(path, expected_size, expected_sha256)
            if verified:
                if object_sha256 in self._integrity_tombstones:
                    return False
                self._integrity_failures.discard(object_sha256)
            else:
                self._integrity_failures.add(object_sha256)
                for item in self._items:
                    payload = item.get("payload") or {}
                    if object_sha256 not in {item.get("sha256"), payload.get("sha256")}:
                        continue
                    item["payload_state"] = "failed"
                    item["available"] = False
                    for provider in item.get("providers", []):
                        if local_device_id and provider.get("device_id") == local_device_id:
                            provider["state"] = "unavailable"
            return verified

    def _install_verified_object_locked(self, object_sha256, expected_size,
                                        expected_sha256, *, data=None,
                                        source_path=None):
        path = self._object_path(object_sha256)
        if source_path is not None:
            source_path = os.path.abspath(source_path)
            if source_path == os.path.abspath(path):
                raise OSError("corrupt clipboard object has no independent replacement source")
        os.makedirs(self.objects_dir, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{object_sha256}.", suffix=".tmp", dir=self.objects_dir)
        digest = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(fd, "wb") as destination:
                if data is not None:
                    view = memoryview(data)
                    for offset in range(0, len(view), 1024 * 1024):
                        chunk = view[offset:offset + 1024 * 1024]
                        destination.write(chunk)
                        digest.update(chunk)
                        total += len(chunk)
                else:
                    with open(source_path, "rb") as source:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            destination.write(chunk)
                            digest.update(chunk)
                            total += len(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            if total != expected_size or digest.hexdigest() != expected_sha256.lower():
                raise ValueError("received clipboard object size or hash mismatch")
            os.replace(temporary, path)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise
        if source_path is not None:
            try:
                os.remove(source_path)
            except OSError:
                pass
        return path

    def invalidate_completed_receipt(self, transfer_id, item_id, object_sha256,
                                     local_device_id=None):
        """Quarantine an invalid object and remove persisted deliverability evidence."""
        with self._lock:
            self._ensure_writable()
            path = self._object_path(object_sha256)
            snapshot = self._snapshot_locked()
            try:
                affected_hashes = {object_sha256}
                affected_hashes = self._expand_hash_references(
                    affected_hashes, self._items, self._received_cache)
                affected_item_ids, removed_sessions = self._reject_integrity_evidence_locked(
                    affected_hashes, local_device_id=local_device_id)
                if transfer_id in self._transfer_sessions:
                    removed_sessions.add(transfer_id)
                    self._transfer_sessions.pop(transfer_id, None)
                self._record_integrity_tombstones_locked(affected_hashes)
                self._revision += 1
                self._save()
            except Exception:
                self._restore_locked(snapshot)
                raise

            quarantine_path = None
            if os.path.exists(path):
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                quarantine_path = f"{path}.corrupt-{stamp}"
                os.replace(path, quarantine_path)
            return {
                "affected_item_ids": affected_item_ids,
                "removed_transfer_ids": removed_sessions,
                "affected_hashes": affected_hashes,
                "quarantine_path": quarantine_path,
            }

    # ── mutation ───────────────────────────────────────────────────
    def _next_seq(self):
        self._seq += 1
        return self._seq

    def write_object(self, sha256, data):
        """Store a blob content-addressed (dedup: skip if it already exists)."""
        path = self._object_path(sha256)
        if os.path.exists(path):
            return path
        tmp = path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return path
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def write_object_from_file(self, sha256, source_path, move=False):
        """Store a blob from a file path (dedup-safe, atomic where possible)."""
        path = self._object_path(sha256)
        source_path = os.path.abspath(source_path)
        if os.path.abspath(path) == source_path and os.path.exists(path):
            return path
        if os.path.exists(path):
            if move and os.path.exists(source_path) and os.path.abspath(source_path) != os.path.abspath(path):
                try:
                    os.remove(source_path)
                except OSError:
                    pass
            return path

        tmp = path + ".tmp"
        os.makedirs(self.objects_dir, exist_ok=True)
        try:
            if move:
                try:
                    os.replace(source_path, path)
                    return path
                except OSError:
                    pass
            with open(source_path, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            os.replace(tmp, path)
            if move:
                try:
                    os.remove(source_path)
                except OSError:
                    pass
            return path
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _add_item_locked(self, item, data=None, enforce=None, make_current=False,
                         replace_existing=False, received_cache=None,
                         publish_verified_object=False):
        it = cm.version_item(item)
        existing_index = next((index for index, existing in enumerate(self._items)
                               if existing.get("item_id") == it["item_id"]), None)
        if existing_index is not None and not replace_existing:
            raise ValueError("clipboard item_id already exists")
        if existing_index is not None:
            existing = self._items[existing_index]
            if not cm.same_item_lineage(existing, it):
                raise ValueError("clipboard item identity or revision conflict")
            it["seq"] = existing.get("seq", 0)
            it["pinned"] = existing.get("pinned", False)
        else:
            it["seq"] = self._next_seq()
        if data is not None and it.get("sha256"):
            self.write_object(it["sha256"], data)
            if it.get("payload_state") == "metadata_only":
                it["payload_state"] = "cached"
            it["available"] = True
        else:
            if (publish_verified_object
                    or self.has_committed_object(it.get("sha256", ""))):
                if it.get("payload_state") in ("metadata_only", "missing"):
                    it["payload_state"] = "cached"
                it["available"] = True
            else:
                it["available"] = it.get("payload_state") in (
                    "source_available", "materialized")
        if self._item_tombstoned_locked(it):
            it["payload_state"] = "missing"
            it["available"] = False
            for provider in it.get("providers", []):
                if isinstance(provider, dict):
                    provider["state"] = "unavailable"
        if existing_index is None:
            self._items.append(it)
        else:
            self._items[existing_index] = it
        if make_current:
            self._current_item_id = it["item_id"]
        if received_cache is not None:
            self._record_cache_entry_locked(*received_cache)
        self._revision += 1
        evicted = self._enforce_locked(*enforce) if enforce else []
        return it, evicted

    def _remove_new_unreferenced_object_locked(self, path, sha256):
        if (path is None or any(
                item.get("sha256") == sha256
                and item.get("payload_state") in _OBJECT_DELIVERABLE_PAYLOAD_STATES
                for item in self._items) or sha256 in self._cache_object_hashes()):
            return
        try:
            os.remove(path)
        except OSError:
            pass

    def add_item(self, item, data=None, enforce=None, make_current=False,
                  replace_existing=False, received_cache=None):
        """Add an item (optionally with its blob). Returns the stored item.

        ``enforce`` may be ``(max_items, max_total_bytes)`` to run eviction after.
        Dedup: if an item with the same sha256 already exists, its content is not
        re-written; a new history entry is still appended (same content, new copy
        event) unless it is the exact same trailing entry.
        """
        with self._lock:
            self._ensure_writable()
            snapshot = self._snapshot_locked()
            object_sha = cm.version_item(item).get("sha256", "")
            new_object_path = (self._object_path(object_sha)
                               if data is not None and object_sha
                               and not self.has_object(object_sha) else None)
            try:
                it, evicted = self._add_item_locked(
                    item, data=data, enforce=enforce, make_current=make_current,
                    replace_existing=replace_existing, received_cache=received_cache,
                    publish_verified_object=data is not None)
                self._save()
                self._cleanup_unreferenced_objects()
                return copy.deepcopy(it), evicted
            except Exception:
                self._restore_locked(snapshot)
                self._remove_new_unreferenced_object_locked(new_object_path, object_sha)
                raise

    def commit_received_item(self, item, completed_session, *, data=None,
                             object_source_path=None, enforce=None, make_current=False,
                             replace_existing=False, received_cache=None):
        """Atomically publish a received item and its completed receipt session."""
        if data is not None and object_source_path is not None:
            raise ValueError("received item has multiple object sources")
        session = ctt.TransferSession.from_snapshot(completed_session)
        if (session.profile != self.profile_id
                or session.direction not in ("receive", "incoming")
                or session.state != ctt.TransferSessionState.completed
                or session.item_id != item.get("item_id")
                or session.item_revision != cm.item_revision(item)):
            raise ValueError("invalid completed receive session")
        payload = item.get("payload") or {}
        if (session.progress.get("payload_sha256") != payload.get("sha256")
                or session.logical_bytes != payload.get("size")):
            raise ValueError("completed receive evidence does not match item payload")

        with self._lock:
            self._ensure_writable()
            snapshot = self._snapshot_locked()
            object_sha = item.get("sha256", "")
            expected_sha = payload.get("sha256", "")
            expected_size = payload.get("size")
            supplies_object = data is not None or object_source_path is not None
            object_existed = self.has_object(object_sha)
            object_verified = self._file_matches_payload(
                self._object_path(object_sha), expected_size, expected_sha)
            if object_sha in self._integrity_tombstones:
                object_verified = False
            if object_existed and not object_verified:
                self._integrity_failures.add(object_sha)
            new_object_path = (self._object_path(object_sha)
                               if supplies_object and not object_existed else None)
            try:
                if not object_verified:
                    if not supplies_object:
                        raise ValueError("received clipboard object is missing or corrupt")
                    self._install_verified_object_locked(
                        object_sha, expected_size, expected_sha,
                        data=data, source_path=object_source_path)
                elif object_source_path is not None:
                    try:
                        os.remove(object_source_path)
                    except OSError:
                        pass
                repaired_hashes = self._item_reference_hashes(item)
                for repaired_hash in repaired_hashes:
                    self._integrity_tombstones.pop(repaired_hash, None)
                    self._integrity_failures.discard(repaired_hash)
                it, evicted = self._add_item_locked(
                    item, data=None, enforce=enforce, make_current=make_current,
                    replace_existing=replace_existing, received_cache=received_cache,
                    publish_verified_object=True)
                if not any(existing.get("item_id") == session.item_id
                           for existing in self._items):
                    raise ValueError("completed received item was evicted before commit")
                self._transfer_sessions[session.transfer_id] = session.snapshot()
                ordered = sorted(self._transfer_sessions.items(),
                                 key=lambda pair: pair[1]["updated_at"], reverse=True)
                self._transfer_sessions = dict(ordered[:MAX_TRANSFER_SESSION_SNAPSHOTS])
                if session.transfer_id not in self._transfer_sessions:
                    raise ValueError("completed receive session was not retained")
                self._save()
                self._cleanup_unreferenced_objects()
                return copy.deepcopy(it), evicted
            except Exception:
                self._restore_locked(snapshot)
                self._remove_new_unreferenced_object_locked(new_object_path, object_sha)
                raise

    def set_current(self, item_id):
        with self._lock:
            self._ensure_writable()
            if item_id is not None and not any(
                    item.get("item_id") == item_id for item in self._items):
                return False
            if self._current_item_id == item_id:
                return True
            snapshot = self._snapshot_locked()
            try:
                self._current_item_id = item_id
                self._revision += 1
                self._save()
                return True
            except BaseException:
                self._restore_locked(snapshot)
                raise

    def reset_current(self):
        with self._lock:
            self._ensure_writable()
            if self._current_item_id is None:
                return True
            snapshot = self._snapshot_locked()
            try:
                self._current_item_id = None
                self._revision += 1
                self._save()
                return True
            except BaseException:
                self._restore_locked(snapshot)
                raise

    def track_remote_revision(self, revision):
        """Record the peer's history revision to detect stale updates.

        Does NOT set local current_item_id — remote announcements/manifests
        must never overwrite what the local device knows is on its clipboard.
        """
        with self._lock:
            self._ensure_writable()
            if revision <= self.remote_revision:
                return False
            snapshot = self._snapshot_locked()
            previous_remote = self._index_extra.get("remote_revision")
            try:
                self._index_extra["remote_revision"] = int(revision)
                self._revision += 1
                self._save()
                return True
            except BaseException:
                self._restore_locked(snapshot)
                if previous_remote is None:
                    self._index_extra.pop("remote_revision", None)
                else:
                    self._index_extra["remote_revision"] = previous_remote
                raise

    def mark_available(self, item_id, available=True):
        with self._lock:
            self._ensure_writable()
            for it in self._items:
                if it.get("item_id") == item_id:
                    if available and self._item_tombstoned_locked(it):
                        return False
                    snapshot = self._snapshot_locked()
                    try:
                        it["available"] = bool(available)
                        it["payload_state"] = "cached" if available else "missing"
                        self._revision += 1
                        self._save()
                        return True
                    except BaseException:
                        self._restore_locked(snapshot)
                        raise
        return False

    def set_pinned(self, item_id, pinned):
        with self._lock:
            self._ensure_writable()
            for it in self._items:
                if it.get("item_id") == item_id:
                    snapshot = self._snapshot_locked()
                    try:
                        it["pinned"] = bool(pinned)
                        self._save()
                        return True
                    except BaseException:
                        self._restore_locked(snapshot)
                        raise
        return False

    def _sha_refcount(self, sha256, exclude_id=None):
        return sum(1 for it in self._items
                   if it.get("sha256") == sha256 and it.get("item_id") != exclude_id)

    def delete_item(self, item_id):
        with self._lock:
            self._ensure_writable()
            target = next((it for it in self._items if it.get("item_id") == item_id), None)
            has_sessions = any(snapshot.get("item_id") == item_id
                               for snapshot in self._transfer_sessions.values())
            if not target and not has_sessions:
                return False
            snapshot = self._snapshot_locked()
            try:
                if target:
                    self._items = [it for it in self._items if it.get("item_id") != item_id]
                self._remove_transfer_sessions_for_item_locked(item_id)
                if self._current_item_id == item_id:
                    self._current_item_id = None
                self._revision += 1
                self._save()
            except BaseException:
                self._restore_locked(snapshot)
                raise
            if target:
                self._cleanup_item_files(target)
            return bool(target)

    def clear(self):
        with self._lock:
            self._ensure_writable()
            snapshot = self._snapshot_locked()
            self._items = []
            self._current_item_id = None
            self._received_cache = {}
            self._materialization_leases = {}
            self._transfer_sessions = {}
            self._integrity_tombstones = {}
            self._integrity_failures = set()
            self._revision += 1
            try:
                self._save()
            except BaseException:
                self._restore_locked(snapshot)
                raise
            for d in (self.objects_dir, self.previews_dir):
                try:
                    shutil.rmtree(d, ignore_errors=True)
                    os.makedirs(d, exist_ok=True)
                except OSError:
                    pass
            return True

    # ── transfer session status ────────────────────────────────────
    def save_transfer_session(self, snapshot):
        session = ctt.TransferSession.from_snapshot(snapshot)
        if session.profile != self.profile_id:
            raise ValueError("transfer session profile does not match store")
        with self._lock:
            self._ensure_writable()
            if session.state == ctt.TransferSessionState.completed:
                item = next((candidate for candidate in self._items
                             if candidate.get("item_id") == session.item_id), None)
                if ((item is not None and self._item_tombstoned_locked(item))
                        or self._session_reference_hashes(snapshot)
                        & self._integrity_failures):
                    raise ValueError("completed transfer references tombstoned object")
            previous = copy.deepcopy(self._transfer_sessions)
            try:
                self._transfer_sessions[session.transfer_id] = session.snapshot()
                ordered = sorted(self._transfer_sessions.items(),
                                 key=lambda pair: pair[1]["updated_at"], reverse=True)
                self._transfer_sessions = dict(ordered[:MAX_TRANSFER_SESSION_SNAPSHOTS])
                self._save()
                return copy.deepcopy(self._transfer_sessions.get(session.transfer_id))
            except BaseException:
                self._transfer_sessions = previous
                raise

    def transfer_sessions_snapshot(self):
        with self._lock:
            return copy.deepcopy(self._transfer_sessions)

    def _remove_transfer_sessions_for_item_locked(self, item_id):
        removed = [transfer_id for transfer_id, snapshot in self._transfer_sessions.items()
                   if snapshot.get("item_id") == item_id]
        for transfer_id in removed:
            self._transfer_sessions.pop(transfer_id, None)
        return removed

    def _enforce_locked(self, max_items, max_total_bytes):
        plan = cm.eviction_plan(self._items, max_items, max_total_bytes)
        if self._current_item_id and self._current_item_id in plan:
            plan = [iid for iid in plan if iid != self._current_item_id]
        for iid in plan:
            self._delete_locked(iid)
        return plan

    def _delete_locked(self, item_id):
        target = next((it for it in self._items if it.get("item_id") == item_id), None)
        if not target:
            return
        self._items = [it for it in self._items if it.get("item_id") != item_id]
        self._remove_transfer_sessions_for_item_locked(item_id)
        if self._current_item_id == item_id:
            self._current_item_id = None

    def enforce_limits(self, max_items, max_total_bytes):
        with self._lock:
            self._ensure_writable()
            snapshot = self._snapshot_locked()
            try:
                evicted = self._enforce_locked(max_items, max_total_bytes)
                if evicted:
                    self._revision += 1
                    self._save()
                    self._cleanup_unreferenced_objects()
                return evicted
            except BaseException:
                self._restore_locked(snapshot)
                raise

    def build_manifest(self, device_id):
        with self._lock:
            items = copy.deepcopy(self._items)
            for item in items:
                local_available = self._item_payload_available_locked(item)
                if not local_available and item.get("payload_state") in (
                        "cached", "materialized"):
                    item["payload_state"] = "missing"
                item["available"] = local_available
                for provider in item.get("providers", []):
                    if provider.get("device_id") == device_id:
                        provider["state"] = "available" if local_available else "unavailable"
                        provider["last_seen_at"] = time.time()
                if local_available and device_id and not any(
                        provider.get("device_id") == device_id for provider in item.get("providers", [])):
                    payload = item.get("payload") or {}
                    provider = {"device_id": device_id, "state": "available",
                                "last_seen_at": time.time()}
                    if payload.get("sha256"):
                        provider["payload_sha256"] = payload["sha256"]
                    if payload.get("size") is not None:
                        provider["payload_size"] = payload["size"]
                    item.setdefault("providers", []).append(provider)
            return cm.build_manifest(self.profile_id, device_id, self._revision, items,
                                     current_item_id=self._current_item_id)

    def _cleanup_item_files(self, target):
        sha = target.get("sha256")
        if sha and self._sha_refcount(sha) == 0 and sha not in self._cache_object_hashes():
            try:
                os.remove(self._object_path(sha))
            except OSError:
                pass
        prev = os.path.join(self.previews_dir, f"{target.get('item_id')}.png")
        try:
            os.remove(prev)
        except OSError:
            pass

    def _cleanup_unreferenced_objects(self):
        referenced = ({item.get("sha256") for item in self._items
                       if item.get("sha256")
                       and item.get("payload_state") in _OBJECT_DELIVERABLE_PAYLOAD_STATES}
                      | self._cache_object_hashes())
        try:
            names = os.listdir(self.objects_dir)
        except OSError:
            return
        for name in names:
            if (name not in referenced and not name.endswith(".tmp")
                    and ".corrupt-" not in name):
                try:
                    os.remove(os.path.join(self.objects_dir, name))
                except OSError:
                    pass

    def _cache_object_hashes(self):
        hashes = set()
        for key, entry in self._received_cache.items():
            if cm.is_valid_sha256(key):
                hashes.add(key)
            if isinstance(entry, dict):
                for field in ("object_sha256", "content_sha256"):
                    value = entry.get(field)
                    if cm.is_valid_sha256(value):
                        hashes.add(value)
        return hashes

    # ── received cache ─────────────────────────────────────────────
    def _record_cache_entry_locked(self, content_sha256, payload_sha256=None,
                                   payload_size=None, providers=None):
        entry = cm.make_cache_entry(content_sha256, payload_sha256, payload_size,
                                    providers=providers)
        if self._cache_reference_hashes(content_sha256, entry) & self._integrity_failures:
            raise ValueError("cache entry references tombstoned object")
        existing = self._received_cache.get(content_sha256)
        if existing:
            entry["received_at"] = existing.get("received_at", entry["received_at"])
            merged = {provider.get("device_id"): provider
                      for provider in existing.get("providers", [])
                      if isinstance(provider, dict) and provider.get("device_id")}
            for provider in entry.get("providers", []):
                if provider.get("device_id"):
                    merged[provider["device_id"]] = provider
            if merged:
                entry["providers"] = list(merged.values())
        self._received_cache[content_sha256] = entry
        return entry

    def record_cache_entry(self, content_sha256, payload_sha256=None, payload_size=None,
                           providers=None):
        with self._lock:
            self._ensure_writable()
            snapshot = self._snapshot_locked()
            try:
                entry = self._record_cache_entry_locked(
                    content_sha256, payload_sha256, payload_size, providers)
                self._revision += 1
                self._save()
                return entry
            except BaseException:
                self._restore_locked(snapshot)
                raise

    def access_cache_entry(self, content_sha256):
        with self._lock:
            entry = self._received_cache.get(content_sha256)
            if not entry:
                return False
            entry["last_access"] = time.time()
            self._revision += 1
            self._save()
            return True

    def get_cache_entry(self, content_sha256):
        with self._lock:
            entry = self._received_cache.get(content_sha256)
            return copy.deepcopy(entry) if entry else None

    def cache_entries_snapshot(self):
        with self._lock:
            return copy.deepcopy(self._received_cache)

    def remove_cache_entry(self, content_sha256):
        with self._lock:
            self._ensure_writable()
            entry = self._received_cache.pop(content_sha256, None)
            if entry is None:
                return False
            self._revision += 1
            self._save()
            self._cleanup_unreferenced_objects()
            return True

    def cache_protected_hashes(self, extra_protected=None):
        with self._lock:
            protected = set()
            for item in self._items:
                sha = item.get("sha256")
                if not sha or not cm.is_valid_sha256(sha):
                    continue
                if item.get("pinned") or item.get("item_id") == self._current_item_id:
                    protected.add(sha)
            if extra_protected:
                for h in extra_protected:
                    if cm.is_valid_sha256(h):
                        protected.add(h)
            return protected

    def remove_ghost_cache_entries(self):
        """Remove cache entries whose content_sha256 matches no item in the store."""
        with self._lock:
            item_hashes = {item.get("sha256") for item in self._items
                           if item.get("sha256") and cm.is_valid_sha256(item["sha256"])}
            ghost = {key: self._received_cache[key] for key in list(self._received_cache)
                     if key not in item_hashes}
            if not ghost:
                return {}
            for key in ghost:
                self._received_cache.pop(key, None)
            self._revision += 1
            self._save()
            self._cleanup_unreferenced_objects()
            return ghost

    def evict_cache(self, protected_hashes=None, target_unique_bytes=None):
        with self._lock:
            self._ensure_writable()
            protected = self.cache_protected_hashes(extra_protected=protected_hashes)
            evictable = cm.evictable_cache_entries(self._received_cache, protected)
            evicted = {}
            for key, entry in evictable:
                entry_size = entry.get("payload_size") or 0
                if target_unique_bytes is not None and target_unique_bytes <= 0:
                    break
                removed = self._received_cache.pop(key, None)
                if removed:
                    evicted[key] = removed
                    if target_unique_bytes is not None:
                        target_unique_bytes -= entry_size
            if evicted:
                self._revision += 1
                self._save()
                self._cleanup_unreferenced_objects()
            return evicted

    def cache_snapshot(self):
        with self._lock:
            entries = copy.deepcopy(self._received_cache)
            unique_bytes = sum(
                e.get("payload_size", 0) or 0 for e in entries.values())
            protected = self.cache_protected_hashes()
            protected_bytes = sum(
                e.get("payload_size", 0) or 0
                for k, e in entries.items() if k in protected)
            return {
                "entry_count": len(entries),
                "unique_bytes": unique_bytes,
                "protected_count": len(protected),
                "protected_bytes": protected_bytes,
                "eviction_count": sum(
                    1 for e in entries.values()
                    if e.get("last_access", 0) > e.get("received_at", 0)),
            }

    # ── materialization leases ──────────────────────────────────────
    def set_lease(self, item_id, dest_path):
        with self._lock:
            self._ensure_writable()
            snapshot = self._snapshot_locked()
            try:
                lease = cm.make_lease(self.profile_id, item_id, dest_path)
                existing = self._materialization_leases.get(item_id)
                if existing:
                    lease["created_at"] = existing.get("created_at", lease["created_at"])
                self._materialization_leases[item_id] = lease
                self._revision += 1
                self._save()
                return lease
            except BaseException:
                self._restore_locked(snapshot)
                raise

    def get_lease(self, item_id):
        with self._lock:
            entry = self._materialization_leases.get(item_id)
            return copy.deepcopy(entry) if entry else None

    def bind_lease_sequence(self, item_id, owner_sequence):
        with self._lock:
            lease = self._materialization_leases.get(item_id)
            if not lease:
                return False
            if lease.get("state") != cm.LEASE_ACTIVE:
                return False
            snapshot = self._snapshot_locked()
            try:
                lease["owner_sequence"] = int(owner_sequence)
                lease["last_access"] = time.time()
                self._revision += 1
                self._save()
                return True
            except BaseException:
                self._restore_locked(snapshot)
                raise

    def release_lease(self, item_id):
        with self._lock:
            lease = self._materialization_leases.pop(item_id, None)
            if not lease:
                return False
            if lease.get("dest_path"):
                try:
                    csrc.cleanup_temp_tree(lease["dest_path"], max_age_hours=0)
                except Exception:
                    pass
            self._revision += 1
            self._save()
            return True

    def release_leases_for_item(self, item_id):
        with self._lock:
            released = []
            for key in list(self._materialization_leases.keys()):
                lease = self._materialization_leases.get(key)
                if lease and lease.get("item_id") == item_id:
                    self._materialization_leases.pop(key)
                    if lease.get("dest_path"):
                        try:
                            csrc.cleanup_temp_tree(lease["dest_path"], max_age_hours=0)
                        except Exception:
                            pass
                    released.append(key)
            if released:
                self._revision += 1
                self._save()
            return released

    def active_lease_hashes(self):
        with self._lock:
            hashes = set()
            for lease in self._materialization_leases.values():
                if lease.get("state") == cm.LEASE_ACTIVE:
                    item_id = lease.get("item_id")
                    if item_id:
                        for item in self._items:
                            if item.get("item_id") == item_id and item.get("sha256"):
                                hashes.add(item["sha256"])
            return hashes

    def release_stale_leases(self, current_sequence=None):
        with self._lock:
            released = []
            for key in list(self._materialization_leases.keys()):
                lease = self._materialization_leases.get(key)
                if not lease:
                    continue
                state = lease.get("state")
                seq = lease.get("owner_sequence")
                if state == cm.LEASE_STALE or state == cm.LEASE_RELEASED:
                    self._materialization_leases.pop(key)
                    released.append(key)
                elif current_sequence is not None and seq is not None and seq != current_sequence:
                    self._materialization_leases.pop(key)
                    released.append(key)
            if released:
                self._revision += 1
                self._save()
            return released

    def cleanup_leases(self, max_age_hours=None):
        with self._lock:
            cutoff = cm.lease_stale_cutoff(max_age_hours)
            removed = []
            for key in list(self._materialization_leases.keys()):
                lease = self._materialization_leases.get(key)
                if not lease:
                    continue
                state = lease.get("state")
                last_access = lease.get("last_access", 0)
                if state != cm.LEASE_ACTIVE and last_access < cutoff:
                    self._materialization_leases.pop(key)
                    if lease.get("dest_path"):
                        try:
                            csrc.cleanup_temp_tree(lease["dest_path"], max_age_hours=0)
                        except Exception:
                            pass
                    removed.append(key)
            if removed:
                self._revision += 1
                self._save()
            return removed

    def lease_snapshot(self):
        with self._lock:
            active = sum(1 for lease in self._materialization_leases.values()
                         if lease.get("state") == cm.LEASE_ACTIVE)
            released = sum(1 for lease in self._materialization_leases.values()
                           if lease.get("state") in (cm.LEASE_RELEASED, cm.LEASE_STALE))
            return {
                "total": len(self._materialization_leases),
                "active": active,
                "released": released,
            }

    def cleanup_temp(self, max_age_hours=None):
        try:
            csrc.cleanup_temp_tree(self.temp_dir, max_age_hours=max_age_hours)
            incoming_dir = os.path.join(self.root, "temp", "incoming")
            csrc.cleanup_temp_tree(incoming_dir, max_age_hours=max_age_hours)
        except OSError:
            pass


def profile_dir_name(identity):
    """Filesystem-safe directory name for a profile/peer identity."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(identity))
    return safe or "unknown"
