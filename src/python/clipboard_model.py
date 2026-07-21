"""FlowShift clipboard — pure data model + logic (platform independent).

Layer 1 of the clipboard feature: item shapes, hashing, manifest build/parse,
manifest-based sync diff (dedup + only-missing + manual-required by size),
history limits + eviction (FIFO, size, pinning), byte/rate/ETA formatting, ZIP
strategy decision, and chunk planning. No Windows APIs, no filesystem, no
network here — all of it is unit-testable on any OS.

The store (filesystem) lives in ``clipboard_store.py`` and the wire messages in
``clipboard_protocol.py``; both build on this module.
"""
from __future__ import annotations

import os
import hashlib
import time
import uuid

import clipboard_html as chtml

# ── Item kinds ──────────────────────────────────────────────────────
KIND_TEXT = "text"
KIND_HTML = "html"
KIND_IMAGE = "image"
KIND_GIF = "gif"
KIND_FILE = "file"
KIND_FILE_BATCH = "file_batch"
KIND_AUDIO = "audio"
KIND_BINARY = "binary"
CLIP_KINDS = (KIND_TEXT, KIND_HTML, KIND_IMAGE, KIND_GIF, KIND_FILE,
              KIND_FILE_BATCH, KIND_AUDIO, KIND_BINARY)


def is_gif_item(item):
    """Return True when an item represents an animated GIF preview candidate."""
    if not isinstance(item, dict):
        return False
    preview_kind = str(item.get("preview_kind", "")).strip().lower()
    if preview_kind == "animated_gif":
        return True
    kind = str(item.get("kind", "")).strip().lower()
    mime = str(item.get("mime", "")).strip().lower()
    display_name = os.path.basename(str(item.get("display_name", ""))).strip().lower()
    if kind == KIND_GIF or mime == "image/gif":
        return True
    if kind in (KIND_FILE, KIND_BINARY) and display_name.endswith(".gif"):
        return True
    return False

# File extensions that are already compressed -> zipping wastes CPU for no gain.
ALREADY_COMPRESSED_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
    ".mp3", ".aac", ".ogg", ".opus", ".m4a", ".flac",
    ".mp4", ".mkv", ".mov", ".avi", ".webm",
    ".zip", ".7z", ".rar", ".gz", ".bz2", ".xz", ".zst",
    ".pdf", ".docx", ".xlsx", ".pptx",
}

PREVIEW_TEXT_MAX = 4096   # chars kept for a text preview in the manifest


# ── Defaults / settings ─────────────────────────────────────────────
DEFAULT_CLIPBOARD_SETTINGS = {
    "enabled": False,
    "history_max_items": 200,          # 20..999
    "history_max_total_gb": 2.0,       # 0.1..100
    "max_auto_transfer_mb": 100,       # items larger are manual-only
    "max_item_gb": 50.0,               # hard per-item cap (manual + chunked)
    "persist": True,
    "sync_on_activate": True,
    "manual_only": False,
    "direction_mode": "source_to_target",   # or "bidirectional_manual"
    "intercept_win_v": False,
    "capture_plaintext_alongside_html": False,
    "paste_hotkey": "Ctrl+Alt+V",
    "clipboard_transfer_max_retries": 5,
    "clipboard_transfer_retry_delay_ms": 500,
    "clipboard_transfer_max_parallel": 1,
    "clipboard_max_transfer_kib_per_sec": 0,
    "clipboard_disk_assembler_threshold_mb": 32,
    "clipboard_ram_zip_limit_mb": 256,
    "clipboard_temp_cleanup_max_age_hours": 24,
    "byte_unit": "auto",               # byte|KB|MB|KiB|MiB|auto
    "rate_unit": "auto",               # B/s|KB/s|MB/s|KiB/s|MiB/s|auto
    "thumbnail_size": "mittel",        # klein|mittel|gross|custom
    "thumbnail_custom_px": 96,
    "zip_strategy": "auto",            # auto|never|always_batch
    "max_retries": 5,
}

_BYTE_UNITS = ("byte", "KB", "MB", "KiB", "MiB", "auto")
_RATE_UNITS = ("B/s", "KB/s", "MB/s", "KiB/s", "MiB/s", "auto")


