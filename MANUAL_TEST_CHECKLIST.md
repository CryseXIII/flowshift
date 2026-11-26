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
python -m unittest src/python/test_update_stable_path.py src/python/test_publish_github_release.py
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

Release built on the server (`scripts/publish_github_release.py assets`, no
GitHub Actions): the server cannot run Windows, so after each publication on
one Windows PC download `FlowShift-Setup.exe` from GitHub `releases/latest`,
install it over the existing installation, confirm that Settings > Software
Update shows the new version and the WebGUI opens, and run the Windows-only
lines above (stress suites, PowerShell tests) on that tag.

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
  wheel hotkey (Ctrl+Right click) is refused with "... is assigned twice:
  Command wheel and Clipboard overlay".
- [ ] (`0.7.0-dev.50`, Viktor 24.11.) Settings > Command Wheel: set the open
  hotkey to Ctrl+Space (record it or take Alt off Ctrl+Alt+Space); "Not saved
  yet" appears. Click "Save Command Wheel": next to the button either
  "Command wheel saved. Hotkey: Ctrl+Space" (green) or, when Windows or another
  program owns it, "Command wheel saved (hotkey Ctrl+Space), but not active:
  Ctrl+Space (Command wheel) is already taken by Windows or another program.
  ..." (red). Then toggle "Intercept Win+V" and click "Save Settings" (result
  shown next to that button), press F5: the field still shows Ctrl+Space.
  `flowshift.log` has `command wheel saved hotkey=Ctrl+Space`. Peers and
  Display layout changed on their pages stay after a later "Save Settings".
- [ ] (`0.7.0-dev.51`, Viktor 24.11.) Settings > Command Wheel has exactly one
  hotkey field, no "Add another hotkey". Record Ctrl+side button 1 in it and
  click only "Save Settings" at the bottom: next to that button "Command
  wheel saved. Hotkey: Ctrl+Side button 1. Settings saved."; F5: the field
  still shows Ctrl+Side button 1 and Ctrl+side button 1 opens the wheel.
  "Clear", "Save Settings": a question says only Ctrl+Right click on the
  tray icon opens the wheel then; "Cancel" keeps the hotkey and says so,
  "OK" saves ("Hotkey: none"); F5: the field shows "Not set", one grey
  sentence about the tray icon, no red text. `flowshift.log` has
  `command wheel saved hotkey=none`. An older dev.49/50 config with
  additional wheel hotkeys shows only the first one after the update.
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
- [ ] WebGUI Settings > Command Wheel > Open hotkey: press a side button
  alone, save; that button opens the wheel (tap and hold/swipe); the other
  side button still navigates back/forward in the browser; after "Clear"
  both navigate again.
- [ ] Change "Tap or hold after" (e.g. 600 ms) and switch off "Tap to open" or
  "Hold and swipe" separately: the behavior follows; switching off both is
  refused.
- [ ] While forwarding to another device, neither the hotkey, Ctrl+Right click
  nor the side button opens a local wheel.

### Hotkeys with mouse buttons (`0.7.0-dev.49`)
- [ ] After the update, Settings > Command Wheel > Open hotkey shows
  `Ctrl+Right click` with the green "active" line and no red text.
  `flowshift.log` has
  `command wheel hotkeys migrated: main=Ctrl+Right click` once and
  `mouse hotkey hook installed`.
- [ ] Click the Open hotkey field, hold Ctrl and press side button 1, save:
  the field shows `Ctrl+Side button 1`; Ctrl+side button 1 opens the wheel
  (tap stays open, hold and swipe runs a field), Ctrl+Right click opens the
  normal context menu again, side button 1 alone still goes back in the
  browser. Set it back by Ctrl+Right click in the field (or the list next to
  it), save: Ctrl+Right click opens the wheel again.
- [ ] A keyboard combination as Open hotkey (e.g. Ctrl+Shift+Q) works as
  before including hold and swipe.
- [ ] A plain left or right click in a hotkey field is refused with a hint;
  the browser shows no context menu and does not go back on a side button.
- [ ] "Clear" on the Open hotkey and the clipboard overlay hotkey: saved
  without any red text; the cleared hotkey does nothing; with no wheel hotkey
  at all Ctrl+Right click on the tray icon still opens the wheel.
