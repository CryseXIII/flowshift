"""FlowShift Windows clipboard access (CF_UNICODETEXT read/set + change signal).

Thin ctypes wrapper for the productive Windows path. Text is fully implemented
here; HTML / image (CF_DIB) / files (CF_HDROP) are the next layers and are left
as clearly-marked stubs so nothing pretends to work that does not.

Safe to import on any OS: ``ctypes.windll`` is only touched inside functions, and
every entry point degrades to a no-op / None off Windows.
"""
from __future__ import annotations

import sys

CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIB = 8
GMEM_MOVEABLE = 0x0002


def _is_windows():
    return sys.platform == "win32"


def _user32_kernel32():
    import ctypes
    return ctypes.windll.user32, ctypes.windll.kernel32


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


def has_text():
    if not _is_windows():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsClipboardFormatAvailable(CF_UNICODETEXT))
    except Exception:
        return False


# ── Next layers (explicit stubs, not silently "working") ────────────
def read_image():
    """CF_DIB read — NOT implemented yet (image layer)."""
    return None


def read_files():
    """CF_HDROP read — NOT implemented yet (file layer)."""
    return None


def set_files(paths):
    """CF_HDROP set — NOT implemented yet (file layer)."""
    return False
