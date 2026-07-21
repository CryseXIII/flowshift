# FlowShift - Current State

Updated 2026-07-21 after completion of Phase 1: Overlay Host and IPC Foundation.

## Productive path

- `src/python/tray.py --tray` is the productive Windows runtime.
- `src/python/gui.py` is the tkinter settings and legacy clipboard GUI.
- `webgui/` is the React/Vite settings UI and diagnostic overlay shell.
- `src/python/service.py` is a legacy wrapper and is not the productive path.
- Rust (`src/service`, `src/viewer`) remains experimental, excluded from the
  Cargo workspace, and was not changed for Phase 1.

## Runtime architecture

- Peer input/control uses length-prefixed JSON over TCP 45781; discovery uses
  UDP 45781.
- Local GUI control uses `127.0.0.1:45782`; the local WebGUI/API defaults to
  `127.0.0.1:5000`.
- Stable peer identities are `device:<device_id>` where known, otherwise
  `endpoint:<host>:<port>`.
- `runtime_model.get_interaction_target()` returns `local` while forwarding is
  inactive and the active peer identity while forwarding is active.
- Core runtime health, worker health, pipeline status, session context,
  interaction target and overlay health are reported independently.
- Linux input remains scaffolding only. No productive evdev/uinput backend exists.

## Phase 1 completed

- `overlay_protocol.py` implements validated protocol-v1 JSON envelopes with a
  65,536-byte cap, bounded request IDs and request correlation. It uses
  `send_bytes`/`recv_bytes`; no received data is unpickled.
- `overlay_geometry.py` handles physical virtual-desktop coordinates,
  Per-Monitor-V2 DPI conversion, work areas, clamping and negative coordinates.
- `overlay_host.py` runs pywebview 5.4/WebView2 in a separate process, starts
  hidden, keeps React preloaded, supports show/hide/Escape and watches its owner.
- `overlay_controller.py` owns the host lifecycle, bounded command queue,
  supervision, 1/2/5-second restart backoff, crash recovery and clean shutdown.
- Runtime and Web API expose overlay show/hide/ping diagnostics and report the
  exact overlay health snapshot.
- `webgui/overlay.html` is a separate Vite entry. Its `clipboard` and
  `command_wheel` modes are diagnostic shells only.
- Local interaction targets route to the local host. Remote targets return an
  explicit unsupported result and never open the local overlay by accident.
- Installer scripts deploy overlay modules/assets, install Pillow and
  `pywebview==5.4`, and verify/install the WebView2 Evergreen Runtime.
- Uninstallers stop only identifiable FlowShift overlay hosts and do not remove
  the shared WebView2 runtime.

## Existing implemented behavior

- Windows keyboard/mouse forwarding, stable hotkeys, relative-mouse coalescing,
  extended-key injection, pressed-state cleanup, flying direction switches,
  edge switching, reconnect and runtime diagnostics remain in place.
- Clipboard history supports text, Windows CF_HTML with plaintext fallback,
  files/batches, CF_DIB images, animated GIF previews, content-addressed dedup,
  manifest-only missing-item sync, disk-backed streaming, retry/resume,
  progress, pin/delete/clear and Win+V/Ctrl+Alt+V launch paths.
- The production installer runs FlowShift in the interactive user session using
  a Scheduled Task. Optional NSSM remains a non-input Session-0 helper only.

## Final automated verification

- Python compilation and all pure service, clipboard, transfer, streaming,
  diagnostics, overlay protocol/geometry and overlay lifecycle suites passed.
- `worker_smoke_test.py` passed worker health, real forwarding, direction switch,
  clipboard and overlay routing/lifecycle checks.
- `e2e_test.py` passed runtime handshake, input and shutdown.
- `reconnect_stress_test.py 30` passed all 30 peer reconnect rounds.
- Overlay IPC stress passed 1,000 ping/pong and 1,000 hide/response requests,
  malformed/unknown/oversized cases, 20 short sessions and host cleanup.
- Headless overlay stress passed 200 reusable show/hide cycles, forced crash and
  restart, 10 independent shutdown cycles and zero live child processes.
- A visible WebView2 stress run passed 200 cycles and crash recovery; a visible
  125% DPI smoke test loaded React with the expected physical dimensions.
- `npm ci --include=dev` completed with zero vulnerabilities; `npm run build`
  emitted both `dist/index.html` and `dist/overlay.html`.
- All five PowerShell installer/uninstaller scripts parse with zero errors.

## Manual validation still required

- Visible placement on primary and secondary displays, including a monitor with
  negative virtual-desktop coordinates.
- Windows scaling at 100%, 125%, 150% and 200%, plus Escape/focus behavior in
  normal desktop applications.
- Fresh-machine install/uninstall, including WebView2 detection.
- Two-device forwarding checks listed in `MANUAL_TEST_CHECKLIST.md`, including
  mouse feel, extended-key text selection and direction labels.

## Scope boundary and next phase

- Phase 1 is complete. Do not begin Phase 2 without explicit authorization.
- React Clipboard Overlay, Command Wheel behavior, remote overlay routing,
  click-through composition and right-click-hold are not implemented yet.
- The next planned phase is Clipboard Semantics Refactor; later phases remain in
  `TODO_CURRENT.md`.
- The repository changes are not automatically deployed to the existing
  `%ProgramFiles%\FlowShift` installation.

## Security note

Git history contains an old real `config.json` with device/network identifiers
and an old `start_flowshift.vbs` with a hardcoded development path (commit
`c777cff`). Current tracked files are clean. Rewriting published history requires
explicit approval and is outside Phase 1.
