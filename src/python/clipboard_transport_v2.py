"""Productive ``clipboard_stream_v2`` transport activation (Phase 3, slice 12).

This module binds the transport-neutral V2 foundation to the productive peer
link:

- strict control messages on the legacy JSON peer link (offer/preflight,
  accept/reject, resume request/response, cancel/cancel_ack);
- the dedicated typed data channel: one bounded legacy JSON ``channel`` hello
  carrying a single-use nonce, a JSON ``channel_ack`` barrier, then typed
  frames only (:mod:`clipboard_framing_v2`): ``manifest`` / ``manifest_ack``
  with receiver resume offsets, raw binary payload frames, cumulative window
  ACKs, ``complete`` with sender hashes/fingerprints, ``complete_ack``;
- :class:`OutgoingTransferSession`: the sender state machine with finite
  preflight, manifest-ACK, window-ACK, reconnect and final-ACK deadlines,
  outgoing resume journal, cancel in every phase and privacy-safe status;
- :class:`ReceiverSession`: the receiver wrapper around
  :class:`clipboard_streaming_v2.IncomingTransferStage` that owns the typed
  channel thread and drives staging, ACK batching, verification, publication
  and cancel/pause transitions.

All remote data is untrusted: every message and frame is validated for type,
size, bounds, identity and state before it touches a stage or a window.
Status, logs and messages never contain local absolute paths.
"""
from __future__ import annotations

import copy
import re
import secrets
import socket
import threading
import time

import clipboard_flow_control_v2 as cfc2
import clipboard_framing_v2 as cfr2
import clipboard_manifest_v2 as cman2
import clipboard_model as cbm
import clipboard_preflight_v2 as cpf2
import clipboard_protocol as cbp
import clipboard_resume_v2 as cres2
import clipboard_streaming_v2 as cstr2
import clipboard_transfer_control_v2 as cctl2
from clipboard_framing_v2 import MAX_ENTRY_INDEX, MAX_LOGICAL_OFFSET


SCHEMA_VERSION = 1
PROTOCOL_MAJOR = 2
STRATEGY = "stream_v2"

T_PREFIX = "clipboard_stream_v2_"
T_OFFER = T_PREFIX + "offer"
T_ACCEPT = T_PREFIX + "accept"
T_REJECT = T_PREFIX + "reject"
T_RESUME_REQUEST = T_PREFIX + "resume_request"
T_RESUME_RESPONSE = T_PREFIX + "resume_response"
T_CANCEL = T_PREFIX + "cancel"
T_CANCEL_ACK = T_PREFIX + "cancel_ack"
T_CHANNEL_HELLO = T_PREFIX + "channel"
T_CHANNEL_ACK = T_PREFIX + "channel_ack"
T_MANIFEST = T_PREFIX + "manifest"
T_MANIFEST_ACK = T_PREFIX + "manifest_ack"
T_COMPLETE = T_PREFIX + "complete"
T_COMPLETE_ACK = T_PREFIX + "complete_ack"
T_WINDOW_ACK = cbp.T_STREAM_V2_ACK

# Messages routed over the normal peer link (``ClipboardManager.handle``).
CONTROL_LINK_TYPES = frozenset((
    T_OFFER, T_ACCEPT, T_REJECT, T_RESUME_REQUEST, T_RESUME_RESPONSE,
    T_CANCEL, T_CANCEL_ACK,
))

NONCE_HEX_LENGTH = 32
NONCE_LIFETIME_SECONDS = 60.0
MAX_PENDING_NONCES = 64
MAX_IDENTITY_LENGTH = 256
MAX_PREFLIGHT_LIFETIME_SECONDS = 86400
DEFAULT_PREFLIGHT_LIFETIME_SECONDS = 15 * 60
CHANNEL_POLL_SECONDS = 0.25
RECONNECT_RETRY_SECONDS = 1.0
MAX_RESUME_ATTEMPTS = 128

