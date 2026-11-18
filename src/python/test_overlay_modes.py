"""Pure placement tests for the per-mode overlay geometry."""
from __future__ import annotations

import math
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import overlay_geometry as geometry
from overlay_geometry import MODE_SIZES_CSS, WorkArea, css_to_physical, mode_placement

WHEEL_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                        "webgui", "src", "overlay", "wheelGeometry.js")


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

class WheelShapeTests(unittest.TestCase):
    """The visible wheel is only the ring sectors; hub, gaps and corners are not."""

    def shape(self, sectors=8, pages=1, size=360):
        return geometry.wheel_shape_polygons(sectors, pages, size)

    def polar(self, radius_css, angle_deg, size=360):
        factor = size / 360
        rad = math.radians(angle_deg)
        return (size / 2 + radius_css * factor * math.cos(rad),
                size / 2 + radius_css * factor * math.sin(rad))

    def test_sector_middles_are_inside_the_shape(self):
        polygons = self.shape(8)
        for index in range(8):
            angle = -90 + index * 45
            x, y = self.polar(113, angle)
            self.assertTrue(geometry.point_in_shape(x, y, polygons), index)

    def test_hub_corners_and_outside_ring_are_transparent(self):
        polygons = self.shape(8)
        for point in [(180, 180), (180, 150), (2, 2), (358, 2), (2, 358), (358, 358),
                      self.polar(172, 0)]:
            self.assertFalse(geometry.point_in_shape(*point, polygons), point)

    def test_gap_between_sectors_is_transparent(self):
        polygons = self.shape(8)
        # Sector boundaries lie at -90 +/- 22.5 deg; the 2.5 deg gap is cut out.
        x, y = self.polar(150, -90 + 22.5)
        self.assertFalse(geometry.point_in_shape(x, y, polygons))
        x, y = self.polar(150, -90 + 22.5 + 3)
        self.assertTrue(geometry.point_in_shape(x, y, polygons))

    def test_single_slot_is_a_full_ring_without_hub(self):
        polygons = self.shape(1)
        for angle in (-90, 0, 90, 180, 45, 225):
            self.assertTrue(geometry.point_in_shape(*self.polar(113, angle), polygons), angle)
        self.assertFalse(geometry.point_in_shape(180, 180, polygons))

    def test_page_indicator_pill_only_with_several_pages(self):
        self.assertFalse(geometry.point_in_shape(180, 180, self.shape(4, pages=1)))
        self.assertTrue(geometry.point_in_shape(180, 180, self.shape(4, pages=3)))
        self.assertTrue(geometry.point_in_shape(180, 180, self.shape(4, pages=16)))
        # The pill never reaches into the ring, even with the maximum of dots.
        self.assertLess(geometry.wheel_pill_width_css(geometry.WHEEL_MAX_DOTS),
                        2 * geometry.WHEEL_INNER_RADIUS_CSS)
        self.assertEqual(geometry.wheel_pill_width_css(16), geometry.WHEEL_TEXT_PILL_WIDTH_CSS)

    def test_shape_scales_with_dpi(self):
        size = css_to_physical(360, 192)
        polygons = self.shape(8, size=size)
        self.assertTrue(geometry.point_in_shape(*self.polar(113, -90, size), polygons))
        self.assertFalse(geometry.point_in_shape(size / 2, size / 2, polygons))
        xs = [x for polygon in polygons for x, _ in polygon]
        self.assertLessEqual(max(xs), size)
        self.assertGreaterEqual(min(xs), 0)
        self.assertGreater(max(xs) - min(xs), 2 * 166 * 2 - 2)

    def test_page_shapes_follow_payload_slot_counts(self):
        data = {"pages": [["copy", "paste", "cut"], ["undo", "bogus"], []],
                "actions": {"copy": {}, "paste": {}, "cut": {}, "undo": {}}}
        self.assertEqual(geometry.wheel_slot_counts(data), [3, 1])
        placement = mode_placement("command_wheel", 960, 520, WorkArea(0, 0, 1920, 1040, 96))
        first = geometry.overlay_shape_polygons("command_wheel", placement, data, 0)
        second = geometry.overlay_shape_polygons("command_wheel", placement, data, 1)
        # Page 2 has one slot: a full ring, so the bottom of the ring is a button.
        bottom = self.polar(113, 90)
        self.assertTrue(geometry.point_in_shape(*bottom, second))
        # Page 1 has three sectors (top, lower right, lower left): straight down is a gap.
        self.assertFalse(geometry.point_in_shape(*bottom, first))
        self.assertTrue(geometry.point_in_shape(*self.polar(113, 30), first))
        # An out-of-range page falls back to the first page.
        self.assertEqual(geometry.overlay_shape_polygons("command_wheel", placement, data, 9),
                         first)

    def test_clipboard_shape_is_rounded_panel(self):
        placement = mode_placement("clipboard", 100, 200, WorkArea(0, 0, 1920, 1040, 96))
        polygons = geometry.overlay_shape_polygons("clipboard", placement)
        self.assertTrue(geometry.point_in_shape(210, 260, polygons))
        self.assertFalse(geometry.point_in_shape(0.5, 0.5, polygons))
        self.assertFalse(geometry.point_in_shape(placement.width + 5, 10, polygons))

    def test_wheel_center_stays_on_cursor_and_moves_only_at_edges(self):
        area = WorkArea(0, 0, 1920, 1040, 96)
        placement = mode_placement("command_wheel", 961, 521, area)
        self.assertEqual(geometry.placement_center(placement), (961, 521))
        edge = mode_placement("command_wheel", 3, 1035, area)
        self.assertEqual(geometry.placement_center(edge), (180, 1040 - 180))

    def test_wheel_center_on_negative_secondary_monitor_at_200_percent(self):
        area = WorkArea(-3840, -200, 0, 1960, 192)
        placement = mode_placement("command_wheel", -1001, 333, area)
        self.assertEqual(placement.width, 720)
        self.assertEqual(geometry.placement_center(placement), (-1001, 333))

    def test_python_constants_match_wheel_geometry_js(self):
        with open(WHEEL_JS, encoding="utf-8") as handle:
            source = handle.read()

        def js(name):
            match = re.search(rf"export const {name} = ([0-9.]+)", source)
            self.assertIsNotNone(match, name)
            return float(match.group(1))

        self.assertEqual(js("WHEEL_SIZE"), geometry.WHEEL_SIZE_CSS)
        self.assertEqual(js("OUTER_RADIUS"), geometry.WHEEL_OUTER_RADIUS_CSS)
        self.assertEqual(js("INNER_RADIUS"), geometry.WHEEL_INNER_RADIUS_CSS)
        self.assertEqual(js("SECTOR_GAP_DEG"), geometry.WHEEL_SECTOR_GAP_DEG)
        self.assertEqual(js("MAX_SLOTS"), geometry.WHEEL_MAX_SLOTS)
        self.assertEqual(js("MAX_DOTS"), geometry.WHEEL_MAX_DOTS)


if __name__ == "__main__":
    unittest.main()
