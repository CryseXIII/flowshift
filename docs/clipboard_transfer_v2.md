# Clipboard Transfer V2 Architecture

## Status and Scope

This document is the Phase 3 design and implementation contract for
`clipboard_stream_v2`. It distinguishes the productive legacy implementation
from the V2 target. A section marked **planned** is not shipped until its code
and tests exist.

The productive runtime remains `src/python/tray.py --tray`. V2 extends that
runtime; it does not replace it with the experimental Rust code and does not
build the Phase 4 clipboard UI.

## Productive Legacy Path

The current path is:

```text
WM_CLIPBOARDUPDATE
  -> tray._capture_clipboard_sequence
  -> ClipboardManager.capture_*
  -> ClipboardStore index/object update
  -> metadata announcement or activation manifest
  -> request to the selected profile peer
  -> deterministic ZIP for every file or batch
  -> preflight
  -> Base64 chunks in JSON frames
  -> RAM or disk assembler
  -> whole-payload verification
  -> content object
  -> ZIP extraction into a materialization directory
  -> CF_HDROP
```

Relevant productive modules are `clipboard_files.py`, `clipboard_sources.py`,
`clipboard_protocol.py`, `clipboard_runtime.py`, `clipboard_transfer.py`,
`clipboard_store.py`, `clipboard_model.py`, `clipboard_win.py`, `tray.py`, and
`runtime_model.py`.

### Current full-payload work

File capture recursively scans and hashes every source file in
`clipboard_files.scan_paths`. File transfer then reads the source again while
building a deterministic ZIP. A disk-backed ZIP is read once for its payload
hash and again for network transmission. The receiver writes the ZIP, reads it
again for final verification, retains it as an object, and writes the logical
payload again when extracting it for `CF_HDROP`.

`capture_files_all` repeats that scan and hash for each target profile. Store
startup also rehashes every persisted local source before marking it available,
and manifest construction repeats source revalidation.

Small payloads are assembled completely in RAM. `ChunkAssembler` retains every
chunk, joins them into another complete byte string, and the runtime hashes the
result again before storing it.

### Current transport and state

All peer traffic uses a four-byte big-endian length followed by UTF-8 JSON.
Clipboard chunks are Base64 fields in those JSON messages. The hard JSON frame
limit is 28 MiB. TCP `sendall` is the only effective payload backpressure.
`clipboard_transfer_ack` exists as a message name but is not part of the
productive lifecycle.

Transfer jobs and task closures, inbound assemblers, preflight waiters and
unsolicited result maps, preflight approvals, progress, retries, remote
metadata/current/revisions, and provider connectivity are held in memory.
Several of these registries have no terminal-state eviction or inbound-session
bound. There is no journal.
Orderly shutdown removes active partial files; crash remnants have no durable
association with a transfer and are eventually age-cleaned.

The current chunk-index resume request is not a restart mechanism. Its
productive handler creates a new transfer ID while the receiver still owns an
assembler under the original ID.

### Existing protections retained by V2

- Clipboard notifications enter a bounded, coalescing queue.
- Self-writes are suppressed by Windows clipboard sequence and digest.
- Item, manifest, string, size, and provider metadata have hard bounds.
- Announcements and peer manifests exclude local source paths.
- Store indexes use flushed temporary files and atomic replacement.
- Preflight prevents intentional payload transmission before acceptance.
- Legacy chunks and complete payloads have SHA-256 checks.
- Transfer submission is bounded and sender worker count is clamped.
- Current and pinned items, active jobs, and leases protect cache entries.
- Existing schema migration preserves unknown supported-version fields and
  opens future schemas read-only.

### Legacy limitations V2 must remove

- Copying files performs full content reads before history display.
- Empty directories and source fingerprints are not represented.
- Reparse points are not handled as an explicit security decision.
- Single files and batches both use ZIP and Base64.
- Source changes between capture and transfer are not reliably detected.
- Provider metadata does not drive provider routing or failover.
- Productive connect/disconnect does not update clipboard provider/session
  lifecycle.
- Send failures can be swallowed by the tray send adapter.
- No receiver-confirmed completion, ACK window, or bounded in-flight bytes
  exists.
