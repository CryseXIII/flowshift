"""Runtime tests for command wheel action execution and overlay openers in tray.py.

The productive ``tray`` module is imported with a temporary data directory; the
overlay controller and Win32 foreground calls are replaced by recording fakes so
the tests assert the concrete effects: overlay hidden, foreground restored,
exact key events in ``inject_queue``, payload shapes without private paths.
"""
from __future__ import annotations

import os
import queue
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import overlay_actions as oa


class _FakeController:
    def __init__(self):
        self.calls = []

    def request_overlay(self, mode, target, cursor_position, payload):
        self.calls.append(("request", mode, target, cursor_position, payload))
        return {"ok": True, "supported": True, "queued": True}

    def show(self, mode, target, x, y, payload):
        self.calls.append(("show", mode, target, (x, y), payload))
        return {"type": "overlay_visible", "payload": {"mode": mode}}

    def hide(self):
        self.calls.append(("hide",))
        return {"type": "overlay_hidden", "payload": {}}

    def snapshot(self):
        return {"visible": False}


@unittest.skipUnless(sys.platform == "win32", "tray.py imports Windows ctypes APIs")
class TrayOverlayActionTests(unittest.TestCase):
    tray = None

    @classmethod
    def setUpClass(cls):
        cls.class_tmp = tempfile.TemporaryDirectory(prefix="fs-tray-actions-", ignore_cleanup_errors=True)
        cls.old_env = {k: os.environ.get(k) for k in ("FLOWSHIFT_LOG_DIR", "FLOWSHIFT_CONFIG")}
        os.environ["FLOWSHIFT_LOG_DIR"] = cls.class_tmp.name
        os.environ["FLOWSHIFT_CONFIG"] = os.path.join(cls.class_tmp.name, "config.json")
        import tray as productive_tray
        cls.tray = productive_tray

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
        self.saved = {
            "controller": tray._overlay_controller,
            "config": tray.istate.config,
            "peers": tray.istate.peers,
            "active": (tray.istate.active, tray.istate.active_peer),
            "cursor": tray._current_cursor_pos,
            "clip_mgr": tray._clip_mgr,
            "prev": tray._overlay_prev_foreground,
        }
        self.controller = _FakeController()
        tray._overlay_controller = self.controller
        tray._current_cursor_pos = lambda: (640, 360)
        tray.istate.peers = {}
        tray.istate.active = False
        tray.istate.active_peer = None
        tray.istate.config = dict(self.saved["config"])
        tray.istate.config["peers"] = [
            {"name": "Alpha", "host": "10.0.0.2", "port": 45781, "device_id": "aaaa0001"},
            {"name": "Beta", "host": "10.0.0.3", "port": 45781, "device_id": "bbbb0002"},
        ]
        tray.istate.config.pop("command_wheel", None)
        self.drain_inject_queue()
        # Foreground fakes: a fixed "previous" window handle that is always valid.
        self.foreground = {"current": 4242, "set": []}
        self.orig = (tray.user32.GetForegroundWindow, tray.user32.SetForegroundWindow, tray.user32.IsWindow)
        tray.user32.GetForegroundWindow = lambda: self.foreground["current"]
        tray.user32.SetForegroundWindow = lambda h: self.foreground["set"].append(h.value if hasattr(h, "value") else h) or 1
        tray.user32.IsWindow = lambda h: 1
        with tray._overlay_prev_lock:
            tray._overlay_prev_foreground = None

    def tearDown(self):
        tray = self.tray
        (tray.user32.GetForegroundWindow, tray.user32.SetForegroundWindow, tray.user32.IsWindow) = self.orig
        tray._overlay_controller = self.saved["controller"]
        tray.istate.config = self.saved["config"]
        tray.istate.peers = self.saved["peers"]
        tray.istate.active, tray.istate.active_peer = self.saved["active"]
        tray._current_cursor_pos = self.saved["cursor"]
        tray._clip_mgr = self.saved["clip_mgr"]
        with tray._overlay_prev_lock:
            tray._overlay_prev_foreground = self.saved["prev"]
        self.drain_inject_queue()

    def drain_inject_queue(self):
        drained = []
        while True:
            try:
                drained.append(self.tray.istate.inject_queue.get_nowait())
            except queue.Empty:
                return drained

    # ── payloads and openers ────────────────────────────────────────
    def test_clipboard_overlay_payload_lists_profiles_without_paths(self):
        payload = self.tray.clipboard_overlay_payload()
        self.assertEqual(payload["profile"], "device:aaaa0001")
        self.assertEqual([p["identity"] for p in payload["profiles"]],
                         ["device:aaaa0001", "device:bbbb0002"])
        self.assertEqual(payload["profiles"][0]["label"], "Alpha")
        self.assertFalse(payload["profiles"][0]["connected"])
        self.assertEqual(set(payload), {"profile", "profiles"})
        self.tray.istate.active = True
        self.tray.istate.active_peer = "device:bbbb0002"
        self.assertEqual(self.tray.clipboard_overlay_payload()["profile"], "device:bbbb0002")

    def test_hotkey_openers_enqueue_the_right_mode_and_remember_foreground(self):
        tray = self.tray
        result = tray.show_clipboard_overlay()
        self.assertEqual(result, {"ok": True, "supported": True, "queued": True})
        kind, mode, target, cursor, payload = self.controller.calls[-1]
        self.assertEqual((kind, mode, target, cursor), ("request", "clipboard", {"kind": "local", "identity": "local"}, (640, 360)))
        self.assertEqual(payload["profile"], "device:aaaa0001")
        self.assertEqual(tray._overlay_prev_foreground, 4242)

        self.foreground["current"] = 777
        tray.show_command_wheel()
        kind, mode, _target, _cursor, payload = self.controller.calls[-1]
        self.assertEqual((kind, mode), ("request", "command_wheel"))
        self.assertEqual(payload["pages"], [list(p) for p in oa.DEFAULT_PAGES])
        self.assertIn("copy", payload["actions"])
        self.assertEqual(payload["hotkey"], "Ctrl+Alt+Space")
        self.assertEqual(tray._overlay_prev_foreground, 777)

    # ── execution ───────────────────────────────────────────────────
    def test_key_action_hides_overlay_restores_foreground_then_injects(self):
        tray = self.tray
        tray.show_command_wheel()
        self.controller.calls.clear()
        result = tray.execute_action(oa.get_action("copy"), {"source": "command_wheel"})
        self.assertEqual(result, {"ok": True, "reason": None})
        self.assertEqual(self.controller.calls, [("hide",)])
        self.assertEqual(self.foreground["set"], [4242])
        self.assertEqual(self.drain_inject_queue(), [
            {"type": "key", "code": 0x11}, {"type": "key", "code": 0x43},
            {"type": "key_up", "code": 0x43}, {"type": "key_up", "code": 0x11},
        ])

    def test_key_action_without_known_foreground_still_injects_but_reports_it(self):
        tray = self.tray
        result = tray.execute_action(oa.get_action("delete"))
        self.assertEqual(result, {"ok": True, "reason": "foreground_not_restored"})
        self.assertEqual(self.foreground["set"], [])
        self.assertEqual(self.drain_inject_queue(),
                         [{"type": "key", "code": 0x2E}, {"type": "key_up", "code": 0x2E}])

    def test_open_clipboard_action_switches_to_the_clipboard_overlay(self):
        tray = self.tray
        result = tray.execute_action(oa.get_action("open_clipboard"))
        self.assertEqual(result, {"ok": True, "reason": None})
        self.assertEqual(self.controller.calls[-1][:2], ("request", "clipboard"))
        self.assertEqual(self.drain_inject_queue(), [])

    def test_clipboard_sync_action_sends_manifest_for_the_profile(self):
        tray = self.tray
        sent = []

        class FakeManager:
            def send_manifest(self, ident):
                sent.append(ident)

        tray._clip_mgr = FakeManager()
        result = tray.execute_action(oa.get_action("clipboard_sync"))
        self.assertEqual(result, {"ok": True, "reason": None})
        self.assertEqual(sent, ["device:aaaa0001"])
        self.assertEqual(self.controller.calls, [("hide",)])
        tray.istate.config["peers"] = []
        self.assertEqual(tray.execute_action(oa.get_action("clipboard_sync")),
                         {"ok": False, "reason": "no_profile"})

    def test_invalid_actions_are_refused_without_side_effects(self):
        tray = self.tray
        self.assertEqual(tray.execute_action({"id": "x", "kind": "shell", "cmd": "rm"}),
                         {"ok": False, "reason": "unknown_kind"})
        self.assertEqual(tray.execute_action({"id": "x", "kind": "runtime", "operation": "reboot"}),
                         {"ok": False, "reason": "unknown_operation"})
        self.assertEqual(tray.execute_action("copy"), {"ok": False, "reason": "invalid_action"})
        self.assertEqual(self.controller.calls, [])
        self.assertEqual(self.drain_inject_queue(), [])

    def test_wheel_hotkey_is_read_from_config_with_defaults(self):
        tray = self.tray
        self.assertEqual(oa.wheel_config_from(tray.istate.config)["hotkey"], oa.DEFAULT_HOTKEY)
        tray.istate.config["command_wheel"] = {"hotkey": {"mods": tray.MOD_CTRL, "vk": 0x57}}
        self.assertEqual(oa.wheel_config_from(tray.istate.config)["hotkey"], {"mods": tray.MOD_CTRL, "vk": 0x57})
        self.assertEqual(tray.tray_mods_to_rhk(tray.MOD_CTRL | tray.MOD_ALT), tray.RHK_CTRL | tray.RHK_ALT)


if __name__ == "__main__":
    unittest.main()
