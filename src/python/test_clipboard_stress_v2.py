"""Deterministic stress and fault-injection tests for the productive stream_v2
transport (Phase 3, sections 28-29).

Fault injection is applied with ``unittest.mock.patch`` on productive classes;
the runtime itself contains no injection hooks. Every test drives two real
``ClipboardManager`` instances through the transport-level ``Pair`` fixture of
``test_clipboard_transport_v2`` (control link + real data socketpair).
"""
from __future__ import annotations

import collections
import errno
import json
import os
import random
import socket
import struct
import sys
import threading
import time
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clipboard_files as cf
import clipboard_flow_control_v2 as cfc2
import clipboard_framing_v2 as cfr2
import clipboard_manifest_v2 as cman2
import clipboard_resume_v2 as cres2
import clipboard_streaming_v2 as cstr2
import clipboard_transfer_control_v2 as cctl2
import clipboard_transport_v2 as ctv2
import runtime_model as rm
from test_clipboard_transport_v2 import (FAST, MIB, _Fixture, _incoming, _outgoing, _settings,
                                         make_sources, sha, wait_until)

STATUS_KEYS = {
    "transfer_id", "item_id", "direction", "strategy", "state", "journal_state",
    "current_file", "file_index", "file_count", "bytes_done", "total_bytes", "percent",
    "rate_bytes_per_s", "eta_seconds", "resume_bytes", "retry_count", "provider", "peer_id",
    "preflight_state", "error_code",
}

BIG_SETTINGS = dict(max_auto_transfer_mb=4096)


def _strings(value):
    """Yield every string leaf of a nested JSON-like structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child)


def _part_files(incoming_root):
    found = []
    for base, _dirs, files in os.walk(incoming_root):
        found.extend(os.path.join(base, f) for f in files if f.endswith(".part"))
    return found


def _slow_accept(delay):
    original = cstr2.IncomingTransferStage.accept

    def accept(self_stage, chunk, **kwargs):
        time.sleep(delay)
        return original(self_stage, chunk, **kwargs)
    return accept


class _StressFixture(_Fixture):
    def baseline_threads(self):
        # Let any start-up threads of freshly built managers settle first.
        time.sleep(0.05)
        return threading.active_count()

    def assert_threads_settled(self, baseline, slack=2, timeout=15.0):
        ok = wait_until(lambda: threading.active_count() <= baseline + slack, timeout)
        self.assertTrue(ok, f"thread leak: baseline={baseline} now={threading.active_count()} "
                            f"{sorted(t.name for t in threading.enumerate())}")

    def assert_no_orphan_parts(self, pair):
        """Every ``.part`` file under B's incoming root must belong to a journal."""
        incoming_root = pair.B.store("device:A").object_store_v2.incoming_root
        journals = {j.transfer_id for j in pair.B.stream_v2_journal_store().inventory().incoming}
        for part in _part_files(incoming_root):
            tid = os.path.basename(os.path.dirname(part))
            self.assertIn(tid, journals, f"orphan partial without journal: {part}")

    def wait_completed(self, pair, item, timeout=60):
        sender = wait_until(lambda: _sender_for(pair, item["item_id"]), timeout=timeout)
        self.assertTrue(sender, pair.A.stream_v2_status())
        self.assertTrue(wait_until(lambda: sender.state == "completed", timeout=timeout),
                        sender.status())
        self.assertTrue(wait_until(lambda: (pair.B.store("device:A").get_item(item["item_id"])
                                            or {}).get("payload", {}).get("encoding")
                                   == "object_manifest_v2", timeout=timeout))
        return sender


def _sender_for(pair, item_id):
    return next((s for s in _outgoing(pair) if s.item_id == item_id), None)


def _receiver_for(pair, item_id):
    return next((s for s in _incoming(pair) if s.item_id == item_id), None)


class ParserStressTests(unittest.TestCase):
    def test_ten_thousand_binary_frames_through_parser(self):
        rng = random.Random(1234)
        transfer_id = uuid.uuid4().hex
        expected = []
        stream = bytearray()
        controls = 0
        for i in range(10_000):
            if i % 100 == 0:
                stream += cfr2.encode_json_control_frame({"type": "ctl", "seq": i})
                controls += 1
            payload = bytes([i % 251]) * (i % 37 + 1)
            entry_index, offset = i % 5, i * 64
            expected.append((entry_index, offset, payload))
            stream += cfr2.encode_binary_payload_frame(transfer_id, entry_index, offset, payload)
        max_frame = max(len(cfr2.encode_binary_payload_frame(transfer_id, 0, 0, b"x" * 37)),
                        len(cfr2.encode_json_control_frame({"type": "ctl", "seq": 9900})))
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)

        def _writer():
            view = memoryview(bytes(stream))
            pos = 0
            while pos < len(view):
                size = rng.choice((1, 2, 3, 7, 64, 511, 4096, 65_000))
                a.sendall(view[pos:pos + size])
                pos += size

        writer = threading.Thread(target=_writer, daemon=True)
        started = time.monotonic()
        writer.start()
        reader = cfr2.TypedFrameReader(b)
        got_payload, got_controls, max_buf = 0, 0, 0
        while got_payload < 10_000:
            frame = reader.read_frame(5.0)
            self.assertIsNotNone(frame, "parser stalled")
            max_buf = max(max_buf, len(reader._buf))
            if isinstance(frame, cfr2.JsonControlFrame):
                self.assertEqual(frame.message, {"type": "ctl", "seq": got_payload})
                got_controls += 1
                continue
            entry_index, offset, payload = expected[got_payload]
            self.assertEqual(frame.transfer_id, transfer_id)
            self.assertEqual(frame.entry_index, entry_index)
            self.assertEqual(frame.offset, offset)
            self.assertEqual(frame.payload, payload)
            self.assertEqual(frame.checksum_kind, cfr2.CHECKSUM_SHA256)
            self.assertEqual(frame.checksum.hex(), sha(payload))
            got_payload += 1
        elapsed = time.monotonic() - started
        writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(got_payload, 10_000)
        self.assertEqual(got_controls, controls)
        self.assertEqual(controls, 100)
        self.assertLess(elapsed, 20.0)
        # Reader stays healthy: buffer drained, no failure latch, idle read is a clean timeout.
        self.assertEqual(len(reader._buf), 0)
        self.assertFalse(reader._failed)
        self.assertLessEqual(max_buf, cfr2._READ_SIZE + max_frame)
        self.assertIsNone(reader.read_frame(0.05))
        # Still usable for a further frame after the burst.
        a.sendall(cfr2.encode_json_control_frame({"type": "tail"}))
        self.assertEqual(reader.read_frame(5.0).message, {"type": "tail"})


