"""FlowShift clipboard image support (pure, standard-library only, testable).

Windows delivers clipboard images as ``CF_DIB`` (a BITMAPINFOHEADER + optional
color table + pixel data — i.e. a BMP without the 14-byte file header). We store
and transfer images as a normal **BMP** blob (lossless, trivial to round-trip):

  * ``dib_to_bmp`` / ``bmp_to_dib`` — add/strip the 14-byte BITMAPFILEHEADER.
  * ``parse_bmp`` — width/height/bit-count/compression/pixel-offset.
  * ``bmp_to_ppm`` — decode an uncompressed 24/32-bit BMP into a P6 PPM (which
    Tk's PhotoImage can display), with optional nearest-neighbour downscale for a
    thumbnail. Returns ``None`` for formats we cannot decode (a placeholder icon
    is shown instead — never a crash).

Browsers and many programs put the picture on the clipboard more than once:
``CF_DIB``/``CF_DIBV5`` (``CF_DIBV5`` may carry an alpha channel) and the
registered ``PNG`` format, which is the only one that keeps transparency
reliably. A clipboard image item therefore stores, in this order of
preference (Viktor 24.11.2026):

  * the ``PNG`` bytes when the clipboard has them (mime ``image/png``);
  * otherwise the DIB the source program placed first (``CF_DIBV5`` keeps its
    124-byte header with the alpha mask, ``CF_DIB`` stays as it was), as BMP.

``image_format`` / ``image_size`` tell the stored blob apart, ``trim_png``
drops allocation padding behind ``IEND``, ``bmp_thumbnail_rgba`` decodes a
BMP into a small RGBA thumbnail without Pillow (sampling only the output
pixels, so a huge screenshot costs the same as a small one), and
``paste_formats`` lists what goes back on the clipboard when an item is set
again (``PNG`` plus DIBs, so painting programs can paste it).

No Windows APIs here; everything is unit-testable with synthetic BMP bytes.
Pillow is only imported lazily inside ``paste_formats`` for PNG items.
"""
from __future__ import annotations

import io
import struct

BI_RGB = 0
BI_BITFIELDS = 3
BI_ALPHABITFIELDS = 6
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
BITMAPV5HEADER_SIZE = 124
_LCS_SRGB = 0x73524742
_LCS_GM_IMAGES = 4
# PNG items whose pixels exceed this get no DIB when set again (only PNG).
PASTE_MAX_PIXELS = 100_000_000


def dib_to_bmp(dib):
    """Prepend a BITMAPFILEHEADER to a CF_DIB byte string -> a full BMP."""
    if len(dib) < 40:
        return None
    bi_size = struct.unpack_from("<I", dib, 0)[0]
    bit_count = struct.unpack_from("<H", dib, 14)[0]
    compression = struct.unpack_from("<I", dib, 16)[0]
    clr_used = struct.unpack_from("<I", dib, 32)[0]

    color_table = 0
    if bit_count <= 8:
        entries = clr_used if clr_used else (1 << bit_count)
        color_table = entries * 4
    elif compression == BI_BITFIELDS and bi_size == 40:
        color_table = 12   # 3 DWORD channel masks follow a v3 header
    elif compression == BI_ALPHABITFIELDS and bi_size == 40:
        color_table = 16   # 4 DWORD masks (with alpha) follow a v3 header

    off_bits = 14 + bi_size + color_table
    file_size = 14 + len(dib)
    header = b"BM" + struct.pack("<IHHI", file_size, 0, 0, off_bits)
    return header + dib


def bmp_to_dib(bmp):
    """Strip the 14-byte BITMAPFILEHEADER from a BMP -> a CF_DIB byte string."""
    if len(bmp) < 14 or bmp[:2] != b"BM":
        return None
    return bmp[14:]


