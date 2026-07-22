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
import re
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
ITEM_SCHEMA_VERSION = 1
PAYLOAD_STATES = (
    "metadata_only", "source_available", "cached", "materialized",
    "receiving", "missing", "failed",
)
_SAFE_ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _bounded_string(value, field, maximum, allow_empty=True, multiline=False):
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value):
        raise ValueError(f"invalid clipboard {field}")
    allowed_controls = "\r\n\t" if multiline else ""
    if any(ord(char) < 32 and char not in allowed_controls for char in value):
        raise ValueError(f"invalid clipboard {field}")
    return value


def _nonnegative_int(value, field):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"invalid clipboard {field}")
    return value


def is_valid_item_id(value):
    return isinstance(value, str) and bool(_SAFE_ITEM_ID.fullmatch(value))


def is_valid_sha256(value):
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


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
    "cache_received_payloads": True,
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


def version_item(item, origin_device_id="", origin_event_id=None, payload_state=None):
    """Return an additive schema-v1 item without discarding unknown fields."""
    if not isinstance(item, dict):
        raise ValueError("clipboard item must be an object")
    version = item.get("schema_version", ITEM_SCHEMA_VERSION)
    if (not isinstance(version, int) or isinstance(version, bool)
            or version < 0 or version > ITEM_SCHEMA_VERSION):
        raise ValueError(f"unsupported clipboard item schema: {version!r}")

    out = dict(item)
    out["schema_version"] = ITEM_SCHEMA_VERSION
    item_id = str(out.get("item_id") or new_item_id())
    if not _SAFE_ITEM_ID.fullmatch(item_id):
        raise ValueError("invalid clipboard item_id")
    out["item_id"] = item_id
    if out.get("kind") not in CLIP_KINDS:
        raise ValueError("invalid clipboard kind")
    out["mime"] = _bounded_string(out.get("mime", "application/octet-stream"),
                                   "mime", 255, allow_empty=False)
    out["display_name"] = _bounded_string(out.get("display_name", ""),
                                           "display_name", 512)
    out["preview_text"] = _bounded_string(out.get("preview_text", ""),
                                           "preview_text", PREVIEW_TEXT_MAX, multiline=True)
    out["preview_hash"] = _bounded_string(out.get("preview_hash", ""),
                                           "preview_hash", 64)
    out["size"] = _nonnegative_int(out.get("size", 0), "size")
    out["seq"] = _nonnegative_int(out.get("seq", 0), "seq")
    out["file_count"] = _nonnegative_int(out.get("file_count", 0), "file_count")
    out["total_file_size"] = _nonnegative_int(
        out.get("total_file_size", 0), "total_file_size")
    created_at = out.get("created_at")
    if not isinstance(created_at, (int, float)) or isinstance(created_at, bool):
        created_at = _now()
    out["created_at"] = float(created_at)

    origin = out.get("origin")
    origin = dict(origin) if isinstance(origin, dict) else {}
    origin["device_id"] = _bounded_string(
        str(origin.get("device_id") or origin_device_id or ""), "origin device_id", 128)
    if origin_event_id is not None:
        origin["event_id"] = str(origin_event_id)
    else:
        origin["event_id"] = str(origin.get("event_id") or item_id)
    if not _SAFE_ITEM_ID.fullmatch(origin["event_id"]):
        raise ValueError("invalid clipboard origin event_id")
    captured_at = origin.get("captured_at")
    if not isinstance(captured_at, (int, float)) or isinstance(captured_at, bool):
        captured_at = created_at
    origin["captured_at"] = float(captured_at)
    out["origin"] = origin

    payload = out.get("payload")
    payload = dict(payload) if isinstance(payload, dict) else {}
    content_sha = str(out.get("sha256") or payload.get("content_sha256") or "")
    if not _SHA256.fullmatch(content_sha):
        raise ValueError("invalid clipboard sha256")
    out["sha256"] = content_sha
    payload["content_sha256"] = content_sha
    is_file = out.get("kind") in (KIND_FILE, KIND_FILE_BATCH)
    payload.setdefault("encoding", "deterministic_zip" if is_file else "raw")
    if payload["encoding"] not in ("raw", "deterministic_zip"):
        raise ValueError("invalid clipboard payload encoding")
    if "sha256" not in payload:
        payload["sha256"] = None if is_file else content_sha
    if "size" not in payload:
        payload["size"] = None if is_file else int(out.get("size", 0) or 0)
    if payload.get("sha256") is not None and not _SHA256.fullmatch(str(payload["sha256"])):
        raise ValueError("invalid clipboard payload sha256")
    if payload.get("size") is not None:
        payload["size"] = _nonnegative_int(payload["size"], "payload size")
    if payload["encoding"] == "raw":
        if payload.get("sha256") is not None and payload["sha256"].lower() != content_sha.lower():
            raise ValueError("raw clipboard payload hash differs from content hash")
        if payload.get("size") is not None and payload["size"] != out["size"]:
            raise ValueError("raw clipboard payload size differs from item size")
    out["payload"] = payload

    providers = out.get("providers")
    if providers is not None and not isinstance(providers, list):
        raise ValueError("invalid clipboard providers")
    out["providers"] = [dict(provider) for provider in (providers or [])
                        if isinstance(provider, dict)]
    if len(out["providers"]) != len(providers or []) or len(out["providers"]) > 64:
        raise ValueError("invalid clipboard providers")
    for provider in out["providers"]:
        _bounded_string(provider.get("device_id", ""), "provider device_id", 128,
                        allow_empty=False)
        if provider.get("state") not in ("available", "stale", "unavailable"):
            raise ValueError("invalid clipboard provider state")
        if provider.get("payload_sha256") is not None \
                and not is_valid_sha256(provider.get("payload_sha256")):
            raise ValueError("invalid clipboard provider payload_sha256")
        if provider.get("payload_size") is not None:
            _nonnegative_int(provider.get("payload_size"), "provider payload_size")
        last_seen = provider.get("last_seen_at")
        if (not isinstance(last_seen, (int, float)) or isinstance(last_seen, bool)
                or last_seen < 0):
            raise ValueError("invalid clipboard provider last_seen_at")
    if out.get("metadata") is not None and not isinstance(out.get("metadata"), dict):
        raise ValueError("invalid clipboard metadata")
    state = payload_state if payload_state is not None else out.get("payload_state")
    if state not in PAYLOAD_STATES:
        state = "source_available" if out.get("available") else "metadata_only"
    out["payload_state"] = state
    out["available"] = state in ("source_available", "cached", "materialized")
    return out


