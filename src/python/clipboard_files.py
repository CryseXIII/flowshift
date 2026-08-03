"""Metadata-first file capture plus the legacy lazy ZIP transfer path."""
from __future__ import annotations

import hashlib
import io
import os
import stat
import shutil
import tempfile
import re
import zipfile

import clipboard_manifest_v2 as manifest_v2
import clipboard_model as cbm
import clipboard_paths as cpaths
import clipboard_sources as csrc

CHUNK_READ = 1024 * 1024
# Fixed timestamp so the zip bytes are deterministic (dedup-friendly).
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
SOURCE_FINGERPRINT_VERSION = 1
DEFAULT_MAX_DIRECTORIES = 100000
DEFAULT_MAX_DEPTH = 128
MAX_RECEIVED_ZIP_ENTRIES = 100000
MAX_RECEIVED_ZIP_LOGICAL_BYTES = 100 * 1024 * 1024 * 1024


class CaptureLimitError(ValueError):
    pass


def hash_file(path, max_bytes=None, cancelled=None):
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        while True:
            if cancelled and cancelled():
                raise CaptureLimitError("clipboard capture cancelled")
            b = f.read(CHUNK_READ)
            if not b:
                break
            total += len(b)
            if max_bytes is not None and total > int(max_bytes):
                raise CaptureLimitError("clipboard source grew during capture")
            h.update(b)
    return h.hexdigest()


def _is_reparse_point(path_stat):
    return (stat.S_ISLNK(path_stat.st_mode)
            or bool(getattr(path_stat, "st_file_attributes", 0)
                    & _FILE_ATTRIBUTE_REPARSE_POINT))


def _lstat_no_reparse(path):
    path = os.path.abspath(path)
    drive, tail = os.path.splitdrive(path)
    current = drive + os.sep if tail.startswith(os.sep) else drive
    for component in [part for part in tail.split(os.sep) if part]:
        current = os.path.join(current, component) if current else component
        path_stat = os.lstat(current)
        if _is_reparse_point(path_stat):
            raise CaptureLimitError("clipboard source contains a reparse point")
    return os.lstat(path)


def source_fingerprint(path, path_stat=None, *, logical_size=None):
    """Return a versioned metadata fingerprint without opening the source."""
    path_stat = path_stat or _lstat_no_reparse(path)
    size = int(path_stat.st_size if logical_size is None else logical_size)
    mtime_ns = max(0, int(getattr(path_stat, "st_mtime_ns", 0)))
    device = getattr(path_stat, "st_dev", None)
    inode = getattr(path_stat, "st_ino", None)
    strong = (isinstance(device, int) and not isinstance(device, bool) and device >= 0
              and isinstance(inode, int) and not isinstance(inode, bool) and inode > 0)
    fingerprint = {
        "version": SOURCE_FINGERPRINT_VERSION,
        "size": max(0, size),
        "mtime_ns": mtime_ns,
        "strength": "strong" if strong else "weak",
        "strong": strong,
    }
    if strong:
        fingerprint.update(device=device, inode=inode)
    return fingerprint


def validate_source_snapshot(entry, *, opened_stat=None):
    """Validate a captured entry against its path and optional open handle."""
    if not isinstance(entry, dict) or not entry.get("abspath"):
        raise CaptureLimitError("clipboard source snapshot is invalid")
    expected_type = entry.get("type", "file")
    expected = entry.get("source_fingerprint")
    if (expected_type not in ("file", "directory") or not isinstance(expected, dict)
            or expected.get("version") != SOURCE_FINGERPRINT_VERSION):
        raise CaptureLimitError("clipboard source snapshot is invalid")
    try:
        path_stat = _lstat_no_reparse(entry["abspath"])
    except (OSError, CaptureLimitError) as exc:
        raise CaptureLimitError("clipboard source is unavailable or unsafe") from exc

    def checked_fingerprint(path_stat):
        if expected_type == "directory":
            if not stat.S_ISDIR(path_stat.st_mode):
                raise CaptureLimitError("clipboard source type changed")
            logical_size = 0
        else:
            if not stat.S_ISREG(path_stat.st_mode):
                raise CaptureLimitError("clipboard source type changed")
            logical_size = int(path_stat.st_size)
        current = source_fingerprint(
            entry["abspath"], path_stat, logical_size=logical_size)
        if current != expected or logical_size != int(entry.get("size", -1)):
            raise CaptureLimitError("clipboard source fingerprint changed")
        return current

    current = checked_fingerprint(path_stat)
    if opened_stat is not None:
        checked_fingerprint(opened_stat)
    return current


