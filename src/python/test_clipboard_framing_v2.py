"""Focused tests for strict clipboard transfer V2 typed framing."""
import hashlib
import socket
import struct
import threading
import time
import unittest
import uuid

import clipboard_framing_v2 as framing


TRANSFER_ID = "12345678-1234-5678-9abc-123456789abc"


def read_encoded(encoded):
    reader_sock, writer_sock = socket.socketpair()
    try:
        writer_sock.sendall(encoded)
        return framing.TypedFrameReader(reader_sock).read_frame(0.5)
    finally:
        reader_sock.close()
        writer_sock.close()


def raw_frame(kind, major, flags, body):
    following = struct.pack("!BBH", kind, major, flags) + body
    return struct.pack("!I", len(following)) + following


class TypedFrameRoundTripTests(unittest.TestCase):
    def test_json_control_roundtrip_requires_object(self):
        message = {"type": "manifest", "name": "Gr\u00f6\u00dfe", "count": 2}
        frame = read_encoded(framing.encode_json_control_frame(message))
        self.assertEqual(frame, framing.JsonControlFrame(message))
        for invalid in (None, [], "message", {"bad": float("nan")}, {1: "bad"}):
            with self.subTest(invalid=invalid), self.assertRaises(framing.TypedFrameError):
                framing.encode_json_control_frame(invalid)

    def test_binary_payload_roundtrip_with_sha256(self):
        payload = b"raw\x00payload" * 100
        frame = read_encoded(framing.encode_binary_payload_frame(
            TRANSFER_ID, 7, 2 ** 40, payload,
        ))
        self.assertEqual(frame.transfer_id, uuid.UUID(TRANSFER_ID).hex)
        self.assertEqual(frame.entry_index, 7)
        self.assertEqual(frame.offset, 2 ** 40)
        self.assertEqual(frame.payload, payload)
        self.assertEqual(frame.checksum_kind, framing.CHECKSUM_SHA256)
        self.assertEqual(frame.checksum, hashlib.sha256(payload).digest())

    def test_binary_payload_roundtrip_without_checksum_and_at_boundaries(self):
        frame = read_encoded(framing.encode_binary_payload_frame(
            TRANSFER_ID, framing.MAX_ENTRY_INDEX, framing.MAX_LOGICAL_OFFSET, b"",
            checksum_kind=framing.CHECKSUM_NONE,
        ))
        self.assertEqual(frame.entry_index, framing.MAX_ENTRY_INDEX)
        self.assertEqual(frame.offset, framing.MAX_LOGICAL_OFFSET)
        self.assertEqual(frame.payload, b"")
        self.assertIsNone(frame.checksum)

    def test_length_prefix_covers_typed_header_and_body(self):
        encoded = framing.encode_binary_payload_frame(TRANSFER_ID, 0, 0, b"abc")
        self.assertEqual(struct.unpack("!I", encoded[:4])[0], len(encoded) - 4)
        self.assertEqual(
            tuple(encoded[4:8]),
            (framing.FRAME_KIND_BINARY_PAYLOAD, framing.PROTOCOL_MAJOR, 0, 0),
        )


