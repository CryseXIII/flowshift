# Toolchain and Dependency Policy

## Production Baseline

FlowShift release builds use CPython 3.15.1, Node.js 24.18.1 LTS,
npm 12.0.2, Pillow 13.0.0, pywebview 6.2.1, React 19.2.8, Vite 8.3.5,
and Vitest 4.1.12. Node.js 26.5.1 is a compatibility-test lane and is not
required on end-user systems.

The productive Windows runtime supports 64-bit CPython 3.11 through 3.15.
FlowShift reuses a compatible existing interpreter by default. It never claims
ownership of that interpreter, including after an explicitly requested update.
Automatic installation of missing Python downloads the official CPython 3.15.1
x64 installer from python.org and verifies its published SHA-256 before running
it. FlowShift uninstallers only offer to remove a prerequisite that the
installation state proves FlowShift installed.

## Locks and Reproducibility

`requirements.in` contains exact direct Python dependencies.
`requirements.txt` and `requirements-audit.txt` are generated with
`pip-compile --generate-hashes` and lock their complete transitive graphs and
artifact hashes. Runtime installation uses `pip --require-hashes`.
`webgui/package-lock.json` is lockfile version 3 and npm
installs it with `npm ci`; all direct package versions and npm itself are exact.

Releases are built on the own server without GitHub Actions
(`scripts/release_build.py`, driven by `scripts/publish_github_release.py
assets`). Its toolchain is pinned by SHA-256: Wine 10.0 (Debian trixie
packages), Inno Setup 6.7.4, Node.js 24.18.1 and npm 12.0.2. The build runs the
Python unit tests and the npm security audit, builds WebGUI with the production
Node LTS, stages a curated payload, and binds release metadata to the resulting
installer hash. Updating a tool means updating its pin in
`scripts/release_build.py` (and `TOOLCHAIN_ID`, which reinstalls the cached
toolchain). The former tag workflow `.github/workflows/release.yml` only runs
by hand. The packaged installer contains prebuilt
WebGUI assets, so normal users do not install Node.js, npm, Vite, or development
dependencies.

## Update Strategy

Dependabot checks GitHub Actions, npm, and Python dependencies weekly. A weekly
and pull-request workflow runs dependency audits and exercises WebGUI on both
the pinned active LTS and Current Node releases. Updates use stable releases
only. Major upgrades require focused tests before merge; production pins change
only after those tests pass. Release locks never use floating versions.
