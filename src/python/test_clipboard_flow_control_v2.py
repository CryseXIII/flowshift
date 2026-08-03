"""Focused tests for bounded clipboard stream V2 flow control and ACKs."""
from __future__ import annotations

import threading
import time
import unittest
import uuid
import socket

import clipboard_flow_control_v2 as flow
import clipboard_framing_v2 as framing
import clipboard_protocol as protocol


def transfer_id():
    return uuid.uuid4().hex


class LimitsAndQueueTests(unittest.TestCase):
    def test_default_limits_match_architecture(self):
        limits = flow.FlowControlLimits()
        self.assertEqual(limits.chunk_size, 2 * flow.MIB)
        self.assertEqual(limits.max_active_transfers_global, 2)
        self.assertEqual(limits.max_active_transfers_per_peer, 1)
        self.assertEqual(limits.max_inflight_chunks_per_transfer, 4)
        self.assertEqual(limits.max_inflight_bytes_global, 16 * flow.MIB)

    def test_invalid_limits_are_rejected(self):
        for kwargs in (
                {"chunk_size": flow.MIB - 1},
                {"chunk_size": 4 * flow.MIB + 1},
                {"max_active_transfers_global": 0},
                {"max_active_transfers_global": 1,
                 "max_active_transfers_per_peer": 2},
                {"ack_interval_seconds": float("inf")},
                {"window_ack_timeout_seconds": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                flow.FlowControlLimits(**kwargs)

    def test_queue_blocks_by_count_and_releases_producer(self):
        chunks = flow.BoundedChunkQueue(1, 4)
        chunks.put(b"1234")
        result = []

        def producer():
            chunks.put(b"x", timeout=1)
            result.append("put")

        thread = threading.Thread(target=producer)
        thread.start()
        time.sleep(0.03)
        self.assertEqual(chunks.snapshot()["waiting_producers"], 1)
        self.assertEqual(chunks.get(), b"1234")
        thread.join(1)
        self.assertEqual(result, ["put"])
        self.assertEqual(chunks.snapshot()["high_water_bytes"], 4)

    def test_queue_enforces_bytes_and_close_wakes_waiters(self):
        chunks = flow.BoundedChunkQueue(3, 4)
        chunks.put(b"123")
        errors = []

        def producer():
            try:
                chunks.put(b"12")
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=producer)
        thread.start()
        time.sleep(0.03)
        chunks.close()
        thread.join(1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], flow.FlowControlClosed)
        self.assertEqual(chunks.snapshot()["queued_bytes"], 0)

    def test_queue_timeout_and_oversized_item(self):
        chunks = flow.BoundedChunkQueue(1, 2)
        with self.assertRaises(flow.FlowControlError):
            chunks.put(b"123")
        chunks.put(b"12")
        with self.assertRaises(flow.FlowControlTimeout):
            chunks.put(b"x", timeout=0.01)

    def test_custom_queue_items_require_immutable_freeze(self):
        with self.assertRaises(ValueError):
            flow.BoundedChunkQueue(1, 2, size_fn=lambda item: len(item[0]))
        source = bytearray(b"ab")
        chunks = flow.BoundedChunkQueue(
            1, 2, size_fn=lambda item: len(item[0]),
            freeze_fn=lambda item: (bytes(item[0]),))
        chunks.put((source,))
        source[:] = b"zz"
        self.assertEqual(chunks.get(), (b"ab",))
        plain = flow.BoundedChunkQueue(1, 8)
        with self.assertRaises(flow.FlowControlError):
            plain.put([])


