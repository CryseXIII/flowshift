# FlowShift - Current State

Updated for the Phase 2.3 closure release.

## Release state

- Current version: `0.5.4`.
- Current stable release: `v0.5.4`.
- Active implementation phase: None.
- Next planned phase: Phase 3 - Clipboard Transfer Hardening.
- Phase 3 has not started.
- The immutable `v0.5.3` tag remains unchanged; its release workflow failed.

## v0.5.4 closure corrections

- Reproduced the immutable `v0.5.3` release command with 236 tests, 5 failures, and 9 errors. Full causes are recorded in `docs/v0.5.3-ci-failure.md`.
- Repaired the event overflow deadline and made overflow accounting deterministic.
- Added producer backpressure and exact accounting to the 10,000-event, 1,000-sequence throughput test.
- Added an instrumented in-memory transport that serializes the real protocol frame before delivery through `ClipboardManager.handle`.
- The metadata stress test delivers exactly 5,000 announcements to the receiver through the productive handler path.
- Transport metrics separately count `metadata_message_bytes`, decoded `payload_content_bytes`, and `control_message_bytes`.
- The 5,000-announcement test proves metadata bytes are positive and decoded payload content bytes are exactly zero.
- A calibration test sends a real serialized transfer chunk and proves the payload counter increases by the decoded content size.
- Provider fixtures now satisfy protocol payload identity rules while testing available, offline, stale, unconfirmed, fallback, malformed, and reconciliation states.
- Provider import ignores malformed non-dictionary entries instead of raising `AttributeError`.
- Global cache enforcement counts shared content SHA values once across stores and evicts all duplicate records together.
- Cache-limit settings are supplied persistently to tests; lease protection uses a real leased item; synthetic hashes are valid SHA-256 values.

## Verified test baseline

- Exact release discovery from `src/python`: `python -m unittest discover -p "test_*.py"` - 237 tests, exit code 0.
- Runtime worker smoke test: passed.
- Runtime E2E test: passed.
- Reconnect stress: 30 rounds passed.
- Overlay IPC stress: passed, including 1,000 ping/pong and 1,000 hide/response exchanges.
- Overlay show/hide stress: passed, including 200 primary cycles and all child processes reaped.
- External updater PowerShell tests: 7 passed, 0 failed.
- PowerShell parser validation: exit code 0.
- WebGUI: 9 tests passed; production build passed.
- Curated release packaging contract: passed for stable payload staging.

## Manual validation still open

The remaining real hardware and clean-VM checks are listed only in `TODO_CURRENT.md`, `MANUAL_TEST_CHECKLIST.md`, and `docs/install_test_checklist.md`.

## Productive path

- `src/python/tray.py --tray` is the productive Windows runtime.
- `src/python/gui.py` is the tkinter settings and legacy clipboard GUI.
- `webgui/` is the React/Vite settings UI and diagnostic overlay shell.
- Rust (`src/service`, `src/viewer`) remains experimental.
