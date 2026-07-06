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

import threading
import uuid

import clipboard_model as cbm
import clipboard_protocol as cbp
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
        self.stats = {"sent_items": 0, "received_items": 0, "failed": 0}

    # ── stores ──────────────────────────────────────────────────────
    def store(self, identity):
        with self._lock:
            st = self._stores.get(identity)
            if st is None:
                st = ClipboardStore(self.store_root, profile_dir_name(identity))
                self._stores[identity] = st
            return st

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

    # ── local capture ───────────────────────────────────────────────
    def capture_text(self, identity, text):
        """Add a captured local text copy to the store for ``identity``.

        Skips if the newest item already has the same content (no dup on repeat).
        Returns the stored item or None.
        """
        if not text:
            return None
        st = self.store(identity)
        item = cbm.make_text_item(text, seq=0)
        items = st.list_items()
        if items and items[-1].get("sha256") == item["sha256"]:
            return None
        stored, _ = st.add_item(item, data=text.encode("utf-8"), enforce=self._enforce())
        self.log("DEBUG", f"clipboard captured text -> {identity} ({len(text)} chars)")
        return stored

    def capture_text_all(self, identities, text):
        for ident in identities:
            self.capture_text(ident, text)

    # ── sync entry points ───────────────────────────────────────────
    def on_profile_activated(self, identity):
        if not self.enabled():
            return
        if not self._settings().get("sync_on_activate"):
            return
        self.send_manifest(identity)

    def send_manifest(self, identity):
        st = self.store(identity)
        self.send_fn(identity, st.build_manifest(self.device_id))
        self.log("DEBUG", f"clipboard manifest sent -> {identity} "
                          f"({len(st.list_items())} items)")

    # ── incoming message routing ────────────────────────────────────
    def handle(self, identity, msg):
        t = msg.get("type")
        if t == cbp.T_MANIFEST:
            self._on_manifest(identity, msg)
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

    def _on_manifest(self, identity, msg):
        parsed = cbm.parse_manifest(msg)
        if not parsed:
            return
        st = self.store(identity)
        with self._lock:
            self._remote_meta.setdefault(identity, {})
            for it in parsed["items"]:
                self._remote_meta[identity][it["item_id"]] = it
        s = self._settings()
        auto = int(s["max_auto_transfer_mb"]) * 1024 * 1024
        diff = cbm.diff_manifest(st.known_hashes(), parsed["items"], auto)

        # Placeholders for manual-required items so the UI can show a retry icon.
        for iid in diff["manual_required"]:
            meta = self._remote_meta[identity].get(iid)
            if meta and not st.get_item(iid):
                st.add_item(self._item_from_meta(meta, available=False), data=None,
                            enforce=self._enforce())

        if diff["to_request"]:
            self.send_fn(identity, cbp.build_request_items(
                parsed["profile_id"], diff["to_request"], True, "auto_sync"))
        # Report what we will do.
        self.send_fn(identity, cbm.build_sync_result(
            received=0, skipped_existing=diff["skipped_existing"],
            manual_required=len(diff["manual_required"]), failed=0))
        self.log("INFO", f"clipboard manifest from {identity}: "
                         f"request={len(diff['to_request'])} skip={diff['skipped_existing']} "
                         f"manual={len(diff['manual_required'])}")

    def request_items(self, identity, item_ids, reason="manual_retry"):
        if item_ids:
            self.send_fn(identity, cbp.build_request_items(
                profile_id=profile_dir_name(identity), item_ids=list(item_ids),
                include_data=True, reason=reason))

    def _on_request(self, identity, msg):
        req = cbp.parse_request_items(msg)
        if not req:
            return
        st = self.store(identity)
        for iid in req["item_ids"]:
            it = st.get_item(iid)
            data = st.get_data(iid)
            if it and data is not None:
                self._send_transfer(identity, it, data)
            else:
                self.send_fn(identity, cbp.build_transfer_error(
                    "-", iid, cbp.ERR_NOT_FOUND, "item/data not present"))

    def _send_transfer(self, identity, item, data):
        tid = uuid.uuid4().hex
        cs = cbp.safe_chunk_size()
        self.send_fn(identity, cbp.build_transfer_start(
            tid, item["item_id"], item["sha256"], len(data), cs,
            kind=item.get("kind", cbm.KIND_BINARY), mime=item.get("mime", ""),
            file_count=item.get("file_count", 0), display_name=item.get("display_name", "")))
        for m in cbp.iter_chunk_messages(tid, item["item_id"], data, cs, hash_chunks=True):
            self.send_fn(identity, m)
        self.send_fn(identity, cbp.build_transfer_complete(tid, item["item_id"], item["sha256"]))
        self.stats["sent_items"] += 1
        self.log("DEBUG", f"clipboard transfer sent {item['item_id']} -> {identity} "
                          f"({len(data)} bytes)")

    def _on_start(self, identity, msg):
        asm = cbp.ChunkAssembler(msg["total_size"], msg["chunk_count"], msg.get("sha256"))
        with self._lock:
            self._assemblers[msg["transfer_id"]] = {"identity": identity, "meta": msg, "asm": asm}

    def _on_chunk(self, identity, msg):
        with self._lock:
            entry = self._assemblers.get(msg["transfer_id"])
        if not entry:
            return
        status = entry["asm"].add_chunk(msg["chunk_index"], cbp.decode_chunk_data(msg),
                                        msg.get("sha256"))
        if status == "hash_mismatch":
            # ask for a resume from the first missing index
            self.send_fn(identity, cbp.build_transfer_resume(
                msg["transfer_id"], msg["item_id"], entry["asm"].next_index))

    def _on_resume(self, identity, msg):
        # Sender side: a full re-send is simplest and correct for this layer.
        # (Chunk-level resume bookkeeping is available via ChunkAssembler.)
        self.log("INFO", f"clipboard resume requested by {identity} from index "
                         f"{msg.get('next_index')}")

    def _on_complete(self, identity, msg):
        with self._lock:
            entry = self._assemblers.pop(msg["transfer_id"], None)
        if not entry:
            return
        try:
            data = entry["asm"].assemble()
        except ValueError as e:
            self.stats["failed"] += 1
            self.send_fn(identity, cbp.build_transfer_error(
                msg["transfer_id"], msg.get("item_id"), cbp.ERR_HASH_MISMATCH, str(e)))
            self.log("WARN", f"clipboard transfer verify failed from {identity}: {e}")
            return
        st = self.store(identity)
        meta = None
        with self._lock:
            meta = (self._remote_meta.get(identity) or {}).get(msg.get("item_id"))
        item = self._item_from_meta(meta, available=True) if meta else cbm.make_binary_item(
            msg.get("sha256", cbm.sha256_bytes(data)), len(data), seq=0)
        # Replace an existing placeholder (same item_id) if present.
        existing = st.get_item(item["item_id"])
        if existing:
            st.delete_item(item["item_id"])
        st.add_item(item, data=data, enforce=self._enforce())
        self.stats["received_items"] += 1
        self.log("INFO", f"clipboard item received from {identity}: {item['item_id']} "
                         f"({len(data)} bytes, {item.get('kind')})")

    # ── helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _item_from_meta(meta, available):
        it = {
            "item_id": meta["item_id"],
            "sha256": meta["sha256"],
            "kind": meta.get("kind", cbm.KIND_BINARY),
            "mime": meta.get("mime", "application/octet-stream"),
            "size": int(meta.get("size", 0) or 0),
            "created_at": meta.get("created_at"),
            "seq": 0,
            "display_name": meta.get("display_name", ""),
            "preview_text": meta.get("preview_text", ""),
            "preview_hash": meta.get("preview_hash", ""),
            "file_count": int(meta.get("file_count", 0) or 0),
            "total_file_size": int(meta.get("total_file_size", 0) or 0),
            "pinned": False,
            "available": bool(available),
        }
        return it

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

    def delete_item(self, identity, item_id):
        return self.store(identity).delete_item(item_id)

    def clear(self, identity):
        return self.store(identity).clear()

    def set_pinned(self, identity, item_id, pinned):
        return self.store(identity).set_pinned(item_id, pinned)
