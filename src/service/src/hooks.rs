use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use tokio::sync::mpsc;
use windows::Win32::Foundation::*;
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Input::KeyboardAndMouse::*;
use windows::Win32::UI::WindowsAndMessaging::*;

use flowshift_shared::protocol::InputEventKind;

static HOOK_TX: std::sync::OnceLock<mpsc::UnboundedSender<InputEventKind>> =
    std::sync::OnceLock::new();
static HOOK_ACTIVE: std::sync::OnceLock<Arc<AtomicBool>> = std::sync::OnceLock::new();
static HOOK_EXIT_FLAG: std::sync::OnceLock<Arc<AtomicBool>> = std::sync::OnceLock::new();
static HOOK_MODS: std::sync::OnceLock<Mutex<u8>> = std::sync::OnceLock::new();

const M_CTRL: u8 = 0x01;
const M_SHIFT: u8 = 0x02;
const M_ALT: u8 = 0x04;
const M_WIN: u8 = 0x08;

fn vk_to_mod_bit(vk: u16) -> Option<u8> {
    match vk as u32 {
        VK_CONTROL | VK_LCONTROL | VK_RCONTROL => Some(M_CTRL),
        VK_SHIFT | VK_LSHIFT | VK_RSHIFT => Some(M_SHIFT),
        VK_MENU | VK_LMENU | VK_RMENU => Some(M_ALT),
        VK_LWIN | VK_RWIN => Some(M_WIN),
        _ => None,
    }
}

fn update_modifiers(vk: u16, down: bool) {
    if let Some(bit) = vk_to_mod_bit(vk) {
        if let Some(mods) = HOOK_MODS.get() {
            let mut m = mods.lock().unwrap();
            if down {
                *m |= bit;
            } else {
                *m &= !bit;
            }
        }
    }
}

fn current_mods() -> u8 {
    HOOK_MODS
        .get()
        .map(|m| *m.lock().unwrap())
        .unwrap_or(0)
}

fn is_exit_hotkey(vk: u16) -> bool {
    let mods = current_mods();
    mods & (M_CTRL | M_ALT) == (M_CTRL | M_ALT) && vk as u32 == 0x30 // '0'
}

fn is_forward_hotkey(vk: u16) -> bool {
    let mods = current_mods();
    mods & (M_CTRL | M_ALT) == (M_CTRL | M_ALT) && {
        let c = vk;
        (0x31..=0x39).contains(&(c as u32))
    }
}

unsafe extern "system" fn keyboard_proc(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code >= 0 {
        let kb = &*(lparam.0 as *const KBDLLHOOKSTRUCT);
        let vk = kb.vkCode as u16;
        let down = matches!(wparam.0 as u32, WM_KEYDOWN | WM_SYSKEYDOWN);

        update_modifiers(vk, down);

        let active = HOOK_ACTIVE
            .get()
            .is_some_and(|a| a.load(Ordering::Relaxed));

        if active && is_exit_hotkey(vk) && down {
            if let Some(flag) = HOOK_EXIT_FLAG.get() {
                flag.store(true, Ordering::Relaxed);
            }
            return LRESULT(1);
        }

        let is_hotkey = is_forward_hotkey(vk) || is_exit_hotkey(vk);

        if active && !is_hotkey {
            let event = InputEventKind::KeyDown {
                code: vk,
                modifiers: current_mods(),
            };
            if let Some(tx) = HOOK_TX.get() {
                let _ = tx.send(event);
            }
            return LRESULT(1);
        }

        if is_hotkey && down && !active {
            if let Some(tx) = HOOK_TX.get() {
                let _ = tx.send(InputEventKind::KeyDown {
                    code: vk,
                    modifiers: current_mods(),
                });
            }
            return LRESULT(1);
        }
    }
    CallNextHookEx(HHOOK::default(), code, wparam, lparam)
}

