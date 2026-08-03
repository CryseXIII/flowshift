# FlowShift - Current State

## Release state

- Current version: `0.6.0-dev.4`.
- Current stable release: `v0.5.4`.
- Active implementation phase: Phase 3 - Clipboard Transfer Hardening.
- Active phase specification: `docs/phases/phase_3_clipboard_transfer_hardening.md`.
- Phase 3 toolchain and dependency modernization is complete.
- The productive legacy clipboard path and binding V2 target architecture are
  documented in `docs/clipboard_transfer_v2.md`.
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

## Open work

- Implement capability negotiation, the schema-2 session/item model, batch
  manifest, and centralized remote path validation.
- Implement the remaining Phase 3 transfer slices and release `v0.6.0`.
- Keep the existing manual hardware and VM checks open in `TODO_CURRENT.md`.

## Next planned phase

- Phase 4 is not started and must not begin automatically.
