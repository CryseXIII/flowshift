"""Unit tests for the Command Wheel Action Registry."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import overlay_actions as oa
from runtime_model import MOD_ALT, MOD_CTRL


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
        self.assertEqual(wheel["hotkey"], {"mods": MOD_CTRL | MOD_ALT, "vk": 0x20})
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
        for bad in ({"mods": 0, "vk": 0x57}, {"mods": 16, "vk": 0x57},
                    {"mods": MOD_CTRL, "vk": 0x01}, {"mods": MOD_CTRL},
                    {"mods": True, "vk": 0x57}, "Ctrl+W"):
            with self.assertRaises(oa.ActionError) as ctx:
                oa.normalize_hotkey(bad)
            self.assertEqual(ctx.exception.code, "invalid_hotkey", repr(bad))

    def test_wheel_payload_only_contains_used_actions(self):
        payload = oa.wheel_payload({"command_wheel": {"pages": [["cut", "paste"]]}})
        self.assertEqual(payload["pages"], [["cut", "paste"]])
        self.assertEqual(set(payload["actions"]), {"cut", "paste"})
        self.assertEqual(payload["actions"]["cut"]["label"], "Cut")
        self.assertEqual(payload["hotkey"], "Ctrl+Alt+Space")
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


if __name__ == "__main__":
    unittest.main()
