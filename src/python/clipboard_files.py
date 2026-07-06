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
import zipfile

import clipboard_model as cbm

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
    compress = zipfile.ZIP_STORED if compressible_ratio < 0.5 else zipfile.ZIP_DEFLATED
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compress) as zf:
        for e in sorted(entries, key=lambda x: x["rel"]):
            zi = zipfile.ZipInfo(filename=e["rel"], date_time=_ZIP_DATE)
            zi.compress_type = compress
            with open(e["abspath"], "rb") as f:
                zf.writestr(zi, f.read())
    return buf.getvalue()


def unpack_bundle(data, dest_dir):
    """Extract a ZIP bundle into dest_dir. Returns the list of extracted files."""
    os.makedirs(dest_dir, exist_ok=True)
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            # Guard against path traversal.
            target = os.path.normpath(os.path.join(dest_dir, name))
            if not target.startswith(os.path.normpath(dest_dir)):
                continue
            if name.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                dst.write(src.read())
            out.append(target)
    return out


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
    else:
        display = f"{scan['file_count']} Dateien ({cbm.format_bytes(scan['total_size'])})"
    item = {
        "item_id": cbm.new_item_id(),
        "sha256": csha,
        "kind": kind,
        "mime": "application/zip",
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
    return item


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
