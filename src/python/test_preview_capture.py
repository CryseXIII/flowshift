"""Remote preview self-test: capture, encode and show one frame on this PC.

The grabber returns a stand-in picture made in the test (no screen needed).
With Pillow installed (the runtime has it) one more test runs the real JPEG
encoder and decodes the returned data URL again.
"""
from __future__ import annotations

import base64
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import preview_capture as pc

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is a runtime dependency
    Image = None

MONITORS = [{"left": 0, "top": 0, "width": 1920, "height": 1080, "primary": True},
            {"left": -2560, "top": 0, "width": 2560, "height": 1440, "primary": False},
            {"left": 0, "top": -1080, "width": 1920, "height": 1080, "primary": False}]
SETTINGS = {"enabled": True, "fps": 15, "max_width": 960, "monitor": "cursor"}


class _Clock:
    def __init__(self, step):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


class _FakeImage:
    """Enough of a Pillow image for ``capture_frame``: size, resize, mode, save."""

    def __init__(self, size, mode="RGB"):
        self.size, self.mode = size, mode

    def resize(self, size):
        return _FakeImage(size, self.mode)

    def convert(self, mode):
        return _FakeImage(self.size, mode)

    def save(self, buf, format=None, quality=None):  # noqa: A002 - Pillow name
        buf.write(b"\xff\xd8" + f"{format}:{self.size[0]}x{self.size[1]}:{quality}".encode() + b"\xff\xd9")


class MonitorChoiceTests(unittest.TestCase):
    def test_cursor_primary_and_number(self):
        rect, label = pc.choose_monitor("cursor", MONITORS, MONITORS[1])
        self.assertEqual((rect["left"], label), (-2560, "screen with the mouse"))
        rect, label = pc.choose_monitor("cursor", MONITORS, None)
        self.assertEqual((rect["left"], label), (0, "main screen"))
        rect, _ = pc.choose_monitor(2, MONITORS)
        self.assertEqual(rect["top"], -1080)

    def test_missing_number_says_how_many_screens_exist(self):
        rect, reason = pc.choose_monitor(5, MONITORS)
        self.assertIsNone(rect)
        self.assertIn("this PC has 3 screen(s)", reason)

    def test_scaled_size_keeps_aspect_and_never_enlarges(self):
        self.assertEqual(pc.scaled_size(2560, 1440, 960), (960, 540))
        self.assertEqual(pc.scaled_size(800, 600, 960), (800, 600))


class SelfTestTests(unittest.TestCase):
    def grab(self, rect):
        self.grabbed.append(rect)
        return _FakeImage((rect["width"], rect["height"]), "RGBA")

    def setUp(self):
        self.grabbed = []
        self.logs = []

    def test_capture_encode_and_picture_for_the_browser(self):
        report = pc.self_test(SETTINGS, MONITORS, MONITORS[1], grab=self.grab, frames=3,
                              clock=_Clock(0.004), log=lambda lv, t: self.logs.append((lv, t)))
        self.assertTrue(report["ok"], report)
        self.assertEqual(len(self.grabbed), 3)
        self.assertEqual(self.grabbed[0]["left"], -2560)
        self.assertEqual((report["width"], report["height"]), (960, 540))
        prefix = "data:image/jpeg;base64,"
        self.assertTrue(report["image"].startswith(prefix))
        self.assertEqual(base64.b64decode(report["image"][len(prefix):]),
                         b"\xff\xd8JPEG:960x540:70\xff\xd9")
        steps = {s["step"]: s for s in report["steps"]}
        self.assertTrue(steps[pc.STEP_CAPTURE]["ok"])
        self.assertIn("2560x1440", steps[pc.STEP_CAPTURE]["detail"])
        self.assertTrue(steps[pc.STEP_RATE]["ok"])  # 8 ms per frame -> about 125 fps
        self.assertFalse(steps[pc.STEP_STREAM]["ok"])
        self.assertIn("preview_unavailable", steps[pc.STEP_STREAM]["detail"])
        texts = [t for _lv, t in self.logs]
        self.assertTrue(any(t.startswith("remote_preview self-test started") for t in texts))
        self.assertTrue(any("self-test capture ok 2560x1440 -> 960x540" in t for t in texts))

    def test_slow_capture_marks_the_frame_rate(self):
        report = pc.self_test(dict(SETTINGS, fps=30), MONITORS, None, grab=self.grab, frames=1,
                              clock=_Clock(0.05))
        rate = next(s for s in report["steps"] if s["step"] == pc.STEP_RATE)
        self.assertFalse(rate["ok"])
        self.assertIn("lower the frame rate", rate["detail"])

    def test_capture_failure_names_the_step_and_reason(self):
        def broken(_rect):
            raise OSError("screen grab failed")
        report = pc.self_test(SETTINGS, MONITORS, None, grab=broken,
                              log=lambda lv, t: self.logs.append((lv, t)))
        self.assertFalse(report["ok"])
        self.assertIsNone(report["image"])
        last = report["steps"][-1]
        self.assertEqual((last["step"], last["ok"]), (pc.STEP_CAPTURE, False))
        self.assertIn("OSError: screen grab failed", last["detail"])
        self.assertIn(("WARN", "remote_preview self-test failed at capture: screen capture failed: "
                               "OSError: screen grab failed"), self.logs)

    @unittest.skipIf(Image is None, "Pillow missing (installed with FlowShift on Windows)")
    def test_real_jpeg_encoder(self):
        def grab(rect):
            return Image.new("RGB", (rect["width"], rect["height"]), (30, 120, 200))
        report = pc.self_test(SETTINGS, MONITORS, MONITORS[1], grab=grab, frames=1)
        data = base64.b64decode(report["image"].split(",", 1)[1])
        picture = Image.open(io.BytesIO(data))
        self.assertEqual((picture.format, picture.size), ("JPEG", (960, 540)))

    def test_no_monitor(self):
        report = pc.self_test(dict(SETTINGS, monitor=4), MONITORS[:1], None, grab=self.grab)
        self.assertFalse(report["ok"])
        self.assertEqual(report["steps"][-1]["step"], pc.STEP_MONITOR)
        self.assertEqual(self.grabbed, [])


if __name__ == "__main__":
    unittest.main()