- Inbound assemblers and several transfer registries have no lifecycle bound.
- ZIP extraction lacks complete Windows path, collision, and expansion checks.
- Cache-disabled receives still persist complete objects.
- Cache eviction commonly removes accounting metadata without freeing objects
  or updating provider availability.
- Normal clipboard API responses can expose store-private absolute paths.
- Leases are persisted but not productively retired on later clipboard
  ownership changes.

## V2 Compatibility Contract

V2 is selected only when both live peer links advertise
`clipboard_stream_v2: true`. Otherwise the existing, size-limited and
preflight-protected strategy is used.

Productive peer transport is not cryptographically authenticated or encrypted.
The configured endpoint and self-asserted device ID are not proof of identity.
V2 does not claim confidentiality against a hostile LAN peer. A fresh random
channel nonce exchanged over the active control link binds each V2 data channel
and each resume attempt to that connection. The nonce is single-use, expires,
is never logged, and is not a substitute for future peer authentication.

Strategy names are stable status values:

- `stream_v2`
- `legacy_zip_v1`

Negotiation is per live peer connection. It is logged once and exposed through
status. V1 and V2 transfer messages are never mixed within one transfer.

Existing text, HTML, image, and legacy file objects remain readable. Existing
deterministic ZIP batches are pasted through the legacy lazy extraction path;
there is no mandatory bulk migration. A legacy item may be converted lazily
only when a user operation already requires reading it, and failure leaves the
legacy object valid.

`item_id` continues to identify a copy event and is never a content hash. A
successful first V2 hash updates the same history item and increments its
revision; it does not append another history row.

## Data Roots and Migration

**Planned:** New installations use:

```text
%ProgramData%\FlowShift\clipboard\
  profiles\<profile-key>\
  objects\sha256\<prefix>\<hash>
  manifests\<digest>.json
  temp\incoming\<transfer_id>\
  temp\outgoing\<transfer_id>\
  journals\incoming\<transfer_id>.json
  journals\outgoing\<transfer_id>.json
  materialized\<item_id>\<lease_id>\
```

Current packaged installations derive clipboard data from
`FLOWSHIFT_LOG_DIR`, which places it under
`%ProgramData%\FlowShift\logs\clipboard`. The V2 root resolver must detect and
continue using that legacy root when it contains user data. It must not copy or
delete all existing payloads during upgrade. New installations use the
canonical root through a separate data-root setting. The active root is
recorded in install state and diagnostics without exposing it in normal peer or
clipboard APIs.

One root resolver is shared by runtime, installer, updater, uninstaller, and
diagnostics. If both roots contain data it fails closed for manual recovery
rather than merging. Existing legacy profile directories remain mapped through
a persisted identity-to-directory table; new profiles use a collision-resistant
encoding of validated identity. Literal `.`, `..`, separators, reserved names,
and lossy sanitization are forbidden.

## Metadata-first Capture

**Planned:** Explorer file capture performs bounded enumeration and metadata
collection only. It does not calculate full file hashes and does not build a
ZIP.

Each capture records:

- stable `item_id` and item revision;
- local source reference kept only in local storage;
- relative path and entry type (`file` or `directory`);
- size, total size, file count, and directory count;
- `mtime_ns`;
- volume serial and file index when Windows exposes them;
- source availability;
- source fingerprint;
- hash state and optional final SHA-256.

Hash states are `unhashed`, `hashing`, `verified`, `changed`, and `invalid`.
Existing raw objects become `verified` only after their bytes are revalidated.
Legacy file and batch identities remain legacy or `unhashed`: their logical
item hash is not the ZIP hash and their sources have no captured fingerprint.

The source fingerprint is a versioned tuple of type, size, `mtime_ns`, volume
identity, and file identity. A fallback without file identity is explicitly
marked weaker. The sender snapshots fingerprints before opening streams and
checks them again after each file and after the complete transfer. Resume is
valid only when all relevant fingerprints still match.

Symlinks, junctions, mount points, and other reparse points are detected without
following them. V2 rejects the capture with a concrete error. No recursion may
escape the selected roots.

