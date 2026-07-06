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
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runtime_model as rm
import platform_capabilities as caps
import input_backends
import version
import clipboard_model as cbm
import clipboard_protocol as cbp
import clipboard_win
from clipboard_runtime import ClipboardManager
from runtime_model import (  # noqa: F401  (re-exported for legacy imports/tests)
    MOD_CTRL, MOD_SHIFT, MOD_ALT, MOD_WIN, MOD_NAMES, MODIFIER_VKS, VK_NAMES,
    vk_name, mods_name, format_hotkey,
    HotkeyBinding, load_hotkeys, default_hotkeys, sync_hotkeys,
    peer_identity, make_forward_action, parse_forward_action,
    resolve_peer_by_action, is_forward_action, is_return_action,
    peer_display_name, FramedReader, PressTracker,
    scale_mouse_point, normalize_absolute, diff_connectors,
    send_msg, recv_msg, recv_exact,
    is_extended_key, MouseCoalescer, mouse_settings, DEFAULT_MOUSE_SETTINGS,
    plan_activation, fwd_switch_ok, resolve_mouse_settings,
)

# Input backend for this OS (Windows here). Used to advertise real capabilities
# in the hello handshake. Capture/injection stay in the native paths below.
_backend = input_backends.get_backend("windows")

# Cache version info once (git calls are relatively slow; no CMD window).
_local_version = version.version_info()

BASE = os.path.dirname(__file__)


def _resolve_data_dir():
    """Directory for runtime data (config, logs). Env override for installed use.

    The installed service runs from %ProgramFiles% (read-only for non-admins), so
    config + logs live in %ProgramData%\\FlowShift via FLOWSHIFT_LOG_DIR. When run
    straight from the repo (dev), everything stays next to this file.
    """
    d = os.environ.get("FLOWSHIFT_LOG_DIR")
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            return BASE
        return d
    return BASE


DATA_DIR = _resolve_data_dir()
CONFIG_FILE = os.environ.get("FLOWSHIFT_CONFIG") or os.path.join(DATA_DIR, "config.json")
GUI_FILE = os.path.join(BASE, "gui.py")
AUTO_START_NAME = "FlowShift"
LOG_FILE = os.path.join(DATA_DIR, "flowshift.log")
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
WM_LBUTTONDBLCLK = 0x0203

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 1
NIF_ICON = 2
NIF_TIP = 4

INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
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

# RegisterHotKey uses different bit layout than tray internal mods
WM_HOTKEY = 0x0312
WM_RELOAD_HOTKEYS = WM_APP + 2  # posted to the window thread on config change
WM_APP_QUIT = WM_APP + 3        # posted to the window thread to quit the message loop
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

_log_lock = threading.Lock()
_rate_limit_lock = threading.Lock()
_rate_limit_last = {}
_config_mtime = 0.0
_connector_threads = {}
_connector_lock = threading.Lock()
_shutdown_requested = False
_shutdown_event = threading.Event()
_runtime_started_at = time.time()


def log(level, msg):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] [{level}] {msg}"
    with _log_lock:
        try:
            print(line)
        except Exception:
            pass
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
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


# ── Worker supervision ──────────────────────────────────────────────
# Runtime workers must never die silently: a crashed worker (e.g. the
# forward_loop NameError bug) previously stopped forwarding without any visible
# signal. Every worker runs through ``run_worker`` which catches + logs the full
# traceback and marks the worker failed; the status snapshot exposes health so
# the GUI can show it, and ``forwarding_ready`` refuses to swallow input when the
# forward_loop is not alive.
_worker_lock = threading.Lock()
_workers = {}  # name -> {"thread","started_at","failed","last_error"}

# Workers whose death breaks forwarding and must surface as a runtime error.
CRITICAL_WORKERS = (
    "forward_loop", "inject_loop", "network_thread",
    "connect_to_peers", "local_control_thread",
)


def _register_worker(name, thread):
    with _worker_lock:
        _workers[name] = {
            "thread": thread,
            "started_at": time.time(),
            "failed": False,
            "last_error": None,
        }


def _mark_worker_failed(name, err):
    with _worker_lock:
        w = _workers.get(name)
        if w is not None:
            w["failed"] = True
            w["last_error"] = repr(err)


def run_worker(name, target):
    """Run a worker function, logging any crash (with traceback) instead of
    letting the thread die silently."""
    try:
        log("INFO", f"worker started: {name}")
        target()
    except Exception as e:
        tb = traceback.format_exc()
        log("ERROR", f"worker crashed: {name}: {e!r}\n{tb}")
        _mark_worker_failed(name, e)
    finally:
        # A clean exit during shutdown is normal; otherwise this is noteworthy.
        level = "INFO" if _shutdown_event.is_set() else "WARN"
        log(level, f"worker exited: {name}")


def start_worker(name, target):
    t = threading.Thread(target=run_worker, args=(name, target), name=name, daemon=True)
    _register_worker(name, t)
    t.start()
    return t


def worker_health():
    out = {}
    with _worker_lock:
        items = list(_workers.items())
    for name, w in items:
        alive = bool(w["thread"] and w["thread"].is_alive())
        out[name] = {
            "alive": alive,
            "failed": bool(w["failed"]),
            "last_error": w["last_error"],
            "started_at": w["started_at"],
        }
    return out


def critical_workers_down():
    """Names of critical workers that are dead or crashed (ignored during shutdown)."""
    if _shutdown_event.is_set():
        return []
    down = []
    health = worker_health()
    for name in CRITICAL_WORKERS:
        w = health.get(name)
        if w is None or not w["alive"] or w["failed"]:
            down.append(name)
    return down


def forward_loop_healthy():
    if _shutdown_event.is_set():
        return True
    w = worker_health().get("forward_loop")
    return bool(w and w["alive"] and not w["failed"])


# ── Event-pipeline diagnostics ──────────────────────────────────────
_pipeline_lock = threading.Lock()
_pipeline = {
    "events_queued": 0,        # events put on the forward queue by hooks/synthetic
    "events_forwarded": 0,     # events actually sent to a peer
    "events_send_failed": 0,   # send attempts that failed
    "input_batches_received": 0,  # input batches received from a peer
    "events_injected": 0,      # events injected locally by inject_loop
    "inject_failed": 0,        # inject attempts that raised
}


def pipe_inc(key, n=1):
    with _pipeline_lock:
        _pipeline[key] = _pipeline.get(key, 0) + n


def pipeline_snapshot():
    with _pipeline_lock:
        d = dict(_pipeline)
    try:
        d["event_queue_size"] = istate.event_queue.qsize()
        d["inject_queue_size"] = istate.inject_queue.qsize()
    except Exception:
        d["event_queue_size"] = -1
        d["inject_queue_size"] = -1
    return d


# ── Session context (Session 0 = service, cannot do interactive input) ──
def session_info():
    info = {"session_id": None, "interactive": True, "username": None,
            "is_service_session": False}
    try:
        import getpass
        info["username"] = getpass.getuser()
    except Exception:
        pass
    try:
        kernel32.ProcessIdToSessionId.argtypes = [ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.ProcessIdToSessionId.restype = ctypes.c_int
        pid = kernel32.GetCurrentProcessId()
        sid = ctypes.c_ulong(0)
        if kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid)):
            s = int(sid.value)
            info["session_id"] = s
            info["is_service_session"] = (s == 0)
            info["interactive"] = (s != 0)
    except Exception:
        pass
    return info


def runtime_instance_already_running():
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.GetLastError.restype = ctypes.c_uint
    kernel32.CreateMutexW(None, 0, RUNTIME_MUTEX_NAME)
    return kernel32.GetLastError() == 183


def signal_main_quit():
    """Make the MAIN window thread leave its message loop.

    ``PostQuitMessage`` only affects the calling thread's queue, so calling it
    from a worker/hook thread never stops the main ``GetMessageW`` loop (the
    process would linger and keep holding the singleton mutex -> next start
    fails). Instead we post a message to the window; ``wnd_proc`` (which runs on
    the main thread) then calls ``PostQuitMessage``.
    """
    try:
        if _hwnd:
            user32.PostMessageW(_hwnd, WM_APP_QUIT, 0, 0)
        else:
            user32.PostQuitMessage(0)
    except Exception:
        pass


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
            istate.active_peer_label = None
    except Exception:
        pass
    try:
        istate.set_clip(False)
    except Exception:
        pass
    try:
        # Release anything we injected locally so no key/button stays stuck.
        release_injected_inputs("shutdown")
    except Exception:
        pass
    try:
        close_all_peer_connections("shutdown")
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
    # Quit the main message loop ON the main thread (not this worker thread).
    signal_main_quit()

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
# SendInput: check the return value (number of events inserted) + GetLastError.
user32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
user32.SendInput.restype = ctypes.c_uint

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


