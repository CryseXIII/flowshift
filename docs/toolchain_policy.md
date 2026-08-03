# Toolchain and Dependency Policy

## Production Baseline

FlowShift 0.6.0 release builds use CPython 3.14.6, Node.js 24.18.1 LTS,
npm 12.0.2, Pillow 12.3.0, pywebview 6.2.1, React 19.2.8, Vite 8.2.0,
and Vitest 4.1.10. Node.js 26.5.1 is a compatibility-test lane and is not
required on end-user systems.

The productive Windows runtime supports 64-bit CPython 3.10 through 3.14.
FlowShift reuses a compatible existing interpreter by default. It never claims
ownership of that interpreter, including after an explicitly requested update.
Automatic installation of missing Python uses the stable Python 3.14 winget
package. FlowShift uninstallers only offer to remove a prerequisite that the
installation state proves FlowShift installed.

## Locks and Reproducibility

`requirements.in` contains exact direct Python dependencies.
`requirements.txt` and `requirements-audit.txt` are generated with
`pip-compile --generate-hashes` and lock their complete transitive graphs and
artifact hashes. Runtime installation uses `pip --require-hashes`.
`webgui/package-lock.json` is lockfile version 3 and npm
installs it with `npm ci`; all direct package versions and npm itself are exact.

The release workflow pins GitHub Actions to immutable commit SHAs, runs Python
and npm security audits, builds WebGUI with the production Node LTS, validates
the complete repository, stages a curated payload, and binds release metadata
to the resulting installer hash. The packaged installer contains prebuilt
WebGUI assets, so normal users do not install Node.js, npm, Vite, or development
dependencies.

## Update Strategy

Dependabot checks GitHub Actions, npm, and Python dependencies weekly. A weekly
and pull-request workflow runs dependency audits and exercises WebGUI on both
the pinned active LTS and Current Node releases. Updates use stable releases
only. Major upgrades require focused tests before merge; production pins change
only after those tests pass. Release locks never use floating versions.