class DisconnectResumeStressTests(_StressFixture):
    def test_hundred_disconnect_resume_cycles(self):
        cycles = 100
        pair = self.pair(settings_a=_settings(**BIG_SETTINGS), settings_b=_settings(**BIG_SETTINGS))
        baseline = self.baseline_threads()
        payload = os.urandom((cycles + 3) * MIB + 4321)
        spec = {"cycles/big.bin": payload, "cycles/tail.txt": b"tail after resume storm"}
        original = cstr2.IncomingTransferStage.accept
        breaks = {"count": 0}

        def breaking_accept(self_stage, chunk, **kwargs):
            written = original(self_stage, chunk, **kwargs)
            # Break after every accepted chunk until 100 disconnects happened.
            # ``pause(disconnected=True)`` then checkpoints exactly this chunk,
            # so each resume restarts from a strictly larger durable prefix.
            if breaks["count"] < cycles and not written.file_complete:
                breaks["count"] += 1
                pair.break_channels()
            return written

        with mock.patch.object(cstr2.IncomingTransferStage, "accept", breaking_accept):
            item = self.capture_and_sync(pair, spec)
            sender = self.wait_completed(pair, item, timeout=150)
        self.assertEqual(breaks["count"], cycles)
        self.assertEqual(sender.state, "completed")
        self.assertEqual(sender.bytes_done, item["size"])
        self.assertEqual(sender.status()["retry_count"], cycles)
        self.assertEqual(sender.journal.state, "completed")
        self.assertEqual(len(pair.messages("A", ctv2.T_RESUME_REQUEST)), cycles)
        responses = pair.messages("B", ctv2.T_RESUME_RESPONSE)
        self.assertEqual(len(responses), cycles)
        durable = [sum(f["durable_offset"] for f in r["files"]) for r in responses]
        self.assertEqual(durable, sorted(set(durable)), "resume offsets strictly increase")
        self.assertGreaterEqual(durable[0], MIB)
        self.assertEqual(durable[-1], cycles * MIB)
        self.assertEqual(sender.resume_bytes, cycles * MIB)
        self.assertEqual(len(pair.messages("A", ctv2.T_OFFER)), 1)
        journal = pair.B.stream_v2_journal_store().load("incoming", sender.transfer_id)
        self.assertEqual(journal.state, "completed")
        self.assertEqual(journal.retry_count, cycles)
        incoming_root = pair.B.store("device:A").object_store_v2.incoming_root
        self.assertFalse(os.path.lexists(os.path.join(incoming_root, sender.transfer_id)))
        self.assertEqual(_part_files(incoming_root), [])
        self.assert_materialized(pair, item["item_id"], spec)
        self.assertTrue(wait_until(lambda: not pair.A.transfer_activity_state()["busy"]))
        self.assertFalse(pair.B.transfer_activity_state()["busy"])
        self.assert_threads_settled(baseline)


