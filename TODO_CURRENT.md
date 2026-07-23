# FlowShift - Open Development Tasks

## Current phase

Phase 2.3 – Final Clipboard Closure — **completed v0.5.3**

Phase 3 (Clipboard Transfer Hardening) may now begin.

## Completed tasks

- [x] global cache enforcement: active transfer protection uses correct store
- [x] global cache accounting: freed bytes, not remaining, subtracted
- [x] global cache enforcement: honest diagnosis when limit cannot be satisfied
- [x] content-addressed dedup: shared SHA counted once across stores
- [x] provider state import: remote state respected (not blindly set to available)
- [x] event stress split: overflow, throughput, normal — real metrics
- [x] announcement stress: real byte-transfer counters, strong assertions
- [x] protection tests: pinned, active transfer, lease, local current
- [x] full regression + v0.5.3 release

## Future phases (not started)

### Phase 3: Clipboard Transfer Hardening

- Batch manifest transfers.
- Disk peak planning.
- Reduced HDD write amplification.
- Persistent resume state across runtime restarts.

### Phase 4–8: React Overlay, Command Wheel, Shell Integration
