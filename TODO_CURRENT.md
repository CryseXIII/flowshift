# FlowShift - Open Development Tasks

## Current phase

Phase 2.2 – Final Clipboard Semantics Closure

Every commit increments `VERSION` exactly once. Dev versions are `0.5.2-dev.N`.
The final fully validated release commit changes `VERSION` to `0.5.2` and tags `v0.5.2`.

Phase 3 (Transfer Hardening, UI, Shell) must not start until Phase 2.2 is released.

## Phase 2.1 closed slices (v0.5.1)

All 9 corrective slices completed, validated, and released as v0.5.1.

## Phase 2.2 remaining work

- [x] Task 1: Current-item semantics separation — `track_remote_revision` replaces `apply_remote_current`.
- [x] Task 2: Provider lifecycle — online/offline/stale/invalid/unconfirmed states, connect/disconnect hooks.
- [x] Task 3: Stress tests at scale — 10k events, 5k announcements, persistence cycles, global cache limit.
- [ ] Task 4: Documentation — rewrite TODO_CURRENT.md and HANDOFF_CURRENT.md for Phase 2.2 state.
- [ ] Task 5: Full regression — compile check, unittest discover, worker smoke, e2e, all stress suites.
- [ ] Task 6: Tag and release v0.5.2 on GitHub.

## Open validation tasks

- Validate fresh install, upgrade, rollback, and uninstall on a disposable Windows x64 VM.
- Verify provider lifecycle disconnect/reconcile in a real two-device setup.

## Future phases (not started)

### Phase 3: Clipboard Transfer Hardening

- Batch manifest transfers.
- Disk peak planning.
- Reduced HDD write amplification.
- Persistent resume state across runtime restarts.

### Phase 4: React Clipboard Overlay

- Win+V integration.
- Interaction Target routing.
- Item rendering, markup, and previews.
- Selection, live progress, and clipboard materialization.

### Phase 5–8: Command Wheel, Shell Integration, Full Hardening
