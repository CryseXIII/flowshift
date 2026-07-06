"""FlowShift clipboard file/batch bundling tests (pure + filesystem, any OS)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_files as cf
import clipboard_model as cbm

_failures = []


def check(cond, label):
    if cond:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


tmp = tempfile.mkdtemp(prefix="fs_clipfiles_")
d = os.path.join(tmp, "src")
os.makedirs(os.path.join(d, "sub"), exist_ok=True)
p1 = os.path.join(d, "a.txt")
p2 = os.path.join(d, "b.txt")
p3 = os.path.join(d, "sub", "c.txt")
open(p1, "w").write("alpha")
open(p2, "w").write("bravo")
open(p3, "w").write("charlie")


# ── scan + hashing ──────────────────────────────────────────────────
scan = cf.scan_paths([p1, p2, p3])
check(scan["file_count"] == 3, "scan finds 3 files")
check(scan["total_size"] == len("alpha") + len("bravo") + len("charlie"), "scan total size")
rels = sorted(e["rel"] for e in scan["files"])
check(rels == ["a.txt", "b.txt", "sub/c.txt"], "scan rel paths (with common base)")
check(all(len(e["sha256"]) == 64 for e in scan["files"]), "scan hashes files")

# Directory drop is walked.
scan_dir = cf.scan_paths([d])
check(scan_dir["file_count"] == 3, "directory drop is walked recursively")


# ── content identity is stable + dedup-friendly ─────────────────────
csha1 = cf.content_sha(scan["files"])
csha2 = cf.content_sha(cf.scan_paths([p3, p1, p2])["files"])   # different order
check(csha1 == csha2, "content_sha independent of input order")
open(p1, "w").write("ALPHA-changed")
csha3 = cf.content_sha(cf.scan_paths([p1, p2, p3])["files"])
check(csha3 != csha1, "content_sha changes when a file changes")
open(p1, "w").write("alpha")  # restore


# ── deterministic zip build + unpack round-trip ─────────────────────
scan = cf.scan_paths([p1, p2, p3])
blob1 = cf.build_bundle_bytes(scan["files"], scan["compressible_ratio"])
blob2 = cf.build_bundle_bytes(scan["files"], scan["compressible_ratio"])
check(blob1 == blob2, "zip bundle is deterministic (same bytes -> dedup)")
check(cbm.sha256_bytes(blob1) == cbm.sha256_bytes(blob2), "zip blob sha stable")

dest = os.path.join(tmp, "out")
extracted = cf.unpack_bundle(blob1, dest)
check(len(extracted) == 3, "unpack extracts 3 files")
got = {}
for f in extracted:
    got[os.path.relpath(f, dest).replace("\\", "/")] = open(f).read()
check(got.get("a.txt") == "alpha" and got.get("sub/c.txt") == "charlie",
      "unpacked files have original content + structure")


# ── make_file_item (single vs batch) ────────────────────────────────
single = cf.make_file_item([p1])
check(single["kind"] == cbm.KIND_FILE and single["file_count"] == 1, "single file -> KIND_FILE")
check(single["display_name"] == "a.txt", "single file display_name = filename")
batch = cf.make_file_item([p1, p2, p3])
check(batch["kind"] == cbm.KIND_FILE_BATCH and batch["file_count"] == 3, "many -> KIND_FILE_BATCH")
check("Dateien" in batch["display_name"], "batch display_name mentions file count")
check(cf.local_source_paths(batch) and len(cf.local_source_paths(batch)) == 3,
      "local_source_paths returns the source files")

# lazy bundle from a captured item round-trips
blob = cf.bundle_for_item(batch)
check(blob is not None, "bundle_for_item builds a blob")
dest2 = os.path.join(tmp, "out2")
ex2 = cf.unpack_bundle(blob, dest2)
check(len(ex2) == 3, "bundle_for_item blob unpacks to 3 files")

# Same content set -> same content identity (dedup across copies).
batch2 = cf.make_file_item([p3, p2, p1])
check(batch2["sha256"] == batch["sha256"], "same file set -> same content identity (dedup)")


# ── path-traversal guard ────────────────────────────────────────────
import io as _io, zipfile as _zip
buf = _io.BytesIO()
with _zip.ZipFile(buf, "w") as zf:
    zf.writestr("../evil.txt", "nope")
safe_dest = os.path.join(tmp, "safe")
res = cf.unpack_bundle(buf.getvalue(), safe_dest)
check(not os.path.exists(os.path.join(tmp, "evil.txt")), "unpack blocks path traversal")


print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
    sys.exit(1)
print("All clipboard file tests passed.")
