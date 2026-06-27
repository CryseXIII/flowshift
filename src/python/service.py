"""
FlowShift Service v0.2.1
Critical fixes: 64-bit dwExtraInfo, exception-safe hooks, union init
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import queue
import socket
import struct
import sys
import threading
import time

if not hasattr(ctypes.wintypes, 'LRESULT'):
    ctypes.wintypes.LRESULT = ctypes.c_long
if not hasattr(ctypes.wintypes, 'HHOOK'):
    ctypes.wintypes.HHOOK = ctypes.c_void_p
if not hasattr(ctypes.wintypes, 'ATOM'):
    ctypes.wintypes.ATOM = ctypes.c_ushort
if not hasattr(ctypes.wintypes, 'BOOL'):
    ctypes.wintypes.BOOL = ctypes.c_int
if not hasattr(ctypes.wintypes, 'HINSTANCE'):
    ctypes.wintypes.HINSTANCE = ctypes.c_void_p
if not hasattr(ctypes.wintypes, 'HMODULE'):
    ctypes.wintypes.HMODULE = ctypes.c_void_p

# ULONG_PTR = pointer-sized integer (4 bytes on 32-bit, 8 bytes on 64-bit)
# This matches dwExtraInfo in Win32 structs, avoiding pointer type issues.
try:
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        ULONG_PTR = ctypes.c_ulonglong
    else:
        ULONG_PTR = ctypes.c_ulong
except Exception:
    ULONG_PTR = ctypes.c_size_t

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_QUIT = 0x0012

INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800

MOD_CTRL = 1
MOD_SHIFT = 2
MOD_ALT = 4
MOD_WIN = 8

MODIFIER_VKS = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}

VK_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Escape",
    0x20: "Space", 0x2D: "Insert", 0x2E: "Delete", 0x24: "Home",
    0x23: "End", 0x21: "PageUp", 0x22: "PageDown",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x2C: "PrintScreen", 0x13: "Pause", 0x91: "ScrollLock",
    0x70: "F1", 0x71: "F2", 0x72: "F3", 0x73: "F4",
    0x74: "F5", 0x75: "F6", 0x76: "F7", 0x77: "F8",
    0x78: "F9", 0x79: "F10", 0x7A: "F11", 0x7B: "F12",
    0x7C: "F13", 0x7D: "F14", 0x7E: "F15", 0x7F: "F16",
    0x80: "F17", 0x81: "F18", 0x82: "F19", 0x83: "F20",
    0x5B: "Win", 0x5C: "Win",
    0xA0: "LShift", 0xA1: "RShift",
    0xA2: "LCtrl", 0xA3: "RCtrl",
    0xA4: "LAlt", 0xA5: "RAlt",
}

MOD_NAMES = {MOD_CTRL: "Ctrl", MOD_SHIFT: "Shift", MOD_ALT: "Alt", MOD_WIN: "Win"}

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.wintypes.LONG), ("y", ctypes.wintypes.LONG)]

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.wintypes.DWORD),
        ("scanCode", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]

class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.wintypes.LONG),
        ("top", ctypes.wintypes.LONG),
        ("right", ctypes.wintypes.LONG),
        ("bottom", ctypes.wintypes.LONG),
    ]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.wintypes.DWORD),
        ("u", INPUT_UNION),
    ]

HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.wintypes.LRESULT,
    ctypes.c_int,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Notschalter: Kill-Datei in %TEMP%
KILL_FILE = os.path.join(os.environ.get("TEMP", "."), "flowshift_kill")
_emergency_stop = False


def vk_name(vk: int) -> str:
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    return VK_NAMES.get(vk, f"VK_0x{vk:02X}")


def mods_name(mods: int) -> str:
    parts = []
    for bit, name in sorted(MOD_NAMES.items()):
        if mods & bit:
            parts.append(name)
    return "+".join(parts) if parts else ""


def format_hotkey(mods: int, vk: int) -> str:
    prefix = mods_name(mods)
    key = vk_name(vk)
    return f"{prefix}+{key}" if prefix else key


def get_mods() -> int:
    m = 0
    if user32.GetAsyncKeyState(0x11) & 0x8000 or user32.GetAsyncKeyState(0xA2) & 0x8000 or user32.GetAsyncKeyState(0xA3) & 0x8000:
        m |= MOD_CTRL
    if user32.GetAsyncKeyState(0x10) & 0x8000 or user32.GetAsyncKeyState(0xA0) & 0x8000 or user32.GetAsyncKeyState(0xA1) & 0x8000:
        m |= MOD_SHIFT
    if user32.GetAsyncKeyState(0x12) & 0x8000 or user32.GetAsyncKeyState(0xA4) & 0x8000 or user32.GetAsyncKeyState(0xA5) & 0x8000:
        m |= MOD_ALT
    if user32.GetAsyncKeyState(0x5B) & 0x8000 or user32.GetAsyncKeyState(0x5C) & 0x8000:
        m |= MOD_WIN
    return m


class HotkeyBinding:
    def __init__(self, action: str, mods: int, key: int, label: str = ""):
        self.action = action
        self.mods = mods
        self.key = key
        self.label = label

    def matches(self, mods: int, vk: int) -> bool:
        return self.mods == mods and self.key == vk

    def display(self) -> str:
        return format_hotkey(self.mods, self.key)


def default_hotkeys(peers: list) -> list[dict]:
    hk = []
    for i, p in enumerate(peers):
        if i < 9:
            hk.append({"action": f"forward_{i}", "mods": MOD_CTRL | MOD_ALT, "key": 0x31 + i, "label": f"Forward to {p['name']}"})
    hk.append({"action": "return_local", "mods": MOD_CTRL | MOD_ALT, "key": 0x30, "label": "Return to local"})
    return hk


def load_hotkeys(cfg: dict) -> list[HotkeyBinding]:
    raw = cfg.get("hotkeys")
    if not raw:
        raw = default_hotkeys(cfg.get("peers", []))
        cfg["hotkeys"] = raw
    bindings = []
    for h in raw:
        bindings.append(HotkeyBinding(
            action=h["action"], mods=h.get("mods", MOD_CTRL | MOD_ALT),
            key=h["key"], label=h.get("label", h["action"]),
        ))
    return bindings


class State:
    def __init__(self):
        self.active = False
        self.active_peer: str | None = None
        self.event_queue: queue.Queue[dict] = queue.Queue()
        self.inject_queue: queue.Queue[dict] = queue.Queue()
        self.peers: dict[str, tuple[socket.socket, str, int]] = {}
        self.config: dict = {}
        self.hotkeys: list[HotkeyBinding] = []
        self.lock = threading.Lock()
        self._mods = 0

    def update_mods(self, vk: int, down: bool) -> int:
        bit = {0x11: 1, 0xA2: 1, 0xA3: 1,
               0x10: 2, 0xA0: 2, 0xA1: 2,
               0x12: 4, 0xA4: 4, 0xA5: 4,
               0x5B: 8, 0x5C: 8}.get(vk, 0)
        if down:
            self._mods |= bit
        else:
            self._mods &= ~bit
        return self._mods

    def current_mods(self) -> int:
        return self._mods

    def find_hotkey(self, mods: int, vk: int) -> HotkeyBinding | None:
        for hk in self.hotkeys:
            if hk.matches(mods, vk):
                return hk
        return None

    def get_capture_region(self):
        r = self.config.get("capture_region")
        if r:
            return (r["x"], r["y"], r["x"] + r["width"], r["y"] + r["height"])
        return None

    def set_clip(self, enable: bool):
        try:
            if enable:
                region = self.get_capture_region()
                if region:
                    r = RECT(region[0], region[1], region[2], region[3])
                    user32.ClipCursor(ctypes.byref(r))
                else:
                    user32.ClipCursor(None)
            else:
                user32.ClipCursor(None)
        except Exception:
            pass


state = State()


@HOOKPROC
KILL_VK = 0x4B  # K

def is_kill_combo(mods, vk):
    return mods == 0x0F and vk == KILL_VK

def keyboard_proc(nCode: int, wParam: int, lParam: int) -> int:
    try:
        if _emergency_stop:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        if nCode >= 0:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = kb.vkCode
            down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            if down and is_kill_combo(state.current_mods(), vk):
                global _emergency_stop
                _emergency_stop = True
                state.active = False
                state.active_peer = None
                try:
                    with open(KILL_FILE, "w") as _f:
                        _f.write("1")
                except Exception:
                    pass
                user32.PostQuitMessage(0)
                return 1

            with state.lock:
                state.update_mods(vk, down)

                if down:
                    mods = state.current_mods()
                    hk = state.find_hotkey(mods, vk)
                    if hk:
                        if hk.action == "return_local" and state.active:
                            state.active = False
                            state.active_peer = None
                            state.set_clip(False)
                            return 1
                        elif hk.action.startswith("forward_") and not state.active:
                            idx = int(hk.action.split("_")[1])
                            peers = state.config.get("peers", [])
                            if 0 <= idx < len(peers):
                                name = peers[idx]["name"]
                                state.active = True
                                state.active_peer = name
                                state.set_clip(True)
                                return 1

                if state.active:
                    if down:
                        pass_through = state.find_hotkey(state.current_mods(), vk)
                    else:
                        pass_through = None
                    if not pass_through:
                        ev = {"type": "key" if down else "key_up", "code": vk}
                        state.event_queue.put(ev)
                        return 1
    except Exception:
        pass
    return user32.CallNextHookEx(None, nCode, wParam, lParam)


@HOOKPROC
def mouse_proc(nCode: int, wParam: int, lParam: int) -> int:
    try:
        if _emergency_stop:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        if nCode >= 0:
            with state.lock:
                if not state.active:
                    return user32.CallNextHookEx(None, nCode, wParam, lParam)

            ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents

            if wParam == WM_MOUSEMOVE:
                ev = {"type": "mousemove", "x": ms.pt.x, "y": ms.pt.y}
            elif wParam == WM_LBUTTONDOWN:
                ev = {"type": "mousedown", "button": 0}
            elif wParam == WM_LBUTTONUP:
                ev = {"type": "mouseup", "button": 0}
            elif wParam == WM_RBUTTONDOWN:
                ev = {"type": "mousedown", "button": 1}
            elif wParam == WM_RBUTTONUP:
                ev = {"type": "mouseup", "button": 1}
            elif wParam == WM_MBUTTONDOWN:
                ev = {"type": "mousedown", "button": 2}
            elif wParam == WM_MBUTTONUP:
                ev = {"type": "mouseup", "button": 2}
            elif wParam == WM_MOUSEWHEEL:
                delta = ctypes.c_short(ms.mouseData >> 16).value
                ev = {"type": "wheel", "delta": delta}
            else:
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            state.event_queue.put(ev)
            return 1
    except Exception:
        pass
    return user32.CallNextHookEx(None, nCode, wParam, lParam)


def inject_send(ev: dict) -> None:
    try:
        inp = INPUT()
        t = ev.get("type", "")

        if t in ("key", "key_up"):
            inp.type = INPUT_KEYBOARD
            ki = KEYBDINPUT()
            ki.wVk = ev["code"]
            ki.dwFlags = 0 if t == "key" else KEYEVENTF_KEYUP
            inp.u.ki = ki
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

        elif t == "mousemove":
            inp.type = INPUT_MOUSE
            mi = MOUSEINPUT()
            mi.dx = ev["x"]
            mi.dy = ev["y"]
            mi.dwFlags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE
            inp.u.mi = mi
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

        elif t == "mousedown":
            inp.type = INPUT_MOUSE
            mi = MOUSEINPUT()
            flags = [MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_MIDDLEDOWN]
            mi.dwFlags = flags[ev["button"]]
            inp.u.mi = mi
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

        elif t == "mouseup":
            inp.type = INPUT_MOUSE
            mi = MOUSEINPUT()
            flags = [MOUSEEVENTF_LEFTUP, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_MIDDLEUP]
            mi.dwFlags = flags[ev["button"]]
            inp.u.mi = mi
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

        elif t == "wheel":
            inp.type = INPUT_MOUSE
            mi = MOUSEINPUT()
            mi.mouseData = ev["delta"] & 0xFFFFFFFF
            mi.dwFlags = MOUSEEVENTF_WHEEL
            inp.u.mi = mi
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    except Exception:
        pass


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def send_msg(sock: socket.socket, msg: dict) -> None:
    data = json.dumps(msg).encode("utf-8")
    sock.sendall(struct.pack("!I", len(data)) + data)


def recv_msg(sock: socket.socket) -> dict:
    raw_len = recv_exact(sock, 4)
    msglen = struct.unpack("!I", raw_len)[0]
    data = recv_exact(sock, msglen)
    return json.loads(data)


def peer_handler(conn: socket.socket, addr: tuple, is_server: bool) -> None:
    try:
        first = recv_msg(conn)
        if first.get("type") == "ping":
            send_msg(conn, {"type": "pong"})
            conn.close()
            return

        if is_server:
            peer_name = first.get("display_name", str(addr))
            send_msg(conn, {"type": "hello", "device_id": state.config.get("device_id", ""),
                            "display_name": state.config.get("device_name", ""), "os": "windows"})
        else:
            send_msg(conn, {"type": "hello", "device_id": state.config.get("device_id", ""),
                            "display_name": state.config.get("device_name", ""), "os": "windows"})
            peer_name = first.get("display_name", str(addr))

        with state.lock:
            state.peers[peer_name] = (conn, addr[0], addr[1])

        while True:
            msg = recv_msg(conn)
            if msg.get("type") == "input":
                for ev in msg.get("events", []):
                    state.inject_queue.put(ev)

    except (ConnectionError, OSError, json.JSONDecodeError):
        pass
    finally:
        conn.close()
        with state.lock:
            for n, (c, *_) in list(state.peers.items()):
                if c is conn:
                    del state.peers[n]
                    break


def network_thread(host: str, port: int) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(5)
    srv.settimeout(1.0)

    while True:
        try:
            conn, addr = srv.accept()
            threading.Thread(target=peer_handler, args=(conn, addr, True), daemon=True).start()
        except socket.timeout:
            continue
        except OSError:
            break


def connect_to_peers() -> None:
    peers = state.config.get("peers", [])
    for p in peers:
        name = p["name"]
        host = p["host"]
        port = p.get("port", 45781)

        def connect_one(n: str, h: str, po: int) -> None:
            while True:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.connect((h, po))
                    peer_handler(sock, (h, po), False)
                except (ConnectionError, OSError):
                    pass
                time.sleep(5)

        threading.Thread(target=connect_one, args=(name, host, port), daemon=True).start()


def forward_loop() -> None:
    while True:
        ev = state.event_queue.get()
        with state.lock:
            peer_name = state.active_peer
            if peer_name is None:
                continue
            conn_data = state.peers.get(peer_name)
            if conn_data is None:
                continue
            conn, *_ = conn_data

        try:
            send_msg(conn, {"type": "input", "events": [ev]})
        except (ConnectionError, OSError):
            pass


def inject_loop() -> None:
    while True:
        ev = state.inject_queue.get()
        inject_send(ev)


def load_config() -> dict:
    paths = [
        os.path.expandvars("%APPDATA%/flowshift/config.json"),
        os.path.expanduser("~/.config/flowshift/config.json"),
        os.path.join(os.path.dirname(__file__), "config.json"),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)

    default = {
        "device_name": os.environ.get("COMPUTERNAME", "unknown"),
        "device_id": __import__("uuid").uuid4().hex[:8],
        "port": 45781,
        "peers": [],
    }
    cfg_path = paths[-1]
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    with open(cfg_path, "w") as f:
        json.dump(default, f, indent=2)
    return default


def hook_thread() -> None:
    kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, keyboard_proc, None, 0)
    ms_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, mouse_proc, None, 0)

    if not kb_hook:
        err = kernel32.GetLastError()
        print(f"FATAL: SetWindowsHookExW(WH_KEYBOARD_LL) failed, error={err}")
        sys.exit(1)
    if not ms_hook:
        err = kernel32.GetLastError()
        print(f"FATAL: SetWindowsHookExW(WH_MOUSE_LL) failed, error={err}")
        sys.exit(1)

    print("  input hooks installed")

    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(msg)
        user32.DispatchMessageW(msg)

    user32.UnhookWindowsHookEx(kb_hook)
    user32.UnhookWindowsHookEx(ms_hook)


def watchdog_thread() -> None:
    """Beobachtet Kill-Datei. Bei Fund: Notschalter aktivieren und beenden."""
    global _emergency_stop
    while True:
        if os.path.exists(KILL_FILE):
            try:
                os.remove(KILL_FILE)
            except Exception:
                pass
            _emergency_stop = True
            user32.PostQuitMessage(0)
            break
        time.sleep(1.0)


def main() -> None:
    state.config = load_config()
    state.hotkeys = load_hotkeys(state.config)

    print(f"FlowShift Service v0.2.1")
    print(f"  device: {state.config.get('device_name', '?')}")
    print(f"  peers: {len(state.config.get('peers', []))}")
    for p in state.config.get("peers", []):
        print(f"    {p['name']} -> {p['host']}:{p.get('port', 45781)}")
    for hk in state.hotkeys:
        print(f"    {hk.display():20s} -> {hk.label}")

    threading.Thread(target=forward_loop, daemon=True).start()
    threading.Thread(target=inject_loop, daemon=True).start()
    threading.Thread(target=network_thread, args=("0.0.0.0", state.config.get("port", 45781)), daemon=True).start()
    threading.Thread(target=connect_to_peers, daemon=True).start()
    threading.Thread(target=watchdog_thread, daemon=True).start()

    print("  Notschalter aktiv: Datei %TEMP%\\flowshift_kill anlegen zum Beenden")
    hook_thread()


if __name__ == "__main__":
    main()
