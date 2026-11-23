# FlowShift - Open Development Tasks

## Current phase

Phase 4 - Clipboard Overlay and Command Wheel (target release `v0.7.0`).

## Active phase specification

`docs/phases/phase_4_clipboard_overlay_command_wheel.md`

## Open implementation work

- Full regression per `AGENTS.md`, reconcile `HANDOFF_CURRENT.md`, release
  `v0.7.0` and verify the tag workflow and assets.
- Development update channel: nothing open on the build side; the channel
  installs the merged source state of Forgejo `main` with the scripts (no
  package build, no runner, Viktor 19.11. 22:09). Open are only the Windows
  check steps below.

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
- Verify on Windows (`0.7.0-dev.36`; dev.30-dev.34 still showed the square
  live, dev.34 as a #202020 rounded rectangle): the Command Wheel, now drawn
  natively in a layered window, shows only the ring plus the two page dots in
  the hub (Viktor 20.11.: dots stay), no rectangle, clicks next to the ring
  reach the program below; hover, mouse-wheel paging, click and hold-and-swipe
  run the actions as before. `flowshift.log` must show `overlay native wheel
  ready ... paint=updatelayeredwindow`, `overlay native wheel shown ...` and
  `overlay pixel check mode=command_wheel verdict=ring_only
  strategy=native_layered ... clear=5/5 clickthrough=5/5` (four `point=corner_*`
  lines with `result=clear source=below`, `shown=` equal to `below=`, not
  #202020). Clipboard: `verdict=panel_only` (still WebView2, now with
  `corner=set:0x0` in the `overlay webview background` line). If
  `overlay native wheel unavailable|show failed` appears, the reason in that
  line decides the next step (`MANUAL_TEST_CHECKLIST.md` "Command Wheel on
  the desktop").
- Verify clipboard multi-select on Windows (`0.7.0-dev.41`,
  `MANUAL_TEST_CHECKLIST.md` "Clipboard overlay and WebGUI"): in the overlay
  (Ctrl+Alt+V) and WebGUI `/clipboard` check several items (checkbox,
  Ctrl-click, Shift-click range), icon buttons `All`, `Invert`, `None`
  (tooltip on hover, easy to hit); the trash-can `Delete selected` with count badge removes
  them at once without any question; with pinned entries in the list `All`
  checks every entry, Recent and Pinned (count `M of M`, Pinned tab checked
  too), and All + Delete leaves Recent and Pinned empty, also after a
  FlowShift restart; Ctrl+V still pastes the former `Current` content. WebGUI
  `npm test` and `npm run build`.
- Verify on Windows (`0.7.0-dev.42`, `MANUAL_TEST_CHECKLIST.md` "Web-API
  request guard"): after the update open the WebGUI, change a setting (it
  stays after reload) and run a Command Wheel action (Ctrl+Right click, click
  a sector, the action happens); delete one clipboard item in the WebGUI.
  `flowshift.log` shows no `web_api rejected` line for these. WebGUI
  `npm test` and `npm run build`.
- Verify the clipboard overlay fixes on Windows (`0.7.0-dev.40`,
  `MANUAL_TEST_CHECKLIST.md` "Clipboard overlay fixes"): Clipboard from the
  Command Wheel stays open; click into the search and type, it stays open;
  drag the window and release anywhere, it stays there and no longer follows
  the mouse; `Show files as` Folder/Name/Path changes the file cards in the
  overlay and the WebGUI and is kept; card `Copy` adds no new entry; a new
  copy appears at the top. WebGUI `npm test` and `npm run build`,
  `test_tray_overlay_actions.py`.
- Verify the clipboard file previews on Windows (`0.7.0-dev.43`,
  `MANUAL_TEST_CHECKLIST.md` "Clipboard file previews"): copy a GIF, a
  photo, a video and a text file in Explorer (one at a time and together),
  open the clipboard overlay and the WebGUI `/clipboard`: the GIF moves, photo
  and video show a thumbnail, the video plays a short muted clip on hover, the
  text file shows its Windows icon; the batch card shows four tiles and `+N`;
  move or delete a copied file: neutral tile, no error; scrolling stays smooth;
  `Animated File Previews` = Hover stops the GIF until the mouse is over the
  card. WebGUI `npm test` (`ClipboardFilePreviews.test.jsx`, overlay and view
  tests) and `npm run build`, `packaging/test_release_packaging.ps1`.
- Verify on Windows (`0.7.0-dev.37`, `MANUAL_TEST_CHECKLIST.md` "Command
  Wheel behavior and actions"): move the mouse quickly over several sectors,
  the gaps and the hub, on every page and with hold-and-swipe: only the
  sector under the mouse turns blue, all others look as with no hover, nothing
  fades or flickers. WebGUI `npm test` (`CommandWheel.test.jsx`,
  `OverlayShell.test.jsx`) and `npm run build`.
- Verify the development update channel on Windows (`MANUAL_TEST_CHECKLIST.md`
  "Development update channel (Forgejo)", `docs/install_test_checklist.md` L):
  one-time install of `0.7.0-dev.29` or newer from the Forgejo ZIP of `main`
  on both laptops (dev.22 checkout install and 0.3.0; Node.js 24+ needed for
  the WebGUI build), channel Development + access token in the WebGUI, next
  merge installed automatically from the source archive (update.log,
  rollback on error), no VPN shown quietly; WebGUI `npm test` /
  `npm run build`, `test_update_flowshift.ps1`, `test_release_packaging.ps1`.
- Verify on Windows (`0.7.0-dev.31`, `MANUAL_TEST_CHECKLIST.md` "Development
  update channel (Forgejo)", `docs/install_test_checklist.md` L): install
  dev.31 once by hand over dev.29/dev.30 (their updater still has the
  "access denied" bug); the next merge then installs by itself: `update.log`
  shows `administrator True, working directory ...\ProgramData\FlowShift\updates`
  and `result: success`, Software Update shows the new version under
  Installed and no error, no `update_plan.json` left in
  `ProgramData\FlowShift\updates`; the desktop shortcut `FlowShift` starts
  FlowShift without UAC; `test_update_flowshift.ps1`, WebGUI `npm test` /
  `npm run build`.
- Verify on Windows (`0.7.0-dev.35`, `docs/install_test_checklist.md` L
  "Never two installations at once"): install dev.35 once by hand; later, with
  a newer FlowShift running, run `install_flowshift.bat` from the dev.35
  folder: FlowShift dev.35 installed and running, program folder complete, no
  `FlowShift.staging`/`FlowShift.previous` left, the window names the newer
  version; the next regular check (about 15 minutes later) installs the newer
  version with `result: success`. Also: manual install waits for a running
  update; an update during a manual install changes nothing (`install_busy`);
  uninstall removes `FlowShift.previous`/`.staging`/`.failed-*`.
  `test_update_flowshift.ps1`, `test_release_packaging.ps1`, WebGUI
  `npm test` / `npm run build`.
- Verify the installer Python search from `0.7.0-dev.28` on Viktor's laptop (Python 3.13.2 and 3.15.1 installed, FlowShift `0.7.0-dev.22`): `install_flowshift.bat` over the old installation finds Python without download, `install.log` lists every `python candidate` with exit code and output and the PowerShell host; run `test_install_python_probe.ps1` (and `-RealWebGuiBuild`) with `powershell.exe` and `pwsh.exe` (`docs/install_test_checklist.md` J3).
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
