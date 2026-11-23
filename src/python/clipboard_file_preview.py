"""Per-file previews for clipboard file items (Viktor 23.11.2026).

File cards in the clipboard overlay and the WebGUI show a small preview per
copied file: images as a thumbnail, GIF/APNG/animated WebP as a small
animation, videos as a still frame (plus an optional short muted clip on
hover) and every other file with its Windows type icon. Missing, moved,
changed or locked files get a neutral symbol in the UI instead of an error.

Previews are generated on a background thread and kept in a size-limited
disk cache (``%ProgramData%\\FlowShift\\clipboard\\previews``). A request
never waits for generation: it answers ``pending`` and the UI asks again.
Absolute source paths stay inside this module; the API only exposes item
ids, file indexes and opaque cache keys.

Pillow is optional (the installer ships it). Without it, small browser-native
images are passed through unchanged and the rest falls back to icons.
Windows shell thumbnails and type icons are used only on Windows.

Defaults and ranges of every setting and limit (preview size, source sizes,
frame count, cache size and age, retry wait, response size) live only in
``clipboard_model.DEFAULT_CLIPBOARD_SETTINGS``; ``preview_settings`` reads
the normalised values from there.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import stat
import struct
import sys
import threading
import time
import zlib

try:  # Pillow is optional; every caller handles its absence.
    from PIL import Image, ImageSequence
except Exception:  # pragma: no cover - depends on the environment
    Image = None
    ImageSequence = None

import clipboard_model as cbm

PREVIEW_VERSION = 1

KIND_IMAGE = "image"
KIND_ANIMATED = "animated"
KIND_VIDEO = "video"
KIND_ICON = "icon"
KIND_NONE = "none"               # UI shows the neutral symbol

STATE_READY = "ready"
STATE_PENDING = "pending"
STATE_MISSING = "missing"

IMAGE_EXTS = frozenset((".png", ".apng", ".jpg", ".jpeg", ".jfif", ".gif", ".webp",
                        ".bmp", ".dib", ".tif", ".tiff", ".ico"))
ANIMATION_CAPABLE_EXTS = frozenset((".gif", ".png", ".apng", ".webp"))
VIDEO_EXTS = frozenset((".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".wmv",
                        ".mpg", ".mpeg", ".ogv", ".3gp", ".flv", ".ts", ".mts", ".m2ts"))
# Formats the WebView2/Chromium <video> element plays without extra codecs.
BROWSER_VIDEO_MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mp4",
                      ".webm": "video/webm", ".ogv": "video/ogg"}
# Formats a browser shows as-is (pass-through without Pillow).
_BROWSER_IMAGE_MIME = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif",
                       "webp": "image/webp", "bmp": "image/bmp"}

# Header bytes read to tell the format; the source limits (above them a file
# is not decoded or only its first frame is read, nothing reads a huge file
# completely) come from preview_settings().
SNIFF_BYTES = 64 * 1024
_MIN_FRAME_MS = 20

CACHE_CLEAN_TARGET = 0.8         # after an overflow shrink to 80% of the limit
_QUEUE_MAX = 256
_MB = 1024 * 1024


class PreviewError(Exception):
    """Generation failed for one file; the UI falls back to an icon."""


def _ext(name):
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    dot = base.rfind(".")
    if dot <= 0 or dot == len(base) - 1:
        return ""
    ext = base[dot:].lower()
    return ext if len(ext) <= 16 and ext[1:].isalnum() else ""


def classify(name):
    """Planned preview kind from the file name alone (no I/O)."""
    ext = _ext(name)
    if ext in IMAGE_EXTS:
        return KIND_IMAGE
    if ext in VIDEO_EXTS:
        return KIND_VIDEO
    return KIND_ICON


# ── format sniffing (pure Python, bounded reads) ─────────────────────
def sniff_format(head):
    """Container format from the first bytes, or ``""``."""
    head = bytes(head or b"")
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "gif"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"BM"):
        return "bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:4] == b"\x00\x00\x01\x00":
        return "ico"
    return ""


def _png_is_animated(head):
    pos = 8
    while pos + 8 <= len(head):
        length, ctype = struct.unpack(">I4s", head[pos:pos + 8])
        if ctype == b"acTL":
            if pos + 12 <= len(head):
                return struct.unpack(">I", head[pos + 8:pos + 12])[0] > 1
            return True
        if ctype in (b"IDAT", b"IEND"):
            return False
        pos += 12 + length
    return False


def _webp_is_animated(head):
    # VP8X chunk: flags byte bit 1 (0x02) is the animation flag.
    return len(head) >= 21 and head[12:16] == b"VP8X" and bool(head[20] & 0x02)


def _gif_frame_count(data, stop_at=2):
    """Count image descriptors in a (possibly truncated) GIF, up to ``stop_at``."""
    if len(data) < 13:
        return 0
    flags = data[10]
    pos = 13 + ((3 << ((flags & 7) + 1)) if flags & 0x80 else 0)
    frames = 0
    size = len(data)
    while pos < size:
        block = data[pos]
        if block == 0x3B:                       # trailer
            break
        if block == 0x21:                       # extension: label + sub-blocks
            pos += 2
        elif block == 0x2C:                     # image descriptor
            frames += 1
            if frames >= stop_at:
                return frames
            if pos + 10 > size:
                break
            local = data[pos + 9]
            pos += 10 + ((3 << ((local & 7) + 1)) if local & 0x80 else 0)
            pos += 1                            # LZW minimum code size
        else:
            break
        while pos < size:                       # skip data sub-blocks
            length = data[pos]
            pos += 1 + length
            if length == 0:
                break
    return frames


def _read_head(path, limit):
    with open(path, "rb") as handle:
        return handle.read(limit)


def detect_animation(path, fmt, head, gif_scan_bytes):
    """True when the file is an animated GIF, APNG or animated WebP."""
    if fmt == "png":
        return _png_is_animated(head)
    if fmt == "webp":
        return _webp_is_animated(head)
    if fmt == "gif":
        data = head if len(head) < SNIFF_BYTES else _read_head(path, gif_scan_bytes)
        return _gif_frame_count(data) >= 2
    return False


# ── small pure-Python PNG encoder (shell bitmaps without Pillow) ─────
def encode_png_rgba(width, height, rgba):
    """Encode raw RGBA rows as a PNG (filter 0)."""
    width = int(width)
    height = int(height)
    if width <= 0 or height <= 0 or len(rgba) != width * height * 4:
        raise PreviewError("invalid RGBA buffer")
    stride = width * 4
    raw = b"".join(b"\x00" + bytes(rgba[row * stride:(row + 1) * stride])
                   for row in range(height))

    def chunk(kind, payload):
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# ── Pillow generation ────────────────────────────────────────────────
def _resample():
    resampling = getattr(Image, "Resampling", None)
    if resampling is not None:
        return getattr(resampling, "LANCZOS", getattr(resampling, "BICUBIC", 0))
    return getattr(Image, "LANCZOS", 0)


def _fit(img, max_px):
    img.thumbnail((max_px, max_px), _resample())
    return img


def _checked_open(path, max_pixels):
    img = Image.open(path)
    width, height = img.size
    if width <= 0 or height <= 0 or width * height > max_pixels:
        img.close()
        raise PreviewError("image dimensions out of range")
    return img


def pil_still_png(path, max_px, max_pixels):
    """First frame scaled to ``max_px`` as PNG bytes (Pillow)."""
    if Image is None:
        raise PreviewError("Pillow unavailable")
    with _checked_open(path, max_pixels) as img:
        if img.format == "JPEG":
            try:                                # decode at reduced size, cheap
                img.draft("RGB", (max_px * 2, max_px * 2))
            except Exception:
                pass
        img.seek(0)
        frame = img.convert("RGBA")
    _fit(frame, max_px)
    out = io.BytesIO()
    frame.save(out, format="PNG", optimize=False)
    return out.getvalue(), frame.size


def pil_animation(path, max_px, max_frames, max_pixels):
    """Scaled-down animation; returns (bytes, mime, frame_count) or None."""
    if Image is None or ImageSequence is None:
        return None
    frames = []
    durations = []
    with _checked_open(path, max_pixels) as img:
        default = int(img.info.get("duration", 100) or 100)
        for index, frame in enumerate(ImageSequence.Iterator(img)):
            if index >= max_frames:
                break
            durations.append(max(_MIN_FRAME_MS, int(frame.info.get("duration", default) or default)))
            frames.append(_fit(frame.convert("RGBA"), max_px))
    if len(frames) < 2:
        return None
    out = io.BytesIO()
    try:
        frames[0].save(out, format="WEBP", save_all=True, append_images=frames[1:],
                       duration=durations, loop=0, quality=80, method=4)
        return out.getvalue(), "image/webp", len(frames)
    except Exception:
        out = io.BytesIO()
        frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:],
                       duration=durations, loop=0, disposal=2, optimize=False)
        return out.getvalue(), "image/gif", len(frames)


# ── Windows shell thumbnails and type icons ──────────────────────────
class WindowsShell:
    """Shell thumbnails (IShellItemImageFactory) and type icons via ctypes."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.wt = wintypes
        self.shell32 = ctypes.windll.shell32
        self.gdi32 = ctypes.windll.gdi32
        self.user32 = ctypes.windll.user32
        self.ole32 = ctypes.windll.ole32
        self._com = threading.local()

        class GUID(ctypes.Structure):
            _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16),
                        ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]

        class SIZE(ctypes.Structure):
            _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

        class BITMAP(ctypes.Structure):
            _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                        ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                        ("bmPlanes", ctypes.c_ushort), ("bmBitsPixel", ctypes.c_ushort),
                        ("bmBits", ctypes.c_void_p)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_ushort),
                        ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_uint32),
                        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
                        ("biClrImportant", ctypes.c_uint32)]

        class SHFILEINFOW(ctypes.Structure):
            _fields_ = [("hIcon", ctypes.c_void_p), ("iIcon", ctypes.c_int),
                        ("dwAttributes", ctypes.c_uint32),
                        ("szDisplayName", ctypes.c_wchar * 260),
                        ("szTypeName", ctypes.c_wchar * 80)]

        self.GUID = GUID
        self.SIZE = SIZE
        self.BITMAP = BITMAP
        self.BITMAPINFOHEADER = BITMAPINFOHEADER
        self.SHFILEINFOW = SHFILEINFOW
        # {bcc18b79-ba16-442f-80c4-8a59c30c463b}
        self.iid_image_factory = GUID(0xBCC18B79, 0xBA16, 0x442F,
                                      (ctypes.c_ubyte * 8)(0x80, 0xC4, 0x8A, 0x59,
                                                           0xC3, 0x0C, 0x46, 0x3B))
        self.shell32.SHCreateItemFromParsingName.argtypes = [
            ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p)]
        self.shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
        self.shell32.SHGetFileInfoW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(SHFILEINFOW),
            ctypes.c_uint, ctypes.c_uint]
        self.shell32.SHGetFileInfoW.restype = ctypes.c_size_t
        self.gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        self.gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                         ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_uint]
        self.gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                ctypes.c_uint,
                                                ctypes.POINTER(ctypes.c_void_p),
                                                ctypes.c_void_p, ctypes.c_uint32]
        self.gdi32.CreateDIBSection.restype = ctypes.c_void_p
        self.gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        self.gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
        self.gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.gdi32.SelectObject.restype = ctypes.c_void_p
        self.gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        self.gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
        self.user32.DrawIconEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        self.user32.DestroyIcon.argtypes = [ctypes.c_void_p]

    def _ensure_com(self):
        if not getattr(self._com, "ready", False):
            self.ole32.CoInitializeEx(None, 0x2)   # COINIT_APARTMENTTHREADED
            self._com.ready = True

    def _bitmap_rgba(self, hbitmap):
        ctypes = self.ctypes
        bm = self.BITMAP()
        if not self.gdi32.GetObjectW(hbitmap, ctypes.sizeof(bm), ctypes.byref(bm)):
            raise PreviewError("GetObject failed")
        width, height = int(bm.bmWidth), abs(int(bm.bmHeight))
        if width <= 0 or height <= 0 or width * height > 4096 * 4096:
            raise PreviewError("shell bitmap size out of range")
        header = self.BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(header)
        header.biWidth = width
        header.biHeight = -height                  # top-down rows
        header.biPlanes = 1
        header.biBitCount = 32
        buf = (ctypes.c_ubyte * (width * height * 4))()
        hdc = self.gdi32.CreateCompatibleDC(None)
        try:
            if not self.gdi32.GetDIBits(hdc, hbitmap, 0, height, buf, ctypes.byref(header), 0):
                raise PreviewError("GetDIBits failed")
        finally:
            self.gdi32.DeleteDC(hdc)
        return width, height, bgra_to_rgba(bytes(buf), premultiplied=True)

    def thumbnail_png(self, path, px, icon_only=False):
        """Shell thumbnail (video still) or file icon of an existing file."""
        ctypes = self.ctypes
        self._ensure_com()
        factory = ctypes.c_void_p()
        hr = self.shell32.SHCreateItemFromParsingName(
            path, None, ctypes.byref(self.iid_image_factory), ctypes.byref(factory))
        if hr != 0 or not factory:
            raise PreviewError(f"shell item unavailable hr=0x{hr & 0xFFFFFFFF:08x}")
        vtable = ctypes.cast(ctypes.cast(factory, ctypes.POINTER(ctypes.c_void_p))[0],
                             ctypes.POINTER(ctypes.c_void_p))
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
        get_image = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, self.SIZE, ctypes.c_int,
                                       ctypes.POINTER(ctypes.c_void_p))(vtable[3])
        hbitmap = ctypes.c_void_p()
        try:
            flags = 0x04 if icon_only else 0x08        # SIIGBF_ICONONLY / THUMBNAILONLY
            hr = get_image(factory, self.SIZE(px, px), flags, ctypes.byref(hbitmap))
            if hr != 0 or not hbitmap:
                raise PreviewError(f"shell image unavailable hr=0x{hr & 0xFFFFFFFF:08x}")
            width, height, rgba = self._bitmap_rgba(hbitmap)
        finally:
            if hbitmap:
                self.gdi32.DeleteObject(hbitmap)
            release(factory)
        return encode_png_rgba(width, height, rgba)

    def _draw_icon(self, hicon, px, background):
        ctypes = self.ctypes
        header = self.BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(header)
        header.biWidth = px
        header.biHeight = -px
        header.biPlanes = 1
        header.biBitCount = 32
        bits = ctypes.c_void_p()
        hdc = self.gdi32.CreateCompatibleDC(None)
        dib = self.gdi32.CreateDIBSection(hdc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        if not dib or not bits:
            self.gdi32.DeleteDC(hdc)
            raise PreviewError("CreateDIBSection failed")
        old = self.gdi32.SelectObject(hdc, dib)
        try:
            ctypes.memset(bits, background, px * px * 4)
            self.user32.DrawIconEx(hdc, 0, 0, hicon, px, px, 0, None, 0x0003)  # DI_NORMAL
            return ctypes.string_at(bits, px * px * 4)
        finally:
            self.gdi32.SelectObject(hdc, old)
            self.gdi32.DeleteObject(dib)
            self.gdi32.DeleteDC(hdc)

    def type_icon_png(self, ext, px, directory=False):
        """Icon of a file type by extension; works without the file."""
        ctypes = self.ctypes
        info = self.SHFILEINFOW()
        attrs = 0x10 if directory else 0x80        # FILE_ATTRIBUTE_DIRECTORY / NORMAL
        name = "folder" if directory else ("file" + (ext or ""))
        # SHGFI_ICON | SHGFI_LARGEICON | SHGFI_USEFILEATTRIBUTES
        if not self.shell32.SHGetFileInfoW(name, attrs, ctypes.byref(info),
                                           ctypes.sizeof(info), 0x100 | 0x0 | 0x10):
            raise PreviewError("SHGetFileInfo failed")
        if not info.hIcon:
            raise PreviewError("no type icon")
        try:
            on_black = self._draw_icon(info.hIcon, px, 0x00)
            on_white = self._draw_icon(info.hIcon, px, 0xFF)
        finally:
            self.user32.DestroyIcon(info.hIcon)
        return encode_png_rgba(px, px, icon_rgba_from_backgrounds(on_black, on_white))


def bgra_to_rgba(data, premultiplied=False):
    """BGRA rows (GDI) to straight RGBA; all-zero alpha means opaque."""
    out = bytearray(len(data))
    out[0::4] = data[2::4]
    out[1::4] = data[1::4]
    out[2::4] = data[0::4]
    alpha = data[3::4]
    if not any(alpha):
        out[3::4] = b"\xff" * (len(data) // 4)
        return bytes(out)
    out[3::4] = alpha
    if premultiplied:
        for i in range(0, len(out), 4):
            a = out[i + 3]
            if 0 < a < 255:
                out[i] = min(255, out[i] * 255 // a)
                out[i + 1] = min(255, out[i + 1] * 255 // a)
                out[i + 2] = min(255, out[i + 2] * 255 // a)
    return bytes(out)


def icon_rgba_from_backgrounds(on_black, on_white):
    """Recover RGBA from an icon drawn on black and on white (BGRA rows)."""
    if len(on_black) != len(on_white) or len(on_black) % 4:
        raise PreviewError("icon buffers differ")
    out = bytearray(len(on_black))
    for i in range(0, len(on_black), 4):
        diff = ((on_white[i] - on_black[i]) + (on_white[i + 1] - on_black[i + 1])
                + (on_white[i + 2] - on_black[i + 2])) // 3
        alpha = max(0, min(255, 255 - diff))
        out[i + 3] = alpha
        if alpha:
            out[i] = min(255, on_black[i + 2] * 255 // alpha)
            out[i + 1] = min(255, on_black[i + 1] * 255 // alpha)
            out[i + 2] = min(255, on_black[i] * 255 // alpha)
    return bytes(out)


def _default_shell():
    if sys.platform != "win32":
        return None
    try:
        return WindowsShell()
    except Exception:
        return None


# ── generation of one file ───────────────────────────────────────────
def source_matches(path, size, mtime_ns):
    """True when ``path`` is still the regular file that was copied."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or int(st.st_size) != int(size):
        return False
    return not mtime_ns or int(getattr(st, "st_mtime_ns", 0)) == int(mtime_ns)


def generate(path, size, limits=None, shell=None):
    """Build the preview for one existing file.

    ``limits`` is a ``preview_settings()`` dict (central defaults when
    omitted). Returns ``{"kind", "variants": {name: (bytes, mime)}, ...}``.
    Raises ``OSError`` when the file cannot be opened (locked) and
    ``PreviewError`` when no preview can be built at all.
    """
    limits = limits or preview_settings()
    max_px = limits["max_px"]
    name = os.path.basename(path)
    planned = classify(name)
    ext = _ext(name)
    if planned == KIND_IMAGE and size <= limits["max_image_source_bytes"]:
        head = _read_head(path, SNIFF_BYTES)       # raises OSError when locked
        fmt = sniff_format(head)
        if fmt:
            animated = fmt in ("gif", "png", "webp") and detect_animation(
                path, fmt, head, limits["gif_scan_bytes"])
            result = _image_preview(path, size, fmt, head, animated, limits)
            if result:
                return result
    elif planned == KIND_IMAGE:
        _read_head(path, 1)                        # locked files still show as missing
    if planned == KIND_VIDEO:
        _read_head(path, 1)
        if shell is not None:
            try:
                png = shell.thumbnail_png(path, max_px)
                return {"kind": KIND_VIDEO, "variants": {"still": (png, "image/png")},
                        "video_mime": BROWSER_VIDEO_MIME.get(ext)}
            except Exception:
                pass
    if planned == KIND_ICON:
        _read_head(path, 1)
    if shell is not None:
        try:
            png = shell.thumbnail_png(path, limits["icon_px"], icon_only=True)
        except Exception:
            try:
                png = shell.type_icon_png(ext, limits["icon_px"])
            except Exception as exc:
                raise PreviewError(f"type icon unavailable: {exc}") from exc
        return {"kind": KIND_ICON, "variants": {"still": (png, "image/png")}}
    raise PreviewError("no preview available on this platform")


def _image_preview(path, size, fmt, head, animated, limits):
    max_px = limits["max_px"]
    if Image is not None:
        try:
            still, dims = pil_still_png(path, max_px, limits["max_source_pixels"])
            variants = {"still": (still, "image/png")}
            frames = 1
            if animated and size <= limits["max_animated_source_bytes"]:
                anim = pil_animation(path, max_px, limits["max_frames"],
                                     limits["max_source_pixels"])
                if anim:
                    variants["anim"] = (anim[0], anim[1])
                    frames = anim[2]
            kind = KIND_ANIMATED if "anim" in variants else KIND_IMAGE
            return {"kind": kind, "variants": variants, "width": dims[0],
                    "height": dims[1], "frames": frames}
        except Exception:
            # Pillow refused it (damaged, decompression bomb, unknown codec):
            # never hand such a file to the browser, the type icon follows.
            return None
    mime = _BROWSER_IMAGE_MIME.get(fmt)
    passthrough = limits["max_passthrough_bytes"]
    if mime and size <= passthrough:
        data = head if len(head) >= size else _read_head(path, passthrough + 1)
        if len(data) == size:
            variants = {"still": (data, mime)}
            if animated:
                variants["anim"] = (data, mime)
            return {"kind": KIND_ANIMATED if animated else KIND_IMAGE, "variants": variants}
    return None


# ── disk cache ───────────────────────────────────────────────────────
def cache_key(abspath, size, mtime_ns, max_px):
    raw = f"v{PREVIEW_VERSION}|{os.path.normcase(os.path.abspath(abspath))}|{int(size)}|{int(mtime_ns or 0)}|{int(max_px)}"
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


def icon_key(ext, px):
    return hashlib.sha256(f"v{PREVIEW_VERSION}|icon|{ext}|{int(px)}".encode("utf-8")).hexdigest()


_VARIANTS = ("still", "anim")


class PreviewCache:
    """Size-limited directory of generated previews.

    Each key has ``<key>.json`` (metadata, written last) and one file per
    variant. Reads refresh the modification time at most once an hour; the
    oldest keys go first when the limit is exceeded.
    """

    def __init__(self, root, max_bytes, max_age_s, clock=time.time):
        self.root = root
        self.max_bytes = max(1, int(max_bytes))
        self.max_age_s = max_age_s
        self.clock = clock
        self._lock = threading.RLock()
        self._total = None
        self._touched = {}

    def _path(self, key, suffix):
        if len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key):
            raise ValueError("invalid preview key")
        return os.path.join(self.root, f"{key}.{suffix}")

    def get(self, key):
        path = self._path(key, "json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict) or meta.get("version") != PREVIEW_VERSION:
            return None
        self._touch(key)
        return meta

    def variant_path(self, key, variant):
        if variant not in _VARIANTS:
            return None
        path = self._path(key, variant)
        return path if os.path.isfile(path) else None

    def _touch(self, key):
        now = self.clock()
        if now - self._touched.get(key, 0) < 3600:
            return
        self._touched[key] = now
        try:
            os.utime(self._path(key, "json"), (now, now))
        except OSError:
            pass

    def _write(self, path, data):
        tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)

    def put(self, key, meta, variants):
        """Store variant bytes plus metadata; returns the stored metadata."""
        os.makedirs(self.root, exist_ok=True)
        stored = dict(meta)
        stored["version"] = PREVIEW_VERSION
        stored["variants"] = {}
        added = 0
        with self._lock:
            for name, (data, mime) in variants.items():
                if name not in _VARIANTS:
                    continue
                self._write(self._path(key, name), data)
                stored["variants"][name] = {"mime": mime, "size": len(data)}
                added += len(data)
            body = json.dumps(stored, sort_keys=True).encode("utf-8")
            self._write(self._path(key, "json"), body)
            added += len(body)
            if self._total is not None:
                self._total += added
            if self.total_bytes() > self.max_bytes:
                self.enforce()
        return stored

    def _groups(self):
        groups = {}
        try:
            entries = list(os.scandir(self.root))
        except OSError:
            return groups
        now = self.clock()
        for entry in entries:
            name = entry.name
            key, _, suffix = name.partition(".")
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if ".tmp-" in name:
                if now - st.st_mtime > 600:       # leftovers of a crashed write
                    _unlink(entry.path)
                continue
            group = groups.setdefault(key, {"size": 0, "mtime": 0.0, "paths": []})
            group["size"] += int(st.st_size)
            group["paths"].append(entry.path)
            if suffix == "json":
                group["mtime"] = float(st.st_mtime)
        return groups

    def total_bytes(self):
        with self._lock:
            if self._total is None:
                self._total = sum(group["size"] for group in self._groups().values())
            return self._total

    def enforce(self):
        """Drop expired keys and, above the limit, the oldest keys."""
        with self._lock:
            groups = self._groups()
            now = self.clock()
            total = sum(group["size"] for group in groups.values())
            target = self.max_bytes * CACHE_CLEAN_TARGET if total > self.max_bytes else None
            removed = 0
            for key, group in sorted(groups.items(), key=lambda kv: kv[1]["mtime"]):
                expired = bool(self.max_age_s) and now - group["mtime"] > self.max_age_s
                if not expired and (target is None or total <= target):
                    break
                for path in group["paths"]:
                    _unlink(path)
                total -= group["size"]
                removed += 1
                self._touched.pop(key, None)
            self._total = total
            return removed

    def set_limit(self, max_bytes, max_age_s=None):
        max_bytes = max(1, int(max_bytes))
        if max_age_s is None:
            max_age_s = self.max_age_s
        if max_bytes != self.max_bytes or max_age_s != self.max_age_s:
            self.max_bytes = max_bytes
            self.max_age_s = max_age_s
            self.enforce()


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# ── service used by the web API ──────────────────────────────────────
def _item_file_sources(item):
    """(path, size, abspath|None, mtime_ns) per file, in file-card order."""
    manifest = item.get("batch_manifest") if isinstance(item, dict) else None
    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        return []
    local = {}
    for entry in item.get("files") or []:
        if isinstance(entry, dict) and entry.get("type", "file") == "file" \
                and isinstance(entry.get("rel"), str) and entry.get("abspath"):
            local[entry["rel"]] = entry
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type", "file") != "file":
            continue
        rel = entry.get("path")
        size = entry.get("size", 0)
        if not isinstance(rel, str) or not rel or not isinstance(size, int) \
                or isinstance(size, bool) or size < 0:
            continue
        source = local.get(rel)
        result.append({
            "path": rel,
            "size": size,
            "abspath": source.get("abspath") if source else None,
            "mtime_ns": int((source or {}).get("mtime_ns", 0) or 0),
        })
    return result


def preview_settings(clip_settings=None):
    """Preview settings and limits in working units.

    Values, defaults and ranges come normalised from the central clipboard
    configuration (``clipboard_model.clipboard_settings``); this only
    converts MB/KB/days/megapixels.
    """
    raw = clip_settings if isinstance(clip_settings, dict) else {}
    s = cbm.clipboard_settings({"clipboard": raw})
    return {
        "animation": s["file_preview_animation"],
        "video_hover": s["file_preview_video_hover"],
        "cache_bytes": s["file_preview_cache_mb"] * _MB,
        "cache_max_age_s": s["file_preview_cache_max_age_days"] * 86400,
        "max_px": s["file_preview_px"],
        "icon_px": s["file_preview_icon_px"],
        "max_count": s["file_preview_max_count"],
        "max_image_source_bytes": s["file_preview_max_image_source_mb"] * _MB,
        "max_animated_source_bytes": s["file_preview_max_animated_source_mb"] * _MB,
        "max_passthrough_bytes": s["file_preview_max_passthrough_mb"] * _MB,
        "max_source_pixels": s["file_preview_max_source_megapixels"] * 1_000_000,
        "max_frames": s["file_preview_max_frames"],
        "gif_scan_bytes": s["file_preview_gif_scan_kb"] * 1024,
        "miss_ttl_s": s["file_preview_miss_retry_s"],
        "response_max_bytes": s["file_preview_response_max_mb"] * _MB,
    }


class FilePreviewService:
    """Answers preview descriptors at once and generates in the background."""

    def __init__(self, root, settings_fn=None, log_fn=None, shell="auto",
                 clock=time.time, start_worker=True):
        self.settings_fn = settings_fn or (lambda: {})
        self.log_fn = log_fn
        self.clock = clock
        settings = self.settings()
        self.cache = PreviewCache(root, settings["cache_bytes"], settings["cache_max_age_s"],
                                  clock=clock)
        self._shell = _default_shell() if shell == "auto" else shell
        self._jobs = queue.Queue(maxsize=_QUEUE_MAX)
        self._pending = set()
        self._misses = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = None
        self._start_worker = start_worker
        self._swept = False

    def _clip_settings(self):
        try:
            return self.settings_fn() or {}
        except Exception:
            return {}

    def _log(self, level, message):
        if self.log_fn:
            try:
                self.log_fn(level, message)
            except Exception:
                pass

    def settings(self):
        return preview_settings(self._clip_settings())

    # descriptors -----------------------------------------------------
    def describe(self, item, count=None):
        """Preview state for the first ``count`` files of ``item`` (no waiting).

        ``count`` is capped at ``file_preview_max_count``; missing or invalid
        means that maximum.
        """
        settings = self.settings()
        try:
            count = max(1, min(int(count), settings["max_count"]))
        except (TypeError, ValueError):
            count = settings["max_count"]
        self.cache.set_limit(settings["cache_bytes"], settings["cache_max_age_s"])
        sources = _item_file_sources(item)
        rows = []
        for index, source in enumerate(sources[:count]):
            rows.append(self._describe_one(index, source, settings))
        return {"total_files": len(sources), "previews": rows,
                "animation": settings["animation"], "video_hover": settings["video_hover"]}

    def _row(self, index, source, state, kind, meta=None, key=None):
        ext = _ext(source["path"])
        variants = (meta or {}).get("variants") or {}
        return {
            "index": index,
            "ext": ext[1:] if ext else "",
            "state": state,
            "kind": kind,
            "key": key[:16] if key and variants else None,
            "still": "still" in variants,
            "anim": "anim" in variants,
            "video": bool(meta and meta.get("video_mime")),
        }

    def _describe_one(self, index, source, settings):
        abspath = source.get("abspath")
        retry_s = settings["miss_ttl_s"]
        if not abspath:
            # Remote item: no local file yet, only the type icon.
            if self._shell is None:
                return self._row(index, source, STATE_READY, KIND_NONE)
            ext = _ext(source["path"])
            key = icon_key(ext, settings["icon_px"])
            meta = self.cache.get(key)
            if meta:
                return self._row(index, source, STATE_READY, meta.get("kind", KIND_NONE), meta, key)
            miss = self._misses.get(key)
            if (miss and self.clock() - miss < retry_s) or not self._enqueue(key, ("icon", ext)):
                return self._row(index, source, STATE_READY, KIND_NONE)
            return self._row(index, source, STATE_PENDING, KIND_NONE)
        key = cache_key(abspath, source["size"], source["mtime_ns"], settings["max_px"])
        miss = self._misses.get(key)
        if miss and self.clock() - miss < retry_s:
            return self._row(index, source, STATE_MISSING, KIND_NONE)
        meta = self.cache.get(key)
        if meta:
            if meta.get("video_mime") and not settings["video_hover"]:
                meta = dict(meta, video_mime=None)
            return self._row(index, source, STATE_READY, meta.get("kind", KIND_NONE), meta, key)
        self._enqueue(key, ("file", abspath, source["size"], source["mtime_ns"]))
        return self._row(index, source, STATE_PENDING, KIND_NONE)

    # serving -----------------------------------------------------------
    def resolve(self, item, index, variant, key_prefix=None):
        """(path, mime) of a cached variant, or ``None``."""
        meta, key = self._meta_for(item, index)
        if not meta or (key_prefix and not key.startswith(str(key_prefix))):
            return None
        info = (meta.get("variants") or {}).get(variant)
        path = self.cache.variant_path(key, variant) if info else None
        return (path, info["mime"]) if path else None

    def video_source(self, item, index):
        """(abspath, mime, size) for the hover clip of a still-valid video."""
        settings = self.settings()
        if not settings["video_hover"]:
            return None
        source = self._source(item, index, settings)
        if not source or not source.get("abspath"):
            return None
        mime = BROWSER_VIDEO_MIME.get(_ext(source["path"]))
        if not mime or not source_matches(source["abspath"], source["size"], source["mtime_ns"]):
            return None
        return source["abspath"], mime, source["size"]

    def _source(self, item, index, settings):
        try:
            index = int(index)
        except (TypeError, ValueError):
            return None
        sources = _item_file_sources(item)
        if index < 0 or index >= min(len(sources), settings["max_count"]):
            return None
        return sources[index]

    def _meta_for(self, item, index):
        settings = self.settings()
        source = self._source(item, index, settings)
        if not source:
            return None, None
        if source.get("abspath"):
            key = cache_key(source["abspath"], source["size"], source["mtime_ns"],
                            settings["max_px"])
        else:
            key = icon_key(_ext(source["path"]), settings["icon_px"])
        return self.cache.get(key), key

    # background work ---------------------------------------------------
    def _enqueue(self, key, job):
        with self._lock:
            if key in self._pending:
                return True
            try:
                self._jobs.put_nowait((key, job))
            except queue.Full:
                return False
            self._pending.add(key)
            if self._start_worker and (self._worker is None or not self._worker.is_alive()):
                self._worker = threading.Thread(target=self._run, name="clipboard-file-preview",
                                                daemon=True)
                self._worker.start()
        return True

    def _run(self):
        if not self._swept:
            self._swept = True
            try:
                self.cache.enforce()
            except Exception as exc:
                self._log("DEBUG", f"clipboard preview cache sweep failed: {exc}")
        while not self._stop.is_set():
            try:
                key, job = self._jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.process(key, job)
            finally:
                with self._lock:
                    self._pending.discard(key)

    def run_pending(self):
        """Process every queued job on the calling thread (tests, shutdown)."""
        while True:
            try:
                key, job = self._jobs.get_nowait()
            except queue.Empty:
                return
            try:
                self.process(key, job)
            finally:
                with self._lock:
                    self._pending.discard(key)

    def process(self, key, job):
        started = self.clock()
        limits = self.settings()
        try:
            if job[0] == "icon":
                if self._shell is None:
                    return
                try:
                    png = self._shell.type_icon_png(job[1], limits["icon_px"])
                except Exception as exc:
                    raise PreviewError(f"type icon unavailable: {exc}") from exc
                self.cache.put(key, {"kind": KIND_ICON}, {"still": (png, "image/png")})
                return
            _kind, abspath, size, mtime_ns = job
            if not source_matches(abspath, size, mtime_ns):
                self._misses[key] = self.clock()
                self._log("DEBUG", "clipboard file preview source missing or changed")
                return
            result = generate(abspath, size, limits, shell=self._shell)
            meta = {k: result[k] for k in ("kind", "width", "height", "frames", "video_mime")
                    if result.get(k) is not None}
            self.cache.put(key, meta, result["variants"])
            self._misses.pop(key, None)
            self._log("DEBUG", f"clipboard file preview ready kind={meta['kind']} "
                               f"ms={int((self.clock() - started) * 1000)}")
        except PreviewError as exc:
            # Nothing to show (unsupported format, too large, no shell): the
            # neutral symbol is final for this file version, no retries.
            self.cache.put(key, {"kind": KIND_NONE}, {})
            self._log("DEBUG", f"clipboard file preview none: {exc}")
        except OSError as exc:
            # Locked or vanished between the check and the read.
            self._misses[key] = self.clock()
            self._log("DEBUG", f"clipboard file preview unreadable: {type(exc).__name__}")
        except Exception as exc:
            self._misses[key] = self.clock()
            self._log("DEBUG", f"clipboard file preview failed: {type(exc).__name__}: {exc}")

    def shutdown(self, timeout=1.0):
        self._stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout)
