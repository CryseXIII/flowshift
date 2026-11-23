"""Isolated FlowShift overlay process using authenticated Windows named pipes."""
from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import ipaddress
import json
import math
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from multiprocessing.connection import Listener
from urllib.parse import urljoin, urlparse

import overlay_geometry as geometry
import overlay_native_wheel as native_wheel
import overlay_protocol as protocol
import wheel_gesture as gesture


OVERLAY_WIDTH_CSS = geometry.MODE_SIZES_CSS["clipboard"][0]
OVERLAY_HEIGHT_CSS = geometry.MODE_SIZES_CSS["clipboard"][1]
OVERLAY_TITLE = "FlowShift Overlay"
# Form background. Black because GDI paints it with alpha 0: with DWM
# blur-behind (alpha composition) the form background shows the desktop.
# The colour key #111827 of dev.33 (WS_EX_LAYERED + LWA_COLORKEY) is gone: it
# was set and reported, yet the square stayed visible (Viktor 21.11.).
OVERLAY_FORM_COLOR = "#000000"
# The dev.29-dev.33 form colour; a probe pixel in it points at the window.
OLD_FORM_COLOR = (0x11, 0x18, 0x27)
WS_EX_LAYERED = 0x00080000
# Screen measurement after show (overlay pixel lines in flowshift.log).
PROBE_DELAY = 0.4
PIXEL_TOLERANCE = 10
REGION_RECTS_LOGGED = 16
# A wheel click posts the action to the WebGUI API, as the React wheel did.
ACTION_TIMEOUT = 10.0
SURFACE_NATIVE = "native"
SURFACE_WEBVIEW = "webview"
INFO_PREFIX = protocol.STDERR_INFO_PREFIX
UI_QUEUE_SIZE = 64
INVALID_LOG_INTERVAL = 5.0
DISMISS_POLL_INTERVAL = 0.03
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04
VK_ESCAPE = 0x1B
MOUSE_BUTTONS = (VK_LBUTTON, VK_RBUTTON, VK_MBUTTON)
HIDE_REASONS = frozenset({
    "escape", "outside_click", "focus_lost", "close_button", "action", "runtime",
    "swipe", "swipe_center", "tap_off",
})
# overlay_hidden reason per closing wheel_release outcome.
RELEASE_HIDE_REASONS = {"execute": "swipe", "close_hold": "swipe_center", "close_tap": "tap_off"}


