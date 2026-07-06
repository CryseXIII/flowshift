"""FlowShift poem live-test driver (Laptop -> Surface).

Drives ONE connection-test cycle: it activates forwarding to the connected peer
and writes one poem into the currently focused editor on the remote (Notepad++),
then saves with Ctrl+S. Repeat this (optionally with a laptop service restart in
between) to append several poems to the same file — one poem per connection test.

Everything is real input forwarding (mouse/keyboard injected on the remote via
SendInput); nothing is written over the network directly.

Prereq on the REMOTE (Surface): open Notepad++, create a new file, save it once
to the Desktop as e.g. FlowShift_Gedichte.txt, and leave Notepad++ focused.

Usage (on the laptop, runtime running):
  python src/python/poem_live_test.py --list
  python src/python/poem_live_test.py --index 0            # write poem 0 + save
  python src/python/poem_live_test.py --index 1 --no-save  # write, don't save
  python src/python/poem_live_test.py --index 2 --target Surface
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
VK_CTRL = 0x11
VK_S = 0x53
VK_END = 0x23

POEMS = [
    "Gedicht 1\nFlowShift ist wach,\ndie Maus zieht leis,\nvom Laptop zum Surface,\nein kleiner Beweis.\n",
    "Gedicht 2\nEin Tastenschlag reist,\ndurchs Netz ganz geschwind,\nund malt auf dem Surface,\nwas Finger ersinnt.\n",
    "Gedicht 3\nVerbinden, verschwinden,\ndann wieder zur Stell,\nder Dienst kommt zurueck,\nzuverlaessig und schnell.\n",
    "Gedicht 4\nKein Fenster blitzt auf,\nkein UAC stoert,\nder Cursor gehorcht,\nwie es sich gehoert.\n",
    "Gedicht 5\nVom Laptop gesendet,\naufs Surface gebracht,\nein Vers nach dem andern,\nText Zeile fuer Zeile gemacht.\n",
    "Gedicht 6\nDrei Zyklen, vier Zyklen,\nder Beweis ist erbracht,\nFlowShift hat die Eingabe\nremote vollbracht.\n",
]


def control(payload, timeout=3.0):
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


def key_combo(mod_vk, key_vk):
    """send_synthetic events for a modifier+key chord."""
    return [
        {"type": "key", "code": mod_vk},
        {"type": "key", "code": key_vk},
        {"type": "key_up", "code": key_vk},
        {"type": "key_up", "code": mod_vk},
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--target", default=None, help="peer name / identity / host")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--wait", type=float, default=20.0, help="seconds to wait for peer connect")
    args = ap.parse_args()

    if args.list:
        for i, p in enumerate(POEMS):
            print(f"--- index {i} ---\n{p}")
        return 0

    poem = POEMS[args.index % len(POEMS)]

    # 1) wait for a connected peer
    deadline = time.monotonic() + args.wait
    peer = None
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
        print("FAIL: no connected peer (start/await the Surface runtime + connection)")
        return 1
    identity = peer["identity"]
    print(f"[peer] {peer['name']} [{identity}] os={peer.get('remote_os')} "
          f"ver={(peer.get('remote_version') or {}).get('git_commit','?')[:12]}")

    # 2) ping
    try:
        pr = control({"type": "ping_peer", "profile": identity})
        rtt = (pr.get("ping") or {}).get("rtt_ms")
        print(f"[ping] {'ok rtt='+str(rtt)+'ms' if pr.get('type')=='ok' else pr.get('error')}")
    except Exception as e:
        print(f"[ping] error {e}")

    # 3) activate forwarding
    r = control({"type": "activate", "profile": identity})
    if r.get("type") != "ok":
        print(f"FAIL: activate: {r.get('error')}")
        return 1
    print("[fwd] activated")
    time.sleep(0.6)

    try:
        # 4) move cursor to end of document, then type the poem
        control({"type": "send_synthetic", "events": key_combo(VK_CTRL, VK_END)})
        time.sleep(0.2)
        control({"type": "type_text", "text": poem + "\n"})
        print(f"[type] poem index {args.index} sent ({len(poem)} chars)")
        time.sleep(0.4)
        # 5) save with Ctrl+S
        if not args.no_save:
            control({"type": "send_synthetic", "events": key_combo(VK_CTRL, VK_S)})
            print("[save] Ctrl+S sent")
            time.sleep(0.4)
    finally:
        # 6) always deactivate forwarding
        control({"type": "deactivate"})
        print("[fwd] deactivated")

    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
