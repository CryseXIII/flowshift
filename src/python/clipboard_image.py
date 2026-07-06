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

No Windows APIs here; everything is unit-testable with synthetic BMP bytes.
"""
from __future__ import annotations

import struct

BI_RGB = 0
BI_BITFIELDS = 3


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
