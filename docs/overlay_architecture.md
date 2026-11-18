# FlowShift Overlay Architecture

## Status

The Phase 1 host foundation and Phase 4 functional Clipboard Overlay and Command
Wheel are implemented and automated regression-tested. Visible mixed-DPI and
multi-monitor combinations remain manual checks.

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

Windows pywebview does not promise per-pixel alpha transparency for the
WebView2 window, and a WinForms colour key does not reliably apply to the
WebView2 child windows composed by the browser process. The shaped overlay
therefore uses a Win32 window region (`SetWindowRgn`) on the frameless
top-level form: Windows neither draws nor hit-tests pixels outside the
region, for the form and all its WebView2 child windows, so the desktop stays
visible there and clicks reach the window below. Trade-off: region edges are
hard (no anti-aliasing, no soft shadow); the ring sectors carry a dark outline
so the edge stays clean at 100-200 % scaling. Soft alpha edges would need a
custom WebView2 composition host (DirectComposition) and remain out of scope.

## Frontend layout

Vite is configured as a multi-page application:

- `index.html` remains the settings/control panel.
- `overlay.html` loads a dedicated overlay React entry and stylesheet.

`OverlayShell` dispatches `clipboard` to a fixed-height history view and
`command_wheel` to the radial action menu. Diagnostic cards are rendered only
when the show payload explicitly contains `diagnostic: true`. The clipboard
view polls items and legacy/V2 progress asynchronously and supports set/get,
pin/unpin and delete. The wheel shows at most eight sectors per page, pages
cyclically with the mouse wheel and executes only registered action IDs.

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

Desired overlay dimensions are defined in CSS pixels and converted once to
physical pixels. The command wheel is 360x360 CSS pixels and centered on the
cursor; the clipboard is 420x520 and placed next to it. The native top-level
window is clamped to the monitor work area; when the wheel has to shift inward
at a screen edge, the host moves the cursor onto the wheel hub so the pointer
is always in the middle. React receives physical coordinates and DPI metadata;
CSS/WebView scaling remains WebView2's responsibility.

The visible shape (`overlay_geometry.overlay_shape_polygons`) is computed in
the same physical pixels: for the wheel the annular sectors of the current
page (same angles, radii and 2.5 degree gaps as `wheelGeometry.js`) plus the
small page pill in the hub when there are several pages; for the clipboard a
rounded rectangle. The host applies it as the window region and uses the same
polygons for its outside-click hit test. React reports paging with
`overlay_event("wheel_page", {page})` so the region follows the sector count.

Pure conversion and clamping helpers are testable without creating a window. Visible mixed-DPI behavior still requires hardware validation.

## Lifecycle and supervision

Lifecycle states distinguish stopped, starting, connecting, ready, visible, unhealthy, and shutting down. Runtime status reports process, IPC, ready, visibility, current mode, restart count, and last error independently from core runtime health.

On a failed start or unexpected exit, the controller records the failure and schedules bounded restart eligibility with delays of 1, 2, and 5 seconds, capped at 5 seconds. It does not spin or continuously respawn. The next overlay request may restart the host after the delay. Repeated overlay failures mark only the overlay unhealthy.

Runtime shutdown sends `shutdown`, waits for a bounded grace period, then terminates only its owned child if necessary. No overlay host is intentionally left orphaned.

## Focus and Escape

The overlay does not depend on owning the foreground. Windows may refuse
`SetForegroundWindow` to the overlay process (foreground lock, e.g. after a
hook-consumed Ctrl+RightClick); the earlier focus watcher then closed the
overlay after its 0.75 s grace period. The host now runs a dismiss watcher
(`DismissPolicy`) that polls every 30 ms, without a global hook:

- a new Escape press closes with `escape` (also when another window has focus;
  the key still reaches that window);
- a new left/right/middle press outside the visible shape closes with
  `outside_click`; the click itself also reaches the window below, because the
  region makes those pixels click-through;
- another window becoming foreground (other than the overlay or the window
  that was active before the show, or any window after the overlay itself was
  active) closes with `focus_lost`.

Buttons or Escape already held at show time are not new presses. React also
requests hides through the bridge (`escape`, `close_button`), and the runtime
sends `hide_overlay {reason}` (`action` after a wheel action, otherwise
`runtime`); React itself does not hide after executing, so an action that
opens the clipboard overlay cannot race with its own hide. The host reports
exactly one `overlay_hidden {reason, mode}` per show; the controller logs
`overlay_hidden reason=<reason> mode=<mode>`. On hide the host calls
`window.flowshiftOverlay.hide()`, which unmounts the React views so a hidden
clipboard overlay stops polling. Before showing, the runtime remembers the foreground window; wheel
keyboard actions hide the overlay, restore that window and enqueue the validated
key sequence. The wheel can be opened by its configurable hotkey, a global
`Ctrl+RightClick` low-level mouse hook or the mouse side button chosen in
`command_wheel.trigger.mouse_button` (X1/X2, same hook); only that right-button
or side-button down/up pair is consumed, an unassigned side button keeps
back/forward, and forwarding-active or injected clicks pass through. While
forwarding to another device no trigger opens the wheel locally.

