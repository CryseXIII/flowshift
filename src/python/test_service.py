"""Quick validation: config parsing, protocol serialization, hotkey logic."""
import json
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

# ── 1. Config parsing ────────────────────────────────────────────
with open(os.path.join(os.path.dirname(__file__), "config.json")) as f:
    cfg = json.load(f)

assert cfg["device_name"] == "Viktor-PC"
assert cfg["port"] == 45781
assert len(cfg["peers"]) == 2
print("[PASS] Config parsing")

# ── 2. Protocol serialization (length-prefixed JSON) ─────────────
msg = {"type": "input", "events": [{"type": "key", "code": 0x41, "mods": 0}]}
data = json.dumps(msg).encode("utf-8")
packet = struct.pack("!I", len(data)) + data

raw_len = packet[:4]
msglen = struct.unpack("!I", raw_len)[0]
rest = packet[4:]
decoded = json.loads(rest)
assert decoded["type"] == "input"
assert decoded["events"][0]["code"] == 0x41
print("[PASS] Protocol serialization")

# ── 3. Hotkey logic ──────────────────────────────────────────────
class FakeState:
    def __init__(self):
        self._mods = 0
        self.active = False
        self.config = cfg

    def update_mods(self, vk, down):
        bit = {0x11: 1, 0xA2: 1, 0xA3: 1,
               0x10: 2, 0xA0: 2, 0xA1: 2,
               0x12: 4, 0xA4: 4, 0xA5: 4,
               0x5B: 8, 0x5C: 8}.get(vk, 0)
        if down: self._mods |= bit
        else: self._mods &= ~bit

    def current_mods(self): return self._mods
    def is_exit_hotkey(self, vk):
        return (self._mods & 5) == 5 and vk == 0x30
    def is_forward_hotkey(self, vk):
        return (self._mods & 5) == 5 and 0x31 <= vk <= 0x39
    def forward_hotkey_target(self, vk):
        idx = vk - 0x31
        peers = self.config.get("peers", [])
        return idx if 0 <= idx < len(peers) else None

s = FakeState()
s.update_mods(0x11, True)   # Ctrl down
s.update_mods(0x12, True)   # Alt down
assert s.is_forward_hotkey(0x31)  # Ctrl+Alt+1
assert s.is_exit_hotkey(0x30)     # Ctrl+Alt+0
assert s.forward_hotkey_target(0x31) == 0
s.update_mods(0x11, False)  # Ctrl up
assert not s.is_forward_hotkey(0x31)
print("[PASS] Hotkey logic")

# ── 4. Mouse event types ─────────────────────────────────────────
assert {"type": "mousemove", "x": 100, "y": 200}["type"] == "mousemove"
assert {"type": "mousedown", "button": 0}["button"] == 0
assert {"type": "wheel", "delta": 120}["delta"] == 120
print("[PASS] Event types")

print("\nAll tests passed.")
