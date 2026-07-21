# Clipboard Semantics Contract

This document is the binding implementation contract for FlowShift Phase 2.
It refactors clipboard state and metadata semantics without replacing the tested
payload transfer engine or building the Phase 3 and later user interfaces.

## Scope and non-goals

Phase 2 must add:

- versioned persistent and wire metadata;
- an explicit per-profile current item;
- event-driven Windows capture with a polling fallback;
- sequence-bound suppression of FlowShift's own Windows clipboard writes;
- a bounded capture pipeline;
- metadata-only live announcements and acknowledgements;
- immutable origin and explicit provider state;
- a persistent received-payload cache with protected eviction;
- materialization ownership and lifetime tracking;
- transfer preflight and truthful status/API diagnostics.

Phase 2 must not replace the chunk transfer engine, implement persistent transfer
resume, redesign transfer batches, build the full React Clipboard UI, implement
Command Wheel behavior, or add shell integration. Those belong to later phases.

## Compatibility invariants

The following shipped behavior must remain valid:

- Text, CF_HTML with plaintext fallback, CF_DIB images, GIF previews, files, and
  file batches keep their current capture, history, transfer, and paste paths.
- Content-based dedup, ordered history, pin/delete/clear, limits, manual download,
  progress, manifest reconciliation, and Win+V/Ctrl+Alt+V entry points remain.
- Existing unversioned `index.json` stores migrate without losing valid items or
  blobs. Migration is idempotent and uses backup plus atomic replacement.
- Unknown item fields survive load/save so newer data is not silently destroyed.
- Existing message names and activation manifests remain a compatibility
  fallback. A peer that does not support Phase 2 metadata messages can continue
  using the `v0.4.0` manifest and transfer flow.
- Existing API response fields remain additive-compatible. Local-only source
  paths must not be added to peer metadata.
- `sha256` retains its current content-identity meaning. For normal blobs it is
  the byte hash; for file batches it is the deterministic logical hash of paths
  and file contents. A serialized ZIP has a separate payload hash.
- File logical size and serialized transfer size remain separate values.
- Pinned items survive history eviction. Cache eviction has additional protection
  rules defined below.

## Versioned data model

Phase 2 uses integer schema version `1` for the first explicit clipboard store,
item descriptor, and metadata message schemas. Versions are independent of the
application and peer transport protocol versions.

### Item descriptor

Every persisted or announced item has these semantic groups:

```json
{
  "schema_version": 1,
  "item_id": "copy-event-id",
  "sha256": "legacy-content-identity",
  "kind": "text",
  "mime": "text/plain",
  "size": 5,
  "created_at": 0.0,
  "seq": 1,
  "display_name": "hello",
  "preview_text": "hello",
  "preview_hash": "",
  "file_count": 0,
  "total_file_size": 0,
  "pinned": false,
  "origin": {
    "device_id": "device-id",
    "event_id": "copy-event-id",
    "captured_at": 0.0
  },
  "payload": {
    "content_sha256": "legacy-content-identity",
    "encoding": "raw",
    "sha256": "serialized-byte-hash-or-null",
    "size": 5
  },
  "providers": [],
  "payload_state": "metadata_only",
  "metadata": {}
}
```

Rules:

- `item_id` identifies one history/copy event. It is not a content hash.
- `origin` is assigned once at capture. It is retained through announcements,
  manifests, downloads, restart, and forwarding. Receiving or caching an item
  never changes its origin.
- `origin.event_id` is stable across profiles created from one local Windows
  clipboard event even when profile-specific `item_id` values differ.
- `payload.content_sha256` equals the legacy `sha256` and preserves dedup.
- `payload.encoding` is `raw` for byte-identical text/HTML/image data and
  `deterministic_zip` for the current file/batch transport representation.
- `payload.sha256` and `payload.size` describe serialized transfer bytes. They
  may be null until a lazy deterministic file bundle is verified.
- Local `files[].abspath`, source roots, materialization paths, cache paths, and
  other absolute paths are store-private. They are never announced or included
  in peer manifests.
- `available` remains as a derived compatibility field. New code must use
  provider and payload state internally rather than treating it as authoritative.
- Supported payload states are `metadata_only`, `source_available`, `cached`,
  `materialized`, `receiving`, `missing`, and `failed`.
- Parsers reject unsupported future schema versions safely. They preserve unknown
  fields in supported versions and validate bounded strings, counts, sizes, IDs,
  hashes, kinds, and nested object types.

### Store index

The per-profile `index.json` schema is:

```json
{
  "schema_version": 1,
  "revision": 0,
  "current_item_id": null,
  "items": [],
  "received_cache": {}
}
```

An unversioned index is legacy schema `0`. Migration to schema `1`:

1. Reads and validates the complete legacy document before mutation.
2. Creates one clearly named pre-migration backup beside `index.json`.
3. Adds schema/item defaults without removing unknown fields.
4. Sets `current_item_id` to the newest valid item ID when history is non-empty.
5. Reconciles `available` against actual objects or revalidated local sources.
6. Writes and flushes a temporary file, then atomically replaces `index.json`.
7. Leaves the original and backup intact if any migration/write step fails.

