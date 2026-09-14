"""End-to-end stream V2 file transfer through the productive tray wiring.

Side T is the real ``tray`` module: its ``peer_handler`` (hello handshake,
framed read loop, ``clipboard_*`` dispatch and the ``T_CHANNEL_HELLO`` hand-off
to ``accept_stream_v2_channel``), ``install_peer_connection`` /
``select_clipboard_transfer_strategy`` negotiation, ``_clip_send`` and
``_clip_open_channel``. Side P is a second ``ClipboardManager`` speaking the
same wire protocol over real localhost TCP sockets in both directions.

Windows-only: ``tray.py`` imports Windows ctypes APIs at module import time.
"""
from __future__ import annotations

import hashlib
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_flow_control_v2 as cfc2
import clipboard_model as cbm
import clipboard_streaming_v2 as cstr2
import clipboard_transfer_control_v2 as cctl2
import clipboard_transport_v2 as ctv2
import platform_capabilities as pc
import runtime_model as rm
from clipboard_runtime import ClipboardManager

MIB = 1024 * 1024
TRAY_ID = "aaaa1111"
PEER_ID = "bbbb2222"
TRAY_IDENTITY = f"device:{TRAY_ID}"
PEER_IDENTITY = f"device:{PEER_ID}"
FAST = cctl2.TransferTimeouts(preflight=8, manifest_ack=8, window_ack=8, no_progress=8,
                              reconnect_wait=15, final_complete_ack=8)
LEGACY_TYPES = {"clipboard_transfer_chunk", "clipboard_transfer_start"}


def _raw_settings():
    return {"enabled": True, "sync_on_activate": True, "history_max_items": 999,
            "history_max_total_gb": 10.0, "max_auto_transfer_mb": 100}