- [ ] Peer hotkey row: Ctrl+Side button 2 as "Return to local", save; while
  forwarding, Ctrl+side button 2 returns to the local PC and the click does
  not reach the peer; locally side button 2 keeps "forward". Ctrl+Middle
  click as "Forward to <peer>" starts forwarding.
- [ ] The same hotkey in two places (e.g. two peer rows, or wheel and
  clipboard) shows "... is assigned twice: A and B" and Save stays disabled.
- [ ] Ctrl+Wheel down as clipboard overlay hotkey: one scroll opens the
  overlay once; plain scrolling is untouched.

### Command Wheel behavior and actions
- [ ] The Open hotkey (default Ctrl+Right click) opens the wheel.
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

### Web-API origin check (Herkunftsprüfung, `0.7.0-dev.43`)
- [ ] Overlay click: open the wheel, click Copy in Notepad; the action runs
  and `flowshift.log` has `action check id=copy channel=web_api class=L1`.
- [ ] Swipe: hold the wheel hotkey, swipe to Paste and release; the action
  runs and `flowshift.log` has `action check id=paste channel=wheel_swipe
  class=L1`. Clipboard (open_clipboard) logs `class=L0`.
- [ ] WebGUI Settings > Command Wheel: change a page and save; reopen, the
  change is kept. Save a normal setting (e.g. device name) and a peer edit;
  both are kept. Clipboard tab: delete one item with its trash icon.
- [ ] `install_flowshift.ps1` over the existing installation runs through
  (final program/WebGUI check green); an update via the WebGUI runs through.
- [ ] Foreign page: start a test page on another port (e.g. `python -m
  http.server 8099` in an empty folder, open `http://127.0.0.1:8099/`), then
  in its browser console run `fetch('http://127.0.0.1:5000/api/actions/execute',
  {method:'POST', headers:{'Content-Type':'application/json'},
  body:JSON.stringify({action_id:'select_all'})})` and the same with
  `mode:'no-cors'` and `headers:{'Content-Type':'text/plain'}`, plus
  `/api/inject/type` with `{text:'x'}`: nothing is selected or typed; in the
  Network tab the requests are refused (403, or blocked by CORS preflight)
  and `flowshift.log` has no `action check` line for them.

### Clipboard overlay and WebGUI
- [ ] Clipboard Shift-click range (`0.7.1-dev.3`, Viktor 25.11.), in the
  overlay (Ctrl+Alt+V) and in WebGUI `/clipboard` with at least 6 entries:
  check the 2nd entry (checkbox or Ctrl-click), Shift-click the 5th: the 2nd,
  3rd, 4th and 5th are checked (not only the 2nd and 5th), the bar shows
  `4 of N selected`. Then None, check the 5th, Shift-click the 2nd: the same
  four. Shift-click never sets the Windows clipboard.
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
    filters. Clicks on the profile list, buttons (also the copy actions) and
    cards never close it; a click on another program still does.
  - Drag the overlay by its grip `⠿`, the header, the tab row or the footer,
    release anywhere (also over another program, also after a very fast
    move): the window stays where it was released and no longer follows the
    mouse. Escape or Alt+Tab during the drag ends it as well. `flowshift.log`
    shows `overlay drag end reason=release|escape|focus_lost moves=N at=x,y`.
    Clicking into the search field after moving still keeps it open.
  - After pressing a copy action no new entry appears in the list (Recent
    count unchanged, `Current` unchanged).
  - Copy something new (text or files): it appears at the very top of Recent,
    in the overlay and in the WebGUI.
