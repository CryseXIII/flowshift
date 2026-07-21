# FlowShift - Current State

Updated 2026-07-22 at the documentation-first start of Phase 2.

## Current iteration

- Starting commit: `16ec09ae51fb2a603e6923c797775ea053a4a083`.
- Active scope: Phase 2 Clipboard Semantics Refactor only.
- Phase 3 transfer hardening, the full React Clipboard UI, Command Wheel, remote
  overlay routing, and Windows shell integration must not begin in this iteration.
- `docs/clipboard_semantics.md` is the binding Phase 2 behavior and compatibility
  contract. Production changes must follow it and remain incremental.

## Phase 2 baseline and analysis

- Local `master`, `origin/master`, and GitHub were synchronized at
  `16ec09ae51fb2a603e6923c797775ea053a4a083`; the worktree was clean and the
  stable latest release remained `v0.4.0`.
- Python compilation and full `test_*.py` discovery pass. The first discovery
  run saw one Windows socket abort in the oversized Web API request test; that
  test passed immediately in isolation and full discovery then passed unchanged.
- All 41 worker-smoke checks pass. The first run timed out once waiting for an
  activation control response; cleanup completed and the unchanged isolated
  rerun passed every check.
- E2E, 30 reconnect rounds, 1,000+1,000 overlay IPC requests, and 200 headless
  overlay show/hide cycles with crash recovery pass.
- `npm ci --include=dev` reports zero vulnerabilities, all 9 Vitest checks pass,
  and Vite emits both production entries.
- All 9 tracked PowerShell scripts parse, all 7 updater simulations pass, the
  curated release payload contract passes, and Inno Setup 6.7.3 builds the
  unchanged `v0.4.0` release artifacts.
- The architecture and all `test_clipboard*.py` suites were reviewed before
  production changes. Important compatibility findings are recorded in the
  semantics contract, including the distinct meaning of file content identity
  versus ZIP payload identity.

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
- `78a9ee39aa2f30411cf5862d9b483bf1723b0e11` - `feat: integrate automatic update runtime lifecycle`
- `c0c740ea574d6187edf5c1de38d27edd3547dc8b` - `feat: add update controls to web settings`
- `38834d24263140d733543a6b92d0751ea6324fc1` - `build: add release packaging and publication workflow`
- `cf166df0e5b603cba8faffa610197905a9397100` - `ci: install dependencies before release tests`
- `16ec09ae51fb2a603e6923c797775ea053a4a083` - `docs: record v0.4.0 release validation`

## Phase 1.5 implemented so far

- Root `VERSION` is the only product version source and currently contains
  `0.4.0`; config schema 1 migrates with pre-migration backup and atomic writes.
- Stable update discovery accepts only the validated GitHub latest-release
  contract and the required setup, manifest and checksum assets.
- Update state, bounded discovery/download workers, streamed size-limited
  downloads, SHA-256 verification and race-safe idle reservations are in place.
- The external PowerShell runner validates its plan and installer, preserves
  user state, performs health checks and rolls back failed installations.
- Runtime startup and automatic `notify`, `download` and `install` policies are
  integrated without making update availability part of core runtime health.
- The localhost API exposes status, check, download, install and exact update
  settings routes. Mutating update routes require a local Host, trusted local
  Origin when present, bounded JSON bodies and accept no URL, path or command.
- WebGUI Settings shows installed/latest versions, update state, progress,
  release notes, errors, recovery notices and development/idle blockers. It
  polls active operations quickly and prevents duplicate UI requests.
- Install capability requires an installed layout, downloaded state, matching
  managed asset metadata, an existing expected-size setup file and a safe idle
  runtime snapshot. The handoff performs the final SHA-256 revalidation.
- `packaging/build_release.ps1` stages an explicit production-only payload,
  compiles `FlowShift-Setup.exe`, and binds its exact size and SHA-256 into
  `update-manifest.json` and `SHA256SUMS.txt`.
- Packaged setup embeds prebuilt WebGUI assets and invokes the existing core and
  WebGUI installers. `/FLOWUPDATE` is noninteractive and leaves runtime start and
  health ownership exclusively with the external rollback runner.
