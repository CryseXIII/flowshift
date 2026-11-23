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

The region alone was not enough (dev.30/dev.31 on Viktor's laptop: region set
and kept, yet the dark square stayed). pywebview gives the WebView2 control an
opaque `DefaultBackgroundColor` equal to `background_color` (#111827), and
WebView2 composes its content through DirectComposition, which a window
region does not reliably clip. Since dev.32 the host sets
`DefaultBackgroundColor = Color.Transparent` on the WebView2 control (on the
WinForms UI thread, before the first `show()`), so WebView2 draws only what
the page paints (the page body is already transparent). `window.native` is
pywebview's `BrowserForm`; the control is `native.browser.web_view`
(EdgeChrome backend), with `native.webview` only as fallback
(`overlay_host.webview2_control`). The found path is logged as
`overlay webview control path=...`; if none is found the failure line lists
the form's attribute names (`native attrs=...`). Up to dev.33 pywebview's own
`transparent=True` was not used because it shows and activates the window on
every navigation; dev.34 uses it and switches that off (see below). The region
stays: it clips the opaque form background and decides where clicks land.

The host resolves the HWND robustly (`Form.Handle` as `IntPtr` or int, else
its own top-level window titled `FlowShift Overlay`), verifies with
`GetWindowRgn` after `show()` that the region is still set and re-applies it
once if not. Diagnostics go to `flowshift.log` as INFO lines (host stderr lines
prefixed `info: `, see `overlay_protocol.STDERR_INFO_PREFIX`):
`overlay webview background transparent=ok|failed`,
`overlay shape set mode= page= hwnd= placement= dpi= scale= window= client=
window_dpi= exstyle= polygons= setwindowrgn= region= box=`,
`overlay shape after show mode= kept ...` and once per host
`overlay window tree hwnd= top= children=` followed by one line per window
of the host process and per child window (also of the browser process):
`overlay window top|child hwnd= class= pid= rect=x,y,WxH visible= region=
exstyle=`. Failures stay WARN lines `overlay host: overlay host shape ...`.

dev.32 was still not enough live (Viktor 20.11., square visible, log:
`transparent=failed (CoreWebView2Controller members can only be accessed from
the UI thread ... get_DefaultBackgroundColor)`, region set and kept). Three
causes are addressed since dev.33:

- The colour was read back on the host's worker thread, so the step never
  counted as done and its effect was unproven. Every read and write now runs
  inside `form.Invoke` on the WinForms UI thread; the line reads
  `overlay webview background transparent=ok ui_thread=1 alpha=0
  form_key=#111827/a255 layered=1` (or `failed` with the .NET message).
- The form itself is made see-through: `BackColor` and `TransparencyKey` are
  `OVERLAY_KEY_COLOR` (#111827), so WinForms makes it a layered window with a
  colour key (`layered=1`) and the form background is left out even inside
  the region where the page is transparent.
- The overlay stylesheet set `color-scheme: dark` on the root element. With a
  dark root colour scheme Chromium paints the page canvas dark although `html`
  and `body` are transparent, which fills the whole window. The colour scheme
  now sits on `body`, the canvas stays transparent.

The window region is additionally set on every child window of the form
(WebView2 host and Chrome windows, shifted into their coordinates); the
`overlay shape set` line ends with `children=N [0x..:class:ok ...]`.

dev.33 was still not enough live (Viktor 21.11.: square visible although the
log showed `transparent=ok ui_thread=1 alpha=0 form_key=#111827/a255
layered=1`, the region on the only visible window and on all five children,
kept after show). The logs only proved that calls returned. The page was
checked as well: `overlay.css` has no frame or card behind the wheel (only the
sector fills and the page pill paint; `.diagnostic-card` is only used with
`diagnostic: true`). Since dev.34:

- Transparency is per-pixel alpha instead of a colour key: `create_window`
  uses `transparent=True` (WebView2 is created with alpha 0 before
  CoreWebView2 starts; the host clears pywebview's `window.transparent` flag
  in `before_show`, so pywebview's navigation handler no longer shows and
  activates the hidden window), the form paints black (`OVERLAY_FORM_COLOR`,
  GDI writes alpha 0) without `TransparencyKey`, and
  `DwmEnableBlurBehindWindow` with an empty blur region makes DWM compose the
  window with its alpha channel (the approach winit/tao use for transparent
  WebView2 windows). The region stays for click-through. Log:
  `overlay webview background transparent=ok strategy=alpha ui_thread=1
  alpha=0 form=#000000/a255 form_key=#000000/a0 layered=0 blur=ok`.
- The result is measured on screen instead of trusted: right before the show
  the host copies the desktop under the hidden window (one `BitBlt` from the
  desktop DC with `CAPTUREBLT`), 0.4 s after the show it copies it again and
  compares fixed points (`overlay_geometry.overlay_probe_points`: four
  corners, a hub point beside the page pill, a ring point, the pill; clipboard:
  four rounded corners and the panel centre). Per point:
  `overlay pixel mode= point= at= screen= expect=clear|shape below=#rrggbb
  shown=#rrggbb result=clear|painted|drawn|missing
  source=below|form_111827|black|painted hit=0x..:class:overlay|below`
  (`hit` is `WindowFromPoint`, i.e. where a click lands), then
  `overlay pixel check mode= verdict=ring_only|panel_only|square_visible|
  background_visible|nothing_drawn|unknown strategy=alpha rect= clear=
  clickthrough= drawn= painted= region= rects=N [x,y,WxH;...]` with the
  region read back by `GetRegionData`.
- `POST /api/overlay/probe` (`{"mode": "command_wheel"|"clipboard"}`, local
  WebGUI API) opens the overlay with its real content at the cursor and so
  triggers the same measurement without the hotkey.

dev.34 was still not enough live (Viktor 21.11. 23:33). His screenshots show a
~356x356 rectangle in #202020 with rounded corners and the thin light border
that Windows 11 draws around a normal window; the sector fill measured
#141920, which is exactly `rgba(19, 24, 32, 0.9)` over #202020. The page was
transparent, but DWM composed the WebView2 content onto a window surface that
neither the region nor alpha/blur removed. Since dev.36 the Command Wheel does
not use WebView2 at all:

- `overlay_native_wheel.py` draws the wheel with GDI+ into a 32-bit
  premultiplied bitmap (`PixelFormat32bppPARGB`) and hands it to its own
  window (`WS_POPUP`, `WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW`,
  class `FlowShiftCommandWheel`) with `UpdateLayeredWindow(ULW_ALPHA)`. DWM
  shows exactly these pixels: alpha 0 shows the desktop and passes clicks to
  the window below, so no window region is set (`overlay shape native ...
  region=none`; the polygons still serve the host hit test).
- The window runs on its own thread and message loop
  (`overlay-native-wheel`); the host calls show/hide/aim through
  `PostMessage`. `DWMWA_WINDOW_CORNER_PREFERENCE=DWMWCP_DONOTROUND`,
  `DWMWA_BORDER_COLOR=DWMWA_COLOR_NONE` and
  `DWMWA_SYSTEMBACKDROP_TYPE=DWMSBT_NONE` are set and read back with
  `DwmGetWindowAttribute` (also on the WebView2 form, whose clipboard panel
  stays WebView2).
- The scene matches the React wheel (`overlay.css`): sectors, labels, hover
  spotlight (only the hovered or aimed sector is lit blue; all other sectors
  keep their resting fill, stroke and label colour, no dimming and no
  transition, Viktor 22.11.), hold-and-swipe aim, error label, busy outline, the page pill with
  its dots (Viktor 20.11.: the two dots stay) or `n/m` text. The mouse wheel
  pages (`overlay_event wheel_page`, as React reported it); a click posts
  `POST /api/actions/execute {"action_id", "context": {"source": "command_wheel"}}`
  to the local WebGUI API, exactly what the React wheel sent.
- If the native window cannot start or paint, the host logs
  `overlay native wheel unavailable|show failed (<cause>) surface=webview` and
  shows the WebView2 wheel as before.
- Log: `overlay native wheel ready hwnd= exstyle= corner=set:0x../read:N
  border=... backdrop=... paint=updatelayeredwindow`, per show `overlay native
  wheel shown hwnd= rect= dpi= dwm_corner= dwm_backdrop=`, and the pixel check
  with `strategy=native_layered` (WebView2: `strategy=alpha`) and
  `dwm_corner= dwm_backdrop=` at the end.

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