class AdmissionAndWindowTests(unittest.TestCase):
    def limits(self, **updates):
        values = {
            "max_inflight_chunks_per_transfer": 2,
            "max_inflight_bytes_global": 6,
            "ack_bytes": 4,
            "ack_chunks": 2,
            "ack_interval_seconds": 0.02,
            "window_ack_timeout_seconds": 0.02,
        }
        values.update(updates)
        return flow.FlowControlLimits(**values)

    def test_global_and_per_peer_admission(self):
        coordinator = flow.FlowControlCoordinator(self.limits())
        first = coordinator.open_transfer(transfer_id(), "peer-a")
        second = coordinator.open_transfer(transfer_id(), "peer-b")
        with self.assertRaises(flow.FlowControlTimeout):
            coordinator.open_transfer(transfer_id(), "peer-c")
        with self.assertRaises(flow.FlowControlTimeout):
            coordinator.open_transfer(transfer_id(), "peer-a")
        first.abort()
        third = coordinator.open_transfer(transfer_id(), "peer-a")
        self.assertEqual(coordinator.snapshot()["active_transfers"], 2)
        second.abort()
        third.abort()
        self.assertEqual(coordinator.snapshot()["active_per_peer"], {})

    def test_duplicate_transfer_and_invalid_identity_are_rejected(self):
        coordinator = flow.FlowControlCoordinator(self.limits())
        identifier = transfer_id()
        coordinator.open_transfer(identifier, "peer-a")
        with self.assertRaises(flow.FlowControlError):
            coordinator.open_transfer(identifier, "peer-b")
        with self.assertRaises(flow.FlowControlError):
            coordinator.open_transfer(transfer_id(), "")

    def test_cumulative_ack_releases_exact_chunks(self):
        coordinator = flow.FlowControlCoordinator(self.limits())
        window = coordinator.open_transfer(transfer_id(), "peer-a")
        window.track_sent(0, 0, b"abc")
        window.track_sent(0, 3, b"de")
        ack = protocol.build_stream_v2_ack(window.transfer_id, 0, 5)
        released = window.apply_ack(ack)
        self.assertEqual(released["released_chunks"], 2)
        self.assertEqual(released["released_bytes"], 5)
        self.assertEqual(released["receiver_state"], "transferring")
        self.assertEqual(window.snapshot()["inflight_chunks"], 0)
        self.assertEqual(coordinator.snapshot()["inflight_bytes"], 0)

    def test_window_blocks_until_ack(self):
        coordinator = flow.FlowControlCoordinator(self.limits(
            window_ack_timeout_seconds=1))
        window = coordinator.open_transfer(transfer_id(), "peer-a")
        window.track_sent(0, 0, b"abc")
        window.track_sent(0, 3, b"de")
        result = []

        def sender():
            window.track_sent(0, 5, b"x", timeout=1)
            result.append("sent")

        thread = threading.Thread(target=sender)
        thread.start()
        time.sleep(0.03)
        self.assertEqual(result, [])
        window.apply_ack(protocol.build_stream_v2_ack(window.transfer_id, 0, 3))
        thread.join(1)
        self.assertEqual(result, ["sent"])
        self.assertEqual(window.snapshot()["inflight_bytes"], 3)

    def test_global_byte_budget_is_shared(self):
        coordinator = flow.FlowControlCoordinator(self.limits())
        first = coordinator.open_transfer(transfer_id(), "peer-a")
        second = coordinator.open_transfer(transfer_id(), "peer-b")
        first.track_sent(0, 0, b"1234")
        second.track_sent(0, 0, b"12")
        with self.assertRaises(flow.FlowControlTimeout):
            second.track_sent(0, 2, b"x", timeout=0.01)
        first.apply_ack(protocol.build_stream_v2_ack(first.transfer_id, 0, 4))
        second.track_sent(0, 2, b"x", timeout=0.1)
        self.assertEqual(coordinator.snapshot()["inflight_bytes"], 3)

    def test_stale_wrong_and_invalid_ack_semantics(self):
        coordinator = flow.FlowControlCoordinator(self.limits())
        window = coordinator.open_transfer(transfer_id(), "peer-a")
        window.track_sent(0, 0, b"abcd")
        wrong = protocol.build_stream_v2_ack(transfer_id(), 0, 4)
        self.assertEqual(window.apply_ack(wrong)["released_bytes"], 0)
        stale = protocol.build_stream_v2_ack(window.transfer_id, 1, 0)
        self.assertEqual(window.apply_ack(stale)["released_bytes"], 0)
        with self.assertRaises(flow.FlowControlError):
            window.apply_ack(protocol.build_stream_v2_ack(window.transfer_id, 0, 2))
        with self.assertRaises(flow.FlowControlError):
            window.apply_ack(protocol.build_stream_v2_ack(window.transfer_id, 0, 5))
        valid = protocol.build_stream_v2_ack(window.transfer_id, 0, 4)
        self.assertEqual(window.apply_ack(valid)["released_bytes"], 4)
        self.assertEqual(window.apply_ack(valid)["released_bytes"], 0)

    def test_ack_timeout_and_shutdown_wake(self):
        coordinator = flow.FlowControlCoordinator(self.limits())
        window = coordinator.open_transfer(transfer_id(), "peer-a")
        window.track_sent(0, 0, b"abcd")
        time.sleep(0.03)
        with self.assertRaises(flow.FlowControlTimeout):
            window.check_ack_timeout()
        coordinator.shutdown()
        coordinator = flow.FlowControlCoordinator(self.limits(
            window_ack_timeout_seconds=1))
        window = coordinator.open_transfer(transfer_id(), "peer-a")
        window.track_sent(0, 0, b"abcd")
        errors = []

        def waiter():
            try:
                window.wait_until_empty()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.005)
        coordinator.shutdown()
        thread.join(1)
        self.assertIsInstance(errors[0], flow.FlowControlClosed)
        self.assertEqual(coordinator.snapshot()["inflight_bytes"], 0)

    def test_default_window_wait_has_a_finite_ack_deadline(self):
        coordinator = flow.FlowControlCoordinator(self.limits())
        window = coordinator.open_transfer(transfer_id(), "peer-a")
        window.track_sent(0, 0, b"x")
        started = time.monotonic()
        with self.assertRaises(flow.FlowControlTimeout):
            window.wait_until_empty()
        self.assertLess(time.monotonic() - started, 0.2)

    def test_explicit_sender_timeout_is_capped_by_ack_deadline(self):
        coordinator = flow.FlowControlCoordinator(self.limits())
        window = coordinator.open_transfer(transfer_id(), "peer-a")
        window.track_sent(0, 0, b"abc")
        window.track_sent(0, 3, b"def")
        started = time.monotonic()
        with self.assertRaises(flow.FlowControlTimeout):
            window.track_sent(0, 6, b"x", timeout=5)
        self.assertLess(time.monotonic() - started, 0.2)

    def test_explicit_drain_timeout_is_capped_by_ack_deadline(self):
        coordinator = flow.FlowControlCoordinator(self.limits())
        window = coordinator.open_transfer(transfer_id(), "peer-a")
        window.track_sent(0, 0, b"x")
        started = time.monotonic()
        with self.assertRaises(flow.FlowControlTimeout):
            window.wait_until_empty(timeout=5)
        self.assertLess(time.monotonic() - started, 0.2)

    def test_receiver_terminal_state_closes_window_and_releases_admission(self):
        coordinator = flow.FlowControlCoordinator(self.limits())
        window = coordinator.open_transfer(transfer_id(), "peer-a")
        window.track_sent(0, 0, b"abcd")
        result = window.apply_ack(protocol.build_stream_v2_ack(
            window.transfer_id, 0, 0, receiver_state="failed"))
        self.assertEqual(result["discarded_bytes"], 4)
        self.assertEqual(result["receiver_state"], "failed")
        self.assertEqual(coordinator.snapshot()["active_transfers"], 0)
        with self.assertRaises(flow.FlowControlClosed):
            window.track_sent(0, 4, b"x")

    def test_completed_ack_requires_full_window_and_drain_reports_closure(self):
        coordinator = flow.FlowControlCoordinator(self.limits())
        window = coordinator.open_transfer(transfer_id(), "peer-a")
        window.track_sent(0, 0, b"ab")
        window.track_sent(0, 2, b"cd")
        with self.assertRaises(flow.FlowControlError):
            window.apply_ack(protocol.build_stream_v2_ack(
                window.transfer_id, 0, 2, receiver_state="completed"))
        window.apply_ack(protocol.build_stream_v2_ack(
            window.transfer_id, 0, 4, receiver_state="completed"))
        with self.assertRaises(flow.FlowControlClosed):
            window.wait_until_empty()

    def test_partial_ack_moves_deadline_to_new_oldest_chunk(self):
        coordinator = flow.FlowControlCoordinator(self.limits(
            window_ack_timeout_seconds=0.05))
        window = coordinator.open_transfer(transfer_id(), "peer-a")
        window.track_sent(0, 0, b"ab")
        time.sleep(0.03)
        window.track_sent(0, 2, b"cd")
        window.apply_ack(protocol.build_stream_v2_ack(window.transfer_id, 0, 2))
        time.sleep(0.025)
        self.assertFalse(window.check_ack_timeout())

    def test_receiver_verification_states_keep_admission_active(self):
        coordinator = flow.FlowControlCoordinator(self.limits())
        window = coordinator.open_transfer(transfer_id(), "peer-a")
        window.track_sent(0, 0, b"abcd")
        for state in ("verifying", "finalizing"):
            result = window.apply_ack(protocol.build_stream_v2_ack(
                window.transfer_id, 0, 4, receiver_state=state))
            self.assertEqual(result["receiver_state"], state)
            self.assertFalse(window.snapshot()["closed"])
            self.assertEqual(coordinator.snapshot()["active_transfers"], 1)

    def test_coordinator_constructs_configured_send_and_receive_queues(self):
        limits = self.limits(max_send_queue_chunks=1, max_send_queue_bytes=2,
                             max_receive_queue_chunks=2, max_receive_queue_bytes=3)
        coordinator = flow.FlowControlCoordinator(limits)
        send_queue = coordinator.create_send_queue()
        receive_queue = coordinator.create_receive_queue()
        send_queue.put(memoryview(bytearray(b"ab")))
        receive_queue.put(b"ab")
        receive_queue.put(b"c")
        self.assertEqual(send_queue.snapshot()["high_water_chunks"], 1)
        self.assertEqual(receive_queue.snapshot()["high_water_bytes"], 3)
        self.assertIsInstance(send_queue.get(), bytes)

    def test_coordinator_shutdown_closes_registered_queues(self):
        coordinator = flow.FlowControlCoordinator(self.limits())
        chunks = coordinator.create_receive_queue()
        errors = []
        thread = threading.Thread(target=lambda: self._capture_queue_error(chunks, errors))
        thread.start()
        time.sleep(0.01)
        coordinator.shutdown()
        thread.join(1)
        self.assertIsInstance(errors[0], flow.FlowControlClosed)

    @staticmethod
    def _capture_queue_error(chunks, errors):
        try:
            chunks.get()
        except Exception as exc:
            errors.append(exc)


