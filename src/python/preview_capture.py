"""Remote preview, stage 2: screen capture and encoding, and the self-test.

Taking a picture of the chosen monitor (the one with the mouse by default),
optionally only the area around the mouse, scaled to ``max_width``, with the
mouse arrow drawn in, encoded as JPEG. ``StreamCapture`` is the picture source
of a running preview (``preview_stream.PreviewStreamer``) and encodes only
when the pixels changed. ``self_test`` checks every step on one PC for WebGUI
Settings > Remote Preview: capture, encoding, frame rate, transfer through the
link code (loopback), the preview window and the picture in the browser.

Pure module apart from the default grabber (Pillow ``ImageGrab``, a runtime
dependency already in ``requirements.txt``, the installer and the USB
package); every Windows call is injectable for tests.
"""
from __future__ import annotations

import base64
import hashlib
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
STEP_LOOPBACK = "loopback"
STEP_WINDOW = "window"
STEP_DISPLAY = "display"

STEP_LABELS = {
    STEP_SETTINGS: "Remote preview setting",
    STEP_MONITOR: "Monitor chosen",
    STEP_CAPTURE: "Screen capture",
    STEP_ENCODE: "Picture encoded (JPEG)",
    STEP_RATE: "Frame rate reachable",
    STEP_LOOPBACK: "Transfer through the connection (loopback on this PC)",
    STEP_WINDOW: "Preview window on this PC",
    STEP_DISPLAY: "Picture shown in the browser",
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


def _grab(rect, grab):
    try:
        image = grab(rect)
    except Exception as exc:
        raise CaptureError("capture_failed", f"screen capture failed: {exc.__class__.__name__}: "
                                             f"{str(exc)[:160]}") from exc
    if image is None or not getattr(image, "size", None):
        raise CaptureError("capture_failed", "screen capture returned no picture")
    return image


def _scale(image, max_width):
    try:
        src_w, src_h = image.size
        width, height = scaled_size(src_w, src_h, max_width)
        if (width, height) != (src_w, src_h):
            image = image.resize((width, height))
        if getattr(image, "mode", "RGB") not in ("RGB", "L"):
            image = image.convert("RGB")
        return image
    except Exception as exc:
        raise CaptureError("encode_failed", f"scaling the picture failed: "
                                            f"{exc.__class__.__name__}: {str(exc)[:160]}") from exc


def _encode(image, quality):
    try:
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=int(quality))
        data = buf.getvalue()
    except Exception as exc:
        raise CaptureError("encode_failed", f"encoding the picture failed: "
                                            f"{exc.__class__.__name__}: {str(exc)[:160]}") from exc
    if not data:
        raise CaptureError("encode_failed", "encoding the picture gave no data")
    return data


def capture_frame(rect, max_width, grab=default_grab, quality=JPEG_QUALITY,
                  clock=time.perf_counter):
    """Capture ``rect``, scale to ``max_width``, encode JPEG. Raises ``CaptureError``."""
    started = clock()
    image = _grab(rect, grab)
    captured = clock()
    src_w, src_h = image.size
    image = _scale(image, max_width)
    width, height = image.size
    data = _encode(image, quality)
    done = clock()
    return {
        "jpeg": data, "width": width, "height": height,
        "source_width": src_w, "source_height": src_h,
        "capture_ms": round((captured - started) * 1000, 1),
        "encode_ms": round((done - captured) * 1000, 1),
    }


