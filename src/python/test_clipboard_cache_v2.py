"""End-state tests for V2 cache eviction, provider state, GC and lease retirement."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest import mock

import clipboard_files as files
import clipboard_model as model
import clipboard_object_store_v2 as object_store_v2
import clipboard_preflight_v2 as preflight
import clipboard_resume_v2 as resume
import clipboard_store as stores
import clipboard_streaming_v2 as streaming
from clipboard_runtime import ClipboardManager


LOCAL = "local-device"
PEER = "peer-a"


def _settings(**overrides):
    raw = {"enabled": True}
    raw.update(overrides)
    return lambda: model.clipboard_settings({"clipboard": raw})


def publish_paths(store, root, paths, *, providers=None, make_current=True):
    """Transfer ``paths`` end-to-end via the staging path; returns the committed item.

    Uses ``IncomingTransferStage.publish`` so the stage directory and pending pins
    are gone afterwards and objects are referenced only by the store (nlink 1).
    """
    provisional = files.make_file_item(paths)
    local = provisional["files"]
    remote = copy.deepcopy(provisional)
    remote.pop("files")
    remote["source_available"] = False
    remote["providers"] = providers or [
        {"device_id": PEER, "state": "available", "last_seen_at": time.time()},
        {"device_id": LOCAL, "state": "available", "last_seen_at": time.time()},
    ]
    remote = model.version_item(remote, payload_state="receiving")
    provisional, _ = store.add_item(remote, make_current=make_current)
    identifier = uuid.uuid4().hex
    manifest = provisional["batch_manifest"]
    acceptance = preflight.accept_preflight(
        identifier, manifest, preflight.estimate_stream_v2(manifest, free_bytes=10 ** 12),
        now=0, expires_at=10 ** 12)
    source = streaming.SequentialFileStream(identifier, manifest, local,
                                            accepted_preflight=acceptance)
    try:
        stage = streaming.IncomingTransferStage.create(
            str(Path(root) / "incoming"), identifier, manifest,
            journal_store=resume.ResumeJournalStore(Path(root) / "journals"),
            peer_id=PEER, profile_id=store.profile_id, provider_id=f"device:{PEER}",
            accepted_preflight=acceptance)
        for chunk in source.iter_chunks():
            stage.accept(chunk)
        stage.finalize(source.completion())
        item, evicted = stage.publish(store, provisional)
    finally:
        source.close()
    assert evicted == []
    return item


def _object_hashes(item):
    return {entry["sha256"] for entry in item["batch_manifest"]["entries"]
            if entry["type"] == "file"}


def _receipts_for(store, item_id):
    document = json.loads(Path(store.index_path).read_text(encoding="utf-8"))
    return {key: value for key, value in document.get("v2_receipts", {}).items()
            if value.get("item_id") == item_id}


class V2CacheEvictionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flowshift-cache-v2-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime_root = self.root / "runtime"
        self.manager = ClipboardManager(str(self.runtime_root), LOCAL,
                                        lambda _identity, _msg: None, _settings())
        self.addCleanup(self.manager.shutdown)
        self.identity = f"device:{PEER}"
        self.store = self.manager.store(self.identity)
        self.objects = self.store.object_store_v2
        self.source = self.root / "source"
        self.source.mkdir()

    def write_source(self, name, payloads):
        base = self.source / name
        for rel, data in payloads.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return base

    def publish(self, name, payloads, **kwargs):
        base = self.write_source(name, payloads)
        return publish_paths(self.store, self.runtime_root, [base], **kwargs)

    def object_paths(self, item):
        return {sha: Path(self.objects.object_path(sha)) for sha in _object_hashes(item)}

    def manifest_path(self, item):
        return Path(self.objects.manifest_path(item["payload"]["sha256"]))

    def test_commit_records_cache_entry_under_content_identity(self):
        item = self.publish("a", {"one.bin": b"alpha" * 100, "two.bin": b"beta" * 50})
        entry = self.store.get_cache_entry(item["sha256"])
        self.assertIsNotNone(entry)
        self.assertEqual(entry["content_sha256"], item["sha256"])
        self.assertEqual(entry["payload_sha256"], item["batch_manifest"]["manifest_digest"])
        self.assertEqual(entry["payload_size"], item["batch_manifest"]["total_size"])
        self.assertEqual(entry["payload_size"], 500 + 200)
        self.assertEqual({p["device_id"] for p in entry["providers"]}, {PEER, LOCAL})
        self.assertEqual(self.store.cache_snapshot()["unique_bytes"], 700)
        # Protection keys on the same identity the cache entry uses.
        self.assertIn(item["sha256"], self.store.cache_protected_hashes())

    def test_evicting_v2_item_retires_state_provider_receipt_and_objects(self):
        item = self.publish("a", {"one.bin": b"alpha" * 100, "sub/two.bin": b"beta" * 50})
        objects = self.object_paths(item)
        manifest_path = self.manifest_path(item)
        for path in objects.values():
            self.assertEqual(os.stat(path).st_nlink, 1)
        self.assertTrue(manifest_path.exists())
        self.assertEqual(len(_receipts_for(self.store, item["item_id"])), 1)
        self.assertTrue(self.store.set_current(None))  # not protected as current

        evicted = self.store.evict_cache(target_unique_bytes=1, local_device_id=LOCAL)
        self.assertEqual(set(evicted), {item["sha256"]})

        after = self.store.get_item(item["item_id"])
        self.assertIsNotNone(after, "evicted item must remain listed")
        self.assertEqual(after["payload_state"], "missing")
        self.assertFalse(after["available"])
        self.assertEqual(after["batch_manifest"], item["batch_manifest"])
        self.assertEqual(after["payload"], item["payload"])
        self.assertEqual(after["sha256"], item["sha256"])
        providers = {p["device_id"]: p["state"] for p in after["providers"]}
        self.assertEqual(providers, {PEER: "available", LOCAL: "unavailable"})
        self.assertIsNone(self.store.get_cache_entry(item["sha256"]))
        self.assertEqual(_receipts_for(self.store, item["item_id"]), {})
        for path in objects.values():
            self.assertFalse(path.exists(), path)
        self.assertFalse(manifest_path.exists())
        self.assertIsNone(self.store.v2_manifest_for_item(item["item_id"]))
        self.assertNotIn(item["sha256"], self.store.known_hashes())
        # V2 items are never advertised to legacy peers, evicted or not.
        manifest = self.store.build_manifest(LOCAL)
        self.assertEqual([row for row in manifest["items"] if row["item_id"] == item["item_id"]], [])
        # Restart keeps the evicted state; nothing is resurrected.
        restarted = stores.ClipboardStore(str(self.runtime_root), self.store.profile_id)
        self.assertEqual(restarted.get_item(item["item_id"])["payload_state"], "missing")
        self.assertIsNone(restarted.get_cache_entry(item["sha256"]))

    def test_shared_object_survives_eviction_of_one_item(self):
        shared = b"shared bytes" * 200
        first = self.publish("a", {"shared.bin": shared, "only-a.bin": b"A" * 300})
        second = self.publish("b", {"shared.bin": shared, "only-b.bin": b"B" * 300})
        self.assertNotEqual(first["sha256"], second["sha256"])
        shared_sha = hashlib.sha256(shared).hexdigest()
        only_a = hashlib.sha256(b"A" * 300).hexdigest()
        self.assertEqual(os.stat(self.objects.object_path(shared_sha)).st_nlink, 1)

        self.assertTrue(self.store.set_current(None))
        evicted = self.store.evict_cache(protected_hashes={second["sha256"]},
                                         target_unique_bytes=1, local_device_id=LOCAL)
        self.assertEqual(set(evicted), {first["sha256"]})
        self.assertEqual(self.store.get_item(first["item_id"])["payload_state"], "missing")
        self.assertEqual(self.store.get_item(second["item_id"])["payload_state"], "cached")
        self.assertFalse(Path(self.objects.object_path(only_a)).exists())
        self.assertEqual(Path(self.objects.object_path(shared_sha)).read_bytes(), shared)
        self.assertFalse(self.manifest_path(first).exists())
        self.assertTrue(self.manifest_path(second).exists())
        self.assertIsNotNone(self.store.v2_manifest_for_item(second["item_id"]))
        self.assertEqual(len(_receipts_for(self.store, second["item_id"])), 1)

    def test_materialized_hardlinks_keep_objects_until_lease_release(self):
        item = self.publish("a", {"doc.txt": b"materialized" * 100})
        dest_root = self.root / "dest"
        dest_root.mkdir()
        result = self.manager.materialize_files_result(self.identity, item["item_id"], str(dest_root))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["strategy"], "hardlink")
        (sha, path), = self.object_paths(item).items()
        self.assertEqual(os.stat(path).st_nlink, 2)

        # Active lease protects the item from eviction in the runtime path.
        self.assertIn(item["sha256"], self.store.active_lease_hashes())
        self.assertTrue(self.store.set_current(None))
        self.assertEqual(self.manager._evict_cache_if_needed(self.identity, force=True), {})
        self.assertEqual(self.store.get_item(item["item_id"])["payload_state"], "cached")

        # Store-level eviction without protection retires the item, but the
        # materialization hardlink keeps the object bytes alive for the clipboard.
        evicted = self.store.evict_cache(target_unique_bytes=1, local_device_id=LOCAL)
        self.assertEqual(set(evicted), {item["sha256"]})
        self.assertEqual(self.store.get_item(item["item_id"])["payload_state"], "missing")
        self.assertTrue(path.exists())
        self.assertEqual(os.stat(path).st_nlink, 2)
        self.assertFalse(self.manifest_path(item).exists())
        materialized = Path(result["paths"][0]) / "doc.txt"
        self.assertEqual(materialized.read_bytes(), b"materialized" * 100)

        self.assertEqual(self.store.release_leases_for_item(item["item_id"]), [item["item_id"]])
        self.assertFalse(materialized.exists())
        # Lease release frees the object the hardlink kept alive (retired item).
        self.assertFalse(path.exists())
        self.assertEqual(self.objects.collect_garbage(), 0)

    def test_current_pinned_and_leased_items_are_not_evicted(self):
        current = self.publish("cur", {"c.bin": b"C" * 400})
        pinned = self.publish("pin", {"p.bin": b"P" * 400}, make_current=False)
        leased = self.publish("lease", {"l.bin": b"L" * 400}, make_current=False)
        plain = self.publish("plain", {"x.bin": b"X" * 400}, make_current=False)
        self.assertEqual(self.store.current_item_id, current["item_id"])
        self.assertTrue(self.store.set_pinned(pinned["item_id"], True))
        self.store.set_lease(leased["item_id"], str(self.root / "lease-dest"))

        under_budget = dict(_settings()(), cache_max_mb=1)
        with mock.patch.object(self.manager, "_settings", return_value=under_budget):
            # 1 MiB budget vs 1600 bytes: nothing over budget, so no eviction at all.
            self.assertEqual(self.manager._evict_cache_if_needed(self.identity), {})
        self.assertEqual({row["payload_state"] for row in self.store.list_items()}, {"cached"})
        # Zero budget (bypasses clamping): only the unprotected item may go.
        zero_budget = dict(_settings()(), cache_max_mb=0)
        with mock.patch.object(self.manager, "_settings", return_value=zero_budget):
            evicted = self.manager._evict_cache_if_needed(self.identity)
        self.assertEqual(set(evicted), {plain["sha256"]})
        states = {row["item_id"]: row["payload_state"] for row in self.store.list_items()}
        self.assertEqual(states[current["item_id"]], "cached")
        self.assertEqual(states[pinned["item_id"]], "cached")
        self.assertEqual(states[leased["item_id"]], "cached")
        self.assertEqual(states[plain["item_id"]], "missing")
        local_state = {row["item_id"]: {p["device_id"]: p["state"] for p in row["providers"]}
                       for row in self.store.list_items()}
        self.assertEqual(local_state[plain["item_id"]][LOCAL], "unavailable")
        self.assertEqual(local_state[current["item_id"]][LOCAL], "available")
        for item in (current, pinned, leased):
            for path in self.object_paths(item).values():
                self.assertTrue(path.exists(), path)
        for path in self.object_paths(plain).values():
            self.assertFalse(path.exists(), path)

    def test_global_enforce_uses_remove_cache_entry_lifecycle(self):
        old = self.publish("old", {"o.bin": b"O" * 600}, make_current=False)
        new = self.publish("new", {"n.bin": b"N" * 600})
        self.store.access_cache_entry(new["sha256"])
        # Bypass clamping: 6e-7 GB == 600 bytes, so exactly one item must go.
        limit = dict(_settings()(), cache_max_total_gb=6e-7)
        with mock.patch.object(self.manager, "_settings", return_value=limit):
            report = self.manager._global_cache_enforce()
        self.assertTrue(report["limit_satisfied"], report)
        self.assertEqual(report["freed_bytes"], 600)
        self.assertEqual(self.store.get_item(old["item_id"])["payload_state"], "missing")
        self.assertEqual(self.store.get_item(new["item_id"])["payload_state"], "cached")
        self.assertEqual({p["device_id"]: p["state"]
                          for p in self.store.get_item(old["item_id"])["providers"]}[LOCAL],
                         "unavailable")
        for path in self.object_paths(old).values():
            self.assertFalse(path.exists(), path)
        for path in self.object_paths(new).values():
            self.assertTrue(path.exists(), path)

    def test_gc_ignores_non_deliverable_v2_items_but_fails_closed_on_corrupt_manifest(self):
        item = self.publish("a", {"g.bin": b"G" * 100})
        self.assertEqual(self.objects.collect_garbage(), 0)
        document = json.loads(Path(self.store.index_path).read_text(encoding="utf-8"))
        row = next(r for r in document["items"] if r["item_id"] == item["item_id"])
        row["batch_manifest"]["entries"][0]["sha256"] = "0" * 64
        Path(self.store.index_path).write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(Exception):
            self.objects.collect_garbage()
        for path in self.object_paths(item).values():
            self.assertTrue(path.exists())


class LeaseRetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flowshift-lease-retire-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ClipboardManager(str(self.root / "runtime"), LOCAL,
                                        lambda _identity, _msg: None, _settings())
        self.addCleanup(self.manager.shutdown)
        self.store = self.manager.store("device:peer-a")

    def lease_tree(self, name):
        dest = self.root / "leases" / name
        (dest / "docs").mkdir(parents=True)
        (dest / "docs" / "file.txt").write_bytes(b"leased")
        return dest

    def test_newer_sequence_retires_bound_leases_and_age_cleanup_removes_them(self):
        old_dest = self.lease_tree("old")
        cur_dest = self.lease_tree("cur")
        unbound_dest = self.lease_tree("unbound")
        self.store.set_lease("item-old", str(old_dest))
        self.assertTrue(self.store.bind_lease_sequence("item-old", 5))
        self.store.set_lease("item-cur", str(cur_dest))
        self.assertTrue(self.store.bind_lease_sequence("item-cur", 6))
        self.store.set_lease("item-unbound", str(unbound_dest))

        retired = self.manager.retire_leases_for_sequence(6)
        self.assertEqual(retired, {"device:peer-a": ["item-old"]})
        old = self.store.get_lease("item-old")
        self.assertEqual(old["state"], model.LEASE_STALE)
        self.assertEqual(old["owner_sequence"], 5)
        self.assertTrue((old_dest / "docs" / "file.txt").exists(), "stale tree is retained")
        self.assertEqual(self.store.get_lease("item-cur")["state"], model.LEASE_ACTIVE)
        self.assertEqual(self.store.get_lease("item-unbound")["state"], model.LEASE_ACTIVE)
        # Default age (24h): retirement's own cleanup pass keeps the young stale lease.
        self.assertIsNotNone(self.store.get_lease("item-old"))

        removed = self.store.cleanup_leases(max_age_hours=0)
        self.assertEqual(removed, ["item-old"])
        self.assertIsNone(self.store.get_lease("item-old"))
        self.assertFalse((old_dest / "docs" / "file.txt").exists())
        # Active leases (bound to current or pending write) are never removed by age.
        self.assertEqual(self.store.get_lease("item-cur")["state"], model.LEASE_ACTIVE)
        self.assertTrue((cur_dest / "docs" / "file.txt").exists())
        self.assertEqual(self.store.get_lease("item-unbound")["state"], model.LEASE_ACTIVE)
        self.assertTrue((unbound_dest / "docs" / "file.txt").exists())

    def test_retirement_with_zero_age_removes_stale_tree_in_one_pass(self):
        dest = self.lease_tree("old")
        self.store.set_lease("item-old", str(dest))
        self.store.bind_lease_sequence("item-old", 5)
        cur = self.lease_tree("cur")
        self.store.set_lease("item-cur", str(cur))
        self.store.bind_lease_sequence("item-cur", 6)
        settings = dict(_settings()(), clipboard_temp_cleanup_max_age_hours=0)
        with mock.patch.object(self.manager, "_settings", return_value=settings):
            self.assertEqual(self.manager.retire_leases_for_sequence(6),
                             {"device:peer-a": ["item-old"]})
        self.assertIsNone(self.store.get_lease("item-old"))
        self.assertFalse((dest / "docs" / "file.txt").exists())
        self.assertEqual(self.store.get_lease("item-cur")["state"], model.LEASE_ACTIVE)
        self.assertTrue((cur / "docs" / "file.txt").exists())

    def test_windows_write_binds_and_retires_previous_sequence(self):
        item = model.make_text_item("hello", seq=1)
        self.store.add_item(item, data=b"hello")
        previous = self.lease_tree("previous")
        self.store.set_lease("item-previous", str(previous))
        self.store.bind_lease_sequence("item-previous", 41)
        self.store.set_lease(item["item_id"], str(self.lease_tree("now")))
        ok = self.manager.perform_windows_write(
            "device:peer-a", item["item_id"], {"text"}, "text", "digest",
            lambda: (True, 42), lambda: 42)
        self.assertTrue(ok)
        self.assertEqual(self.store.get_lease(item["item_id"])["owner_sequence"], 42)
        self.assertEqual(self.store.get_lease(item["item_id"])["state"], model.LEASE_ACTIVE)
        self.assertEqual(self.store.get_lease("item-previous")["state"], model.LEASE_STALE)
        self.assertTrue((previous / "docs" / "file.txt").exists())

    def test_invalid_sequence_is_ignored(self):
        self.store.set_lease("item-old", str(self.lease_tree("old")))
        self.store.bind_lease_sequence("item-old", 5)
        self.assertEqual(self.manager.retire_leases_for_sequence(None), {})
        self.assertEqual(self.manager.retire_leases_for_sequence("x"), {})
        self.assertEqual(self.store.get_lease("item-old")["state"], model.LEASE_ACTIVE)


class TempCleanupSettingsTests(unittest.TestCase):
    def test_store_open_uses_normalized_temp_cleanup_age(self):
        temp = tempfile.TemporaryDirectory(prefix="flowshift-cleanup-age-")
        self.addCleanup(temp.cleanup)
        calls = {}
        original_temp = stores.ClipboardStore.cleanup_temp
        original_leases = stores.ClipboardStore.cleanup_leases

        def record_temp(store_self, max_age_hours=None):
            calls["cleanup_temp"] = max_age_hours
            return original_temp(store_self, max_age_hours)

        def record_leases(store_self, max_age_hours=None):
            calls["cleanup_leases"] = max_age_hours
            return original_leases(store_self, max_age_hours)

        with mock.patch.object(stores.ClipboardStore, "cleanup_temp", record_temp), \
                mock.patch.object(stores.ClipboardStore, "cleanup_leases", record_leases):
            manager = ClipboardManager(str(Path(temp.name) / "runtime"), LOCAL,
                                       lambda _identity, _msg: None,
                                       _settings(clipboard_temp_cleanup_max_age_hours=5))
            self.addCleanup(manager.shutdown)
            manager.store("device:peer-a")
        self.assertEqual(calls, {"cleanup_temp": 5, "cleanup_leases": 5})

    def test_pre_normalized_settings_zero_is_honoured(self):
        temp = tempfile.TemporaryDirectory(prefix="flowshift-cleanup-age-")
        self.addCleanup(temp.cleanup)
        normalized = model.clipboard_settings({"clipboard": {"enabled": True}})
        normalized["clipboard_temp_cleanup_max_age_hours"] = 0
        with mock.patch.object(stores.ClipboardStore, "cleanup_temp") as cleanup_temp:
            manager = ClipboardManager(str(Path(temp.name) / "runtime"), LOCAL,
                                       lambda _identity, _msg: None, lambda: normalized)
            self.addCleanup(manager.shutdown)
            manager.store("device:peer-a")
        cleanup_temp.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
