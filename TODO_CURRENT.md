# FlowShift - Open Development Tasks

## Current phase

No active implementation phase. Phase 3 - Clipboard Transfer Hardening is
complete with the stable release `v0.6.3`.

## Active phase specification

None. The last completed specification is
`docs/phases/phase_3_clipboard_transfer_hardening.md`.

## Open implementation work

- None. Phase 4 must be requested explicitly before any implementation starts.

## Open manual hardware and VM tests

- Run the packaged installer, update, rollback, reinstall, and uninstall checks on a clean Windows x64 VM without Python.
- Repeat installation and update checks on Windows with Python already installed.
- Run the two-device live clipboard matrix for text, HTML, images, files, and a 150-file batch on real or virtual peers.
- Run the Phase 3 two-device transfer V2 matrix in `MANUAL_TEST_CHECKLIST.md` (section "Clipboard Transfer V2"), including LAN drops, sender/receiver/dual restarts, disk-full, legacy peer fallback, and the `v0.5.4` -> `v0.6.3` upgrade.
- Verify disconnect/reconnect recovery and temp cleanup across two peers.
- Run visible overlay lifecycle checks on multi-monitor Windows systems at 100%, 125%, 150%, and 200% scaling.
- Complete the remaining checks in `MANUAL_TEST_CHECKLIST.md` and `docs/install_test_checklist.md`.

## Next planned phase

- Phase 4 is not started and must not begin automatically.