- [ ] WebGUI warnings, buttons and fields (`0.7.0-dev.46`, Viktor 24.11.):
  - Dashboard: when only Ctrl+Win+V is taken by Windows (Windows 11), there
    is no red `Hotkey taken` banner; under `Overlay Hotkeys` the line reads
    grey: `Ctrl+Win+V (extra shortcut) is used by Windows or another program.
    Ctrl+Alt+V opens the clipboard overlay.` A taken Command Wheel hotkey or
    a taken configured clipboard hotkey still shows red with the way out.
  - Settings > Clipboard: `Intercept Win+V` off: no Win+V line at all; on and
    Win+V taken: red line. Clipboard Sync off: no clipboard hotkey lines.
  - Dashboard `Hotkeys` table: a hotkey without action shows `not assigned`
    in grey, no red cross. Display tab / Settings `Display Layout`: a layout
    warning is a grey note while edge switching is off, red while it is on.
  - Settings > Command Wheel pages: every slot row shows the action dropdown
    and `↑ Up` / `↓ Down` buttons with tooltip; `Remove page`, `Add page`,
    `Save Command Wheel` have symbol, word and tooltip. The same with the
    Font Awesome CDN blocked (browser offline): the arrows still show.
  - Every dropdown (Settings, Command Wheel, Software Update, Peers mouse
    fields, Clipboard profile), number field, the Forgejo
    token field (`stored`), the Inject text box and the Web GUI port field:
    dark field, thin border, rounded like the buttons, own arrow, lighter
    border on hover, red-pink border and ring on focus, the open list dark
    and readable.
  - Settings page: clear 20 px gap between all sections (Device, Clipboard,
    Transfer, Display Layout, Hotkeys, Command Wheel, System, Software
    Update, Management); Peers and Diagnostics show the same gap.
  - Nothing else changed: saving settings, the wheel and the update access
    works as before. WebGUI `npm test` and `npm run build`.
- [ ] Clipboard copy actions (`0.7.0-dev.45`, Viktor 24.11.):
  - In Explorer copy two files from `C:\...\Berichte`, open the clipboard
    overlay (hotkey and Command Wheel) and WebGUI `/clipboard`. The entry
    shows the row `Copy` with `Full path`, `Folder path`, `File name`,
    `Root name`, `Relative path`; each has a tooltip. There is no
    `Show files as` list any more.
  - Click one after another and paste each with Ctrl+V into Notepad:
    `Full path` = `C:\...\Berichte\a.txt` and `...\b.txt` on two lines,
    `Folder path` = `C:\...\Berichte` once, `File name` = `a.txt`, `b.txt` on
    two lines, `Root name` = `Berichte`, `Relative path` = `Berichte/a.txt`,
    `Berichte/b.txt`. The overlay footer says e.g. `Copied 2 full paths`.
  - After every copy no new entry appears (Recent count unchanged).
  - `Set` on the same entry, then Ctrl+V in an Explorer folder: the files
    themselves are pasted.
  - An entry received from another PC: `Full path` and `Folder path` are
    dimmed, tooltip "Only for files copied on this PC"; the other three
    work.
- [ ] Clipboard file previews (`0.7.0-dev.44`, Viktor 23.11.):
  - In Explorer copy, one after the other, a GIF, a photo (JPG/PNG), a video
    (MP4) and a text file, then all four together. Open the clipboard overlay
    (Ctrl+Alt+V) and WebGUI `/clipboard`: the GIF card moves, photo and video
    show a thumbnail (the video with a small `▶`), the text file shows its
    Windows icon. The four-file card shows the four tiles in a row; copying
    six or more files shows four tiles and `+N`.
  - Hover the video card: a short muted clip plays (first seconds in a loop)
    and stops when the mouse leaves. Settings > Clipboard `Video Preview on
    Hover` off: only the still image.
  - Settings > Clipboard `Animated File Previews` = `Play on mouse hover`:
    the GIF stands still and moves only while the mouse is over its card.
    APNG and animated WebP behave like the GIF.
  - Tooltips of the tiles follow `Show files as` (Folder › name, name,
    relative path).
  - Rename, move or delete a copied file and reopen the overlay: its tile
    shows a neutral symbol with the extension, no error. A file opened
    exclusively by another program (locked) shows the same.
  - Copy a very large file (several GB video or a 100+ MB image): the list
    opens at once, the image gets its type icon, nothing hangs.
  - Open the overlay with 100+ entries and scroll fast: no stutter; tiles
    appear shortly after (grey pulsing tile while generating).
  - `%ProgramData%\FlowShift\clipboard\previews` exists and stays below the
    `File Preview Cache (MB)` limit; `flowshift.log` shows `clipboard file
    preview ready kind=...` lines (DEBUG). Uninstall without `-PurgeData`
    removes this folder.