def _positive_timeout(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def _pipe_address(value):
    value = str(value or "")
    prefix = "\\\\.\\pipe\\"
    name = value[len(prefix):] if value.lower().startswith(prefix.lower()) else ""
    if (not name or len(value) > 240 or any(char in name for char in "\\/:\x00")
            or name in (".", "..")):
        raise argparse.ArgumentTypeError(
            r"must be a Windows named pipe such as \\.\pipe\FlowShiftOverlay-<id>"
        )
    return value


def _auth_key(value):
    try:
        raw = base64.b64decode(str(value or ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be valid base64") from exc
    if len(raw) != 32:
        raise argparse.ArgumentTypeError("must decode to exactly 32 bytes")
    return raw


def _overlay_url(value):
    value = str(value or "").strip()
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a valid HTTP(S) URL") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise argparse.ArgumentTypeError("must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or port is not None and not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must not contain credentials or an invalid port")
    hostname = parsed.hostname.lower()
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname == "localhost"
    if not loopback:
        raise argparse.ArgumentTypeError("must use a loopback host")
    return value


def _positive_pid(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a positive process ID") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive process ID")
    return parsed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="FlowShift isolated overlay host")
    parser.add_argument("--pipe", required=True, type=_pipe_address)
    parser.add_argument("--auth-key", required=True, type=_auth_key)
    parser.add_argument("--url", required=True, type=_overlay_url)
    parser.add_argument("--parent-pid", required=True, type=_positive_pid)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--startup-timeout", type=_positive_timeout, default=10.0)
    return parser.parse_args(argv)


class _Bridge:
    """The complete JavaScript-to-Python surface exposed to the overlay."""

    def __init__(self, host):
        self._host = host

    def hide_overlay(self, reason=None):
        reason = reason if reason in ("escape", "close_button") else "escape"
        return self._host.bridge_hide(reason=reason)

    def overlay_event(self, event, data=None):
        return self._host.bridge_event(event, data)


def native_handle_value(native):
    """HWND of pywebview's native form as int, or None.

    pythonnet hands ``Form.Handle`` over either as ``System.IntPtr`` (with
    ``ToInt64``) or already as a Python int, depending on its version.
    """
    try:
        handle = native.Handle
    except Exception:
        return None
    if isinstance(handle, bool):
        return None
    if not isinstance(handle, int):
        try:
            handle = int(handle.ToInt64())
        except Exception:
            return None
    return handle or None


def webview2_control(native):
    """``(control, path)`` of the WebView2 control inside pywebview's form.

    pywebview's WinForms backend sets ``window.native`` to its ``BrowserForm``;
    the EdgeChromium backend hangs off it as ``form.browser`` and keeps the
    ``WebView2`` control in ``web_view``. ``native.webview`` is only a fallback
    for other pywebview versions. Returns ``(None, None)`` if neither exists.
    """
    browser = getattr(native, "browser", None)
    control = getattr(browser, "web_view", None)
    if control is not None:
        return control, "native.browser.web_view"
    control = getattr(native, "webview", None)
    if control is not None:
        return control, "native.webview"
    return None, None


def native_attr_names(native, limit=40):
    """Public attribute names of ``native`` for the log when the lookup fails."""
    if native is None:
        return "none"
    try:
        names = sorted(n for n in dir(native) if not n.startswith("_"))
    except Exception:
        return "?"
    text = ",".join(names[:limit])
    return text + (",..." if len(names) > limit else "")


def color_text(color):
    """``#rrggbb`` of an ``(r, g, b)`` tuple, ``?`` if unknown."""
    if not color:
        return "?"
    return "#{:02x}{:02x}{:02x}".format(*color)


def same_color(first, second, tolerance=PIXEL_TOLERANCE):
    if not first or not second:
        return False
    return max(abs(a - b) for a, b in zip(first, second)) <= tolerance


def pixel_source(shown, below):
    """What a measured pixel most likely is: the desktop or a window colour."""
    if shown is None:
        return "unknown"
    if same_color(shown, below):
        return "below"
    if same_color(shown, OLD_FORM_COLOR, 3):
        return "form_111827"
    if same_color(shown, (0, 0, 0), 3):
        return "black"
    return "painted"


def pixel_report(mode, points, below, shown, hits, own_root):
    """Per-point results and the overall verdict of one screen measurement.

    ``points`` are ``(name, x, y, expect)`` from
    ``overlay_geometry.overlay_probe_points``; ``below`` the colours before
    the show (window hidden), ``shown`` after it; ``hits`` the
    ``window_at`` answer per point. A point expected ``clear`` must look like
    the desktop below and a click there must not reach the overlay.
    """
    rows = []
    clear_total = clear_ok = drawn_total = drawn_ok = through_ok = 0
    painted = []
    for (name, x, y, expect), before, after, hit in zip(points, below, shown, hits):
        source = pixel_source(after, before) if before is not None else "no_baseline"
        hit_root = hit[1] if hit else None
        hit_text = (f"0x{hit[0]:x}:{hit[2] or '?'}:{'overlay' if hit_root == own_root else 'below'}"
                    if hit and hit[0] else "none")
        if expect == "clear":
            clear_total += 1
            if source == "below":
                clear_ok += 1
                result = "clear"
            else:
                result = "painted"
                painted.append(name)
            if hit_root != own_root:
                through_ok += 1
        else:
            drawn_total += 1
            result = "drawn" if source not in ("below", "no_baseline", "unknown") else "missing"
            drawn_ok += result == "drawn"
        rows.append({"name": name, "at": (x, y), "expect": expect, "below": before,
                     "shown": after, "result": result, "source": source, "hit": hit_text})
    if any(row["below"] is None or row["shown"] is None for row in rows):
        verdict = "unknown"
    elif painted:
        corners = [name for name in painted if name.startswith("corner")]
        verdict = "square_visible" if corners else "background_visible"
    elif drawn_total and not drawn_ok:
        verdict = "nothing_drawn"
    else:
        verdict = "ring_only" if mode == "command_wheel" else "panel_only"
    return {"verdict": verdict, "rows": rows, "painted": painted,
            "clear": f"{clear_ok}/{clear_total}", "drawn": f"{drawn_ok}/{drawn_total}",
            "clickthrough": f"{through_ok}/{clear_total}"}


def region_text(rects, limit=REGION_RECTS_LOGGED):
    """Read-back region as ``count`` and the first rectangles ``x,y,wxh``."""
    shown = ";".join(f"{l},{t},{r - l}x{b - t}" for l, t, r, b in rects[:limit])
    more = f";+{len(rects) - limit}" if len(rects) > limit else ""
    return f"rects={len(rects)} [{shown}{more}]"


def hide_reason(value, default="runtime"):
    """Return a known ``overlay_hidden`` reason; unknown values fall back."""
    return value if isinstance(value, str) and value in HIDE_REASONS else default


class DismissPolicy:
    """Decide from polled input state when a visible overlay must close.

    The overlay does not need the foreground to stay open: Windows may refuse
    ``SetForegroundWindow`` to the overlay process (foreground lock), so the
    window can stay visible while the previous application keeps focus.
    It closes only on

    * a new Escape press (``escape``), also when another window has focus;
    * a new mouse-button press whose position is not on the visible shape
      (``outside_click``), including the transparent hub, gaps and corners;
    * another window becoming foreground that is neither the overlay nor the
      window that was foreground before the show, or any foreign window after
      the overlay itself was foreground (``focus_lost``, e.g. Alt+Tab).

    Buttons or Escape already held when the overlay appears are not new
    presses, so the Ctrl+right click that opened the wheel never closes it.
    While the wheel trigger is still held (``holding``, hold-and-swipe) mouse
    presses are ignored: the gesture ends with the trigger release, which the
    runtime reports as ``wheel_release``.
    """

    def __init__(self, overlay_root, previous_root, buttons_down, escape_down, hit_test):
        self.overlay_root = overlay_root
        self.previous_root = previous_root
        self.buttons_down = dict(buttons_down)
        self.escape_down = bool(escape_down)
        self.hit_test = hit_test
        self.overlay_seen = False

    def update(self, foreground_root, buttons_down, escape_down, cursor, holding=False):
        escape_down = bool(escape_down)
        escape_pressed = escape_down and not self.escape_down
        self.escape_down = escape_down
        pressed = [vk for vk, down in buttons_down.items()
                   if down and not self.buttons_down.get(vk, False)]
        self.buttons_down = dict(buttons_down)
        if escape_pressed:
            return "escape"
        if pressed and not holding and cursor is not None and not self.hit_test(*cursor):
            return "outside_click"
        if foreground_root is None:
            return None
        if foreground_root == self.overlay_root:
            self.overlay_seen = True
            return None
        if self.overlay_seen or foreground_root != self.previous_root:
            return "focus_lost"
        return None


class _Win32:
    """Lazily bound user32/gdi32 functions used by the visible host."""

    def __init__(self):
        from ctypes import wintypes

        self.wintypes = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        u, g = self.user32, self.gdi32
        u.GetForegroundWindow.argtypes = []
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.GetAncestor.argtypes = [ctypes.c_void_p, wintypes.UINT]
        u.GetAncestor.restype = ctypes.c_void_p
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u.GetCursorPos.restype = wintypes.BOOL
        u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        u.SetCursorPos.restype = wintypes.BOOL
        u.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL]
        u.SetWindowRgn.restype = ctypes.c_int
        g.CreatePolygonRgn.argtypes = [ctypes.POINTER(wintypes.POINT), ctypes.c_int, ctypes.c_int]
        g.CreatePolygonRgn.restype = ctypes.c_void_p
        g.CreateRectRgn.argtypes = [ctypes.c_int] * 4
        g.CreateRectRgn.restype = ctypes.c_void_p
        g.CombineRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        g.CombineRgn.restype = ctypes.c_int
        g.DeleteObject.argtypes = [ctypes.c_void_p]
        g.DeleteObject.restype = wintypes.BOOL
        u.FindWindowExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                    wintypes.LPCWSTR, wintypes.LPCWSTR]
        u.FindWindowExW.restype = ctypes.c_void_p
        u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.GetWindowRgn.restype = ctypes.c_int
        u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
        u.GetClientRect.restype = wintypes.BOOL
        u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.GetClassNameW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        self.enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p)
        u.EnumChildWindows.argtypes = [ctypes.c_void_p, self.enum_proc, ctypes.c_void_p]
        u.EnumChildWindows.restype = wintypes.BOOL
        u.EnumWindows.argtypes = [self.enum_proc, ctypes.c_void_p]
        u.EnumWindows.restype = wintypes.BOOL
        u.IsWindowVisible.argtypes = [ctypes.c_void_p]
        u.IsWindowVisible.restype = wintypes.BOOL
        g.GetRgnBox.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
        g.GetRgnBox.restype = ctypes.c_int
        g.GetRegionData.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        g.GetRegionData.restype = wintypes.DWORD
        u.GetDC.argtypes = [ctypes.c_void_p]
        u.GetDC.restype = ctypes.c_void_p
        u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.ReleaseDC.restype = ctypes.c_int
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.WindowFromPoint.restype = ctypes.c_void_p
        g.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        g.CreateCompatibleDC.restype = ctypes.c_void_p
        g.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        g.CreateCompatibleBitmap.restype = ctypes.c_void_p
        g.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        g.SelectObject.restype = ctypes.c_void_p
        g.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                             wintypes.DWORD]
        g.BitBlt.restype = wintypes.BOOL
        g.GetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        g.GetPixel.restype = wintypes.DWORD
        g.DeleteDC.argtypes = [ctypes.c_void_p]
        g.DeleteDC.restype = wintypes.BOOL

    def root(self, hwnd):
        if not hwnd:
            return None
        return self.user32.GetAncestor(hwnd, 2) or hwnd

    def foreground_root(self):
        return self.root(self.user32.GetForegroundWindow())

    def key_down(self, vk):
        return bool(self.user32.GetAsyncKeyState(vk) & 0x8000)

    def cursor(self):
        point = self.wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            return None
        return point.x, point.y

    def set_cursor(self, x, y):
        return bool(self.user32.SetCursorPos(int(x), int(y)))

    def own_window(self, title, pid):
        """Top-level window of this process with the given title, or None."""
        handle = None
        while True:
            handle = self.user32.FindWindowExW(None, handle, None, title)
            if not handle:
                return None
            owner = self.wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            if owner.value == pid:
                return int(handle)

    def has_window_shape(self, hwnd):
        """True if Windows holds a non-empty window region for hwnd."""
        return self.region_info(hwnd)[0] > 1

    def region_info(self, hwnd):
        """``(GetWindowRgn result, bounding box)`` of hwnd's window region.

        ERROR (0): no region set, NULLREGION (1): empty; 2/3: clipped.
        """
        region = self.gdi32.CreateRectRgn(0, 0, 0, 0)
        if not region:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            kind = self.user32.GetWindowRgn(hwnd, region)
            rect = self.wintypes.RECT()
            if kind > 1:
                self.gdi32.GetRgnBox(region, ctypes.byref(rect))
            return kind, (rect.left, rect.top, rect.right, rect.bottom)
        finally:
            self.gdi32.DeleteObject(region)

    def region_rects(self, hwnd):
        """``(GetWindowRgn result, rectangles)`` of hwnd's region, read back.

        The rectangles (left, top, right, bottom) are what Windows really
        holds, not what was handed to ``SetWindowRgn``.
        """
        region = self.gdi32.CreateRectRgn(0, 0, 0, 0)
        if not region:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            kind = self.user32.GetWindowRgn(hwnd, region)
            if kind <= 1:
                return kind, []
            size = self.gdi32.GetRegionData(region, 0, None)
            if not size:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(size)
            if not self.gdi32.GetRegionData(region, size, buffer):
                raise ctypes.WinError(ctypes.get_last_error())
            # RGNDATAHEADER: dwSize, iType, nCount, nRgnSize, rcBound (32 bytes).
            count = int.from_bytes(buffer.raw[8:12], "little")
            rect_type = self.wintypes.RECT * count
            rects = rect_type.from_buffer_copy(buffer.raw[32:32 + ctypes.sizeof(rect_type)])
            return kind, [(r.left, r.top, r.right, r.bottom) for r in rects]
        finally:
            self.gdi32.DeleteObject(region)

    def sample_screen(self, left, top, width, height, points):
        """Colours ``(r, g, b)`` the screen really shows at window-relative points.

        One BitBlt of the rectangle from the desktop DC (the DWM-composed
        picture, including layered and DirectComposition content), then
        GetPixel on the copy. A point outside the copy yields None.
        """
        srccopy, captureblt, clr_invalid = 0x00CC0020, 0x40000000, 0xFFFFFFFF
        screen = self.user32.GetDC(None)
        if not screen:
            raise ctypes.WinError(ctypes.get_last_error())
        memory = bitmap = old = None
        try:
            memory = self.gdi32.CreateCompatibleDC(screen)
            bitmap = self.gdi32.CreateCompatibleBitmap(screen, width, height)
            if not memory or not bitmap:
                raise ctypes.WinError(ctypes.get_last_error())
            old = self.gdi32.SelectObject(memory, bitmap)
            if not self.gdi32.BitBlt(memory, 0, 0, width, height, screen, left, top,
                                     srccopy | captureblt):
                raise ctypes.WinError(ctypes.get_last_error())
            colors = []
            for x, y in points:
                value = self.gdi32.GetPixel(memory, x, y)
                colors.append(None if value == clr_invalid else
                              (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF))
            return colors
        finally:
            if old:
                self.gdi32.SelectObject(memory, old)
            if bitmap:
                self.gdi32.DeleteObject(bitmap)
            if memory:
                self.gdi32.DeleteDC(memory)
            self.user32.ReleaseDC(None, screen)

    def window_at(self, x, y):
        """``(hwnd, root hwnd, class)`` of the window a click at x, y reaches."""
        hwnd = self.user32.WindowFromPoint(self.wintypes.POINT(int(x), int(y)))
        if not hwnd:
            return None, None, ""
        name = ctypes.create_unicode_buffer(128)
        self.user32.GetClassNameW(hwnd, name, len(name))
        return int(hwnd), int(self.root(hwnd)), name.value

    def enable_blur_behind(self, hwnd):
        """Let DWM honour the window's alpha channel (HRESULT, 0 = S_OK).

        DWM_BB_ENABLE | DWM_BB_BLURREGION with an empty blur region: nothing
        is blurred, but DWM composes the window with per-pixel alpha, so the
        black (alpha 0) form background and the transparent WebView2 pixels
        show the desktop. The same call winit/tao use for transparent
        WebView2 windows.
        """
        class _BlurBehind(ctypes.Structure):
            _fields_ = [("dwFlags", self.wintypes.DWORD), ("fEnable", self.wintypes.BOOL),
                        ("hRgnBlur", ctypes.c_void_p),
                        ("fTransitionOnMaximized", self.wintypes.BOOL)]

        dwmapi = ctypes.WinDLL("dwmapi")
        dwmapi.DwmEnableBlurBehindWindow.argtypes = [ctypes.c_void_p,
                                                     ctypes.POINTER(_BlurBehind)]
        dwmapi.DwmEnableBlurBehindWindow.restype = ctypes.c_long
        region = self.gdi32.CreateRectRgn(0, 0, -1, -1)
        try:
            blur = _BlurBehind(0x1 | 0x2, True, region, False)
            return int(dwmapi.DwmEnableBlurBehindWindow(hwnd, ctypes.byref(blur)))
        finally:
            if region:
                self.gdi32.DeleteObject(region)

    def flatten_dwm(self, hwnd):
        """No rounded corners, border or backdrop for hwnd (with read-back)."""
        return native_wheel.dwm_flatten(hwnd)

    def dwm_attributes(self, hwnd):
        """``(corner preference, system backdrop)`` as DWM reports them."""
        return (native_wheel.dwm_read(hwnd, native_wheel.DWMWA_WINDOW_CORNER_PREFERENCE),
                native_wheel.dwm_read(hwnd, native_wheel.DWMWA_SYSTEMBACKDROP_TYPE))

    def window_info(self, hwnd):
        """Size, client size, DPI and extended style of hwnd for the log."""
        window = self.wintypes.RECT()
        client = self.wintypes.RECT()
        self.user32.GetWindowRect(hwnd, ctypes.byref(window))
        self.user32.GetClientRect(hwnd, ctypes.byref(client))
        try:
            dpi = int(self.user32.GetDpiForWindow(ctypes.c_void_p(hwnd)))
        except (AttributeError, OSError):
            dpi = 0
        return {
            "size": f"{window.right - window.left}x{window.bottom - window.top}",
            "client": f"{client.right}x{client.bottom}",
            "dpi": dpi,
            "exstyle": f"0x{self.user32.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF:08x}",
        }

    def describe_window(self, hwnd, level):
        """Facts about one window for the log and for placing child regions."""
        name = ctypes.create_unicode_buffer(128)
        self.user32.GetClassNameW(hwnd, name, len(name))
        owner = self.wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        rect = self.wintypes.RECT()
        self.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return {
            "hwnd": int(hwnd),
            "level": level,
            "class": name.value,
            "pid": int(owner.value),
            "rect": (rect.left, rect.top, rect.right, rect.bottom),
            "visible": bool(self.user32.IsWindowVisible(hwnd)),
            "region": self.region_info(hwnd)[0],
            "exstyle": f"0x{self.user32.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF:08x}",
        }

    def child_windows(self, hwnd):
        """Descriptions of every descendant of hwnd (WebView2 hosts and Chrome)."""
        handles = []

        def visit(child, _param):
            handles.append(child)
            return True

        self.user32.EnumChildWindows(hwnd, self.enum_proc(visit), None)
        return [self.describe_window(child, "child") for child in handles]

    def process_windows(self, pid):
        """All top-level windows of pid, each followed by its descendants."""
        tops = []

        def visit(handle, _param):
            owner = self.wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            if owner.value == pid:
                tops.append(handle)
            return True

        self.user32.EnumWindows(self.enum_proc(visit), None)
        windows = []
        for handle in tops:
            windows.append(self.describe_window(handle, "top"))
            windows.extend(self.child_windows(handle))
        return windows

    def set_window_shape(self, hwnd, polygons):
        """Clip the window to the union of polygons (window-relative pixels).

        Pixels outside the region are neither drawn nor hit-tested by Windows,
        so the desktop stays visible there and clicks reach the window below.
        This clips the WebView2 child windows as well, independent of how
        WebView2 composes its content.
        """
        region = self.gdi32.CreateRectRgn(0, 0, 0, 0)
        if not region:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            for polygon in polygons:
                points = (self.wintypes.POINT * len(polygon))(
                    *[self.wintypes.POINT(x, y) for x, y in polygon])
                part = self.gdi32.CreatePolygonRgn(points, len(polygon), 2)
                if not part:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    self.gdi32.CombineRgn(region, region, part, 2)
                finally:
                    self.gdi32.DeleteObject(part)
            result = self.user32.SetWindowRgn(hwnd, region, True)
            if not result:
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.gdi32.DeleteObject(region)
            raise
        # On success the system owns the region handle; it must not be deleted.
        return result


