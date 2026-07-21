# FlowShift - Open Development Tasks

## Current phase

Phase 1.5 - Release and Update Infrastructure

## Open Phase 1.5 tasks

- Build `FlowShift-Setup.exe` from a curated payload with prebuilt WebGUI assets.
- Add the version-gated GitHub Actions release workflow, generated manifest and
  SHA-256 checksums.
- Complete release packaging, installer, regression and PowerShell parser tests.
- Publish and validate the `v0.4.0` GitHub Release and permanent latest-installer
  URL, then reconcile all release/update documentation.

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