## Batch Manifest

**Planned:** File and directory payloads use a canonical schema-2 manifest.
Canonical JSON uses UTF-8, sorted keys, fixed separators, and no insignificant
whitespace. `manifest_digest` is SHA-256 of those canonical bytes with the
digest field omitted.

The manifest contains:

```json
{
  "schema_version": 2,
  "protocol_major": 2,
  "item_id": "copy-event-id",
  "item_revision": 1,
  "manifest_digest": "64-hex",
  "total_size": 0,
  "file_count": 0,
  "directory_count": 1,
  "entries": [
    {
      "index": 0,
      "path": "folder",
      "type": "directory",
      "size": 0,
      "mtime_ns": 0,
      "source_fingerprint": {},
      "hash_state": "unhashed",
      "sha256": null
    }
  ]
}
```

Entry indices are contiguous, zero-based, immutable for the manifest revision,
and sorted by canonical relative path. Directories are explicit, including
empty directories. File SHA-256 values may be absent before streaming and are
filled by a verified manifest revision after successful hashing.

The offered digest is the immutable provisional manifest digest. Stream
completion supplies the complete entry hash map. Both peers validate it against
streamed bytes, derive the same finalized manifest and digest, and atomically
advance the item revision exactly once. `receiver_complete` carries both
digests and the finalized revision. Replaying finalization is idempotent and
does not create another history item.

Central limits are applied before allocation or traversal:

| Limit | Initial V2 value |
|---|---:|
| Files | 100,000 |
| Directories | 100,000 |
| Total entries | 200,000 |
| Canonical manifest | 16 MiB |
| Relative path | 1,024 UTF-8 bytes |
| Path component | 255 UTF-8 bytes |
| Logical total/item | configured `max_item_gb`, hard 100 TiB |
| Single file | configured `max_item_gb`, hard 100 TiB |
| Binary payload frame | 4 MiB payload, 4 MiB plus header on wire |

Logical sizes and offsets are unsigned 64-bit values. Python integers outside
that range are rejected.

## Remote Path Validation

**Planned:** One validator is used by manifest parsing, staging, object-store
finalization, and materialization. It normalizes separators to `/` for the wire
but does not silently rename entries.

It rejects:

- empty paths or disallowed empty components;
- absolute, UNC, device, or drive-qualified paths;
- `.`, `..`, NUL, colon/alternate-data-stream syntax;
- Windows-invalid characters;
- components ending in dot or space;
- reserved device names, including names with extensions;
- component, path, entry-count, and manifest-size limit violations;
- duplicate canonical paths;
- Windows case-fold collisions;
- file/directory prefix collisions;
- symlink/reparse entries;
- any resolved target outside the staging root;
- any reparse point encountered below an existing staging/materialization root.

One invalid entry rejects the complete manifest. V2 never silently skips or
renames a remote entry.

## Session Model

**Planned:** A thread-safe `TransferSession` is the single source of runtime
transfer state. It is not reconstructed from unrelated dictionaries.

Required fields are transfer ID, direction, item ID/revision, profile, stable
peer identity, provider, strategy, manifest digest, logical/remaining bytes,
file count, timestamps, state, retry count, progress, resume state, preflight
state, and structured error.

States are:

```text
created -> preflight -> accepted -> sending_manifest -> transferring
        -> verifying -> finalizing -> completed
        -> paused -> waiting_reconnect -> transferring
        -> cancelled
        -> failed
```

Transitions are explicit and validated. Terminal states cannot return to active
states. Cancellation is accepted in every non-terminal state. Session count is
bounded globally and per stable peer.

## Typed Framing

**Planned:** The normal peer control/input link keeps its legacy JSON codec. It
negotiates V2 and exchanges a fresh random channel nonce. The initiator opens a
separate socket to the same peer port and sends one bounded legacy JSON channel
hello containing role `clipboard_stream_v2`, stable peer IDs, and that nonce.
The receiver consumes the nonce atomically and replies with a JSON channel ACK.
Only after that explicit barrier does this dedicated socket switch to typed
frames. No buffered post-ACK JSON bytes are allowed.

