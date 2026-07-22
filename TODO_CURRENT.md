# FlowShift - Open Development Tasks

## Current phase

Phase 2 - Clipboard Semantics Refactor

Every new commit from `0.5.0-dev.1` onward must increment the central `VERSION`
development counter exactly once. Development versions are never stable releases;
the final fully validated release commit changes `VERSION` to `0.5.0`.

The binding behavior and compatibility contract is in
`docs/clipboard_semantics.md`. Phase 3 transfer hardening and all later UI,
Command Wheel, and shell-integration phases remain out of scope.

## Phase 2 implementation checklist

- [ ] Run the full regression, bump `VERSION` to `0.5.0`, publish and validate
  `v0.5.0`, then stop before Phase 3.

## Open release validation tasks

- Validate fresh install, upgrade, rollback, and uninstall on a disposable
  Windows x64 VM.

## Hardware validation still required

- Visible React overlay placement on primary and secondary monitors.
- Monitor left of the primary display with negative virtual-desktop coordinates.
- Windows scaling at 100%, 125%, 150%, and 200%.
- Escape/focus behavior in normal desktop applications.
- Fresh-machine installer and uninstaller validation, including WebView2 detection.

## Future phases

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
