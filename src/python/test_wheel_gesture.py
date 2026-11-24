"""Command Wheel hold-and-swipe: direction -> sector, tap vs. hold, host release.

Covers the pure gesture rules (``wheel_gesture``), the trigger config
(``overlay_actions``), the ``wheel_release`` IPC message and the productive
overlay host decision in headless mode.
"""
from __future__ import annotations

import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import overlay_actions as oa
import overlay_controller
import overlay_host
import overlay_protocol as protocol
import wheel_gesture as wg
from overlay_host import DismissPolicy, MOUSE_BUTTONS, VK_LBUTTON

TRIGGER = dict(wg.DEFAULT_TRIGGER)
UP = {vk: False for vk in MOUSE_BUTTONS}


class AimSlotTests(unittest.TestCase):
    def test_eight_sectors_clockwise_from_top(self):
        # Sector 0 top, 2 right, 4 bottom, 6 left (screen y grows downwards).
        self.assertEqual(wg.aim_slot(0, -100, 8, 30), 0)
        self.assertEqual(wg.aim_slot(100, -100, 8, 30), 1)
        self.assertEqual(wg.aim_slot(100, 0, 8, 30), 2)
        self.assertEqual(wg.aim_slot(0, 100, 8, 30), 4)
        self.assertEqual(wg.aim_slot(-100, 0, 8, 30), 6)
        self.assertEqual(wg.aim_slot(-100, -100, 8, 30), 7)

    def test_direction_counts_not_distance(self):
        # Far beyond the ring (166 px) still selects the right sector.
        self.assertEqual(wg.aim_slot(2000, 5, 8, 30), 2)
        self.assertEqual(wg.aim_slot(40, 1, 8, 30), 2)

    def test_dead_zone_selects_nothing(self):
        self.assertIsNone(wg.aim_slot(10, 10, 8, 30))
        self.assertIsNone(wg.aim_slot(0, 0, 8, 30))

    def test_two_sectors_and_single_slot(self):
        self.assertEqual(wg.aim_slot(0, -50, 2, 30), 0)
        self.assertEqual(wg.aim_slot(0, 50, 2, 30), 1)
        self.assertEqual(wg.aim_slot(50, 0, 1, 30), 0)
        self.assertIsNone(wg.aim_slot(50, 0, 0, 30))

    def test_sector_boundaries_match_the_svg_layout(self):
        # 3 sectors: 0 spans -150..-30 deg; just right of -30 is sector 1.
        import math
        for deg, expected in ((-149, 0), (-31, 0), (-29, 1), (89, 1), (91, 2), (209, 2)):
            dx = 100 * math.cos(math.radians(deg))
            dy = 100 * math.sin(math.radians(deg))
            self.assertEqual(wg.aim_slot(dx, dy, 3, 30), expected, deg)


class ReleaseOutcomeTests(unittest.TestCase):
    def test_tap_keeps_open_hold_executes_or_closes(self):
        self.assertEqual(wg.release_outcome(120, TRIGGER, 2), "keep_open")
        self.assertEqual(wg.release_outcome(250, TRIGGER, 2), "execute")
        self.assertEqual(wg.release_outcome(900, TRIGGER, None), "close")

    def test_tap_and_hold_can_be_switched_off_separately(self):
        no_tap = dict(TRIGGER, tap_open=False)
        self.assertEqual(wg.release_outcome(100, no_tap, 2), "close")
        self.assertEqual(wg.release_outcome(400, no_tap, 2), "execute")
        no_hold = dict(TRIGGER, hold_swipe=False)
        self.assertEqual(wg.release_outcome(100, no_hold, 2), "keep_open")
        self.assertEqual(wg.release_outcome(4000, no_hold, 2), "keep_open")

    def test_threshold_is_configurable(self):
        slow = dict(TRIGGER, hold_ms=600)
        self.assertEqual(wg.release_outcome(500, slow, 1), "keep_open")
        self.assertEqual(wg.release_outcome(600, slow, 1), "execute")


class HoldTrackerTests(unittest.TestCase):
    def test_press_release_and_autorepeat(self):
        now = [10.0]
        tracker = wg.HoldTracker(clock=lambda: now[0])
        self.assertTrue(tracker.press("hotkey"))
        self.assertFalse(tracker.press("hotkey"))       # autorepeat / second trigger
        self.assertFalse(tracker.press("ctrl_right_click"))
        now[0] = 10.42
        self.assertIsNone(tracker.release("mouse_x1"))  # other source
        self.assertEqual(tracker.release("hotkey"), 420)
        self.assertIsNone(tracker.release("hotkey"))
        self.assertTrue(tracker.press("mouse_x2"))

    def test_lost_release_expires(self):
        now = [0.0]
        tracker = wg.HoldTracker(clock=lambda: now[0])
        self.assertTrue(tracker.press("ctrl_right_click"))
        now[0] = wg.MAX_HOLD_SECONDS + 1
        self.assertTrue(tracker.press("hotkey"))
        self.assertEqual(tracker.active_source, "hotkey")


