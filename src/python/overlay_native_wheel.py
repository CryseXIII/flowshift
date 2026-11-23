"""Natively drawn Command Wheel: a layered window with real per-pixel alpha.

dev.30-dev.34 drew the wheel with WebView2 inside pywebview's WinForms form
and tried a window region, a colour key, WebView2 alpha 0 and DWM
blur-behind. Every call reported success, yet Viktor's screenshots (21.11.)
showed a 356x356 rounded rectangle in #202020 with the thin light border
Windows 11 draws around a normal window: the sector fill measured #141920,
which is exactly rgba(19, 24, 32, 0.9) over #202020, so the page itself was
transparent and DWM composed it onto its own window surface.

This module avoids WebView2 and the form for the wheel. One ``WS_POPUP``
window with ``WS_EX_LAYERED`` gets its picture only through
``UpdateLayeredWindow`` with ``ULW_ALPHA``: a 32-bit premultiplied bitmap
drawn with GDI+. DWM shows exactly these pixels, a pixel with alpha 0 shows
the desktop and passes clicks to the window below, and such a window has no
DWM frame, border or rounded corners (also switched off explicitly with
``DWMWA_WINDOW_CORNER_PREFERENCE`` and ``DWMWA_SYSTEMBACKDROP_TYPE``).

The scene (what is drawn where, and which sector a point hits) is pure Python
and tested on every platform; only :class:`NativeWheelWindow` needs Windows.
"""
from __future__ import annotations

import ctypes
import math
import queue
import sys
import threading

import overlay_geometry as geometry

WINDOW_CLASS = "FlowShiftCommandWheel"
WINDOW_TITLE = "FlowShift Command Wheel"
CALL_TIMEOUT = 2.0
ARC_STEP_DEG = 1.5
LABEL_FONT_CSS = 13
ERROR_FONT_CSS = 11
PAGE_TEXT_FONT_CSS = 10
LABEL_BOX_CSS = (100, 34)
FONT_FAMILY = "Segoe UI"

# Colours as (alpha, r, g, b), the CSS of webgui/src/overlay/overlay.css.
SECTOR_FILL = (230, 19, 24, 32)          # rgba(19, 24, 32, 0.9)
SECTOR_STROKE = (56, 255, 255, 255)      # rgba(255, 255, 255, 0.22)
HOT_FILL = (225, 44, 82, 138)            # the lit spotlight sector
HOT_STROKE = (217, 255, 255, 255)        # rgba(255, 255, 255, 0.85)
BUSY_STROKE = (160, 255, 255, 255)
LABEL_COLOR = (255, 245, 247, 250)       # #f5f7fa
ERROR_COLOR = (255, 255, 154, 154)       # #ff9a9a
PILL_FILL = (230, 19, 24, 32)
DOT_COLOR = (115, 255, 255, 255)         # rgba(255, 255, 255, 0.45)
DOT_ACTIVE = (255, 255, 255, 255)
DOT_RING = (89, 0, 0, 0)                 # rgba(0, 0, 0, 0.35)
PAGE_TEXT_COLOR = (255, 220, 226, 234)   # #dce2ea
EMPTY_TEXT = "No actions configured"
EMPTY_COLOR = (255, 174, 184, 199)       # #aeb8c7
STROKE_CSS = 1.2
HOT_STROKE_CSS = 1.8


def argb(color):
    """``(a, r, g, b)`` as the 32-bit ARGB value GDI+ expects."""
    a, r, g, b = (max(0, min(255, int(value))) for value in color)
    return (a << 24) | (r << 16) | (g << 8) | b


def _arc(center, radius, start_deg, end_deg, step=ARC_STEP_DEG):
    steps = max(1, int(math.ceil(abs(end_deg - start_deg) / step)))
    return [(center + radius * math.cos(math.radians(start_deg + (end_deg - start_deg) * i / steps)),
             center + radius * math.sin(math.radians(start_deg + (end_deg - start_deg) * i / steps)))
            for i in range(steps + 1)]


