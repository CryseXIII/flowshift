"""Clipboard pictures (no file): format choice, complete storage, thumbnails.

Covers what Windows offers for "Copy image" in a browser or a cut-out from a
program (PNG, CF_DIBV5, CF_DIB), which of them FlowShift keeps, the formats
that go back on the clipboard, and the background thumbnail of the item
card. Runs without Windows and without Pillow; Pillow-only paths are skipped
when the library is missing.
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
import time
import unittest
import zlib
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_file_preview as cfp
import clipboard_image as ci
import clipboard_win as cw
import web_api
from test_web_api_clipboard import FakeManager

LIMITS = cfp.preview_settings()

try:
    from PIL import Image  # noqa: F401
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False


# ── hand-built clipboard payloads ────────────────────────────────────
def _dibv5(width, height, rgba_top_down):
    """CF_DIBV5 as a browser/program writes it: 32-bit with alpha mask."""
    rows = [rgba_top_down[y * width * 4:(y + 1) * width * 4] for y in range(height)]
    bgra = bytearray()
    for row in reversed(rows):                       # bottom-up
        for x in range(width):
            r, g, b, a = row[x * 4:x * 4 + 4]
            bgra += bytes((b, g, r, a))
    return ci.rgba_to_dibv5(width, height, bytes(bgra))


def _dib32_rgb(width, height, bgrx, top_down=False):
    """Plain 32-bit CF_DIB (BI_RGB) whose fourth byte is left at 0."""
    header = struct.pack("<IiiHHIIiiII", 40, width, -height if top_down else height, 1, 32,
                         ci.BI_RGB, len(bgrx), 0, 0, 0, 0)
    return header + bgrx


def _png_pixels(png):
    """Decode a PNG written by cfp.encode_png_rgba (filter 0): (w, h, rgba)."""
    width, height = struct.unpack(">II", png[16:24])
    pos, idat = 8, b""
    while pos < len(png):
        length = struct.unpack(">I", png[pos:pos + 4])[0]
        if png[pos + 4:pos + 8] == b"IDAT":
            idat += png[pos + 8:pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 4
    rgba = b"".join(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)] for y in range(height))
    return width, height, rgba


class FormatTests(unittest.TestCase):
    def test_png_wins_over_bitmaps(self):
        png_ids = (0xC0F1, 0xC0F2)
        # Chromium: CF_BITMAP + PNG, Windows adds CF_DIB/CF_DIBV5 later.
        self.assertEqual(cw.pick_image_source([2, 0xC0F1, 8, 17], png_ids), ("png", 0xC0F1))
        self.assertEqual(cw.pick_image_source([17, 8, 0xC0F2], png_ids), ("png", 0xC0F2))

    def test_bitmap_the_program_placed_first(self):
        self.assertEqual(cw.pick_image_source([17, 8], ()), ("dibv5", cw.CF_DIBV5))
        self.assertEqual(cw.pick_image_source([8, 17], ()), ("dib", cw.CF_DIB))
        self.assertEqual(cw.pick_image_source([13, 2, 8, 17], ()), ("dib", cw.CF_DIB))
        self.assertIsNone(cw.pick_image_source([13, 0xC0F3], (0xC0F1,)))
        self.assertIsNone(cw.pick_image_source([], ()))

    def test_png_names_cover_browsers(self):
        self.assertEqual(cw.PNG_FORMAT_NAMES, ("PNG", "image/png"))

    def test_image_format_and_size(self):
        png = cfp.encode_png_rgba(3, 2, b"\x00" * 24)
        bmp = ci.make_synthetic_bmp(5, 4, [(1, 2, 3)] * 20)
        self.assertEqual((ci.image_format(png), ci.image_size(png)), ("png", (3, 2)))
        self.assertEqual((ci.image_format(bmp), ci.image_size(bmp)), ("bmp", (5, 4)))
        self.assertEqual((ci.image_format(b"GIF89a"), ci.image_size(b"junk")), ("", (0, 0)))

    def test_trim_png_drops_allocation_padding(self):
        png = cfp.encode_png_rgba(2, 2, b"\x10" * 16)
        self.assertEqual(ci.trim_png(png + b"\x00" * 37), png)
        self.assertEqual(ci.trim_png(png), png)
        self.assertIsNone(ci.trim_png(png[:-6]))              # IEND cut off
        self.assertIsNone(ci.trim_png(b"BM" + png))

    def test_dibv5_keeps_alpha_as_bmp(self):
        rgba = bytes((255, 0, 0, 255, 0, 255, 0, 0, 0, 0, 255, 128, 9, 9, 9, 64))
        bmp = ci.dib_to_bmp(_dibv5(2, 2, rgba))
        info = ci.parse_bmp(bmp)
        self.assertEqual((info["bi_size"], info["bit_count"], info["off_bits"]), (124, 32, 138))
        self.assertEqual(ci.bmp_thumbnail_rgba(bmp, 16), (2, 2, rgba))

    def test_alphabitfields_masks_counted(self):
        header = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 32, ci.BI_ALPHABITFIELDS, 4, 0, 0, 0, 0)
        masks = struct.pack("<IIII", 0xFF0000, 0xFF00, 0xFF, 0xFF000000)
        bmp = ci.dib_to_bmp(header + masks + bytes((30, 20, 10, 77)))
        self.assertEqual(ci.parse_bmp(bmp)["off_bits"], 14 + 40 + 16)
        self.assertEqual(ci.bmp_thumbnail_rgba(bmp, 8), (1, 1, bytes((10, 20, 30, 77))))


class BitmapThumbnailTests(unittest.TestCase):
    def test_unused_alpha_byte_means_opaque(self):
        bmp = ci.dib_to_bmp(_dib32_rgb(2, 1, bytes((1, 2, 3, 0, 4, 5, 6, 0))))
        self.assertEqual(ci.bmp_thumbnail_rgba(bmp, 8), (2, 1, bytes((3, 2, 1, 255, 6, 5, 4, 255))))

    def test_top_down_and_bottom_up_rows(self):
        top = ci.dib_to_bmp(_dib32_rgb(1, 2, bytes((0, 0, 255, 255, 255, 0, 0, 255)), top_down=True))
        bottom = ci.dib_to_bmp(_dib32_rgb(1, 2, bytes((255, 0, 0, 255, 0, 0, 255, 255))))
        self.assertEqual(ci.bmp_thumbnail_rgba(top, 8)[2][:4], bytes((255, 0, 0, 255)))
        self.assertEqual(ci.bmp_thumbnail_rgba(bottom, 8)[2][:4], bytes((255, 0, 0, 255)))

    def test_paletted_bitmap(self):
        palette = bytes((0, 0, 255, 0, 0, 255, 0, 0))             # red, green (BGRx)
        header = struct.pack("<IiiHHIIiiII", 40, 2, 1, 1, 8, ci.BI_RGB, 4, 0, 0, 2, 0)
        bmp = ci.dib_to_bmp(header + palette + bytes((1, 0, 0, 0)))
        self.assertEqual(ci.bmp_thumbnail_rgba(bmp, 8),
                         (2, 1, bytes((0, 255, 0, 255, 255, 0, 0, 255))))

    def test_large_screenshot_is_scaled_and_fast(self):
        width, height = 3000, 2000                                  # ~24 MB CF_DIB
        bmp = ci.dib_to_bmp(_dib32_rgb(width, height, b"\x40\x80\xc0\x00" * (width * height)))
        started = time.perf_counter()
        thumb = ci.bmp_thumbnail_rgba(bmp, 160)
        elapsed = time.perf_counter() - started
        self.assertEqual(thumb[:2], (160, 107))
        self.assertEqual(thumb[2][:4], bytes((0xC0, 0x80, 0x40, 255)))
        self.assertLess(elapsed, 2.0)

    def test_unsupported_or_damaged_bitmaps(self):
        bmp = ci.make_synthetic_bmp(2, 2, [(1, 2, 3)] * 4)
        rle = bytearray(bmp)
        rle[30:34] = struct.pack("<I", 1)                            # BI_RLE8
        self.assertIsNone(ci.bmp_thumbnail_rgba(bytes(rle), 8))
        self.assertIsNone(ci.bmp_thumbnail_rgba(bmp[:60], 8))      # pixels cut off
        self.assertIsNone(ci.bmp_thumbnail_rgba(b"not a bmp", 8))


class PasteFormatTests(unittest.TestCase):
    def test_bitmap_items_go_back_as_their_dib(self):
        plain = ci.make_synthetic_bmp(2, 2, [(1, 2, 3)] * 4)
        self.assertEqual(ci.paste_formats(plain), [("DIB", ci.bmp_to_dib(plain))])
        v5 = ci.dib_to_bmp(_dibv5(1, 1, bytes((1, 2, 3, 4))))
        self.assertEqual(ci.paste_formats(v5), [("DIBV5", ci.bmp_to_dib(v5))])
        self.assertEqual(ci.paste_formats(b"junk"), [])

    def test_png_item_keeps_exact_png_first(self):
        png = cfp.encode_png_rgba(2, 1, bytes((255, 0, 0, 0, 0, 0, 255, 255)))
        formats = ci.paste_formats(png)
        self.assertEqual(formats[0], ("PNG", png))
        if not HAVE_PIL:
            self.assertEqual(len(formats), 1)

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_png_item_also_offers_bitmaps_for_paint(self):
        png = cfp.encode_png_rgba(2, 1, bytes((255, 0, 0, 0, 0, 0, 255, 255)))
        names = [name for name, _ in ci.paste_formats(png)]
        self.assertEqual(names, ["PNG", "DIBV5", "DIB"])
        _, dibv5, dib = [data for _, data in ci.paste_formats(png)]
        self.assertEqual(ci.bmp_thumbnail_rgba(ci.dib_to_bmp(dibv5), 8)[2],
                         bytes((255, 0, 0, 0, 0, 0, 255, 255)))
        # Plain bitmap: transparent pixel on white, no alpha.
        flat = ci.bmp_thumbnail_rgba(ci.dib_to_bmp(dib), 8)[2]
        self.assertEqual(flat, bytes((255, 255, 255, 255, 0, 0, 255, 255)))


class GenerateTests(unittest.TestCase):
    def test_bitmap_thumbnail_keeps_transparency(self):
        rgba = bytes((255, 0, 0, 0, 0, 255, 0, 200))
        result = cfp.generate_clip_image(ci.dib_to_bmp(_dibv5(2, 1, rgba)), LIMITS)
        self.assertEqual(result["kind"], cfp.KIND_IMAGE)
        data, mime = result["variants"]["still"]
        self.assertEqual(mime, "image/png")
        if not HAVE_PIL:
            self.assertEqual(_png_pixels(data), (2, 1, rgba))
        self.assertEqual((result["width"], result["height"]), (2, 1))

    def test_large_bitmap_is_scaled_down(self):
        width, height = 2400, 1200
        bmp = ci.dib_to_bmp(_dib32_rgb(width, height, b"\x00\x00\xff\x00" * (width * height)))
        with mock.patch.object(cfp, "Image", None):
            result = cfp.generate_clip_image(bmp, LIMITS)
        w, h, _ = _png_pixels(result["variants"]["still"][0])
        self.assertEqual((w, h), (LIMITS["max_px"], LIMITS["max_px"] // 2))
        self.assertEqual((result["width"], result["height"]), (width, height))

    def test_png_without_pillow_passes_through(self):
        png = cfp.encode_png_rgba(2, 2, b"\x10\x20\x30\x40" * 4)
        with mock.patch.object(cfp, "Image", None):
            result = cfp.generate_clip_image(png, LIMITS)
        self.assertEqual(result["variants"]["still"], (png, "image/png"))
        too_big = dict(LIMITS, max_passthrough_bytes=10)
        with mock.patch.object(cfp, "Image", None), self.assertRaises(cfp.PreviewError):
            cfp.generate_clip_image(png, too_big)

    def test_garbage_is_a_preview_error(self):
        with self.assertRaises(cfp.PreviewError):
            cfp.generate_clip_image(b"<img src=x>", LIMITS)


class HtmlOnlyPictureTests(unittest.TestCase):
    def test_image_address_without_text_is_marked(self):
        import clipboard_html as chtml
        import clipboard_model as cbm
        only = chtml.build_cf_html('<img src="https://example.invalid/cat.png" alt="">')
        item = cbm.make_html_item(only, "")
        self.assertIs(item["metadata"].get("image_only"), True)
        self.assertIs(cbm.manifest_item(item)["metadata"].get("image_only"), True)
        self.assertIs(cbm.public_card_item(item)["metadata"].get("image_only"), True)
        mixed = cbm.make_html_item(chtml.build_cf_html('<p>Katze</p><img src="x.png">'), "")
        self.assertNotIn("image_only", mixed["metadata"])
        text = cbm.make_html_item(chtml.build_cf_html("<b>nur Text</b>"), "")
        self.assertNotIn("image_only", text["metadata"])

    def test_detection_helper(self):
        import clipboard_html as chtml
        self.assertTrue(chtml.html_is_image_only("<div>\n <IMG SRC='a.gif'> </div>"))
        self.assertFalse(chtml.html_is_image_only("<img src=a.gif> Bildunterschrift"))
        self.assertFalse(chtml.html_is_image_only(""))


def _image_item(item_id, data, available=True, mime=None):
    return {"item_id": item_id, "kind": "image", "size": len(data), "created_at": 1.0,
            "sha256": ci_sha(data), "available": available,
            "mime": mime or ("image/png" if ci.image_format(data) == "png" else "image/bmp")}


def ci_sha(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fs-clipimg-cache-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.service = cfp.FilePreviewService(self.root, shell=None, start_worker=False)
        self.bmp = ci.dib_to_bmp(_dibv5(2, 2, b"\x11\x22\x33\x80" * 4))

    def test_pending_then_ready_without_blocking(self):
        calls = []

        def data_fn():
            calls.append(1)
            return self.bmp
        item = _image_item("img-1", self.bmp)
        first = self.service.describe(item, data_fn=data_fn)
        self.assertEqual(first["total_files"], 1)
        self.assertEqual(first["previews"][0]["state"], "pending")
        self.assertEqual(first["previews"][0]["source"], "clipboard")
        self.assertEqual(calls, [])                      # only the worker reads the bytes
        self.service.run_pending()
        self.assertEqual(calls, [1])
        row = self.service.describe(item, data_fn=data_fn)["previews"][0]
        self.assertEqual((row["state"], row["kind"], row["still"], row["ext"]),
                         ("ready", "image", True, "bmp"))
        path, mime = self.service.resolve(item, 0, "still", row["key"])
        self.assertEqual(mime, "image/png")
        self.assertTrue(path.startswith(self.root))
        self.assertIsNone(self.service.resolve(item, 1, "still"))
        self.assertIsNone(self.service.video_source(item, 0))
        # Same picture again (other item id): cached by content, no new work.
        again = self.service.describe(_image_item("img-2", self.bmp), data_fn=data_fn)
        self.assertEqual(again["previews"][0]["state"], "ready")
        self.service.run_pending()
        self.assertEqual(calls, [1])

    def test_picture_not_on_this_device_shows_neutral_symbol(self):
        called = []
        item = _image_item("img-3", self.bmp, available=False)
        row = self.service.describe(item, data_fn=lambda: called.append(1))["previews"][0]
        self.assertEqual((row["state"], row["kind"], row["still"]), ("missing", "none", False))
        self.service.run_pending()
        self.assertEqual(called, [])

    def test_vanished_data_and_damaged_picture(self):
        item = _image_item("img-4", self.bmp)
        self.service.describe(item, data_fn=lambda: None)
        self.service.run_pending()
        row = self.service.describe(item, data_fn=lambda: self.bmp)["previews"][0]
        self.assertEqual(row["state"], "missing")                 # retry wait
        broken = _image_item("img-5", b"BM" + b"\x00" * 80)
        self.service.describe(broken, data_fn=lambda: b"BM" + b"\x00" * 80)
        self.service.run_pending()
        row = self.service.describe(broken)["previews"][0]
        self.assertEqual((row["state"], row["kind"]), ("ready", "none"))

    def test_cache_limit_applies_to_pictures(self):
        service = cfp.FilePreviewService(
            self.root, settings_fn=lambda: {"file_preview_cache_mb": 1},
            shell=None, start_worker=False)
        for index in range(12):
            width = 300 + index
            bmp = ci.dib_to_bmp(_dib32_rgb(width, 300, os.urandom(width * 300 * 4)))
            service.describe(_image_item(f"big-{index}", bmp), data_fn=lambda b=bmp: b)
            service.run_pending()
        self.assertLessEqual(service.cache.total_bytes(), 1024 * 1024)


class ImagePreviewApiTests(unittest.TestCase):
    def setUp(self):
        self.cache_root = tempfile.mkdtemp(prefix="fs-clipimg-api-")
        self.addCleanup(shutil.rmtree, self.cache_root, True)
        self.png = cfp.encode_png_rgba(2, 2, b"\x10\x20\x30\x80" * 4)
        self.item = _image_item("img-9", self.png)
        manager = FakeManager([self.item, {"item_id": "txt-1", "kind": "text", "size": 1}])
        manager._store.get_data = lambda item_id: self.png if item_id == "img-9" else None
        self.previews = cfp.FilePreviewService(self.cache_root, shell=None, start_worker=False)
        web_api._refs = {}
        web_api.init(clip_mgr=manager, file_previews=self.previews)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        web_api._refs = {}

    def get(self, path):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def test_descriptor_then_thumbnail(self):
        data = json.loads(self.get("/api/clipboard/file-previews/img-9?profile=local&count=1")[1])
        self.assertEqual(data["previews"][0]["state"], "pending")
        self.previews.run_pending()
        data = json.loads(self.get("/api/clipboard/file-previews/img-9?profile=local")[1])
        row = data["previews"][0]
        self.assertEqual((row["state"], row["kind"], row["ext"], row["source"]),
                         ("ready", "image", "png", "clipboard"))
        response, body = self.get(
            f"/api/clipboard/file-preview/img-9/0/still?profile=local&v={row['key']}")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "image/png")
        self.assertTrue(body.startswith(ci.PNG_SIGNATURE))
        self.assertNotIn(self.cache_root, json.dumps(data))

    def test_rejects_other_indexes_and_variants(self):
        self.get("/api/clipboard/file-previews/img-9?profile=local")
        self.previews.run_pending()
        for path in ("/api/clipboard/file-preview/img-9/1/still?profile=local",
                     "/api/clipboard/file-preview/img-9/0/video?profile=local",
                     "/api/clipboard/file-previews/txt-1?profile=local"):
            self.assertEqual(self.get(path)[0].status, 404, path)


if __name__ == "__main__":
    unittest.main()
