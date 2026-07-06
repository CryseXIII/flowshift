"""FlowShift worker + forwarding smoke test (Windows).

Catches the class of bug where a runtime worker thread dies silently (e.g. the
`forward_loop` NameError from a missing import): `py_compile` and the pure-logic
tests do NOT exercise the running threads, so this integration smoke test does.

It runs three checks against a real runtime process:

  Test A (worker startup): status.workers.forward_loop.alive / inject_loop.alive
     are true, runtime_healthy is true, no critical worker down or failed.

  Test B (synthetic forwarding path): a fake peer connects, the profile is
     activated, `send_synthetic` is sent to the local runtime, and the fake peer
     must actually RECEIVE an `input` message (proves the whole
     hook->queue->forward_loop->socket path, not just "queued").

  Test C (log scan): the runtime's stdout/err must not contain
     'Exception in thread', 'NameError', or 'worker crashed'.

Skips cleanly on non-Windows (the runtime needs the Win32 API).

Run: python src/python/worker_smoke_test.py
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runtime_model import send_msg, recv_msg, FramedReader

SVC = os.path.join(os.path.dirname(__file__), "tray.py")
CONTROL = ("127.0.0.1", 45782)
PEER_PORT = 45900
PEER_DEVICE_ID = "testpeer0"

_failures = []


def check(cond, label):
    if cond:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


def is_supported():
    return sys.platform == "win32" and hasattr(ctypes, "windll")


def control(payload, timeout=2.0):
    with socket.create_connection(CONTROL, timeout=timeout) as s:
        s.settimeout(timeout)
        send_msg(s, payload)
        return recv_msg(s)


def wait_control_up(timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if control({"type": "status"}, 0.5).get("type") == "status":
                return True
        except Exception:
            time.sleep(0.3)
    return False


def wait_control_down(timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            control({"type": "status"}, 0.5)
        except Exception:
            return True
        time.sleep(0.3)
    return False


class FakePeer:
    """Accepts the runtime's outbound connection, does the hello handshake, and
    records any `input` messages it receives. Can also simulate forwarding TO us
    (fwd_state) and auto-answer fwd_control request_deactivate (flying switch)."""

    def __init__(self, port):
        self.port = port
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(1)
        self.srv.settimeout(1.0)
        self.connected = threading.Event()
        self.inputs = []
        self.fwd_control_requests = []
        self.conn = None
        self.send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self.srv.close()
        except Exception:
            pass

    def send(self, msg):
        with self.send_lock:
            if self.conn is not None:
                send_msg(self.conn, msg)

    def _hello(self):
        return {
            "type": "hello",
            "protocol_version": 1,
            "device_id": PEER_DEVICE_ID,
            "display_name": "TestPeer",
            "os": "windows",
            "desktop": "win32",
            "input_backend": "win32",
            "port": self.port,
            "screen": {"x": 0, "y": 0, "left": 0, "top": 0, "width": 1920, "height": 1080},
        }

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.conn = conn
            try:
                conn.settimeout(1.0)
                reader = FramedReader(conn)
                self.send(self._hello())
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not self.connected.is_set():
                    msg = reader.read_message(1.0)
                    if msg and msg.get("type") == "hello":
                        self.connected.set()
                        break
                while not self._stop.is_set():
                    msg = reader.read_message(1.0)
                    if msg is None:
                        continue
                    if msg.get("type") == "input":
                        self.inputs.append(msg)
                    elif msg.get("type") == "fwd_control":
                        self.fwd_control_requests.append(msg)
                        # Auto-answer: we stop forwarding to them -> ok.
                        self.send({
                            "type": "fwd_control_result",
                            "action": msg.get("action"),
                            "status": "ok",
                            "message": "test-peer-deactivated",
                        })
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass


def main():
    if not is_supported():
        print("[SKIP] worker_smoke_test requires Windows (runtime needs Win32 API).")
        return 0

    tmp = tempfile.mkdtemp(prefix="fs_smoke_")
    cfg_path = os.path.join(tmp, "config.json")
    cfg = {
        "device_name": "SmokeHost",
        "device_id": "5a1b2c3d",
        "port": 45781,
        "peers": [
            {"name": "TestPeer", "host": "127.0.0.1", "port": PEER_PORT,
             "device_id": PEER_DEVICE_ID},
        ],
        "hotkeys": [],
    }
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)

    out_path = os.path.join(tmp, "out.txt")
    out = open(out_path, "w", encoding="utf-8")
    env = dict(os.environ)
    env["FLOWSHIFT_CONFIG"] = cfg_path
    env["FLOWSHIFT_LOG_DIR"] = tmp

    peer = FakePeer(PEER_PORT)
    peer.start()

    proc = subprocess.Popen([sys.executable, SVC, "--tray"],
                            stdout=out, stderr=subprocess.STDOUT, env=env)
    try:
        if not wait_control_up():
            print("[FAIL] runtime control socket did not come up")
            out.flush()
            with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                print(f.read()[-2000:])
            return 1

        # ---- Test A: worker startup ------------------------------------
        st = control({"type": "status"}).get("status", {})
        workers = st.get("workers", {})
        fl = workers.get("forward_loop", {})
        il = workers.get("inject_loop", {})
        check(fl.get("alive") is True and fl.get("failed") is False,
              "Test A: forward_loop alive and not failed")
        check(il.get("alive") is True and il.get("failed") is False,
              "Test A: inject_loop alive and not failed")
        check(st.get("runtime_healthy") is True, "Test A: runtime_healthy true")
        check(st.get("critical_workers_down") == [], "Test A: no critical worker down")
        check("pipeline" in st and "session" in st, "Test A: status has pipeline + session")

        # ---- Test B: synthetic forwarding path -------------------------
        if not peer.connected.wait(timeout=15):
            check(False, "Test B: fake peer connected to runtime")
        else:
            check(True, "Test B: fake peer connected to runtime")
            r = control({"type": "activate", "profile": f"device:{PEER_DEVICE_ID}"})
            check(r.get("type") == "ok", "Test B: profile activated")
            time.sleep(0.3)
            before = st.get("pipeline", {}).get("events_forwarded", 0)
            control({"type": "send_synthetic",
                     "events": [{"type": "key", "code": 0x41},
                                {"type": "key_up", "code": 0x41}]})
            # Wait for the fake peer to actually receive an input message.
            got = False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if peer.inputs:
                    got = True
                    break
                time.sleep(0.2)
            check(got, "Test B: fake peer RECEIVED forwarded input (not just queued)")
            st2 = control({"type": "status"}).get("status", {})
            after = st2.get("pipeline", {}).get("events_forwarded", 0)
            check(after > before, "Test B: pipeline events_forwarded incremented")
            control({"type": "deactivate"})

        # ---- Test D: flying direction switch (fwd_control) --------------
        # Simulate the peer forwarding TO us, then activate our direction: the
        # runtime must request the peer to deactivate first (never both ways).
        peer.fwd_control_requests.clear()
        peer.send({"type": "fwd_state", "active": True, "source_name": "TestPeer"})
        # Wait until the runtime registered the remote forwarding state.
        got_remote = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            st_r = control({"type": "status"}).get("status", {})
            prow = next((p for p in st_r.get("peers", [])
                         if p.get("identity") == f"device:{PEER_DEVICE_ID}"), {})
            if prow.get("remote_forwarding_active"):
                got_remote = True
                break
            time.sleep(0.2)
        check(got_remote, "Test D: runtime registered peer forwarding to us")
        r = control({"type": "activate", "profile": f"device:{PEER_DEVICE_ID}"}, timeout=6.0)
        check(r.get("type") == "ok", "Test D: activation succeeded after remote deactivate")
        check(len(peer.fwd_control_requests) >= 1, "Test D: runtime sent fwd_control request_deactivate")
        st_after = control({"type": "status"}).get("status", {})
        check(st_after.get("active") is True, "Test D: exactly one direction now active (ours)")
        control({"type": "deactivate"})

        # ---- Test E: clipboard control API (manager + store in the runtime) --
        prof = f"device:{PEER_DEVICE_ID}"
        r = control({"type": "clip_capture", "profile": prof, "text": "hallo clipboard"})
        check(r.get("type") == "ok" and r.get("item"), "Test E: clip_capture stored a text item")
        lst = control({"type": "clip_list", "profile": prof})
        check(lst.get("type") == "ok" and len(lst.get("items", [])) == 1,
              "Test E: clip_list returns the captured item")
        item_id = lst["items"][0]["item_id"]
        # capture the same text again -> dedup, still 1 item
        control({"type": "clip_capture", "profile": prof, "text": "hallo clipboard"})
        lst2 = control({"type": "clip_list", "profile": prof})
        check(len(lst2.get("items", [])) == 1, "Test E: duplicate capture deduped")
        rp = control({"type": "clip_pin", "profile": prof, "item_id": item_id, "pinned": True})
        check(rp.get("pinned") is True, "Test E: clip_pin works")
        rd = control({"type": "clip_delete", "profile": prof, "item_id": item_id})
        check(rd.get("deleted") is True, "Test E: clip_delete works")
        lst3 = control({"type": "clip_list", "profile": prof})
        check(len(lst3.get("items", [])) == 0, "Test E: clipboard empty after delete")

        # ---- shutdown --------------------------------------------------
        control({"type": "shutdown"})
        wait_control_down()
        try:
            proc.wait(timeout=8)
        except Exception:
            _failures.append("runtime process did not exit")

        # ---- Test C: log scan ------------------------------------------
        out.flush()
        with open(out_path, "r", encoding="utf-8", errors="replace") as f:
            log_text = f.read()
        for bad in ("Exception in thread", "NameError", "worker crashed"):
            check(bad not in log_text, f"Test C: log has no '{bad}'")
    finally:
        try:
            control({"type": "shutdown"})
        except Exception:
            pass
        peer.stop()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.terminate()
        out.close()

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
        return 1
    print("All worker smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
