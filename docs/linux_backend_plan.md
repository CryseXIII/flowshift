# Linux Backend Plan (evdev / uinput)

Status: **planned, not implemented.** Today `src/python/input_backends/linux_stub.py`
reports honestly that it cannot capture or inject and never crashes. This
document defines how the real Linux↔Linux backend will work.

## Goal

Linux↔Linux (and Windows↔Linux / Linux↔Windows) input sharing using the shared
FlowShift protocol. The Linux backend must be **desktop-agnostic**: it does not
rely on X11 (`xdotool`) or Wayland (`wtype`) helpers as its foundation. Those may
become *optional* fallbacks later, but the fundament is `evdev` + `uinput`.

## Capture (evdev)

- Read events from `/dev/input/event*` via `evdev`.
- Enumerate keyboards and mice/touchpads; open the relevant devices.
- **Filter out FlowShift's own virtual devices** (see injection) to avoid feedback
  loops. Match on the uinput device name we create.
- Optional **exclusive grab** (`EVIOCGRAB`) of the real devices while forwarding,
  so local apps do not also receive the input that is being sent to a peer.
- Translate evdev codes to the canonical key names via
  `keymap.evdev_to_canonical` and emit platform-neutral events
  (`input_events`), carrying `native_code` for loss-less same-OS transport.

## Injection (uinput)

- Create virtual devices via `/dev/uinput`:
  - a virtual **keyboard** (all `KEY_*` we support),
  - a virtual **mouse** (relative `REL_X`/`REL_Y`, `REL_WHEEL`, `BTN_LEFT/RIGHT/MIDDLE`),
  - optionally a virtual **absolute pointer** (`ABS_X`/`ABS_Y`) for absolute
    mouse positioning across mismatched resolutions.
- Map incoming events:
  - key: canonical -> `keymap.canonical_to_evdev` (or `native_code` if source os
    is linux) -> `EV_KEY`.
  - mouse move: support both **relative** (`REL_X/REL_Y`) and **absolute**
    (`ABS_X/ABS_Y` normalised to the virtual pointer range) modes.
  - wheel: `REL_WHEEL`.
  - buttons: `BTN_LEFT` / `BTN_RIGHT` / `BTN_MIDDLE` (+ `BTN_SIDE`/`BTN_EXTRA`).
- Emit `EV_SYN`/`SYN_REPORT` after each logical event.

## Permissions

Injection and capture need device access that a normal user usually lacks:

- Read access to `/dev/input/event*` (evdev capture).
- Read/write access to `/dev/uinput` (injection).
- Options, from least to most privileged:
  1. **udev rule + group**: add the user to an `input`/`uinput` group and ship a
     udev rule granting the group access to `/dev/uinput` and `/dev/input/event*`.
  2. **systemd service / small privileged helper** that owns the devices and
     talks to the user-level agent over a local socket.
  3. Running the agent as root (discouraged; only as a last resort).
- The `hello` capabilities already advertise `requires_uinput`, `requires_evdev`
  and `requires_privileged_helper` so a peer knows what is needed.

## Desktop (X11 / Wayland)

- `uinput` devices are seen by the compositor as real input devices, so injection
  works under **both X11 and Wayland** without desktop-specific code.
- No `xdotool` / `wtype` in the core path. Any such helper is an optional
  fallback, never the fundament.

## Cleanup

- On stop / disconnect / shutdown: release all pressed keys and buttons
  (the `PressTracker` model already exists and is reused).
- Ungrab real devices (`EVIOCGRAB` off).
- Destroy the uinput virtual devices (`UI_DEV_DESTROY`) cleanly.

## Tests

- **Unit** (any OS): evdev<->canonical and canonical<->evdev mapping
  (`keymap`), neutral event conversion for key/mouse/wheel/buttons
  (`input_events`). These already exist in `test_service.py`.
- **Integration** (Linux): inject through a real `/dev/uinput` and read it back
  via evdev on a dummy device; assert the round trip.
- **Manual**:
  - X11 session: forwarding keyboard + mouse + wheel + buttons, no stuck keys.
  - Wayland/KDE session: same.
  - Wayland/GNOME session: same.
  - Mixed resolution target: absolute mouse lands correctly.

## Integration into FlowShift

- Implement `LinuxEvdevUinputBackend(InputBackend)` in
  `src/python/input_backends/linux_evdev.py` with real capabilities.
- `input_backends.get_backend("linux")` returns it once available (replacing the
  stub) — only when it is actually implemented and tested.
- The runtime (`tray.py` today is Windows-only) will be generalised, or a new
  cross-platform `flowshift-agent` entrypoint will select the backend via
  `input_backends.get_backend()`.

## Phasing

```
Phase 1: Windows-Python stabilisieren.               (done / ongoing)
Phase 2: Plattformneutrales Protokoll + Capabilities. (this change: prepared)
Phase 3: Linux evdev/uinput backend als eigener Agent/Backend.
Phase 4: Optionaler Rust-Agent für Windows/Linux.
```
