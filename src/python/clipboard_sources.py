"""FlowShift clipboard transfer sources.

Small clipboard payloads can stay in memory, but large file/batch bundles and
already-persisted blobs need a streaming source so the sender does not load the
whole payload at once.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time

import clipboard_model as cbm


ACTIVE_SUFFIX = ".active"


def active_marker_path(path):
    return str(path) + ACTIVE_SUFFIX


def mark_active(path):
    marker = active_marker_path(path)
    try:
        with open(marker, "a", encoding="utf-8"):
            pass
    except OSError:
        pass
    return marker


def clear_active(path):
    marker = active_marker_path(path)
    try:
        os.remove(marker)
    except OSError:
        pass


def has_active_marker(path):
    return os.path.exists(active_marker_path(path))


def cleanup_temp_tree(root_dir, max_age_hours=None):
    try:
        if max_age_hours is None:
            shutil.rmtree(root_dir, ignore_errors=True)
            os.makedirs(root_dir, exist_ok=True)
            return
        cutoff = time.time() - max(0.0, float(max_age_hours)) * 3600.0
        os.makedirs(root_dir, exist_ok=True)
        for root, _dirs, files in os.walk(root_dir, topdown=False):
            for name in files:
                path = os.path.join(root, name)
                if name.endswith(ACTIVE_SUFFIX):
                    base = path[: -len(ACTIVE_SUFFIX)]
                    try:
                        if not os.path.exists(base) and os.path.getmtime(path) <= cutoff:
                            os.remove(path)
                    except OSError:
                        pass
                    continue
                marker = active_marker_path(path)
                try:
                    mtimes = [os.path.getmtime(path)]
                    if os.path.exists(marker):
                        mtimes.append(os.path.getmtime(marker))
                    if max(mtimes) <= cutoff:
                        os.remove(path)
                        try:
                            os.remove(marker)
                        except OSError:
                            pass
                except OSError:
                    pass
            if root != root_dir:
                try:
                    if not os.listdir(root):
                        os.rmdir(root)
                except OSError:
                    pass
    except OSError:
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


class TransferSource:
    def __init__(self, item_id, total_bytes, sha256, display_name, cleanup_required=False):
        self.item_id = str(item_id or "")
        self.total_bytes = max(0, int(total_bytes))
        self.sha256 = str(sha256 or "")
        self.display_name = str(display_name or "")
        self.cleanup_required = bool(cleanup_required)

    def iter_chunks(self, chunk_size, start_index=0):
        raise NotImplementedError

    def cleanup(self):
        return None


class BytesTransferSource(TransferSource):
    def __init__(self, data, item_id="", display_name="", sha256=None):
        self._data = bytes(data or b"")
        super().__init__(item_id, len(self._data), sha256 or cbm.sha256_bytes(self._data),
                         display_name, cleanup_required=False)

    def iter_chunks(self, chunk_size, start_index=0):
        chunk_size = max(1, int(chunk_size))
        for c in cbm.chunk_plan(self.total_bytes, chunk_size)[max(0, int(start_index)):]:
            piece = self._data[c["offset"]:c["offset"] + c["length"]]
            yield {"index": c["index"], "offset": c["offset"], "data": piece,
                   "sha256": cbm.sha256_bytes(piece)}


class FileTransferSource(TransferSource):
    def __init__(self, path, item_id="", display_name="", sha256=None):
        self.path = os.path.abspath(path)
        total_bytes = os.path.getsize(self.path)
        super().__init__(item_id, total_bytes, sha256 or _hash_file(self.path), display_name,
                         cleanup_required=False)

    def iter_chunks(self, chunk_size, start_index=0):
        chunk_size = max(1, int(chunk_size))
        plan = cbm.chunk_plan(self.total_bytes, chunk_size)
        start_index = max(0, int(start_index))
        with open(self.path, "rb") as f:
            for c in plan[start_index:]:
                f.seek(c["offset"])
                piece = f.read(c["length"])
                yield {"index": c["index"], "offset": c["offset"], "data": piece,
                       "sha256": cbm.sha256_bytes(piece)}


class TempFileTransferSource(FileTransferSource):
    def __init__(self, path, item_id="", display_name="", sha256=None):
        super().__init__(path, item_id=item_id, display_name=display_name, sha256=sha256)
        self.cleanup_required = True
        mark_active(self.path)

    def cleanup(self):
        clear_active(self.path)
        try:
            os.remove(self.path)
        except OSError:
            pass
