# FlowShift - Current State

## Release state

- Current version: `0.7.2-dev.5` (`0.7.2-dev.3` and `0.7.2-dev.4` are
  taken by open tasks: Tailscale discovery, update-box buttons; backport of
  a release into Forgejo `main`
  is part of the publication, Viktor 25.11. 20:30). Tooling only, no change
  to the program. `scripts/publish_github_release.py`: new step `ruecktrag
  --version X.Y.Z` (branch `task/release-vX.Y.Z-nach-main` from
  `origin/main` + `git merge -s ours --no-edit origin/release/vX.Y.Z` in a
  temporary worktree, checks empty diff and the release commit as ancestor,
  pushes only that new branch to Forgejo, never forced, no tags, nothing to
  GitHub; opens or reuses the PR through the Forgejo API with git's
  credential for `origin` (`ForgejoPulls`, Basic auth, never printed), else
  prints the compare link; says to merge it as a merge commit). `publish`
  runs it right after the pushes, `assets` again after `verify` (reuses
  branch and PR); an existing branch that is not a content-neutral merge of
  the release stops it (never forced). `plan` and `verify` print one
  sentence "Backport of vX.Y.Z: done / still missing ..." (`verify` only as
  information); the BLOCKED hint names `ruecktrag`. Bot runs keep their
  `git merge` ban; the script runs from Claude at the console only.
  `flowshift-github release` passes every subcommand through, so no infra
  change. `test_publish_github_release.py` 44 tests green (dry run with
  stand-in Forgejo/GitHub repos: diff empty, release commit ancestor, `plan`
  without BLOCKED after the PR merge, reuse, foreign branch not forced).
  The `v0.7.0` backport itself is already merged (PR #44). Merged with
  `main` (`0.7.2-dev.1` and `0.7.2-dev.2`, below); no code overlap. After
  the merge `test_peer_hand_entered` "first link fills in the entry" was red
  here (read `config.json` before `save_config` had written it, outside the
  lock); the test now waits for the file, program code unchanged (`main`
  fixed the same test the same way). Program fix found by the full smoke:
  `test_clipboard_stress_v2` "cancellation storm" left one staging folder
  under `incoming/` now and then. Cause: the V2 session registry was
  cleaned up by transfer id only; when a late `resume_request` had already
  registered a fresh receiver stage under the same id, purging the old stage
  (`acknowledge_stream_v2_cancel`, maintenance, publish, discard) dropped
  the fresh record too, so no preflight timeout ever removed its folder.
  `ClipboardManager.unregister_stream_v2_session(transfer_id, stage)` now
  only removes the record of that very stage; regression test in
  `test_clipboard_transfer_control_v2.py`.
  Before: `0.7.2-dev.2` (target release `v0.7.2`; tag `v0.7.1`
  exists, release commit `8ec6bb0` on `release/v0.7.1`). Event Log page
  shows warnings and errors (Viktor 26.11. 10:47: a refused edge switch stood
  only in `flowshift.log`). New `src/python/event_log.py`: `tray.log` records
  every INFO/WARN/ERROR line (not DEBUG, not `web_api: ...` access lines) in
  `event_log.BUFFER` (last 500, text cut at 4000 chars) and publishes it as
  SSE `{"type": "log", seq, time, level, text}` (`web_api.publish_log_entry`,
  outside the bus replay history so nothing shows twice). `GET /api/logs
  [?after=seq]` returns the buffer plus `log_folder`/`log_file`; `POST
  /api/logs/open-folder` (guarded) opens the data folder in Explorer
  (`tray._open_log_folder`). An SSE subscriber dropped after a full queue
  now ends its stream so the browser reconnects; the page then reloads
  `/api/logs?after=`. WebGUI `EventLog.jsx`: loads the buffer on open, after
  Resume and after a reconnect, colours WARN/ERROR, level filter "Show
  level" (default INFO and up; "Everything" adds the internal events),
  plain-words sentences from `webgui/src/logExplain.js` (edge refusals by
  reason, no peer for an edge, connection failed, ping failed, web API
  errors, crashed workers, ports, hotkeys), button "Open log folder" with
  the path. The Dashboard edge notices of `0.7.2-dev.1` are unchanged.
  Packaging: `event_log.py` added to `packaging/build_release.ps1`,
  `packaging/test_release_packaging.ps1` and `$RequiredProgramFiles` in
  `install_flowshift.ps1` (update and USB package read that list; the
  uninstaller removes the program folder as before, no new data). Tests:
  `test_event_log.py` (buffer, `/api/logs`, open-folder guard, real
  `tray.log` path), `test_web_api_request_guard.py` (route inventory),
  `test_peer_hand_entered.py` (waits for the saved config instead of
  reading it at once: it failed when another test had imported `tray`
  first); WebGUI `EventLog.test.jsx` (not run here, no vitest).
  Before: `0.7.2-dev.1`. Viktor's
  `flowshift.log` of 26.11. (two Windows PCs, same USB package), three bugs:
  1. Screen edge `invalid_target` with a peer typed in by IP (Scan Network
     found nothing, so no `device_id`): the source sent its own config key
     `endpoint:192.168.8.18:45781` as `target_identity`, the target compared
     it with `device:<own id>`. Now (a) the source sends the identity of the
     link (`_link_wire_identity`: `device:<device_id>` from the hello);
     (b) the first hello of a link to that address/port or the first pong
     fills `device_id` and the announced `display_name` into the entry
     (`name` stays the label), sets `identity` to `device:<id>` and moves
     edge assignments and hotkey actions there, then saves and reloads the
     hotkeys (`rm.adopt_peer_device_id`, `tray._learn_peer_device_id`; the
     connector restarts once under the new token); at start entries that
     already have a `device_id` but endpoint references are moved
     (`rm.migrate_peer_identities` in `load_config`); (c) the target also
     accepts `endpoint:<host>:<port>` when the port is its listen port and the
     host the local address of the link (or loopback), for `0.7.1` sources
     (`rm.edge_target_matches`); (d) the last refused edge is kept in
     `/api/status` `edge_switching.last_rejection` and shown as a plain-words
     notice on the Dashboard (`EdgeRejectionNotice`, cleared by the next
     accepted switch); (e) paired = the rule of the update from a connected
     PC (`peer_update.trusted_peer`: link `device:<id>` and an entry with that
     device ID) also for the remote preview (`preview_authorization(meta,
     paired_peer)`): a paired peer now gets `preview_unavailable` instead of
     `pairing_required` (stage 1 still has no picture; stages 2-4 are not
     built). The update from a connected PC needed no change: it works for a
     typed-in entry as soon as the device ID is filled in.
  2. `GET /api/hotkeys` 500 (`UnboundLocalError` for `it`): a loop variable
     `it` in the clipboard items branch of `do_GET` hid `input_trigger as
     it` for the whole handler; renamed to `raw_item`.
  3. `flowshift.log` 19 MB: `tray.log` rotates at 5 MB to `.1`-`.3`
     (`rm.rotate_log_file`, about 20 MB at most); the web API no longer logs
     successful GET/HEAD/OPTIONS requests (`web_api._quiet_request`), POST,
     DELETE and every status from 400 still are.
  Tests: `test_peer_hand_entered.py` (peer without `device_id` through the
  real `peer_handler`, edge north, target check, Dashboard rejection,
  pairing for update and preview, rotation), `test_web_api_hotkeys.py`
  (Viktor's Ctrl+Alt+1 / Ctrl+Alt+0, quiet log), `test_remote_preview.py`,
  `test_tray_remote_preview.py`; WebGUI `Dashboard.test.jsx`,
  `RemotePreviewSection.test.jsx` (vitest not run here). No installer change:
  no new file, rotated logs live in the data folder that `-PurgeData`
  already removes.
  Merged with `main` (`0.7.1-dev.8`, below), which fixed the same edge
  bug in parallel: both paths stay. The target accepts its own
  `endpoint:` address via `rm.identity_is_self` (local IPv4s) or
  `rm.edge_target_matches` (address of the link); after a hello or pong
  `tray._learn_peer_device` (main: device ID and screen, now also reloads
  the hotkeys) runs first, then `tray._learn_peer_device_id` (announced
  name, further ports). Status carries `last_failure` and
  `last_rejection`; the Dashboard shows `EdgeSwitchNotice` and only without
  `last_failure` the older `EdgeRejectionNotice`, so one hint per refusal.
  Before (from `main`): `0.7.1-dev.8` (scan finds nothing in the same network,
  edge switch to a peer typed in by IP does nothing; Viktor 26.11., two
  Windows PCs). Scan: new `src/python/lan_scan.py`, one UDP socket per IPv4
  adapter bound to its address, sending to the real broadcast address from
  the prefix length (`Get-NetIPAddress` `PrefixLength`/`InterfaceAlias`,
  `tray.get_local_ipv4_networks`; /24 only when the mask is unknown,
  `mask_known` false) and to 255.255.255.255; /31 and /32 adapters
  (Tailscale) are listed as `point_to_point` and skipped; wait 3 s. The
  request carries `reply_port` (the FlowShift port): `discovery_thread`
  replies to the source port and also to that port on the same host, and
  hands a `discover_reply` arriving on 45781 to `lan_scan.INBOX` while a
  scan runs. The installer already allows TCP and UDP 45781 inbound with
  `-Profile Any`, so the reply passes the firewall. `/api/peers/scan`
  returns `adapters` and `adapters_text`; Peers shows "Searched via: ...".
  The address field on an empty scan is part of
  `flowshift-peer-finden-ueber-vpn` (`0.7.1-dev.7`, its own branch). Edge:
  a peer typed in by address is `endpoint:<ip>:<port>`; the target refused
  that `edge_enter` as "foreign target". Now
  `runtime_model.identity_is_self` accepts its own address and port, and
  `rm.learn_peer_device` (`tray._learn_peer_device`, after a successful
  ping and after the hello of a link) stores `device_id`, `screen` and the
  name and moves edges and hotkeys to `device:<id>` (the connector restarts
  once). A triggered but dropped switch (no peer for the edge, connection
  failed, send failed, refused, no answer) is
  `status.edge_switching.last_failure` and the Dashboard notice "Edge switch
  did not happen" for two minutes. Tests: `test_lan_scan.py`,
  `test_manual_peer_edge.py`, `Dashboard.test.jsx` (vitest, not run by the
  test runner).
  Before: `0.7.1-dev.6` (last red WebGUI test of the `0.7.1`
  trial build from `main` `95a668e`, 25.11. 20:24 UTC: 1 of 145 red,
  `SoftwareUpdateSection.test.jsx` "offers the browser-saved Python
  installer after a failed python.org download"). Not flaky, a real bug:
  the merge of `flowshift-usb-paket-python-pruefsumme-falsch` round 2
  (`1922b98`) gave the button "Use downloaded Python installer" in
  `UsbPackagePanel.jsx` an `aria-label` ("Use the Python installer saved in
  the Downloads folder") that replaced its visible text as accessible name,
  so screen readers and voice control ("click Use downloaded ...") did not
  find it by its label (WCAG 2.5.3). The `aria-label` is gone, the `title`
  stays as tooltip; the test is unchanged. The test runner runs no vitest,
  so only the release build saw it.
  Before: `0.7.1-dev.5` (no code change: `v0.7.0` is published,
  `releases/latest` = `v0.7.0` since 25.11. 18:09 UTC, and its release
  commit `52f3e56e` (`release/v0.7.0`, GitHub `master`) is an ancestor of
  Forgejo `main` through `git merge -s ours` (merge `4ea0b5c5`, content of
  `main` unchanged). `flowshift-github release plan` no longer blocks: it
  shows `v0.7.1` as next publication and waits only for Viktor's approval
  (given 25.11. 19:12). Live check readable by the Leitstand without a
  GitHub token: `ap git flowshift diff --quiet
  origin/main...52f3e56e75141eb89c6446d38f6b6a8ee130b814` exits 0 only when
  the release commit is in `main`; `ap befund` is no check for this, it only
  reads reports of host tools under `/srv/opencode/befunde`).
  Before: `0.7.1-dev.4` (course and display of an update of a
  connected PC, Viktor 25.11. 17:34). `peer_update.py`: the receiver sends
  `peer_update_status` over the link to the PC of the offer on every change
  (state, transfer percent, `wait_reason`, error, `failed_attempts`, `paused`;
  `restarting` as the last report before the shutdown) and, after its restart,
  once per process `updated` or `failed` with the updater's reason
  (`on_peer_linked`, only for the handoff record of the last day with
  `peer_identity`). `PeerUpdateMonitor` on the sender turns hello versions,
  link end and these reports into one phase per peer (`older`, `offered`,
  `transferring`, `interrupted`, `ready`, `waiting`, `installing`,
  `restarting`, `updated`, `failed`, `update_only`; restart timeout 5 min ->
  `failed`/`no_return`; `failed`/`not_updated` only when the peer was
  disconnected while `installing`/`restarting` and comes back with the old
  version, a second hello of the other link direction keeps `waiting`/`ready`), sends tray messages at the start and when the peer is
  back, and refuses a switch (edge and hotkey/Dashboard activation) to a peer
  that installs or restarts with "<PC> is being updated ..." (tray message,
  `_tell_switch_blocked`). The receiver keeps waiting while it is controlled
  (`_runtime_install_safety`: `forwarding_active`, `edge_session_active`; the
  sender shows "waits until you are back on this PC"). No update loop: the
  handoff record counts `attempts`; after `MAX_AUTOMATIC_ATTEMPTS` (2) failed
  installs of the same version automatic installs pause (message on both PCs,
  Install by hand still works). Hello carries `update_protocol`;
  `peer_update.link_mode` marks a link with another `protocol_version` as
  `update_only` (link stays up for hello/offer/status/package, switching is
  refused). Status rows (`/api/status` peers) carry `update` and `link_mode`;
  WebGUI `Dashboard.jsx` `PeerUpdateState` per row and `PeerUpdateNotice`.
  Smaller updates: `install_flowshift.ps1` `Get-ReusableVenv` copies the
  previous `.venv` (from `FLOWSHIFT_PREVIOUS_INSTALL` = the runner's rollback
  folder, or `FlowShift.previous`) when `requirements.txt` is byte-identical
  and the venv was built on the same Python; no pip run then; any doubt ->
  fresh venv as before. The peer package itself was already only program +
  WebGUI (about 2-3 MB).
  Before: `0.7.1-dev.3` (the 7 red WebGUI tests of the `v0.7.0`
  build and the Shift-click range, task `flowshift-071-tests-und-umschaltklick`).
  Each red test sorted: `CommandWheelSection.test.jsx` 2 outdated (the
  modifier boxes are those of the shared hotkey field since dev.49 and are
  named `wheel hotkey modifier Alt`, not `wheel modifier Alt`; the boxes
  exist): test uses the real name. `SettingsPanel.test.jsx` 3 outdated: 2
  look for the save error once while it stands on purpose at the top and
  next to the button (dev.51), now checked in `settings-save-result` and
  counted twice; 1 expected `mods: 5` for a cleared peer row after a
  recorded Ctrl+side button (the row keeps `mods: 1`, `key: 0`, no mouse,
  so nothing is registered), now `mods: 1`. `SoftwareUpdateSection.test.jsx`
  1 real: the channel list said "Development (Forgejo main, ...)" on a
  Stable PC although the checklist says no Forgejo text anywhere; the option
  is now "Development (every merged version, own network only)".
  `ClipboardOverlay.test.jsx` 1 real bug, also in WebGUI `/clipboard`:
  `useClipboardSelection.check` read `anchorRef` inside the state updater,
  which React may run only at the next render, when the anchor already is the
  Shift-clicked item; so only both ends were checked. The anchor is now read
  before it moves. Proof: `clipboardSelection.test.js` (both directions,
  both clicks in one React batch, which fails with the old code; range within
  the shown tab), `ClipboardView.test.jsx` (Shift-click range on the page),
  the existing overlay test. New `publish_github_release.py build --version
  0.7.1 --vom-main`: release build of Forgejo `main` as `0.7.1` before the
  approval (VERSION only in the temporary worktree, no tag, no upload, every
  check strict); `test_publish_github_release.py` 33 tests green. Vitest did
  not run here (no `node_modules`, `npm ci` not allowed in this run).
  Before: `0.7.1-dev.2` (update from a connected device, Viktor
  25.11. 16:50: "the client with the lower version is updated and restarted by
  the client with the higher version"). New `src/python/peer_update.py`: after
  the hello the PC with the higher `app_version` sends `peer_update_offer`
  (version, size, SHA-256 of `FlowShift-Peer-<version>.zip`) over the link,
  but only to a device in its own `peers` list with device ID; the package is
  built once on demand into `ProgramData\FlowShift\updates\peer_outbox`
  (`usb_package.collect_program`: program files + stamped WebGUI as
  `webgui/dist`, no offline Python files, `peer_package.json` with size and
  SHA-256 of every file). The receiver (`PeerUpdateController`) takes offers
  only from links whose identity is `device:<id>` of its own device list,
  only versions above the installed one (`accept_decision`: no downgrade, no
  build metadata, `-dev` on Stable only with `updates.from_peers.
  allow_development`), and follows `updates.from_peers.policy` (`notify`
  default / `download` / `install`, `config_schema.DEFAULT_PEER_UPDATES`).
  It dials the address from its own device list (never the offer's source
  address) on the existing FlowShift port with `peer_update_channel_hello`
  (first framed message, dispatched in `peer_handler` like the clipboard
  data-channel hello; no new port); the sender answers only for devices in
  its list and with its own device ID, which must match. 512 KiB base64
  chunks into `updates\downloads\FlowShift-Peer-<version>.zip.part`, three
  tries, each continues at the saved byte; cancel deletes the part file.
  `verify_package` checks size and SHA-256 of the whole file, every ZIP entry
  against `peer_package.json` (nothing missing or extra, no unsafe path or
  link, no `offline/`), `VERSION` and `webgui/dist/VERSION` = offered version
  and the installer's `$RequiredProgramFiles`. Install: announced a minute
  ahead for `install` (tray + WebGUI, cancellable), waits until
  `_runtime_install_safety` is safe and no other update/installation runs,
  reserves the maintenance gate, then `update_handoff.handoff_peer_package`
  (plan `package_kind = peer`); `update_flowshift.ps1` unpacks it like a
  source archive and runs its `install_flowshift.ps1` (prebuilt WebGUI, no
  Node.js; Python packages from PyPI as for every non-USB install), same lock,
  rollback and health check. `updates\peer_update.json` keeps the last
  handoff for the panel after the restart. Web-API `GET /api/update/peer`,
  `POST /api/update/peer/settings|download|install|cancel`; WebGUI
  `PeerUpdatePanel.jsx` in Software Update (icon-only buttons `btn-icon
  btn-touch`, 44 px, `title` + `aria-label`: fa-download, fa-power-off,
  fa-xmark, fa-floppy-disk; test finds them by aria-label). Uninstaller removes
  `updates\peer_outbox` and `updates\peer_update.json`; `peer_update.py` in
  `$RequiredProgramFiles`, `packaging/build_release.ps1` and its test.
  Before: `0.7.1-dev.1` (release build without GitHub Actions,
  Viktor 24.11.: the GitHub account is locked for billing, nothing on GitHub
  may cost money; the `v0.7.0` tag push of 24.11. 22:55 started the
  workflow, GitHub cancelled it after 3 s). `main` carries the content of
  `release/v0.7.0` (only `VERSION` differed) and continues with
  `0.7.1-dev.1`. GitHub releases still work with the token:
  `flowshift-github release probe` created a draft (id 457407445) with one
  asset, read it back and deleted it, no tag left. New
  `scripts/release_build.py` + `publish_github_release.py` steps `build`
  (build and check, no upload, no token), `assets` (build from a clean tag
  checkout, updater validation, upload into a draft, read back and compare,
  publish as latest, `verify --download-setup`) and `probe`. Toolchain
  without root under `~/.cache/flowshift-release-tools`, all pinned by
  SHA-256: Wine 10.0 (Debian trixie wine64/libwine amd64+i386 PE DLLs,
  libunwind8, libz-mingw-w64), Inno Setup 6.7.4 (headless, Wine graphics
  driver `null`), Node.js 24.18.1 + npm 12.0.2; link `~/fsw-nls` for Wine's
  NLS tables. `.github/workflows/release.yml` runs by hand only.
  At that time `v0.7.0` was not published yet: the first real `assets --version 0.7.0` run (with
  Viktor's approval of 24.11. 21:54) built everything up to the WebGUI tests
  and stopped there: `npm test` 7 of 132 red in 4 files
  (`CommandWheelSection.test.jsx` 2: looks for the removed "wheel modifier
  Alt" checkbox; `SettingsPanel.test.jsx` 3: the hotkey conflict text now
  appears twice; `SoftwareUpdateSection.test.jsx` 1: `/Forgejo/` also matches
  the channel option; `ClipboardOverlay.test.jsx` 1: Shift-click range in the
  overlay selects only both ends, possibly a real bug). The WebGUI tests ran
  nowhere since the test runner has no Node. Nothing was uploaded,
  `releases/latest` is still `v0.6.3`. An evidence build of the same tag with
  the red vitest step only logged produced `FlowShift-Setup.exe` 2 683 335
  bytes (sha256 `aa1f25f5...`) that passes the updater validation (not
  uploaded). **`v0.7.0` is now published as it is** (Viktor 25.11. 16:50,
  variant a; approval for `v0.7.0` of 24.11. 21:54 still applies): new
  explicit option `--rote-webgui-tests-nur-protokollieren` of `assets` and
  `build` (never a default) runs `npm test` and, if vitest reports failed
  tests, writes `WARNUNG: WebGUI-Tests rot: N von M` into the build log and
  builds on; a vitest crash or a run without a summary of failed tests still
  stops, and `npm ci`/`audit`/`build`, payload rules, ISCC, manifest,
  SHA256SUMS, the updater validation and the draft read-back stay strict.
  Without the option a red `npm test` stops the build as before. Run:
  `/srv/opencode/autopilot/bin/flowshift-github release assets --version
  0.7.0 --freigabe "Viktor 24.11. 21:54 + 25.11. 16:50 Variante a"
  --rote-webgui-tests-nur-protokollieren`. The red WebGUI tests and the
  Shift-click range in the clipboard overlay are fixed as `0.7.1` (own task
  `flowshift-071-tests-und-umschaltklick`), which is also the real update
  test from `v0.7.0`; `v0.7.1` needs Viktor's approval for that version.
  Before: `0.7.0-dev.56` (USB package and source-only packages,
  Viktor 24.11. 22:38 on dev.53, second try: the Python download went through,
  then "The Python packages for Python 3.15 could not be downloaded: ERROR: No
  matching distribution found for proxy-tools==0.1.0"). `proxy-tools` (needed
  by pywebview) is on PyPI only as sdist, and the build asked pip for wheels
  only (`--only-binary=:all: --platform win_amd64`), so no package could ever
  be built; `test_usb_package.py` stayed green because its PyPI stand-in wrote
  wheels for every requirement. Now `usb_package.wheel_report` asks the PyPI
  JSON API for every line of `requirements.txt` and every Python 3.11-3.15
  which locked file (hash in `requirements.txt`) is a Windows amd64 wheel,
  before anything big is downloaded; a source-only package is built once with
  `pip wheel --no-deps --require-hashes --no-binary <name>` (sdist hash checked
  by pip) and must be `-none-any`, else those Python versions are skipped
  (bundled: stop, `wheels_missing`). `offline/requirements-offline.txt` =
  `requirements.txt` + the built wheel's SHA-256 (manifest `requirements`,
  `built_from_source`; `--check-package` compares it with `requirements.txt`);
  wheel sets are downloaded with it and `--find-links` to the built wheels;
  `install_flowshift.ps1` (`Read-UsbPackage` -> `Requirements`) installs with
  that file, `--no-index --find-links --require-hashes`. Messages name package
  and version and what to do (`wheels_missing`, `source_build_failed`,
  `pypi_unreachable`, `wheels_download_failed` with pip's "No matching
  distribution found for ..."). `usb_package.PIP_COMMAND` lets the PyPI test
  choose pip; without a pip of its own it puts the pinned pip wheel (SHA-256
  checked) on `PYTHONPATH` and starts `python -m pip` (environment patched
  for `_run_pip` and the test's own pip calls; `PipCommandTests` offline).
  Proof: `test_usb_package_pypi.py` against
  the real PyPI (report, real build, offline `pip install --no-index
  --find-links --require-hashes --platform win_amd64 --target` for 3.11-3.15,
  plain `requirements.txt` refused), `test_usb_package.py`
  (`SourceOnlyPackageTests`, `WheelTagTests`); one full real build with the
  real python.org installer: 90 MB ZIP, 49 offline files, `--check-package`
  ok, all five sets installed offline. Windows steps
  `docs/install_test_checklist.md` N3. The 22:29 "wrong SHA-256" was an
  incomplete first download (the 22:38 try passed). Before: `0.7.0-dev.55`
  (USB package Python download, Viktor
  24.11. 22:29 on dev.53: "Create USB package" ended with "The downloaded
  Python installer has the wrong SHA-256"; the pinned SHA-256 and size
  45555248 are right, checked against python.org). Cause found in the code:
  `default_fetch_python` copied the urllib response without comparing
  `Content-Length`; urllib ends a broken connection like a normal end, so a
  cut download was hashed as a whole file. Now `usb_package.py` counts size
  and SHA-256 while the bytes arrive (fsync, close, then rename with a short
  wait for a scanner lock), raises `incomplete` on a short body, records HTTP
  status, content type, redirect and proxy, tries twice
  (`DOWNLOAD_ATTEMPTS`), and the message names the cause (`incomplete`,
  `not_installer` = proxy/filter page, `changed_on_disk` = scanner,
  `http_error`, `network`/`timeout`, `wrong_bytes`), expected/received size
  and shortened SHA-256, HTTP/type and the `flowshift.log` path (one log line
  per try). Way out without dropping the check: the page links the
  python.org file and "Use downloaded Python installer" posts
  `{"python_installer": "downloads"}` (only accepted body, no path); the
  build takes `python-3.15.1-amd64*.exe` from Downloads only with the pinned
  SHA-256 and rechecks the copy. pip and the bundled Python's wheel set get a
  second try; a non-wheel file (proxy page) fails. `install_flowshift.ps1`
  has `$PythonInstallerSize` (message only). Proof: `test_usb_package.py`
  (FetchPython/DownloadPython/PythonFromDownloads/BuildFallback/Job tests),
  `test_web_api_updates.UsbPackageApiTests`; Windows steps
  `docs/install_test_checklist.md` N2. Before: `0.7.0-dev.54` (no code
  change: acceptance of the hotkeys-with-mouse-buttons follow-up checks code
  markers in the read copy instead of a fixed version number, see
  "Server-side live checks" below). Before: `0.7.0-dev.53` (USB stick
  package, Viktor 24.11.: "ein
  zip auf einen USB stick packen ... auf anderen PC's verteile und
  installiere"; the other PCs have no Git, Node.js or Forgejo access).
  Settings > Software Update on an installed PC with channel Development
  shows "Create USB package" (`UsbPackagePanel.jsx`,
  `GET/POST /api/update/usb-package`, empty POST body, 409 on another
  channel or while a build runs, 503 without the job). `usb_package.py`
  packs the installed `Program Files\FlowShift` (program, installer/updater/
  uninstaller scripts, `docs`, `src`; installed `webgui` as `webgui/dist`
  without the port `config.json`) plus `offline/`: the python.org installer
  with version and SHA-256 read from `install_flowshift.ps1`, `pip==26.2` and
  `pip download --only-binary=:all: --platform win_amd64 --require-hashes`
  wheel sets for every Python 3.11-3.15 PyPI has (the bundled 3.15 is
  mandatory), `usb_package.json` with every SHA-256. Work folder
  `%ProgramData%\FlowShift\updates\usb-build` (removed after the run and by the
  uninstaller), result `Downloads\FlowShift-<version>-usb.zip`.
  `install_flowshift.ps1` now copies itself, the `.bat` and
  `install_webgui.ps1` into the program folder (needed to pack it; an
  installation before dev.53 gets "install the current version once more"),
  and with `offline\usb_package.json` (`Read-UsbPackage`) checks every
  offline file, uses the bundled Python if no Python with a wheel set is
  found, installs pip and the requirements with `--no-index --find-links`, and
  sets `updates.channel` to stable via `usb_package.py --stable-channel`
  (config otherwise kept). Same lock, staging and `FlowShift.previous`
  fallback as every run. WebView2 is not bundled (clear message if missing and
  offline). Software Update on Stable says in one sentence that a newer
  installed version than GitHub needs nothing and nothing is downgraded
  (stable discovery already reports an older GitHub release as up to date).
  Setup.exe payload now has `install_flowshift.bat` and `usb_package.py`.
  Proof: `src/python/test_usb_package.py` (build, unpack, PATH with only
  Python, `--check-package`, install copy, Web-API start, `/api/status`
  version, `index.html`/`overlay.html`, channel stable) and
  `test_web_api_updates.UsbPackageApiTests`. Windows steps:
  `docs/install_test_checklist.md` section N. Before:
  `0.7.0-dev.52` (stable channel without Git, Node.js and
  Forgejo, Viktor 24.11.: other PCs update only from GitHub
  `releases/latest`. Fixed on that path: (1) the handoff refused every stable
  update of a PC with an installed prerelease (`0.7.0-dev.N` -> `0.7.0`:
  `invalid_release`); now only the target must be stable. (2) Stable checked
  only at start: a running PC with policy download/install never saw a new
  release; now every 6 h (`STABLE_CHECK_INTERVAL_SECONDS`, counted from the
  policy start when "check on start" is off). (3) After a switch from
  development to stable the page kept the Forgejo result/error: the snapshot
  now shows `idle` ("not checked yet") for a check result of the other
  channel and never a Forgejo-only error (`FORGEJO_ONLY_ERRORS`) on stable.
  (4) Software Update shows Check / Download / Install as a row of steps
  (done/running/failed/next) and after each click the outcome in plain words
  (`STABLE_ERROR_HINTS` for every stable error; restart notice during
  install). Publication Forgejo -> GitHub is one step,
  `scripts/publish_github_release.py` (plan / publish with `--freigabe` /
  verify), only with Viktor's approval; see `docs/setup.md` "Publishing a new
  version for all PCs"). Before: `0.7.0-dev.51` (one command wheel hotkey that is really
  saved, Viktor 24.11. on dev.50: changed or cleared wheel hotkey, "Save",
  F5 -> Ctrl+Right click again; "Add another hotkey" was not asked for.
  Cause, traced request by request: the page has two save buttons; Viktor's
  "Save" was "Save Settings" at the bottom, which posts only
  `POST /api/settings`, and that route ignores `command_wheel` since dev.50
  on purpose, answered "Settings saved." and left config.json unchanged.
  Earlier tests were green because the Python tests posted straight to
  `/api/actions/wheel` and `SettingsPanel.test.jsx` replaces the wheel
  section with an empty stub. Now "Save Settings" first saves a changed
  wheel through `POST /api/actions/wheel` (`CommandWheelSection` ref
  `saveIfChanged`) and shows both results next to the button; every save
  shows success or the error. `command_wheel` version 3 has exactly one
  `hotkey` (no `extra_hotkeys`, no `command_wheel_extra_*` slots, no
  `tray.ID_HK_WHEEL_EXTRA`); version 2 blocks migrate on load, first set
  entry of `[hotkey, *extra_hotkeys]` wins; an unreadable stored hotkey is
  read as cleared, never as Ctrl+Right click. Clearing asks before saving
  and the page says in one neutral sentence that only Ctrl+Right click on
  the tray icon opens the wheel then. The Web-API ignores an empty
  `extra_hotkeys` from an old page and refuses a non-empty one with
  "reload the page". Proof: `src/python/test_wheel_hotkey_roundtrip.py`
  (real HTTP handler + real config.json, restart, GET) and
  `webgui/src/components/SettingsWheelSave.test.jsx` (real page + real
  wheel section, recorded routes and bodies). Before:
  `0.7.0-dev.50` (wheel hotkey change holds, Viktor
  24.11.: Ctrl+Alt+Space without Alt came back after save + F5. Cause in
  his state (`3b27f8a`): "Save Settings" posted the page's whole loaded
  config, `POST /api/settings` wrote its stale `command_wheel` over the wheel
  just saved by `POST /api/actions/wheel` (wheel protected since dev.49).
  Now `web_api.SETTINGS_FOREIGN_KEYS` (`command_wheel`, `peers`,
  `display_layout`, `updates`) are ignored by `/api/settings` and never sent
  by the Settings page; both hotkey save routes wait up to 1.5 s for the
  new registration report (`seq`) and answer `registration`
  (`pending`, `entries`, `inactive`); a hotkey Windows refuses stays saved
  and the WebGUI says next to the save button "saved, but not active: ...
  already taken by Windows or another program"; "Save Settings" shows its
  result next to the button; unsaved wheel changes show "Not saved yet";
  `overlay_actions.wheel_config_from` falls back per field, so one invalid
  part never resets the chosen hotkey. See `docs/hotkey-config.md` "Save
  confirmation and refused hotkeys"). Before:
  `0.7.0-dev.49` (hotkeys with mouse buttons, Viktor
  24.11.: every hotkey field of the WebGUI (command wheel, clipboard overlay,
  peer switching, return to local) records a key or a mouse button (left,
  right, middle, side buttons, wheel) with Ctrl/Alt/Shift/Win, has a list to
  pick a mouse button and `Clear`; a plain left/right click or wheel is
  refused. Model `input_trigger.py`: `{mods, vk}` (RegisterHotKey),
  `{mods, mouse}` (always-on mouse hook, `MouseTriggerMatcher` swallows the
  matching press and release), `null` = cleared (no report entry, no red
  text). `command_wheel` version 2: `hotkey` is the one main trigger,
  default Ctrl+Right click (the formerly fixed path; nothing doubled),
  `extra_hotkeys` up to three more; migration keeps a self-chosen keyboard
  hotkey and the side button as extra hotkeys, drops only the old default
  Ctrl+Alt+Space. Hold and swipe, `tap_open`/`hold_swipe` work with key and
  mouse triggers. Hotkeys assigned twice are refused by both save routes
  (`400 hotkey_conflict` naming both uses) and shown in the WebGUI before
  saving; `POST /api/settings` no longer overwrites `command_wheel` with
  the page's stale copy. See `docs/hotkey-config.md`). Before:
  `0.7.0-dev.48` (remote screen preview stage 1 of 4,
  [Karte 833](http://kanban.thegoodhunter.vps/tasks/833): settings, peer
  messages, lifecycle and a lock that refuses every request with
  `pairing_required` until device pairing exists; no image, see "Remote
  screen preview" below). Before:
  `0.7.0-dev.47` (copied pictures, Viktor 24.11.: a
  picture copied in the browser ("Copy image") or cut out of a program shows
  its thumbnail on the clipboard card like the file previews, transparency
  kept, and pastes again as a picture, also into Paint. The item now keeps
  the registered `PNG` format when present (before only `CF_DIB`: browser
  transparency lost), else the DIB the program placed first (`CF_DIBV5`
  with alpha or `CF_DIB`); setting it puts `PNG` + `CF_DIBV5` + `CF_DIB`
  back. Thumbnails come from the file-preview service and cache
  (`generate_clip_image`, key = content hash); HTML with only an image
  address shows the neutral `IMG` tile and nothing is loaded from the
  network. See `docs/clipboard.md` "Copied pictures"). Before:
  `0.7.0-dev.46` (WebGUI warnings, buttons and fields,
  Viktor 24.11.: red warnings only for real problems of switched-on
  functions, every button with symbol or word plus tooltip, dropdowns and
  fields in the FlowShift style, spacing between Settings sections; see
  "WebGUI warnings, buttons and fields" below). Before:
  `0.7.0-dev.45` (clipboard copy actions back, Viktor
  24.11.: every file entry in the overlay and the WebGUI has the buttons
  `Full path`, `Folder path`, `File name`, `Root name`, `Relative path`
  (row `Copy`, tooltips); each writes exactly that text, one line per file,
  to the Windows clipboard without a new history entry; `Set` still puts the
  files themselves there. Full/folder path come from the private
  `source_paths` of a local copy and are written by the host
  (`clipboard_model.public_card_copy_text` fields `full_paths`,
  `folder_paths`); the card API only gets `file_card.local_paths` (bool), so
  for files from another PC the two buttons are dimmed with the hint "Only for
  files copied on this PC". The dev.40 display switch `Show files as`
  (`clipboard.file_path_display`) is removed: it was not requested, had
  replaced these buttons and was not visible in Viktor's overlay).
  Before: `0.7.0-dev.44` (clipboard file previews, Viktor 23.11.:
  file cards in the overlay and the WebGUI show a preview per file, see
  "Clipboard file previews" below). Before: `0.7.0-dev.43` (body limit per
  writing route on top of the request guard, 16 KiB for `actions/execute` and
  `actions/wheel`, and handler table with central check in `execute_action`,
  see "Web-API origin check and action handlers" below). Before:
  `0.7.0-dev.42` (Web-API request guard: every POST/DELETE needs the local
  Host, the own WebGUI Origin and JSON, every `/api/` GET the local Host; see
  "Web-API request guard" below). Before: `0.7.0-dev.41`
  (clipboard multi-select covers every entry: `All`/`Invert`/`None` act on
  Recent and Pinned alike, Viktor 23.11.; dev.38 had skipped pinned items).
  Before: `0.7.0-dev.40` (clipboard overlay fixes, Viktor 23.11.:
  (1) window drag is host-driven (`overlay_host.DragSession`,
  `bridge_begin_drag`, pywebview `easy_drag=False`): ends on button release
  anywhere, Escape, focus loss; the old page-driven drag kept following the
  mouse when its mouseup was lost; the stored placement follows the move.
  (2) a `Show files as` display switch (removed again in dev.45, replaced by
  the per-entry copy buttons); loose files get their source
  folder name (last part of the private `base`) as root, before all three
  forms were the file name. (3) Card `Copy` writes via
  `perform_windows_write(..., mark_current=False)` (`tray.write_clipboard_text`),
  so the watcher no longer records it as a new item. (4) `GET
  /api/clipboard/items` lists newest first (`clipboard_model.newest_first`),
  the store order was oldest first. (5)/(6) `DismissPolicy`: clicks on the
  overlay's own windows (search field, dropdowns: `WindowFromPoint` +
  `GA_ROOTOWNER`) are inside for the clipboard; the reference foreground of
  a show is read before an own window is hidden and skips own windows
  (`_previous_foreground`); a ~0.6 s settle phase accepts the foreground
  shuffle of the show itself (wheel -> clipboard). Earlier dismissal rules
  (Escape, outside click, Alt+Tab, wheel hub/gaps, hold-and-swipe) unchanged).
  Before: `0.7.0-dev.38` (clipboard multi-select: check several
  items in the overlay and the WebGUI Clipboard tab, All/Invert/None, delete
  at once without confirmation, see "Clipboard multi-select" below). Before:
  `0.7.0-dev.37` (Command Wheel hover: only the sector
  under the mouse or the swipe aim turns blue, all other sectors keep their
  resting look; the 50 % dimming of the others (`DIM_FACTOR` in
  `overlay_native_wheel.wheel_scene`, `.sector--dim` in `overlay.css`) and
  the 90 ms opacity/fill transitions of the WebView2 wheel are gone, Viktor
  22.11.: fast mouse moves looked like flicker). Before: `0.7.0-dev.36`
  (Command Wheel drawn natively in its own
  layered window with `UpdateLayeredWindow` per-pixel alpha instead of
  WebView2; DWM corner rounding/backdrop/border switched off and read back;
  see "Overlay host" below). Before: `0.7.0-dev.35` (installer/updater: one install lock for
  manual install, update runner, in-program updater and uninstaller; the
  installer stages and checks the new files before it swaps the program
  folder and keeps the previous one until the new version runs; the rollback
  copy is never deleted on a failure; no automatic update right after a
  manual install. Before: `0.7.0-dev.34` (overlay: per-pixel alpha via
  `transparent=True` + black form + DWM blur-behind instead of the colour key,
  on-screen pixel measurement after every show (`overlay pixel check`),
  `POST /api/overlay/probe`; `0.7.0-dev.33` set the WebView2 background and
  form colour key on the WinForms UI thread, no dark root `color-scheme` in the
  overlay page, region also on the child windows, every overlay window listed
  in `flowshift.log`; `0.7.0-dev.32` tried the WebView2 background from the
  worker thread; `0.7.0-dev.31` fixed the automatic update
  "access denied" on `C:\Program Files\FlowShift`, `0.7.0-dev.30` the overlay
  window region, `0.7.0-dev.29` the development update channel from Forgejo)).
- Current stable release: `v0.7.1` (tag and release commit `8ec6bb0` in
  Forgejo; whether GitHub `releases/latest` already shows it is checked with
  `flowshift-github release plan`). Older note: `v0.6.3` (published; tag-triggered workflow `success`, assets `FlowShift-Setup.exe`, `SHA256SUMS.txt`, `update-manifest.json` verified; the immutable `v0.6.0`, `v0.6.1`, and `v0.6.2` tags exist without releases, see below).
- Server-side live checks (autopilot acceptance lines) read the merged code
  from the read copy `/srv/opencode/lesestand/flowshift` (follows Forgejo
  `main`), e.g. `grep -c input_trigger
  /srv/opencode/lesestand/flowshift/src/python/tray.py`. There is no
  FlowShift copy under `/srv/opencode/werkstatt` (that is the Werkstatt
  service repo); a check there fails with exit 2 (file not found).
  Never pin an exact `VERSION` in such a check (`grep -c 0.7.0-dev.N
  .../VERSION`): parallel tasks merge into `main` in any order and raise
  `VERSION` past it, so the line fails with exit 1 although the code is live
  (hotkeys-with-mouse-buttons follow-up 2: the check asked for dev.50, a
  number of another task, while main stood at dev.52). Check a code marker
  of the feature instead, e.g. `grep -c NEEDS_MODIFIER
  /srv/opencode/lesestand/flowshift/src/python/input_trigger.py`.
  Behaviour on Windows is only confirmed by Viktor at the PC.
- Active implementation phase: Phase 4 - Clipboard Overlay and Command Wheel (target `v0.7.0`).
- Active phase specification: `docs/phases/phase_4_clipboard_overlay_command_wheel.md`.
- Phase 4 done so far:
  - `overlay_actions.py`: validated Action Registry (keys actions
    copy/paste/cut/delete/select_all/undo/redo, runtime actions
    open_clipboard/clipboard_sync), wheel config `config["command_wheel"]`
    (pages <= 16 x 8 slots, main hotkey default Ctrl+Right click since
    dev.49, before Ctrl+Alt+Space; exactly one hotkey since dev.51,
    version 3).
  - Web-API: `GET /api/actions`, `POST /api/actions/wheel`,
    `POST /api/actions/execute` (runtime refs `execute_action`,
    `reload_hotkeys`). Fixed `_normalize_runtime_peers` (`/api/peers`,
    `/api/display/layout` raised NameError with configured peers).
  - Web-API origin check and action handlers (dev.43, [Karte 725](http://kanban.thegoodhunter.vps/tasks/725)
    stages 1 and 2): every POST/DELETE first passes the request guard from
    dev.42 (`_guard_request`/`request_rejection`: local Host, own `Origin`
    required, no `Transfer-Encoding`, `application/json`), then
    `_authorize_local_write` with the per-route limit from
    `_write_request_policy`, both before the body is read: 16 KiB for
    `actions/execute` and `actions/wheel`, 1 KiB copy-text, 1 MiB clipboard
    delete, else 64 KiB (413 `<label> request body is too large`).
    `execute_action(action, context=None, channel="web_api")` runs through
    `action_handlers.HandlerRegistry.dispatch`: handler and risk class by
    `kind` (`keys` = L1, `runtime` = L0; a handler without class is refused
    at registration), channel check (`wheel_swipe`/`web_api`, else
    `invalid_channel`), one log line `action check id=… channel=… class=…`,
    then the handler. The tray binds the channel: `wheel_swipe` in
    `_finish_wheel_hold`, `web_api` via `functools.partial` when it registers
    `execute_action` with `web_api.init`; the request body can never set it
    (`validate_execute_request` still allows only `action_id`, `context`).
  - Remote screen preview (dev.48, stage 1 of 4): `remote_preview.py`
    (stdlib only) holds `normalize_settings` for `config["remote_preview"]`
    (`enabled` false, `fps` 5-30/15, `max_width` 480-1920/960, `monitor`
    `cursor`|`primary`|0-63, `cursor_crop`, `window.corner`/`width_px`
    200-1200/480), `parse_message` for `preview_request|accept|reject|stop`
    (2 KiB, session id <= 64 plain characters, ranges), `PreviewSession`
    (`idle -> requested -> streaming -> stopping -> idle`; `streaming` only in
    tests) and `preview_authorization`, which always returns
    `(False, "pairing_required")` and is the single exchange point for
    pairing ([Karte 722](http://kanban.thegoodhunter.vps/tasks/722)).
    Capability `remote_preview` in `hello` only while enabled; after a WebGUI
    change `announce_remote_preview_capability` sends a fresh `hello` and a
    repeated `hello` only updates that flag. `tray._notify_fwd_state(...,
    True)` (hotkey/tray and edge) calls `_preview_request`; `peer_handler`
    dispatches `preview_*` to `_handle_preview_message` (target: always
    `preview_reject`; source: `rejected` + log line
    `remote_preview rejected reason=pairing_required`, an accept is answered
    with `preview_stop`). `_preview_stop` in `deactivate_forward` (covers
    edge return/cancel), the `finally` of `peer_handler` (only for the socket
    the request went over) and `request_shutdown`. `GET /api/status` has
    `preview_state`/`preview_reason`; `/api/settings` reads and writes
    `remote_preview` (partial merge, invalid or unknown field -> 400
    `invalid_remote_preview`, nothing stored). WebGUI: Settings > Remote
    Preview (`RemotePreviewSection.jsx`), Dashboard card "Remote Preview".
    No capture, encoder, channel, port or window.
  - Overlay host: per-mode sizes (`overlay_geometry.MODE_SIZES_CSS`, wheel
    360x360 centered on the cursor, clipboard 420x520 next to it, clamped;
    a wheel shifted at a screen edge moves the cursor onto its hub). The
    native window is clipped with `SetWindowRgn` to the visible shape
    (`overlay_geometry.overlay_shape_polygons`): wheel = ring sectors of the
    current page plus the hub page pill, clipboard = rounded panel; hub,
    gaps and corners are transparent and click-through. React reports page
    changes via `overlay_event("wheel_page")`.
    The form itself is opaque (`background_color="#111827"`, pywebview has no
    reliable per-pixel alpha on Windows), so without the region Viktor saw the
    whole dark square (dev.29, 20.11.). The region was skipped silently when
    `window.native.Handle` did not resolve. Since dev.30 the host reads the
    HWND as `IntPtr` or int (`native_handle_value`), falls back to its own
    top-level window titled `FlowShift Overlay` (`_Win32.own_window`), checks
    after `window.show()` with `GetWindowRgn` that the region is still set and
    re-applies it once (`_ensure_shape`). Failures are logged as
    `overlay host: overlay host shape ... (failed|re-applied after show|missing after show)`.
    dev.31 live (Viktor 20.11. 18:12): square still visible although no
    failure line was logged, i.e. the region was set and kept. Cause: pywebview
    sets the WebView2 `DefaultBackgroundColor` opaque to #111827, and WebView2
    (DirectComposition) is not reliably clipped by the window region. Since
    dev.32 `_clear_webview_background` sets it to `Color.Transparent` on the
    UI thread before the first show; the region stays for click-through.
    The control is resolved as pywebview stores it, `native.browser.web_view`
    (fallback `native.webview`, `webview2_control`); logged as
    `overlay webview control path=...`, on failure with `native attrs=...`.
    INFO diagnostics in `flowshift.log`: `overlay webview background
    transparent=ok alpha=0`, `overlay shape set ... hwnd= placement= dpi=
    window= window_dpi= exstyle= polygons= setwindowrgn= region= box=`,
    `overlay shape after show ... kept`, `overlay window tree ... children=`.
    dev.32 live (Viktor 20.11. 21:15): square still visible; log
    `transparent=failed (CoreWebView2Controller members can only be accessed
    from the UI thread ... get_DefaultBackgroundColor)`, region set and kept
    (hwnd 0x130ecc, 360x360, dpi 96, `region=3`). Since dev.33: all WebView2
    and form reads/writes run inside `form.Invoke`; the form gets
    `BackColor`/`TransparencyKey` = `OVERLAY_KEY_COLOR` #111827 (layered
    colour key); `overlay.css` moved `color-scheme: dark light` from `:root`
    to `body` (a dark root colour scheme makes Chromium paint the canvas
    dark despite the transparent background); the region is also set on every
    child window; `overlay window top|child hwnd= class= pid= rect= visible=
    region= exstyle=` lists all windows of the host process once per host.
    dev.33 live (Viktor 21.11. 21:42): square still visible although every
    line reported success (`transparent=ok ui_thread=1 alpha=0
    form_key=#111827/a255 layered=1`, region on the only visible window and
    all five children, kept). The page has no frame/card behind the wheel
    (checked `overlay.css`). Since dev.34 the colour key is gone: the form is
    created with pywebview `transparent=True` (WebView2 alpha 0 from the
    start; the host clears `window.transparent` in `before_show` so pywebview
    no longer shows the hidden window on navigation), paints black
    (`OVERLAY_FORM_COLOR`) and gets `DwmEnableBlurBehindWindow` with an empty
    blur region (per-pixel alpha, as winit/tao do). Log:
    `overlay webview background transparent=ok strategy=alpha ui_thread=1
    alpha=0 form=#000000/a255 form_key=#000000/a0 layered=0 blur=ok`.
    The screen is now measured: desktop under the hidden window before the
    show, the same points 0.4 s after it (`overlay pixel mode= point= ...
    below= shown= result= source= hit=` per point, then `overlay pixel check
    mode= verdict=ring_only|panel_only|square_visible|background_visible|
    nothing_drawn|unknown ... clear= clickthrough= drawn= painted= region=
    rects=`). `POST /api/overlay/probe {"mode":"command_wheel"|"clipboard"}`
    triggers the same measurement.
    dev.34 live (Viktor 21.11. 23:33, "Das Rechteck ist immer noch da"):
    his screenshots show a ~356x356 rounded rectangle in #202020 with the thin
    light Windows 11 window border; sector fill #141920 is exactly
    rgba(19,24,32,0.9) over #202020, gaps #202020: the page is transparent,
    but DWM composes it onto a window surface that neither region nor
    alpha/blur removes. Since dev.36 the wheel no longer uses WebView2:
    `overlay_native_wheel.py` draws it with GDI+ into a 32-bit premultiplied
    bitmap and hands it to a `WS_POPUP` + `WS_EX_LAYERED|TOPMOST|TOOLWINDOW`
    window via `UpdateLayeredWindow(ULW_ALPHA)` on its own thread and message
    loop. Alpha-0 pixels show the desktop and pass clicks through; no window
    region. `DWMWA_WINDOW_CORNER_PREFERENCE=DONOTROUND`,
    `DWMWA_BORDER_COLOR=NONE`, `DWMWA_SYSTEMBACKDROP_TYPE=NONE` are set and
    read back (also on the WebView2 form for the clipboard panel). Hover
    spotlight, aim, mouse-wheel paging (`overlay_event wheel_page`), labels,
    error text and the two page dots are drawn natively; a click posts
    `/api/actions/execute {source: command_wheel}` like the React wheel. The
    clipboard panel stays WebView2. If the native window cannot start or
    paint, the host logs it and falls back to the WebView2 wheel. Log:
    `overlay native wheel ready hwnd= exstyle= corner=set:/read: border=
    backdrop= paint=updatelayeredwindow`, `overlay native wheel shown ...
    dwm_corner= dwm_backdrop=`, `overlay shape native ... region=none`,
    pixel check with `strategy=native_layered` and `dwm_corner=`. Not yet
    verified on Windows: done only when the corner pixels read `result=clear
    source=below` (not #202020).
    Viktor decided (20.11. 17:58): the two page dots in the hub stay visible
    ("sie indizieren die Seiten des Commandwheel"), the rest of the hub is
    see-through (current region: hub pill only).
  - Dismissal (`overlay_host.DismissPolicy`, 30 ms polling, no hook): the
    overlay no longer needs the foreground. The old focus watcher closed it
    after 0.75 s whenever Windows refused `SetForegroundWindow` (dev.22 bug,
    wheel and clipboard). It now closes only on a new Escape press, a new
    mouse press outside the visible shape, or another window becoming
    foreground. Every close is logged by the controller as
    `overlay_hidden reason=<escape|outside_click|focus_lost|close_button|action|runtime|swipe|swipe_center|tap_off> mode=<mode>`.
    `window.flowshiftOverlay.hide()` unmounts the React views on hide, so a
    hidden clipboard overlay stops polling. Outside clicks currently also
    reach the window below (region click-through); swallowing them would
    need a low-level mouse hook and is an open decision for Viktor.
  - Hold and swipe (dev.26, `wheel_gesture.py`): hotkey (registered with
    `MOD_NOREPEAT`), Ctrl+RightClick and the optional mouse side button
    (`command_wheel.trigger.mouse_button` none/x1/x2, unassigned keeps
    back/forward) open the wheel with `data.hold`; the runtime reports the
    release as IPC `wheel_release {held_ms}` (hotkey release by
    `GetAsyncKeyState` polling, mouse release from the trigger hook). The host
    highlights the sector in the cursor direction while held
    (`window.flowshiftOverlay.aim`), ignores mouse presses during the hold and
    answers `wheel_released`: tap (< `hold_ms`, default 250) keeps it open for
    clicking (or closes with `tap_off` when `tap_open` is off), a hold runs the
    sector in that direction (`swipe`, direction only, 30 CSS px dead zone ->
    `swipe_center`). The runtime waits up to 1 s for released modifiers, then
    `execute_action(source=wheel_swipe)`. WebGUI Settings > Command Wheel edits
    side button, tap, hold and threshold (`trigger`, `400 trigger_disabled`
    when both are off). While forwarding no trigger opens a local wheel.
    The curated release payload was missing `hotkey_status.py` (tray import
    would fail in a packaged build); it and `wheel_gesture.py` are now listed
    in `build_release.ps1` and checked by `test_release_packaging.ps1`.
  - Overlay hotkey status (dev.24, `hotkey_status.py`): `register_runtime_hotkeys`
    records every clipboard/wheel registration; a busy combination
    (`RegisterHotKey` error 1409, e.g. Win+V, Ctrl+Win+V, Ctrl+Alt+Space on
    Viktor's laptop) is published as `GET /api/status` -> `overlay_hotkeys`
    with a plain-text message and the working alternative, shown once per new
    conflict set as a tray balloon (+ tooltip hint), as Dashboard banner and
    list, and next to each hotkey in Settings. The clipboard overlay hotkey is
    configurable (`clipboard.overlay_hotkey`, default Ctrl+Alt+V, Settings >
    Clipboard; must differ from the wheel hotkey, `400 hotkey_conflict`).
    WebGUI modifier bits now match the runtime (Shift=2, Alt=4; before, a
    WebGUI "Ctrl+Alt" was stored as Ctrl+Shift). Captures into the local
    history are no longer announced like a peer (no more
    `clipboard announcement failed -> local`). Win+V/Ctrl+Win+V are not
    intercepted via the keyboard hook (Viktor 18.11.: Ctrl+Alt+V suffices).
  - React overlay: `OverlayShell` routes `command_wheel` -> `CommandWheel`
    (SVG sectors <= 8, cyclic mouse-wheel paging, dots, spotlight hover),
    `clipboard` -> `ClipboardOverlay` (fixed-height list, async polling + SSE,
    same list node and scroll offset across refreshes, Set/Get/Pin/Delete,
    legacy + stream V2 progress via `clipboardFormat.js`), diagnostic card only
    with `data.diagnostic === true`.
  - Stream V2 telemetry reports privacy-safe current-file name, one-based file
    ordinal/count, independent current-file and total bytes/percent, rate,
    `HH:mm:ss` ETA and last completed-file duration. Batch cards render separate
    current and total bars; single-file and non-file cards render one bar.
  - `tray.py`: Ctrl+Alt+V, Ctrl+Win+V and (opt-in) Win+V open the clipboard
    overlay; global Ctrl+RightClick (always-on mouse-only hook
    `WheelTriggerHook`, swallows only the Ctrl right click, inactive while
    forwarding), Ctrl+RightClick on the tray icon or the configurable
    wheel hotkey (`ID_HK_WHEEL`) opens the wheel; `execute_action` hides the
    overlay, restores the remembered foreground window and enqueues key events
    into `inject_queue`; runtime actions open the clipboard overlay or send the
    profile manifest. The Tkinter clipboard tab/window and `gui.py --clipboard`
    entry have been removed.
  - WebGUI Settings includes `CommandWheelSection`: it loads the validated
    registry/wheel, edits ordered pages and slots within server limits, captures
    the wheel hotkey with shared hotkey-format helpers, and saves through
    `POST /api/actions/wheel` so the runtime immediately re-registers it.
  - WebGUI `ClipboardView` keeps its list DOM node and scroll offset across
    item/progress refreshes, rejects stale overlapping profile responses, and
    renders legacy plus stream V2 progress through `clipboardFormat.js`; detail
    and list rows share the same polling snapshot.
  - Tkinter retirement prerequisite: WebGUI Settings now exposes the productive
    `persist` and `max_item_gb` controls. `POST /api/settings` normalizes the
    complete clipboard block through `clipboard_model.clipboard_settings`,
    preserves unrelated top-level config, refreshes `istate.hotkeys`, publishes
    status, and requests immediate OS-hotkey re-registration.
  - WebGUI routes are URL-backed; `/clipboard` opens the Clipboard tab directly
    and the HTTP server serves the SPA there. The tray Clipboard menu entry opens
    that route; tray double-click and Web GUI still open the dashboard. Current
    architecture, clipboard and manual-test documentation describes the React UI.
  - Public clipboard card contract: `GET /api/clipboard/items` returns
    whitelisted `public_card_item` rows (relative `file_card` summary, bounded
    previews/names, `is_current`, no `files`/`abspath`/`source_paths`/`base`/
    fingerprint data) plus top-level `current_item_id`; item detail returns the
    same public row plus a bounded relative `file_manifest` slice
    (`manifest_offset`/`manifest_limit`). Covered by
    `test_web_api_clipboard.py`.
  - Clipboard presentation: overlay and WebGUI use the shared `ClipboardCard`
    for one type-specific card per history item, separate Recent/Pinned views,
    safe text/file/batch previews, current/download metadata and item actions.
    File cards expose bounded relative root/name/path lists; copy buttons call
    the local-only JSON `copy-text` route, which regenerates text from the
    persisted public fields instead of trusting client content. File detail
    text also uses only validated relative manifest paths.
  - Local clipboard without peer: every capture also lands in the persistent
    `local` history (`This PC`); overlay payload and
    `GET /api/clipboard/profiles` list `local` first; missing profile params
    fall back to `local`; WebGUI and overlay default to it; sync on `local`
    is an ok-noop.
  - Clipboard multi-select (dev.37, Viktor 22.11.): before this, a single item
    was deleted with the card's `Delete` button ->
    `DELETE /api/clipboard/item/<id>?profile=` -> `ClipboardManager.delete_item`
    (marks the id as deleted for receivers, releases leases, cancels transfers)
    -> `ClipboardStore.delete_item` (removes the row, clears `current` if it
    was that item, writes `index.json`, removes the unreferenced object and the
    preview). The WebGUI tab also had `Clear` (confirm dialog, `POST
    /api/clipboard/clear`, removes pinned items too). The history lives in
    `%ProgramData%\FlowShift\clipboard\profiles\<profile>\index.json` plus
    `objects/<sha256>` and `previews/`. Now both views share
    `clipboardSelection.js` (`useClipboardSelection`) and
    `ClipboardSelectionBar.jsx`: checkbox per card, Ctrl-click toggles,
    Shift-click range, `N of M selected` as text, then four icon-only buttons
    (inline SVG in Font Awesome style, since the overlay does not load Font
    Awesome; each has `title` and `aria-label`, hit area 32 px WebGUI / 28 px
    overlay): check-square `All` (Ctrl+A), arrows `Invert` (Ctrl+I), empty
    square `None` (Ctrl+Shift+A), trash can `Delete selected` with the count as
    a badge (Delete key; greyed out without a selection). The selection spans
    the whole profile (Viktor 23.11.): `All`/`Invert`/`None` act on every
    entry, Recent and Pinned; `useClipboardSelection(scope, visible)` takes
    the search-filtered entries of both tabs as scope (count `M`, All,
    Invert, Delete) and the current tab only for Shift-click ranges.
    Switching tabs keeps the selection; switching profile clears it, a search
    prunes it to the matching entries. Delete is immediate
    and optimistic, one `POST /api/clipboard/delete?profile=` with
    `{"item_ids": [...]}` (central request guard, JSON, at most
    10 000 ids) -> `ClipboardManager.delete_items` -> `ClipboardStore.delete_items`
    (one `index.json` write), logged as `clipboard delete selected profile=
    requested= deleted=`. The WebGUI `Clear` button and its `confirm()` are gone
    (`api.clearClipboard` removed; the backend route stays). Selection clicks
    (checkbox, Ctrl/Shift click, Ctrl/Shift double-click) never call
    `pasteItem`; plain double-click and `Set` paste as before. The Windows
    clipboard is never touched by deleting; deleting the `Current` item only
    drops the mark, the content stays pasteable and comes back into the history
    only when copied again (the watcher reacts to new clipboard sequence
    numbers only).
  - Clipboard file previews (dev.43, Viktor 23.11.): before this, file cards
    showed only the `FILE`/`FILES` badge and names. Now
    `clipboard_file_preview.FilePreviewService` (created in `tray.py` as
    `_file_previews`, passed to `web_api.init(file_previews=...)`) answers
    `GET /api/clipboard/file-previews/<item>?profile=&count=` at once
    (`pending|ready|missing`, kind `image|animated|video|icon|none`, no
    paths) and generates on one background thread into
    `%ProgramData%\FlowShift\clipboard\previews` (`<key>.json` + `.still` /
    `.anim`; key = sha256 of path, size, mtime, size class; limit
    `clipboard.file_preview_cache_mb` default 128, oldest first down to 80 %,
    entries older than 30 days and stale `.tmp-*` removed). Images: Pillow
    thumbnail (JPEG draft decode, max 80 MP, sources over 64 MB only get the
    type icon). GIF/APNG/animated WebP: detected by bounded header parsing,
    scaled animated WebP (GIF fallback, max 90 frames, sources up to 32 MB).
    Videos: Windows shell thumbnail (`IShellItemImageFactory`, ctypes) as
    still; `GET /api/clipboard/file-preview/<item>/<index>/video` streams the
    copied mp4/m4v/mov/webm/ogv with HTTP Range for a muted hover clip (first
    4 s in a loop). Everything else: shell icon of the file, or the type icon
    by extension (`SHGetFileInfoW`) for remote items without local files.
    Missing, moved, changed (size/mtime differ) or locked files answer
    `missing` (retry after 15 s), the UI shows a neutral tile with the
    extension. WebGUI: `clipboardPreview.js`, `ClipboardFilePreviews.jsx`
    (loads when the card scrolls into view, polls while pending); single
    file = preview in the icon slot, batch = first 4 tiles plus `+N`;
    tooltips show the relative path. Settings > Clipboard: `Animated File
    Previews` (Always/Hover, `file_preview_animation`), `Video Preview on
    Hover` (`file_preview_video_hover`), `File Preview Cache (MB)`. Uninstall
    without `-PurgeData` removes the preview cache too. Tests:
    `test_clipboard_file_preview.py`, `ClipboardFilePreviews.test.jsx`.
  - Copied pictures (dev.47, Viktor 24.11.): `clipboard_win.read_image`
    picks the source with `pick_image_source` (registered `PNG`/`image/png`
    first, trimmed with `clipboard_image.trim_png`; else the DIB the program
    placed first by `EnumClipboardFormats` order, `CF_DIBV5` or `CF_DIB`, as
    BMP). `ClipboardManager.capture_image` stores mime `image/png` or
    `image/bmp`, display name `Bild WxH` from `clipboard_image.image_size`.
    `set_image` writes `clipboard_image.paste_formats`: PNG item = exact
    `PNG` (both registered names) + `CF_DIBV5` (alpha) + `CF_DIB` (on white,
    both via Pillow; without Pillow only `PNG`), BMP item = its DIB
    (`CF_DIBV5` when the header is V5). Write suppression still matches
    because the read-back returns the same PNG bytes. Card thumbnail: the
    file-preview routes accept `image`/`gif` items, `FilePreviewService`
    answers one row with `source: "clipboard"` and enqueues a `clip` job
    that reads the stored bytes on the worker (`web_api._clip_image_loader`);
    `generate_clip_image` (Pillow, else `bmp_thumbnail_rgba` sampling only
    the output pixels, PNG pass-through up to 4 MB), cache key
    `clip_image_key(sha256, px)`, same cache limit and cleanup. Not on this
    device / damaged: neutral `IMG` tile. HTML items with only `<img>` get
    `metadata.image_only` (`clipboard_html.html_is_image_only`) and the
    neutral tile; the HTML detail iframe gets a CSP that blocks network
    loads (`clipboardPreview.offlineHtml`). The never-working PPM card
    thumbnail of the WebGUI (`api.getThumbnail`) is removed; the
    `/api/clipboard/thumbnail` route stays for the legacy GUI. Tests:
    `test_clipboard_image_preview.py`, `test_clipboard_sync.py` (PNG
    roundtrip), `ClipboardFilePreviews.test.jsx`.
  - WebGUI warnings, buttons and fields (dev.46, Viktor 24.11., WebGUI
    only, no behaviour change): red only for real problems of switched-on
    functions. `HotkeyStatus.hotkeyEntryLevel` sorts every overlay hotkey
    entry into `ok`/`notice`/`conflict`/`off`: the extra Ctrl+Win+V shortcut
    (`clipboard_ctrl_win_v`, Windows 11 owns it) is a neutral `notice`, Win+V
    is left out while `intercept_win_v` is off and all clipboard slots while
    the clipboard is off (Settings page passes its settings); the Dashboard
    banner lists only `conflict`. Dashboard hotkey table: no action = `not
    assigned` (neutral). Edge-layout warnings (Display tab, Settings) are
    neutral while edge switching is off. Buttons: `ButtonIcon.jsx` draws the
    symbols of icon-led buttons inline (Font Awesome comes from a CDN and
    can be missing): wheel slot `Up`/`Down` (symbol + word + tooltip), remove
    buttons, display picker close, update access save/remove; every button
    and field has a tooltip or name. Pro-only `fa-files` replaced by
    `fa-copy`. `App.css` "Form controls": one look for inputs, dropdowns and
    textareas (`:where` baseline, hover, accent focus ring, own dropdown
    arrow, dark option list via `color-scheme: dark`); `.settings-group`
    has `margin-bottom: 20px` on every page; `.dot` status dots styled.
    The tray balloon for a taken hotkey is unchanged (not WebGUI).
    Same version, stream V2 receiver fix (found by the CI smoke
    `test_clipboard_stress_v2` disk-full test): `IncomingTransferStage.
    _terminal_failure` now commits the retryable `failed` journal before it
    publishes `state == "failed"`. Before, a status poll or a racing resume
    request could see a failed stage whose on-disk journal still said
    `receiving` (resume then rejected as `transfer_terminal`).
- Last completed phase specification: `docs/phases/phase_3_clipboard_transfer_hardening.md`.
- Phase 3 toolchain and dependency modernization is complete.
- The productive legacy clipboard path and binding V2 target architecture are
  documented in `docs/clipboard_transfer_v2.md`.
- Capability negotiation, metadata-first schema-2 capture, canonical batch
  manifests, centralized Windows path validation, and the persistent
  thread-safe transfer-session foundation are implemented.
- Strict typed V2 JSON-control and raw binary-payload framing is implemented,
  including incremental parsing, absolute read deadlines, protocol limits,
  SHA-256 chunk verification, and serialized per-socket writes; it carries the
  productive V2 channel.
- Bounded V2 flow control is implemented as a transport-neutral foundation:
  global and per-peer admission, count/byte-bounded send and receive queues,
  per-transfer in-flight windows, finite ACK deadlines, strict cumulative ACKs,
  batching by bytes/chunks/time/file completion, and slow-receiver loopback
  coverage, including separate verified and durable offsets plus restored sender
  windows; the productive transport uses it.
- The productive legacy transfer queue rejects duplicate IDs and releases
  terminal, rejected, cancelled, and shutdown job closures.
- Transport-neutral direct V2 file streaming and receiver staging are
  implemented. Sources are opened one at a time, read once sequentially, hashed
  during transfer, and revalidated by path and open-handle fingerprint. Incoming
  data is written sequentially to private index-based `.part` files, flushed,
  `fsync`ed, size/hash/fingerprint verified, and atomically renamed per file to
  `.verified` before a finalized manifest revision is returned. Typed-channel
  tests exercise bounded queues, cumulative ACKs, slow receiver backpressure,
  and exact staged bytes.
- Transport-neutral persistent V2 resume is implemented with strict canonical
  incoming/outgoing journals, generation-CAS atomic commits, startup inventory
  and quarantine, checkpoint batching, durable ACK progress, source/partial
  prefix re-hashing, sender/receiver/dual restart reconstruction, tail
  truncation, pause/cancel/purge states, and rename/commit crash reconciliation.
  Productive resume messages and V2 routing remain open.
- Transport-neutral V2 preflight and shared per-file publication are implemented.
  Accepted estimates bind manifest/transfer and durable resume evidence; source
  reads and stage allocation are gated. Same-volume hardlink publication reuses
  receiver hash/change-time evidence, retains retry pins, and commits the same
  history item plus durable receipt before journal completion and stage cleanup.
  Index-write failures, restart finalization, completed cleanup, explicit purge,
  cross-profile dedup/GC, Windows restored-mtime tampering, lock contention, and
  corrupt-index recovery have focused end-state tests. Legacy-only indexes stay
  schema 2; V2 receipt indexes use schema 3. Journal schema 1 migrates with backup
  and CAS generation increment to schema 2; old stage-only completion becomes
  finalizing. V2-only objects are not advertised as legacy ZIP payloads.
  Automatic GC, provider/cache transitions, materialization, runtime preflight
  routing, and productive network activation remain open.
- V2 materialization is implemented (`clipboard_materialize_v2.py`) and wired
  into `ClipboardManager.materialize_files_result` for `object_manifest_v2`
  items: same-volume hardlinks with device/inode confirmation, verified
  streaming copy fallback (cross-volume, unsupported FS, permission, link
  failure), staged build plus atomic rename into the lease directory, `.active`
  markers and `set_lease`, no ZIP output; lease release unlinks only the
  materialization while shared objects remain.
- V2 availability is two-level: load/listing/`known_hashes`/provider state use
  cheap `item_is_deliverable` (regular file + exact size, no payload reads);
  delivery (`v2_manifest_for_item`) and publication verification rehash.
- V2 cache eviction is integrated: V2 receipts record a received-cache entry
  (content identity, `payload_size = total_size`); eviction sets the item to
  `missing`, marks the local provider `unavailable`, prunes the receipt in the
  same index write, and runs reference-aware GC that references objects only
  from deliverable items (pins/hardlinks protect via link count). Runtime
  eviction now removes only the LRU excess over `cache_max_mb`; the previous
  unbounded eviction while under budget was a latent legacy defect.
- Lease retirement is productive: `tray.clipboard_watcher` and
  `perform_windows_write` call `retire_leases_for_sequence`; stale leases keep
  their tree until `cleanup_leases(clipboard_temp_cleanup_max_age_hours)`.
  The temp cleanup settings key mismatch is fixed.
- Preflight V2 and the update idle gate are wired in the runtime:
  `ClipboardManager.preflight_stream_v2_receive` / `prepare_stream_v2_receive`
  (real free space, no stage or journal on rejection, `disk_full` keeps the
  journal retryable with partials) and `transfer_activity_state` feeding
  `activity_snapshot` so the updater `WAITING_FOR_IDLE` loop defers; durable
  V2 `paused`/`waiting_reconnect` stages allow updates, legacy paused jobs block.
  Journal store convention: `<store_root>/journals`.
- Cancellation, timeouts, progress, and cache-disabled materialization are
  implemented for the receiver side (`clipboard_transfer_control_v2.py`:
  `TransferTimeouts`, `DeadlineTracker`, EWMA `TransferRateTracker`).
  `ClipboardManager.cancel_stream_v2_session` works in every phase (journal
  `cancelled`, partials purged after peer ACK or `final_complete_ack` timeout);
  `run_stream_v2_maintenance` is ticked from `tray.clipboard_watcher`;
  `stream_v2_status` is exposed under `diagnostics()["stream_v2"]`
  (`/api/clipboard/status`) with relative file names only. Timeout settings:
  `clipboard_transfer_v2_<name>_timeout_s`. With `cache_received_payloads`
  off, V2 items publish uncached, materialize into the lease tree, are retired,
  and their objects are collected at lease end. Sender-side `manifest_ack` /
  `window_ack` deadlines are live in the outgoing session.
- The V2 transport is productive (`clipboard_transport_v2.py`): both peers
  advertise the capability in hello; `tray.py` selects the strategy
  (`select_clipboard_transfer_strategy`) and hands it to the manager together
  with a channel dialer; the copy path (`_on_request`) starts
  `OutgoingTransferSession` when negotiated, else legacy ZIP. Control messages
  (offer/accept/reject/resume/cancel/cancel_ack) travel on the legacy link;
  payload uses a dedicated socket (channel hello + single-use nonce, typed
  frames, cumulative window ACKs, complete/complete_ack). Receiver dispatch:
  offer -> `prepare_stream_v2_receive` -> `ReceiverSession` -> publish via
  `publish_stream_v2_session` with legacy `make_current`/`enforce` semantics.
  Disconnect -> `waiting_reconnect` -> resume from durable offsets. Peer
  cancels are always ACKed, also for terminal sessions (resume/cancel race).
  Setting `clipboard_transfer_v2_force_legacy` (default false) forces legacy.
- Restart resume is implemented on both sides. Receiver restart: a
  `T_RESUME_REQUEST` reopens the incoming journal via
  `prepare_stream_v2_receive(resume=True)`. Sender restart:
  `ClipboardManager.resume_outgoing_stream_v2(identity)` (called from
  `on_peer_connected`) scans `journals/outgoing`, restores resumable journals
  (`OutgoingTransferSession.restore`, identity/digest checked with
  `validate_resume_match`) and re-offers them with `T_RESUME_REQUEST`; terminal
  leftovers are purged; journals whose source item vanished or changed are
  purged and the peer receives `T_CANCEL(source_missing|source_changed)`.
  A `T_CANCEL` for a transfer without a live session purges an orphaned
  incoming journal/stage (`_purge_orphan_incoming_stream_v2`). A restart counts
  as one resume attempt; `MAX_RESUME_ATTEMPTS` is 128 (cumulative, not reset on
  progress). Retry counts are persisted in the outgoing journal. Receiver
  session stage mutations are serialized through the session lock (cancel vs.
  in-flight `accept` race). A `failed` receiver session with a retryable
  `failed` journal (disk full) accepts a resume request. An unchanged published
  V2 row may be metadata-refreshed by a peer manifest (`_add_item_locked`);
  previously the refresh raised and dropped the whole peer message.
- Known limitations: `resume_inventory` lower-device-id coordination and
  provider failover remain planned; V2 sessions are not mirrored as legacy
  `TransferJob`s (decision: the section-22 status API is
  `diagnostics()["stream_v2"]`, no legacy GUI mirroring in Phase 3); the
  sender's local item stays provisional (revision 0); during update
  maintenance `_begin_incoming_operation` admits only legacy continuations, so
  V2 cancel/cancel_ack are deferred. Lease `pending_write` persistence before
  the clipboard write and startup revalidation of unbound leases remain
  planned. Cross-host channel dialing (`hello.port`) is only exercised on
  localhost. Journal commits validate and digest every entry (about 3 commits
  per file), so batches of several hundred small files approach the default
  `final_complete_ack` timeout; the sender holds its session lock during ACK
  journal commits. Incoming journals whose sender never re-offers and never
  cancels are not timed out without a live stage. A premature duplicate
  `cancel_ack` can let the receiver purge before an in-flight resume request
  arrives (then bounded by the preflight timeout).
- The immutable `v0.5.3`, `v0.6.0`, `v0.6.1`, and `v0.6.2` tags remain unchanged; their release workflows failed and created no release. `v0.6.2` passed the full 627-test regression on CPython 3.14.6 and failed only in `worker_smoke_test.py`: the `activate` control call used a 2 s socket timeout while the hook thread start is bounded by 5 s; the smoke test now allows 15 s, prints the activation latency and dumps the runtime logs on abort (no productive change). `v0.6.1` failed at tag/VERSION validation because commits `8995b8c` and `9678b2b` did not update `VERSION` (bookkeeping error, `VERSION` stayed `0.6.0`). `v0.6.0` failed only in CI (CPython 3.14.6, runner TEMP with 8.3 short names): `safe_target_path` test compared against `Path.resolve()`, and the status-polling stress test used the 8 s FAST final-ack deadline; both tests were corrected, no productive change.

## Agent structure

- Permanent rules live in `AGENTS.md`.
- The active phase is tracked in `TODO_CURRENT.md`.
- The active phase specification is referenced from `TODO_CURRENT.md`.
- `HANDOFF_CURRENT.md` is the compaction checkpoint and operational snapshot.
- Run full repository regression only before a stable release.
- Use focused tests during individual slices.

## Current setup state

- Release CI uses CPython 3.15.1 and Node.js 24.18.1 LTS; dependency CI also
  tests Node.js 26.5.1 Current.
- GitHub Actions are immutable SHA pins and Dependabot covers Actions, npm, and
  Python dependencies.
- Runtime Python dependencies and audit tooling are fully hash-locked; direct
  npm dependencies and npm 12.0.2 are exact and package-lock v3 is committed.
- WebGUI development dependencies use Vite 8.3.5 and Vitest 4.1.12; resolved
  source-map-js 1.2.2 and undici 8.12.3 leave `npm audit` at zero findings.
- The installer accepts supported 64-bit CPython 3.11-3.15. When Python is
  missing it downloads the official CPython 3.15.1 x64 installer and verifies
  the published SHA-256 before execution. It preserves user ownership and
  requires hashed dependencies.
- Python search (dev.28, fix for "Python not found" after the WebGUI build on
  Viktor's laptop with Python 3.13.2): the old probe
  `& $py -c '...print(f"...")' 2>&1` can never succeed under Windows
  PowerShell 5.1 (inner double quotes reach Python unescaped -> SyntaxError;
  stderr + `$ErrorActionPreference = 'Stop'` -> swallowed exception). Setup.exe
  and the updater always use 5.1; the `.bat` path uses 5.1 whenever it does not
  self-elevate into `pwsh`. The new `Invoke-PythonProbe` starts each candidate
  as its own process (`-I -c` probe without quotes, stdin closed, PYTHONHOME/
  PYTHONPATH/VIRTUAL_ENV removed, 20 s limit) and logs per candidate
  `python candidate <path>: exit=.. time=..s stdout=.. stderr=.. -> <verdict>`.
  Candidates: py/python/python3 on PATH, `%SystemRoot%\py.exe`, PEP 514
  registry, `Program Files[ (x86)]\Python3*`, `%LOCALAPPDATA%\Programs\Python\Python3*`
  (no recursive Program Files search, FlowShift's own venv excluded); the real
  `sys.executable` builds the venv. The log also records the PowerShell host.
- Installer order (dev.28): WebGUI build -> Python check -> Python install only
  if nothing usable exists (announced with version, target folder and duration;
  no download if `Program Files\Python315\python.exe` already exists; download
  300 s limit, setup 600 s limit with a progress line every 15 s, setup log
  `%ProgramData%\FlowShift\logs\python-setup.log`) -> stop the running
  FlowShift -> replace files. A failure before the file step leaves the old
  installation untouched; the failure box ends with a `Status :` line saying so
  and whether the old FlowShift answers again (`/api/status` version).
  Native calls with stderr (venv, pip, import smoke, NSSM) go through
  `Invoke-NativeCapture`.
- Tests: `src/python/test_installer_python_probe.py` (no Windows: reproduces the
  PowerShell 5.1 argument passing and the old SyntaxError, runs the real probe
  text, checks step order and failure note) and `test_install_python_probe.ps1`
  (Windows, also in release CI: real installer functions, WebGUI build step
  then Python search under `Stop`, hostile PYTHONHOME, timeout, failing
  candidate; `-RealWebGuiBuild` uses `install_webgui.ps1`).
- The packaged end-user path remains Node-free.
- One installer for program and WebGUI: `install_flowshift.ps1` always
  replaces `%ProgramFiles%\FlowShift\webgui` from the prebuilt, VERSION-stamped
  `webgui/dist` (Vite plugin `flowshift-version-stamp` writes `dist/VERSION`),
  stops/restarts a running FlowShift, keeps `%ProgramData%\FlowShift`, asks
  nothing except UAC (`-Interactive` opt-in) and finally verifies that the
  running runtime reports the same VERSION as the WebGUI and serves
  `index.html` + `overlay.html`. `install_webgui.ps1` is only an internal build
  helper (never installs Node.js); the separate WebGUI bat/uninstaller entry
  points are removed. Setup.exe runs only `install_flowshift.ps1`;
  `build_release.ps1` requires `webgui/dist/VERSION` = VERSION; the updater
  health check requires `overlay.html` and `status.webgui.ok` for the target
  version. The uninstaller asks nothing, removes program + WebGUI and keeps
  user data unless `-PurgeData`.
- Runtime WebGUI check (`webgui_health.py`, `status.webgui`): missing
  `overlay.html` blocks the overlay host start and shows a tray balloon; version
  mismatch/unversioned WebGUI shows tray balloon + dashboard warning. The
  overlay controller backs off 1/2/5/15/30/60 s also before the first ready host
  and gives up for 10 min after 5 failed starts (`overlay.gave_up`).
- The curated release payload includes `web_api.py`, `overlay_actions.py`, and
  the other productive Python modules; its contract test imports them directly
  from the staged payload.

## Productive path

- `src/python/tray.py --tray` is the productive Windows runtime.
- `webgui/` is the React/Vite settings UI (the settings interface; new
  settings go only here) and diagnostic overlay shell.
- `src/python/gui.py` is the legacy tkinter GUI (devices, profiles, hotkeys,
  runtime control, live test); no new settings, staged retirement plan in
  `TODO_CURRENT.md` ("Next planned phase").

## Repository rules (see `AGENTS.md`)

- Development source is Forgejo `private-platform/flowshift` (`main`); GitHub
  receives only releases approved by Viktor.
- Update channels (dev.29): `stable` = GitHub `releases/latest` (stable only,
  FlowShift-Setup.exe; default of every new installation; needs no Git,
  Node.js or Forgejo access; checked at start and every 6 h since dev.52;
  publication only via `scripts/publish_github_release.py` after Viktor's
  approval); `development` = merged state of Forgejo
  `private-platform/flowshift` branch `main` at
  `http://forgejo.thegoodhunter.vps` (`update_model.FORGEJO_*`). No package
  build, no Forgejo runner, no workflow (Viktor 19.11. 22:09). Check:
  `GET /api/v1/repos/private-platform/flowshift/branches/main` (commit ID),
  `GET .../raw/VERSION?ref=<commit>`; only if newer the source archive
  `GET .../archive/<commit>.zip` is downloaded once and checked
  (`update_model.validate_source_archive`: ZIP comment = commit ID from
  `git archive`, one top folder, no `..`/absolute/drive paths, size limits,
  required files, VERSION matches). Its size and SHA-256 bind download
  (`updates\downloads\FlowShift-Source-<version>.zip`) and handoff (plan
  `package_kind = source`). `update_flowshift.ps1` unpacks entry by entry to
  `ProgramData\FlowShift\updates\source\<version>`, keeps the old version as
  `Program Files\FlowShift.rollback-<version>`, runs the archive's
  `install_flowshift.ps1 -FlowUpdate -NonInteractive` in the same PowerShell
  host (5.1 or 7), then the usual start + health check + rollback, and logs
  to `ProgramData\FlowShift\logs\update.log` (install steps: `install.log`).
  Moving the program folder aside (dev.31, Viktor's laptop 20.11.: dev.29 ->
  dev.30 failed with "Der Zugriff auf den Pfad C:\Program Files\FlowShift
  wurde verweigert"): the run was elevated (it only logs after
  `Test-Administrator`; the runtime runs via the `FlowShift` task with
  `RunLevel Highest`), but the runner inherited the runtime's working
  directory `Program Files\FlowShift\src\python`, and that alone blocks the
  rename. Since dev.31 `update_handoff` starts the runner with
  `cwd = ProgramData\FlowShift\updates`, the runner switches there itself
  (`Enter-RunnerDirectory`, also for the installer child processes), stops
  processes still running from the program folder (venv python, `tray.py` /
  `overlay_host.py` / `gui.py` under the base interpreter, their WebView2
  children; `Stop-FlowShiftInstallProcesses`, 10 s grace) and retries the move
  5 times; the final error contains `program folder in use`. The runner
  deletes `update_plan.json` right after its ack; the runtime removes any
  leftover plan at startup (`UpdateStateStore.remove_stale_update_plans`) and
  drops file notices whose file is gone. After a failed run the restarted
  runtime reports `last_error.code = update_install_failed` (with `cause`)
  instead of `interrupted_operation`, and `/api/update/status` carries
  `last_install {from_version, to_version, result, finished_at, installed,
  cause}` (`update_manager.classify_install_failure`: program_folder_in_use,
  runtime_did_not_stop, installer_failed, new_version_did_not_start,
  rollback_failed, other); Software Update explains it in plain words with
  what to do. A runtime without administrator rights no longer hands over
  (`not_elevated`, no unattended UAC prompt); the desktop shortcut
  `FlowShift.lnk` now starts the `FlowShift` task (`schtasks /Run`), so the
  runtime always has the task's highest privileges. An installed dev.29/dev.30
  still has the old runner: dev.31 has to be installed once by hand.
  The WebGUI of a source state is built by `install_webgui.ps1`, so Node.js
  24+ must be on the PC; `update_handoff.find_node` checks this before
  FlowShift is stopped (`node_missing`). Minimum updater for source states:
  `SOURCE_UPDATER_MIN_VERSION = 0.7.0-dev.29`; older installs switch once by
  hand. Access token: Windows Credential Manager `FlowShift:forgejo-update`
  (`update_credentials.py`), entered by Viktor once in the WebGUI
  (`POST /api/update/development-access`, write-only), sent only to the
  Forgejo origin (also on redirects); nothing in config.json, repo or
  scripts. The manager re-checks development every 15 min and on a channel
  switch; unreachable Forgejo (VPN off) -> `last_error.code =
  source_unreachable`, quiet, with `last_successful_check_at` kept. A failed
  or rolled-back version is not installed again automatically
  (`automatic_install_held`, read from `last_update_result.json`); a click on
  Install still can. A downloaded package of the other channel is not
  installable (`blocked_reason = channel_changed`). `version.SemVer` ranks
  `0.7.0` above `0.7.0-dev.N`.
- Every change to installed files, dependencies, folders, autostart, registry,
  ports, WebGUI assets or `%ProgramData%\FlowShift` data updates installer,
  updater, uninstaller and `packaging/` in the same task; checks include
  installing over an older installation and a clean uninstall.
- Every task keeps `README.md`, `HANDOFF_CURRENT.md`, `TODO_CURRENT.md`,
  `MANUAL_TEST_CHECKLIST.md` and `docs/` in line with the code.
- Rust (`src/service`, `src/viewer`) remains experimental.

## Verified baseline (release regression for `v0.6.3`)

Run locally on Windows with CPython 3.12 and Node.js 26.5.0 using the exact
release-workflow commands (CI runs CPython 3.14.6 / Node.js 24.18.1):

- `python -m compileall src/python`: clean.
- `python -m unittest discover -p "test_*.py"` in `src/python`: 627 tests OK on CPython 3.12 and 3.13 (the latter with an 8.3 short-name TEMP)
  (one skipped Windows symlink-privilege test), incl. transport V2 (28),
  stress V2 (10: 10k typed frames, 100 disconnect/resume cycles, 200-file
  batch, cancel storm, concurrent status polling, slow receiver window bound,
  disk-full resume, 10 restarts without thread leak, 1k malformed frames,
  >4 GiB offsets), tray localhost E2E (3, real TCP through `peer_handler` /
  `_clip_send` / `_clip_open_channel`), transfer control V2 (24 incl. raced
  purged-journal release), `test_service.py`, updater, WebGUI API, overlay,
  and legacy transfer/sync/streaming suites.
- `worker_smoke_test.py` (activate answered in 0.02 s locally), `e2e_test.py`, `reconnect_stress_test.py 30`,
  `overlay_ipc_stress_test.py`, `overlay_show_hide_stress_test.py`: exit 0.
- `test_update_flowshift.ps1`: 7 passed; all PowerShell sources parse.
- `python -m pip_audit -r requirements.txt`: no known vulnerabilities.
- WebGUI: `npm ci`, `npm audit --include=dev --audit-level=high` (0
  vulnerabilities after vitest 4.1.11 / nanoid 3.3.19), `npm test` (9 passed),
  `npm run build`: OK.
- `packaging/test_release_packaging.ps1`: passed.

## Last pushed commits

- `5e0c659` - Phase 4 dev.16: refresh WebGUI dependencies (task branch).
- `fda49a6` - Release v0.6.3: Phase 3 clipboard transfer hardening (tag `v0.6.3`; workflow success, release published).
- `e7bb274` - Release v0.6.2: Phase 3 clipboard transfer hardening (tag `v0.6.2`; smoke-test timeout on CI, workflow failed).
- `9678b2b` - Release v0.6.1: Phase 3 clipboard transfer hardening (tag `v0.6.1`; VERSION not updated, workflow failed).
- `8995b8c` - Phase 3 v0.6.1-dev.1: make path containment and status-polling tests CI-stable (VERSION not updated).
- `a7dcadc` - Release v0.6.0: Phase 3 clipboard transfer hardening (tag `v0.6.0`; workflow failed, see release state).
- `6005228` - Phase 3 dev.17: close V2 documentation, release raced receiver stages, and patch WebGUI audit findings.

## Last successful focused tests (Phase 4)

- Update from a connected device (dev.57), Linux: `test_peer_update` (30:
  two instances over real TCP sockets with an installed-layout sender, install/
  notify/download policies, waiting for idle, cancel, Stable without and with
  the development tick, unpaired device, sender refusing an unlisted device,
  other device ID, failed version not repeated, resume after a broken
  transfer, unreachable peer, changed/extra/missing/unsafe files, wrong
  VERSION, downgrade, bytes that differ from the offer; peer handoff plan),
  `test_web_api_updates` (peer routes, strict settings, foreign origin),
  `test_web_api_request_guard`, `test_update_handoff`, `test_update_channel`,
  `test_update_stable_path`, `test_usb_package`, `test_version_config`. Full
  discovery: 1211 tests, only the known `test_clipboard_object_store_v2` /
  `test_clipboard_streaming_v2` read and fsync counters and the Windows-only
  import errors fail (they import nothing this change touches). Not run here:
  WebGUI `npm test` (`PeerUpdatePanel.test.jsx`, `SoftwareUpdateSection.
  test.jsx`) and `npm run build`, PowerShell parsing of
  `update_flowshift.ps1`, `packaging/test_release_packaging.ps1`, two real
  Windows PCs (`docs/install_test_checklist.md` O).

- USB package source-only packages (dev.56): `test_usb_package` (54 with
  `test_web_api_updates`), `test_usb_package_pypi` against the real PyPI
  (2 tests, about 35 s, needs internet; skipped with the reason when there
  is no network path to PyPI, as in the automatic test run's sandbox), one full real build incl. the
  python.org installer and offline `--target` installs for 3.11-3.15.

- Hotkeys with mouse buttons (dev.49): `test_input_trigger` (new: validation,
  display, conflicts, peer rows, `MouseTriggerMatcher` press/release/wheel/
  forwarding), `test_hotkey_status`, `test_overlay_actions` (incl. wheel
  migration), `test_wheel_gesture`, `test_web_api_actions` (mouse hotkey set,
  cleared and set back, old wheel block read migrated, peer mouse rows,
  conflicts refused, settings save keeps the wheel), `test_toolchain_policy`
  green on Linux. `test_tray_overlay_actions.py` (mouse hook decision,
  registration paths, cleared hotkeys, hook missing) updated, Windows only.
  WebGUI tests (`HotkeyStatus.test.jsx`, `CommandWheelSection.test.jsx`,
  `SettingsPanel.test.jsx`) written but not run here (no `node_modules`):
  `npm test`/`npm run build` are Windows check steps.
- Remote screen preview stage 1 (dev.48), Linux: `test_remote_preview` (25
  tests: settings ranges/types/defaults, message size/fields/types, every
  session transition, stop from every state, the lock with `enabled` and a
  claimed known `device_id`), `test_tray_remote_preview` (11 tests through the
  real `tray.peer_handler` on a socket pair; `tray` imported with a
  `ctypes.windll` stand-in off Windows: hotkey and edge switch -> request ->
  `rejected`/`pairing_required`, return, `edge_cancel`, connection drop and
  shutdown -> `idle`, accept -> `preview_stop`, target refuses every request;
  removing the stop in the `finally` makes the drop test fail),
  `test_web_api_remote_preview` (5), `test_web_api_actions`,
  `test_web_api_request_guard`, `test_web_api_clipboard`,
  `test_web_api_updates`, `test_overlay_actions`, `test_update_safety`,
  `test_service.py`: OK. Not run here (no Windows / no `node_modules`):
  WebGUI `npm test` and `npm run build`, `test_tray_overlay_actions`,
  `test_tray_stream_v2_e2e`, `test_release_packaging.ps1`, PowerShell parsing.
- Copied pictures (dev.47), Linux without Pillow:
  `test_clipboard_image_preview` (27 tests: PNG/DIB source choice, PNG
  trim, V5 alpha kept, ALPHABITFIELDS, unused alpha byte = opaque,
  top-down/bottom-up, palette, 3000x2000 thumbnail under 2 s, paste formats,
  generation with/without Pillow, background service pending/ready/missing,
  cache limit, HTTP descriptor and thumbnail, HTML image-address-only; the
  Pillow paste-format test is skipped here), `test_clipboard_file_preview`,
  `test_web_api_clipboard`, `test_clipboard_events`,
  `test_web_api_request_guard`, `test_clipboard_image.py`,
  `test_clipboard_html.py`, `test_clipboard_sync.py`: OK. Not run here (no
  Windows / no `node_modules` / no Pillow): reading and setting the real
  Windows clipboard, the Pillow paste formats, WebGUI `npm test`
  (`ClipboardFilePreviews.test.jsx`) and `npm run build`.
- WebGUI warnings, buttons and fields (dev.46): WebGUI change plus the
  receiver journal-order fix in `clipboard_streaming_v2.py`:
  `test_clipboard_stress_v2` (10 tests, 207 s), `test_clipboard_resume_v2`,
  `test_clipboard_transfer_control_v2`, `test_clipboard_streaming_v2` green on
  Linux (except the known Windows-only fsync count). WebGUI tests written/updated (`HotkeyStatus.test.jsx`,
  `SettingsPanel.test.jsx`, `Dashboard.test.jsx`, `DisplayConfig.test.jsx`,
  `CommandWheelSection.test.jsx`, `SoftwareUpdateSection.test.jsx`) but not
  run here (no `node_modules`, npm not available to the build agent): WebGUI
  `npm test` and `npm run build` are open Windows check steps.
- Clipboard file previews (dev.43), Linux without Pillow:
  `test_clipboard_file_preview` (30 tests: sniffing GIF/APNG/WebP
  animation, PNG encoder, GDI pixel conversion, generation with a fake
  shell, bounded reads of huge files, locked/missing/changed files, cache
  limit/expiry/leftovers, background worker, HTTP descriptor/image/Range
  routes, settings; the Pillow scaling test is skipped here),
  `test_web_api_clipboard`, `test_web_api_actions`,
  `test_clipboard_delete_selected`: OK. Full discovery: 962 tests, only the
  known non-Linux cases fail (`test_clipboard_gif` and
  `test_overlay_lifecycle` exit at import without Pillow/Windows, the two
  Linux-only fsync/hardlink tests). Not run here (no Windows / no
  `node_modules` / no Pillow): the Pillow test, Windows shell thumbnails and
  icons, WebGUI `npm test` (`ClipboardFilePreviews.test.jsx`) and
  `npm run build`, `test_release_packaging.ps1`.
- Clipboard file previews, review round 3: every default and range lives
  only in `clipboard_model.DEFAULT_CLIPBOARD_SETTINGS` /
  `FILE_PREVIEW_INT_LIMITS` (`file_preview_animation`, `_video_hover`,
  `_cache_mb` 8-4096, `_cache_max_age_days` 30, `_px` 96, `_icon_px` 32,
  `_max_count` 8, `_max_image_source_mb` 64, `_max_animated_source_mb` 32,
  `_max_passthrough_mb` 4, `_max_source_megapixels` 80, `_max_frames` 90,
  `_gif_scan_kb` 2048, `_miss_retry_s` 15, `_response_max_mb` 16);
  `clipboard_file_preview.preview_settings` only converts the normalised
  values to working units, `generate`/`PreviewCache`/the service and
  `web_api` take them from there (no module constants or own clamps). The
  WebGUI settings fields show the server's normalised values without own
  fallbacks or ranges. The resume-journal speed-up and the relaxed
  `test_malformed_frame_burst_is_contained` of round 2 were taken out again
  (separate task); `test_clipboard_stress_v2` passes unchanged here (10
  tests, about 150 s). The request-guard route list includes the two
  read-only file-preview GET routes.

- Clipboard overlay fixes (dev.40), Linux: `test_overlay_dismiss` (drag end
  on release outside/Escape/focus loss, search-field click inside with stale
  placement, settle phase, previous foreground), `test_web_api_clipboard`
  (newest first, source folder root/path, setting validation),
  `test_web_api_actions` (setting saved/read back), `test_clipboard_events`
  (own text write suppressed, no new item, current unchanged), overlay
  modes/shape/native wheel/gesture/actions: OK. Not run here (no Windows /
  no `node_modules`): `test_tray_overlay_actions` (payload field), WebGUI
  `npm test` (`ClipboardOverlay.test.jsx`, `ClipboardView.test.jsx`) and
  `npm run build`.

- Repeated text, HTML, image, file and file-batch copy events create distinct
  persistent history items while content identities and object bytes remain
  deduplicated; each repeated multi-file selection remains one batch item.
- Release packaging includes and imports `overlay_actions.py` from the curated
  staged payload; `packaging/test_release_packaging.ps1` passes.
- Python dependency/toolchain refresh: hashed runtime and audit locks install
  on CPython 3.13; Pillow 13.0.0, CFFI 2.1.1 and pythonnet 3.2.1 publish
  CPython 3.15-compatible Windows artifacts; `pip-audit` reports no known
  vulnerabilities. Toolchain policy, clipboard GIF, overlay foundation and
  overlay lifecycle tests pass; PowerShell parsing and Python compile checks
  are clean. GitHub Actions supplies the pinned CPython 3.15.1 release.
- Python: test_overlay_actions, test_web_api_actions, test_web_api_clipboard, test_web_api_updates, test_overlay_modes, test_tray_overlay_actions, test_tray_stream_v2_e2e (unittest); test_overlay_lifecycle.py, test_overlay_foundation.py, test_service.py, overlay_show_hide_stress_test.py, overlay_ipc_stress_test.py: all OK.
- Hold and swipe (dev.26): `test_wheel_gesture` (30), `test_overlay_dismiss`,
  `test_overlay_modes`, `test_overlay_actions`, `test_web_api_actions`,
  `test_hotkey_status` (99 tests) and `test_overlay_foundation` (110 checks):
  OK on Linux. Not run here (no Windows/Node): `test_tray_overlay_actions`
  (new side button / hold tests), `test_release_packaging.ps1`, WebGUI
  `npm test` (new trigger and aim tests) and `npm run build`.
- Clipboard cards: `test_web_api_clipboard.py` (8 tests), changed-module
  compile checks, WebGUI `npm test` (40 tests), `npm run build`, and
  `git diff --check`: OK. Linux skips the 13 Windows-only tray overlay tests.
- Installer/WebGUI consistency (dev.23): `test_webgui_health` (11 tests incl.
  overlay give-up/backoff), `test_web_api_actions`, `test_web_api_clipboard`,
  `test_web_api_updates`, `test_tray_overlay_actions` (Windows-only cases
  skipped on Linux), Python compile: OK. Not run here (no Windows/Node 24):
  PowerShell parsing, `test_release_packaging.ps1`, `test_update_flowshift.ps1`,
  `test_overlay_lifecycle.py`, WebGUI `npm test` (new `Dashboard.test.jsx`) and
  `npm run build`.
- Transfer telemetry: `test_clipboard_transfer_control_v2` (25 tests),
  `test_clipboard_transport_v2` (28 tests), WebGUI `npm test` (41 tests), Python
  compile and `git diff --check`: OK. Stream V2 now persists all per-file
  `storage_state=verified` transitions in one contiguous journal generation;
  the latest 200-file batch stress passes in 101.937 s instead of exceeding 240 s.
- WebGUI baseline before this slice: npm test (34 tests) and npm run build: OK.
- Dependency refresh on Node 24.18.1 / npm 12.0.2: clean `npm ci`, 34 tests,
  production build, and `npm audit --include=dev --audit-level=high` with zero
  findings. Linux Python discovery completed 677 tests in 454.650 s; five
  assertions and the Windows named-pipe lifecycle module remain non-green on Linux.

- Update channel (dev.29, source states from Forgejo main):
  `test_update_channel` (28: commit binding, unsafe archive paths, token only
  to Forgejo, VPN off, Node probe, held automatic install),
  `test_update_manager`, `test_update_handoff`, `test_update_stress`,
  `test_update_runtime`, `test_web_api_updates`, `test_update_client.py`,
  `test_update_safety.py`, `test_version_config.py`: OK on Linux. Not run here
  (no Windows/Node 24): WebGUI `npm test` and `npm run build`, PowerShell
  parsing, `test_update_flowshift.ps1`, `test_release_packaging.ps1`, a real
  source update on Windows (check steps in `docs/install_test_checklist.md`).
- Update "access denied" fix (dev.31): `test_update_handoff` (runner
  `cwd` outside the program folder, `not_elevated`), `test_update_manager`
  (Viktor's dev.29 -> dev.30 state: plan removed, `update_install_failed` /
  `program_folder_in_use`, `last_install`; failure causes),
  `test_update_channel`, `test_web_api_updates`: OK on Linux. Not run here (no
  Windows/Node): PowerShell parsing, `test_update_flowshift.ps1` (new: busy
  folder retried, permanently busy folder keeps and restarts the old
  version), WebGUI `npm test` (new last-install tests) and `npm run build`,
  a real update on Windows.

- Install lock and safe replace (dev.35; 21.11. two update runs and a manual
  install of an older checkout ran at once, one removed the other's source
  and rollback copy, FlowShift was gone): `test_install_lock` (lock held =
  sharing violation, busy runner ack, handoff stops before writing the plan,
  install blocked while locked, first start after a manual install waits one
  check interval), `test_installer_python_probe` (staging before stop, swap
  after the check, lock before step 1, marker before start, previous folder
  removed only after the final check), `test_update_manager`,
  `test_update_handoff`, `test_update_channel`, `test_update_stress`,
  `test_web_api_updates`, `test_tray_overlay_actions`: OK on Linux (152
  tests). Not run here (no Windows/Node): PowerShell parsing,
  `test_update_flowshift.ps1` (new: held lock -> `busy` without changes,
  lock free after a run, rollback failure keeps the copy and notifies, missing
  copy leaves the folder untouched, failed start and failed backup bring the
  old version back, older copies removed only after success),
  `test_release_packaging.ps1` (`install_lock.py` in the payload), WebGUI
  `npm test` (`install_busy` hint) and `npm run build`, a real install and
  update on Windows (`docs/install_test_checklist.md` L).

- Origin check and handler table (dev.43, merged with the dev.42 request
  guard), Linux: `test_web_api_actions` (`OriginCheckTests`: foreign/`null`
  Origin, foreign Host, `text/plain`, missing Content-Type, chunked, oversize
  body on 18 writing routes with a counting fake for execute, inject,
  save_config and the other runtime refs, all counters 0; missing Origin with
  local Host is refused (`origin_required`) on every route; WebGUI Origin runs
  execute/inject/save; WebGUI DELETE with JSON type; body with
  channel/origin/confirm is 400), `test_overlay_actions` (`HandlerTableTests`),
  `test_web_api_request_guard`, plus `test_web_api_updates`,
  `test_web_api_clipboard`, `test_clipboard_delete_selected`,
  `test_update_channel`, `test_overlay_native_wheel` green.
  Mutation check: with the origin check disabled the new tests fail.
  Windows only (not run here): `test_tray_overlay_actions` (channel cases),
  `test_release_packaging.ps1` (`action_handlers.py` in the payload), manual
  steps in `MANUAL_TEST_CHECKLIST.md` "Web-API origin check".

- Native Command Wheel (dev.36): `test_overlay_native_wheel` (27: sector hit
  test incl. gaps/hub/corners, single-slot ring, paging, aim over hover,
  nothing drawn beyond the outer radius, hub only pill + two dots, error
  label, host shows the wheel natively without region, clipboard back on
  WebView2, aim/hide/paging routed natively, fallback to WebView2 on start or
  paint failure, action POST incl. 409/400/unreachable),
  `test_overlay_window_shape`, `test_overlay_dismiss`, `test_overlay_modes`,
  `test_overlay_foundation`, `test_toolchain_policy`: OK on Linux. Not run here
  (no Windows): the real layered window (GDI+/`UpdateLayeredWindow`), the
  pixel check on the desktop, `test_overlay_lifecycle.py`,
  `test_tray_overlay_actions` Windows cases, `test_release_packaging.ps1`
  (`overlay_native_wheel.py` in the payload), PowerShell parsing.

- Clipboard multi-select (dev.41): `test_clipboard_delete_selected` (12:
  store/manager delete of selected ids, pinned deleted when selected,
  All with pinned entries + Delete leaves the history empty after restart,
  current cleared, history empty after restart, endpoint validation, origin
  and content-type guard, `local` fallback), `test_web_api_clipboard`,
  `test_clipboard_semantics`, `test_clipboard_materialize_v2`, `test_clipboard`:
  OK on Linux. `test_clipboard_object_store_v2.test_no_second_full_read_in_normal_publication_commit_and_availability`
  fails on Linux independently of this change (POSIX rehash path; file not
  touched). Not run here (no `node_modules`): WebGUI `npm test`
  (`clipboardSelection.test.js`, `ClipboardSelectionBar.test.jsx`, the
  All-with-pinned and All + Delete cases in `ClipboardOverlay.test.jsx` and
  `ClipboardView.test.jsx`) and `npm run build`; the overlay on Windows.

- Web-API request guard (dev.42, expert round on Kanban card 725): before,
  only `update/*`, `clipboard/delete` and `copy-text` checked Host/Origin;
  `actions/execute`, `actions/wheel`, `inject/*`, settings, peers, overlay,
  shutdown etc. did not, so any web page could post to `127.0.0.1:5000`.
  Now `web_api.request_rejection()` runs in `do_POST`/`do_DELETE` before the
  router `_route_post` and in `do_GET` for `/api/`: local `Host` (DNS
  rebinding), POST/DELETE also `Origin` = own WebGUI (API port, WebGUI port,
  5173; missing Origin refused), `application/json`, no chunked body. Refusal:
  403/415/400 with `error_code`, log `web_api rejected <METHOD> <path>
  reason=<code>` (no query/body). The WebGUI has no token. WebGUI `del()` now
  sends `Content-Type: application/json`; the native Command Wheel
  (`overlay_host._native_execute`) sends `Origin` of its overlay URL.
  `test_web_api_request_guard` (every POST/DELETE route x foreign origin, null
  origin, no origin, foreign Host, foreign port, text/plain, form, no type:
  refused, nothing reaches the runtime; WebGUI/localhost/Vite origins pass;
  native wheel click through the real guard; route inventory fails on a new
  unlisted route), `test_web_api_updates`, `test_web_api_clipboard`,
  `test_clipboard_delete_selected`, `test_web_api_actions`,
  `test_update_channel`, `test_overlay_native_wheel`, `test_service.py`: OK on
  Linux. Not run here: WebGUI `npm test`/`npm run build`, the live WebGUI and
  wheel on Windows.
- Stable channel without Git/Node/Forgejo (dev.52): `test_update_stable_path`
  (real discovery, download, manager and handoff against a fake GitHub that
  fails on any other host, an Authorization header, the Forgejo token
  provider or the Node.js probe: new-install default stable; Check/Download/
  Install by hand from `0.7.0-dev.50` to `0.7.0`; offline check with reason;
  no prerelease on stable; notify/download/install pick up a release
  published after start via the periodic check; check-on-start off waits for
  the interval; switch dev -> stable hides Forgejo errors and re-checks
  GitHub; every stable error has a WebGUI hint; Setup payload covers every
  imported module; Setup/updater call no git/node), `test_publish_github_release`
  (bare stand-in repos: plan read-only, publish needs approval and the planned
  version, pushes release commit + tag to both without touching Forgejo main,
  diverged GitHub blocks; verify), plus `test_update_manager`,
  `test_update_handoff`, `test_update_channel`, `test_web_api_updates`,
  `test_update_stress`, `test_update_runtime`, `test_update_safety`,
  `test_update_client`, `test_version_config`: OK on Linux. Not run here (no
  Windows/vitest): WebGUI `npm test` (`SoftwareUpdateSection.test.jsx`, four
  new cases) and `npm run build`, PowerShell parsing of the changed
  `install_flowshift.ps1` message, a real GitHub update on Windows.

## Open work

- Full release regression and the `v0.7.0` tag workflow remain open in
  `TODO_CURRENT.md`.
- Manual hardware and VM checks remain open in `TODO_CURRENT.md`, including
  the Phase 3 two-device matrix in `MANUAL_TEST_CHECKLIST.md`.
- Development channel: needs no build infrastructure (source states of
  `main`); open are only the Windows check steps in `TODO_CURRENT.md`.
- Stable publication: ready as one step (`scripts/publish_github_release.py`);
  waiting for Viktor's decision whether and when to publish, and whether a
  "pre-release" channel (GitHub prereleases) is wanted; see `TODO_CURRENT.md`.
- Remote screen preview stages 2 to 4 (capture/encoding, image channel,
  preview window) wait for device pairing; see `TODO_CURRENT.md`.
- Update from a connected device: questions for Viktor (development versions
  on Stable PCs, automatic levels before secured pairing) and the Windows
  checks are in `TODO_CURRENT.md`. Known limits: the checksums prove a
  complete, unchanged transfer, not the builder (until pairing, Kanban card
  722); the receiving PC needs PyPI for the Python packages.

## Next planned phase

- None defined after Phase 4.
