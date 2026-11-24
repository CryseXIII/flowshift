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

    def hide(self, reason="runtime"):
        self.calls.append(("hide", reason))
        return {"type": "overlay_hidden", "payload": {}}

    def wheel_release(self, held_ms):
        self.calls.append(("wheel_release", held_ms))
        return dict(self.release_result)

    release_result = {"outcome": "keep_open", "action_id": None}

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
            "webgui": tray._webgui_health_snapshot,
        }
        tray._webgui_health_snapshot = lambda: {"ok": True, "overlay_ready": True}
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
        tray._webgui_health_snapshot = self.saved["webgui"]
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
        # Local history comes first so the overlay works with no peer active.
        self.assertEqual(payload["profile"], "local")
        self.assertEqual([p["identity"] for p in payload["profiles"]],
                         ["local", "device:aaaa0001", "device:bbbb0002"])
        self.assertEqual(payload["profiles"][0]["label"], "This PC")
        self.assertTrue(payload["profiles"][0]["connected"])
        self.assertEqual(payload["profiles"][1]["label"], "Alpha")
        self.assertFalse(payload["profiles"][1]["connected"])
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
        self.assertEqual(payload["profile"], "local")
        self.assertEqual(tray._overlay_prev_foreground, 4242)

        self.foreground["current"] = 777
        tray.show_command_wheel()
        kind, mode, _target, _cursor, payload = self.controller.calls[-1]
        self.assertEqual((kind, mode), ("request", "command_wheel"))
        self.assertEqual(payload["pages"], [list(p) for p in oa.DEFAULT_PAGES])
        self.assertIn("copy", payload["actions"])
        self.assertEqual(payload["hotkey"], "Ctrl+Alt+Space")
        self.assertEqual(tray._overlay_prev_foreground, 777)

    def test_missing_overlay_html_never_starts_the_host_and_notifies(self):
        tray = self.tray
        report = {"ok": False, "state": "incomplete", "overlay_ready": False,
                  "missing_files": ["overlay.html"], "webgui_version": None,
                  "app_version": "0.7.0-dev.23",
                  "message": "WebGUI incomplete (missing: overlay.html)",
                  "action": "Run install_flowshift.bat again"}
        tray._webgui_health_snapshot = lambda: report
        notices = []
        saved_notify = tray.notify_user
        tray.notify_user = lambda title, text, key=None, interval=0: notices.append((title, text, key)) or True
        try:
            for _ in range(3):
                result = tray.show_command_wheel()
                self.assertEqual(result["ok"], False)
                self.assertIn("overlay.html", result["reason"])
            tray.show_clipboard_overlay()
        finally:
            tray.notify_user = saved_notify
        self.assertEqual(self.controller.calls, [])
        self.assertEqual(len(notices), 4)
        self.assertIn("Run install_flowshift.bat again", notices[0][1])
        self.assertEqual(notices[0][2], "webgui:incomplete")

    # ── execution ───────────────────────────────────────────────────
    def test_key_action_hides_overlay_restores_foreground_then_injects(self):
        tray = self.tray
        tray.show_command_wheel()
        self.controller.calls.clear()
        result = tray.execute_action(oa.get_action("copy"), {"source": "command_wheel"})
        self.assertEqual(result, {"ok": True, "reason": None})
        self.assertEqual(self.controller.calls, [("hide", "action")])
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
        # No peer active: sync on the local history is an ok-noop.
        self.assertEqual(result, {"ok": True, "reason": None})
        self.assertEqual(sent, [])
        self.assertEqual(self.controller.calls, [("hide", "action")])
        tray.istate.active = True
        tray.istate.active_peer = "device:bbbb0002"
        self.assertEqual(tray.execute_action(oa.get_action("clipboard_sync")),
                         {"ok": True, "reason": None})
        self.assertEqual(sent, ["device:bbbb0002"])

    def test_invalid_actions_are_refused_without_side_effects(self):
        tray = self.tray
        self.assertEqual(tray.execute_action({"id": "x", "kind": "shell", "cmd": "rm"}),
                         {"ok": False, "reason": "unknown_kind"})
        self.assertEqual(tray.execute_action({"id": "x", "kind": "runtime", "operation": "reboot"}),
                         {"ok": False, "reason": "unknown_operation"})
        self.assertEqual(tray.execute_action("copy"), {"ok": False, "reason": "invalid_action"})
        self.assertEqual(self.controller.calls, [])
        self.assertEqual(self.drain_inject_queue(), [])

    def test_unknown_channel_is_refused_and_no_keys_are_injected(self):
        tray = self.tray
        for channel in (None, "remote", "body"):
            with self.subTest(channel=channel):
                self.assertEqual(tray.execute_action(oa.get_action("copy"), {}, channel=channel),
                                 {"ok": False, "reason": "invalid_channel"})
        self.assertEqual(self.controller.calls, [])
        self.assertEqual(self.drain_inject_queue(), [])
        result = tray.execute_action(oa.get_action("delete"), {}, channel="wheel_swipe")
        self.assertEqual(result, {"ok": True, "reason": "foreground_not_restored"})
        self.assertEqual(self.drain_inject_queue(),
                         [{"type": "key", "code": 0x2E}, {"type": "key_up", "code": 0x2E}])

    def test_productive_handler_table_has_a_class_per_kind(self):
        registry = self.tray._action_handlers
        self.assertEqual({k: registry.risk_class(k) for k in registry.kinds()},
                         {"keys": "L1", "runtime": "L0"})

    def test_wheel_hotkey_is_read_from_config_with_defaults(self):
        tray = self.tray
        self.assertEqual(oa.wheel_config_from(tray.istate.config)["hotkey"], oa.DEFAULT_HOTKEY)
        tray.istate.config["command_wheel"] = {"version": 2,
                                               "hotkey": {"mods": tray.MOD_CTRL, "vk": 0x57}}
        try:
            self.assertEqual(oa.wheel_config_from(tray.istate.config)["hotkey"],
                             {"mods": tray.MOD_CTRL, "vk": 0x57})
        finally:
            tray.istate.config.pop("command_wheel", None)
        self.assertEqual(tray.tray_mods_to_rhk(tray.MOD_CTRL | tray.MOD_ALT), tray.RHK_CTRL | tray.RHK_ALT)

    def _register(self, config_patch, hook_installed=True):
        """register_runtime_hotkeys with fake RegisterHotKey; returns (registered, report)."""
        tray = self.tray
        registered = []
        orig = (tray.user32.RegisterHotKey, tray.user32.UnregisterHotKey, tray._wheel_hook.installed)
        tray.user32.RegisterHotKey = lambda hwnd, hid, mods, vk: registered.append((hid, mods, vk)) or 1
        tray.user32.UnregisterHotKey = lambda hwnd, hid: 1
        tray._wheel_hook.installed = hook_installed
        saved = (tray.istate.hotkeys, dict(tray.istate.config))
        tray.istate.config.update(config_patch)
        tray.istate.hotkeys = tray.load_hotkeys(tray.istate.config) if "hotkeys" in config_patch else []
        try:
            tray.register_runtime_hotkeys(1234)
            report = tray.hotkey_status_snapshot()
        finally:
            (tray.user32.RegisterHotKey, tray.user32.UnregisterHotKey, tray._wheel_hook.installed) = orig
            tray.istate.hotkeys = saved[0]
            tray.istate.config.clear()
            tray.istate.config.update(saved[1])
        return {hid: (mods, vk) for hid, mods, vk in registered}, report

    def test_clipboard_hotkeys_register_ctrl_alt_v_and_ctrl_win_v(self):
        tray = self.tray
        registered = []
        orig = (tray.user32.RegisterHotKey, tray.user32.UnregisterHotKey)
        tray.user32.RegisterHotKey = lambda hwnd, hid, mods, vk: registered.append((hid, mods, vk)) or 1
        tray.user32.UnregisterHotKey = lambda hwnd, hid: 1
        saved_hotkeys = tray.istate.hotkeys
        tray.istate.hotkeys = []
        tray.istate.config["clipboard"] = {"enabled": True, "intercept_win_v": False}
        try:
            tray.register_runtime_hotkeys(1234)
        finally:
            tray.user32.RegisterHotKey, tray.user32.UnregisterHotKey = orig
            tray.istate.hotkeys = saved_hotkeys
            tray.unregister_runtime_hotkeys(1234)
        by_id = {hid: (mods, vk) for hid, mods, vk in registered}
        self.assertEqual(by_id[tray.ID_HK_CLIP_ALT], (tray.RHK_CTRL | tray.RHK_ALT, 0x56))
        self.assertEqual(by_id[tray.ID_HK_CLIP_CWV], (tray.RHK_CTRL | tray.RHK_WIN, 0x56))
        self.assertNotIn(tray.ID_HK_CLIP_WINV, by_id, "Win+V stays opt-in")
        # The default wheel hotkey is Ctrl+right click: a mouse-hook binding,
        # nothing is registered at Windows for it.
        self.assertNotIn(tray.ID_HK_WHEEL, by_id)
        self.assertEqual(tray._mouse_triggers.bindings(),
                         {(tray.MOD_CTRL, "right", "local"): ("wheel", "command_wheel")})
        self.assertEqual(len({tray.ID_HK_CLIP_ALT, tray.ID_HK_CLIP_CWV, tray.ID_HK_CLIP_WINV,
                              tray.ID_HK_WHEEL, tray.ID_HK_KILL} | set(tray.ID_HK_WHEEL_EXTRA)), 8)

    def test_keyboard_and_mouse_wheel_hotkeys_register_on_their_path(self):
        tray = self.tray
        by_id, report = self._register({"command_wheel": {
            "version": 2, "hotkey": {"mods": tray.MOD_CTRL, "mouse": "x1"},
            "extra_hotkeys": [{"mods": tray.MOD_CTRL | tray.MOD_ALT, "vk": 0x20}]}})
        # Keyboard extra hotkey: RegisterHotKey without autorepeat (hold-and-swipe).
        self.assertEqual(by_id[tray.ID_HK_WHEEL_EXTRA[0]],
                         (tray.RHK_CTRL | tray.RHK_ALT | tray.RHK_NOREPEAT, 0x20))
        self.assertEqual(tray._wheel_key_vks, {tray.ID_HK_WHEEL_EXTRA[0]: 0x20})
        self.assertEqual(tray._mouse_triggers.bindings(),
                         {(tray.MOD_CTRL, "x1", "local"): ("wheel", "command_wheel")})
        by_slot = {e["slot"]: e for e in report["entries"]}
        self.assertEqual(by_slot["command_wheel"]["hotkey"], "Ctrl+Side button 1")
        self.assertTrue(by_slot["command_wheel"]["active"])
        self.assertTrue(by_slot["command_wheel_extra_1"]["active"])

    def test_cleared_hotkeys_are_not_registered_and_not_reported(self):
        tray = self.tray
        by_id, report = self._register({
            "command_wheel": {"version": 2, "hotkey": None},
            "clipboard": {"enabled": True, "overlay_hotkey": None}})
        self.assertNotIn(tray.ID_HK_CLIP_ALT, by_id)
        self.assertNotIn(tray.ID_HK_WHEEL, by_id)
        slots = [e["slot"] for e in report["entries"]]
        self.assertNotIn("command_wheel", slots)
        self.assertNotIn("clipboard_overlay", slots)
        self.assertEqual(tray._mouse_triggers.bindings(), {})

    def test_mouse_hotkeys_without_hook_are_reported_with_reason(self):
        _by_id, report = self._register({}, hook_installed=False)
        wheel = {e["slot"]: e for e in report["entries"]}["command_wheel"]
        self.assertFalse(wheel["active"])
        self.assertIn("mouse hook of FlowShift is not running", wheel["message"])

    def test_peer_mouse_hotkeys_bind_return_while_forwarding(self):
        tray = self.tray
        _by_id, _report = self._register({"hotkeys": [
            {"action": "return_local", "mods": tray.MOD_CTRL, "key": 0, "mouse": "x2",
             "label": "Return to local"}]})
        self.assertEqual(tray._mouse_triggers.bindings()[(tray.MOD_CTRL, "x2", "forwarding")],
                         ("return", "return_local"))
        self.assertEqual(tray._peer_hotkey_state[0], {"active": True, "error": None})

    def test_busy_hotkeys_are_reported_with_alternatives(self):
        # Viktor's laptop: Ctrl+Win+V, Win+V and Ctrl+Alt+Space fail with 1409.
        tray = self.tray
        busy = {tray.ID_HK_CLIP_CWV, tray.ID_HK_CLIP_WINV, tray.ID_HK_WHEEL_EXTRA[0]}
        notes = []
        orig = (tray.user32.RegisterHotKey, tray.user32.UnregisterHotKey,
                tray.kernel32.GetLastError, tray.show_tray_notification, tray._wheel_hook.installed)
        tray.user32.RegisterHotKey = lambda hwnd, hid, mods, vk: 0 if hid in busy else 1
        tray.user32.UnregisterHotKey = lambda hwnd, hid: 1
        tray.kernel32.GetLastError = lambda: 1409
        tray.show_tray_notification = lambda title, text: notes.append(text) or True
        tray._wheel_hook.installed = True
        saved_hotkeys = tray.istate.hotkeys
        tray.istate.hotkeys = []
        tray.istate.config["clipboard"] = {"enabled": True, "intercept_win_v": True}
        tray.istate.config["command_wheel"] = {
            "version": 2, "extra_hotkeys": [{"mods": tray.MOD_CTRL | tray.MOD_ALT, "vk": 0x20}]}
        try:
            tray.register_runtime_hotkeys(1234)
            tray.register_runtime_hotkeys(1234)  # same conflicts: no second balloon
        finally:
            (tray.user32.RegisterHotKey, tray.user32.UnregisterHotKey,
             tray.kernel32.GetLastError, tray.show_tray_notification, tray._wheel_hook.installed) = orig
            tray.istate.hotkeys = saved_hotkeys
            tray.istate.config.pop("command_wheel", None)
            tray.unregister_runtime_hotkeys(1234)
        report = tray.hotkey_status_snapshot()
        self.assertFalse(report["ok"])
        self.assertEqual(report["conflicts"], ["clipboard_ctrl_win_v", "clipboard_win_v",
                                               "command_wheel_extra_1"])
        by_slot = {e["slot"]: e for e in report["entries"]}
        self.assertTrue(by_slot["clipboard_overlay"]["active"])
        # The main hotkey Ctrl+right click works (mouse hook) and is named instead.
        self.assertTrue(by_slot["command_wheel"]["active"])
        self.assertIn("Ctrl+Alt+V", by_slot["clipboard_ctrl_win_v"]["alternatives"])
        self.assertIn("Ctrl+Right click", by_slot["command_wheel_extra_1"]["alternatives"])
        self.assertEqual(len(notes), 1)
        self.assertIn("Ctrl+Win+V", notes[0])

    def test_mouse_messages_map_to_buttons(self):
        tray = self.tray
        ev = tray.mouse_trigger_event
        self.assertEqual(ev(tray.WM_RBUTTONDOWN, 0), ("down", "right"))
        self.assertEqual(ev(tray.WM_LBUTTONUP, 0), ("up", "left"))
        self.assertEqual(ev(tray.WM_MBUTTONDOWN, 0), ("down", "middle"))
        self.assertEqual(ev(tray.WM_XBUTTONDOWN, 1 << 16), ("down", "x1"))
        self.assertEqual(ev(tray.WM_XBUTTONUP, 2 << 16), ("up", "x2"))
        self.assertEqual(ev(tray.WM_XBUTTONDOWN, 3 << 16), (None, None))
        self.assertEqual(ev(tray.WM_MOUSEWHEEL, 120 << 16), ("wheel", "wheel_up"))
        self.assertEqual(ev(tray.WM_MOUSEWHEEL, (0x10000 - 120) << 16), ("wheel", "wheel_down"))
        self.assertEqual(ev(tray.WM_MOUSEMOVE, 0), (None, None))

    def test_mouse_hook_opens_the_wheel_and_swallows_press_and_release(self):
        tray = self.tray
        calls = []
        orig = (tray.async_mods, tray.begin_wheel_hold, tray.end_wheel_hold)
        tray._mouse_triggers.set_bindings([(dict(oa.DEFAULT_HOTKEY), ("wheel", "command_wheel"), "local")])
        tray.begin_wheel_hold = lambda source: calls.append(("begin", source)) or True
        tray.end_wheel_hold = lambda source: calls.append(("end", source)) or True
        try:
            tray.async_mods = lambda: 0
            self.assertEqual(tray.mouse_trigger_decide(tray.WM_RBUTTONDOWN, 0, False, False)[0], "pass")
            tray.async_mods = lambda: tray.MOD_CTRL
            for msg in (tray.WM_RBUTTONDOWN, tray.WM_RBUTTONUP):
                decision, target, button = tray.mouse_trigger_decide(msg, 0, False, False)
                tray.run_mouse_trigger(decision, target, button)
            # Injected (our own SendInput) and forwarding: never taken.
            self.assertEqual(tray.mouse_trigger_decide(tray.WM_RBUTTONDOWN, 0, True, False)[0], "pass")
            self.assertEqual(tray.mouse_trigger_decide(tray.WM_RBUTTONDOWN, 0, False, True)[0], "pass")
        finally:
            tray.async_mods, tray.begin_wheel_hold, tray.end_wheel_hold = orig
            tray._mouse_triggers.set_bindings([])
        self.assertEqual(calls, [("begin", "mouse_right"), ("end", "mouse_right")])

    def test_hold_and_swipe_runs_the_selected_action(self):
        tray = self.tray
        executed = []
        channels = []
        orig = (tray.execute_action, tray._wait_wheel_modifiers_released)
        tray.execute_action = lambda action, context=None, channel=None: channels.append(
            channel) or executed.append((action["id"], context)) or {"ok": True, "reason": None}
        tray._wait_wheel_modifiers_released = lambda timeout=1.0: True
        tray._wheel_hold.cancel()
        try:
            self.assertTrue(tray.begin_wheel_hold("ctrl_right_click"))
            show = self.controller.calls[-1]
            self.assertEqual(show[:2], ("request", "command_wheel"))
            self.assertTrue(show[4]["hold"])
            self.assertEqual(show[4]["trigger"]["hold_ms"], 250)
            # A second press during the hold (autorepeat) does not reopen.
            self.assertFalse(tray.begin_wheel_hold("ctrl_right_click"))
            self.controller.release_result = {"outcome": "execute", "action_id": "paste"}
            tray._finish_wheel_hold(400, "ctrl_right_click")
            self.assertEqual(executed, [("paste", {"source": "wheel_swipe"})])
            self.assertEqual(channels, ["wheel_swipe"])
            self.controller.release_result = {"outcome": "close", "action_id": None}
            tray._finish_wheel_hold(400, "ctrl_right_click")
            self.assertEqual(len(executed), 1)
        finally:
            tray.execute_action, tray._wait_wheel_modifiers_released = orig
            tray._wheel_hold.cancel()
            self.controller.release_result = _FakeController.release_result

    def test_wheel_trigger_is_ignored_while_forwarding(self):
        tray = self.tray
        tray._wheel_hold.cancel()
        tray.istate.active = True
        before = len(self.controller.calls)
        self.assertFalse(tray.begin_wheel_hold("hotkey"))
        self.assertEqual(len(self.controller.calls), before)
        self.assertIsNone(tray._wheel_hold.active_source)

    def test_wheel_trigger_hook_thread_starts_and_stops(self):
        import time
        hook = self.tray.WheelTriggerHook()
        hook.start()
        thread = hook._thread
        try:
            self.assertTrue(hook.running)
        finally:
            hook.stop()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(hook.running)

    def test_tray_right_click_with_ctrl_opens_wheel_otherwise_menu(self):
        tray = self.tray
        menu_calls = []
        orig = (tray.ctrl_key_down, tray.show_menu, tray._handle_menu)
        tray.show_menu = lambda hwnd: menu_calls.append(hwnd) or 0
        tray._handle_menu = lambda cmd: None
        try:
            tray.ctrl_key_down = lambda: True
            self.assertEqual(tray.handle_tray_right_click(99), "command_wheel")
            self.assertEqual(self.controller.calls[-1][:2], ("request", "command_wheel"))
            self.assertEqual(menu_calls, [])
            tray.ctrl_key_down = lambda: False
            self.controller.calls.clear()
            self.assertEqual(tray.handle_tray_right_click(99), "menu")
            self.assertEqual(menu_calls, [99])
            self.assertEqual(self.controller.calls, [])
        finally:
            tray.ctrl_key_down, tray.show_menu, tray._handle_menu = orig

    def test_clipboard_tray_entry_opens_the_routed_webgui(self):
        tray = self.tray
        original_read = tray._read_webgui_url
        original_open = tray.open_webgui
        opened = []
        try:
            tray._read_webgui_url = lambda: "http://127.0.0.1:5123/"
            self.assertEqual(tray.webgui_route_url("clipboard"),
                             "http://127.0.0.1:5123/clipboard")
            tray.open_webgui = lambda route="": opened.append(route)
            tray._handle_menu(tray.ID_WEB_CLIPBOARD)
            self.assertEqual(opened, ["clipboard"])
        finally:
            tray._read_webgui_url = original_read
            tray.open_webgui = original_open


if __name__ == "__main__":
    unittest.main()