- [ ] Clipboard copied pictures (`0.7.0-dev.47`, Viktor 24.11.):
  - In Chrome/Edge and in Firefox right-click a picture on a web page,
    `Copy image`. Open the clipboard overlay and WebGUI `/clipboard`: the new
    entry `Bild WxH` shows the picture as a thumbnail in its icon slot (grey
    pulsing tile for a moment first). A transparent PNG (logo) shows its
    transparent parts on the checkerboard, not black.
  - `Set` on that entry, then Ctrl+V in Paint: the picture is inserted.
    Ctrl+V into Word or a browser editor keeps the transparency. After
    `Set` no second entry appears in the list.
  - Snipping Tool (Win+Shift+S) and a selection copied in Paint: the entry
    shows the cut-out as thumbnail and pastes back into Paint.
  - Copy a 4K or 8K screenshot (Print): the list opens at once and stays
    smooth while the thumbnail appears.
  - Copy only part of a web page that contains just a picture as HTML (e.g.
    select a picture in a mail and copy) where no picture data is offered:
    the entry shows the neutral `IMG` tile; the HTML detail in the WebGUI
    shows no picture from the internet (nothing is loaded).
  - `flowshift.log` (DEBUG): `clipboard captured image WxH image/png` for the
    browser picture (`image/bmp` for programs without PNG) and `clipboard
    image preview ready kind=image`.
  - With two PCs: a copied browser picture shows its thumbnail on the other
    PC after the transfer and pastes there too (both on dev.47 or newer).
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

### Test 7: Peer von Hand per IP eingetragen (`0.7.2-dev.1`)
- [ ] Auf A unter Peers B von Hand mit IP und Port eintragen (nicht ueber
      Scan Network), unter Display Layout die Kante North auf B legen,
      speichern. Beide PCs mit `0.7.2-dev.1` oder neuer.
- [ ] Sobald B verbunden ist: `flowshift.log` auf A zeigt einmal
      `peer <IP von B>:<Port> learned device id: endpoint:... -> device:<id>`
      (ggf. zusaetzlich `peer <IP von B> identified as device:<id> ...`); danach verbindet der Eintrag einmal neu. Die
      Peers-Liste zeigt B weiter unter dem eigenen Namen.
- [ ] Maus oben aus dem Bildschirm von A schieben: sie erscheint unten auf B
      (`edge session started source ... -> device:<id> north->south`), kein
      `invalid_target` mehr. Zurueck ueber die untere Kante von B.
- [ ] Ctrl+Alt+1 und Ctrl+Alt+0 wechseln weiter; Dashboard > Hotkeys zeigt
      beide ohne Fehler (kein `GET /api/hotkeys` 500 im Log).
- [ ] Gegenprobe mit einem `0.7.1`-PC als A: die Kante North auf B (`0.7.2`)
      wechselt ebenfalls (B nimmt die eigene Adresse an).
- [ ] Lehnt B einen Wechsel ab (z. B. waehrend B ein Update installiert),
      zeigt das Dashboard auf A genau einen Hinweis "Edge switch did not
      happen" mit dem Grund; nach dem naechsten erfolgreichen Wechsel (oder
      nach zwei Minuten) verschwindet er.
- [ ] Remote Preview auf beiden an, Wechsel nach B: Dashboard auf A zeigt
      `preview_unavailable (devices are paired, ...)`, nicht mehr
      `pairing_required`. Ein Bild gibt es noch nicht (Stufe 2-4 offen).
- [ ] Update von verbundenem PC: hat A eine neuere Version, bietet A sie dem
      von Hand eingetragenen B an (kein `not_paired`).

### Log-Groesse (`0.7.2-dev.1`)
- [ ] WebGUI eine Stunde offen lassen: `flowshift.log` bekommt keine Zeilen
      `web_api: "GET /api/status ...` oder `GET /api/hotkeys` mehr (nur
      Fehler und Aenderungen).
- [ ] Liegt `flowshift.log` ueber 5 MB (z. B. die alte 19-MB-Datei), heisst
      sie nach der naechsten Logzeile `flowshift.log.1` und eine neue
      `flowshift.log` beginnt; hoechstens `.1` bis `.3` bleiben liegen.

### Event Log zeigt Warnungen und Fehler (`0.7.2-dev.2`)
- [ ] Einen abgelehnten Kantenwechsel ausloesen (z. B. waehrend B ein Update
      installiert), dann erst auf A
      WebGUI > Event Log oeffnen: die WARN-Zeile `edge_enter rejected: ...`
      steht schon da (aus der Zeit vor dem Oeffnen), orange, mit dem Satz
      in einfachen Worten darueber.
