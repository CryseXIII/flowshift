use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum Message {
    Hello {
        device_id: String,
        display_name: String,
        os: String,
    },
    RoutingUpdate {
        sender: String,
        table: super::routing::RoutingTable,
    },
    InputEvent {
        sender: String,
        events: Vec<InputEventKind>,
    },
    StreamAnnounce {
        sender: String,
        monitor: u8,
        sdp: String,
        ice_candidates: Vec<String>,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum InputEventKind {
    MouseMove { x: i32, y: i32 },
    MouseButton { button: u8, down: bool },
    MouseWheel { delta: i32, horizontal: bool },
    KeyDown { code: u16, modifiers: u8 },
    KeyUp { code: u16, modifiers: u8 },
}
