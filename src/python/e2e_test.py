"""End-to-end test for the FlowShift runtime.

Skips cleanly on non-Windows (the runtime needs the Win32 API). On Windows it
starts ``tray.py --tray``, waits for the local control socket to come up, then
connects a synthetic peer, exchanges the hello handshake, sends an input event,
and shuts the runtime down cleanly via the control socket.

Exit code 0 = passed or skipped, 1 = failed.

Run: ``python src/python/e2e_test.py``
"""
import ctypes
import os
import socket
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import subprocess

from runtime_model import send_msg, recv_msg

SVC = os.path.join(os.path.dirname(__file__), "tray.py")
PEER = ("127.0.0.1", 45781)
CONTROL = ("127.0.0.1", 45782)


def is_supported():
    """The runtime requires Windows + ctypes.windll (Win32 hooks / SendInput)."""
    return sys.platform == "win32" and hasattr(ctypes, "windll")


def _control(payload, timeout=1.0):
    with socket.create_connection(CONTROL, timeout=timeout) as s:
        s.settimeout(timeout)
        send_msg(s, payload)
        return recv_msg(s)


def _wait_control_up(timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _control({"type": "status"}, timeout=0.5).get("type") == "status":
                return True
        except Exception:
            time.sleep(0.3)
    return False


def _wait_control_down(timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _control({"type": "status"}, timeout=0.5)
        except Exception:
            return True
        time.sleep(0.3)
    return False


def main():
    if not is_supported():
        print(f"[SKIP] e2e_test requires Windows (platform={sys.platform!r}); skipping cleanly.")
        return 0

    temporary = tempfile.TemporaryDirectory(prefix="flowshift-e2e-")
    environment = os.environ.copy()
    environment["FLOWSHIFT_CONFIG"] = os.path.join(temporary.name, "config.json")
    environment["FLOWSHIFT_LOG_DIR"] = os.path.join(temporary.name, "logs")
    environment["PROGRAMDATA"] = os.path.join(temporary.name, "programdata")
    environment["FLOWSHIFT_OVERLAY_HEADLESS"] = "1"
    environment["FLOWSHIFT_DISABLE_AUTOMATIC_UPDATES"] = "1"
    proc = subprocess.Popen(
        [sys.executable, SVC, "--tray"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env=environment,
    )
    failed = False
    try:
        if not _wait_control_up():
            print("FAIL: runtime control socket did not come up")
            proc.terminate()
            try:
                out, _ = proc.communicate(timeout=3)
                if out:
                    print("--- runtime output ---")
                    print(out)
            except Exception:
                pass
            return 1
        print("[OK] runtime up (control socket reachable)")

        sock = socket.create_connection(PEER, timeout=5)
        try:
            sock.settimeout(5)
            send_msg(sock, {
                "type": "hello",
                "device_id": "e2e-test",
                "display_name": "E2ETest",
                "os": "linux",  # deliberately a non-Windows peer
                "screen": {"left": 0, "top": 0, "width": 1920, "height": 1080},
            })
            hello = recv_msg(sock)
            assert hello.get("type") == "hello", f"expected hello, got {hello.get('type')}"
            print(f"[OK] received hello from: {hello.get('display_name')} "
                  f"(os={hello.get('os')}, backend={hello.get('input_backend')})")

            send_msg(sock, {
                "type": "input",
                "events": [{"type": "mousemove", "x": 500, "y": 300}],
            })
            print("[OK] sent input event")
        finally:
            sock.close()

        # Clean shutdown via the control socket.
        try:
            _control({"type": "shutdown"})
        except Exception:
            pass
        if _wait_control_down():
            print("[OK] runtime shut down cleanly")
        else:
            print("FAIL: runtime did not shut down")
            failed = True
    except Exception as e:
        print(f"\nFAIL: {e}")
        failed = True
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.terminate()
        temporary.cleanup()

    if failed:
        return 1
    print("\nAll E2E tests passed!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
