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
| Runtime clipboard watcher + live text sync | **Not yet** | the Windows clipboard watcher thread + manifest exchange + text transfer are the next layer (wiring the foundation into `tray.py`) |
| File / batch chunked transfer wiring | **Not yet** | protocol + chunking + assembler exist and are tested; the runtime send/receive threads that move real bytes are the next layer |
| Windows CF integration (text/HTML/image/CF_HDROP) | **Not yet** | reading/setting the Windows clipboard (incl. multi-file `CF_HDROP`) is the next layer |
| Clipboard window UI (list, drag splitter, item resize, thumbnails) | **Not yet** | the settings tab exists; the rich history window is a later layer |
| Animated GIF preview | **Not yet** | planned, not stubbed away |
| Win+V interception | **Not yet** | setting exists (`intercept_win_v`), hook wiring is a later layer |

**In short:** this pass delivers the **foundation** (model + store + protocol +
tests + settings + installer dirs) that the remaining layers build on, so the
first real transfer will not break on missing plumbing. The interactive parts
(watcher, CF integration, transfer threads, history window, GIF, Win+V) are the
next layers and are explicitly listed as not-yet-done.

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
