# FlowShift - Current State

## Release state

- Current version: `0.6.0-dev.10`.
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

## Last pushed commits

- `110a854` - Phase 3 dev.9: persist transfer resume state.
- `e6a9438` - Phase 3 dev.8: add direct file staging.
- `f8d0355` - Phase 3 dev.7: add bounded transfer flow control.
- `828e7e0` - Phase 3 dev.6: add typed clipboard framing.
- `32530ce` - Phase 3 dev.5: establish transfer v2 foundation.

## Open work

- Implement the remaining Phase 3 slices from provider/materialization and
  runtime preflight/cache/update integration through productive transport,
  hardening/stress validation, and release `v0.6.0`.
- Keep the existing manual hardware and VM checks open in `TODO_CURRENT.md`.

## Next planned phase

- Phase 4 is not started and must not begin automatically.
