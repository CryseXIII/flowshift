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
from multiprocessing.connection import Listener
from urllib.parse import urlparse

import overlay_geometry as geometry
import overlay_protocol as protocol
import wheel_gesture as gesture


OVERLAY_WIDTH_CSS = geometry.MODE_SIZES_CSS["clipboard"][0]
OVERLAY_HEIGHT_CSS = geometry.MODE_SIZES_CSS["clipboard"][1]
OVERLAY_TITLE = "FlowShift Overlay"
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
        g.GetRgnBox.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
        g.GetRgnBox.restype = ctypes.c_int

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

    def child_windows(self, hwnd):
        """``(class name, owning pid)`` of every descendant window of hwnd."""
        children = []

        def visit(child, _param):
            name = ctypes.create_unicode_buffer(128)
            self.user32.GetClassNameW(child, name, len(name))
            owner = self.wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(child, ctypes.byref(owner))
            children.append((name.value, int(owner.value)))
            return True

        self.user32.EnumChildWindows(hwnd, self.enum_proc(visit), None)
        return children

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

    def _log_window_tree(self, handle):
        """Once per host: which windows draw the overlay (WebView2 children)."""
        if self._tree_logged:
            return
        self._tree_logged = True
        try:
            children = self._win().child_windows(handle)
        except (AttributeError, OSError, ValueError) as exc:
            self._info(f"overlay window tree hwnd=0x{handle:x} children=unknown ({exc})")
            return
        listed = " ".join(f"{name}/pid{pid}" for name, pid in children) or "none"
        self._info(f"overlay window tree hwnd=0x{handle:x} host_pid={os.getpid()} "
                   f"children={len(children)} [{listed}]")

    def _dotnet_transparent(self):
        """``(Action, Color.Transparent)`` from pythonnet; separate for tests."""
        from System import Action
        from System.Drawing import Color
        return Action, Color.Transparent

    def _clear_webview_background(self):
        """Make WebView2 draw nothing where the page is transparent.

        pywebview gives the WebView2 control an opaque ``DefaultBackgroundColor``
        (``background_color``, #111827). WebView2 composes its content through
        DirectComposition, which the window region does not reliably clip, so
        that colour filled the whole square even with the region in place.
        With alpha 0 only the ring the page paints stays visible; the region
        still decides where clicks land.
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
        try:
            action, transparent = self._dotnet_transparent()

            def apply():
                control.DefaultBackgroundColor = transparent

            if getattr(form, "InvokeRequired", False):
                form.Invoke(action(apply))
            else:
                apply()
            alpha = getattr(control.DefaultBackgroundColor, "A", "?")
        except Exception as exc:  # pythonnet raises .NET exceptions
            self._info(f"overlay webview background transparent=failed ({exc})")
            return False
        self._webview_cleared = True
        self._info(f"overlay webview background transparent=ok alpha={alpha}")
        return True

    def _apply_shape(self, mode, placement, data, page=0):
        """Clip the window to the visible overlay parts and remember them for hit tests."""
        polygons = geometry.overlay_shape_polygons(mode, placement, data, page)
        with self.state_lock:
            self._polygons = polygons
        if self.args.headless:
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
            f"polygons={len(polygons)} setwindowrgn={result} {self._region_facts(handle)}")
        return True

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
        if not self.args.headless:
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
            self.window.show()
            self._ensure_shape(mode, placement, payload["data"])
            handle = self._native_hwnd()
            if handle is not None:
                self._log_window_tree(handle)
            self._activate_window()
        else:
            self._apply_shape(mode, placement, payload["data"])
        cursor_moved = self._center_cursor(mode, payload, placement)
        with self.state_lock:
            self.visible = True
            self.mode = mode
            self._show_generation += 1
            generation = self._show_generation
        if not self.args.headless:
            self._start_dismiss_watch(generation, previous_root)
        return self._visible_payload(mode, placement, cursor_moved)

    def _native_hwnd(self):
        """HWND of the overlay form; falls back to this process's titled window."""
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
        if self.args.headless or self.window is None:
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
                background_color="#111827",
                js_api=_Bridge(self),
                hidden=True,
            )
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
