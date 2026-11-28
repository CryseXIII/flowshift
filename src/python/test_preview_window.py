"""Remote preview window: placement in the chosen corner and the caption.

The window itself needs Windows (layered, click-through, never activated);
off Windows only its pure helpers run, and creating it says why it cannot.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import preview_window as pw

AREA = {"left": 0, "top": 0, "width": 1920, "height": 1040}   # work area without taskbar


class PlacementTests(unittest.TestCase):
    def test_every_corner_keeps_the_margin(self):
        h = round(480 * 540 / 960) + pw.CAPTION_PX
        expected = {
            "top_left": (16, 16, 480, h),
            "top_right": (1920 - 16 - 480, 16, 480, h),
            "bottom_left": (16, 1040 - 16 - h, 480, h),
            "bottom_right": (1920 - 16 - 480, 1040 - 16 - h, 480, h),
        }
        for corner, box in expected.items():
            self.assertEqual(pw.placement(corner, 480, 960, 540, AREA), box, corner)

    def test_second_monitor_and_too_wide_setting(self):
        left_monitor = {"left": -1280, "top": 100, "width": 1280, "height": 1000}
        x, y, w, h = pw.placement("top_left", 1200, 1920, 1080, left_monitor)
        self.assertEqual((x, y, w), (-1264, 116, 1200))
        x, y, w, h = pw.placement("bottom_right", 5000, 1920, 1080, left_monitor)
        self.assertEqual(w, 1280 - 32)
        self.assertLessEqual(y + h, 100 + 1000 - 16)

    def test_caption(self):
        self.assertEqual(pw.caption("PC-2", fps=14.0), "PC-2 - 14 fps")
        self.assertEqual(pw.caption("PC-2", notice="screen locked"), "PC-2 - screen locked")
        self.assertEqual(pw.caption(""), "other PC")


@unittest.skipIf(sys.platform == "win32", "checks the refusal off Windows")
class OffWindowsTests(unittest.TestCase):
    def test_creating_the_window_names_the_reason(self):
        with self.assertRaises(OSError) as caught:
            pw.PreviewWindow(lambda level, text: None)
        self.assertIn("needs Windows", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