def clipboard_settings(config):
    """Return normalised clipboard settings merged with defaults + clamped."""
    raw = {}
    if isinstance(config, dict):
        raw = config.get("clipboard") or {}
    out = dict(DEFAULT_CLIPBOARD_SETTINGS)
    if isinstance(raw, dict):
        for k in out:
            if k in raw and raw[k] is not None:
                out[k] = raw[k]

    def _clamp_int(key, lo, hi):
        try:
            out[key] = max(lo, min(hi, int(out[key])))
        except (TypeError, ValueError):
            out[key] = DEFAULT_CLIPBOARD_SETTINGS[key]

    def _clamp_float(key, lo, hi):
        try:
            out[key] = max(lo, min(hi, float(out[key])))
        except (TypeError, ValueError):
            out[key] = DEFAULT_CLIPBOARD_SETTINGS[key]

    _clamp_int("history_max_items", 20, 999)
    _clamp_float("history_max_total_gb", 0.1, 100.0)
    _clamp_int("max_auto_transfer_mb", 1, 1024 * 1024)
    _clamp_float("max_item_gb", 0.1, 100000.0)
    _clamp_int("max_retries", 0, 100)
    _clamp_int("clipboard_transfer_max_retries", 0, 100)
    _clamp_int("clipboard_transfer_retry_delay_ms", 0, 60000)
    _clamp_int("clipboard_transfer_max_parallel", 1, 8)
    _clamp_int("clipboard_max_transfer_kib_per_sec", 0, 1024 * 1024)
    _clamp_int("clipboard_disk_assembler_threshold_mb", 1, 1024 * 1024)
    _clamp_int("clipboard_ram_zip_limit_mb", 1, 1024 * 1024)
    _clamp_int("clipboard_temp_cleanup_max_age_hours", 1, 24 * 365)
    _clamp_int("thumbnail_custom_px", 16, 1024)
    out["enabled"] = bool(out["enabled"])
    out["persist"] = bool(out["persist"])
    out["sync_on_activate"] = bool(out["sync_on_activate"])
    out["manual_only"] = bool(out["manual_only"])
    out["intercept_win_v"] = bool(out["intercept_win_v"])
    out["capture_plaintext_alongside_html"] = bool(out["capture_plaintext_alongside_html"])
    if out["byte_unit"] not in _BYTE_UNITS:
        out["byte_unit"] = "auto"
    if out["rate_unit"] not in _RATE_UNITS:
        out["rate_unit"] = "auto"
    if out["direction_mode"] not in ("source_to_target", "bidirectional_manual"):
        out["direction_mode"] = "source_to_target"
    if out["zip_strategy"] not in ("auto", "never", "always_batch"):
        out["zip_strategy"] = "auto"
    if out["thumbnail_size"] not in ("klein", "mittel", "gross", "custom"):
        out["thumbnail_size"] = "mittel"
    return out


# ── Hashing / ids ───────────────────────────────────────────────────
def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def new_item_id():
    return uuid.uuid4().hex


# ── Item construction ───────────────────────────────────────────────
def _now():
    return time.time()


def make_text_item(text, seq, kind=KIND_TEXT, mime="text/plain", created_at=None):
    data = text.encode("utf-8")
    return {
        "item_id": new_item_id(),
        "sha256": sha256_bytes(data),
        "kind": kind,
        "mime": mime,
        "size": len(data),
        "created_at": created_at if created_at is not None else _now(),
        "seq": int(seq),
        "display_name": (text.strip().splitlines()[0][:80] if text.strip() else "(leer)"),
        "preview_text": text[:PREVIEW_TEXT_MAX],
        "preview_hash": "",
        "file_count": 0,
        "total_file_size": 0,
        "pinned": False,
        "available": True,
    }


def make_html_item(cf_html_bytes, preview_text, seq=0, source_url=None, created_at=None):
    preview = (preview_text or "").strip()
    parsed = chtml.parse_cf_html(cf_html_bytes)
    if parsed and source_url is None:
        source_url = parsed.get("source_url")
    if not preview and parsed:
        preview = chtml.html_to_preview_text(parsed.get("fragment") or parsed.get("html") or "",
                                             PREVIEW_TEXT_MAX)
    metadata = {"has_html": True}
    if source_url:
        metadata["source_url"] = source_url
    return {
        "item_id": new_item_id(),
        "sha256": sha256_bytes(cf_html_bytes),
        "kind": KIND_HTML,
        "mime": "text/html",
        "size": len(cf_html_bytes),
        "created_at": created_at if created_at is not None else _now(),
        "seq": int(seq),
        "display_name": "HTML",
        "preview_text": preview[:PREVIEW_TEXT_MAX],
        "preview_hash": "",
        "file_count": 0,
        "total_file_size": 0,
        "pinned": False,
        "available": True,
        "metadata": metadata,
    }