_REASON = re.compile(r"^[a-z0-9_:.-]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_INDEX_KEY = re.compile(r"^(0|[1-9][0-9]{0,5})$")

TERMINAL_STATES = frozenset(("completed", "cancelled", "purged", "failed", "rejected"))


class TransportV2Error(RuntimeError):
    """Path-free transport error with a stable code."""

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = str(code)


# ── validation primitives ─────────────────────────────────────────────
def _uint(value, maximum):
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        return None
    return value


def transfer_id_hex(value):
    """Canonical 32-hex transfer id or None."""
    return cbp._stream_v2_transfer_id(value)


def _identity(value, maximum=MAX_IDENTITY_LENGTH):
    if not isinstance(value, str) or not value or len(value) > maximum:
        return None
    if any(ord(char) < 32 for char in value):
        return None
    return value


def _reason(value, default="unspecified"):
    if value is None:
        return default
    if isinstance(value, str) and _REASON.fullmatch(value):
        return value
    return None


def normalize_reason(value):
    """Best-effort mapping of a free-form reason to the wire alphabet."""
    text = str(value or "unspecified").lower()
    text = re.sub(r"[^a-z0-9_:.-]", "_", text)[:64]
    return text or "unspecified"


def _sha256(value, *, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, str) and _SHA256.fullmatch(value):
        return value
    return False


def _base(msg_type, transfer_id):
    return {"type": msg_type, "schema_version": SCHEMA_VERSION,
            "protocol_major": PROTOCOL_MAJOR, "transfer_id": str(transfer_id)}


def _parse_base(msg, expected_type):
    if (not isinstance(msg, dict) or msg.get("type") != expected_type
            or msg.get("schema_version") != SCHEMA_VERSION
            or isinstance(msg.get("schema_version"), bool)
            or msg.get("protocol_major") != PROTOCOL_MAJOR
            or isinstance(msg.get("protocol_major"), bool)):
        return None
    return transfer_id_hex(msg.get("transfer_id"))


def peer_device_id(identity):
    """Stable peer device id from a runtime identity (``device:<id>``)."""
    identity = str(identity or "")
    if identity.startswith("device:"):
        return identity.split(":", 1)[1]
    return identity


# ── resume file evidence ───────────────────────────────────────────────
def resume_files_from_journal(journal):
    """Path-free per-file durable offsets from an incoming journal."""
    return [{
        "index": int(entry["index"]),
        "durable_offset": int(entry["durable_offset"]),
        "prefix_sha256": entry["prefix_sha256"],
        "completed": bool(entry["completed"]),
    } for entry in journal.entries if entry["type"] == "file"]


def parse_resume_files(value, manifest=None):
    """Validate a receiver file-offset list; optionally against a manifest."""
    if not isinstance(value, list) or len(value) > cman2.MAX_ENTRIES:
        return None
    result = []
    previous = -1
    for item in value:
        if not isinstance(item, dict) or set(item) != {
                "index", "durable_offset", "prefix_sha256", "completed"}:
            return None
        index = _uint(item["index"], MAX_ENTRY_INDEX)
        offset = _uint(item["durable_offset"], MAX_LOGICAL_OFFSET)
        prefix = _sha256(item["prefix_sha256"], nullable=True)
        completed = item["completed"]
        if (index is None or index <= previous or offset is None or prefix is False
                or not isinstance(completed, bool)):
            return None
        if offset > 0 and prefix is None:
            return None
        if offset == 0 and prefix is not None and not completed:
            return None
        result.append({"index": index, "durable_offset": offset,
                       "prefix_sha256": prefix, "completed": completed})
        previous = index
    if manifest is not None:
        files = [entry for entry in manifest["entries"] if entry["type"] == "file"]
        if len(files) != len(result):
            return None
        for wire, item in zip(files, result):
            if item["index"] != wire["index"] or item["durable_offset"] > wire["size"]:
                return None
            if item["completed"] != (item["durable_offset"] == wire["size"]
                                     and (wire["size"] > 0 or item["prefix_sha256"] is not None)):
                return None
    return result


def resume_plan_from_files(outgoing_journal, manifest, files):
    """Build the sender resume plan from receiver-reported durable evidence."""
    validated = parse_resume_files(files, manifest)
    if validated is None:
        raise TransportV2Error("resume_mismatch", "receiver resume evidence is invalid")
    by_index = {entry["index"]: entry for entry in outgoing_journal.entries}
    plan_files = []
    for item in validated:
        sent = by_index.get(item["index"])
        if sent is None or sent["type"] != "file":
            raise TransportV2Error("resume_mismatch", "receiver resume entry is unknown")
        if sent["receiver_durable_offset"] > item["durable_offset"]:
            raise TransportV2Error("resume_mismatch", "receiver durable progress regressed")
        plan_files.append(cres2.ResumeFile(
            item["index"], int(sent["size"]), item["durable_offset"],
            item["prefix_sha256"], item["completed"]))
    try:
        return cres2.ResumePlan(
            outgoing_journal.transfer_id, outgoing_journal.peer_id,
            outgoing_journal.profile_id, outgoing_journal.provider_id,
            outgoing_journal.item_id, outgoing_journal.item_revision,
            outgoing_journal.manifest_digest, outgoing_journal.entry_set_digest,
            tuple(plan_files))
    except cres2.ResumeJournalError as exc:
        raise TransportV2Error("resume_mismatch", "resume plan is inconsistent") from exc


# ── control-link messages ─────────────────────────────────────────────
def build_offer(transfer_id, manifest, *, device_id, profile_id, resume=False,
                msg_type=T_OFFER):
    manifest = cman2.validate_manifest(manifest)
    msg = _base(msg_type, transfer_id)
    msg.update(device_id=str(device_id), profile_id=str(profile_id),
               item_id=manifest["item_id"], resume=bool(resume), manifest=manifest)
    return msg


def parse_offer(msg, msg_type=T_OFFER):
    transfer_id = _parse_base(msg, msg_type)
    if transfer_id is None:
        return None
    try:
        manifest = cman2.validate_manifest(msg.get("manifest"))
    except (cman2.ManifestValidationError, TypeError):
        return None
    device_id = _identity(msg.get("device_id"), 128)
    profile_id = _identity(msg.get("profile_id"))
    resume = msg.get("resume", False)
    if (device_id is None or profile_id is None or not isinstance(resume, bool)
            or msg.get("item_id") != manifest["item_id"]):
        return None
    return {"transfer_id": transfer_id, "device_id": device_id, "profile_id": profile_id,
            "item_id": manifest["item_id"], "resume": resume, "manifest": manifest}


def build_accept(transfer_id, item_id, manifest_digest, channel_nonce, *,
                 preflight_lifetime_s=DEFAULT_PREFLIGHT_LIFETIME_SECONDS, resume=False,
                 files=None, msg_type=T_ACCEPT):
    msg = _base(msg_type, transfer_id)
    msg.update(item_id=str(item_id), manifest_digest=str(manifest_digest),
               channel_nonce=str(channel_nonce),
               preflight_lifetime_s=int(preflight_lifetime_s), resume=bool(resume),
               files=[] if files is None else list(files))
    return msg


def parse_accept(msg, msg_type=T_ACCEPT):
    transfer_id = _parse_base(msg, msg_type)
    if transfer_id is None:
        return None
    nonce = msg.get("channel_nonce")
    digest = _sha256(msg.get("manifest_digest"))
    lifetime = _uint(msg.get("preflight_lifetime_s"), MAX_PREFLIGHT_LIFETIME_SECONDS)
    files = parse_resume_files(msg.get("files"))
    if (not cbm.is_valid_item_id(msg.get("item_id")) or not digest
            or not isinstance(nonce, str) or not _HEX32.fullmatch(nonce)
            or lifetime is None or lifetime < 1 or not isinstance(msg.get("resume"), bool)
            or files is None):
        return None
    return {"transfer_id": transfer_id, "item_id": msg["item_id"], "manifest_digest": digest,
            "channel_nonce": nonce, "preflight_lifetime_s": lifetime,
            "resume": msg["resume"], "files": files}


def build_reject(transfer_id, item_id, reason, msg_type=T_REJECT):
    msg = _base(msg_type, transfer_id)
    msg.update(item_id=str(item_id), reason=normalize_reason(reason))
    return msg


def parse_reject(msg, msg_type=T_REJECT):
    transfer_id = _parse_base(msg, msg_type)
    if transfer_id is None:
        return None
    reason = _reason(msg.get("reason"))
    if reason is None or not cbm.is_valid_item_id(msg.get("item_id")):
        return None
    return {"transfer_id": transfer_id, "item_id": msg["item_id"], "reason": reason}


def build_cancel(transfer_id, reason, msg_type=T_CANCEL):
    msg = _base(msg_type, transfer_id)
    msg["reason"] = normalize_reason(reason)
    return msg


def parse_cancel(msg, msg_type=T_CANCEL):
    transfer_id = _parse_base(msg, msg_type)
    if transfer_id is None:
        return None
    reason = _reason(msg.get("reason"))
    if reason is None:
        return None
    return {"transfer_id": transfer_id, "reason": reason}


def build_cancel_ack(transfer_id):
    return _base(T_CANCEL_ACK, transfer_id)


def parse_cancel_ack(msg):
    transfer_id = _parse_base(msg, T_CANCEL_ACK)
    return None if transfer_id is None else {"transfer_id": transfer_id}


# ── channel handshake (legacy JSON on the dedicated socket) ──────────
def build_channel_hello(transfer_id, channel_nonce, device_id):
    msg = _base(T_CHANNEL_HELLO, transfer_id)
    msg.update(channel_nonce=str(channel_nonce), device_id=str(device_id))
    return msg


def parse_channel_hello(msg):
    transfer_id = _parse_base(msg, T_CHANNEL_HELLO)
    if transfer_id is None:
        return None
    nonce = msg.get("channel_nonce")
    device_id = _identity(msg.get("device_id"), 128)
    if not isinstance(nonce, str) or not _HEX32.fullmatch(nonce) or device_id is None:
        return None
    return {"transfer_id": transfer_id, "channel_nonce": nonce, "device_id": device_id}


def build_channel_ack(transfer_id, ok, reason=None):
    msg = _base(T_CHANNEL_ACK, transfer_id)
    msg.update(ok=bool(ok), reason=normalize_reason(reason) if reason else "ok")
    return msg


def parse_channel_ack(msg):
    transfer_id = _parse_base(msg, T_CHANNEL_ACK)
    if transfer_id is None or not isinstance(msg.get("ok"), bool):
        return None
    reason = _reason(msg.get("reason"), default="ok")
    if reason is None:
        return None
    return {"transfer_id": transfer_id, "ok": msg["ok"], "reason": reason}


# ── typed channel control frames ───────────────────────────────────────
def build_manifest_frame(transfer_id, manifest):
    msg = _base(T_MANIFEST, transfer_id)
    msg.update(item_id=manifest["item_id"], item_revision=int(manifest["item_revision"]),
               manifest_digest=manifest["manifest_digest"])
    return msg


def parse_manifest_frame(msg, manifest):
    transfer_id = _parse_base(msg, T_MANIFEST)
    if (transfer_id is None or msg.get("item_id") != manifest["item_id"]
            or msg.get("item_revision") != manifest["item_revision"]
            or isinstance(msg.get("item_revision"), bool)
            or msg.get("manifest_digest") != manifest["manifest_digest"]):
        return None
    return {"transfer_id": transfer_id}


def build_manifest_ack(transfer_id, manifest_digest, files, *, max_inflight_chunks,
                       chunk_size):
    msg = _base(T_MANIFEST_ACK, transfer_id)
    msg.update(manifest_digest=str(manifest_digest), files=list(files),
               window={"max_inflight_chunks": int(max_inflight_chunks),
                       "chunk_size": int(chunk_size)})
    return msg


def parse_manifest_ack(msg, manifest):
    transfer_id = _parse_base(msg, T_MANIFEST_ACK)
    if transfer_id is None or msg.get("manifest_digest") != manifest["manifest_digest"]:
        return None
    files = parse_resume_files(msg.get("files"), manifest)
    window = msg.get("window")
    if files is None or not isinstance(window, dict) or set(window) != {
            "max_inflight_chunks", "chunk_size"}:
        return None
    inflight = _uint(window["max_inflight_chunks"], 1024)
    chunk_size = _uint(window["chunk_size"], cfr2.MAX_BINARY_PAYLOAD_BYTES)
    if not inflight or not chunk_size:
        return None
    return {"transfer_id": transfer_id, "files": files,
            "max_inflight_chunks": inflight, "chunk_size": chunk_size}


def build_complete_frame(completion):
    msg = _base(T_COMPLETE, completion.transfer_id)
    msg.update(
        manifest_digest=completion.manifest_digest,
        total_bytes=int(completion.total_bytes),
        file_hashes={str(index): digest for index, digest in completion.file_hashes.items()},
        source_fingerprints={str(index): _thaw(value)
                             for index, value in completion.source_fingerprints.items()})
    return msg


def _thaw(value):
    if hasattr(value, "items"):
        return {key: _thaw(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(nested) for nested in value]
    return value


def parse_complete_frame(msg, manifest):
    """Validate a sender completion frame into :class:`SourceStreamCompletion`."""
    transfer_id = _parse_base(msg, T_COMPLETE)
    if transfer_id is None or msg.get("manifest_digest") != manifest["manifest_digest"]:
        return None
    total = _uint(msg.get("total_bytes"), MAX_LOGICAL_OFFSET)
    hashes = msg.get("file_hashes")
    fingerprints = msg.get("source_fingerprints")
    if (total is None or total != manifest["total_size"]
            or not isinstance(hashes, dict) or not isinstance(fingerprints, dict)
            or len(hashes) > cman2.MAX_ENTRIES or len(fingerprints) > cman2.MAX_ENTRIES):
        return None

    def _indexed(mapping):
        out = {}
        for key, value in mapping.items():
            if not isinstance(key, str) or not _INDEX_KEY.fullmatch(key):
                return None
            index = int(key)
            if index > MAX_ENTRY_INDEX:
                return None
            out[index] = value
        return out

    hash_map = _indexed(hashes)
    fingerprint_map = _indexed(fingerprints)
    if hash_map is None or fingerprint_map is None:
        return None
    if any(not _sha256(value) for value in hash_map.values()):
        return None
    if any(not isinstance(value, dict) for value in fingerprint_map.values()):
        return None
    try:
        return cstr2.SourceStreamCompletion(
            transfer_id, manifest["manifest_digest"], total, hash_map, fingerprint_map)
    except cstr2.StreamV2Error:
        return None


def build_complete_ack(transfer_id, manifest_digest, *, status, finalized_manifest_digest=None,
                       item_revision=None, reason=None):
    msg = _base(T_COMPLETE_ACK, transfer_id)
    msg.update(manifest_digest=str(manifest_digest), status=str(status),
               finalized_manifest_digest=finalized_manifest_digest,
               item_revision=item_revision,
               reason=normalize_reason(reason) if reason else "ok")
    return msg


def parse_complete_ack(msg, manifest):
    transfer_id = _parse_base(msg, T_COMPLETE_ACK)
    if (transfer_id is None or msg.get("manifest_digest") != manifest["manifest_digest"]
            or msg.get("status") not in ("completed", "failed")):
        return None
    reason = _reason(msg.get("reason"), default="ok")
    finalized = _sha256(msg.get("finalized_manifest_digest"), nullable=True)
    revision = msg.get("item_revision")
    if revision is not None and _uint(revision, MAX_LOGICAL_OFFSET) is None:
        return None
    if reason is None or finalized is False:
        return None
    if msg["status"] == "completed" and (finalized is None or revision is None):
        return None
    return {"transfer_id": transfer_id, "status": msg["status"], "reason": reason,
            "finalized_manifest_digest": finalized, "item_revision": revision}


# ── provisional receiver item ─────────────────────────────────────────
def provisional_item_for_receive(existing, manifest, *, providers=None):
    """Schema-2 provisional file item bound to ``manifest`` for the receiver.

    Rebuilt from the announced metadata row (``existing``) so the origin and
    display fields survive; ``batch_manifest`` binds the later publication.
    """
    manifest = cman2.validate_manifest(manifest)
    identity_entries = []
    for entry in manifest["entries"]:
        identity_entry = dict(entry)
        identity_entry.pop("index", None)
        identity_entries.append(identity_entry)
    provisional_sha = cman2.build_manifest(
        "metadata-identity", 0, identity_entries)["manifest_digest"]
    base = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    file_entries = [entry for entry in manifest["entries"] if entry["type"] == "file"]
    kind = base.get("kind")
    if kind not in (cbm.KIND_FILE, cbm.KIND_FILE_BATCH):
        kind = (cbm.KIND_FILE if len(manifest["entries"]) == 1 and len(file_entries) == 1
                else cbm.KIND_FILE_BATCH)
    metadata = {key: value for key, value in (base.get("metadata") or {}).items()
                if key not in ("flowshift_file_identity", "flowshift_hash_state",
                               "transfer_status", "transfer_error")}
    display = base.get("display_name") or (
        f"{manifest['file_count']} Dateien ({cbm.format_bytes(manifest['total_size'])})")
    item = {
        "schema_version": cbm.PERSISTED_ITEM_SCHEMA_VERSION,
        "item_id": manifest["item_id"],
        "item_revision": manifest["item_revision"],
        "sha256": provisional_sha,
        "legacy_provisional_sha256": provisional_sha,
        "metadata_identity_sha256": provisional_sha,
        "content_sha256": None,
        "hash_state": "unhashed",
        "batch_manifest": manifest,
        "kind": kind,
        "mime": base.get("mime") or "application/zip",
        "size": manifest["total_size"],
        "seq": int(base.get("seq", 0) or 0),
        "display_name": display,
        "preview_text": base.get("preview_text") or "\n".join(
            entry["path"] for entry in manifest["entries"][:20]),
        "preview_hash": base.get("preview_hash", ""),
        "file_count": manifest["file_count"],
        "directory_count": manifest["directory_count"],
        "total_file_size": manifest["total_size"],
        "pinned": bool(base.get("pinned", False)),
        "available": False,
        "source_available": False,
        "providers": copy.deepcopy(base.get("providers") or providers or []),
        "metadata": metadata,
        "payload": {"content_sha256": None, "encoding": "deterministic_zip",
                    "sha256": None, "size": None},
    }
    if isinstance(base.get("created_at"), (int, float)):
        item["created_at"] = base["created_at"]
    if isinstance(base.get("origin"), dict):
        item["origin"] = copy.deepcopy(base["origin"])
    return cbm.version_item(item, payload_state="receiving")


# ── channel nonce registry ───────────────────────────────────────────
class ChannelNonceRegistry:
    """Single-use, expiring channel nonces bound to one transfer and peer."""

    def __init__(self, lifetime=NONCE_LIFETIME_SECONDS, maximum=MAX_PENDING_NONCES):
        self._lock = threading.Lock()
        self._entries = {}
        self.lifetime = float(lifetime)
        self.maximum = int(maximum)

    def issue(self, transfer_id, identity, *, now=None):
        clock = time.monotonic() if now is None else float(now)
        nonce = secrets.token_hex(NONCE_HEX_LENGTH // 2)
        with self._lock:
            expired = [key for key, entry in self._entries.items() if entry["expires_at"] <= clock]
            for key in expired:
                self._entries.pop(key, None)
            # One live nonce per transfer: a re-offer invalidates the previous one.
            stale = [key for key, entry in self._entries.items()
                     if entry["transfer_id"] == transfer_id]
            for key in stale:
                self._entries.pop(key, None)
            while len(self._entries) >= self.maximum:
                self._entries.pop(next(iter(self._entries)))
            self._entries[nonce] = {"transfer_id": transfer_id, "identity": identity,
                                    "expires_at": clock + self.lifetime}
        return nonce

    def consume(self, nonce, transfer_id, *, now=None):
        """Return the bound identity when the nonce is valid; consumes it."""
        clock = time.monotonic() if now is None else float(now)
        if not isinstance(nonce, str):
            return None
        with self._lock:
            entry = self._entries.pop(nonce, None)
        if entry is None or entry["expires_at"] <= clock or entry["transfer_id"] != transfer_id:
            return None
        return entry["identity"]

    def revoke(self, transfer_id):
        with self._lock:
            for key in [key for key, entry in self._entries.items()
                        if entry["transfer_id"] == transfer_id]:
                self._entries.pop(key, None)

    def __len__(self):
        with self._lock:
            return len(self._entries)


# ── shared status helper ──────────────────────────────────────────────
def _percent(done, total):
    return round(done * 100.0 / total, 2) if total > 0 else 100.0


def _close_socket(sock):
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


# ── sender session ─────────────────────────────────────────────────────
class _Disconnected(Exception):
    """Internal: the data channel or control link failed; resume is possible."""


class _Cancelled(Exception):
    """Internal: the session was cancelled locally or by the peer, or is closing."""


class _Rejected(Exception):
    """Internal: the receiver rejected the offer; the session is terminal."""


class OutgoingTransferSession:
    """Sender state machine for one ``stream_v2`` transfer.

    Lifecycle: ``created -> preflight -> accepted -> sending_manifest ->
    transferring -> finalizing -> completed``; ``waiting_reconnect`` after a
    channel loss with a bounded resume loop; ``rejected``, ``cancelled`` (then
    ``purged`` after the peer ACK or the final-ACK timeout) and ``failed``.
    Runs in its own thread started by :meth:`start`; every wait is bounded by
    :class:`clipboard_transfer_control_v2.TransferTimeouts`.
    """

    def __init__(self, runtime, identity, item, *, transfer_id, journal_store,
                 profile_id, timeouts, open_channel_fn, flow_coordinator,
                 chunk_size=cstr2.DEFAULT_CHUNK_SIZE, preflight_lifetime_s=None):
        manifest = cman2.validate_manifest(item.get("batch_manifest"))
        self.runtime = runtime
        self.identity = str(identity)
        self.peer_id = peer_device_id(identity)
        self.transfer_id = cstr2._transfer_id(transfer_id)
        self.manifest = manifest
        self.item_id = manifest["item_id"]
        self._local_entries = copy.deepcopy(item.get("files") or [])
        self._journal_store = journal_store
        self._profile_id = str(profile_id)
        self._provider_id = f"device:{runtime.device_id}" if runtime.device_id else "local"
        self._timeouts = timeouts
        self._open_channel = open_channel_fn
        self._flow = flow_coordinator
        self._chunk_size = int(chunk_size)
        self._preflight_lifetime = preflight_lifetime_s
        self._lock = threading.RLock()
        self._state = "created"
        self._journal = None
        self._error = None
        self._reject_reason = None
        self._cancel_reason = None
        self._cancel_requested = False
        self._cancel_from_peer = False
        self._cancel_ack = threading.Event()
        self._accept_event = threading.Event()
        self._accept_message = None
        self._reject_message = None
        self._peer_connected = threading.Event()
        self._manifest_ack = None
        self._manifest_ack_event = threading.Event()
        self._complete_ack = None
        self._complete_ack_event = threading.Event()
        self._channel_broken = threading.Event()
        self._window = None
        self._sock = None
        self._writer = None
        self._reader_thread = None
        self._thread = None
        self._source = None
        self._bytes_done = 0
        self._resume_bytes = 0
        self._retry_count = 0
        self._current_index = None
        self._current_offset = 0
        self._rate = cctl2.TransferRateTracker()
        self._deadlines = cctl2.DeadlineTracker(timeouts, "created", time.monotonic())
        self._timeout_code = None
        self._resume_plan = None
        self._finalized_digest = None
        self._finalized_revision = None
        self._closing = False
        self._restored = False

    @classmethod
    def restore(cls, runtime, identity, item, journal, **kwargs):
        """Rebuild a sender session from a durable outgoing journal after a restart.

        The caller passes the still-existing store item (source paths) and the
        loaded journal. Identity (peer, profile, provider) and the manifest
        digest/entry set are validated with
        :func:`clipboard_resume_v2.validate_resume_match`; a mismatch raises
        :class:`clipboard_resume_v2.ResumeJournalError`. The session starts as
        ``paused`` and, once :meth:`start` runs, re-offers the transfer with
        ``T_RESUME_REQUEST`` so the receiver's durable offsets are credited.
        """
        journal = cres2.validate_journal(journal)
        if journal.direction != "outgoing":
            raise cres2.ResumeJournalError("direction_mismatch",
                                           "restore requires an outgoing journal")
        session = cls(runtime, identity, item, transfer_id=journal.transfer_id, **kwargs)
        cres2.validate_resume_match(
            journal, peer_id=session.peer_id, profile_id=session._profile_id,
            provider_id=session._provider_id, manifest=session.manifest)
        session._journal = journal
        session._retry_count = int(journal.retry_count)
        session._restored = True
        session._state = "paused"
        session._deadlines = cctl2.DeadlineTracker(session._timeouts, "paused", time.monotonic())
        return session

    # ── attributes shared with the runtime registry ──────────────────
    @property
    def state(self):
        return self._state

    @property
    def journal(self):
        return self._journal

    @property
    def bytes_done(self):
        return self._bytes_done

    @property
    def resume_bytes(self):
        return self._resume_bytes

    @property
    def error_code(self):
        if self._error is not None:
            return self._error.code
        return self._timeout_code

    @property
    def failure_code(self):
        return None if self._error is None else self._error.code

    @property
    def cancel_reason(self):
        return self._cancel_reason

    @property
    def reject_reason(self):
        return self._reject_reason

    @property
    def timeouts(self):
        return self._timeouts

    @property
    def durably_checkpointed(self):
        with self._lock:
            return (self._journal is not None and self._state in ("paused", "waiting_reconnect")
                    and self._journal.state == self._state and self._sock is None)

    @property
    def finalized_manifest_digest(self):
        return self._finalized_digest

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    # ── control-link events from the runtime ─────────────────────────
    def on_accept(self, parsed):
        with self._lock:
            if self._state not in ("preflight", "waiting_reconnect"):
                return False
            if (parsed["item_id"] != self.item_id
                    or parsed["manifest_digest"] != self.manifest["manifest_digest"]):
                return False
            self._accept_message = parsed
        self._accept_event.set()
        return True

    def on_reject(self, parsed):
        with self._lock:
            if self._state not in ("preflight", "waiting_reconnect"):
                return False
            if parsed["item_id"] != self.item_id:
                return False
            self._reject_message = parsed
        self._accept_event.set()
        return True

    def on_peer_cancel(self, parsed):
        with self._lock:
            if self._state in TERMINAL_STATES:
                return False
            self._cancel_from_peer = True
        self._request_cancel(parsed["reason"])
        return True

    def on_cancel_ack(self):
        self._cancel_ack.set()
        return True

    def on_peer_connected(self):
        self._peer_connected.set()

    # ── runtime-facing operations (duck-typed like IncomingTransferStage) ──
    def cancel(self, reason="user", *, now=None):
        """Cancel in any phase: stop reads, close the window, notify the peer."""
        with self._lock:
            if self._state in TERMINAL_STATES:
                return False
        self._request_cancel(reason)
        return True

    def _request_cancel(self, reason):
        with self._lock:
            if self._cancel_requested:
                return
            self._cancel_requested = True
            self._cancel_reason = str(reason or "user")
        self._accept_event.set()
        self._manifest_ack_event.set()
        self._complete_ack_event.set()
        self._peer_connected.set()
        window = self._window
        if window is not None:
            try:
                window.abort("cancelled")
            except Exception:
                pass
        if self._thread is None or not self._thread.is_alive():
            # No worker thread owns the lifecycle (not started): finish here.
            self._enter_cancelled(notify=not self._cancel_from_peer)

    def acknowledge_cancel(self, *, object_store=None):
        """Peer acknowledged our cancel (or we acknowledged theirs): purge."""
        with self._lock:
            if self._state != "cancelled":
                return False
        self._purge()
        return True

    def check_timeouts(self, now=None, *, object_store=None):
        """Report a fired phase deadline; the worker thread enforces the wait."""
        clock = time.monotonic() if now is None else float(now)
        with self._lock:
            fired = self._deadlines.check(clock)
            if fired is None:
                return None
            phase = self._deadlines.phase
            if phase == "cancelled":
                self._purge()
                return fired
            if self._thread is None or not self._thread.is_alive():
                self._timeout_code = f"{fired}_timeout"
                if phase in ("paused", "waiting_reconnect"):
                    self._set_failed(TransportV2Error("reconnect_timeout"))
                else:
                    self._set_failed(TransportV2Error(self._timeout_code))
            return fired

    def status(self, now=None):
        clock = time.monotonic() if now is None else float(now)
        with self._lock:
            entries = self.manifest["entries"]
            file_entries = [entry for entry in entries if entry["type"] == "file"]
            current = entries[self._current_index] if self._current_index is not None else None
            total = int(self.manifest["total_size"])
            done = min(self._bytes_done, total)
            journal = self._journal
            remaining = self._deadlines.remaining(clock)
            eta = self._rate.eta_seconds(total - done)
            return {
                "transfer_id": self.transfer_id,
                "item_id": self.item_id,
                "direction": "outgoing",
                "strategy": STRATEGY,
                "state": self._state,
                "journal_state": None if journal is None else journal.state,
                "current_file": None if current is None else current["path"],
                "file_index": None if current is None else int(current["index"]),
                "file_count": len(file_entries),
                "current_file_bytes": self._current_offset if current is not None else 0,
                "current_file_size": None if current is None else int(current["size"]),
                "bytes_done": done,
                "total_bytes": total,
                "percent": _percent(done, total),
                "rate_bytes_per_s": round(self._rate.rate_bytes_per_s, 3),
                "eta_seconds": None if eta is None else round(eta, 3),
                "resume_bytes": self._resume_bytes,
                "retry_count": self._retry_count,
                "provider": self._provider_id,
                "peer_id": self.peer_id,
                "preflight_state": ("accepted" if self._state not in ("created", "preflight", "rejected")
                                    else ("rejected" if self._state == "rejected" else "pending")),
                "error_code": self.error_code,
                "cancel_reason": self._cancel_reason,
                "reject_reason": self._reject_reason,
                "timeout": self._deadlines.timeout_name,
                "timeout_remaining_seconds": None if remaining is None else round(remaining, 3),
            }

    def close(self):
        """Stop the worker without cancelling: a resumable journal is retained."""
        with self._lock:
            self._closing = True
        self._accept_event.set()
        self._manifest_ack_event.set()
        self._complete_ack_event.set()
        self._peer_connected.set()
        self._channel_broken.set()
        window = self._window
        if window is not None:
            try:
                window.abort("closing")
            except Exception:
                pass
        _close_socket(self._sock)

    # ── worker ───────────────────────────────────────────────────────
    def start(self):
        if self._thread is not None:
            raise RuntimeError("outgoing session already started")
        self._thread = threading.Thread(
            target=self._run, name=f"stream-v2-send-{self.transfer_id[:8]}", daemon=True)
        self._thread.start()
        return self._thread

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.is_alive()

    def _enter(self, state, *, journal_state=None, retry_count=None):
        with self._lock:
            # Durable first: observers reading ``state`` must find the journal
            # already committed to the matching state.
            if journal_state and self._journal is not None:
                try:
                    self._journal = self._journal_store.commit(
                        cres2.update_journal_state(self._journal, journal_state,
                                                   retry_count=retry_count),
                        self._journal.generation)
                except cres2.ResumeJournalError as exc:
                    self.runtime._log_stream_v2(
                        f"stream_v2 outgoing journal update failed transfer={self.transfer_id} "
                        f"code={exc.code}", level="WARN")
            self._state = state
            self._deadlines.enter(state if state in cctl2.PHASE_TIMEOUTS
                                  or state in cctl2.UNBOUNDED_PHASES else "failed",
                                  time.monotonic())

    def _set_failed(self, error):
        with self._lock:
            if self._state in TERMINAL_STATES:
                return
            self._error = error if isinstance(error, TransportV2Error) else TransportV2Error(
                getattr(error, "code", "failed"), str(error))
            self._rate.pause()
        self._enter("failed", journal_state="failed")

    def _check_cancelled(self):
        with self._lock:
            if self._cancel_requested or self._closing:
                raise _Cancelled()

    def _send_control(self, message):
        try:
            self.runtime.send_fn(self.identity, message)
        except Exception as exc:
            raise _Disconnected(str(exc)) from exc

    def _wait(self, event, timeout_seconds):
        """Wait on ``event`` in slices so cancel and close are observed promptly."""
        deadline = time.monotonic() + float(timeout_seconds)
        while True:
            self._check_cancelled()
            remaining = deadline - time.monotonic()
            if event.is_set():
                return True
            if remaining <= 0:
                return False
            event.wait(min(remaining, CHANNEL_POLL_SECONDS))

    def _run(self):
        try:
            if self._restored:
                # Sender restart: the journal is ``paused``; re-offer via the
                # bounded resume path instead of a fresh offer (section 17).
                # The restart counts as one resume attempt.
                with self._lock:
                    self._retry_count += 1
                    retry_count = self._retry_count
                self._enter("waiting_reconnect", journal_state="waiting_reconnect",
                            retry_count=retry_count)
                self.runtime._log_stream_v2(
                    f"stream_v2 outgoing restored transfer={self.transfer_id} item={self.item_id} "
                    f"retry_count={self._retry_count}")
                self._phase_reconnect()
            else:
                try:
                    self._phase_offer(resume=False)
                except _Disconnected as exc:
                    raise TransportV2Error("peer_unavailable", str(exc)) from exc
            attempts = 0
            while True:
                try:
                    self._phase_transfer()
                    return
                except _Disconnected as exc:
                    attempts += 1
                    self._on_disconnected(str(exc))
                    if attempts > MAX_RESUME_ATTEMPTS:
                        raise TransportV2Error("reconnect_timeout", "too many resume attempts")
                    self._phase_reconnect()
        except _Rejected:
            return
        except _Cancelled:
            if self._closing and not self._cancel_requested:
                self._enter_closed()
            else:
                self._enter_cancelled(notify=not self._cancel_from_peer)
        except TransportV2Error as exc:
            self._teardown_channel()
            self._set_failed(exc)
            self.runtime._log_stream_v2(
                f"stream_v2 outgoing failed transfer={self.transfer_id} item={self.item_id} "
                f"code={exc.code} bytes_done={self._bytes_done}", level="WARN")
        except (cstr2.StreamV2Error, cfc2.FlowControlError) as exc:
            self._teardown_channel()
            self._set_failed(TransportV2Error(getattr(exc, "code", "flow_control"), str(exc)))
            self.runtime._log_stream_v2(
                f"stream_v2 outgoing failed transfer={self.transfer_id} item={self.item_id} "
                f"code={self.error_code} bytes_done={self._bytes_done}", level="WARN")
        except Exception as exc:  # pragma: no cover - defensive: never crash the runtime
            self._teardown_channel()
            self._set_failed(TransportV2Error("internal_error", type(exc).__name__))
            self.runtime._log_stream_v2(
                f"stream_v2 outgoing internal error transfer={self.transfer_id} "
                f"error={type(exc).__name__}", level="WARN")
        finally:
            self._teardown_channel()
            if self._source is not None:
                try:
                    self._source.close()
                except Exception:
                    pass
                self._source = None

    # phase 1: offer/preflight → accept or reject
    def _phase_offer(self, *, resume):
        self._accept_event.clear()
        with self._lock:
            self._accept_message = None
            self._reject_message = None
        msg_type = T_RESUME_REQUEST if resume else T_OFFER
        offer = build_offer(self.transfer_id, self.manifest, device_id=self.runtime.device_id,
                            profile_id=self._profile_id, resume=resume, msg_type=msg_type)
        if not resume:
            self._enter("preflight")
        self._send_control(offer)
        got = self._wait(self._accept_event, self._timeouts.preflight)
        with self._lock:
            accepted = self._accept_message
            rejected = self._reject_message
        if rejected is not None:
            if resume:
                raise TransportV2Error(f"resume_rejected:{rejected['reason']}",
                                       "receiver rejected the resume request")
            with self._lock:
                self._reject_reason = rejected["reason"]
                self._rate.pause()
            self._enter("rejected")
            self.runtime._log_stream_v2(
                f"stream_v2 offer rejected transfer={self.transfer_id} item={self.item_id} "
                f"reason={rejected['reason']}")
            raise _Rejected()
        if not got or accepted is None:
            if resume:
                raise _Disconnected("resume response timeout")
            self._timeout_code = "preflight_timeout"
            raise TransportV2Error("preflight_timeout", "peer did not answer the offer")
        if not resume:
            self._create_journal()
            self._enter("accepted", journal_state="accepted")
            self.runtime._log_stream_v2(
                f"stream_v2 offer accepted transfer={self.transfer_id} item={self.item_id} "
                f"files={self.manifest['file_count']} bytes={self.manifest['total_size']}")
        return accepted

    def _create_journal(self):
        try:
            self._journal = self._journal_store.create_outgoing(
                transfer_id=self.transfer_id, peer_id=self.peer_id,
                profile_id=self._profile_id, provider_id=self._provider_id,
                manifest=self.manifest)
        except cres2.ResumeJournalError as exc:
            raise TransportV2Error("journal_create_failed", str(exc)) from exc

    def _acceptance(self, accepted):
        lifetime = self._preflight_lifetime or accepted["preflight_lifetime_s"]
        now = time.monotonic()
        try:
            estimate = cpf2.estimate_stream_v2(self.manifest, free_bytes=cman2.HARD_MAX_LOGICAL_BYTES)
            return cpf2.accept_preflight(self.transfer_id, self.manifest, estimate, now=now,
                                         expires_at=now + max(1.0, float(lifetime)))
        except cpf2.PreflightV2Error as exc:
            raise TransportV2Error(exc.code, str(exc)) from exc

    # phase 2: channel, manifest, payload, completion
    def _phase_transfer(self):
        with self._lock:
            accepted = self._accept_message
        if accepted is None:
            raise TransportV2Error("preflight_required", "no acceptance available")
        acceptance = self._acceptance(accepted)
        self._admit_window()
        self._open_data_channel(accepted["channel_nonce"])
        # ``sending_manifest`` is only a valid journal transition from
        # ``accepted``; after a resume the journal stays ``waiting_reconnect``
        # until ``transferring`` is committed.
        with self._lock:
            journal_state = self._journal.state if self._journal is not None else None
        self._enter("sending_manifest",
                    journal_state="sending_manifest" if journal_state == "accepted" else None)
        self._manifest_ack_event.clear()
        with self._lock:
            self._manifest_ack = None
        self._send_frame(build_manifest_frame(self.transfer_id, self.manifest))
        got = self._wait(self._manifest_ack_event, self._timeouts.manifest_ack)
        with self._lock:
            ack = self._manifest_ack
        if self._channel_broken.is_set() and ack is None:
            raise _Disconnected("channel closed before manifest_ack")
        if not got or ack is None:
            self._timeout_code = "manifest_ack_timeout"
            raise TransportV2Error("manifest_ack_timeout", "receiver did not acknowledge the manifest")
        plan = None
        if any(item["durable_offset"] or item["completed"] for item in ack["files"]):
            plan = resume_plan_from_files(self._journal, self.manifest, ack["files"])
        with self._lock:
            self._resume_plan = plan
            self._resume_bytes = 0 if plan is None else plan.durable_bytes
            self._bytes_done = self._resume_bytes
        chunk_size = min(self._chunk_size, ack["chunk_size"])
        self._source = cstr2.SequentialFileStream(
            self.transfer_id, self.manifest, self._local_entries, chunk_size=chunk_size,
            cancelled=self._is_cancel_requested, resume_plan=plan,
            accepted_preflight=acceptance)
        self._init_resume_window(plan)
        self._enter("transferring", journal_state="transferring")
        self.runtime._log_stream_v2(
            f"stream_v2 {'resume' if plan is not None else 'start'} outgoing "
            f"transfer={self.transfer_id} item={self.item_id} resume_bytes={self._resume_bytes}")
        try:
            for chunk in self._source.iter_chunks():
                self._check_cancelled()
                self._track_and_send(chunk)
            completion = self._source.completion()
        except cstr2.StreamV2Error as exc:
            if exc.code == "cancelled":
                raise _Cancelled()
            if self._channel_broken.is_set():
                raise _Disconnected("channel closed during payload")
            raise
        except cfc2.FlowControlClosed as exc:
            self._check_cancelled()
            raise _Disconnected(str(exc))
        except cfc2.FlowControlTimeout:
            self._timeout_code = "window_ack_timeout"
            raise TransportV2Error("window_ack_timeout", "receiver window ACK timed out")
        finally:
            source = self._source
            self._source = None
            if source is not None:
                source.close()
        try:
            self._window.wait_until_empty(self._timeouts.window_ack)
        except cfc2.FlowControlTimeout:
            self._timeout_code = "window_ack_timeout"
            raise TransportV2Error("window_ack_timeout", "receiver window ACK timed out")
        except cfc2.FlowControlClosed as exc:
            self._check_cancelled()
            raise _Disconnected(str(exc))
        self._enter("finalizing", journal_state="finalizing")
        self._complete_ack_event.clear()
        with self._lock:
            self._complete_ack = None
        self._send_frame(build_complete_frame(completion))
        got = self._wait(self._complete_ack_event, self._timeouts.final_complete_ack)
        with self._lock:
            ack = self._complete_ack
        if ack is None:
            if self._channel_broken.is_set():
                raise _Disconnected("channel closed before complete_ack")
            self._timeout_code = "final_ack_timeout"
            raise TransportV2Error("final_ack_timeout", "receiver did not confirm completion")
        if ack["status"] != "completed":
            raise TransportV2Error(f"receiver_{ack['reason']}", "receiver finalization failed")
        with self._lock:
            self._finalized_digest = ack["finalized_manifest_digest"]
            self._finalized_revision = ack["item_revision"]
            self._rate.pause()
        self._teardown_channel()
        self._enter("completed", journal_state="completed")
        self.runtime._log_stream_v2(
            f"stream_v2 outgoing completed transfer={self.transfer_id} item={self.item_id} "
            f"bytes={self._bytes_done} revision={self._finalized_revision}")
        self.runtime._on_outgoing_stream_v2_completed(self)

    def _is_cancel_requested(self):
        with self._lock:
            return self._cancel_requested or self._closing

    def _open_data_channel(self, nonce):
        if self._open_channel is None:
            raise TransportV2Error("channel_unavailable", "no data channel opener")
        hello = build_channel_hello(self.transfer_id, nonce, self.runtime.device_id)
        try:
            sock = self._open_channel(self.identity, hello, self._timeouts.preflight)
        except TransportV2Error:
            raise
        except Exception as exc:
            raise _Disconnected(f"channel open failed: {type(exc).__name__}") from exc
        if sock is None:
            raise _Disconnected("channel open refused")
        self._channel_broken.clear()
        with self._lock:
            self._sock = sock
            self._writer = cfr2.TypedFrameWriter(sock)
        self._reader_thread = threading.Thread(
            target=self._reader_loop, args=(sock,),
            name=f"stream-v2-ack-{self.transfer_id[:8]}", daemon=True)
        self._reader_thread.start()

    def _send_frame(self, message):
        writer = self._writer
        if writer is None:
            raise _Disconnected("channel is closed")
        try:
            writer.send_json_control(message)
        except (OSError, cfr2.TypedFrameError) as exc:
            raise _Disconnected(f"channel send failed: {type(exc).__name__}") from exc

    def _admit_window(self):
        """Bounded, cancel-aware admission into the global/per-peer transfer limits."""
        deadline = time.monotonic() + self._timeouts.preflight
        while True:
            self._check_cancelled()
            try:
                window = self._flow.open_transfer(
                    self.transfer_id, self.identity, timeout=CHANNEL_POLL_SECONDS)
            except cfc2.FlowControlTimeout as exc:
                if time.monotonic() >= deadline:
                    raise TransportV2Error("admission_limit", str(exc)) from exc
                continue
            except cfc2.FlowControlClosed as exc:
                raise TransportV2Error("shutting_down", str(exc)) from exc
            except cfc2.FlowControlError as exc:
                raise TransportV2Error("flow_control", str(exc)) from exc
            with self._lock:
                self._window = window
            return

    def _init_resume_window(self, plan):
        window = self._window
        if window is None:
            raise _Disconnected("send window is closed")
        if plan is not None:
            for item in plan.files:
                if not item.completed and item.durable_offset:
                    window.initialize_resume(item.entry_index, item.durable_offset)

    def _track_and_send(self, chunk):
        window = self._window
        writer = self._writer
        if window is None or writer is None:
            raise _Disconnected("channel is closed")
        window.track_sent(chunk.entry_index, chunk.offset, chunk.payload,
                          timeout=self._timeouts.window_ack)
        try:
            writer.send_binary_payload(chunk.transfer_id, chunk.entry_index, chunk.offset,
                                       chunk.payload, checksum=chunk.checksum)
        except (OSError, cfr2.TypedFrameError) as exc:
            raise _Disconnected(f"channel send failed: {type(exc).__name__}") from exc
        clock = time.monotonic()
        with self._lock:
            self._current_index = chunk.entry_index
            self._current_offset = chunk.offset + len(chunk.payload)
            self._bytes_done += len(chunk.payload)
            self._rate.record(len(chunk.payload), clock)
            if self._deadlines.phase == "transferring":
                self._deadlines.progress(clock)

    def _reader_loop(self, sock):
        reader = cfr2.TypedFrameReader(sock)
        try:
            while not self._channel_broken.is_set():
                frame = reader.read_frame(CHANNEL_POLL_SECONDS)
                if frame is None:
                    if self._sock is not sock:
                        return
                    continue
                if not isinstance(frame, cfr2.JsonControlFrame):
                    raise cfr2.TypedFrameError("sender received a payload frame")
                self._on_channel_message(frame.message)
        except (ConnectionError, OSError, ValueError):
            pass
        except cfr2.TypedFrameError as exc:
            self.runtime._log_stream_v2(
                f"stream_v2 outgoing protocol error transfer={self.transfer_id} "
                f"detail={type(exc).__name__}", level="WARN")
        except cfc2.FlowControlError as exc:
            self.runtime._log_stream_v2(
                f"stream_v2 outgoing invalid ACK transfer={self.transfer_id}", level="WARN")
        finally:
            if self._sock is sock:
                self._channel_broken.set()
                window = self._window
                if window is not None:
                    try:
                        window.abort("channel closed")
                    except Exception:
                        pass
                self._manifest_ack_event.set()
                self._complete_ack_event.set()

    def _on_channel_message(self, message):
        msg_type = message.get("type") if isinstance(message, dict) else None
        if msg_type == T_WINDOW_ACK:
            window = self._window
            if window is None:
                return
            result = window.apply_ack(message)
            parsed = cbp.parse_stream_v2_ack(message)
            if parsed is not None and parsed["transfer_id"] == self.transfer_id:
                self._record_receiver_progress(parsed)
            if result["receiver_state"] in ("cancelled", "failed"):
                raise cfr2.TypedFrameError(f"receiver entered {result['receiver_state']}")
            return
        if msg_type == T_MANIFEST_ACK:
            parsed = parse_manifest_ack(message, self.manifest)
            if parsed is None or parsed["transfer_id"] != self.transfer_id:
                raise cfr2.TypedFrameError("invalid manifest_ack")
            with self._lock:
                self._manifest_ack = parsed
            self._manifest_ack_event.set()
            return
        if msg_type == T_COMPLETE_ACK:
            parsed = parse_complete_ack(message, self.manifest)
            if parsed is None or parsed["transfer_id"] != self.transfer_id:
                raise cfr2.TypedFrameError("invalid complete_ack")
            with self._lock:
                self._complete_ack = parsed
            self._complete_ack_event.set()
            return
        raise cfr2.TypedFrameError("unexpected channel control frame")

    def _record_receiver_progress(self, ack):
        with self._lock:
            journal = self._journal
            if journal is None or journal.state != "transferring":
                return
            entry = journal.entries[ack["entry_index"]] if ack["entry_index"] < len(journal.entries) else None
            if entry is None or entry["type"] != "file":
                return
            if (ack["durable_offset"] <= entry["receiver_durable_offset"]
                    and ack["verified_offset"] <= entry["receiver_verified_offset"]):
                return
            try:
                candidate = cres2.update_outgoing_progress(
                    journal, ack["entry_index"],
                    receiver_verified_offset=max(ack["verified_offset"],
                                                 entry["receiver_verified_offset"]),
                    receiver_durable_offset=max(ack["durable_offset"],
                                                entry["receiver_durable_offset"]))
                self._journal = self._journal_store.commit(candidate, journal.generation)
            except cres2.ResumeJournalError:
                pass

    def _teardown_channel(self):
        with self._lock:
            sock = self._sock
            self._sock = None
            self._writer = None
            window = self._window
            self._window = None
        if window is not None:
            try:
                window.abort("closed")
            except Exception:
                pass
        _close_socket(sock)
        reader = self._reader_thread
        self._reader_thread = None
        if reader is not None and reader is not threading.current_thread():
            reader.join(2.0)

    # phase 3: disconnect handling and resume
    def _on_disconnected(self, detail):
        self._teardown_channel()
        with self._lock:
            self._rate.pause()
            self._retry_count += 1
            self._current_index = None
            retry_count = self._retry_count
        self._enter("waiting_reconnect", journal_state="waiting_reconnect",
                    retry_count=retry_count)
        self.runtime._log_stream_v2(
            f"stream_v2 outgoing disconnected transfer={self.transfer_id} item={self.item_id} "
            f"bytes_done={self._bytes_done} detail={normalize_reason(detail)}")

    def _phase_reconnect(self):
        deadline = time.monotonic() + self._timeouts.reconnect_wait
        self._peer_connected.clear()
        while True:
            self._check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._timeout_code = "reconnect_timeout"
                raise TransportV2Error("reconnect_timeout", "peer did not reconnect in time")
            try:
                accepted = self._phase_offer(resume=True)
            except _Disconnected:
                self._wait(self._peer_connected, min(RECONNECT_RETRY_SECONDS, max(0.0, remaining)))
                self._peer_connected.clear()
                continue
            with self._lock:
                self._accept_message = accepted
            self._enter("accepted")
            return

    # phase 4: terminal cleanup
    def _enter_closed(self):
        """Runtime shutdown: retain the journal as ``paused`` for a later resume."""
        self._teardown_channel()
        with self._lock:
            if self._state in TERMINAL_STATES:
                return
            self._rate.pause()
            has_journal = self._journal is not None
        if has_journal:
            self._enter("paused", journal_state="paused")
        else:
            self._enter("failed")
            with self._lock:
                self._error = TransportV2Error("shutting_down")

    def _enter_cancelled(self, *, notify):
        self._teardown_channel()
        with self._lock:
            if self._state in ("cancelled", "purged", "completed", "rejected"):
                return
            self._rate.pause()
        had_journal = self._journal is not None
        self._enter("cancelled", journal_state="cancelled" if had_journal else None)
        self.runtime._log_stream_v2(
            f"stream_v2 outgoing cancelled transfer={self.transfer_id} item={self.item_id} "
            f"reason={self._cancel_reason} bytes_done={self._bytes_done}")
        if notify:
            try:
                self.runtime.send_fn(self.identity, build_cancel(self.transfer_id, self._cancel_reason))
            except Exception:
                pass
            if self._thread is threading.current_thread():
                if self._cancel_ack.wait(self._timeouts.final_complete_ack):
                    self._purge()
        else:
            try:
                self.runtime.send_fn(self.identity, build_cancel_ack(self.transfer_id))
            except Exception:
                pass
            self._purge()

    def _purge(self):
        with self._lock:
            if self._state not in ("cancelled",):
                return
            journal = self._journal
            self._journal = None
            self._state = "purged"
            self._deadlines.enter("purged", time.monotonic())
        if journal is not None:
            try:
                self._journal_store.purge("outgoing", self.transfer_id)
            except cres2.ResumeJournalError:
                pass
        self.runtime._log_stream_v2(
            f"stream_v2 cleanup transfer={self.transfer_id} cause=cancel_ack state=purged")


# ── receiver session ───────────────────────────────────────────────────
class ReceiverSession:
    """Receiver wrapper around one :class:`IncomingTransferStage` plus its channel."""

    def __init__(self, runtime, identity, stage, manifest, *, timeouts, limits):
        self.runtime = runtime
        self.identity = str(identity)
        self.peer_id = peer_device_id(identity)
        self.stage = stage
        self.manifest = manifest
        self.transfer_id = stage.transfer_id
        self.item_id = manifest["item_id"]
        self._timeouts = timeouts
        self._limits = limits
        self._lock = threading.RLock()
        self._sock = None
        self._thread = None
        self._closed = threading.Event()
        self._cancel_notified = False
        self._published = None
        self._published_item = None

    def __getattr__(self, name):
        # Delegate read-only stage attributes (state, journal, bytes_done, ...).
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.stage, name)

    @property
    def state(self):
        return self.stage.state

    @property
    def journal(self):
        return self.stage.journal

    @property
    def bytes_done(self):
        return self.stage.bytes_done

    @property
    def error_code(self):
        return self.stage.error_code

    @property
    def cancel_reason(self):
        return self.stage.cancel_reason

    @property
    def durably_checkpointed(self):
        return self.stage.durably_checkpointed and self._sock is None

    @property
    def published_item(self):
        return self._published_item

    def replace_stage(self, stage):
        """Adopt a reopened stage after a resume request."""
        with self._lock:
            self.stage = stage
            self._cancel_notified = False

    def has_channel(self):
        return self._sock is not None

    def status(self, now=None):
        status = self.stage.status(now)
        status["peer_id"] = self.peer_id
        status["channel"] = "open" if self._sock is not None else "closed"
        return status

    def check_timeouts(self, now=None, *, object_store=None):
        with self._lock:
            fired = self.stage.check_timeouts(now, object_store=object_store)
        if fired is not None and self.stage.state != "receiving":
            self.close_channel()
        return fired

    def cancel(self, reason="user", *, now=None):
        """Cancel the stage and notify the sender exactly once."""
        with self._lock:
            performed = self.stage.cancel(reason, now=now)
        self.close_channel()
        if performed:
            self._notify_cancel(reason)
        return performed

    def _notify_cancel(self, reason):
        with self._lock:
            if self._cancel_notified:
                return
            self._cancel_notified = True
        try:
            self.runtime.send_fn(self.identity, build_cancel(self.transfer_id, reason))
        except Exception:
            pass

    def cancel_from_peer(self, reason):
        """The sender cancelled: cancel silently, ACK, and purge immediately."""
        with self._lock:
            self._cancel_notified = True
            try:
                performed = self.stage.cancel(f"peer:{reason}")
            except cstr2.StreamV2Error as exc:
                # The ACK must still go out so the peer can purge its partials;
                # our own partials fall to the final-ack timeout purge.
                performed = False
                self.runtime._log_stream_v2(
                    f"stream_v2 peer cancel failed transfer={self.transfer_id} code={exc.code}",
                    level="WARN")
        self.close_channel()
        try:
            self.runtime.send_fn(self.identity, build_cancel_ack(self.transfer_id))
        except Exception:
            pass
        return performed or self.stage.state == "cancelled"

    def acknowledge_cancel(self, *, object_store=None):
        with self._lock:
            return self.stage.acknowledge_cancel(object_store=object_store)

    def pause(self, disconnected=False, *, now=None):
        with self._lock:
            return self.stage.pause(disconnected=disconnected, now=now)

    def close(self):
        self.close_channel()
        with self._lock:
            try:
                self.stage.close()
            except cstr2.StreamV2Error:
                pass

    def close_channel(self):
        with self._lock:
            sock = self._sock
            self._sock = None
        self._closed.set()
        _close_socket(sock)

    # ── channel ownership ─────────────────────────────────────────────
    def attach_channel(self, sock):
        with self._lock:
            if self._sock is not None:
                return False
            if self.stage.state != "receiving":
                return False
            self._sock = sock
            self._closed.clear()
            self._thread = threading.Thread(
                target=self._channel_loop, args=(sock,),
                name=f"stream-v2-recv-{self.transfer_id[:8]}", daemon=True)
            self._thread.start()
            return True

    def join(self, timeout=None):
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _channel_loop(self, sock):
        reader = cfr2.TypedFrameReader(sock)
        writer = cfr2.TypedFrameWriter(sock)
        batcher = cfc2.CumulativeAckBatcher(self.transfer_id, self._limits)
        journal = self.stage.journal
        if journal is not None:
            for entry in journal.entries:
                if entry["type"] == "file" and (entry["durable_offset"] or entry["completed"]):
                    try:
                        batcher.restore_entry(entry["index"], verified_offset=entry["durable_offset"],
                                              durable_offset=entry["durable_offset"])
                    except cfc2.FlowControlError:
                        pass
        manifest_seen = False
        try:
            while not self._closed.is_set() and self._sock is sock:
                frame = reader.read_frame(CHANNEL_POLL_SECONDS)
                if frame is None:
                    if self.stage.state != "receiving":
                        break
                    for ack in batcher.poll():
                        writer.send_json_control(ack)
                    continue
                if isinstance(frame, cfr2.JsonControlFrame):
                    message = frame.message
                    msg_type = message.get("type")
                    if msg_type == T_MANIFEST:
                        if manifest_seen or parse_manifest_frame(message, self.manifest) is None \
                                or message.get("transfer_id") != self.transfer_id:
                            raise cfr2.TypedFrameError("invalid manifest frame")
                        manifest_seen = True
                        writer.send_json_control(self._manifest_ack())
                        continue
                    if msg_type == T_COMPLETE:
                        completion = parse_complete_frame(message, self.manifest)
                        if not manifest_seen or completion is None \
                                or completion.transfer_id != self.transfer_id:
                            raise cfr2.TypedFrameError("invalid complete frame")
                        with self._lock:
                            ack = self._finalize(completion)
                        writer.send_json_control(ack)
                        if ack["status"] == "completed":
                            self._published = True
                        break
                    raise cfr2.TypedFrameError("unexpected channel control frame")
                if not manifest_seen:
                    raise cfr2.TypedFrameError("payload before manifest")
                if frame.transfer_id != self.transfer_id:
                    raise cfr2.TypedFrameError("payload for another transfer")
                try:
                    chunk = cstr2.PayloadChunk(frame.transfer_id, frame.entry_index, frame.offset,
                                               frame.payload, frame.checksum or b"")
                except cstr2.StreamV2Error as exc:
                    raise cfr2.TypedFrameError(exc.code) from exc
                with self._lock:
                    # Serialize against control-thread cancel/pause/timeout: they
                    # commit the same journal and close the same part handle.
                    if self.stage.state != "receiving":
                        break
                    try:
                        written = self.stage.accept(chunk)
                    except cstr2.StreamV2Error as exc:
                        if self.stage.state in ("cancelled", "purged", "paused", "waiting_reconnect") \
                                or self._closed.is_set():
                            break  # cancelled or paused concurrently: not a protocol violation
                        if exc.code == "invalid_chunk" or not exc.retryable:
                            raise cfr2.TypedFrameError(exc.code) from exc
                        # Retryable I/O failure (disk full, write): keep the journal,
                        # close the channel so the sender pauses and can resume.
                        self.runtime._log_stream_v2(
                            f"stream_v2 receive failed transfer={self.transfer_id} code={exc.code} "
                            f"bytes_done={self.stage.bytes_done}", level="WARN")
                        break
                ack = batcher.record_verified(
                    written.entry_index, written.offset, written.length,
                    durable_offset=written.durable_offset,
                    file_complete=written.file_complete)
                if ack is not None:
                    writer.send_json_control(ack)
        except cfr2.TypedFrameError as exc:
            self.runtime._log_stream_v2(
                f"stream_v2 receive protocol error transfer={self.transfer_id} "
                f"code={normalize_reason(str(exc)[:64])}", level="WARN")
            self._protocol_failure()
        except cfc2.FlowControlError:
            self._protocol_failure()
        except (ConnectionError, OSError, ValueError):
            self._on_disconnected()
        except cstr2.StreamV2Error as exc:
            self.runtime._log_stream_v2(
                f"stream_v2 receive failed transfer={self.transfer_id} code={exc.code}",
                level="WARN")
        finally:
            with self._lock:
                if self._sock is sock:
                    self._sock = None
            _close_socket(sock)
            with self._lock:
                if self.stage.state == "receiving" and self._published is None:
                    # Channel ended without completion: retain durable progress.
                    try:
                        self.stage.pause(disconnected=True)
                    except cstr2.StreamV2Error:
                        pass

    def _manifest_ack(self):
        journal = self.stage.journal
        files = resume_files_from_journal(journal) if journal is not None else [
            {"index": entry["index"], "durable_offset": 0, "prefix_sha256": None,
             "completed": False}
            for entry in self.manifest["entries"] if entry["type"] == "file"]
        return build_manifest_ack(
            self.transfer_id, self.manifest["manifest_digest"], files,
            max_inflight_chunks=self._limits.max_inflight_chunks_per_transfer,
            chunk_size=self._limits.chunk_size)

    def _finalize(self, completion):
        try:
            self.stage.finalize(completion)
        except cstr2.StreamV2Error as exc:
            self.runtime._log_stream_v2(
                f"stream_v2 verification failed transfer={self.transfer_id} code={exc.code}",
                level="WARN")
            return build_complete_ack(self.transfer_id, self.manifest["manifest_digest"],
                                      status="failed", reason=exc.code)
        try:
            item = self.runtime._publish_received_stream_v2(self)
        except (cstr2.StreamV2Error, ValueError, OSError) as exc:
            code = getattr(exc, "code", None) or "publication_failed"
            self.runtime._log_stream_v2(
                f"stream_v2 publication failed transfer={self.transfer_id} code={normalize_reason(code)}",
                level="WARN")
            return build_complete_ack(self.transfer_id, self.manifest["manifest_digest"],
                                      status="failed", reason=code)
        self._published_item = item
        finalized = item.get("batch_manifest") or {}
        return build_complete_ack(
            self.transfer_id, self.manifest["manifest_digest"], status="completed",
            finalized_manifest_digest=finalized.get("manifest_digest"),
            item_revision=int(item.get("item_revision", 0)))

    def _protocol_failure(self):
        """Hostile or malformed input: cancel, tell the sender, retain until ACK."""
        with self._lock:
            try:
                performed = self.stage.cancel("protocol_error")
            except cstr2.StreamV2Error:
                performed = False
        if performed:
            self._notify_cancel("protocol_error")

    def _on_disconnected(self):
        with self._lock:
            if self.stage.state == "receiving":
                try:
                    self.stage.pause(disconnected=True)
                except cstr2.StreamV2Error:
                    pass
        self.runtime._log_stream_v2(
            f"stream_v2 receive disconnected transfer={self.transfer_id} item={self.item_id} "
            f"bytes_done={self.stage.bytes_done} state={self.stage.state}")


# ── channel opener for sockets (used by tray and tests) ───────────────
def open_channel_socket(connect_fn, hello, timeout):
    """Dial, send one legacy JSON hello, require a positive JSON ack, return the socket.

    ``connect_fn(timeout) -> socket``. The reader buffer must be empty after the
    ACK: no post-ACK JSON bytes may precede the typed frames.
    """
    import runtime_model as rm
    sock = connect_fn(timeout)
    try:
        sock.settimeout(timeout)
        rm.send_msg(sock, hello)
        reader = rm.FramedReader(sock)
        deadline = time.monotonic() + float(timeout)
        reply = None
        while reply is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportV2Error("channel_timeout", "channel ack timed out")
            reply = reader.read_message(min(1.0, remaining))
        parsed = parse_channel_ack(reply)
        if parsed is None or parsed["transfer_id"] != hello["transfer_id"]:
            raise TransportV2Error("channel_rejected", "invalid channel ack")
        if not parsed["ok"]:
            raise TransportV2Error(f"channel_{parsed['reason']}", "channel refused")
        if reader._buf:
            raise TransportV2Error("channel_protocol", "unexpected bytes after channel ack")
        sock.setblocking(True)
        return sock
    except BaseException:
        _close_socket(sock)
        raise
