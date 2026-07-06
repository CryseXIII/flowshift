"""FlowShift remote desktop-file live test (Laptop -> Surface).

Proves the real goal: a file is created on the REMOTE desktop purely through
FlowShift remote input (mouse/keyboard forwarded and injected via SendInput) —
NOT over the network, NOT via SMB/PowerShell/SSH, NOT a remote command.

Scenario (Option A, Notepad fallback — works on any Windows without extra apps):
  1. activate forwarding to the connected peer
  2. Win+R -> type "notepad" -> Enter          (open a fresh editor remotely)
  3. type the poem                              (remote keyboard)
  4. Ctrl+S -> type the desktop path -> Enter   (save via the remote Save dialog)
  5. confirm a possible overwrite dialog with Enter
  6. deactivate forwarding

The Save dialog expands %USERPROFILE% in the file-name field, so the file lands
at %USERPROFILE%\\Desktop\\FlowShift_Remote_Test.txt on the Surface.

SAFETY / GATING:
  * Runs ONLY when explicitly invoked (this CLI). Nothing runs automatically.
  * By default it refuses unless local and remote git commits match. Use --force
    to override (e.g. during development), exactly like the GUI Live Test tab.

Usage (on the laptop, runtime running, Surface connected):
  python src/python/remote_desktop_file_test.py --check          # report only
  python src/python/remote_desktop_file_test.py                  # create the file
  python src/python/remote_desktop_file_test.py --repeat 3       # 3 indexed files
  python src/python/remote_desktop_file_test.py --target Surface --force
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runtime_model import send_msg, recv_msg

CONTROL = ("127.0.0.1", 45782)

# Virtual-key codes
VK_LWIN = 0x5B
VK_R = 0x52
VK_S = 0x53
VK_CTRL = 0x11
VK_RETURN = 0x0D

POEM = (
    "FlowShift ist wach,\n"
    "die Maus zieht leis,\n"
    "vom Laptop zum Surface,\n"
    "ein kleiner Beweis.\n"
)
BASE_FILENAME = "FlowShift_Remote_Test"


def control(payload, timeout=4.0):
    with socket.create_connection(CONTROL, timeout=timeout) as s:
        s.settimeout(timeout)
        send_msg(s, payload)
        return recv_msg(s)


def status():
    r = control({"type": "status"})
    return r.get("status") if r.get("type") == "status" else None


def find_target(st, target):
    peers = [p for p in st.get("peers", []) if p.get("connected")]
    if not peers:
        return None
    if target:
        for p in peers:
            if target in (p.get("name"), p.get("identity")) or target == p.get("host"):
                return p
        return None
    return peers[0]


def tap(vk):
    return [{"type": "key", "code": vk}, {"type": "key_up", "code": vk}]


def chord(mod_vk, key_vk):
    return [
        {"type": "key", "code": mod_vk},
        {"type": "key", "code": key_vk},
        {"type": "key_up", "code": key_vk},
        {"type": "key_up", "code": mod_vk},
    ]


def send_events(events):
    control({"type": "send_synthetic", "events": events})


def type_text(text):
    control({"type": "type_text", "text": text})


def versions_match(st, peer):
    local = st.get("git_commit")
    remote = (peer.get("remote_version") or {}).get("git_commit")
    return bool(local and remote and local != "unknown" and remote != "unknown"
                and local == remote), local, remote


def create_file_remotely(index, total):
    fname = BASE_FILENAME if total == 1 else f"{BASE_FILENAME}_{index+1}"
    path = f"%USERPROFILE%\\Desktop\\{fname}.txt"
    print(f"[file] target: {path}")

    # 2) Win+R -> notepad -> Enter
    send_events(chord(VK_LWIN, VK_R))
    time.sleep(1.0)
    type_text("notepad")
    time.sleep(0.3)
    send_events(tap(VK_RETURN))
    print("[open] Win+R notepad Enter sent")
    time.sleep(1.8)

    # 3) type the poem
    type_text(POEM)
    print(f"[type] poem sent ({len(POEM)} chars)")
    time.sleep(0.6)

    # 4) Ctrl+S -> path -> Enter
    send_events(chord(VK_CTRL, VK_S))
    time.sleep(1.0)
    type_text(path)
    time.sleep(0.4)
    send_events(tap(VK_RETURN))
    print("[save] Ctrl+S, path typed, Enter sent")
    time.sleep(0.8)
    # 5) confirm a possible overwrite dialog
    send_events(tap(VK_RETURN))
    time.sleep(0.4)
    print(f"[done] {fname}.txt should now exist on the Surface desktop")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=None, help="peer name / identity / host")
    ap.add_argument("--wait", type=float, default=20.0)
    ap.add_argument("--repeat", type=int, default=1, help="create N indexed files")
    ap.add_argument("--force", action="store_true", help="skip the version-match gate")
    ap.add_argument("--check", action="store_true", help="report gating only, do nothing")
    args = ap.parse_args()

    # Wait for a connected peer.
    deadline = time.monotonic() + args.wait
    peer = None
    st = None
    while time.monotonic() < deadline:
        try:
            st = status()
        except Exception as e:
            print(f"FAIL: local runtime unreachable: {e}")
            return 1
        if not st:
            print("FAIL: no status from runtime")
            return 1
        peer = find_target(st, args.target)
        if peer:
            break
        time.sleep(0.5)
    if not peer:
        print("FAIL: no connected peer (start the Surface runtime + connect)")
        return 1

    identity = peer["identity"]
    match, local_c, remote_c = versions_match(st, peer)
    print(f"[peer] {peer['name']} [{identity}]")
    print(f"[ver ] local={str(local_c)[:12]} remote={str(remote_c)[:12]} match={match}")

    if args.check:
        print("[check] no action taken")
        return 0 if match else 2

    if not match and not args.force:
        print("ABORT: versions differ. Update+restart both devices, or pass --force.")
        return 2

    # ping (informational)
    try:
        pr = control({"type": "ping_peer", "profile": identity})
        rtt = (pr.get("ping") or {}).get("rtt_ms")
        print(f"[ping] {'ok rtt='+str(rtt)+'ms' if pr.get('type')=='ok' else pr.get('error')}")
    except Exception as e:
        print(f"[ping] error {e}")

    r = control({"type": "activate", "profile": identity})
    if r.get("type") != "ok":
        print(f"FAIL: activate: {r.get('error')}")
        return 1
    print("[fwd] activated")
    time.sleep(0.6)
    try:
        for i in range(max(1, args.repeat)):
            print(f"--- cycle {i+1}/{args.repeat} ---")
            create_file_remotely(i, args.repeat)
            time.sleep(0.6)
    finally:
        control({"type": "deactivate"})
        print("[fwd] deactivated")

    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
