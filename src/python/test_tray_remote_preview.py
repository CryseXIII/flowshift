"""Remote preview through the productive tray wiring.

Runs the real ``tray.peer_handler`` read loop on one end of a socket pair; the
test plays the peer on the other end. Source side: hotkey and screen-edge
switch -> ``preview_request``; a refusal shows its reason; ``preview_accept``
opens the preview window, every complete picture is shown and confirmed with
``preview_ack``; return, ``edge_cancel``, a dropped connection and shutdown
each hide the window and end in ``idle``. Target side: unpaired peers get
``pairing_required``, a switched-off setting ``preview_disabled``; a paired
peer gets ``preview_accept`` and pictures, which stop on ``preview_stop``,
``fwd_state`` inactive and a dropped connection. The window and the screen
capture are stand-ins (no desktop here).

``tray.py`` loads Windows ctypes APIs at import time. Off Windows the import
runs with a stand-in for ``ctypes.windll`` (restored right after); the paths
under test only reach it for cursor and clip calls, which are no-ops here.
"""
from __future__ import annotations

import base64
import ctypes
import os
import queue
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import platform_capabilities as pc
import preview_stream as ps
import remote_preview as rp
import runtime_model as rm

TRAY_ID = "aaaa1111"
PEER_ID = "bbbb2222"
PEER_IDENTITY = f"device:{PEER_ID}"
# Documentation address (RFC 5737): a loopback host would count as the local device.
PEER = {"name": "Peer", "host": "192.0.2.10", "port": 40001, "device_id": PEER_ID}
_MISSING = object()


def _import_tray():
    if "tray" in sys.modules or sys.platform == "win32":
        import tray
        return tray
    saved = {name: getattr(ctypes, name, _MISSING) for name in ("windll", "WINFUNCTYPE")}
    ctypes.windll = mock.MagicMock(name="windll")
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE
    try:
        import tray
    finally:
        for name, value in saved.items():
            if value is _MISSING:
                delattr(ctypes, name)
            else:
                setattr(ctypes, name, value)
    return tray


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    return predicate()


class _FakeWindow:
    """Stand-in for ``preview_window.PreviewWindow``: records what it was told."""

    def __init__(self):
        self.calls = []
        self.painted = 0
        self.last_error = ""

    def show(self, corner, width_px, area, title):
        self.calls.append(("show", corner, width_px, title))

    def frame(self, jpeg, width, height, title):
        self.painted += 1
        self.calls.append(("frame", jpeg, width, height))

    def notice(self, title):
        self.calls.append(("notice", title))

    def hide(self):
        self.calls.append(("hide",))

    def wait_painted(self, count, timeout):
        return self.painted >= count

    def close(self):
        self.calls.append(("close",))

    def kinds(self):
        return [c[0] for c in self.calls]


class _FakePeer:
    """The test's end of the socket pair: hello, then a framed read loop.

    Answers ``edge_enter`` with an ack and every ``preview_request`` with
    ``preview_reject(pairing_required)`` unless ``answer_requests`` is off.
    """

    def __init__(self, sock, preview_capability=True, answer_requests=True, stream_capability=True):
        self.sock = sock
        self.preview_capability = preview_capability
        self.stream_capability = stream_capability
        self.answer_requests = answer_requests
        self.received = queue.Queue()
        self.log = []
        self.lock = threading.Lock()
        self.thread = None

    def hello(self, preview_capability=None):
        offered = self.preview_capability if preview_capability is None else preview_capability
        capabilities = pc.default_capabilities("windows")
        capabilities[pc.REMOTE_PREVIEW] = offered
        capabilities[pc.REMOTE_PREVIEW_STREAM] = bool(offered and self.stream_capability)
        msg = pc.build_hello(PEER_ID, "Peer", None, "windows", "win32", "win32", capabilities,
                             port=PEER["port"])
        msg.update(app_version="test", git_commit="test", git_branch="test")
        return msg

    def start(self):
        rm.send_msg(self.sock, self.hello())
        self.thread = threading.Thread(target=self._loop, name="fake-peer", daemon=True)
        self.thread.start()

    def send(self, msg):
        with self.lock:
            rm.send_msg(self.sock, msg)

    def _loop(self):
        reader = rm.FramedReader(self.sock)
        try:
            while True:
                msg = reader.read_message(0.2)
                if msg is None:
                    continue
                self.log.append(msg)
                self.received.put(msg)
                kind = msg.get("type")
                if kind == "edge_enter":
                    self.send({"type": "edge_enter_ack", "session_id": msg["session_id"], "ok": True})
                elif kind == rp.PREVIEW_REQUEST and self.answer_requests:
                    self.send(rp.build_reject(msg["session_id"], rp.REASON_PAIRING_REQUIRED))
        except Exception:
            return

    def messages(self, kind):
        return [m for m in list(self.log) if m.get("type") == kind]

    def close(self):
        for op in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                op()
            except OSError:
                pass
        if self.thread:
            self.thread.join(3)


