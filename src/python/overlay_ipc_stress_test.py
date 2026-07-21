"""Stress the real headless overlay host over authenticated AF_PIPE IPC."""
from __future__ import annotations

import base64
import gc
import json
import os
import subprocess
import sys
import time
import tracemalloc
import uuid
from multiprocessing.connection import Client

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import overlay_protocol as protocol


PING_COUNT = 1000
SMALL_REQUEST_COUNT = 1000
SHORT_SESSION_COUNT = 20
STARTUP_TIMEOUT = 6.0
MESSAGE_TIMEOUT = 3.0
PROCESS_TIMEOUT = 4.0
MEMORY_GROWTH_LIMIT = 8 * 1024 * 1024
HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay_host.py")


class StressFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise StressFailure(message)


class HostSession:
    def __init__(self, python_executable):
        self.python_executable = os.path.abspath(python_executable)
        self.pipe = rf"\\.\pipe\FlowShiftOverlayStress-{os.getpid()}-{uuid.uuid4().hex}"
        self.auth_key = os.urandom(32)
        self.process = None
        self.connection = None

    def start(self):
        command = [
            self.python_executable,
            HOST,
            "--pipe", self.pipe,
            "--auth-key", base64.b64encode(self.auth_key).decode("ascii"),
            "--url", "http://127.0.0.1:5000/overlay-test",
            "--parent-pid", str(os.getpid()),
            "--startup-timeout", str(STARTUP_TIMEOUT),
            "--headless",
        ]
        self.process = subprocess.Popen(
            command,
            cwd=os.path.dirname(HOST),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        deadline = time.monotonic() + STARTUP_TIMEOUT
        last_error = None
        while time.monotonic() < deadline and self.process.poll() is None:
            try:
                self.connection = Client(
                    self.pipe, family="AF_PIPE", authkey=self.auth_key,
                )
                break
            except (OSError, EOFError) as exc:
                last_error = exc
                time.sleep(0.025)
        require(
            self.connection is not None,
            f"host did not accept a connection: {last_error or self.process.poll()}",
        )
        hello = self.receive()
        require(hello["type"] == "hello", "host did not send hello")
        require(hello["request_id"] is None, "host hello was unexpectedly correlated")
        require(hello["payload"]["role"] == "host", "host hello has wrong role")
        require(hello["payload"]["pid"] == self.process.pid, "host hello PID mismatch")
        request_id = f"hello-{uuid.uuid4().hex}"
        protocol.send_message(
            self.connection, "hello", {"role": "runtime", "pid": os.getpid()}, request_id,
        )
        ready = self.receive()
        require(ready["type"] == "ready", "host did not become ready")
        require(ready["request_id"] == request_id, "ready request_id mismatch")
        require(ready["payload"]["pid"] == self.process.pid, "ready PID mismatch")
        return self

    def receive(self, timeout=MESSAGE_TIMEOUT):
        require(self.connection is not None, "session has no connection")
        require(self.connection.poll(timeout), "timed out waiting for host IPC")
        return protocol.receive_message(self.connection)

    def request(self, message_type, expected_type, request_id):
        protocol.send_message(self.connection, message_type, {}, request_id)
        response = self.receive()
        require(response["request_id"] == request_id, f"{message_type} request_id mismatch")
        require(response["type"] == expected_type, f"{message_type} returned {response['type']}")
        return response

    def shutdown(self):
        if self.connection is not None and self.process is not None and self.process.poll() is None:
            request_id = f"shutdown-{uuid.uuid4().hex}"
            try:
                self.request("shutdown", "shutdown", request_id)
            except (OSError, EOFError, protocol.OverlayProtocolError, StressFailure):
                pass
        self.close_connection()
        if self.process is not None:
            try:
                self.process.wait(timeout=PROCESS_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=PROCESS_TIMEOUT)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=PROCESS_TIMEOUT)

    def close_connection(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
            self.connection = None


def verify_error(session, raw, expected_code, expected_request_id):
    session.connection.send_bytes(raw)
    response = session.receive()
    require(response["type"] == "error", f"{expected_code} did not return error")
    require(response["request_id"] == expected_request_id, f"{expected_code} correlation mismatch")
    require(response["payload"]["code"] == expected_code, f"wrong error code for {expected_code}")


def run():
    counts = {
        "ping": 0,
        "small": 0,
        "malformed": 0,
        "unknown": 0,
        "post_error_ping": 0,
        "oversized": 0,
        "short_sessions": 0,
    }
    session = HostSession(sys.executable)
    tracemalloc.start()
    suite_started = time.monotonic()
    try:
        session.start()
        gc.collect()
        baseline_memory = tracemalloc.get_traced_memory()[0]

        ping_started = time.monotonic()
        for index in range(PING_COUNT):
            session.request("ping", "pong", f"ping-{index}")
            counts["ping"] += 1
        ping_elapsed = time.monotonic() - ping_started

        small_started = time.monotonic()
        for index in range(SMALL_REQUEST_COUNT):
            session.request("hide_overlay", "overlay_hidden", f"hide-{index}")
            counts["small"] += 1
        small_elapsed = time.monotonic() - small_started

        verify_error(session, b"{", "invalid_json", None)
        counts["malformed"] += 1
        unknown_id = "unknown-type"
        unknown = json.dumps({
            "protocol": protocol.PROTOCOL_VERSION,
            "type": "not_a_real_overlay_message",
            "request_id": unknown_id,
            "payload": {},
        }, separators=(",", ":")).encode("utf-8")
        verify_error(session, unknown, "unknown_type", unknown_id)
        counts["unknown"] += 1
        session.request("ping", "pong", "post-errors")
        counts["post_error_ping"] += 1

        gc.collect()
        current_memory, peak_memory = tracemalloc.get_traced_memory()
        memory_growth = max(0, current_memory - baseline_memory)
        require(
            memory_growth <= MEMORY_GROWTH_LIMIT,
            f"driver memory grew {memory_growth} bytes (limit {MEMORY_GROWTH_LIMIT})",
        )

        oversized_rejected = False
        try:
            session.connection.send_bytes(b"x" * (protocol.MAX_MESSAGE_BYTES + 1))
        except (OSError, EOFError):
            oversized_rejected = True
        if not oversized_rejected and session.connection.poll(MESSAGE_TIMEOUT):
            try:
                response = protocol.receive_message(session.connection)
                oversized_rejected = (
                    response["type"] == "error"
                    and response["payload"]["code"] == "message_too_large"
                )
            except protocol.OverlayProtocolError as exc:
                oversized_rejected = exc.code in ("message_too_large", "transport_error")
        if not oversized_rejected and session.process is not None:
            try:
                session.process.wait(timeout=PROCESS_TIMEOUT)
                oversized_rejected = True
            except subprocess.TimeoutExpired:
                pass
        require(oversized_rejected, "oversized frame was not rejected and transport stayed open")
        counts["oversized"] += 1
    finally:
        session.shutdown()

    require(session.process is not None and session.process.poll() is not None,
            "primary host process was not reaped")

    reconnect_started = time.monotonic()
    for index in range(SHORT_SESSION_COUNT):
        short = HostSession(sys.executable)
        try:
            short.start()
            short.request("ping", "pong", f"short-ping-{index}")
            if index % 2:
                short.close_connection()
            counts["short_sessions"] += 1
        finally:
            short.shutdown()
        require(short.process is not None and short.process.poll() is not None,
                f"short-session child {index} was not reaped")
    reconnect_elapsed = time.monotonic() - reconnect_started
    suite_elapsed = time.monotonic() - suite_started
    tracemalloc.stop()

    print(
        "IPC counts: "
        f"ping/pong={counts['ping']}, small hide/response={counts['small']}, "
        f"malformed={counts['malformed']}, unknown={counts['unknown']}, "
        f"post-error ping={counts['post_error_ping']}, oversized={counts['oversized']}, "
        f"short sessions={counts['short_sessions']}"
    )
    print(
        f"Timings: ping={ping_elapsed:.3f}s, small={small_elapsed:.3f}s, "
        f"short sessions={reconnect_elapsed:.3f}s, total={suite_elapsed:.3f}s"
    )
    print(
        f"Driver memory: growth={memory_growth} bytes, peak={peak_memory} bytes, "
        f"limit={MEMORY_GROWTH_LIMIT} bytes"
    )
    print("PASS: overlay IPC stress completed with all owned hosts reaped.")


def main():
    if sys.platform != "win32":
        print("SKIP: overlay_ipc_stress_test requires Windows named pipes.")
        return 0
    try:
        run()
    except Exception as exc:
        tracemalloc.stop()
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