class TriggerConfigTests(unittest.TestCase):
    def test_defaults_and_old_configs(self):
        self.assertEqual(oa.normalize_wheel_config({"pages": [["copy"]]})["trigger"],
                         {"tap_open": True, "hold_swipe": True, "hold_ms": 250})
        payload = oa.wheel_payload({})
        self.assertEqual(payload["trigger"], wg.DEFAULT_TRIGGER)
        self.assertNotIn("hold", payload)

    def test_partial_update_keeps_other_fields(self):
        base = oa.normalize_trigger({"hold_ms": 400})
        merged = oa.normalize_trigger({"tap_open": False}, base)
        self.assertEqual(merged, {"tap_open": False, "hold_swipe": True, "hold_ms": 400})
        # A stored base from before 0.7.0-dev.49 with mouse_button does not leak through.
        merged = oa.normalize_trigger({}, {"hold_ms": 300, "mouse_button": "x1"})
        self.assertEqual(merged, {"tap_open": True, "hold_swipe": True, "hold_ms": 300})

    def test_invalid_trigger_values_are_rejected(self):
        for value, code in (
                ({"hold_ms": 50}, "invalid_trigger"),
                ({"hold_ms": 2001}, "invalid_trigger"),
                ({"hold_ms": True}, "invalid_trigger"),
                ({"tap_open": "yes"}, "invalid_trigger"),
                ({"mouse_button": "x1"}, "invalid_trigger"),
                ({"other": 1}, "invalid_trigger"),
                ("x1", "invalid_trigger"),
                ({"tap_open": False, "hold_swipe": False}, "trigger_disabled")):
            with self.subTest(value=value):
                with self.assertRaises(oa.ActionError) as ctx:
                    oa.normalize_trigger(value)
                self.assertEqual(ctx.exception.code, code)

    def test_payload_trigger_is_sanitized_in_the_host(self):
        self.assertEqual(wg.trigger_from_payload({"trigger": {"hold_ms": "9", "tap_open": 0}}),
                         wg.DEFAULT_TRIGGER)
        self.assertEqual(wg.trigger_from_payload(
            {"trigger": {"tap_open": False, "hold_swipe": False}})["tap_open"], True)


class ProtocolTests(unittest.TestCase):
    def test_wheel_release_messages(self):
        message = protocol.make_message("wheel_release", {"held_ms": 300}, "r1")
        self.assertEqual(message["payload"], {"held_ms": 300})
        protocol.make_message("wheel_released", {"outcome": "execute", "action_id": "copy"}, "r1")
        protocol.make_message("wheel_released", {"outcome": "close", "action_id": None}, "r1")
        for message_type, payload in (
                ("wheel_release", {"held_ms": -1}),
                ("wheel_release", {"held_ms": True}),
                ("wheel_release", {}),
                ("wheel_released", {"outcome": "execute", "action_id": None}),
                ("wheel_released", {"outcome": "close", "action_id": "copy"}),
                ("wheel_released", {"outcome": "boom", "action_id": None})):
            with self.subTest(message_type=message_type, payload=payload):
                with self.assertRaises(protocol.OverlayProtocolError):
                    protocol.make_message(message_type, payload, "r1")


class _Pipe:
    def __init__(self):
        self.messages = []

    def send_bytes(self, data):
        self.messages.append(json.loads(data.decode("utf-8")))


CX, CY = 960, 520


def _host_with_wheel(hold=True, trigger=None, cursor=None):
    args = types.SimpleNamespace(headless=True, startup_timeout=1.0)
    host = overlay_host.OverlayHost(args)
    host.connection = _Pipe()
    data = {
        "pages": [["copy", "paste", "cut", "delete"], ["undo"]],
        "actions": {"copy": {}, "paste": {}, "cut": {}, "delete": {}, "undo": {}},
        "trigger": dict(wg.DEFAULT_TRIGGER, **(trigger or {})),
    }
    if hold:
        data["hold"] = True
    host._run_ui("show", {"mode": "command_wheel",
                          "target": {"kind": "local", "identity": "local"},
                          "x": CX, "y": CY, "data": data})
    host._cursor = lambda: cursor
    return host


def _release(host, held_ms):
    host._handle_message({"type": "wheel_release", "request_id": "r1",
                          "payload": {"held_ms": held_ms}})
    reply = host.connection.messages[-1]
    assert reply["type"] == "wheel_released" and reply["request_id"] == "r1", reply
    return reply["payload"]


def _hidden(host):
    return [m["payload"] for m in host.connection.messages if m["type"] == "overlay_hidden"]


