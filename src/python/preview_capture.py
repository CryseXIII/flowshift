"""Remote preview: screen capture and encoding on this PC, and its self-test.

Viktor 27.11.: the preview of the other PC "never worked", and
``flowshift.log`` of BOOK-Viktor had no single line about it. The reason is in
``remote_preview``: only stage 1 exists (settings, messages, refusal); the
target answers every request with ``preview_unavailable`` because there was no
capture, no image channel and no window. This module is stage 2's first part:
taking one frame of the chosen monitor, scaled to ``max_width`` and encoded as
JPEG, with timings. The self-test on WebGUI Settings > Remote Preview uses it
to show, without a second PC, that capture works here and that the picture can
be shown (the browser displays the frame it returns).

Pure module apart from the default grabber (Pillow ``ImageGrab``, a runtime
dependency already); every Windows call is injectable for tests.
"""
from __future__ import annotations

import base64
import io
import time

JPEG_QUALITY = 70
SELF_TEST_FRAMES = 3
MAX_SELF_TEST_FRAMES = 10

STEP_SETTINGS = "settings"
STEP_MONITOR = "monitor"
STEP_CAPTURE = "capture"
STEP_ENCODE = "encode"
STEP_RATE = "rate"
STEP_DISPLAY = "display"
STEP_STREAM = "stream"

STEP_LABELS = {
    STEP_SETTINGS: "Remote preview setting",
    STEP_MONITOR: "Monitor chosen",
    STEP_CAPTURE: "Screen capture",
    STEP_ENCODE: "Picture encoded (JPEG)",
    STEP_RATE: "Frame rate reachable",
    STEP_DISPLAY: "Picture shown on this PC",
    STEP_STREAM: "Transfer to the other PC and preview window",
}


class CaptureError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _rect_ok(rect):
    return (isinstance(rect, dict)
            and all(isinstance(rect.get(k), int) and not isinstance(rect.get(k), bool)
                    for k in ("left", "top", "width", "height"))
            and rect["width"] > 0 and rect["height"] > 0)


def choose_monitor(setting, monitors, cursor_rect=None):
    """``(rect, label)`` of the monitor ``setting`` names, or ``(None, reason)``.

    ``monitors`` is the list of monitor rectangles (``left/top/width/height``,
    optional ``primary``); ``cursor_rect`` the monitor under the mouse.
    """
    monitors = [m for m in monitors or () if _rect_ok(m)]
    if setting == "cursor":
        if _rect_ok(cursor_rect):
            return dict(cursor_rect), "screen with the mouse"
        setting = "primary"
    if setting == "primary":
        for m in monitors:
            if m.get("primary") or (m["left"] <= 0 < m["left"] + m["width"]
                                    and m["top"] <= 0 < m["top"] + m["height"]):
                return dict(m), "main screen"
        if monitors:
            return dict(monitors[0]), "first screen"
        return None, "no monitor found"
    if isinstance(setting, int) and not isinstance(setting, bool):
        if 0 <= setting < len(monitors):
            return dict(monitors[setting]), f"screen number {setting}"
        return None, (f"screen number {setting} does not exist here "
                      f"(this PC has {len(monitors)} screen(s), numbers 0 to {max(0, len(monitors) - 1)})")
    return None, f"unknown monitor setting {setting!r}"


def scaled_size(width, height, max_width):
    """Size after scaling down to ``max_width`` (never up), aspect kept, at least 1 px."""
    if width <= max_width:
        return int(width), int(height)
    return int(max_width), max(1, round(height * max_width / width))


def default_grab(rect):
    """One picture of ``rect`` (virtual-screen pixels) with Pillow."""
    from PIL import ImageGrab  # noqa: PLC0415 - Windows runtime only
    box = (rect["left"], rect["top"], rect["left"] + rect["width"], rect["top"] + rect["height"])
    return ImageGrab.grab(bbox=box, all_screens=True)


def capture_frame(rect, max_width, grab=default_grab, quality=JPEG_QUALITY,
                  clock=time.perf_counter):
    """Capture ``rect``, scale to ``max_width``, encode JPEG. Raises ``CaptureError``."""
    started = clock()
    try:
        image = grab(rect)
    except Exception as exc:
        raise CaptureError("capture_failed", f"screen capture failed: {exc.__class__.__name__}: "
                                             f"{str(exc)[:160]}") from exc
    if image is None or not getattr(image, "size", None):
        raise CaptureError("capture_failed", "screen capture returned no picture")
    captured = clock()
    try:
        src_w, src_h = image.size
        width, height = scaled_size(src_w, src_h, max_width)
        if (width, height) != (src_w, src_h):
            image = image.resize((width, height))
        if getattr(image, "mode", "RGB") not in ("RGB", "L"):
            image = image.convert("RGB")
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=int(quality))
        data = buf.getvalue()
    except Exception as exc:
        raise CaptureError("encode_failed", f"encoding the picture failed: "
                                            f"{exc.__class__.__name__}: {str(exc)[:160]}") from exc
    if not data:
        raise CaptureError("encode_failed", "encoding the picture gave no data")
    done = clock()
    return {
        "jpeg": data, "width": width, "height": height,
        "source_width": src_w, "source_height": src_h,
        "capture_ms": round((captured - started) * 1000, 1),
        "encode_ms": round((done - captured) * 1000, 1),
    }