Malformed indexes are preserved as corrupt backups and recover to an empty valid
schema rather than being silently overwritten. A future store schema is opened
read-only/fail-closed and is never rewritten by an older runtime.

## Current item

`current_item_id` is the latest clipboard state known for that profile. It is not
the selected history row and is not inferred from list order after migration.

- Successful local capture sets the captured item current.
- Successful explicit paste/set-to-Windows sets that item current.
- A valid remote current-item announcement updates the remote profile's pointer
  after its metadata is stored, even when the payload is not local yet.
- An announcement does not automatically overwrite the local Windows clipboard.
- Failed captures or failed Windows writes do not change current state.
- Deleting, clearing, or evicting the current history item clears the pointer.
  It does not silently select an older item.
- The current pointer is persisted in the same atomic transaction as its related
  history/revision mutation.
- Current metadata can be present while its payload is missing. Status reports
  those facts separately.

## Windows listener and bounded capture

The productive Windows path uses a dedicated message thread with
`AddClipboardFormatListener` and `WM_CLIPBOARDUPDATE`.

- WindowProc only records/coalesces the clipboard sequence and signals a bounded
  queue. It performs no clipboard parsing, hashing, disk access, or network I/O.
- Listener setup failure or unexpected listener exit selects a clearly logged
  `sequence_poll` fallback using `GetClipboardSequenceNumber`.
- Listener shutdown removes the format listener, destroys its window, posts the
  thread quit signal, and joins within a bounded timeout.
- Status exposes mode, running state, fallback reason, last observed sequence,
  last event time, queue depth/drops, and last capture error.
- A bounded capture worker reads one stable sequence at a time and coalesces
  duplicate/newer notifications. Shutdown and update maintenance reject new work.
- Capture keeps the existing precedence: files, image, HTML, then text.
- Clipboard reads are bounded before copying data where Windows exposes a global
  size. File enumeration, path count, recursive entries, total bytes, and hashing
  are bounded and cancellation-aware.
- `max_item_gb` is a hard admission cap for capture, request, preflight, and
  receive. Exceeding it records a controlled reason and never allocates or reads
  the complete rejected payload.
- Capture limits and queue pressure are status-visible; dropping/coalescing work
  is never reported as a successful capture.

## Write suppression

Suppression prevents FlowShift from recapturing its own successful Windows
clipboard writes without suppressing a later genuine user copy of equal content.

- Every write path, including local control and HTTP paste, uses one manager API.
- A write records the sequence before the attempt. A token is committed only
  after all required formats were written successfully.
- The token contains the resulting sequence, normalized format set, content
  identity, creation time, and one-shot state.
- Capture consumes a token only when sequence, formats, and identity match within
  a short bounded lifetime. Consumption removes it.
- A failed/partial write creates no committed token. A later different sequence
  or an expired token cannot suppress a user event.
- HTML fallback text is one atomic multi-format write for suppression purposes.
- File paths are normalized consistently before identity comparison.
- Status exposes only token counts/ages and outcomes, never clipboard contents.

## Metadata announcements and reconciliation

Phase 2 adds versioned `clipboard_announcement` and
`clipboard_announcement_ack` messages. Announcements are live hints; manifests
remain the authoritative reconnect/reconciliation fallback.

An announcement contains only:

- `schema_version`, bounded `announcement_id`, `profile_id`, and sender
  `device_id`;
- sender history revision and current item ID;
- one sanitized item descriptor;
- provider metadata needed to locate a peer, never payload bytes or local paths.

An acknowledgement correlates the announcement ID and reports `accepted`,
`duplicate`, or `rejected` plus a bounded reason code. Replayed announcements are
idempotent. Per-peer/profile revision tracking rejects stale current-pointer
updates while allowing missing item metadata to be reconciled safely.

Announcements never contain Base64 payloads and never start an unsolicited
payload transfer. If policy wants the payload, the receiver explicitly requests
it and completes preflight first. Disconnect/reconnect correctness does not
depend on an acknowledgement: the next manifest reconciles state.

Reconciliation distinguishes:

- `metadata_missing`: the item descriptor/history event is absent locally;
- `payload_missing`: metadata exists but no usable local provider/cache exists;
- `payload_present`: a verified local provider/cache can supply the complete
  payload.

## Origin, providers, and availability

Origin answers where the copy event began. Providers answer where the complete,
verified payload can currently be obtained. They are separate concepts.

A provider record contains a stable device identity, state, last-seen timestamp,
and verified serialized payload hash/size when known. Provider states are
`available`, `stale`, and `unavailable`.

- A device is `available` only if it can supply the complete verified payload.
  Metadata possession alone does not make it a provider.
- A local source provider is revalidated before announcement/preflight/use.
  Changed or missing source files make that provider stale.
- A completed verified received-cache entry makes this device a provider.
- Partial transfers, placeholders, previews, and materialized subsets do not.
- Provider observations are merged idempotently and age to stale on disconnect;
  immutable origin is never rewritten to the latest provider.