def _common_base(original_paths, path_stats):
    if len(original_paths) == 1 and stat.S_ISDIR(path_stats[0].st_mode):
        return os.path.dirname(original_paths[0])
    dirs = [os.path.dirname(path) for path in original_paths]
    try:
        return os.path.commonpath(dirs)
    except ValueError:  # different drives on Windows
        return ""


def scan_paths(paths, max_total_bytes=None, max_files=100000, cancelled=None,
               max_directories=DEFAULT_MAX_DIRECTORIES, max_depth=DEFAULT_MAX_DEPTH):
    """Enumerate a bounded source tree using metadata only and without following links."""
    max_files = max(1, int(max_files))
    max_directories = max(0, int(max_directories))
    max_depth = max(0, int(max_depth))
    max_entries = max_files + max_directories
    original_paths = []
    path_stats = []
    seen_roots = set()
    for raw_path in paths:
        path = os.path.abspath(os.fspath(raw_path))
        key = os.path.normcase(path)
        if key in seen_roots:
            continue
        seen_roots.add(key)
        try:
            path_stat = _lstat_no_reparse(path)
        except OSError:
            continue
        if not (stat.S_ISREG(path_stat.st_mode) or stat.S_ISDIR(path_stat.st_mode)):
            raise CaptureLimitError("clipboard source must be a file or directory")
        if len(original_paths) >= max_entries:
            raise CaptureLimitError("clipboard entry count exceeds capture limit")
        original_paths.append(path)
        path_stats.append(path_stat)
    if not original_paths:
        return {"files": [], "entries": [], "total_size": 0, "file_count": 0,
                "directory_count": 0, "base": "", "compressible_ratio": 0.0,
                "source_paths": []}

    base = _common_base(original_paths, path_stats)
    entries = []
    seen = set()
    total = 0
    comp = 0
    file_count = 0
    directory_count = 0
    stack = [(path, path_stat, 0) for path, path_stat in reversed(
        list(zip(original_paths, path_stats)))]
    scheduled = {os.path.normcase(path) for path, _path_stat, _depth in stack}
    while stack:
        if cancelled and cancelled():
            raise CaptureLimitError("clipboard capture cancelled")
        path, path_stat, depth = stack.pop()
        key = os.path.normcase(path)
        scheduled.discard(key)
        if key in seen:
            continue
        seen.add(key)
        if _is_reparse_point(path_stat):
            raise CaptureLimitError("clipboard source contains a reparse point")
        rel = os.path.relpath(path, base) if base else os.path.basename(path)
        rel = rel.replace("\\", "/")
        is_directory = stat.S_ISDIR(path_stat.st_mode)
        size = 0 if is_directory else int(path_stat.st_size)
        if size < 0 or (max_total_bytes is not None and total + size > int(max_total_bytes)):
            raise CaptureLimitError("clipboard files exceed max_item_gb")
        if is_directory:
            directory_count += 1
            if directory_count > max_directories:
                raise CaptureLimitError("clipboard directory count exceeds capture limit")
        else:
            file_count += 1
            if file_count > max_files:
                raise CaptureLimitError("clipboard file count exceeds capture limit")
        fingerprint = source_fingerprint(path, path_stat, logical_size=size)
        entries.append({
            "abspath": path,
            "rel": rel,
            "type": "directory" if is_directory else "file",
            "size": size,
            "mtime_ns": fingerprint["mtime_ns"],
            "source_fingerprint": fingerprint,
            "source_available": True,
            "hash_state": "unhashed",
            "sha256": None,
        })
        total += size
        if not is_directory and cbm.is_compressible_ext(path):
            comp += 1
        if is_directory:
            if depth >= max_depth:
                try:
                    with os.scandir(path) as children:
                        if next(children, None) is not None:
                            raise CaptureLimitError(
                                "clipboard directory recursion exceeds capture limit")
                except OSError as exc:
                    raise CaptureLimitError("clipboard source directory is unavailable") from exc
                continue
            children = []
            try:
                with os.scandir(path) as iterator:
                    for child in iterator:
                        if cancelled and cancelled():
                            raise CaptureLimitError("clipboard capture cancelled")
                        child_key = os.path.normcase(child.path)
                        if child_key in seen or child_key in scheduled:
                            continue
                        if len(entries) + len(stack) + len(children) >= max_entries:
                            raise CaptureLimitError(
                                "clipboard entry count exceeds capture limit")
                        children.append(child)
                        scheduled.add(child_key)
            except OSError as exc:
                raise CaptureLimitError("clipboard source directory is unavailable") from exc
            for child in sorted(children, key=lambda value: value.name, reverse=True):
                try:
                    child_stat = _lstat_no_reparse(child.path)
                except (OSError, CaptureLimitError) as exc:
                    raise CaptureLimitError("clipboard source disappeared during capture") from exc
                if _is_reparse_point(child_stat):
                    raise CaptureLimitError("clipboard source contains a reparse point")
                if not (stat.S_ISREG(child_stat.st_mode) or stat.S_ISDIR(child_stat.st_mode)):
                    raise CaptureLimitError("clipboard source must be a file or directory")
                stack.append((child.path, child_stat, depth + 1))
    ratio = (comp / file_count) if file_count else 0.0
    entries.sort(key=lambda entry: entry["rel"].encode("utf-8"))
    return {"files": entries, "entries": entries, "total_size": total,
            "file_count": file_count, "directory_count": directory_count,
            "base": base, "compressible_ratio": ratio,
            "source_paths": original_paths}


