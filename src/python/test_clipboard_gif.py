"""FlowShift animated GIF preview tests (pure + runtime helper).

Uses Pillow when available. If Pillow is missing, the test skips cleanly with a
clear message so the runtime still remains startable.
"""
from __future__ import annotations

import os
import io
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from PIL import Image, ImageSequence
except Exception:
    print("[SKIP] Pillow not installed; GIF preview tests skipped.")
    sys.exit(0)

import clipboard_files as cf
import clipboard_model as cm
import clipboard_preview as cpv
from clipboard_runtime import ClipboardManager

_failures = []


def check(cond, label):
    if cond:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


def make_gif_bytes():
    frames = [
        Image.new("RGB", (12, 8), (255, 0, 0)),
        Image.new("RGB", (12, 8), (0, 255, 0)),
        Image.new("RGB", (12, 8), (0, 0, 255)),
    ]
    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[10, 80, 120],
        loop=0,
        disposal=2,
    )
    return buf.getvalue()


gif_bytes = make_gif_bytes()
with Image.open(io.BytesIO(gif_bytes)) as im:
    check(len(list(ImageSequence.Iterator(im))) == 3, "Pillow ImageSequence available")

frames = cpv.gif_frames_to_ppm_frames(gif_bytes, max_px=6, max_frames=60)
check(len(frames) == 3, "gif helper returns 3 frames")
check(all(int(fr["duration_ms"]) >= 30 for fr in frames), "frame durations clamped to >= 30 ms")
check(frames[0]["ppm"].startswith(b"P6\n6 4\n255\n"), "thumbnail keeps aspect ratio at 6x4")

trunc = cpv.gif_preview_package(gif_bytes, max_px=6, max_frames=2)
check(len(trunc["frames"]) == 2, "max_frames limits the preview")
check(trunc["truncated"] is True, "max_frames reports truncation")
check(cpv.gif_frames_to_ppm_frames(b"not a gif", max_px=6) == [], "broken GIF returns empty frames")

tmp = tempfile.mkdtemp(prefix="fs_gif_")
try:
    gif_path = os.path.join(tmp, "demo.gif")
    with open(gif_path, "wb") as f:
        f.write(gif_bytes)

    file_item = cf.make_file_item([gif_path])
    check(file_item is not None and cm.is_gif_item(file_item), "file item with .gif is detected")
    check(file_item["mime"] == "image/gif", "single gif file gets image/gif mime")

    mgr = ClipboardManager(tmp, "device_GIF", lambda ident, msg: None, lambda: {"enabled": True})
    store = mgr.store("device:gif")
    stored_file, _ = store.add_item(file_item, data=None)
    pkg = mgr.preview_frames("device:gif", stored_file["item_id"], max_px=6, max_frames=2)
    check(pkg is not None and len(pkg["frames"]) == 2, "runtime preview_frames returns frames")
    check(pkg["truncated"] is True, "runtime preview_frames reports truncation")

    gif_item = dict(cm.make_binary_item(cm.sha256_bytes(gif_bytes), len(gif_bytes), seq=0,
                                        kind=cm.KIND_GIF, mime="image/gif",
                                        display_name="anim.gif"), available=True)
    store2 = mgr.store("device:gif-raw")
    stored_gif, _ = store2.add_item(gif_item, data=gif_bytes)
    pkg2 = mgr.preview_frames("device:gif-raw", stored_gif["item_id"], max_px=6, max_frames=60)
    check(pkg2 is not None and len(pkg2["frames"]) == 3, "runtime preview_frames handles gif-kind items")
finally:
    try:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass


print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
    sys.exit(1)
print("All clipboard GIF tests passed.")