def make_binary_item(sha256, size, seq, kind=KIND_BINARY, mime="application/octet-stream",
                     display_name="", created_at=None, file_count=0, total_file_size=0,
                     available=True, preview_text="", preview_hash=""):
    return {
        "item_id": new_item_id(),
        "sha256": sha256,
        "kind": kind,
        "mime": mime,
        "size": int(size),
        "created_at": created_at if created_at is not None else _now(),
        "seq": int(seq),
        "display_name": display_name,
        "preview_text": preview_text,
        "preview_hash": preview_hash,
        "file_count": int(file_count),
        "total_file_size": int(total_file_size),
        "pinned": False,
        "available": bool(available),
    }


_MANIFEST_FIELDS = ("item_id", "sha256", "kind", "mime", "size", "created_at",
                    "seq", "display_name", "preview_text", "preview_hash",
                    "file_count", "total_file_size", "available", "metadata")


def manifest_item(item):
    """Reduce a store item to the metadata that goes into a manifest (no data)."""
    return {k: item.get(k) for k in _MANIFEST_FIELDS}


def build_manifest(profile_id, device_id, revision, items):
    return {
        "type": "clipboard_manifest",
        "profile_id": profile_id,
        "device_id": device_id,
        "history_revision": int(revision),
        "items": [manifest_item(it) for it in items],
    }


def parse_manifest(msg):
    if not isinstance(msg, dict) or msg.get("type") != "clipboard_manifest":
        return None
    return {
        "profile_id": msg.get("profile_id"),
        "device_id": msg.get("device_id"),
        "history_revision": msg.get("history_revision", 0),
        "items": list(msg.get("items", [])),
    }


# ── Manifest-based sync diff ─────────────────────────────────────────
def diff_manifest(local_hashes, remote_items, auto_limit_bytes):
    """Decide what to sync from a remote manifest.

    ``local_hashes`` is a set of sha256 strings already present locally (content
    identity -> perfect dedup). ``remote_items`` is the remote manifest item list
    (order = source history order). Items already known are skipped; unknown items
    within ``auto_limit_bytes`` are requested; larger unknown items are marked
    manual-required. Order is preserved.

    Returns dict: to_request, skipped_existing, manual_required, order.
    """
    known = set(local_hashes or ())
    to_request = []
    manual_required = []
    skipped = 0
    order = []
    for it in remote_items:
        sha = it.get("sha256")
        iid = it.get("item_id")
        order.append(iid)
        if sha in known:
            skipped += 1
            continue
        size = int(it.get("size", 0) or 0)
        if size > auto_limit_bytes:
            manual_required.append(iid)
        else:
            to_request.append(iid)
        # A later duplicate of the same new content is only requested once.
        known.add(sha)
    return {
        "to_request": to_request,
        "skipped_existing": skipped,
        "manual_required": manual_required,
        "order": order,
    }


def build_sync_result(received, skipped_existing, manual_required, failed):
    return {
        "type": "clipboard_sync_result",
        "received": int(received),
        "skipped_existing": int(skipped_existing),
        "manual_required": int(manual_required),
        "failed": int(failed),
    }


# ── History limits + eviction ───────────────────────────────────────
def eviction_plan(items, max_items, max_total_bytes):
    """Return the list of item_ids to evict to satisfy the limits.

    FIFO: oldest first (by ``seq``), never evicting pinned items. First enforce
    the item-count cap, then the total-size cap. Pinned items are always kept
    even if that exceeds a limit (documented behaviour).
    """
    ordered = sorted(items, key=lambda it: it.get("seq", 0))
    evict = []
    evict_ids = set()

    # 1) count cap
    non_pinned = [it for it in ordered if not it.get("pinned")]
    count = len(ordered)
    for it in non_pinned:
        if count <= max_items:
            break
        evict.append(it)
        evict_ids.add(it["item_id"])
        count -= 1

    # 2) size cap (over the remaining, still oldest-first, non-pinned)
    remaining = [it for it in ordered if it["item_id"] not in evict_ids]
    total = sum(int(it.get("size", 0) or 0) for it in remaining)
    for it in remaining:
        if total <= max_total_bytes:
            break
        if it.get("pinned"):
            continue
        evict.append(it)
        evict_ids.add(it["item_id"])
        total -= int(it.get("size", 0) or 0)

    return [it["item_id"] for it in evict]


# ── Formatting: bytes / rate / ETA / progress ───────────────────────
def format_bytes(n, unit="auto"):
    n = max(0, int(n))
    if unit == "byte":
        return f"{n} B"
    if unit == "KB":
        return f"{n / 1e3:.1f} KB"
    if unit == "MB":
        return f"{n / 1e6:.1f} MB"
    if unit == "KiB":
        return f"{n / 1024:.1f} KiB"
    if unit == "MiB":
        return f"{n / 1024 / 1024:.1f} MiB"
    # auto (decimal)
    if n < 1e3:
        return f"{n} B"
    if n < 1e6:
        return f"{n / 1e3:.1f} KB"
    if n < 1e9:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1e9:.1f} GB"


