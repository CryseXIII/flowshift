"""Materialize a finalized V2 manifest from the object store into a lease tree.

The productive Windows runtime hands materialized paths to CF_HDROP. Files are
preferably hardlinked to their content-addressed store objects so no payload is
duplicated; when linking is impossible (foreign volume, unsupported filesystem,
permission, link count) the object is streamed into a fresh copy and verified
against the manifest digest while copying.

Hardlinked files share the store object's inode, data and metadata. A
materialized tree is therefore *read-only-shared*: consumers must never modify
a materialized file in place (that would corrupt the shared object). The
runtime only ever unlinks materialized paths (lease release), which removes the
link and leaves the store object intact. Timestamps are only adjusted on copies
for the same reason.

Materialization builds in a sibling staging directory and is atomically moved
into the destination; on any failure the staging tree is removed completely.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass

import clipboard_manifest_v2 as manifest_v2
import clipboard_object_store_v2 as object_store_v2
import clipboard_paths as cpaths


CHUNK_BYTES = 1024 * 1024

STRATEGY_HARDLINK = "hardlink"
STRATEGY_COPY = "copy"
STRATEGY_MIXED = "mixed"


class MaterializationError(RuntimeError):
    """Materialization failed; ``code`` is a stable machine-readable reason.

    Codes: dest_not_empty, object_missing, object_size_mismatch, object_corrupt,
    path_invalid, manifest_invalid, size_limit, io_error.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self):
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class MaterializationResult:
    roots: tuple
    strategy: str
    linked_files: int
    copied_files: int
    bytes: int


