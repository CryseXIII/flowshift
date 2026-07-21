"""Animated preview helpers for clipboard media.

Uses Pillow when available, but keeps imports optional so the GUI/runtime can
still start and fall back gracefully if the dependency is missing.
"""
from __future__ import annotations

import io

try:
    from PIL import Image, ImageSequence
except Exception:  # pragma: no cover - handled gracefully at runtime
    Image = None
    ImageSequence = None

_MIN_FRAME_MS = 30


def _resample_filter():
    if Image is None:
        return None
    resampling = getattr(Image, "Resampling", None)
    if resampling is not None:
        return getattr(resampling, "LANCZOS", getattr(resampling, "BICUBIC", 0))
    return getattr(Image, "LANCZOS", getattr(Image, "BICUBIC", 0))


def _frame_to_ppm(frame, max_px):
    img = frame.convert("RGBA")
    if max_px and max_px > 0:
        try:
            img.thumbnail((max_px, max_px), _resample_filter())
        except Exception:
            img.thumbnail((max_px, max_px))
    rgb = Image.new("RGB", img.size, (255, 255, 255))
    rgb.paste(img, mask=img.getchannel("A") if "A" in img.getbands() else None)
    return f"P6\n{rgb.width} {rgb.height}\n255\n".encode("ascii") + rgb.tobytes()


def _load_gif_frames(data, max_px, max_frames=60):
    if Image is None or ImageSequence is None or not data:
        return [], False

    try:
        with Image.open(io.BytesIO(data)) as im:
            total_frames = int(getattr(im, "n_frames", 1) or 1)
            truncated = total_frames > int(max_frames)
            frames = []
            for index, frame in enumerate(ImageSequence.Iterator(im)):
                if index >= int(max_frames):
                    break
                duration = int(frame.info.get("duration", im.info.get("duration", 100)) or 100)
                duration = max(_MIN_FRAME_MS, duration)
                frames.append({
                    "ppm": _frame_to_ppm(frame.copy(), max_px),
                    "duration_ms": duration,
                })
            return frames, truncated
    except Exception:
        return [], False


def gif_frames_to_ppm_frames(data: bytes, max_px: int, max_frames: int = 60):
    """Return a list of GIF frames as PPM bytes + durations."""
    frames, _ = _load_gif_frames(data, max_px, max_frames=max_frames)
    return frames


def gif_preview_package(data: bytes, max_px: int, max_frames: int = 60):
    """Return frames plus truncation state for control/API consumers."""
    frames, truncated = _load_gif_frames(data, max_px, max_frames=max_frames)
    return {"frames": frames, "truncated": truncated}
