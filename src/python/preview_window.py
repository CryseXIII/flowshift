"""Remote preview, stage 4: the preview window on the controlling PC.

A small always-on-top picture of the other PC's screen in a corner of the
screen the mouse left from. It appears when control moves to the other PC and
disappears on the return. It never takes the focus and lets every click
through (``WS_EX_NOACTIVATE | WS_EX_TRANSPARENT`` on a layered tool window,
shown with ``SW_SHOWNA``), so keyboard and mouse stay with the forwarding.

All window work runs on the window's own thread and message loop, like the
native command wheel (``overlay_native_wheel``). Other threads only hand over
the newest picture (``frame``) or a sentence (``notice``); a picture that was
not painted yet is replaced by the next one, so a slow paint never queues up.

``placement`` and ``caption`` are pure and unit-tested; the window itself
needs Windows (``PreviewWindow`` raises ``OSError`` elsewhere).
"""
from __future__ import annotations

import ctypes
import io
import sys
import threading

WINDOW_CLASS = "FlowShiftRemotePreview"
WINDOW_TITLE = "FlowShift Remote Preview"
MARGIN_PX = 16
CAPTION_PX = 20
NOTICE_RATIO = 9 / 16

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_MOUSEACTIVATE = 0x0021
WM_NCHITTEST = 0x0084
WM_APP_RUN = 0x8000 + 0x52
MA_NOACTIVATE = 3
HTTRANSPARENT = -1
SW_HIDE = 0
SW_SHOWNA = 8
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
ULW_ALPHA = 0x2
AC_SRC_ALPHA = 0x1
ERROR_CLASS_ALREADY_EXISTS = 1410


def placement(corner, width_px, picture_w, picture_h, area, margin=MARGIN_PX, caption=CAPTION_PX):
    """``(x, y, width, height)`` of the window: ``width_px`` wide, picture aspect,
    caption strip on top, in ``corner`` of ``area`` (the work area of a monitor),
    never larger than the area."""
    width = max(1, min(int(width_px), area["width"] - 2 * margin))
    picture_h = max(1, round(width * picture_h / max(1, picture_w)))
    height = min(picture_h + caption, area["height"] - 2 * margin)
    left = area["left"] + margin
    top = area["top"] + margin
    if corner in ("top_right", "bottom_right"):
        left = area["left"] + area["width"] - margin - width
    if corner in ("bottom_left", "bottom_right"):
        top = area["top"] + area["height"] - margin - height
    return int(left), int(top), int(width), int(max(1, height))


def caption(peer_name, fps=None, notice=None):
    """Text of the caption strip, e.g. ``"PC-2 - 14 fps"`` or ``"PC-2 - screen locked"``."""
    text = f"{peer_name or 'other PC'}"
    if notice:
        return f"{text} - {notice}"
    if fps is not None:
        return f"{text} - {fps:g} fps"
    return text


def compose(jpeg, width, height, title):
    """Picture for the window: caption strip plus the scaled picture, BGRA bytes.

    ``jpeg=None`` paints a dark field with only the caption (waiting, locked).
    """
    from PIL import Image, ImageDraw  # noqa: PLC0415 - Pillow, runtime dependency
    canvas = Image.new("RGB", (width, height), (24, 28, 36))
    if jpeg:
        picture = Image.open(io.BytesIO(jpeg)).convert("RGB")
        picture = picture.resize((width, max(1, height - CAPTION_PX)))
        canvas.paste(picture, (0, CAPTION_PX))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, width - 1, height - 1), outline=(90, 100, 120))
    draw.text((6, 4), title[:120], fill=(235, 240, 245))
    return canvas.convert("RGBA").tobytes("raw", "BGRA")


