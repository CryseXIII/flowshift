# FlowShift Clipboard

FlowShift keeps its **own** clipboard history per profile and syncs only the
items a peer is missing (manifest-based). This document describes what is
**implemented and tested today** versus what is still being built, so it never
promises more than the code delivers.

The explicit clipboard semantics (history, availability, current item, leases)
are specified in [`clipboard_semantics.md`](clipboard_semantics.md); the
streaming file transfer engine `clipboard_stream_v2` is specified in
[`clipboard_transfer_v2.md`](clipboard_transfer_v2.md). The status matrix below
describes the current productive behavior.

## Honest status matrix

| Area | Status | Notes |
|---|---|---|
| Data model (`clipboard_model.py`) | **Done + tested** | kinds, sha256, item shapes, manifest build/parse, sync diff (dedup, only-missing, manual-required by size), limits + FIFO/size eviction + pinning, byte/rate/ETA formatting, ZIP-strategy decision, chunk planning, disk-space guard, settings + clamping |
| Per-profile store (`clipboard_store.py`) | **Done + tested** | `%ProgramData%\FlowShift\clipboard\profiles\<id>\` with `index.json` + content-addressed `objects/<sha256>` (auto-dedup), add/list/get, delete-one, delete-selected, delete-all, pin/unpin, size accounting, eviction, persistence across restarts, manifest from store |
| Wire protocol (`clipboard_protocol.py`) | **Done + tested** | manifest / request_items / sync_result, transfer start/chunk/ack/complete/error/resume, base64 chunks under `MAX_FRAME_SIZE`, `ChunkAssembler` with resume/retry/duplicate/hash-mismatch detection |
| Config + WebGUI settings (`clipboard` block) | **Done + tested** | productive settings are editable in WebGUI **Settings**; saves are normalised + clamped via the model before persistence |
| Installer/uninstaller clipboard dirs | **Done** | installer creates the store dirs; uninstaller asks about deleting the history and always cleans temp |
| Runtime manager + manifest sync (`clipboard_runtime.py`) | **Done + tested** | per-profile stores, capture, on-activation manifest exchange, diff → request-only-missing, chunked transfer send/receive, dedup, manual-required + manual retry (integration-tested with two managers) |
| **Text** capture + live sync + paste | **Done + tested** | Windows `CF_UNICODETEXT` read/set (`clipboard_win.py`); watcher captures local text into each peer's store; on profile activation the peer pulls only missing text items; the React WebGUI and overlay can set an item back to the Windows clipboard. Verified in the runtime (worker_smoke Test E) and end-to-end between two managers (`test_clipboard_sync.py`) |
| React clipboard history (view/paste/delete/pin/retry) | **Done + tested** | WebGUI `/clipboard` and the cursor-positioned overlay provide per-profile search/list, set-to-clipboard, pin/unpin, delete, multi-select delete (All/Invert/None, no confirmation)/manual retry as applicable; async refresh keeps the list DOM node and scroll offset stable |
| **File / batch** capture + sync + paste | **Done + tested** | between two `0.6.0` peers files stream over `clipboard_stream_v2` (`clipboard_transport_v2.py`, `clipboard_streaming_v2.py`): metadata-first capture, raw binary frames on a dedicated channel, per-file SHA-256 objects, persistent journals with resume after disconnect and restart, hardlink-or-verified-copy materialization into a lease directory and `CF_HDROP` set. For older peers (or `clipboard_transfer_v2_force_legacy`) `clipboard_files.py` falls back to a deterministic ZIP over the legacy chunked path, unpacked to `temp/incoming`. Locally-captured items paste original paths without a copy. Integration-tested (`test_clipboard_transport_v2`, `test_tray_stream_v2_e2e`, two-manager legacy roundtrip) + runtime (worker_smoke Test F) |
| Windows CF **image** (PNG, CF_DIBV5, CF_DIB) + thumbnails | **Done + tested** (Windows paste check open) | `clipboard_image.py` (DIB↔BMP, PNG trim/size, sampled BMP thumbnail incl. alpha, `paste_formats`); `clipboard_win.py` reads the registered `PNG` first, else the DIB the program placed first, and sets `PNG` + `CF_DIBV5` + `CF_DIB`; items sync as PNG or BMP blob; the card shows a thumbnail from the preview cache (see "Copied pictures"). Tests: `test_clipboard_image_preview.py`, two-manager PNG/BMP roundtrip, worker_smoke Test G |
| Windows CF HTML | **Done + tested** | `clipboard_html.py` builds/parses CF_HTML with byte-correct fragment offsets and safe text previews; `clipboard_win.py` reads/sets the registered `HTML Format` plus plaintext fallback; watcher, manager sync, local control and Web API paste paths are wired |
| Clipboard overlay + WebGUI history | **Done + tested** | the fixed-height React overlay opens at the cursor and the full WebGUI is directly routable at `/clipboard`; both use asynchronous API data rather than a Tkinter history window |
| Per-item transfer progress | **Done + tested** | React list/detail views merge legacy `clip_progress` with stream V2 diagnostics and update without remounting or blocking the UI. Stream V2 reports independent current-file and total bytes/percent, one-based file ordinal/count, rate, `HH:mm:ss` ETA and last-file duration; batch cards show two bars while singleton cards show one |
| GIF handling | **Payload path done + tested** | Pillow decodes bounded frame sets and the official installer includes Pillow; copied GIF/APNG/animated WebP files play as scaled animations on their file cards (see "File previews"); GIF picture items get the same scaled animation on their card |
| Clipboard overlay hotkeys | **Done (hardware verify open)** | with clipboard enabled, the configurable overlay hotkey (`clipboard.overlay_hotkey`, default **Ctrl+Alt+V**, WebGUI Settings > Clipboard > Overlay hotkey) and **Ctrl+Win+V** open the FlowShift overlay window at the mouse; optional **Win+V** interception tries to suppress the OS history. Windows usually reserves Win+V and often Ctrl+Win+V (`RegisterHotKey` error 1409); every busy hotkey is reported in a tray notification, on the WebGUI Dashboard and next to the hotkey in Settings with the combination that works instead (`GET /api/status` -> `overlay_hotkeys`) |

**In short:** **text, HTML, file/batch and image** clipboard paths work and are
tested. History management lives in the React WebGUI and cursor-positioned
overlay, with stable scrolling and live legacy/V2 progress. The old Tkinter
clipboard tab/window and `gui.py --clipboard` entry no longer exist.

## Concepts

- **Own history, not the Windows history.** FlowShift maintains its own ordered
  list. The Windows clipboard is the source (on copy) and the target (on paste).
- **Per profile.** Each peer/profile identity has its own history directory, so
  `Laptop → Surface` and `Surface → Laptop` do not mix.
- **Content-addressed dedup.** Blobs are stored under their SHA-256, so identical
  content is stored once and already-known items are never re-transferred.
- **Copy events stay distinct.** Copying identical content again appends another
  history item/card while reusing the same content-addressed payload bytes.
- **Manifest-based sync.** The source sends metadata only; the target requests
  just the items it is missing (in source order). Copying 3 new things when 200
  are already known transfers exactly 3 items.

## Limits (configurable, WebGUI Settings)

| Setting | Default | Range |
|---|---|---|
| `history_max_items` | 200 | 20–999 |
| `history_max_total_gb` | 2.0 | 0.1–100 |
| `max_auto_transfer_mb` | 100 | items larger are manual-only (retry/download) |
| `max_item_gb` | 50 | high, but manual + chunked only |
| `max_retries` | 5 | 0–100 |

Eviction is FIFO (oldest first); **pinned** items are never auto-evicted. The
count cap is enforced first, then the total-size cap.

## Large files, chunking, retry, ZIP

**Default between `0.6.0` peers: `stream_v2`.** When both peers advertise
`clipboard_stream_v2` in their `hello`, file and batch payloads are streamed
without ZIP or Base64. Control messages (`clipboard_stream_v2_offer/accept/
reject/resume_request/resume_response/cancel/cancel_ack`) travel on the normal
peer link; the payload uses a dedicated TCP channel with typed binary frames
(2 MiB chunks by default, 4 MiB frame bound, per-chunk SHA-256). The receiver
sends cumulative window ACKs (`clipboard_stream_v2_ack`); the sender keeps a
bounded in-flight window and marks a transfer complete only after the
receiver's `complete_ack`. Both sides keep persistent journals under
`clipboard\journals\{incoming,outgoing}` and resume from the durable offset
after a disconnect or after a sender, receiver, or dual restart. Every verified
file lands once in the shared per-file object store
(`clipboard\objects\sha256\...`); pasting materializes the batch into a lease
directory by hardlink or verified copy. Timeouts are finite and configurable
(see the settings reference). Status is exposed under
`diagnostics()["stream_v2"]` / `/api/clipboard/status`. Details:
[`clipboard_transfer_v2.md`](clipboard_transfer_v2.md).

**Legacy fallback (`legacy_zip_v1`)** for peers without the capability or
with `clipboard_transfer_v2_force_legacy: true`:

- Transfers are **chunked** (chunk size chosen so a base64 chunk + JSON envelope
  stays under `MAX_FRAME_SIZE = 28 MiB`). Each chunk can carry a SHA-256 for
  per-chunk verification; the whole item is verified against its SHA-256 on
  completion.
- Large or already-persisted payloads use disk-backed `FileTransferSource`
  objects and are read one chunk at a time instead of loading the entire blob in
  memory. Temporary bundle sources carry active markers and are removed after
  completion; age-based cleanup avoids deleting active transfers.
- **Retry / resume:** the `ChunkAssembler` reports missing indices and the next
  index to resume from; duplicate and hash-mismatched chunks are detected so the
  receiver can request a retry. There is no journal and no resume across a
  restart in the legacy path.
- **ZIP strategy** (`clipboard_model.zip_strategy`, legacy only): single file →
  direct; many already-compressed files (jpg/mp3/…) → multi-file; compressible
  batches use a deterministic bundle. Large bundles can be built to a temporary
  file and streamed from disk. Temp zips are not kept permanently.
- **Disk-space guard** (`has_enough_space`): the receiver checks free space
  before a large transfer; if there is not enough, the item shows a clear
  `Nicht genug Speicherplatz` error instead of a half transfer. (The guard logic
  is implemented + tested; enforcement is wired in the transfer layer.)

## Where the data lives

```
%ProgramData%\FlowShift\clipboard\        # <FLOWSHIFT_LOG_DIR or DATA_DIR>\clipboard
  profiles\<profile_id>\
    index.json          # ordered history + revision
    objects\<sha256>    # legacy content-addressed blobs (text/HTML/image/ZIP)
    previews\           # reserved preview cache
    temp\               # per-profile scratch
  objects\sha256\<prefix>\<hash>   # V2 shared per-file objects (content-addressed)
  objects\pending-v2\              # V2 publication pins of in-flight transfers
  manifests\sha256\<prefix>\<digest>.json  # finalized V2 batch manifests
  incoming\<transfer_id>\          # V2 receiver staging (.part / .verified)
  journals\incoming\<transfer_id>.json     # V2 resume journals (receiver)
  journals\outgoing\<transfer_id>.json     # V2 resume journals (sender)
  temp\incoming\        # legacy receiver scratch for in-flight transfers
  temp\outgoing\        # legacy sender scratch (temp zips); unused by V2
