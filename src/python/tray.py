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
LOG_FILE = os.path.join(BASE, "flowshift.log")
RUNTIME_MUTEX_NAME = "FlowShift_Runtime_Mutex"

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_MEASUREITEM = 0x002C
WM_DRAWITEM = 0x002B
WM_WINDOWPOSCHANGING = 0x0046
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
MOUSEEVENTF_VIRTUALDESK = 0x4000

LLMHF_INJECTED = 0x00000010
LLMHF_LOWER_IL_INJECTED = 0x00000002
HOOK_INJECTED_FLAGS = LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED
INJECTED_EXTRA_INFO = 0x46535348

MF_STRING = 0
MF_SEPARATOR = 0x0800
MF_OWNERDRAW = 0x0100
TPM_RETURNCMD = 0x0100
TPM_LEFTALIGN = 0
TPM_BOTTOMALIGN = 0x0020

ODT_MENU = 1
ODS_SELECTED = 0x0001
ODS_GRAYED = 0x0002
ODS_DISABLED = 0x0004

COLOR_MENU = 4
COLOR_MENUTEXT = 7
COLOR_HIGHLIGHT = 13
COLOR_HIGHLIGHTTEXT = 14

DT_SINGLELINE = 0x0020
DT_VCENTER = 0x0004
DT_LEFT = 0x0000
DT_NOPREFIX = 0x0800
TRANSPARENT = 1
DEFAULT_GUI_FONT = 17

ID_OPEN = 1001
ID_TOGGLE = 1002
ID_STARTUP = 1003
ID_EXIT = 1004
ID_HK_BASE = 2000
ID_HK_KILL = 2999

MOD_CTRL = 1
MOD_SHIFT = 2
MOD_ALT = 4
MOD_WIN = 8

# RegisterHotKey uses different bit layout than tray internal mods
WM_HOTKEY = 0x0312
RHK_ALT = 0x0001
RHK_CTRL = 0x0002
RHK_SHIFT = 0x0004
RHK_WIN = 0x0008
RHK_MOD_NONE = 0x0000

def tray_mods_to_rhk(tray_mods):
    """Map tray internal mod bits to RegisterHotKey mod bits."""
    rhk = 0
    if tray_mods & MOD_CTRL: rhk |= RHK_CTRL
    if tray_mods & MOD_SHIFT: rhk |= RHK_SHIFT
    if tray_mods & MOD_ALT: rhk |= RHK_ALT
    if tray_mods & MOD_WIN: rhk |= RHK_WIN
    return rhk

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

_log_lock = threading.Lock()
_rate_limit_lock = threading.Lock()
_rate_limit_last = {}
_config_mtime = 0.0
_connector_threads = {}
_connector_lock = threading.Lock()
_shutdown_requested = False
_shutdown_event = threading.Event()


def log(level, msg):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] [{level}] {msg}"
    try:
        print(line)
    except Exception:
        pass


def log_rate_limited(key, level, msg, interval=0.5):
    now = time.monotonic()
    with _rate_limit_lock:
        last = _rate_limit_last.get(key, 0.0)
        if now - last < interval:
            return False
        _rate_limit_last[key] = now
    log(level, msg)
    return True
    try:
        with _log_lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def runtime_instance_already_running():
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.GetLastError.restype = ctypes.c_uint
    kernel32.CreateMutexW(None, 0, RUNTIME_MUTEX_NAME)
    return kernel32.GetLastError() == 183


def request_shutdown(reason):
    global _shutdown_requested
    if _shutdown_requested:
        return
    _shutdown_requested = True
    _shutdown_event.set()
    log("INFO", f"shutdown requested: {reason}")
    try:
        with istate.lock:
            istate.active = False
            istate.active_peer = None
    except Exception:
        pass
    try:
        istate.set_clip(False)
    except Exception:
        pass
    try:
        _hook_mgr.stop()
    except Exception:
        pass
    try:
        remove_tray()
    except Exception:
        pass
    try:
        user32.PostQuitMessage(0)
    except Exception:
        pass

try:
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        ULONG_PTR = ctypes.c_ulonglong
    else:
        ULONG_PTR = ctypes.c_ulong
except Exception:
    ULONG_PTR = ctypes.c_size_t

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32
gdi32 = ctypes.windll.gdi32

# Fix 64-bit truncation on GetModuleHandleW (default restype is 32-bit c_int!)
kernel32.GetModuleHandleW.restype = ctypes.c_void_p

# Set argtypes for user32 functions to avoid 64-bit pointer issues
user32.CreateWindowExW.argtypes = [
    ctypes.c_uint,    ctypes.c_wchar_p, ctypes.c_wchar_p,  ctypes.c_uint,
    ctypes.c_int,     ctypes.c_int,     ctypes.c_int,      ctypes.c_int,
    ctypes.c_void_p,  ctypes.c_void_p,  ctypes.c_void_p,   ctypes.c_void_p,
]
user32.CreateWindowExW.restype = ctypes.c_void_p
user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
user32.RegisterClassExW.restype = ctypes.c_ushort
_PTR_INT = ctypes.c_ssize_t if hasattr(ctypes, "c_ssize_t") else (ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long)

user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, _PTR_INT]
user32.DefWindowProcW.restype = _PTR_INT
user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
user32.RegisterHotKey.restype = ctypes.c_int
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
user32.GetMessageW.restype = ctypes.c_int
user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.LoadImageW.restype = ctypes.c_void_p
user32.CreatePopupMenu.restype = ctypes.c_void_p
user32.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p]
user32.AppendMenuW.restype = ctypes.c_int
user32.TrackPopupMenu.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DrawTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
user32.DrawTextW.restype = ctypes.c_int
user32.FillRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
user32.FillRect.restype = ctypes.c_int
user32.GetSysColor.argtypes = [ctypes.c_int]
user32.GetSysColor.restype = ctypes.c_uint
user32.GetSysColorBrush.argtypes = [ctypes.c_int]
user32.GetSysColorBrush.restype = ctypes.c_void_p
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
gdi32.SetBkMode.restype = ctypes.c_int
gdi32.SetTextColor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
gdi32.SetTextColor.restype = ctypes.c_uint
gdi32.GetTextExtentPoint32W.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p]
gdi32.GetTextExtentPoint32W.restype = ctypes.c_int
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.GetDC.restype = ctypes.c_void_p
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.ReleaseDC.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [ctypes.c_void_p]
user32.DestroyMenu.restype = ctypes.c_int
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.restype = ctypes.c_int
user32.GetCursorPos.argtypes = [ctypes.c_void_p]
user32.GetCursorPos.restype = ctypes.c_int
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, _PTR_INT]
user32.PostMessageW.restype = ctypes.c_int
user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.LoadCursorW.restype = ctypes.c_void_p
user32.DestroyWindow.argtypes = [ctypes.c_void_p]
user32.DestroyWindow.restype = ctypes.c_int
user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.UnregisterHotKey.restype = ctypes.c_int
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.ShowWindow.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [ctypes.c_ulong, ctypes.c_uint, ctypes.c_size_t, _PTR_INT]
user32.PostThreadMessageW.restype = ctypes.c_int

