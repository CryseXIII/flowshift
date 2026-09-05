"""Strategy-aware, transfer-bound storage preflight for clipboard stream V2."""
from __future__ import annotations

import hashlib
import math
import re
import time
import uuid
from dataclasses import dataclass

import clipboard_manifest_v2 as manifest_v2
import clipboard_resume_v2 as resume_v2


STRATEGY_STREAM_V2 = "stream_v2"
UINT64_MAX = (1 << 64) - 1
MINIMUM_SAFETY_MARGIN_BYTES = 512 * 1024 * 1024
MINIMUM_METADATA_BYTES = 4096


class PreflightV2Error(ValueError):
    """Path-free preflight rejection with a stable machine-readable code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = str(code)


def _uint(value, name):
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 0 <= value <= UINT64_MAX):
        raise PreflightV2Error("invalid_size_metadata", f"{name} is outside uint64")
    return value


def _add(*values):
    total = 0
    for value in values:
        total += _uint(value, "storage estimate")
        if total > UINT64_MAX:
            raise PreflightV2Error("invalid_size_metadata", "storage estimate exceeds uint64")
    return total


def _transfer_id(value):
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
    except (ValueError, AttributeError):
        parsed = None
    if parsed is None or parsed.int == 0:
        raise PreflightV2Error("preflight_mismatch", "transfer_id must be a non-null UUID")
    return parsed.hex


def _timestamp(value):
    try:
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value)):
            raise ValueError
        return float(value)
    except (ValueError, OverflowError) as exc:
        raise PreflightV2Error("preflight_mismatch", "preflight time must be finite") from exc


@dataclass(frozen=True)
class StorageEstimate:
    strategy: str
    logical_bytes: int
    durable_resume_bytes: int
    remaining_payload_bytes: int
    journal_overhead_bytes: int
    manifest_overhead_bytes: int
    index_overhead_bytes: int
    materialization_bytes: int
    peak_required_bytes: int
    safety_margin_bytes: int
    free_bytes: int
    allowed: bool
    reason: str | None
    item_id: str
    item_revision: int
    manifest_digest: str
    journal_transfer_id: str | None = None
    journal_generation: int | None = None
    journal_digest: str | None = None
    resume_plan: resume_v2.ResumePlan | None = None

    def __post_init__(self):
        for name in (
                "logical_bytes", "durable_resume_bytes", "remaining_payload_bytes",
                "journal_overhead_bytes", "manifest_overhead_bytes", "index_overhead_bytes",
                "materialization_bytes", "peak_required_bytes", "safety_margin_bytes",
                "free_bytes", "item_revision"):
            _uint(getattr(self, name), name)
        if (self.strategy != STRATEGY_STREAM_V2
                or not isinstance(self.item_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.item_id)
                or not isinstance(self.manifest_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", self.manifest_digest)):
            raise PreflightV2Error("preflight_mismatch", "estimate identity is invalid")
        if (self.logical_bytes != _add(self.durable_resume_bytes, self.remaining_payload_bytes)
                or self.peak_required_bytes != _add(
                    self.remaining_payload_bytes, self.journal_overhead_bytes,
                    self.manifest_overhead_bytes, self.index_overhead_bytes,
                    self.materialization_bytes)
                or self.journal_overhead_bytes < MINIMUM_METADATA_BYTES
                or self.manifest_overhead_bytes < MINIMUM_METADATA_BYTES
                or self.safety_margin_bytes < max(
                    MINIMUM_SAFETY_MARGIN_BYTES, self.peak_required_bytes // 20)):
            raise PreflightV2Error("invalid_size_metadata", "estimate accounting is inconsistent")
        needed = _add(self.peak_required_bytes, self.safety_margin_bytes)
        if (not isinstance(self.allowed, bool)
                or (self.allowed and (self.reason is not None or self.free_bytes < needed))
                or (not self.allowed and self.reason not in ("too_large", "policy", "disk_full"))
                or (self.reason == "disk_full" and self.free_bytes >= needed)):
            raise PreflightV2Error("invalid_size_metadata", "estimate decision is inconsistent")
        if self.journal_transfer_id is None:
            if (self.journal_generation is not None or self.journal_digest is not None
                    or self.resume_plan is not None or self.durable_resume_bytes):
                raise PreflightV2Error("preflight_mismatch", "resume binding is incomplete")
        else:
            object.__setattr__(self, "journal_transfer_id", _transfer_id(self.journal_transfer_id))
            _uint(self.journal_generation, "journal_generation")
            if (not isinstance(self.journal_digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", self.journal_digest)
                    or not isinstance(self.resume_plan, resume_v2.ResumePlan)):
                raise PreflightV2Error("preflight_mismatch", "resume binding is invalid")
            try:
                self.resume_plan.__post_init__()
            except resume_v2.ResumeJournalError as exc:
                raise PreflightV2Error("preflight_mismatch", "resume binding is invalid") from exc
            plan = self.resume_plan
            if ((plan.transfer_id, plan.item_id, plan.item_revision, plan.manifest_digest)
                    != (self.journal_transfer_id, self.item_id, self.item_revision,
                        self.manifest_digest)
                    or _add(*(entry.size for entry in plan.files)) != self.logical_bytes
                    or _add(*(entry.durable_offset for entry in plan.files))
                    != self.durable_resume_bytes):
                raise PreflightV2Error("preflight_mismatch", "resume accounting is inconsistent")


@dataclass(frozen=True)
class AcceptedPreflight:
    transfer_id: str
    strategy: str
    item_id: str
    item_revision: int
    manifest_digest: str
    accepted_at: float
    expires_at: float
    estimate: StorageEstimate

    def __post_init__(self):
        object.__setattr__(self, "transfer_id", _transfer_id(self.transfer_id))
        if self.strategy != STRATEGY_STREAM_V2:
            raise PreflightV2Error("preflight_mismatch", "unsupported accepted strategy")
        _timestamp(self.accepted_at)
        _timestamp(self.expires_at)
        if self.expires_at <= self.accepted_at:
            raise PreflightV2Error("preflight_mismatch", "preflight lifetime is invalid")
        if not isinstance(self.estimate, StorageEstimate) or not self.estimate.allowed:
            raise PreflightV2Error("preflight_rejected", "preflight estimate was not accepted")
        self.estimate.__post_init__()
        _uint(self.item_revision, "item_revision")
        if ((self.item_id, self.item_revision, self.manifest_digest)
                != (self.estimate.item_id, self.estimate.item_revision,
                    self.estimate.manifest_digest)
                or (self.estimate.journal_transfer_id is not None
                    and self.transfer_id != self.estimate.journal_transfer_id)):
            raise PreflightV2Error("preflight_mismatch", "acceptance does not match estimate")


def _incoming_journal(manifest, journal):
    if journal is None:
        return None
    try:
        journal = resume_v2.validate_journal(journal)
        resume_v2.validate_resume_match(
            journal, peer_id=journal.peer_id, profile_id=journal.profile_id,
            provider_id=journal.provider_id, manifest=manifest)
    except resume_v2.ResumeJournalError as exc:
        raise PreflightV2Error("preflight_mismatch", "resume journal is invalid") from exc
    if journal.direction != "incoming":
        raise PreflightV2Error("preflight_mismatch", "resume journal does not match manifest")
    return journal


def _metadata_bounds(manifest):
    """Bound actual final encodings, including old/new atomic rewrite overlap."""
    try:
        finalized = manifest_v2.finalize_manifest(manifest, {
            entry["index"]: entry["sha256"] or "f" * 64
            for entry in manifest["entries"] if entry["type"] == "file"})
        manifest_size = max(len(manifest_v2.canonical_manifest_bytes(manifest)),
                            len(manifest_v2.canonical_manifest_bytes(finalized)))
        # The pure journal constructor also checks the stricter fingerprint schema.
        journal = resume_v2._new_journal(
            "incoming", transfer_id="1" * 32, peer_id='"' * 256,
            profile_id='"' * 256, provider_id='"' * 256, manifest=manifest,
            strategy=STRATEGY_STREAM_V2, state="waiting_reconnect", created_ns=UINT64_MAX)
        value = journal.to_dict()
        value.update(generation=UINT64_MAX, retry_count=UINT64_MAX)
        for entry in value["entries"]:
            if entry["type"] == "file":
                digest = entry["expected_sha256"] or (
                    "f" * 64 if entry["size"] else hashlib.sha256(b"").hexdigest())
                entry.update(verified_offset=entry["size"], durable_offset=entry["size"],
                             prefix_sha256=digest, receiver_sha256=digest,
                             completed=True, storage_state="verified")
        value["journal_digest"] = resume_v2.journal_digest(value)
        # JSON false is one byte longer than true at intermediate checkpoints.
        journal_size = _add(len(resume_v2.canonical_journal_bytes(value)),
                            manifest["file_count"])
        if journal_size > resume_v2.MAX_JOURNAL_BYTES:
            raise PreflightV2Error("invalid_size_metadata", "final journal exceeds size limit")
    except (manifest_v2.ManifestValidationError, resume_v2.ResumeJournalError) as exc:
        raise PreflightV2Error("invalid_size_metadata", "final metadata is invalid") from exc
    return (max(MINIMUM_METADATA_BYTES, _add(manifest_size, manifest_size)),
            max(MINIMUM_METADATA_BYTES, _add(journal_size, journal_size)))


def _validate_estimate_manifest(estimate, manifest):
    estimate.__post_init__()
    if ((estimate.item_id, estimate.item_revision, estimate.manifest_digest,
         estimate.logical_bytes) !=
            (manifest["item_id"], manifest["item_revision"], manifest["manifest_digest"],
             manifest["total_size"])):
        raise PreflightV2Error("preflight_mismatch", "estimate does not match manifest")
    manifest_bytes, journal_bytes = _metadata_bounds(manifest)
    if (estimate.manifest_overhead_bytes < manifest_bytes
            or estimate.journal_overhead_bytes < journal_bytes):
        raise PreflightV2Error("invalid_size_metadata", "estimate under-reserves metadata")
    if estimate.resume_plan is not None:
        try:
            resume_v2.validate_resume_plan(
                estimate.resume_plan, estimate.journal_transfer_id, manifest)
        except resume_v2.ResumeJournalError as exc:
            raise PreflightV2Error("preflight_mismatch", "resume estimate mismatches manifest") from exc


def estimate_stream_v2(manifest, *, free_bytes, incoming_journal=None,
                       index_overhead_bytes=0, materialization_bytes=0,
                       hard_item_bytes=None, auto_limit_bytes=None,
                       allow_manual=False):
    """Estimate additional peak bytes without counting a ZIP or object copy.

    free_bytes is currently available space, not volume capacity. The caller
    supplies index_overhead_bytes for its full atomic rewrite plus index growth,
    and materialization_bytes only when a separate payload copy is required.
    Journal/manifest bounds include two maximum encodings, not existing payload.
    """
    try:
        manifest = manifest_v2.validate_manifest(manifest)
    except manifest_v2.ManifestValidationError as exc:
        raise PreflightV2Error("invalid_size_metadata", "manifest is invalid") from exc
    free_bytes = _uint(free_bytes, "free_bytes")
    index_overhead_bytes = _uint(index_overhead_bytes, "index_overhead_bytes")
    materialization_bytes = _uint(materialization_bytes, "materialization_bytes")
    if hard_item_bytes is not None:
        _uint(hard_item_bytes, "hard_item_bytes")
    if auto_limit_bytes is not None:
        _uint(auto_limit_bytes, "auto_limit_bytes")
    if not isinstance(allow_manual, bool):
        raise PreflightV2Error("invalid_size_metadata", "allow_manual must be boolean")
    logical = manifest["total_size"]
    journal = _incoming_journal(manifest, incoming_journal)
    plan = None
    if journal is not None:
        try:
            plan = resume_v2.ResumePlan(
                journal.transfer_id, journal.peer_id, journal.profile_id, journal.provider_id,
                journal.item_id, journal.item_revision, journal.manifest_digest,
                journal.entry_set_digest, tuple(resume_v2.ResumeFile(
                    entry["index"], entry["size"], entry["durable_offset"],
                    entry["prefix_sha256"], entry["completed"])
                    for entry in journal.entries if entry["type"] == "file"))
        except resume_v2.ResumeJournalError as exc:
            raise PreflightV2Error("preflight_mismatch", "journal cannot be resumed") from exc
    durable = _add(*(entry.durable_offset for entry in plan.files)) if plan else 0
    remaining = logical - durable
    manifest_bytes, journal_bytes = _metadata_bounds(manifest)
    peak = _add(remaining, journal_bytes, manifest_bytes,
                index_overhead_bytes, materialization_bytes)
    margin = max(MINIMUM_SAFETY_MARGIN_BYTES, peak // 20)
    total = _add(peak, margin)
    reason = None
    if hard_item_bytes is not None and logical > hard_item_bytes:
        reason = "too_large"
    elif (auto_limit_bytes is not None and not allow_manual
          and logical > auto_limit_bytes):
        reason = "policy"
    elif free_bytes < total:
        reason = "disk_full"
    return StorageEstimate(
        STRATEGY_STREAM_V2, logical, durable, remaining, journal_bytes,
        manifest_bytes, index_overhead_bytes, materialization_bytes, peak,
        margin, free_bytes, reason is None, reason,
        manifest["item_id"], manifest["item_revision"], manifest["manifest_digest"],
        journal.transfer_id if journal else None, journal.generation if journal else None,
        journal.journal_digest if journal else None, plan)


def accept_preflight(transfer_id, manifest, estimate, *, expires_at, now=None):
    """Create evidence bound to exactly one transfer and manifest revision."""
    if not isinstance(estimate, StorageEstimate) or not estimate.allowed:
        raise PreflightV2Error("preflight_rejected", "storage preflight was rejected")
    try:
        manifest = manifest_v2.validate_manifest(manifest)
    except manifest_v2.ManifestValidationError as exc:
        raise PreflightV2Error("preflight_mismatch", "manifest is invalid") from exc
    _validate_estimate_manifest(estimate, manifest)
    accepted_at = _timestamp(time.monotonic() if now is None else now)
    return AcceptedPreflight(
        transfer_id, STRATEGY_STREAM_V2, manifest["item_id"],
        manifest["item_revision"], manifest["manifest_digest"],
        accepted_at, _timestamp(expires_at), estimate)


def validate_acceptance(acceptance, transfer_id, manifest, *, now=None,
                        incoming_journal=None, resume_plan=None):
    """Validate before stage I/O or source reads; reduced reservations need evidence.

    Reopen callers pass the loaded journal before mutating it. Sources pass their
    negotiated resume plan. Full reservations need neither; over-reservation is safe.
    """
    if not isinstance(acceptance, AcceptedPreflight):
        raise PreflightV2Error("preflight_required", "accepted preflight is required")
    try:
        manifest = manifest_v2.validate_manifest(manifest)
    except manifest_v2.ManifestValidationError as exc:
        raise PreflightV2Error("preflight_mismatch", "manifest is invalid") from exc
    acceptance.__post_init__()
    current = _timestamp(time.monotonic() if now is None else now)
    expected = (_transfer_id(transfer_id), STRATEGY_STREAM_V2, manifest["item_id"],
                manifest["item_revision"], manifest["manifest_digest"])
    actual = (acceptance.transfer_id, acceptance.strategy, acceptance.item_id,
              acceptance.item_revision, acceptance.manifest_digest)
    if actual != expected:
        raise PreflightV2Error("preflight_mismatch", "accepted preflight does not match transfer")
    if current >= acceptance.expires_at:
        raise PreflightV2Error("preflight_expired", "accepted preflight has expired")
    if current < acceptance.accepted_at:
        raise PreflightV2Error("preflight_mismatch", "preflight time precedes acceptance")
    estimate = acceptance.estimate
    _validate_estimate_manifest(estimate, manifest)
    journal = _incoming_journal(manifest, incoming_journal)
    if journal is not None:
        if (journal.transfer_id != acceptance.transfer_id
                or (estimate.journal_transfer_id is not None and
                    (journal.transfer_id, journal.generation, journal.journal_digest) !=
                    (estimate.journal_transfer_id, estimate.journal_generation,
                     estimate.journal_digest))):
            raise PreflightV2Error("preflight_mismatch", "resume journal is stale or unrelated")
    if resume_plan is not None:
        try:
            if isinstance(resume_plan, resume_v2.ResumePlan):
                resume_plan.__post_init__()
            resume_v2.validate_resume_plan(resume_plan, acceptance.transfer_id, manifest)
        except resume_v2.ResumeJournalError as exc:
            raise PreflightV2Error("preflight_mismatch", "resume plan is invalid") from exc
        if estimate.resume_plan is not None:
            prior = estimate.resume_plan
            if ((resume_plan.peer_id, resume_plan.profile_id, resume_plan.provider_id)
                    != (prior.peer_id, prior.profile_id, prior.provider_id)
                    or any(new.durable_offset < old.durable_offset or
                           (new.durable_offset == old.durable_offset and
                            (new.prefix_sha256, new.completed) !=
                            (old.prefix_sha256, old.completed))
                           for old, new in zip(prior.files, resume_plan.files))):
                raise PreflightV2Error("preflight_mismatch", "resume plan regresses reserved offsets")
        if journal is not None and any(
                (entry.durable_offset, entry.prefix_sha256, entry.completed) !=
                (journal.entries[entry.entry_index]["durable_offset"],
                 journal.entries[entry.entry_index]["prefix_sha256"],
                 journal.entries[entry.entry_index]["completed"])
                for entry in resume_plan.files):
            raise PreflightV2Error("preflight_mismatch", "resume plan differs from journal")
    durable = (_add(*(entry.durable_offset for entry in resume_plan.files))
               if resume_plan is not None else
               _add(*(entry["durable_offset"] for entry in journal.entries))
               if journal is not None else 0)
    if estimate.remaining_payload_bytes < manifest["total_size"] - durable:
        raise PreflightV2Error("preflight_mismatch", "resume plan exceeds payload reservation")
    return acceptance
