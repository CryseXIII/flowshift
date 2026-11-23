# FlowShift — Manual Test Checklist

## Automated checks first

```
python -m compileall -q src/python
python src/python/test_service.py
python src/python/test_clipboard.py
python src/python/test_clipboard_files.py
python src/python/test_clipboard_image.py
python src/python/test_clipboard_html.py
python src/python/test_clipboard_gif.py
python src/python/test_clipboard_sync.py
python src/python/test_clipboard_transfer.py
python src/python/test_clipboard_streaming.py
python src/python/test_diagnostics.py
python src/python/test_overlay_foundation.py
python src/python/test_overlay_lifecycle.py
python src/python/test_version_config.py
python src/python/test_update_client.py
python src/python/test_update_manager.py
python src/python/test_update_safety.py
python src/python/test_update_runtime.py
python src/python/test_update_handoff.py
python src/python/test_update_stress.py
python src/python/test_web_api_updates.py
python -m unittest src/python/test_update_channel.py
python src/python/worker_smoke_test.py
python src/python/e2e_test.py
python src/python/reconnect_stress_test.py 30
python src/python/overlay_ipc_stress_test.py
python src/python/overlay_show_hide_stress_test.py
cd webgui
npm ci --include=dev
npm test
npm run build
cd ..
powershell -NoProfile -ExecutionPolicy Bypass -File packaging/test_release_packaging.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File packaging/build_release.ps1 -Tag v0.4.0
```

> The worker smoke test catches silent worker crashes (e.g. a missing import in
> `forward_loop`): it starts a real runtime, checks `forward_loop`/`inject_loop`
> are alive, verifies a fake peer actually receives forwarded input, and checks
> overlay startup, local routing, honest remote rejection and shutdown.

> `test_overlay_lifecycle.py`, the integration tests and both overlay stress
> tests require Windows. GIF tests skip cleanly without Pillow. The official
> installation includes Pillow 13.0.0 and `pywebview==6.2.1`.

> Installer / uninstaller manual tests: see `docs/install_test_checklist.md`.
> Every installer-relevant change is checked at least
> with "install over an older installation" (section J2) and "clean uninstall"
> (section I) there.

## Phase 1 Overlay Host

### Visible lifecycle
- [ ] Start the WebGUI and runtime, then run
  `python src/python/overlay_show_hide_stress_test.py --visible`.
- [ ] The React diagnostic shell appears without a browser frame, remains
  topmost, alternates `clipboard`/`command_wheel`, and hides cleanly.
- [ ] The host PID is reused during normal cycles and changes exactly after the
  forced crash/recovery step.
- [ ] Escape hides the overlay without stopping the runtime.
- [ ] After completion, no `overlay_host.py` process remains.

### Multi-monitor and DPI
- [ ] Test cursor placement on the primary and every secondary monitor.
- [ ] Include a monitor left of or above the primary so coordinates are negative.
- [ ] Test Windows scaling at 100%, 125%, 150% and 200%.
- [ ] The overlay stays fully inside the selected monitor's work area and does
  not jump to the primary display.
- [ ] Repeat show/hide while moving between monitors; no stale size or position.

### Failure isolation and diagnostics
- [ ] Open WebGUI Diagnostics and run Overlay Ping, Show Clipboard Diagnostic,
  Show Command Wheel Diagnostic and Hide.
- [ ] Runtime status reports process, IPC, ready, visible, mode, restart count
  and last error independently.
- [ ] Kill only `overlay_host.py`, wait for restart eligibility, and request the
  overlay again. Forwarding, networking and clipboard workers remain healthy.
- [ ] Activate forwarding and request an overlay. Phase 1 must report remote
  targets as unsupported and must not display the local overlay.

## Phase 4 Clipboard Overlay and Command Wheel

### Global triggers and dismissal
- [ ] With forwarding inactive, hold Ctrl and right-click in Explorer, a browser,
  an editor and on the desktop. The Command Wheel opens centered at that cursor
  position; the underlying application receives neither right-button down nor up.
- [ ] Right-click without Ctrl still opens each application's normal context menu.
- [ ] While forwarding is active, Ctrl+RightClick is sent to the peer and does
  not open a local wheel.
- [ ] Copy something, then press `Ctrl+Alt+V` (or the configured overlay
  hotkey) and `Ctrl+Win+V`: the Clipboard Overlay opens as its own frameless
  rounded window at the mouse with the copied entry on top, and stays open until
  an entry is clicked (it becomes the Windows clipboard), a click outside or
  `Escape`. Optional `Win+V` does so only with `intercept_win_v=true`.
- [ ] Hotkey taken: occupy a combination (e.g. keep Windows' Win+V, or start a
  program that owns Ctrl+Alt+Space) and start FlowShift. A tray notification
  "FlowShift: hotkey taken" names the busy combination and what works instead;
  the WebGUI Dashboard shows the banner and the "Overlay Hotkeys" list; Settings
  > Clipboard and Settings > Command Wheel show active/busy next to each hotkey.
  `flowshift.log` has `RegisterHotKey <slot> <hotkey> failed err=1409`.
