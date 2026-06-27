use crate::config::Config;
use flowshift_shared::protocol::InputEventKind;

#[derive(Debug, Clone)]
pub enum HotkeyAction {
    Forward { peer_name: String, peer_idx: usize },
    ReturnLocal,
    None,
}

pub fn match_hotkey(event: &InputEventKind, config: &Config) -> HotkeyAction {
    let (vk, modifiers) = match *event {
        InputEventKind::KeyDown { code, modifiers } => (code, modifiers),
        _ => return HotkeyAction::None,
    };

    let ctrl = (modifiers & 0x01) != 0;
    let alt = (modifiers & 0x04) != 0;
    if !ctrl || !alt {
        return HotkeyAction::None;
    }

    match vk as u32 {
        0x30 => HotkeyAction::ReturnLocal,
        0x31..=0x39 => {
            let idx = (vk as usize) - 0x31;
            if idx < config.peers.len() {
                HotkeyAction::Forward {
                    peer_name: config.peers[idx].name.clone(),
                    peer_idx: idx,
                }
            } else {
                HotkeyAction::None
            }
        }
        _ => HotkeyAction::None,
    }
}
