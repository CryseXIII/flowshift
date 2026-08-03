# FlowShift - Current State

## Release state

- Current version: `0.6.0-dev.1`.
- Current stable release: `v0.5.4`.
- Active implementation phase: Phase 3 - Clipboard Transfer Hardening.
- Active phase specification: `docs/phases/phase_3_clipboard_transfer_hardening.md`.
- Phase 3 implementation has not started.
- The immutable `v0.5.3` tag remains unchanged; its release workflow failed.

## Agent structure

- Permanent rules live in `AGENTS.md`.
- The active phase is tracked in `TODO_CURRENT.md`.
- The active phase specification is referenced from `TODO_CURRENT.md`.
- `HANDOFF_CURRENT.md` is the compaction checkpoint and operational snapshot.
- Run full repository regression only before a stable release.
- Use focused tests during individual slices.

## Current setup state

- Repository agent rules are now rooted at `AGENTS.md`.
- Phase documentation is organized under `docs/phases/`.
- Next step: toolchain audit and baseline.
- Focused tests during slices; full regression only before stable release.

## Productive path

- `src/python/tray.py --tray` is the productive Windows runtime.
- `src/python/gui.py` is the tkinter settings and legacy clipboard GUI.
- `webgui/` is the React/Vite settings UI and diagnostic overlay shell.
- Rust (`src/service`, `src/viewer`) remains experimental.

## Verified baseline

- Last successful focused tests before this setup commit: v0.5.4 release validation, including exact Python discovery, worker smoke, reconnect stress, overlay stress, updater tests, PowerShell parsing, WebGUI tests, production build, and release asset verification.

## Open work

- Supply the full Phase 3 specification document.
- Keep the existing manual hardware and VM checks open in `TODO_CURRENT.md`.

## Next planned phase

- Phase 3 - Clipboard Transfer Hardening, after the full specification is supplied.