- [ ] Change the clipboard overlay hotkey in Settings > Clipboard (e.g.
  Ctrl+Shift+V), save: the new hotkey opens the overlay immediately; choosing the
  wheel hotkey is refused with a plain-text message.
- [ ] Copy files with clipboard enabled and no peer connected: `flowshift.log`
  has `clipboard captured N file(s) -> local` and no
  `clipboard announcement failed -> local`.
- [ ] Open either overlay and touch nothing for 30 seconds: it stays open, also
  when the previously active window kept the focus.
- [ ] `Escape` and clicking outside close either overlay; `flowshift.log` shows
  `overlay_hidden reason=escape` / `reason=outside_click` with the mode. A
  hidden clipboard overlay stops requesting `/api/clipboard/*`. Reopening uses
  the new cursor location; an already open overlay does not follow the cursor.
- [ ] Command Wheel on the desktop and over a bright window: only the ring
  sectors are visible; hub, corners and gaps show the background, no frame,
  title bar or rectangular shadow. Clicking in the hub or a gap closes it.
- [ ] (`0.7.0-dev.30`) No dark square around the wheel at 100 % and 125-200 %
  scaling: a click just outside the outer ring and in a corner of where the
  square was reaches the program below. Clipboard overlay: no dark corners
  outside the rounded panel. `flowshift.log` has no
  `overlay host shape ... failed` / `missing after show` line.
- [ ] (`0.7.0-dev.32`) Same check after the WebView2 background fix: only the
  ring and the two page dots in the hub are visible, the rest of the hub and
  the corners are see-through. `flowshift.log` shows
  `overlay webview control path=native.browser.web_view`,
  `overlay webview background transparent=ok alpha=0`,
  `overlay shape set mode=command_wheel ... setwindowrgn=1 region=3` and
  `overlay shape after show mode=command_wheel kept`; the same lines with
  `mode=clipboard` after opening the clipboard overlay.
- [ ] (`0.7.0-dev.33`) dev.32 still showed the square. Open the wheel once on
  the desktop: only the ring and the two hub dots are visible, a click into a
  former corner reaches the program below; same for the clipboard overlay
  (only the rounded panel). `flowshift.log` shows
  `overlay webview background transparent=ok ui_thread=1 alpha=0
  form_key=#111827/a255 layered=1`, `overlay shape set ... children=N [...:ok]`
  and the `overlay window top|child ...` list.
- [ ] (`0.7.0-dev.34`) dev.33 still showed the square although every call
  reported success. Open the wheel once on the desktop and leave it open for
  one second: only the ring and the two hub dots are visible. `flowshift.log`
  shows `overlay webview background transparent=ok strategy=alpha ui_thread=1
  alpha=0 form=#000000/a255 form_key=#000000/a0 layered=0 blur=ok` and, about
  0.4 s after the show, one `overlay pixel mode=command_wheel point=...` line
  per measured point plus `overlay pixel check mode=command_wheel
  verdict=ring_only ... clear=5/5 clickthrough=5/5 drawn=2/2`. With
  `verdict=square_visible` the `shown=`/`source=` values of the corner points
  name the colour on screen (`form_111827`, `black`, `painted`). Same for the
  clipboard overlay (`verdict=panel_only`). Without opening anything by hand:
  `POST /api/overlay/probe` with `{"mode":"command_wheel"}` or
  `{"mode":"clipboard"}` opens the overlay at the cursor and triggers the
  same measurement.
- [ ] (`0.7.0-dev.36`) dev.34 still showed a #202020 rounded rectangle with a
  thin light border. The wheel is now drawn natively (layered window,
  `UpdateLayeredWindow`). Open it on the desktop over a bright wallpaper:
  only the ring and the two hub dots are visible, no rectangle, no border,
  no rounded frame; hover lights a sector, the mouse wheel changes the page,
  a click runs the action, hold-and-swipe still works, a click into a former
  corner reaches the program below. `flowshift.log` shows
  `overlay native wheel ready hwnd=... exstyle=0x00080088 corner=set:0x0/read:1
  ... paint=updatelayeredwindow`, `overlay native wheel shown ...`,
  `overlay shape native mode=command_wheel ... region=none` and
  `overlay pixel check mode=command_wheel verdict=ring_only
  strategy=native_layered ... clear=5/5 clickthrough=5/5`; each
  `point=corner_*` line has `shown=` equal to `below=` (`source=below`).
  `overlay native wheel unavailable (...)` or `show failed (...)` means the
  WebView2 fallback was used; the text in brackets is the cause.
- [ ] Ctrl+RightClick close to each screen edge and corner: the whole ring is
  visible and the cursor sits in the hub.

### Command Wheel hold and swipe
- [ ] Hold `Ctrl+Alt+Space` (or the configured hotkey), move the mouse to the
  right and release: the action of the right sector runs in the previously
  active window (Notepad: e.g. Paste); `flowshift.log` has
  `command wheel release source=hotkey ... outcome=execute action=<id>` and
  `overlay_hidden reason=swipe`.