Codec mode and parser buffer are per socket. The writer switches under that
socket's lock. Input/control traffic never shares the binary data socket and
cannot be starved by clipboard payload frames. Failed channel setup leaves the
normal peer link in legacy mode and fails or pauses only the V2 session.

Every post-switch frame is:

```text
uint32_be body_length
uint8     frame_kind       1 = JSON control, 2 = clipboard payload
uint8     protocol_major   2
uint16_be flags
body
```

JSON-control bodies are bounded UTF-8 JSON objects. Binary payload bodies begin
with:

```text
16 bytes  transfer UUID
uint32_be entry_index
uint64_be offset
uint32_be payload_length
uint8     checksum_kind    0 = none, 1 = SHA-256
0/32 bytes checksum
raw payload bytes
```

Header and declared payload lengths must match exactly. Unknown frame kinds,
unknown major versions, unsupported flags, invalid UUIDs, out-of-range indices
or offsets, overflow, and oversized frames are protocol errors. They fail the
affected transfer and may close the peer link, but do not crash network or
runtime threads.

The parser supports fragmented reads and multiple frames in one socket read.
It allocates no body above the negotiated hard limit.

## Transfer Control Lifecycle

**Planned:** Control messages are schema-versioned JSON and include transfer ID,
item ID/revision, stable peer identity context, strategy, and manifest digest as
applicable.

The normal lifecycle is:

```text
offer/preflight
  -> preflight_accept
  -> manifest
  -> manifest_ack with receiver resume state and window
  -> binary payload frames
  -> cumulative ACKs
  -> sender_complete with final file hashes/fingerprints
  -> receiver verification and object finalization
  -> receiver_complete ACK
```

The sender marks a transfer completed only after `receiver_complete`. The send
adapter propagates no-link and socket errors to the session. No transfer worker
converts a failed send into success.

## Streaming, Flow Control, and ACKs

**Planned:** Default chunk payload is 2 MiB and configurable from 1 to 4 MiB.
The sender opens one source file at a time, seeks only to a validated resume
offset, reads one chunk, updates the file SHA-256, and submits the raw frame.
After process restart, sender and receiver re-read and hash the durable prefix
before accepting continuation because portable SHA-256 state is not journaled.
A known final hash may be reused only with an unchanged strong fingerprint.
Resume-prefix reads are reported separately in I/O metrics. The receiver writes
new data sequentially to `.part` and updates its hash.

Initial bounds are:

- 2 active transfers globally;
- 1 active transfer per peer;
- 4 in-flight chunks per transfer;
- 16 MiB in-flight bytes globally;
- bounded control and payload queues;
- no unbounded retained completed-job closures.

The receiver sends cumulative flow-control ACKs after 8 MiB, four chunks,
250 ms, or file completion, whichever occurs first. An ACK identifies transfer,
entry index, highest contiguous written offset, highest durable resume offset,
receiver state, and optional missing ranges. ACK thresholds and timeouts are
injectable in tests.

A flow-control ACK may release memory after buffered writes complete. A durable
resume offset advances only after the `.part` data is flushed and `fsync`ed,
then the journal is atomically committed. Checkpoints batch this cost by bytes
and time. Startup truncates or revalidates bytes beyond the durable offset.

The sender cannot release a window slot until ACK. A slow disk therefore closes
the receive window and propagates backpressure without buffering the remaining
file. Duplicate data below the acknowledged offset is ignored only after its
session and manifest are validated. Out-of-order data beyond the bounded window
is rejected.

## Staging and Atomic Finalization

**Planned:** Each incoming transfer owns one staging directory and one journal.
Files are written as `<entry-index>.part`; remote names are not used until the
validated materialization stage. Directories are represented in the manifest,
not created from untrusted paths during payload receipt.

Checkpoints batch journal writes by bytes and time. They flush and `fsync` the
partial before recording a durable offset but do not checkpoint every chunk. File
completion flushes and `fsync`s once, verifies size and SHA-256, revalidates the
sender fingerprint result, moves the verified file atomically into the object
store, then commits the journal.

