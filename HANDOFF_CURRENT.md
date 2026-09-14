# FlowShift - Current State

## Release state

- Current version: `0.6.0-dev.17`.
- Current stable release: `v0.5.4`.
- Active implementation phase: Phase 3 - Clipboard Transfer Hardening.
- Active phase specification: `docs/phases/phase_3_clipboard_transfer_hardening.md`.
- Phase 3 toolchain and dependency modernization is complete.
- The productive legacy clipboard path and binding V2 target architecture are
  documented in `docs/clipboard_transfer_v2.md`.
- Capability negotiation, metadata-first schema-2 capture, canonical batch
  manifests, centralized Windows path validation, and the persistent
  thread-safe transfer-session foundation are implemented.
- Strict typed V2 JSON-control and raw binary-payload framing is implemented,
  including incremental parsing, absolute read deadlines, protocol limits,
  SHA-256 chunk verification, and serialized per-socket writes. Productive
  channel negotiation remains deliberately disabled until transport integration.
- Bounded V2 flow control is implemented as a transport-neutral foundation:
  global and per-peer admission, count/byte-bounded send and receive queues,
  per-transfer in-flight windows, finite ACK deadlines, strict cumulative ACKs,
  batching by bytes/chunks/time/file completion, and slow-receiver loopback
  coverage, including separate verified and durable offsets plus restored sender
  windows. Productive channel integration remains deliberately open.
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
- The immutable `v0.5.3` tag remains unchanged; its release workflow failed.

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

## Verified baseline

- `python -m unittest test_toolchain_policy test_clipboard_gif test_overlay_foundation test_overlay_lifecycle`
- `packaging/test_release_packaging.ps1` including staged productive imports
- `test_update_flowshift.ps1`: 7 passed
- all PowerShell sources parsed successfully
- WebGUI: 9 tests passed, Vite 8.2.0 production build passed
- `npm audit --include=dev --audit-level=high`: 0 vulnerabilities
- `python -m pip_audit -r requirements.txt`: no known vulnerabilities
- Hash-locked runtime and audit requirement dry-runs passed
- Transfer architecture adjacency: 105 file, streaming, and semantics tests
  passed (`test_clipboard_files`, `test_clipboard_streaming`, and
  `test_clipboard_semantics`).
- Slice 3 focused suites passed: V2 foundation, clipboard semantics/events,
  files, transfer, sync, streaming, productive service/tray checks, Python
  compilation, diff checks, and release staging/import packaging.
- Slice 4 focused suites passed: 20 typed-framing tests, 160 adjacent V2,
  streaming, and semantics tests, productive service/tray checks, Python
  compilation, diff checks, and release staging/import packaging.
- Slice 5 focused suites passed: 28 flow-control/ACK tests including typed
  loopback and 1,000 ACK cycles, 208 adjacent V2/framing/streaming/semantics
  tests, 139 legacy transfer checks, 210 productive service checks, Python
  compilation, diff checks, and release staging/import packaging.
- Slice 6 focused implementation suites passed: 74 direct-stream, receiver
  staging, manifest, source-snapshot, flow-control, and typed-channel tests.
- Slice 6 affected-subsystem verification passed: 237 adjacent V2, framing,
  streaming, and semantics tests; legacy transfer/sync/clipboard checks; Python
  compilation, diff checks, and release staging/import packaging.
- Slice 7 focused resume, streaming, and flow-control suites passed: 81 tests.
- Slice 7 affected-subsystem verification passed: 275 resume, V2, staging,
  framing, streaming, and semantics tests plus Python compilation and diff checks.
- Slice 8 affected-subsystem verification passed: 403 tests (one skipped for
  unavailable Windows symlink privilege), plus 13 import-triggered tests; legacy
  transfer/sync scripts, Python compilation, diff checks, and staged release
  packaging/import checks passed. Windows deny-WRITE handoff and restored-mtime
  tampering regressions are covered. POSIX execution and real power-loss testing
  remain unverified; hardware/VM checks remain open.
