# FlowShift Clipboard

FlowShift keeps its **own** clipboard history per profile and syncs only the
items a peer is missing (manifest-based). This document describes what is
**implemented and tested today** versus what is still being built, so it never
promises more than the code delivers.

The explicit clipboard semantics (history, availability, current item, leases)
are specified in [`clipboard_semantics.md`](clipboard_semantics.md); the
streaming file transfer engine `clipboard_stream_v2` is specified in
[`clipboard_transfer_v2.md`](clipboard_transfer_v2.md). The status matrix below
describes the shipped `0.6.0` behavior.

## Honest status matrix

| Area | Status | Notes |
|---|---|---|
| Data model (`clipboard_model.py`) | **Done + tested** | kinds, sha256, item shapes, manifest build/parse, sync diff (dedup, only-missing, manual-required by size), limits + FIFO/size eviction + pinning, byte/rate/ETA formatting, ZIP-strategy decision, chunk planning, disk-space guard, settings + clamping |
| Per-profile store (`clipboard_store.py`) | **Done + tested** | `%ProgramData%\FlowShift\clipboard\profiles\<id>\` with `index.json` + content-addressed `objects/<sha256>` (auto-dedup), add/list/get, delete-one, delete-all, pin/unpin, size accounting, eviction, persistence across restarts, manifest from store |
| Wire protocol (`clipboard_protocol.py`) | **Done + tested** | manifest / request_items / sync_result, transfer start/chunk/ack/complete/error/resume, base64 chunks under `MAX_FRAME_SIZE`, `ChunkAssembler` with resume/retry/duplicate/hash-mismatch detection |
| Config + GUI settings (`clipboard` block) | **Done** | all settings editable in the GUI **Clipboard** tab (no JSON hand-edit), normalised + clamped via the model |
| Installer/uninstaller clipboard dirs | **Done** | installer creates the store dirs; uninstaller asks about deleting the history and always cleans temp |
| Runtime manager + manifest sync (`clipboard_runtime.py`) | **Done + tested** | per-profile stores, capture, on-activation manifest exchange, diff → request-only-missing, chunked transfer send/receive, dedup, manual-required + manual retry (integration-tested with two managers) |
| **Text** capture + live sync + paste | **Done + tested** | Windows `CF_UNICODETEXT` read/set (`clipboard_win.py`); watcher captures local text into each peer's store; on profile activation the peer pulls only missing text items; GUI list can set an item back to the Windows clipboard. Control API + GUI history viewer wired; verified in the runtime (worker_smoke Test E) and end-to-end between two managers (`test_clipboard_sync.py`) |
| GUI clipboard history list (view/paste/delete/pin/retry) | **Done (basic)** | per-profile list with size/status, set-to-clipboard, pin/unpin, delete, clear, manual retry |
| **File / batch** capture + sync + paste | **Done + tested** | between two `0.6.0` peers files stream over `clipboard_stream_v2` (`clipboard_transport_v2.py`, `clipboard_streaming_v2.py`): metadata-first capture, raw binary frames on a dedicated channel, per-file SHA-256 objects, persistent journals with resume after disconnect and restart, hardlink-or-verified-copy materialization into a lease directory and `CF_HDROP` set. For older peers (or `clipboard_transfer_v2_force_legacy`) `clipboard_files.py` falls back to a deterministic ZIP over the legacy chunked path, unpacked to `temp/incoming`. Locally-captured items paste original paths without a copy. Integration-tested (`test_clipboard_transport_v2`, `test_tray_stream_v2_e2e`, two-manager legacy roundtrip) + runtime (worker_smoke Test F) |
| Windows CF **image** (CF_DIB) + thumbnails | **Done + tested** | `clipboard_image.py` (DIB↔BMP, uncompressed 24/32-bit BMP→PPM decode with nearest-neighbour downscale, unsupported→placeholder); `CF_DIB` read/set (`clipboard_win.py`); capture screenshots/images, sync as a BMP blob, paste back as `CF_DIB`; the window shows real PPM thumbnails. Integration-tested (two-manager image roundtrip + thumbnail) + runtime (worker_smoke Test G) |
| Windows CF HTML | **Done + tested** | `clipboard_html.py` builds/parses CF_HTML with byte-correct fragment offsets and safe text previews; `clipboard_win.py` reads/sets the registered `HTML Format` plus plaintext fallback; watcher, manager sync, local control and Web API paste paths are wired |
| Clipboard history WINDOW (list, draggable splitter, thumbnails, per-item progress) | **Done (basic)** | resizable `ClipboardWindow`: per-profile item cards, a draggable splitter (ttk.Panedwindow) between preview and text, thumbnail-size modes (klein/mittel/gross), **real image thumbnails** (async PPM), search, per-item progressbar with live transfer telemetry (bytes/percent/rate/ETA via `clip_progress`), paste/retry/pin/delete/clear. Per-item vertical height drag is a refinement |
| Per-item transfer progress (bytes/percent/rate/ETA) | **Done + tested** | manager tracks received/total/rate per item; `clip_progress` control command; window shows a live progressbar per card |
| Animated GIF preview | **Done + tested** | Pillow decodes bounded frame sets, preserves aspect ratio, clamps unsafe frame delays and returns PPM frames; the history window animates available GIF items and stops scheduled animation work when closed. Pillow is optional at runtime but installed by the official installer |
| Win+V interception + paste hotkey | **Done (needs hardware verify)** | when clipboard is enabled the runtime registers **Ctrl+Alt+V** to open the FlowShift clipboard window; with `intercept_win_v` on it also registers **Win+V** (RegisterHotKey MOD_WIN+V), suppressing the OS clipboard history and opening FlowShift instead. The window opens as a standalone process (`gui.py --clipboard`). Whether Windows lets `Win+V` be captured must be confirmed on hardware; Ctrl+Alt+V is reliable |

**In short:** **text, HTML, file/batch and image** clipboard paths work and are
tested, including animated GIF previews. The **history window** provides real
image thumbnails, a draggable preview/text splitter, live per-item progressbars
and opens via **Ctrl+Alt+V** (and optionally **Win+V**). Per-item vertical height
drag remains a UI refinement and is not claimed as implemented.

## Concepts

- **Own history, not the Windows history.** FlowShift maintains its own ordered
  list. The Windows clipboard is the source (on copy) and the target (on paste).
- **Per profile.** Each peer/profile identity has its own history directory, so
  `Laptop → Surface` and `Surface → Laptop` do not mix.
- **Content-addressed dedup.** Blobs are stored under their SHA-256, so identical
  content is stored once and already-known items are never re-transferred.
- **Manifest-based sync.** The source sends metadata only; the target requests
  just the items it is missing (in source order). Copying 3 new things when 200
  are already known transfers exactly 3 items.

## Limits (configurable, GUI)

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

- GUI **Clipboard** tab: enable/disable, all limits, units, direction mode,
  Win+V interception toggle, paste hotkey, ZIP strategy (legacy path only),
  thumbnail size.
- Delete one item / delete the whole history / clean temp are store operations
  (`ClipboardStore.delete_item`, `.clear`, `.cleanup_temp`).
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
