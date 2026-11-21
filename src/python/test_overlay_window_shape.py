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
BLACK = _Color(255, (0, 0, 0))


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

    def root(self, hwnd):
        return hwnd

    def enable_blur_behind(self, hwnd):
        self.blurred = hwnd
        return self.blur_result

    blur_result = 0
    blurred = None
    # Screen colours: before the show the desktop, after it what the overlay
    # composes; ``screen`` returns them in this order.
    screens = ()

    def sample_screen(self, left, top, width, height, points):
        self.samples = getattr(self, "samples", 0) + 1
        screens = list(self.screens)
        if not screens:
            return [(200, 210, 220)] * len(points)
        return screens[min(self.samples, len(screens)) - 1](points)

    def window_at(self, x, y):
        return (0x9009, 0x9009, "Notepad")

    def region_rects(self, hwnd):
        return 3, [(13, 170, 347, 171), (14, 169, 346, 170)]

    def set_cursor(self, *_args):
        return True


def _host(handle=HWND, win=None, control=True):
    args = types.SimpleNamespace(headless=False, startup_timeout=1.0)
    host = overlay_host.OverlayHost(args, webview=object())
    host.window = _Window(handle, control)
    host._win32 = win or _Win()
    host._start_dismiss_watch = lambda *_args: None
    host._dotnet_colors = lambda: ((lambda fn: fn), _Color(0), BLACK)
    host._start_probe = lambda *args: host.probes.append(args)
    host.probes = []
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
    """The opaque WebView2/form background (#111827) was the visible square."""

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
            "transparent=ok strategy=alpha ui_thread=1 alpha=0 form=#000000/a255 "
            "form_key=#000000/a0"), 1)
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

    def test_form_paints_black_without_colour_key(self):
        # dev.33: BackColor/TransparencyKey #111827 (layered colour key) was
        # reported as set, yet the square stayed. Now black (alpha 0 under DWM
        # alpha composition) and no colour key.
        host = _host()
        with redirect_stderr(io.StringIO()):
            _show(host)
        form = host.window.native
        self.assertIs(form.BackColor, BLACK)
        self.assertEqual(form.TransparencyKey.A, 0)

    def test_dwm_alpha_composition_enabled_and_reported(self):
        host = _host()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
        self.assertEqual(host._win32.blurred, HWND)
        self.assertIn("layered=1 blur=ok", stderr.getvalue())

    def test_failed_blur_behind_is_logged_and_retried(self):
        win = _Win()
        win.blur_result = -2147467259
        host = _host(win=win)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
            _show(host)
        self.assertEqual(stderr.getvalue().count("transparent=failed strategy=alpha"), 2)
        self.assertIn("blur=failed(0x80004005)", stderr.getvalue())

    def test_failing_step_logged_and_retried_next_show(self):
        host = _host()
        form = host.window.native

        def refuse(*_args):
            raise RuntimeError("form busy\n   at System.Windows.Forms")

        type(form).BackColor = property(lambda self: KEY, refuse)
        try:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                _show(host)
                _show(host)
        finally:
            del type(form).BackColor
        text = stderr.getvalue()
        self.assertEqual(text.count("transparent=failed strategy=alpha ui_thread=1 alpha=0 "
                                    "form=failed"), 2)
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


DESKTOP = (200, 210, 220)
RING = (40, 44, 52)


def _screen(colours):
    """Fake screen: ``colours(name, expect)`` per probe point."""
    def sample(points_xy):
        return [colours(name, expect) for (name, _x, _y, expect) in _screen.points]
    return sample


