"""Tests for the overlay hotkey status report and the clipboard overlay hotkey setting."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_model as cbm
import hotkey_status as hks
import web_api
from runtime_model import MOD_ALT, MOD_CTRL, MOD_SHIFT, MOD_WIN

SPACE = {"mods": MOD_CTRL | MOD_ALT, "vk": 0x20}


class ClipboardOverlayHotkeySettingTests(unittest.TestCase):
    def test_default_is_ctrl_alt_v(self):
        hotkey = cbm.clipboard_settings({})["overlay_hotkey"]
        self.assertEqual(hotkey, {"mods": MOD_CTRL | MOD_ALT, "vk": 0x56})
        self.assertEqual(hks.display(hotkey), "Ctrl+Alt+V")

    def test_configured_hotkey_is_kept_and_invalid_falls_back(self):
        custom = {"mods": MOD_CTRL | MOD_SHIFT, "vk": 0x56}
        self.assertEqual(cbm.clipboard_settings({"clipboard": {"overlay_hotkey": custom}})["overlay_hotkey"],
                         custom)
        for bad in ({"mods": 0, "vk": 0x56}, {"mods": 99, "vk": 0x56}, {"mods": 1, "vk": 1},
                    {"mods": 1}, "Ctrl+Alt+V", {"mods": True, "vk": 0x56}):
            self.assertEqual(cbm.clipboard_settings({"clipboard": {"overlay_hotkey": bad}})["overlay_hotkey"],
                             hks.DEFAULT_CLIPBOARD_OVERLAY_HOTKEY, bad)

    def test_default_dict_is_not_shared(self):
        first = cbm.clipboard_settings({})
        first["overlay_hotkey"]["vk"] = 0x41
        self.assertEqual(cbm.clipboard_settings({})["overlay_hotkey"]["vk"], 0x56)

    def test_web_api_rejects_invalid_or_wheel_conflicting_hotkey(self):
        self.assertIsNone(web_api._overlay_hotkey_error({"mods": MOD_CTRL | MOD_SHIFT, "vk": 0x56}, SPACE))
        self.assertEqual(web_api._overlay_hotkey_error({"mods": 0, "vk": 0x56}, SPACE)[0], "invalid_hotkey")
        code, message = web_api._overlay_hotkey_error(dict(SPACE), SPACE)
        self.assertEqual(code, "hotkey_conflict")
        self.assertIn("Ctrl+Alt+Space", message)


class HotkeyReportTests(unittest.TestCase):
    def _viktor_entries(self):
        # Log of 18.11.: Ctrl+Alt+V ok; Ctrl+Win+V, Win+V and the wheel fail with 1409.
        return [
            hks.make_entry(hks.SLOT_CLIPBOARD, hks.DEFAULT_CLIPBOARD_OVERLAY_HOTKEY, True),
            hks.make_entry(hks.SLOT_CLIPBOARD_CTRL_WIN_V, hks.CTRL_WIN_V, False, 1409),
            hks.make_entry(hks.SLOT_CLIPBOARD_WIN_V, hks.WIN_V, False, 1409),
            hks.make_entry(hks.SLOT_WHEEL, SPACE, False, 1409),
        ]

    def test_all_active_report_is_ok_without_notification(self):
        report = hks.build_report([hks.make_entry(hks.SLOT_CLIPBOARD,
                                                  hks.DEFAULT_CLIPBOARD_OVERLAY_HOTKEY, True)])
        self.assertTrue(report["ok"])
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(report["entries"][0]["message"], "Ctrl+Alt+V opens the clipboard overlay.")
        self.assertEqual(hks.notification_text(report), "")

    def test_busy_hotkeys_name_the_combination_and_what_works_instead(self):
        report = hks.build_report(self._viktor_entries())
        self.assertFalse(report["ok"])
        self.assertEqual(report["conflicts"], [hks.SLOT_CLIPBOARD_CTRL_WIN_V, hks.SLOT_CLIPBOARD_WIN_V,
                                               hks.SLOT_WHEEL])
        by_slot = {e["slot"]: e for e in report["entries"]}
        cwv = by_slot[hks.SLOT_CLIPBOARD_CTRL_WIN_V]
        self.assertEqual(cwv["error_code"], 1409)
        self.assertEqual(cwv["alternatives"], ["Ctrl+Alt+V", hks.CLIPBOARD_FALLBACK])
        self.assertIn("Ctrl+Win+V", cwv["message"])
        self.assertIn("already taken by Windows or another program", cwv["message"])
        self.assertIn("Use Ctrl+Alt+V", cwv["message"])
        wheel = by_slot[hks.SLOT_WHEEL]
        self.assertEqual(wheel["alternatives"], [hks.WHEEL_FALLBACK])
        self.assertIn("Ctrl+Alt+Space", wheel["message"])
        self.assertIn("Settings > Command Wheel", wheel["message"])
        self.assertIn("Ctrl+Win+V", report["summary"])
        text = hks.notification_text(report)
        self.assertLessEqual(len(text), 255)
        self.assertIn("Ctrl+Alt+Space (Command wheel) is taken, use Ctrl+Right click.", text)

    def test_other_errors_and_no_working_clipboard_hotkey(self):
        report = hks.build_report([
            hks.make_entry(hks.SLOT_CLIPBOARD, {"mods": MOD_WIN, "vk": 0x41}, False, 5)])
        entry = report["entries"][0]
        self.assertIn("Windows error 5", entry["message"])
        self.assertEqual(entry["alternatives"], [hks.CLIPBOARD_FALLBACK])

    def test_notification_is_truncated_to_balloon_limit(self):
        entries = [hks.make_entry(hks.SLOT_WHEEL, SPACE, False, 1409)] * 20
        self.assertEqual(len(hks.notification_text(hks.build_report(entries))), 255)


if __name__ == "__main__":
    unittest.main()