class AckProtocolAndBatchingTests(unittest.TestCase):
    def limits(self):
        return flow.FlowControlLimits(
            ack_bytes=4, ack_chunks=2, ack_interval_seconds=0.02,
            window_ack_timeout_seconds=1)

    def test_ack_protocol_is_strict_and_separate_from_legacy(self):
        identifier = transfer_id()
        ack = protocol.build_stream_v2_ack(identifier, 2, 10, durable_offset=8,
                                            receiver_state="paused",
                                            missing_ranges=[[12, 14]])
        self.assertEqual(protocol.parse_stream_v2_ack(ack), {
            "transfer_id": identifier, "entry_index": 2,
            "verified_offset": 10, "durable_offset": 8,
            "receiver_state": "paused", "missing_ranges": [[12, 14]],
        })
        self.assertIsNone(protocol.parse_transfer_ack(ack))
        self.assertIsNone(protocol.parse_stream_v2_ack(
            protocol.build_transfer_ack("legacy-transfer", "legacy-item")))
        for changed in (
                dict(ack, extra=True), dict(ack, durable_offset=11),
                dict(ack, transfer_id=str(uuid.UUID(identifier))),
                dict(ack, schema_version=True), dict(ack, protocol_major=True),
                dict(ack, missing_ranges=[[9, 11]]),
                dict(ack, receiver_state="unknown")):
            with self.subTest(changed=changed):
                self.assertIsNone(protocol.parse_stream_v2_ack(changed))

    def test_batcher_emits_for_bytes_and_chunks(self):
        identifier = transfer_id()
        batcher = flow.CumulativeAckBatcher(identifier, self.limits())
        self.assertIsNone(batcher.record_verified(0, 0, 2))
        ack = batcher.record_verified(0, 2, 2)
        self.assertEqual(ack["verified_offset"], 4)

        self.assertIsNone(batcher.record_verified(1, 0, 1))
        ack = batcher.record_verified(1, 1, 1)
        self.assertEqual(ack["verified_offset"], 2)

    def test_batcher_emits_for_time_and_completion(self):
        identifier = transfer_id()
        batcher = flow.CumulativeAckBatcher(identifier, self.limits())
        self.assertIsNone(batcher.record_verified(0, 0, 1, now=1.0))
        self.assertEqual(batcher.poll(now=1.019), [])
        self.assertEqual(batcher.poll(now=1.021)[0]["verified_offset"], 1)
        complete = batcher.record_verified(0, 1, 1, file_complete=True)
        self.assertEqual(complete["receiver_state"], "transferring")
        zero = batcher.record_verified(1, 0, 0, file_complete=True)
        self.assertEqual(zero["verified_offset"], 0)

    def test_batcher_validates_contiguity_and_durable_progress(self):
        batcher = flow.CumulativeAckBatcher(transfer_id(), self.limits())
        batcher.record_verified(0, 0, 1, durable_offset=1)
        with self.assertRaises(flow.FlowControlError):
            batcher.record_verified(0, 2, 1)
        with self.assertRaises(flow.FlowControlError):
            batcher.record_verified(0, 1, 1, durable_offset=3)

    def test_thousand_window_ack_cycles_release_all_state(self):
        limits = flow.FlowControlLimits(
            max_inflight_chunks_per_transfer=1,
            max_inflight_bytes_global=1,
            window_ack_timeout_seconds=1)
        coordinator = flow.FlowControlCoordinator(limits)
        identifier = transfer_id()
        window = coordinator.open_transfer(identifier, "peer-a")
        for offset in range(1000):
            window.track_sent(0, offset, b"x")
            released = window.apply_ack(
                protocol.build_stream_v2_ack(identifier, 0, offset + 1))
            self.assertEqual(released["released_bytes"], 1)
        self.assertTrue(coordinator.close_transfer(identifier))
        self.assertEqual(coordinator.snapshot(), {
            "closed": False, "active_transfers": 0,
            "active_per_peer": {}, "inflight_bytes": 0,
        })


