"""Natively drawn Command Wheel: scene, hit test and the host's surface switch."""
from __future__ import annotations

import io
import json
import math
import os
import sys
import threading
import types
import unittest
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import overlay_geometry as geometry
import overlay_host
import overlay_native_wheel as nw
from test_overlay_window_shape import HWND, WHEEL_DATA, _host, _Win

NATIVE_HWND = 0xA00A
SIZE = 540  # 360 CSS px at 150 %


def _data(pages, labels=None):
    names = {name for page in pages for name in page}
    return {"actions": {name: {"label": (labels or {}).get(name, name.upper())}
                        for name in names},
            "pages": pages}


def _polar(size, radius_css, angle_deg):
    factor = size / geometry.WHEEL_SIZE_CSS
    return (size / 2 + radius_css * factor * math.cos(math.radians(angle_deg)),
            size / 2 + radius_css * factor * math.sin(math.radians(angle_deg)))


class WheelModelTests(unittest.TestCase):
    def test_sector_hit_follows_the_drawn_sectors(self):
        model = nw.WheelModel(_data([["a", "b", "c", "d"]]), SIZE)
        mid = (geometry.WHEEL_INNER_RADIUS_CSS + geometry.WHEEL_OUTER_RADIUS_CSS) / 2
        # Clockwise from the top: a at -90 deg, b at 0, c at 90, d at 180.
        for index, angle in enumerate((-90, 0, 90, 180)):
            self.assertEqual(model.sector_at(*_polar(SIZE, mid, angle)), index)
        self.assertIsNone(model.sector_at(SIZE / 2, SIZE / 2 - 30))   # hub
        self.assertIsNone(model.sector_at(4, 4))                      # corner
        self.assertIsNone(model.sector_at(*_polar(SIZE, mid, -45)))   # gap between a and b

    def test_single_slot_ring_hits_everywhere_on_the_ring(self):
        model = nw.WheelModel(_data([["a"]]), SIZE)
        for angle in (-90, 10, 100, 200):
            self.assertEqual(model.sector_at(*_polar(SIZE, 110, angle)), 0)
        self.assertIsNone(model.sector_at(SIZE / 2, SIZE / 2))

    def test_scroll_cycles_pages_and_clears_hover(self):
        model = nw.WheelModel(_data([["a"], ["b"], ["c"]]), SIZE)
        model.hot = 0
        self.assertTrue(model.scroll(120))
        self.assertEqual((model.page, model.hot, model.slots), (1, None, ["b"]))
        self.assertTrue(model.scroll(-120))
        self.assertTrue(model.scroll(-120))
        self.assertEqual(model.page, 2)
        self.assertFalse(model.scroll(0))
        self.assertFalse(nw.WheelModel(_data([["a"]]), SIZE).scroll(120))

    def test_aim_wins_over_hover(self):
        model = nw.WheelModel(_data([["a", "b", "c"]]), SIZE)
        model.hot = 0
        model.aim = 2
        self.assertEqual(model.active, 2)
        model.aim = 7  # outside this page
        self.assertEqual(model.active, 0)

    def test_invalid_size_rejected(self):
        for size in (0, -1, 1.5, True):
            with self.assertRaises(ValueError):
                nw.WheelModel(WHEEL_DATA, size)


