"""Productive stream_v2 transport activation tests (Phase 3, slice 12).

Two real ``ClipboardManager`` instances are paired at the transport-message
level: control messages travel through per-manager dispatcher threads (like
the peer reader thread in ``tray.peer_handler``) and the dedicated data channel
is a real ``socket.socketpair`` whose accept side runs the same
``accept_stream_v2_channel`` handshake the tray performs. No productive code
path is mocked away.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_flow_control_v2 as cfc2
import clipboard_framing_v2 as cfr2
import clipboard_manifest_v2 as cman2
import clipboard_model as cbm
import clipboard_resume_v2 as cres2
import clipboard_streaming_v2 as cstr2
import clipboard_transfer as ctt
import clipboard_transfer_control_v2 as cctl2
import clipboard_transport_v2 as ctv2
import runtime_model as rm
from clipboard_runtime import ClipboardManager

MIB = 1024 * 1024
FAST = cctl2.TransferTimeouts(preflight=8, manifest_ack=8, window_ack=8, no_progress=8,
                              reconnect_wait=15, final_complete_ack=8)


def _settings(**overrides):
    raw = {"enabled": True, "sync_on_activate": True, "history_max_items": 999,
           "history_max_total_gb": 10.0, "max_auto_transfer_mb": 100}
    raw.update(overrides)
    return cbm.clipboard_settings({"clipboard": raw})


def wait_until(predicate, timeout=15.0, interval=0.01):
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


class Dispatcher:
    """Serial message delivery thread for one manager (models the peer reader)."""

    def __init__(self, manager, name):
        self.manager = manager
        self.queue = queue.Queue()
        self.dropped = 0
        self.thread = threading.Thread(target=self._run, name=f"dispatch-{name}", daemon=True)
        self.thread.start()

    def _run(self):
        while True:
            item = self.queue.get()
            if item is None:
                return
            identity, msg = item
            try:
                self.manager.handle(identity, msg)
            except Exception as exc:  # pragma: no cover - test diagnostics
                print("dispatch error", exc)

    def stop(self):
        self.queue.put(None)
        self.thread.join(5)


class Pair:
    """Manager A (sender) and B (receiver) wired at the transport level."""

    def __init__(self, tmp, *, settings_a=None, settings_b=None, timeouts=FAST,
                 limits=None, strategy_a="stream_v2"):
        self.tmp = tmp
        self.link_up = True
        self.channels = []
        self.sent = {"A": [], "B": []}
        self.lock = threading.Lock()
        self.logs = []
        self.timeouts = timeouts
        self.strategy = strategy_a
        self.settings = {"A": settings_a, "B": settings_b}
        self.limits = limits or cfc2.FlowControlLimits(
            chunk_size=MIB, window_ack_timeout_seconds=timeouts.window_ack)
        self.retired = []
        self.dispatch = {}
        self.A = self._make_manager("A")
        self.B = self._make_manager("B")

    def _make_manager(self, who):
        """Build (or rebuild after a simulated restart) one manager on its store root."""
        settings = self.settings[who]
        manager = ClipboardManager(os.path.join(self.tmp, who), who, self._send_from(who),
                                   lambda: settings or _settings(),
                                   log_fn=self._log(who), open_channel_fn=self._open_channel)
        manager.stream_v2_timeouts = lambda: self.timeouts
        manager._stream_v2_flow = cfc2.FlowControlCoordinator(self.limits)
        manager.set_peer_transfer_strategy("device:" + ("B" if who == "A" else "A"),
                                           self.strategy)
        self.dispatch[who] = Dispatcher(manager, who)
        return manager

    def _restart(self, who):
        old = self.A if who == "A" else self.B
        self.dispatch[who].stop()
        old.shutdown(timeout=3)
        self.retired.append(old)
        manager = self._make_manager(who)
        if who == "A":
            self.A = manager
        else:
            self.B = manager
        return manager

    def restart_a(self):
        """Simulate a sender runtime restart: new manager on the same store root."""
        return self._restart("A")

    def restart_b(self):
        """Simulate a receiver runtime restart: new manager on the same store root."""
        return self._restart("B")

    def _log(self, who):
        def _fn(level, msg):
            self.logs.append((who, level, msg))
        return _fn

    def _send_from(self, who):
        other = "B" if who == "A" else "A"

        def _send(identity, msg):
            with self.lock:
                self.sent[who].append(msg)
                if not self.link_up:
                    raise ConnectionError("control link down")
            self.dispatch[other].queue.put((f"device:{who}", msg))
        return _send

    def _open_channel(self, identity, hello, timeout):
        """Emulates tray._clip_open_channel + peer_handler channel hand-off."""
        receiver = self.B if identity == "device:B" else self.A
        if not self.link_up:
            raise ConnectionError("no route")
        a, b = socket.socketpair()
        with self.lock:
            self.channels.append((a, b))

        def _server():
            reader = rm.FramedReader(b)
            try:
                first = reader.read_message(5.0)
            except Exception:
                b.close()
                return
            if not first or first.get("type") != ctv2.T_CHANNEL_HELLO:
                b.close()
                return
            receiver.accept_stream_v2_channel(b, first, leftover=bytes(reader._buf))

        threading.Thread(target=_server, daemon=True).start()
        return ctv2.open_channel_socket(lambda _t: a, hello, timeout)

    def break_channels(self):
        with self.lock:
            channels = list(self.channels)
            self.channels.clear()
        for a, b in channels:
            for sock in (a, b):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

    def messages(self, who, msg_type):
        with self.lock:
            return [msg for msg in self.sent[who] if msg.get("type") == msg_type]

    def close(self):
        for d in self.dispatch.values():
            d.stop()
        self.A.shutdown(timeout=3)
        self.B.shutdown(timeout=3)
        self.break_channels()


def make_sources(root, spec):
    """Create files under ``root`` from ``{relpath: bytes}`` and return their paths."""
    paths = []
    for rel, payload in spec.items():
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if payload is None:
            path.mkdir(exist_ok=True)
        else:
            path.write_bytes(payload)
        paths.append(str(path))
    return paths


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _outgoing(pair):
    return [rec["stage"] for rec in pair.A._stream_v2_sessions.values()
            if rec["direction"] == "outgoing"]


def _incoming(pair):
    return [rec["stage"] for rec in pair.B._stream_v2_sessions.values()
            if rec["direction"] == "incoming"]


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fs-transport-v2-")
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.src = os.path.join(self.root, "src")
        os.makedirs(self.src)

    def pair(self, **kwargs):
        pair = Pair(os.path.join(self.root, "peers"), **kwargs)
        self.addCleanup(pair.close)
        return pair

    def capture_and_sync(self, pair, spec):
        make_sources(self.src, spec)
        roots = sorted({os.path.join(self.src, rel.split("/", 1)[0]) for rel in spec})
        item = pair.A.capture_files("device:B", roots)
        self.assertIsNotNone(item)
        pair.A.on_profile_activated("device:B")
        return item

    def assert_materialized(self, pair, item_id, spec):
        dest = os.path.join(self.root, "dest")
        result = pair.B.materialize_files_result("device:A", item_id, dest)
        self.assertTrue(result.get("ok"), result)
        self.assertIn(result.get("strategy"), ("hardlink", "copy"), result)
        lease_root = None
        for base, _dirs, files in os.walk(dest):
            for name in files:
                lease_root = lease_root or base
        for rel, payload in spec.items():
            found = [os.path.join(base, f) for base, _d, fs in os.walk(dest) for f in fs
                     if os.path.relpath(os.path.join(base, f), dest).replace(os.sep, "/").endswith(rel)]
            if payload is None:
                continue
            self.assertEqual(len(found), 1, f"{rel} not materialized exactly once: {found}")
            self.assertEqual(sha(Path(found[0]).read_bytes()), sha(payload), rel)


class FullTransferTests(_Fixture):
    def test_multi_file_batch_end_to_end(self):
        pair = self.pair()
        spec = {
            "batch/big.bin": os.urandom(2 * MIB + 12345),
            "batch/small.txt": b"hello stream v2\n",
            "batch/empty.bin": b"",
            "batch/nested/deep.dat": bytes(range(256)) * 500,
            "batch/emptydir": None,
        }
        item = self.capture_and_sync(pair, spec)
        item_id = item["item_id"]
        self.assertTrue(wait_until(lambda: (pair.B.store("device:A").get_item(item_id) or {}).get(
            "payload", {}).get("encoding") == "object_manifest_v2"))
        received = pair.B.store("device:A").get_item(item_id)
        self.assertTrue(received["available"])
        self.assertEqual(received["payload_state"], "cached")
        self.assertEqual(received["item_revision"], 1)
        self.assertEqual(received["hash_state"], "verified")
        expected_hashes = {rel.split("batch/", 1)[1]: sha(data) for rel, data in spec.items()
                           if data is not None}
        got_hashes = {e["path"].split("batch/", 1)[1]: e["sha256"]
                      for e in received["batch_manifest"]["entries"] if e["type"] == "file"}
        self.assertEqual(got_hashes, expected_hashes)
        self.assertTrue(wait_until(lambda: pair.B.stats["received_items"] == 1))
        self.assertTrue(wait_until(lambda: pair.A.stats["sent_items"] == 1))
        sessions = _outgoing(pair)
        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertTrue(wait_until(lambda: session.state == "completed"))
        self.assertEqual(session.finalized_manifest_digest,
                         received["batch_manifest"]["manifest_digest"])
        self.assertEqual(session.journal.state, "completed")
        # Wire evidence: offer/accept on the control link, no legacy chunks, no Base64.
        self.assertEqual(len(pair.messages("A", ctv2.T_OFFER)), 1)
        self.assertEqual(len(pair.messages("B", ctv2.T_ACCEPT)), 1)
        self.assertEqual(pair.messages("A", "clipboard_transfer_chunk"), [])
        self.assertEqual(pair.messages("A", "clipboard_transfer_start"), [])
        # Receiver stage cleaned, journals terminal, nothing busy.
        incoming_root = pair.B.store("device:A").object_store_v2.incoming_root
        self.assertFalse(os.path.lexists(os.path.join(incoming_root, session.transfer_id)))
        journal = pair.B.stream_v2_journal_store().load("incoming", session.transfer_id)
        self.assertEqual(journal.state, "completed")
        self.assertFalse(pair.B.transfer_activity_state()["busy"])
        self.assertTrue(wait_until(lambda: not pair.A.transfer_activity_state()["busy"]))
        # Status privacy: no absolute paths anywhere, relative names only.
        status = json.dumps(pair.A.stream_v2_status() + pair.B.stream_v2_status()
                            + [session.status()])
        self.assertNotIn(self.root.replace("\\", "\\\\"), status)
        self.assertNotIn(self.root, status)
        self.assertNotIn(self.src, status)
        for who, _level, msg in pair.logs:
            self.assertNotIn(self.src, msg, f"{who}: {msg}")
        self.assert_materialized(pair, item_id, spec)
        self.assertEqual(received["batch_manifest"]["directory_count"], 3)
        # A re-sync of the same history does not re-request the finalized item.
        before = len(pair.messages("B", "clipboard_request_items"))
        pair.A.on_profile_activated("device:B")
        time.sleep(0.5)
        self.assertEqual(len(pair.messages("B", "clipboard_request_items")), before)
        self.assertEqual(len(pair.messages("A", ctv2.T_OFFER)), 1)

    def test_status_includes_outgoing_direction_and_strategy(self):
        pair = self.pair()
        item = self.capture_and_sync(pair, {"one.bin": os.urandom(MIB + 1)})
        self.assertTrue(wait_until(lambda: _outgoing(pair)))
        status = wait_until(lambda: [s for s in pair.A.stream_v2_status()
                                     if s["direction"] == "outgoing"])
        self.assertTrue(status)
        self.assertEqual(status[0]["strategy"], "stream_v2")
        self.assertEqual(status[0]["item_id"], item["item_id"])
        self.assertIn(status[0]["state"], ("created", "preflight", "accepted", "sending_manifest",
                                            "transferring", "finalizing", "completed"))
        self.assertTrue(wait_until(lambda: _outgoing(pair)[0].state == "completed"))


class RejectTests(_Fixture):
    def test_preflight_rejection_leaves_no_stage_or_journal(self):
        pair = self.pair()
        real = ctt.check_disk_space

        def _disk(path, required):
            result = real(path, required)
            if os.path.normcase(str(path)).startswith(os.path.normcase(os.path.join(
                    self.root, "peers", "B"))):
                return dict(result, ok=False, free_bytes=1)
            return result

        with mock.patch.object(ctt, "check_disk_space", side_effect=_disk):
            # B's legacy manifest preflight also sees no space; request manually.
            paths = make_sources(self.src, {"r.bin": os.urandom(4096)})
            item = pair.A.capture_files("device:B", paths)
            offer_item = pair.B.store("device:A")
            pair.A.on_profile_activated("device:B")
            self.assertTrue(wait_until(lambda: offer_item.get_item(item["item_id"])))
            pair.B.request_items("device:A", [item["item_id"]])
            self.assertTrue(wait_until(lambda: _outgoing(pair)
                                       and _outgoing(pair)[0].state == "rejected"))
        session = _outgoing(pair)[0]
        self.assertEqual(session.reject_reason, "disk_full")
        self.assertEqual(session.status()["preflight_state"], "rejected")
        self.assertEqual(session.status()["reject_reason"], "disk_full")
        self.assertIsNone(session.journal)
        with self.assertRaises(cres2.ResumeJournalError):
            pair.A.stream_v2_journal_store().load("outgoing", session.transfer_id)
        with self.assertRaises(cres2.ResumeJournalError):
            pair.B.stream_v2_journal_store().load("incoming", session.transfer_id)
        incoming_root = pair.B.store("device:A").object_store_v2.incoming_root
        self.assertFalse(os.path.lexists(os.path.join(incoming_root, session.transfer_id)))
        self.assertEqual(_incoming(pair), [])
        self.assertFalse(pair.B.transfer_activity_state()["busy"])
        self.assertEqual(pair.messages("B", ctv2.T_REJECT)[0]["reason"], "disk_full")
        self.assertFalse(pair.B.store("device:A").get_item(item["item_id"])["available"])
        pair.A.run_stream_v2_maintenance(now=time.monotonic())
        self.assertEqual(_outgoing(pair), [])


class ResumeTests(_Fixture):
    def test_disconnect_mid_transfer_resumes_from_durable_offsets(self):
        pair = self.pair()
        payload = os.urandom(6 * MIB + 777)
        spec = {"large.bin": payload, "tail.txt": b"after the big one"}
        original_accept = cstr2.IncomingTransferStage.accept

        def slow_accept(self_stage, chunk, **kwargs):
            time.sleep(0.05)
            return original_accept(self_stage, chunk, **kwargs)

        with mock.patch.object(cstr2.IncomingTransferStage, "accept", slow_accept):
            item = self.capture_and_sync(pair, spec)
            item_id = item["item_id"]
            self.assertTrue(wait_until(
                lambda: _incoming(pair) and MIB <= _incoming(pair)[0].bytes_done < 5 * MIB))
            receiver = _incoming(pair)[0]
            sender = _outgoing(pair)[0]
            pair.break_channels()
            self.assertTrue(wait_until(lambda: sender.state == "waiting_reconnect"
                                       or sender.state in ("transferring", "completed")))
            self.assertTrue(wait_until(lambda: receiver.journal is not None
                                       and receiver.journal.state in ("waiting_reconnect", "transferring",
                                                                       "completed", "finalizing")))
            self.assertTrue(wait_until(lambda: sender.state == "completed", timeout=30))
        received = pair.B.store("device:A").get_item(item_id)
        self.assertEqual(received["payload"]["encoding"], "object_manifest_v2")
        self.assertEqual(sender.status()["retry_count"], 1)
        resume_bytes = sender.resume_bytes
        self.assertGreater(resume_bytes, 0, "durable checkpoint bytes were credited")
        self.assertLess(resume_bytes, len(payload))
        # Bytes actually re-sent equal total minus the durable prefix: no
        # acknowledged/durable byte travelled twice.
        self.assertEqual(sender.bytes_done, item["size"])
        self.assertTrue(any("resume=True" in msg for _w, _l, msg in pair.logs))
        self.assertEqual(len(pair.messages("A", ctv2.T_RESUME_REQUEST)), 1)
        response = pair.messages("B", ctv2.T_RESUME_RESPONSE)[0]
        durable = sum(f["durable_offset"] for f in response["files"])
        self.assertEqual(durable, resume_bytes)
        self.assert_materialized(pair, item_id, spec)
        journal = pair.B.stream_v2_journal_store().load("incoming", sender.transfer_id)
        self.assertEqual(journal.state, "completed")
        self.assertEqual(journal.retry_count, 1)
        self.assertFalse(pair.B.transfer_activity_state()["busy"])


class RestartTests(_Fixture):
    """Sender-restart resume (section 17): durable outgoing journals are re-offered."""

    def _slow_receiver(self, delay=0.05):
        original_accept = cstr2.IncomingTransferStage.accept

        def slow_accept(self_stage, chunk, **kwargs):
            time.sleep(delay)
            return original_accept(self_stage, chunk, **kwargs)

        patcher = mock.patch.object(cstr2.IncomingTransferStage, "accept", slow_accept)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _start_and_interrupt(self, pair, spec):
        """Start a transfer, wait for partial durable progress, restart the sender."""
        item = self.capture_and_sync(pair, spec)
        self.assertTrue(wait_until(
            lambda: _incoming(pair) and MIB <= _incoming(pair)[0].bytes_done < 5 * MIB))
        sender = _outgoing(pair)[0]
        tid = sender.transfer_id
        pair.restart_a()
        self.assertEqual(sender.state, "paused", sender.status())
        self.assertFalse(sender.is_alive())
        journal = pair.A.stream_v2_journal_store().load("outgoing", tid)
        self.assertEqual(journal.state, "paused")
        self.assertEqual(_outgoing(pair), [])
        return item, tid, journal

    def _assert_resumed_to_completion(self, pair, item, tid, spec):
        self.assertTrue(wait_until(lambda: _outgoing(pair)), "restored sender session registered")
        self.assertEqual(len(_outgoing(pair)), 1)
        restored = _outgoing(pair)[0]
        self.assertEqual(restored.transfer_id, tid)
        self.assertTrue(wait_until(lambda: restored.state == "completed", timeout=30),
                        restored.status())
        self.assertEqual(len(_outgoing(pair)), 1, "no second outgoing session for the item")
        self.assertEqual(len(pair.messages("A", ctv2.T_RESUME_REQUEST)), 1)
        self.assertEqual(len(pair.messages("A", ctv2.T_OFFER)), 1, "no fresh offer after restart")
        total = item["size"]
        self.assertGreater(restored.resume_bytes, 0)
        self.assertLess(restored.resume_bytes, total)
        self.assertEqual(restored.bytes_done, total)
        response = pair.messages("B", ctv2.T_RESUME_RESPONSE)[-1]
        self.assertEqual(sum(f["durable_offset"] for f in response["files"]),
                         restored.resume_bytes)
        self.assertTrue(wait_until(lambda: (pair.B.store("device:A").get_item(item["item_id"])
                                            or {}).get("payload", {}).get("encoding")
                                   == "object_manifest_v2"))
        self.assert_materialized(pair, item["item_id"], spec)
        incoming = pair.B.stream_v2_journal_store().load("incoming", tid)
        self.assertEqual(incoming.state, "completed")
        outgoing = pair.A.stream_v2_journal_store().load("outgoing", tid)
        self.assertEqual(outgoing.state, "completed")
        self.assertEqual(restored.journal.state, "completed")
        self.assertTrue(wait_until(lambda: not pair.A.transfer_activity_state()["busy"]))
        self.assertFalse(pair.B.transfer_activity_state()["busy"])
        for _who, _level, msg in pair.logs:
            self.assertNotIn(self.root, msg)
        return restored

    def test_sender_restart_re_offers_outgoing_journal(self):
        pair = self.pair()
        self._slow_receiver()
        spec = {"large.bin": os.urandom(6 * MIB + 777), "tail.txt": b"after the big one"}
        item, tid, _journal = self._start_and_interrupt(pair, spec)
        receiver = _incoming(pair)[0]
        self.assertTrue(wait_until(lambda: receiver.state in ("waiting_reconnect", "paused")),
                        receiver.status())
        pair.A.on_peer_connected("B", "device:B")
        pair.B.on_peer_connected("A", "device:A")
        restored = self._assert_resumed_to_completion(pair, item, tid, spec)
        self.assertGreaterEqual(restored.status()["retry_count"], 1)
        self.assertTrue(any("restart=True" in msg for _w, _l, msg in pair.logs))

    def test_both_peers_restart_resume(self):
        pair = self.pair()
        self._slow_receiver()
        spec = {"large.bin": os.urandom(6 * MIB + 99), "tail.txt": b"tail"}
        item, tid, _journal = self._start_and_interrupt(pair, spec)
        receiver = _incoming(pair)[0]
        self.assertTrue(wait_until(lambda: receiver.state in ("waiting_reconnect", "paused")),
                        receiver.status())
        pair.restart_b()
        incoming = pair.B.stream_v2_journal_store().load("incoming", tid)
        self.assertIn(incoming.state, ("paused", "waiting_reconnect"))
        self.assertEqual(_incoming(pair), [])
        pair.A.on_peer_connected("B", "device:B")
        pair.B.on_peer_connected("A", "device:A")
        self._assert_resumed_to_completion(pair, item, tid, spec)

    def test_sender_restart_with_changed_source_does_not_re_offer(self):
        pair = self.pair()
        self._slow_receiver()
        spec = {"large.bin": os.urandom(6 * MIB + 5)}
        item, tid, _journal = self._start_and_interrupt(pair, spec)
        # The source item disappeared from the sender's store before the restart
        # completed: the journal must not be re-offered against a different item.
        self.assertTrue(pair.A.store("device:B").delete_item(item["item_id"]))
        receiver = _incoming(pair)[0]
        self.assertTrue(wait_until(lambda: receiver.state in ("waiting_reconnect", "paused")),
                        receiver.status())
        pair.A.on_peer_connected("B", "device:B")
        pair.B.on_peer_connected("A", "device:A")
        self.assertTrue(wait_until(lambda: any(
            "cause=source_missing" in msg for _w, _l, msg in pair.logs)))
        with self.assertRaises(cres2.ResumeJournalError):
            pair.A.stream_v2_journal_store().load("outgoing", tid)
        self.assertEqual(_outgoing(pair), [])
        self.assertEqual(pair.messages("A", ctv2.T_RESUME_REQUEST), [])
        self.assertEqual(len(pair.messages("A", ctv2.T_OFFER)), 1)
        # The live receiver is told to drop its partials right away.
        cancels = pair.messages("A", ctv2.T_CANCEL)
        self.assertEqual([(c["transfer_id"], c["reason"]) for c in cancels],
                         [(tid, "source_missing")])
        self.assertTrue(wait_until(lambda: receiver.state == "purged"), receiver.status())
        self.assertEqual(receiver.cancel_reason, "peer:source_missing")
        with self.assertRaises(cres2.ResumeJournalError) as ctx:
            pair.B.stream_v2_journal_store().load("incoming", tid)
        self.assertEqual(ctx.exception.code, "not_found")
        incoming_root = pair.B.store("device:A").object_store_v2.incoming_root
        self.assertFalse(os.path.lexists(os.path.join(incoming_root, tid)))
        self.assertEqual(len(pair.messages("B", ctv2.T_CANCEL_ACK)), 1)
        received = pair.B.store("device:A").get_item(item["item_id"])
        self.assertIsNotNone(received)
        self.assertFalse(received["available"])
        self.assertTrue(wait_until(lambda: not pair.B.transfer_activity_state()["busy"]))
        # A second peer connect finds nothing to resume and stays quiet.
        self.assertEqual(pair.A.resume_outgoing_stream_v2("device:B"), [])
        self.assertEqual(pair.messages("A", ctv2.T_RESUME_REQUEST), [])
        self.assertEqual(len(pair.messages("A", ctv2.T_CANCEL)), 1)

    def test_sender_restart_source_missing_after_both_restart_purges_receiver_journal(self):
        pair = self.pair()
        self._slow_receiver()
        spec = {"large.bin": os.urandom(6 * MIB + 7)}
        item, tid, _journal = self._start_and_interrupt(pair, spec)
        receiver = _incoming(pair)[0]
        self.assertTrue(wait_until(lambda: receiver.state in ("waiting_reconnect", "paused")),
                        receiver.status())
        pair.restart_b()
        incoming_root = pair.B.store("device:A").object_store_v2.incoming_root
        self.assertTrue(os.path.isdir(os.path.join(incoming_root, tid)))
        self.assertIn(pair.B.stream_v2_journal_store().load("incoming", tid).state,
                      ("paused", "waiting_reconnect"))
        self.assertTrue(pair.A.store("device:B").delete_item(item["item_id"]))
        pair.A.on_peer_connected("B", "device:B")
        pair.B.on_peer_connected("A", "device:A")
        self.assertTrue(wait_until(lambda: any(
            "cause=orphan_cancel" in msg for _w, _l, msg in pair.logs)))
        cancels = pair.messages("A", ctv2.T_CANCEL)
        self.assertEqual([(c["transfer_id"], c["reason"]) for c in cancels],
                         [(tid, "source_missing")])
        self.assertEqual(pair.messages("A", ctv2.T_RESUME_REQUEST), [])
        with self.assertRaises(cres2.ResumeJournalError) as ctx:
            pair.B.stream_v2_journal_store().load("incoming", tid)
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertFalse(os.path.lexists(os.path.join(incoming_root, tid)))
        self.assertTrue(any("state=purged" in msg and "cause=orphan_cancel" in msg
                            for _w, _l, msg in pair.logs), "stage purged via reopen path")
        self.assertEqual(len(pair.messages("B", ctv2.T_CANCEL_ACK)), 1)
        self.assertEqual(_incoming(pair), [])
        self.assertFalse(pair.B.transfer_activity_state()["busy"])
        with self.assertRaises(cres2.ResumeJournalError):
            pair.A.stream_v2_journal_store().load("outgoing", tid)

    def test_sender_restart_with_changed_manifest_digest_purges_journal(self):
        pair = self.pair()
        self._slow_receiver()
        spec = {"large.bin": os.urandom(6 * MIB + 6)}
        item, tid, _journal = self._start_and_interrupt(pair, spec)
        st = pair.A.store("device:B")
        changed = st.get_item(item["item_id"])
        manifest = dict(changed["batch_manifest"], item_revision=2)
        manifest["manifest_digest"] = cman2.manifest_digest(manifest)
        changed["batch_manifest"] = manifest
        changed["item_revision"] = 2
        st.add_item(changed, data=None, make_current=False, replace_existing=True)
        self.assertNotEqual(st.get_item(item["item_id"])["batch_manifest"]["manifest_digest"],
                            _journal.manifest_digest)
        self.assertEqual(pair.A.resume_outgoing_stream_v2("device:B"), [])
        self.assertTrue(any("cause=source_changed" in msg for _w, _l, msg in pair.logs))
        with self.assertRaises(cres2.ResumeJournalError):
            pair.A.stream_v2_journal_store().load("outgoing", tid)
        self.assertEqual(_outgoing(pair), [])
        self.assertEqual(pair.messages("A", ctv2.T_RESUME_REQUEST), [])


class CancelTests(_Fixture):
    def _start_slow_transfer(self, pair):
        original_accept = cstr2.IncomingTransferStage.accept

        def slow_accept(self_stage, chunk, **kwargs):
            time.sleep(0.08)
            return original_accept(self_stage, chunk, **kwargs)

        patcher = mock.patch.object(cstr2.IncomingTransferStage, "accept", slow_accept)
        patcher.start()
        self.addCleanup(patcher.stop)
        item = self.capture_and_sync(pair, {"c.bin": os.urandom(6 * MIB)})
        self.assertTrue(wait_until(
            lambda: _incoming(pair) and 0 < _incoming(pair)[0].bytes_done < 4 * MIB))
        return item, _outgoing(pair)[0], _incoming(pair)[0]

    def _assert_both_purged(self, pair, sender, receiver, item):
        self.assertTrue(wait_until(lambda: sender.state == "purged"), sender.status())
        self.assertTrue(wait_until(lambda: receiver.state == "purged"), receiver.status())
        incoming_root = pair.B.store("device:A").object_store_v2.incoming_root
        self.assertFalse(os.path.lexists(os.path.join(incoming_root, sender.transfer_id)))
        for manager, direction in ((pair.A, "outgoing"), (pair.B, "incoming")):
            with self.assertRaises(cres2.ResumeJournalError):
                manager.stream_v2_journal_store().load(direction, sender.transfer_id)
        self.assertFalse(pair.A.transfer_activity_state()["busy"])
        self.assertFalse(pair.B.transfer_activity_state()["busy"])
        self.assertEqual(len(pair.messages("A", ctv2.T_CANCEL))
                         + len(pair.messages("B", ctv2.T_CANCEL)), 1)
        self.assertEqual(len(pair.messages("A", ctv2.T_CANCEL_ACK))
                         + len(pair.messages("B", ctv2.T_CANCEL_ACK)), 1)
        self.assertFalse(pair.B.store("device:A").get_item(item["item_id"])["available"])
        self.assertTrue(wait_until(lambda: not sender.is_alive()))
        self.assertTrue(receiver.join(5))

    def test_cancel_from_sender_in_transfer_phase(self):
        pair = self.pair()
        item, sender, receiver = self._start_slow_transfer(pair)
        result = pair.A.cancel_stream_v2_session(sender.transfer_id, "user")
        self.assertTrue(result["ok"])
        self.assertTrue(wait_until(lambda: receiver.state in ("cancelled", "purged")))
        self.assertEqual(receiver.cancel_reason, "peer:user")
        self._assert_both_purged(pair, sender, receiver, item)
        self.assertEqual(sender.cancel_reason, "user")

    def test_cancel_from_receiver_in_transfer_phase(self):
        pair = self.pair()
        item, sender, receiver = self._start_slow_transfer(pair)
        result = pair.B.cancel_stream_v2_session(sender.transfer_id, "user")
        self.assertTrue(result["ok"])
        self.assertEqual(result["journal_state"], "cancelled")
        self.assertTrue(wait_until(lambda: sender.state in ("cancelled", "purged")))
        self.assertEqual(sender.cancel_reason, "user")
        self._assert_both_purged(pair, sender, receiver, item)

    def test_deleting_the_receiving_item_cancels_the_transfer(self):
        pair = self.pair()
        item, sender, receiver = self._start_slow_transfer(pair)
        pair.B.delete_item("device:A", item["item_id"])
        self.assertTrue(wait_until(lambda: sender.state in ("cancelled", "purged")))
        self.assertTrue(wait_until(lambda: receiver.state == "purged"))
        self.assertIsNone(pair.B.store("device:A").get_item(item["item_id"]))


class SenderTimeoutTests(_Fixture):
    """The receiver accepts on the control link, but the channel peer stays silent."""

    def _silent_channel(self, pair, *, ack_manifest):
        def _open(identity, hello, timeout):
            a, b = socket.socketpair()
            pair.channels.append((a, b))

            def _server():
                reader = rm.FramedReader(b)
                first = reader.read_message(5.0)
                rm.send_msg(b, ctv2.build_channel_ack(first["transfer_id"], True))
                typed = cfr2.TypedFrameReader(b)
                writer = cfr2.TypedFrameWriter(b)
                try:
                    while True:
                        frame = typed.read_frame(1.0)
                        if frame is None:
                            continue
                        if (ack_manifest and isinstance(frame, cfr2.JsonControlFrame)
                                and frame.message.get("type") == ctv2.T_MANIFEST):
                            manifest = _outgoing(pair)[0].manifest
                            files = [{"index": e["index"], "durable_offset": 0,
                                      "prefix_sha256": None, "completed": False}
                                     for e in manifest["entries"] if e["type"] == "file"]
                            writer.send_json_control(ctv2.build_manifest_ack(
                                first["transfer_id"], manifest["manifest_digest"], files,
                                max_inflight_chunks=4, chunk_size=MIB))
                except (ConnectionError, OSError, cfr2.TypedFrameError):
                    pass

            threading.Thread(target=_server, daemon=True).start()
            return ctv2.open_channel_socket(lambda _t: a, hello, timeout)
        pair.A.open_channel_fn = _open

    def test_manifest_ack_timeout_fails_sender(self):
        short = cctl2.TransferTimeouts(preflight=8, manifest_ack=1, window_ack=8,
                                       no_progress=8, reconnect_wait=8, final_complete_ack=8)
        pair = self.pair(timeouts=short)
        self._silent_channel(pair, ack_manifest=False)
        self.capture_and_sync(pair, {"m.bin": os.urandom(2 * MIB)})
        self.assertTrue(wait_until(lambda: _outgoing(pair) and _outgoing(pair)[0].state == "failed"))
        sender = _outgoing(pair)[0]
        self.assertEqual(sender.error_code, "manifest_ack_timeout")
        self.assertEqual(sender.journal.state, "failed")
        self.assertEqual(sender.status()["timeout"], None)
        self.assertFalse(pair.A.transfer_activity_state()["busy"])

    def test_window_ack_timeout_fails_sender(self):
        short = cctl2.TransferTimeouts(preflight=8, manifest_ack=8, window_ack=1,
                                       no_progress=8, reconnect_wait=8, final_complete_ack=8)
        limits = cfc2.FlowControlLimits(chunk_size=MIB, max_inflight_chunks_per_transfer=2,
                                        window_ack_timeout_seconds=1.0)
        pair = self.pair(timeouts=short, limits=limits)
        self._silent_channel(pair, ack_manifest=True)
        self.capture_and_sync(pair, {"w.bin": os.urandom(5 * MIB)})
        self.assertTrue(wait_until(lambda: _outgoing(pair) and _outgoing(pair)[0].state == "failed"))
        sender = _outgoing(pair)[0]
        self.assertEqual(sender.error_code, "window_ack_timeout")
        self.assertLessEqual(sender.bytes_done, 3 * MIB, "sender kept only the bounded window in flight")
        self.assertEqual(pair.A._stream_v2_flow.snapshot()["active_transfers"], 0)
        self.assertEqual(pair.A._stream_v2_flow.snapshot()["inflight_bytes"], 0)


class NegotiationTests(_Fixture):
    def test_legacy_peer_without_capability_uses_legacy_zip(self):
        pair = self.pair(strategy_a="legacy_zip_v1")
        item = self.capture_and_sync(pair, {"legacy.bin": os.urandom(70000)})
        item_id = item["item_id"]
        self.assertTrue(wait_until(lambda: (pair.B.store("device:A").get_item(item_id) or {}).get(
            "available")))
        received = pair.B.store("device:A").get_item(item_id)
        self.assertEqual(received["payload"]["encoding"], "deterministic_zip")
        self.assertEqual(pair.messages("A", ctv2.T_OFFER), [])
        self.assertGreater(len(pair.messages("A", "clipboard_transfer_chunk")), 0)
        self.assertEqual(pair.A.stream_v2_status(), [])
        self.assertEqual(pair.B.stream_v2_status(), [])

    def test_force_legacy_setting_disables_v2_even_when_negotiated(self):
        pair = self.pair(settings_a=_settings(clipboard_transfer_v2_force_legacy=True))
        self.assertEqual(pair.A.peer_transfer_strategy("device:B"), "legacy_zip_v1")
        item = self.capture_and_sync(pair, {"forced.bin": os.urandom(4096)})
        self.assertTrue(wait_until(lambda: (pair.B.store("device:A").get_item(item["item_id"]) or {}).get(
            "available")))
        self.assertEqual(pair.messages("A", ctv2.T_OFFER), [])
        self.assertGreater(len(pair.messages("A", "clipboard_transfer_chunk")), 0)

    def test_receiver_forced_legacy_rejects_offer(self):
        pair = self.pair(settings_b=_settings(clipboard_transfer_v2_force_legacy=True))
        self.capture_and_sync(pair, {"rej.bin": os.urandom(4096)})
        self.assertTrue(wait_until(lambda: _outgoing(pair) and _outgoing(pair)[0].state == "rejected"))
        self.assertEqual(_outgoing(pair)[0].reject_reason, "legacy_only")

    def test_peer_cancel_for_terminal_sender_is_still_acknowledged(self):
        # Race from section 21: the receiver cancels (closing the channel), the
        # sender re-offers, is rejected and becomes terminal, and only then sees
        # the peer's cancel. The receiver still needs the ACK to purge.
        pair = self.pair(settings_b=_settings(clipboard_transfer_v2_force_legacy=True))
        self.capture_and_sync(pair, {"rej.bin": os.urandom(4096)})
        self.assertTrue(wait_until(lambda: _outgoing(pair) and _outgoing(pair)[0].state == "rejected"))
        sender = _outgoing(pair)[0]
        acks_before = len(pair.messages("A", ctv2.T_CANCEL_ACK))
        self.assertTrue(pair.A.handle("device:B", ctv2.build_cancel(sender.transfer_id, "item_removed")))
        self.assertEqual(len(pair.messages("A", ctv2.T_CANCEL_ACK)), acks_before + 1)
        self.assertEqual(pair.messages("A", ctv2.T_CANCEL_ACK)[-1]["transfer_id"], sender.transfer_id)
        self.assertEqual(sender.state, "rejected")
        self.assertFalse(pair.A.transfer_activity_state()["busy"])

    def test_strategy_requires_channel_opener(self):
        manager = ClipboardManager(os.path.join(self.root, "solo"), "S", lambda *_a: None,
                                   lambda: _settings())
        self.addCleanup(manager.shutdown)
        manager.set_peer_transfer_strategy("device:X", "stream_v2")
        self.assertEqual(manager.peer_transfer_strategy("device:X"), "legacy_zip_v1")
        manager.open_channel_fn = lambda *_a: None
        self.assertEqual(manager.peer_transfer_strategy("device:X"), "stream_v2")
        manager.on_peer_disconnected("X")
        self.assertEqual(manager.peer_transfer_strategy("device:X"), "legacy_zip_v1")


class HostileInputTests(_Fixture):
    """Drive B's receiver directly with a hand-built sender."""

    def _offer_to_b(self, manager_b, sent, item):
        transfer_id = "1" * 32
        manifest = item["batch_manifest"]
        offer = ctv2.build_offer(transfer_id, manifest, device_id="A", profile_id="p")
        manager_b.handle("device:A", offer)
        accepts = [m for m in sent if m.get("type") == ctv2.T_ACCEPT]
        self.assertEqual(len(accepts), 1, sent)
        return transfer_id, ctv2.parse_accept(accepts[0])

    def _receiver(self):
        sent = []
        manager = ClipboardManager(os.path.join(self.root, "B"), "B",
                                   lambda _i, msg: sent.append(msg), lambda: _settings(),
                                   open_channel_fn=lambda *_a: None)
        self.addCleanup(manager.shutdown)
        manager.stream_v2_timeouts = lambda: FAST
        manager.set_peer_transfer_strategy("device:A", "stream_v2")
        return manager, sent

    def _open_channel(self, manager, transfer_id, nonce):
        a, b = socket.socketpair()
        self.addCleanup(lambda: (a.close(), b.close()))
        hello = ctv2.build_channel_hello(transfer_id, nonce, "A")
        threading.Thread(target=lambda: manager.accept_stream_v2_channel(
            b, rm.FramedReader(b).read_message(5.0)), daemon=True).start()
        return ctv2.open_channel_socket(lambda _t: a, hello, 5.0)

    def _item(self):
        paths = make_sources(self.src, {"h.bin": os.urandom(3000)})
        import clipboard_files as cf
        return cf.make_file_item(paths)

    def _assert_protocol_cancel(self, manager, sent, transfer_id):
        receiver = manager._stream_v2_receiver(transfer_id)
        self.assertTrue(wait_until(lambda: receiver.state == "cancelled"), receiver.status())
        cancels = [m for m in sent if m.get("type") == ctv2.T_CANCEL]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0]["reason"], "protocol_error")
        self.assertEqual(receiver.journal.state, "cancelled")
        incoming_root = manager.store("device:A").object_store_v2.incoming_root
        self.assertTrue(os.path.isdir(os.path.join(incoming_root, transfer_id)))
        manager.handle("device:A", ctv2.build_cancel_ack(transfer_id))
        self.assertEqual(receiver.state, "purged")
        self.assertFalse(os.path.lexists(os.path.join(incoming_root, transfer_id)))
        self.assertFalse(manager.transfer_activity_state()["busy"])

    def test_malformed_frame_cancels_without_crash(self):
        manager, sent = self._receiver()
        item = self._item()
        transfer_id, accept = self._offer_to_b(manager, sent, item)
        sock = self._open_channel(manager, transfer_id, accept["channel_nonce"])
        sock.sendall(struct.pack("!I", 3) + b"\x07\x02\x00")  # unknown frame kind
        self._assert_protocol_cancel(manager, sent, transfer_id)

    def test_oversized_frame_is_rejected(self):
        manager, sent = self._receiver()
        item = self._item()
        transfer_id, accept = self._offer_to_b(manager, sent, item)
        sock = self._open_channel(manager, transfer_id, accept["channel_nonce"])
        sock.sendall(struct.pack("!I", cfr2.MAX_TYPED_FRAME_BYTES + 1) + b"\x02\x02\x00\x00")
        self._assert_protocol_cancel(manager, sent, transfer_id)

    def test_out_of_order_payload_is_rejected(self):
        manager, sent = self._receiver()
        item = self._item()
        transfer_id, accept = self._offer_to_b(manager, sent, item)
        sock = self._open_channel(manager, transfer_id, accept["channel_nonce"])
        writer = cfr2.TypedFrameWriter(sock)
        writer.send_json_control(ctv2.build_manifest_frame(transfer_id, item["batch_manifest"]))
        reader = cfr2.TypedFrameReader(sock)
        ack = reader.read_frame(5.0)
        self.assertIsInstance(ack, cfr2.JsonControlFrame)
        self.assertEqual(ack.message["type"], ctv2.T_MANIFEST_ACK)
        writer.send_binary_payload(transfer_id, 0, 100, b"x" * 50)  # offset 100 before 0
        self._assert_protocol_cancel(manager, sent, transfer_id)

    def test_payload_before_manifest_and_wrong_transfer_are_rejected(self):
        manager, sent = self._receiver()
        item = self._item()
        transfer_id, accept = self._offer_to_b(manager, sent, item)
        sock = self._open_channel(manager, transfer_id, accept["channel_nonce"])
        cfr2.TypedFrameWriter(sock).send_binary_payload("2" * 32, 0, 0, b"x")
        self._assert_protocol_cancel(manager, sent, transfer_id)

    def test_invalid_or_reused_nonce_is_refused(self):
        manager, sent = self._receiver()
        item = self._item()
        transfer_id, accept = self._offer_to_b(manager, sent, item)
        with self.assertRaises(ctv2.TransportV2Error) as ctx:
            self._open_channel(manager, transfer_id, "f" * 32)
        self.assertEqual(ctx.exception.code, "channel_invalid_nonce")
        sock = self._open_channel(manager, transfer_id, accept["channel_nonce"])
        self.assertIsNotNone(sock)
        with self.assertRaises(ctv2.TransportV2Error) as ctx:
            self._open_channel(manager, transfer_id, accept["channel_nonce"])
        self.assertIn(ctx.exception.code, ("channel_invalid_nonce", "channel_busy"))
        self.assertEqual(len(manager._stream_v2_nonces), 0)
        for msg in sent:
            self.assertNotIn(accept["channel_nonce"], json.dumps(msg)) if msg.get(
                "type") != ctv2.T_ACCEPT else None
        self.assertTrue(manager.transfer_activity_state()["busy"])

    def test_malformed_control_messages_are_ignored(self):
        manager, sent = self._receiver()
        for msg in ({"type": ctv2.T_OFFER}, {"type": ctv2.T_ACCEPT, "schema_version": 1},
                    {"type": ctv2.T_CANCEL, "schema_version": 1, "protocol_major": 2,
                     "transfer_id": "zz", "reason": "x"},
                    {"type": ctv2.T_OFFER, "schema_version": 1, "protocol_major": 2,
                     "transfer_id": "3" * 32, "device_id": "A", "profile_id": "p",
                     "item_id": "i", "resume": False,
                     "manifest": {"schema_version": 2}}):
            self.assertTrue(manager.handle("device:A", msg))
        self.assertEqual([m for m in sent if m.get("type") != ctv2.T_CANCEL_ACK], [])
        self.assertEqual(manager.stream_v2_status(), [])


