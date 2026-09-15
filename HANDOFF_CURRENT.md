# FlowShift - Current State

## Release state

- Current version: `0.7.0-dev.10`.
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
    360x360 centered on the cursor, clipboard 420x520 next to it, clamped),
    window is activated on show, focus watcher hides on click outside
    (`overlay_hidden(reason=focus_lost)`), Escape stays.
  - React overlay: `OverlayShell` routes `command_wheel` -> `CommandWheel`
    (SVG sectors <= 8, cyclic mouse-wheel paging, dots, spotlight hover),
    `clipboard` -> `ClipboardOverlay` (fixed-height list, async polling + SSE,
    same list node and scroll offset across refreshes, Set/Get/Pin/Delete,
    legacy + stream V2 progress via `clipboardFormat.js`), diagnostic card only
    with `data.diagnostic === true`.
  - `tray.py`: Ctrl+Alt+V, Ctrl+Win+V and (opt-in) Win+V open the clipboard
    overlay; global Ctrl+RightClick (always-on mouse-only hook
    `WheelTriggerHook`, swallows only the Ctrl right click, inactive while
    forwarding), Ctrl+RightClick on the tray icon or the configurable
    wheel hotkey (`ID_HK_WHEEL`) opens the wheel; `execute_action` hides the
    overlay, restores the remembered foreground window and enqueues key events
    into `inject_queue`; runtime actions open the clipboard overlay or send the
    profile manifest. The Tkinter clipboard window is no longer opened by hotkeys.
  - WebGUI Settings includes `CommandWheelSection`: it loads the validated
    registry/wheel, edits ordered pages and slots within server limits, captures
    the wheel hotkey with shared hotkey-format helpers, and saves through
    `POST /api/actions/wheel` so the runtime immediately re-registers it.
  - WebGUI `ClipboardView` keeps its list DOM node and scroll offset across
    item/progress refreshes, rejects stale overlapping profile responses, and
    renders legacy plus stream V2 progress through `clipboardFormat.js`; detail
    and list rows share the same polling snapshot.
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

- Release CI uses CPython 3.14.6 and Node.js 24.18.1 LTS; dependency CI also
  tests Node.js 26.5.1 Current.
- GitHub Actions are immutable SHA pins and Dependabot covers Actions, npm, and
  Python dependencies.
- Runtime Python dependencies and audit tooling are fully hash-locked; direct
  npm dependencies and npm 12.0.2 are exact and package-lock v3 is committed.
- The installer accepts supported 64-bit CPython 3.10-3.14, installs 3.14 only
  when missing, preserves user ownership, and requires hashed dependencies.
- The packaged end-user path remains Node-free.
- The curated release payload now includes `web_api.py` and its contract test
  imports the productive staged modules.

## Productive path

- `src/python/tray.py --tray` is the productive Windows runtime.
- `src/python/gui.py` is the tkinter settings and legacy clipboard GUI.
- `webgui/` is the React/Vite settings UI and diagnostic overlay shell.
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

- `fda49a6` - Release v0.6.3: Phase 3 clipboard transfer hardening (tag `v0.6.3`; workflow success, release published).
- `e7bb274` - Release v0.6.2: Phase 3 clipboard transfer hardening (tag `v0.6.2`; smoke-test timeout on CI, workflow failed).
- `9678b2b` - Release v0.6.1: Phase 3 clipboard transfer hardening (tag `v0.6.1`; VERSION not updated, workflow failed).
- `8995b8c` - Phase 3 v0.6.1-dev.1: make path containment and status-polling tests CI-stable (VERSION not updated).
- `a7dcadc` - Release v0.6.0: Phase 3 clipboard transfer hardening (tag `v0.6.0`; workflow failed, see release state).
- `6005228` - Phase 3 dev.17: close V2 documentation, release raced receiver stages, and patch WebGUI audit findings.

## Last successful focused tests (Phase 4)

- Python: test_overlay_actions, test_web_api_actions, test_web_api_updates, test_overlay_modes, test_tray_overlay_actions, test_tray_stream_v2_e2e (unittest); test_overlay_lifecycle.py, test_overlay_foundation.py, test_service.py, overlay_show_hide_stress_test.py, overlay_ipc_stress_test.py: all OK.
- WebGUI: npm test (29 tests) and npm run build: OK.

## Open work

- Phase 4 slice 6c and the release as listed in `TODO_CURRENT.md` (Tkinter
  clipboard retirement and docs, regression and release `v0.7.0`).
- Manual hardware and VM checks remain open in `TODO_CURRENT.md`, including
  the Phase 3 two-device matrix in `MANUAL_TEST_CHECKLIST.md`.

## Next planned phase

- None defined after Phase 4.