class TrayRemotePreviewTests(unittest.TestCase):
    tray = None

    @classmethod
    def setUpClass(cls):
        cls.class_tmp = tempfile.TemporaryDirectory(prefix="fs-tray-preview-",
                                                    ignore_cleanup_errors=True)
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
        self.saved = {"peers": tray.istate.peers, "config": tray.istate.config,
                      "active": tray.istate.active, "active_peer": tray.istate.active_peer,
                      "edge_session": tray.istate.edge_session}
        self.addCleanup(self._restore)
        self.logs = []
        self.window = _FakeWindow()
        self.screen = {"n": 0, "fail": None}
        for target, value in (("_hook_mgr", mock.MagicMock(running=False)),
                              ("_clip_mgr", mock.MagicMock()),
                              ("_set_cursor_pos", lambda x, y: True),
                              ("_preview_window", self.window),
                              ("_preview_window_error", ""),
                              ("_preview_send_check", (True, "")),
                              ("_cursor_pos", lambda: (10, 10)),
                              ("_monitor_rect_at", lambda x, y, work=False: {
                                  "left": 0, "top": 0, "width": 1920, "height": 1040}),
                              ("log", lambda level, msg: self.logs.append((level, msg)))):
            patcher = mock.patch.object(tray, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The target's screen: a new picture whenever the test bumps screen["n"].
        patcher = mock.patch.object(tray.preview_capture, "StreamCapture",
                                    lambda *a, **k: self.fake_capture)
        patcher.start()
        self.addCleanup(patcher.stop)
        tray.istate.peers = {}
        tray.istate.active = False
        tray.istate.active_peer = None
        tray.istate.edge_session = None
        tray.istate.config = dict(self.saved["config"])
        tray.istate.config.update({
            "device_id": TRAY_ID, "device_name": "Tray", "peers": [dict(PEER)],
            "remote_preview": {"enabled": True},
        })
        tray._preview_stop("test-setup", notify=False)
        self.handlers = []
        self.peers = []

    def _restore(self):
        tray = self.tray
        for peer in self.peers:
            peer.close()
        for thread in self.handlers:
            thread.join(5)
        stuck = [t.name for t in self.handlers if t.is_alive()]
        tray._preview_stop("test-teardown", notify=False)
        tray._preview_out_stop("test-teardown")
        tray._clear_edge_session()
        tray.istate.peers = self.saved["peers"]
        tray.istate.config = self.saved["config"]
        tray.istate.active = self.saved["active"]
        tray.istate.active_peer = self.saved["active_peer"]
        tray.istate.edge_session = self.saved["edge_session"]
        self.assertEqual(stuck, [], "peer_handler did not end")

    # ── fixture helpers ─────────────────────────────────────────────
    def fake_capture(self, previous_key):
        if self.screen["fail"]:
            error = RuntimeError(self.screen["fail"])
            error.code = self.screen["fail"]
            raise error
        key = f"screen-{self.screen['n']}"
        if key == previous_key:
            return None
        return {"key": key, "jpeg": key.encode() * 4000, "width": 960, "height": 540}

    def connect(self, **peer_kwargs):
        tray_end, peer_end = socket.socketpair()
        peer = _FakePeer(peer_end, **peer_kwargs)
        self.peers.append(peer)
        peer.start()
        thread = threading.Thread(target=self.tray.peer_handler,
                                  args=(tray_end, ("127.0.0.1", PEER["port"]), True),
                                  name="tray-peer-handler", daemon=True)
        thread.start()
        self.handlers.append(thread)
        self.assertTrue(wait_until(lambda: self.tray._slot_for_send(
            self.tray.find_link_by_identity(PEER_IDENTITY))), "tray never linked the peer")
        self.assertEqual(peer.received.get(timeout=5)["type"], "hello")
        return peer

    def status(self):
        snap = self.tray.build_status_snapshot()
        return snap["preview_state"], snap["preview_reason"]

    def wait_status(self, expected):
        wait_until(lambda: self.status() == expected)
        self.assertEqual(self.status(), expected)

    def switch_hotkey(self):
        ok, err = self.tray._activate_forward_peer_impl(dict(PEER), "hotkey")
        self.assertTrue(ok, err)

    def switch_edge(self):
        self.tray._set_edge_session_cooldown("source", 0)
        ok = self.tray._start_source_edge_session_impl("east", dict(PEER), 0.5,
                                                       source_screen={"left": 0, "top": 0,
                                                                      "width": 1920, "height": 1080})
        self.assertTrue(ok, self.logs[-5:])
        return self.tray._edge_session_active()["session_id"]

    def rejected_log_lines(self):
        return [m for _level, m in self.logs
                if m.startswith("remote_preview rejected reason=pairing_required")]

    # ── source side ─────────────────────────────────────────────────
    def test_hotkey_switch_is_rejected_then_connection_drop_returns_to_idle(self):
        peer = self.connect()
        self.assertEqual(self.status(), ("idle", ""))
        self.switch_hotkey()
        self.wait_status(("rejected", "pairing_required"))
        requests = peer.messages(rp.PREVIEW_REQUEST)
        self.assertEqual(len(requests), 1)
        self.assertEqual(rp.parse_message(requests[0])[0], True)
        self.assertEqual(peer.messages("fwd_state")[0]["active"], True)
        self.assertEqual(len(self.rejected_log_lines()), 1)
        self.assertTrue(self.tray.istate.active)   # forwarding itself is untouched
        # Connection drop: only the finally of peer_handler can reset it here.
        peer.close()
        self.handlers[0].join(5)
        self.assertFalse(self.handlers[0].is_alive())
        self.assertEqual(self.status(), ("idle", ""))

    def test_edge_switch_then_return_ends_in_idle(self):
        peer = self.connect()
        session_id = self.switch_edge()
        self.wait_status(("rejected", "pairing_required"))
        self.assertEqual(len(peer.messages(rp.PREVIEW_REQUEST)), 1)
        peer.send({"type": "edge_return", "session_id": session_id, "orthogonal_position": 0.5})
        self.wait_status(("idle", ""))
        self.assertFalse(self.tray.istate.active)
        self.assertIsNone(self.tray._edge_session_active())

    def test_edge_switch_then_cancel_ends_in_idle(self):
        peer = self.connect()
        session_id = self.switch_edge()
        self.wait_status(("rejected", "pairing_required"))
        peer.send({"type": "edge_cancel", "session_id": session_id, "reason": "peer-cancel"})
        self.wait_status(("idle", ""))
        self.assertFalse(self.tray.istate.active)

    def test_unanswered_request_is_stopped_on_return(self):
        peer = self.connect(answer_requests=False)
        self.switch_hotkey()
        self.wait_status(("requested", ""))
        session_id = wait_until(lambda: peer.messages(rp.PREVIEW_REQUEST))[0]["session_id"]
        self.tray.deactivate_forward("return_local")
        self.assertEqual(self.status(), ("idle", ""))
        self.assertTrue(wait_until(lambda: peer.messages(rp.PREVIEW_STOP)))
        self.assertEqual(peer.messages(rp.PREVIEW_STOP)[0]["session_id"], session_id)
        self.assertTrue(rp.parse_message(peer.messages(rp.PREVIEW_STOP)[0])[0])

    def test_accepted_preview_shows_and_confirms_pictures_until_the_return(self):
        peer = self.connect(answer_requests=False)
        self.switch_hotkey()
        self.wait_status(("requested", ""))
        session_id = wait_until(lambda: peer.messages(rp.PREVIEW_REQUEST))[0]["session_id"]
        peer.send(ps.build_accept(session_id, {"fps": 15, "max_width": 960}))
        self.wait_status(("streaming", ""))
        shown = [c for c in self.window.calls if c[0] == "show"]
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0][:3], ("show", "bottom_right", 480))
        self.assertIn("waiting for the first picture", shown[0][3])
        first = os.urandom(120 * 1024)             # 3 parts
        second = os.urandom(10 * 1024)
        for seq, jpeg in ((1, first), (2, second)):
            for msg in ps.frame_messages(session_id, seq, jpeg, 960, 540):
                peer.send(msg)
        self.assertTrue(wait_until(lambda: len(peer.messages(ps.PREVIEW_ACK)) == 2))
        self.assertEqual([m["seq"] for m in peer.messages(ps.PREVIEW_ACK)], [1, 2])
        frames = [c for c in self.window.calls if c[0] == "frame"]
        self.assertEqual(frames, [("frame", first, 960, 540), ("frame", second, 960, 540)])
        preview = self.tray.build_status_snapshot()["preview"]
        self.assertEqual((preview["state"], preview["peer"], preview["receiving"]["frames"]),
                         ("streaming", "Peer", 2))
        # The other PC's screen gets locked: the window says so, the session stays.
        peer.send(ps.build_status(session_id, ps.REASON_SCREEN_LOCKED))
        self.assertTrue(wait_until(lambda: self.window.kinds()[-1] == "notice"))
        self.assertIn("screen locked", self.window.calls[-1][1])
        self.assertIn("locked", self.tray.build_status_snapshot()["preview"]["notice"])
        # Return to this PC: window hidden, the other PC told to stop.
        self.tray.deactivate_forward("return_local")
        self.assertEqual(self.status(), ("idle", ""))
        self.assertEqual(self.window.kinds()[-1], "hide")
        self.assertTrue(wait_until(lambda: peer.messages(rp.PREVIEW_STOP)))
        self.assertEqual(peer.messages(rp.PREVIEW_STOP)[0]["session_id"], session_id)
        # A late picture of the ended session is neither shown nor confirmed.
        for msg in ps.frame_messages(session_id, 3, b"late", 4, 3):
            peer.send(msg)
        time.sleep(0.2)
        self.assertEqual(len(peer.messages(ps.PREVIEW_ACK)), 2)
        self.assertEqual(len([c for c in self.window.calls if c[0] == "frame"]), 2)

    def test_foreign_and_broken_pictures_are_not_shown(self):
        peer = self.connect(answer_requests=False)
        self.switch_hotkey()
        session_id = wait_until(lambda: peer.messages(rp.PREVIEW_REQUEST))[0]["session_id"]
        peer.send(ps.build_accept(session_id, {}))
        self.wait_status(("streaming", ""))
        for msg in ps.frame_messages("someone-else", 1, b"x" * 100, 4, 3):
            peer.send(msg)
        broken = ps.frame_messages(session_id, 1, b"y" * 100, 4, 3)[0]
        peer.send(dict(broken, crc=(broken["crc"] + 1) % 2 ** 32))
        peer.send(dict(broken, data="***"))
        good = ps.frame_messages(session_id, 2, b"z" * 100, 4, 3)
        peer.send(good[0])
        self.assertTrue(wait_until(lambda: peer.messages(ps.PREVIEW_ACK)))
        self.assertEqual([m["seq"] for m in peer.messages(ps.PREVIEW_ACK)], [2])
        self.assertEqual([c[1] for c in self.window.calls if c[0] == "frame"], [b"z" * 100])

    def test_older_target_is_named_and_window_trouble_is_honest(self):
        peer = self.connect(stream_capability=False, answer_requests=False)
        self.switch_hotkey()
        session_id = wait_until(lambda: peer.messages(rp.PREVIEW_REQUEST))[0]["session_id"]
        # What 0.7.6 and older answer.
        peer.send(rp.build_reject(session_id, rp.REASON_UNAVAILABLE))
        self.wait_status(("rejected", rp.REASON_UNAVAILABLE))
        preview = self.tray.build_status_snapshot()["preview"]
        self.assertIn("older FlowShift version", preview["reason_text"])
        self.assertTrue(any("without picture transfer (older FlowShift" in m for _l, m in self.logs))
        row = self.tray.build_status_snapshot()["peers"][0]
        self.assertEqual(row["remote_preview"]["code"], rp.REASON_PEER_TOO_OLD)
        self.tray.deactivate_forward("return_local")
        # No window possible on this PC: no request, a plain reason instead.
        with mock.patch.object(self.tray, "_preview_window", None), \
                mock.patch.object(self.tray, "_preview_window_error", "OSError: no desktop"):
            self.switch_hotkey()
            self.wait_status(("rejected", rp.REASON_WINDOW_UNAVAILABLE))
        self.assertEqual(len(peer.messages(rp.PREVIEW_REQUEST)), 1)

    def test_foreign_and_invalid_rejects_are_discarded(self):
        peer = self.connect(answer_requests=False)
        self.switch_hotkey()
        self.wait_status(("requested", ""))
        peer.send(rp.build_reject("someone-else", rp.REASON_PAIRING_REQUIRED))
        peer.send({"type": rp.PREVIEW_REJECT, "session_id": "", "reason": "x"})
        peer.send({"type": rp.PREVIEW_REJECT, "session_id": "a" * 200, "reason": "x"})
        peer.send({"type": "fwd_state", "active": True, "source_name": "Peer"})
        # The link survives malformed preview messages (the read loop keeps going).
        self.assertTrue(wait_until(lambda: (self.tray.find_link_by_identity(PEER_IDENTITY) or {})
                                   .get("remote_forwarding_active")))
        self.assertEqual(self.status(), ("requested", ""))

    def test_no_request_without_capability_or_when_disabled(self):
        peer = self.connect(preview_capability=False)
        self.switch_hotkey()
        time.sleep(0.2)
        self.assertEqual(peer.messages(rp.PREVIEW_REQUEST), [])
        self.assertEqual(self.status(), ("idle", ""))
        self.tray.deactivate_forward("return_local")
        # The peer switches the setting on: its repeated hello updates the capability.
        peer.send(peer.hello(preview_capability=True))
        self.assertTrue(wait_until(lambda: self.tray.find_link_by_identity(PEER_IDENTITY)
                                   ["capabilities"][pc.REMOTE_PREVIEW] is True))
        self.tray.istate.config["remote_preview"] = {"enabled": False}
        self.switch_hotkey()
        time.sleep(0.2)
        self.assertEqual(peer.messages(rp.PREVIEW_REQUEST), [])
        self.tray.deactivate_forward("return_local")
        self.tray.istate.config["remote_preview"] = {"enabled": True}
        self.switch_hotkey()
        self.wait_status(("rejected", "pairing_required"))

    def test_hello_advertises_capability_only_when_enabled(self):
        self.assertIs(self.tray.build_local_hello()["capabilities"][pc.REMOTE_PREVIEW], True)
        self.tray.istate.config["remote_preview"] = {"enabled": False}
        self.assertIs(self.tray.build_local_hello()["capabilities"][pc.REMOTE_PREVIEW], False)
        self.tray.istate.config.pop("remote_preview")
        self.assertIs(self.tray.build_local_hello()["capabilities"][pc.REMOTE_PREVIEW], False)

    def test_shutdown_returns_to_idle(self):
        self.connect()
        self.switch_hotkey()
        self.wait_status(("rejected", "pairing_required"))
        # Everything else request_shutdown tears down is stubbed, so the links
        # stay up and only its own preview stop can reset the state.
        stubs = {"_shutdown_requested": False, "_shutdown_event": threading.Event(),
                 "_file_previews": mock.MagicMock(), "_update_manager": None,
                 "_overlay_controller": None, "_wheel_hook": mock.MagicMock(),
                 "close_all_peer_connections": lambda reason: None,
                 "_cancel_active_edge_session": lambda *a, **k: None,
                 "release_injected_inputs": lambda reason: None,
                 "remove_tray": lambda: None, "signal_main_quit": lambda: None}
        patchers = [mock.patch.object(self.tray, name, value) for name, value in stubs.items()]
        patchers.append(mock.patch.object(self.tray.web_api, "shutdown_api_server", lambda: None))
        for patcher in patchers:
            patcher.start()
        try:
            self.tray.request_shutdown("test")
            self.assertEqual(self.status(), ("idle", ""))
            self.assertIsNotNone(self.tray.find_link_by_identity(PEER_IDENTITY))
        finally:
            for patcher in reversed(patchers):
                patcher.stop()

    # ── target side ─────────────────────────────────────────────────
    def test_target_refuses_unpaired_switched_off_and_invalid_requests(self):
        # Entry without device ID (typed in by address, not yet filled in): not
        # paired, whatever device_id the hello claims.
        unpaired = {k: v for k, v in PEER.items() if k != "device_id"}
        self.tray.istate.config["peers"] = [unpaired]
        peer = self.connect()
        peer.send(rp.build_request("req-1", {"enabled": True}))
        reply = peer.received.get(timeout=5)
        self.assertEqual(reply, rp.build_reject("req-1", "pairing_required"))
        # Paired (device ID in the device list), but the setting is off here.
        self.tray.istate.config["peers"] = [dict(PEER)]
        self.tray.istate.config["remote_preview"] = {"enabled": False}
        peer.send(rp.build_request("req-2", {}))
        reply = peer.received.get(timeout=5)
        self.assertEqual(reply, rp.build_reject("req-2", rp.REASON_DISABLED))
        # Paired and on, but this PC cannot take pictures (no Pillow, not Windows).
        self.tray.istate.config["remote_preview"] = {"enabled": True}
        with mock.patch.object(self.tray, "_preview_send_check", (False, "Pillow missing")):
            peer.send(rp.build_request("req-2b", {}))
            self.assertEqual(peer.received.get(timeout=5),
                             rp.build_reject("req-2b", rp.REASON_CAPTURE_UNAVAILABLE))
        self.tray.istate.config["remote_preview"] = {"enabled": False}
        # Invalid request with a usable id: refused as invalid; unusable id: dropped.
        bad = rp.build_request("req-3", {})
        bad["fps"] = 999
        peer.send(bad)
        self.assertEqual(peer.received.get(timeout=5),
                         rp.build_reject("req-3", rp.REASON_INVALID_REQUEST))
        peer.send({"type": rp.PREVIEW_REQUEST, "session_id": ""})
        peer.send(rp.build_request("req-4", {}))
        self.assertEqual(peer.received.get(timeout=5)["session_id"], "req-4")
        # No preview session or forwarding state on the target.
        self.assertEqual(self.status(), ("idle", ""))
        self.assertFalse(self.tray.istate.active)

    # ── target side: sending pictures ───────────────────────────────
    def start_stream(self, peer, session_id="req-1", **settings):
        peer.send(rp.build_request(session_id, dict({"fps": 30}, **settings)))
        reply = wait_until(lambda: [m for m in list(peer.log) if m.get("session_id") == session_id
                                    and m.get("type") in (rp.PREVIEW_ACCEPT, rp.PREVIEW_REJECT)])
        self.assertEqual(reply, [ps.build_accept(session_id, {"fps": 30, "max_width": 960})])
        return session_id

    def frames(self, peer):
        return [m for m in list(peer.log) if m.get("type") == ps.PREVIEW_FRAME]

    def test_target_sends_pictures_with_flow_control_until_preview_stop(self):
        peer = self.connect()
        session_id = self.start_stream(peer)
        # Without acks only WINDOW pictures go out, however often the screen changes.
        for n in range(1, 6):
            self.screen["n"] = n
            time.sleep(0.05)
        self.assertEqual(len({m["seq"] for m in self.frames(peer)}), ps.WINDOW)
        self.assertGreater(self.tray.build_status_snapshot()["preview"]["sending"]["skipped_slow"], 0)
        # An ack frees the window: the newest screen comes next.
        peer.send(ps.build_ack(session_id, 2))
        self.assertTrue(wait_until(lambda: len({m["seq"] for m in self.frames(peer)}) == 3))
        newest = [m for m in self.frames(peer) if m["seq"] == 3]
        self.assertTrue(base64.b64decode(newest[0]["data"]).startswith(b"screen-5"))
        # Unchanged screen after the ack: nothing more is sent.
        peer.send(ps.build_ack(session_id, 3))
        time.sleep(0.2)
        self.assertEqual(len({m["seq"] for m in self.frames(peer)}), 3)
        sending = self.tray.build_status_snapshot()["preview"]["sending"]
        self.assertEqual((sending["peer"], sending["sent"]), ("Peer", 3))
        self.assertGreater(sending["skipped_same"], 0)
        # The controlling PC returns: preview_stop ends the stream.
        peer.send(rp.build_stop(session_id))
        self.assertTrue(wait_until(lambda: self.tray._preview_out["streamer"] is None))
        count = len(self.frames(peer))
        self.screen["n"] = 99
        time.sleep(0.2)
        self.assertEqual(len(self.frames(peer)), count)
        self.assertTrue(any(m.startswith("remote_preview sending to Peer stopped (peer-stopped)")
                            for _l, m in self.logs))

    def test_target_stops_on_fwd_state_inactive_and_on_disconnect(self):
        peer = self.connect()
        self.start_stream(peer)
        self.assertTrue(wait_until(lambda: self.frames(peer)))
        peer.send({"type": "fwd_state", "active": False, "source_name": "Peer"})
        self.assertTrue(wait_until(lambda: self.tray._preview_out["streamer"] is None))
        self.start_stream(peer, "req-2")
        streamer = wait_until(lambda: self.tray._preview_out["streamer"])
        peer.close()
        self.handlers[0].join(5)
        self.assertIsNone(self.tray._preview_out["streamer"])
        self.assertTrue(streamer.stopped)

    def test_target_reports_a_locked_screen_and_refuses_a_second_pc(self):
        peer = self.connect()
        self.screen["fail"] = ps.REASON_SCREEN_LOCKED
        session_id = self.start_stream(peer)
        status = wait_until(lambda: peer.messages(ps.PREVIEW_STATUS))
        self.assertEqual(status[0], ps.build_status(session_id, ps.REASON_SCREEN_LOCKED))
        self.assertEqual(self.frames(peer), [])
        # Another PC asks while this one is streamed to: busy.
        with mock.patch.dict(self.tray._preview_out, {"identity": "device:cccc3333"}):
            peer.send(rp.build_request("req-9", {}))
            reply = wait_until(lambda: [m for m in peer.log if m.get("session_id") == "req-9"])
        self.assertEqual(reply[0], rp.build_reject("req-9", rp.REASON_BUSY))
        self.screen["fail"] = None
        self.assertTrue(wait_until(lambda: self.frames(peer), timeout=3))
        self.assertEqual(peer.messages(ps.PREVIEW_STATUS)[-1], ps.build_status(session_id, ""))


if __name__ == "__main__":
    unittest.main()
