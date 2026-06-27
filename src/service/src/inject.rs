use windows::Win32::Foundation::*;
use windows::Win32::UI::Input::KeyboardAndMouse::*;

use flowshift_shared::protocol::InputEventKind;

fn send_key(vk: u16, down: bool) {
    unsafe {
        let mut input = INPUT::default();
        input.r#type = INPUT_KEYBOARD;
        input.Anonymous.ki.wVk = vk as u16;
        input.Anonymous.ki.wScan = 0;
        input.Anonymous.ki.dwFlags = if down {
            KEYEVENTF(0)
        } else {
            KEYEVENTF_KEYUP
        };
        input.Anonymous.ki.time = 0;
        input.Anonymous.ki.dwExtraInfo = 0;
        SendInput(&[input], std::mem::size_of::<INPUT>() as i32);
    }
}

fn send_mouse_move(x: i32, y: i32) {
    unsafe {
        let mut input = INPUT::default();
        input.r#type = INPUT_MOUSE;
        input.Anonymous.mi.dx = x;
        input.Anonymous.mi.dy = y;
        input.Anonymous.mi.dwFlags = MOUSE_EVENT_FLAGS(MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE);
        input.Anonymous.mi.time = 0;
        input.Anonymous.mi.dwExtraInfo = 0;
        SendInput(&[input], std::mem::size_of::<INPUT>() as i32);
    }
}

fn send_mouse_button(button: u8, down: bool) {
    let (flag_down, flag_up) = match button {
        0 => (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        1 => (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        2 => (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        _ => return,
    };

    unsafe {
        let mut input = INPUT::default();
        input.r#type = INPUT_MOUSE;
        input.Anonymous.mi.dwFlags = if down { flag_down } else { flag_up };
        input.Anonymous.mi.time = 0;
        input.Anonymous.mi.dwExtraInfo = 0;
        SendInput(&[input], std::mem::size_of::<INPUT>() as i32);
    }
}

fn send_mouse_wheel(delta: i32) {
    unsafe {
        let mut input = INPUT::default();
        input.r#type = INPUT_MOUSE;
        input.Anonymous.mi.mouseData = delta as u32;
        input.Anonymous.mi.dwFlags = MOUSEEVENTF_WHEEL;
        input.Anonymous.mi.time = 0;
        input.Anonymous.mi.dwExtraInfo = 0;
        SendInput(&[input], std::mem::size_of::<INPUT>() as i32);
    }
}

pub fn inject(event: &InputEventKind) {
    match *event {
        InputEventKind::KeyDown { code, .. } => {
            send_key(code, true);
        }
        InputEventKind::KeyUp { code, .. } => {
            send_key(code, false);
        }
        InputEventKind::MouseMove { x, y } => {
            send_mouse_move(x, y);
        }
        InputEventKind::MouseButton { button, down } => {
            send_mouse_button(button, down);
        }
        InputEventKind::MouseWheel { delta, .. } => {
            send_mouse_wheel(delta);
        }
    }
}
