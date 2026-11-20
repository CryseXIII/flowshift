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


class _Color:
    def __init__(self, alpha, rgb=(0, 0, 0)):
        self.A = alpha
        self.R, self.G, self.B = rgb


KEY = _Color(255, (0x11, 0x18, 0x27))


class _Control:
    """WinForms WebView2: the controller refuses access off the UI thread."""

    def __init__(self, form):
        self._form = form
        self._color = _Color(255)

    @property
    def DefaultBackgroundColor(self):
        self._check()
        return self._color

    @DefaultBackgroundColor.setter
    def DefaultBackgroundColor(self, value):
        self._check()
        self._color = value

    def _check(self):
        if not self._form.on_ui_thread:
            raise RuntimeError("CoreWebView2Controller members can only be accessed "
                               "from the UI thread.")


class _Form:
    """pywebview's winforms ``BrowserForm`` (``window.native``).

    As in pywebview's winforms.py/edgechromium.py the form holds the
    EdgeChromium backend in ``browser`` and that keeps the WinForms WebView2
    control in ``web_view``; the form itself has no ``webview`` attribute.
    """

    def __init__(self, handle, control=True, invoke_required=True):
        self.Handle = handle
        self.on_ui_thread = not invoke_required
        self.invoked = 0
        self.BackColor = _Color(255, (0x11, 0x18, 0x27))
        self.TransparencyKey = _Color(0)
        if control:
            self.browser = types.SimpleNamespace(web_view=_Control(self))

    @property
    def InvokeRequired(self):
        return not self.on_ui_thread

    @property
    def control(self):
        return self.browser.web_view

    def Invoke(self, delegate):
        self.invoked += 1
        self.on_ui_thread = True
        try:
            delegate()
        finally:
            self.on_ui_thread = False


class _Window:
    def __init__(self, handle, control=True):
        self.native = _Form(handle, control) if handle is not None else None
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
        self.child_shapes = []
        self.lookups = 0

    def own_window(self, title, pid):
        self.lookups += 1
        assert title == overlay_host.OVERLAY_TITLE and pid == os.getpid()
        return self.found

    def set_window_shape(self, hwnd, polygons):
        if hwnd == HWND:
            self.shapes.append((hwnd, len(polygons)))
        else:
            self.child_shapes.append((hwnd, polygons))
        return 1

    def has_window_shape(self, hwnd):
        return self.keeps.pop(0) if self.keeps else False

    def region_info(self, hwnd):
        return 3, (2, 2, 538, 538)

    def window_info(self, hwnd):
        return {"size": "540x540", "client": "540x540", "dpi": 144, "exstyle": "0x00080008"}

    def describe_window(self, hwnd, level):
        return {"hwnd": hwnd, "level": level, "class": "WindowsForms10.Window",
                "pid": os.getpid(), "rect": (100, 50, 640, 590), "visible": True,
                "region": 3, "exstyle": "0x00080008"}

    def child_windows(self, hwnd):
        return [
            {"hwnd": 0x6006, "level": "child", "class": "WebView2", "pid": os.getpid(),
             "rect": (100, 50, 640, 590), "visible": False, "region": 0,
             "exstyle": "0x00000000"},
            {"hwnd": 0x7007, "level": "child", "class": "Chrome_WidgetWin_0", "pid": 4242,
             "rect": (102, 53, 640, 590), "visible": False, "region": 0,
             "exstyle": "0x00000000"},
        ]

    def process_windows(self, pid):
        return [self.describe_window(HWND, "top")] + self.child_windows(HWND)

    def foreground_root(self):
        return None

    def set_cursor(self, *_args):
        return True


def _host(handle=HWND, win=None, control=True):
    args = types.SimpleNamespace(headless=False, startup_timeout=1.0)
    host = overlay_host.OverlayHost(args, webview=object())
    host.window = _Window(handle, control)
    host._win32 = win or _Win()
    host._start_dismiss_watch = lambda *_args: None
    host._dotnet_colors = lambda: ((lambda fn: fn), _Color(0), KEY)
    return host


def _warnings(text):
    """Host stderr lines the controller logs as WARN (not INFO diagnostics)."""
    return [line for line in text.splitlines()
            if line and not line.startswith(overlay_host.INFO_PREFIX)]


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
        self.assertEqual(_warnings(stderr.getvalue()), [])

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