class BatchStressTests(_StressFixture):
    # Several hundred files; the receiver commits its journal once per file
    # (checkpoint) and once more per file during finalization, so wall time grows
    # roughly quadratically with the file count on the productive path.
    FILES = 200

    def test_several_hundred_files_batch(self):
        # Finalization commits the incoming journal once per file (storage_state
        # "verified"), so the final-ack timeout must cover several hundred commits.
        timeouts = cctl2.TransferTimeouts(preflight=8, manifest_ack=8, window_ack=30,
                                          no_progress=30, reconnect_wait=15, final_complete_ack=90)
        pair = self.pair(timeouts=timeouts)
        rng = random.Random(99)
        spec = {}
        for i in range(self.FILES):
            size = (0 if i % 10 == 0 else rng.randrange(1, 16 * 1024 + 1))
            rel = f"batch/dir{i % 7}/sub{i % 3}/f\u00e4il\u00e9_\u30c6\u30b9\u30c8_{i:03d}.bin"
            spec[rel] = bytes(rng.getrandbits(8) for _ in range(size)) if size < 512 \
                else os.urandom(size)
        item = self.capture_and_sync(pair, spec)
        self.assertEqual(item["batch_manifest"]["file_count"], self.FILES)
        started = time.monotonic()
        receiver = wait_until(lambda: _receiver_for(pair, item["item_id"]), timeout=30)
        self.assertTrue(receiver, "receiver session registered while transferring")
        sender = self.wait_completed(pair, item, timeout=240)
        elapsed = time.monotonic() - started
        received = pair.B.store("device:A").get_item(item["item_id"])
        self.assertEqual(received["hash_state"], "verified")
        entries = [e for e in received["batch_manifest"]["entries"] if e["type"] == "file"]
        self.assertEqual(len(entries), self.FILES)
        expected = {rel.split("batch/", 1)[1]: sha(data) for rel, data in spec.items()}
        got = {e["path"].split("batch/", 1)[1]: e["sha256"] for e in entries}
        self.assertEqual(got, expected)
        status = sender.status()
        self.assertEqual(status["file_count"], self.FILES)
        self.assertEqual(status["bytes_done"], item["size"])
        self.assertEqual(status["percent"], 100.0)
        receiver_status = receiver.status()
        self.assertEqual(receiver_status["file_count"], self.FILES)
        self.assertEqual(receiver_status["bytes_done"], item["size"])
        self.assertEqual(receiver_status["state"], "completed")
        self.assertLess(elapsed, 240.0)
        # Wire: no legacy ZIP chunks and no Base64 payload fields on the control link.
        self.assertEqual(pair.messages("A", "clipboard_transfer_chunk"), [])
        self.assertEqual(pair.messages("A", "clipboard_transfer_start"), [])
        for who in ("A", "B"):
            with pair.lock:
                sent = list(pair.sent[who])
            for msg in sent:
                self.assertNotIn("data", msg, f"{who} sent a Base64 payload field: {msg.get('type')}")
        self.assertEqual(len(pair.messages("A", ctv2.T_OFFER)), 1)
        dest = os.path.join(self.root, "dest-count")
        result = pair.B.materialize_files_result("device:A", item["item_id"], dest)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(len(result["paths"]), 1)
        materialized = [os.path.join(b, f) for root in result["paths"]
                        for b, _d, fs in os.walk(root) for f in fs]
        self.assertEqual(len(materialized), self.FILES)
        # Exactly one lease marker sits next to the materialized root.
        markers = [f for f in os.listdir(os.path.dirname(result["paths"][0]))
                   if f.endswith(".active")]
        self.assertEqual(markers, [os.path.basename(result["paths"][0]) + ".active"])
        self.assert_materialized(pair, item["item_id"], spec)
        self.assertTrue(wait_until(lambda: not pair.A.transfer_activity_state()["busy"]))
        self.assertFalse(pair.B.transfer_activity_state()["busy"])


