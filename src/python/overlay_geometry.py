"""Pure overlay placement plus optional Windows monitor/DPI discovery."""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass


DEFAULT_DPI = 96


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def _dpi(dpi):
    value = _number(dpi, "dpi")
    if value <= 0:
        raise ValueError("dpi must be greater than zero")
    return value


@dataclass(frozen=True)
class WorkArea:
    """Physical virtual-desktop work-area bounds and effective monitor DPI."""

    left: int
    top: int
    right: int
    bottom: int
    dpi: int = DEFAULT_DPI

    def __post_init__(self):
        for name in ("left", "top", "right", "bottom"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
        _dpi(self.dpi)
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("work area must have positive width and height")

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top


@dataclass(frozen=True)
class OverlayPlacement:
    """A fully clamped overlay rectangle in physical virtual-desktop pixels."""

    x: int
    y: int
    width: int
    height: int
    dpi: int

    @property
    def scale(self):
        return dpi_scale(self.dpi)


def dpi_scale(dpi):
    """Return the monitor scale relative to the 96-DPI CSS baseline."""
    return _dpi(dpi) / DEFAULT_DPI


def css_to_physical(value, dpi):
    """Convert a non-negative CSS-pixel distance to physical pixels once."""
    number = _number(value, "CSS value")
    if number < 0:
        raise ValueError("CSS value must not be negative")
    return int(math.floor(number * dpi_scale(dpi) + 0.5))


def physical_to_css(value, dpi):
    """Convert a non-negative physical-pixel distance to CSS pixels."""
    number = _number(value, "physical value")
    if number < 0:
        raise ValueError("physical value must not be negative")
    return number / dpi_scale(dpi)


def clamp_overlay_to_work_area(
        cursor_x, cursor_y, width_css, height_css, work_area,
        offset_x=16, offset_y=16):
    """Place a CSS-sized overlay near a physical cursor and clamp it fully."""
    if not isinstance(work_area, WorkArea):
        raise ValueError("work_area must be a WorkArea")
    cursor_x = _number(cursor_x, "cursor_x")
    cursor_y = _number(cursor_y, "cursor_y")
    offset_x = _number(offset_x, "offset_x")
    offset_y = _number(offset_y, "offset_y")
    width = css_to_physical(width_css, work_area.dpi)
    height = css_to_physical(height_css, work_area.dpi)
    if width <= 0 or height <= 0:
        raise ValueError("overlay dimensions must be greater than zero")
    if width > work_area.width or height > work_area.height:
        raise ValueError("overlay dimensions must fit within the work area")

    requested_x = int(round(cursor_x + offset_x))
    requested_y = int(round(cursor_y + offset_y))
    x = min(max(requested_x, work_area.left), work_area.right - width)
    y = min(max(requested_y, work_area.top), work_area.bottom - height)
    return OverlayPlacement(x, y, width, height, int(round(work_area.dpi)))


# CSS sizes per overlay mode. The command wheel is a square centered on the
# cursor; the clipboard panel is a portrait list anchored next to the cursor.
MODE_SIZES_CSS = {
    "clipboard": (420, 520),
    "command_wheel": (360, 360),
}


def mode_placement(mode, cursor_x, cursor_y, work_area):
    """Clamp the mode-specific overlay rectangle around a physical cursor.

    ``command_wheel`` is centered on the cursor so every sector is equally
    reachable; ``clipboard`` keeps the small offset so the cursor does not
    cover the first row. Both are clamped fully into the work area, so near a
    screen edge the wheel shifts instead of being cut off.
    """
    try:
        width_css, height_css = MODE_SIZES_CSS[mode]
    except (KeyError, TypeError):
        raise ValueError("unsupported overlay mode") from None
    if mode == "command_wheel":
        width = css_to_physical(width_css, work_area.dpi)
        height = css_to_physical(height_css, work_area.dpi)
        return clamp_overlay_to_work_area(
            cursor_x, cursor_y, width_css, height_css, work_area,
            offset_x=-(width // 2), offset_y=-(height // 2))
    return clamp_overlay_to_work_area(cursor_x, cursor_y, width_css, height_css, work_area)


def placement_center(placement):
    """Physical pixel at the center of a placement (the wheel's hub)."""
    return placement.x + placement.width // 2, placement.y + placement.height // 2


# Command-wheel shape in CSS pixels of the 360x360 viewBox. These values must
# match webgui/src/overlay/wheelGeometry.js (checked by test_overlay_modes).
WHEEL_SIZE_CSS = MODE_SIZES_CSS["command_wheel"][0]
WHEEL_OUTER_RADIUS_CSS = 166
WHEEL_INNER_RADIUS_CSS = 60
WHEEL_SECTOR_GAP_DEG = 2.5
WHEEL_MAX_SLOTS = 8
WHEEL_MAX_DOTS = 8
WHEEL_REGION_MARGIN_CSS = 1.5
WHEEL_ARC_STEP_DEG = 3.0
# Page indicator pill in the hub: dots (7 px, 6 px gap, 5/4 px padding) up to
# WHEEL_MAX_DOTS pages, otherwise a fixed-width "n/m" text pill.
WHEEL_DOT_CSS = 7
WHEEL_DOT_GAP_CSS = 6
WHEEL_PILL_PAD_X_CSS = 5
WHEEL_PILL_HEIGHT_CSS = 15
WHEEL_TEXT_PILL_WIDTH_CSS = 44
CLIPBOARD_CORNER_RADIUS_CSS = 12


def wheel_page_slots(data):
    """Action ids per renderable wheel page, mirroring normalizeWheelData()."""
    if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
        return []
    actions = data.get("actions") if isinstance(data.get("actions"), dict) else {}
    pages = []
    for page in data["pages"]:
        if not isinstance(page, list):
            continue
        # JavaScript truthiness: any object (even {}) counts, null/""/0 do not.
        slots = [item for item in page if isinstance(item, str) and item
                 and (isinstance(actions.get(item), (dict, list)) or actions.get(item))]
        if slots:
            pages.append(slots[:WHEEL_MAX_SLOTS])
    return pages


def wheel_slot_counts(data):
    """Slot count per renderable wheel page."""
    return [len(slots) for slots in wheel_page_slots(data)]


def wheel_pill_width_css(page_count):
    if page_count <= 1:
        return 0
    if page_count > WHEEL_MAX_DOTS:
        return WHEEL_TEXT_PILL_WIDTH_CSS
    return (page_count * WHEEL_DOT_CSS + (page_count - 1) * WHEEL_DOT_GAP_CSS
            + 2 * WHEEL_PILL_PAD_X_CSS)


def _annular_polygon(center, inner, outer, start_deg, end_deg):
    steps = max(1, int(math.ceil((end_deg - start_deg) / WHEEL_ARC_STEP_DEG)))
    angles = [start_deg + (end_deg - start_deg) * index / steps for index in range(steps + 1)]
    outer_arc = [(center + outer * math.cos(math.radians(a)),
                  center + outer * math.sin(math.radians(a))) for a in angles]
    inner_arc = [(center + inner * math.cos(math.radians(a)),
                  center + inner * math.sin(math.radians(a))) for a in reversed(angles)]
    return outer_arc + inner_arc


def wheel_shape_polygons(sector_count, page_count, size_px):
    """Visible wheel parts as polygons in window-relative physical pixels.

    Each sector is an annular segment with the same angles as the SVG sectors
    (first sector centered at the top, clockwise, ``WHEEL_SECTOR_GAP_DEG``
    between neighbours). A single slot is a full ring, split into two halves
    because a polygon cannot carry a hole. With more than one page the small
    page-indicator pill in the hub is added. Everything else (hub, corners,
    gaps) is outside the shape: transparent and not hit-testable.
    """
    if isinstance(size_px, bool) or not isinstance(size_px, int) or size_px <= 0:
        raise ValueError("size_px must be a positive integer")
    factor = size_px / WHEEL_SIZE_CSS
    center = size_px / 2
    outer = (WHEEL_OUTER_RADIUS_CSS + WHEEL_REGION_MARGIN_CSS) * factor
    inner = (WHEEL_INNER_RADIUS_CSS - WHEEL_REGION_MARGIN_CSS) * factor
    count = max(0, min(int(sector_count), WHEEL_MAX_SLOTS))
    polygons = []
    if count == 1:
        polygons.append(_annular_polygon(center, inner, outer, -90, 90))
        polygons.append(_annular_polygon(center, inner, outer, 90, 270))
    elif count > 1:
        step = 360 / count
        gap = WHEEL_SECTOR_GAP_DEG / 2
        for index in range(count):
            start = -90 - step / 2 + index * step
            polygons.append(_annular_polygon(center, inner, outer,
                                             start + gap, start + step - gap))
    pill = wheel_pill_width_css(page_count)
    if pill:
        half_w = pill * factor / 2
        half_h = WHEEL_PILL_HEIGHT_CSS * factor / 2
        polygons.append([(center - half_w, center - half_h), (center + half_w, center - half_h),
                         (center + half_w, center + half_h), (center - half_w, center + half_h)])
    return [[(int(round(x)), int(round(y))) for x, y in polygon] for polygon in polygons]


def rounded_rect_polygon(width, height, radius):
    """Rounded rectangle outline (window-relative) for the clipboard panel."""
    radius = max(0, min(int(radius), width // 2, height // 2))
    if radius == 0:
        return [(0, 0), (width, 0), (width, height), (0, height)]
    points = []
    corners = ((width - radius, radius, -90), (width - radius, height - radius, 0),
               (radius, height - radius, 90), (radius, radius, 180))
    for cx, cy, start in corners:
        for step in range(7):
            angle = math.radians(start + step * 15)
            points.append((int(round(cx + radius * math.cos(angle))),
                           int(round(cy + radius * math.sin(angle)))))
    return points


def overlay_shape_polygons(mode, placement, data=None, page=0):
    """Window-relative polygons of the visible, clickable overlay surface."""
    if mode == "command_wheel":
        counts = wheel_slot_counts(data or {})
        page = page if isinstance(page, int) and 0 <= page < len(counts) else 0
        sectors = counts[page] if counts else 0
        return wheel_shape_polygons(sectors, len(counts), placement.width)
    if mode == "clipboard":
        return [rounded_rect_polygon(
            placement.width, placement.height,
            css_to_physical(CLIPBOARD_CORNER_RADIUS_CSS, placement.dpi))]
    raise ValueError("unsupported overlay mode")


def point_in_polygon(x, y, polygon):
    """Even-odd ray cast; points exactly on an edge may fall either way."""
    inside = False
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        if (y1 > y) != (y2 > y):
            crossing = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < crossing:
                inside = not inside
    return inside


def point_in_shape(x, y, polygons):
    """True if the window-relative point lies on a visible overlay part."""
    return any(point_in_polygon(x, y, polygon) for polygon in polygons)


def set_per_monitor_v2_awareness():
    """Enable the best available process DPI awareness before window creation."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        try:
            return bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
        except (AttributeError, OSError):
            pass
        try:
            return ctypes.windll.shcore.SetProcessDpiAwareness(2) in (0, -2147024891)
        except (AttributeError, OSError):
            return bool(user32.SetProcessDPIAware())
    except (AttributeError, OSError):
        return False


def get_monitor_work_area(x, y):
    """Return the nearest monitor's physical work area, with a 96-DPI fallback."""
    px = int(_number(x, "x"))
    py = int(_number(y, "y"))
    if sys.platform != "win32":
        return WorkArea(0, 0, 1920, 1080, DEFAULT_DPI)

    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        point = wintypes.POINT(px, py)
        monitor = ctypes.windll.user32.MonitorFromPoint(point, 2)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if not monitor or not ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise OSError("GetMonitorInfoW failed")

        dpi = DEFAULT_DPI
        try:
            dpi_x = wintypes.UINT()
            dpi_y = wintypes.UINT()
            if ctypes.windll.shcore.GetDpiForMonitor(
                    monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
                dpi = int(dpi_x.value) or DEFAULT_DPI
        except (AttributeError, OSError):
            pass
        rect = info.rcWork
        return WorkArea(rect.left, rect.top, rect.right, rect.bottom, dpi)
    except (AttributeError, OSError, ValueError):
        return WorkArea(0, 0, 1920, 1080, DEFAULT_DPI)
