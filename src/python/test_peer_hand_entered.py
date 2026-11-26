"""A peer typed in by address (no device_id), as on Viktor's two PCs (26.11.).

Covered: the device ID and the announced name are filled in at the first link
or ping and edge, hotkeys and identity move from ``endpoint:<host>:<port>`` to
``device:<id>`` (also at start for older entries); the source sends the real
identity of the other PC in ``edge_enter``; the target still accepts its own
address and port from an older source; a refused edge is kept for the
Dashboard; the update from a connected PC and the preview count the entry as
paired once the device ID is in; flowshift.log is rotated at its size limit.
"""
from __future__ import annotations

import copy
import os
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import peer_update
import remote_preview as rp
import runtime_model as rm
from test_tray_remote_preview import PEER_ID, _FakePeer, _import_tray, wait_until

HOST = "192.168.8.18"
PORT = 45781
ENDPOINT = f"endpoint:{HOST}:{PORT}"
DEVICE = f"device:{PEER_ID}"


def viktor_config():
    """Viktor's config: Laptop-Bettina typed in by IP, edge north, Ctrl+Alt+1/0."""
    peer = {"name": "Laptop-Bettina", "host": HOST, "port": PORT, "listen_port": PORT,
            "display_name": "Laptop-Bettina", "identity": ENDPOINT}
    return {
        "device_id": "f2176177", "device_name": "PC-Viktor", "port": PORT,
        "peers": [peer],
        "display_layout": {"enabled": True, "edges": {
            "north": {"peer_identity": ENDPOINT, "target_entry_edge": "south"},
            "south": None, "east": None, "west": None}},
        "hotkeys": [
            {"action": rm.FORWARD_PREFIX + ENDPOINT, "mods": rm.MOD_CTRL | rm.MOD_ALT,
             "key": 0x31, "label": "Forward to Laptop-Bettina"},
            {"action": rm.RETURN_ACTION, "mods": rm.MOD_CTRL | rm.MOD_ALT,
             "key": 0x30, "label": "Return to local"},
        ],
    }