class CancellationStormTests(_StressFixture):
    def test_cancellation_storm(self):
        pair = self.pair()
        baseline = self.baseline_threads()
        rng = random.Random(7)
        patcher = mock.patch.object(cstr2.IncomingTransferStage, "accept", _slow_accept(0.05))
        patcher.start()
        self.addCleanup(patcher.stop)
        transfer_ids = []
        stale_ids = []
        for n in range(20):
            item = self.capture_and_sync(pair, {f"storm{n}/c.bin": os.urandom(3 * MIB + n)})
            iid = item["item_id"]
            self.assertTrue(wait_until(
                lambda: _receiver_for(pair, iid) is not None
                and MIB <= _receiver_for(pair, iid).bytes_done < 3 * MIB),
                f"iteration {n}: {pair.B.stream_v2_status()}")
            sender, receiver = _sender_for(pair, iid), _receiver_for(pair, iid)
            self.assertIsNotNone(sender)
            tid = sender.transfer_id
            self.assertEqual(receiver.transfer_id, tid)
            transfer_ids.append(tid)
            side = rng.choice(("A", "B"))
            manager = pair.A if side == "A" else pair.B
            result = manager.cancel_stream_v2_session(tid, "user")
            self.assertTrue(result["ok"], result)
            # Burst: duplicate cancels, unsolicited ACKs, stale ids, malformed messages.
            stale = uuid.uuid4().hex
            stale_ids.append(stale)
            burst = [
                (pair.A, "device:B", ctv2.build_cancel(tid, "user")),
                (pair.B, "device:A", ctv2.build_cancel(tid, "user")),
                (pair.A, "device:B", ctv2.build_cancel_ack(tid)),
                (pair.B, "device:A", ctv2.build_cancel_ack(tid)),
                (pair.A, "device:B", ctv2.build_cancel(stale, "item_removed")),
                (pair.B, "device:A", ctv2.build_cancel(stale, "item_removed")),
                (pair.A, "device:B", ctv2.build_cancel_ack(stale)),
                (pair.B, "device:A", ctv2.build_cancel_ack(stale)),
                (pair.A, "device:B", {"type": ctv2.T_CANCEL}),
                (pair.B, "device:A", {"type": ctv2.T_CANCEL, "schema_version": 1,
                                      "protocol_major": 2, "transfer_id": "zz", "reason": "x"}),
                (pair.B, "device:A", {"type": ctv2.T_CANCEL_ACK, "transfer_id": 12}),
                (pair.A, "device:X", ctv2.build_cancel(tid, "user")),
            ]
            rng.shuffle(burst)
            for _ in range(3):
                for target, identity, msg in burst:
                    handled = target.handle(identity, msg)
                    self.assertTrue(handled, [m for w, lvl, m in pair.logs
                                              if lvl == "WARN" and "rejected invalid" in m])
            self.assertTrue(wait_until(lambda: sender.state == "purged"), sender.status())
            self.assertTrue(wait_until(lambda: receiver.state == "purged"), receiver.status())
            warns = [msg for who, level, msg in pair.logs if level == "WARN" and tid in msg]
            self.assertIn(sender.cancel_reason, ("user", "peer:user"), warns)
            self.assertIn(receiver.cancel_reason, ("user", "peer:user"), warns)
            self.assertTrue(wait_until(lambda: not sender.is_alive()))
            self.assertTrue(receiver.join(5))
            # Drop the source so the next history sync does not re-request it.
            self.assertTrue(pair.A.store("device:B").delete_item(item["item_id"]))
            self.assertFalse(pair.B.store("device:A").get_item(item["item_id"])["available"])
            if n % 2:
                # Alternate: half the iterations prune via maintenance, half leave the
                # purged sender registered so the next round starts with stale records.
                pair.A.run_stream_v2_maintenance(now=time.monotonic())
                self.assertIsNone(pair.A._stream_v2_record(tid))
        self.assertEqual(len(set(transfer_ids)), 20)
        # A premature (injected) cancel_ack can let B purge before an in-flight
        # resume_request from the sender thread arrives; B then opens a fresh stage
        # nobody feeds. That is bounded by the preflight timeout: after it fires,
        # nothing may remain on either side.
        later = time.monotonic() + FAST.preflight + 1
        pair.A.run_stream_v2_maintenance(now=later)
        pair.B.run_stream_v2_maintenance(now=later)
        self.assertEqual(pair.A._stream_v2_sessions, {})
        leftovers = [(rec["stage"].transfer_id in transfer_ids, rec["stage"].status())
                     for rec in pair.B._stream_v2_sessions.values()]
        self.assertEqual(pair.B._stream_v2_sessions, {}, leftovers)
        self.assertFalse(pair.A.transfer_activity_state()["busy"])
        self.assertFalse(pair.B.transfer_activity_state()["busy"])
        for manager in (pair.A, pair.B):
            inventory = manager.stream_v2_journal_store().inventory()
            self.assertEqual(inventory.incoming, ())
            self.assertEqual(inventory.outgoing, ())
            self.assertEqual(inventory.quarantined, ())
        incoming_root = pair.B.store("device:A").object_store_v2.incoming_root
        self.assertEqual(sorted(os.listdir(incoming_root)) if os.path.isdir(incoming_root) else [],
                         [])
        with pair.lock:
            type_counts = {who: collections.Counter(m.get("type") for m in pair.sent[who])
                           for who in ("A", "B")}
        self.assertEqual(len(pair.messages("A", ctv2.T_OFFER)), 20, type_counts)
        # Every transfer produced a wire-level cancel exchange; naturally sent
        # cancels never reference unknown transfers (only ACKs answer the stale ids).
        natural_cancels = {m["transfer_id"] for who in ("A", "B")
                           for m in pair.messages(who, ctv2.T_CANCEL)}
        natural_acks = {m["transfer_id"] for who in ("A", "B")
                        for m in pair.messages(who, ctv2.T_CANCEL_ACK)}
        self.assertLessEqual(natural_cancels, set(transfer_ids))
        self.assertEqual(natural_cancels | natural_acks >= set(transfer_ids), True,
                         set(transfer_ids) - (natural_cancels | natural_acks))
        self.assertEqual(natural_acks - set(transfer_ids), set(stale_ids))
        self.assert_threads_settled(baseline)


