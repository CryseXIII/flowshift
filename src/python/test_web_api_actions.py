"""Offline HTTP tests for the Command Wheel action routes."""
from __future__ import annotations

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config_schema
import overlay_actions as oa
import web_api
from runtime_model import MOD_ALT, MOD_CTRL, MOD_SHIFT


class FakeState:
    def __init__(self, config):
        self.lock = threading.RLock()
        self.config = config
        self.active_peer = None


class ActionApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary.name) / "config.json"
        config_schema.save_config(self.config_path, {"device_name": "kept", "peers": []})
        self.state = FakeState(config_schema.load_config(self.config_path))
        self.executed = []
        self.execute_result = {"ok": True, "reason": None}
        self.reloads = 0

        def save_config(config):
            return config_schema.save_config(self.config_path, config)

        def execute(action, context):
            self.executed.append((action["id"], context))
            return self.execute_result

        def reload_hotkeys():
            self.reloads += 1

        web_api._refs = {}
        web_api.init(istate=self.state, save_config=save_config,
                     execute_action=execute, reload_hotkeys=reload_hotkeys)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        web_api._refs = {}
        self.temporary.cleanup()

    def request(self, method, path, body=None, raw=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        payload = raw if raw is not None else (json.dumps(body) if body is not None else None)
        # Like the WebGUI's post(): own origin and JSON type on every POST.
        headers = {"Content-Type": "application/json",
                   "Origin": f"http://127.0.0.1:{self.server.server_address[1]}"}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def stored_wheel(self):
        return json.loads(self.config_path.read_text("utf-8")).get("command_wheel")

    def test_get_actions_returns_registry_and_default_wheel(self):
        status, body = self.request("GET", "/api/actions")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual([a["id"] for a in body["actions"]], list(oa.action_ids()))
        self.assertEqual(body["wheel"]["pages"], [list(p) for p in oa.DEFAULT_PAGES])
        # Viktor 24.11.: the one visible main hotkey is Ctrl+right click.
        self.assertEqual(body["wheel"]["hotkey"], {"mods": MOD_CTRL, "mouse": "right",
                                                   "display": "Ctrl+Right click"})
        self.assertEqual(body["wheel"]["extra_hotkeys"], [])
        self.assertEqual(body["limits"], {"max_slots_per_page": 8, "max_pages": 16})
        for action in body["actions"]:
            self.assertNotIn("sequence", action)

    def test_save_wheel_persists_and_reloads_hotkeys(self):
        status, body = self.request("POST", "/api/actions/wheel", {
            "pages": [["cut", "", "paste"], [], ["delete"]],
            "hotkey": {"mods": MOD_CTRL | MOD_SHIFT, "vk": 0x57},
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["wheel"]["pages"], [["cut", "paste"], ["delete"]])
        self.assertEqual(body["wheel"]["hotkey"]["display"], "Ctrl+Shift+W")
        self.assertEqual(self.stored_wheel(),
                         {"version": 2, "pages": [["cut", "paste"], ["delete"]],
                          "hotkey": {"mods": MOD_CTRL | MOD_SHIFT, "vk": 0x57},
                          "extra_hotkeys": [],
                          "trigger": dict(oa.DEFAULT_TRIGGER)})
        self.assertEqual(self.state.config["command_wheel"]["pages"], [["cut", "paste"], ["delete"]])
        self.assertEqual(self.reloads, 1)
        status, body = self.request("GET", "/api/actions")
        self.assertEqual(body["wheel"]["pages"], [["cut", "paste"], ["delete"]])

    def test_trigger_settings_are_saved_partially_and_reload_hotkeys(self):
        status, body = self.request("GET", "/api/actions")
        self.assertEqual(body["wheel"]["trigger"], dict(oa.DEFAULT_TRIGGER))
        self.assertEqual(body["wheel"]["trigger_limits"],
                         {"hold_ms_min": 100, "hold_ms_max": 2000, "max_extra_hotkeys": 3,
                          "mouse_buttons": ["left", "right", "middle", "x1", "x2",
                                            "wheel_up", "wheel_down"]})
        status, body = self.request("POST", "/api/actions/wheel", {"trigger": {"hold_ms": 400}})
        self.assertEqual(status, 200, body)
        status, body = self.request("POST", "/api/actions/wheel", {"trigger": {"tap_open": False}})
        self.assertEqual(status, 200, body)
        expected = {"tap_open": False, "hold_swipe": True, "hold_ms": 400}
        self.assertEqual(body["wheel"]["trigger"], expected)
        self.assertEqual(self.stored_wheel()["trigger"], expected)
        self.assertEqual(self.state.config["command_wheel"]["trigger"], expected)
        self.assertEqual(self.reloads, 2)
        # Switching off the last way to use the wheel is refused, nothing changes.
        status, body = self.request("POST", "/api/actions/wheel", {"trigger": {"hold_swipe": False}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "trigger_disabled")
        self.assertEqual(self.stored_wheel()["trigger"], expected)

    def test_save_pages_only_keeps_existing_hotkey(self):
        self.request("POST", "/api/actions/wheel", {"hotkey": {"mods": MOD_ALT, "vk": 0x51}})
        status, body = self.request("POST", "/api/actions/wheel", {"pages": [["copy"]]})
        self.assertEqual(status, 200)
        self.assertEqual(body["wheel"]["hotkey"]["display"], "Alt+Q")
        self.assertEqual(self.stored_wheel()["hotkey"], {"mods": MOD_ALT, "vk": 0x51})

    def test_mouse_hotkey_can_be_set_cleared_and_set_back(self):
        # Viktor's check: Ctrl+side button instead of Ctrl+right click, then back.
        status, body = self.request("POST", "/api/actions/wheel",
                                    {"hotkey": {"mods": MOD_CTRL, "mouse": "x1"}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["wheel"]["hotkey"]["display"], "Ctrl+Side button 1")
        self.assertEqual(self.stored_wheel()["hotkey"], {"mods": MOD_CTRL, "mouse": "x1"})
        # Cleared on purpose: stays empty (no default comes back).
        status, body = self.request("POST", "/api/actions/wheel", {"hotkey": None})
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["wheel"]["hotkey"])
        self.assertIsNone(self.stored_wheel()["hotkey"])
        status, body = self.request("GET", "/api/actions")
        self.assertIsNone(body["wheel"]["hotkey"])
        # Back to Ctrl+right click, plus a keyboard hotkey as extra trigger.
        status, body = self.request("POST", "/api/actions/wheel", {
            "hotkey": {"mods": MOD_CTRL, "mouse": "right"},
            "extra_hotkeys": [{"mods": MOD_CTRL | MOD_ALT, "vk": 0x20}]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["wheel"]["hotkey"]["display"], "Ctrl+Right click")
        self.assertEqual([hk["display"] for hk in body["wheel"]["extra_hotkeys"]], ["Ctrl+Alt+Space"])
        self.assertEqual(self.reloads, 3)

    def test_old_wheel_block_is_read_as_migrated(self):
        self.state.config["command_wheel"] = {"hotkey": {"mods": MOD_CTRL | MOD_ALT, "vk": 0x20},
                                              "trigger": {"mouse_button": "x2", "hold_ms": 300}}
        status, body = self.request("GET", "/api/actions")
        self.assertEqual(body["wheel"]["hotkey"]["display"], "Ctrl+Right click")
        self.assertEqual([hk["display"] for hk in body["wheel"]["extra_hotkeys"]], ["Side button 2"])
        self.assertEqual(body["wheel"]["trigger"]["hold_ms"], 300)
        # Saving only the trigger keeps the migrated hotkeys.
        status, body = self.request("POST", "/api/actions/wheel", {"trigger": {"hold_ms": 350}})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.stored_wheel()["extra_hotkeys"], [{"mods": 0, "mouse": "x2"}])

    def test_invalid_wheel_is_rejected_and_not_stored(self):
        cases = (
            ({"pages": [["copy", "bogus"]]}, "unknown_action"),
            ({"pages": [["copy"] * 9]}, "too_many_slots"),
            ({"pages": [[]]}, "empty_wheel"),
            ({"hotkey": {"mods": 0, "vk": 0x57}}, "invalid_hotkey"),
            ({"trigger": {"hold_ms": 20}}, "invalid_trigger"),
            ({"trigger": {"mouse_button": "x2"}}, "invalid_trigger"),
            ({"hotkey": {"mods": 0, "mouse": "left"}}, "mouse_needs_modifier"),
            ({"hotkey": {"mods": 0, "mouse": "right"}}, "mouse_needs_modifier"),
            ({"hotkey": {"mods": MOD_CTRL, "mouse": "x9"}}, "invalid_hotkey"),
            ({"extra_hotkeys": [{"mods": MOD_CTRL, "mouse": "right"}]}, "hotkey_conflict"),
            ({"extra_hotkeys": [{"mods": 0, "mouse": "x1"}] * 4}, "too_many_hotkeys"),
            ({"other": 1}, "invalid_request"),
            ({}, "invalid_request"),
        )
        for body, code in cases:
            with self.subTest(body=body):
                status, response = self.request("POST", "/api/actions/wheel", body)
                self.assertEqual(status, 400)
                self.assertEqual(response["error"], code)
        self.assertIsNone(self.stored_wheel())
        self.assertEqual(self.reloads, 0)
        status, response = self.request("POST", "/api/actions/wheel", raw="not json")
        self.assertEqual(status, 400)

    def test_execute_dispatches_validated_action(self):
        status, body = self.request("POST", "/api/actions/execute",
                                    {"action_id": "copy", "context": {"source": "command_wheel"}})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "action_id": "copy", "reason": None})
        self.assertEqual(self.executed, [("copy", {"source": "command_wheel"})])

    def test_execute_reports_runtime_refusal(self):
        self.execute_result = {"ok": False, "reason": "overlay_busy"}
        status, body = self.request("POST", "/api/actions/execute", {"action_id": "paste"})
        self.assertEqual(status, 409)
        self.assertEqual(body, {"ok": False, "action_id": "paste", "reason": "overlay_busy"})

    def test_execute_rejects_unknown_action_without_dispatch(self):
        status, body = self.request("POST", "/api/actions/execute", {"action_id": "format_disk"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "unknown_action")
        status, body = self.request("POST", "/api/actions/execute",
                                    {"action_id": "copy", "context": {"k": {"nested": True}}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_context")
        self.assertEqual(self.executed, [])

    def test_execute_body_cannot_set_channel_origin_or_confirmation(self):
        for key, value in (("channel", "wheel_swipe"), ("origin", "http://127.0.0.1:5000"),
                           ("confirm", True)):
            with self.subTest(key=key):
                status, body = self.request("POST", "/api/actions/execute",
                                            {"action_id": "copy", key: value})
                self.assertEqual((status, body["error"]), (400, "invalid_request"))
        self.assertEqual(self.executed, [])

    def test_execute_without_runtime_returns_503(self):
        web_api._refs.pop("execute_action")
        status, body = self.request("POST", "/api/actions/execute", {"action_id": "copy"})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "not_available")

    def test_peers_route_lists_configured_peers_with_identities(self):
        # Regression: _normalize_runtime_peers used an unimported peer_identity.
        with self.state.lock:
            self.state.config["peers"] = [
                {"name": "Alpha", "host": "10.0.0.2", "port": 45781, "device_id": "aaaa0001"}]
        self.state.peers = {}
        status, body = self.request("GET", "/api/peers")
        self.assertEqual(status, 200, body)
        self.assertEqual([p["identity"] for p in body["peers"]], ["device:aaaa0001"])
        self.assertFalse(body["peers"][0]["connected"])

    def test_settings_save_normalizes_clipboard_and_preserves_top_level_config(self):
        with self.state.lock:
            self.state.config["installation_marker"] = "keep-me"
            self.state.config["clipboard"] = {"history_max_items": 40, "persist": True}
        status, body = self.request("POST", "/api/settings", {
            "device_name": "Updated",
            "history_max_items": 5000,
            "max_item_gb": "not-a-number",
            "persist": False,
            "rate_unit": "bogus",
        })
        self.assertEqual(status, 200, body)
        clip = body["config"]["clipboard"]
        self.assertEqual(clip["history_max_items"], 999)
        self.assertEqual(clip["max_item_gb"], 50.0)
        self.assertFalse(clip["persist"])
        self.assertEqual(clip["rate_unit"], "auto")
        self.assertEqual(body["config"]["device_name"], "Updated")
        self.assertEqual(body["config"]["installation_marker"], "keep-me")
        self.assertEqual(self.reloads, 1)
        stored = json.loads(self.config_path.read_text("utf-8"))
        self.assertEqual(stored["clipboard"]["history_max_items"], 999)

    def test_clipboard_overlay_hotkey_is_saved_and_conflicts_are_rejected(self):
        status, body = self.request("POST", "/api/settings",
                                    {"overlay_hotkey": {"mods": MOD_CTRL | MOD_SHIFT, "vk": 0x56}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["config"]["clipboard"]["overlay_hotkey"],
                         {"mods": MOD_CTRL | MOD_SHIFT, "vk": 0x56})
        self.assertEqual(self.reloads, 1)
        # The wheel default is Ctrl+right click: the same for the clipboard is refused
        # with both uses named.
        status, body = self.request("POST", "/api/settings",
                                    {"overlay_hotkey": {"mods": MOD_CTRL, "mouse": "right"}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "hotkey_conflict")
        self.assertIn("Ctrl+Right click is assigned twice: Command wheel and Clipboard overlay",
                      body["message"])
        status, body = self.request("POST", "/api/settings", {"overlay_hotkey": {"mods": 0, "vk": 0x56}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_hotkey")
        # And the wheel cannot take the clipboard overlay hotkey.
        status, body = self.request("POST", "/api/actions/wheel",
                                    {"hotkey": {"mods": MOD_CTRL | MOD_SHIFT, "vk": 0x56}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "hotkey_conflict")
        self.assertEqual(self.reloads, 1)
        stored = json.loads(self.config_path.read_text("utf-8"))
        self.assertEqual(stored["clipboard"]["overlay_hotkey"], {"mods": MOD_CTRL | MOD_SHIFT, "vk": 0x56})

    def test_clipboard_overlay_hotkey_can_be_a_mouse_button_or_cleared(self):
        status, body = self.request("POST", "/api/settings",
                                    {"overlay_hotkey": {"mods": MOD_CTRL, "mouse": "middle"}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["config"]["clipboard"]["overlay_hotkey"], {"mods": MOD_CTRL, "mouse": "middle"})
        status, body = self.request("POST", "/api/settings", {"overlay_hotkey": None})
        self.assertEqual(status, 200, body)
        stored = json.loads(self.config_path.read_text("utf-8"))
        self.assertIsNone(stored["clipboard"]["overlay_hotkey"])
        # Other settings saves keep it cleared.
        status, body = self.request("POST", "/api/settings", {"intercept_win_v": False})
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["config"]["clipboard"]["overlay_hotkey"])

    def test_peer_hotkeys_accept_mouse_buttons_and_reject_duplicates(self):
        rows = [{"action": "return_local", "mods": MOD_CTRL, "mouse": "x2", "label": "Return to local"},
                {"action": "forward_peer:device:b5", "mods": MOD_CTRL | MOD_ALT, "key": 0x31,
                 "label": "Forward to Surface"}]
        status, body = self.request("POST", "/api/settings", {"hotkeys": rows})
        self.assertEqual(status, 200, body)
        stored = json.loads(self.config_path.read_text("utf-8"))["hotkeys"]
        self.assertEqual(stored[0], {"action": "return_local", "label": "Return to local",
                                     "mods": MOD_CTRL, "key": 0, "mouse": "x2"})
        self.assertNotIn("mouse", stored[1])
        dup = [dict(rows[0]), dict(rows[0], label="Return again")]
        status, body = self.request("POST", "/api/settings", {"hotkeys": dup})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "hotkey_conflict")
        self.assertIn("Return to local and Return again", body["message"])
        status, body = self.request("POST", "/api/settings", {"hotkeys": [
            {"action": "return_local", "mods": 0, "mouse": "left", "label": "x"}]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "mouse_needs_modifier")
        # Nothing of the refused saves was stored.
        self.assertEqual(json.loads(self.config_path.read_text("utf-8"))["hotkeys"], stored)

    def test_settings_save_does_not_undo_a_wheel_save(self):
        self.request("POST", "/api/actions/wheel", {"hotkey": {"mods": MOD_CTRL, "mouse": "x2"}})
        status, page = self.request("GET", "/api/settings")
        self.assertEqual(page["command_wheel"]["hotkey"], {"mods": MOD_CTRL, "mouse": "x2"})
        self.request("POST", "/api/actions/wheel", {"hotkey": {"mods": MOD_CTRL, "mouse": "x1"}})
        # The Settings page posts back what it loaded before (old command_wheel included).
        status, body = self.request("POST", "/api/settings", dict(page, intercept_win_v=False))
        self.assertEqual(status, 200, body)
        self.assertEqual(self.stored_wheel()["hotkey"], {"mods": MOD_CTRL, "mouse": "x1"})

    def fake_runtime_report(self, refused=()):
        """Runtime stand-in: each reload publishes a new report, ``refused`` slots fail with 1409."""
        import hotkey_status as hks
        report = {"value": dict(hks.empty_report(), seq=0)}

        def reload_hotkeys():
            self.reloads += 1
            with self.state.lock:
                wheel = oa.wheel_config_from(self.state.config)
            entries = [hks.make_entry(slot, hk, slot not in refused,
                                      hks.ERROR_HOTKEY_ALREADY_REGISTERED)
                       for slot, hk in oa.wheel_triggers(wheel)]
            report["value"] = dict(hks.build_report(entries), seq=report["value"]["seq"] + 1)

        web_api.init(reload_hotkeys=reload_hotkeys, hotkey_report=lambda: report["value"])

    def test_viktor_wheel_hotkey_change_survives_reload(self):
        # Viktor 24.11.: Ctrl+Alt+Space, Alt removed, saved, F5 -> must still be Ctrl+Space.
        self.fake_runtime_report()
        self.request("POST", "/api/actions/wheel", {"hotkey": {"mods": MOD_CTRL | MOD_ALT, "vk": 0x20}})
        status, page = self.request("GET", "/api/settings")
        status, body = self.request("POST", "/api/actions/wheel", {"hotkey": {"mods": MOD_CTRL, "vk": 0x20}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["wheel"]["hotkey"]["display"], "Ctrl+Space")
        self.assertEqual(body["registration"]["pending"], False)
        self.assertEqual(body["registration"]["inactive"], [])
        self.assertEqual([e["hotkey"] for e in body["registration"]["entries"]], ["Ctrl+Space"])
        # The Settings page saves afterwards with what it loaded before (old wheel inside).
        status, body = self.request("POST", "/api/settings", dict(page, intercept_win_v=True))
        self.assertEqual(status, 200, body)
        # F5: API, memory and the file on disk (as on the next start) keep Ctrl+Space.
        status, body = self.request("GET", "/api/actions")
        self.assertEqual(body["wheel"]["hotkey"], {"mods": MOD_CTRL, "vk": 0x20, "display": "Ctrl+Space"})
        reloaded = config_schema.load_config(self.config_path)
        self.assertEqual(oa.wheel_config_from(reloaded)["hotkey"], {"mods": MOD_CTRL, "vk": 0x20})

    def test_refused_wheel_hotkey_stays_saved_and_says_why(self):
        self.fake_runtime_report(refused={"command_wheel"})
        status, body = self.request("POST", "/api/actions/wheel", {"hotkey": {"mods": MOD_CTRL, "vk": 0x20}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["wheel"]["hotkey"]["display"], "Ctrl+Space")
        self.assertEqual(len(body["registration"]["inactive"]), 1)
        self.assertIn("Ctrl+Space (Command wheel) is already taken by Windows or another program",
                      body["registration"]["inactive"][0])
        self.assertEqual(body["registration"]["entries"][0]["error_code"], 1409)
        # Never silently the old value.
        self.assertEqual(self.stored_wheel()["hotkey"], {"mods": MOD_CTRL, "vk": 0x20})

    def test_save_reports_pending_when_runtime_does_not_answer(self):
        web_api.init(reload_hotkeys=lambda: None, hotkey_report=lambda: {"seq": 7, "entries": []})
        original = web_api.REGISTRATION_WAIT_S
        web_api.REGISTRATION_WAIT_S = 0.1
        try:
            status, body = self.request("POST", "/api/actions/wheel",
                                        {"hotkey": {"mods": MOD_CTRL, "vk": 0x20}})
        finally:
            web_api.REGISTRATION_WAIT_S = original
        self.assertEqual(status, 200, body)
        self.assertTrue(body["registration"]["pending"])
        self.assertEqual(self.stored_wheel()["hotkey"], {"mods": MOD_CTRL, "vk": 0x20})

    def test_settings_save_ignores_stale_peers_layout_and_updates(self):
        status, page = self.request("GET", "/api/settings")
        with self.state.lock:
            self.state.config["peers"] = [{"name": "Neu", "host": "10.0.0.9", "port": 45781}]
        status, body = self.request("POST", "/api/settings", dict(
            page, peers=[], display_layout={"enabled": False}, updates={"channel": "x"},
            device_name="Renamed"))
        self.assertEqual(status, 200, body)
        stored = json.loads(self.config_path.read_text("utf-8"))
        self.assertEqual([p["host"] for p in stored["peers"]], ["10.0.0.9"])
        self.assertEqual(stored["device_name"], "Renamed")
        self.assertEqual(stored.get("updates"), page.get("updates"))
        self.assertEqual(stored.get("display_layout"), page.get("display_layout"))

    def test_settings_save_rejects_non_object_body(self):
        status, body = self.request("POST", "/api/settings", body=[])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_settings")

    def test_clipboard_spa_route_serves_index(self):
        root = Path(self.temporary.name) / "webgui-test"
        root.mkdir()
        (root / "index.html").write_text("<!doctype html><title>FlowShift SPA</title>", "utf-8")
        original = web_api.get_webgui_candidates
        web_api.get_webgui_candidates = lambda: [root]
        try:
            connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
            connection.request("GET", "/clipboard")
            response = connection.getresponse()
            data = response.read().decode("utf-8")
            connection.close()
        finally:
            web_api.get_webgui_candidates = original
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/html; charset=utf-8")
        self.assertIn("FlowShift SPA", data)

    def test_overlay_probe_opens_mode_and_points_at_log(self):
        calls = []

        def probe(mode):
            calls.append(mode)
            return {"type": "overlay_visible", "mode": mode}

        web_api.init(probe_overlay=probe)
        status, body = self.request("POST", "/api/overlay/probe", {"mode": "clipboard"})
        self.assertEqual(status, 200)
        self.assertEqual(body["log"], "overlay pixel check")
        status, body = self.request("POST", "/api/overlay/probe", {})
        self.assertEqual(status, 200)
        self.assertEqual(calls, ["clipboard", "command_wheel"])

    def test_overlay_probe_rejects_unknown_mode_and_reports_unavailable(self):
        status, _body = self.request("POST", "/api/overlay/probe", {"mode": "evil"})
        self.assertEqual(status, 400)
        status, body = self.request("POST", "/api/overlay/probe", {})
        self.assertEqual(status, 503)
        web_api.init(probe_overlay=lambda mode: {"ok": False, "reason": "host down"})
        status, body = self.request("POST", "/api/overlay/probe", {})
        self.assertEqual((status, body["error"]), (503, "host down"))


class CountingQueue:
    def __init__(self, calls):
        self.calls = calls

    def put(self, event):
        self.calls["inject"] += 1


class OriginCheckTests(unittest.TestCase):
    """Writing routes refuse foreign web pages before anything runs."""

    # Every route a web page could use to run actions, inject input or change
    # settings, with a body that would be accepted from the WebGUI.
    PROTECTED = (
        ("/api/actions/execute", {"action_id": "copy"}),
        ("/api/actions/wheel", {"pages": [["copy"]]}),
        ("/api/inject/type", {"text": "attrappe"}),
        ("/api/inject/key", {"vk": 0x41}),
        ("/api/settings", {"device_name": "fremd"}),
        ("/api/display/layout", {"layout": {}}),
        ("/api/peers/add", {"name": "fremd", "host": "10.0.0.9"}),
        ("/api/peers/remove", {"index": 0}),
        ("/api/peers/edit", {"index": 0, "name": "fremd"}),
        ("/api/peers/scan", {"timeout": 0.1}),
        ("/api/peers/ping", {"peer": "Alpha"}),
        ("/api/forwarding/activate", {"profile": "Alpha"}),
        ("/api/forwarding/deactivate", {}),
        ("/api/forwarding/toggle", {"profile": "Alpha"}),
        ("/api/auto-start", {"enabled": True}),
        ("/api/webgui/config", {"port": 5999}),
        ("/api/shutdown", {}),
        ("/api/overlay/show", {"mode": "clipboard"}),
    )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary.name) / "config.json"
        config_schema.save_config(self.config_path, {
            "device_name": "kept",
            "peers": [{"name": "Alpha", "host": "10.0.0.2", "port": 45781}]})
        self.state = FakeState(config_schema.load_config(self.config_path))
        self.calls = {"execute": 0, "inject": 0, "save_config": 0, "forwarding": 0,
                      "ping": 0, "shutdown": 0, "overlay": 0, "scan": 0, "auto_start": 0}
        self.state.inject_queue = CountingQueue(self.calls)

        def count(name, result=None):
            def call(*_args, **_kwargs):
                self.calls[name] += 1
                return result
            return call

        def save_config(config):
            self.calls["save_config"] += 1
            return config_schema.save_config(self.config_path, config)

        web_api._refs = {}
        web_api.init(istate=self.state, save_config=save_config,
                     execute_action=count("execute", {"ok": True, "reason": None}),
                     apply_profile=count("forwarding", (True, None)),
                     deactivate_forward=count("forwarding"),
                     ping_peer=count("ping", {"ok": True}),
                     request_shutdown=count("shutdown"),
                     request_overlay=count("overlay", {"type": "overlay_visible"}))
        self.originals = (web_api._scan_broadcast, web_api._auto_start_set,
                          web_api._auto_start_get, web_api.WEBGUI_CONFIG)
        web_api._scan_broadcast = count("scan", [])
        web_api._auto_start_set = count("auto_start")
        web_api._auto_start_get = lambda: False
        web_api.WEBGUI_CONFIG = Path(self.temporary.name) / "webgui" / "config.json"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        (web_api._scan_broadcast, web_api._auto_start_set,
         web_api._auto_start_get, web_api.WEBGUI_CONFIG) = self.originals
        web_api._refs = {}
        self.temporary.cleanup()

    def send(self, path, body, headers, method="POST"):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
        payload = json.dumps(body)
        connection.putrequest(method, path, skip_host=True)
        # Like the WebGUI: local Host, own Origin, JSON; a case overrides one.
        sent = {"Host": f"127.0.0.1:{self.port}", "Origin": f"http://127.0.0.1:{self.port}",
                "Content-Type": "application/json", "Content-Length": str(len(payload))}
        sent.update(headers)
        for name, value in sent.items():
            if value is not None:
                connection.putheader(name, value)
        connection.endheaders(None if sent["Content-Length"] == "0" else payload.encode("utf-8"))
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def assert_nothing_ran(self):
        self.assertEqual(self.calls, dict.fromkeys(self.calls, 0))
        self.assertNotIn("command_wheel", json.loads(self.config_path.read_text("utf-8")))
        self.assertEqual(self.state.config["device_name"], "kept")
        self.assertFalse(web_api.WEBGUI_CONFIG.exists())

    def test_foreign_requests_are_refused_on_every_writing_route(self):
        too_large = {"Content-Length": "65537"}
        refusals = (
            ({"Origin": "http://127.0.0.1:8080"}, 403, "origin is not allowed"),
            ({"Origin": "https://example.invalid"}, 403, "origin is not allowed"),
            ({"Origin": "null"}, 403, "origin is not allowed"),
            ({"Host": "example.invalid"}, 403, "require the local FlowShift host"),
            ({"Host": f"flowshift.invalid:{self.port}"}, 403, "require the local FlowShift host"),
            ({"Content-Type": "text/plain"}, 415, "require application/json"),
            ({"Content-Type": None}, 415, "require application/json"),
            ({"Transfer-Encoding": "chunked"}, 400, "chunked"),
            (too_large, 413, "body is too large"),
        )
        for path, body in self.PROTECTED:
            for headers, status, message in refusals:
                with self.subTest(path=path, headers=headers):
                    got_status, got = self.send(path, body, headers)
                    self.assertEqual(got_status, status, got)
                    self.assertIn(message, got["error"])
        self.assert_nothing_ran()

    def test_action_routes_have_a_small_body_limit(self):
        for path in ("/api/actions/execute", "/api/actions/wheel"):
            with self.subTest(path=path):
                status, body = self.send(path, {}, {"Content-Length": "16385"})
                self.assertEqual(status, 413)
                self.assertEqual(body["error"], "action request body is too large")
        self.assertEqual(web_api._write_request_policy("/api/actions/execute"),
                         ("action", 16_384))
        self.assertEqual(web_api._write_request_policy("/api/inject/type"),
                         ("local API", 65_536))
        self.assert_nothing_ran()

    def test_webgui_origin_runs_execute_inject_and_save(self):
        for origin in (f"http://127.0.0.1:{web_api.API_PORT}", "http://localhost:5173"):
            with self.subTest(origin=origin):
                status, body = self.send("/api/actions/execute", {"action_id": "copy"},
                                         {"Origin": origin})
                self.assertEqual((status, body["ok"]), (200, True))
                status, body = self.send("/api/inject/type", {"text": "attrappe"},
                                         {"Origin": origin})
                self.assertEqual((status, body), (200, {"ok": True, "chars": 8}))
                status, body = self.send("/api/actions/wheel", {"pages": [["copy"]]},
                                         {"Origin": origin})
                self.assertEqual(status, 200, body)
        self.assertEqual((self.calls["execute"], self.calls["inject"],
                          self.calls["save_config"]), (2, 2, 2))
        stored = json.loads(self.config_path.read_text("utf-8"))
        self.assertEqual(stored["command_wheel"]["pages"], [["copy"]])

    def test_missing_origin_is_refused_even_with_local_host(self):
        # Browsers always send Origin on POST/DELETE and the overlay host names
        # its overlay origin, so a request without one is refused before it runs.
        for path, body in self.PROTECTED:
            for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
                with self.subTest(path=path, host=host):
                    status, got = self.send(path, body, {"Host": host, "Origin": None})
                    self.assertEqual((status, got["error_code"]), (403, "origin_required"))
        self.assert_nothing_ran()

    def test_webgui_delete_needs_json_type_and_own_origin(self):
        deleted = []

        class ClipboardManager:
            def delete_item(self, ident, item_id):
                deleted.append((ident, item_id))
                return True

        web_api.init(clip_mgr=ClipboardManager())
        path = "/api/clipboard/item/attrappe?profile=local"
        status, body = self.send(path, {}, {"Content-Length": "0",
                                            "Origin": "https://example.invalid"},
                                 method="DELETE")
        self.assertEqual((status, body["error"]), (403, "request origin is not allowed"))
        status, body = self.send(path, {}, {"Content-Type": None, "Content-Length": "0"},
                                 method="DELETE")
        self.assertEqual((status, body["error"]), (415, "requests require application/json"))
        self.assertEqual(deleted, [])
        # The WebGUI's del() sends the JSON type without a body.
        status, body = self.send(path, {}, {"Content-Length": "0"}, method="DELETE")
        self.assertEqual((status, body), (200, {"deleted": True}))
        self.assertEqual(deleted, [("local", "attrappe")])


if __name__ == "__main__":
    unittest.main()