- Slice 9 verification passed: 18 materialization tests; 291 adjacent object
  store, safety, streaming V2, semantics, events, files, and legacy streaming
  tests (one skipped symlink-privilege test); legacy transfer/sync scripts;
  release packaging/import contract including `clipboard_materialize_v2`.
- Slice 10a/10b verification passed: 341 tests across cache V2, semantics,
  object store, safety, materialization, events, files, streaming V2, and
  resume V2 (one skipped symlink-privilege test); `test_service.py`, legacy
  transfer/sync/streaming scripts; Python compilation.
- Slice 11a verification passed: 400 tests across update gate V2, streaming
  V2, resume V2, semantics, cache V2, preflight V2, events, materialization,
  object store, updater, and WebGUI update API (one skipped); `test_service.py`;
  legacy transfer/sync/streaming scripts.
- Slice 11b verification passed: 404 tests across transfer control V2,
  streaming V2, resume V2, semantics, cache V2, update gate V2,
  materialization, events, object store, safety, preflight V2, and WebGUI
  update API (one skipped); `test_service.py`; legacy transfer/sync/streaming
  scripts; release packaging contract including `clipboard_transfer_control_v2`.
- Slice 12 verification passed: 462 tests across transport V2 (23, paired real
  managers + socketpair channel), streaming/resume/transfer control/update
  gate/semantics/cache/events/preflight/materialization/object store/flow
  control/framing V2 and WebGUI update API (one skipped); `test_service.py`
  incl. tray channel hand-off; legacy transfer/sync/streaming scripts; release
  packaging contract including `clipboard_transport_v2`.
- Slice 13 verification passed: 517 tests across transport V2 (28 incl.
  sender/both restart and source-changed purge), stress V2 (10: 10k parser
  frames, 100 disconnect/resume cycles, 200-file batch, cancel storm, parallel
  status polling, slow receiver window bound, disk-full resume, 10 restarts
  without thread leak, 1k malformed-frame burst, >4 GiB offsets), tray
  localhost E2E (3: both directions over real TCP through `peer_handler` /
  `_clip_send` / `_clip_open_channel`, mid-transfer link drop resume),
  streaming/resume/transfer control/update gate/semantics/cache/events/
  preflight/materialization/object store/safety/foundation/flow control/
  framing V2 and WebGUI update API (one skipped); `test_service.py`; legacy
  transfer/sync/streaming scripts; release packaging contract.

## Last pushed commits

- `895d473` - Phase 3 dev.15: activate productive V2 clipboard transport.
- `f2714f0` - Phase 3 dev.14: add V2 cancellation, timeouts, progress, and lease-only materialization.
- `f6bf0d1` - Phase 3 dev.13: wire V2 receive preflight and update idle gate.
- `c206bb8` - Phase 3 dev.12: integrate V2 cache eviction, cheap availability, and lease retirement.
- `56b4af5` - Phase 3 dev.11: materialize V2 items by hardlink or verified copy.
- `4eeee30` - Phase 3 dev.10: gate and publish verified file objects.
- `110a854` - Phase 3 dev.9: persist transfer resume state.
- `e6a9438` - Phase 3 dev.8: add direct file staging.
- `f8d0355` - Phase 3 dev.7: add bounded transfer flow control.
- `828e7e0` - Phase 3 dev.6: add typed clipboard framing.
- `32530ce` - Phase 3 dev.5: establish transfer v2 foundation.

## Open work

- Remaining Phase 3 slice: documentation closure (`docs/clipboard_transfer_v2.md`
  restart resume, `MANUAL_TEST_CHECKLIST.md` section-31 matrix, phase spec
  acceptance evidence), full regression with the CI commands, and release
  `v0.6.0`.
- Keep the existing manual hardware and VM checks open in `TODO_CURRENT.md`.

## Next planned phase

- Phase 4 is not started and must not begin automatically.