class PreviewWindow:
    """The preview window on its own thread. ``log(level, text)`` writes diagnostics."""

    def __init__(self, log):
        if sys.platform != "win32":
            raise OSError("the preview window needs Windows")
        self.log = log
        self.hwnd = None
        self.visible = False
        self.painted = 0
        self.last_error = ""
        self._lock = threading.Lock()
        self._pending = None       # newest (jpeg, title) not painted yet
        self._posted = False
        self._geometry = None      # (corner, width_px, area)
        self._box = None           # (x, y, w, h) on screen
        self._paint_done = threading.Condition(self._lock)
        self._ready = threading.Event()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="remote-preview-window", daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0):
            raise OSError("preview window did not start")
        if self._error is not None:
            raise self._error

    # ---- called from other threads ------------------------------------
    def _post(self, item):
        with self._lock:
            self._pending = item
            if self._posted:
                return
            self._posted = True
        self.user32.PostMessageW(self.hwnd, WM_APP_RUN, 0, 0)

    def show(self, corner, width_px, area, title):
        """Place the window (``area`` = work area of a monitor) and show ``title`` until a picture comes."""
        with self._lock:
            self._geometry = (corner, int(width_px), dict(area))
            self._box = None
            self.last_error = ""
        self._post(("show", None, title, None))

    def frame(self, jpeg, width, height, title):
        self._post(("frame", jpeg, title, (width, height)))

    def notice(self, title):
        self._post(("notice", None, title, None))

    def hide(self):
        self._post(("hide", None, "", None))

    def wait_painted(self, count, timeout):
        """Wait until ``painted`` reaches ``count``; returns whether it did."""
        with self._paint_done:
            return self._paint_done.wait_for(lambda: self.painted >= count or self.last_error,
                                             timeout) and self.painted >= count

    def close(self):
        if self.hwnd and self._thread.is_alive():
            self.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            self._thread.join(timeout=1.0)

    # ---- window thread -------------------------------------------------
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

        self.WndClass, self.Blend, self.BitmapInfo = _WndClass, _Blend, _BitmapInfo
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k.GetModuleHandleW.restype = wintypes.HMODULE
        self.instance = k.GetModuleHandleW(None)
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.DefWindowProcW.restype = wintypes.LPARAM
        u.RegisterClassExW.argtypes = [ctypes.POINTER(_WndClass)]
        u.RegisterClassExW.restype = wintypes.ATOM
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                      wintypes.HINSTANCE, wintypes.LPVOID]
        u.CreateWindowExW.restype = wintypes.HWND
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
        u.GetDC.argtypes = [wintypes.HWND]
        u.GetDC.restype = wintypes.HDC
        u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
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
            self._proc = self.wndproc_type(self._wndproc)
            wc = self.WndClass()
            wc.cbSize = ctypes.sizeof(self.WndClass)
            wc.lpfnWndProc = self._proc
            wc.hInstance = self.instance
            wc.lpszClassName = WINDOW_CLASS
            if not self.user32.RegisterClassExW(ctypes.byref(wc)):
                error = ctypes.get_last_error()
                if error != ERROR_CLASS_ALREADY_EXISTS:
                    raise ctypes.WinError(error)
            hwnd = self.user32.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
                | WS_EX_TRANSPARENT, WINDOW_CLASS, WINDOW_TITLE, WS_POPUP, 0, 0, 1, 1,
                None, None, self.instance, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self.hwnd = int(hwnd)
        except Exception as exc:
            self._error = exc if isinstance(exc, OSError) else OSError(str(exc))
            self._ready.set()
            return
        self._ready.set()
        msg = self.wintypes.MSG()
        while self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))

    def _wndproc(self, hwnd, message, wparam, lparam):
        try:
            if message == WM_APP_RUN:
                with self._lock:
                    item, self._pending, self._posted = self._pending, None, False
                if item is not None:
                    self._apply(item)
                return 0
            if message == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if message == WM_NCHITTEST:
                return HTTRANSPARENT
            if message == WM_CLOSE:
                self.user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                self.user32.PostQuitMessage(0)
                return 0
        except Exception as exc:  # never let an exception cross the WndProc
            self.log("WARN", f"remote_preview window message 0x{message:x} failed: {exc!r}")
            return 0
        return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _apply(self, item):
        kind, jpeg, title, size = item
        if kind == "hide":
            self.user32.ShowWindow(self.hwnd, SW_HIDE)
            self.visible = False
            return
        if kind in ("show", "notice") or self._box is None:
            picture_w, picture_h = size or (16, 9)
            self._place(picture_w, picture_h)
        elif kind == "frame" and size:
            self._place(*size, keep=True)
        try:
            x, y, w, h = self._box
            self._paint(compose(jpeg, w, h, title), x, y, w, h)
        except Exception as exc:
            with self._paint_done:
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                self._paint_done.notify_all()
            self.log("WARN", f"remote_preview window paint failed: {exc!r}")
            return
        if not self.visible:
            self.user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self.user32.ShowWindow(self.hwnd, SW_SHOWNA)
            self.visible = True
        with self._paint_done:
            self.painted += 1
            self.last_error = ""
            self._paint_done.notify_all()

    def _place(self, picture_w, picture_h, keep=False):
        with self._lock:
            geometry = self._geometry
        if geometry is None:
            geometry = ("bottom_right", 480, {"left": 0, "top": 0, "width": 1280, "height": 720})
        corner, width_px, area = geometry
        box = placement(corner, width_px, picture_w, picture_h, area)
        if keep and self._box is not None and self._box[2:] == box[2:]:
            return
        self._box = box

    def _paint(self, bgra, x, y, w, h):
        wintypes = self.wintypes
        screen = self.user32.GetDC(None)
        memory = bitmap = old = None
        try:
            memory = self.gdi32.CreateCompatibleDC(screen)
            info = self.BitmapInfo()
            info.biSize = ctypes.sizeof(self.BitmapInfo) - 4
            info.biWidth, info.biHeight = w, -h  # top-down
            info.biPlanes, info.biBitCount = 1, 32
            bits = ctypes.c_void_p()
            bitmap = self.gdi32.CreateDIBSection(memory, ctypes.byref(info), 0,
                                                 ctypes.byref(bits), None, 0)
            if not memory or not bitmap or not bits:
                raise ctypes.WinError(ctypes.get_last_error())
            ctypes.memmove(bits, bgra, min(len(bgra), w * h * 4))
            old = self.gdi32.SelectObject(memory, bitmap)
            blend = self.Blend(0, 0, 255, AC_SRC_ALPHA)
            if not self.user32.UpdateLayeredWindow(
                    self.hwnd, screen, ctypes.byref(wintypes.POINT(x, y)),
                    ctypes.byref(wintypes.SIZE(w, h)), memory,
                    ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if old:
                self.gdi32.SelectObject(memory, old)
            if bitmap:
                self.gdi32.DeleteObject(bitmap)
            if memory:
                self.gdi32.DeleteDC(memory)
            self.user32.ReleaseDC(None, screen)
