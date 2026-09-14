# Clipboard Transfer V2 Architecture

## Status and Scope

This document is the Phase 3 design and implementation contract for
`clipboard_stream_v2`. As of `0.6.0` the V2 engine is productive: file
transfers between two `0.6.0` peers negotiate `stream_v2`; the legacy
`legacy_zip_v1` path remains only as the fallback for older peers or when
`clipboard_transfer_v2_force_legacy` is set. Sections marked **Implemented**
describe shipped code with tests; the short list under "Not in 0.6.0" in
Implementation Boundaries names what is still open.

The productive runtime remains `src/python/tray.py --tray`. V2 extends that
runtime; it does not replace it with the experimental Rust code and does not
build the Phase 4 clipboard UI.

## Legacy Fallback Path (`legacy_zip_v1`)

The legacy path, still used for peers without `clipboard_stream_v2`, is:

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

### Legacy full-payload work

In the legacy path file capture recursively scanned and hashed every source
file in `clipboard_files.scan_paths`. File transfer then read the source again
while building a deterministic ZIP. A disk-backed ZIP was read once for its
payload hash and again for network transmission. The receiver wrote the ZIP,
read it again for final verification, retained it as an object, and wrote the
logical payload again when extracting it for `CF_HDROP`.

`capture_files_all` repeated that scan and hash for each target profile. Store
startup also rehashed every persisted local source before marking it available,
and manifest construction repeated source revalidation.

Small payloads were assembled completely in RAM. `ChunkAssembler` retained
every chunk, joined them into another complete byte string, and the runtime
hashed the result again before storing it.

### Legacy transport and state

All peer control traffic uses a four-byte big-endian length followed by UTF-8
JSON. Legacy clipboard chunks are Base64 fields in those JSON messages. The
hard JSON frame limit is 28 MiB. TCP `sendall` is the only effective payload
backpressure on that link. `clipboard_transfer_ack` is the receiver-confirmed
final completion receipt of the legacy path; it is sent after the receiver
commits `clipboard_transfer_complete` and awaited by the sender when the
request carried `final_ack: true`.

In the legacy path transfer jobs and task closures, inbound assemblers,
preflight waiters and unsolicited result maps, preflight approvals, progress,
retries, remote metadata/current/revisions, and provider connectivity are held
in memory. The legacy path has no journal: orderly shutdown removes active
partial files, and crash remnants have no durable association with a transfer
and are eventually age-cleaned.

The legacy chunk-index resume request is not a restart mechanism. Its handler
creates a new transfer ID while the receiver still owns an assembler under the
original ID.

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

### Legacy limitations removed by V2

These limitations of the legacy path were the motivation for V2. They still
apply while a transfer runs over `legacy_zip_v1`:

- Copying files performs full content reads before history display.
- Empty directories and source fingerprints are not represented.
- Reparse points are not handled as an explicit security decision.
- Single files and batches both use ZIP and Base64.
- Source changes between capture and transfer are not reliably detected.
- No receiver-confirmed ACK window or bounded in-flight bytes exists.
- ZIP extraction lacks complete Windows path, collision, and expansion checks.
- No journal, no byte-level resume after disconnect or restart.

Provider metadata still does not drive provider routing or failover in either
path (see "Not in 0.6.0").

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
The capability flag lives in `input_backends/base.py` and the deterministic
selector in `platform_capabilities.select_clipboard_transfer_strategy`. The
Windows runtime
advertises `clipboard_stream_v2: true` in its `hello` and selects `stream_v2`
for a peer that advertises it as well; `tray.install_peer_connection` records
the selected strategy per link and hands it to
`ClipboardManager.set_peer_transfer_strategy`. The clipboard setting
`clipboard_transfer_v2_force_legacy` (default `false`) stops the
advertisement, selects `legacy_zip_v1` locally and makes the receiver reject
incoming V2 offers with `legacy_only`. Peers without the capability keep the
unchanged legacy path.

Existing text, HTML, image, and legacy file objects remain readable. Existing
deterministic ZIP batches are pasted through the legacy lazy extraction path;
there is no mandatory bulk migration. A legacy item may be converted lazily
only when a user operation already requires reading it, and failure leaves the
legacy object valid.

