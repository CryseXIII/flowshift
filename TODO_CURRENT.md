# FlowShift - Open Development Tasks

## Current phase

No implementation phase is active. Phase 1 (Overlay Host and IPC Foundation) is
complete. Phase 2 must not start without explicit authorization.

## Phase 1 completion

- Python pure, runtime integration, reconnect, overlay lifecycle, IPC stress and
  show/hide stress suites pass.
- A clean `npm ci --include=dev` reports zero vulnerabilities and the production
  build emits both `index.html` and `overlay.html`.
- Architecture, protocol, clipboard, manual-test and handoff documentation is
  reconciled with the implementation.

## Hardware validation still required

- Visible React overlay placement on primary and secondary monitors.
- Monitor left of the primary display with negative virtual-desktop coordinates.
- Windows scaling at 100%, 125%, 150%, and 200%.
- Escape/focus behavior in normal desktop applications.
- Fresh-machine installer and uninstaller validation, including WebView2 detection.

## Future phases

### Phase 2: Clipboard Semantics Refactor

- Event-driven Windows Clipboard Listener.
- Metadata-first live announcements.
- `current_item` semantics.
- Persistent received-cache policy.
- Availability/provider-model foundation.
- Transfer preflight design.

### Phase 3: Clipboard Transfer Hardening

- Batch manifest transfers.
- Disk peak planning.
- Reduced HDD write amplification.
- Persistent resume state across runtime restarts.
- Transfer stress testing.

### Phase 4: React Clipboard Overlay

- Win+V integration.
- Interaction Target routing.
- Item rendering, markup, and previews.
- Selection, live progress, and clipboard materialization.
- Remove the legacy Tkinter fallback only after verified replacement.

### Phase 5: Command Wheel Data Model and Action Registry

- Recursive model and named pages.
- Maximum eight sectors per page.
- Parameter schemas and controlled action execution.
- WebGUI configuration editor.

### Phase 6: React Command Wheel Overlay

- Right-click-hold detection while preserving a normal short right-click.
- Eight sectors, hover, paging, vertical page indicator, and page-name transition.
- Recursive navigation and mouse-wheel paging.

### Phase 7: Windows Shell Integration

- Explorer selection and shell verbs.
- Open With, Send To, Properties, and dynamic context actions.

### Phase 8: Full Integration and Stress Hardening

- Remote interaction targets and rapid overlay use.
- Peer reconnects, runtime restarts, and overlay crashes.
- Large clipboard datasets and massive file transfers.
- Installer, uninstaller, and multi-device hardware validation.
