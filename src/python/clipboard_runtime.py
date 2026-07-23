"""FlowShift clipboard runtime manager (Layer 2: sync + transfer orchestration).

Wires the tested foundation (clipboard_model / clipboard_store /
clipboard_protocol) into a working per-profile sync that runs over the existing
framed peer link. It is deliberately transport-agnostic: it never touches sockets
directly — the runtime passes a ``send_fn(identity, msg)`` callback and routes
incoming ``clipboard_*`` messages to ``handle(identity, msg)``. That keeps this
module pure enough to unit-test two managers against each other in memory
(``test_clipboard_sync.py``) with no Windows APIs and no network.

Responsibilities:
  * one ClipboardStore per peer/profile identity (lazy)
  * capture a local clipboard text/blob item into the relevant store(s)
  * on profile activation: send our manifest so the peer pulls what it lacks
  * on manifest: diff, request only-missing (auto) / mark large ones manual
  * on request: stream the requested items as chunked transfers
  * on transfer: reassemble (resume/hash-verified) and store, marking available
"""
from __future__ import annotations

import copy
from collections import deque
import os
import tempfile
import threading
import time
import uuid

import uuid as _uuid
import clipboard_model as cbm
import clipboard_events as cbe
import clipboard_protocol as cbp
import clipboard_files as cf
import clipboard_sources as csrc
import clipboard_image as ci
import clipboard_html as chm
import clipboard_preview as cpv
import clipboard_transfer as ctt
from clipboard_store import ClipboardStore, profile_dir_name