class WheelSceneTests(unittest.TestCase):
    def _fills(self, items):
        return [item for item in items if item["kind"] in ("sector", "fill", "dot")]

    def test_nothing_is_drawn_outside_the_ring(self):
        """Corners and everything beyond the outer radius stay alpha 0."""
        model = nw.WheelModel(_data([["a", "b", "c", "d"], ["e"]]), SIZE)
        outer = geometry.WHEEL_OUTER_RADIUS_CSS * SIZE / geometry.WHEEL_SIZE_CSS
        for item in self._fills(nw.wheel_scene(model)):
            points = ([point for outline in item["outlines"] for point in outline]
                      if "outlines" in item else
                      [(item["rect"][0], item["rect"][1]),
                       (item["rect"][0] + item["rect"][2], item["rect"][1] + item["rect"][3])])
            for x, y in points:
                self.assertLessEqual(math.hypot(x - SIZE / 2, y - SIZE / 2), outer + 0.01)
        for item in nw.wheel_scene(model):
            if item["kind"] == "text":
                x, y, w, h = item["rect"]
                for cx, cy in ((x, y), (x + w, y), (x, y + h), (x + w, y + h)):
                    self.assertLess(math.hypot(cx - SIZE / 2, cy - SIZE / 2), outer)

    def test_hub_is_empty_except_the_page_dots(self):
        model = nw.WheelModel(_data([["a", "b"], ["c"]]), SIZE)
        items = nw.wheel_scene(model)
        inner = geometry.WHEEL_INNER_RADIUS_CSS * SIZE / geometry.WHEEL_SIZE_CSS
        in_hub = [item["kind"] for item in self._fills(items)
                  if all(math.hypot(x - SIZE / 2, y - SIZE / 2) < inner
                         for outline in item.get("outlines", [[(item.get("rect") or (0, 0))[:2]]])
                         for x, y in outline)]
        # The pill and its two dots (Viktor 20.11.: the two points stay).
        self.assertEqual(in_hub, ["fill", "dot", "dot"])
        dots = [item for item in items if item["kind"] == "dot"]
        self.assertEqual([dot["active"] for dot in dots], [True, False])
        self.assertEqual(dots[0]["fill"], nw.DOT_ACTIVE)

    def test_sectors_labels_and_spotlight(self):
        model = nw.WheelModel(_data([["a", "b", "c"]], {"a": "Copy"}), SIZE)
        items = nw.wheel_scene(model)
        sectors = [item for item in items if item["kind"] == "sector"]
        texts = [item["text"] for item in items if item["kind"] == "text"]
        self.assertEqual([s["action"] for s in sectors], ["a", "b", "c"])
        self.assertEqual(texts, ["Copy", "B", "C"])
        self.assertTrue(all(s["fill"] == nw.SECTOR_FILL for s in sectors))
        self.assertFalse(any(item["kind"] == "dot" for item in items))
        model.hot = 1
        sectors = [item for item in nw.wheel_scene(model) if item["kind"] == "sector"]
        self.assertEqual(sectors[1]["fill"], nw.HOT_FILL)
        self.assertEqual(sectors[0]["fill"], nw.SECTOR_FILL)

    @staticmethod
    def _looks(items):
        """Per sector index: fill, stroke, width and label colour as painted."""
        looks, texts = {}, [item for item in items if item["kind"] == "text"]
        for item in (item for item in items if item["kind"] == "sector"):
            looks[item["index"]] = (item["fill"], item["stroke"], item["stroke_px"],
                                    texts[item["index"]]["color"])
        return looks

    def test_only_the_hovered_sector_changes_the_rest_stays_at_rest(self):
        """Viktor 22.11.: hovering lights one sector blue, the others look as
        with no mouse over the wheel (no dimming, nothing to flicker)."""
        pages = [["a", "b", "c", "d", "e", "f", "g", "h"], ["i", "j", "k"], ["l"]]
        model = nw.WheelModel(_data(pages), SIZE)
        mid = (geometry.WHEEL_INNER_RADIUS_CSS + geometry.WHEEL_OUTER_RADIUS_CSS) / 2
        for page in range(len(pages)):
            model.page, model.hot, model.aim = page, None, None
            rest = self._looks(nw.wheel_scene(model))
            count = len(model.slots)
            for source in ("hot", "aim"):
                for index in range(count):
                    model.hot, model.aim = (index, None) if source == "hot" else (None, index)
                    looks = self._looks(nw.wheel_scene(model))
                    self.assertEqual(looks[index][:3],
                                     (nw.HOT_FILL, nw.HOT_STROKE,
                                      nw.HOT_STROKE_CSS * SIZE / geometry.WHEEL_SIZE_CSS))
                    for other in range(count):
                        if other != index:
                            self.assertEqual(looks[other], rest[other],
                                             (page, source, index, other))
            # Gap between two sectors and the hub: no sector is hot, all at rest.
            step = 360 / count
            for point in (_polar(SIZE, mid, -90 + step / 2), (SIZE / 2, SIZE / 2)):
                model.hot, model.aim = model.sector_at(*point), None
                if count > 1 or point == (SIZE / 2, SIZE / 2):
                    self.assertIsNone(model.hot, (page, point))
                    self.assertEqual(self._looks(nw.wheel_scene(model)), rest)

    def test_hover_while_busy_or_failed_leaves_other_sectors_alone(self):
        model = nw.WheelModel(_data([["a", "b", "c"]]), SIZE)
        model.busy, model.error = "b", ("c", "rejected")
        rest = self._looks(nw.wheel_scene(model))
        model.hot = 0
        looks = self._looks(nw.wheel_scene(model))
        self.assertEqual((looks[1], looks[2]), (rest[1], rest[2]))
        self.assertEqual(looks[1][1], nw.BUSY_STROKE)
        self.assertEqual(looks[2][3], nw.ERROR_COLOR)

    def test_error_text_replaces_label(self):
        model = nw.WheelModel(_data([["a", "b"]]), SIZE)
        model.error = ("b", "rejected")
        texts = [(item["text"], item["color"]) for item in nw.wheel_scene(model)
                 if item["kind"] == "text"]
        self.assertEqual(texts, [("A", nw.LABEL_COLOR), ("rejected", nw.ERROR_COLOR)])

    def test_many_pages_show_page_text(self):
        model = nw.WheelModel(_data([[f"p{i}"] for i in range(10)]), SIZE)
        model.page = 3
        texts = [item["text"] for item in nw.wheel_scene(model) if item["kind"] == "text"]
        self.assertIn("4/10", texts)

    def test_single_slot_is_ring_with_hole(self):
        outlines = nw.sector_outlines(0, 1, SIZE)
        self.assertEqual(len(outlines), 2)
        inner = geometry.WHEEL_INNER_RADIUS_CSS * SIZE / geometry.WHEEL_SIZE_CSS
        for x, y in outlines[1]:
            self.assertAlmostEqual(math.hypot(x - SIZE / 2, y - SIZE / 2), inner, places=6)

    def test_empty_wheel_shows_hint(self):
        items = nw.wheel_scene(nw.WheelModel({"pages": []}, SIZE))
        self.assertEqual([item.get("text") for item in items if item["kind"] == "text"],
                         [nw.EMPTY_TEXT])

    def test_argb(self):
        self.assertEqual(nw.argb((230, 19, 24, 32)), 0xE6131820)
        self.assertEqual(nw.argb((300, -1, 0, 255)), 0xFF0000FF)

    def test_dwm_text(self):
        text = nw.dwm_text({"corner": (0, 1), "border": (0, None), "backdrop": (0x80070057, None)})
        self.assertEqual(text, "corner=set:0x0/read:1 border=set:0x0/read:- "
                               "backdrop=set:0x80070057/read:-")

    def test_native_window_needs_windows(self):
        if sys.platform == "win32":
            self.skipTest("Windows creates the real window")
        with self.assertRaises(OSError):
            nw.NativeWheelWindow(lambda _id: {}, lambda _page: None, lambda _text: None)


