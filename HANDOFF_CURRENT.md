# FlowShift - Current State

## Release state

- Current version: `0.7.0-dev.40` (clipboard overlay fixes, Viktor 23.11.:
  (1) window drag is host-driven (`overlay_host.DragSession`,
  `bridge_begin_drag`, pywebview `easy_drag=False`): ends on button release
  anywhere, Escape, focus loss; the old page-driven drag kept following the
  mouse when its mouseup was lost; the stored placement follows the move.
  (2) `clipboard.file_path_display` (`Show files as` Folder/Name/Path in the
  overlay and WebGUI, saved via `/api/settings`, overlay payload) decides the
  names on file cards and what `Copy` copies; loose files get their source
  folder name (last part of the private `base`) as root, before all three
  forms were the file name. (3) Card `Copy` writes via
  `perform_windows_write(..., mark_current=False)` (`tray.write_clipboard_text`),
  so the watcher no longer records it as a new item. (4) `GET
  /api/clipboard/items` lists newest first (`clipboard_model.newest_first`),
  the store order was oldest first. (5)/(6) `DismissPolicy`: clicks on the
  overlay's own windows (search field, dropdowns: `WindowFromPoint` +
  `GA_ROOTOWNER`) are inside for the clipboard; the reference foreground of
  a show is read before an own window is hidden and skips own windows
  (`_previous_foreground`); a ~0.6 s settle phase accepts the foreground
  shuffle of the show itself (wheel -> clipboard). Earlier dismissal rules
  (Escape, outside click, Alt+Tab, wheel hub/gaps, hold-and-swipe) unchanged).
  Before: `0.7.0-dev.38` (clipboard multi-select: check several
  items in the overlay and the WebGUI Clipboard tab, All/Invert/None, delete
  at once without confirmation, see "Clipboard multi-select" below). Before:
  `0.7.0-dev.37` (Command Wheel hover: only the sector
  under the mouse or the swipe aim turns blue, all other sectors keep their
  resting look; the 50 % dimming of the others (`DIM_FACTOR` in
  `overlay_native_wheel.wheel_scene`, `.sector--dim` in `overlay.css`) and
  the 90 ms opacity/fill transitions of the WebView2 wheel are gone, Viktor
  22.11.: fast mouse moves looked like flicker). Before: `0.7.0-dev.36`
  (Command Wheel drawn natively in its own
  layered window with `UpdateLayeredWindow` per-pixel alpha instead of
  WebView2; DWM corner rounding/backdrop/border switched off and read back;
  see "Overlay host" below). Before: `0.7.0-dev.35` (installer/updater: one install lock for
  manual install, update runner, in-program updater and uninstaller; the
  installer stages and checks the new files before it swaps the program
  folder and keeps the previous one until the new version runs; the rollback
  copy is never deleted on a failure; no automatic update right after a
  manual install. Before: `0.7.0-dev.34` (overlay: per-pixel alpha via
  `transparent=True` + black form + DWM blur-behind instead of the colour key,
  on-screen pixel measurement after every show (`overlay pixel check`),
  `POST /api/overlay/probe`; `0.7.0-dev.33` set the WebView2 background and
  form colour key on the WinForms UI thread, no dark root `color-scheme` in the
  overlay page, region also on the child windows, every overlay window listed
  in `flowshift.log`; `0.7.0-dev.32` tried the WebView2 background from the
  worker thread; `0.7.0-dev.31` fixed the automatic update
  "access denied" on `C:\Program Files\FlowShift`, `0.7.0-dev.30` the overlay
  window region, `0.7.0-dev.29` the development update channel from Forgejo)).