def wait_until(predicate, timeout=20.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
        except Exception:
            value = False
        if value:
            return value
        time.sleep(interval)
    return predicate()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_sources(root, spec):
    paths = []
    for rel, payload in spec.items():
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        paths.append(str(path))
    return paths


def _sessions(manager, direction):
    return [rec["stage"] for rec in manager._stream_v2_sessions.values()
            if rec["direction"] == direction]


def _shutdown_socket(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class _Peer:
    """Side P: a ClipboardManager wired to the tray over real TCP sockets.

    * ``listener`` accepts the tray's outbound peer link (hello exchange, then a
      framed read loop dispatching ``clipboard_*`` into ``manager.handle``) and
      the tray's dialled stream_v2 data channels (handed to
      ``accept_stream_v2_channel`` exactly like ``tray.peer_handler`` does).
    * ``dial_tray`` opens the control link into the tray listener (tray inbound).
    * ``send_fn`` writes framed JSON on the dialled control link.
    * ``open_channel_fn`` dials the tray listener with a channel hello.
    """

    def __init__(self, root, tray_port):
        self.root = root
        self.tray_port = tray_port
        self.lock = threading.Lock()
        self.received = []          # every message the tray sent to P (both links)
        self.sent = []              # every message P sent to the tray
        self.logs = []
        self.channel_socks = []     # accepted data-channel sockets (P receiver side)
        self.stop = threading.Event()
        self.threads = []
        self.control_sock = None    # P -> tray (tray inbound slot)
        self.inbound_sock = None    # tray -> P (tray outbound slot)
        self.inbound_ready = threading.Event()
        self.settings = cbm.clipboard_settings({"clipboard": _raw_settings()})

        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]

        self.manager = ClipboardManager(os.path.join(root, "clipboard"), PEER_ID, self._send,
                                        lambda: self.settings, log_fn=self._log,
                                        open_channel_fn=self._open_channel)
        self.manager.stream_v2_timeouts = lambda: FAST
        self.manager.set_peer_transfer_strategy(TRAY_IDENTITY, "stream_v2")
        self._spawn(self._accept_loop, "peer-accept")

    # ── plumbing ────────────────────────────────────────────────────
    def _spawn(self, target, name, *args):
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()
        self.threads.append(thread)
        return thread

    def _log(self, level, msg):
        with self.lock:
            self.logs.append((level, msg))

    def hello(self):
        capabilities = pc.default_capabilities("windows")
        capabilities[pc.CLIPBOARD_STREAM_V2] = True
        msg = pc.build_hello(PEER_ID, "Peer", None, "windows", pc.detect_desktop("windows"),
                             pc.detect_input_backend("windows"), capabilities, port=self.port)
        msg["app_version"] = "test"
        msg["git_commit"] = "test"
        msg["git_branch"] = "test"
        return msg

    def _send(self, identity, msg):
        assert identity == TRAY_IDENTITY, identity
        with self.lock:
            self.sent.append(msg)
            sock = self.control_sock
        if sock is None:
            raise ConnectionError("peer control link is down")
        rm.send_msg(sock, msg)

    def _open_channel(self, identity, hello, timeout):
        assert identity == TRAY_IDENTITY, identity

        def _connect(connect_timeout):
            sock = socket.create_connection(("127.0.0.1", self.tray_port),
                                            timeout=min(10.0, float(connect_timeout)))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return sock

        return ctv2.open_channel_socket(_connect, hello, timeout)

    def _read_loop(self, sock, label):
        reader = rm.FramedReader(sock)
        try:
            while not self.stop.is_set():
                msg = reader.read_message(0.5)
                if msg is None:
                    continue
                with self.lock:
                    self.received.append(msg)
                if str(msg.get("type", "")).startswith("clipboard_"):
                    try:
                        self.manager.handle(TRAY_IDENTITY, msg)
                    except Exception as exc:  # pragma: no cover - diagnostics
                        self._log("ERROR", f"{label} handle error: {exc!r}")
        except Exception:
            pass

    def _accept_loop(self):
        while not self.stop.is_set():
            try:
                conn, addr = self.listener.accept()
            except OSError:
                return
            self._spawn(self._serve, f"peer-serve-{addr[1]}", conn)

    def _serve(self, conn):
        reader = rm.FramedReader(conn)
        try:
            first = None
            deadline = time.monotonic() + 5.0
            while first is None and time.monotonic() < deadline and not self.stop.is_set():
                first = reader.read_message(0.5)
        except Exception:
            first = None
        if not first:
            conn.close()
            return
        if first.get("type") == ctv2.T_CHANNEL_HELLO:
            with self.lock:
                self.channel_socks.append(conn)
            if not self.manager.accept_stream_v2_channel(conn, first, leftover=bytes(reader._buf)):
                self._log("WARN", "channel hello refused")
            return
        if first.get("type") != "hello":
            conn.close()
            return
        with self.lock:
            self.received.append(first)
        rm.send_msg(conn, self.hello())
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.inbound_sock = conn
        self.inbound_ready.set()
        # Continue with the same reader so nothing buffered is lost.
        try:
            while not self.stop.is_set():
                msg = reader.read_message(0.5)
                if msg is None:
                    continue
                with self.lock:
                    self.received.append(msg)
                if str(msg.get("type", "")).startswith("clipboard_"):
                    try:
                        self.manager.handle(TRAY_IDENTITY, msg)
                    except Exception as exc:  # pragma: no cover - diagnostics
                        self._log("ERROR", f"inbound handle error: {exc!r}")
        except Exception:
            pass

    def dial_tray(self):
        """Open P's control link into the tray listener and complete the hello exchange."""
        sock = socket.create_connection(("127.0.0.1", self.tray_port), timeout=5.0)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        rm.send_msg(sock, self.hello())
        sock.settimeout(5.0)
        reply = rm.recv_msg(sock)
        assert reply.get("type") == "hello", reply
        sock.settimeout(None)
        with self.lock:
            self.received.append(reply)
            self.control_sock = sock
        self._spawn(self._read_loop, "peer-control-reader", sock, "control")
        return reply

    def messages(self, msg_type):
        with self.lock:
            return [m for m in self.received if m.get("type") == msg_type]

    def break_channels(self):
        with self.lock:
            socks = list(self.channel_socks)
            self.channel_socks.clear()
        for sock in socks:
            _shutdown_socket(sock)

    def close(self):
        self.stop.set()
        try:
            self.manager.shutdown(timeout=3)
        except Exception:
            pass
        for sock in (self.control_sock, self.inbound_sock):
            if sock is not None:
                _shutdown_socket(sock)
        _shutdown_socket(self.listener)
        self.break_channels()
        for thread in self.threads:
            thread.join(5)


@unittest.skipUnless(sys.platform == "win32", "tray.py imports Windows ctypes APIs")
class TrayStreamV2E2ETests(unittest.TestCase):
    tray = None

    @classmethod
    def setUpClass(cls):
        cls.class_tmp = tempfile.TemporaryDirectory(prefix="fs-tray-e2e-", ignore_cleanup_errors=True)
        cls.old_env = {k: os.environ.get(k) for k in ("FLOWSHIFT_LOG_DIR", "FLOWSHIFT_CONFIG")}
        os.environ["FLOWSHIFT_LOG_DIR"] = cls.class_tmp.name
        os.environ["FLOWSHIFT_CONFIG"] = os.path.join(cls.class_tmp.name, "config.json")
        import tray as productive_tray
        cls.tray = productive_tray

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.class_tmp.cleanup()

    # ── fixture ─────────────────────────────────────────────────────
    def setUp(self):
        tray = self.tray
        self.temp = tempfile.TemporaryDirectory(prefix="fs-tray-e2e-case-", ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.src = os.path.join(self.root, "src")
        os.makedirs(self.src)

        self.saved = {
            "peers": tray.istate.peers,
            "config": tray.istate.config,
            "clip_mgr": tray._clip_mgr,
        }
        self.addCleanup(self._restore_tray)

        # Tray listener (the productive `peer_handler` serves every accepted socket).
        self.tray_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tray_listener.bind(("127.0.0.1", 0))
        self.tray_listener.listen(8)
        self.tray_port = self.tray_listener.getsockname()[1]
        self.handler_threads = []
        self.listener_stop = threading.Event()

        self.peer = _Peer(os.path.join(self.root, "peer"), self.tray_port)

        tray.istate.peers = {}
        tray.istate.config = dict(self.saved["config"])
        tray.istate.config.update({
            "device_id": TRAY_ID,
            "device_name": "Tray",
            "port": self.tray_port,
            "peers": [{"name": "Peer", "host": "127.0.0.1", "port": self.peer.port,
                       "device_id": PEER_ID}],
            "clipboard": _raw_settings(),
        })

        # Recording spy around the productive `_clip_send` (installed before the
        # manager is built; the module global itself is left untouched).
        self.tray_sent = []
        productive_send = tray._clip_send

        def recording_send(identity, msg):
            self.tray_sent.append((identity, msg))
            return productive_send(identity, msg)

        # Mirror tray.py's own construction of `_clip_mgr` (tray.py:1708) with a
        # temp store root and the configured device id (tray.py:1944).
        self.T = ClipboardManager(os.path.join(self.root, "tray", "clipboard"), TRAY_ID,
                                  recording_send, tray._clip_settings, tray.log,
                                  open_channel_fn=tray._clip_open_channel)
        self.T.stream_v2_timeouts = lambda: FAST
        tray._clip_mgr = self.T

        self.accept_thread = threading.Thread(target=self._tray_accept_loop,
                                              name="tray-accept", daemon=True)
        self.accept_thread.start()

        # Direction 1: P dials the tray (tray inbound slot via peer_handler(is_server=True)).
        self.peer.dial_tray()
        # Direction 2: the tray dials P exactly like connect_one() does
        # (peer_handler(is_server=False, dial_host, dial_port, stop_event)).
        self.outbound_stop = threading.Event()
        self.outbound_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.outbound_sock.settimeout(3.0)
        self.outbound_sock.connect(("127.0.0.1", self.peer.port))
        tray._set_tcp_nodelay(self.outbound_sock)
        self.outbound_thread = threading.Thread(
            target=tray.peer_handler,
            args=(self.outbound_sock, ("127.0.0.1", self.peer.port), False),
            kwargs={"dial_host": "127.0.0.1", "dial_port": self.peer.port,
                    "stop_event": self.outbound_stop},
            name="tray-outbound", daemon=True)
        self.outbound_thread.start()

        link = wait_until(lambda: self._link() if (self._link() or {}).get("inbound")
                          and (self._link() or {}).get("outbound") else None, timeout=10)
        self.assertTrue(link, f"tray never linked both directions: {tray.istate.peers}")
        self.assertTrue(self.peer.inbound_ready.wait(5))
        self.assertEqual(link["identity"], PEER_IDENTITY)
        self.assertEqual(link["device_id"], PEER_ID)
        self.assertEqual(link["listen_port"], self.peer.port)
        self.assertEqual(link["clipboard_transfer_strategy"], "stream_v2")
        self.assertEqual(link["inbound"]["clipboard_transfer_strategy"], "stream_v2")
        self.assertEqual(link["outbound"]["clipboard_transfer_strategy"], "stream_v2")
        self.assertIs(link["outbound"]["conn"], self.outbound_sock)
        # Both hellos advertised the capability.
        tray_hellos = self.peer.messages("hello")
        self.assertEqual(len(tray_hellos), 2)
        for hello in tray_hellos:
            self.assertEqual(hello["device_id"], TRAY_ID)
            self.assertIs(hello["capabilities"][pc.CLIPBOARD_STREAM_V2], True)
        # install_peer_connection notified the tray-side manager exactly once.
        self.assertIn(PEER_ID, self.T._providers)
        self.assertEqual(self.T._providers[PEER_ID]["identity"], PEER_IDENTITY)
        self.peer.manager.on_peer_connected(TRAY_ID, TRAY_IDENTITY)

    def _link(self):
        return self.tray.find_link_by_identity(PEER_IDENTITY)

    def _tray_accept_loop(self):
        while not self.listener_stop.is_set():
            try:
                conn, addr = self.tray_listener.accept()
            except OSError:
                return
            thread = threading.Thread(target=self.tray.peer_handler, args=(conn, addr, True),
                                      name=f"tray-inbound-{addr[1]}", daemon=True)
            thread.start()
            self.handler_threads.append(thread)

    def _restore_tray(self):
        tray = self.tray
        self.peer.close()
        try:
            self.T.shutdown(timeout=3)
        except Exception:
            pass
        self.outbound_stop.set()
        self.listener_stop.set()
        _shutdown_socket(self.tray_listener)
        _shutdown_socket(self.outbound_sock)
        for link in list(tray.istate.peers.values()):
            for direction in ("inbound", "outbound"):
                slot = link.get(direction)
                if slot:
                    _shutdown_socket(slot["conn"])
        self.accept_thread.join(5)
        self.outbound_thread.join(5)
        for thread in self.handler_threads:
            thread.join(5)
        stuck = [t.name for t in [self.accept_thread, self.outbound_thread, *self.handler_threads]
                 if t.is_alive()]
        tray._clip_mgr = self.saved["clip_mgr"]
        tray.istate.config = self.saved["config"]
        tray.istate.peers = self.saved["peers"]
        self.assertEqual(stuck, [], "tray threads did not terminate")

    # ── helpers ─────────────────────────────────────────────────────
    def _spec(self):
        return {
            "batch/big.bin": os.urandom(3 * MIB + 4321),
            "batch/small.txt": b"hello from the productive tray\n",
            "batch/nested/deep/leaf.dat": bytes(range(256)) * 700,
        }

    def _capture(self, sender, receiver_identity, spec):
        make_sources(self.src, spec)
        item = sender.capture_files(receiver_identity, [os.path.join(self.src, "batch")])
        self.assertIsNotNone(item)
        sender.on_profile_activated(receiver_identity)
        return item

    def _assert_completed(self, sender, receiver, sender_identity, item, spec):
        item_id = item["item_id"]
        store = receiver.store(sender_identity)
        self.assertTrue(wait_until(lambda: (store.get_item(item_id) or {}).get(
            "payload", {}).get("encoding") == "object_manifest_v2", timeout=30))
        received = store.get_item(item_id)
        self.assertTrue(received["available"])
        self.assertEqual(received["payload_state"], "cached")
        self.assertEqual(received["hash_state"], "verified")
        expected = {rel.split("batch/", 1)[1]: sha(data) for rel, data in spec.items()}
        got = {e["path"].split("batch/", 1)[1]: e["sha256"]
               for e in received["batch_manifest"]["entries"] if e["type"] == "file"}
        self.assertEqual(got, expected)

        outgoing = _sessions(sender, "outgoing")
        self.assertEqual(len(outgoing), 1)
        session = outgoing[0]
        self.assertTrue(wait_until(lambda: session.state == "completed"))
        self.assertEqual(session.journal.state, "completed")
        self.assertEqual(session.bytes_done, item["size"])
        self.assertEqual(session.finalized_manifest_digest,
                         received["batch_manifest"]["manifest_digest"])

        journal = receiver.stream_v2_journal_store().load("incoming", session.transfer_id)
        self.assertEqual(journal.state, "completed")
        incoming_root = store.object_store_v2.incoming_root
        self.assertFalse(os.path.lexists(os.path.join(incoming_root, session.transfer_id)))
        parts = [os.path.join(b, f) for b, _d, fs in os.walk(incoming_root) for f in fs
                 if f.endswith(".part")]
        self.assertEqual(parts, [])
        self.assertTrue(wait_until(lambda: not sender.transfer_activity_state()["busy"]))
        self.assertFalse(receiver.transfer_activity_state()["busy"])
        self.assertTrue(wait_until(lambda: sender.stats["sent_items"] == 1))
        self.assertEqual(receiver.stats["received_items"], 1)

        dest = os.path.join(self.root, "dest")
        result = receiver.materialize_files_result(sender_identity, item_id, dest)
        self.assertTrue(result.get("ok"), result)
        self.assertIn(result.get("strategy"), ("hardlink", "copy"), result)
        for rel, payload in spec.items():
            found = [os.path.join(b, f) for b, _d, fs in os.walk(dest) for f in fs
                     if os.path.relpath(os.path.join(b, f), dest).replace(os.sep, "/").endswith(rel)]
            self.assertEqual(len(found), 1, f"{rel} not materialized exactly once: {found}")
            self.assertEqual(sha(Path(found[0]).read_bytes()), sha(payload), rel)
        return session

    def _assert_no_legacy_wire(self):
        peer_types = {m.get("type") for m in self.peer.received}
        tray_types = {m.get("type") for _i, m in self.tray_sent}
        self.assertEqual(peer_types & LEGACY_TYPES, set())
        self.assertEqual(tray_types & LEGACY_TYPES, set())
        self.assertTrue(all(identity == PEER_IDENTITY for identity, _m in self.tray_sent))

    # ── tests ───────────────────────────────────────────────────────
    def test_tray_sends_file_batch_to_peer_over_localhost(self):
        spec = self._spec()
        item = self._capture(self.T, PEER_IDENTITY, spec)
        session = self._assert_completed(self.T, self.peer.manager, TRAY_IDENTITY, item, spec)
        # Wire evidence: the offer travelled through the productive _clip_send
        # on the tray's outbound slot and P accepted over its control link.
        self.assertEqual(len(self.peer.messages(ctv2.T_OFFER)), 1)
        self.assertEqual(self.peer.messages(ctv2.T_OFFER)[0]["transfer_id"], session.transfer_id)
        self.assertEqual(len([m for m in self.peer.sent if m.get("type") == ctv2.T_ACCEPT]), 1)
        self.assertEqual(len(self.peer.channel_socks), 1)
        self.assertEqual(session.status()["retry_count"], 0)
        self._assert_no_legacy_wire()
        self.assertTrue(any("stream_v2 channel open" in msg for _l, msg in self.peer.logs))

    def test_peer_sends_file_batch_to_tray_over_localhost(self):
        spec = self._spec()
        item = self._capture(self.peer.manager, TRAY_IDENTITY, spec)
        session = self._assert_completed(self.peer.manager, self.T, PEER_IDENTITY, item, spec)
        # The offer reached the tray-side manager through peer_handler ->
        # _clip_mgr.handle; the accept came back through the productive _clip_send.
        offers = [m for m in self.peer.sent if m.get("type") == ctv2.T_OFFER]
        self.assertEqual(len(offers), 1)
        accepts = [m for _i, m in self.tray_sent if m.get("type") == ctv2.T_ACCEPT]
        self.assertEqual(len(accepts), 1)
        self.assertEqual(accepts[0]["transfer_id"], session.transfer_id)
        # The data channel was dialled into the tray listener and handed off by
        # peer_handler to accept_stream_v2_channel: the channel socket never
        # became a peer link and the handler thread for it has finished.
        self.assertEqual(self.peer.channel_socks, [])
        self.assertEqual(set(self.tray.istate.peers), {PEER_IDENTITY})
        self.assertTrue(wait_until(lambda: sum(1 for t in self.handler_threads if t.is_alive()) == 1))
        self.assertEqual(session.status()["retry_count"], 0)
        self._assert_no_legacy_wire()

    def test_link_drop_mid_transfer_resumes_over_localhost(self):
        payload = os.urandom(7 * MIB + 777)
        spec = {"batch/large.bin": payload, "batch/tail.txt": b"after the big one"}
        self.T._stream_v2_flow = cfc2.FlowControlCoordinator(cfc2.FlowControlLimits(
            chunk_size=MIB, window_ack_timeout_seconds=FAST.window_ack))
        original_accept = cstr2.IncomingTransferStage.accept

        def slow_accept(self_stage, chunk, **kwargs):
            time.sleep(0.12)
            return original_accept(self_stage, chunk, **kwargs)

        P = self.peer.manager
        with mock.patch.object(cstr2.IncomingTransferStage, "accept", slow_accept):
            item = self._capture(self.T, PEER_IDENTITY, spec)
            self.assertTrue(wait_until(
                lambda: _sessions(P, "incoming")
                and MIB <= _sessions(P, "incoming")[0].bytes_done < 5 * MIB))
            receiver = _sessions(P, "incoming")[0]
            sender = _sessions(self.T, "outgoing")[0]
            self.assertEqual(len(self.peer.channel_socks), 1)
            self.peer.break_channels()
            self.assertTrue(wait_until(lambda: sender.state in (
                "waiting_reconnect", "transferring", "completed")))
            self.assertTrue(wait_until(lambda: receiver.journal is not None and receiver.journal.state in (
                "waiting_reconnect", "transferring", "finalizing", "completed")))
            self.assertTrue(wait_until(lambda: sender.state == "completed", timeout=45))
        self._assert_completed(self.T, P, TRAY_IDENTITY, item, spec)
        self.assertEqual(sender.status()["retry_count"], 1)
        self.assertGreater(sender.resume_bytes, 0)
        self.assertLess(sender.resume_bytes, len(payload))
        # The second channel was dialled by the productive _clip_open_channel.
        self.assertEqual(len(self.peer.channel_socks), 1)
        resume_requests = [m for _i, m in self.tray_sent if m.get("type") == ctv2.T_RESUME_REQUEST]
        self.assertEqual(len(resume_requests), 1)
        response = [m for m in self.peer.sent if m.get("type") == ctv2.T_RESUME_RESPONSE][0]
        self.assertEqual(sum(f["durable_offset"] for f in response["files"]), sender.resume_bytes)
        journal = P.stream_v2_journal_store().load("incoming", sender.transfer_id)
        self.assertEqual(journal.state, "completed")
        self.assertEqual(journal.retry_count, 1)
        self._assert_no_legacy_wire()


if __name__ == "__main__":
    unittest.main()