class TypedChannelBackpressureTests(unittest.TestCase):
    def test_slow_receiver_drives_bounded_window_and_cumulative_acks(self):
        sender_sock, receiver_sock = socket.socketpair()
        limits = flow.FlowControlLimits(
            max_inflight_chunks_per_transfer=2,
            max_inflight_bytes_global=4,
            ack_bytes=4, ack_chunks=2, ack_interval_seconds=0.01,
            window_ack_timeout_seconds=1)
        coordinator = flow.FlowControlCoordinator(limits)
        identifier = transfer_id()
        window = coordinator.open_transfer(identifier, "peer-a")
        sender_writer = framing.TypedFrameWriter(sender_sock)
        sender_reader = framing.TypedFrameReader(sender_sock)
        receiver_writer = framing.TypedFrameWriter(receiver_sock)
        receiver_reader = framing.TypedFrameReader(receiver_sock)
        send_queue = coordinator.create_send_queue(
            size_fn=lambda item: len(item[2]),
            freeze_fn=lambda item: (item[0], item[1], bytes(item[2])))
        receive_queue = coordinator.create_receive_queue(
            size_fn=lambda frame: len(frame.payload), freeze_fn=lambda frame: frame)
        reconstructed = bytearray()
        max_inflight = []
        errors = []
        chunks = (b"ab", b"cd", b"ef", b"gh")

        def read_payload_frames():
            try:
                for _ in chunks:
                    frame = receiver_reader.read_frame(1)
                    self.assertIsInstance(frame, framing.BinaryPayloadFrame)
                    receive_queue.put(frame, timeout=1)
            except Exception as exc:
                errors.append(exc)

        def receive_payload():
            try:
                batcher = flow.CumulativeAckBatcher(identifier, limits)
                for index in range(len(chunks)):
                    frame = receive_queue.get(timeout=1)
                    time.sleep(0.01)
                    reconstructed.extend(frame.payload)
                    ack = batcher.record_verified(
                        frame.entry_index, frame.offset, len(frame.payload),
                        file_complete=index == len(chunks) - 1)
                    if ack is not None:
                        receiver_writer.send_json_control(ack)
            except Exception as exc:
                errors.append(exc)

        def receive_acks():
            try:
                while window.snapshot()["acked_offsets"].get(0, 0) < 8:
                    frame = sender_reader.read_frame(1)
                    self.assertIsInstance(frame, framing.JsonControlFrame)
                    window.apply_ack(frame.message)
            except Exception as exc:
                errors.append(exc)

        receiver_thread = threading.Thread(target=receive_payload)
        reader_thread = threading.Thread(target=read_payload_frames)
        reader_thread.start()
        receiver_thread.start()
        ack_thread = None
        try:
            offset = 0
            for index, chunk in enumerate(chunks):
                send_queue.put((0, offset, chunk), timeout=1)
                entry_index, queued_offset, queued_chunk = send_queue.get(timeout=1)
                window.track_sent(entry_index, queued_offset, queued_chunk, timeout=1)
                sender_writer.send_binary_payload(
                    identifier, entry_index, queued_offset, queued_chunk)
                max_inflight.append(window.snapshot()["inflight_chunks"])
                offset += len(chunk)
                if index == 1:
                    ack_thread = threading.Thread(target=receive_acks)
                    ack_thread.start()
            if ack_thread is None:
                self.fail("ACK reader did not start")
            ack_thread.join(2)
            reader_thread.join(2)
            receiver_thread.join(2)
            self.assertFalse(ack_thread.is_alive())
            self.assertFalse(reader_thread.is_alive())
            self.assertFalse(receiver_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(bytes(reconstructed), b"abcdefgh")
            self.assertEqual(max(max_inflight), 2)
            self.assertEqual(window.snapshot()["inflight_bytes"], 0)
            self.assertLessEqual(receive_queue.snapshot()["high_water_chunks"],
                                 limits.max_receive_queue_chunks)
        finally:
            sender_sock.close()
            receiver_sock.close()
            coordinator.shutdown()


if __name__ == "__main__":
    unittest.main()