def legacy_provisional_identity(entries):
    """Deterministic schema-1 identity derived only from canonical metadata."""
    identity_manifest = manifest_v2.build_manifest(
        "metadata-identity", 0, [_manifest_entry(entry) for entry in entries])
    return identity_manifest["manifest_digest"]


def content_sha(entries):
    """Compatibility alias for callers that previously requested content identity."""
    return legacy_provisional_identity(entries)


def _manifest_entry(entry):
    return {
        "path": entry["rel"],
        "type": entry.get("type", "file"),
        "size": int(entry.get("size", 0) or 0),
        "mtime_ns": int(entry.get("mtime_ns", 0) or 0),
        "source_fingerprint": dict(entry.get("source_fingerprint") or {}),
        "hash_state": entry.get("hash_state", "unhashed"),
        "sha256": entry.get("sha256"),
    }


def build_bundle_bytes(entries, compressible_ratio=1.0):
    """Build a deterministic ZIP of the entries (from their abspaths). Returns bytes."""
    entries = _validated_source_entries(entries)
    buf = io.BytesIO()
    build_bundle_to_zipfile(buf, entries, compressible_ratio=compressible_ratio)
    _revalidate_source_entries(entries)
    return buf.getvalue()


def build_bundle_to_zipfile(zf_target, entries, compressible_ratio=1.0):
    compress = zipfile.ZIP_STORED if compressible_ratio < 0.5 else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(zf_target, "w", compression=compress) as zf:
        for e in sorted(entries, key=lambda x: x["rel"]):
            is_directory = e.get("type") == "directory"
            zi = zipfile.ZipInfo(filename=e["rel"].rstrip("/") + ("/" if is_directory else ""),
                                 date_time=_ZIP_DATE)
            zi.compress_type = compress
            zi.external_attr = (0o40700 if is_directory else 0o600) << 16
            if is_directory:
                zf.writestr(zi, b"")
                continue
            with open(e["abspath"], "rb") as src, zf.open(zi, "w") as dst:
                shutil.copyfileobj(src, dst, CHUNK_READ)