KILL_FILE = os.path.join(os.environ.get("TEMP", "."), "flowshift_kill")
_emergency_stop = False
_menu_text_by_id = {}
LOCAL_CTRL_HOST = "127.0.0.1"
LOCAL_CTRL_PORT = 45782

LRESULT = _PTR_INT
WPARAM = ctypes.c_size_t
LPARAM = _PTR_INT
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


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class MEASUREITEMSTRUCT(ctypes.Structure):
    _fields_ = [
        ("CtlType", ctypes.c_uint),
        ("CtlID", ctypes.c_uint),
        ("itemID", ctypes.c_uint),
        ("itemWidth", ctypes.c_uint),
        ("itemHeight", ctypes.c_uint),
        ("itemData", ctypes.c_size_t),
    ]


class DRAWITEMSTRUCT(ctypes.Structure):
    _fields_ = [
        ("CtlType", ctypes.c_uint),
        ("CtlID", ctypes.c_uint),
        ("itemID", ctypes.c_uint),
        ("itemAction", ctypes.c_uint),
        ("itemState", ctypes.c_uint),
        ("hwndItem", ctypes.c_void_p),
        ("hDC", ctypes.c_void_p),
        ("rcItem", RECT),
        ("itemData", ctypes.c_size_t),
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


def measure_menu_text(text):
    hdc = user32.GetDC(None)
    if not hdc:
        return 120, 24
    font = gdi32.GetStockObject(DEFAULT_GUI_FONT)
    old_font = gdi32.SelectObject(hdc, font)
    try:
        size = SIZE()
        if not gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(size)):
            return max(120, len(text) * 9 + 20), 24
        return size.cx + 28, max(24, size.cy + 8)
    finally:
        if old_font:
            gdi32.SelectObject(hdc, old_font)
        user32.ReleaseDC(None, hdc)


def draw_menu_item(dis):
    text = _menu_text_by_id.get(dis.itemID, "")
    rc = RECT(dis.rcItem.left, dis.rcItem.top, dis.rcItem.right, dis.rcItem.bottom)
    selected = bool(dis.itemState & ODS_SELECTED)
    disabled = bool(dis.itemState & (ODS_DISABLED | ODS_GRAYED))

    bg_color = COLOR_HIGHLIGHT if selected else COLOR_MENU
    fg_color = COLOR_HIGHLIGHTTEXT if selected else (COLOR_MENUTEXT if not disabled else COLOR_MENUTEXT)
    user32.FillRect(dis.hDC, ctypes.byref(rc), user32.GetSysColorBrush(bg_color))
    gdi32.SetBkMode(dis.hDC, TRANSPARENT)
    gdi32.SetTextColor(dis.hDC, user32.GetSysColor(fg_color))

    font = gdi32.GetStockObject(DEFAULT_GUI_FONT)
    old_font = gdi32.SelectObject(dis.hDC, font)
    try:
        rc.left += 14
        rc.right -= 8
        user32.DrawTextW(dis.hDC, text, -1, ctypes.byref(rc), DT_LEFT | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX)
    finally:
        if old_font:
            gdi32.SelectObject(dis.hDC, old_font)


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
            cfg = json.load(f)
    else:
        cfg = {"device_name": "Unbekannt", "device_id": "", "port": 45781, "peers": []}

    needs_save = False
    device_id = str(cfg.get("device_id", "")).strip().lower()
    if len(device_id) != 8 or any(c not in "0123456789abcdef" for c in device_id):
        cfg["device_id"] = __import__("uuid").uuid4().hex[:8]
        needs_save = True
    if not cfg.get("device_name"):
        cfg["device_name"] = os.environ.get("COMPUTERNAME", "Unbekannt")
        needs_save = True

    if needs_save or not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)

    try:
        global _config_mtime
        _config_mtime = os.path.getmtime(CONFIG_FILE)
    except OSError:
        _config_mtime = 0.0

    log("DEBUG", f"config loaded device={cfg.get('device_name', '?')} peers={len(cfg.get('peers', []))} port={cfg.get('port', 45781)}")
    return cfg


def reload_config_if_changed(force=False):
    global _config_mtime
    try:
        current = os.path.getmtime(CONFIG_FILE)
    except OSError:
        current = 0.0

    if not force and current == _config_mtime:
        return False

    cfg = load_config()
    with istate.lock:
        istate.config = cfg
        istate.hotkeys = load_hotkeys(cfg)
    log("INFO", f"config reloaded peers={len(cfg.get('peers', []))} hotkeys={len(istate.hotkeys)}")
    return True


def default_hotkeys(peers):
    hk = []
    for i, p in enumerate(peers):
        if i < 9:
            hk.append({"action": f"forward_{i}", "mods": MOD_CTRL | MOD_ALT, "key": 0x31 + i, "label": f"Forward to {p['name']}"})
    hk.append({"action": "return_local", "mods": MOD_CTRL | MOD_ALT, "key": 0x30, "label": "Return to local"})
    return hk


def _is_ipv4(ip):
    try:
        socket.inet_aton(ip)
        return ip.count(".") == 3
    except OSError:
        return False


_local_ipv4_cache = None