`item_id` continues to identify a copy event and is never a content hash. A
successful first V2 hash updates the same history item and increments its
revision; it does not append another history row.

## Data Roots and Migration

**Implemented:** All clipboard data lives below one clipboard root,
`<data>\clipboard`, where `<data>` is `FLOWSHIFT_LOG_DIR` when set and
otherwise the runtime data directory (`tray.DATA_DIR`, in packaged
installations `%ProgramData%\FlowShift`). Within that root the productive
layout is:

```text
<data>\clipboard\
  profiles\<profile-dir>\            per-peer index.json, legacy objects, previews, temp
  objects\sha256\<prefix>\<hash>     shared V2 per-file objects (content-addressed)
  objects\pending-v2\                publication pins of in-flight V2 transfers
  objects\publication-v2.lock        cross-process publication lock
  manifests\sha256\<prefix>\<digest>.json  finalized V2 batch manifests
  incoming\<transfer_id>\            V2 receiver staging (<index>.part / .verified)
  journals\incoming\<transfer_id>.json
  journals\outgoing\<transfer_id>.json
  temp\incoming\, temp\preview\      legacy assembler and preview scratch
```

Lease-owned materializations are created below the destination root the
runtime passes to `materialize_files_result` (`<dest_root>\<profile>\<item_id>`).
Upgrades keep the existing root: no payload, index, journal, or object is
copied or deleted during an update, and legacy profile directories keep their
names. Peer messages and normal clipboard API responses never contain the root
or any path below it.

**Not in 0.6.0:** a separate data-root setting with a shared root resolver
across runtime, installer, updater, uninstaller, and diagnostics (fail-closed
when two roots contain data) and a persisted identity-to-directory table for
profile names. Profile directories continue to use `profile_dir_name`.

## Metadata-first Capture

**Implemented:** Explorer file capture performs
bounded enumeration and metadata collection only. It does not calculate full
file hashes and does not build a ZIP.

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

**Implemented:** File and directory payloads use a
canonical schema-2 manifest. Canonical JSON uses UTF-8, sorted keys, fixed
separators, and no insignificant whitespace. `manifest_digest` is SHA-256 of
those canonical bytes with the digest field omitted.

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

**Implemented:** One validator is used by manifest
parsing, strict productive legacy materialization, V2 staging, object-store
finalization, and V2 materialization. It
normalizes separators to `/` for the wire but does not silently rename entries.

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

**Implemented:** A thread-safe `TransferSession` is
the single source of runtime transfer state. It is not reconstructed from
unrelated dictionaries.

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

The current foundation bounds persisted and in-memory session status globally.
The V2 flow-control layer additionally enforces global and stable-peer active
transfer admission before a stream can allocate its payload window.

Session status snapshots are persisted atomically with schema-2 store state.
Byte-level resume after a restart is claimed only from a durable incoming or
outgoing journal (see "Persistent Journal and Resume"); a non-terminal session
snapshot without a matching journal fails closed. Legacy completion
receipts are receiver-confirmed between supporting peers and remain compatible
with older peers that do not request the additional acknowledgement.

## Typed Framing

The strict typed-frame codec is implemented in `clipboard_framing_v2.py`. It
provides bounded JSON-object control frames, raw binary payload frames, an
incremental timeout-safe reader, and a per-socket serialized writer. Protocol
errors make a reader terminal so a channel cannot continue after malformed or
integrity-invalid input.

**Implemented transport integration (`clipboard_transport_v2.py`):** The normal
peer control/input link keeps its legacy JSON codec. It carries the V2 control
messages `clipboard_stream_v2_offer` (with the complete canonical manifest),
`_accept` (with a fresh random single-use channel nonce, bounded lifetime) or
`_reject` (reason code), `_resume_request` / `_resume_response` (same shapes,
plus per-file durable offsets), and `_cancel` / `_cancel_ack`. The sender opens
a separate socket to the peer's listening port and sends one bounded legacy
JSON `clipboard_stream_v2_channel` hello containing the transfer id, its device
id and the nonce. `tray.peer_handler` recognizes that hello as the first message,
never installs it as a peer link, and hands the socket to
`ClipboardManager.accept_stream_v2_channel`, which consumes the nonce
atomically (bound to transfer and peer identity, expiring, never logged) and
replies with a JSON `channel_ack`. Only after that barrier does the socket
switch to typed frames; buffered post-ACK JSON bytes refuse the channel. The
typed channel then carries `manifest` / `manifest_ack` (receiver resume offsets
and window parameters), binary payload frames, cumulative window ACKs,
`complete` (sender hashes and fingerprints) and `complete_ack` (finalized
digest and revision). Input/control traffic never shares the data socket.
Failed channel setup leaves the peer link in legacy mode and pauses only the
V2 session.

