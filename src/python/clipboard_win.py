"""FlowShift Windows clipboard access (CF_UNICODETEXT / HTML / CF_HDROP / CF_DIB).

Thin ctypes wrapper for the productive Windows path. Text, HTML, image (CF_DIB)
and files (CF_HDROP) are implemented here.

Safe to import on any OS: ``ctypes.windll`` is only touched inside functions, and
every entry point degrades to a no-op / None off Windows.
"""
from __future__ import annotations

import sys
import threading
import time

CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIB = 8
GMEM_MOVEABLE = 0x0002
_HTML_FORMAT_NAME = "HTML Format"
_html_format_id = None


class ClipboardReadError(RuntimeError):
    pass


class ClipboardTooLarge(ClipboardReadError):
    pass


def _is_windows():
    return sys.platform == "win32"


def _user32_kernel32():
    import ctypes
    return ctypes.windll.user32, ctypes.windll.kernel32


def _html_format():
    global _html_format_id
    if not _is_windows():
        return 0
    if _html_format_id is not None:
        return _html_format_id
    try:
        import ctypes
        _html_format_id = int(ctypes.windll.user32.RegisterClipboardFormatW(_HTML_FORMAT_NAME))
    except Exception:
        _html_format_id = 0
    return _html_format_id


def get_sequence_number():
    """Monotonic clipboard change counter (0 if unavailable). Cheap to poll."""
    if not _is_windows():
        return 0
    try:
        import ctypes
        return int(ctypes.windll.user32.GetClipboardSequenceNumber())
    except Exception:
        return 0


class ClipboardListener:
    """Dedicated WM_CLIPBOARDUPDATE message thread with bounded shutdown."""

    WM_CLIPBOARDUPDATE = 0x031D
    WM_CLOSE = 0x0010
    WM_DESTROY = 0x0002
    GWLP_WNDPROC = -4
    HWND_MESSAGE = -3

    def __init__(self, callback):
        self.callback = callback
        self._thread = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._lock = threading.Lock()
        self._hwnd = None
        self._wndproc = None
        self._original_wndproc = None
        self._running = False
        self._error = None
        self._last_sequence = 0
        self._last_event_at = None

    def start(self, timeout=3.0):
        if not _is_windows():
            self._error = "unsupported_platform"
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self._running
            self._ready.clear()
            self._stop_requested.clear()
            self._thread = threading.Thread(target=self._run, name="clipboard-listener",
                                            daemon=True)
            self._thread.start()
        if not self._ready.wait(max(0.0, float(timeout))):
            with self._lock:
                self._error = "listener_start_timeout"
            self.stop(timeout=timeout)
            return False
        return self.running

    @property
    def running(self):
        with self._lock:
            return bool(self._running and self._thread and self._thread.is_alive())

    def _run(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        wndproc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
        ]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                           wintypes.WPARAM, wintypes.LPARAM]
        user32.AddClipboardFormatListener.restype = wintypes.BOOL
        user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
        user32.RemoveClipboardFormatListener.restype = wintypes.BOOL
        user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]

        def window_proc(hwnd, message, wparam, lparam):
            if message == self.WM_CLIPBOARDUPDATE:
                sequence = get_sequence_number()
                with self._lock:
                    self._last_sequence = sequence
                    self._last_event_at = time.time()
                try:
                    self.callback(sequence)
                except Exception:
                    pass
                return 0
            if message == self.WM_CLOSE:
                try:
                    user32.RemoveClipboardFormatListener(hwnd)
                except Exception:
                    pass
                user32.DestroyWindow(hwnd)
                return 0
            if message == self.WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.CallWindowProcW(
                self._original_wndproc, hwnd, message, wparam, lparam)

        try:
            self._wndproc = wndproc_type(window_proc)
            hwnd_parent = ctypes.c_void_p(self.HWND_MESSAGE)
            hwnd = user32.CreateWindowExW(
                0, "STATIC", "FlowShiftClipboardListener", 0,
                0, 0, 0, 0, hwnd_parent, None, None, None)
            if not hwnd:
                raise OSError("CreateWindowExW failed")
            self._hwnd = hwnd
            self._original_wndproc = user32.SetWindowLongPtrW(
                hwnd, self.GWLP_WNDPROC, ctypes.cast(self._wndproc, ctypes.c_void_p))
            if not self._original_wndproc:
                raise OSError("SetWindowLongPtrW failed")
            if not user32.AddClipboardFormatListener(hwnd):
                raise OSError("AddClipboardFormatListener failed")
            if self._stop_requested.is_set():
                raise RuntimeError("listener start cancelled")
            with self._lock:
                self._running = True
                self._error = None
            self._ready.set()
            message = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise OSError("GetMessageW failed")
                if result == 0:
                    if not self._stop_requested.is_set():
                        raise RuntimeError("listener message loop stopped unexpectedly")
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            with self._lock:
                self._error = str(exc)
        finally:
            with self._lock:
                hwnd = self._hwnd
                self._running = False
                self._hwnd = None
            if hwnd:
                try:
                    user32.RemoveClipboardFormatListener(hwnd)
                    user32.DestroyWindow(hwnd)
                except Exception:
                    pass
            self._ready.set()

    def stop(self, timeout=3.0):
        self._stop_requested.set()
        with self._lock:
            hwnd = self._hwnd
            thread = self._thread
        if hwnd and _is_windows():
            try:
                import ctypes
                ctypes.windll.user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)
            except Exception:
                pass
        if thread and thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout)))
        return not (thread and thread.is_alive())

    def snapshot(self):
        with self._lock:
            return {
                "running": self._running,
                "error": self._error,
                "last_sequence": self._last_sequence,
                "last_event_at": self._last_event_at,
            }