class ClipboardManager:
    def __init__(self, store_root, device_id, send_fn, settings_fn, log_fn=None):
        self.store_root = store_root
        self.device_id = device_id
        self.send_fn = send_fn            # send_fn(identity, msg)
        self.settings_fn = settings_fn    # -> clipboard settings dict
        self.log = log_fn or (lambda level, msg: None)
        self._stores = {}
        self._lock = threading.Lock()
        self._assemblers = {}             # transfer_id -> {identity, meta, asm}
        self._remote_meta = {}            # identity -> {item_id -> manifest item}
        self._remote_current = {}         # identity -> current remote item_id
        self._remote_revision = {}        # identity -> latest accepted manifest revision
        self._seen_announcements = {}     # identity -> bounded announcement IDs
        self._pending_announcements = {}  # identity -> bounded sent announcement IDs
        self._providers = {}              # device_id -> {state, last_seen_at, identity, item_count}
        self._announcement_apply_lock = threading.Lock()
        self._jobs = {}                   # item_id -> TransferJob
        self._temp_cleanup_done = False
        self._accepting_work = True
        self._update_maintenance = False
        self._shutting_down = False
        self._shutdown_complete = False
        self._active_local_operations = 0
        self._activity_changed = threading.Condition(self._lock)
        self.stats = {"sent_items": 0, "received_items": 0, "failed": 0,
                      "announcements_sent": 0, "announcements_received": 0,
                      "announcement_acks": 0}
        self._write_suppressor = cbe.ClipboardWriteSuppressor()
        self._windows_write_lock = threading.Lock()
        self._provider_enabled = True
        self._pending_preflight = {}       # request_id -> threading.Event
        self._pending_preflight_result = {}  # request_id -> preflight response
        self._preflight_approved = set()   # (identity, item_id) tuples approved via handshake
        self._transfer_queue = ctt.TransferQueue(
            max_parallel=self._settings().get("clipboard_transfer_max_parallel", 1),
            retry_delay_ms=self._settings().get("clipboard_transfer_retry_delay_ms", 500),
            log_fn=self.log,
        )
        self._cleanup_temp_roots()

    def _begin_local_operation(self):
        with self._lock:
            if not self._accepting_work or self._update_maintenance:
                return False
            self._active_local_operations += 1
            return True

    def _begin_incoming_operation(self, msg):
        with self._lock:
            if not self._accepting_work:
                return False
            if self._update_maintenance:
                message_type = msg.get("type")
                transfer_id = msg.get("transfer_id")
                continuation = (message_type in {cbp.T_CHUNK, cbp.T_COMPLETE}
                                and transfer_id in self._assemblers)
                if not continuation:
                    return False
            self._active_local_operations += 1
            return True

    def _end_local_operation(self):
        with self._lock:
            self._active_local_operations = max(0, self._active_local_operations - 1)
            self._activity_changed.notify_all()

    def set_update_maintenance(self, enabled):
        """Atomically stop or resume admission without cancelling existing work."""
        with self._lock:
            if enabled:
                self._update_maintenance = True
            elif not self._shutting_down:
                self._update_maintenance = False
            return self._update_maintenance

    # ── stores ──────────────────────────────────────────────────────
    def store(self, identity):
        with self._lock:
            st = self._stores.get(identity)
            if st is None:
                st = ClipboardStore(self.store_root, profile_dir_name(identity))
                self._stores[identity] = st
                try:
                    st.cleanup_temp(self._settings().get("temp_cleanup_max_age_hours", 24))
                except Exception:
                    pass
            return st

    def _cleanup_temp_roots(self):
        if self._temp_cleanup_done:
            return
        max_age = self._settings().get("temp_cleanup_max_age_hours", 24)
        try:
            incoming = os.path.join(self.store_root, "temp", "incoming")
            csrc.cleanup_temp_tree(incoming, max_age_hours=max_age)
        except Exception:
            pass
        self._temp_cleanup_done = True

    def _settings(self):
        s = self.settings_fn() or {}
        # Accept either an already-normalised dict or a raw clipboard block.
        if isinstance(s, dict) and "history_max_items" in s and "enabled" in s:
            return s
        return cbm.clipboard_settings({"clipboard": s})

    def enabled(self):
        return bool(self._settings().get("enabled"))

    def _enforce(self):
        s = self._settings()
        return (int(s["history_max_items"]), int(float(s["history_max_total_gb"]) * 1e9))

    def _hard_item_bytes(self):
        return int(float(self._settings().get("max_item_gb", 50.0)) * 1e9)

    def _capture_cancelled(self):
        with self._lock:
            return self._shutting_down

    def _with_local_provider(self, item):
        it = copy.deepcopy(item)
        providers = [provider for provider in it.get("providers", [])
                     if provider.get("device_id") != self.device_id]
        if self.device_id:
            payload = it.get("payload") or {}
            state = "available" if self._provider_enabled else "unavailable"
            provider = {"device_id": self.device_id, "state": state,
                        "last_seen_at": time.time()}
            if payload.get("sha256"):
                provider["payload_sha256"] = payload["sha256"]
            if payload.get("size") is not None:
                provider["payload_size"] = payload["size"]
            providers.append(provider)
        it["providers"] = providers
        return cbm.version_item(it)

    def _update_provider_state(self, device_id, state, identity=None):
        with self._lock:
            entry = self._providers.setdefault(device_id, {
                "state": state, "last_seen_at": time.time(),
                "identity": identity, "item_count": 0,
            })
            if state == "available":
                entry["last_seen_at"] = time.time()
                entry["state"] = state
                if identity is not None:
                    entry["identity"] = identity
            elif state == "unconfirmed":
                entry["state"] = "unconfirmed"
                entry["last_seen_at"] = time.time()
                if identity is not None:
                    entry["identity"] = identity
            elif state == "offline":
                entry["state"] = "offline"
                entry["last_seen_at"] = time.time()
            elif state == "stale":
                if entry.get("state") not in ("invalid",):
                    entry["state"] = "stale"
                    entry["last_seen_at"] = time.time()
            elif state == "invalid":
                entry["state"] = "invalid"
                entry["last_seen_at"] = time.time()

    def provider_registry(self):
        with self._lock:
            return {d: dict(e) for d, e in self._providers.items()}

    def on_peer_connected(self, device_id, identity):
        """Peer reconnected — set unconfirmed until manifest confirms availability."""
        self._update_provider_state(device_id, "unconfirmed", identity=identity)

    def on_peer_disconnected(self, device_id):
        self._update_provider_state(device_id, "offline")

    def _register_remote_providers(self, identity, item):
        for provider in item.get("providers", []):
            did = provider.get("device_id")
            if did:
                self._update_provider_state(did, "available", identity=identity)
                with self._lock:
                    entry = self._providers[did]
                    entry["item_count"] += 1

    def _announce_capture(self, identity, item):
        if not self.device_id:
            return
        st = self.store(identity)
        publication = next((candidate for candidate in st.build_manifest(self.device_id)["items"]
                            if candidate.get("item_id") == item.get("item_id")), None)
        if publication is None:
            return
        message = cbp.build_announcement(
            uuid.uuid4().hex, st.profile_id, self.device_id, st.revision,
            st.current_item_id, publication)
        with self._lock:
            pending = self._pending_announcements.setdefault(identity, deque(maxlen=256))
            pending.append(message["announcement_id"])
        self.send_fn(identity, message)
        self.stats["announcements_sent"] += 1

    @staticmethod
    def _utf8_size_within_limit(text, limit):
        total = 0
        for offset in range(0, len(text), 4096):
            total += len(text[offset:offset + 4096].encode("utf-8"))
            if total > limit:
                return False
        return True

    def _transfer_settings(self):
        s = self._settings()
        return {
            "max_parallel": int(s.get("clipboard_transfer_max_parallel", 1) or 1),
            "max_retries": int(s.get("clipboard_transfer_max_retries",
                                       s.get("max_retries", 5)) or 5),
            "retry_delay_ms": int(s.get("clipboard_transfer_retry_delay_ms", 500) or 500),
            "max_kib_per_sec": int(s.get("clipboard_max_transfer_kib_per_sec", 0) or 0),
            "ram_zip_limit_bytes": int(s.get("clipboard_ram_zip_limit_mb", 256) or 256) * 1024 * 1024,
            "disk_assembler_threshold_bytes": int(
                s.get("clipboard_disk_assembler_threshold_mb", 32) or 32) * 1024 * 1024,
            "temp_cleanup_max_age_hours": int(s.get("clipboard_temp_cleanup_max_age_hours", 24) or 24),
        }

    @staticmethod
    def _completed_indices(asm):
        if hasattr(asm, "completed_indices"):
            try:
                return list(asm.completed_indices())
            except Exception:
                return []
        if hasattr(asm, "_chunks"):
            try:
                return sorted(getattr(asm, "_chunks").keys())
            except Exception:
                return []
        return []

    @staticmethod
    def _error_code_for_exception(exc):
        msg = str(exc).lower()
        if "speicherplatz" in msg or "no space" in msg or "disk full" in msg:
            return cbp.ERR_DISK_FULL
        return cbp.ERR_ABORTED

    def _source_for_item(self, identity, item):
        st = self.store(identity)
        object_path = st.get_object_path_for_item(item.get("item_id"))
        if object_path:
            return csrc.FileTransferSource(object_path, item_id=item.get("item_id", ""),
                                           display_name=item.get("display_name", ""))

        if item.get("kind") in (cbm.KIND_FILE, cbm.KIND_FILE_BATCH):
            s = self._transfer_settings()
            estimate = int(item.get("size", 0) or 0)
            if estimate > s["ram_zip_limit_bytes"]:
                space = ctt.check_disk_space(st.temp_dir, estimate)
                if not space.get("ok"):
                    raise OSError("Nicht genug Speicherplatz")
                disk_free = space.get("free_bytes", 0)
            else:
                disk_free = None
            return cf.build_bundle_source(item, st.temp_dir, s["ram_zip_limit_bytes"],
                                          disk_free_bytes=disk_free)

        data = st.get_data(item.get("item_id"))
        if data is None:
            return None
        return csrc.BytesTransferSource(data, item_id=item.get("item_id", ""),
                                        display_name=item.get("display_name", ""))

    def _start_transfer_queue(self):
        # Recreate with current settings if needed (tests can swap settings_fn).
        s = self._transfer_settings()
        self._transfer_queue = ctt.TransferQueue(
            max_parallel=s["max_parallel"],
            retry_delay_ms=s["retry_delay_ms"],
            log_fn=self.log,
        )

    def _register_job(self, job):
        with self._lock:
            self._jobs[job.item_id] = job
        return job

    def _job_for_item(self, item_id):
        with self._lock:
            return self._jobs.get(item_id)

    def _make_job_from_item(self, identity, item, direction, status=None, error=None,
                            manual_required=False, transfer_id=None, chunk_count=0):
        s = self._transfer_settings()
        job = ctt.make_transfer_job(
            transfer_id=transfer_id or uuid.uuid4().hex,
            profile_id=profile_dir_name(identity),
            item_id=item.get("item_id", ""),
            direction=direction,
            kind=item.get("kind", cbm.KIND_BINARY),
            display_name=item.get("display_name", ""),
            total_bytes=int(item.get("size", 0) or 0),
            chunk_count=chunk_count,
            max_retries=s["max_retries"],
            manual_required=manual_required,
            status=status,
        )
        if error:
            job.error = str(error)
        return self._register_job(job)

    def _write_placeholder_status(self, item, status, error=None):
        md = dict(item.get("metadata", {}) or {})
        md["transfer_status"] = status
        if error:
            md["transfer_error"] = str(error)
        elif "transfer_error" in md:
            md.pop("transfer_error", None)
        item["metadata"] = md
        return item

    def _receive_preflight(self, identity, item, allow_manual=True):
        st = self.store(identity)
        payload = item.get("payload") or {}
        encoding = payload.get("encoding", "raw")
        payload_size = int(item.get("size", 0) or 0)
        logical_size = int(item.get("total_file_size", 0) or 0)
        file_count = int(item.get("file_count", 0) or 0)
        known_transfer_size = payload.get("size")
        content_sha = item.get("sha256", "")
        already_cached = 0
        if content_sha and (st.has_object(content_sha) or self._jobs.get(item.get("item_id"))):
            if known_transfer_size is not None:
                already_cached = int(known_transfer_size)
            else:
                already_cached = payload_size
        materialized = 0
        lease = st.get_lease(item.get("item_id", ""))
        if lease and lease.get("state") == cbm.LEASE_ACTIVE:
            materialized = logical_size or payload_size
        hard = self._hard_item_bytes()
        auto_limit = int(self._settings().get("max_auto_transfer_mb", 100)) * 1024 * 1024
        space = ctt.check_disk_space(st.dir, payload_size)
        free = int(space.get("free_bytes", 0))
        return cbm.compute_transfer_preflight(
            payload_size=payload_size,
            free_bytes=free,
            encoding=encoding,
            known_transfer_size=known_transfer_size,
            logical_size=logical_size,
            file_count=file_count,
            already_cached_bytes=already_cached,
            materialized_size=materialized,
            hard_item_bytes=hard,
            auto_limit_bytes=auto_limit,
            allow_manual=allow_manual,
        )

    def _can_request_item(self, identity, meta):
        pre = self._receive_preflight(identity, meta, allow_manual=False)
        if not pre["allowed"]:
            return {"ok": False, "required_bytes": pre["required_download_bytes"],
                    "free_bytes": pre["free_bytes"],
                    "missing_bytes": max(0, pre["peak_required_bytes"] + pre["safety_margin_bytes"]
                                         - pre["free_bytes"]),
                    "reason": pre["reason"]}
        st = self.store(identity)
        required = int(meta.get("size", 0) or 0)
        return ctt.check_disk_space(st.dir, required)

    def _queue_send_item(self, identity, item_id, resume_from=0, send_start=True):
        if not self._begin_local_operation():
            return None
        try:
            st = self.store(identity)
            item = st.get_item(item_id)
            if not item:
                return None
            data_size = int(item.get("size", 0) or 0)
            chunk_size = cbp.safe_chunk_size()
            chunk_count = cbm.chunk_count(data_size, chunk_size)
            job = self._make_job_from_item(identity, item, "send",
                                           status=ctt.TransferStatus.pending,
                                           chunk_count=chunk_count)

            def _work(current_job):
                self._send_transfer(identity, item_id, current_job, resume_from=resume_from,
                                    send_start=send_start)

            if not self._transfer_queue.submit(job, _work):
                ctt.mark_failed(job, "transfer queue full")
            return job
        finally:
            self._end_local_operation()

    # ── local capture ───────────────────────────────────────────────
    def capture_text(self, identity, text, origin_event_id=None):
        """Add a captured local text copy to the store for ``identity``.

        Skips if the newest item already has the same content (no dup on repeat).
        Returns the stored item or None.
        """
        if (not text or not self._utf8_size_within_limit(text, self._hard_item_bytes())
                or not self._begin_local_operation()):
            return None
        try:
            st = self.store(identity)
            item = cbm.version_item(cbm.make_text_item(text, seq=0),
                                    origin_device_id=self.device_id,
                                    origin_event_id=origin_event_id)
            item = self._with_local_provider(item)
            self._register_remote_providers(identity, item)
            items = st.list_items()
            if items and items[-1].get("sha256") == item["sha256"]:
                st.set_current(items[-1]["item_id"])
                return None
            try:
                stored, _ = st.add_item(item, data=text.encode("utf-8"), enforce=self._enforce(),
                                        make_current=True)
            except OSError as exc:
                self.log("WARN", f"clipboard text capture failed for {identity}: {exc}")
                return None
            self.log("DEBUG", f"clipboard captured text -> {identity} ({len(text)} chars)")
            self._announce_capture(identity, stored)
            return stored
        finally:
            self._end_local_operation()

    def capture_text_all(self, identities, text):
        event_id = uuid.uuid4().hex
        for ident in identities:
            self.capture_text(ident, text, origin_event_id=event_id)

    def capture_files(self, identity, paths, origin_event_id=None):
        """Add a captured local file / file-batch item (metadata + source paths).

        No blob is stored yet: the transfer bundle (zip) is built lazily on
        request, and a local paste uses the original files without a copy.
        """
        if not self._begin_local_operation():
            return None
        try:
            item = cf.make_file_item(paths, max_total_bytes=self._hard_item_bytes(),
                                     cancelled=self._capture_cancelled)
            if not item:
                return None
            item = cbm.version_item(item, origin_device_id=self.device_id,
                                    origin_event_id=origin_event_id)
            item = self._with_local_provider(item)
            st = self.store(identity)
            items = st.list_items()
            if items and items[-1].get("sha256") == item["sha256"]:
                st.set_current(items[-1]["item_id"])
                return None
            try:
                stored, _ = st.add_item(item, data=None, enforce=self._enforce(), make_current=True)
            except OSError as exc:
                self.log("WARN", f"clipboard file capture failed for {identity}: {exc}")
                return None
            self.log("DEBUG", f"clipboard captured {item['file_count']} file(s) -> {identity}")
            self._announce_capture(identity, stored)
            return stored
        finally:
            self._end_local_operation()

    def capture_files_all(self, identities, paths):
        event_id = uuid.uuid4().hex
        for ident in identities:
            self.capture_files(ident, paths, origin_event_id=event_id)

    def capture_image(self, identity, bmp_bytes, origin_event_id=None):
        """Add a captured clipboard image (BMP bytes) to the store for identity."""
        if (not bmp_bytes or len(bmp_bytes) > self._hard_item_bytes()
                or not self._begin_local_operation()):
            return None
        try:
            st = self.store(identity)
            sha = cbm.sha256_bytes(bmp_bytes)
            items = st.list_items()
            if items and items[-1].get("sha256") == sha:
                st.set_current(items[-1]["item_id"])
                return None
            info = ci.parse_bmp(bmp_bytes) or {}
            w, h = info.get("width", 0), info.get("height", 0)
            item = cbm.make_binary_item(sha, len(bmp_bytes), seq=0, kind=cbm.KIND_IMAGE,
                                        mime="image/bmp", display_name=f"Bild {w}x{h}",
                                        available=True)
            item = cbm.version_item(item, origin_device_id=self.device_id,
                                    origin_event_id=origin_event_id)
            item = self._with_local_provider(item)
            try:
                stored, _ = st.add_item(item, data=bmp_bytes, enforce=self._enforce(),
                                        make_current=True)
            except OSError as exc:
                self.log("WARN", f"clipboard image capture failed for {identity}: {exc}")
                return None
            self.log("DEBUG", f"clipboard captured image {w}x{h} -> {identity}")
            self._announce_capture(identity, stored)
            return stored
        finally:
            self._end_local_operation()

    def capture_image_all(self, identities, bmp_bytes):
        event_id = uuid.uuid4().hex
        for ident in identities:
            self.capture_image(ident, bmp_bytes, origin_event_id=event_id)

    def capture_html(self, identity, cf_html_bytes, origin_event_id=None):
        """Add a captured local HTML copy to the store for ``identity``."""
        if (not cf_html_bytes or len(cf_html_bytes) > self._hard_item_bytes()
                or not self._begin_local_operation()):
            return None
        try:
            parsed = chm.parse_cf_html(cf_html_bytes)
            if not parsed:
                return None
            st = self.store(identity)
            item = cbm.make_html_item(
                cf_html_bytes,
                chm.html_to_preview_text(parsed.get("fragment") or parsed.get("html") or "",
                                         cbm.PREVIEW_TEXT_MAX),
                seq=0,
                source_url=parsed.get("source_url"),
            )
            item = cbm.version_item(item, origin_device_id=self.device_id,
                                    origin_event_id=origin_event_id)
            item = self._with_local_provider(item)
            items = st.list_items()
            if items and items[-1].get("sha256") == item["sha256"]:
                st.set_current(items[-1]["item_id"])
                return None
            try:
                stored, _ = st.add_item(item, data=cf_html_bytes, enforce=self._enforce(),
                                        make_current=True)
            except OSError as exc:
                self.log("WARN", f"clipboard html capture failed for {identity}: {exc}")
                return None
            self.log("DEBUG", f"clipboard captured html -> {identity} ({len(cf_html_bytes)} bytes)")
            self._announce_capture(identity, stored)
            return stored
        finally:
            self._end_local_operation()

    def capture_html_all(self, identities, cf_html_bytes):
        event_id = uuid.uuid4().hex
        for ident in identities:
            self.capture_html(ident, cf_html_bytes, origin_event_id=event_id)

    def thumbnail_ppm(self, identity, item_id, max_px=96):
        """Return P6 PPM bytes for an image item's thumbnail, or None."""
        it = self.store(identity).get_item(item_id)
        if not it or it.get("kind") not in (cbm.KIND_IMAGE, cbm.KIND_GIF):
            return None
        data = self.store(identity).get_data(item_id)
        if data is None:
            return None
        try:
            ppm = ci.bmp_to_ppm(data, max_px=max_px)
            if ppm is not None:
                return ppm
            if cbm.is_gif_item(it):
                frames = cpv.gif_frames_to_ppm_frames(data, max_px=max_px, max_frames=1)
                return frames[0]["ppm"] if frames else None
            return None
        except Exception:
            return None

    def preview_frames(self, identity, item_id, max_px=96, max_frames=60, max_preview_bytes=50 * 1024 * 1024):
        """Return animated GIF preview frames for a clipboard item, or None."""
        st = self.store(identity)
        it = st.get_item(item_id)
        if not it or not cbm.is_gif_item(it):
            return None

        data = None
        source = None

        if it.get("kind") in (cbm.KIND_FILE, cbm.KIND_FILE_BATCH):
            local = cf.local_source_paths(it)
            if len(local) == 1 and local[0].lower().endswith(".gif"):
                source = local[0]
            elif it.get("available"):
                dest_root = os.path.join(self.store_root, "temp", "preview")
                paths = self.materialize_files(identity, item_id, dest_root)
                if len(paths or []) == 1 and paths[0].lower().endswith(".gif"):
                    source = paths[0]
        else:
            data = st.get_data(item_id)

        if source:
            try:
                if os.path.getsize(source) > max_preview_bytes:
                    self.log("DEBUG", f"GIF preview failed item={item_id} reason=preview too large")
                    return None
                with open(source, "rb") as f:
                    data = f.read(max_preview_bytes + 1)
                if len(data) > max_preview_bytes:
                    self.log("DEBUG", f"GIF preview failed item={item_id} reason=preview too large")
                    return None
            except Exception as e:
                self.log("DEBUG", f"GIF preview failed item={item_id} reason={e}")
                return None

        if not data or len(data) > max_preview_bytes:
            self.log("DEBUG", f"GIF preview failed item={item_id} reason=no preview data")
            return None

        pkg = cpv.gif_preview_package(data, max_px=max_px, max_frames=max_frames)
        frames = pkg.get("frames") or []
        if frames:
            self.log("DEBUG", f"GIF preview generated item={item_id} frames={len(frames)}")
            return pkg
        self.log("DEBUG", f"GIF preview failed item={item_id} reason=no animated preview")
        return None


    # ── sync entry points ───────────────────────────────────────────
    def on_profile_activated(self, identity):
        if not self.enabled():
            return
        if not self._settings().get("sync_on_activate"):
            return
        self.send_manifest(identity)

    def send_manifest(self, identity):
        if not self._begin_local_operation():
            return False
        try:
            st = self.store(identity)
            self.send_fn(identity, st.build_manifest(self.device_id))
            self.log("DEBUG", f"clipboard manifest sent -> {identity} "
                              f"({len(st.list_items())} items)")
            return True
        finally:
            self._end_local_operation()

    # ── incoming message routing ────────────────────────────────────
    def handle(self, identity, msg):
        if not self._begin_incoming_operation(msg):
            return False
        try:
            t = msg.get("type")
            if t == cbp.T_MANIFEST:
                self._on_manifest(identity, msg)
            elif t == cbp.T_ANNOUNCEMENT:
                self._on_announcement(identity, msg)
            elif t == cbp.T_ANNOUNCEMENT_ACK:
                ack = cbp.parse_announcement_ack(msg)
                if ack:
                    with self._lock:
                        pending = self._pending_announcements.get(identity, deque())
                        if ack["announcement_id"] in pending:
                            pending.remove(ack["announcement_id"])
                            self.stats["announcement_acks"] += 1
            elif t == cbp.T_PREFLIGHT:
                parsed = cbp.parse_preflight(msg)
                if parsed:
                    item = {"item_id": parsed["item_id"], "sha256": parsed["payload_sha256"],
                            "size": parsed["payload_size"],
                            "total_file_size": parsed.get("logical_size") or parsed["payload_size"],
                            "file_count": parsed["file_count"],
                            "kind": "file_batch" if parsed["file_count"] > 1 else "file",
                            "payload": {"encoding": parsed["encoding"],
                                        "size": parsed.get("known_transfer_size")}}
                    pre = self._receive_preflight(identity, item, allow_manual=True)
                    self.send_fn(identity, cbp.build_preflight_response(
                        parsed["profile_id"], parsed["item_id"], pre["allowed"],
                        reason=pre.get("reason"), detail=pre,
                        request_id=parsed.get("request_id")))
            elif t == cbp.T_PREFLIGHT_RESPONSE:
                resp = cbp.parse_preflight_response(msg)
                if resp:
                    rid = resp.get("request_id")
                    if rid:
                        with self._lock:
                            self._pending_preflight_result[rid] = resp
                            ev = self._pending_preflight.pop(rid, None)
                        if ev:
                            ev.set()
                    elif not resp["allowed"]:
                        self.log("INFO", f"preflight rejected for {resp.get('item_id')}: "
                                         f"{resp.get('reason')}")
            elif t == cbp.T_REQUEST:
                self._on_request(identity, msg)
            elif t == cbp.T_START:
                self._on_start(identity, msg)
            elif t == cbp.T_CHUNK:
                self._on_chunk(identity, msg)
            elif t == cbp.T_COMPLETE:
                self._on_complete(identity, msg)
            elif t == cbp.T_SYNC_RESULT:
                self.log("INFO", f"clipboard sync result from {identity}: "
                                 f"recv={msg.get('received')} skip={msg.get('skipped_existing')} "
                                 f"manual={msg.get('manual_required')} fail={msg.get('failed')}")
            elif t == cbp.T_ERROR:
                self.stats["failed"] += 1
                self.log("WARN", f"clipboard transfer error from {identity}: "
                                 f"{msg.get('code')} {msg.get('message')}")
            elif t == cbp.T_RESUME:
                self._on_resume(identity, msg)
            return True
        finally:
            self._end_local_operation()

    def _on_announcement(self, identity, msg):
        with self._announcement_apply_lock:
            return self._on_announcement_locked(identity, msg)

    def _on_announcement_locked(self, identity, msg):
        parsed = cbp.parse_announcement(msg)
        if not parsed:
            announcement_id = msg.get("announcement_id") if isinstance(msg, dict) else ""
            if cbm.is_valid_item_id(announcement_id):
                self.send_fn(identity, cbp.build_announcement_ack(
                    announcement_id, "rejected", "invalid"))
            return
        if identity.startswith("device:") and identity.split(":", 1)[1] != parsed["device_id"]:
            self.send_fn(identity, cbp.build_announcement_ack(
                parsed["announcement_id"], "rejected", "device_mismatch"))
            return
        announcement_id = parsed["announcement_id"]
        with self._lock:
            seen = self._seen_announcements.setdefault(identity, deque(maxlen=256))
            duplicate = announcement_id in seen
        if duplicate:
            self.send_fn(identity, cbp.build_announcement_ack(announcement_id, "duplicate"))
            return

        item = parsed["item"]
        st = self.store(identity)
        with self._lock:
            previous_revision = max(self._remote_revision.get(identity, -1), st.remote_revision)
            fresh = parsed["history_revision"] > previous_revision
        existing = st.get_item(item["item_id"])
        if existing and not self._same_item_identity(existing, item):
            self.send_fn(identity, cbp.build_announcement_ack(
                announcement_id, "rejected", "item_identity_conflict"))
            return
        if not existing:
            metadata_item = self._item_from_meta(item, available=False)
            st.add_item(metadata_item, data=None, enforce=self._enforce(),
                        make_current=False)
        else:
            merged = self._merge_provider_metadata(existing, item)
            st.add_item(merged, data=None, enforce=self._enforce(),
                        make_current=False,
                        replace_existing=True)
        if fresh:
            st.track_remote_revision(parsed["history_revision"])
        with self._lock:
            self._remote_meta.setdefault(identity, {})
            existing_meta = self._remote_meta[identity].get(item["item_id"])
            self._remote_meta[identity][item["item_id"]] = (
                self._merge_provider_metadata(existing_meta, item) if existing_meta else copy.deepcopy(item))
            if fresh:
                self._remote_revision[identity] = parsed["history_revision"]
                self._remote_current[identity] = parsed["current_item_id"]
            seen.append(announcement_id)
        self._register_remote_providers(identity, item)
        self.stats["announcements_received"] += 1
        self.send_fn(identity, cbp.build_announcement_ack(announcement_id, "accepted"))

    @staticmethod
    def _merge_provider_metadata(existing, incoming):
        if not ClipboardManager._same_item_identity(existing, incoming):
            return copy.deepcopy(existing)
        merged = copy.deepcopy(existing)
        providers = {provider.get("device_id"): copy.deepcopy(provider)
                     for provider in merged.get("providers", [])}
        for provider in incoming.get("providers", []):
            device_id = provider.get("device_id")
            previous = providers.get(device_id)
            if (previous is None or provider.get("last_seen_at", 0)
                    >= previous.get("last_seen_at", 0)):
                providers[device_id] = copy.deepcopy(provider)
        merged["providers"] = list(providers.values())
        return cbm.version_item(merged, payload_state=merged.get("payload_state"))

    @staticmethod
    def _same_item_identity(first, second):
        return bool(first and second
                    and first.get("item_id") == second.get("item_id")
                    and first.get("sha256") == second.get("sha256")
                    and (first.get("payload") or {}).get("content_sha256")
                    == (second.get("payload") or {}).get("content_sha256"))

    def _on_manifest(self, identity, msg):
        with self._announcement_apply_lock:
            return self._on_manifest_locked(identity, msg)

    def _on_manifest_locked(self, identity, msg):
        parsed = cbm.parse_manifest(msg)
        if not parsed:
            return
        if identity.startswith("device:") and identity.split(":", 1)[1] != parsed["device_id"]:
            return
        if parsed.get("schema_version") == cbm.ITEM_SCHEMA_VERSION:
            for raw, item in zip(msg.get("items", []), parsed["items"]):
                if raw != cbm.manifest_item(item):
                    return
                payload = item.get("payload") or {}
                for provider in item.get("providers", []):
                    if (provider.get("state") == "available" and payload.get("sha256") is not None
                            and (provider.get("payload_sha256") is None
                                 or provider.get("payload_size") is None)):
                        return
                    if (provider.get("payload_sha256") is not None
                            and provider.get("payload_sha256") != payload.get("sha256")):
                        return
                    if (provider.get("payload_size") is not None
                            and provider.get("payload_size") != payload.get("size")):
                        return
        st = self.store(identity)
        local_hashes = st.known_hashes()
        diff = cbm.diff_manifest(local_hashes, parsed["items"],
                                 int(self._settings()["max_auto_transfer_mb"]) * 1024 * 1024)
        with self._lock:
            self._remote_meta.setdefault(identity, {})
            for it in parsed["items"]:
                if it["item_id"] not in self._remote_meta[identity]:
                    self._remote_meta[identity][it["item_id"]] = copy.deepcopy(it)
            previous_revision = max(self._remote_revision.get(identity, -1), st.remote_revision)
            current_is_fresh = parsed["history_revision"] > previous_revision

        manual_ids = set(diff["manual_required"])
        for meta in parsed["items"]:
            iid = meta["item_id"]
            existing_item = st.get_item(iid)
            if existing_item:
                merged = self._merge_provider_metadata(existing_item, meta)
                st.add_item(merged, data=None, enforce=self._enforce(),
                            make_current=False, replace_existing=True)
                self._register_remote_providers(identity, meta)
                continue
            existing_content = next((item for item in st.list_items()
                                     if item.get("sha256") == meta.get("sha256")), None)
            available = bool(existing_content and existing_content.get("available"))
            item = self._item_from_meta(
                meta, available=available,
                transfer_status=(ctt.TransferStatus.waiting_manual if iid in manual_ids else None),
            )
            if available and existing_content:
                for key in ("files", "base", "compressible_ratio"):
                    if key in existing_content:
                        item[key] = copy.deepcopy(existing_content[key])
                item = cbm.version_item(item, payload_state=existing_content.get("payload_state"))
            st.add_item(item, data=None, enforce=self._enforce(),
                        make_current=False)
            self._register_remote_providers(identity, meta)

        if current_is_fresh:
            current_item_id = parsed.get("current_item_id")
            if current_item_id is None or st.get_item(current_item_id):
                st.track_remote_revision(parsed["history_revision"])
            with self._lock:
                self._remote_revision[identity] = parsed["history_revision"]
                self._remote_current[identity] = current_item_id
        with self._lock:
            for meta in parsed["items"]:
                existing_meta = self._remote_meta[identity].get(meta["item_id"])
                if existing_meta:
                    self._remote_meta[identity][meta["item_id"]] = self._merge_provider_metadata(
                        existing_meta, meta)

        # Placeholders for manual-required items so the UI can show a retry icon.
        for iid in diff["manual_required"]:
            meta = self._remote_meta[identity].get(iid)
            if meta:
                item = st.get_item(iid) or self._item_from_meta(
                    meta, available=False, transfer_status=ctt.TransferStatus.waiting_manual)
                self._make_job_from_item(identity, item, "receive",
                                         status=ctt.TransferStatus.waiting_manual,
                                         manual_required=True)

        ready_to_request = []
        failed_disk = []
        for iid in diff["to_request"]:
            meta = self._remote_meta[identity].get(iid)
            if not meta:
                continue
            disk = self._can_request_item(identity, meta)
            if not disk.get("ok"):
                failed_disk.append((iid, disk))
                item = self._item_from_meta(
                    meta, available=False,
                    transfer_status=ctt.TransferStatus.failed,
                    transfer_error="Nicht genug Speicherplatz")
                st.add_item(item, data=None, enforce=self._enforce(),
                            make_current=(st.current_item_id == iid), replace_existing=True)
                self._make_job_from_item(identity, item, "receive",
                                         status=ctt.TransferStatus.failed,
                                         error="Nicht genug Speicherplatz")
                continue
            ready_to_request.append(iid)

        if ready_to_request:
            self.send_fn(identity, cbp.build_request_items(
                parsed["profile_id"], ready_to_request, True, "auto_sync"))
        # Report what we will do.
        self.send_fn(identity, cbm.build_sync_result(
            received=0, skipped_existing=diff["skipped_existing"],
            manual_required=len(diff["manual_required"]), failed=len(failed_disk)))
        self.log("INFO", f"clipboard manifest from {identity}: "
                         f"request={len(ready_to_request)} skip={diff['skipped_existing']} "
                         f"manual={len(diff['manual_required'])} blocked={len(failed_disk)}")

    def request_items(self, identity, item_ids, reason="manual_retry"):
        if not self._begin_local_operation():
            return False
        try:
            if item_ids:
                self.send_fn(identity, cbp.build_request_items(
                    profile_id=profile_dir_name(identity), item_ids=list(item_ids),
                    include_data=True, reason=reason))
            return True
        finally:
            self._end_local_operation()

    def _on_request(self, identity, msg):
        req = cbp.parse_request_items(msg)
        if not req:
            return
        st = self.store(identity)
        for iid in req["item_ids"]:
            it = st.get_item(iid)
            if it:
                self._queue_send_item(identity, iid)
            else:
                self.send_fn(identity, cbp.build_transfer_error(
                    "-", iid, cbp.ERR_NOT_FOUND, "item/data not present"))

    def _blob_for(self, st, item):
        """Return the transfer blob for an item: stored bytes, or a lazily-built
        zip bundle for a locally-captured file item."""
        if item.get("kind") in (cbm.KIND_FILE, cbm.KIND_FILE_BATCH) and item.get("files") \
                and not st.has_object(item.get("sha256", "")):
            try:
                return cf.bundle_for_item(item)
            except Exception as e:
                self.log("WARN", f"clipboard bundle build failed: {e}")
                return None
        return st.get_data(item["item_id"])

    def _send_transfer(self, identity, item_id, job, resume_from=0, send_start=True):
        st = self.store(identity)
        item = st.get_item(item_id)
        if not item:
            ctt.mark_failed(job, "item not present")
            return
        source = None
        try:
            source = self._source_for_item(identity, item)
            if source is None:
                ctt.mark_failed(job, "item/data not present")
                self.send_fn(identity, cbp.build_transfer_error(
                    job.transfer_id, item_id, cbp.ERR_NOT_FOUND, "item/data not present"))
                return

            cs = cbp.safe_chunk_size()
            plan = cbm.chunk_plan(source.total_bytes, cs)
            job.total_bytes = source.total_bytes
            job.chunk_count = len(plan)
            blob_sha = source.sha256

            # Preflight handshake for new transfers (skip on resume).
            if send_start:
                request_id = str(_uuid.uuid4())
                payload = item.get("payload") or {}
                preflight_msg = cbp.build_preflight(
                    identity, item_id, blob_sha, source.total_bytes,
                    encoding=payload.get("encoding", "raw"),
                    logical_size=item.get("total_file_size") or source.total_bytes,
                    file_count=item.get("file_count", 0),
                    known_transfer_size=payload.get("size"),
                    materialized_size=0,
                    request_id=request_id)
                ev = threading.Event()
                with self._lock:
                    self._pending_preflight[request_id] = ev
                try:
                    self.send_fn(identity, preflight_msg)
                except Exception as exc:
                    with self._lock:
                        self._pending_preflight.pop(request_id, None)
                    ctt.mark_failed(job, f"preflight send failed: {exc}")
                    self.send_fn(identity, cbp.build_transfer_error(
                        job.transfer_id, item_id, cbp.ERR_ABORTED, str(exc)))
                    return
                preflight_timeout = max(5.0, float(self._settings().get("preflight_timeout_sec", 30)))
                got = ev.wait(timeout=preflight_timeout)
                with self._lock:
                    self._pending_preflight.pop(request_id, None)
                    resp = self._pending_preflight_result.pop(request_id, None)
                if not got or resp is None:
                    reason = "preflight_timeout" if not got else "preflight_error"
                    err_code = cbp.ERR_TIMEOUT if not got else cbp.ERR_ABORTED
                    ctt.mark_failed(job, reason)
                    self.send_fn(identity, cbp.build_transfer_error(
                        job.transfer_id, item_id, err_code, reason))
                    self.log("WARN", f"transfer preflight handshake: {reason} item={item_id} "
                                     f"identity={identity}")
                    return
                if not resp.get("allowed"):
                    reason = resp.get("reason", "preflight_rejected")
                    ctt.mark_failed(job, reason)
                    self.send_fn(identity, cbp.build_transfer_error(
                        job.transfer_id, item_id, cbp.ERR_ABORTED, reason))
                    self.log("WARN", f"transfer preflight rejected: {reason} item={item_id} "
                                     f"identity={identity}")
                    return
                with self._lock:
                    self._preflight_approved.add((identity, item_id))

            if send_start:
                self.send_fn(identity, cbp.build_transfer_start(
                    job.transfer_id, item_id, blob_sha, source.total_bytes, cs,
                    kind=item.get("kind", cbm.KIND_BINARY), mime=item.get("mime", ""),
                    file_count=item.get("file_count", 0),
                    display_name=item.get("display_name", "")))

            start_index = max(0, int(resume_from))
            for c in source.iter_chunks(cs, start_index=start_index):
                if job.status == ctt.TransferStatus.cancelled:
                    return
                piece = c["data"]
                chunk_sha = c.get("sha256")
                self.send_fn(identity, cbp.build_transfer_chunk(
                    job.transfer_id, item_id, c["index"], c["offset"], piece, chunk_sha))
                completed = [p["index"] for p in plan[:c["index"] + 1]]
                ctt.update_progress(job, sent_bytes=c["offset"] + len(piece),
                                    completed_chunks=completed,
                                    missing_chunks=ctt.missing_chunk_indices(
                                        len(plan), completed_chunks=completed))
                kib = self._transfer_settings()["max_kib_per_sec"]
                if kib > 0:
                    time.sleep(max(0.0, len(piece) / (kib * 1024.0)))

            self.send_fn(identity, cbp.build_transfer_complete(job.transfer_id, item_id, blob_sha))
            ctt.mark_completed(job)
            self.stats["sent_items"] += 1
            self.log("DEBUG", f"clipboard transfer sent {item_id} -> {identity} "
                              f"({source.total_bytes} bytes)")
        except Exception as e:
            ctt.mark_failed(job, str(e))
            self.send_fn(identity, cbp.build_transfer_error(
                job.transfer_id, item_id, self._error_code_for_exception(e), str(e)))
            self.log("WARN", f"clipboard transfer send failed {item_id} -> {identity}: {e}")
        finally:
            if source is not None:
                try:
                    source.cleanup()
                except Exception:
                    pass

    def _on_start(self, identity, msg):
        if (not cbm.is_valid_item_id(msg.get("item_id"))
                or not cbm.is_valid_item_id(msg.get("transfer_id"))
                or not cbm.is_valid_sha256(msg.get("sha256"))):
            self.log("WARN", f"rejected malformed clipboard transfer start from {identity}")
            return
        with self._lock:
            if not self._accepting_work or self._update_maintenance or self._shutting_down:
                self.send_fn(identity, cbp.build_transfer_error(
                    msg["transfer_id"], msg.get("item_id"), cbp.ERR_ABORTED, "shutting_down"))
                return
        s = self._transfer_settings()
        total_size = int(msg.get("total_size", 0) or 0)
        chunk_count = int(msg.get("chunk_count", 0) or 0)
        meta = None
        with self._lock:
            meta = (self._remote_meta.get(identity) or {}).get(msg.get("item_id"))
        item = self._item_from_meta(meta, available=False) if meta else {
            "item_id": msg.get("item_id"),
            "sha256": msg.get("sha256", ""),
            "kind": msg.get("kind", cbm.KIND_BINARY),
            "mime": msg.get("mime", "application/octet-stream"),
            "size": int(msg.get("total_size", 0) or 0),
            "created_at": None,
            "seq": 0,
            "display_name": msg.get("display_name", ""),
            "preview_text": "",
            "preview_hash": "",
            "file_count": int(msg.get("file_count", 0) or 0),
            "total_file_size": int(msg.get("total_size", 0) or 0),
            "pinned": False,
            "available": False,
        }
        item_id = msg.get("item_id")
        # For preflight-handshake-approved transfers, skip redundant preflight.
        approved = (identity, item_id) in self._preflight_approved
        if not approved:
            preflight = self._receive_preflight(identity, item, allow_manual=True)
            if not preflight["allowed"]:
                reason = preflight.get("reason", "preflight_error")
                err_code = {"disk_full": cbp.ERR_DISK_FULL,
                            "too_large": cbp.ERR_TOO_LARGE}.get(reason, cbp.ERR_ABORTED)
                self.send_fn(identity, cbp.build_transfer_error(
                    msg["transfer_id"], msg.get("item_id"), err_code, reason))
                self.log("WARN", f"transfer preflight rejected: {reason} item={msg.get('item_id')}")
                return
        else:
            with self._lock:
                self._preflight_approved.discard((identity, item_id))
        use_disk = total_size > s["disk_assembler_threshold_bytes"]
        if use_disk:
            space = ctt.check_disk_space(self.store(identity).temp_dir, total_size)
            if not space.get("ok"):
                if meta and not self.store(identity).get_item(item["item_id"]):
                    failed_item = self._item_from_meta(meta, available=False,
                                                       transfer_status=ctt.TransferStatus.failed,
                                                       transfer_error="Nicht genug Speicherplatz")
                    self.store(identity).add_item(failed_item, data=None, enforce=self._enforce())
                    self._make_job_from_item(identity, failed_item, "receive",
                                             status=ctt.TransferStatus.failed,
                                             error="Nicht genug Speicherplatz")
                self.send_fn(identity, cbp.build_transfer_error(
                    msg["transfer_id"], msg.get("item_id"), cbp.ERR_DISK_FULL,
                    "Nicht genug Speicherplatz"))
                self.log("WARN", f"transfer blocked: insufficient disk space item={msg.get('item_id')} "
                                   f"required={space['required_bytes']} free={space['free_bytes']}")
                return
            fd, temp_path = tempfile.mkstemp(prefix=f"{msg['transfer_id']}_", suffix=".part",
                                            dir=self.store(identity).temp_dir)
            os.close(fd)
            try:
                asm = ctt.DiskChunkAssembler(total_size, chunk_count, msg.get("sha256"), temp_path)
            except Exception as e:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                self.send_fn(identity, cbp.build_transfer_error(
                    msg["transfer_id"], msg.get("item_id"), self._error_code_for_exception(e), str(e)))
                self.log("WARN", f"clipboard receiver asm init failed for {msg.get('item_id')}: {e}")
                return
        else:
            asm = cbp.ChunkAssembler(total_size, chunk_count, msg.get("sha256"))

        job = self._make_job_from_item(identity, item, "receive",
                                       status=ctt.TransferStatus.running,
                                       transfer_id=msg["transfer_id"],
                                       chunk_count=chunk_count)
        with self._lock:
            self._assemblers[msg["transfer_id"]] = {
                "identity": identity,
                "meta": copy.deepcopy(msg),
                "item_meta": copy.deepcopy(meta),
                "asm": asm,
                "job": job,
            }

    def _on_chunk(self, identity, msg):
        with self._lock:
            entry = self._assemblers.get(msg["transfer_id"])
        if not entry:
            return
        asm = entry["asm"]
        data = cbp.decode_chunk_data(msg)
        if isinstance(asm, ctt.DiskChunkAssembler):
            status = asm.add_chunk(msg["chunk_index"], msg.get("offset", 0), data,
                                   msg.get("sha256"))
        else:
            status = asm.add_chunk(msg["chunk_index"], data, msg.get("sha256"))
        job = entry.get("job")
        if job is not None and status == "ok":
            completed = self._completed_indices(asm)
            ctt.update_progress(job, received_bytes=entry["asm"].bytes_received,
                                completed_chunks=completed,
                                missing_chunks=ctt.missing_chunk_indices(
                                    asm.chunk_count, completed_chunks=completed))
        if status == "hash_mismatch":
            # ask for a resume from the first missing index
            if job is not None:
                ctt.mark_retry(job, error="hash mismatch")
                if ctt.should_retry(job):
                    self.send_fn(identity, cbp.build_transfer_resume(
                        msg["transfer_id"], msg["item_id"], asm.next_index))
                else:
                    ctt.mark_failed(job, error="hash mismatch")
                    self.send_fn(identity, cbp.build_transfer_error(
                        msg["transfer_id"], msg["item_id"], cbp.ERR_HASH_MISMATCH,
                        "chunk hash mismatch"))

    def _on_resume(self, identity, msg):
        item_id = msg.get("item_id")
        next_index = int(msg.get("next_index", 0) or 0)
        job = self._job_for_item(item_id)
        if not job:
            self.log("INFO", f"clipboard resume requested by {identity} from index {next_index}")
            return
        ctt.mark_retry(job, error=f"resume requested from {next_index}")
        if ctt.should_retry(job):
            self.log("INFO", f"clipboard resume requested by {identity} from index {next_index}")
            self._queue_send_item(identity, item_id, resume_from=next_index, send_start=False)
        else:
            ctt.mark_failed(job, error=f"resume limit reached from {next_index}")
            self.log("WARN", f"clipboard resume limit reached for {item_id}")

    def _on_complete(self, identity, msg):
        with self._lock:
            entry = self._assemblers.pop(msg["transfer_id"], None)
        if not entry:
            return
        job = entry.get("job")
        st = self.store(identity)
        meta = entry.get("item_meta")
        asm = entry["asm"]
        new_object_path = None
        expected_payload_sha = entry["meta"].get("sha256")
        if msg.get("sha256") != expected_payload_sha:
            self.stats["failed"] += 1
            if job is not None:
                ctt.mark_failed(job, "completion hash mismatch")
            try:
                asm.cleanup()
            except Exception:
                pass
            return
        temp_path = None
        try:
            if isinstance(asm, ctt.DiskChunkAssembler):
                result = asm.finalize()
                temp_path = result["path"]
                item = self._item_from_meta(meta, available=True) if meta else cbm.make_binary_item(
                    msg.get("sha256", result["sha256"]), result["size"], seq=0)
                try:
                    item = self._bind_received_payload(identity, item, result["sha256"], result["size"])
                    item = self._with_local_provider(item)
                except ValueError as exc:
                    self.stats["failed"] += 1
                    if job is not None:
                        ctt.mark_failed(job, str(exc))
                    return
                space = ctt.check_disk_space(st.dir, result["size"])
                if not space["ok"]:
                    self.stats["failed"] += 1
                    if job is not None:
                        ctt.mark_failed(job, "Nicht genug Speicherplatz")
                    self.send_fn(identity, cbp.build_transfer_error(
                        msg["transfer_id"], msg.get("item_id"), cbp.ERR_DISK_FULL,
                        "Nicht genug Speicherplatz"))
                    self.log("WARN", f"transfer blocked: insufficient disk space item={msg.get('item_id')} "
                                       f"required={space['required_bytes']} free={space['free_bytes']}")
                    return
                object_existed = st.has_object(item["sha256"])
                st.write_object_from_file(item["sha256"], temp_path, move=True)
                if not object_existed:
                    new_object_path = st.object_path(item["sha256"])
                st.add_item(item, data=None, enforce=self._enforce(),
                            make_current=(item["item_id"] == self._remote_current.get(identity)),
                            replace_existing=bool(st.get_item(item["item_id"])))
                self.log("INFO", f"clipboard item received from {identity}: {item['item_id']} "
                                 f"({result['size']} bytes, {item.get('kind')})")
                self._evict_cache_if_needed(identity)
            else:
                try:
                    data = asm.assemble()
                except ValueError as e:
                    self.stats["failed"] += 1
                    if job is not None:
                        ctt.mark_failed(job, str(e))
                    self.send_fn(identity, cbp.build_transfer_error(
                        msg["transfer_id"], msg.get("item_id"), cbp.ERR_HASH_MISMATCH, str(e)))
                    self.log("WARN", f"clipboard transfer verify failed from {identity}: {e}")
                    return
                item = self._item_from_meta(meta, available=True) if meta else cbm.make_binary_item(
                    msg.get("sha256", cbm.sha256_bytes(data)), len(data), seq=0)
                try:
                    item = self._bind_received_payload(identity, item, cbm.sha256_bytes(data), len(data))
                    item = self._with_local_provider(item)
                except ValueError as exc:
                    self.stats["failed"] += 1
                    if job is not None:
                        ctt.mark_failed(job, str(exc))
                    return
                space = ctt.check_disk_space(st.dir, len(data))
                if not space["ok"]:
                    self.stats["failed"] += 1
                    if job is not None:
                        ctt.mark_failed(job, "Nicht genug Speicherplatz")
                    self.send_fn(identity, cbp.build_transfer_error(
                        msg["transfer_id"], msg.get("item_id"), cbp.ERR_DISK_FULL,
                        "Nicht genug Speicherplatz"))
                    self.log("WARN", f"transfer blocked: insufficient disk space item={msg.get('item_id')} "
                                       f"required={space['required_bytes']} free={space['free_bytes']}")
                    return
                st.add_item(item, data=data, enforce=self._enforce(),
                            make_current=(item["item_id"] == self._remote_current.get(identity)),
                            replace_existing=bool(st.get_item(item["item_id"])))
                self.log("INFO", f"clipboard item received from {identity}: {item['item_id']} "
                                 f"({len(data)} bytes, {item.get('kind')})")
                self._evict_cache_if_needed(identity)
            self.stats["received_items"] += 1
            if job is not None:
                ctt.mark_completed(job)
        except Exception as exc:
            if new_object_path is not None:
                try:
                    os.remove(new_object_path)
                except OSError:
                    pass
            self.stats["failed"] += 1
            if job is not None:
                ctt.mark_failed(job, str(exc))
            self.send_fn(identity, cbp.build_transfer_error(
                msg.get("transfer_id", "-"), msg.get("item_id", ""),
                self._error_code_for_exception(exc), str(exc)))
            self.log("WARN", f"clipboard receive finalization failed from {identity}: {exc}")
        finally:
            if isinstance(asm, ctt.DiskChunkAssembler):
                try:
                    asm.cleanup()
                except Exception:
                    pass

    # ── helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _item_from_meta(meta, available, transfer_status=None, transfer_error=None):
        it = copy.deepcopy(meta)
        it["seq"] = 0
        it["pinned"] = False
        it["available"] = bool(available)
        if isinstance(meta.get("metadata"), dict):
            it["metadata"] = dict(meta["metadata"])
        if transfer_status or transfer_error:
            md = dict(it.get("metadata", {}) or {})
            if transfer_status:
                md["transfer_status"] = transfer_status
            if transfer_error:
                md["transfer_error"] = transfer_error
            it["metadata"] = md
        return cbm.version_item(it, payload_state="cached" if available else "metadata_only")

    def _bind_received_payload(self, identity, item, payload_sha256, payload_size):
        it = copy.deepcopy(item)
        payload = dict(it.get("payload") or {})
        if payload.get("encoding", "raw") == "raw" and it.get("sha256") != payload_sha256:
            raise ValueError("payload content hash mismatch")
        payload["sha256"] = payload_sha256
        payload["size"] = int(payload_size)
        it["payload"] = payload
        it = cbm.version_item(it, payload_state="cached")
        st = self.store(identity)
        content_sha = it.get("sha256", "")
        if cbm.is_valid_sha256(content_sha) and self._cache_enabled():
            providers = [{"device_id": identity.split(":", 1)[1] if identity.startswith("device:") else identity,
                          "state": "available", "last_seen_at": time.time()}]
            st.record_cache_entry(content_sha, payload_sha256, payload_size, providers=providers)
        return it

    def _cache_enabled(self):
        return bool(self._settings().get("cache_received_payloads", True))

    def _evict_cache_if_needed(self, identity, force=False):
        if not self._cache_enabled() and not force:
            return {}
        st = self.store(identity)
        st.remove_ghost_cache_entries()
        with self._lock:
            protected = st.cache_protected_hashes()
            for job in self._jobs.values():
                if job.status in (ctt.TransferStatus.running, ctt.TransferStatus.pending,
                                  ctt.TransferStatus.retrying):
                    item = st.get_item(job.item_id)
                    if item and item.get("sha256"):
                        protected.add(item["sha256"])
            protected |= st.active_lease_hashes()
        max_cache_mb = int(self._settings().get("cache_max_mb", 256))
        target_bytes = max_cache_mb * 1024 * 1024
        snap = st.cache_snapshot()
        current_bytes = snap.get("unique_bytes", 0)
        target_unique = max(0, current_bytes - target_bytes) if current_bytes > target_bytes else None
        return st.evict_cache(protected_hashes=protected, target_unique_bytes=target_unique)

    # ── GUI/control helpers ─────────────────────────────────────────
    def list_items(self, identity):
        return self.store(identity).list_items()

    def get_text(self, identity, item_id):
        data = self.store(identity).get_data(item_id)
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def get_html(self, identity, item_id):
        it = self.store(identity).get_item(item_id)
        if not it or it.get("kind") != cbm.KIND_HTML:
            return None
        return self.store(identity).get_data(item_id)

    def item_kind(self, identity, item_id):
        it = self.store(identity).get_item(item_id)
        return it.get("kind") if it else None

    def materialize_files(self, identity, item_id, dest_root):
        result = self.materialize_files_result(identity, item_id, dest_root)
        return result.get("paths") if result.get("ok") else None

    def materialize_files_result(self, identity, item_id, dest_root):
        if not self._begin_local_operation():
            return {"ok": False, "error": "clipboard manager is shut down"}
        try:
            return self._materialize_files_result(identity, item_id, dest_root)
        finally:
            self._end_local_operation()

    def _materialize_files_result(self, identity, item_id, dest_root):
        """Return absolute file paths for a file/batch item so they can be put on
        the Windows clipboard (CF_HDROP).

        Locally-captured items return their original source paths (no copy).
        Received items are unpacked from their zip bundle into ``dest_root/item``.
        Returns {ok, paths, error}. The compat wrapper ``materialize_files`` still
        returns the list or None.
        """
        st = self.store(identity)
        it = st.get_item(item_id)
        if not it or it.get("kind") not in (cbm.KIND_FILE, cbm.KIND_FILE_BATCH):
            return {"ok": False, "error": "not file/batch"}
        local = cf.local_source_paths(it)
        if local:
            return {"ok": True, "paths": local}
        object_path = st.get_object_path_for_item(item_id)
        if not object_path:
            return {"ok": False, "error": "file data not present (download/retry)"}
        required = int(it.get("total_file_size", 0) or it.get("size", 0) or 0)
        space = ctt.check_disk_space(dest_root, required)
        if not space["ok"]:
            self.log("WARN", f"transfer blocked: insufficient disk space item={item_id} "
                              f"required={space['required_bytes']} free={space['free_bytes']}")
            return {"ok": False, "error": "Nicht genug Speicherplatz", "space": space}
        dest = os.path.join(dest_root, profile_dir_name(identity), item_id)
        try:
            paths = cf.unpack_bundle_file(object_path, dest)
            for path in paths:
                try:
                    csrc.mark_active(path)
                except Exception:
                    pass
            try:
                st.set_lease(item_id, dest)
            except Exception:
                pass
            return {"ok": True, "paths": paths, "lease": True}
        except Exception as e:
            self.log("WARN", f"clipboard unpack failed: {e}")
            return {"ok": False, "error": f"unpack failed: {e}"}

    def delete_item(self, identity, item_id):
        st = self.store(identity)
        st.release_leases_for_item(item_id)
        return st.delete_item(item_id)

    def clear(self, identity):
        return self.store(identity).clear()

    def reset_current(self, identity):
        """Reset current_item_id to None (e.g. clipboard was cleared externally)."""
        return self.store(identity).reset_current()

    def enable_provider(self):
        """Re-enable this device as an available content provider."""
        self._provider_enabled = True

    def disable_provider(self):
        """Stop offering this device as a content provider.

        Future items will mark this device's provider state as
        "unavailable".  Existing items are not retroactively changed.
        """
        self._provider_enabled = False

    def provider_snapshot(self):
        """Return provider state including registry and enabled flag."""
        with self._lock:
            registry = {d: dict(e) for d, e in self._providers.items()}
        return {"provider_enabled": self._provider_enabled, "registry": registry}

    def set_pinned(self, identity, item_id, pinned):
        return self.store(identity).set_pinned(item_id, pinned)

    def mark_current(self, identity, item_id):
        """Persist an item as current after a successful Windows clipboard write."""
        return self.store(identity).set_current(item_id)

    def perform_windows_write(self, identity, item_id, formats, primary_format, digest,
                              write_fn, sequence_fn):
        """Run one Windows write and commit suppression/current state on success."""
        if not self._begin_local_operation():
            return False
        try:
            with self._windows_write_lock:
                before = int(sequence_fn() or 0)
                token = self._write_suppressor.prepare(
                    item_id, formats, primary_format, digest, before)
                success = False
                write_sequence = None
                try:
                    result = write_fn()
                    if isinstance(result, tuple) and len(result) == 2:
                        success = bool(result[0])
                        write_sequence = int(result[1] or 0)
                    else:
                        success = bool(result)
                    return success
                finally:
                    after = (write_sequence if write_sequence is not None
                             else int(sequence_fn() or 0))
                    self._write_suppressor.finish(token, success, after)
                    if success:
                        try:
                            self.mark_current(identity, item_id)
                        except Exception as exc:
                            self.log("WARN", f"clipboard current-item persistence failed: {exc}")
                        try:
                            st = self.store(identity)
                            st.bind_lease_sequence(item_id, after)
                        except Exception as exc:
                            self.log("WARN", f"clipboard lease binding failed: {exc}")
        finally:
            self._end_local_operation()

    def consume_write_suppression(self, sequence, observed_formats, primary_format, digest):
        return self._write_suppressor.consume(
            sequence, observed_formats, primary_format, digest)

    def write_suppression_snapshot(self):
        return self._write_suppressor.snapshot()

    @staticmethod
    def text_digest(text):
        return cbe.text_digest(text)

    @staticmethod
    def bytes_digest(data):
        return cbe.bytes_digest(data)

    @staticmethod
    def file_list_digest(paths):
        return cbe.file_list_digest(paths)

    def progress_snapshot(self):
        """item_id -> unified transfer progress records for the UI progressbars."""
        out = {}
        with self._lock:
            jobs = dict(self._jobs)
            stores = dict(self._stores)
        auto_limit = int(self._settings().get("max_auto_transfer_mb", 100)) * 1024 * 1024
        for st in stores.values():
            for item in st.list_items():
                iid = item.get("item_id")
                if not iid:
                    continue
                job = jobs.get(iid)
                out[iid] = ctt.progress_from_item(item, job, auto_limit_bytes=auto_limit)
        return out

    def activity_snapshot(self):
        """Return all clipboard work relevant to safe runtime shutdown/update."""
        blocking_statuses = (
            ctt.TransferStatus.pending,
            ctt.TransferStatus.running,
            ctt.TransferStatus.retrying,
            ctt.TransferStatus.paused,
        )
        with self._lock:
            jobs = list(self._jobs.values())
            assembler_ids = sorted(self._assemblers)
            accepting = self._accepting_work
            update_maintenance = self._update_maintenance
            shutting_down = self._shutting_down
            shutdown_complete = self._shutdown_complete
            local_operations = self._active_local_operations
        status_counts = {}
        for job in jobs:
            status = getattr(job, "status", None)
            if status in blocking_statuses:
                status_counts[status] = status_counts.get(status, 0) + 1
        queue_activity = self._transfer_queue.activity_snapshot()
        blocking_jobs = sum(status_counts.values())
        blocking = bool(blocking_jobs or assembler_ids or local_operations
                        or queue_activity.get("blocking"))
        return {
            "accepting": bool(accepting and not update_maintenance),
            "update_maintenance": update_maintenance,
            "shutting_down": shutting_down,
            "shutdown_complete": shutdown_complete,
            "blocking": blocking,
            "blocking_jobs": blocking_jobs,
            "blocking_job_statuses": status_counts,
            "active_assemblers": len(assembler_ids),
            "assembler_transfer_ids": assembler_ids,
            "active_local_operations": local_operations,
            "transfer_queue": queue_activity,
            "write_suppression": self.write_suppression_snapshot(),
        }

    def diagnostics(self, identity):
        """Return clipboard runtime diagnostics for the given profile."""
        st = self.store(identity)
        items = st.list_items()
        cache = st.cache_snapshot()
        leases = st.lease_snapshot()
        activity = self.activity_snapshot()
        return {
            "profile_id": st.profile_id,
            "store": {
                "item_count": len(items),
                "total_bytes": st.total_size(),
                "available_count": sum(1 for it in items if it.get("available")),
                "pinned_count": sum(1 for it in items if it.get("pinned")),
            },
            "cache": cache,
            "leases": leases,
            "activity": activity,
        }

    def shutdown(self, timeout=5.0):
        """Stop admission and tear down transfer resources within ``timeout``."""
        timeout = max(0.0, float(timeout))
        deadline = time.monotonic() + timeout
        with self._lock:
            self._accepting_work = False
            self._update_maintenance = True
            self._shutting_down = True

        queue_complete = self._transfer_queue.shutdown(
            timeout=max(0.0, deadline - time.monotonic()), cancel_pending=True)

        with self._lock:
            while self._active_local_operations:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._activity_changed.wait(timeout=remaining)
            local_complete = self._active_local_operations == 0
            entries = list(self._assemblers.values()) if local_complete else []
            if local_complete:
                self._assemblers.clear()

        cleanup_complete = True
        for entry in entries:
            job = entry.get("job")
            if job is not None and job.status not in (
                    ctt.TransferStatus.completed, ctt.TransferStatus.failed,
                    ctt.TransferStatus.cancelled):
                ctt.mark_cancelled(job, "clipboard manager shut down")
            asm = entry.get("asm")
            if isinstance(asm, ctt.DiskChunkAssembler):
                try:
                    asm.cleanup()
                    if (os.path.exists(asm.temp_path)
                            or os.path.exists(csrc.active_marker_path(asm.temp_path))):
                        cleanup_complete = False
                except Exception as exc:
                    cleanup_complete = False
                    self.log("WARN", f"clipboard assembler cleanup failed: {exc}")

        with self._lock:
            if queue_complete and local_complete:
                for job in self._jobs.values():
                    if job.status in (
                            ctt.TransferStatus.pending, ctt.TransferStatus.running,
                            ctt.TransferStatus.retrying, ctt.TransferStatus.paused):
                        ctt.mark_cancelled(job, "clipboard manager shut down")
            jobs_complete = not any(
                job.status in (
                    ctt.TransferStatus.pending, ctt.TransferStatus.running,
                    ctt.TransferStatus.retrying, ctt.TransferStatus.paused)
                for job in self._jobs.values())
            assemblers_complete = not self._assemblers
            self._shutdown_complete = bool(
                queue_complete and local_complete and assemblers_complete
                and jobs_complete and cleanup_complete)
            return self._shutdown_complete
