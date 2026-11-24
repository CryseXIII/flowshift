"""Unit tests for the Command Wheel Action Registry."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import action_handlers as ah
import overlay_actions as oa
from runtime_model import MOD_ALT, MOD_CTRL, MOD_SHIFT


class RegistryTests(unittest.TestCase):
    def test_first_actions_exist_with_exact_kinds(self):
        ids = oa.action_ids()
        for required in ("copy", "paste", "cut", "delete"):
            self.assertIn(required, ids)
            self.assertEqual(oa.get_action(required)["kind"], "keys")
        self.assertEqual(oa.get_action("open_clipboard")["kind"], "runtime")
        self.assertEqual(oa.get_action("clipboard_sync")["kind"], "runtime")

    def test_public_projection_has_no_sequences(self):
        public = oa.public_actions()
        self.assertEqual([a["id"] for a in public], list(oa.action_ids()))
        for entry in public:
            self.assertEqual(set(entry), {"id", "label", "icon", "kind"})

    def test_key_events_are_balanced_and_ordered(self):
        events = oa.key_events_for(oa.get_action("copy"))
        self.assertEqual(events, [
            {"type": "key", "code": 0x11}, {"type": "key", "code": 0x43},
            {"type": "key_up", "code": 0x43}, {"type": "key_up", "code": 0x11},
        ])
        self.assertEqual(oa.key_events_for(oa.get_action("delete")),
                         [{"type": "key", "code": 0x2E}, {"type": "key_up", "code": 0x2E}])
        for action_id in oa.action_ids():
            action = oa.get_action(action_id)
            if action["kind"] != "keys":
                continue
            downs = [e["code"] for e in oa.key_events_for(action) if e["type"] == "key"]
            ups = [e["code"] for e in oa.key_events_for(action) if e["type"] == "key_up"]
            self.assertEqual(sorted(downs), sorted(ups), action_id)

    def test_key_events_rejects_runtime_action(self):
        with self.assertRaises(oa.ActionError) as ctx:
            oa.key_events_for(oa.get_action("open_clipboard"))
        self.assertEqual(ctx.exception.code, "not_key_action")

    def test_unknown_and_malformed_ids_are_rejected(self):
        for bad, code in (("nope", "unknown_action"), ("", "invalid_action"),
                          (None, "invalid_action"), (7, "invalid_action"),
                          ("x" * 65, "invalid_action")):
            with self.assertRaises(oa.ActionError) as ctx:
                oa.get_action(bad)
            self.assertEqual(ctx.exception.code, code, repr(bad))


class WheelConfigTests(unittest.TestCase):
    def test_defaults_when_missing_or_invalid(self):
        wheel = oa.wheel_config_from({})
        self.assertEqual(wheel["pages"], [list(p) for p in oa.DEFAULT_PAGES])
        # Viktor 24.11.: Ctrl+right click is the main hotkey, no extra ones.
        self.assertEqual(wheel["hotkey"], {"mods": MOD_CTRL, "mouse": "right"})
        self.assertEqual(wheel["extra_hotkeys"], [])
        self.assertEqual(wheel["version"], oa.WHEEL_CONFIG_VERSION)
        self.assertLessEqual(max(len(p) for p in wheel["pages"]), oa.MAX_SLOTS_PER_PAGE)
        broken = oa.wheel_config_from({"command_wheel": {"pages": [["nope"]]}})
        self.assertEqual(broken, oa.wheel_config_from({}))

    def test_empty_slots_and_pages_are_dropped(self):
        pages = oa.normalize_pages([["copy", None, "", "paste"], [], ["delete"]])
        self.assertEqual(pages, [["copy", "paste"], ["delete"]])

    def test_limits_are_enforced(self):
        with self.assertRaises(oa.ActionError) as ctx:
            oa.normalize_pages([["copy"] * 9])
        self.assertEqual(ctx.exception.code, "too_many_slots")
        with self.assertRaises(oa.ActionError) as ctx:
            oa.normalize_pages([["copy"]] * 17)
        self.assertEqual(ctx.exception.code, "too_many_pages")
        with self.assertRaises(oa.ActionError) as ctx:
            oa.normalize_pages([[], [None]])
        self.assertEqual(ctx.exception.code, "empty_wheel")
        with self.assertRaises(oa.ActionError) as ctx:
            oa.normalize_pages([["copy", "bogus"]])
        self.assertEqual(ctx.exception.code, "unknown_action")
        with self.assertRaises(oa.ActionError):
            oa.normalize_pages("copy")

    def test_hotkey_validation(self):
        self.assertEqual(oa.normalize_hotkey({"mods": MOD_CTRL, "vk": 0x57}),
                         {"mods": MOD_CTRL, "vk": 0x57})
        self.assertEqual(oa.normalize_hotkey({"mods": MOD_CTRL, "mouse": "x2"}),
                         {"mods": MOD_CTRL, "mouse": "x2"})
        self.assertEqual(oa.normalize_hotkey({"mods": 0, "mouse": "x1"}), {"mods": 0, "mouse": "x1"})
        self.assertIsNone(oa.normalize_hotkey(None))
        for bad in ({"mods": 0, "vk": 0x57}, {"mods": 16, "vk": 0x57},
                    {"mods": MOD_CTRL, "vk": 0x01}, {"mods": MOD_CTRL},
                    {"mods": True, "vk": 0x57}, "Ctrl+W",
                    {"mods": MOD_CTRL, "mouse": "x3"}, {"mods": MOD_CTRL, "vk": 0x57, "mouse": "x1"}):
            with self.assertRaises(oa.ActionError) as ctx:
                oa.normalize_hotkey(bad)
            self.assertEqual(ctx.exception.code, "invalid_hotkey", repr(bad))
        for bad in ({"mods": 0, "mouse": "left"}, {"mods": 0, "mouse": "right"},
                    {"mods": 0, "mouse": "wheel_up"}):
            with self.assertRaises(oa.ActionError) as ctx:
                oa.normalize_hotkey(bad)
            self.assertEqual(ctx.exception.code, "mouse_needs_modifier", repr(bad))

    def test_extra_hotkeys_and_cleared_main_hotkey(self):
        wheel = oa.normalize_wheel_config({
            "version": 2, "hotkey": None,
            "extra_hotkeys": [{"mods": MOD_CTRL, "mouse": "x1"}, None, {"mods": MOD_CTRL | MOD_ALT, "vk": 0x20}]})
        self.assertIsNone(wheel["hotkey"])
        self.assertEqual(wheel["extra_hotkeys"], [{"mods": MOD_CTRL, "mouse": "x1"},
                                                  {"mods": MOD_CTRL | MOD_ALT, "vk": 0x20}])
        self.assertEqual([slot for slot, _ in oa.wheel_triggers(wheel)],
                         ["command_wheel_extra_1", "command_wheel_extra_2"])
        self.assertEqual(oa.wheel_payload({"command_wheel": wheel})["hotkey"], "")
        with self.assertRaises(oa.ActionError) as ctx:
            oa.normalize_extra_hotkeys([{"mods": 0, "mouse": "x1"}] * 4)
        self.assertEqual(ctx.exception.code, "too_many_hotkeys")
        with self.assertRaises(oa.ActionError) as ctx:
            oa.normalize_wheel_config({"version": 2, "extra_hotkeys": [dict(oa.DEFAULT_HOTKEY)]})
        self.assertEqual(ctx.exception.code, "hotkey_conflict")


class WheelMigrationTests(unittest.TestCase):
    """Wheel blocks from before 0.7.0-dev.49 (Viktor 24.11.: existing settings stay valid)."""

    def test_old_default_becomes_ctrl_right_click_without_extra(self):
        cfg = {"command_wheel": {"pages": [["copy"]], "hotkey": {"mods": MOD_CTRL | MOD_ALT, "vk": 0x20},
                                 "trigger": {"tap_open": True, "hold_swipe": True, "hold_ms": 300,
                                             "mouse_button": "none"}}}
        self.assertTrue(oa.migrate_wheel_config(cfg))
        self.assertEqual(cfg["command_wheel"], {
            "version": 2, "pages": [["copy"]], "hotkey": {"mods": MOD_CTRL, "mouse": "right"},
            "extra_hotkeys": [], "trigger": {"tap_open": True, "hold_swipe": True, "hold_ms": 300}})
        # Second load: nothing to do.
        self.assertFalse(oa.migrate_wheel_config(cfg))

    def test_own_hotkey_and_side_button_stay_as_extra_hotkeys(self):
        cfg = {"command_wheel": {"hotkey": {"mods": MOD_CTRL | MOD_SHIFT, "vk": 0x57},
                                 "trigger": {"mouse_button": "x2"}}}
        self.assertTrue(oa.migrate_wheel_config(cfg))
        wheel = cfg["command_wheel"]
        self.assertEqual(wheel["hotkey"], {"mods": MOD_CTRL, "mouse": "right"})
        self.assertEqual(wheel["extra_hotkeys"], [{"mods": MOD_CTRL | MOD_SHIFT, "vk": 0x57},
                                                  {"mods": 0, "mouse": "x2"}])
        self.assertEqual(wheel["pages"], [list(p) for p in oa.DEFAULT_PAGES])

    def test_missing_block_stays_missing_and_uses_defaults(self):
        cfg = {}
        self.assertFalse(oa.migrate_wheel_config(cfg))
        self.assertNotIn("command_wheel", cfg)
        self.assertEqual(oa.wheel_config_from(cfg)["hotkey"], oa.DEFAULT_HOTKEY)

    def test_broken_old_hotkey_is_dropped(self):
        cfg = {"command_wheel": {"hotkey": {"mods": 0, "vk": 0x57}}}
        self.assertTrue(oa.migrate_wheel_config(cfg))
        self.assertEqual(cfg["command_wheel"]["extra_hotkeys"], [])

    def test_wheel_payload_only_contains_used_actions(self):
        payload = oa.wheel_payload({"command_wheel": {"pages": [["cut", "paste"]]}})
        self.assertEqual(payload["pages"], [["cut", "paste"]])
        self.assertEqual(set(payload["actions"]), {"cut", "paste"})
        self.assertEqual(payload["actions"]["cut"]["label"], "Cut")
        self.assertEqual(payload["hotkey"], "Ctrl+Right click")
        self.assertEqual(payload["registry_version"], oa.REGISTRY_VERSION)


class ExecuteRequestTests(unittest.TestCase):
    def test_valid_request_returns_copy_and_clean_context(self):
        action, context = oa.validate_execute_request(
            {"action_id": "paste", "context": {"source": "command_wheel", "page": 1}})
        self.assertEqual(action["id"], "paste")
        self.assertEqual(context, {"source": "command_wheel", "page": 1})
        action["id"] = "mutated"
        self.assertEqual(oa.get_action("paste")["id"], "paste")

    def test_rejections(self):
        cases = (
            ("nope", "invalid_request"),
            ({"action_id": "paste", "extra": 1}, "invalid_request"),
            ({"action_id": "nope"}, "unknown_action"),
            ({"action_id": "paste", "context": []}, "invalid_context"),
            ({"action_id": "paste", "context": {"k": {"nested": 1}}}, "invalid_context"),
            ({"action_id": "paste", "context": {"k": "x" * 257}}, "invalid_context"),
            ({"action_id": "paste", "context": {str(i): i for i in range(17)}}, "invalid_context"),
        )
        for body, code in cases:
            with self.assertRaises(oa.ActionError) as ctx:
                oa.validate_execute_request(body)
            self.assertEqual(ctx.exception.code, code, repr(body))

    def test_body_cannot_choose_channel_origin_or_confirmation(self):
        for key, value in (("channel", "wheel_swipe"), ("channel", "web_api"),
                           ("origin", "http://127.0.0.1:5000"), ("confirm", True)):
            with self.subTest(key=key, value=value):
                with self.assertRaises(oa.ActionError) as ctx:
                    oa.validate_execute_request({"action_id": "copy", key: value})
                self.assertEqual(ctx.exception.code, "invalid_request")


class HandlerTableTests(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.ran = []

        def handler(name):
            def run(action, context):
                self.ran.append((name, action["id"], context))
                return {"ok": True, "reason": None}
            return run

        self.registry = ah.build_registry(
            {"keys": handler("keys"), "runtime": handler("runtime")}, log=self.lines.append)

    def test_every_registered_kind_has_a_risk_class(self):
        self.assertEqual(set(self.registry.kinds()), {"keys", "runtime"})
        self.assertEqual(self.registry.risk_class("keys"), "L1")
        self.assertEqual(self.registry.risk_class("runtime"), "L0")
        for kind in self.registry.kinds():
            self.assertIn(self.registry.risk_class(kind), ah.RISK_CLASSES)
        # Every built-in action has a handler, so none of the nine is lost.
        kinds = {oa.get_action(action_id)["kind"] for action_id in oa.action_ids()}
        self.assertEqual(len(oa.action_ids()), 9)
        self.assertTrue(kinds.issubset(set(self.registry.kinds())), kinds)

    def test_handler_without_class_is_refused(self):
        registry = ah.HandlerRegistry()
        for risk_class in (None, "", "L9", "l1"):
            with self.subTest(risk_class=risk_class):
                with self.assertRaises(ah.HandlerError):
                    registry.register("keys", lambda action, context: None, risk_class)
        with self.assertRaises(ah.HandlerError):
            registry.register("keys", lambda action, context: None)
        self.assertEqual(registry.kinds(), ())
        # A productive kind missing from HANDLER_CLASSES cannot be registered.
        with self.assertRaises(ah.HandlerError):
            ah.build_registry({"shell": lambda action, context: None})

    def test_unknown_channel_is_refused_before_the_handler_runs(self):
        for channel in (None, "", "remote", "WEB_API", "wheel"):
            with self.subTest(channel=channel):
                result = self.registry.dispatch(oa.get_action("copy"), {}, channel)
                self.assertEqual(result, {"ok": False, "reason": "invalid_channel"})
        self.assertEqual(self.ran, [])
        self.assertEqual(self.lines, [])

    def test_unknown_kind_is_refused(self):
        result = self.registry.dispatch({"id": "x", "kind": "shell"}, {}, ah.CHANNEL_WEB_API)
        self.assertEqual(result, {"ok": False, "reason": "unknown_kind"})
        self.assertEqual((self.ran, self.lines), ([], []))

    def test_check_logs_only_id_channel_and_class_then_runs_the_handler(self):
        context = {"source": "command_wheel", "channel": "remote", "note": "inhalt-attrappe"}
        result = self.registry.dispatch(oa.get_action("paste"), context, ah.CHANNEL_WHEEL_SWIPE)
        self.assertEqual(result, {"ok": True, "reason": None})
        self.assertEqual(self.lines, ["action check id=paste channel=wheel_swipe class=L1"])
        self.assertEqual(self.ran, [("keys", "paste", context)])
        self.registry.dispatch(oa.get_action("open_clipboard"), {}, ah.CHANNEL_WEB_API)
        self.assertEqual(self.lines[-1], "action check id=open_clipboard channel=web_api class=L0")


if __name__ == "__main__":
    unittest.main()