def get_local_ipv4s():
    global _local_ipv4_cache
    if _local_ipv4_cache is not None:
        return list(_local_ipv4_cache)

    ips = []
    seen = set()

    def add(ip):
        if not ip:
            return
        ip = str(ip).strip()
        if not _is_ipv4(ip):
            return
        if ip.startswith("127.") or ip.startswith("169.254."):
            return
        if ip in seen:
            return
        seen.add(ip)
        ips.append(ip)

    ps_cmd = (
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object { $_.IPAddress -and $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | "
        "Select-Object IPAddress | ConvertTo-Json -Compress"
    )
    for shell in ("powershell", "pwsh"):
        try:
            proc = subprocess.run(
                [shell, "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                if isinstance(data, dict):
                    data = [data]
                for item in data:
                    add(item.get("IPAddress"))
                if ips:
                    _local_ipv4_cache = tuple(ips)
                    return ips
        except FileNotFoundError:
            continue
        except Exception:
            pass

    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM):
            add(info[4][0])
    except Exception:
        pass

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        add(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    if not ips:
        ips = ["127.0.0.1"]

    _local_ipv4_cache = tuple(ips)
    return list(_local_ipv4_cache)


def is_local_host(host):
    return host in set(get_local_ipv4s())


def get_virtual_screen_spec():
    left = int(user32.GetSystemMetrics(76))
    top = int(user32.GetSystemMetrics(77))
    width = int(user32.GetSystemMetrics(78))
    height = int(user32.GetSystemMetrics(79))
    return {"left": left, "top": top, "width": max(1, width), "height": max(1, height)}


def format_screen_spec(spec):
    if not isinstance(spec, dict):
        return "-"
    return f"{spec.get('width', '?')}x{spec.get('height', '?')}@{spec.get('left', '?')},{spec.get('top', '?')}"


def _scale_mouse_point(x, y, source_spec, target_spec):
    if not isinstance(source_spec, dict) or not isinstance(target_spec, dict):
        return x, y

    src_left = int(source_spec.get("left", 0))
    src_top = int(source_spec.get("top", 0))
    src_width = max(1, int(source_spec.get("width", 1)))
    src_height = max(1, int(source_spec.get("height", 1)))

    tgt_left = int(target_spec.get("left", 0))
    tgt_top = int(target_spec.get("top", 0))
    tgt_width = max(1, int(target_spec.get("width", 1)))
    tgt_height = max(1, int(target_spec.get("height", 1)))

    rel_x = (x - src_left) / max(1, src_width - 1)
    rel_y = (y - src_top) / max(1, src_height - 1)
    return (
        tgt_left + rel_x * max(1, tgt_width - 1),
        tgt_top + rel_y * max(1, tgt_height - 1),
    )


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
        self.lock = threading.RLock()
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
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
user32.SetWindowsHookExW.restype = HHOOK
user32.UnhookWindowsHookEx.argtypes = [HHOOK]
user32.UnhookWindowsHookEx.restype = ctypes.c_int
user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, WPARAM, LPARAM]
user32.CallNextHookEx.restype = LRESULT

KILL_MODS = MOD_CTRL | MOD_ALT | MOD_SHIFT | MOD_WIN
KILL_VK = 0x7B  # F12

def is_kill_combo(mods, vk):
    return mods == KILL_MODS and vk == KILL_VK  # Ctrl+Alt+Shift+Win+F12

@HOOKPROC
def keyboard_proc(code, wparam, lparam):
    global _emergency_stop
    try:
        if code >= 0:
            kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = kb.vkCode
            down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            if (kb.flags & HOOK_INJECTED_FLAGS) or int(kb.dwExtraInfo) == INJECTED_EXTRA_INFO:
                log("DEBUG", f"keyboard ignored injected vk={vk} flags=0x{int(kb.flags):X} extra=0x{int(kb.dwExtraInfo):X}")
                return user32.CallNextHookEx(None, code, wparam, lparam)
            if down and is_kill_combo(istate.current_mods(), vk):
                log("WARN", f"kill switch pressed mods=0x{istate.current_mods():X} vk={vk}")
                _emergency_stop = True
                istate.active = False
                istate.active_peer = None
                try:
                    with open(KILL_FILE, "w") as _f:
                        _f.write("1")
                except Exception:
                    pass
                log("INFO", "kill switch armed, forwarding stopped, quitting message loop")
                update_tray()
                user32.PostQuitMessage(0)
                return 1
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
                            _hook_mgr.stop()
                            update_tray()
                            return 1
                        elif hk.action.startswith("forward_") and not istate.active:
                            idx = int(hk.action.split("_")[1])
                            peers = istate.config.get("peers", [])
                            if 0 <= idx < len(peers):
                                peer_cfg = peers[idx]
                                if is_local_host(peer_cfg.get("host", "")):
                                    return 1
                                name = peer_cfg["name"]
                                istate.active = True
                                istate.active_peer = name
                                istate.set_clip(True)
                                _hook_mgr.start()
                                update_tray()
                                return 1

                if istate.active:
                    if down:
                        pass_through = istate.find_hotkey(istate.current_mods(), vk)
                    else:
                        pass_through = None
                    if not pass_through:
                        ev = {"type": "key" if down else "key_up", "code": vk}
                        log("DEBUG", f"keyboard queued {ev['type']} vk={vk} active_peer={istate.active_peer}")
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
            if (ms.flags & HOOK_INJECTED_FLAGS) or int(ms.dwExtraInfo) == INJECTED_EXTRA_INFO:
                log("DEBUG", f"mouse ignored injected msg={wparam} flags=0x{int(ms.flags):X} extra=0x{int(ms.dwExtraInfo):X}")
                return user32.CallNextHookEx(None, code, wparam, lparam)
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
                if ev["type"] == "mousemove":
                    log_rate_limited(
                        "mouse-queued-move",
                        "DEBUG",
                        f"mouse queued {ev['type']} active_peer={istate.active_peer}",
                        interval=0.25,
                    )
                else:
                    log("DEBUG", f"mouse queued {ev['type']} active_peer={istate.active_peer}")
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
            ki.dwExtraInfo = INJECTED_EXTRA_INFO
            inp.u.ki = ki
            log("DEBUG", f"inject key {t} vk={ev['code']}")
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        elif t == "mousemove":
            inp.type = INPUT_MOUSE
            mi = MOUSEINPUT()
            target_screen = ev.get("target_screen") or get_virtual_screen_spec()
            source_screen = ev.get("source_screen")
            x, y = _scale_mouse_point(ev["x"], ev["y"], source_screen, target_screen)
            sx = max(1, int(target_screen.get("width", 1)) - 1)
            sy = max(1, int(target_screen.get("height", 1)) - 1)
            mi.dx = int(max(0, min(65535, round((x - int(target_screen.get("left", 0))) * 65535 / sx))))
            mi.dy = int(max(0, min(65535, round((y - int(target_screen.get("top", 0))) * 65535 / sy))))
            mi.dwFlags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE | MOUSEEVENTF_VIRTUALDESK
            mi.dwExtraInfo = INJECTED_EXTRA_INFO
            inp.u.mi = mi
            log_rate_limited(
                "inject-mousemove",
                "DEBUG",
                f"inject mousemove x={ev['x']} y={ev['y']} scaled={round(x)},{round(y)} norm={mi.dx},{mi.dy}",
                interval=0.25,
            )
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        elif t == "mousedown":
            inp.type = INPUT_MOUSE
            mi = MOUSEINPUT()
            flags = [MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_MIDDLEDOWN]
            mi.dwFlags = flags[ev["button"]]
            mi.dwExtraInfo = INJECTED_EXTRA_INFO
            inp.u.mi = mi
            log("DEBUG", f"inject mousedown button={ev['button']}")
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        elif t == "mouseup":
            inp.type = INPUT_MOUSE
            mi = MOUSEINPUT()
            flags = [MOUSEEVENTF_LEFTUP, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_MIDDLEUP]
            mi.dwFlags = flags[ev["button"]]
            mi.dwExtraInfo = INJECTED_EXTRA_INFO
            inp.u.mi = mi
            log("DEBUG", f"inject mouseup button={ev['button']}")
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        elif t == "wheel":
            inp.type = INPUT_MOUSE
            mi = MOUSEINPUT()
            mi.mouseData = ev["delta"] & 0xFFFFFFFF
            mi.dwFlags = MOUSEEVENTF_WHEEL
            mi.dwExtraInfo = INJECTED_EXTRA_INFO
            inp.u.mi = mi
            log("DEBUG", f"inject wheel delta={ev['delta']}")
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    except Exception:
        log("ERROR", f"inject failed for event {ev.get('type', '?')}")
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
    remote_device_id = ""
    remote_screen = None
    try:
        conn.settimeout(0.25)
        try:
            first = recv_msg(conn)
        except socket.timeout:
            first = None

        local_screen = get_virtual_screen_spec()

        if first and first.get("type") == "ping":
            sender_name = first.get("display_name", str(addr))
            sender_device_id = first.get("device_id", "") or ""
            sender_screen = first.get("screen")
            log("INFO", f"ping received from {sender_name} {addr[0]}:{addr[1]} device_id={sender_device_id or '-'} screen={format_screen_spec(sender_screen)}")
            pong = {
                "type": "pong",
                "device_id": istate.config.get("device_id", ""),
                "display_name": istate.config.get("device_name", ""),
                "os": "windows",
                "screen": local_screen,
            }
            send_msg(conn, pong)
            log("INFO", f"pong sent to {sender_name} {addr[0]}:{addr[1]} device_id={pong['device_id'] or '-'} screen={format_screen_spec(local_screen)}")
            conn.close()
            return
        send_msg(conn, {"type": "hello", "device_id": istate.config.get("device_id", ""),
                        "display_name": istate.config.get("device_name", ""), "os": "windows",
                        "screen": local_screen})
        log("DEBUG", f"hello sent to {addr[0]}:{addr[1]} server={is_server}")
        if first is None:
            conn.settimeout(5.0)
            first = recv_msg(conn)
        if first and first.get("type") == "hello":
            name = first.get("display_name", str(addr))
            remote_device_id = first.get("device_id", "") or ""
            remote_screen = first.get("screen")
            log("INFO", f"peer hello from {name} {addr[0]}:{addr[1]} device_id={remote_device_id or '-'}")
        conn.settimeout(None)
        with istate.lock:
            peer_entry = istate.peers.setdefault(name, {"inbound": None, "outbound": None})
            peer_entry["inbound" if is_server else "outbound"] = {
                "conn": conn,
                "host": addr[0],
                "port": addr[1],
                "device_id": remote_device_id,
                "display_name": name,
                "screen": remote_screen,
                "direction": "inbound" if is_server else "outbound",
            }
            log("INFO", f"peer linked {name} {addr[0]}:{addr[1]} screen={format_screen_spec(remote_screen)}")
        while True:
            msg = recv_msg(conn)
            if msg.get("type") == "input":
                log("DEBUG", f"input batch from {name}: {len(msg.get('events', []))} events")
                target_screen = get_virtual_screen_spec()
                for ev in msg.get("events", []):
                    payload = dict(ev)
                    if remote_screen and not payload.get("source_screen"):
                        payload["source_screen"] = remote_screen
                    payload["target_screen"] = target_screen
                    istate.inject_queue.put(payload)
    except Exception:
        log("DEBUG", f"peer handler ended for {name} {addr[0]}:{addr[1]}")
        pass
    finally:
        conn.close()
        log("INFO", f"peer disconnected {name} {addr[0]}:{addr[1]}")
        with istate.lock:
            for n, peer_info in list(istate.peers.items()):
                if isinstance(peer_info, dict):
                    removed = False
                    for dir_name in ("inbound", "outbound"):
                        slot = peer_info.get(dir_name)
                        if isinstance(slot, dict) and slot.get("conn") is conn:
                            peer_info[dir_name] = None
                            removed = True
                    if removed and not peer_info.get("inbound") and not peer_info.get("outbound"):
                        del istate.peers[n]
                elif peer_info is conn:
                    del istate.peers[n]


class HookManager:
    """Manages hook thread lifecycle. Hooks only run while active=True."""

    def __init__(self):
        self._thread = None
        self._tid = None
        self._ready = threading.Event()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._tid = None
        self._ready.clear()
        log("DEBUG", "hook thread starting")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            log("WARN", "hook thread did not report ready in time")

    def stop(self):
        tid = self._tid
        self._tid = None
        self._thread = None
        log("DEBUG", "hook thread stopping")
        if tid is not None:
            user32.PostThreadMessageW(tid, 0x0012, 0, 0)  # WM_QUIT

    def _run(self):
        try:
            msg = MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)  # ensure queue
            self._tid = kernel32.GetCurrentThreadId()
            self._ready.set()
            hmod = kernel32.GetModuleHandleW(None)
            kb = user32.SetWindowsHookExW(WH_KEYBOARD_LL, keyboard_proc, hmod, 0)
            ms = user32.SetWindowsHookExW(WH_MOUSE_LL, mouse_proc, hmod, 0)
            if not kb or not ms:
                log("ERROR", f"hook installation failed kb={bool(kb)} ms={bool(ms)} err={kernel32.GetLastError()}")
                return
            log("INFO", "hook installation succeeded")
            msg = MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            log("DEBUG", "hook message loop exited")
            user32.UnhookWindowsHookEx(kb)
            user32.UnhookWindowsHookEx(ms)
        except Exception as e:
            log("ERROR", f"hook thread crashed: {e!r}")


_hook_mgr = HookManager()


def network_thread():
    port = istate.config.get("port", 45781)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("0.0.0.0", port))
        srv.listen(5)
        srv.settimeout(1.0)
        log("INFO", f"tcp listener started on 0.0.0.0:{port}")
        while not _shutdown_event.is_set():
            try:
                c, a = srv.accept()
                log("DEBUG", f"tcp accept from {a[0]}:{a[1]}")
                threading.Thread(target=peer_handler, args=(c, a, True), daemon=True).start()
            except socket.timeout:
                continue
    except OSError:
        log("ERROR", f"tcp listener failed on port {port}")
    finally:
        srv.close()



