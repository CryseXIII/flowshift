# FlowShift - Open Development Tasks

## Current phase

Phase 3 - Clipboard Transfer Hardening.

## Active phase specification

`docs/phases/phase_3_clipboard_transfer_hardening.md`

## Open implementation work

- Remaining Phase 3 slices: sender restart re-offer of outgoing journals and
  legacy `TransferJob` mirroring for GUI progress (decide keep/skip against the
  spec), fault injection and stress validation of the V2 transport, two-peer
  localhost end-to-end run through `tray.py`, documentation closure, full
  regression, and the stable `v0.6.0` release.

## Open manual hardware and VM tests

- Run the packaged installer, update, rollback, reinstall, and uninstall checks on a clean Windows x64 VM without Python.
- Repeat installation and update checks on Windows with Python already installed.
- Run the two-device live clipboard matrix for text, HTML, images, files, and a 150-file batch on real or virtual peers.
- Verify disconnect/reconnect recovery and temp cleanup across two peers.
- Run visible overlay lifecycle checks on multi-monitor Windows systems at 100%, 125%, 150%, and 200% scaling.
- Complete the remaining checks in `MANUAL_TEST_CHECKLIST.md` and `docs/install_test_checklist.md`.

## Next planned phase

- Phase 4 is not started and must not begin automatically.
