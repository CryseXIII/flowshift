"""Overlay dismissal: stays open without foreground, closes with a logged reason."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import overlay_controller
import overlay_host
from overlay_host import (DismissPolicy, DragSession, MOUSE_BUTTONS, VK_ESCAPE, VK_LBUTTON,
                          VK_RBUTTON)

OVERLAY = 0x1001
PREVIOUS = 0x2002
OTHER = 0x3003
TASKBAR = 0x4004
UP ={vk: False for vk in MOUSE_BUTTONS}


def down(*vks):
    state = dict(UP)
    for vk in vks:
        state[vk] = True
    return state


def policy(hit=lambda x, y: x < 100, buttons=None, escape=False, previous=PREVIOUS):
    return DismissPolicy(OVERLAY, previous, buttons or UP, escape, hit)


class DismissPolicyTests(unittest.TestCase):
    def test_stays_open_when_windows_refuses_the_foreground(self):
        # The regression: SetForegroundWindow was refused, the previous window
        # kept the foreground and the old watcher closed after ~0.75 s.
        p = policy()
        for _ in range(1000):  # 30 s at the 30 ms poll interval
            self.assertIsNone(p.update(PREVIOUS, UP, False, (500, 500)))

    def test_stays_open_while_overlay_is_foreground_and_on_inside_clicks(self):
        p = policy()
        self.assertIsNone(p.update(OVERLAY, UP, False, (50, 50)))
        self.assertIsNone(p.update(OVERLAY, down(VK_LBUTTON), False, (50, 50)))
        self.assertIsNone(p.update(OVERLAY, UP, False, (50, 50)))

    def test_click_on_transparent_area_closes(self):
        p = policy()
        self.assertIsNone(p.update(PREVIOUS, UP, False, (500, 500)))
        self.assertEqual(p.update(PREVIOUS, down(VK_LBUTTON), False, (500, 500)),
                         "outside_click")

    def test_right_and_middle_clicks_outside_close_too(self):
        self.assertEqual(policy().update(None, down(VK_RBUTTON), False, (500, 1)),
                         "outside_click")

    def test_button_held_at_show_is_not_a_new_click(self):
        # Ctrl+right click opened the wheel; releasing or holding it is no dismissal.
        p = policy(buttons=down(VK_RBUTTON))
        self.assertIsNone(p.update(PREVIOUS, down(VK_RBUTTON), False, (500, 500)))
        self.assertIsNone(p.update(PREVIOUS, UP, False, (500, 500)))
        self.assertEqual(p.update(PREVIOUS, down(VK_RBUTTON), False, (500, 500)),
                         "outside_click")

    def test_escape_closes_without_foreground(self):
        p = policy()
        self.assertIsNone(p.update(PREVIOUS, UP, False, (50, 50)))
        self.assertEqual(p.update(PREVIOUS, UP, True, (50, 50)), "escape")

    def test_escape_held_at_show_needs_a_new_press(self):
        p = policy(escape=True)
        self.assertIsNone(p.update(PREVIOUS, UP, True, (50, 50)))
        self.assertIsNone(p.update(PREVIOUS, UP, False, (50, 50)))
        self.assertEqual(p.update(PREVIOUS, UP, True, (50, 50)), "escape")

    def test_other_window_activated_closes(self):
        self.assertEqual(policy().update(OTHER, UP, False, (50, 50)), "focus_lost")

    def test_returning_to_previous_window_after_overlay_had_focus_closes(self):
        p = policy()
        self.assertIsNone(p.update(OVERLAY, UP, False, (50, 50)))
        self.assertEqual(p.update(PREVIOUS, UP, False, (50, 50)), "focus_lost")

    def test_transient_null_foreground_is_ignored(self):
        p = policy()
        self.assertIsNone(p.update(None, UP, False, (50, 50)))
        self.assertIsNone(p.update(PREVIOUS, UP, False, (50, 50)))

    def test_foreground_shuffle_right_after_the_show_keeps_the_clipboard_open(self):
        # Wheel -> Clipboard: hiding the wheel window hands the foreground to
        # a window Windows picks (here the taskbar) before the clipboard takes
        # it. Without the settle phase that closed the clipboard at once.
        self.assertEqual(policy().update(TASKBAR, UP, False, (50, 50)), "focus_lost")
        p = DismissPolicy(OVERLAY, PREVIOUS, UP, False, lambda x, y: x < 100, settle_polls=3)
        self.assertIsNone(p.update(TASKBAR, UP, False, (50, 50)))
        for _ in range(100):
            self.assertIsNone(p.update(TASKBAR, UP, False, (50, 50)))
        self.assertIsNone(p.update(OVERLAY, UP, False, (50, 50)))
        self.assertEqual(p.update(OTHER, UP, False, (50, 50)), "focus_lost")

    def test_settle_phase_does_not_cover_later_focus_changes_or_clicks(self):
        p = DismissPolicy(OVERLAY, PREVIOUS, UP, False, lambda x, y: x < 100, settle_polls=2)
        self.assertIsNone(p.update(PREVIOUS, UP, False, (50, 50)))
        self.assertIsNone(p.update(PREVIOUS, UP, False, (50, 50)))
        self.assertEqual(p.update(OTHER, UP, False, (50, 50)), "focus_lost")
        p = DismissPolicy(OVERLAY, PREVIOUS, UP, False, lambda x, y: x < 100, settle_polls=5)
        self.assertEqual(p.update(PREVIOUS, down(VK_LBUTTON), False, (500, 50)),
                         "outside_click")
        p = DismissPolicy(OVERLAY, PREVIOUS, UP, False, lambda x, y: x < 100, settle_polls=5)
        self.assertEqual(p.update(PREVIOUS, UP, True, (50, 50)), "escape")


class DragSessionTests(unittest.TestCase):
    def session(self):
        # Window at 100,200, grabbed at 130,210 (offset 30,10).
        return DragSession((100, 200), (130, 210), False, {OVERLAY})

    def test_window_follows_the_cursor_while_the_button_is_down(self):
        s = self.session()
        self.assertEqual(s.step(True, False, OVERLAY, (130, 210)), ("stay",))
        self.assertEqual(s.step(True, False, OVERLAY, (230, 260)), ("move", 200, 250))
        self.assertEqual(s.step(True, False, None, (231, 260)), ("move", 201, 250))

    def test_release_anywhere_ends_the_drag(self):
        # The page never sees this mouseup (released over another program).
        s = self.session()
        s.step(True, False, OVERLAY, (900, 900))
        self.assertEqual(s.step(False, False, OTHER, (1900, 1000)), ("end", "release"))

    def test_escape_and_focus_loss_end_the_drag(self):
        s = self.session()
        self.assertEqual(s.step(True, True, OVERLAY, (140, 210)), ("end", "escape"))
        s = DragSession((100, 200), (130, 210), True, {OVERLAY})
        self.assertEqual(s.step(True, True, OVERLAY, (140, 210)), ("move", 110, 200))
        self.assertEqual(s.step(True, False, OTHER, (150, 210)), ("end", "focus_lost"))
        self.assertEqual(self.session().step(True, False, OVERLAY, None), ("end", "cursor"))


class _Pipe:
    def __init__(self):
        self.messages = []

    def send_bytes(self, data):
        self.messages.append(json.loads(data.decode("utf-8")))


def _headless_host():
    args = types.SimpleNamespace(headless=True, startup_timeout=1.0)
    host = overlay_host.OverlayHost(args)
    host.connection = _Pipe()
    return host


def _show(host, mode="command_wheel", x=960, y=520, data=None):
    payload = {
        "mode": mode, "target": {"kind": "local", "identity": "local"},
        "x": x, "y": y,
        "data": data if data is not None else {
            "pages": [["copy", "paste"], ["undo"]],
            "actions": {"copy": {}, "paste": {}, "undo": {}},
        },
    }
    return host._run_ui("show", payload)


class HostDismissTests(unittest.TestCase):
    def test_dismissal_reports_reason_and_mode_once(self):
        host = _headless_host()
        _show(host)
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            host.bridge_hide(reason="outside_click"))) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        hidden = [m for m in host.connection.messages if m["type"] == "overlay_hidden"]
        self.assertEqual(len(hidden), 1)
        self.assertEqual(hidden[0]["payload"], {"reason": "outside_click", "mode": "command_wheel"})
        self.assertFalse(host.visible)

    def test_stale_watcher_generation_does_not_hide_a_newer_show(self):
        host = _headless_host()
        _show(host)
        old = host._show_generation
        _show(host, mode="clipboard")
        self.assertTrue(host.bridge_hide(reason="focus_lost", generation=old))
        self.assertTrue(host.visible)
        self.assertEqual(host.connection.messages, [])

    def test_unknown_reason_is_normalized(self):
        host = _headless_host()
        _show(host)
        host.bridge_hide(reason="<script>")
        self.assertEqual(host.connection.messages[-1]["payload"]["reason"], "escape")

    def test_runtime_hide_request_echoes_reason(self):
        host = _headless_host()
        _show(host, mode="clipboard")
        host._handle_message({"type": "hide_overlay", "request_id": "r1",
                              "payload": {"reason": "action"}})
        message = host.connection.messages[-1]
        self.assertEqual((message["type"], message["request_id"]), ("overlay_hidden", "r1"))
        self.assertEqual(message["payload"], {"reason": "action", "mode": "clipboard"})

    def test_hit_test_follows_wheel_page_shape(self):
        host = _headless_host()
        result = _show(host)
        self.assertFalse(result["cursor_moved"])
        cx, cy = 960, 520
        # Page 1 has two sectors (top and bottom); the hub around the small
        # page pill is transparent.
        self.assertTrue(host._hit_test(cx, cy - 113))
        self.assertTrue(host._hit_test(cx, cy))
        self.assertFalse(host._hit_test(cx, cy + 35))
        self.assertFalse(host._hit_test(cx + 113, cy))  # gap between the two sectors
        host.bridge_event("wheel_page", {"page": 1})
        self.assertTrue(host._hit_test(cx + 113, cy))   # single slot: full ring
        self.assertFalse(host._hit_test(cx, cy + 35))
        self.assertFalse(host._hit_test(cx - 200, cy))

    def test_wheel_shifted_at_screen_edge_recenters_cursor(self):
        host = _headless_host()
        result = _show(host, x=3, y=4)
        self.assertTrue(result["cursor_moved"])
        self.assertEqual((result["x"], result["y"]), (0, 0))


class _FakeWin:
    """Scripted Win32 input state for the visible-host paths."""

    SEARCH_FIELD = 0x1101  # WebView2 child window inside the overlay form

    def __init__(self):
        self.lock = threading.Lock()
        self.left = False
        self.escape = False
        self.cursor_pos = (0, 0)
        self.foreground = OVERLAY
        self.moves = []
        self.below = OTHER
        self.pids = {OVERLAY: os.getpid(), PREVIOUS: 1, OTHER: 2, TASKBAR: 3}

    def key_down(self, vk):
        with self.lock:
            return self.left if vk == VK_LBUTTON else self.escape if vk == VK_ESCAPE else False

    def cursor(self):
        with self.lock:
            return self.cursor_pos

    def foreground_root(self):
        with self.lock:
            return self.foreground

    def root(self, hwnd):
        return OVERLAY if hwnd == self.SEARCH_FIELD else hwnd

    root_owner = root

    def window_pid(self, hwnd):
        return self.pids.get(hwnd)

    def window_at(self, x, y):
        return self.below, self.root(self.below), ""

    def move_window(self, hwnd, x, y):
        with self.lock:
            self.moves.append((hwnd, x, y))
        return True


def _visible_clipboard_host():
    """A shown clipboard host switched to the visible code paths with a fake Win32."""
    host = _headless_host()
    _show(host, mode="clipboard", x=500, y=400, data={})
    host.args.headless = False
    fake = _FakeWin()
    host._win = lambda: fake
    host._native_hwnd = lambda: OVERLAY
    return host, fake


def _wait(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


class HostDragTests(unittest.TestCase):
    def setUp(self):
        self.logged = []
        patcher = mock.patch.object(overlay_host.OverlayHost, "_info",
                                    staticmethod(self.logged.append))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_release_outside_the_window_ends_the_drag_for_good(self):
        host, fake = _visible_clipboard_host()
        start = host._placement
        fake.left, fake.cursor_pos = True, (start.x + 40, start.y + 20)
        self.assertTrue(host.bridge_begin_drag())
        with fake.lock:
            fake.cursor_pos = (start.x + 340, start.y + 120)
        self.assertTrue(_wait(lambda: (OVERLAY, start.x + 300, start.y + 100) in fake.moves))
        # Released while the cursor is over another program: the page gets no
        # mouseup, the host still sees the button up.
        with fake.lock:
            fake.cursor_pos = (start.x + 900, start.y + 500)
            fake.foreground = OVERLAY
        with fake.lock:
            fake.left = False
        self.assertTrue(_wait(lambda: any("overlay drag end reason=release" in line
                                          for line in self.logged)))
        moves = len(fake.moves)
        with fake.lock:
            fake.cursor_pos = (start.x + 1200, start.y + 700)
        time.sleep(0.05)
        self.assertEqual(len(fake.moves), moves)
        self.assertTrue(host.visible)
        # The click-outside test uses the moved position.
        self.assertEqual((host._placement.x, host._placement.y), fake.moves[-1][1:])
        self.assertTrue(host._hit_test(host._placement.x + 10, host._placement.y + 10))
        self.assertFalse(host._hit_test(start.x - 50, start.y - 50))

    def test_escape_or_focus_loss_end_the_drag_and_a_new_drag_can_start(self):
        host, fake = _visible_clipboard_host()
        start = host._placement
        fake.left, fake.cursor_pos = True, (start.x + 5, start.y + 5)
        self.assertTrue(host.bridge_begin_drag())
        self.assertFalse(host.bridge_begin_drag())  # one drag at a time
        with fake.lock:
            fake.escape = True
        self.assertTrue(_wait(lambda: any("reason=escape" in line for line in self.logged)))
        with fake.lock:
            fake.escape = False
        self.assertTrue(host.bridge_begin_drag())
        with fake.lock:
            fake.foreground = OTHER
        self.assertTrue(_wait(lambda: any("reason=focus_lost" in line for line in self.logged)))

    def test_no_drag_when_the_button_is_already_up_or_not_the_clipboard(self):
        host, fake = _visible_clipboard_host()
        fake.left = False
        self.assertFalse(host.bridge_begin_drag())
        fake.left = True
        host.mode = "command_wheel"
        self.assertFalse(host.bridge_begin_drag())
        time.sleep(0.03)
        self.assertEqual(fake.moves, [])

    def test_hiding_the_overlay_stops_a_running_drag(self):
        host, fake = _visible_clipboard_host()
        fake.left, fake.cursor_pos = True, (host._placement.x, host._placement.y)
        self.assertTrue(host.bridge_begin_drag())
        host.args.headless = True
        host.bridge_hide(reason="escape")
        self.assertTrue(_wait(lambda: any("reason=hidden" in line for line in self.logged)))


class HostClipboardFocusTests(unittest.TestCase):
    def test_click_into_the_search_field_is_inside_even_with_a_stale_placement(self):
        host, fake = _visible_clipboard_host()
        placement = host._placement
        fake.below = _FakeWin.SEARCH_FIELD
        far = (placement.x + placement.width + 300, placement.y - 200)
        self.assertTrue(host._hit_test(*far))
        p = DismissPolicy(OVERLAY, PREVIOUS, UP, False, host._hit_test)
        self.assertIsNone(p.update(OVERLAY, down(VK_LBUTTON), False, far))
        fake.below = OTHER
        self.assertEqual(p.update(OVERLAY, UP, False, far), None)
        self.assertEqual(p.update(OVERLAY, down(VK_LBUTTON), False, far), "outside_click")

    def test_wheel_keeps_pure_geometry_for_its_transparent_hub(self):
        host = _headless_host()
        _show(host)
        host.args.headless = False
        fake = _FakeWin()
        fake.below = OVERLAY
        host._win = lambda: fake
        host._native_hwnd = lambda: OVERLAY
        self.assertFalse(host._hit_test(960, 520 + 35))

    def test_previous_foreground_skips_the_overlays_own_windows(self):
        host, fake = _visible_clipboard_host()
        host._last_previous_root = PREVIOUS
        fake.foreground = OVERLAY      # the wheel window opening the clipboard
        self.assertEqual(host._previous_foreground(), PREVIOUS)
        fake.foreground = OTHER
        self.assertEqual(host._previous_foreground(), OTHER)


class ControllerHiddenLogTests(unittest.TestCase):
    def test_log_line_contains_reason_and_mode(self):
        self.assertEqual(
            overlay_controller._hidden_log_line({"reason": "focus_lost", "mode": "clipboard"}),
            "overlay_hidden reason=focus_lost mode=clipboard")
        self.assertEqual(overlay_controller._hidden_log_line({}),
                         "overlay_hidden reason=unknown mode=none")
        self.assertEqual(
            overlay_controller._hidden_log_line({"reason": "x y\n", "mode": "evil"}),
            "overlay_hidden reason=unknown mode=none")


if __name__ == "__main__":
    unittest.main()
