"""Tests for clipboard file-card previews: generation, cache and HTTP routes.

Runs without Windows and without Pillow; Pillow-only paths are skipped when
the library is missing.
"""
from __future__ import annotations

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_file_preview as cfp
import clipboard_model as cbm
import web_api
from test_web_api_clipboard import FakeManager


# ── tiny hand-built media files ──────────────────────────────────────
def _gif(frames):
    """1x1 GIF with ``frames`` image descriptors (valid LZW data)."""
    data = b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0x80, 0, 0) + b"\x00\x00\x00\xff\xff\xff"
    for _ in range(frames):
        data += b"\x21\xf9\x04\x00\x0a\x00\x00\x00"           # graphic control, 100 ms
        data += b"\x2c" + struct.pack("<HHHHB", 0, 0, 1, 1, 0)
        data += b"\x02\x02\x44\x01\x00"
    return data + b"\x3b"


def _png(width=2, height=2, animated=False):
    png = cfp.encode_png_rgba(width, height, b"\x10\x20\x30\xff" * (width * height))
    if not animated:
        return png
    body = b"acTL" + struct.pack(">II", 2, 0)
    actl = struct.pack(">I", 8) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    ihdr_end = 8 + 25
    return png[:ihdr_end] + actl + png[ihdr_end:]


def _webp_vp8x(animated):
    flags = 0x02 if animated else 0
    chunk = b"VP8X" + struct.pack("<I", 10) + bytes([flags, 0, 0, 0]) + b"\x00" * 6
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


class FakeShell:
    """Stands in for the Windows shell: records what was asked."""

    def __init__(self, fail_thumbnail=False):
        self.calls = []
        self.fail_thumbnail = fail_thumbnail

    def thumbnail_png(self, path, px, icon_only=False):
        self.calls.append(("thumb", os.path.basename(path), px, icon_only))
        if self.fail_thumbnail:
            raise cfp.PreviewError("no handler")
        return b"PNG-" + (b"icon" if icon_only else b"thumb")

    def type_icon_png(self, ext, px=cfp.ICON_PX, directory=False):
        self.calls.append(("type", ext))
        return b"PNG-type" + ext.encode()


def _file_item(item_id, paths, root):
    """Local file item as clipboard_files builds it (private abspaths)."""
    files = []
    entries = []
    for rel in paths:
        abspath = os.path.join(root, *rel.split("/"))
        st = os.stat(abspath)
        files.append({"abspath": abspath, "rel": rel, "type": "file", "size": st.st_size,
                      "mtime_ns": st.st_mtime_ns})
        entries.append({"path": rel, "type": "file", "size": st.st_size, "mtime_ns": st.st_mtime_ns})
    return {
        "item_id": item_id, "kind": "file" if len(paths) == 1 else "file_batch",
        "mime": "application/zip", "size": sum(f["size"] for f in files),
        "created_at": 1700000000.0, "seq": 1, "display_name": paths[0] if paths else "", "preview_text": "",
        "pinned": False, "available": True, "files": files, "base": root,
        "source_paths": [f["abspath"] for f in files],
        "batch_manifest": {"file_count": len(files), "directory_count": 0,
                           "total_size": sum(f["size"] for f in files), "entries": entries},
    }