# ── Item construction ───────────────────────────────────────────────
def _now():
    return time.time()


def make_text_item(text, seq, kind=KIND_TEXT, mime="text/plain", created_at=None):
    data = text.encode("utf-8")
    return version_item({
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
    }, payload_state="source_available")


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
    return version_item({
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
    }, payload_state="source_available")


def make_binary_item(sha256, size, seq, kind=KIND_BINARY, mime="application/octet-stream",
                     display_name="", created_at=None, file_count=0, total_file_size=0,
                     available=True, preview_text="", preview_hash=""):
    return version_item({
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
    }, payload_state="source_available" if available else "metadata_only")


_MANIFEST_FIELDS = ("schema_version", "item_id", "sha256", "kind", "mime", "size", "created_at",
                     "seq", "display_name", "preview_text", "preview_hash",
                     "file_count", "total_file_size", "available", "origin",
                     "payload", "providers", "payload_state", "metadata")


def manifest_item(item):
    """Reduce a store item to the metadata that goes into a manifest (no data)."""
    versioned = version_item(item)
    result = {k: versioned.get(k) for k in _MANIFEST_FIELDS}
    result["origin"] = {key: versioned["origin"].get(key)
                        for key in ("device_id", "event_id", "captured_at")}
    result["payload"] = {key: versioned["payload"].get(key)
                         for key in ("content_sha256", "encoding", "sha256", "size")}
    result["providers"] = [
        {key: provider.get(key) for key in (
            "device_id", "state", "last_seen_at", "payload_sha256", "payload_size"
        ) if key in provider}
        for provider in versioned.get("providers", [])
    ]
    metadata = versioned.get("metadata") or {}
    result["metadata"] = {}
    if "has_html" in metadata:
        result["metadata"]["has_html"] = bool(metadata["has_html"])
    source_url = metadata.get("source_url")
    if (isinstance(source_url, str) and len(source_url) <= 2048
            and source_url.lower().startswith(("https://", "http://"))):
        result["metadata"]["source_url"] = source_url
    return result


def build_manifest(profile_id, device_id, revision, items, current_item_id=None):
    return {
        "type": "clipboard_manifest",
        "schema_version": ITEM_SCHEMA_VERSION,
        "profile_id": profile_id,
        "device_id": device_id,
        "history_revision": int(revision),
        "current_item_id": current_item_id,
        "items": [manifest_item(it) for it in items],
    }


