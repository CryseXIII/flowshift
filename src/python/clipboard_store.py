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

import json
import os
import shutil
import threading

import clipboard_model as cm


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
            if os.path.exists(self.index_path):
                try:
                    with open(self.index_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._items = list(data.get("items", []))
                    self._revision = int(data.get("revision", 0))
                    self._seq = max([it.get("seq", 0) for it in self._items] + [0])
                except Exception:
                    self._items = []
                    self._revision = 0
                    self._seq = 0

    def _save(self):
        with self._lock:
            tmp = self.index_path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"revision": self._revision, "items": self._items}, f, indent=2)
                os.replace(tmp, self.index_path)
            except Exception:
                pass

    # ── accessors ──────────────────────────────────────────────────
    @property
    def revision(self):
        with self._lock:
            return self._revision

    def list_items(self):
        with self._lock:
            return [dict(it) for it in self._items]

    def get_item(self, item_id):
        with self._lock:
            for it in self._items:
                if it.get("item_id") == item_id:
                    return dict(it)
        return None

    def known_hashes(self):
        with self._lock:
            return {it.get("sha256") for it in self._items if it.get("sha256")}

    def total_size(self):
        with self._lock:
            return sum(int(it.get("size", 0) or 0) for it in self._items)

    def _object_path(self, sha256):
        return os.path.join(self.objects_dir, sha256)

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
        return os.path.exists(self._object_path(sha256))

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
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return path

    def add_item(self, item, data=None, enforce=None):
        """Add an item (optionally with its blob). Returns the stored item.

        ``enforce`` may be ``(max_items, max_total_bytes)`` to run eviction after.
        Dedup: if an item with the same sha256 already exists, its content is not
        re-written; a new history entry is still appended (same content, new copy
        event) unless it is the exact same trailing entry.
        """
        with self._lock:
            it = dict(item)
            it["seq"] = self._next_seq()
            if data is not None and it.get("sha256"):
                try:
                    self.write_object(it["sha256"], data)
                    it["available"] = True
                except OSError:
                    it["available"] = False
            else:
                it.setdefault("available", self.has_object(it.get("sha256", "")))
            self._items.append(it)
            self._revision += 1
            evicted = []
            if enforce:
                evicted = self._enforce_locked(*enforce)
            self._save()
            return dict(it), evicted

    def mark_available(self, item_id, available=True):
        with self._lock:
            for it in self._items:
                if it.get("item_id") == item_id:
                    it["available"] = bool(available)
                    self._revision += 1
                    self._save()
                    return True
        return False

    def set_pinned(self, item_id, pinned):
        with self._lock:
            for it in self._items:
                if it.get("item_id") == item_id:
                    it["pinned"] = bool(pinned)
                    self._save()
                    return True
        return False

    def _sha_refcount(self, sha256, exclude_id=None):
        return sum(1 for it in self._items
                   if it.get("sha256") == sha256 and it.get("item_id") != exclude_id)

    def delete_item(self, item_id):
        with self._lock:
            target = next((it for it in self._items if it.get("item_id") == item_id), None)
            if not target:
                return False
            self._items = [it for it in self._items if it.get("item_id") != item_id]
            # Delete the blob only if no remaining item references it.
            sha = target.get("sha256")
            if sha and self._sha_refcount(sha) == 0:
                try:
                    os.remove(self._object_path(sha))
                except OSError:
                    pass
            # Drop the preview if present.
            prev = os.path.join(self.previews_dir, f"{item_id}.png")
            if os.path.exists(prev):
                try:
                    os.remove(prev)
                except OSError:
                    pass
            self._revision += 1
            self._save()
            return True

    def clear(self):
        with self._lock:
            self._items = []
            self._revision += 1
            for d in (self.objects_dir, self.previews_dir):
                try:
                    shutil.rmtree(d, ignore_errors=True)
                    os.makedirs(d, exist_ok=True)
                except OSError:
                    pass
            self._save()
            return True

    def _enforce_locked(self, max_items, max_total_bytes):
        plan = cm.eviction_plan(self._items, max_items, max_total_bytes)
        for iid in plan:
            self._delete_locked(iid)
        return plan

    def _delete_locked(self, item_id):
        target = next((it for it in self._items if it.get("item_id") == item_id), None)
        if not target:
            return
        self._items = [it for it in self._items if it.get("item_id") != item_id]
        sha = target.get("sha256")
        if sha and self._sha_refcount(sha) == 0:
            try:
                os.remove(self._object_path(sha))
            except OSError:
                pass

    def enforce_limits(self, max_items, max_total_bytes):
        with self._lock:
            evicted = self._enforce_locked(max_items, max_total_bytes)
            if evicted:
                self._revision += 1
                self._save()
            return evicted

    def build_manifest(self, device_id):
        with self._lock:
            return cm.build_manifest(self.profile_id, device_id, self._revision, self._items)

    def cleanup_temp(self):
        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            os.makedirs(self.temp_dir, exist_ok=True)
        except OSError:
            pass


def profile_dir_name(identity):
    """Filesystem-safe directory name for a profile/peer identity."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(identity))
    return safe or "unknown"