def crop_rect(monitor, cursor, max_width):
    """Section of ``monitor`` around ``cursor`` for "Area around the mouse".

    ``max_width`` wide and 16:9 high (never larger than the monitor), centred
    on the mouse and pushed inside the monitor at its borders. Without a mouse
    position on this monitor the whole monitor is returned.
    """
    width = min(monitor["width"], int(max_width))
    height = min(monitor["height"], max(1, round(int(max_width) * 9 / 16)))
    if not cursor:
        return dict(monitor)
    cx, cy = cursor
    left = min(max(cx - width // 2, monitor["left"]), monitor["left"] + monitor["width"] - width)
    top = min(max(cy - height // 2, monitor["top"]), monitor["top"] + monitor["height"] - height)
    return {"left": int(left), "top": int(top), "width": int(width), "height": int(height)}


def draw_cursor(image, x, y):
    """Small white arrow with a black edge at ``(x, y)``: Windows leaves the mouse out."""
    from PIL import ImageDraw  # noqa: PLC0415 - Pillow, runtime dependency
    arrow = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
    ImageDraw.Draw(image).polygon([(x + dx, y + dy) for dx, dy in arrow],
                                  fill=(255, 255, 255), outline=(0, 0, 0))


def pixel_key(image):
    """Fingerprint of the scaled pixels: same key, same picture (sent only on change)."""
    return hashlib.blake2b(image.tobytes(), digest_size=16).digest() + repr(image.size).encode()


class StreamCapture:
    """Picture source of a preview session on the target (``preview_stream.PreviewStreamer``).

    Every call follows the mouse to its monitor (setting ``cursor``), cuts the
    area around the mouse if ``cursor_crop`` is on, scales to ``max_width``,
    draws the mouse arrow, and encodes JPEG only when the pixels differ from
    ``previous_key``. A failed grab while ``desktop_locked()`` is true (sign-in
    or lock screen) raises ``CaptureError("screen_locked")``. The lock is only
    asked after a failure, so a wrong answer of that check can never stop a
    working capture.
    """

    def __init__(self, settings, monitors, cursor_pos, monitor_at, grab=default_grab,
                 desktop_locked=None, cursor=draw_cursor, quality=JPEG_QUALITY,
                 monitors_ttl=5.0, clock=time.monotonic):
        self.monitor = settings.get("monitor", "cursor")
        self.max_width = int(settings.get("max_width") or 960)
        self.cursor_crop = settings.get("cursor_crop") is True
        self.monitors = monitors
        self.cursor_pos = cursor_pos
        self.monitor_at = monitor_at
        self.grab = grab
        self.desktop_locked = desktop_locked
        self.cursor = cursor
        self.quality = quality
        self.monitors_ttl = monitors_ttl
        self.clock = clock
        self._monitors = None
        self._monitors_at = 0.0

    def _monitor_list(self):
        now = self.clock()
        if self._monitors is None or now - self._monitors_at > self.monitors_ttl:
            self._monitors = self.monitors() or []
            self._monitors_at = now
        return self._monitors

    def _locked(self):
        try:
            return bool(self.desktop_locked and self.desktop_locked())
        except Exception:
            return False

    def __call__(self, previous_key=None):
        try:
            pos = self.cursor_pos()
        except Exception:
            pos = None
        cursor_rect = None
        if pos:
            try:
                cursor_rect = self.monitor_at(*pos)
            except Exception:
                cursor_rect = None
        rect, label = choose_monitor(self.monitor, self._monitor_list(), cursor_rect)
        if rect is None:
            raise CaptureError("capture_failed", label)
        inside = bool(pos) and (rect["left"] <= pos[0] < rect["left"] + rect["width"]
                                and rect["top"] <= pos[1] < rect["top"] + rect["height"])
        if self.cursor_crop:
            rect = crop_rect(rect, pos if inside else None, self.max_width)
        try:
            image = _grab(rect, self.grab)
        except CaptureError:
            if self._locked():
                raise CaptureError("screen_locked", "the screen is locked") from None
            raise
        src_w, src_h = image.size
        image = _scale(image, self.max_width)
        width, height = image.size
        if inside and self.cursor:
            try:
                self.cursor(image, round((pos[0] - rect["left"]) * width / src_w),
                            round((pos[1] - rect["top"]) * height / src_h))
            except Exception:
                pass   # a picture without the arrow is still a picture
        key = pixel_key(image)
        if previous_key is not None and key == previous_key:
            return None
        return {"key": key, "jpeg": _encode(image, self.quality), "width": width, "height": height}


def _step(steps, key, ok, detail):
    steps.append({"step": key, "label": STEP_LABELS[key], "ok": ok, "detail": detail})


def self_test(settings, monitors, cursor_rect=None, grab=default_grab, frames=SELF_TEST_FRAMES,
              clock=time.perf_counter, log=None, loopback=None, show_window=None):
    """Check every step of the preview on this PC, without a second PC.

    Steps: setting, monitor, capture, encoding, frame rate, transfer through
    the link code (``loopback(jpeg, width, height) -> (ok, detail, ms)``:
    ``preview_stream.loopback_transfer`` over a local socket pair), the
    preview window (``show_window(jpeg, width, height) -> (ok, detail)``:
    opens the real window for a moment) and the picture in the browser (the
    WebGUI marks that one OK only once the image loaded). Returns ``{"ok",
    "steps": [{step, label, ok, detail}], "image", "width", "height",
    "fps_possible", "message"}``; ``ok`` is true when capture, encoding,
    transfer and window work. Every step is also handed to ``log``.
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
    mbit = len(last["jpeg"]) * 8 * fps / 1_000_000
    _step(steps, STEP_RATE, possible >= fps,
          f"about {possible:g} pictures per second possible, {fps} set"
          + ("" if possible >= fps else "; lower the frame rate or the width")
          + f"; a screen that changes all the time needs about {mbit:.1f} Mbit/s "
            "(an unchanged screen sends nothing)")
    note("INFO", f"remote_preview self-test capture ok {last['source_width']}x{last['source_height']} "
                 f"-> {last['width']}x{last['height']} jpeg={len(last['jpeg'])}B "
                 f"capture_ms={capture_ms:.0f} encode_ms={encode_ms:.0f} fps_possible={possible:g}")
    chain_ok = True
    for key, runner, missing in (
            (STEP_LOOPBACK, loopback, "the transfer code is not available in this runtime"),
            (STEP_WINDOW, show_window, "no preview window on this system (Windows only)")):
        if runner is None:
            ok, detail = False, missing
        else:
            try:
                answer = runner(last["jpeg"], last["width"], last["height"])
                ok, detail = bool(answer[0]), str(answer[1])
            except Exception as exc:
                ok, detail = False, f"{exc.__class__.__name__}: {str(exc)[:160]}"
        _step(steps, key, ok, detail)
        chain_ok = chain_ok and ok
        note("INFO" if ok else "WARN",
             f"remote_preview self-test {key} {'ok' if ok else 'failed'}: {detail}")
    _step(steps, STEP_DISPLAY, True,
          "the picture below comes from this capture; if you see your screen, showing works")
    result.update(
        ok=chain_ok,
        image="data:image/jpeg;base64," + base64.b64encode(last["jpeg"]).decode("ascii"),
        width=last["width"], height=last["height"], fps_possible=possible,
        message=("Capture, encoding, transfer and the preview window work on this PC. "
                 "Switch the setting on on both PCs to see the other PC." if chain_ok else
                 "Capture works, but not every step: see the red line below."),
    )
    return result
