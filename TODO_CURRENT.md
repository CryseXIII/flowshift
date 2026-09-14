# FlowShift - Open Development Tasks

## Current phase

Phase 4 - Clipboard Overlay and Command Wheel (target release `v0.7.0`).

## Active phase specification

`docs/phases/phase_4_clipboard_overlay_command_wheel.md`

## Open implementation work

- Slice 1: Action Registry (`overlay_actions.py`) and Web-API routes
  `/api/actions`, `/api/actions/wheel`, `/api/actions/execute` with tests.
- Slice 2: overlay host per-mode sizing, focus-loss dismiss, wheel payload.
- Slice 3: React `CommandWheel` (8 sectors, cyclic paging, dots, spotlight).
- Slice 4: React `ClipboardOverlay` (fixed height, stable scroll, async refresh,
  item actions, transfer progress).
- Slice 5: `tray.py` hotkeys open the overlays; action execution restores the
  previous foreground window and injects keys via `inject_queue`.
- Slice 6: WebGUI wheel configuration, Tkinter clipboard tab/window replaced,
  documentation, full regression, release `v0.7.0`.

## Open manual hardware and VM tests

- Run the packaged installer, update, rollback, reinstall, and uninstall checks on a clean Windows x64 VM without Python.
- Repeat installation and update checks on Windows with Python already installed.
- Run the two-device live clipboard matrix for text, HTML, images, files, and a 150-file batch on real or virtual peers.
- Run the Phase 3 two-device transfer V2 matrix in `MANUAL_TEST_CHECKLIST.md` (section "Clipboard Transfer V2"), including LAN drops, sender/receiver/dual restarts, disk-full, legacy peer fallback, and the `v0.5.4` -> `v0.6.3` upgrade.
- Verify disconnect/reconnect recovery and temp cleanup across two peers.
- Run visible overlay lifecycle checks on multi-monitor Windows systems at 100%, 125%, 150%, and 200% scaling.
- Complete the remaining checks in `MANUAL_TEST_CHECKLIST.md` and `docs/install_test_checklist.md`.

## Next planned phase

- None defined after Phase 4.