def sector_outlines(index, count, size_px):
    """Float outlines of sector ``index`` of ``count`` (alternate fill).

    Same angles as ``wheelGeometry.sectorPath``: first sector centered at the
    top, clockwise, ``WHEEL_SECTOR_GAP_DEG`` between neighbours. One slot is
    a full ring: an outer and an inner circle, the inner one cut out.
    """
    factor = size_px / geometry.WHEEL_SIZE_CSS
    center = size_px / 2
    outer = geometry.WHEEL_OUTER_RADIUS_CSS * factor
    inner = geometry.WHEEL_INNER_RADIUS_CSS * factor
    if count == 1:
        return [_arc(center, outer, -90, 270)[:-1], _arc(center, inner, -90, 270)[:-1]]
    step = 360 / count
    start = -90 - step / 2 + index * step + geometry.WHEEL_SECTOR_GAP_DEG / 2
    end = start + step - geometry.WHEEL_SECTOR_GAP_DEG
    return [_arc(center, outer, start, end) + list(reversed(_arc(center, inner, start, end)))]


def label_center(index, count, size_px):
    """Radial middle of the sector, as ``wheelGeometry.labelPosition``."""
    factor = size_px / geometry.WHEEL_SIZE_CSS
    step = 360 / count
    mid = math.radians(-90 - step / 2 + index * step + step / 2)
    radius = (geometry.WHEEL_OUTER_RADIUS_CSS + geometry.WHEEL_INNER_RADIUS_CSS) / 2 * factor
    return size_px / 2 + radius * math.cos(mid), size_px / 2 + radius * math.sin(mid)