class HostReleaseTests(unittest.TestCase):
    def test_hold_and_swipe_right_selects_the_right_sector(self):
        # 4 sectors: 0 top, 1 right, 2 bottom, 3 left.
        host = _host_with_wheel(cursor=(CX + 400, CY + 10))
        self.assertEqual(_release(host, 400), {"outcome": "execute", "action_id": "paste"})
        self.assertFalse(host.visible)
        self.assertEqual(_hidden(host), [{"reason": "swipe", "mode": "command_wheel"}])

    def test_release_in_the_center_closes_without_action(self):
        host = _host_with_wheel(cursor=(CX + 5, CY - 5))
        self.assertEqual(_release(host, 400), {"outcome": "close", "action_id": None})
        self.assertFalse(host.visible)
        self.assertEqual(_hidden(host), [{"reason": "swipe_center", "mode": "command_wheel"}])

    def test_short_tap_keeps_the_wheel_open_for_clicking(self):
        host = _host_with_wheel(cursor=(CX + 400, CY))
        self.assertEqual(_release(host, 90), {"outcome": "keep_open", "action_id": None})
        self.assertTrue(host.visible)
        self.assertEqual(_hidden(host), [])
        # The hold is over: a second release report changes nothing.
        self.assertEqual(_release(host, 900), {"outcome": "ignored", "action_id": None})
        self.assertTrue(host.visible)

    def test_tap_switched_off_closes_on_a_short_tap(self):
        host = _host_with_wheel(trigger={"tap_open": False}, cursor=(CX, CY))
        self.assertEqual(_release(host, 90)["outcome"], "close")
        self.assertEqual(_hidden(host), [{"reason": "tap_off", "mode": "command_wheel"}])

    def test_swipe_switched_off_keeps_open_after_a_hold(self):
        host = _host_with_wheel(trigger={"hold_swipe": False}, cursor=(CX + 400, CY))
        self.assertEqual(_release(host, 900)["outcome"], "keep_open")
        self.assertTrue(host.visible)

    def test_swipe_uses_the_current_page(self):
        host = _host_with_wheel(cursor=(CX - 300, CY))
        host.bridge_event("wheel_page", {"page": 1})
        self.assertEqual(_release(host, 400), {"outcome": "execute", "action_id": "undo"})

    def test_wheel_opened_without_hold_ignores_releases(self):
        host = _host_with_wheel(hold=False, cursor=(CX + 400, CY))
        self.assertEqual(_release(host, 900)["outcome"], "ignored")
        self.assertTrue(host.visible)

    def test_release_after_escape_is_ignored(self):
        host = _host_with_wheel(cursor=(CX + 400, CY))
        host.bridge_hide(reason="escape")
        self.assertEqual(_release(host, 900)["outcome"], "ignored")
        self.assertEqual(_hidden(host), [{"reason": "escape", "mode": "command_wheel"}])

    def test_aim_follows_the_cursor_only_while_holding(self):
        host = _host_with_wheel()
        pushed = []
        host._push_aim = pushed.append
        generation = host._show_generation
        host._update_aim((CX, CY - 300), generation)
        host._update_aim((CX, CY - 900), generation)   # same sector: no new push
        host._update_aim((CX + 2, CY + 1), generation)  # dead zone
        self.assertEqual(pushed, [0, None])
        host._cursor = lambda: (CX, CY + 300)
        _release(host, 50)
        host._update_aim((CX, CY + 300), generation)
        self.assertEqual(pushed, [0, None])


class DismissWhileHoldingTests(unittest.TestCase):
    def test_clicks_do_not_close_while_the_trigger_is_held(self):
        policy = DismissPolicy(1, 2, UP, False, lambda x, y: False)
        pressed = {**UP, VK_LBUTTON: True}
        self.assertIsNone(policy.update(2, pressed, False, (5, 5), holding=True))
        self.assertIsNone(policy.update(2, UP, False, (5, 5), holding=True))
        self.assertEqual(policy.update(2, UP, True, (5, 5), holding=True), "escape")

    def test_after_the_hold_outside_clicks_close_again(self):
        policy = DismissPolicy(1, 2, UP, False, lambda x, y: False)
        self.assertIsNone(policy.update(2, UP, False, (5, 5), holding=True))
        pressed = {**UP, VK_LBUTTON: True}
        self.assertEqual(policy.update(2, pressed, False, (5, 5)), "outside_click")


class ControllerReleaseTests(unittest.TestCase):
    def test_release_without_a_running_host_starts_nothing(self):
        controller = overlay_controller.OverlayController.__new__(overlay_controller.OverlayController)
        import threading
        controller._lock = threading.Lock()
        controller._connection = None
        controller._ready = False
        self.assertEqual(controller._do_wheel_release(300),
                         {"outcome": "ignored", "action_id": None})

    def test_invalid_held_time_is_ignored(self):
        controller = overlay_controller.OverlayController.__new__(overlay_controller.OverlayController)
        self.assertEqual(controller.wheel_release(-5)["outcome"], "ignored")
        self.assertEqual(controller.wheel_release(True)["outcome"], "ignored")


if __name__ == "__main__":
    unittest.main()