def build_bundle_to_file(entries, out_path, compressible_ratio=1.0):
    """Build a deterministic ZIP directly on disk and return its size + sha256."""
    out_path = os.path.abspath(out_path)
    parent = os.path.dirname(out_path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_path = out_path + ".tmp"
    entries = _validated_source_entries(entries)
    try:
        build_bundle_to_zipfile(tmp_path, entries, compressible_ratio=compressible_ratio)
        _revalidate_source_entries(entries)
        size = os.path.getsize(tmp_path)
        sha = hash_file(tmp_path)
        os.replace(tmp_path, out_path)
        return {"path": out_path, "size": size, "sha256": sha}
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def build_bundle_source(item, temp_dir, ram_limit_bytes, disk_free_bytes=None):
    """Return a streaming source for a file/file-batch item.

    Small bundles stay in RAM. Large bundles are built to a temp ZIP on disk and
    streamed from there.
    """
    entries = _validated_source_entries(item.get("files", []) or [])
    if not entries:
        return None

    ram_limit_bytes = max(1, int(ram_limit_bytes))
    estimate = sum(int(e.get("size", 0) or 0) for e in entries)
    if estimate <= ram_limit_bytes:
        blob = build_bundle_bytes(entries, item.get("compressible_ratio", 1.0))
        return csrc.BytesTransferSource(blob, item_id=item.get("item_id", ""),
                                        display_name=item.get("display_name", ""))

    temp_dir = os.path.abspath(temp_dir or ".")
    os.makedirs(temp_dir, exist_ok=True)
    if disk_free_bytes is None:
        try:
            disk_free_bytes = shutil.disk_usage(temp_dir).free
        except Exception:
            disk_free_bytes = 0
    if int(disk_free_bytes) < estimate:
        raise OSError("Nicht genug Speicherplatz")

    fd, out_path = tempfile.mkstemp(prefix=f"{item.get('item_id', 'bundle')}_", suffix=".zip",
                                    dir=temp_dir)
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(out_path)
    except OSError:
        pass
    result = build_bundle_to_file(entries, out_path, compressible_ratio=item.get("compressible_ratio", 1.0))
    return csrc.TempFileTransferSource(result["path"], item_id=item.get("item_id", ""),
                                       display_name=item.get("display_name", ""),
                                       sha256=result["sha256"])


def _safe_zip_target(dest_dir, member_name):
    dest_abs = os.path.abspath(dest_dir)
    name = str(member_name or "").replace("\\", "/")
    if not name or name.endswith("/"):
        return None
    if name.startswith("/") or name.startswith("\\"):
        return None
    if name.startswith("//"):
        return None
    if re.match(r"^[A-Za-z]:", name):
        return None
    parts = []
    for part in name.split("/"):
        if part in ("", ".", ".."):
            return None
        if ":" in part:
            return None
        parts.append(part)
    target_abs = os.path.abspath(os.path.join(dest_abs, *parts))
    try:
        if os.path.commonpath([dest_abs, target_abs]) != dest_abs:
            return None
    except ValueError:
        return None
    return target_abs


def _extract_zipfile_streaming(zf, dest_dir, *, return_roots=False):
    os.makedirs(dest_dir, exist_ok=True)
    out = []
    roots = []
    root_keys = set()
    for info in zf.infolist():
        member_name = info.filename.rstrip("/") if info.is_dir() else info.filename
        target = _safe_zip_target(dest_dir, member_name)
        if not target:
            continue
        root_name = member_name.replace("\\", "/").split("/", 1)[0]
        root = _safe_zip_target(dest_dir, root_name)
        root_key = os.path.normcase(root) if root else None
        if root_key and root_key not in root_keys:
            root_keys.add(root_key)
            roots.append(root)
        if info.is_dir() or info.filename.endswith("/"):
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info, "r") as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK_READ)
        out.append(target)
    return roots if return_roots else out