class AdoptDeviceIdTests(unittest.TestCase):
    def test_link_fills_in_device_id_and_moves_edge_and_hotkeys(self):
        cfg = viktor_config()
        new = rm.adopt_peer_device_id(cfg, HOST, {PORT}, PEER_ID.upper(), "LAPTOP-K85O4NIN")
        self.assertEqual(new, DEVICE)
        peer = cfg["peers"][0]
        self.assertEqual(peer["device_id"], PEER_ID)
        self.assertEqual(peer["identity"], DEVICE)
        self.assertEqual(peer["display_name"], "LAPTOP-K85O4NIN")
        self.assertEqual(peer["name"], "Laptop-Bettina")        # own label stays
        self.assertEqual(cfg["display_layout"]["edges"]["north"],
                         {"peer_identity": DEVICE, "target_entry_edge": "south"})
        self.assertEqual(cfg["hotkeys"][0]["action"], rm.FORWARD_PREFIX + DEVICE)
        self.assertEqual(cfg["hotkeys"][1]["action"], rm.RETURN_ACTION)
        # The normal passes keep it: no second hotkey, edge resolves, label kept.
        rm.sync_hotkeys(cfg)
        self.assertEqual([h["key"] for h in cfg["hotkeys"]], [0x31, 0x30])
        self.assertEqual(cfg["hotkeys"][0]["label"], "Forward to Laptop-Bettina")
        layout, warnings = rm.normalize_display_layout(cfg["display_layout"], cfg["peers"])
        self.assertEqual(warnings, [])
        self.assertEqual(layout["edges"]["north"]["peer_identity"], DEVICE)
        self.assertIs(rm.resolve_peer_by_action(cfg, cfg["hotkeys"][0]["action"]), peer)
        # Second time: nothing left to change.
        self.assertIsNone(rm.adopt_peer_device_id(cfg, HOST, {PORT}, PEER_ID, "LAPTOP-K85O4NIN"))

    def test_entry_of_another_pc_or_address_is_left_alone(self):
        cfg = viktor_config()
        cfg["peers"][0]["device_id"] = "99887766"
        before = copy.deepcopy(cfg)
        self.assertIsNone(rm.adopt_peer_device_id(cfg, HOST, {PORT}, PEER_ID))
        self.assertEqual(cfg, before)
        cfg = viktor_config()
        before = copy.deepcopy(cfg)
        self.assertIsNone(rm.adopt_peer_device_id(cfg, "192.168.8.19", {PORT}, PEER_ID))
        self.assertIsNone(rm.adopt_peer_device_id(cfg, HOST, {40000}, PEER_ID))
        self.assertIsNone(rm.adopt_peer_device_id(cfg, HOST, {PORT}, ""))
        self.assertEqual(cfg, before)
        # A second entry (found by scan) already holds that device ID elsewhere.
        cfg["peers"].append({"name": "Scan", "host": "10.0.0.7", "port": PORT, "device_id": PEER_ID})
        self.assertIsNone(rm.adopt_peer_device_id(cfg, HOST, {PORT}, PEER_ID))

    def test_start_migrates_entries_that_have_a_device_id_but_old_references(self):
        cfg = viktor_config()
        cfg["peers"][0]["device_id"] = PEER_ID       # filled in, identity still endpoint
        self.assertTrue(rm.migrate_peer_identities(cfg))
        self.assertEqual(cfg["peers"][0]["identity"], DEVICE)
        self.assertEqual(cfg["display_layout"]["edges"]["north"]["peer_identity"], DEVICE)
        self.assertEqual(cfg["hotkeys"][0]["action"], rm.FORWARD_PREFIX + DEVICE)
        self.assertFalse(rm.migrate_peer_identities(cfg))
        untouched = viktor_config()                   # no device ID: nothing to do
        self.assertFalse(rm.migrate_peer_identities(untouched))
        self.assertEqual(untouched, viktor_config())

    def test_paired_for_update_and_preview_once_the_device_id_is_in(self):
        cfg = viktor_config()
        self.assertIsNone(peer_update.trusted_peer(cfg["peers"], DEVICE, cfg["device_id"]))
        meta = {"identity": DEVICE, "device_id": PEER_ID, "host": HOST}
        self.assertEqual(rp.preview_authorization(
            meta, peer_update.trusted_peer(cfg["peers"], DEVICE, cfg["device_id"])),
            (False, rp.REASON_PAIRING_REQUIRED))
        rm.adopt_peer_device_id(cfg, HOST, {PORT}, PEER_ID, "LAPTOP-K85O4NIN")
        trusted = peer_update.trusted_peer(cfg["peers"], DEVICE, cfg["device_id"])
        self.assertIs(trusted, cfg["peers"][0])
        self.assertEqual(rp.preview_authorization(meta, trusted), (True, ""))


class EdgeTargetMatchTests(unittest.TestCase):
    def test_own_identity_device_id_and_own_address(self):
        own = DEVICE
        self.assertTrue(rm.edge_target_matches(DEVICE, own, PEER_ID, PORT, [HOST]))
        self.assertTrue(rm.edge_target_matches(DEVICE.upper(), "", PEER_ID, PORT, []))
        # Older source: the address typed in there.
        self.assertTrue(rm.edge_target_matches(ENDPOINT, own, PEER_ID, PORT, [HOST]))
        self.assertTrue(rm.edge_target_matches(f"endpoint:127.0.0.1:{PORT}", own, PEER_ID, PORT, []))

    def test_foreign_targets_are_refused(self):
        own = DEVICE
        for target in ("device:99887766", f"endpoint:{HOST}:40000",
                       f"endpoint:192.168.8.19:{PORT}", "name:Laptop", "endpoint:", "",
                       f"endpoint:{HOST}:notaport"):
            self.assertFalse(rm.edge_target_matches(target, own, PEER_ID, PORT, [HOST]), target)


class LogRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "flowshift.log")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_rotates_at_the_limit_and_keeps_three_backups(self):
        self.write(self.path, "a" * 50)
        self.assertFalse(rm.rotate_log_file(self.path, max_bytes=100, backups=3))
        self.assertFalse(os.path.exists(self.path + ".1"))
        for generation in "12345":
            self.write(self.path, generation * 100)
            self.assertTrue(rm.rotate_log_file(self.path, max_bytes=100, backups=3))
            self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.read(self.path + ".1"), "5" * 100)
        self.assertEqual(self.read(self.path + ".2"), "4" * 100)
        self.assertEqual(self.read(self.path + ".3"), "3" * 100)
        self.assertFalse(os.path.exists(self.path + ".4"))

    def test_missing_file_and_no_backups(self):
        self.assertFalse(rm.rotate_log_file(self.path, max_bytes=1))
        self.write(self.path, "x" * 10)
        self.assertTrue(rm.rotate_log_file(self.path, max_bytes=5, backups=0))
        self.assertEqual(self.read(self.path), "")