- [ ] While holding, the sector in the mouse direction is highlighted, also
  when the pointer is far beyond the ring; the mouse wheel still flips pages.
- [ ] Hold and release in the middle: nothing happens, the wheel closes
  (`reason=swipe_center`).
- [ ] Tap the hotkey briefly: the wheel stays open for clicking until a click,
  a click outside or Escape.
- [ ] Same with Ctrl+Right click held and swiped: the action runs and no
  context menu appears.
- [ ] WebGUI Settings > Command Wheel: choose a mouse side button, save; that
  button opens the wheel (tap and hold/swipe); the other side button still
  navigates back/forward in the browser; with "Not used" both navigate again.
- [ ] Change "Tap or hold after" (e.g. 600 ms) and switch off "Tap to open" or
  "Hold and swipe" separately: the behavior follows; switching off both is
  refused.
- [ ] While forwarding to another device, neither the hotkey, Ctrl+Right click
  nor the side button opens a local wheel.

### Command Wheel behavior and actions
- [ ] The configured hotkey (default `Ctrl+Alt+Space`) opens the same wheel as
  global Ctrl+RightClick.
- [ ] A page renders no more than eight radial sectors. The active page dot is
  white; mouse-wheel up/down cycles through all pages and wraps at both ends.
- [ ] Hover one sector. Only that sector turns blue; every other sector looks
  exactly as with the mouse over no sector (same dark fill, same opacity).
  Move the mouse quickly across several sectors, the gaps and the hub: the
  highlight jumps at once, nothing fades, flickers or turns see-through. Same
  on every page and while holding the trigger and swiping. Moving away clears
  the highlight.
- [ ] In Notepad, verify Copy, Paste, Cut, Delete, Select all, Undo and Redo.
  The overlay closes, Notepad regains focus and receives exactly the action.
- [ ] Clipboard opens the Clipboard Overlay. Sync clipboard sends the manifest
  for the active/first profile; with no profile it returns a visible refusal.
- [ ] Edit pages, slot order and hotkey under WebGUI Settings, save, reopen and
  verify the saved layout and newly registered hotkey.

### Clipboard overlay and WebGUI
- [ ] The overlay has a fixed-height scrollable list at the cursor. Set/Get,
  Pin/Unpin, Delete and Sync call the expected operation and remain responsive.
- [ ] Multi-select (`0.7.0-dev.41`), in the overlay and in WebGUI `/clipboard`:
  each card has a checkbox; Ctrl-click a card toggles it, Shift-click selects
  the range from the last clicked card. The bar under Recent/Pinned shows
  `N of M selected` as text and four icon buttons (hover shows the tooltip,
  each easy to hit); the trash can `Delete selected` shows the count as a
  badge and is greyed out with nothing selected. With pinned entries in the
  list, `All` (Ctrl+A) checks every entry, Recent and Pinned: `M of M
  selected` counts all entries, and the Pinned tab shows its entries checked
  too (switching tabs keeps the selection). `Invert` (Ctrl+I) flips every
  entry of both tabs, `None` (Ctrl+Shift+A) clears both. `Delete N` (or the Delete key)
  removes them at once: no question, no dialog, no second click, also for 100+
  items and for checked pinned items. Ctrl/Shift clicks and checkbox clicks
  never set the Windows clipboard; double-click on a card and `Set` still do.
  `flowshift.log` shows `clipboard delete selected profile=... requested=N
  deleted=N`. All + Delete with pinned entries leaves Recent and Pinned empty
  (`Recent 0`, `Pinned 0`), and both stay empty after restarting FlowShift. Deleting the item marked `Current` leaves the
  Windows clipboard content unchanged (Ctrl+V still pastes it); it only
  reappears in the history when it is copied again. The WebGUI has no
  separate `Clear` button any more (Clear = All + Delete).
- [ ] Clipboard overlay fixes (`0.7.0-dev.40`, Viktor 23.11.):
  - Command Wheel `Clipboard` (click and hold-and-swipe): the clipboard opens
    and stays open; `flowshift.log` shows no `overlay_hidden reason=focus_lost`
    or `outside_click` right after the show.
  - Click into the search field and type: the overlay stays open, the list
    filters. Clicks on the profile list, the `Show files as` list, buttons and
    cards never close it; a click on another program still does.
  - Drag the overlay by its grip `⠿`, the header, the tab row or the footer,
    release anywhere (also over another program, also after a very fast
    move): the window stays where it was released and no longer follows the
    mouse. Escape or Alt+Tab during the drag ends it as well. `flowshift.log`
    shows `overlay drag end reason=release|escape|focus_lost moves=N at=x,y`.
    Clicking into the search field after moving still keeps it open.
  - `Show files as` (overlay tab row and WebGUI `/clipboard` list header):
    `Folder`, `Name`, `Path` change every file card at once (files copied
    loose from `C:\...\Berichte` show `Berichte`, `a.txt`, `Berichte/a.txt`);
    the choice survives reopening the overlay and a FlowShift restart and is
    the same in both views. The card's small `Copy` button copies exactly
    what is shown; Ctrl+V in Notepad pastes it.
  - After pressing `Copy` no new entry appears in the list (Recent count
    unchanged, `Current` unchanged).
  - Copy something new (text or files): it appears at the very top of Recent,
    in the overlay and in the WebGUI.