class IncrementalReaderTests(unittest.TestCase):
    def test_one_byte_fragmentation_preserves_parser_state(self):
        encoded = framing.encode_binary_payload_frame(TRANSFER_ID, 3, 19, b"fragmented")
        reader_sock, writer_sock = socket.socketpair()
        try:
            reader = framing.TypedFrameReader(reader_sock)
            for byte in encoded[:-1]:
                writer_sock.sendall(bytes((byte,)))
                self.assertIsNone(reader.read_frame(0.001))
            writer_sock.sendall(encoded[-1:])
            frame = reader.read_frame(0.5)
            self.assertEqual(frame.payload, b"fragmented")
            self.assertEqual((frame.entry_index, frame.offset), (3, 19))
        finally:
            reader_sock.close()
            writer_sock.close()

    def test_multiple_mixed_frames_in_one_read(self):
        first = framing.encode_json_control_frame({"type": "ack", "offset": 4})
        second = framing.encode_binary_payload_frame(TRANSFER_ID, 1, 4, b"next")
        reader_sock, writer_sock = socket.socketpair()
        try:
            reader = framing.TypedFrameReader(reader_sock)
            writer_sock.sendall(first + second)
            self.assertEqual(reader.read_frame(0.5).message["type"], "ack")
            self.assertEqual(reader.read_frame(0.5).payload, b"next")
        finally:
            reader_sock.close()
            writer_sock.close()

    def test_timeout_retains_partial_prefix_and_body(self):
        encoded = framing.encode_json_control_frame({"type": "offer"})
        reader_sock, writer_sock = socket.socketpair()
        try:
            reader = framing.TypedFrameReader(reader_sock)
            writer_sock.sendall(encoded[:2])
            self.assertIsNone(reader.read_frame(0.01))
            writer_sock.sendall(encoded[2:9])
            self.assertIsNone(reader.read_frame(0.01))
            writer_sock.sendall(encoded[9:])
            self.assertEqual(reader.read_frame(0.5).message, {"type": "offer"})
        finally:
            reader_sock.close()
            writer_sock.close()

    def test_timeout_is_absolute_during_slow_drip_and_preserves_socket_mode(self):
        encoded = framing.encode_json_control_frame({"type": "slow"})
        reader_sock, writer_sock = socket.socketpair()
        stopped = threading.Event()

        def drip():
            for byte in encoded[:-1]:
                if stopped.wait(0.01):
                    return
                try:
                    writer_sock.sendall(bytes((byte,)))
                except OSError:
                    return

        sender = threading.Thread(target=drip)
        sender.start()
        try:
            reader = framing.TypedFrameReader(reader_sock)
            started = time.monotonic()
            self.assertIsNone(reader.read_frame(0.05))
            self.assertLess(time.monotonic() - started, 0.15)
            self.assertIsNone(reader_sock.gettimeout())
        finally:
            stopped.set()
            sender.join(1)
            reader_sock.close()
            writer_sock.close()

    def test_connection_close_is_not_a_timeout(self):
        reader_sock, writer_sock = socket.socketpair()
        reader = framing.TypedFrameReader(reader_sock)
        writer_sock.close()
        try:
            with self.assertRaises(ConnectionError):
                reader.read_frame(0.5)
        finally:
            reader_sock.close()