# vk_name / mods_name / format_hotkey are imported from runtime_model.


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


def _pythonw_exe():
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        w = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(w):
            return w
    return exe


def open_gui():
    try:
        subprocess.Popen(
            [_pythonw_exe(), GUI_FILE],
            creationflags=version.CREATE_NO_WINDOW,
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

    # Normalise hotkeys: migrate legacy index-based actions to stable identities,
    # add hotkeys for new peers, keep labels in sync. Persist if it changed.
    if sync_hotkeys(cfg):
        needs_save = True

    if needs_save or not os.path.exists(CONFIG_FILE):
        try:
            os.makedirs(os.path.dirname(CONFIG_FILE) or ".", exist_ok=True)
        except OSError:
            pass
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
    # OS-level hotkeys must be re-registered on the window thread.
    if _hwnd:
        try:
            user32.PostMessageW(_hwnd, WM_RELOAD_HOTKEYS, 0, 0)
        except Exception:
            pass
    return True


# default_hotkeys is imported from runtime_model.



def _is_ipv4(ip):
    try:
        socket.inet_aton(ip)
        return ip.count(".") == 3
    except OSError:
        return False


_local_ipv4_cache = None
_local_ipv4_cache_ts = 0.0
_LOCAL_IPV4_TTL = 30.0


def get_local_ipv4s():
    global _local_ipv4_cache, _local_ipv4_cache_ts
    if _local_ipv4_cache is not None and (time.monotonic() - _local_ipv4_cache_ts) < _LOCAL_IPV4_TTL:
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
                creationflags=version.CREATE_NO_WINDOW,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                if isinstance(data, dict):
                    data = [data]
                for item in data:
                    add(item.get("IPAddress"))
                if ips:
                    _local_ipv4_cache = tuple(ips)
                    _local_ipv4_cache_ts = time.monotonic()
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
    _local_ipv4_cache_ts = time.monotonic()
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


def build_local_hello(msg_type="hello"):
    """Build a protocol-v1 hello/ping/pong advertising OS + capabilities + version.

    Backward compatible: old peers ignore the extra fields; new peers read
    ``os`` / ``desktop`` / ``input_backend`` / ``capabilities`` / version info.
    """
    msg = caps.build_hello_from_backend(
        istate.config.get("device_id", ""),
        istate.config.get("device_name", "") or os.environ.get("COMPUTERNAME", ""),
        get_virtual_screen_spec(),
        _backend,
        port=istate.config.get("port", 45781),
        msg_type=msg_type,
    )
    msg["timestamp"] = time.time()
    msg["app_version"] = _local_version["app_version"]
    msg["git_commit"] = _local_version["git_commit"]
    msg["git_branch"] = _local_version["git_branch"]
    return msg


# Mouse scaling, HotkeyBinding and load_hotkeys are imported from runtime_model.
def _scale_mouse_point(x, y, source_spec, target_spec):
    return scale_mouse_point(x, y, source_spec, target_spec)


def _current_cursor_pos():
    pt = POINT()
    try:
        if user32.GetCursorPos(ctypes.byref(pt)):
            return int(pt.x), int(pt.y)
    except Exception:
        pass
    return None


class InputState:
    def __init__(self):
        self.active = False
        self.active_peer = None          # stable peer identity string, or None
        self.active_peer_label = None    # display name for UI/logging
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
        self._mouse_last_pos = None      # last forwarded source mouse position
        self.active_mouse = None         # resolved mouse settings for the active peer
        self.sent_tracker = PressTracker()    # keys/buttons we forwarded to a peer
        self.inject_tracker = PressTracker()  # keys/buttons we injected locally

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


# ── Clipboard (Layer 2: manager + watcher) ──────────────────────────
CLIPBOARD_ROOT = os.path.join(DATA_DIR, "clipboard")
_clip_last_set_text = None      # text we put on the clipboard (avoid re-capture)
_clip_last_set_files = None     # files we put on the clipboard (avoid re-capture)


def _clip_send(identity, msg):
    """Send a clipboard_* message to a peer over its link slot."""
    link = find_link_by_identity(identity)
    slot = _slot_for_send(link)
    if not slot:
        log("DEBUG", f"clipboard send: no connection for {identity}")
        return
    try:
        with slot["lock"]:
            send_msg(slot["conn"], msg)
    except Exception as e:
        log("DEBUG", f"clipboard send failed to {identity}: {e}")


def _clip_settings():
    return cbm.clipboard_settings(istate.config)


_clip_mgr = ClipboardManager(CLIPBOARD_ROOT, "", _clip_send, _clip_settings, log)


def clipboard_watcher():
    """Poll the Windows clipboard; on change, capture text into each peer's store."""
    _clip_mgr.device_id = istate.config.get("device_id", "")
    last_seq = clipboard_win.get_sequence_number()
    while not _shutdown_event.is_set():
        if _shutdown_event.wait(0.4):
            break
        try:
            if not _clip_settings().get("enabled"):
                last_seq = clipboard_win.get_sequence_number()
                continue
            seq = clipboard_win.get_sequence_number()
            if seq == last_seq:
                continue
            last_seq = seq
            idents = [peer_identity(p) for p in istate.config.get("peers", [])]
            # Files (CF_HDROP) take precedence over text when both are present.
            if clipboard_win.has_files():
                files = clipboard_win.read_files()
                if not files:
                    continue
                global _clip_last_set_files
                if _clip_last_set_files is not None and sorted(files) == _clip_last_set_files:
                    continue
                _clip_mgr.capture_files_all(idents, files)
                continue
            text = clipboard_win.read_text()
            if not text:
                continue
            # Ignore text we just placed ourselves (paste) to avoid a capture loop.
            global _clip_last_set_text
            if _clip_last_set_text is not None and text == _clip_last_set_text:
                continue
            _clip_mgr.capture_text_all(idents, text)
        except Exception as e:
            log_rate_limited("clip-watch-err", "DEBUG", f"clipboard watcher error: {e}", interval=5.0)


# ── Peer connection registry (keyed by stable identity, not display name) ──
def _safe_close(conn):
    try:
        conn.close()
    except Exception:
        pass


def _find_link_locked(keys):
    for link in istate.peers.values():
        if link["identity"] in keys or (link["aliases"] & keys):
            return link
    return None


def install_peer_connection(identity, aliases, direction, conn, meta):
    """Register (or replace) a peer connection under a stable identity.

    A stale connection of the same direction is closed so we never leak sockets.
    """
    keys = {identity} | set(aliases)
    replaced = None
    with istate.lock:
        link = _find_link_locked(keys)
        if link is None:
            link = {
                "identity": identity,
                "aliases": set(),
                "device_id": meta.get("device_id", ""),
                "display_name": meta.get("display_name", ""),
                "host": meta.get("host"),
                "port": meta.get("port"),
                "screen": meta.get("screen"),
                "os": meta.get("os"),
                "capabilities": meta.get("capabilities"),
                "version": meta.get("version"),
                "inbound": None,
                "outbound": None,
                "remote_forwarding_active": False,
                "remote_forwarding_source": "",
            }
            istate.peers[identity] = link
        link["aliases"] |= keys
        if meta.get("device_id"):
            link["device_id"] = meta["device_id"]
        if meta.get("display_name"):
            link["display_name"] = meta["display_name"]
        if meta.get("screen"):
            link["screen"] = meta["screen"]
        if meta.get("os"):
            link["os"] = meta["os"]
        if meta.get("capabilities"):
            link["capabilities"] = meta["capabilities"]
        if meta.get("version"):
            link["version"] = meta["version"]
        old = link.get(direction)
        if old and old.get("conn") is not conn:
            replaced = old.get("conn")
        link[direction] = {
            "conn": conn,
            "host": meta.get("host"),
            "port": meta.get("port"),
            "device_id": meta.get("device_id", ""),
            "display_name": meta.get("display_name", ""),
            "screen": meta.get("screen"),
            "lock": threading.Lock(),
        }
        label = link["display_name"]
    if replaced is not None:
        _safe_close(replaced)
        log("INFO", f"replaced stale {direction} connection for {label}")
    log("INFO", f"peer linked {label} dir={direction} identity={identity}")
    return link


def remove_peer_connection(conn):
    """Drop a connection from the registry. Returns the link if it became empty."""
    removed_link = None
    with istate.lock:
        for key, link in list(istate.peers.items()):
            changed = False
            for d in ("inbound", "outbound"):
                slot = link.get(d)
                if slot and slot.get("conn") is conn:
                    link[d] = None
                    changed = True
            if changed and not link["inbound"] and not link["outbound"]:
                removed_link = link
                del istate.peers[key]
    return removed_link


def close_all_peer_connections(reason):
    with istate.lock:
        links = list(istate.peers.values())
        istate.peers.clear()
    count = 0
    for link in links:
        for d in ("inbound", "outbound"):
            slot = link.get(d)
            if slot:
                _safe_close(slot["conn"])
                count += 1
    if count:
        log("INFO", f"closed {count} peer connection(s): {reason}")


def find_link_by_identity(identity):
    if not identity:
        return None
    with istate.lock:
        return _find_link_locked({identity})


def _slot_for_send(link):
    if not isinstance(link, dict):
        return None
    return link.get("outbound") or link.get("inbound")


def _send_events_via_slot(slot, events):
    src_screen = get_virtual_screen_spec()
    payload = []
    for ev in events:
        e = dict(ev)
        e.setdefault("source_screen", src_screen)
        payload.append(e)
    try:
        with slot["lock"]:
            send_msg(slot["conn"], {"type": "input", "events": payload})
        return True
    except Exception:
        log("ERROR", f"failed to send {len(payload)} event(s) to peer")
        return False


def _send_events_to_identity(identity, events):
    link = find_link_by_identity(identity)
    slot = _slot_for_send(link)
    if not slot:
        log("DEBUG", f"cannot send events, no connection for {identity}")
        return False
    return _send_events_via_slot(slot, events)


def release_injected_inputs(reason):
    """Inject key_up / mouseup for everything we injected but never released."""
    events = istate.inject_tracker.release_events()
    if not events:
        return
    log("INFO", f"releasing {len(events)} stuck injected input(s): {reason}")
    for ev in events:
        try:
            inject(ev)
        except Exception:
            pass


def _notify_fwd_state(identity, active):
    """Tell a peer whether we are actively forwarding to them (fwd_state message)."""
    link = find_link_by_identity(identity)
    slot = _slot_for_send(link)
    if not slot:
        return
    local_name = (istate.config.get("device_name", "") or
                  os.environ.get("COMPUTERNAME", "")).strip() or "Unbekannt"
    try:
        with slot["lock"]:
            send_msg(slot["conn"], {
                "type": "fwd_state",
                "active": active,
                "source_name": local_name,
            })
        log("DEBUG", f"fwd_state sent to {identity}: active={active}")
    except Exception as e:
        log("DEBUG", f"fwd_state notify failed to {identity}: {e}")


# ── Flying direction switch: fwd_control request/response over the link ──
_fwd_control_lock = threading.Lock()
_fwd_control_waiters = {}  # identity -> {"event": Event, "result": dict}


def _await_fwd_control(identity, timeout):
    with _fwd_control_lock:
        entry = _fwd_control_waiters.get(identity)
    if entry is None:
        return {"status": "failed", "message": "no waiter"}
    got = entry["event"].wait(timeout)
    with _fwd_control_lock:
        entry = _fwd_control_waiters.pop(identity, None)
    if not got:
        return {"status": "timeout", "message": "no response from peer"}
    return (entry or {}).get("result") or {"status": "failed"}


def _register_fwd_waiter(identity):
    with _fwd_control_lock:
        _fwd_control_waiters[identity] = {"event": threading.Event(), "result": None}


def _cancel_fwd_waiter(identity):
    with _fwd_control_lock:
        _fwd_control_waiters.pop(identity, None)


def _deliver_fwd_control_result(identity, msg):
    with _fwd_control_lock:
        w = _fwd_control_waiters.get(identity)
        if w is not None:
            w["result"] = msg
            w["event"].set()


def _reply_on_conn(identity, conn, msg):
    """Send a reply on a peer connection, serialised via the link's slot lock."""
    link = find_link_by_identity(identity)
    slot = _slot_for_send(link)
    lock = slot["lock"] if isinstance(slot, dict) and slot.get("lock") else None
    try:
        if lock:
            with lock:
                send_msg(conn, msg)
        else:
            send_msg(conn, msg)
    except Exception as e:
        log("DEBUG", f"reply send failed to {identity}: {e}")


def request_remote_deactivate(identity, reason="switch-direction", timeout=3.0):
    """Ask the peer to stop forwarding to us; wait for its fwd_control_result."""
    link = find_link_by_identity(identity)
    slot = _slot_for_send(link)
    if not slot:
        return {"status": "failed", "message": "no connection to peer"}
    # Register the waiter BEFORE sending so a fast reply is never missed (race).
    _register_fwd_waiter(identity)
    try:
        with slot["lock"]:
            send_msg(slot["conn"], {
                "type": "fwd_control",
                "action": "request_deactivate",
                "requested_by": istate.config.get("device_id", ""),
                "reason": reason,
            })
    except Exception as e:
        _cancel_fwd_waiter(identity)
        return {"status": "failed", "message": str(e)}
    log("INFO", f"fwd_control request_deactivate sent to {identity} ({reason})")
    res = _await_fwd_control(identity, timeout)
    log("INFO", f"fwd_control result from {identity}: {res.get('status')}")
    return res


# ── Central forwarding activation / deactivation ────────────────────
def _activate_forward_peer(peer, source="hotkey"):
    if is_local_host(peer.get("host", "")):
        log("WARN", f"refusing to activate local peer {peer_display_name(peer)}")
        return False, "peer points to the local device"
    ident = peer_identity(peer)

    # Flying direction switch: decide the steps (never both directions at once).
    link = find_link_by_identity(ident)
    remote_fwd = bool(isinstance(link, dict) and link.get("remote_forwarding_active"))
    with istate.lock:
        plan = plan_activation(istate.active, istate.active_peer, ident, remote_fwd)
    if plan["already_active_here"]:
        return True, None

    # 1) If the target peer is forwarding TO us, ask it to stop first and WAIT.
    if plan["need_remote_deactivate"]:
        log("INFO", f"switching direction: {ident} is forwarding to us; requesting deactivate")
        res = request_remote_deactivate(ident, "switch-direction", timeout=3.0)
        if not fwd_switch_ok(res.get("status")):
            log("WARN", f"remote deactivate not confirmed ({res.get('status')}); NOT activating")
            return False, f"Gegenrichtung nicht deaktiviert: {res.get('status')}"
        with istate.lock:
            l2 = _find_link_locked({ident})
            if isinstance(l2, dict):
                l2["remote_forwarding_active"] = False
                l2["remote_forwarding_source"] = ""

    # 2) Stop our own forwarding to a different peer (clean cleanup) first.
    if plan["need_local_deactivate"]:
        deactivate_forward("switch-local")

    # 3) Drain any stale queued events so nothing from before the switch is sent.
    _drain_queue(istate.event_queue)

    with istate.lock:
        istate.active = True
        istate.active_peer = ident
        istate.active_peer_label = peer_display_name(peer)
        istate.active_mouse = resolve_mouse_settings(istate.config, peer)
        istate._mouse_last_pos = _current_cursor_pos()
        istate.set_clip(True)
    istate.sent_tracker.clear()
    _hook_mgr.start()
    log("INFO", f"forwarding activated -> {istate.active_peer_label} "
                f"({istate.active_peer}) via {source}")
    _notify_fwd_state(ident, True)
    # Clipboard: sync missing items with this peer on activation (if enabled).
    try:
        _clip_mgr.on_profile_activated(ident)
    except Exception as e:
        log("DEBUG", f"clipboard on_activate error: {e}")
    update_tray()
    return True, None


def _drain_queue(q):
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass


def activate_forward_action(action, source="hotkey"):
    peer = resolve_peer_by_action(istate.config, action)
    if not peer:
        log("WARN", f"hotkey target unresolved, ignoring: {action}")
        return False, "unresolved hotkey target"
    return _activate_forward_peer(peer, source)


def activate_forward_action_async(action, source="hotkey"):
    """Activate from a hook/window thread WITHOUT blocking on the network switch."""
    def worker():
        try:
            activate_forward_action(action, source)
        except Exception as e:
            log("ERROR", f"async activate failed: {e!r}")
    threading.Thread(target=worker, name="activate-switch", daemon=True).start()


def activate_first_forward_async(source="tray"):
    def worker():
        try:
            activate_first_forward(source)
        except Exception as e:
            log("ERROR", f"async activate-first failed: {e!r}")
    threading.Thread(target=worker, name="activate-first-switch", daemon=True).start()


def activate_first_forward(source="tray"):
    for hk in istate.hotkeys:
        if is_forward_action(hk.action):
            peer = resolve_peer_by_action(istate.config, hk.action)
            if peer:
                return _activate_forward_peer(peer, source)
    log("WARN", "no resolvable forward hotkey to activate")
    return False, "no forwarding target available"


def deactivate_forward(reason="return_local"):
    with istate.lock:
        was_active = istate.active
        prev_identity = istate.active_peer
        istate.active = False
        istate.active_peer = None
        istate.active_peer_label = None
        istate._mouse_last_pos = None
        istate.set_clip(False)
    if was_active and prev_identity:
        release = istate.sent_tracker.release_events()
        if release:
            _send_events_to_identity(prev_identity, release)
        _notify_fwd_state(prev_identity, False)
    istate.sent_tracker.clear()
    _hook_mgr.stop()
    if was_active:
        log("INFO", f"forwarding deactivated ({reason})")
    update_tray()
    return True, None


def forwarding_ready():
    """True only when forwarding is active AND a connected peer slot exists.

    Gates input suppression: if forwarding is nominally active but the peer is
    not actually connected (or cannot be sent to), local input must NOT be
    swallowed. This is the fail-safe that keeps the local machine usable.
    """
    with istate.lock:
        if not istate.active or not istate.active_peer:
            return False
        identity = istate.active_peer
    # If the forward_loop worker is dead, we must NOT swallow input (it would be
    # captured but never sent -> local machine becomes unusable).
    if not forward_loop_healthy():
        log_rate_limited("fwd-loop-dead", "ERROR",
                         "CRITICAL: forward_loop is not running; forwarding disabled, "
                         "keeping input local", interval=2.0)
        return False
    link = find_link_by_identity(identity)
    return rm.should_suppress_input(True, _slot_for_send(link) is not None)


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
                istate.active_peer_label = None
                try:
                    with open(KILL_FILE, "w") as _f:
                        _f.write("1")
                except Exception:
                    pass
                log("INFO", "kill switch armed, forwarding stopped, quitting message loop")
                update_tray()
                signal_main_quit()
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
                mods = istate.current_mods()
                active = istate.active
                hk = istate.find_hotkey(mods, vk) if down else None

            # Hotkey handling runs OUTSIDE the lock so network I/O never blocks
            # the low-level hook callback while holding istate.lock.
            if down and hk is not None:
                if is_return_action(hk.action) and active:
                    deactivate_forward("hotkey")
                    return 1
                if is_forward_action(hk.action) and not active:
                    # Resolve synchronously (cheap); do the (possibly network-
                    # blocking) switch on a worker thread so the hook never stalls.
                    if resolve_peer_by_action(istate.config, hk.action):
                        activate_forward_action_async(hk.action, "hotkey")
                        return 1
                    # unresolved target: fall through, do not swallow the key

            with istate.lock:
                active = istate.active
            if active and not forwarding_ready():
                # Forwarding is on but the peer is not connected: never swallow
                # local input in this state.
                log_rate_limited("kb-not-ready", "WARN",
                                 "forwarding active but peer not connected; keeping keyboard local",
                                 interval=1.0)
                return user32.CallNextHookEx(None, code, wparam, lparam)
            if active:
                pass_through = istate.find_hotkey(istate.current_mods(), vk) if down else None
                if not pass_through:
                    ev = {"type": "key" if down else "key_up", "code": vk}
                    log_rate_limited(
                        f"kb-{ev['type']}", "DEBUG",
                        f"keyboard queued {ev['type']} vk={vk} -> {istate.active_peer_label}",
                        interval=0.25,
                    )
                    istate.event_queue.put(ev)
                    pipe_inc("events_queued")
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
            # Fail-safe: only suppress the mouse when the peer is truly connected.
            if not forwarding_ready():
                log_rate_limited("mouse-not-ready", "WARN",
                                 "forwarding active but peer not connected; keeping mouse local",
                                 interval=1.0)
                return user32.CallNextHookEx(None, code, wparam, lparam)

            ms = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if (ms.flags & HOOK_INJECTED_FLAGS) or int(ms.dwExtraInfo) == INJECTED_EXTRA_INFO:
                log("DEBUG", f"mouse ignored injected msg={wparam} flags=0x{int(ms.flags):X} extra=0x{int(ms.dwExtraInfo):X}")
                return user32.CallNextHookEx(None, code, wparam, lparam)
            ev = None
            if wparam == WM_MOUSEMOVE:
                pos = (int(ms.pt.x), int(ms.pt.y))
                with istate.lock:
                    prev = istate._mouse_last_pos
                # IMPORTANT: do NOT update _mouse_last_pos here.
                # The cursor is suppressed → stays at the activation anchor.
                # ms.pt = actual_cursor_pos + hardware_delta, so
                # dx = ms.pt - anchor  =  true hardware delta for this event.
                if prev is None:
                    # Anchor not set yet; prime it with the real cursor position.
                    with istate.lock:
                        istate._mouse_last_pos = _current_cursor_pos() or pos
                    return 1
                dx = pos[0] - prev[0]
                dy = pos[1] - prev[1]
                if dx or dy:
                    ev = {"type": "mousemove", "mode": "relative", "dx": dx, "dy": dy}
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
                pipe_inc("events_queued")
                return 1
    except Exception:
        pass
    return user32.CallNextHookEx(None, code, wparam, lparam)


def _send_input(inp, desc):
    """Call SendInput for one INPUT and check the result (+GetLastError)."""
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        err = kernel32.GetLastError()
        log("ERROR", f"SendInput failed for {desc}: inserted={sent} err={err}")
        return False
    return True


def _inject_vk_tap(vk):
    """Inject a single virtual-key tap (down + up)."""
    extended = KEYEVENTF_EXTENDEDKEY if is_extended_key(vk) else 0
    for up in (0, KEYEVENTF_KEYUP):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        ki = KEYBDINPUT()
        ki.wVk = vk
        ki.dwFlags = up | extended
        ki.dwExtraInfo = INJECTED_EXTRA_INFO
        inp.u.ki = ki
        _send_input(inp, f"vk 0x{vk:02X} up={bool(up)}")


def _inject_unicode_char(ch):
    """Inject one Unicode character via KEYEVENTF_UNICODE (down + up)."""
    for up in (0, KEYEVENTF_KEYUP):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        ki = KEYBDINPUT()
        ki.wVk = 0
        ki.wScan = ord(ch)
        ki.dwFlags = KEYEVENTF_UNICODE | up
        ki.dwExtraInfo = INJECTED_EXTRA_INFO
        inp.u.ki = ki
        _send_input(inp, f"unicode {ch!r} up={bool(up)}")


def inject(ev):
    try:
        inp = INPUT()
        t = ev.get("type", "")
        if t == "type_text":
            text = str(ev.get("text", ""))
            log("INFO", f"inject type_text len={len(text)}")
            for ch in text:
                if ch == "\n":
                    _inject_vk_tap(0x0D)        # Enter (reliable newline)
                elif ch == "\r":
                    continue
                elif ch == "\t":
                    _inject_vk_tap(0x09)        # Tab
                else:
                    _inject_unicode_char(ch)
            return
        if t in ("key", "key_up"):
            inp.type = INPUT_KEYBOARD
            ki = KEYBDINPUT()
            ki.wVk = ev["code"]
            flags = 0 if t == "key" else KEYEVENTF_KEYUP
            # Extended keys (arrows, Home/End, Insert/Delete, PageUp/Down, right
            # Ctrl/Alt, ...) MUST carry KEYEVENTF_EXTENDEDKEY or Shift+Arrow style
            # selection breaks (Windows treats them as numpad keys otherwise).
            if is_extended_key(ev["code"]):
                flags |= KEYEVENTF_EXTENDEDKEY
            ki.dwFlags = flags
            ki.dwExtraInfo = INJECTED_EXTRA_INFO
            inp.u.ki = ki
            log("DEBUG", f"inject key {t} vk={ev['code']} ext={bool(flags & KEYEVENTF_EXTENDEDKEY)}")
            _send_input(inp, f"key {t} vk={ev['code']}")
        elif t == "mousemove":
            inp.type = INPUT_MOUSE
            mi = MOUSEINPUT()
            if ev.get("mode") == "relative" or "dx" in ev or "dy" in ev:
                mi.dx = int(ev.get("dx", 0))
                mi.dy = int(ev.get("dy", 0))
                mi.dwFlags = MOUSEEVENTF_MOVE
            else:
                target_screen = ev.get("target_screen") or get_virtual_screen_spec()
                source_screen = ev.get("source_screen")
                x, y = _scale_mouse_point(ev["x"], ev["y"], source_screen, target_screen)
                mi.dx = normalize_absolute(x, int(target_screen.get("left", 0)), int(target_screen.get("width", 1)))
                mi.dy = normalize_absolute(y, int(target_screen.get("top", 0)), int(target_screen.get("height", 1)))
                mi.dwFlags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE | MOUSEEVENTF_VIRTUALDESK
            mi.dwExtraInfo = INJECTED_EXTRA_INFO
            inp.u.mi = mi
            ok = _send_input(inp, "mousemove")
            log_rate_limited(
                "inject-mousemove", "DEBUG",
                f"inject mousemove src=({ev.get('x')},{ev.get('y')}) "
                f"mode={ev.get('mode', 'absolute')} "
                f"src_screen={format_screen_spec(ev.get('source_screen'))} "
                f"tgt_screen={format_screen_spec(ev.get('target_screen') or get_virtual_screen_spec())} "
                f"delta=({mi.dx},{mi.dy}) ok={ok}",
                interval=0.25,
            )
        elif t == "mousedown":
            inp.type = INPUT_MOUSE
            mi = MOUSEINPUT()
            flags = [MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_MIDDLEDOWN]
            mi.dwFlags = flags[ev["button"]]
            mi.dwExtraInfo = INJECTED_EXTRA_INFO
            inp.u.mi = mi
            log("DEBUG", f"inject mousedown button={ev['button']}")
            _send_input(inp, f"mousedown button={ev['button']}")
        elif t == "mouseup":
            inp.type = INPUT_MOUSE
            mi = MOUSEINPUT()
            flags = [MOUSEEVENTF_LEFTUP, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_MIDDLEUP]
            mi.dwFlags = flags[ev["button"]]
            mi.dwExtraInfo = INJECTED_EXTRA_INFO
            inp.u.mi = mi
            log("DEBUG", f"inject mouseup button={ev['button']}")
            _send_input(inp, f"mouseup button={ev['button']}")
        elif t == "wheel":
            inp.type = INPUT_MOUSE
            mi = MOUSEINPUT()
            mi.mouseData = ev["delta"] & 0xFFFFFFFF
            mi.dwFlags = MOUSEEVENTF_WHEEL
            mi.dwExtraInfo = INJECTED_EXTRA_INFO
            inp.u.mi = mi
            log("DEBUG", f"inject wheel delta={ev['delta']}")
            _send_input(inp, f"wheel delta={ev['delta']}")
        # Track pressed keys/buttons so we can release them if the peer vanishes.
        istate.inject_tracker.apply(ev)
    except Exception:
        log("ERROR", f"inject failed for event {ev.get('type', '?')}")
        pass


# recv_exact / send_msg / recv_msg are imported from runtime_model.


def _connection_identity(device_id, display_name, dial_host, dial_port, is_server):
    """Primary stable identity for a peer connection."""
    device_id = (device_id or "").strip().lower()
    if device_id:
        return f"device:{device_id}"
    if not is_server and dial_host:
        return f"endpoint:{dial_host}:{dial_port}"
    return f"name:{display_name}"


def _connection_aliases(device_id, display_name, dial_host, dial_port):
    aliases = set()
    device_id = (device_id or "").strip().lower()
    if device_id:
        aliases.add(f"device:{device_id}")
    if dial_host:
        aliases.add(f"endpoint:{dial_host}:{dial_port}")
    if display_name:
        aliases.add(f"name:{display_name}")
    return aliases


def peer_handler(conn, addr, is_server, dial_host=None, dial_port=None, stop_event=None):
    """Handle one peer connection: handshake, then a shutdown-aware read loop.

    ``stop_event`` (set by the connector when the peer's host/port changed) makes
    the read loop exit so a fresh connector can dial the new address.
    """
    name = str(addr)
    remote_device_id = ""
    remote_screen = None
    reader = FramedReader(conn)
    installed = False
    try:
        local_screen = get_virtual_screen_spec()

        # First message: may be a one-shot ping (from ping_peer) or a hello.
        first = reader.read_message(0.3)

        if first and first.get("type") == "ping":
            sender_name = first.get("display_name", str(addr))
            sender_device_id = first.get("device_id", "") or ""
            log("INFO", f"ping received from {sender_name} {addr[0]}:{addr[1]} "
                        f"device_id={sender_device_id or '-'}")
            send_msg(conn, build_local_hello("pong"))
            log("INFO", f"pong sent to {sender_name} {addr[0]}:{addr[1]}")
            return

        # Normal peer: exchange hellos.
        send_msg(conn, build_local_hello("hello"))
        log("DEBUG", f"hello sent to {addr[0]}:{addr[1]} server={is_server}")

        if first is None:
            # Wait for their hello, but stay responsive to shutdown.
            deadline = time.monotonic() + 5.0
            while first is None and not _shutdown_event.is_set() and \
                    not (stop_event and stop_event.is_set()) and time.monotonic() < deadline:
                first = reader.read_message(1.0)

        if not (first and first.get("type") == "hello"):
            log("DEBUG", f"no hello from {addr[0]}:{addr[1]}, dropping")
            return

        name = first.get("display_name", str(addr))
        remote_device_id = first.get("device_id", "") or ""
        remote_screen = first.get("screen")
        remote_meta = caps.parse_hello(first)
        remote_version = {
            "app_version": first.get("app_version", "unknown"),
            "git_commit": first.get("git_commit", "unknown"),
            "git_branch": first.get("git_branch", "unknown"),
            "protocol_version": remote_meta.get("protocol_version", 0),
        }

        # Reject self-connections (same device_id or our own endpoint).
        if remote_device_id and remote_device_id.strip().lower() == \
                str(istate.config.get("device_id", "")).strip().lower():
            log("WARN", f"refusing self-connection to {name} ({addr[0]}:{addr[1]})")
            return

        log("INFO", f"peer hello from {name} {addr[0]}:{addr[1]} "
                    f"device_id={remote_device_id or '-'} os={remote_meta.get('os')} "
                    f"app={remote_version['app_version']} commit={remote_version['git_commit'][:12]}")

        identity = _connection_identity(remote_device_id, name, dial_host, dial_port, is_server)
        aliases = _connection_aliases(remote_device_id, name, dial_host, dial_port)
        direction = "inbound" if is_server else "outbound"
        install_peer_connection(identity, aliases, direction, conn, {
            "device_id": remote_device_id,
            "display_name": name,
            "host": dial_host or addr[0],
            "port": dial_port or addr[1],
            "screen": remote_screen,
            "os": remote_meta.get("os"),
            "capabilities": remote_meta.get("capabilities"),
            "version": remote_version,
        })
        installed = True
        log("INFO", f"peer linked {name} {addr[0]}:{addr[1]} screen={format_screen_spec(remote_screen)}")

        # Read loop: timeout-tolerant, checks shutdown regularly.
        while not _shutdown_event.is_set() and not (stop_event and stop_event.is_set()):
            msg = reader.read_message(1.0)
            if msg is None:
                continue
            if msg.get("type") == "input":
                events = msg.get("events", [])
                pipe_inc("input_batches_received")
                log_rate_limited(f"in-{identity}", "DEBUG",
                                 f"input batch from {name}: {len(events)} events", interval=0.5)
                target_screen = get_virtual_screen_spec()
                for ev in events:
                    payload = dict(ev)
                    if remote_screen and not payload.get("source_screen"):
                        payload["source_screen"] = remote_screen
                    payload["target_screen"] = target_screen
                    istate.inject_queue.put(payload)
            elif msg.get("type") == "fwd_state":
                with istate.lock:
                    link = _find_link_locked({identity})
                    if isinstance(link, dict):
                        link["remote_forwarding_active"] = bool(msg.get("active"))
                        link["remote_forwarding_source"] = str(msg.get("source_name", ""))
                log("INFO", f"fwd_state from {name}: active={msg.get('active')} "
                            f"src={msg.get('source_name', '')!r}")
            elif msg.get("type") == "fwd_control":
                # The peer wants to switch direction and asks us to stop forwarding.
                if msg.get("action") == "request_deactivate":
                    with istate.lock:
                        we_forward_to_them = istate.active and istate.active_peer == identity
                    if we_forward_to_them:
                        deactivate_forward("remote-switch")
                        status, note = "ok", "deactivated"
                    else:
                        status, note = "ok", "was-not-forwarding-to-you"
                    _reply_on_conn(identity, conn, {
                        "type": "fwd_control_result",
                        "action": "request_deactivate",
                        "status": status,
                        "message": note,
                    })
                    log("INFO", f"fwd_control request_deactivate from {name}: {status} ({note})")
            elif msg.get("type") == "fwd_control_result":
                _deliver_fwd_control_result(identity, msg)
                log("INFO", f"fwd_control_result from {name}: {msg.get('status')}")
            elif str(msg.get("type", "")).startswith("clipboard_"):
                try:
                    _clip_mgr.handle(identity, msg)
                except Exception as e:
                    log("ERROR", f"clipboard handle error from {name}: {e!r}")
            elif msg.get("type") == "hello":
                log("DEBUG", f"duplicate hello from {name}")
    except Exception as e:
        log("DEBUG", f"peer handler ended for {name} {addr[0]}:{addr[1]}: {e!r}")
    finally:
        _safe_close(conn)
        if installed:
            removed = remove_peer_connection(conn)
            log("INFO", f"peer disconnected {name} {addr[0]}:{addr[1]}")
            # If this was the last link and it was an input source, release stuck inputs.
            if removed is not None:
                release_injected_inputs(f"peer {name} disconnected")


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


def _set_tcp_nodelay(sock):
    """Disable Nagle's algorithm so small input frames are sent immediately.

    Nagle batches tiny writes to reduce packet count, which adds tens of ms of
    latency to a stream of small mouse/key frames and is a major jitter source.
    """
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


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
                _set_tcp_nodelay(c)
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
    """Stable connector token = peer identity string."""
    return peer_identity(peer)


def peer_token_active(peer):
    if not isinstance(peer, dict):
        return False
    return not is_local_host(peer.get("host", ""))


def find_config_peer(peer_ref):
    if not peer_ref:
        return None
    with istate.lock:
        for p in istate.config.get("peers", []):
            if (p.get("name") == peer_ref or p.get("host") == peer_ref
                    or p.get("device_id") == peer_ref or peer_identity(p) == peer_ref):
                return dict(p)
    return None


def config_has_peer_token(token):
    with istate.lock:
        for p in istate.config.get("peers", []):
            if peer_identity(p) == token:
                return True
    return False


def find_config_peer_by_token(token):
    """Return a fresh copy of the current config peer for a connector token."""
    with istate.lock:
        for p in istate.config.get("peers", []):
            if peer_identity(p) == token:
                return dict(p)
    return None


def connect_one(token, stop_event):
    """Outbound connector for one peer token.

    The current host/port are re-read from the config on every loop iteration
    (so a same-identity address edit is picked up), and ``stop_event`` lets the
    connector manager tear this connector down when the address changed or the
    peer was removed.
    """
    peer = find_config_peer_by_token(token)
    name = peer.get("name", peer.get("host", "peer")) if peer else token
    log("INFO", f"connector thread started for {name} ({token})")
    while not _shutdown_event.is_set() and not stop_event.is_set():
        peer = find_config_peer_by_token(token)
        if not peer:
            log("INFO", f"connector exiting for {name} because peer was removed")
            return
        name = peer.get("name", peer.get("host", "peer"))
        host = peer.get("host")
        port = int(peer.get("port", 45781))
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect((host, port))
            _set_tcp_nodelay(s)
            log("DEBUG", f"outbound connect ok to {name} {host}:{port}")
            peer_handler(s, (host, port), False, dial_host=host, dial_port=port,
                         stop_event=stop_event)
        except Exception as e:
            log("DEBUG", f"outbound connect failed to {name} {host}:{port}: {e}")
        finally:
            if s is not None:
                _safe_close(s)
        # Backoff, but stay responsive to shutdown and stop.
        end = time.monotonic() + 5.0
        while time.monotonic() < end:
            if _shutdown_event.is_set() or stop_event.is_set():
                break
            time.sleep(0.2)
    log("DEBUG", f"connector thread ended for {name} ({token})")


def connect_to_peers():
    log("INFO", "peer connector manager started")
    while not _shutdown_event.is_set():
        reload_config_if_changed()
        peers = list(istate.config.get("peers", []))
        with _connector_lock:
            # Desired token -> (host, port) from the live config.
            desired = {}
            for peer in peers:
                if not peer_token_active(peer):
                    continue
                try:
                    port = int(peer.get("port", 45781) or 45781)
                except (TypeError, ValueError):
                    port = 45781
                desired[peer_token(peer)] = (peer.get("host"), port)

            # Current running connectors -> their (host, port).
            current = {tok: (info["host"], info["port"])
                       for tok, info in _connector_threads.items()}

            to_stop, to_start = diff_connectors(current, desired)

            # Stop connectors that are gone or whose address changed.
            for token in to_stop:
                info = _connector_threads.pop(token, None)
                if info is not None:
                    info["stop"].set()
                    old = f"{info['host']}:{info['port']}"
                    if token in desired:
                        new = f"{desired[token][0]}:{desired[token][1]}"
                        log("INFO", f"peer {token} address changed {old} -> {new}, restarting connector")
                    else:
                        log("INFO", f"peer {token} removed, stopping connector ({old})")

            # (Re)start connectors that are new, changed, or died.
            for token in set(to_start) | set(desired):
                info = _connector_threads.get(token)
                if info is not None and info["thread"].is_alive():
                    continue
                host, port = desired[token]
                stop_event = threading.Event()
                thread = threading.Thread(target=connect_one, args=(token, stop_event), daemon=True)
                _connector_threads[token] = {"thread": thread, "host": host,
                                             "port": port, "stop": stop_event}
                log("INFO", f"starting connector thread for {token} -> {host}:{port}")
                thread.start()
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
        send_msg(sock, build_local_hello("ping"))
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


def _forward_send_event(ev, identity, label):
    """Send one event to the active peer; deactivate (fail-safe) on failure.

    Returns True on success, False if forwarding was deactivated.
    """
    link = find_link_by_identity(identity)
    slot = _slot_for_send(link)
    if not slot:
        log("WARN", f"no connection for active peer {label}; deactivating forwarding (fail-safe)")
        deactivate_forward("peer-not-connected")
        return False
    if _send_events_via_slot(slot, [ev]):
        istate.sent_tracker.apply(ev)
        pipe_inc("events_forwarded")
        return True
    log("WARN", f"forward send failed -> {label}; deactivating forwarding (fail-safe)")
    pipe_inc("events_send_failed")
    deactivate_forward("send-failed")
    return False


def forward_loop():
    """Coalescing sender thread.

    The hooks never touch the network; they only enqueue events here. Mouse-move
    events are accumulated and flushed at a fixed interval (coalescing) so a flood
    of tiny hardware deltas becomes a small number of network sends + SendInput
    calls -> much smoother remote motion. Keyboard, mouse buttons and wheel are
    sent immediately and in order; a pending movement is always flushed BEFORE
    such an event so clicks land at the correct position. No key/click is ever
    coalesced or dropped.
    """
    coalescer = None
    flush_interval = DEFAULT_MOUSE_SETTINGS["flush_interval_ms"] / 1000.0
    max_batch = DEFAULT_MOUSE_SETTINGS["max_batch_ms"] / 1000.0
    first_accum = None

    def flush_move(identity, label):
        nonlocal first_accum
        if coalescer is None or not coalescer.pending:
            return True
        d = coalescer.flush()
        first_accum = None
        if d is None:
            return True
        ok = _forward_send_event(
            {"type": "mousemove", "mode": "relative", "dx": d[0], "dy": d[1]},
            identity, label)
        if ok:
            log_rate_limited("fwd-move", "DEBUG",
                             f"forward mousemove dx={d[0]} dy={d[1]} -> {label}", interval=0.5)
        return ok

    while not _shutdown_event.is_set():
        with istate.lock:
            identity = istate.active_peer
            active = istate.active
            label = istate.active_peer_label
            cfg = istate.config

        if not active or not identity:
            # Not forwarding: drop any queued events and reset the coalescer so no
            # stale movement is replayed when forwarding is re-activated.
            try:
                while True:
                    istate.event_queue.get_nowait()
            except queue.Empty:
                pass
            coalescer = None
            first_accum = None
            time.sleep(0.02)
            continue

        if coalescer is None:
            s = mouse_settings(cfg)
            with istate.lock:
                if istate.active_mouse:
                    s = dict(istate.active_mouse)   # per-profile mouse settings
            coalescer = MouseCoalescer(s["sensitivity"], s["accumulate_subpixel"])
            flush_interval = s["flush_interval_ms"] / 1000.0
            max_batch = s["max_batch_ms"] / 1000.0
            first_accum = None

        try:
            ev = istate.event_queue.get(timeout=flush_interval)
        except queue.Empty:
            ev = None

        now = time.monotonic()

        if ev is not None:
            etype = ev.get("type")
            is_rel_move = (etype == "mousemove" and
                           (ev.get("mode") == "relative" or "dx" in ev or "dy" in ev))
            if is_rel_move:
                coalescer.add(int(ev.get("dx", 0)), int(ev.get("dy", 0)))
                if first_accum is None:
                    first_accum = now
            else:
                # Flush pending movement first (ordering), then send immediately.
                if not flush_move(identity, label):
                    continue
                if _forward_send_event(ev, identity, label):
                    log_rate_limited("fwd-ok", "DEBUG",
                                     f"forward {etype} -> {label}", interval=0.5)
                continue

        # Time-based flush: bound movement latency to flush_interval (and never
        # exceed max_batch even under a continuous event flood).
        if coalescer.pending and first_accum is not None:
            elapsed = now - first_accum
            if elapsed >= flush_interval or elapsed >= max_batch:
                flush_move(identity, label)


def _menu_summary():
    return ["keyboard", "mouse move", "mouse buttons", "mouse wheel"]


def resolve_peer_connection(peer_ref):
    """Resolve a config peer reference (identity/name/host/device_id) to a link.

    Returns (display_name, link) or (None, None).
    """
    if not peer_ref:
        return None, None

    # Direct identity match first.
    link = find_link_by_identity(peer_ref)
    if isinstance(link, dict):
        return link.get("display_name"), link

    # Otherwise map a config peer reference onto a live link via its identity.
    with istate.lock:
        cfg_peer = next(
            (
                p
                for p in istate.config.get("peers", [])
                if peer_identity(p) == peer_ref
                or p.get("name") == peer_ref
                or p.get("host") == peer_ref
                or p.get("device_id") == peer_ref
            ),
            None,
        )
    if not cfg_peer or is_local_host(cfg_peer.get("host", "")):
        return None, None
    link = find_link_by_identity(peer_identity(cfg_peer))
    if isinstance(link, dict):
        return link.get("display_name"), link
    return None, None


def _slot_display_name(slot, fallback="-"):
    if not isinstance(slot, dict):
        return fallback
    return (slot.get("display_name") or slot.get("host") or fallback).strip() or fallback


def build_connection_summary(preferred_peer=None):
    local_name = (istate.config.get("device_name", "") or os.environ.get("COMPUTERNAME", "")).strip() or "Unbekannt"

    peer_ref = preferred_peer or istate.active_peer
    peer_label = istate.active_peer_label
    peer_info = None
    if peer_ref:
        name, peer_info = resolve_peer_connection(peer_ref)
        if name:
            peer_label = name

    if peer_info is None:
        for link in istate.peers.values():
            if isinstance(link, dict):
                peer_label = link.get("display_name")
                peer_info = link
                break

    if isinstance(peer_info, dict):
        inbound = peer_info.get("inbound")
        outbound = peer_info.get("outbound")
        if istate.active and outbound:
            remote = _slot_display_name(outbound)
            return {"label": f"{local_name} -> {remote}", "role": "Quelle", "peer": remote, "connected": True}
        if inbound:
            remote = _slot_display_name(inbound)
            return {"label": f"{remote} -> {local_name}", "role": "Ziel", "peer": remote, "connected": True}
        if outbound:
            remote = _slot_display_name(outbound)
            return {"label": f"{local_name} -> {remote}", "role": "Quelle", "peer": remote, "connected": True}

    if istate.active and peer_label:
        return {"label": f"{local_name} -> {peer_label}", "role": "Quelle", "peer": peer_label, "connected": False}

    return {"label": "-", "role": "-", "peer": "-", "connected": False}


def build_status_snapshot():
    with istate.lock:
        summary = build_connection_summary()
        peers_cfg = list(istate.config.get("peers", []))
        local_device = (istate.config.get("device_name", "") or
                        os.environ.get("COMPUTERNAME", "")).strip() or "Unbekannt"
        peer_rows = []
        for p in peers_cfg:
            ident = peer_identity(p)
            _, conn = resolve_peer_connection(ident)
            inbound = conn.get("inbound") if isinstance(conn, dict) else None
            outbound = conn.get("outbound") if isinstance(conn, dict) else None
            connected = bool(inbound or outbound)
            remote_fwd = conn.get("remote_forwarding_active", False) if isinstance(conn, dict) else False
            remote_fwd_src = conn.get("remote_forwarding_source", "") if isinstance(conn, dict) else ""
            # Direction label: only show when forwarding is actually active.
            if istate.active and istate.active_peer == ident:
                link_label = f"{local_device} \u2192 {p['name']}"
                direction = "Quelle"
            elif remote_fwd and remote_fwd_src:
                link_label = f"{remote_fwd_src} \u2192 {local_device}"
                direction = "Ziel"
            elif connected:
                link_label = ""
                direction = ""
            else:
                link_label = ""
                direction = ""
            peer_rows.append({
                "name": p["name"],
                "host": p["host"],
                "port": p.get("port", 45781),
                "identity": ident,
                "selected": ident == istate.active_peer,
                "connected": connected,
                "connected_in": bool(inbound),
                "connected_out": bool(outbound),
                "direction": direction,
                "link_label": link_label,
                "peer_label": p["name"],
                "remote_forwarding_active": remote_fwd,
                "remote_forwarding_source": remote_fwd_src,
                "remote_os": conn.get("os") if isinstance(conn, dict) else None,
                "remote_version": conn.get("version") if isinstance(conn, dict) else None,
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
        # Any live peer connection (network-level), independent of forwarding.
        any_connected = any(r["connected"] for r in peer_rows)
        network_peer = next((r["peer_label"] for r in peer_rows if r["connected"]), "-")
        workers = worker_health()
        down = critical_workers_down()
        sess = session_info()
        return {
            "device_name": istate.config.get("device_name", ""),
            "device_id": istate.config.get("device_id", ""),
            "os": _backend.os_name,
            "capabilities": _backend.get_capabilities(),
            "app_version": _local_version["app_version"],
            "git_commit": _local_version["git_commit"],
            "git_branch": _local_version["git_branch"],
            "protocol_version": _local_version["protocol_version"],
            "runtime_started_at": _runtime_started_at,
            "running": not _shutdown_event.is_set(),
            "shutting_down": _shutdown_requested,
            "enabled": istate.enabled,
            "active": istate.active,
            "active_peer": istate.active_peer_label,
            "active_peer_identity": istate.active_peer,
            "hook_running": _hook_mgr.running,
            "mode": "forwarding" if istate.active else ("paused" if not istate.enabled else "standby"),
            # Worker health + runtime health (a dead critical worker is an error).
            "workers": workers,
            "critical_workers_down": down,
            "runtime_healthy": (len(down) == 0),
            # Event pipeline diagnostics (where do events get stuck?).
            "pipeline": pipeline_snapshot(),
            # Session context: Session 0 (service) cannot do interactive input.
            "session": sess,
            # Clearly SEPARATED state: network vs forwarding vs capture.
            "network_connected": any_connected,
            "network_peer": network_peer,
            "forwarding_active": istate.active,
            "forwarding_target": istate.active_peer_label if istate.active else None,
            "capture_active": bool(istate.active and _hook_mgr.running),
            "connection_label": summary["label"],
            "connection_role": summary["role"],
            "connection_peer": summary["peer"],
            "connection_active": summary["connected"],
            "capture_region": capture,
            "forwarding": _menu_summary(),
            "peers": peer_rows,
            "hotkeys": [
                {
                    "label": hk.label,
                    "display": hk.display(),
                    "action": hk.action,
                    "valid": rm.hotkey_is_valid(istate.config, {"action": hk.action}),
                }
                for hk in istate.hotkeys
            ],
        }


def apply_profile(name, activate=True):
    if not activate:
        return deactivate_forward("control")
    peers = istate.config.get("peers", [])
    match = next(
        (p for p in peers
         if p.get("name") == name or p.get("host") == name
         or p.get("device_id") == name or peer_identity(p) == name),
        None,
    )
    if not match:
        log("WARN", f"unknown profile requested: {name}")
        return False, f"Unknown profile: {name}"
    return _activate_forward_peer(match, source="control")


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
        elif typ == "send_synthetic":
            # Live-test helper: push synthetic input into the forward pipeline so
            # it is really sent to the peer and injected there (proves forwarding,
            # not a remote file-write command). Requires forwarding to be active.
            events = req.get("events", [])
            with istate.lock:
                active = istate.active
            if not active:
                send_msg(conn, {"type": "error", "error": "forwarding not active"})
            else:
                for ev in events:
                    istate.event_queue.put(ev)
                log("INFO", f"live-test: queued {len(events)} synthetic event(s)")
                send_msg(conn, {"type": "ok", "queued": len(events)})
        elif typ == "type_text":
            text = str(req.get("text", ""))
            with istate.lock:
                active = istate.active
            if not active:
                send_msg(conn, {"type": "error", "error": "forwarding not active"})
            else:
                istate.event_queue.put({"type": "type_text", "text": text})
                log("INFO", f"live-test: queued type_text len={len(text)}")
                send_msg(conn, {"type": "ok", "queued": len(text)})
        elif typ == "clip_list":
            ident = req.get("profile", "")
            items = _clip_mgr.list_items(ident) if ident else []
            send_msg(conn, {"type": "ok", "items": items,
                            "total_size": _clip_mgr.store(ident).total_size() if ident else 0})
        elif typ == "clip_capture":
            # Manual add / test hook: capture a text into a peer's store.
            ident = req.get("profile", "")
            text = str(req.get("text", ""))
            it = _clip_mgr.capture_text(ident, text) if ident else None
            send_msg(conn, {"type": "ok", "item": it})
        elif typ == "clip_capture_files":
            ident = req.get("profile", "")
            paths = req.get("paths", [])
            it = _clip_mgr.capture_files(ident, paths) if ident and paths else None
            send_msg(conn, {"type": "ok", "item": it})
        elif typ == "clip_get":
            ident = req.get("profile", "")
            item_id = req.get("item_id", "")
            kind = _clip_mgr.item_kind(ident, item_id) if ident else None
            if kind in (cbm.KIND_FILE, cbm.KIND_FILE_BATCH):
                dest_root = os.path.join(CLIPBOARD_ROOT, "temp", "incoming")
                paths = _clip_mgr.materialize_files(ident, item_id, dest_root)
                if paths:
                    global _clip_last_set_files
                    _clip_last_set_files = sorted(paths)
                    ok_set = clipboard_win.set_files(paths)
                    send_msg(conn, {"type": "ok", "set": bool(ok_set), "kind": kind,
                                    "count": len(paths)})
                else:
                    send_msg(conn, {"type": "error",
                                    "error": "file data not present (download/retry)"})
            else:
                text = _clip_mgr.get_text(ident, item_id) if ident else None
                if text is not None:
                    global _clip_last_set_text
                    _clip_last_set_text = text
                    ok_set = clipboard_win.set_text(text)
                    send_msg(conn, {"type": "ok", "set": bool(ok_set), "kind": "text"})
                else:
                    send_msg(conn, {"type": "error", "error": "no data (may need download)"})
        elif typ == "clip_delete":
            ident = req.get("profile", "")
            ok_del = _clip_mgr.delete_item(ident, req.get("item_id", "")) if ident else False
            send_msg(conn, {"type": "ok", "deleted": bool(ok_del)})
        elif typ == "clip_pin":
            ident = req.get("profile", "")
            ok_pin = _clip_mgr.set_pinned(ident, req.get("item_id", ""), bool(req.get("pinned", True))) if ident else False
            send_msg(conn, {"type": "ok", "pinned": bool(ok_pin)})
        elif typ == "clip_clear":
            ident = req.get("profile", "")
            if ident:
                _clip_mgr.clear(ident)
            send_msg(conn, {"type": "ok"})
        elif typ == "clip_request":
            ident = req.get("profile", "")
            ids = req.get("item_ids", [])
            if ident and ids:
                _clip_mgr.request_items(ident, ids, reason="manual_retry")
            send_msg(conn, {"type": "ok", "requested": len(ids)})
        elif typ == "clip_sync":
            ident = req.get("profile", "")
            if ident:
                _clip_mgr.send_manifest(ident)
            send_msg(conn, {"type": "ok"})
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
        try:
            inject(ev)
            pipe_inc("events_injected")
        except Exception as e:
            pipe_inc("inject_failed")
            log("ERROR", f"inject failed in inject_loop: {e!r}")


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
        active = istate.active
        label = istate.active_peer_label
        local = (istate.config.get("device_name", "") or os.environ.get("COMPUTERNAME", "")).strip() or "FlowShift"
        first_peer = next(
            (v.get("display_name") or v.get("host", "") for v in istate.peers.values() if isinstance(v, dict)),
            None,
        )
    if active and label:
        tip = f"FlowShift | {local} \u2192 {label}"
    else:
        tip = "FlowShift"
    _tray_nid.szTip = tip[:127]
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
        if lparam == WM_LBUTTONDBLCLK:
            open_gui()
        elif lparam == WM_RBUTTONUP:
            cmd = show_menu(hwnd)
            _handle_menu(cmd)
        return 0
    elif msg == WM_RELOAD_HOTKEYS:
        register_runtime_hotkeys(hwnd)
        update_tray()
        return 0
    elif msg == WM_APP_QUIT:
        # Runs on the main thread -> actually stops the GetMessageW loop.
        user32.PostQuitMessage(0)
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
            istate.active_peer_label = None
            try:
                with open(KILL_FILE, "w") as _f:
                    _f.write("1")
            except Exception:
                pass
            request_shutdown("kill-hotkey")
            return 0
        with istate.lock:
            enabled = istate.enabled
            hk = _registered_hotkeys.get(hk_id)
            active = istate.active
        if not enabled or hk is None:
            return 0
        if is_return_action(hk.action) and active:
            deactivate_forward("hotkey")
        elif is_forward_action(hk.action) and not active:
            activate_forward_action_async(hk.action, "hotkey")
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
        if istate.active:
            deactivate_forward("tray-menu")
        else:
            activate_first_forward_async("tray-menu")
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
            signal_main_quit()
            break
        if _shutdown_event.wait(1.0):
            break


_registered_hotkeys = {}  # hotkey_id -> HotkeyBinding (kill switch id maps to None)


def register_runtime_hotkeys(hwnd):
    """(Re)register all OS-level hotkeys from the current config. Window thread only."""
    unregister_runtime_hotkeys(hwnd)
    for i, hk in enumerate(istate.hotkeys):
        hid = ID_HK_BASE + i
        # Never register an invalid hotkey (unresolved forward target or no key).
        reason = rm.hotkey_registration_error(
            istate.config, {"action": hk.action, "key": hk.key}
        )
        if reason:
            log("WARN", f"skipping invalid hotkey label={hk.label!r} "
                        f"hotkey={hk.display()} action={hk.action} reason={reason}")
            continue
        rhk_mods = tray_mods_to_rhk(hk.mods)
        ok = user32.RegisterHotKey(hwnd, hid, rhk_mods, hk.key)
        if ok:
            _registered_hotkeys[hid] = hk
            log("INFO", f"registered hotkey id={hid} {hk.display()} action={hk.action}")
        else:
            log("ERROR", f"RegisterHotKey failed id={hid} {hk.display()} "
                         f"action={hk.action} err={kernel32.GetLastError()}")
    # Kill switch hotkey (Ctrl+Alt+Shift+Win+F12)
    if user32.RegisterHotKey(hwnd, ID_HK_KILL, RHK_CTRL | RHK_ALT | RHK_SHIFT | RHK_WIN, KILL_VK):
        _registered_hotkeys[ID_HK_KILL] = None
    else:
        log("ERROR", f"RegisterHotKey failed for kill switch err={kernel32.GetLastError()}")


def unregister_runtime_hotkeys(hwnd):
    for hid in list(_registered_hotkeys.keys()):
        try:
            user32.UnregisterHotKey(hwnd, hid)
        except Exception:
            pass
        _registered_hotkeys.pop(hid, None)


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

    # Warn loudly if we are in Session 0 (a service): interactive input hooks and
    # SendInput will not reach the user's desktop there.
    sess = session_info()
    if sess.get("is_service_session"):
        log("ERROR", "CRITICAL: FlowShift is running in Session 0 (service session); "
                     "interactive input capture/injection will NOT work. "
                     "Run the runtime in the interactive user session instead.")
    else:
        log("INFO", f"session_id={sess.get('session_id')} interactive={sess.get('interactive')} "
                    f"user={sess.get('username')}")

    # Supervised workers: a crash is logged (with traceback) and marked failed.
    start_worker("discovery_thread", discovery_thread)
    start_worker("network_thread", network_thread)
    start_worker("connect_to_peers", connect_to_peers)
    start_worker("local_control_thread", local_control_thread)
    start_worker("forward_loop", forward_loop)
    start_worker("inject_loop", inject_loop)
    start_worker("watchdog", watchdog)
    start_worker("clipboard_watcher", clipboard_watcher)

    # Register activation/deactivation hotkeys via RegisterHotKey (window thread).
    register_runtime_hotkeys(_hwnd)

    msg = MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

    # Unregister all hotkeys
    unregister_runtime_hotkeys(_hwnd)
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