- [ ] Scroll well down in the overlay and in WebGUI `/clipboard`; trigger item
  polling, SSE refresh and active transfer-progress refresh. The same list DOM
  node remains and the visible scroll position does not jump to the top.
- [ ] During a stream V2 file batch, both views show independent current-file
  and total bars with concrete percent, bytes, rate, `HH:mm:ss` ETA, one-based
  file ordinal/count and last-file duration. A single file has one file bar;
  text and other singleton items have one total bar; completion removes progress.
- [ ] Switch profiles while an old request is delayed. Data from the previous
  profile must never overwrite the newly selected profile.
- [ ] Test wheel and clipboard placement on every monitor at 100%, 125%, 150%
  and 200% scaling, including negative virtual-desktop coordinates.

## Edge Switching WebGUI

### Test 1: Display Settings speichern
- WebGUI oeffnen.
- Display Settings oeffnen.
- Edge Switching aktivieren.
- East edge -> Surface setzen.
- Threshold, Inset, Cooldown setzen.
- Speichern.
- Browser neu laden.
- Erwartung: Einstellungen bleiben sichtbar.

### Test 2: A -> B ueber rechte Kante
- Runtime A und B starten.
- A WebGUI zeigt B als Peer.
- Maus auf A ueber rechte Kante bewegen.
- Erwartung:
  - A loggt edge trigger east.
  - A verbindet zu B, falls noch nicht verbunden.
  - A sendet edge_enter.
  - B setzt Cursor links.
  - A aktiviert Forwarding erst nach edge_enter_ack.
  - Maus/Tastatur funktionieren auf B.

### Test 3: B -> A zurueck ueber linke Kante
- Auf B Maus ueber linke Kante zurueckfuehren.
- Erwartung:
  - B sendet edge_return.
  - A deaktiviert Forwarding.
  - A setzt Cursor nahe rechter Kante.
  - Beide Sessions sind beendet.

### Test 4: Wiederholung
- Test 2 und 3 zehnmal wiederholen.
- Kein Flackern.
- Keine haengenden Tasten.
- Kein Zombie-Prozess.
- Logs bleiben lesbar.

### Test 5: Peer nicht verbunden
- Runtime A starten.
- Runtime B starten, aber Verbindung vorher nicht manuell aktivieren.
- Maus A ueber Edge.
- Erwartung:
  - A verbindet automatisch zu B.
  - Edge-Session startet erst nach Verbindung und Ack.

### Test 6: Disconnect waehrend Edge-Session
- Waehrend A -> B aktiv ist, B Runtime stoppen oder Netzwerk trennen.
- Erwartung:
  - A deaktiviert Forwarding.
  - A behaelt lokale Kontrolle.
  - Keine Taste/Mausbutton haengt.
  - Session wird gecancelt.

## Windows Live Clipboard Tests

1. Start the runtime with `python src/python/tray.py --tray`. PASS: control socket answers; FAIL: no `status` reply; log: `src/python/flowshift.log`.
2. Run `python src/python/flowshift_diagnose.py`. PASS: readable report shows version, runtime, clipboard, storage, dependencies, worker and problems sections; FAIL: report missing or unreadable; logs: `src/python/flowshift.log`, `src/python/flowshift_runtime.out`.
3. Check the report fields. PASS: clipboard root/temp/object dirs, Pillow, free disk, limits and runtime health are present; FAIL: fields missing or obviously wrong; logs: `src/python/flowshift.log`.
4. Run `python src/python/clipboard_live_test.py --quick --yes`. PASS: `TEXT PASS`, `HTML PASS`, `IMAGE PASS`, `FILES PASS`, `FILE_BATCH PASS`; FAIL: any line prints `FAIL`; logs: `src/python/flowshift.log`.
5. Run `python src/python/clipboard_live_test.py --with-runtime --yes`. PASS: it prints `RUNTIME REACHABLE` or `RUNTIME NOT REACHABLE` and still completes the clipboard tests; FAIL: clipboard tests abort early; logs: `src/python/flowshift.log`, `src/python/flowshift_runtime.out`.
6. Run `python src/python/clipboard_live_test.py --large-files 150 --file-size-mb 1 --yes` only when explicitly testing the large path. PASS: `LARGE_FILE_BATCH PASS`; FAIL: file batch fails or hangs; logs: `src/python/flowshift.log`.
7. Run `python src/python/clipboard_live_test.py --out "%TEMP%\FlowShiftLiveTest" --keep-files --yes` when you want to inspect the generated files. PASS: files remain in the chosen output directory; FAIL: files disappear or the run errors; logs: `src/python/flowshift.log`.
8. Open the Clipboard Overlay with `Ctrl+Alt+V` (or the configured overlay hotkey) and `Ctrl+Win+V`. PASS: the React overlay opens at the cursor, is interactive and stays open; a hotkey that Windows already owns is reported in the tray and WebGUI instead of silently doing nothing; FAIL: nothing opens without any notice, or a Tkinter window appears; logs: `src/python/flowshift.log`.
9. Test `Win+V` only if `intercept_win_v=true`. PASS: FlowShift opens the Clipboard Overlay instead of the OS history; FAIL: the OS history opens or the shortcut does nothing; logs: `src/python/flowshift.log`.
10. Two-device check Laptop -> Surface. PASS: copied Text, HTML, Image, 10 Files and 150 small files appear on the target and can be pasted; FAIL: items missing, progress stuck, or paste fails; logs: `src/python/flowshift.log`, `src/python/flowshift_runtime.out`.
11. Review logs after the run. PASS: no worker crashes, no `disk_full`, no `hash_mismatch`; FAIL: any worker crash or transfer error appears; logs: `src/python/flowshift.log`, `src/python/flowshift_runtime.out`.
12. Verify temp cleanup. PASS: fresh materialized files survive long enough to paste and old temp files are removed later; FAIL: files vanish too early or stale temp files never clear; logs: `src/python/flowshift.log`.

