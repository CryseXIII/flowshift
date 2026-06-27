"""
FlowShift Tray v0.2.1
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
import subprocess
import sys
import threading
import time

BASE = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(BASE, "config.json")
GUI_FILE = os.path.join(BASE, "gui.py")
AUTO_START_NAME = "FlowShift"

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205

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

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 1
NIF_ICON = 2
NIF_TIP = 4

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

MF_STRING = 0
MF_SEPARATOR = 0x0800
TPM_RETURNCMD = 0x0100
TPM_LEFTALIGN = 0
TPM_BOTTOMALIGN = 0x0020

ID_OPEN = 1001
ID_TOGGLE = 1002
ID_STARTUP = 1003
ID_EXIT = 1004

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
    0x5B: "Win", 0x5C: "Win",
    0xA0: "LShift", 0xA1: "RShift",
    0xA2: "LCtrl", 0xA3: "RCtrl",
    0xA4: "LAlt", 0xA5: "RAlt",
}

MOD_NAMES = {MOD_CTRL: "Ctrl", MOD_SHIFT: "Shift", MOD_ALT: "Alt", MOD_WIN: "Win"}

try:
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        ULONG_PTR = ctypes.c_ulonglong
    else:
        ULONG_PTR = ctypes.c_ulong
except Exception:
    ULONG_PTR = ctypes.c_size_t

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

KILL_FILE = os.path.join(os.environ.get("TEMP", "."), "flowshift_kill")
_emergency_stop = False

LRESULT = ctypes.c_long
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_long
HICON = ctypes.c_void_p
HWND = ctypes.c_void_p
HINSTANCE = ctypes.c_void_p
HHOOK = ctypes.c_void_p
HMENU = ctypes.c_void_p
HANDLE = ctypes.c_void_p


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint), ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", HINSTANCE),
        ("hIcon", HICON), ("hCursor", HANDLE), ("hbrBackground", HANDLE),
        ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", HICON),
    ]

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", HWND), ("message", ctypes.c_uint),
        ("wParam", WPARAM), ("lParam", LPARAM),
        ("time", ctypes.c_ulong), ("pt", POINT),
    ]

class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint), ("hwnd", HWND), ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint), ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", HICON), ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint), ("dwStateMask", ctypes.c_uint),
        ("szInfo", ctypes.c_wchar * 256), ("uVersion", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", ctypes.c_uint),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", HICON),
    ]

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.c_ulong), ("scanCode", ctypes.c_ulong),
        ("flags", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT), ("mouseData", ctypes.c_ulong),
        ("flags", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR),
    ]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", INPUT_UNION)]

class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
    ]


def vk_name(vk):
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    return VK_NAMES.get(vk, f"VK_0x{vk:02X}")


def mods_name(mods):
    parts = []
    for bit, name in sorted(MOD_NAMES.items()):
        if mods & bit:
            parts.append(name)
    return "+".join(parts) if parts else ""


def format_hotkey(mods, vk):
    prefix = mods_name(mods)
    key = vk_name(vk)
    return f"{prefix}+{key}" if prefix else key


def open_gui():
    try:
        subprocess.Popen(
            [sys.executable, GUI_FILE],
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except Exception:
        pass


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"device_name": "Unbekannt", "device_id": "", "port": 45781, "peers": []}


def default_hotkeys(peers):
    hk = []
    for i, p in enumerate(peers):
        if i < 9:
            hk.append({"action": f"forward_{i}", "mods": MOD_CTRL | MOD_ALT, "key": 0x31 + i, "label": f"Forward to {p['name']}"})
    hk.append({"action": "return_local", "mods": MOD_CTRL | MOD_ALT, "key": 0x30, "label": "Return to local"})
    return hk


class HotkeyBinding:
    def __init__(self, action, mods, key, label=""):
        self.action = action
        self.mods = mods
        self.key = key
        self.label = label

    def matches(self, mods, vk):
        return self.mods == mods and self.key == vk

    def display(self):
        return format_hotkey(self.mods, self.key)


def load_hotkeys(cfg):
    raw = cfg.get("hotkeys")
    if not raw:
        raw = default_hotkeys(cfg.get("peers", []))
        cfg["hotkeys"] = raw
    return [HotkeyBinding(h["action"], h.get("mods", MOD_CTRL | MOD_ALT), h["key"], h.get("label", h["action"])) for h in raw]


class InputState:
    def __init__(self):
        self.active = False
        self.active_peer = None
        self._mods = 0
        self.event_queue = queue.Queue()
        self.inject_queue = queue.Queue()
        self.config = load_config()
        self.hotkeys = load_hotkeys(self.config)
        self.peers = {}
        self.lock = threading.Lock()
        self.kb_hook = HHOOK()
        self.ms_hook = HHOOK()
        self.enabled = True

    def update_mods(self, vk, down):
        bit = {0x11: 1, 0xA2: 1, 0xA3: 1, 0x10: 2, 0xA0: 2, 0xA1: 2,
               0x12: 4, 0xA4: 4, 0xA5: 4, 0x5B: 8, 0x5C: 8}.get(vk, 0)
        if down:
            self._mods |= bit
        else:
            self._mods &= ~bit

    def current_mods(self):
        return self._mods

    def find_hotkey(self, mods, vk):
        for hk in self.hotkeys:
            if hk.matches(mods, vk):
                return hk
        return None

    def get_capture_region(self):
        r = self.config.get("capture_region")
        if r:
            return (r["x"], r["y"], r["x"] + r["width"], r["y"] + r["height"])
        return None

    def set_clip(self, enable):
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


istate = InputState()
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, ctypes.c_ulong, ctypes.c_long)


@HOOKPROC
def keyboard_proc(code, wparam, lparam):
    try:
        if _emergency_stop:
            return user32.CallNextHookEx(None, code, wparam, lparam)
        if code >= 0:
            kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = kb.vkCode
            down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)

            with istate.lock:
                if not istate.enabled:
                    return user32.CallNextHookEx(None, code, wparam, lparam)

                istate.update_mods(vk, down)

                if down:
                    mods = istate.current_mods()
                    hk = istate.find_hotkey(mods, vk)
                    if hk:
                        if hk.action == "return_local" and istate.active:
                            istate.active = False
                            istate.active_peer = None
                            istate.set_clip(False)
                            update_tray()
                            return 1
                        elif hk.action.startswith("forward_") and not istate.active:
                            idx = int(hk.action.split("_")[1])
                            peers = istate.config.get("peers", [])
                            if 0 <= idx < len(peers):
                                name = peers[idx]["name"]
                                istate.active = True
                                istate.active_peer = name
                                istate.set_clip(True)
                                update_tray()
                                return 1

                if istate.active:
                    if down:
                        pass_through = istate.find_hotkey(istate.current_mods(), vk)
                    else:
                        pass_through = None
                    if not pass_through:
                        ev = {"type": "key" if down else "key_up", "code": vk}
                        istate.event_queue.put(ev)
                        return 1
    except Exception:
        pass
    return user32.CallNextHookEx(None, code, wparam, lparam)


@HOOKPROC
def mouse_proc(code, wparam, lparam):
    try:
        if _emergency_stop:
            return user32.CallNextHookEx(None, code, wparam, lparam)
        if code >= 0:
            with istate.lock:
                if not istate.enabled or not istate.active:
                    return user32.CallNextHookEx(None, code, wparam, lparam)

            ms = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            ev = None
            if wparam == WM_MOUSEMOVE:
                ev = {"type": "mousemove", "x": ms.pt.x, "y": ms.pt.y}
            elif wparam == WM_LBUTTONDOWN:
                ev = {"type": "mousedown", "button": 0}
            elif wparam == WM_LBUTTONUP:
                ev = {"type": "mouseup", "button": 0}
            elif wparam == WM_RBUTTONDOWN:
                ev = {"type": "mousedown", "button": 1}
            elif wparam == WM_RBUTTONUP:
                ev = {"type": "mouseup", "button": 1}
            elif wparam == WM_MBUTTONDOWN:
                ev = {"type": "mousedown", "button": 2}
            elif wparam == WM_MBUTTONUP:
                ev = {"type": "mouseup", "button": 2}
            elif wparam == WM_MOUSEWHEEL:
                ev = {"type": "wheel", "delta": ctypes.c_short(ms.mouseData >> 16).value}

            if ev:
                istate.event_queue.put(ev)
                return 1
    except Exception:
        pass
    return user32.CallNextHookEx(None, code, wparam, lparam)


def inject(ev):
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


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise ConnectionError("closed")
        buf += c
    return buf


def send_msg(sock, msg):
    d = json.dumps(msg).encode()
    sock.sendall(struct.pack("!I", len(d)) + d)


def recv_msg(sock):
    r = recv_exact(sock, 4)
    l = struct.unpack("!I", r)[0]
    return json.loads(recv_exact(sock, l))


def peer_handler(conn, addr, is_server):
    name = str(addr)
    try:
        first = recv_msg(conn)
        if first.get("type") == "ping":
            send_msg(conn, {"type": "pong"})
            conn.close()
            return
        if is_server:
            name = first.get("display_name", str(addr))
            send_msg(conn, {"type": "hello", "device_id": istate.config.get("device_id", ""),
                            "display_name": istate.config.get("device_name", ""), "os": "windows"})
        else:
            send_msg(conn, {"type": "hello", "device_id": istate.config.get("device_id", ""),
                            "display_name": istate.config.get("device_name", ""), "os": "windows"})
        with istate.lock:
            istate.peers[name] = (conn, addr[0], addr[1])
        while True:
            msg = recv_msg(conn)
            if msg.get("type") == "input":
                for ev in msg.get("events", []):
                    istate.inject_queue.put(ev)
    except Exception:
        pass
    finally:
        conn.close()
        with istate.lock:
            for n, (c, *_) in list(istate.peers.items()):
                if c is conn:
                    del istate.peers[n]


def network_thread():
    port = istate.config.get("port", 45781)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        srv.settimeout(1.0)
        while True:
            try:
                c, a = srv.accept()
                threading.Thread(target=peer_handler, args=(c, a, True), daemon=True).start()
            except socket.timeout:
                continue
    except OSError:
        pass


def connect_to_peers():
    for p in istate.config.get("peers", []):
        name, host, port = p["name"], p["host"], p.get("port", 45781)
        def connect_one(n, h, po):
            while True:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.connect((h, po))
                    peer_handler(s, (h, po), False)
                except Exception:
                    pass
                time.sleep(5)
        threading.Thread(target=connect_one, args=(name, host, port), daemon=True).start()


def forward_loop():
    while True:
        ev = istate.event_queue.get()
        with istate.lock:
            peer = istate.active_peer
            conn_data = istate.peers.get(peer) if peer else None
        if conn_data:
            try:
                send_msg(conn_data[0], {"type": "input", "events": [ev]})
            except Exception:
                pass


def inject_loop():
    while True:
        ev = istate.inject_queue.get()
        inject(ev)


_hwnd = None
_tray_nid = None


def create_tray(hwnd):
    global _tray_nid
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hwnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAYICON
    nid.hIcon = user32.LoadIconW(None, ctypes.c_void_p(0x7F00))
    nid.szTip = "FlowShift"
    user32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
    _tray_nid = nid
    update_tray()


def update_tray():
    global _tray_nid
    if _tray_nid is None:
        return
    s = " Active" if istate.active else (" Paused" if not istate.enabled else " Standby")
    target = f" -> {istate.active_peer}" if istate.active else ""
    _tray_nid.szTip = f"FlowShift{s}{target}"
    user32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(_tray_nid))


def remove_tray():
    global _tray_nid
    if _tray_nid is not None:
        user32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(_tray_nid))
        _tray_nid = None


def show_menu(hwnd):
    hmenu = user32.CreatePopupMenu()
    user32.AppendMenuW(hmenu, MF_STRING, ID_TOGGLE,
                       "Stop forwarding" if istate.active else "Start forwarding")
    user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
    user32.AppendMenuW(hmenu, MF_STRING, ID_OPEN, "Settings")
    autostart = AutoStartManager.is_set()
    user32.AppendMenuW(hmenu, MF_STRING, ID_STARTUP,
                       f"{'v' if autostart else ' '} Auto-start with Windows")
    user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
    user32.AppendMenuW(hmenu, MF_STRING, ID_EXIT, "Exit")

    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.SetForegroundWindow(hwnd)
    cmd = user32.TrackPopupMenu(hmenu, TPM_RETURNCMD | TPM_LEFTALIGN | TPM_BOTTOMALIGN,
                                 pt.x, pt.y, 0, hwnd, None)
    user32.PostMessageW(hwnd, 0, 0, 0)
    user32.DestroyMenu(hmenu)
    return cmd


WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, ctypes.c_uint, WPARAM, LPARAM)


@WNDPROC
def wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_TRAYICON:
        if lparam == WM_LBUTTONUP:
            with istate.lock:
                if istate.active:
                    istate.active = False
                    istate.active_peer = None
                    istate.set_clip(False)
                else:
                    for hk in istate.hotkeys:
                        if hk.action.startswith("forward_"):
                            idx = int(hk.action.split("_")[1])
                            peers = istate.config.get("peers", [])
                            if 0 <= idx < len(peers):
                                istate.active = True
                                istate.active_peer = peers[idx]["name"]
                                istate.set_clip(True)
                                break
                update_tray()
        elif lparam == WM_RBUTTONUP:
            cmd = show_menu(hwnd)
            _handle_menu(cmd)
        return 0
    elif msg == WM_DESTROY:
        remove_tray()
        user32.PostQuitMessage(0)
        return 0
    elif msg == WM_COMMAND:
        _handle_menu(wparam)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _handle_menu(cmd):
    if cmd == ID_OPEN:
        open_gui()
    elif cmd == ID_TOGGLE:
        with istate.lock:
            if istate.active:
                istate.active = False
                istate.active_peer = None
                istate.set_clip(False)
            else:
                for hk in istate.hotkeys:
                    if hk.action.startswith("forward_"):
                        idx = int(hk.action.split("_")[1])
                        peers = istate.config.get("peers", [])
                        if 0 <= idx < len(peers):
                            istate.active = True
                            istate.active_peer = peers[idx]["name"]
                            istate.set_clip(True)
                            break
        update_tray()
    elif cmd == ID_STARTUP:
        new_val = not AutoStartManager.is_set()
        AutoStartManager.set(new_val)
        update_tray()
    elif cmd == ID_EXIT:
        user32.PostQuitMessage(0)


class AutoStartManager:
    KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    @classmethod
    def cmd(cls):
        script = os.path.abspath(__file__)
        return f'"{sys.executable}" "{script}"'

    @classmethod
    def is_set(cls):
        try:
            advapi = ctypes.windll.advapi32
            hkey = ctypes.c_void_p()
            ret = advapi.RegOpenKeyExW(0x80000001, cls.KEY, 0, 0x20001, ctypes.byref(hkey))
            if ret != 0:
                return False
            buf = ctypes.create_unicode_buffer(512)
            size = ctypes.c_ulong(ctypes.sizeof(buf))
            ret = advapi.RegQueryValueExW(hkey, AUTO_START_NAME, None, None, buf, ctypes.byref(size))
            advapi.RegCloseKey(hkey)
            return ret == 0
        except Exception:
            return False

    @classmethod
    def set(cls, enabled):
        try:
            advapi = ctypes.windll.advapi32
            hkey = ctypes.c_void_p()
            ret = advapi.RegOpenKeyExW(0x80000001, cls.KEY, 0, 0x20006, ctypes.byref(hkey))
            if ret != 0:
                return
            if enabled:
                cmd = cls.cmd()
                advapi.RegSetValueExW(hkey, AUTO_START_NAME, 0, 1, cmd, len(cmd) * 2)
            else:
                advapi.RegDeleteValueW(hkey, AUTO_START_NAME)
            advapi.RegCloseKey(hkey)
        except Exception:
            pass


def watchdog():
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


def run():
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = wnd_proc
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = "FlowShiftTrayClass"
    wc.hbrBackground = ctypes.c_void_p(6)
    user32.RegisterClassExW(ctypes.byref(wc))

    global _hwnd
    _hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "FlowShift", 0,
                                    0, 0, 0, 0, None, None, wc.hInstance, None)
    create_tray(_hwnd)

    threading.Thread(target=network_thread, daemon=True).start()
    threading.Thread(target=connect_to_peers, daemon=True).start()
    threading.Thread(target=forward_loop, daemon=True).start()
    threading.Thread(target=inject_loop, daemon=True).start()
    threading.Thread(target=hook_thread, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

    msg = MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

    remove_tray()


def hook_thread():
    hmod = kernel32.GetModuleHandleW(None)
    kb = user32.SetWindowsHookExW(WH_KEYBOARD_LL, keyboard_proc, hmod, 0)
    ms = user32.SetWindowsHookExW(WH_MOUSE_LL, mouse_proc, hmod, 0)
    if not kb or not ms:
        return
    msg = MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    user32.UnhookWindowsHookEx(kb)
    user32.UnhookWindowsHookEx(ms)


if __name__ == "__main__":
    istate.config = load_config()
    istate.hotkeys = load_hotkeys(istate.config)
    if "--tray" in sys.argv:
        run()
    else:
        open_gui()
        run()
