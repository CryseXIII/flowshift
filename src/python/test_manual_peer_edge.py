"""Peer typed in by address (no device ID) and the screen-edge switch.

Viktor 26.11.: laptop has PC 2 typed in by IP (Ping ok), edge North -> PC 2,
nothing happens. The laptop calls PC 2 ``endpoint:<ip>:45781``; PC 2 compared
that with its own ``device:<id>`` and refused the ``edge_enter`` as a foreign
target. Covered: the edge assignment finds the typed-in peer, the target
recognises itself by address, the first ping/connection stores the device ID
(edges and hotkeys follow), and a dropped switch becomes a WebGUI notice.
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime_model as rm
from test_tray_remote_preview import _import_tray

LAPTOP_ID = "1aa0000000000001"
PC2_ID = "2bb0000000000002"
# Documentation addresses (RFC 5737).
PC2_IP = "192.0.2.40"
LAPTOP_IP = "192.0.2.23"
PC2_SCREEN = {"left": 0, "top": 0, "width": 2560, "height": 1440}


def laptop_config():
    """Laptop: PC 2 typed in by address, edge North -> PC 2, a hotkey for it."""
    endpoint = f"endpoint:{PC2_IP}:45781"
    return {
        "device_id": LAPTOP_ID, "device_name": "Laptop", "port": 45781,
        "peers": [{"name": PC2_IP, "host": PC2_IP, "port": 45781}],
        "display_layout": {"enabled": True, "edges": {
            "north": {"peer_identity": endpoint, "target_entry_edge": "south"},
            "south": None, "east": None, "west": None}},
        "hotkeys": [{"key": 0x70, "mods": 3, "action": rm.FORWARD_PREFIX + endpoint}],
    }


def pc2_config():
    """PC 2: found the laptop by scan (with device ID), South -> laptop."""
    return {
        "device_id": PC2_ID, "device_name": "PC2", "port": 45781,
        "peers": [{"name": "Laptop", "host": LAPTOP_IP, "port": 45781, "device_id": LAPTOP_ID}],
    }


class ManualPeerIdentityTest(unittest.TestCase):
    def test_edge_assignment_finds_the_typed_in_peer(self):
        cfg = laptop_config()
        peers = [rm.normalize_peer(p) for p in cfg["peers"]]
        layout, warnings = rm.normalize_display_layout(cfg["display_layout"], peers)
        self.assertEqual(warnings, [])
        ident = layout["edges"]["north"]["peer_identity"]
        peer = rm.find_peer_by_identity(ident, peers)
        self.assertEqual(peer["host"], PC2_IP)
        self.assertEqual(rm.canonical_peer_identity(peer), f"endpoint:{PC2_IP}:45781")

    def test_target_recognises_itself_by_address_and_port(self):
        cfg = pc2_config()
        self.assertTrue(rm.identity_is_self(f"endpoint:{PC2_IP}:45781", cfg, [PC2_IP, "10.8.0.5"]))
        self.assertTrue(rm.identity_is_self(f"device:{PC2_ID}", cfg, []))
        self.assertTrue(rm.identity_is_self("", cfg, []))
        # Another PC, another port or another device stays foreign.
        self.assertFalse(rm.identity_is_self(f"endpoint:{LAPTOP_IP}:45781", cfg, [PC2_IP]))
        self.assertFalse(rm.identity_is_self(f"endpoint:{PC2_IP}:45782", cfg, [PC2_IP]))
        self.assertFalse(rm.identity_is_self(f"device:{LAPTOP_ID}", cfg, [PC2_IP]))
        self.assertFalse(rm.identity_is_self("endpoint:garbage", cfg, [PC2_IP]))

    def test_first_answer_stores_device_id_screen_and_name_and_moves_edges(self):
        cfg = laptop_config()
        changed = rm.learn_peer_device(cfg, PC2_IP, 45781, PC2_ID.upper(), "PC2", PC2_SCREEN)
        self.assertEqual(changed, (f"endpoint:{PC2_IP}:45781", f"device:{PC2_ID}"))
        peer = cfg["peers"][0]
        self.assertEqual((peer["device_id"], peer["name"], peer["screen"]), (PC2_ID, "PC2", PC2_SCREEN))
        self.assertEqual(cfg["display_layout"]["edges"]["north"]["peer_identity"], f"device:{PC2_ID}")
        self.assertEqual(cfg["hotkeys"][0]["action"], rm.FORWARD_PREFIX + f"device:{PC2_ID}")
        # The edge still resolves after normalising, now by device ID.
        peers = [rm.normalize_peer(p) for p in cfg["peers"]]
        layout, warnings = rm.normalize_display_layout(cfg["display_layout"], peers)
        self.assertEqual((warnings, layout["edges"]["north"]["peer_identity"]), ([], f"device:{PC2_ID}"))
        # Second answer: nothing to do.
        self.assertIsNone(rm.learn_peer_device(cfg, PC2_IP, 45781, PC2_ID, "PC2"))

    def test_learning_keeps_a_chosen_name_and_refuses_doubles_and_self(self):
        cfg = laptop_config()
        cfg["peers"][0]["name"] = "Buero"
        rm.learn_peer_device(cfg, PC2_IP, 45781, PC2_ID, "PC2")
        self.assertEqual(cfg["peers"][0]["name"], "Buero")
        cfg = laptop_config()
        cfg["peers"].append({"name": "PC2", "host": "pc2.lan", "port": 45781, "device_id": PC2_ID})
        self.assertIsNone(rm.learn_peer_device(cfg, PC2_IP, 45781, PC2_ID))
        cfg = laptop_config()
        self.assertIsNone(rm.learn_peer_device(cfg, PC2_IP, 45781, LAPTOP_ID))
        self.assertIsNone(rm.learn_peer_device(cfg, PC2_IP, 45999, PC2_ID))
        self.assertNotIn("device_id", cfg["peers"][0])

    def test_failure_notices_name_edge_peer_and_cause(self):
        notice = rm.edge_failure_notice(rm.EDGE_FAILURE_REJECTED, "north", "PC2", "invalid_target", at=5)
        self.assertEqual((notice["edge"], notice["reason"], notice["at"]), ("north", "rejected", 5))
        self.assertIn("Edge north: PC2 refused the switch", notice["message"])
        self.assertIn("Ping the peer once", notice["message"])
        self.assertIn("no connection to PC2 (connection timeout)",
                      rm.edge_failure_notice(rm.EDGE_FAILURE_CONNECTION, "north", "PC2",
                                             "connection timeout")["message"])
        self.assertIn("is not in the peer list",
                      rm.edge_failure_notice(rm.EDGE_FAILURE_NO_PEER, "west", "", "device:gone")["message"])
        self.assertIn("did not answer within 3 seconds",
                      rm.edge_failure_notice(rm.EDGE_FAILURE_REJECTED, "east", "PC2", "timeout")["message"])


class TrayManualPeerEdgeTest(unittest.TestCase):
    """The productive tray paths, with the network replaced by stand-ins."""

    tray = None

    @classmethod
    def setUpClass(cls):
        cls.class_tmp = tempfile.TemporaryDirectory(prefix="fs-manual-edge-", ignore_cleanup_errors=True)
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
        self.saved = {"config": tray.istate.config, "edge_session": tray.istate.edge_session,
                      "active": tray.istate.active, "active_peer": tray.istate.active_peer,
                      "active_peer_label": tray.istate.active_peer_label}
        self.sent = []
        self.saved_configs = []
        for target, value in (
                ("_send_edge_message", lambda ident, msg: self.sent.append((ident, msg)) or True),
                ("_set_cursor_pos", lambda x, y: True),
                ("get_virtual_screen_spec", lambda: dict(PC2_SCREEN)),
                ("save_config", lambda cfg: self.saved_configs.append(copy.deepcopy(cfg))),
                ("log", lambda level, msg: None)):
            patcher = mock.patch.object(tray, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        tray.istate.edge_session = None
        tray._clear_edge_failure()
        self.addCleanup(self._restore)

    def _restore(self):
        tray = self.tray
        tray._clear_edge_session()
        tray._clear_edge_failure()
        tray.istate.set_clip(False)
        for key, value in self.saved.items():
            setattr(tray.istate, key, value)

    def edge_enter_from_laptop(self, target_identity):
        return {"type": "edge_enter", "session_id": "s1", "source_identity": f"device:{LAPTOP_ID}",
                "target_identity": target_identity, "source_exit_edge": "north",
                "target_entry_edge": "south", "orthogonal_position": 0.5}

    def test_pc2_accepts_edge_enter_for_its_address(self):
        tray = self.tray
        tray.istate.config = pc2_config()
        with mock.patch.object(tray, "get_local_ipv4s", lambda: [PC2_IP]):
            ok = tray._enter_target_edge_session_impl(self.edge_enter_from_laptop(f"endpoint:{PC2_IP}:45781"))
        self.assertTrue(ok)
        self.assertEqual(self.sent[-1][1]["type"], "edge_enter_ack")
        self.assertEqual(tray.istate.edge_session["target_identity"], f"device:{PC2_ID}")

    def test_pc2_still_refuses_another_pcs_address(self):
        tray = self.tray
        tray.istate.config = pc2_config()
        with mock.patch.object(tray, "get_local_ipv4s", lambda: [PC2_IP]):
            ok = tray._enter_target_edge_session_impl(self.edge_enter_from_laptop("endpoint:192.0.2.99:45781"))
        self.assertFalse(ok)
        self.assertEqual(self.sent[-1][1]["error"], "invalid_target")

    def test_laptop_shows_a_refused_switch_as_notice(self):
        tray = self.tray
        tray.istate.config = laptop_config()
        peer = rm.normalize_peer(tray.istate.config["peers"][0])
        with mock.patch.object(tray, "ensure_peer_connected", lambda p, timeout=3.0: (True, None)), \
                mock.patch.object(tray, "_await_edge_result",
                                  lambda sid, timeout: {"type": "edge_enter_reject", "ok": False,
                                                        "error": "invalid_target"}), \
                mock.patch.object(tray, "_peer_switch_block", lambda ident: None):
            ok = tray._start_source_edge_session_impl("north", peer, 0.5, "south", dict(PC2_SCREEN))
        self.assertFalse(ok)
        self.assertEqual(self.sent[0][1]["target_identity"], f"endpoint:{PC2_IP}:45781")
        notice = tray._edge_failure_snapshot()
        self.assertEqual((notice["reason"], notice["edge"], notice["detail"]), ("rejected", "north", "invalid_target"))
        self.assertIn("refused the switch", notice["message"])
        self.assertEqual(tray.build_status_snapshot()["edge_switching"]["last_failure"]["reason"], "rejected")

    def test_laptop_shows_a_failed_connection_as_notice(self):
        tray = self.tray
        tray.istate.config = laptop_config()
        peer = rm.normalize_peer(tray.istate.config["peers"][0])
        with mock.patch.object(tray, "ensure_peer_connected", lambda p, timeout=3.0: (False, "connection timeout")), \
                mock.patch.object(tray, "_peer_switch_block", lambda ident: None):
            self.assertFalse(tray._start_source_edge_session_impl("north", peer, 0.5, "south", dict(PC2_SCREEN)))
        self.assertEqual(tray._edge_failure_snapshot()["reason"], "connection_failed")

    def test_full_switch_by_address_after_learning(self):
        """Laptop -> PC 2 by address: learn the ID on the first answer, then switch."""
        tray = self.tray
        laptop = laptop_config()
        tray.istate.config = laptop
        tray._note_edge_failure(rm.EDGE_FAILURE_REJECTED, "north", "PC2", "invalid_target")
        changed = tray._learn_peer_device(PC2_IP, 45781, PC2_ID, "PC2", dict(PC2_SCREEN))
        self.assertEqual(changed[1], f"device:{PC2_ID}")
        self.assertEqual(self.saved_configs[-1]["peers"][0]["device_id"], PC2_ID)
        self.assertEqual(tray.istate.config["display_layout"]["edges"]["north"]["peer_identity"],
                         f"device:{PC2_ID}")
        peer = rm.normalize_peer(tray.istate.config["peers"][0])

        pc2 = pc2_config()

        def deliver_to_pc2(session_id, timeout):
            # The message the laptop sent, handled by PC 2's check.
            msg = self.sent[-1][1]
            self.assertTrue(rm.identity_is_self(msg["target_identity"], pc2, [PC2_IP]))
            return {"type": "edge_enter_ack", "ok": True}

        with mock.patch.object(tray, "ensure_peer_connected", lambda p, timeout=3.0: (True, None)), \
                mock.patch.object(tray, "_await_edge_result", deliver_to_pc2), \
                mock.patch.object(tray, "_peer_switch_block", lambda ident: None), \
                mock.patch.object(tray, "find_link_by_identity", lambda ident: None), \
                mock.patch.object(tray, "_hook_mgr", mock.MagicMock()), \
                mock.patch.object(tray, "_clip_mgr", mock.MagicMock()), \
                mock.patch.object(tray, "_current_cursor_pos", lambda: (0, 0), create=True), \
                mock.patch.object(tray, "_notify_fwd_state", lambda *a: None):
            ok = tray._start_source_edge_session_impl("north", peer, 0.5, "south", dict(PC2_SCREEN))
        self.assertTrue(ok)
        self.assertEqual(self.sent[-1][1]["target_identity"], f"device:{PC2_ID}")
        # A switch that happened clears the old notice.
        self.assertIsNone(tray._edge_failure_snapshot())


if __name__ == "__main__":
    unittest.main()
