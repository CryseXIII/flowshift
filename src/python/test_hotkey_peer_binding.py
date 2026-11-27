"""Forward hotkeys stay bound to the PC (BOOK-Viktor 27.11. 03:54).

Seen in flowshift.log: ``skipping invalid hotkey Forward (ungueltiges Ziel)
Ctrl+Alt+1 action=forward_peer:device:98194f67 reason=forward target does not
resolve to a peer`` (the entry had become ``endpoint:192.168.8.18:45781``) and
``unknown action forward_endpoint:192.168.8.18:45781`` (the WebGUI stored
``forward_<identity>``). Covered here: the old WebGUI form is understood and
rewritten, an orphaned hotkey follows the PC by its stored name or as the only
candidate, a learned device ID moves the hotkey along, and a hotkey that still
has no PC keeps a plain reason for the red WebGUI line.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime_model as rm

LAN = "192.168.8.18"


def bettina(device_id=""):
    peer = {"name": "Laptop-Bettina", "host": LAN, "port": 45781}
    if device_id:
        peer["device_id"] = device_id
    return peer


def hotkey(action, key=0x31, label="Forward (ungültiges Ziel)", **extra):
    return dict({"action": action, "mods": rm.MOD_CTRL | rm.MOD_ALT, "key": key, "label": label},
                **extra)


class ForwardActionFormTests(unittest.TestCase):
    def test_webgui_form_is_rewritten_to_forward_peer(self):
        self.assertEqual(rm.normalize_forward_action("forward_endpoint:192.168.8.18:45781"),
                         "forward_peer:endpoint:192.168.8.18:45781")
        self.assertEqual(rm.normalize_forward_action("forward_device:98194f67"),
                         "forward_peer:device:98194f67")
        self.assertEqual(rm.normalize_forward_action("forward_peer:device:x"), "forward_peer:device:x")
        self.assertEqual(rm.normalize_forward_action("forward_3"), "forward_3")
        self.assertEqual(rm.normalize_forward_action("return_local"), "return_local")

    def test_sync_makes_the_webgui_action_valid(self):
        cfg = {"peers": [bettina()], "hotkeys": [
            hotkey("forward_endpoint:192.168.8.18:45781", label="Forward to Laptop-Bettina"),
            hotkey("return_local", key=0x30, label="Return to local")]}
        rm.sync_hotkeys(cfg)
        self.assertEqual(cfg["hotkeys"][0]["action"], "forward_peer:endpoint:192.168.8.18:45781")
        self.assertIsNone(rm.hotkey_registration_error(cfg, cfg["hotkeys"][0]))
        self.assertEqual(len([h for h in cfg["hotkeys"] if rm.is_forward_action(h["action"])]), 1)


class OrphanRebindTests(unittest.TestCase):
    def test_viktors_config_hotkey_follows_the_reentered_pc(self):
        # The entry lost its device ID; the hotkey kept the old one.
        cfg = {"peers": [bettina()], "hotkeys": [
            hotkey("forward_peer:device:98194f67"),
            hotkey("return_local", key=0x30, label="Return to local")]}
        self.assertTrue(rm.sync_hotkeys(cfg))
        forwards = [h for h in cfg["hotkeys"] if rm.is_forward_action(h["action"])]
        self.assertEqual(len(forwards), 1)  # no second hotkey added for the same PC
        self.assertEqual(forwards[0]["action"], "forward_peer:endpoint:192.168.8.18:45781")
        self.assertEqual(forwards[0]["key"], 0x31)
        self.assertEqual(forwards[0]["label"], "Forward to Laptop-Bettina")
        self.assertEqual(forwards[0]["target_name"], "Laptop-Bettina")

    def test_stored_name_picks_the_right_pc_among_several(self):
        cfg = {"peers": [{"name": "Surface", "host": "192.168.8.30", "port": 45781}, bettina()],
               "hotkeys": [hotkey("forward_peer:device:98194f67", target_name="Laptop-Bettina"),
                           hotkey("forward_peer:device:dead", key=0x32, target_name="Surface")]}
        rm.rebind_orphaned_forward_hotkeys(cfg)
        self.assertEqual(cfg["hotkeys"][0]["action"], "forward_peer:endpoint:192.168.8.18:45781")
        self.assertEqual(cfg["hotkeys"][1]["action"], "forward_peer:endpoint:192.168.8.30:45781")

    def test_no_guess_between_several_unnamed_hotkeys(self):
        cfg = {"peers": [bettina()],
               "hotkeys": [hotkey("forward_peer:device:a1"), hotkey("forward_peer:device:b2", key=0x32)]}
        self.assertFalse(rm.rebind_orphaned_forward_hotkeys(cfg))
        reason = rm.hotkey_registration_error(cfg, cfg["hotkeys"][0])
        self.assertIn("forward target does not resolve to a peer", reason)
        self.assertIn("device:a1", reason)
        self.assertIn("Choose the PC again", reason)

    def test_a_pc_that_has_its_own_hotkey_is_not_taken(self):
        cfg = {"peers": [bettina()],
               "hotkeys": [hotkey("forward_peer:endpoint:192.168.8.18:45781", key=0x31),
                           hotkey("forward_peer:device:98194f67", key=0x32)]}
        self.assertFalse(rm.rebind_orphaned_forward_hotkeys(cfg))
        self.assertIsNotNone(rm.hotkey_registration_error(cfg, cfg["hotkeys"][1]))

    def test_learned_device_id_moves_the_hotkey_to_the_device(self):
        cfg = {"device_id": "aaaa1111", "peers": [bettina()],
               "hotkeys": [hotkey("forward_peer:endpoint:192.168.8.18:45781",
                                  label="Forward to Laptop-Bettina")]}
        self.assertEqual(rm.learn_peer_device(cfg, LAN, 45781, "98194f67", "Laptop-Bettina"),
                         ("endpoint:192.168.8.18:45781", "device:98194f67"))
        self.assertEqual(cfg["hotkeys"][0]["action"], "forward_peer:device:98194f67")
        self.assertIsNone(rm.hotkey_registration_error(cfg, cfg["hotkeys"][0]))

    def test_unknown_webgui_form_without_pc_gets_the_plain_reason(self):
        cfg = {"peers": [], "hotkeys": []}
        reason = rm.hotkey_registration_error(cfg, hotkey("forward_endpoint:10.0.0.9:45781"))
        self.assertIn("endpoint:10.0.0.9:45781", reason)
        self.assertNotIn("unknown action", reason)


if __name__ == "__main__":
    unittest.main()
