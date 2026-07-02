# FlowShift Current State

## What Changed
- `src/python/tray.py` is the productive runtime path.
- Unsafe process-kill cleanup was removed.
- Shutdown is now event-driven and socket/thread loops stop cleanly.
- `status` now reports `running` and `shutting_down`.
- Mouse move and `status`-poll logs are rate-limited.
- `src/python/gui.py` now uses runtime state instead of `service_proc` as truth.
- `src/python/test_service.py` covers peer canonicalization and mouse scaling.
- `MANUAL_TEST_CHECKLIST.md` documents the remaining hardware checks.

## Important Decisions
- Use `tray.py --tray` as the only productive runtime entrypoint.
- Keep `service.py` as a legacy wrapper only.
- Canonical peer identity: `device_id` first, then endpoint fallback.
- GUI close should not kill the runtime.

## Verification
- `python -m py_compile src/python/tray.py src/python/gui.py src/python/e2e_test.py src/python/service.py src/python/test_service.py`
- `python src/python/test_service.py`

## Next Manual Checks
- Start/stop/restart on both machines.
- Ping each peer and verify reconnect behavior.
- Test mouse forwarding on the target screen.
- Confirm logs stay readable under mouse movement and status polling.
