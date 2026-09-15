# FlowShift - Open Development Tasks

## Current phase

Phase 4 - Clipboard Overlay and Command Wheel (target release `v0.7.0`).

## Active phase specification

`docs/phases/phase_4_clipboard_overlay_command_wheel.md`

## Open implementation work

- Implement the required one-card-per-copy Clipboard presentation in both the
  overlay and WebGUI: type-specific text, single-file and batch cards; separate
  Recent/Pinned views; safe copyable names/path lists; previews and item actions.
- Expose a bounded public Clipboard item contract without private absolute paths
  and extend transfer telemetry for independent current-file/total progress,
  `HH:mm:ss` ETA, file ordinal/count and last-file duration.
- Preserve repeated local copy events as distinct history cards while keeping a
  multi-file selection in one batch item; add productive lifecycle tests.
- Full regression per `AGENTS.md`, reconcile `HANDOFF_CURRENT.md`, release
  `v0.7.0` and verify the tag workflow and assets.

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
