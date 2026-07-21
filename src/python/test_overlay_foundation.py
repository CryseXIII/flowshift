"""Local checks for the Phase-1 pure overlay foundation.

Run: ``PYTHONDONTWRITEBYTECODE=1 python src/python/test_overlay_foundation.py``
"""
import json
import os
import sys
import threading
import time
from dataclasses import FrozenInstanceError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import overlay_geometry as geometry
import overlay_protocol as protocol
from runtime_model import get_interaction_target


_failures = []
_checks = 0


def check(condition, label):
    global _checks
    _checks += 1
    if condition:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


def expect_protocol_error(code, callback, label):
    try:
        callback()
    except protocol.OverlayProtocolError as exc:
        check(exc.code == code, label)
        return exc
    except Exception as exc:
        check(False, f"{label} (arbitrary {type(exc).__name__})")
        return None
    check(False, label)
    return None


def expect_value_error(callback, label):
    try:
        callback()
    except ValueError:
        check(True, label)
    except Exception as exc:
        check(False, f"{label} (arbitrary {type(exc).__name__})")
    else:
        check(False, label)


class FakeConnection:
    def __init__(self, incoming=None):
        self.incoming = incoming
        self.sent = None
        self.receive_limit = None

    def send_bytes(self, data):
        self.sent = data

    def recv_bytes(self, maxlength=None):
        self.receive_limit = maxlength
        return self.incoming

    def send(self, value):
        raise AssertionError("pickle send API used")

    def recv(self):
        raise AssertionError("pickle receive API used")


class FailedConnection:
    def recv_bytes(self, maxlength=None):
        raise OSError("pipe closed")


# Constants and valid message paths.
check(protocol.PROTOCOL_VERSION == 1, "protocol version is 1")
check(protocol.MAX_MESSAGE_BYTES == 65536, "message size limit is 65536")
check(protocol.MAX_REQUEST_ID_LENGTH == 128, "request id limit is 128")
check(protocol.VALID_MESSAGE_TYPES == {
    "hello", "ready", "ping", "pong", "show_overlay", "hide_overlay",
    "overlay_visible", "overlay_hidden", "overlay_event", "shutdown", "error",
}, "valid message types are exact")

hello = protocol.make_message("hello", {"role": "runtime", "pid": 123}, "hello-1")
check(hello["payload"]["role"] == "runtime", "valid runtime hello")
check(protocol.make_message("hello", {"role": "host", "pid": 456})["payload"]["pid"] == 456,
      "valid host hello")
check(protocol.make_message("ready", {"pid": 456})["type"] == "ready", "valid ready")
for message_type in ("ping", "pong", "hide_overlay", "overlay_visible",
                     "overlay_hidden", "shutdown"):
    check(protocol.make_message(message_type)["payload"] == {}, f"valid {message_type}")

show_local = protocol.make_message("show_overlay", {
    "mode": "clipboard",
    "target": {"kind": "local", "identity": "local"},
    "x": -300,
    "y": 40,
    "data": {"source": "test"},
}, "show-1")
check(show_local["payload"]["x"] == -300, "valid local clipboard show")
show_remote = protocol.make_message("show_overlay", {
    "mode": "command_wheel",
    "target": {"kind": "remote", "identity": "device:peer-1"},
    "x": 10,
    "y": -20,
    "data": {},
})
check(show_remote["payload"]["target"]["kind"] == "remote", "valid remote command wheel show")
event = protocol.make_message("overlay_event", {"event": "escape", "data": {}})
check(event["payload"]["event"] == "escape", "valid overlay event")
error = protocol.make_error("not_ready", "overlay is not ready", "show-1")
check(error["type"] == "error" and error["request_id"] == "show-1", "valid correlated error")

encoded = protocol.encode_message(show_local)
check(isinstance(encoded, bytes), "encoding produces bytes")
check(protocol.decode_message(encoded) == show_local, "JSON bytes round trip")
check(json.loads(encoded)["protocol"] == 1, "encoding is JSON")
check(len(protocol.new_request_id()) <= protocol.MAX_REQUEST_ID_LENGTH, "new request id is bounded")
check(protocol.new_request_id() != protocol.new_request_id(), "new request ids are unique")

