# FlowShift - Current State

Updated 2026-07-23 at Phase 2.3 completion.

## Current iteration

- Current version: `0.5.3` (stable).
- Last stable release: `v0.5.3` (Phase 2.3 complete).
- Active scope: Phase 2.3 "Final Clipboard Closure".
- Phase 3 (Transfer Hardening) may now begin.

## Phase 2.1 completed (v0.5.1)

- 9 corrective slices implemented: preflight handshake, event coalescing, current-item semantics, received-payload cache, provider lifecycle, stress tests, API re-check, release workflow fix, full review.
- v0.5.1 tagged and released on GitHub. Tag must not be modified.

## Phase 2.2 completed (v0.5.2)

### Task 1: Current-item semantics separation

- `apply_remote_current` in `clipboard_store.py` replaced with `track_remote_revision`.
- Remote announcements/manifests no longer set `local current_item_id`.
- `make_current=False` in announcement and manifest handlers.

### Task 2: Provider lifecycle

- `_providers` registry in `ClipboardManager` tracking `device_id -> {state, last_seen_at, identity, item_count}`.
- States: `available`, `unconfirmed`, `offline`, `stale`, `invalid`.
- `on_peer_connected(device_id, identity)` sets `unconfirmed` (NOT available).
- `on_peer_disconnected(device_id)` sets `offline`.
- Manifest/announcement reconciliation transitions `unconfirmed` -> `available`.

### Task 3: Stress tests at scale

- Clipboard Event Stress (3.1): 10,000 synthetic notifications, 1,000 distinct sequences, duplicate coalescing, fast bursts, queue pressure/overflow, self-write suppression, shutdown during load.
- Metadata Announcement Stress (3.2): 5,000 announcements, duplicates, out-of-order, ACK correlation, invalid rejection, disconnect/reconnect, zero payload bytes verified.
- Persistence Stress (3.3): 200+ items across 5 save/load cycles, text/images/files, pins survive restart, current-item correct after restart, no duplicates, index integrity.
- Global Cache Limit (3.4): `cache_max_total_gb` setting, `_global_cache_enforce()` sums cache across all stores, evicts excess across peers.
- Provider Lifecycle Stress (3.5): origin -> disconnect -> offline -> reconnect -> unconfirmed -> reconciliation -> available.

## Phase 2.3 completed (v0.5.3)

### Corrections (dev.1: `1d12166`)

- **`_global_cache_enforce`**: active transfer protection now iterates jobs then searches all stores (stale loop variable fixed). Eviction accounting subtracts freed bytes (not remaining bytes). Returns honest `limit_satisfied`/`over_bytes` diagnosis.
- **`_register_remote_providers`**: respects remote provider state from metadata (`available`/`unconfirmed`/`offline`/`stale`/`invalid`) instead of blind promotion to `available`.
- **`_evict_cache_if_needed`**: same active-transfer store lookup fix.
- **`version_item`**: provider state validation expanded to accept all states handled by `_register_remote_providers`.

### Rewritten stress tests (dev.2: `09c2c71`)

- **Event overflow stress** (Test A): 10k events into capacity=4 queue, slow consumer, real overflow metrics.
- **Event throughput stress** (Test B): 10k events, 1k sequences, concurrent producer/consumer, real metrics.
- **Event normal path tests** (Test C): deterministic coalescing, suppression, shutdown-under-load.
- **Provider state import tests** (8 tests): `available` stays `available`, `offline` stays `offline`, `stale` stays `stale`, `unconfirmed` stays `unconfirmed`, unknown -> `unconfirmed`, malformed ignored, duplicate reconciliation, `offline` not promoted by manifest.
- **Global cache limit real tests** (6 tests): byte-level enforcement, pinned protection, active transfer protection, lease protection, local current protection, unsatisfiable diagnosis, dedup counting.
- **Metadata announcement stress**: 500 announcements with real byte-payload assertions and zero-payload-transfer verification.
- **Provider lifecycle**: strengthened disconnect/reconnect/reconciliation cycle tests.

### Test results

- **236 tests pass** across all suites.

### VERSION history (Phase 2.3)

- `1d12166` — `0.5.3-dev.1` — fix global cache enforcement + provider state import
- `09c2c71` — `0.5.3-dev.2` — rewrite stress tests (real metrics, provider import, cache limit)

## Productive path

- `src/python/tray.py --tray` is the productive Windows runtime.
- `src/python/gui.py` is the tkinter settings and legacy clipboard GUI.
- `webgui/` is the React/Vite settings UI and diagnostic overlay shell.
- Rust (`src/service`, `src/viewer`) remains experimental.

## Scope boundary

- Phase 2.3 is complete. Phase 3 (Clipboard Transfer Hardening) may now begin.
- The repository changes are not automatically deployed to any existing installation.
