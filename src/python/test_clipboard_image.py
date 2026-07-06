"""FlowShift clipboard image tests (pure, standard library, any OS)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_image as ci

_failures = []


def check(cond, label):
    if cond:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


# A 2x2 image: red, green / blue, white (top-down)
px = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
bmp = ci.make_synthetic_bmp(2, 2, px)
check(bmp is not None and bmp[:2] == b"BM", "synthetic BMP has BM signature")

info = ci.parse_bmp(bmp)
check(info["width"] == 2 and info["height"] == 2, "parse_bmp width/height")
check(info["bit_count"] == 24 and info["compression"] == 0, "parse_bmp 24-bit BI_RGB")


# ── DIB <-> BMP round-trip ──────────────────────────────────────────
dib = ci.bmp_to_dib(bmp)
check(dib is not None and dib[:2] != b"BM", "bmp_to_dib strips file header")
bmp2 = ci.dib_to_bmp(dib)
check(bmp2 == bmp, "dib_to_bmp restores the exact BMP (round-trip)")


# ── BMP -> PPM decode ───────────────────────────────────────────────
ppm = ci.bmp_to_ppm(bmp)
check(ppm is not None and ppm.startswith(b"P6\n2 2\n255\n"), "bmp_to_ppm P6 header 2x2")
body = ppm.split(b"255\n", 1)[1]
check(len(body) == 2 * 2 * 3, "ppm body size = w*h*3")
# top-left pixel is red
check(body[0:3] == bytes((255, 0, 0)), "ppm top-left pixel is red")
# bottom-right pixel is white
check(body[9:12] == bytes((255, 255, 255)), "ppm bottom-right pixel is white")


# ── downscale (thumbnail) ───────────────────────────────────────────
big = ci.make_synthetic_bmp(8, 8, [(1, 2, 3)] * 64)
thumb = ci.bmp_to_ppm(big, max_px=4)
check(thumb is not None and thumb.startswith(b"P6\n4 4\n"), "downscaled to 4x4 thumbnail")


# ── 32-bit BMP decode ───────────────────────────────────────────────
import struct
# Build a 1x1 32-bit BGRA BMP manually: blue pixel (B=0,G=0,R=255,A=255)
bi = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 32, 0, 4, 2835, 2835, 0, 0)
bgra = bytes((0, 0, 255, 255))
dib32 = bi + bgra
bmp32 = ci.dib_to_bmp(dib32)
ppm32 = ci.bmp_to_ppm(bmp32)
check(ppm32 is not None and ppm32.endswith(bytes((255, 0, 0))), "32-bit BMP decodes (red)")


# ── unsupported format -> None (no crash) ───────────────────────────
# Fake a compressed (BI_RGB->3) header.
bad = bytearray(bmp)
struct.pack_into("<I", bad, 30, 3)   # compression = BI_BITFIELDS
check(ci.bmp_to_ppm(bytes(bad)) is None, "unsupported compression -> None (placeholder)")
check(ci.bmp_to_ppm(b"not a bmp") is None, "garbage -> None")


print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
    sys.exit(1)
print("All clipboard image tests passed.")