class StatusPollingStressTests(_StressFixture):
    def test_parallel_status_polling_during_transfer(self):
        # Eight pollers contend for the manager locks while the receiver
        # verifies, publishes and ACKs 8 MiB; on shared CI runners that phase
        # exceeded the 8 s FAST final-ack deadline, so the deadlines here are
        # the productive defaults rather than the fast test values.
        timeouts = cctl2.TransferTimeouts(preflight=30, manifest_ack=30, window_ack=30,
                                          no_progress=60, reconnect_wait=15,
                                          final_complete_ack=90)
        pair = self.pair(timeouts=timeouts)
        patcher = mock.patch.object(cstr2.IncomingTransferStage, "accept", _slow_accept(0.02))
        patcher.start()
        self.addCleanup(patcher.stop)
        stop = threading.Event()
        snapshots = []
        errors = []
        lock = threading.Lock()

        def _poll(n):
            try:
                while not stop.is_set():
                    for manager, identity in ((pair.A, "device:B"), (pair.B, "device:A")):
                        statuses = manager.stream_v2_status()
                        diag = manager.diagnostics(identity)
                        if n % 2:
                            manager.run_stream_v2_maintenance()
                        else:
                            manager.transfer_activity_state()
                        with lock:
                            snapshots.append((statuses, diag))
            except Exception as exc:  # pragma: no cover - reported via assertion
                with lock:
                    errors.append(repr(exc))

        pollers = [threading.Thread(target=_poll, args=(n,), daemon=True) for n in range(8)]
        for t in pollers:
            t.start()
        try:
            item = self.capture_and_sync(pair, {"poll/big.bin": os.urandom(8 * MIB + 17),
                                                "poll/small.txt": b"small"})
            sender = self.wait_completed(pair, item, timeout=60)
        finally:
            stop.set()
            for t in pollers:
                t.join(10)
        self.assertEqual(errors, [])
        self.assertTrue(all(not t.is_alive() for t in pollers))
        self.assertEqual(sender.state, "completed")
        self.assertEqual(sender.bytes_done, item["size"])
        self.assertGreaterEqual(len(snapshots), 8)
        observed = [s for statuses, _d in snapshots for s in statuses]
        self.assertTrue(observed, "pollers observed live sessions")
        states = {s["state"] for s in observed}
        self.assertIn("transferring", states)
        self.assertTrue(states & {"receiving", "transferring"})
        root_variants = {self.root, self.root.replace("\\", "\\\\"), self.root.replace("\\", "/")}
        for status in observed:
            self.assertTrue(STATUS_KEYS <= set(status), STATUS_KEYS - set(status))
            self.assertEqual(status["strategy"], "stream_v2")
            self.assertEqual(status["item_id"], item["item_id"])
            self.assertEqual(status["transfer_id"], sender.transfer_id)
            self.assertEqual(status["file_count"], 2)
            self.assertEqual(status["total_bytes"], item["size"])
            self.assertLessEqual(status["bytes_done"], item["size"])
            self.assertIn(status["direction"], ("outgoing", "incoming"))
            for text in _strings(status):
                self.assertNotIn(":\\", text, status)
                for variant in root_variants:
                    self.assertNotIn(variant, text, status)
        for _statuses, diag in snapshots:
            self.assertIn("stream_v2", diag)
            for text in _strings(diag["stream_v2"]):
                self.assertNotIn(":\\", text)
        self.assert_materialized(pair, item["item_id"], {"poll/small.txt": b"small"})


class SlowReceiverTests(_StressFixture):
    def test_slow_receiver_bounds_sender_memory_and_queue(self):
        limits = cfc2.FlowControlLimits(chunk_size=MIB, max_inflight_chunks_per_transfer=2,
                                        max_inflight_bytes_global=2 * MIB,
                                        window_ack_timeout_seconds=FAST.window_ack)
        pair = self.pair(limits=limits)
        original_track = cfc2.SendWindow.track_sent
        observed = {"chunks": [], "bytes": [], "global": []}

        def tracking(self_window, entry_index, offset, payload, timeout=None):
            result = original_track(self_window, entry_index, offset, payload, timeout=timeout)
            snap = self_window.snapshot()
            observed["chunks"].append(snap["inflight_chunks"])
            observed["bytes"].append(snap["inflight_bytes"])
            observed["global"].append(pair.A._stream_v2_flow.snapshot()["inflight_bytes"])
            return result

        with mock.patch.object(cstr2.IncomingTransferStage, "accept", _slow_accept(0.04)), \
                mock.patch.object(cfc2.SendWindow, "track_sent", tracking):
            item = self.capture_and_sync(pair, {"slow.bin": os.urandom(10 * MIB + 5)})
            sender = self.wait_completed(pair, item, timeout=60)
        self.assertEqual(len(observed["chunks"]), 11, "one track per chunk, nothing re-sent")
        self.assertEqual(max(observed["chunks"]), 2)
        self.assertLessEqual(max(observed["bytes"]), 2 * MIB)
        self.assertLessEqual(max(observed["global"]), 2 * MIB)
        self.assertGreaterEqual(observed["chunks"].count(2), 5, "window was actually filled")
        self.assertEqual(sender.bytes_done, item["size"])
        flow = pair.A._stream_v2_flow.snapshot()
        self.assertEqual(flow["active_transfers"], 0)
        self.assertEqual(flow["inflight_bytes"], 0)
        self.assertEqual(sender.status()["retry_count"], 0)
        self.assertEqual(sender.error_code, None)