def discovery_thread():
    port = istate.config.get("port", 45781)
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("", port))
        srv.settimeout(1.0)
        log("INFO", f"discovery listener started on udp :{port}")
        while not _shutdown_event.is_set():
            try:
                data, addr = srv.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                req = json.loads(data.decode("utf-8"))
            except Exception:
                continue

            if req.get("type") != "discover":
                continue

            log("DEBUG", f"discovery probe from {addr[0]}:{addr[1]}")

            reply = {
                "type": "discover_reply",
                "device_id": istate.config.get("device_id", ""),
                "display_name": istate.config.get("device_name", ""),
                "port": port,
                "screen": get_virtual_screen_spec(),
            }
            try:
                srv.sendto(json.dumps(reply).encode("utf-8"), addr)
                log("DEBUG", f"discovery reply sent to {addr[0]}:{addr[1]}")
            except Exception:
                pass
    except OSError:
        log("ERROR", f"discovery listener failed on port {port}")
    finally:
        srv.close()


def peer_token(peer):
    device_id = str(peer.get("device_id", "")).strip()
    if device_id:
        return ("device_id", device_id)
    return ("endpoint", peer.get("name"), peer.get("host"), int(peer.get("port", 45781)))


def peer_token_active(peer):
    if not isinstance(peer, dict):
        return False
    return not is_local_host(peer.get("host", ""))