def parse_bmp(bmp):
    """Return dict(width,height,bit_count,compression,off_bits,top_down) or None."""
    if len(bmp) < 54 or bmp[:2] != b"BM":
        return None
    off_bits = struct.unpack_from("<I", bmp, 10)[0]
    bi_size = struct.unpack_from("<I", bmp, 14)[0]
    width = struct.unpack_from("<i", bmp, 18)[0]
    height = struct.unpack_from("<i", bmp, 22)[0]
    bit_count = struct.unpack_from("<H", bmp, 28)[0]
    compression = struct.unpack_from("<I", bmp, 30)[0]
    top_down = height < 0
    return {"width": abs(width), "height": abs(height), "bit_count": bit_count,
            "compression": compression, "off_bits": off_bits, "bi_size": bi_size,
            "top_down": top_down}


def bmp_to_ppm(bmp, max_px=None):
    """Decode an uncompressed 24/32-bit BMP to a P6 PPM (bytes), optional downscale.

    Returns None for unsupported formats (compressed / paletted) — callers show a
    placeholder instead of crashing.
    """
    info = parse_bmp(bmp)
    if not info:
        return None
    if info["compression"] != BI_RGB or info["bit_count"] not in (24, 32):
        return None
    w, h = info["width"], info["height"]
    if w <= 0 or h <= 0:
        return None
    bpp = info["bit_count"] // 8
    row_size = ((info["bit_count"] * w + 31) // 32) * 4   # padded to 4 bytes
    off = info["off_bits"]
    needed = off + row_size * h
    if len(bmp) < needed:
        return None

    # Extract RGB rows top-down (BMP is bottom-up unless height<0).
    rows = []  # list of bytearray of RGB triples, top row first
    for y in range(h):
        src_y = y if info["top_down"] else (h - 1 - y)
        base = off + src_y * row_size
        rgb = bytearray(w * 3)
        for x in range(w):
            px = base + x * bpp
            b = bmp[px]; g = bmp[px + 1]; r = bmp[px + 2]
            o = x * 3
            rgb[o] = r; rgb[o + 1] = g; rgb[o + 2] = b
        rows.append(rgb)

    # Optional nearest-neighbour downscale so a big image becomes a thumbnail.
    if max_px and (w > max_px or h > max_px):
        scale = max(w, h) / float(max_px)
        nw = max(1, int(w / scale))
        nh = max(1, int(h / scale))
        out_rows = []
        for ny in range(nh):
            sy = min(h - 1, int(ny * scale))
            src = rows[sy]
            line = bytearray(nw * 3)
            for nx in range(nw):
                sx = min(w - 1, int(nx * scale))
                so = sx * 3
                do = nx * 3
                line[do] = src[so]; line[do + 1] = src[so + 1]; line[do + 2] = src[so + 2]
            out_rows.append(line)
        rows = out_rows
        w, h = nw, nh

    body = b"".join(rows)
    return (f"P6\n{w} {h}\n255\n".encode("ascii")) + bytes(body)


def image_format(data):
    """``"png"`` or ``"bmp"`` for a stored clipboard image blob, else ``""``."""
    data = bytes(data[:8] if data else b"")
    if data.startswith(PNG_SIGNATURE):
        return "png"
    if data.startswith(b"BM"):
        return "bmp"
    return ""


def png_size(png):
    """(width, height) from the IHDR chunk, or None."""
    if len(png or b"") < 24 or not png.startswith(PNG_SIGNATURE) or png[12:16] != b"IHDR":
        return None
    width, height = struct.unpack_from(">II", png, 16)
    if width <= 0 or height <= 0:
        return None
    return width, height


def image_size(data):
    """(width, height) of a stored PNG or BMP blob, or (0, 0)."""
    fmt = image_format(data)
    if fmt == "png":
        return png_size(data) or (0, 0)
    if fmt == "bmp":
        info = parse_bmp(data)
        if info:
            return info["width"], info["height"]
    return 0, 0


def trim_png(data, max_chunks=1_000_000):
    """PNG bytes up to and including ``IEND``, or None if the chunks break.

    ``GlobalSize`` of a clipboard handle may be larger than the PNG that was
    written; the padding behind ``IEND`` is not part of the picture.
    """
    data = bytes(data or b"")
    if not data.startswith(PNG_SIGNATURE):
        return None
    pos = len(PNG_SIGNATURE)
    for _ in range(max_chunks):
        if pos + 12 > len(data):
            return None
        length = struct.unpack_from(">I", data, pos)[0]
        kind = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > len(data):
            return None
        if kind == b"IEND":
            return data[:end]
        pos = end
    return None


def _masks(bmp, info):
    """(r, g, b, a) channel masks of a 16/32-bit BMP."""
    if info["compression"] in (BI_BITFIELDS, BI_ALPHABITFIELDS):
        # Masks sit right behind the 40-byte core header, inside V4/V5
        # headers as well as directly after a v3 header.
        if len(bmp) < 66:
            return None
        red, green, blue = struct.unpack_from("<III", bmp, 54)
        alpha = 0
        if (info["bi_size"] >= 56 or info["compression"] == BI_ALPHABITFIELDS) and len(bmp) >= 70:
            alpha = struct.unpack_from("<I", bmp, 66)[0]
        return red, green, blue, alpha
    if info["bit_count"] == 32:
        return 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000
    return None


def _channel(value, mask):
    if not mask:
        return 0
    shift = (mask & -mask).bit_length() - 1
    top = mask >> shift
    return ((value & mask) >> shift) * 255 // top if top else 0


def bmp_thumbnail_rgba(bmp, max_px):
    """Decode a BMP into a thumbnail: (width, height, RGBA bytes) or None.

    Handles uncompressed 1/4/8/24/32-bit and 32-bit bit-field images (also
    with a ``BITMAPV5HEADER`` alpha mask). Only the output pixels are read
    from the source (nearest neighbour), so the cost does not grow with the
    source size. A 32-bit image whose alpha bytes are all zero is opaque
    (``CF_DIB`` leaves that byte unused).
    """
    info = parse_bmp(bmp)
    if not info:
        return None
    width, height, bits = info["width"], info["height"], info["bit_count"]
    if width <= 0 or height <= 0 or bits not in (1, 4, 8, 24, 32):
        return None
    compression = info["compression"]
    if compression not in (BI_RGB, BI_BITFIELDS, BI_ALPHABITFIELDS):
        return None
    if compression != BI_RGB and bits != 32:
        return None
    row_size = ((bits * width + 31) // 32) * 4
    off = info["off_bits"]
    if off <= 0 or len(bmp) < off + row_size * height:
        return None
    palette = None
    masks = None
    if bits <= 8:
        clr_used = struct.unpack_from("<I", bmp, 46)[0]
        entries = clr_used or (1 << bits)
        start = 14 + info["bi_size"]
        if start + entries * 4 > off:
            return None
        palette = [bmp[start + i * 4:start + i * 4 + 3] for i in range(entries)]
    elif bits == 32:
        masks = _masks(bmp, info)
        if not masks:
            return None
    max_px = max(1, int(max_px))
    scale = max(width, height) / float(max_px)
    if scale > 1:
        out_w = max(1, int(round(width / scale)))
        out_h = max(1, int(round(height / scale)))
    else:
        out_w, out_h = width, height
    out = bytearray(out_w * out_h * 4)
    any_alpha = False
    for oy in range(out_h):
        sy = min(height - 1, int((oy + 0.5) * height / out_h))
        row = off + (sy if info["top_down"] else height - 1 - sy) * row_size
        for ox in range(out_w):
            sx = min(width - 1, int((ox + 0.5) * width / out_w))
            o = (oy * out_w + ox) * 4
            if bits == 24:
                px = row + sx * 3
                out[o], out[o + 1], out[o + 2], out[o + 3] = bmp[px + 2], bmp[px + 1], bmp[px], 255
            elif bits == 32:
                value = struct.unpack_from("<I", bmp, row + sx * 4)[0]
                alpha = _channel(value, masks[3]) if masks[3] else 255
                out[o] = _channel(value, masks[0])
                out[o + 1] = _channel(value, masks[1])
                out[o + 2] = _channel(value, masks[2])
                out[o + 3] = alpha
                any_alpha = any_alpha or alpha != 0
            else:
                bit = sx * bits
                byte = bmp[row + bit // 8]
                index = (byte >> (8 - bits - bit % 8)) & ((1 << bits) - 1)
                bgr = palette[index] if index < len(palette) else b"\x00\x00\x00"
                out[o], out[o + 1], out[o + 2], out[o + 3] = bgr[2], bgr[1], bgr[0], 255
    if bits == 32 and not any_alpha:
        out[3::4] = b"\xff" * (out_w * out_h)
    return out_w, out_h, bytes(out)


def rgba_to_dibv5(width, height, bgra_bottom_up):
    """``CF_DIBV5`` bytes: BITMAPV5HEADER, 32-bit with alpha mask, bottom-up."""
    width, height = int(width), int(height)
    if width <= 0 or height <= 0 or len(bgra_bottom_up) != width * height * 4:
        raise ValueError("invalid BGRA buffer")
    header = struct.pack(
        "<IiiHHIIiiIIIIIII36sIIIIIII",
        BITMAPV5HEADER_SIZE, width, height, 1, 32, BI_BITFIELDS, len(bgra_bottom_up),
        2835, 2835, 0, 0,
        0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000,
        _LCS_SRGB, b"\x00" * 36, 0, 0, 0, _LCS_GM_IMAGES, 0, 0, 0)
    return header + bytes(bgra_bottom_up)


def _png_dibs(png, max_pixels):
    """(CF_DIBV5, CF_DIB) for a PNG via Pillow, or None without Pillow."""
    try:
        from PIL import Image
    except Exception:
        return None
    with Image.open(io.BytesIO(png)) as img:
        width, height = img.size
        if width <= 0 or height <= 0 or width * height > max_pixels:
            return None
        img.seek(0)
        rgba = img.convert("RGBA")
    dibv5 = rgba_to_dibv5(width, height, rgba.tobytes("raw", "BGRA", 0, -1))
    # CF_DIB without alpha: transparent parts become white, not black, in
    # programs that read only the plain bitmap.
    flat = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    flat.alpha_composite(rgba)
    out = io.BytesIO()
    flat.convert("RGB").save(out, format="BMP")
    return dibv5, bmp_to_dib(out.getvalue())


def paste_formats(data, max_pixels=PASTE_MAX_PIXELS):
    """Clipboard formats for setting a stored image again.

    Returns a list of ``(name, bytes)`` with name ``"PNG"``, ``"DIBV5"`` or
    ``"DIB"`` in the order they go on the clipboard. PNG items keep their
    exact bytes under ``PNG`` (transparency) and get both DIBs when Pillow is
    available; BMP items go back as the DIB they came from.
    """
    fmt = image_format(data)
    if fmt == "png":
        formats = [("PNG", bytes(data))]
        try:
            dibs = _png_dibs(data, max_pixels)
        except Exception:
            dibs = None
        if dibs:
            formats += [("DIBV5", dibs[0]), ("DIB", dibs[1])]
        return formats
    if fmt == "bmp":
        dib = bmp_to_dib(data)
        info = parse_bmp(data)
        if not dib or not info:
            return []
        return [("DIBV5" if info["bi_size"] >= BITMAPV5HEADER_SIZE else "DIB", dib)]
    return []


def make_synthetic_bmp(width, height, pixels_rgb):
    """Build a 24-bit BI_RGB BMP from top-down RGB pixels (for tests/tools).

    ``pixels_rgb`` is a flat list of (r,g,b) tuples, row-major top-down.
    """
    bi_size = 40
    row_size = ((24 * width + 31) // 32) * 4
    pad = row_size - width * 3
    pixel_data = bytearray()
    # BMP stores bottom-up.
    for y in range(height - 1, -1, -1):
        for x in range(width):
            r, g, b = pixels_rgb[y * width + x]
            pixel_data += bytes((b, g, r))
        pixel_data += b"\x00" * pad
    dib = struct.pack("<IiiHHIIiiII", bi_size, width, height, 1, 24, BI_RGB,
                      len(pixel_data), 2835, 2835, 0, 0) + bytes(pixel_data)
    return dib_to_bmp(dib)
