"""FlowShift live network test (Laptop -> Surface), user-triggered.

This drives a **real input-forwarding** proof: it activates a forwarding profile
on the LOCAL runtime and sends synthetic mouse moves, a click and a typed text to
the remote peer, which injects them via SendInput. It does NOT write the file
over the network / via a remote command — the point is to prove input forwarding.

Safety gates (all enforced before anything is sent):
  * a peer must be connected,
  * local and remote FlowShift versions (git commit) must match (or --force),
  * local git must be clean and pushed (or --force).

Nothing runs automatically: you must invoke this script. Prepare the remote by
opening and focusing an editor (Notepad/Notepad++) first.

Usage:
  python src/python/live_network_test.py            # gated run
  python src/python/live_network_test.py --force     # bypass version/push gate
  python src/python/live_network_test.py --check      # only report, do nothing
"""
from __future__ import annotations

import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import version
from runtime_model import send_msg, recv_msg

CONTROL = ("127.0.0.1", 45782)

LIVE_TEXT = ("FlowShift ist wach,\ndie Maus zieht leis,\n"
             "vom Laptop zum Surface,\nein kleiner Beweis.\n")


def control(payload, timeout=2.0):
    with socket.create_connection(CONTROL, timeout=timeout) as s:
        s.settimeout(timeout)
        send_msg(s, payload)
        return recv_msg(s)


def get_status():
    r = control({"type": "status"})
    return r.get("status") if r.get("type") == "status" else None


def pick_connected_peer(status):
    for p in status.get("peers", []):
        if p.get("connected"):
            return p
    return None


def versions_match(local_vi, peer):
    rv = (peer or {}).get("remote_version") or {}
    rc = rv.get("git_commit")
    lc = local_vi.get("git_commit")
    return bool(rc and lc and rc != "unknown" and lc != "unknown" and rc == lc), rv


def main():
    force = "--force" in sys.argv
    check_only = "--check" in sys.argv

    try:
        status = get_status()
    except Exception as e:
        print(f"FAIL: local runtime control socket unreachable: {e}")
        return 1
    if not status:
        print("FAIL: local runtime did not return status")
        return 1

    local_vi = version.version_info()
    dirty = version.git_dirty()
    pushed = version.git_pushed()
    peer = pick_connected_peer(status)

    print(f"Local : v{local_vi['app_version']} commit={local_vi['git_commit'][:12]} "
          f"branch={local_vi['git_branch']} proto={local_vi['protocol_version']}")
    print(f"Git   : dirty={dirty} pushed={pushed}")
    if not peer:
        print("FAIL: no connected peer. Start the peer runtime and wait for connect.")
        return 1
    match, rv = versions_match(local_vi, peer)
    print(f"Peer  : {peer['name']} [{peer['identity']}] "
          f"v{rv.get('app_version','?')} commit={str(rv.get('git_commit','?'))[:12]} "
          f"proto={rv.get('protocol_version','?')}")
    print(f"Match : {match}")

    if check_only:
        return 0

    if not force:
        if not match:
            print("REFUSED: versions differ. Update both devices to the same revision, or --force.")
            return 2
        if pushed is not True or dirty is not False:
            print("REFUSED: commit + push first (git clean & pushed), or --force.")
            return 2

    identity = peer["identity"]
    print(f"\n>>> Activating forwarding to {peer['name']} ...")
    r = control({"type": "activate", "profile": identity})
    if r.get("type") != "ok":
        print(f"FAIL: activate: {r.get('error')}")
        return 1
    time.sleep(0.6)

    print(">>> Sending mouse movement ...")
    control({"type": "send_synthetic",
             "events": [{"type": "mousemove", "x": x, "y": 400} for x in range(200, 1500, 130)]})
    print(">>> Sending left click ...")
    control({"type": "send_synthetic",
             "events": [{"type": "mousedown", "button": 0}, {"type": "mouseup", "button": 0}]})
    time.sleep(0.4)
    print(">>> Typing test text via remote keyboard ...")
    control({"type": "type_text", "text": LIVE_TEXT})
    time.sleep(0.4)
    print(">>> Deactivating forwarding ...")
    control({"type": "deactivate"})
    print("\nDONE. On the remote, save the file (Ctrl+S) as FlowShift_Remote_Test.txt on the Desktop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