def unpack_bundle_file(zip_path, dest_dir):
    """Extract a ZIP bundle from a file path with streaming member extraction."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        return _extract_zipfile_streaming(zf, dest_dir)


def unpack_bundle_roots_file(zip_path, dest_dir, *, expected_logical_bytes=None,
                             expected_file_count=None, hard_item_bytes=None):
    """Strictly validate and extract a received ZIP as top-level roots."""
    if (expected_logical_bytes is not None
            and (not isinstance(expected_logical_bytes, int)
                 or isinstance(expected_logical_bytes, bool)
                 or expected_logical_bytes < 0)):
        raise cpaths.PathValidationError("invalid expected archive logical size")
    if (expected_file_count is not None
            and (not isinstance(expected_file_count, int)
                 or isinstance(expected_file_count, bool)
                 or expected_file_count < 0)):
        raise cpaths.PathValidationError("invalid expected archive file count")
    if (hard_item_bytes is not None
            and (not isinstance(hard_item_bytes, int) or isinstance(hard_item_bytes, bool)
                 or hard_item_bytes < 0)):
        raise cpaths.PathValidationError("invalid archive hard item limit")
    logical_limit = min(
        MAX_RECEIVED_ZIP_LOGICAL_BYTES,
        hard_item_bytes if hard_item_bytes is not None else MAX_RECEIVED_ZIP_LOGICAL_BYTES,
    )
    if expected_logical_bytes is not None and expected_logical_bytes > logical_limit:
        raise cpaths.PathValidationError("expected archive logical size exceeds limit")
    if expected_file_count is not None and expected_file_count > MAX_RECEIVED_ZIP_ENTRIES:
        raise cpaths.PathValidationError("expected archive file count exceeds limit")
    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = zf.infolist()
        if len(infos) > MAX_RECEIVED_ZIP_ENTRIES:
            raise cpaths.PathValidationError("archive entry count exceeds limit")
        entries = []
        logical_bytes = 0
        file_count = 0
        for info in infos:
            mode = (info.external_attr >> 16) & 0xFFFF
            dos_attributes = info.external_attr & 0xFFFF
            if stat.S_ISLNK(mode) or dos_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise cpaths.PathValidationError("archive contains a symlink or reparse point")
            is_directory = info.is_dir() or info.filename.endswith("/")
            mode_type = stat.S_IFMT(mode)
            if mode_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise cpaths.PathValidationError("archive contains a special filesystem entry")
            member_name = info.filename.rstrip("/") if is_directory else info.filename
            logical_bytes += int(info.file_size)
            if not is_directory:
                file_count += 1
            if logical_bytes > logical_limit:
                raise cpaths.PathValidationError("archive logical expansion exceeds limit")
            entries.append({"path": member_name,
                            "type": "directory" if is_directory else "file"})
        if (expected_logical_bytes is not None
                and logical_bytes != int(expected_logical_bytes)):
            raise cpaths.PathValidationError("archive logical size does not match metadata")
        if expected_file_count is not None and file_count != int(expected_file_count):
            raise cpaths.PathValidationError("archive file count does not match metadata")
        canonical = cpaths.validate_path_entries(entries)
        dest_abs = os.path.abspath(dest_dir)
        parent = cpaths.ensure_safe_directory_root(os.path.dirname(dest_abs))
        if os.path.exists(dest_abs):
            existing_root = cpaths.ensure_safe_directory_root(dest_abs)
            for path in canonical:
                cpaths.safe_target_path(existing_root, path)
            if os.listdir(existing_root):
                raise cpaths.PathValidationError("target root is not empty")
        staging = tempfile.mkdtemp(prefix=f".{os.path.basename(dest_abs)}-extract-", dir=parent)

        roots = []
        root_keys = set()
        extracted_bytes = 0
        try:
            staging_root = cpaths.ensure_safe_directory_root(staging)
            for info, entry in zip(infos, entries):
                root_name = cpaths.canonical_relative_path(entry["path"]).split("/", 1)[0]
                root_key = cpaths.windows_collision_key(root_name)
                if root_key not in root_keys:
                    root_keys.add(root_key)
                    roots.append(root_name)
                target = cpaths.safe_target_path(staging_root, entry["path"])
                if entry["type"] == "directory":
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(target.parent, exist_ok=True)
                member_bytes = 0
                with zf.open(info, "r") as src, open(target, "wb") as dst:
                    while True:
                        block = src.read(CHUNK_READ)
                        if not block:
                            break
                        member_bytes += len(block)
                        extracted_bytes += len(block)
                        if (member_bytes > info.file_size or extracted_bytes > logical_bytes
                                or extracted_bytes > logical_limit):
                            raise cpaths.PathValidationError(
                                "archive expansion exceeds announced size")
                        dst.write(block)
                if member_bytes != info.file_size:
                    raise cpaths.PathValidationError(
                        "archive member size does not match metadata")
            if extracted_bytes != logical_bytes:
                raise cpaths.PathValidationError("archive logical size changed during extraction")

            if os.path.exists(dest_abs):
                os.rmdir(dest_abs)
            os.replace(staging, dest_abs)
            staging = None
            return [os.path.join(dest_abs, name) for name in roots]
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)


def unpack_bundle(data, dest_dir):
    """Extract a ZIP bundle into dest_dir. Returns the list of extracted files."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return _extract_zipfile_streaming(zf, dest_dir)