- `.github/workflows/release.yml` gates tags against stable root `VERSION`, runs
  update/API/runtime/WebGUI/packaging tests, creates a complete draft release,
  verifies all three assets, and only then publishes it as latest.
- The updater compatibility floor defaults to `0.4.0`; it does not automatically
  rise with product versions and block older installations from future updates.
- Tag `v0.4.0` resolves to `cf166df0e5b603cba8faffa610197905a9397100`.
- Public release: `https://github.com/CryseXIII/flowshift/releases/tag/v0.4.0`.
- Permanent setup URL:
  `https://github.com/CryseXIII/flowshift/releases/latest/download/FlowShift-Setup.exe`.
- The published setup is 2,334,533 bytes with SHA-256
  `a3a679d817ce6481ace1cd77669ccd695c8c84b67050315e66a6aac11bf0a4af`.
- The public latest-release API exposes exactly `FlowShift-Setup.exe`,
  `update-manifest.json`, and `SHA256SUMS.txt`; all are uploaded and non-empty.

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

Current Phase 1.5 API/WebGUI verification:

- 34 update-manager and update-Web-API tests pass, including settings
  preservation, request-shape rejection, admission outcomes and missing managed
  installer invalidation.
- 9 React Software Update tests pass, including plaintext release notes,
  progress, operation admission, waiting-for-idle and development-mode states.
- Python compilation, the Vite production build and all 41 worker-smoke checks
  pass after the API/WebGUI integration.
- Curated payload tests and Inno Setup 6.7.3 compilation pass locally. The
  generated setup is bound to a schema-1 stable manifest and two-file checksum
  list with the exact required release asset names.
- Release workflow YAML passes local syntax linting. Tagged run
  `https://github.com/CryseXIII/flowshift/actions/runs/29868191597` completed every
  test, build, draft verification, upload and publish step successfully.
- Final local regression passed Python compile/discovery, 9 React tests, Vite
  build, 25 PowerShell parser checks, 7 updater simulations, all 41 worker-smoke
  checks, E2E, 30 reconnect rounds, 1,000+1,000 IPC requests, 200 overlay cycles,
  setup compilation, manifest validation and checksum verification.
- E2E and reconnect harnesses now isolate config, logs and update state under
  temporary directories instead of touching the machine's `%ProgramData%`.
- The first tag run correctly stopped before packaging because clean Python 3.12
  lacked declared dependencies; no release was created. After adding the
  requirements install/cache step, the clean 3.12 suite and tagged run passed.
- Public manifest size/hash match GitHub's asset metadata and `SHA256SUMS.txt`.
  The permanent latest URLs resolve to `v0.4.0`, and a real update-client check
  reports `up_to_date 0.4.0` without an error.

## Manual validation still required

- Visible placement on primary and secondary displays, including a monitor with
  negative virtual-desktop coordinates.
- Windows scaling at 100%, 125%, 150% and 200%, plus Escape/focus behavior in
  normal desktop applications.
- Fresh-machine install/update/rollback/uninstall, including Python and WebView2
  detection and the interactive Scheduled Task.
- Two-device forwarding checks listed in `MANUAL_TEST_CHECKLIST.md`, including
  mouse feel, extended-key text selection and direction labels.

## Scope boundary and active phase

- Phase 1 and the automated/release scope of Phase 1.5 are complete. Phase 2 is
  now authorized and active; manual hardware and clean-VM validation remain
  listed in `TODO_CURRENT.md`.
- React Clipboard Overlay, Command Wheel behavior, remote overlay routing,
  click-through composition and right-click-hold are not implemented yet.
- The active phase is Clipboard Semantics Refactor. Later phases remain in
  `TODO_CURRENT.md` and must not be started as part of Phase 2.
- The repository changes are not automatically deployed to the existing
  `%ProgramFiles%\FlowShift` installation.

## Security note

Git history contains an old real `config.json` with device/network identifiers
and an old `start_flowshift.vbs` with a hardcoded development path (commit
`c777cff`). Current tracked files are clean. Rewriting published history requires
explicit approval and is outside Phase 1.
