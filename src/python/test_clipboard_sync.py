"""FlowShift clipboard sync integration test (two managers, in-memory routing).

Proves the Layer-2 sync pipeline without Windows APIs or sockets: two
ClipboardManagers exchange manifest / request / chunked transfer messages through
an in-memory router, so a peer pulls exactly the missing text items in order and
stores the real bytes (hash-verified). Also covers dedup (no re-transfer),
manual-required for oversize items, and delete/pin.

Run: python src/python/test_clipboard_sync.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_model as cbm
from clipboard_runtime import ClipboardManager

_failures = []


def check(cond, label):
    if cond:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


SETTINGS = cbm.clipboard_settings({"clipboard": {
    "enabled": True, "sync_on_activate": True,
    "history_max_items": 999, "history_max_total_gb": 10.0,
    "max_auto_transfer_mb": 100,
}})


def build_pair(tmp):
    """Two managers A and B wired so a message A sends to peer 'device:B' is
    delivered to B.handle('device:A', msg) and vice versa."""
    inbox = {"A": [], "B": []}

    def send_from_A(identity, msg):   # A -> its peer device:B
        inbox["B"].append(("device:A", msg))

    def send_from_B(identity, msg):   # B -> its peer device:A
        inbox["A"].append(("device:B", msg))

    A = ClipboardManager(os.path.join(tmp, "A"), "A", send_from_A, lambda: SETTINGS)
    B = ClipboardManager(os.path.join(tmp, "B"), "B", send_from_B, lambda: SETTINGS)

    def pump():
        # Deliver until both inboxes are empty (bounded).
        for _ in range(10000):
            if not inbox["A"] and not inbox["B"]:
                return
            if inbox["A"]:
                sender_ident, msg = inbox["A"].pop(0)
                A.handle(sender_ident, msg)
            elif inbox["B"]:
                sender_ident, msg = inbox["B"].pop(0)
                B.handle(sender_ident, msg)
    return A, B, pump


tmp = tempfile.mkdtemp(prefix="fs_clipsync_")

# ── A captures 3 texts, activates -> B pulls exactly 3, in order ────
A, B, pump = build_pair(tmp)
A.capture_text("device:B", "erste Zeile")
A.capture_text("device:B", "zweite Zeile")
A.capture_text("device:B", "dritte Zeile")
check(len(A.list_items("device:B")) == 3, "A stored 3 local text items")

A.on_profile_activated("device:B")   # A sends its manifest to B
pump()

b_items = B.list_items("device:A")
check(len(b_items) == 3, "B pulled exactly 3 items")
texts_b = [B.get_text("device:A", it["item_id"]) for it in b_items]
check(texts_b == ["erste Zeile", "zweite Zeile", "dritte Zeile"],
      "B received the 3 texts in source order with correct content")
check(all(it.get("available") for it in b_items), "B items marked available")
check(B.stats["received_items"] == 3 and A.stats["sent_items"] == 3,
      "transfer stats: A sent 3, B received 3")

# Progress telemetry: each received item reached 100% and is no longer active.
prog = B.progress_snapshot()
check(len(prog) == 3 and all(abs(p["percent"] - 100.0) < 1e-6 and not p["active"]
                             for p in prog.values()),
      "progress snapshot: all 3 items at 100% and inactive")


# ── Only-new sync: A adds 2 more, re-activate -> B pulls only 2 ─────
A.capture_text("device:B", "vierte Zeile")
A.capture_text("device:B", "fuenfte Zeile")
B.stats["received_items"] = 0
A.stats["sent_items"] = 0
A.on_profile_activated("device:B")
pump()
check(len(B.list_items("device:A")) == 5, "B now has 5 items")
check(B.stats["received_items"] == 2, "only the 2 NEW items were transferred (dedup)")
check(A.stats["sent_items"] == 2, "A sent only the 2 new items")


# ── Re-activate with nothing new -> zero transfers ──────────────────
B.stats["received_items"] = 0
A.stats["sent_items"] = 0
A.on_profile_activated("device:B")
pump()
check(B.stats["received_items"] == 0, "no re-transfer when nothing is new")


# ── Bidirectional: B captures, activates -> A pulls ─────────────────
B.capture_text("device:A", "von B kopiert")
B.on_profile_activated("device:A")
pump()
a_from_b = [A.get_text("device:B", it["item_id"]) for it in A.list_items("device:B")]
check("von B kopiert" in a_from_b, "A pulled B's captured text (bidirectional)")


# ── Delete + pin ────────────────────────────────────────────────────
first = B.list_items("device:A")[0]
B.set_pinned("device:A", first["item_id"], True)
check(B.list_items("device:A")[0]["pinned"] is True, "pin sets the flag")
n_before = len(B.list_items("device:A"))
B.delete_item("device:A", first["item_id"])
check(len(B.list_items("device:A")) == n_before - 1, "delete removes one item")


# ── Manual-required: oversize item is not auto-transferred ──────────
A2, B2, pump2 = build_pair(tempfile.mkdtemp(prefix="fs_clipsync2_"))
# Inject a huge fake item directly into A2's store metadata via a text that is
# reported oversize by settings: use a small auto limit for this pair.
huge_settings = cbm.clipboard_settings({"clipboard": {
    "enabled": True, "sync_on_activate": True, "max_auto_transfer_mb": 1}})
A2.settings_fn = lambda: huge_settings
B2.settings_fn = lambda: huge_settings
big_text = "x" * (2 * 1024 * 1024)   # 2 MB > 1 MB auto limit
A2.capture_text("device:B", big_text)
A2.on_profile_activated("device:B")
pump2()
b2 = B2.list_items("device:A")
check(len(b2) == 1 and b2[0]["available"] is False,
      "oversize item stored as placeholder (manual required), not auto-transferred")
# Manual retry pulls it.
B2.request_items("device:A", [b2[0]["item_id"]], reason="manual_retry")
pump2()
b2b = B2.list_items("device:A")
check(b2b[0]["available"] is True and B2.get_text("device:A", b2b[0]["item_id"]) == big_text,
      "manual retry transfers the oversize item and it verifies")


# ── File / batch transfer roundtrip (A captures files -> B pulls) ───
A3, B3, pump3 = build_pair(tempfile.mkdtemp(prefix="fs_clipsync3_"))
srcdir = tempfile.mkdtemp(prefix="fs_clipsrc_")
os.makedirs(os.path.join(srcdir, "sub"), exist_ok=True)
fpaths = []
for rel, content in [("one.txt", "eins"), ("two.txt", "zwei"), ("sub/three.txt", "drei")]:
    fp = os.path.join(srcdir, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    open(fp, "w").write(content)
    fpaths.append(fp)

captured = A3.capture_files("device:B", fpaths)
check(captured is not None and captured["kind"] == cbm.KIND_FILE_BATCH,
      "A captured a file batch")
A3.on_profile_activated("device:B")
pump3()
b3 = B3.list_items("device:A")
check(len(b3) == 1 and b3[0]["available"] is True and b3[0]["file_count"] == 3,
      "B pulled the file batch (available, 3 files)")

# B materialises the received files (unpacks the bundle) and content matches.
dest_root = tempfile.mkdtemp(prefix="fs_clipdest_")
paths = B3.materialize_files("device:A", b3[0]["item_id"], dest_root)
check(paths is not None and len(paths) == 3, "B materialised 3 files from the bundle")
contents = {}
for p in paths:
    contents[os.path.basename(p)] = open(p).read()
check(contents.get("one.txt") == "eins" and contents.get("three.txt") == "drei",
      "materialised files have original content")

# Same file set captured again -> dedup, no new transfer.
B3.stats["received_items"] = 0
A3.stats["sent_items"] = 0
A3.capture_files("device:B", fpaths)   # dedup: newest already same content id
A3.on_profile_activated("device:B")
pump3()
check(B3.stats["received_items"] == 0, "re-capturing the same files transfers nothing (dedup)")

# Locally-captured item pastes original paths without a copy.
local_paths = A3.materialize_files("device:B", captured["item_id"], dest_root)
check(local_paths is not None and len(local_paths) == 3 and all(os.path.exists(p) for p in local_paths),
      "local file item returns original source paths (no copy)")


# ── Summary ─────────────────────────────────────────────────────────
print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
    sys.exit(1)
print("All clipboard sync tests passed.")
