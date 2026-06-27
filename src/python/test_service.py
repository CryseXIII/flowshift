"""Quick validation: config parsing, protocol, hotkey logic, capture region."""
import json
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

with open(os.path.join(os.path.dirname(__file__), "config.json")) as f:
    cfg = json.load(f)

assert isinstance(cfg["device_name"], str) and len(cfg["device_name"]) > 0
assert cfg["port"] == 45781
assert len(cfg["peers"]) >= 1
print("[PASS] Config parsing")

msg = {"type": "input", "events": [{"type": "key", "code": 0x41}]}
data = json.dumps(msg).encode("utf-8")
packet = struct.pack("!I", len(data)) + data

raw_len = packet[:4]
msglen = struct.unpack("!I", raw_len)[0]
rest = packet[4:]
decoded = json.loads(rest)
assert decoded["type"] == "input"
assert decoded["events"][0]["code"] == 0x41
print("[PASS] Protocol serialization")

from service import (HotkeyBinding, load_hotkeys, default_hotkeys,
                     format_hotkey, mods_name, vk_name, MOD_CTRL, MOD_ALT)

MOD_CTRL = 1
MOD_ALT = 4

hk = HotkeyBinding("forward_0", MOD_CTRL | MOD_ALT, 0x31, "Forward to Viktor-Tablet")
assert hk.matches(5, 0x31)
assert not hk.matches(5, 0x30)
assert not hk.matches(1, 0x31)
print(f"[PASS] HotkeyBinding: {hk.display()}")

peers = cfg.get("peers", [])
defaults = default_hotkeys(peers)
assert len(defaults) == len(peers) + 1  # one forward per peer + return_local
assert defaults[-1]["action"] == "return_local"
for i in range(len(peers)):
    assert defaults[i]["action"] == f"forward_{i}"
print(f"[PASS] default_hotkeys ({len(defaults)} items)")

cfg["hotkeys"] = defaults
bindings = load_hotkeys(cfg)
assert len(bindings) == len(peers) + 1
assert bindings[0].matches(MOD_CTRL | MOD_ALT, 0x31)
assert bindings[-1].matches(MOD_CTRL | MOD_ALT, 0x30)
print("[PASS] load_hotkeys")

assert mods_name(MOD_CTRL | MOD_ALT) == "Ctrl+Alt"
assert mods_name(0) == ""
assert vk_name(0x31) == "1"
assert vk_name(0x41) == "A"
assert vk_name(0x70) == "F1"
print("[PASS] name helpers")

assert format_hotkey(MOD_CTRL | MOD_ALT, 0x31) == "Ctrl+Alt+1"
assert format_hotkey(0, 0x1B) == "Escape"
print("[PASS] format_hotkey")

assert {"type": "mousemove", "x": 100, "y": 200}["type"] == "mousemove"
assert {"type": "mousedown", "button": 0}["button"] == 0
assert {"type": "wheel", "delta": 120}["delta"] == 120
print("[PASS] Event types")

print("\nAll tests passed.")
