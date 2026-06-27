mod config;
mod hotkey;
mod inject;
mod network;

use std::sync::Arc;
use std::sync::atomic::Ordering;

use tokio::sync::mpsc;

use flowshift_shared::protocol::InputEventKind;

mod hooks;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "flowshift_service=info".into()),
        )
        .init();

    let cfg = config::Config::load()?;
    tracing::info!(
        "FlowShift Service v{} - {} ({})",
        env!("CARGO_PKG_VERSION"),
        cfg.device_name,
        cfg.device_id
    );

    if cfg.peers.is_empty() {
        tracing::warn!("no peers configured. Edit config at: {:?}", config::Config::path());
    }

    for peer in &cfg.peers {
        tracing::info!("  peer: {} -> {}:{}", peer.name, peer.host, peer.port);
    }

    // Channel: hooks thread -> main event loop
    let (event_tx, mut event_rx) = mpsc::unbounded_channel::<InputEventKind>();

    // Start input hooks
    let hooks = hooks::Hooks::start(event_tx)?;
    tracing::info!("input hooks installed");

    // Start TCP server + connect to peers
    let peers = network::connect_to_peers(
        &cfg.peers,
        &cfg.device_name,
        &cfg.device_id,
        cfg.device_name.clone(),
        cfg.device_id.clone(),
    )
    .await;

    let server_handle = tokio::spawn(async move {
        if let Err(e) = network::start_server(
            cfg.port,
            cfg.device_name.clone(),
            cfg.device_id.clone(),
            peers.clone(),
        )
        .await
        {
            tracing::error!("server error: {}", e);
        }
    });

    tracing::info!("ready. Press Ctrl+Alt+N to forward to peer N, Ctrl+Alt+0 to return");

    let mut active_peer: Option<String> = None;

    while let Some(event) = event_rx.recv().await {
        match hotkey::match_hotkey(&event, &cfg) {
            hotkey::HotkeyAction::Forward { peer_name, .. } => {
                hooks.set_active(true);
                active_peer = Some(peer_name.clone());
                tracing::info!(">> forwarding input to: {}", peer_name);
            }
            hotkey::HotkeyAction::ReturnLocal => {
                hooks.set_active(false);
                active_peer = None;
                tracing::info!("<< returned to local input");
            }
            hotkey::HotkeyAction::None => {
                // Regular input event while forwarding
                if let Some(ref peer) = active_peer {
                    if let Err(e) = network::send_event(&peers, peer, &event).await {
                        tracing::warn!("failed to send event to {}: {}", peer, e);
                    }
                }
            }
        }
    }

    server_handle.abort();
    Ok(())
}