- Current stable release: `v0.6.3` (published; tag-triggered workflow `success`, assets `FlowShift-Setup.exe`, `SHA256SUMS.txt`, `update-manifest.json` verified; the immutable `v0.6.0`, `v0.6.1`, and `v0.6.2` tags exist without releases, see below).
- Active implementation phase: Phase 4 - Clipboard Overlay and Command Wheel (target `v0.7.0`).
- Active phase specification: `docs/phases/phase_4_clipboard_overlay_command_wheel.md`.
- Phase 4 done so far:
  - `overlay_actions.py`: validated Action Registry (keys actions
    copy/paste/cut/delete/select_all/undo/redo, runtime actions
    open_clipboard/clipboard_sync), wheel config `config["command_wheel"]`
    (pages <= 16 x 8 slots, hotkey default Ctrl+Alt+Space).
  - Web-API: `GET /api/actions`, `POST /api/actions/wheel`,
    `POST /api/actions/execute` (runtime refs `execute_action`,
    `reload_hotkeys`). Fixed `_normalize_runtime_peers` (`/api/peers`,
    `/api/display/layout` raised NameError with configured peers).
  - Overlay host: per-mode sizes (`overlay_geometry.MODE_SIZES_CSS`, wheel
    360x360 centered on the cursor, clipboard 420x520 next to it, clamped;
    a wheel shifted at a screen edge moves the cursor onto its hub). The
    native window is clipped with `SetWindowRgn` to the visible shape
    (`overlay_geometry.overlay_shape_polygons`): wheel = ring sectors of the
    current page plus the hub page pill, clipboard = rounded panel; hub,
    gaps and corners are transparent and click-through. React reports page
    changes via `overlay_event("wheel_page")`.
    The form itself is opaque (`background_color="#111827"`, pywebview has no
    reliable per-pixel alpha on Windows), so without the region Viktor saw the
    whole dark square (dev.29, 20.11.). The region was skipped silently when
    `window.native.Handle` did not resolve. Since dev.30 the host reads the
    HWND as `IntPtr` or int (`native_handle_value`), falls back to its own
    top-level window titled `FlowShift Overlay` (`_Win32.own_window`), checks
    after `window.show()` with `GetWindowRgn` that the region is still set and
    re-applies it once (`_ensure_shape`). Failures are logged as
    `overlay host: overlay host shape ... (failed|re-applied after show|missing after show)`.
    dev.31 live (Viktor 20.11. 18:12): square still visible although no
    failure line was logged, i.e. the region was set and kept. Cause: pywebview
    sets the WebView2 `DefaultBackgroundColor` opaque to #111827, and WebView2
    (DirectComposition) is not reliably clipped by the window region. Since
    dev.32 `_clear_webview_background` sets it to `Color.Transparent` on the
    UI thread before the first show; the region stays for click-through.
    The control is resolved as pywebview stores it, `native.browser.web_view`
    (fallback `native.webview`, `webview2_control`); logged as
    `overlay webview control path=...`, on failure with `native attrs=...`.
    INFO diagnostics in `flowshift.log`: `overlay webview background
    transparent=ok alpha=0`, `overlay shape set ... hwnd= placement= dpi=
    window= window_dpi= exstyle= polygons= setwindowrgn= region= box=`,
    `overlay shape after show ... kept`, `overlay window tree ... children=`.
    dev.32 live (Viktor 20.11. 21:15): square still visible; log
    `transparent=failed (CoreWebView2Controller members can only be accessed
    from the UI thread ... get_DefaultBackgroundColor)`, region set and kept
    (hwnd 0x130ecc, 360x360, dpi 96, `region=3`). Since dev.33: all WebView2
    and form reads/writes run inside `form.Invoke`; the form gets
    `BackColor`/`TransparencyKey` = `OVERLAY_KEY_COLOR` #111827 (layered
    colour key); `overlay.css` moved `color-scheme: dark light` from `:root`
    to `body` (a dark root colour scheme makes Chromium paint the canvas
    dark despite the transparent background); the region is also set on every
    child window; `overlay window top|child hwnd= class= pid= rect= visible=
    region= exstyle=` lists all windows of the host process once per host.
    dev.33 live (Viktor 21.11. 21:42): square still visible although every
    line reported success (`transparent=ok ui_thread=1 alpha=0
    form_key=#111827/a255 layered=1`, region on the only visible window and
    all five children, kept). The page has no frame/card behind the wheel
    (checked `overlay.css`). Since dev.34 the colour key is gone: the form is
    created with pywebview `transparent=True` (WebView2 alpha 0 from the
    start; the host clears `window.transparent` in `before_show` so pywebview
    no longer shows the hidden window on navigation), paints black
    (`OVERLAY_FORM_COLOR`) and gets `DwmEnableBlurBehindWindow` with an empty
    blur region (per-pixel alpha, as winit/tao do). Log:
    `overlay webview background transparent=ok strategy=alpha ui_thread=1
    alpha=0 form=#000000/a255 form_key=#000000/a0 layered=0 blur=ok`.
    The screen is now measured: desktop under the hidden window before the
    show, the same points 0.4 s after it (`overlay pixel mode= point= ...
    below= shown= result= source= hit=` per point, then `overlay pixel check
    mode= verdict=ring_only|panel_only|square_visible|background_visible|
    nothing_drawn|unknown ... clear= clickthrough= drawn= painted= region=
    rects=`). `POST /api/overlay/probe {"mode":"command_wheel"|"clipboard"}`
    triggers the same measurement.
    dev.34 live (Viktor 21.11. 23:33, "Das Rechteck ist immer noch da"):
    his screenshots show a ~356x356 rounded rectangle in #202020 with the thin
    light Windows 11 window border; sector fill #141920 is exactly
    rgba(19,24,32,0.9) over #202020, gaps #202020: the page is transparent,
    but DWM composes it onto a window surface that neither region nor
    alpha/blur removes. Since dev.36 the wheel no longer uses WebView2:
    `overlay_native_wheel.py` draws it with GDI+ into a 32-bit premultiplied
    bitmap and hands it to a `WS_POPUP` + `WS_EX_LAYERED|TOPMOST|TOOLWINDOW`
    window via `UpdateLayeredWindow(ULW_ALPHA)` on its own thread and message
    loop. Alpha-0 pixels show the desktop and pass clicks through; no window
    region. `DWMWA_WINDOW_CORNER_PREFERENCE=DONOTROUND`,
    `DWMWA_BORDER_COLOR=NONE`, `DWMWA_SYSTEMBACKDROP_TYPE=NONE` are set and
    read back (also on the WebView2 form for the clipboard panel). Hover
    spotlight, aim, mouse-wheel paging (`overlay_event wheel_page`), labels,
    error text and the two page dots are drawn natively; a click posts
    `/api/actions/execute {source: command_wheel}` like the React wheel. The
    clipboard panel stays WebView2. If the native window cannot start or
    paint, the host logs it and falls back to the WebView2 wheel. Log:
    `overlay native wheel ready hwnd= exstyle= corner=set:/read: border=
    backdrop= paint=updatelayeredwindow`, `overlay native wheel shown ...
    dwm_corner= dwm_backdrop=`, `overlay shape native ... region=none`,
    pixel check with `strategy=native_layered` and `dwm_corner=`. Not yet
    verified on Windows: done only when the corner pixels read `result=clear
    source=below` (not #202020).
    Viktor decided (20.11. 17:58): the two page dots in the hub stay visible
    ("sie indizieren die Seiten des Commandwheel"), the rest of the hub is
    see-through (current region: hub pill only).
  - Dismissal (`overlay_host.DismissPolicy`, 30 ms polling, no hook): the
    overlay no longer needs the foreground. The old focus watcher closed it
    after 0.75 s whenever Windows refused `SetForegroundWindow` (dev.22 bug,
    wheel and clipboard). It now closes only on a new Escape press, a new
    mouse press outside the visible shape, or another window becoming
    foreground. Every close is logged by the controller as
    `overlay_hidden reason=<escape|outside_click|focus_lost|close_button|action|runtime|swipe|swipe_center|tap_off> mode=<mode>`.
    `window.flowshiftOverlay.hide()` unmounts the React views on hide, so a
    hidden clipboard overlay stops polling. Outside clicks currently also
    reach the window below (region click-through); swallowing them would
    need a low-level mouse hook and is an open decision for Viktor.
  - Hold and swipe (dev.26, `wheel_gesture.py`): hotkey (registered with
    `MOD_NOREPEAT`), Ctrl+RightClick and the optional mouse side button
    (`command_wheel.trigger.mouse_button` none/x1/x2, unassigned keeps
    back/forward) open the wheel with `data.hold`; the runtime reports the
    release as IPC `wheel_release {held_ms}` (hotkey release by
    `GetAsyncKeyState` polling, mouse release from the trigger hook). The host
    highlights the sector in the cursor direction while held
    (`window.flowshiftOverlay.aim`), ignores mouse presses during the hold and
    answers `wheel_released`: tap (< `hold_ms`, default 250) keeps it open for
    clicking (or closes with `tap_off` when `tap_open` is off), a hold runs the
    sector in that direction (`swipe`, direction only, 30 CSS px dead zone ->
    `swipe_center`). The runtime waits up to 1 s for released modifiers, then
    `execute_action(source=wheel_swipe)`. WebGUI Settings > Command Wheel edits
    side button, tap, hold and threshold (`trigger`, `400 trigger_disabled`
    when both are off). While forwarding no trigger opens a local wheel.
    The curated release payload was missing `hotkey_status.py` (tray import
    would fail in a packaged build); it and `wheel_gesture.py` are now listed
    in `build_release.ps1` and checked by `test_release_packaging.ps1`.
  - Overlay hotkey status (dev.24, `hotkey_status.py`): `register_runtime_hotkeys`
    records every clipboard/wheel registration; a busy combination
    (`RegisterHotKey` error 1409, e.g. Win+V, Ctrl+Win+V, Ctrl+Alt+Space on
    Viktor's laptop) is published as `GET /api/status` -> `overlay_hotkeys`
    with a plain-text message and the working alternative, shown once per new
    conflict set as a tray balloon (+ tooltip hint), as Dashboard banner and
    list, and next to each hotkey in Settings. The clipboard overlay hotkey is
    configurable (`clipboard.overlay_hotkey`, default Ctrl+Alt+V, Settings >
    Clipboard; must differ from the wheel hotkey, `400 hotkey_conflict`).
    WebGUI modifier bits now match the runtime (Shift=2, Alt=4; before, a
    WebGUI "Ctrl+Alt" was stored as Ctrl+Shift). Captures into the local
    history are no longer announced like a peer (no more
    `clipboard announcement failed -> local`). Win+V/Ctrl+Win+V are not
    intercepted via the keyboard hook (Viktor 18.11.: Ctrl+Alt+V suffices).
  - React overlay: `OverlayShell` routes `command_wheel` -> `CommandWheel`
    (SVG sectors <= 8, cyclic mouse-wheel paging, dots, spotlight hover),
    `clipboard` -> `ClipboardOverlay` (fixed-height list, async polling + SSE,
    same list node and scroll offset across refreshes, Set/Get/Pin/Delete,
    legacy + stream V2 progress via `clipboardFormat.js`), diagnostic card only
    with `data.diagnostic === true`.
  - Stream V2 telemetry reports privacy-safe current-file name, one-based file
    ordinal/count, independent current-file and total bytes/percent, rate,
    `HH:mm:ss` ETA and last completed-file duration. Batch cards render separate
    current and total bars; single-file and non-file cards render one bar.
  - `tray.py`: Ctrl+Alt+V, Ctrl+Win+V and (opt-in) Win+V open the clipboard
    overlay; global Ctrl+RightClick (always-on mouse-only hook
    `WheelTriggerHook`, swallows only the Ctrl right click, inactive while
    forwarding), Ctrl+RightClick on the tray icon or the configurable
    wheel hotkey (`ID_HK_WHEEL`) opens the wheel; `execute_action` hides the
    overlay, restores the remembered foreground window and enqueues key events
    into `inject_queue`; runtime actions open the clipboard overlay or send the
    profile manifest. The Tkinter clipboard tab/window and `gui.py --clipboard`
    entry have been removed.
  - WebGUI Settings includes `CommandWheelSection`: it loads the validated
    registry/wheel, edits ordered pages and slots within server limits, captures
    the wheel hotkey with shared hotkey-format helpers, and saves through
    `POST /api/actions/wheel` so the runtime immediately re-registers it.
  - WebGUI `ClipboardView` keeps its list DOM node and scroll offset across
    item/progress refreshes, rejects stale overlapping profile responses, and
    renders legacy plus stream V2 progress through `clipboardFormat.js`; detail
    and list rows share the same polling snapshot.
  - Tkinter retirement prerequisite: WebGUI Settings now exposes the productive
    `persist` and `max_item_gb` controls. `POST /api/settings` normalizes the
    complete clipboard block through `clipboard_model.clipboard_settings`,
    preserves unrelated top-level config, refreshes `istate.hotkeys`, publishes
    status, and requests immediate OS-hotkey re-registration.
  - WebGUI routes are URL-backed; `/clipboard` opens the Clipboard tab directly
    and the HTTP server serves the SPA there. The tray Clipboard menu entry opens
    that route; tray double-click and Web GUI still open the dashboard. Current
    architecture, clipboard and manual-test documentation describes the React UI.
  - Public clipboard card contract: `GET /api/clipboard/items` returns
    whitelisted `public_card_item` rows (relative `file_card` summary, bounded
    previews/names, `is_current`, no `files`/`abspath`/`source_paths`/`base`/
    fingerprint data) plus top-level `current_item_id`; item detail returns the
    same public row plus a bounded relative `file_manifest` slice
    (`manifest_offset`/`manifest_limit`). Covered by
    `test_web_api_clipboard.py`.
  - Clipboard presentation: overlay and WebGUI use the shared `ClipboardCard`
    for one type-specific card per history item, separate Recent/Pinned views,
    safe text/file/batch previews, current/download metadata and item actions.
    File cards expose bounded relative root/name/path lists; copy buttons call
    the local-only JSON `copy-text` route, which regenerates text from the
    persisted public fields instead of trusting client content. File detail
    text also uses only validated relative manifest paths.
  - Local clipboard without peer: every capture also lands in the persistent
    `local` history (`This PC`); overlay payload and
    `GET /api/clipboard/profiles` list `local` first; missing profile params
    fall back to `local`; WebGUI and overlay default to it; sync on `local`
    is an ok-noop.
  - Clipboard multi-select (dev.37, Viktor 22.11.): before this, a single item
    was deleted with the card's `Delete` button ->
    `DELETE /api/clipboard/item/<id>?profile=` -> `ClipboardManager.delete_item`
    (marks the id as deleted for receivers, releases leases, cancels transfers)
    -> `ClipboardStore.delete_item` (removes the row, clears `current` if it
    was that item, writes `index.json`, removes the unreferenced object and the
    preview). The WebGUI tab also had `Clear` (confirm dialog, `POST
    /api/clipboard/clear`, removes pinned items too). The history lives in
    `%ProgramData%\FlowShift\clipboard\profiles\<profile>\index.json` plus
    `objects/<sha256>` and `previews/`. Now both views share
    `clipboardSelection.js` (`useClipboardSelection`) and
    `ClipboardSelectionBar.jsx`: checkbox per card, Ctrl-click toggles,
    Shift-click range, `N of M selected` as text, then four icon-only buttons
    (inline SVG in Font Awesome style, since the overlay does not load Font
    Awesome; each has `title` and `aria-label`, hit area 32 px WebGUI / 28 px
    overlay): check-square `All` (Ctrl+A), arrows `Invert` (Ctrl+I), empty
    square `None` (Ctrl+Shift+A), trash can `Delete selected` with the count as
    a badge (Delete key; greyed out without a selection). `All`/`Invert` skip pinned items, hand-checked pinned items stay
    selected and are deleted. Only visible items stay selected (switching
    view/profile or a search clears/prunes the selection). Delete is immediate
    and optimistic, one `POST /api/clipboard/delete?profile=` with
    `{"item_ids": [...]}` (local host/origin guard as copy-text, JSON, at most
    10 000 ids) -> `ClipboardManager.delete_items` -> `ClipboardStore.delete_items`
    (one `index.json` write), logged as `clipboard delete selected profile=
    requested= deleted=`. The WebGUI `Clear` button and its `confirm()` are gone
    (`api.clearClipboard` removed; the backend route stays). Selection clicks
    (checkbox, Ctrl/Shift click, Ctrl/Shift double-click) never call
    `pasteItem`; plain double-click and `Set` paste as before. The Windows
    clipboard is never touched by deleting; deleting the `Current` item only
    drops the mark, the content stays pasteable and comes back into the history
    only when copied again (the watcher reacts to new clipboard sequence
    numbers only).
- Last completed phase specification: `docs/phases/phase_3_clipboard_transfer_hardening.md`.
- Phase 3 toolchain and dependency modernization is complete.
- The productive legacy clipboard path and binding V2 target architecture are
  documented in `docs/clipboard_transfer_v2.md`.
- Capability negotiation, metadata-first schema-2 capture, canonical batch
  manifests, centralized Windows path validation, and the persistent
  thread-safe transfer-session foundation are implemented.
- Strict typed V2 JSON-control and raw binary-payload framing is implemented,
  including incremental parsing, absolute read deadlines, protocol limits,
  SHA-256 chunk verification, and serialized per-socket writes; it carries the
  productive V2 channel.
- Bounded V2 flow control is implemented as a transport-neutral foundation:
  global and per-peer admission, count/byte-bounded send and receive queues,
  per-transfer in-flight windows, finite ACK deadlines, strict cumulative ACKs,
  batching by bytes/chunks/time/file completion, and slow-receiver loopback
  coverage, including separate verified and durable offsets plus restored sender
  windows; the productive transport uses it.
- The productive legacy transfer queue rejects duplicate IDs and releases
  terminal, rejected, cancelled, and shutdown job closures.
- Transport-neutral direct V2 file streaming and receiver staging are
  implemented. Sources are opened one at a time, read once sequentially, hashed
  during transfer, and revalidated by path and open-handle fingerprint. Incoming
  data is written sequentially to private index-based `.part` files, flushed,
  `fsync`ed, size/hash/fingerprint verified, and atomically renamed per file to
  `.verified` before a finalized manifest revision is returned. Typed-channel
  tests exercise bounded queues, cumulative ACKs, slow receiver backpressure,
  and exact staged bytes.
- Transport-neutral persistent V2 resume is implemented with strict canonical
  incoming/outgoing journals, generation-CAS atomic commits, startup inventory
  and quarantine, checkpoint batching, durable ACK progress, source/partial
  prefix re-hashing, sender/receiver/dual restart reconstruction, tail
  truncation, pause/cancel/purge states, and rename/commit crash reconciliation.
  Productive resume messages and V2 routing remain open.
- Transport-neutral V2 preflight and shared per-file publication are implemented.
  Accepted estimates bind manifest/transfer and durable resume evidence; source
  reads and stage allocation are gated. Same-volume hardlink publication reuses
  receiver hash/change-time evidence, retains retry pins, and commits the same
  history item plus durable receipt before journal completion and stage cleanup.
  Index-write failures, restart finalization, completed cleanup, explicit purge,
  cross-profile dedup/GC, Windows restored-mtime tampering, lock contention, and
  corrupt-index recovery have focused end-state tests. Legacy-only indexes stay
  schema 2; V2 receipt indexes use schema 3. Journal schema 1 migrates with backup
  and CAS generation increment to schema 2; old stage-only completion becomes
  finalizing. V2-only objects are not advertised as legacy ZIP payloads.
  Automatic GC, provider/cache transitions, materialization, runtime preflight
  routing, and productive network activation remain open.
- V2 materialization is implemented (`clipboard_materialize_v2.py`) and wired
  into `ClipboardManager.materialize_files_result` for `object_manifest_v2`
  items: same-volume hardlinks with device/inode confirmation, verified
  streaming copy fallback (cross-volume, unsupported FS, permission, link
  failure), staged build plus atomic rename into the lease directory, `.active`
  markers and `set_lease`, no ZIP output; lease release unlinks only the
  materialization while shared objects remain.
- V2 availability is two-level: load/listing/`known_hashes`/provider state use
  cheap `item_is_deliverable` (regular file + exact size, no payload reads);
  delivery (`v2_manifest_for_item`) and publication verification rehash.
- V2 cache eviction is integrated: V2 receipts record a received-cache entry
  (content identity, `payload_size = total_size`); eviction sets the item to
  `missing`, marks the local provider `unavailable`, prunes the receipt in the
  same index write, and runs reference-aware GC that references objects only
  from deliverable items (pins/hardlinks protect via link count). Runtime
  eviction now removes only the LRU excess over `cache_max_mb`; the previous
  unbounded eviction while under budget was a latent legacy defect.
- Lease retirement is productive: `tray.clipboard_watcher` and
  `perform_windows_write` call `retire_leases_for_sequence`; stale leases keep
  their tree until `cleanup_leases(clipboard_temp_cleanup_max_age_hours)`.
  The temp cleanup settings key mismatch is fixed.
- Preflight V2 and the update idle gate are wired in the runtime:
  `ClipboardManager.preflight_stream_v2_receive` / `prepare_stream_v2_receive`
  (real free space, no stage or journal on rejection, `disk_full` keeps the
  journal retryable with partials) and `transfer_activity_state` feeding
  `activity_snapshot` so the updater `WAITING_FOR_IDLE` loop defers; durable
  V2 `paused`/`waiting_reconnect` stages allow updates, legacy paused jobs block.
  Journal store convention: `<store_root>/journals`.
- Cancellation, timeouts, progress, and cache-disabled materialization are
  implemented for the receiver side (`clipboard_transfer_control_v2.py`:
  `TransferTimeouts`, `DeadlineTracker`, EWMA `TransferRateTracker`).
  `ClipboardManager.cancel_stream_v2_session` works in every phase (journal
  `cancelled`, partials purged after peer ACK or `final_complete_ack` timeout);
  `run_stream_v2_maintenance` is ticked from `tray.clipboard_watcher`;
  `stream_v2_status` is exposed under `diagnostics()["stream_v2"]`
  (`/api/clipboard/status`) with relative file names only. Timeout settings:
  `clipboard_transfer_v2_<name>_timeout_s`. With `cache_received_payloads`
  off, V2 items publish uncached, materialize into the lease tree, are retired,
  and their objects are collected at lease end. Sender-side `manifest_ack` /
  `window_ack` deadlines are live in the outgoing session.
- The V2 transport is productive (`clipboard_transport_v2.py`): both peers
  advertise the capability in hello; `tray.py` selects the strategy
  (`select_clipboard_transfer_strategy`) and hands it to the manager together
  with a channel dialer; the copy path (`_on_request`) starts
  `OutgoingTransferSession` when negotiated, else legacy ZIP. Control messages
  (offer/accept/reject/resume/cancel/cancel_ack) travel on the legacy link;
  payload uses a dedicated socket (channel hello + single-use nonce, typed
  frames, cumulative window ACKs, complete/complete_ack). Receiver dispatch:
  offer -> `prepare_stream_v2_receive` -> `ReceiverSession` -> publish via
  `publish_stream_v2_session` with legacy `make_current`/`enforce` semantics.
  Disconnect -> `waiting_reconnect` -> resume from durable offsets. Peer
  cancels are always ACKed, also for terminal sessions (resume/cancel race).
  Setting `clipboard_transfer_v2_force_legacy` (default false) forces legacy.
- Restart resume is implemented on both sides. Receiver restart: a
  `T_RESUME_REQUEST` reopens the incoming journal via
  `prepare_stream_v2_receive(resume=True)`. Sender restart:
  `ClipboardManager.resume_outgoing_stream_v2(identity)` (called from
  `on_peer_connected`) scans `journals/outgoing`, restores resumable journals
  (`OutgoingTransferSession.restore`, identity/digest checked with
  `validate_resume_match`) and re-offers them with `T_RESUME_REQUEST`; terminal
  leftovers are purged; journals whose source item vanished or changed are
  purged and the peer receives `T_CANCEL(source_missing|source_changed)`.
  A `T_CANCEL` for a transfer without a live session purges an orphaned
  incoming journal/stage (`_purge_orphan_incoming_stream_v2`). A restart counts
  as one resume attempt; `MAX_RESUME_ATTEMPTS` is 128 (cumulative, not reset on
  progress). Retry counts are persisted in the outgoing journal. Receiver
  session stage mutations are serialized through the session lock (cancel vs.
  in-flight `accept` race). A `failed` receiver session with a retryable
  `failed` journal (disk full) accepts a resume request. An unchanged published
  V2 row may be metadata-refreshed by a peer manifest (`_add_item_locked`);
  previously the refresh raised and dropped the whole peer message.
- Known limitations: `resume_inventory` lower-device-id coordination and
  provider failover remain planned; V2 sessions are not mirrored as legacy
  `TransferJob`s (decision: the section-22 status API is
  `diagnostics()["stream_v2"]`, no legacy GUI mirroring in Phase 3); the
  sender's local item stays provisional (revision 0); during update
  maintenance `_begin_incoming_operation` admits only legacy continuations, so
  V2 cancel/cancel_ack are deferred. Lease `pending_write` persistence before
  the clipboard write and startup revalidation of unbound leases remain
  planned. Cross-host channel dialing (`hello.port`) is only exercised on
  localhost. Journal commits validate and digest every entry (about 3 commits
  per file), so batches of several hundred small files approach the default
  `final_complete_ack` timeout; the sender holds its session lock during ACK
  journal commits. Incoming journals whose sender never re-offers and never
  cancels are not timed out without a live stage. A premature duplicate
  `cancel_ack` can let the receiver purge before an in-flight resume request
  arrives (then bounded by the preflight timeout).
- The immutable `v0.5.3`, `v0.6.0`, `v0.6.1`, and `v0.6.2` tags remain unchanged; their release workflows failed and created no release. `v0.6.2` passed the full 627-test regression on CPython 3.14.6 and failed only in `worker_smoke_test.py`: the `activate` control call used a 2 s socket timeout while the hook thread start is bounded by 5 s; the smoke test now allows 15 s, prints the activation latency and dumps the runtime logs on abort (no productive change). `v0.6.1` failed at tag/VERSION validation because commits `8995b8c` and `9678b2b` did not update `VERSION` (bookkeeping error, `VERSION` stayed `0.6.0`). `v0.6.0` failed only in CI (CPython 3.14.6, runner TEMP with 8.3 short names): `safe_target_path` test compared against `Path.resolve()`, and the status-polling stress test used the 8 s FAST final-ack deadline; both tests were corrected, no productive change.

## Agent structure

- Permanent rules live in `AGENTS.md`.
- The active phase is tracked in `TODO_CURRENT.md`.
- The active phase specification is referenced from `TODO_CURRENT.md`.
- `HANDOFF_CURRENT.md` is the compaction checkpoint and operational snapshot.
- Run full repository regression only before a stable release.
- Use focused tests during individual slices.

## Current setup state

- Release CI uses CPython 3.15.1 and Node.js 24.18.1 LTS; dependency CI also
  tests Node.js 26.5.1 Current.
- GitHub Actions are immutable SHA pins and Dependabot covers Actions, npm, and
  Python dependencies.
- Runtime Python dependencies and audit tooling are fully hash-locked; direct
  npm dependencies and npm 12.0.2 are exact and package-lock v3 is committed.
- WebGUI development dependencies use Vite 8.3.5 and Vitest 4.1.12; resolved
  source-map-js 1.2.2 and undici 8.12.3 leave `npm audit` at zero findings.
- The installer accepts supported 64-bit CPython 3.11-3.15. When Python is
  missing it downloads the official CPython 3.15.1 x64 installer and verifies
  the published SHA-256 before execution. It preserves user ownership and
  requires hashed dependencies.
- Python search (dev.28, fix for "Python not found" after the WebGUI build on
  Viktor's laptop with Python 3.13.2): the old probe
  `& $py -c '...print(f"...")' 2>&1` can never succeed under Windows
  PowerShell 5.1 (inner double quotes reach Python unescaped -> SyntaxError;
  stderr + `$ErrorActionPreference = 'Stop'` -> swallowed exception). Setup.exe
  and the updater always use 5.1; the `.bat` path uses 5.1 whenever it does not
  self-elevate into `pwsh`. The new `Invoke-PythonProbe` starts each candidate
  as its own process (`-I -c` probe without quotes, stdin closed, PYTHONHOME/
  PYTHONPATH/VIRTUAL_ENV removed, 20 s limit) and logs per candidate
  `python candidate <path>: exit=.. time=..s stdout=.. stderr=.. -> <verdict>`.
  Candidates: py/python/python3 on PATH, `%SystemRoot%\py.exe`, PEP 514
  registry, `Program Files[ (x86)]\Python3*`, `%LOCALAPPDATA%\Programs\Python\Python3*`
  (no recursive Program Files search, FlowShift's own venv excluded); the real
  `sys.executable` builds the venv. The log also records the PowerShell host.
- Installer order (dev.28): WebGUI build -> Python check -> Python install only
  if nothing usable exists (announced with version, target folder and duration;
  no download if `Program Files\Python315\python.exe` already exists; download
  300 s limit, setup 600 s limit with a progress line every 15 s, setup log
  `%ProgramData%\FlowShift\logs\python-setup.log`) -> stop the running
  FlowShift -> replace files. A failure before the file step leaves the old
  installation untouched; the failure box ends with a `Status :` line saying so
  and whether the old FlowShift answers again (`/api/status` version).
  Native calls with stderr (venv, pip, import smoke, NSSM) go through
  `Invoke-NativeCapture`.
- Tests: `src/python/test_installer_python_probe.py` (no Windows: reproduces the
  PowerShell 5.1 argument passing and the old SyntaxError, runs the real probe
  text, checks step order and failure note) and `test_install_python_probe.ps1`
  (Windows, also in release CI: real installer functions, WebGUI build step
  then Python search under `Stop`, hostile PYTHONHOME, timeout, failing
  candidate; `-RealWebGuiBuild` uses `install_webgui.ps1`).
- The packaged end-user path remains Node-free.
- One installer for program and WebGUI: `install_flowshift.ps1` always
  replaces `%ProgramFiles%\FlowShift\webgui` from the prebuilt, VERSION-stamped
  `webgui/dist` (Vite plugin `flowshift-version-stamp` writes `dist/VERSION`),
  stops/restarts a running FlowShift, keeps `%ProgramData%\FlowShift`, asks
  nothing except UAC (`-Interactive` opt-in) and finally verifies that the
  running runtime reports the same VERSION as the WebGUI and serves
  `index.html` + `overlay.html`. `install_webgui.ps1` is only an internal build
  helper (never installs Node.js); the separate WebGUI bat/uninstaller entry
  points are removed. Setup.exe runs only `install_flowshift.ps1`;
  `build_release.ps1` requires `webgui/dist/VERSION` = VERSION; the updater
  health check requires `overlay.html` and `status.webgui.ok` for the target
  version. The uninstaller asks nothing, removes program + WebGUI and keeps
  user data unless `-PurgeData`.
- Runtime WebGUI check (`webgui_health.py`, `status.webgui`): missing
  `overlay.html` blocks the overlay host start and shows a tray balloon; version
  mismatch/unversioned WebGUI shows tray balloon + dashboard warning. The
  overlay controller backs off 1/2/5/15/30/60 s also before the first ready host
  and gives up for 10 min after 5 failed starts (`overlay.gave_up`).
- The curated release payload includes `web_api.py`, `overlay_actions.py`, and
  the other productive Python modules; its contract test imports them directly
  from the staged payload.

## Productive path

- `src/python/tray.py --tray` is the productive Windows runtime.
- `webgui/` is the React/Vite settings UI (the settings interface; new
  settings go only here) and diagnostic overlay shell.
- `src/python/gui.py` is the legacy tkinter GUI (devices, profiles, hotkeys,
  runtime control, live test); no new settings, staged retirement plan in
  `TODO_CURRENT.md` ("Next planned phase").

## Repository rules (see `AGENTS.md`)

- Development source is Forgejo `private-platform/flowshift` (`main`); GitHub
  receives only releases approved by Viktor.
- Update channels (dev.29): `stable` = GitHub `releases/latest` (stable only,
  FlowShift-Setup.exe); `development` = merged state of Forgejo
  `private-platform/flowshift` branch `main` at
  `http://forgejo.thegoodhunter.vps` (`update_model.FORGEJO_*`). No package
  build, no Forgejo runner, no workflow (Viktor 19.11. 22:09). Check:
  `GET /api/v1/repos/private-platform/flowshift/branches/main` (commit ID),
  `GET .../raw/VERSION?ref=<commit>`; only if newer the source archive
  `GET .../archive/<commit>.zip` is downloaded once and checked
  (`update_model.validate_source_archive`: ZIP comment = commit ID from
  `git archive`, one top folder, no `..`/absolute/drive paths, size limits,
  required files, VERSION matches). Its size and SHA-256 bind download
  (`updates\downloads\FlowShift-Source-<version>.zip`) and handoff (plan
  `package_kind = source`). `update_flowshift.ps1` unpacks entry by entry to
  `ProgramData\FlowShift\updates\source\<version>`, keeps the old version as
  `Program Files\FlowShift.rollback-<version>`, runs the archive's
  `install_flowshift.ps1 -FlowUpdate -NonInteractive` in the same PowerShell
  host (5.1 or 7), then the usual start + health check + rollback, and logs
  to `ProgramData\FlowShift\logs\update.log` (install steps: `install.log`).
  Moving the program folder aside (dev.31, Viktor's laptop 20.11.: dev.29 ->
  dev.30 failed with "Der Zugriff auf den Pfad C:\Program Files\FlowShift
  wurde verweigert"): the run was elevated (it only logs after
  `Test-Administrator`; the runtime runs via the `FlowShift` task with
  `RunLevel Highest`), but the runner inherited the runtime's working
  directory `Program Files\FlowShift\src\python`, and that alone blocks the
  rename. Since dev.31 `update_handoff` starts the runner with
  `cwd = ProgramData\FlowShift\updates`, the runner switches there itself
  (`Enter-RunnerDirectory`, also for the installer child processes), stops
  processes still running from the program folder (venv python, `tray.py` /
  `overlay_host.py` / `gui.py` under the base interpreter, their WebView2
  children; `Stop-FlowShiftInstallProcesses`, 10 s grace) and retries the move
  5 times; the final error contains `program folder in use`. The runner
  deletes `update_plan.json` right after its ack; the runtime removes any
  leftover plan at startup (`UpdateStateStore.remove_stale_update_plans`) and
  drops file notices whose file is gone. After a failed run the restarted
  runtime reports `last_error.code = update_install_failed` (with `cause`)
  instead of `interrupted_operation`, and `/api/update/status` carries
  `last_install {from_version, to_version, result, finished_at, installed,
  cause}` (`update_manager.classify_install_failure`: program_folder_in_use,
  runtime_did_not_stop, installer_failed, new_version_did_not_start,
  rollback_failed, other); Software Update explains it in plain words with
  what to do. A runtime without administrator rights no longer hands over
  (`not_elevated`, no unattended UAC prompt); the desktop shortcut
  `FlowShift.lnk` now starts the `FlowShift` task (`schtasks /Run`), so the
  runtime always has the task's highest privileges. An installed dev.29/dev.30
  still has the old runner: dev.31 has to be installed once by hand.
  The WebGUI of a source state is built by `install_webgui.ps1`, so Node.js
  24+ must be on the PC; `update_handoff.find_node` checks this before
  FlowShift is stopped (`node_missing`). Minimum updater for source states:
  `SOURCE_UPDATER_MIN_VERSION = 0.7.0-dev.29`; older installs switch once by
  hand. Access token: Windows Credential Manager `FlowShift:forgejo-update`
  (`update_credentials.py`), entered by Viktor once in the WebGUI
  (`POST /api/update/development-access`, write-only), sent only to the
  Forgejo origin (also on redirects); nothing in config.json, repo or
  scripts. The manager re-checks development every 15 min and on a channel
  switch; unreachable Forgejo (VPN off) -> `last_error.code =
  source_unreachable`, quiet, with `last_successful_check_at` kept. A failed
  or rolled-back version is not installed again automatically
  (`automatic_install_held`, read from `last_update_result.json`); a click on
  Install still can. A downloaded package of the other channel is not
  installable (`blocked_reason = channel_changed`). `version.SemVer` ranks
  `0.7.0` above `0.7.0-dev.N`.
- Every change to installed files, dependencies, folders, autostart, registry,
  ports, WebGUI assets or `%ProgramData%\FlowShift` data updates installer,
  updater, uninstaller and `packaging/` in the same task; checks include
  installing over an older installation and a clean uninstall.
- Every task keeps `README.md`, `HANDOFF_CURRENT.md`, `TODO_CURRENT.md`,
  `MANUAL_TEST_CHECKLIST.md` and `docs/` in line with the code.
- Rust (`src/service`, `src/viewer`) remains experimental.

## Verified baseline (release regression for `v0.6.3`)

Run locally on Windows with CPython 3.12 and Node.js 26.5.0 using the exact
release-workflow commands (CI runs CPython 3.14.6 / Node.js 24.18.1):

- `python -m compileall src/python`: clean.
- `python -m unittest discover -p "test_*.py"` in `src/python`: 627 tests OK on CPython 3.12 and 3.13 (the latter with an 8.3 short-name TEMP)
  (one skipped Windows symlink-privilege test), incl. transport V2 (28),
  stress V2 (10: 10k typed frames, 100 disconnect/resume cycles, 200-file
  batch, cancel storm, concurrent status polling, slow receiver window bound,
  disk-full resume, 10 restarts without thread leak, 1k malformed frames,
  >4 GiB offsets), tray localhost E2E (3, real TCP through `peer_handler` /
  `_clip_send` / `_clip_open_channel`), transfer control V2 (24 incl. raced
  purged-journal release), `test_service.py`, updater, WebGUI API, overlay,
  and legacy transfer/sync/streaming suites.
- `worker_smoke_test.py` (activate answered in 0.02 s locally), `e2e_test.py`, `reconnect_stress_test.py 30`,
  `overlay_ipc_stress_test.py`, `overlay_show_hide_stress_test.py`: exit 0.
- `test_update_flowshift.ps1`: 7 passed; all PowerShell sources parse.
- `python -m pip_audit -r requirements.txt`: no known vulnerabilities.
- WebGUI: `npm ci`, `npm audit --include=dev --audit-level=high` (0
  vulnerabilities after vitest 4.1.11 / nanoid 3.3.19), `npm test` (9 passed),
  `npm run build`: OK.
- `packaging/test_release_packaging.ps1`: passed.

## Last pushed commits

- `5e0c659` - Phase 4 dev.16: refresh WebGUI dependencies (task branch).
- `fda49a6` - Release v0.6.3: Phase 3 clipboard transfer hardening (tag `v0.6.3`; workflow success, release published).
- `e7bb274` - Release v0.6.2: Phase 3 clipboard transfer hardening (tag `v0.6.2`; smoke-test timeout on CI, workflow failed).
- `9678b2b` - Release v0.6.1: Phase 3 clipboard transfer hardening (tag `v0.6.1`; VERSION not updated, workflow failed).
- `8995b8c` - Phase 3 v0.6.1-dev.1: make path containment and status-polling tests CI-stable (VERSION not updated).
- `a7dcadc` - Release v0.6.0: Phase 3 clipboard transfer hardening (tag `v0.6.0`; workflow failed, see release state).
- `6005228` - Phase 3 dev.17: close V2 documentation, release raced receiver stages, and patch WebGUI audit findings.

## Last successful focused tests (Phase 4)

- Clipboard overlay fixes (dev.40), Linux: `test_overlay_dismiss` (drag end
  on release outside/Escape/focus loss, search-field click inside with stale
  placement, settle phase, previous foreground), `test_web_api_clipboard`
  (newest first, source folder root/path, setting validation),
  `test_web_api_actions` (setting saved/read back), `test_clipboard_events`
  (own text write suppressed, no new item, current unchanged), overlay
  modes/shape/native wheel/gesture/actions: OK. Not run here (no Windows /
  no `node_modules`): `test_tray_overlay_actions` (payload field), WebGUI
  `npm test` (`ClipboardOverlay.test.jsx`, `ClipboardView.test.jsx`) and
  `npm run build`.

- Repeated text, HTML, image, file and file-batch copy events create distinct
  persistent history items while content identities and object bytes remain
  deduplicated; each repeated multi-file selection remains one batch item.
- Release packaging includes and imports `overlay_actions.py` from the curated
  staged payload; `packaging/test_release_packaging.ps1` passes.
- Python dependency/toolchain refresh: hashed runtime and audit locks install
  on CPython 3.13; Pillow 13.0.0, CFFI 2.1.1 and pythonnet 3.2.1 publish
  CPython 3.15-compatible Windows artifacts; `pip-audit` reports no known
  vulnerabilities. Toolchain policy, clipboard GIF, overlay foundation and
  overlay lifecycle tests pass; PowerShell parsing and Python compile checks
  are clean. GitHub Actions supplies the pinned CPython 3.15.1 release.
- Python: test_overlay_actions, test_web_api_actions, test_web_api_clipboard, test_web_api_updates, test_overlay_modes, test_tray_overlay_actions, test_tray_stream_v2_e2e (unittest); test_overlay_lifecycle.py, test_overlay_foundation.py, test_service.py, overlay_show_hide_stress_test.py, overlay_ipc_stress_test.py: all OK.
- Hold and swipe (dev.26): `test_wheel_gesture` (30), `test_overlay_dismiss`,
  `test_overlay_modes`, `test_overlay_actions`, `test_web_api_actions`,
  `test_hotkey_status` (99 tests) and `test_overlay_foundation` (110 checks):
  OK on Linux. Not run here (no Windows/Node): `test_tray_overlay_actions`
  (new side button / hold tests), `test_release_packaging.ps1`, WebGUI
  `npm test` (new trigger and aim tests) and `npm run build`.
- Clipboard cards: `test_web_api_clipboard.py` (8 tests), changed-module
  compile checks, WebGUI `npm test` (40 tests), `npm run build`, and
  `git diff --check`: OK. Linux skips the 13 Windows-only tray overlay tests.
- Installer/WebGUI consistency (dev.23): `test_webgui_health` (11 tests incl.
  overlay give-up/backoff), `test_web_api_actions`, `test_web_api_clipboard`,
  `test_web_api_updates`, `test_tray_overlay_actions` (Windows-only cases
  skipped on Linux), Python compile: OK. Not run here (no Windows/Node 24):
  PowerShell parsing, `test_release_packaging.ps1`, `test_update_flowshift.ps1`,
  `test_overlay_lifecycle.py`, WebGUI `npm test` (new `Dashboard.test.jsx`) and
  `npm run build`.
- Transfer telemetry: `test_clipboard_transfer_control_v2` (25 tests),
  `test_clipboard_transport_v2` (28 tests), WebGUI `npm test` (41 tests), Python
  compile and `git diff --check`: OK. Stream V2 now persists all per-file
  `storage_state=verified` transitions in one contiguous journal generation;
  the latest 200-file batch stress passes in 101.937 s instead of exceeding 240 s.
- WebGUI baseline before this slice: npm test (34 tests) and npm run build: OK.
- Dependency refresh on Node 24.18.1 / npm 12.0.2: clean `npm ci`, 34 tests,
  production build, and `npm audit --include=dev --audit-level=high` with zero
  findings. Linux Python discovery completed 677 tests in 454.650 s; five
  assertions and the Windows named-pipe lifecycle module remain non-green on Linux.

- Update channel (dev.29, source states from Forgejo main):
  `test_update_channel` (28: commit binding, unsafe archive paths, token only
  to Forgejo, VPN off, Node probe, held automatic install),
  `test_update_manager`, `test_update_handoff`, `test_update_stress`,
  `test_update_runtime`, `test_web_api_updates`, `test_update_client.py`,
  `test_update_safety.py`, `test_version_config.py`: OK on Linux. Not run here
  (no Windows/Node 24): WebGUI `npm test` and `npm run build`, PowerShell
  parsing, `test_update_flowshift.ps1`, `test_release_packaging.ps1`, a real
  source update on Windows (check steps in `docs/install_test_checklist.md`).
- Update "access denied" fix (dev.31): `test_update_handoff` (runner
  `cwd` outside the program folder, `not_elevated`), `test_update_manager`
  (Viktor's dev.29 -> dev.30 state: plan removed, `update_install_failed` /
  `program_folder_in_use`, `last_install`; failure causes),
  `test_update_channel`, `test_web_api_updates`: OK on Linux. Not run here (no
  Windows/Node): PowerShell parsing, `test_update_flowshift.ps1` (new: busy
  folder retried, permanently busy folder keeps and restarts the old
  version), WebGUI `npm test` (new last-install tests) and `npm run build`,
  a real update on Windows.

- Install lock and safe replace (dev.35; 21.11. two update runs and a manual
  install of an older checkout ran at once, one removed the other's source
  and rollback copy, FlowShift was gone): `test_install_lock` (lock held =
  sharing violation, busy runner ack, handoff stops before writing the plan,
  install blocked while locked, first start after a manual install waits one
  check interval), `test_installer_python_probe` (staging before stop, swap
  after the check, lock before step 1, marker before start, previous folder
  removed only after the final check), `test_update_manager`,
  `test_update_handoff`, `test_update_channel`, `test_update_stress`,
  `test_web_api_updates`, `test_tray_overlay_actions`: OK on Linux (152
  tests). Not run here (no Windows/Node): PowerShell parsing,
  `test_update_flowshift.ps1` (new: held lock -> `busy` without changes,
  lock free after a run, rollback failure keeps the copy and notifies, missing
  copy leaves the folder untouched, failed start and failed backup bring the
  old version back, older copies removed only after success),
  `test_release_packaging.ps1` (`install_lock.py` in the payload), WebGUI
  `npm test` (`install_busy` hint) and `npm run build`, a real install and
  update on Windows (`docs/install_test_checklist.md` L).

- Native Command Wheel (dev.36): `test_overlay_native_wheel` (27: sector hit
  test incl. gaps/hub/corners, single-slot ring, paging, aim over hover,
  nothing drawn beyond the outer radius, hub only pill + two dots, error
  label, host shows the wheel natively without region, clipboard back on
  WebView2, aim/hide/paging routed natively, fallback to WebView2 on start or
  paint failure, action POST incl. 409/400/unreachable),
  `test_overlay_window_shape`, `test_overlay_dismiss`, `test_overlay_modes`,
  `test_overlay_foundation`, `test_toolchain_policy`: OK on Linux. Not run here
  (no Windows): the real layered window (GDI+/`UpdateLayeredWindow`), the
  pixel check on the desktop, `test_overlay_lifecycle.py`,
  `test_tray_overlay_actions` Windows cases, `test_release_packaging.ps1`
  (`overlay_native_wheel.py` in the payload), PowerShell parsing.

- Clipboard multi-select (dev.37): `test_clipboard_delete_selected` (11:
  store/manager delete of selected ids, pinned deleted when selected,
  current cleared, history empty after restart, endpoint validation, origin
  and content-type guard, `local` fallback), `test_web_api_clipboard`,
  `test_clipboard_semantics`, `test_clipboard_materialize_v2`, `test_clipboard`:
  OK on Linux. `test_clipboard_object_store_v2.test_no_second_full_read_in_normal_publication_commit_and_availability`
  fails on Linux independently of this change (POSIX rehash path; file not
  touched). Pure selection rules checked with node. Not run here (no
  `node_modules`): WebGUI `npm test` (new `clipboardSelection.test.js`,
  multi-selection cases in `ClipboardOverlay.test.jsx` and
  `ClipboardView.test.jsx`) and `npm run build`; the overlay on Windows.

## Open work

- Full release regression and the `v0.7.0` tag workflow remain open in
  `TODO_CURRENT.md`.
- Manual hardware and VM checks remain open in `TODO_CURRENT.md`, including
  the Phase 3 two-device matrix in `MANUAL_TEST_CHECKLIST.md`.
- Development channel: needs no build infrastructure (source states of
  `main`); open are only the Windows check steps in `TODO_CURRENT.md`.

## Next planned phase

- None defined after Phase 4.