Every post-switch frame is:

```text
uint32_be following_length  bytes after this field, including typed header
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

Header and declared payload lengths must match exactly. JSON control bodies are
limited to 16 MiB and reject duplicate keys, excessive nesting, non-finite or
oversized numbers, and non-object roots. Binary payloads are limited to 4 MiB.
Unknown frame kinds, unknown major versions, unsupported flags, invalid UUIDs,
out-of-range indices or offsets, overflow, checksum mismatches, and oversized
frames are protocol errors. They fail the affected transfer and close the typed
channel, but do not crash network or runtime threads.

The parser supports fragmented reads and multiple frames in one socket read.
It allocates no body above the negotiated hard limit.

## Transfer Control Lifecycle

**Implemented:** Control messages are schema-versioned JSON (`schema_version`
1, `protocol_major` 2) and include the canonical transfer id plus item id,
item revision and manifest digest as applicable. Every field is validated for
type, bounds and identity before it reaches a session; malformed messages are
logged and ignored, unknown transfers get only a `cancel_ack`.

The normal lifecycle is:

```text
offer/preflight                 (control link, carries the manifest)
  -> accept (nonce) | reject (reason)
  -> channel hello / channel_ack (dedicated socket)
  -> manifest
  -> manifest_ack with receiver resume offsets and window
  -> binary payload frames
  -> cumulative ACKs
  -> complete with final file hashes/fingerprints
  -> receiver verification, publication and complete_ack
