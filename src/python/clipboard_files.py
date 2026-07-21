"""FlowShift clipboard file/batch bundling (pure + filesystem, testable).

A file or file-batch clipboard item is transferred as a single **deterministic
ZIP bundle** so it rides the exact same tested chunked-transfer + ChunkAssembler
path as any other blob. This module builds/unpacks that bundle and computes a
**content identity** (independent of the zip bytes) so identical file sets dedup
across copies.

Two hashes on purpose:
  * ``content_sha`` — sha256 over the sorted (relpath, file-sha256) list. Stable
    -> used as the item identity for dedup and as the store object key.
  * the transferred blob's own sha256 (computed on the zip bytes at send time) is
    what the ChunkAssembler verifies; it is carried in ``transfer_start``.

No Windows APIs here; only the standard library. Reads real files, so tests use a
temp dir on any OS.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import tempfile
import re
import zipfile

import clipboard_model as cbm
import clipboard_sources as csrc

CHUNK_READ = 1024 * 1024
# Fixed timestamp so the zip bytes are deterministic (dedup-friendly).
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK_READ)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _expand(paths):
    """Yield (abspath) for every real file under the given paths (dirs walked)."""
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for name in sorted(files):
                    yield os.path.join(root, name)
        elif os.path.isfile(p):
            yield p


def _common_base(files, original_paths):
    if len(original_paths) == 1 and os.path.isdir(original_paths[0]):
        return os.path.abspath(os.path.dirname(os.path.abspath(original_paths[0])))
    dirs = [os.path.dirname(f) for f in files]
    if not dirs:
        return ""
    try:
        return os.path.commonpath(dirs)
    except ValueError:  # different drives on Windows
        return ""


def scan_paths(paths):
    """Scan drop paths into file entries with rel paths, sizes and hashes.

    Returns a dict: files [{abspath, rel, size, sha256}], total_size, file_count,
    base, compressible_ratio.
    """
    files = sorted(set(_expand(paths)))
    base = _common_base(files, list(paths))
    entries = []
    total = 0
    comp = 0
    for f in files:
        try:
            size = os.path.getsize(f)
        except OSError:
            continue
        rel = os.path.relpath(f, base) if base else os.path.basename(f)
        rel = rel.replace("\\", "/")
        entries.append({"abspath": f, "rel": rel, "size": size, "sha256": hash_file(f)})
        total += size
        if cbm.is_compressible_ext(f):
            comp += 1
    ratio = (comp / len(entries)) if entries else 0.0
    return {"files": entries, "total_size": total, "file_count": len(entries),
            "base": base, "compressible_ratio": ratio}


def content_sha(entries):
    """Stable content identity over sorted (rel, file-sha256)."""
    h = hashlib.sha256()
    for e in sorted(entries, key=lambda x: x["rel"]):
        h.update(e["rel"].encode("utf-8"))
        h.update(b"\0")
        h.update(e["sha256"].encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def build_bundle_bytes(entries, compressible_ratio=1.0):
    """Build a deterministic ZIP of the entries (from their abspaths). Returns bytes."""
    buf = io.BytesIO()
    build_bundle_to_zipfile(buf, entries, compressible_ratio=compressible_ratio)
    return buf.getvalue()


def build_bundle_to_zipfile(zf_target, entries, compressible_ratio=1.0):
    compress = zipfile.ZIP_STORED if compressible_ratio < 0.5 else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(zf_target, "w", compression=compress) as zf:
        for e in sorted(entries, key=lambda x: x["rel"]):
            zi = zipfile.ZipInfo(filename=e["rel"], date_time=_ZIP_DATE)
            zi.compress_type = compress
            zi.external_attr = 0o600 << 16
            with open(e["abspath"], "rb") as src, zf.open(zi, "w") as dst:
                shutil.copyfileobj(src, dst, CHUNK_READ)


def build_bundle_to_file(entries, out_path, compressible_ratio=1.0):
    """Build a deterministic ZIP directly on disk and return its size + sha256."""
    out_path = os.path.abspath(out_path)
    parent = os.path.dirname(out_path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_path = out_path + ".tmp"
    try:
        build_bundle_to_zipfile(tmp_path, entries, compressible_ratio=compressible_ratio)
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
    entries = []
    for f in item.get("files", []) or []:
        path = f.get("abspath")
        if path and os.path.exists(path):
            entries.append(f)
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


def _extract_zipfile_streaming(zf, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    out = []
    for info in zf.infolist():
        target = _safe_zip_target(dest_dir, info.filename)
        if not target:
            continue
        if info.is_dir() or info.filename.endswith("/"):
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info, "r") as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK_READ)
        out.append(target)
    return out


def unpack_bundle_file(zip_path, dest_dir):
    """Extract a ZIP bundle from a file path with streaming member extraction."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        return _extract_zipfile_streaming(zf, dest_dir)


def unpack_bundle(data, dest_dir):
    """Extract a ZIP bundle into dest_dir. Returns the list of extracted files."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return _extract_zipfile_streaming(zf, dest_dir)


def make_file_item(paths, seq=0, created_at=None):
    """Build a clipboard file/file-batch item (metadata + source files).

    The item carries its source ``files`` (with per-file hashes) so the bundle can
    be built lazily on transfer and pasted locally without a copy. ``sha256`` is
    the stable content identity.
    """
    scan = scan_paths(paths)
    entries = scan["files"]
    if not entries:
        return None
    csha = content_sha(entries)
    kind = cbm.KIND_FILE if scan["file_count"] == 1 else cbm.KIND_FILE_BATCH
    if scan["file_count"] == 1:
        display = os.path.basename(entries[0]["abspath"])
        mime = "image/gif" if display.lower().endswith(".gif") else "application/zip"
    else:
        display = f"{scan['file_count']} Dateien ({cbm.format_bytes(scan['total_size'])})"
        mime = "application/zip"
    item = {
        "item_id": cbm.new_item_id(),
        "sha256": csha,
        "kind": kind,
        "mime": mime,
        "size": scan["total_size"],
        "created_at": created_at,
        "seq": int(seq),
        "display_name": display,
        "preview_text": "\n".join(e["rel"] for e in entries[:20]),
        "preview_hash": "",
        "file_count": scan["file_count"],
        "total_file_size": scan["total_size"],
        "pinned": False,
        "available": True,
        # File-specific metadata (kept in index.json; used for lazy bundling +
        # local paste without a copy).
        "files": [{"rel": e["rel"], "size": e["size"], "sha256": e["sha256"],
                   "abspath": e["abspath"]} for e in entries],
        "base": scan["base"],
        "compressible_ratio": scan["compressible_ratio"],
    }
    return cbm.version_item(item, payload_state="source_available")


def local_source_paths(item):
    """Absolute source paths for a locally-captured file item (or [] if none)."""
    out = []
    for f in item.get("files", []) or []:
        p = f.get("abspath")
        if p and os.path.exists(p):
            out.append(p)
    return out


def bundle_for_item(item):
    """Build the transfer blob (zip bytes) for a locally-captured file item."""
    entries = []
    for f in item.get("files", []) or []:
        if f.get("abspath") and os.path.exists(f["abspath"]):
            entries.append(f)
    if not entries:
        return None
    return build_bundle_bytes(entries, item.get("compressible_ratio", 1.0))
