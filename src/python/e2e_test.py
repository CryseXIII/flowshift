"""End-to-end test: start service, connect, exchange messages."""
import json
import os
import socket
import struct
import subprocess
import sys
import time

SVC = os.path.join(os.path.dirname(__file__), "service.py")


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def send_msg(sock, msg):
    data = json.dumps(msg).encode()
    sock.sendall(struct.pack("!I", len(data)) + data)


def recv_msg(sock):
    raw = recv_exact(sock, 4)
    l = struct.unpack("!I", raw)[0]
    return json.loads(recv_exact(sock, l))


def main():
    proc = subprocess.Popen(
        [sys.executable, SVC],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(2)

    try:
        sock = socket.create_connection(("127.0.0.1", 45781), timeout=5)
        print("[OK] Connected to service")

        send_msg(sock, {
            "type": "hello",
            "device_id": "e2e-test",
            "display_name": "E2ETest",
            "os": "windows",
        })

        hello = recv_msg(sock)
        assert hello["type"] == "hello", f"Expected hello, got {hello['type']}"
        print(f"[OK] Received Hello from: {hello['display_name']}")

        send_msg(sock, {
            "type": "input",
            "events": [{"type": "mousemove", "x": 500, "y": 300}],
        })
        print("[OK] Sent input event")

        sock.close()
        print("\nAll E2E tests passed!")
    except Exception as e:
        print(f"\nFAIL: {e}")
        raise
    finally:
        proc.terminate()
        proc.wait(timeout=3)
        for line in proc.stdout or []:
            pass


if __name__ == "__main__":
    main()
