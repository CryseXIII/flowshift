# FlowShift - Open Development Tasks

## Current phase

Phase 4 - Clipboard Overlay and Command Wheel (target release `v0.7.0`).

## Active phase specification

`docs/phases/phase_4_clipboard_overlay_command_wheel.md`

## Open implementation work

- `v0.7.1` is tagged (release commit `8ec6bb0`). If GitHub
  `releases/latest` does not show it yet, finish the publication with
  Viktor's approval (`docs/setup.md` "Publishing a new version for all PCs").
- `v0.7.2` (Viktor 26.11.: ship urgently after the fixes of `0.7.2-dev.1`:
  screen edge with a peer typed in by IP, `GET /api/hotkeys` 500, log
  rotation): after the merge prove the release build green with `python3
  scripts/publish_github_release.py build --version 0.7.2 --vom-main` (also
  runs the WebGUI tests `Dashboard.test.jsx` and
  `RemotePreviewSection.test.jsx` changed here), then publish only with
  Viktor's approval for `v0.7.2`.
- Remote preview stages 2-4 (picture): Viktor wants to test the stream.
  Pairing is now the device-list rule (a peer with device ID in the own
  list, also when typed in and filled in at the first connection); a paired
  peer gets `preview_unavailable`. Capture, image channel and window are not
  built; they need their own task (stage 2-4 below).
- Decision for Viktor (asked 24.11., not started): whether an extra channel
  "Pre-release" (reads GitHub prereleases) should let a second PC test new
  functions without every Stable PC getting them.