def find_config_peer(peer_ref):
    if not peer_ref:
        return None
    with istate.lock:
        for p in istate.config.get("peers", []):
            if p.get("name") == peer_ref or p.get("host") == peer_ref or p.get("device_id") == peer_ref:
                return dict(p)
    return None


def config_has_peer_token(token):
    with istate.lock:
        for p in istate.config.get("peers", []):
            if peer_token(p) == token:
                return True
    return False


def connect_one(peer, token):
    name = peer.get("name", peer.get("host", "peer"))
    host = peer.get("host")
    port = int(peer.get("port", 45781))
    log("INFO", f"connector thread started for {name} -> {host}:{port}")
    while not _shutdown_event.is_set():
        s = None
        if not config_has_peer_token(token):
            log("INFO", f"connector exiting for {name} because peer was removed")
            return
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect((host, port))
            log("DEBUG", f"outbound connect ok to {name} {host}:{port}")
            peer_handler(s, (host, port), False)
        except Exception as e:
            log("DEBUG", f"outbound connect failed to {name} {host}:{port}: {e}")
        finally:
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass
        if _shutdown_event.wait(5):
            break


def connect_to_peers():
    log("INFO", "peer connector manager started")
    while not _shutdown_event.is_set():
        reload_config_if_changed()
        peers = list(istate.config.get("peers", []))
        with _connector_lock:
            desired_tokens = set()
            for peer in peers:
                if not peer_token_active(peer):
                    continue
                token = peer_token(peer)
                desired_tokens.add(token)
                thread = _connector_threads.get(token)
                if thread is None or not thread.is_alive():
                    thread = threading.Thread(target=connect_one, args=(dict(peer), token), daemon=True)
                    _connector_threads[token] = thread
                    log("INFO", f"starting connector thread for {peer.get('name', peer.get('host', '?'))}")
                    thread.start()
            for token in list(_connector_threads.keys()):
                if token not in desired_tokens:
                    del _connector_threads[token]
        if _shutdown_event.wait(2):
            break


def ping_peer(peer_ref):
    reload_config_if_changed()
    peer = find_config_peer(peer_ref)
    if not peer:
        raise ValueError(f"unknown peer: {peer_ref}")

    host = peer.get("host")
    port = int(peer.get("port", 45781))
    local_name = (istate.config.get("device_name", "") or os.environ.get("COMPUTERNAME", "")).strip() or "Unbekannt"
    local_device_id = istate.config.get("device_id", "")
    local_screen = get_virtual_screen_spec()

    log("INFO", f"ping start {local_name} -> {peer.get('name', host)} {host}:{port}")
    sock = None
    started = time.monotonic()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(4.0)
        sock.connect((host, port))
        log("DEBUG", f"ping tcp connect ok -> {peer.get('name', host)} {host}:{port}")
        send_msg(sock, {
            "type": "ping",
            "device_id": local_device_id,
            "display_name": local_name,
            "os": "windows",
            "screen": local_screen,
        })
        log("DEBUG", f"ping sent -> {peer.get('name', host)} {host}:{port} screen={format_screen_spec(local_screen)}")
        reply = recv_msg(sock)
        if reply.get("type") != "pong":
            log("WARN", f"ping unexpected reply from {peer.get('name', host)} {host}:{port}: {reply.get('type', '?')}")
            raise ValueError(f"unexpected reply: {reply.get('type', '?')}")

        rtt_ms = round((time.monotonic() - started) * 1000)
        remote_name = reply.get("display_name", peer.get("name", host))
        remote_id = reply.get("device_id", "") or ""
        log("INFO", f"pong received from {remote_name} {host}:{port} device_id={remote_id or '-'} rtt_ms={rtt_ms}")
        return {
            "peer": peer.get("name", host),
            "host": host,
            "port": port,
            "rtt_ms": rtt_ms,
            "reply": reply,
        }
    except Exception as e:
        log("ERROR", f"ping failed -> {peer.get('name', host)} {host}:{port}: {e}")
        raise
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass


def forward_loop():
    while not _shutdown_event.is_set():
        try:
            ev = istate.event_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        with istate.lock:
            peer = istate.active_peer
            conn_data = resolve_peer_connection(peer) if peer else None
            conn_data = conn_data[1] if conn_data else None
        if conn_data and isinstance(conn_data, dict):
            send_data = conn_data.get("outbound") or conn_data.get("inbound")
        else:
            send_data = None
        if send_data:
            try:
                log("DEBUG", f"forward {ev.get('type', '?')} -> {peer}")
                payload = dict(ev)
                payload["source_screen"] = get_virtual_screen_spec()
                send_msg(send_data["conn"], {"type": "input", "events": [payload]})
                log("DEBUG", f"forward sent {ev.get('type', '?')} -> {peer}")
            except Exception:
                log("ERROR", f"forward send failed for {ev.get('type', '?')} -> {peer}")
                pass
        else:
            log("DEBUG", f"forward dropped {ev.get('type', '?')} no connection peer={peer}")


def _menu_summary():
    return ["keyboard", "mouse move", "mouse buttons", "mouse wheel"]


def resolve_peer_connection(peer_ref):
    if not peer_ref:
        return None, None

    peer_info = istate.peers.get(peer_ref)
    if isinstance(peer_info, dict):
        return peer_ref, peer_info

    cfg_peer = next(
        (
            p
            for p in istate.config.get("peers", [])
            if p.get("name") == peer_ref or p.get("host") == peer_ref or p.get("device_id") == peer_ref
        ),
        None,
    )
    if not cfg_peer:
        return None, None

    host = cfg_peer.get("host")
    port = cfg_peer.get("port", 45781)
    device_id = cfg_peer.get("device_id")

    if is_local_host(host):
        return None, None

    for actual_name, actual_info in istate.peers.items():
        if not isinstance(actual_info, dict):
            continue
        if device_id:
            for dir_name in ("inbound", "outbound"):
                slot = actual_info.get(dir_name)
                if isinstance(slot, dict) and slot.get("device_id") == device_id:
                    return actual_name, actual_info
        for dir_name in ("inbound", "outbound"):
            slot = actual_info.get(dir_name)
            if isinstance(slot, dict) and slot.get("host") == host and slot.get("port", 45781) == port:
                return actual_name, actual_info

    return None, None