Disk-full, short write, flush, hash, journal, and rename failures have distinct
error codes. A `.part` is never published as an object or provider payload.

## Persistent Journal and Resume

**Planned:** Incoming and outgoing journals use a versioned schema and atomic
temporary-write, flush, `fsync`, and replace. Each records:

- transfer/session identity and stable peer identity;
- item ID/revision and manifest digest;
- provider and strategy;
- source fingerprints;
- expected file sizes/hashes;
- completed files and highest durable contiguous offsets;
- verified and remaining bytes;
- retry count, timestamps, and state.

At startup, journals are parsed with strict size and field bounds. Corrupt,
stale, or orphaned supported-version journals do not crash startup. They are
quarantined for bounded cleanup, and no corresponding partial is considered
valid without revalidation. Future-version journals and their partials remain
untouched and read-only.

Disconnect transitions active sessions to `waiting_reconnect`, commits the
journal, closes source/partial handles, and frees all in-flight buffers.
Reconnect resumes only after stable peer identity, item revision, manifest
digest, and source fingerprints match. The receiver supplies durable offsets;
already verified complete files are not retransmitted.

On reconnect either peer sends a bounded `resume_inventory` on the control link.
The peer replies with matching session IDs and fresh one-time channel nonces.
For duplicate live sessions, the lower stable device ID coordinates and chooses
the journal with the greatest mutually valid durable progress. Both sides then
exchange manifest/fingerprint state and durable offsets before an idempotent
resume acceptance. Unknown sessions are rejected without item metadata. A
finite reconnect deadline owns transition to failed cleanup.

The same handshake applies after sender restart, receiver restart, or both.
Changed source, changed manifest, corrupt partial, impossible offset, or stale
journal causes explicit resume rejection and a safe restart or terminal failure
according to policy.

## Preflight V2

**Planned:** Preflight is keyed by transfer ID, strategy, item revision,
manifest digest, and expiry. It runs before source hashing or payload reads.

For V2 it includes remaining staging bytes, unverified object bytes, bounded
journal/manifest overhead, requested materialization bytes, and safety margin.
Durably verified resume bytes are subtracted exactly once. It does not reserve
space for a ZIP or extracted duplicate unless legacy strategy is selected.

Legacy preflight retains its worst-case ZIP accounting and size limits. A
successful estimate is not a disk reservation; later disk-full errors remain
explicitly handled.

## Object Store and Provider State

**Planned:** A process-wide object-store service stores every verified file by
its own SHA-256. A batch manifest maps names and directories to those objects.
Equal contents are physically stored once in the shared clipboard object root.
Items, manifests, sessions, and leases create explicit references. Publication
uses same-volume temporary files and atomic no-replace semantics under a
per-hash lock. An existing object is accepted only after size/hash verification.
Global garbage collection takes the same locks and deletes only unreferenced
objects.

An item becomes a local available provider only after every referenced object
and manifest is verified and readable. Journals and partials never create
provider availability. Object loss, corruption, or physical eviction updates
item payload state and provider state atomically.

Provider routing first chooses local verified data, then connected providers in
stable-ID order, preferring an existing resumable session. The provider must
advertise the exact item revision and manifest digest. On failure the next
matching provider receives a new one-time channel offer; incompatible providers
are not tried. Productive peer connect/disconnect calls clipboard lifecycle
hooks.

When received caching is disabled, synchronization remains metadata-only. On
explicit selection, verified data flows into a temporary lease-owned
materialization, is retained only through the bound clipboard lease, and is not
advertised as a durable provider. Failed selections expire through
journal-aware cleanup; preflight counts the complete remaining materialization.

## Materialization and Leases

**Planned:** A validated manifest is materialized into a new hidden lease
directory. On NTFS and the same volume, regular files use hardlinks from the
object store. Other volumes, unsupported filesystems, permission failures, and
link failures use a streaming copy fallback.

The complete tree is constructed under a temporary name and atomically renamed
before `CF_HDROP` receives exactly the selected top-level manifest roots, not
every leaf. Multiple roots, empty directories, Unicode names, and validated
root collisions preserve structure. Failed materialization is never exposed.

