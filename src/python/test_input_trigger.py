"""Hotkey triggers with keys or mouse buttons (``input_trigger``), platform independent.

Covers validation (a plain left/right click or wheel is refused), display text,
conflicts, peer hotkey rows and the decision of the always-on mouse hook
(``MouseTriggerMatcher``): which presses are swallowed, that the release of a
swallowed press is swallowed exactly once, wheel notches fire once per scroll.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import input_trigger as it
import runtime_model as rm
from runtime_model import MOD_ALT, MOD_CTRL, MOD_SHIFT, MOD_WIN

CTRL_RIGHT = {"mods": MOD_CTRL, "mouse": "right"}
WHEEL = ("wheel", "command_wheel")


class NormalizeTests(unittest.TestCase):
    def test_keys_mouse_buttons_and_empty(self):
        self.assertEqual(it.normalize({"mods": MOD_CTRL, "vk": 0x56}), {"mods": MOD_CTRL, "vk": 0x56})
        self.assertEqual(it.normalize(dict(CTRL_RIGHT)), CTRL_RIGHT)
        for button in it.MOUSE_BUTTONS:
            self.assertEqual(it.normalize({"mods": MOD_ALT, "mouse": button})["mouse"], button)
        self.assertIsNone(it.normalize(None))
        with self.assertRaises(it.TriggerError):
            it.normalize(None, allow_empty=False)

    def test_plain_left_right_and_wheel_are_refused(self):
        for button in ("left", "right", "wheel_up", "wheel_down"):
            with self.subTest(button=button):
                with self.assertRaises(it.TriggerError) as ctx:
                    it.normalize({"mods": 0, "mouse": button})
                self.assertEqual(ctx.exception.code, "mouse_needs_modifier")
        # Middle click and the side buttons may be used alone.
        for button in ("middle", "x1", "x2"):
            self.assertEqual(it.normalize({"mods": 0, "mouse": button}), {"mods": 0, "mouse": button})

    def test_invalid_shapes(self):
        for bad in ("Ctrl+V", {"mods": MOD_CTRL}, {"mods": MOD_CTRL, "vk": 0x56, "mouse": "x1"},
                    {"mods": 32, "mouse": "x1"}, {"mods": True, "mouse": "x1"},
                    {"mods": MOD_CTRL, "mouse": "x3"}, {"mods": 0, "vk": 0x56},
                    {"mods": MOD_CTRL, "vk": 0x07}):
            with self.subTest(bad=bad):
                with self.assertRaises(it.TriggerError):
                    it.normalize(bad)
        # Peer hotkeys may be a key alone (F-keys), the overlay hotkeys not.
        self.assertEqual(it.normalize({"mods": 0, "vk": 0x75}, key_needs_modifier=False),
                         {"mods": 0, "vk": 0x75})

    def test_display(self):
        self.assertEqual(it.display(CTRL_RIGHT), "Ctrl+Right click")
        self.assertEqual(it.display({"mods": MOD_CTRL | MOD_SHIFT, "mouse": "x1"}), "Ctrl+Shift+Side button 1")
        self.assertEqual(it.display({"mods": 0, "mouse": "x2"}), "Side button 2")
        self.assertEqual(it.display({"mods": MOD_WIN, "mouse": "wheel_down"}), "Win+Wheel down")
        self.assertEqual(it.display({"mods": MOD_CTRL | MOD_ALT, "vk": 0x20}), "Ctrl+Alt+Space")
        self.assertEqual(it.display(None), "")

    def test_same_and_conflicts(self):
        self.assertTrue(it.same(CTRL_RIGHT, {"mods": MOD_CTRL, "mouse": "right"}))
        self.assertFalse(it.same(CTRL_RIGHT, {"mods": MOD_CTRL | MOD_SHIFT, "mouse": "right"}))
        self.assertFalse(it.same(None, None))
        found = it.find_conflicts([("a", "Command wheel", CTRL_RIGHT), ("b", "Clipboard", None),
                                   ("c", "Return to local", dict(CTRL_RIGHT)),
                                   ("d", "Forward", {"mods": MOD_CTRL, "vk": 0x31})])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["slots"], ["a", "c"])
        self.assertEqual(found[0]["message"], "Ctrl+Right click is assigned twice: Command wheel and "
                                              "Return to local. Choose another hotkey for one of them.")


class PeerHotkeyTests(unittest.TestCase):
    def test_rows_with_mouse_buttons_keep_key_zero(self):
        rows = it.normalize_peer_hotkeys([
            {"action": "return_local", "mods": MOD_CTRL, "mouse": "x1", "label": "Return"},
            {"action": "forward_peer:device:b5", "mods": 5, "key": 0x31, "label": "Fwd"},
            {"action": "forward_peer:device:c6", "mods": 5, "key": 0, "label": "Empty"}])
        self.assertEqual(rows[0], {"action": "return_local", "label": "Return", "mods": MOD_CTRL,
                                   "key": 0, "mouse": "x1"})
        self.assertEqual([it.peer_hotkey_trigger(r) for r in rows],
                         [{"mods": MOD_CTRL, "mouse": "x1"}, {"mods": 5, "vk": 0x31}, None])
        with self.assertRaises(it.TriggerError):
            it.normalize_peer_hotkeys([{"action": "return_local", "mods": 0, "mouse": "right"}])
        with self.assertRaises(it.TriggerError):
            it.normalize_peer_hotkeys("nope")

    def test_runtime_binding_of_a_mouse_row(self):
        cfg = {"peers": [], "hotkeys": [
            {"action": "return_local", "mods": MOD_CTRL, "key": 0, "mouse": "x2", "label": "Return"}]}
        [binding] = rm.load_hotkeys(cfg)
        self.assertEqual(binding.trigger(), {"mods": MOD_CTRL, "mouse": "x2"})
        self.assertEqual(binding.display(), "Ctrl+Side button 2")
        # Never matched by the keyboard path, and registrable (not "no key assigned").
        self.assertFalse(binding.matches(MOD_CTRL, 0))
        self.assertIsNone(rm.hotkey_registration_error(
            cfg, {"action": "return_local", "key": 0, "mouse": "x2"}))
        self.assertEqual(rm.hotkey_registration_error(cfg, {"action": "return_local", "key": 0}),
                         "no key assigned")


class MouseTriggerMatcherTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.m = it.MouseTriggerMatcher(clock=lambda: self.now[0])
        self.m.set_bindings([
            (dict(CTRL_RIGHT), WHEEL, "local"),
            ({"mods": MOD_CTRL, "mouse": "x1"}, ("wheel", "command_wheel_extra_1"), "local"),
            ({"mods": MOD_CTRL | MOD_ALT, "vk": 0x20}, WHEEL, "local"),  # keys are ignored here
            ({"mods": MOD_CTRL, "mouse": "x2"}, ("return", "return_local"), "forwarding"),
            ({"mods": MOD_CTRL, "mouse": "wheel_down"}, ("clipboard",), "local"),
        ])

    def test_ctrl_right_click_press_and_release_are_swallowed_once(self):
        d = self.m.decide
        self.assertEqual(d("down", "right", 0), ("pass", None, "right"))  # context menus stay
        self.assertEqual(d("up", "right", 0), ("pass", None, "right"))
        self.assertEqual(d("down", "right", MOD_CTRL), ("press", WHEEL, "right"))
        # The release is swallowed even when Ctrl was let go first.
        self.assertEqual(d("up", "right", 0), ("release", WHEEL, "right"))
        self.assertEqual(d("up", "right", 0), ("pass", None, "right"))
        # Other modifiers make a different combination.
        self.assertEqual(d("down", "right", MOD_CTRL | MOD_SHIFT)[0], "pass")

    def test_injected_and_unbound_events_pass(self):
        self.assertEqual(self.m.decide("down", "right", MOD_CTRL, injected=True)[0], "pass")
        self.assertEqual(self.m.decide("down", "left", MOD_CTRL)[0], "pass")
        self.assertEqual(self.m.decide("down", "x1", 0)[0], "pass")  # side button keeps "back"
        self.assertEqual(self.m.decide("down", "bogus", MOD_CTRL)[0], "pass")
        self.assertTrue(self.m.interested("right"))
        self.assertFalse(self.m.interested("left"))

    def test_side_button_and_forwarding_bindings(self):
        d = self.m.decide
        self.assertEqual(d("down", "x1", MOD_CTRL), ("press", ("wheel", "command_wheel_extra_1"), "x1"))
        self.assertEqual(d("up", "x1", MOD_CTRL)[0], "release")
        # While forwarding only the "forwarding" bindings count (return to local) ...
        self.assertEqual(d("down", "right", MOD_CTRL, forwarding_active=True)[0], "pass")
        self.assertEqual(d("down", "x2", MOD_CTRL, forwarding_active=True),
                         ("press", ("return", "return_local"), "x2"))
        # ... and the release is swallowed after forwarding ended, too.
        self.assertEqual(d("up", "x2", 0, forwarding_active=False)[0], "release")
        # Locally the return hotkey is not taken: Ctrl+side button 2 stays "forward".
        self.assertEqual(d("down", "x2", MOD_CTRL)[0], "pass")

    def test_wheel_fires_once_per_scroll(self):
        d = self.m.decide
        self.assertEqual(d("wheel", "wheel_down", MOD_CTRL), ("fire", ("clipboard",), "wheel_down"))
        self.now[0] += 0.05
        self.assertEqual(d("wheel", "wheel_down", MOD_CTRL)[0], "swallow")
        # Notches closer than WHEEL_REPEAT_S belong to the same scroll, even
        # when the scroll as a whole lasts longer.
        self.now[0] += it.WHEEL_REPEAT_S - 0.05
        self.assertEqual(d("wheel", "wheel_down", MOD_CTRL)[0], "swallow")
        self.now[0] += it.WHEEL_REPEAT_S - 0.05
        self.assertEqual(d("wheel", "wheel_down", MOD_CTRL)[0], "swallow")
        self.now[0] += it.WHEEL_REPEAT_S + 0.01
        self.assertEqual(d("wheel", "wheel_down", MOD_CTRL)[0], "fire")
        self.assertEqual(d("wheel", "wheel_up", MOD_CTRL)[0], "pass")
        self.assertEqual(d("wheel", "wheel_down", 0)[0], "pass")  # plain scrolling untouched

    def test_new_bindings_keep_a_running_hold(self):
        self.assertEqual(self.m.decide("down", "right", MOD_CTRL)[0], "press")
        self.m.set_bindings([({"mods": MOD_CTRL, "mouse": "x1"}, WHEEL, "local")])
        self.assertTrue(self.m.interested("right"))
        self.assertEqual(self.m.decide("up", "right", 0)[0], "release")
        self.assertFalse(self.m.interested("right"))
        self.assertEqual(self.m.decide("down", "right", MOD_CTRL)[0], "pass")
        self.assertEqual(self.m.bindings(), {(MOD_CTRL, "x1", "local"): WHEEL})


if __name__ == "__main__":
    unittest.main()
