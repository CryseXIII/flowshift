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
        handed = []

        def loopback(jpeg, width, height):
            handed.append(("loopback", jpeg, width, height))
            return True, "37 KB in 1 part(s) sent and put together again", 3.0

        def window(jpeg, width, height):
            handed.append(("window", jpeg, width, height))
            return True, "shown for 2 s"

        report = pc.self_test(SETTINGS, MONITORS, MONITORS[1], grab=self.grab, frames=3,
                              clock=_Clock(0.004), log=lambda lv, t: self.logs.append((lv, t)),
                              loopback=loopback, show_window=window)
        self.assertTrue(report["ok"], report)
        jpeg = b"\xff\xd8JPEG:960x540:70\xff\xd9"
        self.assertEqual(handed, [("loopback", jpeg, 960, 540), ("window", jpeg, 960, 540)])
        self.assertEqual([s["step"] for s in report["steps"]],
                         [pc.STEP_SETTINGS, pc.STEP_MONITOR, pc.STEP_CAPTURE, pc.STEP_ENCODE,
                          pc.STEP_RATE, pc.STEP_LOOPBACK, pc.STEP_WINDOW, pc.STEP_DISPLAY])
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
        self.assertIn("Mbit/s", steps[pc.STEP_RATE]["detail"])
        self.assertTrue(steps[pc.STEP_LOOPBACK]["ok"])
        self.assertEqual(steps[pc.STEP_WINDOW]["detail"], "shown for 2 s")
        texts = [t for _lv, t in self.logs]
        self.assertTrue(any(t.startswith("remote_preview self-test started") for t in texts))
        self.assertTrue(any("self-test capture ok 2560x1440 -> 960x540" in t for t in texts))
        self.assertIn("remote_preview self-test window ok: shown for 2 s", texts)

    def test_failed_transfer_or_window_makes_the_test_red_with_the_step(self):
        report = pc.self_test(SETTINGS, MONITORS, None, grab=self.grab, frames=1,
                              loopback=lambda *a: (False, "the picture arrived incomplete", 0.0),
                              show_window=lambda *a: (_ for _ in ()).throw(OSError("no desktop")),
                              log=lambda lv, t: self.logs.append((lv, t)))
        self.assertFalse(report["ok"])
        steps = {s["step"]: s for s in report["steps"]}
        self.assertEqual((steps[pc.STEP_LOOPBACK]["ok"], steps[pc.STEP_LOOPBACK]["detail"]),
                         (False, "the picture arrived incomplete"))
        self.assertEqual((steps[pc.STEP_WINDOW]["ok"], steps[pc.STEP_WINDOW]["detail"]),
                         (False, "OSError: no desktop"))
        self.assertIsNotNone(report["image"])   # the browser still shows the capture
        self.assertIn(("WARN", "remote_preview self-test window failed: OSError: no desktop"),
                      self.logs)

    def test_without_window_support_the_step_says_why(self):
        report = pc.self_test(SETTINGS, MONITORS, None, grab=self.grab, frames=1,
                              loopback=lambda *a: (True, "ok", 1.0))
        window = next(s for s in report["steps"] if s["step"] == pc.STEP_WINDOW)
        self.assertEqual((window["ok"], window["detail"]),
                         (False, "no preview window on this system (Windows only)"))
        self.assertFalse(report["ok"])

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

    @unittest.skipIf(Image is None, "Pillow missing (installed with FlowShift on Windows)")
    def test_real_stream_capture_draws_the_mouse_and_encodes(self):
        capture = pc.StreamCapture(dict(SETTINGS, monitor="cursor"), lambda: MONITORS,
                                   lambda: (100, 100), lambda x, y: MONITORS[0],
                                   grab=lambda r: Image.new("RGB", (r["width"], r["height"])))
        frame = capture(None)
        picture = Image.open(io.BytesIO(frame["jpeg"]))
        self.assertEqual((picture.format, picture.size), ("JPEG", (960, 540)))
        # The arrow at (50, 50) after scaling 1920 -> 960 is white on the black screen.
        self.assertGreater(picture.convert("L").getpixel((51, 56)), 128)

    def test_no_monitor(self):
        report = pc.self_test(dict(SETTINGS, monitor=4), MONITORS[:1], None, grab=self.grab)
        self.assertFalse(report["ok"])
        self.assertEqual(report["steps"][-1]["step"], pc.STEP_MONITOR)
        self.assertEqual(self.grabbed, [])