def capsule_outline(cx, cy, width, height, step=15):
    """Pill (fully rounded rectangle) centered at cx, cy."""
    radius = min(width, height) / 2
    left, right = cx - width / 2 + radius, cx + width / 2 - radius
    points = []
    for x, start in ((right, -90), (left, 90)):
        for i in range(180 // step + 1):
            angle = math.radians(start + i * step)
            points.append((x + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


class WheelModel:
    """State of one shown wheel: page, hover, aim, running action, error."""

    def __init__(self, data, size_px):
        if isinstance(size_px, bool) or not isinstance(size_px, int) or size_px <= 0:
            raise ValueError("size_px must be a positive integer")
        self.size = size_px
        self.pages = geometry.wheel_page_slots(data)
        actions = data.get("actions") if isinstance(data, dict) else None
        self.actions = actions if isinstance(actions, dict) else {}
        self.page = 0
        self.hot = None
        self.aim = None
        self.busy = None
        self.error = None

    @property
    def slots(self):
        return self.pages[self.page] if self.pages else []

    @property
    def active(self):
        if isinstance(self.aim, int) and 0 <= self.aim < len(self.slots):
            return self.aim
        return self.hot

    def label(self, action_id):
        meta = self.actions.get(action_id)
        text = meta.get("label") if isinstance(meta, dict) else None
        return str(text) if isinstance(text, str) and text else str(action_id)

    def polygons(self):
        """Window-relative visible shape, identical to the host's hit polygons."""
        return geometry.wheel_shape_polygons(len(self.slots), len(self.pages), self.size)

    def sector_at(self, x, y):
        """Index of the sector under the window-relative point, or None."""
        count = len(self.slots)
        if not count:
            return None
        polygons = self.polygons()
        sectors = polygons[:2] if count == 1 else polygons[:count]
        for index, polygon in enumerate(sectors):
            if geometry.point_in_polygon(x, y, polygon):
                return 0 if count == 1 else index
        return None

    def scroll(self, delta):
        """Mouse wheel paging like ``nextPage``; True if the page changed."""
        count = len(self.pages)
        if count < 2 or not delta:
            return False
        step = 1 if delta > 0 else -1
        self.page = (self.page + step) % count
        self.hot = None
        self.error = None
        return True


def wheel_scene(model):
    """Drawing items for the model; everything not listed stays alpha 0."""
    size = model.size
    factor = size / geometry.WHEEL_SIZE_CSS
    center = size / 2
    items = []
    slots = model.slots
    if not slots:
        width = 170 * factor
        items.append({"kind": "fill", "outlines": [capsule_outline(center, center, width,
                                                                    26 * factor)],
                      "fill": PILL_FILL})
        items.append({"kind": "text", "text": EMPTY_TEXT, "color": EMPTY_COLOR,
                      "font_px": 13 * factor, "bold": False,
                      "rect": (center - width / 2, center - 13 * factor, width, 26 * factor)})
        return items
    # Only the sector under the mouse (or the swipe aim) changes; every other
    # sector keeps exactly its resting look (Viktor 22.11.: dimming the rest
    # flickered on fast mouse moves). Painting is immediate, no transition.
    active = model.active
    count = len(slots)
    for index, action_id in enumerate(slots):
        hot = active is not None and index == active
        fill, stroke, width = SECTOR_FILL, SECTOR_STROKE, STROKE_CSS
        if hot:
            fill, stroke, width = HOT_FILL, HOT_STROKE, HOT_STROKE_CSS
        elif model.busy == action_id:
            stroke = BUSY_STROKE
        failed = bool(model.error) and model.error[0] == action_id
        text_color = ERROR_COLOR if failed else LABEL_COLOR
        items.append({"kind": "sector", "index": index, "action": action_id,
                      "outlines": sector_outlines(index, count, size),
                      "fill": fill, "stroke": stroke, "stroke_px": width * factor})
        lx, ly = label_center(index, count, size)
        box_w, box_h = LABEL_BOX_CSS[0] * factor, LABEL_BOX_CSS[1] * factor
        items.append({"kind": "text", "text": model.error[1] if failed else model.label(action_id),
                      "color": text_color, "bold": not failed,
                      "font_px": (ERROR_FONT_CSS if failed else LABEL_FONT_CSS) * factor,
                      "rect": (lx - box_w / 2, ly - box_h / 2, box_w, box_h)})
    pages = len(model.pages)
    pill = geometry.wheel_pill_width_css(pages)
    if pill:
        height = geometry.WHEEL_PILL_HEIGHT_CSS * factor
        items.append({"kind": "fill", "outlines": [capsule_outline(center, center,
                                                                    pill * factor, height)],
                      "fill": PILL_FILL})
        if pages > geometry.WHEEL_MAX_DOTS:
            items.append({"kind": "text", "text": f"{model.page + 1}/{pages}",
                          "color": PAGE_TEXT_COLOR, "bold": True,
                          "font_px": PAGE_TEXT_FONT_CSS * factor,
                          "rect": (center - pill * factor / 2, center - height / 2,
                                   pill * factor, height)})
        else:
            dot = geometry.WHEEL_DOT_CSS * factor
            left = center - pill * factor / 2 + geometry.WHEEL_PILL_PAD_X_CSS * factor
            for page in range(pages):
                x = left + page * (geometry.WHEEL_DOT_CSS + geometry.WHEEL_DOT_GAP_CSS) * factor
                items.append({"kind": "dot", "rect": (x, center - dot / 2, dot, dot),
                              "fill": DOT_ACTIVE if page == model.page else DOT_COLOR,
                              "stroke": DOT_RING, "stroke_px": max(1.0, factor),
                              "active": page == model.page})
    return items


# --------------------------------------------------------------------------
# Windows: GDI+ painter and the layered window.

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEWHEEL = 0x020A
WM_MOUSELEAVE = 0x02A3
WM_APP_RUN = 0x8000 + 0x51
SW_HIDE = 0
SW_SHOWNA = 8
ULW_ALPHA = 0x2
AC_SRC_ALPHA = 0x1
IDC_HAND = 32649
TME_LEAVE = 0x2
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWCP_DONOTROUND = 1
DWMSBT_NONE = 1
DWMWA_COLOR_NONE = 0xFFFFFFFE
ERROR_CLASS_ALREADY_EXISTS = 1410


def dwm_flatten(hwnd):
    """No rounded corners, no border, no backdrop; returns the read-back.

    ``{"corner": (set_hr, value), "border": (set_hr, None), "backdrop": ...}``;
    ``value`` is what ``DwmGetWindowAttribute`` reports afterwards (None if
    Windows cannot read it back, e.g. before Windows 11).
    """
    dwmapi = ctypes.WinDLL("dwmapi")
    dwmapi.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                             ctypes.c_void_p, ctypes.c_uint]
    dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
    result = {}
    for name, attribute, value in (("corner", DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_DONOTROUND),
                                   ("border", DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE),
                                   ("backdrop", DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE)):
        data = ctypes.c_uint(value)
        hr = int(dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), 4))
        result[name] = (hr & 0xFFFFFFFF, dwm_read(hwnd, attribute) if name != "border" else None)
    return result


