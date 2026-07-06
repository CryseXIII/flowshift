"""FlowShift clipboard foundation tests (pure logic + filesystem store).

Covers clipboard_model (kinds, hashing, manifest, sync diff, eviction,
formatting, zip strategy, chunk planning, disk guard, settings), clipboard_store
(per-profile history, dedup, persistence, FIFO/size eviction, pin, delete) and
clipboard_protocol (sync + chunked transfer messages, ChunkAssembler with
resume/retry/hash-mismatch). Runs on any OS (uses a temp dir).

Run: python src/python/test_clipboard.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_model as cm
import clipboard_protocol as cp
from clipboard_store import ClipboardStore, profile_dir_name

_failures = []


def check(cond, label):
    if cond:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


# ── model: hashing + items ──────────────────────────────────────────
check(cm.sha256_bytes(b"hello") == cm.sha256_bytes(b"hello"), "sha256 deterministic")
check(cm.sha256_bytes(b"a") != cm.sha256_bytes(b"b"), "sha256 distinguishes content")
t1 = cm.make_text_item("Hello\nWorld", seq=1)
check(t1["kind"] == cm.KIND_TEXT and t1["size"] == len(b"Hello\nWorld"), "text item size")
check(t1["display_name"] == "Hello", "text item display_name = first line")
check(t1["available"] is True and t1["preview_text"].startswith("Hello"), "text item preview")


# ── model: settings clamp ───────────────────────────────────────────
cs = cm.clipboard_settings({"clipboard": {"history_max_items": 5000, "byte_unit": "bogus"}})
check(cs["history_max_items"] == 999, "clipboard history_max_items clamped to 999")
check(cs["byte_unit"] == "auto", "bad byte_unit -> auto")
cs2 = cm.clipboard_settings({"clipboard": {"history_max_items": 1}})
check(cs2["history_max_items"] == 20, "clipboard history_max_items min 20")
check(cm.clipboard_settings({})["enabled"] is False, "clipboard disabled by default")


# ── model: manifest + sync diff ─────────────────────────────────────
items = [cm.make_text_item(f"item {i}", seq=i) for i in range(5)]
man = cm.build_manifest("prof", "devA", 5, items)
check(man["type"] == "clipboard_manifest" and len(man["items"]) == 5, "manifest built")
check("preview_text" in man["items"][0] and "pinned" not in man["items"][0],
      "manifest carries metadata, not internal-only fields")
parsed = cm.parse_manifest(man)
check(parsed and parsed["history_revision"] == 5, "manifest parses back")

# Target already has the first 3 by content (sha256); only 2 new -> request 2.
local_hashes = {items[0]["sha256"], items[1]["sha256"], items[2]["sha256"]}
diff = cm.diff_manifest(local_hashes, man["items"], auto_limit_bytes=100 * 1024 * 1024)
check(len(diff["to_request"]) == 2, "sync diff requests only the 2 new items")
check(diff["skipped_existing"] == 3, "sync diff skips 3 known items")
check(diff["order"] == [it["item_id"] for it in man["items"]], "sync diff preserves order")

# 3 new items among 200 existing -> only 3 requested.
existing = [cm.make_text_item(f"old {i}", seq=i) for i in range(200)]
known = {it["sha256"] for it in existing}
new_items = [cm.make_text_item(f"new {i}", seq=200 + i) for i in range(3)]
remote = cm.build_manifest("p", "d", 203, existing + new_items)
diff2 = cm.diff_manifest(known, remote["items"], 100 * 1024 * 1024)
check(diff2["to_request"] == [it["item_id"] for it in new_items],
      "3 new among 200 -> exactly 3 requested")
check(diff2["skipped_existing"] == 200, "200 known skipped")

# Large unknown item -> manual required, not auto-requested.
big = cm.make_binary_item("deadbeef" * 8, size=200 * 1024 * 1024, seq=999,
                          kind=cm.KIND_FILE, display_name="huge.bin", available=False)
man_big = cm.build_manifest("p", "d", 1, [big])
diff3 = cm.diff_manifest(set(), man_big["items"], auto_limit_bytes=100 * 1024 * 1024)
check(diff3["manual_required"] == [big["item_id"]] and diff3["to_request"] == [],
      "item > auto limit -> manual required")

sr = cm.build_sync_result(3, 197, 2, 0)
check(sr["received"] == 3 and sr["manual_required"] == 2, "sync result shape")


# ── model: eviction (FIFO + size + pinning) ─────────────────────────
ev_items = [dict(cm.make_text_item(f"x{i}", seq=i), size=100) for i in range(10)]
plan = cm.eviction_plan(ev_items, max_items=5, max_total_bytes=10 ** 9)
check(len(plan) == 5 and plan == [ev_items[i]["item_id"] for i in range(5)],
      "eviction drops the 5 oldest to satisfy max_items")
# Pin the oldest -> it is kept, next-oldest evicted instead.
ev_items[0]["pinned"] = True
plan2 = cm.eviction_plan(ev_items, max_items=5, max_total_bytes=10 ** 9)
check(ev_items[0]["item_id"] not in plan2, "pinned oldest item is not evicted")
# Size cap: each 100 bytes, cap 300 -> keep newest 3, evict 7.
plan3 = cm.eviction_plan([dict(it, pinned=False) for it in ev_items],
                         max_items=999, max_total_bytes=300)
check(len(plan3) == 7, "size cap evicts down to <= max_total_bytes")


# ── model: formatting ───────────────────────────────────────────────
check(cm.format_bytes(500) == "500 B", "format_bytes B")
check(cm.format_bytes(12_400_000) == "12.4 MB", "format_bytes auto MB")
check(cm.format_bytes(1536, "KiB") == "1.5 KiB", "format_bytes KiB")
check(cm.format_rate(820_000) == "820.0 KB/s", "format_rate auto KB/s")
check(cm.format_eta(43) == "00:43", "format_eta mm:ss")
check(cm.format_eta(3723) == "01:02:03", "format_eta hh:mm:ss")
check(cm.progress_percent(0, 0) == 100.0, "progress 0/0 -> 100%")
check(abs(cm.progress_percent(1, 4) - 25.0) < 1e-9, "progress 1/4 -> 25%")
prog = cm.format_progress(12_400_000, 48_000_000, 820_000, "MB", "KB/s")
check("12.4 MB / 48.0 MB" in prog and "25.8%" in prog and "820.0 KB/s" in prog and "ETA" in prog,
      "format_progress full line")


# ── model: zip strategy ─────────────────────────────────────────────
check(cm.zip_strategy(1, 5_000_000, 1.0, 8 * 10**9, 100 * 10**9) == "direct",
      "single file -> direct")
check(cm.zip_strategy(3000, 500_000_000, 0.0, 8 * 10**9, 100 * 10**9) == "multi",
      "3000 already-compressed jpg -> multi (no zip)")
check(cm.zip_strategy(500, 5_000_000, 1.0, 8 * 10**9, 100 * 10**9) == "zip_ram",
      "many small compressible -> zip_ram")
check(cm.zip_strategy(500, 2_000_000_000, 1.0, 1_000_000_000, 100 * 10**9) == "zip_disk",
      "large compressible, low ram -> zip_disk")
check(cm.zip_strategy(500, 2_000_000_000, 1.0, 100_000_000, 1_000_000) == "multi",
      "large but no disk for temp zip -> multi")
check(cm.is_compressible_ext("a.txt") is True and cm.is_compressible_ext("a.jpg") is False,
      "compressible ext detection")


# ── model: chunk planning + disk guard ──────────────────────────────
plan = cm.chunk_plan(2_500_000, 1_000_000)
check(len(plan) == 3 and plan[0]["offset"] == 0 and plan[-1]["length"] == 500_000,
      "chunk_plan splits with correct last length")
check(cm.chunk_count(2_500_000, 1_000_000) == 3, "chunk_count")
check(cm.chunk_plan(0, 1000) == [], "chunk_plan empty for 0 bytes")
check(cm.has_enough_space(100, 10 ** 9) is True, "disk guard enough space")
check(cm.has_enough_space(10 ** 9, 100) is False, "disk guard insufficient space")
check(cp.safe_chunk_size() > 0, "safe_chunk_size positive")


# ── protocol: sync messages ─────────────────────────────────────────
req = cp.build_request_items("prof", ["a", "b"], True, "manual_retry")
check(cp.parse_request_items(req)["item_ids"] == ["a", "b"], "request_items roundtrip")
start = cp.build_transfer_start("t1", "i1", "abc", 2_500_000, 1_000_000, kind=cm.KIND_FILE)
check(start["chunk_count"] == 3 and start["type"] == cp.T_START, "transfer_start chunk_count")


# ── protocol: chunked transfer + assembler ──────────────────────────
payload = bytes((i * 7) % 256 for i in range(2_500_000))
sha = cm.sha256_bytes(payload)
cc = cm.chunk_count(len(payload), 1_000_000)
asm = cp.ChunkAssembler(len(payload), cc, expected_sha=sha)
msgs = list(cp.iter_chunk_messages("t1", "i1", payload, chunk_size=1_000_000, hash_chunks=True))
check(len(msgs) == cc, "iter_chunk_messages count")
# Deliver out of order, skipping one to test resume, then completing.
for m in msgs[1:]:
    cp.decode_chunk_data(m)  # ensure decodable
    asm.add_chunk(m["chunk_index"], cp.decode_chunk_data(m), m.get("sha256"))
check(asm.is_complete() is False, "assembler incomplete with a gap")
check(asm.next_index == 0, "assembler resume points at missing index 0")
check(asm.missing_indices() == [0], "assembler reports missing chunk 0")
asm.add_chunk(0, cp.decode_chunk_data(msgs[0]), msgs[0].get("sha256"))
check(asm.is_complete() is True, "assembler complete after gap filled")
check(asm.assemble() == payload, "assembler reassembles original bytes")

# duplicate + hash mismatch detection
asm2 = cp.ChunkAssembler(len(payload), cc, expected_sha=sha)
asm2.add_chunk(0, cp.decode_chunk_data(msgs[0]), msgs[0].get("sha256"))
check(asm2.add_chunk(0, b"x", None) == "duplicate", "assembler detects duplicate")
check(asm2.add_chunk(1, b"tampered", cm.sha256_bytes(b"other")) == "hash_mismatch",
      "assembler detects hash mismatch")

ack = cp.build_transfer_ack("t1", 2)
check(ack["chunk_index"] == 2 and ack["status"] == "ok", "transfer_ack shape")
err = cp.build_transfer_error("t1", "i1", cp.ERR_DISK_FULL, "no space")
check(err["code"] == cp.ERR_DISK_FULL, "transfer_error shape")
res = cp.build_transfer_resume("t1", "i1", 5)
check(res["next_index"] == 5, "transfer_resume shape")


# ── store: per-profile persistence, dedup, delete, pin, eviction ────
tmp = tempfile.mkdtemp(prefix="fs_clip_")
check(profile_dir_name("device:879c6b39") == "device_879c6b39", "profile_dir_name sanitised")

store = ClipboardStore(tmp, "device_A")
it_a, _ = store.add_item(cm.make_text_item("alpha", seq=0), data=b"alpha")
it_b, _ = store.add_item(cm.make_text_item("bravo", seq=0), data=b"bravo")
check(len(store.list_items()) == 2, "store holds two items")
check(store.get_data(it_a["item_id"]) == b"alpha", "store returns blob data")
check(it_a["sha256"] in store.known_hashes(), "store known_hashes")

# Dedup: same content stored once (object reused), history entry still added.
it_a2, _ = store.add_item(cm.make_text_item("alpha", seq=0), data=b"alpha")
check(store.get_data(it_a2["item_id"]) == b"alpha", "dedup: second copy still readable")
# Deleting one copy keeps the shared object for the other.
store.delete_item(it_a2["item_id"])
check(store.get_data(it_a["item_id"]) == b"alpha", "shared object kept after deleting one copy")
# Deleting the last copy removes the object.
store.delete_item(it_a["item_id"])
check(store.has_object(it_a["sha256"]) is False, "object removed when last ref deleted")

# Pin + eviction.
store2 = ClipboardStore(tmp, "device_B")
ids = []
for i in range(10):
    it, _ = store2.add_item(dict(cm.make_text_item(f"n{i}", seq=0), size=100), data=f"n{i}".encode())
    ids.append(it["item_id"])
store2.set_pinned(ids[0], True)
evicted = store2.enforce_limits(max_items=5, max_total_bytes=10 ** 9)
remaining = {it["item_id"] for it in store2.list_items()}
check(ids[0] in remaining, "pinned item survives eviction")
check(len(store2.list_items()) <= 6, "eviction reduced to the cap (pinned kept)")

# Persistence across reopen.
rev = store2.revision
store3 = ClipboardStore(tmp, "device_B")
check(len(store3.list_items()) == len(store2.list_items()), "store persists across reopen")
check(store3.revision == rev, "revision persists")

# clear
store3.clear()
check(store3.list_items() == [], "clear empties the history")

# manifest from store
store4 = ClipboardStore(tmp, "device_C")
store4.add_item(cm.make_text_item("hi", seq=0), data=b"hi")
m = store4.build_manifest("devC")
check(m["type"] == "clipboard_manifest" and len(m["items"]) == 1, "store builds manifest")


# ── Summary ─────────────────────────────────────────────────────────
print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
    sys.exit(1)
print("All clipboard tests passed.")