class _FakeNative:
    def __init__(self, on_execute, on_page, log, fail_show=False):
        self.on_execute, self.on_page, self.log = on_execute, on_page, log
        self.hwnd = NATIVE_HWND
        self.dwm = {"corner": (0, 1), "border": (0, None), "backdrop": (0, 1)}
        self.calls = []
        self.fail_show = fail_show

    def exstyle(self):
        return nw.WS_EX_LAYERED | nw.WS_EX_TOPMOST | nw.WS_EX_TOOLWINDOW

    def show(self, placement, data):
        if self.fail_show:
            raise OSError("UpdateLayeredWindow failed")
        self.calls.append(("show", placement.width, data))
        return True

    def hide(self):
        self.calls.append(("hide",))

    def set_aim(self, slot):
        self.calls.append(("aim", slot))

    def close(self):
        self.calls.append(("close",))


class _DwmWin(_Win):
    def dwm_attributes(self, hwnd):
        return (1, 1) if hwnd == NATIVE_HWND else (None, None)


def _native_host(fail_create=False, fail_show=False):
    host = _host(win=_DwmWin())
    host.args.url = "http://127.0.0.1:5000/overlay.html"
    host.made = []

    def factory(on_execute, on_page, log):
        if fail_create:
            raise OSError("RegisterClassExW failed")
        native = _FakeNative(on_execute, on_page, log, fail_show)
        host.made.append(native)
        return native

    host._native_factory = factory
    host.window.hide = lambda: host.window.calls.append("hide")
    host._send = lambda *args, **kwargs: host.sent.append(args) or True
    host.sent = []
    return host


