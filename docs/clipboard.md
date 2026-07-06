# FlowShift Clipboard

FlowShift keeps its **own** clipboard history per profile and syncs only the
items a peer is missing (manifest-based). This document describes what is
**implemented and tested today** versus what is still being built, so it never
promises more than the code delivers.

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
| **File / batch** capture + sync + paste | **Done + tested** | `clipboard_files.py` bundles files into a deterministic ZIP that rides the tested chunked-transfer path; content-identity hash gives cross-copy dedup; lazy bundle build on request; `CF_HDROP` read/set (`clipboard_win.py`); received bundles unpack to `temp/incoming` and are set as a file list; locally-captured items paste original paths without a copy. Integration-tested (two-manager file batch roundtrip) + runtime (worker_smoke Test F) |
| Windows CF image/HTML/CF_DIB | **Not yet** | text (`CF_UNICODETEXT`) and files (`CF_HDROP`) are wired; image/HTML are stubs in `clipboard_win.py` |
| Clipboard history WINDOW (list, draggable splitter, thumbnails, per-item progress) | **Done (basic)** | resizable `ClipboardWindow`: per-profile item cards, a draggable splitter (ttk.Panedwindow) between preview and text, thumbnail-size modes (klein/mittel/gross), search, per-item progressbar with live transfer telemetry (bytes/percent/rate/ETA via `clip_progress`), paste/retry/pin/delete/clear. Opens from the Clipboard tab. Per-item vertical height drag + real image/GIF thumbnails are refinements |
| Per-item transfer progress (bytes/percent/rate/ETA) | **Done + tested** | manager tracks received/total/rate per item; `clip_progress` control command; window shows a live progressbar per card; `test_clipboard_sync` verifies 100%/inactive after transfer |
| Animated GIF preview | **Not yet** | needs the image (CF_DIB) layer first |
| Win+V interception | **Not yet** | setting exists (`intercept_win_v`); the window opens from the GUI today, global Win+V is a later layer |

**In short:** the **text and file/batch vertical slices are complete and tested**,
and there is now a **working clipboard history window** with a draggable
preview/text splitter, thumbnail sizes, search and live per-item progressbars.
The remaining pieces (image/GIF content, per-item height drag, global Win+V) are
the next layers and are honestly listed as not-yet-done.

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

- Transfers are **chunked** (chunk size chosen so a base64 chunk + JSON envelope
  stays under `MAX_FRAME_SIZE = 28 MiB`). Each chunk can carry a SHA-256 for
  per-chunk verification; the whole item is verified against its SHA-256 on
  completion.
- **Retry / resume:** the `ChunkAssembler` reports missing indices and the next
  index to resume from; duplicate and hash-mismatched chunks are detected so the
  receiver can request a retry.
- **ZIP strategy** (`clipboard_model.zip_strategy`): single file → direct; many
  already-compressed files (jpg/mp3/…) → multi-file (no zip); many compressible
  files that fit RAM → RAM-streamed zip; too big for RAM but disk ok → temp zip
  on disk; not enough disk → multi-file. Temp zips are not kept permanently.
- **Disk-space guard** (`has_enough_space`): the receiver checks free space
  before a large transfer; if there is not enough, the item shows a clear
  `Nicht genug Speicherplatz` error instead of a half transfer. (The guard logic
  is implemented + tested; enforcement is wired in the transfer layer.)

## Where the data lives

```
%ProgramData%\FlowShift\clipboard\
  profiles\<profile_id>\
    index.json          # ordered history + revision
    objects\<sha256>    # content-addressed blobs (dedup)
    previews\<item>.png # thumbnails (later layer)
    temp\               # per-profile scratch
  temp\incoming\        # receiver scratch for in-flight transfers
  temp\outgoing\        # sender scratch (e.g. temp zips)
```

## Managing the history

- GUI **Clipboard** tab: enable/disable, all limits, units, direction mode,
  Win+V interception toggle, paste hotkey, ZIP strategy, thumbnail size.
- Delete one item / delete the whole history / clean temp are store operations
  (`ClipboardStore.delete_item`, `.clear`, `.cleanup_temp`).
- Uninstaller asks whether to delete the clipboard history and always cleans
  transient temp files.

## Settings reference (`config.json` → `clipboard`)

See `src/python/config.example.json` for a full example. All keys are validated
and clamped by `clipboard_model.clipboard_settings`, so a bad value can never
destabilise the runtime.