def dwm_read(hwnd, attribute):
    """``DwmGetWindowAttribute`` as int, or None if Windows refuses it."""
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        dwmapi.DwmGetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                                 ctypes.c_void_p, ctypes.c_uint]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
        data = ctypes.c_uint(0)
        if dwmapi.DwmGetWindowAttribute(hwnd, attribute, ctypes.byref(data), 4) != 0:
            return None
        return int(data.value)
    except (AttributeError, OSError):
        return None


def dwm_text(result):
    """``corner=set:0x0/read:1 ...`` for the log."""
    parts = []
    for name in ("corner", "border", "backdrop"):
        hr, value = result.get(name, (None, None))
        read = "-" if value is None else value
        parts.append(f"{name}=set:{'?' if hr is None else f'0x{hr:x}'}/read:{read}")
    return " ".join(parts)


class _GdiPlus:
    """The few GDI+ flat API calls the wheel needs (gdiplus.dll)."""

    def __init__(self):
        from ctypes import wintypes

        self.dll = ctypes.WinDLL("gdiplus")
        p = ctypes.c_void_p
        pp = ctypes.POINTER(ctypes.c_void_p)
        real = ctypes.c_float
        sigs = {
            "GdipCreateBitmapFromScan0": [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                          p, pp],
            "GdipGetImageGraphicsContext": [p, pp],
            "GdipGraphicsClear": [p, ctypes.c_uint32],
            "GdipSetSmoothingMode": [p, ctypes.c_int],
            "GdipSetTextRenderingHint": [p, ctypes.c_int],
            "GdipSetPixelOffsetMode": [p, ctypes.c_int],
            "GdipCreatePath": [ctypes.c_int, pp],
            "GdipAddPathPolygon": [p, p, ctypes.c_int],
            "GdipAddPathEllipse": [p, real, real, real, real],
            "GdipFillPath": [p, p, p],
            "GdipDrawPath": [p, p, p],
            "GdipDeletePath": [p],
            "GdipCreateSolidFill": [ctypes.c_uint32, pp],
            "GdipDeleteBrush": [p],
            "GdipCreatePen1": [ctypes.c_uint32, real, ctypes.c_int, pp],
            "GdipDeletePen": [p],
            "GdipCreateFontFamilyFromName": [wintypes.LPCWSTR, p, pp],
            "GdipGetGenericFontFamilySansSerif": [pp],
            "GdipCreateFont": [p, real, ctypes.c_int, ctypes.c_int, pp],
            "GdipDeleteFont": [p],
            "GdipDeleteFontFamily": [p],
            "GdipCreateStringFormat": [ctypes.c_int, ctypes.c_ushort, pp],
            "GdipSetStringFormatAlign": [p, ctypes.c_int],
            "GdipSetStringFormatLineAlign": [p, ctypes.c_int],
            "GdipSetStringFormatTrimming": [p, ctypes.c_int],
            "GdipDeleteStringFormat": [p],
            "GdipDrawString": [p, wintypes.LPCWSTR, ctypes.c_int, p, p, p, p],
            "GdipFlush": [p, ctypes.c_int],
            "GdipDeleteGraphics": [p],
            "GdipDisposeImage": [p],
        }
        for name, argtypes in sigs.items():
            function = getattr(self.dll, name)
            function.argtypes = argtypes
            function.restype = ctypes.c_int

        class _Input(ctypes.Structure):
            _fields_ = [("GdiplusVersion", ctypes.c_uint32), ("DebugEventCallback", p),
                        ("SuppressBackgroundThread", wintypes.BOOL),
                        ("SuppressExternalCodecs", wintypes.BOOL)]

        self.dll.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_size_t),
                                            ctypes.POINTER(_Input), p]
        self.dll.GdiplusStartup.restype = ctypes.c_int
        self.token = ctypes.c_size_t(0)
        status = self.dll.GdiplusStartup(ctypes.byref(self.token),
                                         ctypes.byref(_Input(1, None, False, False)), None)
        if status != 0:
            raise OSError(f"GdiplusStartup failed with status {status}")
        self.fonts = {}

    def call(self, name, *args):
        status = getattr(self.dll, name)(*args)
        if status != 0:
            raise OSError(f"{name} failed with status {status}")

    def _new(self, name, *args):
        out = ctypes.c_void_p()
        self.call(name, *args, ctypes.byref(out))
        return out

    def font(self, size_px, bold):
        key = (round(size_px, 1), bool(bold))
        if key not in self.fonts:
            try:
                family = self._new("GdipCreateFontFamilyFromName", FONT_FAMILY, None)
            except OSError:
                family = self._new("GdipGetGenericFontFamilySansSerif")
            # FontStyleBold = 1, UnitPixel = 2.
            self.fonts[key] = (family, self._new("GdipCreateFont", family,
                                                 ctypes.c_float(size_px), 1 if bold else 0, 2))
        return self.fonts[key][1]

    def _path(self, outlines):
        path = self._new("GdipCreatePath", 0)  # FillModeAlternate: inner ring is a hole
        for outline in outlines:
            points = (ctypes.c_float * (2 * len(outline)))(
                *[value for point in outline for value in point])
            self.call("GdipAddPathPolygon", path, points, len(outline))
        return path

    def _fill(self, graphics, path, color):
        brush = self._new("GdipCreateSolidFill", argb(color))
        try:
            self.call("GdipFillPath", graphics, brush, path)
        finally:
            self.dll.GdipDeleteBrush(brush)

    def _stroke(self, graphics, path, color, width):
        pen = self._new("GdipCreatePen1", argb(color), ctypes.c_float(width), 2)
        try:
            self.call("GdipDrawPath", graphics, pen, path)
        finally:
            self.dll.GdipDeletePen(pen)

    def _text(self, graphics, item):
        fmt = self._new("GdipCreateStringFormat", 0, 0)
        brush = self._new("GdipCreateSolidFill", argb(item["color"]))
        try:
            self.call("GdipSetStringFormatAlign", fmt, 1)        # center
            self.call("GdipSetStringFormatLineAlign", fmt, 1)    # center
            self.call("GdipSetStringFormatTrimming", fmt, 3)     # ellipsis
            rect = (ctypes.c_float * 4)(*item["rect"])
            self.call("GdipDrawString", graphics, item["text"], -1,
                      self.font(item["font_px"], item["bold"]), rect, fmt, brush)
        finally:
            self.dll.GdipDeleteBrush(brush)
            self.dll.GdipDeleteStringFormat(fmt)

    def paint(self, bits, width, height, items):
        """Draw ``items`` into the premultiplied 32-bit top-down bitmap ``bits``."""
        # PixelFormat32bppPARGB: GDI+ writes premultiplied alpha, as
        # UpdateLayeredWindow with AC_SRC_ALPHA expects.
        bitmap = self._new("GdipCreateBitmapFromScan0", width, height, width * 4,
                           0x000E200B, bits)
        graphics = None
        try:
            graphics = self._new("GdipGetImageGraphicsContext", bitmap)
            self.call("GdipSetSmoothingMode", graphics, 4)        # AntiAlias
            self.call("GdipSetPixelOffsetMode", graphics, 4)      # Half
            self.call("GdipSetTextRenderingHint", graphics, 4)    # AntiAlias (keeps alpha)
            self.call("GdipGraphicsClear", graphics, 0)
            for item in items:
                if item["kind"] == "text":
                    self._text(graphics, item)
                    continue
                if item["kind"] == "dot":
                    path = self._new("GdipCreatePath", 0)
                    self.call("GdipAddPathEllipse", path, *[ctypes.c_float(v) for v in item["rect"]])
                else:
                    path = self._path(item["outlines"])
                try:
                    self._fill(graphics, path, item["fill"])
                    if item.get("stroke"):
                        self._stroke(graphics, path, item["stroke"], item["stroke_px"])
                finally:
                    self.dll.GdipDeletePath(path)
            self.call("GdipFlush", graphics, 1)
        finally:
            if graphics:
                self.dll.GdipDeleteGraphics(graphics)
            self.dll.GdipDisposeImage(bitmap)