def _show(host, mode="command_wheel", data=None):
    return host._show({"mode": mode, "target": {"kind": "local", "identity": "local"},
                       "x": 960, "y": 520, "data": WHEEL_DATA if data is None else data})


class HostNativeWheelTests(unittest.TestCase):
    def test_wheel_shows_in_native_window_not_in_webview(self):
        host = _native_host()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
        native = host.made[0]
        self.assertEqual(native.calls[0][0], "show")
        self.assertEqual(host.window.calls, [])           # the WebView2 form stays hidden
        self.assertEqual(host._win32.shapes, [])          # no window region needed
        self.assertEqual(host._native_hwnd(), NATIVE_HWND)
        self.assertTrue(host._hit_test(960, 520 - 113))   # ring above the hub
        self.assertFalse(host._hit_test(960, 520))        # hub
        log = stderr.getvalue()
        self.assertIn("overlay native wheel ready hwnd=0xa00a exstyle=0x00080088 "
                      "corner=set:0x0/read:1", log)
        self.assertIn("paint=updatelayeredwindow", log)
        self.assertIn("overlay native wheel shown hwnd=0xa00a", log)
        self.assertIn("dwm_corner=1 dwm_backdrop=1", log)
        self.assertIn("overlay shape native mode=command_wheel", log)
        self.assertEqual(len(host.probes), 1)             # pixel check scheduled
        self.assertIsNotNone(host.probes[0][4])           # desktop sampled before show

    def test_pixel_check_names_native_strategy_and_dwm(self):
        host = _native_host()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
            _gen, mode, placement, points, below = host.probes[0]
            report = host._measure(mode, placement, points, below)
        # The fake screen never changes: corners and hub read as desktop,
        # clicks there reach the window below, the ring counts as missing.
        self.assertEqual((report["clear"], report["clickthrough"], report["verdict"]),
                         ("5/5", "5/5", "nothing_drawn"))
        self.assertIn("strategy=native_layered", stderr.getvalue())
        self.assertIn("dwm_corner=1 dwm_backdrop=1", stderr.getvalue())

    def test_clipboard_after_wheel_hides_native_and_uses_webview(self):
        host = _native_host()
        with redirect_stderr(io.StringIO()):
            _show(host)
            _show(host, mode="clipboard", data={})
        native = host.made[0]
        self.assertEqual(native.calls[-1], ("hide",))
        self.assertEqual(host.window.calls, ["show"])
        self.assertEqual(host._native_hwnd(), HWND)
        with redirect_stderr(io.StringIO()):
            _show(host)
        self.assertEqual(native.calls[-1][0], "show")
        self.assertEqual(host.window.calls, ["show", "hide"])  # clipboard form hidden
        self.assertEqual(len(host.made), 1)               # one window per host

    def test_hide_and_aim_go_to_native_window(self):
        host = _native_host()
        with redirect_stderr(io.StringIO()):
            _show(host)
            host._push_aim(2)
            host._hide()
        self.assertEqual(host.made[0].calls[1:], [("aim", 2), ("hide",)])
        self.assertFalse(host.visible)

    def test_native_paging_refits_shape_and_reports_event(self):
        host = _native_host()
        data = {"actions": {"a": {"label": "A"}, "b": {"label": "B"}, "c": {"label": "C"}},
                "pages": [["a", "b"], ["c"]]}
        with redirect_stderr(io.StringIO()):
            _show(host, data=data)
            before = list(host._polygons)
            host.made[0].on_page(1)
        self.assertEqual(host._wheel_page_index, 1)
        self.assertNotEqual(host._polygons, before)
        self.assertIn(("overlay_event", {"event": "wheel_page", "data": {"page": 1}}), host.sent)

    def test_factory_failure_falls_back_to_webview_once(self):
        host = _native_host(fail_create=True)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
            _show(host)
        self.assertEqual(host.window.calls, ["show", "show"])
        self.assertEqual(stderr.getvalue().count("overlay native wheel unavailable "
                                                 "(RegisterClassExW failed) surface=webview"), 1)
        self.assertTrue(host._win32.shapes)
        self.assertTrue(all(shape == (HWND, 4) for shape in host._win32.shapes))

    def test_show_failure_falls_back_to_webview(self):
        host = _native_host(fail_show=True)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            _show(host)
        self.assertEqual(host.window.calls, ["show"])
        self.assertIn("overlay native wheel show failed (UpdateLayeredWindow failed)",
                      stderr.getvalue())
        self.assertIn("strategy=alpha", "strategy=" + host._strategy())

    def test_ctypes_argument_error_on_show_still_falls_back(self):
        host = _native_host()
        stderr = io.StringIO()

        def broken(*_args):
            raise __import__("ctypes").ArgumentError("argument 2: wrong type")

        with redirect_stderr(stderr):
            host._native_wheel_window()
            host._native_wheel.show = broken
            _show(host)
            _show(host)
        self.assertEqual(host.window.calls, ["show", "show"])
        self.assertIn("overlay native wheel show failed (argument 2: wrong type)", stderr.getvalue())
        self.assertIn(("hide",), host.made[0].calls)

    def test_headless_never_creates_native_window(self):
        host = _native_host()
        host.args.headless = True
        _show(host)
        self.assertEqual(host.made, [])