def _step(steps, key, ok, detail):
    steps.append({"step": key, "label": STEP_LABELS[key], "ok": ok, "detail": detail})


def self_test(settings, monitors, cursor_rect=None, grab=default_grab, frames=SELF_TEST_FRAMES,
              clock=time.perf_counter, log=None):
    """Capture and show test on this PC, without a second PC.

    Returns ``{"ok", "steps": [{step, label, ok, detail}], "image", "width",
    "height", "fps_possible", "message"}``; ``image`` is a ``data:image/jpeg``
    URL of the last frame for the browser to show. ``ok`` says whether capture
    and encoding work here; the step ``stream`` always says that transfer and
    window are not built yet. Every step is also handed to ``log``.
    """
    def note(level, text):
        if log:
            try:
                log(level, text)
            except Exception:
                pass

    steps = []
    settings = settings if isinstance(settings, dict) else {}
    fps = int(settings.get("fps") or 15)
    max_width = int(settings.get("max_width") or 960)
    enabled = settings.get("enabled") is True
    _step(steps, STEP_SETTINGS, True,
          ("on" if enabled else "off (the self-test runs anyway; switch it on to ask the other PC)")
          + f", {fps} fps, at most {max_width} px wide")
    note("INFO", f"remote_preview self-test started enabled={enabled} fps={fps} max_width={max_width} "
                 f"monitor={settings.get('monitor', 'cursor')}")
    rect, label = choose_monitor(settings.get("monitor", "cursor"), monitors, cursor_rect)
    result = {"ok": False, "steps": steps, "image": None, "width": 0, "height": 0,
              "fps_possible": 0.0, "message": ""}
    if rect is None:
        _step(steps, STEP_MONITOR, False, label)
        result["message"] = f"No picture: {label}."
        note("WARN", f"remote_preview self-test failed at monitor: {label}")
        return result
    _step(steps, STEP_MONITOR, True,
          f"{label}: {rect['width']}x{rect['height']} at ({rect['left']}, {rect['top']})")
    frames = max(1, min(int(frames or 1), MAX_SELF_TEST_FRAMES))
    shots = []
    for _ in range(frames):
        try:
            shots.append(capture_frame(rect, max_width, grab=grab, clock=clock))
        except CaptureError as exc:
            key = STEP_CAPTURE if exc.code == "capture_failed" else STEP_ENCODE
            if key == STEP_ENCODE:
                _step(steps, STEP_CAPTURE, True, "picture taken")
            _step(steps, key, False, str(exc))
            result["message"] = f"No picture: {exc}."
            note("WARN", f"remote_preview self-test failed at {key}: {exc}")
            return result
    last = shots[-1]
    capture_ms = sum(s["capture_ms"] for s in shots) / len(shots)
    encode_ms = sum(s["encode_ms"] for s in shots) / len(shots)
    _step(steps, STEP_CAPTURE, True,
          f"{last['source_width']}x{last['source_height']} in {capture_ms:.0f} ms (average of {len(shots)})")
    _step(steps, STEP_ENCODE, True,
          f"{last['width']}x{last['height']}, {len(last['jpeg']) // 1024} KB in {encode_ms:.0f} ms")
    per_frame = max(0.001, (capture_ms + encode_ms) / 1000.0)
    possible = round(1.0 / per_frame, 1)
    _step(steps, STEP_RATE, possible >= fps,
          f"about {possible:g} pictures per second possible, {fps} set"
          + ("" if possible >= fps else "; lower the frame rate or the width"))
    _step(steps, STEP_DISPLAY, True,
          "the picture below comes from this capture; if you see your screen, showing works")
    _step(steps, STEP_STREAM, False,
          "not built yet in this version: the other PC answers 'preview_unavailable', "
          "no picture is sent and no preview window opens")
    note("INFO", f"remote_preview self-test capture ok {last['source_width']}x{last['source_height']} "
                 f"-> {last['width']}x{last['height']} jpeg={len(last['jpeg'])}B "
                 f"capture_ms={capture_ms:.0f} encode_ms={encode_ms:.0f} fps_possible={possible:g}")
    note("INFO", "remote_preview self-test: transfer and preview window are not built yet "
                 "(the other PC refuses with preview_unavailable)")
    result.update(
        ok=True,
        image="data:image/jpeg;base64," + base64.b64encode(last["jpeg"]).decode("ascii"),
        width=last["width"], height=last["height"], fps_possible=possible,
        message="Capture and display work on this PC. Sending the picture to the other PC "
                "and the preview window are not built yet.",
    )
    return result
