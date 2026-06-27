"""
FlowShift Service v0.1.0
Python prototype – zero external dependencies (ctypes + stdlib only)
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

# ── Missing wintypes (not in all Python versions) ────────────────
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

# ── Win32 constants ──────────────────────────────────────────────
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
KEYEVENTF_EXTENDEDKEY = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
VK_CONTROL = 0x11
VK_MENU = 0x12

# ── ctypes structures ────────────────────────────────────────────
class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.wintypes.DWORD),
        ("scanCode", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt_x", ctypes.wintypes.LONG),
        ("pt_y", ctypes.wintypes.LONG),
        ("pt_pad", ctypes.c_long * 0),  # unnamed padding
        ("mouseData", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.wintypes.LONG), ("y", ctypes.wintypes.LONG)]

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.wintypes.DWORD),
        ("u", INPUT_UNION),
    ]

# ── Hook procedure types ─────────────────────────────────────────
HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.wintypes.LRESULT,
    ctypes.c_int,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)

# ── Global state (shared between hook callbacks and main logic) ──
class State:
    def __init__(self):
        self.active = False
        self.active_peer: str | None = None
        self.event_queue: queue.Queue[dict] = queue.Queue()
        self.inject_queue: queue.Queue[dict] = queue.Queue()
        self.peers: dict[str, tuple[socket.socket, str, int]] = {}
        self.config: dict = {}
        self.lock = threading.Lock()
        self._mods = 0

    def update_mods(self, vk: int, down: bool) -> int:
        bit = {0x11: 1, 0xA2: 1, 0xA3: 1,  # Ctrl
               0x10: 2, 0xA0: 2, 0xA1: 2,  # Shift
               0x12: 4, 0xA4: 4, 0xA5: 4,  # Alt
               0x5B: 8, 0x5C: 8}[vk]  # Win
        if down:
            self._mods |= bit
        else:
            self._mods &= ~bit
        return self._mods

    def current_mods(self) -> int:
        return self._mods

    def is_exit_hotkey(self, vk: int) -> bool:
        return (self._mods & 5) == 5 and vk == 0x30  # Ctrl+Alt+0

    def is_forward_hotkey(self, vk: int) -> bool:
        return (self._mods & 5) == 5 and 0x31 <= vk <= 0x39  # Ctrl+Alt+1..9

    def forward_hotkey_target(self, vk: int) -> int | None:
        idx = vk - 0x31
        peers = self.config.get("peers", [])
        if 0 <= idx < len(peers):
            return idx
        return None


state = State()

# ── Hook callbacks ───────────────────────────────────────────────
@HOOKPROC
def keyboard_proc(nCode: int, wParam: int, lParam: int) -> int:
    if nCode >= 0:
        kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        vk = kb.vkCode
        down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)

        with state.lock:
            state.update_mods(vk, down)

            if state.active and state.is_exit_hotkey(vk) and down:
                state.active = False
                state.active_peer = None
                print("<< local input restored")
                return 1

            if not state.active and state.is_forward_hotkey(vk) and down:
                idx = state.forward_hotkey_target(vk)
                if idx is not None:
                    peers = state.config.get("peers", [])
                    name = peers[idx]["name"]
                    state.active = True
                    state.active_peer = name
                    print(f">> forwarding to: {name}")
                    return 1

            if state.active and not (state.is_forward_hotkey(vk) or state.is_exit_hotkey(vk)):
                ev = {
                    "type": "key" if down else "key_up",
                    "code": vk,
                    "mods": state.current_mods(),
                }
                state.event_queue.put(ev)
                return 1

    return ctypes.windll.user32.CallNextHookEx(None, nCode, wParam, lParam)


@HOOKPROC
def mouse_proc(nCode: int, wParam: int, lParam: int) -> int:
    if nCode >= 0:
        with state.lock:
            if not state.active:
                return ctypes.windll.user32.CallNextHookEx(None, nCode, wParam, lParam)

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
            return ctypes.windll.user32.CallNextHookEx(None, nCode, wParam, lParam)

        state.event_queue.put(ev)
        return 1

    return ctypes.windll.user32.CallNextHookEx(None, nCode, wParam, lParam)


# ── Input injection (SendInput) ──────────────────────────────────
user32 = ctypes.windll.user32

def inject_send(ev: dict) -> None:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = KEYBDINPUT()

    t = ev.get("type", "")

    if t in ("key", "key_up"):
        inp.type = INPUT_KEYBOARD
        inp.u.ki.wVk = ev["code"]
        inp.u.ki.dwFlags = 0 if t == "key" else KEYEVENTF_KEYUP
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    elif t == "mousemove":
        inp.type = INPUT_MOUSE
        inp.u.mi.dx = ev["x"]
        inp.u.mi.dy = ev["y"]
        inp.u.mi.dwFlags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    elif t == "mousedown":
        inp.type = INPUT_MOUSE
        flags = [MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_MIDDLEDOWN]
        inp.u.mi.dwFlags = flags[ev["button"]]
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    elif t == "mouseup":
        inp.type = INPUT_MOUSE
        flags = [MOUSEEVENTF_LEFTUP, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_MIDDLEUP]
        inp.u.mi.dwFlags = flags[ev["button"]]
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    elif t == "wheel":
        inp.type = INPUT_MOUSE
        inp.u.mi.mouseData = ev["delta"] & 0xFFFFFFFF
        inp.u.mi.dwFlags = MOUSEEVENTF_WHEEL
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


# ── Network ──────────────────────────────────────────────────────
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

        # Ping vom Network-Scanner → kurz antworten und schliessen
        if first.get("type") == "ping":
            send_msg(conn, {"type": "pong"})
            conn.close()
            return

        if is_server:
            peer_name = first.get("display_name", str(addr))
            send_msg(conn, {
                "type": "hello",
                "device_id": state.config.get("device_id", ""),
                "display_name": state.config.get("device_name", ""),
                "os": "windows",
            })
        else:
            send_msg(conn, {
                "type": "hello",
                "device_id": state.config.get("device_id", ""),
                "display_name": state.config.get("device_name", ""),
                "os": "windows",
            })
            peer_name = first.get("display_name", str(addr))

        print(f"  peer connected: {peer_name} ({addr[0]})")

        with state.lock:
            state.peers[peer_name] = (conn, addr[0], addr[1])

        while True:
            msg = recv_msg(conn)
            if msg.get("type") == "input":
                for ev in msg.get("events", []):
                    state.inject_queue.put(ev)

    except (ConnectionError, OSError, json.JSONDecodeError) as e:
        pn = str(addr)
        with state.lock:
            for n, (c, *_) in state.peers.items():
                if c is conn:
                    pn = n
                    break
        print(f"  peer {pn} disconnected: {e}")
    finally:
        conn.close()
        with state.lock:
            to_remove = [n for n, (c, *_) in state.peers.items() if c is conn]
            for n in to_remove:
                del state.peers[n]


def network_thread(host: str, port: int) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(5)
    print(f"  TCP server listening on {host}:{port}")

    srv.settimeout(1.0)

    while True:
        try:
            conn, addr = srv.accept()
            threading.Thread(
                target=peer_handler, args=(conn, addr, True), daemon=True
            ).start()
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
                except (ConnectionError, OSError) as e:
                    print(f"  retry {n} ({h}:{po}) in 5s: {e}")
                time.sleep(5)

        threading.Thread(
            target=connect_one, args=(name, host, port), daemon=True
        ).start()


# ── Event forwarder ──────────────────────────────────────────────
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
        try:
            inject_send(ev)
        except Exception as e:
            print(f"inject error: {e}")


# ── Config ───────────────────────────────────────────────────────
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


# ── Hook thread (message pump) ───────────────────────────────────
def hook_thread() -> None:
    kb_hook = ctypes.windll.user32.SetWindowsHookExW(
        WH_KEYBOARD_LL, keyboard_proc, None, 0
    )
    ms_hook = ctypes.windll.user32.SetWindowsHookExW(
        WH_MOUSE_LL, mouse_proc, None, 0
    )

    if not kb_hook:
        err = ctypes.windll.kernel32.GetLastError()
        print(f"FATAL: SetWindowsHookExW(WH_KEYBOARD_LL) failed, error={err}")
        sys.exit(1)
    if not ms_hook:
        err = ctypes.windll.kernel32.GetLastError()
        print(f"FATAL: SetWindowsHookExW(WH_MOUSE_LL) failed, error={err}")
        sys.exit(1)

    print("  input hooks installed")

    msg = ctypes.wintypes.MSG()
    while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        ctypes.windll.user32.TranslateMessage(msg)
        ctypes.windll.user32.DispatchMessageW(msg)

    ctypes.windll.user32.UnhookWindowsHookEx(kb_hook)
    ctypes.windll.user32.UnhookWindowsHookEx(ms_hook)


# ── Main ─────────────────────────────────────────────────────────
def main() -> None:
    print(f"FlowShift Service v0.1.0")
    state.config = load_config()
    print(f"  device: {state.config.get('device_name', '?')}")
    print(f"  peers: {len(state.config.get('peers', []))}")
    for p in state.config.get("peers", []):
        print(f"    {p['name']} -> {p['host']}:{p.get('port', 45781)}")
    print(f"  hotkeys: Ctrl+Alt+N -> peer N, Ctrl+Alt+0 -> local")

    # Start threads
    threading.Thread(target=forward_loop, daemon=True).start()
    threading.Thread(target=inject_loop, daemon=True).start()
    threading.Thread(target=network_thread, args=("0.0.0.0", state.config.get("port", 45781)), daemon=True).start()
    threading.Thread(target=connect_to_peers, daemon=True).start()

    # Hook thread (blocking – message pump)
    hook_thread()


if __name__ == "__main__":
    main()
