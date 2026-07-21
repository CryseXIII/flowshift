"""FlowShift Windows clipboard access (CF_UNICODETEXT / HTML / CF_HDROP / CF_DIB).

Thin ctypes wrapper for the productive Windows path. Text, HTML, image (CF_DIB)
and files (CF_HDROP) are implemented here.

Safe to import on any OS: ``ctypes.windll`` is only touched inside functions, and
every entry point degrades to a no-op / None off Windows.
"""
from __future__ import annotations

import sys

CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIB = 8
GMEM_MOVEABLE = 0x0002
_HTML_FORMAT_NAME = "HTML Format"
_html_format_id = None


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


def read_text(retries=5, delay=0.02):
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
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    for _ in range(retries):
        if user32.OpenClipboard(None):
            try:
                if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    return None
                h = user32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return None
                p = kernel32.GlobalLock(h)
                if not p:
                    return None
                try:
                    return ctypes.c_wchar_p(p).value
                finally:
                    kernel32.GlobalUnlock(h)
            finally:
                user32.CloseClipboard()
        time.sleep(delay)
    return None


def set_text(text):
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
                return True
            finally:
                user32.CloseClipboard()
        time.sleep(0.02)
    return False


def set_html(cf_html_bytes, fallback_text=None):
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
                return bool(html_ok and text_ok)
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


def read_html(retries=5, delay=0.02):
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
                    return None
                size = kernel32.GlobalSize(h)
                p = kernel32.GlobalLock(h)
                if not p:
                    return None
                try:
                    return ctypes.string_at(p, size)
                finally:
                    kernel32.GlobalUnlock(h)
            finally:
                user32.CloseClipboard()
        time.sleep(delay)
    return None


# ── Files (CF_HDROP) ────────────────────────────────────────────────
def read_files():
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
                    return None
                count = shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
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
    return None


def set_files(paths):
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
                return True
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
def read_image():
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
                    return None
                size = kernel32.GlobalSize(h)
                p = kernel32.GlobalLock(h)
                if not p:
                    return None
                try:
                    dib = ctypes.string_at(p, size)
                    return ci.dib_to_bmp(dib)
                finally:
                    kernel32.GlobalUnlock(h)
            finally:
                user32.CloseClipboard()
        import time
        time.sleep(0.02)
    return None


def set_image(bmp):
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
                return True
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