def _slot_display_name(slot, fallback="-"):
    if not isinstance(slot, dict):
        return fallback
    return (slot.get("display_name") or slot.get("host") or fallback).strip() or fallback


def build_connection_summary(preferred_peer=None):
    local_name = (istate.config.get("device_name", "") or os.environ.get("COMPUTERNAME", "")).strip() or "Unbekannt"

    peer_name = preferred_peer or istate.active_peer
    peer_info = None
    if peer_name:
        _, peer_info = resolve_peer_connection(peer_name)

    if peer_info is None:
        for actual_name, actual_info in istate.peers.items():
            if isinstance(actual_info, dict):
                peer_name = actual_name
                peer_info = actual_info
                break

    if isinstance(peer_info, dict):
        inbound = peer_info.get("inbound")
        outbound = peer_info.get("outbound")
        if istate.active and outbound:
            remote = _slot_display_name(outbound)
            return {
                "label": f"{local_name} -> {remote}",
                "role": "Quelle",
                "peer": remote,
                "connected": True,
            }
        if inbound:
            remote = _slot_display_name(inbound)
            return {
                "label": f"{remote} -> {local_name}",
                "role": "Ziel",
                "peer": remote,
                "connected": True,
            }
        if outbound:
            remote = _slot_display_name(outbound)
            return {
                "label": f"{local_name} -> {remote}",
                "role": "Quelle",
                "peer": remote,
                "connected": True,
            }

    if istate.active and peer_name:
        return {
            "label": f"{local_name} -> {peer_name}",
            "role": "Quelle",
            "peer": peer_name,
            "connected": False,
        }

    return {
        "label": "-",
        "role": "-",
        "peer": "-",
        "connected": False,
    }


def build_status_snapshot():
    with istate.lock:
        summary = build_connection_summary()
        peers_cfg = list(istate.config.get("peers", []))
        peer_rows = []
        for p in peers_cfg:
            _, conn = resolve_peer_connection(p["name"])
            inbound = conn.get("inbound") if isinstance(conn, dict) else None
            outbound = conn.get("outbound") if isinstance(conn, dict) else None
            row_summary = build_connection_summary(p["name"])
            peer_rows.append({
                "name": p["name"],
                "host": p["host"],
                "port": p.get("port", 45781),
                "selected": p["name"] == istate.active_peer,
                "connected": bool(inbound or outbound),
                "connected_in": bool(inbound),
                "connected_out": bool(outbound),
                "direction": row_summary["role"],
                "link_label": row_summary["label"],
                "peer_label": row_summary["peer"],
                "remote": [
                    (outbound or inbound)["host"],
                    (outbound or inbound)["port"],
                ] if (inbound or outbound) else None,
            })
        capture = istate.config.get("capture_region")
        if capture:
            capture = {
                "x": capture["x"],
                "y": capture["y"],
                "width": capture["width"],
                "height": capture["height"],
            }
        return {
            "device_name": istate.config.get("device_name", ""),
            "running": not _shutdown_event.is_set(),
            "shutting_down": _shutdown_requested,
            "enabled": istate.enabled,
            "active": istate.active,
            "active_peer": istate.active_peer,
            "hook_running": _hook_mgr.running,
            "mode": "forwarding" if istate.active else ("paused" if not istate.enabled else "standby"),
            "connection_label": summary["label"],
            "connection_role": summary["role"],
            "connection_peer": summary["peer"],
            "connection_active": summary["connected"],
            "capture_region": capture,
            "forwarding": _menu_summary(),
            "peers": peer_rows,
            "hotkeys": [
                {"label": hk.label, "display": hk.display(), "action": hk.action}
                for hk in istate.hotkeys
            ],
        }


def apply_profile(name, activate=True):
    with istate.lock:
        if not activate:
            log("INFO", "forwarding deactivated")
            istate.active = False
            istate.active_peer = None
            istate.set_clip(False)
            _hook_mgr.stop()
            update_tray()
            return True, None
        peers = istate.config.get("peers", [])
        match = next((p for p in peers if p.get("name") == name or p.get("host") == name or p.get("device_id") == name), None)
        if not match:
            log("WARN", f"unknown profile requested: {name}")
            return False, f"Unknown profile: {name}"
        if is_local_host(match.get("host", "")):
            log("WARN", f"refusing to activate local profile: {match.get('name', name)}")
            return False, "This profile points to the local device"
        if activate:
            log("INFO", f"forwarding activated -> {match.get('name', name)}")
            istate.active = True
            istate.active_peer = match.get("name", name)
            istate.set_clip(True)
            _hook_mgr.start()
        else:
            istate.active = False
            istate.active_peer = None
            istate.set_clip(False)
            _hook_mgr.stop()
        update_tray()
        return True, None


def local_control_thread():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((LOCAL_CTRL_HOST, LOCAL_CTRL_PORT))
        srv.listen(5)
        srv.settimeout(1.0)
        log("INFO", f"control socket started on {LOCAL_CTRL_HOST}:{LOCAL_CTRL_PORT}")
        while not _shutdown_event.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=local_control_handler, args=(conn,), daemon=True).start()
    except Exception:
        log("ERROR", f"control socket failed on {LOCAL_CTRL_HOST}:{LOCAL_CTRL_PORT}")
    finally:
        srv.close()


def local_control_handler(conn):
    try:
        req = recv_msg(conn)
        typ = req.get("type")
        if typ == "status":
            log_rate_limited("local-status", "DEBUG", "local control request: status", interval=2.0)
        else:
            log("DEBUG", f"local control request: {typ}")
        if typ == "status":
            reload_config_if_changed()
            send_msg(conn, {"type": "status", "status": build_status_snapshot()})
        elif typ == "activate":
            reload_config_if_changed()
            ok, err = apply_profile(req.get("profile", ""), True)
            if ok:
                send_msg(conn, {"type": "ok", "status": build_status_snapshot()})
            else:
                send_msg(conn, {"type": "error", "error": err})
        elif typ == "deactivate":
            reload_config_if_changed()
            apply_profile("", False)
            send_msg(conn, {"type": "ok", "status": build_status_snapshot()})
        elif typ == "ping_peer":
            reload_config_if_changed()
            peer_ref = req.get("profile", "")
            log("INFO", f"local control request: ping_peer {peer_ref}")
            result = ping_peer(peer_ref)
            send_msg(conn, {"type": "ok", "ping": result})
        elif typ == "shutdown":
            log("INFO", "local control request: shutdown")
            send_msg(conn, {"type": "ok"})
            threading.Thread(target=request_shutdown, args=("local-control",), daemon=True).start()
        elif typ == "toggle":
            reload_config_if_changed()
            with istate.lock:
                active = istate.active
            if active:
                apply_profile("", False)
            else:
                ok, err = apply_profile(req.get("profile", ""), True)
                if not ok:
                    send_msg(conn, {"type": "error", "error": err})
                    return
            send_msg(conn, {"type": "ok", "status": build_status_snapshot()})
        else:
            log("WARN", f"unknown local control command: {typ}")
            send_msg(conn, {"type": "error", "error": f"unknown command: {typ}"})
    except Exception as e:
        log("ERROR", f"local control error: {e}")
        try:
            send_msg(conn, {"type": "error", "error": str(e)})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def inject_loop():
    while not _shutdown_event.is_set():
        try:
            ev = istate.inject_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        inject(ev)


