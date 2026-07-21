"""FlowShift clipboard streaming / disk-backed transfer tests."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_files as cf
import clipboard_model as cm
import clipboard_protocol as cpb
import clipboard_sources as csrc
import clipboard_transfer as ct
from clipboard_runtime import ClipboardManager
from clipboard_store import ClipboardStore

_failures = []


def check(cond, label):
    if cond:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


def write_file(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


tmp = tempfile.mkdtemp(prefix="fs_clip_stream_")
try:
    # ── TransferSource primitives ───────────────────────────────────
    bsrc = csrc.BytesTransferSource(b"abcdef", item_id="b1", display_name="bytes")
    bchunks = list(bsrc.iter_chunks(2))
    check([c["index"] for c in bchunks] == [0, 1, 2], "BytesTransferSource chunk indexes")
    check(b"".join(c["data"] for c in bchunks) == b"abcdef", "BytesTransferSource data roundtrip")
    check(bsrc.sha256 == cm.sha256_bytes(b"abcdef"), "BytesTransferSource sha256")
    check([c["index"] for c in bsrc.iter_chunks(2, start_index=1)] == [1, 2],
          "BytesTransferSource resume skips earlier chunks")

    src_path = os.path.join(tmp, "source.bin")
    write_file(src_path, "abcdefgh")
    fsrc = csrc.FileTransferSource(src_path, item_id="f1", display_name="file.bin")
    fchunks = list(fsrc.iter_chunks(3))
    check(b"".join(c["data"] for c in fchunks) == b"abcdefgh", "FileTransferSource data roundtrip")
    check(fsrc.sha256 == cm.sha256_bytes(b"abcdefgh"), "FileTransferSource sha256")
    check([c["index"] for c in fsrc.iter_chunks(3, start_index=1)] == [1, 2],
          "FileTransferSource resume skips earlier chunks")
    fsrc.cleanup()
    check(os.path.exists(src_path), "FileTransferSource cleanup does not delete source file")

    tmp_path = os.path.join(tmp, "temp.bin")
    write_file(tmp_path, "temp-data")
    tsrc = csrc.TempFileTransferSource(tmp_path, item_id="t1", display_name="temp.bin")
    tsrc.cleanup()
    check(not os.path.exists(tmp_path) and not os.path.exists(csrc.active_marker_path(tmp_path)),
          "TempFileTransferSource cleanup deletes temp file")

    # ── Deterministic ZIP bundle build ───────────────────────────────
    file_a = os.path.join(tmp, "zip", "a.txt")
    file_b = os.path.join(tmp, "zip", "sub", "b.txt")
    write_file(file_a, "alpha")
    write_file(file_b, "bravo")
    scan = cf.scan_paths([os.path.join(tmp, "zip")])
    out1 = os.path.join(tmp, "bundle1.zip")
    out2 = os.path.join(tmp, "bundle2.zip")
    res1 = cf.build_bundle_to_file(scan["files"], out1, compressible_ratio=scan["compressible_ratio"])
    res2 = cf.build_bundle_to_file(scan["files"], out2, compressible_ratio=scan["compressible_ratio"])
    check(res1["sha256"] == res2["sha256"] and res1["size"] == res2["size"],
          "build_bundle_to_file deterministic sha/size")
    check(open(out1, "rb").read() == open(out2, "rb").read(), "build_bundle_to_file deterministic bytes")
    unpack_dir = os.path.join(tmp, "unpack")
    copy_calls = []
    orig_copy = cf.shutil.copyfileobj

    def traced_copy(src, dst, length=0):
        copy_calls.append(length)
        return orig_copy(src, dst, length)

    cf.shutil.copyfileobj = traced_copy
    try:
        extracted = cf.unpack_bundle_file(out1, unpack_dir)
    finally:
        cf.shutil.copyfileobj = orig_copy
    check(len(extracted) == 2, "build_bundle_to_file unpacks")
    extracted_map = {os.path.relpath(p, unpack_dir).replace("\\", "/"): open(p, "r", encoding="utf-8").read()
                     for p in extracted}
    check(any(v == "alpha" for v in extracted_map.values()), "build_bundle_to_file preserves file content")
    check(copy_calls and all(call == cf.CHUNK_READ for call in copy_calls),
          "unpack_bundle_file streams via copyfileobj")

    bad_zip = os.path.join(tmp, "bad.zip")
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../evil.txt", "nope")
        zf.writestr("/abs.txt", "nope")
        zf.writestr("\\abs.txt", "nope")
        zf.writestr("C:/drive.txt", "nope")
        zf.writestr("C:\\drive.txt", "nope")
        zf.writestr("C:drive.txt", "nope")
        zf.writestr("//server/share/file.txt", "nope")
        zf.writestr("\\\\server\\share\\file.txt", "nope")
        zf.writestr("foo/../../evil.txt", "nope")
        zf.writestr("good/nested.txt", "good")
    safe_dest = os.path.join(tmp, "safe")
    safe_out = cf.unpack_bundle_file(bad_zip, safe_dest)
    safe_rels = sorted(os.path.relpath(p, safe_dest).replace("\\", "/") for p in safe_out)
    check(safe_rels == ["good/nested.txt"], "unpack_bundle_file blocks traversal + absolute paths")
    check(not os.path.exists(os.path.join(tmp, "evil.txt")), "unpack_bundle_file blocks path traversal")
    tree_rels = []
    for root, _dirs, files in os.walk(safe_dest):
        for name in files:
            tree_rels.append(os.path.relpath(os.path.join(root, name), safe_dest).replace("\\", "/"))
    check(not any(rel.startswith("C:") or rel.startswith("server/") for rel in tree_rels),
          "unpack_bundle_file blocks drive-prefixed dirs")
    check(not any(rel.startswith("server/") for rel in tree_rels), "unpack_bundle_file blocks UNC paths")

    small_item = cf.make_file_item([file_a])
    small_src = cf.build_bundle_source(small_item, os.path.join(tmp, "bundle-temp"), 1024 * 1024)
    check(isinstance(small_src, csrc.BytesTransferSource), "build_bundle_source uses RAM for small bundle")

    big_item = cf.make_file_item([file_a, file_b])
    big_src = cf.build_bundle_source(big_item, os.path.join(tmp, "bundle-temp"), 1)
    check(isinstance(big_src, csrc.TempFileTransferSource), "build_bundle_source uses temp file for large bundle")
    big_temp = big_src.path
    big_src.cleanup()
    check(not os.path.exists(big_temp), "TempFileTransferSource cleanup removes bundle temp file")

    settings = cm.clipboard_settings({"clipboard": {
        "enabled": True,
        "sync_on_activate": True,
        "history_max_items": 999,
        "history_max_total_gb": 10.0,
        "max_auto_transfer_mb": 100,
        "clipboard_disk_assembler_threshold_mb": 1,
        "clipboard_ram_zip_limit_mb": 1,
        "clipboard_temp_cleanup_max_age_hours": 24,
    }})

    # ── Materialize received file-batch from object path ─────────────
    mat_root = os.path.join(tmp, "mat")
    mat_mgr = ClipboardManager(mat_root, "dev", lambda ident, msg: None, lambda: settings)
    mat_store = mat_mgr.store("device:A")
    received_batch = cf.make_file_item([file_a, file_b])
    received_batch["files"] = [dict(f, abspath=os.path.join(tmp, "missing", f["rel"]))
                                 for f in received_batch.get("files", [])]
    bundle_zip = os.path.join(tmp, "received_bundle.zip")
    cf.build_bundle_to_file(scan["files"], bundle_zip, compressible_ratio=scan["compressible_ratio"])
    object_path = mat_store.write_object_from_file(received_batch["sha256"], bundle_zip, move=False)
    stored_batch, _ = mat_store.add_item(received_batch, data=None)
    check(stored_batch["available"], "received file batch stored as available when object exists")

    mat_store.get_data = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("get_data should not be used"))
    unpack_calls = []
    orig_unpack_file = cf.unpack_bundle_file

    def traced_unpack_file(zip_path, dest_dir):
        unpack_calls.append(zip_path)
        return orig_unpack_file(zip_path, dest_dir)

    cf.unpack_bundle_file = traced_unpack_file
    disk_calls = []
    old_check = ct.check_disk_space

    def traced_check(path, required_bytes, safety_margin_bytes=None):
        disk_calls.append((path, required_bytes))
        return {"ok": True, "path": path, "free_bytes": required_bytes + 1, "required_bytes": required_bytes,
                "margin_bytes": 0, "missing_bytes": 0}

    ct.check_disk_space = traced_check
    try:
        incoming_root = os.path.join(mat_root, "temp", "incoming")
        result = mat_mgr.materialize_files_result("device:A", stored_batch["item_id"], incoming_root)
    finally:
        cf.unpack_bundle_file = orig_unpack_file
        ct.check_disk_space = old_check

    check(result.get("ok") and len(result.get("paths") or []) == 2, "materialize_files_result returns two paths")
    check(unpack_calls == [object_path], "materialize_files_result uses object path")
    check(disk_calls and disk_calls[0][1] == stored_batch.get("total_file_size"),
          "materialize_files_result checks total_file_size")
    check(all(os.path.exists(p) for p in result.get("paths") or []), "materialized files exist on disk")
    check(all(os.path.exists(csrc.active_marker_path(p)) for p in result.get("paths") or []),
          "materialized files get active markers")

    fresh_extra = os.path.join(incoming_root, "fresh.txt")
    write_file(fresh_extra, "fresh")
    csrc.mark_active(fresh_extra)
    past = time.time() - 48 * 3600
    now = time.time()
    for p in result.get("paths") or []:
        os.utime(p, (past, past))
        os.utime(csrc.active_marker_path(p), (past, past))
    os.utime(fresh_extra, (now, now))
    os.utime(csrc.active_marker_path(fresh_extra), (now, now))
    mat_store.cleanup_temp(max_age_hours=24)
    check(all(not os.path.exists(p) for p in result.get("paths") or []), "cleanup removes old materialized files")
    check(os.path.exists(fresh_extra), "cleanup keeps fresh materialized file")

    disk_fail = []

    def failing_check(path, required_bytes, safety_margin_bytes=None):
        disk_fail.append(required_bytes)
        return {"ok": False, "path": path, "free_bytes": 0, "required_bytes": required_bytes,
                "margin_bytes": 0, "missing_bytes": required_bytes}

    ct.check_disk_space = failing_check
    try:
        disk_full = mat_mgr.materialize_files_result("device:A", stored_batch["item_id"], incoming_root)
    finally:
        ct.check_disk_space = old_check
    check(not disk_full.get("ok") and disk_full.get("error") == "Nicht genug Speicherplatz",
          "materialize_files_result blocks on disk full")
    check(disk_fail and disk_fail[0] == stored_batch.get("total_file_size"),
          "materialize_files_result uses total_file_size for disk check")

    os.remove(object_path)
    missing = mat_mgr.materialize_files_result("device:A", stored_batch["item_id"], incoming_root)
    check(not missing.get("ok") and "file data not present" in missing.get("error", ""),
          "materialize_files_result reports missing object")

    # ── ClipboardStore file-object APIs ──────────────────────────────
    store = ClipboardStore(os.path.join(tmp, "store"), "dev")
    obj_src = os.path.join(tmp, "object-src.bin")
    write_file(obj_src, "object-data")
    obj_sha = cm.sha256_bytes(b"object-data")
    obj_path = store.write_object_from_file(obj_sha, obj_src, move=False)
    check(os.path.exists(obj_path) and open(obj_path, "rb").read() == b"object-data",
          "write_object_from_file copies")
    check(os.path.exists(obj_src), "write_object_from_file copy keeps source file")

    move_src = os.path.join(tmp, "move-src.bin")
    write_file(move_src, "move-data")
    move_sha = cm.sha256_bytes(b"move-data")
    move_path = store.write_object_from_file(move_sha, move_src, move=True)
    check(os.path.exists(move_path) and open(move_path, "rb").read() == b"move-data",
          "write_object_from_file move stores object")
    check(not os.path.exists(move_src), "write_object_from_file move removes source file")
    check(store.object_path(obj_sha) == obj_path, "object_path returns store path")

    item = cm.make_binary_item(obj_sha, len(b"object-data"), seq=1,
                               kind=cm.KIND_BINARY, display_name="object.bin",
                               available=False)
    stored_item, _ = store.add_item(item, data=None)
    check(stored_item["available"], "add_item marks existing object available")
    check(store.get_object_path_for_item(stored_item["item_id"]) == obj_path,
          "get_object_path_for_item returns object path")

    old_temp = os.path.join(store.temp_dir, "old.part")
    new_temp = os.path.join(store.temp_dir, "new.part")
    active_temp = os.path.join(store.temp_dir, "active.part")
    write_file(old_temp, "old")
    write_file(new_temp, "new")
    write_file(active_temp, "active")
    past = time.time() - 48 * 3600
    now = time.time()
    os.utime(old_temp, (past, past))
    os.utime(new_temp, (now, now))
    csrc.mark_active(active_temp)
    store.cleanup_temp(max_age_hours=24)
    check(not os.path.exists(old_temp), "temp cleanup removes stale temp file")
    check(os.path.exists(new_temp), "temp cleanup keeps fresh temp file")
    check(os.path.exists(active_temp) and os.path.exists(csrc.active_marker_path(active_temp)),
          "temp cleanup keeps active temp file")

    # ── DiskChunkAssembler ──────────────────────────────────────────
    payload = b"abcdefgh"
    asm_path = os.path.join(tmp, "recv.part")
    asm = ct.DiskChunkAssembler(len(payload), 4, cm.sha256_bytes(payload), asm_path)
    check(asm.add_chunk(1, 2, b"cd", cm.sha256_bytes(b"cd")) == "ok", "DiskChunkAssembler add out of order")
    check(asm.add_chunk(0, 0, b"ab", cm.sha256_bytes(b"ab")) == "ok", "DiskChunkAssembler add first chunk")
    check(asm.add_chunk(0, 0, b"ab", cm.sha256_bytes(b"ab")) == "duplicate", "DiskChunkAssembler duplicate")
    check(asm.missing_indices() == [2, 3] and asm.next_index == 2, "DiskChunkAssembler missing chunks")
    check(asm.bytes_received == 4, "DiskChunkAssembler bytes_received")
    check(asm.add_chunk(2, 4, b"ef", "wrong") == "hash_mismatch", "DiskChunkAssembler hash mismatch")
    check(asm.add_chunk(2, 4, b"ef", cm.sha256_bytes(b"ef")) == "ok", "DiskChunkAssembler add middle chunk")
    check(asm.add_chunk(3, 6, b"gh", cm.sha256_bytes(b"gh")) == "ok", "DiskChunkAssembler add last chunk")
    final = asm.finalize()
    check(final["sha256"] == cm.sha256_bytes(payload) and final["size"] == len(payload),
          "DiskChunkAssembler finalize validates sha/size")
    check(open(final["path"], "rb").read() == payload, "DiskChunkAssembler file roundtrip")
    asm.cleanup()
    check(not os.path.exists(asm_path), "DiskChunkAssembler cleanup removes temp file")

    # ── Runtime send path uses streaming source ─────────────────────
    sent = []
    mgr = ClipboardManager(os.path.join(tmp, "runtime"), "dev", lambda ident, msg: sent.append((ident, msg)), lambda: settings)
    st = mgr.store("device:A")
    file_item = cm.make_binary_item("f" * 64, 6, seq=1, kind=cm.KIND_FILE,
                                    display_name="demo.bin", available=True)
    st.add_item(file_item, data=None)

    class FakeSource:
        def __init__(self):
            self.total_bytes = 6
            self.sha256 = cm.sha256_bytes(b"abcdef")
            self.display_name = "demo.bin"
            self.cleanup_called = False
            self.start_indices = []

        def iter_chunks(self, chunk_size, start_index=0):
            self.start_indices.append(start_index)
            chunks = [
                {"index": 0, "offset": 0, "data": b"ab", "sha256": cm.sha256_bytes(b"ab")},
                {"index": 1, "offset": 2, "data": b"cd", "sha256": cm.sha256_bytes(b"cd")},
                {"index": 2, "offset": 4, "data": b"ef", "sha256": cm.sha256_bytes(b"ef")},
            ]
            for c in chunks[start_index:]:
                yield c

        def cleanup(self):
            self.cleanup_called = True

    source = FakeSource()
    mgr._source_for_item = lambda identity, item: source
    job = ct.make_transfer_job("send1", "device:A", file_item["item_id"], "send",
                               file_item["kind"], file_item["display_name"], 0)
    mgr._send_transfer("device:A", file_item["item_id"], job)
    check(source.start_indices == [0], "_send_transfer starts streaming at chunk 0")
    check(source.cleanup_called, "_send_transfer cleans up on success")
    check(any(msg[1]["type"] == cpb.T_START for msg in sent), "_send_transfer sends transfer_start")
    check(any(msg[1]["type"] == cpb.T_COMPLETE for msg in sent), "_send_transfer sends transfer_complete")
    check(job.status == ct.TransferStatus.completed, "_send_transfer marks completed")

    sent.clear()
    source2 = FakeSource()
    mgr._source_for_item = lambda identity, item: source2
    job2 = ct.make_transfer_job("send2", "device:A", file_item["item_id"], "send",
                                file_item["kind"], file_item["display_name"], 0)
    mgr._send_transfer("device:A", file_item["item_id"], job2, resume_from=1, send_start=False)
    check(source2.start_indices == [1], "_send_transfer resumes from requested chunk")

    class FailingSource(FakeSource):
        def iter_chunks(self, chunk_size, start_index=0):
            self.start_indices.append(start_index)
            raise RuntimeError("boom")

    sent.clear()
    failing = FailingSource()
    mgr._source_for_item = lambda identity, item: failing
    job3 = ct.make_transfer_job("send3", "device:A", file_item["item_id"], "send",
                                file_item["kind"], file_item["display_name"], 0)
    mgr._send_transfer("device:A", file_item["item_id"], job3)
    check(failing.cleanup_called, "_send_transfer cleans up on error")
    check(any(msg[1]["type"] == cpb.T_ERROR for msg in sent), "_send_transfer sends transfer_error on failure")
    check(job3.status == ct.TransferStatus.failed, "_send_transfer marks failed on source error")

    # ── Runtime receive path uses disk assembler for large payloads ─
    recv_msgs = []
    recv_mgr = ClipboardManager(os.path.join(tmp, "recv-runtime"), "dev",
                                lambda ident, msg: recv_msgs.append((ident, msg)),
                                lambda: settings)
    recv_payload = b"abcdefgh" * 262145
    transfer_id = "rx1"
    recv_mgr._on_start("device:A", {
        "type": cpb.T_START,
        "transfer_id": transfer_id,
        "item_id": "rx-item",
        "sha256": cm.sha256_bytes(recv_payload),
        "total_size": len(recv_payload),
        "chunk_size": 2,
        "chunk_count": 2,
        "kind": cm.KIND_BINARY,
        "mime": "application/octet-stream",
        "file_count": 0,
        "display_name": "rx.bin",
    })
    asm_rt = recv_mgr._assemblers[transfer_id]["asm"]
    check(isinstance(asm_rt, ct.DiskChunkAssembler), "large receive uses DiskChunkAssembler")
    midpoint = len(recv_payload) // 2
    recv_chunks = [
        (0, 0, recv_payload[:midpoint]),
        (1, midpoint, recv_payload[midpoint:]),
    ]
    for idx, offset, piece in recv_chunks:
        recv_mgr._on_chunk("device:A", cpb.build_transfer_chunk(
            transfer_id, "rx-item", idx, offset, piece, cm.sha256_bytes(piece)))
    recv_mgr._on_complete("device:A", {
        "type": cpb.T_COMPLETE,
        "transfer_id": transfer_id,
        "item_id": "rx-item",
        "sha256": cm.sha256_bytes(recv_payload),
    })
    recv_store = recv_mgr.store("device:A")
    recv_items = recv_store.list_items()
    check(len(recv_items) == 1 and recv_items[0]["available"], "large receive stores available item")
    check(recv_store.get_data(recv_items[0]["item_id"]) == recv_payload, "large receive stores object bytes")

    # An inbound assembler exists before a placeholder/store item does. Progress
    # cannot see it, but safety activity must.
    pending_mgr = ClipboardManager(os.path.join(tmp, "pending-runtime"), "dev",
                                   lambda ident, msg: None, lambda: settings)
    pending_mgr._on_start("device:A", {
        "type": cpb.T_START,
        "transfer_id": "pending-rx",
        "item_id": "not-in-store",
        "sha256": cm.sha256_bytes(b"abcd"),
        "total_size": 4,
        "chunk_size": 2,
        "chunk_count": 2,
        "kind": cm.KIND_BINARY,
        "mime": "application/octet-stream",
        "file_count": 0,
        "display_name": "pending.bin",
    })
    check("not-in-store" not in pending_mgr.progress_snapshot(),
          "progress omits inbound assembler without store item")
    pending_activity = pending_mgr.activity_snapshot()
    check(pending_activity["active_assemblers"] == 1 and pending_activity["blocking"],
          "activity catches inbound assembler omitted by progress")
    check(pending_mgr.shutdown(timeout=1.0), "manager cleans incomplete assembler on shutdown")
    check(pending_mgr.activity_snapshot()["active_assemblers"] == 0,
          "shutdown removes incomplete assembler")

    # ── Runtime disk-full guard on start ─────────────────────────────
    old_check = ct.check_disk_space
    try:
        ct.check_disk_space = lambda path, required_bytes, safety_margin_bytes=None: {
            "ok": False, "path": path, "free_bytes": 0, "required_bytes": required_bytes,
            "margin_bytes": 0, "missing_bytes": required_bytes,
        }
        disk_msgs = []
        disk_mgr = ClipboardManager(os.path.join(tmp, "disk-full"), "dev",
                                    lambda ident, msg: disk_msgs.append((ident, msg)),
                                    lambda: settings)
        disk_mgr._on_start("device:A", {
            "type": cpb.T_START,
            "transfer_id": "disk1",
            "item_id": "disk-item",
            "sha256": cm.sha256_bytes(payload),
            "total_size": len(payload) * 1024 * 1024,
            "chunk_size": 2,
            "chunk_count": 4,
            "kind": cm.KIND_BINARY,
            "mime": "application/octet-stream",
            "file_count": 0,
            "display_name": "disk.bin",
        })
        check("disk1" not in disk_mgr._assemblers, "disk-full start does not create assembler")
        check(any(msg[1]["type"] == cpb.T_ERROR and msg[1]["code"] == cpb.ERR_DISK_FULL for msg in disk_msgs),
              "disk-full start sends transfer_error")
    finally:
        ct.check_disk_space = old_check

finally:
    try:
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass


print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
    sys.exit(1)
print("All clipboard streaming tests passed.")
