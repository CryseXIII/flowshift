"""Overlay dismissal: stays open without foreground, closes with a logged reason."""
from __future__ import annotations

import json
import os
import sys
import threading
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import overlay_controller
import overlay_host
from overlay_host import DismissPolicy, MOUSE_BUTTONS, VK_LBUTTON, VK_RBUTTON

OVERLAY = 0x1001
PREVIOUS = 0x2002
OTHER = 0x3003
UP = {vk: False for vk in MOUSE_BUTTONS}


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