class _ApiHandler(BaseHTTPRequestHandler):
    replies = []
    seen = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).seen.append((self.path, self.headers.get("Content-Type"),
                                 self.headers.get("Origin"), json.loads(body)))
        status, reply = type(self).replies.pop(0)
        data = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


class NativeExecuteTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _ApiHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        _ApiHandler.seen = []
        self.host = _native_host()
        self.host.args.url = f"http://127.0.0.1:{self.server.server_address[1]}/overlay.html"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _run(self, status, reply):
        _ApiHandler.replies = [(status, reply)]
        with redirect_stderr(io.StringIO()):
            return self.host._native_execute("copy")

    def test_click_posts_action_like_the_react_wheel(self):
        result = self._run(200, {"ok": True, "result": {"ok": True}})
        self.assertEqual(result, {"ok": True, "result": {"ok": True}})
        origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.assertEqual(_ApiHandler.seen, [("/api/actions/execute", "application/json", origin,
                                             {"action_id": "copy",
                                              "context": {"source": "command_wheel"}})])

    def test_conflict_reply_is_passed_through(self):
        self.assertEqual(self._run(409, {"ok": False, "reason": "busy"}),
                         {"ok": False, "reason": "busy"})

    def test_server_error_becomes_reason(self):
        self.assertEqual(self._run(400, {"ok": False, "error": "unknown_action",
                                         "message": "unknown action"}),
                         {"ok": False, "reason": "unknown action"})

    def test_unreachable_api_is_a_failed_result(self):
        self.host.args.url = "http://127.0.0.1:9/overlay.html"
        with redirect_stderr(io.StringIO()):
            result = self.host._native_execute("copy")
        self.assertIs(result["ok"], False)
        self.assertTrue(result["reason"])


if __name__ == "__main__":
    unittest.main()