def read_text(retries=5, delay=0.02, max_bytes=None):
    """Return the current clipboard text (CF_UNICODETEXT) or None."""
    if not _is_windows():
        return None
    import ctypes
    import time
    user32, kernel32 = _user32_kernel32()
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    for _ in range(retries):
        if user32.OpenClipboard(None):
            try:
                if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    return None
                h = user32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    raise ClipboardReadError("clipboard text handle unavailable")
                if max_bytes is not None and kernel32.GlobalSize(h) > int(max_bytes):
                    raise ClipboardTooLarge("clipboard text exceeds max_item_gb")
                p = kernel32.GlobalLock(h)
                if not p:
                    raise ClipboardReadError("clipboard text lock failed")
                try:
                    max_chars = max(0, int(kernel32.GlobalSize(h) // ctypes.sizeof(ctypes.c_wchar)))
                    return ctypes.wstring_at(p, max_chars).split("\0", 1)[0]
                finally:
                    kernel32.GlobalUnlock(h)
            finally:
                user32.CloseClipboard()
        time.sleep(delay)
    raise ClipboardReadError("clipboard remained locked while reading text")


def set_text(text, return_sequence=False):
    """Put ``text`` on the Windows clipboard (so Ctrl+V pastes it). Returns bool."""
    if not _is_windows():
        return False
    import ctypes
    import time
    user32, kernel32 = _user32_kernel32()
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    for _ in range(5):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
                if not h:
                    return False
                p = kernel32.GlobalLock(h)
                if not p:
                    return False
                ctypes.memmove(p, data, size)
                kernel32.GlobalUnlock(h)
                if not user32.SetClipboardData(CF_UNICODETEXT, h):
                    return False
                return (True, get_sequence_number()) if return_sequence else True
            finally:
                user32.CloseClipboard()
        time.sleep(0.02)
    return False


def set_html(cf_html_bytes, fallback_text=None, return_sequence=False):
    """Put CF_HTML on the Windows clipboard; optionally add CF_UNICODETEXT too."""
    if not _is_windows() or not cf_html_bytes:
        return False
    import ctypes
    import time
    user32, kernel32 = _user32_kernel32()
    fmt = _html_format()
    if not fmt:
        return False
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    def _set_handle(format_id, payload_bytes):
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload_bytes))
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        if not p:
            kernel32.GlobalFree(h)
            return False
        try:
            ctypes.memmove(p, payload_bytes, len(payload_bytes))
        finally:
            kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(format_id, h):
            kernel32.GlobalFree(h)
            return False
        return True

    html_ok = False
    text_ok = fallback_text is None
    for _ in range(5):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                if fallback_text is not None:
                    text_data = ctypes.create_unicode_buffer(fallback_text)
                    text_ok = _set_handle(CF_UNICODETEXT, ctypes.string_at(text_data, ctypes.sizeof(text_data)))
                html_ok = _set_handle(fmt, bytes(cf_html_bytes))
                success = bool(html_ok and text_ok)
                return (success, get_sequence_number()) if return_sequence else success
            finally:
                user32.CloseClipboard()
        time.sleep(0.02)
    return False