class SniffTests(unittest.TestCase):
    def test_classify_by_extension(self):
        self.assertEqual(cfp.classify("Cat.GIF"), cfp.KIND_IMAGE)
        self.assertEqual(cfp.classify("dir/clip.mp4"), cfp.KIND_VIDEO)
        self.assertEqual(cfp.classify("notes.txt"), cfp.KIND_ICON)
        self.assertEqual(cfp.classify("noext"), cfp.KIND_ICON)

    def test_formats(self):
        self.assertEqual(cfp.sniff_format(_gif(1)), "gif")
        self.assertEqual(cfp.sniff_format(_png()), "png")
        self.assertEqual(cfp.sniff_format(_webp_vp8x(False)), "webp")
        self.assertEqual(cfp.sniff_format(b"\xff\xd8\xff\xe0"), "jpeg")
        self.assertEqual(cfp.sniff_format(b"hello"), "")

    def test_animation_detection(self):
        self.assertEqual(cfp._gif_frame_count(_gif(1)), 1)
        self.assertEqual(cfp._gif_frame_count(_gif(3)), 2)   # stops at 2
        self.assertTrue(cfp.detect_animation("-", "gif", _gif(2)))
        self.assertFalse(cfp.detect_animation("-", "gif", _gif(1)))
        self.assertTrue(cfp.detect_animation("-", "png", _png(animated=True)))
        self.assertFalse(cfp.detect_animation("-", "png", _png()))
        self.assertTrue(cfp.detect_animation("-", "webp", _webp_vp8x(True)))
        self.assertFalse(cfp.detect_animation("-", "webp", _webp_vp8x(False)))

    def test_png_encoder_roundtrip(self):
        png = cfp.encode_png_rgba(2, 1, b"\x01\x02\x03\x04\x05\x06\x07\x08")
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = struct.unpack(">II", png[16:24])
        self.assertEqual((width, height), (2, 1))
        idat_len = struct.unpack(">I", png[33:37])[0]
        raw = zlib.decompress(png[41:41 + idat_len])
        self.assertEqual(raw, b"\x00\x01\x02\x03\x04\x05\x06\x07\x08")
        with self.assertRaises(cfp.PreviewError):
            cfp.encode_png_rgba(2, 2, b"\x00" * 4)

    def test_gdi_pixel_conversion(self):
        # BGRA without alpha (old icons, video frames) becomes opaque RGBA.
        self.assertEqual(cfp.bgra_to_rgba(b"\x01\x02\x03\x00"), b"\x03\x02\x01\xff")
        # Premultiplied half-transparent blue: 128/128 -> 255.
        self.assertEqual(cfp.bgra_to_rgba(b"\x80\x00\x00\x80", premultiplied=True),
                         b"\x00\x00\xff\x80")
        # Opaque red pixel and a fully transparent one.
        black = b"\x00\x00\xff\x00" + b"\x00\x00\x00\x00"
        white = b"\x00\x00\xff\x00" + b"\xff\xff\xff\x00"
        self.assertEqual(cfp.icon_rgba_from_backgrounds(black, white),
                         b"\xff\x00\x00\xff" + b"\x00\x00\x00\x00")


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fs-preview-src-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, name, data):
        path = os.path.join(self.root, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    @unittest.skipIf(cfp.Image is not None, "pass-through applies without Pillow")
    def test_without_pillow_small_images_pass_through(self):
        png = _png()
        path = self.write("a.png", png)
        result = cfp.generate(path, len(png), shell=None)
        self.assertEqual(result["kind"], cfp.KIND_IMAGE)
        self.assertEqual(result["variants"]["still"], (png, "image/png"))
        gif = _gif(2)
        path = self.write("b.gif", gif)
        result = cfp.generate(path, len(gif), shell=None)
        self.assertEqual(result["kind"], cfp.KIND_ANIMATED)
        self.assertEqual(result["variants"]["anim"], (gif, "image/gif"))

    @unittest.skipIf(cfp.Image is None, "Pillow not installed")
    def test_pillow_scales_and_animates(self):
        from PIL import Image
        frames = [Image.new("RGB", (300, 150), color) for color in ("red", "blue", "green")]
        path = os.path.join(self.root, "moving.gif")
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=80, loop=0)
        result = cfp.generate(path, os.path.getsize(path), max_px=96, shell=None)
        self.assertEqual(result["kind"], cfp.KIND_ANIMATED)
        self.assertEqual(result["frames"], 3)
        self.assertEqual((result["width"], result["height"]), (96, 48))
        anim, mime = result["variants"]["anim"]
        self.assertIn(mime, ("image/webp", "image/gif"))
        with Image.open(__import__("io").BytesIO(anim)) as decoded:
            self.assertEqual(getattr(decoded, "n_frames", 1), 3)
            self.assertLessEqual(max(decoded.size), 96)
        still_path = os.path.join(self.root, "photo.jpg")
        Image.new("RGB", (800, 600), "white").save(still_path, quality=80)
        result = cfp.generate(still_path, os.path.getsize(still_path), max_px=96, shell=None)
        self.assertEqual(result["kind"], cfp.KIND_IMAGE)
        self.assertNotIn("anim", result["variants"])
        self.assertEqual((result["width"], result["height"]), (96, 72))

    def test_video_uses_shell_still_and_browser_clip(self):
        path = self.write("clip.mp4", b"\x00" * 64)
        shell = FakeShell()
        result = cfp.generate(path, 64, shell=shell)
        self.assertEqual(result["kind"], cfp.KIND_VIDEO)
        self.assertEqual(result["variants"]["still"], (b"PNG-thumb", "image/png"))
        self.assertEqual(result["video_mime"], "video/mp4")
        self.assertEqual(shell.calls[0], ("thumb", "clip.mp4", cfp.DEFAULT_MAX_PX, False))

    def test_video_without_thumbnail_handler_shows_type_icon(self):
        path = self.write("clip.mkv", b"\x00" * 64)
        result = cfp.generate(path, 64, shell=FakeShell(fail_thumbnail=True))
        self.assertEqual(result["kind"], cfp.KIND_ICON)
        self.assertEqual(result["variants"]["still"], (b"PNG-type.mkv", "image/png"))

    def test_other_files_get_their_icon(self):
        path = self.write("notes.txt", b"hello")
        shell = FakeShell()
        result = cfp.generate(path, 5, shell=shell)
        self.assertEqual(result["kind"], cfp.KIND_ICON)
        self.assertEqual(result["variants"]["still"], (b"PNG-icon", "image/png"))
        with self.assertRaises(cfp.PreviewError):
            cfp.generate(path, 5, shell=None)

    def test_huge_files_are_not_read_completely(self):
        reads = []
        real = cfp._read_head

        def recording(path, limit):
            reads.append(limit)
            return real(path, limit)

        big = cfp.MAX_IMAGE_SOURCE_BYTES + 1
        path = self.write("huge.png", _png())
        with mock.patch.object(cfp, "_read_head", recording):
            result = cfp.generate(path, big, shell=FakeShell())
        self.assertEqual(result["kind"], cfp.KIND_ICON)
        self.assertEqual(reads, [1])
        reads.clear()
        gif = _gif(2) + b"\x00" * 10
        path = self.write("anim.gif", gif)
        with mock.patch.object(cfp, "_read_head", recording), \
                mock.patch.object(cfp, "Image", None):
            cfp.generate(path, cfp.MAX_PASSTHROUGH_BYTES + 1, shell=FakeShell())
        self.assertTrue(reads)
        self.assertLessEqual(max(reads), cfp.GIF_SCAN_BYTES)

    def test_locked_file_raises_oserror(self):
        path = self.write("locked.png", _png())
        with mock.patch("builtins.open", side_effect=PermissionError("locked")):
            with self.assertRaises(OSError):
                cfp.generate(path, 10, shell=None)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fs-preview-cache-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.now = [1_000_000.0]
        self.cache = cfp.PreviewCache(self.root, max_bytes=10_000, clock=lambda: self.now[0])

    def key(self, n):
        return cfp.cache_key(f"C:/f{n}.png", 10, 1)

    def test_put_get_and_variant_path(self):
        key = self.key(1)
        meta = self.cache.put(key, {"kind": "image"}, {"still": (b"abc", "image/png")})
        self.assertEqual(meta["variants"]["still"], {"mime": "image/png", "size": 3})
        self.assertEqual(self.cache.get(key)["kind"], "image")
        path = self.cache.variant_path(key, "still")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"abc")
        self.assertIsNone(self.cache.variant_path(key, "anim"))
        self.assertIsNone(self.cache.variant_path(key, "json"))
        with self.assertRaises(ValueError):
            self.cache.get("../../etc/passwd")

    def test_key_changes_with_file_version(self):
        self.assertNotEqual(cfp.cache_key("C:/a.png", 10, 1), cfp.cache_key("C:/a.png", 10, 2))
        self.assertNotEqual(cfp.cache_key("C:/a.png", 10, 1), cfp.cache_key("C:/a.png", 11, 1))
        self.assertEqual(len(cfp.cache_key("C:/a.png", 10, 1)), 64)

    def test_size_limit_removes_oldest_down_to_target(self):
        for n in range(12):
            key = self.key(n)
            self.cache.put(key, {"kind": "image"}, {"still": (b"x" * 900, "image/png")})
            json_path = os.path.join(self.root, f"{key}.json")
            os.utime(json_path, (self.now[0] + n, self.now[0] + n))
        self.cache.enforce()
        self.assertLessEqual(self.cache.total_bytes(), 10_000)
        self.assertLessEqual(self.cache.total_bytes(), 10_000 * cfp.CACHE_CLEAN_TARGET + 1000)
        self.assertIsNone(self.cache.get(self.key(0)))          # oldest gone
        self.assertIsNotNone(self.cache.get(self.key(11)))      # newest kept
        on_disk = sum(os.path.getsize(os.path.join(self.root, n)) for n in os.listdir(self.root))
        self.assertEqual(on_disk, self.cache.total_bytes())

    def test_expired_and_leftover_files_are_removed(self):
        old, fresh = self.key(1), self.key(2)
        self.cache.put(old, {"kind": "image"}, {"still": (b"a", "image/png")})
        self.cache.put(fresh, {"kind": "image"}, {"still": (b"b", "image/png")})
        stale = self.now[0] - cfp.CACHE_MAX_AGE_S - 10
        os.utime(os.path.join(self.root, f"{old}.json"), (stale, stale))
        leftover = os.path.join(self.root, f"{fresh}.still.tmp-1-2")
        with open(leftover, "wb") as handle:
            handle.write(b"partial")
        os.utime(leftover, (stale, stale))
        self.cache.enforce()
        self.assertIsNone(self.cache.get(old))
        self.assertFalse(os.path.exists(os.path.join(self.root, f"{old}.still")))
        self.assertIsNotNone(self.cache.get(fresh))
        self.assertFalse(os.path.exists(leftover))

    def test_lowering_the_limit_cleans_up(self):
        for n in range(5):
            self.cache.put(self.key(n), {"kind": "image"}, {"still": (b"x" * 1500, "image/png")})
        self.cache.set_limit(2_000)
        self.assertLessEqual(self.cache.total_bytes(), 2_000)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="fs-preview-files-")
        self.cache_root = tempfile.mkdtemp(prefix="fs-preview-svc-")
        self.addCleanup(shutil.rmtree, self.src, True)
        self.addCleanup(shutil.rmtree, self.cache_root, True)
        self.now = [5_000.0]
        self.settings = {}
        self.shell = FakeShell()
        self.service = cfp.FilePreviewService(
            self.cache_root, settings_fn=lambda: self.settings, shell=self.shell,
            clock=lambda: self.now[0], start_worker=False)

    def write(self, rel, data):
        path = os.path.join(self.src, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_pending_then_ready_in_file_card_order(self):
        self.write("Pics/a.txt", b"text")
        self.write("Pics/b.mp4", b"\x00" * 32)
        self.write("Pics/c.gif", _gif(2))
        item = _file_item("batch-1", ["Pics/a.txt", "Pics/b.mp4", "Pics/c.gif"], self.src)
        first = self.service.describe(item, count=2)
        self.assertEqual(first["total_files"], 3)
        self.assertEqual([row["state"] for row in first["previews"]], ["pending", "pending"])
        self.assertEqual([row["ext"] for row in first["previews"]], ["txt", "mp4"])
        self.service.run_pending()
        ready = self.service.describe(item, count=2)["previews"]
        self.assertEqual([row["state"] for row in ready], ["ready", "ready"])
        self.assertEqual([row["kind"] for row in ready], ["icon", "video"])
        self.assertTrue(ready[1]["still"] and ready[1]["video"])
        self.assertEqual(len(ready[1]["key"]), 16)
        path, mime = self.service.resolve(item, 1, "still", ready[1]["key"])
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"PNG-thumb")
        self.assertEqual(mime, "image/png")
        self.assertIsNone(self.service.resolve(item, 1, "still", "0" * 16))
        self.assertIsNone(self.service.resolve(item, 7, "still"))
        # No private path in a descriptor.
        self.assertNotIn(self.src, json.dumps(ready))

    def test_missing_moved_and_changed_files_are_neutral(self):
        path = self.write("a.png", _png())
        item = _file_item("file-1", ["a.png"], self.src)
        os.remove(path)
        self.service.describe(item)
        self.service.run_pending()
        row = self.service.describe(item)["previews"][0]
        self.assertEqual((row["state"], row["kind"]), ("missing", "none"))
        # The file comes back changed: still neutral, the copy is gone.
        self.write("a.png", _png(3, 3))
        self.now[0] += cfp.MISS_TTL_S + 1
        self.assertEqual(self.service.describe(item)["previews"][0]["state"], "pending")
        self.service.run_pending()
        self.assertEqual(self.service.describe(item)["previews"][0]["state"], "missing")

    def test_locked_file_is_neutral_and_retried_later(self):
        self.write("doc.txt", b"x")
        item = _file_item("file-2", ["doc.txt"], self.src)
        self.service.describe(item)
        with mock.patch.object(cfp, "generate", side_effect=PermissionError("in use")):
            self.service.run_pending()
        self.assertEqual(self.service.describe(item)["previews"][0]["state"], "missing")
        self.now[0] += cfp.MISS_TTL_S + 1
        self.assertEqual(self.service.describe(item)["previews"][0]["state"], "pending")
        self.service.run_pending()
        self.assertEqual(self.service.describe(item)["previews"][0]["kind"], "icon")

    def test_unsupported_without_shell_is_final_none(self):
        service = cfp.FilePreviewService(self.cache_root, shell=None, start_worker=False)
        self.write("doc.txt", b"x")
        item = _file_item("file-3", ["doc.txt"], self.src)
        service.describe(item)
        service.run_pending()
        row = service.describe(item)["previews"][0]
        self.assertEqual((row["state"], row["kind"], row["still"]), ("ready", "none", False))
        self.assertTrue(service._jobs.empty())

    def test_remote_items_show_type_icons(self):
        item = _file_item("remote-1", [], self.src)
        item["batch_manifest"]["entries"] = [{"path": "Docs/report.pdf", "type": "file", "size": 5}]
        item["files"] = []
        self.assertEqual(self.service.describe(item)["previews"][0]["state"], "pending")
        self.service.run_pending()
        row = self.service.describe(item)["previews"][0]
        self.assertEqual((row["state"], row["kind"], row["ext"]), ("ready", "icon", "pdf"))
        self.assertIn(("type", ".pdf"), self.shell.calls)
        self.assertIsNone(self.service.video_source(item, 0))

    def test_video_source_and_settings(self):
        path = self.write("v.mp4", b"\x00" * 40)
        self.write("v.mkv", b"\x00" * 40)
        item = _file_item("batch-2", ["v.mkv", "v.mp4"], self.src)
        self.assertEqual(self.service.video_source(item, 1), (path, "video/mp4", 40))
        self.assertIsNone(self.service.video_source(item, 0))       # not browser-playable
        self.settings = {"file_preview_video_hover": False, "file_preview_animation": "hover"}
        self.assertIsNone(self.service.video_source(item, 1))
        result = self.service.describe(item)
        self.assertEqual((result["animation"], result["video_hover"]), ("hover", False))

    def test_count_is_bounded(self):
        names = [f"f{n}.txt" for n in range(12)]
        for name in names:
            self.write(name, b"1")
        item = _file_item("batch-3", names, self.src)
        self.assertEqual(len(self.service.describe(item, count=50)["previews"]), cfp.MAX_PREVIEW_COUNT)
        self.assertEqual(len(self.service.describe(item, count="x")["previews"]), 4)

    def test_background_worker_generates(self):
        service = cfp.FilePreviewService(self.cache_root, shell=self.shell)
        self.addCleanup(service.shutdown)
        self.write("w.txt", b"x")
        item = _file_item("file-4", ["w.txt"], self.src)
        service.describe(item)
        for _ in range(200):
            if service.describe(item)["previews"][0]["state"] == "ready":
                break
            threading.Event().wait(0.01)
        self.assertEqual(service.describe(item)["previews"][0]["kind"], "icon")