- Update from a connected device (`0.7.1-dev.2`): built with these defaults,
  questions for Viktor asked 25.11.:
  - Development versions: a PC on Stable takes a `-dev` version from a
    connected device only with the tick "Also accept development versions"
    (off by default; meant for Viktor's own second PCs, not for other
    people's PCs). Keep it so, or turn it on by default?
  - Until secured pairing ([Karte 722](http://kanban.thegoodhunter.vps/tasks/722))
    a device in the same network that fakes address and device ID of a paired
    PC could offer a package; the checksums only prove a complete transfer.
    Default is therefore "Notify only". Allow "Download, install and restart
    automatically" already now (current state), or only after pairing?
  - Follow-up if wanted: with an unchanged `requirements.txt` the receiver
    now keeps its Python packages (`0.7.1-dev.4`); only when it changed it
    still loads them from PyPI. The sender has no wheels of its own to send
    along (only a USB-installed PC had them, and the installer does not keep
    them); shipping them would need a wheel cache on every PC. Wanted?
- Course and display of an update of a connected PC (`0.7.1-dev.4`, Viktor
  25.11. 17:34): built with these choices, questions for Viktor:
  - "No restart while PC 1 controls PC 2": built as "the install waits until
    you are back on PC 1" (no question "update PC 2 now?" on PC 1). Keep, or
    add the question with a button?
  - Pause after failed attempts: after 2 failed installs of the same version
    automatic attempts stop until Install is clicked on PC 2 or a newer
    version comes. Is 2 right, or rather a timed pause (e.g. 24 h)?
  - With PC 2 on "Notify only" (default) the update still needs a click on
    PC 2; for "no handgrip at PC 2" PC 2 must be set to "Download, install
    and restart automatically" once (see the pairing question above).
- Development update channel: nothing open on the build side; the channel
  installs the merged source state of Forgejo `main` with the scripts (no
  package build, no runner, Viktor 19.11. 22:09). Open are only the Windows
  check steps below.
- Remote screen preview ([Karte 833](http://kanban.thegoodhunter.vps/tasks/833)):
  stage 1 is built (`0.7.0-dev.48`: settings, messages, lifecycle; since
  `0.7.2-dev.1` a peer with device ID in the own device list counts as paired
  and gets `preview_unavailable`, others `pairing_required`). Secured device
  pairing ([Karte 722](http://kanban.thegoodhunter.vps/tasks/722)) replaces
  that rule later; the only exchange point is
  `remote_preview.preview_authorization`:
  - Stage 2: capture and encoding on the target (monitor, cursor crop, fps,
    width) and the `requested -> streaming` transition behind pairing.
  - Stage 3: image channel between the paired peers (no unauthenticated
    channel, no new open port without installer/firewall update).
  - Stage 4: preview window on the source (corner and width from the
    settings), shown while forwarding and closed on return.

## Open manual hardware and VM tests

- Peer typed in by IP and log size (`0.7.2-dev.1`, `MANUAL_TEST_CHECKLIST.md`
  "Test 7: Peer von Hand per IP eingetragen" and "Log-Groesse"): two Windows
  PCs, one entered by hand, edge North switches, hotkeys and Dashboard
  without 500, Dashboard notice on a refused edge, preview answers
  `preview_unavailable`, `flowshift.log` without poll lines and rotated at
  5 MB.

- Verify the `v0.7.1` update on Windows (`0.7.1-dev.3`,
  `MANUAL_TEST_CHECKLIST.md` "Clipboard Shift-click range"): a PC with
  `v0.7.0` on channel Stable updates to `v0.7.1` by itself (or with Install);
  in the clipboard overlay (Ctrl+Alt+V) and in WebGUI `/clipboard` click the
  2nd entry, Shift-click the 5th: entries 2 to 5 are checked, count `4 of N`;
  upwards the same. Settings > Software Update on Stable shows no "Forgejo"
  in the channel list. WebGUI `npm test` and `npm run build`.

- Verify the update from a connected device on two Windows PCs
  (`0.7.1-dev.2`, `MANUAL_TEST_CHECKLIST.md` "Update from a connected
  device", `docs/install_test_checklist.md` O): offer shown with "Notify
  only", nothing transferred before the click; "Install and restart" installs
  and restarts; automatic level announces a minute ahead, waits for a running
  transfer, can be cancelled; network cut resumes; Stable without the tick
  ignores `-dev`; uninstall removes `peer_outbox`. WebGUI `npm test`
  (`PeerUpdatePanel.test.jsx`, `SoftwareUpdateSection.test.jsx`) and
  `npm run build`, PowerShell parsing of `update_flowshift.ps1`,
  `packaging/test_release_packaging.ps1`.

- Verify the course and display of an update of a connected PC on two
  Windows PCs (`0.7.1-dev.4`, `MANUAL_TEST_CHECKLIST.md` "Course and display
  of an update of a connected PC", `docs/install_test_checklist.md` O2):
  PC 1's Dashboard shows each step, PC 2 waits while controlled, edge switch
  during the restart says "is being updated", PC 2 reconnects by itself,
  failure shows the reason and pauses after 2 attempts, unchanged
  `requirements.txt` reuses the venv. WebGUI `npm test` (`Dashboard.test.jsx`)
  and `npm run build`, PowerShell parsing of `install_flowshift.ps1` and
  `update_flowshift.ps1`.

- Verify the USB package with source-only packages on Windows
  (`0.7.0-dev.56`, `docs/install_test_checklist.md` N3): on Viktor's PC
  "Create USB package" builds proxy-tools from source and saves the ZIP; a
  clean Windows VM without Python and network installs it, FlowShift and the
  overlay start.

- Verify the USB package download failures on Windows (`0.7.0-dev.55`,
  `docs/install_test_checklist.md` N2; on Viktor's PC the 22:38 second try
  already got Python through): network cut during the download shows try 2 of
  2 and the cause; "Use downloaded Python installer" with the browser-saved
  file. WebGUI `npm test` (fallback case) and `npm run build`.

- Verify the USB stick package on Windows (`0.7.0-dev.53`,
  `docs/install_test_checklist.md` section N): "Create USB package" on the
  development PC saves `Downloads\FlowShift-0.7.0-dev.53-usb.zip` (real
  python.org and PyPI downloads); install from the unpacked ZIP on a PC
  without Python, Git and Node.js and without network, over an older
  version, with a damaged offline file, and uninstall. WebGUI `npm test`
  (`SoftwareUpdateSection.test.jsx`) and `npm run build`,
  `packaging/test_release_packaging.ps1`.

- Verify the stable channel on a Windows PC without Git, Node.js and Forgejo
  (`0.7.0-dev.52`, `MANUAL_TEST_CHECKLIST.md` "Stable update channel on a PC
  without Git, Node.js and Forgejo", `docs/install_test_checklist.md` L): first
  install from the GitHub `FlowShift-Setup.exe`, then (after the next GitHub
  release) the update with notify (Check/Download/Install by hand, steps row
  and messages), download and install, and once from an installed
  `0.7.0-dev.N` switched to Stable; no Forgejo text on the page. WebGUI
  `npm test` (`SoftwareUpdateSection.test.jsx`) and `npm run build`,
  PowerShell parsing, `packaging/test_release_packaging.ps1`.

- Verify remote preview stage 1 on two Windows PCs (`0.7.0-dev.48`,
  `MANUAL_TEST_CHECKLIST.md` "Remote preview stage 1" and the installer
  line in "Installer, WebGUI and program consistency"): switching per hotkey
  and screen edge and returning unchanged; Settings > Remote Preview saves,
  keeps values after reload and refuses invalid values; with the switch on on
  both PCs the Dashboard shows `Rejected` / `pairing_required` after a switch
  and `Idle` after the return; `flowshift.log` shows
  `remote_preview rejected reason=pairing_required`; no window, no new port;
  install over an older version and uninstall. WebGUI `npm test`
  (`RemotePreviewSection.test.jsx`, `Dashboard.test.jsx`,
  `SettingsPanel.test.jsx`) and `npm run build`,
  `packaging/test_release_packaging.ps1`.

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
- Verify the Web-API origin check and the action handler table on Windows
  (`0.7.0-dev.43`, `MANUAL_TEST_CHECKLIST.md` "Web-API origin check"):
  overlay click and hold-and-swipe still run actions (`flowshift.log` shows
  `action check id=... channel=wheel_swipe|web_api class=L0|L1`), WebGUI
  saves wheel settings and other settings, `install_flowshift.ps1` and the
  update run through, a page on another port cannot trigger `execute` or
  `inject` by `fetch` (403, nothing runs). `test_tray_overlay_actions.py` and
  `packaging/test_release_packaging.ps1` on Windows.
- Verify the one wheel hotkey on Windows (`0.7.0-dev.51`,
  `MANUAL_TEST_CHECKLIST.md` "0.7.0-dev.51, Viktor 24.11."): no "Add another
  hotkey"; wheel hotkey Ctrl+side button 1, "Save Settings", F5: still
  there and it opens the wheel; Clear, "Save Settings", confirm the question,
  F5: field stays empty, one neutral sentence about the tray icon, no red
  text. WebGUI `npm test` (`CommandWheelSection.test.jsx`,
  `SettingsWheelSave.test.jsx`, `SettingsPanel.test.jsx`,
  `HotkeyStatus.test.jsx`) and `npm run build`;
  `test_tray_overlay_actions.py` on Windows.
- Known: `python3 -m unittest test_clipboard_file_preview
  test_tray_remote_preview` fails `test_accept_is_never_followed_before_pairing`
  in this order only (each module alone passes); not caused by dev.50,
  isolate the shared state.
- Verify hotkeys with mouse buttons on Windows (`0.7.0-dev.49`,
  `MANUAL_TEST_CHECKLIST.md` "Hotkeys with mouse buttons"): after the update
  Settings > Command Wheel > Open hotkey shows `Ctrl+Right click` active and
  no red text; click the field, Ctrl+side button 1, save: the wheel opens
  with it (tap and hold/swipe), Ctrl+Right click gives the normal context
  menu; set back to Ctrl+Right click the same way. Clear works without red
  text; a mouse peer hotkey "Return to local" works while forwarding; a
  hotkey set twice is refused with both names. WebGUI `npm test` and
  `npm run build`, `test_tray_overlay_actions.py` on Windows.
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
  the mouse; card copy actions add no new entry; a new copy appears at the
  top. WebGUI `npm test` and `npm run build`,
  `test_tray_overlay_actions.py`.
- Verify the clipboard copy actions on Windows (`0.7.0-dev.45`,
  `MANUAL_TEST_CHECKLIST.md` "Clipboard copy actions"): copy two files in
  Explorer, open the clipboard (hotkey and from the Command Wheel), on the
  entry click `Full path`, `Folder path` and `File name` one after another and
  paste each into an editor: exactly that text (one line per file, the folder
  once), no new entry in the list; `Set` still pastes the files in Explorer;
  `Show files as` is gone. The dev.40/dev.44 checks (stays open, search
  click, drag, newest on top, All incl. pinned, previews) still pass. WebGUI
  `npm test` and `npm run build`.
- Verify copied pictures on Windows (`0.7.0-dev.47`,
  `MANUAL_TEST_CHECKLIST.md` "Clipboard copied pictures"): in the browser
  right-click a picture, `Copy image`, open the clipboard (overlay and WebGUI
  `/clipboard`): the entry shows the picture as a thumbnail, a transparent PNG
  on the checkerboard; `Set` and paste into Paint inserts the picture, paste
  into Word/a browser field keeps the transparency; a Snipping Tool cut-out
  shows its thumbnail too; a 4K/8K screenshot appears without the list
  stalling; no second entry appears after `Set`. `flowshift.log` shows
  `clipboard captured image WxH image/png` and `clipboard image preview
  ready`. WebGUI `npm test` (`ClipboardFilePreviews.test.jsx`) and
  `npm run build`.
- Verify the clipboard file previews on Windows (`0.7.0-dev.44`,
  `MANUAL_TEST_CHECKLIST.md` "Clipboard file previews"): copy a GIF, a
  photo, a video and a text file in Explorer (one at a time and together),
  open the clipboard overlay and the WebGUI `/clipboard`: the GIF moves, photo
  and video show a thumbnail, the video plays a short muted clip on hover, the
  text file shows its Windows icon; the batch card shows four tiles and `+N`;
  move or delete a copied file: neutral tile, no error; scrolling stays smooth;
  `Animated File Previews` = Hover stops the GIF until the mouse is over the
  card. WebGUI `npm test` (`ClipboardFilePreviews.test.jsx`, overlay and view
  tests) and `npm run build`, `packaging/test_release_packaging.ps1`.
- Verify the WebGUI look on Windows (`0.7.0-dev.46`, `MANUAL_TEST_CHECKLIST.md`
  "WebGUI warnings, buttons and fields"): Dashboard and Settings show no red
  hotkey warning when only Ctrl+Win+V is taken (neutral grey hint) and none
  for Win+V while `Intercept Win+V` is off; Settings > Command Wheel pages:
  every slot shows `Up`/`Down` with arrow and tooltip; dropdowns, number
  fields, the Forgejo token field (`stored`) and the Inject text field look
  like the rest (dark, accent border on focus, lighter border on hover, dark
  option list); clear gaps between the Settings sections. WebGUI `npm test`
  and `npm run build`.
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