# Strict malformed-input handling.
expect_protocol_error("invalid_json", lambda: protocol.decode_message(b"{"), "malformed JSON rejected")
expect_protocol_error("invalid_json", lambda: protocol.decode_message(b"\xff"), "invalid UTF-8 rejected")
expect_protocol_error("invalid_message", lambda: protocol.decode_message("{}"), "text input rejected")
expect_protocol_error("invalid_message", lambda: protocol.decode_message(b"[]"), "non-object JSON rejected")
expect_protocol_error("invalid_message", lambda: protocol.decode_message(
    json.dumps({**hello, "extra": True}).encode()), "unknown top-level field rejected")
expect_protocol_error("invalid_protocol", lambda: protocol.decode_message(
    json.dumps({**hello, "protocol": 2}).encode()), "wrong protocol rejected")
expect_protocol_error("invalid_protocol", lambda: protocol.decode_message(
    json.dumps({**hello, "protocol": True}).encode()), "boolean protocol rejected")
expect_protocol_error("unknown_type", lambda: protocol.make_message("launch", {}), "unknown type rejected")
expect_protocol_error("invalid_request_id", lambda: protocol.make_message("ping", {}, 42),
                      "non-string request id rejected")
expect_protocol_error("invalid_request_id", lambda: protocol.make_message("ping", {}, ""),
                      "empty request id rejected")
expect_protocol_error("invalid_request_id", lambda: protocol.make_message(
    "ping", {}, "x" * 129), "long request id rejected")
expect_protocol_error("invalid_payload", lambda: protocol.make_message("ping", []),
                      "non-object payload rejected")
expect_protocol_error("invalid_payload", lambda: protocol.make_message(
    "hello", {"role": "worker", "pid": 1}), "invalid hello role rejected")
expect_protocol_error("invalid_payload", lambda: protocol.make_message(
    "hello", {"role": [], "pid": 1}), "malformed hello role is controlled")
expect_protocol_error("invalid_payload", lambda: protocol.make_message(
    "hello", {"role": "host", "pid": True}), "boolean hello pid rejected")
expect_protocol_error("invalid_payload", lambda: protocol.make_message(
    "ready", {"pid": False}), "boolean ready pid rejected")
expect_protocol_error("invalid_payload", lambda: protocol.make_message("show_overlay", {
    **show_local["payload"], "mode": "unknown"}), "invalid overlay mode rejected")
expect_protocol_error("invalid_payload", lambda: protocol.make_message("show_overlay", {
    **show_local["payload"], "mode": []}), "malformed overlay mode is controlled")
expect_protocol_error("invalid_payload", lambda: protocol.make_message("show_overlay", {
    **show_local["payload"], "target": []}), "non-object target rejected")
expect_protocol_error("invalid_payload", lambda: protocol.make_message("show_overlay", {
    **show_local["payload"], "target": {"kind": "cloud", "identity": "x"}}),
    "invalid target kind rejected")
expect_protocol_error("invalid_payload", lambda: protocol.make_message("show_overlay", {
    **show_local["payload"], "target": {"kind": [], "identity": "x"}}),
    "malformed target kind is controlled")
expect_protocol_error("invalid_payload", lambda: protocol.make_message("show_overlay", {
    **show_local["payload"], "target": {"kind": "local", "identity": "peer"}}),
    "invalid local identity rejected")
expect_protocol_error("invalid_payload", lambda: protocol.make_message("show_overlay", {
    **show_local["payload"], "target": {"kind": "remote", "identity": "  "}}),
    "empty remote identity rejected")
expect_protocol_error("invalid_payload", lambda: protocol.make_message("show_overlay", {
    **show_local["payload"], "x": True}), "boolean x rejected")
expect_protocol_error("invalid_payload", lambda: protocol.make_message("show_overlay", {
    **show_local["payload"], "y": False}), "boolean y rejected")
expect_protocol_error("invalid_payload", lambda: protocol.make_message("show_overlay", {
    **show_local["payload"], "data": []}), "non-object nested data rejected")
expect_protocol_error("invalid_payload", lambda: protocol.make_message(
    "overlay_event", {"event": ""}), "empty overlay event rejected")
