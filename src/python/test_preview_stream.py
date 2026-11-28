"""Remote preview picture transfer: protocol, flow control and abort.

Pure tests of ``preview_stream`` (no screen, no Windows). The streamer is
driven tick by tick with a fake clock; one test runs its real thread against
an acking reader, one sends a picture over a real socket pair with the link
framing of ``runtime_model``.
"""
from __future__ import annotations

import base64
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import preview_stream as ps
import runtime_model as rm


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class _CaptureError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _parsed(messages):
    out = []
    for msg in messages:
        ok, fields = ps.parse_stream_message(msg)
        assert ok, (fields, msg)
        out.append(fields)
    return out


class MessageTests(unittest.TestCase):
    def test_picture_is_cut_into_parts_and_put_together_again(self):
        jpeg = bytes(range(256)) * 500     # 128000 bytes -> 3 parts of at most 48 KiB
        messages = ps.frame_messages("s1", 7, jpeg, 960, 540)
        self.assertEqual([m["part"] for m in messages], [0, 1, 2])
        self.assertTrue(all(m["parts"] == 3 and m["size"] == len(jpeg) for m in messages))
        assembler = ps.FrameAssembler()
        results = [assembler.add(f) for f in _parsed(messages)]
        self.assertEqual(results[:2], [None, None])
        self.assertEqual(results[2], {"seq": 7, "jpeg": jpeg, "width": 960, "height": 540})
        self.assertEqual(assembler.dropped, 0)

    def test_parser_refuses_bad_values(self):
        good = ps.frame_messages("s1", 1, b"x" * 10, 4, 3)[0]
        bad_cases = {
            "session": dict(good, session_id="no spaces allowed"),
            "seq zero": dict(good, seq=0),
            "seq bool": dict(good, seq=True),
            "part out of range": dict(good, part=1),
            "too many parts": dict(good, parts=ps.MAX_PARTS + 1),
            "too large": dict(good, size=ps.MAX_FRAME_BYTES + 1),
            "no width": dict(good, width=0),
            "huge height": dict(good, height=ps.MAX_SIDE_PX + 1),
            "not base64": dict(good, data="***"),
            "empty data": dict(good, data=""),
            "data too long": dict(good, data="A" * (ps.CHUNK_BYTES * 2)),
            "crc text": dict(good, crc="1"),
        }
        for name, msg in bad_cases.items():
            self.assertEqual(ps.parse_stream_message(msg), (False, "invalid_message"), name)
        self.assertEqual(ps.parse_stream_message({"type": "preview_ack", "session_id": "s1"}),
                         (False, "invalid_message"))
        self.assertEqual(ps.parse_stream_message(ps.build_status("s1", "Not A Token")),
                         (False, "invalid_message"))
        self.assertEqual(ps.parse_stream_message(ps.build_status("s1", "")),
                         (True, {"type": "preview_status", "session_id": "s1", "reason": ""}))
        self.assertEqual(ps.parse_stream_message(ps.build_ack("s1", 3)),
                         (True, {"type": "preview_ack", "session_id": "s1", "seq": 3}))

    def test_a_picture_never_exceeds_the_limit(self):
        with self.assertRaises(ValueError):
            ps.frame_messages("s1", 1, b"x" * (ps.MAX_FRAME_BYTES + 1), 4, 3)
        with self.assertRaises(ValueError):
            ps.frame_messages("s1", 1, b"", 4, 3)

    def test_parts_fit_the_link_frame_limit(self):
        msg = ps.frame_messages("a" * 32, ps.MAX_SEQ, b"\xff" * ps.CHUNK_BYTES, 8192, 8192)[0]
        self.assertLess(len(rm.pack_frame(msg)), 70 * 1024)
        self.assertTrue(ps.parse_stream_message(msg)[0])

    def test_accept_carries_the_values_the_target_uses(self):
        self.assertEqual(ps.build_accept("s1", {"fps": 24, "max_width": 1280}),
                         {"type": "preview_accept", "session_id": "s1", "fps": 24, "max_width": 1280})