def has_text():
    if not _is_windows():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsClipboardFormatAvailable(CF_UNICODETEXT))
    except Exception:
        return False


def has_html():
    if not _is_windows():
        return False
    try:
        import ctypes
        fmt = _html_format()
        return bool(fmt and ctypes.windll.user32.IsClipboardFormatAvailable(fmt))
    except Exception:
        return False


def read_html(retries=5, delay=0.02, max_bytes=None):
    """Return the current clipboard HTML payload (CF_HTML bytes) or None."""
    if not _is_windows():
        return None
    import ctypes
    import time
    user32, kernel32 = _user32_kernel32()
    fmt = _html_format()
    if not fmt:
        return None
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    for _ in range(retries):
        if user32.OpenClipboard(None):
            try:
                if not user32.IsClipboardFormatAvailable(fmt):
                    return None
                h = user32.GetClipboardData(fmt)
                if not h:
                    raise ClipboardReadError("clipboard html handle unavailable")
                size = kernel32.GlobalSize(h)
                if max_bytes is not None and size > int(max_bytes):
                    raise ClipboardTooLarge("clipboard html exceeds max_item_gb")
                p = kernel32.GlobalLock(h)
                if not p:
                    raise ClipboardReadError("clipboard html lock failed")
                try:
                    return ctypes.string_at(p, size)
                finally:
                    kernel32.GlobalUnlock(h)
            finally:
                user32.CloseClipboard()
        time.sleep(delay)
    raise ClipboardReadError("clipboard remained locked while reading html")


# ── Files (CF_HDROP) ────────────────────────────────────────────────
def read_files(max_paths=100000):
    """Return a list of file paths from the clipboard (CF_HDROP), or None."""
    if not _is_windows():
        return None
    import ctypes
    user32, kernel32 = _user32_kernel32()
    shell32 = ctypes.windll.shell32
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    shell32.DragQueryFileW.restype = ctypes.c_uint
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
    for _ in range(5):
        if user32.OpenClipboard(None):
            try:
                if not user32.IsClipboardFormatAvailable(CF_HDROP):
                    return None
                h = user32.GetClipboardData(CF_HDROP)
                if not h:
                    raise ClipboardReadError("clipboard file handle unavailable")
                count = shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
                if count > max(1, int(max_paths)):
                    raise ClipboardTooLarge("clipboard file count exceeds capture limit")
                out = []
                for i in range(count):
                    n = shell32.DragQueryFileW(h, i, None, 0)
                    buf = ctypes.create_unicode_buffer(n + 1)
                    shell32.DragQueryFileW(h, i, buf, n + 1)
                    out.append(buf.value)
                return out
            finally:
                user32.CloseClipboard()
        import time
        time.sleep(0.02)
    raise ClipboardReadError("clipboard remained locked while reading files")


