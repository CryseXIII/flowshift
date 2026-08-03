# FlowShift Repository Agent Rules

## 1. Scope and authority

These rules apply to the complete FlowShift repository unless a more specific
nested `AGENTS.md` exists.

The actual repository state, productive code paths, tests, Git history, and CI
results are the source of truth.

Do not rely blindly on old plans, completion messages, TODO files, handoff
documents, or comments when they conflict with the actual code.

Never begin a later implementation phase unless the user explicitly requested
it.

Phase-specific requirements belong in `docs/phases/` and are referenced by
`TODO_CURRENT.md`.

## 2. Productive architecture

The productive Windows runtime starts through:

`src/python/tray.py --tray`

Preserve existing productive functionality, including:

- peer discovery and reconnect behavior
- input forwarding and edge switching
- clipboard metadata and current-item semantics
- provider lifecycle
- clipboard cache and leases
- overlay host and IPC
- WebGUI
- installer and updater
- release packaging

Rust code is experimental unless a phase specification explicitly says
otherwise.

Do not replace the productive Python runtime with Rust.

## 3. Required session start

At the start of a new session or after compaction:

1. Run:
   - `git status --short`
   - `git branch --show-current`
   - `git fetch --all --prune`
   - `git pull --ff-only`
   - `git rev-parse HEAD`
   - `git log -10 --oneline`
   - `cat VERSION`
2. Read:
   - `AGENTS.md`
   - `TODO_CURRENT.md`
   - `HANDOFF_CURRENT.md`
   - the active phase specification referenced by `TODO_CURRENT.md`
3. Inspect the productive code paths affected by the active task.
4. Do not repeat a full repository audit unless the task or discovered evidence
   requires it.

Preserve all existing uncommitted user work.

## 4. Work style

Work in small, complete, logical slices.

For every slice:

1. inspect the relevant productive path;
2. implement the full lifecycle, not only isolated helpers;
3. add or update meaningful tests;
4. run focused tests;
5. inspect the diff;
6. update `VERSION`;
7. update open task and handoff state;
8. commit;
9. push;
10. continue with the next open slice.

Do not stop after every commit to ask whether work should continue.

Do not implement unrelated cleanup or speculative architecture.

Do not add dead APIs, unused protocol types, placeholder implementations, or
tests that only exercise mocks while bypassing the productive path.

Do not silently change public behavior outside the active phase.

## 5. Testing policy

Use three test levels.

### During implementation

Run only:

- tests for the changed modules;
- directly adjacent integration tests;
- compile or build checks relevant to the changed files.

Do not run the complete project regression after every small edit or commit.

### After a major logical slice

Run:

- the complete affected subsystem tests;
- relevant worker, network, reconnect, persistence, or WebGUI integration tests.

### Before a stable release

Run the complete repository regression using the exact commands expected by CI,
including:

- full Python test discovery;
- relevant stress suites;
- worker and end-to-end checks;
- reconnect and overlay tests;
- updater and packaging tests;
- PowerShell parsing;
- WebGUI tests and production build;
- security or dependency gates required by the release workflow.

A stable release is not complete until the tag-triggered GitHub Actions workflow
is successful and the expected release assets have been verified.

Never weaken, skip, or delete a valid test merely to obtain a green result.

Tests for critical semantics must assert concrete end states.

Weak checks such as these are insufficient for central behavior:

- `processed > 0`
- `result is not None`
- conditional assertions that pass when the expected object disappeared
- checking only that no exception occurred

For important fixes, consider whether the test would fail if the original bug
were intentionally reintroduced.

## 6. Keep command output out of model context

Do not paste complete successful test logs into the conversation.

For large commands:

- redirect full output to an ignored temporary file or operating-system temp
  directory;
- inspect the exit code;
- show only the summary;
- on failure, inspect and report only the relevant failing sections.

Do not commit generated test logs.

Prefer targeted searches, diffs, and line ranges over repeatedly reading entire
large files.

## 7. Versioning and commits

The repository contains a central `VERSION` file.

Every own commit must increment `VERSION` exactly once in the same commit.

During an active target release `X.Y.Z`, development commits use:

`X.Y.Z-dev.N`

Each own commit uses the next unused `dev.N`.

No two own commits may use the same version.

Do not create stable tags for development versions.

Only the final release commit changes `VERSION` to the stable version.

Push every completed, tested logical commit.

Use focused commit messages.

Do not create unrelated `misc` or `cleanup` collection commits.

Never:

- amend already pushed public commits;
- force push;
- rewrite published history;
- move an existing release tag.

## 8. TODO_CURRENT.md

`TODO_CURRENT.md` contains only currently open work.