- Compatibility `available` is true when at least one usable local source or
  cache exists, and false for metadata-only/missing payloads.

## Persistent received-payload cache

`clipboard.cache_received_payloads` defaults to `true`. A verified received
payload is retained in the content-addressed object store and indexed by payload
identity, size, receive/access times, and providers.

- Cache accounting uses unique on-disk payload bytes, not summed history sizes.
- Cache entries are reused across duplicate history items in the profile.
- Reads/materialization update last access using bounded/coalesced persistence.
- Eviction is deterministic least-recently-used among eligible entries and may
  run after receive, history mutation, maintenance, or startup reconciliation.
- Pinned items, the current item, in-flight transfers, active materialization
  leases, and objects referenced by an executing paste/write are protected.
- Evicting cached bytes keeps item metadata/history and changes payload state to
  missing. It never changes origin or claims successful history deletion.
- Deleting history removes unreferenced cache metadata/bytes only when no
  protection or other item reference remains.
- With caching disabled, received bytes live only as long as needed for the
  active paste/materialization contract; metadata and provider observations may
  still persist.

## File materialization lifetime

Materialized files are owned by leases rather than age alone.

- Materialization creates a lease for profile, item, destination, creation time,
  access time, and owner sequence when placed on the Windows clipboard.
- A lease is active while its Windows clipboard sequence remains current or an
  executing paste/write holds it. Clipboard change releases the ownership lease.
- Cleanup never removes active leases. Released/stale leases are removed after
  the configured cleanup age; startup repairs stale markers after crashes.
- Re-materialization clears/replaces its managed destination atomically so files
  from an older bundle cannot survive.
- Delete/clear releases related inactive materializations without invalidating a
  currently owned Windows clipboard payload.
- Extraction enforces path safety, member-count, per-member, total expanded-size,
  and configured hard-item limits independent of advertised metadata.
- The existing `materialize_files` wrapper remains compatible while the richer
  result includes lease and payload-state details.

## Transfer preflight

New-protocol transfers require a receiver decision before `transfer_start`.
Legacy peers may use the existing direct request/start flow as a compatibility
fallback.

Preflight correlates request, item, profile, provider, content identity, payload
hash, logical size, serialized size, encoding, and intended cache/materialization
policy. The receiver responds with `accepted` or a bounded rejection reason such
as `metadata_missing`, `payload_missing`, `too_large`, `disk_full`, `policy`,
`busy`, or `shutting_down`.

The receiver validates:

- item and provider still match known metadata;
- hard item and automatic/manual policy limits;
- expected receive plus cache/materialization disk requirement and safety margin;
- bounded queue capacity, maintenance state, and duplicate/in-flight work;
- supported encoding and verified payload identity information.

Acceptance reserves bounded receive admission but not unlimited disk. Transfer
start must match the accepted offer exactly; otherwise it is rejected. Manual
requests use the same preflight and hard limits as automatic requests.

## Status and API foundation

Status/control/HTTP additions are additive and contain no clipboard payload or
absolute local source/cache/materialization paths. They expose:

- current item ID and separate metadata/payload state;
- listener mode/health/fallback/sequence and capture queue pressure;
- suppression counts and last outcome;
- origin device and provider states;
- unique cache bytes, entry count, protected count, and eviction totals;
- active/released materialization counts;
- queued/active inbound and outbound transfers keyed by profile plus item;
- last preflight rejection reason and announcement/ack/reconciliation counters.

Diagnostics remain useful when the runtime is offline and must not disclose
clipboard preview text, filenames, payload bytes, or local absolute paths.

## Required verification

Phase 2 is not complete until automated coverage includes:

- legacy migration, backups, atomic failure, corrupt/future schema handling, and
  restart persistence;
- current-item update/delete/evict/restart and metadata-without-payload behavior;
- one-shot sequence suppression, failed writes, HTML multi-format writes, HTTP
  paste, and a later user copy of identical content;
- listener startup/event/shutdown, forced fallback, event coalescing, queue
  pressure, lock contention, and shutdown/maintenance admission;
- bounded text/HTML/image/file capture and hard item caps;
- announcement validation, no-payload/no-path guarantees, ACK correlation,
  duplicate/stale/reconnect behavior, and manifest reconciliation;
- immutable origin, multiple providers, provider loss/staleness, and verified
  provider promotion after receive;
- cache restart, unique-byte accounting, LRU eviction, all protection classes,
  and caching-disabled behavior;
- materialization lease ownership/release/crash cleanup and extraction limits;
- accepted/rejected/mismatched preflight, manual parity, disk pressure, and queue
  admission;
- truthful status/API diagnostics and payload/path privacy;
- all existing clipboard and full-project regressions.

Windows manual verification must cover Explorer, Office/browser HTML, screenshots,
images, repeated same-content copies, rapid copy bursts, listener fallback, and
file materialization across clipboard ownership changes. Existing clean-VM and
overlay hardware tasks remain open until separately completed.