class AssemblerTests(unittest.TestCase):
    def setUp(self):
        self.assembler = ps.FrameAssembler()
        self.first = _parsed(ps.frame_messages("s1", 1, b"a" * 100000, 10, 10))
        self.second = _parsed(ps.frame_messages("s1", 2, b"b" * 60000, 10, 10))

    def test_newer_picture_replaces_an_unfinished_one(self):
        self.assertIsNone(self.assembler.add(self.first[0]))
        self.assertIsNone(self.assembler.add(self.second[0]))
        done = self.assembler.add(self.second[1])
        self.assertEqual((done["seq"], done["jpeg"]), (2, b"b" * 60000))
        self.assertEqual(self.assembler.dropped, 1)
        # The rest of the older picture arrives late: thrown away.
        self.assertIsNone(self.assembler.add(self.first[1]))
        self.assertEqual(self.assembler.dropped, 2)

    def test_missing_part_or_wrong_checksum_drops_the_picture(self):
        self.assertIsNone(self.assembler.add(self.first[0]))
        self.assertIsNone(self.assembler.add(self.first[2]))      # part 1 missing
        self.assertEqual(self.assembler.dropped, 2)                # broken + the unfinished one
        broken = [dict(f) for f in self.second]
        broken[1]["data"] = b"c" * len(broken[1]["data"])
        self.assertIsNone(self.assembler.add(broken[0]))
        self.assertIsNone(self.assembler.add(broken[1]))
        self.assertEqual(self.assembler.dropped, 3)

    def test_part_zero_missing_is_not_started(self):
        self.assertIsNone(self.assembler.add(self.first[1]))
        self.assertEqual(self.assembler.dropped, 1)
        self.assertIsNone(self.assembler.add(self.first[2]))


class FlowControlTests(unittest.TestCase):
    def test_window_ack_and_stall(self):
        clock = _Clock()
        flow = ps.FlowControl(window=2, stall_seconds=10, clock=clock)
        self.assertTrue(flow.can_send())
        flow.sent(1)
        flow.sent(2)
        self.assertFalse(flow.can_send())
        self.assertFalse(flow.acked(9))        # never sent
        self.assertTrue(flow.acked(2))         # cumulative: 1 and 2
        self.assertEqual(flow.in_flight(), 0)
        flow.sent(3)
        clock.now += 10.5
        self.assertTrue(flow.stalled())
        flow.acked(3)
        self.assertFalse(flow.stalled())


class StreamerTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.sent = []
        self.ended = []
        self.logs = []
        self.counter = 0
        self.captures = 0
        self.fail = None
        self.size = 1000

    def capture(self, previous_key):
        self.captures += 1
        if self.fail:
            raise _CaptureError(self.fail)
        key = f"k{self.counter}"
        if key == previous_key:
            return None
        return {"key": key, "jpeg": bytes([self.counter % 256]) * self.size,
                "width": 960, "height": 540}

    def streamer(self, send=None, **kwargs):
        return ps.PreviewStreamer("s1", 15, self.capture, send or self.sent.append,
                                  log=lambda lv, t: self.logs.append((lv, t)),
                                  on_end=self.ended.append, clock=self.clock, **kwargs)

    def frames(self):
        return [m for m in self.sent if m["type"] == ps.PREVIEW_FRAME]

    def test_unchanged_screen_sends_nothing(self):
        streamer = self.streamer()
        self.assertEqual(streamer.step(), "sent")
        streamer.on_ack(1)
        for _ in range(5):
            self.assertEqual(streamer.step(), "same")
        self.assertEqual(len(self.frames()), 1)
        self.counter = 1
        self.assertEqual(streamer.step(), "sent")
        self.assertEqual(streamer.snapshot()["skipped_same"], 5)

    def test_slow_line_skips_pictures_instead_of_queueing(self):
        streamer = self.streamer()
        results = []
        for tick in range(10):        # the screen changes every tick, no ack comes
            self.counter = tick
            results.append(streamer.step())
        self.assertEqual(results, ["sent", "sent"] + ["slow"] * 8)
        self.assertEqual(len(self.frames()), ps.WINDOW)
        self.assertEqual(self.captures, ps.WINDOW)   # no capture work while the window is full
        self.assertEqual(streamer.snapshot()["skipped_slow"], 8)
        # The ack of picture 1 frees one place: exactly one more picture.
        self.assertTrue(streamer.on_ack(1))
        self.counter = 99
        self.assertEqual(streamer.step(), "sent")
        self.assertEqual(streamer.step(), "slow")
        self.assertEqual([m["seq"] for m in self.frames()], [1, 2, 3])

    def test_no_ack_for_too_long_ends_the_stream(self):
        streamer = self.streamer(stall_seconds=10)
        streamer.step()
        self.clock.now += 11
        self.assertEqual(streamer.step(), "stalled")
        self.assertEqual(self.ended, [ps.REASON_STALLED])
        self.assertEqual(streamer.step(), "stopped")
        self.assertTrue(any("no answer for 10 s" in t for _lv, t in self.logs))

    def test_locked_screen_is_reported_once_and_recovery_too(self):
        streamer = self.streamer()
        self.fail = ps.REASON_SCREEN_LOCKED
        self.assertEqual(streamer.step(), "error")
        self.assertEqual(streamer.step(), "wait")          # retries only once a second
        self.clock.now += 1.1
        self.assertEqual(streamer.step(), "error")
        statuses = [m for m in self.sent if m["type"] == ps.PREVIEW_STATUS]
        self.assertEqual(statuses, [ps.build_status("s1", ps.REASON_SCREEN_LOCKED)])
        self.fail = None
        self.clock.now += 1.1
        self.assertEqual(streamer.step(), "sent")
        statuses = [m for m in self.sent if m["type"] == ps.PREVIEW_STATUS]
        self.assertEqual(statuses[-1], ps.build_status("s1", ""))
        self.assertEqual(self.ended, [])
        other = self.streamer()
        self.fail = "capture_failed"
        other.step()
        self.assertEqual(self.sent[-1], ps.build_status("s1", ps.REASON_CAPTURE_FAILED))

    def test_stop_between_parts_sends_no_further_part(self):
        self.size = ps.CHUNK_BYTES * 3       # three parts
        holder = {}

        def send(msg):
            self.sent.append(msg)
            if msg.get("part") == 0:
                holder["streamer"]._stop.set()   # what stop() does from another thread
        holder["streamer"] = streamer = self.streamer(send=send)
        self.assertEqual(streamer.step(), "stopped")
        self.assertEqual([m["part"] for m in self.frames()], [0])

    def test_link_gone_ends_the_stream(self):
        def send(_msg):
            raise ConnectionError("link closed")
        streamer = self.streamer(send=send)
        self.assertEqual(streamer.step(), "stopped")
        self.assertEqual(self.ended, [ps.REASON_SEND_FAILED])

    def test_other_messages_can_go_between_the_parts(self):
        # The link's send lock is taken per part, never for a whole picture.
        self.size = ps.CHUNK_BYTES * 2 + 10
        lock = threading.Lock()
        order = []

        def send(msg):
            with lock:
                order.append(f"part{msg.get('part')}")
            if msg.get("part") == 0:
                other = threading.Thread(target=lambda: (lock.acquire(), order.append("input"),
                                                         lock.release()))
                other.start()
                other.join(2)
        self.streamer(send=send).step()
        self.assertEqual(order, ["part0", "input", "part1", "part2"])

    def test_thread_streams_until_stopped(self):
        received = []
        holder = {}

        def send(msg):
            received.append(msg)
            if msg["type"] == ps.PREVIEW_FRAME:
                holder["s"].on_ack(msg["seq"])

        def capture(previous_key):
            key = time.monotonic()
            return {"key": key, "jpeg": b"x" * 100, "width": 4, "height": 3}

        streamer = ps.PreviewStreamer("s1", 30, capture, send,
                                      log=lambda lv, t: self.logs.append((lv, t)))
        holder["s"] = streamer
        streamer.start()
        deadline = time.monotonic() + 3
        while len(received) < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        streamer.stop()
        self.assertFalse(streamer._thread.is_alive())
        self.assertGreaterEqual(len(received), 5)
        count = len(received)
        time.sleep(0.15)
        self.assertEqual(len(received), count)     # nothing after stop
        self.assertTrue(any(t.startswith("remote_preview sending ended session=s1 reason=stopped")
                            for _lv, t in self.logs))


