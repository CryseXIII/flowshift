# FlowShift Overlay Architecture

## Status

Architecture decision for Phase 1. The Clipboard Overlay and Command Wheel feature implementations remain future work.

## Requirements

The shared overlay infrastructure must provide a preloaded React UI in a separate process. The overlay process must be frameless, topmost, cursor-positioned, reusable across show/hide cycles, DPI-aware, and multi-monitor aware. Its failure must not stop input forwarding, peer networking, injection, clipboard workers, or the main runtime.

The productive runtime remains `src/python/tray.py --tray`. Rust remains experimental and is not part of the productive path.

## Options considered

### Tkinter

Rejected for new overlay work. It cannot host the React/Vite frontend and would extend the legacy UI technology that the overlay is intended to replace.

### Browser or Edge application window

Rejected as the host. It can render React without a new Python dependency, but it does not provide reliable frameless, hidden-preloaded, topmost lifecycle control or a clean native show/hide API.

### Electron

Rejected because it would ship a second browser runtime and add disproportionate package size and process overhead while Windows already provides WebView2.

### Tauri or a custom native WebView2 host

Technically capable, including per-pixel transparency. Rejected for Phase 1 because it would introduce a new native build toolchain and a separately maintained binary. A custom WebView2 composition host would also add substantial COM, DirectComposition, DPI, and input-lifecycle complexity.

### Python pywebview with WebView2

Selected. `pywebview` runs its UI loop in a dedicated process, uses the installed Windows WebView2 runtime, renders the existing React/Vite output, and provides hidden startup, frameless windows, always-on-top, move, show, hide, and JavaScript evaluation. It preserves the Python productive architecture and requires no Rust migration.

Windows pywebview does not promise arbitrary per-pixel transparency. Phase 1 therefore uses a visually suitable frameless overlay surface rather than claiming transparent pixels. If later Command Wheel design requires per-pixel click-through composition, the host implementation can be replaced behind the same IPC protocol without changing runtime routing or React feature code.

## Frontend layout

Vite is configured as a multi-page application:

- `index.html` remains the settings/control panel.
- `overlay.html` loads a dedicated overlay React entry and stylesheet.

The overlay shell supports the protocol modes `clipboard` and `command_wheel`, but Phase 1 renders diagnostic state only. It does not implement clipboard item interaction or Command Wheel behavior.

The host loads `http://127.0.0.1:<web-port>/overlay.html`. Production assets are deployed with the existing WebGUI build; development can use the Vite server and its `/api` proxy.

## Process model

The runtime owns one `OverlayController`. The controller lazily starts one `overlay_host.py` child process. The host starts hidden and keeps the React page loaded. Subsequent requests reuse that process and call show/hide through IPC.

The controller owns a bounded command queue and performs process and IPC work on background threads. Global hotkey and input-hook callbacks only enqueue work and never wait for process startup, WebView initialization, or IPC.

The host process owns the WebView UI loop and a bounded UI-command queue. A host crash is isolated from the runtime.

## Local IPC

Windows Named Pipes are selected through Python's `multiprocessing.connection` `AF_PIPE` transport, using `send_bytes` and `recv_bytes` with JSON. Pickle-based `send`/`recv` is not used.

Each host start receives:

- a random pipe name,
- a random authentication key,
- the local overlay URL,
- the protocol version.

The authentication key activates the connection challenge/response. The random endpoint and key are generated per host lifetime. Named Pipes are local to Windows and are not exposed on TCP, UDP, or the LAN.

Protocol version 1 supports:

- `hello`
- `ready`
- `ping`
- `pong`
- `show_overlay`
- `hide_overlay`
- `overlay_visible`
- `overlay_hidden`
- `overlay_event`
- `shutdown`
- `error`

Messages contain `protocol`, `type`, `request_id`, and `payload`. Validation rejects unknown fields/types where relevant, unsupported modes, malformed request identifiers, and payloads above the configured byte limit. Invalid input returns a correlated `error` when possible and never terminates the runtime. Queues are bounded and no polling loop spins without a wait.

## Generic routing and Interaction Target

`get_interaction_target()` is a pure central helper:

- inactive forwarding returns `local`;
- active forwarding returns the active peer identity as a remote target.

The generic routing API accepts `mode`, `interaction_target`, cursor position, and payload. Phase 1 routes local targets to the local host. Remote routing is represented explicitly by the protocol and target data but returns a truthful unsupported result until the remote overlay phase; trigger code must not duplicate placement decisions.

## Coordinates, DPI, and monitors

Runtime and IPC cursor coordinates use Windows physical virtual-desktop pixels. They may be negative for displays positioned left or above the primary monitor.

The overlay host enables Per-Monitor-V2 awareness before creating a window. For the monitor containing the requested point it obtains:

- physical work-area bounds,
- monitor DPI,
- scale factor relative to 96 DPI.

Desired overlay dimensions are defined in CSS pixels and converted once to physical pixels. The native top-level window is then clamped to the monitor work area. React receives diagnostic physical coordinates and DPI metadata; CSS/WebView scaling remains WebView2's responsibility.

Pure conversion and clamping helpers are testable without creating a window. Visible mixed-DPI behavior still requires hardware validation.

## Lifecycle and supervision

Lifecycle states distinguish stopped, starting, connecting, ready, visible, unhealthy, and shutting down. Runtime status reports process, IPC, ready, visibility, current mode, restart count, and last error independently from core runtime health.

On a failed start or unexpected exit, the controller records the failure and schedules bounded restart eligibility with delays of 1, 2, and 5 seconds, capped at 5 seconds. It does not spin or continuously respawn. The next overlay request may restart the host after the delay. Repeated overlay failures mark only the overlay unhealthy.

Runtime shutdown sends `shutdown`, waits for a bounded grace period, then terminates only its owned child if necessary. No overlay host is intentionally left orphaned.

## Focus and Escape

Phase 1 uses an interactive overlay window. The React shell handles Escape through the pywebview JavaScript bridge, which requests a host-side hide and emits `overlay_hidden`. Complex click-through, mouse capture, right-click-hold, and focus restoration belong to later feature phases.

## Security

- IPC is a local authenticated Named Pipe, not a listening LAN socket.
- Only JSON bytes are decoded; no received data is unpickled or evaluated.
- Message and queue limits are enforced.
- React never executes operating-system actions directly.
- The Phase-1 bridge exposes only overlay hide/diagnostic event operations.
- Future Command Wheel actions must pass through a validated runtime Action Registry.

The overlay page is served by the existing localhost HTTP server. This decision does not grant that server new system-action endpoints.

## Installer and uninstaller impact

`pywebview` is a declared Python runtime dependency. The installer already creates an isolated FlowShift virtual environment and installs `requirements.txt`; it must also verify the WebView2 Evergreen Runtime before declaring overlay support ready. Node and Vite remain build-time requirements unless prebuilt assets are supplied.

The core and WebGUI installers must deploy `overlay_host.py`, overlay support modules, `overlay.html`, and its hashed assets. The uninstaller must request runtime shutdown and stop an owned overlay process before removing files. Shared WebView2 is detected but never removed by FlowShift.

## Failure recovery

Failures are logged at controller initialization, process start, IPC connect/disconnect, ready, show/hide request and completion, process exit, restart scheduling/result, invalid message, timeout, and shutdown. Frame-level UI rendering is not logged.

If WebView2 or pywebview is unavailable, IPC cannot become ready, or the host crashes, the controller reports the exact error in overlay status. Core runtime health remains based on its existing critical workers and is not falsified by overlay availability.
