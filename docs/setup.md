# Setup Guide (current Python prototype)

The productive FlowShift today is a **Python prototype for Windows** distributed
through a packaged Windows setup. There is no video viewer or Tauri GUI yet.

## Requirements

- Windows 10/11
- 64-bit CPython 3.11-3.15 (the installer can securely install Python 3.15.1
  from python.org when missing)
- All devices on the same LAN (Ethernet recommended)
- Firewall must allow **TCP 45781** (peer link) and **UDP 45781** (discovery)

## Install

### Option 1 - packaged setup (recommended, Windows x64)

1. Download the latest stable
   [FlowShift-Setup.exe](https://github.com/CryseXIII/flowshift/releases/latest/download/FlowShift-Setup.exe).
2. Run it and accept the UAC prompt.
3. It installs to `%ProgramFiles%\FlowShift`, keeps config/logs in
   `%ProgramData%\FlowShift`, creates Desktop + Start Menu shortcuts, and
   registers a **user-session Scheduled Task** (`FlowShift`, AtLogOn) that runs
   the runtime in your interactive session (where input hooks + `SendInput`
   work). It starts the runtime immediately and verifies the control socket.

The initial release is not code-signed and may trigger Windows SmartScreen.
Verify its SHA-256 against `SHA256SUMS.txt` from the same GitHub Release.

> **Important — Session 0 / services:** input forwarding must run in the
> interactive user session. A Windows *service* (session 0) CANNOT capture or
> inject interactive input. The installer therefore does NOT install a service by
> default; the primary path is the scheduled task above. `-WithNssm` adds an
> OPTIONAL helper service (manual start) that is explicitly NOT the input path.
> The GUI shows a red `Session: 0 (Dienst)` warning if the runtime ever runs in
> session 0, and refuses to run the live test there.

The packaged setup contains prebuilt WebGUI assets. Node.js, npm, and Vite are
build-time dependencies only and are not required on the target machine.

### Option 2 - source installer

1. Clone or copy the repository.
2. Double-click `install_flowshift.bat`. It installs or upgrades the program
   and the WebGUI together (the WebGUI is mandatory). Run it over an older
   installation without uninstalling first: it stops a running FlowShift,
   replaces program and WebGUI files (no old WebGUI assets remain), keeps
   `%ProgramData%\FlowShift` (config.json, clipboard history, logs), starts
   FlowShift again and verifies that the running runtime reports the same
   `VERSION` as the WebGUI and serves `index.html` and `overlay.html`.
   It asks nothing except the Windows UAC prompt; `-Interactive` enables
   confirmation questions.

   The WebGUI is taken from the prebuilt, version-stamped `webgui/dist` (no
   Node.js needed). Only if that build is missing or belongs to another
   version and Node.js 24+ is already installed, the internal helper
   `install_webgui.ps1` rebuilds it. There is no separate WebGUI installer or
   uninstaller anymore.

### Option 2b - USB stick package (other PCs without Git, Node.js or Forgejo)

Since `0.7.0-dev.53`. Simple steps:

1. On your own PC (update channel "Development"): WebGUI → **Settings →
   Software Update → Create USB package**. After a few minutes the page shows
   `Saved: ...\Downloads\FlowShift-<version>-usb.zip`, for example
   `FlowShift-0.7.0-dev.53-usb.zip`. It is the version installed on this PC.
2. Copy the ZIP to a USB stick.
3. On the other PC: unpack the ZIP, open the unpacked folder, double-click
   `install_flowshift.bat`, accept the Windows prompt. Done.

An older FlowShift on the other PC is updated by the same double-click, with
the same lock and the same fallback to the old version on an error as every
installation; settings and clipboard history stay.

What the ZIP contains: the program, the installer/updater/uninstaller, the
ready-built WebGUI (`webgui/dist`, so no Node.js), and the folder `offline`
with the official Python installer (same version and SHA-256 as the
installer uses), pip and all Python packages as Windows wheel files for every
supported Python version that has them. The installer checks every file there
against the SHA-256 in `offline/usb_package.json`, takes Python and packages
only from the stick (`pip --no-index`) and downloads nothing from Forgejo,
python.org or PyPI. Why everything is bundled: the other PCs may have no
internet or a filtered one, and a half-finished installation is worse than a
bigger ZIP (about 70-100 MB). If the PC already has a Python for which the
stick has no packages, the bundled Python is installed next to it and used for
FlowShift.

Not bundled: the Microsoft WebView2 runtime (about 150 MB; part of Windows 11
and current Windows 10). If it is missing, the installer tries to download it
and otherwise says plainly that the PC has to be online once.

After the installation the PC uses the update channel **Stable (GitHub)**
(set by the installer even if an older configuration said Development) and
never contacts Forgejo. Its version is often newer than the latest GitHub
release; Software Update then says in one sentence that there is nothing to
do. FlowShift never installs an older version.

The button exists only on the development channel of an installed FlowShift
(not in a development checkout). It packs what is installed in
`C:\Program Files\FlowShift`; an installation older than `0.7.0-dev.53` has no
installer scripts in its program folder yet, then the page asks to install the
current version once more. The work folder
`%ProgramData%\FlowShift\updates\usb-build` is removed after every run and by
the uninstaller; the ZIP in Downloads stays.

**When a download fails** (since `0.7.0-dev.55`). The Python installer is
counted while it arrives (size and SHA-256) and compared with the
`Content-Length` of python.org, because a broken connection otherwise looks
like a normal end. Every download is tried twice. If the Python installer is
still wrong, the page names the cause (`incomplete`: connection broke off;
`not_installer`: a web page of a proxy, firewall or web filter came back;
`changed_on_disk`: the bytes arrived right but the file was changed on disk
afterwards, usually a virus scanner; `http_error`, `network`/`timeout`;
`wrong_bytes`), the expected size (`$PythonInstallerSize` in
`install_flowshift.ps1`) and SHA-256, what arrived (size, shortened SHA-256,
HTTP status, content type) and the path of `flowshift.log`, which holds one
line per try (`usb package python download try N: cause=... http=... type=...
content_length=... received=... sha256=... stream_sha256=...`, plus proxy and
redirect if any). The way out keeps the check: click the
`python-3.15.1-amd64.exe` link under the message, save the file with the
browser in Downloads, click **Use downloaded Python installer**. The build
then takes the newest `python-3.15.1-amd64*.exe` in Downloads whose SHA-256 is
the pinned one (also `... (1).exe`), refuses any other file with its size and
SHA-256, and checks the copy in the work folder once more. The API accepts
only the fixed body `{"python_installer": "downloads"}`, never a path. pip and
the wheel sets are tried twice too (the bundled Python's set; other Python
versions without wheels on PyPI are skipped after one try), and a file that is
not a real wheel (a proxy page) counts as a failure; the message names the
last pip lines and asks to check access to `pypi.org` and
`files.pythonhosted.org`.

**Packages that PyPI has only as source** (since `0.7.0-dev.56`). Before
anything big is downloaded, the build asks the PyPI JSON API
(`https://pypi.org/pypi/<name>/<version>/json`) for every line of
`requirements.txt` and every supported Python (3.11-3.15) whether a locked
file (SHA-256 in `requirements.txt`) is a wheel for Windows amd64
(`usb_package.wheel_report`). `proxy-tools==0.1.0` (needed by pywebview)
exists only as sdist, so `pip download --only-binary=:all:` alone ended with
"No matching distribution found for proxy-tools==0.1.0" (Viktor 24.11. 22:38).
Such a package is built once with `pip wheel --no-deps --require-hashes
--no-binary <name>` (pip checks the sdist against `requirements.txt` first) and
must come out as a pure-Python `-none-any` wheel; otherwise it would need a
compiler on the other PC and the Python versions concerned are skipped (for
the bundled Python the build stops). `offline/requirements-offline.txt` is
`requirements.txt` plus the SHA-256 of each built wheel (nothing else changes;
`--check-package` and the installer compare it), `usb_package.json` lists it
under `requirements` and the build under `built_from_source` (sdist and wheel
SHA-256). The wheel sets are downloaded with that file and `--find-links` to
the built wheels, and the installer runs `pip install --no-index --find-links
<offline\wheels\cp3XY> --require-hashes -r offline\requirements-offline.txt`.
Messages name the package and version: `wheels_missing` (no Windows wheel and
no buildable source; trying again does not help, FlowShift needs another
version in `requirements.txt`), `source_build_failed` (building failed,
usually network; check `pypi.org`/`files.pythonhosted.org`),
`pypi_unreachable`, and `wheels_download_failed` names the package from pip's
"No matching distribution found for ...". `src/python/test_usb_package_pypi.py`
runs all of this against the real PyPI (see `docs/install_test_checklist.md`
N3).

### Option 3 - manual / dev

1. Copy the `src/python/` folder to **every** device.
2. Optionally edit `config.json` (or `config.example.json` as a template), or
   configure everything from the GUI.
3. Set `FLOWSHIFT_CONFIG` / `FLOWSHIFT_LOG_DIR` to keep runtime data out of the
   repo (the installer does this automatically).

## Run

Settings: use the **WebGUI** (tray menu → Web GUI, served locally on
`127.0.0.1:5000`). It is the settings interface of FlowShift; new settings are
added only there.

Legacy Tkinter GUI (kept until the WebGUI covers the remaining tabs, see the
staged plan in `TODO_CURRENT.md`; no new settings are added here):
```cmd
python gui.py
```
- Tab **Geräte**: this device (name/port) and peers (add/edit/remove/scan)
- Tab **Profile**: activate/deactivate forwarding, ping, live status
- Tab **Hotkeys**: configure the key combinations
- Tab **Steuerung**: start/stop the runtime + log + capture region

Productive runtime directly (this is what actually forwards input):
```cmd
python tray.py --tray
```

> Input hooks and `SendInput` injection generally require the runtime to run
> with the **same privilege level** as the target apps. Run as Administrator if
> forwarding into elevated windows.

## First configuration

1. Start FlowShift on **all** devices (`tray.py --tray`, or via the GUI).
2. Open the WebGUI → devices are discovered on the LAN (or add a peer by IP).
3. Assign hotkeys (WebGUI Settings; the legacy Tkinter tab "Hotkeys" still
   works). Each peer gets a `Ctrl+Alt+<n>` forward hotkey;
   `Ctrl+Alt+0` returns input to the local machine.
4. Press a peer's hotkey to forward keyboard/mouse to it; press it again or
   `Ctrl+Alt+0` to return.

## Emergency stop

Kill switch: **Ctrl+Alt+Shift+Win+F12** stops forwarding and quits the runtime.

## Updates and rollback

The WebGUI Software Update section can notify, download automatically, or
download and install automatically. It shows the installed version, the
channel, the source, the last check and the last successful check.

- Channel **Stable** is the default for every new installation and the way all
  PCs except Viktor's development laptops get updates. It accepts only stable
  releases from `CryseXIII/flowshift` (GitHub `releases/latest`) and needs
  nothing on the PC but internet: **no Git, no Node.js, no Forgejo access**
  (the Setup.exe contains the prebuilt WebGUI). FlowShift checks GitHub at
  start and every 6 hours while it runs, so a new release reaches a running PC
  without a restart:
  - policy `install`: downloads, verifies and installs it by itself (when
    FlowShift is idle) and restarts;
  - policy `download`: downloads and verifies it, then waits for a click on
    Install;
  - policy `notify` (or automatic updates off): Settings > Software Update shows
    "Update available"; click **Check**, **Download**, **Install** one after
    another.
  The three steps are shown as a row with a tick (done), a cross (failed) or
  an arrow (next). After each click the page says what came out ("Check
  finished: FlowShift 0.7.0 is available. Next step: Download.") or, in plain
  words, what went wrong and what to do (no internet, GitHub busy, checksum
  wrong, ...). During Install FlowShift closes and starts again after about a
  minute; the page says so and reconnects by itself.
  A PC that once ran a development build (`0.7.0-dev.N`) also installs the next
  stable release through this channel; prereleases are never installed.
- Channel **Development** follows branch `main` of `private-platform/flowshift`
  in the own Forgejo (`http://forgejo.thegoodhunter.vps`, own network or VPN
  only). There is no package build: FlowShift reads the newest `main` commit
  and its `VERSION` every 15 minutes and right after switching. For a newer
  version it downloads the source archive of exactly that commit, checks it
  (commit ID in the ZIP comment, no paths outside the archive, matching
  `VERSION`, size limits), and installs it with the `install_flowshift.ps1`
  it contains (`-FlowUpdate -NonInteractive`). No Git and no manual download
  or unpacking on the PC.
- Because `webgui\dist` is not in the repository, a development update builds
  the WebGUI itself: Node.js 24 or newer (LTS) must be installed. Without it
  the page says so before FlowShift is stopped.
- The channel needs a Forgejo access token with read access to the repository,
  entered once in the WebGUI and kept in the Windows Credential Manager
  (`FlowShift:forgejo-update`), never in `config.json`, the repository or a
  script. It is only sent to the Forgejo address. Without network or VPN the
  check is skipped quietly and shown as "Forgejo is not reachable" with the
  last successful check.
- A development state whose installation failed and was rolled back is not
  retried automatically; Install retries it, the next merged state is
  installed normally.
- One-time switch from FlowShift older than `0.7.0-dev.29`: download the
  current `main` from Forgejo as ZIP once, unpack, double-click
  `install_flowshift.bat`, then choose channel Development and enter the
  access in the WebGUI.

Switching from a development build back to Stable installs nothing until a
stable release newer than the installed version exists (`0.7.0` is newer than
every `0.7.0-dev.N`). Right after the switch the page shows "Not checked yet"
instead of the development result, never a Forgejo error, and FlowShift checks
GitHub at once. FlowShift verifies release metadata,
installer size, and SHA-256 before installation. Automatic installation waits
for forwarding, input, overlay, and clipboard activity to become idle.

Installation runs in a separate elevated process. It backs up user JSON and the
Scheduled Task, keeps the previous `%ProgramFiles%\FlowShift` directory as a
temporary rollback, starts the new runtime, and checks its control socket, API,
version, and WebGUI. A setup or health failure restores the prior installation.
`%ProgramData%\FlowShift` remains the persistent user-data location. Every
update run is logged to `%ProgramData%\FlowShift\logs\update.log`; the steps of
a development (source) installation are in `install.log`.

The update runs from `%ProgramData%\FlowShift\updates`, never from inside the
program folder, and stops processes still running from
`%ProgramFiles%\FlowShift` (overlay host, legacy settings window) before it
moves the folder aside; a folder that stays busy is retried a few times. If it
still fails, the previous version keeps running and Software Update says in
plain words what happened and what to do. Updates need the administrator
rights of the `FlowShift` autostart task; the desktop shortcut starts FlowShift
through that task, so no UAC prompt appears. FlowShift `0.7.0-dev.29` and
`0.7.0-dev.30` cannot update themselves past this point ("access denied"):
install `0.7.0-dev.31` or newer once with `install_flowshift.bat`.

Never two installations at once (since `0.7.0-dev.35`): `install_flowshift.ps1`
(by hand, from Setup.exe or inside an update), `update_flowshift.ps1`, the
updater inside FlowShift and the uninstaller share one lock,
`%ProgramData%\FlowShift\updates\install.lock`, held open for the whole run
(Windows releases it when the process ends, also after a crash). A manual
installation that starts during an update waits for it (up to 20 minutes); an
update that finds the lock taken changes nothing, FlowShift keeps running and
tries again at its next regular check (Software Update: "Another FlowShift
installation or update was running").

Check first, then replace: `install_flowshift.ps1` copies the new program
files to `%ProgramFiles%\FlowShift.staging` and checks them (required files,
`VERSION`, file count) while the old FlowShift still runs. Only then is the
program folder swapped. A manual run keeps the previous folder as
`%ProgramFiles%\FlowShift.previous` until the new version answered the final
check and moves it back on any failure; during an update the update runner
keeps `%ProgramFiles%\FlowShift.rollback-<version>`. The rollback copy is only
ever moved back, never deleted on a failure, and copies of older versions are
removed only after the new version runs. If the previous version cannot be
started again, a Windows message says where the copy is and to run
`install_flowshift.bat` once more.

After a manual installation FlowShift does not update itself right away: the
first start waits one regular check interval (15 minutes) before its first
automatic check. A manual installation from an older folder says that a newer
version is installed or known, installs what was asked for, and the automatic
update brings the newer version back at that next check.

Development checkouts may check and download releases, but cannot install them.

## Publishing a new version for all PCs

Forgejo `main` is where FlowShift is developed; the other PCs only see GitHub
releases. Bringing the current Forgejo state to them is one step,
`scripts/publish_github_release.py`, and it runs **only after Viktor approved
exactly this version** (AGENTS.md 0.3 and 13). It needs Git and push rights to
both repositories on the machine that publishes, never on the PCs.

1. Overview, changes nothing (fetch only):
   `python3 scripts/publish_github_release.py plan`
   Shows the Forgejo `main` commit and its `VERSION` (`0.7.0-dev.N`), the
   version it would become (`0.7.0`), the last GitHub release, every commit
   since then, and anything that blocks (tag exists already, version not newer,
   GitHub `master` not a fast forward of `main`).
2. Before asking Viktor: run the full regression of AGENTS.md section 5 "Before
   a stable release" (Windows lanes included) and list what is new, untested or
   known broken.
3. With Viktor's approval:
   `python3 scripts/publish_github_release.py publish --version 0.7.0 --freigabe "Viktor <date> <where>"`
   It creates the release commit (`VERSION` = `0.7.0`, the approval in the
   commit message) on top of Forgejo `main` in a temporary worktree, the tag
   `v0.7.0`, pushes both to Forgejo (branch `release/v0.7.0` and the tag) and
   then to GitHub (`master` as fast forward, never forced, and the tag).
4. Build and publish the release **on the server, without GitHub Actions**
   (the GitHub account is locked for billing and nothing on GitHub may cost
   money; GitHub releases themselves are free):
   `/srv/opencode/autopilot/bin/flowshift-github release assets --version 0.7.0 --freigabe "Viktor <date> <where>"`
   `scripts/release_build.py` builds from a clean checkout of the tag: Python
   unit tests, WebGUI `npm ci` / `npm audit` / `npm test` / `npm run build`
   (Node.js 24.18.1, npm 12.0.2), the curated payload with the lists of
   `packaging/build_release.ps1` and the rules of
   `packaging/test_release_packaging.ps1`, `FlowShift-Setup.exe` with Inno
   Setup 6.7.4 under Wine 10 (no root, headless), `update-manifest.json` and
   `SHA256SUMS.txt`. The script checks the three files with the updater's
   validation, uploads them into a draft release, downloads them back and
   compares the hashes, and only then publishes the release as latest and runs
   `verify --download-setup` on `releases/latest`. A published release is never
   replaced; a failed run leaves an invisible draft that the next run reuses.
   The toolchain is installed once under `~/.cache/flowshift-release-tools`;
   every download is pinned by SHA-256 (Debian packages from the signed
   trixie index, Inno Setup from the official jrsoftware release, Node.js from
   nodejs.org). The full build log is in the printed work folder.
   `flowshift-github release probe` proves without any trace that the token
   may create releases and upload assets (a draft that is deleted again).
5. Check anytime, read-only:
   `python3 scripts/publish_github_release.py verify --version 0.7.0 --download-setup`
   checks `releases/latest` with the same validation an installed FlowShift
   uses (tag, assets, manifest, checksums, Setup.exe hash).
6. On a Windows PC: download `FlowShift-Setup.exe` from the release and install
   it once over an existing installation; the server cannot run the installer.
7. Merge `release/v0.7.0` into Forgejo `main` keeping main's content
   (`git merge -s ours origin/release/v0.7.0`, only `VERSION` differs) and
   set the next development version `0.7.1-dev.1`. Without this merge GitHub
   `master` is no ancestor of `main` and `plan` blocks the next publication
   (it names this command).

`.github/workflows/release.yml` no longer starts on tags; it can only be run
by hand on GitHub and would only work again with a paid or unlocked account.

PCs with policy `install` then update by themselves within 6 hours (or at
their next start); the others see "Update available" in Software Update.

## Troubleshooting

| Problem | Fix |
|---|---|
| Device not found | Check firewall (UDP/TCP 45781), same subnet |
| Cannot connect | Confirm the peer runs `tray.py --tray`, correct IP/port |
| Hotkey shows "(ungültig)" | Its target peer was removed; reassign or reset hotkeys |
| Input not injected into elevated app | Run the runtime as Administrator |

## Not available yet

- `flowshift-viewer.exe` (no video receiver; `src/viewer` is a stub)
- `flowshift-service.exe` (Rust service is experimental and does not build)
- video/monitor matrix

## Linux (planned, not working yet)

Cross-platform support (Linux↔Linux, Windows↔Linux) is **prepared but not
functional**. The protocol, capability handshake, backend abstraction and key
mapping are in place, but the Linux input backend is still a stub
(`src/python/input_backends/linux_stub.py`).

When implemented, the Linux backend will:
- capture via **evdev** (`/dev/input/event*`) and inject via **uinput**
  (`/dev/uinput`) — desktop-agnostic, working under both X11 and Wayland;
- require **device permissions**: read access to `/dev/input/event*`, read/write
  to `/dev/uinput` (via a udev rule + group, or a small privileged helper /
  systemd service).

Do not expect Linux forwarding to work until this is actually implemented and
tested. Full plan: [linux_backend_plan.md](linux_backend_plan.md).