### Hold and swipe

The press of a trigger opens the wheel with `data.hold = true`
(`wheel_gesture.py`, `tray.begin_wheel_hold`); the wheel hotkey is registered
with `MOD_NOREPEAT`, and further presses during a hold are ignored. The runtime
detects the release (mouse hook up event, or `GetAsyncKeyState` polling for the
hotkey key, which `RegisterHotKey` does not report) and sends
`wheel_release {held_ms}` through the controller queue. While the trigger is
held the host's dismiss watcher computes the sector in the cursor direction
from the hub every 30 ms (direction only, a cursor beyond the ring still
counts; 30 CSS px dead zone) and calls `window.flowshiftOverlay.aim(index|null)`
so React highlights it; mouse presses do not close the wheel during the hold
(Escape still does). On the release the host answers
`wheel_released {outcome, action_id}`:

- shorter than `trigger.hold_ms` (default 250 ms): `keep_open`, the wheel stays
  open for clicking and the normal dismissal rules apply; with
  `tap_open = false` it closes (`tap_off`);
- held: `execute` with the sector's action (`swipe`) or `close` inside the dead
  zone (`swipe_center`); with `hold_swipe = false` it stays open.

For `execute` the runtime waits up to 1 s until Ctrl/Alt/Shift/Win are up and
then runs `execute_action` (`source = wheel_swipe`), so the action reaches the
remembered window exactly like a click.

## Security

- IPC is a local authenticated Named Pipe, not a listening LAN socket.
- Only JSON bytes are decoded; no received data is unpickled or evaluated.
- Message and queue limits are enforced.
- React never executes operating-system actions directly.
- The bridge exposes only overlay hide (with a whitelisted reason) and
  overlay events; the only event the host interprets is `wheel_page`
  (validated integer page, used to recompute the window region). The swipe
  result is decided by the host from its own geometry and the registry-backed
  show payload; the runtime re-validates the returned action id.
- Command Wheel actions go through localhost HTTP, `overlay_actions.py`
  validation and `tray.execute_action`; React cannot submit arbitrary key
  sequences or shell commands.

The overlay page is served by the existing localhost HTTP server. This decision does not grant that server new system-action endpoints.

## Installer and uninstaller impact

`pywebview==6.2.1` is a declared Python runtime dependency. The installer creates
an isolated FlowShift virtual environment, installs `requirements.txt`, and
verifies or installs the WebView2 Evergreen Runtime. Node and Vite remain
build-time requirements unless prebuilt assets are supplied.

`install_flowshift.ps1` deploys `overlay_host.py`, overlay support modules,
`overlay.html`, and its hashed assets in one run and replaces the previous
WebGUI completely. The uninstaller requests runtime shutdown and stops only an
identifiable FlowShift overlay process before removing files.

Before starting the host, the runtime checks the served WebGUI
(`webgui_health.py`): without `overlay.html` it does not spawn the host and shows
a tray notice instead. The WebGUI `VERSION` must equal the program `VERSION`;
a mismatch is reported in the tray and on the dashboard (`status.webgui`). A
host that never becomes ready is retried with growing delays (1, 2, 5, 15, 30,
60 s); after five failed starts the controller pauses for ten minutes and
reports it (`overlay.gave_up`).
Shared WebView2 is detected but never removed by FlowShift.

## Failure recovery

Failures are logged at controller initialization, process start, IPC connect/disconnect, ready, show/hide request and completion, process exit, restart scheduling/result, invalid message, timeout, and shutdown. Frame-level UI rendering is not logged.

If WebView2 or pywebview is unavailable, IPC cannot become ready, or the host crashes, the controller reports the exact error in overlay status. Core runtime health remains based on its existing critical workers and is not falsified by overlay availability.

## Verification record

Phase 1 passed pure protocol/geometry and controller lifecycle suites, runtime
worker integration, malformed and oversized IPC cases, 2,000 sequential stress
requests, 20 short IPC sessions, 200 reusable show/hide cycles, forced host
crash/restart, 10 independent shutdown cycles, and clean child-process reaping.
Phase 4 adds registry/WebAPI/runtime tests and React tests for sectors, cyclic
paging, execution, clipboard actions, stable scroll and legacy/V2 progress. A
visible WebView2 run completed 200 show/hide cycles and crash recovery, and a
visible 125% DPI smoke test loaded React with expected dimensions. Mixed-monitor
and 100/150/200% DPI combinations remain a manual hardware-validation
requirement.