def set_files(paths, return_sequence=False):
    """Put a file list on the clipboard (CF_HDROP) so Explorer/apps can paste.

    Builds a DROPFILES header + a double-null-terminated wide path list.
    """
    if not _is_windows() or not paths:
        return False
    import ctypes
    user32, kernel32 = _user32_kernel32()
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    # DROPFILES: DWORD pFiles; POINT pt(2xLONG); BOOL fNC; BOOL fWide  => 20 bytes
    class DROPFILES(ctypes.Structure):
        _fields_ = [("pFiles", ctypes.c_uint32), ("x", ctypes.c_int32),
                    ("y", ctypes.c_int32), ("fNC", ctypes.c_int32),
                    ("fWide", ctypes.c_int32)]

    joined = "".join(p + "\0" for p in paths) + "\0"
    path_bytes = joined.encode("utf-16-le")
    header = DROPFILES()
    header.pFiles = ctypes.sizeof(DROPFILES)
    header.fWide = 1
    total = ctypes.sizeof(DROPFILES) + len(path_bytes)

    for _ in range(5):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                h = kernel32.GlobalAlloc(GMEM_MOVEABLE, total)
                if not h:
                    return False
                p = kernel32.GlobalLock(h)
                if not p:
                    return False
                ctypes.memmove(p, ctypes.byref(header), ctypes.sizeof(DROPFILES))
                ctypes.memmove(p + ctypes.sizeof(DROPFILES),
                               path_bytes, len(path_bytes))
                kernel32.GlobalUnlock(h)
                if not user32.SetClipboardData(CF_HDROP, h):
                    return False
                return (True, get_sequence_number()) if return_sequence else True
            finally:
                user32.CloseClipboard()
        import time
        time.sleep(0.02)
    return False


def has_files():
    if not _is_windows():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsClipboardFormatAvailable(CF_HDROP))
    except Exception:
        return False


# ── Next layers (explicit stubs, not silently "working") ────────────
def read_image(max_bytes=None):
    """Read a clipboard image (CF_DIB) and return it as BMP bytes, or None."""
    if not _is_windows():
        return None
    import ctypes
    import clipboard_image as ci
    user32, kernel32 = _user32_kernel32()
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    for _ in range(5):
        if user32.OpenClipboard(None):
            try:
                if not user32.IsClipboardFormatAvailable(CF_DIB):
                    return None
                h = user32.GetClipboardData(CF_DIB)
                if not h:
                    raise ClipboardReadError("clipboard image handle unavailable")
                size = kernel32.GlobalSize(h)
                if max_bytes is not None and size > int(max_bytes):
                    raise ClipboardTooLarge("clipboard image exceeds max_item_gb")
                p = kernel32.GlobalLock(h)
                if not p:
                    raise ClipboardReadError("clipboard image lock failed")
                try:
                    dib = ctypes.string_at(p, size)
                    return ci.dib_to_bmp(dib)
                finally:
                    kernel32.GlobalUnlock(h)
            finally:
                user32.CloseClipboard()
        import time
        time.sleep(0.02)
    raise ClipboardReadError("clipboard remained locked while reading image")


def set_image(bmp, return_sequence=False):
    """Put a BMP image on the clipboard as CF_DIB. Returns bool."""
    if not _is_windows() or not bmp:
        return False
    import ctypes
    import clipboard_image as ci
    dib = ci.bmp_to_dib(bmp)
    if not dib:
        return False
    user32, kernel32 = _user32_kernel32()
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    for _ in range(5):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
                if not h:
                    return False
                p = kernel32.GlobalLock(h)
                if not p:
                    return False
                ctypes.memmove(p, dib, len(dib))
                kernel32.GlobalUnlock(h)
                if not user32.SetClipboardData(CF_DIB, h):
                    return False
                return (True, get_sequence_number()) if return_sequence else True
            finally:
                user32.CloseClipboard()
        import time
        time.sleep(0.02)
    return False


def has_image():
    if not _is_windows():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsClipboardFormatAvailable(CF_DIB))
    except Exception:
        return False