---

## Clipboard Transfer V2 (two devices, Phase 3)

Preparation: both peers run `v0.6.3`, `clipboard_transfer_v2_force_legacy=false`.
Status is read from `/api/clipboard/status` (`stream_v2` section) or the
control socket `diagnostics`. Data roots: `<data>/clipboard/objects`,
`<data>/clipboard/incoming`, `<data>/clipboard/journals`. Expected end state
for every completed case: journals `completed`, no `.part` under `incoming`,
`transfer_activity_state().busy == false`, pasted tree byte-identical (compare
with `Get-FileHash`). Logs: `flowshift.log` must not contain absolute source
paths of the copying device.

| # | Case | Steps | PASS | FAIL |
|---|------|-------|------|------|
| 1 | Small file | copy a 4 KiB file, paste on peer | file identical, `strategy=stream_v2` in status | legacy ZIP used, hash differs |
| 2 | Large file | copy a 2 GiB file | progress, rate and ETA update; RAM of the runtime stays flat (< +150 MiB) | RAM grows with file size, transfer stalls |
| 3 | 150-file batch | copy 150 x 1 MiB (`clipboard_live_test.py --large-files 150`) | all files pasted, one manifest item | missing files, timeout |
| 4 | Nested folders | copy a folder with 4 levels | tree identical incl. empty leaves | flattened or missing dirs |
| 5 | Unicode | names with umlauts, CJK, emoji, spaces | names identical on peer | renamed or rejected |
| 6 | Zero-byte file | copy an empty file inside a batch | file exists with size 0 | file missing |
| 7 | Empty directory | copy an empty folder | empty folder created on paste | folder missing |
| 8 | Cancel during transfer | invoke cancellation through the V2 control/test harness at ~50 % | both sides `cancelled`, stage purged, no `.part` | orphan partials, busy stays true |
| 9 | LAN drop at 10 % | pull the cable/Wi-Fi at ~10 %, reconnect within 60 s | `waiting_reconnect` then resume, `retry_count=1`, `resume_bytes` > 0, no bytes re-sent | restart from 0 or failed |
| 10 | LAN drop at 50 % | same at ~50 % | as 9 | as 9 |
| 11 | LAN drop at 90 % | same at ~90 % | as 9, completion after reconnect | as 9 |
| 12 | Sender restart | kill the sender runtime mid-transfer, start it again | sender re-offers on reconnect, receiver resumes from durable offsets | new transfer from 0, orphan journal |
| 13 | Receiver restart | kill the receiver runtime mid-transfer, start it again | receiver sends resume request, transfer completes | receiver stage lost, restart from 0 |
| 14 | Both restarts | kill both mid-transfer, start both | transfer completes after reconnect | stuck `waiting_reconnect` |
| 15 | Source changed | append to a source file during transfer | transfer fails with `source_changed`, nothing published on peer | corrupted item published |
| 16 | Source deleted | delete a source file during transfer | `source_missing`, receiver journal purged | hang |
| 17 | Target disk nearly full | free space < preflight need | offer rejected with `insufficient_space`, no stage created | partial written |
| 18 | Target disk fills during transfer | fill the disk mid-transfer, then free space | `failed(disk_full)` journal, resume request accepted after space is freed | stage deleted, no resume |
| 19 | Cache on | `cache_received_payloads=true`; paste twice | second paste hardlinks from object store, no re-transfer | re-transfer |
| 20 | Cache off | `cache_received_payloads=false`; paste | item materializes into the lease tree, objects collected at lease end | objects linger |
| 21 | Provider change | copy on A, then copy the same item on B, paste on C | provider switches, item stays pasteable | stale provider, paste fails |
| 22 | V2 to V2 | both `0.6.3` | `stream_v2` negotiated | legacy |
| 23 | V2 to Legacy | peer on `v0.5.4` | `legacy_zip_v1` negotiated, paste works | failure |
| 24 | Update during transfer | trigger updater while transfer runs | update deferred until idle (`update gate`), transfer completes | runtime killed mid-transfer |
| 25 | RAM and HDD observation | Task Manager during cases 2 and 3 | flat RAM, disk write roughly once per payload byte | doubled writes, RAM growth |
| 26 | Upgrade `v0.5.4` -> `v0.6.3` | install over existing setup | config, history metadata, cache objects, leases, provider and update state preserved; old ZIP batches still paste | data loss |

