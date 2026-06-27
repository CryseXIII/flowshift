"""Standalone menu test: create & show menu via #32770 window, insert items, log everything."""
import ctypes, ctypes.wintypes, sys, os, time, threading, uuid

u = ctypes.windll.user32
k = ctypes.windll.kernel32
s = ctypes.windll.shell32

k.GetModuleHandleW.restype = ctypes.c_void_p
hInst = k.GetModuleHandleW(None)

# Configure APIs
for fn, args, restype in [
    (u.CreateWindowExW, [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
    (u.SetWindowLongPtrW, [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p], ctypes.c_void_p),
    (u.DefWindowProcW, [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_long], ctypes.c_long),
    (u.CreatePopupMenu, [], ctypes.c_void_p),
    (u.DestroyMenu, [ctypes.c_void_p], ctypes.c_int),
    (u.TrackPopupMenu, [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
    (u.SetForegroundWindow, [ctypes.c_void_p], ctypes.c_int),
    (u.GetCursorPos, [ctypes.c_void_p], ctypes.c_int),
    (u.PostMessageW, [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_long], ctypes.c_int),
    (u.SendMessageW, [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_long], ctypes.c_long),
    (u.PostQuitMessage, [ctypes.c_int], None),
    (u.LoadIconW, [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
    (u.GetMenuStringW, [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint], ctypes.c_int),
    (u.DestroyWindow, [ctypes.c_void_p], ctypes.c_int),
]:
    if args:
        fn.argtypes = args
    if restype is not None:
        fn.restype = restype

class MII(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint), ("fMask", ctypes.c_uint),
        ("fType", ctypes.c_uint), ("fState", ctypes.c_uint),
        ("wID", ctypes.c_uint), ("hSubMenu", ctypes.c_void_p),
        ("hbmpChecked", ctypes.c_void_p), ("hbmpUnchecked", ctypes.c_void_p),
        ("dwItemData", ctypes.c_size_t), ("dwTypeData", ctypes.c_wchar_p),
        ("cch", ctypes.c_uint), ("hbmpItem", ctypes.c_void_p),
    ]

MIIM_STRING = 0x40
MIIM_ID = 0x02

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_long)

WM_TRAYICON = 0x8001
WM_RBUTTONUP = 0x0205
WM_DESTROY = 0x0002

def log(msg):
    with open(os.path.join(os.environ.get("TEMP", "."), "flowshift_mt.txt"), "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()

@WNDPROC
def wp(hwnd, msg, wparam, lparam):
    if msg == WM_TRAYICON:
        if lparam == WM_RBUTTONUP:
            log("=== RCLICK ===")
            hm = u.CreatePopupMenu()
            if not hm:
                log("  CreatePopupMenu FAILED")
                return 0
            log("  hmenu = " + str(hm))
            items = [("Test Item 1", 1001), ("Test Item 2", 1002), ("Exit", 1003)]
            for i, (text, uid) in enumerate(items):
                mii = MII()
                mii.cbSize = ctypes.sizeof(MII)
                mii.fMask = MIIM_STRING | MIIM_ID
                mii.fType = 0  # MFT_STRING
                mii.wID = uid
                # Use explicit create_unicode_buffer to ensure proper wchar_t encoding
                wbuf = ctypes.create_unicode_buffer(text)
                mii.dwTypeData = ctypes.cast(wbuf, ctypes.c_wchar_p).value if hasattr(ctypes.cast(wbuf, ctypes.c_wchar_p), 'value') else wbuf
                mii.cch = len(text)
                r = u.InsertMenuItemW(hm, i, 1, ctypes.byref(mii))
                log(f"  InsertMenuItemW i={i} text='{text}' uid={uid} r={r}")
                # Verify the text was stored by using GetMenuStringW
                buf = ctypes.create_unicode_buffer(256)
                got = u.GetMenuStringW(hm, i, buf, 255, 0x0400)  # MF_BYPOSITION
                log(f"  GetMenuStringW: {got} -> '{buf.value}'")
            pt = ctypes.wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            fg = u.SetForegroundWindow(hwnd)
            log(f"  SetForegroundWindow={fg}")
            u.PostMessageW(hwnd, 0, 0, 0)
            log(f"  Before TrackPopupMenu at ({pt.x},{pt.y})")
            cmd = u.TrackPopupMenu(hm, 0x0120, pt.x, pt.y, 0, hwnd, None)
            log(f"  TrackPopupMenu returned {cmd}")
            u.DestroyMenu(hm)
            if cmd == 1003:
                u.PostQuitMessage(0)
        return 0
    elif msg == WM_DESTROY:
        u.PostQuitMessage(0)
        return 0
    elif msg in (0x0116, 0x0117):  # WM_INITMENU, WM_INITMENUPOPUP - forward to DefDlgProcW
        if _orig_wndproc:
            try:
                r = u.CallWindowProcW(_orig_wndproc, hwnd, msg, wparam, lparam)
                log(f"  forwarded WM_INITMENU to dialog proc -> {r}")
                return r
            except Exception as e:
                log(f"  CallWindowProcW failed: {e}")
        return 0
    else:
        log(f"  msg={msg:#x} wp={wparam:#x} lp={lparam:#x}")
    return 0

hwnd = u.CreateWindowExW(0x80, "#32770", "MenuTest", 0x80000000, -32000, -32000, 0, 0, None, None, hInst, None)
log(f"hwnd={hwnd}")
assert hwnd, "CreateWindowExW failed"

old = u.SetWindowLongPtrW(hwnd, -4, ctypes.cast(wp, ctypes.c_void_p))
log(f"SetWindowLongPtrW old={old:#x}")

# Add tray icon
class NID(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p), ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint), ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", ctypes.c_void_p), ("szTip", ctypes.c_wchar * 128),
    ]
nid = NID()
nid.cbSize = ctypes.sizeof(NID)
nid.hwnd = hwnd
nid.uID = 1
nid.uFlags = 1 | 2 | 4
nid.uCallbackMessage = WM_TRAYICON
nid.hIcon = u.LoadIconW(None, ctypes.c_void_p(0x7F00))
nid.szTip = "MenuTest"
s.Shell_NotifyIconW.argtypes = [ctypes.c_int, ctypes.c_void_p]
s.Shell_NotifyIconW.restype = ctypes.c_int
r = s.Shell_NotifyIconW(0, ctypes.byref(nid))
log(f"Shell_NotifyIconW NIM_ADD={r}")

# Simulate right-click after 2s
def sim():
    time.sleep(2)
    log("=== SIMULATING WM_TRAYICON ===")
    u.SendMessageW(hwnd, WM_TRAYICON, 1, WM_RBUTTONUP)
    log("=== SIM DONE ===")
    # After menu dismissed, exit
    u.PostQuitMessage(0)

threading.Thread(target=sim, daemon=True).start()

msg = ctypes.wintypes.MSG()
log("=== MSG LOOP START ===")
while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
    u.TranslateMessage(ctypes.byref(msg))
    u.DispatchMessageW(ctypes.byref(msg))

s.Shell_NotifyIconW(2, ctypes.byref(nid))
u.DestroyWindow(hwnd)
log("=== DONE ===")
