# FlowShift - Current State

Updated 2026-07-23 at Phase 2.2 start.

## Current iteration

- Current version: `0.5.2-dev.4` (development, not stable).
- Last stable release: `v0.5.1` (Phase 2.1 complete).
- Active scope: Phase 2.2 "Final Clipboard Semantics Closure".
- Phase 3 (Transfer Hardening), Phase 4 (React Clipboard Overlay), and later phases must not start.

## Phase 2.1 completed (v0.5.1)

- 9 corrective slices implemented: preflight handshake, event coalescing, current-item semantics, received-payload cache, provider lifecycle, stress tests, API re-check, release workflow fix, full review.
- v0.5.1 tagged and released on GitHub. Tag must not be modified.
- 182 unit tests pass; overlay lifecycle (63), version-config (60), worker smoke, E2E, 30 reconnect rounds pass.

## Phase 2.2 completed

### Task 1: Current-item semantics separation

- `apply_remote_current` in `clipboard_store.py` replaced with `track_remote_revision`.
- Remote announcements/manifests no longer set `local current_item_id`.
- `make_current=False` in announcement and manifest handlers.
- 4 tests updated; 137 clipboard tests pass.

### Task 2: Provider lifecycle

- `_providers` registry in `ClipboardManager` tracking `device_id -> {state, last_seen_at, identity, item_count}`.
- States: `available`, `unconfirmed`, `online`, `offline`, `stale`, `invalid`.
- `on_peer_connected(device_id, identity)` sets `unconfirmed` (NOT available).
- `on_peer_disconnected(device_id)` sets `offline`.
- Manifest/announcement reconciliation transitions `unconfirmed` → `available`.
- 10 provider lifecycle tests pass, including reconnect-then-reconciliation.

### Task 3: Stress tests at scale

- **Clipboard Event Stress** (3.1): 10,000 synthetic notifications, 1,000 distinct sequences, duplicate coalescing, fast bursts, queue pressure/overflow, self-write suppression, shutdown during load.
- **Metadata Announcement Stress** (3.2): 5,000 announcements, duplicates, out-of-order, ACK correlation, invalid rejection, disconnect/reconnect, zero payload bytes verified.
- **Persistence Stress** (3.3): 200+ items across 5 save/load cycles, text/images/files, pins survive restart, current-item correct after restart, no duplicates, index integrity, provider registry not recovered as available.
- **Global Cache Limit** (3.4): `cache_max_total_gb` setting (default 10.0 GB), `_global_cache_enforce()` sums cache across all stores, evicts excess across peers, protects pinned/active items.
- **Provider Lifecycle Stress** (3.5): origin has payload → disconnect → offline → reconnect → unconfirmed → reconciliation → available; cache eviction removes provider status; lost announcement repaired by manifest.
- 34 stress tests pass (27 fast + 7 announcement).

## VERSION history (Phase 2.2)

- `991b4e1` — `0.5.2-dev.1` — current-item semantics separation
- `7d25ee8` — `0.5.2-dev.2` — provider lifecycle
- `7562dcd` — `0.5.2-dev.3` — fix: on_peer_connected sets unconfirmed, not available
- `902d435` — `0.5.2-dev.4` — stress tests at scale + global cache limit

## What's next (before v0.5.2)

- [ ] Task 4: Documentation — TODO_CURRENT.md and HANDOFF_CURRENT.md rewritten.
- [ ] Task 5: Full regression — compile, discover, smoke, E2E, all stress.
- [ ] Task 6: Tag and release v0.5.2.

## Productive path

- `src/python/tray.py --tray` is the productive Windows runtime.
- `src/python/gui.py` is the tkinter settings and legacy clipboard GUI.
- `webgui/` is the React/Vite settings UI and diagnostic overlay shell.
- Rust (`src/service`, `src/viewer`) remains experimental.

## Test baseline

- `test_clipboard_*.py`: 181 tests pass (147 original + 34 stress).
- `test_*.py` discovery: all suites pass.
- Stress suites: event (7), announcement (7), persistence (6), cache (4), provider-lifecycle (7), suppression (3).

## Scope boundary

- Phase 2.2 is active and nearing completion.
- Phase 3 and later phases are not started and must not be started in this iteration.
- The repository changes are not automatically deployed to any existing installation.