class DiskFullTests(_StressFixture):
    def test_simulated_disk_full_on_receiver_pauses_and_resumes(self):
        pair = self.pair()
        baseline = self.baseline_threads()
        original = cstr2.IncomingTransferStage.accept
        calls = {"n": 0}

        class FullDisk:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def write(self, _payload):
                raise OSError(errno.ENOSPC, "no space left on device")

        def failing_accept(self_stage, chunk, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                self_stage.checkpoint()  # productive forced checkpoint: chunk 1 is durable
                self_stage._file = FullDisk(self_stage._file)
            return original(self_stage, chunk, **kwargs)

        patcher = mock.patch.object(cstr2.IncomingTransferStage, "accept", failing_accept)
        patcher.start()
        payload = os.urandom(5 * MIB + 9)
        item = self.capture_and_sync(pair, {"full.bin": payload})
        self.assertTrue(wait_until(lambda: _incoming(pair) and _incoming(pair)[0].state == "failed"),
                        pair.B.stream_v2_status())
        receiver = _incoming(pair)[0]
        sender = _outgoing(pair)[0]
        tid = sender.transfer_id
        self.assertEqual(receiver.error_code, "disk_full")
        self.assertEqual(receiver.stage.failure_code, "disk_full")
        journal = pair.B.stream_v2_journal_store().load("incoming", tid)
        self.assertEqual(journal.state, "failed")
        self.assertEqual(journal.entries[0]["durable_offset"], MIB)
        incoming_root = pair.B.store("device:A").object_store_v2.incoming_root
        partial = os.path.join(incoming_root, tid, "0.part")
        self.assertTrue(os.path.isfile(partial))
        self.assertEqual(os.path.getsize(partial), MIB)
        # The sender lost its channel and now waits/asks for a resume.
        self.assertTrue(wait_until(lambda: sender.state in ("waiting_reconnect", "accepted",
                                                            "transferring", "completed")
                                   or sender.state == "failed"), sender.status())
        patcher.stop()
        sender_ok = wait_until(lambda: sender.state == "completed", timeout=40)
        self.assertTrue(sender_ok, sender.status())
        self.assertEqual(sender.status()["retry_count"], 1)
        self.assertEqual(sender.resume_bytes, MIB, "resume started from the durable prefix")
        self.assertEqual(sender.bytes_done, item["size"])
        self.assertEqual(len(pair.messages("A", ctv2.T_RESUME_REQUEST)), 1)
        responses = pair.messages("B", ctv2.T_RESUME_RESPONSE)
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["files"][0]["durable_offset"], MIB)
        self.assertEqual(pair.messages("B", ctv2.T_REJECT), [])
        journal = pair.B.stream_v2_journal_store().load("incoming", tid)
        self.assertEqual(journal.state, "completed")
        self.assertEqual(journal.retry_count, 1)
        self.assertFalse(os.path.lexists(os.path.join(incoming_root, tid)))
        self.assert_materialized(pair, item["item_id"], {"full.bin": payload})
        received = pair.B.store("device:A").get_item(item["item_id"])
        self.assertEqual(received["hash_state"], "verified")
        self.assertEqual(received["batch_manifest"]["entries"][0]["sha256"], sha(payload))
        self.assertTrue(wait_until(lambda: not pair.A.transfer_activity_state()["busy"]))
        self.assertFalse(pair.B.transfer_activity_state()["busy"])
        self.assert_threads_settled(baseline)


class RestartLeakTests(_StressFixture):
    def test_no_thread_or_worker_leak_across_repeated_restarts(self):
        pair = self.pair()
        baseline = self.baseline_threads()
        items = []
        specs = []
        transfer_ids = []
        for i in range(10):
            specs.append({f"r{i}a/x.bin": os.urandom(MIB + i)})
            items.append(self.capture_and_sync(pair, specs[-1]))
            transfer_ids.append(self.wait_completed(pair, items[-1]).transfer_id)
            pair.restart_a()
            pair.A.on_peer_connected("B", "device:B")
            pair.B.on_peer_connected("A", "device:A")
            self.assertEqual(pair.A.resume_outgoing_stream_v2("device:B"), [])
            specs.append({f"r{i}b/y.bin": os.urandom(MIB + 2 * i)})
            items.append(self.capture_and_sync(pair, specs[-1]))
            last_sender = self.wait_completed(pair, items[-1])
            transfer_ids.append(last_sender.transfer_id)
        self.assertEqual(len(items), 20)
        self.assertEqual(len(set(transfer_ids)), 20)
        self.assertEqual(len({it["item_id"] for it in items}), 20)
        for who in ("A", "B"):
            self.assertTrue(wait_until(lambda: all(
                s["state"] in ("completed", "purged") for s in
                (pair.A if who == "A" else pair.B).stream_v2_status())))
        pair.A.run_stream_v2_maintenance(now=time.monotonic())
        pair.B.run_stream_v2_maintenance(now=time.monotonic())
        self.assertEqual(pair.A._stream_v2_sessions, {})
        self.assertEqual(pair.B._stream_v2_sessions, {})
        # Retired managers hold only their finished sender records (the first one
        # sent a single item, every later one two): terminal, no live worker.
        retired_records = [rec for retired in pair.retired
                           for rec in retired._stream_v2_sessions.values()]
        self.assertEqual([len(r._stream_v2_sessions) for r in pair.retired], [1] + [2] * 9,
                         [[(rec["stage"].item_id[:6], rec["stage"].state)
                           for rec in r._stream_v2_sessions.values()] for r in pair.retired])
        self.assertEqual([r["stage"].state for r in retired_records], ["completed"] * 19)
        self.assertFalse(any(r["stage"].is_alive() for r in retired_records))
        for retired in pair.retired:
            self.assertFalse(retired._accepting_work)
        self.assertEqual(len(pair.retired), 10)
        # Journals: the receiver keeps one completed journal per transfer; the
        # sender purges terminal leftovers on every peer reconnect, so only the
        # last transfer's completed journal is left, and one more reconnect
        # scan removes it without re-offering anything.
        inv_a = pair.A.stream_v2_journal_store().inventory()
        inv_b = pair.B.stream_v2_journal_store().inventory()
        self.assertEqual([(j.state, j.transfer_id) for j in inv_a.outgoing],
                         [("completed", last_sender.transfer_id)])
        self.assertEqual(sorted(j.state for j in inv_b.incoming), ["completed"] * 20)
        self.assertEqual(sorted(j.transfer_id for j in inv_b.incoming), sorted(transfer_ids))
        self.assertEqual(pair.A.resume_outgoing_stream_v2("device:B"), [])
        self.assertEqual(pair.A.stream_v2_journal_store().inventory().outgoing, ())
        self.assertEqual(inv_a.incoming, ())
        self.assertEqual(inv_b.outgoing, ())
        self.assertEqual(inv_a.quarantined + inv_b.quarantined, ())
        incoming_root = pair.B.store("device:A").object_store_v2.incoming_root
        self.assertEqual(_part_files(incoming_root), [])
        self.assert_no_orphan_parts(pair)
        self.assertEqual(pair.B.stats["received_items"], 20)
        self.assertEqual(len(pair.messages("A", ctv2.T_OFFER)), 20)
        self.assertEqual(pair.messages("A", ctv2.T_RESUME_REQUEST), [])
        self.assertFalse(pair.A.transfer_activity_state()["busy"])
        self.assertFalse(pair.B.transfer_activity_state()["busy"])
        self.assert_threads_settled(baseline)
        for it, spec in zip(items[-2:], specs[-2:]):
            self.assert_materialized(pair, it["item_id"], spec)