Do not retain completed tasks as:

- `[x]`
- completed sections
- historical task lists

Remove completed tasks entirely.

It must identify:

- the active phase;
- the referenced phase specification;
- currently open implementation tasks;
- genuinely open manual tests;
- the next planned phase.

After a phase is complete, remove its implementation tasks and mark no active
implementation phase.

Do not begin the next phase automatically.

## 9. HANDOFF_CURRENT.md

`HANDOFF_CURRENT.md` is a current operational snapshot, not a historical diary.

Keep it concise and accurate.

It must contain as applicable:

- current version;
- current stable release;
- active phase;
- active phase specification;
- productive architecture;
- completed architecture relevant to future work;
- last pushed commits that already exist;
- last successful focused tests;
- open tasks;
- open manual tests;
- known limitations;
- next planned phase.

Do not list a commit's own unknown future hash inside that commit.

Report the final HEAD separately after committing.

Remove stale or contradictory statements.

## 10. Context and compaction policy

Keep the active model context small.

Do not repeatedly paste:

- complete source files;
- full historical prompts;
- full successful test logs;
- complete Git histories;
- unchanged documentation.

Use repository files as persistent memory.

Prefer a clean pushed commit boundary before compaction.

Before compaction:

1. finish or safely stop the current logical slice;
2. run the relevant focused tests;
3. commit and push completed work when appropriate;
4. update `TODO_CURRENT.md`;
5. update `HANDOFF_CURRENT.md` with:
   - current version;
   - last pushed commit;
   - completed slices;
   - open slices;
   - last successful tests;
   - any uncommitted files;
6. verify `git status --short`.

Compact when any of these conditions applies:

- the context indicator is approximately half full or higher;
- two or three substantial logical slices have accumulated;
- a large new implementation area is about to begin;
- a major test or release stage is about to begin;
- the agent begins repeating or losing prior decisions.

When supported by the client, execute:

`/compact`

If the slash command can only be invoked by the user, stop at the nearest safe
boundary and output only:

`Bitte jetzt /compact ausführen. Danach setze ich anhand von AGENTS.md, TODO_CURRENT.md und HANDOFF_CURRENT.md fort.`

After compaction, perform the required session-start procedure again and
continue without asking for a new technical briefing.

Start a fresh OpenCode session for every new major phase and after every stable
release.

## 11. Dependency and toolchain policy

When a task updates dependencies or toolchains:

- determine the newest stable supported release from authoritative sources;
- do not use alpha, beta, RC, nightly, or canary versions;
- prefer the newest active LTS for production tools where applicable;
- test major upgrades fully;
- pin or lock the exact tested release state;
- do not use uncontrolled floating dependencies in a stable release;
- use automated dependency update PRs rather than silently changing production
  versions at runtime.

The normal end-user installer must not install development tools that are not
required at runtime.

## 12. Security and data safety

Treat all remote protocol data as untrusted.

Validate:

- message sizes;
- integer bounds;
- paths;
- filenames;
- IDs;
- offsets;
- state transitions;
- frame sizes;
- file counts;
- storage limits.

Do not expose private absolute local paths in normal API responses or logs.

Do not delete user data, caches, configuration, transfer journals, or local
changes without a documented and tested lifecycle.

## 13. Release rules

Before a stable release:

1. complete the full implementation definition of done;
2. run the complete regression;
3. update documentation;
4. clean `TODO_CURRENT.md`;
5. reconcile `HANDOFF_CURRENT.md`;
6. create and push the stable release commit;
7. create and push the immutable version tag;
8. wait for GitHub Actions;
9. verify the workflow status is `SUCCESS`;
10. verify all required release assets;
11. verify the update manifest and hashes;
12. verify the release is the expected latest stable release.

Never claim a release is complete because the workflow “should” succeed.

Verify the actual result.

After a successful stable release, stop.

Do not automatically start the next phase.

## 14. Stop conditions

Continue autonomously until one of these conditions occurs:

1. the explicitly assigned stable release is published and fully validated;
2. a real technical blocker prevents further work;
3. a hard usage or context limit is reached and compaction is unavailable.

For a blocker or hard limit, report only:

- last pushed commit;
- current `VERSION`;
- `git status --short`;
- uncommitted files;
- completed slices;
- remaining slices;
- last successful tests;
- exact blocker.

Do not represent an intermediate commit as phase completion.

## 15. Communication

Keep progress messages concise.

Do not narrate every file read or successful command.

Report:

- completed logical slice;
- commit hash;
- version;
- relevant tests;
- genuine risks or blockers.

Do not ask “shall I continue?” while open tasks remain.