class _EdgeTarget(_FakePeer):
    """Fake other PC that checks ``edge_enter`` like ``tray`` does on the target."""

    def __init__(self, sock, reject_with=None):
        super().__init__(sock)
        self.reject_with = reject_with

    def _loop(self):
        reader = rm.FramedReader(self.sock)
        try:
            while True:
                msg = reader.read_message(0.2)
                if msg is None:
                    continue
                self.log.append(msg)
                self.received.put(msg)
                if msg.get("type") != "edge_enter":
                    continue
                ok = not self.reject_with and rm.edge_target_matches(
                    msg.get("target_identity"), DEVICE, PEER_ID, PORT, [HOST])
                reply = {"type": "edge_enter_ack" if ok else "edge_enter_reject",
                         "session_id": msg["session_id"], "ok": ok}
                if not ok:
                    reply["error"] = self.reject_with or "invalid_target"
                self.send(reply)
        except Exception:
            return


class TrayHandEnteredPeerTests(unittest.TestCase):
    tray = None

    @classmethod
    def setUpClass(cls):
        cls.class_tmp = tempfile.TemporaryDirectory(prefix="fs-tray-hand-", ignore_cleanup_errors=True)
        cls.old_env = {k: os.environ.get(k) for k in ("FLOWSHIFT_LOG_DIR", "FLOWSHIFT_CONFIG")}
        os.environ["FLOWSHIFT_LOG_DIR"] = cls.class_tmp.name
        os.environ["FLOWSHIFT_CONFIG"] = os.path.join(cls.class_tmp.name, "config.json")
        cls.tray = _import_tray()
        cls.real_log = staticmethod(cls.tray.log)

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.class_tmp.cleanup()

    def test_tray_log_stays_under_the_limit(self):
        path = os.path.join(self.class_tmp.name, "rotation", "flowshift.log")
        os.makedirs(os.path.dirname(path))
        with mock.patch.object(self.tray, "LOG_FILE", path), \
                mock.patch.object(rm, "LOG_MAX_BYTES", 2000), \
                mock.patch("builtins.print"):
            for n in range(203):
                self.real_log("INFO", f"line {n:04d} " + "x" * 40)
        self.assertLess(os.path.getsize(path), 2000)
        for backup in (".1", ".2", ".3"):
            self.assertLess(os.path.getsize(path + backup), 2000 + 200)
        self.assertFalse(os.path.exists(path + ".4"))
        with open(path, encoding="utf-8") as f:
            self.assertIn("line 0202", f.read())

    def setUp(self):
        tray = self.tray
        self.saved = {"peers": tray.istate.peers, "config": tray.istate.config,
                      "hotkeys": tray.istate.hotkeys, "active": tray.istate.active,
                      "active_peer": tray.istate.active_peer,
                      "edge_session": tray.istate.edge_session}
        self.addCleanup(self._restore)
        self.logs = []
        self.config_file = os.path.join(self.class_tmp.name, "config.json")
        for target, value in (("_hook_mgr", mock.MagicMock(running=False)),
                              ("_clip_mgr", mock.MagicMock()),
                              ("_set_cursor_pos", lambda x, y: True),
                              ("CONFIG_FILE", self.config_file),
                              ("log", lambda level, msg: self.logs.append((level, msg)))):
            patcher = mock.patch.object(tray, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        tray.istate.peers = {}
        tray.istate.active = False
        tray.istate.active_peer = None
        tray.istate.edge_session = None
        tray.istate.config = viktor_config()
        tray._note_edge_rejection(None, None, None)
        self.handlers = []
        self.fakes = []

    def _restore(self):
        tray = self.tray
        for fake in self.fakes:
            fake.close()
        for thread in self.handlers:
            thread.join(5)
        tray._clear_edge_session()
        tray._note_edge_rejection(None, None, None)
        for key, value in self.saved.items():
            setattr(tray.istate, key, value)

    def connect_outbound(self, **kwargs):
        """The tray dials the address typed in; the other PC answers with its hello."""
        tray_end, peer_end = socket.socketpair()
        fake = _EdgeTarget(peer_end, **kwargs)
        self.fakes.append(fake)
        fake.start()
        thread = threading.Thread(target=self.tray.peer_handler,
                                  args=(tray_end, (HOST, PORT), False),
                                  kwargs={"dial_host": HOST, "dial_port": PORT},
                                  name="tray-peer-handler", daemon=True)
        thread.start()
        self.handlers.append(thread)
        self.assertTrue(wait_until(lambda: self.tray._slot_for_send(
            self.tray.find_link_by_identity(DEVICE))), "tray never linked the peer")
        self.assertEqual(fake.received.get(timeout=5)["type"], "hello")
        return fake

    def edge_north(self):
        self.tray._set_edge_session_cooldown("source", 0)
        peer = self.tray.find_peer_by_identity(
            self.tray.istate.config["display_layout"]["edges"]["north"]["peer_identity"],
            self.tray.istate.config["peers"])
        self.assertIsNotNone(peer)
        return self.tray._start_source_edge_session_impl(
            "north", dict(peer), 0.5, target_entry_edge="south",
            source_screen={"left": 0, "top": 0, "width": 1920, "height": 1080})

    def test_first_link_fills_in_the_entry_and_the_north_edge_switches(self):
        fake = self.connect_outbound()
        cfg = self.tray.istate.config
        self.assertTrue(wait_until(lambda: cfg["peers"][0].get("device_id") == PEER_ID))
        self.assertEqual(cfg["peers"][0]["identity"], DEVICE)
        self.assertEqual(cfg["display_layout"]["edges"]["north"]["peer_identity"], DEVICE)
        self.assertEqual(cfg["hotkeys"][0]["action"], rm.FORWARD_PREFIX + DEVICE)
        self.assertEqual([hk.action for hk in self.tray.istate.hotkeys],
                         [rm.FORWARD_PREFIX + DEVICE, rm.RETURN_ACTION])
        with open(self.config_file, encoding="utf-8") as f:
            self.assertIn(DEVICE, f.read())
        self.assertTrue(self.edge_north(), self.logs[-5:])
        enter = fake.messages("edge_enter")[-1]
        self.assertEqual(enter["target_identity"], DEVICE)
        self.assertEqual(self.tray._edge_session_active()["role"], "source")
        self.assertIsNone(self.tray.build_status_snapshot()["edge_switching"]["last_rejection"])

    def test_source_sends_the_device_id_even_if_the_entry_is_not_filled_in(self):
        fake = self.connect_outbound()
        # As if storing had failed: the entry still says endpoint:..., the link knows better.
        self.tray.istate.config = viktor_config()
        self.assertTrue(self.edge_north(), self.logs[-5:])
        self.assertEqual(fake.messages("edge_enter")[-1]["target_identity"], DEVICE)

    def test_refused_edge_is_shown_on_the_dashboard(self):
        self.connect_outbound(reject_with="invalid_target")
        self.assertFalse(self.edge_north())
        rejection = self.tray.build_status_snapshot()["edge_switching"]["last_rejection"]
        self.assertEqual(rejection["reason"], "invalid_target")
        self.assertEqual(rejection["peer"], "Laptop-Bettina")
        self.assertEqual(rejection["edge"], "north")

    def test_target_accepts_its_own_address_from_an_older_source(self):
        tray = self.tray
        tray.istate.config = {"device_id": PEER_ID, "device_name": "LAPTOP-K85O4NIN",
                              "port": PORT, "peers": [], "display_layout": rm.default_display_layout()}
        sent = []
        with mock.patch.object(tray, "_send_edge_message",
                               lambda identity, msg: sent.append((identity, msg)) or True):
            msg = {"type": "edge_enter", "session_id": "s1", "source_identity": "device:f2176177",
                   "target_identity": ENDPOINT, "source_exit_edge": "north",
                   "target_entry_edge": "south", "orthogonal_position": 0.5}
            self.assertTrue(tray._enter_target_edge_session_impl(msg, local_host=HOST))
            self.assertEqual(sent[-1][1]["type"], "edge_enter_ack")
            self.assertEqual(tray._edge_session_active()["target_identity"], DEVICE)
            tray._clear_edge_session()
            foreign = dict(msg, session_id="s2", target_identity="endpoint:192.168.8.99:45781")
            self.assertFalse(tray._enter_target_edge_session_impl(foreign, local_host=HOST))
            self.assertEqual(sent[-1][1]["type"], "edge_enter_reject")
            self.assertEqual(sent[-1][1]["error"], "invalid_target")


if __name__ == "__main__":
    unittest.main()