def make_file_item_from_scan(scan, seq=0, created_at=None, item_id=None):
    """Build one copy-event item from an already enumerated metadata snapshot."""
    entries = scan.get("entries", scan.get("files", []))
    if not entries:
        return None
    item_id = item_id or cbm.new_item_id()
    item_revision = 0
    batch_manifest = manifest_v2.build_manifest(
        item_id, item_revision, [_manifest_entry(entry) for entry in entries])
    provisional_sha = legacy_provisional_identity(entries)
    file_entries = [entry for entry in entries if entry.get("type", "file") == "file"]
    if len(entries) == 1 and len(file_entries) == 1:
        kind = cbm.KIND_FILE
        display = os.path.basename(file_entries[0]["abspath"])
        mime = "image/gif" if display.lower().endswith(".gif") else "application/zip"
    else:
        kind = cbm.KIND_FILE_BATCH
        display = f"{scan['file_count']} Dateien ({cbm.format_bytes(scan['total_size'])})"
        mime = "application/zip"
    item = {
        "item_id": item_id,
        "item_revision": item_revision,
        "sha256": provisional_sha,
        "legacy_provisional_sha256": provisional_sha,
        "metadata_identity_sha256": provisional_sha,
        "content_sha256": None,
        "hash_state": "unhashed",
        "batch_manifest": batch_manifest,
        "kind": kind,
        "mime": mime,
        "size": scan["total_size"],
        "created_at": created_at,
        "seq": int(seq),
        "display_name": display,
        "preview_text": "\n".join(e["rel"] for e in entries[:20]),
        "preview_hash": "",
        "file_count": scan["file_count"],
        "directory_count": scan.get("directory_count", 0),
        "total_file_size": scan["total_size"],
        "pinned": False,
        "available": True,
        "source_available": True,
        "files": [dict(entry) for entry in entries],
        "source_paths": list(scan.get("source_paths", [])),
        "base": scan["base"],
        "compressible_ratio": scan["compressible_ratio"],
        "payload": {"content_sha256": None, "encoding": "deterministic_zip",
                    "sha256": None, "size": None},
    }
    return cbm.version_item(item, payload_state="source_available")


def make_file_item(paths, seq=0, created_at=None, max_total_bytes=None, max_files=100000,
                   cancelled=None):
    """Build a metadata-only item whose legacy ZIP remains lazy."""
    scan = scan_paths(paths, max_total_bytes=max_total_bytes, max_files=max_files,
                      cancelled=cancelled)
    return make_file_item_from_scan(scan, seq=seq, created_at=created_at)


def _source_entry_available(entry):
    path = entry.get("abspath") if isinstance(entry, dict) else None
    if not path:
        return False
    try:
        path_stat = _lstat_no_reparse(path)
    except (OSError, CaptureLimitError):
        return False
    expected_type = entry.get("type", "file")
    if expected_type == "directory":
        if not stat.S_ISDIR(path_stat.st_mode):
            return False
        logical_size = 0
    else:
        if not stat.S_ISREG(path_stat.st_mode):
            return False
        logical_size = int(path_stat.st_size)
    expected = entry.get("source_fingerprint")
    if isinstance(expected, dict) and expected.get("version") == SOURCE_FINGERPRINT_VERSION:
        if source_fingerprint(path, path_stat, logical_size=logical_size) != expected:
            return False
    elif logical_size != entry.get("size"):
        return False
    legacy_sha = entry.get("sha256")
    if expected_type != "directory" and cbm.is_valid_sha256(legacy_sha):
        try:
            return hash_file(path, max_bytes=logical_size).lower() == legacy_sha.lower()
        except (OSError, CaptureLimitError):
            return False
    return True


def _validated_source_entries(entries):
    validated = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("abspath") \
                or not _source_entry_available(entry):
            raise CaptureLimitError("clipboard source changed before legacy bundle creation")
        validated.append(entry)
    if not validated:
        raise CaptureLimitError("clipboard source is unavailable")
    return validated


def _revalidate_source_entries(entries):
    if not all(_source_entry_available(entry) for entry in entries):
        raise CaptureLimitError("clipboard source changed during legacy bundle creation")


def local_sources_available(item):
    entries = item.get("files") if isinstance(item, dict) else None
    return bool(isinstance(entries, list) and entries
                and all(_source_entry_available(entry) for entry in entries))


def local_source_paths(item):
    """Absolute source paths for a locally-captured file item (or [] if none)."""
    if not local_sources_available(item):
        return []
    roots = item.get("source_paths")
    if isinstance(roots, list) and roots:
        return list(roots)
    out = []
    for f in item.get("files", []) or []:
        p = f.get("abspath")
        if p and f.get("type", "file") == "file":
            out.append(p)
    return out


def bundle_for_item(item):
    """Build the transfer blob (zip bytes) for a locally-captured file item."""
    entries = item.get("files", []) or []
    if not entries:
        return None
    return build_bundle_bytes(entries, item.get("compressible_ratio", 1.0))
