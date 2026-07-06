"""FlowShift reconnect / stress test (local, no real hooks required).

Starts the productive runtime (``tray.py --tray``), then repeatedly:
  * queries the control socket for status
  * connects a synthetic peer, exchanges hello, sends an input event, disconnects
  * reconnects and repeats

Finally it asks the runtime to shut down and verifies the process exits.

This exercises the connection registry, framed reader, reconnect path and clean
shutdown. It does NOT require Administrator rights or real input hooks.

Run: ``python src/python/reconnect_stress_test.py [rounds]``
"""
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import subprocess

from runtime_model import send_msg, recv_msg

SVC = os.path.join(os.path.dirname(__file__), "tray.py")
PEER_PORT = 45781
CONTROL = ("127.0.0.1", 45782)


def control(payload, timeout=1.0):
    with socket.create_connection(CONTROL, timeout=timeout) as s:
        s.settimeout(timeout)
        send_msg(s, payload)
        return recv_msg(s)


def wait_control_up(timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = control({"type": "status"}, timeout=0.5)
            if resp.get("type") == "status":
                return True
        except Exception:
            time.sleep(0.3)
    return False


def wait_control_down(timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            control({"type": "status"}, timeout=0.5)
        except Exception:
            return True
        time.sleep(0.3)
    return False


def one_peer_cycle(idx):
    s = socket.create_connection(("127.0.0.1", PEER_PORT), timeout=5)
    try:
        s.settimeout(5)
        send_msg(s, {
            "type": "hello",
            "device_id": f"stress-{idx:04d}",
            "display_name": f"StressPeer{idx}",
            "os": "test",
            "screen": {"left": 0, "top": 0, "width": 1920, "height": 1080},
        })
        hello = recv_msg(s)
        assert hello.get("type") == "hello", f"expected hello, got {hello.get('type')}"
        send_msg(s, {"type": "input", "events": [
            {"type": "mousemove", "x": 100 + idx, "y": 200,
             "source_screen": {"left": 0, "top": 0, "width": 1920, "height": 1080}},
            {"type": "key", "code": 0x41},
            {"type": "key_up", "code": 0x41},
        ]})
        time.sleep(0.05)
    finally:
        s.close()


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    proc = subprocess.Popen(
        [sys.executable, SVC, "--tray"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    failures = []
    try:
        if not wait_control_up():
            print("FAIL: runtime control socket did not come up")
            return 1
        print("[OK] runtime up, control socket reachable")

        for i in range(rounds):
            try:
                one_peer_cycle(i)
            except Exception as e:
                failures.append(f"round {i}: {e!r}")
                print(f"[FAIL] round {i}: {e!r}")
            else:
                if i % 5 == 0:
                    print(f"[OK] reconnect round {i}")

        # Runtime must still be reachable and report running after all churn.
        resp = control({"type": "status"})
        if resp.get("status", {}).get("running"):
            print("[OK] runtime still running after reconnect churn")
        else:
            failures.append("runtime not running after churn")
            print("[FAIL] runtime not running after churn")

        # Clean shutdown via control socket.
        try:
            control({"type": "shutdown"})
        except Exception as e:
            print(f"[WARN] shutdown request error (may be fine): {e!r}")
        if wait_control_down():
            print("[OK] runtime shut down cleanly")
        else:
            failures.append("runtime did not shut down")
            print("[FAIL] runtime did not shut down")

        # The process must actually EXIT (a lingering process keeps the singleton
        # mutex and breaks the next start).
        try:
            proc.wait(timeout=8)
            print(f"[OK] runtime process exited (code {proc.returncode})")
        except Exception:
            failures.append("runtime process did not exit (lingering)")
            print("[FAIL] runtime process did not exit")
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.terminate()

    print()
    if failures:
        print(f"FAILED: {len(failures)} issue(s)")
        return 1
    print(f"All reconnect/stress checks passed ({rounds} rounds).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
