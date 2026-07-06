"""FlowShift pure-logic tests (platform independent).

These tests import only ``runtime_model`` and therefore run on any OS without
Windows APIs. They cover exactly the bugs that were fixed:

* hotkey index drift (delete / rename / insert peers)
* legacy ``forward_<index>`` migration
* mouse scaling (corners, resolutions, negative virtual-screen origins)
* protocol framing (4-byte BE length + JSON, timeout-tolerant reader)
* peer resolution by stable identity
* pressed key / button cleanup

Run: ``python src/python/test_service.py``
"""
import os
import socket
import struct
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime_model as rm
from runtime_model import (
    MOD_CTRL, MOD_ALT,
    default_hotkeys, sync_hotkeys, migrate_hotkeys,
    peer_identity, make_forward_action, parse_forward_action,
    resolve_peer_by_action, hotkey_is_valid,
    format_hotkey, mods_name, vk_name,
    scale_mouse_point, normalize_absolute,
    pack_frame, FramedReader, PressTracker,
    HotkeyBinding, load_hotkeys,
    FORWARD_PREFIX, UNRESOLVED_ACTION,
)

_failures = []


def check(cond, label):
    if cond:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


# ── Hotkey text helpers ─────────────────────────────────────────────
check(mods_name(MOD_CTRL | MOD_ALT) == "Ctrl+Alt", "mods_name Ctrl+Alt")
check(mods_name(0) == "", "mods_name empty")
check(vk_name(0x31) == "1", "vk_name digit")
check(vk_name(0x41) == "A", "vk_name letter")
check(vk_name(0x70) == "F1", "vk_name F1")
check(format_hotkey(MOD_CTRL | MOD_ALT, 0x31) == "Ctrl+Alt+1", "format_hotkey Ctrl+Alt+1")
check(format_hotkey(0, 0x1B) == "Escape", "format_hotkey Escape")


# ── Peer identity ───────────────────────────────────────────────────
check(peer_identity({"device_id": "abcd1234"}) == "device:abcd1234", "identity by device_id")
check(peer_identity({"host": "192.168.1.5", "port": 45781}) == "endpoint:192.168.1.5:45781",
      "identity by endpoint")
check(make_forward_action({"device_id": "abcd1234"}) == "forward_peer:device:abcd1234",
      "make_forward_action")
check(parse_forward_action("forward_peer:device:abcd1234") == "device:abcd1234",
      "parse_forward_action")
check(parse_forward_action("return_local") is None, "parse return_local -> None")
check(parse_forward_action(UNRESOLVED_ACTION) is None, "parse unresolved -> None")


# ── default_hotkeys ─────────────────────────────────────────────────
peers = [
    {"name": "Peer1", "host": "192.168.1.100", "port": 45781, "device_id": "aaaa1111"},
    {"name": "Peer2", "host": "192.168.1.101", "port": 45781, "device_id": "bbbb2222"},
]
defaults = default_hotkeys(peers)
check(len(defaults) == len(peers) + 1, "default_hotkeys count")
check(defaults[-1]["action"] == "return_local", "default_hotkeys last is return_local")
check(defaults[0]["action"] == "forward_peer:device:aaaa1111", "default_hotkeys[0] -> Peer1")
check(defaults[1]["action"] == "forward_peer:device:bbbb2222", "default_hotkeys[1] -> Peer2")


# ── load_hotkeys / HotkeyBinding ────────────────────────────────────
cfg = {"peers": peers, "hotkeys": default_hotkeys(peers)}
bindings = load_hotkeys(cfg)
check(len(bindings) == len(peers) + 1, "load_hotkeys count")
check(bindings[0].matches(MOD_CTRL | MOD_ALT, 0x31), "binding[0] matches Ctrl+Alt+1")
check(bindings[-1].matches(MOD_CTRL | MOD_ALT, 0x30), "binding[-1] matches Ctrl+Alt+0")


# ── HOTKEY INDEX DRIFT: the core regression ─────────────────────────
def fresh_cfg():
    return {
        "peers": [
            {"name": "Alpha", "host": "10.0.0.1", "port": 45781, "device_id": "aaaa1111"},
            {"name": "Bravo", "host": "10.0.0.2", "port": 45781, "device_id": "bbbb2222"},
        ],
        "hotkeys": [],
    }