expect_protocol_error("invalid_payload", lambda: protocol.make_error("", "message"),
                      "empty error code rejected")
expect_protocol_error("invalid_payload", lambda: protocol.make_error("code", ""),
                      "empty error message rejected")

large = protocol.make_message("ping", {"blob": "x" * protocol.MAX_MESSAGE_BYTES}, "large")
large_exc = expect_protocol_error("message_too_large", lambda: protocol.encode_message(large),
                                  "oversized encode rejected")
check(large_exc is not None and large_exc.request_id == "large", "oversized encode remains correlated")
expect_protocol_error("message_too_large", lambda: protocol.decode_message(
    b"x" * (protocol.MAX_MESSAGE_BYTES + 1)), "oversized decode rejected before JSON")

# Byte-only connection helpers and receive bounds.
outgoing = FakeConnection()
protocol.send_message(outgoing, hello)
check(outgoing.sent == protocol.encode_message(hello), "send_message uses send_bytes")
incoming = FakeConnection(outgoing.sent)
check(protocol.receive_message(incoming) == hello, "receive_message uses recv_bytes")
check(incoming.receive_limit == protocol.MAX_MESSAGE_BYTES, "receive requests a bounded frame")
oversized_connection = FakeConnection(b"x" * (protocol.MAX_MESSAGE_BYTES + 1))
expect_protocol_error("message_too_large", lambda: protocol.receive_message(oversized_connection),
                      "oversized receive rejected")
expect_protocol_error("transport_error", lambda: protocol.receive_message(FailedConnection()),
                      "ordinary pipe failure is a transport error")

# Request correlation, capacity, timeout, cancellation, and a threaded resolve.
tracker = protocol.RequestTracker()
check(tracker.max_pending == 128, "request tracker default bound is 128")
check(tracker.register("request-1") == "request-1", "request registered")
response = protocol.make_message("pong", {}, "request-1")
check(tracker.resolve(response) is True, "known response resolved")
check(tracker.wait("request-1", 0.1) == response, "resolved response returned")
check(tracker.pending_count == 0, "resolved request removed after wait")

tracker.register("duplicate")
expect_protocol_error("duplicate_request", lambda: tracker.register("duplicate"),
                      "duplicate registration rejected")
tracker.resolve(protocol.make_message("pong", {}, "duplicate"))
expect_protocol_error("duplicate_response", lambda: tracker.resolve(
    protocol.make_message("pong", {}, "duplicate")), "duplicate resolution rejected")
check(tracker.wait("duplicate", 0) is not None, "first duplicate response remains available")
expect_protocol_error("unknown_request", lambda: tracker.resolve(
    protocol.make_message("pong", {}, "missing")), "unknown resolution rejected")

tracker.register("timeout")
expect_protocol_error("timeout", lambda: tracker.wait("timeout", 0.001), "request timeout is controlled")
expect_protocol_error("unknown_request", lambda: tracker.wait("timeout", 0),
                      "timed-out request is removed")
tracker.register("invalid-timeout")
expect_protocol_error("invalid_timeout", lambda: tracker.wait("invalid-timeout", float("nan")),
                      "non-finite timeout is controlled")
tracker.cancel_all("cleanup")

tracker.register("cancel-1")
tracker.register("cancel-2")
check(tracker.cancel_all("overlay stopped") == 2, "cancel_all reports cancelled count")
cancel_exc = expect_protocol_error("cancelled", lambda: tracker.wait("cancel-1", 0),
                                   "cancelled wait raises controlled error")
check(cancel_exc is not None and cancel_exc.message == "overlay stopped", "cancel reason retained")
expect_protocol_error("cancelled", lambda: tracker.wait("cancel-2", 0), "all requests cancelled")

bounded = protocol.RequestTracker(max_pending=2)
bounded.register("one")
bounded.register("two")
check(bounded.pending_count == 2, "bounded tracker counts pending requests")
expect_protocol_error("too_many_pending", lambda: bounded.register("three"),
                      "bounded pending limit enforced")
bounded.cancel_all("done")

threaded = protocol.RequestTracker()
threaded.register("threaded")


def delayed_resolve():
    time.sleep(0.01)
    threaded.resolve(protocol.make_message("ready", {"pid": 7}, "threaded"))


