use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RoutingEntry {
    pub display_id: String,
    pub source_id: String,
    pub source_monitor: u8,
    pub mode: StreamMode,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum StreamMode {
    Extended,
    Duplicated,
    InputOnly,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RoutingTable {
    pub entries: Vec<RoutingEntry>,
    pub input_target: String,
    pub input_keyboard_only: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Profile {
    pub name: String,
    pub hotkey: HotkeyBinding,
    pub input: InputConfig,
    pub display_routing: Vec<RoutingEntry>,
    pub audio: AudioConfig,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HotkeyBinding {
    pub modifiers: Vec<String>,
    pub key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InputConfig {
    pub target: String,
    pub keyboard_only: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AudioConfig {
    pub source: String,
    pub enabled: bool,
}
