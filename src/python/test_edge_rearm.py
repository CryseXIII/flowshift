"""After a return the same screen edge must not switch again at once.

Viktor 27.11. (v0.7.3, two PCs): his PC has three monitors, PC 2 lies North,
on PC 2 his PC lies South. Returning, especially by hotkey (Ctrl+Alt+0), left
the pointer at the North edge, which switched to PC 2 again straight away.
Covered without a real mouse: the re-arm guard (edge held until the mouse was
``rearm_distance_px`` away once and ``rearm_ms`` passed), the pointer moved
inwards on the monitor it left from, the return over the edge of the other
PC, the mirror case on PC 2 (entry over South, no bounce back) and the one
Event Log line for a held-back switch.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime_model as rm
from test_tray_remote_preview import _import_tray

PC1_ID = "1aa0000000000001"
PC2_ID = "2bb0000000000002"
# Viktor's PC: three monitors side by side, the middle one is the tallest.
LEFT = {"left": -1920, "top": 120, "width": 1920, "height": 1080}
MIDDLE = {"left": 0, "top": 0, "width": 2560, "height": 1440}
RIGHT = {"left": 2560, "top": 180, "width": 1920, "height": 1080}
PC1_SCREEN = {"left": -1920, "top": 0, "width": 6400, "height": 1440}
PC2_SCREEN = {"left": 0, "top": 0, "width": 1920, "height": 1080}


def monitor_at(x, y):
    for m in (LEFT, MIDDLE, RIGHT):
        if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
            return dict(m)
    return None


class EdgeGeometryTest(unittest.TestCase):
    def test_distance_and_band(self):
        self.assertEqual(rm.edge_distance("north", 100, 0, PC1_SCREEN), 0)
        self.assertEqual(rm.edge_distance("north", 100, 40, PC1_SCREEN), 40)
        self.assertEqual(rm.edge_distance("east", 4479, 500, PC1_SCREEN), 0)
        self.assertEqual(rm.edge_distance("west", -1900, 500, PC1_SCREEN), 20)
        self.assertEqual(rm.edge_distance("south", 10, 1079 - 5, PC2_SCREEN), 5)
        self.assertEqual(rm.edges_at_point(1200, 2, PC1_SCREEN, 3), ["north"])
        self.assertEqual(rm.edges_at_point(-1920, 0, PC1_SCREEN, 3), ["north", "west"])
        self.assertEqual(rm.edges_at_point(1200, 700, PC1_SCREEN, 3), [])

    def test_pointer_moves_inwards_on_its_own_monitor(self):
        # North edge on the middle monitor: straight down by the distance.
        self.assertEqual(rm.point_moved_inward(1200, 1, ["north"], 40, PC1_SCREEN, MIDDLE), (1200, 40))
        # East edge of the right monitor: left by the distance, same height.
        self.assertEqual(rm.point_moved_inward(4479, 500, ["east"], 40, PC1_SCREEN, RIGHT), (4439, 500))
        # Corner: away from both edges.
        self.assertEqual(rm.point_moved_inward(-1920, 0, ["north", "west"], 40, PC1_SCREEN, MIDDLE),
                         (0, 40))
        # Never off the monitor it is on (small monitor at the top).
        tiny = {"left": 0, "top": 0, "width": 800, "height": 30}
        self.assertEqual(rm.point_moved_inward(100, 0, ["north"], 40, PC1_SCREEN, tiny), (100, 29))

    def test_layout_keeps_the_rearm_values(self):
        layout, _ = rm.normalize_display_layout({"enabled": True})
        self.assertEqual((layout["rearm_distance_px"], layout["rearm_ms"]),
                         (rm.EDGE_REARM_DISTANCE_PX, rm.EDGE_REARM_MS))
        layout, _ = rm.normalize_display_layout({"rearm_distance_px": "60", "rearm_ms": 0})
        self.assertEqual((layout["rearm_distance_px"], layout["rearm_ms"]), (60, 0))
        layout, _ = rm.normalize_display_layout({"rearm_distance_px": "x", "rearm_ms": None})
        self.assertEqual((layout["rearm_distance_px"], layout["rearm_ms"]), (40, 400))
        self.assertIn("rearm_distance_px", rm.default_display_layout())
        # Unchanged older values.
        self.assertEqual((layout["threshold_px"], layout["inset_px"], layout["cooldown_ms"],
                          layout["return_cooldown_ms"]), (3, 24, 600, 400))


class EdgeRearmGuardTest(unittest.TestCase):
    def test_held_until_the_mouse_left_and_the_time_passed(self):
        g = rm.EdgeRearmGuard()
        g.hold(["north"], 100.0, "hotkey", "PC2")
        self.assertEqual(g.check("north"), "held_first")
        self.assertEqual(g.check("north"), "held")
        self.assertEqual(g.check("south"), "armed")
        # Resting at the edge for a long time does not re-arm it.
        g.update(1200, 1, PC1_SCREEN, 40, 400, 160.0)
        self.assertEqual(g.held_edges(), ["north"])
        # 39 px is still inside the band, 40 px is out.
        g.update(1200, 39, PC1_SCREEN, 40, 400, 161.0)
        self.assertEqual(g.held_edges(), ["north"])
        self.assertEqual(g.update(1200, 40, PC1_SCREEN, 40, 400, 161.0), ["north"])
        self.assertEqual(g.check("north"), "armed")

    def test_left_quickly_still_waits_for_the_lock_time(self):
        g = rm.EdgeRearmGuard()
        g.hold(["north"], 100.0)
        g.update(1200, 200, PC1_SCREEN, 40, 400, 100.1)   # left, but only 100 ms
        g.update(1200, 1, PC1_SCREEN, 40, 400, 100.2)     # back at the edge
        self.assertEqual(g.check("north"), "held_first")
        self.assertEqual(g.update(1200, 1, PC1_SCREEN, 40, 400, 100.5), ["north"])

    def test_distance_zero_is_a_time_lock_only(self):
        g = rm.EdgeRearmGuard()
        g.hold(["north"], 100.0)
        g.update(1200, 0, PC1_SCREEN, 0, 400, 100.3)
        self.assertEqual(g.held_edges(), ["north"])
        g.update(1200, 0, PC1_SCREEN, 0, 400, 100.4)
        self.assertEqual(g.held_edges(), [])

    def test_a_new_hold_logs_again(self):
        g = rm.EdgeRearmGuard()
        g.hold(["south"], 1.0)
        self.assertEqual(g.check("south"), "held_first")
        g.hold(["south", "bogus"], 2.0)
        self.assertEqual(g.held_edges(), ["south"])
        self.assertEqual(g.check("south"), "held_first")

    def test_message_names_edge_peer_and_the_way_back(self):
        text = rm.edge_held_back_message("north", "switch", "PC2", 40, 400)
        self.assertEqual(text, "edge switch north held back: the mouse came back from PC2 at this edge "
                               "just now; it is armed again once the mouse was 40 px away from the edge "
                               "(and after 400 ms)")
        self.assertIn("edge return south held back", rm.edge_held_back_message("south", "return", "", 0, 300))
        self.assertIn("after 300 ms", rm.edge_held_back_message("south", "return", "", 0, 300))


class TrayEdgeRearmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.class_tmp = tempfile.TemporaryDirectory()
        cls.old_env = {k: os.environ.get(k) for k in ("FLOWSHIFT_LOG_DIR", "FLOWSHIFT_CONFIG")}
        os.environ["FLOWSHIFT_LOG_DIR"] = cls.class_tmp.name
        os.environ["FLOWSHIFT_CONFIG"] = os.path.join(cls.class_tmp.name, "config.json")
        cls.tray = _import_tray()

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.class_tmp.cleanup()

    def setUp(self):
        tray = self.tray
        self.saved = {k: getattr(tray.istate, k) for k in
                      ("config", "edge_session", "active", "active_peer", "active_peer_label")}
        self.sent, self.moves, self.logs = [], [], []
        self.cursor = [1200, 700]
        self.screen = dict(PC1_SCREEN)

        def set_cursor(x, y):
            self.moves.append((x, y))
            self.cursor[:] = [x, y]
            return True

        for target, value in (
                ("_send_edge_message", lambda ident, msg: self.sent.append((ident, msg)) or True),
                ("_set_cursor_pos", set_cursor),
                ("_current_cursor_pos", lambda: tuple(self.cursor)),
                ("get_virtual_screen_spec", lambda: dict(self.screen)),
                ("_monitor_rect_at", monitor_at),
                ("_hook_mgr", mock.MagicMock()),
                ("_notify_fwd_state", lambda *a: None),
                ("_send_events_to_identity", lambda *a: None),
                ("_preview_stop", lambda *a, **k: None),
                ("update_tray", lambda: None),
                ("log", lambda level, msg: self.logs.append((level, msg)))):
            patcher = mock.patch.object(tray, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        tray._edge_rearm.clear()
        tray.istate.edge_session = None
        self.addCleanup(self._restore)

    def _restore(self):
        tray = self.tray
        tray._edge_rearm.clear()
        tray._clear_edge_session()
        tray.istate.set_clip(False)
        for key, value in self.saved.items():
            setattr(tray.istate, key, value)

    def pc1_config(self):
        return {"device_id": PC1_ID, "device_name": "PC1", "port": 45781,
                "peers": [{"name": "PC2", "host": "192.0.2.40", "port": 45781, "device_id": PC2_ID}],
                "display_layout": {"enabled": True, "edges": {
                    "north": {"peer_identity": f"device:{PC2_ID}", "target_entry_edge": "south"}}}}

    def pc2_config(self):
        return {"device_id": PC2_ID, "device_name": "PC2", "port": 45781,
                "peers": [{"name": "PC1", "host": "192.0.2.23", "port": 45781, "device_id": PC1_ID}],
                "display_layout": {"enabled": True, "edges": {
                    "south": {"peer_identity": f"device:{PC1_ID}", "target_entry_edge": "north"}}}}

    def forwarding_by_edge(self, x):
        """PC 1 forwards to PC 2 after the North edge; the pointer is frozen at the exit."""
        tray = self.tray
        tray.istate.config = self.pc1_config()
        tray.istate.edge_session = {"session_id": "s1", "role": "source",
                                    "source_identity": f"device:{PC1_ID}",
                                    "target_identity": f"device:{PC2_ID}",
                                    "source_exit_edge": "north", "target_entry_edge": "south",
                                    "orthogonal_position": 0.5, "source_screen": dict(PC1_SCREEN),
                                    "target_display_name": "PC2", "active": True}
        tray.istate.active = True
        tray.istate.active_peer = f"device:{PC2_ID}"
        tray.istate.active_peer_label = "PC2"
        self.cursor[:] = [x, 0]

    def held_back_lines(self):
        return [m for level, m in self.logs if level == "INFO" and "held back" in m]

    def test_hotkey_return_moves_the_pointer_inwards_and_holds_the_edge(self):
        tray = self.tray
        self.forwarding_by_edge(1200)
        tray.deactivate_forward("hotkey")
        # Off the edge, the distance down, still on the middle monitor.
        self.assertEqual(tuple(self.cursor), (1200, 40))
        self.assertEqual(monitor_at(*self.cursor), MIDDLE)
        self.assertEqual(tray._edge_rearm.held_edges(), ["north"])
        self.assertEqual(self.sent[-1][1]["type"], "edge_cancel")
        # Straight back up within the lock time: held, one log line only.
        now = time.monotonic()
        tray._edge_rearm.update(1200, 0, PC1_SCREEN, 40, 400, now + 0.1)
        self.assertTrue(tray._edge_held_back("north", "switch"))
        self.assertTrue(tray._edge_held_back("north", "switch"))
        self.assertTrue(tray._edge_held_back("north", "switch"))
        lines = self.held_back_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("edge switch north held back", lines[0])
        self.assertIn("from PC2", lines[0])
        # Away by the distance and the lock time over: the edge switches again.
        tray._edge_rearm.update(1200, 300, PC1_SCREEN, 40, 400, now + 1.0)
        self.assertFalse(tray._edge_held_back("north", "switch"))
        self.assertIn("held_edges", tray.build_status_snapshot()["edge_switching"])

    def test_hotkey_return_on_the_right_monitor_stays_there(self):
        tray = self.tray
        self.forwarding_by_edge(0)
        tray.istate.edge_session = None
        self.cursor[:] = [4479, 600]       # pointer frozen at the east edge, right monitor
        tray.deactivate_forward("hotkey")
        self.assertEqual(tuple(self.cursor), (4439, 600))
        self.assertEqual(monitor_at(*self.cursor), RIGHT)
        self.assertEqual(tray._edge_rearm.held_edges(), ["east"])

    def test_hotkey_return_away_from_edges_leaves_the_pointer(self):
        tray = self.tray
        self.forwarding_by_edge(0)
        tray.istate.edge_session = None
        self.cursor[:] = [900, 700]
        tray.deactivate_forward("hotkey")
        self.assertEqual(self.moves, [])
        self.assertEqual(tray._edge_rearm.held_edges(), [])

    def test_lost_connection_and_cancel_hold_the_edge_too(self):
        tray = self.tray
        for reason in ("peer-not-connected", "send-failed"):
            tray._edge_rearm.clear()
            self.forwarding_by_edge(1500)
            tray.deactivate_forward(reason)
            self.assertEqual(tuple(self.cursor), (1500, 40), reason)
            self.assertEqual(tray._edge_rearm.held_edges(), ["north"], reason)
        tray._edge_rearm.clear()
        self.forwarding_by_edge(1500)
        tray._handle_edge_cancel({"type": "edge_cancel", "session_id": "s1", "reason": "busy"})
        self.assertEqual(tuple(self.cursor), (1500, 40))
        self.assertEqual(tray._edge_rearm.held_edges(), ["north"])

    def test_return_over_pc2s_edge_comes_in_at_the_inset_and_does_not_switch_back(self):
        tray = self.tray
        self.forwarding_by_edge(1200)
        tray._handle_edge_return({"type": "edge_return", "session_id": "s1", "orthogonal_position": 0.25})
        x, y = self.cursor
        self.assertEqual(y, 24)                      # unchanged inset
        self.assertEqual((x, y), rm.edge_position_to_point("north", 0.25, PC1_SCREEN, 24))
        self.assertEqual(tray._edge_rearm.held_edges(), ["north"])
        now = time.monotonic()
        # Resting there, and nudging up to the edge, stays held.
        tray._edge_rearm.update(x, 24, PC1_SCREEN, 40, 400, now + 2.0)
        tray._edge_rearm.update(x, 1, PC1_SCREEN, 40, 400, now + 2.1)
        self.assertTrue(tray._edge_held_back("north", "switch"))
        tray._edge_rearm.update(x, 60, PC1_SCREEN, 40, 400, now + 2.2)
        self.assertFalse(tray._edge_held_back("north", "switch"))

    def test_pc2_entry_over_south_does_not_bounce_back(self):
        tray = self.tray
        tray.istate.config = self.pc2_config()
        self.screen = dict(PC2_SCREEN)
        msg = {"type": "edge_enter", "session_id": "s2", "source_identity": f"device:{PC1_ID}",
               "target_identity": f"device:{PC2_ID}", "source_exit_edge": "north",
               "target_entry_edge": "south", "orthogonal_position": 0.5, "source_display_name": "PC1"}
        self.assertTrue(tray._enter_target_edge_session_impl(msg))
        self.assertEqual(self.cursor[1], 1079 - 24)
        self.assertEqual(tray._edge_rearm.held_edges(), ["south"])
        now = time.monotonic()
        # Mouse pushed straight back down: the return is held, logged once.
        tray._edge_rearm.update(960, 1079, PC2_SCREEN, 40, 400, now + 1.0)
        self.assertTrue(tray._edge_held_back("south", "return"))
        self.assertTrue(tray._edge_held_back("south", "return"))
        lines = self.held_back_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("edge return south held back: the mouse came back from PC1", lines[0])
        # In by the distance once, then down again: it returns.
        tray._edge_rearm.update(960, 1079 - 40, PC2_SCREEN, 40, 400, now + 1.1)
        self.assertFalse(tray._edge_held_back("south", "return"))

    def test_pc2_after_pc1s_hotkey_return_does_not_switch_south_by_itself(self):
        tray = self.tray
        tray.istate.config = self.pc2_config()
        self.screen = dict(PC2_SCREEN)
        tray.istate.edge_session = {"session_id": "s3", "role": "target",
                                    "source_identity": f"device:{PC1_ID}",
                                    "target_identity": f"device:{PC2_ID}",
                                    "source_exit_edge": "north", "target_entry_edge": "south",
                                    "source_display_name": "PC1", "active": True}
        self.cursor[:] = [700, 1078]       # PC 2's pointer left resting on its South edge
        tray._handle_edge_cancel({"type": "edge_cancel", "session_id": "s3", "reason": "hotkey"})
        self.assertIsNone(tray.istate.edge_session)
        self.assertEqual(self.moves, [])   # nobody sits at PC 2: no jump there
        self.assertEqual(tray._edge_rearm.held_edges(), ["south"])
        # Hours later, nobody touched PC 2's mouse: still held.
        tray._edge_rearm.update(700, 1078, PC2_SCREEN, 40, 400, time.monotonic() + 3600)
        self.assertTrue(tray._edge_held_back("south", "switch"))

    def test_pc2_lost_link_holds_its_entry_edge(self):
        tray = self.tray
        tray.istate.config = self.pc2_config()
        self.screen = dict(PC2_SCREEN)
        tray.istate.edge_session = {"session_id": "s4", "role": "target",
                                    "source_identity": f"device:{PC1_ID}",
                                    "target_identity": f"device:{PC2_ID}",
                                    "source_exit_edge": "north", "target_entry_edge": "south",
                                    "source_display_name": "PC1", "active": True}
        self.cursor[:] = [700, 1079]
        self.assertTrue(tray._cancel_active_edge_session("peer-disconnected", notify_remote=False))
        self.assertEqual(tray._edge_rearm.held_edges(), ["south"])


if __name__ == "__main__":
    unittest.main()