class SettingsTests(unittest.TestCase):
    def test_clipboard_settings_normalize_preview_keys(self):
        out = cbm.clipboard_settings({"clipboard": {"file_preview_animation": "blink",
                                                    "file_preview_cache_mb": 1,
                                                    "file_preview_video_hover": 0}})
        self.assertEqual(out["file_preview_animation"], "always")
        self.assertEqual(out["file_preview_cache_mb"], 8)
        self.assertIs(out["file_preview_video_hover"], False)
        self.assertEqual(cfp.preview_settings(out)["cache_bytes"], 8 * 1024 * 1024)
        defaults = cbm.clipboard_settings({})
        self.assertEqual((defaults["file_preview_animation"], defaults["file_preview_cache_mb"],
                          defaults["file_preview_video_hover"]), ("always", 128, True))


class FilePreviewApiTests(unittest.TestCase):
    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="fs-preview-api-")
        self.cache_root = tempfile.mkdtemp(prefix="fs-preview-api-cache-")
        self.addCleanup(shutil.rmtree, self.src, True)
        self.addCleanup(shutil.rmtree, self.cache_root, True)
        for rel, data in (("Clips/movie.mp4", bytes(range(256)) * 4), ("Clips/read.txt", b"hi")):
            path = os.path.join(self.src, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
        self.item = _file_item("batch-9", ["Clips/movie.mp4", "Clips/read.txt"], self.src)
        text = {"item_id": "txt-1", "kind": "text", "size": 1, "created_at": 1.0}
        self.previews = cfp.FilePreviewService(self.cache_root, shell=FakeShell(),
                                               start_worker=False)
        web_api._refs = {}
        web_api.init(clip_mgr=FakeManager([self.item, text]), file_previews=self.previews)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        web_api._refs = {}

    def get(self, path, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def test_descriptor_then_cached_image(self):
        response, body = self.get("/api/clipboard/file-previews/batch-9?profile=local&count=4")
        self.assertEqual(response.status, 200)
        data = json.loads(body)
        self.assertEqual([row["state"] for row in data["previews"]], ["pending", "pending"])
        self.previews.run_pending()
        data = json.loads(self.get("/api/clipboard/file-previews/batch-9?profile=local")[1])
        video = data["previews"][0]
        self.assertEqual((video["kind"], video["still"], video["video"]), ("video", True, True))
        self.assertNotIn(self.src, json.dumps(data))
        response, body = self.get(
            f"/api/clipboard/file-preview/batch-9/0/still?profile=local&v={video['key']}")
        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"PNG-thumb")
        self.assertEqual(response.getheader("Content-Type"), "image/png")
        self.assertIn("max-age", response.getheader("Cache-Control"))

    def test_video_clip_supports_ranges(self):
        response, body = self.get("/api/clipboard/file-preview/batch-9/0/video?profile=local",
                                  {"Range": "bytes=10-19"})
        self.assertEqual(response.status, 206)
        self.assertEqual(body, (bytes(range(256)) * 4)[10:20])
        self.assertEqual(response.getheader("Content-Range"), "bytes 10-19/1024")
        self.assertEqual(response.getheader("Content-Type"), "video/mp4")
        response, body = self.get("/api/clipboard/file-preview/batch-9/0/video?profile=local")
        self.assertEqual((response.status, len(body)), (200, 1024))
        response, _ = self.get("/api/clipboard/file-preview/batch-9/0/video?profile=local",
                               {"Range": "bytes=5000-"})
        self.assertEqual(response.status, 416)
        response, _ = self.get("/api/clipboard/file-preview/batch-9/1/video?profile=local")
        self.assertEqual(response.status, 404)                     # text file: no clip

    def test_rejects_bad_requests(self):
        for path, status in (
                ("/api/clipboard/file-previews/txt-1?profile=local", 404),
                ("/api/clipboard/file-previews/nope?profile=local", 404),
                ("/api/clipboard/file-previews/..%2F..?profile=local", 400),
                ("/api/clipboard/file-preview/batch-9/0/json?profile=local", 404),
                ("/api/clipboard/file-preview/batch-9/x/still?profile=local", 404),
                ("/api/clipboard/file-preview/batch-9/0/still?profile=local", 404)):
            response, _ = self.get(path)
            self.assertEqual(response.status, status, path)

    def test_settings_expose_preview_defaults(self):
        class _State:
            lock = threading.Lock()
            config = {"clipboard": {"file_preview_animation": "hover"}}
        web_api._refs["istate"] = _State()
        data = json.loads(self.get("/api/settings")[1])
        self.assertEqual(data["file_preview_animation"], "hover")
        self.assertIs(data["file_preview_video_hover"], True)
        self.assertEqual(data["file_preview_cache_mb"], 128)


if __name__ == "__main__":
    unittest.main()