cfg = fresh_cfg()
sync_hotkeys(cfg)
action_b = make_forward_action({"device_id": "bbbb2222"})
check(resolve_peer_by_action(cfg, action_b)["name"] == "Bravo", "hotkey resolves to Bravo")

# Delete Alpha -> Bravo hotkey must still mean Bravo (never silently reassign).
cfg["peers"] = [p for p in cfg["peers"] if p["name"] != "Alpha"]
sync_hotkeys(cfg)
check(resolve_peer_by_action(cfg, action_b)["name"] == "Bravo",
      "after delete Alpha: hotkey still means Bravo")

# The old Alpha hotkey must be invalid, not pointing at Bravo.
action_a = make_forward_action({"device_id": "aaaa1111"})
check(resolve_peer_by_action(cfg, action_a) is None,
      "after delete Alpha: Alpha hotkey is invalid (not reassigned)")

# Rename Bravo -> hotkey still means the same peer (identity via device_id).
cfg["peers"][0]["name"] = "Bravo-Renamed"
sync_hotkeys(cfg)
resolved = resolve_peer_by_action(cfg, action_b)
check(resolved is not None and resolved["name"] == "Bravo-Renamed",
      "after rename Bravo: hotkey still means the same peer")

# Insert Charlie before Bravo -> Bravo hotkey unaffected.
cfg["peers"].insert(0, {"name": "Charlie", "host": "10.0.0.3", "port": 45781, "device_id": "cccc3333"})
sync_hotkeys(cfg)
check(resolve_peer_by_action(cfg, action_b)["name"] == "Bravo-Renamed",
      "after insert Charlie: Bravo hotkey unaffected")


# ── Legacy migration ────────────────────────────────────────────────
legacy = {
    "peers": [
        {"name": "Alpha", "host": "10.0.0.1", "port": 45781, "device_id": "aaaa1111"},
        {"name": "Bravo", "host": "10.0.0.2", "port": 45781, "device_id": "bbbb2222"},
    ],
    "hotkeys": [
        {"action": "forward_0", "mods": MOD_CTRL | MOD_ALT, "key": 0x31, "label": "Forward to Alpha"},
        {"action": "forward_1", "mods": MOD_CTRL | MOD_ALT, "key": 0x32, "label": "Forward to Bravo"},
        {"action": "return_local", "mods": MOD_CTRL | MOD_ALT, "key": 0x30, "label": "Return to local"},
    ],
}
changed = migrate_hotkeys(legacy)
check(changed, "legacy migration reports change")
check(legacy["hotkeys"][1]["action"] == "forward_peer:device:bbbb2222",
      "forward_1 migrated to Bravo identity")
check(legacy["hotkeys"][0]["action"] == "forward_peer:device:aaaa1111",
      "forward_0 migrated to Alpha identity")
check(legacy["hotkeys"][2]["action"] == "return_local", "return_local unchanged by migration")

# Legacy index out of range must not crash and must become invalid.
legacy2 = {
    "peers": [{"name": "Alpha", "host": "10.0.0.1", "port": 45781, "device_id": "aaaa1111"}],
    "hotkeys": [{"action": "forward_9", "mods": MOD_CTRL | MOD_ALT, "key": 0x39, "label": "x"}],
}
migrate_hotkeys(legacy2)
check(legacy2["hotkeys"][0]["action"] == UNRESOLVED_ACTION, "forward_9 -> unresolved (no crash)")
check(resolve_peer_by_action(legacy2, legacy2["hotkeys"][0]["action"]) is None,
      "unresolved legacy hotkey resolves to None")
check(not hotkey_is_valid(legacy2, legacy2["hotkeys"][0]), "unresolved hotkey is invalid")


