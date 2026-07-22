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
import clipboard_sources as csrc


STORE_SCHEMA_VERSION = 1


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
                self._seq = max([item.get("seq", 0) for item in self._items
                                 if isinstance(item.get("seq", 0), int)
                                 and not isinstance(item.get("seq", 0), bool)] + [0])
                return

            revision = data.get("revision", 0)
            items = data.get("items", [])
            cache = data.get("received_cache", {})
            leases = data.get("materialization_leases", {})
            if (not isinstance(revision, int) or isinstance(revision, bool) or revision < 0
                    or not isinstance(items, list) or not isinstance(cache, dict)
                    or not isinstance(leases, dict)):
                self._recover_corrupt(ValueError("invalid clipboard index structure"))
                return

            known = {"schema_version", "revision", "current_item_id", "items",
                     "received_cache", "materialization_leases"}
            self._index_extra = {key: copy.deepcopy(value) for key, value in data.items()
                                 if key not in known}
            migrated = version < STORE_SCHEMA_VERSION
            loaded_items = []
            try:
                for item in items:
                    if not isinstance(item, dict) or not item.get("item_id"):
                        raise ValueError("invalid clipboard index item")
                    local_sources = self._local_sources_available(item)
                    object_available = self.has_object(item.get("sha256", ""))
                    previous_state = item.get("payload_state")
                    if local_sources:
                        state = "source_available"
                    elif object_available:
                        state = "cached"
                    elif previous_state in ("failed", "metadata_only"):
                        state = previous_state
                    else:
                        state = "missing"
                    loaded_items.append(cm.version_item(item, payload_state=state))
            except (TypeError, ValueError, OSError) as exc:
                self._recover_corrupt(exc)
                return

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
            self._received_cache = copy.deepcopy(cache)
            self._materialization_leases = copy.deepcopy(leases)
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
        files = item.get("files")
        if not isinstance(files, list) or not files:
            return False
        for entry in files:
            if not isinstance(entry, dict) or not entry.get("abspath"):
                return False
            path = entry["abspath"]
            expected_size = entry.get("size")
            expected_sha = entry.get("sha256")
            try:
                if (not os.path.isfile(path) or os.path.getsize(path) != expected_size
                        or not isinstance(expected_sha, str) or len(expected_sha) != 64):
                    return False
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                if digest.hexdigest() != expected_sha.lower():
                    return False
            except OSError:
                return False
        return True

    def _document(self):
        document = copy.deepcopy(self._index_extra)
        document.update({
            "schema_version": STORE_SCHEMA_VERSION,
            "revision": self._revision,
            "current_item_id": self._current_item_id,
            "items": self._items,
            "received_cache": self._received_cache,
            "materialization_leases": self._materialization_leases,
        })
        return document

    def _ensure_writable(self):
        if self._read_only:
            raise RuntimeError(self._load_error or "clipboard store is read-only")

    def _snapshot_locked(self):
        return (copy.deepcopy(self._items), self._revision, self._seq,
                self._current_item_id, copy.deepcopy(self._received_cache),
                copy.deepcopy(self._materialization_leases))

    def _restore_locked(self, snapshot):
        (self._items, self._revision, self._seq, self._current_item_id,
         self._received_cache, self._materialization_leases) = snapshot

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
                    if item.get("sha256") and item.get("available")
                    and (self.has_object(item.get("sha256"))
                         or self._local_sources_available(item))}

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
        it = self.get_item(item_id)
        if not it:
            return None
        path = self._object_path(it.get("sha256", ""))
        return path if os.path.exists(path) else None

    def get_data(self, item_id):
        it = self.get_item(item_id)
        if not it:
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

    def add_item(self, item, data=None, enforce=None, make_current=False,
                 replace_existing=False):
        """Add an item (optionally with its blob). Returns the stored item.

        ``enforce`` may be ``(max_items, max_total_bytes)`` to run eviction after.
        Dedup: if an item with the same sha256 already exists, its content is not
        re-written; a new history entry is still appended (same content, new copy
        event) unless it is the exact same trailing entry.
        """
        with self._lock:
            self._ensure_writable()
            snapshot = self._snapshot_locked()
            new_object_path = None
            try:
                it = cm.version_item(item)
                existing_index = next((index for index, existing in enumerate(self._items)
                                       if existing.get("item_id") == it["item_id"]), None)
                if existing_index is not None and not replace_existing:
                    raise ValueError("clipboard item_id already exists")
                if existing_index is not None:
                    existing = self._items[existing_index]
                    it["seq"] = existing.get("seq", 0)
                    it["pinned"] = existing.get("pinned", False)
                else:
                    it["seq"] = self._next_seq()
                if data is not None and it.get("sha256"):
                    object_existed = self.has_object(it["sha256"])
                    self.write_object(it["sha256"], data)
                    if not object_existed:
                        new_object_path = self._object_path(it["sha256"])
                    if it.get("payload_state") == "metadata_only":
                        it["payload_state"] = "cached"
                    it["available"] = True
                else:
                    if self.has_object(it.get("sha256", "")):
                        if it.get("payload_state") in ("metadata_only", "missing"):
                            it["payload_state"] = "cached"
                        it["available"] = True
                    else:
                        it["available"] = it.get("payload_state") in (
                            "source_available", "materialized")
                if existing_index is None:
                    self._items.append(it)
                else:
                    self._items[existing_index] = it
                if make_current:
                    self._current_item_id = it["item_id"]
                self._revision += 1
                evicted = []
                if enforce:
                    evicted = self._enforce_locked(*enforce)
                self._save()
                self._cleanup_unreferenced_objects()
                return copy.deepcopy(it), evicted
            except BaseException:
                self._restore_locked(snapshot)
                if new_object_path is not None:
                    try:
                        os.remove(new_object_path)
                    except OSError:
                        pass
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

    def apply_remote_current(self, item_id, revision):
        with self._lock:
            self._ensure_writable()
            if revision <= self.remote_revision:
                return False
            if item_id is not None and not any(
                    item.get("item_id") == item_id for item in self._items):
                return False
            snapshot = self._snapshot_locked()
            previous_remote = self._index_extra.get("remote_revision")
            try:
                self._current_item_id = item_id
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
            if not target:
                return False
            snapshot = self._snapshot_locked()
            try:
                self._items = [it for it in self._items if it.get("item_id") != item_id]
                if self._current_item_id == item_id:
                    self._current_item_id = None
                self._revision += 1
                self._save()
            except BaseException:
                self._restore_locked(snapshot)
                raise
            self._cleanup_item_files(target)
            return True

    def clear(self):
        with self._lock:
            self._ensure_writable()
            snapshot = self._snapshot_locked()
            self._items = []
            self._current_item_id = None
            self._received_cache = {}
            self._materialization_leases = {}
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
                local_available = (self.has_object(item.get("sha256", ""))
                                   or self._local_sources_available(item))
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
        referenced = ({item.get("sha256") for item in self._items if item.get("sha256")}
                      | self._cache_object_hashes())
        try:
            names = os.listdir(self.objects_dir)
        except OSError:
            return
        for name in names:
            if name not in referenced and not name.endswith(".tmp"):
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
    def record_cache_entry(self, content_sha256, payload_sha256=None, payload_size=None,
                           providers=None):
        with self._lock:
            self._ensure_writable()
            snapshot = self._snapshot_locked()
            try:
                entry = cm.make_cache_entry(content_sha256, payload_sha256, payload_size,
                                            providers=providers)
                existing = self._received_cache.get(content_sha256)
                if existing:
                    entry["received_at"] = existing.get("received_at", entry["received_at"])
                    existing_providers = existing.get("providers", [])
                    merged = {ep.get("device_id"): ep for ep in existing_providers
                              if isinstance(ep, dict) and ep.get("device_id")}
                    for p in entry.get("providers", []):
                        if p.get("device_id"):
                            merged[p["device_id"]] = p
                    if merged:
                        entry["providers"] = list(merged.values())
                self._received_cache[content_sha256] = entry
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