def format_rate(bps, unit="auto"):
    bps = max(0.0, float(bps))
    if unit == "B/s":
        return f"{int(bps)} B/s"
    if unit == "KB/s":
        return f"{bps / 1e3:.1f} KB/s"
    if unit == "MB/s":
        return f"{bps / 1e6:.1f} MB/s"
    if unit == "KiB/s":
        return f"{bps / 1024:.1f} KiB/s"
    if unit == "MiB/s":
        return f"{bps / 1024 / 1024:.1f} MiB/s"
    if bps < 1e3:
        return f"{int(bps)} B/s"
    if bps < 1e6:
        return f"{bps / 1e3:.1f} KB/s"
    if bps < 1e9:
        return f"{bps / 1e6:.1f} MB/s"
    return f"{bps / 1e9:.1f} GB/s"


def format_eta(seconds):
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def progress_percent(done, total):
    if total <= 0:
        return 100.0
    return max(0.0, min(100.0, done * 100.0 / total))


def format_progress(done, total, rate_bps=0.0, byte_unit="auto", rate_unit="auto"):
    pct = progress_percent(done, total)
    eta = None
    if rate_bps > 0 and total > done:
        eta = (total - done) / rate_bps
    return (f"{format_bytes(done, byte_unit)} / {format_bytes(total, byte_unit)} "
            f"({pct:.1f}%) | {format_rate(rate_bps, rate_unit)} | ETA {format_eta(eta)}")


# ── ZIP strategy decision ───────────────────────────────────────────
def is_compressible_ext(name):
    name = str(name or "").lower()
    dot = name.rfind(".")
    ext = name[dot:] if dot >= 0 else ""
    return ext not in ALREADY_COMPRESSED_EXTS


def zip_strategy(file_count, total_size, compressible_ratio, ram_free, disk_free,
                 ram_zip_limit=256 * 1024 * 1024, mode="auto"):
    """Decide how to move a file item.

    Returns one of: 'direct' (single file), 'multi' (many files, no zip benefit),
    'zip_ram' (stream a zip from RAM), 'zip_disk' (build a temp zip on disk).
    """
    file_count = int(file_count)
    total_size = int(total_size)
    if mode == "never":
        return "direct" if file_count <= 1 else "multi"
    if file_count <= 1:
        return "direct"
    # Many files.
    mostly_compressed = compressible_ratio < 0.5
    if mode == "auto" and mostly_compressed:
        # jpg/mp3 etc: zipping wastes CPU, send as a multi-file transfer.
        return "multi"
    # Compressible batch (or forced): prefer RAM zip when it fits, else disk.
    if total_size <= ram_zip_limit and total_size <= max(0, ram_free) // 2:
        return "zip_ram"
    if disk_free >= total_size * 2:   # need room for the temp zip
        return "zip_disk"
    # Not enough disk for a temp zip: fall back to multi-file.
    return "multi"


# ── Chunk planning ──────────────────────────────────────────────────
DEFAULT_CHUNK_SIZE = 1 * 1024 * 1024   # 1 MiB


def default_chunk_size(max_frame_size):
    """A safe chunk size that stays under the frame limit even after base64."""
    # base64 inflates ~4/3; leave head-room for JSON envelope too.
    safe = int((max_frame_size - 4096) * 3 // 4)
    return max(64 * 1024, min(DEFAULT_CHUNK_SIZE, safe))


def chunk_plan(total_size, chunk_size=DEFAULT_CHUNK_SIZE):
    total_size = max(0, int(total_size))
    chunk_size = max(1, int(chunk_size))
    plan = []
    idx = 0
    offset = 0
    while offset < total_size:
        length = min(chunk_size, total_size - offset)
        plan.append({"index": idx, "offset": offset, "length": length})
        offset += length
        idx += 1
    return plan


def chunk_count(total_size, chunk_size=DEFAULT_CHUNK_SIZE):
    chunk_size = max(1, int(chunk_size))
    total_size = max(0, int(total_size))
    return (total_size + chunk_size - 1) // chunk_size


# ── Disk-space guard ────────────────────────────────────────────────
def has_enough_space(needed_bytes, free_bytes, margin_bytes=64 * 1024 * 1024):
    """True if there is room for ``needed_bytes`` plus a safety margin."""
    return int(free_bytes) >= int(needed_bytes) + int(margin_bytes)
