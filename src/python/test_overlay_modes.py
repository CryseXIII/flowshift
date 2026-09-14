"""Pure placement tests for the per-mode overlay geometry."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from overlay_geometry import MODE_SIZES_CSS, WorkArea, css_to_physical, mode_placement


class ModePlacementTests(unittest.TestCase):
    area = WorkArea(0, 0, 1920, 1040, 96)

    def test_command_wheel_is_centered_on_cursor(self):
        placement = mode_placement("command_wheel", 960, 520, self.area)
        w, h = MODE_SIZES_CSS["command_wheel"]
        self.assertEqual((placement.width, placement.height), (w, h))
        self.assertEqual(placement.x, 960 - w // 2)
        self.assertEqual(placement.y, 520 - h // 2)

    def test_command_wheel_is_square(self):
        w, h = MODE_SIZES_CSS["command_wheel"]
        self.assertEqual(w, h)

    def test_command_wheel_shifts_inside_edges(self):
        w, h = MODE_SIZES_CSS["command_wheel"]
        top_left = mode_placement("command_wheel", 5, 5, self.area)
        self.assertEqual((top_left.x, top_left.y), (0, 0))
        bottom_right = mode_placement("command_wheel", 1919, 1039, self.area)
        self.assertEqual((bottom_right.x, bottom_right.y), (1920 - w, 1040 - h))

    def test_command_wheel_centers_in_physical_pixels_at_150_percent(self):
        area = WorkArea(0, 0, 2880, 1560, 144)
        placement = mode_placement("command_wheel", 1440, 780, area)
        w = css_to_physical(MODE_SIZES_CSS["command_wheel"][0], 144)
        self.assertEqual(placement.width, w)
        self.assertEqual(placement.x, 1440 - w // 2)
        self.assertEqual(placement.dpi, 144)

    def test_clipboard_keeps_cursor_offset_and_portrait_size(self):
        placement = mode_placement("clipboard", 100, 200, self.area)
        w, h = MODE_SIZES_CSS["clipboard"]
        self.assertGreater(h, w)
        self.assertEqual((placement.x, placement.y), (116, 216))
        self.assertEqual((placement.width, placement.height), (w, h))

    def test_clipboard_clamps_on_secondary_negative_monitor(self):
        area = WorkArea(-1920, -300, 0, 780, 96)
        w, h = MODE_SIZES_CSS["clipboard"]
        placement = mode_placement("clipboard", -10, 770, area)
        self.assertEqual((placement.x, placement.y), (0 - w, 780 - h))

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            mode_placement("diagnostic", 1, 1, self.area)
        with self.assertRaises(ValueError):
            mode_placement(None, 1, 1, self.area)


if __name__ == "__main__":
    unittest.main()