The lease is persisted as `pending_write` before the Windows clipboard write
and bound to the successful sequence afterward. Failed writes release it
immediately. Startup retires unbound leases unless current `CF_HDROP` ownership
and paths can be revalidated. Every newer clipboard sequence, including a later
FlowShift write, retires the previous sequence's leases. Cleanup never deletes a
bound active lease solely because of age.

## Cancellation, Timeouts, and Updates

**Planned:** Cancellation stops source reads, closes the send window, notifies
the peer, and releases buffers and workers. Retention is explicit:

| Cause | Journal and partial policy |
|---|---|
| User cancel | mark cancelled; delete after peer ACK or timeout |
| Disconnect | checkpoint and retain until reconnect deadline |
| ACK/no-progress timeout | checkpoint and retain for bounded retry |
| Source changed | mark stale, reject resume, delete receiver partials |
| Integrity failure | mark invalid; quarantine then bounded-delete |
| Disk/write/flush failure | retain only the last durable checkpoint for explicit retry |
| Final rename failure | retain verified stage and journal for bounded finalization retry |

Finite configurable timeouts cover preflight, manifest ACK, ACK window, no
progress, reconnect wait, and final completion ACK. There are no indefinite
waits.

The update idle gate blocks `preflight`, `accepted`, `sending_manifest`,
`transferring`, `verifying`, and `finalizing`. `paused` and
`waiting_reconnect` block while they own uncommitted state; after a clean durable
checkpoint and closed handles they permit update while preserving journals and
partials. Startup resumes them after update. Update rollback preserves schema-2
indexes, journals, partials, objects, leases, and provider state.

## Status, Privacy, and Logging

**Planned:** Status exposes transfer ID, item ID, strategy, state, a privacy-safe
relative current file name, file index/count, file and total bytes, percent,
EWMA rate, ETA, resume bytes, retry
count, provider, preflight state, and structured error code. Paused time is
excluded from rate and ETA.

Normal local clipboard APIs return an explicit public item projection. They do
not return source paths, store roots, materialization paths, journal paths, or
other private absolute paths. Peer metadata follows the same rule.

Logging is structured and rate-limited at lifecycle boundaries: negotiation,
strategy, manifest, preflight, journal, stream start, file complete, pause,
disconnect, resume, verification, source changed, disk full, finalization,
legacy fallback, and cleanup. It logs neither clipboard content nor private
absolute paths and never logs each ordinary chunk.

## Implementation Boundaries

The planned implementation is divided into complete slices:

1. capability, schema-2 item/session model, manifest, path validation;
2. typed framing and parser integration;
3. bounded flow control and cumulative ACKs;
4. staging and direct file streams;
5. persistent journals and disconnect/restart resume;
6. shared per-file object store and lease-based materialization;
7. provider, cache, preflight, API, connection, and update integration;
8. fault injection, resource instrumentation, and stress tests.

Each slice must preserve the legacy fallback and productive Windows runtime,
add concrete end-state tests, update the central version and operational state,
and be committed and pushed before the next slice.

## Acceptance Evidence

V2 is not complete until tests prove, through connected productive runtime
components:

- V2-to-V2 selects `stream_v2`; a legacy peer selects `legacy_zip_v1`;
- V2 file payload uses raw binary frames, no Base64 and no ZIP;
- capture does not read complete files;
- source reads and receiver writes are approximately one payload pass;
- bounded memory depends on chunk/window limits, not total file size;
- strict path, count, size, frame, integer, and collision limits reject hostile
  input safely;
- receiver-confirmed completion and provider publication occur only after
  verification and atomic finalization;
- disconnects at early, middle, and final positions resume correctly;
- sender, receiver, and dual restart resume from durable offsets;
- changed source, corrupt journal/partial, disk full, hash mismatch, write,
  flush, and rename failure never publish incomplete data;
- zero-byte files, empty directories, Unicode, 150-file batches, 64-bit offsets,
  hardlink and copy fallback all complete with exact final trees;
- cancellation and timeout storms leave no workers, buffers, or orphan partials;
- cache/provider/lease/update states reflect physical reality;
- status and logs contain no private source paths.