class MalformedBurstTests(_StressFixture):
    def test_malformed_frame_burst_is_contained(self):
        pair = self.pair()
        baseline = self.baseline_threads()
        paths = make_sources(self.src, {"hostile.bin": os.urandom(3000)})
        item = cf.make_file_item(paths)
        transfer_id = uuid.uuid4().hex
        pair.B.handle("device:A", ctv2.build_offer(transfer_id, item["batch_manifest"],
                                                   device_id="A", profile_id="p"))
        accepts = [m for m in pair.messages("B", ctv2.T_ACCEPT) if m["transfer_id"] == transfer_id]
        self.assertEqual(len(accepts), 1)
        nonce = ctv2.parse_accept(accepts[0])["channel_nonce"]
        a, b = socket.socketpair()
        self.addCleanup(lambda: (a.close(), b.close()))
        hello = ctv2.build_channel_hello(transfer_id, nonce, "A")
        threading.Thread(target=lambda: pair.B.accept_stream_v2_channel(
            b, rm.FramedReader(b).read_message(5.0)), daemon=True).start()
        sock = ctv2.open_channel_socket(lambda _t: a, hello, 5.0)
        receiver = pair.B._stream_v2_receiver(transfer_id)
        self.assertEqual(receiver.state, "receiving")
        rng = random.Random(4242)
        other = uuid.uuid4().hex
        variants = [
            struct.pack("!I", 3) + b"\x07\x02\x00",                                # unknown kind
            struct.pack("!I", cfr2.MAX_TYPED_FRAME_BYTES + 1) + b"\x02\x02\x00\x00",  # oversized
            struct.pack("!I", 3) + b"\x01\x09\x00",                                # bad major
            struct.pack("!I", 3) + b"\x01\x02\x01",                                # flags set
            struct.pack("!I", 1) + b"\x01",                                        # short header
            cfr2.encode_binary_payload_frame(other, 0, 0, b"x" * 100),             # wrong transfer
            cfr2.encode_binary_payload_frame(transfer_id, 0, 0, b"x" * 100),       # before manifest
            cfr2.encode_json_control_frame({"type": "bogus"}),                     # unexpected ctl
            struct.pack("!I", 10) + b"\x02\x02\x00" + b"\x00" * 7,                 # truncated binary
        ]
        sent, refused = 0, 0
        for _ in range(1000):
            frame = rng.choice(variants)
            try:
                sock.sendall(frame)
                sent += 1
            except OSError:
                refused += 1
        self.assertEqual(sent + refused, 1000)
        # The very first malformed frame is fatal: the receiver cancels once and closes.
        self.assertTrue(wait_until(lambda: receiver.state == "cancelled"), receiver.status())
        self.assertEqual(receiver.cancel_reason, "protocol_error")
        self.assertEqual(receiver.bytes_done, 0)
        self.assertFalse(receiver.has_channel())
        self.assertTrue(receiver.join(5), "receiver channel thread exited")
        sock.settimeout(2.0)
        try:
            data = sock.recv(16)
        except OSError:
            data = b""
        self.assertEqual(data, b"", "receiver end of the channel is closed")
        with self.assertRaises(OSError):
            for _ in range(64):
                sock.sendall(variants[0])
        cancels = [m for m in pair.messages("B", ctv2.T_CANCEL) if m["transfer_id"] == transfer_id]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0]["reason"], "protocol_error")
        # A never had this session: it acknowledges so B can purge partials + journal.
        self.assertTrue(wait_until(lambda: receiver.state == "purged"), receiver.status())
        acks = [m for m in pair.messages("A", ctv2.T_CANCEL_ACK) if m["transfer_id"] == transfer_id]
        self.assertEqual(len(acks), 1)
        incoming_root = pair.B.store("device:A").object_store_v2.incoming_root
        self.assertFalse(os.path.lexists(os.path.join(incoming_root, transfer_id)))
        with self.assertRaises(cres2.ResumeJournalError):
            pair.B.stream_v2_journal_store().load("incoming", transfer_id)
        self.assertFalse(pair.B.transfer_activity_state()["busy"])
        self.assertEqual(len(pair.B._stream_v2_nonces), 0)
        # The runtime still serves a valid transfer afterwards.
        spec = {"after/valid.bin": os.urandom(2 * MIB + 3), "after/t.txt": b"ok"}
        valid = self.capture_and_sync(pair, spec)
        sender = self.wait_completed(pair, valid)
        self.assertEqual(sender.bytes_done, valid["size"])
        self.assertEqual(pair.B.stream_v2_journal_store().load(
            "incoming", sender.transfer_id).state, "completed")
        self.assert_materialized(pair, valid["item_id"], spec)
        self.assertTrue(wait_until(lambda: not pair.A.transfer_activity_state()["busy"]))
        self.assertFalse(pair.B.transfer_activity_state()["busy"])
        self.assert_threads_settled(baseline)


