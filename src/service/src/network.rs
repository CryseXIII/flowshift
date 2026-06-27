use std::collections::HashMap;
use std::sync::Arc;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Mutex;

use flowshift_shared::protocol::{InputEventKind, Message};

pub type SharedState = Arc<Mutex<HashMap<String, PeerConnection>>>;

#[derive(Debug)]
pub struct PeerConnection {
    pub name: String,
    pub writer: tokio::sync::Mutex<tokio::io::WriteHalf<TcpStream>>,
}

pub async fn start_server(
    port: u16,
    device_name: String,
    device_id: String,
    peers: SharedState,
) -> anyhow::Result<()> {
    let addr = format!("0.0.0.0:{}", port);
    let listener = TcpListener::bind(&addr).await?;
    tracing::info!("TCP server listening on {}", addr);

    loop {
        let (stream, addr) = listener.accept().await?;
        tracing::debug!("incoming connection from {}", addr);

        let peers = peers.clone();
        let device_name = device_name.clone();
        let device_id = device_id.clone();

        tokio::spawn(async move {
            if let Err(e) =
                handle_connection(stream, &device_name, &device_id, peers).await
            {
                tracing::warn!("connection error: {}", e);
            }
        });
    }
}

async fn handle_connection(
    mut stream: TcpStream,
    local_name: &str,
    local_id: &str,
    peers: SharedState,
) -> anyhow::Result<()> {
    let (reader, mut writer) = stream.split();

    // Send hello
    let hello = Message::Hello {
        device_id: local_id.to_string(),
        display_name: local_name.to_string(),
        os: "windows".into(),
    };
    send_message(&mut writer, &hello).await?;

    // Read peer's hello
    let (peer_name, peer_id) = match read_message(reader).await? {
        Message::Hello {
            device_id,
            display_name,
            ..
        } => (display_name, device_id),
        _ => anyhow::bail!("expected Hello message"),
    };

    tracing::info!("peer connected: {} ({})", peer_name, peer_id);

    {
        let mut map = peers.lock().await;
        map.insert(
            peer_name.clone(),
            PeerConnection {
                name: peer_name.clone(),
                writer: tokio::sync::Mutex::new(writer),
            },
        );
    }

    Ok(())
}

pub async fn send_event(
    peers: &SharedState,
    peer_name: &str,
    event: &InputEventKind,
) -> anyhow::Result<()> {
    let mut map = peers.lock().await;
    if let Some(conn) = map.get(peer_name) {
        let msg = Message::InputEvent {
            sender: String::new(),
            events: vec![event.clone()],
        };
        let data = serde_json::to_vec(&msg)?;
        let len = (data.len() as u32).to_be_bytes();

        let mut writer = conn.writer.lock().await;
        writer.write_all(&len).await?;
        writer.write_all(&data).await?;
    }
    Ok(())
}

pub async fn connect_to_peers(
    peers_list: &[crate::config::Peer],
    local_name: &str,
    local_id: &str,
    device_name: String,
    device_id: String,
) -> SharedState {
    let state: SharedState = Arc::new(Mutex::new(HashMap::new()));

    for peer in peers_list {
        let addr = format!("{}:{}", peer.host, peer.port);
        let state = state.clone();
        let dname = device_name.clone();
        let did = device_id.clone();
        let pname = peer.name.clone();

        tokio::spawn(async move {
            loop {
                match TcpStream::connect(&addr).await {
                    Ok(stream) => {
                        tracing::info!("connected to peer {} at {}", pname, addr);
                        if let Err(e) =
                            handle_outgoing(stream, &dname, &did, state, &pname).await
                        {
                            tracing::warn!("peer {} disconnected: {}", pname, e);
                        }
                    }
                    Err(e) => {
                        tracing::debug!("could not connect to {}: {}", addr, e);
                    }
                }
                tokio::time::sleep(tokio::time::Duration::from_secs(5)).await;
            }
        });
    }

    state
}

async fn handle_outgoing(
    stream: TcpStream,
    local_name: &str,
    local_id: &str,
    peers: SharedState,
    peer_name: &str,
) -> anyhow::Result<()> {
    let (reader, writer) = stream.into_split();

    // Send hello
    let hello = Message::Hello {
        device_id: local_id.to_string(),
        display_name: local_name.to_string(),
        os: "windows".into(),
    };
    let mut w = tokio::sync::Mutex::new(writer);
    send_message(&mut w, &hello).await?;

    // Store connection
    {
        let mut map = peers.lock().await;
        map.insert(
            peer_name.to_string(),
            PeerConnection {
                name: peer_name.to_string(),
                writer: w,
            },
        );
    }

    // Read incoming messages
    read_loop(reader, peers).await
}

async fn read_message(
    mut reader: tokio::io::ReadHalf<TcpStream>,
) -> anyhow::Result<Message> {
    let mut len_buf = [0u8; 4];
    reader.read_exact(&mut len_buf).await?;
    let len = u32::from_be_bytes(len_buf) as usize;

    let mut buf = vec![0u8; len];
    reader.read_exact(&mut buf).await?;

    Ok(serde_json::from_slice(&buf)?)
}

async fn read_loop(
    mut reader: tokio::io::ReadHalf<TcpStream>,
    peers: SharedState,
) -> anyhow::Result<()> {
    loop {
        let mut len_buf = [0u8; 4];
        reader.read_exact(&mut len_buf).await?;
        let len = u32::from_be_bytes(len_buf) as usize;

        let mut buf = vec![0u8; len];
        reader.read_exact(&mut buf).await?;

        let msg: Message = serde_json::from_slice(&buf)?;
        match msg {
            Message::InputEvent { events, .. } => {
                for event in events {
                    crate::inject::inject(&event);
                }
            }
            Message::Hello { .. } => {
                tracing::debug!("got Hello from peer (already connected)");
            }
            _ => {}
        }
    }
}

async fn send_message(
    writer: &mut tokio::sync::Mutex<tokio::io::WriteHalf<TcpStream>>,
    msg: &Message,
) -> anyhow::Result<()> {
    let data = serde_json::to_vec(msg)?;
    let len = (data.len() as u32).to_be_bytes();
    let mut w = writer.lock().await;
    w.write_all(&len).await?;
    w.write_all(&data).await?;
    Ok(())
}