class ShapeDiagnosticsTests(unittest.TestCase):
    """What the region really did must be readable in flowshift.log."""

    def test_shape_line_carries_handle_size_dpi_polygons_and_result(self):
        host = _host()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
        text = stderr.getvalue()
        line = next(l for l in text.splitlines() if "overlay shape set" in l)
        self.assertTrue(line.startswith(overlay_host.INFO_PREFIX))
        for part in ("mode=command_wheel", f"hwnd=0x{HWND:x}", "placement=", "dpi=",
                     "window=540x540", "window_dpi=144", "exstyle=0x00080008",
                     "polygons=4", "setwindowrgn=1", "region=3 box=2,2,538,538"):
            self.assertIn(part, line)
        self.assertIn("overlay shape after show mode=command_wheel kept", text)
        self.assertIn("children=2 [0x6006:WebView2:ok 0x7007:Chrome_WidgetWin_0:ok]", line)
        self.assertIn("overlay window tree", text)
        self.assertIn("top=1 children=2", text)

    def test_every_process_window_listed_with_class_size_visibility_region(self):
        host = _host()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
        lines = [l for l in stderr.getvalue().splitlines() if "overlay window " in l
                 and "tree" not in l]
        self.assertEqual(len(lines), 3)
        self.assertIn(f"overlay window top hwnd=0x{HWND:x} class=WindowsForms10.Window", lines[0])
        self.assertIn("rect=100,50,540x540 visible=1 region=3", lines[0])
        self.assertIn("overlay window child hwnd=0x7007 class=Chrome_WidgetWin_0 pid=4242 "
                      "rect=102,53,538x537 visible=0 region=0", lines[2])
        self.assertTrue(all(l.startswith(overlay_host.INFO_PREFIX) for l in lines))

    def test_child_windows_get_the_shape_in_their_own_coordinates(self):
        host = _host()
        with redirect_stderr(io.StringIO()):
            _show(host)
        form_polygons = overlay_host.geometry.overlay_shape_polygons(
            "command_wheel", host._placement, WHEEL_DATA, 0)
        by_hwnd = dict(host._win32.child_shapes)
        self.assertEqual(by_hwnd[0x6006], [list(p) for p in form_polygons])
        x, y = form_polygons[0][0]
        self.assertEqual(by_hwnd[0x7007][0][0], (x - 2, y - 3))

    def test_window_tree_logged_once_per_host(self):
        host = _host()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
            _show(host, mode="clipboard", data={})
        self.assertEqual(stderr.getvalue().count("overlay window tree"), 1)


class WebViewBackgroundTests(unittest.TestCase):
    """The opaque WebView2 background (#111827) was the visible square."""

    def test_background_made_transparent_on_ui_thread_once(self):
        host = _host()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
            _show(host, mode="clipboard", data={})
        form = host.window.native
        self.assertEqual(form.control._color.A, 0)
        self.assertEqual(form.invoked, 1)
        self.assertEqual(stderr.getvalue().count(
            "transparent=ok ui_thread=1 alpha=0 form_key=#111827/a255"), 1)
        self.assertIn("control path=native.browser.web_view", stderr.getvalue())

    def test_dev32_cross_thread_read_no_longer_fails(self):
        # dev.32 live: "CoreWebView2Controller members can only be accessed
        # from the UI thread" from get_DefaultBackgroundColor on the worker.
        host = _host()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
        self.assertNotIn("transparent=failed", stderr.getvalue())
        self.assertFalse(host.window.native.on_ui_thread)

    def test_form_gets_its_colour_as_transparency_key(self):
        host = _host()
        with redirect_stderr(io.StringIO()):
            _show(host)
        form = host.window.native
        self.assertIs(form.TransparencyKey, KEY)
        self.assertIs(form.BackColor, KEY)

    def test_layered_style_reported(self):
        host = _host()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
        self.assertIn("layered=1", stderr.getvalue())

    def test_failing_step_logged_and_retried_next_show(self):
        host = _host()
        form = host.window.native

        def refuse(*_args):
            raise RuntimeError("form busy\n   at System.Windows.Forms")

        type(form).TransparencyKey = property(lambda self: KEY, refuse)
        try:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                _show(host)
                _show(host)
        finally:
            del type(form).TransparencyKey
        text = stderr.getvalue()
        self.assertEqual(text.count("transparent=failed ui_thread=1 alpha=0 form_key=failed"), 2)
        self.assertIn("(form busy)", text)
        self.assertNotIn("System.Windows.Forms", text)

    def test_control_resolved_via_browser_web_view_with_fallback(self):
        control = object()
        real = types.SimpleNamespace(
            browser=types.SimpleNamespace(web_view=control))
        self.assertEqual(overlay_host.webview2_control(real),
                         (control, "native.browser.web_view"))
        legacy = types.SimpleNamespace(webview=control)
        self.assertEqual(overlay_host.webview2_control(legacy),
                         (control, "native.webview"))
        self.assertEqual(overlay_host.webview2_control(None), (None, None))
        self.assertEqual(
            overlay_host.webview2_control(types.SimpleNamespace(browser=None)),
            (None, None))

    def test_background_cleared_before_show(self):
        host = _host()
        order = []
        host.window.show = lambda: order.append(
            ("show", host.window.native.control._color.A))
        with redirect_stderr(io.StringIO()):
            _show(host)
        self.assertEqual(order, [("show", 0)])

    def test_missing_control_is_logged_and_retried(self):
        host = _host(control=False)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
            _show(host)
        self.assertEqual(host.window.calls, ["show", "show"])
        self.assertEqual(stderr.getvalue().count("transparent=failed"), 2)
        # The next round needs evidence of what the form really offers.
        self.assertIn("native attrs=", stderr.getvalue())
        self.assertIn("Handle", stderr.getvalue())

    def test_dotnet_error_does_not_break_show(self):
        host = _host()

        def boom():
            raise RuntimeError("no pythonnet")

        host._dotnet_colors = boom
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
        self.assertEqual(host.window.calls, ["show"])
        self.assertIn("transparent=failed (no pythonnet)", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