```

`OutgoingTransferSession` marks a transfer completed only after a positive
`complete_ack`. The send adapter propagates no-link and socket errors into the
session (`waiting_reconnect` or `failed`); no worker converts a failed send into
success. On the receiver `ClipboardManager._on_stream_v2_offer` runs
`prepare_stream_v2_receive` (reject creates neither stage nor journal), stores
the schema-2 provisional item bound to the manifest, and the channel thread
drives `IncomingTransferStage.accept` / `finalize` and
`publish_stream_v2_session`, which commits with the legacy current-item,
enforce, eviction and stats semantics.

## Streaming, Flow Control, and ACKs

The transport-neutral V2 flow-control and ACK foundation is implemented in
`clipboard_flow_control_v2.py`. It provides validated limits, global and
per-stable-peer admission, bounded send/receive queues, a per-transfer sender
window, finite ACK waits, and cumulative ACK batching. Bytes-like payloads are
copied to immutable bytes before retention so queue accounting cannot be
bypassed by mutable buffers or small views of large backing allocations.

The dedicated V2 ACK control message is strictly separate from the legacy final
completion ACK. It carries canonical transfer UUID, entry index, contiguous
verified offset, durable offset, receiver state, and bounded missing ranges.
ACK offsets use the same limits as binary framing. Stale ACKs are idempotent;
ACKs beyond sent data or inside a chunk are rejected. Paused and terminal
receiver states close the sender window and release admission, while verification
and finalization remain active states that continue to block conflicting work.

The legacy `TransferQueue` now rejects duplicate transfer IDs and retires
completed, cancelled, rejected, and shutdown job closures instead of retaining
them without a bound.

Direct streaming primitives are implemented in `clipboard_streaming_v2.py`.
Default chunk payload is 2 MiB and bounded by the 4 MiB frame limit. The sender
opens one source file at a time, validates the captured path and open handle,
reads sequentially without seeking or pre-hashing, updates the file SHA-256, and
submits immutable raw chunks. Empty files and directories produce no binary
payload but remain explicit completion evidence. Every source is revalidated
after its read and again before sender completion becomes available.

Transport-neutral persistent resume is implemented in `clipboard_resume_v2.py`.
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

The implemented receiver batcher emits cumulative flow-control ACKs after
8 MiB, four chunks, 250 ms, or file completion, whichever occurs first. The
thresholds and timeouts are injectable in tests. ACKs are recorded only after a
complete sequential stage write and receiver hash update; a final file chunk is
reported only after flush and `fsync`. Durable offsets advance independently of
verified flow-control offsets only after the persistent journal commit succeeds.

A flow-control ACK may release memory after buffered writes complete. A durable
resume offset advances only after the `.part` data is flushed and `fsync`ed,
then the journal is atomically committed. Checkpoints batch this cost by bytes
and time. Startup truncates or revalidates bytes beyond the durable offset.

The sender cannot release a window slot until ACK. The tested typed-channel
loopback routes actual sequential file chunks through bounded send and receive
queues into verified receiver staging, delays the receiver, and verifies exact
bytes and hashes while in-flight chunks remain at or below the configured limit.
Duplicate data below the acknowledged offset may be ignored only after its
session and manifest are validated. Out-of-order data beyond the bounded window
is rejected.

## Staging and Atomic Finalization

Each incoming direct stream owns a dedicated staging directory. Files are
created exclusively and written sequentially as `<entry-index>.part`; remote
names are never used during receipt. Directories are represented in the
validated manifest and are not created from remote paths during staging.

At file completion the stage flushes and `fsync`s, validates physical size, and
retains the receiver SHA-256. Transfer finalization requires exact sender hashes,
source fingerprints, total bytes, transfer ID, and provisional manifest digest.
Only then are `.part` files renamed atomically, one file at a time, to
`<entry-index>.verified`, and the finalized next manifest revision is returned.
No staged result is exposed until every rename succeeds. Journal-backed stages
preserve restart-safe state on failure and reconcile rename/commit crash windows;
non-journal stages retain best-effort cleanup. Cross-process object publication
is handled by the shared object store under its publication lock (see "Object
Store and Provider State").

Checkpoints batch journal writes by bytes and time. They flush and `fsync` the
partial before recording a durable offset but do not checkpoint every chunk. File
completion flushes and `fsync`s once, verifies size and SHA-256, revalidates the
sender fingerprint result, moves the verified file atomically into the object
store, then commits the journal.

Disk-full, short write, generic write, flush, hash, size, and rename failures
have distinct path-free error codes. A `.part` is never returned as verified
data or published as an object or provider payload.

## Persistent Journal and Resume

Incoming and outgoing journals use a strict versioned canonical JSON schema and
same-directory temporary-write, flush, `fsync`, and write-through atomic replace.
Generation CAS is serialized across store instances. Each journal records:

- transfer/session identity and stable peer identity;
- item ID/revision and manifest digest;
- provider and strategy;
- source fingerprints;
- expected file sizes/hashes;
- completed files and highest durable contiguous offsets;
- verified and remaining bytes;
- retry count, timestamps, and state.

Startup inventory parses journals with strict size, field, geometry, digest, and
duplicate-key bounds. Corrupt supported-version journals do not crash startup
and are quarantined; future-version journals remain untouched. No partial is
accepted without exact size, prefix hash, regular-file, and stage ownership
validation. Bytes beyond the journal-durable offset are truncated and `fsync`ed.

Pause/disconnect forces a durable checkpoint, transitions to `paused` or
`waiting_reconnect`, and closes partial handles while retaining journal and stage.
Cancel is terminal but preserves state for coordinated cleanup; explicit purge
uses a persistent `purging` tombstone. Reopen is allowed only for resumable
states and validates stable peer identity, item revision, manifest digest, entry
geometry, and source fingerprints. Sender resume re-hashes the retained prefix,
reports prefix and payload reads separately, and starts emission at the receiver
durable offset. Completed files are validated without retransmission.

On a data-channel loss the receiver checkpoints and enters `waiting_reconnect`;
the sender enters `waiting_reconnect`, keeps its outgoing journal and, within
the finite reconnect deadline, sends `clipboard_stream_v2_resume_request`
(retrying when the control link is down and when `on_peer_connected` fires).
The receiver reopens its stage from the journal through
`prepare_stream_v2_receive(resume=True)`, answers with a fresh one-time nonce
and its per-file durable offsets, and repeats them in `manifest_ack`. The
sender validates that evidence against its outgoing journal, re-hashes the
retained prefix and resumes emission exactly at the durable offsets; durable
bytes never travel twice. Unknown or terminal sessions are rejected without
item metadata. A bounded `resume_inventory` exchange with lower-device-id
coordination for duplicate live sessions is not in 0.6.0.

Restart resume is productive for sender restart, receiver restart, or both.
The receiver reopen after a runtime restart uses the journal path above. After
a sender restart, `ClipboardManager.resume_outgoing_stream_v2(identity)` runs
from `on_peer_connected`: it scans `journals/outgoing`, purges terminal
leftovers, restores every resumable journal for that peer into a paused
`OutgoingTransferSession` (`restore`, with identity and digest checked by
`validate_resume_match`) and re-offers it with `clipboard_stream_v2_resume_request`.
A restart counts as one resume attempt; the retry count is persisted in the
outgoing journal and bounded by `MAX_RESUME_ATTEMPTS` (128, cumulative). If the
source item vanished or its manifest changed, the journal is purged and the
peer receives `clipboard_stream_v2_cancel` with reason `source_missing` or
`source_changed`; a cancel for a transfer without a live session purges an
orphaned incoming journal and stage on the receiver.
Changed source, changed manifest, corrupt partial, impossible offset, or stale
journal causes explicit resume rejection and a safe restart or terminal failure
according to policy.

## Preflight V2

**Implemented, transport-neutral:** Preflight is keyed by transfer ID, strategy, item revision,
manifest digest, and expiry. It runs before source hashing or payload reads.

For V2 it includes remaining staging bytes, unverified object bytes, bounded
journal/manifest overhead, requested materialization bytes, and safety margin.
Durably verified resume bytes are subtracted exactly once. It does not reserve
space for a ZIP or extracted duplicate unless legacy strategy is selected.

Legacy preflight retains its worst-case ZIP accounting and size limits. A
successful estimate is not a disk reservation; later disk-full errors remain
explicitly handled.

`clipboard_preflight_v2` validates immutable estimates and accepted evidence.
Fresh stage creation and source reads require acceptance; reopening also checks
the loaded journal before any retained data is truncated or rehashed. Reduced
reservations bind journal generation/digest and durable per-file offsets; a full
reservation may conservatively cover a resumed transfer. Metadata bounds include
final manifest/journal encoding and atomic rewrite overlap. The caller supplies
index rewrite/growth and any requested materialization allocation. This is not a
network authorization token; the offer/accept routing binds the accepted
transfer to a single-use channel nonce instead.

**Implemented runtime wiring:** `ClipboardManager.preflight_stream_v2_receive`
reads real free space of the profile store, applies hard/auto limits and lease
materialization bytes, and accepts via `clipboard_preflight_v2`; the pure
`compute_stream_v2_preflight` in `clipboard_model` gives the same accounting
(resume credit only with a journal; cache-disabled counts the full remaining
payload). `prepare_stream_v2_receive` is the receiver session preparation the
transport calls: a rejected preflight creates neither stage directory nor
journal. Disk-full during staging or journal commit maps to `disk_full`; the
journal stays retryable (`failed`), the partial and last durable offset are
kept, and the stage is never `cancelled`.

## Object Store and Provider State

**Implemented, transport-neutral:** A shared object-store service stores every verified file by
its own SHA-256. A batch manifest maps names and directories to those objects.
Equal contents are physically stored once in the shared clipboard object root.
Items, manifests, sessions, and leases create explicit references. Publication
uses same-volume temporary files and atomic no-replace semantics under a
bounded cross-process root lock. An existing object is accepted only after size/hash verification
or unchanged process-local verified file evidence.
Global garbage collection takes the same locks and deletes only unreferenced
objects.

Normal publication links receiver-verified `.verified` files into the shared
same-volume namespace without another payload copy or hash pass. Receiver evidence
includes file identity, size, mtime, and change time (Windows FILE_BASIC_INFO,
not creation time). Restart recovery rehashes stages before issuing fresh evidence.
Windows denies concurrent WRITE opens during verified rename and object publication.
POSIX cannot enforce those sharing modes and conservatively rehashes the handoff;
the single-read fast path is verified on the productive Windows platform.
Unsupported hardlinks/cross-volume publication fails with retained staging; there
is no silent copy that bypasses preflight accounting.

`IncomingTransferStage.publish` installs objects and manifest, atomically replaces
the same history item with a durable transfer receipt, then completes the journal
and cleans staging. Index failures retain stages and publication pins for retry.
`recover_finalization` uses rehashed journal evidence; `cleanup_completed` retries
cleanup only after verifying the actual durable index receipt. Explicit purge
accepts the object-store service to release transfer-bound pending pins. Shared
objects are removed only by explicit reference-aware GC, not transfer cancellation.

Legacy-only indexes remain schema 2. The first V2 receipt writes schema 3 without
moving legacy objects. Resume journals are schema 2; validated schema-1 journals
get a preserved backup and one CAS generation increment. Old incoming `completed`
means stage-only completion and migrates to `finalizing`, never provider availability.
V2-only items are excluded from schema-1 announcements and legacy payload access;
`known_hashes` additionally reports the metadata identity of a finalized V2
item so the peer's schema-1 manifest does not re-request the same copy event.
Network completion and resume messages are implemented; provider routing and
failover between providers are not in 0.6.0.

Availability is checked at two levels. Index load, listing, `known_hashes`, and
provider state use `item_is_deliverable`: manifest/payload/content-identity
consistency plus regular-file and exact-size checks for the manifest file and
every object, without reading payload bytes. Delivery (`v2_manifest_for_item`,
used by materialization) and publication verification rehash objects unless the
process-local fingerprint cache is unchanged. A same-size corrupted object is
therefore listed as cached but is refused at paste time.

V2 items participate in received-cache accounting keyed by content identity with
`payload_size = total_size`. Evicting such an entry sets the item to `missing`,
marks the local provider `unavailable`, prunes the transfer receipt in the same
index write, and then runs reference-aware GC. GC references objects only from
items in a deliverable payload state; pending pins and materialized hardlinks
still protect objects through their link count. Runtime eviction removes only
the LRU excess above `cache_max_mb`; the former unbounded eviction of every
unprotected entry while under budget was a latent legacy defect that would have
freed V2 objects after each receive. Current, pinned, and leased items remain
protected.

An item is a local available provider only after every referenced object
and manifest is verified and readable (`item_is_deliverable`). Journals and
partials never create provider availability. Object loss, corruption, or
physical eviction updates item payload state and provider state atomically.

**Not in 0.6.0:** provider routing and failover. The target design chooses
local verified data first, then connected providers in stable-ID order,
preferring an existing resumable session; the provider must advertise the
exact item revision and manifest digest, and on failure the next matching
provider receives a new one-time channel offer. In 0.6.0 a request is sent to
the selected profile peer only. Productive peer connect/disconnect already
calls the clipboard lifecycle hooks (`on_peer_connected` /
`on_peer_disconnected`).

When received caching is disabled, synchronization remains metadata-only. On
explicit selection, verified data flows into a temporary lease-owned
materialization, is retained only through the bound clipboard lease, and is not
advertised as a durable provider. Failed selections expire through
journal-aware cleanup; preflight counts the complete remaining materialization.

## Materialization and Leases

**Implemented for published V2 items:** `clipboard_materialize_v2` materializes a
validated manifest into the existing lease directory
(`<dest_root>\<profile>\<item_id>`). On the same volume, regular files use
hardlinks from the object store; a linked target must report the object's
device and inode. Other volumes, unsupported filesystems, permission failures,
and link failures use a streaming copy fallback that re-verifies size and
SHA-256 while copying and aborts as `object_corrupt` on mismatch. Hardlink
metadata is never modified because it is shared with the object store.

The complete tree is constructed under a temporary sibling name and atomically
renamed before `CF_HDROP` receives exactly the top-level manifest roots, not
every leaf. Empty directories, zero-byte files, and nested structures are
preserved. Failed materialization removes its staging and is never exposed. No
ZIP is produced. Lease release unlinks only the materialization; shared objects
remain until reference-aware GC. `ClipboardManager.materialize_files_result`
routes `object_manifest_v2` items to this path and reports the strategy.

**Implemented:** A lease is created at materialization and bound to the
successful Windows clipboard sequence. Every newer observed clipboard sequence
(`tray.clipboard_watcher`) and every later FlowShift write retires leases bound
to an older sequence: they become `stale` with the tree retained, and
`cleanup_leases` removes stale/released trees after the configured
`clipboard_temp_cleanup_max_age_hours` (also used for temp sweeps). Active leases
are never deleted because of age. Lease release unlinks only the materialization.

**Not in 0.6.0:** persisting the lease as `pending_write` before the Windows
clipboard write so failed writes release it immediately, and startup
revalidation of unbound leases against current `CF_HDROP` ownership and paths.

## Cancellation, Timeouts, and Updates

**Implemented for the receiver stage:** `IncomingTransferStage.cancel(reason)`
works in every phase (accepted/preflight, transfer, verification, finalization,
paused, waiting_reconnect, failed): it closes the open handle, drops in-memory
buffers and finalization results, marks the journal `cancelled` and keeps the
partials until `acknowledge_cancel` (peer ACK) or the final-ack timeout purges
stage and journal. `ClipboardManager.cancel_stream_v2_session(transfer_id,
reason)` and `acknowledge_stream_v2_cancel` expose this for registered sessions;
a cancelled session is no longer busy for the update idle gate. Sender-side
cancellation is implemented in `OutgoingTransferSession.cancel`: it stops
source reads, closes the send window, marks the outgoing journal `cancelled`,
sends `clipboard_stream_v2_cancel` and purges after `cancel_ack` or the
final-ACK timeout. A cancel received from the peer is acknowledged and purged
immediately on either side; a receiver protocol violation (malformed,
oversized, out-of-order or foreign frame) cancels with `protocol_error` and
notifies the sender. Retention is explicit:

| Cause | Journal and partial policy |
|---|---|
| User cancel | mark cancelled; delete after peer ACK or timeout |
| Disconnect | checkpoint and retain until reconnect deadline |
| ACK/no-progress timeout | checkpoint and retain for bounded retry |
| Source changed | mark stale, reject resume, delete receiver partials |
| Integrity failure | mark invalid; quarantine then bounded-delete |
| Disk/write/flush failure | retain only the last durable checkpoint for explicit retry |
| Final rename failure | retain verified stage and journal for bounded finalization retry |

**Implemented:** `clipboard_transfer_control_v2.TransferTimeouts` holds the six
finite timeouts (preflight, manifest ACK, window ACK, no progress, reconnect
wait, final complete ACK), read from the clipboard settings keys
`clipboard_transfer_v2_<name>_timeout_s` (clamped to 1..86400 s). A
`DeadlineTracker` arms exactly one absolute deadline per phase; it fires once
when `now >= deadline`. `IncomingTransferStage.check_timeouts(now)` applies:
preflight without payload -> purge (`preflight_timeout`); no progress while
receiving -> checkpoint and retain as `paused` (`no_progress_timeout`), then
bounded by reconnect wait; `paused`/`waiting_reconnect` past reconnect wait ->
retryable `failed` with `reconnect_timeout`, partials retained; finalizing past
the final-ack timeout -> in-memory `failed` with `final_ack_timeout` while the
journal stays `finalizing` with the verified stage for a bounded finalization
retry; cancelled past the final-ack timeout -> purge. The outgoing session
enforces the preflight (offer), manifest-ACK, window-ACK (through the flow
control window), reconnect and final-complete-ACK deadlines in its worker
thread and reports `manifest_ack_timeout` / `window_ack_timeout` /
`reconnect_timeout` / `final_ack_timeout` as its error code.
`ClipboardManager.run_stream_v2_maintenance` runs the checks
from the productive clipboard watcher tick (throttled to one pass per second)
and logs fired timeouts and state transitions. There are no indefinite waits.

The update idle gate is implemented: `ClipboardManager.transfer_activity_state`
reports `busy` for legacy jobs (pending/running/retrying/paused), open
assemblers, and V2 stages in `receiving`, `verifying`, `finalizing`, or an
active resume; it feeds `activity_snapshot`, so the existing updater
`WAITING_FOR_IDLE` loop defers the install with a rate-limited INFO log. Policy
`paused_and_waiting_reconnect_allow_update`: V2 `paused` and
`waiting_reconnect` stages block only while they own uncommitted state; after a
durable checkpoint with closed handles they permit the update while preserving
journals and partials. Legacy paused jobs still block. Update rollback
preserves schema-2 indexes, journals, partials, objects, leases, and provider
state.

## Status, Privacy, and Logging

**Implemented:** `IncomingTransferStage.status()`,
`OutgoingTransferSession.status()` and
`ClipboardManager.stream_v2_status()` expose transfer ID, item ID, direction
(`incoming` / `outgoing`), strategy,
state and journal state, the manifest-relative current file name, file
index/count, current file bytes, bytes done and total bytes, percent, an EWMA
rate, ETA, resume bytes, retry count (incremented per journal reopen),
provider, preflight state, structured error code, cancel reason and the active
timeout with its remaining seconds. Paused, waiting-reconnect and finalizing
time is excluded from rate and ETA: the estimate freezes and the first sample
after a pause only re-anchors the clock. The list is published under the new
`stream_v2` key of `ClipboardManager.diagnostics` (`/api/clipboard/status`)
without changing the existing keys.

Normal local clipboard APIs return an explicit public item projection. They do
not return source paths, store roots, materialization paths, journal paths, or
other private absolute paths. Peer metadata follows the same rule.

Logging is structured and rate-limited at lifecycle boundaries: negotiation,
strategy, manifest, preflight, journal, stream start, file complete, pause,
disconnect, resume, verification, source changed, disk full, finalization,
legacy fallback, and cleanup. `ClipboardManager._log_stream_v2` logs stream
start/resume, cancel, cancel cleanup, timeouts, observed state transitions,
finalization and lease-only cleanup with transfer/item IDs, states, codes and
byte counts only. It logs neither clipboard content nor private absolute paths
and never logs each ordinary chunk; repeated keyed events are limited to one
entry per interval.

## Implementation Boundaries

The implementation landed in these slices:

1. capability, schema-2 item/session model, manifest, path validation;
2. typed framing and parser integration;
3. bounded flow control and cumulative ACKs;
4. staging and direct file streams;
5. persistent journals and disconnect/restart resume;
6. shared per-file object store and lease-based materialization;
7. provider, cache, preflight, API, connection, and update integration;
8. fault injection, resource instrumentation, and stress tests.

Each slice preserved the legacy fallback and productive Windows runtime, added
concrete end-state tests, updated the central version and operational state,
and was committed and pushed before the next slice.

### Not in 0.6.0

These parts of the design are not implemented in `0.6.0` and are not started
automatically:

- `resume_inventory` exchange with lower-device-id coordination for duplicate
  live sessions;
- provider routing and failover between providers (requests go to the selected
  profile peer only);
- lease `pending_write` persistence before the clipboard write and startup
  revalidation of unbound leases;
- a separate data-root setting, shared root resolver, and persisted
  identity-to-directory table for profile names;
- mirroring V2 sessions into the legacy `TransferJob` GUI list (V2 sessions are
  reported through `diagnostics()["stream_v2"]` / `/api/clipboard/status`).

## Acceptance Evidence

V2 counts as complete because tests prove, through connected productive runtime
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

**Evidence (`0.6.0`):** the automated suites in `src/python` cover these
points through connected productive components: `test_clipboard_transport_v2`
(paired real managers, socket channels, disconnect at early/middle/final
positions, receiver/sender/dual restart, changed source purge),
`test_clipboard_stress_v2` (10k typed frames, 100 disconnect/resume cycles,
200-file batch, cancel storm, concurrent status polling, slow-receiver window
bound, disk-full resume, restart thread-leak check, malformed-frame burst,
>4 GiB offsets), `test_tray_stream_v2_e2e` (two peers over real localhost TCP
through `tray.peer_handler`, `_clip_send`, `_clip_open_channel`, both
directions, mid-transfer link drop), plus the framing, flow control, staging,
resume, object store, materialization, preflight, cache, update gate, and
WebGUI status suites. Remaining manual evidence is listed in
`MANUAL_TEST_CHECKLIST.md`.