class PixelProbeTests(unittest.TestCase):
    """Measure the screen, not the return values (Viktor 21.11.: square stays)."""

    def _measure(self, shown, below=DESKTOP, hit_overlay=False, data=None, mode="command_wheel"):
        host = _host()
        with redirect_stderr(io.StringIO()):
            _show(host, mode=mode, data=WHEEL_DATA if data is None else data)
        placement = host._placement
        points = overlay_host.geometry.overlay_probe_points(mode, placement, host._data)
        _screen.points = points
        host._win32.screens = (_screen(shown),)
        if hit_overlay:
            host._win32.window_at = lambda x, y: (0x7007, HWND, "Chrome_WidgetWin_0")
        below_colours = None if below is None else [below] * len(points)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            report = host._measure(mode, placement, points, below_colours)
        return report, stderr.getvalue()

    def test_show_samples_desktop_before_and_schedules_probe(self):
        host = _host()
        with redirect_stderr(io.StringIO()):
            _show(host)
        (generation, mode, placement, points, below), = host.probes
        self.assertEqual(mode, "command_wheel")
        self.assertEqual(len(below), len(points))
        self.assertEqual(below[0], DESKTOP)

    def test_ring_only_when_corners_and_hub_show_the_desktop(self):
        report, text = self._measure(lambda name, expect: DESKTOP if expect == "clear" else RING)
        self.assertEqual(report["verdict"], "ring_only")
        line = next(l for l in text.splitlines() if "overlay pixel check" in l)
        self.assertTrue(line.startswith(overlay_host.INFO_PREFIX))
        for part in ("verdict=ring_only", "strategy=alpha", "clear=5/5", "clickthrough=5/5",
                     "drawn=1/1", "painted=-", "region=3 rects=2 [13,170,334x1;14,169,332x1]"):
            self.assertIn(part, line)
        corner = next(l for l in text.splitlines() if "point=corner_tl" in l)
        self.assertIn("expect=clear below=#c8d2dc shown=#c8d2dc result=clear source=below "
                      "hit=0x9009:Notepad:below", corner)

    def test_square_in_old_form_colour_is_named(self):
        report, text = self._measure(lambda name, expect: (0x11, 0x18, 0x27))
        self.assertEqual(report["verdict"], "square_visible")
        self.assertIn("painted=corner_tl,corner_tr,corner_bl,corner_br,hub", text)
        self.assertIn("result=painted source=form_111827", text)

    def test_hub_only_painted_is_background_visible(self):
        report, _text = self._measure(
            lambda name, expect: (34, 34, 34) if name in ("hub", "ring") else DESKTOP)
        self.assertEqual(report["verdict"], "background_visible")
        self.assertEqual(report["painted"], ["hub"])

    def test_clicks_on_clear_corners_reaching_overlay_are_counted(self):
        report, _text = self._measure(
            lambda name, expect: DESKTOP if expect == "clear" else RING, hit_overlay=True)
        self.assertEqual(report["clickthrough"], "0/5")

    def test_without_baseline_verdict_unknown(self):
        report, _text = self._measure(lambda name, expect: DESKTOP, below=None)
        self.assertEqual(report["verdict"], "unknown")

    def test_nothing_drawn_is_not_reported_as_success(self):
        report, _text = self._measure(lambda name, expect: DESKTOP)
        self.assertEqual(report["verdict"], "nothing_drawn")

    def test_clipboard_panel_only(self):
        report, text = self._measure(
            lambda name, expect: DESKTOP if expect == "clear" else RING,
            data={}, mode="clipboard")
        self.assertEqual(report["verdict"], "panel_only")
        self.assertIn("overlay pixel check mode=clipboard", text)

    def test_probe_skipped_when_hidden_before_measurement(self):
        host = _host()
        host.stop_event.wait = lambda _timeout: False
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
            host.visible = False
            self.assertIsNone(host._probe_after_show(*host.probes[0]))
        self.assertIn("overlay pixel check mode=command_wheel skipped", stderr.getvalue())

    def test_probe_after_show_measures_current_page(self):
        host = _host()
        host.stop_event.wait = lambda _timeout: False
        _screen.points = overlay_host.geometry.overlay_probe_points(
            "command_wheel", overlay_host.geometry.OverlayPlacement(0, 0, 360, 360, 96),
            WHEEL_DATA)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
            host._win32.screens = (_screen(
                lambda name, expect: DESKTOP if expect == "clear" else RING),)
            report = host._probe_after_show(*host.probes[0])
        self.assertEqual(report["verdict"], "ring_only")

    def test_sample_failure_logged(self):
        host = _host()

        def fail(*_args):
            raise OSError("BitBlt failed")

        host._win32.sample_screen = fail
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
        self.assertIn("overlay pixel sample failed (BitBlt failed)", stderr.getvalue())
        self.assertIsNone(host.probes[0][4])


class ProbePointTests(unittest.TestCase):
    def _points(self, mode, data, dpi=96):
        size = overlay_host.geometry.MODE_SIZES_CSS[mode]
        scale = dpi / 96
        placement = overlay_host.geometry.OverlayPlacement(
            0, 0, int(size[0] * scale), int(size[1] * scale), dpi)
        return {name: expect for name, _x, _y, expect
                in overlay_host.geometry.overlay_probe_points(mode, placement, data)}

    def test_wheel_corners_and_hub_clear_ring_and_pill_drawn(self):
        two_pages = {"actions": WHEEL_DATA["actions"], "pages": [["a", "b"], ["c", "d"]]}
        for dpi in (96, 144):
            self.assertEqual(self._points("command_wheel", two_pages, dpi), {
                "corner_tl": "clear", "corner_tr": "clear", "corner_bl": "clear",
                "corner_br": "clear", "hub": "clear", "ring": "shape", "pill": "shape"})

    def test_single_slot_ring_point_is_inside(self):
        one = {"actions": {"a": {"label": "a"}}, "pages": [["a"]]}
        self.assertEqual(self._points("command_wheel", one)["ring"], "shape")

    def test_clipboard_rounded_corners_clear_panel_drawn(self):
        self.assertEqual(self._points("clipboard", {}), {
            "corner_tl": "clear", "corner_tr": "clear", "corner_bl": "clear",
            "corner_br": "clear", "panel": "shape"})


class TransparentWindowTests(unittest.TestCase):
    def test_navigation_no_longer_shows_the_hidden_window(self):
        host = _host()
        host.window.transparent = True
        host._keep_hidden_on_navigation(host.window)
        self.assertFalse(host.window.transparent)


if __name__ == "__main__":
    unittest.main()
