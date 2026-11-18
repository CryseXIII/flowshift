# FlowShift - Open Development Tasks

## Current phase

Phase 4 - Clipboard Overlay and Command Wheel (target release `v0.7.0`).

## Active phase specification

`docs/phases/phase_4_clipboard_overlay_command_wheel.md`

## Open implementation work

- Full regression per `AGENTS.md`, reconcile `HANDOFF_CURRENT.md`, release
  `v0.7.0` and verify the tag workflow and assets.

## Open manual hardware and VM tests

- Run the packaged installer, update, rollback, reinstall, and uninstall checks on a clean Windows x64 VM without Python.
- Repeat installation and update checks on Windows with Python already installed.
- Run the two-device live clipboard matrix for text, HTML, images, files, and a 150-file batch on real or virtual peers.
- Run the Phase 3 two-device transfer V2 matrix in `MANUAL_TEST_CHECKLIST.md` (section "Clipboard Transfer V2"), including LAN drops, sender/receiver/dual restarts, disk-full, legacy peer fallback, and the `v0.5.4` -> `v0.6.3` upgrade.
- Verify disconnect/reconnect recovery and temp cleanup across two peers.
- Run visible overlay lifecycle checks on multi-monitor Windows systems at 100%, 125%, 150%, and 200% scaling.
- Verify the shaped, see-through Command Wheel and the dismissal rules from `0.7.0-dev.23` on Windows (stays open 30 s, Escape/outside click close with logged reason, action reaches the previous window, edge centering).
- Verify on Windows (`0.7.0-dev.24`): busy overlay hotkeys appear as tray notification, Dashboard banner and per-hotkey status in Settings; the configurable clipboard overlay hotkey opens the clipboard window at the mouse and it stays open; no `announcement failed -> local` warning when copying.
- Decision for Viktor: outside clicks currently close the overlay and also reach the window below; swallowing them (Windows context-menu style) would need a low-level mouse hook in the overlay host.
- Complete the remaining checks in `MANUAL_TEST_CHECKLIST.md` and `docs/install_test_checklist.md`.

## Next planned phase

- None defined after Phase 4.