class _Screen:
    """Fake screen picture with pixels, so "only on change" can be tested."""

    def __init__(self, size, pixels=b"a", mode="RGB"):
        self.size, self.pixels, self.mode = size, pixels, mode

    def resize(self, size):
        return _Screen(size, self.pixels, self.mode)

    def convert(self, mode):
        return _Screen(self.size, self.pixels, mode)

    def tobytes(self):
        return self.pixels + repr(self.size).encode()

    def save(self, buf, format=None, quality=None):  # noqa: A002 - Pillow name
        buf.write(b"JPEG:" + self.tobytes())


class StreamCaptureTests(unittest.TestCase):
    def setUp(self):
        self.pixels = b"one"
        self.cursor = (2000 - 2560, 700)           # on the left 2560x1440 monitor
        self.grabbed = []
        self.drawn = []
        self.locked = False

    def grab(self, rect):
        self.grabbed.append(dict(rect))
        if self.locked:
            raise OSError("screen grab failed")
        return _Screen((rect["width"], rect["height"]), self.pixels)

    def monitor_at(self, x, y):
        return next(m for m in MONITORS if m["left"] <= x < m["left"] + m["width"]
                    and m["top"] <= y < m["top"] + m["height"])

    def capture(self, **settings):
        return pc.StreamCapture(dict(SETTINGS, **settings), lambda: MONITORS,
                                lambda: self.cursor, self.monitor_at, grab=self.grab,
                                desktop_locked=lambda: self.locked,
                                cursor=lambda image, x, y: self.drawn.append((image.size, x, y)))

    def test_follows_the_mouse_and_sends_only_on_change(self):
        capture = self.capture()
        first = capture(None)
        self.assertEqual(self.grabbed[-1], MONITORS[1])
        self.assertEqual((first["width"], first["height"]), (960, 540))
        # Mouse at x=2000 of 2560 -> 750 of 960; y=700 of 1440 -> 262 (rounded) of 540.
        self.assertEqual(self.drawn[-1], ((960, 540), 750, 262))
        self.assertTrue(first["jpeg"].startswith(b"JPEG:one"))
        self.assertIsNone(capture(first["key"]))    # same pixels: nothing to send
        self.pixels = b"two"
        second = capture(first["key"])
        self.assertNotEqual(second["key"], first["key"])
        # The mouse moves to the main screen: the next picture is of that screen.
        self.cursor = (100, 100)
        self.pixels = b"main screen"
        third = capture(second["key"])
        self.assertEqual(self.grabbed[-1]["left"], 0)
        self.assertEqual((third["width"], third["height"]), (960, 540))

    def test_area_around_the_mouse(self):
        capture = self.capture(cursor_crop=True, max_width=960)
        frame = capture(None)
        # 960x540 around (-560, 700), inside the left monitor; not scaled any more.
        self.assertEqual(self.grabbed[-1], {"left": -1040, "top": 430, "width": 960, "height": 540})
        self.assertEqual((frame["width"], frame["height"]), (960, 540))
        self.assertEqual(self.drawn[-1], ((960, 540), 480, 270))
        # At the monitor's corner the section is pushed inside.
        self.cursor = (-2550, 5)
        capture(None)
        self.assertEqual(self.grabbed[-1], {"left": -2560, "top": 0, "width": 960, "height": 540})

    def test_crop_rect_never_leaves_the_monitor(self):
        mon = {"left": 0, "top": 0, "width": 1280, "height": 720}
        self.assertEqual(pc.crop_rect(mon, (1279, 719), 1920), mon)
        self.assertEqual(pc.crop_rect(mon, None, 640), mon)
        self.assertEqual(pc.crop_rect(mon, (640, 360), 640),
                         {"left": 320, "top": 180, "width": 640, "height": 360})

    def test_locked_screen_is_named_and_unlocked_errors_stay_capture_errors(self):
        capture = self.capture()
        self.locked = True
        with self.assertRaises(pc.CaptureError) as caught:
            capture(None)
        self.assertEqual(caught.exception.code, "screen_locked")

        def broken(rect):
            raise OSError("driver gone")
        failing = pc.StreamCapture(SETTINGS, lambda: MONITORS, lambda: None, self.monitor_at,
                                   grab=broken, desktop_locked=lambda: False)
        with self.assertRaises(pc.CaptureError) as caught:
            failing(None)
        self.assertEqual(caught.exception.code, "capture_failed")

    def test_a_broken_lock_check_never_stops_a_working_capture(self):
        def lock_check():
            raise OSError("OpenInputDesktop denied")
        capture = pc.StreamCapture(SETTINGS, lambda: MONITORS, lambda: None, self.monitor_at,
                                   grab=self.grab, desktop_locked=lock_check)
        self.assertIsNotNone(capture(None))


if __name__ == "__main__":
    unittest.main()