class TypedFrameValidationTests(unittest.TestCase):
    def assert_rejected(self, encoded, pattern=None):
        if pattern is None:
            context = self.assertRaises(framing.TypedFrameError)
        else:
            context = self.assertRaisesRegex(framing.TypedFrameError, pattern)
        with context:
            read_encoded(encoded)

    def test_rejects_invalid_common_headers_before_body(self):
        cases = (
            (struct.pack("!I", 3), "shorter"),
            (struct.pack("!I", framing.MAX_TYPED_FRAME_BYTES + 1), "limit"),
            (raw_frame(9, 2, 0, b""), "kind"),
            (raw_frame(1, 3, 0, b"{}"), "major"),
            (raw_frame(1, 2, 1, b"{}"), "flags"),
        )
        for encoded, pattern in cases:
            with self.subTest(pattern=pattern):
                self.assert_rejected(encoded, pattern)

    def test_rejects_binary_kind_specific_limit_before_body(self):
        following_length = 4 + framing.MAX_BINARY_PAYLOAD_BYTES + 1024
        encoded = struct.pack("!I", following_length) + struct.pack("!BBH", 2, 2, 0)
        self.assert_rejected(encoded, "kind")

    def test_rejects_malformed_json_non_object_duplicates_and_constants(self):
        deep = b'{"a":' + b"[" * 70 + b"0" + b"]" * 70 + b"}"
        for body in (
                b"{", b"[]", b'{"a":1,"a":2}', b'{"a":NaN}', b"\xff",
                deep, b'{"a":123456789012345678901}', b'{"a":1e9999}'):
            with self.subTest(body=body):
                self.assert_rejected(raw_frame(1, 2, 0, body))

    def test_rejects_truncated_binary_header_and_payload_length_mismatch(self):
        self.assert_rejected(raw_frame(2, 2, 0, b"short"), "truncated")
        valid = bytearray(framing.encode_binary_payload_frame(
            TRANSFER_ID, 0, 0, b"payload", checksum_kind=framing.CHECKSUM_NONE,
        ))
        payload_length_offset = 4 + 4 + 16 + 4 + 8
        valid[payload_length_offset:payload_length_offset + 4] = struct.pack("!I", 8)
        self.assert_rejected(bytes(valid), "length")

    def test_rejects_unknown_checksum_null_uuid_and_checksum_mismatch(self):
        binary_header = struct.pack("!16sIQIB", uuid.UUID(TRANSFER_ID).bytes, 0, 0, 0, 9)
        self.assert_rejected(raw_frame(2, 2, 0, binary_header), "checksum")
        null_header = struct.pack("!16sIQIB", b"\0" * 16, 0, 0, 0, 0)
        self.assert_rejected(raw_frame(2, 2, 0, null_header), "null")
        encoded = bytearray(framing.encode_binary_payload_frame(TRANSFER_ID, 0, 0, b"data"))
        encoded[-1] ^= 1
        self.assert_rejected(bytes(encoded), "checksum")

    def test_rejects_oversized_declared_payload_without_receiving_payload(self):
        body = struct.pack(
            "!16sIQIB", uuid.UUID(TRANSFER_ID).bytes, 0, 0,
            framing.MAX_BINARY_PAYLOAD_BYTES + 1, framing.CHECKSUM_NONE,
        )
        self.assert_rejected(raw_frame(2, 2, 0, body), "payload")

    def test_rejects_entry_index_and_offset_above_protocol_ceilings(self):
        invalid_bodies = (
            struct.pack(
                "!16sIQIB", uuid.UUID(TRANSFER_ID).bytes,
                framing.MAX_ENTRY_INDEX + 1, 0, 0, framing.CHECKSUM_NONE),
            struct.pack(
                "!16sIQIB", uuid.UUID(TRANSFER_ID).bytes,
                0, framing.MAX_LOGICAL_OFFSET + 1, 0, framing.CHECKSUM_NONE),
        )
        for body in invalid_bodies:
            with self.subTest(body=body):
                self.assert_rejected(raw_frame(2, 2, 0, body), "limit")

    def test_reader_is_terminal_after_any_protocol_error(self):
        reader_sock, writer_sock = socket.socketpair()
        try:
            reader = framing.TypedFrameReader(reader_sock)
            writer_sock.sendall(raw_frame(9, 2, 0, b"") + framing.encode_json_control_frame({}))
            with self.assertRaisesRegex(framing.TypedFrameError, "kind"):
                reader.read_frame(0.5)
            with self.assertRaisesRegex(framing.TypedFrameError, "unusable"):
                reader.read_frame(0.5)
        finally:
            reader_sock.close()
            writer_sock.close()

    def test_encoder_rejects_invalid_integer_uuid_payload_and_checksum_inputs(self):
        invalid_calls = (
            lambda: framing.encode_binary_payload_frame(TRANSFER_ID, -1, 0, b""),
            lambda: framing.encode_binary_payload_frame(
                TRANSFER_ID, framing.MAX_ENTRY_INDEX + 1, 0, b""),
            lambda: framing.encode_binary_payload_frame(TRANSFER_ID, 0, -1, b""),
            lambda: framing.encode_binary_payload_frame(
                TRANSFER_ID, 0, framing.MAX_LOGICAL_OFFSET + 1, b""),
            lambda: framing.encode_binary_payload_frame(
                TRANSFER_ID, 0, framing.MAX_LOGICAL_OFFSET, b"x"),
            lambda: framing.encode_binary_payload_frame("not-a-uuid", 0, 0, b""),
            lambda: framing.encode_binary_payload_frame(uuid.UUID(int=0), 0, 0, b""),
            lambda: framing.encode_binary_payload_frame(TRANSFER_ID, 0, 0, "text"),
            lambda: framing.encode_binary_payload_frame(
                TRANSFER_ID, 0, 0, b"x", checksum_kind=framing.CHECKSUM_NONE,
                checksum=b"x"),
            lambda: framing.encode_binary_payload_frame(
                TRANSFER_ID, 0, 0, b"x", checksum_kind=framing.CHECKSUM_SHA256,
                checksum=b"wrong"),
            lambda: framing.encode_binary_payload_frame(
                TRANSFER_ID, 0, 0, b"x", checksum_kind=9),
        )
        for invalid_call in invalid_calls:
            with self.subTest(call=invalid_call), self.assertRaises(framing.TypedFrameError):
                invalid_call()

    def test_encoder_rejects_payload_over_limit(self):
        with self.assertRaisesRegex(framing.TypedFrameError, "payload"):
            framing.encode_binary_payload_frame(
                TRANSFER_ID, 0, 0, b"x" * (framing.MAX_BINARY_PAYLOAD_BYTES + 1),
            )


class RecordingSocket:
    def __init__(self):
        self.frames = []
        self.active = 0
        self.max_active = 0
        self.guard = threading.Lock()

    def sendall(self, data):
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.002)
        self.frames.append(bytes(data))
        with self.guard:
            self.active -= 1


class TypedFrameWriterTests(unittest.TestCase):
    def test_concurrent_writes_never_enter_socket_sendall_together(self):
        sock = RecordingSocket()
        writer = framing.TypedFrameWriter(sock)
        threads = [
            threading.Thread(target=writer.send_json_control, args=({"index": index},))
            for index in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sock.max_active, 1)
        self.assertEqual(len(sock.frames), 20)
        decoded = {read_encoded(encoded).message["index"] for encoded in sock.frames}
        self.assertEqual(decoded, set(range(20)))


if __name__ == "__main__":
    unittest.main()