# ── Endpoint-only peers (no device_id) ──────────────────────────────
ep_cfg = {"peers": [{"name": "Manual", "host": "192.168.5.9", "port": 45781}], "hotkeys": []}
sync_hotkeys(ep_cfg)
ep_action = make_forward_action(ep_cfg["peers"][0])
check(ep_action == "forward_peer:endpoint:192.168.5.9:45781", "endpoint action format")
check(resolve_peer_by_action(ep_cfg, ep_action)["name"] == "Manual", "endpoint peer resolves")


# ── Mouse scaling ───────────────────────────────────────────────────
src = {"left": 0, "top": 0, "width": 100, "height": 100}
tgt = {"left": 10, "top": 20, "width": 200, "height": 300}
check(tuple(round(v) for v in scale_mouse_point(0, 0, src, tgt)) == (10, 20), "scale top-left corner")
check(tuple(round(v) for v in scale_mouse_point(99, 99, src, tgt)) == (209, 319), "scale bottom-right corner")
# Out-of-range points are clamped into the target screen.
check(tuple(round(v) for v in scale_mouse_point(200, 200, src, tgt)) == (209, 319), "scale clamps overflow")
check(tuple(round(v) for v in scale_mouse_point(-50, -50, src, tgt)) == (10, 20), "scale clamps underflow")
# Negative virtual-screen origin (multi-monitor to the left / above).
neg = {"left": -1920, "top": -100, "width": 1920, "height": 1080}
x, y = scale_mouse_point(-1920, -100, neg, tgt)
check((round(x), round(y)) == (10, 20), "scale negative-origin top-left")
# Missing spec -> identity.
check(scale_mouse_point(5, 6, None, tgt) == (5, 6), "scale missing source -> identity")
# normalize_absolute maps to 0..65535 range.
check(normalize_absolute(0, 0, 1920) == 0, "normalize left edge")
check(normalize_absolute(1919, 0, 1920) == 65535, "normalize right edge")
check(normalize_absolute(-100, 0, 1920) == 0, "normalize clamps below 0")


# ── Protocol framing ────────────────────────────────────────────────
msg = {"type": "input", "events": [{"type": "key", "code": 0x41}]}
frame = pack_frame(msg)
n = struct.unpack("!I", frame[:4])[0]
check(n == len(frame) - 4, "pack_frame length prefix")
import json as _json
check(_json.loads(frame[4:]) == msg, "pack_frame payload roundtrip")


def test_framed_reader():
    a, b = socket.socketpair()
    try:
        reader = FramedReader(a)
        # Nothing sent yet -> timeout returns None, buffer intact.
        check(reader.read_message(0.05) is None, "FramedReader timeout -> None")
        # Send a message split across two writes (framing must not desync).
        data = pack_frame({"type": "hello", "n": 1})
        b.sendall(data[:3])
        check(reader.read_message(0.05) is None, "FramedReader partial frame -> None")
        b.sendall(data[3:])
        got = reader.read_message(0.5)
        check(got == {"type": "hello", "n": 1}, "FramedReader reassembles split frame")
        # Two back-to-back messages in one write.
        b.sendall(pack_frame({"i": 1}) + pack_frame({"i": 2}))
        check(reader.read_message(0.5) == {"i": 1}, "FramedReader message 1 of 2")
        check(reader.read_message(0.5) == {"i": 2}, "FramedReader message 2 of 2")
    finally:
        a.close()
        b.close()


test_framed_reader()


# ── Pressed key / button cleanup ────────────────────────────────────
tr = PressTracker()
tr.apply({"type": "key", "code": 0x41})
tr.apply({"type": "key", "code": 0x42})
tr.apply({"type": "key_up", "code": 0x41})     # A released
tr.apply({"type": "mousedown", "button": 0})
tr.apply({"type": "mousemove", "x": 1, "y": 2})  # ignored (not stateful)
releases = tr.release_events()
check({"type": "key_up", "code": 0x42} in releases, "cleanup releases held key B")
check({"type": "key_up", "code": 0x41} not in releases, "cleanup does not release freed key A")
check({"type": "mouseup", "button": 0} in releases, "cleanup releases held mouse button")
check(tr.release_events() == [], "cleanup clears state after release")


# ── Summary ─────────────────────────────────────────────────────────
print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
    sys.exit(1)
print("All tests passed.")
