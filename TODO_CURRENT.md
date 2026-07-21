# FlowShift - Open Development Tasks

## Current phase

Phase 1: Overlay Host and IPC Foundation

## Open tasks

- Integrate overlay assets and Python dependencies into installation and uninstallation.
- Add unit, IPC stress, show/hide stress, crash-recovery, and shutdown coverage.
- Run the full regression suite and the WebGUI production build.
- Reconcile README, architecture, protocol, clipboard documentation, manual checklist, and handoff with code.

## Required tests

- Full existing Python regression suite.
- Overlay protocol, correlation, size-limit, target, coordinate, DPI, lifecycle, and backoff unit tests.
- 1000-request IPC stress run with invalid and oversized frames and reconnect churn.
- 200-cycle show/hide stress run with process reuse verification.
- Overlay-host crash recovery and repeated shutdown tests.
- `npm ci --include=dev` and `npm run build`.

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

- Stream-first file transfers.
- Batch manifest transfers.
- Disk peak planning.
- Reduced HDD write amplification.
- Resume and persistence.
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