class NativeWheelWindow:
    """The wheel's own layered window on its own thread and message loop.

    ``on_execute(action_id)`` runs on a worker thread and returns the API
    result (``{"ok": ...}``); ``on_page(page)`` reports mouse-wheel paging;
    ``log(text)`` writes diagnostics. All window work happens on the wheel
    thread; other threads use :meth:`show`, :meth:`hide`, :meth:`set_aim`.
    """

    def __init__(self, on_execute, on_page, log):
        if sys.platform != "win32":
            raise OSError("the native wheel needs Windows")
        self.on_execute = on_execute
        self.on_page = on_page
        self.log = log
        self.hwnd = None
        self.dwm = {}
        self.model = None
        self.visible = False
        self._calls = queue.Queue()
        self._ready = threading.Event()
        self._error = None
        self._down = None
        self._tracking = False
        self._thread = threading.Thread(target=self._run, name="overlay-native-wheel",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0):
            raise OSError("native wheel window did not start")
        if self._error is not None:
            raise self._error

    # ---- called from other threads ------------------------------------

    def call(self, function, wait=True):
        if threading.current_thread() is self._thread:
            return function()
        done = {"event": threading.Event(), "result": None, "error": None}

        def run():
            try:
                done["result"] = function()
            except Exception as exc:  # reported to the caller
                done["error"] = exc
            finally:
                done["event"].set()

        self._calls.put(run)
        self.user32.PostMessageW(self.hwnd, WM_APP_RUN, 0, 0)
        if not wait:
            return None
        if not done["event"].wait(CALL_TIMEOUT):
            raise OSError("native wheel call timed out")
        if done["error"] is not None:
            raise done["error"]
        return done["result"]

    def show(self, placement, data):
        return self.call(lambda: self._show(placement, data))

    def hide(self):
        return self.call(self._hide)

    def set_aim(self, slot):
        return self.call(lambda: self._set_aim(slot))

    def close(self):
        if self.hwnd and self._thread.is_alive():
            self.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            self._thread.join(timeout=1.0)

    # ---- wheel thread ------------------------------------------------

    def _bind(self):
        from ctypes import wintypes

        self.wintypes = wintypes
        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        g = self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.wndproc_type = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT,
                                               wintypes.WPARAM, wintypes.LPARAM)

        class _WndClass(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                        ("lpfnWndProc", self.wndproc_type), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]

        class _Track(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]

        class _Blend(ctypes.Structure):
            _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                        ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

        class _BitmapInfo(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD), ("bmiColors", wintypes.DWORD)]

        self.WndClass, self.Track, self.Blend, self.BitmapInfo = (
            _WndClass, _Track, _Blend, _BitmapInfo)
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k.GetModuleHandleW.restype = wintypes.HMODULE
        self.instance = k.GetModuleHandleW(None)
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                     wintypes.LPARAM]
        u.DefWindowProcW.restype = wintypes.LPARAM
        u.RegisterClassExW.argtypes = [ctypes.POINTER(_WndClass)]
        u.RegisterClassExW.restype = wintypes.ATOM
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                      wintypes.HINSTANCE, wintypes.LPVOID]
        u.CreateWindowExW.restype = wintypes.HWND
        u.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        u.LoadCursorW.restype = wintypes.HANDLE
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.PostMessageW.restype = wintypes.BOOL
        u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                  wintypes.UINT]
        u.GetMessageW.restype = wintypes.BOOL
        u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.DispatchMessageW.restype = wintypes.LPARAM
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.TrackMouseEvent.argtypes = [ctypes.POINTER(_Track)]
        u.GetDC.argtypes = [wintypes.HWND]
        u.GetDC.restype = wintypes.HDC
        u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC,
                                          ctypes.POINTER(wintypes.POINT),
                                          ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                                          ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
                                          ctypes.POINTER(_Blend), wintypes.DWORD]
        u.UpdateLayeredWindow.restype = wintypes.BOOL
        g.CreateCompatibleDC.argtypes = [wintypes.HDC]
        g.CreateCompatibleDC.restype = wintypes.HDC
        g.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BitmapInfo), wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE,
                                       wintypes.DWORD]
        g.CreateDIBSection.restype = wintypes.HBITMAP
        g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        g.SelectObject.restype = wintypes.HGDIOBJ
        g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        g.DeleteDC.argtypes = [wintypes.HDC]

    def _run(self):
        try:
            self._bind()
            self.gdiplus = _GdiPlus()
            self._proc = self.wndproc_type(self._wndproc)
            wc = self.WndClass()
            wc.cbSize = ctypes.sizeof(self.WndClass)
            wc.lpfnWndProc = self._proc
            wc.hInstance = self.instance
            wc.hCursor = self.user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND))
            wc.lpszClassName = WINDOW_CLASS
            if not self.user32.RegisterClassExW(ctypes.byref(wc)):
                error = ctypes.get_last_error()
                if error != ERROR_CLASS_ALREADY_EXISTS:
                    raise ctypes.WinError(error)
            hwnd = self.user32.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW, WINDOW_CLASS, WINDOW_TITLE,
                WS_POPUP, 0, 0, 1, 1, None, None, self.instance, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self.hwnd = int(hwnd)
            try:
                self.dwm = dwm_flatten(self.hwnd)
            except (AttributeError, OSError) as exc:
                self.dwm = {}
                self.log(f"overlay native wheel dwm failed ({exc})")
        except Exception as exc:
            self._error = exc if isinstance(exc, OSError) else OSError(str(exc))
            self._ready.set()
            return
        self._ready.set()
        msg = self.wintypes.MSG()
        while self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))

    def exstyle(self):
        return self.user32.GetWindowLongW(self.hwnd, -20) & 0xFFFFFFFF

    def _wndproc(self, hwnd, message, wparam, lparam):
        try:
            if message == WM_APP_RUN:
                while True:
                    try:
                        self._calls.get_nowait()()
                    except queue.Empty:
                        break
                return 0
            if message == WM_MOUSEMOVE:
                self._mouse_move(*self._point(lparam))
                return 0
            if message == WM_MOUSELEAVE:
                self._tracking = False
                self._set_hot(None)
                return 0
            if message == WM_LBUTTONDOWN:
                self._down = self._sector(*self._point(lparam))
                return 0
            if message == WM_LBUTTONUP:
                index = self._sector(*self._point(lparam))
                down, self._down = self._down, None
                if index is not None and index == down:
                    self._click(index)
                return 0
            if message == WM_MOUSEWHEEL:
                delta = ctypes.c_short((wparam >> 16) & 0xFFFF).value
                # Wheel down (negative delta) is the next page, as deltaY > 0.
                if self.model is not None and self.model.scroll(-delta):
                    self._paint()
                    self.on_page(self.model.page)
                return 0
            if message == WM_CLOSE:
                self.user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                self.user32.PostQuitMessage(0)
                return 0
        except Exception as exc:  # never let an exception cross the WndProc
            self.log(f"overlay native wheel message 0x{message:x} failed ({exc})")
            return 0
        return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    @staticmethod
    def _point(lparam):
        return ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value

    def _sector(self, x, y):
        return self.model.sector_at(x, y) if self.model is not None else None

    def _mouse_move(self, x, y):
        if not self._tracking:
            track = self.Track(ctypes.sizeof(self.Track), TME_LEAVE, self.hwnd, 0)
            self._tracking = bool(self.user32.TrackMouseEvent(ctypes.byref(track)))
        self._set_hot(self._sector(x, y))

    def _set_hot(self, index):
        if self.model is not None and self.model.hot != index:
            self.model.hot = index
            self._paint()

    def _set_aim(self, slot):
        if self.model is None:
            return False
        slot = slot if isinstance(slot, int) and not isinstance(slot, bool) else None
        if self.model.aim != slot:
            self.model.aim = slot
            self._paint()
        return True

    def _click(self, index):
        model = self.model
        if model is None or model.busy is not None or not 0 <= index < len(model.slots):
            return
        action_id = model.slots[index]
        model.busy = action_id
        model.error = None
        self._paint()

        def work():
            try:
                result = self.on_execute(action_id)
            except Exception as exc:
                result = {"ok": False, "reason": str(exc) or "failed"}
            self.call(lambda: self._finish(model, action_id, result), wait=False)

        threading.Thread(target=work, name="overlay-native-wheel-action", daemon=True).start()

    def _finish(self, model, action_id, result):
        if model is not self.model:
            return
        model.busy = None
        if isinstance(result, dict) and result.get("ok") is False:
            model.error = (action_id, str(result.get("reason") or result.get("error")
                                          or "rejected")[:60])
        self._paint()

    def _show(self, placement, data):
        self.model = WheelModel(data, placement.width)
        self._down = None
        self.position = (placement.x, placement.y)
        result = self._paint(force=True)
        self.user32.ShowWindow(self.hwnd, SW_SHOWNA)
        # HWND_TOPMOST, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE.
        self.user32.SetWindowPos(self.hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0013)
        self.visible = True
        return result

    def _hide(self):
        self.user32.ShowWindow(self.hwnd, SW_HIDE)
        self.visible = False
        self.model = None
        self._tracking = False
        return True

    def _paint(self, force=False):
        """Render the model and hand the bitmap to DWM (UpdateLayeredWindow)."""
        if self.model is None or (not force and not self.visible):
            return False
        size = self.model.size
        wintypes = self.wintypes
        screen = self.user32.GetDC(None)
        memory = bitmap = old = None
        try:
            memory = self.gdi32.CreateCompatibleDC(screen)
            info = self.BitmapInfo()
            info.biSize = ctypes.sizeof(self.BitmapInfo) - 4
            info.biWidth, info.biHeight = size, -size  # top-down
            info.biPlanes, info.biBitCount = 1, 32
            bits = ctypes.c_void_p()
            bitmap = self.gdi32.CreateDIBSection(memory, ctypes.byref(info), 0,
                                                 ctypes.byref(bits), None, 0)
            if not memory or not bitmap or not bits:
                raise ctypes.WinError(ctypes.get_last_error())
            old = self.gdi32.SelectObject(memory, bitmap)
            self.gdiplus.paint(bits, size, size, wheel_scene(self.model))
            x, y = self.position
            blend = self.Blend(0, 0, 255, AC_SRC_ALPHA)
            if not self.user32.UpdateLayeredWindow(
                    self.hwnd, screen, ctypes.byref(wintypes.POINT(x, y)),
                    ctypes.byref(wintypes.SIZE(size, size)), memory,
                    ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA):
                raise ctypes.WinError(ctypes.get_last_error())
            return True
        finally:
            if old:
                self.gdi32.SelectObject(memory, old)
            if bitmap:
                self.gdi32.DeleteObject(bitmap)
            if memory:
                self.gdi32.DeleteDC(memory)
            self.user32.ReleaseDC(None, screen)