worker = threading.Thread(target=delayed_resolve)
worker.start()
check(threaded.wait("threaded", 1)["type"] == "ready", "thread-safe wait observes resolution")
worker.join()

# Interaction target fail-safe.
check(get_interaction_target(False, "device:peer") == {"kind": "local", "identity": "local"},
      "inactive interaction target is local")
check(get_interaction_target(True, "  device:peer  ") == {
    "kind": "remote", "identity": "device:peer"}, "active target is trimmed remote identity")
check(get_interaction_target(True, None) == {"kind": "local", "identity": "local"},
      "missing active identity fails safe to local")
check(get_interaction_target(True, "   ") == {"kind": "local", "identity": "local"},
      "blank active identity fails safe to local")

# DPI conversion and physical virtual-desktop placement.
for dpi, expected in ((96, 100), (120, 125), (144, 150), (192, 200)):
    check(geometry.css_to_physical(100, dpi) == expected, f"CSS to physical at {dpi} DPI")
    check(geometry.physical_to_css(expected, dpi) == 100, f"physical to CSS at {dpi} DPI")
check(geometry.dpi_scale(120) == 1.25, "120 DPI scale is 1.25")
check(geometry.dpi_scale(192) == 2.0, "192 DPI scale is 2.0")

normal = geometry.WorkArea(0, 0, 1920, 1080, 96)
placed = geometry.clamp_overlay_to_work_area(100, 100, 300, 200, normal, 20, 10)
check(placed == geometry.OverlayPlacement(120, 110, 300, 200, 96), "normal work-area placement")
check(placed.scale == 1.0, "placement exposes DPI scale")
right = geometry.clamp_overlay_to_work_area(1900, 100, 300, 200, normal)
check(right.x == 1620, "right edge clamps fully")
bottom = geometry.clamp_overlay_to_work_area(100, 1070, 300, 200, normal)
check(bottom.y == 880, "bottom edge clamps fully")
left = geometry.clamp_overlay_to_work_area(-50, 100, 300, 200, normal, 0, 0)
check(left.x == 0, "left edge clamps fully")
top = geometry.clamp_overlay_to_work_area(100, -50, 300, 200, normal, 0, 0)
check(top.y == 0, "top edge clamps fully")

negative = geometry.WorkArea(-1920, -200, 0, 880, 144)
negative_placed = geometry.clamp_overlay_to_work_area(-1910, -190, 400, 300, negative, -20, -20)
check(negative_placed.x == -1920 and negative_placed.y == -200,
      "negative work-area left/top clamp")
check(negative_placed.width == 600 and negative_placed.height == 450,
      "requested CSS dimensions convert once at monitor DPI")
negative_edge = geometry.clamp_overlay_to_work_area(-10, 870, 400, 300, negative)
check(negative_edge.x == -600 and negative_edge.y == 430,
      "negative work-area right/bottom clamp")

try:
    normal.left = 2
    check(False, "WorkArea is immutable")
except FrozenInstanceError:
    check(True, "WorkArea is immutable")
try:
    placed.x = 2
    check(False, "OverlayPlacement is immutable")
except FrozenInstanceError:
    check(True, "OverlayPlacement is immutable")

expect_value_error(lambda: geometry.dpi_scale(0), "zero DPI rejected")
expect_value_error(lambda: geometry.dpi_scale(True), "boolean DPI rejected")
expect_value_error(lambda: geometry.css_to_physical(-1, 96), "negative CSS dimension rejected")
expect_value_error(lambda: geometry.physical_to_css(-1, 96), "negative physical dimension rejected")
expect_value_error(lambda: geometry.clamp_overlay_to_work_area(
    0, 0, 0, 10, normal), "zero overlay width rejected")
expect_value_error(lambda: geometry.clamp_overlay_to_work_area(
    0, 0, 2000, 10, normal), "oversized overlay width rejected")
expect_value_error(lambda: geometry.WorkArea(0, 0, 0, 10), "invalid work-area dimensions rejected")


print()
if _failures:
    print(f"FAILED: {len(_failures)} of {_checks} check(s): {', '.join(_failures)}")
    sys.exit(1)
print(f"All {_checks} checks passed.")