- [ ] Seite offen lassen und einen weiteren Wechsel ausloesen: die neue Zeile
      erscheint sofort, ohne Neuladen, und nur einmal.
- [ ] "Show level" > "Warnings and errors" blendet INFO aus, "Errors only"
      zeigt nur rote Zeilen, "Everything" zeigt auch `status_update`.
- [ ] "Open log folder" oeffnet `C:\ProgramData\FlowShift\logs` im Explorer;
      der Pfad `C:\ProgramData\FlowShift\logs\flowshift.log` steht daneben.
- [ ] Eine Stunde offen: keine DEBUG-Zeilen und keine `web_api: "GET ...`
      Zeilen auf der Seite.

### Scan per adapter and edge switch to a peer typed in by IP (`0.7.1-dev.8`)
Two Windows PCs in the same network, the laptop with WLAN plus WireGuard
and/or Tailscale, both with the same FlowShift version.
- Laptop, Peers > Scan Network: PC 2 is listed within about 3 s; below the
  buttons "Searched via: WLAN (192.168.x.y/NN), ..." names the adapters
  (Tailscale with /32 is not listed, it has no local network).
- Same from PC 2: the laptop is listed.
- Laptop: remove PC 2, add it with Add Peer by IP only, click Ping: the ping
  shows a time; Settings shows PC 2 with its device ID and name now.
- Display: North -> PC 2, PC 2: South -> laptop. Move the mouse over the
  North edge: the pointer appears at the bottom of PC 2 and comes back.
- Close FlowShift on PC 2, move over North on the laptop: the Dashboard
  shows "Edge switch did not happen" with "no connection to PC 2".
- Firewall: `Get-NetFirewallRule -DisplayName 'FlowShift*'` lists
  `FlowShift (TCP)` and `FlowShift (UDP)` with profile Any.

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
- [ ] Remote preview stage 1 (`0.7.0-dev.48`): after installing over an older
      version, `%ProgramFiles%\FlowShift\src\python\remote_preview.py` exists
      and FlowShift starts normally; after the uninstall the program folder
      including this file is gone. No new firewall rule and no new port.
- [ ] USB package (`0.7.0-dev.53`, `docs/install_test_checklist.md` section
      N): on the development PC "Create USB package" saves
      `FlowShift-0.7.0-dev.53-usb.zip` in Downloads; on a PC without Git,
      Node.js and Forgejo (best: no internet) unpack it, double-click
      `install_flowshift.bat`: FlowShift runs, WebGUI shows `0.7.0-dev.53`,
      channel Stable, no Forgejo message, the no-downgrade sentence is shown.
      Double-click again over the same and over an older version: clean
      upgrade. Uninstall removes it.
- [ ] USB package download failures (`0.7.0-dev.55`,
      `docs/install_test_checklist.md` section N2): a broken Python download
      is tried twice, the message shows cause, expected/received size and
      SHA-256, HTTP status/type and the `flowshift.log` path; "Use downloaded
      Python installer" with the browser-saved `python-3.15.1-amd64.exe`
      builds the ZIP, a wrong file is refused.
- [ ] USB package with source-only packages (`0.7.0-dev.56`,
      `docs/install_test_checklist.md` section N3): "Create USB package" shows
      "Building the Python package proxy-tools 0.1.0 from its source" and
      saves the ZIP; installed offline on a clean PC, FlowShift and the overlay
      start.

## Remote preview stage 1 (`0.7.0-dev.48`, [Karte 833](http://kanban.thegoodhunter.vps/tasks/833))