---

## Installer, WebGUI and program consistency

- [ ] Install over an older installation without uninstalling first (see
      `docs/install_test_checklist.md`, section J2): only the UAC prompt, no
      console question, FlowShift is restarted, `config.json` is unchanged.
- [ ] Right after the installer finishes: WebGUI opens, command wheel
      (Ctrl+Right click) and clipboard overlay (Ctrl+Win+V) open at once.
- [ ] Delete `%ProgramFiles%\FlowShift\webgui\overlay.html` and restart
      FlowShift: a tray notice "WebGUI needs an update" appears with the repair
      hint, the dashboard shows the same warning, pressing Ctrl+Win+V several
      times starts no `overlay_host.py` process, and `flowshift.log` shows no
      restart loop. Re-run `install_flowshift.bat` to repair.
- [ ] Change `%ProgramFiles%\FlowShift\webgui\VERSION` to an older version:
      tray notice and dashboard warning name both versions; overlays still open.
- [ ] Uninstall removes program and WebGUI and keeps `%ProgramData%\FlowShift`
      unless `-PurgeData` is given.
- [ ] With Python already installed, the installer finds it after the WebGUI
      build and downloads nothing; `install.log` lists every
      `python candidate` with exit code and output
      (`docs/install_test_checklist.md`, section J3). Run
      `test_install_python_probe.ps1` with `powershell.exe` and `pwsh.exe`.

## Web-API request guard

- [ ] Open the WebGUI (`http://127.0.0.1:5000/`), change a setting, reload:
      the change stays. Delete one clipboard item: it disappears.
- [ ] Ctrl+Right click, click a Command Wheel sector: the action runs in the
      previous window; hold-and-swipe works as before.
- [ ] `flowshift.log` has no `web_api rejected` line for these steps.
- [ ] Optional: from any other web page's browser console,
      `fetch('http://127.0.0.1:5000/api/inject/key', {method: 'POST'})` is
      refused and `flowshift.log` shows
      `web_api rejected POST /api/inject/key reason=...`.

## Development update channel (Forgejo)

- [ ] One-time switch (dev.22 checkout install and 0.3.0): download `main` as
      ZIP from Forgejo, unpack, double-click `install_flowshift.bat` (needs
      Node.js 24+ for the WebGUI build). Then Settings → Software Update:
      choose channel `Development`, save; the page shows source
      `Forgejo private-platform/flowshift (main)` and the installed version.
- [ ] Enter the Forgejo access token, click the floppy-disk icon (tooltip
      "Save access", big enough to hit easily): the box says
      "(stored)", `config.json` contains no token, Windows Credential Manager
      lists `FlowShift:forgejo-update`; "Last check" updates within seconds.
- [ ] After the next merge to `main` the new `0.7.0-dev.N` appears within 15
      minutes and is handled by the chosen policy (notify / download /
      install, install waits for forwarding, input, overlay and transfers to
      be idle); nothing is downloaded or unpacked by hand, `update.log` lists
      the steps. A forced failure of the archive's install script rolls back
      to the previous version and is not retried automatically.