unsafe extern "system" fn mouse_proc(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code >= 0 {
        let active = HOOK_ACTIVE
            .get()
            .is_some_and(|a| a.load(Ordering::Relaxed));

        if active {
            let ms = &*(lparam.0 as *const MSLLHOOKSTRUCT);
            let wm = wparam.0 as u32;

            let event = match wm {
                WM_MOUSEMOVE => Some(InputEventKind::MouseMove {
                    x: ms.pt.x,
                    y: ms.pt.y,
                }),
                WM_LBUTTONDOWN => Some(InputEventKind::MouseButton {
                    button: 0,
                    down: true,
                }),
                WM_LBUTTONUP => Some(InputEventKind::MouseButton {
                    button: 0,
                    down: false,
                }),
                WM_RBUTTONDOWN => Some(InputEventKind::MouseButton {
                    button: 1,
                    down: true,
                }),
                WM_RBUTTONUP => Some(InputEventKind::MouseButton {
                    button: 1,
                    down: false,
                }),
                WM_MBUTTONDOWN => Some(InputEventKind::MouseButton {
                    button: 2,
                    down: true,
                }),
                WM_MBUTTONUP => Some(InputEventKind::MouseButton {
                    button: 2,
                    down: false,
                }),
                WM_MOUSEWHEEL => {
                    let delta = ((ms.mouseData >> 16) as i16) as i32;
                    Some(InputEventKind::MouseWheel {
                        delta,
                        horizontal: false,
                    })
                }
                _ => None,
            };

            if let Some(ev) = event {
                if let Some(tx) = HOOK_TX.get() {
                    let _ = tx.send(ev);
                }
                return LRESULT(1);
            }
        }
    }
    CallNextHookEx(HHOOK::default(), code, wparam, lparam)
}

pub struct Hooks {
    active: Arc<AtomicBool>,
    exit_flag: Arc<AtomicBool>,
    thread_id: u32,
    join_handle: Option<std::thread::JoinHandle<()>>,
}

impl Hooks {
    pub fn start(
        tx: mpsc::UnboundedSender<InputEventKind>,
    ) -> anyhow::Result<Self> {
        let active = Arc::new(AtomicBool::new(false));
        let exit_flag = Arc::new(AtomicBool::new(false));

        HOOK_TX
            .set(tx)
            .map_err(|_| anyhow::anyhow!("hooks already started"))?;
        HOOK_ACTIVE
            .set(active.clone())
            .map_err(|_| anyhow::anyhow!("hooks already started"))?;
        HOOK_EXIT_FLAG
            .set(exit_flag.clone())
            .map_err(|_| anyhow::anyhow!("hooks already started"))?;
        HOOK_MODS
            .set(Mutex::new(0))
            .map_err(|_| anyhow::anyhow!("hooks already started"))?;

        let thread_id = std::sync::Arc::new(std::sync::Mutex::new(0u32));
        let thread_id_clone = thread_id.clone();

        let join_handle = std::thread::Builder::new()
            .name("flowshift-hooks".into())
            .spawn(move || unsafe {
                let tid = windows::Win32::System::Threading::GetCurrentThreadId();
                *thread_id_clone.lock().unwrap() = tid;

                let hmod = GetModuleHandleW(None).unwrap_or_default();

                let kb_hook = SetWindowsHookExW(
                    WH_KEYBOARD_LL,
                    Some(keyboard_proc),
                    hmod,
                    0,
                )
                .expect("SetWindowsHookExW(WH_KEYBOARD_LL) failed");

                let ms_hook = SetWindowsHookExW(
                    WH_MOUSE_LL,
                    Some(mouse_proc),
                    hmod,
                    0,
                )
                .expect("SetWindowsHookExW(WH_MOUSE_LL) failed");

                tracing::info!("input hooks installed");

                let mut msg = MSG::default();
                while GetMessageW(&mut msg, None, 0, 0).as_bool() {
                    TranslateMessage(&msg);
                    DispatchMessageW(&msg);
                }

                let _ = UnhookWindowsHookEx(kb_hook);
                let _ = UnhookWindowsHookEx(ms_hook);

                tracing::info!("hooks thread exiting");
            })?;

        let tid = *thread_id.lock().unwrap();

        Ok(Self {
            active,
            exit_flag,
            thread_id: tid,
            join_handle: Some(join_handle),
        })
    }

    pub fn set_active(&self, active: bool) {
        self.active.store(active, Ordering::Relaxed);
    }

    pub fn is_active(&self) -> bool {
        self.active.load(Ordering::Relaxed)
    }

    pub fn check_exit(&self) -> bool {
        self.exit_flag.swap(false, Ordering::Relaxed)
    }
}

impl Drop for Hooks {
    fn drop(&mut self) {
        if self.thread_id != 0 {
            unsafe {
                let _ = PostThreadMessageW(
                    self.thread_id,
                    WM_QUIT,
                    WPARAM(0),
                    LPARAM(0),
                );
            }
        }
        if let Some(handle) = self.join_handle.take() {
            let _ = handle.join();
        }
    }
}