class ReceiveStatsTests(unittest.TestCase):
    def test_rate_over_the_last_seconds(self):
        clock = _Clock()
        stats = ps.ReceiveStats(span=2.0, clock=clock)
        for _ in range(20):
            stats.add(2048)
            clock.now += 0.1
        snap = stats.snapshot()
        self.assertEqual((snap["frames"], snap["kb"]), (20, 40))
        self.assertEqual(snap["fps"], 10.0)
        clock.now += 5
        self.assertEqual(stats.snapshot()["fps"], 0.0)


class LoopbackTests(unittest.TestCase):
    def test_picture_through_the_link_framing(self):
        jpeg = os.urandom(150 * 1024)
        ok, detail, ms = ps.loopback_transfer(jpeg, 960, 540, rm.send_msg, rm.recv_msg)
        self.assertTrue(ok, detail)
        self.assertIn("150 KB in 4 part(s)", detail)
        self.assertGreaterEqual(ms, 0)

    def test_changed_picture_is_detected(self):
        def corrupting_send(sock, msg):
            if msg.get("type") == ps.PREVIEW_FRAME and msg["part"] == 1:
                raw = bytearray(base64.b64decode(msg["data"]))
                raw[0] ^= 0xFF
                msg = dict(msg, data=base64.b64encode(bytes(raw)).decode("ascii"))
            rm.send_msg(sock, msg)
        ok, detail, _ = ps.loopback_transfer(os.urandom(100 * 1024), 4, 3, corrupting_send,
                                             rm.recv_msg)
        self.assertFalse(ok)
        self.assertEqual(detail, "the picture arrived incomplete or changed")

    def test_broken_link_fails_with_a_reason(self):
        def no_recv(sock):
            raise socket.timeout("timed out")
        ok, detail, _ = ps.loopback_transfer(b"x" * 10, 4, 3, rm.send_msg, no_recv)
        self.assertFalse(ok)
        self.assertIn("timed out", detail)


if __name__ == "__main__":
    unittest.main()
