# FlowShift - Current State

Updated 2026-07-21 at the start of Phase 1.5: Release and Update Infrastructure.

## Current iteration

- Starting commit: `55147ee4914197c299652919b58798e3281f9845`.
- Active scope: Phase 1.5 release, installer, versioning and automatic update
  infrastructure only.
- Phase 2 Clipboard Semantics and Command Wheel work must not begin in this
  iteration.

## Exact commits of latest iteration

Phase 1 closed through these exact commits:

- `d0625b9eb2ec1dfd99fd03d3f49b649be8bf9b28` - `docs: establish overlay architecture and active roadmap`
- `b7d1ec2b0ccfc406c2e1cdce1184216665e1d4a4` - `fix: restore omitted clipboard and diagnostics runtime`
- `f9f0b377edf57c9818a07980506daa92746b860a` - `feat: add overlay protocol and targeting primitives`
- `3e30978b72b38ad082083b4367b3409d29763371` - `feat: add preloaded React overlay shell`
- `787679fe44220216c260e3655fcb3db515714e2b` - `feat: add isolated overlay host and lifecycle controller`
- `ffed780d9bb711edc54604340e883e6b23aa797c` - `feat: integrate overlay routing and runtime supervision`
- `85c06bc208debf427156e96a0828228b1a88e843` - `build: integrate overlay runtime into installation lifecycle`
- `590198300c74986c9d07fb700ac1e3b285d5a658` - `test: add overlay IPC and lifecycle stress coverage`
- `55147ee4914197c299652919b58798e3281f9845` - `docs: close overlay foundation phase`

Phase 1.5 commits pushed so far:

- `42d63b764167a82bf7d787a3ceae6f49b19441aa` - `docs: enforce active todo and handoff rules`
- `8b653473c438aae6e01739c27b37d9b638d30b1d` - `feat: centralize version and config schema migration`
- `5728267a274e01ae3a1a575f24fb1ddc1824f26f` - `feat: add github release update discovery`
- `f81c99c92f87f7e00b9b8f51c003d187e29e6fa4` - `feat: add update state manager and download verification`
- `c0a28ca462d1cb4c6a9ab34e20697080208fc5ea` - `feat: add race-safe update idle gate`
- `1ca7016493eb0bda73ab9a483802a18e7525c13f` - `feat: add isolated update installer handoff and rollback`

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

- Phase 1 is complete and Phase 1.5 is active. Do not begin Phase 2 until Phase
  1.5 has been released and a separate instruction authorizes it.
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
