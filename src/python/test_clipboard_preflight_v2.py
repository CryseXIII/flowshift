"""Storage accounting and exact acceptance bindings, without payload I/O."""
import copy
import hashlib
import tempfile
import unittest
import uuid
from dataclasses import FrozenInstanceError, fields, replace
from unittest import mock

import clipboard_manifest_v2 as manifests
import clipboard_model as legacy
import clipboard_preflight_v2 as preflight
import clipboard_resume_v2 as resume


def manifest(size=100, *, count=1, revision=9, fingerprint=None):
    return manifests.build_manifest("item-1", revision, [{
        "path": f"file-{index:05d}", "type": "file", "size": size,
        "source_fingerprint": fingerprint or {"size": size},
        "hash_state": "unhashed", "sha256": None,
    } for index in range(count)])


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.manifest = manifest()
        self.transfer_id = uuid.uuid4().hex

    def estimate(self, **kwargs):
        kwargs.setdefault("free_bytes", 10 ** 12)
        return preflight.estimate_stream_v2(self.manifest, **kwargs)

    def accept(self, estimate=None, **kwargs):
        kwargs.setdefault("now", 10)
        kwargs.setdefault("expires_at", 20)
        return preflight.accept_preflight(
            self.transfer_id, self.manifest, estimate or self.estimate(), **kwargs)

    def validate(self, accepted, **kwargs):
        kwargs.setdefault("now", 15)
        return preflight.validate_acceptance(
            accepted, self.transfer_id, self.manifest, **kwargs)

    def journal(self, direction="incoming", **kwargs):
        # Real persisted metadata, not a mock of the resume validator.
        with tempfile.TemporaryDirectory() as root:
            return getattr(resume.ResumeJournalStore(root), f"create_{direction}")(
                transfer_id=self.transfer_id, peer_id="peer-1", profile_id="profile-1",
                provider_id="provider-1", manifest=self.manifest, created_ns=1, **kwargs)

    def progressed(self, journal, durable=40, verified=70):
        return resume.update_incoming_progress(
            journal, 0, durable_offset=durable, verified_offset=verified,
            prefix_sha256="a" * 64 if durable else None, updated_ns=2)

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(preflight.PreflightV2Error) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_exact_space_and_one_byte_short(self):
        estimate = self.estimate(index_overhead_bytes=1234, materialization_bytes=4567)
        needed = estimate.peak_required_bytes + estimate.safety_margin_bytes
        exact = self.estimate(free_bytes=needed, index_overhead_bytes=1234,
                              materialization_bytes=4567)
        self.assertTrue(exact.allowed)
        self.assertIsNone(exact.reason)
        self.assertEqual(exact.peak_required_bytes, 100 + exact.journal_overhead_bytes
                         + exact.manifest_overhead_bytes + 1234 + 4567)
        short = self.estimate(free_bytes=needed - 1, index_overhead_bytes=1234,
                              materialization_bytes=4567)
        self.assertFalse(short.allowed)
        self.assertEqual(short.reason, "disk_full")
        self.assert_code("preflight_rejected", self.accept, short)

    def test_limits_and_manual_override(self):
        self.assertEqual(self.estimate(hard_item_bytes=99).reason, "too_large")
        self.assertEqual(self.estimate(auto_limit_bytes=99).reason, "policy")
        self.assertTrue(self.estimate(auto_limit_bytes=99, allow_manual=True).allowed)
        self.assertTrue(self.estimate(hard_item_bytes=100, auto_limit_bytes=100).allowed)
        self.assertEqual(self.estimate(hard_item_bytes=99, allow_manual=True).reason,
                         "too_large")

    def test_all_numeric_options_strict_even_when_policy_short_circuits(self):
        for name in ("free_bytes", "index_overhead_bytes", "materialization_bytes",
                     "hard_item_bytes", "auto_limit_bytes"):
            for value in (True, False, -1, 1.0, "1", float("nan"), 1 << 64):
                with self.subTest(name=name, value=value):
                    self.assert_code("invalid_size_metadata", self.estimate,
                                     **{name: value})
        self.assert_code("invalid_size_metadata", self.estimate,
                         hard_item_bytes=0, auto_limit_bytes=True, allow_manual=True)
        self.assert_code("invalid_size_metadata", self.estimate, allow_manual=1)

    def test_uint64_overflow_in_peak_and_safety(self):
        self.assert_code("invalid_size_metadata", self.estimate,
                         index_overhead_bytes=preflight.UINT64_MAX)
        estimate = self.estimate()
        self.assert_code("invalid_size_metadata", self.estimate,
                         index_overhead_bytes=preflight.UINT64_MAX
                         - estimate.peak_required_bytes)
        self.manifest = manifest(manifests.HARD_MAX_LOGICAL_BYTES)
        estimate = self.estimate(free_bytes=preflight.UINT64_MAX)
        self.assertTrue(estimate.allowed)
        self.assertEqual(estimate.safety_margin_bytes, estimate.peak_required_bytes // 20)

    def test_malformed_manifest_and_revision_overflow(self):
        for name in ("total_size", "file_count", "item_revision"):
            for value in (True, 1 << 64, float("nan")):
                bad = copy.deepcopy(self.manifest)
                bad[name] = value
                self.assert_code("invalid_size_metadata", preflight.estimate_stream_v2,
                                 bad, free_bytes=10 ** 12)
        self.manifest = manifest(revision=preflight.UINT64_MAX)
        self.assert_code("invalid_size_metadata", self.estimate)

    def test_immutable_estimate_validates_every_integer_field(self):
        estimate = self.estimate()
        with self.assertRaises(FrozenInstanceError):
            estimate.logical_bytes = 0
        for field in fields(estimate):
            if type(getattr(estimate, field.name)) is int:
                for value in (True, -1, 1 << 64, float("nan")):
                    with self.subTest(field=field.name, value=value):
                        self.assert_code("invalid_size_metadata", replace, estimate,
                                         **{field.name: value})

    def test_inconsistent_estimate_accounting_and_decisions(self):
        estimate = self.estimate()
        for kwargs in ({"remaining_payload_bytes": 0}, {"logical_bytes": 1},
                       {"peak_required_bytes": 1}, {"safety_margin_bytes": 0},
                       {"free_bytes": 0}, {"allowed": 1}, {"reason": "disk_full"},
                       {"allowed": False}, {"allowed": False, "reason": "disk_full"},
                       {"journal_overhead_bytes": 0}, {"manifest_overhead_bytes": 0}):
            with self.subTest(kwargs=kwargs):
                self.assert_code("invalid_size_metadata", replace, estimate, **kwargs)
        self.assert_code("preflight_mismatch", replace, estimate, strategy="legacy_zip_v1")

    def test_accept_requires_exact_manifest_not_just_same_size_or_item(self):
        estimate = self.estimate()
        other = copy.deepcopy(self.manifest)
        other["entries"][0]["path"] = "different"
        other["manifest_digest"] = manifests.manifest_digest(other)
        for value in (other, manifest(101), manifest(revision=10)):
            self.assert_code("preflight_mismatch", preflight.accept_preflight,
                             self.transfer_id, value, estimate, now=10, expires_at=20)
        altered = replace(estimate, logical_bytes=99, remaining_payload_bytes=99,
                          peak_required_bytes=estimate.peak_required_bytes - 1)
        self.assert_code("preflight_mismatch", self.accept, altered)

    def test_metadata_underreservation_rejected_even_with_consistent_sum(self):
        self.manifest = manifest(count=30)
        estimate = self.estimate()
        altered = replace(estimate, journal_overhead_bytes=4096,
                          peak_required_bytes=estimate.peak_required_bytes
                          - estimate.journal_overhead_bytes + 4096)
        self.assert_code("invalid_size_metadata", self.accept, altered)

    def test_acceptance_immutable_and_bound_to_estimate(self):
        accepted = self.accept()
        with self.assertRaises(FrozenInstanceError):
            accepted.expires_at = 100
        for kwargs in ({"item_id": "other"}, {"item_revision": 10},
                       {"manifest_digest": "f" * 64}):
            self.assert_code("preflight_mismatch", replace, accepted, **kwargs)
        self.assert_code("invalid_size_metadata", replace, accepted, item_revision=True)

    def test_expiry_is_inclusive_and_future_acceptance_rejected(self):
        accepted = self.accept()
        self.assertIs(self.validate(accepted, now=10), accepted)
        self.assertIs(self.validate(accepted, now=19.999), accepted)
        self.assert_code("preflight_expired", self.validate, accepted, now=20)
        self.assert_code("preflight_expired", self.validate, accepted, now=21)
        self.assert_code("preflight_mismatch", self.validate, accepted, now=9)

    def test_all_time_inputs_reject_nonfinite_bool_string_and_huge_int(self):
        accepted = self.accept()
        for value in (True, False, "15", float("nan"), float("inf"),
                      -float("inf"), 10 ** 1000):
            with self.subTest(value=value):
                self.assert_code("preflight_mismatch", self.accept, now=value)
                self.assert_code("preflight_mismatch", self.accept, expires_at=value)
                self.assert_code("preflight_mismatch", self.validate, accepted, now=value)
                self.assert_code("preflight_mismatch", replace, accepted, accepted_at=value)
                self.assert_code("preflight_mismatch", replace, accepted, expires_at=value)
        self.assert_code("preflight_mismatch", self.accept, expires_at=10)

    def test_default_monotonic_time(self):
        with mock.patch.object(preflight.time, "monotonic", return_value=12):
            accepted = self.accept(now=None)
            self.assertEqual(accepted.accepted_at, 12)
            self.assertIs(self.validate(accepted, now=None), accepted)

    def test_validation_rejects_absent_expired_other_transfer_and_manifest(self):
        self.assert_code("preflight_required", self.validate, None)
        accepted = self.accept()
        self.assert_code("preflight_mismatch", preflight.validate_acceptance,
                         accepted, uuid.uuid4().hex, self.manifest, now=15)
        self.assert_code("preflight_mismatch", preflight.validate_acceptance,
                         accepted, self.transfer_id, manifest(101), now=15)
        for transfer in (True, "bad", "0" * 32):
            self.assert_code("preflight_mismatch", preflight.accept_preflight,
                             transfer, self.manifest, self.estimate(), now=10, expires_at=20)

    def test_resume_subtracts_only_durable_not_verified_bytes(self):
        journal = self.progressed(self.journal())
        estimate = self.estimate(incoming_journal=journal)
        self.assertEqual(estimate.durable_resume_bytes, 40)
        self.assertEqual(estimate.remaining_payload_bytes, 60)
        self.assertEqual(estimate.journal_transfer_id, self.transfer_id)
        self.assertEqual(estimate.journal_generation, journal.generation)
        self.assertEqual(estimate.journal_digest, journal.journal_digest)
        accepted = self.accept(estimate)
        self.assertIs(self.validate(accepted, incoming_journal=journal), accepted)
        self.assertIs(self.validate(accepted, resume_plan=estimate.resume_plan), accepted)
        self.assert_code("preflight_mismatch", self.validate, accepted)
        verified_only = self.estimate(incoming_journal=self.progressed(self.journal(), 0))
        self.assertEqual(verified_only.remaining_payload_bytes, 100)

    def test_resume_journal_other_transfer_and_stale_generation_or_digest(self):
        journal = self.progressed(self.journal())
        estimate = self.estimate(incoming_journal=journal)
        accepted = self.accept(estimate)
        self.assert_code("preflight_mismatch", preflight.accept_preflight,
                         uuid.uuid4().hex, self.manifest, estimate, now=10, expires_at=20)
        stale = resume.update_journal_state(journal, "paused", updated_ns=3)
        self.assert_code("preflight_mismatch", self.validate, accepted, incoming_journal=stale)
        changed = journal.to_dict()
        changed["updated_ns"] += 1
        changed["journal_digest"] = resume.journal_digest(changed)
        self.assert_code("preflight_mismatch", self.validate, accepted, incoming_journal=changed)
        changed["transfer_id"] = uuid.uuid4().hex
        changed["journal_digest"] = resume.journal_digest(changed)
        self.assert_code("preflight_mismatch", self.validate, self.accept(), incoming_journal=changed)

    def test_journal_entry_identity_and_direction_must_match(self):
        self.assert_code("preflight_mismatch", self.estimate,
                         incoming_journal=self.journal("outgoing"))
        original = self.manifest
        self.manifest = manifest(101)
        other = self.journal().to_dict()
        self.manifest = original
        other["manifest_digest"] = original["manifest_digest"]
        other["journal_digest"] = resume.journal_digest(other)
        self.assert_code("preflight_mismatch", self.estimate, incoming_journal=other)
        bad = self.journal().to_dict()
        bad["entries"][0]["durable_offset"] = True
        bad["journal_digest"] = resume.journal_digest(bad)
        self.assert_code("preflight_mismatch", self.estimate, incoming_journal=bad)

    def test_resume_plan_underreservation_other_transfer_and_hash_mismatch(self):
        journal = self.progressed(self.journal())
        estimate = self.estimate(incoming_journal=journal)
        accepted = self.accept(estimate)
        plan = estimate.resume_plan
        for changed in (replace(plan, transfer_id=uuid.uuid4().hex),
                        replace(plan, provider_id="different"),
                        replace(plan, files=(replace(plan.files[0], durable_offset=30),)),
                        replace(plan, files=(replace(plan.files[0], prefix_sha256="b" * 64),))):
            self.assert_code("preflight_mismatch", self.validate, accepted, resume_plan=changed)
        self.assert_code("preflight_mismatch", self.validate, accepted, resume_plan={})

    def test_resume_estimate_binding_fields_are_strict_and_immutable(self):
        estimate = self.estimate(incoming_journal=self.progressed(self.journal()))
        for value in (True, -1, 1 << 64):
            self.assert_code("invalid_size_metadata", replace, estimate,
                             journal_generation=value)
        for kwargs in ({"journal_digest": None}, {"journal_transfer_id": None},
                       {"resume_plan": None}, {"journal_digest": "invalid"}):
            self.assert_code("preflight_mismatch", replace, estimate, **kwargs)
        with self.assertRaises(FrozenInstanceError):
            estimate.resume_plan.files[0].durable_offset = 100

    def test_equal_total_resume_offsets_cannot_move_to_other_files(self):
        self.manifest = manifest(count=2)
        estimate = self.estimate(incoming_journal=self.progressed(self.journal()))
        plan = estimate.resume_plan
        changed = replace(plan, files=(
            replace(plan.files[0], durable_offset=0, prefix_sha256=None),
            replace(plan.files[1], durable_offset=40, prefix_sha256="a" * 64)))
        self.assertEqual(sum(entry.durable_offset for entry in changed.files), 40)
        self.assert_code("preflight_mismatch", self.validate, self.accept(estimate),
                         resume_plan=changed)

    def test_directory_only_manifest_and_journal_metadata(self):
        self.manifest = manifests.build_manifest("directories", 0, [{
            "path": f"directory-{index:05d}", "type": "directory", "size": 0,
            "source_fingerprint": {}, "hash_state": "unhashed", "sha256": None,
        } for index in range(50)])
        journal = self.journal()
        estimate = self.estimate(incoming_journal=journal)
        self.assertEqual(estimate.logical_bytes, 0)
        self.assertEqual(estimate.remaining_payload_bytes, 0)
        self.assertGreaterEqual(estimate.journal_overhead_bytes,
                                2 * len(resume.canonical_journal_bytes(journal)))
        self.assertGreaterEqual(estimate.manifest_overhead_bytes,
                                2 * len(manifests.canonical_manifest_bytes(self.manifest)))
        accepted = self.accept(estimate)
        self.assertIs(self.validate(accepted, incoming_journal=journal), accepted)

    def test_more_reservation_is_permitted_but_plan_must_match_supplied_journal(self):
        journal = self.progressed(self.journal())
        estimate = self.estimate(incoming_journal=journal)
        plan = estimate.resume_plan
        full = self.accept()
        self.assertIs(self.validate(full), full)
        self.assertIs(self.validate(full, resume_plan=plan), full)
        accepted = self.accept(estimate)
        advanced = replace(plan, files=(replace(plan.files[0], durable_offset=50,
                                               prefix_sha256="b" * 64),))
        self.assertIs(self.validate(accepted, resume_plan=advanced), accepted)
        self.assert_code("preflight_mismatch", self.validate, accepted,
                         incoming_journal=journal, resume_plan=advanced)

    def test_complete_and_empty_files_have_zero_remaining_payload(self):
        for size in (0, 100):
            self.manifest = manifest(size)
            digest = hashlib.sha256(b"").hexdigest() if not size else "f" * 64
            journal = resume.update_incoming_progress(
                self.journal(), 0, verified_offset=size, durable_offset=size,
                completed=True, prefix_sha256=digest, receiver_sha256=digest)
            estimate = self.estimate(incoming_journal=journal)
            self.assertEqual(estimate.remaining_payload_bytes, 0)
            self.assertEqual(estimate.durable_resume_bytes, size)
            accepted = self.accept(estimate)
            self.assertIs(self.validate(accepted, incoming_journal=journal), accepted)

    def test_metadata_bounds_actual_finalized_manifest_and_journal_with_atomic_overlap(self):
        self.manifest = manifest(count=100)
        journal = self.journal().to_dict()
        journal.update(generation=preflight.UINT64_MAX, retry_count=preflight.UINT64_MAX,
                       created_ns=preflight.UINT64_MAX, updated_ns=preflight.UINT64_MAX,
                       state="waiting_reconnect", peer_id='"' * 256,
                       profile_id='"' * 256, provider_id='"' * 256)
        for entry in journal["entries"]:
            entry.update(verified_offset=100, durable_offset=100, completed=True,
                         prefix_sha256="f" * 64, receiver_sha256="f" * 64,
                         storage_state="verified")
        journal["journal_digest"] = resume.journal_digest(journal)
        finalized = manifests.finalize_manifest(self.manifest, {i: "f" * 64 for i in range(100)})
        final_bytes = len(manifests.canonical_manifest_bytes(finalized))
        journal_bytes = len(resume.canonical_journal_bytes(journal))
        estimate = self.estimate(index_overhead_bytes=12345)
        self.assertGreater(final_bytes, len(manifests.canonical_manifest_bytes(self.manifest)))
        self.assertGreater(journal_bytes, len(manifests.canonical_manifest_bytes(self.manifest)))
        self.assertGreaterEqual(estimate.manifest_overhead_bytes, 2 * final_bytes)
        self.assertGreaterEqual(estimate.journal_overhead_bytes, 2 * journal_bytes)
        self.assertLess(estimate.journal_overhead_bytes, 2 * journal_bytes + 1000)
        self.assertEqual(estimate.index_overhead_bytes, 12345)

    def test_metadata_size_limits_and_stricter_journal_fingerprints(self):
        with mock.patch.object(resume, "MAX_JOURNAL_BYTES", 100):
            self.assert_code("invalid_size_metadata", self.estimate)
        finalized = manifests.finalize_manifest(self.manifest, {0: "f" * 64})
        limit = len(manifests.canonical_manifest_bytes(self.manifest))
        self.assertGreater(len(manifests.canonical_manifest_bytes(finalized)), limit)
        # Finalization uses the production manifest validator and its wire ceiling.
        finalize = manifests.finalize_manifest
        with mock.patch.object(preflight.manifest_v2, "finalize_manifest",
                               side_effect=lambda value, hashes: finalize(
                                   value, hashes, limits=manifests.ManifestLimits(
                                       max_manifest_bytes=limit))):
            self.assert_code("invalid_size_metadata", self.estimate)
        self.manifest = manifest(fingerprint={"value": "x" * 1025})
        self.assert_code("invalid_size_metadata", self.estimate)

    def test_preflight_performs_no_filesystem_or_payload_io(self):
        journal = self.progressed(self.journal())
        with mock.patch("builtins.open", side_effect=AssertionError("payload I/O")), \
                mock.patch("os.open", side_effect=AssertionError("filesystem I/O")), \
                mock.patch("os.stat", side_effect=AssertionError("filesystem I/O")):
            estimate = self.estimate(incoming_journal=journal)
            accepted = self.accept(estimate)
            self.assertIs(self.validate(accepted, incoming_journal=journal), accepted)
            self.assertEqual(estimate.remaining_payload_bytes, 60)

    def test_legacy_zip_preflight_stays_worst_case(self):
        logical = 10 * 1024 ** 3
        self.manifest = manifest(logical)
        direct = self.estimate()
        zipped = legacy.compute_transfer_preflight(
            logical, 10 ** 12, encoding="deterministic_zip", logical_size=logical,
            materialized_size=logical)
        self.assertEqual(zipped["required_download_bytes"], logical)
        self.assertEqual(zipped["required_temporary_bytes"], logical)
        self.assertEqual(zipped["required_materialized_bytes"], logical)
        self.assertEqual(zipped["peak_required_bytes"], 3 * logical)
        self.assertEqual(direct.remaining_payload_bytes, logical)
        self.assertEqual(direct.materialization_bytes, 0)
        self.assertLess(direct.peak_required_bytes, logical + 32 * 1024)
        free = direct.peak_required_bytes + direct.safety_margin_bytes
        self.assertTrue(self.estimate(free_bytes=free).allowed)
        self.assertEqual(legacy.compute_transfer_preflight(
            logical, free, encoding="deterministic_zip", logical_size=logical,
            materialized_size=logical)["reason"], "disk_full")


if __name__ == "__main__":
    unittest.main()
