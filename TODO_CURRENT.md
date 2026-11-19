# FlowShift - Open Development Tasks

## Current phase

Phase 4 - Clipboard Overlay and Command Wheel (target release `v0.7.0`).

## Active phase specification

`docs/phases/phase_4_clipboard_overlay_command_wheel.md`

## Open implementation work

- Full regression per `AGENTS.md`, reconcile `HANDOFF_CURRENT.md`, release
  `v0.7.0` and verify the tag workflow and assets.
- Development update channel: register a Forgejo Actions runner with label
  `flowshift-build` (Linux, node 24, python3, wine + Inno Setup 6 at
  `/opt/innosetup/ISCC.exe`) and enable Actions for
  `private-platform/flowshift`; then confirm the first
  `dev-package` run publishes `v0.7.0-dev.27` with three verified assets.
- Make sure the GitHub publication (`git-sync veroeffentlichen`) never pushes
  the Forgejo `v*-dev.*` tags: `.github/workflows/release.yml` triggers on
  every `v*` tag.

## Open manual hardware and VM tests

- Upgrade over an older installation with `install_flowshift.bat` (no prior
  uninstall): only UAC, FlowShift restarted, old WebGUI replaced, `config.json`
  unchanged; WebGUI, command wheel (Ctrl+Right click) and clipboard overlay
  (Ctrl+Win+V) work immediately (`docs/install_test_checklist.md` J2,
  `MANUAL_TEST_CHECKLIST.md` "Installer, WebGUI and program consistency").
- Windows CI lanes for the installer change: PowerShell parsing,
  `packaging/test_release_packaging.ps1`, `test_update_flowshift.ps1`,
  `test_overlay_lifecycle.py`, WebGUI `npm test` and `npm run build`.

- Run the packaged installer, update, rollback, reinstall, and uninstall checks on a clean Windows x64 VM without Python.
- Repeat installation and update checks on Windows with Python already installed.
- Run the two-device live clipboard matrix for text, HTML, images, files, and a 150-file batch on real or virtual peers.
- Run the Phase 3 two-device transfer V2 matrix in `MANUAL_TEST_CHECKLIST.md` (section "Clipboard Transfer V2"), including LAN drops, sender/receiver/dual restarts, disk-full, legacy peer fallback, and the `v0.5.4` -> `v0.6.3` upgrade.
- Verify disconnect/reconnect recovery and temp cleanup across two peers.
- Run visible overlay lifecycle checks on multi-monitor Windows systems at 100%, 125%, 150%, and 200% scaling.
- Verify on Windows (`0.7.0-dev.24`): busy overlay hotkeys appear as tray notification, Dashboard banner and per-hotkey status in Settings; the configurable clipboard overlay hotkey opens the clipboard window at the mouse and it stays open; no `announcement failed -> local` warning when copying.
- Verify the shaped, see-through Command Wheel and the dismissal rules from `0.7.0-dev.25` on Windows (stays open 30 s, Escape/outside click close with logged reason, action reaches the previous window, edge centering).
- Verify Command Wheel hold and swipe from `0.7.0-dev.26` on Windows (`MANUAL_TEST_CHECKLIST.md` "Command Wheel hold and swipe"): hold hotkey, swipe right, release runs the right sector; release in the middle does nothing; a short tap keeps the wheel open for clicking; the side button chosen in Settings opens the wheel; WebGUI `npm test`/`npm run build` and `test_tray_overlay_actions.py` on Windows.
- Verify the development update channel on Windows (`MANUAL_TEST_CHECKLIST.md`
  "Development update channel (Forgejo)", `docs/install_test_checklist.md` L):
  one-time install of the development Setup on both laptops (dev.22 checkout
  install and 0.3.0), access token stored in the Credential Manager, next
  merge installed automatically, no VPN shown quietly; WebGUI `npm test` /
  `npm run build`, `test_update_flowshift.ps1`, `test_release_packaging.ps1`.
- Decision for Viktor: outside clicks currently close the overlay and also reach the window below; swallowing them (Windows context-menu style) would need a low-level mouse hook in the overlay host.
- Complete the remaining checks in `MANUAL_TEST_CHECKLIST.md` and `docs/install_test_checklist.md`.

## Next planned phase

- None defined after Phase 4.
- Proposal awaiting Viktor's decision (not started): retire the legacy
  Tkinter GUI (`src/python/gui.py`) in three stages. Nothing is removed before
  Viktor approves each stage.
  - Stage 1: the WebGUI covers everything `gui.py` still offers: device name,
    forwarding profiles, runtime start/stop and the live test. Until then
    `gui.py` stays as it is (no new settings, only fixes).
  - Stage 2: once stage 1 is confirmed on Windows, tray and installer no longer
    start `gui.py`; the tray "Settings" entry opens the WebGUI only. The file
    stays in the repository as a fallback for one release.
  - Stage 3: after one release without complaints, remove `gui.py`, its tests
    and the Tk dependency from installer, packaging and docs (installer and
    uninstaller updated in the same task).