class LargeOffsetTests(unittest.TestCase):
    def test_64bit_offsets_over_4gib_without_physical_files(self):
        huge = 2 ** 33 + 4097        # 8 GiB + 4097 bytes, logical only
        small = 5
        entries = [
            {"path": "huge.bin", "type": "file", "size": huge, "mtime_ns": 7,
             "source_fingerprint": {"size": huge, "file_id": 1}, "hash_state": "unhashed",
             "sha256": None},
            {"path": "small.bin", "type": "file", "size": small, "mtime_ns": 7,
             "source_fingerprint": {"size": small, "file_id": 2}, "hash_state": "unhashed",
             "sha256": None},
        ]
        manifest = cman2.validate_manifest(cman2.build_manifest("item-huge", 1, entries))
        self.assertEqual(manifest["total_size"], huge + small)
        self.assertGreater(manifest["total_size"], 2 ** 32)
        import tempfile
        with tempfile.TemporaryDirectory(prefix="fs-huge-journal-") as root:
            store = cres2.ResumeJournalStore(root)
            ids = dict(transfer_id=uuid.uuid4().hex, peer_id="peer-b", profile_id="profile",
                       provider_id="device:B")
            journal = store.create_outgoing(manifest=manifest, **ids)
            journal = store.commit(cres2.update_journal_state(journal, "accepted"), journal.generation)
            journal = store.commit(cres2.update_journal_state(journal, "sending_manifest"),
                                   journal.generation)
            journal = store.commit(cres2.update_journal_state(journal, "transferring"),
                                   journal.generation)
            verified, durable = 2 ** 33 + 1, 2 ** 32 + 4 * MIB
            candidate = cres2.update_outgoing_progress(
                journal, 0, receiver_verified_offset=verified, receiver_durable_offset=durable)
            committed = store.commit(candidate, journal.generation)
            loaded = store.load("outgoing", ids["transfer_id"])
            self.assertEqual(loaded.entries[0]["receiver_verified_offset"], verified)
            self.assertEqual(loaded.entries[0]["receiver_durable_offset"], durable)
            self.assertEqual(loaded.generation, committed.generation)
            self.assertEqual(loaded.entries[0]["size"], huge)
            document = json.loads(cres2.canonical_journal_bytes(loaded))
            self.assertEqual(document["entries"][0]["receiver_durable_offset"], durable)
            # Resume plan from receiver-reported durable evidence beyond 4 GiB.
            files = [{"index": 0, "durable_offset": 2 ** 33, "prefix_sha256": "a" * 64,
                      "completed": False},
                     {"index": 1, "durable_offset": 0, "prefix_sha256": None, "completed": False}]
            plan = ctv2.resume_plan_from_files(loaded, manifest, files)
            self.assertEqual(plan.durable_bytes, 2 ** 33)
            self.assertEqual([f.durable_offset for f in plan.files], [2 ** 33, 0])
            self.assertEqual(plan.files[0].size, huge)
            # Regression below the receiver's committed durable offset is refused.
            with self.assertRaises(ctv2.TransportV2Error) as ctx:
                ctv2.resume_plan_from_files(loaded, manifest, [dict(files[0], durable_offset=1)])
            self.assertEqual(ctx.exception.code, "resume_mismatch")
            # Offsets above the 64-bit wire range never enter a journal.
            with self.assertRaises(cres2.ResumeJournalError):
                cres2.update_outgoing_progress(
                    loaded, 0, receiver_verified_offset=2 ** 64, receiver_durable_offset=0)
        # Framing: offset 2**33 round-trips exactly and stays under the logical ceiling.
        transfer_id = ids["transfer_id"]
        frame = cfr2.encode_binary_payload_frame(transfer_id, 0, 2 ** 33, b"tail")
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.sendall(frame)
        decoded = cfr2.TypedFrameReader(b).read_frame(5.0)
        self.assertIsInstance(decoded, cfr2.BinaryPayloadFrame)
        self.assertEqual(decoded.offset, 2 ** 33)
        self.assertEqual(decoded.entry_index, 0)
        self.assertEqual(decoded.transfer_id, transfer_id)
        self.assertEqual(decoded.payload, b"tail")
        # Raw wire layout: length(4) + common header(4) + uuid(16) + entry_index(4) + offset(8).
        self.assertEqual(struct.unpack("!Q", frame[28:36])[0], 2 ** 33)
        # Window ACK bookkeeping accepts > 4 GiB offsets too.
        coordinator = cfc2.FlowControlCoordinator(cfc2.FlowControlLimits(chunk_size=MIB))
        window = coordinator.open_transfer(transfer_id, "device:B")
        window.initialize_resume(0, 2 ** 33)
        window.track_sent(0, 2 ** 33, b"tail")
        snap = window.snapshot()
        self.assertEqual(snap["inflight_bytes"], 4)
        self.assertEqual(snap["durable_offsets"], {0: 2 ** 33})
        coordinator.shutdown()


if __name__ == "__main__":
    unittest.main()