- [ ] dev.31 installed once by hand over dev.29/dev.30, then the next merge:
      Software Update shows the new version under Installed and no error;
      `update.log` has `result: success` and its start line names a working
      directory under `ProgramData\FlowShift\updates`. After a failed attempt
      the page says in plain words what happened ("Update to ... was not
      installed", "What to do: ...") instead of technical notices.
- [ ] Never two installations at once (dev.35): with a newer FlowShift
      running, double-click `install_flowshift.bat` in an older FlowShift
      folder (dev.35 or newer). Afterwards FlowShift is installed and runs, the
      program folder is complete, the window noted the newer version; about
      15 minutes later the next check installs the newer version again and
      `update.log` ends with `result: success` (details:
      `docs/install_test_checklist.md` L).
- [ ] Without Node.js 24+: the page explains that Node.js LTS is needed and
      FlowShift keeps running.
- [ ] Without network/VPN: status shows "Forgejo is not reachable", "Last
      successful check" keeps the older time, no tray pop-up, no restart loop.
- [ ] Wrong token: "Forgejo refused the stored access"; the trash icon ("Remove access") empties
      the credential; switching to Stable shows source GitHub and blocks
      installing an already downloaded development state.
- [ ] `uninstall_flowshift.bat -PurgeData` also removes
      `FlowShift:forgejo-update` from the Credential Manager.

## Runtime lifecycle

- [ ] `python src/python/tray.py --tray` — tray icon appears, no CMD popup.
- [ ] Control socket answers `status` on `127.0.0.1:45782`.
- [ ] Control socket answers `diagnostics` and returns a serializable report.
- [ ] `python src/python/flowshift_diagnose.py` prints a report even when the runtime is down.
- [ ] GUI status shows `Runtime: gesund (alle Worker aktiv)` (green) and
      `Session: <id> interaktiv` (green, NOT Session 0).
- [ ] GUI `Pipeline:` line updates while forwarding (queued/forwarded/injected).
- [ ] Start from GUI twice: second start does NOT create a second runtime (singleton mutex).
- [ ] Stop from GUI: state `stopping → stopped` only after control socket is gone.
- [ ] Restart (stop → wait → start) comes back to `running`.
- [ ] Repeat Start/Stop 20×: no zombie processes (`Get-NetTCPConnection -LocalPort 45781,45782`).
- [ ] UAC: no prompt on normal start. Elevated path via "Elevated Runtime installieren" (one UAC, then none).

---

## Tray icon behaviour

- [ ] **Single left-click**: does nothing (no profile activation).
- [ ] **Double left-click**: opens the WebGUI dashboard.
- [ ] **Right-click**: shows menu (Forwarding start/stop, Settings, Clipboard,
  Web GUI, FlowShift directory, Auto-start, Exit).
- [ ] **Ctrl+Right-click** on the tray icon: opens the Command Wheel instead of
  the tray menu.
- [ ] Tooltip when **no profile active**: `FlowShift` (just the name, nothing else).
- [ ] Tooltip when **profile active** (e.g. Laptop → Surface): `FlowShift | Laptop → Surface`.

---

## Profile rows & direction labels (commit e137af8)

- [ ] With runtime running but **no profile active**: profile rows show peer names with
  **no direction label, no "verbunden", no "offline"** — columns are empty.
- [ ] After **activating Laptop → Surface**: Laptop's profile row shows `Laptop → Surface` / Quelle.
- [ ] After activation: **Surface's profile row** also shows `Laptop → Surface` / Ziel
  (via `fwd_state` protocol message). Verify by opening GUI on Surface.
- [ ] After **deactivating**: both profile rows clear back to empty.
- [ ] **Forwarding-Toggle-Button** text: inactive → `Forwarding starten → Surface-Viktor`;
  active → `Forwarding stoppen (Surface-Viktor)`.
- [ ] With profile A active: "Aktivieren" button for all other peers is **disabled**
  (prevents circular forwarding without first deactivating).
- [ ] Status panel shows separate lines: Netzwerk / Forwarding / Capture — never misleads.
- [ ] **"Status aktualisieren"** button updates immediately (polling loop runs every 1 s anyway).

---

## Mouse movement + smoothing (delta forwarding + coalescing sender)

- [ ] Activate profile Laptop → Surface. Move mouse on Laptop.
- [ ] Surface cursor moves proportionally (not frozen, not jumping back to start position).
- [ ] Cursor stays suppressed on Laptop (Laptop apps do not receive cursor moves).
- [ ] Move to screen edges: cursor stops at edge on Surface (clamped correctly).
- [ ] Deactivate: Laptop cursor unfreezes.
- [ ] Repeat after a Laptop runtime restart: anchor is re-primed on each activation.
- [ ] **Smoothness**: slow move, fast move, small precision move, diagonal, circle,
      window drag, mouse text selection. Compare to the previous jittery feel and
      note the result in the report (smoother / not / measured flush rate).
- [ ] Clicks (left/right/double/middle) remain reliable during heavy movement.
- [ ] Wheel remains reliable during heavy movement.
- [ ] Optional tuning in `config.json` `"mouse"` block: raise/lower
      `flush_interval_ms` and `sensitivity`, confirm effect.

---

## Keyboard forwarding + text selection (extended-key fix)

- [ ] Individual keys (letters, digits, function keys) forwarded and typed on Surface.
- [ ] Open Notepad/Notepad++ on Surface, type text, then from the Laptop:
  - [ ] **Shift+Left/Right** selects character-wise.
  - [ ] **Shift+Up/Down** selects line-wise.
  - [ ] **Shift+Home** selects to line start; **Shift+End** to line end.
  - [ ] **Ctrl+Shift+Left/Right** selects word-wise.
  - [ ] **Ctrl+Shift+Home/End** selects to document start/end.
- [ ] `type_text` via GUI Live Test: multi-line text with Enter and Tab lands correctly.
- [ ] No keys/modifiers stuck after deactivate/disconnect (synthetic key_up sent).
- [ ] Kill switch `Ctrl+Alt+Shift+Win+F12`: stops forwarding and quits runtime.

---

## Remote desktop-file creation (real remote input)

- [ ] Versions match on both devices (or use `--force`).
- [ ] Run `python src/python/remote_desktop_file_test.py --check` → reports match.
- [ ] Run `python src/python/remote_desktop_file_test.py` → creates
      `FlowShift_Remote_Test.txt` on the **Surface** desktop with the 4-line poem,
      purely via forwarded Win+R / notepad / typing / Ctrl+S.
- [ ] Repeat 3×, stopping+starting the Laptop runtime between runs (Surface runtime
      stays up); use `--repeat` or re-run. Files appear each time.

---

## Clipboard — text layer (two devices)

- [ ] WebGUI **Settings**: enable clipboard and persistence, set limits, save,
  reload and verify the normalized values remain.
- [ ] On Laptop, copy several texts (Ctrl+C). The watcher captures them; WebGUI
  `/clipboard` shows them under the peer profile after Refresh.
- [ ] Activate Laptop → Surface. On the Surface WebGUI, the peer profile's
  history pulls exactly the missing text items in order and marks them available.
- [ ] Copy 2 more on Laptop, re-activate: only the 2 new items transfer.
- [ ] On Surface, select an item → "Paste to Clipboard" → Ctrl+V pastes it.
- [ ] Pin/unpin, delete one and delete selected (All + Delete) work in WebGUI;
  Set/Get, Pin/Unpin, Delete and delete selected work in the overlay.
- [ ] Bidirectional: copy on Surface, activate Surface → Laptop, Laptop pulls it.
- [ ] Runtime health stays green; mouse/keyboard stay responsive during sync.

## Clipboard — React history UI

- [ ] `/clipboard` opens directly on the Clipboard tab, including after browser
  reload; unknown WebGUI routes fall back to Dashboard.
- [ ] The legacy Tkinter GUI contains no Clipboard tab, and launching
  `gui.py --clipboard` no longer opens a standalone history window.
- [ ] Image/GIF items show bounded static thumbnails without blocking list
  interaction. Animated GIF playback is not claimed by the current UI.

## Clipboard — HTML

- [ ] Copied HTML shows an `HTML` item in WebGUI and the Clipboard Overlay.
- [ ] Preview text is readable and does not show raw markup.
- [ ] `clip_get` pastes HTML into an app that supports `HTML Format`.
- [ ] Plaintext fallback is also set on the clipboard.

---

## Peer discovery & reconnect

- [ ] Peer auto-discovered via UDP broadcast (no manual IP needed if on same LAN).
- [ ] Ping Laptop → Surface: RTT shown in GUI log, Surface logs `ping received` / `pong sent`.
- [ ] Disconnect Surface: GUI shows no active connection (no stale "verbunden" labels).
- [ ] Reconnect Surface (without restarting Laptop runtime): peer returns, profile activatable.
- [ ] Edit peer host/port while runtime runs: connector restarts with new address
  (log: `address changed … restarting connector`).

---

## Circular forwarding (commit e137af8)

- [ ] Laptop activates → Surface. In Surface GUI, try to activate → Laptop:
  "Aktivieren" button is **disabled** as long as Surface's own profile is inactive.
  (If Surface has no active profile, its button is enabled.)
- [ ] Activate Surface → Laptop while Laptop → Surface is active: injected events on
  Surface carry `INJECTED_EXTRA_INFO` so the Surface hook does NOT forward them back.
  No forwarding loop occurs (verified by hook filter: `LLMHF_INJECTED`).

---

## Hotkeys

- [ ] Default hotkeys: Ctrl+Alt+1 forward, Ctrl+Alt+0 return. Both fire correctly.
- [ ] Delete peer A: peer B hotkey still works. Peer A hotkey shows "(ungültig)".
- [ ] Rename B: hotkey still targets B (identity-stable).
- [ ] Change hotkey in GUI while running: new combo works immediately (re-registered via `WM_RELOAD_HOTKEYS`).
- [ ] Invalid hotkey (`key == 0` or unresolved target): skipped at registration, log shows warning.

---

## Input cleanup (no stuck keys/buttons)

- [ ] Hold a key while forwarding active, press Return-to-local: no key stuck on Surface.
- [ ] Pull network cable while forwarding: keys released within 1 s (disconnect triggers cleanup).
- [ ] Hold mouse button, deactivate: mouseup sent to Surface.

---

## Live Test tab

- [ ] Both devices on same Git commit: button enabled. Different commits: button disabled.
- [ ] Override checkbox bypasses version gate.
- [ ] `Live Test starten` activates → mouse moves → click → text typed → deactivates.
  Verify on Surface that text appeared.

---

## Logging

- [ ] `flowshift.log` is written (file logging confirmed).
- [ ] Mouse-move and `status`-poll logs are rate-limited (no flooding).
- [ ] Shutdown logged once; all loops stop cleanly.
- [ ] GUI log shows `fwd_state` notifications when remote activates/deactivates.