Two Windows PCs (A and B) with `0.7.0-dev.48`. Stage 1 never shows a picture:
the other PC refuses every request until device pairing exists
([Karte 722](http://kanban.thegoodhunter.vps/tasks/722)).

- [ ] With the preview switched off (default): switching per hotkey and per
      screen edge and returning work exactly as before; mouse and keyboard
      behave unchanged. Dashboard card "Remote Preview" shows `Idle`.
- [ ] WebGUI on A: Settings > Remote Preview shows the switch, frame rate,
      maximum width, monitor, area around the mouse, window corner and width
      and the notice "Wirkt erst, wenn die Geräte gekoppelt sind. Gekoppelt
      ist ein PC, sobald er mit Geräte-ID in der Geräteliste steht ..."
      Change values, save, reload the page:
      the values are kept.
- [ ] Enter an invalid value (frame rate 99 or window width 50) and save:
      "Not saved: a value is outside its range ..." appears, after a reload
      the previous values are still there.
- [ ] Switch the preview on, on A and on B (no restart needed). Switch from
      A to B per hotkey: the Dashboard on A shows "Remote Preview: Rejected"
      with `preview_unavailable (devices are paired, the preview picture
      itself is not built in yet)` when both PCs have each other with device
      ID (Scan Network, or typed in and connected once), otherwise
      `pairing_required (devices are not paired yet)`; `flowshift.log` on A
      contains `remote_preview rejected reason=...`, on B `remote_preview
      request from ... refused reason=...`. No window appears; mouse and
      keyboard keep working on B.
- [ ] Return to A (hotkey or edge): the Dashboard on A shows `Idle` again
      (`remote_preview stopped (...) state=idle` in the log).
- [ ] The same with a switch over the screen edge, then once with a cancelled
      edge session and once by pulling the network cable of B while forwarding:
      each ends in `Idle`.
- [ ] `netstat -ano | findstr LISTENING` on A and B lists the same FlowShift
      ports as with the previous version (45781 and the WebGUI port among
      them); no new port.
- [ ] With the preview on only on A: no request is sent (B does not advertise
      it), `flowshift.log` on A says `remote_preview not requested: ... does not
      offer remote_preview`, the Dashboard stays `Idle`.

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

## Stable update channel on a PC without Git, Node.js and Forgejo (`0.7.0-dev.52`)

Viktor 24.11.: all other PCs update only from GitHub. Test on a Windows PC (or
VM) where `git`, `node` and `npm` are not installed and Forgejo is not reachable
(no VPN). Needs a GitHub release newer than the installed version (after
Viktor's approval, `docs/setup.md` "Publishing a new version for all PCs"); until
then only the first two points can be checked.

- [ ] First installation: download `FlowShift-Setup.exe` from GitHub
      `releases/latest`, run it: one UAC prompt, no Node.js/Git question,
      FlowShift starts. Settings → Software Update: Channel `Stable`, Source
      `GitHub CryseXIII/flowshift`, policy `Notify only`; no Forgejo box, no
      Forgejo text anywhere on the page, also not in the opened channel list
      (`Development (every merged version, own network only)`, `0.7.1-dev.3`).
- [ ] Click `Check`: the three steps row shows Check with a tick ("No newer
      version" or "Found X"), the message says "Check finished: ...". Without
      internet: Check with a cross and "No internet connection. Connect to the
      internet and click Check again."
- [ ] Policy notify: after a newer GitHub release, `Check` → "Check finished:
      FlowShift X is available. Next step: Download."; `Download` → progress,
      then "Download finished and verified ..."; `Install` → "Installing
      FlowShift X: FlowShift closes now ...", then "FlowShift is restarting
      ...", after about a minute the page shows Installed X, "Last update:
      FlowShift X was installed" and `update.log` ends with `result: success`.
- [ ] Policy download (automatic updates on): with FlowShift running since
      before the release, the release is downloaded within 6 hours without a
      restart (or right after a FlowShift restart); state "Ready to install",
      nothing is installed until `Install` is clicked.
- [ ] Policy install: the release is installed by itself (after idle), FlowShift
      restarts with the new version; `update.log` `result: success`.
- [ ] A PC that ran `0.7.0-dev.N` (development channel), switched to Stable:
      the page shows "Not checked yet" and no Forgejo error, checks GitHub at
      once and installs the stable `0.7.0` like above (before dev.52 the
      install stopped with `invalid_release`).
- [ ] Uninstall afterwards (`uninstall_flowshift.bat`): program folder and
      WebGUI gone, `ProgramData\FlowShift\updates\downloads` gone, settings kept.

## Update from a connected device (`0.7.1-dev.2`)

Two PCs with `0.7.1-dev.2` or newer, each in the other's device list (with
device ID), A has the higher version. Details and the log lines:
`docs/install_test_checklist.md` section O.

- [ ] B, Settings > Software Update > "Updates from connected devices": the
      setting shows "Notify only (recommended)", the tick "Also accept
      development versions" is off.
- [ ] Connect A and B: B shows "A offers FlowShift <version>" and a tray
      message; nothing is transferred yet.
- [ ] B: "Install and restart": progress, restart, the panel shows the last
      update from A as installed; Software Update shows the new version under
      Installed.
- [ ] B on "Download, install and restart automatically", Save: the next
      newer version of A installs by itself after a tray message a minute
      before, never during a clipboard file transfer or while forwarding;
      "Cancel" in that minute stops it.
- [ ] Stable PC without the tick ignores a `-dev` version of A; with the tick
      it is offered. A PC that is not in B's device list offers nothing.
- [ ] WebGUI `npm test` (`PeerUpdatePanel.test.jsx`,
      `SoftwareUpdateSection.test.jsx`) and `npm run build`;
      `packaging/test_release_packaging.ps1`, PowerShell parsing of
      `update_flowshift.ps1`.

## Course and display of an update of a connected PC (`0.7.1-dev.4`)

PC 1 has the newer version, PC 2 the older one and the setting "Download,
install and restart automatically"; both in each other's device list.

- [ ] Connect PC 1 and PC 2 (no mouse over the edge yet): the Dashboard of
      PC 1 shows at PC 2 "Older version X", then "Update is being transferred
      (N %)" and a tray message "Updating PC 2".
- [ ] While the transfer runs, move to PC 2 over the edge and stay there:
      PC 1's Dashboard (after returning) showed "Update waits until you are
      back on this PC"; PC 2 did not restart while you were on it.
- [ ] Back on PC 1: PC 2 shows "Is being installed", then "Restarts ...". Move
      the mouse over the edge now: tray message "PC 2 is being updated ..."
      instead of nothing.
- [ ] Without touching PC 2: it reconnects by itself, PC 1 shows "Connected
      again, version Y" (same as PC 1) and the tray message "PC 2 is
      updated"; the edge switch works again.
- [ ] Failure: on PC 2 make the install fail (e.g. block `pip` by unplugging
      the network with a changed `requirements.txt`): PC 2 runs the old version
      again, PC 1 shows "Update failed: <reason>; runs on with X". After the
      second failed attempt "automatic attempts paused after 2", no third
      restart.
- [ ] Unchanged `requirements.txt`: `install.log` on PC 2 says "venv of the
      previous version reused" and "dependencies unchanged"; no PyPI download.
- [ ] WebGUI `npm test` (`Dashboard.test.jsx`) and `npm run build`; PowerShell
      parsing of `install_flowshift.ps1` and `update_flowshift.ps1`.

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

## Peers over Tailscale and WireGuard (0.7.2-dev.3, Viktor 26.11.)

Two Windows PCs that reach each other only over Tailscale (or WireGuard),
both with `0.7.2-dev.3` or newer, neither has the other in its list.

- [ ] PC 1, WebGUI Peers, `Scan Network`: PC 2 appears under Discovered with
  `Found via` = `Tailscale` (or `VPN`); `Add` adds it.
- [ ] Tailscale off on PC 2 (or FlowShift stopped there), scan again: the blue
  box says "Over a VPN this is normal ..." and how many Tailscale devices
  were asked; the address field is open, port `45781` filled in.
- [ ] Enter PC 2's Tailscale name or `100.x.y.z` there, Save: the note
  "Both PCs need each other in their list" appears; "This PC" shows the own
  addresses (one as "Tailscale address"), port and device ID.
- [ ] PC 2, WebGUI Peers (reload): "PC 1 (100.x.y.z) has added this PC" with
  `Add here too`; click it: PC 1 is in PC 2's list; the `x` button (tooltip `Not now`) removes the box
  and adds nothing. `flowshift.log` on PC 2 shows `pair request from ...`.
- [ ] PC 1, Ping PC 2 while FlowShift on PC 2 is stopped: the red box names
  the cause ("nothing listens on port 45781" or, with the firewall rule
  disabled, "Windows Firewall ... 'Public' network ... UDP and TCP").
- [ ] Windows Defender Firewall > Inbound Rules on both PCs: `FlowShift (TCP)`
  and `FlowShift (UDP)`, port 45781, profile `All`.
- [ ] WebGUI `npm test` (`PeersPanel.test.jsx`) and `npm run build`.

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
