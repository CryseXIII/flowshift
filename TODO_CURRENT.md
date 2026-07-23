# FlowShift - Open Development Tasks

## Current phase

Phase 2.3 – Final Clipboard Closure

Every commit increments `VERSION` exactly once. Dev versions are `0.5.3-dev.N`.
The final fully validated release commit changes `VERSION` to `0.5.3` and tags `v0.5.3`.

Phase 3 (Clipboard Transfer Hardening) must not start until Phase 2.3 is released.

## Remaining open tasks

- [ ] global cache enforcement: active transfer protection uses correct store
- [ ] global cache accounting: freed bytes, not remaining, subtracted
- [ ] global cache enforcement: honest diagnosis when limit cannot be satisfied
- [ ] content-addressed dedup: shared SHA counted once across stores
- [ ] provider state import: remote state respected (not blindly set to available)
- [ ] event stress split: overflow, throughput, normal — real metrics
- [ ] announcement stress: real byte-transfer counters, strong assertions
- [ ] protection tests: pinned, active transfer, lease, local current
- [ ] full regression + v0.5.3 release

## Future phases (not started)

### Phase 3: Clipboard Transfer Hardening

- Batch manifest transfers.
- Disk peak planning.
- Reduced HDD write amplification.
- Persistent resume state across runtime restarts.

### Phase 4–8: React Overlay, Command Wheel, Shell Integration