def _open_object_readonly(path):
    """Open a store object read-only without following reparse points.

    Returns (handle, stat). Raises MaterializationError(object_missing) when the
    object does not exist or is not a regular file.
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise MaterializationError("object_missing", "store object is missing") from None
    if not stat.S_ISREG(before.st_mode) or cpaths._is_reparse_point(path, before):
        raise MaterializationError("object_missing", "store object is not a regular file")
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise MaterializationError("object_missing", "store object is missing") from None
    handle = os.fdopen(fd, "rb", buffering=0)
    try:
        opened = os.fstat(handle.fileno())
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise MaterializationError("object_missing", "store object changed while opening")
    except BaseException:
        handle.close()
        raise
    return handle, opened


def _try_hardlink(object_path, target, object_stat):
    """Return True when ``target`` is now a hardlink of the opened object."""
    try:
        os.link(object_path, target)
    except OSError:
        return False
    try:
        linked = os.lstat(target)
        if (stat.S_ISREG(linked.st_mode)
                and (linked.st_dev, linked.st_ino) == (object_stat.st_dev, object_stat.st_ino)
                and linked.st_size == object_stat.st_size):
            return True
    except OSError:
        pass
    try:
        os.unlink(target)
    except OSError:
        pass
    return False


def _copy_verified(handle, target, entry):
    size = entry["size"]
    hasher = hashlib.sha256()
    copied = 0
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as out:
            handle.seek(0)
            remaining = size
            while remaining:
                chunk = handle.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    break
                hasher.update(chunk)
                out.write(chunk)
                copied += len(chunk)
                remaining -= len(chunk)
            trailing = handle.read(1) if remaining == 0 else b""
            out.flush()
            os.fsync(out.fileno())
        if copied != size or trailing or hasher.hexdigest() != entry["sha256"]:
            raise MaterializationError("object_corrupt",
                                       "store object content does not match manifest")
    except BaseException:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    mtime_ns = entry.get("mtime_ns")
    if isinstance(mtime_ns, int) and mtime_ns > 0:
        # Never a future timestamp: lease cleanup removes files by age.
        clamped = min(mtime_ns, time.time_ns())
        try:
            os.utime(target, ns=(clamped, clamped))
        except OSError:
            pass


def _materialize_file(object_store, entry, target):
    """Materialize one file entry; returns "hardlink" or "copy"."""
    try:
        object_path = object_store.object_path(entry["sha256"])
    except object_store_v2.ObjectStoreV2Error as exc:
        raise MaterializationError("path_invalid", "manifest entry digest is invalid") from exc
    # Hold the store lock for the short open+link window so cooperating GC
    # cannot unlink the object between the size check and the link.
    with object_store.locked():
        handle, info = _open_object_readonly(object_path)
        try:
            if info.st_size != entry["size"]:
                raise MaterializationError("object_size_mismatch",
                                           "store object size does not match manifest")
            if _try_hardlink(object_path, target, info):
                handle.close()
                return STRATEGY_HARDLINK
        except BaseException:
            handle.close()
            raise
    # Copy fallback: the open handle keeps content readable regardless of GC.
    with handle:
        _copy_verified(handle, target, entry)
    return STRATEGY_COPY


def materialize_manifest(object_store, manifest, dest, *, hard_item_bytes=None):
    """Materialize ``manifest`` from ``object_store`` into the empty ``dest`` tree.

    Returns MaterializationResult with absolute top-level root paths in
    manifest entry order. Raises MaterializationError; never leaves a partial
    destination or staging tree behind.
    """
    try:
        manifest = manifest_v2.validate_manifest(manifest)
    except manifest_v2.ManifestValidationError as exc:
        raise MaterializationError("manifest_invalid", "manifest is invalid") from exc
    total = int(manifest["total_size"])
    if hard_item_bytes is not None and total > int(hard_item_bytes):
        raise MaterializationError("size_limit", "item exceeds the configured size limit")

    dest_abs = os.path.abspath(os.fspath(dest))
    try:
        parent = cpaths.ensure_safe_directory_root(os.path.dirname(dest_abs))
        if os.path.lexists(dest_abs):
            existing_root = cpaths.ensure_safe_directory_root(dest_abs)
            if os.listdir(existing_root):
                raise MaterializationError("dest_not_empty", "materialization target is not empty")
    except cpaths.PathValidationError as exc:
        raise MaterializationError("path_invalid", str(exc)) from exc
    except OSError as exc:
        raise MaterializationError("io_error", f"target root is not accessible: {exc.strerror}") from exc

    try:
        staging = tempfile.mkdtemp(prefix=f".{os.path.basename(dest_abs)}-materialize-", dir=parent)
    except OSError as exc:
        raise MaterializationError("io_error", f"cannot create staging: {exc.strerror}") from exc

    roots = []
    root_keys = set()
    linked = copied = 0
    materialized_bytes = 0
    try:
        try:
            staging_root = cpaths.ensure_safe_directory_root(staging)
            for entry in manifest["entries"]:
                root_name = entry["path"].split("/", 1)[0]
                root_key = cpaths.windows_collision_key(root_name)
                if root_key not in root_keys:
                    root_keys.add(root_key)
                    roots.append(root_name)
                target = cpaths.safe_target_path(staging_root, entry["path"])
                if entry["type"] == "directory":
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(target.parent, exist_ok=True)
                strategy = _materialize_file(object_store, entry, os.fspath(target))
                if strategy == STRATEGY_HARDLINK:
                    linked += 1
                else:
                    copied += 1
                materialized_bytes += int(entry["size"])
            if materialized_bytes != total:
                raise MaterializationError("object_size_mismatch",
                                           "materialized bytes do not match manifest total")
            if os.path.lexists(dest_abs):
                os.rmdir(dest_abs)
            os.replace(staging, dest_abs)
            staging = None
        except cpaths.PathValidationError as exc:
            raise MaterializationError("path_invalid", str(exc)) from exc
        except object_store_v2.ObjectStoreV2Error as exc:
            raise MaterializationError("io_error", f"object store unavailable: {exc.code}") from exc
        except OSError as exc:
            raise MaterializationError("io_error", f"materialization failed: {exc.strerror}") from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    if copied and linked:
        strategy = STRATEGY_MIXED
    elif copied:
        strategy = STRATEGY_COPY
    else:
        strategy = STRATEGY_HARDLINK
    return MaterializationResult(
        roots=tuple(os.path.join(dest_abs, name) for name in roots),
        strategy=strategy, linked_files=linked, copied_files=copied,
        bytes=materialized_bytes)
