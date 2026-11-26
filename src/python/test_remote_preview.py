"""Remote preview stage 1: settings, message checks, session lifecycle and the lock."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import platform_capabilities as pc
import remote_preview as rp


def _request(**overrides):
    msg = {"type": rp.PREVIEW_REQUEST, "session_id": "abc123", "fps": 15,
           "max_width": 960, "monitor": "cursor", "cursor_crop": False}
    msg.update(overrides)
    return msg


class SettingsTests(unittest.TestCase):
    def test_defaults_for_missing_or_wrong_container(self):
        for raw in (None, {}, [], "on", 5):
            self.assertEqual(rp.normalize_settings(raw), rp.DEFAULT_SETTINGS)
        self.assertIs(rp.DEFAULT_SETTINGS["enabled"], False)

    def test_valid_values_are_kept(self):
        raw = {"enabled": True, "fps": 30, "max_width": 1920, "monitor": 2, "cursor_crop": True,
               "window": {"corner": "top_left", "width_px": 1200}}
        self.assertEqual(rp.normalize_settings(raw), raw)
        lower = {"fps": 5, "max_width": 480, "monitor": 0, "window": {"width_px": 200}}
        got = rp.normalize_settings(lower)
        self.assertEqual((got["fps"], got["max_width"], got["monitor"], got["window"]["width_px"]),
                         (5, 480, 0, 200))
        self.assertEqual(rp.normalize_settings({"monitor": "primary"})["monitor"], "primary")

    def test_out_of_range_and_wrong_types_fall_back(self):
        cases = {
            "enabled": ["true", 1, None],
            "fps": [4, 31, 15.0, "15", True],
            "max_width": [479, 1921, 960.5, "960"],
            "monitor": [-1, 64, "secondary", 1.0, None, True],
            "cursor_crop": ["false", 0, None],
        }
        for key, values in cases.items():
            for value in values:
                errors = []
                got = rp.normalize_settings({key: value}, errors)
                self.assertEqual(got[key], rp.DEFAULT_SETTINGS[key], (key, value))
                self.assertEqual(errors, [key], (key, value))

    def test_window_fallbacks(self):
        errors = []
        got = rp.normalize_settings({"window": {"corner": "middle", "width_px": 199, "x": 1}},
                                    errors)
        self.assertEqual(got["window"], rp.DEFAULT_SETTINGS["window"])
        self.assertEqual(sorted(errors), ["window.corner", "window.width_px", "window.x"])
        errors = []
        got = rp.normalize_settings({"window": "big"}, errors)
        self.assertEqual(got["window"], rp.DEFAULT_SETTINGS["window"])
        self.assertEqual(errors, ["window"])
        self.assertEqual(rp.normalize_settings({"window": {"width_px": 1201}})["window"]["width_px"],
                         480)

    def test_unknown_keys_dropped_and_reported(self):
        errors = []
        got = rp.normalize_settings({"enabled": True, "debug_stream": True}, errors)
        self.assertNotIn("debug_stream", got)
        self.assertIs(got["enabled"], True)
        self.assertEqual(errors, ["debug_stream"])
        errors = []
        rp.normalize_settings("x", errors)
        self.assertEqual(errors, ["remote_preview"])

    def test_result_is_a_copy(self):
        got = rp.normalize_settings({})
        got["window"]["width_px"] = 999
        self.assertEqual(rp.DEFAULT_SETTINGS["window"]["width_px"], 480)


class MessageTests(unittest.TestCase):
    def test_valid_request_keeps_only_known_fields(self):
        ok, fields = rp.parse_message(_request(extra="ignored", image=b"x".hex()))
        self.assertTrue(ok)
        self.assertEqual(fields, {"type": rp.PREVIEW_REQUEST, "session_id": "abc123", "fps": 15,
                                  "max_width": 960, "monitor": "cursor", "cursor_crop": False})

    def test_build_request_round_trip(self):
        msg = rp.build_request("s-1", {"enabled": True, "fps": 20, "monitor": 1})
        ok, fields = rp.parse_message(msg)
        self.assertTrue(ok)
        self.assertEqual((fields["fps"], fields["max_width"], fields["monitor"]), (20, 960, 1))

    def test_session_id_rules(self):
        for sid in ("", None, 5, "a" * 65, "has space", "bad\nline", "ümlaut"):
            ok, reason = rp.parse_message(_request(session_id=sid))
            self.assertFalse(ok, sid)
            self.assertEqual(reason, rp.REASON_INVALID_MESSAGE)
        self.assertTrue(rp.parse_message(_request(session_id="a" * 64))[0])

    def test_missing_and_wrong_fields_are_invalid_requests(self):
        for key in ("fps", "max_width", "monitor", "cursor_crop"):
            msg = _request()
            del msg[key]
            self.assertEqual(rp.parse_message(msg), (False, rp.REASON_INVALID_REQUEST), key)
        for key, value in (("fps", 4), ("fps", "15"), ("max_width", 5000), ("monitor", -2),
                           ("monitor", "all"), ("cursor_crop", "yes"), ("fps", True)):
            self.assertEqual(rp.parse_message(_request(**{key: value})),
                             (False, rp.REASON_INVALID_REQUEST), (key, value))

    def test_too_large_message(self):
        ok, reason = rp.parse_message(_request(padding="x" * rp.MAX_MESSAGE_BYTES))
        self.assertFalse(ok)
        self.assertEqual(reason, rp.REASON_INVALID_MESSAGE)

    def test_non_dict_and_unknown_type(self):
        for msg in (None, [], "preview_request", {"type": "preview_image", "session_id": "a"}):
            self.assertEqual(rp.parse_message(msg), (False, rp.REASON_INVALID_MESSAGE))

    def test_reject_and_stop_reasons(self):
        ok, fields = rp.parse_message(rp.build_reject("s1", rp.REASON_PAIRING_REQUIRED))
        self.assertTrue(ok)
        self.assertEqual(fields["reason"], "pairing_required")
        for reason in (None, "", "Bad Reason", "x" * 65, 3):
            self.assertFalse(rp.parse_message(rp.build_reject("s1", reason))[0], reason)
        ok, fields = rp.parse_message({"type": rp.PREVIEW_STOP, "session_id": "s1"})
        self.assertEqual((ok, fields["reason"]), (True, rp.REASON_STOPPED))
        ok, fields = rp.parse_message(rp.build_stop("s1", "<script>"))
        self.assertEqual((ok, fields["reason"]), (True, rp.REASON_STOPPED))

    def test_accept_needs_numbers_in_range(self):
        msg = {"type": rp.PREVIEW_ACCEPT, "session_id": "s1", "fps": 10, "max_width": 640}
        self.assertEqual(rp.parse_message(msg)[0], True)
        msg["fps"] = 100
        self.assertEqual(rp.parse_message(msg), (False, rp.REASON_INVALID_REQUEST))

    def test_never_raises_on_odd_input(self):
        nested = {"type": rp.PREVIEW_STOP, "session_id": "s1"}
        node = nested
        for _ in range(2000):
            node["n"] = {}
            node = node["n"]
        self.assertEqual(rp.parse_message(nested)[0], False)
        self.assertEqual(rp.parse_message({"type": rp.PREVIEW_STOP, "session_id": "s1",
                                           "x": object()})[0], False)


class SessionTests(unittest.TestCase):
    def test_full_lifecycle_through_streaming(self):
        s = rp.PreviewSession()
        self.assertEqual(s.snapshot(), {"state": "idle", "reason": ""})
        self.assertTrue(s.request("s1"))
        self.assertEqual(s.state, rp.REQUESTED)
        self.assertTrue(s.accept("s1"))
        self.assertEqual(s.state, rp.STREAMING)
        self.assertEqual(s.stop(), "s1")
        self.assertEqual(s.state, rp.STOPPING)
        self.assertTrue(s.stopped())
        self.assertEqual(s.snapshot(), {"state": "idle", "reason": ""})
        self.assertIsNone(s.session_id)

    def test_reject_shows_rejected_until_stop(self):
        s = rp.PreviewSession()
        s.request("s1")
        self.assertTrue(s.reject("s1", rp.REASON_PAIRING_REQUIRED))
        self.assertEqual(s.state, rp.IDLE)
        self.assertEqual(s.snapshot(), {"state": "rejected", "reason": "pairing_required"})
        self.assertIsNone(s.stop())       # nothing to tell the peer
        self.assertFalse(s.stopped())
        self.assertEqual(s.snapshot(), {"state": "idle", "reason": ""})

    def test_duplicate_request_and_foreign_ids_are_discarded(self):
        s = rp.PreviewSession()
        self.assertTrue(s.request("s1"))
        self.assertFalse(s.request("s2"))
        self.assertFalse(s.request("s1"))
        self.assertFalse(s.accept("s2"))
        self.assertFalse(s.reject("s2", "pairing_required"))
        self.assertIsNone(s.stop("s2"))
        self.assertEqual((s.state, s.session_id), (rp.REQUESTED, "s1"))
        self.assertFalse(s.request(""))
        self.assertFalse(s.request("x" * 65))

    def test_transitions_out_of_order(self):
        s = rp.PreviewSession()
        self.assertFalse(s.accept("s1"))
        self.assertFalse(s.reject("s1", "pairing_required"))
        self.assertFalse(s.stopped())
        s.request("s1")
        s.accept("s1")
        self.assertFalse(s.accept("s1"))
        self.assertFalse(s.reject("s1", "pairing_required"))
        self.assertFalse(s.request("s2"))
        s.stop()
        self.assertFalse(s.request("s3"))   # stopping: no new request before idle
        self.assertIsNone(s.stop())         # second stop while stopping
        self.assertEqual(s.state, rp.STOPPING)
        self.assertTrue(s.stopped())
        self.assertTrue(s.request("s3"))

    def test_stop_from_every_state(self):
        for prepare, expect_id in (
                (lambda s: None, None),
                (lambda s: s.request("s1"), "s1"),
                (lambda s: (s.request("s1"), s.accept("s1")), "s1"),
                (lambda s: (s.request("s1"), s.stop()), None),
                (lambda s: (s.request("s1"), s.reject("s1", "pairing_required")), None)):
            s = rp.PreviewSession()
            prepare(s)
            self.assertEqual(s.stop(), expect_id)
            s.stopped()
            self.assertEqual(s.snapshot(), {"state": "idle", "reason": ""})

    def test_peer_stop_with_matching_id(self):
        s = rp.PreviewSession()
        s.request("s1")
        self.assertEqual(s.stop("s1"), "s1")
        self.assertTrue(s.stopped())

    def test_invalid_reject_reason_is_sanitised(self):
        s = rp.PreviewSession()
        s.request("s1")
        s.reject("s1", "Not A Token")
        self.assertEqual(s.snapshot()["reason"], rp.REASON_INVALID_MESSAGE)


class LockTests(unittest.TestCase):
    def test_every_request_is_refused_with_pairing_required(self):
        metas = (
            None, {},
            {"identity": "device:1122aabb", "device_id": "1122aabb", "host": "127.0.0.1"},
            {"device_id": "1122aabb", "enabled": True, "paired": True, "trusted": True},
            {"host": "192.168.1.50", "remote_preview": {"enabled": True}},
        )
        for meta in metas:
            self.assertEqual(rp.preview_authorization(meta), (False, "pairing_required"), meta)

    def test_paired_only_with_the_device_id_in_the_own_device_list(self):
        meta = {"identity": "device:1122aabb", "device_id": "1122aabb", "host": "192.0.2.5"}
        entry = {"name": "Laptop", "host": "192.0.2.5", "port": 45781, "device_id": "1122AABB"}
        self.assertEqual(rp.preview_authorization(meta, entry), (True, ""))
        # Entry typed in by address, device ID not filled in yet: not paired.
        typed_in = {"name": "Laptop", "host": "192.0.2.5", "port": 45781}
        self.assertEqual(rp.preview_authorization(meta, typed_in), (False, "pairing_required"))
        # Link known only by address or name, or another device ID: not paired.
        for identity in ("endpoint:192.0.2.5:45781", "name:Laptop", "device:99887766"):
            self.assertEqual(rp.preview_authorization(dict(meta, identity=identity), entry),
                             (False, "pairing_required"), identity)

    def test_no_image_path_in_module(self):
        # Stage 1 must not offer an image: no accept builder, no frame/channel API.
        for name in dir(rp):
            lowered = name.lower()
            self.assertFalse(any(word in lowered for word in ("frame", "capture", "encode",
                                                              "channel", "build_accept")), name)


class CapabilityTests(unittest.TestCase):
    def test_capability_key_and_old_peers(self):
        self.assertIn(pc.REMOTE_PREVIEW, pc.CAPABILITY_KEYS)
        old = pc.parse_hello({"type": "hello", "os": "windows",
                              "capabilities": {"keyboard_capture": True}})
        self.assertIs(old["capabilities"][pc.REMOTE_PREVIEW], False)
        legacy = pc.parse_hello({"type": "hello", "os": "windows"})
        self.assertIs(legacy["capabilities"][pc.REMOTE_PREVIEW], False)
        new = pc.parse_hello({"type": "hello", "os": "windows",
                              "capabilities": {pc.REMOTE_PREVIEW: True}})
        self.assertIs(new["capabilities"][pc.REMOTE_PREVIEW], True)
        self.assertIs(pc.parse_hello({"capabilities": {pc.REMOTE_PREVIEW: "yes"}})
                      ["capabilities"][pc.REMOTE_PREVIEW], False)


if __name__ == "__main__":
    unittest.main()
