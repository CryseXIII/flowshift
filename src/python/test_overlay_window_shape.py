"""Visible overlay host: the window region must stick, or the square shows."""
from __future__ import annotations

import io
import os
import sys
import types
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import overlay_host
from overlay_host import native_handle_value

HWND = 0x5005
WHEEL_DATA = {
    "actions": {name: {"label": name} for name in ("a", "b", "c", "d")},
    "pages": [["a", "b", "c", "d"]],
}


class _IntPtr:
    def __init__(self, value):
        self.value = value

    def ToInt64(self):
        return self.value


class _Window:
    def __init__(self, handle):
        self.native = types.SimpleNamespace(Handle=handle) if handle is not None else None
        self.calls = []

    def evaluate_js(self, _script):
        return None

    def show(self):
        self.calls.append("show")

    def resize(self, *_args):
        pass

    def move(self, *_args):
        pass


class _Win:
    """Fake Win32 layer; ``keeps`` lists has_window_shape answers in order."""

    def __init__(self, keeps=(True,), found=HWND):
        self.keeps = list(keeps)
        self.found = found
        self.shapes = []
        self.lookups = 0

    def own_window(self, title, pid):
        self.lookups += 1
        assert title == overlay_host.OVERLAY_TITLE and pid == os.getpid()
        return self.found

    def set_window_shape(self, hwnd, polygons):
        self.shapes.append((hwnd, len(polygons)))

    def has_window_shape(self, hwnd):
        return self.keeps.pop(0) if self.keeps else False

    def foreground_root(self):
        return None

    def set_cursor(self, *_args):
        return True


def _host(handle=HWND, win=None):
    args = types.SimpleNamespace(headless=False, startup_timeout=1.0)
    host = overlay_host.OverlayHost(args, webview=object())
    host.window = _Window(handle)
    host._win32 = win or _Win()
    host._start_dismiss_watch = lambda *_args: None
    return host


def _show(host, mode="command_wheel", data=None):
    payload = {
        "mode": mode, "target": {"kind": "local", "identity": "local"},
        "x": 960, "y": 520, "data": WHEEL_DATA if data is None else data,
    }
    return host._show(payload)


class NativeHandleTests(unittest.TestCase):
    def test_intptr_and_int_handles(self):
        self.assertEqual(native_handle_value(types.SimpleNamespace(Handle=_IntPtr(HWND))), HWND)
        self.assertEqual(native_handle_value(types.SimpleNamespace(Handle=HWND)), HWND)

    def test_missing_or_null_handle(self):
        self.assertIsNone(native_handle_value(None))
        self.assertIsNone(native_handle_value(types.SimpleNamespace(Handle=0)))
        self.assertIsNone(native_handle_value(types.SimpleNamespace(Handle=_IntPtr(0))))
        self.assertIsNone(native_handle_value(types.SimpleNamespace(Handle=True)))
        self.assertIsNone(native_handle_value(types.SimpleNamespace(Handle="x")))


class WindowShapeTests(unittest.TestCase):
    def test_wheel_shape_applied_before_show_and_verified(self):
        host = _host(handle=_IntPtr(HWND))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
        self.assertEqual(host._win32.shapes, [(HWND, 4)])
        self.assertEqual(host.window.calls, ["show"])
        self.assertEqual(stderr.getvalue(), "")

    def test_region_lost_on_show_is_reapplied_and_logged(self):
        host = _host(win=_Win(keeps=[False, True]))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
        self.assertEqual(len(host._win32.shapes), 2)
        self.assertIn("shape re-applied after show mode=command_wheel", stderr.getvalue())

    def test_region_that_never_sticks_is_logged(self):
        host = _host(win=_Win(keeps=[False, False]))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host, mode="clipboard", data={})
        self.assertIn("shape missing after show mode=clipboard", stderr.getvalue())

    def test_unreadable_native_handle_falls_back_to_own_window(self):
        # The dark square: Form.Handle did not resolve, so no region was set
        # and nothing was logged. Now the host finds its own titled window.
        host = _host(handle=None)
        with redirect_stderr(io.StringIO()):
            _show(host)
        self.assertGreater(host._win32.lookups, 0)
        self.assertEqual(host._win32.shapes[0], (HWND, 4))

    def test_no_window_at_all_is_logged_not_silent(self):
        host = _host(handle=None, win=_Win(keeps=[], found=None))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
        self.assertIn("native window handle not found", stderr.getvalue())
        self.assertIn("shape missing after show", stderr.getvalue())

    def test_clipboard_overlay_gets_rounded_region(self):
        host = _host()
        with redirect_stderr(io.StringIO()):
            _show(host, mode="clipboard", data={})
        hwnd, count = host._win32.shapes[0]
        self.assertEqual((hwnd, count), (HWND, 1))


if __name__ == "__main__":
    unittest.main()