def parse_manifest(msg):
    if not isinstance(msg, dict) or msg.get("type") != "clipboard_manifest":
        return None
    version = msg.get("schema_version", 0)
    if (not isinstance(version, int) or isinstance(version, bool)
            or version < 0 or version > ITEM_SCHEMA_VERSION):
        return None
    try:
        _bounded_string(msg.get("profile_id", ""), "profile_id", 256, allow_empty=False)
        _bounded_string(msg.get("device_id", ""), "device_id", 128, allow_empty=False)
        revision = _nonnegative_int(msg.get("history_revision", 0), "history_revision")
        current_item_id = msg.get("current_item_id")
        if current_item_id is not None and not _SAFE_ITEM_ID.fullmatch(str(current_item_id)):
            raise ValueError("invalid clipboard current_item_id")
        if not isinstance(msg.get("items", []), list) or len(msg.get("items", [])) > 999:
            raise ValueError("invalid clipboard manifest items")
        items = [version_item(item) for item in list(msg.get("items", []))]
    except (TypeError, ValueError):
        return None
    return {
        "schema_version": version,
        "profile_id": msg.get("profile_id"),
        "device_id": msg.get("device_id"),
        "history_revision": revision,
        "current_item_id": current_item_id,
        "items": items,
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
# ── Received cache entry ──────────────────────────────────────────
_CACHE_ENTRY_FIELDS = (
    "content_sha256", "payload_sha256", "payload_size",
    "received_at", "last_access", "providers",
)


def make_cache_entry(content_sha256, payload_sha256=None, payload_size=None,
                     providers=None, received_at=None):
    if not is_valid_sha256(content_sha256):
        raise ValueError("invalid cache entry content_sha256")
    if payload_sha256 is not None and not is_valid_sha256(payload_sha256):
        raise ValueError("invalid cache entry payload_sha256")
    if payload_size is not None:
        _nonnegative_int(payload_size, "cache entry payload_size")
    now = _now()
    stamp = received_at if isinstance(received_at, (int, float)) and not isinstance(received_at, bool) else now
    entry = {
        "content_sha256": content_sha256,
        "payload_sha256": payload_sha256,
        "payload_size": payload_size,
        "received_at": stamp,
        "last_access": stamp,
    }
    if providers:
        if not isinstance(providers, list) or len(providers) > 64:
            raise ValueError("invalid cache entry providers")
        entry["providers"] = [dict(p) for p in providers if isinstance(p, dict)]
    return entry


def validate_cache_entry(entry):
    if not isinstance(entry, dict):
        return None
    result = {}
    for key in _CACHE_ENTRY_FIELDS:
        if key not in entry:
            continue
        value = entry[key]
        if key == "content_sha256":
            if not is_valid_sha256(value):
                return None
        elif key == "payload_sha256":
            if value is not None and not is_valid_sha256(value):
                return None
        elif key == "payload_size":
            if (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                return None
        elif key in ("received_at", "last_access"):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                return None
        elif key == "providers":
            if not isinstance(value, list) or len(value) > 64:
                return None
            value = [dict(p) for p in value if isinstance(p, dict)]
        result[key] = value
    return result


def evictable_cache_entries(entries, protected_hashes):
    return [(key, entry) for key, entry in sorted(
        entries.items(), key=lambda kv: kv[1].get("last_access", 0))
            if key not in protected_hashes]


# ── Materialization lease ─────────────────────────────────────────
LEASE_ACTIVE = "active"
LEASE_STALE = "stale"
LEASE_RELEASED = "released"
LEASE_STATES = (LEASE_ACTIVE, LEASE_STALE, LEASE_RELEASED)
DEFAULT_LEASE_CLEANUP_AGE_HOURS = 24
_LEASE_FIELDS = (
    "profile_id", "item_id", "dest_path", "created_at", "last_access",
    "owner_sequence", "state",
)


def make_lease(profile_id, item_id, dest_path):
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError("invalid lease profile_id")
    if not is_valid_item_id(item_id):
        raise ValueError("invalid lease item_id")
    if not isinstance(dest_path, str) or not dest_path:
        raise ValueError("invalid lease dest_path")
    now = _now()
    return {
        "profile_id": profile_id,
        "item_id": item_id,
        "dest_path": dest_path,
        "created_at": now,
        "last_access": now,
        "owner_sequence": None,
        "state": LEASE_ACTIVE,
    }


def validate_lease(entry):
    if not isinstance(entry, dict):
        return None
    result = {}
    for key in _LEASE_FIELDS:
        if key not in entry:
            continue
        value = entry[key]
        if key == "profile_id":
            if not isinstance(value, str) or not value:
                return None
        elif key == "item_id":
            if not is_valid_item_id(value):
                return None
        elif key == "dest_path":
            if not isinstance(value, str) or not value:
                return None
        elif key in ("created_at", "last_access"):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                return None
        elif key == "owner_sequence":
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                return None
        elif key == "state":
            if value not in LEASE_STATES:
                return None
        result[key] = value
    return result


def lease_stale_cutoff(max_age_hours=None):
    if max_age_hours is None:
        max_age_hours = DEFAULT_LEASE_CLEANUP_AGE_HOURS
    return _now() - max_age_hours * 3600


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


# ── Transfer preflight ─────────────────────────────────────────────---
PREFLIGHT_REJECTIONS = frozenset({
    "metadata_missing", "payload_missing", "too_large", "disk_full",
    "policy", "busy", "shutting_down", "invalid_size_metadata",
    "destination_unavailable", "preflight_error",
})
PREFLIGHT_SAFETY_MARGIN_BYTES = 512 * 1024 * 1024  # 512 MiB


def preflight_safety_margin(peak_bytes):
    return max(PREFLIGHT_SAFETY_MARGIN_BYTES, peak_bytes // 20)


def compute_transfer_preflight(
    payload_size,
    free_bytes,
    encoding="raw",
    known_transfer_size=None,
    logical_size=None,
    file_count=0,
    already_cached_bytes=0,
    materialized_size=0,
    hard_item_bytes=None,
    auto_limit_bytes=None,
    allow_manual=False,
):
    """Compute whether a transfer can proceed safely.

    Returns a dict with keys:
        allowed, required_download_bytes, required_temporary_bytes,
        required_materialized_bytes, peak_required_bytes, free_bytes,
        safety_margin_bytes, reason
    """
    if not isinstance(payload_size, int) or isinstance(payload_size, bool) or payload_size < 0:
        return {"allowed": False, "reason": "invalid_size_metadata"}
    if not isinstance(free_bytes, int) or isinstance(free_bytes, bool) or free_bytes < 0:
        return {"allowed": False, "reason": "destination_unavailable"}

    required_download = max(0, payload_size - already_cached_bytes)
    required_download = min(required_download, payload_size)

    if encoding == "deterministic_zip":
        if known_transfer_size is not None and known_transfer_size >= 0:
            transfer_bytes = int(known_transfer_size)
        elif logical_size is not None and logical_size >= 0:
            transfer_bytes = int(logical_size)
        else:
            transfer_bytes = payload_size
        required_temporary = transfer_bytes
        if already_cached_bytes >= transfer_bytes:
            required_temporary = 0
        peak_required = required_download + required_temporary + materialized_size
    else:
        required_temporary = 0
        peak_required = required_download + materialized_size

    required_materialized = materialized_size
    margin = preflight_safety_margin(peak_required)
    total_needed = peak_required + margin

    if hard_item_bytes is not None and payload_size > hard_item_bytes:
        return {"allowed": False, "reason": "too_large",
                "required_download_bytes": required_download,
                "required_temporary_bytes": required_temporary,
                "required_materialized_bytes": required_materialized,
                "peak_required_bytes": peak_required, "free_bytes": free_bytes,
                "safety_margin_bytes": margin}

    if auto_limit_bytes is not None and not allow_manual and payload_size > auto_limit_bytes:
        return {"allowed": False, "reason": "policy",
                "required_download_bytes": required_download,
                "required_temporary_bytes": required_temporary,
                "required_materialized_bytes": required_materialized,
                "peak_required_bytes": peak_required, "free_bytes": free_bytes,
                "safety_margin_bytes": margin}

    if free_bytes < total_needed:
        return {"allowed": False, "reason": "disk_full",
                "required_download_bytes": required_download,
                "required_temporary_bytes": required_temporary,
                "required_materialized_bytes": required_materialized,
                "peak_required_bytes": peak_required, "free_bytes": free_bytes,
                "safety_margin_bytes": margin}

    return {"allowed": True, "reason": None,
            "required_download_bytes": required_download,
            "required_temporary_bytes": required_temporary,
            "required_materialized_bytes": required_materialized,
            "peak_required_bytes": peak_required, "free_bytes": free_bytes,
            "safety_margin_bytes": margin}