```

Pasted V2 batches are materialized into a lease directory
`<dest_root>\<profile>\<item_id>` (hardlinks into `objects\sha256` on the same
volume, verified copy otherwise); the lease is retired when the Windows
clipboard moves on and the tree is removed by the age-based lease cleanup.

## Managing the history

- WebGUI **Settings**: enable/disable, persistence, history/transfer limits,
  overlay hotkey (with its live registration status), direction mode, Win+V
  interception and ZIP strategy (legacy path only).
- The local history (`local`, "This PC") is a store, not a peer: captures land
  there without any announcement; only real peer identities receive
  `clipboard_announce` messages.
- WebGUI **Clipboard** (`/clipboard`): select a profile, search, set/get,
  pin/unpin, delete, sync and inspect per-item transfer progress.
- Multi-select (overlay and WebGUI): checkbox per card, Ctrl-click toggles,
  Shift-click selects a range; the bar shows `N of M selected` as text and
  four icon buttons with tooltip and `aria-label`: `All` (Ctrl+A), `Invert`
  (Ctrl+I), `None` (Ctrl+Shift+A) and the trash can `Delete selected` with the
  count as a badge (Delete key, greyed out without a selection). The
  selection spans the whole clipboard of the profile: `All`, `Invert` and
  `None` act on every entry, Recent and Pinned, without exception; the tabs
  only change which entries are shown, switching tabs keeps the selection,
  and `M` in `N of M selected` counts all entries (after the search filter).
  Deleting acts at once without any confirmation, pinned entries included.
  Clearing the whole list is `All` + `Delete`; the former WebGUI `Clear`
  button (with a confirmation dialog) is gone. `POST /api/clipboard/delete?profile=<id>` with
  `{"item_ids": [...]}` (local host/origin, JSON, at most 10 000 ids) returns
  `{"deleted": n, "item_ids": [...]}` and writes `index.json` once
  (`ClipboardManager.delete_items` -> `ClipboardStore.delete_items`).
- Order: both views list the most recently copied item first
  (`created_at`, then the local sequence; `clipboard_model.newest_first` in
  `GET /api/clipboard/items`). The store keeps insertion order.
- Copy actions on file cards (overlay and WebGUI, row `Copy`): `Full path`,
  `Folder path`, `File name`, `Root name`, `Relative path`, each with a
  tooltip. `POST /api/clipboard/item/<id>/copy-text` with `field` =
  `full_paths` | `folder_paths` | `file_names` | `root_names` |
  `relative_paths` writes exactly that text (one value per line, folders
  once) through `tray.write_clipboard_text` = `perform_windows_write(...,
  mark_current=False)`, so the watcher records no new item. Full and folder
  paths are the private `source_paths` of a local copy (as Explorer put them
  on the clipboard); the host writes them, the API answer and the card list
  never contain them, the card only gets `file_card.local_paths` (bool).
  Items received from a peer have no local paths: the two buttons stay
  dimmed with the tooltip "Only for files copied on this PC". `Set` still
  puts the files themselves on the clipboard. Batch cards list the file
  names, preview tooltips show the relative path; there is no display
  switch (the dev.40 `Show files as` / `clipboard.file_path_display` was
  removed in dev.45). Files copied loose from one folder get that folder's
  name (only the last path part of the private `base`) as root and as start
  of the relative path; a copied folder keeps its own name as root. Received
  items without a local `base` keep the manifest names.
- File previews (`clipboard_file_preview.py`): every file card shows a small
  preview per file, a single file in the card's icon slot, several files as
  a row of the first four tiles plus `+N`. Images get a Pillow thumbnail;
  GIF, APNG and animated WebP a scaled animation (always, or only while the
  mouse is over the card: `clipboard.file_preview_animation` = `always` |
  `hover`, Settings > Clipboard `Animated File Previews`); videos the Windows
  shell thumbnail as still plus, for mp4/m4v/mov/webm/ogv, a short muted clip
  on hover (`file_preview_video_hover`); every other file its Windows type
  icon. Generation runs on a background thread; `GET
  /api/clipboard/file-previews/<item_id>?profile=&count=` answers at once
  with `state` `pending` | `ready` | `missing` and `kind` `image` |
  `animated` | `video` | `icon` | `none` per file (index, extension, cache
  key prefix, never a path); images come from `GET
  /api/clipboard/file-preview/<item_id>/<index>/still|anim?v=<key>`, the
  video clip from `.../video` (HTTP Range, the copied file itself, only while
  it is unchanged). The cards load them when they scroll into view and ask
  again while `pending`. Missing, moved, changed or locked files and
  unsupported types show a neutral tile with the extension. Limits: images
  over 64 MB or 80 megapixels only get the type icon, animations are read up
  to 32 MB and 90 frames, nothing reads a large file completely. Cache:
  `%ProgramData%\FlowShift\clipboard\previews`, at most
  `file_preview_cache_mb` (default 128, 8-4096), oldest first down to 80 %,
  entries unused for 30 days are removed; an uninstall removes it even when
  the history is kept. Received items without local files show the type
  icon by extension. Tooltips show the relative path.
  All defaults and ranges of these settings and limits are kept only in the
  central clipboard configuration (`clipboard_model.DEFAULT_CLIPBOARD_SETTINGS`,
  ranges in `FILE_PREVIEW_INT_LIMITS`) and can be overridden in the
  `clipboard` section of `config.json`:

  | Key | Default | Range | Meaning |
  |---|---|---|---|
  | `file_preview_animation` | `always` | `always`/`hover` | animations always or only on hover |
  | `file_preview_video_hover` | `true` | | muted clip when hovering a video |
  | `file_preview_cache_mb` | 128 | 8-4096 | disk cache size |
  | `file_preview_cache_max_age_days` | 30 | 1-3650 | unused entries are removed after this |
  | `file_preview_px` | 96 | 32-512 | preview edge in pixels (tiles are 40-48 CSS px, 2x HiDPI) |
  | `file_preview_icon_px` | 32 | 16-256 | type icon edge in pixels |
  | `file_preview_max_count` | 8 | 1-32 | previews per card at most |
  | `file_preview_max_image_source_mb` | 64 | 1-4096 | larger images only get the type icon |
  | `file_preview_max_animated_source_mb` | 32 | 1-4096 | larger animations only get a still |
  | `file_preview_max_passthrough_mb` | 4 | 1-64 | unscaled images when Pillow is missing |
  | `file_preview_max_source_megapixels` | 80 | 1-1000 | larger images are not decoded |
  | `file_preview_max_frames` | 90 | 2-1000 | frames of a preview animation |
  | `file_preview_gif_scan_kb` | 2048 | 64-65536 | bytes read to tell an animated GIF |
  | `file_preview_miss_retry_s` | 15 | 1-3600 | wait before a missing/locked file is checked again |
  | `file_preview_response_max_mb` | 16 | 1-256 | largest cached preview the API serves |
- Copied pictures (browser "Copy image", a cut-out from Snipping Tool, Paint
  or another program; no file): Windows offers them as `CF_DIB`/`CF_DIBV5`,
  often also as the registered `PNG` format and an `HTML Format` fragment
  with the image address. Before dev.47 FlowShift kept only `CF_DIB` (the
  transparency of browser pictures was lost, `PNG` was dropped) and the
  card showed nothing, the PPM thumbnail could not be drawn by the WebGUI.
  Since dev.47 `clipboard_win.read_image` keeps the most complete copy
  (`pick_image_source`): the `PNG` bytes when present (`PNG` or `image/png`,
  trimmed behind `IEND`, item mime `image/png`, transparency kept), else the
  DIB the program placed first (`CF_DIBV5` with its alpha mask or `CF_DIB`)
  as a BMP blob (mime `image/bmp`). Setting the item again
  (`clipboard_image.paste_formats`) puts a PNG item back as `PNG` plus
  `CF_DIBV5` (alpha) and `CF_DIB` (transparent parts on white, for Paint and
  other programs that only read bitmaps; the two bitmaps need Pillow, which
  the installer ships), a BMP item as its original DIB. The card shows the
  picture in the icon slot like a file preview (same routes
  `GET /api/clipboard/file-previews/<item_id>` and
  `.../file-preview/<item_id>/0/still`, row field `source: "clipboard"`),
  whole on a checkerboard so transparency stays visible. The thumbnail is
  built on the preview thread (`clipboard_file_preview.generate_clip_image`,
  Pillow, or without Pillow a sampled BMP decode that reads only the
  thumbnail pixels, so an 8K screenshot costs the same as a small one), at
  most `file_preview_px` large, keyed by the item's content hash and kept in
  the same preview cache (size limit, age cleanup above). Pictures not on
  this device yet (from a peer, not transferred) and damaged ones show the
  neutral `IMG` tile. HTML that is only an image address without picture
  data (`metadata.image_only`) shows the neutral `IMG` tile; the address is
  never loaded, and the HTML detail frame blocks every network load
  (Content-Security-Policy: only `data:` images). Both peers need dev.47 or
  newer to paste a PNG picture received from the other device.
- The card `Copy` button writes through the same write suppression as `Set`
  (`ClipboardManager.perform_windows_write(..., mark_current=False)`): the
  watcher recognizes FlowShift's own write, no new history item appears and
  `Current` does not change.
- Overlay window: it is moved by dragging the grip `⠿`, the header, the tab
  row or the footer; the overlay host follows the cursor while the left
  button is physically down and ends on release anywhere, Escape or focus
  loss (`overlay drag end reason=...` in `flowshift.log`); pywebview's
  `easy_drag` is off. Clicks on the overlay's own windows (search field,
  dropdowns) never count as outside clicks, and a foreground change right
  after the show (the wheel opening the clipboard) does not close it.
- Deleting history items never touches the Windows clipboard. If the deleted
  item is the current one, only the `Current` mark is gone; the content stays
  pasteable with Ctrl+V and returns to the history only when copied again.
- Delete one / several items, delete the whole history and clean temp are
  store operations (`ClipboardStore.delete_item`, `.delete_items`, `.clear`,
  `.cleanup_temp`). The history lives in
  `%ProgramData%\FlowShift\clipboard\profiles\<id>\index.json` plus
  `objects/` and `previews/`.
- Uninstaller asks whether to delete the clipboard history and always cleans
  transient temp files.

## Settings reference (`config.json` → `clipboard`)

See `src/python/config.example.json` for a full example. All keys are validated
and clamped by `clipboard_model.clipboard_settings`, so a bad value can never
destabilise the runtime.

Transfer-engine keys added with `stream_v2`:

| Setting | Default | Notes |
|---|---|---|
| `clipboard_transfer_v2_force_legacy` | `false` | `true` stops advertising `clipboard_stream_v2`, selects `legacy_zip_v1` locally and rejects incoming V2 offers with `legacy_only` |
| `clipboard_transfer_v2_preflight_timeout_s` | 60 | wait for the peer's accept/reject after an offer, and for the first payload after accept |
| `clipboard_transfer_v2_manifest_ack_timeout_s` | 30 | wait for `manifest_ack` on the data channel |
| `clipboard_transfer_v2_window_ack_timeout_s` | 30 | wait for a cumulative window ACK while the send window is full |
| `clipboard_transfer_v2_no_progress_timeout_s` | 60 | receiver without new bytes -> checkpoint and `paused` |
| `clipboard_transfer_v2_reconnect_wait_timeout_s` | 300 | how long a paused / `waiting_reconnect` transfer keeps waiting before it fails (journal and partials retained) |
| `clipboard_transfer_v2_final_complete_ack_timeout_s` | 30 | wait for `complete_ack` / publication; purge deadline for cancelled transfers |

All `*_timeout_s` values are read by `clipboard_transfer_control_v2.TransferTimeouts.from_settings`
and clamped to 1..86400 seconds; invalid values fall back to the default.
`zip_strategy` and the `clipboard_transfer_final_ack_*` keys affect only the
legacy `legacy_zip_v1` path.