_hwnd = None
_tray_nid = None
_orig_wndproc = None


def create_tray(hwnd):
    global _tray_nid
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hwnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAYICON
    ico_path = os.path.join(BASE, "flowshift.ico")
    # Load small icon (16x16) for tray with LR_LOADFROMFILE
    hIcon = user32.LoadImageW(None, ico_path, 1, 16, 16, 0x00000010)
    if not hIcon:
        hIcon = user32.LoadImageW(None, ico_path, 1, 32, 32, 0x00000010)
    if not hIcon:
        hIcon = user32.LoadIconW(None, ctypes.c_void_p(0x7F00))
    nid.hIcon = hIcon
    nid.szTip = "FlowShift"
    shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
    _tray_nid = nid
    update_tray()


def update_tray():
    global _tray_nid
    if _tray_nid is None:
        return
    with istate.lock:
        s = " Active" if istate.active else (" Paused" if not istate.enabled else " Standby")
        summary = build_connection_summary()
    suffix = f" | {summary['label']}" if summary["label"] != "-" else ""
    _tray_nid.szTip = f"FlowShift{s}{suffix}"
    shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(_tray_nid))


def remove_tray():
    global _tray_nid
    if _tray_nid is not None:
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(_tray_nid))
        _tray_nid = None


MF_SEPARATOR = 0x0800
MF_STRING = 0x0000
MF_ENABLED = 0x0000
TPM_RETURNCMD = 0x0100
TPM_LEFTALIGN = 0x0000
TPM_BOTTOMALIGN = 0x0020


class MENUITEMINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("fMask", ctypes.c_uint),
        ("fType", ctypes.c_uint),
        ("fState", ctypes.c_uint),
        ("wID", ctypes.c_uint),
        ("hSubMenu", ctypes.c_void_p),
        ("hbmpChecked", ctypes.c_void_p),
        ("hbmpUnchecked", ctypes.c_void_p),
        ("dwItemData", ctypes.c_size_t),
        ("dwTypeData", ctypes.c_wchar_p),
        ("cch", ctypes.c_uint),
        ("hbmpItem", ctypes.c_void_p),
    ]

MIIM_STRING = 0x00000040
MIIM_ID = 0x00000002
MFT_STRING = 0
MFT_SEPARATOR = 0x0800


def show_menu(hwnd):
    global _menu_text_by_id
    hmenu = user32.CreatePopupMenu()
    if not hmenu:
        return 0
    autostart_enabled = AutoStartManager.is_set()
    items = [
        (ID_TOGGLE, "Stop forwarding" if istate.active else "Start forwarding"),
        (0, None),
        (ID_OPEN, "Settings"),
        (ID_STARTUP, f"{'v' if autostart_enabled else ' '} Auto-start with Windows"),
        (0, None),
        (ID_EXIT, "Exit"),
    ]
    _menu_text_by_id = {uid: text for uid, text in items if text is not None}
    user32.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p]
    user32.AppendMenuW.restype = ctypes.c_int
    for uid, text in items:
        if text is None:
            user32.AppendMenuW(hmenu, MFT_SEPARATOR, 0, None)
        else:
            user32.AppendMenuW(hmenu, MF_OWNERDRAW, uid, None)

    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.SetForegroundWindow(hwnd)
    user32.PostMessageW(hwnd, 0, 0, 0)
    cmd = user32.TrackPopupMenu(hmenu, TPM_RETURNCMD | TPM_LEFTALIGN | TPM_BOTTOMALIGN,
                                 pt.x, pt.y, 0, hwnd, None)
    user32.DestroyMenu(hmenu)
    return cmd


WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, ctypes.c_uint, WPARAM, LPARAM)


@WNDPROC
def wnd_proc(hwnd, msg, wparam, lparam):
    global _orig_wndproc
    if msg == WM_TRAYICON:
        if lparam == WM_LBUTTONUP:
            with istate.lock:
                if istate.active:
                    istate.active = False
                    istate.active_peer = None
                    istate.set_clip(False)
                    _hook_mgr.stop()
                else:
                    for hk in istate.hotkeys:
                        if hk.action.startswith("forward_"):
                            idx = int(hk.action.split("_")[1])
                            peers = istate.config.get("peers", [])
                            if 0 <= idx < len(peers):
                                istate.active = True
                                istate.active_peer = peers[idx]["name"]
                                istate.set_clip(True)
                                _hook_mgr.start()
                                break
                update_tray()
        elif lparam == WM_RBUTTONUP:
            cmd = show_menu(hwnd)
            _handle_menu(cmd)
        return 0
    elif msg == WM_MEASUREITEM:
        try:
            mis = ctypes.cast(lparam, ctypes.POINTER(MEASUREITEMSTRUCT)).contents
            if mis.CtlType == ODT_MENU:
                text = _menu_text_by_id.get(mis.itemID, "")
                w, h = measure_menu_text(text)
                mis.itemWidth = w
                mis.itemHeight = h
                return 1
        except Exception:
            pass
        return 0
    elif msg == WM_DRAWITEM:
        try:
            dis = ctypes.cast(lparam, ctypes.POINTER(DRAWITEMSTRUCT)).contents
            if dis.CtlType == ODT_MENU:
                draw_menu_item(dis)
                return 1
        except Exception:
            pass
        return 0
    elif msg == WM_HOTKEY:
        hk_id = wparam
        if hk_id == ID_HK_KILL:
            log("WARN", "WM_HOTKEY kill switch received")
            _emergency_stop = True
            istate.active = False
            istate.active_peer = None
            try:
                with open(KILL_FILE, "w") as _f:
                    _f.write("1")
            except Exception:
                pass
            request_shutdown("kill-hotkey")
            return 0
        with istate.lock:
            if not istate.enabled:
                return 0
            if hk_id >= ID_HK_BASE:
                idx = hk_id - ID_HK_BASE
                hotkeys = istate.hotkeys
                if 0 <= idx < len(hotkeys):
                    hk = hotkeys[idx]
                    if hk.action == "return_local" and istate.active:
                        istate.active = False
                        istate.active_peer = None
                        istate.set_clip(False)
                        _hook_mgr.stop()
                        update_tray()
                    elif hk.action.startswith("forward_") and not istate.active:
                        peer_idx = int(hk.action.split("_")[1])
                        peers = istate.config.get("peers", [])
                        if 0 <= peer_idx < len(peers):
                            istate.active = True
                            istate.active_peer = peers[peer_idx]["name"]
                            istate.set_clip(True)
                            _hook_mgr.start()
                            update_tray()
        return 0
    elif msg == WM_DESTROY:
        request_shutdown("destroy")
        return 0
    elif msg == WM_COMMAND:
        _handle_menu(wparam)
        return 0
    elif msg == WM_WINDOWPOSCHANGING:
        return 0
    # Forward all other messages to default dialog proc
    try:
        if _orig_wndproc:
            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, HWND, ctypes.c_uint, WPARAM, LPARAM]
            user32.CallWindowProcW.restype = LRESULT
            return user32.CallWindowProcW(_orig_wndproc, hwnd, msg, wparam, lparam)
    except Exception:
        pass
    return 0


