use serde::{Deserialize, Serialize};
use std::path::PathBuf;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Peer {
    pub name: String,
    pub host: String,
    pub port: u16,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HotkeyDef {
    pub modifiers: Vec<String>,
    pub key: String,
    pub target: Option<String>,
    pub mode: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub device_name: String,
    pub device_id: String,
    pub port: u16,
    pub hotkeys: Vec<HotkeyDef>,
    pub peers: Vec<Peer>,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            device_name: std::env::var("COMPUTERNAME")
                .unwrap_or_else(|_| "unknown".into()),
            device_id: uuid::Uuid::new_v4().to_string(),
            port: 45781,
            hotkeys: vec![
                HotkeyDef {
                    modifiers: vec!["Ctrl".into(), "Alt".into()],
                    key: "1".into(),
                    target: None,
                    mode: "forward".into(),
                },
                HotkeyDef {
                    modifiers: vec!["Ctrl".into(), "Alt".into()],
                    key: "0".into(),
                    target: None,
                    mode: "local".into(),
                },
            ],
            peers: vec![],
        }
    }
}

impl Config {
    pub fn path() -> PathBuf {
        let base = std::env::var("APPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|_| {
                std::env::var("HOME")
                    .map(|h| PathBuf::from(h).join(".config"))
                    .unwrap_or_else(|_| PathBuf::from("."))
            });
        base.join("flowshift").join("config.json")
    }

    pub fn load() -> anyhow::Result<Self> {
        let p = Self::path();
        if p.exists() {
            let data = std::fs::read_to_string(&p)?;
            Ok(serde_json::from_str(&data)?)
        } else {
            let cfg = Config::default();
            cfg.save()?;
            Ok(cfg)
        }
    }

    pub fn save(&self) -> anyhow::Result<()> {
        let p = Self::path();
        if let Some(parent) = p.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let data = serde_json::to_string_pretty(self)?;
        std::fs::write(&p, data)?;
        Ok(())
    }

    pub fn find_peer_index(&self, name: &str) -> Option<usize> {
        self.peers.iter().position(|p| p.name == name)
    }

    pub fn match_hotkey_mods(&self, mods: u8, vk: u16) -> Option<&HotkeyDef> {
        let key = match vk as u32 {
            0x30 => "0",
            0x31 => "1",
            0x32 => "2",
            0x33 => "3",
            0x34 => "4",
            0x35 => "5",
            0x36 => "6",
            0x37 => "7",
            0x38 => "8",
            0x39 => "9",
            _ => return None,
        };
        let ctrl = (mods & 0x01) != 0;
        let alt = (mods & 0x04) != 0;
        if !ctrl || !alt {
            return None;
        }
        self.hotkeys.iter().find(|h| h.key == key)
    }
}