class OverlayHost:
    def __init__(self, args, webview=None):
        self.args = args
        self.webview = webview
        self.listener = None
        self.connection = None
        self.window = None
        self.stop_event = threading.Event()
        self.ui_ready = threading.Event()
        self.ui_queue = queue.Queue(maxsize=UI_QUEUE_SIZE)
        self.send_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.visible = False
        self.mode = None
        self._show_generation = 0
        self._placement = None
        self._data = {}
        self._polygons = []
        self._wheel_page_index = 0
        self._holding = False
        self._aim = None
        self._win32 = None
        self._webview_cleared = False
        self._tree_logged = False
        # The wheel draws in its own layered window (overlay_native_wheel);
        # the clipboard panel and the fallback use the WebView2 form.
        self._native_wheel = None
        self._native_failed = None
        self._native_factory = (native_wheel.NativeWheelWindow
                                if sys.platform == "win32" else None)
        self._surface = None
        self._dismiss_lock = threading.Lock()
        self._invalid_lock = threading.Lock()
        self._invalid_last = 0.0
        self._invalid_suppressed = 0
        self._ipc_thread = None
        self._ui_thread = None
        self._parent_thread = None

    def _log_invalid(self, text):
        now = time.monotonic()
        with self._invalid_lock:
            if now - self._invalid_last < INVALID_LOG_INTERVAL:
                self._invalid_suppressed += 1
                return
            suffix = (f" ({self._invalid_suppressed} similar messages suppressed)"
                      if self._invalid_suppressed else "")
            self._invalid_last = now
            self._invalid_suppressed = 0
        print(f"overlay host invalid IPC: {text}{suffix}", file=sys.stderr, flush=True)

    def _send(self, message_type, payload=None, request_id=None):
        connection = self.connection
        if connection is None:
            return False
        with self.send_lock:
            protocol.send_message(connection, message_type, payload, request_id)
        return True

    def _send_error(self, exc, request_id=None):
        if isinstance(exc, protocol.OverlayProtocolError):
            code = exc.code
            text = exc.message
            request_id = exc.request_id or request_id
        else:
            code = "host_error"
            text = str(exc).strip() or type(exc).__name__
        try:
            self._send("error", {"code": code, "message": text}, request_id)
        except protocol.OverlayProtocolError:
            self.stop_event.set()

    def _placement_for(self, payload):
        area = geometry.get_monitor_work_area(payload["x"], payload["y"])
        return geometry.mode_placement(payload["mode"], payload["x"], payload["y"], area)

    @staticmethod
    def _visible_payload(mode, placement, cursor_moved=False):
        return {
            "mode": mode,
            "x": placement.x,
            "y": placement.y,
            "width": placement.width,
            "height": placement.height,
            "dpi": placement.dpi,
            "cursor_moved": bool(cursor_moved),
        }

    def _win(self):
        if self._win32 is None:
            self._win32 = _Win32()
        return self._win32

    @staticmethod
    def _info(text):
        """Diagnostic line; the controller writes it to flowshift.log as INFO."""
        print(f"{INFO_PREFIX}{text}", file=sys.stderr, flush=True)

    def _window_facts(self, handle):
        try:
            info = self._win().window_info(handle)
        except (AttributeError, OSError, ValueError) as exc:
            return f"window=unknown ({exc})"
        return (f"window={info['size']} client={info['client']} "
                f"window_dpi={info['dpi']} exstyle={info['exstyle']}")

    def _region_facts(self, handle):
        try:
            kind, box = self._win().region_info(handle)
        except (AttributeError, OSError, ValueError) as exc:
            return f"region=unknown ({exc})"
        return f"region={kind} box={','.join(str(value) for value in box)}"

    @staticmethod
    def _window_line(window):
        left, top, right, bottom = window["rect"]
        return (f"overlay window {window['level']} hwnd=0x{window['hwnd']:x} "
                f"class={window['class'] or '?'} pid={window['pid']} "
                f"rect={left},{top},{right - left}x{bottom - top} "
                f"visible={int(window['visible'])} region={window['region']} "
                f"exstyle={window['exstyle']}")

    def _log_window_tree(self, handle):
        """Once per host: every window of this process and its children.

        dev.32 live: the region on the form was set and kept, yet the square
        stayed. This lists which windows really exist (top-level and the
        WebView2/Chrome children, also of other processes), so the log shows
        whether another window draws the square.
        """
        if self._tree_logged:
            return
        self._tree_logged = True
        try:
            windows = self._win().process_windows(os.getpid())
        except (AttributeError, OSError, ValueError) as exc:
            self._info(f"overlay window tree hwnd=0x{handle:x} windows=unknown ({exc})")
            return
        tops = sum(1 for window in windows if window["level"] == "top")
        self._info(f"overlay window tree hwnd=0x{handle:x} host_pid={os.getpid()} "
                   f"top={tops} children={len(windows) - tops}")
        for window in windows:
            self._info(self._window_line(window))

    def _dotnet_colors(self):
        """``(Action, Color.Transparent, form colour)`` from pythonnet; separate for tests."""
        from System import Action
        from System.Drawing import Color, ColorTranslator
        return Action, Color.Transparent, ColorTranslator.FromHtml(OVERLAY_FORM_COLOR)

    @staticmethod
    def _color_text(color):
        try:
            return f"#{int(color.R):02x}{int(color.G):02x}{int(color.B):02x}/a{int(color.A)}"
        except (AttributeError, TypeError, ValueError):
            return "?"

    def _layered(self):
        handle = self._native_hwnd()
        if handle is None:
            return "?"
        try:
            exstyle = int(self._win().window_info(handle)["exstyle"], 16)
        except (AttributeError, OSError, ValueError, KeyError):
            return "?"
        return int(bool(exstyle & WS_EX_LAYERED))

    def _blur_behind(self):
        """DWM alpha composition for the form; ``ok`` or the failure."""
        handle = self._native_hwnd()
        if handle is None:
            return "failed(no hwnd)"
        try:
            hresult = self._win().enable_blur_behind(handle)
        except (AttributeError, OSError) as exc:
            return f"failed({exc})"
        return "ok" if hresult == 0 else f"failed(0x{hresult & 0xFFFFFFFF:08x})"

    def _flatten_form_dwm(self):
        """Switch off the Windows 11 corner rounding and backdrop of the form."""
        handle = self._native_hwnd()
        if handle is None:
            return "dwm=unknown (no hwnd)"
        try:
            return native_wheel.dwm_text(self._win().flatten_dwm(handle))
        except (AttributeError, OSError) as exc:
            return f"dwm=unknown ({exc})"

    def _clear_webview_background(self):
        """Make the overlay window draw nothing outside the visible ring.

        Two layers drew the dark square: the WebView2 control's opaque
        ``DefaultBackgroundColor`` and the form's ``BackColor`` (#111827).
        dev.30-dev.33 tried the window region, then WebView2 alpha 0 plus the
        form colour as ``TransparencyKey`` (layered colour key): every call
        reported success and the square stayed (Viktor 21.11.). A colour key
        and a window region act on the form's GDI surface; WebView2 draws
        through DirectComposition on top of it.
        Now the window is composed with per-pixel alpha instead, as transparent
        WebView2 windows elsewhere do (winit/tao): the WebView2 control is
        created transparent (``transparent=True`` in ``create_window``) and set
        to alpha 0 again here on the UI thread, the form paints black (GDI
        writes alpha 0) without a colour key, and DWM blur-behind with an
        empty blur region makes DWM honour that alpha. The region stays for
        click-through. Whether the screen really shows the desktop is
        measured after the show (``overlay pixel check``).
        """
        if self._webview_cleared:
            return True
        form = getattr(self.window, "native", None)
        control, path = webview2_control(form)
        if control is None:
            self._info("overlay webview background transparent=failed "
                       "(WebView2 control not found; native attrs="
                       f"{native_attr_names(form)})")
            return False
        self._info(f"overlay webview control path={path}")
        result = {}
        try:
            action, transparent, black = self._dotnet_colors()

            def apply():
                # Runs on the UI thread; .NET errors are kept, not raised, so
                # one failing step does not hide the result of the other.
                result["ui_thread"] = not getattr(form, "InvokeRequired", False)
                try:
                    control.DefaultBackgroundColor = transparent
                    result["alpha"] = getattr(control.DefaultBackgroundColor, "A", "?")
                except Exception as exc:  # pythonnet raises .NET exceptions
                    result["webview_error"] = str(exc).splitlines()[0] if str(exc) else repr(exc)
                try:
                    form.BackColor = black
                    result["form"] = self._color_text(form.BackColor)
                    result["form_key"] = self._color_text(form.TransparencyKey)
                except Exception as exc:
                    result["form_error"] = str(exc).splitlines()[0] if str(exc) else repr(exc)

            if getattr(form, "InvokeRequired", False):
                form.Invoke(action(apply))
            else:
                apply()
        except Exception as exc:  # pythonnet raises .NET exceptions
            self._info(f"overlay webview background transparent=failed ({exc})")
            return False
        blur = self._blur_behind()
        dwm = self._flatten_form_dwm()
        webview_ok = "webview_error" not in result and result.get("alpha") == 0
        form_ok = "form_error" not in result
        facts = (f"strategy=alpha ui_thread={int(bool(result.get('ui_thread')))} "
                 f"alpha={result.get('alpha', '?')} "
                 f"form={result.get('form', 'failed')} "
                 f"form_key={result.get('form_key', 'failed')} "
                 f"layered={self._layered()} blur={blur} {dwm}")
        errors = "".join(f" ({result[name]})" for name in ("webview_error", "form_error")
                         if name in result)
        state = "ok" if webview_ok and form_ok and blur == "ok" else "failed"
        self._info(f"overlay webview background transparent={state} {facts}{errors}")
        self._webview_cleared = state == "ok"
        return self._webview_cleared

    def _apply_shape(self, mode, placement, data, page=0):
        """Clip the window to the visible overlay parts and remember them for hit tests."""
        polygons = geometry.overlay_shape_polygons(mode, placement, data, page)
        with self.state_lock:
            self._polygons = polygons
        if self.args.headless:
            return True
        if self._surface == SURFACE_NATIVE:
            # The layered window shows only its own alpha: no region needed,
            # the polygons serve the hit test and the measurement.
            self._info(f"overlay shape native mode={mode} page={page} "
                       f"placement={placement.width}x{placement.height} dpi={placement.dpi} "
                       f"polygons={len(polygons)} region=none")
            return True
        handle = self._native_hwnd()
        if handle is None:
            print("overlay host shape failed: native window handle not found",
                  file=sys.stderr, flush=True)
            return False
        try:
            result = self._win().set_window_shape(handle, polygons)
        except (AttributeError, OSError) as exc:
            print(f"overlay host shape failed: {exc}", file=sys.stderr, flush=True)
            return False
        self._info(
            f"overlay shape set mode={mode} page={page} hwnd=0x{handle:x} "
            f"placement={placement.width}x{placement.height} dpi={placement.dpi} "
            f"scale={placement.scale:g} {self._window_facts(handle)} "
            f"polygons={len(polygons)} setwindowrgn={result} {self._region_facts(handle)} "
            f"{self._shape_children(handle, polygons)}")
        return True

    def _shape_children(self, handle, polygons):
        """Clip the child windows (WebView2 host, Chrome) with the same shape.

        The form's region was set and kept in dev.32 and the square still
        showed, so the windows that actually draw the page get it as well,
        shifted into each child's own coordinates. All children, not only
        visible ones: before the first show the hidden form makes every child
        report invisible.
        """
        try:
            win = self._win()
            children = win.child_windows(handle)
            left, top = win.describe_window(handle, "top")["rect"][:2]
        except (AttributeError, OSError, ValueError, KeyError) as exc:
            return f"children=unknown ({exc})"
        clipped = []
        for child in children:
            dx, dy = left - child["rect"][0], top - child["rect"][1]
            shifted = [[(x + dx, y + dy) for x, y in polygon] for polygon in polygons]
            try:
                win.set_window_shape(child["hwnd"], shifted)
                clipped.append(f"0x{child['hwnd']:x}:{child['class'] or '?'}:ok")
            except (AttributeError, OSError) as exc:
                clipped.append(f"0x{child['hwnd']:x}:{child['class'] or '?'}:failed({exc})")
        return f"children={len(children)} [{' '.join(clipped)}]"

    def _ensure_shape(self, mode, placement, data, page=0):
        """After show: verify Windows kept the region, re-apply it once if not.

        Without the region the square window would catch clicks outside the
        ring and the form's background (``background_color``) would show.
        """
        try:
            handle = self._native_hwnd()
            if handle is not None and self._win().has_window_shape(handle):
                self._info(f"overlay shape after show mode={mode} kept hwnd=0x{handle:x} "
                           f"{self._region_facts(handle)}")
                return True
            self._apply_shape(mode, placement, data, page)
            handle = self._native_hwnd()
            if handle is not None and self._win().has_window_shape(handle):
                print(f"overlay host shape re-applied after show mode={mode}",
                      file=sys.stderr, flush=True)
                return True
        except (AttributeError, OSError) as exc:
            print(f"overlay host shape check failed: {exc}", file=sys.stderr, flush=True)
            return False
        print(f"overlay host shape missing after show mode={mode}",
              file=sys.stderr, flush=True)
        return False

    def _hit_test(self, x, y):
        with self.state_lock:
            placement = self._placement
            polygons = self._polygons
        if placement is None:
            return False
        return geometry.point_in_shape(x - placement.x, y - placement.y, polygons)

    def _center_cursor(self, mode, payload, placement):
        """Keep the cursor on the wheel hub when the wheel was shifted inward."""
        if mode != "command_wheel":
            return False
        center_x, center_y = geometry.placement_center(placement)
        if abs(center_x - payload["x"]) <= 1 and abs(center_y - payload["y"]) <= 1:
            return False
        if self.args.headless:
            return True
        try:
            return self._win().set_cursor(center_x, center_y)
        except (AttributeError, OSError):
            return False

    def _show(self, payload):
        if payload["target"]["kind"] != "local":
            raise protocol.OverlayProtocolError(
                "unsupported_target", "remote overlays are not supported locally"
            )
        placement = self._placement_for(payload)
        mode = payload["mode"]
        with self.state_lock:
            self._placement = placement
            self._data = payload["data"]
            self._wheel_page_index = 0
            self._holding = mode == "command_wheel" and payload["data"].get("hold") is True
            self._aim = None
        previous_root = None
        probe_points, below = [], None
        native = self._native_wheel_window() if mode == "command_wheel" else None
        if native is not None:
            shown = self._show_native(native, mode, placement, payload["data"])
            if shown is None:
                native = None
            else:
                previous_root, probe_points, below = shown
        if native is None and not self.args.headless:
            if self._surface == SURFACE_NATIVE:
                self._hide_native()
            self._surface = SURFACE_WEBVIEW
            update_payload = {
                "mode": payload["mode"],
                "target": payload["target"],
                "x": placement.x,
                "y": placement.y,
                "dpi": placement.dpi,
                "scale": placement.scale,
                "data": payload["data"],
            }
            update = json.dumps(update_payload, ensure_ascii=True, allow_nan=False,
                                 separators=(",", ":"))
            self._position_window(placement)
            self._clear_webview_background()
            self._apply_shape(mode, placement, payload["data"])
            try:
                previous_root = self._win().foreground_root()
            except (AttributeError, OSError):
                previous_root = None
            self.window.evaluate_js(
                "if (window.flowshiftOverlay && "
                "typeof window.flowshiftOverlay.update === 'function') {"
                f"window.flowshiftOverlay.update({update});"
                "}"
            )
            probe_points = self._probe_points(mode, placement, payload["data"])
            with self.state_lock:
                was_visible = self.visible
            # The desktop under the hidden window, compared after the show.
            below = None if was_visible else self._sample(placement, probe_points)
            self.window.show()
            self._ensure_shape(mode, placement, payload["data"])
            handle = self._native_hwnd()
            if handle is not None:
                self._log_window_tree(handle)
            self._activate_window()
        elif native is None:
            self._apply_shape(mode, placement, payload["data"])
        cursor_moved = self._center_cursor(mode, payload, placement)
        with self.state_lock:
            self.visible = True
            self.mode = mode
            self._show_generation += 1
            generation = self._show_generation
        if not self.args.headless:
            self._start_dismiss_watch(generation, previous_root)
            self._start_probe(generation, mode, placement, probe_points, below)
        return self._visible_payload(mode, placement, cursor_moved)

    def _native_wheel_window(self):
        """The native wheel window, created on first use; None means WebView2.

        Not in headless mode, not off Windows, and not again after it failed
        to start once in this host (logged, the WebView2 wheel stays usable).
        """
        if self.args.headless or self._native_failed is not None:
            return None
        if self._native_wheel is not None:
            return self._native_wheel
        if self._native_factory is None:
            self._native_failed = "not available"
            return None
        try:
            wheel = self._native_factory(self._native_execute, self._native_page, self._info)
        except Exception as exc:  # ctypes.ArgumentError etc.: fall back, never break the show
            self._native_failed = str(exc) or type(exc).__name__
            self._info(f"overlay native wheel unavailable ({self._native_failed}) "
                       "surface=webview")
            return None
        self._native_wheel = wheel
        try:
            exstyle = f"0x{wheel.exstyle():08x}"
        except (AttributeError, OSError):
            exstyle = "?"
        self._info(f"overlay native wheel ready hwnd=0x{wheel.hwnd:x} exstyle={exstyle} "
                   f"{native_wheel.dwm_text(wheel.dwm)} paint=updatelayeredwindow")
        return wheel

    def _show_native(self, native, mode, placement, data):
        """Show the wheel in the layered window.

        Returns ``(previous_root, probe_points, below)`` or None when the
        native window failed; the caller then shows the WebView2 wheel.
        """
        with self.state_lock:
            was_visible = self.visible
        previous = self._surface
        if previous == SURFACE_WEBVIEW and self.window is not None:
            if was_visible:
                self._notify_react_hidden()
            self.window.hide()
        self._surface = SURFACE_NATIVE
        self._apply_shape(mode, placement, data)
        try:
            previous_root = self._win().foreground_root()
        except (AttributeError, OSError):
            previous_root = None
        probe_points = self._probe_points(mode, placement, data)
        below = (None if was_visible and previous == SURFACE_NATIVE
                 else self._sample(placement, probe_points))
        try:
            native.show(placement, data)
        except Exception as exc:  # ctypes.ArgumentError etc.: fall back to WebView2
            self._native_failed = str(exc) or type(exc).__name__
            self._info(f"overlay native wheel show failed ({self._native_failed}) "
                       "surface=webview")
            self._hide_native()
            self._surface = previous if previous != SURFACE_NATIVE else None
            return None
        self._info(f"overlay native wheel shown hwnd=0x{native.hwnd:x} "
                   f"rect={placement.x},{placement.y},{placement.width}x{placement.height} "
                   f"dpi={placement.dpi} {self._dwm_facts(native.hwnd)}")
        self._log_window_tree(native.hwnd)
        self._activate_window()
        return previous_root, probe_points, below

    def _hide_native(self):
        if self._native_wheel is None:
            return
        try:
            self._native_wheel.hide()
        except Exception as exc:
            print(f"overlay host native wheel hide failed: {exc}", file=sys.stderr, flush=True)

    def _dwm_facts(self, handle):
        """Corner preference and backdrop as DWM reports them for hwnd."""
        try:
            corner, backdrop = self._win().dwm_attributes(handle)
        except (AttributeError, OSError) as exc:
            return f"dwm=unknown ({exc})"
        text = lambda value: "-" if value is None else value  # noqa: E731
        return f"dwm_corner={text(corner)} dwm_backdrop={text(backdrop)}"

    def _native_execute(self, action_id):
        """Run a clicked wheel action through the WebGUI API, as React did."""
        url = urljoin(self.args.url, "/api/actions/execute")
        body = json.dumps({"action_id": action_id, "context": {"source": "command_wheel"}})
        # The API accepts POSTs only from the WebGUI origin; the overlay page
        # has that origin, so the native wheel names it like the browser did.
        parsed = urlparse(self.args.url)
        request = urllib.request.Request(url, data=body.encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Origin": f"{parsed.scheme}://{parsed.netloc}"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=ACTION_TIMEOUT) as response:
                result = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read() or b"{}")
            except ValueError:
                result = {}
            if exc.code != 409 or not isinstance(result, dict):
                result = {"ok": False, "reason": (result.get("message") or result.get("error")
                                                  if isinstance(result, dict) else None)
                          or f"HTTP {exc.code}"}
        except (OSError, ValueError) as exc:
            result = {"ok": False, "reason": str(exc) or "failed"}
        if not isinstance(result, dict):
            result = {"ok": False, "reason": "invalid response"}
        self._info(f"overlay native wheel action id={action_id} "
                   f"ok={int(result.get('ok') is not False)}")
        return result

    def _native_page(self, page):
        """Mouse-wheel paging in the native wheel: same path as React's report."""
        return self.bridge_event("wheel_page", {"page": page})

    def _probe_points(self, mode, placement, data, page=0):
        try:
            return geometry.overlay_probe_points(mode, placement, data, page)
        except ValueError:
            return []

    def _sample(self, placement, points):
        """Screen colours at the probe points, or None (logged) on failure."""
        if not points:
            return None
        try:
            return self._win().sample_screen(
                placement.x, placement.y, placement.width, placement.height,
                [(x, y) for _name, x, y, _expect in points])
        except (AttributeError, OSError, ValueError) as exc:
            self._info(f"overlay pixel sample failed ({exc})")
            return None

    def _start_probe(self, generation, mode, placement, points, below):
        threading.Thread(
            target=self._probe_after_show, args=(generation, mode, placement, points, below),
            name="overlay-pixel-probe", daemon=True,
        ).start()

    def _probe_after_show(self, generation, mode, placement, points, below):
        """Measure, after WebView2 painted, what the screen really shows."""
        if self.stop_event.wait(PROBE_DELAY):
            return None
        with self.state_lock:
            current = self.visible and self._show_generation == generation
            page = self._wheel_page_index
            data = self._data
        if not current:
            self._info(f"overlay pixel check mode={mode} skipped "
                       "(hidden before the measurement)")
            return None
        # Same coordinates; the expectation follows the page shown now.
        points = self._probe_points(mode, placement, data, page) or points
        return self._measure(mode, placement, points, below)

    def _measure(self, mode, placement, points, below):
        """Log every probe pixel, the click target there and the read-back region.

        dev.30-dev.33 logged only return values (SetWindowRgn=1, alpha=0,
        layered=1) while Viktor saw the square. These lines show the screen:
        ``below`` is the desktop before the show, ``shown`` the same pixel with
        the overlay visible; ``hit`` the window a click there reaches.
        """
        shown = self._sample(placement, points)
        if shown is None:
            return None
        handle = self._native_hwnd()
        win = self._win()
        hits = []
        for _name, x, y, _expect in points:
            try:
                hits.append(win.window_at(placement.x + x, placement.y + y))
            except (AttributeError, OSError):
                hits.append(None)
        try:
            own_root = win.root(handle) if handle else None
        except (AttributeError, OSError):
            own_root = handle
        report = pixel_report(mode, points, below or [None] * len(points), shown, hits,
                              own_root)
        for row in report["rows"]:
            x, y = row["at"]
            self._info(
                f"overlay pixel mode={mode} point={row['name']} at={x},{y} "
                f"screen={placement.x + x},{placement.y + y} expect={row['expect']} "
                f"below={color_text(row['below'])} shown={color_text(row['shown'])} "
                f"result={row['result']} source={row['source']} hit={row['hit']}")
        self._info(
            f"overlay pixel check mode={mode} verdict={report['verdict']} "
            f"strategy={self._strategy()} "
            f"rect={placement.x},{placement.y},{placement.width}x{placement.height} "
            f"clear={report['clear']} clickthrough={report['clickthrough']} "
            f"drawn={report['drawn']} painted={','.join(report['painted']) or '-'} "
            f"{self._region_rects_facts(handle)} "
            f"{self._dwm_facts(handle) if handle is not None else 'dwm=unknown (no hwnd)'}")
        return report

    def _strategy(self):
        """How the shown overlay gets its transparency, for the log."""
        return "native_layered" if self._surface == SURFACE_NATIVE else "alpha"

    def _region_rects_facts(self, handle):
        if handle is None:
            return "region=unknown (no hwnd)"
        try:
            kind, rects = self._win().region_rects(handle)
        except (AttributeError, OSError, ValueError) as exc:
            return f"region=unknown ({exc})"
        return f"region={kind} {region_text(rects)}"

    def _native_hwnd(self):
        """HWND of the shown overlay surface.

        The native wheel window while the wheel is drawn natively, otherwise
        the WebView2 form; falls back to this process's titled window.
        """
        if self._surface == SURFACE_NATIVE and self._native_wheel is not None:
            return self._native_wheel.hwnd
        handle = native_handle_value(getattr(self.window, "native", None))
        if handle is not None or self.args.headless:
            return handle
        try:
            return self._win().own_window(OVERLAY_TITLE, os.getpid())
        except (AttributeError, OSError):
            return None

    def _activate_window(self):
        """Bring the shown overlay to the foreground so focus loss is meaningful."""
        handle = self._native_hwnd()
        if handle is None:
            return
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SetForegroundWindow(ctypes.c_void_p(handle))
        except (AttributeError, OSError):
            pass

    def _start_dismiss_watch(self, generation, previous_root):
        thread = threading.Thread(
            target=self._dismiss_watch, args=(generation, previous_root),
            name="overlay-dismiss-watch", daemon=True,
        )
        thread.start()

    def _dismiss_watch(self, generation, previous_root):
        """Poll foreground, Escape and mouse buttons until the overlay must close.

        Polling ``GetAsyncKeyState`` works without the overlay owning the
        foreground and without a global hook; a newer show supersedes this
        watcher. The decision itself is :class:`DismissPolicy`.
        """
        handle = self._native_hwnd()
        if handle is None:
            return
        try:
            win = self._win()
            policy = DismissPolicy(
                overlay_root=win.root(handle),
                previous_root=previous_root,
                buttons_down={vk: win.key_down(vk) for vk in MOUSE_BUTTONS},
                escape_down=win.key_down(VK_ESCAPE),
                hit_test=self._hit_test,
            )
        except (AttributeError, OSError) as exc:
            print(f"overlay host dismiss watch unavailable: {exc}", file=sys.stderr, flush=True)
            return
        while not self.stop_event.wait(DISMISS_POLL_INTERVAL):
            with self.state_lock:
                if not self.visible or self._show_generation != generation:
                    return
                holding = self._holding
            try:
                cursor = win.cursor()
                reason = policy.update(
                    win.foreground_root(),
                    {vk: win.key_down(vk) for vk in MOUSE_BUTTONS},
                    win.key_down(VK_ESCAPE),
                    cursor,
                    holding=holding,
                )
            except (AttributeError, OSError) as exc:
                print(f"overlay host dismiss watch failed: {exc}", file=sys.stderr, flush=True)
                return
            if holding and not reason:
                self._update_aim(cursor, generation)
            if reason:
                self.bridge_hide(reason=reason, generation=generation)
                return

    def _cursor(self):
        if self.args.headless:
            return None
        try:
            return self._win().cursor()
        except (AttributeError, OSError):
            return None

    def _swipe_target(self, cursor):
        """``(slot_index, action_id)`` in the direction of ``cursor``, or ``(None, None)``.

        Only the direction from the hub counts, so a cursor beyond the ring
        still selects; inside the dead zone nothing is selected.
        """
        with self.state_lock:
            placement = self._placement
            data = self._data
            page = self._wheel_page_index
        if placement is None or cursor is None:
            return None, None
        pages = geometry.wheel_page_slots(data)
        if not pages:
            return None, None
        slots = pages[page] if 0 <= page < len(pages) else pages[0]
        center_x, center_y = geometry.placement_center(placement)
        dead_zone = geometry.css_to_physical(gesture.DEAD_ZONE_CSS, placement.dpi)
        slot = gesture.aim_slot(cursor[0] - center_x, cursor[1] - center_y,
                                len(slots), dead_zone)
        return (None, None) if slot is None else (slot, slots[slot])

    def _push_aim(self, slot):
        if self.args.headless:
            return
        if self._surface == SURFACE_NATIVE and self._native_wheel is not None:
            try:
                self._native_wheel.set_aim(slot)
            except Exception as exc:
                print(f"overlay host aim update failed: {exc}", file=sys.stderr, flush=True)
            return
        if self.window is None:
            return
        value = "null" if slot is None else str(int(slot))
        try:
            self.window.evaluate_js(
                "if (window.flowshiftOverlay && "
                "typeof window.flowshiftOverlay.aim === 'function') {"
                f"window.flowshiftOverlay.aim({value});"
                "}"
            )
        except Exception as exc:
            print(f"overlay host aim update failed: {exc}", file=sys.stderr, flush=True)

    def _update_aim(self, cursor, generation):
        """Highlight the sector in the swipe direction while the trigger is held."""
        with self.state_lock:
            data = self._data
        if not gesture.trigger_from_payload(data)["hold_swipe"]:
            return
        slot, _action = self._swipe_target(cursor)
        with self.state_lock:
            if (not self.visible or not self._holding or self._show_generation != generation
                    or slot == self._aim):
                return
            self._aim = slot
        self._push_aim(slot)

    def _wheel_release(self, held_ms):
        """The runtime reports the release of the wheel trigger (hold-and-swipe).

        Returns the ``wheel_released`` payload. A tap leaves the wheel open for
        clicking (or closes it when tapping is off); a hold runs the sector in
        the cursor direction or closes inside the dead zone. The action itself
        is executed by the runtime, which receives the id.
        """
        with self._dismiss_lock:
            with self.state_lock:
                if not self.visible or self.mode != "command_wheel" or not self._holding:
                    return {"outcome": gesture.OUTCOME_IGNORED, "action_id": None}
                self._holding = False
                data = self._data
                mode = self.mode
                had_aim = self._aim is not None
                self._aim = None
            trigger = gesture.trigger_from_payload(data)
            slot, action_id = self._swipe_target(self._cursor())
            outcome = gesture.release_outcome(held_ms, trigger, slot)
            if outcome == gesture.OUTCOME_KEEP_OPEN:
                if had_aim:
                    self._push_aim(None)
                return {"outcome": outcome, "action_id": None}
            if outcome == gesture.OUTCOME_EXECUTE:
                reason = RELEASE_HIDE_REASONS["execute"]
            elif held_ms < trigger["hold_ms"]:
                reason = RELEASE_HIDE_REASONS["close_tap"]
            else:
                reason = RELEASE_HIDE_REASONS["close_hold"]
            self._run_ui("hide")
            self._send("overlay_hidden", {"reason": reason, "mode": mode})
            return {"outcome": outcome,
                    "action_id": action_id if outcome == gesture.OUTCOME_EXECUTE else None}

    def _position_window(self, placement):
        """Apply a physical-pixel rectangle after Per-Monitor-V2 setup."""
        try:
            from ctypes import wintypes

            handle = self._native_hwnd()
            if handle is None:
                raise ValueError("native window handle not found")
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.UINT,
            ]
            user32.SetWindowPos.restype = wintypes.BOOL
            if not user32.SetWindowPos(
                    wintypes.HWND(handle), wintypes.HWND(-1), placement.x, placement.y,
                    placement.width, placement.height, 0x0200):
                raise ctypes.WinError(ctypes.get_last_error())
        except (AttributeError, TypeError, ValueError):
            self.window.resize(placement.width, placement.height)
            self.window.move(placement.x, placement.y)

    def _verify_react_ready(self, *_args):
        deadline = time.monotonic() + self.args.startup_timeout
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            try:
                ready = self.window.evaluate_js(
                    "Boolean(window.flowshiftOverlay && "
                    "typeof window.flowshiftOverlay.update === 'function')"
                )
                if ready:
                    self.ui_ready.set()
                    return
            except Exception:
                pass
            time.sleep(0.05)

    def _keep_hidden_on_navigation(self, *_args):
        """Runs after pywebview built the transparent form, before it starts.

        pywebview's EdgeChromium backend reads ``window.transparent`` again
        in ``on_navigation_start`` and then calls ``form.Show()`` and
        ``Activate()``: the hidden overlay would pop up and take the focus
        at startup. The transparent WebView2 background is already set by
        then, so the flag is cleared for everything after construction.
        """
        try:
            self.window.transparent = False
        except AttributeError:
            pass

    def _on_loaded(self, *_args):
        threading.Thread(
            target=self._verify_react_ready,
            name="overlay-react-ready",
            daemon=True,
        ).start()

    def _watch_parent(self):
        synchronize = 0x00100000
        wait_object_0 = 0
        handle = ctypes.windll.kernel32.OpenProcess(
            synchronize, False, self.args.parent_pid,
        )
        if not handle:
            return
        try:
            result = ctypes.windll.kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        if result != wait_object_0 or self.stop_event.is_set():
            return
        self.stop_event.set()
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
        if not self.args.headless and self.window is not None:
            try:
                self._run_ui("destroy", timeout=1.0)
            except Exception:
                pass

    def _hide(self, notify=True):
        with self.state_lock:
            was_visible = self.visible
            self.visible = False
            self.mode = None
        if not self.args.headless and self._surface == SURFACE_NATIVE:
            self._hide_native()
            return
        if not self.args.headless and self.window is not None:
            # Unmount the React view first so a hidden overlay stops polling.
            # Skipped on destroy, where the page may already be gone.
            if notify and was_visible:
                self._notify_react_hidden()
            self.window.hide()

    def _notify_react_hidden(self):
        try:
            self.window.evaluate_js(
                "if (window.flowshiftOverlay && "
                "typeof window.flowshiftOverlay.hide === 'function') {"
                "window.flowshiftOverlay.hide();"
                "}"
            )
        except Exception as exc:
            print(f"overlay host hide notify failed: {exc}", file=sys.stderr, flush=True)

    def _ui_worker(self):
        while True:
            command, payload, done = self.ui_queue.get()
            try:
                if command == "show":
                    done["result"] = self._show(payload)
                elif command == "hide":
                    self._hide()
                elif command == "destroy":
                    self._hide(notify=False)
                    if self._native_wheel is not None:
                        self._native_wheel.close()
                    if self.window is not None:
                        self.window.destroy()
                elif command == "stop":
                    pass
                else:
                    raise ValueError(f"unknown UI command: {command}")
            except Exception as exc:
                done["error"] = exc
            finally:
                done["event"].set()
                self.ui_queue.task_done()
            if command in ("destroy", "stop"):
                return

    def _run_ui(self, command, payload=None, timeout=None):
        if self.args.headless:
            if command == "show":
                return self._show(payload)
            if command in ("hide", "destroy"):
                self._hide()
                return None
        done = {"event": threading.Event(), "result": None, "error": None}
        try:
            self.ui_queue.put((command, payload, done), timeout=0.25)
        except queue.Full as exc:
            raise protocol.OverlayProtocolError("ui_busy", "overlay UI queue is full") from exc
        if not done["event"].wait(timeout or self.args.startup_timeout):
            raise protocol.OverlayProtocolError("ui_timeout", "overlay UI command timed out")
        if done["error"] is not None:
            raise done["error"]
        return done["result"]

    def _handle_message(self, message):
        message_type = message["type"]
        request_id = message["request_id"]
        if message_type == "hello":
            if message["payload"]["role"] != "runtime":
                raise protocol.OverlayProtocolError(
                    "invalid_handshake", "hello must identify the runtime", request_id
                )
            if not self.ui_ready.wait(self.args.startup_timeout):
                raise protocol.OverlayProtocolError(
                    "startup_timeout", "overlay UI did not become ready", request_id
                )
            self._send("ready", {"pid": os.getpid()}, request_id)
        elif message_type == "ping":
            self._send("pong", {}, request_id)
        elif message_type == "show_overlay":
            result = self._run_ui("show", message["payload"])
            self._send("overlay_visible", result, request_id)
        elif message_type == "hide_overlay":
            reason = hide_reason(message["payload"].get("reason"))
            with self._dismiss_lock:
                with self.state_lock:
                    mode = self.mode
                self._run_ui("hide")
            self._send("overlay_hidden", {"reason": reason, "mode": mode}, request_id)
        elif message_type == "wheel_release":
            result = self._wheel_release(message["payload"]["held_ms"])
            self._send("wheel_released", result, request_id)
        elif message_type == "shutdown":
            self._send("shutdown", {}, request_id)
            if not self.args.headless:
                try:
                    self._run_ui("destroy", timeout=2.0)
                except Exception:
                    pass
            self.stop_event.set()
        else:
            raise protocol.OverlayProtocolError(
                "unexpected_message", f"host cannot accept {message_type}", request_id
            )

    def _ipc_loop(self):
        try:
            self.connection = self.listener.accept()
            self._send("hello", {"role": "host", "pid": os.getpid()})
            while not self.stop_event.is_set():
                try:
                    message = protocol.receive_message(self.connection)
                except protocol.OverlayProtocolError as exc:
                    if exc.code == "transport_error":
                        break
                    self._log_invalid(f"{exc.code}: {exc.message}")
                    self._send_error(exc)
                    continue
                try:
                    self._handle_message(message)
                except Exception as exc:
                    self._log_invalid(str(exc))
                    self._send_error(exc, message.get("request_id"))
        except (OSError, EOFError, protocol.OverlayProtocolError) as exc:
            if not self.stop_event.is_set():
                self._log_invalid(str(exc))
        finally:
            if (not self.args.headless and self.window is not None
                    and self._ui_thread is not None and self._ui_thread.is_alive()):
                try:
                    self._run_ui("destroy", timeout=1.0)
                except Exception:
                    pass
            self.stop_event.set()

    def bridge_hide(self, reason="escape", generation=None):
        """Hide on a user dismissal and report ``overlay_hidden`` with its reason.

        Serialized so concurrent dismissals (Escape in React plus the watcher)
        report exactly one ``overlay_hidden`` per show.
        """
        if self.stop_event.is_set():
            return False
        reason = hide_reason(reason, default="escape")
        with self._dismiss_lock:
            with self.state_lock:
                if not self.visible:
                    return True
                if generation is not None and generation != self._show_generation:
                    return True
                mode = self.mode
            if self.args.headless:
                self._hide()
            else:
                done = {"event": threading.Event(), "result": None, "error": None}
                try:
                    self.ui_queue.put_nowait(("hide", None, done))
                except queue.Full:
                    return False
                if not done["event"].wait(1.0) or done["error"] is not None:
                    return False
            try:
                self._send("overlay_hidden", {"reason": reason, "mode": mode})
            except protocol.OverlayProtocolError:
                return False
        return True

    def _wheel_page(self, data):
        """React paged the wheel: refit the window shape to that page's sectors."""
        page = data.get("page")
        if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page < 64:
            return False
        with self.state_lock:
            if not self.visible or self.mode != "command_wheel":
                return False
            placement = self._placement
            wheel_data = self._data
            self._wheel_page_index = page
        self._apply_shape("command_wheel", placement, wheel_data, page)
        return True

    def bridge_event(self, event, data=None):
        if not isinstance(event, str) or not event.strip() or not isinstance(data or {}, dict):
            return False
        if event.strip() == "wheel_page":
            self._wheel_page(data or {})
        try:
            self._send("overlay_event", {"event": event.strip(), "data": data or {}})
        except protocol.OverlayProtocolError:
            return False
        return True

    def run(self):
        self.listener = Listener(
            self.args.pipe, family="AF_PIPE", authkey=self.args.auth_key,
        )
        try:
            self._parent_thread = threading.Thread(
                target=self._watch_parent, name="overlay-parent-watch", daemon=True,
            )
            self._parent_thread.start()
            if self.args.headless:
                self.ui_ready.set()
                self._ipc_loop()
                return 0

            self.window = self.webview.create_window(
                OVERLAY_TITLE,
                self.args.url,
                width=OVERLAY_WIDTH_CSS,
                height=OVERLAY_HEIGHT_CSS,
                resizable=False,
                frameless=True,
                on_top=True,
                # transparent=True creates the WebView2 control with alpha 0
                # before CoreWebView2 starts (_clear_webview_background).
                # pywebview would also show the window on every navigation;
                # _keep_hidden_on_navigation switches that off before the
                # first one.
                transparent=True,
                background_color=OVERLAY_FORM_COLOR,
                js_api=_Bridge(self),
                hidden=True,
            )
            self.window.events.before_show += self._keep_hidden_on_navigation
            self.window.events.loaded += self._on_loaded
            self._ui_thread = threading.Thread(
                target=self._ui_worker, name="overlay-host-ui", daemon=True,
            )
            self._ipc_thread = threading.Thread(
                target=self._ipc_loop, name="overlay-host-ipc", daemon=True,
            )
            self._ui_thread.start()
            self._ipc_thread.start()
            self.webview.start(gui="edgechromium", debug=False)
            self.stop_event.set()
            if self._ui_thread.is_alive():
                done = {"event": threading.Event(), "result": None, "error": None}
                try:
                    self.ui_queue.put_nowait(("stop", None, done))
                except queue.Full:
                    pass
            self._ipc_thread.join(timeout=2.0)
            self._ui_thread.join(timeout=2.0)
            return 0
        finally:
            self.stop_event.set()
            if self.connection is not None:
                try:
                    self.connection.close()
                except OSError:
                    pass
            if self.listener is not None:
                try:
                    self.listener.close()
                except OSError:
                    pass


def main(argv=None):
    args = parse_args(argv)
    if sys.platform != "win32":
        print("overlay host requires Windows named pipes", file=sys.stderr)
        return 2

    webview = None
    if not args.headless:
        geometry.set_per_monitor_v2_awareness()
        try:
            import webview as imported_webview
        except ImportError as exc:
            print(
                f"overlay host requires pywebview in normal mode: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return 3
        webview = imported_webview

    try:
        return OverlayHost(args, webview=webview).run()
    except (OSError, ValueError) as exc:
        print(f"overlay host failed: {exc}", file=sys.stderr, flush=True)
        return 4


if __name__ == "__main__":
    sys.exit(main())
