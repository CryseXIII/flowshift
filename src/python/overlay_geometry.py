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