class MessageContractTests(unittest.TestCase):
    def test_resume_files_validation(self):
        good = [{"index": 0, "durable_offset": 10, "prefix_sha256": "a" * 64, "completed": False}]
        self.assertEqual(ctv2.parse_resume_files(good), good)
        self.assertIsNone(ctv2.parse_resume_files([{"index": 0, "durable_offset": 10,
                                                    "prefix_sha256": None, "completed": False}]))
        self.assertIsNone(ctv2.parse_resume_files([dict(good[0], index=-1)]))
        self.assertIsNone(ctv2.parse_resume_files([good[0], good[0]]))
        self.assertIsNone(ctv2.parse_resume_files([dict(good[0], durable_offset=2 ** 64)]))
        self.assertIsNone(ctv2.parse_resume_files("nope"))

    def test_nonce_registry_is_single_use_and_expiring(self):
        registry = ctv2.ChannelNonceRegistry(lifetime=10, maximum=2)
        nonce = registry.issue("1" * 32, "device:A", now=0)
        self.assertIsNone(registry.consume(nonce, "2" * 32, now=1))
        self.assertIsNone(registry.consume("0" * 32, "1" * 32, now=1))
        second = registry.issue("1" * 32, "device:A", now=1)
        self.assertIsNone(registry.consume(nonce, "1" * 32, now=2), "re-offer revokes the old nonce")
        self.assertEqual(registry.consume(second, "1" * 32, now=2), "device:A")
        self.assertIsNone(registry.consume(second, "1" * 32, now=2), "single use")
        expired = registry.issue("3" * 32, "device:A", now=0)
        self.assertIsNone(registry.consume(expired, "3" * 32, now=11))

    def test_complete_frame_round_trip_and_bounds(self):
        import clipboard_files as cf
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.bin"
            path.write_bytes(b"abc")
            item = cf.make_file_item([str(path)])
        manifest = item["batch_manifest"]
        completion = cstr2.SourceStreamCompletion(
            "4" * 32, manifest["manifest_digest"], 3, {0: sha(b"abc")},
            {0: manifest["entries"][0]["source_fingerprint"]})
        frame = ctv2.build_complete_frame(completion)
        parsed = ctv2.parse_complete_frame(frame, manifest)
        self.assertEqual(parsed.file_hashes[0], sha(b"abc"))
        self.assertIsNone(ctv2.parse_complete_frame(dict(frame, total_bytes=4), manifest))
        self.assertIsNone(ctv2.parse_complete_frame(
            dict(frame, file_hashes={"x": sha(b"abc")}), manifest))
        self.assertIsNone(ctv2.parse_complete_frame(
            dict(frame, file_hashes={"0": "zz"}), manifest))


if __name__ == "__main__":
    unittest.main()