def _handle_menu(cmd):
    if cmd == ID_OPEN:
        open_gui()
    elif cmd == ID_TOGGLE:
        with istate.lock:
            if istate.active:
                istate.active = False
                istate.active_peer = None
                istate.set_clip(False)
                _hook_mgr.stop()
            else:
                for hk in istate.hotkeys:
                    if hk.action.startswith("forward_"):
                        idx = int(hk.action.split("_")[1])
                        peers = istate.config.get("peers", [])
                        if 0 <= idx < len(peers):
                            istate.active = True
                            istate.active_peer = peers[idx]["name"]
                            istate.set_clip(True)
                            _hook_mgr.start()
                            break
        update_tray()
    elif cmd == ID_STARTUP:
        new_val = not AutoStartManager.is_set()
        AutoStartManager.set(new_val)
        update_tray()
    elif cmd == ID_EXIT:
        request_shutdown("tray-exit")


class AutoStartManager:
    KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    @classmethod
    def pythonw(cls):
        exe = sys.executable
        if exe.lower().endswith('python.exe'):
            exe = exe[:-4] + 'w.exe'
        return exe if os.path.exists(exe) else sys.executable

    @classmethod
    def cmd(cls):
        script = os.path.abspath(__file__)
        return f'"{cls.pythonw()}" "{script}" --tray'

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
    while not _shutdown_event.is_set():
        if os.path.exists(KILL_FILE):
            try:
                os.remove(KILL_FILE)
            except Exception:
                pass
            _emergency_stop = True
            user32.PostQuitMessage(0)
            break
        if _shutdown_event.wait(1.0):
            break


def run():
    hInst = kernel32.GetModuleHandleW(None)

    # Set DPI awareness to avoid scaling issues with menus
    try:
        shcore = ctypes.windll.shcore
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_int
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    # Singleton: named mutex so only one FlowShift runtime instance runs
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.GetLastError.restype = ctypes.c_uint
    kernel32.CreateMutexW(None, 0, RUNTIME_MUTEX_NAME)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        log("WARN", "another FlowShift runtime instance is already running")
        return  # another instance is already running

    global _shutdown_requested
    _shutdown_requested = False
    _shutdown_event.clear()

    global _hwnd, _orig_wndproc
    # Use #32770 dialog class + WS_POPUP (top-level, can SetForegroundWindow)
    _hwnd = user32.CreateWindowExW(0, "#32770", "FlowShift", 0x80000000,
                                    -32000, -32000, 1, 1, None, None, hInst, None)
    if not _hwnd:
        raise RuntimeError("Failed to create hidden window")
    # Apply dark mode for proper menu rendering on dark theme
    try:
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_int
        val = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(_hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass
    _orig_wndproc = user32.SetWindowLongPtrW(_hwnd, -4, ctypes.cast(wnd_proc, ctypes.c_void_p))
    create_tray(_hwnd)

    threading.Thread(target=discovery_thread, daemon=True).start()
    threading.Thread(target=network_thread, daemon=True).start()
    threading.Thread(target=connect_to_peers, daemon=True).start()
    threading.Thread(target=local_control_thread, daemon=True).start()
    threading.Thread(target=forward_loop, daemon=True).start()
    threading.Thread(target=inject_loop, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

    # Register activation/deactivation hotkeys via RegisterHotKey
    for i, hk in enumerate(istate.hotkeys):
        rhk_mods = tray_mods_to_rhk(hk.mods)
        user32.RegisterHotKey(_hwnd, ID_HK_BASE + i, rhk_mods, hk.key)
    # Kill switch hotkey (Ctrl+Alt+Shift+Win+F12)
    user32.RegisterHotKey(_hwnd, ID_HK_KILL, RHK_CTRL | RHK_ALT | RHK_SHIFT | RHK_WIN, KILL_VK)

    msg = MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

    # Unregister all hotkeys
    for i in range(len(istate.hotkeys)):
        user32.UnregisterHotKey(_hwnd, ID_HK_BASE + i)
    user32.UnregisterHotKey(_hwnd, ID_HK_KILL)
    _hook_mgr.stop()
    remove_tray()


def nssm_install_cmd():
    script = os.path.abspath(__file__)
    pw = AutoStartManager.pythonw()
    lines = [
        f'nssm install FlowShift "{pw}" "{script} --tray"',
        f'nssm set FlowShift AppDirectory "{BASE}"',
        'nssm set FlowShift Description "FlowShift - Tastatur/Maus Sharing"',
        'nssm set FlowShift Start SERVICE_AUTO_START',
        "nssm start FlowShift",
    ]
    return lines


def nssm_remove_cmd():
    return [
        "nssm stop FlowShift",
        "nssm remove FlowShift confirm",
    ]


if __name__ == "__main__":
    istate.config = load_config()
    istate.hotkeys = load_hotkeys(istate.config)

    if "--install-nssm" in sys.argv:
        print("=== NSSM Installationsbefehle (als Admin ausführen) ===")
        print()
        for line in nssm_install_cmd():
            print(line)
        print()
        print("Lade nssm herunter von: https://nssm.cc/download")
        print("Lege nssm.exe in %PATH% oder ins flowshift Verzeichnis")
        sys.exit(0)

    if "--remove-nssm" in sys.argv:
        print("=== NSSM Deinstallationsbefehle ===")
        print()
        for line in nssm_remove_cmd():
            print(line)
        sys.exit(0)

    if "--tray" in sys.argv:
        run()
    else:
        open_gui()
        run()
